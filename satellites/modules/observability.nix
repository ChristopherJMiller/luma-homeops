{ config, lib, pkgs, ... }:

{
  services.prometheus.exporters.node = {
    enable = true;
    listenAddress = "0.0.0.0";
    port = 9100;
    enabledCollectors = [
      "systemd"
      "processes"
    ];
    openFirewall = false;
  };

  networking.firewall.extraInputRules = ''
    ip saddr 192.168.0.0/24 tcp dport 9100 accept comment "node-exporter from LAN"
    ip saddr 192.168.0.0/24 tcp dport 4243 accept comment "comin exporter from LAN"
  '';
}
