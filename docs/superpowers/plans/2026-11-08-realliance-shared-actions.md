# Realliance shared actions: the `vale` action (implementation plan)

<!-- vale write-good.TooWordy = NO -->
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
<!-- vale write-good.TooWordy = YES -->

**Goal:** `realliance/actions` ships a `vale` composite action at the Owner-signed tag `v1.1.0`, with a consumer workflow template and `scripts/lint-docs`. A public repo then gets the same Vale findings in CI, in a pre-commit hook and from `lint-docs`, and an approved fork PR can't reach a token, change the rules or inject workflow commands through the Vale step.

**Architecture:** one engine, `vale/run.sh` (POSIX `sh`), serves the composite step, `lint-docs` and the tests. It clears the job tokens, fetches Vale 3.25.1 and checks it against `vale/tool.lock`, and builds a temporary config from the styles and words in git plus the consumer's checked word lists. It lints tracked regular files only, with Vale under `env -i` and `--no-global`. Findings print between `stop-commands` tokens; a counts line from the output template decides the exit. One pin, the 40-hex commit of an Owner-signed tag, selects the action, Vale, the styles and the shared words; Chris signs the tag at a STOP.

**Tech Stack:** the Vale 3.25.1 binary from GitHub releases; styles Google v0.7.1, write-good v0.4.1, `proselint` v0.3.4; POSIX `sh` scripts checked with `shellcheck -s sh`; Forgejo Actions composite actions on forgejo-runner 13.2.1 (`act`) in the `tiny` class; Forgejo 15.0.11 (API, PRs through `fj`); GnuPG; a `repo: local` pre-commit hook on the consumer side.

**Spec:** `docs/superpowers/specs/2026-11-07-realliance-shared-actions-design.md` (Chris approved it on 2026-11-08, with that day's builder-check amendment). Adoption in `realliance/docs` belongs to the docs-site spec, `2026-11-07-realliance-docs-site-design.md`; this plan ends at the hand-off.

## Global Constraints

### Fixed by the spec

- Gate: errors fail the job, warnings print and never block *(Chris decided)*. A Vale crash (exit 2) always fails.
- `vale/vale.ini`: `BasedOnStyles = Vale, Google, write-good, proselint`, `write-good.E-Prime = NO`, `Google.Units = NO`, `MinAlertLevel = warning`, `Vocab = Realliance, Local`. No `Packages` line, and nothing runs `vale sync`.
- Inputs, exactly: `paths` (default `.`), `extensions` (default `md mdx`), `fail-level` (default `error`; `error` or `warning`), `vocab` (default `.vale/vocab`). Outputs: `errors`, `warnings`. No `token`, `config`, `version` or level-floor input.
- Pin form: `uses: https://git.realliance.net/realliance/actions/vale@<40-hex commit> # v1.1.0`. A valid pin is the `^{commit}` of an annotated `v*` tag with a good signature from a key an Owners-team member publishes at `https://git.realliance.net/<user>.gpg`. Never a moving tag; `v1` stays at `v1.0.0`'s commit.
- One version line, `vX.Y.Z`, for the whole repo. Major: an input or output removed or changed in meaning. Minor: a new action or input, or any change to Vale, the styles or the shared words. Patch: a fix that changes no interface. `vale` ships as `v1.1.0`.
- Tags: Owners and maintainers may push `v*` (the live protection stays); consumers pin only an Owner-signed tag. Chris signs releases.
- Tokens `run.sh` clears: `GITHUB_TOKEN`, `FORGEJO_TOKEN`, `GITEA_TOKEN`, `ACTIONS_RUNTIME_TOKEN`, `ACTIONS_ID_TOKEN_REQUEST_TOKEN`, `ACTIONS_ID_TOKEN_REQUEST_URL`.
- Consumer template: `runs-on: tiny`, `timeout-minutes: 10`, an anonymous `git fetch` of `$GITHUB_SHA` with the three blanks, then the pinned `uses:`. Never `actions/checkout`, never `pull_request_target`.
- Shared vocabulary `Realliance`: Chris's personal `accept.txt`, minus personal names and home network host names, plus `[Jj]ujutsu`, `MDX`, `JSX`, `OpenTofu`, `git-pages`. Chris reads it at the release STOP.
- Bumps by hand PR. Vale moves monthly, to the newest release at least 14 days old.
- Non-goals hold: no reusable workflows, no PR comments or annotations, no changed-files mode, no `rst`, AsciiDoc or XML, no forge Renovate, no other shared action.

### Pins (read 2026-11-07)

| What | Value |
|---|---|
| Vale | 3.25.1, published 2026-10-22. URL `https://github.com/vale-cli/vale/releases/download/v3.25.1/vale_3.25.1_<platform>.tar.gz`. The repo moved from `errata-ai/vale`; use the new name |
| `Linux_64-bit` | `4bf5c5176b682b1da0830f471584d3494f1c2e27ee3029e1ad6f6661d4962182` |
| `Linux_arm64` | `2a69755b68118c612c455fc0c5ea1b9438519aa1c852e3fb303b3ad97141bbb0` |
| `macOS_64-bit` | `e27097e84a812d45546de8d6a4ffa75538cab39c96f79b153b601240f8cefaf9` |
| `macOS_arm64` | `fbcaa68d9b9e229b9c8dc918f209e93572ce943039700b7e04a423ff12f684cd` |
| Checksums file | `vale_3.25.1_checksums.txt`, sha256 `dd2b8190fa4766098a4eb3f864700e34cabc3c6a8148b12fe0bfdd615a811c87` |
| Google | `vale-cli/Google` tag `v0.7.1` (MIT), `Google.zip` sha256 `4b67fca1f2a88595b2a578ab98bbf3017a23af677b04f93e862843f3ea1a9a0b` |
| write-good | `vale-cli/write-good` tag `v0.4.1` (MIT), `write-good.zip` sha256 `2960ef10861ddc7dc76292a4967eb3789bf05ca24f862042f6d15f53332887f0` |
| `proselint` | `vale-cli/proselint` tag `v0.3.4` (BSD-3-Clause), `proselint.zip` sha256 `48b1fb481ba44f6505d44346b283f6cdc74efd234f913bbdb09bf34420fab81c` |
| Release signer | Chris, `https://git.realliance.net/chris.gpg`, fingerprint `E9245FED84E0EC721C7B5C336BFB8037115ADE26` (it signed `v1.0.0`) |
| `realliance/actions` today | `main` = `v1.0.0`^{commit} = `v1`^{commit} = `37a3a4b86ce871b1e1f89fdcb460de57f6bf41e8`; `refs/pull/1/head` exists, so the next PR is #2 |

The zips hold no licence: `update-styles.sh` copies each repo's `LICENSE` at the tag.

### How work lands

- **`realliance/actions`** (forge, public): a separate clone, `~/Repos/realliance-actions-vale`, branch `vale`. Leave `~/Repos/realliance-actions` alone. `git commit -S`, then `fj -H git.realliance.net pr create` (read `fj pr create --help` for flags). **Chris merges** each PR: the classifier blocks a session's own merge, so ask him to merge or to say "merge PR #N", then run `fj -H git.realliance.net pr merge realliance/actions#<n> -d`.
- **luma-homeops** (public): edit on `main`, `nix develop --command git commit -S`, pre-commit passing, push `main`. In this git-crypt repo, use `git diff --no-textconv` and `git show --no-textconv`.
- Every shell that signs exports `DISPLAY=:10.0 XAUTHORITY=$HOME/.Xauthority`. The actions clone has no `user.signingkey`, so tags take `-u E9245FED84E0EC721C7B5C336BFB8037115ADE26`.
- **At execution start, one AskUserQuestion** gets Chris's OK for the routine writes: pushing and deleting the branches `vale`, `probe-runner`, `probe-pr-head` and `pin-smoke` on `realliance/actions`, opening and closing the probe PR, opening the `vale` PR, and the luma-homeops docs push. Merges, the tag and his Vale switch each need their own word.
- **STOP, ask Chris** = one AskUserQuestion that states the change, the evidence and what follows. A rejected or interrupted call may already have run, so read live state before redoing anything.
- Never move or re-push a tag. A wrong tag gets a new patch tag.
- **Ledger:** `.superpowers/sdd/2026-11-08-realliance-shared-actions/progress.md` in luma-homeops (excluded from git by `.git/info/exclude`). Record answers, probe results, run timings and rulings. No secrets, no personal names.
- `SCRATCH` is a directory outside every repo, readable by Chris only.
- No cluster or router change happens here, so no Gate G. CLAUDE.md S1 still applies: read the forge's state before writing to it.
- **Order.** Task 1 may run beside Tasks 2 to 5, in its own worktree. Tasks 2 to 6 run in order, since they share `run.sh` and `test/vale.sh`. Task 6 needs Task 1's answers for the README. Tasks 7, 8 and 9 follow in order; Task 9 may wait for Chris.

### Execution rules

- The plan runs subagent-driven.
- Local builds and tests use 4 threads: `taskset -c 0-3`, `GOMAXPROCS=4`. Run the suite as `taskset -c 0-3 env GOMAXPROCS=4 nix shell nixpkgs#unzip -c sh test/vale.sh`.
- Never `--no-verify`, never `--no-gpg-sign`, never force-push.
- Never open `*.secret.*`, `nodes/*` or any `*-secrets` repo. Every grep carries `--exclude='*.secret.*'`. This plan needs no secret: the action takes no token.
- Every commit ends with:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01VEim3itci5Wu1hTGS8MxW4`
- All prose the builder writes (README, docs, PR text, workflow headers) passes Chris's Vale with 0 errors, 0 warnings and 0 suggestions: run `nix shell nixpkgs#vale -c vale <file>` from inside the repo. The README also passes `run.sh` at `fail-level` `warning`. Each README stays brief, about a screen a section, commands over prose.
- `realliance/actions` and luma-homeops are public: no personal names, home host names or secrets in code, fixtures, commits or PR text.
- `tiny`: 1 CPU, 2 GiB, 15 min, egress on 80 and 443, a fresh pod a job. Its image is `debian:trixie` with git, curl, unzip, tar, coreutils and node, without `jq`, Python or `shellcheck`. The `realliance` owner has `tiny_concurrent` 2 (global 1 today) and 120 minutes a day shared by all its repos; each launch reserves 20 minutes until it settles. Batch commits before a push. The plan budgets about 15 launches.
- Forgejo 15 has no job-log API. Read results with `curl -sSf 'https://git.realliance.net/api/v1/repos/realliance/actions/actions/tasks?limit=20' | jq '.workflow_runs[] | {name, status, head_sha, event, run_started_at, updated_at}'`. That's why every CI check must fail its job: a printed line proves nothing. Chris can read a log in the browser.
- Waits over 60 s use the `Monitor` tool with an until-loop on that query, never a sleep loop.

### Decisions this plan makes

- **D1. Vale runs under `env -i`** with `PATH=/usr/bin:/bin`, `HOME`, `XDG_CONFIG_HOME` and `TMPDIR` (three empty temporary directories), plus `GOMAXPROCS` when set. No token, no `VALE_*` variable and no user config reaches Vale, whatever the runner sets. This is stricter than spec step 5.
- **D2. Counting reads a template line, never a path.** `vale/output.tmpl` is exactly the block below. Vale 3.14.2 prints `docs/a.md` for `./docs/a.md`, so the template adds the `./`. The last output line is always `#vale-counts` followed by one `E` an error and one `W` a warning. `run.sh` takes the counts from it, and exits 2 when they disagree with Vale's exit (1 if and only if there are errors) or when the line is missing. Checked on Vale 3.14.2; Task 3 checks it on 3.25.1.

  ```text
  {{- range .Files}}{{$p := .Path}}{{range .Alerts}}./{{$p}}:{{.Line}}:{{index .Span 0}}: {{.Severity}} {{.Check}} {{.Message}}
  {{end}}{{end}}#vale-counts {{range .Files}}{{range .Alerts}}{{if eq .Severity "error"}}E{{else if eq .Severity "warning"}}W{{end}}{{end}}{{end}}
  ```

- **D3. Output shape.** Line 1 `::stop-commands::<32 hex from /dev/urandom>`. Then Vale's findings and its error output. Then `::<same token>::`. Last, the summary: `vale 3.25.1: errors <E>, warnings <W>, files <N>`, plus `, skipped <K>` when `K` > 0, plus `; vale off: <path> <path>` when any file has an inline switch. Every other message `run.sh` prints starts with `vale: `.
- **D4. Paths.**
  - Listing uses `git --literal-pathspecs -c core.quotePath=false ls-files -s -- <paths>`; modes `100644` and `100755` only, and the working tree must hold a regular file there. A listed entry with an allowed extension that fails this, a symbolic link or a submodule, counts as skipped.
  - A path git quotes (a control character, `"` or `\`) exits 2. Spaces, colons, non-ASCII and leading dashes work, since every path goes to Vale as `./<path>`.
  - `paths` and `extensions` split on any whitespace, so one a line and the docs-site spec's `paths: content README.md CONTRIBUTING.md` both work. Name the parent of a directory whose name holds a space: `paths` can't name that directory itself.
  - Inputs must be relative, inside the repo, without `..` and without a leading `-`. Zero files exits 2. Files under `vocab` never count as prose.
  - `run.sh` must run at the top of a git checkout. Optional arguments name files, and the run keeps those that also fall under `paths`.
- **D5. Extensions:** the allowlist is `md mdx txt html`, formats Vale parses itself. `vale.ini` gives each the full rule set: a `[*.{md,txt,html}]` section and a `[*.mdx]` section, the same three lines in each. The `mdx` section gains a key only if the MDX test needs one, with a comment saying why.
- **D6. System directories (spec step 1)** means `PATH=<tool dir>:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin:/run/current-system/sw/bin:/etc/profiles/per-user/<user>/bin:/nix/var/nix/profiles/default/bin:/opt/homebrew/bin`. `<user>` is `$USER`, else `$LOGNAME`, and the entry drops out unless the name matches `^[a-z_][a-z0-9_-]*$`. On Chris's NixOS laptop, `git` and `tar` live in the per-user profile and `curl` and `sha256sum` in `/run/current-system/sw/bin`. Every entry is absolute and fixed, none under `$HOME` or a workspace. A missing tool exits 2 naming it; `run.sh` takes `sha256sum` or else `shasum -a 256`.
- **D7. The vocabulary rule** covers the consumer's `Local` list and the shared `Realliance` list, checked on every run.
  - A line is blank, or a word built from `A-Z a-z 0-9 _ ' -`, `\.` and case classes `[Xx]`: an upper-case letter, then the same letter in lower case. 64 characters at most, at least one letter or digit.
  - No comments, no trailing space, no carriage return.
  - A refusal exits 2 and prints the file and line number, never the entry.
  - The shared list spells Chris's patterns out: `[Rr]epos?` becomes `[Rr]epo` and `[Rr]epos`.
  - Consumer word lists count only when the index lists them as regular files and the working tree holds regular files; a symbolic link, or a list missing from the index, exits 2.
- **D8. Inline switches** are `vale off` and `vale <Style> = NO` or `vale <Style.Rule> = NO` inside `<!-- -->` or `{/* */}`. The summary names every file that has one.
- **D9. Caches are atomic.** The Vale tarball caches as `vale-<version>-<sha256>.tar.gz` under `$RUNNER_TEMP/realliance-actions/`, or `${XDG_CACHE_HOME:-$HOME/.cache}/realliance-actions/` without `RUNNER_TEMP`. It lands by rename after its checksum passes, and every run checks it again and unpacks it into the run's temp dir. A bad cached file triggers one fresh download; a bad fresh download exits 2.
- **D10. `lint-docs` input grammar.** It finds the pin in `.forgejo/workflows/*.yml` and `*.yaml`, and there must be exactly one `realliance/actions/vale@<40 hex>` line. It reads `paths`, `extensions`, `fail-level` and `vocab` from the block-style `with:` under that line. A value may be plain, quoted, or a `|` block. A flow map or a `>` scalar exits 2. Inputs the step doesn't set stay unset, so `run.sh`'s defaults apply, and they equal `action.yml`'s.
- **D11. Hostile fixtures live in git,** in `test/fixtures/vale/hostile/`: a file named `::set-env name=PWNED::yes.md`, a link `environ.md` pointing at `/proc/self/environ`, a `.vale.ini` with `MinAlertLevel = error`, a file with `vale off`, and a file with lines starting `::add-path::/tmp/pwned` and `##[set-env name=PWNED;]yes`. The unit suite and the real runner read the same tree.
- **D12. `action.yml` carries no token blanks.** `act` sets the job env again on composite steps (spec Constraints, rechecked by Task 1), so `run.sh` clears the tokens itself.
- **D13. The README's `pages-publish` sample** pins `actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.1.0` with `persist-credentials: false`, as forge-bot and realliance.net do, next to the pinned `pages-publish`.

<!-- vale Google.Headings = NO -->
## Review Focus
<!-- vale Google.Headings = YES -->

1. **`paths` names nothing,** through a typo, a renamed directory or a one-line list split wrong. A green check over zero files looks like a clean tree. Expected: exit 2, `vale: no files to check`. Test: Task 3 `zero-files`.
2. **An inline switch for one rule or style,** `<!-- vale Google.Passive = NO -->` or `<!-- vale write-good = NO -->`, turns rules off as surely as `vale off`. Expected: the summary names the file. Test: Task 4 `vale-off`.
3. **The workflow's `with:` uses a form `lint-docs` reads differently,** for example a `paths: |` block, a one-line list, a quoted `fail-level` or a flow map. CI and local runs then disagree. Expected: `lint-docs` passes the same values the runner would, or exits 2. Test: Task 5 `lint-docs-inputs`, plus Task 3 `defaults`.
4. **A word list saved on Windows,** with CRLF endings or trailing spaces, gives entries that silently never match. Expected: exit 2 with the file and line. Test: Task 4 `vocab-refused`.
5. **A first run interrupted, or two hook runs at once,** leave a half-written cache that later runs trust. Expected: a broken cache entry gets replaced, never used. Test: Task 3 `cache-corrupt`, Task 5 `lint-docs-cache`.

---

### Task 0: start (no code)

**Files:** create the ledger (Global Constraints).

- [ ] **Step 1: One AskUserQuestion** for the routine writes listed under How work lands. Ledger the answer.
- [ ] **Step 2: Clone.** `git clone https://git.realliance.net/realliance/actions.git ~/Repos/realliance-actions-vale && git -C ~/Repos/realliance-actions-vale switch -c vale`. Expected: `main` at `37a3a4b`, tags `v1` and `v1.0.0` only, `git ls-remote origin` as in Pins. If `main` moved, ledger the new commits and read them before Task 2.
- [ ] **Step 3: Read the trust anchors.** In a fresh `GNUPGHOME=$(mktemp -d)`: `curl -fsS https://git.realliance.net/chris.gpg | gpg --import`, then `git -C ~/Repos/realliance-actions-vale verify-tag --raw v1.0.0`. Expected: `VALIDSIG E9245FED84E0EC721C7B5C336BFB8037115ADE26`. Then `gh api repos/vale-cli/vale/releases/tags/v3.25.1 --jq '.assets[] | "\(.name) \(.digest)"'`. Expected: the four digests in Pins. Ledger both.

### Task 1: runner probes (throwaway branches)

Answers the spec's "unverified" list through the real runner. The answers go in the ledger and the README; no product code depends on them, except the STOP in Step 5.

**Files:** in a worktree, `git -C ~/Repos/realliance-actions-vale worktree add -b probe-runner ~/Repos/realliance-actions-probe origin/main`; on `probe-runner`: `.forgejo/workflows/probe.yml`, `probe/blank/action.yml`. On branch `probe-pr-head` from `main`: `probe/remote/action.yml`. Neither branch merges. Remove the worktree at the end.

- [ ] **Step 1: The PR-head commit.** In the worktree, on a new branch `probe-pr-head` from `main`, commit `probe/remote/action.yml`, a composite whose one step runs `true`. Push, open PR #2 titled `probe, do not merge`, note its commit `C`, then delete the branch. Expected: `git ls-remote origin` shows `C` only as `refs/pull/2/head`.
- [ ] **Step 2: The probe workflow.** `on: push: branches: [probe-runner]`. Six jobs on `tiny`, `timeout-minutes: 5`, each starting with the template's anonymous checkout. Each job passes if and only if the answer is yes:
  - `composite-env-blank-honoured`: `uses: ./probe/blank`, whose step sets `env: {GITHUB_TOKEN: "", FORGEJO_TOKEN: ""}` and runs `test -z "${GITHUB_TOKEN-}" && test -z "${FORGEJO_TOKEN-}"`.
  - `set-env-works`: one step prints `::set-env name=PROBE::1`; the next runs `test "${PROBE-}" = 1`.
  - `add-path-works`: one step prints `::add-path::/probe-path`; the next checks that `$PATH` holds `/probe-path`.
  - `stop-commands-honoured`: one step prints `::stop-commands::t0k3n`, then `::set-env name=PROBE2::1`, then `::t0k3n::`; the next runs `test -z "${PROBE2-}"`.
  - `runner-temp-writable`: `test -n "${RUNNER_TEMP-}" && test -w "$RUNNER_TEMP"`.
  - `pull-head-fetchable`: `uses: https://git.realliance.net/realliance/actions/probe/remote@<C>`.
- [ ] **Step 3: Push `probe-runner`,** then use `Monitor` on the tasks query until all six jobs leave `waiting` and `running`. Expected: each ends `success` or `failure`.
- [ ] **Step 4: Ledger the six answers,** then delete `probe-runner` and close PR #2 (`fj pr close`). Expected: `git ls-remote origin 'refs/heads/probe-*'` prints nothing.
- [ ] **Step 5: STOP if `set-env-works` passes and `stop-commands-honoured` fails.** The block in D3 then protects nothing, and the `./` prefix alone guards line starts. Report to the coordinator before Task 3.

### Task 2: pinned inputs (Vale lock, styles, shared words)

**Files** (in the actions clone):
- Create: `vale/tool.lock`, `vale/update-tool-lock.sh`, `vale/styles.lock`, `vale/update-styles.sh`
- Create: `vale/styles/Google/**`, `vale/styles/write-good/**`, `vale/styles/proselint/**`, each with its `LICENSE`
- Create: `vale/styles/config/vocabularies/Realliance/accept.txt`, `reject.txt` (empty)
- Create: `test/vale.sh` (the harness and the `locks` case)

**Interfaces (produces):**
- `vale/tool.lock`: `#` comment lines, then `version 3.25.1`, then one line a platform, `<platform> <sha256> <url>`, for `Linux_64-bit`, `Linux_arm64`, `macOS_64-bit` and `macOS_arm64`.
- `vale/update-tool-lock.sh <version>` rewrites `tool.lock` from the release's checksums file and prints the release date for the reviewer. `--check` rebuilds it in memory for the locked version and exits 1 on any difference.
- `vale/styles.lock`: one line a package, `<package> <repo> <tag> <zip sha256>`.
- `vale/update-styles.sh <package> <tag>` downloads the zip, records its sha256 in `styles.lock` as the new pin for the reviewer to read, and replaces `vale/styles/<package>/` with the zip's contents plus `LICENSE` from `https://raw.githubusercontent.com/<repo>/<tag>/LICENSE`. `--check` downloads every locked zip and licence, exits 1 on a checksum mismatch or any difference from the tree, and exits 2 without `unzip`.
- `test/vale.sh [case…]`: POSIX `sh`, runs every case or the named ones. A case is a function `case_<name>`, with `-` written as `_`. `check <status> <label>` prints `ok   <case>: <label>` or `FAIL <case>: <label>`. The suite ends with `ALL PASSED` or `<n> FAILED` and exits with the failure count, like `test/run.sh`. Temp files go under `mktemp -d`, never the workspace. A skipped check fails when `GITHUB_ACTIONS=true`.

- [ ] **Step 1: Failing test `locks`.**
  - `tool.lock` holds `version 3.25.1` and exactly the four platform lines, with the Pins checksums and URLs.
  - `update-tool-lock.sh --check` exits 0.
  - `styles.lock` holds the three Pins lines, and `update-styles.sh --check` exits 0.
  - Each `vale/styles/<package>/LICENSE` exists.
  - In a temp copy of `vale/`, after one changed byte in a Google rule, `update-styles.sh --check` exits 1.

  Run `sh test/vale.sh locks`. Expected: `FAIL` (files missing).
- [ ] **Step 2: Write the two scripts** and run them to fill `tool.lock`, `styles.lock` and `vale/styles/`.
- [ ] **Step 3: Build the shared `accept.txt`** from `~/.config/vale/styles/config/vocabularies/Realliance/accept.txt`, in D7's form, sorted and unique:
  - drop personal names, and node, workstation, laptop and satellite host names;
  - spell out every pattern;
  - add the five spec words.

  Write the dropped entries to `$SCRATCH/vocab-dropped.txt` for Chris. Never commit them or put them in the ledger.
- [ ] **Step 4: Run** `sh test/vale.sh locks`. Expected: `ALL PASSED`. Then `nix shell nixpkgs#shellcheck -c shellcheck -s sh vale/*.sh test/vale.sh`. Expected: clean.
- [ ] **Step 5: Commit** signed: `vale: pin Vale 3.25.1, the three styles and the shared words`.

### Task 3: the engine

**Files:**
- Create: `vale/run.sh`, `vale/vale.ini`, `vale/output.tmpl` (D2), `vale/action.yml`
- Create: fixtures under `test/fixtures/vale/` for `clean`, `error`, `warning`, `mdx`, `extensions`, `crash`
- Edit: `test/vale.sh`

**Interfaces:**
- Consumes: Task 2's `tool.lock`, `styles/`, harness and `check`.
- Produces, `vale/run.sh [file…]`:
  - Environment: `INPUT_PATHS`, `INPUT_EXTENSIONS`, `INPUT_FAIL_LEVEL`, `INPUT_VOCAB`, each defaulting as in Global Constraints. Optional `GITHUB_OUTPUT` gets `errors=<E>` and `warnings=<W>` after a finished run. Optional `RUNNER_TEMP` (D9).
  - It finds `tool.lock`, `vale.ini`, `output.tmpl` and `styles/` beside itself.
  - Exit codes: 0 pass; 1 findings that reach `fail-level`; 2 input, refusal, tool or Vale failure.
  - Output as in D3.
- Produces, `vale/action.yml`: one composite step, `id: vale`, `shell: sh`, `run: sh "${{ github.action_path }}/run.sh"`, the four inputs as `INPUT_*` env as in `pages-publish`, and outputs `errors` and `warnings` from `steps.vale.outputs`.
- Produces, harness helpers: `mkrepo <fixture>` copies `test/fixtures/vale/<fixture>/` with `cp -R` into a temp dir, then runs `git init`, `git add -A` and a commit with a fixed identity and `GIT_CONFIG_GLOBAL=/dev/null`, and prints the path. `runvale <repo> [VAR=value…] [-- file…]` runs `run.sh` there, with output in `$T/log` and the exit code in `$rc`.

- [ ] **Step 1: Failing tests.** One case each:
  - `clean`: `rc` 0; the last line is `vale 3.25.1: errors 0, warnings 0, files 1`.
  - `error`: `rc` 1; a line starts `./docs/bad.md:` and holds `error Vale.Spelling`; `GITHUB_OUTPUT` holds `errors=1` and `warnings=0`.
  - `warning`: write the sentence so it draws warnings only (Vale 3.14.2 makes `very` a `proselint.Very` error, so avoid it). `rc` 0 by default; `rc` 1 with `INPUT_FAIL_LEVEL=warning`.
  - `mdx`: a `.mdx` file with an `import`, an `export const`, front matter, a string prop holding a misspelling, and JSX children holding another. Exactly one finding, on the JSX child's line.
  - `extensions`: `a.md`, `a.mdx`, `a.txt` and `a.html` share one misspelling. With `INPUT_EXTENSIONS="md mdx txt html"`, each draws `Vale.Spelling`. With the defaults, `files 2`.
  - `zero-files`: `INPUT_PATHS=nope` exits 2 with `vale: no files to check`.
  - `bad-input`: `INPUT_FAIL_LEVEL=suggestion` exits 2, and so does `INPUT_EXTENSIONS=rst`.
  - `crash`: a `chmod 000` file exits 2 with zero findings. If Vale 3.25.1 skips such a file instead, use an MDX parse error and ledger which.
  - `checksum`: a temp copy of `vale/` with one hex digit of the host platform's sha256 changed exits 2 with `checksum`, and leaves no cache file under the bad name (cache set up as in `cache-corrupt`).
  - `cache-corrupt`: with `RUNNER_TEMP` unset and a temp `XDG_CACHE_HOME` (so CI exercises the same cache path), run once, append a byte to the cached tarball, run again. Expected: `rc` 0, and the cached file's sha256 matches the lock again.
  - `workspace`: after `error`, `git status --porcelain --ignored` in the repo prints nothing.
  - `defaults`: the four `default:` values in `action.yml` equal `run.sh`'s fallbacks.
  - `block`: exactly one `::stop-commands::` line with 32 hex characters comes before the first finding, its closing `::<token>::` comes after the last, and the summary is the last line.

  Run `sh test/vale.sh`. Expected: these cases `FAIL`, `locks` passes.
- [ ] **Step 2: Write `run.sh`,** following spec steps 2, 4, 5, 6 and 7 and D1 to D5 and D9, except D4's quoted-path refusal; Task 4 adds spec steps 1 and 3, D6, D7, D8 and that refusal. Pass files as positional parameters, built with `set --` from the listing. `vale.ini` follows D5, with `StylesPath = styles` resolved inside the temp config dir.
- [ ] **Step 3: Run the suite.** Expected: `ALL PASSED`. If 3.25.1 prints paths unlike 3.14.2, fix the template so findings still start with `./`, and ledger it. Then `shellcheck`. Expected: clean.
- [ ] **Step 4: Commit** signed: `vale: the engine, its config, output template and action`.

### Task 4: words and hostile input

**Files:**
- Edit: `vale/run.sh`, `test/vale.sh`
- Create: fixtures `vocab`, `vocab-refused`, `vale-off`, `odd-names`, and `hostile` (D11)

**Interfaces:**
- Consumes: Task 3's `run.sh` interface and the harness helpers.
- Produces: `run.sh` with spec steps 1 and 3: the token list cleared, the D6 `PATH` set before any other command, both word lists checked (D7), and `Local` copied into the temp styles. Plus the quoted-path refusal and the D8 switch detection.

- [ ] **Step 1: Probe, ledger only.** Run Vale 3.25.1 with `--no-global`, without `env -i`. Set `VALE_CONFIG_PATH` and `VALE_STYLES_PATH` to hostile files, and put a `.vale.ini` under `$XDG_CONFIG_HOME/vale/` and in `$HOME`. Ledger which of them Vale still reads. D1 makes the answer moot for `run.sh`.
- [ ] **Step 2: Failing tests.**
  - `tokens`: the six token variables hold `tok-<random>`, and the tree is `hostile`. The token appears nowhere in `$T/log`, and the summary says `skipped 1`.
  - `hostile`: Vale checks the `::set-env` file, and its finding line starts `./::set-env`. Outside the block, no line starts with `::` or `##[`, apart from the opener and closer. Temp files named by `GITHUB_ENV` and `GITHUB_PATH` stay empty. The counts are errors 0 and warnings 1 or more, so the tree's `.vale.ini` had no effect.
  - `user-config`: hostile `HOME`, `XDG_CONFIG_HOME`, `VALE_CONFIG_PATH` and `VALE_STYLES_PATH`, each with `MinAlertLevel = error` and no styles, plus a `.vale.ini` and `.vale/styles/` inside the tree. The findings equal the `error` case's.
  - `path-hijack`: `PATH` starts with a directory of fake `git`, `curl`, `tar` and `sha256sum` that write a marker file, and `USER` is `../../../<that directory>`. No marker appears, and the findings equal the `error` case's.
  - `vocab`: a `Local` accepted word silences `Vale.Spelling` for itself. A `Local` rejected word draws an `error Vale.Avoid`. `[Jj]ujutsu` accepts both cases. With `INPUT_EXTENSIONS="md txt"`, the word lists aren't checked as prose.
  - `vocab-exempts`: an accepted word also silences a rule other than spelling. This pins Vale's word-level behavior, which the spec accepts.
  - `vocab-refused`: `.*`, `(?i)foo`, `foo|bar`, `fo+`, `[A-Z]ab`, `[Ab]c`, `\w+`, a CRLF line and a trailing space each exit 2. The message names the file and line, and `$T/log` never holds the entry. A linked `accept.txt` exits 2, and so does one missing from the index. A `.*` in the shared list, in a temp copy of `vale/`, exits 2.
  - `vale-off`: the summary names exactly `./off.md ./rule.md ./style.md ./jsx.mdx`. It skips `./plain.md`, which mentions `vale off` only in inline code.
  - `odd-names`: `with space.md`, `café.md`, `-dash.md` and `a:b.md` each draw their finding. A name with a newline, made by the test and never committed, exits 2. `INPUT_PATHS` of `../x`, `/etc` or `-x` exits 2.

  Run `sh test/vale.sh`. Expected: these cases `FAIL`, earlier ones pass.
- [ ] **Step 3: Write the changes.** Then run the suite. Expected: `ALL PASSED`. Then `shellcheck`. Expected: clean.
- [ ] **Step 4: Commit** signed: `vale: word lists, token clearing and hostile-input checks`.

### Task 5: consumer template and `lint-docs`

**Files:**
- Create: `vale/template/docs.yml`, `vale/template/lint-docs` (executable, POSIX `sh`)
- Edit: `test/vale.sh`

**Interfaces:**
- Consumes: `run.sh` (Tasks 3 and 4).
- Produces, `vale/template/docs.yml`: `on` `pull_request` and `push` to `main`, job `vale`, then the Global Constraints shape, block-style `with:`. The `uses:` line reads `uses: https://git.realliance.net/realliance/actions/vale@<40-hex commit> # <tag>`, and `paths: <content directory>`. A header comment says what to fill in and that the repo must be public.
- Produces, `scripts/lint-docs [file…]`, copied from the template:
  - It runs from the repo's top level, else exits 2.
  - It finds the pin and inputs per D10.
  - It fetches `${REALLIANCE_ACTIONS_URL:-https://git.realliance.net/realliance/actions}` at the pin with `git -c credential.helper= -c http.extraHeader= fetch --depth=1 --no-tags` and `GIT_TERMINAL_PROMPT=0`, into a temp sibling of `${XDG_CACHE_HOME:-$HOME/.cache}/realliance-actions/<commit>`.
  - It checks that `FETCH_HEAD` is the pin, else exits 2, and renames the checkout into place; if that dir exists already, it drops its own copy.
  - A final dir without `vale/run.sh` gets removed and fetched again.
  - Last, it runs `exec sh <dir>/vale/run.sh "$@"` with the inputs it read, as `INPUT_*`.

- [ ] **Step 1: Failing tests.** Each uses a temp consumer repo, a temp `XDG_CACHE_HOME`, and `REALLIANCE_ACTIONS_URL` pointing at a bare clone of the actions clone with the pin at its `HEAD`.
  - `lint-docs-pin`: the template, filled with that `HEAD` and `paths: docs`, makes `lint-docs` exit 1 on the `error` fixture. `<cache>/realliance-actions/<HEAD>/vale/run.sh` exists. The findings equal those of `INPUT_PATHS=docs sh <that run.sh>`, with the block's tokens removed.
  - `lint-docs-files`: `lint-docs docs/bad.md README.md` checks `files 1`, since `README.md` falls outside `paths`.
  - `lint-docs-inputs`: a `paths: |` block of two directories, a one-line `paths: docs notes`, and `fail-level: "warning"` each reach `run.sh` as written. `with: { paths: docs }`, two pins across two workflow files, no pin, and a run from a subdirectory each exit 2.
  - `lint-docs-cache`: a final dir lacking `vale/run.sh` gets replaced, and the run passes. A fetch whose `FETCH_HEAD` differs from the pin exits 2.
  - `template`: `docs.yml` has `runs-on: tiny` and `timeout-minutes: 10`. Its checkout step blanks all three tokens in its own `env:`. It holds no `actions/checkout` and no `pull_request_target`. The `uses:` line matches the placeholder form.

  Run `sh test/vale.sh`. Expected: these cases `FAIL`.
- [ ] **Step 2: Write both files.** Then run the suite. Expected: `ALL PASSED`. Then `shellcheck`. Expected: clean.
- [ ] **Step 3: Commit** signed: `vale: consumer workflow template and scripts/lint-docs`.

### Task 6: CI, README and the PR

**Files:**
- Create: `.forgejo/workflows/test.yml`
- Edit: `README.md`, `test/vale.sh` (a `workflow` case)

**Interfaces:**
- Consumes: everything in `vale/` and `test/`.
- Produces: the check that runs on every PR and push to `main` of `realliance/actions`.

- [ ] **Step 1: Failing test `workflow`.** In `test.yml` and the template, every `run:` step blanks `GITHUB_TOKEN`, `FORGEJO_TOKEN` and `ACTIONS_RUNTIME_TOKEN` in its own `env:`. Neither file holds `actions/checkout`, `pull_request_target`, `always()` or `continue-on-error`. Every job has `runs-on: tiny` and `timeout-minutes` of 10 or less. Run it. Expected: `FAIL` (no workflow).
- [ ] **Step 2: Write `test.yml`.** A header comment states these rules, the reason for the hostile tree, and that `uses:` steps keep the job token (D12). Triggers: `pull_request` and `push` to `main`. One job, `vale`, on `tiny`, `timeout-minutes: 10`, with these steps:
  1. The template's anonymous checkout. On a PR event, this run is the first proof that `$GITHUB_SHA` fetches.
  2. `sh test/vale.sh`.
  3. `uses: ./vale` with `paths: README.md` and `fail-level: warning`.
  4. `id: hostile`, `uses: ./vale` with `paths: test/fixtures/vale/hostile`.
  5. An assertion step, which passes `steps.hostile.outputs.errors` and `warnings` in through env. It checks that `PWNED` is unset, `$PATH` holds no `/tmp/pwned`, errors is 0, warnings is 1 or more, `git status --porcelain --ignored` prints nothing, and `RUNNER_TEMP` holds a value.

  Run `sh test/vale.sh workflow`. Expected: `ok`.
- [ ] **Step 3: Rewrite `README.md`,** brief. The sections:
  - **Pinning:** the rule and the pin form; why `@<commit>` needs checking (Task 1's `pull-head-fetchable` answer); `v1` frozen at `v1.0.0`, don't use it. Then the check, as commands: a fresh `GNUPGHOME`, `curl -fsS https://git.realliance.net/<owner>.gpg | gpg --import`, `git fetch origin tag vX.Y.Z`, `git cat-file -t vX.Y.Z` (`tag`), `git verify-tag vX.Y.Z`, and `git rev-parse 'vX.Y.Z^{commit}'`, which equals the pin. Owners today: Chris.
  - **`vale`:** what it does in two lines. Adopting it: copy `vale/template/docs.yml` and `vale/template/lint-docs`, add the hook below and `.vale/vocab/accept.txt`. Then the inputs table, the outputs, the word rule, and the fact that the summary names inline switches. The hook:

    ```yaml
    - repo: local
      hooks:
        - id: lint-docs
          name: vale
          entry: scripts/lint-docs
          language: script
          files: \.mdx?$
          require_serial: true
    ```

  - **`pages-publish`:** today's text, the `@v1` sample in the pinned form (D13), and "plus a moving `v1`" removed.
  - **Personal setup:** spec "Chris's local Vale" steps 1 to 4, written for anyone.
  - **Contributing and releases:** members open and merge PRs; `sh test/vale.sh` (and `test/run.sh` for `pages-publish`); the version policy; Owners and maintainers may push `v*` tags, consumers pin only an Owner-signed one; the monthly bump with `update-tool-lock.sh` (a release at least 14 days old) and `update-styles.sh`, where the new findings show on the bump PR.
  - **Licence:** MIT; the styles keep theirs, in `vale/styles/*/LICENSE`.

  Check it with Vale (Execution rules) and with `INPUT_PATHS=README.md INPUT_FAIL_LEVEL=warning sh vale/run.sh`. Expected: 0/0/0 and exit 0.
- [ ] **Step 4: Full local run.** Run the suite (Execution rules), then `nix shell nixpkgs#shellcheck -c shellcheck -s sh vale/*.sh vale/template/lint-docs test/vale.sh`. Expected: `ALL PASSED` and clean.
- [ ] **Step 5: Commit, push `vale`, open the PR.** Commit signed: `vale: CI on tiny and the README`. Then `fj -H git.realliance.net pr create`, base `main`, head `vale`. The body holds: a summary, D1 to D13, the case list, the probe answers, and a note that Chris reads the shared words at the release STOP. Use `Monitor` on the tasks query for the PR run. Expected: `success`. Ledger its wall time, `run_started_at` to `updated_at`; over 5 min is a finding. A failure Chris can read in the browser; fix it and push one batch.
- [ ] **Step 6: Chris merges.** Ask him to merge PR #N, or to say "merge PR #N". Then use `Monitor` for the `push` run on `main`. Expected: `success`. Ledger the `main` commit.

### Task 7: release `v1.1.0` (STOP)

**Files:** none in git; the ledger.

- [ ] **Step 1: Prepare the reading.** `git -C ~/Repos/realliance-actions-vale fetch -q origin && git log --oneline v1.0.0..origin/main` and `git diff --stat v1.0.0 origin/main`. Then the shared `accept.txt` and `$SCRATCH/vocab-dropped.txt`, and the `main` push run's `success`.
- [ ] **Step 2: STOP, ask Chris** in one AskUserQuestion. It shows the range and the words, and asks him to unlock `gpg` so the session signs `v1.1.0` on `<main commit>` and pushes it. Options: *unlocked: sign and push*, *change the words first*, *hold*. If he changes the words: a fix PR, he merges, back to Step 1.
- [ ] **Step 3: Sign and push.** `git tag -s -a -u E9245FED84E0EC721C7B5C336BFB8037115ADE26 -m "v1.1.0: the vale action" v1.1.0 <main commit>`, then `git push origin v1.1.0`.
- [ ] **Step 4: Check it as a consumer would,** with the README's commands in a fresh `GNUPGHOME`. Expected: `VALIDSIG E9245FED84E0EC721C7B5C336BFB8037115ADE26`, type `tag`, `^{commit}` = `<main commit>`, and `v1`^{commit} still `37a3a4b`. Ledger the pin line `uses: https://git.realliance.net/realliance/actions/vale@<main commit> # v1.1.0`.
- [ ] **Step 5: Pin smoke.** In a worktree on a new branch `pin-smoke` from `main` (as in Task 1), `.forgejo/workflows/docs.yml` is the template filled with the pin, `paths: README.md`, and `push: branches: [pin-smoke]` as its trigger. Push, then use `Monitor`. Expected: `success`, the job under a minute. Delete the branch. Ledger the timing.

### Task 8: luma-homeops docs and hand-off

**Files:** edit `docs/pages.md` (its section on the recommended `pages-publish` action, lines 41 to 51 today); the ledger.

- [ ] **Step 1: Edit `docs/pages.md`.** `git pull --ff-only` first. The sample becomes `pages-publish@<v1.1.0 commit> # v1.1.0`. The paragraph says: pin the commit of an Owner-signed `v*` tag, never a branch or a moving tag; the actions README lists the check; Owners and maintainers may push `v*` tags, and consumers pin only an Owner-signed one. Check with Vale: 0/0/0. `grep -n '@v1\b' docs/pages.md` prints nothing.
- [ ] **Step 2: Commit and push.** `nix develop --command git commit -S` with the subject `docs/pages: pin pages-publish by Owner-signed commit`, then push `main`.
- [ ] **Step 3: Hand-off entry** in the ledger, for the docs-site plan:
  - the pin line;
  - `vale/template/docs.yml`, `vale/template/lint-docs` and the README's hook;
  - an empty `.vale/vocab/`;
  - a `paths:` list in either D4 form;
  - the open questions from spec Hand-off step 2, the check's context string on Forgejo 15 and protected file patterns for `.forgejo/`.

  Also: no known consumer of `v1` remains, so an Owner may delete it when Chris chooses. That's his call, not a step here.

### Task 9: the personal switch (STOP, phase 2)

**Files:** none in git. Chris's `~/.config/vale/`, which only he changes, or the session on his word.

- [ ] **Step 1: Prepare.** `$SCRATCH/Personal-accept.txt` = `grep -vxF -f <shared accept.txt> <his accept.txt>`. Pattern entries the shared list spelled out stay; the overlap does no harm. Then a checked copy of `vale/styles/` from `v1.1.0` (Task 7 Step 4's commands) in `$SCRATCH/styles-v1.1.0/`.
- [ ] **Step 2: STOP, ask Chris** to run the README's Personal setup, or to let the session run it:
  1. Move `~/.config/vale/styles/config/vocabularies/Realliance` to `Personal` beside it, and replace its `accept.txt` with `$SCRATCH/Personal-accept.txt`.
  2. Copy `$SCRATCH/styles-v1.1.0/` over `~/.config/vale/styles/`, leaving `Personal/` alone.
  3. In `.vale.ini`: the shared settings, `Vocab = Realliance, Personal`, no `Packages` line, and his `[*.{md,txt,rst,html}]` glob kept.
- [ ] **Step 3: Check, read-only.** From inside luma-homeops, `nix shell nixpkgs#vale -c vale ls-config` shows both vocabularies and no packages. `diff -r ~/.config/vale/styles/Google $SCRATCH/styles-v1.1.0/Google` prints nothing. A file holding one shared word and one `Personal` word draws 0/0/0. Ledger it: phase 2 done.
