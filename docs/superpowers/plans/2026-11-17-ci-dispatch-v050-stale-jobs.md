# ci-dispatch v0.5.0: stale-job hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Azure outages stop holding paid jobs, stuck queues become visible, and a held job can be released through an authenticated control route, in ci-dispatch v0.5.0.

**Architecture:** Azure-side create failures settle as a new non-try outcome and open a paid-class breaker that reuses the existing lock probe. Creates run in a goroutine bounded at 60 s and finish under the step lock on a later tick. Ledger schema v3 adds `releases` (which reset a key's tries and back-off) and `absorbed` (late-group compute, global only). A new `internal/control` server on its own listener and token takes releases from the Forgejo fork.

**Tech Stack:** Go 1.26 (toolchain go1.26.10), SQLite ledger, hand-written Prometheus exposition, no new modules (`deps_test.go`).

**Spec:** `docs/superpowers/specs/2026-11-17-ci-stale-jobs-design.md` (luma-homeops `c1e0c115`). This is plan 1 of 4; plans 2–4 (luma-homeops, the Forgejo fork patch, the `fj` overlay) consume the names in this plan's Interfaces blocks.

## Global Constraints

- Repo `realliance-ops/ci-dispatch`, base `origin/main` = `19a65ae` (v0.4.0). Work in a new worktree `~/Repos/ci-dispatch-stale-jobs` on branch `stale-jobs`. Signed commits (`git commit -S`).
- Go is not on PATH: run Go through `nix shell nixpkgs#go -c …`. Every local build/test: `GOMAXPROCS=4 taskset -c 0-3 go test -race -p=4 …`. CI runs `go vet ./...` and `go test -race ./...`.
- Breaker back-off: 1, 2, 5, then 10 min, capped at 10 (`gate.BackoffAfter`). Only a successful create closes it.
- Create bound: 60 s. In-flight creates: at most 2.
- A key gets 20 `unavailable`/`restart` undos free; each beyond 20 counts as a try.
- Keep review I1: a create with an unclear answer is deleted with its runner, never adopted.
- Automatic second round: a `launch-failing` hold at least 60 min old, paid classes only, once per key.
- Manual release: every class; at most 3 per key per rolling 24 h. Releasable: `launch-failing`, `backoff`. Not releasable: `no-budget`, `owner-limit`, `global-cap`, `label`, `denied`, `invalid`, `concurrency`.
- Control route `POST /v1/holds/{job_id}/{attempt}/release`, its own listener (default `:8082`) and bearer token file, distinct from the usage token; `via` is `api`, `button` or `comment` over HTTP, `auto` only from the loop.
- Late-group compute is booked from the `created-at` tag to the first poll that finds the group gone, counted in global spend, `global-cap` and drift, never per owner.
- Do not touch `internal/config/testdata/site-config.yaml` (it must equal luma's `config.yaml`; plan 2 adds the control keys to both).
- Stop before tagging v0.5.0 or pinning an image.

## Review Focus

1. A dispatcher restart while creates are in flight: the row is `registered` with no goroutine behind it. Expect adoption if the group is listed, else an undo as `restart` that is no try (Task 4, `TestRestartWithCreateInFlight`).
2. A release for a key whose launch is active in the same tick: expect `not-held` and no second launch (Task 7, `TestReleaseWhileActive`).
3. An outage while Azure is also locked or denied: `ci_dispatch_azure_locked` stays 0 for an outage alone, so `CIDispatchLocked` (critical, 24/7) does not fire for it (Task 3, `TestBreakerIsNotALock`).
4. Upgrading a populated v2 ledger with active rows and holds: everything survives, v3 tables are empty (Task 1, `TestMigrateV2ToV3`).
5. A restart after an automatic release: no second automatic release for that key (Task 8, `TestAutoReleaseSurvivesRestart`).

---

### Task 1: Ledger v3 — releases, absorbed, tries that reset

**Files:**
- Create: `internal/ledger/schema_v3.sql`, `internal/ledger/release.go`, `internal/ledger/release_test.go`
- Modify: `internal/ledger/ledger.go` (embeds `:32-41`, `migrateTo :246`, outcome constants `:130-141`, `qHeld/qTries/qFailed :121-125`, `Held :630`, `Failed :640`, `Tries :670`), `internal/loop/loop.go:87-93` (`OutcomeRestart` aliases the ledger constant)
- Test: `internal/ledger/money_test.go` (`TestMigrateV1ToV2 :203`, `TestMigrateErrorLeavesV1 :284`: replace the hard-coded `2` with `schemaVersion`), `internal/ledger/fix_test.go` (`TestExactColumnSet`), `internal/ledger/index_test.go`

**Interfaces:**
- Produces:
  ```go
  const OutcomeUnavailable = "unavailable"
  const OutcomeRestart = "restart"
  const FreeUnavailable = 20
  const (ViaAPI = "api"; ViaButton = "button"; ViaComment = "comment"; ViaAuto = "auto")
  type Release struct {
      Key model.Key; OwnerID, RepoID int64; At time.Time
      ActorID int64; ActorName, Via string
  }
  func (l *Ledger) Release(ctx context.Context, r Release) error
  func (l *Ledger) LastRelease(ctx context.Context, k model.Key) (time.Time, error)            // zero if none
  func (l *Ledger) ReleaseCounts(ctx context.Context, k model.Key, since time.Time) (manual, auto int, err error)
  func (l *Ledger) Releases(ctx context.Context, owner int64, from, to time.Time) ([]Release, error) // by at, oldest first
  func (l *Ledger) HoldOf(ctx context.Context, k model.Key) (Hold, bool, error)               // uncleared only
  func (l *Ledger) LastLaunch(ctx context.Context, k model.Key) (Launch, bool, error)
  ```
  `Tries`, `Failed` and `Held` keep their signatures and count only rows with `created` after `LastRelease(k)`.

- [ ] **Step 1: Write the failing tests** in `release_test.go` and the migration test in `money_test.go`:

```go
func TestMigrateV2ToV3(t *testing.T)            // buildV1 then migrateTo(…, 2) with an active row, an undone row and a hold;
                                                // Open → version 3, one meta row, rows/holds unchanged, releases and absorbed exist and are empty;
                                                // a second Open changes nothing (dump before == after).
func TestTriesSkipsUnavailableAndRestart(t *testing.T) // undo outcomes: 3×unavailable, 1×restart, 1×"error: create-group" → Tries == 1
func TestTriesCountsUnavailableBeyondFree(t *testing.T) // 22×unavailable → Tries == 2
func TestReleaseResetsTriesAndBackoff(t *testing.T)     // 3 error undos created t0..t0+3m; Release at t0+10m → Tries 0, Failed n 0, Held n 0;
                                                        // one more error undo at t0+11m → Tries 1
func TestReleaseCounts(t *testing.T)                    // manual at now-1h, now-2h, auto at now-1h, manual at now-25h → ReleaseCounts(k, now-24h) == (2, 1)
func TestReleaseRefusesUnknownVia(t *testing.T)         // Via "web" → error, no row
func TestHoldOfIgnoresCleared(t *testing.T)             // SetHold then ClearHold → HoldOf ok == false
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd ~/Repos/ci-dispatch-stale-jobs && nix shell nixpkgs#go -c env GOMAXPROCS=4 taskset -c 0-3 go test -race -p=4 ./internal/ledger/`
Expected: FAIL (undefined: `Release`, `OutcomeUnavailable`, …)

- [ ] **Step 3: Write `schema_v3.sql`**

```sql
CREATE TABLE releases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL, attempt INTEGER NOT NULL, owner_id INTEGER NOT NULL, repo_id INTEGER NOT NULL,
  at INTEGER NOT NULL, actor_id INTEGER NOT NULL DEFAULT 0, actor_name TEXT NOT NULL DEFAULT '',
  via TEXT NOT NULL CHECK (via IN ('api', 'button', 'comment', 'auto')));
CREATE INDEX releases_key ON releases(job_id, attempt, at);
CREATE INDEX releases_owner_at ON releases(owner_id, at);
CREATE TABLE absorbed (
  launch_id INTEGER PRIMARY KEY, class TEXT NOT NULL,
  compute_from INTEGER NOT NULL, compute_to INTEGER NOT NULL,
  hourly_micro INTEGER NOT NULL, charged_micro INTEGER NOT NULL, at INTEGER NOT NULL);
CREATE INDEX absorbed_from ON absorbed(compute_from);
UPDATE meta SET schema_version = 3;
```

- [ ] **Step 4: Implement** the embed, `schemaVersion = 3`, `case 3:` in `migrateTo`, the constants, and `release.go`. `qTries`/`qFailed`/`qHeld` gain `AND created > ?` (the last release, 0 if none). `qTries` excludes undone `held`, `locked`, `unavailable` and `restart`; `Tries` adds `max(0, n - FreeUnavailable)` where `n` counts undone `unavailable`+`restart` rows after the last release (its own indexed query, `qFree`). `loop.OutcomeRestart = ledger.OutcomeRestart`. Add `releases` and `absorbed` to `TestExactColumnSet`; add `qFree`, `qLastRelease` and `qReleaseCounts` to `TestKeyQueriesUseAnIndex` (they must SEARCH by `launches_key` or `releases_key`).

- [ ] **Step 5: Run the ledger tests and see them pass**

Run: same as Step 2. Expected: PASS, including the existing `TestSchemaV1Frozen`, `TestMigrationsIdempotent`, `TestNoTokenColumn` and `TestPackageDoesNotLog`.

- [ ] **Step 6: Commit** — `ledger: schema v3 (releases, absorbed); unavailable and restart undos are no try`

### Task 2: Azure failures are not tries

**Files:**
- Modify: `internal/loop/acibackend.go` (`Create :330-361`), `internal/loop/loop.go` (outcome mapping `:929-939`)
- Test: `internal/loop/aci_test.go`

**Interfaces:**
- Consumes: `ledger.OutcomeUnavailable` (Task 1).
- Produces: `var errAzureDown = errors.New("loop: azure unavailable")` and `func azureDown(err error) bool` in `acibackend.go`. `Create` wraps a down error in `errAzureDown`, keeping today's `ErrNotCreated` wrapping (an ambiguous create stays outside `ErrNotCreated`, so I1's delete still runs).

The rule: an error is a try only if it is a deterministic refusal — `azure.ErrRejected`, `azure.ErrPolicy`, `azure.ErrRefused`, `azure.ErrNotFound`, `azure.ErrExists`. `ErrLocked` and `ErrNoCapacity` keep their paths. Every other error (5xx, 429, `MaybeCreated`, transport, `context.DeadlineExceeded`, `errAzureSlow`, a failed token or GET-first) is down.

- [ ] **Step 1: Write the failing tests**

```go
func TestACICreateClassifiesUnavailable(t *testing.T) // table over fakeAzure.failOnce("CreateGroup", err):
    // fmt.Errorf("%w: status 500", azure.ErrUnavailable) → outcome "unavailable", Tries 0
    // realMaybeCreated(t) (503 after send)            → "unavailable", Tries 0, one DeleteGroup sent (I1)
    // context.DeadlineExceeded                        → "unavailable", Tries 0
    // errors.New("dial tcp: i/o timeout")             → "unavailable", Tries 0
    // azure.ErrRejected / azure.ErrPolicy / azure.ErrExists → "error: create-group", Tries 1
    // azure.ErrNoCapacity → "held"; errLockedFake → "locked" (unchanged)
func TestBrownoutNeverHoldsLaunchFailing(t *testing.T) // failAlways CreateGroup ErrUnavailable for 10 launches → no hold
    // with reason launch-failing, Tries 0; clear the failure → the job reaches Created
func TestRestartLeftoverIsNotATry(t *testing.T)        // crashTick between SetRegistered and Create, restart, group absent →
    // row undone "restart", Tries 0
```

- [ ] **Step 2: Run them and see them fail**

Run: `nix shell nixpkgs#go -c env GOMAXPROCS=4 taskset -c 0-3 go test -race -p=4 ./internal/loop/ -run 'Classifies|Brownout|RestartLeftover'`
Expected: FAIL (outcome `error: create-group`, Tries 1)

- [ ] **Step 3: Implement** `azureDown`, the `errAzureDown` wrap in `Create`, and in `launch` map `errors.Is(err, errAzureDown)` to `settlement{kind: settleUndo, outcome: ledger.OutcomeUnavailable}`.

- [ ] **Step 4: Run the loop package** (same command without `-run`). Expected: PASS; `TestACIErrorOutcomes` and `TestACICreateMapsErrors` updated only where a 5xx row's expected outcome changes to `unavailable`.

- [ ] **Step 5: Commit** — `loop: an Azure outage undoes as unavailable, no try`

### Task 3: Paid-class breaker with one probe

**Files:**
- Modify: `internal/loop/acibackend.go` (fields `:61-101`, `Probe :409-411`, success/failure bookkeeping), `internal/loop/paid.go` (`paidInputs :78`, `admits :127-133`, `paidPass :360`, `paidRelock :405-412`, `probe :462`, `shut`), `internal/metrics/metrics.go`
- Test: `internal/loop/aci_test.go`, `internal/metrics/metrics_test.go`

**Interfaces:**
- Consumes: `errAzureDown` (Task 2).
- Produces: `func (b *aciBackend) Unavailable() bool`; interface `downer interface{ Unavailable() bool }` and `func (s *step) down() bool` in `paid.go`; `paidInputs.down bool`; metric `ci_dispatch_azure_unavailable` (gauge, 0/1) via `func (m *Metrics) SetAzureUnavailable(bool)`, present from start.

Behavior: a down failure sets `unavail++`, `unavailAt = clock()`; any successful create sets `unavail = 0` and `denied = 0`; reads never touch `unavail`. `admits` requires `(!locked || probe) && (!down || probe)`. `Probe()` is true when `!locked`, nothing is creating (Task 4's counter; 0 here), at least one of `denied`/`unavail` is set, and each set one's `BackoffAfter(n)` has passed. `s.probe()` and `shut(bs)` treat a backend as closed when `Locked() || Unavailable()`. `SetAzureLocked` keeps reading `Locked()` only.

- [ ] **Step 1: Write the failing tests**

```go
func TestBreakerOpensAndProbes(t *testing.T)
    // failAlways CreateGroup ErrUnavailable; two paid jobs; first create fails →
    // next tick both decide "wait not-ready"; CreateGroup count stays 1 until clock +1m;
    // at +1m exactly one CreateGroup (the probe), fails → none until +2m after it;
    // clear the failure → the probe succeeds → both jobs reach Created within two ticks;
    // ci_dispatch_azure_unavailable 1 while open, 0 after.
func TestBreakerIgnoresReads(t *testing.T)      // while open, Groups lists succeed → still "wait not-ready"
func TestBreakerIsNotALock(t *testing.T)        // while open: azure_locked 0, Locked() false; with setLocked too: both gauges 1
func TestTinyLaunchesWhileBreakerOpen(t *testing.T) // a tiny job reaches Created while the paid class waits not-ready
```
In `metrics_test.go`: add the family to `allowed`, `want` and `busy()`.

- [ ] **Step 2: Run them and see them fail** (`-run 'Breaker|metrics'` across `./internal/loop/ ./internal/metrics/`). Expected: FAIL.

- [ ] **Step 3: Implement** as described above.

- [ ] **Step 4: Run `./internal/loop/ ./internal/metrics/`.** Expected: PASS, including `TestACILockedWaitsNotReady` and `TestACILockedCreateAndDelete` unchanged.

- [ ] **Step 5: Commit** — `loop: a paid-class breaker opens on an Azure outage and closes on a probe that creates`

### Task 4: Creates leave the step, bounded at 60 s

**Files:**
- Modify: `internal/azure/client.go:83,232` (`httpTimeout` 30 s → 60 s, the clamp to match), `internal/loop/backend.go`, `internal/loop/acibackend.go` (split `Create`; `Room :243-246`), `internal/loop/loop.go` (`launch :885-963`, Tick order `:382-416`), `internal/loop/reap.go` (Registered rows `:172-188`, `awaitAbsence :264-307`)
- Test: `internal/loop/acifix_test.go`, `internal/loop/fakeazure_test.go` (a hang that ends on context cancel)

**Interfaces:**
- Consumes: `azureDown`, breaker bookkeeping (Tasks 2–3).
- Produces (in `backend.go`):
  ```go
  type creating struct{ name string; done chan struct{}; err error }
  type asyncCreator interface {
      StartCreate(s Spec) *creating   // returns at once; the goroutine calls only b.az.CreateGroup, bounded by createBound
      FinishCreate(c *creating) error // step lock held: today's Create tail (classification, denied/unavail, latch)
      Creating() int
  }
  ```
  `aciBackend`: `const createBound = 60 * time.Second`, `const maxCreating = 2`, field `createBound time.Duration` (tests shorten it), field `creating int`. `Room(ctx) = !b.Slow() && b.creating < maxCreating`. Loop: `lp.creates map[int64]*pendingCreate` keyed by launch id; `func (s *step) finishCreates()` runs right after `readActive`; `func (s *step) afterCreate(l *inflight, d model.Decision, err error)` is the tail of today's `launch` from `b.Create` on, shared by both paths. `finishCreates` points `l.bs` at the current step's list state for the backend before calling it.

The goroutine touches no loop, ledger or backend field: it writes `c.err` and closes `c.done`. Rows in `lp.creates` are skipped by the Registered-row check and by `awaitAbsence`. `azure.Client` is safe to share (`tokenCache` has its own mutex); `-race` must stay clean.

- [ ] **Step 1: Write the failing tests**

```go
func TestCreateRunsOffTheStep(t *testing.T)
    // hang("CreateGroup", true); a paid job and a tiny job; 12 ticks: ReadOK each tick,
    // tiny reaches Created, the paid row stays registered and is not undone;
    // unhang → next tick the paid row is Created and "launched" is logged.
func TestCreateBoundCutsAHungPut(t *testing.T)
    // createBound 200ms; CreateGroup blocks until its context ends →
    // outcome "unavailable", one urgent DeleteGroup, the runner removed, Tries 0.
func TestInFlightCreateCap(t *testing.T)   // hang; 4 paid jobs → exactly 2 CreateGroup calls, 2 rows registered, no rows for the rest
func TestRestartWithCreateInFlight(t *testing.T)
    // hang; restart the rig on the same ledger; (a) group listed → row adopted (Created);
    // (b) group absent → undone "restart", Tries 0.
```
`TestTinyLaunchesWhileAzureDeleteHangs`, `TestTinyLaunchesWhileAnyAzureCallHangs`, `TestTinyLaunchesWhileAzureFailsSlowly`, `TestAmbiguousCreateCutByTheBudgetStillDeletes` and `TestLatchSkipsTheStepsOtherPaidLaunches` keep passing; adjust only expectations that assumed the PUT ran inside the tick.

- [ ] **Step 2: Run them and see them fail** (`go test -race ./internal/loop/ -run 'Create|InFlight|RestartWith'`). Expected: FAIL.

- [ ] **Step 3: Implement** the split, the map, `finishCreates`, `afterCreate`, the reaper skips and the timeout change.

- [ ] **Step 4: Run `./internal/azure/ ./internal/loop/` with `-race`.** Expected: PASS, no race reports.

- [ ] **Step 5: Commit** — `loop: creates run off the step, bounded at 60 s, two at a time`

### Task 5: Broken groups fail fast; pickup timeouts back off

**Files:**
- Modify: `internal/loop/reap.go` (the Created-row path near `:238`), `internal/ledger/ledger.go` (`qFailed`)
- Test: `internal/loop/aci_test.go`, `internal/ledger/release_test.go`

**Interfaces:**
- Produces: outcome `"error: start"` (`undoError("start")`). `qFailed` counts `(state='undone' AND substr(outcome,1,7)='error: ') OR (state='reaped' AND outcome='pickup-timeout')`, after the last release.

A Created row (not yet Running) whose listed group is `Broken` or `Stopped && !Ran` is undone at once as `error: start` through `undoCompute` (a try, error back-off). ARM's list gives no image-pull events, so a group that pulls in a loop while Pending still waits for the pickup timeout.

- [ ] **Step 1: Write the failing tests**

```go
func TestBrokenGroupUndoneBeforePickup(t *testing.T)   // after Created, set Provisioning "Failed"; clock +30s → undone "error: start", DeleteGroup sent, Tries 1
func TestStoppedBeforeRunnerIsUndone(t *testing.T)     // State "Stopped", Terminated, ExitCode 1, runner never declared → "error: start"
func TestPickupTimeoutBacksOff(t *testing.T)           // two pickup-timeout reaps → Failed n 2; gate decides "wait backoff" until BackoffAfter(2) passes
```
`TestKeyQueriesUseAnIndex` keeps `qFailed` on `launches_key`.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `./internal/ledger/ ./internal/loop/` with `-race`.** Expected: PASS.
- [ ] **Step 5: Commit** — `reap: a broken group fails fast; pickup timeouts back off`

### Task 6: The ledger books late groups, globally

**Files:**
- Create: `internal/ledger/absorb.go`, `internal/ledger/absorb_test.go`
- Modify: `internal/ledger/money.go` (`DayCharged :147`, `PaidCommitted :100`), `internal/loop/reap.go` (`awaitAbsence`), `internal/loop/loop.go` (the loop's ledger interface `:129-155`)
- Test: `internal/ledger/money_test.go` (`TestPaidQueriesUseIndexes`), `internal/loop/aci_test.go`

**Interfaces:**
- Consumes: the `absorbed` table (Task 1).
- Produces: `func (l *Ledger) Absorb(ctx context.Context, launchID int64, from, to time.Time) error` — idempotent per launch, `charged_micro = min(reserved_micro, CostOf(hourly_micro, to-from))` from the launch row. `DayCharged` adds `absorbed` by `compute_from` in the day (`qDayAbsorbed`, on `absorbed_from`). `PaidCommitted`'s global `Used` adds `absorbed` by `compute_from` in `[from, to)`; the owners map and `PaidUse` do not change.

Loop: while a row waits for its group to go (`awaitAbsence`) and the group is listed, keep the earliest `Compute.Created` in `bs.e.seen[x.ID]` (in memory; a restart loses it, an accepted gap of cents). When the row settles gone, its `ComputeAt` is zero and `seen` has it, call `Absorb(x.ID, seen, bs.at)`.

- [ ] **Step 1: Write the failing tests**

```go
func TestAbsorbCountsGlobalNotOwner(t *testing.T) // small, 10 min absorbed: DayCharged +cost; PaidCommitted global +cost;
                                                  // owners[owner] unchanged; PaidUse(owner) unchanged
func TestAbsorbIsIdempotent(t *testing.T)         // Absorb twice → one row, cost once
func TestLateGroupIsBooked(t *testing.T)          // realMaybeCreated 503; the group appears in the next listing (created-at tag = launch time),
                                                  // DeleteGroup, gone on a later poll → absorbed row from tag to that poll's time
func TestNeverListedGroupIsNotBooked(t *testing.T) // 503 after send, group never listed → no absorbed row
```
Add `qDayAbsorbed` to `TestPaidQueriesUseIndexes` (index `absorbed_from`).

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `./internal/ledger/ ./internal/loop/ ./internal/watch/` with `-race`.** Expected: PASS.
- [ ] **Step 5: Commit** — `ledger: book late groups' compute to the global spend`

### Task 7: The control route

**Files:**
- Create: `internal/control/control.go`, `internal/control/control_test.go`
- Modify: `internal/config/config.go` (`rawConfig :87-141`, `Config :23-54`, `Load :192-230`, `validate :280`), `cmd/ci-dispatch/main.go` (`:128-195`), `README.md` (near `:69`)
- Test: `internal/config/config_test.go`, `cmd/ci-dispatch/main_test.go`

**Interfaces:**
- Consumes: `ledger.Release`, `ReleaseCounts`, `HoldOf`, `LastLaunch`, `Failed`, `Held` (Task 1); `gate.BackoffUntil`.
- Produces (plan 3's Forgejo client depends on this exact wire format):
  ```go
  type Store interface {
      LastLaunch(context.Context, model.Key) (ledger.Launch, bool, error)
      HoldOf(context.Context, model.Key) (ledger.Hold, bool, error)
      Failed(context.Context, model.Key) (int, time.Time, error)
      Held(context.Context, model.Key) (int, time.Time, error)
      ReleaseCounts(context.Context, model.Key, time.Time) (int, int, error)
      Release(context.Context, ledger.Release) error
  }
  func New(token secret.Value, st Store, onRelease func(via string), log *slog.Logger) (*Server, error)
  const ManualCap = 3
  const CapWindow = 24 * time.Hour
  type Request struct { ActorID int64 `json:"actor_id"`; ActorName string `json:"actor_name"`; Via string `json:"via"` }
  type Result  struct { JobID int64 `json:"job_id"`; Attempt int64 `json:"attempt"`; State string `json:"state"`; Reason model.Reason `json:"reason,omitempty"` }
  // State: "released" | "not-held" | "not-releasable"
  ```
  Config: `ControlListen string` (yaml `control_listen`, default `":8082"`), `ControlTokenFile string` (yaml `control_token_file`, optional; empty means no control server). Validation: when set, `ControlListen` differs from `Listen` and `UsageListen`.

HTTP: auth exactly like `usage` (one `Authorization` header, sha256 + constant-time compare, 401 with `WWW-Authenticate: Bearer realm="ci-dispatch control"`). Path exactly `/v1/holds/{job_id}/{attempt}/release` with positive ids (else 404/400). `POST` only (405, `Allow: POST`). Body ≤ 4 KiB, JSON with unknown fields refused, `actor_id > 0`, `actor_name` 1–255 bytes, `via` in `api|button|comment` (else 400). Decision: no launch row → `not-held`; an active row → `not-held`; an uncleared hold `launch-failing` → releasable; an uncleared hold with another reason → `not-releasable` with it; else in back-off (`gate.BackoffUntil(...)` after now) → releasable as `backoff`; else `not-held`. A releasable key with `manual >= ManualCap` in `CapWindow` → 429, `{"error":"release cap reached"}`, `Retry-After` in seconds. Owner, repo and class come from `LastLaunch`. On release: write the row, call `onRelease(via)`, log `"released"` with key, actor and via. Responses use `no-store` and `nosniff` like `usage.writeJSON`.

- [ ] **Step 1: Write the failing tests** (`control_test.go` uses a real ledger in `t.TempDir()`, like `usage_test.go:openLedger`):

```go
func TestControlAuth(t *testing.T)            // no header, wrong token, two headers → 401 with the realm; right token → not 401
func TestControlRoutes(t *testing.T)          // GET → 405 Allow: POST; /v1/holds/1/1 → 404; /v1/holds/0/1/release → 400;
                                              // via "auto" → 400; unknown field → 400; 5 KiB body → 400
func TestReleaseLaunchFailing(t *testing.T)   // 3 error undos + hold launch-failing → 200 released; ledger row with actor and via; Tries 0
func TestReleaseBackoff(t *testing.T)         // 1 error undo just now → 200 released, reason backoff
func TestReleaseNotReleasable(t *testing.T)   // table: no-budget, owner-limit, global-cap, label, denied, invalid, concurrency → 200 not-releasable, no row
func TestReleaseNotHeld(t *testing.T)         // no launches → not-held; launches but no hold and no back-off → not-held
func TestReleaseWhileActive(t *testing.T)     // an active row for the key → not-held, no row
func TestReleaseCap(t *testing.T)             // 3 releases → 200; the 4th → 429 with Retry-After; an auto release does not count
```
`config_test.go`: `TestControlOptional` (minimal config → `ControlTokenFile == ""`), a `TestLoadRejects` row for `control_listen: ":8081"` with a token file. `main_test.go`: a setup with a control token file → POST without the token is 401, with it reaches the handler (404 for an unknown key's path shape is fine); `checkNoLeak` covers the control token sentinel; the `"serving"` line carries `control_listen`.

- [ ] **Step 2: Run them and see them fail** (`./internal/control/ ./internal/config/ ./cmd/ci-dispatch/`). Expected: FAIL.
- [ ] **Step 3: Implement** the package, config fields and the wiring (read with `readSecret`, append to `servers`/`died` so `shutdown` covers it). Document the route, the token and the two config keys in `README.md` next to the usage API.
- [ ] **Step 4: Run the three packages with `-race`.** Expected: PASS.
- [ ] **Step 5: Commit** — `control: release a held job (POST /v1/holds/{job}/{attempt}/release)`

### Task 8: Automatic second round; releases in the usage API and metrics

**Files:**
- Modify: `internal/loop/loop.go` (after the `holds` phase), `internal/gate/gate.go`, `internal/usage/usage.go` (`Usage :346`, `Store :62-71`), `internal/metrics/metrics.go`, `cmd/ci-dispatch/main.go` (pass `m.Release` as `onRelease`)
- Test: `internal/loop/aci_test.go`, `internal/usage/usage_test.go`, `internal/metrics/metrics_test.go`

**Interfaces:**
- Consumes: Tasks 1 and 7.
- Produces: `const gate.AutoReleaseAfter = time.Hour`; `func (s *step) autoRelease()`; usage field `Releases []Released \`json:"releases"\`` with `type Released struct { JobID, Attempt, RepoID int64; At string; ActorID int64; ActorName, Via string }` (JSON `job_id`, `attempt`, `repo_id`, `at` RFC3339 UTC, `actor_id`, `actor_name`, `via`), for the owner and period, oldest first; `usage.Store` gains `Releases`. Metric `ci_dispatch_releases_total{via}` (via `api|button|comment|auto`) through `func (m *Metrics) Release(via string)`.

`autoRelease`: for each uncleared hold with reason `launch-failing`, a paid class, `now - Since >= AutoReleaseAfter`, and no earlier `auto` release for the key (`ReleaseCounts(k, time.Time{})`), write `Release{Via: ViaAuto, ActorName: "ci-dispatch"}`, count it, and log `"auto release"`.

- [ ] **Step 1: Write the failing tests**

```go
func TestAutoReleaseAfterAnHour(t *testing.T)      // paid key held launch-failing at t: +59m no release; +60m one auto release; next tick it launches
func TestAutoReleaseOnce(t *testing.T)             // it fails 3 more times → held again; +2h → still one auto release
func TestNoAutoReleaseForTiny(t *testing.T)        // a tiny launch-failing hold +2h → no release
func TestAutoReleaseSurvivesRestart(t *testing.T)  // auto release, restart, fail again, +2h → still one
func TestUsageListsReleases(t *testing.T)          // two releases for the owner in the period, one for another owner → two, oldest first, fields as above
```
`metrics_test.go`: the new family in `allowed`, `want`, `busy()`.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `./internal/...` with `-race`.** Expected: PASS.
- [ ] **Step 5: Commit** — `loop: one automatic release for a paid launch-failing hold after an hour`

### Task 9: Oldest waiting job per class

**Files:**
- Modify: `internal/metrics/metrics.go`, `internal/loop/loop.go` (next to `jobGauge :1053`, using `lp.firstSeen :214`)
- Test: `internal/metrics/metrics_test.go`, `internal/loop/aci_test.go`

**Interfaces:**
- Produces: `ci_dispatch_oldest_waiting_seconds{class,reason}` (the five metric classes × the 13 `model.Reason` values, present from start at 0) through `func (m *Metrics) SetOldestWaiting(map[metrics.Waiting]float64)` with `type Waiting struct{ Class model.Class; Reason model.Reason }`. Plan 2's alert reads it.

Per tick, after `plan`: for each decision whose action is `wait` or `hold`, whose job is `waiting`, and whose key has no active launch row, age = `now - lp.firstSeen[k]` (skip a key with no entry); keep the max per (class, reason). A pod restart resets ages; accepted.

- [ ] **Step 1: Write the failing tests**

```go
func TestOldestWaitingGauge(t *testing.T) // a paid job waits not-ready 31 fake min → gauge{small,not-ready} ≈ 1860;
                                          // it launches → gauge{small,not-ready} 0
func TestOldestWaitingSkipsActive(t *testing.T) // a key with an active launch row is not counted
```
`metrics_test.go`: the family in `allowed`, `want`, `busy()` with out-of-set values dropped.

- [ ] **Step 2: Run them and see them fail.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `./internal/...` with `-race`.** Expected: PASS.
- [ ] **Step 5: Commit** — `metrics: the oldest waiting job per class and reason`

### Task 10: Whole-branch check and PR

**Files:** none new.

- [ ] **Step 1: Run the full suite as CI does**

Run: `nix shell nixpkgs#go -c sh -c 'gofmt -l . && GOMAXPROCS=4 taskset -c 0-3 go vet ./... && GOMAXPROCS=4 taskset -c 0-3 go test -race -p=4 ./...'`
Expected: no `gofmt` output, vet clean, all packages `ok` (including `deps_test.go`: no new modules).

- [ ] **Step 2: Push the branch and open a PR** to `realliance-ops/ci-dispatch` `main` titled `v0.5.0: stale-job hardening`, linking the spec and this plan. CI on the forge must pass.

- [ ] **Step 3: Stop.** Report the PR and the branch head. Merging after the whole-branch review is pre-approved; the v0.5.0 tag and the image pin (plan 2) wait for Chris.
