{ config, lib, pkgs, ... }:

{
  imports = [
    ./hardware.nix
    ./secrets.nix
  ];

  satellites.comin.autoReboot = {
    enable = true;
    window = "03:00";
  };

  services.octoprint = {
    enable = true;
    host = "0.0.0.0";
    port = 5000;
  };

  users.users.octoprint.extraGroups = [ "dialout" "video" ];

  # USB webcam streaming. Point OctoPrint's webcam settings (or Cura's
  # OctoPrint plugin) at:
  #   stream URL:   http://octoprint.local:8080/?action=stream
  #   snapshot URL: http://octoprint.local:8080/?action=snapshot
  systemd.services.mjpg-streamer = {
    description = "MJPG-Streamer for OctoPrint webcam";
    after = [ "network.target" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "simple";
      ExecStart = ''
        ${pkgs.mjpg-streamer}/bin/mjpg_streamer \
          -i "input_uvc.so -d /dev/video0 -r 640x480 -f 10" \
          -o "output_http.so -p 8080 -w ${pkgs.mjpg-streamer}/share/mjpg-streamer/www"
      '';
      Restart = "on-failure";
      RestartSec = 5;
      User = "octoprint";
      SupplementaryGroups = [ "video" ];
    };
  };

  networking.firewall.extraInputRules = ''
    ip saddr 192.168.0.0/24 tcp dport 5000 accept comment "OctoPrint web UI from LAN"
    ip saddr 192.168.0.0/24 tcp dport 8080 accept comment "OctoPrint webcam stream from LAN"
  '';

  # --- Deferred (re-enable once the cluster-side CephNFS export Job is fixed) ---
  #
  # satellites.nfsMounts."/var/lib/octoprint/uploads" = {
  #   server = "192.168.0.230";
  #   remotePath = "/satellites/octoprint";
  # };
}
