# NetworkPolicy enforcement ledger

## Baseline (2026-11-03, before A1)

- G1: 3 nodes Ready, none SchedulingDisabled; Ceph 145 pgs, all active+clean.
- G2: Ceph HEALTH_WARN ("1 clients failing to respond to cache pressure"; auth-insecure checks muted). Argo: only `rook` and `rook-cluster` Degraded.
- G3: edge snapshot, 40 hosts; saved to scratch `edge-a1-before.txt`.
- G4: no non-Running/Succeeded pods. NetworkPolicies: 5 in `argo-cd`, 2 in `metallb-system` (7 total).

## Answers

- 2026-11-03: Chris pre-approved A1 on the condition that Step 2 proves both policies are orphans. It did.
- 2026-11-03 (Task 3 Step 11): (a) Chris pre-approved the A3a push, on condition that the static tests are green, `idle` exits 2 before deploy, and Gate G before equals the A2 after-state. (b) **The §6.5 break-glass is pre-approved as a standing S2/S4 exception for this rollout.** Each §6.5 command can run without its own STOP, and Chris is told right after any use. Each rollback renders nothing (`resources: []`); the Argo Application is never deleted.

## A1 - removed the two orphaned MetalLB policies

Proof of orphan: no Argo app tracks a NetworkPolicy in `metallb-system`; 0 pods with `instance=metallb-release`; 2 policies present.

Objects as deleted, with server-set fields (uid, resourceVersion, creationTimestamp, generation) removed so `kubectl apply -f` can re-create them:

```yaml
apiVersion: v1
items:
  - apiVersion: networking.k8s.io/v1
    kind: NetworkPolicy
    metadata:
      labels:
        app.kubernetes.io/component: controller
        app.kubernetes.io/instance: metallb-release
        app.kubernetes.io/managed-by: Helm
        app.kubernetes.io/name: metallb
        app.kubernetes.io/version: 0.14.8
        argocd.argoproj.io/instance: metallb-release
        helm.sh/chart: metallb-6.3.16
      name: metallb-release-controller
      namespace: metallb-system
    spec:
      egress:
        - {}
      ingress:
        - ports:
            - port: 7472
              protocol: TCP
            - port: 9443
              protocol: TCP
      podSelector:
        matchLabels:
          app.kubernetes.io/component: controller
          app.kubernetes.io/instance: metallb-release
          app.kubernetes.io/name: metallb
      policyTypes:
        - Ingress
        - Egress
  - apiVersion: networking.k8s.io/v1
    kind: NetworkPolicy
    metadata:
      labels:
        app.kubernetes.io/component: speaker
        app.kubernetes.io/instance: metallb-release
        app.kubernetes.io/managed-by: Helm
        app.kubernetes.io/name: metallb
        app.kubernetes.io/version: 0.14.8
        argocd.argoproj.io/instance: metallb-release
        helm.sh/chart: metallb-6.3.16
      name: metallb-release-speaker
      namespace: metallb-system
    spec:
      egress:
        - {}
      ingress:
        - ports:
            - port: 7472
              protocol: TCP
            - port: 9443
              protocol: TCP
      podSelector:
        matchLabels:
          app.kubernetes.io/component: speaker
          app.kubernetes.io/instance: metallb-release
          app.kubernetes.io/name: metallb
      policyTypes:
        - Ingress
        - Egress
kind: List
metadata:
```

After the delete: `metallb-system` has no NetworkPolicies. TCP checks ok for .6:22, .7:443, .8:1883, .230:2049, .231:5000; `wg-gateway` LB IP `192.168.0.232`.
Gate G after: nodes 3 Ready; Ceph HEALTH_WARN, 145 active+clean; Argo only `rook`/`rook-cluster` Degraded; no bad pods; netpols now 5 in `argo-cd` only; edge diff empty once per-request OAuth PKCE/state values are normalised.

## A2 — Argo's chart policies off (2026-11-03)

Chris pre-approved A2 on 2026-11-03, conditional on render check 5 before and 0 after, and Gate G before matching the Task 1 baseline. Both held.
Render check (temp copy of chart, `helm template`): 5 NetworkPolicies before, 0 after.
Commit `47eb70d8` adds `argo-cd.global.networkPolicy.create: false` in `cluster/argo-cd/values.yaml`; pushed 10:22 local.
Gate G before: 3 nodes Ready; Ceph HEALTH_WARN (warn codes MDS_CLIENT_RECALL plus the muted AUTH_INSECURE_*), 145/145 active+clean; Argo only `rook`/`rook-cluster` Degraded; no bad pods; 5 netpols, all `argo-cd`. `scripts/ceph-gate.sh` latency check read 1278 ms (>= 1000 ms), which is the SMR slow-op noise, not a PG state change.
Sync: `argo-cd` reached Synced/Healthy at the new sha within 10 s of the wait starting.
Gate G after: `kubectl get netpol -A` prints `No resources found`; same nodes, Ceph and Argo state as before (ceph-gate latency 1238 ms); no bad pods; edge diff empty (PKCE/state/nonce normalised), `ci.chrismiller.xyz` row included.

## A3a — the enforcer on `bottom` (2026-11-03)

**A3a_start=2026-11-03T18:35:10Z** (agent Ready on `bottom`). Task 4 (A3b) starts no earlier than **2026-11-04T18:35:10Z**, gated on `scripts/netpol-verify.sh idle` PASS with restarts 0 and `packet_id=0`.

**How to roll back (pre-approved; Global Constraints "render nothing").** Use one commit. Edit `cluster/kube-network-policies/kustomization.yaml` so that, below its header comment, it reads exactly:

```yaml
# netpol: rolled back (Global Constraints)
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources: []
```

- **The edit:** add the marker line, replace the `resources:` list with `[]`, and delete the `patches:` block. Leave `upstream-install.yaml`, the other files and `cluster/applications/kube-network-policies.yaml` as they are. Never delete the Application.
- **pre-commit accepts this state.** `netpol-manifests` asserts the empty render and an unchanged Application, keeps the sha check, and skips the deployed-state tests with a reason. This was proven in a scratch copy with the real hooks, with no commit (fix round 1).
- **The marker is required.** `resources: []` without it fails on purpose, and the failure message names the marker.
- **Push and verify.** Argo prunes all six objects (`allowEmpty: true`). Verify with `kubectl -n kube-system get ds kube-network-policies`, which should report NotFound.
- **Leftover nftables table.** With `--fail-open=true` the agent leaves its nftables table on the node until the next reboot. Its set is empty and its rules are flagged `bypass`, so it is harmless (§6.5 step 3).
- **Redeploy** with `git revert` of the rollback commit.

Task 4 should also update the rollback comments in that kustomization and in the Application to name the marker. Task 4 edits `cluster/` anyway; fix round 1 left `cluster/` untouched.

Pre-approved (Answers, Task 3). All three conditions held before the push.

S1 (read-only, 18:25Z):
- `bottom` `/var/run/netns` lists `cni-*`; nothing listens on `:9080`; `/var/run/nri` is absent.
- The kernel has `CONFIG_NETFILTER_NETLINK_QUEUE=y`, `NFT_QUEUE=y`, `NF_CONNTRACK_LABELS=y` and `NFT_CT=y`, and no nfqueue was bound.
- There are 0 NetworkPolicies. `bottom` memory requests are at 86%.

Upstream: `install.yaml` v1.1.3 has sha256 `a40594785fce33ded02582117f3b78123af53c62f3b73d514f62c5541974984a` and blob `165e7129`, matching the pins. Its pod labels are `tier: node`, `app: kube-network-policies` and `k8s-app: kube-network-policies`, so `app=kube-network-policies` holds (F6).

The v1.1.3 source was read for these facts:
- Metric names. `packet_count`, `unresolved_ip_count` and `packet_process_time` are vectors that appear only after their first observation.
- The flag log format: `"flag" name="…" value="…"`.
- Queue gating. Every queue rule, including DNS prerouting, looks up `@podips-v4/v6`, and `divertAll` is false in the standard flavour.

Static tests (`scripts/test_netpol_manifests.py`): 5 failed before the manifests existed, then 5 passed. The fifth test, which checks that the PodMonitor selects the DaemonSet's pods on a port they expose, was added for F6.

`idle` before the deploy: exit 2, `no kube-network-policies agent pods`.

Commit `fe14b177`, pushed 18:34:57Z. Argo synced it at 18:35:07Z. The pod was created at 18:35:07Z, pulled its image in 1.6 s and was Ready at 18:35:10Z, so `numberReady/desiredNumberScheduled` = `1/1`. Both `applications` and `kube-network-policies` were Synced/Healthy at `fe14b177` by 18:35:16Z.

Checks after the deploy:
- The running `imageID` is `…@sha256:d9350ce2…300d`, and the log's build commit is `d3fc697c`.
- The kernel queue on `bottom` reads `98 1 0 2 128 0 0 0 1`: bound, nothing queued, no drops, `id_sequence` 0.
- `idle` at 18:36:23Z and again at 18:37:43Z: `PASS bottom kube-network-policies-2q4mz packet_id=0 packets=0 restarts=0 flags=ok`, then `idle: PASS (1 agents, 0 NetworkPolicies)`.
- `top pod`: 1m / 18Mi, then 1m / 20Mi. The Expected is <50m / <64Mi, so there is no gap to record (D12).
- `/metrics` has `nfqueue_{packet_id,queue_total,queue_dropped,user_dropped}{queue="98"}` = 0 and `verdict_log_dropped` = 0. It also has `packet_process_duration_microseconds_count 0`, which shows no packet was processed. `packet_count`, `unresolved_ip_count` and `packet_process_time` are absent, as expected before any packet.
- Prometheus: `up{namespace="kube-system",pod=~"kube-network-policies-.*"}` is one series, value 1, instance `192.168.0.242:9080`. Every alert input series is present, and every alert expression evaluates to empty.
- `scripts/check-prometheus-rules.sh` exits 0 (`selected=39 groups=42 alerting=170 recording=82`). The `kube-network-policies` group is loaded from its uid file, and all 3 rules are `ok` and `inactive`. The only WARN is the existing `WARN unselected music-streaming/gonic-alerts`.

Gate G before (18:26Z):
- 3 nodes Ready.
- Ceph HEALTH_WARN, with warn codes MDS_CLIENT_RECALL plus the muted AUTH_INSECURE_*. 145/145 PGs active+clean.
- Argo: only `rook`/`rook-cluster` Degraded, 58 apps.
- No bad pods and 0 NetworkPolicies.
- Edge snapshot: 40 rows, identical to the A2 after-state.
- ceph-gate latency 1313 ms. This is SMR noise and not a criterion.

Gate G after (18:36–18:37Z): identical to before, apart from the new app (59 apps).
- Same warn codes, 145/145 active+clean, ceph-gate latency 1313 ms.
- No bad pods, 0 NetworkPolicies, and no Warning events since the push.
- The edge diff is empty after the `code_challenge|state|nonce` normalisation; the `ci.chrismiller.xyz` row is included.

No rollback trigger fired, and break-glass was not used.
- 2026-11-03 (realliance-pages Task 1): the edge snapshot now has 4 more hosts and 2 more probes. This plan's next Gate G must re-baseline first, since an old before-file no longer lines up.

## A3b: the enforcer on all three nodes (2026-11-04)

**A3b_start=2026-11-04T18:41:00Z** (all three agents Ready). Tasks 6 and 7 start no earlier than **2026-11-05T18:41:00Z** and run back to back. Task 5 can run during this soak.

Pre-approved (Answers, 2026-11-03), on condition that the 24 h idle gate passes and Gate G is clean. Both held, so the Step 5 STOP needed no new question.

24 h gate (Step 1) at 18:36:24Z: `PASS bottom kube-network-policies-2q4mz packet_id=0 packets=0 restarts=0 flags=ok`, then `idle: PASS (1 agents, 0 NetworkPolicies)`. `top pod`: 1m / 26Mi.

S1 on `top` and `middle` (read-only):
- Each lists 54 `cni-*` entries in `/var/run/netns`. Nothing listens on `:9080`, `/proc/net/netfilter/nfnetlink_queue` is empty, and `/var/run/nri` is absent.
- Memory requests are at 85% on both, about 4.3 Gi below allocatable, so the 64Mi request preempts nothing.

Commit `a10e39b8` drops the `kubernetes.io/hostname` node selector; the render keeps the upstream selector `kubernetes.io/os: linux`. It also names the rollback marker in the comments of the kustomization and of `cluster/applications/kube-network-policies.yaml` (the Task 4 carry). Static tests: 1 failed with the new expectation, then 16 passed after the patch edit.

Gate G before (18:39–18:41Z), a fresh baseline for this task:
- 3 nodes Ready, none cordoned.
- Ceph HEALTH_WARN, with warn codes MDS_CLIENT_RECALL plus the muted AUTH_INSECURE_*. 145/145 PGs active+clean.
- Argo: 60 apps, only `rook`/`rook-cluster` Degraded.
- No bad pods and 0 NetworkPolicies.
- Edge snapshot: 46 rows (scratch `edge-t4-before.txt`).

Pushed at 18:40:49Z, and Argo synced at 18:40:52Z.
- The DaemonSet created the `top` and `middle` pods together at 18:40:52Z. They were Ready at 18:40:54Z and 18:40:55Z.
- Then it replaced `bottom`'s pod: `kube-network-policies-r5bd2` started and was Ready at 18:41:00Z.
- `numberReady/desiredNumberScheduled` = `3/3` at 18:41:00Z. All three run digest `d9350ce2…300d`, with 0 restarts.

Checks after the deploy:
- The kernel queue reads `98 1 0 2 128 0 0 0 1` on all three nodes: bound, nothing queued, no drops.
- `idle` at 18:41:16Z FAILED on all three: the `nfqueue_*` gauges were absent. An agent publishes them only after its first collection, and the pods were 16–24 s old. Every counter read 0, so no rollback trigger fired.
- `idle` at 18:41:53Z: three `PASS … packet_id=0 packets=0 restarts=0 flags=ok` lines, then `idle: PASS (3 agents, 0 NetworkPolicies)`.
- `top pod` at 18:41:53Z and 18:42:43Z: 1m / 20–22Mi per agent. The rollback limits are CPU 100m and memory 200Mi. No agent logged an `E`/`F` line.
- Prometheus `up{namespace="kube-system",pod=~"kube-network-policies-.*"}`: 3 series (`.240`, `.241` and `.242` on 9080), each 1. DaemonSet ready 3, desired 3. No `KubeNetworkPolicies*` alert is pending or firing.
- `scripts/check-prometheus-rules.sh` exits 0 (`selected=40 groups=43 alerting=178 recording=82`). The `kube-network-policies` group's 3 rules are `ok` and `inactive`. The only WARN is the existing `unselected music-streaming/gonic-alerts`.

Gate G after (18:41–18:42Z): identical to before. The warn codes are the same, all 145 PGs are active+clean, and no Argo app changed status. Gate G finds no bad pods, no NetworkPolicies and no Warning events since the push. The edge diff is empty after the `code_challenge|state|nonce` normalisation.

No rollback trigger fired, and break-glass was not used.
