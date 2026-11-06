# Runner classes phases 0 and 1: ledger

Plan: `2026-11-04-runner-classes-phase-0-1.md`. This file holds no secret, no Azure id and no hidden org, team or group name.

Tasks 0 to 17 kept their answers, rulings, proof results and Gate G summaries in the controller's SDD notes and task reports under `.superpowers/sdd/2026-11-04-runner-classes-phase-0-1/` (`progress.md`, `task-*-report.md`, git-ignored). This file starts at Task 18.

## Task 18: deploy in dry-run

### Preconditions (2026-11-06T00:48Z)

- N1, as Chris relaxed it on 2026-11-05: NetworkPolicy Task 7's components are on `main` (dc2aa9a8). `B2_done` is not required for dry-run; Task 19 keeps the full gate.
- N3 carve-out: NetworkPolicy Task 7's 24 h soak may stay open, because its Gate G excludes `ci-dispatch` and `ci-tiny`.
- Task 15's `ci.yaml` is on `realliance-ops/forge` `main` (820efdb, signed).
- The usage token is in sso-secrets `main` (11e2fab, pushed).
- No `ci-dispatch` PVC or PV exists, nor either namespace or the Application. That rules out a ledger from before `launches_key`, which `migrate()` would keep as is, since it returns early at schema version 1.
- Image: v0.1.0, `git.realliance.net/realliance-ops/ci-dispatch:sha-9055e60@sha256:39ab99aa87f2e76dae145c67fb7e75fc5e2490d7de76cea95f1a4582ad7dc3ef` (Chris approved it at Task 14 Step 4).
- Both token files are one printable line with no whitespace, checked by count only: usage 64 bytes, Forgejo 40 bytes.

### Gate G before (2026-11-06T00:49:53Z to 00:51Z)

The cluster already ran `main` bb4ae992: Argo synced netpol B1 review M5 to `netpol-canary` at 00:36:09Z.

- G1: 3 nodes `Ready`, none `SchedulingDisabled`. 145/145 PGs `active+clean` (one deep-scrubbing). No hop, drain or recovery.
- G2: Ceph `HEALTH_WARN`, `MDS_CLIENT_RECALL` (the `rook-satellites` CephNFS client) only, besides the three muted `AUTH_INSECURE_*`. 9/9 OSDs up and in. Argo: 62 apps, all `Synced`; only `rook` and `rook-cluster` not Healthy (Degraded), as in the baseline.
- G3: 51 edge rows (16×200, 14×302, 3×401, 1×303, 1×404, the rest bare paths). The normalised diff against NetworkPolicy Task 7's after-snapshot (21:56Z) is empty.
- G4: no pod outside `Running`/`Succeeded`. 5 NetworkPolicies, all in `netpol-canary`. Memory requests: top 86 %, middle 85 %, bottom 88 % (baseline 85/85/87). No pod over 100 restarts. One Warning event in 10 min, `ResourceLimits` on `acid-chrismillerxyz`, which has nothing to do with this change.
- `scripts/netpol-verify.sh stray --since 15m` (from `main`, 3ef3cf6a) at 00:50:34Z: `stray: PASS (567 deny lines, policed: netpol-canary)`; every agent's log covers the window.
- Deny lines naming `ci-dispatch/` or `ci-tiny/`, 15 min, all three agents: 0.
- `scripts/check-prometheus-rules.sh`: exit 0, `selected=41 groups=44 alerting=179 recording=82`; the only WARN is the existing `unselected music-streaming/gonic-alerts`.

### Review (2026-11-06): approved to push, 5 Minors

- M1, M2, M3 and M5 are fixed: alert wording, the startup-probe comment, the undone threshold 7 with `for: 1h` load-bearing, and `backend="galaxy"` in the test data.
- M4, Chris's ruling: linear history. The branch is rebased onto `main` with `--gpg-sign`, and the push is a fast-forward of it.
- **Carry → Task 20 (`docs/ci.md`):** `CIDispatchStoredConfigRejected` fires at start when the ledger's copy of `ci.yaml` fails its proof. The usual cause is that its signer is no longer in the keyring: a rotated or expired key.
  - **Rotate an Owner key in this order:**
    1. Add the new key to `cluster/ci-dispatch/dispatcher/keyring/` (and forge-bot's, F3).
    2. Sign a new head of `realliance-ops/forge` with it, and wait for `ci_dispatch_config_verified` 1.
    3. Only then remove the old key.

    In this order the stored copy is always signed by a key still in the keyring.
  - **Clear the alert:** the counter stays above zero until the pod restarts. Once `ci_dispatch_config_verified` is 1, run `kubectl -n ci-dispatch delete pod ci-dispatch-0`. With no key rotated or expired, treat the alert as tampering.

### Gate G before, re-run after the rebase (2026-11-06T01:23:38Z to 01:26Z)

`main` was at 92ca5dee, and the branch was rebased onto it.

- G1: 3 nodes `Ready`, none `SchedulingDisabled`. 145/145 PGs `active+clean`. No hop, drain or recovery.
- G2: Ceph `HEALTH_OK`; only the three muted `AUTH_INSECURE_*` remain, after `MDS_CLIENT_RECALL` cleared. 9/9 OSDs up and in. Argo: 62 apps, all `Synced`; only `rook` is Degraded (`rook-cluster` is Healthy again with Ceph).
- G3: 51 edge rows. The normalised diff against 00:50Z is empty, and the certs are the same.
- G4:
  - No pod outside `Running`/`Succeeded`.
  - 5 NetworkPolicies, all in `netpol-canary`.
  - Memory requests: **top 86 %, middle 85 %, bottom 88 %**.
  - No pod over 100 restarts. One Warning event, `ResourceLimits` on `acid-chrismillerxyz`, unrelated.
  - `ci-dispatch` and `ci-tiny` don't exist, and neither does any `ci-dispatch` PVC.
- `scripts/netpol-verify.sh stray --since 15m` (`main`'s round 2, 920a2cf4) at 01:23:56Z: `stray: PASS (819 deny lines, policed: netpol-canary)`. Every window is covered, with no `GONE` or `STRAY` line. The count matches the NetworkPolicy ledger's 818 at 01:00Z.
- Deny lines naming `ci-dispatch/` or `ci-tiny/`, 15 min, all three agents: 0.
- `scripts/check-prometheus-rules.sh`: exit 0, `selected=41 groups=44 alerting=179 recording=82`; the only WARN is the existing `unselected music-streaming/gonic-alerts`.

### Push and Step 4 (2026-11-06)

- **Chris approved** the push ("Approve, if Gate G matches"). The 01:24Z Gate G before matched.
- **Pushed 2026-11-06T01:28:52Z**: `main` 92ca5dee → f2d295d1, a fast-forward of 6 signed commits.
- **Rollout:** pod `ci-dispatch-0` was created at 01:28:59Z on `top` and was `Ready` at 01:29:18Z with 0 restarts. PVC `ci-dispatch-ledger` was `Bound` (`rook-ceph-block-ssd`, 1Gi). The app was `Synced/Healthy` at 01:29:22Z.
- **`dry_run_start=2026-11-06T01:29:38Z`**: `Ready` at 01:29:18Z, and the first scrape, at 01:29:38Z, read `ci_dispatch_config_verified` 1.
- **Logs:** `serving` (tiny `dry-run`, the paid classes `off`) and `running ready=true` at 01:29:09Z, then decision lines only. No `WARN` or `ERROR` line, and no "start incomplete".
- **Metrics** (01:30:05Z):
  - `up{namespace="ci-dispatch"}` is one series at 1. `ci_dispatch_config_verified` and `config_ever_verified` are 1.
  - `class_mode{class="tiny"}` is 1 (dry-run). `store_rejected_total` is 0.
  - Every `errors_total` and `orphans` series is 0. `time() - loop_last_success` stays under 35 s, one scrape interval.
- **`scripts/check-prometheus-rules.sh`** (01:32:20Z): exit 0, `selected=42 groups=45 alerting=188 recording=82`. The `ci-dispatch` group's 9 rules are all `ok` and `inactive`, and no `CIDispatch*` or `CITiny*` alert exists.
- **Probe:**
  - A private scratch repo `forgejo-admin/ci-dispatch-dryrun-probe-8c8b3e` (repo id 292, owner id 1) got one `runs-on: tiny` workflow at 01:31:57Z.
  - At 01:32:04Z the dispatcher logged exactly one `decision`: key `203-1`, `action=launch`, `mode=dry-run`, ids only.
  - `ci_dispatch_launches_total{outcome="dry-run"}` became 1.
  - `ci-tiny` stayed empty (no Job, Pod or Secret) for more than 2 minutes, and no `ci-tiny-*` runner existed before or after.
  - Run 141 stayed `waiting`. The scratch repo was deleted at 01:34:18Z; the job left the listing, and `ci_dispatch_jobs{class="tiny"}` went back to 0.
- **`scripts/netpol-verify.sh stray --since 15m`** (01:44:42Z, 15 min after `Ready`): `stray: PASS (567 deny lines, policed: ci-dispatch,ci-tiny,netpol-canary)`.
- **Deny lines** naming `ci-dispatch/` or `ci-tiny/` over 15 min: 0 on each agent (190, 189 and 189 lines in total, all from the canary).
  - **D16 holds:** the dispatcher runs on `top`, so part of its API traffic goes to its own node's IP. None of it was denied.

### Gate G after (01:33–01:45Z)

It matches the 01:24Z before, apart from the two namespaces.

- G1: 3 nodes `Ready`, none cordoned. 145/145 PGs `active+clean`.
- G2: Ceph `HEALTH_OK`; 9/9 OSDs. Argo: 63 apps (with `ci-dispatch`, `Synced/Healthy`), only `rook` Degraded.
  - Two pushes from others followed: Renovate #2715 (the immich extensions-job digest) and phase 2 Task 3 (`azure/ci`, not rendered). Argo sat at 7ddbe09e, `Synced`.
- G3: 51 edge rows. The normalised diff is empty, and the certs are the same.
- G4:
  - No pod outside `Running`/`Succeeded`, and no Warning event since the push.
  - NetworkPolicies went from 5 to 11, the 6 new ones in `ci-dispatch` (`allow-dns`, `ci-dispatch`, `default-deny`) and `ci-tiny` (`allow-dns`, `default-deny`, `egress-internet-web`).
  - Memory requests: top 86 % (26604Mi, the dispatcher's 48Mi on top of 26556Mi), middle 85 %, bottom 88 %. At 01:34Z top briefly read 87 %, from a trivy-operator scan Job pod (256Mi) that finished.
- No rollback trigger fired: no crash loop, no denied API, Forgejo or DNS traffic, the config verified, and nothing else in Gate G changed.

## Task 19: `tiny` on (2026-11-06)

### Step 1: preconditions

- **Waived by Chris** (2026-11-06): Task 18 Step 5's 48 h dry-run compare, NetworkPolicy Task 7 Step 11's 24 h gate, and the ≤ 86 % memory gate. The 48 h compare and Step 11 still run later as post-checks.
- **Met:**
  - NetworkPolicy B2: `B2_done=2026-11-06T17:13:28Z`; `runner` PASS (22 rows, 19/19 deny-logged).
  - NetworkPolicy Task 9's restart gap: the agent was Ready at t0 + 7.1 s, and its rules were back at t0 + 6.3 s, the fail-open time. Both are in the NetworkPolicy ledger (8dd46cfd).
  - `docs/ci.md` is on `main` (2f920cf3 to cdd66dbe): the pause, the break-glass and the weekly check.
- **Memory requested** (17:21Z, after the general runner, pages and B2 deploys; forge-bot is not deployed): top **86 %** (26604Mi), middle **85 %** (26430Mi), bottom **88 %** (27135Mi). `NodeMemoryRequestsHigh` fires over 90 %. A `tiny` pod adds 512Mi, about 1.7 points, and its placement prefers nodes other than `top`.
- `ci.yaml` keeps `global.tiny_concurrent: 1`.

### Step 2: the change

- `cluster/ci-dispatch/dispatcher/config.yaml`: `classes.tiny` goes from `dry-run` to `"on"`, quoted, because YAML 1.1 reads a bare `on` as true. `test_config_values` expects `"on"`.
- Fail-first: with the expectation changed and the config not, `test_config_values` failed (`{'tiny': 'dry-run'} != {'tiny': 'on'}`). After the config change: `test_ci_dispatch_manifests.py` + `test_netpol_manifests.py` gave 52 passed, 1 skipped (the forge-bot keyring test). Every pre-commit hook passed.
- The render differs from `main` in the ConfigMap only: its data, and its name `ci-dispatch-config-mh8k58t484` → `ci-dispatch-config-4cfh56gm69`, which the StatefulSet references, and the pod restarts on it.
- The StatefulSet and Application header comments no longer say `tiny` is in dry-run.
- Commit: `ci-dispatch: tiny on (runner classes Task 19)`, signed, rebased (`-S`) onto 8dd46cfd.

### Step 3: Gate G before (17:21–17:23Z)

- **The STOP is pre-approved** (Chris, 2026-11-06): push once B2 and the restart gap pass and Gate G matches.
- G1: 3 nodes `Ready`, none cordoned. 145/145 PGs `active+clean`; 9/9 OSDs up and in. No hop, drain or recovery.
- G2: Ceph `HEALTH_WARN`, `MDS_CLIENT_RECALL` plus the three `AUTH_INSECURE_*` checks (the known noise). Argo: 63 apps, all `Synced`; only `rook` and `rook-cluster` Degraded.
- G3: 51 edge rows; the normalised diff against Task 18's after-snapshot is empty.
- G4:
  - No pod outside `Running`/`Succeeded`, and none over 100 restarts.
  - NetworkPolicies: `ci-dispatch` 3, `ci-tiny` 3, `forgejo-runner` 3, `netpol-canary` 5.
  - Memory requests as in Step 1.
- `ci-tiny` is empty. `ci-dispatch-0` is on `top`, 0 restarts, 15 h old. Metrics: `class_mode{class="tiny"}` 1 (dry-run), `config_verified` 1, `store_rejected_total` 0, every `errors_total` and `orphans` series 0, `launches_total{outcome="dry-run"}` 1 (Task 18's probe).
- Firing alerts: `Watchdog` only.
- `check-prometheus-rules.sh`: exit 0, `selected=42 groups=45 alerting=188 recording=82`, the one gonic WARN.
- `stray --since 15m` (17:22Z): `FAIL (0 of 752 deny lines stray, 1 agent logs short of 15m)`. The only cause is middle's agent `k456z`, 343 s old after Task 9's restart. `stray --since 5m`: PASS (275 deny lines, policed `ci-dispatch,ci-tiny,forgejo-runner,netpol-canary`). Deny lines naming `ci-dispatch/` or `ci-tiny/` over 15 min: 0 on each agent.
- The runners are idle: the galaxy runner's last tasks (realliance-ops/ci-dispatch 180 and 181) ended at 17:19:46Z, and the general runner's last (realliance.net 143) on 2026-11-05.

### Step 4: push and rollout

- **Pushed 2026-11-06T17:24:40Z**: `main` 8dd46cfd → 7fd0efb8, a fast-forward of 2 signed commits (c7ca1f7a the change, 7fd0efb8 this ledger).
- Argo created the new `ci-dispatch-0` at 17:24:43Z on `top` (ConfigMap `ci-dispatch-config-4cfh56gm69`) and was `Ready` at 17:25:04Z with 0 restarts; the app was `Synced/Healthy` at 17:25:04Z.
- **`tiny_on=2026-11-06T17:24:54Z`**: the log's `serving` line with `"tiny":"on"`, then `running ready=true`. No `WARN` or `ERROR`. Metrics: `class_mode{class="tiny"}` 2, `config_verified` 1, `store_rejected_total` 0, `up` 1.

### Step 5: acceptance (17:27:37–17:39:36Z, realliance/sso)

- **Two test fixes first,** both in realliance/sso PR #63, found at this first live run:
  - 9df453a, before the run: the egress test dialled the literal host `$PEER_IP`. NetworkPolicy Task 6 added two `$PEER_IP` rows after Task 16 wrote the suite, and the suite filled only `$WAN_IP`. It now skips `$HOST_IP` and `$PEER_IP`, as `test_forge_ci.py` does, and fails on any other variable: 23 rows (19 dropped, 3 open, 1 dns).
  - dffeb57, after the run: `test_tiny_job_hardened` asserted that `KUBERNETES_SERVICE_HOST` is unset. The kubelet sets it, and `KUBERNETES_PORT*`, in every container, even with `enableServiceLinks: false`. The live pod's environment had them; its spec had `enableServiceLinks: false`, no service account token, a read-only root, uid/gid 1000, all capabilities dropped, `RuntimeDefault` and no privilege escalation. The check is now that no other `*_SERVICE_HOST` exists.
- **Full run** (`CI_TINY_ACCEPTANCE=1 … pytest tests/forge/test_forge_ci_tiny.py -v`, sso ba3f9fc + 9df453a): **8 passed, 1 failed** in 11 min 59 s. The failure was `test_tiny_job_hardened`, on the `KUBERNETES_SERVICE_HOST` line only: with dffeb57, which changes that line alone, the test passed live (17:39:49Z, 17 s): every hardening property holds.
- **Not rolled back.** The trigger is a failing acceptance test; this one failed on an assertion the kubelet can never meet, and no property of the pod was missing. Chris or the coordinator may overrule; the rollback is `tiny: "off"` (docs/ci.md).
- 10 launches (keys 220-1 to 233-1, every one `try` 1): 9 settled `finished`, 1 reaped `gone-before-pickup` (the cancel half of `test_nothing_outlives_the_job`), charged 1 or 2 minutes each. `launches_total{outcome="error"}` and `{outcome="undone"}` 0, `reaped_total{reason="pickup"}` 0, every `errors_total` and `orphans` series 0. Holds and waits as designed: `owner-busy` (226-1), `owner-limit` (228-1, `forgejo-admin`), `label` (229-1, 231-1), `class-off` (230-1, `small`).
- **Start times, `ci_dispatch_start_seconds`** (first sighting to running, so it includes queue waits): 5 observations, sum 90.0 s; 2 ≤ 10 s, 2 in 10–20 s, 1 in 30–60 s (the second job of `test_owner_runs_one_at_a_time`, which waited `owner-busy` for 40 s). The dispatcher observes a start only when a tick sees the job running, so the five jobs that ended within one tick have none. No launch hit the 3 min pickup timeout, so `tiny.pickup` stays 3m.
- **`netpol-gate`:** 9 of the 10 pods captured (224's pod went before its gate finished). Each logged exactly **1** `egress not enforced yet; waiting` line, and each gate finished **6 s** after the pod's creation. Every pod ran on `middle`.

### Step 6: stray and canary

- `scripts/netpol-verify.sh canary` (17:40:58–17:43:09Z): `canary: PASS (3 nodes)`, bottom `rows 25/25 deny-logged 21/21`, `stray` PASS.
- `scripts/netpol-verify.sh stray --since 30m` (17:46:43Z): `stray: PASS (1589 deny lines, 87 from pods since gone or moved, policed: ci-dispatch,ci-tiny,forgejo-runner,netpol-canary)`. The 87 are the `ci-tiny` pods' lines.
- **The matrix expected every one of the 87 `ci-tiny` deny lines:** 3 per pod from `netpol-gate`'s API probe (to .240–.242:6443 after DNAT), and 60 from the egress job, 20 distinct targets, each a dropped row of the matrix or one of the two extras (github.com:22, 1.1.1.1:853). No deny line named `ci-dispatch/`, and none had a `ci-tiny` pod as destination. A live watch over the run printed no unexpected line.

### Step 7: sizing (17:43Z)

- `max_over_time(container_memory_working_set_bytes{namespace=~"ci-dispatch|ci-tiny"}[6h])`: dispatcher **10.9Mi** (6 h average 9.3–10.3Mi, the dry-run pod and the new one); `tiny` runner 3.5–19Mi (19Mi: the egress job's node probes), `netpol-gate` 3.1–3.6Mi.
- CPU, `max_over_time(rate(container_cpu_usage_seconds_total[5m])[6h:1m])`: dispatcher **0.5m** (average 0.3–0.4m); `tiny` runner 0.3–2.7m. The jobs are seconds long, so these rates understate their peaks.
- **Dispatcher request:** `max(48Mi, 10.9Mi × 1.25 = 13.6Mi)` = 48Mi, unchanged, so Step 7 needs no deploy. `tiny` stays at the spec's 250m/512Mi requests and 1/2Gi limits.

### Gate G after (17:43–17:46Z)

It matches the 17:21Z before.

- G1: 3 nodes `Ready`, none cordoned. 145/145 PGs `active+clean` (2 scrubbing); 9/9 OSDs.
- G2: Ceph `HEALTH_WARN`, `MDS_CLIENT_RECALL` and the muted `AUTH_INSECURE_*`. Argo: 63 apps, all `Synced`; only `rook` and `rook-cluster` Degraded.
- G3: 51 edge rows; the normalised diff is empty, and the certs are the same.
- G4: no pod outside `Running`/`Succeeded`, none over 100 restarts; NetworkPolicies unchanged (3, 3, 3, 5); memory requests top 86 %, middle 85 %, bottom 88 %, unchanged (no `tiny` pod running); `ci-tiny` empty; no Warning event in `ci-tiny` or `ci-dispatch`.
- `check-prometheus-rules.sh`: exit 0, `selected=42 groups=45 alerting=188 recording=82`.
- Alerts: no `NodeMemoryRequestsHigh`, `CIDispatch*` or `CITinyQuotaFull` in the last 30 min. **`PagesTakedownRequested` fires from 17:39:10Z, caused by this suite:** `test_owner_minutes_cap_holds` deletes a private `forgejo-admin` repo, and the pages relay turns every private repo delete into an `unpublish_requested` event (docs/pages.md: private repos aren't checked). It touched no site, and clears after 6 h, about 23:39Z.

### Step 8

- realliance/sso PR #63 (the two test fixes): draft → ready for review (17:47Z). **Chris** merges.
