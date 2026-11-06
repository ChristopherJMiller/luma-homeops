# Forgejo fork image: build and deploy (implementation plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CI on the paid `medium` class builds the `realliance/forgejo` patch queue into an OCI image in the forge's own registry, and the live forge (git.realliance.net) runs that image. A keep-warm DaemonSet means a rollout never pulls from a forge that is down.

**Architecture:**
- **Build:** a fork workflow runs `make check` and `make apply`, then upstream's `make frontend backend` (static, `bindata timetzdata`, with an explicit version).
- **Image:** `crane append` adds one layer holding `/app/gitea/gitea` onto the pinned upstream rootless image, the realliance.net `build-image.sh` pattern. Publishing pushes the result to `git.realliance.net/realliance/forgejo` as `realliance-ci`.
- **Deploy:** luma-homeops sets chart `image.fullOverride` to the hand-reviewed digest. A DaemonSet, rendered through `extraDeploy` with the same image and synced one Argo wave earlier, pulls the image onto every node while the old forge still serves it.

**Tech stack:** Forgejo 15.0.11 (Go 1.26.10, Node 24.21.0 + npm), crane v0.22.1, Forgejo Actions on `runs-on: medium` (ACI, 4 vCPU / 16 GiB, no Docker, no Nix), forgejo-helm chart 17.3.2 through Argo CD, pytest manifest tests.

**Spec:** `docs/superpowers/specs/2026-11-06-forgejo-fork-image-research.md`. This evidence-cited research is the spec; section numbers below (§1–§5) refer to it. Chris's decisions are in Global Constraints.

## Global Constraints

- Build on `runs-on: medium`, billed to `realliance` ($30/period) (Chris, 2026-11-05). Never build in the live tiny class or locally for release.
- The live forge pulls from the forge registry only: no ghcr copy (Chris, 2026-11-05).
- Fork patches never add DB migrations, so the same-version upstream image is always a boot fallback (Chris, 2026-11-05). CI enforces this (Task 2).
- Keep-warm DaemonSet: it runs the forge image as `sleep` on all 3 nodes, one Argo sync wave before Forgejo (Chris, 2026-11-06). It replaces his earlier "no pre-pull" rule for this image.
- Upstream pin: tag `v15.0.11`, commit `753f7ed`. Base image `code.forgejo.org/forgejo/forgejo@<digest>`, where `<digest>` is the `image.digest` in `cluster/applications/forgejo-release.yaml` (`sha256:3cd6bc2b74c8…f9f40c`, rootless index), platform `linux/amd64`.
- Build flags (§1): `TAGS="bindata timetzdata"`, `CGO_ENABLED=0`, `GOTOOLCHAIN=go1.26.10`, `GOFLAGS=-trimpath`, `LDFLAGS=-buildid=`, `FORGEJO_GENERATE_SKIP_HASH=true`, all passed on the `make` command line.
- Version: a tag build is `15.0.11-realliance.N+gitea-1.22.0`; a dispatch build is `15.0.11-realliance.dev.<sha7>+gitea-1.22.0`. Each build's version is unique, because `AssetVersion` busts browser caches from it.
- Image: `git.realliance.net/realliance/forgejo`. Tags `v15.0.11-realliance.N` (tag builds) and `sha-<7>` (every build). The deploy pin is `…:v15.0.11-realliance.N@sha256:<64>`, the amd64 manifest digest. Never move a tag.
- Registry credential: a second named token on `realliance-ci`, `realliance-forgejo-registry` with scope `write:package`, in the repo secret `REGISTRY_TOKEN` on `realliance/forgejo` and in `sso-secrets` as `REALLIANCE_FORGEJO_REGISTRY_TOKEN` (§3). Never use `ops-ci`'s token.
- No Renovate manager and no automerge for this image. Chris reviews the pin by hand (§3 risks).
- The fork work happens in a separate clone, `~/Repos/realliance-forgejo-ci`. Never write in `~/Repos/realliance-forgejo`, where another session works (§5).
- The scripts check each pinned download (crane, the Node tarball) against a sha256 written beside it.
- CLAUDE.md S1–S9 apply. Tokens are never printed, never in argv, never in logs.

## Review Focus

1. **A node is NotReady during a rollout.** The DaemonSet never turns healthy, so the sync stalls before Forgejo's Recreate, and the old forge keeps serving. The runbook gives the selective sync. Test: Task 4 `test_keepwarm_wave_before_forgejo`.
2. **The fork binary doesn't start on Alpine.** The DaemonSet runs `/app/gitea/gitea --version` before `sleep`, so it crash-loops in wave −1. The sync **waits** there, and never fails: Argo here has no sync timeout, and it never reads a DaemonSet as Degraded. The old pod keeps serving, and nothing alerts, so the rollout's `Monitor` watches the keep-warm pods. The way out is a revert of the pin, never a sync of Forgejo (`docs/forgejo.md`, "When the sync waits at wave -1"). Test: Task 4 `test_keepwarm_runs_version_first`.
3. **Someone re-pushes a tag or deletes a package version.** Publish refuses to move an existing tag, and the deploy pins a digest. Test: Task 2 `refuse-to-move` case.
4. **A patch adds a migration.** The fallback stops being safe. The build fails before compiling. Test: Task 2 `no-migrations` case.
5. **A build reuses a version string.** Browsers serve stale assets. The workflow derives the version from the tag or sha and fails when the tag's `X.Y.Z` ≠ `.gitmodules` `tag`. Test: Task 3 `version` cases.

---

### Task 0: Start (no code)

**Files:** create the ledger `.superpowers/sdd/2026-11-06-forgejo-fork-image/progress.md`.

- [ ] **Step 1: Record the decisions** in Global Constraints and the cross-plan gates, with their states today:
  - **G1:** phase 2 Task 21, `medium` on. It gates Task 3 Step 4 and later.
  - **G2:** the live ci-dispatch carries main's #3 (blocked jobs) and, if merged, #4. Phase 2 merges main into `v2` after its Task 13; this gates Task 5.
  - **G3:** the other session's #16 PR (10 patches, the navbar change) has merged into the fork's `main`. It gates Task 5's release build.
- [ ] **Step 2: Clone** the fork with `git clone https://git.realliance.net/realliance/forgejo.git ~/Repos/realliance-forgejo-ci`, then create branch `ci-image`. Expected: a clean tree, `git submodule status` shows `upstream` at `753f7ed`.

### Task 1: Registry credential

**Files:** `~/Repos/realliance-sso/tests/forge/test_forge_ci.py` (the `realliance-ci` token test), `docs/forgejo.md` (rotation and offboarding), `sso-secrets` `forge/secrets.env`.

- [ ] **Step 1: Failing test.** Change the one-token test to `test_realliance_ci_tokens`: `realliance-ci`'s token names are exactly `{"realliance-net-registry", "realliance-forgejo-registry"}`. Run it with `nix develop --command pytest tests/forge/test_forge_ci.py -k realliance_ci_tokens -v`. Expected: FAIL (one token).
- [ ] **Step 2: STOP, then ask Chris** to mint the token; minting is security-sensitive. Mint it with `forgejo admin user generate-access-token --username realliance-ci --token-name realliance-forgejo-registry --scopes write:package --raw`. Pipe the output, never printed, into the repo secret PUT (`/api/v1/repos/realliance/forgejo/actions/secrets/REGISTRY_TOKEN`) and into `sso-secrets` `REALLIANCE_FORGEJO_REGISTRY_TOKEN`. Commit sso-secrets signed. Chris may run the pipe himself with `!`.
- [ ] **Step 3:** re-run the Step 1 test. Expected: PASS. Then commit the test, PR to realliance/sso, and merge (tests only).
- [ ] **Step 4: Docs.** In `docs/forgejo.md`, the `SITE_REGISTRY_TOKEN` rotation becomes "realliance-ci's two tokens", with the same guarded delete for each (`ACCT=realliance-ci OLD=<name>`). The offboarding step 3 rotates both. Run vale (0/0/0), commit signed, push to main.

### Task 2: Build scripts and their local tests (fork clone)

**Files:** in `~/Repos/realliance-forgejo-ci`, create `scripts/ci-tools.sh`, `scripts/build-image.sh`, `scripts/publish-image.sh`, `scripts/check-patches.sh` and `scripts/test-ci.sh`.

**Interfaces (produces):**
- `scripts/ci-tools.sh` installs crane v0.22.1 and Node 24.21.0 (with npm) into `$CI_TOOLS` (default `.ci-tools/`), each verified by a sha256 written in the script. It prints `PATH` additions to stdout.
- `scripts/check-patches.sh` exits 1 if any `patches/*.patch` touches `models/migrations/`, `models/forgejo_migrations/`, adds `x.Sync(`, `queue.Create`, or `cron.Register`. It exits 0 otherwise.
- `scripts/build-image.sh <binary> <version> <revision> <out-dir>` writes an OCI layout to `<out-dir>` and the amd64 manifest digest to `<out-dir>/digest`. It follows realliance.net `scripts/build-image.sh`: a deterministic tar holding `app/gitea/gitea` (0755, uid/gid 0, sorted, `--mtime` = the commit time), a job-local `crane registry serve`, `crane copy --platform linux/amd64` of the base, `crane append`, then `crane mutate` of the labels `org.opencontainers.image.{source,version,revision,base.name,base.digest}`, and `crane pull --format=oci`.
- `scripts/publish-image.sh <out-dir> <tag>...` logs in with `crane auth login git.realliance.net -u realliance-ci --password-stdin` from `$REGISTRY_TOKEN`. It refuses to push a tag that already exists at a different digest, pushes, re-reads the digest, and prints `git.realliance.net/realliance/forgejo:<tag>@<digest>`.

- [ ] **Step 1: Failing tests in `scripts/test-ci.sh`** (bash, `set -euo pipefail`, run as `bash scripts/test-ci.sh`):
  - `no-migrations`: `check-patches.sh` on a fixture dir whose patch touches `models/forgejo_migrations/v99.go` exits 1. On the real `patches/` it exits 0.
  - `layer`: `build-image.sh` on a static hello-world binary standing in for gitea, against the real base by digest. Then `crane config` keeps upstream's `User`, `Entrypoint`, `Env` and `WorkingDir`; `crane export | tar -t` lists `app/gitea/gitea` from the new layer; the labels hold the version and revision given; and two runs give the same digest.
  - `refuse-to-move`: against a local `crane registry serve`, `publish-image.sh` pushes tag `t1`, then refuses to push `t1` again at a different digest (exit ≠ 0, the registry unchanged). Re-pushing the same digest succeeds.
  - `pins`: every download URL in `ci-tools.sh` has a sha256 beside it.

  Run it. Expected: FAIL (scripts missing).
- [ ] **Step 2: Implement** the four scripts. Use shellcheck, `--help` and no trailing comments on command lines.
- [ ] **Step 3: Run** `bash scripts/test-ci.sh`. Expected: all PASS. Run shellcheck clean.
- [ ] **Step 4: Commit** signed on `ci-image`: `ci: image build scripts (crane append onto upstream rootless)`.

### Task 3: The workflow, its first run, reproducibility

**Files:** `.forgejo/workflows/image.yml`, `README.md` (replace "No builds or CI yet").

- [ ] **Step 1: Failing version test.** Add `version` cases to `scripts/test-ci.sh` for a new `scripts/version.sh <ref>`:
  - `refs/tags/v15.0.11-realliance.3` → `15.0.11-realliance.3+gitea-1.22.0`.
  - A branch ref at sha `abcdef1…` → `15.0.11-realliance.dev.abcdef1+gitea-1.22.0`.
  - `refs/tags/v15.0.12-realliance.1` exits 1, because `.gitmodules` says `v15.0.11`.

  Run it. Expected: FAIL. Implement `version.sh`, run again, expected PASS.
- [ ] **Step 2: Write `image.yml`.**
  - **Triggers:** `push: tags: ['v15.0.11-realliance.*']` and `workflow_dispatch`. No PR builds.
  - **Runner:** `runs-on: medium`, `timeout-minutes: 40`, `concurrency: {group: image, cancel-in-progress: false}`.
  - **Steps:**
    1. Check out, with actions pinned by sha as in luma-homeops' `forgejo-runner-class-image.yml`.
    2. Set a git identity for `am`.
    3. `scripts/ci-tools.sh`.
    4. `scripts/check-patches.sh`.
    5. `make check`, then `make apply`.
    6. In `upstream/`: `npm ci`, then `make frontend backend` with the Global Constraints flags and `FORGEJO_VERSION=$(scripts/version.sh "$GITHUB_REF")`.
    7. Binary checks: `--version` matches, `go version -m` shows the tags, `CGO_ENABLED=0` and `-trimpath`, and `file` says "statically linked".
    8. `build-image.sh`, then `publish-image.sh` with `sha-<7>`, plus `v15.0.11-realliance.N` on a tag build.
    9. Print the pin line and `/proc/meminfo`'s peak.
- [ ] **Step 3: PR** to realliance/forgejo from `ci-image`. Merge it as fast-forward: no deploy happens, and it touches no file the other session touches (§5).
- [ ] **Step 4: Gate G1.** Wait for `medium` on (phase 2 Task 21).
- [ ] **Step 5: First run,** a `workflow_dispatch` on `main`. Expected: success, `sha-<7>` pushed. Ledger, for phase 2 Task 21 Step 5: wall time, start time, peak memory, the charge from the usage API, pass or fail. If wall time is over 75 % of 40 min, raise it as a finding.
- [ ] **Step 6: Reproducibility.** Dispatch again on the same sha. Expected: the same digest. If it differs, find the cause before Task 5. Then compare `crane config` against the upstream amd64 config: only labels differ, plus the one extra layer.

### Task 4: luma-homeops deploy prep (no fork image yet)

**Files:** `cluster/applications/forgejo-release.yaml` (`extraDeploy`), create `scripts/test_forgejo_image.py`, `docs/forgejo.md` (new "Fork image" section), `.claude/skills/talos-upgrade/SKILL.md` (image-cache preflight).

**Interfaces (produces):** a DaemonSet `forgejo-keepwarm` in namespace `forgejo` with annotation `argocd.argoproj.io/sync-wave: "-1"`, image `{{ include "gitea.image" . }}`.

- [ ] **Step 1: Failing tests in `scripts/test_forgejo_image.py`.** They render with `helm template` the way `test_realliance_net_pin.py` does.
  - `test_keepwarm_wave_before_forgejo`: the DaemonSet's wave is `-1`. The Deployment has no wave, or one ≥ 0.
  - `test_keepwarm_same_image`: the DaemonSet image equals the Deployment's main container image. Check with and without `fullOverride`.
  - `test_keepwarm_runs_version_first`: the command is `["sh","-c","/app/gitea/gitea --version && exec sleep 2147483647"]`.
  - `test_keepwarm_minimal`: requests `cpu: 1m, memory: 8Mi`, limit `memory: 64Mi`, `runAsNonRoot`, uid 1000, `readOnlyRootFilesystem`, all capabilities dropped, `automountServiceAccountToken: false`, tolerations covering all three nodes.
  - `test_fork_pin`: if `fullOverride` is set, it matches `^git\.realliance\.net/realliance/forgejo:v15\.0\.11-realliance\.\d+@sha256:[0-9a-f]{64}$`, and the upstream `digest:` line is still present.

  Run `nix develop --command python -m pytest -q -p no:cacheprovider scripts/test_forgejo_image.py`. Expected: FAIL.
- [ ] **Step 2: Implement** the `extraDeploy` DaemonSet. Then run the tests, expected PASS, and run `scripts/check-kustomizations.sh`.
- [ ] **Step 3: Docs.**
  - A "Fork image" section in `docs/forgejo.md`: build and release (Owner pushes the signed tag; Chris reviews the digest); the deploy order (ci-dispatch with #3 first; no Actions job running); the keep-warm DaemonSet and the selective emergency sync `argocd app sync forgejo-release --resource apps:Deployment:forgejo`; and the fallback runbook (§4e, delete the `fullOverride` line).
  - In the talos-upgrade skill, before draining Forgejo's node: `talosctl image list` on the targets shows the forge image.
  - vale 0/0/0, no trailing comments in code blocks.
- [ ] **Step 4: Gate G, push to main, Monitor** until `forgejo-release` is Synced/Healthy and the DaemonSet is 3/3 on the **upstream** image. Expected: a no-op for Forgejo itself, with no Recreate. That proves the wave against the image that is live today.

### Task 5: Release and deploy

**Gates:** G1, G2, G3, Task 4 done.

- [ ] **Step 1: STOP, then ask Chris** to push the signed tag `v15.0.11-realliance.1` on the fork's `main` (an Owner's `v*` tag). The workflow publishes it. Ledger the pin line, and show Chris the digest to review.
- [ ] **Step 2: One signed commit** in `cluster/applications/forgejo-release.yaml`:
  - `image.fullOverride: <pin line>`;
  - remove the `user-settings-navbar.tmpl` mount and its ConfigMap entry (the fork's patch replaces it);
  - add the #16 config `[realliance] ACCOUNT_URL` and `[realliance.ci_usage] URL`, with `TOKEN` from a SealedSecret;
  - add a NetworkPolicy ingress rule from the Forgejo pods to the ci-dispatch usage API.

  `test_fork_pin` passes.
- [ ] **Step 3: STOP, then ask Chris** to approve the rollout. Downtime is 1–3 min. Rollback triggers: any Step 5 failure, or the pod not Ready within 10 min. Rollback is §4e.
- [ ] **Step 4: Push** when no Actions job is running. `Monitor` the DaemonSet (wave −1) to 3/3 on the new digest, then Forgejo's Recreate to Ready.
  - The `Monitor` watches each keep-warm pod's state, not only the counts. It stops on `CrashLoopBackOff`, `OOMKilled`, `Error`, `ErrImagePull` or `ImagePullBackOff`, and has a time bound. On a crash loop, read `kubectl -n forgejo logs <pod> --previous`, then follow the runbook's crash-loop case: revert the pin, never sync Forgejo.
  - This is the DaemonSet's first update (Task 4 proved only its creation). Ledger the operation message `waiting for healthy state of apps/DaemonSet/forgejo-keepwarm` as the evidence that the wave ran before the Recreate.
- [ ] **Step 5: Acceptance.**
  - `/api/v1/version` reads `15.0.11-realliance.1+gitea-1.22.0`.
  - `nix develop --command pytest tests/forge -v` in realliance-sso passes.
  - A jj PR-rounds smoke passes.
  - The CI Usage tab and the account link render.
  - A `blocked` job shows as held.
  - Ledger it.

### Task 6: Fallback drill and follow-ups

- [ ] **Step 1: STOP, then ask Chris** for a quiet window. Run §4e: delete `fullOverride`, push, Monitor until the forge is up on upstream and the data is intact (log in, a repo and a PR load). Then restore `fullOverride` and Monitor forward through the DaemonSet. Ledger both downtimes.
- [ ] **Step 2: Ledger the follow-ups:** an upstream pull mirror on the forge (it removes Codeberg from the build path); the CI cache for npm and Go; an optional in-place layer replacement, which saves 47 MB.
