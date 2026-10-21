#!/usr/bin/env bash
# Go/no-go for Rook changes (Luma phase 0b).
#
#   scripts/ceph-gate.sh [--health-only]
#
# Prints exactly one line: "GATE PASS" or "GATE FAIL: <reason>[, <reason>...]".
# Exit 0 = pass. Read-only; safe to run from Monitor every 5 minutes.
# Fails closed: if a check cannot be evaluated, that is a failure.
#
# --health-only skips the latency and backup-window rules.
# CEPH_GATE_MAX_LATENCY_MS (default 1000) exists only to prove the failing
# path; values above 1000 are refused. Loosening the gate is Chris's call.
set -uo pipefail

: "${KUBECONFIG:=/tmp/galaxy-kubeconfig}"; export KUBECONFIG

health_only=0
case "${1:-}" in
  "") ;;
  --health-only) health_only=1; shift ;;
  *) echo "usage: $0 [--health-only]" >&2; exit 2 ;;
esac
[ "$#" -eq 0 ] || { echo "usage: $0 [--health-only]" >&2; exit 2; }

max_ms="${CEPH_GATE_MAX_LATENCY_MS:-1000}"
if ! [[ "$max_ms" =~ ^[0-9]+$ ]] || [ "$max_ms" -gt 1000 ]; then
  echo "refusing CEPH_GATE_MAX_LATENCY_MS=$max_ms: must be an integer <= 1000 (loosening the gate is Chris's call)" >&2
  exit 2
fi

ALLOWED='["AUTH_INSECURE_CLIENT_KEY_TYPE","AUTH_INSECURE_KEYS_ALLOWED","AUTH_INSECURE_KEYS_CREATABLE","BLUESTORE_SPURIOUS_READ_ERRORS","MDS_CLIENT_RECALL","BLUESTORE_SLOW_OP_ALERT"]'

fails=()
ceph() { kubectl -n rook exec deploy/rook-ceph-tools -- ceph "$@" 2>/dev/null; }

# 1. health checks beyond the allowed set (SLOW_OPS is deliberately not allowed)
if h=$(ceph health detail -f json) && jq -e . >/dev/null 2>&1 <<<"$h"; then
  bad=$(jq -r --argjson a "$ALLOWED" '[(.checks // {}) | keys[] | select(. as $k | $a | index($k) | not)] | join("+")' <<<"$h")
  [ -z "$bad" ] || fails+=("health checks: $bad")
else
  fails+=("ceph health unreadable")
fi

if s=$(ceph status -f json) && jq -e . >/dev/null 2>&1 <<<"$s"; then
  # 2. every PG active+clean (+scrubbing / +deep allowed)
  badpg=$(jq -r '[.pgmap.pgs_by_state[] | select(((.state_name | split("+")) - ["scrubbing","deep"]) != ["active","clean"]) | "\(.state_name)=\(.count)"] | join(" ")' <<<"$s")
  [ -z "$badpg" ] || fails+=("pgs not active+clean: $badpg")
  # 3. 3 mons in quorum
  nq=$(jq -r '.quorum_names | length' <<<"$s")
  [ "$nq" = "3" ] || fails+=("mon quorum $nq/3")
  # 4. 9 osds up and in
  read -r no nu ni < <(jq -r '.osdmap | "\(.num_osds) \(.num_up_osds) \(.num_in_osds)"' <<<"$s")
  { [ "$no" = "9" ] && [ "$nu" = "9" ] && [ "$ni" = "9" ]; } || fails+=("osds ${nu:-?} up/${ni:-?} in of ${no:-?}")
else
  fails+=("ceph status unreadable")
fi

# 5. no node SchedulingDisabled
if n=$(kubectl get nodes -o json 2>/dev/null) && jq -e . >/dev/null 2>&1 <<<"$n"; then
  cord=$(jq -r '[.items[] | select(.spec.unschedulable == true) | .metadata.name] | join(",")' <<<"$n")
  [ -z "$cord" ] || fails+=("node SchedulingDisabled: $cord")
else
  fails+=("nodes unreadable")
fi

if [ "$health_only" -eq 0 ]; then
  # 6. OSD commit latency, 15 min max, via wget in the Prometheus pod
  POD=$(kubectl -n prometheus get pod -l app.kubernetes.io/name=prometheus -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  q='max(max_over_time(ceph_osd_commit_latency_ms[15m]))'
  r=""
  [ -z "$POD" ] || r=$(kubectl -n prometheus exec "$POD" -c prometheus -- \
      wget -qO- --post-data="query=$(printf '%s' "$q" | sed -e 's/%/%25/g' -e 's/+/%2B/g' -e 's/&/%26/g')" \
      "http://localhost:9090/api/v1/query" 2>/dev/null)
  lat=$(jq -r 'if .status=="success" then (.data.result[0].value[1] // "") else "" end' <<<"$r" 2>/dev/null)
  if [[ "$lat" =~ ^[0-9.]+$ ]]; then
    awk -v l="$lat" -v m="$max_ms" 'BEGIN{exit !(l < m)}' || fails+=("latency ${lat%%.*}ms >= ${max_ms}ms (15m max osd commit)")
  else
    fails+=("latency unreadable")
  fi
  # 7. backup window 00:30-05:00 UTC
  hm=$((10#$(date -u +%H%M)))
  if [ "$hm" -ge 30 ] && [ "$hm" -lt 500 ]; then fails+=("backup window 00:30-05:00 UTC"); fi
fi

if [ "${#fails[@]}" -eq 0 ]; then echo "GATE PASS"; exit 0; fi
out="GATE FAIL: ${fails[0]}"
for f in "${fails[@]:1}"; do out+=", $f"; done
echo "$out"; exit 1
