# realliance.net on the Forge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to run this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** the Realliance forge builds the image that serves realliance.net. The source moves from GitHub to `git.realliance.net/realliance/realliance.net` with its full history. The restyle onto the Realliance brand ships as previewed, with brand assets synced from the first signed `realliance/brand` tag. CI runs on a new `general` forgejo-runner that never shares anything with forge-bot's runner. The cluster pins `git.realliance.net/realliance/realliance.net:2.4.0@sha256:<digest>`. Task 8 archives the GitHub repo with a pointer README.

**Architecture:**
- **Repo.** The `realliance-ops/forge` inventory declares `realliance/realliance.net`: public, with fast-forward-only merges and a protected, signed `main`. The local clone pushes GitHub's `main`, every tag and the member-bio branches to it, before `main` gets protected.
- **Runner.** A second runner Deployment, `forgejo-runner-general`, has its own namespace, PVC, SealedSecret connection and slim image. It registers at repo scope for `realliance/realliance.net` only, with the label `general`.
- **CI.** The CI workflow downloads a pinned Deno and crane and checks their sha256. `build.ts` renders the site. `crane append` adds one layer, holding the site and `nginx.conf`, onto the pinned nginx base, and `crane mutate` sets port 8080. This all happens against a job-local `crane registry serve`, so PR builds need no credentials. Push builds then push the result to the forge registry with a `write:package` token kept in a repo secret. Tags are `X.Y.Z` (from the git tag) and `sha-<7>`, and CI never moves an existing tag.
- **Deploy.** The luma-homeops pin is a manual `tag@sha256` re-pin, and Argo rolls it out. Anonymous pulls go through the WAN hairpin and Forgejo redirects them to B2 (`SERVE_DIRECT`).
- **NetworkPolicy.** The runner's egress policy uses the NetworkPolicy plan's components and `netpol-gate` unchanged, and lands only after that plan's B2.

**Tech Stack:** Forgejo 15.0.11 (Actions, container registry, B2 `SERVE_DIRECT`), forgejo-runner v13.2.1 in host mode, OpenTofu with `svalabs/forgejo` 1.7.0 (the inventory), Deno 2.8.3, crane (go-containerregistry v0.22.1), nginx 1.20.2 (the base the site has today), Argo CD, SealedSecrets, kube-network-policies v1.1.3 components, Talos v1.10.9 / containerd 2.0.7, GitHub Actions → GHCR (runner image only), pytest, `fj`, `gh`, chrome-devtools MCP (screenshots).

**Spec:** `.superpowers/website/decisions.md` (Chris, 2026-11-04) plus `.superpowers/website/report.md` (the preview of branch `brand` at `427178d`). Background:
- the forge-bot + CI spec, "Security model, plainly" (`docs/superpowers/specs/2026-11-02-forge-bot-and-ci-design.md`);
- the runner registration in `.superpowers/sdd/2026-11-02-forge-bot-and-ci/task-7-brief.md`, as corrected by `task-7-report.md`;
- the B0 outcome in that plan's `progress.md`;
- `.superpowers/pages-research/tiny-runner.md`.

## Global Constraints

### Pins (resolved read-only, 2026-11-04)

| What | Value | Source / note |
|---|---|---|
| Deno | **2.8.3**, `https://github.com/denoland/deno/releases/download/v2.8.3/deno-x86_64-unknown-linux-gnu.zip`, sha256 `30455b845ffa6082209c3590269c910ad3b7efdf28c9879afd4006c47ae54197` | Release published 2026-06-11; the sha256 is from its `.zip.sha256sum` asset. The preview used this version locally. |
| crane | **go-containerregistry v0.22.1**, `https://github.com/google/go-containerregistry/releases/download/v0.22.1/go-containerregistry_Linux_x86_64.tar.gz`, sha256 `0ab7a1d6932a213aed964ce97666c3077fe691c8606413674a8b3e0b9ec4cda0` | Published 2026-09-04, from `checksums.txt`. Its source confirms that `mutate --exposed-ports` **replaces** the port map, that `mutate --repo` pushes by digest, and that `registry serve` and `pull --format=oci` exist. |
| nginx base | `docker.io/library/nginx:stable-alpine@sha256:74694f2de64c44787a81f0554aa45b281e468c0c58b8665fafceda624d31e556` (index; linux/amd64 `sha256:f6609f898bcdad15047629edc4033d17f9f90e2339fb5ccb97da267f16902251`) | Chris's decision: the existing Dockerfile's pin. That pin is **nginx 1.20.2, built 2021-11-16**, the same nginx as the live image. See Q1. |
| `actions/checkout` | `fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.1.0` | forge-bot's pin |
| Runner image bases | `data.forgejo.org/forgejo/runner:13.2.1@sha256:568cea8c15ddc01fc8f330124892f572af7db37fa28344a2c1914405793bf61d`, `node:24.21.0-trixie-slim@sha256:173f125896c3b47ddf056734c7ea789d04595a6a08769a8f78e0df642781fb66`, `debian:trixie-20261005-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f` | the same pins as `images/forgejo-runner/Dockerfile` |
| General runner image | `ghcr.io/christopherjmiller/forgejo-runner-general:sha-<7>@sha256:<64>` | set in Task 3 Part A |
| Live image (the **rollback target**) | `quay.io/realliance/realliance.net:2.3@sha256:7405276fcd58b6ade36c76147a23dfaba6eac6287880c14e37160d032961e359` | Config (read 2026-11-04): `ExposedPorts {"80/tcp"}`, `User ""`, `Entrypoint ["/docker-entrypoint.sh"]`, `Cmd ["nginx","-g","daemon off;"]`, `StopSignal SIGQUIT`, `NGINX_VERSION=1.20.2`, created 2023-03-05. A GitHub push webhook feeds a Quay build trigger. **Never delete this Quay repo during this plan.** |
| New image | `git.realliance.net/realliance/realliance.net:2.4.0@sha256:<D>` | set in Task 5 |

Check a forge digest anonymously with `crane digest git.realliance.net/realliance/realliance.net:<tag>`, using the pinned crane from `scripts/ci-tools.sh`.

### Values fixed by this plan

- **Forge repo:** `realliance/realliance.net`, public, Actions and packages on, fast-forward-only. `main` gets the same protection as `brand_main` (no direct push, signed commits, Owners merge). A hand-made tag protection, `*`, is Owners-only. Inventory resources: `forgejo_repository.realliance_net` and `forgejo_branch_protection.realliance_net_main`.
- **Runner:**
  - names: namespace, Argo app and Deployment **`forgejo-runner-general`**; PVC **`forgejo-runner-general-work`** (5Gi, `rook-ceph-block-ssd`, RWO); Secret/SealedSecret **`forgejo-runner-general-config`**, key `config.yml`;
  - label **`general`** (`general:host`); registration and connection **`general-realliance-net`**, at scope `realliance/realliance.net`;
  - canonical secret `RUNNER_GENERAL_REALLIANCE_NET_SECRET` in `~/Repos/realliance-sso-secrets/forge/runner.env`.
- **Runner config** (`config.yml`): `runner: {file: /work/.runner, capacity: 1, timeout: 30m, labels: ["general:host"]}`, `cache: {enabled: false}`, `host: {workdir_parent: /work/act}`, plus the single connection.
- **Registry token:** a `forge-ci` access token named `realliance-net-registry` with scope `write:package`. Its canonical copy is `REALLIANCE_NET_REGISTRY_TOKEN` in `~/Repos/realliance-sso-secrets/forge/secrets.env`. The repo Actions secret is **`SITE_REGISTRY_TOKEN`** on `realliance/realliance.net`. Its name is not `REGISTRY_TOKEN`, so nobody confuses it with the org secret of that name.
- **Image:** repository `git.realliance.net/realliance/realliance.net`, tags `sha-<7>` (on `main` and tag pushes) and `X.Y.Z` (tag pushes only, matching `^[0-9]+\.[0-9]+\.[0-9]+$`). The config equals the base's except `ExposedPorts {"8080/tcp":{}}` and two labels: `org.opencontainers.image.source=https://git.realliance.net/realliance/realliance.net` and `org.opencontainers.image.revision=<commit>`. No version label exists, so a `main` build and a tag build of one commit have one digest.
- **Brand in the site repo:**
  - hand-edited: `brand/uses.txt`, `brand/release-signers.asc`, `brand/release-signers.fpr`;
  - generated by `scripts/sync-brand.sh --tag`: `brand/map.json` and `brand.lock`;
  - tokens `{{brand:<logical>}}` in `src/index.pug`, `src/brand.css` and `static/terminated.html`.

### How changes land

- **luma-homeops** (public). Work in a worktree, never in the main checkout. Other agents keep uncommitted files there (for example `images/pages-relay/*` on 2026-11-04).
  - `git -C ~/Repos/luma-homeops worktree add ~/Repos/luma-homeops-site -b realliance-net-site origin/main`.
  - `git add` only the task's paths, never `-A`. Commit with `nix develop --command git commit -S …`. pre-commit runs yamlfmt, kustomize-build, `netpol-manifests`, `pages-manifests` and, from Task 3, this plan's hook. If a hook rewrites a file, `git add` it and commit again.
  - Never use `--no-verify` or `--no-gpg-sign` (S6). Every shell that signs or uses ssh/gpg exports `DISPLAY=:10.0 XAUTHORITY=$HOME/.Xauthority`.
  - To land: `git fetch origin && git rebase origin/main && git push origin HEAD:main`, since Chris prefers direct pushes to `main`.
- **A push that changes what Argo renders is a deploy**, and Argo syncs in about 3 min. It happens only after that task's STOP. Docs-only, ledger-only and `images/`/`.github/` pushes are routine (covered by the start OK).
- **`realliance/realliance.net` (forge):** signed commits (`git commit -S`), branch `brand`, then a PR. Merge fast-forward only with `fj -H git.realliance.net pr merge realliance/realliance.net#<n> -d`. Sign tags (`git tag -s`).
- **`realliance-ops/forge` (inventory, private):** a branch and a signed commit, then `fj -H git.realliance.net pr create …`. An Owner merges fast-forward, so the head of `main` stays Owner-signed. Then apply from the laptop with the README's env (`nix develop ~/Repos/realliance-sso` has OpenTofu).
- **`realliance/sso`** (GitHub, public): `git commit -S`, a branch, `gh pr create --fill`. Merge only when Chris says merge.
- **`realliance/sso-secrets`** (private): `git commit -S`, push to `main` (covered by the start OK).
- **Every commit message** in every repo ends with:

  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01FWhaDeswMqYvQaVdpL8oit
  ```

- **STOP—ask Chris** is one AskUserQuestion. It states the change, the Gate G evidence, what to watch and the rollback trigger. The options are `approve: do it, and roll back at once if a trigger fires` and `hold`. A rejected or interrupted call may already have run, so read the live state before redoing anything.
- **Ledger:** `docs/superpowers/plans/2026-11-04-realliance-net-on-forge.ledger.md`. It records answers, Gate G summaries, run ids, digests, timestamps and rulings. No secrets and no hidden org, team or group names.
- **Waits:** use `Monitor` for anything over 60 s, never a sleep loop.

### Gate G (before every deploy push, and again after)

- G1. No Talos/Kubernetes hop, drain or Ceph recovery is running. 3 nodes are `Ready` with no `SchedulingDisabled`, and every Ceph PG is `active+clean`.
- G2. Put a `cluster-snapshot` summary in the ledger. Baseline on 2026-11-04: Ceph `HEALTH_OK` (AUTH_INSECURE_* muted; the MDS_CLIENT_RECALL/BLUESTORE noise is acceptable), 145/145 PGs `active+clean`, and in Argo only `rook`/`rook-cluster` not Healthy.
- G3. Run `cd .claude/skills/edge-ingress && nix-shell -p openssl jq --run ./edge-snapshot.sh > $SCRATCH/edge-<task>-<before|after>.txt` and compare with `diff <(sed 's/ cert=.*//' before | sed -E 's/(code_challenge|state|nonce)=[^& ]*/\1=X/g') <(…after…)`. This is the netpol plan's ruled normalisation.
- G4. Record `kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded` and `kubectl get netpol -A`.

### Cluster, secrets, compute

- `export KUBECONFIG=/tmp/galaxy-kubeconfig`; refresh it from Talos (CLAUDE.md) if the cluster rejects it. Run `talosctl` as `nix develop --command talosctl --talosconfig nodes/talosconfig -n <ip> -e <ip> …`, passing the path only and never reading the file.
- **Never `Read`, `cat` or diff** a `*.secret.yaml`, `nodes/*`, `forge/secrets.env` or `forge/runner.env`. Values travel only through pipes, stdin and `umask 077` files, never through argv, echo, assertion messages, logs, PR text or the ledger. In git-crypt repos use `git diff --no-textconv`.
- **GitHub webhook configs embed credentials** (the Quay trigger URL does). Never print `.config`. List hooks with `gh api repos/realliance/realliance.net/hooks --jq '.[] | {id, events, active, host: (.config.url | sub("^[a-z]+://[^@/]*@"; "") | split("/")[0])}'`.
- **Public repos** (luma-homeops, realliance.net, sso) never carry a hidden org, team or group name.
- **Local compute:** `taskset -c 0-3` for Deno builds; no local docker or kind. Put downloaded tools into their own empty directory under `$SCRATCH`, and never run a downloaded archive from inside the directory you extracted it into.

## Decisions this plan makes

- **D1. One inventory PR, two applies.** A history push to `main` must land before `main` gets protected (`enable_push = false`), so `auto_init = false`.
  - The PR declares both resources. Its plan must read `2 to add, 0 to change, 0 to destroy`.
  - Apply 1 is `-target=forgejo_repository.realliance_net` (1 add). Then push the history. Apply 2 is a plain `tofu apply` (1 add, the protection). Afterwards `tofu plan` reads `No changes.`
  - One review covers the end state.
- **D2. What "full history" means.** It covers GitHub's `main` (24fd4fa, the approved 2025-08 alice commit), all 12 tags and `chrmiller/{eddie,jared,meacham}-bio`. `renovate/docker.io-node-lts-alpine` stays out (Q2).
  - Old tags carry `.github/workflows/publish.yml` (`runs-on: ubuntu-latest`), so the tag push creates up to 7 `waiting` runs.
  - No runner has that label, and Forgejo's abandoned-job cron cancels them after 24 h. Keeping `has_actions` off for the push would cost an inventory *change* (Task 2 records it instead).
- **D3. `427178d` is re-signed.** It lacks a signature (`%G? = N`), and the protected `main` requires signed commits. The plan re-signs it with `git commit --amend -S --no-edit` on the same tree, so its content and message stay the same.
- **D4. No Docker anywhere.** The forge repo drops `Dockerfile`, `.dockerignore`, `.github/` (workflows and the GitHub-handle CODEOWNERS) and `.renovaterc.json`, since Renovate does not reach the forge.
  - In their place: `scripts/ci-tools.sh` (pinned tools), `scripts/build-image.sh` (layer, then append and mutate) and `scripts/check-image.sh` (assertions).
  - The workflow is thin, and every script runs locally.
- **D5. The append and mutate steps run against a job-local `crane registry serve`.** The result leaves the job as an OCI layout (`crane pull --format=oci`), and `crane push <layout>` keeps the manifest bytes, so the digest stays the same. Nothing intermediate ever reaches the forge, and PR builds hold no credential.
- **D6. Runtime identical to today, except the restyle.**
  - `nginx.conf` is byte-identical to the live image's (checked in Task 4) and the base is the same nginx.
  - The image inherits the base config: Entrypoint, `Cmd`, StopSignal, `User ""`.
  - `ExposedPorts` becomes `8080/tcp`, the port nginx actually listens on; the live image still says 80.
  - The cluster's `runAsUser: 101`, read-only root, `/tmp` emptyDir and 5Mi limit stay the same. The `/health` behaviour lives in `nginx.conf`, so it stays the same too.
- **D7. Reproducible builds.**
  - The content hash of their bytes names member images, not `crypto.randomUUID()`.
  - Use `deno run --frozen`.
  - Pin the three unversioned `deno.land/x` imports to the versions `deno.lock` already resolved: `pug@v0.1.6`, `twd@v0.4.8`, `recursive_readdir@v2.0.0`.
  - The layer tar is deterministic (`--sort=name`, uid/gid 0, `--mtime=@<commit time>`, modes 0644/0755).
  - Proof: two clean builds produce one digest. CI refuses to move an existing tag, which also catches drift between a `main` build and a tag build.
- **D8. Brand consumption.**
  - Copy only the files the site uses (`brand/uses.txt`), and keep their content-hashed names.
  - `build.ts` resolves `{{brand:<logical>}}` through `brand/map.json` and fails on an unknown name, a leftover token or a `/brand/` path that is not in the map.
  - Verify the tag signature against signers committed in the site repo (`brand/release-signers.{asc,fpr}`), never against Forgejo's `verified` flag. This is forge-bot's rule, and the check fails closed.
  - The build drops `--stub`. A tag build fails while `brand.lock` says `UNTAGGED-STUB`, so there is no stub release.
- **D9. The registry token can reach every `realliance` package.** Forgejo 15 has no repo-scoped tokens (forge-bot B0, outcome B). The org secret `REGISTRY_TOKEN` (the same `forge-ci` reach) is visible to this repo's jobs anyway.
  - The new token adds separate rotation, not new reach.
  - The defences are the digest pin, `automerge: false` in Renovate, and CI never moving a tag.
- **D10. The runner is `general`, not `tiny`.** It stays persistent, not ephemeral, so the `tiny` label, which promises one job per pod, would oversell it.
  - Slim image (no Go, ko or gcc); PSA `restricted`; actions cache off; per-job `DENO_DIR`.
  - Repo-scoped registration (the research's preferred scope).
- **D11. The NetworkPolicy lands only after the netpol plan's B2** and reuses its components and gate verbatim. The runner may deploy earlier, unconfined like forge-bot's runner today (Q3), because it adds no policy and so cannot affect the netpol soak.
- **D12. Pre-pull on all three nodes** with `talosctl image pull`. This tests containerd against the forge's 307-to-B2 redirect before any pod depends on it, and it shrinks the "Forgejo down means no new pods" window to evictions onto nodes that have garbage-collected the image.
- **D13. Probe the forge job token before minting anything** (Task 4). If it can push packages, which the research says it cannot before v16, Chris decides whether to drop the static token.

## Review focus

1. **Someone re-pushes `:2.4.0` with the org-wide package token.** A reasonable person expects the live site not to change. Expected: no cluster change (digest pin), no automerged Renovate PR, and CI refuses to move the tag. Tests: Task 6 `test_renovate_never_automerges_site`; Task 4 Step 6 (a publish against a moved tag exits 1).
2. **A page still references an unhashed or wrong brand path.** The font silently falls back, or the favicon returns 404. Expected: the build fails on any unresolved token or a stray `/brand/` path, and every referenced asset is 200 live with brand.lock's sha256. Tests: the `build.ts` guard (Task 1 Step 5), Task 1 Step 6 negatives, Task 7 Step 2 loop.
3. **The `auto` SVG logo under the live CSP.** The logo's ink switches through an internal `<style>` with `prefers-color-scheme`, and the SVG response itself carries `style-src 'self'`. People expect the ink to follow dark and light in every browser. Expected: correct in Chromium and Firefox. Fallback: a `<picture>` with the `-latte`/`-mocha` variants, with no CSP change. Tests: Task 1 Step 8 (local, exact CSP), Task 7 Step 3 (live).
4. **The first workload to pull from the forge registry.** Nothing in the cluster does today, because forge-bot is not deployed yet. Forgejo answers HEAD manifests; GET manifests and blobs are 307 redirects to B2. Expected: containerd 2.0.7 pulls by digest through the hairpin. Test: Task 5 Step 7 (`talosctl image pull` on each node), before the re-pin.
5. **The new runner shares state with forge-bot's runner**, which is the cache-poisoning path into the administrator-token bot. Expected: no shared namespace, PVC, Secret, registration or cache, and neither pod can read the other's connection token or forge-bot's secrets. Tests: Task 3 `test_nothing_shared_with_forgejo_runner` (static) and sso `test_general_runner_holds_no_other_credentials` (live).

## Coordination with concurrent plans

**NetworkPolicy plan** (`docs/superpowers/plans/2026-11-03-networkpolicy-enforcement.md`). Its A3a soak runs until 2026-11-04T18:35:10Z; then come Task 4 (A3b), Tasks 6–7 (canary, at least 24 h green) and Task 8 (B2, the runner policy).
- N1. This plan creates **no NetworkPolicy before NP Task 8 finishes** (`B2_done` in its ledger, and `netpol-verify.sh runner` PASS). Task 3 Part C then reuses `cluster/components/netpol/{default-deny,allow-dns,egress-internet-only}` and forgejo-runner's `netpol-gate`, both verbatim.
- N2. Task 3 Part B (runner deploy, no policy) can land at any time outside an NP Gate G before/after window. Prefer after NP Task 4 Step 8. Write the push time into the NP ledger so that its next Gate G diff explains the new namespace.
- N3. Never edit `scripts/test_netpol_manifests.py`, `scripts/netpol-verify.sh` or `cluster/netpol-canary/*`. If, by Part C, `netpol-manifests` requires an allow-list entry for a newly policed namespace, the NP owner adds it (ask through Chris), and Part C waits.
- N4. Once Part C is live, NP Tasks 9, 12 and 13, and every later enforcer restart, include the general runner in their runner-idle check (`/repos/realliance/realliance.net/actions/tasks`). Note this in the NP ledger at Part C.
- N5. Serialize STOPs: never push a deploy of this plan between another plan's Gate G `before` and `after`.

**forge-bot plan** (`docs/superpowers/plans/2026-11-02-forge-bot-and-ci.md`; Task 16 deploy held on branch `forge-bot-deploy`):
- F1. `.renovaterc.json` is a shared file, and `forge-bot-deploy` adds a rule to it. Rebase immediately before every commit that touches it.
- F2. In `realliance/sso` this plan adds only `tests/forge/test_forge_runner_general.py`. It never edits `forge_helpers.py`, `conftest.py` or `pytest.ini`.
- F3. The `galaxy` runner's allowlist (forge-bot only) never changes. The general runner is never registered for `realliance/forge-bot`.
- F4. No step restarts Forgejo. Registration and secrets go through `forgejo-cli` and the API.

**Realliance Pages plan:** serialize STOPs (N5). The edge snapshot already covers `realliance.net` and `www.realliance.net`.

**Brand repo** (`realliance/brand`, built by another agent):
- B1. Never edit `realliance/brand`. If its first signed tag lacks a logical name in Task 1's table, **STOP—ask Chris**; the brand executor owns the names.

## What this plan delivers of the `tiny` runner class

The research (`.superpowers/pages-research/tiny-runner.md` §6) describes a controller that mints an **ephemeral, repo-scoped** registration per queued `tiny` job and runs it in a **one-job, throwaway pod** (PSA restricted, per-pod SSD volume, quotas, 15-minute deadline, DNS plus internet-only egress).

**Delivered here, as the seed:**
- A second runner pool that shares nothing with forge-bot's: namespace, PVC, connection and image are all separate. This closes the cache-poisoning path into the administrator-token bot.
- The class's image shape: forgejo-runner, node, git, curl and unzip, with no Go, ko or gcc, pinned by digest and built like `images/forgejo-runner`.
- The class's pod hardening, plus PSA `restricted` (forge-bot's namespace is `baseline`).
- `cache.enabled: false`, so the runner offers no actions cache.
- Repo-scope registration, one connection per allowlisted repo. Add `realliance/brand` later as a second connection.
- The NetworkPolicy egress set (DNS, the forge via the hairpin, the internet without RFC1918), from Part C on.
- Toolchains pinned by sha256 inside the job.

**Not delivered:**
- **No ephemeral or one-job runners and no controller.** The pod is long-lived. `HOME`/`TMPDIR` and act's action clones in `~/.cache/act` persist between the jobs of one pod lifetime (a restart wipes them). The connection token is long-lived and readable by every job. The allowlist limits tenants to trusted repos.
- No `tiny` label, so the class is not open to every forge user.
- No per-job volume, no ResourceQuota, no per-owner rate limits or caps, no 15-minute deadline (`runner.timeout` is 30 min), no `activeDeadlineSeconds`, and no class-level alerts.
- No change to `DEFAULT_REPO_UNITS` or the fork-PR approval defaults.
- No Nix in the image. `realliance/brand`'s CI (a Nix build) needs its own decision when it joins.
- No narrower package token. Forgejo 15 cannot scope one, and v16's Authorized Integrations would (the 15 → 19 LTS plan).

**Residual risk, plainly:** the forge-bot spec's accepted risk applies, now for a second repo.
- Any `realliance` member can push a branch to `realliance/realliance.net` and run a workflow on this runner. That workflow can read `SITE_REGISTRY_TOKEN` and the org's `REGISTRY_TOKEN`, publish any `realliance` package, and reach whatever the pod reaches: everything until Part C, the internet only after it.
- A rogue image still reaches galaxy only through a reviewed re-pin by digest.
- The revisit trigger is the same: anyone not fully trusted gets `/realliance`.

## Order and dependencies

| Task | Needs | Can run while waiting for |
|---|---|---|
| 0 Start | — | — |
| 1 Brand sync | the first signed `realliance/brand` tag | — (Tasks 2–4 run first if the tag is late) |
| 2 Forge repo | 0 | brand tag |
| 3A Runner image | 0 | brand tag |
| `3B` Runner deploy | 2, 3A | brand tag |
| 3C Runner NetworkPolicy | `3B`, NP B2 done | may run after Task 8 |
| 4 CI | 2, `3B` | brand tag |
| 5 Release 2.4.0 | 1, 4 | — |
| 6 Re-pin | 5 | — |
| 7 Verify | 6 | — |
| 8 GitHub archive | 7 | — |
| 9 Docs | 7 (3C's lines once it lands) | — |

## File map

| Path | Responsibility | Task |
|---|---|---|
| `docs/superpowers/plans/2026-11-04-realliance-net-on-forge.ledger.md` | ledger | 0 |
| `~/Repos/realliance.net/scripts/sync-brand.sh`, `build.ts`, `src/index.pug`, `src/brand.css`, `static/terminated.html`, `brand.lock`, `brand/{uses.txt,map.json,release-signers.asc,release-signers.fpr}`, `static/brand/**`, `static/favicon.ico` | brand from the signed tag | 1 |
| `~/Repos/realliance-forge/repos.tf`, `README.md` | the forge repo, declared | 2 |
| `images/forgejo-runner-general/Dockerfile`, `.github/workflows/forgejo-runner-general-image.yml` | slim runner image | 3A |
| `cluster/forgejo-runner-general/{kustomization,namespace,pvc,deployment}.yaml`, `runner-config.secret.yaml` → `runner-config.yaml`, `cluster/applications/forgejo-runner-general.yaml`, `cluster/applications/kustomization.yaml` | the general runner | `3B`, 3C |
| `scripts/test_runner_general.py`, `scripts/test_realliance_net_pin.py`, `.pre-commit-config.yaml` (hook `realliance-net`), `.renovaterc.json` | static guards, Renovate rules | 3A, `3B`, 3C, 6 |
| `~/Repos/realliance-sso/tests/forge/test_forge_runner_general.py` | live runner checks | `3B` |
| `~/Repos/realliance.net/.forgejo/workflows/ci.yaml`, `scripts/{ci.sh,ci-tools.sh,build-image.sh,check-image.sh}`, `build.ts`, `Justfile`, `deno.lock`, `README.md`; delete `Dockerfile`, `.dockerignore`, `.github/`, `.renovaterc.json` | CI without Docker | 4 |
| `cluster/realliance.net/home.yaml` | the re-pin | 6 |
| GitHub `realliance/realliance.net` `README.md` | pointer, then archive | 8 |
| `docs/forgejo.md`, `CLAUDE.md` | runbook section, topology note | 9 |

---

### Task 0: start (worktree, ledger, baseline, execution-start sign-off)

**Files:**
- Create: `docs/superpowers/plans/2026-11-04-realliance-net-on-forge.ledger.md` (in the worktree)

- [ ] **Step 1: Worktree.** Run `git -C ~/Repos/luma-homeops fetch origin && git -C ~/Repos/luma-homeops worktree add ~/Repos/luma-homeops-site -b realliance-net-site origin/main`. Expected: a clean tree at `origin/main`. Leave the main checkout's uncommitted files alone.
- [ ] **Step 2: Execution-start OK.** Ask one AskUserQuestion that covers four things.
  - **Routine pushes:**
    - luma-homeops docs, ledger, `images/` and `.github/`;
    - sso-secrets commits;
    - realliance.net branch pushes, the PR, its fast-forward merge and the signed `2.4.0` tag;
    - Forgejo API writes for tag protection and the package link;
    - `talosctl image pull` on the three nodes.
  - **Pre-approval of the inventory apply**, only if Task 2 Step 3 shows exactly `forgejo_repository.realliance_net` + `forgejo_branch_protection.realliance_net_main`, `2 to add, 0 to change, 0 to destroy`.
  - **Pre-approval of the registry-token mint** (Task 4 Step 7), only if the forge refused the job-token probe.
  - **Q1–Q7** below.

  Ledger the answers. Anything not pre-approved stays a STOP.
- [ ] **Step 3: Baseline.**
  - Gate G1, G2 and G4; G3 to `$SCRATCH/edge-site-0.txt`.
  - Memory requests per node (`kubectl describe node <n> | grep -A8 'Allocated resources'`). Expected (2026-11-04 15:00Z): top 85 %, middle 85 %, bottom 87 %.
  - `ceph df` `block-pool-ssd` MAX AVAIL. Expected: about 138 GiB.
  - `kubectl -n realliance-net get pods -o wide` and imageIDs. Expected: 2/2 `home-…` on top/middle, `…@sha256:7405276f…`, about 2Mi each.
- [ ] **Step 4: Capture the site `before`.**
  - `curl -sSI https://realliance.net/` and `https://www.realliance.net/` → `$SCRATCH/site-before-headers.txt`.
  - The exact `content-security-policy` line → `$SCRATCH/csp-before.txt`. Expected: `default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; upgrade-insecure-requests`.
  - `curl -sS https://realliance.net/health`. Expected: `alive`.
- [ ] **Step 5: Cross-plan reads.**
  - NP ledger: A3a/A3b/B2 state. pages ledger. forge-bot `progress.md`: Task 16 state.
  - `curl -sS https://git.realliance.net/api/v1/repos/realliance/brand/tags | jq -r '.[].name'`. Expected on 2026-11-04: `[]`, so Task 1 waits.
  - Ledger everything.
- [ ] **Step 6: Commit and push** the ledger (docs-only): `docs: realliance.net-on-forge ledger (Task 0)`.

### Task 1: brand-tag sync

Runs once `realliance/brand` has its first signed `v*` tag. Until then, do Tasks 2–4; Task 5 waits.

**Files** (in `~/Repos/realliance.net`, branch `brand`):
- Edit: `scripts/sync-brand.sh` (finish `--tag`, add `--check --release`, delete `--stub`), `build.ts`, `src/index.pug`, `src/brand.css`, `static/terminated.html`, `README.md` ("Brand files")
- Create: `brand/uses.txt`, `brand/release-signers.asc`, `brand/release-signers.fpr`
- Generated: `brand/map.json`, `brand.lock`, `static/brand/**`, `static/favicon.ico`

**Interfaces:**
- **Consumes** the tag's `dist/manifest.json`, whose real shape differs from the stub's assumption: `{"about": …, "files": {"<logical>": {"file": "<path, content-hashed except fixed/, .txt and .json>", "sha256": "<64>", "bytes": N}}, "tools": {…}}`.
- **Produces:**
  - `brand/uses.txt`: one logical name per line, with an optional second field, a fixed destination under `static/`.
  - `brand/map.json`: `{"<logical>": "/brand/<file>" | "/<fixed dest>"}`, keys sorted.
  - `brand.lock`: `tag=`, `tag_object=`, `commit=`, `source=https://git.realliance.net/realliance/brand.git`, then `<sha256>  <path under static/>` lines (sha256sum format).
  - `sync-brand.sh --check [--release]`: `--release` also fails unless `tag=` matches `^v[0-9]`.

Mapping (preview file → brand logical name; confirmed present in an untagged build of 2026-11-04, re-checked against the tag in Step 1):

| Site use | Preview file | Logical name |
|---|---|---|
| hero logo (`.hero-logo`, 256×236) | `brand/img/logo.svg` (2c auto) | `img/logo-auto.svg` (viewBox 360×332) |
| terminated mark (72×36) | `brand/img/logo-mark.svg` (`3d` auto) | `img/mark-wide-auto.svg` |
| `rel=icon` SVG | `brand/img/favicon.svg` | `img/icon.svg` |
| `rel=icon` PNG | `brand/img/favicon.png` (= forgejo `favicon.png`) | `img/icon-180.png` |
| `apple-touch-icon` | `brand/img/apple-touch-icon.png` | `img/apple-touch-icon-180.png` |
| `og:image` (absolute URL) | `brand/img/og.png` (= forgejo `logo.png`) | `img/logo-tile-512.png` |
| `/favicon.ico` | `static/favicon.ico` (keycloak) | `fixed/keycloak/img/favicon.ico` → `dest` `favicon.ico` |
| fonts | `brand/fonts/*.woff2` (5) | `fonts/atkinson-hyperlegible-{Regular,Italic,Bold,BoldItalic}.woff2`, `fonts/fredoka-latin.woff2` |
| licences | `brand/fonts/OFL-*.txt` | `fonts/OFL-atkinson-hyperlegible.txt`, `fonts/OFL-fredoka.txt`, `LICENSES.txt`, `LICENSE-CC-BY-4.0.txt` |

- [ ] **Step 1: Read the tag (read-only).**
  - `T=$SCRATCH/brand-tag; mkdir -p $T && GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 git clone -q --depth 1 --branch <tag> https://git.realliance.net/realliance/brand.git $T/brand`.
  - `git -C $T/brand cat-file -p <tag> | grep -c 'BEGIN PGP SIGNATURE\|BEGIN SSH SIGNATURE'` gives the format.
  - `git -C $T/brand verify-tag --raw <tag> 2>&1 | grep VALIDSIG` with Chris's keyring. Expected: `GOODSIG`/`VALIDSIG` from an Owner key; ledger the primary fingerprint.
  - `jq -r '.files | keys[]' $T/brand/dist/manifest.json`. Expected: every logical name in the table. If one is missing or renamed: **STOP—ask Chris (B1)**.
  - On 2026-11-04 the untagged build's Atkinson Regular was byte-identical to the preview's, while Fredoka and both logos differed. That makes Step 8 a real visual check.
- [ ] **Step 2: Signers.** Write the tag signer's armored public key (`gpg --armor --export <primary fpr>`) to `brand/release-signers.asc` and its 40-hex primary fingerprint to `brand/release-signers.fpr`. Expected: the same Owner fingerprint that verifies the inventory head (forge-bot ledger, `E9245FED…`). If it differs, ledger why.
- [ ] **Step 3: `brand/uses.txt`** from the table, one line per logical name, the favicon line as `fixed/keycloak/img/favicon.ico favicon.ico`.
- [ ] **Step 4: Finish `scripts/sync-brand.sh --tag <tag>`.**
  1. Clone the tag as in Step 1.
  2. Import `brand/release-signers.asc` into a `umask 077` temporary `GNUPGHOME`, then require a `VALIDSIG` line from `git verify-tag --raw` whose primary-key fingerprint (its last field) appears in `brand/release-signers.fpr`. Otherwise exit 1 with `no valid signature from a brand release signer`. If the real tag is SSH-signed, use the matching `allowed_signers` check instead. The other format always aborts (`unsupported signature format`).
  3. Require an annotated tag. Record `tag_object` and `commit` (`<tag>^{commit}`).
  4. For each `uses.txt` line: the logical name must be in the manifest; the dist file's sha256 must equal the manifest's; then copy it to `static/brand/<file>` or the fixed destination. Any miss exits 1.
  5. Empty `static/brand/` first, then write `brand/map.json` and `brand.lock`.

  Also add `--check --release`, delete `--stub` and its local paths, and update the header comment.
- [ ] **Step 5: Tokens and `build.ts`.**
  - Replace every literal brand path with `{{brand:<logical>}}`, for example `href="{{brand:img/icon.svg}}"`, `url("{{brand:fonts/fredoka-latin.woff2}}")`, `content="https://realliance.net{{brand:img/logo-tile-512.png}}"`.
  - `build.ts` loads `brand/map.json` and resolves tokens in the rendered HTML, the postcss output and every `static/**/*.html`. It copies other static files verbatim.
  - It throws if any `{{brand:` remains, a logical name is unknown, or a `/brand/[^"')\s]+` in the output is not a map value.
  - Keep `width=256 height=236` only if the tagged `img/logo-auto.svg` viewBox is still 360×332; otherwise compute the height from the viewBox. Do the same for the 72×36 mark.
- [ ] **Step 6: Sync and prove it.**
  - Run `scripts/sync-brand.sh --tag <tag>`. Expected: `sync-brand: <tag> verified and copied`.
  - Run `scripts/sync-brand.sh --check --release`. Expected: `static/ matches brand.lock (tag=<tag>)`.
  - Negatives, each in a scratch copy of the repo, each exiting 1: one flipped byte under `static/brand/` → `--check` fails; an empty `release-signers.fpr` → `--tag` fails on the signature; an unknown name added to `uses.txt` → `--tag` fails; a hand-inserted `{{brand:img/nope.svg}}` → `build.ts` fails.
- [ ] **Step 7: Build locally.**
  - Fetch Deno per Pins into an empty `$SCRATCH/tools` and check its sha256 (Task 4's `scripts/ci-tools.sh` once it exists).
  - Run `taskset -c 0-3 deno run --allow-read --allow-write --allow-env --allow-net --allow-import --allow-run --allow-sys --lock=deno.lock build.ts`, with `--frozen` once Task 4 has landed.
  - Expected: exit 0. `grep -c '{{brand:' build/index.html build/terminated.html build/brand.css` is `0`. Every `/brand/…` path in those files exists under `build/`.
- [ ] **Step 8: Visual check against the preview.**
  - Serve `build/` with a scratch `python -I` http server that adds the exact `csp-before.txt` header to **every** response, SVGs included.
  - chrome-devtools: `emulate` with `colorScheme` dark and light at viewport `1280x900x1`, and dark at `390x844x3,mobile,touch`; then `/terminated.html` in dark and light. `take_screenshot` each to `.superpowers/website/sync-*.png`.
  - Firefox headless, dark and light, with `layout.css.prefers-color-scheme.content-override` 0 and 1 in a scratch profile, for `/` → `sync-firefox-{dark,light}.png`.
  - `list_console_messages`: Expected no CSP violation. `list_network_requests`: Expected all same-origin.
  - Compare with `after-*.png`. Expected: identical layout; Fredoka may differ slightly.
  - **STOP—ask Chris**, with side-by-sides, if anything else visibly differs, or if the logo ink does not follow the scheme in either browser. Proposed fix for the ink: `<picture><source media="(prefers-color-scheme: dark)" srcset="{{brand:img/logo-mocha.svg}}"><img src="{{brand:img/logo-latte.svg}}" …></picture>` and the same for the mark, with no CSP change.
- [ ] **Step 9: Commit** (signed): `Sync brand assets from realliance/brand <tag> (signed; hashed names via brand/map.json)`. Push the branch if the branch already exists on the forge (Task 4); CI re-runs.

### Task 2: The forge repo and the history push

**Files:**
- Edit: `~/Repos/realliance-forge/repos.tf`, `README.md` (the critical-repos sentence names `realliance/realliance.net`)

**Interfaces:**
- Produces: `realliance/realliance.net`, holding GitHub's `main` at `24fd4fa`, the tags `1.0 1.1.0 1.1.1 1.2.0 1.2.1 1.2.2 1.2.3 2.0 2.1 2.2 2.3 2.3.1` and, per Q2, `chrmiller/{eddie,jared,meacham}-bio`. `main` has protection and `*` tags are Owners-only. In the local clone the remotes become `origin` = forge and `github` = GitHub.

- [ ] **Step 1: Read state (S1).**
  - `curl -sS -o /dev/null -w '%{http_code}' https://git.realliance.net/api/v1/repos/realliance/realliance.net`. Expected: `404` (the result on 2026-11-04).
  - `git -C ~/Repos/realliance-forge status` is clean at `origin/main`.
  - `tofu plan` at `main` (README env). Expected: `No changes.`
- [ ] **Step 2: Declare it.** On branch `realliance-net`, append the following to `repos.tf`, then run `tofu fmt -check && tofu validate`:

```hcl
# The Realliance website, realliance.net. Moved from github.com/realliance/realliance.net on
# 2026-11-04 with its full history; CI on the forge's `general` runner builds the image that
# luma-homeops pins by digest (cluster/realliance.net/home.yaml). Public. auto_init is off: the
# history is pushed into the empty repo BEFORE main is protected (first apply targets the repo only).
# The tag protection ("*", Owners) has no provider resource: set by hand.
resource "forgejo_repository" "realliance_net" {
  owner          = forgejo_organization.realliance.name
  name           = "realliance.net"
  description    = "The Realliance website, realliance.net"
  private        = false
  default_branch = "main"
  auto_init      = false

  has_actions  = true
  has_packages = true

  # same merge style as the inventory: a fast-forward lands the author's own signed commits as they are
  allow_fast_forward_only_merge = true
  default_merge_style           = "fast-forward-only"
  allow_merge_commits           = false
  allow_squash_merge            = false
  allow_rebase                  = false
  allow_rebase_explicit         = false

  lifecycle {
    prevent_destroy = true
  }
}

# Changes by PR only, signed commits, Owners merge.
resource "forgejo_branch_protection" "realliance_net_main" {
  repository_id          = forgejo_repository.realliance_net.id
  branch_name            = "main"
  enable_push            = false
  require_signed_commits = true
  required_approvals     = 0

  enable_merge_whitelist     = true
  merge_whitelist_teams      = ["Owners"]
  enable_approvals_whitelist = true
  approvals_whitelist_teams  = ["Owners"]
}
```

- [ ] **Step 3: Plan.** Run `tofu plan -out $SCRATCH/site-inventory.tfplan`. Expected: `Plan: 2 to add, 0 to change, 0 to destroy.`, naming exactly those two addresses. Ledger the summary.
- [ ] **Step 4: Commit, push the branch, open the PR.** Commit signed: `Declare realliance/realliance.net: the website, public, FF-only, protected main`. Then run `fj -H git.realliance.net pr create …` (check `fj pr create --help` for the flags).
- [ ] **Step 5: STOP—inventory apply.** This is pre-approved at Task 0 **only** if Step 3 matched exactly; otherwise ask.
  - Merge fast-forward: `fj -H git.realliance.net pr merge realliance-ops/forge#<n> -d`.
  - `git switch main && git pull --ff-only && git verify-commit HEAD`. Expected: a good signature from the Owner key, so the head is Owner-signed.
  - Re-plan at `main`. Expected: the same two adds.
- [ ] **Step 6: Apply 1 (D1).** Run `tofu apply -target=forgejo_repository.realliance_net`. Expected: the targeting warning and `Apply complete! Resources: 1 added, 0 changed, 0 destroyed.` Then the repo API returns 200 with `empty: true` and `private: false`.
- [ ] **Step 7: Push the history** from `~/Repos/realliance.net`:
  - `git remote rename origin github && git remote add origin https://git.realliance.net/realliance/realliance.net.git && git fetch github --prune --tags`.
  - Expected: `git ls-remote --tags github | wc -l` matches the 12 local tags, at the same shas.
  - `git push origin refs/remotes/github/main:refs/heads/main && git push origin --tags`.
  - Per Q2: `git push origin 'refs/remotes/github/chrmiller/*:refs/heads/chrmiller/*'`.
  - Check: `diff <(git ls-remote github | grep -v renovate/ | grep -v HEAD | sort) <(git ls-remote origin | grep -v HEAD | sort)`. Expected: empty. Forge `main` = `24fd4fa` (the alice commit ships).
- [ ] **Step 8: Old-workflow runs (D2).** `curl -sS 'https://git.realliance.net/api/v1/repos/realliance/realliance.net/actions/tasks?limit=50' | jq '[.workflow_runs[] | {status, name: .name}]'`. Expected: 0–7 runs `waiting`, from old tags' `publish.yml`. Ledger the count. Cancel them in the web UI (Actions → run → Cancel) or leave them for the 24 h abandoned-job cron.
- [ ] **Step 9: Apply 2.** Run `tofu apply`. Expected: `1 added, 0 changed, 0 destroyed` (the protection). Then `tofu plan`. Expected: `No changes.`
- [ ] **Step 10: Tag protection by hand.** As the break-glass administrator (token read from `forge/secrets.env` inside a `python -I` scratch script that prints only the status), call `POST /api/v1/repos/realliance/realliance.net/tag_protections` with `{"name_pattern": "*", "whitelist_teams": ["Owners"]}`. Expected: `201`.
- [ ] **Step 11: Verify.** `GET /api/v1/repos/realliance/realliance.net/branch_protections`. Expected: `main` with `enable_push: false`, `require_signed_commits: true`, merge whitelist `["Owners"]`. The repo settings show fast-forward-only. Ledger; commit and push the ledger.

### Task 3: The general runner

#### Part A: the image (luma-homeops, not a deploy)

**Files:**
- Create: `images/forgejo-runner-general/Dockerfile`, `.github/workflows/forgejo-runner-general-image.yml`
- Edit: `.renovaterc.json`

**Interfaces:**
- Produces: `ghcr.io/christopherjmiller/forgejo-runner-general:sha-<7>@sha256:<64>`, which contains forgejo-runner 13.2.1, node 24 (JS actions and `netpol-gate`), git, curl, unzip, tar, coreutils and ca-certificates, runs as uid 1000, and has no go, ko, gcc, docker or nix.

- [ ] **Step 1: Dockerfile.** Header comment: the general runner class seed; spec references; "Renovate bumps the FROM lines; the Deployment pin is a second, manual re-pin". Stages use the three Pins (runner, node, debian).
  - `apt-get install -y --no-install-recommends ca-certificates git curl unzip libstdc++6 && rm -rf /var/lib/apt/lists/* && useradd -u 1000 -m runner`.
  - Copy `forgejo-runner` and `node`; then `USER 1000:1000`, `WORKDIR /home/runner`, `ENTRYPOINT ["forgejo-runner"]`.
- [ ] **Step 2: Workflow.** Copy `.github/workflows/forgejo-runner-image.yml` and change three things:
  - paths become `images/forgejo-runner-general/**` and the workflow itself; `IMAGE: ghcr.io/${{ github.repository_owner }}/forgejo-runner-general`;
  - the context becomes `images/forgejo-runner-general`;
  - the smoke test asserts `forgejo-runner --version | grep -F 13.2.1`, `node --version | grep -E '^v24\.'`, `git --version`, `curl --version`, `unzip -v >/dev/null`, `tar --version`, `sha256sum --version`, `test "$(id -u)" = 1000`, and `! command -v docker && ! command -v nix && ! command -v go && ! command -v gcc`.
- [ ] **Step 3: Renovate.** Add a `kubernetes` rule for `ghcr.io/christopherjmiller/forgejo-runner-general` with `automerge: false`. Its description follows the existing runner rule: every job can read the connection token, so a new image changes the CI trust boundary. Rebase first (F1).
- [ ] **Step 4: Commit and push** (routine): `forgejo-runner-general: slim runner image (no Go/ko/gcc) for the general runner`.
- [ ] **Step 5: Get the pin.** Watch `gh run watch` with `Monitor`. Expected: success. Then `gh api /users/ChristopherJMiller/packages/container/forgejo-runner-general/versions --jq '.[0] | {name, tags: .metadata.container.tags}'`. Expected: `sha-<7>` and `latest`.
- [ ] **Step 6: Anonymous pull check.** `curl -sS 'https://ghcr.io/token?scope=repository:christopherjmiller/forgejo-runner-general:pull' | jq -r .token`, then a manifest HEAD with it. Expected: `200`. On `401`: **STOP—ask Chris** to set the package public in the GitHub UI (no API exists), then retry.

#### Part B: registration and deploy (no network policy yet)

**Files:**
- Create: `cluster/forgejo-runner-general/{kustomization,namespace,pvc,deployment}.yaml`, `runner-config.secret.yaml` → `runner-config.yaml` (SealedSecret), `cluster/applications/forgejo-runner-general.yaml`, `scripts/test_runner_general.py`
- Edit: `cluster/applications/kustomization.yaml`, `.pre-commit-config.yaml` (hook `realliance-net`)
- Create (sso): `tests/forge/test_forge_runner_general.py`
- Private: `forge/runner.env` (`RUNNER_GENERAL_REALLIANCE_NET_SECRET`)

**Interfaces:**
- Consumes: the image pin (Part A); `realliance/realliance.net` (Task 2); `manifests`, `kube`, `forge_api`, `push_probe_workflow`, `delete_branch`, `galaxy_only`, `_load_env`, `FORGE_SECRETS` and `LUMA` from `tests/forge_helpers.py` (F2).
- Produces: Deployment `forgejo-runner-general/forgejo-runner-general`, label `general`, serving `realliance/realliance.net` only. Its `/work` holds `act/`, `home/`, `tmp/` and `.runner` (unused), and there is no cache dir.

- [ ] **Step 1: Write the failing static tests** in `scripts/test_runner_general.py`. Its docstring says it never reads `*.secret.*`. It renders with `kubectl kustomize`, like `test_netpol_manifests.py`'s helper.
  - `test_namespace_restricted`: `pod-security.kubernetes.io/enforce == "restricted"`.
  - `test_pod_hardened`: the forge-bot plan's `test_runner_manifests_unprivileged` set, plus `readOnlyRootFilesystem`, `replicas == 1` and `strategy Recreate`. Requests `cpu: 100m, memory: 256Mi`; limits `cpu: "1", memory: 2Gi`.
  - `test_image_pinned`: matches `^ghcr\.io/christopherjmiller/forgejo-runner-general:sha-[0-9a-f]{7}@sha256:[0-9a-f]{64}$`.
  - `test_start_wipes_job_state`: the command contains `rm -rf /work/home /work/tmp /work/act`.
  - `test_nothing_shared_with_forgejo_runner`: namespace, PVC claim, Secret name and pod labels all differ from `cluster/forgejo-runner`'s render; no volume names `forgejo-runner-work` or `forgejo-runner-config`; the image repository differs.
  - `test_app_registered`: `forgejo-runner-general.yaml` has the shape of `forgejo-runner.yaml` (path, namespace, prune + selfHeal + allowEmpty, CreateNamespace) and appears in the applications kustomization.
  - `test_renovate_never_automerges_runner`: the Part A rule.
  - Hook `realliance-net` runs `python -m pytest -q -p no:cacheprovider scripts/test_runner_general.py` (Task 6 adds its second file), on files `^(cluster/(forgejo-runner-general|forgejo-runner|realliance\.net|applications|components/netpol)/.*|images/forgejo-runner-general/.*|scripts/test_(runner_general|realliance_net_pin)\.py|\.renovaterc\.json|\.pre-commit-config\.yaml)$`.
  - Run. Expected: FAIL (no `cluster/forgejo-runner-general`).
- [ ] **Step 2: Write the failing live tests** in sso `tests/forge/test_forge_runner_general.py`, on branch `runner-general`:
  - `test_general_runner_registered_for_site_only`. `GET /repos/realliance/realliance.net/actions/runners`: exactly one runner named `general-realliance-net` with labels `{general}`. `GET /repos/realliance/forge-bot/actions/runners`: no `general-*` runner. The `galaxy` runner is absent from the site repo.
  - `test_general_runner_holds_no_other_credentials` (`galaxy_only`). For `FORGEJO_ADMIN_TOKEN`, `FORGE_BOT_FORGEJO_TOKEN`, `forge_bot_client_secret`, `RUNNER_FORGE_BOT_SECRET` and the `ci-registry` token: `kube -n forgejo-runner-general exec -i deploy/forgejo-runner-general -- sh -c 'grep -rqsF -f /dev/stdin /etc /work /proc/1/environ'` with the value on stdin returns a code in (1, 2). Include a positive control. Symmetrically, `RUNNER_GENERAL_REALLIANCE_NET_SECRET` is not readable in `forgejo-runner`.
  - `test_general_probe_job_hardened_and_wiped`. Use `push_probe_workflow("realliance/realliance.net", "general", script)` with the script `test "$(id -u)" = 1000; test ! -e /var/run/docker.sock; test ! -e /var/run/secrets/kubernetes.io; curl --version; unzip -v >/dev/null; node --version; ! command -v go; test -z "$(find /work/act -name 'leftover-*' 2>/dev/null)"; touch "$GITHUB_WORKSPACE/leftover-$GITHUB_RUN_NUMBER"`. Push it twice; both runs must reach `success`. `delete_branch` in `finally`.
  - Run with `nix develop --command pytest tests/forge/test_forge_runner_general.py -v`. Expected: FAIL (no runner).
- [ ] **Step 3: Capacity reads (S1).** `ceph df`: `block-pool-ssd` keeps ≥ 40 GiB after 5Gi (Expected about 133 GiB). The node the scheduler picks (middle or bottom) stays < 90 % memory requested after +256Mi (Expected about 86 % or 88 %). Otherwise **STOP—ask Chris**. Ledger the numbers.
- [ ] **Step 4: STOP—mint the runner credential.** Name the repo scope, the label and where the secret lives. On approval:
  - In `nix develop ~/Repos/realliance-sso`, from `~/Repos/realliance-sso-secrets`: `umask 077; printf 'RUNNER_GENERAL_REALLIANCE_NET_SECRET=%s\n' "$(openssl rand -hex 20)" >> forge/runner.env`. Check that the value is non-empty without printing it (`grep -c '^RUNNER_GENERAL_REALLIANCE_NET_SECRET=[0-9a-f]\{40\}$' forge/runner.env` → `1`). Signed commit, push.
  - Register, exactly in Task 7's verified form: `(. forge/runner.env; printf %s "$RUNNER_GENERAL_REALLIANCE_NET_SECRET") | kubectl -n forgejo exec -i deploy/forgejo -c forgejo -- forgejo forgejo-cli actions register --name general-realliance-net --scope realliance/realliance.net --labels general --secret-stdin=-`. Expected: exit 0, one UUID on stdout. Ledger it (the value is not secret).
  - Do **not** use `create-runner-file`: in v13.2.1 it takes the secret only in argv and counts as deprecated (Task 7 report). Derive the connection the way Task 7 did: `uuid = UUID(bytes=secret[:16].encode())`, `token = secret`. Assert that the derived uuid equals the printed UUID.
- [ ] **Step 5: Write the manifests.**
  - `namespace.yaml`: PSA `restricted`, with a comment.
  - `pvc.yaml`: 5Gi, `rook-ceph-block-ssd` (reclaim `Retain`: deleting the PVC leaves a Released PV, S4).
  - `deployment.yaml`: a copy of `cluster/forgejo-runner/deployment.yaml` with these changes:
    - names and labels `forgejo-runner-general`; the Part A image; env only `HOME=/work/home`, `TMPDIR=/work/tmp`; the Step 1 resources;
    - node affinity `NotIn [top]` only;
    - header comment: what persists, the shared `HOME`/`TMPDIR`/`~/.cache/act` within one pod lifetime, the accepted risk with its spec reference, `never registered for realliance/forge-bot`, and `NetworkPolicy: none until Part C`.
  - `kustomization.yaml`: namespace plus the four resources, with a comment naming the allowlist (v1: `realliance/realliance.net`).
  - `runner-config.secret.yaml`: a scratch `python -I` script (umask 077, no argv secrets, prints only a byte count) reads `forge/runner.env` and writes the Secret with the `config.yml` from Global Constraints and the single connection `general-realliance-net: {url: https://git.realliance.net/, uuid, token}`, in the format `forgejo-runner generate-config` (v13.2.1) prints.
  - Seal with `KUBECONFIG=/tmp/galaxy-kubeconfig nix develop --command ./sign.sh`. Expected: `runner-config.yaml` with `kind: SealedSecret`. `git check-attr -a cluster/forgejo-runner-general/runner-config.secret.yaml` reports git-crypt.
  - `cluster/applications/forgejo-runner-general.yaml` and its line in the applications kustomization.
- [ ] **Step 6: Static checks.** Run `nix develop --command bash -c 'python -m pytest -q -p no:cacheprovider scripts/test_runner_general.py scripts/test_netpol_manifests.py && scripts/check-kustomizations.sh && kubectl kustomize cluster/forgejo-runner-general | grep -c "^kind:"'`. Expected: all pass, and `4`.
- [ ] **Step 7: Commit** (signed): `forgejo-runner-general: second runner (label general, realliance/realliance.net only; restricted PSA; no shared cache)`. Check with `git diff --cached --no-textconv --stat`.
- [ ] **Step 8: Gate G `before`, N2 and N5, then STOP—deploy push.** Say plainly: unconfined egress until Part C, the same as forge-bot's runner today (Q3). Watch: app Synced/Healthy and pod 1/1 Ready. Rollback triggers: CrashLoop, the runner not declaring within 5 min, or any Gate G4 change. Rollback is `git revert`, then push.
- [ ] **Step 9: Push and `Monitor`** until `forgejo-runner-general` shows Synced/Healthy and its pod is Ready (timeout 10 min).
  - `kubectl -n forgejo-runner-general logs deploy/forgejo-runner-general | grep -E 'declared successfully|\[poller\] launched'`. Expected: both lines, for `general-realliance-net`.
  - The repo runners API shows it `online` with labels `["general"]`.
- [ ] **Step 10: Verify.**
  - Run the Step 2 suite. Expected: 3 passed.
  - `kubectl -n forgejo-runner-general top pod`. Ledger the idle use.
  - Gate G `after`. Expected: identical apart from the new namespace.
  - If the NP soak or A3b is live: `scripts/netpol-verify.sh idle`. Expected: still `0 NetworkPolicies`, `packet_id=0`.
  - Ledger this, and add one line to the NP ledger (N2).
  - Commit the sso tests and open the PR (merge on Chris's word).

#### Part C: network policy (after the netpol plan's `B2`)

**Files:**
- Edit: `cluster/forgejo-runner-general/kustomization.yaml` (`components:` with the three NP paths, plus a comment naming netpol spec §6.3), `deployment.yaml` (`initContainers: [netpol-gate]` copied verbatim from `cluster/forgejo-runner/deployment.yaml`, and resources from measurement), `scripts/test_runner_general.py`

- [ ] **Step 1: Preconditions.**
  - The NP ledger has `B2_done`, and `scripts/netpol-verify.sh runner` and `canary` PASS.
  - Runner idle: `curl -sS 'https://git.realliance.net/api/v1/repos/realliance/realliance.net/actions/tasks?limit=20' | jq '[.workflow_runs[] | select(.status=="running" or .status=="waiting")] | length'`. Expected: `0`.
  - N3 (no NP allow-list edit is pending).
- [ ] **Step 2: Write the failing tests.**
  - `test_policies_equal_forgejo_runner`: the rendered NetworkPolicies of `forgejo-runner-general`, with the namespace normalised, equal `forgejo-runner`'s (names, selectors, egress rules), and no rule allows ingress.
  - `test_gate_equals_forgejo_runner`: the `netpol-gate` command, resources and securityContext equal forgejo-runner's; its image equals this Deployment's runner image.
  - Run. Expected: 2 FAIL.
- [ ] **Step 3: Write the change**, keeping the gate's two NP comments. Set the requests from Task 4's measurement: memory `max(256Mi, peak working set during a site build × 1.25)` and CPU `max(100m, idle × 1.25)`. Update `test_pod_hardened`'s expected values to match. Run the tests and kustomize. Expected: PASS. Commit: `forgejo-runner-general: default-deny + DNS + forge/internet egress, netpol-gate (netpol §6.3 components)`.
- [ ] **Step 4: Gate G and N5, then STOP—NetworkPolicy push.** Rollback triggers: the runner cannot fetch tasks (revert), or the policy blocks a CI dependency (revert; the fix goes through the NP owner).
- [ ] **Step 5: Push and `Monitor`** until Ready.
  - `kubectl -n forgejo-runner-general logs deploy/forgejo-runner-general -c netpol-gate`. Expected: zero or more `egress not enforced yet; waiting` lines; ledger the count.
  - The runner declares and launches its poller.
- [ ] **Step 6: Verify.**
  - If `netpol-verify.sh runner` accepts a namespace by then, run it for this namespace. Otherwise run the `all`-scope rows of `cluster/netpol-canary/matrix.txt`, read-only, through `kubectl -n forgejo-runner-general exec deploy/forgejo-runner-general -c runner -- node -e "$(cat scripts/netpol-connect.js)" …`. Expected: every `dropped` row is `timeout`, and `git.realliance.net:443`, `github.com:443` and DNS are `open`.
  - `scripts/netpol-verify.sh stray`. Expected: PASS, with `forgejo-runner-general` policed.
  - Re-run the last realliance.net CI run. Expected: `success`.
  - Gate G after.
  - Ledger it; add the N4 line to the NP ledger.

### Task 4: CI (realliance.net, branch `brand`)

**Files** (in `~/Repos/realliance.net`):
- Create: `.forgejo/workflows/ci.yaml`, `scripts/ci.sh`, `scripts/ci-tools.sh`, `scripts/build-image.sh`, `scripts/check-image.sh`
- Edit: `build.ts` (content-hashed member image names, pinned imports), `deno.lock` (updated once for the pinned specifiers), `Justfile` (`--frozen`; `image` target), `README.md` (developing, image, releasing)
- Delete: `Dockerfile`, `.dockerignore`, `.github/`, `.renovaterc.json`
- Forge: repo secret `SITE_REGISTRY_TOKEN`. Private: `forge/secrets.env` (`REALLIANCE_NET_REGISTRY_TOKEN`)

**Interfaces:**
- `scripts/ci-tools.sh <dir>`: downloads Deno and crane per Pins with `curl -fsSL --proto '=https' --tlsv1.2`, checks each with `sha256sum -c --strict`, extracts only `deno` (unzip) and `crane` (tar) into `<dir>`, and prints `deno --version | head -1` and `crane version`. Any mismatch exits 1 before it extracts anything.
- `scripts/build-image.sh <out-dir>`, run after `build/` exists:
  1. Stage `build/` as `usr/share/nginx/html/` and `nginx.conf` as `etc/nginx/nginx.conf`.
  2. Write a deterministic layer: `tar --sort=name --format=gnu --numeric-owner --owner=0 --group=0 --mtime="@$(git log -1 --format=%ct)" --mode='a+rX,u+w,go-w' -C <stage> -cf <out>/layer.tar etc usr`.
  3. Start `crane registry serve --address 127.0.0.1:<random port>`, killed by `trap`.
  4. `crane append --platform linux/amd64 -b <nginx pin> -f <out>/layer.tar -t 127.0.0.1:<p>/site:appended`.
  5. `crane mutate 127.0.0.1:<p>/site:appended --exposed-ports 8080/tcp -l org.opencontainers.image.source=https://git.realliance.net/realliance/realliance.net -l org.opencontainers.image.revision=<sha> -t 127.0.0.1:<p>/site:built`.
  6. `scripts/check-image.sh 127.0.0.1:<p>/site:built`.
  7. `crane pull --format=oci … <out>/oci`, and `crane digest …` to `<out>/digest`.
- `scripts/check-image.sh <ref>`:
  - `crane config <ref>` satisfies: `.os=="linux"`, `.architecture=="amd64"`, `.config.ExposedPorts=={"8080/tcp":{}}`, `.config.Entrypoint==["/docker-entrypoint.sh"]`, `.config.Cmd==["nginx","-g","daemon off;"]`, `.config.StopSignal=="SIGQUIT"`, `(.config.User//"")==""`, `NGINX_VERSION=1.20.2` present, and the source label as given earlier.
  - The manifest's layers are the base's layers, unchanged, plus exactly one more.
  - The last layer (`crane blob`) lists only `etc/`, `etc/nginx/`, `etc/nginx/nginx.conf` and paths under `usr/share/nginx/html/`. Its `nginx.conf` is byte-identical to the repo's. It holds `index.html`, `terminated.html`, `favicon.ico`, `brand.css`, `tailwind.css`, every `brand.lock` path, and one `members/*.jpg` per `members/*/about.toml`.
- `scripts/ci.sh build|publish`.
  - `build` (no credentials): ci-tools into `.ci/bin`; `DENO_DIR=.ci/deno`, `DENO_NO_UPDATE_CHECK=1`; `sync-brand.sh --check`, plus `--release` when `GITHUB_REF` is `refs/tags/*`; the frozen Deno build; `build-image.sh .ci/image`.
  - `publish` (needs `SITE_REGISTRY_TOKEN`):
    - the repository is `${SITE_IMAGE_REPO:-git.realliance.net/realliance/realliance.net}`; the override exists only for Step 6's local test; `crane auth login` runs only when the host is `git.realliance.net`;
    - the tag list is `sha-${GITHUB_SHA::7}`, plus `${GITHUB_REF#refs/tags/}` when the ref is a tag, which must match `^[0-9]+\.[0-9]+\.[0-9]+$` or the job exits 1;
    - `DOCKER_CONFIG=$(mktemp -d)` with an EXIT trap, then `crane auth login git.realliance.net -u forge-ci --password-stdin <<<"$SITE_REGISTRY_TOKEN"`;
    - for each tag: if `crane digest` reports `MANIFEST_UNKNOWN`/`NAME_UNKNOWN`, `crane push .ci/image/oci <repo>:<tag>`; if it reports any other error, exit 1; if it returns a digest different from `.ci/image/digest`, exit 1 with `refusing to move <tag>`;
    - after each, `crane digest` must equal the built digest;
    - append `<repo>:<tag>@<digest>` to `$GITHUB_STEP_SUMMARY` if the variable exists, and print it.
- `.forgejo/workflows/ci.yaml`:
  - `on: {push: {branches: [main], tags: ["*.*.*"]}, pull_request: {}}`; one job `build` with `runs-on: general`;
  - steps: checkout (Pins), then `scripts/ci.sh build`, then `scripts/ci.sh publish` with `if: github.event_name == 'push'` and `env: {SITE_REGISTRY_TOKEN: ${{ secrets.SITE_REGISTRY_TOKEN }}}`;
  - header comment: the Forgejo 15 differences, the accepted risk with its spec reference, and "NEVER approve fork PR runs".

- [ ] **Step 1: Read what `the current image has`.** With the pinned crane in an empty `$SCRATCH/tools`:
  - `crane export quay.io/realliance/realliance.net@sha256:7405276fcd58b6ade36c76147a23dfaba6eac6287880c14e37160d032961e359 - | tar -xO etc/nginx/nginx.conf | diff - nginx.conf`. Expected: empty (D6).
  - `crane export docker.io/library/nginx@sha256:f6609f898bcdad15047629edc4033d17f9f90e2339fb5ccb97da267f16902251 - | tar -xO etc/nginx/mime.types | grep -E 'svg|woff2'`. Ledger the result: it decides Task 7's content-type expectations.
- [ ] **Step 2: Determinism (D7).** In `build.ts`, name member images `members/<first 16 hex of sha256(encoded jpg)>.jpg`. Pin `pug@v0.1.6`, `twd@v0.4.8` and `recursive_readdir@v2.0.0`. Run `deno cache --lock=deno.lock build.ts` once, then build with `--frozen`. Expected: `git diff --stat deno.lock` touches only the `redirects` entries.
- [ ] **Step 3: Write the scripts, the workflow and the README.** Delete the D4 files.
- [ ] **Step 4: Local proof (nothing pushed).**
  - Run `taskset -c 0-3 scripts/ci.sh build` twice, from `git clean -xfd` (after checking that `git status` is clean). Expected: `check-image: PASS` both times and the same `sha256:` in `.ci/image/digest` both times.
  - `crane config` of the built image `jq`-diffed against the live quay config. Expected: only `ExposedPorts`, the two new labels and `rootfs`/`history` differ.
- [ ] **Step 5: Measure.** While Step 4 runs, ledger the peak RSS (`/usr/bin/time -v`). Part C uses it, together with the runner pod's Prometheus `container_memory_working_set_bytes` during Step 9.
- [ ] **Step 6: Prove `never move`.**
  - With a scratch local `crane registry serve` standing in for the forge, push an unrelated image to `site:sha-<7>`.
  - Run `SITE_IMAGE_REPO=127.0.0.1:<p>/site GITHUB_SHA=$(git rev-parse HEAD) GITHUB_REF=refs/heads/main scripts/ci.sh publish`. Expected: exit 1 with `refusing to move sha-<7>`.
  - Then `publish` against an empty scratch registry. Expected: success, and the remote digest equals the built one.
- [ ] **Step 7: Job-token probe (D13), before minting anything.**
  - Push branch `ci-token-probe` with a one-off workflow (`runs-on: general`). It builds a minimal image into a local `crane registry serve`, then runs `crane auth login git.realliance.net -u "$GITHUB_ACTOR" --password-stdin <<<"${{ github.token }}"`, then `crane copy … git.realliance.net/realliance/realliance.net:token-probe-<hex>`. The job **succeeds only if the forge refuses the push**.
  - Expected: `success` with a 401/403/`denied` in the log. Ledger "Forgejo 15.0.11: the job token cannot push packages", delete the branch, and continue to Step 8.
  - If the push succeeded: delete that package version (administrator `DELETE /api/v1/packages/realliance/container/realliance.net/token-probe-<hex>`), ledger it, and **STOP—ask Chris** whether publish should use the job token instead.
- [ ] **Step 8: STOP—mint the registry token** (pre-approved at Task 0 if the forge refused the push in Step 7).
  - `umask 077; T=$(kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin user generate-access-token --username forge-ci --token-name realliance-net-registry --scopes write:package --raw); printf 'REALLIANCE_NET_REGISTRY_TOKEN=%s\n' "$T" >> ~/Repos/realliance-sso-secrets/forge/secrets.env; unset T`. Check the shape without printing (`grep -c '^REALLIANCE_NET_REGISTRY_TOKEN=.\{20,\}$'` → `1`). Signed commit to sso-secrets, push.
  - A scratch `python -I` script reads the administrator token and this token from `secrets.env` and calls `PUT /api/v1/repos/realliance/realliance.net/actions/secrets/SITE_REGISTRY_TOKEN` with `{"data": …}`, printing only the status. Expected: `201` or `204`.
  - Reach (D9): with this token, push a minimal image to `realliance/realliance.net:scope-probe-<hex>`. Expected: accepted. Then delete that version (administrator API). Ledger `write:package, every realliance package (B0 outcome B)`.
- [ ] **Step 9: Commit and push the branch; open the PR.**
  - Signed commits: `Reproducible build: content-hashed member images, pinned imports, --frozen` and `CI on the forge: Deno + crane image build on the general runner; drop Docker and GitHub CI`.
  - Re-sign the restyle commit first (D3): `git rebase --exec 'git commit --amend -S --no-edit' 24fd4fa` (signs every commit on the branch). Expected: `git log --format='%h %G?' 24fd4fa..brand` shows `G` or `U` for all.
  - `git push origin brand`; `fj -H git.realliance.net pr create …` (base `main`, head `brand`), body: report.md summary, the D-list, the testing done.
  - `Monitor` the `pull_request` run. Expected: `success`, with `check-image: PASS` and a digest in the log. The job pushes nothing: the package list on the forge is still empty or holds only the deleted probes.
  - Ledger the run id and duration.

### Task 5: release 2.4.0

**Interfaces:**
- Consumes: the PR (Task 4), with Task 1 landed (`brand.lock` names the signed tag).
- Produces: `git.realliance.net/realliance/realliance.net:2.4.0` and `:sha-<7>`, both at digest `D`; the package linked to the repo; `D` cached on all three nodes.

- [ ] **Step 1: Pre-merge checks.**
  - `git log --show-signature main..brand`. Expected: every commit has a good Owner signature.
  - The PR's latest run is `success` at the PR head.
  - `scripts/sync-brand.sh --check --release`. Expected: PASS.
  - Gate G1 (CI uses the cluster).
- [ ] **Step 2: Merge** (start OK, Q4): `fj -H git.realliance.net pr merge realliance/realliance.net#<n> -d`.
  - Expected: `main` equals the PR head, fast-forward, and the signatures stay the same.
  - `Monitor` the `push` run on `main`. Expected: `success`, publishing `sha-<7>@sha256:D`.
- [ ] **Step 3: Tag.** `git fetch origin && git tag -s 2.4.0 -m "realliance.net 2.4.0: Realliance brand restyle, built on the forge" origin/main && git push origin 2.4.0`.
  - `Monitor` the tag run. Expected: `success`, publishing `2.4.0`, with `sha-<7>` confirmed at the same `D`.
  - `refusing to move` means the build is not reproducible: **STOP**, investigate, and never re-pin from it.
- [ ] **Step 4: Anonymous checks from rowlett (no credentials).**
  - `crane digest git.realliance.net/realliance/realliance.net:2.4.0`. Expected: `D`.
  - `curl -sSI` a HEAD on the manifest (realm from `curl -sSI https://git.realliance.net/v2/`, anonymous pull token). Expected: `200`, `docker-content-digest: D`, a Docker v2 manifest content-type.
  - A GET on the manifest with `-D -` and no redirect following. Expected: `307` to `s3.us-east-005.backblazeb2.com`. Following it, `sha256sum` of the body equals `D`.
  - `scripts/check-image.sh git.realliance.net/realliance/realliance.net:2.4.0`. Expected: PASS.
- [ ] **Step 5: Link the package.** Administrator `POST /api/v1/packages/realliance/container/realliance.net/-/link/realliance.net`. Expected: `201`.
- [ ] **Step 6: The release review for the re-pin** (forge-bot spec, "Re-pin"): `git verify-tag 2.4.0`, `git verify-commit 2.4.0^{commit}`, and `git log --stat 2.3..2.4.0` plus `git diff --stat 2.3 2.4.0`. Summarise them for Task 6's STOP.
- [ ] **Step 7: Pre-pull on every node (D12; Review Focus 4).** Run `talosctl … -n <ip> -e <ip> image pull git.realliance.net/realliance/realliance.net:2.4.0@sha256:D` for `192.168.0.241`, then `.242`, then `.240`.
  - Expected: each succeeds, and `talosctl -n <ip> image list | grep -c <D's first 12 hex>` ≥ 1.
  - If the client (v1.14.1) refuses to talk to a v1.10.9 node, ledger it. The rollout is then the test: `maxUnavailable: 1` keeps one old pod serving.
  - Any other failure: ledger the error and **STOP—ask Chris**. Do not re-pin.

### Task 6: The re-pin (luma-homeops)

**Files:**
- Edit: `cluster/realliance.net/home.yaml` (the image line, plus a comment on the line before it), `.renovaterc.json`, `.pre-commit-config.yaml` (the hook also runs the new file)
- Create: `scripts/test_realliance_net_pin.py`

- [ ] **Step 1: Write the failing tests.**
  - `test_site_image_pinned_to_forge`: the `home` container image matches `^git\.realliance\.net/realliance/realliance\.net:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$`.
  - `test_rest_of_the_deployment_unchanged`: explicit values, not a git-sha comparison: `replicas: 2`, RollingUpdate `maxUnavailable: 1`, containerPort 8080, requests and limits `5Mi`/`5m`, pod and container `runAsUser: 101`/`runAsGroup: 101`, `readOnlyRootFilesystem: true`, the `/tmp` Memory emptyDir, liveness `httpGet /health :8080`, and preStop `[/usr/sbin/nginx, -s, quit]`. The Service, the Ingresses and their `realliance-net-set-csp@kubernetescrd` middleware annotation stay the same.
  - `test_renovate_never_automerges_site`: a `kubernetes` rule with `matchPackageNames == ["git.realliance.net/realliance/realliance.net"]` and `automerge: false`, after every rule or preset that could match it (the forge-bot guard's ordering idea).
  - Run. Expected: FAIL.
- [ ] **Step 2: Write the change.**
  - Image: `git.realliance.net/realliance/realliance.net:2.4.0@sha256:D`.
  - Comment on the line before it: built by Forgejo Actions in `realliance/realliance.net` on the `general` runner; manual re-pin after `git verify-tag` and review (CLAUDE.md sharp edge 6); anonymous pulls go through the WAN hairpin and are 307'd to B2, so **new pods cannot pull while Forgejo is down** (pre-pulled on every node at release); rollback is `git revert` of the re-pin (`quay.io/…:2.3@sha256:7405276f…` stays published).
  - The Renovate rule's description says why: any `realliance` package token can re-push a tag.
  - Rebase first (F1). Run the tests and kustomize. Expected: PASS.
- [ ] **Step 3: Commit** (signed): `realliance.net: 2.4.0 from the forge registry (brand restyle; reviewed 2.3..2.4.0, sha256:D)`.
- [ ] **Step 4: Gate G "before" and N5, then STOP—re-pin push.**
  - Evidence: the CI run ids, `D`, the anonymous checks, the three pre-pulls, the Task 5 Step 6 review, and the Task 1 screenshots.
  - Watch: the rollout to 2/2 at the new ReplicaSet, and Argo Synced/Healthy.
  - Rollback triggers: a new pod not Ready within 5 min; CrashLoop, OOMKilled or ImagePullBackOff; any non-200 for `/`, `/health` or `/terminated.html`; a changed CSP; visible breakage. Then revert and push, and this approval covers both.
- [ ] **Step 5: Push and `Monitor`** (safe-rollout skill) until `deploy/home` shows `2/2` updated and available, with both pods' imageID `…@sha256:D` (timeout 10 min).
- [ ] **Step 6: Gate G `after`.** Expected: the edge diff is empty for every host, the `realliance.net` and `www.realliance.net` rows included (status, content-type and CSP prefix unchanged).

### Task 7: live verification

**Files:** none; results go to the ledger, and the screenshots to `.superpowers/website/live-*.png`.

- [ ] **Step 1: Headers.**
  - `curl -sSI https://realliance.net/` and `https://www.realliance.net/`. Expected: `200`, `content-type: text/html`, a `content-security-policy` line byte-equal to `$SCRATCH/csp-before.txt` (`diff`), `server: nginx` and a fresh `last-modified`.
  - `git log --oneline -- cluster/traefik` shows no commit from this plan.
  - `curl -sS -D - https://realliance.net/health`. Expected: `200`, `content-type: text/plain`, body `alive`.
  - `curl -sSI https://realliance.net/terminated.html` → `200`; `/favicon.ico` → `200`.
  - Repeat `/` from outside the LAN (the SSO VPS, as `tests/forge` does). Expected: the same status and CSP.
- [ ] **Step 2: Every brand asset.**
  - For each `/brand/…` URL in the live `index.html`, `terminated.html` and `brand.css`, `curl -sS -o f -w '%{http_code} %{content_type}'`. Expected: `200`, the content-type from Task 4 Step 1 (`image/svg+xml`, `image/png`, the woff2 type nginx 1.20.2 has), and `sha256sum f` equal to `brand.lock`.
  - A preview-era path (`/brand/img/logo.svg`). Expected: `404`, which shows the hashed names are live.
- [ ] **Step 3: Screenshots** (chrome-devtools, live).
  - `emulate` `colorScheme` dark and light at `1280x900x1`; dark and light at `390x844x3,mobile,touch`; `/terminated.html` dark and light → `.superpowers/website/live-*.png`.
  - Firefox headless dark and light of `/` (Task 1 Step 8 method).
  - Expected: they match `sync-*.png`; the logo ink follows the scheme in both browsers; `list_console_messages` shows no CSP violation; `list_network_requests` shows only same-origin requests.
- [ ] **Step 4: Content.**
  - The members are in the order alice, chris, connorjc, eddie, and alice's card reads `she/it` with `Virtualized Storage, Prolog, libMahjong`.
  - At `390x600` the last member card is reachable by scrolling (the `overflow:hidden` fix).
  - The hero logo and h1 "Realliance" are present.
- [ ] **Step 5: Pods after 30 min.** `kubectl -n realliance-net get pods` shows restarts `0`, with no `OOMKilled` in `describe`. `kubectl -n realliance-net top pods` stays well under the 5Mi limit (Expected about 2Mi).
- [ ] **Step 6: Ledger and report** to Chris (links to the screenshots). Commit and push the ledger.

### Task 8: archive the GitHub repo

**Files:**
- Edit (GitHub `realliance/realliance.net`, branch `moved-to-forge` → `main`): `README.md` only

- [ ] **Step 1: Read state.**
  - `gh api repos/realliance/realliance.net --jq '{archived, default_branch}'`. Expected: not archived.
  - The hooks, through the credential-stripping `jq` in Global Constraints. Expected: one `push` hook to `quay.io`.
  - `gh pr list` → none. `gh issue list` → #17 `Dependency Dashboard`.
  - Branch protection on `main`: 1 approval, `enforce_admins: false`.
- [ ] **Step 2: Prepare the pointer.** On branch `moved-to-forge` from `github/main`, replace `README.md` with:

```markdown
# realliance.net — moved

The Realliance website now lives on the Realliance forge:
**https://git.realliance.net/realliance/realliance.net**

It is built and released from there. This GitHub repository is archived (read-only) and is not
updated. Its full history and every tag were carried over to the forge.
```

  Commit it signed and push the branch to `github`.
- [ ] **Step 3: STOP—archive.** List the five actions, in order:
  1. Delete the Quay push webhook (`gh api -X DELETE repos/realliance/realliance.net/hooks/<id>`), so the README merge triggers no Quay build. Its credential lived in that URL (Q5).
  2. `gh pr create --fill` and `gh pr merge --squash --admin --delete-branch` (the administrator override of the 1-approval rule).
  3. Close #17 with a pointer comment.
  4. `gh repo edit realliance/realliance.net --description "Moved to https://git.realliance.net/realliance/realliance.net" --homepage https://realliance.net`.
  5. `gh repo archive realliance/realliance.net --yes`.

  Rollback: `gh repo unarchive`. The webhook is not re-created.
- [ ] **Step 4: Execute** the five actions in order.
- [ ] **Step 5: Verify.**
  - `gh api repos/realliance/realliance.net --jq .archived` → `true`; hooks `[]`; the README on github.com shows the pointer.
  - The rollback target still serves anonymously: `crane digest quay.io/realliance/realliance.net:2.3`. Expected: `sha256:7405276f…`.
  - Ledger; commit and push the ledger.

### Task 9: docs

**Files:**
- Edit: `docs/forgejo.md` (new section), `CLAUDE.md` (topology row; sharp edge 6)

- [ ] **Step 1: `docs/forgejo.md` § `realliance.net and the general runner`.** It covers:
  - **Where things live:** the repo, the workflow, the image, the pin, the Argo app `realliance-net`, the runner namespace.
  - **Release:**
    1. A PR to the protected `main`, fast-forward merge by an Owner.
    2. `git tag -s X.Y.Z` by an Owner; CI publishes `X.Y.Z` and `sha-<7>` and never moves a tag.
    3. Verify anonymously, then pre-pull with `talosctl image pull` on each node.
    4. Re-pin in luma-homeops after `git verify-tag` and a review of the range.
  - **Rollback:** `git revert` the re-pin. The previous digest stays in the forge registry; the quay 2.3 image stays until Chris retires it.
  - **Brand sync:** `scripts/sync-brand.sh --tag vX.Y.Z`, `brand/uses.txt` and the signers file. Add a signer by committing their key and fingerprint.
  - **The general runner:**
    - the allowlist (one connection per repo);
    - how to add `realliance/brand` later: a new secret, `forgejo-cli … register --scope realliance/brand --labels general --secret-stdin=-`, the derived connection, reseal, STOP;
    - rotation: re-register with a new secret, reseal, push, delete the old runner row;
    - never `realliance/forge-bot`;
    - the NetworkPolicy state (Part C done or pending);
    - the accepted risk, the revisit trigger, and what it does not offer (the `tiny`-class section of this plan).
  - **Tokens:** `SITE_REGISTRY_TOKEN` (forge-ci, `write:package`, every realliance package); rotate it on any member offboarding, as forge-bot's runbook does.
  - **Dependencies:** new pods cannot pull while Forgejo is down; a WAN-IP change breaks node pulls until the router's reflection rules follow (the inherited hairpin limitation).
- [ ] **Step 2: `CLAUDE.md`.**
  - A topology row **Realliance website**: `realliance.net`/`www`, ns `realliance-net` (Argo `realliance-net`, `cluster/realliance.net/`); source `git.realliance.net/realliance/realliance.net` (moved from GitHub 2026-11-04, GitHub archived); CI on the forge's `general` runner (ns `forgejo-runner-general`, repo-scoped, never forge-bot's runner) with Deno + crane and no Docker; a manual `X.Y.Z@sha256` re-pin; pulls through the WAN hairpin redirect to B2, so **no new pods while Forgejo is down**; runbook `docs/forgejo.md` § realliance.net.
  - Sharp edge 6: add `the realliance.net image (built on the forge; re-pin by hand after review)`.
- [ ] **Step 3: Verify.**
  - `grep -n 'forgejo-runner-general\|realliance.net/realliance' CLAUDE.md docs/forgejo.md | wc -l`. Expected: ≥ 4.
  - `grep -nE 'secret|token' docs/forgejo.md | grep -i 'realliance.net\|general'`. Expected: names only, no values.
  - No hidden names (a manual read).
- [ ] **Step 4: Commit and push** (docs-only): `docs: realliance.net on the forge and the general runner (runbook, CLAUDE.md topology)`. Then remove the worktree once every task, including 3C, has landed.

## Open questions (asked at Task 0)

- **Q1.** nginx base. The decided pin is nginx **1.20.2, built 2021-11-16** (Alpine of that era, EOL), and the forge has no Renovate to move it. Ship 2.4.0 on it, as decided? *Recommendation:* yes; this is the same runtime as today, with a clean rollback. Then a 2.4.1 that only moves the base to a current `nginx:stable-alpine` digest at least 2 weeks old.
- **Q2.** Carry the three unmerged member-bio branches (`chrmiller/eddie-bio`, `jared-bio`, `meacham-bio`) to the forge? *Default:* yes; drop `renovate/*`.
- **Q3.** Deploy the general runner before the netpol plan's B2, unconfined like forge-bot's runner today, or hold it until B2, about 2026-11-06 or later? *Default:* deploy now, so PR CI works while the brand tag is pending.
- **Q4.** May the executor merge the site PR and push the signed `2.4.0` tag as you (the forge-bot precedent), with you reviewing before the re-pin? Or do you merge and tag yourself?
- **Q5.** Quay. The GitHub repo's push webhook targets a Quay build trigger whose URL embeds a credential. Task 8 deletes the hook. Do you also want to delete or rotate the trigger in Quay? How long should `quay.io/realliance/realliance.net:2.3` stay as the rollback target (default: at least 14 days)?
- **Q6.** Brand release signers: only your key for now, with Connor's added when he becomes an Owner?
- **Q7.** The page's `Github` button. Keep it as previewed (default), or also link the forge?
