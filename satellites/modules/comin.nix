{ config, lib, pkgs, ... }:

let
  cfg = config.satellites.comin;
in
{
  options.satellites.comin = {
    repoUrl = lib.mkOption {
      type = lib.types.str;
      default = "https://github.com/ChristopherJMiller/luma-homeops";
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

    # Yield CPU/IO to interactive services (esp. sshd) during evaluation,
    # substitution, and switch-to-configuration. Without this, deploys on
    # the Pi 3Bs peg all 4 cores and new SSH handshakes time out.
    systemd.services.comin.serviceConfig = {
      Nice = 10;
      CPUWeight = 20;
      IOWeight = 20;
    };

    systemd.services."satellites-reboot-if-needed" = lib.mkIf cfg.autoReboot.enable {
      description = "Reboot satellite if booted generation is stale (kernel/initrd update pending)";
      serviceConfig = {
        Type = "oneshot";
        ExecStart = pkgs.writeShellScript "satellites-reboot-if-needed" ''
          set -eu
          booted=$(readlink -f /run/booted-system)
          current=$(readlink -f /run/current-system)
          if [ "$booted" = "$current" ]; then
            echo "booted matches current — no reboot needed"
            exit 0
          fi
          ${lib.optionalString config.services.octoprint.enable ''
          # Never reboot mid-print. Ask OctoPrint for the active job state; a
          # stale-kernel reboot that kills an 8-hour print is far worse than
          # deferring the reboot one more night. Self-applies on any satellite
          # that runs OctoPrint (gated on services.octoprint.enable).
          octocfg=/var/lib/octoprint/config.yaml
          apikey=$(${pkgs.gawk}/bin/awk \
            '/^api:/{f=1;next} f&&/^[^[:space:]]/{f=0} f&&/^[[:space:]]*key:/{print $2; exit}' \
            "$octocfg" 2>/dev/null || true)
          state=""
          if [ -n "$apikey" ]; then
            state=$(${pkgs.curl}/bin/curl -sf --max-time 10 -H "X-Api-Key: $apikey" \
              http://127.0.0.1:${toString config.services.octoprint.port}/api/job \
              | ${pkgs.jq}/bin/jq -r '.state // empty' 2>/dev/null || true)
          fi
          case "$state" in
            Printing*|Pausing*|Paused*|Resuming*|Starting*|Cancelling*)
              echo "OctoPrint state=\"$state\" — print in progress, deferring reboot to next window"
              exit 0
              ;;
            "")
              echo "OctoPrint state indeterminate (api unreachable / no key) — deferring reboot to be safe"
              exit 0
              ;;
            *)
              echo "OctoPrint state=\"$state\" — not printing, reboot permitted"
              ;;
          esac
          ''}
          echo "booted=$booted differs from current=$current — rebooting"
          ${pkgs.systemd}/bin/systemctl reboot
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
