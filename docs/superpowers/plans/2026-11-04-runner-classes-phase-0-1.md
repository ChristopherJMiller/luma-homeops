# Runner classes, phases 0 and 1: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to run this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Moved (2026-11-04):** since 2026-11-04 `ci-dispatch` lives at `realliance-ops/ci-dispatch` (id 240, plan `2026-11-04-bots-to-realliance-ops.md`), and so do its Go module and image. Its `image` job pushes as `ops-ci` with the `realliance-ops` org secret `REGISTRY_TOKEN`. The `galaxy-ci-dispatch` runner row matches by repo id, so it still serves the repo; a re-registration uses `--scope realliance-ops/ci-dispatch`. Tasks 1 to 5 keep the paths they ran with.

**Goal:** a member of the Realliance forge puts `runs-on: tiny` in a workflow and the job runs, with no setup, in a fresh hardened pod on galaxy that nothing else shares and that goes away with the job. The `ci-dispatch` controller gates every launch on the owner's signed limits, accounts for it in a ledger, and reaps anything that outlives its job. Phase 0 first proves the Forgejo 15 mechanics on the live forge and runs the one approved Spot probe.

**Architecture:**
- `ci-dispatch` (Go, one replica, StatefulSet in namespace `ci-dispatch`) polls `GET /admin/actions/runners/jobs` every 5 s.
- For each waiting `tiny` job that passes the gates, it writes a reservation to its SQLite ledger. It then registers an ephemeral instance runner, and creates one suspended batch Job in `ci-tiny` with a per-job Secret owned by that Job. Last, it resumes the Job.
- The pod waits in the `netpol-gate` init container until the enforcer covers its egress, then runs `forgejo-runner one-job --wait --handle <h>`. The handle binds the runner to that one job attempt.
- Limits come from `ci.yaml` in the private inventory `realliance-ops/forge`, read only at an Owner-signed head and checked locally with OpenPGP. Class modes (`off`, `dry-run`, `on`) come from a luma-homeops ConfigMap, which is Chris's switch.
- A read-only usage API on a ClusterIP Service serves the numbers the Forgejo fork's runner page uses later.

**Tech Stack:** Forgejo 15.0.11 LTS (chart 17.3.2); forgejo-runner v13.2.1 (`one-job`, `--handle`, `--token-url file:`); Go 1.26 (toolchain go1.26.10) with the standard library plus exactly `github.com/ProtonMail/go-crypto` v1.5.2, `go.yaml.in/yaml/v3` v3.0.5 and `modernc.org/sqlite`; ko onto distroless static; Kubernetes v1.33.13 batch Jobs, ResourceQuota, RBAC, Pod Security `restricted`; kube-network-policies v1.1.3 components; kube-prometheus-stack (ServiceMonitor, PrometheusRule, `promtool`); Argo CD; SealedSecrets; OpenTofu `svalabs/forgejo` 1.7.0 (the inventory); `az` (the Spot probe only); pytest in `realliance/sso`.

**Spec:** `docs/superpowers/specs/2026-11-04-runner-classes-design.md`, approved by Chris on 2026-11-04 (commit a0d79f07). Chris answered its decisions: probe Spot once in phase 0; no gVisor; the $120 subscription budget already exists in `azure/logging/`. Private research: `.superpowers/runners-research/findings.md` and `.superpowers/pages-research/tiny-runner.md`. Both stay out of git, and no subscription, tenant or resource id from them goes anywhere: say "the Realliance subscription".

## Global Constraints

### Pins (resolved read-only, 2026-11-04)

| What | Value | Source and notes |
|---|---|---|
| Forgejo | 15.0.11 (`/swagger.v1.json` reports `15.0.11+gitea-1.22.0`) | `POST /admin/actions/runners` takes `{name, description, ephemeral}` and returns `{id, uuid, token}`. `GET /admin/actions/runners/jobs?labels=` returns `id, attempt, handle, name, needs, owner_id, repo_id, runs_on, status, task_id`. `GET /admin/actions/runners` pages with `page`/`limit` and returns `id, uuid, name, status, ephemeral, owner_id, repo_id, labels, version`. `DELETE /admin/actions/runners/{id}` exists. |
| forgejo-runner | v13.2.1. `one-job` flags: `--wait`, `--handle`, `--url`, `--uuid`, `--token-url`, `--label` (repeatable), `--config` | source at tag `v13.2.1`, `internal/app/cmd/cmd.go`. `--token-url` takes only `file:` URLs (`ResolveSecretURL`). `one-job` refuses more than one connection. |
| Proof pod image | `ghcr.io/christopherjmiller/forgejo-runner:sha-6b269ab@sha256:3bb2fca689b0c986039be061a4201580488c30bcdcb476815f80d737f97b76e9` | the `galaxy` runner's image in `cluster/forgejo-runner/deployment.yaml` (runner 13.2.1, node, uid 1000) |
| `tiny` image | `ghcr.io/christopherjmiller/forgejo-runner-general:sha-<7>@sha256:<64>` | the realliance.net plan's Task 3A pin, consumed unchanged (R2) |
| Dispatcher image | `git.realliance.net/realliance-ops/ci-dispatch:sha-<7>@sha256:<64>` | set in Task 14 |
| ko base | `gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab` | forge-bot's `.ko.yaml` |
| Go modules | `github.com/ProtonMail/go-crypto v1.5.2`, `go.yaml.in/yaml/v3 v3.0.5` (forge-bot's), `modernc.org/sqlite v1.61.0` (2026-10-21) | Task 6 Step 1 re-reads `proxy.golang.org/<module>/@v/<v>.info` and takes the newest release at least 2 weeks old |
| CI pins | `actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.1.0`, `golang.org/x/vuln/cmd/govulncheck@v1.9.0` | forge-bot's `ci.yaml` |
| API server endpoints | `192.168.0.240`, `.241`, `.242`, TCP 6443 | `kubectl get endpointslices -n default -l kubernetes.io/service-name=kubernetes`, 2026-11-04 |

### Values fixed by the spec or by this plan

- **Names.** Argo app `ci-dispatch`, path `cluster/ci-dispatch/` with sub-kustomizations `dispatcher/` (namespace `ci-dispatch`) and `tiny/` (namespace `ci-tiny`), both Pod Security `restricted`. StatefulSet, Service and ServiceAccount `ci-dispatch`. PVC `ci-dispatch-ledger` (1Gi, `rook-ceph-block-ssd`, RWO). ConfigMaps `ci-dispatch-config` and `ci-dispatch-keyring` (hash-suffixed) and `ci-tiny-runner` in `ci-tiny`. SealedSecrets `ci-dispatch-forgejo` (key `forgejo-token`) and `ci-dispatch-usage` (key `usage-token`). PrometheusRule `ci-dispatch-alerts`. Component `cluster/components/netpol/egress-internet-web`.
- **Forge.** Local site-administrator account `ci-dispatch` (`ci-dispatch@realliance.net`). Its token `CI_DISPATCH_FORGEJO_TOKEN` holds exactly `read:admin,write:admin,read:repository,read:user,read:organization`. Usage token `CI_DISPATCH_USAGE_TOKEN`. Both canonical in `~/Repos/realliance-sso-secrets/forge/secrets.env`. Repo `realliance-ops/ci-dispatch` (public, Go module `git.realliance.net/realliance-ops/ci-dispatch`, MIT OR Apache-2.0). Second `galaxy` registration `galaxy-ci-dispatch` at scope `realliance/ci-dispatch`, secret `RUNNER_CI_DISPATCH_SECRET` in `forge/runner.env`.
- **Runner names.** `ci-tiny-<job>-<attempt>-<try>`, always ephemeral, always instance scope. The reaper touches a runner only if all three hold (prefix `ci-`, `ephemeral`, `owner_id == 0 && repo_id == 0`).
- **`tiny` shape (spec).** Job: `suspend: true` at create, `backoffLimit: 0`, `activeDeadlineSeconds: 1200`, `ttlSecondsAfterFinished: 0`. Runner `--label tiny:host`, capacity 1, timeout 15m, cache off. Pod: uid/gid 1000, read-only root, all capabilities dropped, `RuntimeDefault`, no service account token, no service links. Requests `250m`/`512Mi`, limits `1` CPU and `2Gi`. `/work` is an `emptyDir` with `medium: Memory`, `sizeLimit: 1Gi`. Placement prefers nodes other than `top` and spreads across nodes. Quota in `ci-tiny`: `pods: 3`, `requests.cpu: 750m`, `requests.memory: 1536Mi`, `limits.memory: 6Gi`.
- **Gates, limits and timing (spec).** Loop 5 s, sweep 5 min. `tiny` defaults: 1 job at once per owner, 120 minutes a UTC day, 2 pods globally. Each launch reserves 20 minutes. Pickup timeout 3 min; after 3 failed launches the job holds as `launch-failing`. Orphans go after 10 min.
- **Label routing.** The listing query is `labels=tiny,small,medium,large,docker` from the start (D13). Only `tiny` can be anything but `off` in phase 1; `config.Load` rejects any other class in another mode.
- **Ids, not names.** Logs, metrics, the ledger and the usage API carry owner and repo ids only. Metrics carry no owner at all.
- **Periods.** A period is a UTC calendar month *(Chris)*. The `period=` parameter of the usage API takes `current`, `previous` or `YYYY-MM`.

### How changes land

- **luma-homeops** (public). Work in the worktree `~/Repos/luma-homeops-ci` on branch `ci-dispatch` (`git -C ~/Repos/luma-homeops worktree add ~/Repos/luma-homeops-ci -b ci-dispatch origin/main`), never in the main checkout, which other agents share. `git add` only the task's paths. Commit with `nix develop --command git commit -S …`; pre-commit runs yamlfmt, kustomize-build, `netpol-manifests`, `pages-manifests`, the other plans' hooks and, from Task 17, `ci-dispatch-manifests`. If a hook rewrites a file, `git add` it and commit again. Land with `git fetch origin && git rebase origin/main && git push origin HEAD:main`.
- **A push that changes what Argo renders is a deploy, and every deploy is a STOP.** Docs-only, ledger-only, `images/` and `.github/` pushes are routine.
- **`realliance-ops/ci-dispatch`** (forge, public; clone `~/Repos/ci-dispatch`): signed commits on branch `v1`, a PR, **Chris** merges fast-forward (`fj -H git.realliance.net pr merge realliance-ops/ci-dispatch#<n> -d`).
- **`realliance-ops/forge`** (inventory, private; `~/Repos/realliance-forge`): a branch, a signed commit, `fj … pr create`; **Chris** merges fast-forward so the head stays Owner-signed. Apply from the laptop with the README's env, only after its STOP.
- **`realliance/sso`** (GitHub, public): signed commits on a branch, `gh pr create --fill`; merge only when Chris says merge. This plan adds only `tests/ci_helpers.py`, `tests/forge/test_forge_ci_dispatch.py`, `tests/forge/test_forge_runner_proofs.py` and `tests/forge/test_forge_ci_tiny.py`. It never edits `forge_helpers.py`, `conftest.py` or `pytest.ini`.
- **`realliance/sso-secrets`** (private): `git commit -S`, push `main` (covered by the start OK).
- **Every commit** in every repo ends with the attribution lines the session's harness gives (the running model's `Co-Authored-By` and the `Claude-Session` line), never a hard-coded model name.

- Every shell that signs or uses ssh/gpg exports `DISPLAY=:10.0 XAUTHORITY=$HOME/.Xauthority`. Never `--no-verify` or `--no-gpg-sign` (S6).
- **STOP—ask Chris** is one AskUserQuestion that states the change, the Gate G evidence, what to watch and the rollback trigger. Options: `approve: do it, and roll back at once if a trigger fires` and `hold`. A rejected or interrupted call may already have run, so read live state before redoing anything.
- **Rollback.** Revert the task's commit. For the first `ci-dispatch` push, set `cluster/ci-dispatch/kustomization.yaml` to `resources: []`; Argo prunes the rest (`allowEmpty: true`) but leaves the Namespace `ci-dispatch` and the PVC (both `Prune=false,Delete=false`), so the ledger survives. Never roll back by deleting an `Application` file. Stopping all `tiny` work at once is the spec's break-glass: scale `ci-dispatch` to 0 (S2 incident exception), then delete the Jobs in `ci-tiny`.
- **Ledger:** `docs/superpowers/plans/2026-11-04-runner-classes-phase-0-1.ledger.md`. Answers, Gate G summaries, proof results, timings, digests and rulings. No secrets, no Azure ids, no hidden org, team or group names.
- **Waits:** `Monitor` for anything over 60 s, never a sleep loop.

### Gate G (before every deploy push, and again after)

- G1. No Talos or Kubernetes hop, drain or Ceph recovery. 3 nodes `Ready`, none `SchedulingDisabled`; every PG `active+clean`.
- G2. A `cluster-snapshot` summary in the ledger. Baseline (2026-11-04): Ceph `HEALTH_OK` or the known `HEALTH_WARN` noise, 145/145 PGs `active+clean`, and in Argo only `rook`/`rook-cluster` not Healthy.
- G3. `cd .claude/skills/edge-ingress && nix-shell -p openssl jq --run ./edge-snapshot.sh > $SCRATCH/edge-<task>-<before|after>.txt`, compared with the netpol plan's ruled normalisation: `diff <(sed 's/ cert=.*//' before | sed -E 's/(code_challenge|state|nonce)=[^& ]*/\1=X/g') <(…after…)`.
- G4. `kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded`, `kubectl get netpol -A`, and memory requests per node (`kubectl describe node <n> | grep -A8 'Allocated resources'`). Expected (2026-11-04): top 85 %, middle 85 %, bottom 87 %.

### Cluster, secrets, compute

- `export KUBECONFIG=/tmp/galaxy-kubeconfig`; refresh from Talos (CLAUDE.md) if rejected. `kubectl`, `kubeseal`, `az` and `promtool` run through `nix develop --command …` in luma-homeops; pytest and tofu through `nix develop --command …` in `realliance-sso`.
- `SCRATCH=~/Repos/luma-homeops/.superpowers/sdd/2026-11-04-runner-classes-phase-0-1/scratch` (git-ignored). Define it in every shell that uses `$SCRATCH`.
- Never `Read`, `cat` or diff a `*.secret.yaml`, `nodes/*`, `forge/secrets.env` or `forge/runner.env`. Values travel through pipes, stdin and `umask 077` files, never argv, echo, assertion messages, logs, PR text or the ledger. In git-crypt repos use `git diff --no-textconv`.
- Azure output never reaches the ledger with an id in it: every `az` call uses `-o none` or a `--query` that selects named, id-free fields.
- Go, pinned to 4 threads: `GO="taskset -c 0-3 nix develop $HOME/Repos/luma-homeops --command env GOMAXPROCS=4 go"`, then `$GO test -race -p=4 ./...`. No local docker or kind; CI builds images.
- CLAUDE.md S1 to S9 apply: read live state before every write, GitOps only, patience over retries, confirm destructive actions.

## Decisions this plan makes

- **D1. Phasing follows the spec's table.** The `ci-dispatch` account and token, `realliance/ci-dispatch` in the inventory, and its `galaxy` registration are phase 0. They come before the proofs, because the proofs register runners with the real token and so prove its scopes. The token's SealedSecret is phase 1 (Task 17).
- **D2. The Spot probe uses a throwaway resource group,** `rg-realliance-ci-spot-probe`, deleted at the end of Task 4. The CI resource group belongs to phase 2's `azure/ci/` terraform, which Chris applies, and creating it by hand now would leave state drift for that plan.
- **D3. Three modules beyond the standard library, no more.**
  - `ProtonMail/go-crypto` checks OpenPGP signatures and `go.yaml.in/yaml/v3` parses `ci.yaml`; forge-bot uses both versions already.
  - `modernc.org/sqlite` is the SQLite driver. Pure Go, so ko keeps `CGO_ENABLED=0` and the distroless static base.
  - Metrics are hand-written text exposition, as in `pages-relay`, not `client_golang`.
  - `TestDirectRequires` fails the build if `go.mod` requires any other module directly; `govulncheck` runs in CI. The Kubernetes and Forgejo clients are hand-written (spec).
- **D4. Copy the signature checks; don't import them.** `internal/signed` is forge-bot v0.1.0's `internal/inventory/verify.go` (commit 9be4857) plus its real-git fixture, because Go forbids importing another module's `internal/`. The header names the source commit.
- **D5. StatefulSet, not Deployment with `Recreate`.** forge-bot's Task 16 review (I1) showed that a Deployment with `Recreate` is no singleton under eviction or drain. `ci-dispatch` also takes `flock(LOCK_EX|LOCK_NB)` on `/data/.lock` at start and exits 1 if another process holds it. The ledger's partial unique index (one active launch per job and attempt) is the third guard. The PVC and the Namespace `ci-dispatch` carry `argocd.argoproj.io/sync-options: Prune=false,Delete=false`; pruning a Namespace deletes its PVC whatever the PVC says.
- **D6. Memory `emptyDir` for `/work`, and no new StorageClass in phase 1** (spec, "Why memory, not disk").
- **D7. Every phase 1 alert is weekday (`severity: warning`).** `CIDispatchOrphanCompute` moves to the 24/7 route in phase 2, when orphans cost money. A `tiny` orphan pod dies at its Kubernetes deadline anyway. Phase 1 edits no `Alertmanager` config.
- **D8. Ledger backup.** Phase 1 code writes a nightly consistent copy (`VACUUM INTO /data/snapshots/ledger-<date>.db`, keeping 7). The restic CronJob and its host land in phase 2, before any paid class, which fits the spec's rule that paid classes stay off until a restore. Losing a phase 1 ledger costs one day of `tiny` minutes.
- **D10. The member's guide is `docs/members.md` in `realliance-ops/ci-dispatch`,** because the forge has no wiki yet. It moves to the wiki once one exists.
- **D11. `forgejo-admin` is the caps test owner.** `ci.yaml` gives it `tiny_minutes_per_day: 20`, exactly one reservation, so the acceptance suite can prove an `owner-limit` hold with a real job. The break-glass account never needs CI.
- **D12. Dry-run writes no launch rows.** It may write holds. It logs each decision once, counts `ci_dispatch_launches_total{outcome="dry-run"}` once per decision change, and launches nothing.
- **D13. The listing query includes every class label from phase 1 on,** so a `[tiny, docker]` or `[small, medium]` job shows as a `label` hold in the usage API instead of waiting unseen. Paid classes stay `off`, so their jobs wait.
- **D14. Phase 0's proofs are a pytest drill** (`tests/forge/test_forge_runner_proofs.py`, `CI_DRILL=runner-proofs`). The Forgejo 17 and 19 LTS upgrades re-run it, since everything here rests on the `--handle` semantics.
- **D15. The usage API (port 8081) gets no ingress rule in phase 1.** Tests reach it through `kubectl port-forward`, which enters the pod over localhost. The rule for the Forgejo pods arrives with the fork page.
- **D16. The dispatcher's API server rule** allows TCP 6443 to the three node IPs, the post-DNAT endpoints of `kubernetes.default`. Task 18 proves it against deny logs.
- **D17. Launch order: reserve, register, Job (suspended), Secret owned by the Job, resume.** The Secret needs the Job's uid, and a suspended Job starts no pod until its Secret exists. `blockOwnerDeletion` stays unset, so the Role needs no `finalizers` right.
- **D18. Handles and the runner row.** The dispatcher accepts a handle only if it matches `^[A-Za-z0-9_-]{1,128}$` and passes it as an argv element, never through a shell. Task 3 ledgers when Forgejo deletes an ephemeral runner row (at pickup or at job end) and the `status` values on the way. Task 12's pickup check keys on those values; the default assumption: the row exists, and the job still shows `waiting`.
- **D19. `ForgeUnexpectedAdmin` belongs to forge-bot** (a follow-up for that plan). Until it ships, Task 1 and `docs/ci.md` record the expected local accounts (`forgejo-admin`, `forge-bot`, `forge-ci`, `ops-ci`, `ci-dispatch`), and the runbook's weekly check compares `GET /admin/users` (administrators and local accounts) and the non-ephemeral runners against them.

## Review focus

1. **A job cancelled or superseded after its runner registered, before pickup.** A person expects the pod and the runner to go away and the owner to pay at most a minute. Expected: gone within the 3-minute pickup timeout, no `ci-*` runner left, charged ≤ 1 minute. Tests: Task 12 `TestReapJobGoneBeforePickup`; Task 16 `test_nothing_outlives_the_job`.
2. **A re-run of a finished job** (same job id, attempt 2). Expected: a new launch under the new key, never blocked by attempt 1's ledger row. Tests: Task 10 `TestReserveNewAttempt`; Task 12 `TestRerunLaunches`.
3. **A dispatcher restart between `RegisterRunner` and `CreateJob`.** Expected: on start, reconcile deletes the orphan runner and frees the reservation before any new launch; no double launch. Test: Task 12 `TestStartupReconcileAfterCrashMidLaunch` (table over every launch step).
4. **A held job logs every 5 s for 24 h.** The Azure workspace has a 0.25 GB daily cap (commit 09578b8d). Expected: one log line per decision change per key, and none per loop. Test: Task 12 `TestDecisionLogOncePerChange`.
5. **The UTC midnight boundary.** A `tiny` job reserved at 23:55 that ends at 00:10. Expected: charged to the day it launched, and no double charge. Test: Task 10 `TestTinyMinutesChargedToLaunchDay`.
6. **Someone's own repo runner named `ci-…`.** Expected: the reaper never deletes it, because it carries a repo or owner scope. Test: Task 12 `TestReaperIgnoresForeignRunners`.

## Coordination with concurrent plans

**NetworkPolicy plan** (`docs/superpowers/plans/2026-11-03-networkpolicy-enforcement.md`; its ledger on 2026-11-04 shows A1, A2 and A3a done, `A3a_start=2026-11-03T18:35:10Z`; A3b no earlier than 2026-11-04T18:35:10Z; then Tasks 6 and 7, at least 24 h green, then Task 8, which is B2):
- N1. Tasks 0 to 16 need nothing from it. Task 17 needs its Task 7 on `main` (it creates `cluster/components/netpol/{default-deny,allow-dns,egress-internet-only}`), so schedule Task 17 after that, and run Tasks 15 and 16 meanwhile. The first `ci-dispatch` push (Task 18) waits for `B2_done` in that ledger and a current `scripts/netpol-verify.sh runner` PASS. `tiny: on` (Task 19) also waits for its Task 9's ledgered restart gap. The earliest `B2_done` is about 2026-11-06.
- N2. This plan adds one component, `cluster/components/netpol/egress-internet-web/`. It never edits the other components, `scripts/test_netpol_manifests.py`, `scripts/netpol-verify.sh` or `cluster/netpol-canary/*`. If `netpol-manifests` needs an entry for `ci-dispatch` or `ci-tiny`, the NetworkPolicy owner adds it (ask through Chris), and Task 18 waits.
- N3. Serialize STOPs: no deploy of this plan between another plan's Gate G before and after runs. Also push nothing to a `realliance-ops/ci-dispatch` branch (each push starts a `galaxy` run) while its Task 8 Steps 1 to 11 or its Task 9 is open.
- N4. Task 3's probe pods carry no policy, so no agent enrolls them, and they never run inside that plan's `idle`, baseline or canary measurement windows.
- N5. After Task 5 the `galaxy` runner serves `realliance/ci-dispatch` too. Its runner-idle check (B2 Step 1, Task 9 Step 1) then covers `/repos/realliance-ops/ci-dispatch/actions/tasks` (`realliance/ci-dispatch` before 2026-11-04); write that into its ledger at Task 5. After Task 19, every enforcer restart, Talos hop or kube-network-policies bump first pauses `tiny` and checks that `ci-tiny` has no pods (`docs/ci.md`).

**forge-bot plan** (`docs/superpowers/plans/2026-11-02-forge-bot-and-ci.md`; Task 16 deploy held on branch `forge-bot-deploy`):
- F1. Other plans edit `.renovaterc.json` too; rebase immediately before every commit that touches it.
- F2. Task 5 changes `cluster/forgejo-runner/runner-config.secret.yaml`, which forge-bot's CI and NetworkPolicy B2 also depend on. It runs only with no forge-bot CI job in flight and outside B2's window.
- F3. `cluster/ci-dispatch/dispatcher/keyring/*` is byte-identical to `cluster/forge-bot/keyring/*`; a static test enforces that once both exist on `main`.
- F4. Hand the expected administrator list (D19) to that plan's executor through Chris, for `ForgeUnexpectedAdmin`.

**realliance.net plan** (`docs/superpowers/plans/2026-11-04-realliance-net-on-forge.md`):
- R1. Its rule F3 ("the `galaxy` runner's allowlist never changes") gives way for `realliance-ops/ci-dispatch` (id 240) only, by the approved spec ("Build"). Ledger that in its ledger at Task 5.
- R2. Task 17 consumes its Task 3A image pin and waits for it. While its `general` runner exists, a static test keeps the two pins equal.
- R3. Retiring `general` into `tiny` is the spec's phase 1b, a follow-up here.

**Realliance Pages plan:** serialize STOPs (N3).

## File map

| Path | Responsibility | Task |
|---|---|---|
| `docs/superpowers/plans/2026-11-04-runner-classes-phase-0-1.ledger.md` | ledger | 0 |
| `~/Repos/realliance-sso/tests/ci_helpers.py` | shared helpers for the three test files | 1, 3, 16 |
| `~/Repos/realliance-sso/tests/forge/test_forge_ci_dispatch.py` | account, token scopes, repo, `galaxy` registration | 1, 2, 5 |
| `~/Repos/realliance-sso/tests/forge/test_forge_runner_proofs.py` | phase 0 drill | 3 |
| `~/Repos/realliance-sso/tests/forge/test_forge_ci_tiny.py` | phase 1 acceptance | 16 |
| `~/Repos/realliance-forge/repos.tf`, `README.md`, `ci.yaml` | repo declaration; signed limits | 2, 15 |
| `cluster/forgejo-runner/runner-config.secret.yaml` → `runner-config.yaml` | second `galaxy` connection | 5 |
| `~/Repos/ci-dispatch/**` | the controller | 6–14 |
| `cluster/applications/ci-dispatch.yaml`, `cluster/applications/kustomization.yaml`, `cluster/ci-dispatch/**`, `cluster/components/netpol/egress-internet-web/*` | deploy | 17–19 |
| `scripts/test_ci_dispatch_manifests.py`, `scripts/test-ci-dispatch-alerts.sh`, `.pre-commit-config.yaml`, `.renovaterc.json` | static tests, alert tests, hook, Renovate rule | 17 |
| `docs/ci.md`, `CLAUDE.md`, `docs/forgejo.md` | runbook, topology row, pointer | 20 |

---

## Phase 0: proofs

### Task 0: start

**Files:**
- Create: `docs/superpowers/plans/2026-11-04-runner-classes-phase-0-1.ledger.md` (sections: Answers, Baseline, one per task)

- [ ] **Step 1: Worktree.** Run the worktree command under How changes land. Expected: a clean tree at `origin/main`.
- [ ] **Step 2: Record the answers.** Chris answered on 2026-11-04, so ask nothing. Ledger, under Answers:
  - credit periods are calendar months (the old Q1 is withdrawn);
  - `modernc.org/sqlite` accepted (Q2: yes);
  - `tiny` may go on before `ForgeUnexpectedAdmin`, with the weekly check in `docs/ci.md` (Q3: yes);
  - the Spot probe, pre-approved in exactly the shape Task 4 states;
  - the start OK covers routine pushes: luma-homeops docs and ledger, `realliance-ops/ci-dispatch` branch pushes and PRs, sso-secrets commits, `realliance/sso` test branches.

  Anything not covered stays a STOP.
- [ ] **Step 3: Baseline.** Gate G1, G2 and G4; G3 to `$SCRATCH/edge-ci-0.txt`. Also ledger:
  - `ceph df` for `block-pool-ssd` MAX AVAIL (Expected: about 138 GiB);
  - `GET /api/v1/admin/users` filtered to `is_admin` with `FORGEJO_ADMIN_TOKEN`, logins only (Expected: `forgejo-admin`, `forge-bot` and the human Owners).
- [ ] **Step 4: Cross-plan reads.** Ledger, from their ledgers and `progress.md`: the NetworkPolicy state (A3b, B1, `B2_done`), the realliance.net Task 3A pin state, and forge-bot's Task 16 state.
- [ ] **Step 5: Commit and push** the ledger (docs-only): `docs: runner classes phase 0-1 ledger (Task 0)`.

### Task 1: The `ci-dispatch` account and its token

**Files:**
- Create: `~/Repos/realliance-sso/tests/ci_helpers.py`, `tests/forge/test_forge_ci_dispatch.py` (branch `ci-dispatch`)
- Edit (private): `~/Repos/realliance-sso-secrets/forge/secrets.env` (`CI_DISPATCH_FORGEJO_TOKEN`)

**Interfaces:**
- Produces, in `ci_helpers`:
  - `CI_TOKEN_KEY = "CI_DISPATCH_FORGEJO_TOKEN"`; `ci_token() -> str` reads it from `FORGE_SECRETS` through `_load_env`, never printing it.
  - `EXPECTED_ADMINS = {"forgejo-admin", "forge-bot", "forge-ci", "ci-dispatch"}` (local accounts; the test adds the Owners at run time from the `realliance-ops` Owners team).
- Produces: the local account `ci-dispatch` (site administrator, `source_id` 0, `prohibit_login` false) and its token.

- [ ] **Step 1: Write the failing tests.**

```python
def test_ci_dispatch_account_local_admin():
    a = forge_api("GET", "/api/v1/users/ci-dispatch").json()
    assert (a["source_id"], a["is_admin"], a["prohibit_login"]) == (0, True, False)

def test_ci_dispatch_token_scopes():
    t = ci_token()
    assert forge_api("GET", "/api/v1/admin/actions/runners/jobs", token=t, params={"labels": "tiny"}).status_code == 200
    assert forge_api("GET", "/api/v1/admin/actions/runners", token=t).status_code == 200
    assert forge_api("GET", f"/api/v1/repos/{INVENTORY}/branches/main", token=t).status_code == 200
    assert forge_api("GET", "/api/v1/users/forgejo-admin", token=t).status_code == 200
    assert forge_api("POST", "/api/v1/user/repos", token=t, json={}).status_code == 403   # no write:repository; an invalid body, so a wrong scope answers 422 and creates nothing
    assert forge_api("PATCH", f"/api/v1/repos/{FORGE_BOT_REPO}", token=t, json={"description": 1}).status_code == 403   # invalid body: 422 would mean a wrong scope, and nothing changes

def test_site_admins_as_expected():          # D19, the interim check
    admins = {u["login"] for u in forge_api_paged("/api/v1/admin/users") if u["is_admin"]}
    assert admins <= EXPECTED_ADMINS | team_members("realliance-ops", "Owners")
```

  `forge_api_paged` lives in `ci_helpers` (the same paging as `forge_helpers._paged`, which is private).
- [ ] **Step 2: Run them.** `nix develop --command pytest tests/forge/test_forge_ci_dispatch.py -v`. Expected: FAIL (404 for `ci-dispatch`; `KeyError` for the token). `test_site_admins_as_expected` PASSES, as the guard.
- [ ] **Step 3: Create the account** (S1 first: `kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin user list --admin`). Run `kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin user create --username ci-dispatch --email ci-dispatch@realliance.net --admin --random-password --must-change-password=false >/dev/null`. The password is never seen and never needed: internal sign-in and basic auth are off. Never set `prohibit_login` on this account; it would stop its token too.
- [ ] **Step 4: STOP—ask Chris, then mint the token** straight into sso-secrets. The token can read every private repo and create administrators (spec, "The administrator token"):

```bash
cd ~/Repos/realliance-sso-secrets
kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin user generate-access-token --username ci-dispatch \
  --token-name ci-dispatch --scopes "read:admin,write:admin,read:repository,read:user,read:organization" --raw \
  | { read -r t; printf 'CI_DISPATCH_FORGEJO_TOKEN=%s\n' "$t" >> forge/secrets.env; }
git add forge/secrets.env && git commit -S -m "forge: ci-dispatch API token" && git push
```

- [ ] **Step 5: Run the tests.** Expected: PASS. Ledger the administrator list (logins only) and the mint time.
- [ ] **Step 6: Commit** the sso tests (signed; PR later, with Task 5).

### Task 2: `realliance/ci-dispatch` in the inventory

**Files:**
- Edit (inventory, branch `ci-dispatch-decl`): `repos.tf`, `README.md`
- Extend: `tests/forge/test_forge_ci_dispatch.py`

**Interfaces:**
- Produces: `forgejo_repository.ci_dispatch` and `forgejo_branch_protection.ci_dispatch_main`, shaped like `forge_bot` and `forge_bot_main`; a hand-set `v*` tag protection (Owners); the local clone `~/Repos/ci-dispatch`.

- [ ] **Step 1: Write the failing test** `test_ci_dispatch_repo_public_and_protected`: the body of `test_forge_bot_repo_public_and_protected` (`test_forge_inventory.py`) with `realliance/ci-dispatch`. Add `test_ci_dispatch_registry_secret_exists`: the name `REGISTRY_TOKEN` appears in `GET /repos/realliance/ci-dispatch/actions/secrets` (never its value). Run them. Expected: FAIL (404).
- [ ] **Step 2: Declare it.** `repos.tf` gets the two resources with the comment `Runner classes controller: gates and launches one-job runners per queued job (Go). Public; its code is not secret. Same shape as forge-bot. auto_init: protection needs an existing main. An Owner sets the v* tag protection by hand.` Description: `Realliance CI dispatcher: one fresh runner per job, within each owner's limits (Go)`. `has_actions = true`, `has_packages = true`, `prevent_destroy`. README: add it to the critical-repos list.
- [ ] **Step 3: Plan.** Expected: `2 to add, 0 to change, 0 to destroy`. Commit signed, push, open the PR. **Chris:** merges fast-forward.
- [ ] **Step 4: STOP—ask Chris, then apply.** Expected: `2 added`. Then `tofu plan`: `No changes.`
- [ ] **Step 5: Tag protection and clone.** `forge_api("POST", "/api/v1/repos/realliance/ci-dispatch/tag_protections", json={"name_pattern": "v*", "whitelist_teams": ["Owners"]})` returns 201. `git clone ssh://git@git.realliance.net/realliance/ci-dispatch.git ~/Repos/ci-dispatch`.
- [ ] **Step 5b: Registry secret.** The `image` job logs in with `REGISTRY_TOKEN`, as forge-bot's does. A `python -I` script reads the `forge-ci` token from sso-secrets and sends `PUT /repos/realliance/ci-dispatch/actions/secrets/REGISTRY_TOKEN` with the value in the JSON body, never in argv or output. The workflow's header comment already carries the accepted-risk text.
- [ ] **Step 6: Run the tests.** Expected: PASS, and `test_forge_inventory.py`'s drift test stays green.

### Task 3: The Forgejo 15 proofs (drill)

**Files:**
- Create: `~/Repos/realliance-sso/tests/forge/test_forge_runner_proofs.py`
- Extend: `tests/ci_helpers.py`

**Interfaces:**
- Produces, in `ci_helpers`:
  - `push_workflow(repo: str, wf: dict, branch: str | None = None) -> tuple[str, str]`: `push_probe_workflow`'s mechanics with a whole workflow, for `needs:`, `concurrency:` and list `runs-on`.
  - `list_jobs(labels: list[str], token: str) -> list[dict]`: `GET /admin/actions/runners/jobs`.
  - `register_ephemeral(name: str, token: str) -> dict`: `POST /admin/actions/runners` with `ephemeral: true`; the result stays in memory and never reaches a log or assertion message.
  - `runner_rows(token: str) -> list[dict]`: paged `GET /admin/actions/runners`.
  - `probe_pod(name: str, handle: str, reg: dict, label: str, *, ns: str = "ci-probe") -> None`: applies a Secret (`token`, on stdin) and a Pod with the `tiny` pod shape that Global Constraints fixes, running `forgejo-runner one-job --wait --handle <h> --url https://git.realliance.net --uuid <uuid> --token-url file:///creds/token --label <label>:host` with the proof pod image.
  - `timeline() -> dict[str, float]`: the step timestamps each test fills.
- Every test is `@pytest.mark.slow` and skips unless `CI_DRILL=runner-proofs`. Each one uses a fresh label `ci-probe-<hex>` that no other runner carries, so even a mistake cannot take another repo's job.
- The probe repo is `forgejo-admin/ci-probe-<hex>` (private; the administrator creates it and the teardown deletes it). Its workflows run only `run:` steps, so the runner downloads no action.

- [ ] **Step 1: Write the drill.**
  - `test_handle_binds_runner_to_one_attempt`: two waiting jobs A and B with the same label; register one runner; `probe_pod` with A's handle. Assert A reaches `success`, and B is still `waiting` 60 s after A ends.
  - `test_runner_row_lifecycle`: during that run, poll `runner_rows` every 2 s; record the row's `status` while waiting, while running and after. Assert the row disappears within 60 s of A's end.
  - `test_spent_credential_rejected`: a second pod with A's uuid and token and B's handle. Assert it exits non-zero within 60 s with an authentication error, and B still waits.
  - `test_labels_subset_rule`: jobs with `runs-on` `[L]`, `[L, docker]`, `[docker]` and `[L, other]`. `labels=L,docker` returns exactly the first three (filtered to the probe repo), and `labels=L` returns exactly `[L]`.
  - `test_needs_and_concurrency_visibility`: job `b` with `needs: a`, and two runs in one `concurrency` group. Record the `status` and `needs` the listing shows for `b` while `a` waits, and for the second run while the first holds the group. Assert the dispatcher's rule: it launches only for `status == "waiting"`, and a job held back never shows as `waiting`. If it does, STOP: Task 11's gate needs another signal.
  - `test_pod_shape_runs`: A's pod used the `tiny` pod shape (read-only root, uid 1000, memory `/work`, `HOME` and `TMPDIR` under `/work`); the job script asserts `id -u` = 1000 and that `/work` is `tmpfs`.
  - `test_repo_listing_scope`: with an ephemeral instance runner registered, does `GET /repos/{owner}/{repo}/actions/runners` list it? Record the answer.
  - Timing, recorded by every test: job first seen in the listing, registration returned, pod `Running`, job `running` (`task_id` set), job done.
  - Teardown deletes the pods, Secrets, runners left behind (`DELETE /admin/actions/runners/{id}`) and the probe repo.
- [ ] **Step 2: Run without the env.** Expected: all skipped.
- [ ] **Step 3: Gate G, then STOP—ask Chris.** The writes:
  - create namespace `ci-probe` (Pod Security `restricted`) with `kubectl create -f $SCRATCH/ci-probe-ns.yaml`;
  - probe Secrets and Pods in it, with no NetworkPolicy, running only the drill's own scripts;
  - ephemeral instance runners with unique labels;
  - at the end, `kubectl delete namespace ci-probe` (an S4 delete, approved in this same STOP);
  - none during the NetworkPolicy plan's measurement windows (N4).
- [ ] **Step 4: Run the drill.** `CI_DRILL=runner-proofs nix develop --command pytest tests/forge/test_forge_runner_proofs.py -v`. Expected: PASS.
- [ ] **Step 5: Clean up and verify.** `kubectl delete namespace ci-probe`, then `Monitor` until it disappears. `runner_rows` shows no `ci-probe-*` runner. The probe repo returns 404.
- [ ] **Step 6: Ledger the rulings** that later tasks consume:
  - the runner row's lifecycle (D18), feeding Task 12's pickup check;
  - the label subset rule with `docker`;
  - how `needs` and `concurrency` show in the listing;
  - the answer to `test_repo_listing_scope`. If instance runners show in a repo listing, hand the filter (`not ephemeral`, or `owner_id` and `repo_id` set) to forge-bot's `tests/forge/test_forge_ci.py` (`all galaxy labels`) and to the realliance.net plan's runner-listing test (`exactly one runner`), through Chris;
  - median and longest time from registration to `running`. If the longest is over 90 s, raise the `tiny` pickup timeout to twice that and ledger the ruling. Task 17 Step 4 sets it in `config.yaml`.
- [ ] **Step 7: Commit** the drill (signed, sso branch `ci-dispatch`).

### Task 4: Azure facts and the Spot probe

**Files:** none in git; results go to the ledger.

**Pre-approved (Chris, 2026-11-04):** exactly one `Standard_D2as_v6` Spot VM for about 5 minutes, then deleted. Anything else is a new STOP: a second attempt, another size or region, a pay-as-you-go fallback, or a failed delete.

- [ ] **Step 1: Read-only facts.** Inside `nix develop`:
  - `az account show --query '{name:name,state:state}'`. Expected: name `Realliance`, `Enabled`.
  - For `westus3` and `westus2`: `az vm list-skus -l <r> --resource-type virtualMachines --query "[?contains(['Standard_D2as_v6','Standard_D4as_v6','Standard_D8as_v6'], name)].{name:name,restrictions:restrictions[].reasonCode}"`, and `az vm list-usage -l <r> --query "[?name.value=='cores'||name.value=='lowPriorityCores'||contains(name.value,'DASv6')].{n:name.value,cur:currentValue,lim:limit}"`.
  - The retail Spot and pay-as-you-go price for `Standard_D2as_v6` in each region, from `https://prices.azure.com/api/retail/prices` (public).
  - Pick the region: `westus3` if it restricts none of the three sizes, else `westus2`. Ledger the facts and the pick.
- [ ] **Step 2: Create the throwaway group and the VM.** Set `R` to the Step 1 pick. Put a fresh, never-used key at `$SCRATCH/spot/probe.pub` (`ssh-keygen -t ed25519 -N '' -f $SCRATCH/spot/probe`). Then:

```bash
az group create -n rg-realliance-ci-spot-probe -l "$R" --tags purpose=spot-probe expires-at="$(date -u -d '+1 hour' +%FT%TZ)" -o none
t0=$(date +%s)
az vm create -g rg-realliance-ci-spot-probe -n spot-probe --size Standard_D2as_v6 --image Ubuntu2404 \
  --priority Spot --eviction-policy Delete --max-price -1 \
  --public-ip-address "" --nsg "" --admin-username probe --ssh-key-values "$SCRATCH/spot/probe.pub" \
  --os-disk-delete-option Delete --nic-delete-option Delete --tags purpose=spot-probe -o none; rc=$?
t1=$(date +%s)
```

  No public IP and no NSG, so nothing can connect inbound.
- [ ] **Step 3: Observe** (only if `rc == 0`). `az vm show -g … -n spot-probe --query '{priority:priority,eviction:evictionPolicy,size:hardwareProfile.vmSize}'`. Then `Monitor` `az vm get-instance-view … --query "instanceView.statuses[].code"` until it shows `PowerState/running` and the agent is `Ready`. Ledger `t1 − t0` and the time to running. If `rc != 0`, ledger the error code and message with ids removed.
- [ ] **Step 4: Delete, at about 5 minutes after `t0`, success or not.** `az group delete -n rg-realliance-ci-spot-probe --yes --no-wait`, then `Monitor` `az group exists -n rg-realliance-ci-spot-probe` until `false`. A delete that fails or takes over 30 minutes is a STOP: something still runs.
- [ ] **Step 5: Ledger the verdict.** Pass: phase 3 may try Spot first, behind an off switch, and treats the pass as something that can stop working. Fail: VMs stay pay-as-you-go. Delete `$SCRATCH/spot/`.

### Task 5: The `galaxy` registration for `realliance/ci-dispatch`

**Files:**
- Edit: `cluster/forgejo-runner/runner-config.secret.yaml` → `runner-config.yaml` (resealed)
- Edit (private): `forge/runner.env` (`RUNNER_CI_DISPATCH_SECRET`)
- Extend: `tests/forge/test_forge_ci_dispatch.py`

**Interfaces:**
- Produces: the `galaxy` runner holds two connections, `galaxy-forge-bot` (unchanged) and `galaxy-ci-dispatch`, both with label `galaxy:host`.

- [ ] **Step 1: Write the failing test** `test_galaxy_serves_forge_bot_and_ci_dispatch_only`. Runners with `galaxy` labels appear exactly on `realliance/forge-bot` (`galaxy-forge-bot`) and `realliance/ci-dispatch` (`galaxy-ci-dispatch`). `realliance/realliance.net`, `realliance/brand`, `realliance/pages` and `realliance/forgejo` show none. Add `test_ci_dispatch_and_forge_bot_write_access_equal`: the collaborators and teams with write access on the two repos are the same set (the premise for sharing one runner; the spec's "Build" section). Run them. Expected: the first FAILS; the second PASSES, and a failure of the second is a STOP.
- [ ] **Step 2: Preconditions (F2, N5).** `git log --oneline -- cluster/forgejo-runner`, and re-read its non-secret files. Runner idle: forge-bot's `actions/tasks` shows nothing `running` or `waiting`. The NetworkPolicy ledger shows no B2 or Task 9 window open. Read pages' ledger too: no acceptance suite is running on `galaxy`. Run the write-access test and ledger its result before Step 3.
- [ ] **Step 3: STOP—ask Chris.** It mints a runner credential and redeploys the `galaxy` runner (Recreate). Ask him to hold forge-bot CI pushes for the window. Rollback trigger: the runner does not reconnect both connections within 10 min (revert, reseal and push).
- [ ] **Step 4: Register.**
  - In sso-secrets: `printf 'RUNNER_CI_DISPATCH_SECRET=%s\n' "$(openssl rand -hex 20)" >> forge/runner.env`, then a signed commit and a push.
  - `(. forge/runner.env; printf %s "$RUNNER_CI_DISPATCH_SECRET") | kubectl -n forgejo exec -i deploy/forgejo -c forgejo -- forgejo forgejo-cli actions register --name galaxy-ci-dispatch --scope realliance/ci-dispatch --labels galaxy --secret-stdin=-`. Expected: one UUID; ledger it, since a UUID is not secret.
- [ ] **Step 5: Add the connection without reading the file.** A scratch `python -I` script under `umask 077`:
  - loads the decrypted `runner-config.secret.yaml`; if it holds no `server.connections` map, it stops, and so does the task (STOP: the format differs from forge-bot's Task 7);
  - derives the new `uuid` (the UUID of the bytes `secret[:16]`) and `token` (the 40-hex secret itself) inside the script, as forge-bot's `task-7-report.md` section 2 describes, and asserts the `uuid` equals the one `register` printed. It never runs `create-runner-file`, which takes the secret only as an argv element;
  - adds `galaxy-ci-dispatch: {url: https://git.realliance.net/, uuid, token, labels: [galaxy:host]}` next to the existing connection, changing nothing else;
  - writes it back and prints only the connection names, their labels and a byte count.

  Expected output: `galaxy-forge-bot [galaxy:host]`, `galaxy-ci-dispatch [galaxy:host]`. Then `KUBECONFIG=/tmp/galaxy-kubeconfig nix develop --command ./sign.sh`, and check `git diff --no-textconv --stat`.
- [ ] **Step 6: Commit and push** (`forgejo-runner: second repo-scoped connection for realliance/ci-dispatch (runner classes Task 5)`). `Monitor` until the pod is Ready. Expected in its log: `declared successfully` for both connections.
- [ ] **Step 7: Verify.** Run the test. Expected: PASS. Then run forge-bot's `test_runner_registered_with_galaxy_label_only`. Expected: PASS. Ledger N5 into the NetworkPolicy ledger, and R1 into the realliance.net ledger.
- [ ] **Step 8: Land the sso tests.** `gh pr create --fill` for branch `ci-dispatch`. **Chris:** merges.

## Phase 1: `ci-dispatch` and `tiny`

All Go work is in `~/Repos/ci-dispatch`, on branch `v1`, with a PR that Chris merges in Task 14. Every task ends with `$GO vet ./... && $GO test -race -p=4 ./...` green; then commit (signed) and push. The PR's CI run on `galaxy` stays green. Each branch push starts such a run, so N3's no-push rule covers Tasks 6 to 14.

### Task 6: Scaffold: module, model, config, CI, the dependency guard

**Files:**
- Create: `go.mod`, `LICENSE-MIT`, `LICENSE-APACHE`, `README.md`, `.ko.yaml`, `.forgejo/workflows/ci.yaml`, `cmd/ci-dispatch/main.go`, `internal/model/model.go` and its test, `internal/config/config.go` and its test, `internal/secret/secret.go` and its test (copied from forge-bot 9be4857), `deps_test.go`

**Interfaces (produces):**

```go
// internal/model
type Class string                          // Tiny="tiny", Small="small", Medium="medium", Large="large"
type Mode int                              // ModeOff=0, ModeDryRun=1, ModeOn=2
func ParseMode(s string) (Mode, error)     // "off" | "dry-run" | "on"
type Job struct {
    ID, Attempt, RepoID, OwnerID, TaskID int64
    Handle, Name, Status string
    RunsOn, Needs []string
}
type Key struct{ JobID, Attempt int64 }
func (k Key) String() string                // "<job>-<attempt>"
func Route(runsOn []string) (c Class, docker, ok bool) // exactly one size label; docker only with a paid size
type Action string                          // Launch, Wait, Hold
type Reason string                          // holds: label, denied, no-budget, owner-limit, global-cap, launch-failing
                                            // waits: class-off, owner-busy, class-full, not-ready
type Decision struct{ Job Job; Key Key; Class Class; Action Action; Reason Reason }
var ValidHandle = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

// internal/config (the luma-homeops ConfigMap)
type Config struct {
    Forgejo   struct{ URL, PublicURL, TokenFile string }
    Inventory struct{ Repo, Branch, File, Keyring, Fingerprints string }
    Classes   map[model.Class]model.Mode
    Kube      struct{ Namespace, TokenFile, CAFile, Host string }
    Tiny      struct{ Image, RunnerConfigMap string; RunnerTimeout, HardStop, Pickup time.Duration }
    Interval, Sweep time.Duration
    Ledger, Snapshots string
    Listen, UsageListen, UsageTokenFile string
}
func Load(path string) (Config, error)
```

- [ ] **Step 1: Resolve module pins.** For each of the three modules, read `https://proxy.golang.org/<m>/@v/list` and the `.info` of the newest candidates. Pick the newest release at least 2 weeks old. Ledger the versions and dates.
- [ ] **Step 2: Write the failing tests.**
  - `TestRoute` (table): `[tiny]` → `tiny`; `[small, docker]` → `small` with Docker; `[tiny, docker]`, `[small, medium]`, `[docker]` and `[]` → rejected.
  - `TestParseMode`, `TestKeyString`, `TestValidHandle` (rejects `a b`, `../x`, `$(x)` and 129 characters).
  - `TestLoadDefaults`: a minimal file gives `Interval` 5s, `Sweep` 5m, `Tiny.Pickup` 3m, `Tiny.HardStop` 20m, `Tiny.RunnerTimeout` 15m, `Listen` `:8080`, `UsageListen` `:8081` and every class `ModeOff`.
  - `TestLoadRejects` (table): an unknown key (strict decoding); an unknown class; a mode outside the three; any paid class not `off` ("no backend in v1"); `Tiny.HardStop` ≤ `Tiny.RunnerTimeout`; an image without `@sha256:`.
  - `TestDirectRequires`: parse `go.mod`; the direct requirements are exactly the three modules.
  - `TestSecretRedacts`: the copied forge-bot tests, unchanged.
- [ ] **Step 3: Run them.** Expected: FAIL (packages do not compile).
- [ ] **Step 4: Write the code.**
  - `go.mod`: `module git.realliance.net/realliance-ops/ci-dispatch`, `go 1.26.0`, `toolchain go1.26.10`.
  - `.ko.yaml`: the base from Pins, `linux/amd64`, `-trimpath`, `-s -w`.
  - `.forgejo/workflows/ci.yaml`: forge-bot's file with `./cmd/ci-dispatch` and `KO_DOCKER_REPO: git.realliance.net/realliance-ops/ci-dispatch`, the same `ko login … -u ops-ci`, and the same header comment (never approve fork-PR runs).
  - `main.go`: `--config` (default `/etc/ci-dispatch/config/config.yaml`), `Load` or exit 1. The loop arrives in Task 13.
  - README: purpose, the classes table, safety rules (link the spec in luma-homeops), modes, the Forgejo client's two writes, and how Chris pins releases by hand in luma-homeops after review.
- [ ] **Step 5: Run the tests.** Expected: PASS.
- [ ] **Step 6: Commit, push branch `v1`, open the PR.** Expected: its run is `success` on `galaxy-ci-dispatch`. If no runner picks it up within 5 min, read the runner pod's log first. If the 3Gi `galaxy` pod runs out of memory compiling `modernc.org/sqlite` under `-race`, run `-race` on the other packages and a plain `go test` on `ledger`; never raise the runner limit without a STOP.

### Task 7: Signed limits: `internal/signed` and `internal/limits`

**Files:**
- Create: `internal/signed/verify.go`, `parse.go`, their tests and `testdata/` (copied from forge-bot 9be4857 `internal/inventory/verify*.go`, `parse.go`, `parse_test.go` and `testdata/gen-git-fixture.sh` with its outputs); `internal/limits/limits.go`, `source.go` and their tests

**Interfaces (produces):**

```go
// internal/signed: forge-bot's API, renamed only by package
func ParseFingerprints(r io.Reader) ([]string, error)
func LoadKeyring(armored io.Reader, fingerprints []string) (*Keyring, error)
func ReconstructCommit(payload, signature string) []byte
func CommitSHA1(raw []byte) string
func (k *Keyring) VerifyCommit(sha, payload, signature string, now time.Time) (fingerprint string, err error)

// internal/limits
type Micro int64                                           // micro-dollars
const CeilingPeriod Micro = 100_000_000                    // $100 a period
const CeilingHour Micro = 600_000                          // $0.60 an hour
const TinyQuotaPods = 3
type Owner struct {
    PaidPerPeriod Micro
    PaidConcurrent, TinyConcurrent, TinyMinutesPerDay int
    Source string                                          // "default" | "override"
}
type Limits struct {
    Defaults Owner
    Global   struct{ PaidPerPeriod, PaidPerHour Micro; TinyConcurrent int }
    Classes  map[model.Class]bool
    Owners   map[int64]Owner                               // by owner id
    Deny     map[int64]bool
}
var BuiltIn Limits                                          // tiny defaults; paid zero; classes {tiny: true}
type Raw struct{ /* the spec's ci.yaml, strict */ }
func Parse(b []byte) (Raw, error)
func Resolve(ctx context.Context, r Raw, id func(context.Context, string) (int64, error)) (Limits, error)
func (l Limits) For(owner int64) (o Owner, denied bool)
var ErrAboveCeiling, ErrUnknownOwner, ErrUnknownClass, ErrAboveQuota error

type Store interface {
    SaveConfig(ctx context.Context, sha string, body []byte, at time.Time) error
    LastConfig(ctx context.Context) (sha string, body []byte, at time.Time, err error)
}
type Forge interface {
    BranchHead(ctx context.Context, owner, repo, branch string) (string, error)
    CommitSignature(ctx context.Context, owner, repo, sha string) (payload, signature string, err error)
    FileAt(ctx context.Context, owner, repo, path, sha string) ([]byte, error)
    OwnerID(ctx context.Context, name string) (int64, error)
}
type Source struct{ /* forge, keyring, store, repo, branch, file */ }
func (s *Source) Reload(ctx context.Context, now time.Time) error   // each sweep and at start
func (s *Source) Current() (l Limits, verified bool, everVerified bool)
```

- [ ] **Step 1: Copy** `verify.go`, `parse.go` (`ParseFingerprints`), their tests and the fixture verbatim, changing only the package clause and a header naming forge-bot 9be4857. Run its tests. Expected: PASS at once. This step has no red phase, because the copy carries its own tests.
- [ ] **Step 2: Write the failing `limits` tests.**
  - `TestParseSpecExample`: the spec's `ci.yaml` parses; `$10` is `10_000_000`.
  - `TestParseRejects` (table): an unknown key, a duplicate key, a negative value, a class outside the four, a non-numeric dollar value.
  - `TestCeilings`: `global.paid_usd_per_period: 101` and `paid_usd_per_hour: 0.61` give `ErrAboveCeiling`; `100` and `0.60` pass; `global.tiny_concurrent: 4` gives `ErrAboveQuota`.
  - `TestResolveUnknownNameFailsLoad`: one unresolvable owner or denied name fails the whole load.
  - `TestForAppliesOverrides`: an override sets only the fields it names, and `Source` is `override`.
  - `TestSourceKeepsLastVerified` (fakes): a verified head, then an unsigned head, gives the first config with `verified == false` and `everVerified == true`.
  - `TestSourceBeforeAnyVerified`: an unsigned head and an empty store give `BuiltIn` (`tiny` only) and `everVerified == false`.
  - `TestSourceRestoresFromStore`: a restart with the Forgejo fake failing serves the stored config, still re-verified through `VerifyCommit` against the stored commit object.
  - `TestSourceReadsFileAtVerifiedSHA`: `FileAt` gets the verified commit, never a branch name.
- [ ] **Step 3: Run them.** Expected: FAIL.
- [ ] **Step 4: Write the code** with strict YAML (`KnownFields(true)`) and dollars as decimal strings parsed into `Micro` without floats.
- [ ] **Step 5: Run the tests.** Expected: PASS. Commit: `limits: Owner-signed ci.yaml, ceilings in code, last-verified fallback`.

### Task 8: `internal/forgejo`

**Files:**
- Create: `internal/forgejo/client.go`, `guard.go`, `client_test.go`, `writes_test.go`

**Interfaces (produces):**

```go
type Client struct{ /* base URL, token secret.Value, guarded *http.Client */ }
func New(baseURL string, token secret.Value, hc *http.Client) *Client
func (c *Client) Jobs(ctx context.Context, labels []string) ([]model.Job, error)
func (c *Client) Runners(ctx context.Context) ([]Runner, error)                 // paged, limit=50
func (c *Client) RegisterRunner(ctx context.Context, name, description string) (Registration, error) // ephemeral: true, always
func (c *Client) DeleteRunner(ctx context.Context, id int64) error             // 404 is success
func (c *Client) BranchHead(ctx context.Context, owner, repo, branch string) (string, error)
func (c *Client) CommitSignature(ctx context.Context, owner, repo, sha string) (payload, signature string, err error)
func (c *Client) FileAt(ctx context.Context, owner, repo, path, sha string) ([]byte, error)
func (c *Client) OwnerID(ctx context.Context, name string) (int64, error)      // GET /users/{name}; orgs answer too
type Runner struct{ ID, OwnerID, RepoID int64; UUID, Name, Status string; Ephemeral bool; Labels []string }
type Registration struct{ ID int64; UUID string; Token secret.Value }
```

- [ ] **Step 1: Write the failing tests** (an `httptest` fake Forgejo that records every request).
  - `TestJobsQuery`: `labels=tiny,small,medium,large,docker` exactly; every field decodes.
  - `TestRunnersPaged`: 120 runners over three pages; a short page ends the listing.
  - `TestRegisterRunnerEphemeral`: the body is exactly `{"name": …, "description": …, "ephemeral": true}`; the token comes back as `secret.Value`.
  - `TestDeleteRunner404IsSuccess`.
  - `TestBranchHeadCommitSignatureFileAt`: the paths forge-bot uses, with `?ref=<sha>`.
  - `TestGuardAllowList` (runtime): every request the client makes is a GET on the allowed shapes, `POST /admin/actions/runners` or `DELETE /admin/actions/runners/<digits>`. The guard refuses, before sending anything: `GET /admin/actions/runners/registration-token` (it mints a token), another host, `%2F` and `..`.
  - `TestNoOtherWritesInSource` (static, `go/parser` over non-test files): `http.MethodPost` and `http.MethodDelete` appear once each, inside `RegisterRunner` and `DeleteRunner`; `PUT`, `PATCH`, `http.Post` and `http.PostForm` never appear.
  - `TestErrorsNeverContainToken`: a 401 or 500 error's text lacks the token, and so does a registration response.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Write the code,** shaped like forge-bot's client (`guardTransport`, `scrub`, `Authorization: token …`, a 30 s timeout).
- [ ] **Step 4: Run the tests.** Expected: PASS. Commit: `forgejo: jobs, runners, inventory reads; two writes only`.

### Task 9: `internal/kube` and `internal/tiny`

**Files:**
- Create: `internal/kube/client.go`, `guard.go`, `client_test.go`; `internal/tiny/manifest.go`, `manifest_test.go`

**Interfaces (produces):**

```go
// internal/kube: in-cluster, one namespace, fixed at construction
func New(host, caFile, tokenFile, namespace string) (*Client, error) // re-reads the projected token every minute
func (c *Client) Jobs(ctx context.Context, selector string) ([]JobStatus, error)
func (c *Client) Pods(ctx context.Context, selector string) ([]PodStatus, error)
func (c *Client) CreateJob(ctx context.Context, manifest []byte) (uid string, err error)
func (c *Client) ResumeJob(ctx context.Context, name string) error   // merge patch {"spec":{"suspend":false}} only
func (c *Client) DeleteJob(ctx context.Context, name string) error   // propagationPolicy=Background; 404 is success
func (c *Client) CreateSecret(ctx context.Context, manifest []byte) error
func (c *Client) DeleteSecret(ctx context.Context, name string) error
type JobStatus struct{ Name, UID, Key string; Created, ExpiresAt time.Time; Active, Succeeded, Failed int32; Suspended bool }
type PodStatus struct{ Name, JobName, Phase, Node string; Started time.Time }

// internal/tiny: pure manifest builders
type Spec struct {
    Key model.Key; Try int; OwnerID int64
    Handle, UUID, PublicURL, Image, RunnerConfigMap string
    ExpiresAt time.Time
    HardStop, RunnerTimeout time.Duration                         // from Config.Tiny; HardStop is activeDeadlineSeconds
}
func Name(k model.Key, try int) string                              // "ci-tiny-<job>-<attempt>-<try>"
func JobManifest(s Spec) ([]byte, error)                            // refuses a handle that fails model.ValidHandle
func SecretManifest(name, jobUID string, token secret.Value) []byte // ownerReferences: that Job, no blockOwnerDeletion
```

- [ ] **Step 1: Write the failing tests.**
  - `TestJobManifestShape`: decode it and assert every pod item that Global Constraints fixes. Also: `suspend: true`; labels `app.kubernetes.io/name: ci-tiny`, `app.kubernetes.io/managed-by: ci-dispatch` and `ci-dispatch/key`; annotations `ci-dispatch/owner-id` and `ci-dispatch/expires-at`; the preferred anti-`top` node affinity; the spread across nodes with `ScheduleAnyway`; `activeDeadlineSeconds` equals `Spec.HardStop`; env `HOME=/work/home` and `TMPDIR=/work/tmp`; `/work` is a memory `emptyDir` with `sizeLimit: 1Gi`.
  - `TestRunnerArgv`: `command == ["sh", "-c", "mkdir -p /work/home /work/tmp /work/act && exec forgejo-runner \"$@\"", "sh", "one-job", "--config", "/etc/ci-tiny/config.yml", "--wait", "--handle", h, "--url", url, "--uuid", u, "--token-url", "file:///creds/token", "--label", "tiny:host"]`. The handle is one argv element, never inside the script.
  - `TestGateIsSpecVerbatim`: the `netpol-gate` init container's script equals the NetworkPolicy spec §6.3 body byte for byte (read from a `testdata/` copy). Its image is the `tiny` image; requests `10m`/`32Mi`, limit `128Mi`; the same securityContext as the runner.
  - `TestJobManifestRefusesBadHandle`.
  - `TestSecretOwnedByJob`: one `ownerReference` (`batch/v1`, `Job`, name, uid), no `blockOwnerDeletion`, key `token`, `type: Opaque`.
  - `TestKubeGuard` (runtime, fake API server): only the seven request shapes in Interfaces, in the configured namespace only. The guard refuses each of these unsent: a path in another namespace, a PATCH body other than `{"spec":{"suspend":false}}`, and a GET on a Secret.
  - `TestKubeTokenReread`: with an injected clock, the client picks up a rotated token file after a minute; the test never sleeps.
  - `TestNoOtherWritesInSource` (static, as in Task 8).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Write the code** with `encoding/json` structs for the manifests, no templates.
- [ ] **Step 4: Run the tests.** Expected: PASS. Commit: `kube, tiny: one namespace, seven requests; the tiny Job and its Secret`.

### Task 10: `internal/ledger`

**Files:**
- Create: `internal/ledger/ledger.go`, `schema.sql` (embedded), `ledger_test.go`

**Interfaces (produces):**

```go
type State string // reserved, registered, created, running, ended, undone, reaped
type Launch struct {
    ID int64; Key model.Key; Try int; OwnerID, RepoID int64
    Class model.Class; Backend string                 // "galaxy"
    RunnerID int64; RunnerName, Compute string
    State State; Outcome string
    ReservedMinutes, ChargedMinutes int
    ReservedMicro, ChargedMicro int64                 // 0 for tiny; phase 2 uses them
    Created, Registered, ComputeAt, Started, Ended, Expires time.Time
}
type Hold struct{ Key model.Key; OwnerID, RepoID int64; Class model.Class; Reason model.Reason; Since time.Time }
func Open(path string) (*Ledger, error)               // flock on <dir>/.lock, WAL, migrations; ErrLocked if held
func (l *Ledger) Reserve(ctx context.Context, x Launch) (Launch, error)  // ErrActive if the key has an active launch
func (l *Ledger) SetRegistered(ctx context.Context, id, runnerID int64, runnerName string, at time.Time) error
func (l *Ledger) SetCreated(ctx context.Context, id int64, compute string, at time.Time) error
func (l *Ledger) SetRunning(ctx context.Context, id int64, at time.Time) error
func (l *Ledger) End(ctx context.Context, id int64, at time.Time, outcome string, chargedMinutes int) error
func (l *Ledger) Undo(ctx context.Context, id int64, outcome string) error
func (l *Ledger) Active(ctx context.Context) ([]Launch, error)
func (l *Ledger) Tries(ctx context.Context, k model.Key) (int, error)
func (l *Ledger) TinyMinutes(ctx context.Context, owner int64, day time.Time) (int, error)
func (l *Ledger) SetHold(ctx context.Context, h Hold) error            // upsert by key
func (l *Ledger) ClearHold(ctx context.Context, k model.Key, at time.Time) error
func (l *Ledger) Holds(ctx context.Context, owner int64) ([]Hold, error)
func (l *Ledger) SaveConfig(ctx context.Context, sha string, body []byte, at time.Time) error
func (l *Ledger) LastConfig(ctx context.Context) (sha string, body []byte, at time.Time, err error)
func (l *Ledger) Launches(ctx context.Context, owner int64, from, to time.Time, limit int) ([]Launch, error)
func (l *Ledger) Snapshot(ctx context.Context, dir string, now time.Time) (string, error) // VACUUM INTO, keep 7
```

The schema has `launches`, `holds`, `config` and `meta(schema_version)`. A partial unique index `ON launches(job_id, attempt) WHERE state IN ('reserved','registered','created','running')` makes a second active launch for one key impossible. The runner token is never a column.

- [ ] **Step 1: Write the failing tests** (a temp dir per test).
  - `TestReserveTwiceIsErrActive`; `TestReserveAfterUndoOrEnd` (try 2 works); `TestReserveNewAttempt` (Review Focus 2).
  - `TestStateTransitions` (table): only forward moves; `End` on `undone` errors.
  - `TestTinyMinutesChargedToLaunchDay` (Review Focus 5): an open launch counts its 20 reserved minutes, a settled one its charged minutes, by the UTC day of `Created`.
  - `TestHoldsUpsertAndClear`.
  - `TestSecondOpenIsErrLocked`: a second `Open` on the same directory gives `ErrLocked` (D5).
  - `TestSnapshotConsistentAndPruned`: the copy opens and holds every row; 9 days of snapshots leave 7.
  - `TestNoTokenColumn`: the schema has no column named like `token` or `secret`.
  - `TestMigrationsIdempotent`.
  - `TestSnapshotWithUnwritableTmpdir`: with `TMPDIR` read-only, a snapshot and a sorted `Launches` query still work.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Write the code** with `database/sql` and `modernc.org/sqlite`: `journal_mode=WAL`, `temp_store=MEMORY`, `busy_timeout=5000`, `foreign_keys=on`, and `BEGIN IMMEDIATE` for every write.
- [ ] **Step 4: Run the tests.** Expected: PASS. Commit: `ledger: SQLite, one active launch per key, flock, nightly snapshot`.

### Task 11: `internal/gate` and fair order

**Files:**
- Create: `internal/gate/gate.go`, `gate_test.go`

**Interfaces (produces):**

```go
type Inputs struct {
    Modes        map[model.Class]model.Mode
    Limits       limits.Limits
    Ready        bool                    // ledger writable; config verified once or BuiltIn allowed for tiny
    OwnerRunning map[int64]int           // active tiny launches per owner
    ClassRunning map[model.Class]int
    TinyUsed     map[int64]int           // minutes today per owner, from the ledger
    Tries        map[model.Key]int
    Launching    map[model.Key]bool      // keys with an active launch: never decided again
    Reserve      int                     // 20
}
func Plan(jobs []model.Job, in Inputs) []model.Decision
```

`Plan` keeps only `status == "waiting"` jobs (Task 3's ruling) that are not `Launching`. It orders them oldest id first, round robin across owners. It decides each in the spec's gate order and counts every `Launch` against the capacity and minutes for the jobs after it.

- [ ] **Step 1: Write the failing tests** (tables).
  - `TestGateOrder`: one row per gate in the spec's table, each failing alone, giving the exact action and reason.
  - `TestTinyBuiltInBeforeConfig`: no verified config, `tiny` on → `Launch` with `BuiltIn`; a paid class set `on` in `Inputs` → `Wait not-ready`; a paid class `off` → `Wait class-off`.
  - `TestDeniedOwnerHoldsTiny`: the deny list covers `tiny` too.
  - `TestFairOrder`: owner A has jobs 1, 2, 3 and owner B has job 4; global capacity 2 → launch 1 and 4.
  - `TestPlanCountsOwnLaunches`: two jobs of one owner with `tiny_concurrent` 1 → one `Launch`, one `Wait owner-busy`.
  - `TestOnlyWaitingJobs`: `running` and blocked jobs are never decided.
  - `TestLaunchFailingAfterThreeTries`.
  - `TestDryRunSameDecisions`: `dry-run` decides exactly what `on` would.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Write the code.** `Plan` is pure: no clock, no I/O.
- [ ] **Step 4: Run the tests.** Expected: PASS. Commit: `gate: the spec's gates in order, fair across owners`.

### Task 12: `internal/loop`: reconcile, launch, reap, settle

**Files:**
- Create: `internal/loop/loop.go`, `reap.go`, `decisions.go`, `fakes_test.go`, `loop_test.go`, `reap_test.go`, `decisions_test.go`; `internal/metrics/metrics.go` and its test (moved here from Task 13, because `loop.New` needs both)

**Interfaces (produces):**

```go
type Forge interface {
    Jobs(context.Context, []string) ([]model.Job, error)
    Runners(context.Context) ([]forgejo.Runner, error)
    RegisterRunner(context.Context, string, string) (forgejo.Registration, error)
    DeleteRunner(context.Context, int64) error
}
type Kube interface { /* the internal/kube method set */ }
type Limits interface{ Current() (limits.Limits, bool, bool); Reload(context.Context, time.Time) error }
type Loop struct{ /* cfg, forge, kube, ledger, limits, metrics, decisions log */ }
func New(cfg config.Config, f Forge, k Kube, l *ledger.Ledger, lim Limits, m *metrics.Metrics, d *Decisions) *Loop
func (lp *Loop) Start(ctx context.Context, now time.Time) error   // reconcile before any launch
func (lp *Loop) Tick(ctx context.Context, now time.Time) Result   // every Interval
func (lp *Loop) Sweep(ctx context.Context, now time.Time) Result  // every Sweep: runners, orphans, config, snapshot at 03:00 UTC
func (lp *Loop) Run(ctx context.Context)                          // never overlapping
func (lp *Loop) Healthy(now time.Time) bool                       // a tick finished within 3 × Interval, whatever Forgejo answered
```

`Tick`:
1. Read the jobs and the `ci-tiny` Jobs and Pods. A failed Forgejo jobs read skips the gate and the launches only; a failed Kubernetes read skips everything. Reaping an expired Job always runs. Either failure leaves the success timestamp unchanged, though the tick still counts as finished for `Healthy`.
2. Reconcile each active launch: pod `Running` and job `running` → `running`; Job gone or finished → `ended`, then settle.
3. `gate.Plan`.
4. For each `Launch`, in mode `on`: `Reserve` → `RegisterRunner` → `SetRegistered` → `CreateJob` → `CreateSecret` → `ResumeJob` → `SetCreated`. A failure at any step undoes the earlier steps in reverse and calls `Undo`. In `dry-run`: log and count only.
5. Write holds and clear them when a job leaves `waiting`.
6. Reap (below), then settle: `charged = max(1, ceil((Ended − ComputeAt) / 1 min))`.

Reap rules (the spec's table, `tiny` values):
- a launch whose job is still `waiting` after the pickup timeout: delete the compute and the runner, then retry; after 3 tries, hold `launch-failing`;
- a launch whose job left the listing before pickup: delete both;
- a Job past `ci-dispatch/expires-at`: delete it;
- a `ci-tiny` Job, or a runner that is `ci-*`, ephemeral and instance-scoped, with no active ledger row and older than 10 min: delete it (a runner's age is first-seen time, which it lacks itself);
- anything else, never.

Also built here (moved from Task 13, F3):
- Metrics, with label values from fixed sets only and every series present from start:
  - `ci_dispatch_loop_last_success_timestamp` (it updates only when every read succeeded; it feeds `CIDispatchStale`, not `/healthz`);
  - `ci_dispatch_jobs{class,state}`, `ci_dispatch_holds{class,reason}`, `ci_dispatch_class_mode{class}`;
  - `ci_dispatch_launches_total{class,backend,outcome}` (`outcome`: `launched`, `dry-run`, `error`, `undone`);
  - `ci_dispatch_start_seconds{class,backend}` (histogram; buckets 5, 10, 20, 30, 60, 120, 300; from first seen to `running`);
  - `ci_dispatch_reaped_total{reason}` (`pickup`, `gone-before-pickup`, `expired`, `orphan-job`, `orphan-runner`), `ci_dispatch_orphans{kind}` (`job`, `runner`, `expired`);
  - `ci_dispatch_errors_total{api}` (`forgejo`, `kube`, `ledger`, `inventory`);
  - `ci_dispatch_config_verified`, `ci_dispatch_config_ever_verified`.
  - Phase 2 adds the money, price, sweeper and drift metrics.
- `Decisions.Log(d model.Decision, now time.Time)`: one slog JSON line per key whenever its action or reason changes, with fields `key`, `owner_id`, `repo_id`, `class`, `action`, `reason`, `mode`. Settled launches log `reserved_minutes` and `charged_minutes`.

- [ ] **Step 1: Write the failing tests** (in-memory fakes that record every call, and a fake clock).
  - `TestTickLaunchesInOrder`: exactly reserve, register, create the Job, create the Secret, resume, mark created.
  - `TestUndoAtEveryStep` (table over each step failing): the loop undoes every earlier side effect and frees the reservation, and the next tick can launch the key again.
  - `TestStartupReconcileAfterCrashMidLaunch` (Review Focus 3; a table over each state a crash leaves): after `Start`, no duplicate runner or Job exists, and `Start` deletes orphans before the first `Tick` launches anything.
  - `TestReapPickupTimeoutRetries`; `TestLaunchFailingAfterThree`.
  - `TestReapJobGoneBeforePickup` (Review Focus 1): charged ≤ 1 minute.
  - `TestReapExpired`; `TestReapOrphansAfterTenMinutes`.
  - `TestReaperIgnoresForeignRunners` (Review Focus 6): a repo-scoped `ci-x`, an owner-scoped `ci-y` and a persistent `ci-z` survive.
  - `TestRerunLaunches` (Review Focus 2).
  - `TestDryRunWritesNothing`: no launch row, no Forgejo or Kubernetes write (it may write holds); the dry-run counter rises once per decision change, not per tick.
  - `TestReadFailureWritesNothing`: a failed jobs read makes no launch or ledger write and leaves the success timestamp unchanged.
  - `TestForgejoDownReaperStillWorks`: with every Forgejo call failing, an expired Job is still deleted.
  - `TestRunNeverOverlaps`.
  - `TestHealthyWhileForgejoDown`: with every Forgejo call failing, `Healthy` stays true while ticks finish, and turns false only when a tick wedges for over 3 × Interval.
  - `TestMetricsFixedLabels`: every label value belongs to its set, and no label name contains `owner`.
  - `TestMetricsTextFormat`: the output parses under the Prometheus text rules (a small checker in the test).
  - `TestDecisionLogOncePerChange` (Review Focus 4): 1,000 ticks over one held job give one line; a change of reason gives a second.
  - `TestLogsCarryIdsOnly`: no line carries a repo or owner name, even when a fake job name contains one.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Write the code.**
- [ ] **Step 4: Run the tests,** then `$GO test -race -count=20 ./internal/loop/`. Expected: PASS. Commit: `loop: reconcile, launch with undo, reap, settle; restart-safe`.

### Task 13: Metrics, the usage API, decision logs, `main`

**Files:**
- Create: `internal/usage/usage.go` and its test (`internal/metrics` and `internal/loop/decisions.go` belong to Task 12)
- Edit: `cmd/ci-dispatch/main.go`

**Interfaces (produces):**
- Usage API on `UsageListen`, with `Authorization: Bearer <usage token>` compared through `subtle.ConstantTimeCompare`:
  - `GET /v1/owners/{owner_id}/usage?period=current|previous|YYYY-MM`: the spec's JSON. In phase 1, `paid` carries the owner's limit and zeros, and `global.paid` is `closed`.
  - `GET /v1/owners/{owner_id}/jobs?period=…&limit=…` (`limit` ≤ 200).
  - `func Period(now time.Time, which string) (start, end time.Time, err error)`: the UTC calendar month, first day to last day, for `current`, `previous` or `YYYY-MM`.
- `main`: load the config; read both secrets; `ledger.Open` (exit 1 on `ErrLocked`); build the clients (30 s timeouts); `Start`; serve `:8080` (`/metrics`, and `/healthz` from `Healthy`) and `:8081`; `Run` until SIGTERM. SIGTERM finishes the step in flight, at most 20 s.

- [ ] **Step 1: Write the failing tests.**
  - `TestUsageAuth`: no token → 401; a wrong token → 401; the right token → 200; `/metrics` and `/healthz` live on the other port only.
  - `TestUsageShape`: the golden spec JSON, with money as decimal strings.
  - `TestPeriod` (table): a month boundary; December to January; `previous`; `YYYY-MM`; a malformed value.
  - `TestJobsLimitCapped`.
  - `TestMainRefusesSecondInstance`, `TestMainRefusesMissingSecretFile` (the error names the path, never the content).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Write the code.**
- [ ] **Step 4: Run the tests.** Expected: PASS. Commit: `ci-dispatch: metrics, usage API, decision log, main`.

### Task 14: Review, release, pin

**Files:** none new.

- [ ] **Step 1: Whole-branch review** (superpowers:requesting-code-review) against the spec's sections on the administrator token and on security, and this plan's Review focus. Fix in one round; ledger the rulings.
- [ ] **Step 2: Chris merges** PR `v1` fast-forward and signs tag `v0.1.0`. The `main` run builds `git.realliance.net/realliance-ops/ci-dispatch:sha-<7>`. Expected: run `success`, and the log ends in `…@sha256:<64>`. Fallback, per the spec: an Owner runs the same `ko build` locally from that commit with `DOCKER_CONFIG` in a temp dir. Ledger which path built it.
- [ ] **Step 3: Link the package.** `POST /api/v1/packages/realliance-ops/container/ci-dispatch/-/link/ci-dispatch` returns 201. Check that an anonymous manifest GET returns 200, and hash the body, since B2 direct serving drops `Docker-Content-Digest`.
- [ ] **Step 4: STOP—ask Chris to review the release** (`git verify-tag v0.1.0`, `git log -p` of the whole branch). Ledger his approval and the digest.

### Task 15: `ci.yaml` in the inventory

**Files:**
- Create (inventory, branch `ci-limits`): `ci.yaml`
- Edit: `README.md` ("What lives where": `ci.yaml` → ci-dispatch, Owner-signed heads only)

- [ ] **Step 1: Write it** from the spec's example, with:
  - `defaults` as the spec;
  - `global` as the spec, except `tiny_concurrent: 1` (F14: raise it to 2 after a clean day);
  - `classes: {tiny: on, small: off, medium: off, large: off}`;
  - `owners: {realliance: {tiny_concurrent: 2}, forgejo-admin: {tiny_minutes_per_day: 20}}` (D11);
  - `deny: []`;
  - a header comment: names resolve to ids at each load, an unknown name fails the load, the ceilings live in code, and only an Owner-signed head counts.
- [ ] **Step 2: Check locally** with a scratch Go test in `~/Repos/ci-dispatch` that runs `limits.Parse` on the file. The same test then runs `Resolve` read-only against the live forge, with `CI_DISPATCH_FORGEJO_TOKEN` from the environment, so a mistyped owner fails now. Expected: PASS.
- [ ] **Step 3: Commit signed, push, open the PR.** **Chris:** merges fast-forward. Nothing to apply: tofu ignores the file.

### Task 16: The acceptance suite, written first (`realliance/sso`)

**Files:**
- Create: `tests/forge/test_forge_ci_tiny.py` (branch `ci-tiny`)
- Extend: `tests/ci_helpers.py` (`usage(owner_id) -> dict` and `usage_raw(owner_id, token)` through `kubectl -n ci-dispatch port-forward svc/ci-dispatch 0:8081`; `tiny_pods()`; `wait_status(repo, sha, status)`)

**Interfaces:**
- Consumes: `forge_user`, `push_probe_workflow`, `push_workflow`, `runs_for`, `delete_branch`, `wait_for`, `kube`, `galaxy_only`, `LUMA`.

- [ ] **Step 1: Write the tests.**
  - `test_member_repo_runs_tiny`: a throwaway member creates a public repo (`sudo`), and a `runs-on: tiny` probe reaches `success` within 5 min. Its owner's usage shows one launch.
  - `test_tiny_job_hardened`: in the job, `id -u` is 1000, writing to `/` fails, `/work` is `tmpfs` with at most 1 GiB, `HOME` is `/work/home`, there is no service account token and no Docker socket.
  - `test_tiny_job_egress_confined`: the job runs `scripts/netpol-connect.js` against the `all` rows of `cluster/netpol-canary/matrix.txt` (without the `$HOST_IP` rows), plus `dropped github.com:22`, `dropped 1.1.1.1:853` and `open 1.1.1.1:80`, then `git ls-remote` on the forge and on GitHub. Every row must match. The test first checks from the laptop that `github.com:22` and `1.1.1.1:853` answer, so a timeout in the job means the policy dropped the packet, not that the target is dead.
  - `test_nothing_outlives_the_job` (Review Focus 1): 60 s after the run ends, `ci-tiny` has no Job, Pod or Secret for its key, and no runner `ci-tiny-<job>-*` exists. Then a probe that someone cancels before pickup leaves nothing behind either.
  - `test_owner_runs_one_at_a_time`: a member pushes two probes that each sleep 30 s; the second starts only after the first ends.
  - `test_owner_minutes_cap_holds` (D11): a `forgejo-admin` repo runs one job, and a second holds as `owner-limit` in its usage. Its teardown says the account stays capped until 00:00 UTC. The test reads the usage first: if `forgejo-admin` has already used its 20 minutes today, it asserts the hold only and skips the launch half.
  - `test_label_combo_holds`: `runs-on: [tiny, docker]` holds as `label`; `runs-on: small` waits with no hold; neither ever runs.
  - `test_usage_api_auth_and_ids`: 401 without the token; 200 with it; the body holds ids and no login or repo names.
  - `test_dispatcher_secrets_not_in_tiny_pod` (`galaxy_only`): during a probe that sleeps 60 s, the test runs `kube -n ci-tiny exec <pod> -c runner -- sh -c 'cat /proc/1/environ; find /etc /work /creds -type f -exec cat {} +'` and searches the output locally with `grep -F -f` over a `umask 077` temp file, deleted at once, that holds `CI_DISPATCH_FORGEJO_TOKEN`, `CI_DISPATCH_USAGE_TOKEN`, `FORGEJO_ADMIN_TOKEN` and `FORGE_BOT_FORGEJO_TOKEN` from the test's own environment. No token ever enters a pod or an argv. Expected: no match. A positive control finds a nonce the job wrote. Tell the realliance.net executor, through Chris, that its general-runner test pipes tokens into a pod the same way.
- [ ] **Step 2: Run them.** `nix develop --command pytest tests/forge/test_forge_ci_tiny.py -v`. Expected: FAIL or ERROR (no dispatcher).
- [ ] **Step 3: Commit signed, open a draft PR.**

### Task 17: The luma-homeops manifests (not deployed)

**Files:**
- Create: `cluster/applications/ci-dispatch.yaml`; `cluster/ci-dispatch/kustomization.yaml`
- Create in `cluster/ci-dispatch/dispatcher/`: `kustomization.yaml`, `namespace.yaml`, `serviceaccount.yaml`, `pvc.yaml`, `statefulset.yaml`, `service.yaml`, `networkpolicy.yaml`, `servicemonitor.yaml`, `prometheusrule.yaml`, `alerts_test.yaml`, `config.yaml`, `keyring/owners.asc`, `keyring/fingerprints.txt`, `forgejo-token.secret.yaml` → `forgejo-token.yaml`, `usage-token.secret.yaml` → `usage-token.yaml`
- Create in `cluster/ci-dispatch/tiny/`: `kustomization.yaml`, `namespace.yaml`, `resourcequota.yaml`, `rbac.yaml`, `runner-config.yaml`
- Create: `cluster/components/netpol/egress-internet-web/{kustomization,networkpolicy}.yaml`
- Create: `scripts/test_ci_dispatch_manifests.py`, `scripts/test-ci-dispatch-alerts.sh`
- Edit: `cluster/applications/kustomization.yaml`, `.pre-commit-config.yaml` (hook `ci-dispatch-manifests`), `.renovaterc.json`

**Interfaces:**
- Consumes: the Task 14 digest, the realliance.net Task 3A pin (R2), forge-bot's keyring (F3), Task 3's pickup ruling, and the NetworkPolicy plan's Task 7 components on `main`. If `cluster/components/` is absent, wait for that task and rebase (N1).
- Produces: Service `ci-dispatch.ci-dispatch.svc.cluster.local`, ports `http` 8080 and `usage` 8081.

- [ ] **Step 1: Write the failing static tests** in `scripts/test_ci_dispatch_manifests.py`. Its docstring says it never reads `*.secret.*`; it renders with `kubectl kustomize`, like `test_netpol_manifests.py`'s helper.
  - `test_app_registered`: path `cluster/ci-dispatch`, prune + selfHeal + allowEmpty, `CreateNamespace=false` (the namespaces are manifests), no `resources-finalizer`, and listed in the applications kustomization.
  - `test_namespaces_restricted`: `ci-dispatch` and `ci-tiny` carry `pod-security.kubernetes.io/enforce: restricted`.
  - `test_dispatcher_pod`:
    - a StatefulSet with `replicas: 1`; image matching `^git\.realliance\.net/realliance-ops/ci-dispatch:sha-[0-9a-f]{7}@sha256:[0-9a-f]{64}$`;
    - uid and `fsGroup` 65532, read-only root, all capabilities dropped, `RuntimeDefault`;
    - `automountServiceAccountToken: false` and a projected token volume (`expirationSeconds: 3600`, plus `kube-root-ca.crt`);
    - `enableServiceLinks: false`; the PVC at `/data`;
    - requests `10m`/`48Mi`, limit `256Mi`, `GOMEMLIMIT=192MiB`;
    - a 64Mi `emptyDir` at `/tmp` and `TMPDIR=/tmp` (for SQLite temp files, since the root stays read-only);
    - `terminationGracePeriodSeconds: 30`; `startupProbe` and `livenessProbe` on `/healthz` (a wedge detector: it stays up while Forgejo is down).
  - `test_pvc_kept`: `rook-ceph-block-ssd`, `1Gi`, RWO, `Prune=false,Delete=false`; the Namespace `ci-dispatch` carries the same annotation.
  - `test_rbac_exact`: one Role in `ci-tiny` with exactly `batch/jobs` [create, get, list, patch, delete], `secrets` [create, delete] and `pods` [get, list]; one RoleBinding to `ci-dispatch/ci-dispatch`; no ClusterRole and no ClusterRoleBinding.
  - `test_quota_exact`: the four quota values from Global Constraints.
  - `test_tiny_runner_config`: the ConfigMap's `config.yml` has `runner.capacity: 1`, `runner.timeout: 15m`, `cache.enabled: false`, `host.workdir_parent: /work/act` and no `server.connections`; `runner.timeout` equals `Tiny.RunnerTimeout` in the dispatcher's `config.yaml`.
  - `test_config_values`: `classes == {"tiny": "dry-run", "small": "off", "medium": "off", "large": "off"}`; the `tiny` image equals `cluster/forgejo-runner-general/deployment.yaml`'s runner image while that file exists; `forgejo.url` is `http://forgejo-http.forgejo.svc.cluster.local:3000`.
  - `test_keyring_matches_forge_bot` (F3): byte-identical to `cluster/forge-bot/keyring/*` when that directory exists, otherwise skipped with a reason.
  - `test_tiny_policies`: exactly `default-deny`, `allow-dns` and `egress-internet-web` in `ci-tiny`, and no ingress rule.
  - `test_egress_internet_web`: its WAN rule equals `egress-internet-only`'s; its internet rule has the same `except` list, and ports exactly TCP 80 and 443.
  - `test_dispatcher_policy`: in `ci-dispatch`, `default-deny`, `allow-dns` and `ci-dispatch`. That last one allows ingress from namespace `prometheus` on TCP 8080 only, and egress to the Forgejo pods on TCP 3000 and to `192.168.0.240/32`, `.241/32` and `.242/32` on TCP 6443 (D16). Each rule has a comment.
  - `test_monitoring`: the ServiceMonitor has `release: prometheus`, port `http` and a keep-list of `ci_dispatch_.*`. The PrometheusRule has `release: prometheus` and exactly the five phase 1 alerts, all `severity: warning` (D7).
  - `test_renovate_never_automerges_ci_dispatch`: a dedicated `automerge: false` rule for `git.realliance.net/realliance-ops/ci-dispatch`, after every rule that could match it.
  - `test_no_plain_secrets`: the render holds no `kind: Secret`, only SealedSecrets.
  - Run. Expected: FAIL (no `cluster/ci-dispatch`).
- [ ] **Step 2: Write the alert rules and their tests.** `prometheusrule.yaml`:
  - `CIDispatchStale`: `time() - ci_dispatch_loop_last_success_timestamp > 300 or absent(ci_dispatch_loop_last_success_timestamp)`, `for: 2m`.
  - `CIDispatchLaunchFailing`: two rules sharing the name, with `reason` labels. `max(ci_dispatch_holds{reason="launch-failing"}) > 0` (`reason: held`), and `sum(increase(ci_dispatch_launches_total{outcome="error"}[15m])) > 0` for 15m (`reason: errors`).
  - `CIDispatchConfigUntrusted`: `ci_dispatch_config_verified == 0`, `for: 15m`.
  - `CIDispatchOrphanCompute`: `max by (kind) (ci_dispatch_orphans) > 0`, `for: 15m`.
  - `CITinyQuotaFull`: `kube_resourcequota{namespace="ci-tiny",resource="pods",type="used"} >= ignoring(type) kube_resourcequota{namespace="ci-tiny",resource="pods",type="hard"}`, `for: 30m`.

  `alerts_test.yaml` holds one firing and one quiet case per alert. `scripts/test-ci-dispatch-alerts.sh` follows `scripts/test-pages-alerts.sh`. Run it before writing the rules, expecting a failure, and again afterwards, expecting a pass.
- [ ] **Step 3: Write the secrets** without opening any `.secret.yaml`.
  - In sso-secrets: `printf 'CI_DISPATCH_USAGE_TOKEN=%s\n' "$(openssl rand -hex 32)" >> forge/secrets.env`, then a signed commit and a push.
  - For each of the two, a scratch `python -I` script under `umask 077` reads `forge/secrets.env` and writes the one-key Secret, printing only a byte count.
  - Seal: `KUBECONFIG=/tmp/galaxy-kubeconfig nix develop --command ./sign.sh`. Expected: two `kind: SealedSecret` files.
- [ ] **Step 4: Write the manifests** that pass Step 1's tests. `config.yaml` sets `tiny: dry-run`, and `tiny.pickup` to twice Task 3's longest registration-to-running time plus the `netpol-gate` wait that the NetworkPolicy ledger records at B2 (Task 19 Step 5 may still adjust it). The `tiny` sub-kustomization uses `components: [../../components/netpol/default-deny, ../../components/netpol/allow-dns, ../../components/netpol/egress-internet-web]`. The dispatcher sub-kustomization uses the first two and its own `networkpolicy.yaml`. The StatefulSet's comment carries D5's reasoning. The `Application` carries the comment `roll back by rendering nothing, never by deleting this file`. The `ci-dispatch` Namespace manifest carries `argocd.argoproj.io/sync-options: Prune=false,Delete=false`, as the PVC does (D5).
- [ ] **Step 5: Hook and Renovate.** `ci-dispatch-manifests` runs `python -m pytest -q -p no:cacheprovider scripts/test_ci_dispatch_manifests.py` on files `^(cluster/(ci-dispatch|components/netpol|forgejo-runner-general|forge-bot|applications)/.*|scripts/test_ci_dispatch_manifests\.py|\.renovaterc\.json|\.pre-commit-config\.yaml)$`. Rebase before touching `.renovaterc.json` (F1).
- [ ] **Step 6: Run everything.** The static tests, `scripts/test-ci-dispatch-alerts.sh`, `scripts/test_netpol_manifests.py` and `scripts/check-kustomizations.sh`. Expected: PASS. If `netpol-manifests` fails on the new namespaces, follow N2.
- [ ] **Step 7: Commit on the branch** (`ci-dispatch: dispatcher in dry-run and the ci-tiny namespace (runner classes Task 17)`) and push the branch only, never `main`.

### Task 18: Deploy in dry-run

- [ ] **Step 1: Preconditions.** N1: `B2_done` is in the NetworkPolicy ledger, and `scripts/netpol-verify.sh runner` PASSes now. Chris has merged Task 15's `ci.yaml`. No other plan's Gate G window is open (N3).
- [ ] **Step 2: Gate G before, then STOP—ask Chris.** This is the first Argo app holding a site-administrator token. Watch:
  - app `ci-dispatch` `Synced/Healthy`, pod `ci-dispatch-0` Ready, PVC `Bound`;
  - `ci_dispatch_config_verified` 1 within one sweep;
  - zero deny lines for `srcPod="ci-dispatch/`.

  Rollback triggers: a crash loop; any deny line for its legitimate traffic (the API server, Forgejo, DNS); config never verifies; any change in Gate G4 beyond the new namespaces.
- [ ] **Step 3: Push** (`git rebase origin/main && git push origin HEAD:main`). `Monitor` until Synced/Healthy and Ready (timeout 10 min).
- [ ] **Step 4: Verify.**
  - Logs: one startup line, then decisions only when they change; no `error`.
  - `scripts/netpol-verify.sh stray --since 15m`. Expected: PASS, with `ci-dispatch` and `ci-tiny` among the policed namespaces. Count the deny lines naming `ci-dispatch/` over 15 min. Expected: 0.
  - `scripts/check-prometheus-rules.sh`: exit 0, and `ci-dispatch-alerts` loaded. Prometheus `up{namespace="ci-dispatch"}` gives one series at 1.
  - A probe with `runs-on: tiny` in a `forgejo-admin` scratch repo logs one `launch` decision with mode `dry-run`, and nothing appears in `ci-tiny`.
  - Gate G after: identical to before, apart from the two namespaces.
- [ ] **Step 5: 48 h dry-run.** Ledger `dry_run_start`. At +48 h, compare the decision log against what the gates should have decided for every `tiny` job seen. Expected: no surprise, no error counter over 0, `CIDispatchStale` never fired. Forgejo abandons a probe left waiting after 24 h; ledger that.

### Task 19: `tiny` on

**Files:**
- Edit: `cluster/ci-dispatch/dispatcher/config.yaml` (`tiny: on`), `scripts/test_ci_dispatch_manifests.py` (expected mode)

- [ ] **Step 1: Preconditions.** Task 18 Step 5 green. The NetworkPolicy ledger holds its Task 9 restart gap (N1). `docs/ci.md` is on `main` (Task 20 Step 1), so the pause, the break-glass and the weekly check exist before `tiny` runs. Every node is at or below 86 % memory requested, measured after the general runner, forge-bot and pages deploys; ledger the three numbers. Each `tiny` pod adds 512Mi, about 1.7 points of one node. `ci.yaml` keeps `global.tiny_concurrent: 1`; raise it to 2 only after a clean day, with an Owner-signed commit (no deploy).
- [ ] **Step 2: Change the expected mode,** watch `test_config_values` fail, set `tiny: on`, watch it pass. Commit: `ci-dispatch: tiny on (runner classes Task 19)`.
- [ ] **Step 3: Gate G, then STOP—ask Chris.** Every member can now run code on galaxy. Rollback trigger: any acceptance test fails, any deny line from `ci-tiny/` that the matrix did not expect, or `NodeMemoryRequestsHigh`. Rollback: `tiny: off`, revert and push; for a fast stop, the break-glass.
- [ ] **Step 4: Push and `Monitor`** the pod rollout (the config hash changes).
- [ ] **Step 5: Acceptance.** `nix develop --command pytest tests/forge/test_forge_ci_tiny.py -v`. Expected: all PASS. Ledger the start times from `ci_dispatch_start_seconds`, and the `netpol-gate` wait count from the first pods (`kubectl -n ci-tiny logs <pod> -c netpol-gate`). If any launch hit the pickup timeout, raise `tiny.pickup` to twice the slowest start; that config change is a deploy, so it gets its own STOP.
- [ ] **Step 6: Stray check.** `scripts/netpol-verify.sh stray --since 30m` PASSes, and `scripts/netpol-verify.sh canary` PASSes.
- [ ] **Step 7: Sizing.** Ledger `max_over_time(container_memory_working_set_bytes{namespace=~"ci-dispatch|ci-tiny"}[6h])` and the same for CPU. Set the dispatcher's request to `max(48Mi, idle × 1.25)`; leave `tiny` at the spec's values. A changed request makes this a deploy: Gate G, **STOP—ask Chris**, push and `Monitor`.
- [ ] **Step 8: Mark the sso PR ready.** **Chris:** merges.

### Task 20: docs

**Order.** Step 1 lands early, as its own docs-only push during the Task 18 dry-run and before Task 19 Step 3 (F2), with vale run on `docs/ci.md` alone. The other steps follow Task 19.

**Files:**
- Create: `docs/ci.md`; `~/Repos/ci-dispatch/docs/members.md` (D10)
- Edit: `CLAUDE.md` (a topology row "Forge CI"), `docs/forgejo.md` (a pointer under "Where things live")

- [ ] **Step 1: `docs/ci.md`,** covering:
  - the classes in phase 1 and what each mode does;
  - pausing `tiny` (`tiny: off`, a deploy STOP) and the break-glass;
  - changing a limit: an Owner-signed `ci.yaml` commit; what an unsigned head does;
  - finding a job's record: the `jobs` endpoint of the usage API for its owner (port-forward and the usage token), and the decision log in Azure by `key`. The image is distroless, so nothing can `exec` into it;
  - alerts and what each means;
  - rotating the dispatcher token, the usage token and the `galaxy-ci-dispatch` runner secret;
  - the weekly check (D19): site administrators, local accounts (`forgejo-admin`, `forge-bot`, `forge-ci`, `ops-ci`, `ci-dispatch`) and non-ephemeral runners, each against its expected list;
  - before any enforcer restart, Talos hop or kube-network-policies bump, pause `tiny` and confirm `ci-tiny` has no pods (N5);
  - re-running `CI_DRILL=runner-proofs` at every Forgejo upgrade (D14).
- [ ] **Step 2: `docs/members.md`:** `runs-on: tiny`, its limits, what the pod can and cannot reach (ports 80 and 443 only), no `docker` on `tiny`, no `ubuntu-latest`, and how to see usage until the fork page exists (ask an Owner).
- [ ] **Step 3: Verify.** `grep -n 'ci-dispatch' CLAUDE.md docs/forgejo.md | wc -l` gives at least 2. `grep -nE 'secret|token' docs/ci.md` shows names only. Run vale on both new files. Expected: no output.
- [ ] **Step 4: Commit and push** (docs-only): `docs: forge CI runbook and the tiny class (runner classes Task 20)`. The members' guide goes to `realliance-ops/ci-dispatch` through a PR.

### Task 21: one week, then phase 1 ends

- [ ] **Step 1: At `tiny_on + 7 d`,** check the spec's "done when":
  - `max_over_time(ALERTS{alertname=~"CIDispatch.*|CITinyQuotaFull"}[7d])` is empty;
  - `max_over_time(ci_dispatch_orphans[7d])` is 0;
  - `ci_dispatch_reaped_total` shows only `pickup` or `gone-before-pickup` reasons, each explained by a ledgered event;
  - a ledger snapshot query finds no launch in `created` or `running` past its `Expires`;
  - `scripts/netpol-verify.sh stray --since 24h` PASSes.
- [ ] **Step 2: Ledger** `phase1_done=<UTC>` and open the follow-ups below as their own plans.

## Follow-ups (later plans)

- **Phase 1b:** retire `general` into `tiny` once phase 1 has run a week (the spec's table). realliance.net's workflow says `runs-on: tiny`; then remove the Deployment, its claim (S4) and its registration.
- **Phase 2, ACI:** `azure/ci/` (both resource groups, the custom role, Azure Policy, the service principal, both budgets, the sweeper and its lock), the price feed, the ACI backend, paid accounting, the ledger's restic backup (D8), the 24/7 route for the money alerts and `CIDispatchOrphanCompute` (D7), and the Azure egress rule.
- **Phase 2b:** retire `galaxy` into `small`.
- **Phase 3, VMs:** `docker`, `large`, and Spot only if Task 4 passed.
- **forge-bot:** `ForgeUnexpectedAdmin` with the D19 list.
- **The fork's usage page:** Forgejo receives `CI_DISPATCH_USAGE_TOKEN`, and `ci-dispatch` gains an ingress rule for the Forgejo pods on 8081.
- **Forgejo 19 LTS:** job webhooks, with polling kept as a backstop; re-run the phase 0 drill first.
- **Warm capacity:** only if a class's median start stays over 2 minutes for a month.
