---
name: satellite-add
description: Add a new satellite (NixOS edge device) to the fleet — scaffolds the host config, wires it into satellites/flake.nix, adds the DHCP reservation in router/ansible, adds Prometheus scrape targets, and produces a flashable SD image via CI. Use when the user wants to onboard a new Pi, SBC, or mini-PC into the satellites/ subsystem. Handles board-specific quirks (Pi Zero 2 W / Pi 3 / Pi 4 / Pi 5) including kernel strips that don't break the build. NEVER bakes secrets into the Nix closure (PSKs go to the SD card filesystem at flash time). NEVER ssh-rebuilds onto a satellite (SAT1 — pull, never push).
---

# satellite-add

Onboard a new satellite into `satellites/` end-to-end: host config, flake outputs, DHCP reservation, Prometheus targets, kernel strip, CI build, flash. The user runs the final flash themselves; this skill drives everything else.

## Hard rules (from CLAUDE.md S1-S5 + satellites/README.md SAT1-5)

- **SAT1: Pull, never push.** Never `ssh ... nixos-rebuild` to a satellite. Edit the repo → push to `satellites/release` → comin pulls. The only exception is a one-time bootstrap to fix a broken seed image (e.g., wrong `comin.repoUrl`), and even then prefer re-flashing.
- **SAT2: Signed commits if `satellites.comin.gpgPublicKeyPaths` is set.** Currently empty for v1, but if Chris turns it on, unsigned tags don't deploy.
- **SAT3: `modules/base.nix` is load-bearing.** A bad networking or sshd change there bricks the device unrecoverably (only re-flash recovers). Treat changes to base.nix like changes to `nodes/controlplane.yaml` — get explicit sign-off, never combine with other risky changes.
- **SAT4: Upper-orbit IP space (`192.168.0.243-.254`) is finite.** Coordinate with `router/ansible/group_vars/vyos_routers.yml` reservations. Don't widen the MetalLB `lan-internal` pool without first shrinking DHCP `range_stop`.
- **SAT5: Verify rollback before declaring done.** After first convergence, demonstrate that pinning the previous signed tag/commit causes the device to revert.

## Secret handling (critical)

- **WiFi PSK never enters the Nix closure.** It lives at `/var/lib/iwd/<SSID>.psk` on the device's mutable filesystem. `scripts/flash.sh` writes it post-`dd` so it survives every comin generation switch. Do NOT use `services.iwd.networks.<SSID>.psk = "..."` — that would bake the PSK into `/nix/store` and the public CI artifact.
- **Per-device SSH host keys** (when we eventually bake them for agenix): generated locally via `scripts/new-host.sh`, stored at `satellites/.keys/<host>_ed25519` (git-crypt encrypted per `.gitattributes`).

## Prerequisites — confirm before starting

1. **Callsign** (hostname, e.g. `weather-sensor`, `kiosk-bedroom`) — descriptive, lowercase, kebab-case
2. **Board** — see "Board matrix" below
3. **Networking** — Ethernet, WiFi, or both? For WiFi need SSID + PSK at flash time.
4. **Role / app modules** — what does it do? OctoPrint, MQTT bridge, frigate, etc.
5. **Static IP slot in upper orbit** — pick from `.243-.254` (check `router/ansible/group_vars/vyos_routers.yml` for what's free)

Ask the user for any missing piece. Don't guess hostnames or IP slots.

## Board matrix

| Board | nvmd module | SoC | RAM | Net | Notes |
|-------|-------------|-----|-----|-----|-------|
| Pi Zero 2 W | `raspberry-pi-02.base` | bcm2710 | 512 MB | WiFi only | Slowest aarch64 Pi; aggressive kernel strip pays off |
| Pi 3B (original) | `raspberry-pi-3.base` | bcm2837 | 1 GB | eth + wifi, **shared MAC** | What `octoprint` runs on. WiFi reservation Just Works because eth/wifi share MAC. |
| Pi 3B+ | `raspberry-pi-3.base` | bcm2837 | 1 GB | eth + wifi, **separate MACs** | Reserve the MAC of whichever interface you'll use. |
| Pi 4B | `raspberry-pi-4.base` | bcm2711 | 1-8 GB | gigabit eth + wifi, **separate MACs**, PCIe | Don't strip `PCI` (Pi 4 has PCIe). |
| Pi CM4 | `raspberry-pi-4.base` + custom config.txt | bcm2711 | 1-8 GB | depends on carrier | Boot config varies by carrier board. |
| Pi 5 | `raspberry-pi-5.base` | bcm2712 | 4-16 GB | gigabit eth + wifi, PCIe, NVMe support | Most capable. Keep `NVME_CORE`, `DAX` if using NVMe. |
| Other aarch64 SBC | n/a | varies | varies | varies | Skip nvmd; use upstream `<nixpkgs>/nixos/modules/installer/sd-card/sd-image-aarch64.nix`. |
| x86 mini-PC | n/a | x86_64 | varies | varies | Different image format entirely — installer ISO, not SD image. Out of v1 scope but plumbing exists. |

If the board isn't in `satellites/modules/boards/`, create a new module first (see "Adding a new board" below).

## End-to-end workflow

### 1. Verify the callsign and IP slot

```bash
# Existing hosts (so you don't collide)
ls satellites/hosts/
grep -E '^\s+- name:' router/ansible/group_vars/vyos_routers.yml
```

### 2. Scaffold the host directory

Use `satellites/scripts/new-host.sh <callsign> <board>` — it generates the SSH host keypair into `.keys/` (git-crypt'd) and creates `hosts/<callsign>/{default,hardware}.nix`.

If the script doesn't exist for your case, hand-craft from `satellites/hosts/octoprint/` as a template.

`hosts/<callsign>/default.nix` skeleton:

```nix
{ config, lib, pkgs, ... }:

{
  imports = [
    ./hardware.nix
    ./secrets.nix  # placeholder; empty until agenix needed
  ];

  satellites.comin.autoReboot = {
    enable = true;        # default off — opt in per host
    window = "03:00";     # local maintenance window
  };

  # ... per-role services here (e.g. services.octoprint, services.mosquitto, ...)
}
```

### 3. Wire into `satellites/flake.nix`

Add an entry to `nixosConfigurations`:

```nix
nixosConfigurations = {
  octoprint = mkHost { ... };
  <callsign> = mkHost {
    hostName = "<callsign>";
    board = "<board>";       # matches satellites/modules/boards/<board>.nix
    modules = [ ./hosts/<callsign> ];
  };
};
```

And to `packages.aarch64-linux` (or x86_64 if relevant):

```nix
packages.aarch64-linux = {
  sdImage-octoprint = ...;
  sdImage-<callsign> = (mkBootstrap {
    hostName = "<callsign>";
    board = "<board>";
  }).config.system.build.sdImage;
};
```

### 4. Add CI matrix entry

`.github/workflows/satellites-sd-images.yml`:

```yaml
strategy:
  matrix:
    host: [octoprint, <callsign>]
```

### 5. Add DHCP reservation

`router/ansible/group_vars/vyos_routers.yml`, under `dhcp.static_mappings`, in the upper-orbit comment block:

```yaml
- name: <callsign>
  mac: "xx:xx:xx:xx:xx:xx"     # see MAC strategy below
  ip: 192.168.0.<243-254>
```

Apply via the `vyos-deploy` skill (commit-confirm 10 for DHCP changes per S8).

**MAC strategy:**
- If hardware is already plugged in on its old image, look it up: `ssh chris@192.168.0.1 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show dhcp server leases | grep -i <callsign-or-mac-prefix>'`
- Pi MACs use OUI `b8:27:eb:` (Foundation), `dc:a6:32:` (later models), `e4:5f:01:` (Pi 5)
- If the device hasn't been on the network: first-boot it on WiFi (no reservation), let it grab a dynamic lease, then read the MAC and add the reservation
- For Pi 3B+ / Pi 4 / Pi 5: pick the MAC of the interface you'll actually use (eth or wifi)

### 6. Add Prometheus targets

`cluster/applications/prometheus-stack.yaml` — append to both `satellites` (node-exporter) and `satellites-comin` jobs:

```yaml
- job_name: satellites
  static_configs:
    - targets:
        - 192.168.0.243:9100   # octoprint
        - 192.168.0.<NEW>:9100 # <callsign>
- job_name: satellites-comin
  static_configs:
    - targets:
        - 192.168.0.243:4243
        - 192.168.0.<NEW>:4243
      labels:
        component: comin
```

### 7. Commit + push

```bash
git add satellites/ router/ansible/group_vars/vyos_routers.yml \
        cluster/applications/prometheus-stack.yaml \
        .github/workflows/satellites-sd-images.yml
git commit -m 'satellites: add <callsign> (<board>)'
git push origin main
git push origin main:satellites/release --force-with-lease
```

CI will build the new SD image on aarch64 runners and push to `christopherjmiller.cachix.org`.

### 8. Flash

Once CI is green:

```bash
WIFI_SSID=<ssid> WIFI_PSK=<psk> ./satellites/scripts/flash.sh <callsign>
# (auto-detects USB SD reader, downloads CI artifact, prompts before dd)
```

Or for an Ethernet-only board, omit the WiFi env vars (script will prompt; just leave SSID blank).

### 9. Watch convergence

```bash
# After Pi boots, poll DHCP table for the new MAC
ssh chris@192.168.0.1 \
  '/opt/vyatta/bin/vyatta-op-cmd-wrapper show dhcp server leases | grep <callsign>'

# Once on the network, verify comin loop on first poll (60s)
ssh admin@<callsign>.local 'systemctl is-active comin; journalctl -u comin -n 30 --no-pager'

# Generation should differ from booted-system if the runtime config has stuff
# the bootstrap doesn't (it usually will)
ssh admin@<callsign>.local 'readlink /run/booted-system /run/current-system'
```

If `current-system` differs from `booted-system`, comin switched successfully. Reboot will happen automatically at the configured maintenance window if a kernel/initrd change was included.

## Adding a new board module

If the board doesn't have a file in `satellites/modules/boards/`, create one. Template based on `pi-3b.nix`:

```nix
{ config, lib, pkgs, nixos-raspberrypi, ... }:

{
  imports = with nixos-raspberrypi.nixosModules; [
    raspberry-pi-<N>.base       # 02, 3, 4, or 5
    # Optional add-ons for Pi 5:
    # raspberry-pi-5.page-size-16k
    # raspberry-pi-5.display-vc4
  ];

  networking.useNetworkd = true;
  networking.useDHCP = false;

  # Ethernet (skip for Pi Zero 2 W which has none)
  systemd.network.networks."10-eth0" = {
    matchConfig.Name = "eth0";
    networkConfig.DHCP = "yes";
  };

  # WiFi (for any board with onboard radio)
  networking.wireless.iwd.enable = true;
  systemd.network.networks."20-wlan0" = {
    matchConfig.Name = "wlan0";
    networkConfig.DHCP = "yes";
  };

  systemd.services.systemd-networkd-wait-online.enable = lib.mkForce false;
  systemd.services.NetworkManager-wait-online.enable = lib.mkForce false;

  # Kernel strip — START CONSERVATIVE. See "Kernel strip pitfalls" below.
  boot.kernelPatches = [{
    name = "<board>-strip";
    patch = null;
    structuredExtraConfig = with lib.kernel; {
      # ALWAYS-SAFE: non-Pi GPU drivers
      DRM_AMDGPU  = lib.mkForce no;
      DRM_RADEON  = lib.mkForce no;
      DRM_I915    = lib.mkForce no;
      DRM_XE      = lib.mkForce no;
      DRM_NOUVEAU = lib.mkForce no;
      # Add per-board safe disables only after CI proves them green.
    };
  }];
}
```

Board-specific tips:
- **pi-02 (Zero 2 W)**: no Ethernet — drop the `10-eth0` block. 512 MB RAM = consider `zram` configured aggressively. WiFi-only means PSK injection is mandatory at flash time.
- **pi-3b**: see existing file. Shared MAC is the quirk.
- **pi-4**: PCIe present — do NOT add `PCI = no` to the strip set.
- **pi-5**: keep `NVME_CORE` enabled. May want `raspberry-pi-5.page-size-16k` for performance.

## Kernel strip pitfalls (we learned these the hard way)

1. **`extraStructuredConfig` was renamed** to `structuredExtraConfig`. The old name produces a clear error message but it's still confusing.
2. **Don't go aggressive in one shot.** Disabling many top-level Kconfig categories (`ATA`, `SCSI`, `NVME_CORE`, `KVM`, `BT`, etc.) triggers kconfig's interactive prompt fallback when dependent options become under-specified, and the build fails with `Error in reading or end of file` after ~30s. Add disables ONE category at a time, push, watch CI green, then add the next.
3. **Don't import `profiles/perlless.nix`.** Comin → git → Perl, and the closure assertion fails *after* the kernel + everything else has built. Wastes the entire build. Re-enable only with a Perl-free git override.
4. **First build on a new board is slow** (~60-90 min on aarch64 GitHub runners) because cachix doesn't have that kernel hash yet. After it lands in cachix, subsequent builds substitute in ~3-5 min.

## CI / cachix expectations

- Workflow: `.github/workflows/satellites-sd-images.yml`
- Runner: `ubuntu-24.04-arm` (native aarch64, free for public repos)
- Build time: ~3-5 min if kernel cached, ~60-90 min on cold build
- Cachix push: automatic via `cachix-action` if `CACHIX_AUTH_TOKEN` secret is set
- Required secret: `CACHIX_AUTH_TOKEN` in repo settings (token from `christopherjmiller.cachix.org` dashboard)
- Concurrency: `cancel-in-progress: true` per ref — pushing a new commit cancels in-flight builds for that ref

If CI fails with `Pushing is disabled.`, the auth token is missing. Tell the user to add it via the GitHub repo settings UI (gh CLI doesn't have repo-admin scope by default).

## Common operational gotchas

- **DHCP lease stickiness**: when a device reboots, the new request sometimes gets a different IP than the static-mapping because the old lease is still "active" in dhcpd's database. Fix: `ssh chris@192.168.0.1 '/opt/vyatta/bin/vyatta-op-cmd-wrapper clear dhcp-server lease <old-ip>'` then have the device renew (reboot or `dhclient -r && dhclient`).
- **comin repo URL typos**: the bootstrap image has the URL baked in. If `satellites.comin.repoUrl` is wrong, comin loops on `authentication required` (libgit2's response to a 404). Only remedy is re-flash with a corrected image — `ssh ... nixos-rebuild` is denied by SAT1.
- **mDNS doesn't traverse routers.** `<callsign>.local` only resolves on the same L2 segment. If the resolver is on a different subnet (e.g., k8s pods), set up an mDNS reflector or use unicast DNS.
- **Pi 3 (original B model) shares MAC across eth0/wlan0.** Pi 3B+ and later split them. Pick reservation accordingly.
- **SSH banner timeouts during heavy switch.** When comin is mid-eval/mid-build, Pi 3 sshd can fail the banner exchange. Pi pings fine. Wait it out (load average will drop).

## Verification gates (run all before declaring done)

```bash
# 1. SSH works as admin
ssh admin@<callsign>.local 'uname -a; uptime'

# 2. comin is running and converged
ssh admin@<callsign>.local 'systemctl is-active comin'
ssh admin@<callsign>.local 'readlink /run/booted-system /run/current-system'
# After a switch, current != booted is fine — auto-reboot timer handles it.

# 3. Prometheus is scraping
KUBECONFIG=/tmp/galaxy-kubeconfig kubectl -n monitoring exec -it \
  prometheus-prometheus-stack-kube-prom-prometheus-0 -- \
  promtool query instant http://localhost:9090 \
  'up{job="satellites",instance="192.168.0.<NEW>:9100"}'
# Expect value=1

# 4. comin metrics also scraping
# Same as above with job="satellites-comin", port :4243

# 5. mDNS discovery
resolvectl query <callsign>.local
# Expect the IP. If it fails, check avahi on the device: systemctl status avahi-daemon

# 6. Rollback drill (SAT5)
# Pin satellites/release to the previous commit; watch the device revert.
# Don't skip this for the FIRST satellite on a new board class.
```

## What this skill does NOT do

- **Apply ansible to the router** — that's `vyos-deploy`. This skill produces the DHCP reservation edit; deploying it is a separate step.
- **Build the SD image locally.** It pushes to CI and downloads the artifact. Use `--build` flag on `flash.sh` only as a fallback if CI is broken.
- **Mutate live satellite config via SSH.** Always edits flow through git → comin.
- **Decide what services to run on the satellite.** That's a design call the user makes; this skill scaffolds plumbing.
- **Generate or manage WiFi credentials.** PSK comes from the user at flash time, never in git.
- **Set up cluster-side service ingress / Authentik for the satellite's services.** That's a separate task (split-ingress pattern lives in `cluster/`).
