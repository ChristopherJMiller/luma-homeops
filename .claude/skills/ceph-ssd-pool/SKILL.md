---
name: ceph-ssd-pool
description: Add per-node SATA SSDs to galaxy's Rook-Ceph as an `ssd`-class pool (`block-pool-ssd` / StorageClass `rook-ceph-block-ssd`) and move latency-sensitive PVCs (Postgres clusters, app config volumes) onto it — the #2665 plan. Use when the SSDs arrive, when adding any new OSD device, when creating a Ceph pool or StorageClass, or when migrating a PVC between StorageClasses. Encodes the ordering that prevents CRUSH from spilling HDD data onto the SSDs and Rook from grabbing an unpartitioned disk. NEVER lets an SSD OSD join before every existing pool is pinned to deviceClass hdd. NEVER plugs a disk in before the storage config lists devices explicitly by-id. NEVER wipes a device without matching its serial to the new drive. NEVER migrates DB/WAL of the existing raw-mode OSDs in place (Rook can't activate it — see Background).
---

# ceph-ssd-pool

Tier by **placement**, not by cache. Three `ssd`-class OSDs (one per node), a second replicated pool restricted to class `ssd`, a second StorageClass, and per-app migration of the PVCs that hurt on SMR spinners. No existing OSD is touched. Issue: #2665.

## Background — why this shape (read once)

- The six HDD OSDs are **raw mode** (`ROOK_CV_MODE=raw`, `block -> /dev/sda`, whole disk, no LVM). Rook starts them with `ceph-volume raw activate --device /dev/sda`, which (Ceph 19.2.6 `rawbluestore.activate`) only scans the device it is given. A DB device added in place with `bluefs-bdev-new-db` would never be attached → OSD won't start. Rook's only supported "DB on SSD" layout is LVM mode (`allowRawMode=false` whenever `metadataDevice` is set), and raw→LVM means wiping and rebuilding every OSD: ~12–24 h per node at 2 of 3 copies, ~7 TB of SMR backfill, for 2 GB of metadata per OSD. Not worth it here.
- Ceph cache tiering is deprecated; bcache under an OSD on immutable Talos is DIY and fragile. Skip both.
- The SSDs are **partitioned** (`p1` ≈ 64 GiB reserved, `p2` = OSD) so that when an SMR drive is replaced with CMR, its new OSD can be built LVM-mode with DB on `p1` — opportunistic, no extra downtime.

Every step below reads state before writing (S1), goes through git for anything Argo owns (S2), and stops for Chris before Ceph writes (S7) and before anything destructive (S4).

## Toolbox

```bash
export KUBECONFIG=/tmp/galaxy-kubeconfig
TOOLS=$(kubectl -n rook get pod -l app=rook-ceph-tools --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
ceph() { kubectl -n rook exec "$TOOLS" -- ceph "$@"; }
```

Watch anything longer than a minute with `Monitor`, not a Bash loop.

## Step 0 — on arrival: verify the drives are what they claim (no cluster changes)

Used enterprise SATA. Read SMART **before** they go in a node — a laptop with a SATA dock, or in the node via a one-shot privileged pod once installed (step 3):

```bash
smartctl -a /dev/sdX | grep -E 'Model|Serial|Power_On_Hours|Media_Wearout|Available_Reservd|Power_Loss_Cap|Unsafe_Shutdown|Reallocated'
```

Reject if: `Power_Loss_Cap_Test` failing (PLP caps dead — the whole reason for buying these), `Media_Wearout_Indicator` < 30, `Available_Reservd_Space` < 100, reallocated sectors > 0. Record each serial — it's the safety check in step 4.

## Step 1 — pin every existing pool to `deviceClass: hdd` (before any SSD exists)

Today every pool uses `replicated_rule` on root `default` with **no device class**. The moment an `ssd` OSD joins, CRUSH spreads HDD-pool data onto it by weight. Pinning while only HDDs exist changes the rule but not a single PG mapping.

Rook-managed pools (git):

```yaml
# cluster/rook-cluster/block-pool.yaml — CephBlockPool block-pool
spec:
  failureDomain: host
  deviceClass: hdd          # <- add
  replicated: { size: 3 }

# cluster/rook-cluster/fs-pool.yaml — CephFilesystem fs-pool
spec:
  metadataPool: { deviceClass: hdd, replicated: { size: 3 } }   # <- add deviceClass
  dataPools:    [ { deviceClass: hdd, replicated: { size: 3 } } ]
```

Ceph-owned pools (`.mgr`, `.nfs`) have no CR — after the Rook rules exist, one Ceph write each (S7: show Chris, get a yes):

```bash
ceph osd crush rule create-replicated replicated_hdd default host hdd
ceph osd pool set .mgr crush_rule replicated_hdd
ceph osd pool set .nfs crush_rule replicated_hdd
```

**Verify (must all hold before step 2):**

```bash
ceph osd pool ls detail | grep -E 'crush_rule'          # every pool on an hdd-class rule
ceph osd crush rule dump | jq -r '.[] | "\(.rule_name): \(.steps[]|select(.op=="take")|.item_name)"'   # take default~hdd
ceph -s | grep -E 'misplaced|remapped'                    # nothing — zero data movement
ceph pg dump pgs_brief 2>/dev/null | grep -vc 'active+clean'   # 0
```

If PGs remap here, stop: something differed from the assumption that only HDD OSDs exist.

## Step 2 — storage config: explicit devices by-id (before plugging in)

`deviceFilter: ^sd.` would hand a freshly-inserted `sdc` to Rook as an OSD before it's partitioned. Replace it with explicit per-node devices. Current HDD ids (2026-09-20 — re-read with the command if in doubt):

```bash
kubectl -n rook exec deploy/rook-ceph-osd-2 -- sh -c 'ls -l /dev/disk/by-id/ata-ST8000* | grep -v part'
```

```yaml
# cluster/rook-cluster/cluster.yaml
  storage:
    useAllNodes: false
    useAllDevices: false
    config:
      osdsPerDevice: "1"
    nodes:
      - name: top
        devices:
          - name: /dev/disk/by-id/ata-ST8000DM004-2U9188_ZR15WYG5   # sda
          - name: /dev/disk/by-id/ata-ST8000DM004-2U9188_ZR15WBG1   # sdb
      - name: middle
        devices:
          - name: /dev/disk/by-id/ata-ST8000DM004-2U9188_ZR15X13V
          - name: /dev/disk/by-id/ata-ST8000DM004-2U9188_ZR166HF3
      - name: bottom
        devices:
          - name: /dev/disk/by-id/ata-ST8000DM004-2U9188_ZR15WZVR
          - name: /dev/disk/by-id/ata-ST8000DM004-2U9188_ZR15WBHQ
    onlyApplyOSDPlacement: false
```

Also add the per-class OSD resources now — nodes sit at 75–85 % memory *requests*; a default 4 Gi OSD pod per node would eat the last headroom, and a 480 GB OSD doesn't need it:

```yaml
  resources:
    osd:      { requests: { cpu: 500m, memory: 4Gi }, limits: { memory: 4Gi } }   # existing (hdd)
    osd-ssd:  { requests: { cpu: 250m, memory: 1536Mi }, limits: { memory: 2Gi } } # <- add; Rook doc: `osd-<deviceClass>`
```

Commit, push, watch Argo. **Verify:** `kubectl -n rook get pods -l app=rook-ceph-osd` unchanged (same 6 pods, no restarts); the `rook-ceph-osd-prepare-<node>` job logs say each existing device is already an OSD and is skipped; `ceph osd tree` unchanged. Only now is it safe to plug the SSDs in.

## Step 3 — physical install (same open-case window as #2603)

Per node: SATA data lead from the OCuLink→4×SATA breakout (the two HDDs use two of the four — confirm), SATA power, 2.5"→3.5" bracket in the D-313SE-MATX bay. Nodes are control-plane + etcd + mon: **one node at a time, powered off cleanly** — `kubectl drain`, `ceph osd set noout` first, `unset noout` after it's back and `active+clean` (S5). Between nodes: `ceph -s` healthy, quorum 3/3, all OSDs up.

After each node returns: `talosctl get disks` shows the SSD (it will not be an OSD — step 2 guarantees Rook ignores it).

## Step 4 — zap and partition (destructive; serial check first)

One-shot privileged pod on the node (`kubectl -n kube-system run … --overrides` with `privileged: true`, `/dev` hostPath, `nodeName`, image `alpine` + `apk add sgdisk smartmontools util-linux`). **Before any write, match the serial against step 0's list:**

```bash
DEV=/dev/sdc; smartctl -i $DEV | grep -E 'Model|Serial'      # must be one of the S4510 serials
lsblk -o NAME,SIZE,MODEL,SERIAL $DEV                         # ~447G, INTEL SSDSC2KB480G8
```

Then:

```bash
sgdisk --zap-all $DEV && blkdiscard $DEV
sgdisk -n1:0:+64G -t1:8300 -c1:ceph-db-reserved \
       -n2:0:0    -t2:8300 -c2:ceph-osd $DEV
partprobe $DEV; lsblk $DEV
ls -l /dev/disk/by-id/ | grep INTEL_SSDSC2KB480G8_<serial>   # note the -part2 id
```

Add `p2` to that node's device list (git):

```yaml
          - name: /dev/disk/by-id/ata-INTEL_SSDSC2KB480G8_<serial>-part2
            config:
              deviceClass: ssd     # Rook auto-detects from rotational=0; make it explicit
```

Commit, push. Rook's prepare job creates the OSD (raw mode on the partition). **Verify:**

```bash
ceph osd tree                       # new osd on this host, class ssd, CRUSH weight ~0.4
ceph osd df tree | grep ssd
ceph -s                             # still zero misplaced — step 1 is doing its job
kubectl -n rook get pod -l ceph-osd-id=<new> -o jsonpath='{.items[0].spec.containers[0].resources}'   # the osd-ssd numbers
ceph config show osd.<new> osd_memory_target                  # Rook sets it equal to the 2Gi memory limit
```

If `ceph -s` shows misplaced objects after an SSD OSD joins, a pool escaped step 1 — `ceph osd pool ls detail` finds it; fix its rule before the next node. Repeat step 4 per node.

## Step 5 — pool + StorageClass (git)

```yaml
# cluster/rook-cluster/block-pool-ssd.yaml
---
apiVersion: ceph.rook.io/v1
kind: CephBlockPool
metadata: { name: block-pool-ssd, namespace: rook }
spec:
  failureDomain: host
  deviceClass: ssd
  replicated: { size: 3 }
---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: rook-ceph-block-ssd        # NOT the default class; rook-ceph-block stays default
provisioner: rook.rbd.csi.ceph.com
parameters:                        # identical to rook-ceph-block except pool:
  clusterID: rook
  pool: block-pool-ssd
  imageFormat: '2'
  imageFeatures: layering,fast-diff,object-map,deep-flatten,exclusive-lock
  csi.storage.k8s.io/provisioner-secret-name: rook-csi-rbd-provisioner
  csi.storage.k8s.io/provisioner-secret-namespace: rook
  csi.storage.k8s.io/controller-expand-secret-name: rook-csi-rbd-provisioner
  csi.storage.k8s.io/controller-expand-secret-namespace: rook
  csi.storage.k8s.io/node-stage-secret-name: rook-csi-rbd-node
  csi.storage.k8s.io/node-stage-secret-namespace: rook
  csi.storage.k8s.io/fstype: ext4
reclaimPolicy: Retain
allowVolumeExpansion: true
```

Add to `cluster/rook-cluster/kustomization.yaml`. **Verify:** `ceph osd pool ls detail | grep block-pool-ssd` → rule takes `default~ssd`; a throwaway 1 Gi PVC on the new class binds and `rbd -p block-pool-ssd ls` lists its image; delete the PVC + purge the PV (Retain).

Capacity: 3 × ~410 GB replicated ×3 ≈ **400 GB usable**. Keep the pool under 75 % — `ceph df` — it's the only place these workloads can live.

## Step 6 — migrate PVCs, app by app

Candidates (2026-09-20): all `acid-*` Postgres volumes, `mm-{lidarr,prowlarr,radarr,sonarr,sabnzbd}-config`, `mm-plex-config`, `home-assistant-persist`, `mosquitto`, `gonic-data`, `gonic-playlists`, `filebrowser-data`, `alertmanager-*`, `data-trivy-server-0`. **Stay on HDD:** `mm-media` (3 T), `prometheus-*` (2×200 G), `gonic-music`, `gonic-cache`, `attic`, `registry-cache`.

Rehearse the whole pattern once on something small and low-value (`gonic-playlists`, 1 Gi) before touching a database.

### 6a — config volumes (stop → copy → switch → start)

Argo self-heals `kubectl scale` within a minute, so scaling goes through git:

1. Commit `replicas: 0` (raw Deployment) or the chart's `replicaCount`/equivalent → push → pod gone, RBD unmapped.
2. New PVC in git alongside the old one (new name, `storageClassName: rook-ceph-block-ssd`, same size) → push → Bound.
3. One-shot copy Job (`alpine` + `apk add rsync`), both PVCs mounted, on the same node:
   ```bash
   rsync -aHAX --numeric-ids --info=progress2 /old/ /new/ && sync
   ```
   then `diff <(cd /old && find . | sort) <(cd /new && find . | sort)` empty, and `du -s` within a few KB.
4. Commit: app points at the new claim, `replicas` back → push → app up, check its own health (arr UI, HA, etc.).
5. After a day: delete the old PVC in git; its PV is `Retain` → `kubectl patch pv … reclaimPolicy Delete` then delete the PV so the RBD image is freed (S4: confirm; see how the fit PVs were purged).

### 6b — Postgres (`acid-*`, Zalando operator): clone onto the new class

The operator won't migrate PVCs, but it clones by `pg_basebackup` from a live cluster in the same namespace (no `timestamp` = direct clone):

```yaml
# new CR next to the old one, e.g. cluster/media/psql-ssd.yaml
metadata: { name: acid-auth-ssd }
spec:
  clone: { cluster: acid-auth }
  volume: { size: 20Gi, storageClass: rook-ceph-block-ssd }
  # everything else copied from the old CR (users, databases, version, patroni, sidecars)
```

Sequence per cluster: `pg_dump` to somewhere safe first (belt and braces) → apply the clone CR → wait `Running`, row counts match on a couple of tables → **quiesce the app** (replicas 0 via git) → re-clone or let the clone catch up (a direct clone is point-in-time; for anything with writes, clone *while the app is stopped*) → switch the app's DB host to `acid-<x>-ssd` (chart value / env) → start → verify → delete the old `postgresql` CR after a day and purge its PVs. The `-ssd` suffix is permanent; it's cosmetic.

`acid-media` is 2 × 200 Gi *requested*; check `SELECT pg_database_size(current_database())` and clone with a right-sized `volume.size`.

Alternative worth trying on `acid-royaltracker` (small, 2 instances) first: change `volume.storageClass` on the existing CR, then delete the **replica's** PVC + pod so it rebuilds on the new class, `patronictl switchover`, repeat for the old primary. Zero rename, minimal downtime — *if* the operator recreates the StatefulSet's volumeClaimTemplate; verify it does before relying on it.

### 6c — Argo/Helm gotchas

- Helm-rendered PVCs (`mega-media`, `home-assistant`, `mosquitto`): the claim name usually comes from `persistence.existingClaim` / `storageClass` values — create the SSD PVC as a raw manifest in the app's `cluster/<app>/` dir and point the chart at it.
- Never `kubectl scale` / `kubectl edit` these; commits only.

## Cleanup found on the way (do with the first migration)

- `authentik/*` — the whole namespace is orphaned: authentik was removed 2026-09-23 (oauth2-proxy replaced it). 4 × 8 Gi redis PVCs + 2 × 20 Gi pgdata-acid-auth. Delete PVCs, purge PVs.
- `default/pgdata-acid-royaltracker-0` — a stray in the `default` namespace; confirm nothing binds it, purge.

## Rollback

- Steps 1–2: git revert; no data moved.
- Step 4: `ceph osd out <id>` → `ceph osd purge <id> --yes-i-really-mean-it` (S7; the OSD holds nothing yet) → remove the device entry → wipe the partition.
- Step 6: the old PVC/cluster still exists until you delete it — switch the app back in git.

## Related

- `ceph-doctor` — before and between every step; PGs must be `active+clean` throughout.
- `safe-rollout` — watching Rook prepare jobs and app restarts.
- `edge-ingress` — nothing here touches ingress; listed so nobody looks.
- #2665 (this plan), #2661 (boot NVMes — separate), #2603 (cooling; same open-case window).
