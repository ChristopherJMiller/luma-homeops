#!/usr/bin/env bash
# Live checks for NetworkPolicy enforcement (kube-network-policies).
# Design: docs/superpowers/specs/2026-11-03-networkpolicy-enforcement-design.md §9.
#
#   scripts/netpol-verify.sh idle
#   scripts/netpol-verify.sh selftest
#   scripts/netpol-verify.sh baseline
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
#     "syncing nftables rules" line carrying an error.
# The queue metrics come from a collector that runs every 30 s and first runs
# before the queue is bound, so run this at least a minute after the pod is Ready.
#
# baseline -- B1a, before any canary policy (§6.4 "Baseline first"). The three
# netpol-probe pods each run `probe.sh once baseline`, in parallel, and every
# row the canary runs (N, counted from the repo's matrix.txt, never a literal,
# F8) must answer: open, or refused for a must-drop target. A target that does
# neither would pass as "timeout" once policy is in, so it FAILs here. A pod
# whose count differs (a stale ConfigMap) FAILs too. Exit 2 unless exactly 3
# Running probe pods sit on 3 distinct nodes. It skips a pod with a
# deletionTimestamp (a rollout's outgoing pod).
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
# One `probe.sh once` in a pod. A healthy run takes seconds; every row at its 10 s PROBE_DEADLINE
# would take about 270 s for 26 rows.
EXEC_TIMEOUT=300

usage() { echo "usage: $0 idle|selftest|baseline" >&2; exit 2; }
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

baseline() {
  local root probe matrix rows n live running nodes pod node rc summary npass nfail fail=0
  root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
  probe=$root/cluster/netpol-canary/probe.sh
  matrix=$root/cluster/netpol-canary/matrix.txt
  [ -f "$probe" ] && [ -f "$matrix" ] || die "missing $probe or $matrix"
  # shellcheck source=/dev/null
  . "$probe"
  # N, the rows each pod runs. The addresses only let the variables expand; the count is what matters.
  HOST_IP=192.168.0.240 WAN_IP=192.0.2.1 probe_rows "$matrix" canary > "$tmp/rows" || die "matrix.txt does not expand"
  rows=$(wc -l < "$tmp/rows")
  count_or_fail "the matrix row count" "$rows"
  [ "$rows" -gt 0 ] || die "matrix.txt has no canary rows"

  kc -n "$CANARY_NS" get pods -l "$PROBE_SELECTOR" -o json > "$tmp/probes.json" 2> /dev/null \
    || die "cannot list pods in $CANARY_NS; is KUBECONFIG right?"
  n=$(items_len "$tmp/probes.json"); count_or_fail "the probe pod count" "$n"
  # name node phase, for every pod without a deletionTimestamp
  jq -r '.items[] | select(.metadata.deletionTimestamp == null)
      | [.metadata.name, (.spec.nodeName // "-"), (.status.phase // "-")] | @tsv' "$tmp/probes.json" > "$tmp/probes.tsv" 2> /dev/null \
    || parse_fail "the probe pod list"
  live=$(wc -l < "$tmp/probes.tsv")
  [ "$live" -gt 0 ] || die "no netpol-probe pods in $CANARY_NS"
  running=$(awk -F'\t' '$3 == "Running"' "$tmp/probes.tsv" | wc -l)
  nodes=$(awk -F'\t' '$3 == "Running" { print $2 }' "$tmp/probes.tsv" | sort -u | wc -l)
  if [ "$live" -ne "$CANARY_NODES" ] || [ "$running" -ne "$CANARY_NODES" ] || [ "$nodes" -ne "$CANARY_NODES" ]; then
    sed 's/^/  /' "$tmp/probes.tsv" >&2
    die "need exactly $CANARY_NODES Running netpol-probe pods on $CANARY_NODES nodes (found $live, $running Running, on $nodes nodes)"
  fi

  # The three in parallel. </dev/null keeps kubectl off the loop's input.
  while IFS=$'\t' read -r pod node _; do
    (
      timeout "$EXEC_TIMEOUT" kubectl -n "$CANARY_NS" exec "$pod" -c probe -- bash /etc/netpol-probe/probe.sh once baseline \
        > "$tmp/$pod.out" 2> "$tmp/$pod.err" < /dev/null
      echo $? > "$tmp/$pod.rc"
    ) &
  done < "$tmp/probes.tsv"
  wait

  while IFS=$'\t' read -r pod node _; do
    rc=$(cat "$tmp/$pod.rc" 2> /dev/null)
    echo "== $node $pod (exit ${rc:-?})"
    cat "$tmp/$pod.out"
    [ ! -s "$tmp/$pod.err" ] || head -n 5 "$tmp/$pod.err" | sed 's/^/   stderr: /'
    summary=$(grep '^SUMMARY ' "$tmp/$pod.out" | tail -n 1)
    npass=$(grep -c '^PASS ' "$tmp/$pod.out")
    nfail=$(grep -c '^FAIL ' "$tmp/$pod.out")
    if [ "${rc:-}" = 0 ] && [ "$summary" = "SUMMARY $rows/$rows baseline node=$node" ] \
      && [ "$npass" -eq "$rows" ] && [ "$nfail" -eq 0 ]; then
      echo "PASS $node $rows/$rows rows answer"
    else
      echo "FAIL $node exit=${rc:-?} pass=$npass/$rows fail=$nfail summary='${summary:-none}'"
      fail=1
    fi
  done < "$tmp/probes.tsv"

  if [ "$fail" -eq 0 ]; then
    echo "baseline: PASS ($CANARY_NODES nodes × $rows rows answer)"
    return 0
  fi
  echo "baseline: FAIL ($CANARY_NODES nodes × $rows rows)"
  return 1
}

# deny_match LOGFILE SRC_POD DST -> 0 if some line of an agent's log is a deny verdict from
# SRC_POD (ns/name, anchored: ...-abcde never matches ...-abcdef) to DST, which is IP:PORT
# (port anchored) or IP: (any port). Regex metacharacters, the dots in an IP, are escaped.
deny_match() {
  local log=$1 src dst
  src=$(printf '%s' "$2" | sed 's/[][\.*^$(){}?+|\\]/\\&/g')
  dst=$(printf '%s' "$3" | sed 's/[][\.*^$(){}?+|\\]/\\&/g')
  case $3 in *: ) ;; *) dst+='("| |$)' ;; esac
  grep -F '"Packet verdict"' "$log" \
    | grep -E '(^| )verdict="?deny"?( |$)' \
    | grep -E "(^| )srcPod=\"?${src}\"?( |\$)" \
    | grep -Eq "(^| )dst=\"?${dst}"
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

[ "$#" -eq 1 ] || usage
CHECK=$1
case "$1" in
  idle) idle ;;
  selftest) selftest ;;
  baseline) baseline ;;
  *) usage ;;
esac
