# Realliance shared actions, Vale first: design

- **Date:** 2026-11-07.
- **Status:** draft for Chris's review. Nothing built.
- **Research:** Claude's shared-actions report of 2026-11-07 (session scratchpad, not kept). It read
  the Forgejo v15.0.11 source the forge runs and the copy of `act` in its runner. Constraints restates
  the facts this design rests on.
- **Related:** runner classes spec `2026-11-04-runner-classes-design.md` (`tiny`); Pages spec
  `2026-11-03-realliance-pages-design.md` (`pages-publish`); `docs/forgejo.md`; `docs/ci.md`.
- **Markers:** *(Chris decided)* marks a decision Chris made on 2026-11-07. *(Claude's call)* marks a
  default this doc chose; Chris may override any of them.

## Design summary

| Piece | Design | Marker |
|---|---|---|
| Start | Shared pipeline tasks start now, with Vale first | *(Chris decided)* |
| Gate | The docs repo's `main` requires the Vale check from the first PR. Errors fail it; warnings print. Rules as in Chris's `~/.config/vale/.vale.ini`: Vale, Google, write-good, `proselint` | *(Chris decided)* |
| Docs repo | Public on the forge. Outsiders open fork PRs, and a member approves each fork run | *(Chris decided)* |
| Mechanism | Composite actions in `realliance/actions`, one directory each; no reusable workflows yet | *(Claude's call)* |
| Pin | Full URL, a 40-hex commit, a `# vX.Y.Z` comment. The commit must be an Owner-signed `v*` tag's; no moving `v1` | *(Claude's call)* |
| Tags | Only Owners push `v*` tags on `realliance/actions`; one version line for the whole repo | *(Claude's call)* |
| Bumps | Renovate on the forge opens pin PRs. Vale moves monthly, to releases at least 14 days old | *(Claude's call)* |
| Repo config | Repos add vocabulary words, as literal words only; they can't switch rules off | *(Claude's call)* |
| Runs on | `tiny`, every token cleared, Vale 3.25.1 checked by checksum, styles in git, `--no-global`, `stop-commands`, regular files only, `scripts/lint-docs` for local runs | *(Claude's call)* |
| Chris's Vale | `~/.config/vale` moves to the shared styles and keeps a personal vocabulary; his step | *(Claude's call)* |
| Backlog | `checkout`, `fetch-tool`, `workflow-lint`, `go-ci`, then `oci-publish`; only `vale` gets built now | *(Claude's call)* |

## Intent

Every Realliance repo needs the same few pipeline steps: prose lint, an anonymous checkout, a checked
tool download, Go checks, an image push. Today each repo copies them, and the copies drift: two
`ci-tools.sh` files already differ. Each copy also re-derives the security rules (anonymous fetch,
blank tokens, checksum-verified tools), and each re-derivation can miss one. A shared home lets one
reviewed fix reach every repo through one reviewed pin bump.

Vale goes first *(Chris decided)*. Realliance is starting a public docs site, and Chris wants the prose
standard he applies to his own writing on everything Realliance publishes, from the first PR. Outsiders
may open fork PRs there *(Chris decided)*, so the lint job reads untrusted text in a job that holds a
token. The first shared action is a security boundary, not a convenience.

Pins also make rule changes cheap to absorb. A new Vale release or style tag shows its new findings on
the pin-bump PR, where someone expects them, not on an unrelated docs PR the next morning.

## Goals

1. A repo adopts the gate with one job, and gets the same findings in CI, in a pre-commit hook, and
   from `scripts/lint-docs`, for the same pin.
2. From the docs repo's first content PR, nothing merges to `main` with a Vale error. Warnings show in
   the job log and never block *(Chris decided)*.
3. One pin selects the action, the Vale binary, the styles and the vocabulary. Nothing unpinned loads at
   run time.
4. An approved fork PR that changes only content can't reach a token, change the lint config, or inject
   workflow commands through the Vale step.
5. Vale, the styles and the pins stay current without anyone watching release feeds.
6. Chris's personal lint and Realliance CI apply the same rules and the same shared vocabulary.

## Non-goals

- Reusable workflows *(Claude's call)*, until a whole job, with its class and triggers, should match
  across three or more repos. The Go CI shared by forge-bot and ci-dispatch is the likely first case.
- Building any shared action besides `vale` now (Follow-ups).
- PR comments, annotations or job summaries: Forgejo 15 shows no annotations, and a comment needs a
  write token in a job that reads untrusted text.
- A changed-files mode *(Claude's call)*. The new site is small, and a whole-tree gate keeps `main` clean
  when rules change. Add it when a large existing tree adopts Vale.
- A report-only trial period *(Chris decided)*.
- Formats that make Vale call an outside program: reStructuredText, AsciiDoc, XML.

## Anti-goals

Any of these makes the design a failure, even if the lint runs:

- A pin that can change without review: a moving tag, a branch head, or an untagged commit.
- A repo that can loosen the shared config or switch a rule off.
- A token or secret that Vale, or anything it parses, can read; a token printed in a public log.
- CI and `scripts/lint-docs` disagreeing for the same pin. People stop trusting the gate and add words
  until it turns green.
- A gate noisy enough that contributors pad the vocabulary with misspellings or wrap ordinary prose in
  `vale off`.
- A fork PR run that starts without a member's approval.
- Renovate merging a pin by itself, or reading its rules from a branch any member can change.
- Personal names or home network host names in the public shared vocabulary.
- Per-job downloads from the forge's packages or release assets, which B2 serves against its daily cap.

## Constraints

| Fact | The design |
|---|---|
| A reusable workflow can't read its own repo's files at its pinned ref (no `job_workflow_sha`). The server expands only the short form, without `runs-on`, in a public owner, never a full URL (`services/actions/reusable_workflows.go`) | composite actions, checked out at the pin |
| The forge's `DEFAULT_ACTIONS_URL` is GitHub, so a short `uses:` resolves there | full URLs only |
| `@ref` resolves any object in the repo, fork PR heads included, and anyone can fetch it (`uploadpack.allowAnySHA1InWant`) | a reviewer checks each pin against its tag |
| A push or same-repo PR job token can write every unit of its repo; a fork PR's reads. `ACTIONS_RUNTIME_TOKEN` counts too. `act` sets `env:` again on composite steps | `run.sh` clears every token itself |
| The runner treats `::` log lines as workflow commands and shows no annotations | findings print between `stop-commands` tokens |
| A fork PR run waits for approval unless Forgejo trusts its poster; `realliance` members have write on every `realliance` repo | members approve fork runs |
| `tiny`: 1 CPU, 2 GiB, 15 min, egress on 80 and 443, a fresh pod per job; per owner, one job at a time and 120 minutes a UTC day | the docs job finishes in under a minute |
| Packages and release assets come from B2 against a daily cap; git objects don't | styles in git; Vale from GitHub releases |
| `nixpkgs` has Vale 3.14.2; native MDX needs 3.18, JSX 3.19; 3.25.1 (2026-10-22) fixes lost MDX alerts; 3.26.0 turns 14 days old on 2026-11-14 | pin 3.25.1, downloaded and checked |
| Without `--no-global`, Vale merges the user-level `.vale.ini` underneath | `--no-global`, empty `XDG_CONFIG_HOME` |
| Tag protection, read 2026-11-07: `v*` for teams `Owners` and `maintainers`. Maintainers administer every `realliance` repo and its settings | protection stops members; the signer check stops maintainers |

## Versioning and trust

**One version line.** Tags are `vX.Y.Z` across `realliance/actions`, so Renovate tracks one tag
stream. Major: an input or output removed or changed in meaning. Minor: a new action or input, or any
change to Vale, the styles, or the shared words. Patch: a fix that changes no interface. `vale` ships as
`v1.1.0`. A consumer writes:

```yaml
uses: https://git.realliance.net/realliance/actions/vale@<40-hex commit> # v1.1.0
```

**A valid pin** is the `^{commit}` of an annotated `v*` tag with a good signature from a key that a
member of the `realliance` Owners team publishes on the forge (`/<user>.gpg`, which only that user can
change). The pin-bump reviewer checks all three, with commands the README lists (`git verify-tag`,
`git rev-parse vX.Y.Z^{commit}`). A key list kept in `realliance/actions` wouldn't do: any member can
merge to its `main`.

**Releases.** A PR to `main`, merged by any member. An Owner reads the range since the last tag, then
pushes a signed, annotated tag. Forgejo checks no tag signature; consumers do.

**Tag rights.** `docs/forgejo.md` says Owners and maintainers may tag; `docs/pages.md` and the actions
README say Owners only. The forge matches `docs/forgejo.md`. Land these together:

1. An Owner sets the `v*` protection (id 6) to team `Owners` alone, by API `PATCH` or repo settings.
2. In `realliance/sso`, `tests/forge/test_forge_inventory.py` asserts `{Owners, maintainers}` for
   `brand` and `actions` (`test_release_tags_limited_to_owners_and_maintainers`). Split it: `actions`
   expects `{Owners}`, and `brand` stays.
3. `docs/forgejo.md`: `actions` is Owners-only; `brand` and `forgejo` keep both teams. `docs/pages.md`
   keeps "Owners-only" and drops the `@v1` sample. The actions README drops "plus a moving `v1`" and
   shows the pinned form.

A maintainer, as repo administrator, can still edit the protection. The signer rule binds them, and the
SSO test reports the change on its next run. Moving the repo into `realliance-ops` would close the gap
at the cost of member PRs; not proposed.

**The `v1` tag** stays at `v1.0.0`'s commit and never moves. An Owner deletes it once no known consumer
uses it; the two doc samples are the only local ones.

## The `vale` action

### Files in `realliance/actions`

| Path | Holds |
|---|---|
| `vale/action.yml` | one composite step running `run.sh`, inputs as `INPUT_*` env, like `pages-publish` |
| `vale/run.sh` | the engine, shared by CI and local runs |
| `vale/vale.ini` | `BasedOnStyles = Vale, Google, write-good, proselint`, `write-good.E-Prime = NO`, `Google.Units = NO`, `MinAlertLevel = warning`; sections for `md` and `mdx` |
| `vale/tool.lock`, `update-tool-lock.sh` | Vale 3.25.1's URL and checksum for Linux and macOS on amd64 and arm64, written from the release's checksums file |
| `vale/styles/` | Google, write-good and `proselint` at pinned tags, with their licences, about 380 KB |
| `vale/styles/config/vocabularies/Realliance/` | the shared `accept.txt` and `reject.txt` |
| `vale/styles.lock`, `update-styles.sh` | each package's repo, tag and zip checksum; the re-copy tool |
| `vale/output.tmpl` | `path:line:col: level Rule message` |
| `test/vale.sh`, `test/fixtures/vale/` | the tests |

Nothing runs `vale sync`: its `Packages` fetch latest-release URLs and pin nothing.

### Vocabulary

The shared `Realliance` list starts from Chris's personal `accept.txt`, about 145 entries. The builder
drops personal names and home network host names, since the repo is public, and adds `[Jj]ujutsu`,
`MDX`, `JSX`, `OpenTofu`, `git-pages`. Chris reads the list before he tags `v1.1.0`.

A consumer adds words in `.vale/vocab/accept.txt` and `reject.txt`, loaded as a second vocabulary,
`Local`. Each entry is a literal word, optionally with a one-letter case class such as `[Jj]ujutsu`.
`run.sh` refuses any other pattern syntax, so one entry can't accept everything. A word may also exempt
itself from rules besides spelling; that's word-level and visible in review, so accepted, and the tests
pin it.

Inline `<!-- vale off -->` and `{/* vale off */}` stay allowed for quoted bad examples. The summary
line names each file that uses one.

### Interface

| Input | Default | Meaning |
|---|---|---|
| `paths` | `.` | Directories or files, one per line |
| `extensions` | `md mdx` | File types, from an allowlist of formats Vale parses itself |
| `fail-level` | `error` | `error`, or `warning` for a stricter repo. A Vale crash (exit 2) always fails |
| `vocab` | `.vale/vocab` | The repo's own word lists |

Outputs: `errors`, `warnings`. There's no `token`, `config`, `version` or level-floor input: one pin
selects the action, Vale, the styles and the shared words, and the floor stays at `warning`.

### What `run.sh` does

1. Clears `GITHUB_TOKEN`, `FORGEJO_TOKEN`, `GITEA_TOKEN`, `ACTIONS_RUNTIME_TOKEN` and
   `ACTIONS_ID_TOKEN_REQUEST_*`, and sets `PATH` to the system directories plus its tool directory.
2. Downloads Vale to `$RUNNER_TEMP`, or `~/.cache/realliance-actions/` locally, and checks its checksum
   before extracting. Nothing lands in the workspace.
3. Validates the repo's vocabulary, then builds a temporary styles directory from `styles/` and it.
4. Lists files with `git ls-files -s`, keeping modes 100644 and 100755 only (no symbolic links, no
   submodules), filters by extension, and passes `./`-prefixed paths.
5. Runs `vale --no-global --config=<tmp>/vale.ini` with an empty `XDG_CONFIG_HOME`. It never reads the
   repo's `.vale.ini`.
6. Prints findings through `output.tmpl` between `::stop-commands::<random>` and its closing token,
   then one summary line: counts, files checked, files with `vale off`.
7. Exits by `fail-level`.

### The docs repo's job

The docs repo doesn't exist yet; its name, site generator and content path belong to the docs project.
Its first commit carries this job. Before the first content PR, a maintainer makes `main` PR-only with
the Vale check required.

```yaml
on: { pull_request: {}, push: { branches: [main] } }
jobs:
  vale:
    runs-on: tiny
    timeout-minutes: 10
    steps:
      - name: Check out, anonymously
        env: { GITHUB_TOKEN: "", FORGEJO_TOKEN: "", ACTIONS_RUNTIME_TOKEN: "" }
        run: |
          git init -q . && git fetch -q --depth=1 --no-tags "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY" "$GITHUB_SHA"
          git checkout -q --detach FETCH_HEAD
      - uses: https://git.realliance.net/realliance/actions/vale@<40-hex commit> # v1.1.0
        with: { paths: <content directory> }
```

No `actions/checkout`, which writes the token into `.git/config`, and never `pull_request_target`.

### Local entry point

`scripts/lint-docs [files…]`, a template in the README, reads the `vale@<commit>` pin from the
workflow, fetches `realliance/actions` at that commit once, anonymously, into
`~/.cache/realliance-actions/<commit>`, and runs its `vale/run.sh`. A `repo: local` pre-commit hook
passes it the staged `.md` and `.mdx` files; an npm script may wrap it. The workflow's pin serves all
three entry points, where a hook pointing at `realliance/actions` would add a second pin that drifts.

### Tests

`test/vale.sh`: a clean tree; an error; a warning-only tree that passes; MDX imports and JSX; a symbolic
link skipped; a fake token absent from the output; a `::` filename with no effect; a hostile user-level
config ignored; a refused vocabulary pattern; a `vale off` file named. A `realliance/actions` workflow
runs it on `tiny` for every PR and push, through the real runner, with the same token blanks.

## Security of the Vale job

On a push or same-repo PR, the job token can push unprotected refs, write issues, releases and the wiki,
and dispatch a workflow that reads the repo's secrets. Vale needs none of it. Only the runner process
uses it, to clone the action.

| Attack in a content-only PR | Stopped by |
|---|---|
| A symbolic link to `/proc/self/environ`, so `Vale.Spelling` echoes a token into the public log | regular files only; tokens cleared first |
| A filename or line starting `::` that runs `add-path` or `set-env` for later steps | `stop-commands`; the `./` prefix |
| The PR's own `.vale.ini`, styles or `Packages` | fixed config, `--no-global`, no sync |
| A format that runs a program from `PATH` | the extension allowlist, a fixed `PATH`, tools outside the workspace |
| A parser crash or an oversize file | exit 2, or an out-of-memory stop in a 2 GiB pod: the check fails |

A fork PR can also edit the workflow. A dropped job leaves the required check missing, so the PR can't
merge; the approving member and the merging reviewer both read the `.forgejo/` diff. An approved run on
`tiny` still runs the PR's code with internet access.

## Pin bumps: Renovate on the forge

| Item | Design *(Claude's call)* |
|---|---|
| Where | a CronJob on galaxy, Argo-managed in `cluster/renovate-forge/`, with small requests: galaxy sits at 84 to 89 % of memory requested |
| Account | a bot declared in `realliance-ops/forge` beside forge-bot and ci-dispatch; its token a SealedSecret. A read-only GitHub token for release queries is Chris's step |
| Repos | a repo opts in by adding the bot as a write collaborator; `requireConfig` on |
| Rules | in its global config in luma-homeops, never a shared preset on a member-run branch |
| `realliance/actions` pins | the `github-actions` manager on `.forgejo/workflows`; tag updates only, digest updates off, so no PR proposes a branch head; never automerged, since a person checks the signer |
| Vale | a custom manager on `vale/tool.lock`, from `vale-cli/vale` releases, `minimumReleaseAge` 14 days, scheduled monthly. The PR also rewrites every checksum through `update-tool-lock.sh`; a stale checksum fails the download in CI |
| Styles | the same schedule and age rule on `styles.lock`, re-copied by `update-styles.sh` |

Until the bot runs, members bump pins by hand in a PR, with the same review.

## Chris's local Vale (his step)

The actions README gets a short "Personal setup" section. After `v1.1.0`, Chris:

1. Renames his personal vocabulary from `Realliance` to `Personal`, keeping only the words the shared
   list dropped.
2. Copies `vale/styles/` from a verified tag over `~/.config/vale/styles/`, leaving
   `config/vocabularies/Personal/` alone.
3. Edits `~/.config/vale/.vale.ini`: the shared settings, `Vocab = Realliance, Personal`, and no
   `Packages` line, so a stray `vale sync` can't overwrite the copied styles. His wider file glob stays.
4. Repeats steps 2 and 3 for each release he adopts.

The config path stays the same, so instructions that name it keep working. His `nixpkgs` Vale lags the
pin, so ad-hoc runs apply the same rules but can differ at the edges, chiefly in MDX. Exact CI results
come from `scripts/lint-docs`.

## Phasing

| Phase | Delivers | Done when |
|---|---|---|
| 1 | the `vale` action, tests and CI; tag rights, SSO test and doc fixes; `v1.1.0` | CI green on `tiny`; the tag verifies against an Owner key; the protection lists `Owners` alone; the SSO suite passes |
| 2 | the docs repo's job, `scripts/lint-docs`, hook, `.vale/vocab`, branch protection | an error blocks a merge; warnings alone don't; a fork PR waits for approval; the job takes under a minute |
| 3 | Renovate on the forge | a tag-based pin PR on the docs repo; a Vale PR that respects the age rule and schedule |
| 4 | Chris's local switch | his config loads `Realliance` and `Personal` from the shared styles |

## Left to the builder

- The required check's context string on Forgejo 15, and whether protected file patterns can force
  review of `.forgejo/` changes.
- The extension allowlist, checked against Vale 3.25.1's formats; the `mdx` section of `vale.ini`.
- The temporary styles layout and the vocabulary rule's exact form.
- Renovate's Forgejo platform and version; the `hostRules` that mark `git.realliance.net` as Forgejo;
  whether `github-actions` writes commit plus comment for a full-URL forge action; whether checksums
  come from a post-upgrade task or a member; the CronJob's schedule and resources.
- Unverified in the research, so test it: whether `act` 13.2.1 honours `env:` blanks in a composite;
  whether its clone fetches a `refs/pull/*` commit; whether `set-env` still works; what `--no-global`
  drops besides the user file.

## Follow-ups

Out of scope now, in this order *(Claude's call)*:

1. `checkout`: an anonymous fetch at `$GITHUB_SHA`, replacing seven copies, two of which leave the token in `.git/config`.
2. `fetch-tool`: a checksum-verified download from a lock file, replacing two drifted `ci-tools.sh` copies and Vale's own.
3. `workflow-lint`: full-URL pins, Owner-signed tags, token blanks, no `always()` or `continue-on-error`; it automates the pin review.
4. `go-ci`: module verify, tidy, vet, race tests and `govulncheck` for forge-bot and ci-dispatch; the first reusable-workflow case.
5. `oci-publish`, last: a push that never moves a tag and reads the digest back; it holds `REGISTRY_TOKEN`, so its own tests.

Separately: on Forgejo 15, a same-repo PR's job token can write the whole repo. Audit the other repos'
workflows for steps that leave it in place.
