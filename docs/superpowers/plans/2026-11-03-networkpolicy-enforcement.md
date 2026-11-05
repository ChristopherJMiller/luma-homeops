# NetworkPolicy Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce standard NetworkPolicy on galaxy with upstream kube-network-policies v1.1.3, run as an Argo-managed DaemonSet beside the Talos flannel. Prove it with a permanent canary on every node, then confine the forgejo-runner's egress, then turn Argo's own chart policies back on.

**Architecture:** The enforcer is a new Argo app, `kube-network-policies`, in `kube-system`. It renders upstream's `install.yaml` (vendored byte-for-byte) plus one kustomize patch. It lands on `bottom` first and then on all nodes, and it does nothing until a policy selects a pod. Reusable kustomize components in `cluster/components/netpol/` carry the policy shapes. The canary (`netpol-canary`) and the runner use the same components, so whatever the canary proves holds for the runner too. One re-runnable script, `scripts/netpol-verify.sh`, holds every live test in §9 of the spec. One pytest file, `scripts/test_netpol_manifests.py`, holds every static test and also runs as a pre-commit hook.

**Tech Stack:** Talos v1.10.9 / k8s v1.33.13, flannel v0.26.7, kube-proxy in nftables mode. kube-network-policies v1.1.3 (`standard`). Argo CD v3.5.3 (chart 10.9.2), kustomize components, kube-prometheus-stack (PodMonitor, PrometheusRule). agnhost 2.66.1. bash + kubectl + jq, Node 22 (devshell) / the runner image's Node, pytest + PyYAML (devshell `python`).

**Spec:** `docs/superpowers/specs/2026-11-03-networkpolicy-enforcement-design.md` (commit 7a59fd46), including "Answers, 2026-11-03 (Chris)". Read §3.4, §6 and §7 before any task. Where §6 already fixes a YAML body, this plan cites the section instead of repeating it.

## Global Constraints

### Pins (resolved read-only, 2026-11-03)

| What | Value | Source |
|---|---|---|
| Enforcer image | `registry.k8s.io/networking/kube-network-policies:v1.1.3@sha256:d9350ce205a63ba85c67795191304cef0318d084422ca44da49c4655eeba300d` | OCI index digest from `GET https://registry.k8s.io/v2/networking/kube-network-policies/manifests/v1.1.3` (header `Docker-Content-Digest`). Platforms: linux/amd64 `sha256:0a80c6a9…2ba8` (config `created` 2026-11-02T18:45:38Z), arm64, s390x. The two-week pin-age rule is waived for this image (Answers 4). |
| Enforcer fallback (only if v1.1.3 regresses; spec §5) | `…:v1.1.2@sha256:c671f98e444825d57c590c92da1b5e66070917c4d3dff1f9279775e6b73254fe` | same API. **Never below v1.1.1** (#380). |
| Vendored manifest | upstream `install.yaml` at tag `v1.1.3` (tag object `5478b9bf35e6ee70d4f836aa35652fe10f1199db`, commit `d3fc697c1acaa0c8872c262ff3e04d8299eac6b3`, tagged 2026-11-02T18:46:54Z), blob `165e712983bcca4e156ede0f7203731a578a0606`, file sha256 `a40594785fce33ded02582117f3b78123af53c62f3b73d514f62c5541974984a` | `https://raw.githubusercontent.com/kubernetes-sigs/kube-network-policies/v1.1.3/install.yaml`. **Upstream's file still names image `v1.1.2`**. The patch sets the real image, and Renovate is turned off for the vendored file. |
| Canary image | `registry.k8s.io/e2e-test-images/agnhost:2.66.1@sha256:6e2eef87d0dc77e070a7549bc5fdfa290c0fefe3359eb33c2ad1eda692fe66a4` | OCI index, built 2026-07-29. Alpine with bash, bind-tools (`dig`) and `/agnhost connect` (prints `TIMEOUT`/`REFUSED`/`DNS: …`/`OTHER: …` on stderr, exit 1; `--timeout` defaults to 0, so always pass `--timeout=3s`) |
| Runner image (gate) | exactly the `runner` container's current image string in `cluster/forgejo-runner/deployment.yaml` | it has `node` |

Re-check a digest with: `curl -sSLI -H 'Accept: application/vnd.oci.image.index.v1+json' https://registry.k8s.io/v2/<repo>/manifests/<tag> | grep -i docker-content-digest`.

### Values fixed by the spec (verbatim)

- Agent args, in order: `/bin/netpol`, `--hostname-override=$(MY_NODE_NAME)`, `--v=1`, `--log-verdicts=deny`, `--fail-open=true`, `--nfqueue-id=98`, `--metrics-bind-address=:9080`, `--disable-nri=true`. Resources: requests `cpu: 50m, memory: 64Mi`, limit `memory: 256Mi`. `priorityClassName: system-node-critical`, `tolerations: [{operator: Exists}]`, `updateStrategy.rollingUpdate.maxUnavailable: 1`, `hostNetwork: true`, container port `metrics` 9080. Drop the `/var/run/nri` hostPath and mount; keep `/var/run/netns` (§6.1).
- Nodes: top `192.168.0.240` (pod CIDR `10.244.3.0/24`), middle `.241` (`10.244.4.0/24`), bottom `.242` (`10.244.0.0/24`). flannel.1 is `10.244.X.0`, cni0 is `10.244.X.1`. Services are `10.96.0.0/12`, `kubernetes` is `10.96.0.1`.
- DNS selector: namespace `kube-system`, `k8s-app: kube-dns`, UDP+TCP 53. Traefik: namespace `traefik`, `app.kubernetes.io/name: traefik`. Prometheus: namespace `prometheus`, `app.kubernetes.io/name: prometheus`.
- Runner egress is exactly §6.3: DNS, `152.44.247.0/25` TCP 443, and `0.0.0.0/0` except the ten CIDRs listed in §6.3.
- **Never select pods in:** `kube-system`, `rook`, `metallb-system`, `prometheus`, `argo-cd` (except the chart's own set, Task 12), `telephony`, `home-assistant`. `dns` and `rabbitmq-system` are excluded too until Task 14 has proven `allow-from-nodes` (§6.2, §7.1.6).
- Every NetworkPolicy carries a comment that says what the pod needs. Rules allow by selector, never by ClusterIP or LB IP (§6.2).
- **Never set Talos `kubeNetworkPoliciesEnabled`** (§5, §8). Never run two enforcers.
- **No step runs during a Talos/k8s hop, a node drain or Ceph recovery** (§7, S5). #2506 stays paused until Task 8 (B2) is done (§8).

### How changes land

- **luma-homeops.** Edit, then run the static tests, then commit with `nix develop --command git commit …`. pre-commit runs yamlfmt, kustomize-build and the new `netpol-manifests` hook. If a hook rewrites a file, `git add` it and commit again. Never use `--no-verify` or `--no-gpg-sign` (S6); signing follows the repo's current config. Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **A push to `main` is a deploy**, because Argo syncs within ~3 min. A push that changes anything rendered into the cluster happens only after that task's **STOP**. A docs-only or ledger-only push needs no STOP.
- **Rollback changes what the child app renders.** For an app that already existed, that means `git revert` of the task's commit. For an app this plan creates (`kube-network-policies`, `netpol-canary`), it means a commit that sets that app's `kustomization.yaml` to `resources: []` and removes its `patches`/`components`; Argo then prunes everything (`allowEmpty: true`). **Never roll back by deleting an Application file.** No app here carries `resources-finalizer.argocd.argoproj.io`, so the root app would orphan the DaemonSet and leave it running.
- **STOP — ask Chris** means one AskUserQuestion that states the change, the Gate G evidence, what you will watch and the rollback trigger. The options are "approve: push, and roll back at once if a trigger fires" and "hold". Wait for the answer. A call that was rejected or interrupted may already have run, so read the live state before you redo anything.
- **Ledger:** `docs/superpowers/plans/2026-11-03-networkpolicy-enforcement.ledger.md`. Record Chris's answers, Gate G summaries, timestamps (soaks start and end), measurements and the saved YAML. No secrets, no hidden names.
- **Waits:** use `Monitor` for anything over 60 s, and never a sleep loop. Resume the 24 h and 1-week soaks from the ledger timestamps.

### Gate G (before every cluster-affecting push or `kubectl` write, and again afterwards)

- G1. No hop, drain or recovery. `kubectl get nodes` shows 3 `Ready` nodes and no `SchedulingDisabled`. Ceph PGs are all `active+clean`.
- G2. Run the `cluster-snapshot` skill and put its summary in the ledger. Expected baseline: Ceph `HEALTH_WARN`, with BLUESTORE slow-op noise only. In Argo, only `rook` and `rook-cluster` are not Healthy (Degraded).
- G3. Edge snapshot: `cd .claude/skills/edge-ingress && nix-shell -p openssl jq --run ./edge-snapshot.sh > $SCRATCH/edge-<task>-<before|after>.txt`, where `$SCRATCH` is your session scratchpad. Compare with `diff <(sed 's/ cert=.*//' before) <(sed 's/ cert=.*//' after)`.
- G4. `kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded` and `kubectl get netpol -A`, both recorded.
- After the change, repeat G2–G4. Expected: identical to the "before" state, apart from the intended change.

### Cluster, secrets, compute

- `export KUBECONFIG=/tmp/galaxy-kubeconfig`. If it is rejected, refresh it from Talos (CLAUDE.md). Run `talosctl` as `nix develop --command talosctl --talosconfig nodes/talosconfig -n <ip> -e <ip> …`, passing the path only and never reading the file.
- Never open `*.secret.yaml` or `nodes/*`. Static tests skip `*.secret.*`. Every repo grep uses `--exclude='*.secret.*'`. `git diff`/`show` that leaves the terminal uses `--no-textconv`.
- Public repo: no secret values and no hidden org, team or group names in files, the ledger or commit messages.
- Local compute is limited to 4 threads. Run no local docker or kind without asking Chris.

### Coordination with the forge-bot plan (`docs/superpowers/plans/2026-11-02-forge-bot-and-ci.md`)

- `cluster/forgejo-runner/` is shared, and that plan's later tasks also change it (`efce651c` already changed the start command). Before Tasks 8 and 9, run `git log --oneline -- cluster/forgejo-runner` and re-read every non-secret file there, then build on what landed.
- **Runner idle**: `for r in realliance-ops/forge-bot realliance-ops/ci-dispatch realliance/realliance.net; do curl -sSf "https://git.realliance.net/api/v1/repos/$r/actions/tasks?limit=20" | jq -e '[.workflow_runs[] | select(.status=="running" or .status=="waiting")] | length == 0' >/dev/null && echo "$r idle" || echo "$r BUSY OR ERROR"; done`. Expected three `idle` lines. `-f` makes a non-200 (the old `realliance/forge-bot` path answers 301) fail loudly instead of printing nothing, and `jq -e` fails on a non-empty result; any `BUSY OR ERROR` line, or empty output, is not a pass. `galaxy` serves the first two repos; the general runner (`forgejo-runner-general`) serves the third. A repo that moves again means this list is stale: re-check it against `docs/forgejo.md`. In the same STOP, ask Chris to hold forge-bot CI pushes for the window: strict mode cuts denied flows, and the pod restarts.

### Break-glass

§6.5, exactly as written there. Chris answers whether it is pre-approved at Task 3's STOP, and the ledger records the answer. If it is not pre-approved, each §6.5 command is its own STOP.

## Review Focus

1. **A pod dialing its own node** (Talos apid, the kubelet, the API server on `$HOST_IP`). That traffic is delivered locally and judged on v1.1.3's `input` hook, not on the `postrouting` hook that §3.4 describes. Expected: it is dropped on every node. Test: matrix row `dropped all $HOST_IP 50000` (Task 5), exercised by `canary` (Task 7) and `runner` (Task 8).
2. **A refused or down target read as "blocked".** An RST means the packet got through, and a dead target passes for free. Expected: `refused` fails when enforced, and the baseline proves that every must-drop target answers. Test: `selftest` judge cases (Task 5) and `baseline` (Task 6).
3. **Traffic that no policy selects gets judged anyway.** Causes include a policy that selects a hostNetwork pod (the #380 class), a node IP enrolled by mistake, or a stale IP→pod mapping. Expected: every deny line names a pod in a namespace that has a policy. Test: `stray` (Task 7), re-run in Tasks 8, 9, 12 and 13.
4. **The runner starting where enforcement is not live yet**: a new pod IP not yet enrolled (no NRI), a node reboot, or no agent on the node. Expected: it never registers or takes a job before its egress is enforced. Test: the gate's two-sided check in Task 8 (exit 1 before the policy, exit 0 after), and the restart test (Task 9).
5. **The permanent canary's own deny lines.** Roughly 17 dropped rows × 2–3 SYNs × 3 pods per run is about 180k lines/day at 60 s, all shipped to Azure (1 GB/day cap) and router syslog. Expected: volume within what Chris chose at Task 6. Test: the measurement step in Task 7.

## File map

| Path | Responsibility | Task |
|---|---|---|
| `cluster/applications/kube-network-policies.yaml`, `cluster/kube-network-policies/*` | the enforcer app (vendored upstream + patch + PodMonitor + PrometheusRule) | 3, 4 |
| `cluster/components/netpol/{default-deny,allow-dns,egress-internet-only}/` | reusable policy components (§6.2/§6.3) | 7 |
| `cluster/components/netpol/{allow-from-traefik,allow-from-prometheus,allow-same-namespace,egress-open}/` | adoption components, created at first use | 13 |
| `cluster/components/netpol/allow-from-nodes/` | the nine host-source addresses, created when proven | 14 |
| `cluster/applications/netpol-canary.yaml`, `cluster/netpol-canary/*` | permanent canary: probe DaemonSet, echo server, matrix, policies, alert | 5, 6, 7 |
| `cluster/forgejo-runner/kustomization.yaml`, `deployment.yaml` | runner consumes the components, plus the `netpol-gate` init container | 8 |
| `scripts/netpol-verify.sh` | every live test (`idle`, `selftest`, `baseline`, `canary`, `stray`, `runner`); read-only; exit 0 pass / 1 mismatch / 2 could not run | 3, 5–8 |
| `scripts/netpol-connect.js` | Node connect/DNS probe for the runner and its CI jobs | 5 |
| `scripts/test_netpol_manifests.py` | static tests; also the `netpol-manifests` pre-commit hook | 3, 4, 6, 7, 8, 13 |
| `docs/networkpolicy.md` | runbook (break-glass, verify, adding policy, sharp edges) | 10 |
| `~/Repos/realliance-sso/tests/forge/test_forge_ci.py` | `test_runner_job_egress_confined` (job context, §9.3) | 8 |

---

### Task 1: A1 — delete the two orphaned MetalLB policies

**Files:**
- Create: `docs/superpowers/plans/2026-11-03-networkpolicy-enforcement.ledger.md` (sections: Baseline, Answers, one per task)

**Interfaces:**
- Produces: the ledger, and the Gate G baseline (`$SCRATCH/edge-a1-before.txt`) that every later Gate G is compared with.

- [ ] **Step 1: Create the ledger and run Gate G (baseline).** Record G1–G4.
  Expected: 7 NetworkPolicies (5 in `argo-cd`, 2 in `metallb-system`); Ceph `HEALTH_WARN` with PGs active+clean; only `rook` and `rook-cluster` Degraded.
- [ ] **Step 2: Prove they are orphans (the "before" state of the test).**
  Run: `kubectl -n argo-cd get applications.argoproj.io -o json | jq -r '.items[] | .metadata.name as $n | .status.resources[]? | select(.kind=="NetworkPolicy" and .namespace=="metallb-system") | $n'`
  Expected: no output.
  Run: `kubectl -n metallb-system get pods -l app.kubernetes.io/instance=metallb-release --no-headers | wc -l; kubectl -n metallb-system get netpol --no-headers | wc -l`
  Expected: `0` then `2`.
- [ ] **Step 3: Save both objects to the ledger.** Paste the output of `kubectl -n metallb-system get networkpolicy metallb-release-controller metallb-release-speaker -o yaml` into a fenced block under "A1".
- [ ] **Step 4: STOP — ask Chris.** This is an unmanaged delete with no repo source (spec A1, S4).
- [ ] **Step 5: Delete them.**
  Run: `kubectl -n metallb-system delete networkpolicy metallb-release-controller metallb-release-speaker`
  Expected: two `deleted` lines.
- [ ] **Step 6: Verify.**
  Run: `kubectl -n metallb-system get netpol` → Expected: `No resources found`.
  Run: `for t in 192.168.0.6:22 192.168.0.7:443 192.168.0.8:1883 192.168.0.230:2049 192.168.0.231:5000; do timeout 3 bash -c "</dev/tcp/${t%:*}/${t#*:}" && echo "ok $t" || echo "FAIL $t"; done` → Expected: five `ok` lines.
  Run: `kubectl -n ops get svc wg-gateway -o jsonpath='{.status.loadBalancer.ingress[0].ip}'` → Expected: `192.168.0.232`.
  Then run Gate G after. Expected: edge diff empty, Ceph and Argo the same as baseline.
- [ ] **Step 7: Commit and push the ledger** (docs only): `docs: netpol ledger; A1 removed the two orphaned MetalLB policies`.

### Task 2: A2 — Argo's chart policies off

**Files:**
- Modify: `cluster/argo-cd/values.yaml`. Under `argo-cd.global`, add `networkPolicy: {create: false}` with the comment `# netpol plan A2: off until B4 (Task 12). Argo is the rollback path while enforcement rolls out.`

**Interfaces:**
- Produces: a cluster with zero NetworkPolicies, which is the precondition for `netpol-verify.sh idle`.

- [ ] **Step 1: Write the render check (it shows 5 now).**
  Run: `t=$(mktemp -d); cp cluster/argo-cd/{Chart.yaml,Chart.lock,values.yaml} "$t"/; nix develop --command sh -c "helm dependency build $t >/dev/null && helm template argo-install $t -n argo-cd | grep -c '^kind: NetworkPolicy$'"`
  Expected: `5`. Use a temp copy because `cluster/argo-cd/charts/` holds a stale gitignored 7.6.8 tarball.
- [ ] **Step 2: Add the value.**
- [ ] **Step 3: Re-run the Step 1 command.** Expected: `0`.
- [ ] **Step 4: Commit:** `argo-cd: chart NetworkPolicies off until enforcement is proven (netpol A2)`.
- [ ] **Step 5: Gate G, then STOP — ask Chris** (`argo-cd`).
- [ ] **Step 6: Push.** Monitor until `kubectl -n argo-cd get application argo-cd -o jsonpath='{.status.sync.status}/{.status.health.status}/{.status.sync.revision}'` prints `Synced/Healthy/<the new sha>` (timeout 10 min).
- [ ] **Step 7: Verify.** Run `kubectl get netpol -A`. Expected: `No resources found`. Then run Gate G after; the edge diff should be empty, including the `ci.chrismiller.xyz` row.
  Rollback trigger: `argo-cd` is Degraded, or any app stops syncing. Then `git revert` and push.
- [ ] **Step 8: Write the ledger, then commit and push it.**

### Task 3: A3a — the enforcer, on `bottom` only

**Files:**
- Create: `cluster/kube-network-policies/upstream-install.yaml` (vendored, byte-identical), `kustomization.yaml`, `daemonset-patch.yaml`, `podmonitor.yaml`, `prometheusrule.yaml`
- Create: `cluster/applications/kube-network-policies.yaml`, `scripts/netpol-verify.sh`, `scripts/test_netpol_manifests.py`
- Modify: `cluster/applications/kustomization.yaml` (add `kube-network-policies.yaml`), `.pre-commit-config.yaml`, `.renovaterc.json`

**Interfaces:**
- Produces:
  - Argo app `kube-network-policies` and DaemonSet `kube-system/kube-network-policies`, with pods labelled `app=kube-network-policies` and metrics on `<node IP>:9080`.
  - `scripts/test_netpol_manifests.py` with:
    - `render(path: str) -> list[dict]` (`kustomize build`, parsed);
    - `ENFORCER_IMAGE_RE = r"registry\.k8s\.io/networking/kube-network-policies:v(\d+)\.(\d+)\.(\d+)@sha256:[0-9a-f]{64}"`;
    - `ENFORCER_ARGS` (the list in Global Constraints);
    - `UPSTREAM_SHA256`.
  - `scripts/netpol-verify.sh`: `set -uo pipefail`, `KUBECONFIG` defaulting to `/tmp/galaxy-kubeconfig`, a `kc()` timeout wrapper as in `scripts/check-prometheus-rules.sh`, exit codes 0/1/2, read-only, safe from `Monitor`. Subcommand `idle` is added here.

- [ ] **Step 1: Read state (S1), read-only.**
  Run: `nix develop --command talosctl --talosconfig nodes/talosconfig -n 192.168.0.242 -e 192.168.0.242 ls /var/run/netns` → Expected: `cni-…` entries.
  Run: `… -n 192.168.0.242 -e 192.168.0.242 netstat -l -t | grep -c ':9080 '` → Expected: `0`.
  Run: `kubectl get netpol -A` → Expected: `No resources found` (A1 and A2 are done).
- [ ] **Step 2: Write the failing static tests** in `scripts/test_netpol_manifests.py`:
  - `test_upstream_vendored_byte_identical`: the sha256 of `cluster/kube-network-policies/upstream-install.yaml` == `a40594785fce33ded02582117f3b78123af53c62f3b73d514f62c5541974984a`.
  - `test_enforcer_daemonset`: render `cluster/kube-network-policies` and take the one DaemonSet.
    - Its image fullmatches `ENFORCER_IMAGE_RE` with version ≥ (1,1,1).
    - `args == ENFORCER_ARGS`; volumes `== {"netns"}`; mounts `== {"/var/run/netns"}`.
    - `hostNetwork`, `priorityClassName == "system-node-critical"`, `tolerations == [{"operator": "Exists"}]`, `maxUnavailable == 1`.
    - requests/limits as in Global Constraints; a port named `metrics` on 9080; env `MY_NODE_NAME` from `spec.nodeName`.
    - `nodeSelector == {"kubernetes.io/os": "linux", "kubernetes.io/hostname": "bottom"}`.
  - `test_enforcer_monitoring`: the PodMonitor in `kube-system` has label `release: prometheus`, selector `app: kube-network-policies` and port `metrics`. The PrometheusRule in `prometheus` has `release: prometheus` and alerts `{KubeNetworkPoliciesAgentNotReady, KubeNetworkPoliciesQueueDrops, KubeNetworkPoliciesVerdictLogDropped}`.
  - `test_enforcer_app`: the Application has path `cluster/kube-network-policies`, destination namespace `kube-system`, automated prune + selfHeal + allowEmpty, `ServerSideApply=true` in syncOptions and no `CreateNamespace`. It is listed in `cluster/applications/kustomization.yaml`.
- [ ] **Step 3: Run them.**
  Run: `nix develop --command python -m pytest -q -p no:cacheprovider scripts/test_netpol_manifests.py`
  Expected: 4 failed (missing files / kustomize build error).
- [ ] **Step 4: Write `netpol-verify.sh idle`.** Contract:
  - Exit 2 if any NetworkPolicy exists (`policies exist: idle check meaningless`), or if there are no pods with `-l app=kube-network-policies`.
  - For each agent, read `kubectl get --raw /api/v1/namespaces/kube-system/pods/<pod>:9080/proxy/metrics` and require all of:
    - Ready, 0 restarts;
    - `kube_network_policies_nfqueue_queue_total` present (the queue is bound);
    - `kube_network_policies_nfqueue_packet_id` == 0 (no packet ever queued since bind);
    - the sum of `kube_network_policies_packet_count` == 0 (absent counts as 0);
    - the pod log has a `"flag" name="<n>" value="?<v>"?` line for each of `fail-open=true`, `log-verdicts=deny`, `disable-nri=true`, `nfqueue-id=98`, `metrics-bind-address=:9080`, `v=1`;
    - no klog `E`/`F` lines (`^[EF][0-9]{4} `) and no `"syncing nftables rules"` line carrying `error`.
  - Print `PASS <node> <pod> packet_id=0 packets=0 restarts=0 flags=ok` (or `FAIL <node> <pod> <check>`), then `idle: PASS (<n> agents, 0 NetworkPolicies)`.
- [ ] **Step 5: Run it.** Run: `scripts/netpol-verify.sh idle`. Expected: exit 2, `no kube-network-policies agent pods`.
- [ ] **Step 6: Vendor upstream.**
  Run: `curl -sSfL https://raw.githubusercontent.com/kubernetes-sigs/kube-network-policies/v1.1.3/install.yaml -o cluster/kube-network-policies/upstream-install.yaml && sha256sum cluster/kube-network-policies/upstream-install.yaml`
  Expected: `a40594785fce33ded02582117f3b78123af53c62f3b73d514f62c5541974984a`.
- [ ] **Step 7: Write the app.**
  - `kustomization.yaml`: `resources: [upstream-install.yaml, podmonitor.yaml, prometheusrule.yaml]`, `patches: [{path: daemonset-patch.yaml}]`. Set **no** kustomization-level `namespace`, because the PrometheusRule lives in `prometheus`. Header comment: source tag, commit and sha256 from Pins, and the warning that upstream's own image line says v1.1.2.
  - `daemonset-patch.yaml`: the §6.1 outline with the image from Pins, plus `nodeSelector: {kubernetes.io/hostname: bottom}` and the comment `# A3a only; Task 4 removes this line`.
    - Strategic-merge deletions: `volumes: [{name: nri-plugin, $patch: delete}]` and, under the container, `volumeMounts: [{mountPath: /var/run/nri, $patch: delete}]`.
    - The spec's comments go on the args.
  - `podmonitor.yaml`: `kube-system/kube-network-policies`, `release: prometheus`, `namespaceSelector.matchNames: [kube-system]`, selector `app: kube-network-policies`, `podMetricsEndpoints: [{port: metrics, interval: 30s, path: /metrics}]`.
  - `prometheusrule.yaml`: `prometheus/kube-network-policies`, labels `release: prometheus`, group `kube-network-policies`, every alert `severity: warning`:
    - `KubeNetworkPoliciesAgentNotReady`, `for: 5m`: `kube_daemonset_status_number_ready{namespace="kube-system",daemonset="kube-network-policies"} < kube_daemonset_status_desired_number_scheduled{namespace="kube-system",daemonset="kube-network-policies"} or absent(kube_daemonset_status_desired_number_scheduled{namespace="kube-system",daemonset="kube-network-policies"})`.
    - `KubeNetworkPoliciesQueueDrops`: `increase(kube_network_policies_nfqueue_queue_dropped[10m]) > 0 or increase(kube_network_policies_nfqueue_user_dropped[10m]) > 0` (gauges of kernel counters; `increase` absorbs resets).
    - `KubeNetworkPoliciesVerdictLogDropped`: `increase(kube_network_policies_verdict_log_dropped[10m]) > 0`.
    - `NetpolCanaryFailing` is **not** here. It ships with the canary's policies in Task 7, so the baseline window cannot fire it.
  - `cluster/applications/kube-network-policies.yaml`: the shape of `immich.yaml`, with `destination.namespace: kube-system` and `syncOptions: [ServerSideApply=true, PrunePropagationPolicy=foreground]`. Header comment: runbook `docs/networkpolicy.md`, spec path, and "roll back by rendering nothing, never by deleting this file".
- [ ] **Step 8: Hooks and Renovate.**
  - `.pre-commit-config.yaml`: add `|cluster/kube-network-policies/upstream-install\.yaml` to yamlfmt's `exclude`. Add a local hook `netpol-manifests` with `language: system`, `entry: python -m pytest -q -p no:cacheprovider scripts/test_netpol_manifests.py`, `pass_filenames: false` and `files: ^(cluster/.*|scripts/test_netpol_manifests\.py)$`.
  - `.renovaterc.json` `packageRules`:
    - `{"matchManagers": ["kubernetes"], "matchPackageNames": ["registry.k8s.io/networking/kube-network-policies"], "groupName": "kube-network-policies", "groupSlug": "kube-network-policies", "automerge": false, "description": "The NetworkPolicy enforcer: every bump, digest included, changes the datapath on every node. Read the release notes; after merge re-run scripts/netpol-verify.sh canary (docs/networkpolicy.md). Never below v1.1.1."}`. The own group keeps it from blocking `group:allNonMajor`.
    - `{"matchFileNames": ["cluster/kube-network-policies/upstream-install.yaml"], "enabled": false, "description": "Vendored byte-identical from upstream v1.1.3; its image line (v1.1.2) is overridden by daemonset-patch.yaml."}`.
- [ ] **Step 9: Run the checks.**
  Run: `nix develop --command python -m pytest -q -p no:cacheprovider scripts/test_netpol_manifests.py && nix develop --command scripts/check-kustomizations.sh && jq -e . .renovaterc.json >/dev/null && echo ok`
  Expected: `4 passed`, then `ok`.
- [ ] **Step 10: Commit:** `kube-network-policies: v1.1.3 enforcer beside flannel, bottom only (netpol A3a)`.
- [ ] **Step 11: Gate G, then STOP — ask Chris.** It is a new privileged datapath DaemonSet in `kube-system`. Ask both questions in one AskUserQuestion:
  - (a) Approve the push.
  - (b) Is the §6.5 break-glass pre-approved for this rollout as a standing S2/S4 exception? Record (b) under Answers.
- [ ] **Step 12: Push.** Monitor until `applications` and `kube-network-policies` are both `Synced/Healthy` and `kubectl -n kube-system get ds kube-network-policies -o jsonpath='{.status.numberReady}/{.status.desiredNumberScheduled}'` prints `1/1` (timeout 10 min).
- [ ] **Step 13: Verify (§9.1).**
  - Run: `scripts/netpol-verify.sh idle`. Expected: `PASS bottom … packet_id=0 packets=0 restarts=0 flags=ok`, `idle: PASS (1 agents, 0 NetworkPolicies)`.
  - Run: `kubectl -n kube-system top pod -l app=kube-network-policies`. Expected: CPU < 50m, memory < 64Mi.
  - Run: `scripts/check-prometheus-rules.sh`. Expected: exit 0, and the `kube-network-policies` group is loaded.
  - Prometheus `up{namespace="kube-system",pod=~"kube-network-policies-.*"}` (exec+wget, as in `check-prometheus-rules.sh`). Expected: one series, value 1.
  - Gate G after. Expected: identical to before, with no new non-Running pods.
  - Rollback triggers (§7 A3a): crash-loop, CPU > 100m or memory > 200Mi sustained, any counter > 0, or any edge or Ceph change. Then roll back by rendering nothing (Global Constraints) and push.
- [ ] **Step 14: Ledger:** `A3a_start=<UTC>`. Task 4 starts no earlier than +24 h.

### Task 4: A3b — all three nodes

**Files:**
- Modify: `cluster/kube-network-policies/daemonset-patch.yaml` (drop the `nodeSelector` hostname line and its comment), `scripts/test_netpol_manifests.py` (expect `nodeSelector == {"kubernetes.io/os": "linux"}`)

**Interfaces:**
- Consumes: `idle` (Task 3).
- Produces: one agent per node. Every later task assumes this.

- [ ] **Step 1: 24 h gate.** Run `scripts/netpol-verify.sh idle` at ≥ A3a_start + 24 h. Expected: PASS, with restarts 0 and `packet_id=0`. That covers the whole window, because the kernel's queue id is cumulative since bind.
- [ ] **Step 2: Change the test expectation and run it.** Expected: `test_enforcer_daemonset` FAILS on nodeSelector.
- [ ] **Step 3: Drop the line, then re-run.** Expected: all pass.
- [ ] **Step 4: Commit:** `kube-network-policies: all nodes (netpol A3b)`.
- [ ] **Step 5: Gate G, then STOP — ask Chris.** Say that the `top` and `middle` pods are created together (`maxUnavailable` governs only replacements) and that `bottom`'s pod is replaced.
- [ ] **Step 6: Push.** Monitor until `numberReady/desiredNumberScheduled` prints `3/3` (timeout 15 min).
- [ ] **Step 7: Verify.** Run `scripts/netpol-verify.sh idle`. Expected: three `PASS` lines and `idle: PASS (3 agents, 0 NetworkPolicies)`. Then run Gate G after and the Task 3 Step 13 `top` and `up` checks; expect 3 series. Rollback triggers as in Task 3.
- [ ] **Step 8: Ledger:** `A3b_start=<UTC>`. Tasks 6 and 7 start no earlier than +24 h and run back to back, because the baseline window is short. Task 5 can run during this soak.

### Task 5: Canary probe logic (offline)

**Files:**
- Create: `cluster/netpol-canary/probe.sh` (bash, executable, with a shebang), `cluster/netpol-canary/matrix.txt`, `scripts/netpol-connect.js` (not executable)
- Modify: `scripts/netpol-verify.sh` (`selftest`, `deny_match`)

**Interfaces:**
- Produces:
  - **`matrix.txt`**: whitespace-separated columns `expect scope host port backend note…`, where:
    - `expect` ∈ `dns|open|dropped`;
    - `scope` ∈ `all|canary` (`canary` rows run only in the canary);
    - `backend` is `-`, or `ns/service`, whose EndpointSlice IPs are the post-DNAT destination in deny logs.
    - `#` starts a comment. `$HOST_IP` (the pod's node) and `$WAN_IP` (git.realliance.net's A record, resolved per run) are expanded.
  - **`probe.sh`** (sourceable; it runs `main` only when executed):
    - `probe_classify EXIT STDERR` → `open|timeout|refused|other`. Exit 0 is `open`; `TIMEOUT` is `timeout`; `REFUSED` is `refused`; anything else, including `DNS: …`, is `other`.
    - `probe_judge EXPECT GOT MODE` → return 0 or 1. `enforced`: open←open, dns←open, dropped←timeout. `baseline`: open←open, dns←open, dropped←open or refused. Everything else is 1.
    - `probe_rows MATRIX SCOPE` → expanded rows. `SCOPE=canary` gives all+canary rows; `SCOPE=runner` gives `all` rows only. Returns 2 if a selected row needs an unset variable.
    - `probe_check EXPECT HOST PORT` → got. For dns: `dig +short +time=3 +tries=1 HOST` non-empty → `open`, else `other`. For tcp: `/agnhost connect --timeout=3s HOST:PORT`, then classify.
    - `probe_once MODE` → one line per row, `PASS|FAIL <expect> <got> <host>:<port> <note>`, then `SUMMARY <pass>/<total> <mode> node=$NODE_NAME`. Returns 0 only if every row passes. Rows run **sequentially**: parallel agnhost processes would need about 20× the memory.
    - `probe_loop` → every `$PROBE_INTERVAL` s, start to start: resolve `WAN_IP`, run `probe_once "$PROBE_MODE"`, touch `/tmp/ready` on a pass and remove it on a fail. Print the table only when the pass/fail set changes, to keep logs quiet.
    - Usage: `probe.sh loop | probe.sh once enforced|baseline`. `MATRIX` defaults to `/etc/netpol-probe/matrix.txt`.
  - **`netpol-connect.js`** → `node netpol-connect.js tcp HOST PORT` or `node netpol-connect.js dns HOST`. It also works as `node -e "$(cat netpol-connect.js)" tcp HOST PORT`. It prints `open|refused|timeout|other:<code>` and exits 0. Timeout is 3000 ms. The argv split is `process.argv.slice(require.main === module ? 2 : 1)`.
  - **`netpol-verify.sh`**:
    - `deny_match LOGFILE SRC_POD DST` → 0 if some line has `"Packet verdict"`, `verdict="?deny"?`, `srcPod="?SRC_POD"?` and `dst="?DST`. `DST` is `IP:PORT`, or `IP:` to match any port.
    - `selftest` (offline).

- [ ] **Step 1: Write `selftest`** (it sources `cluster/netpol-canary/probe.sh`). Cases, each printing `ok`/`FAIL <case>`:
  - `probe_classify`:
    - `0 ""` → open;
    - `1 TIMEOUT` → timeout;
    - `1 REFUSED` → refused;
    - `1 "DNS: lookup x: no such host"` → other;
    - `1 "OTHER: connect: no route to host"` → other.
  - `probe_judge` returns 0 for:
    - `dropped timeout enforced`;
    - `dropped refused baseline`;
    - `dropped open baseline`;
    - `open open enforced`;
    - `dns open baseline`.
  - `probe_judge` returns 1 for:
    - `dropped refused enforced` (Review Focus 2);
    - `dropped open enforced`;
    - `dropped timeout baseline`;
    - `open timeout enforced`;
    - `dns other enforced`.
  - `probe_rows` on a fixture with rows `dns all a 53 - n`, `open canary b 9376 - n` and `dropped all $HOST_IP 50000 - n`:
    - scope `runner` gives 2 rows;
    - `HOST_IP` unset returns 2;
    - `HOST_IP=192.168.0.242` gives a row with `192.168.0.242 50000`.
  - The real `matrix.txt` parses:
    - every row has ≥ 5 columns, valid `expect`/`scope`, a numeric port and a `-` or `ns/name` backend;
    - it contains `dns kubernetes.default.svc.cluster.local`;
    - it contains `open`: `netpol-echo.netpol-canary.svc.cluster.local:9376`, `git.realliance.net:443`, `1.1.1.1:443`;
    - it contains `dropped`: `netpol-echo.netpol-canary.svc.cluster.local:9377`, `192.168.0.117:443`, `192.168.0.119:443`, `192.168.0.151:443`, `192.168.0.2:80`, `192.168.0.2:443`, `192.168.0.1:22`, `10.96.0.1:443`, `192.168.0.240:6443`, `forgejo-http.forgejo.svc.cluster.local:3000`, `192.168.0.7:443`, `192.168.0.8:1883`, `192.168.0.230:2049`, `192.168.0.231:5000`, `192.168.0.240:50000`, `$HOST_IP:50000` (Review Focus 1), `$WAN_IP:8585`.
  - `deny_match` on the fixture line `I1103 18:00:00.000000       1 verdictlog.go:172] "Packet verdict" id=7 verdict="deny" family="IPv4" protocol="TCP" src="10.244.0.9:40000" dst="192.168.0.117:443" srcPod="netpol-canary/netpol-probe-abcde" dstPod="external" direction="egress" evaluator="x"`:
    - it matches for (`netpol-canary/netpol-probe-abcde`, `192.168.0.117:443`) and for `192.168.0.117:`;
    - the same line with `verdict="accept"` does not match;
    - another srcPod does not match;
    - an unquoted variant matches.
  - `netpol-connect.js`:
    - a local `node` listener started by the test → `open`;
    - `tcp 127.0.0.1 1` → `refused`;
    - `dns localhost` → `open`;
    - `dns no-such-host.invalid` → `other:ENOTFOUND`.
- [ ] **Step 2: Run it.** Run: `nix develop --command scripts/netpol-verify.sh selftest`. Expected: exit 2, because `probe.sh` is missing.
- [ ] **Step 3: Implement** `probe.sh`, `matrix.txt` (the 21 rows above; backends: 9377 → `netpol-canary/netpol-echo`, `10.96.0.1` → `default/kubernetes`, forgejo → `forgejo/forgejo-http`, `.7` → `traefik/traefik`, `.8` → `home-assistant/mosquitto`, `.230` → `rook/rook-ceph-nfs-satellites-lb`, `.231` → `registry/registry`, every other row `-`) and `netpol-connect.js`.
- [ ] **Step 4: Run it again.** Run: `nix develop --command scripts/netpol-verify.sh selftest`. Expected: every case `ok`, then `selftest: PASS`, exit 0.
- [ ] **Step 5: Commit and push** (nothing renders yet, because `cluster/netpol-canary/` has no kustomization): `netpol-canary: probe logic, target matrix, Node connect probe; netpol-verify selftest`.

### Task 6: B1a — canary workloads, no policy, baseline

**Files:**
- Create: `cluster/netpol-canary/{kustomization.yaml,namespace.yaml,probe.yaml,echo.yaml}`, `cluster/applications/netpol-canary.yaml`
- Modify: `cluster/applications/kustomization.yaml`, `scripts/test_netpol_manifests.py`, `scripts/netpol-verify.sh` (`baseline`)

**Interfaces:**
- Consumes: Task 5's `probe.sh`, `matrix.txt` and `probe_once`.
- Produces:
  - Namespace `netpol-canary` (`pod-security.kubernetes.io/enforce: restricted`).
  - DaemonSet `netpol-probe`, labelled `app.kubernetes.io/name: netpol-probe`, with env `PROBE_MODE`, `PROBE_INTERVAL`, `HOST_IP` (`status.hostIP`), `NODE_NAME` (`spec.nodeName`) and `MATRIX`.
  - Deployment + Service `netpol-echo`, labelled `app.kubernetes.io/name: netpol-echo`, ports 9376 (`open`) and 9377 (`closed`).
  - ConfigMap generator `netpol-probe` (`probe.sh`, `matrix.txt`), mounted at `/etc/netpol-probe`.

- [ ] **Step 0: Precondition (§9.1).** `A3b_start` was ≥ 24 h ago, and `scripts/netpol-verify.sh idle` reports `idle: PASS (3 agents, 0 NetworkPolicies)`.
- [ ] **Step 1: Write the failing static test `test_canary_workloads`.**
  - Images are `registry.k8s.io/e2e-test-images/agnhost:<tag>@sha256:<64 hex>`. Pin the digest form, not the exact value, so Renovate's agnhost digest automerge does not break the hook.
  - Pod `runAsNonRoot`, `runAsUser: 1000`, seccomp `RuntimeDefault`. Containers: `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop == ["ALL"]`.
  - `automountServiceAccountToken: false`, `enableServiceLinks: false`, no `hostNetwork`.
  - Probe: `command == ["bash", "/etc/netpol-probe/probe.sh", "loop"]`, `PROBE_MODE == "baseline"`, an emptyDir at `/tmp`.
  - Echo: `args == ["porter"]`, env `SERVE_PORT_9376=open` and `SERVE_PORT_9377=closed`, readiness `tcpSocket` 9376.
  - The namespace label is as above. **No NetworkPolicy is rendered.**
  - Run it. Expected: FAIL.
- [ ] **Step 2: Write `baseline`.** For each `netpol-probe` pod (exit 2 unless there are exactly 3 Running pods on 3 distinct nodes), run `kubectl -n netpol-canary exec <pod> -- bash /etc/netpol-probe/probe.sh once baseline`, the three in parallel. Print each table. Final line: `baseline: PASS (3 nodes × <rows> rows answer)`. Run it. Expected: exit 2 (no probe pods).
- [ ] **Step 3: Write the manifests.**
  - Probe: resources requests `cpu: 10m, memory: 32Mi`, limit `memory: 128Mi`. Readiness `exec: [sh, -c, 'test -n "$(find /tmp/ready -mmin -3 2>/dev/null)"']`, `periodSeconds: 15`. No liveness probe: a failing canary is the signal, not something to restart.
  - Echo: one replica, requests `cpu: 5m, memory: 16Mi`, limit `memory: 64Mi`. The Service is ClusterIP 9376/9377.
  - Application: the shape of `forgejo-runner.yaml` (CreateNamespace, PruneLast).
  - `PROBE_INTERVAL` takes the value Chris picks in Step 6. Write `"60"` now and change it at Step 6 if needed.
- [ ] **Step 4: Run the static tests and `check-kustomizations.sh`.** Expected: pass.
- [ ] **Step 5: Commit:** `netpol-canary: probe DaemonSet and echo server, no policy yet (netpol B1, baseline)`.
- [ ] **Step 6: Gate G, then STOP — ask Chris.** B1 is additive, but it is cluster-affecting. In the same question, ask for the canary interval, because the design is silent on deny-log volume (Review Focus 5):
  - (1) **60 s** (the spec). Estimate: ~180k deny lines/day to Azure and router syslog. Readiness `-mmin -3`, alert `for: 10m`.
  - (2) **300 s**. About 36k/day. Readiness `-mmin -11`, alert `for: 15m`.
  - (3) **60 s plus a fluent-bit exclusion.** Add a grep filter with `Match kube.var.log.containers.kube-network-policies-*_kube-system_*` and `Exclude log srcPod="?netpol-canary/` to `cluster/applications/fluent-bit.yaml`, and two cases to `scripts/test-fluent-bit-exclusion.sh`: a canary verdict is dropped, a `forgejo-runner/` verdict is shipped. That test needs docker, which needs Chris's OK.
  Record the answer and apply it in this commit (amend before pushing) or, for (3), in Task 7.
  Mention that the baseline window dials the BMCs, the KVM and router SSH every interval, so Task 7 follows within the same session (≤ 1 h).
- [ ] **Step 7: Push.** Monitor until the `netpol-canary` app is `Synced/Healthy`, the DaemonSet is `3/3` and `netpol-echo` is `1/1` (timeout 10 min).
- [ ] **Step 8: Verify the baseline (§6.4).** Run: `scripts/netpol-verify.sh baseline`. Expected: `SUMMARY 21/21 baseline` on each node, then `baseline: PASS`.
  A dropped row that neither connects nor is refused would give a false pass later. Ledger it and raise it at Task 7's STOP: remove or replace that row. Do not leave it in.
- [ ] **Step 9: Ledger:** the baseline tables, Chris's interval answer, `B1a_at=<UTC>`.

### Task 7: B1b — components and canary policies; 24 h green

**Files:**
- Create: `cluster/components/netpol/default-deny/`, `allow-dns/`, `egress-internet-only/`, each holding `kustomization.yaml` (`apiVersion: kustomize.config.k8s.io/v1alpha1`, `kind: Component`, `resources: [networkpolicy.yaml]`) and `networkpolicy.yaml`
- Create: `cluster/netpol-canary/networkpolicy.yaml`, `cluster/netpol-canary/prometheusrule.yaml`
- Modify: `cluster/netpol-canary/kustomization.yaml` (`components: [../components/netpol/default-deny, ../components/netpol/allow-dns, ../components/netpol/egress-internet-only]`, plus the two new resources), `probe.yaml` (`PROBE_MODE: enforced`), `scripts/test_netpol_manifests.py`, `scripts/netpol-verify.sh` (`canary`, `stray`)
- Modify (only if Chris picked option 3): `cluster/applications/fluent-bit.yaml`, `scripts/test-fluent-bit-exclusion.sh`

**Interfaces:**
- Consumes: `probe_once`, `deny_match`, the matrix `backend` column.
- Produces:
  - Components named `default-deny`, `allow-dns` and `egress-internet-only`: NetworkPolicy names equal the directory names, `podSelector: {}`, and no namespace (the consumer sets it).
    - `default-deny` is `policyTypes: [Ingress, Egress]` with no rules.
    - `allow-dns` is §6.3's DNS rule.
    - `egress-internet-only` holds §6.3's other two rules (WAN /25 TCP 443, and `0.0.0.0/0` with the ten excepts), as one policy with `policyTypes: [Egress]`.
  - `netpol-verify.sh canary [--node NAME]` and `netpol-verify.sh stray [--since DUR]`.

- [ ] **Step 1: Write the failing static tests.**
  - `test_components_match_spec`: the three components render as described. `SPEC_6_3_RULES`, a literal copy of §6.3's three egress rules, equals the union of their egress rules.
  - `test_canary_policies`: the rendered `netpol-canary` NetworkPolicies are exactly `{default-deny, allow-dns, egress-internet-only, probe-egress-to-echo, echo-ingress-from-probe}`, all in namespace `netpol-canary`.
    - `probe-egress-to-echo` selects the probe, `policyTypes: [Egress]`, egress to podSelector echo on TCP 9376 and 9377.
    - `echo-ingress-from-probe` selects the echo, `policyTypes: [Ingress]`, ingress from podSelector probe on TCP 9376 only. With the egress rule allowing both ports, a drop on 9377 can only be ingress default-deny (§9.2).
  - `policy_violations(docs: list[dict]) -> list[str]`. It returns one message per NetworkPolicy whose namespace is empty or in `FORBIDDEN = {"kube-system","rook","metallb-system","prometheus","argo-cd","telephony","home-assistant","dns","rabbitmq-system"}`. Add the comment "dns and rabbitmq-system leave this set only through plan Task 14".
    - `test_forbidden_guard_detects`: a synthetic NetworkPolicy in `rook` plus one with no namespace gives 2 violations.
    - `test_no_policy_in_forbidden_namespaces`: render every kustomization directory under `cluster/`, except those under `cluster/components/`, that has a non-secret YAML mentioning `kind: NetworkPolicy` or `components/netpol`. The result must be `[]`.
  - Update `test_canary_workloads` (Task 6): expect `PROBE_MODE == "enforced"` and drop its "no NetworkPolicy" assertion.
  - Run. Expected: 5 failed. `policy_violations` is undefined, and the components and canary policies are missing.
- [ ] **Step 2: Write `stray` and `canary`.**
  - `stray`: read all agents' logs for `--since` (default 15m). The policed namespaces are those with ≥ 1 NetworkPolicy. Every `verdict="deny"` line must have a `srcPod` or `dstPod` in a policed namespace. Print up to 10 `STRAY <line>`, then `stray: PASS (<n> deny lines, policed: <ns,…>)`. Exit 1 on any stray line.
  - `canary`: exit 2 unless there are 3 Running probe pods on distinct nodes (or the one on `--node`).
    - Run `probe.sh once enforced` in each, in parallel.
    - Read all agents' logs `--since=10m`.
    - For every pod × `dropped` row, `deny_match` with `SRC_POD=netpol-canary/<pod>`. `DST` is the literal `host:port` when the backend is `-`. Otherwise it is any IP from `kubectl -n <ns> get endpointslices -l kubernetes.io/service-name=<svc> -o jsonpath='{range .items[*].endpoints[*]}{.addresses[0]}{"\n"}{end}'`, matched as `IP:`.
    - Then run `stray`.
    - Print `<node> rows <p>/21 deny-logged <d>/17`, then `canary: PASS (<n> nodes)`.
- [ ] **Step 3: Run `canary` against the live, unpoliced canary.** Run: `scripts/netpol-verify.sh canary`. Expected: exit 1, with every `dropped` row `FAIL … open` or `refused`.
- [ ] **Step 4: Implement the components, the two canary policies, `PROBE_MODE: enforced` and `prometheusrule.yaml`.** The rule is `prometheus`-namespaced and labelled `release: prometheus`. Alert `NetpolCanaryFailing`, `severity: warning`, `for: 10m` (or 15m per Chris), expr `kube_daemonset_status_number_ready{namespace="netpol-canary",daemonset="netpol-probe"} < kube_daemonset_status_desired_number_scheduled{namespace="netpol-canary",daemonset="netpol-probe"} or absent(kube_daemonset_status_desired_number_scheduled{namespace="netpol-canary",daemonset="netpol-probe"})`. For option 3, also add the fluent-bit filter and test cases, and ask before running that test locally.
- [ ] **Step 5: Run the static tests and `check-kustomizations.sh`.** Expected: all pass.
- [ ] **Step 6: Commit:** `netpol: policy components; canary default-deny + DNS + internet-only egress (netpol B1)`.
- [ ] **Step 7: Gate G, then STOP — ask Chris.** Include the baseline result, and any row to remove from Task 6 Step 8.
- [ ] **Step 8: Push.** Monitor until the app is `Synced/Healthy` and the probe is `3/3` Ready in enforced mode (timeout 15 min). The first run after a new pod's IP is enrolled may fail once.
- [ ] **Step 9: Verify (§9.2).**
  - Run: `scripts/netpol-verify.sh canary`. Expected: each node `rows 21/21 deny-logged 17/17`, `stray: PASS (…, policed: netpol-canary)`, `canary: PASS (3 nodes)`.
  - Run: `kubectl -n netpol-canary get deploy netpol-echo -o jsonpath='{.status.readyReplicas}'` → Expected: `1`. Restarts must be 0: kubelet's root probe passes under default-deny (§3.4 item 3).
  - Run: `scripts/check-prometheus-rules.sh` → Expected: exit 0 with the `netpol-canary` group loaded.
  - The first real deny line must match `deny_match`. If the live shape differs, fix the matcher and the selftest fixture in one commit.
- [ ] **Step 10: Measure log volume (Review Focus 5).**
  Run: `for p in $(kubectl -n kube-system get pod -l app=kube-network-policies -o name); do kubectl -n kube-system logs $p --since=10m | grep '"Packet verdict"'; done | tee $SCRATCH/verdicts.txt | wc -l; wc -c < $SCRATCH/verdicts.txt`
  Ledger lines/day = lines × 144 and bytes/day = bytes × 144, against the budget implied by Chris's choice. Expected: ≈ 1250 lines per 10 min at 60 s, or ≈ 250 at 300 s. If it is more than 1.5× that, STOP — ask Chris.
- [ ] **Step 11: 24 h gate.**
  - At ≥ +24 h, run `scripts/netpol-verify.sh canary`. Expected: PASS.
  - Prometheus query `max_over_time(ALERTS{alertname="NetpolCanaryFailing"}[24h])`. Expected: an empty result.
  - Gate G after. Expected: matches baseline.
  - Rollback trigger (§7 B1): any mismatch. Investigate before going on. Roll back by rendering nothing.
  - Ledger `B1_green_at=<UTC>`.

### Task 8: B2 — forgejo-runner policies and the `netpol-gate`

**Files:**
- Modify: `cluster/forgejo-runner/kustomization.yaml` (add `components:`, the same three paths as the canary, and a comment pointing at §6.3), `cluster/forgejo-runner/deployment.yaml` (add `initContainers: [netpol-gate]` before `containers:`)
- Modify: `scripts/test_netpol_manifests.py`, `scripts/netpol-verify.sh` (`runner`)
- Modify (cross-repo): `~/Repos/realliance-sso/tests/forge/test_forge_ci.py`. Add `test_runner_job_egress_confined`.

**Interfaces:**
- Consumes:
  - the components (Task 7);
  - `scripts/netpol-connect.js` and `matrix.txt` rows with scope `all`;
  - `deny_match` and `stray`;
  - `push_probe_workflow`, `runs_for`, `wait_for`, `delete_branch`, `LUMA`, `FORGE_BOT_REPO` and `CYCLE_WAIT` from realliance-sso `tests/forge_helpers.py`.
- Produces: `netpol-verify.sh runner`. The gate container is named `netpol-gate`.

- [ ] **Step 1: Coordinate and check preconditions.**
  - Run `git log --oneline -- cluster/forgejo-runner` and re-read the non-secret files there.
  - Check `B1_green_at` ≥ 24 h ago and `scripts/netpol-verify.sh canary` PASS now.
  - Run the runner-idle command. Expected: three `idle` lines.
- [ ] **Step 2: Write the failing static tests.**
  - `test_runner_policies_equal_spec_6_3`: the rendered NetworkPolicies in `forgejo-runner` are exactly `{default-deny, allow-dns, egress-internet-only}`. The union of their egress rules equals `SPEC_6_3_RULES`. No rule allows ingress.
  - `test_runner_gate`: `initContainers` names == `["netpol-gate"]`.
    - Its image equals the `runner` container's image, and its `securityContext` equals the runner container's.
    - `command[:2] == ["sh","-c"]`, and `command[2]` contains `host:"10.96.0.1",port:443,timeout:3000`, `s.on("timeout",()=>process.exit(0))`, `s.on("connect",()=>process.exit(1))`, `s.on("error",()=>process.exit(1))` and `sleep 3` (the §6.3 body).
    - Requests are `cpu: 10m, memory: 32Mi`, the limit `memory: 128Mi`.
  - Run. Expected: 2 failed.
- [ ] **Step 3: Write `runner`.**
  - Exit 2 unless the `forgejo-runner` pod is Running and Ready. `HOST_IP` is the pod's `status.hostIP`; `WAN_IP` is `dig +short git.realliance.net | grep -E '^[0-9.]+$' | tail -1`.
  - For each `probe_rows matrix.txt runner` row, run `kubectl -n forgejo-runner exec <pod> -c runner -- node -e "$(cat scripts/netpol-connect.js)" <tcp HOST PORT | dns HOST>`, then `probe_judge … enforced`.
  - Then run `deny_match` (`SRC_POD=forgejo-runner/<pod>`) for each dropped row, then `stray`.
  - Final line: `runner: PASS (19 rows, 16/16 deny-logged)`.
- [ ] **Step 4: Run the live "before" checks.**
  - Run: `scripts/netpol-verify.sh runner`. Expected: exit 1, with the dropped rows `open` or `refused`.
  - Run the §6.3 gate one-liner in the runner pod: `kubectl -n forgejo-runner exec deploy/forgejo-runner -c runner -- node -e '<the §6.3 node expression>'; echo rc=$?`. Expected: `rc=1` (connected, so not enforced: the gate would wait).
- [ ] **Step 5: Write `test_runner_job_egress_confined`** in realliance-sso, on a new branch `netpol-runner-egress`.
  - It reads `LUMA/"scripts/netpol-connect.js"` and the `all` rows of `LUMA/"cluster/netpol-canary/matrix.txt"`, skipping `$HOST_IP` rows.
  - It builds a job script with `set -eu`:
    - write the JS to `"$TMPDIR/netpol-connect.js"` (the root filesystem is read-only);
    - `WAN_IP=$(node -e 'require("dns").lookup("git.realliance.net",{family:4},(e,a)=>{if(e)process.exit(1);console.log(a)})')`;
    - one check per row (dropped→`timeout`, open/dns→`open`), counting failures and printing `FAIL <row> got <x>`;
    - `git ls-remote https://git.realliance.net/realliance-ops/forge-bot HEAD`, `git ls-remote https://github.com/actions/checkout HEAD` and `go mod download golang.org/x/text@v0.20.0`;
    - `exit $fail`.
  - It pushes the script with `push_probe_workflow(FORGE_BOT_REPO, "galaxy", script)`, then asserts `_settled(...) == ["success"]` within `CYCLE_WAIT`, and deletes the branch in `finally`.
  - Run: `cd ~/Repos/realliance-sso && nix develop --command pytest tests/forge/test_forge_ci.py::test_runner_job_egress_confined -v`. Expected: FAIL (the probe run is `failure`).
- [ ] **Step 6: Implement** the kustomization `components:` and the `netpol-gate` init container. Use the §6.3 body verbatim. Add two comments:
  - "never allow the API server in this namespace's egress, or this gate waits forever";
  - "keep this image equal to the runner's; `netpol-manifests` (pre-commit) fails otherwise, and Renovate bumps both lines together".
- [ ] **Step 7: Run the static tests.** Run: `nix develop --command python -m pytest -q -p no:cacheprovider scripts/test_netpol_manifests.py && nix develop --command scripts/check-kustomizations.sh`, then `cd ~/Repos/realliance-sso && nix develop --command pytest tests/forge/test_forge_ci.py::test_runner_manifests_unprivileged -v`. Expected: all pass.
- [ ] **Step 8: Commit:** `forgejo-runner: default-deny + DNS + forge/internet egress, netpol-gate init (netpol B2)`.
- [ ] **Step 9: Gate G, then STOP — ask Chris.** It is a live service in another plan's deployment. Show the idle output and the B1 evidence, and ask him to hold forge-bot CI pushes. Rollback triggers (§7 B2): a legitimate dependency is blocked (add an allow rule, or revert if urgent), or the runner cannot fetch tasks (revert).
- [ ] **Step 10: Push.** Monitor until the `forgejo-runner` app is `Synced/Healthy` and the new pod is `1/1` Running (timeout 15 min). The strategy is Recreate.
  - Run: `kubectl -n forgejo-runner logs deploy/forgejo-runner -c netpol-gate`. Expected: zero or more `egress not enforced yet; waiting` lines and no error. Ledger the count: it is the no-NRI enrolment delay.
  - Run: `kubectl -n forgejo-runner logs deploy/forgejo-runner -c runner | grep -E 'declared successfully|\[poller\] launched'`. Expected: both lines.
- [ ] **Step 11: Verify (§9.3).**
  - Run: `scripts/netpol-verify.sh runner`. Expected: `runner: PASS (19 rows, 16/16 deny-logged)` and `stray: PASS (…, policed: forgejo-runner,netpol-canary)`.
  - Re-run Step 4's gate one-liner. Expected: `rc=0` (timeout, so enforced).
  - Run: `cd ~/Repos/realliance-sso && nix develop --command pytest tests/forge/test_forge_ci.py -k "runner_job_egress_confined or probe_job_hardened_and_workspace_wiped" -v`. Expected: 2 passed.
  - **Pipeline criterion:** if `realliance-ops/forge-bot` has `.forgejo/workflows/` on `main`, re-run its latest run. Expected: `success`. If it does not (true on 2026-11-03), ledger that the full checkout/`go mod download`/test/ko-push pipeline is proven at forge-bot plan Task 16 Step 1.
  - Run `scripts/netpol-verify.sh canary`. Expected: PASS. Then Gate G after.
- [ ] **Step 12: Land the realliance-sso test.** Run `DISPLAY=:10.0 XAUTHORITY=$HOME/.Xauthority git commit -S -m "tests: forge CI runner job egress is confined (galaxy netpol B2)"` on the branch, then `gh pr create --fill`. **Chris:** merges, and only when he says so.
- [ ] **Step 13: Ledger:** `B2_done=<UTC>`. Task 12 (B4) waits until ≥ +7 days.

### Task 9: Restart behaviour (§9.4)

**Files:** none. Results go to the ledger.

**Interfaces:**
- Consumes: `canary --node`, `runner`.

- [ ] **Step 1: Preconditions.** Runner idle (three `idle` lines), CI hold agreed, Gate G. Then identify the runner node and its agent pod:
  `N=$(kubectl -n forgejo-runner get pod -l app.kubernetes.io/name=forgejo-runner -o jsonpath='{.items[0].spec.nodeName}'); kubectl -n kube-system get pod -l app=kube-network-policies --field-selector spec.nodeName=$N -o name`
- [ ] **Step 2: STOP — ask Chris.** This deletes a `kube-system` pod. Say that the runner is unconfined during the gap, which is why it must be idle.
- [ ] **Step 3: Delete it.** Run `date -u +%T; kubectl -n kube-system delete <that pod>` and ledger `t0`.
- [ ] **Step 4: Watch.**
  - Monitor until the replacement pod on `$N` is Ready, and ledger `t_ready`. Expected: within 60 s.
  - Then Monitor `scripts/netpol-verify.sh canary --node $N && scripts/netpol-verify.sh runner` until it passes, and ledger `t_enforced`. Expected: within 60 s of `t_ready`.
  - Rollback trigger: no replacement within 5 min. Traffic fails open, so the runner is unconfined. STOP — ask Chris whether to scale `forgejo-runner` to 0 (the S2 incident exception; revert through the repo afterwards).
- [ ] **Step 5: Ledger the observed gap** (`t_enforced − t0`), then commit and push the ledger.

### Task 10: Docs, once B2 has landed (§10)

**Files:**
- Create: `docs/networkpolicy.md`, the runbook. Sections:
  - what enforces and where;
  - `scripts/netpol-verify.sh` subcommands and when to run each;
  - break-glass, with the §6.5 commands verbatim and the pre-approval status;
  - adding a policy: components, forbidden namespaces, allow by selector (post-DNAT), and a comment on every policy;
  - sharp edges from §3.4 items 1, 3, 4, 6 and 7, plus the `input` hook for pod→own node;
  - rollback by rendering nothing;
  - bumps: the Renovate rule, then re-run `canary`;
  - the discovery procedure (§7.1).
- Modify: `CLAUDE.md`:
  - a topology row "NetworkPolicy": kube-network-policies v1.1.3, the Argo app `kube-network-policies`, enforced only where a policy selects a pod, the canary `netpol-canary`, the runbook link;
  - a sharp edge: rules are judged post-DNAT and pre-SNAT; root on the node is exempt but the API server (uid 65534) and kernel Ceph clients are not; host sources are node IP or flannel.1/cni0.
- Modify: `docs/gramps.md:254`, `docs/telephony.md:84` and the comment at `cluster/dex/grpc-tls.yaml:11`. Rewrite each "flannel does not enforce NetworkPolicy" to "no NetworkPolicy selects this namespace yet; enforcement exists (docs/networkpolicy.md)".
- Modify: `docs/superpowers/specs/2026-11-02-forge-bot-and-ci-design.md` (lines 74 and 289): append "superseded 2026-11-03: enforced since <B2_done>; see the NetworkPolicy spec".
- Modify: `docs/luma.md`. In the phase 4 row, mark the enforcer part as done.
- Modify: `.claude/skills/talos-upgrade/SKILL.md` (§8 gate):
  - per-node check 7: `kubectl -n kube-system get pod -l app=kube-network-policies --field-selector spec.nodeName=<name>` Ready, and `scripts/netpol-verify.sh canary --node <name>` PASS;
  - after `upgrade-k8s`, item 6: `scripts/netpol-verify.sh canary` PASS on all three;
  - a hazard line: never set `kubeNetworkPoliciesEnabled`.
- Not touched: `cluster/dex/config.secret.yaml` carries the same comment, but it is a secret file. Ledger that it was left alone. `docs/forgejo.md` has no such line today; edit it only if one has appeared since.

- [ ] **Step 1: The check.** Run: `grep -rn --exclude='*.secret.*' -i 'does not enforce NetworkPolicy\|enforces no NetworkPolicy' CLAUDE.md docs cluster .claude | grep -v 'specs/2026-11-03-networkpolicy'`. Expected now: hits in gramps.md, telephony.md, grpc-tls.yaml, the forge-bot spec and the forge-bot plan.
- [ ] **Step 2: Make the edits above.**
- [ ] **Step 3: Re-run the check.** Expected: only the forge-bot spec lines that now carry "superseded", and the in-flight forge-bot plan, which is left alone. Its executor learns the change at Task 11.
- [ ] **Step 4: Commit and push.** This is docs only; the dex comment does not change the rendered object. Message: `docs: NetworkPolicy runbook; enforcement is live (CLAUDE.md, talos-upgrade gate, stale "flannel does not enforce" lines)`.

### Task 11: B3 — forge-bot's policy is real (runs with forge-bot plan Task 16)

**Files:** none in this plan. forge-bot's `networkpolicy.yaml` belongs to the forge-bot plan.

**Interfaces:**
- Consumes: `stray`. forge-bot's policy as that plan specifies it: DNS, Forgejo pods TCP 3000, `23.163.0.48/32` TCP 443.

- [ ] **Step 1: Precondition, before forge-bot's first deploy.**
  Run: `dig +short sso.realliance.net; kubectl -n forgejo get pods -l app.kubernetes.io/name=forgejo,app.kubernetes.io/instance=forgejo --no-headers | wc -l`
  Expected for the policy to work: an answer containing `23.163.0.48`, then `≥1`.
  On 2026-11-03 the answer was `104.21.57.107` and `172.67.190.20` (Cloudflare proxy), from both the LAN resolver and 1.1.1.1. If that still holds, **STOP — ask Chris** before forge-bot deploys. Its Keycloak calls would be dropped unless forge-bot dials the origin directly or the policy changes. Also tell the forge-bot executor (through Chris) that its Task 16 comment "flannel does not enforce this" is now false.
- [ ] **Step 2: After forge-bot is deployed** (in dry-run), verify over two cycles (≥ 11 min).
  - Run: `scripts/netpol-verify.sh stray --since 15m`. Expected: PASS, with `forge-bot` among the policed namespaces.
  - Run: `for p in $(kubectl -n kube-system get pod -l app=kube-network-policies -o name); do kubectl -n kube-system logs $p --since=15m; done | grep -c 'srcPod="\?forge-bot/'`. Expected: `0`.
  - forge-bot's `forge_bot_cycle_last_success_timestamp` must be < 6 min old, with no read errors in its logs.
  - Rollback trigger (§7 B3): read errors. The fix goes into forge-bot's policy, through the forge-bot plan.
- [ ] **Step 3: Ledger the result.**

### Task 12: B4 — Argo's chart policies back on (≥ 7 days after B2)

**Files:**
- Modify: `cluster/argo-cd/values.yaml`. Set `networkPolicy.create: true`, with the comment `# the chart's 5 ingress policies (netpol spec §3.2); back on at B4 <date>`.

**Interfaces:**
- Consumes: `stray`, `canary`, and Task 2's render check.

- [ ] **Step 1: Preconditions.** `B2_done` ≥ 7 days ago. `scripts/netpol-verify.sh canary && scripts/netpol-verify.sh runner` PASS. `max_over_time(ALERTS{alertname=~"NetpolCanaryFailing|KubeNetworkPolicies.*"}[7d])` is empty.
- [ ] **Step 2: Render check, before.** Re-run the Task 2 Step 1 command. Expected: `0`.
- [ ] **Step 3: Set the value and re-run.** Expected: `5`.
- [ ] **Step 4: Commit:** `argo-cd: chart NetworkPolicies back on (netpol B4)`.
- [ ] **Step 5: Gate G.** Save the current `reconciledAt` values: `kubectl -n argo-cd get applications.argoproj.io -o json | jq -r '.items[] | "\(.metadata.name) \(.status.reconciledAt)"' > $SCRATCH/reconciled-before.txt`.
- [ ] **Step 6: STOP — ask Chris** (`argo-cd`). State the break-glass path: §6.5 step 1, under the pre-approval answer.
- [ ] **Step 7: Push.** Monitor until `argo-cd` is `Synced/Healthy` at the new sha (timeout 10 min).
- [ ] **Step 8: Verify.**
  - Run: `kubectl -n argo-cd get netpol --no-headers | wc -l` → Expected: `5`.
  - After 5 min, every app's `reconciledAt` must be newer than the saved value: diff against `reconciled-before.txt`, so that no line is unchanged.
  - Prometheus `up{namespace="argo-cd"}`. Expected: every series 1 (repo-server and redis metrics are still scraped).
  - Run: `scripts/netpol-verify.sh stray --since 60m` one hour after the push. Expected: PASS, with `argo-cd` policed and no deny line naming `argo-cd/`.
  - Gate G after. Expected: the `ci.chrismiller.xyz` edge row is unchanged.
  - Rollback trigger: any sync failure. Revert; if Argo cannot apply the revert, use §6.5 step 1.
- [ ] **Step 9: Ledger, then commit and push it.**

### Task 13: C — adopt one namespace (repeat per namespace; §7.1 order)

**Files:**
- Create at first use: `cluster/components/netpol/allow-from-traefik/` (ingress from Traefik's namespace and pod selector, all ports), `allow-from-prometheus/` (ingress from Prometheus's namespace and pod selector, all ports), `allow-same-namespace/` (ingress from `podSelector: {}`) and `egress-open/` (`policyTypes: [Egress]`, `egress: [{}]`). Each is a Component with `podSelector: {}`.
- Modify: `cluster/<ns-app>/kustomization.yaml` (`components:`), `scripts/test_netpol_manifests.py` (an `ADOPTED = {"<ns>": {<expected policy names>}}` entry checked by `test_adopted_namespaces`)

**Interfaces:**
- Consumes: `default-deny` and `allow-dns` (Task 7), `stray`, Gate G.

- [ ] **Step 1: Pick the next namespace** in §7.1 order: leaf web apps, then apps with their own Postgres, then `oauth2-proxy`/`dex`/`mcp-gateway` (read Luma §13.1 first). A namespace with the same shape as one already adopted waits until that one has had a clean week.
- [ ] **Step 2: Inventory its inbound flows (S1)** and ledger them:
  - its Ingress hosts (Traefik);
  - ServiceMonitors, PodMonitors and `prometheus.io/scrape` annotations (Prometheus);
  - same-namespace clients (its Postgres);
  - cross-namespace clients (for example forge-bot → `forgejo` :3000, and LB port 22 from anywhere with `etp: Cluster` per §7.1.2);
  - any webhook. If it has one, stop: Task 14 first.
  The set is `default-deny` + `allow-dns` + `egress-open` + `allow-from-traefik` + (`allow-from-prometheus` if scraped) + `allow-same-namespace` + any explicit cross-namespace rule (a commented NetworkPolicy in that app).
- [ ] **Step 3: Write `test_adopted_namespaces`.** Add the expected entry and run it. Expected: FAIL.
- [ ] **Step 4: Add the components.** Run the tests, which also run the forbidden-namespace guard. Expected: PASS. Then commit: `<ns>: NetworkPolicy, ingress-first (netpol C)`.
- [ ] **Step 5: Gate G, then STOP — ask Chris** (§7 C, and "yes" for anything with a webhook).
- [ ] **Step 6: Push.** Monitor until the app is Synced/Healthy and every pod in the namespace is Ready.
- [ ] **Step 7: Verify.**
  - Gate G after. Expected: an empty edge diff for its hosts.
  - Prometheus `up{namespace="<ns>"}`: every series 1.
  - Run: `scripts/netpol-verify.sh stray`. Expected: PASS.
  - Run: `… logs … | grep -c '"\?<ns>/'` over 1 h. Expected: `0` deny lines for legitimate traffic.
  - Rollback trigger: deny lines for legitimate traffic. Add a rule, or revert.
  - Ledger `C_<ns>_at`.
- [ ] **Optional: discovery (§7.1)** when the flows are unclear. Apply a per-namespace allow-all, then set `--log-verdicts=all` in `daemonset-patch.yaml` for a bounded window. That change is its own STOP, because it rolls the agents on every node. Do it only while this namespace, the runner and the canary are the only policed ones, and only with the volume choice from Task 6. Read the accept lines, then revert the flag in the next commit.
- [ ] **Never in this plan:** egress restriction for an adopted namespace (§7.1.5) without a ledgered dependency list, and the namespaces in `FORBIDDEN`.

### Task 14: Prove `allow-from-nodes` (only before the first webhook namespace: `dns` or `rabbitmq-system`)

**Files:**
- Create (temporary): `cluster/netpol-hostprobe/` and `cluster/applications/netpol-hostprobe.yaml`:
  - namespace `netpol-hostprobe` with `pod-security.kubernetes.io/enforce: privileged`;
  - a hostNetwork DaemonSet on the agnhost pin, with `dnsPolicy: ClusterFirstWithHostNet` and `runAsUser: 1000`, so the root exemption does not apply;
  - command `bash -c 'while :; do for h in netpol-echo-headless netpol-echo; do /agnhost connect --timeout=3s $h.netpol-canary.svc.cluster.local:9378; done; sleep 60; done'`.
- Create: `cluster/components/netpol/allow-from-nodes/` (ingress from the nine `/32`s: `192.168.0.240`, `.241`, `.242`, `10.244.3.0`, `10.244.4.0`, `10.244.0.0`, `10.244.3.1`, `10.244.4.1`, `10.244.0.1`)
- Modify:
  - `cluster/netpol-canary/echo.yaml`: `SERVE_PORT_9378`, Service port 9378, and a headless Service `netpol-echo-headless`, whose DNS answer is the pod IP for the direct path;
  - `cluster/netpol-canary/networkpolicy.yaml`;
  - `scripts/test_netpol_manifests.py`: `FORBIDDEN` loses `dns` and `rabbitmq-system` at the end.

**Interfaces:**
- Consumes: the canary echo, `deny_match`.
- Produces: the `allow-from-nodes` component, proven.

- [ ] **Step 1: STOP — ask Chris** before pushing the hostprobe. It is a privileged-PSS namespace with hostNetwork pods. On each node, every interval, the hostprobe dials the echo pod IP on 9378 directly and through the `netpol-echo` ClusterIP. 9378 has no ingress allow yet.
- [ ] **Step 2: Collect the denied sources.** Run `… logs … | grep 'dst="\?<echo pod IP>:9378'` and read the `src=` addresses. Expected: they are a subset of the nine above, covering same-node (node IP), cross-node (flannel.1) and direct (cni0). Ledger the observed set. Any address outside the nine is a design error: STOP.
- [ ] **Step 3: Add `allow-from-nodes` and a policy on echo :9378.** STOP, then push. Expected: every hostprobe dial connects, and no deny lines for 9378 for 1 h.
- [ ] **Step 4: Remove the hostprobe app** by rendering nothing, and remove 9378. STOP, then push. Then edit `FORBIDDEN` and commit: `netpol: allow-from-nodes proven against deny logs (netpol Task 14)`. The webhook namespace itself is adopted through Task 13, with `allow-from-nodes` on its webhook port.
