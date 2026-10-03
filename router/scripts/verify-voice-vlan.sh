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
# answers <ns|host> <ip> <port>: true if anything answers within 3 s.
# An expired timeout exits 124 (coreutils) or 143 (busybox); a connect
# exits 0 and a refusal 1.
answers() {
  local rc=0
  if [ "$1" = ns ]; then
    in_ns timeout 3 bash -c "exec 3<>/dev/tcp/$2/$3" 2>/dev/null || rc=$?
  else
    timeout 3 bash -c "exec 3<>/dev/tcp/$2/$3" 2>/dev/null || rc=$?
  fi
  [ "$rc" -lt 124 ]
}
reachable() { # ip port why
  if answers ns "$1" "$2"; then pass "VLAN 20 reaches $1:$2 ($3)"
  else fail "VLAN 20 cannot reach $1:$2 ($3)"; fi
}
blocked() { # ip port why
  if ! answers host "$1" "$2"; then
    fail "$1:$2 ($3) silent even from the LAN; can't judge the firewall"
  elif answers ns "$1" "$2"; then
    fail "VLAN 20 reached $1:$2 ($3); must be dropped"
  else
    pass "VLAN 20 blocked from $1:$2 ($3)"
  fi
}

reachable "$PBX" 5060 "SIP"
reachable "$PBX" 8088 "phone directory"
blocked "$PBX" 50000 "Talos apid: PBX host but not a voice port"
blocked 192.168.0.7 443 "Traefik"
blocked 192.168.0.58 80 "HDHomeRun"
blocked 192.168.0.1 22 "router SSH, LAN address"
blocked "$GW" 22 "router SSH, voice address"
blocked "$GW" 53 "router DNS"
blocked 1.1.1.1 443 "internet"

# --- NTP ----------------------------------------------------------------
ntp_out=$(in_ns timeout 10 "$BUSYBOX" ntpd -n -w -d -p "$GW" 2>&1 || true)
if grep -q 'reply from' <<<"$ntp_out"; then pass "NTP answers on $GW"
else fail "no NTP reply from $GW"; fi

# --- LAN → VOICE (Asterisk calls phones; admins open phone web UIs) -------
if ping -c1 -W2 "$addr" >/dev/null 2>&1; then pass "LAN reaches $addr on VLAN 20"
else fail "LAN cannot reach $addr on VLAN 20"; fi

if [ "$failed" -eq 0 ]; then echo "ALL PASS"; else echo "SOME CHECKS FAILED"; fi
exit "$failed"
