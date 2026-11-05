# Runner classes, phase 2 (`small` and `medium` on ACI): implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to run this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** every forge member's repo can put `runs-on: small` or `runs-on: medium` in a workflow and the job runs in a fresh Azure Container Instances group that nothing else shares. The owner's signed dollar limit pays for it, and the ledger and the usage API account for it. Azure-side backstops, a sweeper that can lock the dispatcher out, and two budgets bound what a bug or a stolen credential can spend.

**Architecture:**
- `azure/ci/` (terraform, Chris applies) creates two resource groups in `westus3`. The **CI group** holds only job compute, behind an Azure Policy fence, and the dispatcher's service principal may write only there. The **guard group** holds the sweeper, a Logic App that runs every 10 min. It deletes anything older than 90 min. If burn stays over $0.60 an hour for two sweeps in a row, or a group passes the per-job egress cap, or the CI group's $85 budget is spent, it deletes all compute and removes the dispatcher's role assignment (the **lock**). Only Chris's apply restores it.
- `ci-dispatch` v0.2.0 adds a backend seam to the loop: tiny's Kubernetes Jobs, unchanged, and a new ACI backend. A paid launch reserves its worst-case cost in the ledger, registers the same ephemeral instance runner, and PUTs one container group. The group gets the runner credential as a `secureValue` and runs `ci-class-entry`, which execs `timeout` and `forgejo-runner one-job --handle`. The ARM client is stdlib HTTP behind an allow-list guard, like `internal/kube`.
- A watcher goroutine refreshes prices daily from Azure's public retail feed and reads the sweeper's run history every 5 min. Each day it also compares the ledger with Cost Management.
- Paid numbers reach the usage API. The ledger's snapshots get a nightly restic backup.

**Tech Stack:** Azure Container Instances (Linux, Standard SKU), ARM REST, Azure Policy, custom RBAC roles, Logic Apps (Consumption), Cost Management and Consumption budgets, Azure Monitor metrics (egress); terraform with `hashicorp/azurerm`, `hashicorp/azuread`, `Azure/azapi` and `hashicorp/time`; Go 1.26 stdlib (no Azure SDK); GitHub Actions to ghcr for the class image; forgejo-runner v13.2.1; kube-prometheus-stack; Argo CD; SealedSecrets; restic; pytest in `realliance/sso`.

**Spec:** `docs/superpowers/specs/2026-11-04-runner-classes-design.md` (approved 2026-11-04), phase 2 row: "`azure/ci/` (both groups, the role, Policy, the service principal, both budgets, the sweeper and its lock), the price feed, the ACI backend, and paid accounting". Done when: "paid jobs run; reservations hold; a drill with an over-ceiling resource trips the sweeper and locks; the first closed period drifts under 10 %". Read with it: plan `2026-11-04-runner-classes-phase-0-1.md` (conventions, Gate G, STOP wording, Tasks 0–21) and its SDD ledger `.superpowers/sdd/2026-11-04-runner-classes-phase-0-1/progress.md` (rulings).

## Pre-flight rulings applied (2026-11-05)

From `.superpowers/sdd/2026-11-05-runner-classes-phase-2/preflight.md` § 6. F1 is Chris's ruling; F2–F39 adopt the scan's proposed ruling, taking the first option where it offers a choice.

| F | Ruling, in short | Changed |
|---|---|---|
| F1 | A paid held undo after compute existed charges `CostOf(compute_at → stop)`, settles on absence, no try; `ErrNoCapacity` stays 0. `UndoHeld(id, at, chargedMicro)`. Held back-off 1/2/5/10/10, then 60 | Global Constraints, E11, Tasks 10, 14 |
| F2 | `Backend.SettleOnAbsence()`, `deleteSent`; `Compute` gains `Deleting`, `Hourly`, `Own`; `Listing` holds every group | E10, Tasks 11, 14 |
| F3 | `Create` undoes its own partial steps; `ErrNotCreated` for definite refusals | Task 11, 14 |
| F4 | `Listing.At` = clock before the request; paid `ComputeAt` = clock after `CreateGroup` returns | E10, Tasks 11, 14 |
| F5 | Merge `origin/main` into `v2` at gate (b); never rebase or force-push | How changes land, E1, Gates |
| F6 | The dispatcher's assignment has a fixed `uuidv5` name; the restore plan is 1 to add, 0 to change | Tasks 2, 3, 5 |
| F7 | The trip deletes the role assignment first, then the groups | E5, Task 3 |
| F8 | T14 sets the money gauges every tick, the watcher sets price age and sweeper time; `PaidCommitted` returns used and reserved apart | Tasks 10, 13, 14, 15 |
| F9 | Config shape is `paid.classes.<c>`; `testdata/site-config.yaml` | Tasks 13, 18 |
| F10 | Rollback split: migration error → revert; after a good migration → classes `off` | E13, Tasks 19, 20 |
| F11 | Charge to the first stopped sighting (or the container's finish time), release on absence; v2 `stopped_at` | Values, Tasks 10, 14 |
| F12 | `CostOf` rounds to whole seconds, int64, computed in Go inside the transaction; `Reserve` refuses negatives | Task 10 |
| F13 | No ledger `Burn`/`PaidRunning`; the loop sums `Active()`; `PaidUse` and `PaidCommitted` share one helper | Tasks 10, 12, 14 |
| F14 | No Azure id in a transcript: grep plans to counts and addresses, `-g`, `{subscriptionId}` | How changes land, Tasks 2–5 |
| F15 | Task 7 starts on a hand-written fixture; Step 6 swaps in the real one | Task 7 |
| F16 | No `instanceView` in the list → `Groups()` GETs each own group; `tag@digest` refused → `name@sha256:` | Tasks 4, 7, 18 |
| F17 | `TestLiveLocked` with the service principal during the drill | Task 5 |
| F18 | Raw meter asserts (±2 % sum), `$skip` allowed, a rounding-up decimal parser | Task 6 |
| F19 | `model.Shape(c)` is the one shape table; `prices.Floor(c)` is a func; literals pinned on both sides, Task 16 checks they match | Tasks 3, 6, 7, 13, 16 |
| F20 | `Proxy: nil` on the ARM transport; the PUT check binds shape to class; every 4xx gets a class | Task 7, E11 |
| F21 | Hash token-shaped runs; usage computes remaining from rounded cents; the cap test compares decimals | Tasks 8, 10 |
| F22 | Fair sort = phase 1's key (`TinyUsed` + `HeldMinutes`), then `Committed`, then oldest | Task 12 |
| F23 | Undo outcome per error class; `max(stored, Floor)` on read; atomic price/state snapshot | E11, Task 14 |
| F24 | `PaidNotReady` fires in dry-run, per class; `CISweeperStale`'s absent branch `for: 30m` | Task 18 |
| F25 | `ledger.Drift(from, to)`; the period ratio has no floor and the day floor stays; the first period is read wholly after Task 21 | Tasks 10, 15, 22 |
| F26 | Rotation is one STOP (apply, pipe, reseal, deploy); ledger the 150-day date | Tasks 4, 20 |
| F27 | `printf` prefix plus `cat`; `--subscription Realliance` | Task 4 |
| F28 | Gate G after Task 9's and Task 21's pushes; G1/G2 in Task 4's STOP; Task 9 restates the scripted edit | Tasks 4, 9, 21 |
| F29 | Task 10 lists `main.go` and the usage tests; `git mv schema.sql schema_v1.sql`, new `schema_v2.sql`, v2 UPDATEs `meta` | Task 10 |
| F30 | Count planned instances; `test_fence_values` ignores `sweeper`; dropped rules; `tf.sh show -json`; `dispatcher_assignment_id` sensitive | Tasks 2, 3 |
| F31 | Task 0 commits this plan into the worktree | Task 0 |
| F32 | The `runner-class` hook watches `images/forgejo-runner/Dockerfile`; create → Running < `PullAllowance` | Tasks 1, 4 |
| F33 | Match the workflow template to the server's normalised form so the plan reads "No changes." | Task 4 |
| F34 | E13's text; restic at 03:50 | E13, E16, Task 18 |
| F35 | Two more amendments (the guard-group `runs/read`, settling on absence); `docs/backups.md` | Task 20 |
| F36 | Task 0 ledgers phase 1's two unclaimed carries | Task 0 |
| F37 | The name grammar derives from `model`, one test; `claimed()`/`reconcileOne` use `ComputeName` | Tasks 7, 11 |
| F38 | `TestBuiltBodiesPassTheGuard` lives in `internal/azure`; `internal/aci` imports no `internal/prices` | Task 13 |
| F39 | An Azure-only read failure doesn't clear `ReadOK`; the fake's Deleting phase lasts at most 1 poll | Tasks 11, 14 |

## Global Constraints

### Rulings carried from phases 0 and 1 (binding)

- A credit period is a UTC calendar month. A launch counts in the period, and a `tiny` launch in the day, of its `Created`.
- The ledger is `modernc.org/sqlite`; ci-dispatch adds no Go module (`TestDependencyGuard` unchanged).
- **Spot is unavailable** on this subscription (phase 0 Task 4). No Spot code, probe or Policy exception; Policy denies Spot ACI.
- `ci.yaml` enforcement keys on numeric ids; names are labels, and a stale name is a finding.
- The reaper's name grammar covers `ci-tiny-…` only today. Phase 2 extends `ownName` (runners) and keeps the Kubernetes Job filter `tiny`-only (Task 11).
- `DeleteRunner(ctx, id, uuid)` reads the runner first and refuses one that isn't ours. `ErrNotOurRunner` is a refusal, never retried.
- A Forgejo-side hold (runner idle at pickup) is a held undo, which counts no try.
  - **Back-off:** the wait after the 1st to 6th held undo is 1, 2, 5, 10, 10, then 60 min (Chris, 2026-11-05). Phase 1's `gate.HeldBackoffAfter(held)` and `gate.BackoffUntil(held, heldAt, failed, failedAt)` own it, and every paid use calls them, never a copy.
  - **Charge (F1, Chris):** tiny held undos charge nothing. A paid held undo after compute existed charges `CostOf(compute_at → stop)`, settled on absence. `ErrNoCapacity` made no compute and charges 0. `Tries` and `Held` keep keying on `undone` + `held`.
- **Phase 1 interfaces to reuse, never re-derive** (v1 at `aa6c499` plus its round 2):
  - schema v1 with `launches_key`;
  - `gate.HeldBackoffAfter`, `gate.BackoffUntil`, `gate.Eligible`, and `Inputs.HeldMinutes` (the order-only fair-share input);
  - `ledger.Failed`, plus the back-off on error undos;
  - the `ci_dispatch_config_findings{kind}` gauge;
  - `restore` verifying at now, then at `min(at, now)` only on key or signature expiry;
  - `Proxy: nil` on the Forgejo transport, which the Azure and price clients copy (F20).
- Step budget 20 s, wedge limit `max(3 × step_budget, 2 min)`; every Azure call runs under the step's budget.
- Paid-gate stub (`gate.go`, "No paid backend in v1"): replace it, never delete the gate (Task 11 M5 carry).
- **Settle on observed absence** (phase 1 Task 14 review, M6). A paid launch never settles because a DELETE was accepted. ACI bills until the group is gone, so a row leaves its active state only once a list read after the delete lacks the group. The global $0.60-an-hour bound counts compute that still exists, not just ledger rows. (E10, Task 14)
- **Schema v1 is frozen** (Task 14 review, I1). Phase 1 deploys v1 with `CREATE INDEX launches_key ON launches(job_id, attempt)` before phase 2 exists, so every phase 2 ledger change is a v2 migration and `schema_v1.sql` is never edited. (Task 10)
- **The job listing doesn't page** (Task 14 review, M3). Forgejo 15's `GET /admin/actions/runners/jobs` has no paging, and the client caps the body at 16 MiB. Past the cap (about 80k listed jobs) `readForge` fails every tick, and nothing launches for anyone, paid classes included. Running paid jobs still stop at their hard stops, and the reaper and the sweeper still delete compute, since neither needs Forgejo. The accepted risk goes in `docs/ci.md` (Task 20); the fix is a paged listing on the fork's `WISHLIST.md`.
- **Spec amendments approved by Chris (2026-11-05)** are listed under Decided. Task 20 writes them into the spec in one commit.

### Pins (resolved read-only, 2026-11-05)

| What | Value | Source and notes |
|---|---|---|
| Region | `westus3` | phase 0 Task 4 pick (no size restricted in `list-skus`) |
| ARM API versions | `Microsoft.ContainerInstance` `2023-05-01`; `Microsoft.CostManagement` `2023-11-01`; `Microsoft.Logic` `2019-05-01`; `Microsoft.Consumption` `2023-05-01`; role assignments `2022-04-01`; resources and tags `2021-04-01`; `Microsoft.Insights` metrics `2023-10-01` (E17) | Task 4 Step 2 checks each with read-only `az provider show`. It takes a newer GA version at least 2 weeks old if one exists, and ledgers the choice |
| Token | `POST https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`, `grant_type=client_credentials`, scope `https://management.azure.com/.default` | |
| Retail prices | `GET https://prices.azure.com/api/retail/prices?api-version=2023-01-01-preview&$filter=serviceName eq 'Container Instances' and armRegionName eq 'westus3' and priceType eq 'Consumption'` | public, no auth; Task 6 Step 1 captures the live answer as a fixture |
| Terraform | `terraform` from the flake; `hashicorp/azurerm ~> 4.0`, `hashicorp/azuread 3.9.0` (entra's lock), `Azure/azapi ~> 2.0`, `hashicorp/time ~> 0.12`; exact versions at least 2 weeks old, committed in `azure/ci/.terraform.lock.hcl` | backend: `azure/bootstrap`'s storage account, key `realliance-ci.tfstate` |
| Class image bases | the five `FROM` lines of `images/forgejo-runner/Dockerfile`, byte for byte | runner 13.2.1, golang 1.26.9, node 24.21.0, ko v0.19.1, debian trixie slim |
| Class image | `ghcr.io/christopherjmiller/forgejo-runner-class:sha-<7>@sha256:<64>` | Task 1 |
| Dispatcher | `git.realliance.net/realliance-ops/ci-dispatch:sha-<7>@sha256:<64>`, tag `v0.2.0` | Task 16 |
| restic | `ghcr.io/restic/restic:0.19.1@sha256:2f0373803493361f9304a57150d464677f69a9dad487afec202105aafb2592f2` | `cluster/music-streaming/restic-backup-cronjob.yaml` |

### Values fixed by the spec (verbatim)

- **Shapes.** `small`, ACI: 2 vCPU, 8 GiB, runner timeout 30 min, hard stop 40 min after launch, $0.117 an hour. `medium`, ACI: 4 vCPU, 16 GiB, 45 min, 55 min, $0.233 an hour. Pickup timeout on ACI: 6 min.
- **Limits.** Per owner: $10 a period and 2 paid jobs at once by default, with signed overrides. Global: **$80** a period and at most $0.60 an hour of running compute. Both are ceilings in code, and config may only lower them. The spec said $100; Chris lowered it on 2026-11-05 (cache design, Decided).
- **Backstops.**
  - The sweeper is a Logic App in the guard group, every 10 min. It deletes anything older than 90 min. Over $0.60 an hour for two sweeps in a row, it deletes all compute and **locks**.
  - The CI group's budget is **$85** over the calendar month (the spec's $105, lowered with the cap by Chris on 2026-11-05); it also deletes all compute and locks.
  - **The $0.60-an-hour ceiling, rechecked against $85.** It holds and stays: spec row 2's worst case becomes $85 + 48 h × $0.60 = $113.80. Adding logging, state and the sweeper (about $12) and the cache host ($12.41) gives about $138, under $150. The arithmetic would allow up to about $0.84 an hour, (150 − 85 − 24.41) / 48, so nothing forces a change. Egress isn't in this arithmetic: E17.
  - **Lock** means the sweeper removes the dispatcher's role assignment on the CI group, and only Chris restores it, with a terraform apply.
  - The sweeper emails on a failed run. The dispatcher alerts when the sweeper's run history goes stale (30 min).
  - The $120 subscription budget stays in `azure/logging/`, untouched.
- **ACI object.** One container group per job; restart policy `Never`; no IP address, no VNet, no managed identity. The runner credential travels as a secure environment value. A `timeout` wrapper stops the group at its hard stop even when galaxy is down. The class image (runner, git, node, Go, ko) comes from ghcr. Egress stays open.
- **Prices** refresh daily; the last ones serve up to 7 days, then paid jobs wait.
- **Credential:** at most 6 months, a SealedSecret in `ci-dispatch`, its canonical copy in sso-secrets.
- **Alerts** (spec table): `CIDispatchSpendHigh` at 80 % and 95 % of the global period cap, `CIDispatchLocked`, `CIDispatchOrphanCompute` and `CISweeperStale` (30 min) go 24/7 to forge-bot's urgent receiver; `CIDispatchDrift` (> 10 % for a closed day) is weekday.

### Values this plan fixes

- **Money in micro-dollars.**
  - Reservation = `ceil(hourly × hard stop / 1 h)`: `small` 78_000, `medium` 213_584.
  - Charge = `min(reserved, CostOf(hourly, stop − compute_at))` (F11, F12).
    - `stop` is the container's `finishTime` when the list carries it, else the read time of the first list that shows the group stopped (`stopped_at`), else the read time of the first list that lacks it.
    - The reservation releases only on absence (M6).
    - `CostOf` rounds the duration up to whole seconds, then computes `ceil(secs × hourly / 3600)` in int64.
  - Burn = the sum of `HourlyMicro` over the loop's `Active()` paid rows (`reserved`, `registered`, `created`, `running`), plus `Compute.Hourly` (= `prices.ShapePrice`) of every listed group that no active row claims (F2, F13). That covers orphans, groups still deleting after an undo, and anything else in the CI group, and it is the sweeper's own pricing. The dispatcher's burn is therefore never below the sweeper's. (M6)
  - The gate allows `burn ≤ 600_000`; the sweeper trips on `> 600_000`. (E3, E4)
- **Price floor** = the spec's 2026-11-04 list prices, 117_000 (`small`) and 233_000 (`medium`), served by the func `prices.Floor(c)` (F19). The dispatcher uses `max(feed, floor)`, re-applied to stored prices when it reads them (F23), and rejects a feed above 3 × floor. `model.Shape(c)` (2/8, 4/16) is the one shape table that prices, `aci` and the Azure guard share. The sweeper prices groups by shape: 2/8 → 117_000, 4/16 → 233_000, any other shape → 1_000_000. (E4)
- **Timeout wrapper** = hard stop − 5 min (`aci.PullAllowance`): `small` 35m, `medium` 50m. `expires-at` = launch + hard stop.
- **Names.** RGs `rg-realliance-ci` and `rg-realliance-ci-guard`. App registration `realliance-ci-dispatch`. Roles `Realliance CI dispatcher`, `Realliance CI sweeper`, `Realliance CI sweeper history reader`. Logic App `ci-sweeper`. Budgets `realliance-ci` ($85) and `realliance-ci-guard` ($5). Policy initiative `realliance-ci-fence`. Action group `ci-sweeper-failed`.
- **Compute and runner names** are both `ci-<class>-<job>-<attempt>-<try>`. **Tags:** `forge-ci=1`, `ci-class`, `ci-key` (`<job>-<attempt>`), `ci-try`, `ci-owner-id`, `created-at`, `expires-at` (RFC 3339 UTC). The sweeper's state is the RG tag `ci-sweeper-over-at`.
- **Image contract.** Entry `/usr/local/bin/ci-class-entry <timeout> <class> <handle> <url> <uuid>`; token env `RUNNER_TOKEN` → file `/run/ci/token` (0600); runner configs `/etc/ci-class/runner-{small,medium}.yml`. The container runs as root, as the spec says.
- **Secrets.** SealedSecret `ci-dispatch-azure` holds keys `tenant-id`, `client-id`, `subscription-id` and `client-secret`, mounted at `/etc/ci-dispatch/azure/`. The canonical copies are `CI_DISPATCH_AZURE_{TENANT_ID,CLIENT_ID,SUBSCRIPTION_ID,CLIENT_SECRET}` in `~/Repos/realliance-sso-secrets/forge/secrets.env`. The client secret expires 180 days after creation. At 150 days `time_rotating` replaces it, and that apply destroys the old one at once, so rotation is one STOP (F26).
- **Cadence.** The Azure list is read at most every 30 s (`azure.poll`). The watcher runs every 5 min. Drift: from 06:00 UTC each day it compares day D−2, and computes a ratio only when Azure's day is at least $0.50. The period ratio runs on day 3 of the next month.
- **Caps test owner.** `forgejo-admin` (id 1) gets `paid_usd_per_period: 0.15`: one `small` reservation fits and a second does not (D11's pattern).
- **Egress (E17,** *plan's call***).** A 10 GB cap per container group, priced at 87_000 µ$ a GB. `EgressCost(bytes) = ceil(bytes × 87_000 / 1e9)`. Rate windows: 5 min for the dispatcher, 10 min for the sweeper. The watcher's egress pass runs every 60 s, and paid launches need one within 5 min.

### Gates

Every task names one:
- **(a) now:** runs in parallel with phase 1. It touches no file in `realliance-ops/ci-dispatch` that phase 1 Tasks 13 and 14 have in flight.
- **(b) after phase 1 Task 14:** needs `v1` merged into `main` of `realliance-ops/ci-dispatch`, and then `origin/main` merged into `v2` (F5), before any change to an existing dispatcher package.
- **(c) after phase 1 Task 18:** needs the dispatcher deployed in dry-run on `main` of luma-homeops before any manifest change, deploy or live paid job.

Paid `on` needs neither `tiny: on` nor NetworkPolicy B2. Paid jobs never touch `ci-tiny`. (E14)

### How changes land

- As phase 0–1's "How changes land", with these repos and branches:
  - **luma-homeops:** worktree `~/Repos/luma-homeops-ci2` on branch `ci-phase2` (`git -C ~/Repos/luma-homeops worktree add ~/Repos/luma-homeops-ci2 -b ci-phase2 origin/main`), never the main checkout. Group (c) manifests go on branch `ci-phase2-manifests`, cut from `main` after phase 1 Task 18.
  - **`realliance-ops/ci-dispatch`:** branch `v2` in worktree `~/Repos/ci-dispatch-v2`, cut from `origin/v1`. Groups (a) and (b) commit there. At gate (b), run `git merge -S origin/main`, a signed merge commit (F5). Never rebase or force-push `v2`: it's pushed. `main` stays an ancestor, so **Chris** can still fast-forward the PR (Task 16).
  - **`realliance-ops/forge`:** branch `ci-paid-limits`. **`realliance/sso`:** branch `ci-paid`.
- Signed commits only (`nix develop --command git commit -S`, with `DISPLAY=:10.0 XAUTHORITY=$HOME/.Xauthority`). Never `--no-verify`, never force-push. Every commit ends with the session's attribution lines.
- A push that changes what Argo renders is a deploy, and every deploy is a STOP. Pushes of docs, ledger, `images/`, `.github/` and `azure/ci/*.tf` code are routine; a terraform **apply** is Chris's, always.
- **Chris applies all terraform.** The agent writes, then runs `umask 077; ./azure/ci/tf.sh plan -no-color -out=$SCRATCH/ci.tfplan | grep -E '^(Plan:|No changes|  # )'`. Only counts and resource addresses reach the transcript, never scopes, principal ids or role ids (F14). It STOPs, and Chris runs `./azure/ci/tf.sh apply $SCRATCH/ci.tfplan`.
- **STOP—ask Chris** is phase 0–1's: one AskUserQuestion with the change, the Gate G evidence, what to watch and the rollback trigger. A rejected call may already have run, so read live state first.
- **Ledger:** `docs/superpowers/plans/2026-11-05-runner-classes-phase-2.ledger.md`. It holds no secret, no Azure id (subscription, tenant, object or resource id) and no hidden org, team or group name.
- **Serialize STOPs** with phase 1 and every other plan (phase 0–1 N3). Push nothing to a `realliance-ops/ci-dispatch` branch while NetworkPolicy Task 8 Steps 1–11 or Task 9 is open.
- **Gate G** is phase 0–1's (G1–G4), before and after every deploy push. Azure-only tasks skip G3/G4 but still serialize.

### Secrets, Azure, compute

- Never print, echo, `cat` or diff a secret. Values travel through pipes, stdin and `umask 077` files; never argv, assertions, logs or the ledger. In git-crypt repos use `git diff --no-textconv`. Never open `*.secret.yaml`, `*.secret.auto.tfvars` or `nodes/*`; a `python -I` script writes them and prints a byte count.
- **Azure output** reaches the ledger or the transcript only through `-o none` or a `--query` of named, id-free fields (F14).
  - Scope commands by name (`az role assignment list -g rg-realliance-ci`).
  - Write ARM URLs with az's placeholder, never the id in argv (`az rest --url 'https://management.azure.com/subscriptions/{subscriptionId}/resourceGroups/…'`).
  - Terraform plan files and their JSON (`./azure/ci/tf.sh show -json`) stay in `$SCRATCH`, mode 0600.
- **No GUID in public files:** a static test (Task 2, Task 18) fails on any UUID-shaped string in `azure/ci/` or `cluster/ci-dispatch/` outside `*.secret.*` and lock files.
- **Go:** `GO="taskset -c 0-3 nix develop $HOME/Repos/luma-homeops --command env GOMAXPROCS=4 go"`, then `$GO test -race -p=4 ./...`. No local Docker; CI builds images.
- `SCRATCH=~/Repos/luma-homeops/.superpowers/sdd/2026-11-05-runner-classes-phase-2/scratch` (git-ignored).
- CLAUDE.md S1–S9 apply: read live state before every write, GitOps only, patience over retries, confirm destructive actions.

## Decisions this plan makes

- **E1. Order.** Group (a) runs now and builds everything that doesn't touch in-flight code:
  - the image, both terraform halves, the apply, the drill;
  - two new leaf Go packages (`internal/prices`, `internal/azure`) on `v2`;
  - the acceptance suite and the 24/7 route.

  Leaf packages add files only, so merging `origin/main` into `v2` at gate (b) is clean (F5). If Chris reads "dispatcher code" strictly, Tasks 6 and 7 move to (b) with no other change.
- **E2. A backend seam before ACI.** Task 11 moves `tiny` behind a `Backend` interface with no behavior change: the whole phase 1 loop suite, the crash matrix included, passes unedited (one allowed exception, Task 11). The seam has two settle models, chosen by `SettleOnAbsence()`: tiny settles on a successful delete, as today, and ACI only on observed absence (F2). ACI then plugs in (Task 14). It's one shared reconcile with one branch, so one crash-safety proof.
- **E3. The reservation is the exact worst case** *(Chris, 2026-11-05: spec amendment a)*. The spec rounds `medium` to "$0.21", below its own worst case of $0.2136, against its rule "refuses any launch it can't pay for at worst case". The ledger reserves 213_584 µ$, and the spec's $0.08 and $0.21 are display values.
- **E4. A price floor in code.** The gate never prices a class below its 2026-11-04 list price, so a feed answering $0 can't lift the $0.60-an-hour ceiling. A higher feed price is used as is, and a feed above 3 × floor is rejected as suspect. The sweeper prices by shape with the same numbers, and prices an unknown shape at $1 an hour.
- **E5. The sweeper** *(Chris, 2026-11-05: spec amendment b)*:
  - **Definition:** one `azapi_resource` Logic App whose definition is one reviewed JSON template. It runs one at a time (trigger concurrency 1) with a 1-minute timeout on every HTTP action, so a hung run can't block the queue unseen.
  - **State:** "two sweeps in a row" lives in the CI group's tag `ci-sweeper-over-at`, which the sweeper alone can write.
  - **Budget:** the $85 budget emails at 80 % and 100 %. The lock itself comes from the sweeper reading the budget's `currentSpend` every run, so no action-group webhook URL exists, and the alert-evaluation lag goes away (the cost-data lag stays).
  - **Delete set:** it deletes only `Microsoft.ContainerInstance/containerGroups`; phase 3 extends that list with the role.
  - **Lock scope:** the lock deletes only the dispatcher's custom-role assignment. It has a fixed name, `uuidv5("url", "realliance-ci/dispatcher-assignment")`, so a restore re-creates the same id the sweeper's definition names (F6). Cost Management Reader and the history reader stay, so the dispatcher can still report that it's locked.
  - **Trip order:** the role assignment DELETE goes **first**, then the groups (F7). A failed or timed-out group DELETE can then never skip the lock.
  - **Failure email:** after a lock the run ends `Failed`, so the failed-run email fires.
- **E6. "Both budgets"** *(Chris, 2026-11-05: spec amendment c)* = `realliance-ci` ($85 since Chris's 2026-11-05 cap change, CI group, locks through the sweeper) and `realliance-ci-guard` ($5, guard group, email only, which catches a runaway sweeper). The $120 subscription budget is the existing one.
- **E7. The Policy fence** (deny, CI group, initiative `realliance-ci-fence`). It denies:
  - any type but `containerGroups` (mode `Indexed`), and any location but `westus3`;
  - more than one container, or a shape other than 2/8 or 4/16;
  - an `ipAddress`, `subnetIds`, an identity, `osType` other than Linux, `restartPolicy` other than `Never`, Spot priority, volumes, `imageRegistryCredentials` or init containers;
  - an image that doesn't match `like` `ghcr.io/christopherjmiller/forgejo-runner-class*` and `contains` `@sha256:` (Policy's `like` takes one wildcard only);
  - a missing `forge-ci` or `expires-at` tag.

  Task 2 Step 1 checks every alias exists. A rule whose alias is missing is dropped and ledgered, and the dispatcher's guard still enforces it.
- **E8. The dispatcher's custom role** gets exactly `Microsoft.ContainerInstance/containerGroups/{read,write,delete}` and `Microsoft.Insights/metrics/read` (for E17's egress reads) on the CI group. VM, disk and network rights arrive with phase 3. It never holds `containers/exec`, `logs`, `start`, `stop` or any `Microsoft.Authorization` action. Cost Management Reader goes on the CI group, and `Microsoft.Logic/workflows/runs/read` on `ci-sweeper` only. If the live smoke (Task 7) gets a 403 on the cost query, a custom `query/action` role takes Cost Management Reader's place.
- **E9. Azure ids never go public.** Tenant, client and subscription ids live in the SealedSecret, never in `config.yaml`. Terraform reads `subscription_id` and `alert_emails` from `azure/ci/ci.secret.auto.tfvars` (git-crypt), as `azure/logging` does.
- **E10. The list cadence, staleness, and settling on absence** (M6, F2, F4):
  - ACI is listed at most every 30 s. `Listing.At` is the clock **before** the request is sent.
  - A paid launch's `ComputeAt`, and the ambiguous-create mark, are the clock **after** `CreateGroup` returns.
  - The loop records `deleteSent[name]` at each DELETE. It judges a group absent only from a `Listing` whose `At` is later than both `ComputeAt` (or `Registered` for a crash leftover) and `deleteSent`. A cached list, even one read in the same step, can't end a launch it predates.
  - A DELETE answered 200, 202 or 204 settles nothing. A group whose `provisioningState` is `Deleting` is present, and a `registered` row whose group is `Deleting` is not adopted. Absence releases the reservation; the charge stops at `stop` (Values: F11).
  - Every ACI settle path waits for absence: finished, gone before pickup, pickup timeout, held undo, expired, crash leftover, and ambiguous create.
  - An **ambiguous create** (`ErrUnavailable` after the PUT left) may have made a group. The loop sends `DeleteGroup`, keeps the row `registered`, and undoes it once a later list lacks the group.
  - A **definite refusal** made nothing, so the loop undoes at once with no DELETE. Definite refusals are `ErrNotCreated`-wrapped (F3): `ErrExists`, `ErrPolicy`, `ErrNoCapacity`, `ErrLocked`, `ErrRefused`, and every other 4xx.
- **E11. Azure error classes and their undo outcomes** (F20, F23):
  - 403 `AuthorizationFailed` → `ErrLocked`. Paid classes wait `not-ready` and `ci_dispatch_azure_locked` reads 1. The undo outcome is `locked`, which `Tries` skips, with no back-off: `not-ready` already gates relaunches.
  - `ContainerGroupQuotaReached`, `ServiceUnavailable` and the quota code the drill records → `ErrNoCapacity`. It gives `UndoHeld(id, at, 0)`, no try, on `gate.HeldBackoffAfter`'s schedule.
  - `RequestDisallowedByPolicy` → `ErrPolicy`; `ErrRefused` (the guard) and every other 4xx (for example 400 `InvalidRequestContent`) → a definite refusal. Each is an error undo and a failed try, with phase 1's error back-off (`BackoffUntil`), logged at error.
  - 429, 5xx and transport errors → `ErrUnavailable`. For a create: absence first, then an error undo with the error back-off and a try. For a read: retried at the next poll.
- **E12. The watcher** is a separate goroutine (`internal/watch`), off the loop's step lock. It refreshes prices, reads the sweeper's runs, and computes drift. The loop reads prices from the ledger.
- **E13. Rollback is split** (F10, F34):
  - A **migration error** leaves the ledger at v1, because the step runs in one transaction. Revert the pin to v0.1.0.
  - **After a good migration**, set the classes `off` and keep v0.2.0. v0.1.0 refuses schema v2 and would crash-loop.
  - A forced downgrade restores the nightly v1 snapshot, which Task 19 Step 1 checks exists, and loses at most that day's accounting (`docs/ci.md`).
- **E14. Paid dry-run lasts 24 h, then `on`.** The paid gates are new, but the loop they run in has phase 1's dry-run, crash matrix and a week of `tiny` behind it.
- **E15. The 24/7 route is shared.** If forge-bot Task 15 has not created `forge-urgent`, Task 9 creates it in forge-bot's exact shape, with both plans' alerts in its regex.
- **E16. Ledger backup.** CronJob `restic-ci-dispatch` (ns `ci-dispatch`, 03:50 UTC, after the 03:00 snapshot; F34) runs as uid 65532, so the namespace stays Pod Security `restricted` with no `DAC_OVERRIDE`. It mounts the PVC read-only with `podAffinity` to `ci-dispatch-0`, backs up `/data/snapshots` only, and uses host `ci-dispatch-ledger`.
- **E17. A per-job internet egress cap** (Chris, 2026-11-05: required before paid goes on; the values are *(plan's call)*, so Chris can change them).
  - **Why:** the money model counts compute only, and ACI internet egress costs about $0.087 a GB past 100 GB a month, uncapped.
  - **The cap:** 10 GB (10_000_000_000 bytes) transmitted per container group over its life *(plan's call)*. Egress is priced at **87_000 µ$ a GB**, ignoring the 100 GB free tier, which is the money-safe side *(plan's call)*.
  - **The source:** each group's Azure Monitor metric `NetworkBytesTransmittedPerSecond`, read with `aggregation=Average&interval=FULL`, so one number covers a whole timespan. Bytes over a span = the average × the span's seconds. Two reads per group: since creation (the total), and the last window (the rate). Task 4 Step 4 checks the metric name and the `FULL` interval on the smoke group.
  - **The dispatcher (watcher, every 60 s):**
    - it reads both numbers for every listed group, own or not, and hands them to the loop;
    - the loop deletes an **own** group whose total ≥ the cap (reap reason `egress-cap`, settled on absence);
    - it adds every group's recent rate to the burn, `rate_bytes_per_s × 3600 × 87_000 / 1e9` µ$ an hour, so a heavy uploader stops new launches at once (`class-full`);
    - it records the bytes per launch, and the settle charges the owner `EgressCost(bytes)`, outside the compute reservation and uncapped, since Azure bills what was sent. That cost counts in the owner's and the global committed. An owner's egress therefore runs into their own $10 limit, not everyone's $80;
    - paid classes are `not-ready` unless the egress pass succeeded within 5 min, so an unwatched egress means no new paid jobs.
  - **The sweeper (each run, independent of the dispatcher):** it reads both numbers for **every** container group with its own managed identity and its own constants in `fence.json`. It deletes any group at or over the cap, whatever its name or tags, and adds each group's 10-minute rate to its burn for the two-sweeps rate trip. It reads nothing the dispatcher writes, so it holds when the dispatcher is compromised.
  - **Lag and overshoot:** a 1-min metric grain plus 1–3 min of metric latency plus the 60 s poll means the dispatcher sees a crossing within about 5 min, and the sweeper within one run. At ACI's unmeasured bandwidth (assume up to 1 Gbps), a job can overshoot the cap by tens of GB before it dies. The burn term is what stops new launches in that time.
  - **Residual risk:** the sweeper sees only groups alive at a run. A compromised dispatcher that churns groups shorter than 10 min evades its egress rule, as it evades the compute rate trip, so only the budget (8–48 h late) bounds it. The spec's row 2 ($85 + 48 h × $0.60) no longer bounds that case, since egress can outrun $0.60 an hour. See Decided's "open for Chris" line.
  - **Rights:** `Microsoft.Insights/metrics/read` joins the dispatcher's custom role (so the lock removes it) and the sweeper's role. Requests in the Azure guard: see Task 7, request #8.
  - **Sweeper cost** rises by about 2 HTTP actions per group per run, roughly $2–4 a month at peak. The ceiling arithmetic still holds: $85 + $28.80 + about $15 + $12.41 ≈ $141.

## Review Focus

1. **Money runaway through the dispatcher's own arithmetic.** Four causes: a launch in `reserved` or `registered` left out of the burn, a group that still runs with no active row (an orphan, or one still deleting), a configured ceiling above the code's, or a price feed answering $0. Expected: the burn counts every active row and every listed group no row claims, the code ceilings ($80 a period, $0.60 an hour) bind whatever config says, and an hourly price never drops below the floor. Tests: Task 14 `TestBurnCountsEveryActiveState` and `TestBurnCountsUnclaimedGroups`; Task 12 `TestPaidCeilingClampsConfig` and `TestPaidPlanCountsOwnLaunches`; Task 6 `TestHourlyFloor`.
2. **The lock doesn't lock.** The sweeper deletes the wrong assignment, the dispatcher reads 403 as an outage and keeps creating, or a group created during RBAC propagation survives. Expected: within two sweeps plus propagation (30 min at most), all compute is gone and the dispatcher's creates fail, until Chris applies. Tests: Task 3 `test_lock_deletes_only_the_dispatcher_assignment` and `test_trip_rule` (the assignment goes first); Task 2 `test_dispatcher_assignment_named`; Task 7 `TestErrorClasses`; Task 14 `TestACILockedWaitsNotReady`; Task 5 drill with `TestLiveLocked`, the service principal's own 403.
3. **A reservation that never frees, or frees early.** Something deletes the group, Azure is unreadable, a DELETE is accepted but the group is still `Deleting`, or the cached list predates the launch. Expected: the reservation frees on the first fresh list that lacks the group. It never frees on an accepted DELETE, or on a list read before the PUT returned, even within the same step. It stays held while Azure is unreadable. Tests: Task 14 `TestReservationFreedWhenGroupVanishes`, `TestPaidNeverSettlesOnAcceptedDelete`, `TestStaleListNeverSettlesAFreshLaunch`, `TestReservationKeptWhileAzureUnreadable` and `TestNoActivePaidRowOutlivesExpiry`.
4. **The runner credential leaks.** Paths: the group definition, an ARM error, a log line, the Activity Log, or the job's environment. Expected: it travels only as `secureValue`, no GET or error returns it, and it leaves the environment before the runner starts. Tests: Task 7 `TestGuardRefusesTokenInValue` and `TestErrorsNeverContainSecrets`; Task 13 `TestBodyTokenOnlySecure` and `TestBodyRedacts`; Task 1 smoke (`/proc/1/environ`); Task 8 `test_runner_credential_not_in_group_definition`.
5. **The sweeper stops silently.** It's disabled, a hung run blocks a queue of concurrency 1, or the dispatcher's history read fails. Expected: `CISweeperStale` fires 30 min after the last *finished* run. Tests: Task 3 `test_every_http_action_has_a_timeout`; Task 15 `TestSweeperTimestampOnlyFromFinishedRuns`; Task 18 alert cases `CISweeperStale` (stale and absent).
6. **Egress runaway.** A job, or a compromised dispatcher, sends data out of Azure past the per-job cap, or fast enough that egress outruns the compute ceiling. Expected:
   - the dispatcher deletes its own group within about 5 min of the 10 GB crossing, and the sweeper deletes any group at or over it within one run, whatever the dispatcher does;
   - both burns count the egress rate, so new launches stop and two hot sweeps lock;
   - the owner is charged the bytes;
   - an unread egress metric blocks new paid launches.

   Tests: Task 3 `test_egress_rule`; Task 7 `TestGuardMetricsRead`; Task 10 `TestEgressCharge`; Task 14 `TestEgressCapDeletesOwnGroup` and `TestBurnCountsEgress`; Task 15 `TestEgressPassAndStaleness`; Task 18 alert `CIDispatchEgressCapHit`.

## Coordination with concurrent plans

- **P1. Phase 1** (`2026-11-04-runner-classes-phase-0-1.md`):
  - Group (b) waits for its Task 14 (v1 → main, `v0.1.0`) and group (c) for its Task 18.
  - Task 18 here edits the files its Task 17 created. Its `scripts/test_ci_dispatch_manifests.py` changes only where phase 2 changes the expected value (classes, the dispatcher policy, the alert set).
  - Its Task 20 `docs/ci.md` must exist before Task 20 here.
  - Its Task 19 (`tiny: on`) and Task 19 here are separate deploys; serialize their STOPs.
- **P2. forge-bot** (`2026-11-02-forge-bot-and-ci.md`): its Task 15 owns `forge-urgent` (E15). Hand any Alertmanager change to its executor through Chris, and never edit the sealed config without Chris (its Task 15 Step 1).
- **P3. NetworkPolicy** (`2026-11-03-networkpolicy-enforcement.md`):
  - Phase 2 adds no component. It adds one rule to `cluster/ci-dispatch/dispatcher/networkpolicy.yaml` (TCP 443 to the internet) and one policy for the restic pods.
  - If `netpol-manifests` needs an entry for them, the NetworkPolicy owner adds it.
- **P4. The Forgejo fork** (`realliance/forgejo`) is the first consumer (Task 21); this plan doesn't build it.

## The first consumer: the fork's image build (estimate)

- **The build.** It runs `npm ci` and webpack, then a Go build of the backend with `bindata`, then `crane append` onto the upstream rootless image and a push to the forge registry.
- **On `small`, cold (no cache survives a job):**
  - checkout 0.5 min;
  - `npm ci` 1–2 min;
  - webpack 3–5 min;
  - module download 1 min;
  - backend build 8–12 min;
  - `crane` and push about 1 min;
  - **total 15–21 min.** That fits the 30-min runner timeout with 9–15 min to spare. Peak memory (webpack, the Go linker) is 3–4 GiB of 8.
- **On `medium`:** about 9–13 min, at about $0.045 a build against about $0.035 on `small`.
- **Decided (Chris, 2026-11-05):** `medium`. It halves the wall time for about a cent, and keeps a slow cold build clear of `small`'s hard stop.
- **Two facts for the fork's workflow, not this plan:**
  - The class image is Debian/glibc and the upstream rootless image is Alpine/musl. The binary must build with `CGO_ENABLED=0` and no `sqlite` tag, or it won't start.
  - Task 4 Step 4 measures the container's free disk. The build needs 4–6 GB.

## File map

| Path | Responsibility | Task |
|---|---|---|
| `images/forgejo-runner-class/{Dockerfile,ci-class-entry,runner-small.yml,runner-medium.yml}`, `.github/workflows/forgejo-runner-class-image.yml`, `scripts/test_runner_class_image.py` | the class image and its contract | 1 |
| `azure/ci/{main,fence,variables,outputs}.tf`, `fence.json`, `policies/*.json`, `tf.sh`, `README.md`, `ci.secret.auto.tfvars`, `.terraform.lock.hcl` | groups, budgets, Policy, roles, service principal | 2 |
| `azure/ci/sweeper.tf`, `azure/ci/sweeper/workflow.json.tftpl` | the sweeper, its identity, failed-run alert | 3 |
| `scripts/test_azure_ci.py`, `.pre-commit-config.yaml` (hooks `azure-ci`, `runner-class`), `.renovaterc.json` | static tests, hooks, Renovate rule | 1–3 |
| ci-dispatch `internal/prices/*`, `internal/model/shape.go` | the price feed, the floor, the one shape table | 6 |
| ci-dispatch `internal/azure/*`, `internal/model/names.go` | guarded ARM and token client (eight requests, egress metrics), the one name grammar | 7 |
| sso `tests/forge/test_forge_ci_paid.py`, `tests/ci_helpers.py` | paid acceptance | 8 |
| `cluster/prometheus-stack/alertmanager-config.secret.yaml` → `alertmanager-config.yaml` | 24/7 route | 9 |
| ci-dispatch `internal/ledger/{schema_v1.sql,schema_v2.sql,ledger.go}`, `internal/limits/limits.go`, `internal/usage/*`, `cmd/ci-dispatch/main.go` | schema v2, money ($80 ceiling, egress), usage paid numbers | 10 |
| ci-dispatch `internal/loop/{backend,tinybackend}.go`, `loop.go`, `reap.go` | the seam, two settle models | 11 |
| ci-dispatch `internal/gate/*` | paid gates | 12 |
| ci-dispatch `internal/config/*`, `internal/aci/*`, `internal/metrics/*` | paid config, ACI body builder, metrics | 13 |
| ci-dispatch `internal/loop/acibackend.go` and tests | ACI in the loop | 14 |
| ci-dispatch `internal/watch/*`, `cmd/ci-dispatch/main.go` | watcher (prices, sweeper history, drift, egress), wiring | 15 |
| inventory `ci.yaml` | paid classes on, caps owner, global $80 | 17 |
| `cluster/ci-dispatch/dispatcher/*` (config, azure SealedSecret, policy, alerts, restic), `cluster/backups/{prometheusrule,restic-canary-cronjob}.yaml`, `scripts/test_ci_dispatch_manifests.py` | deploy | 18 |
| `docs/ci.md`, `docs/backups.md`, `CLAUDE.md`, `azure/ci/README.md`, ci-dispatch `docs/members.md`, `docs/superpowers/specs/2026-11-04-runner-classes-design.md` (amendments) | docs, spec amendments | 20 |

---

## Group (a): starts now

### Task 0: Start

**Gate:** (a). **Files:** create the ledger (sections: Answers, Baseline, one per task).

- [ ] **Step 1: Worktrees.** luma-homeops `ci-phase2` as How changes land says. For ci-dispatch, run `git -C ~/Repos/ci-dispatch fetch && git -C ~/Repos/ci-dispatch worktree add ~/Repos/ci-dispatch-v2 -b v2 origin/v1`. Expected: clean trees.
- [ ] **Step 2: Record the answers.** Under Answers, ledger:
  - Chris (2026-11-05): phase 2 ships everyone at once, starts in parallel with phase 1, and Chris applies all terraform;
  - Spot is unavailable;
  - every carried ruling, by its `progress.md` line;
  - the pre-flight rulings (F1 Chris; F2–F39 adopted);
  - two phase 1 carries addressed to phase 2 that this plan doesn't claim (F36). Phase 1 Task 12 fix round's "M8 → Chris at Task 19/phase 2", and the hold table's growth ("revisit in phase 2"). Both are open items for Chris, raised at Task 16's review.
- [ ] **Step 3: Cross-plan reads.** Ledger:
  - phase 1's Task 13, 14 and 18 states;
  - forge-bot Task 15 (does `forge-urgent` exist?);
  - the NetworkPolicy plan's open windows;
  - `az account show --query '{name:name,state:state}'`. Expected: `Realliance`, `Enabled`.
- [ ] **Step 4: Commit and push** the ledger and this plan, copied into the worktree as it stands (F31), docs-only: `docs: runner classes phase 2 plan and ledger (Task 0)`.

### Task 1: The class image

**Gate:** (a).

**Files:**
- Create: `images/forgejo-runner-class/Dockerfile`, `ci-class-entry` (mode 0755), `runner-small.yml`, `runner-medium.yml`, `.github/workflows/forgejo-runner-class-image.yml`, `scripts/test_runner_class_image.py`
- Edit: `.pre-commit-config.yaml` (hook `runner-class` on `^(images/forgejo-runner-class/.*|images/forgejo-runner/Dockerfile|scripts/test_runner_class_image\.py|\.github/workflows/forgejo-runner-class-image\.yml)$`; F32: the FROM-equality test reads the galaxy Dockerfile too), `.renovaterc.json`

**Interfaces:**
- Produces the image contract in Global Constraints. `ci-class-entry` takes exactly 5 arguments. It exits 2 without exec'ing if any of these hold:
  - the class is not `small` or `medium`;
  - the handle fails `^[A-Za-z0-9_-]{1,128}$`;
  - the timeout fails `^[0-9]{1,2}m$`;
  - `RUNNER_TOKEN` is empty.

  Otherwise it runs `umask 077; mkdir -p /run/ci /work/act`, writes the token to `/run/ci/token`, runs `unset RUNNER_TOKEN`, then:
  ```
  exec timeout --kill-after=30s <timeout> forgejo-runner one-job --config /etc/ci-class/runner-<class>.yml --wait --handle <h> --url <url> --uuid <uuid> --token-url file:///run/ci/token --label <class>:host
  ```
  Every value is a quoted argv element, never interpolated into a command string.

- [ ] **Step 1: Write the failing static tests** in `scripts/test_runner_class_image.py`:
  - `test_froms_equal_galaxy_runner`: the five `FROM` lines equal `images/forgejo-runner/Dockerfile`'s.
  - `test_runs_as_root`: no `USER` line (spec: root in a hypervisor-isolated group).
  - `test_toolchain_installed`: `apt-get install` names `ca-certificates git gcc libc6-dev libstdc++6 make curl unzip`.
  - `test_runner_configs`: both files have `runner.capacity: 1`, `cache.enabled: false` and `host.workdir_parent: /work/act`. `runner.timeout` is `30m` (small) and `45m` (medium). Neither has `server.connections`.
  - `test_entry_contract`: the script is `#!/bin/sh` with `set -eu`, contains `unset RUNNER_TOKEN` before `exec timeout`, and has no `eval`. `"$3"` (the handle) appears only as one quoted argument.
  - `test_renovate_never_automerges_class_image`: a rule matching `ghcr.io/christopherjmiller/forgejo-runner-class` with `automerge: false`, after every rule that could match it.

  Run: `nix develop --command pytest -q scripts/test_runner_class_image.py`. Expected: FAIL (no files).
- [ ] **Step 2: Write the image, configs, entry script and Renovate rule** (description copied in tone from the `forgejo-runner` rule: "a new image is a change to the CI trust boundary"). Expected: the tests PASS.
- [ ] **Step 3: Write the workflow**, a copy of `forgejo-runner-image.yml` with the class paths. Its smoke step, against the pushed image:
  - versions: `forgejo-runner --version | grep -F 13.2.1`, `go version | grep -F go1.26.`, `node --version | grep -E '^v24\.'`, plus `ko version`, `gcc`, `make`, `git`, `curl`, `unzip` and `timeout --version`;
  - the environment: `test "$(id -u)" = 0`, and `! command -v docker && ! command -v nix`;
  - the entry contract, with a stub `forgejo-runner` first on `PATH` that prints its argv, `grep -c RUNNER_TOKEN /proc/self/environ` (expected `0`), the token file's mode (`600`) and whether its content equals the nonce (`same`). Run it with `RUNNER_TOKEN=nonce-$RANDOM`; expected argv is exactly the contract's;
  - refusals: `a b` as the handle, `large` as the class, and an empty token each exit 2, and the stub never prints.
- [ ] **Step 4: Commit and push** (routine: `images/`, `.github/`). `Monitor` the GitHub run: `gh run watch` on the newest `forgejo-runner-class image` run. Expected: success. Ledger the `sha-<7>@sha256:<64>` pin and the compressed size (`crane manifest … | jq '[.layers[].size]|add'`).
- [ ] **Step 5: Chris** sets the ghcr package `forgejo-runner-class` to public (GitHub UI), since ACI pulls anonymously. Verify: an anonymous `crane digest` of the pin returns the ledgered digest.

### Task 2: `azure/ci/`, the fence

**Gate:** (a).

**Files:**
- Create: `azure/ci/main.tf` (providers with `resource_provider_registrations = "none"`; backend key `realliance-ci.tfstate`; both RGs, the RG ignoring `tags["ci-sweeper-over-at"]`; both budgets; `azurerm_resource_provider_registration` for whichever of `Microsoft.ContainerInstance` and `Microsoft.Logic` Step 1 finds unregistered).
- Create: `azure/ci/fence.tf` (Policy definitions, the initiative and its assignment on the CI group; role definition `Realliance CI dispatcher`; `azuread_application`, `azuread_service_principal`, `time_rotating` at 150 days, `azuread_application_password` with `end_date = timeadd(time_rotating.dispatcher.id, "4320h")`; assignments of the custom role and of `Cost Management Reader`, both scoped to the CI group. The custom-role assignment has the fixed `name = uuidv5("url", "realliance-ci/dispatcher-assignment")` (F6), so a restore re-creates the same id and no GUID sits in a file).
- Create: `azure/ci/variables.tf`, `outputs.tf` (`dispatcher_client_id`, `tenant_id`, `dispatcher_client_secret` and `dispatcher_assignment_id`, all `sensitive`; F30), `fence.json`, `policies/{types,location,aci-shape,aci-isolation,aci-image,tags}.json`, `tf.sh`, `ci.secret.auto.tfvars` (`subscription_id`, `alert_emails`, written by a `python -I` script from `azure/logging/logging.secret.auto.tfvars`, never opened), `scripts/test_azure_ci.py`.
- Edit: `.pre-commit-config.yaml` (hook `azure-ci` on `^(azure/ci/.*|scripts/test_azure_ci\.py|\.pre-commit-config\.yaml)$`).

**Interfaces:**
- Produces `fence.json`, which every value a test pins comes from. Terraform reads it with `jsondecode(file(...))`:
  ```json
  {"region": "westus3", "ci_group": "rg-realliance-ci", "guard_group": "rg-realliance-ci-guard",
   "budget_ci_usd": 85, "budget_guard_usd": 5, "paid_cap_usd": 80,
   "shapes": [{"cpu": 2, "memory_gb": 8}, {"cpu": 4, "memory_gb": 16}],
   "image_like": "ghcr.io/christopherjmiller/forgejo-runner-class*", "image_contains": "@sha256:",
   "dropped_rules": [],
   "dispatcher_actions": ["Microsoft.ContainerInstance/containerGroups/read",
     "Microsoft.ContainerInstance/containerGroups/write", "Microsoft.ContainerInstance/containerGroups/delete",
     "Microsoft.Insights/metrics/read"],
   "egress": {"cap_bytes": 10000000000, "micro_per_gb": 87000, "metric": "NetworkBytesTransmittedPerSecond"}}
  ```
- `tf.sh` copies `azure/entra/tf.sh`: it checks `az account show`, refuses unless `ci.secret.auto.tfvars` reads as cleartext (git-crypt unlocked, checked with `grep -q '^subscription_id'`), and runs `exec terraform "$@"`.
- **Rotation (F26).** At 150 days `time_rotating` replaces the password, and that same apply destroys the old one. The dispatcher fails auth until the new secret is deployed, so rotation is one STOP run back to back: Chris's apply, the pipe into sso-secrets (Task 4 Step 3's form), reseal, and the deploy push. Task 4 Step 3 ledgers the 150-day date.

- [ ] **Step 1: Check the Policy aliases** (read-only):
  ```
  az provider show -n Microsoft.ContainerInstance --expand resourceTypes/aliases --query "resourceTypes[?resourceType=='containerGroups'].aliases[].name" -o tsv
  ```
  Ledger, for each rule in E7, whether its alias exists. A missing one: drop the rule and ledger it. Also read `az provider show -n <rp> --query registrationState` for `Microsoft.ContainerInstance` and `Microsoft.Logic`. Declare `azurerm_resource_provider_registration` only for one that isn't `Registered`; a comment records the other.
- [ ] **Step 2: Write the failing tests** in `scripts/test_azure_ci.py`:
  - `test_fence_values`: `fence.json` without its `sweeper` key (Task 3's) equals the preceding block, with `budget_ci_usd == 85` and `paid_cap_usd == 80`. The budget must exceed the code ceiling: `budget_ci_usd > paid_cap_usd` (F30, Chris's cap change).
  - `test_dispatcher_assignment_named` (F6): the custom-role assignment's `name` is `uuidv5("url", "realliance-ci/dispatcher-assignment")`, and the sweeper's template takes its id from that resource.
  - `test_dispatcher_role_exact`: the role's `actions` equal `fence.json`'s `dispatcher_actions`; `not_actions`, `data_actions` and `not_data_actions` are empty; `assignable_scopes` is the CI group only.
  - `test_policies_deny`: every policy JSON has `effect` `deny`. `types` allows exactly `Microsoft.ContainerInstance/containerGroups` with mode `Indexed`. `location` allows `westus3` only. `aci-shape` admits exactly the two shapes and one container. `aci-isolation` denies each field E7 lists, minus the rules Step 1 dropped, which the test reads from a `fence.json` list `dropped_rules` (F30). `aci-image` uses `image_like` (one `*` only) and `image_contains`. `tags` requires `forge-ci` and `expires-at`.
  - `test_password_lifetime`: `time_rotating.rotation_days == 150`, and `end_date` adds `4320h`.
  - `test_assignments_scoped_to_ci_group`: no role assignment in `fence.tf` scopes to a subscription.
  - `test_no_guid_in_public_files`: no `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}` in `azure/ci/**` outside `*.secret.*` and `.terraform.lock.hcl`.
  - `test_fmt`: `terraform fmt -check -recursive azure/ci` exits 0.
  - Plan-level tests, skipped unless `AZURE_CI_PLAN_JSON` names a `./azure/ci/tf.sh show -json` file:
    - `test_plan_creates_only`: every planned action is `create`, and their count equals the planned resource **instances** (F30: `for_each` and `count` expand), checked against a list of expected addresses in the test;
    - `test_plan_sp_has_two_assignments_in_ci_group`.

  Run them. Expected: FAIL.
- [ ] **Step 3: Write the terraform** that passes them. Comments state the spec layer each resource implements (layers 5, 8 and 9).
- [ ] **Step 4: Plan.** Run `./azure/ci/tf.sh init`, then the plan in How changes land's grep form (F14). Then run `umask 077; ./azure/ci/tf.sh show -json $SCRATCH/ci.tfplan > $SCRATCH/plan.json`, and the tests with `AZURE_CI_PLAN_JSON=$SCRATCH/plan.json`. Expected: PASS, and `Plan: <N> to add, 0 to change, 0 to destroy.` Ledger N and the addresses, nothing else from the plan.
- [ ] **Step 5: Commit and push** (code only; nothing applied yet).

### Task 3: `azure/ci/`, the sweeper and its lock

**Gate:** (a).

**Files:**
- Create: `azure/ci/sweeper.tf`, holding:
  - the workflow: `azapi_resource` `Microsoft.Logic/workflows@2019-05-01` in the guard group, with a system-assigned identity and its definition from `templatefile("sweeper/workflow.json.tftpl", …)`;
  - the sweeper's custom role and its assignment to the identity on the CI group;
  - the history reader role and its assignment to the dispatcher's service principal on the workflow;
  - the action group `ci-sweeper-failed`, emailing `alert_emails`;
  - an `azurerm_monitor_metric_alert` on the workflow's `RunsFailed`: total > 0, window PT15M, frequency PT5M.
- Create: `azure/ci/sweeper/workflow.json.tftpl`
- Extend: `fence.json` (sweeper values), `scripts/test_azure_ci.py`

**Interfaces:**
- `fence.json` gains:
  ```json
  "sweeper": {"interval_minutes": 10, "max_age_minutes": 90, "ceiling_micro_per_hour": 600000,
    "repeat_window_minutes": 12, "action_timeout": "PT1M",
    "price_micro_per_hour": {"2/8": 117000, "4/16": 233000, "other": 1000000},
    "delete_types": ["Microsoft.ContainerInstance/containerGroups"],
    "actions": ["Microsoft.ContainerInstance/containerGroups/read", "Microsoft.ContainerInstance/containerGroups/delete",
      "Microsoft.Resources/subscriptions/resourceGroups/read", "Microsoft.Resources/subscriptions/resourceGroups/resources/read",
      "Microsoft.Resources/tags/read", "Microsoft.Resources/tags/write", "Microsoft.Consumption/budgets/read",
      "Microsoft.Authorization/roleAssignments/read", "Microsoft.Authorization/roleAssignments/delete",
      "Microsoft.Insights/metrics/read"],
    "egress_rate_window_minutes": 10}
  ```
  The egress cap and price come from `fence.json`'s top-level `egress` block (Task 2), shared with nothing in the dispatcher (E17).
- **The run, in order** (every HTTP action authenticates with `ManagedServiceIdentity`, audience `https://management.azure.com/`):
  1. Read the container groups, the CI group's resources with `$expand=createdTime`, the budget, and the RG's tags.
  2. For **every** container group, whatever its name or tags, read `NetworkBytesTransmittedPerSecond` twice with `aggregation=Average&interval=FULL`: over `createdTime → now` (total bytes = average × seconds) and over the last 10 min (the rate). Delete any group whose total ≥ `egress.cap_bytes` (E17).
  3. Sum the burn sequentially: shape price, plus each group's egress rate as `rate_bytes_per_s × 3600 × egress.micro_per_gb / 1e9` µ$ an hour.
  4. Delete each listed `delete_types` resource older than `max_age_minutes`.
  5. Trip when either holds:
     - burn > `ceiling_micro_per_hour` and the tag `ci-sweeper-over-at` ≥ now − `repeat_window_minutes`;
     - the budget's `currentSpend.amount` ≥ `budget_ci_usd`.

     The trip **first** DELETEs `${dispatcher_assignment_id}`. That id is stable across restores (F6), and 204 or 404 counts as done. **Then** it deletes every container group, running after the assignment action whether it `Succeeded`, `Failed` or `TimedOut` (F7). Last it ends `Terminate` `Failed`, code `Locked`.
  6. Without a trip: over the ceiling, merge the tag with the current time; otherwise delete the tag if present.
- The ABAC condition: the sweeper's assignment carries `condition_version = "2.0"` with a condition limiting `roleAssignments/delete` to `@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {dispatcher role id}`. If the plan refuses it, drop it and ledger that *(plan's call)*. The CI group holds no other assignment worth guarding at its scope.

- [ ] **Step 1: Write the failing tests** (they render the template with fixed placeholder values):
  - `test_trigger`: recurrence `Minute`/`10`, and `runtimeConfiguration.concurrency.runs == 1`.
  - `test_every_http_action_has_a_timeout` (Review Focus 5): every `Http` action, inside loops and conditions included, has `limit.timeout` `PT1M` and MSI authentication with that audience.
  - `test_age_kill`: the filter compares `createdTime` with `max_age_minutes` (90) and the type with `delete_types`.
  - `test_burn_by_shape`: the price expressions use exactly the three `price_micro_per_hour` values, which equal the literals 117000, 233000 and 1000000. Task 6 pins the same literals and Task 16's review checks they match (F19).
  - `test_trip_rule`: the comparisons use `600000`, the window `12` and `budget_ci_usd` (85). The assignment DELETE is the trip's first action. The group-delete loop's `runAfter` on it is exactly `[Succeeded, Failed, TimedOut]`, and the run ends `Failed` (F7).
  - `test_lock_deletes_only_the_dispatcher_assignment` (Review Focus 2): exactly one `DELETE` aims at `Microsoft.Authorization`. Its URI is the `dispatcher_assignment_id` placeholder, never the Cost Management Reader or history-reader assignment.
  - `test_sweeper_role_exact`: actions equal `fence.json` `sweeper.actions`.
  - `test_history_reader_exact`: `["Microsoft.Logic/workflows/runs/read"]`, scoped to the workflow.
  - `test_failed_run_alert`: metric `RunsFailed`, threshold 0, operator `GreaterThan`, to `ci-sweeper-failed`.
  - `test_egress_rule` (Review Focus 6, E17):
    - the metric loop runs over every container group from the list, with no filter on name or tags;
    - both metric reads use `NetworkBytesTransmittedPerSecond`, `aggregation=Average` and `interval=FULL`, and the delete compares against `egress.cap_bytes` (10000000000);
    - the burn's egress term uses `egress.micro_per_gb` (87000) and the 10-minute window;
    - no expression reads a tag, a dispatcher output or anything outside the CI group. The rule holds with a compromised dispatcher.

  Run them. Expected: FAIL.
- [ ] **Step 2: Write the template and `sweeper.tf`.** Expected: PASS.
- [ ] **Step 3: Plan** as in Task 2 Step 4 (grep form). Expected: Task 2's N plus this task's resources to add, 0 to change, 0 to destroy. Ledger the count.
- [ ] **Step 4: Commit and push.**

### Task 4: Chris applies; read-only checks; credentials; smoke

**Gate:** (a), after Tasks 1–3.

- [ ] **Step 1: STOP—ask Chris** to apply `$SCRATCH/ci.tfplan`. State:
  - two RGs, the fence, the service principal, two budgets and the sweeper; cost about $2 a month; the sweeper starts at once and finds nothing;
  - Gate G1 and G2 evidence from the ledger (F28): the apply touches no cluster object, but serializes with other plans' windows.

  Rollback trigger: an apply error, or anything outside the two RGs. Rollback: Chris's `./azure/ci/tf.sh destroy`, after a STOP.
- [ ] **Step 2: Verify read-only** (F14 forms throughout).
  - The plan, in the grep form, reports `No changes.` (F33). If it shows `1 to change` on `azapi_resource` because the server normalised the definition, edit the template to the server's form and re-plan until it doesn't. That's a code change; Chris re-applies if it differs.
  - `az provider show -n Microsoft.ContainerInstance --query registrationState` is `Registered`. Check the API versions in Pins.
  - `az role assignment list -g rg-realliance-ci --query "[].roleDefinitionName" -o tsv` lists `Realliance CI dispatcher`, `Cost Management Reader` and `Realliance CI sweeper`, and nothing else.
  - The sweeper's two newest runs show `Succeeded`: `az rest --url 'https://management.azure.com/subscriptions/{subscriptionId}/resourceGroups/rg-realliance-ci-guard/providers/Microsoft.Logic/workflows/ci-sweeper/runs?api-version=2019-05-01&$top=2' --query "value[].properties.status"`.
- [ ] **Step 3: Credentials into sso-secrets**, never in argv or output (F27):
  ```bash
  cd ~/Repos/realliance-sso-secrets
  for k in tenant_id:TENANT_ID dispatcher_client_id:CLIENT_ID dispatcher_client_secret:CLIENT_SECRET; do
    { printf 'CI_DISPATCH_AZURE_%s=' "${k##*:}"; ~/Repos/luma-homeops-ci2/azure/ci/tf.sh output -raw "${k%%:*}"; echo; } >> forge/secrets.env
  done
  { printf 'CI_DISPATCH_AZURE_SUBSCRIPTION_ID='; az account show --subscription Realliance --query id -o tsv; } >> forge/secrets.env
  ```
  Signed commit and push. Ledger dates only, never ids: the secret's expiry (creation + 180 d) and its **rotation date** (creation + 150 d, F26), the day the rotation STOP falls due.
- [ ] **Step 4: Smoke, as Chris's identity** (pre-approved in Step 1's STOP; cost under $0.02). Create a `small` group `ci-smoke-<hex>`:
  - the class pin, with both tags;
  - command `["/usr/local/bin/ci-class-entry","35m","small","smokehandle","https://git.realliance.net","00000000-0000-0000-0000-000000000000"]`;
  - `RUNNER_TOKEN` as a secure value holding a nonce, passed as `--secure-environment-variables @<umask-077 file holding RUNNER_TOKEN=<nonce>>`, never in argv.

  Then:
  - record the time from create to `Running` and to `Terminated` (`Monitor`). Expected: create → `Running` under `aci.PullAllowance` (5 min). If it isn't, raise `PullAllowance` in Task 13 to twice the measured time, so `small`'s timeout wrapper gets 40 min minus that (F32);
  - a second group runs `["sh","-c","df -h / /run; nproc; free -g; curl -fsS -o /dev/null https://github.com"]`. Ledger its output (the free disk the fork build needs);
  - **egress metric check** (E17). Run `az monitor metrics list-definitions --resource <group> -g rg-realliance-ci --resource-type Microsoft.ContainerInstance/containerGroups --query "[].name.value"`; it lists `NetworkBytesTransmittedPerSecond`. Then `az rest` GET that metric for the second group (`{subscriptionId}` form, `api-version=2023-10-01&metricnames=NetworkBytesTransmittedPerSecond&aggregation=Average&interval=FULL&timespan=<created>/<now>`) returns one data point above 0. Ledger the metric's latency (seconds from the transfer to a non-zero point). If `interval=FULL` is refused, STOP: the sweeper's two-reads design (Task 3) needs a rework before Task 5;
  - `az container show` and `az container logs` of the first group, searched locally with `grep -F -f <nonce file>`. Expected: no match;
  - save the raw ACI **list** (`az rest --url 'https://management.azure.com/subscriptions/{subscriptionId}/resourceGroups/rg-realliance-ci/providers/Microsoft.ContainerInstance/containerGroups?api-version=2023-05-01'`) to `$SCRATCH/aci-list.json`, mode 0600, and save a copy as the Task 7 fixture with every GUID replaced by zeros. Ledger whether the list carries `instanceView.state`, the container's `currentState`, its `exitCode` and its `finishTime`, and whether `tag@sha256` image references are accepted. The fallbacks are fixed now (F16):
    - no `instanceView` in the list → Task 7's `Groups()` GETs each `GroupName` group after the list (request #3, already allowed), under the budget, with the `Azure` interface unchanged;
    - `tag@digest` refused → the pin becomes `name@sha256:`, and Task 18's regex changes to match;
  - delete both groups; `Monitor` until `az container list -g rg-realliance-ci --query "length(@)"` is `0`.
- [ ] **Step 5: Policy negatives** (each refused at create, free). Expected `RequestDisallowedByPolicy` for each:
  - 1 vCPU/2 GiB;
  - image `busybox`;
  - `--ip-address Public`;
  - `--assign-identity`;
  - location `westus2`;
  - no `forge-ci` tag.

  Ledger each code. Any create that succeeds is a STOP: delete it, and fix the fence first.
- [ ] **Step 6: Region check.** If Step 4 got a capacity refusal (`ServiceUnavailable`), STOP: moving to `westus2` is Chris's call (a terraform change and a re-apply).

### Task 5: The sweeper drill

**Gate:** (a), after Task 4 and Task 7 Step 5 (`live_test.go` exists). v0.2.0 can't be running yet, since Task 19 needs this task.

- [ ] **Step 1: STOP—ask Chris.** Pre-approve exactly what follows:
  - groups created as his identity in the CI group, for about 2.5 h and about $0.65;
  - the sweeper removing the dispatcher's assignment;
  - `TestLiveLocked` while locked: the service principal's own calls, $0, creating nothing (F17);
  - his restore apply.
- [ ] **Step 2: Age kill.** Create one `small` group `ci-drill-age-<hex>` running `["sleep","7200"]`, with `expires-at` 3 h away (the sweeper ignores tags). `Monitor` until it disappears. Expected: 90–102 min after creation (a sweep every 10 min, plus the delete). Ledger the time.
- [ ] **Step 3: Rate trip.** Create five `medium` groups `ci-drill-rate-<hex>-{1..5}`: 20 cores and $1.165 an hour. Then try a sixth, `small`. Expected: a quota refusal. Ledger its exact error code; it feeds `azure.ErrNoCapacity` (E11). `Monitor`:
  1. The first over-ceiling sweep sets `ci-sweeper-over-at`.
  2. The second removes `Realliance CI dispatcher` from the CI group **first**, then deletes the five groups (F7); its run ends `Failed` with code `Locked`.
  3. The failed-run email arrives (Chris confirms).

  Expected: steps 1–2 within 22 min of the creates.
- [ ] **Step 4: Verify the lock.**
  - Names only: `az role assignment list -g rg-realliance-ci --query "[].roleDefinitionName" -o tsv` lists `Cost Management Reader` and `Realliance CI sweeper`, not `Realliance CI dispatcher`.
  - The budget action of the newest succeeded run shows `Succeeded`, which proves the identity reads `currentSpend`.
  - **From the dispatcher's side** (F17): run `CI_AZURE_LIVE=1 CI_AZURE_CREDS=$SCRATCH/azure-creds $GO test -tags live -run TestLiveLocked ./internal/azure/`, with Task 7 Step 6's credential files. `Monitor` it for up to 30 min of RBAC propagation. Expected: `Groups` and `CreateGroup("ci-small-0-0-2", …)` both return `ErrLocked`, and `aci_group_names()` stays empty. Ledger the real error code (it confirms or corrects `TestErrorClasses`'s 403 row), then `rm -r $SCRATCH/azure-creds`.
- [ ] **Step 5: STOP—Chris restores.** The plan in the grep form reports exactly `Plan: 1 to add, 0 to change, 0 to destroy.`. That's the assignment, under its fixed name, so the workflow's parameter is unchanged (F6). Chris applies. The next sweep below the ceiling clears the tag. Ledger the drill: timings, the quota code, and its cost from the next day's Cost Management, which Task 22 needs (F25). This meets the done-when "a drill with an over-ceiling resource trips the sweeper and locks".

### Task 6: `internal/prices`

**Gate:** (a); a new leaf package on `v2`.

**Files:** create `internal/prices/{prices.go,decimal.go,guard.go,prices_test.go,guard_test.go,writes_test.go}`, `testdata/aci-westus3.json`, and `internal/model/shape.go` with its test. It's a new file in an existing package, so `v2` still only adds files.

**Interfaces (produces):**
```go
// model (shape.go): the one shape table; prices, aci and the azure guard all read it (F19)
func Shape(c Class) (vcpu, gib int, ok bool)                       // small 2/8, medium 4/16; others !ok
// prices
type Rates struct{ VCPUHour, GiBHour limits.Micro; Fetched time.Time }
func Floor(c model.Class) (limits.Micro, bool)                     // small 117_000, medium 233_000; a func, not a mutable map
const SuspectFactor = 3
var ErrSuspect, ErrAmbiguous, ErrUnit, ErrDecimal error
func Fetch(ctx context.Context, hc *http.Client, region string, now time.Time) (Rates, error)
func Hourly(r Rates, c model.Class) (limits.Micro, error)          // max(vcpu×VCPUHour + gib×GiBHour, Floor(c)); > SuspectFactor×Floor → ErrSuspect
func ShapePrice(vcpu, gib float64) limits.Micro                    // the sweeper's table: 2/8 → 117_000, 4/16 → 233_000, anything else → 1_000_000
func ParseUSD(s string) (limits.Micro, error)                      // any number of decimals or an exponent; sub-micro amounts round UP (F18)
```
`limits.ParseMicro` is not reused: it takes at most 6 decimals and no exponent, and feed and Cost Management numbers carry both. `Fetch` builds its transport with `Proxy: nil`, as phase 1's Forgejo client does (F20).

- [ ] **Step 1: Capture the fixture.** Make a public read of the Pins URL with `curl -fsS`, following `NextPageLink`. Save the items the filter returns as `testdata/aci-westus3.json` (public data). Ledger the vCPU and memory meters' `meterName`, `skuName`, `productName` and `unitOfMeasure`. Those strings become the parser's exact match (Linux, Standard; never Windows, Spot, Confidential or GPU).
- [ ] **Step 2: Write the failing tests.**
  - `TestFetchParsesFixture` (F18): the raw `VCPUHour` and `GiBHour` equal the ledgered meter prices exactly. The unclamped sums `2×VCPUHour + 8×GiBHour` and `4×VCPUHour + 16×GiBHour` are within ±2 % of 117_000 and 233_000. The test checks these raw values, not the floor-clamped `Hourly`.
  - `TestShape` (model): `small` → (2, 8, true), `medium` → (4, 16, true), `tiny` and `large` → `ok` false.
  - `TestParseUSD`: `"0.0000135"` → 14 (rounded up); `"1.35e-05"` → 14; `"0.117"` → 117_000; `"-1"`, `""` and `"NaN"` → `ErrDecimal`.
  - `TestHourlyFloor` (Review Focus 1): rates of 0 give 117_000 and 233_000; 50 % of list gives the floor; 110 % gives the feed value.
  - `TestHourlySuspect`: rates that put `small` at 351_001 give `ErrSuspect`.
  - `TestFetchRefusesAmbiguous`: two items matching one meter give `ErrAmbiguous`.
  - `TestShapePriceMatchesSweeper`: (2, 8) → 117_000, (4, 16) → 233_000, and (2, 16), (1, 2) and (0, 0) → 1_000_000. These are literals pinned here and in Task 3's `test_burn_by_shape`. A luma-homeops hook can't read this repo, so Task 16's review checks the two match (F19).
  - `TestUnits`: `1 Hour` and `1 Second` (×3600) parse; any other unit gives `ErrUnit`.
  - `TestGuard` (F18): only GET to `https://prices.azure.com/api/retail/prices` with the Pins query, plus at most one numeric `$skip` (the form `NextPageLink` uses). A `NextPageLink` to another host or path, or with any other change, is refused unsent. At most 10 pages; no redirect followed. A set `HTTPS_PROXY` is ignored (F20).
  - `TestNoWritesInSource`: the AST holds no method but GET (kube's `writes_test.go` pattern).
- [ ] **Step 3: Run them.** Expected: FAIL. **Step 4: Write the code** (money parsed by `ParseUSD`, never through a float). **Step 5: Run.** Expected: PASS. Commit: `prices: the ACI retail feed, a floor at list price`. Push `v2` (outside the N3 windows). Never rebase it afterwards (F5).

### Task 7: `internal/azure`, the guarded ARM client

**Gate:** (a); a new leaf package on `v2`. Step 6 (live) after Tasks 4 and 5's quota code (F15).

**Files:** create `internal/azure/{client.go,guard.go,token.go,creds.go,client_test.go,guard_test.go,writes_test.go,live_test.go}` and `testdata/`.
- Steps 1–4 start from a list fixture **hand-written** to the `2023-05-01` schema and a placeholder quota code `ContainerGroupQuotaReached`. Step 6 swaps in Task 4's id-free fixture and Task 5's real code, then re-runs (F15).
- Hand-written Cost Management and run-history answers.

**Interfaces (produces):**
```go
type Credentials struct{ TenantID, ClientID, SubscriptionID string; Secret secret.Value }
func LoadCredentials(dir string) (Credentials, error)  // files tenant-id, client-id, subscription-id, client-secret; one line each; errors name the file, never its content
type Config struct{ ResourceGroup, Region, GuardResourceGroup, Sweeper, Image string }
func New(c Credentials, cfg Config, hc *http.Client) (*Client, error)
type Group struct {
    Name string; Tags map[string]string
    Provisioning, State string // provisioningState (incl. "Deleting"); instanceView.state
    Terminated bool; ExitCode int; Finish time.Time   // the container's currentState; Finish zero if absent (F11)
    CPU, MemoryGB float64
}
type Run struct{ Status string; Start, End time.Time }   // End zero while running
func (c *Client) Groups(ctx context.Context) ([]Group, error)                // every group in the CI group, own names or not; pages through nextLink on the same path only.
                                                                             // If the list lacks instanceView (Task 4 Step 4), it GETs each GroupName group (#3) under the caller's deadline (F16)
func (c *Client) Group(ctx context.Context, name string) (Group, error)
func (c *Client) CreateGroup(ctx context.Context, name string, body secret.Value) error // GET first: present → ErrExists, nothing sent
func (c *Client) DeleteGroup(ctx context.Context, name string) error         // 200, 202, 204, 404 are success
func (c *Client) DailyCost(ctx context.Context, from, to time.Time) (map[string]limits.Micro, error) // "2006-01-02" → µ$, USD only
func (c *Client) SweeperRuns(ctx context.Context) ([]Run, error)              // newest first, at most 10
func (c *Client) Egress(ctx context.Context, group string, from, to time.Time) (avgBytesPerSecond float64, err error) // request #8, E17; bytes = avg × (to − from)
var ErrNotFound, ErrExists, ErrLocked, ErrNoCapacity, ErrPolicy, ErrRejected, ErrUnavailable, ErrRefused error // ErrRejected: any other 4xx (F20)
var GroupName = model.NameRE(model.Small, model.Medium)  // derived from model's grammar, not retyped (F37)
```
This task also adds the new file `internal/model/names.go` (add-only, like `shape.go`), holding the one name grammar (F37) and its test `TestNameGrammar`:
```go
func ComputeName(c Class, k Key, try int) string        // "ci-<class>-<job>-<attempt>-<try>"
func NameRE(classes ...Class) *regexp.Regexp            // ^ci-(<c1>|<c2>…)-[0-9]{1,18}-[0-9]{1,9}-[0-9]{1,3}$
```
The transport has `Proxy: nil`, as phase 1's Forgejo client does: it carries the client secret and bearer tokens (F20).

**The guard** allows exactly these eight requests, to the configured subscription and RG names, character for character:

| # | Request | Query |
|---|---|---|
| 1 | `POST login.microsoftonline.com/<tenant>/oauth2/v2.0/token`, form keys exactly `grant_type, client_id, client_secret, scope` | none |
| 2 | `GET management.azure.com/subscriptions/<s>/resourceGroups/<ci>/providers/Microsoft.ContainerInstance/containerGroups` | `api-version` [, `$skipToken`] |
| 3 | `GET …/containerGroups/<GroupName>` | `api-version` |
| 4 | `PUT …/containerGroups/<GroupName>`, body check below | `api-version` |
| 5 | `DELETE …/containerGroups/<GroupName>` | `api-version` |
| 6 | `POST …/resourceGroups/<ci>/providers/Microsoft.CostManagement/query`, body exactly `{type: ActualCost, timeframe: Custom, timePeriod, dataset: {granularity: Daily, aggregation: {totalCost: {name: Cost, function: Sum}}}}` | `api-version` |
| 7 | `GET …/resourceGroups/<guard>/providers/Microsoft.Logic/workflows/<sweeper>/runs` | `api-version`, `$top=10` |
| 8 | `GET …/resourceGroups/<ci>/providers/Microsoft.ContainerInstance/containerGroups/<name>/providers/Microsoft.Insights/metrics`, any valid group name (`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`), since foreign groups count in the burn (E17) | exactly `api-version=2023-10-01`, `metricnames=NetworkBytesTransmittedPerSecond`, `aggregation=Average`, `interval=FULL`, and one `timespan` of two RFC 3339 times |

**The PUT body check.** The guard refuses unless all of these hold:
- the body has unique keys, top-level keys ⊆ {`location`, `tags`, `properties`}, `location` is the region, and the `forge-ci` and `expires-at` tags are present;
- `properties` keys ⊆ {`containers`, `osType`, `restartPolicy`, `sku`}, with `Linux`, `Never` and `Standard`;
- there is exactly one container, its keys ⊆ {`name`, `properties`}, and its properties keys ⊆ {`image`, `command`, `resources`, `environmentVariables`};
- the image equals `Config.Image`, and `resources.requests` equals `model.Shape` of the class in the group name: 2/8 for `ci-small-…` and 4/16 for `ci-medium-…`. Otherwise a `small` row would price 117_000 against the sweeper's 233_000, breaking "the dispatcher's burn ≥ the sweeper's" (F20);
- the command has 6 elements: `[0]` is the entry path, `[1]` is at most `60m`, `[2]` is the class in the group name, `[3]` matches `model.ValidHandle`, `[4]` starts with `https://` and `[5]` is a UUID;
- `environmentVariables` is exactly one `{name: RUNNER_TOKEN, secureValue: <non-empty>}` with no `value` key.

- [ ] **Step 1: Write the failing tests** (an `httptest` fake ARM and token server that records every request).
  - `TestTokenRequestExact`: the form is exact, the token is cached until 5 min before `expires_in`, and an expired secret's `AADSTS` error text holds no secret.
  - `TestGuardAllowsTheEight`, and `TestGuardRefuses` (table, each refused unsent):
    - another subscription or RG, or the guard RG for anything but #7;
    - a name outside `GroupName`;
    - `…/containers/runner/exec`, `/logs`, `/start`, `/stop` or `/restart`;
    - `%2F` or `..`, an extra query key, or another `api-version`;
    - a `Host` header mismatch, or any redirect.
  - `TestGuardPutBody` (table): the builder fixture passes. Each of these is refused:
    - `RUNNER_TOKEN` carried in `value`, a second env var, or a duplicate key;
    - an `ipAddress`, `identity`, `subnetIds`, `volumes`, `diagnostics`, `imageRegistryCredentials` or `initContainers` key, or `priority: Spot`;
    - two containers, shape 2/16, `ci-small-…` with 4/16 or `ci-medium-…` with 2/8 (F20), another image or location, `restartPolicy: Always` or `osType: Windows`;
    - a missing `expires-at` tag, or a timeout of `61m`.
  - `TestGuardRefusesTokenInValue` (Review Focus 4): a body whose only env var has `value` set is refused, whatever its name.
  - `TestCostQueryBodyExact`, `TestDailyCostParse` (columns found by name, USD only), `TestSweeperRunsParse` (a run without `endTime` gives `End` zero).
  - `TestErrorClasses` (table):
    - 403 `AuthorizationFailed` → `ErrLocked`;
    - 409 `ContainerGroupQuotaReached`, 409 or 503 `ServiceUnavailable`, and Task 5's quota code → `ErrNoCapacity`;
    - 400 `RequestDisallowedByPolicy` → `ErrPolicy`;
    - 404 → `ErrNotFound`;
    - 400 `InvalidRequestContent`, 409 `Conflict` and any other 4xx → `ErrRejected` (F20: every 4xx gets a class, and none is ambiguous);
    - 429, 500 and a transport error → `ErrUnavailable`.
  - `TestTransportIgnoresProxy` (F20): with `HTTPS_PROXY` set, requests still dial the fake directly.
  - `TestGuardMetricsRead` (Review Focus 6, E17): request #8 passes for an own and a foreign group name. Each of these is refused unsent: another `metricnames`, `interval=PT1M`, an extra key, a second `timespan`, the guard RG, or the metrics path on anything but a container group. `Egress` parses the single data point, and an empty `timeseries` gives 0, not an error.
  - `TestGroupsFallbackGets` (F16): with a list fixture lacking `instanceView`, `Groups` issues one GET per `GroupName` group and none for other names, and fills `State`, `Terminated`, `ExitCode` and `Finish` from them.
  - `TestGroupsReportsDeletingAndForeign`: a `Deleting` group and a `ci-drill-…` group both come back, with `Provisioning` and the shape set.
  - `TestGroupsPaged` (`nextLink` on another path is refused), `TestCreateGroupGetFirst`, `TestDeleteAcceptsAsync`.
  - `TestErrorsNeverContainSecrets` (Review Focus 4): the client secret, the bearer token and a PUT body's `secureValue` are absent from every error's text, including errors that echo the request.
  - `TestLoadCredentials`: four files read; a missing or empty file's error names the path, never the content; `String()` and `GoString()` of `Credentials` redact the secret.
  - `TestNoOtherWritesInSource` (AST, kube's pattern): `MethodPut` only in `CreateGroup`, `MethodDelete` only in `DeleteGroup`, `MethodPost` only in `token` and `DailyCost`, plus the guard's functions. No `os/exec`, `net/http/httputil` or `reflect`; no `http.Client` or `http.Transport` literal outside `New` and `guarded`.
- [ ] **Step 2: Run them.** Expected: FAIL. **Step 3: Write the code** (kube's `guardTransport` shape, `scrub`, 30 s HTTP timeout). **Step 4: Run.** Expected: PASS. Commit: `azure: guarded ARM client: eight requests, one resource group`.
- [ ] **Step 5: Write `live_test.go`** (build tag `live`, skipped unless `CI_AZURE_LIVE=1` and `CI_AZURE_CREDS` names a directory). `TestLiveRoundTrip`:
  1. `CreateGroup("ci-small-0-0-1", body)`, with a nonce token and Task 4 Step 4's command.
  2. `Groups` shows it with its tags.
  3. `Group`'s raw answer, searched locally, lacks the nonce.
  4. `DeleteGroup`, then 404 within 5 min.

  The test also calls `DailyCost` for yesterday (any value, USD) and checks that `SweeperRuns` returns at least one finished run.

  `TestLiveLocked` runs only during Task 5's lock (F17): `Groups` and `CreateGroup("ci-small-0-0-2", body)` both return `ErrLocked`, and a list read as Chris afterwards shows no such group. The test prints only the error class and ARM's error code.
- [ ] **Step 6: STOP—ask Chris, then run it.** It's the service principal's first write; cost under a cent.
  - First swap in Task 4's real list fixture and Task 5's quota code, and re-run Steps 2–4 (F15).
  - A `python -I` script writes the four files from sso-secrets into `$SCRATCH/azure-creds/` (umask 077). Keep them until Task 5 Step 4 has run.
  - Run `CI_AZURE_LIVE=1 CI_AZURE_CREDS=$SCRATCH/azure-creds $GO test -tags live -run TestLiveRoundTrip ./internal/azure/`. Expected: PASS.
  - A 403 on `DailyCost` invokes E8's fallback: amend Task 2 and re-plan.

### Task 8: The paid acceptance suite, written first (`realliance/sso`)

**Gate:** (a).

**Files:**
- Create: `tests/forge/test_forge_ci_paid.py`
- Extend: `tests/ci_helpers.py` (`aci_group_names() -> list[str]` via `az container list -g rg-realliance-ci --query "[].name" -o tsv`; `paid_usage(owner_id)`)

Every test is skipped unless `CI_PAID_ACCEPTANCE=1` and the `ci-dispatch` StatefulSet is ready.

- [ ] **Step 1: Write the tests.**
  - `test_member_repo_runs_small` and `test_member_repo_runs_medium`: a throwaway member's public repo runs a `runs-on: small` probe, and then a `medium` one, to `success` within 8 min. Usage then shows one job per class, `used_usd` > `0.00`, and `reserved_usd` back to `0.00`.
  - `test_paid_job_shape`, in the job:
    - `nproc` is 2, MemTotal is 8 GiB ± 10 %, and `id -u` is 0;
    - no `KUBERNETES_SERVICE_HOST`; `RUNNER_TOKEN` not in `env`; `/run/ci/token` is mode `600`;
    - `curl -m 5 https://192.168.0.7` fails;
    - `curl -m 5 -H Metadata:true 'http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://management.azure.com/'` returns no `access_token`;
    - `git ls-remote` works against the forge and against GitHub.
  - `test_paid_nothing_outlives_the_job`: 120 s after the run ends, no `ci-small-<job>-*` group and no runner. A probe cancelled before pickup also leaves nothing within the 6-min pickup timeout plus 2 polls.
  - `test_owner_paid_cap_holds` (caps owner `forgejo-admin`, $0.15): first, read usage. If `remaining_usd` < `0.08`, assert that A holds `owner-limit` and stop. Otherwise:
    1. A runs and sleeps 90 s.
    2. B shows hold `owner-limit` while A runs (two reservations, 0.156, exceed 0.15).
    3. After A settles, B launches if `Decimal(remaining_usd) ≥ Decimal("0.078")`, and otherwise stays held. The test compares decimals against the 78_000 µ$ reservation, never a rounded `"0.08"` string (F21).
  - `test_label_combos_paid`: `[small, docker]` and `large` wait with no hold row; `[small, medium]` holds `label`; none ever runs.
  - `test_usage_paid_numbers`: `used_usd + reserved_usd + remaining_usd == limit_usd` exactly, as decimals. That holds because usage computes `remaining` from the rounded cents (Task 10, F21). `classes.small.jobs` counts distinct jobs. `global.paid` is `open` or `near-cap`.
  - `test_runner_credential_not_in_group_definition` (Review Focus 4): the job prints `sha256(/run/ci/token)` and sleeps 60 s. Meanwhile the test fetches `az container show -g rg-realliance-ci -n <name>` JSON and the group's Activity Log (`az monitor activity-log list -g rg-realliance-ci --offset 1h --query "[?contains(resourceId,'<name>')]"`), both saved locally with mode 0600 (F14). It hashes every 40-hex substring of both and finds no match. The positive control finds the hash in the job's own log.
  - `test_dispatcher_secrets_not_in_group` (F21): the job prints the `sha256` of every token-shaped run (`[A-Za-z0-9._~+/=-]{20,}`) in each env value and each file under `/run`, `/etc/ci-class` and `/root`. That catches a secret inside a larger file. The hashes are matched locally against `CI_DISPATCH_FORGEJO_TOKEN`, `CI_DISPATCH_USAGE_TOKEN`, `CI_DISPATCH_AZURE_CLIENT_SECRET` and `FORGEJO_ADMIN_TOKEN`: no match. A positive control plants a nonce in a file and finds it.
- [ ] **Step 2: Run.** Expected: all skipped. The secret guard (`tests/test_no_secret_in_asserts.py`) is green. **Step 3: Commit signed; open a draft PR.**

### Task 9: The 24/7 route for the money alerts

**Gate:** (a) to write. Its push is a deploy of `prometheus-stack-config`.

**Files:** modify `cluster/prometheus-stack/alertmanager-config.secret.yaml`, and regenerate `alertmanager-config.yaml`.

- [ ] **Step 1: STOP—ask Chris.** If `forge-urgent` doesn't exist, he supplies the webhook file for a channel both Owners see (forge-bot Task 15 Step 1), and approves editing the sealed config.
- [ ] **Step 2: Route tests first** (forge-bot Task 15 Step 2's scratch `amtool` method). New rows:
  - `alertname=CIDispatchSpendHigh severity=critical`, `CIDispatchLocked`, `CIDispatchOrphanCompute` and `CISweeperStale` → `forge-urgent`;
  - `CIDispatchDrift severity=warning` and `CIDispatchPaidNotReady severity=warning` → `homelab-default`.

  forge-bot's rows stay as they are. Expected: the new rows FAIL.
- [ ] **Step 3: Edit, scripted, never opened** (forge-bot Task 15's method, F28). A scratch `python -I` script under `umask 077` does the whole edit:
  - loads the decrypted `alertmanager-config.secret.yaml`;
  - adds `|CIDispatch(SpendHigh|Locked|OrphanCompute)|CISweeperStale` to `forge-urgent`'s route regex, or first creates the receiver and route in forge-bot's exact shape, reading the webhook from Chris's file;
  - writes the file back, and prints only the receiver names and route matchers.

  Re-run the route tests. Expected: all pass.
- [ ] **Step 4: Reseal, Gate G before, push, `Monitor`** the `prometheus-stack-config` sync, **then Gate G after** (F28). **Step 5: Drill (Chris, outside weekday hours).** Send `amtool alert add alertname=CIDispatchLocked severity=critical drill=true --end=+10m`; both Owners confirm receipt. Ledger the time, and tell forge-bot's executor through Chris.

## Group (b): after phase 1 Task 14

Every task: `$GO vet ./... && $GO test -race -p=4 ./...` green, then a signed commit and a push to `v2`. The merge comes first (F5): `git -C ~/Repos/ci-dispatch-v2 merge -S origin/main`, a signed merge commit, never a rebase or force-push. Expected: no conflict, since Tasks 6 and 7 add files only. After the merge, re-read the phase 1 interfaces Global Constraints lists as they now stand on `main`: the held-undo back-off boundary, `HeldMinutes`, `Failed`, and the error-undo back-off (F22, F23). Ledger any difference before Task 10.

### Task 10: Ledger schema v2, paid accounting, usage paid numbers

**Gate:** (b).

**Files** (F29):
- `git mv internal/ledger/schema.sql internal/ledger/schema_v1.sql`, byte-identical. Create `internal/ledger/schema_v2.sql`. Modify `internal/ledger/ledger.go` (stepwise `migrate`, both embedded).
- Modify: `internal/limits/limits.go` (`CostOf`), `internal/usage/usage.go`, `cmd/ci-dispatch/main.go` (the `usage.New` signature).
- Create: `internal/ledger/money_test.go`. The migration test builds a v1 database itself from `schema_v1.sql`; no binary fixture.
- Extend: `internal/usage/usage_test.go`, and the phase 1 usage tests that call `usage.New`.

**Interfaces (produces):**
```go
// limits: the money formulas (F12, E17)
func CostOf(hourly Micro, d time.Duration) Micro // d rounded UP to whole seconds; ceil(secs × hourly / 3600) in int64; d ≤ 0 → 0
func EgressCost(bytes int64) Micro               // ceil(bytes × 87_000 / 1e9); bytes ≤ 0 → 0
const CeilingPeriod Micro = 80_000_000           // was 100_000_000 (Chris, 2026-11-05: the cache host); CeilingHour stays 600_000
const EgressMicroPerGB Micro = 87_000            // plan's call (E17)
const EgressCapBytes int64 = 10_000_000_000      // plan's call (E17)
// ledger: Launch gains HourlyMicro int64 and StoppedAt time.Time.
// Reserve refuses a negative ReservedMicro or HourlyMicro (phase 1 N3 carry, F12).
func (l *Ledger) SetStopped(ctx context.Context, id int64, at time.Time) error // first stop sighting or the container's finish time; a later call keeps the earlier value (F11)
func (l *Ledger) SetEgress(ctx context.Context, id int64, bytes int64) error  // the group's total so far; keeps the max of stored and given (E17)
// Every settle (End, Reap, UndoHeld) adds EgressCost(egress_bytes) to charged_micro, outside the compute cap (E17)
// End and Reap read compute_at, hourly_micro, reserved_micro and stopped_at inside their BEGIN IMMEDIATE.
// They set charged_micro = min(reserved, CostOf(hourly, stop − compute_at)) in Go, where stop = stopped_at if set, else at; 0 if compute_at is 0.
func (l *Ledger) UndoHeld(ctx context.Context, id int64, at time.Time, chargedMicro limits.Micro) error
    // F1 (Chris): caps at reserved_micro and refuses a negative charge; tiny and ErrNoCapacity pass 0.
    // Tries and Held still key on state undone + outcome held, so a charged held undo is still no try.
type Money struct{ Used, Reserved limits.Micro } // Used: settled charged; Reserved: active rows' reserved_micro + EgressCost of their egress so far (E17)
func (l *Ledger) PaidCommitted(ctx context.Context, from, to time.Time) (global Money, owners map[int64]Money, err error) // by created (F8: used and reserved apart)
func (l *Ledger) PaidUse(ctx context.Context, owner int64, from, to time.Time) (PaidUse, error) // shares one SQL helper with PaidCommitted (F13)
type PaidUse struct{ Money; Running int; Classes map[model.Class]ClassUse }
type ClassUse struct{ Jobs, Minutes int; Used limits.Micro }
func (l *Ledger) SetPrices(ctx context.Context, hourly map[model.Class]limits.Micro, at time.Time) error
func (l *Ledger) Prices(ctx context.Context) (map[model.Class]limits.Micro, time.Time, error) // empty and zero time if never set
func (l *Ledger) SetDrift(ctx context.Context, day string, ledgerMicro, azureMicro limits.Micro, at time.Time) error
func (l *Ledger) Drift(ctx context.Context, from, to time.Time) ([]DriftRow, error) // F25: the period sum, and the watcher's restart check
type DriftRow struct{ Day string; Ledger, Azure limits.Micro }
func (l *Ledger) DayCharged(ctx context.Context, day time.Time) (limits.Micro, error) // paid charged_micro by the UTC day of compute_at
```
There is no ledger `Burn` or `PaidRunning` (F13). The loop sums `HourlyMicro` and counts paid rows over the `Active()` it already reads (Task 14).

Schema v2 (I1: v1 with `launches_key` is what phase 1 deployed, so `schema_v1.sql` stays byte for byte as v0.1.0 shipped it):
- `ALTER TABLE launches ADD COLUMN hourly_micro INTEGER NOT NULL DEFAULT 0`;
- `ALTER TABLE launches ADD COLUMN stopped_at INTEGER NOT NULL DEFAULT 0` (F11);
- `ALTER TABLE launches ADD COLUMN egress_bytes INTEGER NOT NULL DEFAULT 0` (E17);
- `CREATE INDEX launches_created ON launches(created)` and `CREATE INDEX launches_compute_at ON launches(compute_at)`, for the global period and day sums;
- tables `prices(class PRIMARY KEY, hourly_micro, fetched_at)` and `drift(day PRIMARY KEY, ledger_micro, azure_micro, at)`;
- `UPDATE meta SET schema_version = 2` (an UPDATE, not an INSERT; F29).

`migrate` runs v1 then v2 on an empty file, and v2 alone on a v1 ledger, each step in one `BEGIN IMMEDIATE`. v0.1.0 refuses v2 (E13).

**Usage:**
- `paid` comes from `PaidUse`. Usage rounds `used` and `reserved` half-up to cents first, then computes `remaining_usd = max(0, limit − used − reserved)` from those cents, so the three strings always add up to the limit (F21).
- `global.paid` comes from a new `New` argument `paidState func() string` (`open`, `near-cap` or `closed`). Until Task 15 wires `lp.PaidState`, `main` passes `func() string { return "closed" }`.

- [ ] **Step 1: Write the failing tests.**
  - `TestSchemaV1Frozen`: `schema_v1.sql`'s sha256 equals v0.1.0's `schema.sql` (tag `v0.1.0`, not any earlier commit), `launches_key` included.
  - `TestPaidQueriesUseIndexes`: `EXPLAIN QUERY PLAN` for `PaidCommitted`, `PaidUse` and `DayCharged` shows `SEARCH` with an index and no `SCAN launches` (I1's pin, applied to the new queries; F13).
  - `TestMigrateV1ToV2`: a database built from `schema_v1.sql` with launch, hold and config rows opens under v2. Every row survives, `launches_key` is still there, `schema_version` is 2 with one `meta` row, and a second `Open` changes nothing.
  - `TestCostOf` (table, F12): (117_000, 10 min) → 19_500; (117_000, 10 min + 1 ns) → 19_533; (117_000, 1 s) → 33; (233_000, 55 min) → 213_584; (233_000, 48 h) → 11_184_000, with no overflow; (117_000, 0) → 0.
  - `TestPaidChargeCappedAtReservation`: `small` reserved 78_000 and ended 50 min after `compute_at` charges 78_000, for `End` and `Reap` alike. `Undo` charges 0.
  - `TestChargeStopsAtStoppedAt` (F11): `compute_at` t, `SetStopped` at t + 3 min, then a second `SetStopped` at t + 4 min that changes nothing, then `End` at t + 4 min 30 s. The charge is 5_850, not 8_775.
  - `TestUndoHeldCharges` (F1):
    - `UndoHeld(id, at, 11_700)` (6 min of `small`) stores 11_700;
    - `Tries` is unchanged and `Held` rises by 1;
    - a charge above the reservation is capped, and a negative one is refused;
    - `UndoHeld(…, 0)` behaves as phase 1's did.
  - `TestReserveRefusesNegativeMoney`.
  - `TestCeilingPeriodIs80` (Chris's cap change): `limits.Parse` takes `global.paid_usd_per_period: 80` and refuses `80.01` and `100` with `ErrAboveCeiling`. Phase 1's `TestCeilings` rows that used 100 and 101 move to 80 and 80.01. That's the one phase 1 test this task edits, and it says so.
  - `TestEgressCharge` (Review Focus 6, E17):
    - `EgressCost(10_000_000_000)` = 870_000 and `EgressCost(1)` = 1;
    - a `small` row with `SetEgress` 2_000_000_000, 1_000_000_000 (ignored, lower) and an `End` at 10 min charges 19_500 + 174_000 = 193_500. The egress part isn't capped by the 78_000 reservation;
    - while the row is active, `PaidCommitted`'s `Reserved` is 78_000 + 174_000;
    - an `UndoHeld` row with egress charges its egress too.
  - `TestPaidChargedToLaunchPeriod`: a launch created 2026-11-30T23:59:59Z and ended in December counts in November's `PaidCommitted`, and never in December's.
  - `TestPaidCommittedSplitsUsedAndReserved` (F8): an ended row counts in `Used` only and an active row in `Reserved` only, both globally and per owner.
  - `TestPaidUseAgreesWithCommitted` (F13): over random rows, the sum of `PaidUse` over owners equals `PaidCommitted`.
  - `TestPricesRoundTrip`, `TestDriftRange`, and `TestTinyRowsCarryNoMoney` (tiny's `hourly_micro` and `charged_micro` stay 0).
  - `TestUsagePaidNumbers`: rows built to the spec's example give exactly `"limit_usd": "10.00", "used_usd": "3.42", "reserved_usd": "0.21", "remaining_usd": "6.37"`, plus `concurrent.running` and `classes.small {jobs, minutes, used_usd}` from the golden fixture.
  - `TestUsageCentsAddUp` (F21): used 5_000 µ$ and reserved 9_995_000 µ$ against $10 give `"0.01"`, `"10.00"` and `"0.00"`, which sum to at least the limit and never below zero. Over random amounts, `used + reserved + remaining == limit` whenever `remaining > 0`.
  - `TestUsageRemainingNeverNegative`, and `TestUsageGlobalPaid` (each of the three strings from `paidState`).
- [ ] **Step 2: Run.** Expected: FAIL. **Step 3: Write the code** (Go integer arithmetic only, inside each write's transaction; no money in SQL). **Step 4: Run** the new tests and every phase 1 ledger and usage test. Expected: PASS. Commit: `ledger: schema v2, paid money in micro-dollars; usage: paid numbers`.

### Task 11: The backend seam (no behavior change) and the name grammar

**Gate:** (b).

**Files:**
- Create: `internal/loop/backend.go`, `tinybackend.go`, `backend_test.go`
- Modify: `internal/loop/loop.go`, `reap.go`. The name grammar is Task 7's `internal/model/names.go`.

**Interfaces (produces):**
```go
// consumed from Task 7: model.ComputeName(c, k, try), model.NameRE(classes...)
// loop
type Compute struct {
    Name string; Created, ExpiresAt time.Time
    Own bool                                          // the name is in this backend's grammar; reaper and reconcile touch Own items only
    Suspended, Running, Ran, Stopped, Broken, Deleting bool
    Finish time.Time                                  // ACI: the container's finish time when known (F11); tiny: zero
    Hourly limits.Micro                               // ACI: prices.ShapePrice of the group's shape; tiny: 0 (F2)
}
type Listing struct{ Items map[string]Compute; At time.Time } // every listed object, own or not; At = the backend's clock BEFORE the request (F4)
type Spec struct {
    Key model.Key; Try int; Class model.Class; OwnerID int64; Name, Handle string
    Reg forgejo.Registration; Created, ExpiresAt time.Time
    HourlyMicro limits.Micro                          // 0 for tiny
}
var ErrNotCreated = errors.New("loop: compute definitely not created") // wraps every refusal after which nothing exists (F3)
type Backend interface {
    Name() string                                     // the ledger's backend and the metrics label
    List(ctx context.Context) (Listing, error)
    Create(ctx context.Context, s Spec) error         // undoes its own partial steps (F3); see below
    Delete(ctx context.Context, name string) error    // absent is success
    SettleOnAbsence() bool                            // tiny false, ACI true (F2)
    Timing(c model.Class) (hardStop, pickup time.Duration)
}
type Option func(*Loop)
func WithBackend(classes []model.Class, b Backend) Option
func New(cfg config.Config, f Forge, k Kube, l *ledger.Ledger, lim Limits, m *metrics.Metrics, d *Decisions, opts ...Option) *Loop
var ownName  = model.NameRE(model.Tiny, model.Small, model.Medium) // runners (F37)
var tinyName = model.NameRE(model.Tiny)                            // Kubernetes Jobs
```
- **`Create`'s three answers** (F3):
  - `nil`.
  - An error wrapping `ErrNotCreated`: nothing exists, so the loop undoes at once with no `Delete`. Tiny returns it once its own partial-step cleanup succeeded, which today's `undoLaunch` does. ACI returns it for definite refusals (E10).
  - Any other error: the compute may exist. The loop calls `Delete`; tiny then undoes, and ACI waits for absence.
- **The tiny backend** wraps `Kube`:
  - `Create` = `CreateJob` (suspended), `CreateSecret(tiny.SecretManifest)`, `ResumeJob`, in D17's order;
  - `Delete` = `DeleteJob`, then a best-effort `DeleteSecret`;
  - `List` filters Jobs with `tinyName`, never `ownName`. `At` is read before the Jobs request.
- **Two settle models** (F2):
  - `SettleOnAbsence() == false` (tiny): a successful `Delete` settles, as phase 1 does.
  - `true` (ACI): the loop records `deleteSent[name]` and settles only on a `Listing` whose `At` > max(`ComputeAt`, `deleteSent[name]`) and that lacks the name. That covers every path: finished, gone before pickup, pickup timeout, held undo, expired, leftover, ambiguous create.
  - A `registered` row whose compute is present is adopted unless it is `Deleting` or `Broken`.
- **Names:** `claimed()` and `reconcileOne` derive names with `model.ComputeName(x.Class, x.Key, x.Try)`, never `tiny.Name` (F37). A paid crash leftover has `RunnerID` 0, so its name is the only handle.
- **Read failures:** a failed `List` of backend B skips B's expire, reap, reconcile and launches. When every backend's `List` failed, the step skips the rest, as phase 1's Kubernetes failure does.
- **`ReadOK`:** a failed Kubernetes or Forgejo read clears it, as in phase 1. A failed **Azure-only** read does not (F39), so `CIDispatchStale` doesn't fire through a lock that lasts days. Azure has its own signals: `errors_total{api="azure"}`, `azure_locked` and `CIDispatchPaidNotReady`.

- [ ] **Step 1: Write the failing tests.**
  - `TestComputeNameMatchesTiny`: for 100 random keys and tries, `model.ComputeName(Tiny, k, t) == tiny.Name(k, t)`.
  - `TestOwnNameCoversPaid`: `ci-small-1-2-3` and `ci-medium-1-2-3` match; `ci-large-1-2-3`, `ci-drill-…`, `ci-probe-…` and `ci-tiny-1-2` don't.
  - `TestGrammarHasOneSource` (F37): a static test confirms that `ownName`, `tinyName` and `azure.GroupName` are built by `model.NameRE`, and that no other regexp literal in the module starts `^ci-`.
  - `TestReaperRunnerGrammarCoversPaidClasses`: an orphan `ci-small-…` ephemeral instance runner is reaped after 10 min, and a repo-scoped one never.
  - `TestTinyListIgnoresPaidNames`: a Kubernetes Job named `ci-small-1-1-1` in `ci-tiny` is neither listed, reaped nor adopted.
  - `TestSettleOnAbsenceSeam` (fake backend with `SettleOnAbsence() == true`, F2, F4):
    - an accepted `Delete` settles nothing;
    - a `Listing` with `At` before `deleteSent` that lacks the name settles nothing;
    - the next `Listing` read after it settles, with that `At` as the end;
    - a `registered` row whose compute is `Deleting` isn't adopted;
    - non-`Own` items are never reaped or adopted.
  - `TestCreateNotCreatedSkipsDelete` (F3): an error wrapping `ErrNotCreated` gives `Undo` with no `Delete`, and any other error gives `Delete` first.
  - `TestAzureOnlyReadFailureKeepsReadOK` (F39): a fake second backend failing `List` leaves `ReadOK` true and the success timestamp moving.
  - **Regression:** every phase 1 test in `internal/loop` passes **unedited**, `TestStartupReconcileAfterCrashMidLaunch` and `TestUndoAtEveryStep` included. One exception is allowed (F3): if `TestUndoAtEveryStep`'s CreateJob row can only hold by gaining a harmless trailing `DeleteSecret`, that row alone may change. Step 4 then names it.
- [ ] **Step 2: Run.** Expected: the new tests FAIL. **Step 3: Refactor.** **Step 4: Run** the new tests, the full suite, and `$GO test -race -count=20 ./internal/loop/`. Then `git diff origin/main -- internal/loop/*_test.go` shows only the new test file, plus at most that one row. Expected: PASS. Commit: `loop: a backend seam with two settle models; tiny behind it, unchanged`.

### Task 12: Paid gates

**Gate:** (b).

**Files:** modify `internal/gate/gate.go`, `gate_test.go`.

**Interfaces (produces):** `Inputs` gains:
```go
PaidRunning map[int64]int                 // active paid launches per owner, counted by the loop over Active() (F13)
Committed   map[int64]limits.Micro        // per owner, this period: Used + Reserved from ledger.PaidCommitted
GlobalCommitted limits.Micro
Burn        limits.Micro                  // Σ HourlyMicro over Active() paid rows + Compute.Hourly of listed groups no active row claims (Task 14, M6)
Price       map[model.Class]Price         // absent: no price
PaidReady   map[model.Class]bool          // prices < 7 d old, the backend listed this step, not locked
type Price struct{ Hourly, Reserve limits.Micro }
```
The paid path replaces the stub. Its order follows the spec's gate table:
1. after back-off, a `docker` job waits `class-off` (no VM backend in phase 2);
2. `PaidRunning + planned ≥ min(owner.PaidConcurrent, MaxPaidConcurrent)` → wait `owner-busy`;
3. `Burn + plannedBurn + Hourly > min(Global.PaidPerHour, CeilingHour)` → wait `class-full`;
4. `Committed[o] + plannedOwner + Reserve > owner.PaidPerPeriod` → hold `owner-limit`;
5. `GlobalCommitted + plannedGlobal + Reserve > min(Global.PaidPerPeriod, CeilingPeriod)` → hold `global-cap`;
6. `!Ready || !PaidReady[c] || no price` → wait `not-ready`;
7. otherwise launch.

**Fair order** (F22): owners sort by phase 1's key, `TinyUsed + HeldMinutes` ascending (`Inputs.HeldMinutes`, the order-only input from phase 1's I3 fix). Ties go by `Committed` ascending, then the oldest job.
- Paid held undos do **not** feed `HeldMinutes`. They now charge money (F1), so they count through `Committed`.
- Task 10's gate (b) merge re-reads the key as it stands on `main`, and this task uses that.

- [ ] **Step 1: Write the failing tests.**
  - `TestPaidGateOrder` (table, each gate failing alone). Values:
    - two running gives `owner-busy`;
    - a burn of 483_001 plus 117_000 gives `class-full`;
    - committed 9_922_001 plus 78_000 against $10 gives `owner-limit`;
    - global 79_922_001 plus 78_000 gives `global-cap` (the $80 ceiling);
    - a stale price, a missing price, `PaidReady` false and `docker` each give their reason.
  - `TestBurnAtCeilingExactly`: a burn of 483_000 plus 117_000 equals 600_000 and launches.
  - `TestPaidCeilingClampsConfig` (Review Focus 1): `Global.PaidPerHour` 900_000 and `PaidPerPeriod` 150_000_000, set directly to bypass `Parse`, still bind at 600_000 and 80_000_000.
  - `TestPaidPlanCountsOwnLaunches` (Review Focus 1): six owners each with a waiting `small` and a burn of 0 give exactly five launches (585_000); the sixth waits `class-full`.
  - `TestOwnerLimitAboveGlobalStillBound` (carry M3).
  - `TestFairOrderPaid` (F22): owner A (tiny 0, held 30 min) sorts after owner B (tiny 20). Two owners tied on the key sort by `Committed`. A paid held undo moves `Committed`, never `HeldMinutes`.
  - `TestTinyUnaffected`: every phase 1 gate test passes unedited.
- [ ] **Step 2: Run.** Expected: FAIL. **Step 3: Write the code** (`Plan` stays pure). **Step 4: Run.** Expected: PASS. Commit: `gate: paid gates in the spec's order; the ceilings bind`.

### Task 13: Paid config, the ACI body builder, metrics

**Gate:** (b).

**Files:**
- Modify: `internal/config/config.go` and its test, `internal/metrics/metrics.go` and its test
- Create: `internal/aci/{aci.go,aci_test.go,imports_test.go}`

**Interfaces (produces):**
```go
// config: new sections
Azure struct{ Files, ResourceGroup, Region, GuardResourceGroup, Sweeper string; Poll time.Duration } // poll default 30s
Paid  struct{ Image string; PriceMaxAge time.Duration; Classes map[model.Class]PaidClass }         // max age default 168h
type PaidClass struct{ RunnerTimeout, HardStop, Pickup time.Duration }
// aci (pure: no network import)
const EntryPoint = "/usr/local/bin/ci-class-entry"
const PullAllowance = 5 * time.Minute
const MaxHardStop = time.Hour
type Spec struct {
    Key model.Key; Try int; Class model.Class; OwnerID int64
    Handle, UUID, PublicURL, Image, Region string
    Created, ExpiresAt time.Time; HardStop time.Duration; Token secret.Value
}
func Body(s Spec) (secret.Value, error)   // the PUT body; shape from model.Shape (F19); refuses a bad handle, a class without a shape, a hard stop > MaxHardStop
```
`internal/aci` imports `internal/model` and `internal/secret` only: no `internal/prices` and no network package (F38).
- **`config.Load`** (F9). The key shape is `paid.classes.<c>`, as in `Paid.Classes`; Task 18's `config.yaml` uses the same shape:
  ```yaml
  paid:
    image: ghcr.io/christopherjmiller/forgejo-runner-class:sha-…@sha256:…
    price_max_age: 168h
    classes:
      small:  {runner_timeout: 30m, hard_stop: 40m, pickup: 6m}
      medium: {runner_timeout: 45m, hard_stop: 55m, pickup: 6m}
  ```
  - It accepts `small` and `medium` in `dry-run` or `on` only with `azure` set. Defaults fill missing `paid.classes.<c>` fields, so the block is optional. `large` other than `off` gets "no backend in phase 2".
  - It refuses `hard_stop > 1h`, `hard_stop − PullAllowance ≤ runner_timeout`, `pickup ≥ hard_stop`, an image without `@sha256:`, and `poll` outside 10 s–5 min.
  - Defaults: `small` {30m, 40m, 6m} and `medium` {45m, 55m, 6m}.
- **Metrics:**
  - backends `{small, aci}` and `{medium, aci}`; reap reasons `orphan-group` and `egress-cap` (E17); orphan kind `group`; APIs `azure`, `prices`;
  - gauges `ci_dispatch_burn_usd_per_hour`, `ci_dispatch_period_usd{kind="used|reserved|cap"}`, `ci_dispatch_azure_locked` and `ci_dispatch_paid_ready{class}`, present from the start. Task 14 sets them every tick (F8);
  - `ci_dispatch_price_age_seconds` is **absent until a price exists**, so a 0 can't read as "fresh". `ci_dispatch_sweeper_last_run_timestamp` and `ci_dispatch_drift_ratio{window="day|period"}` are absent until known. The watcher sets all three (F8).

- [ ] **Step 1: Write the failing tests.**
  - `TestLoadPaid` (table): each rule above; the defaults; a phase 1 file still loads. It also loads `testdata/site-config.yaml`, a copy of Task 18's `config.yaml`, kept by hand so the two repos can't drift unseen (F9). A file using the flat `paid.small` key fails strict decoding.
  - `TestBodyShape`: decoded, the body has:
    - location `westus3` and one container named `runner` with the configured image;
    - requests 2/8 (`small`) or 4/16 (`medium`);
    - `osType` Linux, `restartPolicy` Never, `sku` Standard;
    - none of `ipAddress`, `subnetIds`, `identity`, `volumes`, `diagnostics`, `imageRegistryCredentials`, `initContainers` or `priority`.
  - `TestBodyCommand`: exactly `[EntryPoint, "35m", "small", h, "https://git.realliance.net", uuid]`, and `"50m"` for `medium`.
  - `TestBodyTokenOnlySecure` (Review Focus 4): `environmentVariables == [{name: RUNNER_TOKEN, secureValue: token}]`; no `value` key; the token occurs once in the body.
  - `TestBodyTags`: the seven tags, `expires-at` = `ExpiresAt` in RFC 3339 UTC.
  - `TestBodyRedacts` (Review Focus 4): `%v`, `%+v`, `%#v`, `%x` and `slog` of the returned value print no token.
  - `TestBuiltBodiesPassTheGuard`, in `internal/azure` (F38, like `kube/tiny_test.go`): `aci.Body` → `azure`'s guard, for both classes and boundary hard stops.
  - `TestNoNetworkImports`: `internal/aci`'s full dependency set (`go list -deps`) holds no `net`, `net/http`, `os/exec` or `internal/prices`.
  - `TestMetricsFixedLabels` and `TestMetricsTextFormat`, extended.
  - `TestPaidPairsCounted` (F8): `Launch(Small, "aci", Launched)` and `Launch(Medium, "aci", Launched)` move `ci_dispatch_launches_total`. An unknown pair is dropped silently today (preflight I16), so a typo in the backend name would hide paid launches.
- [ ] **Step 2: Run.** Expected: FAIL. **Step 3: Write the code.** **Step 4: Run.** Expected: PASS. Commit: `config, aci, metrics: paid classes, the container group body, money gauges`.

### Task 14: ACI in the loop

**Gate:** (b).

**Files:** create `internal/loop/acibackend.go`, `aci_test.go`, `fakeazure_test.go`; modify `internal/loop/loop.go` (paid reservation in `launch`, gate inputs, money gauges, `PaidState`, prices).

**Interfaces (produces):**
```go
type Azure interface {
    Groups(ctx context.Context) ([]azure.Group, error)
    CreateGroup(ctx context.Context, name string, body secret.Value) error
    DeleteGroup(ctx context.Context, name string) error
}
func NewACIBackend(a Azure, cfg config.Config, clock func() time.Time) Backend // SettleOnAbsence() == true
func (lp *Loop) PaidState() string    // "closed": no paid class on, locked or not ready; "near-cap": committed ≥ 80 % of the cap; else "open"
func (lp *Loop) SetPrices(p map[model.Class]limits.Micro, at time.Time) // the watcher's hand-off; Start re-reads ledger.Prices
type Egress struct{ Total int64; RateBytesPerSec float64 }           // E17
func (lp *Loop) EgressTargets() map[string]time.Time                    // every group in the newest listing → its Created, for the watcher
func (lp *Loop) SetEgress(e map[string]Egress, at time.Time)            // the watcher's hand-off, every 60 s
```
`PaidState` and `SetPrices` go through one atomic snapshot (prices, locked, ready), so the watcher, the usage server and the loop never see a torn view (F23).

- **List** is cached for `azure.poll`. `Listing.At` is the backend clock read **before** the `Groups` request (F4). `Items` holds every group: `Own` = `azure.GroupName` matches, `Hourly` = `prices.ShapePrice(CPU, MemoryGB)`.
  - `Running` = state `Running`; `Ran` = terminated with exit 0; `Stopped` = state `Stopped`, `Succeeded` or `Failed`; `Broken` = provisioning `Failed`; `Deleting` = provisioning `Deleting`;
  - `Finish` = the container's finish time; `Created` = tag `created-at`, else first seen; `ExpiresAt` = tag `expires-at`.
- **A paid launch:**
  - It reserves with `ReservedMicro = CostOf(hourly, hardStop)`, `HourlyMicro = hourly` and `ReservedMinutes = hardStop` in minutes, then registers, `SetRegistered`, `CreateGroup(aci.Body)`, `SetCreated`.
  - `hourly` is `max(stored price, prices.Floor(c))`, re-applied on every read of stored prices (F23).
  - `ComputeAt`, and the ambiguous-create mark, are the clock read **after** `CreateGroup` returns (F4).
- **Stop time** (F11): the first reconcile that sees a claimed group `Stopped`, or with a `Finish`, calls `ledger.SetStopped(id, Finish or Listing.At)`. Charges stop there; the reservation still waits for absence.
- **Error handling** (E11, F23), with ACI's `Create` wrapping definite refusals in `ErrNotCreated` (F3):

  | `CreateGroup` answer | Undo | Try | Back-off | Charge |
  |---|---|---|---|---|
  | `ErrNoCapacity` | `UndoHeld(id, at, 0)` at once | no | `gate.HeldBackoffAfter` | 0 |
  | `ErrLocked` | `Undo(id, "locked")` at once; `locked` set until a good `List` | no (`Tries` skips `locked`) | none: `not-ready` gates relaunches | 0 |
  | `ErrPolicy`, `ErrRejected`, `ErrRefused`, `ErrExists` | error undo at once (no `DeleteGroup`; `ErrExists` deletes nothing) | yes | phase 1's error back-off (`gate.BackoffUntil`) | 0 |
  | `ErrUnavailable` (ambiguous) | `DeleteGroup`; row stays `registered`; error undo on absence | yes | error back-off | 0 |

- **Pickup-idle held undo, paid** (F1, Chris): the runner declared and reads idle at the 6-min pickup timeout. The loop calls `DeleteGroup`, waits for absence, then `UndoHeld(id, at, limits.CostOf(HourlyMicro, stop − ComputeAt))`, with no try. The key backs off on `gate.HeldBackoffAfter`: 1, 2, 5, 10, 10, then 60 min.
- **Burn for the gate** (F13, M6) = Σ `HourlyMicro` over `Active()` paid rows, plus `Compute.Hourly` of each item in the newest successful listing that no active row claims, own names or not. `PaidRunning` is counted over the same `Active()`. Without a successful list since start, paid classes are `not-ready`.
- **Egress** (E17):
  - an **own** group whose `Total` ≥ `limits.EgressCapBytes` is deleted, with reap reason `egress-cap`, and settles on absence;
  - each claimed row gets `ledger.SetEgress(id, Total)`;
  - the burn adds `RateBytesPerSec × 3600 × EgressMicroPerGB / 1e9` µ$ an hour for every listed group;
  - `PaidReady` is false unless `SetEgress` arrived within the last 5 min. Foreign groups over the cap are the sweeper's to delete.
- **Money gauges, every tick** (F8):
  - `ci_dispatch_burn_usd_per_hour` = that burn;
  - `ci_dispatch_period_usd{kind="used"}` and `{kind="reserved"}` from `PaidCommitted`'s global `Money`, and `{kind="cap"}` = `min(Global.PaidPerPeriod, CeilingPeriod)`;
  - `ci_dispatch_azure_locked`, and `ci_dispatch_paid_ready{class}`.
- **Reconcile** follows Task 11's absence model (E10). A `Stopped` group is deleted, and its row settles once a later list, read after the DELETE, lacks it.
- **Decision log:** a paid launch's line carries `reserved_usd`, and its settled line `charged_usd` (spec, "Logs").

- [ ] **Step 1: Write the failing tests** (the fake Azure records calls and can fail any of them; its `Deleting` phase lasts at most one poll, F39; fake clock):
  - `TestACILaunchOrder`: reserve (78_000 µ$, 40 min) → register → set-registered → create-group → set-created, with the body from `aci.Body`. `ComputeAt` is the clock after `CreateGroup` returned.
  - `TestACIErrorOutcomes` (table, the error table above): for each answer, the ledger call, `Tries` before and after, the back-off end from `gate.BackoffUntil`/`HeldBackoffAfter`, whether `DeleteGroup` was sent, and charge 0. `ErrUnavailable` gives `DeleteGroup` and leaves the row `registered`. A later list showing it `Deleting` changes nothing, the first list without it gives the error undo, and a failed `DeleteGroup` leaves the row active.
  - `TestACIHeldUndoCharges` (F1): a pickup-idle group, created 6 min before, is deleted. On the list that lacks it, `UndoHeld` charges `CostOf(117_000, 6 min)` = 11_700, `Tries` stays 0, and the next wait is 1 min. After the key's sixth held undo the wait is 60 min.
  - `TestACINoCapacityBacksOff`: `ErrNoCapacity` gives `UndoHeld(…, 0)` with no try, then `Wait backoff` for 1 min, on the same schedule.
  - `TestPaidNeverSettlesOnAcceptedDelete` (Review Focus 3, M6): a `Stopped` group's `DeleteGroup` answers 202. The next fresh list shows it with `provisioningState` `Deleting`: no `End`, burn still counts 117_000, and the gate sees the reservation. The list after that lacks it, giving `End`, with the charge stopped at the `Stopped` sighting (F11).
  - `TestBurnCountsEveryActiveState` (Review Focus 1, moved from Task 10 by F13): `Active()` rows in `reserved`, `registered`, `created` and `running` (117_000 each) give a burn of 468_000. Ended, undone and reaped rows add nothing.
  - `TestBurnCountsUnclaimedGroups` (Review Focus 1, M6): one active `small` row plus listed groups no row claims, namely a `ci-small-…` orphan, a `Deleting` 2/8 group whose row was undone, and a `ci-drill-…` 4/16 group. The burn is 117_000 + 117_000 + 117_000 + 233_000 = 584_000. A sixth `small` job then waits `class-full`, since 584_000 + 117_000 > 600_000. A listed 2/16 group counts 1_000_000.
  - `TestMoneyGauges` (F8): after a launch, burn is 0.117 and `period_usd{reserved}` 0.078. After its settle at 10 min, `period_usd{used}` is 0.0195 and `{reserved}` 0. `paid_ready` and `azure_locked` follow the fake. `price_age` stays absent until `SetPrices`.
  - `TestACILockedWaitsNotReady` (Review Focus 2): `List` returns `ErrLocked`. Paid jobs then wait `not-ready`, `CreateGroup` is never called, `ci_dispatch_azure_locked` is 1, `PaidState()` is `closed`, `ReadOK` stays true (F39), and tiny still launches. The next good list clears it.
  - `TestStaleListNeverSettlesAFreshLaunch` (Review Focus 3, F4):
    - a launch created at t, against a cached list read at t − 20 s that lacks it, is not ended;
    - **same step:** a list read at the step's start, before the PUT, with `At` > the step's `now`, ends nothing;
    - the first list read after `CreateGroup` returned that lacks the group ends the launch as `lost` or `finished`.
  - `TestReservationFreedWhenGroupVanishes` (Review Focus 3): the sweeper deletes a running group. On the next fresh list the row ends with charge `min(78_000, CostOf(...))`, and `PaidCommitted`'s `Reserved` drops by the reservation.
  - `TestReservationKeptWhileAzureUnreadable` (Review Focus 3): with `List` failing for an hour, the row stays active, burn and committed keep counting it, and no paid launch happens.
  - `TestNoActivePaidRowOutlivesExpiry` (Review Focus 3, property, 500 seeded runs): random sequences of group states, deletions and Azure failures, with `Deleting` bounded at one poll. From the first moment Azure answers every call, each active paid row settles by `max(that moment, Expires) + 3 × poll`.
  - `TestPricesFloorReappliedOnRead` (F23): a stored price of 50_000 for `small` reserves as 117_000.
  - `TestEgressCapDeletesOwnGroup` (Review Focus 6):
    - `SetEgress` with an own group at 10_000_000_000 bytes gives `DeleteGroup` and `reaped_total{reason="egress-cap"}` +1. The row settles on absence with `EgressCost` included;
    - at 9_999_999_999 nothing is deleted;
    - a foreign `ci-drill-…` group over the cap is never deleted by the loop.
  - `TestBurnCountsEgress` (Review Focus 6): one `small` row (117_000) plus a group sending 1_000_000 bytes/s gives a burn of 117_000 + 313_200 = 430_200. One more such group (743_400 > 600_000) makes the next `small` wait `class-full`.
  - `TestPaidNotReadyWithoutEgressPass` (E17): with no `SetEgress` for 5 min, paid jobs wait `not-ready` and `paid_ready` is 0; tiny is unaffected.
  - `TestPaidStateSnapshotRace` (F23): `SetPrices`, `PaidState` and a tick run concurrently under `-race`, and every `PaidState` answer matches one whole snapshot.
  - `TestACIStoppedTornDownThenSettled`, `TestACIPickupTimeoutSixMinutes`, `TestACIExpiredDeletedWhileForgejoDown`.
  - `TestACIOrphanGroupReaped`: an `Own` group with no ledger row older than 10 min is deleted; younger, or `ci-drill-…` or `ci-smoke-…`, is never touched, though it counts in the burn.
  - `TestTinyUnaffectedByAzureOutage`; `TestAzureListedAtMostEveryPoll` (6 ticks at 5 s give 1 `Groups` call).
  - `TestStartupReconcileAfterCrashMidLaunch` extended with ACI rows: no duplicate group or runner, no row left active with its group gone, and leftovers named by `ComputeName` with `RunnerID` 0.
  - `TestPaidDecisionLogCarriesMoney`: the launch and settle lines carry `reserved_usd` `"0.08"` and the settled `charged_usd`; no name appears.
- [ ] **Step 2: Run.** Expected: FAIL. **Step 3: Write the code.** **Step 4: Run,** then `$GO test -race -count=20 ./internal/loop/`. Expected: PASS. Commit: `loop: the ACI backend: reserve, register, create; lock, capacity, absence`.

### Task 15: The watcher and `main`

**Gate:** (b).

**Files:** create `internal/watch/{watch.go,watch_test.go}`; modify `cmd/ci-dispatch/main.go` and its test.

**Interfaces (produces):**
```go
type Source interface {
    DailyCost(ctx context.Context, from, to time.Time) (map[string]limits.Micro, error)
    SweeperRuns(ctx context.Context) ([]azure.Run, error)
    Egress(ctx context.Context, group string, from, to time.Time) (float64, error)
}
type Loop interface {   // the loop's hand-offs (Task 14)
    SetPrices(map[model.Class]limits.Micro, time.Time)
    EgressTargets() map[string]time.Time
    SetEgress(map[string]loop.Egress, time.Time)
}
type Watcher struct{ /* cfg, src, fetch, ledger, metrics, loop, log */ }
func New(cfg config.Config, src Source, fetch func(context.Context, time.Time) (prices.Rates, error),
    l *ledger.Ledger, m *metrics.Metrics, lp Loop, log *slog.Logger) *Watcher
func (w *Watcher) Step(ctx context.Context, now time.Time) error       // the 5-min pass, under a 20 s budget
func (w *Watcher) EgressStep(ctx context.Context, now time.Time) error // the 60 s pass, under a 20 s budget (E17)
func (w *Watcher) Run(ctx context.Context)                             // EgressStep every 60 s, Step every 5 min, both first at start
```
- **`Step`** does four things. The watcher, not the loop, sets `price_age`, the sweeper timestamp and the drift ratios (F8).
  - **Prices:** refresh when the stored ones are 24 h old or more. Store `prices.Hourly` per class with `ledger.SetPrices`, hand them to `lp.SetPrices`, and set `ci_dispatch_price_age_seconds` from `fetched_at`. An error keeps the stored ones, and the age keeps growing.
  - **Sweeper:** set the timestamp to the newest run with an `End`.
  - **Day drift:** from 06:00 UTC, compute the ratio for D−2 once, from `DayCharged` and `DailyCost`. Skip it if `ledger.Drift` already holds that day (restart-safe, F25). The day ratio keeps its $0.50 floor, so at today's volume it rarely evaluates; that is accepted (F25).
  - **Period drift:** on day 3 of a month, compute the ratio for the previous month from `ledger.Drift(from, to)` sums, with **no** floor (F25).
- **`EgressStep`** (E17): for each of `lp.EgressTargets()`, two `Egress` reads, since `Created` and over the last 5 min, handed to `lp.SetEgress`. A failed read for one group skips that group, and a failed pass hands nothing, so the loop's 5-min freshness rule then blocks paid launches.
- **`main`:** when `azure` is configured, it calls `LoadCredentials(cfg.Azure.Files)`, builds `azure.New` and `NewACIBackend`, runs `WithBackend([small, medium], …)`, and starts `watch.Run`. Without `azure`, the binary behaves as v0.1.0.

- [ ] **Step 1: Write the failing tests.**
  - `TestPricesRefreshedDaily`: two steps 23 h apart fetch once; at 24 h they fetch again. A failed fetch keeps the stored prices, and the age keeps growing.
  - `TestSweeperTimestampOnlyFromFinishedRuns` (Review Focus 5): a newest run with no `End` leaves the timestamp at the previous finished run, and so does a failed `SweeperRuns`.
  - `TestDriftDay`: ledger 950_000 against Azure 1_000_000 gives 0.05; Azure 400_000 gives no ratio. The row is written once a day, and a restart the same day writes nothing new (F25).
  - `TestDriftPeriod`: on 2026-12-03 the November ratio from `ledger.Drift` sums, computed even when every day was under $0.50.
  - `TestPriceAgeAbsentUntilFirstPrice` (F8): no gauge before the first stored price, then the age from `fetched_at`.
  - `TestEgressPassAndStaleness` (Review Focus 6): two targets give two `Egress` reads each and one `SetEgress` with `Total = avg × seconds since Created` and the 5-min rate. One group's failed read omits only that group. A pass with `Source` failing entirely calls no `SetEgress`.
  - `TestMainWithoutAzureRunsTinyOnly`, `TestMainRefusesMissingAzureFiles` (the error names the path, never the content).
- [ ] **Step 2: Run.** Expected: FAIL. **Step 3: Write the code.** **Step 4: Run.** Expected: PASS. Commit: `watch: prices, sweeper history, drift; main wires the ACI backend`.

### Task 16: Review, release, pin

**Gate:** (b).

- [ ] **Step 1: Whole-branch review** (superpowers:requesting-code-review) of `v2` against:
  - the spec's "Budget enforcement" and "A paid job" sections;
  - this plan's Review Focus;
  - the guard pattern: does any ARM request leave outside the eight?
  - **F19's cross-repo check:** the shape prices (117_000, 233_000, 1_000_000) and the egress constants (10_000_000_000 bytes, 87_000 µ$ a GB) in `internal/prices`, `internal/limits` and `azure/ci/fence.json` are equal;
  - the two phase 1 carries ledgered in Task 0 Step 2 (F36), put to Chris.

  Fix in one round, and ledger the rulings.
- [ ] **Step 2: PR `v2` → `main`. Chris** merges fast-forward and signs tag `v0.2.0`. The `main` run builds the image. Ledger the digest, checking the manifest anonymously as phase 1 Task 14 Step 3 does.
- [ ] **Step 3: STOP—ask Chris to review the release** (`git verify-tag v0.2.0`, `git log -p v0.1.0..v0.2.0`). Ledger his approval.

## Group (c): after phase 1 Task 18

### Task 17: `ci.yaml`, paid classes on

**Gate:** authored any time; merged before Task 19. A signed `small: on` has no effect while the ConfigMap keeps the class `off`.

**Files:** inventory branch `ci-paid-limits`: `ci.yaml`.

- [ ] **Step 1: Edit.**
  - `classes: {tiny: on, small: on, medium: on, large: off}`.
  - `forgejo-admin` gains `paid_usd_per_period: 0.15`.
  - `realliance` (id 52) gains `paid_usd_per_period: 30` *(Chris, 2026-11-05)*.
  - Every other owner, `realliance-ops` included, stays on the defaults: $10 and 2.
  - `global.paid_usd_per_period` drops from 100 to **80** (Chris, 2026-11-05); `paid_usd_per_hour` stays 0.60.
  - **Order matters:** v0.2.0's code ceiling is $80, so a head saying 100 fails its `Parse` with `ErrAboveCeiling`. This commit must therefore merge before Task 19 deploys v0.2.0. v0.1.0 accepts 80, since lowering a value is always allowed.
- [ ] **Step 2: Check locally** with phase 1 Task 15 Step 2's scratch test (`limits.Parse`, then a read-only `Resolve`). Expected: PASS, no findings. **Step 3: Commit signed, PR. Chris** merges fast-forward.

### Task 18: The luma-homeops manifests (not deployed)

**Gate:** (c). Branch `ci-phase2-manifests`.

**Files:**
- In `cluster/ci-dispatch/dispatcher/`:
  - modify `config.yaml`, `statefulset.yaml`, `networkpolicy.yaml`, `prometheusrule.yaml`, `alerts_test.yaml` and `kustomization.yaml`;
  - `config.yaml` gains `azure` and `paid` in Task 13's shape, `paid.classes.<c>` (F9), with classes `small: dry-run, medium: dry-run`;
  - `statefulset.yaml` gains the projected secret `ci-dispatch-azure` at `/etc/ci-dispatch/azure`;
  - create `azure-credentials.secret.yaml` → `azure-credentials.yaml`, `restic-cronjob.yaml`, `restic-networkpolicy.yaml` and `restic-config.secret.yaml` → `restic-config.yaml`.
- Modify:
  - `cluster/backups/prometheusrule.yaml`: both cronjob regexes gain `ci-dispatch`, making `restic-(home-assistant|plex|gonic|satellites|ci-dispatch)`;
  - `cluster/backups/restic-canary-cronjob.yaml`: host `ci-dispatch-ledger`, snapshot-exists only;
  - `scripts/test_ci_dispatch_manifests.py` and `scripts/test-ci-dispatch-alerts.sh`.

- [ ] **Step 1: Write the failing static tests.**
  - `test_config_values` (phase 2, F9):
    - classes `small` and `medium` `dry-run`, `large` `off`, `tiny` as phase 1 left it;
    - `azure` = {`/etc/ci-dispatch/azure`, `rg-realliance-ci`, `westus3`, `rg-realliance-ci-guard`, `ci-sweeper`, `30s`};
    - `paid.classes.small` {30m, 40m, 6m} and `paid.classes.medium` {45m, 55m, 6m};
    - `paid.image` matches `^ghcr\.io/christopherjmiller/forgejo-runner-class:sha-[0-9a-f]{7}@sha256:[0-9a-f]{64}$`, or `…forgejo-runner-class@sha256:[0-9a-f]{64}$` if Task 4 found `tag@digest` refused (F16). It starts with `fence.json`'s `image_like` prefix and contains its `image_contains`;
    - the file equals ci-dispatch's `internal/config/testdata/site-config.yaml` apart from the image digest. The executor checks this by hand when either changes (F9).
  - `test_paid_runner_timeouts_match_image`: `images/forgejo-runner-class/runner-<c>.yml`'s `runner.timeout` equals `paid.classes.<c>.runner_timeout`.
  - `test_azure_secret`: one SealedSecret `ci-dispatch-azure` with exactly the four keys, mounted read-only with mode 0440 under fsGroup 65532.
  - `test_dispatcher_policy` and `test_dispatcher_union_exact` (phase 2): phase 1's rules plus exactly one egress rule: `0.0.0.0/0` with `egress-internet-only`'s `except` list, ports exactly TCP 443, with a comment.
  - `test_no_guid_in_public_files` (`cluster/ci-dispatch/**`).
  - `test_restic_ledger_job`:
    - CronJob `restic-ci-dispatch` at `50 3 * * *`, after the 03:00 UTC snapshot (F34), with `concurrencyPolicy: Forbid` and `restartPolicy: Never`;
    - uid and gid 65532, `runAsNonRoot`, all capabilities dropped, `RuntimeDefault`, read-only root, with `emptyDir` mounts for `RESTIC_CACHE_DIR=/cache` and `HOME=/tmp`;
    - the PVC `ci-dispatch-ledger` mounted read-only at `/data`, backing up only `/data/snapshots`, with `RESTIC_HOST=ci-dispatch-ledger`;
    - required `podAffinity` to `app.kubernetes.io/name: ci-dispatch`;
    - the image equal to the other restic jobs'.
  - `test_restic_policy`: pods `app.kubernetes.io/name: restic-backup` get egress DNS and internet TCP 443 only, and no ingress.
  - `test_monitoring` (phase 2), the exact alert set, with severities:
    - `CIDispatchSpendHigh` (two rules, `level` 80 and 95), `CIDispatchLocked`, `CIDispatchOrphanCompute` and `CISweeperStale` `critical`;
    - `CIDispatchDrift`, `CIDispatchPaidNotReady` and `CIDispatchEgressCapHit` `warning`;
    - phase 1's others unchanged.
  - `test_backup_alerts_cover_ledger`.

  Run. Expected: FAIL.
- [ ] **Step 2: Alert rules,** tests first (`alerts_test.yaml`, one firing and one quiet case each; run the script red, then green):
  - `CIDispatchSpendHigh`: `sum(ci_dispatch_period_usd{kind=~"used|reserved"}) / max(ci_dispatch_period_usd{kind="cap"}) >= 0.80` (and `>= 0.95`), `for: 5m`. The cap is $80, so these fire at $64 and $76.
  - `CIDispatchLocked`: `max(ci_dispatch_azure_locked) == 1`, `for: 2m`.
  - `CIDispatchOrphanCompute`: phase 1's expression, now `severity: critical`.
  - `CISweeperStale` (F24), two rules sharing the name:
    - stale: `time() - max(ci_dispatch_sweeper_last_run_timestamp) > 1800`, `for: 2m`;
    - absent: `absent(ci_dispatch_sweeper_last_run_timestamp)`, `for: 30m`. A pod move, a 6-min RBD detach or the break-glass scale-to-0 therefore doesn't page as "sweeper stale"; `CIDispatchStale` covers a missing dispatcher.

    Review Focus 5: one firing case each, stale and absent, and a quiet case for a 10-min absence.
  - `CIDispatchDrift`: `max(ci_dispatch_drift_ratio) > 0.10`.
  - `CIDispatchPaidNotReady` (the spec's gate row, "wait, with an alert"; F24): `ci_dispatch_class_mode{class=~"small|medium"} >= 1 and on(class) ci_dispatch_paid_ready == 0`, `for: 30m`. It fires per class and in `dry-run` too, where Task 19 uses it as a rollback trigger.
  - `CIDispatchEgressCapHit` (E17): `increase(ci_dispatch_reaped_total{reason="egress-cap"}[1h]) > 0`.
- [ ] **Step 3: Secrets**, never opened:
  - a `python -I` script writes the four-key Secret from sso-secrets;
  - another copies `cluster/music-streaming/restic-config.secret.yaml` into the `ci-dispatch` namespace, with the same keys, printing only byte counts;
  - `KUBECONFIG=/tmp/galaxy-kubeconfig nix develop --command bash -O globstar ./sign.sh`. Expected: two new SealedSecrets.
- [ ] **Step 4: Manifests** that pass Step 1. `config.yaml`'s comment names `docs/ci.md` for moving a paid class to `on`, and says the class image is re-pinned by hand (CLAUDE.md sharp edge 6).
- [ ] **Step 5: Run everything:** the static tests, `scripts/test-ci-dispatch-alerts.sh`, `scripts/test_netpol_manifests.py` and `scripts/check-kustomizations.sh`. Expected: PASS. Commit on the branch; push the branch only.

### Task 19: Deploy v0.2.0, paid classes in dry-run

**Gate:** (c), and Tasks 4, 5, 9, 16, 17 and 18 done.

- [ ] **Step 1: Preconditions.**
  - Phase 1 Task 18's dry-run is green.
  - Task 17's `ci.yaml`, with `global.paid_usd_per_period: 80`, is the Owner-signed head. v0.2.0 would reject a head saying 100.
  - The plan, in the grep form, reports `No changes.` (F14).
  - No other plan's Gate G window is open.
  - A v1 snapshot from today exists for E13: the dispatcher's log shows today's `snapshot` line (the image is distroless, so nothing can `exec` into it). Otherwise wait for 03:00 UTC.
- [ ] **Step 2: Gate G, then STOP—ask Chris.** The change: v0.2.0, schema v2, Azure egress on 443, the Azure credential in the pod, paid classes in `dry-run`. Watch:
  - Synced/Healthy and `ci-dispatch-0` Ready;
  - `ci_dispatch_azure_locked 0`, flat `ci_dispatch_errors_total{api=~"azure|prices"}`, price age under 1 h;
  - a fresh sweeper timestamp, burn 0, and an egress pass within 5 min (E17);
  - zero deny lines for `ci-dispatch/` and `restic-backup`.

  Rollback triggers: a crash loop, a migration error, any deny line on legitimate traffic, or `CIDispatchPaidNotReady`, which fires in dry-run (F24). Rollback, split by E13 and F10:
  - a migration error, which leaves the ledger at v1, gets a revert of the pin to v0.1.0;
  - anything after a good migration gets classes `off`, with v0.2.0 kept. Never downgrade over a v2 ledger; that takes the snapshot restore in `docs/ci.md`.
- [ ] **Step 3: Push and `Monitor`** until Synced, Healthy and Ready (timeout 10 min).
- [ ] **Step 4: Verify.**
  - `ci_dispatch_paid_ready{class}` is 1 for both classes. In a `forgejo-admin` scratch repo ($0.15), a `runs-on: small` probe logs one `launch` decision in mode `dry-run`. A `runs-on: medium` probe holds `owner-limit`, because its 0.2136 reservation exceeds 0.15; that proves the money gate on live data. `aci_group_names()` stays empty.
  - Run the restic job now (`kubectl -n ci-dispatch create job --from=cronjob/restic-ci-dispatch restic-ci-dispatch-first`, in this STOP), and `Monitor` it to `Complete`. `restic snapshots --host ci-dispatch-ledger` shows one.
  - `scripts/netpol-verify.sh stray --since 15m` PASSes.
  - Gate G after.
- [ ] **Step 5: 24 h dry-run** (E14). Compare the decision log with what the paid gates should have decided. Expected: no surprises and no error counters.

### Task 20: Docs

**Gate:** Step 1 lands, docs-only, before Task 21.

**Files:** modify `docs/ci.md`, `docs/backups.md` (the new restic host `ci-dispatch-ledger`, F35), `CLAUDE.md` (the "Forge CI" row: paid classes, `azure/ci/`, the lock, the egress cap), and the spec; create `azure/ci/README.md` (one screen); modify ci-dispatch `docs/members.md`.

- [ ] **Step 1: `docs/ci.md`,** new sections:
  - paid classes and their modes;
  - pausing them (`off`, a deploy STOP);
  - limits and the caps owner;
  - reading an owner's spend (usage API) and the month's (`ci_dispatch_period_usd`);
  - **after a trip:** find out why (the sweeper's failed run, the dispatcher's log); only Chris unlocks, with `./azure/ci/tf.sh apply`; `ci_dispatch_azure_locked` clears within one poll;
  - rotating the Azure credential (F26). At 150 days `time_rotating` replaces the secret, and that apply **destroys the old one at once**: it doesn't wait for day 180. Rotation is one STOP run back to back: Chris's apply, the pipe into sso-secrets, reseal, and the deploy push. Paid classes are `not-ready` between the apply and the deploy. The due date is in the ledger;
  - **downgrading the binary** (F10): v0.1.0 refuses schema v2. Restore the newest v1 snapshot from before the v0.2.0 deploy (restic host `ci-dispatch-ledger`), then pin v0.1.0. Paid classes stay `off`;
  - **the egress cap** (E17): 10 GB per job, priced at $0.087 a GB, charged to the owner. A job killed at the cap shows reap reason `egress-cap` and `CIDispatchEgressCapHit`. The value lives in `fence.json` (the sweeper) and `limits.EgressCapBytes` (the dispatcher), which must change together;
  - break-glass: scale to 0, then `az container list -g rg-realliance-ci --query "[?tags.\"forge-ci\"=='1'].name" -o tsv` and delete each;
  - re-running the sweeper drill (Task 5) after any change to `azure/ci/sweeper*`;
  - the month-end drift check;
  - restoring the ledger: paid classes stay `off` until a restore (spec);
  - known risks:
    - the job listing doesn't page in Forgejo 15 and is capped at 16 MiB. Past about 80k listed jobs nothing launches, paid classes included; running jobs keep their hard stops, and the reaper and the sweeper keep deleting. Recovery is cancelling the flood. The fix, a paged listing, is on the fork's `WISHLIST.md` (M3);
    - a paid launch settles only when its group is gone from a list, never on an accepted DELETE. A spend figure can therefore trail the delete by one poll (30 s) and a `Deleting` phase (M6).
- [ ] **Step 2: `docs/members.md`:**
  - `runs-on: small` and `runs-on: medium`, and what $10 buys (about 85 h of `small`);
  - root in a fresh group with open egress, no Docker (phase 3), no cache, and no `ubuntu-latest`;
  - internet egress is charged at $0.087 a GB, and a job that sends more than 10 GB is killed (E17);
  - a job held at the limit waits up to 24 h.
- [ ] **Step 2b: The spec amendments, in one commit.** Edit `docs/superpowers/specs/2026-11-04-runner-classes-design.md` with a dated amendment block (2026-11-05, Chris) like its pre-flight one, and fix the body text it touches:
  - (a) the reservation is the exact worst case (E3; the table's "Reserved per job" reads 78_000 and 213_584 µ$);
  - (b) the lock fires from the sweeper reading `currentSpend` each run (E5);
  - (c) the two budgets are CI and guard $5 (E6), with the CI amount as (g) sets it;
  - (d) the per-class name grammar `ci-<class>-<job>-<attempt>-<try>`, with Kubernetes Jobs `ci-tiny-*` only (in "Reaper");
  - (e) the dispatcher's role holds ACI rights only until phase 3 (E8);
  - (f) ids are authoritative and names are labels (the `ci.yaml` comment);
  - (g) **the paid cap drops to $80 a period and the CI budget to $85** (Chris, 2026-11-05, for the cache host). The text changes in the summary, "Limits", the backstop table, the scenario table (row 2 becomes $85 + $28.80, about $138 with logging, state, the sweeper and the cache host), and the `ci.yaml` example;
  - two corrections the pre-flight found (F35): the dispatcher's `runs/read` sits in the **guard** group (the spec's "Azure identity" says the CI group only), and paid launches settle on observed absence ("Settle", M6);
  - (h) the per-job egress cap (E17), written only if Chris keeps the plan's call; otherwise its final values.

  Commit: `spec: runner classes amendments (Chris, 2026-11-05)`.
- [ ] **Step 3: Verify:** vale on each new or changed file, no output; `grep -nE 'secret|token' docs/ci.md` shows names only; no GUID. **Step 4: Commit and push** (docs-only; members' guide through a ci-dispatch PR).

### Task 21: Paid classes on; acceptance; the first consumer

**Gate:** (c), and Task 19 Step 5 green, Task 20 Step 1 on `main`, the Task 9 route live, and a restic snapshot present.

- [ ] **Step 1: Change the expected modes**, watch `test_config_values` fail, set `small: on` and `medium: on`, and watch it pass. Commit: `ci-dispatch: small and medium on (runner classes phase 2)`.
- [ ] **Step 2: Gate G, then STOP—ask Chris.** Every member can now spend up to their limit. Rollback triggers: any acceptance failure, a 24/7 alert, or burn over $0.60 an hour. Rollback: classes `off` and a revert; fast stop: the break-glass.
- [ ] **Step 3: Push and `Monitor`** the rollout, then Gate G after (F28).
- [ ] **Step 4: Acceptance.** `CI_PAID_ACCEPTANCE=1 nix develop --command pytest tests/forge/test_forge_ci_paid.py -v`. Expected: all PASS. Ledger:
  - start times from `ci_dispatch_start_seconds{backend="aci"}`;
  - each probe's charge;
  - next day, Cost Management's figure for the CI group against `DayCharged`.

  If a launch hit the pickup timeout, raise `pickup` to twice the slowest start; that's a deploy, with its own STOP. Mark the sso PR ready; **Chris** merges. This meets the done-when "paid jobs run; reservations hold".
- [ ] **Step 5: The first consumer.** When the fork's image workflow runs on `runs-on: medium` (Chris, 2026-11-05), billed to `realliance` ($30), ledger its wall time, start time, peak memory (`/proc/meminfo` at the end of the job), cost, and pass or fail. If the wall time is over 75 % of its class's runner timeout, recommend the next class.

### Task 22: The first closed period

**Gate:** after Task 21.

- [ ] **Step 1: The first period wholly after Task 21** (F25). That's the first UTC calendar month that **starts** after Task 21, so the drill's and smoke's unledgered groups fall outside it. On day 3 of the month after it, read `ci_dispatch_drift_ratio{window="period"}`. Expected: under 0.10, which meets the spec's last done-when. Over 0.10: compare `drift` rows with Cost Management by day, find the cause, fix it, and re-check next month. Likely causes: rounding, the tail between the stop and the delete (F11), a launch undone after its PUT, or egress the metric undercounted (E17).
- [ ] **Step 2: Two weeks stable:**
  - `max_over_time(ALERTS{alertname=~"CIDispatch.*|CISweeperStale",severity="critical"}[14d])` is empty;
  - `max_over_time(ci_dispatch_orphans{kind="group"}[14d])` is 0;
  - `./azure/ci/tf.sh plan` reports `No changes.`

  Ledger `phase2_done=<UTC>`, and open phase 2b.

## Follow-ups (later plans)

- **Phase 2b:** `galaxy` retires into `small`. forge-bot's and ci-dispatch's workflows say `runs-on: small` and bill to their owner; then remove the Deployment and its cache claim (S4). `realliance-ops` stays at the $10 default until then; any raise is Chris's call at 2b.
- **Phase 3:** VMs, `docker` and `large`. The dispatcher role, the Policy types and the sweeper's `delete_types` grow together, and the drill re-runs. Pay-as-you-go only.
- **The fork's usage page:** the usage token reaches Forgejo, and the ingress rule on 8081.

## Decided (Chris, 2026-11-05)

- **Paid limits at go-live:** `realliance` gets $30 a period. Every other owner, `realliance-ops` included, stays at the $10 default (Task 17).
- **Spec amendments, all six approved:**
  - (a) E3, the exact worst-case reservation;
  - (b) E5, the lock via `currentSpend`;
  - (c) E6, the two budgets (the CI amount now $85, per the cap line below);
  - (d) the per-class name grammar;
  - (e) the ACI-only dispatcher role until phase 3;
  - (f) ids authoritative.

  Task 20 Step 2b writes them into the spec in one commit.
- **The fork's image build** uses `medium` (Task 21 Step 5).
- **F1, paid held undos:** a paid held undo after compute existed charges `CostOf(compute_at → stop)`, settled on absence, no try. `ErrNoCapacity` stays at 0. The held back-off is 1, 2, 5, 10, 10, then 60 min (Tasks 10, 14).
- **The paid cap is $80 a period and the CI budget $85** (cache design, `docs/superpowers/specs/2026-11-05-ci-cache-design.md`). It pays for an always-on Azure cache host ($12.41 a month, in its own group outside the CI group) under the $150 cap. The $0.60-an-hour ceiling holds against $85 and stays (Values fixed by the spec). It's the seventh spec amendment, (g) in Task 20. It changes `limits.CeilingPeriod` (Task 10), `fence.json` and the budget (Task 2), the sweeper's trip (Task 3), the gate tests (Task 12) and `ci.yaml` (Task 17).
- **A per-job egress cap before paid goes on** (E17). The design is in Tasks 3, 4, 7, 10, 14, 15 and 18 and Review Focus 6. The values (10 GB, 87_000 µ$ a GB, charged to the owner) are the plan's call.

### Open for Chris

- **The egress cap's values** (E17, *plan's call*): 10 GB per job; the free tier ignored; egress charged to the owner outside the compute reservation. One more point in it reaches the spec's own worst case, and needs his eye. A compromised dispatcher that churns groups shorter than one sweeper run evades the sweeper's egress rule, as it evades the compute rate trip. Then only the budget, 8–48 h late, bounds egress, which can outrun $0.60 an hour. The spec's row 2 no longer bounds that case. No cheap fix exists without a VNet and a NAT gateway (about $32 a month); the plan accepts the residual and names it.
