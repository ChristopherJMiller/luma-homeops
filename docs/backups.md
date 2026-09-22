# Backups — what is protected, how, and how to get it back

Three tiers, three tools, one Backblaze B2 account. Restore procedures are
below and **each has been executed at least once** (dates at the bottom).
Every off-site copy is readable from a laptop with nothing from the cluster.

| Tier | What | Tool | Where in B2 | Schedule (UTC) |
|---|---|---|---|---|
| Family media | CephFS `/family` (PVC `media/family-media`) | rclone mirror, files-as-files | `galaxy-family/current/…`, deletions → `galaxy-family/deleted/<date>/…` | nightly 03:30 |
| Postgres | all six `postgresql` clusters | postgres-operator logical backup (`pg_dumpall \| gzip`) | `galaxy-cluster-backups/spilo/<cluster>/<uid>/logical_backups/<ts>.sql.gz` | nightly 00:30 |
| Config volumes | HA persist, Plex config, gonic data+playlists, CephFS `/satellites` | restic, one repo, host = volume | `galaxy-cluster-backups/restic/config` | nightly 01:00–02:00 |

**Deliberately not backed up** (re-acquirable, or not worth the bytes):
`media/mm-media` (1.7 TiB arr library), `music-streaming/gonic-music-pvc`
(rsync'd from lidarr nightly), Prometheus TSDB, `registry-cache`, `attic`
storage, `trivy` DB, Plex transcode/cache. If one of these dies, the fix is a
rescan/resync, not a restore.

## Cold-start dependencies — keep these OUTSIDE the cluster and repo

If all of these are in the password manager, everything else is derivable.
If any is missing, that is a bigger hole than anything below.

1. The GitHub repo (and the git-crypt key that unlocks `*.secret.yaml`,
   `nodes/*`, `satellites/.keys/*`).
2. B2 **master** key — the account itself.
3. B2 app key `galaxy-operator-laptop` (both buckets, incl. `deleteFiles`
   and `readBucketInfo`; the only key that can prune for real or read
   point-in-time versions).
4. restic repo password for `restic/config`.

The in-cluster keys (`galaxy-family-cluster`, `galaxy-pg-cluster`,
`galaxy-restic-cluster`) are also in the sealed secrets, but those only
decrypt with a live sealed-secrets controller.

## How the buckets are made safe against the cluster itself

Every key the cluster holds has `listBuckets, listFiles, readFiles,
writeFiles` and **no `deleteFiles`**. Through the S3 API, a "delete" with such
a key only *hides* the current version — the bytes stay until a lifecycle
rule purges them:

| Bucket / prefix | Hidden versions purged after | Meaning |
|---|---|---|
| `galaxy-family` (all) | 90 days | a hostile `rclone purge` is reversible for 90 days |
| `galaxy-cluster-backups/restic/` | 30 days | restic `prune` is a soft delete for 30 days |
| `galaxy-cluster-backups/spilo/` | uploaded→hidden 30 days, hidden→purged 1 day | 30 nightly dumps kept, no delete capability needed |

Both buckets have SSE-B2 at rest. Family media is **not** client-side
encrypted — chosen so the archive stays legible without any tooling
(`toad-backups` has been that way since 2023). `rclone crypt` is the
one-line switch if that ever changes; its password then joins the list above.

Native-B2-API clients (restic `b2:` backend, rclone `b2` backend) reject
these keys ("not supported on API version N"); everything uses the S3
endpoint `https://s3.us-west-004.backblazeb2.com`, region `us-west-004`.
rclone needs `no_check_bucket = true` (a restricted key can't
`CreateBucket`).

## Family media

### Restore a file or folder (laptop, no cluster)

```bash
# one-time: rclone remote "b2" with the galaxy-operator-laptop key
export RCLONE_CONFIG_B2_TYPE=s3 RCLONE_CONFIG_B2_PROVIDER=Other \
       RCLONE_CONFIG_B2_ENDPOINT=https://s3.us-west-004.backblazeb2.com \
       RCLONE_CONFIG_B2_REGION=us-west-004 RCLONE_CONFIG_B2_NO_CHECK_BUCKET=true \
       RCLONE_CONFIG_B2_ACCESS_KEY_ID=… RCLONE_CONFIG_B2_SECRET_ACCESS_KEY=…

nix develop --command rclone copy "b2:galaxy-family/current/2015 Peru" ./restore/
# something deleted last week? it was moved aside:
nix develop --command rclone ls "b2:galaxy-family/deleted/"
# or view the bucket as of a moment (needs the laptop key; allow clock skew):
nix develop --command rclone copy --s3-version-at 2026-09-19T00:00:00Z \
  "b2:galaxy-family/current/2015 Peru" ./restore/
```

Or the B2 web console → `galaxy-family` → "Show file versions" → download.

### Restore the whole volume into the cluster

`docs/backups/family-restore.job.yaml` — a hand-applied Job that `rclone copy`s
`b2:galaxy-family/current` into the PVC (copy, never sync: additive and safe
on a populated volume). Because the PVC is CephFS RWX, nothing needs to be
scaled down. It optionally verifies every file against a sha1 manifest
ConfigMap (`toad-manifest`), which is how the initial 2026-09 ingest was
checked.

```bash
kubectl apply -f docs/backups/family-restore.job.yaml
kubectl -n media logs -f job/family-restore
kubectl delete -f docs/backups/family-restore.job.yaml
```

**Rate-limit it.** The OSDs are SMR drives: after ~25 GiB of sustained
writes their on-disk cache fills and commit latency goes to seconds — OSDs
get marked down, Prometheus restarts, Postgres stalls (2026-09-21, first
ingest attempt at 30 MB/s). Keep `--bwlimit` low (single-digit MB/s), run
overnight, and watch `ceph -s` slow ops. Same applies to any bulk drop over
NFS.

**Memory limit.** The kernel cephfs client has no cgroup-writeback support:
dirty page cache from writes is charged to the writing pod's cgroup but never
throttled by it. A memory limit on a heavy CephFS writer = OOM-kill (seen at
2 Gi and 3 Gi with rclone using 50 MB of its own). The restore Job has no
memory limit on purpose; filebrowser's was raised to 1 Gi for the same reason.

### Failure modes designed out
- Empty/unmounted volume at backup time → the CronJob refuses to sync
  (< 100 files) instead of moving the bucket to `deleted/`.
- Deletions and overwrites go to `deleted/<date>/` **and** stay as hidden
  versions for 90 days.
- The cluster key cannot purge versions.

## Postgres

Nightly `pg_dumpall` per cluster, uploaded by the operator's own CronJob
(`logical-backup-<cluster>` in each namespace). No point-in-time recovery —
worst case is losing up to 24 h of writes. Config: `configLogicalBackup` in
`cluster/applications/postgres-operator.yaml`; creds: Secret
`logical-backup-s3` in each cluster's namespace (the operator reads it from
there, not from its own).

Gotchas already hit:
- The operator only re-renders its backup CronJobs when the image or
  schedule changes. After changing S3 config: delete the six CronJobs and
  `kubectl -n postgres-operator rollout restart deploy/postgres-operator`.
- The chart's default `logical-backup` image (v1.12.0) is newer than the
  operator (v1.11.0) and dies on an unset env var; the image is pinned to
  v1.11.0 in the values — keep them in lockstep.
- A dump of a 31 MB database took ~11 minutes: cold catalog reads on the
  spinners. Fine for a nightly, don't panic at the duration.

### Restore — `docs/backups/pg-restore.sh`

```bash
docs/backups/pg-restore.sh <namespace> <source-cluster> [target-cluster] [dump-key]
```

It downloads the newest dump (or the one named), waits for the target to
accept connections, loads it with `psql`, **re-applies the target's
operator-managed role passwords** (pg_dumpall carries `ALTER ROLE … PASSWORD`
for every role and would otherwise lock Patroni and the apps out), and
prints per-database row counts for target vs source. "already exists" errors
are expected and counted, not fatal.

**Drill** (safe, no downtime): load into a throwaway cluster.
```bash
kubectl apply -f docs/backups/acid-drill.yaml        # edit namespace to match
docs/backups/pg-restore.sh home-assistant acid-ha acid-drill
kubectl delete -f docs/backups/acid-drill.yaml
kubectl -n home-assistant delete pvc pgdata-acid-drill-0   # retained by policy; S4
```

**Real restore of cluster X** (data lost or corrupted):
1. Scale X's app(s) to 0 and pause Argo self-heal on that app
   (`argocd app set <app> --sync-policy none`).
2. Delete the `postgresql` CR and its `pgdata-<X>-*` PVCs (S4 — confirm).
   Argo recreates the CR; the operator brings up an empty cluster with fresh
   role passwords in its Secrets.
3. `docs/backups/pg-restore.sh <ns> <X> <X>` — same script, target = X.
4. Re-enable Argo sync; scale the app back up. Its user Secret still
   matches because step 3 re-applied it.

## Config volumes (restic)

One repo, snapshots grouped by `--host`:

| host | what | notes |
|---|---|---|
| `home-assistant-persist` | HA config, `.storage/`, custom components | recorder DB is in Postgres; local `home-assistant_v2.db*` excluded |
| `mm-plex-config` | Plex library DB, metadata, prefs | live SQLite may be torn; Plex's own `*.db-YYYY-MM-DD` backups in the same dir are consistent. Cache/Logs excluded |
| `gonic` | `/data` (gonic.db) + `/playlists` | small; a torn DB just means a rescan |
| `satellites-cephfs` | CephFS `/satellites` | currently empty — octoprint's NFS mount is still commented out in `satellites/hosts/octoprint/default.nix`. Backed up anyway so it's covered the day a satellite starts using it; the canary only checks a snapshot exists |

Retention: `--keep-daily 14 --keep-weekly 8 --keep-monthly 12`, applied
weekly by `backups/restic-maintenance` (Sunday 05:00, also runs `check
--read-data-subset=10%`). `backups/restic-canary` restores one known file per
host on the 1st of every month.

The RWO volumes (HA, Plex, gonic) are RBD, so their jobs carry a required
`podAffinity` to the app pod: they run on the same node, mounting the same
PVC read-only. If the app is scaled to 0 the job can't schedule — that fails
and alerts, which is the correct outcome.

Gotchas already hit (2026-09-21, first scheduled cycle):
- The jobs run as root with `capabilities.drop: [ALL]` + `add: [DAC_OVERRIDE]`.
  Dropping ALL removes root's ability to read the app's `0600` files
  (Plex `.LocalAdminToken`); `DAC_READ_SEARCH` would be the minimal fix but
  the cluster-wide PodSecurity default is **baseline**, which rejects it at
  admission — pods silently never start and the Job fails with no logs.
  `DAC_OVERRIDE` is baseline-allowed; the mounts are read-only anyway.
- Every backup job is `restartPolicy: Never`. With `OnFailure` the controller
  deletes the pod when backoffLimit is hit, taking the logs with it.
- `family-backup` excludes `*.partial` so a concurrent ingest/restore can't
  fail the nightly mirror on rclone's in-flight temp files.

### Inspect / restore a file (laptop, no cluster)

```bash
export RESTIC_REPOSITORY=s3:https://s3.us-west-004.backblazeb2.com/galaxy-cluster-backups/restic/config
export RESTIC_PASSWORD=…                                  # password manager
export AWS_ACCESS_KEY_ID=… AWS_SECRET_ACCESS_KEY=…         # galaxy-operator-laptop key
nix develop --command restic snapshots --group-by host
nix develop --command restic ls latest --host home-assistant-persist | less
nix develop --command restic restore latest --host home-assistant-persist \
  --include /data/.storage --target ./out               # → ./out/data/.storage/…
```

Paths inside snapshots are the container's mount points: `/data` (and
`/playlists` for gonic).

### Restore a whole volume in place

```bash
argocd app set home-assistant-release --sync-policy none   # stop self-heal fighting step 2
kubectl -n home-assistant scale deploy/ha-home-assistant --replicas=0
# run a Job that mounts the PVC at /data and does:
#   restic restore latest --host home-assistant-persist --target /
# (snapshot paths are absolute, so --target / puts them back under /data)
argocd app set home-assistant-release --sync-policy automated --self-heal
```

For a **drill**, skip the scale-down: restore into a scratch PVC and `diff -r`
against the live one.

### Repo maintenance from the laptop
Only the laptop key can hard-delete. Normally unnecessary — the in-cluster
`restic-maintenance` prunes (soft) and the 30-day lifecycle purges. If the
repo is ever left locked by a killed job: `restic unlock`.

## Alerting

`PrometheusRule backup-alerts` (namespace `prometheus` — the stock rules in
`cluster/prometheus-stack/rules/` are ConfigMaps and are not loaded):

- `BackupCronJobStale` — no success in 36 h (nightly jobs)
- `BackupCronJobNeverSucceeded` — exists 48 h without a success
- `BackupJobFailed` — any failed run, incl. maintenance and canaries

Severity `warning` → Discord `homelab-default`, weekdays 09:00–22:00. A
backup that breaks on Saturday night shows up Monday morning; that is the
intended trade.

## Drill log

| Date | Tier | What was done | Result |
|---|---|---|---|
| 2026-09-21 | Postgres | `acid-ha` dump → `acid-drill`; schema, indexes, FKs, role passwords, Patroni health, row counts compared | exact to dump time (13 tables / 33 idx; 367,472 rows; entities created 5 min after the dump correctly absent) |
| 2026-09-21 | Config | first snapshot of each host taken by hand-triggered jobs | see git log |
| 2026-09-22 | Family | ingest of the 2023 `toad-backups` dump (320 GiB) via `family-restore.job.yaml`, then sha1 of every file vs the manifest taken from the bucket listing | all 4,943 files match. Took ~23 h at 4 MB/s after the 30 MB/s attempt tanked Ceph; 19 files are 0 bytes in the 2023 source (listed in the ingest notes) |
| 2026-09-22 | All | first fully scheduled cycle | 6 pg dumps + 4 restic snapshots succeeded on schedule; the cycle before caught the PodSecurity/DAC and OnFailure-log bugs above |

## History / why it looks like this

Before 2026-09 there was no backup system: two one-off uploads to B2
(`luma-backups`, a 2022 restic repo of `/srv` on the old `luma` box whose
password is lost; `toad-backups`, a 2023 plain-file dump of the NAS media
library), never exercised, never restored. `toad-backups` is the seed of
`/family`. Delete `luma-backups` once its password is confirmed gone.
Alternatives considered (Velero, k8up, kopia, WAL-G) and why not: Velero's
object backup is redundant with Argo and its RBD-snapshot/B2 path is the
least-trodden; k8up is this design as an operator — worth it past ~10
volumes; WAL-G gives PITR nobody needs here and fills small PVCs with WAL if
B2 is unreachable.
