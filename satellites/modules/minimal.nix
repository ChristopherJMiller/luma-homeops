{ config, lib, pkgs, modulesPath, ... }:

{
  imports = [
    "${modulesPath}/profiles/minimal.nix"
    "${modulesPath}/profiles/perlless.nix"
  ];

  documentation.enable = false;
  documentation.nixos.enable = false;
  documentation.man.enable = false;
  documentation.info.enable = false;
  documentation.doc.enable = false;

  fonts.fontconfig.enable = false;

  xdg.autostart.enable = false;
  xdg.icons.enable = false;
  xdg.mime.enable = false;
  xdg.sounds.enable = false;

  boot.supportedFilesystems = lib.mkForce [ "ext4" "vfat" "nfs" "nfs4" ];

  environment.defaultPackages = lib.mkForce [ ];

  programs.command-not-found.enable = false;
  services.udisks2.enable = false;
  services.fwupd.enable = false;
}
