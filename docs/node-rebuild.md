# Rebuilding a node onto a new system disk

A node's NVMe holds Talos, etcd, the Rook mon store (`/var/lib/rook`) and the
containerd cache. The Ceph OSDs sit on the SATA disks and survive a rebuild. This
procedure swaps that disk and reinstalls Talos over the BMC, with no USB stick and
no console. It was first run on 2026-10-27 for the #2661 NVMe swaps. One node at a
time (S5).

Tools: `bmcctl` (tools/bmcctl/README.md), `talosctl`, `kubectl`, and `ceph` via
`rook-ceph-tools`, all inside `nix develop`.

| Node | IP | BMC | etcd member ID (2026-10-27) |
|---|---|---|---|
| top | 192.168.0.240 | 192.168.0.119 | f47a4d5e07e564af |
| middle | 192.168.0.241 | 192.168.0.117 | f81edc2ee2f1d6cc |
| bottom | 192.168.0.242 | 192.168.0.151 | 8ebb26b3a997a237 |

Facts this relies on, checked 2026-10-27:

- **One machine config serves all three nodes.** `nodes/controlplane.yaml` matched
  the live config on all three, apart from YAML quoting. Hostnames come from the
  router's DHCP reservations by MAC, so they survive a disk swap.
  `install.disk: /dev/nvme0n1` is the only M.2 slot and cannot match a Ceph disk.
- **A cold cache strands nothing.** All 110 running images still resolved upstream.
  A rebuilt node pulls everything again; docker.io pulls go through the in-cluster
  mirror at .231.
- **The kubelet serving CSR is approved automatically**
  (`kubelet-serving-cert-approver`).
- **The Rook mon recovers by itself.** With an empty `/var/lib/rook/mon-X`, its
  `init-mon-fs` runs `ceph-mon --mkfs` and the mon syncs from quorum. Rook fails a
  mon over after **2 h** out of quorum (`healthCheck.daemonHealth.mon.timeout`), and
  on 3 nodes that strands it. Keep each node's outage under that, or raise it first.

**Never run `talosctl bootstrap`.** The cluster exists, and a rebuilt control-plane
node joins it. Bootstrapping would start a second etcd cluster.

## Once, before the first node

```bash
export TALOSCONFIG=$PWD/nodes/talosconfig
# A kubeconfig pinned to a node that stays up (re-point it per node; the VIP is fine too).
talosctl -n 192.168.0.240,192.168.0.241,192.168.0.242 -e 192.168.0.242 etcd status
mkdir -p ~/etcd-snapshots
talosctl -n 192.168.0.242 -e 192.168.0.242 etcd snapshot ~/etcd-snapshots/galaxy-$(date +%Y%m%d-%H%M).db
bmcctl serve --hours 10          # one desktop sudo prompt (firewall); stops itself
bmcctl status                    # power, temps, BIOS boot profile, media, per BMC
```

## Per node

### A. Take it out (node still up)

1. **Gate.** All nodes Ready; etcd 3/3 healthy; `ceph -s` shows only the standing
   warnings and all PGs `active+clean`; no unexpected Pending pods.
2. **etcd leader off it:** `talosctl -n IP -e IP etcd forfeit-leadership`, if it
   leads.
3. **Patroni switchovers** for every primary on this node that has a replica
   elsewhere. Do them before cordoning (known_node_maintenance). Single-instance
   clusters (`acid-attic`, `acid-ha`) just move with the drain.
4. **noout for its OSDs:** `ceph osd set-group noout NODE`.
5. **Cordon, then evict in batches of about 8**, most important first, watching the
   receiving nodes. The media stack on middle is `nodeName`-pinned: leave it. Two
   nodes cannot hold three nodes' requests, so pick what waits Pending: Prometheus,
   immich-ml, trivy-server.
6. **Stage the boot profile** if this node goes UEFI: `bmcctl bios NODE --set uefi`
   (applies at the next POST).
7. **Shut it down:** `talosctl -n IP -e IP shutdown --force`. Wait for
   `bmcctl power NODE status` to read Off.

### B. At the bench

Unplug it and pull it. Then:

- Swap the NVMe, and label the old one with the node and date.
- Case fans go on **FAN2** (empty on every board; the BMC drives it).
- Fit the cooler only if it takes **LGA115x screw mounting with M3 screws** (the board
  has no AM4 mount); otherwise clean and repaste the old one.
- middle only: reseat the DIMMs and leave the RX 580 in.

Rack it, plug it in, and don't press power. The BMC returns about 2 min after AC,
and the node may power itself on and stop at "no boot device". That is fine.

### C. Reinstall

```bash
bmcctl mount NODE && bmcctl boot NODE cd && bmcctl power NODE restart --force
talosctl -n IP -e IP get disks --insecure              # maintenance mode: new NVMe is nvme0n1
talosctl -n PEER -e PEER etcd remove-member MEMBER_ID  # PEER = a healthy node
talosctl apply-config --insecure -n IP -e IP --file nodes/controlplane.yaml
bmcctl eject NODE                                      # the ISO runs from RAM; the override was one-shot
```

If talosctl 1.14 and the 1.10 maintenance API disagree, use
`~/.cache/luma-homeops/talosctl-v1.10.9` (checksum-verified).

Watch it install, reboot from the NVMe and join: `talosctl -n IP -e IP dmesg -f`,
`talosctl -n PEER -e PEER etcd members`, `kubectl get node NODE -w`.

### D. Back into service

1. **Uncordon as soon as it is Ready** (cordoned, its own Ceph pods can't come
   back). Then `ceph osd unset-group noout NODE`.
2. **Verify:**
   - `talosctl -n IP version` is v1.10.9, and `get extensions` lists amdgpu and amd-ucode.
   - etcd 3/3 healthy, mon back in quorum, every OSD up, PGs `active+clean`.
   - The node has no Pending or crash-looping pods.
3. **UEFI check:** `talosctl -n IP ls /sys/firmware/efi` exists, and dmesg shows
   amdgpu initializing `0000:30:00.0` (the iGPU), plus `0000:10:00.0` (the RX 580)
   on middle.
4. **Temperatures:** `bmcctl status NODE`, against the 7-day baseline in #2603.

## Rollback

- **The ISO won't boot under UEFI:** run `bmcctl bios NODE --set legacy`, then
  `bmcctl power NODE restart --force`. The ISO boots legacy, as before; carry on,
  and that node's iGPU stays dead.
- **The install or join fails and can't be fixed forward:** put the old NVMe back,
  set the legacy profile and boot it. Its etcd member was removed, so wipe its
  stale etcd data before it can rejoin:
  `talosctl -n IP reset --system-labels-to-wipe EPHEMERAL --reboot` (S4: ask first).
- **The node is out longer than about 1h45:** raise the Rook mon timeout before it
  hits 2 h, or expect to `kubectl -n rook scale deploy rook-ceph-mon-X --replicas=1`
  after a stranded failover (known_node_maintenance).
