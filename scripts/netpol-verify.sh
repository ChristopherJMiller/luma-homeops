#!/usr/bin/env bash
# Live checks for NetworkPolicy enforcement (kube-network-policies).
# Design: docs/superpowers/specs/2026-11-03-networkpolicy-enforcement-design.md §9.
#
#   scripts/netpol-verify.sh idle
#   scripts/netpol-verify.sh selftest
#   scripts/netpol-verify.sh baseline
#   scripts/netpol-verify.sh canary [--node NAME]
#   scripts/netpol-verify.sh stray [--since DUR]      DUR like 15m (the default), 2h
#   scripts/netpol-verify.sh alert
#
# Exit 0 = pass, 1 = at least one FAIL (an answer that does not parse is a FAIL
# too, never a vacuous pass), 2 = the check could not run or is meaningless (no
# cluster access, no agent pods, policies exist for `idle`, or `canary` is still
# ambiguous after a second run).
# Read-only; safe to run from Monitor. scripts/test_netpol_verify.py tests it
# against a stub kubectl (pre-commit hook `netpol-verify-selftest`).
#
# selftest -- offline (no cluster): the canary's probe.sh and matrix.txt, deny_match and
# scripts/netpol-connect.js. Exit 0 = every case ok, 1 = a case failed, 2 = probe.sh missing.
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
#     "syncing nftables rules" line (any case) carrying an error= key.
# The queue metrics come from a collector that runs every 30 s and first runs
# before the queue is bound, so run this at least a minute after the pod is Ready.
#
# baseline -- B1a, before any canary policy (§6.4 "Baseline first"). The three
# netpol-probe pods each run `probe.sh once baseline`, in parallel, and every
# row the canary runs there must answer: open, or refused for a must-drop
# target. Each node checks its own N: matrix.txt expanded with that pod's node
# name and host IP, less its own BMC (never a literal, F8). A target that does
# neither would pass as "timeout" once policy is in, so it FAILs here. A pod
# whose count differs (a stale ConfigMap) FAILs too. Exit 2 unless exactly 3
# Running probe pods sit on 3 distinct nodes. It skips a pod with a
# deletionTimestamp (a rollout's outgoing pod).
#
# canary -- B1b and after (§9.2): each probe pod (or the one on --node) runs
# `probe.sh once enforced`, in parallel, and must pass every row. Its table must
# match the repo's expansion row by row, so a stale ConfigMap FAILs. Then every
# dropped row needs a deny line inside that run's window (T0 to T1, read from the
# pod's own clock), from that pod, in the row's direction (row_direction): to the
# literal host:port when the row's backend is "-", or else to one of the
# backend's post-DNAT IP:PORTs (§3.4 item 1, backend_dsts), port anchored, and
# from a connect attempt (source port) of its own (claim_rows). A timeout that no
# agent logged is a FAIL: the packet may never have reached an enforcer. A row
# that only a shared destination or a second run's attempts could prove, and
# that pairing by the row timeout cannot settle, reads AMBIGUOUS; that pod runs
# again, up to CANARY_RERUNS (3) times, and if that leaves it ambiguous, `canary`
# exits 2 and never PASSes. Then `stray` over the runs' windows. Prints
# `<node> rows <p>/<N> deny-logged <d>/<D>` per pod. Exit 2 unless the pods are
# there (3 Running on 3 nodes, or 1 on --node).
#
# stray -- every deny line in every agent's log for --since must name a pod
# (srcPod or dstPod) in a namespace that has a NetworkPolicy (Review Focus 3),
# and no policed pod it names may be a node IP or an IP that another pod held at
# the time (stray_check). A pod since gone, or since moved to another IP, is a
# `GONE` line, information only. It reads each agent's previous container too,
# and FAILs when a log does not reach back to --since (a rotated file, a replaced
# pod). Prints a `window` line per agent, up to 10 `STRAY [why] <line>` and 10
# `GONE [why] <line>`, then the verdict.
#
# alert -- Prometheus has loaded the NetpolCanaryFailing rule as the object writes it, checked
# by name (alert()).
set -uo pipefail

: "${KUBECONFIG:=/tmp/galaxy-kubeconfig}"; export KUBECONFIG
NS=kube-system
SELECTOR=app=kube-network-policies   # upstream install.yaml v1.1.3 pod template label
QUEUE=98

# Flags the agent must log at start (pkg/cmd + klog's -v), as name=value.
FLAGS=(fail-open=true log-verdicts=deny disable-nri=true nfqueue-id=98 metrics-bind-address=:9080 v=1)

CANARY_NS=netpol-canary
PROBE_SELECTOR=app.kubernetes.io/name=netpol-probe
CANARY_NODES=3
# How many more runs an AMBIGUOUS pod gets (review round 3, R3-1).
CANARY_RERUNS=3
# exec_timeout ROWS -> seconds to allow one `probe.sh once` in a pod: every row and the WAN lookup
# at probe.sh's deadline, plus 30 s. A healthy run takes seconds. Needs probe.sh sourced.
exec_timeout() { echo $((($1 + 1) * PROBE_DEADLINE_DEFAULT + 30)); }

usage() { echo "usage: $0 idle | selftest | baseline | canary [--node NAME] | stray [--since DUR] | alert" >&2; exit 2; }
die() { echo "ERROR: $*" >&2; exit 2; }
# The cluster answered, but not with what we can judge: a FAIL, not "could not run".
parse_fail() { echo "FAIL parse: $*"; echo "${CHECK:-idle}: FAIL (unparseable answer)"; exit 1; }
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

# sync_error LOG -> 0 if an agent log has a failed nftables sync. Upstream (controller.go, v1.1.3)
# logs "Syncing nftables rules" at each sync and "syncing nftables rules" error=... on failure, so
# match either case and key on error=. Captured, then tested: a pipeline into grep -q could die of
# SIGPIPE on a long log under pipefail and miss the line (review round 1, M1).
sync_error() {
  local sync
  sync=$(grep -iF 'syncing nftables rules' "$1")
  grep -Eq '(^| )error=' <<< "$sync"
}

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
      sync_error "$log" && why+=("nftables-sync-error")
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

baseline() {
  local root probe matrix n live running nodes pod node hostip rows limit rc summary npass nfail fail=0 counts
  root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
  probe=$root/cluster/netpol-canary/probe.sh
  matrix=$root/cluster/netpol-canary/matrix.txt
  [ -f "$probe" ] && [ -f "$matrix" ] || die "missing $probe or $matrix"
  # shellcheck source=/dev/null
  . "$probe"

  kc -n "$CANARY_NS" get pods -l "$PROBE_SELECTOR" -o json > "$tmp/probes.json" 2> /dev/null \
    || die "cannot list pods in $CANARY_NS; is KUBECONFIG right?"
  n=$(items_len "$tmp/probes.json"); count_or_fail "the probe pod count" "$n"
  # name node phase hostIP, for every pod without a deletionTimestamp
  jq -r '.items[] | select(.metadata.deletionTimestamp == null)
      | [.metadata.name, (.spec.nodeName // "-"), (.status.phase // "-"), (.status.hostIP // "-")] | @tsv' \
    "$tmp/probes.json" > "$tmp/probes.tsv" 2> /dev/null || parse_fail "the probe pod list"
  live=$(wc -l < "$tmp/probes.tsv")
  [ "$live" -gt 0 ] || die "no netpol-probe pods in $CANARY_NS"
  running=$(awk -F'\t' '$3 == "Running"' "$tmp/probes.tsv" | wc -l)
  nodes=$(awk -F'\t' '$3 == "Running" { print $2 }' "$tmp/probes.tsv" | sort -u | wc -l)
  if [ "$live" -ne "$CANARY_NODES" ] || [ "$running" -ne "$CANARY_NODES" ] || [ "$nodes" -ne "$CANARY_NODES" ]; then
    sed 's/^/  /' "$tmp/probes.tsv" >&2
    die "need exactly $CANARY_NODES Running netpol-probe pods on $CANARY_NODES nodes (found $live, $running Running, on $nodes nodes)"
  fi

  # Each pod's N: the rows its probe.sh runs there (its node's variables, less its own BMC).
  # WAN_IP is a placeholder; only the count matters.
  while IFS=$'\t' read -r pod node _ hostip; do
    if NODE_NAME=$node HOST_IP=$hostip WAN_IP=192.0.2.1 probe_rows "$matrix" canary > "$tmp/$pod.rows" 2> /dev/null; then
      wc -l < "$tmp/$pod.rows" > "$tmp/$pod.n"
    else
      echo "?" > "$tmp/$pod.n"
    fi
  done < "$tmp/probes.tsv"

  # The three in parallel. </dev/null keeps kubectl off the loop's input.
  while IFS=$'\t' read -r pod node _ _; do
    rows=$(cat "$tmp/$pod.n")
    if [[ ! "$rows" =~ ^[0-9]+$ ]] || [ "$rows" -eq 0 ]; then echo "baseline: $node: matrix.txt does not expand"; continue; fi
    limit=$(exec_timeout "$rows")
    echo "baseline: $node $rows rows, exec timeout ${limit} s"
    (
      timeout "$limit" kubectl -n "$CANARY_NS" exec "$pod" -c probe -- bash /etc/netpol-probe/probe.sh once baseline \
        > "$tmp/$pod.out" 2> "$tmp/$pod.err" < /dev/null
      echo $? > "$tmp/$pod.rc"
    ) &
  done < "$tmp/probes.tsv"
  wait

  counts=""
  while IFS=$'\t' read -r pod node _ _; do
    rows=$(cat "$tmp/$pod.n")
    rc=$(cat "$tmp/$pod.rc" 2> /dev/null)
    echo "== $node $pod (exit ${rc:-?})"
    cat "$tmp/$pod.out" 2> /dev/null
    [ ! -s "$tmp/$pod.err" ] || head -n 5 "$tmp/$pod.err" | sed 's/^/   stderr: /'
    summary=$(grep '^SUMMARY ' "$tmp/$pod.out" 2> /dev/null | tail -n 1)
    npass=$(grep -c '^PASS ' "$tmp/$pod.out" 2> /dev/null)
    nfail=$(grep -c '^FAIL ' "$tmp/$pod.out" 2> /dev/null)
    if [[ "$rows" =~ ^[0-9]+$ ]] && [ "$rows" -gt 0 ] && [ "${rc:-}" = 0 ] \
      && [ "$summary" = "SUMMARY $rows/$rows baseline node=$node" ] \
      && [ "${npass:-0}" -eq "$rows" ] && [ "${nfail:-0}" -eq 0 ]; then
      echo "PASS $node $rows/$rows rows answer"
    else
      echo "FAIL $node exit=${rc:-?} pass=${npass:-0}/$rows fail=${nfail:-0} summary='${summary:-none}'"
      fail=1
    fi
    counts+="$node $rows, "
  done < "$tmp/probes.tsv"
  counts=${counts%, }

  # One N for every node prints as "3 nodes × N"; otherwise each node's.
  if [ "$(awk -F'\t' '{ print $1 }' "$tmp/probes.tsv" | while read -r pod; do cat "$tmp/$pod.n"; done | sort -u | wc -l)" -eq 1 ]; then
    counts="$CANARY_NODES nodes × $(cat "$tmp/$(head -n 1 "$tmp/probes.tsv" | cut -f1).n") rows"
  else
    counts="$CANARY_NODES nodes: $counts rows"
  fi
  if [ "$fail" -eq 0 ]; then
    echo "baseline: PASS ($counts answer)"
    return 0
  fi
  echo "baseline: FAIL ($counts)"
  return 1
}

# deny_match LOGFILE SRC_POD DST [DIRECTION] -> 0 if some line of an agent's log is a deny verdict
# from SRC_POD (ns/name, anchored: ...-abcde never matches ...-abcdef) to DST, which is IP:PORT
# (port anchored) or IP: (any port), judged in DIRECTION (egress or ingress) when one is given.
# Regex metacharacters, the dots in an IP, are escaped. The last grep reads all its input (no -q):
# under pipefail, an early exit would stop the greps before it with SIGPIPE and turn a match into
# a miss.
deny_match() {
  local log=$1 src dst dirre=.
  src=$(printf '%s' "$2" | sed 's/[][\.*^$(){}?+|\\]/\\&/g')
  dst=$(printf '%s' "$3" | sed 's/[][\.*^$(){}?+|\\]/\\&/g')
  case $3 in *: ) ;; *) dst+='("| |$)' ;; esac
  [ -z "${4-}" ] || dirre="(^| )direction=\"?${4}\"?( |\$)"
  grep -F '"Packet verdict"' "$log" \
    | grep -E '(^| )verdict="?deny"?( |$)' \
    | grep -E "(^| )srcPod=\"?${src}\"?( |\$)" \
    | grep -E "$dirre" \
    | grep -E "(^| )dst=\"?${dst}" > /dev/null
}

# The direction a dropped row's deny must carry. The probe's egress denies a target that no policy
# selects. The 9377 echo row isolates the other side (§9.2): probe-egress-to-echo allows
# 9377, so only the echo's ingress can drop it. The 9378 row stays egress: echo-ingress-from-probe
# allows 9378, so only the probe's egress can drop it. scripts/test_netpol_verify.py keeps these
# keys in matrix.txt.
ROW_INGRESS=(netpol-echo.netpol-canary.svc.cluster.local:9377)
row_direction() {
  local r
  for r in "${ROW_INGRESS[@]}"; do [ "$1" != "$r" ] || { echo ingress; return 0; }; done
  echo egress
}

# deny_attempts LOG -> "key<TAB>srcPod<TAB>srcPort<TAB>dst<TAB>direction<TAB>seconds" per deny line,
# where key is the klog stamp "MMDD HH:MM:SS" and seconds the same stamp to the microsecond, counted
# from the start of the year (UTC). The enforcer judges before SNAT, so a connect attempt keeps one
# source port across its SYN retransmits, even when kube-proxy sends each SYN to another backend:
# within one pod, the source port names the attempt.
deny_attempts() {
  grep -F '"Packet verdict"' "$1" | awk '
    function val(key,   s, v) {
      s = " " $0
      if (!match(s, " " key "=\"?[^\" ]*")) return ""
      v = substr(s, RSTART + length(key) + 2, RLENGTH - length(key) - 2)
      sub(/^"/, "", v)
      return v
    }
    BEGIN { split("0 31 59 90 120 151 181 212 243 273 304 334", cum, " ") }
    val("verdict") == "deny" {
      sp = val("src"); sub(/^.*:/, "", sp)
      secs = (cum[substr($0, 2, 2) + 0] + substr($0, 4, 2) - 1) * 86400 + substr($0, 7, 2) * 3600 + substr($0, 10, 2) * 60 + substr($0, 13, 9)
      printf "%s\t%s\t%s\t%s\t%s\t%.6f\n", substr($0, 2, 4) " " substr($0, 7, 8), val("srcPod"), sp, val("dst"), val("direction"), secs
    }'
}

# One dropped row follows another 3 s apart: agnhost --timeout=3s, plus the spawn of the next tool.
# Live, 2026-11-06: 473 consecutive attempts 3.020 to 3.073 s apart (the ClusterIP row to the
# literal .240:6443 row, 3.022 to 3.024 s). claim_rows pairs attempts with this.
ROW_GAP_MIN=2.9
ROW_GAP_MAX=3.5

# claim_rows REQS ATTEMPTS -> "row<TAB>status<TAB>why" per row of REQS ("row<TAB>direction<TAB>
# dst,dst,..."), the must-drop rows in the order the probe runs them; status 1 (logged), 0 (not
# logged) or A (ambiguous). ATTEMPTS is one pod's deny_attempts within its run's window. Each
# must-drop row needs a logged attempt of its own: a deny in the row's direction to one of its
# destinations, from a source port that no row overlapping it has claimed. Rows with the same
# destinations and direction (top's .240:50000 twice) are the same traffic and share one claim.
#
# Overlapping rows (10.96.0.1:443, which lands on 192.168.0.24x:6443, and the literal
# 192.168.0.240:6443) claim narrowest first. A wider row then needs a distinctive attempt, one
# that reached a destination no narrower overlapping row has (.241 or .242 here). An attempt that
# only reached the shared .240 could be the literal row's traffic from another run, so that row is
# A. Also A: an overlapping group with more attempts than rows, which means another run's attempts
# are in the window. Greedy matching is exact while overlapping rows nest, as they do in matrix.txt.
#
# Pairing settles the first kind (review round 3, R3-1). With no other run in the window (no row of
# the pod outside the group has more attempts than rows; another run adds attempts to the rows next
# to the group, 3 s either side), the group holds one attempt per row, and its attempts sit
# ROW_GAP_MIN to ROW_GAP_MAX seconds apart per row between them in the probe's order: then the
# k-th attempt in time is the k-th row's, if it fits that row. In the 1-in-27 case where all three
# ClusterIP SYNs land on .240, that names the earlier attempt the ClusterIP row's. A ClusterIP path
# that never logs leaves one attempt for two rows, so it still reads NOT-LOGGED. Another run in
# the window leaves the group A, and canary runs the pod again.
claim_rows() {
  awk -F'\t' -v gapmin="$ROW_GAP_MIN" -v gapmax="$ROW_GAP_MAX" '
    function overlaps(a, b,   n, arr, i) {
      n = split(cands[a], arr, ",")
      for (i = 1; i <= n; i++) if ((b, arr[i]) in incand) return 1
      return 0
    }
    function find(x) { while (comp[x] != x) x = comp[x]; return x }
    # fitsrow(sp, r, g): the attempt has a line to the destinations of r in its direction, and no
    # line in that direction to a destination of the group g outside them.
    function fitsrow(sp, r, g,   m, hit) {
      hit = 0
      for (m = 1; m <= nl; m++) {
        if (lsp[m] != sp || ldir[m] != rdir[r] || !((g, ldst[m]) in inunion)) continue
        if (!((r, ldst[m]) in incand)) return 0
        hit = 1
      }
      return hit
    }
    FNR == NR {
      key = $2 "|" $3
      if (!(key in rid)) {
        nreq++; rid[key] = nreq; rdir[nreq] = $2; cands[nreq] = $3; comp[nreq] = nreq
        ncand[nreq] = split($3, c, ",")
        for (i = 1; i <= ncand[nreq]; i++) incand[nreq, c[i]] = 1
      }
      rowreq[++nrows] = rid[key]; rowid[nrows] = $1; nrowsof[rid[key]]++
      next
    }
    { nl++; lsp[nl] = $3; ldst[nl] = $4; ldir[nl] = $5; ltime[nl] = $6 + 0 }
    END {
      for (i = 1; i <= nreq; i++) ord[i] = i
      for (i = 1; i <= nreq; i++)
        for (j = i + 1; j <= nreq; j++)
          if (ncand[ord[j]] < ncand[ord[i]] || (ncand[ord[j]] == ncand[ord[i]] && ord[j] < ord[i])) { t = ord[i]; ord[i] = ord[j]; ord[j] = t }
      # Overlapping rows form groups; the destinations of a group are those of all its rows.
      for (a = 1; a <= nreq; a++)
        for (b = a + 1; b <= nreq; b++)
          if (overlaps(a, b) || overlaps(b, a)) comp[find(b)] = find(a)
      for (r = 1; r <= nreq; r++) {
        g = find(r); greqs[g]++; grows[g] += nrowsof[r]
        n = split(cands[r], c, ",")
        for (i = 1; i <= n; i++) inunion[g, c[i]] = 1
      }
      for (o = 1; o <= nreq; o++) {
        r = ord[o]; st[r] = 0; why[r] = ""; kind[r] = ""; g = find(r)
        pick = ""; pickd = ""
        for (l = 1; l <= nl; l++) {
          if (ldir[l] != rdir[r] || !((r, ldst[l]) in incand)) continue
          blocked = 0
          for (q = 1; q <= nreq; q++) if (((q, lsp[l]) in claimed) && overlaps(q, r)) { blocked = 1; break }
          if (blocked) continue
          # The attempt belongs to this row only if each of its lines to a destination in the group
          # goes to a destination of this row: a literal .240:6443 attempt never reaches .241.
          if (!fitsrow(lsp[l], r, g)) continue
          if (pick == "") pick = lsp[l]
          shared = 0
          for (p = 1; p < o; p++) if (overlaps(ord[p], r) && ((ord[p], ldst[l]) in incand)) { shared = 1; break }
          if (!shared && pickd == "") pickd = lsp[l]
        }
        if (pickd != "") { claimed[r, pickd] = 1; st[r] = 1 }
        else if (pick != "") { claimed[r, pick] = 1; st[r] = "A"; kind[r] = "shared"; why[r] = "its only attempts reached destinations a narrower row shares" }
      }
      # Attempts per row and per group, and each attempt first SYN in the group.
      for (l = 1; l <= nl; l++)
        for (r = 1; r <= nreq; r++)
          if (ldir[l] == rdir[r] && ((r, ldst[l]) in incand)) {
            if (!((r, lsp[l]) in rseen)) { rseen[r, lsp[l]] = 1; ratt[r]++ }
            g = find(r)
            if (!((g, lsp[l]) in seen)) { seen[g, lsp[l]] = 1; gatt[g]++; gsp[g, gatt[g]] = lsp[l]; first[g, lsp[l]] = ltime[l] }
            else if (ltime[l] < first[g, lsp[l]]) first[g, lsp[l]] = ltime[l]
          }
      # Another run in the window: a row outside the overlapping groups has more attempts than rows.
      # Inside a group, one attempt can match two rows, so the group count (below) is the measure.
      other = 0
      for (r = 1; r <= nreq; r++) if (greqs[find(r)] == 1 && ratt[r] > nrowsof[r]) other = 1
      for (r = 1; r <= nreq; r++) {
        g = find(r)
        if (greqs[g] > 1 && gatt[g] > grows[g] && st[r] == 1) { st[r] = "A"; kind[r] = "excess"; why[r] = gatt[g] " attempts for " grows[g] " overlapping rows: another run is in the window" }
      }
      # Pairing by the row timeout, for groups left A only by shared destinations.
      for (g = 1; g <= nreq; g++) {
        if (find(g) != g || greqs[g] < 2 || other || gatt[g] != grows[g]) continue
        want = 0; bad = 0
        for (r = 1; r <= nreq; r++) if (find(r) == g) { if (kind[r] == "shared") want = 1; if (kind[r] == "excess") bad = 1 }
        if (!want || bad) continue
        n = 0
        for (k = 1; k <= nrows; k++) if (find(rowreq[k]) == g) { n++; prow[n] = rowreq[k]; ppos[n] = k }
        for (i = 1; i <= n; i++) psp[i] = gsp[g, i]
        for (i = 2; i <= n; i++)
          for (j = i; j > 1 && first[g, psp[j]] < first[g, psp[j - 1]]; j--) { t = psp[j]; psp[j] = psp[j - 1]; psp[j - 1] = t }
        ok = (n == gatt[g])
        for (i = 1; ok && i <= n; i++) {
          if (!fitsrow(psp[i], prow[i], g)) ok = 0
          if (i > 1) {
            gap = first[g, psp[i]] - first[g, psp[i - 1]]; steps = ppos[i] - ppos[i - 1]
            if (gap < steps * gapmin || gap > steps * gapmax) ok = 0
          }
        }
        if (ok) for (r = 1; r <= nreq; r++) if (find(r) == g) { st[r] = 1; why[r] = ""; kind[r] = "paired" }
      }
      for (k = 1; k <= nrows; k++) print rowid[k] "\t" st[rowreq[k]] "\t" why[rowreq[k]]
    }' "$1" "$2"
}

# backend_dsts NS SVC PORT -> one IP:PORT per endpoint address: where a dial to the Service on PORT
# lands after kube-proxy's DNAT, which is what the enforcer judges (design §3.4 item 1). PORT picks
# the Service port, its name picks the EndpointSlice port. A headless Service does no DNAT, so the
# probe dials the pod on PORT itself. Nothing on a port the Service lacks: such a row can never match.
backend_dsts() {
  local ns=$1 svc=$2 port=$3 f=$tmp/eps/$1_$2
  mkdir -p "$tmp/eps"
  [ -s "$f.svc.json" ] || kc -n "$ns" get service "$svc" -o json > "$f.svc.json" 2> /dev/null < /dev/null \
    || die "cannot read Service $ns/$svc"
  [ -s "$f.eps.json" ] || kc -n "$ns" get endpointslices -l "kubernetes.io/service-name=$svc" -o json > "$f.eps.json" 2> /dev/null < /dev/null \
    || die "cannot read the EndpointSlices of $ns/$svc"
  jq -r --argjson port "$port" --slurpfile svc "$f.svc.json" '
      ($svc[0].spec) as $s
      | if $s.clusterIP == "None" then .items[] | .endpoints[]? | .addresses[0] | "\(.):\($port)"
        else ([$s.ports[]? | select(.port == $port) | (.name // "")] | first) as $name
          | if $name == null then empty
            else .items[] | ([.ports[]? | select((.name // "") == $name) | .port] | first) as $tp
              | select($tp != null) | .endpoints[]? | .addresses[0] | "\(.):\($tp)"
            end
        end' "$f.eps.json" 2> /dev/null | sort -u || parse_fail "the Service or EndpointSlices of $ns/$svc"
}

# dur_seconds 15m -> 900 (DUR is N s, m or h)
dur_seconds() {
  local n=${1%[smh]}
  case $1 in *s) echo "$n" ;; *m) echo $((n * 60)) ;; *h) echo $((n * 3600)) ;; esac
}

# klog_epoch LINE -> the seconds since the epoch of LINE's klog header ([IWEF]MMDD HH:MM:SS, UTC),
# in the latest year that is not in the future. Returns 1 for a line that is not klog.
klog_epoch() {
  local y t now
  [[ ${1-} =~ ^[IWEF]([0-9]{2})([0-9]{2})\ ([0-9]{2}:[0-9]{2}:[0-9]{2}) ]] || return 1
  y=$(date -u +%Y) now=$(date -u +%s)
  t=$(date -u -d "$y-${BASH_REMATCH[1]}-${BASH_REMATCH[2]} ${BASH_REMATCH[3]}" +%s 2> /dev/null) || return 1
  [ "$t" -le $((now + 300)) ] || t=$(date -u -d "$((y - 1))-${BASH_REMATCH[1]}-${BASH_REMATCH[2]} ${BASH_REMATCH[3]}" +%s)
  echo "$t"
}

# klog_key EPOCH -> the klog stamp of EPOCH, "MMDD HH:MM:SS" (UTC). Keys compare as strings within
# a year: a window across New Year reads empty, which fails closed.
klog_key() { date -u -d "@$1" '+%m%d %H:%M:%S'; }

# klog_window FROM TO < LOG -> the lines stamped from FROM to TO, both klog keys, inclusive.
klog_window() { awk -v a="$1" -v b="$2" '{ k = substr($0, 2, 4) " " substr($0, 7, 8) } k >= a && k <= b'; }

# agent_logs SINCE -> $tmp/agents.log, every agent's log for the last SINCE, with the previous
# container's when the agent restarted. Also $tmp/coverage.tsv: pod, node, seconds covered, and the
# log's start. kubectl reads only a container's current log file, so a rotated file, a lost
# previous container, or a pod the DaemonSet replaced leaves the start of the window unread; only
# the coverage shows it. Exit 2 when there is no agent or kubectl cannot read a current log.
agent_logs() {
  local since=$1 n pod node restarts started from start prev
  kc -n "$NS" get pods -l "$SELECTOR" -o json > "$tmp/agents.json" 2> /dev/null || die "cannot list pods in $NS; is KUBECONFIG right?"
  n=$(items_len "$tmp/agents.json"); count_or_fail "the agent pod count" "$n"
  [ "$n" -gt 0 ] || die "no kube-network-policies agent pods"
  jq -r '.items[] | [.metadata.name, (.spec.nodeName // "-"),
      ([.status.containerStatuses[]? | select(.name == "kube-network-policies") | .restartCount][0] // 0),
      ([.status.containerStatuses[]? | select(.name == "kube-network-policies") | .state.running.startedAt][0] // "-")] | @tsv' \
    "$tmp/agents.json" > "$tmp/agents.tsv" 2> /dev/null || parse_fail "the agent pod list"
  : > "$tmp/agents.log"
  : > "$tmp/coverage.tsv"
  while IFS=$'\t' read -r pod node restarts started; do
    kc -n "$NS" logs "$pod" -c kube-network-policies --since="$since" > "$tmp/$pod.cur" 2> /dev/null < /dev/null \
      || die "cannot read the log of $NS/$pod"
    # The current file's first line: where this log starts.
    kc -n "$NS" logs "$pod" -c kube-network-policies --limit-bytes=4096 > "$tmp/$pod.head" 2> /dev/null < /dev/null \
      || die "cannot read the log of $NS/$pod"
    from=$(klog_epoch "$(head -n 1 "$tmp/$pod.head")") || from=""
    start=$(date -u -d "$started" +%s 2> /dev/null) || start=""
    : > "$tmp/$pod.prev"
    # The file starts with the container (kubelet has not rotated it), and an earlier container ran: read
    # that one too, and the log starts where its file does.
    if [ -n "$from" ] && [ -n "$start" ] && [ "$from" -le $((start + 60)) ] && [[ "$restarts" =~ ^[1-9][0-9]*$ ]]; then
      if kc -n "$NS" logs "$pod" -c kube-network-policies --previous --since="$since" > "$tmp/$pod.prev" 2> /dev/null < /dev/null \
        && kc -n "$NS" logs "$pod" -c kube-network-policies --previous --limit-bytes=4096 > "$tmp/$pod.prevhead" 2> /dev/null < /dev/null \
        && prev=$(klog_epoch "$(head -n 1 "$tmp/$pod.prevhead")"); then
        from=$prev
      else
        : > "$tmp/$pod.prev"
      fi
    fi
    cat "$tmp/$pod.prev" "$tmp/$pod.cur" >> "$tmp/agents.log"
    if [ -n "$from" ]; then
      printf '%s\t%s\t%s\t%s\n' "$pod" "$node" $(($(date -u +%s) - from)) "$(date -u -d "@$from" +%FT%TZ)" >> "$tmp/coverage.tsv"
    else
      printf '%s\t%s\t0\tunknown\n' "$pod" "$node" >> "$tmp/coverage.tsv"
    fi
  done < "$tmp/agents.tsv"
}

# policed_namespaces -> sets POLICED: the namespaces with at least one NetworkPolicy, comma-separated.
policed_namespaces() {
  kc get networkpolicies -A -o json > "$tmp/np.json" 2> /dev/null || die "cannot list NetworkPolicies; is KUBECONFIG right?"
  POLICED=$(jq -r 'if (.items | type) == "array" then [.items[].metadata.namespace] | unique | join(",") else error("no .items") end' \
    "$tmp/np.json" 2> /dev/null) || parse_fail "the NetworkPolicy list"
}

# cluster_pods -> $tmp/pods-now.tsv: "ns/name<TAB>podIP<TAB>since<TAB>holds" for every pod now,
# since as YYYYMMDDHHMMSS (UTC): when the pod got its current IP. That is the last transition of
# its PodReadyToStartContainers condition, which the kubelet sets once the sandbox and its network
# are up; a sandbox recreated after a node reboot gets a new IP while startTime stays put
# (review round 3, R3-2). startTime when the condition is absent (Kubernetes before 1.29). holds is 1 when
# the pod holds its IP: Running or Pending and not hostNetwork. A finished pod keeps its podIP in
# status after the IP is free again.
cluster_pods() {
  kc get pods -A -o json > "$tmp/allpods.json" 2> /dev/null < /dev/null || die "cannot list pods; is KUBECONFIG right?"
  jq -r '.items[] | [(.metadata.namespace + "/" + .metadata.name), (.status.podIP // "-"),
      (([.status.conditions[]? | select(.type == "PodReadyToStartContainers") | .lastTransitionTime][0] // .status.startTime // "")
        | gsub("[-:TZ]"; "") | if . == "" then "-" else . end),
      (if ((.status.phase == "Running" or .status.phase == "Pending") and (.spec.hostNetwork != true) and (.status.podIP // "") != "") then 1 else 0 end)]
      | @tsv' "$tmp/allpods.json" > "$tmp/pods-now.tsv" 2> /dev/null || parse_fail "the pod list"
}

# stray_check LOG -> prints up to 10 "STRAY [why] <line>" and 10 "GONE [why] <line>", and sets
# DENY_N, STRAY_N and GONE_N; 0 only if no line is stray (Review Focus 3). Each deny line must name
# a pod (srcPod or dstPod, ns/name) in a namespace in $POLICED. "external" names no pod. For each
# policed pod P that a line names, with IP X, logged at T:
#   - X outside 10.244.0.0/16: STRAY, a node IP enrolled (the #380 class).
#   - X now held by another pod Q that started before T: STRAY. At T, X was already Q's, so the
#     enforcer had a stale IP-to-pod mapping.
#   - P holds X now: fine.
#   - Otherwise P is gone, or now has another IP (a DaemonSet pod keeps its name across a node
#     reboot), and nobody took X before T: GONE, information only. A Job pod deleted when it
#     finishes (ci-tiny) is the usual case.
# A line naming no policed pod means the enforcer judged traffic that no policy selects: STRAY.
stray_check() {
  grep -F '"Packet verdict"' "$1" | grep -E '(^| )verdict="?deny"?( |$)' > "$tmp/deny.txt"
  DENY_N=$(wc -l < "$tmp/deny.txt")
  awk -v policed="$POLICED" -v podsfile="$tmp/pods-now.tsv" -v year="$(date -u +%Y)" -v today="$(date -u +%m%d)" \
    -v strayf="$tmp/stray.txt" -v gonef="$tmp/gone.txt" '
    function val(key,   s, v) {
      s = " " $0
      if (!match(s, " " key "=\"?[^\" ]*")) return ""
      v = substr(s, RSTART + length(key) + 2, RLENGTH - length(key) - 2)
      sub(/^"/, "", v)
      return v
    }
    function nsof(p) { return index(p, "/") ? substr(p, 1, index(p, "/") - 1) : "" }
    BEGIN {
      m = split(policed, p, ",")
      for (i = 1; i <= m; i++) if (p[i] != "") ok[p[i]] = 1
      FS = "\t"
      while ((getline l < podsfile) > 0) {
        split(l, f, "\t")
        live[f[1]] = 1; ipof[f[1]] = f[2]
        if (f[4] == 1) { holder[f[2]] = f[1]; since[f[2]] = f[3] }
      }
      FS = " "
    }
    {
      md = substr($0, 2, 4); hms = substr($0, 7, 8); gsub(":", "", hms)
      t = ((md > today) ? year - 1 : year) md hms
      stray = ""; gone = ""; sides = 0
      for (k = 1; k <= 2; k++) {
        pod = val(k == 1 ? "srcPod" : "dstPod")
        ip = val(k == 1 ? "src" : "dst"); sub(/:[0-9]+$/, "", ip)
        if (!(nsof(pod) in ok)) continue
        sides++
        if (index(ip, "10.244.") != 1) { stray = stray "; " pod " has ip " ip ", outside 10.244.0.0/16"; continue }
        h = (ip in holder) ? holder[ip] : ""
        if (h == pod) continue
        if (h != "" && since[ip] != "-" && since[ip] < t) { stray = stray "; " pod " named with ip " ip ", which " h " has held since " since[ip]; continue }
        if (pod in live) gone = gone "; " pod " now has ip " ipof[pod]
        else gone = gone "; " pod " is gone"
      }
      if (sides == 0) stray = "; no pod in a policed namespace"
      if (stray != "") print "[" substr(stray, 3) "] " $0 > strayf
      else if (gone != "") print "[" substr(gone, 3) "] " $0 > gonef
    }
  ' "$tmp/deny.txt"
  touch "$tmp/stray.txt" "$tmp/gone.txt"
  STRAY_N=$(wc -l < "$tmp/stray.txt")
  GONE_N=$(wc -l < "$tmp/gone.txt")
  head -n 10 "$tmp/stray.txt" | sed 's/^/STRAY /'
  head -n 10 "$tmp/gone.txt" | sed 's/^/GONE /'
  [ "$STRAY_N" -eq 0 ]
}

# stray_counts -> the counts after stray_check, for the verdict line.
stray_counts() {
  local s="$DENY_N deny lines"
  [ "${GONE_N:-0}" -eq 0 ] || s+=", $GONE_N from pods since gone or moved"
  echo "$s"
}

stray() {
  local want short=0 pod node covered from rc=0
  want=$(dur_seconds "$1")
  agent_logs "$1"
  policed_namespaces
  cluster_pods
  # kubectl reads only the current log file. At about 5 MiB a day per agent and kubelet's 10 MiB
  # file size, a log rotates about every two days, and a 24 h window then reads SHORT for about a
  # day after it. Gate on shorter windows, or read the rotated files (talosctl read /var/log/pods/…).
  while IFS=$'\t' read -r pod node covered from; do
    if [ "$covered" -ge "$want" ]; then
      echo "window $node $pod: covered, log from $from"
    else
      echo "window $node $pod: SHORT, $covered s of $want s (log from $from)"
      short=$((short + 1))
    fi
  done < "$tmp/coverage.tsv"
  stray_check "$tmp/agents.log" || rc=1
  if [ "$rc" -eq 0 ] && [ "$short" -eq 0 ]; then
    echo "stray: PASS ($(stray_counts), policed: ${POLICED:-none})"
    return 0
  fi
  if [ "$short" -eq 0 ]; then
    echo "stray: FAIL ($STRAY_N of $DENY_N deny lines stray, policed: ${POLICED:-none})"
  else
    echo "stray: FAIL ($STRAY_N of $DENY_N deny lines stray, $short agent logs short of $1, policed: ${POLICED:-none})"
  fi
  return 1
}

# canary_exec POD -> $tmp/POD.{out,err,rc}: one `probe.sh once enforced` in POD, between two
# readings of the pod's own clock (the T0 and T1 lines). The run's window is then in node time,
# with no skew to guess; the agents' clocks follow the same NTP.
canary_exec() {
  local pod=$1 limit
  limit=$(exec_timeout "$(wc -l < "$tmp/$pod.rows")")
  timeout "$limit" kubectl -n "$CANARY_NS" exec "$pod" -c probe -- bash -c \
    'echo "T0 $(date -u "+%m%d %H:%M:%S")"; bash /etc/netpol-probe/probe.sh once enforced; rc=$?; echo "T1 $(date -u "+%m%d %H:%M:%S")"; exit $rc' \
    > "$tmp/$pod.out" 2> "$tmp/$pod.err" < /dev/null
  echo $? > "$tmp/$pod.rc"
}

# canary_judge POD NODE -> $tmp/POD.report (what to print) and $tmp/POD.verdict (PASS, FAIL or
# AMBIGUOUS), from $tmp/attempts.tsv and the backends' destinations ($tmp/dst).
canary_judge() {
  local pod=$1 node=$2 rows rc summary npass nfail d=0 dn=0 amb=0 order=ok i=0 row line expect host port backend
  local lexpect lhp w t0 t1 e0 e1 k0="" k1="" st why dir b verdict
  rows=$(wc -l < "$tmp/$pod.rows")
  rc=$(cat "$tmp/$pod.rc" 2> /dev/null)
  {
    echo "== $node $pod (exit ${rc:-?})"
    grep -vE '^T[01] ' "$tmp/$pod.out" 2> /dev/null
    [ ! -s "$tmp/$pod.err" ] || head -n 5 "$tmp/$pod.err" | sed 's/^/   stderr: /'
  } > "$tmp/$pod.report"
  summary=$(grep '^SUMMARY ' "$tmp/$pod.out" 2> /dev/null | tail -n 1)
  grep -E '^(PASS|FAIL) ' "$tmp/$pod.out" > "$tmp/$pod.lines" 2> /dev/null
  npass=$(grep -c '^PASS ' "$tmp/$pod.lines")
  nfail=$(grep -c '^FAIL ' "$tmp/$pod.lines")
  # The run's window: T0 to T1 on the pod's clock, a second either side. A loop run that started
  # before T0 has its attempts outside it, since an attempt starts with its first SYN. A loop run
  # that overlaps the exec lands inside; claim_rows reports what that leaves ambiguous.
  t0=$(sed -n 's/^T0 //p' "$tmp/$pod.out" 2> /dev/null | head -n 1)
  t1=$(sed -n 's/^T1 //p' "$tmp/$pod.out" 2> /dev/null | tail -n 1)
  if e0=$(klog_epoch "I$t0") && e1=$(klog_epoch "I$t1"); then
    k0=$(klog_key $((e0 - 1))) k1=$(klog_key $((e1 + 1)))
  else
    order="the pod printed no T0/T1 clock reading"
  fi
  printf '%s\t%s\n' "$k0" "$k1" > "$tmp/$pod.window"
  awk -F'\t' -v p="$CANARY_NS/$pod" -v a="$k0" -v b="$k1" 'a != "" && $2 == p && $1 >= a && $1 <= b' "$tmp/attempts.tsv" > "$tmp/$pod.attempts"
  # Row i of the repo's expansion against line i of the pod's table: a stale ConfigMap shows here.
  [ "$(wc -l < "$tmp/$pod.lines")" -eq "$rows" ] || order="the pod ran $(wc -l < "$tmp/$pod.lines") rows, the repo has $rows"
  : > "$tmp/$pod.reqs"
  : > "$tmp/$pod.labels"
  while IFS=$'\t' read -r row line; do
    i=$((i + 1))
    read -r expect _ host port backend _ <<< "$row"
    read -r _ lexpect _ lhp _ <<< "$line"
    if [ "$host" = '$WAN_IP' ]; then
      [ "$lexpect" = "$expect" ] && [[ "$lhp" == *:"$port" ]] || order="row \$WAN_IP:$port, the pod ran '$lexpect $lhp'"
    else
      [ "$lexpect" = "$expect" ] && [ "$lhp" = "$host:$port" ] || order="row $expect $host:$port, the pod ran '$lexpect $lhp'"
    fi
    [ "$expect" = dropped ] || continue
    dn=$((dn + 1))
    # What this row's deny must name: the literal host:port the pod dialled, or each post-DNAT
    # IP:PORT of its backend, port anchored.
    if [ -z "$lhp" ]; then
      w=""
    elif [ "$backend" = - ]; then
      w=$lhp
    else
      w=$(paste -sd, "$tmp/dst/${backend%%/*}_${backend#*/}_$port")
    fi
    printf '%s\t%s\t%s\n' "$i" "$(row_direction "$host:$port")" "$w" >> "$tmp/$pod.reqs"
    printf '%s\t%s\t%s\n' "$i" "${lhp:-$host:$port}" "${backend/#-/the literal}" >> "$tmp/$pod.labels"
  done < <(paste -d$'\t' "$tmp/$pod.rows" "$tmp/$pod.lines")
  claim_rows "$tmp/$pod.reqs" "$tmp/$pod.attempts" > "$tmp/$pod.claims"
  while IFS=$'\t' read -r i st why; do
    [ "$st" != 1 ] || { d=$((d + 1)); continue; }
    IFS=$'\t' read -r _ dir w < <(awk -F'\t' -v i="$i" '$1 == i' "$tmp/$pod.reqs")
    IFS=$'\t' read -r _ lhp b < <(awk -F'\t' -v i="$i" '$1 == i' "$tmp/$pod.labels")
    if [ "$st" = A ]; then
      amb=$((amb + 1))
      echo "AMBIGUOUS $lhp ($dir to $b: ${w:-none}; $why)" >> "$tmp/$pod.report"
    else
      echo "NOT-LOGGED $lhp (no $dir deny of its own from $CANARY_NS/$pod to $b: ${w:-none})" >> "$tmp/$pod.report"
    fi
  done < "$tmp/$pod.claims"
  if [ "$amb" -eq 0 ]; then
    echo "$node rows $npass/$rows deny-logged $d/$dn" >> "$tmp/$pod.report"
  else
    echo "$node rows $npass/$rows deny-logged $d/$dn ambiguous $amb" >> "$tmp/$pod.report"
  fi
  verdict=FAIL
  if [ "${rc:-}" = 0 ] && [ "$summary" = "SUMMARY $rows/$rows enforced node=$node" ] && [ "$npass" -eq "$rows" ] \
    && [ "$nfail" -eq 0 ] && [ "$order" = ok ] && [ "$rows" -gt 0 ]; then
    if [ "$d" -eq "$dn" ]; then verdict=PASS; elif [ $((d + amb)) -eq "$dn" ]; then verdict=AMBIGUOUS; fi
  fi
  [ "$verdict" != FAIL ] || echo "FAIL $node exit=${rc:-?} summary='${summary:-none}' order=$order" >> "$tmp/$pod.report"
  echo "$verdict" > "$tmp/$pod.verdict"
}

# canary_logs -> $tmp/attempts.tsv from every agent's last 10 minutes, read after the runs so their
# deny lines are in.
canary_logs() {
  agent_logs 10m
  deny_attempts "$tmp/agents.log" > "$tmp/attempts.tsv"
}

canary() {
  local only=$1 root probe matrix n live running nodes want pod node hostip b p fail=0 amb=0 again k0 k1 lo="" hi="" round
  root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
  probe=$root/cluster/netpol-canary/probe.sh
  matrix=$root/cluster/netpol-canary/matrix.txt
  [ -f "$probe" ] && [ -f "$matrix" ] || die "missing $probe or $matrix"
  # shellcheck source=/dev/null
  . "$probe"

  kc -n "$CANARY_NS" get pods -l "$PROBE_SELECTOR" -o json > "$tmp/probes.json" 2> /dev/null \
    || die "cannot list pods in $CANARY_NS; is KUBECONFIG right?"
  n=$(items_len "$tmp/probes.json"); count_or_fail "the probe pod count" "$n"
  jq -r '.items[] | select(.metadata.deletionTimestamp == null)
      | [.metadata.name, (.spec.nodeName // "-"), (.status.phase // "-"), (.status.hostIP // "-")] | @tsv' \
    "$tmp/probes.json" > "$tmp/probes.all" 2> /dev/null || parse_fail "the probe pod list"
  [ -s "$tmp/probes.all" ] || die "no netpol-probe pods in $CANARY_NS"
  if [ -n "$only" ]; then
    awk -F'\t' -v n="$only" '$2 == n' "$tmp/probes.all" > "$tmp/probes.tsv"
    want=1
  else
    cp "$tmp/probes.all" "$tmp/probes.tsv"
    want=$CANARY_NODES
  fi
  live=$(wc -l < "$tmp/probes.tsv")
  running=$(awk -F'\t' '$3 == "Running"' "$tmp/probes.tsv" | wc -l)
  nodes=$(awk -F'\t' '$3 == "Running" { print $2 }' "$tmp/probes.tsv" | sort -u | wc -l)
  if [ "$live" -ne "$want" ] || [ "$running" -ne "$want" ] || [ "$nodes" -ne "$want" ]; then
    sed 's/^/  /' "$tmp/probes.all" >&2
    [ -z "$only" ] || die "need exactly 1 Running netpol-probe pod on $only (found $live, $running Running)"
    die "need exactly $CANARY_NODES Running netpol-probe pods on $CANARY_NODES nodes (found $live, $running Running, on $nodes nodes)"
  fi

  # Each pod's rows, in the order probe.sh runs them: matrix.txt expanded with that pod's node
  # name and host IP. $WAN_IP stays a placeholder; the pod's own table has the address it dialled.
  while IFS=$'\t' read -r pod node _ hostip; do
    NODE_NAME=$node HOST_IP=$hostip WAN_IP='$WAN_IP' probe_rows "$matrix" canary > "$tmp/$pod.rows" 2> /dev/null \
      || die "matrix.txt does not expand for $node"
  done < "$tmp/probes.tsv"
  # Post-DNAT destinations for each backend row: IP:PORT, port anchored.
  mkdir -p "$tmp/dst"
  cat "$tmp"/*.rows | awk '$1 == "dropped" && $5 != "-" { print $5, $4 }' | sort -u > "$tmp/backends.txt"
  while read -r b p; do
    backend_dsts "${b%%/*}" "${b#*/}" "$p" > "$tmp/dst/${b%%/*}_${b#*/}_$p"
  done < "$tmp/backends.txt"

  while IFS=$'\t' read -r pod node _ _; do
    echo "canary: $node $(wc -l < "$tmp/$pod.rows") rows, exec timeout $(exec_timeout "$(wc -l < "$tmp/$pod.rows")") s"
    canary_exec "$pod" &
  done < "$tmp/probes.tsv"
  wait
  canary_logs
  policed_namespaces
  cluster_pods
  while IFS=$'\t' read -r pod node _ _; do
    canary_judge "$pod" "$node"
    IFS=$'\t' read -r k0 k1 < "$tmp/$pod.window"
    if [ -n "$k0" ]; then
      [ -n "$lo" ] && [[ ! "$k0" < "$lo" ]] || lo=$k0
      [ -n "$hi" ] && [[ ! "$k1" > "$hi" ]] || hi=$k1
    fi
  done < "$tmp/probes.tsv"

  # An ambiguous pod runs again with a fresh window, up to CANARY_RERUNS times. The loop's run
  # recurs every PROBE_INTERVAL; its two attempts for the overlapping rows, 3 s apart, can straddle
  # the end of one run and the start of the next (seen live, 2026-11-06), but no third. So of four
  # back-to-back runs (about 65 s each) at least two are free of it; claim_rows pairs the rest.
  for round in $(seq "$CANARY_RERUNS"); do
    awk -F'\t' '{ print $1 "\t" $2 }' "$tmp/probes.tsv" | while IFS=$'\t' read -r pod node; do
      [ "$(cat "$tmp/$pod.verdict")" = AMBIGUOUS ] && printf '%s\t%s\n' "$pod" "$node"
    done > "$tmp/again.tsv"
    [ -s "$tmp/again.tsv" ] || break
    while IFS=$'\t' read -r pod node; do
      echo "RERUN $node $pod ($round of $CANARY_RERUNS): overlapping rows ambiguous, one more run"
      grep '^AMBIGUOUS ' "$tmp/$pod.report" | sed 's/^/  last run: /'
      canary_exec "$pod" &
    done < "$tmp/again.tsv"
    wait
    canary_logs
    while IFS=$'\t' read -r pod node; do
      canary_judge "$pod" "$node"
      IFS=$'\t' read -r k0 k1 < "$tmp/$pod.window"
      if [ -n "$k0" ]; then
        [ -n "$lo" ] && [[ ! "$k0" < "$lo" ]] || lo=$k0
        [ -n "$hi" ] && [[ ! "$k1" > "$hi" ]] || hi=$k1
      fi
    done < "$tmp/again.tsv"
  done

  while IFS=$'\t' read -r pod node _ _; do
    cat "$tmp/$pod.report"
    case $(cat "$tmp/$pod.verdict") in
      PASS) ;;
      AMBIGUOUS) amb=$((amb + 1)) ;;
      *) fail=1 ;;
    esac
  done < "$tmp/probes.tsv"

  # stray over every run's window, from the first T0 to the last T1.
  if [ -n "$lo" ]; then klog_window "$lo" "$hi" < "$tmp/agents.log" > "$tmp/agents.run"; else : > "$tmp/agents.run"; fi
  if stray_check "$tmp/agents.run"; then
    echo "stray: PASS ($(stray_counts), policed: ${POLICED:-none})"
  else
    echo "stray: FAIL ($STRAY_N of $DENY_N deny lines stray, policed: ${POLICED:-none})"
    fail=1
  fi

  [ "$want" -eq 1 ] && n="1 node" || n="$want nodes"
  if [ "$fail" -ne 0 ]; then
    echo "canary: FAIL ($n)"
    return 1
  fi
  if [ "$amb" -ne 0 ]; then
    echo "canary: AMBIGUOUS ($amb of $n still ambiguous after $((CANARY_RERUNS + 1)) runs; run it again)"
    return 2
  fi
  echo "canary: PASS ($n)"
  return 0
}

# norm_promql < EXPR -> EXPR with no whitespace or parentheses and each {...}'s matchers sorted.
# Prometheus reports a rule's query in its own form (matchers sorted, its own spacing and grouping),
# so both sides go through this before comparing. It drops grouping, and a matcher value holding
# a comma would split wrongly; neither matters for the canary's expression.
norm_promql() {
  jq -Rrs 'gsub("[\\s()]"; "") | [scan("\\{[^}]*\\}|[^{]+")]
    | map(if startswith("{") then "{" + (.[1:-1] | split(",") | sort | join(",")) + "}" else . end) | join("")'
}

# alert -- Prometheus loads and evaluates NetpolCanaryFailing as written. check-prometheus-rules.sh
# exits 0 when every selected rule loads, so a PrometheusRule that is missing or unlabelled passes it
# unseen; and an update keeps the object's uid, so the rule file's name cannot show which version
# loaded. This asks Prometheus for the rule by name: one group named netpol-canary, from the live
# object's uid file, health ok, its `for` as written, and its query equal to the object's expr.
alert() {
  local uid forsec got want_q have_q
  kc -n prometheus get prometheusrule netpol-canary -o json > "$tmp/pr.json" 2> /dev/null \
    || die "cannot read PrometheusRule prometheus/netpol-canary"
  uid=$(jq -r '.metadata.uid // empty' "$tmp/pr.json" 2> /dev/null) || parse_fail "the PrometheusRule"
  forsec=$(dur_seconds "$(jq -r '[.spec.groups[].rules[] | select(.alert == "NetpolCanaryFailing") | .for][0] // empty' "$tmp/pr.json" 2> /dev/null)")
  want_q=$(jq -j '[.spec.groups[].rules[] | select(.alert == "NetpolCanaryFailing") | .expr][0] // empty' "$tmp/pr.json" 2> /dev/null | norm_promql)
  kc get --raw '/api/v1/namespaces/prometheus/services/prometheus-operated:9090/proxy/api/v1/rules?rule_name[]=NetpolCanaryFailing' \
    > "$tmp/rules.json" 2> /dev/null || die "cannot query Prometheus rules"
  got=$(jq -r --arg file "prometheus-netpol-canary-$uid.yaml" '
      [.data.groups[] | select(.name == "netpol-canary") | . as $g | .rules[] | select(.name == "NetpolCanaryFailing")
        | [($g.file | split("/") | last) == $file, .health, .state, (.duration | tostring)] | @tsv] | .[]' \
    "$tmp/rules.json" 2> /dev/null) || parse_fail "the rules API answer"
  have_q=$(jq -j '[.data.groups[] | select(.name == "netpol-canary") | .rules[] | select(.name == "NetpolCanaryFailing") | .query][0] // empty' \
    "$tmp/rules.json" 2> /dev/null | norm_promql)
  if [ -n "$uid" ] && [ -n "$forsec" ] && [ "$(wc -l <<< "$got")" -eq 1 ] \
    && [ "$got" = "$(printf 'true\tok\t%s\t%s' "$(cut -f3 <<< "$got")" "$forsec")" ] && [ -n "$want_q" ] && [ "$have_q" = "$want_q" ]; then
    echo "PASS group netpol-canary from prometheus-netpol-canary-$uid.yaml: NetpolCanaryFailing health=ok state=$(cut -f3 <<< "$got") for=${forsec}s, query matches the object's expr"
    echo "alert: PASS"
    return 0
  fi
  echo "FAIL group netpol-canary: want one NetpolCanaryFailing from prometheus-netpol-canary-${uid:-?}.yaml, health ok, for ${forsec:-?}s; got '${got:-nothing}' (uid-file health state for)"
  [ "$have_q" = "$want_q" ] || printf 'FAIL query: loaded  %s\nFAIL query: object  %s\n' "${have_q:-none}" "${want_q:-none}"
  echo "alert: FAIL"
  return 1
}

selftest() {
  local root probe nfail=0 t line fixture
  root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
  probe=$root/cluster/netpol-canary/probe.sh
  [ -f "$probe" ] || die "missing $probe"
  # shellcheck source=/dev/null
  . "$probe"
  tmp_st=$(mktemp -d); trap 'rm -rf "${tmp_st-}" "${tmp-}"' EXIT
  ok() { echo "ok $1"; }
  bad() { echo "FAIL $1"; nfail=$((nfail + 1)); }
  eq() { [ "$2" = "$3" ] && ok "$1" || bad "$1 (want '$3', got '$2')"; }
  rc() { local want=$1 name=$2; shift 2; "$@" > /dev/null 2>&1; local got=$?; [ "$got" -eq "$want" ] && ok "$name" || bad "$name (want exit $want, got $got)"; }

  eq "classify 0" "$(probe_classify 0 "")" open
  eq "classify TIMEOUT" "$(probe_classify 1 TIMEOUT)" timeout
  eq "classify REFUSED" "$(probe_classify 1 REFUSED)" refused
  eq "classify DNS" "$(probe_classify 1 "DNS: lookup x: no such host")" other
  eq "classify OTHER" "$(probe_classify 1 "OTHER: connect: no route to host")" other

  for t in "dropped timeout enforced" "dropped refused baseline" "dropped open baseline" "open open enforced" "dns open baseline" "dns open enforced" "open open baseline"; do
    # shellcheck disable=SC2086
    rc 0 "judge pass $t" probe_judge $t
  done
  for t in "dropped refused enforced" "dropped open enforced" "dropped timeout baseline" "open timeout enforced" "dns other enforced" "open refused baseline" "dropped other enforced"; do
    # shellcheck disable=SC2086
    rc 1 "judge fail $t" probe_judge $t
  done

  fixture=$tmp_st/fixture.txt
  printf '%s\n' '# comment' 'dns all a 53 - n' 'open canary b 9376 - n' 'dropped all $HOST_IP 50000 - n' > "$fixture"
  eq "rows runner count" "$(HOST_IP=1.2.3.4 probe_rows "$fixture" runner | wc -l)" 2
  eq "rows canary count" "$(HOST_IP=1.2.3.4 probe_rows "$fixture" canary | wc -l)" 3
  rc 2 "rows HOST_IP unset" env -u HOST_IP bash -c '. "$1"; probe_rows "$2" runner' _ "$probe" "$fixture"
  printf '%s\n' 'dns all a 53 - n' 'open canary b 9376 - n' > "$tmp_st/novars.txt"
  rc 0 "rows no var needed" env -u HOST_IP bash -c '. "$1"; probe_rows "$2" runner' _ "$probe" "$tmp_st/novars.txt"
  case $(HOST_IP=192.168.0.242 probe_rows "$fixture" canary) in
    *"192.168.0.242 50000"*) ok "rows HOST_IP expanded" ;; *) bad "rows HOST_IP expanded" ;;
  esac
  case $(WAN_IP=9.9.9.9 HOST_IP=192.168.0.240 probe_rows "$root/cluster/netpol-canary/matrix.txt" canary) in
    *"9.9.9.9 8585"*) ok "rows WAN_IP expanded" ;; *) bad "rows WAN_IP expanded" ;;
  esac

  # $PEER_IP: the node after HOST_IP in NODE_IPS, wrapping (top -> middle -> bottom -> top).
  eq "peer of top" "$(probe_peer_ip 192.168.0.240)" 192.168.0.241
  eq "peer of middle" "$(probe_peer_ip 192.168.0.241)" 192.168.0.242
  eq "peer of bottom" "$(probe_peer_ip 192.168.0.242)" 192.168.0.240
  rc 1 "peer of a non-node" probe_peer_ip 10.0.0.1
  rc 1 "peer of nothing" probe_peer_ip ""
  eq "peer NODE_IPS override" "$(NODE_IPS="a b" probe_peer_ip b)" a
  rc 1 "peer of a lone node" env NODE_IPS=a bash -c '. "$1"; probe_peer_ip a' _ "$probe"
  case $(HOST_IP=192.168.0.242 WAN_IP=9.9.9.9 probe_rows "$root/cluster/netpol-canary/matrix.txt" canary) in
    *"192.168.0.240 10250"*) ok "rows PEER_IP expanded" ;; *) bad "rows PEER_IP expanded" ;;
  esac
  printf '%s\n' 'dropped all $PEER_IP 10250 - n' > "$tmp_st/peer.txt"
  rc 2 "rows PEER_IP, HOST_IP unset" env -u HOST_IP bash -c '. "$1"; probe_rows "$2" runner' _ "$probe" "$tmp_st/peer.txt"
  rc 2 "rows PEER_IP, HOST_IP not a node" env HOST_IP=10.0.0.1 bash -c '. "$1"; probe_rows "$2" runner' _ "$probe" "$tmp_st/peer.txt"

  # A probe skips its own node's BMC (middle cannot reach its own); every BMC is still dialled from
  # the other two nodes, and with NODE_NAME unknown probe_rows skips nothing (fail closed).
  local m=$root/cluster/netpol-canary/matrix.txt bmc node from all
  eq "own BMC of top" "$(probe_own_bmc top)" 192.168.0.119
  eq "own BMC of middle" "$(probe_own_bmc middle)" 192.168.0.117
  eq "own BMC of bottom" "$(probe_own_bmc bottom)" 192.168.0.151
  rc 1 "own BMC of a non-node" probe_own_bmc n1
  rc 1 "own BMC of nothing" probe_own_bmc ""
  case $(NODE_NAME=middle HOST_IP=192.168.0.241 WAN_IP=9.9.9.9 probe_rows "$m" canary) in
    *"192.168.0.117 443"*) bad "rows skip own BMC" ;;
    *"192.168.0.119 443"*"192.168.0.151 443"*) ok "rows skip own BMC" ;;
    *) bad "rows skip own BMC (other BMCs missing)" ;;
  esac
  all=$(NODE_NAME= HOST_IP=192.168.0.241 WAN_IP=9.9.9.9 probe_rows "$m" canary | wc -l)
  eq "rows, NODE_NAME unset, skip nothing" "$(env -u NODE_NAME HOST_IP=192.168.0.241 WAN_IP=9.9.9.9 bash -c '. "$1"; probe_rows "$2" canary' _ "$probe" "$m" | wc -l)" "$all"
  for bmc in 192.168.0.119 192.168.0.117 192.168.0.151; do
    from=0
    for node in top:192.168.0.240 middle:192.168.0.241 bottom:192.168.0.242; do
      # Captured first: under pipefail, grep -q exiting early would fail the pipeline (SIGPIPE).
      case $(NODE_NAME=${node%%:*} HOST_IP=${node#*:} WAN_IP=9.9.9.9 probe_rows "$m" canary) in *" $bmc 443 "*) from=$((from + 1)) ;; esac
    done
    eq "BMC $bmc dialled from two nodes" "$from" 2
  done
  eq "rows on middle, one fewer" "$(NODE_NAME=middle HOST_IP=192.168.0.241 WAN_IP=9.9.9.9 probe_rows "$m" canary | wc -l)" $((all - 1))

  # F9: runs start PROBE_INTERVAL apart, and never less than 5 s apart.
  eq "sleep 300 after a 54 s run" "$(probe_sleep_for 300 54)" 246
  eq "sleep 300 after a 0 s run" "$(probe_sleep_for 300 0)" 300
  eq "sleep floor, run near interval" "$(probe_sleep_for 60 58)" 5
  eq "sleep floor, run past interval" "$(probe_sleep_for 60 200)" 5

  # Stub tools: agnhost variants, and a dig that answers nothing (no WAN lookup offline).
  local stub=$tmp_st/stub t0 t1
  mkdir -p "$stub"
  printf '#!/bin/sh\nexit 0\n' > "$stub/agnhost-open"
  printf '#!/bin/sh\ncat > /dev/null\nexit 0\n' > "$stub/agnhost-reads-stdin"
  printf '#!/bin/sh\nexec sleep 30\n' > "$stub/agnhost-hangs"
  printf '#!/bin/sh\ntouch "$MARK"\nexec sleep 3\n' > "$stub/agnhost-slow"
  printf '#!/bin/sh\nexit 0\n' > "$stub/dig"
  chmod +x "$stub"/*
  printf '%s\n' 'open all 127.0.0.1 1 - a' 'dropped all 127.0.0.2 2 - b' 'dropped all 127.0.0.3 3 - c' > "$tmp_st/three.txt"

  # A tool that reads stdin must not eat the remaining rows.
  eq "rows keep their stdin" "$(MATRIX=$tmp_st/three.txt AGNHOST=$stub/agnhost-reads-stdin NODE_NAME=n1 probe_once baseline | tail -n 1)" \
    "SUMMARY 3/3 baseline node=n1"
  # PROBE_DEADLINE cuts off a hung tool, and the row counts as other: never a timeout, so never a pass.
  t0=$(date +%s)
  eq "hung tool is other" "$(PROBE_DEADLINE=1 AGNHOST=$stub/agnhost-hangs probe_check dropped 127.0.0.1 1)" other
  t1=$(date +%s)
  [ $((t1 - t0)) -le 4 ] && ok "hung tool cut off ($((t1 - t0)) s)" || bad "hung tool cut off (took $((t1 - t0)) s)"

  # loop: TERM ends it at once (exit 0), asleep or mid-run. In a pod bash is PID 1 and ignores
  # TERM without a trap. bash also defers a trap until a foreground command ends, so the loop runs
  # both the run and the sleep in the background and waits on them.
  term_loop() { # term_loop NAME AGNHOST FILE_THAT_SHOWS_IT_STARTED
    local name=$1 agn=$2 mark=$3 lp code
    rm -f "$tmp_st/ready" "$tmp_st/mark"
    (
      export PATH="$stub:$PATH" MATRIX=$tmp_st/three.txt AGNHOST=$agn MARK=$tmp_st/mark READY_FILE=$tmp_st/ready \
        PROBE_MODE=baseline PROBE_INTERVAL=300 NODE_NAME=n1
      exec bash "$probe" loop
    ) > "$tmp_st/loop-$name.out" 2>&1 &
    lp=$!
    for _ in $(seq 100); do [ -e "$mark" ] && break; sleep 0.1; done
    if [ ! -e "$mark" ]; then bad "loop TERM $name (never started)"; kill -KILL "$lp" 2> /dev/null; wait "$lp" 2> /dev/null; return; fi
    kill -TERM "$lp"
    for _ in $(seq 20); do kill -0 "$lp" 2> /dev/null || break; sleep 0.1; done
    if kill -0 "$lp" 2> /dev/null; then
      bad "loop TERM $name (still running 2 s later)"; kill -KILL "$lp"; wait "$lp" 2> /dev/null; return
    fi
    wait "$lp"; code=$?
    eq "loop TERM $name exit" "$code" 0
  }
  term_loop asleep "$stub/agnhost-open" "$tmp_st/ready"
  term_loop mid-run "$stub/agnhost-slow" "$tmp_st/mark"

  # loop, start to start (F9): with an 8 s interval and a 2 s run, the second run starts about 8 s
  # after the first. Sleeping a whole interval after each run would make it about 10 s. (Interval
  # minus run must clear the 5 s floor, or the floor sets the gap. The loop counts whole seconds,
  # so the gap is 7 s now and then.)
  local lp gap
  printf '#!/bin/sh\ndate +%%s.%%N >> "$MARK"\nsleep 2\nexit 0\n' > "$stub/agnhost-2s"
  chmod +x "$stub/agnhost-2s"
  printf '%s\n' 'open all 127.0.0.1 1 - a' > "$tmp_st/one.txt"
  rm -f "$tmp_st/starts"
  (
    export PATH="$stub:$PATH" MATRIX=$tmp_st/one.txt AGNHOST=$stub/agnhost-2s MARK=$tmp_st/starts \
      READY_FILE=$tmp_st/ready PROBE_MODE=baseline PROBE_INTERVAL=8 NODE_NAME=n1
    exec bash "$probe" loop
  ) > "$tmp_st/loop-cadence.out" 2>&1 &
  lp=$!
  for _ in $(seq 150); do
    [ -f "$tmp_st/starts" ] && [ "$(wc -l < "$tmp_st/starts")" -ge 2 ] && break
    sleep 0.1
  done
  kill -TERM "$lp" 2> /dev/null; wait "$lp" 2> /dev/null
  gap=$(awk 'NR == 1 { a = $1 } NR == 2 { printf "%.1f", $1 - a }' "$tmp_st/starts" 2> /dev/null)
  if [ -n "$gap" ] && awk -v g="$gap" 'BEGIN { exit !(g >= 6.5 && g <= 9) }'; then
    ok "loop start to start ($gap s apart, interval 8 s, run 2 s)"
  else
    bad "loop start to start (second run ${gap:-never} s after the first, want about 8)"
  fi

  # baseline's exec timeout grows with the row count: (rows + 1) x the probe's deadline + 30 s.
  eq "exec timeout, 26 rows" "$(exec_timeout 26)" $((27 * PROBE_DEADLINE_DEFAULT + 30))
  eq "exec timeout, 40 rows" "$(exec_timeout 40)" $((41 * PROBE_DEADLINE_DEFAULT + 30))
  [ "$(exec_timeout 27)" -gt "$(exec_timeout 26)" ] && ok "exec timeout grows with rows" || bad "exec timeout grows with rows"

  # probe_once with the network stubbed out: judging, summary line and exit code.
  (
    probe_check() { case $1 in dropped) echo timeout ;; *) echo open ;; esac; }
    export MATRIX=$root/cluster/netpol-canary/matrix.txt HOST_IP=192.168.0.242 WAN_IP=10.0.0.2 NODE_NAME=n1 PROBE_SCOPE=canary
    out=$(probe_once enforced); r=$?
    n=$(grep -c '^PASS ' <<< "$out")
    # Every canary row ran: the count comes from matrix.txt, never a literal (F8).
    want=$(awk '$1 !~ /^#/ && NF && ($2 == "all" || $2 == "canary")' "$MATRIX" | wc -l)
    [ "$r" -eq 0 ] && [ "$n" -eq "$want" ] && grep -q "^SUMMARY $n/$n enforced node=n1\$" <<< "$out"
  ) && ok "probe_once pass, every canary row" || bad "probe_once pass, every canary row"
  (
    probe_check() { case $1 in dropped) echo refused ;; *) echo open ;; esac; }
    export MATRIX=$root/cluster/netpol-canary/matrix.txt HOST_IP=192.168.0.242 WAN_IP=10.0.0.2 NODE_NAME=n1 PROBE_SCOPE=canary
    out=$(probe_once enforced); r=$?
    [ "$r" -ne 0 ] && grep -q '^FAIL dropped refused ' <<< "$out" && grep -q '^SUMMARY [0-9]*/[0-9]* enforced' <<< "$out"
  ) && ok "probe_once fail on refused" || bad "probe_once fail on refused"

  # The real matrix.
  local m=$root/cluster/netpol-canary/matrix.txt expect scope host port backend rest nrows=0 shape=ok
  while read -r expect scope host port backend rest; do
    case $expect in '' | '#'*) continue ;; esac
    nrows=$((nrows + 1))
    case $expect in dns | open | dropped) ;; *) shape="expect '$expect'" ;; esac
    case $scope in all | canary) ;; *) shape="scope '$scope'" ;; esac
    [[ $port =~ ^[0-9]+$ ]] || shape="port '$port'"
    [[ $backend == - || $backend =~ ^[a-z0-9-]+/[a-z0-9-]+$ ]] || shape="backend '$backend'"
  done < "$m"
  eq "matrix shape ($nrows rows)" "$shape" ok
  has() { grep -Eq "^$1[[:space:]]+(all|canary)[[:space:]]+$2[[:space:]]+$3([[:space:]]|\$)" "$m" && ok "matrix $1 $2:$3" || bad "matrix $1 $2:$3"; }
  has dns kubernetes\\.default\\.svc\\.cluster\\.local '[0-9]+'
  has open netpol-echo\\.netpol-canary\\.svc\\.cluster\\.local 9376
  has open git\\.realliance\\.net 443
  has open 1\\.1\\.1\\.1 443
  for t in netpol-echo.netpol-canary.svc.cluster.local:9377 192.168.0.117:443 192.168.0.119:443 192.168.0.151:443 \
      192.168.0.2:80 192.168.0.2:443 192.168.0.1:22 10.96.0.1:443 192.168.0.240:6443 \
      forgejo-http.forgejo.svc.cluster.local:3000 192.168.0.7:443 192.168.0.8:1883 192.168.0.230:2049 \
      192.168.0.231:5000 192.168.0.240:50000 '$HOST_IP:50000' '$WAN_IP:8585' \
      192.168.0.5:6443 '$PEER_IP:10250' '$PEER_IP:50000' 192.168.0.1:53 netpol-echo-pod.netpol-canary.svc.cluster.local:9378; do
    has dropped "$(printf '%s' "${t%:*}" | sed 's/[.$]/\\&/g')" "${t##*:}"
  done

  # deny_match
  local log=$tmp_st/deny.log pod=netpol-canary/netpol-probe-abcde
  line='I1103 18:00:00.000000       1 verdictlog.go:172] "Packet verdict" id=7 verdict="deny" family="IPv4" protocol="TCP" src="10.244.0.9:40000" dst="192.168.0.117:443" srcPod="netpol-canary/netpol-probe-abcde" dstPod="external" direction="egress" evaluator="x"'
  printf '%s\n' "$line" > "$log"
  rc 0 "deny_match ip:port" deny_match "$log" $pod 192.168.0.117:443
  rc 0 "deny_match ip:" deny_match "$log" $pod 192.168.0.117:
  rc 1 "deny_match wrong port" deny_match "$log" $pod 192.168.0.117:44
  rc 1 "deny_match dot is literal" deny_match "$log" $pod 192x168.0.117:443
  rc 1 "deny_match ip prefix" deny_match "$log" $pod 192.168.0.11:
  rc 1 "deny_match other pod" deny_match "$log" netpol-canary/netpol-probe-zzzzz 192.168.0.117:443
  rc 1 "deny_match pod prefix" deny_match "$log" netpol-canary/netpol-probe-abcd 192.168.0.117:443
  printf '%s\n' "${line/verdict=\"deny\"/verdict=\"accept\"}" > "$log"
  rc 1 "deny_match accept verdict" deny_match "$log" $pod 192.168.0.117:443
  printf '%s\n' 'I1103 18:00:00.000000       1 x.go:1] "Packet verdict" id=7 verdict=deny protocol=TCP src=10.244.0.9:40000 dst=192.168.0.117:443 srcPod=netpol-canary/netpol-probe-abcde dstPod=external' > "$log"
  rc 0 "deny_match unquoted" deny_match "$log" $pod 192.168.0.117:443
  printf '%s\n' "${line/netpol-probe-abcde/netpol-probe-abcdef}" > "$log"
  rc 1 "deny_match longer pod name" deny_match "$log" $pod 192.168.0.117:443
  # A real log: the match first, then thousands of lines from the same pod. Under pipefail, a
  # `grep -q` that stops at the first match makes the greps before it die of SIGPIPE, and the
  # pipeline reads as no match.
  { printf '%s\n' "$line"; for t in $(seq 20000); do printf '%s\n' "${line/192.168.0.117:443/192.168.0.2:$t}"; done; } > "$log"
  rc 0 "deny_match long log, match first" deny_match "$log" $pod 192.168.0.117:443

  # sync_error: either case of upstream's line, keyed on error=; a long log must not hide it (M1).
  local ok_sync='I1105 22:00:01.000000       1 controller.go:508] "Syncing nftables rules" logger="nftables-sync" elapsed="1.1ms"'
  local bad_sync='I1105 22:00:00.000000       1 controller.go:976] "syncing nftables rules" logger="nftables-sync" error="netlink receive: no such file or directory"'
  printf '%s\n' "$ok_sync" "$ok_sync" > "$log"
  rc 1 "sync_error clean syncs" sync_error "$log"
  printf '%s\n' "$ok_sync" "$bad_sync" > "$log"
  rc 0 "sync_error upstream error line" sync_error "$log"
  printf '%s\n' "${bad_sync/syncing/Syncing}" > "$log"
  rc 0 "sync_error capital S" sync_error "$log"
  { printf '%s\n' "$bad_sync"; for t in $(seq 20000); do printf '%s\n' "$ok_sync"; done; } > "$log"
  rc 0 "sync_error long log, error first" sync_error "$log"

  # netpol-connect.js
  local js=$root/scripts/netpol-connect.js lport lpid
  if command -v node > /dev/null 2>&1; then
    node -e 'const s=require("net").createServer(c=>c.end()).listen(0,"127.0.0.1",()=>{console.log(s.address().port);setTimeout(()=>process.exit(0),20000)})' > "$tmp_st/port" &
    lpid=$!
    for _ in $(seq 50); do [ -s "$tmp_st/port" ] && break; sleep 0.1; done
    lport=$(cat "$tmp_st/port")
    eq "js tcp open" "$(node "$js" tcp 127.0.0.1 "$lport")" open
    kill "$lpid" 2> /dev/null
    eq "js tcp refused" "$(node "$js" tcp 127.0.0.1 1)" refused
    eq "js dns localhost" "$(node "$js" dns localhost)" open
    eq "js dns invalid" "$(node "$js" dns no-such-host.invalid)" other:ENOTFOUND
    eq "js via node -e" "$(node -e "$(cat "$js")" tcp 127.0.0.1 1)" refused
    # Bad arguments print other:usage. Never a stack trace, a dial to localhost, or a guess.
    eq "js usage: no host" "$(node "$js" tcp 2> /dev/null)" other:usage
    eq "js usage: no port" "$(node "$js" tcp 127.0.0.1 2> /dev/null)" other:usage
    eq "js usage: port not a number" "$(node "$js" tcp 127.0.0.1 http 2> /dev/null)" other:usage
    eq "js usage: port 0" "$(node "$js" tcp 127.0.0.1 0 2> /dev/null)" other:usage
    eq "js usage: port too big" "$(node "$js" tcp 127.0.0.1 65536 2> /dev/null)" other:usage
    eq "js usage: extra argument" "$(node "$js" tcp 127.0.0.1 1 x 2> /dev/null)" other:usage
    eq "js usage: dns, no host" "$(node "$js" dns 2> /dev/null)" other:usage
    eq "js usage: no mode" "$(node "$js" 2> /dev/null)" other:usage
  else
    bad "node not found (netpol-connect.js untested)"
  fi

  if [ "$nfail" -eq 0 ]; then echo "selftest: PASS"; return 0; fi
  echo "selftest: FAIL ($nfail)"; return 1
}

[ "$#" -ge 1 ] || usage
CHECK=$1
shift
case "$CHECK" in
  idle | selftest | baseline | alert) [ "$#" -eq 0 ] || usage; "$CHECK" ;;
  canary)
    if [ "$#" -eq 0 ]; then canary ""
    elif [ "$#" -eq 2 ] && [ "$1" = --node ] && [ -n "$2" ]; then canary "$2"
    else usage; fi ;;
  stray)
    if [ "$#" -eq 0 ]; then stray 15m
    elif [ "$#" -eq 2 ] && [ "$1" = --since ] && [[ "$2" =~ ^[0-9]+[smh]$ ]]; then stray "$2"
    else usage; fi ;;
  *) usage ;;
esac
