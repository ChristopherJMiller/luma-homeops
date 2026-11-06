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
