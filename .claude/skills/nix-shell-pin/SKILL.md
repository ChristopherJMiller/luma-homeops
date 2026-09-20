---
name: nix-shell-pin
description: Maintain the pinned dev toolchain (flake.nix + flake.lock, with shell.nix as a flake-compat shim) — bump nixpkgs, add or remove a tool, or diagnose "command not found" / "attribute missing" / a tool that misbehaves inside the shell. Use when a CLI in the toolchain (az, terraform, talosctl, kubectl, helm, argocd, ansible, kubeseal, pre-commit, python, node) is missing, broken, or too old, or when upstream drops a package (nodejs_20 style). NEVER unpins to a floating <nixpkgs>. NEVER bumps flake.lock without building the shell and printing every tool's version.
---

# nix-shell-pin

The operator toolchain is `flake.nix` → `devShells.<system>.default`, pinned by `flake.lock` (nixpkgs `nixos-unstable` at a specific commit). Two equivalent entry points:

```bash
nix develop --command <cmd>            # flake-native
nix-shell shell.nix --run '<cmd>'      # shim via flake-compat; what CLAUDE.md and the skills use
```

They resolve to the same store paths — `shell.nix` reads `flake.lock` and imports the flake, so there is exactly one source of truth. `pre-commit`, `az`, `python3`, `terraform` etc. exist *only* inside the shell; `git commit` outside it fails with "pre-commit not found".

## Why pinned

The host's `<nixpkgs>` channel shipped `azure-cli 2.79` with a broken MSAL dependency (`Session.request() got an unexpected keyword argument 'claims_challenge'`): `az login` and every token refresh failed, which blocked the terraform state backend (2026-09-19). A lock makes the toolchain reproducible regardless of the host channel.

## Bump the pin

```bash
nix flake update nixpkgs            # rewrites flake.lock only
git diff flake.lock                 # one input, old rev -> new rev
```

Then **build and print every version before committing** — a bump can drop or rename packages (`nodejs_20` disappeared 2026-09-19: "support was removed given upstream End-of-Life"):

```bash
nix develop --command bash -c '
  for t in az terraform talosctl kubectl helm argocd ansible kubeseal kustomize node python3 pre-commit sops ipmitool uv ruff; do
    printf "%-10s " $t; command -v $t >/dev/null && ($t --version 2>/dev/null || $t version 2>/dev/null) | head -1 || echo MISSING
  done'
```

First build after a bump pulls a fresh toolchain from the binary cache — run it with `run_in_background` and read the output file; it can take minutes. Commit `flake.lock` on its own with the reason.

`flake-compat` is the other input; it almost never needs bumping. Renovate does not manage `flake.lock` (it has never touched `satellites/flake.lock` either).

## Add / remove a tool

Edit `packages` in `flake.nix` with a comment saying what in the repo uses it (follow the existing pattern). Check the attribute exists first:

```bash
nix eval --raw nixpkgs#<attr>.name    # uses the locked nixpkgs when run inside the repo? No — use:
nix eval --raw .#devShells.x86_64-linux.default.name >/dev/null && nix eval --impure --expr '(builtins.getFlake (toString ./.)).inputs.nixpkgs.legacyPackages.x86_64-linux ? <attr>'
```

Flakes only see **git-tracked** files — `git add` a new file before expecting the flake to read it (not relevant for the devShell, which references no repo files, but it bites for anything else).

## Known quirks (as of the 2026-09-19 pin)

- `python3` on PATH is **3.14** (pulled in by another package) even though `python313` is listed; the ceph-nfs-export-operator project uses `uv`, which pins its own interpreter, so this doesn't matter there. Use `python3.13` explicitly if it does.
- `kubectl` client is v1.37 against a v1.33 API server — outside the official ±1 skew but works for everything we do. It'll converge as #2506 progresses.

## Diagnosing

| Symptom | Cause | Fix |
|---|---|---|
| `pre-commit not found` on `git commit` | committing outside the shell | `nix develop --command git commit …` |
| `python3: command not found` in a Bash tool call | same — python lives in the shell | prefix with nix develop, or use `jq`/`sed` |
| `error: Node.js NN support was removed` on build | lock moved past an EOL | bump to the next LTS `nodejs_NN` in `flake.nix` |
| `az login` hangs / `claims_challenge` traceback | host-channel azure-cli (unpinned) | make sure you're in *this* shell, not `nix-shell -p azure-cli` |
| `warning: Git tree '…' is dirty` | harmless; flakes note uncommitted changes | ignore |
| shim says `attribute 'flake-compat' missing` | `flake.lock` lacks the input | `nix flake lock` |

Ad-hoc one-offs (`nix-shell -p openssl`, `nix-shell -p yq-go`) still use the host channel — fine for tools the repo doesn't need permanently.

## CI

`.github/workflows/` does **not** use this flake (satellites SD images use `satellites/flake.nix`; the operator build uses its own Dockerfile). Lock bumps don't affect CI.
