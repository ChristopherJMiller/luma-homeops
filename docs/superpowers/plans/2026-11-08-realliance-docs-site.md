# Realliance docs site Implementation Plan

<!-- vale write-good.TooWordy = NO -->
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
<!-- vale write-good.TooWordy = YES -->

**Goal:** `https://docs.realliance.net/` serves the Realliance member docs from Cloudflare's edge, branded like the forge and linted by Vale on every PR. A reviewed merge to `main` of `realliance/docs` goes live within about an hour, and the site keeps serving while the forge, galaxy or the home uplink is down.

**Architecture:**
- **Source:** `realliance/docs`, public. `Fumadocs` on React Router and `Vite`, built static, branded from `realliance/brand` `v0.1.0`. Its `site.yml` runs `vale` (the shared action) and `build` on `tiny`. It holds no secret and deploys nothing.
- **Deploy:** `realliance-ops/docs-deploy`, Owners-only. Each hour at `:17`, or at once on dispatch, it compares the live `build.json` with `main`'s commit. On a difference it builds that commit and deploys it with `wrangler` to the assets-only Worker `realliance-docs`. The token sits in one step of one job.
- **Name:** `cloudflare/dns/workers.tf` attaches `docs.realliance.net` to the Worker as a Workers Custom Domain, through a second provider alias whose token the cluster never gets.

**Tech Stack:** `fumadocs-core` 16.18.1, `fumadocs-mdx` 15.5.0, `@fumadocs/base-ui` 16.18.1, React Router 8.5.1, React 19.3.1, `Vite` 8.3.6, Tailwind 4.3.5, TypeScript 7.0.2, static `Orama` search, `Shiki` Catppuccin themes; `wrangler` (one exact pin, Task 5); Node 24.21.0 with a checksummed `npm`; Forgejo 15 Actions on `tiny`; OpenTofu with `svalabs/forgejo` 1.7.0 (`realliance-ops/forge`); Terraform with `cloudflare/cloudflare` 5.25.0 (`cloudflare/dns/`); pytest (`realliance/sso` `tests/forge/`, luma-homeops `scripts/`); bash, and Node scripts with no dependencies.

**Spec:** `docs/superpowers/specs/2026-11-07-realliance-docs-site-design.md`, approved by Chris on 2026-11-08 with its amendment of that day. Read it with `2026-11-07-realliance-shared-actions-design.md` (the Vale action, the pin rule, the hand-off) and that spec's plan, written in parallel. Task 6 consumes that plan's outputs.

## Global Constraints

### Fixed values (from the spec)

- **Repositories:** `realliance/docs` is public: outsiders open fork PRs, a member approves each run, and it holds no secret. `realliance-ops/docs-deploy` is public and Owners-only. `realliance-ops/forge` `repos.tf` creates both and protects their `main`.
- **Worker:** `realliance-docs`, assets only, no routes. `wrangler.jsonc` is `{ "name": "realliance-docs", "compatibility_date": "2026-11-07", "workers_dev": false, "preview_urls": false, "assets": { "directory": "./site", "not_found_handling": "404-page", "html_handling": "auto-trailing-slash" } }`.
- **Name:** `docs.realliance.net` is `cloudflare_workers_custom_domain.docs` in `cloudflare/dns/workers.tf`, with `provider = cloudflare.workers` and `account_id = data.cloudflare_zone.realliance_net.account.id`. The fallback is pre-approved for one case only, Cloudflare refusing the custom domain over the wildcard: a `proxied = true` `AAAA` record for `docs` at `100::` in `records.tf`, plus a `cloudflare_workers_route`.
- **Workflows:** `realliance/docs` has `.forgejo/workflows/site.yml` with `name: site`, on `pull_request` and on `push` to `main`, jobs `vale` and `build`. `realliance-ops/docs-deploy` has `.forgejo/workflows/deploy.yml` with `on: {schedule: [{cron: "17 * * * *"}], workflow_dispatch: {}}`, `concurrency: {group: docs-deploy, cancel-in-progress: false}`, and jobs `check`, `build` and `deploy`.
- **Step rules:** every `run` step blanks `GITHUB_TOKEN`, `FORGEJO_TOKEN` and `ACTIONS_RUNTIME_TOKEN` in its own `env:`. Every job checks out anonymously: `git init -q .`, `git fetch -q --depth=1 --no-tags "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY" "$GITHUB_SHA"`, `git checkout -q --detach FETCH_HEAD`. Never `actions/checkout`, `pull_request_target`, `always()` or `continue-on-error`. Every `uses:` names a 40-hex commit. The artifact steps use `realliance/forgejo` `image.yml`'s pins: `https://data.forgejo.org/forgejo/upload-artifact@16871d9e8cfcf27ff31822cac382bbb5450f1e1e` and `https://data.forgejo.org/forgejo/download-artifact@d8d0a99033603453ad2255e58720b460a0555e1e`.
- **`tiny`:** 1 CPU, 2 GiB, 15 min, `/work` a 1 GiB memory volume inside the 2 GiB, egress on 80 and 443, `node` 24.21.0 and no `npm`. Installs run `npm ci --ignore-scripts` with the cache under `$RUNNER_TEMP`, deleted before the build.
- **Deploy credential:** secret `CLOUDFLARE_DEPLOY_TOKEN` and variable `CLOUDFLARE_ACCOUNT_ID` on `realliance-ops/docs-deploy`, read only by the `deploy` job's last step. The token is account-owned, "Specified Workers" = `realliance-docs`, role Editor, no zone permissions, a year's expiry, no IP filter. It rotates at expiry and when an Owner changes.
- **Terraform workers token:** Workers Routes write on `realliance.net` and Workers Editor for the account, in its own git-crypt file that `tf.sh` exports and the cluster never gets. The cluster's DNS token stays unchanged.
- **Versions:** `fumadocs-core` 16.18.1; `fumadocs-mdx` 15.5.0; `fumadocs-ui` as `npm:@fumadocs/base-ui@16.18.1`; `react-router`, `@react-router/node` and `@react-router/dev` 8.5.1; `react` and `react-dom` 19.3.1; `vite` 8.3.6; `tailwindcss` and `@tailwindcss/vite` 4.3.5; `typescript` 7.0.2; `lucide-react` 1.70.0. No `takumi-js`. `wrangler` is the newest release at least 14 days old, pinned exactly in both repositories.
- **Brand:** `realliance/brand` `v0.1.0`: tag object `3cbe737588c07551fb95739ca84661837a15510a`, commit `bad7f245a91ada5a88550cc9729ad0522d94c3b8`, signer `E9245FED84E0EC721C7B5C336BFB8037115ADE26` (Chris, `https://git.realliance.net/chris.gpg`). `realliance-palette.css` and `mark-wide-auto` stay unused.
- **Browser:** no request to a third-party origin, no Google Fonts, no cookie. `_headers` sets `nosniff`, a referrer policy, HSTS without `includeSubDomains`, and a CSP on `default-src 'self'` and `frame-ancestors 'none'` that names no other origin. `/assets/*` and `/brand/*` cache for a year; `/build.json` doesn't cache.
- **Pages:** at the root, with Welcome at `/`. Each page links "Edit on the forge" to its source. Prose goes in component children: no `description=` prop, and front matter `description` at most 160 characters. The MDX map registers `Card`, `Tabs`, `Steps`, `Callout` and `Accordion`.
- **Content boundary:** the site carries what a member does and sees, and the limits a member meets. These stay in `luma-homeops`: operator procedures, global cap values, threat analyses, contact routes, restore steps, secret names, key stores, account and zone ids, tokens, node names, addresses, the LAN and the WAN address. Pages use the phrases "Realliance's own hardware" and "the cloud" for where things run.
- **Thresholds:** the build peaks under 1.5 GiB (a hard STOP, Task 1). Each `build` and `deploy` finishes within 5 min with `/work` under 0.8 GiB, and a `check` within a minute. The v1 search index stays under 200 KB.

### How work lands

- **Execution:** subagent-driven, one task at a time, a fresh reviewer for each.
- **Local compute:** 4 threads: `taskset -c 0-3`, `UV_THREADPOOL_SIZE=4`, `nix --cores 4`. No local docker or kind.
- **Signing and hooks:** sign every commit (`git commit -S`; in luma-homeops `nix develop -c git commit -S`). Never `--no-verify` or `--no-gpg-sign`, and never force-push. Shells that sign or use `ssh` or `gpg` export `DISPLAY=:10.0 XAUTHORITY=$HOME/.Xauthority`. Every commit message ends with these two lines:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01VEim3itci5Wu1hTGS8MxW4`
- **luma-homeops** (public on GitHub): worktree `~/Repos/luma-homeops-docs`, branch `docs-site`, made for git-crypt in four steps: `git -C ~/Repos/luma-homeops fetch -q origin`; `git -C ~/Repos/luma-homeops worktree add --no-checkout ~/Repos/luma-homeops-docs -b docs-site origin/main`; copy `~/Repos/luma-homeops/.git/git-crypt` into `~/Repos/luma-homeops/.git/worktrees/luma-homeops-docs/`; `git -C ~/Repos/luma-homeops-docs checkout docs-site`. The ledger and docs push to `main`. `cloudflare/dns/` pushes only after its apply.
- **`realliance-ops/forge`:** worktree `~/Repos/realliance-forge-docs` on branch `docs-repos` from `origin/main`, a signed commit, `fj -H git.realliance.net pr create`. Chris merges fast-forward. Plans and applies run in `~/Repos/realliance-forge` after `git pull --ff-only`, with the README's env block, only inside a STOP.
- **`realliance/sso`** (GitHub): worktree `~/Repos/realliance-sso-docs` on branch `docs-site` from `origin/main`, which carries the merged PR #66. `git commit -S`, `gh pr create --fill`, merge only when Chris says merge. Forge writes go through `forge_api` (`cd ~/Repos/realliance-sso-docs/tests && nix develop --command python -c …`).
- **`realliance/docs`:** clone `ssh://git@git.realliance.net/realliance/docs.git` to `~/Repos/realliance-docs`. Work on branches; merge by Task 2's rule and R1.
- **`realliance-ops/docs-deploy`:** clone to `~/Repos/realliance-docs-deploy`. PRs; Chris merges fast-forward.
- **Prose:** everything written for people goes through Chris's Vale config: `nix shell nixpkgs#vale -c vale <files>`, with 0 errors and 0 warnings. Page prose in `realliance/docs` also passes `scripts/lint-docs`, the CI gate's pin.
- **Waits:** `Monitor` for anything over 60 s (CI runs, ticks, applies), never a sleep loop.
- **Ledger:** `docs/superpowers/plans/2026-11-08-realliance-docs-site.ledger.md` in luma-homeops. It records answers, gate states, measurements, plan counts, version and deployment ids, rulings and times. luma-homeops is public, so no account id, zone id or token goes in it.

### Secrets

- Never open, `cat`, `grep` or diff a `*.secret.*` file, anything under `nodes/`, or any file in a `*-secrets` repo. Every grep excludes them: `grep --exclude='*.secret.*'`, `git grep … -- ':!*.secret.*'`, `git diff --no-textconv`.
- Three sanctioned uses read them, and none prints a value: `cloudflare/dns/tf.sh`, the forge README's `set -a; . …; set +a` apply block, and the SSO test suite with its `forge_api`.
- No secret goes in argv, a log, PR text, a commit message or the ledger. Tokens travel through pipes, stdin and `umask 077` files. Chris pastes tokens himself.

### When to stop and ask

- **STOP—ask Chris** is one AskUserQuestion: the change, the evidence, what to watch, the rollback trigger. Its options are "approve: do it, and roll back at once if a trigger fires" and "hold" (the house wording). A rejected or interrupted call may already have run, so read live state before redoing anything.
- An apply (`tofu` on `realliance-ops/forge` or `realliance/sso` `dns/`, Terraform on `cloudflare/dns`) runs only when its plan shows exactly the lines the task names. Any other line: stop and report.
- Chris's own steps: both Cloudflare tokens, the laptop deploy that creates the Worker, every merge the house rules reserve to him, `wrangler login`, and `az login` when stale.

### Gates

- **A1, the shared-actions plan:** `realliance/actions` `v1.1.0` exists as an annotated tag, verifies against `https://git.realliance.net/chris.gpg`, and its `^{commit}` is the pin. Its tree holds `vale/template/docs.yml` and `vale/template/lint-docs`. A1 gates Task 6 and every task after it.
- **A2, B2 state:** no other plan has a `realliance-ops/forge` apply open. A2 gates Task 2's apply.

## Decisions and findings

- **D1. Measure in a scratch repo.** The spec measures before any repository exists, so Task 1 runs in Chris's personal repo `chris/docs-measure`, which Task 16 archives.
- **D2. Check names.** Forgejo names a job's status `"<workflow name> / <job name> (<event>)"` (`services/actions/commit_status.go`), so the required contexts are `site / vale (pull_request)` and `site / build (pull_request)`. Task 1 confirms the form on a real PR before `repos.tf` names them. `protected_file_patterns` stays unset: on Forgejo 15 a PR that touches a protected file can't merge at all (`MergeBlockedByProtectedFiles`), so the setting can't force review of `.forgejo/`. Review of the diff and the required checks carry that.
- **D3. No administrator bypass.** Forgejo 15 has `apply_to_admins` on a branch rule; provider 1.7.0 doesn't. Unless R1 picks option B, an Owner sets it to `true` on `realliance/docs` `main` through the API, and the SSO test asserts it, so no administrator merges past Vale.
- **D4. Merge style of `realliance/docs`:** squash by default, fast-forward allowed, no merge commits, no rebase.
- **D5. `docs-deploy` keeps its own copies** of `tool.lock`, `fetch-npm.sh` and `output-checks.mjs`, plus `changed.sh` and `build-site.sh`. CI and the laptop fallback share `build-site.sh`. The `deploy` job needs `npm` without running any docs code.
- **D6. The `deploy` job runs the output checks again** from its own checkout, on the unpacked tree. The `build` job's checkout is writable by the docs build code that ran before its checks.
- **D7. `wrangler` for previews lives in `preview/`,** a package of its own, so the `npm ci` in CI installs only the measured set.
- **D8. Brand paths.** Files keep the manifest's hashed names under `public/brand/`, mirroring `dist/`, so the fonts CSS's relative `url()`s hold. `app/brand.ts` finds paths in `brand.lock.json`. `sync-brand.sh --css <palette.json>` prints `app/brand.css`, which `check-build.sh` compares.
- **D9. No account id in a public log.** The variable isn't masked the way a secret is, so the Deploy step pipes `wrangler`'s output through a `sed` that replaces it, and sets `WRANGLER_SEND_METRICS=false`.
- **D10. Leak patterns.** `check-content.sh` also fails on a dotted IPv4 address, a 32-hex id, and `secrets.env`, `.secret.` or a `-secrets` repo name.
- **D11. Terraform token plumbing:** `variable "workers_api_token"` (sensitive), set by `tf.sh` as `TF_VAR_workers_api_token` from `cloudflare/dns/workers-api-token.secret.conf`. `scripts/test_docs_site.py` and a pre-commit hook `docs-site` guard it.
- **D12. `npm`:** the release that Node 24.21.0 bundles. `tool.lock` holds one line: `npm <version> https://registry.npmjs.org/npm/-/npm-<version>.tgz sha512-<base64>`.
- **D13. No `llms.txt` and no image route.** Nothing in the spec uses them.
- **D14. Zone TLS.** `realliance/sso` `dns/cloudflare.tf` says the zone's TLS default is off and turns on `strict` for `sso` through a configuration rule. That layer owns the zone's entry-point rule sets. If HTTPS fails on `docs`, Task 10 adds the same rule there.
- **F1. Every job receives every secret.** Forgejo 15 sends a job's runner all repo and org secrets, referenced or not (`services/actions/secret.go`, `getSecretsOfJob` calls `FetchActionSecrets(ownerID, repoID)`). The runner classes spec counts a `tiny` job's own secrets as readable by it. That puts `CLOUDFLARE_DEPLOY_TOKEN` and the `realliance-ops` org secret `REGISTRY_TOKEN` in the runner of the `build` job of `docs-deploy`, which runs `realliance/docs` code, though in no step's env. Trust still rests on review of `main`, as the spec's residual risk 2 says, without the push-and-dispatch detour. Task 1 records `ptrace_scope` and the user ids on `tiny`, which decide whether a step can read its runner. Task 0 asks Chris to accept this (recommended; the README records it) or hold. The `realliance` org holds no secret (bots plan ledger, 2026-11-04), and Task 2's test keeps it so.
- **F2. Acceptance 1 misnames `sso`.** `sso.realliance.net` is a record of `realliance/sso` `dns/` behind the Cloudflare proxy, for the SSO box, not the WAN. The check becomes: `www` and `git` resolve to the WAN address, and `sso` answers as before.
- **F3. luma-homeops is public,** though the spec calls it private. The site's content rules don't change; the ledger holds no account or zone id.
- **R1. Who approves?** One approval on `realliance/docs` `main`, and Forgejo never counts a PR poster's own approval. The builder opens PRs as `chris`. Task 0 asks Chris to choose. **A:** keep 1, and he names a member who approves the builder's PRs (recommended). **B:** keep 1, and Owners may override (`apply_to_admins` false), which can also skip Vale. **C:** start at 0 like the inventory, and raise to 1 when a second reviewer is active.

<!-- vale Google.Headings = NO -->
## Review Focus
<!-- vale Google.Headings = YES -->

1. **`build.json` missing, stale or not JSON** (the first tick, a cached copy, the 404 page's HTML, an unreachable site). `check` reports `changed`, so the tick builds and deploys `main`; it never stalls or deploys an empty commit. An unreadable `main` fails the job. Test: Task 7 `changed` cases.
2. **A link without its trailing slash, and client navigation's `.data` and search fetches.** `/pages/publish` redirects to `/pages/publish/`, and the `.data` files and the search index load with types the client accepts. Test: Task 5 probes `trailing-slash` and `data-types`.
3. **A page moves.** Its old URL keeps working through `_redirects`, and a redirect to a missing page fails the build. Test: Task 3 cases `moved-page` and `redirect-to-missing`.
4. **Prose in a short prop,** such as a long `title=` on a `Card`. Vale skips string props, so `check-content.sh` fails any string prop over 60 characters, `href` and `src` aside, on a registered component. Test: Task 3 case `long-title-prop`.
5. **`main` moves between `check` and `build`, or a dispatch lands during a tick.** The run builds and tags the commit `check` read, `build.json` says the same, and the next tick picks up the newer commit. The concurrency group queues the dispatch. Test: Task 7 `workflow` case `checked-commit`, and Task 11 Step 5.

---

## Tasks

### Task 0: Start (no code)

**Files:** create the ledger in the luma-homeops worktree.

- [ ] **Step 1:** Create the luma-homeops worktree (How work lands). Record the state of gates A1 (with the pin, if closed) and A2.
- [ ] **Step 2:** One AskUserQuestion. Chris:
  - approves the routine work: ledger and docs pushes to luma-homeops `main`; the scratch repo `chris/docs-measure` and its `tiny` runs; branches and PRs in `realliance/docs`, `realliance-ops/docs-deploy` and `realliance/sso`; the Owner API settings in Task 2 Step 5. Each STOP still needs its own OK.
  - answers R1.
  - rules on F1: accept it, recorded in the `docs-deploy` README (recommended), or hold the design.
- [ ] **Step 3:** Write the ledger: plan and spec paths, the answers, the gates. Vale it, commit, push to `main`.

### Task 1: Measure the build again (the phase 1 gate)

**Files:** a scaffold in `$SCRATCH/docs-measure/` (the executing session's scratchpad); Chris's repo `chris/docs-measure`; the ledger.

**Interfaces:**
- Produces: the scaffold that Task 3 copies (`package.json`, `package-lock.json`, `tool.lock`, `app/`, `source.config.ts`, `react-router.config.ts`, `vite.config.ts`, `tsconfig.json`). The ledger gains both peaks, both `/work` sizes, `$RUNNER_TEMP`'s location, the context string (D2), `ptrace_scope` and the user ids (F1).

- [ ] **Step 1: Scaffold.** In a new empty directory, `npm pack` the `create-fumadocs-app` release published the same day as `fumadocs-core` 16.18.1 (`npm view create-fumadocs-app time --json`). Unpack it and read it; never run it. Copy its React Router template to `$SCRATCH/docs-measure/` and set Global Constraints' exact versions, with no `^` or `~`. Drop the Google Fonts links and the image route (D13). Add 11 placeholder pages under `content/docs/`, a few paragraphs each, copied from `docs/pages.md` and `docs/ci.md` (the scratch repo is public, as luma-homeops is).
- [ ] **Step 2: Tools.** Fetch Node from `https://nodejs.org/dist/v24.21.0/node-v24.21.0-linux-x64.tar.gz` into `~/.cache/realliance-docs/node-v24.21.0`, checking sha256 `6e1db87ef58b8819e5d5402eff1536491b18edd8eb7bee5ef7897876e88dc5ff` before extracting. Write `tool.lock` (D12) from `npm view npm@<bundled version> dist.integrity`. Lock the tree with `npm install --ignore-scripts` under that Node and `npm`.
- [ ] **Step 3: Local measure** under 1 CPU and 2 GiB, with the tree on a memory-backed directory, as on `tiny`:
  ```bash
  rm -rf /dev/shm/docs-measure && cp -r "$SCRATCH/docs-measure" /dev/shm/ && rm -rf /dev/shm/docs-measure/node_modules
  taskset -c 0-3 systemd-run --user --wait --pipe -p MemoryMax=2G -p MemorySwapMax=0 -p CPUQuota=100% \
    --setenv=PATH="$HOME/.cache/realliance-docs/node-v24.21.0/bin:/run/current-system/sw/bin" \
    bash -c 'cd /dev/shm/docs-measure && npm ci --ignore-scripts --no-audit --no-fund --cache "$PWD/.npm-cache" && du -sxm . && rm -rf .npm-cache && npm run build && du -sxm .'
  ```
  Expected: exit 0 and a `Memory peak:` line. Ledger the peak, both sizes, and the build time.
- [ ] **Step 4: A real `tiny` run.** Create the repo: `forge_api("POST", "/api/v1/user/repos", sudo="chris", json={"name": "docs-measure", "private": False})`. Push the scaffold with a `.forgejo/workflows/site.yml`: `name: site`, on `pull_request` and `push`, one job `build` on `tiny` that follows Task 6's `build` job up to the build, with no checks. It fetches `npm` inline from `tool.lock` (`curl`, the `sha512` check, `tar`), the logic Task 3 moves into `fetch-npm.sh`. It also prints `$RUNNER_TEMP`, `df -m /work`, `du -sxm /work` after the install and after the build, `/proc/sys/kernel/yama/ptrace_scope`, `id -u`, the `Uid:` line of each ancestor's `/proc/<pid>/status` up to PID 1, and `/sys/fs/cgroup/memory.peak` last. Then push branch `probe` with one changed page and open a same-repo PR.
- [ ] **Step 5: Read** both runs, with `Monitor` until they end. From `GET /api/v1/repos/chris/docs-measure/commits/<head>/statuses`, ledger the context: expected `site / build (pull_request)`. Ledger the printed values and the wall time.
- [ ] **Step 6: Hard STOP if either peak, Step 3's or Step 5's, reaches 1.5 GiB.** Report the numbers to Chris and do nothing more (spec, phase 1). Below it, go on.

### Task 2: Repositories and their protection

**Files:**
- Edit: `~/Repos/realliance-forge-docs/repos.tf`, `README.md`.
- Create: `~/Repos/realliance-sso-docs/tests/forge/test_forge_docs.py`.
- Edit: `tests/forge_helpers.py` (add `DOCS_REPO = "realliance/docs"` and `DOCS_DEPLOY_REPO = "realliance-ops/docs-deploy"`) and `tests/forge/test_forge_inventory.py`, both in `realliance/sso`.

**Interfaces:**
- Consumes: D2's contexts (Task 1), R1's approval count.
- Produces: tofu addresses `forgejo_repository.docs`, `forgejo_branch_protection.docs_main`, `forgejo_repository.docs_deploy` and `forgejo_branch_protection.docs_deploy_main`; both repos with a `main`.

- [ ] **Step 1: Failing tests.** In `test_forge_docs.py`:
  - `test_docs_repo_public_and_protected`: `realliance/docs` is public with Actions on. Its `main` rule has `enable_push` false, `required_approvals` = R1's count, `dismiss_stale_approvals` and `block_on_rejected_reviews` true, `enable_status_check` true, `status_check_contexts == ["site / vale (pull_request)", "site / build (pull_request)"]`, and `apply_to_admins` as D3 says.
  - `test_docs_tags_limited_to_owners_and_maintainers`: one tag rule, `*`, teams `{"Owners", "maintainers"}`, no usernames.
  - `test_docs_deploy_repo_public_and_protected`: shaped like `test_forge_bot_repo_public_and_protected` without its tag line: public, Actions on, packages off, `fast-forward-only`; `main` takes no push, needs signed commits, and lists only Owners to merge and approve.
  - `test_docs_has_no_actions_secret`: the secret names of `realliance/docs` and of org `realliance` are both `[]`.
  In `test_forge_inventory.py`, `OPS_REPOS` gains `DOCS_DEPLOY_REPO`. `test_inventory_declares_no_realliance_repo` admits `docs` beside `forgejo_upstream` among `realliance` repos, and `docs_main` and `docs_deploy_main` among the protected. `test_members_cannot_push_bots` covers `DOCS_DEPLOY_REPO` too.
  Run `nix develop --command pytest tests/forge/test_forge_docs.py -q`. Expected: FAIL (404).
- [ ] **Step 2: `repos.tf`.** Add `forgejo_repository.docs`: owner `forgejo_organization.realliance.name`, name `docs`, `private = false`, `default_branch = "main"`, `auto_init = true`, Actions, issues and pull requests on, wiki, projects and packages off, merge styles by D4, `prevent_destroy`. Add `forgejo_branch_protection.docs_main` from the spec's table: no push, R1's approvals, `dismiss_stale_approvals`, `block_on_rejected_reviews`, and `enable_status_check` with D2's contexts. Add `forgejo_repository.docs_deploy` and `forgejo_branch_protection.docs_deploy_main` shaped like `forge_bot` and `forge_bot_main`, with `has_packages = false`. Each block gets a comment on why it exists, as the file's others do. The README's "What lives where" and "Critical repos" name both. They also say that maintainers' UI edits to `realliance/docs` show as drift, since the inventory declares it. `tofu fmt -check` and `tofu validate` pass (`nix develop ~/Repos/realliance-sso`).
- [ ] **Step 3:** Commit signed, open the PR. Chris merges fast-forward.
- [ ] **Step 4: STOP—apply** (gate A2). In `~/Repos/realliance-forge`, `git pull --ff-only`, the README's env block, then `tofu plan -no-color | grep -E '^(Plan:|  # )'`. Expected exactly the four addresses of Interfaces, each `will be created`, and `Plan: 4 to add, 0 to change, 0 to destroy.` After approval, `tofu apply`, then `tofu plan -detailed-exitcode`: exit 0. Ledger the counts.
- [ ] **Step 5: Owner settings,** which the provider can't hold: `forge_api("POST", "/api/v1/repos/realliance/docs/tag_protections", json={"name_pattern": "*", "whitelist_teams": ["Owners", "maintainers"]})` returns 201. By D3, `forge_api("PATCH", "/api/v1/repos/realliance/docs/branch_protections/main", json={"apply_to_admins": True})` returns 200. Then `tofu plan -detailed-exitcode` exits 0, and the field still reads `true`.
- [ ] **Step 6:** Run `nix develop --command pytest tests/forge/test_forge_docs.py tests/forge/test_forge_inventory.py -q`. Expected: PASS. Commit signed, open the PR to `realliance/sso`, merge when Chris says.

### Task 3: Checks and the `npm` fetch

**Files** (in `~/Repos/realliance-docs`, branch `skeleton` from `origin/main`):
- Copy from Task 1: the scaffold.
- Create: `tool.lock`, `scripts/fetch-npm.sh`, `scripts/check-content.sh`, `scripts/check-build.sh`, `scripts/output-checks.mjs`, `scripts/test-checks.sh`, `test/fixtures/content/<case>/`, `test/fixtures/site/<case>/`, `.gitignore`.

**Interfaces:**
- Produces:
  - `scripts/fetch-npm.sh [lock]` reads `tool.lock`, downloads into `${RUNNER_TEMP:-$HOME/.cache/realliance-docs}`, checks the `sha512` before extracting, and prints only the directory to put on `PATH`. It refuses a URL other than `https://registry.npmjs.org/npm/-/npm-<version>.tgz`.
  - `scripts/check-content.sh [dir]` (default `content/docs`) exits 1 on any failure and prints one `file:line: reason` for each.
  - `node scripts/output-checks.mjs <site dir> <content dir>` holds the output checks, Node standard library only. `scripts/check-build.sh <site dir>` runs the brand check (Task 4) and then `output-checks.mjs` on `content/docs`. Task 7 copies `output-checks.mjs` byte for byte.
  - A page maps to HTML as `index.mdx` to `index.html`, `<a>/index.mdx` to `<a>/index.html`, and `<a>/<b>.mdx` to `<a>/<b>/index.html`.

- [ ] **Step 1: Failing cases** in `scripts/test-checks.sh`, each with its expected exit:
  - Content, expected to fail: `description-prop` (`<Card description="…">`); `long-description` (161 characters of front matter); `long-title-prop` (a 61-character string prop other than `href` or `src` on `Card`, `Callout`, `Tab` or `Accordion`); `ipv4`; `hex-id` (a run of exactly 32 hex characters; a 40-hex commit passes); `secret-name`; `jj-not-beta` (a page under `jj/` whose `title` lacks `(Beta)`, or whose first element isn't a warning `Callout`). Expected to pass: `ok`.
  - Site, expected to fail: `google-font`; `ext-link` (`<link href="https://…">`); `ext-script` (`<script src="http://…">`); `css-url` (`url(https://…)`); `broken-link` (`href="/missing/"`); `redirect-to-missing`; `no-404`; `missing-text` (HTML without its page's title, or without its first paragraph's text); `big-file` (one sparse file over 25 MiB, made at test time); `too-many-files` (20,001 files, made at test time); `cookie` (`Set-Cookie` in `_headers`); `csp-origin` (a CSP naming a scheme, a host or `*`); `hsts-subdomains`. Expected to pass: `ok` and `moved-page` (an old path redirected to a live page).
  - `fetch-npm`, expected to fail: a wrong `sha512` exits 1 and extracts nothing; another host exits 2.
  Run `bash scripts/test-checks.sh`. Expected: FAIL, since the scripts don't exist yet.
- [ ] **Step 2: Build the scripts.** `output-checks.mjs` fails on: `googleapis` or `gstatic` in any file; an `http(s)://` URL in a `<link>` `href`, a `<script>` `src` or a CSS `url()`; an internal `href` or `_redirects` target that maps to no file (`<p>/index.html`, `<p>.html` or `<p>`); a missing `404.html`; a page's HTML lacking its title or the first 40 characters of its first paragraph, compared with tags stripped and entities decoded; more than 20,000 files, or a file over 25 MiB; `_headers` lacking `X-Content-Type-Options: nosniff`, a `Referrer-Policy`, HSTS, or a CSP with `default-src 'self'` and `frame-ancestors 'none'`; HSTS with `includeSubDomains`; a CSP source with a scheme, a host or `*`; `Set-Cookie` in `_headers`.
- [ ] **Step 3:** `bash scripts/test-checks.sh`: all PASS. `nix shell nixpkgs#shellcheck -c shellcheck scripts/*.sh`: clean.
- [ ] **Step 4:** Commit signed, push `skeleton`, no PR yet.

### Task 4: Sync the brand

**Files** (`realliance/docs`, branch `skeleton`):
- Create: `scripts/sync-brand.sh`, `scripts/test-brand.sh`, `scripts/check-contrast.mjs`, `keys/chris.asc`, `operators.yml`, `brand.lock.json`, `brand/palette.json`, `public/brand/…`, `app/brand.css`, `app/brand.ts`.
- Edit: `scripts/check-build.sh`, so the brand check runs first.

**Interfaces:**
- Consumes: `check-build.sh` (Task 3).
- Produces:
  - `scripts/sync-brand.sh <vX.Y.Z> [--rebuild]`, adapted from `~/Repos/realliance-sso/scripts/sync-brand.sh`: verify, copy, write `app/brand.css` and `brand.lock.json`, prune `public/brand/` files the map no longer names. `scripts/sync-brand.sh --css <palette.json>` prints `app/brand.css` and needs no network.
  - `brand.lock.json` has keys `repo`, `tag`, `tag_object`, `commit`, `signer`, `manifest_sha256` and `files` (served path to `{from: <logical name>, sha256}`).
  - `app/brand.ts` exports `brandFile(logical: string): string`, the `/brand/…` URL of a logical name, and throws for a name not in the lock.
  - `node scripts/check-contrast.mjs` exits 1 on any pair under 4.5:1.

- [ ] **Step 1: Failing tests** in `scripts/test-brand.sh`:
  - `css-covers-preset`: `--css brand/palette.json` sets every `--color-fd-*` that the pinned preset `catppuccin.css` defines (find its path in the package), once under `:root` and once under `.dark`, plus the `#nd-sidebar` overrides under both.
  - `css-values`: each value equals its palette key by the spec's table, Latte on `:root` and Mocha on `.dark`. `background` takes `bg`. `foreground`, and the `-foreground` of `card`, `popover`, `secondary` and `accent`, take `text`. `muted-foreground` takes `text-muted`. `card` and `popover` take `surface`. `muted`, `secondary`, `accent` and `#nd-sidebar` take `surface-hover`. `border` takes `border`; `primary` and `ring` take `primary`; `primary-foreground` takes `primary-contrast`. `info`, `success`, `warning` and `error` take their own names, and `idea` takes `warning`.
  - `verifies-v0.1.0`: `sync-brand.sh v0.1.0` exits 0, and `brand.lock.json` holds Global Constraints' tag object, commit and signer.
  - `foreign-signer`: against a local fixture repo (`BRAND_REPO=file://…`) whose tag a throwaway key signs (a temp `GNUPGHOME`, `--quick-gen-key`, no passphrase), it exits 1 with "not an operator" and changes no file.
  - `tampered-file`: a fixture with one `dist/` file changed after its manifest exits 1 and changes no file.
  - `brand-css-drift`, in `test-checks.sh`: `check-build.sh` fails when `app/brand.css` differs from the `--css` output.
  - `contrast`: `check-contrast.mjs` on a palette where `text` equals `bg` exits 1.
  Run both scripts. Expected: FAIL.
- [ ] **Step 2: Keys.** `keys/chris.asc` from `curl -fsS https://git.realliance.net/chris.gpg`; `operators.yml` lists `chris` with the fingerprint, which `gpg --show-keys keys/chris.asc` confirms.
- [ ] **Step 3: Build it.** `sync-brand.sh` keeps the SSO script's verification. Its map (D8) sends each of these to `public/brand/<its manifest file>`: `css/realliance-fonts.css`; the six `fonts/*.woff2` and the three `fonts/OFL-*.txt`; `img/mark-wide-latte.svg`, `img/mark-wide-mocha.svg`, `img/icon.svg`, `img/icon-32.png`, `img/apple-touch-icon-180.png`, `img/logo-tile-512.png`; `LICENSE-CC-BY-4.0.txt` and `LICENSES.txt`. `palette.json` goes to `brand/palette.json`. It has no palette-block check. `--css` prints a "generated by scripts/sync-brand.sh; don't edit" header, the token blocks, `--font-sans: var(--rl-font-body)`, `--font-mono: var(--rl-font-code)`, and a heading rule on `var(--rl-font-heading)`. `check-contrast.mjs` requires WCAG 4.5:1, in both palettes, for `text`, `text-muted` and `primary` on `bg` and on `surface`, `primary-contrast` on `primary`, and `error`, `success`, `warning` and `info` on `bg`. Then run `scripts/sync-brand.sh v0.1.0`.
- [ ] **Step 4:** All tests PASS, `shellcheck` clean. Commit signed, push.

### Task 5: The skeleton site

**Files** (`realliance/docs`, branch `skeleton`): `app/root.tsx`, `app/routes.ts`, `app/routes/`, `app/app.css`, `app/components/mdx.tsx`, `react-router.config.ts`, `source.config.ts`, `vite.config.ts`, `content/docs/meta.json`, `content/docs/index.mdx`, `public/_headers`, `public/_redirects`, `wrangler.jsonc`, `preview/package.json`, `preview/package-lock.json`, `scripts/preview.sh`, the `scripts` of `package.json`.

**Interfaces:**
- Consumes: `brandFile()` (Task 4), `check-build.sh` (Task 3).
- Produces: `npm run build` writes `build/client/`, with the pages rendered at build time, `404.html`, the search index, `_headers` and `_redirects`. `npm run preview [dir]` copies `dir` (default `build/client`) to `site/` and serves it with `wrangler dev` on `http://localhost:8787`. The ledger gains the `wrangler` version both repositories pin.

- [ ] **Step 1: Failing check:** `npm run build && scripts/check-build.sh build/client`. Expected: FAIL, from the scaffold's placeholders and the missing `404.html` and headers.
- [ ] **Step 2: The app.** Pages sit at `/` (base URL `/`), with Welcome from `content/docs/index.mdx`: a short real welcome, no placeholder text. Every page renders at build time (`ssr: false`), and the build writes a branded `404.html`. Static `Orama` search loads its index from the same origin on first open (`Ctrl+K`); if Workers serves the default name with a type the client refuses, the index moves to `/search.json`. The header logo switches between `mark-wide-latte` and `mark-wide-mocha` by the `dark` class. The favicons are `icon.svg`, `icon-32.png` and `apple-touch-icon-180.png`. `og:image` is `logo-tile-512.png`'s absolute URL. Fonts load from `brandFile("css/realliance-fonts.css")`, with a `preload` link for the body font. CSS order: the `catppuccin.css` preset, then `app/brand.css`. `Shiki` uses `catppuccin-latte` and `catppuccin-mocha`, switched by the same class. "Edit on the forge" points at `https://git.realliance.net/realliance/docs/src/branch/main/content/docs/<path>.mdx`. The MDX map registers `Card`, `Tabs`, `Steps`, `Callout` and `Accordion`. No `llms.txt` and no image route (D13). The root package has no lifecycle scripts.
- [ ] **Step 3: Headers.** `public/_headers` follows Global Constraints. `script-src` holds what hydration and the theme script need: hashes when the inline scripts match on every page, else `'unsafe-inline'` for scripts alone, ledgered as a ruling. `/assets/*` and `/brand/*` get `Cache-Control: public, max-age=31536000, immutable`, and `/build.json` gets `Cache-Control: no-store`. `public/_redirects` holds a comment line only.
- [ ] **Step 4: Preview (D7).** Pick `wrangler`: the newest release at least 14 days old, from `npm view wrangler time --json`, ledgered. `preview/package.json` pins it exactly. `wrangler.jsonc` equals Global Constraints'. `scripts/preview.sh` copies its directory to `site/` (ignored by git) and runs `wrangler dev` from `preview/`.
- [ ] **Step 5: Probes** against `npm run preview`:
  - `trailing-slash`: `curl -sI localhost:8787/<page without its slash>` answers with a redirect to the slashed path.
  - `data-types`: a page's `.data` file and the search index answer 200, and a client navigation in Chrome logs no error.
  - `404`: an unknown path answers 404 with the branded page.
  - `headers`: every `_headers` line arrives, and no `set-cookie` does.
  - `no-js`: `curl` output holds the Welcome text.
  - `code-contrast`: on a page with a code block, Lighthouse's color-contrast audit passes in both themes. If Latte's comment or yellow tokens fall under 4.5:1, override those two.
- [ ] **Step 6:** `scripts/check-build.sh build/client` and `scripts/test-checks.sh` PASS. Commit signed, push `skeleton`.

### Task 6: CI and the Vale gate

**Gate:** this task waits for A1.

**Files:** in `realliance/docs`: `.forgejo/workflows/site.yml`, `scripts/lint-docs`, `.pre-commit-config.yaml`, `.vale/vocab/accept.txt`, `.vale/vocab/reject.txt`, `README.md`, `CONTRIBUTING.md`. In `realliance/sso`: `tests/forge/test_forge_docs.py`.

**Interfaces:**
- Consumes: the A1 pin `<V>` (40 hex), and `vale/template/docs.yml` and `vale/template/lint-docs` at it; the scripts of Tasks 3 to 5.
- Produces: the required checks `site / vale (pull_request)` and `site / build (pull_request)`; the artifact `site`, which holds the contents of `build/client`.

- [ ] **Step 1: Verify the pin** as the shared-actions spec says: import `https://git.realliance.net/chris.gpg` into a temp `GNUPGHOME`; `git verify-tag v1.1.0` shows a good signature from Global Constraints' fingerprint; `git rev-parse v1.1.0^{commit}` prints `<V>`. Ledger it.
- [ ] **Step 2: Failing test** in `test_forge_docs.py`: `test_docs_fork_pr_run_waits_for_approval`. A `forge_user(groups=("/apps/forgejo",))`, who isn't a member, forks `realliance/docs`, commits a one-line page change and opens a PR. Within 2 minutes, `GET /api/v1/repos/realliance/docs/actions/runs?event=pull_request` lists a run for that head with `need_approval` true and no task started. Cleanup closes the PR and deletes the fork. Run it. Expected: FAIL, since `main` has no workflow yet.
- [ ] **Step 3: `site.yml`.** `name: site`; `on: {pull_request: {}, push: {branches: [main]}}`. Job `vale` comes from the template, with pin `<V>` and comment `# v1.1.0`; its `paths` input is a block scalar, one per line: `content`, `README.md`, `CONTRIBUTING.md`. Job `build` runs on `tiny` with `timeout-minutes: 10`: the anonymous checkout; `scripts/fetch-npm.sh >>"$GITHUB_PATH"`; `npm ci --ignore-scripts --no-audit --no-fund --cache "$RUNNER_TEMP/npm-cache"`, then delete the cache; `du -sxm /work`; `npm run build`; `scripts/test-checks.sh`; `scripts/check-content.sh`; `node scripts/check-contrast.mjs`; `scripts/check-build.sh build/client`; `du -sxm /work` and `cat /sys/fs/cgroup/memory.peak`; upload `build/client` as `site`. Global Constraints' step rules apply to every step.
- [ ] **Step 4: Local gate and front matter.** `scripts/lint-docs` comes from the template and reads its pin from `.forgejo/workflows/site.yml`; if the template names another workflow file, set the path. `.pre-commit-config.yaml` has one `repo: local` hook, `lint-docs`, on staged `.md` and `.mdx` files. `.vale/vocab/accept.txt` takes the site's own literal words that the shared list lacks; `reject.txt` starts empty. `README.md` fits one screen: what this is, `npm ci --ignore-scripts`, `npm run dev`, `npm run preview`, `scripts/lint-docs`, where it deploys. `CONTRIBUTING.md`: fork, open a PR, a member approves your run, Vale errors block. `scripts/lint-docs`: 0 errors, 0 warnings.
- [ ] **Step 5: First merge.** Open the PR `skeleton` to `main`, and `Monitor` both checks to success. Ledger each job's wall time, the `/work` sizes, the peak and the artifact's size. Approve by R1, then merge.
- [ ] **Step 6: Phase 1 acceptance,** each result ledgered:
  - A Vale error: branch `probe/vale-error` with a misspelled word on a page, and a PR. `site / vale` fails, and Forgejo refuses `POST /api/v1/repos/realliance/docs/pulls/<n>/merge`. Close the PR, delete the branch.
  - Warnings only: branch `probe/vale-warning` with one `write-good` warning. Both checks succeed, and the log prints the warning. Close, delete.
  - The `vale` job finishes in under a minute.
  - Step 2's test PASSES, and so do `test_forge_docs.py` and `test_forge_inventory.py`. Commit the test signed, open the PR, merge when Chris says.

### Task 7: `docs-deploy` contents

**Files** (`~/Repos/realliance-docs-deploy`, branch `initial`): `.forgejo/workflows/deploy.yml`, `wrangler.jsonc`, `package.json`, `package-lock.json`, `tool.lock`, `scripts/fetch-npm.sh`, `scripts/output-checks.mjs`, `scripts/changed.sh`, `scripts/build-site.sh`, `scripts/check-output.sh`, `scripts/test.sh`, `test/fixtures/`, `README.md`.

**Interfaces:**
- Consumes: byte copies of `output-checks.mjs` and `fetch-npm.sh` from `realliance/docs` `main`; the `wrangler` version (Task 5).
- Produces:
  - `scripts/changed.sh <event>` prints `commit=<40 hex>` and `changed=true|false`, for `$GITHUB_OUTPUT`. It reads `main` with `git ls-remote "${DOCS_REPO_URL:-https://git.realliance.net/realliance/docs}" refs/heads/main`. It reads the served commit with `curl -fsS --max-time 20 -H 'Cache-Control: no-cache' "${SITE_URL:-https://docs.realliance.net}/build.json?run=${GITHUB_RUN_ID:-local}"` and parses it with `node`. `changed` is `true` on a dispatch, on another commit, or on any failure to read `build.json`. An unreadable `main` exits 1.
  - `scripts/build-site.sh <commit> <out dir>` fetches `realliance/docs` at `<commit>` anonymously into a temp dir. It gets `npm` from that tree's `tool.lock` through this repo's `fetch-npm.sh`, runs `npm ci --ignore-scripts` with the cache deleted before the build, then `npm run build`. It writes `build/client/build.json` as `{"commit":"<commit>"}`, runs `scripts/check-output.sh`, writes `<out>/site.tar` (relative paths, sorted), and prints only `sha256=<hex>` on `stdout`; its logs go to `stderr`.
  - `scripts/check-output.sh <site dir> <content dir>` runs `output-checks.mjs`, and given a `COMMIT` in its env, checks that `build.json` names it.

- [ ] **Step 1: Failing tests** in `scripts/test.sh`, run with `nix shell nixpkgs#yq-go nixpkgs#shellcheck`:
  - `workflow`: triggers and concurrency as Global Constraints; the three token blanks in every run step; no `actions/checkout`, `pull_request_target`, `always()` or `continue-on-error`; every `uses:` at 40 hex; exactly one `secrets.` reference, `secrets.CLOUDFLARE_DEPLOY_TOKEN`, in `deploy`'s last step, with the one `vars.` reference beside it; `build` has `if: needs.check.outputs.changed == 'true'`; `deploy` has `needs: [check, build]`. Case `checked-commit`: `build` and `deploy` take the commit from `needs.check.outputs.commit` and never run `ls-remote`. `deploy` runs `scripts/check-output.sh` on the unpacked tree before the Deploy step (D6), and the Deploy step pipes through the redaction (D9).
  - `changed`, with a local bare repo and `file://` fixtures: the served commit equal to `main` gives `false`; another commit, the 404 page's HTML, malformed JSON and an unreachable URL each give `true`; `workflow_dispatch` with the same commit gives `true`; a missing `main` exits 1.
  - `check-output`: Task 3's site cases, copied.
  - `wrangler`: `wrangler.jsonc` equals Global Constraints', with no `routes` and no `main`.
  - `copies`: `output-checks.mjs` and `fetch-npm.sh` match the sha256 values the README lists beside the `realliance/docs` commit they came from.
  Run it. Expected: FAIL.
- [ ] **Step 2: Build it.**
  - `check` (`timeout-minutes: 3`): the anonymous checkout of this repo, then `scripts/changed.sh "$GITHUB_EVENT_NAME" >>"$GITHUB_OUTPUT"`. It installs nothing.
  - `build` (`timeout-minutes: 10`): the checkout, `scripts/build-site.sh "$COMMIT" out >>"$GITHUB_OUTPUT"`, and the upload of `out/site.tar` as `site`.
  - `deploy` (`timeout-minutes: 10`): the checkout; the download of `site`; `sha256sum -c` against `needs.build.outputs.sha256`; a refusal of any absolute or `..` entry (`tar -tf`); unpack into `site/`. It fetches `realliance/docs` at `COMMIT` anonymously, as data only, and runs `COMMIT=… scripts/check-output.sh site <that tree>/content/docs`. Then `scripts/fetch-npm.sh` with this repo's lock, `npm ci --ignore-scripts`, and the spec's Deploy step. That step adds `WRANGLER_SEND_METRICS: "false"`, exits when `CLOUDFLARE_ACCOUNT_ID` is empty, and runs `set -o pipefail` with `… 2>&1 | sed -u "s/$CLOUDFLARE_ACCOUNT_ID/<account>/g"`. If `--tag` refuses a full commit, use a 12-character prefix, with the full id in `--message`.
  - `README.md`, one screen, Vale-clean: what the repo does; the schedule and the dispatch; the laptop fallback (Node 24 from `nix shell nixpkgs#nodejs_24`, `npm ci --ignore-scripts` here for `wrangler`, `wrangler login`, `scripts/build-site.sh <main commit> out`, unpack, `wrangler deploy --tag <commit> --message <commit>`); holding a rollback (turn off `deploy.yml` first with `Disable workflow` under Actions, then `wrangler rollback`); the spec's recovery steps; the residual risks, F1 among them; which files are copies, and from which commit.
- [ ] **Step 3:** Tests PASS, `shellcheck` clean.
- [ ] **Step 4:** Commit signed, push `initial`, open the PR. Don't merge it: Task 11 does.

### Task 8: Chris deploys from his laptop

**Interfaces:**
- Produces: the Worker `realliance-docs` and its first deployment, tagged with `realliance/docs` `main`'s commit.

- [ ] **Step 1: STOP—Chris's step, the first production deploy.** On his laptop, in a clone of `docs-deploy` at `initial`, Chris runs `wrangler login`, then the README's laptop fallback. This creates the Worker with no route, and counts as the first fallback drill.
- [ ] **Step 2: Read.** Under Chris's login, `wrangler deployments list` shows one version tagged with the commit. Ledger the version id and the commit. No URL serves it yet: `workers_dev` and `preview_urls` are off.

### Task 9: `docs.realliance.net` in `cloudflare/dns/`

**Files** (luma-homeops worktree): create `cloudflare/dns/workers.tf` and `scripts/test_docs_site.py`; edit `cloudflare/dns/main.tf`, `cloudflare/dns/tf.sh`, `cloudflare/dns/README.md` and `.pre-commit-config.yaml`. Chris writes `cloudflare/dns/workers-api-token.secret.conf`.

**Interfaces:**
- Consumes: the Worker (Task 8).
- Produces: `provider "cloudflare" { alias = "workers", api_token = var.workers_api_token }`; `variable "workers_api_token" { type = string, sensitive = true }`. `tf.sh` exports `TF_VAR_workers_api_token` from `workers-api-token.secret.conf`, a single `api-token=<value>` line, and never prints it. It fails with a message naming the file when the file is missing or still encrypted.

- [ ] **Step 1: Failing tests** in `scripts/test_docs_site.py`:
  - `test_docs_domain_shape`: exactly one of two shapes holds. Either the custom domain: one `cloudflare_workers_custom_domain` `docs`, `hostname` `docs.realliance.net`, service `realliance-docs`, `provider = cloudflare.workers`, `zone_id = local.zones["realliance.net"].id`, `account_id = data.cloudflare_zone.realliance_net.account.id`, and no `docs` record in `records.tf`. Or the fallback: an `AAAA` `docs` at `100::` with `proxied = true` in `records.tf`, plus one `cloudflare_workers_route` for `docs.realliance.net/*` to `realliance-docs` on the workers provider.
  - `test_no_account_id_in_tf`: no `account_id` holds a literal in any `cloudflare/dns/*.tf`.
  - `test_workers_provider`: the alias and the sensitive variable, as in Interfaces.
  - `test_tf_sh_workers_token`: in a scratch copy, with a stub `terraform` that reports only whether it sees `TF_VAR_workers_api_token`, and the value's length, a fake `tok-<random>` reaches the stub and never shows on `tf.sh`'s `stdout` or `stderr`. Without the file, `tf.sh` exits 1 and names it.
  - `test_workers_token_file_is_git_crypted`: `git check-attr filter cloudflare/dns/workers-api-token.secret.conf` reports `git-crypt`.
  Add the pre-commit hook `docs-site`, `files: ^(cloudflare/dns/.*|scripts/test_docs_site\.py|\.gitattributes|\.pre-commit-config\.yaml)$`. Run `nix develop --command python -m pytest -q -p no:cacheprovider scripts/test_docs_site.py`. Expected: FAIL.
- [ ] **Step 2: Write** `workers.tf` (the spec's block), the `main.tf` additions, and `tf.sh` (its read of the cluster token stays). The README's design diagram and Workflow gain the custom domain and the second token. Tests PASS, and the `pages-manifests` hook still passes. Commit signed on `docs-site`; don't push.
- [ ] **Step 3: STOP—Chris creates the Terraform workers token** with Global Constraints' scope, and writes it himself, under `umask 077`, into `cloudflare/dns/workers-api-token.secret.conf` as `api-token=<value>`. The session never opens the file. After `git add`, `git cat-file -p :cloudflare/dns/workers-api-token.secret.conf | head -c 10 | od -c` starts with `\0 G I T C R Y P T`. Commit signed.
- [ ] **Step 4: Plan.** `az account show` succeeds, or Chris runs `az login`. `./cloudflare/dns/tf.sh plan -no-color | grep -E '^(Plan:|No changes|  # )'`. Expected exactly `# cloudflare_workers_custom_domain.docs will be created` and `Plan: 1 to add, 0 to change, 0 to destroy.` If the default provider can't read the zone, set `provider = cloudflare.workers` on the data source, have Chris add Zone Read on `realliance.net` to the workers token, and plan again. Any other line: stop and report.
- [ ] **Step 5: STOP—apply.** `./cloudflare/dns/tf.sh apply`, then `plan` again: `No changes`. If Cloudflare refuses the custom domain over the wildcard, take the pre-approved fallback: drop the custom domain, add the `AAAA` record and the route, run the tests, and plan. Expected exactly `Plan: 2 to add, 0 to change, 0 to destroy.`, then **STOP—apply** again. Ledger the counts, never the account id.
- [ ] **Step 6: Edge checks,** ledgered. `curl -sSv https://docs.realliance.net/` answers 200 from Cloudflare, with a valid certificate for the name. `dig +short www.realliance.net git.realliance.net @1.1.1.1` gives the WAN address, and `sso.realliance.net` answers as before (F2). `dig +short docs.realliance.net .1.1.1` gives Cloudflare addresses, never the WAN address, so the site doesn't depend on galaxy (goal 2). If HTTPS fails or redirects to `http://`, do Task 10; else skip it. Push `docs-site` to `main`.

### Task 10: The zone TLS rule for docs, if needed

Run this task only when Task 9 Step 6 fails.

**Files:** `~/Repos/realliance-sso-docs/dns/cloudflare.tf`.

- [ ] **Step 1:** Add rule `docs_ssl_strict` to `cloudflare_ruleset.config`, shaped like `sso_ssl_strict`: `expression = "(http.host eq \"docs.realliance.net\")"`, `action = "set_config"`, `action_parameters = { ssl = "strict" }`. Commit signed, open the PR, merge when Chris says.
- [ ] **Step 2:** Plan `dns/` as its operator does. Expected exactly `# cloudflare_ruleset.config will be updated in-place` and `Plan: 0 to add, 1 to change, 0 to destroy.` **STOP—apply**, plan again (empty), then repeat Task 9 Step 6.

### Task 11: Deploy token and the pipeline

**Files:** the `docs-deploy` PR `initial`; `realliance/sso` `tests/forge/test_forge_docs.py`.

- [ ] **Step 1: Failing test:** `test_docs_deploy_holds_its_secret_and_variable`. The repo secret names of `realliance-ops/docs-deploy` include `CLOUDFLARE_DEPLOY_TOKEN`, and a variable `CLOUDFLARE_ACCOUNT_ID` exists; no assertion message carries its value. Run it. Expected: FAIL.
- [ ] **Step 2: STOP—Chris creates the deploy token** from the Credential table. Before storing it, he runs acceptance 7: in a scratch copy of `docs-deploy` whose config adds `routes: [{"pattern": "docs-probe.realliance.net", "custom_domain": true}]`, with the current `site/`, `wrangler deploy` with that token from stdin fails on authorisation. If it succeeds, he deletes the probe domain and the token, and the work stops. Otherwise he stores the token as `CLOUDFLARE_DEPLOY_TOKEN` and the account id as variable `CLOUDFLARE_ACCOUNT_ID` on `realliance-ops/docs-deploy`, in the UI or through a pipe he runs. Step 1's test PASSES.
- [ ] **Step 3:** Chris merges `initial` fast-forward, which turns `deploy.yml` on.
- [ ] **Step 4: STOP—the first pipeline deploy.** Dispatch through `forge_api("POST", "/api/v1/repos/realliance-ops/docs-deploy/actions/workflows/deploy.yml/dispatches", json={"ref": "main"})`, and `Monitor` it. Expected: `check`, `build` and `deploy` succeed, `check` within a minute and the others within 5 min each. `https://docs.realliance.net/build.json` and the newest tag in `wrangler deployments list` name `main`'s commit. The log shows no account id. Chris confirms in the dashboard that the `workers.dev` route stays off, and `tf.sh plan` still says `No changes`, so the deploy left the name alone. Ledger where Forgejo stored the artifact. Rollback trigger: a failed `deploy` after its upload, or a served page that differs from `main`. Rollback: turn off `deploy.yml`, then `wrangler rollback`, as the README says.
- [ ] **Step 5: Ticks.** `Monitor` the next tick at `:17`: `GET /api/v1/repos/realliance-ops/docs-deploy/actions/tasks` lists one new task, `check`, while `build` and `deploy` show skipped. Merge a one-line docs PR (approved by R1), and see it served by the next tick. Dispatch once while a tick runs: the concurrency group queues it, and it runs after (Review Focus 5). Ledger the times.
- [ ] **Step 6:** Commit the test signed, open the PR, merge when Chris says.

### Task 12: Runbook lines

**Files** (luma-homeops worktree): `docs/forgejo.md`, `CLAUDE.md`.

- [ ] **Step 1:** In `docs/forgejo.md`, a section "Docs site (docs.realliance.net)": the two repositories; the hourly check at `:17` and the dispatch; the Worker, and the custom domain in `cloudflare/dns/`; the laptop fallback and the rollback hold, pointing at the `docs-deploy` README; where the token lives and when it rotates (at expiry, and when an Owner changes); F1 in one line. "Removing someone" gains step 4: when an Owner leaves, rotate the docs deploy token.
- [ ] **Step 2:** In `CLAUDE.md`'s topology table, one row "Realliance docs": `docs.realliance.net` on Cloudflare Workers static assets, off galaxy, so it serves while the forge is down; source `realliance/docs`; deploy `realliance-ops/docs-deploy`, hourly or on dispatch; the name in `cloudflare/dns/workers.tf`; runbook `docs/forgejo.md` § Docs site.
- [ ] **Step 3:** Vale both, 0/0/0. Commit signed, push to `main`. Ledger phase 2 as done.

### Task 13: v1 pages, part 1

**Files** (`realliance/docs`, branch `content-v1`): `content/docs/meta.json`, `content/docs/index.mdx`, `start/meta.json`, `start/sign-in.mdx`, `actions/meta.json`, `actions/pipelines.mdx`, `runners/meta.json`, `runners/choosing.mdx`, `runners/limits.mdx`.

Every page has front matter `title` and `description` (at most 160 characters), and its prose in children. The root `meta.json` orders the v1 sections as Start, Forgejo Actions, Runner classes, Pages, `jj` (beta), Recovery, Contributing. Facts come from luma-homeops runbooks and specs, within the content boundary:

| Path | Title | Covers | Sources |
|---|---|---|---|
| `/` | Welcome | What Realliance is; the forge, the SSO, CI, Pages and this site, as cards; that this site serves while the forge is down | `docs/forgejo.md` intro |
| `/start/sign-in/` | Sign in with SSO | Sign-in only through `sso.realliance.net`; the account made at first sign-in; the username from the SSO; team access that syncs at sign-in; an email change made in both places | `docs/forgejo.md` § Who gets in |
| `/actions/pipelines/` | Pipelines in 10 minutes | `.forgejo/workflows/`; triggers; `runs-on`; secrets (fork PRs get none; never print one); full-URL `uses:` pinned to a commit, since a short name resolves on GitHub; the anonymous checkout from the Vale template; why not `actions/checkout` | `docs/ci.md`, shared-actions spec, `image.yml`'s rules |
| `/runners/choosing/` | Choosing `tiny`, `small` or `medium` | Each class's shape, time limit and cost to its owner; what waits (`docker`, `large`) | `docs/ci.md` § Classes and modes, § Paid classes |
| `/runners/limits/` | Limits, minutes and why a job waits | Daily `tiny` minutes (120 by default) and the 20 reserved for each launch; that jobs wait their turn when `tiny` is busy, with no numbers; each hold and its meaning; the Usage tab; the default paid limit; that a Realliance-wide cap exists, with no values | `docs/ci.md` § Changing a limit, § Finding a job's record |

- [ ] **Step 1: Draft** the pages. Then `scripts/lint-docs` (0 errors; fix each warning unless a ledgered ruling keeps it), `scripts/check-content.sh`, and `npm run build && scripts/check-build.sh build/client`. The task reviewer reads each page against the content boundary.
- [ ] **Step 2:** Commit signed, push `content-v1`.

### Task 14: v1 pages, part 2

**Files** (`realliance/docs`, branch `content-v1`): `pages/meta.json`, `pages/publish.mdx`, `pages/reference.mdx`, `jj/meta.json`, `jj/using.mdx`, `jj/reviewing.mdx`, `recovery/meta.json`, `recovery/forge-down.mdx`, `contributing/meta.json`, `contributing/writing.mdx`.

| Path | Title | Covers | Sources |
|---|---|---|---|
| `/pages/publish/` | Publish a site | A public repo's `pages` branch serves at `https://<owner>.realliance.page/<repo>/`; `<owner>/pages` is the root site; the `pages-publish` action at the A1 pin, in the pinned form; about a minute to go live | `docs/pages.md` |
| `/pages/reference/` | URL, naming, size, headers and redirects | Owner names that get no site; lower case; the 128 MB cap and what happens past it; `_headers` (the allowed list) and `_redirects`, with the project-path rule; no cookies; an Owner takes a site down, and deleting the branch doesn't | `docs/pages.md` |
| `/jj/using/` | Using the forge with `jj` (Beta) | Change ids on commits, conflict labels, the `jj` pill and its clone command | fork patches `feat-ui-show-jj-change-ids-on-commits`, `feat-ui-label-commits-that-hold-a-jj-conflict`, `feat-ui-jj-pill-and-clone-command-for-repos-made-wit` |
| `/jj/reviewing/` | Reviewing a `jj` PR round by round (Beta) | Each push kept as a round; review round by round; purging a round | `feat-keep-every-push-of-a-jj-PR-as-a-round`, `feat-ui-review-jj-PRs-round-by-round`, `feat-purge-a-PR-round` |
| `/recovery/forge-down/` | If the forge is down | What stops (Git hosting, CI, Pages publishing, the registry); what keeps working (this site, your own clones); that operators are on it. No contact channel and no restore steps, and no promise that Pages sites keep serving | spec § Content |
| `/contributing/writing/` | Writing these docs | Style and the Vale rules (errors block, warnings print); `scripts/lint-docs`; prose in children, short props; new words in `.vale/vocab/accept.txt`, literal only; inline `vale off` named in the summary; fork PRs wait for a member; previews from the `site` artifact with `npm run preview` | shared-actions spec, Tasks 5 and 6 |

The `jj` pages carry "(Beta)" in their titles and open with a warning `Callout`: the fork's `jj` features may change at the 19 LTS rebase, and rebase merges and divergent changes wait for the fork. Read patches in `~/Repos/realliance-forgejo-ci/patches/`; never write in `~/Repos/realliance-forgejo`, where another session works.

- [ ] **Step 1: Draft,** then Task 13 Step 1's checks.
- [ ] **Step 2:** The search index is under 200 KB, and for each page a word found only in its body appears in the index (a `grep` on the built index). Ledger both.
- [ ] **Step 3:** Commit signed, push `content-v1`.

### Task 15: Chris's review and the launch

- [ ] **Step 1:** Open the PR `content-v1`, and `Monitor` both checks to success. Give Chris the `site` artifact with `npm run preview`, the page list and the content boundary.
- [ ] **Step 2: STOP—Chris reviews the 11 v1 pages before launch.** Apply his edits on the branch and re-run the checks, until he approves.
- [ ] **Step 3:** Approve by R1 and merge. Dispatch `deploy.yml`, or wait for the tick, and `Monitor` until `build.json` names the merge commit. Ledger the launch time.

### Task 16: Acceptance and wrap-up

- [ ] **Step 1: The spec's acceptance,** each result ledgered:
  1. Task 9 Step 6 again; `tf.sh plan` says `No changes`; the forge's `tofu plan -detailed-exitcode` exits 0.
  2. In Chrome (the chrome-devtools MCP): a first visit under emulated `prefers-color-scheme: dark` shows Mocha. The toggle switches the palette, logo and code theme together. `evaluate_script` reads every `--color-fd-*` in both themes, and each equals its `brand/palette.json` source by Task 4's mapping.
  3. The network list on load and after a search holds same-origin requests only, and the console shows no CSP violation.
  4. A search for a body-only word finds its page.
  5. Task 6 Step 6's results; `test_forge_docs.py` and `test_forge_inventory.py` PASS.
  6. Task 11 Steps 4 and 5.
  7. Task 11 Step 2.
  8. The job logs' durations and `/work` sizes.
  9. `curl -sSI` on an unknown path answers 404 with the branded page. On `/`, an `/assets/` file and `/build.json`, every `_headers` header arrives and no `set-cookie` does.
  10. With JavaScript off, each v1 page shows its text. With it on, a client navigation between two pages makes no full load. At 360 pixels wide (`resize_page`), `document.documentElement.scrollWidth <= 360` on each v1 page.
  11. Lighthouse's color-contrast audit passes on `/` and `/actions/pipelines/` in both themes, and `check-contrast.mjs` passes.
- [ ] **Step 2: Wrap-up.** Archive `chris/docs-measure` (`PATCH` with `archived: true`). Ledger the follow-ups: the other 14 pages; Renovate pins once the forge's bot runs; `fetch-tool` in place of `fetch-npm.sh` in both repositories; moving the `build` to a repository with no secret if Chris revisits F1; moving the `realliance-ops` org `REGISTRY_TOKEN` to the bots' repo secrets. Vale the ledger, commit signed, push.
