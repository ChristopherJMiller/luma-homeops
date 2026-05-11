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
    extraStructuredConfig = with lib.kernel; {
      # --- Other-vendor GPU drivers (Pi has VC4 only) ---
      DRM_AMDGPU = lib.mkForce no;
      DRM_RADEON = lib.mkForce no;
      DRM_I915 = lib.mkForce no;
      DRM_XE = lib.mkForce no;
      DRM_NOUVEAU = lib.mkForce no;
      DRM_MGAG200 = lib.mkForce no;
      DRM_AST = lib.mkForce no;
      DRM_QXL = lib.mkForce no;
      DRM_BOCHS = lib.mkForce no;
      DRM_VMWGFX = lib.mkForce no;
      DRM_GMA500 = lib.mkForce no;
      DRM_HYPERV = lib.mkForce no;
      DRM_VIRTIO_GPU = lib.mkForce no;

      # --- Audio (headless, no speakers/HATs with audio) ---
      SOUND = lib.mkForce no;
      SND = lib.mkForce no;

      # --- Bluetooth (we don't pair anything) ---
      BT = lib.mkForce no;
      BT_HCIUART = lib.mkForce no;

      # --- NFC ---
      NFC = lib.mkForce no;

      # --- Server-class memory features Pi 3 lacks ---
      DAX = lib.mkForce no;
      CXL_BUS = lib.mkForce no;
      LIBNVDIMM = lib.mkForce no;

      # --- Virtualization host (Pi 3 can't host VMs usefully) ---
      KVM = lib.mkForce no;
      XEN = lib.mkForce no;

      # --- Storage Pi 3 doesn't have (only mmc + USB) ---
      ATA = lib.mkForce no;
      SCSI = lib.mkForce no;
      NVME_CORE = lib.mkForce no;

      # --- Software RAID / LVM ---
      BLK_DEV_DM = lib.mkForce no;
      MD = lib.mkForce no;

      # --- Filesystems we don't use (keep: ext4, vfat, nfs4, overlay, tmpfs) ---
      BTRFS_FS = lib.mkForce no;
      XFS_FS = lib.mkForce no;
      JFS_FS = lib.mkForce no;
      REISERFS_FS = lib.mkForce no;
      OCFS2_FS = lib.mkForce no;
      GFS2_FS = lib.mkForce no;
      F2FS_FS = lib.mkForce no;
      NTFS3_FS = lib.mkForce no;
      UDF_FS = lib.mkForce no;
      HFS_FS = lib.mkForce no;
      HFSPLUS_FS = lib.mkForce no;
      CIFS = lib.mkForce no;
      CEPH_FS = lib.mkForce no;
      AFS_FS = lib.mkForce no;

      # --- Enterprise networking ---
      MPLS = lib.mkForce no;
      NET_DSA = lib.mkForce no;
      L2TP = lib.mkForce no;

      # --- Legacy buses Pi 3 doesn't have (Pi 4+ has PCIe, Pi 3 doesn't) ---
      PCMCIA = lib.mkForce no;
      FIREWIRE = lib.mkForce no;

      # --- Crypto hardware accelerators ---
      CRYPTO_DEV_FSL_CAAM = lib.mkForce no;
      CRYPTO_DEV_SAFEXCEL = lib.mkForce no;
      CRYPTO_DEV_INTEL_QAT = lib.mkForce no;
      CRYPTO_DEV_AMD_CCP = lib.mkForce no;

      # --- Things we don't use ---
      INFINIBAND = lib.mkForce no;
      CAN = lib.mkForce no;
      LIRC = lib.mkForce no;
    };
  }];
}
