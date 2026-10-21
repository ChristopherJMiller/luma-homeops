#!/usr/bin/env bash
# Deploy check: does every PrometheusRule meant for this Prometheus actually
# load and evaluate? (Luma spec §12.4.)
#
#   scripts/check-prometheus-rules.sh [--would-fire]
#
# Exit 0 = pass, 1 = at least one FAIL (or no groups loaded at all),
# 2 = the check could not run (no cluster access, unreadable API, or a selector
# shape it does not understand). Read-only; safe to run from Monitor.
#
# Why it exists: until 2026-10 the ruleSelector picked PrometheusRule CRDs
# while most of our rules shipped as ConfigMaps, so most alerts -- stock and
# custom -- were silently inert. A rule nobody evaluates looks exactly like a
# rule that never fires.
#
# The convention it enforces: every PrometheusRule for this Prometheus carries
# `release: prometheus` (any namespace). A rule carrying that label, or the
# legacy `prometheus: kube-prometheus` + `role: alert-rules` pair, that the
# live selectors do not pick is reported `FAIL inert`.
#
# How "loaded" is decided: the operator writes each selected object to a rule
# file named exactly `<namespace>-<name>-<metadata.uid>.yaml`, and
# /api/v1/rules reports each group's file. Matching on the uid catches a stale
# file left by a deleted-and-recreated object.
#
# Rule health `unknown` (never evaluated yet) counts as a FAIL, so run this one
# evaluation interval (30s) after the config reloader has picked the change up
# (allow ~3 minutes after an Argo sync for the ConfigMap to reach the pod).
#
# --would-fire additionally runs every alerting rule's expr -- from selected
# AND inert objects -- as an instant query and prints
#   WOULD-FIRE <alert> <severity> <series>
# for each non-empty result (<series> = number of series), followed by up to
# three indented sample series. It ignores `for:`, and it under-reports rules
# that depend on recording rules which are not loaded yet. Use it before
# widening the selector, to see what would page.
set -uo pipefail

: "${KUBECONFIG:=/tmp/galaxy-kubeconfig}"; export KUBECONFIG
NS=prometheus

usage() { echo "usage: $0 [--would-fire]" >&2; exit 2; }
would_fire=0
case "${1:-}" in
  "") ;;
  --would-fire) would_fire=1; shift ;;
  *) usage ;;
esac
[ "$#" -eq 0 ] || usage

die() { echo "ERROR: $*" >&2; exit 2; }
kc() { timeout 60 kubectl --request-timeout=50s "$@"; }

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

# --- 1. selectors, from the live Prometheus CR ------------------------------
kc -n "$NS" get prometheus -o json > "$tmp/prom.json" 2>/dev/null || die "cannot read the Prometheus CR in $NS"
[ "$(jq '.items | length' "$tmp/prom.json")" = 1 ] || die "expected exactly one Prometheus in $NS"
PROM_NS=$(jq -r '.items[0].metadata.namespace' "$tmp/prom.json")

# Prints a k=v,... selector string, or "<null>" for a null selector.
# An empty string means `{}`: matches everything.
selector() {
  jq -r --arg p "$1" '.items[0].spec[$p]
    | if . == null then "<null>"
      elif ((.matchExpressions // []) | length) > 0 then "<matchExpressions>"
      else (.matchLabels // {}) | to_entries | map("\(.key)=\(.value)") | join(",")
      end' "$tmp/prom.json"
}
RULE_SEL=$(selector ruleSelector)
NS_SEL=$(selector ruleNamespaceSelector)
for s in "$RULE_SEL" "$NS_SEL"; do
  [ "$s" != "<matchExpressions>" ] || die "the Prometheus CR uses matchExpressions in a rule selector; this check only understands matchLabels"
done

# Namespace set: JSON array of names, or null for every namespace.
case "$NS_SEL" in
  "<null>") NSSET=$(jq -cn --arg n "$PROM_NS" '[$n]') ;;   # null = Prometheus's own namespace only
  "")       NSSET=null ;;                                  # {} = every namespace
  *)        kc get ns -l "$NS_SEL" -o json > "$tmp/ns.json" 2>/dev/null || die "cannot list namespaces for $NS_SEL"
            NSSET=$(jq -c '[.items[].metadata.name]' "$tmp/ns.json") ;;
esac

# --- 2. selected and all PrometheusRules -----------------------------------
kc get prometheusrules -A -o json > "$tmp/all.json" 2>/dev/null || die "cannot list PrometheusRules"
case "$RULE_SEL" in
  "<null>") echo '{"items":[]}' > "$tmp/sel.json" ;;          # null selector matches no objects
  "")       cp "$tmp/all.json" "$tmp/sel.json" ;;
  *)        kc get prometheusrules -A -l "$RULE_SEL" -o json > "$tmp/sel.json" 2>/dev/null || die "cannot list PrometheusRules for $RULE_SEL" ;;
esac

# --- 3. what Prometheus has loaded ------------------------------------------
POD=$(kc -n "$NS" get pod -l app.kubernetes.io/name=prometheus -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
[ -n "$POD" ] || die "no prometheus pod found; is KUBECONFIG right?"

promget() {   # $1 = API path
  kc -n "$NS" exec "$POD" -c prometheus -- wget -qO- "http://localhost:9090$1" 2>/dev/null
}
promq() {     # $1 = PromQL; POST so the expression needs only form encoding
  kc -n "$NS" exec "$POD" -c prometheus -- \
    wget -qO- --post-data="query=$(jq -rn --arg q "$1" '$q|@uri')" "http://localhost:9090/api/v1/query" 2>/dev/null
}

promget /api/v1/rules > "$tmp/rules.json"
jq -e '.status == "success"' "$tmp/rules.json" >/dev/null 2>&1 || die "cannot read /api/v1/rules"
promq 'increase(prometheus_rule_evaluation_failures_total[10m]) > 0' > "$tmp/evalfail.json"
jq -e '.status == "success"' "$tmp/evalfail.json" >/dev/null 2>&1 || die "cannot query prometheus_rule_evaluation_failures_total"

# --- 4. report -----------------------------------------------------------------
# One jq pass classifies every object and emits the FAIL/WARN lines, then a
# final "SUMMARY selected groups alerting recording" line.
jq -nr \
  --slurpfile all "$tmp/all.json" --slurpfile sel "$tmp/sel.json" \
  --slurpfile rules "$tmp/rules.json" --slurpfile ef "$tmp/evalfail.json" \
  --argjson nss "$NSSET" '
  def base: split("/") | last;
  def labelled: (.metadata.labels // {}) as $l
    | $l.release == "prometheus" or ($l.prometheus == "kube-prometheus" and $l.role == "alert-rules");
  def id: "\(.metadata.namespace)/\(.metadata.name)";

  [ $sel[0].items[] | select($nss == null or (.metadata.namespace | IN($nss[]))) | .metadata.uid ] as $selU
  | ($rules[0].data.groups) as $loaded
  | [ $all[0].items[] | . + {sel: (.metadata.uid | IN($selU[]))} ] as $objs
  | [ $objs[] | select(.sel) ] as $selected
  | [ $selected[] | "\(.metadata.namespace)-\(.metadata.name)-\(.metadata.uid).yaml" as $b
      | { obj: ., groups: [ $loaded[] | select(.file | base == $b) ] } ] as $matched

  | ( $objs[] | select((.sel | not) and labelled) | "FAIL inert \(id)" ),
    ( $matched[] | .obj as $o | .groups as $g
      | if ($g | length) == 0 then "FAIL not-loaded \($o | id)"
        else
          ( $o.spec.groups[]?.name | select(IN($g[].name) | not) | "FAIL missing-group \($o | id)/\(.)" ),
          ( $g[] | .name as $gn | .rules[] | select(.health != "ok")
            | "FAIL unhealthy \($o | id)/\($gn)/\(.name): \(if (.lastError // "") != "" then .lastError else "health=\(.health)" end)" )
        end ),
    ( $ef[0].data.result[] | "FAIL eval-failures \(.metric.rule_group)" ),
    ( $objs[] | select((.sel | not) and (labelled | not)) | "WARN unselected \(id)" ),
    ( [ $matched[].groups[] ] as $mg
      | "SUMMARY \($selected | length) \($mg | length) \([ $mg[].rules[] | select(.type == "alerting") ] | length) \([ $mg[].rules[] | select(.type == "recording") ] | length)" )
  ' > "$tmp/report.txt" || die "could not evaluate the report"

grep -v '^SUMMARY ' "$tmp/report.txt"
read -r _ n g a r < <(grep '^SUMMARY ' "$tmp/report.txt")
echo "selected=$n groups=$g alerting=$a recording=$r"

rc=0
grep -q '^FAIL ' "$tmp/report.txt" && rc=1
[ "${g:-0}" -gt 0 ] || rc=1

# --- 6. --would-fire -------------------------------------------------------------
if [ "$would_fire" -eq 1 ]; then
  # alert <TAB> severity <TAB> ns/name <TAB> url-encoded expr, from selected + inert objects
  jq -nr --slurpfile all "$tmp/all.json" --slurpfile sel "$tmp/sel.json" --argjson nss "$NSSET" '
    [ $sel[0].items[] | select($nss == null or (.metadata.namespace | IN($nss[]))) | .metadata.uid ] as $selU
    | $all[0].items[]
    | select((.metadata.uid | IN($selU[])) or ((.metadata.labels // {}) as $l
        | $l.release == "prometheus" or ($l.prometheus == "kube-prometheus" and $l.role == "alert-rules")))
    | "\(.metadata.namespace)/\(.metadata.name)" as $id
    | .spec.groups[]?.rules[]? | select(.alert)
    | [ .alert, (.labels.severity // "none"), $id, (.expr | tostring | @uri) ] | @tsv
  ' > "$tmp/alerts.tsv" || die "could not list alerting rules"

  # One exec for every query: the pod's shell reads one encoded expr per line
  # and writes one JSON response per line (empty line on a failed request).
  cut -f4 "$tmp/alerts.tsv" > "$tmp/queries.txt"
  # shellcheck disable=SC2016  # $q is expanded by the pod's shell, not this one
  timeout 600 kubectl --request-timeout=590s -n "$NS" exec -i "$POD" -c prometheus -- sh -c '
    while IFS= read -r q; do
      wget -qO- --post-data="query=$q" http://localhost:9090/api/v1/query 2>/dev/null; echo
    done' < "$tmp/queries.txt" > "$tmp/answers.txt" || die "would-fire queries did not complete"
  [ "$(wc -l < "$tmp/queries.txt")" = "$(wc -l < "$tmp/answers.txt")" ] || die "would-fire: got $(wc -l < "$tmp/answers.txt") answers for $(wc -l < "$tmp/queries.txt") queries"

  echo
  fired=0
  while IFS=$'\t' read -r alert sev id _ <&3 && IFS= read -r ans <&4; do
    st=$(jq -r '.status // "error"' <<<"$ans" 2>/dev/null)
    if [ "$st" != "success" ]; then
      echo "WARN would-fire-error $id/$alert: $(jq -r '.error // "no response"' <<<"$ans" 2>/dev/null || echo "no response")"
      continue
    fi
    cnt=$(jq -r '.data.result | length' <<<"$ans")
    [ "$cnt" -gt 0 ] || continue
    echo "WOULD-FIRE $alert $sev $cnt"
    jq -r --arg id "$id" '.data.result[:3][] | "    \($id) {\(.metric | to_entries | map("\(.key)=\(.value)") | join(","))} \(.value[1])"' <<<"$ans"
    fired=$((fired+1))
  done 3< "$tmp/alerts.tsv" 4< "$tmp/answers.txt"
  echo "would-fire: $fired of $(wc -l < "$tmp/alerts.tsv") alerting rules return series now"
fi

exit "$rc"
