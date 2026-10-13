# Core switch — TRENDnet TEG-30284

The switch every wired device hangs off: 24×1G + 4×10G SFP+ (ports 25–28),
Aricent ISS firmware 3.00.003. Its VLANs and ports are code:
`switch/teg-30284.yaml` is the desired state and `swctl` (`tools/swctl`, in
`nix develop`) converges the switch to it. Design and phases:
`docs/superpowers/specs/2026-10-11-core-switch-as-code-design.md`.

`swctl status` shows the live port map (link, VLANs, and who is behind each
port from the MAC table, the router's ARP table and DHCP leases).

## How the switch is managed

Only through the router. Router **eth3** (`SWITCH-OOB`, `192.168.99.1/30`) is
cabled to switch **port 2**, the only member of **VLAN 99**; the switch's one
management address is `192.168.99.2` on VLAN 99 (its `vlan1` interface is
disabled). The router's forward rules 130/131 drop everything to and from
eth3, so no LAN or tailnet host reaches the switch and the switch reaches
nothing. `swctl` runs its SNMP/CLI work on the router over SSH.

No change to the LAN can cut this path: `swctl` refuses to touch the
`management:` port or VLAN, and VLAN 99 may be on no other port.

Web UI (rarely needed — accounts, SNMPv3, things SNMP can't do):

```bash
ssh -N -L 18081:192.168.99.2:80 chris@192.168.0.1   # then http://localhost:18081 (Chromium; Firefox's login got a 400)
```

## Day to day

| Command | Does | Exit |
|---|---|---|
| `swctl status` | ports, links, VLANs, devices | 0 |
| `swctl plan` | diff the file against the switch, as the writes `apply` would make | 0 in sync, 2 changes, 3 refused (management path not as declared) |
| `swctl apply` | converge (see below) | 0 applied, 1 reverted, 3 refused, 4 revert incomplete, 5 the dead-man fired first |
| `swctl save` | CLI `save` — only when the switch matches the file and no dead-man is armed | 3 refused |
| `swctl backup` | raw SNMP dump to `switch/backups/teg-30284.snmp` (commit it) | |
| `swctl disarm` | stop a dead-man left armed by a crashed apply | |
| `swctl import` | regenerate the file from the switch (bootstrap; keeps `desc`s) | |

A change: edit `switch/teg-30284.yaml`, `swctl plan`, `swctl apply`, check
`swctl status`, `swctl save`, `swctl backup`, commit.

### What `apply` does

1. Reads the switch; refuses unless the switch itself learns the router's
   management MAC on the declared `management:` port, untagged in its VLAN —
   so editing the file can't redirect the management path.
2. Notes which **canaries** (the file's list plus devices on changed ports
   that should stay put) answer ARP; refuses if none answer (a broken check
   must not pass silently).
3. Arms a **dead-man** on the router: a `systemd-run` timer (default 5 min)
   that replays the reverse writes. If this session, the workstation or the
   tailnet dies mid-change, the router puts the switch back by itself.
4. Writes, ordered so no port is ever without an untagged VLAN.
5. Verifies: the switch must equal the file, and every canary that answered
   must still answer (a lost one gets a second look after 5 s — Wi-Fi clients
   doze). Pass → disarm, then re-read once more (exit 5 if the dead-man beat
   us to it). Fail → revert now from the switch's *actual* state, then disarm.
6. Never saves. Unsaved changes vanish on a reboot or power cycle — the last
   line of rollback.

Rehearsed 2026-10-11 on unused port 28: apply + revert, and a simulated
session loss where the router's dead-man reverted on its own after 1 min
(`save` refused while it was armed). The dead-man script is shipped to the
router base64-encoded and writes `/tmp/swctl-deadman-fired` first.

## Rules

- Never send CLI `reset` (factory reset) or `reboot` from automation.
- Every CLI session ends with `logout`. `exit` is not a command, and a session
  closed without `logout` holds one of ~4 slots for ~30 min ("Exceeding Max Cli
  Sessions !"). `swctl` guarantees it; hand-rolled telnet must too.
- Save only a verified state (`swctl save` checks).
- Writes that change VLAN 1's membership wait until no DVR recording is in
  progress, unless they touch only unused ports.
- The management path (`management:`, port 2, VLAN 99, the switch's ipif)
  changes only by hand, with someone at the switch.

## Gotchas

- The switch lets a port be untagged in several VLANs at once and never
  removes it from VLAN 1 itself; `swctl` keeps exactly one.
- Creating a VLAN over SNMP needs `createAndGo` alone in its PDU; the name
  is a separate write.
- The CLI manages the box, not switching: `config ipif <vlan> ipaddress A.B.C.D
  MASK` edits an existing IP interface; creating one needs the web UI
  (System → L3 Feature → IPv4 Interface). `save` prints nothing on success.
- Recovering management if eth3 or its cable dies: plug a laptop into switch
  port 2 with `192.168.99.1/30`.

## Hardening still to do (spec phase 1f/1g)

- [ ] SNMP: new random RW community (SNMP-COMMUNITY-MIB, via the router), then
      destroy `public`/`private` and the noAuth USM users `ReadOnly`/`ReadWrite`.
      Update `credentials.secret.yaml` in the same change.
- [ ] Web UI: admin password (git-crypt), cloud agent off.
- [ ] Firmware 3.00.010 (SSH keys, security fixes) — someone home; about 3 min
      whole-house outage; config backup first.
- [ ] NTP from the router (needs an input rule for 192.168.99.0/30).

## Next phases

Phase 2 voice: **done 2026-10-13** (VLAN 20 "VOICE"; port 1 tagged 20; port 6 = IP
phone, untagged 20); phase 3 MGMT (BMCs) + KVM VLANs; phase 4 IoT — see the spec.
