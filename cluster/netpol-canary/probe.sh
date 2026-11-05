#!/usr/bin/env bash
# NetworkPolicy canary probe (design §6.4, §9.2). The netpol-probe DaemonSet mounts it at /etc/netpol-probe.
#
#   probe.sh loop                       a run every $PROBE_INTERVAL s (default 300), start to start, >= 5 s apart
#   probe.sh once enforced|baseline     one pass, exit 0 only if every row passed
#
# Sourceable: it runs main only when executed. Env: MATRIX (default
# /etc/netpol-probe/matrix.txt), PROBE_SCOPE (canary|runner, default canary),
# PROBE_MODE (loop only, default enforced), PROBE_INTERVAL, HOST_IP, NODE_NAME,
# NODE_IPS (for $PEER_IP; default top, middle and bottom), AGNHOST (default /agnhost),
# PROBE_DEADLINE (how many seconds a tool may run before probe.sh kills it, default 10),
# READY_FILE (default /tmp/ready).
# Rows run sequentially: parallel agnhost processes would need ~20x the memory.

# probe_classify EXIT STDERR -> open|timeout|refused|other
probe_classify() {
  local code=$1 err=${2-}
  if [ "$code" -eq 0 ]; then echo open; return 0; fi
  case $err in
    *TIMEOUT*) echo timeout ;;
    *REFUSED*) echo refused ;;
    *) echo other ;;
  esac
}

# probe_judge EXPECT GOT MODE -> 0 pass, 1 fail
probe_judge() {
  case "$3:$1:$2" in
    enforced:open:open | enforced:dns:open | enforced:dropped:timeout) return 0 ;;
    baseline:open:open | baseline:dns:open | baseline:dropped:open | baseline:dropped:refused) return 0 ;;
  esac
  return 1
}

# probe_peer_ip HOST_IP -> the node after HOST_IP in NODE_IPS, wrapping, so each probe dials
# another node and every node gets dialled. Returns 1 unless HOST_IP is one of two or more nodes.
# The default must match the router's DHCP reservations (a static test checks it).
probe_peer_ip() {
  local host=$1 ips i
  read -r -a ips <<< "${NODE_IPS:-192.168.0.240 192.168.0.241 192.168.0.242}"
  [ "${#ips[@]}" -ge 2 ] || return 1
  for i in "${!ips[@]}"; do
    if [ "${ips[$i]}" = "$host" ]; then
      echo "${ips[$(((i + 1) % ${#ips[@]}))]}"
      return 0
    fi
  done
  return 1
}

# probe_rows MATRIX SCOPE -> expanded rows ("expect scope host port backend note...").
# SCOPE canary = all+canary rows, runner = all rows. Returns 2 if a selected row needs an unset variable.
probe_rows() {
  local matrix=$1 scope=$2 expect rscope host port backend note line
  local rc=0
  while read -r expect rscope host port backend note; do
    case $expect in '' | '#'*) continue ;; esac
    case $scope:$rscope in
      canary:all | canary:canary | runner:all) ;;
      *) continue ;;
    esac
    case $host in
      '$HOST_IP') [ -n "${HOST_IP-}" ] || { echo "probe_rows: HOST_IP unset (row $host:$port)" >&2; rc=2; continue; }; host=$HOST_IP ;;
      '$PEER_IP') host=$(probe_peer_ip "${HOST_IP-}") || { echo "probe_rows: no peer node for HOST_IP '${HOST_IP-}' (row \$PEER_IP:$port)" >&2; rc=2; continue; } ;;
      '$WAN_IP') [ -n "${WAN_IP-}" ] || { echo "probe_rows: WAN_IP unset (row $host:$port)" >&2; rc=2; continue; }; host=$WAN_IP ;;
    esac
    line="$expect $rscope $host $port $backend${note:+ $note}"
    echo "$line"
  done < "$matrix"
  return $rc
}

# probe_check EXPECT HOST PORT -> got. agnhost's --timeout and dig's +time already bound each
# tool. The outer `timeout` catches a hang: it kills the tool, and the row counts as other,
# never as a pass.
probe_check() {
  local expect=$1 host=$2 port=$3 out err code deadline=${PROBE_DEADLINE:-10}
  if [ "$expect" = dns ]; then
    out=$(timeout "$deadline" dig +short +time=3 +tries=1 "$host" 2> /dev/null | grep -v '^;' || true)
    if [ -n "$out" ]; then echo open; else echo other; fi
    return 0
  fi
  err=$(timeout "$deadline" "${AGNHOST:-/agnhost}" connect --timeout=3s "$host:$port" 2>&1 > /dev/null) && code=0 || code=$?
  # Killed at the deadline: GNU timeout exits 124; busybox's execs the tool, which dies of TERM or KILL.
  case $code in 124 | 137 | 143) echo other; return 0 ;; esac
  probe_classify "$code" "$err"
}

# probe_once MODE -> table + SUMMARY; 0 only if every row passed, 2 if the matrix could not be expanded
probe_once() {
  local mode=$1 rows expect rscope host port backend note got pass=0 total=0 rc=0 verdict
  rows=$(probe_rows "${MATRIX:-/etc/netpol-probe/matrix.txt}" "${PROBE_SCOPE:-canary}") || rc=$?
  if [ "$rc" -ne 0 ]; then echo "SUMMARY 0/0 $mode node=${NODE_NAME-?} matrix-error"; return 2; fi
  while read -r expect rscope host port backend note; do
    [ -n "$expect" ] || continue
    total=$((total + 1))
    # </dev/null: a tool that reads stdin would otherwise eat the remaining rows.
    got=$(probe_check "$expect" "$host" "$port" < /dev/null)
    if probe_judge "$expect" "$got" "$mode"; then verdict=PASS; pass=$((pass + 1)); else verdict=FAIL; fi
    echo "$verdict $expect $got $host:$port${note:+ $note}"
  done <<< "$rows"
  echo "SUMMARY $pass/$total $mode node=${NODE_NAME-?}"
  [ "$total" -gt 0 ] && [ "$pass" -eq "$total" ]
}

# probe_wan_ip -> git.realliance.net's A record, or nothing
probe_wan_ip() {
  timeout "${PROBE_DEADLINE:-10}" dig +short +time=3 +tries=1 git.realliance.net A 2> /dev/null \
    | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -n 1
}

# probe_run MODE -> resolve $WAN_IP, then probe_once MODE
probe_run() {
  WAN_IP=$(probe_wan_ip || true)
  export WAN_IP
  probe_once "$1"
}

# probe_sleep_for INTERVAL ELAPSED -> seconds to the next run: start to start, never under 5 (F9)
probe_sleep_for() {
  local d=$(($1 - $2))
  [ "$d" -ge 5 ] || d=5
  echo "$d"
}

# probe_wait CMD... runs CMD in the background and waits for it. In the pod bash is PID 1, and
# bash runs a trap only after the foreground command ends; `wait` returns at once on a signal, so
# a TERM stops the loop mid-run or mid-sleep (probe_stop).
probe_wait() {
  "$@" &
  PROBE_CHILD=$!
  wait "$PROBE_CHILD"
}

probe_stop() {
  [ -z "${PROBE_CHILD-}" ] || kill "$PROBE_CHILD" 2> /dev/null
  exit 0
}

probe_loop() {
  local interval=${PROBE_INTERVAL:-300} ready=${READY_FILE:-/tmp/ready} start table sig last="" rc
  # PID 1 ignores TERM unless it has a handler.
  trap probe_stop TERM INT
  while :; do
    start=$(date +%s)
    rc=0
    probe_wait probe_run "${PROBE_MODE:-enforced}" > "$ready.run" || rc=$?
    table=$(< "$ready.run")
    if [ "$rc" -eq 0 ]; then touch "$ready"; else rm -f "$ready"; fi
    # The pass/fail set, not the timing: print only when it changes.
    sig=$(grep -E '^(PASS|FAIL) ' <<< "$table" | awk '{print $1, $2, $4}' | sort | tr '\n' ';')
    sig="$rc;$sig"
    if [ "$sig" != "$last" ]; then echo "$(date -u +%FT%TZ) $table"; last=$sig; fi
    probe_wait sleep "$(probe_sleep_for "$interval" $(($(date +%s) - start)))"
  done
}

main() {
  set -uo pipefail
  case ${1-} in
    loop) probe_loop ;;
    once) case ${2-} in
        enforced | baseline) probe_run "$2" ;;
        *) echo "usage: $0 once enforced|baseline" >&2; return 2 ;;
      esac ;;
    *) echo "usage: $0 loop | once enforced|baseline" >&2; return 2 ;;
  esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
