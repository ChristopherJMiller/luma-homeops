# Telephony Stage 1 — Voice VLAN Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the VyOS router a voice VLAN (20, `192.168.20.0/24`). Phones
on it get DHCP pointing at the PBX for TFTP and at the router for NTP, and
can reach only the PBX's voice ports.

**Architecture:** VLAN 20 is a tagged sub-interface of the LAN port
`eth2`. Ansible adds the interface, a second Kea DHCP shared network, and
a `VOICE-OUT` firewall chain that VOICE-originated forward traffic jumps
into. Every high-risk router change is guarded by a single site-level
commit-confirm "parachute", lifted out of `nat.yml` and rehearsed once
before the real change. A root-run script then proves the result from a
throwaway network namespace on the dev machine, before any phone moves.

**Tech Stack:** VyOS 1.5-rolling-202409240007 (Kea DHCP, nftables
firewall), Ansible with `vyos.vyos` + `ansible.netcommon`, bash, and
busybox (`udhcpc`, `ntpd`) for the checks.

**Spec:** `docs/superpowers/specs/2026-10-02-telephony-lab-design.md`, §4 and §8 (Stage 1).

## Global Constraints

- Every router change goes through `router/ansible/` only. **Never type
  `set` on the router.** (CLAUDE.md S8, sharp edge 2, vyos-deploy skill.)
- Every run with tags `network`, `firewall`, `nat` or `services` is
  covered by the commit-confirm parachute: arm → apply → verify →
  confirm → save.
- Always run `--check --diff` before applying. **Stop if the diff shows
  any line this plan doesn't list.**
- Before every real apply, get Chris's explicit go-ahead (CLAUDE.md S4:
  a VyOS commit touching firewall, NAT or interfaces).
- Voice values, verbatim from the spec: VLAN `20` on `eth2`; router
  `192.168.20.1/24`; DHCP range `192.168.20.100`–`.199`, `subnet-id 2`,
  lease `86400`, option 66 `tftp-server-name` = `192.168.0.240`,
  `ntp-server` = `192.168.20.1`; VOICE → PBX `192.168.0.240` allowed only on
  `5060` tcp+udp, `10000-10099` udp, `69` udp, `8088` tcp; everything else
  from VOICE dropped; WAN → VOICE dropped; NTP (udp 123) from VOICE to the
  router accepted.
- Run Ansible from the pinned toolchain:
  `nix develop /home/chris/Repos/luma-homeops --command bash -c 'cd router/ansible && …'`.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
  and are made inside the toolchain so pre-commit runs (S6). Never use
  `--no-verify`.
- The phones are **not touched** in Stage 1.

## Review Focus

1. **A switch between the test host and the router drops 802.1Q-tagged
   frames.** Expect a clear "no lease, probably the switch" failure, not
   a hang. Pinned by Task 2's DHCP-failure message.
2. **A malformed VOICE rule breaks forwarding for the whole LAN.** Expect
   the parachute's verify step to fail and the router to reboot to its
   saved config. Pinned by Task 1 (the rehearsal proves verify → confirm)
   and Task 3 (verify runs on the real change).
3. **A VOICE host reaches router management (SSH 22, DNS 53 on either
   router address).** Expect drops. Pinned by Task 2's `blocked` checks on
   `192.168.0.1:22`, `192.168.20.1:22` and `192.168.20.1:53`.
4. **A VOICE host reaches a non-voice port on the PBX node.** Talos `apid`
   on `:50000` is open. Expect a drop: the rules are scoped by port, not
   just by host. Pinned by Task 2's `blocked 192.168.0.240 50000`.
5. **A "blocked" result that is really a dead target.** Expect every
   blocked probe to be preceded by a LAN-side control probe that must
   answer, so silence from VOICE can only mean the firewall. Pinned by
   Task 2's `blocked()` helper.

---

## File Structure

| File | Responsibility |
|---|---|
| `router/ansible/playbooks/commit-confirm-arm.yml` (new) | Arms the VyOS commit-confirm timer. Nothing else. |
| `router/ansible/playbooks/commit-confirm-verify.yml` (new) | Checks from the control node that the LAN still reaches the WAN and the port-forwards still work, then cancels the timer. |
| `router/ansible/site.yml` | Includes the arm before and the verify after the high-risk includes. |
| `router/ansible/playbooks/nat.yml` | Loses its private parachute; keeps only NAT rules. |
| `router/ansible/group_vars/vyos_routers.yml` | Renames the parachute vars; adds the `voice:` block. |
| `router/ansible/playbooks/network.yml` | Adds the `eth2 vif 20` task. |
| `router/ansible/playbooks/firewall.yml` | Adds the rule-100 description (rehearsal) and the voice firewall task. |
| `router/ansible/playbooks/services.yml` | Adds the VOICE DHCP task. |
| `router/scripts/verify-voice-vlan.sh` (new) | The Stage 1 acceptance test, run from the dev machine as root. |
| `CLAUDE.md`, `router/README.md`, `.claude/skills/vyos-deploy/SKILL.md` | Point at the site-level parachute; record the voice VLAN. |

---

### Task 1: Site-level commit-confirm parachute, plus a rehearsal

**Files:**
- Create: `router/ansible/playbooks/commit-confirm-arm.yml`
- Create: `router/ansible/playbooks/commit-confirm-verify.yml`
- Modify: `router/ansible/site.yml` (the `tasks:` list)
- Modify: `router/ansible/playbooks/nat.yml` (remove lines 1–19 and 75–105, the arm/verify/confirm tasks)
- Modify: `router/ansible/group_vars/vyos_routers.yml:175-183` (rename vars)
- Modify: `router/ansible/playbooks/firewall.yml` (the rule-100 description line)

**Interfaces:**
- Produces: group vars `commit_confirm_minutes` (int) and
  `commit_confirm_verify_urls` (list of URL). Any `site.yml` run tagged
  `network`, `firewall`, `nat` or `services` is parachuted. Tasks 3+
  rely on this.

- [ ] **Step 1: Write `commit-confirm-arm.yml`**

```yaml
---
# Commit-confirm parachute, part 1 of 2 (part 2: commit-confirm-verify.yml).
#
# Arms VyOS's commit-confirm timer (`config-mgmt commit_confirm`): unless it
# is cancelled, the router reboots to its *saved* config after N minutes.
# site.yml includes this before, and part 2 after, every high-risk include
# (network, firewall, nat, services). Each vyos_config task commits as it
# goes (save: false), so one timer covers them all; site.yml saves only
# after part 2 has verified and cancelled it. vyos_config itself has no
# commit-confirm support, and an interactive `commit-confirm` in another
# session can't cover Ansible's commits (VyOS refuses: "No configuration
# changes to commit").
  - name: Arm commit-confirm rollback timer ({{ commit_confirm_minutes }} min)
    vyos.vyos.vyos_command:
      commands:
      # -y: skip the interactive "Proceed ?" prompt (hangs a non-tty session).
        - sudo sg vyattacfg 'config-mgmt commit_confirm -y -t={{ commit_confirm_minutes }}'
    when: not ansible_check_mode
    changed_when: true
    tags: [network, firewall, nat, services]
```

- [ ] **Step 2: Write `commit-confirm-verify.yml`**

```yaml
---
# Commit-confirm parachute, part 2 of 2 (part 1: commit-confirm-arm.yml).
#
# Post-apply check, run from the control node (on the LAN). Each URL in
# commit_confirm_verify_urls must return a status < 500. The apex
# hostnames are Cloudflare-proxied, so a request leaves via the WAN and
# comes back in through the port-forwards. A pass proves LAN→WAN
# forwarding, WAN→Traefik NAT and that we still manage the router (we
# are talking to it). A DNS-only name would take NAT reflection and
# prove nothing.
  - name: Verify LAN egress and port-forwards (via Cloudflare)
    ansible.builtin.uri:
      url: '{{ item }}'
      method: GET
      status_code: [200, 301, 302, 401, 403, 404]
      timeout: 15
    loop: '{{ commit_confirm_verify_urls }}'
    delegate_to: localhost
    when: not ansible_check_mode
    register: commit_confirm_verify
    retries: 3
    delay: 5
    until: commit_confirm_verify is succeeded
    tags: [network, firewall, nat, services]

# `config-mgmt confirm` stops the reboot timer, then tries to finalise a
# commit-log entry that only the interactive CLI writes (it exports
# IN_COMMIT_CONFIRM around its commit; vyos_config's commits are logged
# normally at commit time instead). That second step throws a harmless
# traceback, so stop the timer directly and assert it is gone.
  - name: Confirm commit (cancel rollback timer)
    vyos.vyos.vyos_command:
      commands:
        - sudo systemctl stop --quiet commit-confirm.timer
        - sudo pkill -f commit-confirm-notify.py || true
        - systemctl is-active commit-confirm.timer || true
    register: commit_confirm_result
    when: not ansible_check_mode
    changed_when: true
    failed_when: "'inactive' not in commit_confirm_result.stdout[-1]"
    tags: [network, firewall, nat, services]
```

- [ ] **Step 3: Wire both into `site.yml`**

In `router/ansible/site.yml`, replace the `tasks:` list (from
`- name: Include system configuration` through the `Include monitoring
configuration` entry) with:

```yaml
    tasks:
      # Parachute (see playbooks/commit-confirm-arm.yml). Tags select it for
      # every high-risk run; system and monitoring runs skip it.
      - name: Arm commit-confirm parachute
        include_tasks: playbooks/commit-confirm-arm.yml
        tags: [network, firewall, nat, services]

      - name: Include system configuration
        include_tasks: playbooks/system.yml
        tags: [system]

      - name: Include network configuration
        include_tasks: playbooks/network.yml
        tags: [network]

      - name: Include firewall configuration
        include_tasks: playbooks/firewall.yml
        tags: [firewall]

      - name: Include NAT configuration
        include_tasks: playbooks/nat.yml
        tags: [nat]

      - name: Include services configuration
        include_tasks: playbooks/services.yml
        tags: [services]

      - name: Verify and confirm (cancels the parachute)
        include_tasks: playbooks/commit-confirm-verify.yml
        tags: [network, firewall, nat, services]

      - name: Include monitoring configuration
        include_tasks: playbooks/monitoring.yml
        tags: [monitoring]
```

Leave the `Commit and save all changes` task, `pre_tasks` and
`post_tasks` unchanged.

- [ ] **Step 4: Strip the private parachute from `nat.yml`**

Delete the header comment and the `Arm commit-confirm rollback timer`
task (the top of the file down to the line before `- name: Configure
destination NAT rules`). Also delete everything from the `# Post-apply
check, run from the control node.` comment to the end of the file. Put
this header in their place:

```yaml
---
# VyOS NAT Configuration Tasks
#
# High-risk: covered by the site-level commit-confirm parachute
# (playbooks/commit-confirm-arm.yml / commit-confirm-verify.yml), which
# site.yml wraps around every network/firewall/nat/services run.
```

- [ ] **Step 5: Rename the parachute vars in group_vars**

Replace the block at the end of `router/ansible/group_vars/vyos_routers.yml`:

```yaml
# playbooks/nat.yml: commit-confirm window and the outside-in check that
# must pass before the commit is confirmed. The apex is
# Cloudflare-proxied, so the request comes back in through eth0 (a
# DNS-only name would take NAT reflection and prove nothing).
nat_commit_confirm_minutes: 10
nat_verify_urls:
  - https://chrismiller.xyz/
```

with:

```yaml
# Commit-confirm parachute (playbooks/commit-confirm-*.yml): the rollback
# window, and the outside-in check that must pass before it is cancelled.
# The apex is Cloudflare-proxied, so the request comes back in through
# eth0 (a DNS-only name would take NAT reflection and prove nothing).
commit_confirm_minutes: 10
commit_confirm_verify_urls:
  - https://chrismiller.xyz/
```

- [ ] **Step 6: Add the rehearsal change**

The rehearsal is a description on the existing WAN→inside forward rule.
It's harmless and makes a real diff under the `firewall` tag. In
`router/ansible/playbooks/firewall.yml`, under `# Forward filter rules`,
insert after `- set firewall ipv4 forward filter rule 100 action 'jump'`:

```yaml
        - set firewall ipv4 forward filter rule 100 description 'WAN to inside, via OUTSIDE-IN'
```

- [ ] **Step 7: Check that no old var names remain and the syntax is valid**

Run:
```bash
cd /home/chris/Repos/luma-homeops
grep -rn 'nat_commit_confirm_minutes\|nat_verify_urls' router/ansible --include=*.yml
nix develop . --command bash -c 'cd router/ansible && ansible-playbook site.yml --syntax-check'
```
Expected: the grep prints nothing; syntax-check prints `playbook: site.yml`.

- [ ] **Step 8: Preview**

Run:
```bash
nix develop . --command bash -c 'cd router/ansible && ansible-playbook site.yml --check --diff --tags firewall'
```
Expected: the only changed line is
`set firewall ipv4 forward filter rule 100 description 'WAN to inside, via OUTSIDE-IN'`.
The arm, verify and confirm tasks show as skipped (check mode). **Any
other changed line means drift: stop and report it.**

- [ ] **Step 9: Commit**

```bash
git add router/ansible/site.yml router/ansible/playbooks/commit-confirm-arm.yml \
  router/ansible/playbooks/commit-confirm-verify.yml router/ansible/playbooks/nat.yml \
  router/ansible/group_vars/vyos_routers.yml router/ansible/playbooks/firewall.yml
nix develop . --command git commit -m "router: one site-level commit-confirm parachute

The arm/verify/confirm chain lived only in nat.yml, so network, firewall
and services runs - the voice VLAN needs all three - had no rollback
timer. site.yml now wraps it around every high-risk include. Rehearsal
change: a description on forward rule 100.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 10: GATE: Chris approves the rehearsal apply**

Show Chris the Step 8 diff. Do not continue without an explicit yes.

- [ ] **Step 11: Apply the rehearsal**

Run:
```bash
nix develop . --command bash -c 'cd router/ansible && ansible-playbook site.yml --tags firewall'
```
Expected, in order: `Arm commit-confirm rollback timer (10 min)` changed
→ firewall tasks (one changed) → `Verify LAN egress and port-forwards`
ok → `Confirm commit (cancel rollback timer)` changed → `Commit and save
all changes` → `PLAY RECAP` with `failed=0`. If any task fails, **do
nothing**: the router reboots to its saved config within 10 min. Watch
the timer with Monitor and report.

- [ ] **Step 12: Verify on the router**

Run:
```bash
ssh chris@192.168.0.1 'systemctl is-active commit-confirm.timer; /opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands | grep "forward filter rule 100 description"'
ls -t router/ansible/backups/ | head -1
```
Expected: `inactive`; the description line is present; the newest backup
file is timestamped from this run.

---

### Task 2: The Stage 1 acceptance test (written first, fails first)

**Files:**
- Create: `router/scripts/verify-voice-vlan.sh` (mode 755)

**Interfaces:**
- Consumes: nothing from earlier tasks. It tests the live router.
- Produces: `router/scripts/verify-voice-vlan.sh <lan-iface>`, which
  prints `PASS`/`FAIL` lines and exits 0 only if everything passes.
  Task 3 and Stage 2 re-run it.

- [ ] **Step 1: Write the script**

```bash
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
```

- [ ] **Step 2: Lint it**

Run:
```bash
chmod 755 router/scripts/verify-voice-vlan.sh
nix shell nixpkgs#shellcheck --command shellcheck router/scripts/verify-voice-vlan.sh
```
Expected: no output (clean).

- [ ] **Step 3: Chris runs it and it must fail**

VLAN 20 doesn't exist yet. Ask Chris to run this in his own terminal (sudo will
want a password; `!` in the Claude prompt works only if sudo doesn't), then
paste the output:
```
router/scripts/verify-voice-vlan.sh enp3s0
```
Expected: `FAIL  no DHCP lease on VLAN 20` and exit 1. The namespace is
cleaned up afterwards (`ip netns list` shows no `voicetest`).

- [ ] **Step 4: Commit**

```bash
git add router/scripts/verify-voice-vlan.sh
nix develop . --command git commit -m "router: acceptance test for the voice VLAN

Runs a VLAN-20 interface in a throwaway netns and checks the lease
options, the PBX ports a phone may reach, and that it cannot reach
router management, other LAN hosts, other PBX-node ports or the
internet. Each 'blocked' probe is paired with a LAN-side control.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Voice VLAN on the router

**Files:**
- Modify: `router/ansible/group_vars/vyos_routers.yml` (new `voice:` block after `dhcp:`)
- Modify: `router/ansible/playbooks/network.yml` (new task at the end)
- Modify: `router/ansible/playbooks/firewall.yml` (new task at the end)
- Modify: `router/ansible/playbooks/services.yml` (new task after `Configure DHCP static mappings`)

**Interfaces:**
- Consumes: the Task 1 parachute (tags `network`, `firewall`,
  `services`) and the Task 2 script.
- Produces: the router state Stage 2 depends on. Option 66 →
  `192.168.0.240`, and VOICE → PBX on `5060`/`10000-10099`/`69`/`8088`.

- [ ] **Step 1: Add the `voice:` block to group_vars**

Insert after the `dhcp:` block (before `# Firewall Groups`):

```yaml
# Voice VLAN for the telephony lab
# (docs/superpowers/specs/2026-10-02-telephony-lab-design.md §4).
# A tagged sub-interface of the LAN port. Phones tag their own frames
# (Admin VLAN ID 20) until a managed switch puts them on access ports, a
# move that needs no change here. The router routes VOICE<->LAN without
# NAT; firewall.yml lets VOICE reach only the PBX.
voice:
  parent_interface: eth2
  vlan_id: 20
  description: VOICE
  address: 192.168.20.1/24
  gateway: 192.168.20.1
  network: 192.168.20.0/24
  pbx_address: 192.168.0.240    # Asterisk: hostNetwork pod pinned to node `top`
  dhcp:
    shared_network: VOICE
    subnet_id: 2                # LAN is 1
    lease_time: 86400
    range_start: 192.168.20.100
    range_stop: 192.168.20.199
```

- [ ] **Step 2: Add the sub-interface task to `network.yml`**

Append:

```yaml
  - name: Configure voice VLAN sub-interface
    vyos.vyos.vyos_config:
      lines:
        - "set interfaces ethernet {{ voice.parent_interface }} vif {{ voice.vlan_id }} address '{{ voice.address }}'"
        - "set interfaces ethernet {{ voice.parent_interface }} vif {{ voice.vlan_id }} description '{{ voice.description }}'"
      save: false
    tags: [network, voice]
```

- [ ] **Step 3: Add the voice firewall task to `firewall.yml`**

Append:

```yaml
  - name: Configure voice VLAN firewall (VOICE may reach only the PBX)
    vyos.vyos.vyos_config:
      lines:
      # Groups
        - "set firewall group interface-group VOICE interface '{{ voice.parent_interface }}.{{ voice.vlan_id }}'"
        - "set firewall group network-group NET-VOICE-v4 network '{{ voice.network }}'"
        - "set firewall group address-group PBX-v4 address '{{ voice.pbx_address }}'"
      # VOICE-OUT: everything a phone may start. The default drop covers the
      # rest of the LAN and the WAN (the voice subnet has no source NAT
      # either). Replies to flows the LAN starts (Asterisk INVITEs, RTP,
      # TFTP data from ephemeral ports) pass on the global
      # established/related policy.
        - set firewall ipv4 name VOICE-OUT default-action 'drop'
        - set firewall ipv4 name VOICE-OUT rule 10 action 'accept'
        - set firewall ipv4 name VOICE-OUT rule 10 description 'SIP'
        - set firewall ipv4 name VOICE-OUT rule 10 destination group address-group 'PBX-v4'
        - set firewall ipv4 name VOICE-OUT rule 10 destination port '5060'
        - set firewall ipv4 name VOICE-OUT rule 10 protocol 'tcp_udp'
        - set firewall ipv4 name VOICE-OUT rule 20 action 'accept'
        - set firewall ipv4 name VOICE-OUT rule 20 description 'RTP'
        - set firewall ipv4 name VOICE-OUT rule 20 destination group address-group 'PBX-v4'
        - set firewall ipv4 name VOICE-OUT rule 20 destination port '10000-10099'
        - set firewall ipv4 name VOICE-OUT rule 20 protocol 'udp'
        - set firewall ipv4 name VOICE-OUT rule 30 action 'accept'
        - set firewall ipv4 name VOICE-OUT rule 30 description 'TFTP provisioning'
        - set firewall ipv4 name VOICE-OUT rule 30 destination group address-group 'PBX-v4'
        - set firewall ipv4 name VOICE-OUT rule 30 destination port '69'
        - set firewall ipv4 name VOICE-OUT rule 30 protocol 'udp'
        - set firewall ipv4 name VOICE-OUT rule 40 action 'accept'
        - set firewall ipv4 name VOICE-OUT rule 40 description 'Phone directory (HTTP)'
        - set firewall ipv4 name VOICE-OUT rule 40 destination group address-group 'PBX-v4'
        - set firewall ipv4 name VOICE-OUT rule 40 destination port '8088'
        - set firewall ipv4 name VOICE-OUT rule 40 protocol 'tcp'
      # Forward hooks. There is no forward default-deny, so WAN→VOICE is
      # dropped explicitly.
        - set firewall ipv4 forward filter rule 110 action 'drop'
        - set firewall ipv4 forward filter rule 110 description 'WAN to voice VLAN'
        - set firewall ipv4 forward filter rule 110 destination group network-group 'NET-VOICE-v4'
        - set firewall ipv4 forward filter rule 110 inbound-interface group 'WAN'
        - set firewall ipv4 forward filter rule 200 action 'jump'
        - set firewall ipv4 forward filter rule 200 description 'Voice VLAN egress'
        - set firewall ipv4 forward filter rule 200 inbound-interface group 'VOICE'
        - set firewall ipv4 forward filter rule 200 jump-target 'VOICE-OUT'
      # Input: phones may ask the router for the time. DHCP needs no rule:
      # Kea listens on raw sockets, which see packets before this filter.
        - set firewall ipv4 input filter rule 70 action 'accept'
        - set firewall ipv4 input filter rule 70 description 'NTP from voice VLAN'
        - set firewall ipv4 input filter rule 70 destination port '123'
        - set firewall ipv4 input filter rule 70 protocol 'udp'
        - set firewall ipv4 input filter rule 70 source group network-group 'NET-VOICE-v4'
      save: false
    tags: [firewall, voice]
```

- [ ] **Step 4: Add the VOICE DHCP task to `services.yml`**

Insert after the `Configure DHCP static mappings` task:

```yaml
  # Voice VLAN. Cisco phones look for option 150, then fall back to 66
  # (tftp-server-name). This VyOS build has no plain option 150, so 66 it is.
  - name: Configure DHCP server for the voice VLAN
    vyos.vyos.vyos_config:
      lines:
        - "set service dhcp-server shared-network-name {{ voice.dhcp.shared_network }} subnet {{ voice.network }} lease '{{ voice.dhcp.lease_time }}'"
        - "set service dhcp-server shared-network-name {{ voice.dhcp.shared_network }} subnet {{ voice.network }} option default-router '{{ voice.gateway }}'"
        - "set service dhcp-server shared-network-name {{ voice.dhcp.shared_network }} subnet {{ voice.network }} option ntp-server '{{ voice.gateway }}'"
        - "set service dhcp-server shared-network-name {{ voice.dhcp.shared_network }} subnet {{ voice.network }} option tftp-server-name '{{ voice.pbx_address }}'"
        - "set service dhcp-server shared-network-name {{ voice.dhcp.shared_network }} subnet {{ voice.network }} range 0 start '{{ voice.dhcp.range_start }}'"
        - "set service dhcp-server shared-network-name {{ voice.dhcp.shared_network }} subnet {{ voice.network }} range 0 stop '{{ voice.dhcp.range_stop }}'"
        - "set service dhcp-server shared-network-name {{ voice.dhcp.shared_network }} subnet {{ voice.network }} subnet-id '{{ voice.dhcp.subnet_id }}'"
      save: false
    tags: [services, dhcp, voice]
```

- [ ] **Step 5: Syntax check**

Run:
```bash
nix develop . --command bash -c 'cd router/ansible && ansible-playbook site.yml --syntax-check'
```
Expected: `playbook: site.yml`.

- [ ] **Step 6: Preview**

Run:
```bash
nix develop . --command bash -c 'cd router/ansible && ansible-playbook site.yml --check --diff --tags network,firewall,services'
```
Expected changed lines: the 2 `interfaces ethernet eth2 vif 20` lines,
the 3 group lines, the 21 `VOICE-OUT` lines, the 8 forward rule
110/200 lines, the 5 input rule 70 lines and the 7 VOICE DHCP lines.
**Nothing else.** In particular, nothing under `shared-network-name
LAN`, the zigbee2mqtt container or existing rules. Any other line means
drift: stop and report it.

- [ ] **Step 7: Commit**

```bash
git add router/ansible/group_vars/vyos_routers.yml router/ansible/playbooks/network.yml \
  router/ansible/playbooks/firewall.yml router/ansible/playbooks/services.yml
nix develop . --command git commit -m "router: voice VLAN 20 for the telephony lab

eth2 vif 20 (192.168.20.0/24) with its own Kea pool: option 66 points
phones at the PBX on top (.240), NTP at the router. VOICE-originated
traffic jumps to VOICE-OUT, which accepts only SIP/RTP/TFTP/directory to
the PBX and drops the rest; WAN to VOICE is dropped; NTP to the router
is allowed. Spec: docs/superpowers/specs/2026-10-02-telephony-lab-design.md

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 8: GATE: Chris approves the apply**

Show Chris the Step 6 diff and say that a failure reboots the router to
its saved config (LAN down for about a minute). Do not continue without
an explicit yes.

- [ ] **Step 9: Apply**

Run:
```bash
nix develop . --command bash -c 'cd router/ansible && ansible-playbook site.yml --tags network,firewall,services'
```
Expected: arm changed → network, firewall and services tasks (the voice
ones changed) → verify ok → confirm changed → save → `failed=0`. If any
task fails, **do nothing**: wait out the timer with Monitor and report.

- [ ] **Step 10: Verify on the router**

Run:
```bash
ssh chris@192.168.0.1 'systemctl is-active commit-confirm.timer
/opt/vyatta/bin/vyatta-op-cmd-wrapper show interfaces | grep -E "eth2(\.20)? "
/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands | grep -cE "VOICE|NET-VOICE|PBX-v4|eth2 vif 20"'
ls -t router/ansible/backups/ | head -1
```
Expected: `inactive`; `eth2.20` with `192.168.20.1/24`, state `u/u`; the
count is ≥ 30; a new backup file from this run.

- [ ] **Step 11: Chris re-runs the acceptance test (in his terminal); it must pass**

```
router/scripts/verify-voice-vlan.sh enp3s0
```
Expected: every line `PASS`, ending `ALL PASS`, exit 0. If DHCP fails
while `eth2.20` is `u/u`, the switch path is dropping tagged frames
(Review Focus 1). Report it; don't change the router.

---

### Task 4: Documentation

**Files:**
- Modify: `CLAUDE.md:18` (WAN row) and the topology table (new Voice VLAN row after `LAN-internal LB`)
- Modify: `router/README.md:54-68` ("Safe Deployment with Rollback Timer")
- Modify: `.claude/skills/vyos-deploy/SKILL.md:52-54`

**Interfaces:** none (docs only).

- [ ] **Step 1: CLAUDE.md**

In the WAN row, replace
`NAT rules are Ansible-managed with a built-in commit-confirm parachute (\`router/ansible/playbooks/nat.yml\`).`
with
`NAT rules are Ansible-managed. Every network/firewall/nat/services run is wrapped in a commit-confirm parachute (\`router/ansible/playbooks/commit-confirm-{arm,verify}.yml\`, included by \`site.yml\`).`

After the `LAN-internal LB` row, add:

```markdown
| Voice VLAN | `eth2` vif 20, `192.168.20.0/24`, gw `.1` | Telephony lab phones. Reach only the PBX at `.240` (SIP 5060, RTP 10000–10099, TFTP 69, HTTP 8088) plus router NTP; no WAN, no other LAN hosts. DHCP option 66 → `.240`. Phones self-tag (Admin VLAN ID) until a managed switch exists. Test: `router/scripts/verify-voice-vlan.sh`. Spec: `docs/superpowers/specs/2026-10-02-telephony-lab-design.md`. |
```

- [ ] **Step 2: router/README.md**

Replace the whole `### Safe Deployment with Rollback Timer` section,
including its code block, with:

````markdown
### Safe Deployment with Rollback Timer

Built in. `site.yml` arms VyOS's commit-confirm timer before any
`network`, `firewall`, `nat` or `services` include
(`playbooks/commit-confirm-arm.yml`). It checks from this machine that
the LAN still reaches the WAN and the port-forwards still answer
(`commit_confirm_verify_urls`), then cancels the timer
(`playbooks/commit-confirm-verify.yml`) and saves. If anything fails
first, the router reboots to its last saved config after
`commit_confirm_minutes`. Don't race it.

Don't open `commit-confirm` in a separate SSH session: it can't cover
Ansible's commits (VyOS answers "No configuration changes to commit").
````

- [ ] **Step 3: vyos-deploy skill**

Replace the paragraph at `.claude/skills/vyos-deploy/SKILL.md:52-54`
(`` `playbooks/nat.yml` is the reference implementation; copy the pattern into
any other high-risk playbook (`network.yml`, `firewall.yml`) before you
first need it there: ``) with:

```markdown
The parachute is site-level: `site.yml` includes
`playbooks/commit-confirm-arm.yml` before and
`playbooks/commit-confirm-verify.yml` after the high-risk includes,
tagged `network`, `firewall`, `nat` and `services`, so any run with one of
those tags is covered (vars: `commit_confirm_minutes`,
`commit_confirm_verify_urls`). Rehearsed 2026-10-02. The chain:
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md router/README.md .claude/skills/vyos-deploy/SKILL.md
nix develop . --command git commit -m "docs: site-level router parachute and the voice VLAN

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Ask Chris before pushing**

Tasks 1–4 are local commits on `main`. Ask whether to `git push` (his
standing preference is direct pushes to main, not PRs).
