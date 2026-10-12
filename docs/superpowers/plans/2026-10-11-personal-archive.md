# Personal Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Chris's cold personal files (old Minecraft servers, school projects, old videos, etc. — ~38 GiB now on rowlett's nearly-full disk) a home on galaxy that is NFS-mounted at home, mirrored to B2 nightly, and browsable read-only from a travelling laptop straight out of B2 — then prove every local file is in B2 so Chris can delete the local copies.

**Architecture:** A second static CephFS directory, `/archive`, built exactly like `/family` (static PV/PVC in ns `media`, LAN NFS export to rowlett only, nightly `rclone sync` to its own B2 bucket with a no-delete key, monthly canary). Kept separate from `/family` so it never reaches Immich or the family's shared albums. Laptops don't touch the house at all: a home-manager user service `rclone mount`s both B2 mirrors read-only with a local VFS cache.

**Tech Stack:** Rook-Ceph CephFS + the in-repo ceph-nfs-export operator, Argo CD, Sealed Secrets, rclone 1.75.1 (ghcr, digest-pinned), Backblaze B2 S3 API, NixOS + home-manager (`~/nixos`), b2 CLI v4 (`nix shell nixpkgs#backblaze-b2`).

**Spec:** No separate spec — design agreed in session 2026-10-11 and recorded in "Design decisions" below. Precedents it copies: `cluster/family/`, `cluster/rook-cluster/family-export.yaml`, `docs/backups.md`.

## Design decisions (agreed 2026-10-11)

- **Home = NFS, road = B2.** rowlett mounts `/archive` (and `/family`) over NFS on the LAN. celebi mounts the B2 *mirrors* read-only with rclone — no tailscale route, SNAT change or ACL work (that plan is dropped). The mirror trails the cluster by ≤1 day; browsing old files is the only road requirement.
- **B2 stays an incremental backup**, not a sync target: `current/` mirror, overwrites/deletions moved to `deleted/<date>/`, hidden versions purged after 90 days. Uploads and API calls are free; downloads are free up to 3× stored data per month (~1.1 TB at ~370 GiB stored) — laptop browsing costs nothing beyond storage (~$0.25/month for the archive at $6.95/TB).
- **One archive volume, organised by category** (chosen 2026-10-11): `Minecraft/`, `School/{TSA,College}/`, `Projects/`, `Games/`, `Music/` (+ `Sheet Music/`), `Videos/{Recordings,Creative,Unsorted}/`, `Backups/foundry-2024/`, `Documents/`. Ambiguous items go to `Videos/Unsorted/` to be sorted later over NFS. One volume, not one per category: they share one lifecycle and one protection level.
- **Separate bucket `galaxy-archive`** with its own keys, so a family key can never touch the archive and vice versa.
- Later, not here: a Minecraft "boot an old server from cold" project (a pod can mount `/archive/OldMCServers/<world>`); filebrowser access to `/archive`.

## Global Constraints

- SMR OSDs: bulk writes ≤ 8 MB/s (`rsync --bwlimit=6M` for the ingest), with the slow-ops guard from Task 4 running. Sustained writes past ~25 GiB at high rates stall the cluster (docs/backups.md "Rate-limit it").
- Never write to `b2:galaxy-*/current` directly — the nightly `rclone sync` would move it to `deleted/`. Files enter B2 only through `/family` or `/archive`.
- Cluster B2 keys: `listBuckets,listFiles,readFiles,writeFiles` — **never `deleteFiles`**. Laptop keys: `listBuckets,listFiles,readFiles` only.
- Everything under `/archive` is uid/gid 1000; **no `fsGroup`** on pods mounting it (cephfs CSIDriver fsGroupPolicy=File would chown the whole tree).
- One Secret per `*.secret.yaml` (kubeseal reads only the first document). `./sign.sh` skips a `.secret.yaml` whose sealed sibling already exists.
- GitOps (S2): cluster changes go through the repo; Argo applies. Direct pushes to `main` are Chris's preference.
- rclone image is exactly `ghcr.io/rclone/rclone:1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5` (same as family). No Docker Hub images.
- S9: never print B2 key IDs or secrets in chat, commit messages or logs.
- S6: never `--no-verify`; commit through `nix develop` so pre-commit runs.
- **Claude never deletes Chris's local files.** Task 7 hands Chris a verified list; Chris runs the delete.
- Steps marked **(Chris)** need sudo, the B2 master key, or physical access to celebi.

## Review Focus

1. **Empty or unmounted `/archive` at backup time** — must never turn into "move the whole bucket to `deleted/`". Pinned in Task 5 Step 5: run the job with `MIN_FILES` above the real file count (Task 5 runs after ingest, so a genuinely empty volume isn't available) and confirm it exits 2 without touching the bucket.
2. **Laptop offline or B2 unreachable** — the mount must not hang login or freeze Dolphin indefinitely; cached listings should still work. Pinned in Task 6 Step 6.
3. **Lost or stolen laptop** — its key must not be able to write or delete. Pinned in Task 6 Step 5 (key-level write attempt must be denied, not just the mount's `--read-only`).
4. **Laptop disk filled by the VFS cache** — celebi travels with limited disk. Pinned in Task 6 Step 1 (size caps from measured free space) and Step 7 (cache stays under cap).
5. **Files lost in the reorganisation, or odd paths** — an `Old Videos` entry missing from every mapping list would silently stay behind, and paths with spaces and `#` (`…/videothumbs/<hash>#7204.jpg`) can break the verifier or the ingest. Pinned in Task 4 Step 5 (mapping must cover every entry exactly once), Task 4 Step 7 (hash-multiset comparison) and Task 2 Step 3.

---

### Task 1 (Chris): B2 bucket and keys

**Files:** none in git. Records go to the password manager.

**Interfaces:**
- Produces: bucket `galaxy-archive`; keys `galaxy-archive-cluster` (Task 5), `galaxy-archive-laptop-ro` and `galaxy-family-laptop-ro` (Task 6).

- [ ] **Step 1: Authorize the b2 CLI with the master key** (from the password manager). A shell function, not a `$B2` variable: zsh doesn't word-split unquoted variables.

```bash
b2() { nix shell nixpkgs#backblaze-b2 --command b2 "$@"; }
b2 account authorize
```
Expected: prompts for keyID/applicationKey, then prints account info.

- [ ] **Step 2: Create the bucket** — private, SSE-B2, hidden versions purged after 90 days (matches `galaxy-family`)

```bash
b2 bucket create \
  --default-server-side-encryption SSE-B2 \
  --lifecycle-rule '{"daysFromHidingToDeleting": 90, "daysFromUploadingToHiding": null, "fileNamePrefix": ""}' \
  galaxy-archive allPrivate
b2 bucket get galaxy-archive
```
Expected: `bucket get` shows `"bucketType": "allPrivate"`, the lifecycle rule above, and `"defaultServerSideEncryption": {"mode": "SSE-B2", ...}`. (If this b2 CLI version rejects `--lifecycle-rule`, it is the older `--lifecycle-rules '[{…}]'` form taking a JSON array of the same object.)

- [ ] **Step 3: Create the three keys** (each prints its keyID and applicationKey **once** — save each to the password manager immediately)

```bash
b2 key create --bucket galaxy-archive galaxy-archive-cluster   listBuckets,listFiles,readFiles,writeFiles
b2 key create --bucket galaxy-archive galaxy-archive-laptop-ro listBuckets,listFiles,readFiles
b2 key create --bucket galaxy-family  galaxy-family-laptop-ro  listBuckets,listFiles,readFiles
b2 key list --long | grep -E 'galaxy-(archive|family)-(cluster|laptop-ro)'
```
Expected: `key list` shows the three keys with exactly those capabilities — no `deleteFiles` on any of them.

- [ ] **Step 4: Check the operator key reaches the new bucket.** docs/backups.md says `galaxy-operator-laptop` covers "both buckets". If it's an all-buckets key, it now covers `galaxy-archive` too; if it's bucket-restricted, note that in Task 5's docs step.

```bash
b2 key list --long | grep galaxy-operator-laptop
```
Expected: bucket column empty/`-` (all buckets) or a bucket name — record which.

---

### Task 2: `verify-local-in-b2.sh` — "is every local file in B2?"

Built first because it is the acceptance test for Tasks 4, 5 and 7, and it can be proven today against `PhotosVideos`, whose answer is already known.

**Files:**
- Create: `docs/backups/verify-local-in-b2.sh`

**Interfaces:**
- Consumes: rclone remotes, one per bucket (env `RCLONE_CONFIG_<NAME>_*` or rclone.conf). Convention in this plan: `fam:` = a `galaxy-family` key, `arc:` = a `galaxy-archive` key, each loaded from its `cluster/<app>/b2.secret.yaml` without printing the values.
- Produces: `verify-local-in-b2.sh <remote:path>... -- <local dir>...` → prints `MISSING<TAB><bytes><TAB><path>` per local file with no same-MD5 object under any given remote path, then `summary: N files checked, M missing (X bytes)`; exit 0 if none missing, 1 if any, 2 on usage error.

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# Is every local file already in B2? Matches by content (MD5), not path, so
# renamed folders and files that moved between buckets still count. Use it
# before deleting a local copy of anything that is supposed to be mirrored.
#
#   docs/backups/verify-local-in-b2.sh fam:galaxy-family/current arc:galaxy-archive/current \
#     -- ~/ForMediaServer ~/Videos/"Old Videos"
#
# Remotes are ordinary rclone remotes (rclone.conf, or RCLONE_CONFIG_<NAME>_*
# env as in docs/backups.md). B2 keys are scoped to one bucket, so use one
# remote per bucket — e.g. fam: with a galaxy-family key, arc: with a
# galaxy-archive key.
# Every object rclone wrote carries an MD5, multipart ones included; an object
# without one can only cause a false MISSING, never a false match.
# Skipped: zero-byte files (nothing to lose) and Thumbs.db/.DS_Store (every
# mirror excludes them).
set -euo pipefail

remotes=()
while [ $# -gt 0 ] && [ "$1" != "--" ]; do remotes+=("$1"); shift; done
[ "${1:-}" = "--" ] && shift
if [ ${#remotes[@]} -eq 0 ] || [ $# -eq 0 ]; then
  echo "usage: $0 <remote:path>... -- <local dir>..." >&2
  exit 2
fi

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

for r in "${remotes[@]}"; do
  rclone lsf -R --files-only --hash MD5 --format h "$r"
done | { grep -E '^[0-9a-f]{32}$' || true; } | sort -u > "$work/remote"
# (|| true: an empty bucket means "everything missing", not an abort; a failing
# rclone still fails the script through pipefail.)

# md5sum -z: NUL-terminated records, no backslash-escaping of odd filenames.
find "$@" -type f -size +0 ! -name Thumbs.db ! -name .DS_Store -print0 |
  xargs -0 -r md5sum -z > "$work/local"

awk 'FNR == NR { have[$0] = 1; next }
     { n++; h = substr($0, 1, 32); p = substr($0, 35)
       if (!(h in have)) { printf "%s\0", p } }
     END { printf "%d", n > "/dev/stderr" }' \
  "$work/remote" RS='\0' "$work/local" > "$work/missing" 2> "$work/count"

checked=$(cat "$work/count")
missing=0 bytes=0
while IFS= read -r -d '' p; do
  s=$(stat -c %s -- "$p")
  printf 'MISSING\t%s\t%s\n' "$s" "$p"
  missing=$((missing + 1)) bytes=$((bytes + s))
done < "$work/missing"

echo "summary: $checked files checked, $missing missing ($bytes bytes)"
[ "$missing" -eq 0 ]
```

- [ ] **Step 2: Run it against PhotosVideos — the known answer**

Run (executor loads the family cluster key into `RCLONE_CONFIG_FAM_*` without printing it — the `RCLONE_CONFIG_B2_*` values from `cluster/family/b2.secret.yaml`, renamed):
```bash
chmod +x docs/backups/verify-local-in-b2.sh
docs/backups/verify-local-in-b2.sh fam:galaxy-family/current fam:galaxy-family/immich/current \
  -- ~/ForMediaServer/ForMediaServer/PhotosVideos
```
Expected **before** the 2026-10-12 03:30 UTC family-backup run: exit 1, exactly three MISSING lines — `00002.MTS` (1305673728), `Sounds.mp4` (27484849), `GOPR0916.MP4` (87019060) — and `summary: 2093 files checked, 3 missing (1420177637 bytes)` (2,097 files minus 4 `Thumbs.db`; PhotosVideos has no zero-byte files). Expected **after** that run: exit 0, `summary: 2093 files checked, 0 missing (0 bytes)`. Those numbers come from the 2026-10-11 hand comparison.

- [ ] **Step 3: Odd-filename case** (Review Focus 5)

```bash
docs/backups/verify-local-in-b2.sh fam:galaxy-family/current -- ~/Videos/"Old Videos" > /tmp/ov.txt; echo "exit=$?"
grep -c $'^MISSING\t' /tmp/ov.txt
grep -F 'videothumbs/0003ecd13408f2060973e724696ae90e#7204.jpg' /tmp/ov.txt
tail -1 /tmp/ov.txt
```
Expected: exit 1; the `#7204.jpg` path prints intact with its directory (`Videos/Old Videos/HCIInfomercial/1638737676940/videothumbs/…`); no `stat: cannot stat` errors; `summary:` line present.

- [ ] **Step 4: Commit**

```bash
git add docs/backups/verify-local-in-b2.sh
nix develop --command git commit -m "backups: verify-local-in-b2.sh — prove local files are mirrored before deleting them"
git push origin main
```

---

### Task 3: `/archive` volume and NFS export

**Files:**
- Create: `cluster/archive/pv.yaml`, `cluster/archive/pvc.yaml`, `cluster/archive/kustomization.yaml`
- Create: `cluster/rook-cluster/archive-export.yaml`
- Modify: `cluster/rook-cluster/kustomization.yaml` (add `archive-export.yaml` after `family-export.yaml`)
- Create: `cluster/applications/archive.yaml`
- Modify: `cluster/applications/kustomization.yaml` (add `archive.yaml` after `family.yaml`)

**Interfaces:**
- Produces: PVC `media/archive` (RWX, bound to PV `archive`, CephFS `/archive`); NFS export `192.168.0.230:/archive`, RW for 192.168.0.21 only.

- [ ] **Step 1: Confirm it doesn't exist yet** (S1)

```bash
export KUBECONFIG=/tmp/galaxy-kubeconfig
kubectl get pv archive; kubectl -n media get pvc archive; kubectl -n rook get cephnfsexports
```
Expected: `NotFound` for both; exports list shows `family` and `satellites-octoprint` only.

- [ ] **Step 2: Write `cluster/archive/pv.yaml`**

```yaml
---
# Static CephFS PV for Chris's personal archive (/archive): cold files such as
# old Minecraft servers, school projects and old videos. Kept apart from
# /family so none of it reaches Immich or the family's shared albums.
# Same shape as cluster/family/pv.yaml — read that file's comments: a plain
# top-level directory in fs-pool, mkdir'd by the ceph-nfs-export operator
# (cluster/rook-cluster/archive-export.yaml); everything under it is
# 1000:1000; never set fsGroup on a pod that mounts it.
apiVersion: v1
kind: PersistentVolume
metadata:
  name: archive
spec:
  accessModes:
    - ReadWriteMany
  capacity:
    storage: 1Ti
  persistentVolumeReclaimPolicy: Retain
  volumeMode: Filesystem
  storageClassName: ""
  csi:
    driver: rook.cephfs.csi.ceph.com
    volumeHandle: archive-static
    nodeStageSecretRef:
      name: rook-csi-cephfs-node
      namespace: rook
    volumeAttributes:
      clusterID: rook
      fsName: fs-pool
      staticVolume: "true"
      rootPath: /archive
      kernelMountOptions: ms_mode=prefer-crc
```

- [ ] **Step 3: Write `cluster/archive/pvc.yaml`**

```yaml
---
# Binds the static PV above (see cluster/family/pvc.yaml).
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: archive
  namespace: media
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: ""
  volumeName: archive
  resources:
    requests:
      storage: 1Ti
```

- [ ] **Step 4: Write `cluster/archive/kustomization.yaml`** (backup pieces join in Task 5)

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: media
resources:
  - pv.yaml
  - pvc.yaml
```

- [ ] **Step 5: Write `cluster/rook-cluster/archive-export.yaml`** and add it to `cluster/rook-cluster/kustomization.yaml`

```yaml
---
# LAN NFS export of the personal archive (CephFS /archive, see
# cluster/archive/). Same rules as family-export.yaml: clients listed one per
# machine, no_root_squash + uid 1000 on the client. Road access is NOT this
# export — laptops read the B2 mirror (docs/backups.md "Browse from a laptop").
#   rowlett: /mnt/archive (automount, ~/nixos hosts/rowlett/configuration.nix)
apiVersion: nfs.luma-homelab.io/v1alpha1
kind: CephNFSExport
metadata:
  name: archive
  namespace: rook
spec:
  cluster: satellites
  fs: fs-pool
  path: /archive
  pseudo: /archive
  squash: no_root_squash
  accessType: NONE
  clients:
    - addresses: [192.168.0.21/32] # rowlett (Chris's workstation)
      accessType: RW
      squash: no_root_squash
```

- [ ] **Step 6: Write `cluster/applications/archive.yaml`** and add it to `cluster/applications/kustomization.yaml`

```yaml
---
# Personal archive: a static CephFS volume (/archive) plus its nightly B2
# mirror (bucket galaxy-archive). In the media namespace next to /family so
# both mirrors share one shape; the LAN NFS export is
# cluster/rook-cluster/archive-export.yaml.
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: archive
  namespace: argo-cd
spec:
  project: default
  source:
    repoURL: https://github.com/ChristopherJMiller/luma-homeops.git
    path: cluster/archive
  destination:
    server: https://kubernetes.default.svc
    namespace: media
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
      allowEmpty: true
    syncOptions:
      - PrunePropagationPolicy=foreground
      - PruneLast=true
```

- [ ] **Step 7: Render, commit, push**

```bash
nix develop --command kustomize build cluster/archive >/dev/null && echo archive-ok
nix develop --command kustomize build cluster/rook-cluster | grep -A3 'name: archive'
git add cluster/archive cluster/rook-cluster/archive-export.yaml cluster/rook-cluster/kustomization.yaml \
        cluster/applications/archive.yaml cluster/applications/kustomization.yaml
nix develop --command git commit -m "archive: static CephFS /archive volume + LAN NFS export (personal cold files)"
git push origin main
```
Expected: `archive-ok`; the rook-cluster render includes the `CephNFSExport` named `archive`; pre-commit passes.

- [ ] **Step 8: Verify live** (Monitor, not a sleep loop, while Argo syncs)

```bash
kubectl -n argo-cd get applications.argoproj.io archive rook-cluster -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status
kubectl -n media get pvc archive
kubectl -n rook get cephnfsexport archive -o jsonpath='{.status}'; echo
```
Expected: `archive` Synced/Healthy; PVC `Bound` to `archive`; export status `{"applied":true,"reconcile":{"cluster":"satellites","path":"/archive","pseudo":"/archive"}}` (same shape the `family` export reports). `rook-cluster` may show Degraded from the chronic SMR warning (#2521) — that's expected.

---

### Task 4: rowlett mount + ingest (with a Ceph guard)

**Files:**
- Modify: `~/nixos/hosts/rowlett/configuration.nix` (the `fileSystems."/mnt/family"` block added 2026-10-11)

**Interfaces:**
- Consumes: export `192.168.0.230:/archive` (Task 3).
- Produces: `/mnt/archive` on rowlett; `/archive` populated in the category layout (Step 6).

- [ ] **Step 1: Replace the single `/mnt/family` entry with a shared helper** (keep the existing comment block, extended)

```nix
  # galaxy's NFS exports (CephFS, served by NFS-Ganesha on the cluster's
  # lan-internal LB IP). Both are mirrored to B2 nightly; see luma-homeops
  # docs/backups.md.
  #   /mnt/family  — family archive: Immich indexes it (06:00 UTC), shared
  #                  with the family as albums. B2: galaxy-family (03:30 UTC).
  #   /mnt/archive — Chris's personal cold files, never in Immich.
  #                  B2: galaxy-archive (04:00 UTC).
  # The exports only admit 192.168.0.21/32 — LAN only. Away from home, read
  # the B2 mirrors instead (common/galaxy-b2-mounts.nix on celebi).
  #  - automount + noauto: nothing blocks boot or login when the cluster is
  #    down; the mount happens on first access and drops after 10 idle min.
  #  - hard (the default) on purpose: a soft mount can silently truncate a
  #    write on timeout, which is the wrong trade for an archive.
  #  - Ceph sits on SMR disks: throttle bulk copies (rsync --bwlimit=8M);
  #    sustained writes past ~25 GiB stall the whole cluster.
  # Everything on both volumes is 1000:1000 and the exports are
  # no_root_squash, so chris (uid 1000) writes with matching ownership.
  fileSystems =
    let
      galaxyNfs = path: {
        device = "192.168.0.230:${path}";
        fsType = "nfs4";
        options = [
          "x-systemd.automount"
          "noauto"
          "x-systemd.idle-timeout=600"
          "x-systemd.mount-timeout=30s"
          "_netdev"
        ];
      };
    in
    {
      "/mnt/family" = galaxyNfs "/family";
      "/mnt/archive" = galaxyNfs "/archive";
    };
```

- [ ] **Step 2: Evaluate, then (Chris) rebuild**

```bash
cd ~/nixos && nix run nixpkgs#nixpkgs-fmt -- --check hosts/rowlett/configuration.nix
nix eval .#nixosConfigurations.rowlett.config.fileSystems --apply 'fs: builtins.attrNames fs' --json
```
Expected: `0 / 1 would have been reformatted`; the list contains `"/"`, `"/boot"`, `"/mnt/archive"`, `"/mnt/family"`.
**(Chris):** `sudo nixos-rebuild switch --flake /home/chris/nixos#rowlett`

- [ ] **Step 3: Verify the mount**

```bash
ls -la /mnt/archive; findmnt /mnt/archive -o SOURCE,FSTYPE,OPTIONS
touch /mnt/archive/.write-test && rm /mnt/archive/.write-test && echo writable
```
Expected: empty dir owned `chris 1000`; source `192.168.0.230:/archive`, `nfs4`, `vers=4.2`, `hard`; `writable`.

- [ ] **Step 4: Arm the Ceph guard** (Monitor tool, before any copy). It pauses rsync after 3 consecutive checks with ≥10 slow ops and resumes when clear.

```bash
export KUBECONFIG=/tmp/galaxy-kubeconfig
TOOLS=$(kubectl -n rook get pod -l app=rook-ceph-tools -o jsonpath='{.items[0].metadata.name}')
bad=0 paused=0
while true; do
  n=$(kubectl -n rook exec "$TOOLS" -- ceph health 2>/dev/null | grep -oE '[0-9]+ slow ops' | grep -oE '^[0-9]+' || echo 0)
  if [ "${n:-0}" -ge 10 ]; then bad=$((bad + 1)); else bad=0; fi
  if [ $bad -ge 3 ] && [ $paused -eq 0 ]; then pkill -STOP -x rsync && paused=1 && echo "PAUSED rsync: $n slow ops"; fi
  if [ $bad -eq 0 ] && [ $paused -eq 1 ]; then pkill -CONT -x rsync && paused=0 && echo "RESUMED rsync"; fi
  pgrep -x rsync >/dev/null || { echo "no rsync running"; }
  sleep 30
done
```
Expected: silent while healthy; a `PAUSED`/`RESUMED` pair if the disks struggle.

- [ ] **Step 5: Map `Old Videos` onto the layout — every entry exactly once** (Review Focus 5). The rest of the sources move whole; `Old Videos` is split by hand-made lists, so check the lists cover it before copying anything.

```bash
cd ~/Videos/"Old Videos"
college=(ConceptReview.mp4 ConceptReviewproject ConceptReviewTranscoded.mp4 DesignReview designreview.mp4
  FinalPresentation FinalPresentationTranscoded.mp4 HCIImplementation HCIInfomercial HCISubmission1
  infomercialtranscoded.m4v junior1-final.mp4 junior1.kdenlive junior1.mp4 juniorprelim megpitch
  pitch.mp4 projectproposal.mp4 projectproposaltranscoded.mp4)
creative=(editordemo.mp4 editordemo.webm himboshortvid.kdenlive himboshortvid.mp4 LearningVFX
  Marioofthedeep MixTakeShotcut MixTakeShotcut.mp4 MixTapeViewer mixtapeviewer.kdenlive mixtapeviewer.mp4
  particlegamedev.mp4 particlegamedev.webm Streaming)
unsorted=(0000-0250.m4v 20131017113401956.pdf DnD ncmpd.webm output.gif plex project.m4v run-plex.sh
  sag.mp4 titles zoom_1.mp4)
family=(00002.MTS 00002.nfo GOPR0916.MP4 GOPR0916.nfo Sounds.mp4 Sounds.nfo) # already in /family
recordings=( [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]\ [0-9][0-9]-[0-9][0-9]-[0-9][0-9].mkv ) # OBS captures
ls -1A | LC_ALL=C sort > /tmp/ov-all
printf '%s\n' "${college[@]}" "${creative[@]}" "${unsorted[@]}" "${family[@]}" "${recordings[@]}" | LC_ALL=C sort > /tmp/ov-mapped
comm -3 /tmp/ov-all /tmp/ov-mapped; echo "recordings=${#recordings[@]}"
```
Expected: `comm` prints nothing (an unmapped entry shows in column 1, a listed name that doesn't exist or is listed twice in column 2) and `recordings=17`. Fix the lists until it's silent. Keep this shell open: Step 6 uses the arrays.

- [ ] **Step 6: Ingest into the category layout at 6 MB/s** (~38 GiB, ~61.5 k files, ~2 h)

```bash
A=/mnt/archive F=~/ForMediaServer/ForMediaServer
R=(rsync -rt --chmod=D2755,F644 --bwlimit=6M --exclude=Thumbs.db --exclude=.DS_Store --info=stats1)
mkdir -p "$A"/{Minecraft,School/TSA,School/College,Projects,Games,Documents,"Music/Sheet Music",Videos/Recordings,Videos/Creative,Videos/Unsorted,Backups/foundry-2024}
"${R[@]}" "$F/OldMCServers/"   "$A/Minecraft/"
"${R[@]}" "$F/OldTSAProjects/" "$A/School/TSA/"
"${R[@]}" "$F/Projects/"       "$A/Projects/"
"${R[@]}" "$F/Games/"          "$A/Games/"
"${R[@]}" "$F/Documents/"      "$A/Documents/"
"${R[@]}" --exclude=/MMB --exclude='/Music Sheets' --exclude=/MID "$F/Music/" "$A/Music/"
"${R[@]}" "$F/Music/MMB" "$F/Music/Music Sheets" "$F/Music/MID" "$A/Music/Sheet Music/"
# Whole, container_cache included: it holds the foundryvtt-11.315 installer, and an
# old Foundry world restores only on its matching version. lost+found is empty.
"${R[@]}" ~/ForMediaServer/foundry-backup/ "$A/Backups/foundry-2024/"
cd ~/Videos/"Old Videos"
"${R[@]}" -- "${college[@]}"    "$A/School/College/"
"${R[@]}" -- "${creative[@]}"   "$A/Videos/Creative/"
"${R[@]}" -- "${unsorted[@]}"   "$A/Videos/Unsorted/"
"${R[@]}" -- "${recordings[@]}" "$A/Videos/Recordings/"
ls "$A" "$A/School" "$A/Videos" "$A/Music"
```
Expected: no `rsync error`; top level `Backups Documents Games Minecraft Music Projects School Videos`; `School` → `College TSA`; `Videos` → `Creative Recordings Unsorted`; `Music` holds the album folders plus `Sheet Music`. **Not copied, on purpose:** `~/ForMediaServer/home-assistant` (the 2024 HA backup; Chris chose to skip it after reviewing its four automations; see "Decisions") and the six `family` entries.

- [ ] **Step 7: Verify from inside the cluster, by content** — read what Ceph stored, not rowlett's page cache. Paths changed in the reorganisation, so compare the **multiset of file hashes**: identical lists prove every byte arrived, wherever it landed, and that nothing was dropped or duplicated. One-shot Pod mounting PVC `media/archive` read-only, running as 1000:

```bash
kubectl -n media run archive-verify --rm -i --restart=Never \
  --image=ghcr.io/rclone/rclone:1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5 \
  --overrides='{"spec":{"securityContext":{"runAsUser":1000,"runAsGroup":1000,"runAsNonRoot":true},
    "containers":[{"name":"v","image":"ghcr.io/rclone/rclone:1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5",
      "command":["sh","-c","cd /archive && find . -type f -exec md5sum {} +"],
      "volumeMounts":[{"name":"a","mountPath":"/archive","readOnly":true}]}],
    "volumes":[{"name":"a","persistentVolumeClaim":{"claimName":"archive","readOnly":true}}]}}' \
  | cut -c1-32 | LC_ALL=C sort > /tmp/archive-incluster.hashes
( F=~/ForMediaServer/ForMediaServer
  find "$F"/{OldMCServers,OldTSAProjects,Projects,Games,Documents,Music} ~/ForMediaServer/foundry-backup \
    -type f ! -name Thumbs.db ! -name .DS_Store -exec md5sum {} + ;
  cd ~/Videos/"Old Videos" && find . -type f ! -name Thumbs.db ! -name .DS_Store \
    ! -name 00002.MTS ! -name 00002.nfo ! -name GOPR0916.MP4 ! -name GOPR0916.nfo ! -name Sounds.mp4 ! -name Sounds.nfo \
    -exec md5sum {} + ) | cut -c1-32 | LC_ALL=C sort > /tmp/archive-local.hashes
wc -l /tmp/archive-local.hashes /tmp/archive-incluster.hashes
diff /tmp/archive-local.hashes /tmp/archive-incluster.hashes && echo IDENTICAL
```
Expected: `IDENTICAL`, equal line counts (~61.5 k: the ~64 k source files minus `home-assistant`'s 2,220 and the excluded names). Ceph no worse than before the ingest (`ceph pg stat` all `active+clean`).

No commit for the ingest itself; commit the `~/nixos` change only if Chris asks.

---

### Task 5: nightly B2 mirror, canary, alerts, docs

**Files:**
- Create: `cluster/archive/b2.secret.yaml` (git-crypt), then generated `cluster/archive/b2.yaml` (SealedSecret)
- Create: `cluster/archive/backup-cronjob.yaml`, `cluster/archive/canary-cronjob.yaml`
- Modify: `cluster/archive/kustomization.yaml`
- Modify: `cluster/backups/prometheusrule.yaml` (three regexes)
- Modify: `docs/backups.md`, `CLAUDE.md` (Storage row)

**Interfaces:**
- Consumes: key `galaxy-archive-cluster` (Task 1); populated `/archive` (Task 4 — the MIN_FILES gate needs it).
- Produces: CronJob `media/archive-backup` (04:00 UTC) → `b2:galaxy-archive/current`, `deleted/<date>/`; CronJob `media/archive-canary` (06:45 UTC on the 1st); both covered by the backup alerts.

- [ ] **Step 1: (Chris) write the secret, then seal it.** Fill the two key fields from the password manager; never paste them into chat.

`cluster/archive/b2.secret.yaml`:
```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: archive-b2
  namespace: media
stringData:
  RCLONE_CONFIG_B2_TYPE: s3
  RCLONE_CONFIG_B2_PROVIDER: Other
  RCLONE_CONFIG_B2_ENDPOINT: https://s3.us-west-004.backblazeb2.com
  RCLONE_CONFIG_B2_REGION: us-west-004
  RCLONE_CONFIG_B2_NO_CHECK_BUCKET: "true"
  RCLONE_CONFIG_B2_ACCESS_KEY_ID: "keyID of galaxy-archive-cluster"
  RCLONE_CONFIG_B2_SECRET_ACCESS_KEY: "applicationKey of galaxy-archive-cluster"
```
```bash
nix develop --command ./sign.sh
ls -l cluster/archive/b2.yaml && grep -c 'kind: SealedSecret' cluster/archive/b2.yaml
```
Expected: `b2.yaml` exists, non-empty, contains `kind: SealedSecret` once.

- [ ] **Step 2: Write `cluster/archive/backup-cronjob.yaml`** — family-backup with archive names, `--links` (none today, but a future symlink must not silently vanish from the mirror), 04:00 UTC (between family 03:30 and immich 04:30).

```yaml
---
# Nightly mirror of /archive to B2 (bucket galaxy-archive). Same design as
# cluster/family/backup-cronjob.yaml (read its comments): files-as-files,
# current/ = exact mirror, deleted/<date>/ = anything removed or overwritten,
# plus 90 days of hidden versions; the key cannot delete. Laptops browse
# current/ read-only (docs/backups.md "Browse from a laptop").
apiVersion: batch/v1
kind: CronJob
metadata:
  name: archive-backup
  namespace: media
  labels:
    app.kubernetes.io/name: archive-backup
    backup-tier: archive
spec:
  schedule: "0 4 * * *"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  startingDeadlineSeconds: 3600
  jobTemplate:
    spec:
      backoffLimit: 2
      activeDeadlineSeconds: 72000 # 20h; sync is incremental, so a killed run resumes next night
      template:
        metadata:
          labels:
            app.kubernetes.io/name: archive-backup
        spec:
          restartPolicy: Never
          securityContext:
            runAsUser: 1000
            runAsGroup: 1000
            runAsNonRoot: true
            seccompProfile:
              type: RuntimeDefault
          containers:
            - name: rclone
              image: ghcr.io/rclone/rclone:1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5
              securityContext:
                allowPrivilegeEscalation: false
                capabilities:
                  drop: [ALL]
              envFrom:
                - secretRef:
                    name: archive-b2
              env:
                - name: MIN_FILES
                  value: "100"
              command: ["/bin/sh"]
              args:
                - -c
                - |
                  set -eu
                  SRC=/archive
                  DST=b2:galaxy-archive

                  # Sanity gate: an empty or barely-populated source means the
                  # volume failed to mount or was wiped — never let that become
                  # "move everything in the bucket to deleted/".
                  n=$(find "$SRC" -type f | head -n $((MIN_FILES + 1)) | wc -l)
                  if [ "$n" -le "$MIN_FILES" ]; then
                    echo "refusing to sync: only $n files under $SRC (min $MIN_FILES)" >&2
                    exit 2
                  fi

                  echo "archive-backup start $(date -u +%FT%TZ): $n+ files"
                  rclone sync "$SRC" "$DST/current" \
                    --backup-dir "$DST/deleted/$(date -u +%F)" \
                    --links \
                    --exclude '.DS_Store' --exclude 'Thumbs.db' --exclude '._*' --exclude '*.partial' \
                    --transfers 4 --checkers 8 \
                    --s3-chunk-size 16M --s3-upload-concurrency 4 --buffer-size 16M \
                    --bwlimit 10M \
                    --fast-list --stats 5m --stats-one-line --stats-log-level NOTICE \
                    --log-level INFO
                  echo "archive-backup done $(date -u +%FT%TZ)"
              volumeMounts:
                - name: archive
                  mountPath: /archive
                  readOnly: true
              resources:
                requests:
                  cpu: 200m
                  memory: 256Mi
                limits:
                  cpu: "2"
                  memory: 1Gi
          volumes:
            - name: archive
              persistentVolumeClaim:
                claimName: archive
                readOnly: true
```
(`MIN_FILES` is an env var rather than a literal so Step 5 can exercise the gate.)

- [ ] **Step 3: Write `cluster/archive/canary-cronjob.yaml`** — monthly read-back of one known file. MD5 recorded from rowlett 2026-10-11.

```yaml
---
# Monthly restore check for the archive mirror: fetch one known file straight
# from B2 and compare its md5 to the value recorded from rowlett's copy before
# ingest (2026-10-11). Proves the bucket is readable with the cluster key and
# the bytes are intact. Same pattern as cluster/family/canary-cronjob.yaml.
apiVersion: batch/v1
kind: CronJob
metadata:
  name: archive-canary
  namespace: media
  labels:
    app.kubernetes.io/name: archive-backup
    backup-tier: archive
spec:
  schedule: "45 6 1 * *"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  startingDeadlineSeconds: 3600
  jobTemplate:
    spec:
      backoffLimit: 1
      activeDeadlineSeconds: 900
      template:
        metadata:
          labels:
            app.kubernetes.io/name: archive-backup
        spec:
          restartPolicy: Never
          securityContext:
            runAsUser: 1000
            runAsGroup: 1000
            runAsNonRoot: true
            seccompProfile:
              type: RuntimeDefault
          containers:
            - name: rclone
              image: ghcr.io/rclone/rclone:1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5
              securityContext:
                allowPrivilegeEscalation: false
                capabilities:
                  drop: [ALL]
              envFrom:
                - secretRef:
                    name: archive-b2
              command: ["/bin/sh"]
              args:
                - -c
                - |
                  set -eu
                  f='current/School/TSA/EngineeringDesign/20160217_203318.jpg'
                  want=b82ce6e6947f5294d10bb4da189bbe22
                  got=$(rclone cat "b2:galaxy-archive/$f" | md5sum | cut -d' ' -f1)
                  if [ "$got" = "$want" ]; then echo "archive canary OK: $f md5=$got"; else echo "archive canary FAILED: $f md5=$got want=$want" >&2; exit 1; fi
              resources:
                requests:
                  cpu: 50m
                  memory: 64Mi
                limits:
                  cpu: 500m
                  memory: 256Mi
```

- [ ] **Step 4: Wire it up — kustomization, alert regexes**

`cluster/archive/kustomization.yaml` resources become:
```yaml
resources:
  - pv.yaml
  - pvc.yaml
  - b2.yaml
  - backup-cronjob.yaml
  - canary-cronjob.yaml
```
In `cluster/backups/prometheusrule.yaml`, add `|archive-backup` after `family-backup` in the `BackupCronJobStale` and `BackupCronJobNeverSucceeded` regexes, and `|archive-backup|archive-canary` after `family-canary` in `BackupJobFailed`:
```
cronjob=~"restic-(home-assistant|plex|gonic|satellites)|family-backup|archive-backup|immich-backup|logical-backup-.*"
job_name=~"(restic-.*|family-backup|family-canary|archive-backup|archive-canary|immich-backup|logical-backup-.*)-[0-9]+"
```

- [ ] **Step 5: Commit, push, and prove the gate before the first real run** (Review Focus 1)

```bash
nix develop --command kustomize build cluster/archive >/dev/null && echo ok
git add cluster/archive cluster/backups/prometheusrule.yaml
nix develop --command git commit -m "archive: nightly B2 mirror (galaxy-archive), monthly canary, backup alerts"
git push origin main
```
After Argo syncs (Monitor on `kubectl -n media get cronjob archive-backup`), run a gate test whose `MIN_FILES` exceeds the real file count (~61.5 k):
```bash
kubectl -n media create job archive-gate-test --from=cronjob/archive-backup --dry-run=client -o json \
  | jq '.spec.template.spec.containers[0].env = [{"name":"MIN_FILES","value":"10000000"}] | .spec.backoffLimit = 0' \
  | kubectl apply -f -
kubectl -n media wait --for=condition=Failed job/archive-gate-test --timeout=10m
kubectl -n media logs job/archive-gate-test
kubectl -n media delete job archive-gate-test
```
Expected: the log line `refusing to sync: only N files under /archive (min 10000000)`, where N is the real file count (~61,500, from the Task 4 ingest); job `Failed` after one attempt (exit 2); the bucket is still empty (`rclone lsf b2:galaxy-archive` with the archive key prints nothing). The job name doesn't match the alert regexes' `-[0-9]+` suffix, so this test doesn't page.

- [ ] **Step 6: First real run, by hand, with the Ceph guard from Task 4 Step 4 still armed** (the mirror only reads Ceph, but it's ~38 GiB of reads)

```bash
kubectl -n media create job archive-backup-first --from=cronjob/archive-backup
```
Watch with a Monitor on `kubectl -n media get job archive-backup-first` until `Complete` or `Failed` (≈1.5 h at 10 MB/s). Then:
```bash
kubectl -n media logs job/archive-backup-first | tail -5
docs/backups/verify-local-in-b2.sh arc:galaxy-archive/current fam:galaxy-family/current -- \
  ~/ForMediaServer/ForMediaServer/{OldMCServers,OldTSAProjects,Projects,Games,Documents,Music} \
  ~/ForMediaServer/foundry-backup ~/Videos/"Old Videos"
```
(`home-assistant` is left out on purpose: it is not archived. The family bucket is in the list because the three family videos in `Old Videos` live there, not in the archive. `arc:` = the `galaxy-archive-cluster` key, `fam:` = the family cluster key, both loaded as env without printing.)
Expected: job `Complete`, log ends `archive-backup done …`; verifier exit 0, `0 missing`. Then fire the canary once: `kubectl -n media create job archive-canary-first --from=cronjob/archive-canary` → logs `archive canary OK`. Delete both one-off jobs afterwards so they don't sit in history.

- [ ] **Step 7: Docs**

`docs/backups.md`:
- Tier table, after the Family media row:
  `| Personal archive | CephFS /archive (PVC media/archive) | rclone mirror, files-as-files | galaxy-archive/current/…, deletions → galaxy-archive/deleted/<date>/… | nightly 04:00 |`
- "How the buckets are made safe" lifecycle table: `| galaxy-archive (all) | 90 days | same as galaxy-family |`
- Cold-start list: add `galaxy-archive-laptop-ro` / `galaxy-family-laptop-ro` (read-only; laptop browsing) and, per Task 1 Step 4, whether `galaxy-operator-laptop` covers `galaxy-archive`.
- New section `## Browse from a laptop` — the B2 mirrors are mounted read-only by `~/nixos` `common/galaxy-b2-mounts.nix`; rclone.conf remotes `b2family-ro` / `b2archive-ro`; mirrors trail the cluster by ≤1 day; downloads free to 3× stored/month.
- New section `## Before deleting a local copy` — `docs/backups/verify-local-in-b2.sh` usage and its zero-byte/Thumbs.db skips.
- Restore log at the bottom: a row for the archive's first full mirror + canary.

`CLAUDE.md` Storage row: change "CephNFS export at `192.168.0.230` for satellites." to "CephNFS exports at `192.168.0.230`: satellites, plus `/family` and `/archive` to rowlett (both mirrored to B2 nightly; laptops browse the B2 mirrors read-only)."

```bash
git add docs/backups.md CLAUDE.md
nix develop --command git commit -m "docs: personal archive tier, laptop browsing, verify-before-delete"
git push origin main
```

---

### Task 6: celebi — read-only B2 mounts

**Files (in `~/nixos`):**
- Create: `common/galaxy-b2-mounts.nix`
- Modify: `hosts/celebi/home.nix` (import beside `webdav-sync`, add two `systemd.user.services` beside the webdav ones)
- Installed on celebi, never in `~/nixos` (public) or the Nix store: `~/.config/rclone/galaxy-b2-ro.conf` (chmod 600), copied from luma-homeops `laptops/celebi/galaxy-b2-ro.secret.conf` (git-crypt; ruled 2026-10-12)

**Interfaces:**
- Consumes: keys `galaxy-family-laptop-ro`, `galaxy-archive-laptop-ro` (Task 1); `b2:galaxy-archive/current` populated (Task 5).
- Produces: user services `galaxy-family` → `~/Galaxy/family`, `galaxy-archive` → `~/Galaxy/archive`.

- [ ] **Step 1: (Chris, on celebi) measure disk** — the two caches share it

```bash
df -h ~
```
Use `--vfs-cache-max-size 20G` per mount when free space ≥ 200 GiB; otherwise 10 % of free space split between the two. Put the chosen number in Step 2.

- [ ] **Step 2: Write `common/galaxy-b2-mounts.nix`** (the same "module returns units, home.nix wires them" idiom as `common/webdav-sync.nix`)

```nix
{ pkgs, ... }:

# Read-only rclone mounts of galaxy's B2 mirrors, for browsing the family
# archive and the personal archive from anywhere (luma-homeops
# docs/backups.md "Browse from a laptop"). Reads come from Backblaze, not the
# home uplink, and are free up to 3x stored data per month. The mirrors trail
# the cluster by up to a day (nightly sync); at home, rowlett uses NFS instead.
#
# Credentials: hand-written remotes `b2family-ro` and `b2archive-ro` in
# ~/.config/rclone/rclone.conf (chmod 600) from the read-only keys
# galaxy-family-laptop-ro / galaxy-archive-laptop-ro in the password manager —
# never in this repo or the Nix store. Read-only keys: a lost laptop can read,
# never change or delete.
let
  mount = { description, remote, dir }: {
    Unit.Description = description;
    Service = {
      Type = "notify";
      ExecStartPre = "${pkgs.coreutils}/bin/mkdir -p ${dir}";
      ExecStart = builtins.concatStringsSep " " [
        "${pkgs.rclone}/bin/rclone mount ${remote} ${dir}"
        "--config %h/.config/rclone/rclone.conf"
        "--read-only"
        # Full cache: a file opened once is served locally afterwards, which
        # is what makes Dolphin usable on hotel wifi.
        "--vfs-cache-mode full"
        "--vfs-cache-max-size 20G"
        "--vfs-cache-max-age 720h"
        # The mirrors change at most nightly; S3 has no change polling.
        "--dir-cache-time 12h"
        "--poll-interval 0"
        # Offline: fail a read in about a minute instead of hanging the caller.
        "--timeout 1m"
        "--contimeout 15s"
        "--low-level-retries 3"
      ];
      ExecStop = "/run/wrappers/bin/fusermount3 -u ${dir}";
      Environment = [ "PATH=/run/wrappers/bin" ]; # rclone finds the setuid fusermount3 here
      Restart = "on-failure";
      RestartSec = "30s";
    };
    Install.WantedBy = [ "default.target" ];
  };
in
{
  family = mount {
    description = "galaxy family archive (B2 mirror, read-only)";
    remote = "b2family-ro:galaxy-family/current";
    dir = "%h/Galaxy/family";
  };
  archive = mount {
    description = "galaxy personal archive (B2 mirror, read-only)";
    remote = "b2archive-ro:galaxy-archive/current";
    dir = "%h/Galaxy/archive";
  };
}
```

- [ ] **Step 3: Wire into `hosts/celebi/home.nix`**

In the `let` block, after `webdav-sync = import ../../common/webdav-sync.nix { inherit pkgs; };`:
```nix
  galaxy-b2 = import ../../common/galaxy-b2-mounts.nix { inherit pkgs; };
```
After `systemd.user.services.webdav-watch = webdav-sync.watchService;`:
```nix

  # Read-only browsing of galaxy's B2 mirrors (family + personal archive).
  systemd.user.services.galaxy-family = galaxy-b2.family;
  systemd.user.services.galaxy-archive = galaxy-b2.archive;
```
Evaluate from rowlett:
```bash
cd ~/nixos && nix run nixpkgs#nixpkgs-fmt -- --check common/galaxy-b2-mounts.nix hosts/celebi/home.nix
nix eval .#nixosConfigurations.celebi.config.home-manager.users.chris.systemd.user.services.galaxy-family.Service.ExecStart
```
Expected: no reformat needed; the ExecStart string contains `rclone mount b2family-ro:galaxy-family/current %h/Galaxy/family` and `--read-only`.

- [ ] **Step 4: (Chris, on celebi) install the config + rebuild**

The remotes are already written (Task 1) and stored git-crypt encrypted at luma-homeops `laptops/celebi/galaxy-b2-ro.secret.conf`. Copy the decrypted file from rowlett (where the repo is unlocked) into its own file on celebi; celebi never gets the git-crypt key:
```bash
scp rowlett:Repos/luma-homeops/laptops/celebi/galaxy-b2-ro.secret.conf /tmp/g.conf \
  && install -m600 /tmp/g.conf ~/.config/rclone/galaxy-b2-ro.conf && rm /tmp/g.conf
```
```bash
cd ~/nixos && git pull && sudo nixos-rebuild switch --flake .#celebi
systemctl --user start galaxy-family galaxy-archive   # home-manager may only suggest starting new units
systemctl --user status galaxy-family galaxy-archive --no-pager | grep -E 'Active:|galaxy'
```
Expected: both `active (running)`.

- [ ] **Step 5: Browse + read-only proofs** (Review Focus 3)

```bash
ls ~/Galaxy/family ~/Galaxy/archive
sha1sum ~/Galaxy/family/OldPictures/DSC00024.JPG
md5sum ~/Galaxy/archive/School/TSA/EngineeringDesign/20160217_203318.jpg
touch ~/Galaxy/family/x 2>&1
rclone --config ~/.config/rclone/galaxy-b2-ro.conf touch b2family-ro:galaxy-family/current/ro-test 2>&1 | tail -1
rclone --config ~/.config/rclone/galaxy-b2-ro.conf touch b2archive-ro:galaxy-archive/current/ro-test 2>&1 | tail -1
```
Expected: family lists its 13 folders + `WhistlerTuskPanorama.png`; archive lists `Backups Documents Games Minecraft Music Projects School Videos`; sha1 `24a94c2430e5a594731df147440b708b370b106b` (the family canary's value); md5 `b82ce6e6947f5294d10bb4da189bbe22`; `touch` → `Read-only file system`; both rclone writes fail with an access-denied / unauthorized error from B2 (the key lacks `writeFiles`), **not** success.

- [ ] **Step 6: Offline behaviour** (Review Focus 2)

```bash
ls ~/Galaxy/family/OldPictures >/dev/null && echo cached-listing-ok   # warm the dir cache first
nmcli networking off
time ls ~/Galaxy/family/OldPictures | head -3
time head -c 1M ~/Galaxy/family/HomeDV/"10-02 - 12-25-02.mp4" > /dev/null; echo "exit=$?"
nmcli networking on
sleep 60; systemctl --user is-active galaxy-family galaxy-archive
```
Expected: cached listing returns instantly offline; the uncached read **fails** (non-zero exit, I/O error) within ~2 min rather than hanging; after reconnecting both services are `active` without a manual restart.

- [ ] **Step 7: Cache cap** (Review Focus 4)

```bash
du -sh ~/.cache/rclone/vfs/b2family-ro ~/.cache/rclone/vfs/b2archive-ro 2>/dev/null
```
Expected: each ≤ the Step 1 cap (`--vfs-cache-max-size` evicts oldest files beyond it).

- [ ] **Step 8: Commit in `~/nixos`** (only with Chris's go-ahead — that repo's commits are his call)

```bash
cd ~/nixos && git add common/galaxy-b2-mounts.nix hosts/celebi/home.nix hosts/rowlett/configuration.nix
git commit -m "galaxy: NFS automounts on rowlett, read-only B2 mirror mounts on celebi"
```

---

### Task 7: final verification and the local-delete handoff

**Files:** none.

**Interfaces:**
- Consumes: `verify-local-in-b2.sh` (Task 2); both buckets current (Tasks 5, plus the family nightly run).

- [ ] **Step 1: Everything Chris wants to delete, against both buckets**

```bash
docs/backups/verify-local-in-b2.sh fam:galaxy-family/current fam:galaxy-family/immich/current arc:galaxy-archive/current \
  -- ~/ForMediaServer/ForMediaServer ~/ForMediaServer/foundry-backup ~/Videos/"Old Videos"
```
Expected: exit 0, `0 missing`. Any MISSING line blocks Step 3 for that file: find out why (excluded name? arrived after the last nightly run?) and fix it before going on.

**Deliberately not in B2, and still deleted in Step 3:** `~/ForMediaServer/home-assistant` (the 2024 HA backup: Chris chose to skip it; its one idea worth keeping, a phone-alarm wake-up light, is recorded under "Not in this plan"), plus `Thumbs.db`/`.DS_Store` files and zero-byte files, which the verifier skips. Chris confirms this list at Step 3.

- [ ] **Step 2: Restore drill from the archive bucket** — stream 6 files back out of B2 and re-hash (no disk writes), the same drill run on the family bucket 2026-10-11: the two largest files plus 4 random ones; every line `OK`.

- [ ] **Step 3: (Chris) delete the local copies** — only after Steps 1–2 pass:

```bash
rm -r ~/ForMediaServer ~/Videos/"Old Videos"
df -h /
```
Expected: ~70 GiB freed on rowlett's root filesystem (58 GiB `~/ForMediaServer` + 12 GiB `Old Videos`).

---

## Decisions (settled at plan review, 2026-10-11)

1. **`Music/`:** archive only, as `/archive/Music` (sheet music and the MIDI under `Music/Sheet Music/`). Whether gonic should also play it is for Chris to look into later; `/archive/Music` can be mounted read-only into gonic as a second music path without copying anything.
2. **Layout:** reorganised by category during the copy (Task 4 Steps 5–6). Ambiguous `Old Videos` items go to `Videos/Unsorted/`.
3. **Old HA backup (`~/ForMediaServer/home-assistant`):** not archived. Of its four automations, only the phone-alarm wake-up light is worth keeping as an idea (see below); the other three paired bedroom lights, which room groups now cover.
4. **`foundry-backup`:** archived whole as `Backups/foundry-2024/`, `container_cache` included (the v11.315 installer the old world needs).
5. **Laptop cache size:** from celebi's measured free space (Task 6 Step 1).
6. **Execution:** native, in this session, with one whole-branch review at the end.

## Not in this plan

- Filebrowser (`share.chrismiller.xyz`) access to `/archive` — a one-volume follow-up.
- Minecraft cold-start project over `/archive/Minecraft`.
- A self-hosted game library over `/archive/Games` (e.g. Drop; RomM for emulation), mounted read-only the way Immich mounts `/family`.
- gonic playing `/archive/Music` (see Decisions 1).
- **HA idea to port** (via the ha-config skill, not as-is): the 2024 `[Bedroom] [WUR] Morning Light Wake Up` automation. When `sensor.minutes_until_next_alarm` (from the phone's next-alarm sensor) drops below 1 and Chris is home with the bedroom off, it sets a dimmed bedroom scene with circadian lighting paused and turns on `input_boolean.bedroom_waking_up_mode`. Turning on the main light ends the mode and resumes circadian. Today's equivalent would pause or resume `adaptive_lighting` instead of the old `circadian_lighting` switch.
- Tailscale NFS for laptops — superseded by the B2 mounts.
- **Flagged for separate review:** `mediadav.chrismiller.xyz` (Argo app `media-webdav`) is a public WebDAV with htpasswd basic auth over `media/mm-media` — the public-password-endpoint pattern this design avoids.
