{ config, lib, pkgs, ... }:

let
  cfg = config.satellites.comin;
in
{
  options.satellites.comin = {
    repoUrl = lib.mkOption {
      type = lib.types.str;
      default = "https://github.com/chrisemiller/luma-homeops";
      description = "HTTPS URL of the repository comin pulls from.";
    };

    branch = lib.mkOption {
      type = lib.types.str;
      default = "satellites/release";
      description = ''
        Branch or tag comin tracks. We use a signed tag/branch so that
        device updates are gated on operator-signed commits.
      '';
    };

    pollPeriodSeconds = lib.mkOption {
      type = lib.types.int;
      default = 60;
      description = "How often comin polls the remote.";
    };

    gpgPublicKeyPaths = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = ''
        Armored GPG public key files baked into the image. Commits on the
        tracked branch must be signed by one of these keys for comin to
        deploy them.
      '';
    };

    autoReboot = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          Whether to auto-reboot during the configured maintenance window
          when the current generation differs from the booted generation
          (i.e. kernel/initrd needs activation).
        '';
      };

      window = lib.mkOption {
        type = lib.types.str;
        default = "03:00";
        description = ''
          OnCalendar expression for the reboot check. Default 03:00 daily.
          See systemd.time(7).
        '';
      };
    };
  };

  config = {
    services.comin = {
      enable = true;
      hostname = config.networking.hostName;
      repositoryType = "flake";
      repositorySubdir = "satellites";
      gpgPublicKeyPaths = cfg.gpgPublicKeyPaths;
      remotes = [{
        name = "origin";
        url = cfg.repoUrl;
        branches.main = {
          name = cfg.branch;
          operation = "switch";
        };
        poller.period = cfg.pollPeriodSeconds;
      }];
    };

    systemd.services."satellites-reboot-if-needed" = lib.mkIf cfg.autoReboot.enable {
      description = "Reboot satellite if booted generation is stale (kernel/initrd update pending)";
      serviceConfig = {
        Type = "oneshot";
        ExecStart = pkgs.writeShellScript "satellites-reboot-if-needed" ''
          set -eu
          booted=$(readlink -f /run/booted-system)
          current=$(readlink -f /run/current-system)
          if [ "$booted" != "$current" ]; then
            echo "booted=$booted differs from current=$current — rebooting"
            ${pkgs.systemd}/bin/systemctl reboot
          else
            echo "booted matches current — no reboot needed"
          fi
        '';
      };
    };

    systemd.timers."satellites-reboot-if-needed" = lib.mkIf cfg.autoReboot.enable {
      description = "Daily maintenance-window check for pending reboot";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.autoReboot.window;
        Persistent = false;
        RandomizedDelaySec = "5min";
      };
    };
  };
}
