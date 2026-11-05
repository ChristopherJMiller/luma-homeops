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

## Cross-plan: the galaxy runner serves `realliance/ci-dispatch` too (runner classes Task 5, 2026-11-04)

Runner classes N5. Commit `f5f71a53` (pushed at 19:38:29Z) gives the `galaxy` runner in `forgejo-runner` a second repo-scoped connection, `galaxy-ci-dispatch`, for `realliance/ci-dispatch`. The same change rotated the `galaxy-forge-bot` connection to a new runner row. The secret change alone does not roll the Deployment, so `kubectl rollout restart` at 19:39:58Z restarted it. The new pod declared both connections at 19:40:00Z.

- The runner-idle check in B2 Step 1, Task 9 Step 1 and every later enforcer restart now covers both repos the runner serves: `/repos/realliance/forge-bot/actions/tasks` and `/repos/realliance/ci-dispatch/actions/tasks`. It also covers the general runner's `/repos/realliance/realliance.net/actions/tasks`, as the note for Task 3 Part B says.
- The CI hold in the same STOP covers branch pushes to `realliance/ci-dispatch`, since each one starts a `galaxy` run. The runner classes plan holds them itself (its N3) while Task 8 Steps 1 to 11 or Task 9 is open.
- No NetworkPolicy changed. The pod keeps its name prefix, namespace and labels, so B2's selectors still match it.

## Cross-plan: forge-bot and ci-dispatch moved to `realliance-ops` (bots-to-realliance-ops Tasks 3 and 4, 2026-11-04)

Forgejo transferred `realliance/ci-dispatch` (id 240) at 21:27:18Z and `realliance/forge-bot` (id 94) at 21:30:35Z into `realliance-ops`, which is now public. Runner rows 17 (`galaxy-forge-bot`) and 18 (`galaxy-ci-dispatch`) match jobs by repo id, so the `galaxy` runner serves both repos unchanged. No NetworkPolicy and no runner config changed.

- The old API paths now answer 301, and `curl` without `-L` prints no count. The runner-idle check in B2 Step 1, Task 9 Step 1 and every later enforcer restart uses `/repos/realliance-ops/forge-bot/actions/tasks` and `/repos/realliance-ops/ci-dispatch/actions/tasks`. The general runner's check keeps `/repos/realliance/realliance.net/actions/tasks`.
- Task 8 Step 5's job script runs `git ls-remote https://git.realliance.net/realliance-ops/forge-bot HEAD`. Git follows the old path's redirect, but the redirect lasts only until someone creates a repo of that name in `realliance`.
- The CI hold in the same STOP covers branch pushes to both `realliance-ops` repos. Only `realliance-ops` Owners can push to them now.

## B1a: the canary, no policy yet (Task 6, 2026-11-05)

**B1a_at=2026-11-05T21:21:17Z.** That is when `scripts/netpol-verify.sh baseline` passed on all three nodes. Task 7 (B1b) follows within the hour. Until its policies are in, every probe dials the BMCs, the KVM, and router SSH every 300 s.

### Answers

- **Interval:** 300 s (Chris, 2026-11-03). That gives `PROBE_INTERVAL: "300"` and readiness `find /tmp/ready -mmin -11`. Task 7's alert uses `for: 15m`.
- **The Step 6 STOP:** pre-approved (Chris, 2026-11-05), conditional on every check matching the plan.
- **The own-BMC skip** (below): it changes the pre-approved checks, so the controller asked Chris. Chris approved pushing `3c1039ce` (2026-11-05, AskUserQuestion).

### What deployed

- Argo app `netpol-canary`, from commits `123e42bc` and `d29886dc` (the review fixes) plus `3c1039ce` (the own-BMC skip). It renders:
  - Namespace `netpol-canary`, with PodSecurity `enforce: restricted`;
  - DaemonSet `netpol-probe`, in `PROBE_MODE=baseline`;
  - Deployment `netpol-echo`, agnhost porter on 9376 and 9377;
  - Services `netpol-echo` (ClusterIP) and `netpol-echo-pod` (headless);
  - no NetworkPolicy.
- Deployed ConfigMap: **`netpol-probe-6247dhf725`**. The first push ran `netpol-probe-b29fh85tmd`.
- **N: 26 rows in matrix.txt** (1 dns, 3 open, 22 dropped). Each probe skips its own node's BMC, so **every node runs 25 canary rows, 21 of them dropped**. In runner scope a node runs 22 rows, 19 dropped; a caller with no node name gets 23 rows, 20 dropped. Task 7 expects `rows 25/25 deny-logged 21/21` per node.
- Image `registry.k8s.io/e2e-test-images/agnhost:2.66.1@sha256:6e2eef87…66a4`. `top pod`: 2m / 1Mi per probe, and 1m / 9Mi for echo.

### Step 0 (20:48:40Z)

- A3b_start + 24 h was at 18:41:00Z.
- `idle`: `PASS … packet_id=0 packets=0 restarts=0 flags=ok` for top `9dcl7`, middle `9rdg7` and bottom `r5bd2`, then `idle: PASS (3 agents, 0 NetworkPolicies)`.
- The three agents have 0 restarts and no lastState. They started at A3b: 18:40:54Z, 18:40:54Z and 18:41:00Z. All run digest `d9350ce2…300d`.
- The kubelet restarts at 14:53–14:56Z (the PID cap) did not restart them.

### Gate G before (20:49Z)

- G1: 3 nodes Ready, none cordoned. 145/145 PGs active+clean.
- G2: Ceph HEALTH_WARN: MDS_CLIENT_RECALL plus the muted AUTH_INSECURE_*. Argo: 61 apps, all Synced, only `rook` and `rook-cluster` Degraded.
- G3: the edge snapshot was re-baselined (`edge-t6-before.txt` in scratch). It has 51 rows: 35 host rows and 16 probes, with no 000 or err row.
  - Hosts: 16×200, 14×302, 1×303, 3×401, 1×404. Probes: 5×200, 3×302, 3×401, 2×403, 3×404.
  - That is 16 probes, not the 11 of the Pages note: the Forgejo sign-in probes landed since.
- G4: no non-Running pods, 0 NetworkPolicies. The only Warning event is the routine postgres-operator `ResourceLimits` one in `chrismillerxyz`.

### Push 1: the canary (20:50:10Z)

- `7b0949b0..d29886dc`, a fast-forward with no force; it carried exactly `123e42bc` and `d29886dc`.
- Renovate then pushed `4ff430e4` (the `ha-reload` python digest) and `3bd5352c` (the immich `extensions-job` Postgres digest). Argo synced the cluster to `3bd5352c`.
- **First-run race.** At 20:50:20Z the first runs on bottom and middle failed two rows:
  - `open refused netpol-echo…:9376`;
  - `dropped other netpol-echo-pod…:9378`.

  The echo pod was not Ready yet, so the ClusterIP had no endpoints and the headless name had no record. top's first run passed. The second runs (20:55:19–23Z) passed these rows on every node. Expect the same one-run failure whenever the echo pod is new; it clears 300 s later.
- The DaemonSet stayed at 2/3, with no restarts and no crash loop.

### Baseline 1 (20:56:08Z): FAIL, middle's own BMC

top 26/26 and bottom 26/26. middle 25/26, with one failure: `FAIL dropped timeout 192.168.0.117:443`.

- `192.168.0.117` is **middle's own BMC**. The map in `tools/bmcctl/src/bmcctl/nodes.py` is top .119, middle .117, bottom .151.
- Middle's host cannot reach its own BMC, because NC-SI on `enp35s0f0` drops unicast from the host (known since 2026-10-30, and the reason ipmi-exporter avoids middle). The row can never answer from middle, so after Task 7 it would always pass as "dropped" without proving anything.
- Every other row answered on all three nodes. The echo pod refused the 9378 row, which counts as an answer; the rest were open.

**Fix `3c1039ce`, `netpol-canary: each probe skips its own node's BMC`.**
- `probe.sh`:
  - `probe_own_bmc NODE_NAME` reads a `NODE_BMCS` default.
  - `probe_rows` skips the row that dials the probe node's own BMC. With no known node it skips nothing, which fails closed.
  - Every BMC is still dialled from the other two nodes.
- A static test pins `NODE_BMCS`, and the `NODE_IPS` ring, to bmcctl's `NODES`, and the BMCs to the matrix's BMC rows. The `netpol-manifests` hook runs on that file too.
- `baseline` checks each node against its own N: matrix.txt expanded with that pod's node name and host IP.

### Push 2: the own-BMC skip (21:20:28Z)

- `3bd5352c..3c1039ce`, a fast-forward with no force; it carried exactly `3c1039ce`.
- At 21:20:50Z: the app was Synced/Healthy at `3c1039ce`, and the DaemonSet was 3/3/3 on `netpol-probe-6247dhf725` (generation 2, observed 2). Probe pods `lqnml` (middle), `r8h7n` (top) and `rkwn5` (bottom) started at 21:20:31–35Z and were Ready with 0 restarts. Echo was 1/1, the same pod as push 1.

### Baseline 2 (21:21:15–17Z): PASS

`SUMMARY 25/25 baseline` on middle, top and bottom, then `baseline: PASS (3 nodes × 25 rows answer)`. The exec timeout was 290 s per pod. The run had 75 PASS lines and 0 FAIL.

| Expect | Row | Target | top | middle | bottom |
|---|---|---|---|---|---|
| dns | cluster DNS (CoreDNS) | `kubernetes.default.svc.cluster.local:53` | open | open | open |
| open | echo server, same namespace | `netpol-echo.netpol-canary.svc.cluster.local:9376` | open | open | open |
| dropped | echo server, port the policy does not allow | `netpol-echo.netpol-canary.svc.cluster.local:9377` | open | open | open |
| dropped | echo pod IP direct, headless, unserved port | `netpol-echo-pod.netpol-canary.svc.cluster.local:9378` | refused | refused | refused |
| open | forge via the WAN /25 and NAT reflection | `git.realliance.net:443` | open | open | open |
| open | public internet | `1.1.1.1:443` | open | open | open |
| dropped | BMC (top's own; top skips it) | per node | skipped (own BMC) | `192.168.0.119:443` open | `192.168.0.119:443` open |
| dropped | BMC (bottom's own; bottom skips it) | per node | `192.168.0.151:443` open | `192.168.0.151:443` open | skipped (own BMC) |
| dropped | KVM | `192.168.0.2:80` | open | open | open |
| dropped | KVM | `192.168.0.2:443` | open | open | open |
| dropped | router SSH | `192.168.0.1:22` | open | open | open |
| dropped | router DNS over TCP (router input) | `192.168.0.1:53` | open | open | open |
| dropped | API server ClusterIP | `10.96.0.1:443` | open | open | open |
| dropped | API server on a node | `192.168.0.240:6443` | open | open | open |
| dropped | API server on the control-plane VIP (Talos, held by one node) | `192.168.0.5:6443` | open | open | open |
| dropped | Forgejo ClusterIP | `forgejo-http.forgejo.svc.cluster.local:3000` | open | open | open |
| dropped | Traefik LB | `192.168.0.7:443` | open | open | open |
| dropped | mosquitto LB | `192.168.0.8:1883` | open | open | open |
| dropped | CephNFS LB | `192.168.0.230:2049` | open | open | open |
| dropped | registry LB | `192.168.0.231:5000` | open | open | open |
| dropped | Talos apid on a node | `192.168.0.240:50000` | open | open | open |
| dropped | Talos apid on the probe's own node (nftables input chain, v1.1.3) | per node | `192.168.0.240:50000` open | `192.168.0.241:50000` open | `192.168.0.242:50000` open |
| dropped | Talos apid on another node | per node | `192.168.0.241:50000` open | `192.168.0.242:50000` open | `192.168.0.240:50000` open |
| dropped | kubelet on another node | per node | `192.168.0.241:10250` open | `192.168.0.242:10250` open | `192.168.0.240:10250` open |
| dropped | zigbee2mqtt through the WAN address (router input) | `152.44.247.88:8585` | open | open | open |
| dropped | BMC (middle's own; middle skips it) | per node | `192.168.0.117:443` open | skipped (own BMC) | `192.168.0.117:443` open |

### Gate G after

After push 1 (21:07Z):
- G1: 3 nodes Ready; 145/145 PGs active+clean.
- G2: the same Ceph warn codes. Argo had 62 apps. The 61 kept their sync and health status; their revision moved `7b0949b0` → `3bd5352c` (Renovate, expected). The new `netpol-canary` was Synced/Progressing, held there by middle's probe.
- G3: the edge diff against before is empty after normalising `code_challenge|state|nonce`, and the cert CNs are identical.
- G4: no non-Running pods, 0 NetworkPolicies.

After push 2 (21:21–21:22Z):
- G1: 3 nodes Ready; 145/145 PGs active+clean.
- G2: the same Ceph warn codes. Argo: 62 apps, the 61 unchanged, 41 git-sourced apps at `3c1039ce`, and `netpol-canary` Synced/Healthy.
- G3: the edge diff is empty again (51 rows).
- G4: no non-Running pods, 0 NetworkPolicies. The agents still have 0 restarts, and `idle` at 21:21:57Z gave `PASS (3 agents, 0 NetworkPolicies)` with `packet_id=0`.
- One Warning event lay outside netpol-canary: one failed readiness probe on `media/mm-plex` at 21:14:20Z (count 1). The pod has been Ready since 14:53Z with 0 restarts. With 0 NetworkPolicies no agent has queued a packet, so the canary did not cause it.

No rollback trigger fired, and break-glass was not used.

### Carries

- **Task 7:**
  - per node, `rows 25/25 deny-logged 21/21`;
  - the first-run race described earlier;
  - option (a): the echo ingress also allows TCP 9378 from the probe, so the pod-IP row isolates the probe's egress;
  - the F5 JSON6902 patch for `kind: NetworkPolicy`, with `components:`/`patches:` after `resources:`.
- **Task 8:**
  - the runner's counts are 22/19 when it knows its node, or 23/20 without one;
  - `test_runner_job_egress_confined` must skip the `$HOST_IP` and `$PEER_IP` rows, and the row for the runner node's own BMC.
