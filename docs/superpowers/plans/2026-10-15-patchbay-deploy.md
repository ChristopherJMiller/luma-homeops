# Deploy patchbay to galaxy (`telephony`)

**Status:** draft for sign-off (CLAUDE.md "Plan, then execute").
**Spec:** `docs/superpowers/specs/2026-10-14-patchbay-design.md` §6 ("In
luma-homeops") and §7. **Code:** github.com/ChristopherJMiller/patchbay
(release A complete: W1–W3 plus a final review; CI green on GitHub).

**Goal:** the PBX runs on `top`, the Desk phone shows up in `patchbay status`
with an accurate state and human action, a Linphone softphone registers and
calls 600/911/0, and the second 7945 appears as an unknown phone. Real
calls on the Desk phone wait for SIP firmware (Chris's Cisco zip), which
this plan does not need.

---

## 0. Preconditions (each is a go/no-go)

| # | Check | State on 2026-10-15 | Needed |
|---|---|---|---|
| P1 | Ceph healthy enough to land changes | **HEALTH_ERR**: `pg 1.b` (block-pool) `active+clean+inconsistent`, 1 scrub error. Object `rbd_data.5e2c3a16c18.000000000008473e`: a `read_error` on **osd.4's** shard only (middle, `ST8000DM004 …ZR15X13V`); osd.0 and osd.2 hold clean copies. osd.4's drive: SMART passed, 264 reallocated sectors, **flat since April** (not growing), 0 pending. Also `BLUESTORE_SPURIOUS_READ_ERRORS` on osd.4 (1 retried read) and the usual MDS cache-pressure warning from `rook-satellites`. | **Chris approves `ceph pg repair 1.b`** (S7). It rewrites osd.4's copy from the two good replicas. Then a deep scrub of 1.b (`ceph pg deep-scrub 1.b`) must come back clean, and health returns to its usual HEALTH_WARN. If osd.4's reallocated count starts growing, plan a CMR replacement (sharp edge 1). |
| P2 | Argo otherwise healthy | Every app Synced/Healthy except `rook`, `rook-cluster` (Degraded = P1) | P1 resolved |
| P3 | Host ports free on `top` | Checked (`talosctl netstat -l`): 69, 5060, 5038 (loopback), 6970, 8088, 9440, 10000–10099 all free; `top` has kubelet/etcd/apiserver/Talos, node-exporter 9100, MetalLB 7472/7946, VXLAN 4789 | Re-check on the day |
| P4 | Namespace `telephony` absent | Absent | Re-check |
| P5 | patchbay release `v0.1.0-rc.1` published | Not yet (see §1) | §1 done, CI green |

## 1. patchbay side (before luma-homeops)

1. **Final review lands** (running now): fixes merged to main, CI `ci-ok` green.
2. **The chart carries its image digest.** Today a chart version names its
   image by tag only (`appVersion`), and luma-homeops would have to pin the
   digest separately; a chart bump from Renovate would then run the new
   chart with the old pinned image. Change the `release` job to write the
   `image` job's digest into the packaged chart's `values.yaml`
   (`image.digest`), so one chart version = one exact image. luma-homeops
   then pins only the chart version. (Small CI change; docs/releasing.md.)
3. **Cut `v0.1.0-rc.1`.** First run of the release pipeline: image
   `0.1.0-rc.1`, GitHub pre-release with the chart, Helm index on gh-pages.
   Verify `helm search repo patchbay --devel` sees it and the chart's
   `image.digest` matches the attested image (`gh attestation verify`).

## 2. luma-homeops changes (one commit)

Follows the repo's existing pair pattern (`home-assistant-release` +
`home-assistant`, `mega-media-release` + `media`) rather than the spec's
single multi-source app: the chart in one Application, the resources in
another. **Deviation from spec §6, recorded in the runbook.**

- **`cluster/applications/telephony-release.yaml`** (chart):
  - `repoURL: https://christopherjmiller.github.io/patchbay`, `chart: patchbay`,
    `targetRevision: 0.1.0-rc.1`, `releaseName: patchbay`;
  - destination namespace `telephony`, `CreateNamespace=true`,
    `managedNamespaceMetadata.labels: {pod-security.kubernetes.io/enforce: privileged}`
    (hostNetwork + NET_BIND_SERVICE; the `media`/`music-streaming` pattern);
  - automated sync, prune, selfHeal;
  - `valuesObject`:
    ```yaml
    pbx:
      nodeName: top
      address: 192.168.0.240
    timezone: America/Los_Angeles
    ntpServer: 192.168.20.1          # voice VLAN gateway (router input rule 70)
    allowedNetworks: [192.168.20.0/24, 192.168.0.0/24]   # phones; LAN softphones
    firmware:
      persistence:
        storageClassName: rook-ceph-block-ssd   # galaxy has NO default StorageClass
        size: 1Gi
    podMonitor:
      enabled: true                  # labels release: prometheus by default
    ```
    Number plan: chart defaults (1XX devices, 3XX people, 7XX streams, 600,
    911, reserved 2XX/0/9./*8).
- **`cluster/applications/telephony.yaml`** (resources): path
  `cluster/telephony`, namespace `telephony`, automated sync with prune,
  `SkipDryRunOnMissingResource=true` (the first sync can race the CRDs).
- **`cluster/telephony/`**: `kustomization.yaml` plus
  ```yaml
  kind: Phone      # desk
  spec: {model: cisco-7945, mac: D0C78915E8C8, number: "100", label: Desk}
  kind: Softphone  # chris-laptop
  spec: {number: "150", label: "Chris (laptop)"}
  kind: Person     # chris
  spec: {number: "301", name: Chris, rings: [{phone: desk}, {softphone: chris-laptop}]}
  ```
- **`cluster/applications/kustomization.yaml`**: both apps.
- **`.renovaterc.json`**: add `patchbay` to the "operators with upgrade/delete
  authority" rule (patch automerges, minor/major wait for a human), since a
  chart bump recreates the PBX pod and drops calls.
- **Docs:** `docs/telephony.md` runbook (status, softphone QR, firmware load,
  adding a phone, the second-phone switch step, rollback); CLAUDE.md
  topology row "Voice VLAN" (PBX deployed; `patchbay status`); the
  2026-10-02 lab spec gets "superseded by patchbay".

## 3. Router: TCP 6970 (decision needed)

The voice VLAN's `VOICE-OUT` chain allows .240's 5060, RTP 10000–10099, 69
and 8088 and **drops** everything else, including tcp 6970, where 9.x
Cisco firmware may try HTTP config downloads before TFTP. A silent drop
makes each such file wait out a timeout.

| Option | Cost | What we learn |
|---|---|---|
| **A. Allow tcp 6970 now (recommended)** | One Ansible rule (`VOICE-OUT rule 50`), commit-confirm. Serves the same files TFTP 69 already serves, read-only, connection-capped (128 total, 16 per source) | patchbay's fetch log records every :6970 fetch, which answers spec §8.6 "does it try 6970?" better than a drop log |
| B. Keep the drop, decide at the bench | none now | needs the drop logged (`default-log` on VOICE-OUT) to learn anything |
| C. Turn the drop into a reject | one rule | the phone falls back at once; we never learn if it would use 6970 |

Deploy with the vyos-deploy skill: `--check --diff`, then `site.yml --tags
firewall` (commit-confirm parachute). Independent of §4's order; can go
first.

## 4. Order of operations

1. P1: Chris approves → `ceph pg repair 1.b` → deep-scrub clean → HEALTH_WARN.
2. §1 (patchbay): review fixes, digest-in-chart, `v0.1.0-rc.1`.
3. §3 (router), if option A.
4. Re-run P2–P4. Build the CLI from the tag (`nix develop -c make build` in
   patchbay at `v0.1.0-rc.1`).
5. Commit + push §2. Watch (Monitor, not loops):
   - Argo: `telephony-release`, `telephony` → Synced/Healthy;
   - `kubectl -n telephony get pods -o wide`: controller Running; PBX pod on
     `top` with asterisk, agent, fileserver Ready (first start renders
     config after the controller mints the agent token: ~1 min);
   - PVC `Bound` on `rook-ceph-block-ssd`.
6. Verify with the CLI, always `--kubeconfig /tmp/galaxy-kubeconfig -n telephony`:
   - `patchbay status`: `desk` should leave `Unseen` within a minute (the
     7945 at 192.168.20.100 already polls TFTP on .240). Expected:
     **NeedsTrustReset** (it still holds the old CUCM trust list and asks for
     the signed `.sgn`) or **NeedsFirmware** (SCCP load), with a
     `humanAction` saying so. Either one is a correct, useful result today.
   - `patchbay softphone qr chris-laptop` → scan in Linphone → `Registered`;
     dial **600** (echo), **911** (announcement), **0** (not available),
     **301** (rings the softphone; the desk is not registered yet).
   - Agent observations: the Desk phone's fetches; any :6970 attempts.
7. Later (separate sign-off, near the end of release A): second 7945
   (d0:c7:89:15:eb:b5, now on the LAN at .61) → its switch port to
   `untagged: 20` via `swctl` (switch/README.md) → it appears in `patchbay
   status` and as an `UnknownPhone` Event → add `Phone kitchen` (or its real
   location) in git.
8. When Chris's Cisco zip arrives: fill the manifest digests in patchbay (a
   commit + release), bump the chart here, `patchbay firmware load <zip>`,
   factory-reset the Desk phone (the human action will say so), and run the
   spec §8.6 bench tests.

## 5. What to watch, and rollback

- **Rollback:** remove both apps from `cluster/applications/kustomization.yaml`
  and push. Argo prunes the workloads. CRDs and the firmware PVC carry
  `helm.sh/resource-policy: keep` and stay; delete them by hand only if
  abandoning patchbay (S4: confirm first). Router: revert the 6970 rule via
  Ansible. Phones are unaffected either way (they keep polling TFTP).
- **Calls depend on `top`.** A Talos upgrade hop (#2506) or the NVMe stalls
  (#2661) on `top` take the PBX down for their duration; accepted in spec §7.
- **Chart or image upgrades recreate the PBX pod** (host ports): in-progress
  calls drop. Renovate will not automerge minor/major (§2).
- **Secrets:** the controller generates every password into Secrets in
  `telephony`; nothing goes through `sign.sh` and nothing is in git.
- **What is NOT changed:** DHCP, the voice VLAN, the switch (until step 7),
  any other namespace.
