# Core switch as code (TRENDnet TEG-30284) — design

Status: approved 2026-10-11. Chris: "proceed autonomously on this, i trust you
can figure out these pieces and how to handle so safely." This spec builds
**phase 1** (foundation) and specifies phase 2 (voice port). Phases 3–4 each
change other systems (BMC addressing, Plex, Tailscale routes, Wi-Fi hardware)
and get their own short spec when started.

## Goal

The core switch becomes config-as-code like the router, so VLANs and tagging
are a repo change. Chris's priorities, in order: **isolate risky devices**
(IoT, the ONE KVM, cameras) and give **management interfaces** (BMCs, the
switch itself) their own network; later the telephony lab's phones on VLAN 20.

Chris is often away, so every change must be **remote-safe**: the router is
reachable over Tailscale, and the router↔switch management path is kept out
of reach of every change, so it can always undo one.

Success:

1. The switch's VLAN and port config lives in `switch/teg-30284.yaml`.
   `swctl plan` shows drift; `swctl apply` converges, verifies, and rolls
   itself back on failure, including when the operator's session dies.
2. Only the router can manage the switch, over a path no `swctl` change can
   alter.
3. No factory credentials anywhere on the switch.

## What the switch is (probe, 2026-10-11)

| | |
|---|---|
| Model | TEG-30284 v2.0R: 24×1G + 4×10G SFP+ (ports 25–28), no PoE. Aricent ISS firmware **3.00.003** (latest 3.00.010 adds SSH keys). Boot loader 1.00.001. Uptime 77 days. |
| Management | `192.168.10.200/24` on `vlan1`, **no gateway**, no DHCP. Answers only on its own subnet: the router reaches it through a temporary `192.168.10.1/24` on eth2 (16b4f44c). LAN hosts can't reach it via the router (no return route), but any host on the LAN can by giving itself a 192.168.10.x address. |
| Credentials | Factory `admin`/`admin` (telnet + web). SNMP v2c `public` (RO) and `private` (**RW**); `public` can read the community table, i.e. learn `private`. Two built-in SNMPv3 users `ReadOnly`/`ReadWrite` with **noAuthNoPriv**. |
| CLI | Telnet only (SSH listens but is disabled until 3.00.010). Cisco-ish banner, D-Link-ish verbs. **Manages the box, not the switching**: `config account/configuration/firmware/ipif/ipv6if`, `save`, `reboot`, **`reset` (factory reset — never send)**, `show switch/ipif/boot_file/flash/route/firmware`, plus a `cloud` agent and debug verbs. No VLAN or port commands. Pager: `a` = all. **Leaks a session slot whenever a connection closes without `logout`** (`exit` is not a command); at about 4 leaked slots it answers "Exceeding Max Cli Sessions !" and the CLI is unusable until the slots expire or the switch reboots. |
| SNMP | Standard **Q-BRIDGE writes work** (tested on unused port 28 + VLAN 4000, reverted): create VLAN (`dot1qVlanStaticRowStatus` createAndGo **alone** — combining it with the name in one PDU gets commitFailed), name, egress/untagged bitmaps (8 bytes, port N = bit 7−(N−1)%8 of byte (N−1)/8), PVID. **The switch lets a port be untagged in several VLANs at once** and does not remove it from VLAN 1 — the tool must. Ingress filtering on and admit-all on every port. RSTP running; no topology changes. SNMP-COMMUNITY/USM/VACM MIBs present. |
| Web UI | Vue single-page app, RSA-encrypted login, undocumented API. Used once for what SNMP can't do (accounts, SNMPv3 users with auth, cloud agent). |
| Cloud agent | `cloud connect/serverurl/upgrade`: a vendor phone-home. Inert today (no gateway); must be off and WAN-blocked before the switch ever gets a gateway. |
| Clock | 2019 — no NTP. |

Port map from the switch's MAC table (2026-10-11):

| Port | Device | | Port | Device |
|---|---|---|---|---|
| 1 | VyOS eth2 (uplink) | | 18 | Hue bridge .227 |
| 8 | ONE KVM .2 | | 22 | BMC middle .117 |
| 10 | HDHomeRun .58 | | 24 | BMC top .119 |
| 12 | BMC bottom .151 | | 25 | bottom .242 (10G) |
| 14 | desk: workstation .21 + Mac Studio .3 (downstream switch) | | 26 | middle .241 (10G) |
| 16 | TP-Link Archer AXE75 AP .15 + Wi-Fi clients | | 27 | top .240 (10G) |

Free: 2–7, 9, 11, 13, 15, 17, 19–21, 23, 28. The voice VLAN 20 exists on the
router only: **the switch never had it** (CLAUDE.md said otherwise; corrected
with this work). The phones are not plugged in.

## Design

### Management path: protected, router-only

- **Interim (now):** router `192.168.10.1/24` on eth2 → port 1 → the switch's
  `vlan1` ipif.
- **Target:** a dedicated cable **router eth3 ↔ switch port 2**. Port 2 is the
  only member of **VLAN 99** (untagged, PVID 99); the switch ipif moves to
  VLAN 99 as `192.168.99.2/30`, gateway `.1`; router eth3 is
  `192.168.99.1/30` (`SWITCH-OOB`). Nothing else touches VLAN 99, so no
  change to the LAN can cut the router off from the switch.
- **Only the router talks to the switch.** `swctl` runs on the workstation
  but executes every SNMP/CLI operation *on the router* over SSH, so the
  switch never needs a route to anything but the router. The router firewall
  drops forwarding to and from `SWITCH-OOB` entirely: no host, tailnet or
  LAN, reaches the switch except via the router itself, and the switch has
  no WAN (the cloud agent stays dead).
- **Protected set:** port 2, VLAN 99, the ipif, and — while the interim path
  is in use — port 1's untagged VLAN 1 membership and PVID. `swctl` refuses
  any plan that touches them; adding *tagged* VLANs to port 1 is allowed
  (the uplink must carry them). Because the protected set is never touched,
  the router's SNMP path survives any `swctl` change, so a revert can always
  reach the switch.

### swctl

`tools/swctl`, built like `hactl`: Python, stdlib + PyYAML, `bin/swctl` on
PATH in `nix develop`, tests under `tools/swctl/tests`.

**Transport.** One SSH connection to the router (`chris@192.168.0.1`) per
command, running `net-snmp` there (`snmpbulkwalk`/`snmpget`/`snmpset`, all
present on VyOS). Credentials (community; later the SNMPv3 user) live in
`switch/credentials.secret.yaml` (git-crypt) and are passed on stdin, never
on a command line or in a log. The CLI client (telnet from the router) is a
small Python script piped to the router's `python3`, with a whitelist of
commands and `logout` in a `finally`.

**Desired state** (`switch/teg-30284.yaml`):

```yaml
vlans:                      # every VLAN on the switch; anything else is removed
  1:  {name: DefaultVLAN}
  20: {name: VOICE}
ports:                      # every port 1-28
  1:  {desc: VyOS eth2 uplink, untagged: 1, tagged: [20]}
  2:  {desc: router eth3 (switch OOB), untagged: 99, protected: true}
  10: {desc: HDHomeRun, untagged: 1}
```

Rules `swctl` enforces: every port has exactly one untagged VLAN and its PVID
equals it; a port is a member only of the VLANs listed for it; ingress
filtering stays on; VLANs referenced by ports exist; VLAN 1 is never
deleted; the protected set is unchanged.

**Commands.**

| Command | Does |
|---|---|
| `swctl status` | Ports: link/speed, VLANs, and who is behind each one (MAC table + router ARP + DHCP host names). Read-only. |
| `swctl import` | Writes `switch/teg-30284.yaml` from the live switch (bootstrap), keeping existing `desc`s. |
| `swctl plan` | Diff desired vs live as the exact SNMP operations, in order. Exit 2 = changes. Refuses invalid desired state or touches to the protected set. |
| `swctl apply` | See below. |
| `swctl backup` | Raw dump of the Q-BRIDGE/IF state to `switch/backups/` (git-tracked, not secret). |

**Apply sequence.**

1. Read live state; compute the plan; refuse if it touches the protected set.
2. **Snapshot** the live state of every VLAN/port the plan touches and render
   the *revert* as SNMP operations.
3. **Arm the dead-man on the router:** copy the revert script to the router
   and start it under `systemd-run --on-active=<N>min` (default 5). If the
   operator's session, the workstation, or the tailnet drops mid-change, the
   router reverts by itself.
4. Apply the operations, ordered so no port is ever without an untagged VLAN:
   create VLANs → add memberships → set PVIDs → remove old memberships →
   delete VLANs.
5. **Verify:** re-read the switch (must equal the desired state) and check
   that every device the MAC table placed on a changed port is still
   reachable from the router (ARP/ping, using the VLAN's gateway), unless
   the plan marks it as expected to move.
6. Pass → stop the dead-man. Fail → run the revert now, re-read, stop the
   dead-man, report.
7. **Save** is a separate, explicit `swctl save` (CLI `save`). Unsaved
   changes are lost on reboot, which is the last line of rollback: a reboot or
   power-cycle returns the switch to its last saved config.

So there are three lines of rollback: the in-session revert, the router's
dead-man, and the saved config. The first two need only the protected SNMP
path, which no change can break.

### Router changes (Ansible, commit-confirm)

- eth3: `SWITCH-OOB`, `192.168.99.1/30`, enabled (link down until cabled).
- Firewall: forward drop to/from `SWITCH-OOB` in both directions; input from
  `SWITCH-OOB` allows only NTP (the switch's clock) and established traffic.
- After the cut-over: remove `switch_factory_mgmt` (the temporary eth2
  address).

### Hardening (one-time)

1. SNMP, via SNMP from the router (remote): a new random RW community mapped
   to the `ReadWrite` security name (SNMP-COMMUNITY-MIB), verify it, then
   destroy `public`, `private` and the noAuth USM users. Do this **after** the
   cut-over to VLAN 99: before it, any LAN host could still reach the agent,
   and v2c is cleartext.
2. Web UI, once: new admin password (git-crypt), the cloud agent off; after
   the firmware upgrade, an SSH key for `admin` and telnet off. SNMPv3 authPriv
   replaces v2c only if the web UI can create such a user (USM cloning from a
   noAuth user can't add auth).
3. NTP from the router; syslog to the router later.

## Phases

**Phase 1 — foundation (this spec's build).**

| Step | Who / where | Risk |
|---|---|---|
| 1a `swctl` status/import/plan/backup + tests; import the live state | remote, read-only | none |
| 1b `swctl apply` with snapshot + dead-man + verify; rehearse on unused port 28 | remote | unused port only |
| 1c router eth3 `SWITCH-OOB` + firewall | remote, commit-confirm | link down |
| 1d VLAN 99 on port 2 via `swctl apply` | remote | unused port |
| 1e cable eth3 ↔ port 2; move the ipif to VLAN 99 (CLI `config ipif`); save | **Chris home** (power-cycle is the fallback) | mgmt only — the data plane is untouched |
| 1f SNMP hardening; remove 192.168.10.1 from eth2 | remote | protected path only |
| 1g firmware 3.00.010 after a config backup; web-UI hardening | **Chris home** | whole-house outage of about 3 min; brick risk |

**Phase 2 — voice port.** Port 1 carries VLAN 20 tagged; one free port
(proposed: 9) becomes untagged VLAN 20 for the phones' PoE switch, labelled.
Applied before the phones are plugged in; `router/scripts/verify-voice-vlan.sh`
proves it once they are.

**Phase 3 — management and KVM (own spec).** VLAN 30 `MGMT` for the BMCs
(ports 12, 22, 24: their IPs change, ipmi-exporter targets follow, galaxy +
admin hosts allowed in, no WAN). VLAN 31 `KVM` for the ONE KVM alone, since
it has a pre-auth RCE and must not share L2 with the BMCs; the tailnet /32
route and the kvm-rdp bridge follow it.

**Phase 4 — IoT (own spec).** VLAN 40 `IOT`, no access to LAN except named
services (HA, Plex → HDHomeRun). Wired: Hue bridge, HDHomeRun (Plex's DVR
device URL changes). Wi-Fi IoT needs an access point that maps an SSID to a
VLAN; the Archer AXE75 can't. Options for Chris: a second, cheap AP on an IoT
access port, or replace the AXE75 with an Omada EAP that does both.

## Rules for working on the switch

- Never send `reset` or `reboot` from automation (the rollback reboot is a
  manual, deliberate step); never `save` without a verified state.
- Every CLI session ends with `logout`.
- No switch writes while galaxy is mid-maintenance or the DVR is recording a
  game Chris cares about, unless the write touches only unused ports.
- The protected set is changed only by hand, with Chris home.

## Testing

- Unit tests (pytest, no network): bitmap encode/decode, YAML ↔ model,
  invariants, plan ordering, revert rendering, protected-set refusal.
- Live: `swctl status`/`plan` against the switch; the apply rehearsal on port
  28 (create, verify, revert, dead-man fire and disarm).
