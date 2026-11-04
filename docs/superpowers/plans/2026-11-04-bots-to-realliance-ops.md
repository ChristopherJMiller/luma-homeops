# Bots to `realliance-ops`: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to run this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `realliance/forge-bot` and `realliance/ci-dispatch` move to the Owners-only org `realliance-ops`, so that only Owners can push a branch that runs on the `galaxy` runner. Each repo keeps its id, history, runner registration, and protections. forge-bot's reviewed `v0.1.0` image keeps its digest at its new path.

**Why (Chris, 2026-11-04, runner plan ruling F30):** `realliance` members (2 people, write) and maintainers reach every `realliance` repo through all-repos teams. A branch they push to either bot repo runs on `galaxy`, where the job can read both connection tokens, the org secret `REGISTRY_TOKEN` (push any `realliance` package), and the Go build cache behind forge-bot's release images. Both bots hold site-administrator forge tokens.

**Approach:** Forgejo's transfer API moves each repo in place. The inventory follows by refresh, with no state surgery. A copy by digest moves the image, and a new packages-only identity in `realliance-ops` pushes later images.

## Global Constraints

### Facts (read-only, 2026-11-04)

| What | Value | Source |
|---|---|---|
| Forge | Forgejo `15.0.11+gitea-1.22.0` | `/api/v1/version`; source at `~/Repos/realliance-forgejo/upstream` (tag v15.0.11) |
| Repos | `realliance/forge-bot` id 94 (tag `v0.1.0`, PR #1 merged, 0 issues, 0 releases, 0 stars, 3 watchers); `realliance/ci-dispatch` id 240 (README only, no workflows) | `GET /repos/…` |
| Tag protections | forge-bot id 1, ci-dispatch id 5; both `v*` → `["Owners"]` | `GET …/tag_protections` |
| Runner rows | `galaxy-forge-bot` row 17 (`repo_id` 94), `galaxy-ci-dispatch` row 18 (`repo_id` 240); both `owner_id` 0, `idle` | `GET …/actions/runners` |
| Absent on both repos | webhooks, deploy keys, push mirrors, collaborators, repo secrets, repo variables; no org webhooks in either org | administrator API reads |
| Secrets per org | `realliance`: `REGISTRY_TOKEN` (forge-ci, `write:package`; canonical `FORGE_CI_PACKAGE_TOKEN`). `realliance-ops`: none | `GET /orgs/…/actions/secrets` |
| `realliance-ops` | `visibility: private`, teams `["Owners"]`, repos `["realliance-ops/forge"]`, no packages | `GET /orgs/realliance-ops…` |
| Package | `realliance/container/forge-bot`: `sha-9be4857` → `sha256:6b42aa63fc579f46e79df488a29846ef9b1c7defcd0f425fa40d5e6745218827` (OCI manifest, config `sha256:90e34b93…`), plus 2 SBOM tags; linked to repo 94 | `GET /packages/realliance` and the registry |
| Provider | `svalabs/forgejo` 1.7.0: repository `owner` has `RequiresReplaceIfConfigured` (`repository_resource.go:509`); `Read` loads the repo by numeric id (`getRepositoryByID`, `:1619`) and takes `owner` from the reply; branch protection `Read` loads the repo by id, then reads the rule by owner and name | provider source at tag v1.7.0 |
| Runner matching | a repo-scoped runner takes jobs `WHERE repo_id = runner.RepoID` (`models/actions/task.go:302`); `--scope` resolves to an id only at registration | Forgejo source |

### Decisions

- **D1. Transfer, never re-create.** Editing `owner` first plans a destroy and create, which `prevent_destroy` refuses. `POST /repos/{owner}/{repo}/transfer` `{"new_owner": "realliance-ops"}` as the break-glass administrator runs at once (`StartRepositoryTransfer`: a site administrator goes straight to `TransferOwnership`) and answers 202. A 201 means a pending transfer: reject it and STOP.
- **D2. No state surgery.** The transfer keeps the `repository` row and its id. The next refresh reads the new owner into state. Then `owner = forgejo_organization.realliance_ops.name` plans no replacement, and the addresses stay, so `tofu state mv` has nothing to move. The API hides whitelist team ids that belong to another org (`ToBranchProtection` filters by the owner's teams), so the plan shows one in-place update per repo, the branch protection's whitelists. The 2026-11-03 inventory move saw the same (commit `69265e5`). Fallback, only if a plan says `forces replacement`: STOP, then use the pattern of that commit (`removed { destroy = false }` plus `import`).
- **D3. Copy `v0.1.0` by digest; never rebuild it.** Forgejo 15 has no package transfer (the package API offers get, delete, files, link and unlink), and `LinkToRepository` refuses a package whose owner differs from the repo's. A ko rebuild isn't byte-identical in practice. The registry holds SBOM tags for two builds of commit `9be4857`. The `main` run (task 52) built `sha256:2cf16b32…` and the tag run (task 53) built `sha256:6b42aa63…`. Their SBOM files differ in the embedded module version (`v0.0.0-20261104015512-9be4857b47a0` against `v0.1.0`), because Go stamps the version from the checked-out tag. `crane copy` writes the source manifest bytes and blobs unchanged, so the digest Chris reviewed is the digest at the new path.
- **D4. A new pusher, `ops-ci`, in a new team `realliance-ops/ci`** (`repo.packages` write, `includes_all_repositories = false`, no repos). forge-ci can't serve: Forgejo 15 tokens can't narrow to one org (forge-bot plan B0, outcome B), and `realliance` jobs read forge-ci tokens (the org `REGISTRY_TOKEN`, and `SITE_REGISTRY_TOKEN` on `realliance/realliance.net`, where members push). Package rights come from org teams alone (`DeterminePackageAccessMode`), and the Actions job token gets none (`TODO: ActionUser permission check` in the source). Team `ci` reaches no repo, so only Owners read or write code in `realliance-ops`.
- **D5. `realliance-ops` becomes `visibility = "public"` (Chris decides at Task 0).** A private org hides its public repos and their packages from everyone outside it (`GetUserRepoPermission`, "Prevent strangers"; `DeterminePackageAccessMode`). Anonymous reads that depend on these repos today: T16's image pull (no `imagePullSecrets`), sso `anonymous_manifest` and raw-file reads, the NetworkPolicy plan's runner-idle `curl`, Renovate's tag reads and the Pages drill. The inventory stays `private = true`, and the org name is public already (CLAUDE.md). If Chris keeps the org private, T16 needs an image pull secret built from a `read:package` token for `ops-ci` (another token, his approval), those checks need tokens, the Pages drill moves, and both bots stop being public code.
- **D6. Module paths.** ci-dispatch has no code yet: the runner plan's Task 6 starts at `git.realliance.net/realliance-ops/ci-dispatch`. forge-bot keeps `git.realliance.net/realliance/forge-bot` through `v0.1.x`, since a new path changes the binary and so the reviewed digest. The old repo path redirects (API 301) until someone creates a repo of that name in `realliance`, which members can, so Tasks 5 to 7 move every reference.
- **D7. No token rotation and no cache wipe**, unless Task 1's audit finds a run that no Owner, `forgejo-admin` or drill started. Only runs on `galaxy` could read the connection tokens and the Go cache.
- **D8. Retire the `realliance` org `REGISTRY_TOKEN` once the move holds (Task 8).** Its consumers are forge-bot's `ci.yaml` (moving) and ci-dispatch by inheritance (moving). `realliance.net` uses its own secret. Task 1 confirms that no other `realliance` repo has a workflow. The forge-ci token behind it stays valid: the token routes accept only password Basic auth (`ReqBasicOrRevProxyAuth`), and this forge turns that off.

### How changes land

- **Inventory** (`~/Repos/realliance-forge`): a branch, a signed commit, `fj -H git.realliance.net pr create`; **Chris** merges fast-forward. Apply from the laptop with the README's env, only inside a STOP. B2 has no state lock: one operator applies at a time, so first check that no other plan has an apply open (runner plan Task 15).
- **forge-bot** (`~/Repos/forge-bot`, `realliance-ops/forge-bot` after Task 4): a signed branch, a PR, **Chris** merges fast-forward.
- **`realliance/sso`**: worktree `~/Repos/realliance-sso-ops`, branch `bots-ops` from `origin/main`, `git commit -S`, `gh pr create --fill`; merge only when Chris says merge.
- **`realliance/sso-secrets`**: `git commit -S`, push `main`.
- **luma-homeops**: worktree `~/Repos/luma-homeops-ops`, branch `bots-ops` from `origin/main`. This plan pushes docs, ledgers and manifest comments to `main`. A comment changes nothing Argo renders: check that `kustomize build` output stays the same. T16 changes go to branch `forge-bot-deploy` (worktree `~/Repos/luma-homeops-fb`), never to `main`.
- Sign every commit (`nix develop --command git commit -S …` in luma-homeops) and end it with:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01FWhaDeswMqYvQaVdpL8oit`
- Shells that sign or use `ssh` or `gpg` export `DISPLAY=:10.0 XAUTHORITY=$HOME/.Xauthority`. Never `--no-verify` or `--no-gpg-sign` (S6).
- **Secrets:** never `Read`, `cat` or diff `forge/secrets.env` or a `*.secret.yaml`. Tokens travel through pipes, stdin and `umask 077` files, never argv, echo, logs, PR text or the ledger. Shell reads: `fapi() { curl -sS -H @- "https://git.realliance.net/api/v1$1" <<<"Authorization: token $FORGEJO_ADMIN_TOKEN"; }` after `set -a; . ~/Repos/realliance-sso-secrets/forge/secrets.env; set +a`, all inside `( … )`. Writes go through the sso helper `forge_api` (`cd ~/Repos/realliance-sso/tests && nix develop --command python -c …`).
- No hidden org, team or group names anywhere. Naming `realliance-ops` is fine.
- **STOP—ask Chris** is one AskUserQuestion with the change, what to watch and the rollback trigger. Options: `approve: do it, and roll back at once if a trigger fires` and `hold`. A rejected or interrupted call may already have run: read live state before redoing anything.
- **Ledger:** `docs/superpowers/plans/2026-11-04-bots-to-realliance-ops.ledger.md`: answers, live reads, plan counts, digests, timestamps. No secrets.

### Coordination

- **forge-bot plan:** this plan finishes (Task 6) before its Task 16 deploy STOP, which waits for NetworkPolicy Task 11 anyway. Task 6 edits branch `forge-bot-deploy` and sso PR #41.
- **Runner classes plan:** its Task 6 (first push to ci-dispatch) waits for this plan's Task 5. Its values change in Task 6 here.
- **NetworkPolicy plan:** its runner-idle checks name `/repos/realliance/{forge-bot,ci-dispatch}/actions/tasks`. After the move the old path answers 301, and `curl` without `-L` prints no count. Task 6 adds the new paths to its ledger.
- Serialize STOPs with the other plans, and run Tasks 3 and 4 only while no plan holds a CI-hold window on `galaxy`.

## Tasks

### Task 0: start

- [ ] **Step 1:** One AskUserQuestion. Chris answers the open questions (D5 visibility; the username `ops-ci`; D8, the forge-ci token's future). He approves the routine work: ledger and docs pushes, the sso branch, the image copy and package links in Task 5. He holds pushes and PRs to both bot repos from Task 1 to Task 5.
- [ ] **Step 2:** Create the luma-homeops and sso worktrees and the ledger, recording the answers.

**Expected outputs:** answers in the ledger; worktrees on `bots-ops`.

### Task 1: pre-flight (read-only)

- [ ] **Step 1: Queues idle.** For both repos, `fapi "/repos/realliance/<r>/actions/tasks?limit=50"` filtered to `running` or `waiting`, and `fapi "/admin/actions/runners/jobs?labels=galaxy"`.
- [ ] **Step 2: Live state equals the Facts table.** Re-read every row, and confirm that `realliance-ops/forge-bot` and `realliance-ops/ci-dispatch` return 404.
- [ ] **Step 3: Workflow consumers.** For each repo in `fapi /orgs/realliance/repos`, list `.forgejo/workflows` and look for `REGISTRY_TOKEN`.
- [ ] **Step 4: Exposure audit (D7).** Read both repos' `activities/feeds`, every page, and group by `act_user` and `op_type`. Attribute every entry without a user (expected: the Pages drill's Actions-token push to `pages`).
- [ ] **Step 5: Drift and deploy state.** `tofu plan` in `~/Repos/realliance-forge`; `kubectl get ns forge-bot` and the Argo app list; `fapi /repos/realliance/ci-dispatch/contents/.forgejo` (404).

**Expected outputs:** 0 jobs `running` or `waiting` and `[]` from the administrator listing; every Facts row as stated; only forge-bot's `ci.yaml` names `REGISTRY_TOKEN`; actors ⊆ {`chris`, `forgejo-admin`} plus attributed drill pushes; `No changes.`; no `forge-bot` namespace or app. Any other result is a STOP (for the audit, propose rotating both connection tokens and clearing the `galaxy` PVC, an S4 delete).

### Task 2: registry identity and visibility (inventory PR A)

**Files:** inventory `teams.tf`, `orgs.tf`, `README.md`; sso `tests/forge_helpers.py`, `tests/forge/test_forge_{inventory,ci,ci_dispatch,pages,runner_general,runner_proofs}.py`; sso-secrets `forge/secrets.env` (+ `OPS_CI_PACKAGE_TOKEN`).

- [ ] **Step 1: Tests first** (sso, branch `bots-ops`):
  - `forge_helpers.py`: `FORGE_BOT_REPO = "realliance-ops/forge-bot"`, `CI_DISPATCH_REPO = "realliance-ops/ci-dispatch"` (moved out of the test file).
  - `test_ops_org_shape` (replaces `test_ops_org_owners_only`): `visibility == "public"`; teams `{"Owners", "ci"}`; `ci` has `repo.packages: write` and no other unit, `includes_all_repositories` false and no repos; `team_members("realliance-ops", "ci") == {"ops-ci"}`; the Owners bound as before; an anonymous `GET /api/v1/repos/realliance-ops/forge` answers 404.
  - `test_ops_ci_account`: `source_id` 0, not an administrator.
  - `test_ops_registry_token_scope`: `OPS_CI_PACKAGE_TOKEN` pushes `realliance-ops/ci-probe-<hex>` (201), and the registry refuses it at `realliance/ci-probe-<hex>`; the registry also refuses forge-ci's token at `realliance-ops/ci-probe-<hex>`; cleanup deletes the probes. `test_package_token_scope` probes `realliance/ci-probe-<hex>`, never `realliance/forge-bot`.
  - `test_members_cannot_push_bots` (replaces `test_member_cannot_push_forge_bot_main`), for both repos with a throwaway member (`_member(forge_user)`): `collaborators/<u>/permission` is `read`; as that member (`sudo`), `POST /repos/<r>/branches` and a contents write to `main` both fail (403 or 404).
  - `test_ci_dispatch_and_forge_bot_write_access_equal` also asserts `== ({"Owners"}, set())`; the registry-secret test reads `/orgs/realliance-ops/actions/secrets`.
  - `DRILL_REPO`, `BOT_REPO` and the proofs' repo names use the constants.
  - Run `nix develop --command pytest tests/forge -v -k "ops or bots or ci_dispatch or forge_bot"`. Expected: the new and moved tests FAIL (404 at the new paths, no team `ci`, no `ops-ci`).
- [ ] **Step 2: Inventory PR A** (branch `bots-ops-identity`): `forgejo_team.ops_ci` shaped like `realliance_ci`, `forgejo_team_member.ops_ci` (`ops-ci`), `realliance_ops` `visibility = "public"` and description "Owners-only: the forge inventory and the forge's bots"; comments and README say "Owners, plus team `ci`: packages only, no repos". `tofu plan`. Expected: `2 to add, 1 to change, 0 to destroy`.
- [ ] **Step 3: STOP—ask Chris** (creates an account and mints a token). After his OK and his merge of PR A:
  - `forgejo admin user list` (S1), then `kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin user create --username ops-ci --email ops-ci@realliance.net --random-password --must-change-password=false >/dev/null`;
  - `tofu apply`. Expected: `2 added, 1 changed, 0 destroyed`, then `No changes.`;
  - in `~/Repos/realliance-sso-secrets`: `kubectl … forgejo admin user generate-access-token --username ops-ci --token-name ops-registry --scopes write:package --raw | { read -r t; printf 'OPS_CI_PACKAGE_TOKEN=%s\n' "$t" >> forge/secrets.env; }`, then commit signed and push;
  - `forge_api("PUT", "/api/v1/orgs/realliance-ops/actions/secrets/REGISTRY_TOKEN", json={"data": <token read from the env file in Python>})`. Expected: 201 or 204.
- [ ] **Step 4:** Run `test_ops_org_shape`, `test_ops_ci_account` and `test_ops_registry_token_scope`. Expected: PASS.

**Expected outputs:** team `realliance-ops/ci` with member `ops-ci`; org `realliance-ops` public; inventory still 404 to anonymous; `REGISTRY_TOKEN` in `realliance-ops` org secrets; plan clean.

### Task 3: transfer `ci-dispatch` (inventory PR B1)

ci-dispatch goes first: it holds a README and no CI, so it rehearses every step at no cost.

- [ ] **Step 1: Prepare PR B1** (branch `ci-dispatch-ops`): `owner = forgejo_organization.realliance_ops.name` in `forgejo_repository.ci_dispatch`, and the tag-protection comment's path. Open it; nobody merges or applies it before the transfer.
- [ ] **Step 2: Re-run Task 1 Step 1 for ci-dispatch** and check that `realliance-ops/ci-dispatch` still returns 404.
- [ ] **Step 3: STOP—ask Chris (transfer `realliance/ci-dispatch`).** Watch: the 202, the runner row, the plan count. Rollback trigger: anything but 202, or a plan other than `0 to add, 1 to change, 0 to destroy`. Then:
  - `forge_api("POST", "/api/v1/repos/realliance/ci-dispatch/transfer", json={"new_owner": "realliance-ops"})`. Expected: 202.
  - `forge_api("PATCH", "/api/v1/repos/realliance-ops/ci-dispatch/tag_protections/5", json={"whitelist_teams": ["Owners"]})`. Expected: 200.
  - `tofu plan -refresh-only`. Expected: drift on `forgejo_repository.ci_dispatch` (owner, full name, URLs) and on `forgejo_branch_protection.ci_dispatch_main` (whitelists empty), nothing else.
  - Chris merges B1. `tofu plan`. Expected: `0 to add, 1 to change, 0 to destroy`, the whitelists only, and no `forces replacement`. Apply, then `No changes.`
- [ ] **Step 4:** `git -C ~/Repos/ci-dispatch remote set-url origin https://git.realliance.net/realliance-ops/ci-dispatch.git && git -C ~/Repos/ci-dispatch fetch`.
- [ ] **Step 5: Verify what moved** (one ledger line per row):

| Item | Verify | If wrong |
|---|---|---|
| Code, tags, issues, PRs, releases, wiki, stars, watchers | counts and tag list as in Task 1 | rollback |
| Team access | `GET …/teams` → Owners only; collaborators `[]` | rollback |
| Branch protection | `merge_whitelist_teams` and `approvals_whitelist_teams` `["Owners"]` | the Step 3 apply |
| Tag protection | `[("v*", ["Owners"])]` | the Step 3 PATCH |
| Webhooks, deploy keys, mirrors, repo secrets | `[]` (none existed) | none needed |
| The org's secret | a job sees `realliance-ops` secrets (`FetchActionSecrets` uses the repo's owner) | Task 2 |
| Runner | row 18 at `GET /repos/realliance-ops/ci-dispatch/actions/runners`, `repo_id` 240, `idle` | rollback |
| Old path | `GET /api/v1/repos/realliance/ci-dispatch` → 301 to the new path | none needed |

- [ ] **Step 6:** Run the ci-dispatch tests in `tests/forge/test_forge_ci_dispatch.py`. Expected: PASS.

**Expected outputs:** `realliance-ops/ci-dispatch`, id 240, public, Owners-only; plan clean; runner row 18 in place.

### Task 4: transfer `forge-bot` (inventory PR B2)

- [ ] **Step 1: Prepare PR B2** (branch `forge-bot-ops`): `forgejo_repository.forge_bot` owner as in B1, and its comment's tag-protection path.
- [ ] **Step 2: Re-run Task 1 Steps 1 and 2 for forge-bot.**
- [ ] **Step 3: STOP—ask Chris (transfer `realliance/forge-bot`).** Same commands, watch list and triggers as Task 3 Step 3, with `forge-bot`, tag protection id 1, `forgejo_branch_protection.forge_bot_main` and PR B2.
- [ ] **Step 4:** Point `~/Repos/forge-bot` at `https://git.realliance.net/realliance-ops/forge-bot.git` and fetch.
- [ ] **Step 5:** The verification table from Task 3 Step 5, with row 17 and `repo_id` 94. Also: the old package still exists (no package moves) and still links to repo 94.

**Expected outputs:** `realliance-ops/forge-bot`, id 94, public, Owners-only; plan `No changes.`; runner row 17 in place.

### Task 5: the image, the package link and CI

- [ ] **Step 1: Copy `v0.1.0` (D3):**

```bash
umask 077; export DOCKER_CONFIG=$(mktemp -d); trap 'rm -rf "$DOCKER_CONFIG"' EXIT
grep '^OPS_CI_PACKAGE_TOKEN=' ~/Repos/realliance-sso-secrets/forge/secrets.env | cut -d= -f2- |
  nix shell nixpkgs#crane -c crane auth login git.realliance.net -u ops-ci --password-stdin
d=6b42aa63fc579f46e79df488a29846ef9b1c7defcd0f425fa40d5e6745218827
nix shell nixpkgs#crane -c crane copy "git.realliance.net/realliance/forge-bot@sha256:$d" git.realliance.net/realliance-ops/forge-bot:sha-9be4857
nix shell nixpkgs#crane -c crane copy "git.realliance.net/realliance/forge-bot:sha256-$d.sbom" "git.realliance.net/realliance-ops/forge-bot:sha256-$d.sbom"
```

- [ ] **Step 2: Same bytes.** Take an anonymous pull token for `repository:realliance-ops/forge-bot:pull`, then `curl -sSL` the `sha-9be4857` manifest (OCI `Accept`) and `sha256sum` the body; blobs serve straight from B2, which drops `Docker-Content-Digest`. Expected: `6b42aa63…8827`, config `sha256:90e34b93…`, version creator `ops-ci`.
- [ ] **Step 3: Links.** `forge_api("POST", "/api/v1/packages/realliance-ops/container/forge-bot/-/link/forge-bot")` → 201; `forge_api("POST", "/api/v1/packages/realliance/container/forge-bot/-/unlink")` → 204. The old package stays for rollback (follow-up 2).
- [ ] **Step 4: forge-bot PR** (branch `ops-registry`): in `.forgejo/workflows/ci.yaml`, `KO_DOCKER_REPO: git.realliance.net/realliance-ops/forge-bot`, `ko login … -u ops-ci`, and the risk comment names `realliance-ops` Owners. The README's build line and CI account follow. Expected: the PR's `test` job succeeds on `galaxy-forge-bot` within 10 minutes, which proves runner row 17 serves the moved repo.
- [ ] **Step 5: Chris merges** fast-forward (`fj -H git.realliance.net pr merge realliance-ops/forge-bot#<n> -d`). Expected: the `main` run's `test` and `image` jobs succeed, and `realliance-ops/forge-bot:sha-<7>` appears, created by `ops-ci`. Nothing pins it: no review covers it.

**Expected outputs:** `realliance-ops/forge-bot:sha-9be4857` at digest `6b42aa63…8827`, linked to `realliance-ops/forge-bot`; CI green on `galaxy` with the new secret.

### Task 6: re-pin T16 and update the other plans (branches and docs, no deploy)

- [ ] **Step 1: T16 branch** (`~/Repos/luma-homeops-fb`, `forge-bot-deploy`): image `git.realliance.net/realliance-ops/forge-bot:sha-9be4857@sha256:6b42aa63fc579f46e79df488a29846ef9b1c7defcd0f425fa40d5e6745218827` in `cluster/forge-bot/statefulset.yaml`; the path in its comments and in `kustomization.yaml`; the image pattern in `scripts/test-forge-bot-alerts.sh` and `scripts/sync-forge-bot-alerts.sh` (then re-run the sync, which rewrites the `prometheusrule.yaml` header); `IMAGE` in `scripts/test_forge_bot_renovate.py`; `matchPackageNames` in `.renovaterc.json`; the path in `docs/forgejo.md`. Run `nix develop --command scripts/test-forge-bot-alerts.sh`, the Renovate guard and `kustomize build cluster/forge-bot`. Signed commit, push the branch only.
- [ ] **Step 2: sso PR #41** (`~/Repos/realliance-sso-fb`): the `IMAGE` pattern and the `anonymous_manifest` path in `tests/forge/test_forge_bot.py`; rebase onto `bots-ops` once it merges.
- [ ] **Step 3: Plans** (luma-homeops `bots-ops`): the forge-bot plan gains a dated note under its header ("since 2026-11-04 the repo and image live at `realliance-ops/forge-bot`; the Go module path stays"), and its image value and Tasks 16 to 19 use the new path. The runner classes plan's repo, Go module, image and `fj` merge path move to `realliance-ops`. The NetworkPolicy ledger gets a cross-plan note with the new runner-idle paths. Each plan's ledger gets one line.

**Expected outputs:** `scripts/test-forge-bot-alerts.sh` SUCCESS; Renovate guard PASS; rendered `forge-bot` manifests differ from before only in the image line; plans and ledgers updated.

### Task 7: docs, CLAUDE.md, tests merged

- [ ] **Step 1: `docs/forgejo.md`:** "Where things live" names both bots in `realliance-ops`. "Gate before the first hidden org" states `realliance-ops` = Owners plus team `ci` (packages only), so `realliance` members can't push either bot. The GitHub mirror note adds that the one-to-one mapping puts forge-bot's and ci-dispatch's mirrors in the `realliance-ops` GitHub org, public there, beside the private inventory mirror. Neither bot has a mirror today. Runner re-registrations use `--scope realliance-ops/<repo>`.
- [ ] **Step 2: CLAUDE.md**, Realliance forge row: "…OpenTofu in `git.realliance.net/realliance-ops/forge` (Owners-only org, also home of the bots `forge-bot` and `ci-dispatch`)".
- [ ] **Step 3: Manifest comments:** `cluster/forgejo-runner/kustomization.yaml` and `cluster/forgejo-runner-general/deployment.yaml` name the new paths. Check that `kustomize build` output stays the same.
- [ ] **Step 4:** Lint every changed Markdown file with `nix shell nixpkgs#vale -c vale --config ~/.config/vale/.vale.ini --no-wrap --output=line <files>` (expected: no output), commit signed, push `bots-ops` to `main`. Open the sso PR (`gh pr create --fill`); merge on Chris's word.

**Expected outputs:** docs, CLAUDE.md and comments name `realliance-ops`; vale clean; renders unchanged; sso PR open.

### Task 8: verification and retiring the old secret

- [ ] **Step 1: Members can't push.** Run `nix develop --command pytest tests/forge -v` alone (not while the pages suite runs). Expected: all PASS, among them `test_members_cannot_push_bots`, the writers test (`({"Owners"}, set())` on both repos), `test_galaxy_serves_forge_bot_and_ci_dispatch_only` and `test_ops_registry_token_scope`.
- [ ] **Step 2: CI on `galaxy`.** Task 5's PR and `main` runs succeeded on row 17; row 18 is `idle` at its new path.
- [ ] **Step 3: Inventory.** `tofu plan`. Expected: `No changes.`
- [ ] **Step 4: STOP—ask Chris (delete the `realliance` org secret, D8).** `forge_api("DELETE", "/api/v1/orgs/realliance/actions/secrets/REGISTRY_TOKEN")` → 204; the sso registry test then asserts its absence. Rollback after this step needs a new secret.

**Expected outputs:** suite green; plan clean; `realliance` org secrets `[]`.

## Rollback (a transfer back)

Triggers: a transfer answers anything but 202; a plan shows `forces replacement` or any count but the expected one; the runner doesn't take the moved repo's PR job within 10 minutes; a verification row has no in-place fix.

1. `fapi /repos/realliance/<repo>`: a 301 is the redirect. A 200 with another id means someone took the name: STOP.
2. `forge_api("POST", "/api/v1/repos/realliance-ops/<repo>/transfer", json={"new_owner": "realliance"})` → 202. The `realliance` all-repos teams come back on their own, members included, which is the pre-plan state.
3. Revert PR B1 or B2; `tofu plan` → `0 to add, 1 to change, 0 to destroy`; apply.
4. PATCH the tag protection's whitelist again (id 1 or 5).
5. forge-bot: revert the CI PR, re-link the old package (`…/packages/realliance/container/forge-bot/-/link/forge-bot`), and point T16's pin back at the old path (the digest stays the same).
6. Before Task 8 the `realliance` org secret still exists; after it, Chris sets one again.

## Follow-ups

1. forge-bot's Go module path moves to `git.realliance.net/realliance-ops/forge-bot` in its next reviewed release (D6).
2. Delete the old `realliance/forge-bot` package versions after T16 has run a week (STOP).
3. The runner config's connection comment names `realliance/forge-bot`: fix it at the next reseal.
4. Revoking forge-ci's `FORGE_CI_PACKAGE_TOKEN` needs a path Forgejo lacks (D8): an item for the fork's `WISHLIST.md`, or Chris accepts it.
5. `docs/ci.md`'s expected local accounts (runner plan D19, Task 20) gain `ops-ci`.
