{
  description = "satellites — NixOS edge devices in orbit around the galaxy cluster";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

    nixos-raspberrypi = {
      url = "github:nvmd/nixos-raspberrypi/main";
      inputs.nixpkgs.follows = "nixpkgs";
    };

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
    extra-substituters = [ "https://nixos-raspberrypi.cachix.org" ];
    extra-trusted-public-keys = [
      "nixos-raspberrypi.cachix.org-1:4iMO9LXa8BqhU+Rpg6LQKiGa2lsNh/j2oiYLNOQ5sPI="
    ];
  };

  outputs = inputs@{ self, nixpkgs, nixos-raspberrypi, ... }:
    let
      mkHost = import ./lib/mkHost.nix { inherit inputs; };
    in
    {
      nixosConfigurations = {
        octoprint = mkHost {
          hostName = "octoprint";
          board = "pi-3b";
          modules = [ ./hosts/octoprint ];
        };
      };

      packages.aarch64-linux = {
        sdImage-octoprint = self.nixosConfigurations.octoprint.config.system.build.sdImage;
      };
    };
}
