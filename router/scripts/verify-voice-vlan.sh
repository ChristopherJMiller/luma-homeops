#!/usr/bin/env bash
# Stage 1 acceptance test for the voice VLAN
# (docs/superpowers/specs/2026-10-02-telephony-lab-design.md §8).
#
# Puts a throwaway VLAN-20 interface in its own network namespace, so the
# host's networking is untouched, and checks what a phone on VLAN 20 sees:
# the DHCP lease and options, the PBX ports it may reach, and everything it
# must not. TCP probes tell "allowed" from "dropped" by whether anything
# answers within 3 s. A live host answers at once (connected, or refused
# when nothing listens yet); a dropped packet never answers. Every
# "blocked" probe first runs the same probe from the LAN, which must
# answer, so silence from VLAN 20 can only be the firewall. The UDP rules
# (SIP over UDP, RTP, TFTP) are proven in stage 2 by real phone traffic.
#
# Run as root with busybox on PATH:
#   nix shell nixpkgs#busybox --command sudo env "PATH=$PATH" \
#     router/scripts/verify-voice-vlan.sh enp3s0
set -euo pipefail

PARENT=${1:?usage: $0 <LAN interface, e.g. enp3s0>}
NS=voicetest
IF=vtest20
GW=192.168.20.1
PBX=192.168.0.240
failed=0

pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; failed=1; }
in_ns() { ip netns exec "$NS" "$@"; }

cleanup() {
  ip netns del "$NS" 2>/dev/null || true   # takes the VLAN interface with it
  ip link del "$IF" 2>/dev/null || true     # ...unless we died before moving it
}
trap cleanup EXIT
cleanup

ip netns add "$NS"
ip link add link "$PARENT" name "$IF" type vlan id 20
ip link set "$IF" netns "$NS"
in_ns ip link set lo up
in_ns ip link set "$IF" up

# --- DHCP --------------------------------------------------------------
# udhcpc hands the lease to its script as environment variables. This
# script only prints them, so nothing is configured behind our back.
hook=$(mktemp)
cat >"$hook" <<'EOF'
#!/bin/sh
[ "$1" = bound ] && echo "ip=$ip router=$router tftp=$tftp ntpsrv=$ntpsrv"
exit 0
EOF
chmod +x "$hook"
lease=$(in_ns busybox udhcpc -i "$IF" -f -q -n -t 5 -s "$hook" -O tftp -O ntpsrv 2>/dev/null | grep '^ip=' || true)
rm -f "$hook"

if [ -z "$lease" ]; then
  fail "no DHCP lease on VLAN 20"
  echo "      Is eth2.20 up on the router? If it is, a switch between this"
  echo "      host and the router is probably dropping 802.1Q-tagged frames."
  exit 1
fi

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
answers() {
  local rc=0
  if [ "$1" = ns ]; then
    in_ns timeout 3 bash -c "exec 3<>/dev/tcp/$2/$3" 2>/dev/null || rc=$?
  else
    timeout 3 bash -c "exec 3<>/dev/tcp/$2/$3" 2>/dev/null || rc=$?
  fi
  [ "$rc" -ne 124 ]
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
ntp_out=$(in_ns timeout 10 busybox ntpd -n -w -d -p "$GW" 2>&1 || true)
if grep -q 'reply from' <<<"$ntp_out"; then pass "NTP answers on $GW"
else fail "no NTP reply from $GW"; fi

# --- LAN → VOICE (Asterisk calls phones; admins open phone web UIs) -------
if ping -c1 -W2 "$addr" >/dev/null 2>&1; then pass "LAN reaches $addr on VLAN 20"
else fail "LAN cannot reach $addr on VLAN 20"; fi

if [ "$failed" -eq 0 ]; then echo "ALL PASS"; else echo "SOME CHECKS FAILED"; fi
exit "$failed"
