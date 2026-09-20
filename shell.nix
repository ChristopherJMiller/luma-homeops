# Compatibility shim: `nix-shell shell.nix --run '…'` (used throughout CLAUDE.md
# and the skills) resolves to the flake's devShell, pinned by flake.lock.
# flake-compat itself is pinned via the lock too, so there is exactly one
# source of truth. Prefer `nix develop` / direnv `use flake` for new workflows.
let
  lock = builtins.fromJSON (builtins.readFile ./flake.lock);
  fc = lock.nodes.flake-compat.locked;
  flake-compat = fetchTarball {
    url = "https://github.com/${fc.owner}/${fc.repo}/archive/${fc.rev}.tar.gz";
    sha256 = fc.narHash;
  };
in
(import flake-compat { src = ./.; }).shellNix
