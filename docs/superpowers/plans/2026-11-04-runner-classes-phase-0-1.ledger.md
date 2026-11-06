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
