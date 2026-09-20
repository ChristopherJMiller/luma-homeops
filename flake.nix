{
  description = "luma-homeops operator toolchain";

  # nixos-unstable, pinned by flake.lock. Bump with `nix flake update nixpkgs`
  # and then build the shell + check every tool version (see the
  # nix-shell-pin skill) before committing the lock.
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-compat = {
      url = "github:edolstra/flake-compat";
      flake = false;
    };
  };

  outputs = { self, nixpkgs, ... }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f (import nixpkgs {
        inherit system;
        config.allowUnfree = true; # terraform (BSL)
      }));
    in
    {
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            git
            kubectl
            krew
            pre-commit
            kubeseal
            nodejs_22
            kustomize
            talosctl
            kubernetes-helm
            argocd
            pinentry-tty
            ansible
            azure-cli
            terraform

            # satellites/: zstd for flash.sh image decompression. agenix is not in
            # nixpkgs as a top-level pkg; invoke it directly with:
            #   nix run github:ryantm/agenix -- -e satellites/secrets/<file>.age
            zstd

            # lagrange satellite glue: sops to decrypt the lagrange repo's
            # secrets/satellite.yaml (admin token, wg-private-key) and wg/wg-pubkey
            # to derive/inspect WireGuard keys for cluster/lagrange-satellite/.
            sops
            wireguard-tools

            # BMC access (AST2500 on the X570D4I-2T nodes, IPs .117/.119/.151):
            # ipmitool for SEL/sensors/power/SOL over lanplus; Redfish works via
            # plain curl. Creds live in nodes/bmc.env (git-crypt).
            ipmitool

            # operators/ceph-nfs-export-operator/: kopf-based reconciler.
            # python3 + uv for dep management; ruff for lint; docker for local image
            # build/test before pushing to ghcr.
            python313
            uv
            ruff
            docker_29

            # Backblaze B2 CLI: inspect/prune the existing B2 account and manage
            # buckets/app-keys for the cluster backup targets. nixpkgs installs
            # the binary as `backblaze-b2` (alias `b2v4`), NOT `b2`. Auth via
            # `backblaze-b2 account authorize` (stored in ~/.config/b2, not in
            # the repo).
            backblaze-b2
          ];
        };
      });
    };
}
