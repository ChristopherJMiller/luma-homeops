---
name: ceph-doctor
description: Read-only Ceph inspection workflow when the cluster is showing degraded/error/warn state. Identifies inconsistent PGs, bad shards, OSD trouble, capacity issues — and proposes safe repair commands. Use when ceph -s is not HEALTH_OK, when the user mentions ceph/rook/storage problems, or before doing anything that depends on storage being healthy. NEVER runs `pg repair`, `osd out`, `osd destroy`, or any write command without explicit user approval.
---

# ceph-doctor

Diagnose Ceph problems on galaxy without changing state. The default outcome is a written assessment + a proposed repair plan, **not** a repair.

## Ground rules (CLAUDE.md S7)

- All commands here are read-only. No exceptions.
- `ceph pg repair`, `ceph osd out/down/destroy/reweight`, scrub-schedule changes, and pool config changes require **explicit user approval per command** before running. Show the command, explain the effect, ask, wait.
- Inconsistent PGs do not auto-fix. Slow is correct.

## Refresh kubeconfig if needed

```bash
nix-shell shell.nix --run 'TALOSCONFIG=$PWD/nodes/talosconfig \
  talosctl -n 192.168.0.5 -e 192.168.0.5 kubeconfig --force /tmp/galaxy-kubeconfig'
export KUBECONFIG=/tmp/galaxy-kubeconfig
```

## Find the toolbox pod

Every ceph command goes through this pod:

```bash
TOOLS=$(kubectl -n rook get pod -l app=rook-ceph-tools -o jsonpath='{.items[0].metadata.name}')
ceph() { kubectl -n rook exec "$TOOLS" -- ceph "$@"; }
rados() { kubectl -n rook exec "$TOOLS" -- rados "$@"; }
rbd() { kubectl -n rook exec "$TOOLS" -- rbd "$@"; }
```

## Inspect (in this order)

0. **Is it actually a data problem?** Before anything else, separate *advisory* health from *data-path* health. `HEALTH_ERR` with `113 pgs active+clean, 6 osds up/in` is not an outage.

   ```bash
   ceph pg stat; ceph osd stat        # if PGs are all active+clean and OSDs all up/in, the data path is fine
   ceph health detail | grep -E '^\[(ERR|WRN)\]'
   ```

   Known advisory codes on galaxy (2026-09) and what they really are:

   | Code | Meaning | Urgency |
   |---|---|---|
   | `AUTH_INSECURE_*` (6 codes, 2 of them ERR) | Ceph ≥19.2.6 flags cephx keys using the legacy `aes` type ([CVE-2025-30156](https://docs.ceph.com/en/latest/security/CVE-2025-30156/)). **Phase 1 done 2026-09-20**: daemons + admin on `aes256k` via `spec.security.cephx` in `cluster/rook-cluster/cluster.yaml` (Rook automates; toolbox pod needs a restart afterwards). CSI + NFS keys stay `aes` until the node kernel is ≥7.0 (#2660); the three residual warnings are muted in the CR on purpose. If `AUTH_INSECURE_SERVICE_*` ever returns as ERR, someone bumped `allowedCiphers` or a daemon key regressed — re-read #2660 before touching anything. | Days, not minutes |
   | `DAEMON_OLD_VERSION` | Some daemons lag the image tag (2026-09-20: 2 MDS on 19.2.5 while the rest is 19.2.6). Rook rolls MDS last and can leave them; a `kubectl rollout restart deploy/rook-ceph-mds-*` fixes it, which is safe *only* with `ceph fs status` showing the standby MDS active. | Low |
   | `RECENT_CRASH` | Crashes are listed until archived — galaxy's are from 2025 (`ceph crash ls-new`). `ceph crash archive-all` is a read-modify of the crash log, not the data path. | Cosmetic |
   | `BLUESTORE_SLOW_OP_ALERT` | SMR HDDs; chronic, see CLAUDE.md sharp edge #1. | Noise |

   `RECENT_CRASH` within the last 24 h, or any code not in this table, is not advisory — keep going down the list.


1. **Top-level state**

   ```bash
   ceph -s
   ceph health detail
   ```

2. **If `PG_DAMAGED` / `OSD_SCRUB_ERRORS` / inconsistent PGs**

   For each inconsistent PG:
   ```bash
   ceph pg <pgid> query | jq '{state, last_deep_scrub_stamp, scrubber, last_scrub_stamp, num_scrub_errors: .info.stats.stat_sum.num_scrub_errors}'
   rados list-inconsistent-obj <pgid> --format=json | jq '.inconsistents[] | {object: .object.name, errors, bad_shards: [.shards[] | select(.errors|length>0) | {osd, errors}]}'
   ```

   For each bad object: identify which shard's digest disagrees with the others. The shard with `data_digest_mismatch_info` is the bad one.

3. **OSD layout**

   ```bash
   ceph osd tree
   ceph osd df tree
   ```

   Note: which host hosts which OSDs. Bit-rot pattern (bad shards on different hosts) → individual disk media issue. Concentrated pattern (multiple bad OSDs on the same host) → controller / cable / PSU on that node.

4. **Per-OSD trouble signs**

   ```bash
   ceph osd perf
   ceph health detail | grep -i 'spurious\|slow\|crashed'
   ```

   `BLUESTORE_SPURIOUS_READ_ERRORS` is a transient warn — note but don't react.

5. **Pool / RBD identification**

   When an inconsistency hits an RBD object (`rbd_data.<image_id>.<offset>`), find the owning image and PVC:

   ```bash
   for img in $(rbd ls -p block-pool); do
     rbd info -p block-pool "$img" 2>/dev/null | grep -q '<image_id>' && echo "MATCH: $img" && break
   done

   kubectl get pv -o json | jq -r '.items[] | select(.spec.csi.volumeAttributes.imageName == "<csi-vol-name>") | {pv: .metadata.name, claim: (.spec.claimRef.namespace + "/" + .spec.claimRef.name), capacity: .spec.capacity.storage}'
   ```

## Decide repair-safety

For each inconsistent PG:

- **2-of-3 majority of clean shards + primary is clean** → `ceph pg repair <pgid>` is safe. Repair will overwrite the single bad shard from the primary.
- **2-of-3 majority of clean shards + primary is the bad shard** → still recoverable, but `pg repair` could propagate the bad data. Need to flag and discuss before any repair. Modern Ceph picks the shard whose digest matches `selected_object_info.data_digest`, so it's usually still safe — but explicitly verify by reading `.shards[*].data_digest` against `.selected_object_info.data_digest`.
- **No clean majority (≤1 clean shard)** → don't `pg repair`. Stop and discuss with user. Possible recovery via object-store-tool, possible data loss.

## Propose, don't execute

In your response to the user:

1. Summarize what you found in 5-7 lines (state, count of damaged PGs, root pattern, affected workload).
2. Propose specific commands, one per line, with the consequence of each.
3. Ask which to run.

Never proactively run a `repair`/`out`/`destroy` even if confident.

## When to escalate

- Multiple OSDs on the same host with `BLUESTORE_SPURIOUS_READ_ERRORS` and growing → likely failing controller/cable/PSU. Recommend SMART check + dmesg inspection on that node, not Ceph commands.
- `HEALTH_ERR` that is *entirely* `AUTH_INSECURE_*` codes with clean PGs → not an incident; propose the key-rotation session and move on. Don't let the ERR badge (or Argo's Degraded rollup, #2521) stall unrelated work like a Talos hop — S5's "Ceph healthy between hops" means PGs active+clean, not HEALTH_OK.
- `ceph -s` shows `pgs: ... incomplete` or `peering` → quorum / peering issue, not a data integrity issue. Different playbook.
- Ceph capacity > 80% RAW USED → space crunch is its own emergency; recommend reaping unused PVs, growing storage, or purging snapshots before any other work.

## Useful reads (for the model)

- `ceph -s` "data" line: PG state distribution. `active+clean+inconsistent` means data is reachable but failing scrub.
- A PG repair triggers an immediate deep scrub with the repair flag. On HDDs at homelab scale, expect 30-90 minutes per PG including the post-repair verify scrub.
- `osd_scrub_sleep`, `osd_max_scrubs`, `osd_scrub_load_threshold` control how aggressive scrubs are. Don't tune mid-repair.
