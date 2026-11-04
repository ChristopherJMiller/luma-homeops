# Bots to `realliance-ops`: ledger

Plan: `docs/superpowers/plans/2026-11-04-bots-to-realliance-ops.md`. Answers, live reads, plan counts, digests and timestamps (UTC). No secrets.

## Task 0: answers (Chris, 2026-11-04)

- D5: `realliance-ops` becomes **public**. The inventory `realliance-ops/forge` stays private.
- The registry account is `ops-ci`, a local account in a new team `realliance-ops/ci` (packages only, no repos). Its `write:package` token is the `realliance-ops` org secret `REGISTRY_TOKEN`.
- D8: the `realliance` org `REGISTRY_TOKEN` goes in Task 8. The old forge-ci token behind it gets a guarded database delete (one transaction that asserts exactly 1 row), then a check that the forge rejects it.
- Defaults: forge-bot keeps its Go module path until its next reviewed release; ci-dispatch starts on the new path; the old `realliance/forge-bot` package goes once T16 has run for a week.
- The transfers (Tasks 3 and 4) and Task 8 Step 4 stay STOPs. "Merge as needed" covers the inventory and CI PRs.

## Task 1: pre-flight (read-only)

- Queues: `realliance/forge-bot` 62 tasks (41 `success`, 21 `skipped`), none `running` or `waiting`; `realliance/ci-dispatch` 0 tasks; the administrator `galaxy` job listing returned `null`.
- Facts table: every row matched. Forgejo `15.0.11+gitea-1.22.0`. Runner rows 17 `galaxy-forge-bot` (`repo_id` 94) and 18 `galaxy-ci-dispatch` (`repo_id` 240), both `owner_id` 0 and `idle`. Package `realliance/container/forge-bot`: `sha-9be4857` plus 2 SBOM tags, created by `forge-ci`. An anonymous pull of the `sha-9be4857` manifest hashed to `sha256:6b42aa63…8827` (OCI), config `sha256:90e34b93…`.
- Workflow consumers: only forge-bot's `.forgejo/workflows/ci.yaml` names `REGISTRY_TOKEN`. `realliance.net` has GitHub-style workflows that use `GITHUB_TOKEN` only.
- Exposure audit (D7): **clean**. forge-bot's 77 feed entries are `chris` (27), `forgejo-admin` (48) and 2 Actions-user entries, which are the Pages drill's push to `refs/heads/pages` at 03:07:30Z. All 43 runs came from `chris` or `forgejo-admin`. ci-dispatch has 1 entry and 0 runs. No rotation and no PVC wipe.
- Drift: `tofu plan` gave `No changes.` No `forge-bot` namespace and no forge-bot Argo app.

## Task 2: registry identity and visibility

- sso branch `bots-ops` (`0afe2f4`): the constants move to `realliance-ops`, plus `test_ops_org_shape`, `test_ops_ci_account`, `test_ops_registry_token_scope` and `test_members_cannot_push_bots`. Red run: 10 failed, 5 passed.
- Inventory PR A, `realliance-ops/forge` #12 (`27dc657`, fast-forward): plan `2 to add, 1 to change, 0 to destroy`, applied as `2 added, 1 changed, 0 destroyed`, then `No changes.`
- `ops-ci` is user id 502 (local, not an administrator, `source_id` 0; its password went to `/dev/null`). Team `realliance-ops/ci` is id 50. The org is public, and the inventory still answers 404 anonymously.
- The token went to sso-secrets `2792155` (`OPS_CI_PACKAGE_TOKEN`) and to the `realliance-ops` org secret `REGISTRY_TOKEN` (201).
- Green: `test_ops_org_shape`, `test_ops_ci_account` and `test_ops_registry_token_scope`.

## Task 3: `ci-dispatch`

- Inventory PR B1, #13 (`da87a27`).
- Transfer: `POST /repos/realliance/ci-dispatch/transfer` answered **202** at 21:27:18Z. Tag protection 5 → `v*`/`["Owners"]` (200).
- The refresh-only plan showed the expected drift. The B1 plan was `0 to add, 1 to change, 0 to destroy` (whitelists only), applied at 21:28:11Z, then `No changes.`
- Clone `~/Repos/ci-dispatch` re-pointed; `origin/main` `6cf9bab`.

## Task 4: `forge-bot`

- Inventory PR B2, #14 (`33265fb`). The first guarded run (21:30:21Z) refused, because its queue check read ci-dispatch's old path (301). The re-run went ahead.
- Transfer: **202** at 21:30:35Z. Tag protection 1 fixed (200).
- B2 plan `0/1/0`, applied at 21:31:13Z, then `No changes.`
- Clone `~/Repos/forge-bot` re-pointed; `origin/main` `9be4857`, tag `v0.1.0`.
- Verification: both repos keep their ids (94, 240) and are public and Owners-only. Old paths answer 301. Runner rows 17 and 18 didn't change. Probe tasks 79 and 80 were green on `galaxy`, and task 81 saw the `realliance-ops` `REGISTRY_TOKEN` (length only). `test_members_cannot_push_bots` passed, and the selection gave 15 passed.
- Ruling: hold pushes and merges to forge-bot `main` until Task 5's CI PR lands.

## Task 5: image, package link, CI

- `crane copy` as `ops-ci` (token on stdin, `DOCKER_CONFIG` in a `umask 077` temp dir) of `realliance/forge-bot@sha256:6b42aa63…8827` → `realliance-ops/forge-bot:sha-9be4857` (21:36:45Z), plus SBOM tag `sha256-6b42aa63…8827.sbom` (21:36:46Z). The registry mounted the blobs; nothing re-uploaded them.
- **Same bytes:** an anonymous pull token for `repository:realliance-ops/forge-bot:pull`, then a GET of the `sha-9be4857` manifest (307 to B2, `application/vnd.oci.image.manifest.v1+json`), whose body hashes to `6b42aa63fc579f46e79df488a29846ef9b1c7defcd0f425fa40d5e6745218827`. Config `sha256:90e34b93…2626`. The old path hashes the same. `ops-ci` created both versions.
- Links: `POST /packages/realliance-ops/container/forge-bot/-/link/forge-bot` returned 201, and `POST /packages/realliance/container/forge-bot/-/unlink` returned 204. The new package's versions show repo 94. The old package keeps its 3 versions, unlinked, for rollback.
- forge-bot PR #2 (branch `ops-registry`, signed `26594e0`): `KO_DOCKER_REPO: git.realliance.net/realliance-ops/forge-bot`, `ko login … -u ops-ci`, and the risk comment names the `realliance-ops` Owners. The README's build line, CI account and module-path note follow. The PR's `test` job (task 82) succeeded in 19 s.
- Merged with `fj pr merge realliance-ops/forge-bot#2 -d`, a fast-forward: `main` = `26594e0`. The `main` run passed `test` (task 84) and `image` (task 85, 21:39:08–21:39:14Z). The run pushed `realliance-ops/forge-bot:sha-26594e0` plus its SBOM, created by `ops-ci` and linked to repo 94. Nothing pins it. The hold on forge-bot `main` ends here.

## Task 6: T16 re-pin and the other plans

- luma-homeops branch `forge-bot-deploy` `a4098739` (pushed as a branch only): the image is `git.realliance.net/realliance-ops/forge-bot:sha-9be4857@sha256:6b42aa63…8827`. The commit also moves the comments, the alert sync and test patterns, the Renovate rule and its guard, and `docs/forgejo.md`, and re-syncs the alert header. Results: `test-forge-bot-alerts.sh` SUCCESS, the Renovate guard 5 passed, and the rendered `forge-bot` manifests differ only in the image line. The NetworkPolicy names no path.
- sso PR #41: `IMAGE` → `realliance-ops`, and `anonymous_manifest(FORGE_BOT_REPO, …)` (`ede4805`). After #48 merged, the branch moved onto `main` by rebase and went up as `a216569`. Run against the T16 branch: 20 passed and 1 failed, `test_pinned_image_is_the_built_release`.
- **Finding (the transfer):** Forgejo keeps `action_run.owner_id` on transfer. forge-bot's 43 runs from before the move still carry the `realliance` owner (52), and the 7 since then carry `realliance-ops` (208). `/repos/realliance-ops/forge-bot/actions/runs` lists only the 7, while `/actions/tasks` selects by repo id and lists all 85 tasks, and `runs_for` can't see v0.1.0's build. `a216569` makes that test read the tasks (all pages, job `image`, event `push`), and it now passes. `test_main_push_builds_and_galaxy_runs_pin` fails until T16 deploys, as expected.
- An interrupted test run (a 600 s `timeout`, killed inside `test_allowlist_jobs_wait`) left the scratch repo `realliance/ci-probe-a60da525` (id 252, with a waiting `galaxy` job) and the forge-bot branch `ci-probe-7ba213ed`. Task 6 deleted both by hand, as the test's `finally` would have done (204, 204). The `galaxy` job listing is `null` again. Two `ubuntu-latest` probe jobs on repo 94 (110 and 112) wait for the 24 h abandoned-job cron, by the test's design.
- Plans (`c6e4dc2a`): the forge-bot plan gets a move note, its image value and Tasks 16 to 19 change, and the runner classes plan's repo, module, image, package link and `fj` path change. The NetworkPolicy ledger gets a cross-plan note with the new runner-idle paths.

## Task 7: docs and tests

- `docs/forgejo.md`, CLAUDE.md (the Realliance forge row), and the comments in `cluster/forgejo-runner/kustomization.yaml` and `cluster/forgejo-runner-general/deployment.yaml` name `realliance-ops`. Both runner renders are byte-identical to before. Vale is clean on the changed lines.
- sso PR #48 (`bots-ops`, `0afe2f4`) squash-merged as `aa14177`.

## The restore-drill fixture (Chris, 2026-11-04)

- Before: `realliance/restore-drill-fixture` id 16, private, with the `realliance` all-repos teams (Owners, maintainers, members) and no hooks, keys, mirrors, secrets or variables. `main` `c3065015b632`. One LFS object (row 4, `oid` `5493a3f4…63cd`, 1048576 bytes), served from B2 by a 303.
- Transfer: **202** at 22:16:22Z, after a guard read 200 (id 16, private) at the source and 404 at the target.
- After: id 16, private, teams `["Owners"]`, anonymous 404. In the database the repo's owner is 208, LFS row 4 still points at repo 16, and a redirect row maps (52, `restore-drill-fixture`) to 16. At the new path the LFS media answers 303 to B2 with sha256 = `oid`, and a fresh clone with LFS gives `object.bin` with sha256 = `oid`.
- Old path: the API answers 301. git fails with `RPC failed; HTTP 301`: git follows a redirect only on the first request, and a private repo's 301 comes on the authenticated retry after the 401. With `http.followRedirects=true` the old URL reaches `c3065015b632`.
- `restore-drill.sh fixture` (read-only when the repo exists) says the repo already exists. `check` didn't run, because `forgejo-drill` is down.
- luma-homeops `39931637`: `FIXTURE=`, the create call (org and name from `FIXTURE`), `docs/forgejo.md`, and `check` compares `realliance-ops/forge`. The old inventory path would fail its prod `ls-remote` for the same 301 reason. The fixture stays out of the inventory, and `tofu plan` says `No changes.`

## Task 8: verification, retiring the old secret

- Step 1 (sso `main` `aa14177`, run alone from 22:19Z to 22:43Z): 86 passed, 2 failed, 11 skipped (the opt-in drills). Both failures, `test_member_cannot_see_inventory` and `test_pages_repo_is_root_site`, stopped in `forge_login` ("Forgejo did not start a session") before their assertions. Re-run alone, both passed. The passes include `test_members_cannot_push_bots`, the writers test, `test_galaxy_serves_forge_bot_and_ci_dispatch_only`, `test_ops_registry_token_scope` and `test_allowlist_jobs_wait`.
- Step 2: tasks 82 (PR `test`), 84 (`main` `test`) and 85 (`main` `image`) succeeded on runner 17, and task 83 (PR `image`) skipped by design. Rows 17 and 18 are `idle` at the new paths, and the `galaxy` job listing is `null`.
- Step 3: `tofu plan` on inventory `354690f`: `No changes.`
- Step 4 checks (22:45:45Z): a scan of every branch, tag and open PR of the 5 `realliance` repos found no `REGISTRY_TOKEN`, `toJSON(secrets)`, `secrets[` or `secrets: inherit` in any workflow or action file. The only secret reference is `GITHUB_TOKEN` in `realliance.net`, which publishes to ghcr.io. No repo has secrets or variables. forge-ci had one token row (30, `ci-registry`, `write:package`). Its PBKDF2 hash matched `FORGE_CI_PACKAGE_TOKEN` and no other sso-secrets value, and no other file held the value. The DELETE predicate (id, uid 137, name, scope, hash) selected 1 row. Positive control: the token read `/packages/realliance` (200), and the registry's token carried `UserID` 137.
- Step 4 (22:46:11Z): deleting the `realliance` org secret `REGISTRY_TOKEN` gave **204**, and that org's secrets are `[]`; `realliance-ops` keeps its own. One transaction on `acid-forgejo-0`, the leader: DELETE, `GET DIAGNOSTICS ROW_COUNT`, raise unless 1, COMMIT. It deleted 1 row, and forge-ci holds 0 tokens. The old token now gets **401** from the API and **401** from the registry's `/v2/token`.
- sso-secrets `e427db0` removes `FORGE_CI_PACKAGE_TOKEN`. sso PR #49 (`4c27b83`, open) adds `test_realliance_has_no_registry_token` and `test_forge_ci_holds_no_token` and drops the forge-ci leg of `test_ops_registry_token_scope`; the 5 token tests pass. Until #49 merges, the two tests on `main` that read the removed key fail with `KeyError`.
- Rollback now needs a new token: mint one, then set the org secret again.
- Follow-up 4 needs no item in the fork's `WISHLIST.md`: the database delete revoked forge-ci's token.
