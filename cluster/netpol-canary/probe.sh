#!/usr/bin/env bash
# NetworkPolicy canary probe (design §6.4, §9.2). Not rendered by anything yet.
#
#   probe.sh loop                       every $PROBE_INTERVAL s (default 300), start to start
#   probe.sh once enforced|baseline     one pass, exit 0 only if every row passed
#
# Sourceable: it runs main only when executed. Env: MATRIX (default
# /etc/netpol-probe/matrix.txt), PROBE_SCOPE (canary|runner, default canary),
# PROBE_MODE (loop only, default enforced), PROBE_INTERVAL, HOST_IP, NODE_NAME,
# AGNHOST (default /agnhost), READY_FILE (default /tmp/ready).
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
      '$WAN_IP') [ -n "${WAN_IP-}" ] || { echo "probe_rows: WAN_IP unset (row $host:$port)" >&2; rc=2; continue; }; host=$WAN_IP ;;
    esac
    line="$expect $rscope $host $port $backend${note:+ $note}"
    echo "$line"
  done < "$matrix"
  return $rc
}

# probe_check EXPECT HOST PORT -> got
probe_check() {
  local expect=$1 host=$2 port=$3 out err code
  if [ "$expect" = dns ]; then
    out=$(dig +short +time=3 +tries=1 "$host" 2> /dev/null | grep -v '^;' || true)
    if [ -n "$out" ]; then echo open; else echo other; fi
    return 0
  fi
  err=$("${AGNHOST:-/agnhost}" connect --timeout=3s "$host:$port" 2>&1 > /dev/null) && code=0 || code=$?
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
    got=$(probe_check "$expect" "$host" "$port")
    if probe_judge "$expect" "$got" "$mode"; then verdict=PASS; pass=$((pass + 1)); else verdict=FAIL; fi
    echo "$verdict $expect $got $host:$port${note:+ $note}"
  done <<< "$rows"
  echo "SUMMARY $pass/$total $mode node=${NODE_NAME-?}"
  [ "$total" -gt 0 ] && [ "$pass" -eq "$total" ]
}

# probe_wan_ip -> git.realliance.net's A record, or nothing
probe_wan_ip() {
  dig +short +time=3 +tries=1 git.realliance.net A 2> /dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -n 1
}

probe_loop() {
  local interval=${PROBE_INTERVAL:-300} ready=${READY_FILE:-/tmp/ready} start now table sig last="" rc
  while :; do
    start=$(date +%s)
    WAN_IP=$(probe_wan_ip || true); export WAN_IP
    rc=0; table=$(probe_once "${PROBE_MODE:-enforced}") || rc=$?
    if [ "$rc" -eq 0 ]; then touch "$ready"; else rm -f "$ready"; fi
    # The pass/fail set, not the timing: print only when it changes.
    sig=$(grep -E '^(PASS|FAIL) ' <<< "$table" | awk '{print $1, $2, $4}' | sort | tr '\n' ';')
    sig="$rc;$sig"
    if [ "$sig" != "$last" ]; then echo "$(date -u +%FT%TZ) $table"; last=$sig; fi
    now=$(date +%s)
    [ $((now - start)) -ge "$interval" ] || sleep $((interval - (now - start)))
  done
}

main() {
  set -uo pipefail
  case ${1-} in
    loop) probe_loop ;;
    once) case ${2-} in
        enforced | baseline)
          WAN_IP=$(probe_wan_ip || true); export WAN_IP
          probe_once "$2" ;;
        *) echo "usage: $0 once enforced|baseline" >&2; return 2 ;;
      esac ;;
    *) echo "usage: $0 loop | once enforced|baseline" >&2; return 2 ;;
  esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
