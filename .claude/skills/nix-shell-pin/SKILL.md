---
name: nix-shell-pin
description: Maintain the pinned nixpkgs in shell.nix — bump the pin, add or remove a tool, or diagnose "command not found" / "attribute missing" / a tool that misbehaves inside nix-shell. Use when a CLI in the dev toolchain (az, terraform, talosctl, kubectl, helm, argocd, ansible, kubeseal, pre-commit, python, node) is missing, broken, or too old, or when Renovate/upstream drops a package (nodejs_20 style). NEVER unpins back to <nixpkgs>. NEVER bumps the pin without building the shell and printing every tool's version.
---

# nix-shell-pin

`shell.nix` is the entire dev toolchain and it is **pinned to a specific nixos-unstable commit** via `fetchTarball` (since 2026-09-19). Everything the operating guide says to run through `nix-shell shell.nix --run '…'` comes from that pin — including `pre-commit`, which is why `git commit` outside the shell fails with "pre-commit not found".

## Why pinned

The host's `<nixpkgs>` channel shipped `azure-cli 2.79` with a broken MSAL dependency (`Session.request() got an unexpected keyword argument 'claims_challenge'`): `az login` and every token refresh failed, which blocked the terraform state backend. A pin makes the toolchain reproducible regardless of what the host channel is doing. There is no flake; `nix develop` does not work here.

## Bump the pin

```bash
rev=$(gh api repos/NixOS/nixpkgs/git/ref/heads/nixos-unstable --jq .object.sha)
sha=$(nix-prefetch-url --unpack https://github.com/NixOS/nixpkgs/archive/$rev.tar.gz)
echo "$rev $sha"
```

Edit the `url` and `sha256` in the `fetchTarball` block at the top of `shell.nix`, then **build it and print every version** before committing (a bump can drop or rename packages — `nodejs_20` disappeared on 2026-09-19 with "support was removed given upstream End-of-Life"):

```bash
nix-shell shell.nix --run '
  for t in az terraform talosctl kubectl helm argocd ansible kubeseal kustomize node python3 pre-commit sops ipmitool uv ruff; do
    printf "%-10s " $t; command -v $t >/dev/null && ($t --version 2>/dev/null || $t version 2>/dev/null) | head -1 || echo MISSING
  done'
```

The first build after a bump downloads a fresh toolchain from the binary cache — run it in the background (`run_in_background`) and check the output file; it can take several minutes.

Commit the pin change on its own with the reason in the message.

## Add / remove a tool

Add the attribute to `packages` with a comment saying what in the repo uses it (the file already does this — follow the pattern). Check it exists in the pinned nixpkgs first:

```bash
nix-instantiate --eval -E 'let p = import (fetchTarball { url = "<url from shell.nix>"; sha256 = "<sha from shell.nix>"; }) {}; in p ? <attr>'
```

## Diagnosing

| Symptom | Cause | Fix |
|---|---|---|
| `pre-commit not found` on `git commit` | committing outside the shell | `nix-shell shell.nix --run 'git commit …'` |
| `python3: command not found` in a Bash tool call | same — python lives in the shell | prefix with nix-shell, or use `jq`/`sed` |
| `error: Node.js NN support was removed` on build | pin moved past an EOL | bump to the next LTS `nodejs_NN` |
| `az login` hangs / `claims_challenge` traceback | host-channel azure-cli (unpinned) | make sure you're in *this* shell, not a `nix-shell -p azure-cli` |
| tool version is older than expected | the pin is old | bump (above) |

Ad-hoc one-offs (`nix-shell -p openssl`, `nix-shell -p yq-go`) are fine for tools the repo doesn't need permanently; they use the host channel, which is acceptable for openssl and yq.

## Override for testing

```bash
nix-shell shell.nix --arg pkgs 'import <nixpkgs> {}'          # host channel
nix-shell shell.nix --arg pkgs 'import (fetchTarball "https://github.com/NixOS/nixpkgs/archive/<rev>.tar.gz") {}'
```

## CI

`.github/workflows/` does **not** use `shell.nix` (the satellites SD-image build uses `satellites/flake.nix`; the operator build uses its own Dockerfile). Pin bumps don't affect CI.
