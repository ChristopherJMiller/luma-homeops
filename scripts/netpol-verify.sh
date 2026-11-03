#!/usr/bin/env bash
# Live checks for NetworkPolicy enforcement (kube-network-policies).
# Design: docs/superpowers/specs/2026-11-03-networkpolicy-enforcement-design.md §9.
#
#   scripts/netpol-verify.sh idle
#
# Exit 0 = pass, 1 = at least one FAIL (an answer that does not parse is a FAIL
# too, never a vacuous pass), 2 = the check could not run or is meaningless (no
# cluster access, no agent pods, or policies exist for `idle`).
# Read-only; safe to run from Monitor. scripts/test_netpol_verify.py tests it
# against a stub kubectl (pre-commit hook `netpol-verify-selftest`).
#
# idle -- with zero NetworkPolicies the enforcer must judge nothing (§3.4 item 2,
# §9.1). Coverage first: ds/kube-network-policies must have
# desiredNumberScheduled == numberReady == the number of agent pods listed ==
# the number of PASS rows, so a node whose agent was never created is a FAIL.
# Then for each agent pod it requires:
#   - Ready, 0 restarts;
#   - nfqueue_queue_total{queue="98"} present: the queue is bound;
#   - nfqueue_packet_id{queue="98"} == 0: the kernel has queued no packet since
#     the bind. The id is cumulative, so one reading covers the agent's lifetime;
#   - nfqueue_queue_dropped, nfqueue_user_dropped and verdict_log_dropped == 0;
#   - the sum of packet_count == 0 (the series only exist after a first packet,
#     so absent counts as 0);
#   - its log shows the flags we set, and no klog E/F line and no
#     "syncing nftables rules" line carrying an error.
# The queue metrics come from a collector that runs every 30 s and first runs
# before the queue is bound, so run this at least a minute after the pod is Ready.
set -uo pipefail

: "${KUBECONFIG:=/tmp/galaxy-kubeconfig}"; export KUBECONFIG
NS=kube-system
SELECTOR=app=kube-network-policies   # upstream install.yaml v1.1.3 pod template label
QUEUE=98

# Flags the agent must log at start (pkg/cmd + klog's -v), as name=value.
FLAGS=(fail-open=true log-verdicts=deny disable-nri=true nfqueue-id=98 metrics-bind-address=:9080 v=1)

usage() { echo "usage: $0 idle" >&2; exit 2; }
die() { echo "ERROR: $*" >&2; exit 2; }
# The cluster answered, but not with what we can judge: a FAIL, not "could not run".
parse_fail() { echo "FAIL parse: $*"; echo "idle: FAIL (unparseable answer)"; exit 1; }
count_or_fail() { [[ "$2" =~ ^[0-9]+$ ]] || parse_fail "$1 is '$2', not a count"; }
kc() { timeout 60 kubectl --request-timeout=50s "$@"; }
# Length of a list response's .items; errors (so the caller FAILs) when .items is not an array.
items_len() { jq 'if (.items | type) == "array" then .items | length else error("no .items array") end' "$1" 2>/dev/null; }

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

# metric <file> <name> [<label>=<value>] -> prints each matching sample's value
metric() {
  local re="^$2"
  if [ -n "${3:-}" ]; then re+="\\{([^}]*,)?${3%%=*}=\"${3#*=}\"(,[^}]*)?\\} "; else re+="(\\{[^}]*\\})? "; fi
  grep -E "$re" "$1" | awk '{print $2}'
}
is_zero() { awk -v x="$1" 'BEGIN { exit !(x + 0 == 0) }'; }

idle() {
  local n_np n desired ds_ready rows pass=0 failed=0 fail=0
  kc get networkpolicies -A -o json > "$tmp/np.json" 2>/dev/null || die "cannot list NetworkPolicies; is KUBECONFIG right?"
  n_np=$(items_len "$tmp/np.json"); count_or_fail "the NetworkPolicy count" "$n_np"
  [ "$n_np" -eq 0 ] || die "policies exist: idle check meaningless ($n_np NetworkPolicies)"

  kc -n "$NS" get pods -l "$SELECTOR" -o json > "$tmp/pods.json" 2>/dev/null || die "cannot list pods in $NS"
  n=$(items_len "$tmp/pods.json"); count_or_fail "the agent pod count" "$n"
  [ "$n" -gt 0 ] || die "no kube-network-policies agent pods"

  kc -n "$NS" get daemonset kube-network-policies -o json > "$tmp/ds.json" 2>/dev/null || die "cannot read ds/kube-network-policies in $NS"
  desired=$(jq -r '.status.desiredNumberScheduled' "$tmp/ds.json" 2>/dev/null); count_or_fail "desiredNumberScheduled" "$desired"
  ds_ready=$(jq -r '.status.numberReady' "$tmp/ds.json" 2>/dev/null); count_or_fail "numberReady" "$ds_ready"

  # name node ready restarts
  jq -r '.items[] | [ .metadata.name, (.spec.nodeName // "-"),
      ([.status.conditions[]? | select(.type == "Ready") | .status][0] // "False"),
      ([.status.containerStatuses[]?.restartCount] | add // 0) ] | @tsv' "$tmp/pods.json" > "$tmp/pods.tsv" 2>/dev/null \
    || parse_fail "the agent pod list"
  rows=$(wc -l < "$tmp/pods.tsv")
  [ "$rows" -eq "$n" ] || parse_fail "$rows pod rows for $n pods"

  while IFS=$'\t' read -r pod node ready restarts; do
    local why=() m="$tmp/$pod.metrics" log="$tmp/$pod.log" v f total drops pid="?" packets="?" fl="?"
    [ "$ready" = True ] || why+=("not-ready")
    if [[ ! "$restarts" =~ ^[0-9]+$ ]]; then why+=("restarts-unparseable=$restarts")
    elif [ "$restarts" -ne 0 ]; then why+=("restarts=$restarts"); fi

    if kc get --raw "/api/v1/namespaces/$NS/pods/$pod:9080/proxy/metrics" > "$m" 2>/dev/null; then
      total=$(metric "$m" kube_network_policies_nfqueue_queue_total "queue=$QUEUE")
      [ -n "$total" ] || why+=("nfqueue_queue_total{queue=\"$QUEUE\"}-absent")
      pid=$(metric "$m" kube_network_policies_nfqueue_packet_id "queue=$QUEUE")
      if [ -z "$pid" ]; then why+=("nfqueue_packet_id-absent"); pid=absent
      elif ! is_zero "$pid"; then why+=("packet_id=$pid"); fi
      for v in nfqueue_queue_dropped nfqueue_user_dropped; do
        drops=$(metric "$m" "kube_network_policies_$v" "queue=$QUEUE")
        if [ -z "$drops" ]; then why+=("$v-absent")
        elif ! is_zero "$drops"; then why+=("$v=$drops"); fi
      done
      drops=$(metric "$m" kube_network_policies_verdict_log_dropped)
      if [ -z "$drops" ]; then why+=("verdict_log_dropped-absent")
      elif ! is_zero "$drops"; then why+=("verdict_log_dropped=$drops"); fi
      packets=$(metric "$m" kube_network_policies_packet_count | awk '{ s += $1 } END { printf "%.0f", s }')
      is_zero "$packets" || why+=("packets=$packets")
    else
      why+=("metrics-unreadable")
    fi

    if kc -n "$NS" logs "$pod" -c kube-network-policies > "$log" 2>/dev/null; then
      fl=ok
      for f in "${FLAGS[@]}"; do
        grep -Eq "\"flag\" name=\"${f%%=*}\" value=\"?${f#*=}\"?( |\$)" "$log" || { why+=("flag-${f%%=*}"); fl=bad; }
      done
      grep -Eq '^[EF][0-9]{4} ' "$log" && why+=("klog-error-lines=$(grep -Ec '^[EF][0-9]{4} ' "$log")")
      grep -F '"syncing nftables rules"' "$log" | grep -q 'error' && why+=("nftables-sync-error")
    else
      why+=("logs-unreadable")
    fi

    if [ "${#why[@]}" -eq 0 ]; then
      echo "PASS $node $pod packet_id=$pid packets=$packets restarts=$restarts flags=$fl"
      pass=$((pass + 1))
    else
      echo "FAIL $node $pod ${why[*]}"
      failed=$((failed + 1))
    fi
  done < "$tmp/pods.tsv"

  # Coverage: every scheduled agent is Ready, listed and judged, and every row PASSed.
  if [ "$desired" -ne "$n" ] || [ "$ds_ready" -ne "$n" ]; then
    echo "FAIL coverage desiredNumberScheduled=$desired numberReady=$ds_ready pods=$n"
    fail=1
  fi
  if [ $((pass + failed)) -ne "$n" ]; then
    echo "FAIL coverage judged=$((pass + failed)) pods=$n"
    fail=1
  fi
  [ "$failed" -eq 0 ] && [ "$pass" -eq "$n" ] && [ "$pass" -eq "$desired" ] || fail=1

  if [ "$fail" -eq 0 ]; then
    echo "idle: PASS ($n agents, 0 NetworkPolicies)"
    return 0
  fi
  echo "idle: FAIL ($n agents, 0 NetworkPolicies)"
  return 1
}

[ "$#" -eq 1 ] || usage
case "$1" in
  idle) idle ;;
  *) usage ;;
esac
