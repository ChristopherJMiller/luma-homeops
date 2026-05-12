{
  description = "satellites — NixOS edge devices in orbit around the galaxy cluster";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

    # Intentionally do NOT use inputs.nixpkgs.follows = "nixpkgs" here.
    # That would change every derivation hash and miss every cachix build at
    # https://nixos-raspberrypi.cachix.org (kernel, ffmpeg-rpi, pipewire, ...).
    # Carrying nvmd's pinned nixpkgs in the closure costs a few hundred MB of
    # store but saves 2+ hours of aarch64 emulated compilation per build.
    nixos-raspberrypi.url = "github:nvmd/nixos-raspberrypi/main";

    comin = {
      url = "github:nlewo/comin";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    agenix = {
      url = "github:ryantm/agenix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  nixConfig = {
    extra-substituters = [
      "https://nixos-raspberrypi.cachix.org"
      "https://christopherjmiller.cachix.org"
    ];
    extra-trusted-public-keys = [
      "nixos-raspberrypi.cachix.org-1:4iMO9LXa8BqhU+Rpg6LQKiGa2lsNh/j2oiYLNOQ5sPI="
      "christopherjmiller.cachix.org-1:SpwpBjcK+4KV9+rd6V5+01ivGMu4KPBytdgbst3GNnE="
    ];
  };

  outputs = inputs@{ self, nixpkgs, nixos-raspberrypi, ... }:
    let
      mkHost = import ./lib/mkHost.nix { inherit inputs; };

      # Bootstrap: minimal image with just enough to boot + run comin.
      # Once on the Pi, comin pulls the FULL nixosConfigurations.<hostName>
      # below and switch-to-configurations into it. Kernel matches both ways
      # so the device only needs to substitute userspace from cache.nixos.org.
      mkBootstrap = { hostName, board }: mkHost {
        inherit hostName board;
        modules = [ ];
      };
    in
    {
      # FULL host configurations — what comin evaluates on the device.
      nixosConfigurations = {
        octoprint = mkHost {
          hostName = "octoprint";
          board = "pi-3b";
          modules = [ ./hosts/octoprint ];
        };
        printer = mkHost {
          hostName = "printer";
          board = "pi-3b";
          modules = [ ./hosts/printer ];
        };
      };

      # BOOTSTRAP SD images — what we flash. Identical hostName so comin
      # auto-targets the full nixosConfigurations entry above.
      packages.aarch64-linux = {
        sdImage-octoprint = (mkBootstrap {
          hostName = "octoprint";
          board = "pi-3b";
        }).config.system.build.sdImage;
        sdImage-printer = (mkBootstrap {
          hostName = "printer";
          board = "pi-3b";
        }).config.system.build.sdImage;
      };
    };
}
