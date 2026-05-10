{ config, lib, pkgs, ... }:

{
  imports = [
    ./hardware.nix
    ./secrets.nix
  ];

  satellites.comin = {
    autoReboot = {
      enable = true;
      window = "03:00";
    };
  };

  satellites.nfsMounts."/var/lib/octoprint/uploads" = {
    server = "192.168.0.230";
    remotePath = "/exports/satellites/octoprint";
  };

  services.octoprint = {
    enable = true;
    host = "0.0.0.0";
    port = 5000;
  };

  users.users.octoprint.extraGroups = [ "dialout" ];

  networking.firewall.extraInputRules = ''
    ip saddr 192.168.0.0/24 tcp dport 5000 accept comment "OctoPrint web UI from LAN"
  '';

  # Webcam streaming: out of scope for v1. To enable, add a USB or CSI camera,
  # enable services.mjpg-streamer here, and open port 8080 on the LAN.
}
