# bmcctl

The three nodes' BMCs (ASRock Rack X570D4I-2T, AST2500, AMI MegaRAC firmware
1.90, BIOS P2.50) as one CLI: power and temperatures, the boot-relevant BIOS
settings, a one-time boot override, and **remote install media**. With it, a node
whose system disk was replaced can be reinstalled with no USB stick and no one at
the console.

- Run: `bmcctl …` inside `nix develop`.
- Test: `nix develop --command python -m pytest -q tools/bmcctl`
- Credentials: `nodes/bmc.env` (git-crypt), read per call; never printed.
- Nodes and BMC addresses: `src/bmcctl/nodes.py` (top .119, middle .117, bottom .151).

## Commands

| Command | Does |
|---|---|
| `bmcctl status [node…]` | power, CPU/board temps, fans, boot override (now + staged), boot-relevant BIOS (current → staged), remote media |
| `bmcctl iso` | fetch the Talos ISO that matches `machine.install.image` in `nodes/controlplane.yaml` into `~/.cache/luma-homeops/install-media/` |
| `bmcctl serve [--hours 6]` | serve that ISO to the BMCs (asks for sudo once, for the firewall) |
| `bmcctl unserve` | stop serving, close the firewall |
| `bmcctl mount NODE` | attach the served ISO as the node's virtual CD (the host sees USB `Virtual CDROM0`) |
| `bmcctl eject NODE` | detach it (remote media stays enabled, idle) |
| `bmcctl power NODE status\|on\|restart\|off` | power state / on; `restart` and `off` are hard (ForceRestart/ForceOff) and need `--force`; shut a running node down with `talosctl shutdown` |
| `bmcctl boot NODE cd\|disk\|clear` | one-time UEFI boot override for the next boot |
| `bmcctl bios NODE [--set uefi\|legacy]` | show, or stage for the next POST, the boot profile |

## How install media works

The ISO is derived, not stored. `install.image` names the Image Factory schematic
and version, and the factory builds the matching ISO (`bmcctl iso`).

The BMC mounts remote media only from NFS, CIFS or an http(s) URL. Pulling straight
from factory.talos.dev does not work: plain HTTP redirects to HTTPS, and range
requests redirect again. So rowlett serves the cached ISO itself:

- **darkhttpd**, from the flake's nixpkgs, runs in a systemd **user** unit
  (`bmc-media-http`). It is read-only, has no listing, and supports byte ranges.
  It needs no root.
- **The firewall is the one root step.** rowlett's NixOS firewall admits nothing
  on TCP 8740. A root transient unit (`bmc-media-fw`) inserts an ACCEPT for the
  three BMC addresses only, and its `ExecStopPost` removes it. `sudo` goes through
  the desktop askpass (ksshaskpass), so the password prompt appears on the
  desktop.
- **Both units stop themselves** after `--hours`, so a forgotten `unserve` does not
  leave a port open.

The BMC side splits by API:

- **Redfish** does BIOS settings (staged in `Systems/Self/Bios/SD`), the boot
  override (staged in `Systems/Self/SD`; this firmware rejects it on
  `Systems/Self`) and the remote-media on/off switch (`AMIVirtualMedia.EnableRMedia`).
- **Mount and eject use the web UI's JSON API** (`/api/settings/media/…`), because
  this firmware's Redfish VirtualMedia collection stays empty.

## Firmware quirks (learned 2026-10-27 on top's BMC)

- **Don't switch remote media off and on through the web API's PUT.** After that,
  top's BMC stopped listing the configured image (`remote/images` stayed `[]`
  through refreshes) until a BMC reset (Redfish `Manager.Reset` `ForceRestart`,
  about 3 minutes; the host keeps running). bmcctl uses Redfish's `EnableRMedia`
  for the switch.
- **`active_redirections` answers 500 code 16416** ("Error while getting Media
  Info") whenever nothing is redirecting. That is the empty answer, not a fault;
  an untouched BMC does the same.
- **The media PUT must carry `hd_remote_retry_count` and `hd_remote_retry_interval`**
  (the UI round-trips `rmedia_retry_*` under those names). Without them the BMC
  saves the rest and answers 500 "Error in setting Remote Media Misc Configuration".
- **Saving the media settings registers an http image** in `remote/images` within
  about 8 s. **Don't call `remote/rmediarefreshlist`** for an http source: it
  empties the list, because it re-scans NFS/CIFS shares and finds nothing at an
  http URL. That was the real cause of the "wedge" above, made worse by the
  off/on toggle.
- **A started image leaves `remote/images`** and shows up in
  `active_redirections` with `state: 1`.
- **Redfish's `RMediaStatus` lags the real state** by minutes; trust
  `/api/settings/media/general`.
- **These settings are staged until the next POST**: the BIOS profile and the boot
  override both take effect at the next boot. `bmcctl status` shows anything staged.

## BIOS profiles

`legacy` is how all three nodes ran until 2026-10. `uefi` (boot filter, storage
and video OpROM all set to UEFI-only) is #2596's proposed fix for the 5700G iGPU.
In legacy/CSM boot the firmware publishes no ACPI VFCT table, so amdgpu cannot
find the iGPU vBIOS. The ASPEED BMC VGA stays primary either way. Attribute names
and values are listed in `src/bmcctl/bios.py`.
