{ config, lib, pkgs, modulesPath, ... }:

{
  imports = [
    "${modulesPath}/profiles/minimal.nix"
    # NB: profiles/perlless.nix is tempting but fails the build via a
    # closure-assertion if ANY transitive dep pulls Perl in — and `git`
    # (which comin depends on) does. We'd save ~150 MB but break the
    # build. Revisit only with a Perl-free git override.
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
