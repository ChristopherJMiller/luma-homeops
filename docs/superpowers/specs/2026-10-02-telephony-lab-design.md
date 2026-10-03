# Telephony lab — design

**Status:** draft for review · **Date:** 2026-10-02 · **Scope:** lab, internal calls only

## 1. Intent

**What Chris asked for.** Get the IP phones onto their own network,
labelled by person or location, and able to call each other. Grow it
step by step from there. The long-term aim is a general way to connect
any PBX number to any audio system: a handset, a person, a TV tuner, and
eventually an AI agent. Keep it Kubernetes-native on galaxy.

**Explicitly not in this lab:** AI or voice agents, a PSTN trunk (no
outside line), voicemail, paging/intercom, TLS/SRTP, and HA. The house
telephony design doc (2026-09-06) remains the long-term vision. This lab
departs from it on one point by choice: the PBX runs on the cluster, not
on a bare-metal NixOS host.

**Done when** (detail in §8):

1. Both Cisco 7945Gs are on VLAN 20, registered over SIP, and each shows
   its own label.
2. Phone ↔ phone and phone ↔ softphone calls work, with audio both ways
   and caller ID names.
3. Dialling a person rings that person's phones.
4. Dialling a stream number plays live HDHomeRun audio, and holds a tuner
   only while someone is listening.

## 2. Starting state

Observed read-only on 2026-10-02.

| Thing | State |
|---|---|
| Handsets | Two **Cisco CP-7945G**: `.61` (MAC `D0C78915EBB5`) and `.62` (MAC `D0C78915E8C8`). Firmware `jar45sccp.9-4-2SR4-3`, i.e. **SCCP, not SIP**. Both still configured for their previous owner's Unified CM (`*.blackhawk.edu`). Clocks read 2018. No CDP/LLDP neighbour, so they sit on an unmanaged switch. |
| Router | VyOS. One flat LAN on `eth2` (`192.168.0.0/24`). `eth1`, `eth3` and `eth4` are unused. **The forward filter has no default-deny.** Its only rule sends WAN→inside traffic to `OUTSIDE-IN`. The input filter defaults to drop and has **no NTP rule**: the router's NTP `allow-client` list covers the LAN, but the firewall drops it. That's a pre-existing gap, flagged and not fixed here. |
| Cluster | No Talos host firewall (no `NetworkRuleConfig`), so `hostNetwork` ports are reachable. Nothing in the repo uses `hostNetwork`. `middle` is at ~99% of memory requests, so avoid it. |
| HDHomeRun | FLEX DUO (`HDFX-2US`) at `192.168.0.58`: **2 tuners**, 66 ATSC channels, each at `http://192.168.0.58:5004/auto/v<ch>` with AC3 audio. |
| Repo | **Public.** Cisco firmware and SIP passwords must never be committed in clear. |

## 3. Approaches considered

| Option | Verdict |
|---|---|
| **Asterisk (PJSIP), stock community image, our own manifests** | **Chosen.** Asterisk is the design doc's PBX. Its dialplan can send any number anywhere, including AudioSocket for streams and later agents. The config is text, so it suits GitOps. |
| kubevoip (Helm chart; Kamailio + RTPengine + Asterisk + Postgres) | **Rejected.** Its `AsteriskPool` CRD exposes only `echoExtension` and `voicemail`, with no custom dialplan, so a number can't be routed to a stream without forking it. It's also one maintainer, created June 2026 at v1alpha1, with no feature commits since June. It needs Postgres, which is fsync-sensitive on the SMR Ceph, plus UDP LoadBalancers. Its CRD model (`SIPUser`, `CallRoute`) is roughly our registry in §5.1, but with an operator we couldn't fix. |
| FreeSWITCH | Excellent media handling, but the packages now sit behind a SignalWire token, the config is XML, and it would mean departing from the design doc's AudioSocket/ARI architecture. |
| FreePBX / FusionPBX | GUI-driven with database-held state. Works against GitOps. |
| Existing Asterisk Helm charts | None maintained (`fonos` 2020, `routr` 2021). A chart would only wrap a Deployment, a ConfigMap and a Secret anyway. |
| Keep SCCP (`chan-sccp-b`) | Rejected. Asterisk removed `chan_skinny` in version 21, every future handset will be SIP, and the 7945 has an official SIP load. |

## 4. Network: voice VLAN 20

### 4.1 Shape

```
             VyOS eth2  ── untagged: LAN 192.168.0.0/24 (unchanged)
                        └─ vif 20:  VOICE 192.168.20.0/24, gw .1
                                 │
   unmanaged switch ─────────────┤  (passes 802.1Q frames)
     ├─ 7945 "office"   tags its own frames VLAN 20 (Admin VLAN ID)
     ├─ 7945 "kitchen"  tags its own frames VLAN 20
     └─ node `top` 192.168.0.240 (untagged LAN) ← Asterisk, hostNetwork
```

The router routes between VOICE and LAN **without NAT**, so Asterisk sees
the phones' real addresses. That's what keeps SIP simple.

**Interim weakness, accepted:** each phone chooses its own VLAN, so a
compromised phone could drop the tag and land on the LAN. The firewall
still blocks VOICE→LAN, but only for frames that are actually tagged. The
fix is a managed PoE switch with VLAN 20 access ports, which needs
**zero router changes**. Until then this is separate addressing, not
containment.

### 4.2 Router changes (Ansible, `router/ansible/`)

Everything goes through the playbooks and is deployed with
`commit-confirm 10` (rule S8, vyos-deploy skill), because it touches
interfaces, DHCP and the firewall.

| Area | Change |
|---|---|
| Interface (`network.yml`, group_vars `networks.voice`) | `eth2 vif 20`, address `192.168.20.1/24`, description `VOICE` |
| DHCP (`services.yml`, new group_vars block) | Shared network `VOICE`, subnet `192.168.20.0/24`, `subnet-id 2`, range `.100–.199`, lease 86400, default router `.1`. **Option 66 (`tftp-server-name`) = `192.168.0.240`** (Cisco phones fall back to 66 when 150 is absent). **`ntp-server` = `192.168.20.1`**. No DNS option: everything is addressed by IP. |
| Firewall groups (`firewall.yml`) | Interface group `VOICE` = `eth2.20`. Network group `NET-VOICE-v4` = `192.168.20.0/24`. Address group `PBX-v4` = `192.168.0.240`. |
| Forward filter: VOICE → PBX | Accept from inbound group `VOICE` to `PBX-v4` for: `5060` tcp+udp (SIP), `10000–10099` udp (RTP), `69` udp (TFTP), `8088` tcp (phone directory). |
| Forward filter: VOICE → anything else | **Drop.** This rule follows the accepts above. It covers the rest of the LAN and the WAN; there's also no source NAT for `192.168.20.0/24`. |
| Forward filter: WAN → VOICE | **Drop** (destination `NET-VOICE-v4`, inbound `WAN`). Defence in depth, since there's no forward default-deny. |
| Input filter: VOICE → router | Accept udp `123` (NTP) from `NET-VOICE-v4`. DHCP needs no rule: Kea uses raw sockets, the same reason LAN DHCP works today under input default-drop (verify on rollout). Everything else hits the input default drop. VOICE isn't in interface group `LAN`, so SSH management stays LAN-only. |

LAN → VOICE stays allowed (no rule; the forward default is accept). Asterisk
needs it to send INVITEs and RTP to the phones and to serve TFTP data from
its ephemeral ports. Admins need it for the phones' web UIs. Replies flow
through the global `established`/`related` state policy.

## 5. PBX: `cluster/telephony/`

### 5.1 The registry: one file of record

`cluster/telephony/registry.yaml` is human-edited and contains no secrets.
Every number in the house is defined here:

```yaml
pbx:
  address: 192.168.0.240        # node `top`; phones register and fetch TFTP here
phones:                         # endpoints: things that register
  - id: office                  # PJSIP endpoint + SIP username; stable
    number: 101
    label: Office               # phone screen label + caller ID name
    type: cisco-7945            # cisco-7945 → TFTP config generated
    mac: D0C78915EBB5
  - id: kitchen
    number: 102
    label: Kitchen
    type: cisco-7945
    mac: D0C78915E8C8
  - id: chris-laptop
    number: 150
    label: Chris (laptop)
    type: softphone             # no provisioning; user types creds into Linphone
people:                         # aliases: dialling one rings a set of phones
  - id: chris
    number: 301
    name: Chris
    rings: [office, chris-laptop]
streams:                        # one-way audio from any ffmpeg-readable URL
  - id: king5
    number: 705
    name: KING 5
    source: http://192.168.0.58:5004/auto/v5.1
```

The ids, labels and people above are illustrative. The initial contents
are whatever Chris picks at implementation time, and changing any of them
is a one-line edit.

**Number plan.** This follows the design doc where it applies and reserves
the rest so later stages don't renumber anything.

| Range | Meaning | Lab |
|---|---|---|
| `1XX` | Phones (locations) | yes |
| `2XX` | Intercom, auto-answer (design doc §8) | reserved |
| `3XX` | People | yes |
| `600` | Echo test | yes |
| `7XX` | Streams | yes |
| `0`, `9…`, `*8` | Assistant, outside line, page-all | reserved; plays "not available" |
| `911` | Emergency | **plays an explicit "this phone cannot call emergency services" announcement.** No PSTN exists. A dead line on 911 is worse than a clear message. |

**Extension point (not built).** A future `audiosocket:` entry, i.e.
`{number, service: host:port}`, routes a number to *any* service that
speaks AudioSocket, two-way. That's how an AI agent plugs in later without
touching Asterisk config by hand. `streams` is sugar for "AudioSocket to
the stream bridge, with this source".

### 5.2 Generation: commit time, reviewable

`scripts/render-telephony.py` reads `registry.yaml` and writes
`cluster/telephony/generated/`:

| Output | Content |
|---|---|
| `pjsip_registry.conf` | One endpoint + aor + auth per phone, built from templates. The password is the placeholder `@@SIP_PASSWORD:<id>@@`. |
| `extensions_registry.conf` | The `[registry]` dialplan context: phones → `Dial(PJSIP/<id>,30)`; people → `Dial(PJSIP/a&PJSIP/b,30)`; streams → `Answer()`, `AudioSocket(<uuid>,stream-bridge.telephony.svc.cluster.local:9092)`, then a "service unavailable" playback if the bridge refuses. |
| `tftp/SEP<MAC>.cnf.xml` | Cisco SIP config per `cisco-7945`: registrar `pbx.address`, line 1 (`id`, label, placeholder password), `phoneLabel`, NTP `192.168.20.1`, time zone, `loadInformation` = the SIP load, `directoryURL` → §5.4. |
| `static-http/directory.xml` | A `CiscoIPPhoneDirectory` of every phone, person and stream, by name. |
| `streams.json` | `{uuid: {id, name, source}}`. The UUID is `uuid5(fixed namespace, stream id)`, so it stays stable across renders. The stream bridge reads this file; it never parses the registry. |

It runs as a **pre-commit hook**, the same pattern as `mirror-family-list.sh`.
When it rewrites outputs, the commit stops so the generated diff gets
reviewed and staged. It's also a validator: duplicate numbers, numbers
outside their range, unknown `rings` ids or an invalid MAC fail the
commit. It checks that every phone `id` has a key in
`sip-passwords.secret.yaml`, which is readable locally because git-crypt
is unlocked. It never prints values (rule S9). If PyYAML isn't in the
toolchain, it gets added through the nix-shell-pin skill.

Hand-written, non-generated config sits next to it in
`cluster/telephony/config/`: `pjsip.conf` (transport, endpoint templates,
`#include`s the generated file), `extensions.conf` (`[from-phones]` with
911, the reserved numbers, `600`, and a catch-all "not in service";
`#include`s the generated context), `rtp.conf` (`10000–10099`),
`modules.conf`, `http.conf`, `manager.conf` (disabled) and `ari.conf`
(disabled).

### 5.3 Workload

The Argo Application `cluster/applications/telephony.yaml` follows the
`music-streaming` pattern, with `managedNamespaceMetadata` setting
`pod-security.kubernetes.io/enforce: privileged`. That's required for
`hostNetwork`. The kustomization uses `configMapGenerator` for both
`config/` and `generated/`, so a hashed name rolls the pod whenever
anything changes (the mcp-gateway lesson of 2026-09-27).

**Deployment `asterisk`:** `replicas: 1`, `strategy: Recreate` (host
ports can't overlap), `hostNetwork: true`,
`dnsPolicy: ClusterFirstWithHostNet` (so it can resolve the bridge
Service), `nodeSelector: kubernetes.io/hostname: top`.

| Container | Image | Job |
|---|---|---|
| init `render` | `andrius/asterisk:22.10.1_debian-trixie@sha256:…` (reused; it has `sh` + `sed`) | Seeds emptyDir `/etc/asterisk` from the image's stock configs, then overlays both ConfigMaps on top, so any file we don't ship keeps its upstream default. Writes the generated TFTP files into emptyDir `/tftp`. Copies the SIP firmware from the PVC into `/tftp`. Replaces every `@@SIP_PASSWORD:<id>@@` with the matching key from the mounted Secret. **Fails the pod if any `@@…@@` remains**, so a missing password is a visible CrashLoop rather than a phone that silently can't register. |
| `asterisk` | same image, default entrypoint (drops to the `asterisk` user itself) | SIP on `192.168.0.240:5060` udp/tcp; RTP `10000–10099`; HTTP `8088` serving only `/static/` (the directory). AMI and ARI disabled. |
| `tftp` | a stock **TFTP-only** server image (tftp-hpa class; exact image chosen and digest-pinned in the plan) | Serves `/tftp` read-only on udp 69. **Deliberately not dnsmasq.** On `hostNetwork` on the LAN, dnsmasq is one misplaced flag away from a rogue DHCP server for the whole house. |

**Ports bound on `top`:** 5060, 10000–10099, 69 and 8088. None clashes
with Talos or Kubernetes (kubelet and control-plane ports are
10248–10259). The plan verifies this on the node before the first sync.

**Storage:** PVC `tftp-firmware`, 1 Gi, `rook-ceph-block`, RWO (the pod is
pinned, so RWO is fine). It holds the Cisco SIP load for 7945/7965,
loaded **once by hand** and documented in the runbook, never committed.
The PVC is also mounted read-write at `/firmware` in the `tftp` container,
so `kubectl cp` has somewhere to write. A `kubectl rollout restart`
afterwards makes the init container copy it into `/tftp`. Firmware only matters for the SCCP→SIP migration and for
any future 79x5. A factory reset clears config, not firmware.

**Secret:** `sip-passwords.secret.yaml` holds the cleartext source of
truth (git-crypt): one key per phone `id`, 24+ random characters. Flow:
`./sign.sh` → `sip-passwords.yaml` (SealedSecret).

### 5.4 Labels, caller ID and the directory

"Assign people or locations to phones" shows up in four places, all
generated from the registry:
- the phone's own screen (`phoneLabel` + line label);
- the caller ID name on the receiving phone (PJSIP `callerid`);
- the Directories button (`directory.xml`, served by Asterisk's built-in
  HTTP static content, so no extra server);
- dialling a person's `3XX` number to ring their set of phones.

### 5.5 Security posture (lab)

- Asterisk listens only on the node's LAN address. **No NAT rule, no WAN
  exposure, no trunk.** Stolen credentials can make internal calls and
  nothing else.
- PJSIP has no anonymous endpoint, and each phone has its own password.
  An endpoint ACL accepts registrations only from `192.168.20.0/24`
  (phones) and `192.168.0.0/24` (softphones).
- **Known Cisco-SIP limitation:** a `SEP<MAC>.cnf.xml` contains its
  line's password in clear, and anyone who can reach TFTP can fetch it.
  TFTP is reachable only from VOICE and the LAN. Accepted for a lab with
  no PSTN; revisit before a trunk exists.
- AMI and ARI are off. HTTP serves static files only.

## 6. Stream bridge: `images/stream-bridge/`

The one piece of custom runtime code. It's a Python asyncio service
(standard library only) plus `ffmpeg`, with a Dockerfile in
`images/stream-bridge/` built by a GitHub Actions workflow like the other
`images/*`. The image is pinned as `:sha-<commit>@sha256:…` in its
Deployment, with the same two-commit pattern as CLAUDE.md sharp edge 6.

It runs as a normal pod in namespace `telephony`, not `hostNetwork`, with
ClusterIP Service `stream-bridge:9092` (TCP). It mounts `streams.json`
from the generated ConfigMap.

**Behaviour:**
1. Asterisk connects and sends the AudioSocket ID frame (a 16-byte UUID).
   An unknown UUID closes the connection, and the dialplan plays "service
   unavailable".
2. The UUID maps to a source. If a **session** for it is already running,
   this caller joins it. Otherwise the bridge starts
   `ffmpeg -i <source> -vn -ac 1 -ar 8000 -f s16le pipe:1`.
3. The session reads 320-byte frames (20 ms of 8 kHz signed-linear audio,
   the protocol's baseline format) and fans them out to each listener's
   **bounded** queue. A slow listener drops frames; it never stalls the
   others.
4. Each listener is paced at 20 ms on a monotonic clock. Audio coming
   from Asterisk is read and discarded. Hangup or EOF removes the
   listener.
5. **When the last listener leaves, a 5-second grace period starts, then
   ffmpeg is killed, which frees the tuner.** That's why the bridge exists
   at all; Asterisk's music-on-hold approach would hold a tuner around the
   clock.
6. If ffmpeg fails to start or exits (for example the HDHomeRun answering
   503 when both tuners are busy), every listener in the session is
   closed and the dialplan plays "service unavailable". No dead air.

**Known lab behaviour:** callers hear about 1–3 s of silence while the
tuner locks. Audio is narrowband, because AudioSocket is 8 kHz even
though the 7945s can do G.722 between themselves.

**Tests:** pytest unit tests in `images/stream-bridge/tests/`, run in the
image CI:
- AudioSocket frame encode/decode, including the ID, audio, hangup and
  error kinds;
- session lifecycle against a fake source process: join, fan-out, grace
  teardown, source failure → listeners closed;
- pacing (frames per second within tolerance).

## 7. Phone migration: SCCP → SIP (runbook)

Written up at implementation as `docs/telephony.md`. The shape:

1. Chris supplies the Cisco SIP load for 7945/7965 (`cmterm-7945_7965-sip.9-4-2…`;
   Cisco keeps it behind an account login). Load it into the PVC.
2. Factory-reset each phone: hold `#` while powering it on, then press
   `123456789*0#`. This clears the Blackhawk config and its trust list
   (ITL); otherwise the phone rejects a config file that isn't signed. If
   it still rejects the config, erase the ITL manually under Settings →
   Security → Trust List.
3. Unlock settings (`**#`). Under Network Configuration → Admin. VLAN ID,
   enter `20`. The phone re-DHCPs on VOICE and gets option 66 = `.240`.
4. The phone fetches `SEP<MAC>.cnf.xml`, sees that `loadInformation`
   names the SIP load, downloads it over TFTP, reboots into SIP, fetches
   its config again and registers.

## 8. Stages and exit criteria

Each stage ends at a check, and nothing moves on until it passes.

**Stage 1: voice VLAN.**
- Router change applied under commit-confirm, then confirmed.
- Verified **from the dev machine** with a temporary VLAN sub-interface
  (`ip link add link enp3s0 name enp3s0.20 type vlan id 20`, then DHCP)
  *before* touching the phones:
  - it gets a `192.168.20.1xx` lease carrying options 66 and NTP;
  - it can reach `192.168.0.240` on the four allowed ports;
  - it **cannot** reach other LAN hosts (e.g. `.7`, `.58`) or the
    internet;
  - NTP from `.1` answers.
- Then the sub-interface is removed.

**Stage 2: Asterisk and phones.**
- The Argo app is Healthy and the pod is Running on `top`.
- `pjsip show endpoints` shows both 7945s and the softphone as
  registered.
- 101↔102 and 101↔150 calls work both ways, with two-way audio and caller
  ID showing labels.
- Dialling a person rings all of that person's phones.
- `600` echoes.
- `911` plays the announcement; reserved numbers play "not available".
- Each phone shows its label and the correct time; the Directories button
  lists everything.

**Stage 3: stream bridge.**
- Dialling `705` plays KING 5 audio within about 3 s.
- Two phones on `705` at once use **one** tuner (check
  `http://192.168.0.58/status.json`).
- After both hang up, the tuner is free within about 10 s.
- With both tuners busy, the caller hears "service unavailable".

## 9. Operations

- **Availability:** phones depend on node `top`. A drain or reboot of
  `top`, including the pending Talos upgrade hops, means no calls until
  the pod returns. That's accepted for a lab.
- **Rollback:**
  - Stage 1: commit-confirm auto-reverts if not confirmed. After
    confirmation, revert the Ansible commit and re-apply. Phones go back
    to the LAN when Admin VLAN ID is cleared.
  - Stage 2/3: revert in git and Argo prunes. Deleting the namespace or
    PVC asks first (rule S4).
- **Config changes:** edit `registry.yaml` → commit (the hook regenerates)
  → Argo rolls the pod, which drops any active calls. Fine for a lab.
- **Monitoring:** out of scope for v1. When it's added, any
  ServiceMonitor needs `release: prometheus` (memory: inert monitors).

## 10. Out of scope / later

AI agents (via the `audiosocket:` extension point); PSTN trunk;
voicemail; paging and auto-answer intercom (`2XX`, `*8`); TLS/SRTP;
managed PoE switch (no router change needed); metrics and a Grafana
dashboard; IVR-style channel selection ("dial 700, then the channel");
moving the PBX off a single pinned node.
