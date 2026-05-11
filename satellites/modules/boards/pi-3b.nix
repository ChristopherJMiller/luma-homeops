{ config, lib, pkgs, nixos-raspberrypi, ... }:

{
  imports = with nixos-raspberrypi.nixosModules; [
    raspberry-pi-3.base
  ];

  networking.useNetworkd = true;
  networking.useDHCP = false;
  systemd.network.networks."10-eth0" = {
    matchConfig.Name = "eth0";
    networkConfig.DHCP = "yes";
  };

  systemd.services.systemd-networkd-wait-online.enable = lib.mkForce false;
  systemd.services.NetworkManager-wait-online.enable = lib.mkForce false;

  # Strip the kernel down to what a Pi 3 + headless OctoPrint-class
  # appliance actually uses. The stock nvmd kernel inherits NixOS's
  # "enable every driver as a module" policy, which on aarch64 emulation
  # adds ~60 minutes of cc1 invocations for AMD GPUs, Intel Xe, NFC, MPLS,
  # InfiniBand, server-class memory, virt hosts, etc. — none of which a
  # Pi 3 will ever encounter.
  #
  # We keep: USB stack, USB serial (printer), USB ethernet (smsc95xx),
  # mmc, watchdog, i2c/spi/gpio (HAT compat), DRM_VC4 (Pi GPU), ext4,
  # vfat, NFSv4, overlayfs, cgroups, namespaces, netfilter.
  #
  # If you later need something disabled here, override it with
  # `lib.mkForce module` in the host config — but expect a rebuild.
  boot.kernelPatches = [{
    name = "pi3-satellite-strip";
    patch = null;
    # Conservative first pass: only the four biggest non-Pi GPU drivers.
    # Earlier attempts that disabled NVME_CORE, ATA, SCSI, BT, etc. blew
    # up kconfig's interactive-prompt fallback ("Error in reading or end
    # of file" cascade) because too many transitive options became
    # under-specified. The GPU drivers are well-isolated and safely
    # disabled — they account for ~150 MB of module code and a big chunk
    # of compile time.
    #
    # If a future build still feels too long, add disables here ONE
    # category at a time and verify CI is green before adding the next.
    structuredExtraConfig = with lib.kernel; {
      DRM_AMDGPU = lib.mkForce no;
      DRM_RADEON = lib.mkForce no;
      DRM_I915 = lib.mkForce no;
      DRM_XE = lib.mkForce no;
      DRM_NOUVEAU = lib.mkForce no;
    };
  }];
}
