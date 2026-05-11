{ inputs }:

{ hostName, board, modules ? [ ], system ? "aarch64-linux" }:

let
  boardModule = ../modules/boards + "/${board}.nix";
in
inputs.nixos-raspberrypi.lib.nixosSystem {
  inherit system;
  modules = [
    inputs.nixos-raspberrypi.nixosModules.sd-image
    ../modules/base.nix
    ../modules/immutable.nix
    ../modules/minimal.nix
    ../modules/comin.nix
    ../modules/observability.nix
    ../modules/nfs-client.nix
    boardModule
    inputs.agenix.nixosModules.default
    inputs.comin.nixosModules.comin
    { networking.hostName = hostName; }
  ] ++ modules;
}
