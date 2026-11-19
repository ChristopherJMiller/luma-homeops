# fj actions retry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `fj actions retry <run>` releases the jobs ci-dispatch holds in a run, from Chris's machines.

**Architecture:** A source patch to forgejo-cli 0.6.0 carried in `ChristopherJMiller/nixos-configs` (`/etc/nixos`) through `overrideAttrs`. The new subcommand resolves the run number shown in the web UI to its id, then calls plan 3's `POST /api/v1/repos/{owner}/{repo}/actions/runs/{run_id}/release` with the login fj already holds. forgejo-api 0.11.1 has no public raw request, so the patch adds `reqwest` as a direct dependency (already in the lock through forgejo-api), which changes `Cargo.lock` and needs a `cargoDeps` override.

**Tech Stack:** Rust (forgejo-cli 0.6.0, clap, Fluent help text), Nix (`rustPlatform.buildRustPackage`, `fetchCargoVendor`), NixOS flake.

**Spec:** `docs/superpowers/specs/2026-11-17-ci-stale-jobs-design.md` §10. Plan 4 of 4; consumes plan 3's API route (200 `{"jobs":[{"job_id","attempt","state","reason"}]}`, 403, 429, 503).

## Global Constraints

- `/etc/nixos` has two uncommitted files that are not ours: never stage, change or stash them. Commit only the new files and the `common/forgejo.nix` change.
- Build with `nix build --cores 4` and `-j 1`; no other heavy builds alongside.
- The patch touches only `src/`, `localization/` and `Cargo.toml`/`Cargo.lock`; the version string gains no suffix.
- Activation needs root: Chris runs the switch (`! sudo nixos-rebuild switch --flake /etc/nixos#<host>`); the devbox picks it up on its own next rebuild.
- Output: one line per job (`job <id> attempt <n>: released|not held|not releasable (<reason>)`), "nothing held" for an empty list; 403 → "needs write access to Actions"; 429 → "release cap reached, try again in <Retry-After>"; 503 → "CI is unreachable"; exit status 1 for those three.

## Review Focus

1. An OAuth login whose token expired: `retry` must refresh it the way `get_api` does, not fail with 401 (Task 1 test against a stub server, or manual check after switch).
2. A run number that doesn't exist: "no run #N in owner/repo", exit 1, no POST.
3. A repo with over 50 runs: the lookup pages until it finds the run number or runs out.

---

### Task 1: The fj patch

**Files (new, in `/etc/nixos`):**
- Create: `pkgs/forgejo-cli/actions-retry.patch`, `pkgs/forgejo-cli/default.nix`
- Modify: `common/forgejo.nix` (use the overridden package in `homeModule` and `credentialHelper`)

**Interfaces:**
- Consumes: `ActionsSubcommand` (`src/actions.rs:34-66`), `RepoInfo::get_current(host_name, repo, remote, keys)`, `keys.get_api(url)` / the stored login for its token, `localization/en-US/messages.ftl` (`help-cmd-…` keys).
- Produces: `ActionsSubcommand::Retry { run: u64 }` and `async fn retry(repo: &RepoInfo, keys: &mut KeyInfo, run: u64) -> eyre::Result<()>`.

- [ ] **Step 1:** Unpack the source (`nix build --no-link --print-out-paths nixpkgs#forgejo-cli.src`), copy it to a scratch git repo, and write the change: the clap variant with help `help-cmd-actions-retry` ("Release the jobs CI holds in a run, by its run number"), the lookup (`GET …/actions/runs?limit=50&page=N`, match `index_in_repo == run`, up to 20 pages), the POST with the login's token (refresh first, like `get_api`), and the output rules above. Add `reqwest` to `Cargo.toml` at the version already locked, `features = ["json"]` only if the lock already carries them.
- [ ] **Step 2:** `cargo build` in a `nix shell nixpkgs#cargo nixpkgs#rustc nixpkgs#pkg-config nixpkgs#openssl` with `-j 4` → compiles; `cargo test` → existing tests pass. Export `git diff` as `actions-retry.patch`.
- [ ] **Step 3:** `default.nix`: `forgejo-cli.overrideAttrs (o: { patches = (o.patches or []) ++ [ ./actions-retry.patch ]; cargoDeps = rustPlatform.fetchCargoVendor { inherit (o) src; patches = [ ./actions-retry.patch ]; hash = …; }; })`; get the hash with `lib.fakeHash` first, then the reported one. Wire it into `common/forgejo.nix` the way `customPackages` are (`flake.nix:90`).
- [ ] **Step 4:** `nix build --cores 4 -j 1 /etc/nixos#nixosConfigurations.<host>.config.system.build.toplevel` → success; `result/sw/bin/fj actions retry --help` shows the help line (or the built package's `bin/fj`).
- [ ] **Step 5:** Commit signed in `/etc/nixos` (only the three paths), push to `origin` (GitHub). Ask Chris to run the switch.
- [ ] **Step 6: Verify after the switch:** `fj --host git.realliance.net actions --repo conduit/platform retry 999999` → "no run #999999", exit 1; on a finished run → "nothing held", exit 0.
