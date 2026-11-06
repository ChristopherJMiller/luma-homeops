# Fork image: build on `medium`, deploy to the forge (research, 2026-11-06)

Research only, for the implementation plan. This research changed nothing. Abbreviations: **F** = `~/Repos/realliance-forgejo`
(read-only), **U** = upstream Forgejo at the pin `753f7ed` (v15.0.11), read with `git show 753f7ed:<path>`,
**H** = `~/Repos/luma-homeops`, **C** = forgejo-helm chart 17.3.2 (`helm pull`, scratch copy).

## Summary

- The build fits easily: about 11 to 16 minutes cold on `medium` (4 vCPU, 16 GiB), peak about 4 GiB, about $0.06 a build.
- CI can't use the fork's `make build`, which wraps `nix develop`. The class image has no Nix, and a Nix-shell binary links a `/nix/store` glibc.
- Use `make check` and `make apply`, then upstream's own `make frontend backend`, with the image's tags, `CGO_ENABLED=0` and an explicit version.
- Image: `crane append` one layer holding `/app/gitea/gitea` onto the pinned upstream **rootless** image. The binary embeds every template and asset; this mirrors realliance.net's `build-image.sh`.
- Push as `realliance-ci` with a **second, named token** in a new repo secret. Deploy via chart `image.fullOverride`, digest-pinned by hand. No Renovate.
- **The bootstrap risk is bigger than a reschedule.** With `strategy: Recreate`, *every* rollout of a forge-hosted image pulls while Forgejo is down. A cold node can never start it. This needs a pull-before-stop step; see 4d. It conflicts with "no pre-pulling", so Chris must decide.
- Fallback to upstream is one deleted line (`fullOverride`), and it is data-safe: no patch adds a migration, model, queue or cron.
- Coordination: the other session is at Task 5 of the CI-usage plan (#16), making 10 patches, one of which changes `navbar.tmpl`. The live custom `navbar.tmpl` override would **shadow** that change. The live `ci-dispatch` and the `v2` branch both lack the `blocked` handling the 7th patch needs.

---

## 1. Build recipe

**Answer.**

- *Source tree.* `make check` applies the series to a scratch worktree of the pin and checks that `patches/` matches what export writes. `make apply` inits `upstream/` as a shallow, blobless clone from Codeberg, checks out branch `realliance` at the gitlink, and `git am -3`s `patches/series` in order. Main has 7 patches today and will have 10 after the other session's PR. `git am` needs a committer identity in CI; `check` sets its own, but `apply` does not.
- *Build.* Call upstream's Makefile directly, matching `Dockerfile.rootless` minus Docker/xx:
  `npm ci` → `make frontend` (webpack to `public/assets`) → `make backend`, with `TAGS="bindata timetzdata"`, `CGO_ENABLED=0`, `FORGEJO_VERSION=15.0.11-realliance.N+gitea-1.22.0`, `GOFLAGS=-trimpath`, `LDFLAGS=-buildid=` and `FORGEJO_GENERATE_SKIP_HASH=true` (pass them on the make command line, as the Dockerfile does: the Makefile assigns `GOFLAGS :=`).
  - `bindata` embeds `templates/`, `public/` (with the webpack output), `options/` (locales) and migration templates into the binary.
  - `timetzdata` is required: the Alpine rootless image ships no `tzdata`. The fork Makefile's default `TAGS` omits it.
  - Set the version explicitly. In CI, `git describe` depends on tags that a shallow submodule fetch may lack, and on `git am` commit IDs that change every run. Forgejo derives `AssetVersion`, the `?v=` cache-buster on every asset URL, from the version. A fork build reporting upstream's `15.0.11+gitea-1.22.0` would serve stale upstream JS/CSS from browser caches, up to 6 h.
- *CGO.* Upstream's binary is static, `CGO_ENABLED=1` with `sqlite` tags, built on musl. On the Debian class image either:
  - **(recommended)** `CGO_ENABLED=0`, no sqlite: a pure-Go static binary. The forge runs Postgres, so nothing is lost. It needs no libc, and it is the plan's own note.
  - or `make static-executable` (glibc `-static`, `netgo osusergo`). That is how upstream's own release binaries are built (xgo), and it keeps the sqlite parity. It also brings glibc static-link warnings.
- *Toolchains.*
  - Go: `go.mod` asks for `go 1.26.0` and `toolchain go1.26.10`, and the upstream 15.0.11 binary was built with **go1.26.10**. The class image has Go 1.26.9 without `GOTOOLCHAIN=local`, so the default `auto` downloads go1.26.10, about 70 MB. Set `GOTOOLCHAIN=go1.26.10` to make that explicit.
  - Node: upstream wants Node ≥ 20 **and npm**; `node-check` fails without npm. The class image copies only the `node` binary (24.21.0): no npm, no npx. The job fetches a pinned, checksum-verified Node 24.21.0 tarball for npm.
  - Also needed: crane, which the job fetches the way realliance.net's `ci-tools.sh` does. git, make, curl and tar are present.
- *Prebuilt frontend? No.* The queue changes `web_src/css/repo.css` and `web_src/css/features/gitgraph.css` today, and the other session's commit `61c7677`/`b4e8a0d` adds `RepoActionView.vue`. The frontend has to be built from the patched tree.
- *Time and memory on `medium`.*
  - Plan estimate: 9 to 13 min. Add about 2 to 3 min for the tool and toolchain downloads, the Codeberg clone (about 47 MB), `make check` and the push: **about 11 to 16 min**, against the 45 min runner timeout. The 75 % rule (34 min) leaves a wide margin.
  - Peak memory: the plan's 3 to 4 GiB (webpack heap, Go link of a ~117 MB binary) of 16 GiB.
  - Disk: about 0.65 GB of `node_modules` (measured locally) plus the Go module cache and build cache, about 4 to 6 GB, against about 47 GB free.
  - Cost: $0.233/h × about 0.25 h ≈ **$0.06**. Each launch reserves $0.2136 in the ledger until it settles. That is far under `realliance`'s $30.
- *Fallbacks if it doesn't fit* (unlikely): `NODE_OPTIONS=--max-old-space-size=6144`, `GOFLAGS=-p=2`. Last resort: split jobs, webpack on `small` and the Go build on `medium` with `public/assets` passed as an artifact. A prebuilt upstream frontend is ruled out, see above.
- *The flake* builds nothing useful for CI: it has only `devShells` (default, and `build` with unpinned nixpkgs `go`/`nodejs`). The local `upstream/gitea` from that shell is "dynamically linked, interpreter /nix/store/…glibc-2.44…/ld-linux-x86-64.so.2". It would not start on Alpine.

**Evidence.**

| Claim | Where |
|---|---|
| `make build` wraps `nix develop`; default TAGS has no `timetzdata` | F `Makefile:5-6`, `:29-30` |
| apply/check internals; `am` without identity | F `scripts/patches:60-68` (shallow blobless init), `:123-135` (`build_on`, `am -3` at `:131`), `:210-237` (`check` sets `-c user.*` at `:225`) |
| pin and source | F `.gitmodules:4-9` (Codeberg, `shallow`, `tag = v15.0.11`); `git ls-tree HEAD upstream` = `753f7ed…` |
| flake shells only | F `flake.nix:18-39` |
| upstream recipe | U `Dockerfile.rootless:3` (golang:1.26-alpine3.23), `:10` (`TAGS=bindata timetzdata …`), `:31` (nodejs npm), `:36-39` (clean, frontend, generate-backend, static-executable) |
| no tzdata in the runtime image | U `Dockerfile.rootless:67-76` |
| version and ldflags | U `Makefile:85-100`, `:115`, `:260-265` (`verify-version`); U `modules/setting/server.go:270` (`AssetVersion` from `AppVer`) |
| bindata set; build targets | U `Makefile:129`, `:847-853`, `:871-878`, `:890-891` (release = xgo, glibc `-static`) |
| npm required; Go min | U `Makefile:305-312`, `:322-330`; U `go.mod:3,5`; U `package.json:4-6` |
| upstream binary facts | `go version -m` on layer `ee041d49…`: go1.26.10, `CGO_ENABLED=1`, tags `netgo,osusergo,bindata,timetzdata,sqlite,sqlite_unlock_notify`, `vcs.revision=753f7ed`, `Version=15.0.11+gitea-1.22.0`, statically linked, 116,633,816 bytes |
| class image | H `images/forgejo-runner-class/Dockerfile:6` (no Docker, no Nix), `:12-13` (Go 1.26.9, Node 24.21.0), `:20` (git gcc libc6-dev make curl unzip), `:24-25` (node binary only); `runner-medium.yml:8` (45m) |
| shape, price, estimate | H `docs/superpowers/plans/2026-11-05-runner-classes-phase-2.md:107` (4 vCPU/16 GiB, 55 min hard stop), `:124` (reservation), `:276-289` (estimate, "CGO_ENABLED=0" note, 4 to 6 GB disk), `:1338` ($30, ledger the first build); `~/Repos/ci-dispatch-v2/internal/prices/prices.go:58-65` (medium floor 233_000 µ$/h) |
| free disk | H `…phase-2.ledger.md:227` (47G free, `small`) |
| frontend touched | F `patches/*` diff headers (`web_src/css/…`); `git -C F/upstream show --stat 61c7677` (`RepoActionView.vue`) |

**Open risks.**

- `medium` is still `off` (H `cluster/ci-dispatch/dispatcher/config.yaml:28`). The first build waits for phase 2 Task 21.
- The build depends on codeberg.org (source), registry.npmjs.org, proxy.golang.org, nodejs.org and code.forgejo.org (base) all being up. A pull mirror of upstream on the forge is a later hardening.
- `npm ci` and module downloads run cold every time, because the CI cache is designed but not built.

## 2. Image without Docker

**Answer.**

- Upstream layout, amd64 child `sha256:ab0b8fd8…` of the pinned index `sha256:3cd6bc2b…`:
  - Alpine 3.23.7; user `1000:1000`; entrypoint `dumb-init -- docker-entrypoint.sh`; `GITEA_*` environment; volume `/var/lib/gitea`.
  - The server binary is **its own layer**: `/app/gitea/gitea`, root:root 0755, 46.7 MB compressed.
  - `/usr/local/bin/gitea` (a wrapper script) and `forgejo-cli` exec it.
  - Every other layer is base OS, scripts, symlinks, `environment-to-ini` (8.8 MB) and bash completion. **No on-disk templates or assets**: with `bindata` they are all inside the binary.
- Live runs the **rootless** variant, pinned by the index digest. That index is identical on `code.forgejo.org`, `data.forgejo.org` and `codeberg.org`.
- Recommendation: realliance.net's `scripts/build-image.sh`, nearly verbatim.
  1. Make one deterministic tar holding `app/gitea/gitea` (0755, uid/gid 0, sorted, `--mtime` = the commit time).
  2. Start a job-local `crane registry serve`.
  3. `crane copy --platform linux/amd64 code.forgejo.org/forgejo/forgejo@sha256:3cd6bc2b…` into it.
  4. `crane append --platform linux/amd64 -b <base@digest> -f layer.tar`. Append keeps the user, env, entrypoint and workdir.
  5. `crane mutate` the labels (and annotations) the base sets: `org.opencontainers.image.source` = the fork repo, `version` = `15.0.11-realliance.N`, `revision` = the fork commit, `base.name`/`base.digest` = the upstream pin.
  6. `crane pull --format=oci`, record the digest, then push.
- Size: about 77 MB base + about 47 MB new layer ≈ 124 MB per cold node pull. The old binary stays as dead weight in a lower layer. Optional later: a ~30-line go-containerregistry program that *replaces* layer 8 instead of appending, saving 47 MB.
- `ko` is in the class image but wrong here: it puts the binary at `/ko-app/…` and rewrites the entrypoint.
- Smoke tests: run the built binary (`--version`) in the job. It is static, so it runs on Debian. Then check `go version -m` for tags, `CGO_ENABLED=0` and the version, and `file` for "statically linked".
- Tools: crane (fetched, pinned sha256; realliance.net pins v0.22.1), Go (present, auto-switches to 1.26.10), Node + npm (fetched), plus GNU tar and git, which are present.

**Evidence.** `crane manifest`/`crane config` (anonymous) of `…@sha256:ab0b8fd8…`: 12 layers; history shows `COPY --chown=root:root /go/src/forgejo.org/gitea /app/gitea/gitea` = layer `ee041d49…` (46,744,019 B). `crane digest code.forgejo.org/forgejo/forgejo:15.0.11-rootless` = `sha256:3cd6bc2b…`. U `Dockerfile.rootless:92-111`; U `docker/rootless/usr/local/bin/gitea` (`GITEA="/app/gitea/gitea"`). H `cluster/applications/forgejo-release.yaml:38-41` (rootless + digest). realliance.net `scripts/build-image.sh:52-53` (tar), `:56-70` (local registry, copy, append), `:76-87` (mutate overrides), `:91-95` (pull to OCI, digest); `scripts/ci-tools.sh:14-15` (crane pin).

**Open risks.** Reproducibility (same digest on rebuild) needs a fixed version string, `-trimpath`, `-buildid=`, `npm ci` and the same Go. Verify it once by building twice. It matters because any member can delete a package version (H `docs/forgejo.md:282`); a non-reproducible rebuild means a re-pin.

## 3. Push and auth

**Answer.**

- Reuse realliance.net's pattern: `crane auth login git.realliance.net -u realliance-ci --password-stdin`, a "refuse to move a tag" check, push, then re-read the digest and print `repo:tag@digest` in the log.
- Use **a second token on `realliance-ci`** (for example `realliance-forgejo-registry`, scope `write:package`) in a new repo secret `REGISTRY_TOKEN` on `realliance/forgejo`. Copying `SITE_REGISTRY_TOKEN` and minting a new account are both worse:
  - A copy couples the two rotations and leaves the runbook with two secrets to remember.
  - A new account must join the expected-local-accounts list and `ForgeUnexpectedAdmin`, and it buys no isolation, because Forgejo 15 can't scope a package token below the org.
  - Cost of the chosen option: the sso test that expects exactly one `realliance-ci` token and the rotation runbook must learn the second token. The offboarding step 3 then rotates both.
- Never use `ops-ci`'s `REGISTRY_TOKEN`: it reaches `realliance-ops` packages (forge-bot, ci-dispatch), and members can read secrets in `realliance` repos.
- Pulls are anonymous: `realliance` packages are public, and realliance.net's pin pulls with no pull secret (checked: `crane digest git.realliance.net/realliance/realliance.net:2.4.0` = the H pin). The package doesn't exist yet (`crane ls` → `NAME_UNKNOWN`).
- Tags:
  - git tag `v15.0.11-realliance.N` (the fork README's scheme; `v*` tags are protected);
  - image tags `v15.0.11-realliance.N` and `sha-<7>`;
  - in-binary version `15.0.11-realliance.N+gitea-1.22.0`.
  - The deploy pins `…:v15.0.11-realliance.N@sha256:<amd64 manifest digest>`.
  - Guard: the job fails if the tag's `X.Y.Z` ≠ `.gitmodules` `tag`.
  - A semver prerelease sorts below `15.0.11`; that is the same shape as upstream's own `git describe` dev builds.
- Triggers: tag push (publish) and `workflow_dispatch` (`sha-` only). No PR builds: they cost `realliance`'s budget and would expose the token path. `concurrency:` one group, because two `medium` jobs plus push egress near the sweeper's $0.60/h line.

**Evidence.** H `docs/forgejo.md:29-31` (`ops-ci`/`REGISTRY_TOKEN` scope), `:268` (crane, `realliance-ci`), `:314` (token reaches every `realliance` package), `:316-322` (one-token test, rotation), `:87` (offboarding rotates the token). H `docs/ci.md:444` (expected local accounts). realliance.net `scripts/ci.sh:85-104`. F `README.md:36-39` (tag scheme; "Deploys pin them by digest after Chris reviews them"). `.superpowers/forgejo-fork/report.md` (`v*` tag protection). H `…phase-2.md:108` (2 `medium` at once, $0.48 gate, $0.60 sweeper line).

**Open risks.** Any `realliance` member who can push a branch can read the token and push a `realliance/forgejo` tag. **Only the hand-reviewed digest pin keeps that off the live forge**, so never add Renovate automerge for this image. The first push uploads all base layers (about 124 MB of ACI egress), later ones about 47 MB. The forge's B2 upload goes over the home uplink.

## 4. Deploy

**4a. Today.**

- The Argo app `forgejo-release` deploys chart 17.3.2 from `code.forgejo.org` (OCI).
- `image.rootless: true` and `image.digest: sha256:3cd6bc2b…`; the chart derives the tag `15.0.11-rootless`.
- `strategy: Recreate` (one RWO repos volume), `pullPolicy: IfNotPresent` (chart default).
- No Renovate manager sees the digest inside `valuesObject`, and chart bumps never automerge.
- Argo's own source is GitHub, so Argo works while the forge is down.

**4b. What changes.**

1. Add `image.fullOverride: git.realliance.net/realliance/forgejo:v15.0.11-realliance.N@sha256:…`. Keep `rootless: true`, which also drives the chart's paths and ports. Keep the upstream `digest:` line untouched: with `fullOverride` set the chart ignores it, so it doubles as the documented fallback.
2. Patch-specific config:
   - if the build has the other session's `navbar.tmpl` patches, **drop** the `custom/templates/user/settings/navbar.tmpl` override (mount and ConfigMap entry) and set `[realliance] ACCOUNT_URL`;
   - `[realliance.ci_usage]` `URL`/`TOKEN` for the Usage tab;
   - NetworkPolicy ingress to the usage API from the Forgejo pods.
   These are the "deploy steps in #16".
3. Re-pin `ci-dispatch` to a build that has #3 (blocked jobs) **before or with** an image carrying the `blocked` patch.
4. A pin-guard test (like `test_realliance_net_pin.py`): `fullOverride` matches `git.realliance.net/realliance/forgejo:v15.0.11-realliance.N@sha256:<64>`, and the upstream fallback `digest` stays present.

**4c. Rollout, downtime, safety.**

- Recreate: the old pod stops (grace 60 s), then the new pod runs its init containers, one of which is `configure_gitea.sh` → `gitea migrate`, a no-op here. Then it starts.
- About 1 to 3 min of downtime with the image cached. Add RBD re-attach if the pod moves node (normally under a minute).
- Never two versions at once. Sessions live in the DB (`session.PROVIDER=db`), and queues (LevelDB) on `/data`; both survive.
- Roll out when no Actions job is running: runners mid-job lose the forge, and paid ones keep billing.

**4d. The bootstrap risk is every rollout, not only a reschedule.**

- With Recreate, the old Forgejo is gone before the new pod's kubelet pulls. The manifest and the `/v2` token come from Forgejo; only blobs redirect to B2.
- Any node without the image cached gives `ImagePullBackOff` with the forge down, and nothing recovers by itself.
- realliance.net doesn't solve this. It accepts "new pods can't pull while Forgejo is down", and its pre-pull is optional. Its old pods keep serving; Forgejo's can't.
- The same trap fires on reschedules: drains for the 3 pending Talos hops (#2506, each drains all 3 nodes), a node rebuild (EPHEMERAL wipe), image GC under disk pressure, or a package deleted from the registry.
- Within "own registry only", the clean fix is a **pull-before-stop hook**:
  - an Argo `PreSync` hook in the chart's `extraDeploy`, a DaemonSet or one Job per node;
  - its image is `{{ include "gitea.image" . }}` (`extraDeploy` is `tpl`-rendered), so it always pulls exactly what the Deployment will run, the fallback included;
  - it runs `/app/gitea/gitea --version` and then sleeps (DaemonSet), while the old pod still serves the registry;
  - it doubles as a "binary starts on Alpine" gate: a failed hook fails the sync, and the old pod keeps running.
- This *is* in-cluster pre-pulling, so **it needs Chris's sign-off** against the "no pre-pulling" decision. The alternatives are manual `talosctl image pull` per release (declined), a ghcr copy (declined), or a forge that can't roll forward.
- Hook caveat: with a node NotReady, a DaemonSet hook never turns healthy. The emergency sync is selective, without hooks: `argocd app sync forgejo-release --resource apps:Deployment:forgejo`.
- Before each Talos drain of Forgejo's node, check `talosctl image list` on the target nodes; add this to the talos-upgrade skill.

**4e. Fallback runbook (pin the same-version upstream digest).** Trigger: the Forgejo pod is in `ErrImagePull`/`ImagePullBackOff`/`CrashLoopBackOff` on the fork image.

1. In H, delete the `fullOverride:` line in `cluster/applications/forgejo-release.yaml`. The chart then renders `code.forgejo.org/forgejo/forgejo:15.0.11-rootless@sha256:3cd6bc2b…`. Commit and push to GitHub `main` (S2; `kubectl set image` is reverted by self-heal).
2. Argo syncs from GitHub plus code.forgejo.org with no forge dependency. If the hook stalls, run the selective sync from 4d. Watch with `Monitor` (safe-rollout).
3. Expect `gitea migrate` to be a no-op and the data to be intact. Lost while on upstream:
   - the jj UI, PR-round views, the `blocked` status (ci-dispatch falls back to guessing), the Usage tab and the account link (keep the old navbar override in git history to restore);
   - round refs stay hidden from fetches, because `transfer.hideRefs` persists in Forgejo's gitconfig on `/data`. They show up in commit counts and graphs, and a mirror-mode push mirror could push `refs/realliance/*`.
4. Roll forward only with the hook in place, or while Forgejo is up with the image cached on every node.

**Evidence.**

- H `cluster/applications/forgejo-release.yaml:28-30` (chart), `:36-37` (Recreate), `:38-41` (image), `:75-80` (navbar subPath mount), `:192-197` (session db, level queue); H `cluster/applications/forgejo.yaml:13` (Argo source GitHub); H `cluster/forgejo/kustomization.yaml:24-29` (override ConfigMap).
- H `docs/forgejo.md:280`, `:324` (realliance.net: new pods need Forgejo up), `:326-340` (override table), `:378` (no Renovate manager for the image).
- C `values.yaml:51-58` (image, IfNotPresent), `:337` (grace 60), `:762-764` (`extraDeploy`); C `templates/_helpers.tpl:46-64` (`fullOverride` wins; `-rootless` suffix logic); C `templates/gitea/extra-list.yaml` (`tpl`-rendered); C `scripts/configure_gitea.sh.tpl:8` (`gitea migrate`); C `templates/gitea/deployment.yaml:63-201` (init containers use the same image).
- No migrations: no `migrations`, `x.Sync`, `queue.Create` or `cron.Register` in any `patches/*.patch` or in `753f7ed..realliance`. The rounds patch message says "There are no migrations: the data lives in git refs and in the push comment's JSON". Its `modules/git/git.go` hunk adds `transfer.hideRefs` via `configAddNonExist`, and its extra `PushActionContent` fields are `omitempty` JSON that upstream ignores.
- Forgejo blocks downgrades only by migration count (U `models/forgejo_migrations/migrate.go:184`), not by the app semver.

**Open risks.**

- A WAN-IP change breaks node pulls until the router's reflection rules follow (H `docs/forgejo.md:324`); combined with a reschedule, that is an outage.
- Package deletion by any member, as above.
- The hook adds a sync-time dependency on all nodes being Ready.

## 5. Coordination with the other session

**Answer.**

- The other session is executing the CI-usage plan (#16) from `.superpowers/sdd/usage-page-plan/` in F. Tasks 0 to 4 are done: ci-dispatch PR #4 is open, and the fork commits are the account link, the CI usage tab, and the held-job note.
- It is in **Task 5**: squash (done: `upstream/` `realliance` now ends `8fb402b`, `c931331`, `b4e8a0d`), then `make export` and `make check` (→ 10 patches), a `WISHLIST.md` row, then a signed commit on branch `ci-usage-page` and a PR "Closes #16". Merge only on Chris's word.
- Its rules: nobody else touches `patches/`, and `upstream/` is its working tree.
- **No file conflict** with `.forgejo/workflows/image.yml`: their PR touches `patches/*` and `WISHLIST.md`, and the image work adds `.forgejo/` plus probably a `scripts/` file and a README edit (its "No builds or CI yet" section, `README.md:47-51`, still names upstream's Dockerfile).
- **Process conflicts:**
  1. Never write in F while it works: do the image work in a separate clone. An untracked file in the shared tree could ride into their commit.
  2. Build the first deployable image after their PR merges, so it holds all 10 patches. Any earlier build is a pipeline test only.
  3. The 7th patch (`blocked`) needs a ci-dispatch with #3. Live is `sha-9055e60`, which lacks `f80e496`/`5cc64ca`/`2159c9f`, and the `v2` branch (phase 2, needed for `medium`) lacks them too. Merge #3 into `v2` before Task 21.
  4. The navbar shadowing from 4b.
  5. README planned a `make check` workflow on `tiny`; keep it separate from `image.yml` or fold `make check` in as step 1. Either way, agree on it in one PR.

**Evidence.** F `git log` (main `5675439`, signed; `wish-*` branches stale); F `WISHLIST.md:8-9`, `:24-29` ("Deploy that `ci-dispatch` before or with the forge image"); F `.superpowers/sdd/usage-page-plan/progress.md` (Tasks 0-5, environment notes), `task-5-brief.md` (Steps 1-6); `git -C F/upstream show --stat 8fb402b` (`templates/user/settings/navbar.tmpl`); `git -C ~/Repos/ci-dispatch merge-base` checks; H `cluster/ci-dispatch/dispatcher/statefulset.yaml:65` (live pin).

---

## Recommended plan outline (7 tasks)

1. **Preconditions (no code).**
   - Chris decides the pull-before-stop hook (4d) and the token approach (3).
   - Confirm phase 2 Task 21 (`medium` on) is scheduled, and that `v2` carries ci-dispatch #3.
   - Wait for the other session's #16 PR to merge.
2. **Registry credential.** STOP for Chris:
   - mint the `realliance-ci` token `realliance-forgejo-registry` (`write:package`) and set the repo secret `REGISTRY_TOKEN` on `realliance/forgejo`;
   - store it in `sso-secrets`;
   - update the sso one-token test and the H rotation and offboarding runbooks.
3. **Fork CI, in a separate clone** (`scripts/ci-tools.sh`, `scripts/build-image.sh`, `.forgejo/workflows/image.yml`):
   - pinned crane and Node tarball;
   - `make check` → `make apply` → `npm ci` → `make frontend backend` (CGO 0, `bindata timetzdata`, explicit version, `-trimpath`, `-buildid=`);
   - binary checks; deterministic layer; `crane append`/`mutate` onto `code.forgejo.org/forgejo/forgejo@sha256:3cd6bc2b…` (amd64);
   - publish with refuse-to-move;
   - tag and `workflow_dispatch` triggers, one `concurrency` group, `runs-on: medium`;
   - update the README's "No builds or CI yet" section.
   PR to the fork.
4. **First build (dry run).** `workflow_dispatch` → `sha-<7>`. Ledger for phase 2 Task 21 Step 5: wall time, start, peak `/proc/meminfo`, cost. Rebuild once and compare digests (reproducibility). Check `crane config` (user, entrypoint, labels) against upstream.
5. **H deploy prep (no rollout yet).**
   - the `PreSync` pull hook via `extraDeploy`;
   - the pin-guard test;
   - the #16 config (`[realliance]`, `[realliance.ci_usage]`, usage NetworkPolicy ingress);
   - the navbar override removal;
   - docs: `docs/forgejo.md` gets a "Fork image" section, the fallback runbook and the deploy order; the talos-upgrade skill gets an image-cache preflight.
   Land with `fullOverride` still absent: this should be a no-op sync, which proves the hook against the upstream image.
6. **Release and deploy.** STOP for Chris:
   - an Owner pushes the signed tag `v15.0.11-realliance.1`; Chris reviews the digest;
   - re-pin ci-dispatch (#3, #4) first;
   - then a commit adding `fullOverride` while no Actions jobs run;
   - `Monitor` the hook and the Recreate rollout;
   - acceptance: `tests/forge`, `/api/v1/version` = `15.0.11-realliance.1+gitea-1.22.0`, a jj PR-rounds smoke, the Usage tab, the account link, a `blocked` job.
   Rollback trigger: any acceptance failure, or the pod not Ready within 10 min. Rollback = 4e.
7. **Fallback drill and follow-ups.**
   - Exercise 4e once in a quiet window: upstream pin, then forward again with the hook.
   - Follow-ups: an upstream pull mirror on the forge, the CI cache for npm and Go, and an optional in-place layer replacement (−47 MB).
