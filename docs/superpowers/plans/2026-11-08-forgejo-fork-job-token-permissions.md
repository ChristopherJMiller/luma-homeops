# Forgejo fork job token permissions: implementation plan

<!-- vale write-good.TooWordy = NO -->
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
<!-- vale write-good.TooWordy = YES -->

**Goal:** a job's runner gets only the secrets its job names, and a workflow's `permissions:` sets what the job token may do, over an instance default and a repo override. A job on a protected ref publishes its own repo's images with `packages: write`. Then the three static registry tokens retire and the default flips to `restricted`. Forge users may create their own orgs, which neither escape forge-bot nor get paid CI.

**Architecture:**
- **Fork.** Three new patches end the `realliance/forgejo` queue, in one PR and one release `v15.0.11-realliance.N`.
  - A filters `getSecretsOfTask` by the secrets the task job's payload names.
  - B adds the leaf package `services/actions/jobtoken`. Its `Resolve` decides every right of a task's token from the workflow file at the run's source commit, the repo override and the instance default. The four call sites that hard-code "own repo write, fork read" call it.
  - C's rule in `services/packages` lets that token write a container or generic package named after its repo, from a protected ref, and `/v2/token` carries the task ID into the package JWT.
- **Bots.** forge-bot `v0.2.0` reads Owners only from `owner_orgs` in `forge-bot.yaml`. ci-dispatch `v0.4.0` gives an org without an `owners:` entry `org_defaults`, with no paid classes. A cached `/admin/orgs` list and a `/repositories/<id>` owner check classify each job and fail closed.
- **Rollout.** luma-homeops pins the fork digest with `DEFAULT_TOKEN_PERMISSIONS: permissive` and opens org creation. Then the four publishers move to the job token one at a time, the static tokens retire, and the default flips.

**Tech Stack:** Forgejo 15.0.11 (Go 1.26.10, `go.yaml.in/yaml/v3`, `github.com/hashicorp/golang-lru/v2`, `xorm` on SQLite in tests); forgejo-runner v13.2.1; Forgejo Actions on `medium` (ACI), `tiny`, `galaxy` and `general`; crane v0.22.1 and ko; the Go 1.26 standard library for forge-bot and ci-dispatch; pytest in `realliance/sso`; the OpenTofu inventory `realliance-ops/forge`; Argo CD with forgejo-helm 17.3.2; `promtool`.

**Spec:** `docs/superpowers/specs/2026-11-08-forgejo-fork-job-token-permissions-design.md`, approved by Chris on 2026-11-08 with its amendment box. Read it whole, beside this plan; section names below are the spec's. House style comes from `docs/superpowers/plans/2026-11-06-forgejo-fork-image.md` (fork release, rollout, keep-warm, fallback, acceptance) and `docs/superpowers/plans/2026-11-05-runner-classes-phase-2.md` (dispatcher releases). Gate G and the STOP form come from `docs/superpowers/plans/2026-11-04-runner-classes-phase-0-1.md`.

## Global Constraints

### From the spec

- No migrations, no new `xorm` tag, nothing `scripts/check-patches.sh` flags. Settings live in app.ini and in the Actions unit's JSON as `ActionsConfig.TokenPermissions`, `json:",omitempty"`, where empty means to follow the instance.
- Three patch files: A `Upstream: candidate`; B and C `Upstream: not-needed`. One PR, one release, about 700 production lines, and one resolver that decides every right of the token.
- `[actions] DEFAULT_TOKEN_PERMISSIONS` takes `permissive` (every repo unit write, packages read) or `restricted` (contents and packages read, everything else none). It stays `permissive` until Task 22.
- Scopes are GitHub's keys only:
  - `contents` covers `TypeCode`, `TypeReleases`, `TypeWiki`, plus read on the external wiki and tracker;
  - `issues`, `pull-requests`, `actions` and `repository-projects` cover `TypeIssues`, `TypePullRequests`, `TypeActions` and `TypeProjects`;
  - `packages` is patch C's, and the repo's packages tab follows it;
  - `id-token`, `statuses`, `checks`, `deployments`, `discussions`, `security-events`, `attestations` and `pages` mean nothing here. An unknown key or a bad level reads as `none` and logs once per task.
- Levels are `read`, `write`, `none`. `read-all` and `write-all` set every key, so `write-all` includes `packages: write`; `{}` sets none; a key the block leaves out is `none`; metadata always stays readable.
- Resolution, first match wins: the job's block (it replaces the workflow's), the workflow's top-level block, the repo override, the instance default. Clamps:
  - a fork PR (`IsForkPullRequest`) gets read at most;
  - a reusable inner job gets at most its caller job's result, lowered by the called workflow's own block when local;
  - another repo keeps the `forgejo-actions` user's own rights, and the API answers 404 there.
- The resolver reads `WorkflowDirectory/WorkflowID` at `GetWorkflowSourceCommit()` and takes the top-level and `jobs.<JobID>` blocks. It caches per task ID in process memory and skips the read for a fork PR. When the read fails, it fails closed to the default and logs the task.
- Patch C's write rule, all six must hold:
  1. `packages` resolves to `write`;
  2. the package owner is the task repo's owner;
  3. a container whose image, in lower case, is `<repo>` or starts with `<repo>/`, or a generic package named `<repo>`;
  4. not a fork PR, and `TriggerEvent` is `push`, `workflow_dispatch`, `schedule` or `release`;
  5. `Ref` is a protected branch, or a tag that a protected-tag rule matches;
  6. not a delete.
- Patch C's read rule: the task owner's packages are readable at `packages` read or better, even for a private owner.
- No silent refusals. A job holding `packages: write` that fails a rule gets a 403 naming the rule, and Forgejo logs a warning naming the task, the repo, the package type and name, and the rule. For another type the message reads: the job token's `packages: write` covers only container and generic packages named after the repository. Container routes carry it in the OCI error body.
- Patch A: `secrets.NAME` or `secrets['NAME']`, matched ignoring case, anywhere in the task job's `WorkflowPayload`. Any other use of `secrets` sends every secret. `GITHUB_TOKEN`, `GITEA_TOKEN`, `FORGEJO_TOKEN`, `ACTIONS_STEP_DEBUG` and `ACTIONS_RUNNER_DEBUG` always go. Fork PRs still get none, and an inner reusable job's caller set stays whole.
- forge-bot: `forge-bot.yaml` must hold `owner_orgs: [realliance, realliance-ops]`, and forge-bot reads it only at the Owner-signed head. If it's absent, or a listed org is missing or has no Owners team, the read fails with `ErrIncomplete`.
- ci-dispatch:
  - `ci.yaml` gains `org_defaults:` (paid 0, `tiny` as in `defaults`);
  - the guard gains two GET-only shapes, `/admin/orgs` with `page` and `limit`, and `/repositories/<id>`;
  - an `owners:` entry always wins; an id in the org list gets `org_defaults`; `defaults` apply only when the job's `owner_id` equals the current owner of its `repo_id` (cached 10 min);
  - the list refreshes with each limits load and every 10 min, and a failed read keeps the last good list; over 1 h old, or none, sends every unlisted owner to `org_defaults`;
  - a gauge for the list's age drives `CIDispatchOrgListStale`; guests stay on `defaults`.
- Forgejo config: `service.DEFAULT_ALLOW_CREATE_ORGANIZATION: true`, `admin.DISABLE_REGULAR_ORG_CREATION: false` and `actions.DEFAULT_TOKEN_PERMISSIONS: permissive`, in the Rollout step 3 values change.
- The anti-goals hold in every task: no token or secret value in a log line, an error or a fixture; no package delete by the job token; no dropped secret; no deploy that breaks a workflow.

### Execution

- Subagent-driven (superpowers:subagent-driven-development). Each implementer reads this plan and the spec. Reviewers read diffs with `--no-textconv` and with `*.secret.*` left out.
- Four threads locally: Go runs under `taskset -c 0-3` with `GOMAXPROCS=4` and `go test -p=4`; nix builds take `--cores 4`. No local Docker, no local end-to-end runs.
- Forgejo tests:
  - locally, only the unit packages through `scripts/test-go.sh unit` (Task 1), plus `go vet` of the touched packages;
  - every `tests/integration` test, `TestAPIv1Permissions` included, runs only in CI on `medium`, through `.forgejo/workflows/test.yml` (Task 1);
  - a patch task dispatches that workflow twice at most: once red, after `make export` writes its failing tests, and once green.
- Fork patches carry no database migrations (Chris, 2026-11-05).
- Never `--no-verify`, never `--no-gpg-sign`, never force-push, never rebase a pushed branch. If `main` moves under a pushed branch, merge `origin/main` into it with a signed merge commit.
- Never open `*.secret.*`, `nodes/*` or any `*-secrets` repo.
  - Every grep carries `--exclude='*.secret.*'`, or `-- ':!*.secret.*'` for `git grep`; every diff and show takes `--no-textconv`.
  - Forge reads that need the administrator token go through the `realliance/sso` harness, which loads the token itself: `cd ~/Repos/realliance-sso/tests && nix develop --command python -c '…forge_api(…)…'`.
- `tiny` mounts its root file system read-only, so everything a `tiny` job writes goes under `$RUNNER_TEMP`.
- Every commit ends with these two lines. The patch commits inside `upstream/` carry them after their `Upstream:` trailer.
  ```text
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01VEim3itci5Wu1hTGS8MxW4
  ```
- All prose (README files, docs, PR bodies, workflow headers, the ledger) passes `nix shell nixpkgs#vale -c vale <file>` with 0 errors and 0 warnings. Each README stays brief.
- Signed commits: `nix develop --command git commit -S` in luma-homeops, `git commit -S` elsewhere, with `DISPLAY=:10.0 XAUTHORITY=$HOME/.Xauthority` set. Chris unlocks GnuPG when asked.
- No new Go module: the fork already has both libraries, and forge-bot and ci-dispatch add none (`TestDependencyGuard` unchanged). ci-dispatch `v0.4.0` changes no ledger schema.
- `GO="taskset -c 0-3 nix develop $HOME/Repos/luma-homeops --command env GOMAXPROCS=4 go"` for forge-bot and ci-dispatch, as phase 2 does.
- CLAUDE.md S1–S9 apply. Read live state before each write. A rejected call may already have run.

### How changes land

| Repo | Where the agent works | Who merges |
|---|---|---|
| `realliance/forgejo` | a fresh clone, `~/Repos/realliance-forgejo-perms`, on branches `perms-ci`, `token-permissions` and `publish-job-token`. Never write in `~/Repos/realliance-forgejo` or `~/Repos/realliance-forgejo-ci`, which other sessions use | PR with `fj -H git.realliance.net pr create`; Chris merges |
| `realliance-ops/forge-bot` | worktree `~/Repos/forge-bot-owner-orgs` from `origin/main`; branches `owner-orgs`, `publish-job-token` | Chris fast-forwards and signs the tag |
| `realliance-ops/ci-dispatch` | worktree `~/Repos/ci-dispatch-org-defaults` from `origin/main`; branches `org-defaults`, `publish-job-token` | Chris fast-forwards and signs the tag |
| `realliance-ops/forge`, the inventory | worktree `~/Repos/realliance-forge-perms` from `origin/main`; branches `owner-orgs`, `org-defaults` | Chris fast-forwards, so the head stays Owner-signed |
| `realliance/realliance.net` and `realliance/actions` | worktrees `~/Repos/realliance.net-perms` (branches `split-publish`, `publish-job-token`) and `~/Repos/realliance-actions-perms` (branch `pages-contents-write`) | Chris merges |
| `realliance/sso` on GitHub | worktree `~/Repos/realliance-sso-perms` from `origin/main`; branches `job-token-acceptance`, `static-tokens-retired`, `workflow-blocks` | `gh pr create`; Chris merges |
| luma-homeops | worktree `~/Repos/luma-homeops-token`, branch `token-permissions`, made git-crypt-safe (Task 0). Before a push: `git fetch origin && git rebase --gpg-sign origin/main` (local commits only), then `git push origin HEAD:main` | a docs push is routine; a push that changes what Argo renders is a deploy and a STOP |

- **STOP—ask Chris** is phase 0–1's: one AskUserQuestion with the change, the Gate G evidence for a deploy, what to watch and the rollback trigger. Its options are `approve: do it, and roll back at once if a trigger fires` and `hold`. Chris merges every PR and pushes every release tag; the classifier blocks the session's own merges.
- **Gate G** is phase 0–1's G1–G4, before and after every deploy push. Serialize every STOP and deploy with other plans' windows (phase 0–1, N3).
- **Gate O**, the open-org gate, needs two things:
  1. forge-bot: either `v0.2.0` runs with Task 5 merged, or forge-bot isn't deployed and `forge-bot-deploy` pins `v0.2.0`;
  2. ci-dispatch: `v0.4.0` runs, and it verified Task 8's head.
- **Ledger:** `~/Repos/luma-homeops/.superpowers/sdd/2026-11-08-forgejo-fork-job-token-permissions/progress.md`, git-ignored. It holds answers, rulings, run numbers, timings, costs, digests and pins, and never a secret, a token or a hidden org name.
- **The CI loop.**
  - Dispatch with `fj -H git.realliance.net actions dispatch <workflow> <ref> -r <owner>/<repo>`.
  - Find the run with `fj -H git.realliance.net actions tasks -r <owner>/<repo>` and wait on it with `Monitor`.
  - Read the job log anonymously at `https://git.realliance.net/<owner>/<repo>/actions/runs/<run>/jobs/<job>/attempt/1/logs`; Task 0 Step 4 checks this route.

### Answered while planning

- forge-bot `v0.1.0` decodes `forge-bot.yaml` with `KnownFields(true)`. With an unknown key, `ParseBotConfig` fails and `override()` ignores `allow_bulk_until`. `forge_bot_inventory_ok{check="bot_config"}` then reads 0 (`ForgeBotInventoryInvalid`), and every job still runs. An early `owner_orgs` costs only the override, but Task 5 keeps the spec's order.
- forge-bot isn't on luma-homeops `main` on 2026-11-08. Its manifests sit on branch `forge-bot-deploy`, which the forge-bot plan owns: `sha-9be4857` (`v0.1.0`), every job in `dry-run`, `source_id: 1`. Task 4 covers both cases.
- ci-dispatch decodes `ci.yaml` strictly too, so `org_defaults` lands only after `v0.4.0` runs (Task 8).
- `services/actions` imports `services/context`, and that imports `services/packages`. A resolver inside `services/actions` can't serve patch C, so it goes in the leaf package `services/actions/jobtoken`.
- The Conan registry shares `CreateAuthorizationToken` and `ParseAuthorizationToken`, so patch C adds task variants beside them and keeps both signatures.
- Forgejo 15 accepts the runtime JWT as the job token, with the same task ID (`services/auth/method/oauth2.go:153-160`), so the resolver covers it.
- `actions/checkout` sends the job token, and forge-bot, ci-dispatch and realliance.net check out with it. Ruling R1 keeps a public repo readable to its own `{}` token.
- Basic authentication with a task token ignores the user name (`services/auth/method/basic.go:60-118`).
- Existing integration tests use fixture task 47, whose workflow file no commit holds. Under `permissive` they fall back to today's rights, so they double as the regression suite.
- `make test-sqlite#<x>` passes `-test.run` to the shell unquoted, and the upstream CI builds the server (`make backend`) before the SQLite suite, because git hooks call it. `scripts/test-go.sh` does both.
- Live pins on 2026-11-08: Forgejo `v15.0.11-realliance.2`, ci-dispatch `sha-f415caa` (`v0.3.1`), forge-bot `v0.1.0` (not deployed). Task 0 reads them again.

### Rulings this plan makes

Chris may override any of these.

| # | Ruling | Why |
|---|---|---|
| R1 | On its own repo the token never holds less than the `forgejo-actions` user has there for code, releases and wiki (Task 11 narrowed it): read access on a public repo, which lets `{}` clone a public repo. Issues and pull requests stay at the block's level, because issues read lets a Forgejo token comment | Rollout step 3 expects every workflow to pass, and three of them check out with the token. GitHub lets any token read a public repo |
| R2 | Package reads keep today's rule for every owner; `packages` read or better adds the task owner's private packages | The read rule adds rights and never takes away what anonymous has |
| R3 | The eight ignored GitHub keys drop silently; only unknown keys and bad levels log | `id-token: write` would otherwise log on every task |
| R4 | On its own repo the token's `Permission` holds `AccessMode: read` and one `UnitsMode` entry for each unit | `HasAccess()` stays true, so no 404, and nothing treats the token as a repo administrator |
| R5 | The resolver lives in `services/actions/jobtoken` | The import cycle in Answered while planning |
| R6 | Cancelling an upload (`DELETE …/blobs/uploads/<uuid>`) counts as part of a write | It deletes no blob or manifest |
| R7 | A scans only expression text: `${{ … }}` spans in any scalar, and whole `if:` values. Any other `secrets` token, or a payload that doesn't parse, sends every secret and logs the task | Plain text such as a step named "Check secrets" mustn't count, and sending more is the safe direction |
| R8 | A failed workflow read logs a warning and adds no run warning | Forgejo 15 has no run-warning channel for this |
| R9 | ci-dispatch without `org_defaults` gives orgs paid 0 and `tiny` as `defaults`; the `Kind` zero value is "org"; the usage API classifies by the org list alone | It fails closed, `v0.4.0` can deploy before `ci.yaml` carries the key, and the Usage tab shows the limits the gate applies |
| R10 | forge-bot drops `/admin/orgs` from its guard; an untrusted inventory head or a failed inventory read now stops every job | It no longer reads that route, and without `owner_orgs` no loss may run |
| R11 | The one-time `allow_create_organization` loop patches every account on the SSO login source, prohibited ones included, and no local account | Service accounts never need orgs, and a returning member keeps the flag |
| R12 | The registry login name becomes `forgejo-actions` wherever the job token logs in | Forgejo ignores the name, and `realliance-ci` would mislead |
| R13 | The four moves run right after acceptance and before the rotations. A token whose consumer can't move in that phase rotates at once instead | The spec's step 3 rotates a token at once while its consumer still needs it |
| R14 | If forge-bot still isn't deployed at Task 4, its re-pin goes on `forge-bot-deploy` with Chris's OK, and the forge-bot plan deploys it | That branch belongs to another plan |

<!-- vale Google.Headings = NO -->
## Review Focus
<!-- vale Google.Headings = YES -->

1. **A job checks out its own public repo under `{}`, or under `packages: write` alone.** A reasonable person expects the clone to work, as on GitHub. Without R1, the CI of forge-bot, ci-dispatch and realliance.net breaks at the rollout. Test: Task 11 `TestJobTokenPermissionsPublicClone`.
2. **A workflow names a secret in a form the scanner misses**, such as brackets with spaces, lower case, an `if:` without braces, container or service credentials, workflow `env` or a reusable `secrets:` mapping. The job then loses a secret it names, which is an anti-goal. Test: Task 9 `TestSecretReferences`, rows 1–15.
3. **The resolver can't read the workflow file at the source commit**: a dispatch on a since-deleted branch, a schedule on an old commit, a renamed file. Expected: the job runs under the repo override or the instance default, with one warning per task and no 500. Tests: Task 10 `TestResolveFixtureTaskUnread`, Task 11 matrix row 12.
4. **An image name only looks like the repo**: `<repo>2`, `<repo>-cache`, `<Repo>` in upper case, a generic `<repo>/x`. Expected: upper case passes, and Forgejo refuses the other three. Test: Task 12 `TestCheckTaskTarget`.
5. **ci-dispatch restarts, or Forgejo's org listing fails for a while.** A user's paid job mustn't sit at `no-budget` while the dispatcher reads its first list, and a fresh list keeps users on `defaults` through a short outage. Tests: Task 6 `TestColdStartReadsOrgsBeforeFirstPlan` and `TestOrgListFailureKeepsLastGood`.

---

## Order

| Spec step | Tasks |
|---|---|
| 1, workflow blocks before the image | 1, 2, and the `{}` blocks in 3 and 6 |
| forge-bot first | 3, 4, 5 |
| ci-dispatch before the config change | 6, 7, 8 |
| 2, the fork release | 9 to 14 |
| live acceptance tests, red before the rollout | 15 |
| 3, the rollout with the config change | 16, 17 |
| 4, the moves, one repo at a time | 18 (the fork), 19 (forge-bot, then ci-dispatch), 20 (realliance.net) |
| the rotations, and 5, the retirements | 21 |
| 6, the flip | 22 |

Tasks 9 to 13 need nothing from Tasks 2 to 8 and may run beside them. The release tag (Task 14) waits for Tasks 4 and 7. Task 16 waits for Task 14 and Gate O, or ships the fork without the org keys (Task 16 Step 2).

### Task 0: Start (no code)

**Files:** the ledger; the worktrees and the fork clone.

- [ ] **Step 1: Ledger.** Create it with the plan path, Gate O's two parts and each part's state.
- [ ] **Step 2: Worktrees.**
  - luma-homeops, git-crypt-safe:
    ```bash
    git -C ~/Repos/luma-homeops fetch -q origin
    git -C ~/Repos/luma-homeops worktree add --no-checkout ~/Repos/luma-homeops-token -b token-permissions origin/main
    cp -r ~/Repos/luma-homeops/.git/git-crypt ~/Repos/luma-homeops/.git/worktrees/luma-homeops-token/
    git -C ~/Repos/luma-homeops-token checkout token-permissions
    ```
  - The fork: `git clone https://git.realliance.net/realliance/forgejo.git ~/Repos/realliance-forgejo-perms`, then `make apply` inside `nix develop`. Expected: `make status` reports `synced`, over `753f7edc7`.
  - The other worktrees of the table, each from a fresh `origin/main`.
  - Expected: `df -h /` shows 20 GiB free or more, because the Forgejo build cache takes room. Otherwise STOP and tell Chris.
- [ ] **Step 3: Live state (S1).** Ledger each:
  - `curl -s https://git.realliance.net/api/v1/version`, and `git ls-remote --tags https://git.realliance.net/realliance/forgejo 'v15.0.11-realliance.*'`. Task 14's N is the highest plus one;
  - on luma-homeops `origin/main`, the `image.fullOverride` line in `cluster/applications/forgejo-release.yaml` and the image in `cluster/ci-dispatch/dispatcher/statefulset.yaml`;
  - forge-bot: `git -C ~/Repos/luma-homeops ls-tree origin/main cluster/forge-bot`, `kubectl get statefulset -n forge-bot` (a read), and its job modes in `cluster/forge-bot/config.yaml`;
  - the open PRs of every repo in the table (`fj … pr list`, `gh pr list`), and whether the shared-actions plan's README rewrite (its Task 6) is open on `realliance/actions`;
  - through `forge_api`, the tag and branch protections of `realliance/forgejo`, `realliance-ops/forge-bot`, `realliance-ops/ci-dispatch` and `realliance/realliance.net`. Expected: `v*`, `v*`, `v*` and `[0-9]*`, and `main` on each;
  - every workflow file in the non-mirror repos of `realliance` and `realliance-ops` (`/repos/search`, then `/contents/.forgejo/workflows`), with each job's `runs-on` and whether it names a secret, uses `actions/checkout` or calls `pages-publish`. Task 17 runs each once, and Task 22 needs a block in each.
- [ ] **Step 4: The log route.** Run `curl -s -o /dev/null -w '%{http_code}\n' https://git.realliance.net/realliance/forgejo/actions/runs/<latest image run>/jobs/0/attempt/1/logs`. Expected: `200`. Otherwise ledger the code: Task 1's workflow then also uploads its log as artifact `go-test-log`, with the `upload-artifact` fork pinned in `image.yml`.
- [ ] **Step 5: Runner masking.** Read forgejo-runner v13.2.1's `internal/pkg/report/reporter.go`, and ledger whether it masks the value of every secret the task carries, referenced or not. Task 15's first test depends on the answer.
- [ ] **Step 6: Baseline.** Gate G1, G2 and G4 into the ledger.

### Task 1: Fork CI, `permissions:` blocks and a test workflow

**Gate:** none. Spec Rollout step 1, for `image.yml`.

**Files (fork, branch `perms-ci`):**
- Change: `.forgejo/workflows/image.yml`, `scripts/test-ci.sh`, `README.md`
- Create: `.forgejo/workflows/test.yml`, `scripts/test-go.sh`

**Interfaces (produces):**
- `scripts/test-go.sh unit [<regex>]` runs `go test -p=4 -tags 'sqlite sqlite_unlock_notify'` in `upstream/`, with `GOTOOLCHAIN=go1.26.10` and `CGO_ENABLED=1`, over `UNIT_PACKAGES`: `./modules/setting/... ./models/git/... ./models/repo/... ./services/actions/... ./services/packages/... ./routers/api/packages/...`. A regular expression becomes `-run`.
- `scripts/test-go.sh integration [<regex>]` runs, in `upstream/`:
  1. `TAGS='sqlite sqlite_unlock_notify' make backend`;
  2. `make integrations.sqlite.test generate-ini-sqlite`;
  3. `GITEA_ROOT="$PWD" GITEA_CONF=tests/sqlite.ini ./integrations.sqlite.test -test.run "<regex>"` with `USE_REPO_TEST_DIR=1`.

  The default regular expression is `INTEGRATION_RUN='^(TestActions|TestJobToken|TestAPIv1Permissions|TestPackageContainer|TestPackageGeneric|TestAPIRepoTags|TestAPIRemoveIssueLabel|TestAPIReplaceIssueLabelsActionsToken|TestAPILFS|TestAPINewWikiPage)'`.
- Run as root, as on `medium`, either mode first creates an unprivileged user `forgejo` and runs itself again as that user, as the upstream CI does, with `TMPDIR` under `$RUNNER_TEMP`.
- `.forgejo/workflows/test.yml`:
  - `name: test`; `on: workflow_dispatch: {}` and nothing else; `concurrency: {group: test, cancel-in-progress: false}`;
  - one job `go`, with `runs-on: medium`, `timeout-minutes: 40` and `permissions: {}`;
  - steps: the anonymous fetch at `$GITHUB_SHA`, as in `image.yml`; `make check`, then `make apply`, with `GIT_COMMITTER_NAME` and `GIT_COMMITTER_EMAIL` in the job's env, as in `image.yml`; `scripts/test-go.sh unit`; `scripts/test-go.sh integration`;
  - every step has a `- name:` and blanks `GITHUB_TOKEN`, `FORGEJO_TOKEN` and `ACTIONS_RUNTIME_TOKEN` in its own `env:`;
  - no `secrets.`, no `actions/checkout`, no `github.token`, and no `uses:` unless Task 0 Step 4 failed.

- [ ] **Step 1: Failing tests** in `scripts/test-ci.sh`:
  - `workflow` also requires `    permissions: {}` in `build` and in `publish`, by the index's job column, and still exactly one `secrets.` line;
  - a new case `test-workflow`, in `all` and `no_tools`, checks `.forgejo/workflows/test.yml` for every point under Interfaces: the one trigger, no `secrets.`, none of `actions/checkout`, `github.token`, `enable-openid-connect` or `ACTIONS_ID_TOKEN`, the job keys, the blanks in every run step, the anonymous fetch, and both `scripts/test-go.sh` calls;
  - a new case `test-go`, in `no_tools`: `bash scripts/test-go.sh --help` exits 0, and an unknown mode exits 2.

  Run `bash scripts/test-ci.sh workflow test-workflow test-go`. Expected: fail.
- [ ] **Step 2: Write** the blocks, `test.yml` and `scripts/test-go.sh`: `shellcheck` clean, `--help` from the header, no trailing comments on command lines. `image.yml`'s header says that Forgejo 15 ignores `permissions:`, that the blocks bite once the fork image carries patch B, and that `publish` keeps `{}` while it uses `REGISTRY_TOKEN`. The README's Commands list gains one line each for `scripts/test-go.sh` and the `test` workflow. vale.
- [ ] **Step 3: Run** `bash scripts/test-ci.sh no-migrations version workflow test-workflow test-go`, then `taskset -c 0-3 nix develop .#build --command env GOMAXPROCS=4 scripts/test-go.sh unit`. Expected: pass, the unit run on today's queue.
- [ ] **Step 4: Commit** signed, as `ci: permissions blocks in image.yml; a dispatch-only Go test workflow on medium`. Push `perms-ci`, dispatch `test.yml` on it, and `Monitor`. Expected: success. Ledger the wall time, the cost from the usage API and the slowest test. A run over 30 min is a finding: split the selection into two jobs before Task 9.
- [ ] **Step 5: PR**, then **STOP—ask Chris** to merge `perms-ci`.

### Task 2: realliance.net and `pages-publish` blocks

**Gate:** none. Spec Rollout step 1.

**Files:**
- realliance.net (`split-publish`): change `.forgejo/workflows/ci.yaml`
- `realliance/actions` (`pages-contents-write`): change `README.md`, the `pages-publish` section
- luma-homeops: change `docs/pages.md`

- [ ] **Step 1: Split** realliance.net's `ci.yaml`:
  - `build`, on `general` with `permissions: {}`: checkout with `persist-credentials: false`, `scripts/ci.sh build`, then upload `.ci/image/oci` and `.ci/image/digest` as artifact `image`, with the `upload-artifact` fork pinned in `image.yml` (`16871d9e8cfcf27ff31822cac382bbb5450f1e1e`). It outputs the digest;
  - `publish`, with `needs: build`, `if: github.event_name == 'push'`, `runs-on: general` and `permissions: {}`: checkout, download `image` into `.ci/image` with the pinned `download-artifact` fork (`d8d0a99033603453ad2255e58720b460a0555e1e`), and check its digest against `needs.build.outputs.digest` in a step without the token. Then comes `scripts/ci.sh publish`, the only step with `SITE_REGISTRY_TOKEN`;
  - the header explains the split (patch A sends a PR run no secret) and that `permissions:` bites only on the fork image.

  Check: `grep -n 'secrets\.' .forgejo/workflows/ci.yaml` prints one line, in `publish`'s last step, and `git check-ignore .ci/image/digest` prints the path, so `ci.sh publish`'s clean-tree check ignores the artifact.
- [ ] **Step 2: PR.** Commit signed, push, open it. Expected: the PR run runs `build` alone and passes. **STOP—ask Chris** to merge, then `Monitor` the `main` run. Expected: both jobs pass, and the log ends with `git.realliance.net/realliance/realliance.net:sha-<7>@sha256:<64>`. A `main` push publishes `sha-<7>` only, which no Renovate rule deploys.
- [ ] **Step 3: The `pages-publish` README.** One paragraph plus `permissions: {contents: write}` on the sample job: the job that runs `pages-publish` needs `contents: write`, and from the `restricted` default on, a job without it can't push the `pages` branch. If the shared-actions plan's README rewrite is open (Task 0 Step 3), add this to that PR through Chris instead. vale. PR, then **STOP—ask Chris** to merge.
- [ ] **Step 4: luma-homeops** `docs/pages.md`: the same block in its sample. vale, commit signed, push to `main` (docs).
- [ ] **Step 5: Chris** posts the announcement to members. Draft it in the ledger in three sentences: the change (`contents: write` on the publishing job), when (Task 22's flip, date to follow), and the README link.

### Task 3: forge-bot reads Owners from `owner_orgs`

**Gate:** none.

**Files (worktree `~/Repos/forge-bot-owner-orgs`, branch `owner-orgs`):**
- Change: `internal/inventory/parse.go`, `internal/model/model.go`, `internal/cycle/read.go`, `internal/forgejo/client.go`, `internal/forgejo/guard.go`, `internal/metrics/alerts.yaml`, `internal/metrics/alerts_test.yaml`, `README.md`, `.forgejo/workflows/ci.yaml`
- Test: `internal/inventory/parse_test.go`, `internal/forgejo/client_test.go`, `internal/forgejo/writes_test.go`, `internal/cycle/cycle_test.go`, `internal/cycle/fakes_test.go`

**Interfaces (produces):**
- `inventory.ParseOwnerOrgs(b []byte) ([]string, error)` returns `forge-bot.yaml`'s `owner_orgs`. It returns `ErrNoOwnerOrgs` when the key is absent, null or empty, and `ErrBadOwnerOrg` for a name that fails `nameRE`, is `.` or `..`, or repeats another one ignoring case.
- `BulkUntil`, `ParseBotConfig` and `ParseOwnerOrgs` decode one struct with `allow_bulk_until` and `owner_orgs`, so each accepts both keys and needs neither.
- `model.Inventory.OwnerOrgs []string` holds the list only at a trusted head whose `forge-bot.yaml` carries a valid one, and nil otherwise.
- `(*forgejo.Client).Owners(ctx context.Context, orgs []string) (map[string]bool, error)` reads the teams and the Owners members of the listed orgs only. It returns `ErrIncomplete` when `orgs` is empty, a listed org answers 404, or one has no Owners team.
- `readForge` passes `inv.OwnerOrgs`; the cycle's Forgejo interface, and `fakes_test.go`, take the new signature.
- The guard's GET list keeps `^/admin/users$` and drops `orgs`.

- [ ] **Step 1: Failing tests:**
  - `TestParseOwnerOrgs`: `owner_orgs: [realliance, realliance-ops]` gives both; absent, `null` and `[]` give `ErrNoOwnerOrgs`; `[a/b]`, `[..]` and `[A, a]` give `ErrBadOwnerOrg`;
  - `TestBotConfigAcceptsOwnerOrgs`: a file with both keys parses in `ParseBotConfig` and `BulkUntil`, and a third key still fails;
  - `TestOwnersListedOrgsOnly`: the fake holds `realliance`, `realliance-ops` and `userorg`, each with an Owners team (`boss`, `ops`, `mallory`). `Owners(ctx, []string{"realliance", "realliance-ops"})` returns `{boss, ops}`, and the fake saw no `/admin/orgs` request;
  - `TestOwnersEmptyListIsIncomplete`, `TestOwnersListedOrgMissingIsIncomplete` (a 404), and the existing test for an org without an Owners team, on the new signature;
  - `TestGuardAllowList`: the guard refuses `GET /api/v1/admin/orgs`, and `/admin/users` still goes;
  - `TestOwnerOfUserOrgLosesAccessAdminAndTeams`: `mallory` owns `userorg` only, is a site administrator, sits in a mapped `realliance` team and has left the SSO access group, with `owner_orgs: [realliance, realliance-ops]`. Expected plan: prohibit login, remove the site administrator flag, remove from the team, and no `owner_needs_action` finding;
  - `TestNoOwnerOrgsStopsEveryJob`: at a trusted head whose `forge-bot.yaml` lacks the key, the Forgejo read fails, every job plans zero changes, `forge_bot_read_ok{source="forgejo"}` is 0 and `forge_bot_inventory_ok{check="bot_config"}` is 0;
  - `TestUntrustedHeadStopsEveryJob`: an unsigned head gives zero changes in every job, where before only the teams job stopped.

  Run `$GO test -race -p=4 ./...`. Expected: fail.
- [ ] **Step 2: Write the code** for what the Interfaces say.
- [ ] **Step 3: Run** `$GO vet ./...` and `$GO test -race -p=4 ./...`. Expected: pass.
- [ ] **Step 4: Alerts and README.**
  - `ForgeBotInventoryUntrusted`'s summary becomes `The inventory head is not signed by an Owner, so forge-bot can't read owner_orgs and every job stops (docs/forgejo.md#forge-bot)`. `ForgeBotInventoryInvalid`'s says that for `bot_config` forge-bot drops the override, and that without a valid `owner_orgs` every job stops. `alerts_test.yaml` follows. Run both `promtool` commands from the README. Expected: success.
  - README: the "Complete reads or nothing" row (a failed inventory read, an untrusted head or a missing `owner_orgs` stops every job), the Owners row at `README.md:61` (only the Owners of the orgs in `owner_orgs`; an Owner of any other org is an ordinary account), and the `owners_unknown` and `owner_needs_action` rows. vale.
- [ ] **Step 5: Blocks.** In `.forgejo/workflows/ci.yaml`, `test` and `image` get `permissions: {}`. The header's "there is no `permissions:` key" becomes: Forgejo ignores `permissions:` until the fork image carries patch B, and `image` keeps `{}` while it uses `REGISTRY_TOKEN`.
- [ ] **Step 6: Commit** signed, in two commits, `forge-bot: Owners only from owner_orgs in forge-bot.yaml` and `ci: permissions blocks`. Push `owner-orgs` and open the PR. Expected: the PR run passes; `image` skips on a PR.

### Task 4: forge-bot `v0.2.0`, released and pinned

**Gate:** after Task 3's PR passes.

**Files (luma-homeops):** `cluster/forge-bot/statefulset.yaml` (the pin, at line 59 on `forge-bot-deploy`), `cluster/forge-bot/prometheusrule.yaml`, `cluster/forge-bot/alerts_test.yaml`.

- [ ] **Step 1: STOP—ask Chris** to fast-forward `owner-orgs` into `main` and push the signed tag `v0.2.0`. `Monitor` the tag run. Ledger the `image` job's `sha-<7>`, and the digest from an anonymous `crane digest git.realliance.net/realliance-ops/forge-bot:sha-<7>`.
- [ ] **Step 2: STOP—ask Chris to review the release:** `git verify-tag v0.2.0` and `git log -p v0.1.0..v0.2.0`. Ledger his approval.
- [ ] **Step 3: The re-pin.** Read forge-bot's state again (S1), then follow its case.
  - **On `main`:** one signed commit with the pin `git.realliance.net/realliance-ops/forge-bot:sha-<7>@sha256:<64>` and Task 3's two summaries in the PrometheusRule and its test. `promtool` and `scripts/test_forge_bot_renovate.py` pass. **Gate G, then STOP—ask Chris.**
    - Downtime: one StatefulSet pod restart and one missed 5-minute cycle, nothing users see.
    - Watch: the pod Ready. Until Task 5 merges, `forge_bot_read_ok{source="forgejo"}` reads 0 with the `ErrIncomplete` line, as expected; nothing else may fail.
    - Rollback trigger: a crash loop, any other read failure, or `ForgeBotUnexpectedSSOWrite`.
    - Rollback: revert the pin to `sha-9be4857`. That build reads every org's Owners again, so once Task 16 opens org creation, a rollback lets user-org Owners escape. Say so in the STOP.
    - Push, `Monitor` to Ready, Gate G after, then start Task 5 at once: keep the gap under 30 min (`ForgeBotStale`).
  - **Not deployed (R14):** the same commit on `forge-bot-deploy`, only after **STOP—ask Chris**, with no deploy. Ledger the pin for the forge-bot plan; Task 5 then waits for that plan's deploy.

### Task 5: Inventory `owner_orgs`

**Gate:** forge-bot `v0.2.0` runs (Task 4, or the forge-bot plan's deploy).

**Files:** the inventory's `forge-bot.yaml`, branch `owner-orgs`.

- [ ] **Step 1: Edit.** Add `owner_orgs: [realliance, realliance-ops]` with a comment: the orgs whose Owners forge-bot spares, required since `v0.2.0`; an Owner of any other org is an ordinary account. Check it with `inventory.ParseOwnerOrgs` in a scratch test in the forge-bot worktree. Expected: both names.
- [ ] **Step 2: Merge.** Commit signed and open the PR. **STOP—ask Chris** to fast-forward, then `Monitor` the next cycle. Expected: `forge_bot_read_ok{source="forgejo"}` and `forge_bot_inventory_ok{check="bot_config"}` both read 1; each job's dry-run plan count equals the count from before Task 4 (ledger both); no `owners_unknown`.

### Task 6: ci-dispatch `org_defaults`

**Gate:** none.

**Files (worktree `~/Repos/ci-dispatch-org-defaults`, branch `org-defaults`):**
- Change: `internal/limits/limits.go`, `internal/forgejo/guard.go`, `internal/forgejo/client.go`, `internal/gate/gate.go`, `internal/loop/loop.go`, `internal/usage/usage.go`, `internal/metrics/metrics.go`, `cmd/ci-dispatch/main.go`, `README.md`, `.forgejo/workflows/ci.yaml`
- Create: `internal/orgs/orgs.go`, `internal/orgs/orgs_test.go`
- Test: `internal/limits/limits_test.go`, `internal/forgejo/client_test.go`, `internal/forgejo/writes_test.go`, `internal/gate/gate_test.go`, `internal/loop/loop_test.go`, `internal/usage/usage_test.go`, `internal/metrics/metrics_test.go`

**Interfaces (produces):**
- `limits.Kind`, with `KindOrg Kind = iota` as the zero value, then `KindUser`.
- `limits.Raw.OrgDefaults Limit` (`yaml:"org_defaults"`), checked like `defaults`. `limits.Limits.OrgDefaults Owner` starts at paid 0, paid concurrency 0 and the `tiny` fields of the resolved `Defaults`; the file's `org_defaults` fields apply over that. `BuiltIn` gets the same.
- `func (l Limits) For(owner int64, k Kind) (o Owner, denied bool)`: a deny first; then an `owners:` entry, whatever `k`; then `Defaults`, with Source `default`, for `KindUser`; else `OrgDefaults`, with Source `org-default`.
- `(*forgejo.Client).Orgs(ctx context.Context) ([]int64, error)` pages `GET /admin/orgs?page=N&limit=50` until the count equals `X-Total-Count` with no id twice, retried once, as `Runners` does.
- `(*forgejo.Client).RepoOwner(ctx context.Context, repoID int64) (int64, error)` returns `owner.id` from `GET /repositories/<id>`.
- Guard GET shapes: `shape("/admin/orgs", "page", "limit")` and `shape("/repositories/[0-9]{1,18}")`.
- Package `internal/orgs`:
  ```go
  type Forge interface {
  	Orgs(ctx context.Context) ([]int64, error)
  	RepoOwner(ctx context.Context, repoID int64) (int64, error)
  }

  const RefreshEvery, StaleAfter, RepoTTL = 10 * time.Minute, time.Hour, 10 * time.Minute

  func New(f Forge) *Book
  func (b *Book) Refresh(ctx context.Context, now time.Time, force bool) error
  func (b *Book) LastGood() time.Time
  func (b *Book) Kind(ctx context.Context, ownerID, repoID int64, now time.Time) limits.Kind
  func (b *Book) OwnerKind(ownerID int64, now time.Time) limits.Kind
  ```
  - `Refresh` reads when `force` is true, or when the last attempt is `RefreshEvery` old; a failed read keeps the last good list.
  - `Kind`: no list, or one older than `StaleAfter`, gives `KindOrg`; an id in the list gives `KindOrg`. Otherwise it compares `RepoOwner`, cached `RepoTTL` per repo, with `ownerID`: equal gives `KindUser`, and a mismatch or a failed read gives `KindOrg`.
  - `OwnerKind`, for the usage API, which has no job: a fresh list without the id gives `KindUser`; anything else gives `KindOrg`.
- `gate.Inputs.Kinds map[model.Key]limits.Kind`. `gate.Plan` calls `in.Limits.For(j.OwnerID, in.Kinds[k])`, so a missing key reads as `KindOrg`.
- The loop:
  - `step.config()` calls `lp.orgs.Refresh(ctx, now, headChanged)` after the limits reload, where `headChanged` means `Status().Head` changed since the last refresh, and sets the gauge;
  - `plan()` fills `in.Kinds` through `Kind`, for each eligible job whose owner has no `owners:` entry and no deny.
- `usage.Server` takes `OwnerKind func(owner int64, now time.Time) limits.Kind` and calls `lim.For(owner, OwnerKind(owner, now))`; `org-default` passes through as `limit_source`.
- The metric `ci_dispatch_org_list_age_seconds`, a gauge, is absent until the first good list and read against the clock at each scrape, as `ci_dispatch_price_age_seconds` is. Its setter is `func (m *Metrics) SetOrgListFetched(at time.Time)`.

- [ ] **Step 1: Failing tests:**
  - limits: `TestParseOrgDefaults` (the four fields, the ceilings as in `defaults`); `TestOrgDefaultsAbsentFailsClosed` (no key gives paid 0, concurrency 0, and `tiny` fields equal to `defaults`); `TestForKinds` (id 52 in `owners:` gets its entry with either kind; a deny holds with either; an unlisted id gets `defaults` as `KindUser` and `org_defaults` as `KindOrg`; `Kind(0)` is `KindOrg`);
  - forgejo: `TestOrgsPaged` (two pages, `X-Total-Count` 51; one retry after a mismatch, then unavailable) and `TestRepoOwner`. `TestGuardAllowList` passes `GET /admin/orgs?page=1&limit=50` and `GET /repositories/12`, and refuses `GET /admin/orgs/x`, `GET /repositories/abc`, `GET /repositories/12/extra`, `POST /admin/orgs` and `DELETE /repositories/12`;
  - orgs: `TestKindListedOrg`, `TestKindUserMatchesRepoOwner`, `TestKindRepoOwnerMismatchIsOrg` (a run from before a transfer), `TestKindRepoReadFailsIsOrg`, `TestKindStaleListIsOrg` (61 min after the last good list), `TestKindColdStartIsOrg`, `TestRefreshKeepsLastGoodOnError`, `TestRefreshEveryTenMinutesUnlessForced`, `TestRepoOwnerCachedTenMinutes` (one read in 10 min, a second after), `TestOwnerKind`;
  - gate: `TestPaidOrgDefaultsHoldNoBudget` (an org's `small` job holds `no-budget`) and `TestMissingKindIsOrg`;
  - loop: `TestColdStartReadsOrgsBeforeFirstPlan`, where on the first tick the fake answers the org list and a user's `small` job launches, not `no-budget`; and `TestOrgListFailureKeepsLastGood`, where after one good list, 50 min of failed reads keep the user on `defaults`, and at 61 min the job holds `no-budget`;
  - usage: `TestUsageOrgShowsOrgDefaults` (`limit_source` `org-default`, `limit_usd` `0.00`);
  - metrics: the gauge is absent before `SetOrgListFetched`, then grows with the clock.

  Run `$GO test -race -p=4 ./...`. Expected: fail.
- [ ] **Step 2: Write the code** for what the Interfaces say.
- [ ] **Step 3: Run** `$GO mod tidy -diff`, `$GO vet ./...` and `$GO test -race -p=4 ./...`. Expected: pass, `TestDependencyGuard` included.
- [ ] **Step 4: README and blocks.** The reads-and-writes bullet (`README.md:37-38`) names `/admin/orgs` and `/repositories/<id>` among the reads, and a line covers `org_defaults`. In `.forgejo/workflows/ci.yaml`, `test` and `image` get `permissions: {}`, with the header as in Task 3 Step 5. vale.
- [ ] **Step 5: Commit** signed, as `limits: org_defaults for orgs without an owners entry` and `ci: permissions blocks`. Push and open the PR. Expected: the PR run passes.

### Task 7: ci-dispatch `v0.4.0`, released, alerted and pinned

**Gate:** after Task 6's PR passes.

**Files (luma-homeops):** `cluster/ci-dispatch/dispatcher/statefulset.yaml`, `prometheusrule.yaml` and `alerts_test.yaml` beside it, `docs/ci.md`.

- [ ] **Step 1: Release.** First run `docs/ci.md` "Releasing a dispatcher": the three copies agree. **STOP—ask Chris** to fast-forward and push the signed tag `v0.4.0`. `Monitor` the tag run, then ledger `sha-<7>` and the digest, read anonymously.
- [ ] **Step 2: STOP—ask Chris to review the release:** `git verify-tag v0.4.0` and `git log -p v0.3.1..v0.4.0`.
- [ ] **Step 3: The alert, failing first.** In `alerts_test.yaml`:
  - `CIDispatchOrgListStale` fires once `ci_dispatch_org_list_age_seconds` has held at 3700 for 15 min, with `severity: warning` and `reason: stale`, and stays quiet at 600;
  - its `reason: absent` rule fires after 30 min without the series while `up` is 1.

  Run `scripts/test-ci-dispatch-alerts.sh`. Expected: fail. Then add the two rules, `> 3600` for 15m and `absent(…)` for 30m, with the summaries `ci-dispatch's organisation list is over an hour old, so every owner without an owners: entry gets org_defaults, with no paid classes (docs/ci.md)` and `ci-dispatch has no organisation list, so every owner without an owners: entry gets org_defaults (docs/ci.md)`. Update the header's counts. Run again. Expected: pass.
- [ ] **Step 4: docs/ci.md.** An org without an `owners:` entry runs on `tiny` only (`org_defaults`), and paid classes need an `owners:` entry by PR. Add the alert's row to Alerts and the `org_defaults` row to the section Changing a limit, and put the org rule in place of the sentence that starts `Each user and each org is an owner`. vale.
- [ ] **Step 5: Deploy.** One signed commit with the pin `git.realliance.net/realliance-ops/ci-dispatch:sha-<7>@sha256:<64>` and Steps 3–4; `scripts/test_ci_dispatch_manifests.py` passes. **Gate G, then STOP—ask Chris.**
  - Downtime: the StatefulSet's one pod restarts, launches pause for about a minute, and queued jobs wait.
  - Watch: Ready; `ci_dispatch_org_list_age_seconds` appears within a tick and stays under 660; `ci_dispatch_config_verified` reads 1; the log shows no guard refusal; a member's paid job, or the caps owner's, still launches.
  - Rollback trigger: a crash loop, `CIDispatchStale`, a listed owner's job held `no-budget`, or the gauge still absent after 15 min.
  - Rollback: revert the pin to `sha-f415caa` (`v0.3.1`). That's clean until Task 8. After Task 8, `v0.3.1` rejects both the head and its stored copy, so it runs on the built-in limits, `tiny` only, until `ci.yaml` drops `org_defaults`.
  - Push, `Monitor` the rollout, Gate G after.

### Task 8: Inventory `org_defaults`

**Gate:** `v0.4.0` runs (Task 7).

**Files:** the inventory's `ci.yaml` and `README.md`, branch `org-defaults`.

- [ ] **Step 1: Edit.** After `defaults`, `ci.yaml` gains:
  ```yaml
  org_defaults:
    paid_usd_per_period: 0
    paid_concurrent: 0
    tiny_concurrent: 1
    tiny_minutes_per_day: 120
  ```
  Its comment: an org without an `owners:` entry runs on `tiny` only, and getting `small` or `medium` takes an `owners:` entry by PR. In the README, "What lives where" and "New org" say that members create their own orgs in the web UI, outside this repo, while `realliance` and `realliance-ops` stay here. Check with a scratch `limits.Parse` and a read-only `Resolve` in the ci-dispatch worktree, in the form of phase 1 Task 15 Step 2. Expected: pass, no findings.
- [ ] **Step 2: Merge.** Commit signed and open the PR. **STOP—ask Chris** to fast-forward. `Monitor` until `ci_dispatch_config_verified` reads 1 at that head, with no finding, and ledger the head.

### Task 9: patch A, secrets by reference

**Gate:** after Task 1 merged, or branch `token-permissions` cut from `perms-ci`.

**Files (fork, branch `token-permissions`; code in `upstream/`, then `make export`):**
- Create: `services/actions/secret_refs.go`, `services/actions/secret_refs_test.go`, `tests/integration/actions_secrets_referenced_test.go`
- Change: `services/actions/secret.go:20-26`, `services/actions/secret_test.go`

**Interfaces:**
- `func secretReferences(payload []byte) (names map[string]bool, all bool)` returns the upper-cased names the payload references (R7); `all` is true for any other use of `secrets`, or a payload that doesn't parse.
- `getSecretsOfTask` keeps a secret from `getSecretsOfJob`'s map when `all` is true or `names[strings.ToUpper(k)]` holds. It keeps `ACTIONS_STEP_DEBUG` and `ACTIONS_RUNNER_DEBUG` whenever the map has them, then sets the three token names as today. `getSecretsOfJob` and the inner-call recursion stay as they are.
- The upstream commit is `feat(actions): send a job only the secrets it names`, with `Upstream: candidate`.

- [ ] **Step 1: Failing unit tests.** `TestSecretReferences`, each fragment inside a minimal single-job workflow:

  | # | Fragment | Names | All |
  |---|---|---|---|
  | 1 | a step's `env: {A: "${{ secrets.S1 }}"}` | `S1` | no |
  | 2 | a step's `if: secrets.S2 != ''` | `S2` | no |
  | 3 | `${{ secrets['S3'] }}` and `${{ secrets["s4"] }}` | `S3`, `S4` | no |
  | 4 | `${{ secrets[ 'S5' ] }}` | `S5` | no |
  | 5 | `${{ toJSON(secrets) }}` | none | yes |
  | 6 | `${{ secrets[matrix.name] }}` | none | yes |
  | 7 | `${{ secrets[format('A_{0}', matrix.n)] }}` | none | yes |
  | 8 | `run: echo "no secrets here"` | none | no |
  | 9 | `name: Check secrets` | none | no |
  | 10 | container and service `credentials: {password: "${{ secrets.REG }}"}` | `REG` | no |
  | 11 | the workflow's `env: {W: "${{ secrets.W }}"}` | `W` | no |
  | 12 | `with: {token: "${{ secrets.lower_case }}"}` | `LOWER_CASE` | no |
  | 13 | `${{ steps.secrets.outputs.x }}` and `${{ inputs.secrets }}` | none | no |
  | 14 | a payload that doesn't parse | none | yes |
  | 15 | the keys `secrets: inherit` and `on.workflow_call.secrets` | none | no |

  `TestGetSecretsOfTaskFiltersByReference`, with fixtures, in `secret_test.go`: give job 600 a payload that names `SECRET_1`, and the task gets `SECRET_1` and the three token names but not `SECRET_2`; with `toJSON(secrets)` it gets both.

  Run `taskset -c 0-3 nix develop .#build --command env GOMAXPROCS=4 scripts/test-go.sh unit 'TestSecretReferences|TestGetSecretsOf'` from the fork root. Expected: fail.
- [ ] **Step 2: Failing integration test,** `TestActionsSecretsByReference`. Patch B adds the setting later in the series, so Task 11 repeats one of these cases under both defaults. Repo secrets `S1` and `S2`, org secret `S3`. It reads `task.Secrets` from the mock runner's `fetchTask`:
  - a job naming `S1` in `env` and `S2` in an `if` receives exactly `S1`, `S2` and the three token names;
  - `toJSON(secrets)` receives all three;
  - a `secrets: inherit` inner job naming `S3` receives `S3`;
  - a fork PR receives only the token names, as today.

  Commit it in `upstream/` as A's commit, run `make export`, commit `patches/` signed, push, dispatch `test.yml` on `token-permissions`. Expected: `TestActionsSecretsByReference` fails, and the rest pass.
- [ ] **Step 3: Write the code** for `secretReferences` and the filter. Step 1's command passes. Amend A's commit, export, commit signed, push, dispatch. Expected: pass.
- [ ] **Step 4: Check.** `scripts/check-patches.sh` and `make check` pass.

### Task 10: patch B core, the parser, resolver, setting and override

**Files (`upstream/`):**
- Create: `services/actions/jobtoken/parse.go`, `parse_test.go`, `resolve.go`, `resolve_test.go`, `repo.go`, `repo_test.go`, `main_test.go`
- Change: `modules/setting/actions.go`, `modules/setting/actions_test.go`, `models/repo/repo_unit.go`

**Interfaces (produces; Tasks 11 and 12 consume):**
```go
package jobtoken // forgejo.org/services/actions/jobtoken

type Permissions struct {
	Contents, Issues, PullRequests, Actions, Projects, Packages perm.AccessMode
}

func Permissive() Permissions // every repo scope write, Packages read
func Restricted() Permissions // Contents and Packages read, the rest none
func (p Permissions) Min(q Permissions) Permissions
func (p Permissions) Unit(t unit.Type) perm.AccessMode

// Block is one parsed `permissions:` value. A nil *Block means none at that level.
type Block struct{ /* the scopes it sets */ }

func ParseBlocks(workflow []byte, jobID string) (top, job *Block, unknown []string, err error)
func (b *Block) Permissions() Permissions

func Resolve(ctx context.Context, task *actions_model.ActionTask) (Permissions, error)
func RepoPermission(ctx context.Context, repo *repo_model.Repository, task *actions_model.ActionTask) (access_model.Permission, error)
```
- `Unit` maps Code, Releases and Wiki to `Contents`; ExternalWiki and ExternalTracker to `min(Contents, read)`; Issues, PullRequests, Actions, Projects and Packages to their own scope; any other unit to none.
- The pure core is `resolve(in resolveInput) Permissions`, with `resolveInput{Instance, Repo string; Top, Job *Block; Caller *Permissions; Fork bool}`:
  - an inner job, one with `Caller` set, gets `min(*Caller, Job or Top)` when the called file has either block, else `*Caller`;
  - any other job gets the first set of Job, Top, Repo and Instance;
  - a fork PR's result is then `min(result, read on every scope)`.
- `Resolve` keeps an LRU, `lru.New[int64, Permissions](4096)`, keyed by task ID.
  - It loads the job, the run and the repo. A fork PR skips the read; any other run gets `ParseBlocks` on `WorkflowDirectory/WorkflowID` at `GetWorkflowSourceCommit()`.
  - An inner job finds its caller with `FindOuterWorkflowCall`, resolves the caller first, and reads the called file only when the caller's `uses:` is local (`./…`).
  - A failed read or parse logs one `log.Warn` with the task ID and resolves with `Top` and `Job` nil, failing closed to the default. An unknown key logs once per task. It returns an error only for a database failure.
- `RepoPermission`: another repo gets `access_model.GetActionRepoPermission`, unchanged. Its own repo gets `Permission{AccessMode: read, Units: repo.Units, UnitsMode: …}`, where each unit's mode is `max(min(base, p.Unit(u)), floor(u))`. `base` is `GetActionRepoPermission`'s mode, and `floor` the `forgejo-actions` user's own mode on that unit (R1, R4).
- The setting `DEFAULT_TOKEN_PERMISSIONS` maps to the field `DefaultTokenPermissions` of `setting.Actions` and defaults to `permissive`, with constants `setting.TokenPermissionsPermissive` and `setting.TokenPermissionsRestricted`. Any other value fails `loadActionsFrom` with `invalid [actions] DEFAULT_TOKEN_PERMISSIONS: %q`.
- `repo_model.ActionsConfig.TokenPermissions string` (`json:",omitempty"`) holds `""`, `permissive` or `restricted`.
- The upstream commit is `feat(actions): permissions: for the job token, with an instance default and a repo override`, with `Upstream: not-needed`. Task 11 amends it.

- [ ] **Step 1: Failing unit tests:**
  - `TestParseBlocks`: `read-all` sets every scope to read, `packages` included; `write-all` sets every scope to write; `{}` gives an empty, non-nil block; `contents: write` gives contents write and the rest none; `jobs.build.permissions: {issues: write}` comes back as `job` for `build` and nil for `deploy`; `id-token: write` drops silently; `bogus: write` and `contents: admin` drop and appear in `unknown`; a file without the key gives nil for both; a file that doesn't parse gives an error;
  - `TestResolve`, on the pure core, row by row:
    1. no block, Instance `permissive`: `Permissive()`;
    2. no block, Instance `restricted`: `Restricted()`;
    3. Repo `restricted` over Instance `permissive`: `Restricted()`;
    4. Top `{}` over Repo `permissive`: all none;
    5. Job `{issues: write}` over Top `{contents: write}`: issues write, the rest none;
    6. Top `write-all`: every scope write, packages included;
    7. a fork with Top `write-all`: every scope read;
    8. Caller `{contents: write, issues: write}` with an inner Top `{contents: read}`: contents read, the rest none;
    9. Caller set, inner without blocks: the caller's result;
    10. Instance `permissive`: `Packages` read;
  - `TestResolveFixtureTaskUnread` (Review Focus 3): fixture task 47, whose workflow file no commit holds, resolves to `Permissive()` and then, under `restricted`, to `Restricted()`. Two calls for one task log one warning;
  - `TestRepoPermissionFloor`: with a `{}` result seeded in the cache, a public repo gives code, releases and wiki read (the floor), and issues, pull requests and actions none, `HasAccess()` and not `IsAdmin()`; a private repo gives every unit none, with `HasAccess()` still true;
  - `TestLoadActionsDefaultTokenPermissions`: absent gives `permissive`, `restricted` gives `restricted`, `bogus` gives the error text;
  - `TestActionsConfigTokenPermissionsRoundTrip`: `ToDB` leaves the field out when empty and keeps `DisabledWorkflows`, and `FromDB` of today's form, `{"DisabledWorkflows":["a.yml"]}`, leaves it empty.

  Run `scripts/test-go.sh unit 'TestParseBlocks|TestResolve|TestRepoPermission|TestLoadActions|TestActionsConfig'`. Expected: fail.
- [ ] **Step 2: Write the code** for what the Interfaces say. First read `services/actions/schedule_tasks.go`, and ledger whether a schedule run's `CommitSHA` is the commit that registered its schedule (the spec leaves it to the builder). The read uses `GetWorkflowSourceCommit()` either way, and a schedule whose file has since moved resolves to the default.
- [ ] **Step 3: Run** Step 1's command and `go vet` on the three packages. Expected: pass. Commit in `upstream/`, export, commit `patches/` signed. No dispatch yet: Task 11 adds the integration tests.

### Task 11: patch B enforcement, the settings page and the matrix

**Files (`upstream/`):**
- Change: `routers/api/v1/permissions/repo_access.go`, `routers/web/repo/githttp.go:192-210`, `services/lfs/server.go:544-559`, `routers/web/web.go` (the repo settings `/actions` group), `templates/repo/settings/actions.tmpl`, `templates/repo/settings/navbar.tmpl`, `options/locale_next/locale_en-US.json`, `routers/api/v1/permissions/tests/fixture.go`, `routers/api/v1/permissions/tests/repo_access.go`, `routers/api/v1/permissions/tests/req_repo_writer.go`
- Create: `routers/web/repo/setting/actions_token.go`, `templates/repo/settings/actions_token.tmpl`, `tests/integration/actions_job_token_permissions_test.go`

**Interfaces:**
- Consumes `jobtoken.RepoPermission` and `jobtoken.Resolve` from Task 10.
- `repo_access.go`: the Actions branch keeps the 404 for another repo, then sets the permission from `RepoPermission`.
- `githttp.go`: `RepoPermission` replaces `GetActionRepoPermission`, and `EnvActionPerm` carries `p.UnitAccessMode(unitType)`, the code or wiki level, not `p.AccessMode`. `hook_pre_receive.go` doesn't change.
- `lfs/server.go`: another repo stays refused; its own repo answers `RepoPermission(…).CanAccess(accessMode, unit.TypeCode)`.
- The settings page, in `routers/web/repo/setting/actions_token.go`:
  - `func ActionsTokenPermissions(ctx *context.Context)` for GET, and `func ActionsTokenPermissionsPost(ctx *context.Context)` for POST with the form field `token_permissions`: `""`, `permissive` or `restricted`. Any other value flashes an error and writes nothing;
  - saving Settings, Units (and the API `has_actions: true` edit) carries the existing Actions unit's whole config into the rebuilt unit, so the override and `DisabledWorkflows` survive (Task 11 fix);
  - the route `m.Combo("/token").Get(repo_setting.ActionsTokenPermissions).Post(repo_setting.ActionsTokenPermissionsPost)` sits inside the repo settings `/actions` group, behind `reqRepoAdmin`, with `PageType` `token`;
  - it writes `ActionsConfig.TokenPermissions` with `repo_model.UpdateRepoUnit`, as `disableOrEnableWorkflowFile` does;
  - locale keys under `actions.token_permissions.`: `title`, `desc`, `follow_instance`, `permissive`, `restricted`, `update_success` and `invalid`. The page names the instance default when the repo follows it.

- [ ] **Step 1: Declarative cases**, run by `TestAPIv1Permissions`. The fixture gains the keys `token.instance` (the instance default) and `token.repo` (the repo override), and the function test's `protect` guards the setting.
  - In `repo_access.go`: the Actions doer reaches its public and its private repo under both defaults with no 404, and another repo still gets `Not Found`.
  - In `req_repo_writer.go`, the Actions doer as an Issues writer: refused under `restricted`; allowed under `permissive`; refused under the repo override `restricted` with the instance `permissive`; refused on a fork PR under `permissive`.
- [ ] **Step 2: Failing integration tests** in `actions_job_token_permissions_test.go`, every case under both instance defaults:
  - `TestJobTokenPermissionsMatrix` starts one run per row. Each run's token tries six operations: C, an issue comment (`POST …/issues/1/comments`); R, a release (`POST …/releases`); P, a `git push` of a new branch; W, a wiki push; D, a dispatch (`POST …/actions/workflows/<id>/dispatches`); L, an LFS batch `upload`. Forgejo answers a refused operation with 403 or 404 from the API, or with the 403 `User permission denied` to git, and never with 500. Expected sets, where none means Forgejo refuses every operation:

    | # | Repo override | Workflow block | Job block | Fork PR | `permissive` | `restricted` |
    |---|---|---|---|---|---|---|
    | 1 | none | none | none | no | CRPWDL | none |
    | 2 | `permissive` | none | none | no | CRPWDL | CRPWDL |
    | 3 | `restricted` | none | none | no | none | none |
    | 4 | none | `{}` | none | no | none | none |
    | 5 | none | `contents: write` | none | no | RPWL | RPWL |
    | 6 | none | `contents: write` | `issues: write` | no | C | C |
    | 7 | `restricted` | `{}` | `actions: write` | no | D | D |
    | 8 | none | `write-all` | none | no | CRPWDL | CRPWDL |
    | 9 | none | `read-all` | none | no | none | none |
    | 10 | none | none | none | yes | none | none |
    | 11 | `permissive` | `write-all` | none | yes | none | none |
    | 12 | none | the run's `WorkflowID` set in the database to a file its commit lacks, before the token's first use | none | no | CRPWDL | none |

    Row 4 also runs C with the runtime JWT (`ACTIONS_RUNTIME_TOKEN`). Expected: refused.
  - `TestJobTokenPermissionsArtifact`: a `{}` job uploads and downloads an artifact with its runtime token.
  - `TestJobTokenPermissionsReusable`: a caller job with `{contents: write, issues: write}` calls a local workflow whose top block is `{contents: read}`, and the inner task can neither push nor comment. With no block in the called file, it can do both.
  - `TestJobTokenPermissionsPublicClone` (Review Focus 1): a `{}` task clones its public repo with `git clone`, and Forgejo refuses its push; on a private repo Forgejo refuses the clone.
  - `TestJobTokenSecretsUnderBothDefaults`: Task 9's first case, a job naming `S1` in `env` and `S2` in an `if`, receives exactly those and the three token names under each default.
  - `TestJobTokenSettingsPage`: a repo administrator sets `restricted`; the stored `ActionsConfig` holds it and the page shows it; a new task under the `permissive` instance can't write issues; a non-administrator gets 404; `token_permissions=bogus` stores nothing and flashes the error.

  Amend B's commit with the tests, export, commit signed, push, dispatch. Expected: the new tests fail, because the call sites still hard-code their rights, and the rest pass.
- [ ] **Step 3: Write the code** for the call sites and the page.
- [ ] **Step 4: Run** `scripts/test-go.sh unit` and `go vet` on the touched packages locally. Amend, export, commit, push, dispatch. Expected: the whole `INTEGRATION_RUN` selection passes, the existing tests on fixture task 47 included.

### Task 12: patch C, `packages: write`

**Files (`upstream/`):**
- Create: `services/packages/jobtoken.go`, `services/packages/jobtoken_test.go`, `tests/integration/api_packages_job_token_test.go`
- Change: `services/packages/auth.go`, `services/packages/package_update.go`, `services/packages/container/manifest.go:585-610`, `routers/api/packages/container/auth.go`, `routers/api/packages/container/container.go:146-172`, `routers/api/packages/api.go:45-91, 889-950`, `models/git/protected_tag.go`

**Interfaces:**
- Consumes `jobtoken.Resolve` from Task 10.
- `git_model.IsTagProtected(ctx context.Context, repoID int64, tagName string) (bool, error)` is true when any protected-tag rule of the repo matches, glob or `/regex/`.
- `packages_service.CreateAuthorizationTokenForTask(u *user_model.User, scope auth_model.AccessTokenScope, taskID int64) (string, error)` and `packages_service.ParseAuthorizationTokenWithTask(req *http.Request) (uid int64, scope auth_model.AccessTokenScope, taskID int64, err error)`. The claims gain `TaskID int64` (`json:",omitempty"`), and the two existing functions wrap these with 0 and keep their signatures.
- `container.Authenticate` passes the value of `ctx.Authentication.ActionsTaskID()`. `container.Auth.Verify` accepts a JWT with a task ID only when `uid` is the Actions user and `auth_method.CheckTaskIsRunning` holds; its result's `ActionsTaskID()` returns that ID. Anything else gets an incorrect-credential result.
- `services/packages/jobtoken.go`:
  ```go
  type TaskRule string

  const (
  	RulePackagesWrite TaskRule = "packages-write"
  	RuleOwner         TaskRule = "owner"
  	RuleName          TaskRule = "name"
  	RuleType          TaskRule = "type"
  	RuleEvent         TaskRule = "event"
  	RuleRef           TaskRule = "ref"
  	RuleDelete        TaskRule = "delete"
  )

  type TaskRefusal struct{ Rule TaskRule }

  func (e *TaskRefusal) Error() string
  func CheckTaskWrite(ctx context.Context, task *actions_model.ActionTask, packages perm.AccessMode, owner *user_model.User, typ packages_model.Type, name string, isDelete bool) error
  func TaskReadMode(packages perm.AccessMode, task *actions_model.ActionTask, owner *user_model.User, today perm.AccessMode) perm.AccessMode
  ```
  - `CheckTaskWrite` checks the six rules in the spec's order and returns the first that fails. Its pure part, rules 2, 3, 4 and 6, is `checkTaskTarget(repo, run, owner, typ, name, isDelete) TaskRule`, which the unit test calls.
  - `Error()` names the rule; `RuleType`'s text is the spec's sentence.
  - `TaskReadMode` returns `max(today, read)` when `owner` is the task repo's owner and `packages` resolves to read or better, and `today` otherwise.
- `reqPackageAccess`, for an Actions task (the Actions user with `ActionsTaskID()`), resolves `Packages`.
  - A write route calls `CheckTaskWrite` with the container type and `ctx.Params("image")`, the generic type and `ctx.Params("packagename")`, or the route's other type and an empty name. `isDelete` is true for a DELETE other than an upload cancel (R6).
  - `RulePackagesWrite` keeps today's 401. Any other refusal answers 403 with `TaskRefusal.Error()`, as an OCI `DENIED` body on `/v2`, and logs `log.Warn("job token package write refused: task %d repo %d type %s name %q rule %s", …)`.
  - A read uses `TaskReadMode`. The three manual container write branches set `image` before `reqPackageAccess`.
- `packages_service.LinkToTaskRepository(ctx context.Context, pkg *packages_model.Package, repo *repo_model.Repository) error` checks the same owner and an unlinked package, with no user check. `tryAutoLink` with the Actions user as creator links by image name only, through it.
- The upstream commit is `feat(packages): packages: write lets the job token publish its repo's images`, with `Upstream: not-needed`.

- [ ] **Step 1: Failing unit tests:**
  - `TestCheckTaskTarget` (Review Focus 4), for repo `forge-bot` of owner `ops`, a `push` run, not a fork:

    | Type | Owner | Name | Delete | Rule |
    |---|---|---|---|---|
    | container | `ops` | `forge-bot` | no | none |
    | container | `ops` | `forge-bot/cache` | no | none |
    | container | `ops` | `Forge-Bot` | no | none |
    | container | `ops` | `forge-bot2` | no | `name` |
    | container | `ops` | `forge-bot-cache` | no | `name` |
    | container | `other` | `forge-bot` | no | `owner` |
    | generic | `ops` | `forge-bot` | no | none |
    | generic | `ops` | `forge-bot/x` | no | `name` |
    | npm | `ops` | `forge-bot` | no | `type` |
    | container | `ops` | `forge-bot` | yes | `delete` |

    Event rows: `pull_request` and `pull_request_target` give `event`, and so does a fork PR on `push`; `release`, `schedule` and `workflow_dispatch` pass.
  - `TestTaskReadMode`: a private task owner at `packages: read` gives read; at none, `today`; another owner, `today`.
  - `TestIsTagProtected`, with fixtures: `v*` matches `v1.2.3`; `/^v[0-9]+$/` matches `v12` and not `v1.2`; no rule gives false.
  - `TestPackageClaimsTaskID`: a JWT from `CreateAuthorizationTokenForTask(…, 7)` parses with task ID 7, and one from `CreateAuthorizationToken` with 0.

  Run `scripts/test-go.sh unit 'TestCheckTaskTarget|TestTaskReadMode|TestIsTagProtected|TestPackageClaimsTaskID'`. Expected: fail.
- [ ] **Step 2: Failing integration tests** in `api_packages_job_token_test.go`. They get `/v2/token` with basic auth, as `api_packages_container_test.go:104-178` does, and make protected tags as `api_repo_tags_test.go:179` does. Every case runs under both instance defaults:
  - `TestJobTokenPackagesContainer`: a push run on a protected tag with `packages: write` pushes `<owner>/<repo>` and `<owner>/<repo>/sub`; the package's creator is `forgejo-actions`, and auto-link sets the repo;
  - `TestJobTokenPackagesRefusals`: `<owner>/<other>` gets 403 naming `name`; a `pull_request` task, `event`; an unprotected branch and a dispatch on one, `ref`; a job without `packages: write`, today's 401; a manifest DELETE and a blob DELETE, `delete`. Each 403 left one warning line naming its rule;
  - `TestJobTokenPackagesGeneric`: an upload under `<repo>` works, another name gets 403 `name`, and a DELETE gets `delete`;
  - `TestJobTokenPackagesOtherType`: an npm publish under `packages: write` gets a 403 whose body holds the type sentence, plus the warning;
  - `TestJobTokenPackagesJWTAfterTask`: a JWT from a running task gets 401 once the task ends;
  - `TestJobTokenPackagesPrivateRead`: a `packages: read` task pulls a private owner's image, and a `{}` task can't.

  Amend C's commit with the tests, export, commit, push, dispatch. Expected: the new tests fail, the rest pass.
- [ ] **Step 3: Write the code** for what the Interfaces say.
- [ ] **Step 4: Run** the unit tests locally. Amend, export, commit, push, dispatch. Expected: the whole selection passes, `TestPackageContainer` and `TestPackageGeneric` included.

### Task 13: The fork PR

**Files (fork):** `WISHLIST.md`, `patches/`.

- [ ] **Step 1: Checks.**
  - `scripts/check-patches.sh` prints `13 patches, no migrations, tables, columns, queues or cron tasks`; `make check` and `bash scripts/test-ci.sh no-migrations version workflow test-workflow test-go` pass.
  - The production line count, `git -C upstream diff --stat <A>~1..<C> -- . ':!*_test.go' ':!tests/' ':!options/'`, comes to about 700. Over 900 is a finding for Chris.
  - The three patches' test files hold no token-shaped value: `grep -nE '[0-9a-f]{40}'` over them finds fixture shas only.
- [ ] **Step 2: WISHLIST.** A row for job token permissions and secret scoping, in the queue as three patches, and notes that carry the spec's "Carry to the 19 LTS" table. vale.
- [ ] **Step 3: PR.** Commit, push, dispatch `test.yml` once more (green), and open the PR from `token-permissions`. Its body holds the summary, R1 to R8, the matrix, the run link and the line count. **STOP—ask Chris** to merge.

### Task 14: the fork release

**Gate:** after Task 13 merged, and Tasks 4 and 7 done.

- [ ] **Step 1: STOP—ask Chris** to push the signed tag `v15.0.11-realliance.N` on `main`, N from Task 0 read again. `Monitor` `image.yml`: `build` on `medium`, then `publish` on `tiny`, still with `REGISTRY_TOKEN`. Ledger the pin line, show Chris the digest, and ledger his review.
- [ ] **Step 2: Check** anonymously: `crane digest git.realliance.net/realliance/forgejo:v15.0.11-realliance.N` equals the pin, and `crane config` shows the version label `15.0.11-realliance.N`.

### Task 15: Live acceptance tests, red before the rollout

**Files (`realliance/sso`, branch `job-token-acceptance`):**
- Create: `tests/forge/test_forge_job_token.py`
- Change: `tests/forge_helpers.py` (`push_probe_workflow` gains optional `permissions` and `env` arguments; the defaults change nothing), `tests/forge/test_forge_signin.py` (`test_member_cannot_create_org` becomes `test_member_can_create_org`), `tests/forge/test_forge_ci_dispatch.py`

- [ ] **Step 1: Tests:**
  - `test_job_gets_only_named_secrets`: a public throwaway repo holds secrets `S1` and `S2` with random throwaway values. A `tiny` probe names `S1` in a step's `env` and echoes both values literally; the test reads the log anonymously. Expected after A: `S1`'s value shows as `***`, and `S2`'s shows in the clear. If Task 0 Step 5 found that the runner masks only referenced secrets, drop this test and ledger why;
  - `test_empty_permissions_cannot_push_or_dispatch`: the throwaway repo also holds a `workflow_dispatch` workflow, the dispatch target. A `tiny` probe with `permissions: {}` runs a script that exits 0 only when a `git push` of a new branch and a dispatch of that workflow, both with `${{ github.token }}`, each answer 403. Expected: the run succeeds;
  - `test_member_can_create_org`: a fresh member creates an org and gets 201; the test then deletes it;
  - `test_ci_dispatch_token_reads_orgs_and_repositories`: both reads answer 200.

  Run `nix develop --command pytest tests/forge/test_forge_job_token.py tests/forge/test_forge_signin.py tests/forge/test_forge_ci_dispatch.py -v`. Expected now: the first three fail, since today's Forgejo sends every secret, ignores the block and refuses org creation. Ledger them as the red half.
- [ ] **Step 2: Draft PR.** Commit signed, push, `gh pr create --draft`. Task 17 marks it ready.

### Task 16: Forgejo rollout with the org config

**Gate:** after Task 14 and Gate O.

**Files (luma-homeops):**
- Change: `cluster/applications/forgejo-release.yaml`, `.pre-commit-config.yaml`, `docs/forgejo.md`, `docs/superpowers/specs/2026-11-07-sso-member-invites-design.md` (lines 319 and 325)
- Create: `scripts/test_forgejo_config.py`, `scripts/forgejo-allow-create-org.py`, `scripts/test_forgejo_allow_create_org.py`

**Interfaces (produces):**
- `scripts/forgejo-allow-create-org.py --source-id <id> [--apply]` reads the administrator token from stdin, never argv. It pages `GET /api/v1/admin/users?limit=50`, checking the count against `X-Total-Count`. For every account with that `source_id` and `allow_create_organization` false, it sends `PATCH /api/v1/admin/users/<login>` with `login_name`, `source_id` and `allow_create_organization: true`.
  - The default is a dry run that prints the count and the logins. `--apply` lists again afterwards and exits non-zero unless it finds no account to patch.
  - Its pure part is `plan_patches(users: list[dict], source_id: int) -> list[dict]`. Task 0 ledgers the source id, which is 1 in forge-bot's config.

- [ ] **Step 1: Failing tests:**
  - `scripts/test_forgejo_config.py`: `test_token_permissions_default_explicit` (`DEFAULT_TOKEN_PERMISSIONS` under `gitea.config.actions` is `permissive` or `restricted`) and `test_org_creation_open` (`service.DEFAULT_ALLOW_CREATE_ORGANIZATION` true and `admin.DISABLE_REGULAR_ORG_CREATION` false);
  - `scripts/test_forgejo_allow_create_org.py`: `plan_patches` skips local accounts (`source_id` 0) and accounts already allowed, and keeps prohibited SSO accounts (R11); every body holds exactly `login_name`, `source_id` and `allow_create_organization: true`; the entry point refuses a token in argv and a missing `--source-id`;
  - a hook `forgejo-config` in `.pre-commit-config.yaml` runs both files when `forgejo-release.yaml`, the script or a test changes.

  Run `nix develop --command python -m pytest -q -p no:cacheprovider scripts/test_forgejo_config.py scripts/test_forgejo_allow_create_org.py`. Expected: fail.
- [ ] **Step 2: Values and the script.**
  - `image.fullOverride` takes Task 14's pin, `git.realliance.net/realliance/forgejo:v15.0.11-realliance.N@sha256:<64>`;
  - `gitea.config.actions` gains `DEFAULT_TOKEN_PERMISSIONS: permissive`, with a comment that the fork reads it and upstream ignores it;
  - `service.DEFAULT_ALLOW_CREATE_ORGANIZATION: true` and `admin.DISABLE_REGULAR_ORG_CREATION: false`. The "orgs are code" comment becomes: users may create their own orgs, and `realliance` and `realliance-ops` are code in `realliance-ops/forge`.

  Run Step 1's tests, `scripts/test_forgejo_image.py` and `scripts/check-kustomizations.sh`. Expected: pass. If Gate O isn't met but Chris wants the fork out, leave the two org keys for a later commit under this task's STOP form, and ledger that.
- [ ] **Step 3: Docs,** in the same commit:
  - `docs/forgejo.md` gets a section, "Job token permissions": the default and the flip, the repo page (Settings, Actions, Job token), the blocks and GitHub's keys, `packages: write` and its six rules, and secrets by reference. It also covers the fallback: on the upstream image every job gets every secret again and the job token can't publish, so after Task 21 a fallback stops all four publishers. It ends with the spec's Known limits: a process a step leaves running on `galaxy` or `general`, the one-job runner credential, and `packages: write` held for the whole job;
  - "Who gets in" in `docs/forgejo.md`, the Owners paragraph and the org rule (`:74-77`): anyone may create an org; the Owners of `realliance` and `realliance-ops` are code; forge-bot spares only those (`owner_orgs`), and `owner_needs_action` concerns only them;
  - "Removing someone" gets a step: list the orgs where the person is the only Owner. Leave each untouched, so its repos keep serving, and add it to `ci.yaml` `deny` if it still runs CI. Take its Pages sites down only on request or abuse, and hand it to a remaining member only when that member asks and Chris agrees;
  - "19 LTS hop carry list" gets the three rows of the spec's "Carry to the 19 LTS";
  - in the invites spec, lines 319 and 325: a guest may create orgs, and each org it creates is a CI owner on `org_defaults`, `tiny` only.

  vale on every changed file. Commit signed, as `forgejo: fork v15.0.11-realliance.N (job token permissions), permissive default, open org creation`.
- [ ] **Step 4: Gate G, then STOP—ask Chris** to approve the rollout.
  - Downtime: 17–29 s, as in the last two Recreates. Push when no Actions job runs: three quiet minutes in `fj … actions tasks` of the busy repos.
  - Watch with `Monitor`: the keep-warm DaemonSet in wave −1 reaching 3/3 on the new digest, stopping on `CrashLoopBackOff`, `OOMKilled`, `Error`, `ErrImagePull` or `ImagePullBackOff`; then Forgejo's Recreate to Ready; then `/api/v1/version` reading `15.0.11-realliance.N+gitea-1.22.0`.
  - Rollback triggers: the pod not Ready in 10 min; a keep-warm crash loop, where you revert the pin and never sync Forgejo (`docs/forgejo.md`, "When the sync waits at wave -1"); any failure in Task 17 Step 1.
  - Rollback: revert this commit, since every node still holds the previous pin, or follow the runbook's Fallback to upstream. The org keys go back with it.
  - Gate G after.

### Task 17: Acceptance and the existing accounts

**Gate:** after Task 16 deployed.

- [ ] **Step 1: Acceptance,** each part ledgered:
  - the runner drill, `docs/ci.md` "Runner drill at every Forgejo upgrade"; deleting `ci-probe` is an S4 ask;
  - `nix develop --command pytest tests/forge -v` in `~/Repos/realliance-sso-perms`, on Task 15's branch. Expected: Task 15's four tests pass now, and the rest as before the rollout; ledger any failure with its cause;
  - one run of each workflow on Task 0's list: `image.yml` and `test.yml` by dispatch on `main`, and the rest by their next push, or **Chris** re-runs the latest `main` run from the UI. Expected: every run passes. A permission failure under `permissive` is a rollback trigger;
  - the blocks bite, which `test_empty_permissions_cannot_push_or_dispatch` covers.

  Mark the sso PR ready, then **STOP—ask Chris** to merge it.
- [ ] **Step 2: The existing accounts.** The agent can't read the administrator token, so **STOP—Chris runs** the script with the token piped from `sso-secrets`: first `scripts/forgejo-allow-create-org.py --source-id 1` (dry), then with `--apply`. Expected: the closing listing finds no account left. Ledger both counts.

### Task 18: The fork publishes with the job token

**Gate:** after Task 17.

**Files (fork, branch `publish-job-token`):** `.forgejo/workflows/image.yml`, `scripts/publish-image.sh`, `scripts/test-ci.sh`, `README.md`

- [ ] **Step 0: Prerequisite, merge rule on `main`.** Patch C counts a branch as protected only when it restricts push, restricts merge (an Owners merge whitelist, or at least one approval with stale approvals dismissed) and has no unprotected file patterns. `realliance/forgejo` `main` doesn't meet that today, so Forgejo would refuse `publish`. Add the merge whitelist or approval rule as IaC in `realliance-ops/forge`; the tofu apply is a **STOP—Chris runs**. forge-bot and ci-dispatch already meet the rule. Task 18 doesn't switch until it applies.
- [ ] **Step 1: Failing tests.** Read the `v*` tag protection and the `main` protection again (S1). The `workflow` case now requires no `secrets.` line at all, and exactly one `github.token`, as `REGISTRY_TOKEN: ${{ github.token }}` in the env of `publish`'s last step. `build` keeps `permissions: {}`, and `publish` holds `permissions:` with `packages: write`. The `refuse-to-move` case checks that the login user is `forgejo-actions`. Run `bash scripts/test-ci.sh workflow refuse-to-move`. Expected: fail.
- [ ] **Step 2: Change** `image.yml` and `publish-image.sh`, which logs in with `-u forgejo-actions` and still reads the token from `REGISTRY_TOKEN` on stdin.
  - The header and the README's "Accepted risk": build code no longer reaches a registry token, and the blocks closed the push-and-dispatch risk.
  - `publish` now holds a token that writes only `realliance/forgejo` images, and only from a protected ref, so a dispatch on an unprotected branch can't publish.

  vale. Run the two cases. Expected: pass.
- [ ] **Step 3: PR.** Commit signed, push, open it. **STOP—ask Chris** to merge, and ask whether he wants dispatch builds from `image/*` branches, which would take an `image/*` protection rule with Owners-only push.
- [ ] **Step 4: The real publish.** Dispatch `image.yml` on `main` and `Monitor`. Expected: the log ends with `…/forgejo:sha-<7>@sha256:<64>`; `GET /api/v1/packages/realliance/container/forgejo/sha-<7>` names the creator `forgejo-actions`; the package links to `realliance/forgejo`. If the package predates patch C and has no link, ask Chris to link it once with `POST /api/v1/packages/realliance/container/forgejo/-/link/forgejo`.

### Task 19: The bots publish with the job token

**Gate:** after Task 18 Step 4. forge-bot goes first, and ci-dispatch follows once forge-bot's check passes.

**Files:** `.forgejo/workflows/ci.yaml` in each bot, branch `publish-job-token`.

- [ ] **Step 1: forge-bot.** Read the `v*` and `main` protections again.
  - `image` gets `permissions:` with `packages: write`, `REGISTRY_TOKEN: ${{ github.token }}` and `ko login git.realliance.net -u forgejo-actions --password-stdin`; `test` keeps `{}`.
  - The header's accepted risk: the image job's token writes only `realliance-ops/forge-bot` images, from `main` or a `v*` tag.
  - Check: `grep -n 'secrets\.' .forgejo/workflows/ci.yaml` prints nothing. vale.

  Commit signed and open the PR; its run passes. **STOP—ask Chris** to fast-forward, then `Monitor` the `main` run. Expected: `image` prints `git.realliance.net/realliance-ops/forge-bot:sha-<7>@sha256:<64>`, the version's creator is `forgejo-actions`, and the package links to repo 94.
- [ ] **Step 2: ci-dispatch.** The same, with `realliance-ops/ci-dispatch` and repo 240.

These are CI changes only: no release tag, and the live pins stay.

### Task 20: realliance.net publishes with the job token

**Gate:** after Task 19.

**Files (realliance.net, branch `publish-job-token`):** `.forgejo/workflows/ci.yaml`, `scripts/ci.sh`

- [ ] **Step 1: Change.** Read the `[0-9]*` and `main` protections again.
  - `publish` gets `permissions:` with `packages: write`, and `SITE_REGISTRY_TOKEN: ${{ github.token }}`.
  - `ci.sh publish` logs in as `forgejo-actions`, and its comments name the job token.
  - The header: Renovate still automerges `X.Y.Z`, and only `main` or a protected `[0-9]*` tag can publish.
  - Check: no `secrets.` line. vale.

  Commit signed, open the PR, then **STOP—ask Chris** to merge.
- [ ] **Step 2: The real publish.** `Monitor` the `main` run. Expected: it publishes `sha-<7>`, with creator `forgejo-actions` and a link to `realliance/realliance.net`. The next signed `X.Y.Z` tag publishes the same way, and Renovate deploys it as before; ledger that run when it comes.

### Task 21: Rotations and retirements

**Gate:** after Tasks 18–20. Each token goes only after its consumer's real publish.

**Files:** `realliance/sso` branch `static-tokens-retired`, `tests/forge/test_forge_ci.py`; luma-homeops `docs/forgejo.md`.

- [ ] **Step 1: The `ops-ci` rotation runbook,** beside "Rotating `realliance-ci`'s tokens" in `docs/forgejo.md`:
  1. the guarded delete with `ACCT=ops-ci OLD=ops-registry`;
  2. mint with `--username ops-ci --token-name ops-registry --scopes write:package`;
  3. set the org secret with `PUT /api/v1/orgs/realliance-ops/actions/secrets/REGISTRY_TOKEN`;
  4. store it in `sso-secrets` as `OPS_CI_PACKAGE_TOKEN`;
  5. run `test_ops_registry_token_scope`.

  It stays as the runbook for a token that must rotate (R13).
- [ ] **Step 2: Failing test** in sso, `test_no_static_registry_tokens`: `realliance-ci` and `ops-ci` hold no token; neither `realliance/forgejo` nor `realliance/realliance.net` holds a secret; `realliance-ops` has no org secret `REGISTRY_TOKEN`. The same commit removes `test_realliance_ci_tokens`, `test_site_registry_token_scope` and `test_ops_registry_token_scope`. Run it. Expected: fail, since the tokens exist.
- [ ] **Step 3: One STOP—ask Chris per token,** in this order. Each is a database write.
  1. `realliance/forgejo`'s `REGISTRY_TOKEN`: delete the repo secret (`DELETE /api/v1/repos/realliance/forgejo/actions/secrets/REGISTRY_TOKEN`), run the guarded delete with `ACCT=realliance-ci OLD=realliance-forgejo-registry`, and **Chris** drops `REALLIANCE_FORGEJO_REGISTRY_TOKEN` from `sso-secrets`.
  2. `realliance-ops`'s `REGISTRY_TOKEN`, once both bots moved: delete the org secret, run the delete with `ACCT=ops-ci OLD=ops-registry`, and **Chris** drops `OPS_CI_PACKAGE_TOKEN`.
  3. `SITE_REGISTRY_TOKEN`: delete the repo secret, run the delete with `ACCT=realliance-ci OLD=realliance-net-registry`, and **Chris** drops `REALLIANCE_NET_REGISTRY_TOKEN`.

  A token whose consumer couldn't move rotates by its runbook instead (R13). After each one, that consumer's next run still publishes.
- [ ] **Step 4: Run** Step 2's test. Expected: pass. Open the PR, then **STOP—ask Chris** to merge.
- [ ] **Step 5: Docs.** In `docs/forgejo.md`: the token tables; offboarding step 3, which goes, since no static token remains for a member to read; the realliance.net accepted-risk note, since publishing now needs `main` or a protected tag. Tasks 18–20 already changed the workflow comments and the fork README. vale, then push to `main` (docs).
- [ ] **Step 6: Chris** decides whether `realliance-ci`, which now holds no token, stays. Ledger it.

### Task 22: The flip to `restricted`

**Gate:** after Task 21; every Realliance workflow carries a block; members heard about it (Task 2 Step 5).

**Files:** `realliance/sso` branch `workflow-blocks`, `tests/forge/test_forge_job_token.py`; luma-homeops `cluster/applications/forgejo-release.yaml`, `docs/forgejo.md`.

- [ ] **Step 1: Failing first.** `test_realliance_workflows_declare_permissions` reads every workflow of the non-mirror repos of `realliance` and `realliance-ops`, at the default branch. It fails naming each job that has neither a job block nor a top-level block. Run it. Expected: it names only workflows added since Task 0. Each named repo gets a block by a PR, or Chris asks its maintainers. Run again. Expected: pass. Open the PR, then **STOP—ask Chris** to merge.
- [ ] **Step 2: The flip.** Set `DEFAULT_TOKEN_PERMISSIONS: restricted`, and make the docs section say so. The tests pass. **Gate G, then STOP—ask Chris.**
  - Downtime: one Recreate, 17–29 s.
  - Watch for a day: every run's outcome, from `fj … actions tasks` per repo each morning and evening, into the ledger.
  - Rollback trigger: a Realliance workflow failing for a right its block grants, which is a bug, so go back to `permissive`. A repo that breaks for lack of a block gets the override `permissive` on its Job token page while it adds one.
  - Push, `Monitor` the rollout, Gate G after.
- [ ] **Step 3: Close.** Ledger the day's runs and each spec Goal with its evidence.
