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

  # CUPS print server for a USB-attached paper printer. Web UI on :631.
  # After first boot:
  #   1. Plug printer into USB, power it on.
  #   2. Browse to http://printer.local:631/admin
  #   3. "Add Printer" -> pick the USB device -> select PPD from the
  #      bundled drivers (gutenprint covers most non-HP/Brother/Epson).
  #   4. Tick "Share this printer" so cups-browsed announces it via mDNS.
  services.printing = {
    enable = true;
    listenAddresses = [ "*:631" ];
    allowFrom = [ "all" ];        # firewall scopes this to the LAN
    browsing = true;
    defaultShared = true;
    webInterface = true;
    drivers = with pkgs; [
      gutenprint
      gutenprintBin
      hplip
      brlaser
      epson-escpr
    ];
  };

  networking.firewall.extraInputRules = ''
    ip saddr 192.168.0.0/24 tcp dport 631 accept comment "CUPS IPP from LAN"
    ip saddr 192.168.0.0/24 udp dport 631 accept comment "CUPS browsing from LAN"
  '';

  # Server-level mDNS records so the CUPS host itself is discoverable in
  # print pickers (macOS/iOS/Windows). cups-browsed publishes a per-queue
  # _ipp._tcp record on top of this once a printer is added in the web UI.
  services.avahi.extraServiceFiles.cups = ''
    <?xml version="1.0" standalone='no'?>
    <!DOCTYPE service-group SYSTEM "avahi-service.dtd">
    <service-group>
      <name replace-wildcards="yes">CUPS on %h</name>
      <service>
        <type>_ipp._tcp</type>
        <subtype>_universal._sub._ipp._tcp</subtype>
        <port>631</port>
        <txt-record>rp=printers/</txt-record>
        <txt-record>note=galaxy satellite print server</txt-record>
      </service>
      <service>
        <type>_http._tcp</type>
        <port>631</port>
        <txt-record>path=/</txt-record>
      </service>
    </service-group>
  '';

  users.users.admin.extraGroups = [ "lp" "lpadmin" ];
}
