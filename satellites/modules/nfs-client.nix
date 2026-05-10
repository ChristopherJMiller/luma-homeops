{ config, lib, pkgs, ... }:

let
  cfg = config.satellites.nfsMounts;
in
{
  options.satellites.nfsMounts = lib.mkOption {
    description = ''
      Set of NFSv4 mounts to create. Keys are the local mount points
      (e.g. "/var/lib/octoprint/uploads"), values describe the remote
      export.

      Mounts are configured as automounts so a slow or briefly unreachable
      NFS server doesn't block boot.
    '';
    default = { };
    type = lib.types.attrsOf (lib.types.submodule {
      options = {
        server = lib.mkOption {
          type = lib.types.str;
          description = "NFS server IP or hostname (typically the lan-internal MetalLB IP).";
        };
        remotePath = lib.mkOption {
          type = lib.types.str;
          description = "Path on the NFS server, e.g. /exports/satellites/octoprint.";
        };
        options = lib.mkOption {
          type = lib.types.listOf lib.types.str;
          default = [
            "nfsvers=4.2"
            "sec=sys"
            "hard"
            "noatime"
            "x-systemd.automount"
            "x-systemd.idle-timeout=600"
            "x-systemd.mount-timeout=30"
            "x-systemd.requires=network-online.target"
          ];
          description = "Mount options.";
        };
      };
    });
  };

  config = lib.mkIf (cfg != { }) {
    # NFSv4 doesn't strictly need rpcbind, but nixpkgs' NFS module enables
    # it by default — let that win rather than fight the module.

    fileSystems = lib.mapAttrs
      (mountPoint: mount: {
        device = "${mount.server}:${mount.remotePath}";
        fsType = "nfs4";
        options = mount.options;
      })
      cfg;
  };
}
