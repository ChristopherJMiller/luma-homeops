# Luma 0b: platform prep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare galaxy for Luma phase 1. Reclaim over-requested memory first, then give the household-critical BestEffort pods real requests, so that `top` and `bottom` each keep at least 4 GiB of free request room. Load the stock and Luma alert rules cluster-wide without flooding Discord. Keep `luma-*` logs inside the house, and make the router's syslog receiver actually store what fluent-bit sends.

**Architecture:** Every cluster change edits an existing Argo-managed values block or Rook CR in this repo. The router change goes through `router/ansible` under the commit-confirm parachute. Changes land one change set at a time: commit, sync, watch to a definite outcome, then the next one. Four small scripts are the repeatable tests:
- `scripts/ceph-gate.sh` is the go/no-go gate for every Rook task.
- `scripts/test-fluent-bit-exclusion.sh` runs the real tail input and grep filter against fixture files.
- `router/scripts/verify-syslog-receiver.sh` proves the router stores remote syslog in its own file, and nowhere else.
- `scripts/check-prometheus-rules.sh` proves every PrometheusRule meant for this Prometheus is selected, loaded and evaluating cleanly.

**Tech Stack:** Argo CD (chart argo-cd 10.9.2, v3.5.3); kube-prometheus-stack 79.12.0 (Prometheus v3.8.0, Alertmanager 0.29.0); Rook v1.19.11 with Ceph 19.2.6 (CSI through the ceph-csi-operator); nfs-ganesha 5.9; fluent-bit 2.2.2 (chart 0.47.10); Traefik v3.7.14; MetalLB 0.15.3; trivy-operator chart 0.25.0; ha-helm v0.6.0; Sealed Secrets; VyOS 1.5-rolling-202409240007 with rsyslog, driven by Ansible `vyos.vyos` over `network_cli`.

**Spec:** `docs/superpowers/specs/2026-10-18-luma-design.md`: §2 (starting state), the §12.4 metrics bullet, §13.3, §14 "0b" and the Capacity table. Chris's 0b decisions are recorded in `docs/luma.md` ("Decisions after spec approval").

**Status:** revised 2026-10-19 after two adversarial reviews (Ceph/Rook; alerting/logs) and Chris's decisions. Measurements are read-only: the first set was taken 01:30–01:57 UTC on 10-19, and the revision re-measured 02:30–02:55 UTC.

## Global Constraints

- CLAUDE.md S1–S9 apply to every task. The S2 exceptions this plan uses are:
  - `kubectl rollout restart` and `kubectl delete pod`;
  - one temporary scale to 0 of the Rook-owned `rook-ceph-nfs-satellites-a` Deployment (Task 12; Rook sets it back to 1);
  - throwaway canary objects that a task creates and removes within itself, always in namespace **`luma-0b-canary`**. Never create, label or delete `luma-system`; phase 1 owns it.
  - time-boxed Alertmanager silences (`amtool silence add`) around Rook tasks.
- Spec, verbatim:
  - "Rook mons 4 → 1.5 Gi (they stay system-node-critical)".
  - "Prometheus 4 → 2.5 Gi, gonic".
  - "**OSD requests are left alone** until we know whether `osd_memory_target` follows the request or the limit". We now know; Chris still chose to defer the cut (see "Deferred").
  - "Target: about 8 GiB of net headroom on top of today's, with no node over 85% real use."
  - "These are Rook changes under S4/S7, so Chris approves them in the plan."
- **Exit metric for headroom (supersedes the cluster-wide total):** free memory requests on each node Luma may use. That is `top` and `bottom`, because `middle` is excluded until its memtest.
  - Each must have at least **4096 Mi** free: phase 1's ≈3.3 GiB plus about 0.7 GiB of margin.
  - That also satisfies the phase-1 P3 gate (≥ 2.2 GiB on one node other than `middle`).
  - The cluster-wide net is reported, but it is not the exit.
- Spec, verbatim: "**fluent-bit excludes every `luma-*` namespace** from the Azure and syslog outputs (an input exclude on the namespace path)". The namespaces are `luma-system`, `luma-sandbox` and `luma-browser`. This plan adds a filter that drops Traefik access lines for Luma routers, because they carry OAuth `code=` query strings.
- Spec, verbatim: "Rules are PrometheusRule CRDs in namespace `prometheus` with the selector labels, plus a deploy check that they appear in `/api/v1/rules`." Also: "ServiceMonitors need `release=prometheus`."
- **The Rook-touching tasks are 7, 8, 11 and 12.** Each one needs all of the following:
  - `scripts/ceph-gate.sh` PASS within the 15 minutes before the commit. The gate includes the latency rule (15-minute max OSD commit latency under 1000 ms) and the backup-window rule (no Rook work between 00:30 and 05:00 UTC, that is 17:30–22:00 PDT).
  - Chris's explicit go for that task.
  - No cordoned node, no drain, no memtest, no NVMe swap and no Talos upgrade in progress (S5).
  - A time-boxed silence `namespace="rook"`, covering the task's expected duration plus 30 minutes, added before the commit and expired afterwards. Ceph's own critical alerts are **never** silenced: if one fires, that is the signal.
  - **Tasks 11 and 12 also need Chris's memtest PASS on `middle`.** Chris decided that the memtest runs before them.
- **Rook can restart more than you asked for.** None of these tasks should restart an OSD. But if a CephCluster reconcile does update OSD Deployments, Rook updates them in host-sized batches (`ceph osd ok-to-stop --max`), so every OSD on a host can go down at once.
  - If any `rook-ceph-osd-*` pod restarts during a Rook task, do not interrupt it (S3).
  - Watch the PGs return to `active+clean`, and report to Chris.
- **Only one change set is in flight at a time.** Watch every rollout with the safe-rollout skill or Monitor. Never use a Bash sleep loop.
- **Order: reclaims before additions.**
  - Task 9 adds +2496 Mi on `bottom`. It runs after Tasks 4, 6, 7 and 8 have freed that node; otherwise `bottom` would reach 97.8%.
  - If the Rook gate blocks Tasks 7 and 8 for days, Chris may approve Task 9 after Task 6 alone. `bottom` then lands at about 91% and `top` at about 94%.
- **Requests only.**
  - Reclaims lower `requests.memory` and leave every limit as it is.
  - Additions add no new limit, except the MDS one in Task 11, where the limit sets the MDS cache.
  - Never set request == limit on Plex or SAB. Neither is touched here.
- **Sizing rule for a new request:** round 1.25 × the 10-day max working set up to a multiple of 64 Mi. Where a task departs from this, it states the measured reason (repo-server, immich-ml, Prometheus).
- **Fit check before every scheduling change.** Run `kubectl describe node <n> | grep -A8 'Allocated resources'` on each node, and compare with the "free after" column of the projection table. Stop if any node is more than 512 Mi worse than projected.
- **Measurement hygiene.** Keep per-container range queries to 3 days or less, and run them one at a time.
  - This revision's 7- and 10-day queries pushed Prometheus's working set from 1.3 to 3.4 GiB.
  - That is page cache: RSS stayed at 1.1 GiB. But it is 83% of the 4Gi limit, and it would poison Task 10's sizing.
- **Toolchain:**
  - Run tools with `nix develop -c …` and `KUBECONFIG=/tmp/galaxy-kubeconfig` (refresh the kubeconfig as CLAUDE.md describes).
  - `yq`, `amtool` and `promtool` are not in the dev shell. Use `nix shell nixpkgs#yq-go nixpkgs#prometheus-alertmanager -c …`.
  - `docker` is on rowlett. `amtool` also exists inside the Alertmanager pod; use that one for silences.
- **Git:**
  - Commit straight to `main` with `nix develop -c git commit …`, and pre-commit must pass.
  - Commit signing is currently off: pass no `-S`, and never `--no-gpg-sign` or `--no-verify`.
  - `git add` named paths only. The tree carries Chris's unrelated in-progress telephony and docs edits.
  - Push as `ChristopherJMiller`; check with `gh api user --jq .login`.
  - Use `git diff --no-textconv` for anything that leaves the terminal.
- **Router:**
  - Use the vyos-deploy skill: `site.yml --check --diff` first, then the run with the site-level commit-confirm parachute (`commit_confirm_minutes: 10`).
  - The Ansible run needs Chris's OK at execution time.
  - Never `set` anything by hand on VyOS.
- **Secrets:**
  - Never print a secret value. `alertmanager-config.secret.yaml` and the live `alertmanager-main` Secret hold the Discord webhooks. Decode them only into `umask 077` scratch files, compare them by sha256, and delete the files afterwards.
  - fluent-bit's azure output logs the Log Analytics workspace id on every flush. Grep fluent-bit logs; never paste them into chat, docs or commits.

## Review Focus

1. **A new request fits no allowed node, or lands where it breaks the per-node exit.**
   - A Recreate pod (HA, MDS, gonic, immich-ml) stays down until it schedules. A RollingUpdate pod leaves a Pending surge pod behind.
   - If nfs-ganesha (5 Gi) landed on `top`, `top` would end at 95.7% and fail the exit.
   - Expected: reclaims land first, and placement preferences put NFS on `middle` and the MDS pods on `top` and `bottom`.
   - Test: the fit-check step in Tasks 9, 11 and 12; the per-node "free after" comparison after every task; the Task 13 exit.
2. **Stock alerts flood Discord the moment the rules load.** The critical route pings `@everyone` every 30 minutes.
   - Expected: only Watchdog, InfoInhibitor and `severity=info` alerts fire on day one, and all of them route to `null`.
   - Test: the route matrix in Task 4 (source and deployed config), the `--would-fire` run in Task 5, and the one-hour Discord delta of 0 in Task 5.
3. **Cluster syslog floods the router.**
   - Today's `/var/log/messages` rotates at 256 KiB, roughly every 6 minutes. Remote lines landing there would erase the router's own logs within seconds.
   - The router has one 128 GB SSD.
   - Expected: remote lines go only to `/var/log/cluster/cluster.log`, rotated by size and compressed.
   - Test: `router/scripts/verify-syslog-receiver.sh`, which checks the marker is in `cluster.log` and absent from `messages`, plus a disk-use check 24 hours later (Task 3).
4. **The `luma-*` match catches too much or too little,** including Traefik's access lines for Luma hosts, which carry OAuth codes.
   - Expected dropped: `luma-system`, `luma-sandbox`, `luma-browser` and `luma-0b-canary` files, and Traefik lines naming a `luma-…` router.
   - Expected shipped: namespace `luma`, `notluma-x`, a pod or container named `luma-…` in `default`, and Traefik lines for other routers, including `notluma`.
   - Test: the Task 2 fixtures.
5. **NFS clients during the ganesha restart** (rowlett: NFSv4.1, automount, `hard`).
   - A RollingUpdate would run two ganesha instances with the same `nodeid` against the `rados_cluster` grace DB.
   - Expected: a clean stop, one grace-period stall, no stale handles, the new instance not blocklisted, and its RADOS config watch present.
   - Test: Task 12, Steps 3–5.

---

## Measured baseline (2026-10-19 UTC, read-only)

**Ceph:**
- PG 1.c was repaired 01:16→01:53. At 02:47 the state was HEALTH_WARN, with `BLUESTORE_SPURIOUS_READ_ERRORS` (osd.4, left unmuted on purpose) and the three muted `AUTH_INSECURE_*` checks.
- `MDS_CLIENT_RECALL` (client `rook-satellites`) and `BLUESTORE_SLOW_OP_ALERT` come and go. Both are part of "normal WARN noise".
- MDS: 9 sessions (1 ganesha, 8 kernel CSI). `ceph osd blocklist ls` lists 0 entries. Mon `store.db` is 118 MB.
- **The OSD latency gate is strict on SMR.** Over 7 days, the cluster-wide 15-minute max of `ceph_osd_commit_latency_ms` was under 1000 ms in only **25%** of windows (median 1.13 s; daytime UTC no better).
  - Per HDD OSD, the share of windows over 1 s runs from 14% (osd.4) to 32% (osd.5). The SSD OSDs never exceed it.
  - At 02:47 UTC osd.3 was at 1167 ms, so the gate failed.
  - Expect to wait hours for a window. Arm Monitor on the gate and never loosen it without Chris.
- Argo: everything is Synced/Healthy except `rook` and `rook-cluster`, which are Degraded (long-standing, #2521).

**Memory requests.** Allocatable is 30830 Mi per node, 90.3 GiB in total. These numbers were unchanged between 01:56 and 02:40.

| node | requests | % | free | real use now | 7-day max |
|---|---|---|---|---|---|
| top | 27959 Mi | 90.7% | 2871 Mi | 74% | 76.0% |
| middle | 28250 Mi | 91.6% | 2580 Mi | 47% | 46.7% |
| bottom | 27669 Mi | 89.7% | 3161 Mi | 66% | 57.4% |
| **total** | **83878 Mi** | | **8612 Mi (8.4 GiB)** | | |

- Placement today:
  - `top`: mon-d, osd-1/2/5, Prometheus, HA, MDS-b, nfs-ganesha, two CSI ctrlplugins.
  - `middle`: mon-v, mgr-a (standby), osd-4/6/7, gonic, the media stack.
  - `bottom`: mon-u, mgr-b (active), osd-0/3/8, MDS-a, Grafana, trivy, the MetalLB controller, both Argo repo-servers, immich-ml, two CSI ctrlplugins.
- The single-node-loss deficit, by the stock recording rule (Pending and Running pods only), is 21.7 GiB. That matches spec §2.
  - The earlier "28.4 GiB" figure summed raw `kube_pod_container_resource_requests`, which includes finished pods.

**Findings that shape the plan:**
- **`osd_memory_target` follows the request, capped at 0.8 × the limit.**
  - Source: Ceph 19.2.6 `parse_env`. osd.1 runs at its 1536 Mi request; osd.0 runs at 0.8 × 4 Gi.
  - So an HDD OSD request cut to 3328 Mi would be byte-identical. Chris deferred it anyway (see "Deferred").
- **nfs-ganesha's 4.7 GiB is a cache at its default bound, not a leak.**
  - MDCACHE `Entries_HWMark` is 100000, and 100,008 caps were measured.
  - There is no override path in Rook 1.19 with Ganesha 5.9: Rook's `ganesha.conf` already has an `MDCACHE` block, Ganesha declares it `CONFIG_UNIQUE`, and the CephNFS spec has no config field.
  - The 11-day max is 4796 Mi. It is sized at 5 Gi.
- **Rook NFS facts** (`pkg/operator/ceph/nfs`, v1.19.11):
  - The Deployment is RollingUpdate with maxSurge 1 and maxUnavailable 0, so old and new ganesha overlap, both with `nodeid = satellites.a` under `RecoveryBackend = rados_cluster`.
  - Rook sets `replicas: 1` on every reconcile, but its watch predicate ignores Deployment updates. A manual scale to 0 therefore sticks until the CephNFS CR changes.
  - `spec.server.priorityClassName` and `placement` exist. Today the pod has no priority class (priority 0) and no affinity.
- **Rook MDS facts** (`file/mds/config.go`):
  - `mds_cache_memory_limit` = limit × `cacheMemoryLimitFactor` (default 0.5), or request × `cacheMemoryRequestFactor` when there is no limit.
  - Rook writes it to the **mon config store** (`monStore.Set`), so a revert does not remove it.
  - Today the key is absent from `ceph config dump`, and `ceph config show mds.fs-pool-{a,b}` gives 4294967296 (the default). The MDS Deployments are Recreate, with no priority class.
- **CSI:**
  - Requests come from the rook-ceph chart's `csi.csi{RBD,CephFS}{Provisioner,Plugin}Resource` strings, flow through `rook-ceph-operator-config`, and land in the `drivers.csi.ceph.io` CRs `rook.rbd.csi.ceph.com` and `rook.cephfs.csi.ceph.com`.
  - `kubectl get csinode` lists both drivers on all 3 nodes.
  - Per-container 7-day max: plugin 63 Mi, sidecars 51 Mi or less, registrar 22 Mi. Requests are 512, 128 and 128 Mi.
- **Argo repo-server:**
  - 2 pods, both on `bottom`. Request = limit = 1 Gi, and the `copyutil` init container inherits the same `repoServer.resources`, because chart 10.9.2 defaults `repoServer.copyutil.resources` to it.
  - An HPA (1–2, memory 15% of 50%) has sat at max for two years. The PDB is `minAvailable: 1`.
  - Working set: 7-day p95 160–239 Mi; 10-day max 395–397 Mi (manifest-render spikes).
- **immich-machine-learning:**
  - Request 1 Gi, limit 3 Gi, Recreate.
  - Idle at 274 Mi (7-day p50 = p95 = 274 Mi). It peaked at 1043 and 1169 Mi on 10-13 and 10-14, during the ML batch after the archive import. Models unload after the idle TTL.
- **Prometheus:**
  - RSS was 1.1–1.3 GiB. The daily working-set max was 1.25–1.87 GiB until this revision's long queries took it to 3.4 GiB.
  - Last restart: 10-11 06:06 (`Completed`, not OOMKilled); 26 restarts in its lifetime.
- **trivy:** `operator.resources` in `cluster/applications/trivy-operator.yaml` is dead config, because chart 0.25.0 reads the top-level `resources:` key. Its intended 512 Mi limit would OOM an operator whose 10-day max is 868 Mi.
- **Rules:**
  - 3 custom PrometheusRule objects are selected, loading 6 groups, all health `ok`.
  - **35** stock kps objects (`kubectl get prometheusrules -A -l release=prometheus`) exist but nothing selects them.
  - `music-streaming/gonic-alerts` carries `app=gonic` only.
  - Loaded rule files are named `<ns>-<name>-<metadata.uid>.yaml`.
- **Day-one alerts** (stock expressions evaluated live, ignoring `for:`):
  - TargetDown would fire for `kube-scheduler`, `kube-controller-manager` and `kube-proxy` (0/3 each; Talos binds them to 127.0.0.1) and for satellites `.243` and `.244`, which are intentionally off.
  - Watchdog fires always, by design.
  - KubeHpaMaxedOut would fire (repo-server).
  - CPUThrottlingHigh (info) would fire (trivy jobs).
  - KubeMemoryOvercommit would fire permanently, by design.
  - KubeAPIErrorBudgetBurn would not (about 0.0003 against 0.01).
- **etcd is unmonitored.** Service `kube-system/prometheus-kube-prometheus-kube-etcd` has no endpoints, because Talos runs etcd as a system service, not a pod. The stock etcd rules therefore evaluate to nothing.
- **Alertmanager:**
  - The weekday window `09:00–22:00` has no `location`, so it is UTC (02:00–15:00 PDT).
  - Severity `none` and `info` fall through to `homelab-default`.
  - If the `alertmanager-main` Secret ever goes missing, the operator falls back to a generated default config whose root route is `null`. That is **silent loss**.
  - Notification metrics in 0.29 carry only an `integration` label (all three receivers are Discord), so delivery is counted per integration, not per receiver.
  - Stock kps ships no Ceph rules; the only Ceph rules are in the dead ConfigMap.
- **Router syslog is a black hole.**
  - VyOS 1.5-rolling-202409240007 has no CLI for an rsyslog input. There is no `imudp` and no UDP 514 listener.
  - `set system syslog host 0.0.0.0` is a *forwarding target*: it renders `*.info @0.0.0.0:514`.
  - `set system syslog file kubernetes-logs` renders a Python dict as the omfile target, because of a template bug: `:omfile:${{ file }}` uses the dict, not the file name.
  - fluent-bit's syslog output has no `Syslog_Message_Key`, so even a working receiver would get empty message bodies.
  - Volume: about 1 GB/day of container log input across the 3 nodes. `syslog.0` has logged about 430k retries per node since 10-05.
  - The firewall input rule 100 already accepts UDP 514 from `NET-INSIDE-v4` (192.168.0.0/24).
  - The router disk is a 128 GB TLC SSD with 103 GB free.
- **Traefik v3.7.14 access logs are CLF.** Each line holds the full request URI and the router name, for example `"websecure-<ns>-<ingress>-<host>@kubernetes"`. They are shipped to Azure today, including OAuth `code=` query strings.
- **fluent-bit is not scraped by Prometheus** (no monitor, no annotation). Its metrics are readable only through `kubectl get --raw /api/v1/namespaces/logging/pods/<pod>:2020/proxy/api/v1/metrics/prometheus`.
- **Backup window:** CronJobs run from 00:30 to about 05:00 UTC. `family-backup` at 03:30 and `archive-backup` at 04:00 walk the CephFS trees.

### Sizing (before → after)

| component (count) | file and key | request before → after | limit | measured (Mi) | Δ total | task |
|---|---|---|---|---|---|---|
| Argo repo-server (2), incl. `copyutil` init | `cluster/argo-cd/values.yaml` `argo-cd.repoServer.resources` | 1Gi → 256Mi | 1Gi | p95 ≤239, max 397 | −1536 | 4 |
| immich-machine-learning | `cluster/applications/immich-release.yaml` `machine-learning…containers.main.resources` | 1Gi → 384Mi | 3Gi | idle 274, batch 1169 | −640 | 6 |
| gonic | `cluster/music-streaming/deployment.yaml` | 2Gi → 256Mi | 2Gi | max 103 | −1792 | 6 |
| mons (3) | `cluster/rook-cluster/cluster.yaml` `spec.resources.mon` | 4Gi → 1536Mi | 4Gi | max 663 | −7680 | 7 |
| mgr (2) | same file, `spec.resources.mgr` | 2Gi → 1408Mi | 2Gi | max 1102 (active) | −1280 | 7 |
| CSI ctrlplugin (4) | `cluster/applications/rook.yaml` `csi.csi{RBD,CephFS}ProvisionerResource` | 1024 → 384 per pod | unchanged | ≤216 per pod | −2560 | 8 |
| CSI nodeplugin (6) | same file, `csi.csi{RBD,CephFS}PluginResource` | 640 → 160 per pod | unchanged | ≤84 per pod | −2880 | 8 |
| MetalLB controller | `cluster/metallb/release.yaml` `controller.resources` | 0 → 128Mi | none | 86 | +128 | 9 |
| MetalLB speaker (3) | same file, `speaker.resources` | 0 → 64Mi | none | 36 | +192 | 9 |
| Grafana | `cluster/applications/prometheus-stack.yaml` `grafana.resources` | 0 → 448Mi | none | 349 | +448 | 9 |
| Grafana sidecars (2) | same file, `grafana.sidecar.resources` | 0 → 384Mi | none | 297, 275 | +768 | 9 |
| trivy-operator | `cluster/applications/trivy-operator.yaml`, top-level `resources` | 0 → 1088Mi | none | 868 | +1088 | 9 |
| Home Assistant | `cluster/applications/home-assistant-release.yaml` `resources` | 0 → 1Gi | none | 768 | +1024 | 9 |
| Prometheus | `cluster/applications/prometheus-stack.yaml` `prometheus.prometheusSpec.resources` | 4Gi → measured (est. 2560Mi) | 4Gi | RSS ≤1.3 GiB | ≈ −1536 | 10 |
| MDS (2) | `cluster/rook-cluster/fs-pool.yaml` `spec.metadataServer` | 0 → 1664Mi | 4Gi (cache 2 GiB) | max 1312 | +3328 | 11 |
| nfs-ganesha | `cluster/rook-cluster/ceph-nfs.yaml` `spec.server` | 0 → 5Gi | none | max 4796 | +5120 | 12 |

- Additions total +12096 Mi; reclaims total −19904 Mi.
- **The net is −7808 Mi: 7.6 GiB more free, so cluster headroom goes from 8.4 to 16.0 GiB.** That meets the spec's "about 8 GiB".
- Mons are kept at the spec's 1536Mi rather than 2 Gi.
  - The max over 10.6 days, which includes three PG repairs, is 663 Mi.
  - The store is 118 MB, so `mon_memory_target` (2 GiB, a cache-autotune ceiling) cannot fill.
  - The daemons keep `oom_score_adj −997`.
  - 2 Gi would cost 1.5 GiB of reclaim and leave the exit intact (top/bottom 5943/6105 Mi free).

### Projection: free requests (Mi) after each task, in order

| after task | top | middle | bottom |
|---|---|---|---|
| baseline | 2871 | 2580 | 3161 |
| 4 (repo-server) | 2871 | 2580 | 4697 |
| 6 (immich-ml, gonic) | 2871 | 4372 | 5337 |
| 7 (mons, mgr) | 5431 | 7572 | 8537 |
| 8 (CSI) | 7671 | 8532 | 10777 |
| 9 (MetalLB, Grafana, trivy, HA on top) | 6583 | 8468 | 8281 |
| 10 (Prometheus at 2560Mi) | 8119 | 8468 | 8281 |
| memtest on `middle` (Chris) | — | — | — |
| 11 (MDS-b on top, MDS-a on bottom) | 6455 | 8468 | 6617 |
| 12 (NFS on middle) | **6455 (79.1%)** | **3348 (89.1%)** | **6617 (78.5%)** |

- NFS's placement decides the exit. Every other placement fails it:
  - on `top`: 1335 Mi free (95.7%);
  - on `bottom`: 1497 Mi free (95.1%).
- This is why Task 12 prefers `middle` once the memtest passes.
- It also moves 4.7 GiB of real use off `top` (74% now, with the worn NVMe) onto `middle` (47% now).

## Sequence at a glance

| task | touches | Chris "go" | user-visible blip |
|---|---|---|---|
| 1 Gate script and baseline | read-only | — | — |
| 2 fluent-bit `luma-*` exclusion and Traefik filter | logging | sign-off, plus the canary-namespace delete | — |
| 3 Router syslog receiver | **router** (Ansible), logging | **yes** | none |
| 4 Alert-noise pre-work and repo-server | Alertmanager, kps, argo-cd (prunes kube-system objects) | **yes** | — |
| 5 Rule loading and platform rules | Prometheus | sign-off | — |
| 6 immich-ml and gonic reclaim | no | sign-off | ML pauses briefly; music about 1 min |
| 7 mon and mgr requests | **Rook** | **yes** | — |
| 8 CSI requests | **Rook** | **yes** | — |
| 9 Requests for non-Rook BestEffort pods | no | Chris picks HA's window | HA 1–2 min |
| 10 Prometheus request | no | sign-off | Prometheus WAL replay |
| — memtest86+ on `middle` | Chris, maintenance window | — | drain of `middle` |
| 11 MDS requests, priority, placement | **Rook** | **yes** | CephFS failover: seconds |
| 12 nfs-ganesha request, priority, placement | **Rook** | **yes** | NFS stall: minutes |
| 13 Exit verification and records | read-only | — | — |

---

### Task 1: Gate script and baseline

**Files:**
- Create `scripts/ceph-gate.sh`.

**Interfaces:**
- Produces `scripts/ceph-gate.sh [--health-only]`.
  - No other arguments. `KUBECONFIG` defaults to `/tmp/galaxy-kubeconfig`.
  - It prints exactly one line: `GATE PASS`, or `GATE FAIL: <reason>[, <reason>…]`. Exit code 0 means pass.
  - `--health-only` skips the latency and clock rules. Task 13 and the non-Rook tasks use that mode.
  - It is safe to run from Monitor every 5 minutes.
  - `CEPH_GATE_MAX_LATENCY_MS` (default 1000) exists only to prove the failing path. The script rejects any value above 1000: loosening the gate is Chris's call, never an env var.
- Its PromQL goes through `kubectl exec` into the Prometheus pod with `wget`, in the same way as `scripts/check-dashboards.sh`.

- [ ] **Step 1: Write the script.** It checks these rules, in this order, and reports every failing one:
  1. `ceph health detail -f json` check names, minus the allowed set, is empty. The allowed set is the three `AUTH_INSECURE_*`, `BLUESTORE_SPURIOUS_READ_ERRORS`, `MDS_CLIENT_RECALL` and `BLUESTORE_SLOW_OP_ALERT`. `SLOW_OPS` is deliberately not allowed.
  2. Every PG is `active+clean`; `+scrubbing` or `+deep` is allowed.
  3. 3 mons are in quorum.
  4. `9 osds: 9 up … 9 in`.
  5. No node is `SchedulingDisabled`.
  6. (Skipped with `--health-only`.) `max(max_over_time(ceph_osd_commit_latency_ms[15m])) < 1000`.
  7. (Skipped with `--health-only`.) The current UTC time is outside [00:30, 05:00).

- [ ] **Step 2: Run it now.**
  - Record the result: PASS is rare. At 02:47 UTC on 10-19 the gate failed both the latency rule (osd.3 at 1167 ms) and the backup-window rule.
  - `CEPH_GATE_MAX_LATENCY_MS=1 scripts/ceph-gate.sh` must FAIL with `latency`. This proves the failing path at any hour.
  - `CEPH_GATE_MAX_LATENCY_MS=5000 scripts/ceph-gate.sh` must exit non-zero with a refusal.
  - `--health-only` must PASS.

- [ ] **Step 3: Maintenance state.**
  - Argo shows nothing unhealthy beyond `rook` and `rook-cluster`.
  - Ask Chris once (AskUserQuestion): when is middle's memtest window? Record the date in Status. Tasks 11 and 12 wait for its verdict.

- [ ] **Step 4: Re-measure.**
  - Record per-node `Allocated resources` and `kubectl top nodes`.
  - If any node's requests moved by more than 512 Mi from the baseline table, redo the projection table before going on.

- [ ] **Step 5: Commit.**

```bash
git add scripts/ceph-gate.sh
nix develop -c git commit -m "scripts: ceph-gate.sh, the go/no-go for Rook changes (Luma 0b)"
git push origin main
```

### Task 2: fluent-bit drops `luma-*` namespaces at the input, and Luma's Traefik lines

**Files:**
- Create `scripts/test-fluent-bit-exclusion.sh`.
- Modify `cluster/applications/fluent-bit.yaml`:
  - the `Name tail` `[INPUT]`;
  - a new `[FILTER] Name grep`, placed **first** in `valuesObject.config.filters`, ahead of the kubernetes filter.

**Interfaces:**
- Produces `nix shell nixpkgs#yq-go -c scripts/test-fluent-bit-exclusion.sh`, with exit code 0 = all cases pass. It needs `docker` and is not a pre-commit hook. Task 13 re-runs it.

- [ ] **Step 1: Write the test.** The script:
  1. Extracts the `Name tail` `[INPUT]` section and every `Name grep` `[FILTER]` section from the Application.
  2. Writes a config of `[SERVICE] Flush 1`, those sections and `[OUTPUT] Name stdout, Match *`. Two test-only overrides go into the copy: `Read_From_Head On`, and `DB` pointed into the temp dir.
  3. Runs `docker run --rm fluent/fluent-bit:<valuesObject.image.tag>` for about 15 s. A temp dir is mounted at `/var/log/containers`, holding CRI-format lines (`<RFC3339> stdout F <payload>`).
  4. Prints PASS or FAIL per case, and exits 1 on any FAIL.

  | fixture file (`<id>` = 64 hex) | payload | expected |
  |---|---|---|
  | `app-1_default_app-<id>.log` | marker | shipped |
  | `luma-core-1_default_core-<id>.log` | marker | shipped (pod name `luma-…`) |
  | `web-1_default_luma-agent-<id>.log` | marker | shipped (container name `luma-…`) |
  | `x-1_luma_c-<id>.log` | marker | shipped (namespace exactly `luma`) |
  | `x-1_notluma-x_c-<id>.log` | marker | shipped |
  | `core-1_luma-system_core-<id>.log` | marker | **dropped** |
  | `sb-1_luma-sandbox_shell-<id>.log` | marker | **dropped** |
  | `br-1_luma-browser_chrome-<id>.log` | marker | **dropped** |
  | `c-1_luma-0b-canary_c-<id>.log` | marker | **dropped** |
  | `traefik-1_traefik_traefik-<id>.log` | 5 CLF lines: routers `websecure-luma-system-luma-luma-chrismiller-xyz@kubernetes`, `web-luma-system-luma-luma-chrismiller-xyz@kubernetes`, `luma-system-share@kubernetescrd`, `websecure-media-mm-plex-plex-chrismiller-xyz@kubernetes`, `websecure-notluma-x-a-b@kubernetes` | first 3 **dropped**, last 2 shipped |

  - Each line's marker is unique: the file stem, plus the line number for Traefik.
  - A case passes when its marker is present in stdout, or absent for a dropped case.

- [ ] **Step 2: Run it.** Expected: FAIL on the 4 dropped namespace cases and the 3 dropped Traefik lines, because nothing is excluded today.

- [ ] **Step 3: Add to the tail INPUT.** The line is `Exclude_Path /var/log/containers/*_luma-*_*.log`, with a comment saying:
  - CRI log names are `<pod>_<namespace>_<container>-<id>.log`;
  - pod, namespace and container names cannot contain `_`, so `_luma-…_` can only be the namespace;
  - the exclusion is at the input, so neither the azure output nor the syslog output ever sees these files;
  - `K8S-Logging.Exclude Off` stays, so a workload can never opt itself back in or out.

- [ ] **Step 4: Add the grep FILTER.**
  - `Name grep`, `Match kube.var.log.containers.traefik-*_traefik_*`, `Exclude log "(web-|websecure-)?luma-[a-z0-9-]+@kubernetes(crd)?"`.
  - Comment:
    - Traefik's CLF line names the router `<entrypoint>-<namespace>-<ingress>-<host>@kubernetes` (or `<ns>-<name>@kubernetescrd`), and the URI carries OAuth `code=`;
    - this drops Luma's request lines from both outputs, while `kubectl logs` keeps them;
    - the leading quote anchors the namespace, so `notluma` is kept.

- [ ] **Step 5: Run it again.** Expected: all 14 checks PASS (9 file cases and 5 Traefik lines).

- [ ] **Step 6: Commit and watch.**

```bash
git add scripts/test-fluent-bit-exclusion.sh cluster/applications/fluent-bit.yaml
nix develop -c git commit -m "logging: fluent-bit drops luma-* namespaces at the input and Luma's Traefik lines (Luma 0b)"
git push origin main
```

  - Argo `fluent-bit` goes Synced/Healthy and the DaemonSet rolls 3/3; the new pods are young. Use Monitor.
  - The live ConfigMap contains the `Exclude_Path` line and the grep filter.

- [ ] **Step 7: Live check with throwaway canaries.**
  1. Create namespace `luma-0b-canary`, labelled `purpose=luma-0b-canary`. If it already exists, stop and ask.
  2. Start two `busybox:1.36.1` pods, both with `nodeName: bottom`, that echo `MARKER-<uuid>-<namespace>` every 5 s:
     - `fb-canary` in `luma-0b-canary`;
     - `fb-canary-control` in `default`.
  3. Both must be `Running`, and each marker must show in its own `kubectl logs`. This proves the pods really log.
  4. After 2 minutes, on the fluent-bit pod on `bottom`, take `kubectl -n logging logs <pod> --since=5m | grep inotify_fs_add`.
     - At least 1 line must name `fb-canary-control_default_`.
     - 0 lines may name `_luma-0b-canary_`.
  5. **Chris runs the Azure check** (it needs `az login`).
     - After 20 minutes (ingestion lag), read `WS` from Secret `logging/azure-log-analytics`, key `workspace-id`, into a variable without echoing it.
     - For each marker, run `nix develop -c az monitor log-analytics query -w "$WS" --analytics-query "luma_homelab_logs_CL | where TimeGenerated > ago(1h) | where * has '<marker>' | count" -o tsv`.
     - Expected: the control marker counts more than 0; the canary marker counts 0.
     - If the query fails because of the Data Collector API retirement (see Follow-ups), record that and continue. The inotify result is the gate.
  6. The router check for these same canaries is in Task 3.

- [ ] **Step 8: Clean up.**
  - Delete both pods, then namespace `luma-0b-canary`. This is S4: Chris pre-approves this one deletion when he signs off the plan. Re-confirm first that only the canary was ever in it.
  - Record the result in Status.
  - **Rollback:** revert the commit.

### Task 3: Router syslog receiver (Ansible) and fluent-bit's message key — **Chris approval**

**Files:**
- Create `router/ansible/templates/rsyslog-cluster-receiver.conf.j2` and `router/ansible/templates/cluster-syslog.logrotate.j2`.
- Modify `router/ansible/playbooks/monitoring.yml`: replace the "Configure syslog to receive remote logs" task.
- Modify `router/ansible/group_vars/vyos_routers.yml`: the `monitoring.syslog` block.
- Modify `router/scripts/vyos-postconfig-bootup.script`: the boot-time re-install, and the listener check in the LAN-recovery loop.
- Create `router/scripts/verify-syslog-receiver.sh`.
- Modify `cluster/applications/fluent-bit.yaml`: the syslog `[OUTPUT]`.

**Interfaces:**
- Consumes the Task 2 exclusion, which must be live before the router stores anything.
- Produces `router/scripts/verify-syslog-receiver.sh [marker]`, run from rowlett.
  - It sends one RFC3164 UDP line, `logger --udp -n 192.168.0.1 -P 514 -t luma0b-syslog-test <marker>` (a uuid marker if none is given), then over SSH counts the marker in `/var/log/cluster/cluster.log` and in `/var/log/messages`.
  - PASS = 1 and 0, plus a `192.168.0.1:514` UDP listener in `ss -ulnp`. Exit code 0 = pass.
  - Task 13 re-runs it.
- Produces the variables `monitoring.syslog.{receiver_enabled: true, listen_address: 192.168.0.1, listen_port: 514, log_path: /var/log/cluster/cluster.log, rotate_size: 100M, rotate_count: 200}`. They replace `log_file`, `archive_size` and `archive_files`.

- [ ] **Step 1: Run the verifier.** Expected: FAIL, with no listener and a count of 0.

- [ ] **Step 2: Write the templates.**
  - The rsyslog drop-in loads `imudp`, and its `input()` is bound to `listen_address:listen_port` with `ruleset="cluster"`.
  - That ruleset holds one `omfile` action to `log_path`, with `rotation.sizeLimit` = `rotate_size` and `rotation.sizeLimitCommand` running `logrotate` on the cluster file. Then it calls `stop`, so remote lines never reach `/var/log/messages` (Review Focus 3).
  - The logrotate file covers `log_path` with `size {{ rotate_size }}`, `rotate {{ rotate_count }}`, `compress`, `delaycompress`, `missingok` and `create 0640 root adm`.
    - `size` must be present. Otherwise logrotate, when rsyslog invokes it at the size limit, finds no due condition, and rsyslog keeps re-invoking it.
  - Header comment, in both files:
    - VyOS 1.5-rolling-202409240007 has no CLI for an rsyslog input;
    - this file is installed by Ansible and re-installed at boot by the postconfig script;
    - never edit it on the router.

- [ ] **Step 3: Rewrite the monitoring task.**
  - **`vyos_config`:** `delete system syslog host 0.0.0.0` and `delete system syslog file kubernetes-logs`. The first is a forward target, not a listener; the second renders a dict. Remove the old `set` lines for both.
  - **`vyos_command`, guarded by a sha256 compare:** the commands run only when the rendered template's `hash('sha256')` differs from `sudo sha256sum` of the installed file.
    - `printf %s '<b64>' | base64 -d | sudo tee /config/rsyslog/<name> >/dev/null`, where `<b64>` = `lookup('template', …) | b64encode`. `network_cli` cannot copy files, and `/config` survives image upgrades.
    - `sudo install -D -m 0644` into `/etc/rsyslog.d/10-cluster-receiver.conf` and `/etc/logrotate.d/cluster-syslog`.
    - `sudo install -d -m 0750 -o root -g adm /var/log/cluster`.
    - `sudo rsyslogd -N1`. On a non-zero exit, remove the drop-in from `/etc/rsyslog.d` and fail before any restart.
    - `sudo systemctl restart rsyslog`.
    - Assert that `ss -ulnp` shows `192.168.0.1:514`.
  - **When `receiver_enabled: false`:** remove both installed files, then restart rsyslog. This is the rollback path.
  - **In check mode:** `vyos_command` skips non-show commands, so add a `debug` task that prints both rendered templates when `ansible_check_mode`. Neither holds a secret.
  - Keep the firewall task as it is (rule 100 already accepts UDP 514 from `NET-INSIDE-v4`). **No firewall, NAT or interface line changes.**

- [ ] **Step 4: Postconfig script.**
  - Add a block that re-installs both files from `/config/rsyslog/` when the `/etc` copies are missing or differ.
  - Add `ss -ulnp | grep -q "${LAN_IP}:514" || systemctl restart rsyslog` to the existing LAN-recovery loop. That heals the same cold-boot race as kea and pdns: rsyslog bound before eth2 had its address.
  - Deploy it by the file's own documented procedure (scp, then `sudo install`). Chris's OK covers this one file.

- [ ] **Step 5: Preview.**
  - Run `cd router/ansible && nix develop -c ansible-playbook site.yml --check --diff --tags monitoring`.
  - Expected diff: exactly the two `delete system syslog …` paths, plus the debug output of the two templates.
  - Anything else is drift: stop and report.

- [ ] **Step 6: Apply, with Chris's OK.**
  - Run `nix develop -c ansible-playbook site.yml --tags monitoring`. The site-level parachute arms `commit_confirm 10`, verifies from outside and confirms.
  - A new file appears in `router/ansible/backups/`.

- [ ] **Step 7: Re-run the verifier.** Expected: PASS. Also, `/etc/rsyslog.d/00-vyos.conf` no longer contains `omfile:${` or `@0.0.0.0`.

- [ ] **Step 8: fluent-bit message key.**
  - Add `Syslog_Message_Key log` to the syslog `[OUTPUT]`, with a comment: the record key from the cri/docker parsers is `log`, and without it every message body is empty.
  - Commit together with the router files, and only after Step 7 PASSes. If the parachute rolled the router back, fix the cause and repeat Steps 5–7 before committing, so that the repo never records an undeployed router state. Push, then watch the DaemonSet roll 3/3 (Monitor).

```bash
git add router/ansible/templates/rsyslog-cluster-receiver.conf.j2 router/ansible/templates/cluster-syslog.logrotate.j2 \
  router/ansible/playbooks/monitoring.yml router/ansible/group_vars/vyos_routers.yml \
  router/scripts/vyos-postconfig-bootup.script router/scripts/verify-syslog-receiver.sh \
  cluster/applications/fluent-bit.yaml
nix develop -c git commit -m "router: real rsyslog receiver for cluster logs; fluent-bit sends message bodies (Luma 0b)"
git push origin main
```

- [ ] **Step 9: End-to-end with canaries.**
  - Repeat Task 2 Step 7, items 1–3 (namespace `luma-0b-canary`, the same two pods, `Running`, markers visible in `kubectl logs`).
  - After 2 minutes, over SSH: `grep -c <control-marker> /var/log/cluster/cluster.log` must be more than 0, and `grep -c <canary-marker>` must be 0.
  - The fluent-bit `syslog.0` retry counter must stop climbing: read `fluentbit_output_retries_total{name="syslog.0"}` through the API-server pod proxy twice, 10 minutes apart, on each pod. Delta ≤ 5.
  - Clean up as in Task 2 Step 8.

- [ ] **Step 10: 24 hours later.**
  - `du -sh /var/log/cluster` must be under 3 GB.
  - `ls /var/log/cluster` must show size-rotated, compressed files.
  - `/var/log/messages` must not have rotated faster than before (its own `messages.N` timestamps).
  - Record the daily volume in Status.
  - **Rollback:** set `receiver_enabled: false`, run the playbook (parachute as usual), and revert the fluent-bit line.

### Task 4: Alert-noise pre-work and the repo-server change (lands before any rule loads) — **Chris approval**

**Files:**
- Modify `cluster/prometheus-stack/alertmanager-config.secret.yaml`, and regenerate `cluster/prometheus-stack/alertmanager-config.yaml` from it.
- Modify `cluster/applications/prometheus-stack.yaml`:
  - `kubeControllerManager`, `kubeScheduler`, `kubeProxy`;
  - `defaultRules.disabled`;
  - the `satellites` and `satellites-comin` scrape jobs.
- Modify `cluster/argo-cd/values.yaml`: `argo-cd.repoServer`.

**Interfaces:**
- Produces the receiver `null`, and the route order: `Watchdog|InfoInhibitor` → `null`, then `severity="info"` → `null`, then the existing routes.
- Produces the route matrix below. Task 13 re-runs it against the live Secret.

- [ ] **Step 1: Write the route tests and watch them fail.**
  - Work on a scratch copy: `umask 077; t=$(mktemp -d)`, then `nix shell nixpkgs#yq-go -c yq '.stringData["alertmanager.yaml"]' cluster/prometheus-stack/alertmanager-config.secret.yaml > "$t/am.yaml"`.
  - For each row, run `amtool config routes test --config.file "$t/am.yaml" --verify.receivers=<receiver> <labels>`.

  | labels | receiver |
  |---|---|
  | `alertname=Watchdog severity=none` | `null` |
  | `alertname=InfoInhibitor severity=none` | `null` |
  | `alertname=CPUThrottlingHigh severity=info` | `null` |
  | `alertname=KubePodCrashLooping severity=warning` | `homelab-default` |
  | `alertname=KubePersistentVolumeFillingUp severity=warning` | `storage-alerts` |
  | `alertname=KubeAPIDown severity=critical` | `critical-alerts` |
  | `alertname=CephOSDDown severity=critical` | `critical-alerts` |
  | `alertname=CephStorageCapacityCritical severity=critical` | `critical-alerts` |
  | `alertname=NodeMemoryRequestsHigh severity=warning` | `homelab-default` |
  | `alertname=ControlPlanePodNotReady severity=warning` | `homelab-default` |

  - Expected now: rows 1–3 FAIL (they get `homelab-default`), and the rest PASS.

- [ ] **Step 2: Edit the Alertmanager config.**
  - Add a receiver `- name: 'null'` with no configs.
  - Prepend the two routes.
  - Add `location: America/Los_Angeles` to the `weekdays` time interval (Chris's decision). Its times stay `09:00–22:00`, Monday to Friday.
  - Change nothing else.

- [ ] **Step 3: Re-run the tests.**
  - Regenerate `$t/am.yaml`. All 10 rows must PASS, and `amtool check-config "$t/am.yaml"` must succeed.
  - `sha256sum` it (after `sed -e '$a\'`, which normalises the trailing newline) and keep the hash as `$SRC_SHA`.
  - `rm -r "$t"`.

- [ ] **Step 4: Reseal, with assertions.**
  - Run `rm cluster/prometheus-stack/alertmanager-config.yaml && nix develop -c ./sign.sh`. Its exit code must be 0.
  - The sealed file must be non-empty, `yq .kind` must be `SealedSecret`, and `yq '.spec.encryptedData | keys'` must be exactly `["alertmanager.yaml"]`.
  - `git status --short cluster/prometheus-stack/` must show exactly two lines, both ` M`: `alertmanager-config.secret.yaml` and `alertmanager-config.yaml`. Not `D`, not `??`.

- [ ] **Step 5: kps values.**
  - Add `kubeControllerManager: {enabled: false}`, `kubeScheduler: {enabled: false}` and `kubeProxy: {enabled: false}`, with this comment:
    > Talos binds these to 127.0.0.1. All 9 targets have always been down. Scraping them needs machine-config `bind-address` changes (S4). Readiness is covered by `control-plane-alerts`.
  - Add `defaultRules.disabled: {KubeMemoryOvercommit: true}`, with this comment:
    > By design, two nodes cannot hold three nodes' requests (spec §14: Luma is shed during drains). Replaced by `ClusterMemoryDrainDeficitHigh` and `NodeMemoryRequestsHigh` in `rules/capacity-alerts.yaml`.
  - On both satellite jobs, add a `relabel_configs` entry: `source_labels: [__address__]`, `regex: '192\.168\.0\.24[34]:\d+'`, `action: drop`. Comment: octoprint and printer are intentionally powered off (Chris, 2026-10-18); delete this rule when they return. The target lists stay as the record.
  - Leave `kubeEtcd` as it is. Its Service has no endpoints, so etcd is unmonitored, and that is Follow-up 1.

- [ ] **Step 6: Argo diff review (S4, kube-system).**
  - Auto-sync leaves no window to read a live diff, so render both sides before committing:
    - extract `valuesObject` before (`git show HEAD:…`) and after with yq;
    - run `helm template prometheus kube-prometheus-stack --repo https://prometheus-community.github.io/helm-charts --version 79.12.0 -f <values>` for each;
    - diff the sorted `kind namespace/name` lists.
  - Confirm that the toggles remove **only** these:
    - 3 Services in `kube-system` (`prometheus-kube-prometheus-{kube-controller-manager,kube-scheduler,kube-proxy}`);
    - their 3 ServiceMonitors;
    - 3 Grafana dashboard ConfigMaps (`prometheus-kube-prometheus-{controller-manager,scheduler,proxy}`);
    - 4 PrometheusRule objects (`kube-scheduler.rules`, `kubernetes-system-controller-manager`, `kubernetes-system-kube-proxy`, `kubernetes-system-scheduler`).
  - Chris's go for this task covers the kube-system deletions.

- [ ] **Step 7: Argo repo-server.**
  - Set `argo-cd.repoServer.autoscaling.enabled: false`, `argo-cd.repoServer.replicas: 2` and `resources.requests.memory: 256Mi`. The limit stays 1Gi.
  - Comment:
    - never 1 replica, because `minAvailable: 1` would block every drain;
    - `copyutil` inherits this block, so the pod's effective request drops too;
    - 256Mi is Chris's call: p95 ≤239 Mi, with spikes to 397 Mi during manifest renders, which the 1Gi limit covers.
  - This removes `KubeHpaMaxedOut`, and it is the first reclaim (−1536 Mi on `bottom`).

- [ ] **Step 8: Commit.** There are two commits: the alertmanager pair plus `prometheus-stack.yaml`, then `cluster/argo-cd/values.yaml` on its own. Push.

- [ ] **Step 9: Verify the Alertmanager config end to end.**
  - The SealedSecret `prometheus/alertmanager-main` shows `Synced=True`.
  - Secret `alertmanager-main` exists. Its `alertmanager.yaml`, decoded into a `umask 077` scratch file and normalised the same way, has sha256 == `$SRC_SHA`. A missing Secret means silent loss, so check this before anything else.
  - The Alertmanager CR shows `Reconciled=True`.
  - Decode the operator's `alertmanager-prometheus-kube-prometheus-alertmanager-generated` (`alertmanager.yaml.gz`, gunzip) into scratch, and run the 10-row matrix against **that** file. It is the config the pod loads.
  - `alertmanager_config_last_reload_successful == 1`, and `alertmanager_config_hash` has changed.
  - The deployed config contains `location: America/Los_Angeles`. Check it quote-tolerantly, for example `grep -cE "location: ['\"]?America/Los_Angeles['\"]?"`, printing counts only.
  - Delete the scratch files.

- [ ] **Step 10: Verify the rest.**
  - Argo apps `prometheus-stack-config`, `kube-prometheus-stack` and `argo-cd` are Synced/Healthy.
  - `/api/v1/targets` has no `kube-controller-manager`, `kube-scheduler`, `kube-proxy` or `satellites*` job. `up{job=~"satellites.*"}` is empty.
  - The HPA is gone. `argo-install-argocd-repo-server` is at 2/2 Ready, and both the main and the `copyutil` containers request 256Mi.
  - The PDB shows `ALLOWED DISRUPTIONS 1`.
  - A hard refresh of the biggest chart renders: `kubectl -n argo-cd annotate application kube-prometheus-stack argocd.argoproj.io/refresh=hard --overwrite`. Argo clears the annotation itself; the app returns to Synced. After 1 hour there are 0 repo-server restarts.
  - **Rollback:** revert. The revert restores both the secret file and the sealed file.

### Task 5: Stock and namespace-scoped rules load, plus the platform rules

**Files:**
- Create `scripts/check-prometheus-rules.sh`.
- Create these rule files in `cluster/prometheus-stack/rules/`:
  - `selftest.yaml`;
  - `ceph-critical-alerts.yaml`;
  - `capacity-alerts.yaml`;
  - `control-plane-alerts.yaml`.
- Modify `cluster/prometheus-stack/rules/kustomization.yaml`: add the four resources, and fix its stale selector comment.
- Modify `cluster/prometheus-stack/rules/media-pipeline-alerts.yaml`: add the label, and fix its header comment.
- Modify `cluster/ipmi-exporter/prometheusrule.yaml` and `cluster/backups/prometheusrule.yaml`: add the label.
- Modify `cluster/applications/prometheus-stack.yaml`: `prometheus.prometheusSpec.ruleSelector` and `ruleNamespaceSelector`.

**Interfaces:**
- Produces `scripts/check-prometheus-rules.sh [--would-fire]`. `KUBECONFIG` defaults to `/tmp/galaxy-kubeconfig`; exit code 0 means pass. Task 10, Task 13 and the phase-1 deploy plan use it as the spec §12.4 deploy check.
- Produces the convention: **every PrometheusRule for this Prometheus carries `release: prometheus`.** It may live in any namespace. Luma's own rules still go in `prometheus`.
- Produces these PrometheusRules, all in `prometheus` and labelled `release: prometheus`:
  - **`platform-selftest`**: group `platform-selftest`, `record: platform:prometheusrule_selftest:loaded`, `expr: vector(1)`.
  - **`ceph-critical-alerts`**: group `ceph-critical`, holding `CephClusterHealthError`, `CephOSDDown`, `CephPGInconsistent` and `CephStorageCapacityCritical`.
    - Expressions, `for:` values and severity (`critical`) are copied from `rook-ceph-alerts.yaml`.
    - Not `CephClusterHealthWarning`: the chronic SMR WARN would fire forever.
  - **`capacity-alerts`**: group `capacity`.
    - `ClusterMemoryDrainDeficitHigh`: `sum(namespace_memory:kube_pod_container_resource_requests:sum) - (sum(kube_node_status_allocatable{resource="memory",job="kube-state-metrics"}) - max(kube_node_status_allocatable{resource="memory",job="kube-state-metrics"})) > 24 * 2^30`, `for: 1h`, warning. Comment: the deficit was 21.7 GiB before 0b and is projected at 14.0 GiB after (phase 4 brings it back to about 24), so this fires only if the cluster ends up worse than before 0b.
    - `NodeMemoryRequestsHigh`: `sum by (node) (cluster:namespace:pod_memory:active:kube_pod_container_resource_requests) / on (node) group_left() max by (node) (kube_node_status_allocatable{resource="memory",job="kube-state-metrics"}) > 0.95`, `for: 30m`, warning.
    - Task 12 adds `NfsGaneshaOverRequest`.
  - **`control-plane-alerts`**: group `control-plane`.
    - `ControlPlanePodNotReady`: `max by (pod) (kube_pod_status_ready{namespace="kube-system",condition="true",pod=~"kube-(scheduler|controller-manager)-.+"}) == 0`, `for: 10m`, warning.
    - `ControlPlanePodMissing`: `count(kube_pod_info{namespace="kube-system",pod=~"kube-scheduler-.+"}) < 3 or count(kube_pod_info{namespace="kube-system",pod=~"kube-controller-manager-.+"}) < 3 or absent(kube_pod_info{namespace="kube-system",pod=~"kube-scheduler-.+"}) or absent(kube_pod_info{namespace="kube-system",pod=~"kube-controller-manager-.+"})`, `for: 10m`, warning.

- [ ] **Step 1: Write the check.** It follows the style of `scripts/check-dashboards.sh` (`kubectl exec` into the Prometheus pod and `wget` the API).
  1. **Selectors.** Read the live Prometheus CR's `.spec.ruleSelector` and `.spec.ruleNamespaceSelector`.
     - Convert `matchLabels` to a `k=v,…` selector string. Exit 2 with a message if `matchExpressions` appears.
     - The namespace set: `ruleNamespaceSelector` null means only `prometheus`; `{}` means every namespace; anything else means `kubectl get ns -l <sel>`.
  2. **Selected** = `kubectl get prometheusrules -A -l <ruleSel>`, restricted to that namespace set.
  3. **Loaded** = the groups in `/api/v1/rules`, keyed by `basename(file)`. A selected rule's expected basename is exactly `<ns>-<name>-<metadata.uid>.yaml`.
  4. **Report:**
     - `FAIL inert <ns>/<name>`: a rule labelled `release=prometheus`, or carrying the legacy pair, that is not selected;
     - `FAIL not-loaded <ns>/<name>`: no file with the exact basename;
     - `FAIL missing-group <ns>/<name>/<group>`: a `spec.groups[].name` that is absent under that file;
     - `FAIL unhealthy <ns>/<name>/<group>/<rule>: <lastError>`: rule health is not `ok`. `unknown` counts, so run the check after one evaluation interval;
     - `FAIL eval-failures <rule_group>`: `increase(prometheus_rule_evaluation_failures_total[10m]) > 0`;
     - `WARN unselected <ns>/<name>`: any other PrometheusRule.
  5. **Summary:** print `selected=<n> groups=<g> alerting=<a> recording=<r>`. Exit 1 on any FAIL, or if `g == 0`.
  6. **`--would-fire`:** additionally run every alerting rule's `expr`, from selected and inert objects alike, as an instant query, and print `WOULD-FIRE <alert> <severity> <series>` for each non-empty result.
     - It ignores `for:`, and it under-reports rules that depend on recording rules which are not loaded yet.

- [ ] **Step 2: Run it.**
  - First record `S = kubectl get prometheusrules -A -l release=prometheus --no-headers | wc -l`. It should be 31 after Task 4 (35 − 4); use whatever it prints.
  - Expected: S `FAIL inert` lines, one `WARN unselected music-streaming/gonic-alerts`, `selected=3 groups=6`, and exit code 1.

- [ ] **Step 3: Add the rule files and relabel.**
  - Add the four new files.
  - Add `release: prometheus` to the three custom rules, keeping their legacy labels.
  - Commit, push, and wait for sync.
  - Expected: still FAIL, now with S + 4 inert rules (the new four included). The 6 custom groups are still loaded and healthy.

- [ ] **Step 4: Would-fire, before the flip.**
  - Run `scripts/check-prometheus-rules.sh --would-fire`.
  - Expected `WOULD-FIRE` alerts: only `Watchdog`, `InfoInhibitor` and `CPUThrottlingHigh`.
  - None of the six platform alerts may appear: the deficit is 21.7 GiB, below 24; the highest node is 91.6%, below 95%; all control-plane pods are Ready; Ceph is at WARN, with 9/9 OSDs up, 0 inconsistent PGs and 19% used.
  - Anything else: stop and report to Chris with the evaluated cause.

- [ ] **Step 5: Flip the selector.**
  - Set `ruleSelector: {matchLabels: {release: prometheus}}` and `ruleNamespaceSelector: {}`, with a comment that points at the convention.
  - Commit and push.
  - Watch `kube-prometheus-stack`. The operator rewrites the rule ConfigMaps and the config-reloader reloads them, with no Prometheus restart. **Allow up to 5 minutes** for the kubelet ConfigMap sync and the first evaluations: run the check under Monitor until it passes or 5 minutes have elapsed.

- [ ] **Step 6: Run the check.**
  - Expected: PASS, with `selected = S + 7` (S stock, 3 custom, 4 new) and `groups = S + 6 + 4`. Record the exact summary.
  - `platform:prometheusrule_selftest:loaded` returns 1.
  - `NodeMemoryRequestsHigh`'s expression without `> 0.95` returns 3 series that match `kubectl describe node` within 1 point.
  - The deficit expression without `> 24 * 2^30` returns about 21.7 GiB.

- [ ] **Step 7: Would-fire, after the flip.** Ten minutes after Step 5, run `--would-fire` again. Only the same three alerts may appear, now with the recording rules loaded.

- [ ] **Step 8: Throwaway checks in `luma-0b-canary`** (the same conventions as Task 2).
  1. Create PrometheusRule `luma0b-canary`, labelled `release: prometheus`, with `record: luma0b:canary:loaded`, `expr: vector(1)`.
     - Expected: within 5 minutes it appears in `/api/v1/rules` under a basename starting `luma-0b-canary-luma0b-canary-`. **This proves the namespace selector.**
  2. Create pod `crashloop-canary`: `busybox:1.36.1`, `command: ["sh","-c","exit 1"]`.
     - Expected: within about 6 minutes `/api/v1/alerts` shows `KubePodCrashLooping` with `state="pending"` and `namespace="luma-0b-canary"`. **This proves stock alerts cover Luma-style namespaces.**
  3. Delete both before the alert's 15-minute `for:` elapses, so that no Discord message is sent.
  4. Delete the namespace (S4, pre-approved, after re-confirming that only the canaries were in it).

- [ ] **Step 9: Rule-evaluation load, one hour after Step 5.**
  - `max(prometheus_rule_group_last_duration_seconds)` must be under 10 s.
  - `sum(increase(prometheus_rule_group_iterations_missed_total[1h]))` must be 0.
  - Record Prometheus's RSS and working set before and after; Task 10 uses them.
  - If the `kube-apiserver-burnrate.rules` group misses evaluations, propose to Chris that `defaultRules.rules.kubeApiserverBurnrate` and `kubeApiserverSlos` go to `false`.

- [ ] **Step 10: One-hour Alertmanager watch (Monitor).**
  - Watch `alertname`, `severity` and `receivers[].name` in `/api/v2/alerts`.
  - Take `alertmanager_notifications_total{integration="discord"}` and `alertmanager_notifications_failed_total` at the start and at the end.
  - Expected:
    - Watchdog, InfoInhibitor and CPUThrottlingHigh go to `null`;
    - nothing goes to any Discord receiver;
    - the Discord notification delta is **0**, and the failed delta is 0.
  - For anything else, report it to Chris with the evaluated cause.
    - If it is noise, a follow-up `defaultRules.disabled` commit goes in only with his OK.
    - If it is real, report it and do not fix it (working agreement).
  - **Rollback:** revert the Step 5 commit. That returns the cluster to 3 selected objects and 6 groups.

### Task 6: immich-ml and gonic reclaim

**Files:**
- `cluster/applications/immich-release.yaml`: the machine-learning `main` container's `requests.memory` goes 1Gi → 384Mi. The limit stays 3Gi.
- `cluster/music-streaming/deployment.yaml`: the gonic container's `requests.memory` goes 2Gi → 256Mi. The limit stays 2Gi.

- [ ] **Step 1: Re-verify immich-ml's real use** (Chris asked for this). Over 3 days:
  - `max_over_time(container_memory_working_set_bytes{namespace="immich",container="main",pod=~"immich-machine-learning.*"}[3d])` and its p95.
  - Expected: p95 about 274 Mi, with a max under 1.2 GiB.
  - If p95 is over 307 Mi (384/1.25), stop and ask Chris.
  - Record the departure from the sizing rule in the comment: idle 274 Mi; batch bursts to 1169 Mi while models are loaded, unloading after the idle TTL; the 3Gi limit covers bursts; immich-ml is the designated "may wait" pod in drains.

- [ ] **Step 2: Commit immich-ml.**
  - Push and watch. It is Recreate: ML jobs queue for about a minute, and models load from the CephFS cache PVC.
  - Verify: Ready, `Burstable`, request 384Mi.
  - Verify `kubectl -n immich exec deploy/immich-machine-learning -- python3 -c "import urllib.request;print(urllib.request.urlopen('http://localhost:3003/ping').read())"` prints `b'pong'`.

- [ ] **Step 3: Commit gonic.**
  - Push and watch. It is Recreate: music stops for about a minute.
  - It must reach Ready, which also proves an RBD mount on `middle`.
  - **Rollback:** revert the commit for the app concerned.

### Task 7: Rook mon, then mgr, requests — **Chris approval**

**Files:** `cluster/rook-cluster/cluster.yaml`.
- First commit: `spec.resources.mon.requests.memory: 1536Mi`. Its `limits.memory: 4Gi` stays.
- Second commit: `spec.resources.mgr.requests.memory: 1408Mi`. Its `limits.memory: 2Gi` stays.

- [ ] **Step 1: Prerequisites.**
  - `scripts/ceph-gate.sh` PASS, and Chris's go.
  - Silence, inside the Alertmanager pod: `amtool --alertmanager.url=http://localhost:9093 silence add namespace=rook --duration=2h --author=claude --comment="Luma 0b Task 7"`. Record the id.

- [ ] **Step 2: The test that holds across the change.**
  - `ceph config show-with-defaults mon.d | grep '^mon_memory_target '` must print `2147483648 … default`, both before and after. Mon targets are not request-derived.
  - Record the before value.

- [ ] **Step 3: Commit the mon change.**
  - Rook rolls `mon-d`, `mon-u` and `mon-v` one at a time and waits for quorum.
  - Watch with Monitor until all three mon pods request `1536Mi`. Expect 5–15 minutes.
  - After each restart, `ceph mon stat` must show all 3 in quorum.
  - No `rook-ceph-osd-*` pod restarts; if one does, apply the Global Constraint.

- [ ] **Step 4: Re-check.** Step 2's value is unchanged, and `--health-only` PASSes.

- [ ] **Step 5: Commit the mgr change.** This can follow without a new gate run, if it is within the same silence.
  - The standby restarts first, then the active fails over.
  - `ceph mgr stat` shows 1 active and 1 standby.
  - The `ceph_health_status` series is present again within 5 minutes (the mgr is scraped through pod annotations).

- [ ] **Step 6: Close out.**
  - `--health-only` PASS. Expire the silence (`amtool silence expire <id>`).
  - **Rollback:** revert. Rook rolls the daemons back the same way.

### Task 8: CSI requests — **Chris approval**

**Files:** `cluster/applications/rook.yaml`, under `valuesObject.csi`: `csiRBDProvisionerResource`, `csiRBDPluginResource`, `csiCephFSProvisionerResource` and `csiCephFSPluginResource`.

- [ ] **Step 1: Prerequisites.**
  - Gate PASS, Chris's go, and a 2-hour `namespace=rook` silence.
  - Record `kubectl get csinode -o json | jq -r '.items[] | "\(.metadata.name): \([.spec.drivers[].name]|sort|join(","))"'`. Expected: all 3 nodes list both `rook.cephfs.csi.ceph.com` and `rook.rbd.csi.ceph.com`.

- [ ] **Step 2: Write the four strings.**
  - Copy each default string **verbatim, with every entry**, from `helm show values --repo https://charts.rook.io/release rook-ceph --version 1.19.11`. That includes `csi-omap-generator` and `liveness-prometheus`.
  - Change only these memory requests:
    - `csi-provisioner`, `csi-resizer`, `csi-attacher`, `csi-snapshotter`: 128Mi → 64Mi;
    - `csi-rbdplugin`, `csi-cephfsplugin`: 512Mi → 128Mi;
    - `driver-registrar`: 128Mi → 32Mi.
  - Limits and CPU stay unchanged.

- [ ] **Step 3: Commit and sync `rook`.**
  - The `rook-ceph-operator-config` CSI keys show the new values.
  - The `drivers.csi.ceph.io` CRs `rook.rbd.csi.ceph.com` and `rook.cephfs.csi.ceph.com` show them within 10 minutes (`.spec.controllerPlugin.resources`, `.spec.nodePlugin.resources`). If they do not, run `kubectl -n rook rollout restart deploy/rook-ceph-operator`, which is the precedent in the SMR memory note.
  - The ctrlplugin Deployments and nodeplugin DaemonSets roll, and the pods request 384Mi and 160Mi.

- [ ] **Step 4: Prove registration and mounts.**
  - The Step 1 csinode listing is identical.
  - CephFS: `kubectl -n immich rollout restart deploy/immich-machine-learning`, which mounts `immich-ml-cache-fs`. It must reach Ready.
  - RBD: `kubectl -n trivy-system delete pod trivy-server-0`. It must reach Ready.

- [ ] **Step 5: Close out.**
  - `--health-only` PASS, and expire the silence.
  - **Rollback:** revert, then restart the operator.

### Task 9: Real requests for the non-Rook BestEffort pods

**Files** (one commit, sync and watch per file, in this order):
1. `cluster/metallb/release.yaml`
2. `cluster/applications/prometheus-stack.yaml` (the `grafana` block)
3. `cluster/applications/trivy-operator.yaml`
4. `cluster/applications/home-assistant-release.yaml`

- [ ] **Step 1: Fit check.**
  - These additions need +1088 Mi on `top` (HA and a speaker), +2496 Mi on `bottom` (Grafana, trivy, the MetalLB controller and a speaker) and +64 Mi on `middle`.
  - Projected free before this task: 7671, 8532 and 10777 Mi (top, middle, bottom). Re-run the check.

- [ ] **Step 2: MetalLB.**
  - Set `controller.resources.requests.memory: 128Mi` and `speaker.resources.requests.memory: 64Mi`.
  - The speakers roll, and L2 is re-announced for `.7`, `.8` and `.230` within seconds.
  - Verify: both pods are QoS `Burstable`, and `curl -sI https://home.chrismiller.xyz` returns 200 from the LAN.

- [ ] **Step 3: Grafana.**
  - Set `grafana.resources.requests.memory: 448Mi` and `grafana.sidecar.resources.requests.memory: 384Mi`. The one sidecar key covers both sidecars.
  - Verify: the pod is `Burstable`, and `https://dashboard.chrismiller.xyz/api/health` reports `"database": "ok"`.

- [ ] **Step 4: trivy-operator.**
  - Delete the dead `operator.resources` block.
  - Add top-level `resources: {requests: {cpu: 100m, memory: 1088Mi}}` with no limit. Comment that chart 0.25.0 reads only the top-level key, and that the old 512Mi limit would OOM the operator (868 Mi max).
  - Verify: the operator pod is `Burstable` and has no restarts after an hour.

- [ ] **Step 5: Home Assistant.**
  - Set `resources: {requests: {memory: 1Gi}}`.
  - Set `affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution: [{weight: 100, preference: {matchExpressions: [{key: kubernetes.io/hostname, operator: In, values: [top]}]}}]`. The spec's capacity plan puts HA on `top`.
  - The strategy is Recreate, so expect 1–2 minutes of outage.
  - Chris picks the window: daytime, with someone home (`known_ha_reload_blips`).
  - Verify with `nix develop -c hactl health` (ha-config skill): no new failures compared with a run just before the change. The pod must be `Burstable` and on `top`.

- [ ] **Step 6: Record** where each pod landed and the new per-node requests, against the projection row.
  - **Rollback:** revert the commit for the app concerned.

### Task 10: Prometheus request

**Files:** `cluster/applications/prometheus-stack.yaml`: `prometheus.prometheusSpec.resources.requests.memory`. Its limit of 4Gi stays.

- [ ] **Step 1: Size it,** no earlier than 72 hours after Task 5 Step 5.
  - Take `R = max_over_time(container_memory_rss{namespace="prometheus",container="prometheus"}[3d])`. Also record the 3-day p99 of the working set.
  - The request is **max(2560Mi, ceil64(1.25 × R))**. Expect 2560Mi.
  - Record this departure from the working-set rule, with its measured reason: Prometheus's working set is mostly page cache from the TSDB blocks that queries touch, which the kernel reclaims before OOM. On 10-19 it was 3.4 GiB of working set against 1.1 GiB of RSS.
  - If `1.25 × R` comes out above 3 GiB, stop and ask Chris. That would mean the rules grew the head far more than expected.

- [ ] **Step 2: Preconditions.**
  - `scripts/ceph-gate.sh` PASS: the latency rule matters here, because the WAL replays from an HDD PVC. The backup-window rule also holds.
  - No Rook task in flight.

- [ ] **Step 3: Commit.**
  - The StatefulSet restarts and replays its WAL, which takes minutes. Watch with Monitor.
  - Watch for **OOMKilled during replay**: `kubectl -n prometheus get pod prometheus-prometheus-kube-prometheus-prometheus-0 -o jsonpath='{.status.containerStatuses[?(@.name=="prometheus")].lastState.terminated.reason}'` must not be `OOMKilled`. Record the replay RSS peak.
  - Verify: Ready, targets up again, and `scripts/check-prometheus-rules.sh` PASSes.
  - **Rollback:** revert.

### Checkpoint: memtest86+ on `middle` (Chris)

- Chris runs it in a maintenance window, following S5 and `known_node_maintenance`:
  - `noout` first;
  - Patroni switchovers before the cordon;
  - drain in batches;
  - pick what waits Pending.
- After Tasks 4–10 the drain strands far less than the 21.7 GiB of the baseline.
- Record the verdict in Status.
  - **PASS:** Tasks 11 and 12 run as written, with no `middle` exclusion for data-path reasons.
  - **FAIL or no verdict:** Tasks 11 and 12 do not run, and the plan is replanned with Chris. A failing DIMM is its own incident, because middle's OSDs are writing through it.
  - If Chris nevertheless wants Task 11 or 12 before a verdict, the `middle` exclusion is `preferred`, weight 100, never `required`, and the per-node exit is then not met (see the projection).

### Task 11: MDS requests, priority and placement — **Chris approval**

**Files:** `cluster/rook-cluster/fs-pool.yaml`, under `spec.metadataServer`:
- `resources: {requests: {memory: 1664Mi}, limits: {memory: 4Gi}}`.
- `cacheMemoryLimitFactor: 0.5`, set explicitly. Rook's default is the same, but a default can change under an upgrade. With the limit, this gives `mds_cache_memory_limit` = 2 GiB; the cache holds 0.89 GB today.
- `priorityClassName: system-cluster-critical`, **in this same commit**. With a request and priority 0, the MDS would become a preemption victim, for example of the standby mgr during a drain.
- `placement`:
  - `podAntiAffinity` preferred, weight 100, on `app=rook-ceph-mds`, topology `kubernetes.io/hostname`, so a and b split.
  - `nodeAffinity` preferred, weight 100, `kubernetes.io/hostname NotIn [middle]`. Comment: this is capacity, not the memtest; `middle` takes nfs-ganesha in Task 12; preferred, so a drain can always re-place it.

- [ ] **Step 1: The tests, before.** Record each of these:
  - `ceph config show mds.fs-pool-a mds_cache_memory_limit` and the same for `mds.fs-pool-b`. Both are `4294967296` today. Also `ceph config get` (the store value; absent today).
  - Session count on the active MDS: `ceph tell mds.<active> session ls | jq length`, with the active name from `ceph fs status fs-pool -f json`. It is 9 today.
  - `ceph osd blocklist ls`: 0 entries today.
  - A timed CephFS read: `kubectl -n immich exec deploy/immich-server -- sh -c 's=$(date +%s%N); n=$(ls /family | wc -l); e=$(date +%s%N); echo "$n entries $(( (e-s)/1000000 )) ms"'`.

- [ ] **Step 2: Fit and prerequisites.**
  - The memtest checkpoint is PASS.
  - Each of `top` and `bottom` has at least 1664 Mi free; about 8.1 GiB is projected.
  - Gate PASS, Chris's go, and a 1-hour `namespace=rook` silence.

- [ ] **Step 3: Commit.**
  - Rook recreates the MDS pods (Recreate).
  - Watch `ceph fs status fs-pool` until it settles on one `active` and one `standby-replay`, on `top` and `bottom`.
  - CephFS consumers stay Ready: `kubectl -n gramps get pods` and `kubectl -n immich get pods`.

- [ ] **Step 4: The tests, after.**
  - Both MDS give `ceph config show … mds_cache_memory_limit` = `2147483648`.
  - Both pods show `priorityClassName: system-cluster-critical`, and QoS is `Burstable`.
  - The session count equals the before count (±1 for CSI churn).
  - `ceph osd blocklist ls` has no new entries.
  - The timed read returns the same entry count, in under 5 s.
  - `--health-only` PASS. Expire the silence.

- [ ] **Step 5: The morning after.** At or after 05:00 UTC, once the 03:30 `family-backup` and 04:00 `archive-backup` walks have run:
  - `ceph health detail` shows no `MDS_CACHE_OVERSIZED`;
  - MDS RSS stays under its 4Gi limit.
  - Record the cache size.
  - **Rollback:**
    - revert;
    - then, with Chris's go (a config write, not a repair; S7-acceptable), run `ceph config rm mds.fs-pool-a mds_cache_memory_limit` and `ceph config rm mds.fs-pool-b mds_cache_memory_limit`. Rook wrote them to the mon config store, and a revert does not remove them;
    - then check that `ceph config show` is back to `4294967296`.

### Task 12: nfs-ganesha request, priority and placement — **Chris approval**

**Files:**
- `cluster/rook-cluster/ceph-nfs.yaml`, under `spec.server`:
  - `resources: {requests: {memory: 5Gi}}`, with no limit. Comment:
    - the bounded cache (MDCACHE `Entries_HWMark` 100000; 100,008 caps measured);
    - there is no override path in Rook 1.19 with Ganesha 5.9;
    - the upstream question.
  - `priorityClassName: system-cluster-critical`, **in this same commit**. It may preempt priority-0 pods, Luma's included, when it has to move during a drain. That is consistent with spec §14 ("Luma is shed during drains").
  - `placement.nodeAffinity` preferred, weight 100, `kubernetes.io/hostname In [middle]`. Comment:
    - after the memtest PASS, `middle` has the most request room, the lowest real use and the healthiest NVMe;
    - on `top` or `bottom`, the per-node exit fails;
    - preferred, so a drain of `middle` re-places it.
- `cluster/prometheus-stack/rules/capacity-alerts.yaml`: add `NfsGaneshaOverRequest`: `max by (pod) (container_memory_working_set_bytes{namespace="rook",container="nfs-ganesha"}) > on (pod) max by (pod) (kube_pod_container_resource_requests{namespace="rook",container="nfs-ganesha",resource="memory"})`, `for: 30m`, warning. Comment: the 5Gi request assumes the cache bound holds; this says when it does not.

- [ ] **Step 1: Fit and prerequisites.**
  - The memtest checkpoint is PASS.
  - `middle` has at least 5120 Mi free; about 8.3 GiB is projected.
  - Gate PASS, Chris's go, and a 1-hour `namespace=rook` silence.
  - Chris confirms that no copy into `/family` or `/archive` is running on rowlett.

- [ ] **Step 2: Before.** Record each of these:
  - `ceph osd blocklist ls`;
  - the MDS `session ls` entry for `nfs.satellites…`;
  - `rados -p .nfs -N satellites listwatchers conf-nfs.satellites`, which shows one watcher, the current pod.

- [ ] **Step 3: Clean stop, then push.** The clean stop avoids two ganesha instances with `nodeid = satellites.a` running at once.
  1. Commit locally.
  2. Run `kubectl -n rook scale deploy rook-ceph-nfs-satellites-a --replicas=0`. Rook ignores Deployment updates, so this sticks.
  3. Monitor until the pod is gone and its MDS session has closed.
  4. Push.
  - Argo syncs `rook-cluster`, and Rook's CephNFS reconcile rewrites the Deployment with the new spec and `replicas: 1`.
  - If no new pod appears within 10 minutes of the sync, run `kubectl -n rook scale deploy rook-ceph-nfs-satellites-a --replicas=1` and report.
  - Expect rowlett's `hard` mounts to stall for several minutes, then resume.

- [ ] **Step 4: Verify the server.**
  - The new pod is Ready on `middle`, with `priorityClassName: system-cluster-critical` and QoS `Burstable`.
  - `rook-ceph-nfs-satellites-lb` endpoints point at the new pod IP (`.230` is the LB IP).
  - The ganesha log shows the grace period starting and ending, and any `reclaim` lines. Grep for `grace` and `reclaim`, and record them.
  - `ceph osd blocklist ls`: no entry for the new instance's address. A stale entry for the old address after a clean stop is unexpected: record it.
  - `rados … listwatchers conf-nfs.satellites` shows the new pod. The "lost RADOS watch" gotcha in `docs/backups.md` applies.
  - A new `nfs.satellites…` session appears in the MDS `session ls`.

- [ ] **Step 5: Client proof — Chris, on rowlett.** The LB status is not proof.
  - `grep 192.168.0.230 /proc/mounts` shows `vers=4.1`.
  - `ls /mnt/family | head` and `ls /mnt/archive | head` work.
  - `head -c 1048576 <some file> | wc -c` prints `1048576`.
  - Expected: no `Stale file handle`, no EIO.

- [ ] **Step 6: Close out.**
  - `MDS_CLIENT_RECALL` clears, because the cache is empty after the restart. It returns once a walk refills 100k entries; that is still the normal set.
  - `scripts/check-prometheus-rules.sh` PASSes, with `NfsGaneshaOverRequest` loaded.
  - `--health-only` PASS. Expire the silence.
  - **Rollback:** revert, using the same clean-stop sequence.

### Task 13: Exit verification and records

**Files:**
- Modify this plan's Status section (add a results table).
- Modify `docs/luma.md`: the 0b row in the phase table, and the "Memory headroom" gate.

- [ ] **Step 1: Headroom, per node.** Run `kubectl describe node top middle bottom | grep -A8 'Allocated resources'`.
  - **Exit:** `top` and `bottom` each have at least 4096 Mi of free memory requests; about 6.3 and 6.5 GiB are projected.
  - Also record `middle`, and the cluster-wide net against the 8.4 GiB baseline (projected 16.0 GiB).

- [ ] **Step 2: Real use.** Run `max_over_time((100*(1 - node_memory_MemAvailable_bytes/node_memory_MemTotal_bytes))[7d:5m])`. **Exit:** under 85% on every node.

- [ ] **Step 3: Rules and alerting.**
  - `scripts/check-prometheus-rules.sh` PASSes, and `platform:prometheusrule_selftest:loaded` = 1. **Exit:** the test rule is visible in `/api/v1/rules`.
  - Re-run Task 4's 10-row route matrix against the **live** `alertmanager-main` Secret (0600 scratch copy).
  - The `alertmanager_notifications_failed_total` delta since Task 5 is 0.
  - Run `scripts/check-dashboards.sh`: no new DEAD panels compared with a run before Task 4.

- [ ] **Step 4: Ceph.** **Exit:** `scripts/ceph-gate.sh --health-only` PASSes, so Ceph is back to its normal WARN noise.

- [ ] **Step 5: Logs.**
  - `scripts/test-fluent-bit-exclusion.sh` PASSes.
  - `router/scripts/verify-syslog-receiver.sh` PASSes.
  - The live results from Tasks 2 and 3 are recorded.

- [ ] **Step 6: Records.**
  - Commit the docs.
  - Update these memory notes:
    - `known_alert_rules_not_loading`: resolved, with the new label convention and the platform rules;
    - `known_middle_mem_requests`: the new per-node numbers, and NFS on `middle`;
    - `known_issues`: the ganesha cache finding, the router syslog fix and its daily volume, the MDS `config rm` rollback trap, and the OSD latency gate's pass rate.

## Deferred: HDD OSD request cut (old Task 9) — revisit after middle's memtest

- Chris chose to skip it for now. The facts, for when it comes back:
  - It would set `spec.resources.osd.requests.memory` from 4Gi to 3328Mi, with the limit staying 4Gi. That gives −4608 Mi, −1536 per node.
  - The `osd_memory_target` = min(3328Mi, 0.8 × 4Gi) = **3435973836 B, unchanged**.
  - Rook restarts the OSDs in host-sized batches (`ok-to-stop --max`), so both HDD OSDs of a host can go down at once. On SMR, expect 30–60 minutes.
  - It needs a time-boxed `CephOSDDown` silence plus the `namespace=rook` one, and the full gate.
- Revisit it after the memtest verdict, and after the NVMe swaps if phase 4's capacity needs it.

## Open questions

**Answered by Chris (2026-10-18/19):**
- HDD OSD cut: skipped, now under "Deferred".
- Extra reclaims: Argo repo-server 256Mi at a fixed 2 replicas (Task 4), and immich-ml 384Mi after verifying real use (Task 6).
- The Alertmanager weekday window uses `America/Los_Angeles` (Task 4).
- Router syslog: fix the receiver through Ansible with commit-confirm (Task 3), rather than drop fluent-bit's output.
- Satellites `.243` and `.244` are intentionally off; their targets are dropped by relabeling (Task 4).
- Memtest on `middle` runs before the MDS and NFS tasks (the checkpoint).

**Still open (only Chris can answer):**
1. **NFS on `middle` after the memtest PASS.** The exit depends on it, and it puts the family export on the media node. Confirm, or name another reclaim that frees about 1.7 GiB on `top`.
2. **The ganesha cache upstream.** File a Rook issue or PR for a CephNFS ganesha config override (`MDCACHE Entries_HWMark`, worth about 4 GiB), or accept 5 Gi permanently?
3. **Router SSD load.** The receiver writes about 1–1.5 GB/day of cluster logs to the router's 128 GB TLC SSD, capped at 100M × 200 compressed. Acceptable, or should the syslog output carry only warnings and above?
4. **Azure.** The HTTP Data Collector API passed its retirement date (2026-09-14) and still answers. Should the migration to the Logs Ingestion API (DCR) be scheduled now as its own plan?
5. **Gate waits.** The 1000 ms latency gate passes in about 25% of 15-minute windows. Accept multi-hour waits for each Rook task (my recommendation), rather than loosening it?

## Follow-ups and out of scope (surface, don't do)

1. **etcd metrics (first).**
   - Talos `cluster.etcd.extraArgs.listen-metrics-urls: http://0.0.0.0:2381` in `nodes/controlplane.yaml` (S4, machine config), plus kps `kubeEtcd.endpoints: [192.168.0.240, 192.168.0.241, 192.168.0.242]`.
   - etcd is unmonitored today, despite about 3 elections a day and the worn NVMes (#2661).
2. **Controller-manager, scheduler and kube-proxy metrics** need Talos `bind-address` (S4). The readiness rules cover liveness only.
3. **Watchdog → an external heartbeat** (a dead man's switch). Today Watchdog routes to `null`.
4. **Azure:** migrate fluent-bit's azure output to `azure_logs_ingestion` before the retired API stops answering. Add a fluent-bit PodMonitor (`release: prometheus`) so that output retries and drops become visible.
5. **Traefik access logs ship every app's OAuth callback `code=` query strings** to Azure today (oauth2-proxy, Dex). Consider dropping query strings cluster-wide. For phase 1, the per-router `traefik.ingress.kubernetes.io/router.observability.accesslogs: "false"` on Luma Ingresses is optional defence in depth: check the annotation against the Traefik v3.7 docs first.
6. **The dead ConfigMap rules** (the `luma-homelab-alert-rules` generator): convert or delete them. Four Ceph rules now live in `ceph-critical-alerts`.
7. **`gonic-alerts`** references an unscraped job and the removed `music-sync` CronJob.
8. **Upstream Rook:** a CephNFS ganesha config override would reclaim about 4 GiB (Open question 2).
9. **VyOS:** the `system syslog file` template bug (`${{ file }}`) is in this two-year-old rolling image. Do not use `system syslog file` until the image is upgraded, and check the receiver drop-in survives that upgrade (the postconfig script re-installs it).
10. **Phase-1 plan cross-references:** its P3 gate says "0b's throwaway `luma-system` namespace is gone". That should now read "`luma-0b-canary` is gone", and 0b never touches `luma-system`.

## Self-review (2026-10-19)

- **Spec coverage:**
  - Ceph gate → Task 1.
  - Household-critical BestEffort requests (nfs-ganesha, MDS, HA, Grafana, MetalLB) → Tasks 9, 11 and 12.
  - Reclaims (mons, mgr, CSI, Prometheus, gonic) → Tasks 6, 7, 8 and 10, plus Chris's repo-server and immich-ml reclaims in Tasks 4 and 6.
  - OSD requests left alone → Deferred.
  - Rule selector fix → Task 5.
  - fluent-bit exclusion → Task 2.
  - Exit: headroom per node, the test rule in `/api/v1/rules`, and Ceph's normal WARN → Task 13.
  - No node over 85% real use → Task 13 Step 2.
- **Review findings applied:**
  - Ceph/Rook:
    - H1 → priority classes and preferred placement, Tasks 11 and 12;
    - H2 → the gate script and the OSD-batch note;
    - H3 → per-node exit and projection;
    - M1 → `config rm` rollback and the explicit factor;
    - M2 → the reclaims-first order;
    - M3 → Task 10;
    - M4 → the clean stop;
    - M5 → Tasks 8, 11 and 12;
    - L → mons kept at 1536 with reasons, the ganesha alert, HA on `top`.
  - Alerting/logs:
    - H1 → the check script;
    - H2 → Task 4 Steps 4 and 9;
    - H3 → `ceph-critical-alerts` and the silences;
    - M1 → the capacity rules;
    - M2 → location, reload check and Discord deltas;
    - M3 → runtime counts;
    - M4 → etcd recorded, control-plane rules;
    - M5 → canary Running, logs visible, Azure run by Chris, retirement flagged, fluent-bit unscraped;
    - M6 → `luma-0b-canary`;
    - M7 → the grep filter;
    - Low → Task 13 Step 3, the 5-minute Monitor, the Argo diff review, the Watchdog follow-up.
- **Names checked across tasks:**
  - scripts: `ceph-gate.sh`, `test-fluent-bit-exclusion.sh`, `verify-syslog-receiver.sh`, `check-prometheus-rules.sh`;
  - rules: `platform-selftest`, `ceph-critical-alerts`, `capacity-alerts`, `control-plane-alerts`;
  - namespace: `luma-0b-canary`.
- **Numbers checked against the live cluster:**
  - 35 stock objects; 9 MDS sessions; 0 blocklist entries;
  - the `drivers.csi.ceph.io` CRD name; the rule-file basename format;
  - Alertmanager metric labels; the 25% gate pass rate.
