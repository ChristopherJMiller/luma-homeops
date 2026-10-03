#!/usr/bin/env bash
# Stage 1 acceptance test for the voice VLAN
# (docs/superpowers/specs/2026-10-02-telephony-lab-design.md §8).
#
# Run it as yourself, from anywhere in the repo:
#   router/scripts/verify-voice-vlan.sh enp3s0
# It fetches busybox and tcpdump from the repo's pinned nixpkgs, asks sudo
# once for the part that needs root, and reads the router's VLAN-20 receive
# counter over SSH before and after, so a failure comes with evidence of
# where the frames stopped.
#
# The root part puts a throwaway VLAN-20 interface in its own network
# namespace (the host's networking is untouched) and checks what a phone on
# VLAN 20 sees: the DHCP lease and options, the PBX ports it may reach, and
# everything it must not. TCP probes tell "allowed" from "dropped" by
# whether anything answers within 3 s. A live host answers at once
# (connected, or refused when nothing listens yet); a dropped packet never
# answers. Every "blocked" probe first runs the same probe from the LAN,
# which must answer, so silence from VLAN 20 can only be the firewall. The
# UDP rules (SIP over UDP, RTP, TFTP) are proven in stage 2 by real phone
# traffic.
#
# Only busybox's udhcpc and ntpd are used, always by full path. Never put
# busybox on PATH for this script: its ip has no netns, and its timeout
# exits 143 instead of 124.
set -euo pipefail

PARENT=${1:?usage: $0 <LAN interface, e.g. enp3s0>}
NS=voicetest
IF=vtest20
GW=192.168.20.1
PBX=192.168.0.240
ROUTER=chris@192.168.0.1

die() { echo "FAIL  $*" >&2; exit 2; }

# ===================== user part: tools, sudo, evidence =====================
if [ "${VOICE_TEST_ROOT:-}" != 1 ]; then
  repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
  pinned() { # attr → store path, from the repo's flake.lock
    nix build --no-link --print-out-paths --inputs-from "$repo" "nixpkgs#$1^out"
  }
  router_rx() {
    ssh -o BatchMode=yes -o ConnectTimeout=5 "$ROUTER" \
      cat /sys/class/net/eth2.20/statistics/rx_packets 2>/dev/null || echo "?"
  }

  echo "Fetching busybox and tcpdump (pinned nixpkgs)..."
  busybox=$(pinned busybox)/bin/busybox
  tcpdump=$(pinned tcpdump)/bin/tcpdump
  rx_before=$(router_rx)

  log=$(mktemp)
  trap 'rm -f "$log"' EXIT
  rc=0
  sudo env PATH="$PATH" VOICE_TEST_ROOT=1 BUSYBOX="$busybox" TCPDUMP="$tcpdump" \
    "$0" "$PARENT" | tee "$log" || rc=$?

  rx_after=$(router_rx)
  echo
  echo "Router eth2.20 received packets: before=$rx_before after=$rx_after"
  if grep -q '^FAIL  no DHCP lease' "$log"; then
    sent=$(sed -n 's/^INFO  tagged VLAN-20 frames sent: \([0-9]*\)$/\1/p' "$log")
    echo -n "Diagnosis: "
    if [ "${sent:-0}" -eq 0 ]; then
      echo "this host sent no tagged frames. Local problem; see the udhcpc lines above."
    elif [ "$rx_before" = "?" ] || [ "$rx_after" = "?" ]; then
      echo "frames left this host, but the router counter is unreadable (ssh-add?)."
    elif [ "$rx_after" -eq "$rx_before" ]; then
      echo "tagged frames left this host but never reached the router. Something on"
      echo "           the path (switch, dock, powerline, mesh) drops 802.1Q frames."
    else
      echo "tagged frames reach the router but no lease came back. Check Kea on the router."
    fi
  fi
  exit "$rc"
fi

# ========================== root part: the checks ===========================
[ -x "${BUSYBOX:-}" ] || die "busybox not found at '${BUSYBOX:-}'"
[ -x "${TCPDUMP:-}" ] || die "tcpdump not found at '${TCPDUMP:-}'"
ip netns list >/dev/null 2>&1 || die "'$(command -v ip)' has no netns support (need iproute2)"
[ "$(id -u)" = 0 ] || die "the checks need root (run without sudo; the script asks)"
[ -e "/sys/class/net/$PARENT" ] || die "no interface '$PARENT'"

failed=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; failed=1; }
in_ns() { ip netns exec "$NS" "$@"; }

cap=$(mktemp)
cap_pid=
# shellcheck disable=SC2329  # reached via cleanup, which runs from the EXIT trap
report_capture() {
  [ -n "$cap_pid" ] || return 0
  kill "$cap_pid" 2>/dev/null || true
  wait "$cap_pid" 2>/dev/null || true
  cap_pid=
  local mac total sent
  mac=$(cat "/sys/class/net/$PARENT/address")
  total=$(grep -c . "$cap" || true)
  sent=$(grep -ci " $mac > " "$cap" || true)
  echo "INFO  tagged VLAN-20 frames sent: $sent"
  echo "INFO  tagged VLAN-20 frames received: $((total - sent))"
}
# shellcheck disable=SC2329  # runs from the EXIT trap
cleanup() {
  report_capture
  rm -f "$cap"
  ip netns del "$NS" 2>/dev/null || true   # takes the VLAN interface with it
  ip link del "$IF" 2>/dev/null || true     # ...unless we died before moving it
}
ip netns del "$NS" 2>/dev/null || true
ip link del "$IF" 2>/dev/null || true
trap cleanup EXIT

# Watch the wire: every VLAN-20 frame this host sends or receives.
"$TCPDUMP" -i "$PARENT" -nn -e -l "vlan 20" >"$cap" 2>/dev/null &
cap_pid=$!
sleep 1   # let tcpdump attach before anything is sent

ip netns add "$NS"
ip link add link "$PARENT" name "$IF" type vlan id 20
ip link set "$IF" netns "$NS"
in_ns ip link set lo up
in_ns ip link set "$IF" up

# --- DHCP --------------------------------------------------------------
# udhcpc hands the lease to its script as environment variables. This
# script only prints them, so nothing is configured behind our back.
hook=$(mktemp)
dhcp_log=$(mktemp)
cat >"$hook" <<'EOF'
#!/bin/sh
[ "$1" = bound ] && echo "ip=$ip router=$router tftp=$tftp ntpsrv=$ntpsrv"
exit 0
EOF
chmod +x "$hook"
lease=$(in_ns "$BUSYBOX" udhcpc -i "$IF" -f -q -n -t 5 -s "$hook" -O tftp -O ntpsrv 2>"$dhcp_log" \
  | grep '^ip=' || true)
rm -f "$hook"

if [ -z "$lease" ]; then
  fail "no DHCP lease on VLAN 20"
  sed 's/^/      udhcpc: /' "$dhcp_log" | tail -6
  rm -f "$dhcp_log"
  exit 1
fi
rm -f "$dhcp_log"

field() { sed -n "s/.*\<$1=\([^ ]*\).*/\1/p" <<<"$lease"; }
expect_eq() { # what got want
  if [ "$2" = "$3" ]; then pass "$1 = $3"; else fail "$1 = '$2', want $3"; fi
}

addr=$(field ip)
if [[ $addr =~ ^192\.168\.20\.1[0-9][0-9]$ ]]; then
  pass "lease $addr (in .100-.199)"
else
  fail "lease '$addr' is outside 192.168.20.100-199"
fi
expect_eq "router" "$(field router)" "$GW"
expect_eq "option 66 (TFTP server)" "$(field tftp)" "$PBX"
expect_eq "NTP server" "$(field ntpsrv)" "$GW"

in_ns ip addr add "$addr/24" dev "$IF"
in_ns ip route add default via "$GW"

# --- Reachability -------------------------------------------------------
# answers <ns|host> <tcp|udp> <ip> <port> [payload]: true only if the target
# itself answers within 5 s. TCP: it connects or refuses. UDP: it replies,
# or its ICMP port-unreachable comes back as "Connection refused" on the
# connected socket. Everything else is silence: a timeout, and also "No
# route to host", which a dead LAN host produces when ARP gives up after
# about 3 s (counting that as an answer once let a dead target pass).
# Payloads are printf formats, sent so a live listener has something to
# answer: SIP OPTIONS, a TFTP read request, a DNS query. A UDP probe gets a
# second try 1.5 s later, because hosts rate-limit ICMP port-unreachable to
# about one per second per peer, and probes run back to back.
answers() {
  local where=$1 tries=1 err rc
  [ "$2" = udp ] && tries=2
  shift
  # shellcheck disable=SC2016  # expanded by the inner bash, from its arguments
  local probe='
    if [ "$1" = tcp ]; then exec 3<>"/dev/tcp/$2/$3"; exit; fi
    exec 3<>"/dev/udp/$2/$3"
    printf -- "$4" >&3
    read -r -t 4 -n 1 _ <&3'
  while :; do
    rc=0
    if [ "$where" = ns ]; then
      err=$(in_ns timeout 5 bash -c "$probe" probe "$1" "$2" "$3" "${4:-x}" 2>&1 >/dev/null) || rc=$?
    else
      err=$(timeout 5 bash -c "$probe" probe "$1" "$2" "$3" "${4:-x}" 2>&1 >/dev/null) || rc=$?
    fi
    if [ "$rc" -eq 0 ] || [[ $err == *"Connection refused"* ]]; then return 0; fi
    tries=$((tries - 1))
    [ "$tries" -gt 0 ] || return 1
    sleep 1.5
  done
}
reachable() { # proto ip port why [payload]
  if answers ns "$1" "$2" "$3" "${5:-}"; then pass "VLAN 20 reaches $2 $1/$3 ($4)"
  else fail "VLAN 20 cannot reach $2 $1/$3 ($4)"; fi
}
blocked() { # proto ip port why [payload]
  if ! answers host "$1" "$2" "$3" "${5:-}"; then
    fail "$2 $1/$3 ($4) silent even from the LAN; can't judge the firewall"
  elif answers ns "$1" "$2" "$3" "${5:-}"; then
    fail "VLAN 20 reached $2 $1/$3 ($4); must be dropped"
  else
    pass "VLAN 20 blocked from $2 $1/$3 ($4)"
  fi
}

sip_options="OPTIONS sip:probe@$PBX SIP/2.0\r\nVia: SIP/2.0/UDP 0.0.0.0:5060;rport;branch=z9hG4bKvoiceprobe\r\nMax-Forwards: 70\r\nFrom: <sip:probe@invalid>;tag=probe\r\nTo: <sip:probe@$PBX>\r\nCall-ID: voice-vlan-probe\r\nCSeq: 1 OPTIONS\r\nContent-Length: 0\r\n\r\n"
tftp_rrq='\0\001voice-vlan-probe\0octet\0'
dns_query='\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x01'

reachable tcp "$PBX" 5060 "SIP"
reachable udp "$PBX" 5060 "SIP" "$sip_options"
reachable udp "$PBX" 69 "TFTP" "$tftp_rrq"
reachable udp "$PBX" 10099 "RTP, top of the range"
reachable tcp "$PBX" 8088 "phone directory"
blocked tcp "$PBX" 50000 "Talos apid: PBX host but not a voice port"
blocked udp "$PBX" 9999 "PBX host, UDP outside the voice ports"
blocked tcp 192.168.0.7 443 "Traefik"
blocked tcp 192.168.0.58 80 "HDHomeRun"
blocked tcp 192.168.0.1 22 "router SSH, LAN address"
blocked tcp "$GW" 22 "router SSH, voice address"
blocked tcp "$GW" 53 "router DNS"
blocked udp 192.168.0.1 53 "router DNS, LAN address" "$dns_query"
blocked tcp 1.1.1.1 443 "internet"

# --- IPv6 ---------------------------------------------------------------
# The phones are IPv4-only and there is no IPv6 firewall, so the router must
# have no IPv6 presence on VLAN 20 at all. A link-local address there would
# expose everything listening on :: (sshd, the zigbee2mqtt frontend). Two
# checks: probe the EUI-64 link-local derived from its MAC, and see whether
# it answers an all-nodes ping under any address.
router_mac=$(in_ns ip neigh show "$GW" | awk '{print $5; exit}')
if [ -z "$router_mac" ]; then
  fail "IPv6: couldn't learn the router's MAC on VLAN 20"
else
  IFS=: read -r -a m <<<"$router_mac"
  ll=$(printf 'fe80::%02x%02x:%02xff:fe%02x:%02x%02x' \
    $((0x${m[0]} ^ 2)) $((0x${m[1]})) $((0x${m[2]})) $((0x${m[3]})) $((0x${m[4]})) $((0x${m[5]})))
  for port in 22 8585; do
    if answers ns tcp "$ll%$IF" "$port"; then
      fail "IPv6: router answers on [$ll]:$port over VLAN 20"
    else
      pass "IPv6: router silent on [$ll]:$port"
    fi
  done
  in_ns ping -6 -c 2 -w 3 -I "$IF" ff02::1 >/dev/null 2>&1 || true
  if in_ns ip -6 neigh show dev "$IF" | grep -qi "lladdr $router_mac"; then
    fail "IPv6: the router answers all-nodes pings on VLAN 20"
  else
    pass "IPv6: the router has no presence on VLAN 20"
  fi
fi

# --- NTP ----------------------------------------------------------------
ntp_out=$(in_ns timeout 10 "$BUSYBOX" ntpd -n -w -d -p "$GW" 2>&1 || true)
if grep -q 'reply from' <<<"$ntp_out"; then pass "NTP answers on $GW"
else fail "no NTP reply from $GW"; fi

# --- LAN → VOICE (Asterisk calls phones; admins open phone web UIs) -------
if ping -c1 -W2 "$addr" >/dev/null 2>&1; then pass "LAN reaches $addr on VLAN 20"
else fail "LAN cannot reach $addr on VLAN 20"; fi

if [ "$failed" -eq 0 ]; then echo "ALL PASS"; else echo "SOME CHECKS FAILED"; fi
exit "$failed"
