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

  # A skipped serial write during a print = layer shift. Guarantee CPU/IO
  # for OctoPrint under any contention (comin deploy, plugin work, etc.).
  systemd.services.octoprint.serviceConfig = {
    Nice = -5;
    CPUWeight = 500;
    IOWeight = 500;
  };

  # ustreamer, not mjpg-streamer: nixpkgs' mjpg-streamer input_uvc.so has an
  # undefined-symbol dlopen failure (`resolutions_help`). Lower CPUWeight
  # so a busy webcam can never starve the print loop.
  systemd.services.ustreamer = {
    description = "uStreamer for OctoPrint webcam";
    after = [ "network.target" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "simple";
      ExecStart = ''
        ${pkgs.ustreamer}/bin/ustreamer \
          --device=/dev/video0 \
          --resolution=640x480 \
          --desired-fps=10 \
          --host=127.0.0.1 \
          --port=8080
      '';
      Restart = "on-failure";
      RestartSec = 5;
      User = "octoprint";
      SupplementaryGroups = [ "video" ];
      CPUWeight = 50;
    };
  };

  # nginx on :80 fronts OctoPrint (:5000) + webcam (:8080) — matches OctoPi's
  # haproxy layout, so OctoPrint's stock webcam settings ("/webcam/?action=…")
  # work and ustreamer's native /stream + /snapshot are reachable under /webcam/.
  services.nginx = {
    enable = true;
    recommendedProxySettings = true;
    virtualHosts."octoprint" = {
      default = true;
      locations."/" = {
        proxyPass = "http://127.0.0.1:5000";
        proxyWebsockets = true;
        extraConfig = ''
          client_max_body_size 0;
          proxy_read_timeout 600s;
          proxy_send_timeout 600s;
        '';
      };
      locations."/webcam/" = {
        proxyPass = "http://127.0.0.1:8080/";
        extraConfig = ''
          proxy_buffering off;
        '';
      };
    };
  };

  # extraInputRules is nftables-only; this satellite uses iptables. LAN-only
  # is enforced by the perimeter (router NAT, no port-forward for these).
  networking.firewall.allowedTCPPorts = [
    80    # OctoPrint web UI (nginx frontend)
    5000  # OctoPrint direct (fallback during cutover)
  ];

  # Advertise port 80 (nginx) — OctoPi-style frontend port.
  services.avahi.extraServiceFiles.octoprint = ''
    <?xml version="1.0" standalone='no'?>
    <!DOCTYPE service-group SYSTEM "avahi-service.dtd">
    <service-group>
      <name replace-wildcards="yes">OctoPrint on %h</name>
      <service>
        <type>_octoprint._tcp</type>
        <port>80</port>
        <txt-record>path=/</txt-record>
      </service>
      <service>
        <type>_http._tcp</type>
        <port>80</port>
        <txt-record>path=/</txt-record>
      </service>
    </service-group>
  '';

  # --- Deferred (re-enable once the cluster-side CephNFS export Job is fixed) ---
  #
  # satellites.nfsMounts."/var/lib/octoprint/uploads" = {
  #   server = "192.168.0.230";
  #   remotePath = "/satellites/octoprint";
  # };
}
