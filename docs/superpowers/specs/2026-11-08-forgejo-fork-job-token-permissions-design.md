# Forgejo fork: job token permissions and secret scoping: design

> **Amendment (2026-11-08, builder check).** Five rulings changed this doc, and the text below
> follows them:
>
> - ci-dispatch reads `/admin/orgs` and `/repositories/<id>` through two new GET-only guard shapes.
>   It keeps the last good org list for 1 h, and fails closed when the list is older or the owner id
>   is doubtful;
> - forge-bot requires `owner_orgs`, which changes all four consumers; it deploys before the file
>   gains the key;
> - the probe stopped without a verdict, so all three static tokens rotate after A unless retired;
> - `permissions:` blocks land before the fork image (Rollout step 1);
> - patch C never refuses silently: a 403 names the rule, and a warning goes to the log.

- **Date:** 2026-11-08.
- **Status:** draft for Chris's review, amended after the builder check. Nothing built.
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
  `permissions` in the payload (`act/jobparser/model.go:29, 166`). Neither Forgejo 15 nor runner
  v13.2.1 reads any key of the block, `id-token` included: OIDC follows `enable-openid-connect:`.
  A block can land in a workflow before the fork image does, and changes nothing until then.
- **Realliance today.** All six relevant repos and both orgs are public. Release tags are already
  protected: forgejo, brand and actions `v*`, realliance.net `[0-9]*`, forge-bot and ci-dispatch
  `v*` (`realliance-ops/forge` `repos.tf:103-122, 193-196`). Each repo also protects `main`.
- **Runner v13.2.1** keeps a job's secrets in its own memory (`internal/app/run/runner.go:358`). It
  puts only referenced ones into steps (`act/runner/expression.go:513-537`), and it reads
  `ACTIONS_STEP_DEBUG` itself (`internal/pkg/report/reporter.go:97`). Steps run as children of the
  runner with its uid.
  - On galaxy, Claude read `ptrace_scope=1`, `CapEff=0` and uid 1000 from a runner pod, so job code
    can't read runner memory there.
  - On the paid ACI classes, the job runs as root in the runner's container. Chris stopped a probe
    before it gave a verdict, so this design assumes job code there **can** read runner memory
    (Rollout step 3).

## Decided

*(Chris decided)*, 2026-11-08:

- Ship A, B and C as one fork series.
- **A:** referenced secrets only.
- **B:** `permissions:` with a **`permissive` default now**. The instance setting and the per-repo
  override exist from the first release. Realliance's workflows get blocks: `pages-publish` callers
  `contents: write`, `build` jobs `{}`. A later step flips the instance default to `restricted`.
- **C:** `packages: write`, shipped now.

*(Claude's call)*:

- The cancelled probe counts as proof that job code on ACI can read runner memory. After A is live,
  each of the three static registry tokens rotates, unless its consumer has moved on, and then Chris
  revokes it instead (Rollout step 3). Chris may override this.
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
| Own repo, the R1 floor | A token never holds less than the `forgejo-actions` user has on its own repo for **code, releases and wiki**, so `{}` can clone and download from a public repo (Task 11 ruling). Issues and pull requests are not in the floor: on Forgejo, issues read lets a token open issues and comment, so a floor there would give `{}` a write |
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
5. the run's `Ref` is a protected ref (Task 12 ruling). A branch counts only when its rule
   (`GetFirstMatchProtectedBranchRule`, `models/git/protected_branch_list.go:80`, as pre-receive
   reads it) restricts push (no direct push, or a push whitelist), restricts merge (a merge whitelist,
   or at least one required approval with stale approvals dismissed), and has no unprotected file
   patterns. A tag counts when any protected-tag rule matches it (`models/git/protected_tag.go:52, 95`);
6. the request is not a delete. C never lets the job token delete container blobs or manifests,
   or generic packages or files.

**Read rule.** The task owner's packages are readable whenever `packages` resolves to `read` or
better, even for a private owner. Other owners keep today's rule (`services/packages/perm.go:18-72`).

**No silent refusals** *(Claude's call)*. When a job holding `packages: write` fails a rule, it gets
a 403 whose message names the rule, and Forgejo logs a warning that names the task, the repo, the
package type and name, and the rule. For a type other than container or generic, the message reads:
the job token's `packages: write` covers only container and generic packages named after the
repository. Container routes carry the message in the OCI error body. Other types keep today's
refusal text for every other caller.

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
| `reqPackageAccess`, `routers/api/packages/api.go:45-91` | For an Actions task, apply the write and read rules with the route's name parameter (`image` or `packagename`). A write to any other type gets the logged 403 described earlier |
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
| C | In the `api_packages_container_test.go` style (`/v2/token` with basic auth, `:104-178`):<br>• a protected-tag job pushes `<owner>/<repo>` and `<owner>/<repo>/sub`;<br>• Forgejo refuses `<owner>/<other>`;<br>• it also refuses a `pull_request` task, an unprotected branch, a job without `packages: write`, and a delete;<br>• it refuses a JWT after its task ends;<br>• a generic upload works under `<repo>` and fails under another name;<br>• an npm publish under `packages: write` gets a 403 that names the rule, plus a warning in the log;<br>• auto-link sets the repo. Tests create protected tags as `api_repo_tags_test.go:179` does |

Every case runs with `DEFAULT_TOKEN_PERMISSIONS` both `permissive` and `restricted`, so the tests
cover the flip before it ships.

## Rollout

Each step waits for the one before it. Every forge change follows `docs/forgejo.md` and S1–S9.

1. **Workflow blocks**, each a PR in its repo, *before* the fork image *(Claude's call)*. Forgejo 15
   ignores the blocks (Constraints), so they change nothing yet. Landing them first lets step 3
   check the new image against real blocks.
   - `image.yml`: `build` gets `permissions: {}`, and `publish` gets `{}` while it still uses the
     static token.
   - realliance.net: split `ci.yaml` into `build` (`{}`) and a push-only `publish` job that
     receives the OCI layout as an artifact, as `image.yml` does. Pull-request runs then carry no
     secret reference, so patch A sends them none.
   - forge-bot and ci-dispatch: `test` and `image` get `{}`.
   - `realliance/actions`: the `pages-publish` README tells callers to set `contents: write` on the
     publishing job. Announce it to members.
2. **Fork release.**
   - A, B and C merge into `realliance/forgejo` `main` by PR, signed. `make check` and the fork's
     `check-patches.sh` pass.
   - An Owner tags `v15.0.11-realliance.N`. `image.yml` builds it. This build still publishes with
     `REGISTRY_TOKEN`.
3. **Forgejo rollout.**
   - luma-homeops pins the reviewed digest in `cluster/applications/forgejo-release.yaml` and sets
     `DEFAULT_TOKEN_PERMISSIONS: permissive` explicitly under `gitea.config.actions`.
   - Run the runner drill (`docs/ci.md`, "Runner drill at every Forgejo upgrade") and one run of
     each Realliance workflow.
     - Expected: every workflow still passes.
     - The blocks now bite: a `{}` job's token gets 403 on a push and on a dispatch.
     - A job that names no secret gets none.
   - The same values change opens org creation, once its prerequisites are live (Open org creation
     for users).
   - **Rotations.** The probe gave no verdict, so assume that job code on ACI could read every
     secret a runner held before A *(Claude's call; Chris may override)*. Once A is live, handle
     each of the three tokens as follows; each is a STOP for Chris:
     - if its consumer still needs it, rotate it at once;
     - if its consumer has already moved to `packages: write`, step 5 revokes it and no new value is
       minted.

     | Token | How |
     |---|---|
     | `REGISTRY_TOKEN` (realliance/forgejo) and `SITE_REGISTRY_TOKEN` | "Rotating `realliance-ci`'s tokens" in `docs/forgejo.md` |
     | realliance-ops `REGISTRY_TOKEN` (`ops-ci`) | The same guarded delete and mint, with `ACCT=ops-ci`. It has no runbook yet; the builder writes one beside `realliance-ci`'s |
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
   - With each one, update:
     - the realliance/sso tests (`test_realliance_ci_tokens`, `test_site_registry_token_scope`);
     - in `docs/forgejo.md`: the token tables, offboarding step 3 and the accepted-risk notes;
     - the fork README's accepted push-and-dispatch risk, which closed in step 3;
     - the realliance.net and forge-bot workflow comments.
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
  2. the Forgejo config change, riding Rollout step 3;
  3. tests and docs.
- The ci-dispatch change ships with the forge-bot fix, because both have to be live before the flip
  *(Claude's call)*.

### forge-bot: Owners of Realliance's orgs only

**Today.** `Owners()` (`forge-bot internal/forgejo/client.go:332-356`) reads the Owners team of
every org in `/admin/orgs`. Four plans spare anyone in that set and log a finding instead:

- `access` never prohibits login (`internal/plan/access.go:38`);
- `admin` never removes the site administrator flag (`admin.go:27`);
- `teams` never removes from a mapped team (`teams.go:76`);
- `avatar` never applies its loss change for a user without SSO access (`avatar.go:54`).

With open org creation, every org creator would escape all four.

**Fix** *(Chris decided; the details are Claude's calls)*:
- `forge-bot.yaml` gains a **required** field, `owner_orgs: [realliance, realliance-ops]`, which
  forge-bot reads only at the Owner-signed head, like the rest of that file.
- The field is an explicit list rather than `teams.yaml` keys, because `realliance-ops` has no mapped
  team.
- `Owners()` reads only the listed orgs. If the field is absent, or a listed org is missing or has
  no Owners team, the read fails with `ErrIncomplete`. The whole Forgejo read then fails
  (`internal/cycle/read.go:190-194`), so no loss job runs without the list.
- **All four consumers change.** forge-bot now treats an Owner of a user org like anyone else: it prohibits login,
  removes the site administrator flag, and removes them from `realliance` teams, as intended.
- `owners_unknown` (`FindingOwnersUnknown`, `internal/plan/plan.go:64, 101`; `avatar.go:67`) and
  `owner_needs_action` (`plan.go:165-166`, `internal/metrics/metrics.go:34`) now concern
  Realliance's Owners only. The forge-bot README's Owners row (`README.md:61`) and `docs/forgejo.md`
  say so.
- **Size:** about 50 lines, plus tests:
  - an Owner of a user org loses access, the site administrator flag and `realliance` team membership;
  - an absent `owner_orgs`, or a listed org without an Owners team, fails the read.

**Deploy order.** Today's forge-bot decodes `forge-bot.yaml` strictly: `KnownFields(true)` in
`internal/inventory/parse.go:60-62`, used by `BulkUntil` at `:116-120`. An unknown key makes it
reject the file (`read.go:112-115`). So:

1. deploy the new forge-bot, which accepts the key and requires it;
2. merge `owner_orgs` into the inventory.

Between the two, the new forge-bot's Forgejo read fails and nothing runs, which is safe; keep the gap
short. The builder confirms what today's forge-bot does with a rejected file before relying on this
order.

### Budgets for orgs in ci-dispatch

**Today.**
- It budgets by the job's `owner_id`, and gives every unlisted owner `defaults`, $10 of paid
  included (`ci-dispatch internal/limits/limits.go:155-165`, `internal/gate/gate.go:274-279`). It
  can't tell an org from a user.
- The token already holds `read:admin` and `read:repository` (`docs/ci.md:520`), but the transport
  guard allows only the `/admin/actions/runners` routes, `/users/<n>` and some `/repos/…` shapes
  (`internal/forgejo/guard.go:49-58`).

**Change** *(Chris decided the rule; the shape is Claude's call)*:
- **Config.** `ci.yaml` gains `org_defaults:` (paid 0, `tiny` as in `defaults`).
- **Guard.** Two GET-only shapes, each with a guard test in `TestGuardAllowList`
  (`internal/forgejo/writes_test.go:151`):
  - `/admin/orgs` with `page` and `limit`;
  - `/repositories/<id>`, for the owner check that follows.

  Writes stay runner register and delete only, and the README's bullet on reads and writes
  (`README.md:37-38`) names both reads.
- **Classification**, by the same `owner_id` that budgets the job:
  - an `owners:` entry always wins, and a stale or missing org list never affects it;
  - else, an id in the org list gets `org_defaults`;
  - else, `defaults`, but only when the dispatcher can match the id with confidence.
- **Confidence.** The job's `owner_id` must equal the current owner of its `repo_id`
  (`/repositories/<id>`, cached 10 min). A run from before a repo transfer keeps the old owner id
  (`internal/forgejo/client.go:239-242`). A mismatch, or a failed repo read, counts as an org, which
  fails closed.
- **Freshness.** The dispatcher refreshes the org list with each limits load and every 10 min.
  After a failed read it keeps the last good list. If that list is over 1 h old, or none exists
  (a cold start), every unlisted owner gets `org_defaults`. A gauge for the list's age drives a new
  alert, `CIDispatchOrgListStale`, in `cluster/ci-dispatch/dispatcher/prometheusrule.yaml`, with a
  case in `alerts_test.yaml`.
- A paid job that lands on `org_defaults` holds `no-budget`, as it does today.
- **Size:** about 200 lines, plus about 250 lines of tests (config parse, classification including
  the stale owner and cold start, gate, guard) and about 25 lines of alert YAML.
- `realliance-ops`, unlisted, drops to `tiny` only. That is harmless, because its CI runs on
  `galaxy`.
- Guests stay on user `defaults` *(Chris decided)*.

### The rest

| Piece | Change | Marker |
|---|---|---|
| Forgejo config | In `cluster/applications/forgejo-release.yaml`: `service.DEFAULT_ALLOW_CREATE_ORGANIZATION: true` (`:236`) and `admin.DISABLE_REGULAR_ORG_CREATION: false` (`:244`). This goes in the values change of Rollout step 3 | Chris decided |
| Existing accounts | `DEFAULT_ALLOW_CREATE_ORGANIZATION` only sets new accounts. A one-time, guarded loop over `/api/v1/admin/users` patches `allow_create_organization: true`, with `login_name` and `source_id` as the removal step sends them. Chris runs it (a STOP).<br>**Why not a forge-bot job:** after the flip, no new account needs it, and a reconciler would undo a site administrator who turns the flag off for one person after abuse | Claude's call |
| Pages | No change. The relay maps any public owner to `<owner>.realliance.page` (`images/pages-relay/relay.go:172-245`), so a public user org gets `<org>.realliance.page`, with `<org>/pages` as its root site. Takedown stays Owner-only. No reserved-name list: `realliance.page` is its own domain, and user sites carry the same exposure today | Claude's call |
| Team sync | Both group-to-team paths name only `realliance` teams: the login source's group-team map (`forgejo-release.yaml:193-194`) and forge-bot's `teams.yaml`, whose plan touches listed teams only (`teams.go:15, 42-44`). Neither reaches a user org | Fact; no change |
| Leavers | The `docs/forgejo.md` section Removing someone gets a step: list the orgs where the person is the only Owner. Leave each org untouched, so its repos keep serving, and add it to `ci.yaml` `deny` if it still runs CI. Take its Pages sites down only on request or abuse. Hand the org to a remaining member only when that member asks and Chris agrees | Claude's call |
| Many orgs per user | No per-user cap. The global `tiny` and paid caps bound the cost | Claude's call |

**Flips.**
- **Tests:**
  - realliance/sso `tests/forge/test_forge_signin.py::test_member_cannot_create_org` (`:49-53`) becomes `test_member_can_create_org`: a 201, after which the test deletes the org;
  - the ci-dispatch and forge-bot tests in the two subsections.
- **Docs:**
  - `docs/forgejo.md`: the Owners paragraph and the org-creation rule (`:74-77`), the new removal step, and what `owner_needs_action` covers now;
  - the "orgs are code" comment in `forgejo-release.yaml`;
  - `docs/ci.md`: unlisted orgs run on `tiny` only, and the new alert's row;
  - the `realliance-forge` `ci.yaml` comments and README;
  - the README of forge-bot and the README of ci-dispatch;
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

### Forgejo differences

- Reader-can-comment: on Forgejo, a token with issues or pull-requests read can open issues and
  comment on unlocked ones, unlike GitHub. A job that declares `issues: read` can comment. A
  candidate later patch: Actions tokens need issues or pull-requests write to create issues or
  comments.
- AGit push-to-open-PR (`refs/for/`): the code level gates it, and it fails today (proc-receive
  fails for the Actions user). A pin test refuses it; an upstream fix breaks the test and reopens
  the decision.
- API pull request creation uses the `forgejo-actions` user's own rights.
- Git over http on another repo still uses that user's own rights, including write on a wiki open
  to every signed-in user (see "Other repos: Unchanged").
- Remote reusable workflows are not read; they take the caller's result, so one gets the caller's
  `packages: write`.
- A private repo under `{}` needs `contents: read` to clone.

### Packages

- Under `permissive`, package reads are owner-wide for private packages. The orgs are public today; it
  matters for hidden orgs, team-scoped members and private users with collaborators.
- Forgejo judges protection at publish time. Tags that predate a rule, pull-mirror and migration tags
  (which skip pre-receive), release events that re-run an Owner-chosen commit with writer metadata,
  and site admins all get past it.
- Image tags are not bound to the git ref.
- `/v2/_catalog` and the API v1 package reads keep the user's own rights. Conan with a task JWT
  returns 500.
- A write to another private owner gets 401 at the group read gate and no rule-owner warning. That
  doesn't reveal the owner exists.
- `task.OwnerID` goes stale across a repo transfer.
- Each package request reloads the task, which costs extra queries.

### Test coverage

- The permissions matrix runs on SQLite only; the declarative cases run on every database.

## Left to the builder

- File and function names, the scanner for A, and the cache's size and eviction.
- The Job token settings page: its template, locale strings and flash messages.
- How to match protected tags: reuse `matchString`, private today, through a small exported helper,
  or `GetProtectedTags` plus a glob.
- Whether a schedule run's `CommitSHA` is the commit that registered its schedule. The git read
  relies on it.
- Whether a failed git read shows a run warning as well as the log line.
- What today's forge-bot does with a rejected `forge-bot.yaml`, before the forge-bot deploy order
  relies on it.
- The `ops-ci` rotation runbook, and the threshold for `CIDispatchOrgListStale`.
- Patch subjects, commit messages, `Upstream:` trailers, and the fork's README and WISHLIST rows.
- The luma-homeops doc changes the Rollout lists, and a `docs/forgejo.md` section on job token
  permissions.
