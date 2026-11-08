# Forgejo fork: job token permissions and secret scoping: design

- **Date:** 2026-11-08.
- **Status:** draft for Chris's review. Nothing built.
- **Research:** Claude's token-permissions report of 2026-11-08 (session scratchpad, not kept). It
  read the fork's source, upstream `forgejo` (head `14d5d663e`), Codeberg, Gitea and runner v13.2.1.
  Constraints and Facts restate what this design rests on.
- **Related:** fork image plan `docs/superpowers/plans/2026-11-06-forgejo-fork-image.md` and its
  research spec; forge-bot spec `2026-11-02-forge-bot-and-ci-design.md`; runner classes spec
  `2026-11-04-runner-classes-design.md`; Pages spec `2026-11-03-realliance-pages-design.md`;
  `docs/forgejo.md`; `docs/ci.md`. The fork lives at `git.realliance.net/realliance/forgejo`
  (local clone `~/Repos/realliance-forgejo-ci`).
- **Markers:** *(Chris decided)* marks a decision Chris made on 2026-11-08 unless dated. *(Claude's
  call)* marks a default this doc chose; Chris may override any of them.
- **Line numbers** are from `upstream/` in the fork clone with today's queue applied (`realliance`
  at `62033a4b4`, on v15.0.11). Runner lines are from `code.forgejo.org/forgejo/runner` v13.2.1, or
  from v12.10.2 where marked.

## Intent

On the forge, the job token (`GITHUB_TOKEN`, `FORGEJO_TOKEN`, `${{ github.token }}`) can write
everything in its own repo and nothing in the package registry. Publishing needs a static
`write:package` token on a service account instead. Each such token reaches every package its owner
has, and any member who can push a branch can read it. On top of that, Forgejo sends **every** org
and repo secret to every job's runner, even secrets the job never names. That is how the fork's
`REGISTRY_TOKEN` comes to sit in the memory of the paid `medium` build runner, next to `npm ci`.

Three patches in the fork's queue fix this:

- **A** sends a job only the secrets it names;
- **B** lets a workflow narrow or grant the token per unit with GitHub's `permissions:`, on top of
  an instance default and a per-repo override;
- **C** finishes the `// TODO: ActionUser permission check` in `services/packages/perm.go:27`, so
  that a job on a protected ref can publish its own repo's image with `packages: write` and no
  static token.

Then the three static registry tokens retire, and the default flips to read-only.

## Goals

1. A job's runner receives only the secrets its job names, plus the job token.
2. `permissions: {}` leaves the token unable to push, dispatch, comment or release. A job block
   replaces the workflow block, as on GitHub.
3. An instance default (`permissive` or `restricted`) and a per-repo override exist from the first
   release. The later flip to `restricted` is a config change, not a patch.
4. A job on a protected ref publishes `<owner>/<repo>` images with `packages: write`, and nothing
   else.
5. `REGISTRY_TOKEN` (realliance/forgejo), `REGISTRY_TOKEN` (realliance-ops org, `ops-ci`) and
   `SITE_REGISTRY_TOKEN` (realliance.net) are all revoked.
6. The patches stay small and easy to rebase: three files in the queue, about 700 production lines, no
   migration, and one place that decides the token's rights.

## Non-goals

- An org-level default. Later, if needed, through the `user_setting` table, which needs no
  migration.
- Gitea's per-unit maximums and cross-repo access grants.
- OIDC through `permissions: id-token`. `enable-openid-connect:` stays the switch, as upstream has
  it.
- Package types other than container and generic, and package deletion by the job token.
- A server-side refusal to move an existing image tag. The publish scripts keep that guard.
- Processes that outlive a step on long-lived runners, and the one-job runner credential that job
  code can read. Known limits covers both.
- Authorized Integrations. They arrive with the 19 LTS (Carry to 19).

## Anti-goals

The design fails if any of these happen, even when everything else works:

- **A migration**, a new `xorm` tag, or anything `scripts/check-patches.sh` flags.
- **A token that can write a package** not named after its repo, or from a pull request, a fork, an
  unprotected ref, or a job that didn't ask.
- **A token that can delete a package.**
- **A dropped secret:** a job that names a secret and doesn't get it.
- **A deploy that breaks a workflow.** The default stays `permissive` until the flip step.
- **Non-GitHub permission keys.** From v17 the runner's schema reads them, and it rejects unknown
  keys.
- **A token or secret value in a log line, an error or a test fixture.**

## Constraints

- **No migrations** *(Chris decided, 2026-11-05)*. The same-version upstream image must stay a boot
  fallback. Settings go in existing JSON or key/value storage: the Actions unit's config is JSON in
  `repo_unit.config TEXT` (`models/repo/repo_unit.go:76-83, 223-255`). Upstream main adds fields
  there the same way (`ActionsConfig.OIDCSubjectFormat`, `json:",omitempty"`).
- **Small, and easy to rebase**, carried onto the 19 LTS in about May 2027 (fork README). One resolver
  function holds every token decision, so a rewrite upstream touches one place.
- **Upstream won't converge on `permissions:`.**
  - [#13164](https://codeberg.org/forgejo/forgejo/pulls/13164) (v16) shows a warning on every
    workflow with `permissions:`, which Forgejo ignores, and points to Authorized Integrations.
    Maintainers on [#13079](https://codeberg.org/forgejo/forgejo/pulls/13079) reject the key: the
    goal is a read-only token; workflow-granted `packages: write` lets a code writer escalate; and
    linking a package to a repo carries no permission meaning.
  - They point people to Authorized Integrations instead (v16;
    [docs](https://forgejo.org/docs/v16.0/user/authorized-integrations/), which need migrations).
    They plan a per-repo read-only switch
    ([#13183](https://codeberg.org/forgejo/forgejo/issues/13183#issuecomment-17992532)) and
    per-repo service accounts ([#14026](https://codeberg.org/forgejo/forgejo/issues/14026)).
  - Gitea 1.26 shipped token permissions
    ([#36173](https://github.com/go-gitea/gitea/pull/36173)) with a database column, and removed
    package access before merging.
  - That makes A an upstream candidate. B's repo switch matches #13183. B's parser and C stay in
    the fork.
- **Forgejo 15 drops `permissions:`.** Runner v12.10.2's job parser has no field for it
  (`act/jobparser/model.go:15-22, 124-139`), so `WorkflowPayload` lacks it at both levels. The run
  records the file and commit, though: `WorkflowDirectory`/`WorkflowID` (`models/actions/run.go:50-51`)
  and `GetWorkflowSourceCommit()` (`run.go:292-297`). From v17, runner v13's job parser keeps
  `permissions` in the payload (`act/jobparser/model.go:29, 166`).
- **Realliance today.** All six relevant repos and both orgs are public. Release tags are already
  protected: forgejo, brand and actions `v*`, realliance.net `[0-9]*`, forge-bot and ci-dispatch
  `v*` (`realliance-ops/forge` `repos.tf:103-122, 193-196`). Each repo also protects `main`.
- **Runner v13.2.1** keeps a job's secrets in its own memory (`internal/app/run/runner.go:358`). It
  puts only referenced ones into steps (`act/runner/expression.go:513-537`), and it reads
  `ACTIONS_STEP_DEBUG` itself (`internal/pkg/report/reporter.go:97`). Steps run as children of the
  runner with its uid.
  - On galaxy, Claude read `ptrace_scope=1`, `CapEff=0` and uid 1000 from a runner pod, so job code
    can't read runner memory there.
  - On the paid ACI classes, the job runs as root in the runner's container, and that question is
    open (Rollout step 2).

## Decided

*(Chris decided)*, 2026-11-08:

- Ship A, B and C as one fork series.
- **A:** referenced secrets only.
- **B:** `permissions:` with a **`permissive` default now**. The instance setting and the per-repo
  override exist from the first release. Realliance's workflows get blocks: `pages-publish` callers
  `contents: write`, `build` jobs `{}`. A later step flips the instance default to `restricted`.
- **C:** `packages: write`, shipped now.
- A separate paid-class probe runs now. If job code on ACI can read the runner's memory,
  `REGISTRY_TOKEN` rotates after A lands.

*(Claude's call)*:

- `packages: write` requires a protected ref, and each publishing repo keeps a protected-tag rule.
  Most exist already.
- The rest of the rules in Design:
  - the name rule, container and generic only;
  - the event allowlist, never delete;
  - dynamic `secrets` uses send everything;
  - `permissive` grants no package write;
  - the job block replaces the workflow block;
  - `contents` covers code, releases and wiki;
  - permissions come from the workflow file at the run's source commit;
  - three patch files rather than one;
  - the order in which repos move.

Three patch files, because each needs its own `Upstream:` trailer: A `candidate`, B and C
`not-needed`. They ship in one PR and one release.

## Design

### Patch A: secrets by reference

`getSecretsOfTask` (`services/actions/secret.go:20-26`) filters the map from `getSecretsOfJob`
before adding the three token names. It runs on the task's own job only. The recursion that builds
an inner reusable job's set (`secret.go:64-144`) keeps the caller's full set, so `secrets: inherit`
and `secrets:` mappings still work.

| Rule | Behaviour |
|---|---|
| What counts as a reference | `secrets.NAME` or `secrets['NAME']` in any scalar of the task job's `WorkflowPayload`: `run`, `env`, `with`, `if`, container and service credentials, workflow `env`. Matched case-insensitively, like Forgejo's secret names |
| Dynamic use | Any other use of `secrets`, such as `toJSON(secrets)` or `secrets[matrix.x]`, sends every secret, as GitHub does. Only someone who edits the workflow can write one, and that is already the accepted risk |
| Always sent | `GITHUB_TOKEN`, `GITEA_TOKEN`, `FORGEJO_TOKEN`, `ACTIONS_STEP_DEBUG`, `ACTIONS_RUNNER_DEBUG` |
| Error direction | A stray match only sends one extra secret. Missing a reference is the failure to test for |
| Unchanged | Fork PRs get no secrets (`secret.go:43-48`). Skipped steps still count, because they sit in the payload |

### Patch B: `permissions:` and the default

**Scopes.** Only GitHub's keys *(Claude's call)*.

| Key | Forgejo units |
|---|---|
| `contents` | `TypeCode`, `TypeReleases`, `TypeWiki`, plus read on the external wiki and tracker. Commit statuses need code write (`routers/api/v1/api.go:1010`) |
| `issues`, `pull-requests`, `actions`, `repository-projects` | `TypeIssues`, `TypePullRequests`, `TypeActions` (dispatch, runs), `TypeProjects` |
| `packages` | Owner-level, patch C. The repo's packages tab follows it |
| `id-token`, `statuses`, `checks`, `deployments`, `discussions`, `security-events`, `attestations`, `pages` | Ignored. An unknown key or a bad level reads as `none` and logs once per task |

**Levels and forms.** Each key takes `read`, `write` or `none`.
- `read-all` and `write-all` set every key.
- `{}` sets none.
- Metadata always stays readable, so the repo never answers 404.
- A key the block leaves out is `none`.

**Defaults.** One instance setting, plus a repo override in the Actions unit's JSON
(`ActionsConfig.TokenPermissions`, empty meaning "follow the instance"). Repo admins set the
override on a new page, Settings → Actions → Job token, under the repo settings routes
(`routers/web/web.go:1189-1201`, `reqRepoAdmin`). It writes the field the way the switch that
turns a workflow off does (`routers/web/repo/actions/view.go:877-900`).

```ini
[actions]
; permissive: every repo unit write, packages read (Forgejo 15's behaviour).
; restricted: contents and packages read, everything else none (GitHub's restricted default).
DEFAULT_TOKEN_PERMISSIONS = permissive
```

`permissive` deliberately grants no package write *(Claude's call)*: no job gets new power without
asking for it. `write-all` in a workflow does include `packages: write`, as on GitHub.

**Resolution**, first match wins, then the clamps:

1. the job's `permissions:` block, which replaces the workflow's block;
2. the workflow's top-level block;
3. the repo override;
4. the instance default.

| Clamp | Rule |
|---|---|
| Fork PRs (`IsForkPullRequest`, which includes fork `pull_request_target`, `services/actions/trust.go:150`) | Read at most on every key, whatever the workflow says. Same as today |
| Reusable inner jobs | At most the caller job's result, lowered by the called workflow's own block when local |
| Other repos | Unchanged: the `forgejo-actions` user's own rights. Read on public repos, none on private (`repo_permission.go:167`); 404 in the API (`repo_access.go:24-27`) |

**Source.**
- The resolver reads `WorkflowDirectory/WorkflowID` at `GetWorkflowSourceCommit()`, takes the
  top-level and `jobs.<JobID>` blocks, and caches the result per task ID in process memory. A
  task's rights never change. The cache is neither a queue nor a cron, so `check-patches.sh`
  passes it.
- For a fork PR it skips the read and clamps.
- If the read fails, it fails closed to the default and logs the task.
- An inner job finds its caller with `FindOuterWorkflowCall`, as `secret.go:72` does.

**Enforcement.** One resolver in `services/actions` (the builder names it) replaces the four
inline copies of "own repo write, fork read":

| Path | Change |
|---|---|
| REST API, `routers/api/v1/permissions/repo_access.go:16-43` | Call the resolver. Releases, issues, PRs, wiki, labels, statuses and dispatch all pass through here |
| git transport and wiki, `routers/web/repo/githttp.go:192-210` | Call the resolver. At `:210`, hand the hook the unit's level (`UnitAccessMode` of code or wiki), not `AccessMode` |
| Pre-receive hook, `routers/private/hook_pre_receive.go:598-612` | No change. It applies that one integer to all units, which is now the code or wiki level |
| LFS, `services/lfs/server.go:544-559` | Call the resolver, code unit |
| `models/perm/access/repo_permission.go:143-168` | Kept for other repos, and as the base the resolver narrows |
| Not touched | Web UI routes, the artifact and cache endpoints (they check the runtime JWT themselves, so `permissions: {}` still uploads artifacts), and branch and tag protection |

### Patch C: the package rule

**Write rule.** The job token may write a package only when these all hold:

1. the task's resolved `packages` level is `write`;
2. the package owner is the task repo's owner;
3. the type is **container** and the image, compared in lower case, is `<repo>` or starts with
   `<repo>/`, or the type is **generic** and the package name is `<repo>`;
4. the run is not a fork PR, and its `TriggerEvent` is `push`, `workflow_dispatch`, `schedule` or
   `release`;
5. the run's `Ref` is a branch with a protection rule (`GetFirstMatchProtectedBranchRule`,
   `models/git/protected_branch_list.go:80`), or a tag that a protected-tag rule matches
   (`models/git/protected_tag.go:52, 95`);
6. the request is not a delete. C never lets the job token delete container blobs or manifests,
   or generic packages or files.

**Read rule.** The task owner's packages are readable whenever `packages` resolves to `read` or
better, even for a private owner. Other owners keep today's rule (`services/packages/perm.go:18-72`).

**Why these rules** *(Claude's call)*:
- The name rule matches the convention Forgejo already uses to link images to repos
  (`services/packages/container/manifest.go:660-676`) and needs no stored state.
- Container and generic have no owner-wide index, which answers the shared-index objection.
- Package-to-repo links have no permission meaning, the first repo to push could claim an unlinked
  package, and the pusher sets an image's `org.opencontainers.image.source` label about itself.
- Protected refs answer the escalation objection. A code writer gets the grant only through a ref
  they can't push to directly, which is what an Authorized Integration's ref and event claim rules
  express. This is stricter than today, where any member who can push a branch can read
  `SITE_REGISTRY_TOKEN`.

**The `/v2/token` change.**
- Today `container.Authenticate` (`routers/api/packages/container/container.go:148-172`) turns a
  task-token login into a package JWT that holds only user −2 (`services/packages/auth.go:21-46`).
  The next `/v2` request then loses the task (`container/auth.go:45-71`).
- The patch adds a `TaskID` claim when the login is an Actions task. `container.Auth.Verify` then
  returns an Actions-task result for that ID if the task is still running (`CheckTaskIsRunning`,
  `services/auth/method/oauth2.go:103-112`).
- A JWT outlives its task harmlessly: once the task stops, Forgejo refuses the next `/v2` request.

**Enforcement.**

| Path | Change |
|---|---|
| `reqPackageAccess`, `routers/api/packages/api.go:45-91` | For an Actions task, apply the write and read rules with the route's name parameter (`image` or `packagename`). Every other type keeps today's rule |
| Container manual routes, `api.go:889-950` | Move `SetParams("image")` ahead of the access check in the three write branches |
| Generic routes, `api.go:493-505` | Covered by `reqPackageAccess` |
| Auto-link, `container/manifest.go:418-435` and `services/packages/package_update.go:19-39` | A −2 creator links the image to the repo once the name rule held, so the package shows on the repo page |
| Quota, `api.go:93-114` | No change: it charges the owner |

## Tests

Tests follow the upstream style: integration tests in `tests/integration` built on the
`createActionsTestRepo` and `createWorkflowFile` helpers (`actions_job_test.go:736, 772`) and the
mock runner's `fetchTask` (`actions_runner_test.go:175`). The task token comes from the fetched
task's context (`actions_job_test.go:476-478`). Each patch also gets table-driven unit tests next
to its code.

| Patch | Integration cases |
|---|---|
| A | Repo and org secrets `S1`, `S2` and `S3`. A job naming `S1` in `env` and `S2` in `if` receives exactly those plus the five always-sent names. `toJSON(secrets)` receives all. A `secrets: inherit` inner job naming `S3` receives `S3`. A fork PR receives none |
| B | One case per row of a matrix over instance default × repo override × workflow block × job block × fork PR. Each case tries: issue comment, release create, git push, wiki push, workflow dispatch, LFS upload. Also: a job block replacing the workflow block, `{}` still uploading an artifact, and the reusable inner-job cap. Declarative cases go in `routers/api/v1/permissions/tests/repo_access.go`, whose fixture already models tasks |
| C | In the `api_packages_container_test.go` style (`/v2/token` with basic auth, `:104-178`):<br>• a protected-tag job pushes `<owner>/<repo>` and `<owner>/<repo>/sub`;<br>• Forgejo refuses `<owner>/<other>`;<br>• it also refuses a `pull_request` task, an unprotected branch, a job without `packages: write`, and a delete;<br>• it refuses a JWT after its task ends;<br>• a generic upload works under `<repo>` and fails under another name;<br>• auto-link sets the repo. Tests create protected tags as `api_repo_tags_test.go:179` does |

Every case runs with `DEFAULT_TOKEN_PERMISSIONS` both `permissive` and `restricted`, so the tests
cover the flip before it ships.

## Rollout

Each step waits for the one before it. Every forge change follows `docs/forgejo.md` and S1–S9.

1. **Fork release.**
   - A, B and C merge into `realliance/forgejo` `main` by PR, signed. `make check` and the fork's
     `check-patches.sh` pass.
   - An Owner tags `v15.0.11-realliance.N`. `image.yml` builds it. This build still publishes with
     `REGISTRY_TOKEN`.
2. **Forgejo rollout.**
   - luma-homeops pins the reviewed digest in `cluster/applications/forgejo-release.yaml` and sets
     the default explicitly under `gitea.config.actions`.
   - Run the runner drill (`docs/ci.md`, "Runner drill at every Forgejo upgrade") and one run of
     each Realliance workflow. Expected: nothing changes, because the default is `permissive`.
   - The same values change opens org creation, once its prerequisites are live (Open org creation
     for users).
   - **Probe hook.** If the paid-class probe found that job code can read the runner's memory
     (Yama `ptrace_scope` 0 or absent, or `CAP_SYS_PTRACE` present), rotate `REGISTRY_TOKEN` now,
     after A is live. Use "Rotating `realliance-ci`'s tokens" in `docs/forgejo.md`; it's a STOP for
     Chris. If the probe found it can't, record that and skip the rotation.
3. **Workflow blocks**, each a PR in its repo:
   - `image.yml`: `build` gets `permissions: {}`, and `publish` gets `{}` while it still uses the
     static token. The accepted push-and-dispatch risk in the fork README closes; update that text.
   - realliance.net: split `ci.yaml` into `build` (`{}`) and a push-only `publish` job that
     receives the OCI layout as an artifact, as `image.yml` does. Pull-request runs then get no
     secret at all.
   - forge-bot and ci-dispatch: `test` and `image` get `{}`.
   - `realliance/actions`: the `pages-publish` README tells callers to set `contents: write` on the
     publishing job. Announce it to members.
4. **Static tokens to `packages: write`**, one repo at a time *(Claude's call: the fork first,
   because its build runs on paid ACI; realliance.net last, because Renovate deploys its pushes)*.
   Order: realliance/forgejo, forge-bot, ci-dispatch, realliance.net.
   - For each repo: confirm the protected-tag rule and the `main` protection exist. Set the publish
     job to `permissions: { packages: write }` and log in with the job token. Then check one real
     publish: the digest pin in the log, the package creator showing as `forgejo-actions`, and the
     package linked to the repo.
   - The fork's `workflow_dispatch` `sha-<7>` builds then publish only from protected branches
     *(Claude's call)*. If Chris wants branch builds, add a protection rule for an `image/*` branch
     pattern with Owners-only push.

   ```yaml
   publish:
     permissions:
       packages: write
     steps:
       - name: Publish to the forge registry
         env:
           REGISTRY_TOKEN: ${{ github.token }}
         run: scripts/publish-image.sh image "$TAG"
   ```

5. **Retirements**, each only after its consumer has published with `packages: write`. Each is a
   database write and a STOP for Chris:
   - realliance/forgejo `REGISTRY_TOKEN`: delete the repo secret, revoke `realliance-forgejo-registry`
     on `realliance-ci`, and drop `REALLIANCE_FORGEJO_REGISTRY_TOKEN` from `sso-secrets`.
   - realliance-ops org `REGISTRY_TOKEN` (`ops-ci`): only after both forge-bot and ci-dispatch have
     moved.
   - realliance.net `SITE_REGISTRY_TOKEN`: revoke `realliance-net-registry` and drop
     `REALLIANCE_NET_REGISTRY_TOKEN`.
   - With each one, update the realliance/sso tests (`test_realliance_ci_tokens`,
     `test_site_registry_token_scope`), the token tables, offboarding step 3 and the accepted-risk
     notes in `docs/forgejo.md`, and the realliance.net and forge-bot workflow comments.
   - Once `realliance-ci` holds no token, Chris decides whether the account stays.
6. **Flip to `restricted`.**
   - Before: every Realliance workflow carries a block, and the members who call `pages-publish`
     know to add `contents: write`.
   - Set `DEFAULT_TOKEN_PERMISSIONS: restricted`, roll out, and watch a day of runs.
   - A repo that breaks gets the per-repo override `permissive` while it adds a block.

## Open org creation for users

A companion change that ships in the same rollout. *(Chris decided)*:

- Normal forge users, guests included, may create their own orgs. `realliance` and
  `realliance-ops` stay in the IaC (`realliance-ops/forge`).
- An org that `ci.yaml` doesn't list runs on `tiny` only, with no paid classes. An org that needs
  `small` or `medium` gets an `owners:` entry by PR.
- **Order:**
  1. the forge-bot fix;
  2. the Forgejo config change, riding Rollout step 2;
  3. tests and docs.
- The ci-dispatch change ships with the forge-bot fix, because both have to be live before the flip
  *(Claude's call)*.

| Piece | Change | Marker |
|---|---|---|
| forge-bot Owners (the blocker) | **Today:** `Owners()` (`forge-bot internal/forgejo/client.go:332-356`) reads the Owners team of every org in `/admin/orgs`. The `access`, `admin`, `teams` and `avatar` plans (`internal/plan/access.go:38`, `admin.go:27`, `teams.go:76`, `avatar.go:54`) never take access from anyone in that set; they log a finding instead. Every org creator would then escape automatic removal.<br>**Fix:** `forge-bot.yaml` gains `owner_orgs: [realliance, realliance-ops]`, read only at an Owner-signed head, like the rest of that file. `Owners()` reads only those orgs. A missing or empty list fails the read, so the jobs that take access away skip, as a missing Owners team does today (`ErrIncomplete`).<br>**Size:** about 40 lines, plus a test that forge-bot takes access from an Owner of a user org like anyone else. Release and deploy it before the flip | Chris decided (the fix). Claude's call: an explicit list rather than `teams.yaml` keys, because `realliance-ops` has no mapped team |
| Forgejo config | In `cluster/applications/forgejo-release.yaml`: `service.DEFAULT_ALLOW_CREATE_ORGANIZATION: true` (`:236`) and `admin.DISABLE_REGULAR_ORG_CREATION: false` (`:244`). This goes in the values change of Rollout step 2 | Chris decided |
| Existing accounts | `DEFAULT_ALLOW_CREATE_ORGANIZATION` only sets new accounts. A one-time, guarded loop over `/api/v1/admin/users` patches `allow_create_organization: true`, with `login_name` and `source_id` as the removal step sends them. Chris runs it (a STOP).<br>**Why not a forge-bot job:** after the flip, no new account needs it, and a reconciler would undo a site administrator who turns the flag off for one person after abuse | Claude's call |
| ci-dispatch | **Today:** it budgets by the job's `owner_id`, and gives every unlisted owner `defaults`, $10 of paid included (`ci-dispatch internal/limits/limits.go:155-165`, `internal/gate/gate.go:274-279`). It can't tell an org from a user.<br>**Change:** `ci.yaml` gains `org_defaults:` (paid 0, `tiny` as in `defaults`). The dispatcher reads org ids from `/admin/orgs` with each limits load and every 10 min. Lookup order: an `owners:` entry, then `org_defaults` for an org, then `defaults`. If the dispatcher can't read the org list, unlisted owners get `org_defaults`, so money fails closed and a paid job holds `no-budget`, as it does today.<br>**Size:** about 100 lines plus tests.<br>`realliance-ops`, unlisted, drops to `tiny` only, which is harmless because its CI runs on `galaxy`. Guests stay on user `defaults` | Chris decided (the rule, and guests). Claude's call (the shape, failing closed) |
| Pages | No change. The relay maps any public owner to `<owner>.realliance.page` (`images/pages-relay/relay.go:172-245`), so a public user org gets `<org>.realliance.page`, with `<org>/pages` as its root site. Takedown stays Owner-only. No reserved-name list: `realliance.page` is its own domain, and user sites carry the same exposure today | Claude's call |
| Team sync | Both group-to-team paths name only `realliance` teams: the login source's group-team map (`forgejo-release.yaml:193-194`) and forge-bot's `teams.yaml`, whose plan touches listed teams only (`teams.go:15, 42-44`). Neither reaches a user org | Fact; no change |
| Leavers | The `docs/forgejo.md` section Removing someone gets a step: list the orgs where the person is the only Owner. Leave each org untouched, so its repos keep serving, and add it to `ci.yaml` `deny` if it still runs CI. Take its Pages sites down only on request or abuse. Hand the org to a remaining member only when that member asks and Chris agrees | Claude's call |
| Many orgs per user | No per-user cap. The global `tiny` and paid caps bound the cost | Claude's call |

**Flips.**
- **Tests:**
  - realliance/sso `tests/forge/test_forge_signin.py::test_member_cannot_create_org` (`:49-53`) becomes `test_member_can_create_org`: a 201, after which the test deletes the org;
  - a ci-dispatch gate test that an unlisted org's paid job holds `no-budget`;
  - the forge-bot test in the table.
- **Docs:**
  - `docs/forgejo.md`: the Owners paragraph and the org-creation rule (`:74-77`), plus the new removal step;
  - the "orgs are code" comment in `forgejo-release.yaml`;
  - `docs/ci.md` (unlisted orgs run on `tiny` only);
  - the `realliance-forge` `ci.yaml` comments and README;
  - forge-bot's README Owners rows;
  - the SSO invites spec, "What a guest gets" (`2026-11-07-sso-member-invites-design.md:262`, which says guests can't create orgs).

## Carry to the 19 LTS

| Patch | At the rebase |
|---|---|
| A | Rebase onto `secret.go`'s `CanAccessSecrets` and job namespaces (main `models/actions/run_job.go:217-235`). Open a Codeberg issue before sending it upstream, as maintainers asked of #13079 |
| B | Replace the git read with the payload read (runner v13 keeps `permissions`). Stop the upstream `WarningCodePermissions` (main `services/actions/notifier.go:836-851`), which would otherwise flag every block. If upstream has shipped #13183's read-only switch, map `ActionsConfig.TokenPermissions` onto it and keep only the parser. If #14026's service accounts have landed, re-express the resolver as a reducer |
| C | v16 split the auth methods (`ActionTaskToken`; main `routers/api/packages/api.go:195-210, 852-866`), and `CreateAuthorizationToken` gained `maxExpiry`. Rebase the `TaskID` claim onto both. Then Chris chooses between keeping C (a per-name grant) and Authorized Integrations owned by `realliance-ci` (registry-wide write, gated by repo, ref and event claims) |

Anchors that upstream hasn't changed so far: `repo_permission.go`, `repo_access.go`,
`auth_result_actionstask.go`, `container/auth.go`, and the Actions blocks in `githttp.go` and
`services/lfs/server.go`.

## Known limits (accepted)

- On long-lived host runners (`galaxy`, `general`), a process that a step leaves running can read
  a later step's `environ`, which includes that step's referenced secrets. Not verified. Ephemeral
  classes don't have this.
- The one-job runner credential is readable by job code (`/run/ci/token` on paid, the Secret volume
  on `tiny`). It works for one task only.
- A step in a job that holds `packages: write` holds it for the whole job. That includes third-party
  action steps such as `download-artifact`, which also hold the static token today.

## Left to the builder

- File and function names, the scanner for A, and the cache's size and eviction.
- The Job token settings page: its template, locale strings and flash messages.
- How to match protected tags: reuse `matchString`, private today, through a small exported helper,
  or `GetProtectedTags` plus a glob.
- Whether a schedule run's `CommitSHA` is the commit that registered its schedule. The git read
  relies on it.
- Whether a failed git read shows a run warning as well as the log line.
- Patch subjects, commit messages, `Upstream:` trailers, and the fork's README and WISHLIST rows.
- The luma-homeops doc changes the Rollout lists, and a `docs/forgejo.md` section on job token
  permissions.
