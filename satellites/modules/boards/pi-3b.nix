{ config, lib, pkgs, nixos-raspberrypi, ... }:

{
  imports = with nixos-raspberrypi.nixosModules; [
    raspberry-pi-3.base
  ];

  networking.useNetworkd = true;
  networking.useDHCP = false;
  systemd.network.networks."10-eth0" = {
    matchConfig.Name = "eth0";
    networkConfig.DHCP = "yes";
  };

  systemd.services.systemd-networkd-wait-online.enable = lib.mkForce false;
  systemd.services.NetworkManager-wait-online.enable = lib.mkForce false;
}
