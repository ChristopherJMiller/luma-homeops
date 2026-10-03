{
  description = "luma-homeops operator toolchain";

  # nixos-unstable, pinned by flake.lock. Bump with `nix flake update nixpkgs`
  # and then build the shell + check every tool version (see the
  # nix-shell-pin skill) before committing the lock.
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    # Source-only (flake = false): we want the gramps-mcp derivation from
    # packages/gramps-mcp, not that repo's flake outputs or its input graph.
    # Its patches against cabout-me/gramps-mcp v1.1.0 stay the single
    # source of truth there — copying them here would fork them. Pinned by
    # flake.lock; bump with `nix flake update nixos-configs`.
    nixos-configs = {
      url = "github:ChristopherJMiller/nixos-configs";
      flake = false;
    };
    flake-compat = {
      url = "github:edolstra/flake-compat";
      flake = false;
    };
  };

  outputs = { self, nixpkgs, nixos-configs, ... }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f (import nixpkgs {
        inherit system;
        config.allowUnfree = true; # terraform (BSL)
      }));
    in
    {

      # The MCP server that writes to the family tree, built from Chris's
      # patched derivation in nixos-configs. Upstream v1.1.0 is a year old and
      # his fixes (create_family dropping children, get_type crashing on notes,
      # gender OTHER rejected, a shared-httpx-client teardown race) are still
      # open PRs upstream, so the patched build is the only usable one.
      packages = forAllSystems (pkgs: rec {
        # His derivation carries the upstream bug fixes. Ours adds one
        # deployment adaptation on top, kept HERE rather than in his repo
        # because it is only correct behind this gateway: his laptop uses stdio,
        # where the SDK localhost default is exactly right.
        gramps-mcp =
          (pkgs.callPackage "${nixos-configs}/packages/gramps-mcp" { }).overrideAttrs
            (old: {
              patches = (old.patches or [ ]) ++ [
                ./patches/gramps-mcp-transport-security.patch
              ];
            });

        # Container image straight from that derivation — no second Dockerfile,
        # no copied patches. streamLayeredImage writes the tar to stdout instead
        # of materialising it in the store; CI pipes it into `docker load`.
        gramps-mcp-image = pkgs.dockerTools.streamLayeredImage {
          name = "ghcr.io/christopherjmiller/gramps-mcp";
          tag = "latest";
          # fakeNss supplies /etc/passwd so a numeric user is resolvable;
          # cacert so httpx can verify TLS if the API is ever remote.
          contents = [ pkgs.cacert pkgs.dockerTools.fakeNss ];
          extraCommands = "mkdir -p tmp && chmod 1777 tmp";
          config = {
            Entrypoint = [ "${gramps-mcp}/bin/gramps-mcp" ];
            # No argument => streamable HTTP on 8000 (stdio needs `stdio`).
            ExposedPorts = { "8000/tcp" = { }; };
            User = "65534:65534";
            Env = [
              "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
              "HOME=/tmp"
              "PYTHONDONTWRITEBYTECODE=1"
            ];
          };
        };
      });
      devShells = forAllSystems (pkgs:
        let
          # tools/hactl (Home Assistant agent toolkit): websockets + pyyaml for
          # the HA API, playwright for screenshots, pytest for its tests. The
          # browsers (playwright-driver.browsers below) come from the same
          # nixpkgs pin, so their versions match.
          hactlPython = pkgs.python313.withPackages (ps: [ ps.websockets ps.pyyaml ps.playwright ps.pytest ]);
        in
        {
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
            # (python313 itself comes via hactlPython; uv-managed venvs are unaffected)
            hactlPython
            playwright-driver.browsers
            uv
            ruff
            docker_29

            # Backblaze B2 CLI: inspect/prune the existing B2 account and manage
            # buckets/app-keys for the cluster backup targets. nixpkgs installs
            # the binary as `backblaze-b2` (alias `b2v4`), NOT `b2`. Auth via
            # `backblaze-b2 account authorize` (stored in ~/.config/b2, not in
            # the repo).
            backblaze-b2

            # Go: images/mcp-jwt-auth is Go source now, and its go.sum has to be
            # generated somewhere. Without this, `go mod tidy` is impossible
            # locally and dependency checksums end up resolved at image-build time
            # instead of pinned in the repo.
            go

            # restic: read the 2022 `luma-backups` restic repo on B2, and drive
            # restore drills against the cluster backup repos (see docs/backups.md).
            restic
          ];
          # hactl: its python first on PATH (another package drags in a bare
          # python 3.14 that would otherwise win `python3`), the wrapper on
          # PATH, and Playwright pointed at the nix-built browsers (the ones it
          # downloads itself don't run on NixOS).
          shellHook = ''
            export PATH="${hactlPython}/bin:$(git rev-parse --show-toplevel 2>/dev/null || pwd)/tools/hactl/bin:$PATH"
            export PLAYWRIGHT_BROWSERS_PATH=${pkgs.playwright-driver.browsers}
            export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=true
          '';
        };
      });
    };
}
