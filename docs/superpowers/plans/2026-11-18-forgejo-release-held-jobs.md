# Forgejo fork: release a job ci-dispatch holds — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A member with Actions write on a repo can release the jobs ci-dispatch holds in a run, through the API, a Retry button on the held note, or a `/retry` PR comment, and the forge runs it.

**Architecture:** One new patch in the `realliance/forgejo` queue. `services/realliance/ciusage` gains a `Release` client for ci-dispatch's control route and a `ReleaseRun` service that releases every held job of a run's latest attempt. Three entry points call `ReleaseRun` after Forgejo's own permission check: `POST /api/v1/repos/{owner}/{repo}/actions/runs/{run_id}/release`, a web `POST …/actions/runs/{run}/release` behind the held note's Retry button, and a notifier on PR comments that reacts to `/retry` and replies as `ci-dispatch`. Then the release, the pin and the config in luma-homeops.

**Tech Stack:** Go (Forgejo 15.0.11 + patch queue), Vue (`RepoActionView.vue`), Fluent-style JSON locale, SQLite integration tests; luma-homeops Argo + Sealed Secrets.

**Spec:** `docs/superpowers/specs/2026-11-17-ci-stale-jobs-design.md` §9. Plan 3 of 4; consumes plan 1's control route (`POST /v1/holds/{job_id}/{attempt}/release`, body `{"actor_id","actor_name","via"}`, answers `{"job_id","attempt","state","reason"}` with state `released|not-held|not-releasable`, 429 `{"error":"release cap reached"}` + `Retry-After`) and plan 2's Service port `http://ci-dispatch.ci-dispatch.svc.cluster.local:8082` and token `CI_DISPATCH_CONTROL_TOKEN`.

## Global Constraints

- Fork repo `~/Repos/realliance-forgejo`; first `git pull --ff-only` (local `main` is 5 behind `origin/main` `5ce53eb`, which is `.6`). Work in `nix develop`; `make apply`, commit inside `upstream/` on branch `realliance`, `make export`, `make check`. One new patch, `Upstream: not-needed`, with the session's `Co-Authored-By` / `Claude-Session` trailers.
- No migration, table, column, `xorm` tag, queue or cron task (`scripts/check-patches.sh`). Background work uses a goroutine with `graceful.GetManager().ShutdownContext()` and a bound, never a queue.
- Config: `[realliance.ci_usage] CONTROL_URL` (trimmed, no trailing `/`; empty turns release off) and `CONTROL_TOKEN`; `BOT_USER` defaults to `ci-dispatch`. The release call reuses `TIMEOUT` (3 s default).
- Permission everywhere: the doer has write on the repo's Actions unit (`access_model.GetUserRepoPermission` + `CanWrite(unit.TypeActions)`; the API uses `reqToken(), reqRepoWriter(unit.TypeActions)`).
- `via`: `api` for the API route, `button` for the web route, `comment` for `/retry`.
- `/retry`: a comment whose first line, trimmed, is exactly `/retry`, on a pull request, from anyone but the bot user. Reply as the bot user in one line: what it released, that nothing was held, that the commenter lacks access, or that the cap was reached.
- New integration tests are named `TestActionsRealliance…` so `scripts/test-go.sh`'s `INTEGRATION_RUN` (`^(TestActions|…)`) runs them; add `./services/realliance/...` to `UNIT_PACKAGES` in `scripts/test-go.sh`.
- Local Go runs: `GOMAXPROCS=4 taskset -c 0-3 go test -p=4 -tags 'sqlite sqlite_unlock_notify' …`; the full integration suite runs on the fork's `test` workflow (`medium`).
- Chris authorized the rollout (2026-11-18). Deploy order (docs/forgejo.md, Fork image): ci-dispatch v0.5.0 first (plan 2 done), no Actions job running, keep-warm DaemonSet Ready on every node Forgejo can land on, then Forgejo's Recreate; watch with Monitor.
- Never print a token. `git diff/show/grep` in luma-homeops with `--no-textconv` and `':!*.secret.*'`.

## Review Focus

1. The bot's own reply re-triggering the notifier (a `/retry` loop): skip comments by the bot user (Task 4 `TestActionsReallianceRetryIgnoresBot`).
2. ci-dispatch down or slow: posting a comment or pressing Retry must not hang the request; the API answers 503 with the error, the comment path replies "CI is unreachable" (Tasks 1–4, fake server returning 503 and one that sleeps past `TIMEOUT`).
3. A PR from a fork repo: permission is checked on the base repo, runs are looked up on the base repo (Task 4).
4. A run whose latest attempt has no waiting jobs: the API answers 200 with an empty list, the comment says nothing was held (Task 2, Task 4).
5. A doer with read but not write on Actions: API 403, no button, comment reply "needs write access to Actions" (Tasks 2–4).

---

### Task 1: Config and the release client

**Files (inside `upstream/`):**
- Modify: `modules/setting/realliance.go` (`CIUsage` struct and `loadReallianceFrom`)
- Create: `services/realliance/ciusage/release.go`, `services/realliance/ciusage/release_test.go`
- Modify (fork repo, not a patch): `scripts/test-go.sh` (`UNIT_PACKAGES`)

**Interfaces:**
- Produces:
  ```go
  // setting.Realliance.CIUsage gains: ControlURL, ControlToken, BotUser string
  var ErrReleaseCapped = errors.New("ciusage: release cap reached")
  type Released struct { JobID, Attempt int64; State, Reason string }   // State: released | not-held | not-releasable
  func ReleaseEnabled() bool                                             // CONTROL_URL and CONTROL_TOKEN both set
  func Release(ctx context.Context, jobID, attempt int64, actor *user_model.User, via string) (*Released, error)
  // errors wrap ErrUnavailable (transport, 5xx, bad body, disabled) or ErrReleaseCapped (429)
  ```

- [ ] **Step 1: Failing unit tests** (`release_test.go`, `httptest.Server` as ci-dispatch): `TestReleaseSendsActorAndVia` (POST to `/v1/holds/7/2/release`, `Authorization: Bearer <token>`, body `{"actor_id":<id>,"actor_name":"<name>","via":"api"}`, decodes `released`); `TestReleaseCapped` (429 → `errors.Is(err, ErrReleaseCapped)`); `TestReleaseUnavailable` (503, a non-JSON 200, and a server sleeping 2× `TIMEOUT` → `ErrUnavailable` within `TIMEOUT` + 1 s); `TestReleaseDisabled` (empty `CONTROL_URL` → `ReleaseEnabled()` false, `Release` → `ErrUnavailable` with no request); a `modules/setting` test that `CONTROL_URL` loses a trailing `/` and `BOT_USER` defaults to `ci-dispatch`.
- [ ] **Step 2:** `go test -tags 'sqlite sqlite_unlock_notify' ./services/realliance/ciusage/ ./modules/setting/` → FAIL.
- [ ] **Step 3:** Implement, reusing `get()`'s client settings (timeout, 4 MiB read cap, rate-limited 401 log).
- [ ] **Step 4:** Same command → PASS. Commit inside `upstream/`.

### Task 2: ReleaseRun and the API route

**Files:**
- Create: `services/realliance/ciusage/release_run.go`, `routers/api/v1/repo/action_release.go`, `tests/integration/realliance_release_test.go`
- Modify: `routers/api/v1/api.go` (actions group, next to `/dispatches` at :897), `modules/structs/action.go`, `templates/swagger/v1_json.tmpl` (`make generate-swagger`), `tests/integration/realliance_ci_usage_test.go` (the fake answers `POST /v1/holds/{job}/{attempt}/release`)

**Interfaces:**
- Consumes: Task 1.
- Produces:
  ```go
  func ReleaseRun(ctx context.Context, run *actions_model.ActionRun, doer *user_model.User, via string) ([]Released, error)
  // every job of the run whose Attempt is the run's latest, Status waiting, TaskID 0; stops at ErrReleaseCapped
  // structs: ActionRunRelease{ Jobs []ActionJobRelease `json:"jobs"` }; ActionJobRelease{ JobID, Attempt int64; State, Reason string } (json job_id, attempt, state, reason)
  ```
  Route: `m.Post("/runs/{run_id}/release", reqToken(), reqRepoWriter(unit.TypeActions), mustNotBeArchived(), repo.ReleaseRunHolds)`; the run is looked up by id within the repo (404 otherwise). 200 with the list; 429 on the cap; 503 when release is off or ci-dispatch is unavailable.

- [ ] **Step 1: Failing integration tests:** `TestActionsReallianceReleaseRun` (a waiting job with a hold in the fake → 200, `[{job_id, attempt, state:"released"}]`, the fake saw `via:"api"` and the doer's id and name); `TestActionsReallianceReleaseNothingWaiting` (200, `jobs: []`, no call to the fake); `TestActionsReallianceReleaseNeedsWrite` (read-only collaborator token → 403); `TestActionsReallianceReleaseCapped` (fake 429 → 429); `TestActionsReallianceReleaseUnavailable` (fake 503 → 503). `TestAPIv1Permissions` keeps passing.
- [ ] **Step 2:** Build the integration binary and run `-test.run '^TestActionsReallianceRelease|^TestAPIv1Permissions'` → FAIL.
- [ ] **Step 3:** Implement; regenerate swagger.
- [ ] **Step 4:** Same run → PASS. Commit inside `upstream/`.

### Task 3: The Retry button

**Files:**
- Modify: `routers/web/web.go` (actions group, next to `/runs/{run}/cancel`), `routers/web/repo/actions/held_note.go`, `routers/web/repo/actions/view.go` (`ViewHeldNote`), `web_src/js/components/RepoActionView.vue` (~598), `web_src/js/components/RepoActionView.test.js`, `options/locale_next/locale_en-US.json`, `tests/integration/realliance_held_note_test.go`

**Interfaces:**
- Produces: web `POST /{owner}/{repo}/actions/runs/{run}/release` (`reqRepoActionsWriter`, run by index) → JSON `{"released": n, "message": "<locale text>"}`; `ViewHeldNote` gains `RetryURL string \`json:"retryURL,omitempty"\`` and `RetryText string \`json:"retryText,omitempty"\``, set only when the doer can write Actions and `ReleaseEnabled()`.

- [ ] **Step 1: Failing tests:** `TestActionsReallianceHeldNoteRetryButton` (writer sees `retryURL`; a reader sees the note without it); `TestActionsReallianceWebRelease` (POST with CSRF → 200, `released: 1`, the fake saw `via:"button"`); the Vue test renders a Retry button when `retryURL` is set and none when it isn't.
- [ ] **Step 2:** Run the integration tests and `npx vitest run web_src/js/components/RepoActionView.test.js` → FAIL.
- [ ] **Step 3:** Implement: the button POSTs, then shows the message and reloads the job view. Locale keys `realliance.ci_usage.retry`, `realliance.ci_usage.retry_released`, `…retry_nothing`, `…retry_capped`, `…retry_unavailable`.
- [ ] **Step 4:** PASS. Commit inside `upstream/`.

### Task 4: `/retry` on a pull request

**Files:**
- Create: `services/realliance/ciusage/notifier.go`, `tests/integration/realliance_retry_comment_test.go`
- Modify: the registration point (`init()` in `notifier.go`, like `services/mirror/notifier.go`), `options/locale_next/locale_en-US.json`

**Interfaces:**
- Produces: `type retryNotifier struct{ notify_service.NullNotifier }` with `CreateIssueComment(ctx, doer, repo, issue, comment, mentions)`.

Behavior: return at once unless `ReleaseEnabled()`, `issue.IsPull`, the first line is `/retry`, and the doer is not the bot user. Then, in a goroutine bounded at 30 s on the shutdown context: check permission on the base repo; load the PR; resolve its head commit (`GetRefCommitID(pr.GetGitRefName())`); find runs with `RepoID` = base repo, `CommitSHA` = head and `PullRequestID` = the PR's id; `ReleaseRun(…, "comment")` each; reply with `issue_service.CreateIssueComment` as `GetUserByName(BotUser)`. Replies (locale, one line): `realliance.ci_usage.retry_comment_released` ("Released %d held job(s); they start shortly."), `…_nothing` ("Nothing is held for this pull request's latest commit."), `…_denied` ("@%s, retrying needs write access to Actions."), `…_capped`, `…_unavailable`.

- [ ] **Step 1: Failing tests:** `TestActionsReallianceRetryComment` (writer comments `/retry` → within 5 s the fake saw `via:"comment"` and a bot comment says 1 released); `TestActionsReallianceRetryDenied` (a reader → bot reply denied, no call to the fake); `TestActionsReallianceRetryIgnoresBot` (the bot comments `/retry` → nothing); `TestActionsReallianceRetryNotFirstLine` (`thanks\n/retry` → nothing); `TestActionsReallianceRetryNothingHeld`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** PASS. Commit inside `upstream/`.

### Task 5: Export, fork CI, release

- [ ] **Step 1:** Squash Tasks 1–4's `upstream/` commits into one, subject `feat(actions): release jobs that ci-dispatch holds`, body summarizing the three entry points and the config keys, trailer `Upstream: not-needed`. `make export`, `make check` → clean; `scripts/check-patches.sh` → pass.
- [ ] **Step 2:** Fork commits, signed, following the repo's habit: `patches: release jobs that ci-dispatch holds, the failing tests first`, then `…, the code` (plus the `test-go.sh` change). Push a branch; open a PR to `main`; dispatch the `test` workflow on it and wait for success (`make check`, unit, integration).
- [ ] **Step 3:** Merge (pre-approved). Signed tag `v15.0.11-realliance.7` on the merge commit, push it; watch `image.yml` (`build` on `medium`, `publish` on `tiny`) to success and read the pin line it prints (`…realliance.7@sha256:<digest>`).

### Task 6: Pin, config and rollout (luma-homeops)

**Files:**
- Modify: `cluster/applications/forgejo-release.yaml` (`:42` pin; `:319-324` adds `CONTROL_URL: http://ci-dispatch.ci-dispatch.svc.cluster.local:8082`), `cluster/forgejo/ci-usage.secret.yaml` → resealed `cluster/forgejo/ci-usage.yaml` (adds `CONTROL_TOKEN=` from `CI_DISPATCH_CONTROL_TOKEN`), `scripts/test_forgejo_image.py` (`FORK_PIN`/pin test, `test_fork_config :240` expects `{"URL": USAGE_URL, "CONTROL_URL": CONTROL_URL}` and that `CONTROL_URL` matches the Service's `control` port), `docs/forgejo.md` (Fork config bullet `:424`, the release-notes line for `.7`), `docs/ci.md` (the control token's Forgejo copy; "Releasing a held job" gains `/retry`, the button and `fj actions retry`)

- [ ] **Step 1:** Test changes first; `nix develop -c python -m pytest -q scripts/test_forgejo_image.py` → FAIL.
- [ ] **Step 2 (controller only):** add the `CONTROL_TOKEN=<value>` line to the `realliance.ci_usage` value with `yq` and `strenv` (no printing); `rm cluster/forgejo/ci-usage.yaml && nix develop -c ./sign.sh`.
- [ ] **Step 3:** Pin, `CONTROL_URL`, docs. Test → PASS. Commit signed: `forgejo: fork v15.0.11-realliance.7 (release jobs that ci-dispatch holds)`.
- [ ] **Step 4: Pre-checks (S1).** Plan 2 is deployed (ci-dispatch on v0.5.0). No Actions job running (Site administration, Runners all Idle/Offline; or `GET /api/v1/admin/actions/runners/jobs` empty of running). `kubectl -n forgejo get ds forgejo-keepwarm` and its pods: note desired vs ready with `top` down — if the DaemonSet wants a pod on `top` that can never be Ready, stop and rule (the `.6` rollout on 2026-11-17 is the precedent to check first in Argo's history).
- [ ] **Step 5: Push and watch** with Monitor: keep-warm pulls `.7` on every schedulable node, then Forgejo's Recreate (1–3 min down), Forgejo Ready, `/api/healthz` 200.
- [ ] **Step 6: Verify:** `POST /api/v1/repos/<a repo with a recent finished run>/actions/runs/<id>/release` with Chris's forge token → 200 `jobs: []`; a read-only token → 403; the ci-dispatch log shows no control errors. The first real held job gets released through `/retry` and its run starts — note it in the ledger when it happens.
