{ config, lib, pkgs, ... }:

let
  recipients = import ../secrets/recipients.nix;
in
{
  system.stateVersion = "25.11";

  time.timeZone = lib.mkDefault "America/New_York";

  i18n.supportedLocales = [ "en_US.UTF-8/UTF-8" ];

  nix.settings = {
    experimental-features = [ "nix-command" "flakes" ];
    auto-optimise-store = true;
  };
  nix.gc.automatic = false;
  nix.channel.enable = false;

  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      PermitRootLogin = "no";
      KbdInteractiveAuthentication = false;
    };
  };

  users.mutableUsers = false;
  users.users.admin = {
    isNormalUser = true;
    extraGroups = [ "wheel" "dialout" ];
    openssh.authorizedKeys.keys = builtins.attrValues recipients.operators;
  };
  security.sudo.wheelNeedsPassword = false;

  services.timesyncd.enable = true;
  systemd.services.systemd-time-wait-sync.wantedBy = [ "multi-user.target" ];

  systemd.services.agenix = lib.mkIf (config.age.secrets != { }) {
    after = [ "systemd-time-wait-sync.service" ];
    requires = [ "systemd-time-wait-sync.service" ];
  };

  age.identityPaths = [ "/etc/ssh/ssh_host_ed25519_key" ];

  networking.firewall.enable = true;

  environment.systemPackages = with pkgs; [
    htop
    tmux
    curl
    jq
    usbutils
    pciutils
    nfs-utils
  ];
}
