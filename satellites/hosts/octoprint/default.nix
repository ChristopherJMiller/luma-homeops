{ config, lib, pkgs, ... }:

{
  imports = [
    ./hardware.nix
    ./secrets.nix
  ];

  # Bootstrap phase: this host runs comin and nothing else app-specific.
  # Once the device is online and comin's loop is verified, uncomment the
  # OctoPrint + NFS blocks below and push to satellites/release — comin
  # will pull, build, and converge on the device.
  satellites.comin.autoReboot = {
    enable = true;
    window = "03:00";
  };

  # --- Phase 2: enable OctoPrint -------------------------------------------
  #
  # satellites.nfsMounts."/var/lib/octoprint/uploads" = {
  #   server = "192.168.0.230";
  #   remotePath = "/satellites/octoprint";
  # };
  #
  # services.octoprint = {
  #   enable = true;
  #   host = "0.0.0.0";
  #   port = 5000;
  # };
  #
  # users.users.octoprint.extraGroups = [ "dialout" ];
  #
  # networking.firewall.extraInputRules = ''
  #   ip saddr 192.168.0.0/24 tcp dport 5000 accept comment "OctoPrint web UI from LAN"
  # '';
  #
  # # Webcam streaming: add a USB or CSI camera, enable services.mjpg-streamer
  # # here, open port 8080 on the LAN.
}
