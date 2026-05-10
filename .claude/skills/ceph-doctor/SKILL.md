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
- `ceph -s` shows `pgs: ... incomplete` or `peering` → quorum / peering issue, not a data integrity issue. Different playbook.
- Ceph capacity > 80% RAW USED → space crunch is its own emergency; recommend reaping unused PVs, growing storage, or purging snapshots before any other work.

## Useful reads (for the model)

- `ceph -s` "data" line: PG state distribution. `active+clean+inconsistent` means data is reachable but failing scrub.
- A PG repair triggers an immediate deep scrub with the repair flag. On HDDs at homelab scale, expect 30-90 minutes per PG including the post-repair verify scrub.
- `osd_scrub_sleep`, `osd_max_scrubs`, `osd_scrub_load_threshold` control how aggressive scrubs are. Don't tune mid-repair.
