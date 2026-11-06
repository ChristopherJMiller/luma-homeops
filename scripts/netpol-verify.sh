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
# cluster access, no agent pods, or policies exist for `idle`).
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
# dropped row needs a deny line, stamped after this run began, from that pod, in
# the row's direction (row_direction): to the literal host:port when the row's
# backend is "-", or else to one of the backend's post-DNAT IP:PORTs (§3.4 item
# 1, backend_dsts), port anchored, and from a connect attempt (source port) of
# its own (claim_rows). A timeout that no agent logged is a FAIL: the
# packet may never have reached an enforcer. Then `stray` over the same lines.
# Prints `<node> rows <p>/<N> deny-logged <d>/<D>` per pod. Exit 2 unless the
# pods are there (3 Running on 3 nodes, or 1 on --node).
#
# stray -- every deny line in every agent's log for --since must name a pod
# (srcPod or dstPod) in a namespace that has a NetworkPolicy (Review Focus 3),
# and each policed pod it names must have a pod IP and exist now (stray_check).
# It reads each agent's previous container too, and FAILs when a log does not
# reach back to --since (a rotated file, a replaced pod). Prints a `window` line
# per agent, up to 10 `STRAY [why] <line>`, then the verdict.
#
# alert -- Prometheus has loaded the NetpolCanaryFailing rule, checked by name (alert()).
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
    local why=() m="$tmp/$pod.metrics" log="$tmp/$pod.log" v f total drops pid="?" packets="?" fl="?" sync
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
      # Captured, then tested: a pipeline into grep -q could die of SIGPIPE on a long log and miss it.
      # Upstream logs "Syncing nftables rules" at each sync and "syncing nftables rules" error=... on failure.
      sync=$(grep -iF 'syncing nftables rules' "$log")
      grep -Eq '(^| )error=' <<< "$sync" && why+=("nftables-sync-error")
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

# deny_attempts LOG -> one "srcPod<TAB>srcPort<TAB>dst<TAB>direction" per deny line. The enforcer
# judges before SNAT, so a connect attempt keeps one source port across its SYN retransmits, even
# when kube-proxy sends each SYN to another backend: the source port names the attempt.
deny_attempts() {
  grep -F '"Packet verdict"' "$1" | awk '
    function val(key,   s, v) {
      s = " " $0
      if (!match(s, " " key "=\"?[^\" ]*")) return ""
      v = substr(s, RSTART + length(key) + 2, RLENGTH - length(key) - 2)
      sub(/^"/, "", v)
      return v
    }
    val("verdict") == "deny" { sp = val("src"); sub(/^.*:/, "", sp); print val("srcPod") "\t" sp "\t" val("dst") "\t" val("direction") }'
}

# claim_rows REQS ATTEMPTS -> "row<TAB>1|0" per row of REQS ("row<TAB>direction<TAB>dst,dst,...").
# ATTEMPTS is one pod's deny_attempts. Each must-drop row needs its own logged attempt: a deny in
# the row's direction to one of its destinations, from a source port that no row overlapping it
# has claimed. Rows with the same destinations and direction (top's .240:50000 twice) are the same
# traffic and share one claim. Narrower rows claim first, so the literal 192.168.0.240:6443 row and
# the 10.96.0.1:443 row (which can land on .240:6443) need two attempts between them. When the
# agents logged only one, the wider row reads NOT-LOGGED. Greedy matching is exact while overlapping rows nest,
# as they do in matrix.txt.
claim_rows() {
  awk -F'\t' '
    function overlaps(a, b,   n, arr, i) {
      n = split(cands[a], arr, ",")
      for (i = 1; i <= n; i++) if ((b, arr[i]) in incand) return 1
      return 0
    }
    FNR == NR {
      key = $2 "|" $3
      if (!(key in rid)) {
        nreq++; rid[key] = nreq; rdir[nreq] = $2; cands[nreq] = $3
        ncand[nreq] = split($3, c, ",")
        for (i = 1; i <= ncand[nreq]; i++) incand[nreq, c[i]] = 1
      }
      rowreq[++nrows] = rid[key]; rowid[nrows] = $1
      next
    }
    { nl++; lsp[nl] = $2; ldst[nl] = $3; ldir[nl] = $4 }
    END {
      for (i = 1; i <= nreq; i++) ord[i] = i
      for (i = 1; i <= nreq; i++)
        for (j = i + 1; j <= nreq; j++)
          if (ncand[ord[j]] < ncand[ord[i]] || (ncand[ord[j]] == ncand[ord[i]] && ord[j] < ord[i])) { t = ord[i]; ord[i] = ord[j]; ord[j] = t }
      for (o = 1; o <= nreq; o++) {
        r = ord[o]; ok[r] = 0
        for (l = 1; l <= nl && !ok[r]; l++) {
          if (ldir[l] != rdir[r] || !((r, ldst[l]) in incand)) continue
          blocked = 0
          for (q = 1; q <= nreq; q++) if (((q, lsp[l]) in claimed) && overlaps(q, r)) { blocked = 1; break }
          if (!blocked) { claimed[r, lsp[l]] = 1; ok[r] = 1 }
        }
      }
      for (k = 1; k <= nrows; k++) print rowid[k] "\t" (ok[rowreq[k]] ? 1 : 0)
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

# klog_since KEY < LOG -> the lines stamped at or after KEY ("MMDD HH:MM:SS", UTC). A klog header
# has no year, so a window across New Year reads short, which fails closed.
klog_since() { awk -v k="$1" 'substr($0, 2, 4) " " substr($0, 7, 8) >= k'; }

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

# policed_pods -> $tmp/pods-now.txt: ns/name of every pod that exists now in a policed namespace.
policed_pods() {
  local ns
  : > "$tmp/pods-now.txt"
  for ns in ${POLICED//,/ }; do
    kc -n "$ns" get pods -o json > "$tmp/nspods.json" 2> /dev/null < /dev/null || die "cannot list pods in $ns"
    jq -r --arg ns "$ns" '.items[] | "\($ns)/\(.metadata.name)"' "$tmp/nspods.json" >> "$tmp/pods-now.txt" 2> /dev/null \
      || parse_fail "the pod list of $ns"
  done
}

# stray_check LOG -> prints up to 10 "STRAY [why] <line>" and sets DENY_N and STRAY_N; 0 only if no
# line is stray (Review Focus 3). Each deny line must name a pod (srcPod or dstPod, ns/name) in a
# namespace in $POLICED, and each side it names there must have a pod IP (10.244.0.0/16) and a pod
# that exists now ($tmp/pods-now.txt). "external" names no pod. A line naming no policed pod means
# the enforcer judged traffic that no policy selects. A node IP on the policed side is the #380
# class (a hostNetwork pod enrolled). A pod that is gone can be a stale IP-to-pod mapping, or a pod
# deleted inside the window: run `stray` with a window that starts after the last rollout there.
stray_check() {
  grep -F '"Packet verdict"' "$1" | grep -E '(^| )verdict="?deny"?( |$)' > "$tmp/deny.txt"
  DENY_N=$(wc -l < "$tmp/deny.txt")
  awk -v policed="$POLICED" -v podsfile="$tmp/pods-now.txt" '
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
      while ((getline l < podsfile) > 0) live[l] = 1
    }
    {
      why = ""; sides = 0
      for (k = 1; k <= 2; k++) {
        pod = val(k == 1 ? "srcPod" : "dstPod")
        ip = val(k == 1 ? "src" : "dst"); sub(/:[0-9]+$/, "", ip)
        if (!(nsof(pod) in ok)) continue
        sides++
        if (index(ip, "10.244.") != 1) why = why "; " pod " has ip " ip ", outside 10.244.0.0/16"
        else if (!(pod in live)) why = why "; " pod " is gone"
      }
      if (sides == 0) why = "; no pod in a policed namespace"
      if (why != "") print "[" substr(why, 3) "] " $0
    }
  ' "$tmp/deny.txt" > "$tmp/stray.txt"
  STRAY_N=$(wc -l < "$tmp/stray.txt")
  head -n 10 "$tmp/stray.txt" | sed 's/^/STRAY /'
  [ "$STRAY_N" -eq 0 ]
}

stray() {
  local want short=0 pod node covered from rc=0
  want=$(dur_seconds "$1")
  agent_logs "$1"
  policed_namespaces
  policed_pods
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
    echo "stray: PASS ($DENY_N deny lines, policed: ${POLICED:-none})"
    return 0
  fi
  if [ "$short" -eq 0 ]; then
    echo "stray: FAIL ($STRAY_N of $DENY_N deny lines stray, policed: ${POLICED:-none})"
  else
    echo "stray: FAIL ($STRAY_N of $DENY_N deny lines stray, $short agent logs short of $1, policed: ${POLICED:-none})"
  fi
  return 1
}

canary() {
  local only=$1 root probe matrix n live running nodes want pod node hostip rows limit rc summary npass nfail
  local fail=0 row line expect host port backend lexpect lhp logged d dn order w b p dir t0 i
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

  # Only deny lines stamped after this run began count, less 30 s for clock skew between here and
  # the nodes. An earlier run's lines, or a deleted pod's, prove nothing about this one.
  t0=$(date -u -d "@$(($(date -u +%s) - 30))" '+%m%d %H:%M:%S')
  while IFS=$'\t' read -r pod node _ _; do
    rows=$(wc -l < "$tmp/$pod.rows")
    limit=$(exec_timeout "$rows")
    echo "canary: $node $rows rows, exec timeout ${limit} s"
    (
      timeout "$limit" kubectl -n "$CANARY_NS" exec "$pod" -c probe -- bash /etc/netpol-probe/probe.sh once enforced \
        > "$tmp/$pod.out" 2> "$tmp/$pod.err" < /dev/null
      echo $? > "$tmp/$pod.rc"
    ) &
  done < "$tmp/probes.tsv"
  wait

  # Read after the runs, so their deny lines are in.
  agent_logs 10m
  klog_since "$t0" < "$tmp/agents.log" > "$tmp/agents.run"
  policed_namespaces
  policed_pods
  # Post-DNAT destinations for each backend row: IP:PORT, port anchored.
  mkdir -p "$tmp/dst"
  cat "$tmp"/*.rows | awk '$1 == "dropped" && $5 != "-" { print $5, $4 }' | sort -u > "$tmp/backends.txt"
  while read -r b p; do
    backend_dsts "${b%%/*}" "${b#*/}" "$p" > "$tmp/dst/${b%%/*}_${b#*/}_$p"
  done < "$tmp/backends.txt"
  deny_attempts "$tmp/agents.run" > "$tmp/attempts.tsv"

  while IFS=$'\t' read -r pod node _ _; do
    rows=$(wc -l < "$tmp/$pod.rows")
    rc=$(cat "$tmp/$pod.rc" 2> /dev/null)
    echo "== $node $pod (exit ${rc:-?})"
    cat "$tmp/$pod.out" 2> /dev/null
    [ ! -s "$tmp/$pod.err" ] || head -n 5 "$tmp/$pod.err" | sed 's/^/   stderr: /'
    summary=$(grep '^SUMMARY ' "$tmp/$pod.out" 2> /dev/null | tail -n 1)
    grep -E '^(PASS|FAIL) ' "$tmp/$pod.out" > "$tmp/$pod.lines" 2> /dev/null
    npass=$(grep -c '^PASS ' "$tmp/$pod.lines")
    nfail=$(grep -c '^FAIL ' "$tmp/$pod.lines")
    # Row i of the repo's expansion against line i of the pod's table: a stale ConfigMap shows here.
    d=0 dn=0 order=ok i=0
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
    awk -F'\t' -v p="$CANARY_NS/$pod" '$1 == p' "$tmp/attempts.tsv" > "$tmp/$pod.attempts"
    claim_rows "$tmp/$pod.reqs" "$tmp/$pod.attempts" > "$tmp/$pod.claims"
    while IFS=$'\t' read -r i logged; do
      if [ "$logged" = 1 ]; then d=$((d + 1)); continue; fi
      IFS=$'\t' read -r _ dir w < <(awk -F'\t' -v i="$i" '$1 == i' "$tmp/$pod.reqs")
      IFS=$'\t' read -r _ lhp b < <(awk -F'\t' -v i="$i" '$1 == i' "$tmp/$pod.labels")
      echo "NOT-LOGGED $lhp (no $dir deny of its own from $CANARY_NS/$pod to $b: ${w:-none})"
    done < "$tmp/$pod.claims"
    echo "$node rows $npass/$rows deny-logged $d/$dn"
    if [ "${rc:-}" = 0 ] && [ "$summary" = "SUMMARY $rows/$rows enforced node=$node" ] && [ "$npass" -eq "$rows" ] \
      && [ "$nfail" -eq 0 ] && [ "$order" = ok ] && [ "$d" -eq "$dn" ] && [ "$rows" -gt 0 ]; then
      :
    else
      echo "FAIL $node exit=${rc:-?} summary='${summary:-none}' order=$order"
      fail=1
    fi
  done < "$tmp/probes.tsv"

  if stray_check "$tmp/agents.run"; then
    echo "stray: PASS ($DENY_N deny lines, policed: ${POLICED:-none})"
  else
    echo "stray: FAIL ($STRAY_N of $DENY_N deny lines stray, policed: ${POLICED:-none})"
    fail=1
  fi

  [ "$want" -eq 1 ] && n="1 node" || n="$want nodes"
  if [ "$fail" -eq 0 ]; then
    echo "canary: PASS ($n)"
    return 0
  fi
  echo "canary: FAIL ($n)"
  return 1
}

# alert -- Prometheus loads and evaluates NetpolCanaryFailing. check-prometheus-rules.sh exits 0 when every
# selected rule loads, so a PrometheusRule that is missing or unlabelled passes it unseen. This
# asks Prometheus for the rule by name: one group named netpol-canary, from the live object's uid
# file, the rule's health ok and its `for` as written.
alert() {
  local uid forsec got
  kc -n prometheus get prometheusrule netpol-canary -o json > "$tmp/pr.json" 2> /dev/null \
    || die "cannot read PrometheusRule prometheus/netpol-canary"
  uid=$(jq -r '.metadata.uid // empty' "$tmp/pr.json" 2> /dev/null) || parse_fail "the PrometheusRule"
  forsec=$(dur_seconds "$(jq -r '[.spec.groups[].rules[] | select(.alert == "NetpolCanaryFailing") | .for][0] // empty' "$tmp/pr.json" 2> /dev/null)")
  kc get --raw '/api/v1/namespaces/prometheus/services/prometheus-operated:9090/proxy/api/v1/rules?rule_name[]=NetpolCanaryFailing' \
    > "$tmp/rules.json" 2> /dev/null || die "cannot query Prometheus rules"
  got=$(jq -r --arg file "prometheus-netpol-canary-$uid.yaml" '
      [.data.groups[] | select(.name == "netpol-canary") | . as $g | .rules[] | select(.name == "NetpolCanaryFailing")
        | [($g.file | split("/") | last) == $file, .health, .state, (.duration | tostring)] | @tsv] | .[]' \
    "$tmp/rules.json" 2> /dev/null) || parse_fail "the rules API answer"
  if [ -n "$uid" ] && [ -n "$forsec" ] && [ "$(wc -l <<< "$got")" -eq 1 ] && [ "$got" = "$(printf 'true\tok\t%s\t%s' "$(cut -f3 <<< "$got")" "$forsec")" ]; then
    echo "PASS group netpol-canary from prometheus-netpol-canary-$uid.yaml: NetpolCanaryFailing health=ok state=$(cut -f3 <<< "$got") for=${forsec}s"
    echo "alert: PASS"
    return 0
  fi
  echo "FAIL group netpol-canary: want one NetpolCanaryFailing from prometheus-netpol-canary-${uid:-?}.yaml, health ok, for ${forsec:-?}s; got '${got:-nothing}' (uid-file health state for)"
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
