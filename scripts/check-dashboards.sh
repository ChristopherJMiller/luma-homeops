#!/usr/bin/env bash
# Run every Grafana panel query in cluster/grafana/dashboards/ against live
# Prometheus and report the ones that return nothing.
#
#   scripts/check-dashboards.sh [dashboard-name ...]
#
# NOT a pre-commit hook: it needs cluster access. Run it after adding panels,
# and after retiring any workload -- a decommissioned service leaves its panels
# silently querying metrics that will never return, which is how this repo
# accumulated ~40 dead panels across 6 dashboards by 2026-09-23 (authentik,
# ingress-nginx, external-dns, readarr, and plain wrong metric names).
#
# THE IMPORTANT PART: it strips `OR on() vector(N)` / `or vector(N)` fallbacks
# before testing. Those make a query that matches nothing still return a
# series, so a dead panel renders a flat zero instead of "No data" -- which is
# exactly why nobody noticed. A naive check is fooled the same way a human is.
#
# Some queries are legitimately empty when everything is healthy, e.g.
#   sum(up{job="kube-state-metrics"} == 0) > 0
#   sum(ALERTS{alertstate="firing"})
# Those are the cases where a vector(0) fallback is correct. Read the output;
# do not assume every DEAD line is a bug.
set -uo pipefail

: "${KUBECONFIG:=/tmp/galaxy-kubeconfig}"; export KUBECONFIG
NS=prometheus
POD=$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=prometheus -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
[ -n "$POD" ] || { echo "no prometheus pod found; is KUBECONFIG right?" >&2; exit 1; }

enc() { printf '%s' "$1" | sed -e 's/%/%25/g' -e 's/+/%2B/g' -e 's/&/%26/g'; }   # POST body is form-encoded: a bare + means space

promq() {
  kubectl -n "$NS" exec "$POD" -c prometheus -- \
    wget -qO- --post-data="query=$(enc "$1")" "http://localhost:9090/api/v1/query" 2>/dev/null
}

shopt -s nullglob
files=()
if [ "$#" -gt 0 ]; then for n in "$@"; do files+=("cluster/grafana/dashboards/$n.yaml"); done
else files=(cluster/grafana/dashboards/*.yaml); fi

total=0
for f in "${files[@]}"; do
  [ -f "$f" ] || { echo "no such dashboard: $f" >&2; continue; }
  name=$(basename "$f" .yaml)
  json=$(kubectl create --dry-run=client -f "$f" -o jsonpath='{.data}' 2>/dev/null | jq -r 'to_entries[0].value' 2>/dev/null)
  [ -n "$json" ] || { echo "== $name: could not extract dashboard JSON"; continue; }
  echo "== $name"
  bad=0
  while IFS=$'\t' read -r pid title expr; do
    [ -z "$expr" ] && continue
    case "$expr" in *'$'*) continue;; esac          # templated; needs variable values
    core=$(printf '%s' "$expr" | sed -E 's/[[:space:]]+(OR|or)[[:space:]]+(on\(\)[[:space:]]*)?vector\([0-9.]+\)//g')
    r=$(promq "$core")
    st=$(jq -r '.status // "ERR"' <<<"$r" 2>/dev/null)
    n=$(jq -r '(.data.result // []) | length' <<<"$r" 2>/dev/null)
    hint=""; [ "$core" != "$expr" ] && hint=" (a vector() fallback was hiding this)"
    if [ "$st" != "success" ]; then
      printf '   ERROR [%s] %s%s\n         %s\n' "$pid" "$title" "$hint" "${core:0:110}"; bad=$((bad+1))
    elif [ "${n:-0}" = "0" ]; then
      printf '   DEAD  [%s] %s%s\n         %s\n' "$pid" "$title" "$hint" "${core:0:110}"; bad=$((bad+1))
    fi
  done < <(jq -r '.panels[]? | . as $p | (.targets // [])[] | select(.expr) | [($p.id|tostring), ($p.title // "-"), .expr] | @tsv' <<<"$json")
  [ "$bad" = 0 ] && echo "   ok"
  total=$((total+bad))
done
echo
echo "$total expression(s) returned nothing."
