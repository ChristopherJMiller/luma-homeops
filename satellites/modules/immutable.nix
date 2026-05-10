{ config, lib, pkgs, ... }:

{
  services.journald.storage = "volatile";
  services.journald.extraConfig = ''
    RuntimeMaxUse=64M
    SystemMaxUse=0
  '';

  boot.tmp.useTmpfs = true;
  boot.tmp.tmpfsSize = "50%";

  fileSystems."/var/log" = {
    device = "tmpfs";
    fsType = "tmpfs";
    options = [ "size=64M" "mode=0755" "noatime" ];
  };

  zramSwap = {
    enable = true;
    memoryPercent = 50;
    algorithm = "zstd";
  };

  fileSystems."/".options = lib.mkDefault [ "noatime" "nodiratime" ];

  services.fstrim.enable = false;
}
