# Luma 0b: platform prep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare galaxy for Luma phase 1. Give the household-critical BestEffort pods real memory requests, reclaim over-requested platform memory (target: about 8 GiB of net headroom), load the stock and Luma alert rules cluster-wide, and keep `luma-*` logs inside the house.

**Architecture:** Every change edits an existing Argo-managed values block or Rook CR in this repo. Changes land one change set at a time: commit, Argo sync, watch to a definite outcome, then the next one. Two small scripts are the repeatable tests:
- `scripts/check-prometheus-rules.sh` checks that every PrometheusRule meant for this Prometheus is selected and loaded.
- `scripts/test-fluent-bit-exclusion.sh` runs the real tail input against fixture files and checks that `luma-*` namespaces are dropped.

Rook changes run only once Ceph is back to its normal WARN set, and only with Chris's "go" at execution time.

**Tech Stack:** Argo CD; kube-prometheus-stack 79.12.0 (Prometheus v3.8.0, Alertmanager 0.29.0); Rook v1.19.11 with Ceph 19.2.6 (CSI through the ceph-csi-operator); nfs-ganesha 5.9; fluent-bit 2.2.2 (chart 0.47.10); MetalLB 0.15.3; trivy-operator chart 0.25.0; ha-helm v0.6.0; Sealed Secrets.

**Spec:** `docs/superpowers/specs/2026-10-18-luma-design.md`: §2 (starting state), the §12.4 metrics bullet, §13.3, §14 "0b" and the Capacity table.

**Status:** draft for Chris's review (2026-10-18). Every number below was measured read-only between 01:30 and 01:57 UTC on 2026-10-19.

## Global Constraints

- CLAUDE.md S1–S9 apply to every task. The S2 exceptions this plan uses are:
  - `kubectl rollout restart`;
  - `kubectl delete pod`;
  - throwaway canary objects that a task creates and removes within itself.
- Spec, verbatim:
  - "Rook mons 4 → 1.5 Gi (they stay system-node-critical)".
  - "Prometheus 4 → 2.5 Gi, gonic".
  - "**OSD requests are left alone** until we know whether `osd_memory_target` follows the request or the limit".
  - "Target: about 8 GiB of net headroom on top of today's, with no node over 85% real use."
  - "These are Rook changes under S4/S7, so Chris approves them in the plan."
- Spec, verbatim: "**fluent-bit excludes every `luma-*` namespace** from the Azure and syslog outputs (an input exclude on the namespace path)". The namespaces are `luma-system`, `luma-sandbox` and `luma-browser`.
- Spec, verbatim: "Rules are PrometheusRule CRDs in namespace `prometheus` with the selector labels, plus a deploy check that they appear in `/api/v1/rules`." Also: "ServiceMonitors need `release=prometheus`."
- **Rook-touching tasks are 6, 7, 9, 10 and 11.** Each one needs all three of these:
  - the Task 1 Ceph gate, re-run and passing on the same day;
  - Chris's explicit go for that task;
  - no cordoned node, no drain, no memtest window and no Talos upgrade in progress (S5).
- **Only one change set is in flight at a time.** Watch every rollout with the safe-rollout skill or Monitor. Never use a Bash sleep loop.
- **Requests only.**
  - Reclaims lower `requests.memory` and leave every limit as it is.
  - Additions add no new limit, except the MDS one in Task 10, where the limit sets the MDS cache.
  - Never set request == limit on Plex or SAB. Neither is touched here.
- **Sizing rule for a new request:** round 1.25 × the 10-day max working set up to a multiple of 64 Mi. Where a task departs from this, it gives the measured reason.
- **Fit check before every scheduling change.** Run `kubectl describe node <n> | grep -A8 'Allocated resources'` on each node. The pod must fit on a node it is allowed to use.
  - Until Chris reports a memtest pass on `middle`, the CephFS/NFS data-path pods (MDS, nfs-ganesha) must not be placed on `middle`. This is the docs/luma.md gate.
  - The reason: a client on bad RAM corrupts data *before* Ceph checksums it, and a scrub cannot detect that.
- **Toolchain:**
  - Run tools with `nix develop -c …` and `KUBECONFIG=/tmp/galaxy-kubeconfig` (refresh the kubeconfig as CLAUDE.md describes).
  - `yq`, `amtool` and `promtool` are not in the dev shell. Use `nix shell nixpkgs#yq-go nixpkgs#prometheus-alertmanager -c …`.
  - `docker` is on rowlett.
- **Git:**
  - Commit straight to `main` with `nix develop -c git commit …`, and pre-commit must pass.
  - Commit signing is currently off: pass no `-S`, and never `--no-gpg-sign` or `--no-verify`.
  - `git add` named paths only. The tree carries Chris's unrelated in-progress telephony and docs edits.
  - Push as `ChristopherJMiller`; check with `gh api user --jq .login`.
  - Use `git diff --no-textconv` for anything that leaves the terminal.
- **Secrets:**
  - Never print a secret value. `alertmanager-config.secret.yaml` holds the Discord webhooks: inspect it with greps that exclude URLs, and validate it from a mode-0600 scratch copy that is deleted afterwards.
  - fluent-bit's azure output logs the Log Analytics workspace id on every flush. Grep fluent-bit logs; never paste them into chat, docs or commits.

## Review Focus

1. **A new request fits no allowed node.**
   - A pod with the Recreate strategy (HA, MDS, gonic) stays down until it schedules. A RollingUpdate pod (nfs-ganesha, Grafana) leaves a Pending surge pod behind.
   - Expected: this never happens, because each task runs the fit check first and the reclaims are ordered ahead of the big additions.
   - Test: the fit-check step in Tasks 5, 10 and 11, and the Pending check in every watch.
2. **Stock alerts flood Discord the moment the rules load.**
   - The critical route pings `@everyone` every 30 minutes.
   - Expected: only the day-one set evaluated below fires, and Watchdog, InfoInhibitor and `severity=info` reach no receiver.
   - Test: the route tests in Task 3, and the one-hour per-receiver Alertmanager watch in Task 4.
3. **A request change silently retunes Ceph.**
   - Expected: only the predicted values change.
     - OSD `osd_memory_target` = min(request, 0.8 × limit).
     - MDS `mds_cache_memory_limit` = 0.5 × limit, or 0.8 × request when there is no limit.
     - Mon targets do not move.
   - Test: `ceph config` assertions before and after in Tasks 6, 9 and 10.
4. **The `luma-*` match catches too much or too little.**
   - Expected: `luma-system`, `luma-sandbox` and `luma-browser` are dropped. These are still shipped: namespace `luma`, namespace `notluma-x`, a pod named `luma-core-…` in `default`, and a container named `luma-agent` in `default`.
   - Test: the Task 2 fixture cases.
5. **NFS clients during the ganesha restart** (rowlett's `/family` and `/archive`, and the satellites).
   - Expected: one grace-period stall, then I/O resumes with no stale handles.
   - Test: Task 11 checks first that no copy is running on rowlett, then checks the mount and a read afterwards.

---

## Measured baseline (2026-10-19 UTC, read-only)

**Ceph:**
- At 01:31 the PG 1.c repair was still running: `deep scrubbing for 875s`, so it started at about 01:16 UTC on **10-19** (the evening of 10-18 Pacific).
- By 01:56: HEALTH_WARN, 145/145 PGs `active+clean`. The checks present were:
  - `BLUESTORE_SPURIOUS_READ_ERRORS` (osd.4; left unmuted on purpose);
  - `MDS_CLIENT_RECALL` (client `rook-satellites`);
  - the three muted `AUTH_INSECURE_*` checks.
- This set is "normal WARN noise". `BLUESTORE_SLOW_OP_ALERT` also belongs to it whenever it appears.
- Argo: everything is Synced/Healthy except `rook` and `rook-cluster`, which are Degraded (long-standing, #2521).

**Memory requests.** Allocatable is 30830 Mi per node, 90.3 GiB in total.

| node | requests | % | real use now (MemAvailable) | 7-day p95 | 7-day max |
|---|---|---|---|---|---|
| top | 27959 Mi | 90% | 67.5% | 72.4% | 76.0% |
| middle | 28250 Mi | 91% | 40.0% | 42.6% | 46.7% |
| bottom | 27669 Mi | 89% | 57.2% | 56.0% | 57.4% |
| **total** | **83878 Mi (81.9 GiB)** | | headroom **8612 Mi (8.4 GiB)** | | |

- BestEffort pods use 9982 Mi for real today. The largest:
  - nfs-ganesha: 4755 Mi;
  - MDS a: 1238 Mi; MDS b: 1086 Mi;
  - Grafana: 910 Mi;
  - HA: 632 Mi;
  - trivy-operator: 602 Mi.
- Rook holds 54592 Mi of the requests.

**Findings that shape the plan:**
- **`osd_memory_target` follows the request, capped at 0.8 × the limit.**
  - Source: Ceph 19.2.6, `src/common/config.cc`, `parse_env`: `pod_request = min(POD_MEMORY_REQUEST, POD_MEMORY_LIMIT × osd_memory_target_cgroup_limit_ratio)`, for OSDs only. Rook 1.19.11 sets the env vars and no `--osd-memory-target` argument.
  - Live: osd.1 (SSD; request 1536 Mi, limit 2 Gi) runs at 1610612736 B, which is its request, with source `env`. osd.0 (request 4 Gi, limit 4 Gi) runs at 3435973836 B = 0.8 × 4 Gi.
  - Consequence: an HDD OSD request of 3328 Mi with the limit left at 4 Gi gives a target that is **byte-identical** (Task 9, optional).
  - OSDs and mons are `system-node-critical`, so their daemons run at `oom_score_adj −997`, whatever their request.
- **nfs-ganesha's 4.7 GiB is a cache at its default bound, not a leak.**
  - Its RSS was 52 Mi until 2026-10-12 01:30. It climbed while the `/archive` ingest walked the tree, then went flat at about 4740 Mi from 10-14 on (all of it anonymous RSS, no page cache).
  - The MDS shows that session holding **100,008 caps**. That is Ganesha's `MDCACHE Entries_HWMark` default of 100000, and the pinned caps are what keep `MDS_CLIENT_RECALL` up.
  - **There is no supported way to lower it.**
    - Rook's generated `ganesha.conf` already contains `MDCACHE { Dir_Chunk = 0; }`.
    - Ganesha 5.9 declares MDCACHE `CONFIG_UNIQUE` (`mdcache_read_conf.c`), so a second block added through `ceph nfs cluster config set` is rejected.
    - The Rook 1.19 `CephNFS` spec has no config field.
  - So it is sized honestly at 5 Gi (Task 11). The upstream route is an open question for Chris.
- **MDS:** Rook 1.19.11 sets `mds_cache_memory_limit` to 0.5 × the memory limit, or to 0.8 × the request when there is no limit (`file/mds/config.go`). Today it is the 4 GiB default, and the MDS cache holds 0.89 GB.
- **CSI resources:**
  - They come from the rook-ceph chart's `csi.csi{RBD,CephFS}{Provisioner,Plugin}Resource` strings. These flow into `rook-ceph-operator-config`, then into the Driver CRs `rook.rbd.csi.ceph.com` and `rook.cephfs.csi.ceph.com`.
  - Per-container 7-day max: plugin 63 Mi, sidecars 51 Mi or less, registrar 22 Mi. The requests are 512, 128 and 128 Mi.
- **trivy:** `operator.resources` in `cluster/applications/trivy-operator.yaml` is dead config, because chart 0.25.0 reads the top-level `resources:` key. Its intended 512 Mi limit would OOM an operator whose 10-day max is 868 Mi.
- **Rule loading today:**
  - Three PrometheusRule objects are selected (`ipmi-alerts`, `media-pipeline-alerts`, `backup-alerts`), which load 6 groups.
  - The 37 kps stock PrometheusRules (label `release=prometheus`, 149 alerts and 90 recording rules) exist but nothing selects them.
  - `music-streaming/gonic-alerts` carries `app=gonic` only.
- **Day-one alerts.** I evaluated every stock alert expression against live Prometheus. Ignoring each rule's `for:` window, these would fire:

  | alert | severity | cause |
  |---|---|---|
  | TargetDown | warning | `kube-scheduler`, `kube-controller-manager` and `kube-proxy` are 0/3 up: Talos binds them to 127.0.0.1. Satellites `.243` and `.244` are both unreachable ("no route to host"). |
  | Watchdog | none | always firing, by design |
  | KubeHpaMaxedOut | warning | `argo-cd/argo-install-argocd-repo-server` has sat at max 2 for two years (`ScalingLimited/TooManyReplicas`) |
  | CPUThrottlingHigh | info | trivy scan jobs |
  | KubeMemoryOvercommit | warning | requests exceed what two nodes can hold, by 28.4 GiB. This is by design. |

  - `KubeAPIErrorBudgetBurn` will not fire: the inline burn rates are about 0.0003, against a threshold of 0.01.
  - The 3-day burn-rate rule costs 0.56 s per evaluation, at a 30 s interval.
  - Alertmanager currently has no active alerts and no silences.
- **Alertmanager routing:**
  - Severity `none` or `info` falls through to the `severity!="critical"` route, which goes to the `homelab-default` receiver.
  - The weekday window `09:00–22:00` has no `location`, so it is UTC: 02:00–15:00 Pacific.
  - Stock kps ships no Ceph rules, so the chronic SMR WARN produces no alert. The only rule that would alert on it is in the dead ConfigMap.
- **Router syslog is a black hole.**
  - VyOS has no UDP 514 listener (`ss -ulnp`).
  - Its rendered `/etc/rsyslog.d/00-vyos.conf` has a Python dict as the omfile target, and it forwards to `0.0.0.0:514`.
  - Nothing that fluent-bit sends there is stored. That is also the source of fluent-bit's constant `syslog.0` retry warnings.
  - The input-level exclusion covers this output anyway.

### Sizing (before → after)

| component (count) | file and key | request before → after | limit | 7d p95 / 10d max (Mi) | Δ total | task |
|---|---|---|---|---|---|---|
| MetalLB controller | `cluster/metallb/release.yaml` `controller.resources` | 0 → 128Mi | none | 86 / 86 | +128 | 5 |
| MetalLB speaker (3) | same file, `speaker.resources` | 0 → 64Mi | none | 34 / 36 | +192 | 5 |
| Grafana | `cluster/applications/prometheus-stack.yaml` `grafana.resources` | 0 → 448Mi | none | — / 349 | +448 | 5 |
| Grafana sidecars (2) | same file, `grafana.sidecar.resources` | 0 → 384Mi | none | — / 297, 275 | +768 | 5 |
| trivy-operator | `cluster/applications/trivy-operator.yaml`, top-level `resources` | 0 → 1088Mi | none | 747 / 868 | +1088 | 5 |
| Home Assistant | `cluster/applications/home-assistant-release.yaml` `resources` | 0 → 1Gi | none | 656 / 767 | +1024 | 5 |
| mons (3) | `cluster/rook-cluster/cluster.yaml` `spec.resources.mon` | 4Gi → 1536Mi | 4Gi | ≤629 / ≤651 | −7680 | 6 |
| mgr (2) | same file, `spec.resources.mgr` | 2Gi → 1408Mi | 2Gi | 1027 / 1105 (active) | −1280 | 6 |
| CSI ctrlplugin (4) | `cluster/applications/rook.yaml` `csi.csi{RBD,CephFS}ProvisionerResource` | 1024 → 384Mi per pod | unchanged | ≤216 per pod | −2560 | 7 |
| CSI nodeplugin (6) | same file, `csi.csi{RBD,CephFS}PluginResource` | 640 → 160Mi per pod | unchanged | ≤84 per pod | −2880 | 7 |
| gonic | `cluster/music-streaming/deployment.yaml` | 2Gi → 256Mi | 2Gi | 93 / 103 | −1792 | 8 |
| Prometheus | `cluster/applications/prometheus-stack.yaml` `prometheus.prometheusSpec.resources` | 4Gi → measured (est. 2752Mi) | 4Gi | 1283 / 2185 | ≈ −1344 | 8 |
| HDD OSDs (6), optional | `cluster/rook-cluster/cluster.yaml` `spec.resources.osd` | 4Gi → 3328Mi | 4Gi | ≤3351 / ≤3625 | −4608 | 9 |
| MDS (2) | `cluster/rook-cluster/fs-pool.yaml` `spec.metadataServer.resources` | 0 → 1664Mi | 4Gi (cache 2 GiB) | 1241 / 1309 | +3328 | 10 |
| nfs-ganesha | `cluster/rook-cluster/ceph-nfs.yaml` `spec.server.resources` | 0 → 5Gi | none | 4755 / 4781 | +5120 | 11 |

**Projection:**
- **Without Task 9:** additions +12096 Mi and reclaims −17536 Mi give **net +5.3 GiB**. Headroom goes from 8.4 GiB to **13.7 GiB**.
- **With Task 9:** net **+9.8 GiB**, and headroom **18.2 GiB**.
- **The spec's "about 8 GiB" is reachable only with Task 9**, because ganesha's honest 5 Gi uses up most of the reclaim.

Per node at the end, assuming NFS and MDS-b on top, MDS-a on bottom and everything else where it is now:

| node | without Task 9 | with Task 9 |
|---|---|---|
| top | 96% | 91% |
| middle | 72% | 68% |
| bottom | 86% | 81% |

- Real use does not change, because these pods already run where they are.
- The free headroom sits on `middle` until the memtest verdict.

## Sequence at a glance

| task | touches Rook | Chris "go" | user-visible blip |
|---|---|---|---|
| 1 Gate and baseline | read-only | — | — |
| 2 fluent-bit exclusion | no | sign-off, plus the throwaway-namespace delete | — |
| 3 Alert-noise pre-work | no | yes, for argo-cd | — |
| 4 Rule loading | no | sign-off | — |
| 5 Requests for non-Rook BestEffort pods | no | Chris picks HA's window | HA: 1–2 min |
| 6 mon and mgr requests | **yes** | **yes** | — |
| 7 CSI requests | **yes** | **yes** | — |
| 8 gonic and Prometheus | no | sign-off | music: about 1 min; Prometheus WAL replay |
| 9 HDD OSD requests (optional) | **yes** | **yes** | — |
| 10 MDS requests and placement | **yes** | **yes** | CephFS failover: seconds |
| 11 nfs-ganesha request and placement | **yes** | **yes** | NFS grace period |
| 12 Exit verification and records | read-only | — | — |

**Recommended place for the memtest window:** after Task 9 and before Tasks 10–11.
- The reclaims shrink the Pending set when `middle` is drained.
- With a verdict in hand, the MDS and NFS placement rules may simply leave `middle` allowed.

---

### Task 1: Gate and baseline

**Files:** none (read-only).

**Interfaces:**
- Produces the gate command that Tasks 6, 7, 9, 10 and 11 re-run.
- Produces the baseline the per-node fit checks use.

- [ ] **Step 1: Ceph gate.**

```bash
kubectl -n rook exec deploy/rook-ceph-tools -- ceph health detail -f json | jq -r '
  [.checks|keys[]] - ["AUTH_INSECURE_CLIENT_KEY_TYPE","AUTH_INSECURE_KEYS_ALLOWED","AUTH_INSECURE_KEYS_CREATABLE",
                      "BLUESTORE_SPURIOUS_READ_ERRORS","MDS_CLIENT_RECALL","BLUESTORE_SLOW_OP_ALERT"]
  | if length==0 then "GATE PASS" else "GATE FAIL: \(join(","))" end'
kubectl -n rook exec deploy/rook-ceph-tools -- ceph pg stat
kubectl -n rook exec deploy/rook-ceph-tools -- ceph -s | grep -E 'mon:|osd:'
```

  - Pass when all four hold:
    - the output is `GATE PASS`;
    - every PG is `active+clean` (`+scrubbing` or `+deep` is allowed);
    - 3 mons are in quorum;
    - `9 osds: 9 up … 9 in`.
  - On 2026-10-19 at 01:56 UTC: PASS.
  - On FAIL: stop, run the ceph-doctor skill (read-only), and report to Chris.
    - Tasks 2–5 and 8 do not touch Rook. Chris may waive the gate for them; the default is to wait.

- [ ] **Step 2: Maintenance state.**
  - `kubectl get nodes` shows no `SchedulingDisabled`.
  - Argo shows nothing unhealthy beyond `rook` and `rook-cluster`.
  - Ask Chris once (AskUserQuestion): has `middle`'s memtest run, and with what verdict? Record the answer in this plan's Status line; Tasks 10 and 11 depend on it.

- [ ] **Step 3: Re-measure.**
  - Record per-node `Allocated resources` and `kubectl top nodes`.
  - If any node's requests moved by more than 1 GiB from the baseline table, redo the fit arithmetic in Tasks 5–11 before going on.

### Task 2: fluent-bit drops `luma-*` namespaces at the input

**Files:**
- Create `scripts/test-fluent-bit-exclusion.sh`.
- Modify `cluster/applications/fluent-bit.yaml`, the `Name tail` `[INPUT]` in `valuesObject.config.inputs`.

**Interfaces:**
- Produces `nix shell nixpkgs#yq-go -c scripts/test-fluent-bit-exclusion.sh`, with exit code 0 = all cases pass.
- It needs `docker`. It is not a pre-commit hook.

- [ ] **Step 1: Write the test.** The script:
  1. Extracts the `Name tail` `[INPUT]` section from `.spec.source.helm.valuesObject.config.inputs`.
  2. Writes a config of `[SERVICE] Flush 1`, that INPUT and `[OUTPUT] Name stdout, Match *`. Two test-only overrides go into the copy: `Read_From_Head On`, and `DB` pointed into the temp dir.
  3. Runs `docker run --rm fluent/fluent-bit:<valuesObject.image.tag>` for about 15 s. A temp dir is mounted at `/var/log/containers`, holding one CRI-format line per file (`<RFC3339> stdout F <marker>`).
  4. Prints PASS or FAIL per case, and exits 1 on any FAIL.

  | fixture file (`<id>` = 64 hex chars) | expected |
  |---|---|
  | `app-1_default_app-<id>.log` | shipped |
  | `luma-core-1_default_core-<id>.log` | shipped (the pod name starts with `luma-`) |
  | `web-1_default_luma-agent-<id>.log` | shipped (the container name starts with `luma-`) |
  | `x-1_luma_c-<id>.log` | shipped (the namespace is exactly `luma`) |
  | `x-1_notluma-x_c-<id>.log` | shipped |
  | `core-1_luma-system_core-<id>.log` | **dropped** |
  | `sb-1_luma-sandbox_shell-<id>.log` | **dropped** |
  | `br-1_luma-browser_chrome-<id>.log` | **dropped** |

  Each line's marker is its file's stem, and a case passes when that marker is present in stdout, or absent for a dropped case.

- [ ] **Step 2: Run it.** Expected: FAIL on the three dropped cases, because nothing is excluded today.

- [ ] **Step 3: Add to the tail INPUT.** The line is `Exclude_Path /var/log/containers/*_luma-*_*.log`, with a comment saying:
  - CRI log names are `<pod>_<namespace>_<container>-<id>.log`;
  - pod, namespace and container names cannot contain `_`, so `_luma-…_` can only be the namespace;
  - the exclusion is at the input, so neither the azure output nor the syslog output ever sees these files;
  - `K8S-Logging.Exclude Off` stays, so a workload can never opt itself back in or out.

- [ ] **Step 4: Run it again.** Expected: all 8 PASS.

- [ ] **Step 5: Commit.**

```bash
git add scripts/test-fluent-bit-exclusion.sh cluster/applications/fluent-bit.yaml
nix develop -c git commit -m "logging: fluent-bit drops luma-* namespaces at the tail input (Luma 0b)"
git push origin main
```

- [ ] **Step 6: Watch.** Argo `fluent-bit` goes Synced/Healthy and the DaemonSet rolls 3/3; the new pods are young. Use Monitor.

- [ ] **Step 7: Live check with throwaway canaries.**
  1. Check that `kubectl get ns luma-system` returns NotFound. If it exists, phase 1 owns it: use it, and delete only the pods afterwards.
  2. Otherwise create it, labelled `purpose=luma-0b-canary`.
  3. Start two `busybox:1.36.1` pods, both with `nodeName: bottom`, that echo `MARKER-<uuid>-<namespace>` every 5 s: `fb-canary` in `luma-system`, and `fb-canary-control` in `default`.
  4. After 2 minutes, on the fluent-bit pod on `bottom`, take `kubectl -n logging logs <pod> --since=5m | grep inotify_fs_add`.
     - At least 1 line must name `fb-canary-control_default_`. This proves the check can see a tailed file.
     - 0 lines may name `_luma-system_`.
  5. After 20 minutes (Log Analytics ingestion lag), read `WS` from Secret `logging/azure-log-analytics`, key `workspace-id`, into a variable without echoing it. Then, for each marker, run `nix develop -c az monitor log-analytics query -w "$WS" --analytics-query "luma_homelab_logs_CL | where TimeGenerated > ago(1h) | where * has '<marker>' | count" -o tsv`.
     - Expected: the control marker counts more than 0; the `luma-system` marker counts 0.
  6. There is no router check: the router has no listener (see the baseline). The input-level result covers that output.

- [ ] **Step 8: Clean up.**
  - Delete both pods.
  - Delete namespace `luma-system`. This is S4: Chris pre-approves this one deletion when he signs off the plan. Re-confirm first that only the canary was ever in it.
  - Record the result in Status.
  - **Rollback for this task:** revert the commit.

### Task 3: Alert-noise pre-work (lands before any rule loads)

**Files:**
- Modify `cluster/prometheus-stack/alertmanager-config.secret.yaml`, and regenerate `cluster/prometheus-stack/alertmanager-config.yaml` from it.
- Modify `cluster/applications/prometheus-stack.yaml`: `kubeControllerManager`, `kubeScheduler`, `kubeProxy` and `defaultRules.disabled`.
- Modify `cluster/argo-cd/values.yaml`: `argo-cd.repoServer`. **This one needs Chris's OK**, because the change lands in the argo-cd namespace.

- [ ] **Step 1: Write the route tests and watch them fail.**
  - Work on a scratch copy: `umask 077; t=$(mktemp -d)`, then `nix shell nixpkgs#yq-go -c yq '.stringData["alertmanager.yaml"]' cluster/prometheus-stack/alertmanager-config.secret.yaml > "$t/am.yaml"`.
  - For each row, run `nix shell nixpkgs#prometheus-alertmanager -c amtool config routes test --config.file "$t/am.yaml" --verify.receivers=<receiver> <labels>`.

  | labels | receiver |
  |---|---|
  | `alertname=Watchdog severity=none` | `null` |
  | `alertname=InfoInhibitor severity=none` | `null` |
  | `alertname=CPUThrottlingHigh severity=info` | `null` |
  | `alertname=KubePodCrashLooping severity=warning` | `homelab-default` |
  | `alertname=KubePersistentVolumeFillingUp severity=warning` | `storage-alerts` |
  | `alertname=KubeAPIDown severity=critical` | `critical-alerts` |
  | `alertname=CephClusterHealthError severity=critical` | `critical-alerts` |

  - Expected now: rows 1–3 FAIL (they get `homelab-default`), and rows 4–7 PASS.

- [ ] **Step 2: Edit the Alertmanager config.**
  - Add a receiver `- name: 'null'` with no configs.
  - Prepend two routes to `route.routes`: `alertname=~"Watchdog|InfoInhibitor"` → `null`, then `severity="info"` → `null`.
  - Change nothing else. The UTC window is an open question.

- [ ] **Step 3: Re-run the tests.**
  - Regenerate `$t/am.yaml`. All 7 must PASS, and `amtool check-config "$t/am.yaml"` must succeed.
  - `rm -r "$t"`.

- [ ] **Step 4: Reseal.**
  - `rm cluster/prometheus-stack/alertmanager-config.yaml && nix develop -c ./sign.sh`.
  - `git status --short cluster/` must show only the two alertmanager files.

- [ ] **Step 5: kps values.**
  - Under `valuesObject`, add `kubeControllerManager: {enabled: false}`, `kubeScheduler: {enabled: false}` and `kubeProxy: {enabled: false}`, with this comment:
    > Talos binds these to 127.0.0.1. All 9 targets have always been down. Scraping them needs machine-config `bind-address` changes (S4), which this plan does not make.
  - Add `defaultRules.disabled: {KubeMemoryOvercommit: true}`, with this comment:
    > By design, two nodes cannot hold three nodes' requests (spec §14 capacity: Luma is shed during drains). This alert would fire permanently.

- [ ] **Step 6: Argo repo-server** (with Chris's OK). Set `argo-cd.repoServer.autoscaling.enabled: false` and `argo-cd.repoServer.replicas: 2`.
  - Same two pods, no restart.
  - It removes a `KubeHpaMaxedOut` alert that would otherwise fire forever.
  - If Chris declines, add `KubeHpaMaxedOut: true` to `defaultRules.disabled` instead.

- [ ] **Step 7: Commit.** There are two commits: the alertmanager pair plus `prometheus-stack.yaml`, then `cluster/argo-cd/values.yaml` on its own. Push.

- [ ] **Step 8: Watch and verify.**
  - Argo apps `prometheus-stack-config`, `kube-prometheus-stack` and `argo-cd` go Synced/Healthy.
  - Alertmanager has loaded the new config. Through the API-server service proxy to `prometheus-kube-prometheus-alertmanager:9093`, run `/api/v2/status | jq -r .config.original | grep -cE "receiver: '?null'?"`. It must print 2. Print counts only, never the config.
  - `/api/v1/targets` has no `kube-controller-manager`, `kube-scheduler` or `kube-proxy` job.
  - The HPA is gone, `argo-install-argocd-repo-server` is at 2/2, and the pod ages are unchanged.
  - **Rollback:** revert. The revert restores both the secret file and the sealed file.

### Task 4: Stock and namespace-scoped rules load

**Files:**
- Create `scripts/check-prometheus-rules.sh`.
- Create `cluster/prometheus-stack/rules/selftest.yaml`.
- Modify `cluster/prometheus-stack/rules/kustomization.yaml`: add the resource, and fix its stale selector comment.
- Modify `cluster/prometheus-stack/rules/media-pipeline-alerts.yaml`: add the label, and fix its header comment.
- Modify `cluster/ipmi-exporter/prometheusrule.yaml` and `cluster/backups/prometheusrule.yaml`: add the label.
- Modify `cluster/applications/prometheus-stack.yaml`: `prometheus.prometheusSpec.ruleSelector` and `ruleNamespaceSelector`.

**Interfaces:**
- Produces `scripts/check-prometheus-rules.sh`. It takes no arguments; `KUBECONFIG` defaults to `/tmp/galaxy-kubeconfig`; exit code 0 means pass. Task 8, Task 12 and the phase-1 deploy plan use it as the spec §12.4 deploy check.
- Produces the convention: **every PrometheusRule for this Prometheus carries `release: prometheus`.** This is the same label ServiceMonitors, PodMonitors, Probes and ScrapeConfigs already need.
  - It may live in any namespace. Luma's own rules still go in `prometheus`, as the spec says.
  - The old `prometheus: kube-prometheus` / `role: alert-rules` labels become harmless leftovers.

- [ ] **Step 1: Write the check.** It follows the style of `scripts/check-dashboards.sh` (`kubectl exec` into the Prometheus pod and `wget` the API).
  1. Read the live Prometheus CR's `.spec.ruleSelector` and `.spec.ruleNamespaceSelector`. It handles `matchLabels` only; abort if `matchExpressions` appears.
  2. List every PrometheusRule cluster-wide, and get the loaded groups from `/api/v1/rules`. A loaded group's file path contains `<ns>-<name>-`.
  3. Report:
     - `FAIL inert <ns>/<name>`: a rule labelled `release=prometheus` or `prometheus=kube-prometheus` that the selectors do not select;
     - `FAIL not-loaded <ns>/<name>/<group>`: a rule that is selected but whose group is missing;
     - `WARN unselected <ns>/<name>`: any other PrometheusRule.
  4. Print the summary `selected=<n> groups=<g> alerting=<a> recording=<r>`, then exit 1 if anything FAILed or `g == 0`.

- [ ] **Step 2: Run it.** Expected:
  - 33 `FAIL inert` lines: the kps stock rules, which are 37 minus the four scheduler, controller-manager and kube-proxy rule objects that Task 3's toggles remove;
  - one `WARN unselected music-streaming/gonic-alerts`, and `selected=3 groups=6`;
  - exit code 1.

- [ ] **Step 3: Add the selftest rule and relabel.**
  - `selftest.yaml` is a PrometheusRule `platform-selftest` in namespace `prometheus` with label `release: prometheus`. It has one group, `platform-selftest`, holding one rule: `record: platform:prometheusrule_selftest:loaded`, `expr: vector(1)`.
  - Add `release: prometheus` to the three custom rules.
  - Commit, push, and wait for sync.
  - Expected: the script still FAILs, now with 34 inert rules, the selftest's included. The 6 custom groups are still loaded.

- [ ] **Step 4: Flip the selector.**
  - Set `ruleSelector: {matchLabels: {release: prometheus}}` and `ruleNamespaceSelector: {}`, with a comment that points at the convention above.
  - Commit and push.
  - Watch `kube-prometheus-stack`. The operator rewrites the rule ConfigMaps and the config-reloader reloads them. No Prometheus restart is expected.

- [ ] **Step 5: Run the check.**
  - Expected: PASS, with `selected=37 groups=40`: 3 custom objects (6 groups), the selftest (1 group) and 33 stock objects (1 group each). Record the exact summary.
  - The query `platform:prometheusrule_selftest:loaded` returns 1.

- [ ] **Step 6: Throwaway checks (same conventions as Task 2's namespace).** In a throwaway namespace `luma-system`:
  1. Create PrometheusRule `luma0b-canary`, labelled `release: prometheus`, with `record: luma0b:canary:loaded`, `expr: vector(1)`. Expected: within 2 minutes it appears in `/api/v1/rules` under a file path containing `luma-system-luma0b-canary-`. **This proves the namespace selector.**
  2. Create pod `crashloop-canary`: `busybox:1.36.1`, `command: ["sh","-c","exit 1"]`. Expected: within about 6 minutes `/api/v1/alerts` shows `KubePodCrashLooping` with `state="pending"` and `namespace="luma-system"`. **This proves stock alerts cover Luma's namespaces.**
  3. Delete both before the alert's 15-minute `for:` elapses, so no Discord message is sent.
  4. Delete the namespace (S4, pre-approved, after re-confirming only the canaries were in it).

- [ ] **Step 7: Rule-evaluation load, one hour after Step 4.**
  - `max(prometheus_rule_group_last_duration_seconds)` must be under 10 s.
  - `sum(increase(prometheus_rule_group_iterations_missed_total[1h]))` must be 0.
  - Record the Prometheus container's working set before and after; Task 8 uses it.
  - If the `kube-apiserver-burnrate.rules` group misses evaluations, propose to Chris that `defaultRules.rules.kubeApiserverBurnrate` and `kubeApiserverSlos` go to `false`.

- [ ] **Step 8: Alertmanager watch for one hour (Monitor).** Watch `alertname`, `severity` and `receivers[].name` in `/api/v2/alerts`.
  - Expected:
    - Watchdog, InfoInhibitor and CPUThrottlingHigh go to `null`;
    - `TargetDown{job=~"satellites.*"}` goes to `homelab-default` (open question);
    - nothing goes to `critical-alerts`.
  - For anything else, report it to Chris with the evaluated cause.
    - If it is noise, a follow-up `defaultRules.disabled` commit goes in only with his OK.
    - If it is real, report it and do not fix it (working agreement).
  - **Rollback:** revert the Step 4 commit. That returns the cluster to 3 objects and 6 groups.

### Task 5: Real requests for the non-Rook BestEffort pods

**Files** (one commit, sync and watch per file, in this order):
1. `cluster/metallb/release.yaml`
2. `cluster/applications/prometheus-stack.yaml` (the `grafana` block)
3. `cluster/applications/trivy-operator.yaml`
4. `cluster/applications/home-assistant-release.yaml`

- [ ] **Step 1: Fit check.** These additions need about 1.1 GiB on top (HA and a speaker), about 2.4 GiB on bottom (Grafana, trivy, the MetalLB controller and a speaker) and 64 Mi on middle. The baseline free memory is 2871, 2580 and 3161 Mi. Re-run the check against Task 1's numbers.

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
  - The strategy is Recreate, so expect 1–2 minutes of outage. HA has no affinity and may change node.
  - Chris picks the window: daytime, with someone home (`known_ha_reload_blips`).
  - Verify with `nix develop -c hactl health` (ha-config skill): no new failures compared with a run just before the change. The pod must be `Burstable`.

- [ ] **Step 6: Record** where each pod landed and the new per-node requests.
  - **Rollback:** revert the commit for the app concerned.

### Task 6: Rook mon, then mgr, requests — **Chris approval**

**Files:** `cluster/rook-cluster/cluster.yaml`.
- `spec.resources.mon.requests.memory: 1536Mi`; its `limits.memory: 4Gi` stays.
- Then, in a second commit, `spec.resources.mgr.requests.memory: 1408Mi`; its `limits.memory: 2Gi` stays.

- [ ] **Step 1: Prerequisites.** Run the Task 1 gate (PASS) and get Chris's go.

- [ ] **Step 2: The test that holds across the change.**
  - `ceph config show-with-defaults mon.d | grep '^mon_memory_target '` must print `2147483648 … default`, both before and after. Mon targets are not request-derived.
  - Record the before value.

- [ ] **Step 3: Commit the mon change.**
  - Rook rolls `mon-d`, `mon-u` and `mon-v` one at a time and waits for quorum.
  - Watch with Monitor until all three mon pods request `1536Mi`. Expect 5–15 minutes.
  - After each mon restart, `ceph mon stat` must show all 3 in quorum.
  - The `ceph-mon` process in each pod keeps `oom_score_adj −997` (`system-node-critical`).

- [ ] **Step 4: Re-check.** Step 2's value is unchanged, and the gate PASSes.

- [ ] **Step 5: Commit the mgr change.**
  - The standby restarts first, then the active fails over.
  - `ceph mgr stat` shows 1 active and 1 standby.
  - The `ceph_health_status` series is present again within 5 minutes; ceph-mgr is scraped through pod annotations.

- [ ] **Step 6: Gate PASS.**
  - **Rollback:** revert. Rook rolls the daemons back the same way.

### Task 7: CSI requests — **Chris approval**

**Files:** `cluster/applications/rook.yaml`, under `valuesObject.csi`: `csiRBDProvisionerResource`, `csiRBDPluginResource`, `csiCephFSProvisionerResource` and `csiCephFSPluginResource`.

- [ ] **Step 1: Prerequisites.** Gate PASS, and Chris's go.

- [ ] **Step 2: Write the four strings.**
  - Copy each default string **verbatim, with every entry**, from `helm show values --repo https://charts.rook.io/release rook-ceph --version 1.19.11`. That includes `csi-omap-generator` and `liveness-prometheus`.
  - Change only these memory requests:
    - `csi-provisioner`, `csi-resizer`, `csi-attacher`, `csi-snapshotter`: 128Mi → 64Mi;
    - `csi-rbdplugin`, `csi-cephfsplugin`: 512Mi → 128Mi;
    - `driver-registrar`: 128Mi → 32Mi.
  - Limits and CPU stay unchanged.

- [ ] **Step 3: Commit and sync `rook`.**
  - The `rook-ceph-operator-config` CSI keys show the new values.
  - The `driver.csi.ceph.com` CRs `rook.rbd.csi.ceph.com` and `rook.cephfs.csi.ceph.com` must show them within 10 minutes. If they do not, run `kubectl -n rook rollout restart deploy/rook-ceph-operator`, which is the precedent in the SMR memory note.
  - The ctrlplugin Deployments and nodeplugin DaemonSets roll: the pods request 384Mi and 160Mi.

- [ ] **Step 4: Prove mounts still work on non-household workloads.**
  - CephFS: `kubectl -n immich rollout restart deploy/immich-machine-learning` (it mounts `immich-ml-cache-fs`). It must reach Ready.
  - RBD: `kubectl -n trivy-system delete pod trivy-server-0`. It must reach Ready.

- [ ] **Step 5: Gate PASS.**
  - **Rollback:** revert, then restart the operator.

### Task 8: gonic and Prometheus requests

**Files:**
- `cluster/music-streaming/deployment.yaml`: the gonic container's `requests.memory` goes 2Gi → 256Mi; its limit of 2Gi stays.
- `cluster/applications/prometheus-stack.yaml`: `prometheus.prometheusSpec.resources.requests.memory`; its limit of 4Gi stays.

- [ ] **Step 1: gonic.**
  - Commit, push and watch. gonic uses Recreate, so music stops for about a minute.
  - It must reach Ready, which also re-proves an RBD mount after Task 7.

- [ ] **Step 2: Size Prometheus**, no earlier than 72 hours after Task 4 Step 4.
  - Take `M = max_over_time(container_memory_working_set_bytes{namespace="prometheus",pod="prometheus-prometheus-kube-prometheus-prometheus-0",container="prometheus"}[3d])`.
  - The request is **max(2560Mi, ceil64(1.25 × M))**.
  - The 10-day max before any rules loaded was already 2185 Mi, so expect about 2752Mi. Record the departure from the spec's 2.5 Gi and the measurement behind it.

- [ ] **Step 3: Commit.**
  - The StatefulSet restarts and replays its WAL from the HDD PVC, which takes minutes.
  - Do it only while `ceph osd perf` shows commit latency under 100 ms, with no repair running.
  - Verify: Ready, targets are up again, and `scripts/check-prometheus-rules.sh` PASSes.
  - **Rollback:** revert.

### Task 9 (optional, recommended): HDD OSD requests — **Chris approval**

**Files:** `cluster/rook-cluster/cluster.yaml`.
- `spec.resources.osd.requests.memory` goes 4Gi → 3328Mi; `limits.memory: 4Gi` stays.
- `osd-ssd` is untouched.

**Why this is safe:**
- The target is min(3328Mi, 0.8 × 4Gi = 3276.8Mi). That gives **3435973836 B, unchanged.**
- OSD usage can exceed the request (the 10-day max is 3625 Mi). That costs nothing in OOM terms: the daemons keep `oom_score_adj −997`.
- This task is the only one that gets the plan to the spec's target. That is why it is recommended.

- [ ] **Step 1: The test that must hold.** For each of osd.0, 2, 3, 4, 5 and 6, run `ceph config show osd.$i osd_memory_target`. Today every one is `3435973836`, with source `env`. Record them.

- [ ] **Step 2: Prerequisites.**
  - Gate PASS, and Chris's go.
  - `ceph pg dump pgs_brief 2>/dev/null | grep -c scrubbing` should be 0, or else Chris accepts running alongside the scrubs.
  - `ceph osd perf` is quiet.

- [ ] **Step 3: Commit.**
  - Rook restarts the HDD OSDs one at a time behind `ok-to-stop`. Expect 30–60 minutes on SMR.
  - Watch with Monitor: each OSD comes back up and in, and the PGs return to `active+clean`.

- [ ] **Step 4: Repeat Step 1.**
  - All six must still be `3435973836`; the pods request 3328Mi.
  - Gate PASS.
  - **Rollback:** revert. That is another rolling restart.

### Task 10: MDS requests and placement — **Chris approval**

**Files:** `cluster/rook-cluster/fs-pool.yaml`, under `spec.metadataServer`:
- `resources: {requests: {memory: 1664Mi}, limits: {memory: 4Gi}}`. The limit sets `mds_cache_memory_limit` to 0.5 × 4Gi = 2 GiB, from the 4 GiB default. The cache holds 0.89 GB today.
- `placement.podAntiAffinity`: preferred, weight 100, on `app=rook-ceph-mds` with topology `kubernetes.io/hostname`, so that a and b split across nodes.
- **Unless Chris has reported a memtest pass**, also `placement.nodeAffinity` required: `kubernetes.io/hostname NotIn [middle]`. Add a comment pointing at the memtest gate in docs/luma.md, to be removed once the verdict is in.

- [ ] **Step 1: The test.**
  - `ceph config get mds.fs-pool-a mds_cache_memory_limit` and the same for `mds.fs-pool-b`. Today both are `4294967296`.
  - After the change, both must be `2147483648`.

- [ ] **Step 2: Fit and prerequisites.**
  - Two allowed nodes must each have at least 1664 Mi free. After Tasks 5–8, top and bottom have about 7.9 and 6.0 GiB free, plus 1.5 GiB each if Task 9 ran.
  - Gate PASS, and Chris's go.

- [ ] **Step 3: Commit.**
  - Rook recreates the MDS pods.
  - Watch `ceph fs status fs-pool` until it settles on one `active` and one `standby-replay`, on different nodes and not on `middle` (unless that is allowed).
  - CephFS consumers stay Ready: `kubectl -n gramps get pods` and `kubectl -n immich get pods`.

- [ ] **Step 4: Repeat Step 1** (`2147483648`).
  - `MDS_CACHE_OVERSIZED` is absent, and the gate PASSes.
  - **Rollback:** revert.

### Task 11: nfs-ganesha request and placement — **Chris approval**

**Files:** `cluster/rook-cluster/ceph-nfs.yaml`, under `spec.server`:
- `resources: {requests: {memory: 5Gi}}`, with no limit. Comment:
  - the bounded cache: MDCACHE `Entries_HWMark` defaults to 100000, and 100,008 caps were measured;
  - no override path exists in Rook 1.19 with Ganesha 5.9;
  - the upstream question.
- `placement.nodeAffinity`: required, `NotIn [middle]`, on the same memtest condition as Task 10.

- [ ] **Step 1: Fit and prerequisites.**
  - One allowed node must have at least 5120 Mi free. This is why Task 11 comes after the Prometheus reclaim: top projects to 6.2 GiB free without Task 9, and 7.8 GiB with it.
  - Gate PASS, and Chris's go.
  - Chris confirms that no copy into `/family` or `/archive` is running on rowlett.

- [ ] **Step 2: Commit.** The pod uses RollingUpdate. Watch with Monitor:
  - the new nfs pod becomes Ready on an allowed node, and the old one is gone;
  - `rook-ceph-nfs-satellites-lb` still shows `192.168.0.230`;
  - the ganesha log shows the server initialised, and the grace period ends;
  - `ceph tell mds.fs-pool-a session ls` lists the new `nfs.satellites` client.

- [ ] **Step 3: Client proof.** Chris, on rowlett: `ls /mnt/family | head`, and read one file. Expected: no `Stale file handle`, no EIO.

- [ ] **Step 4: Expect `MDS_CLIENT_RECALL` to clear.** The cache is empty after the restart. The warning returns once a walk refills 100k entries; that is still the normal WARN set.
  - **Rollback:** revert.

### Task 12: Exit verification and records

**Files:**
- Modify this plan's Status section (add a results table).
- Modify `docs/luma.md`: the 0b row in the phase table, and the "Memory headroom" gate.

- [ ] **Step 1: Headroom, before and after.**
  - Per-node `Allocated resources`, set against the baseline table and the projection.
  - **Exit:** the net gain is at least 8 GiB. That holds only if Task 9 ran. Otherwise report the measured shortfall and the options in the follow-ups.

- [ ] **Step 2: Real use.** Run `max_over_time((100*(1 - node_memory_MemAvailable_bytes/node_memory_MemTotal_bytes))[7d:5m])`. **Exit:** under 85% on every node.

- [ ] **Step 3: Rules.**
  - `scripts/check-prometheus-rules.sh` PASSes, and `platform:prometheusrule_selftest:loaded` = 1.
  - **Exit:** the test rule is visible in `/api/v1/rules`, and the stock groups are loaded (for example `kubernetes-apps`, which holds `KubePodCrashLooping`).

- [ ] **Step 4: Ceph.** **Exit:** the Task 1 gate PASSes; Ceph is back to the normal WARN noise.

- [ ] **Step 5: Logs.** **Exit:** `scripts/test-fluent-bit-exclusion.sh` PASSes, and Task 2's live result is recorded.

- [ ] **Step 6: Records.**
  - Commit the docs.
  - Update these memory notes:
    - `known_alert_rules_not_loading`: resolved, with the new label convention;
    - `known_middle_mem_requests`: the new per-node numbers;
    - `known_issues`: the ganesha cache finding, the router syslog finding, and the middle placement rule that must be lifted after the memtest.

## Follow-ups and out of scope (surface, don't do)

- **The dead ConfigMap rules** (the `luma-homelab-alert-rules` generator): convert them or delete them. Their `CephClusterHealthWarning` would fire permanently on the SMR noise.
- **`gonic-alerts`** references an unscraped job and the removed `music-sync` CronJob.
- **Unmonitored control plane:** etcd, controller-manager, scheduler and kube-proxy have no metrics (Talos `bind-address`, S4).
- **Watchdog** could be wired to a real dead man's switch.
- **Lift the `NotIn [middle]` placement** on MDS and NFS after the memtest verdict.
- **Upstream Rook:** a CephNFS ganesha config override (MDCACHE `Entries_HWMark`) would reclaim about 4 GiB.
- **Extra reclaim, not in the spec:**

  | pod | request | use |
  |---|---|---|
  | Argo repo-server (×2) | 1 Gi each | 195 Mi or less |
  | immich-machine-learning | 1 Gi | 274 Mi |
  | Grafana sidecars | — | about 300 Mi each (their watch scope) |

- **OOM protection.** A Burstable pod's `oom_score_adj` is about 900 or more, so a request protects through scheduling and eviction ranking, not through the kernel OOM score. A PriorityClass for MDS, NFS and HA would be the next step.
