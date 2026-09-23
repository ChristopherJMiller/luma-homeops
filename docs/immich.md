# Immich — the family photo library

`photos.chrismiller.xyz`. Phone sync for the family, plus the 327 GiB archive
at `/family` indexed **in place** as a read-only external library.

| Piece | Where |
|---|---|
| Plumbing (namespace, PVCs, database, secrets, backup) | `cluster/immich/`, Argo app `immich` |
| Immich itself | `cluster/applications/immich-release.yaml`, chart `immich` |
| Custom Postgres image | `images/spilo-vchord/`, built by `.github/workflows/spilo-vchord.yml` |
| Auth | Dex static client `immich` (`cluster/dex/config.secret.yaml`) |

## The database is Spilo with VectorChord bolted on

Immich needs the `vchord` extension. Stock Spilo ships pgvector but not
VectorChord, so `acid-immich` — **and only that cluster** — sets
`spec.dockerImage` to `ghcr.io/christopherjmiller/spilo-vchord`, which is stock
Spilo 16 plus the published `postgresql-16-vchord` deb (it installs
`vchord.so` straight into Spilo's `pkglibdir`; nothing is built from source).

Staying inside the postgres-operator is the point: `acid-immich` gets the
nightly logical backup, Patroni, the metrics sidecar and every operational
habit the other six clusters already have. A standalone Postgres would have
needed its own backup job and its own way of doing everything.

Constraints to re-check on any bump (`docs.immich.app/administration/postgres-standalone`):

| Immich wants | We have |
|---|---|
| Postgres `>= 14, < 20` | 16.3 |
| pgvector `>= 0.7, < 0.9` | 0.7.0 (stock Spilo) |
| VectorChord `>= 0.3, < 2.0` | 1.1.1 |

**`shared_preload_libraries` must list Spilo's own eight libraries plus
`vchord`.** Verified 2026-09-23 that the operator *extends* rather than
replaces the value — but if a Spilo bump changes that set, dropping one
silently disables `pg_cron`/`bg_mon`/etc. After any bump:

```bash
kubectl -n immich exec acid-immich-0 -c postgres -- \
  psql -U postgres -tAc "show shared_preload_libraries"
```

`DB_STORAGE_TYPE=HDD` is **not** set on the server — that variable configures
Immich's own Postgres container, which we don't run. The SMR tuning
(`effective_io_concurrency`, `random_page_cost`) lives on `acid-immich`.

## Storage

| Volume | What | Backed up |
|---|---|---|
| `immich-uploads` | phone sync + everything derived from it | yes — nightly rclone mirror to `b2:galaxy-family/immich` |
| `family-media` | the archive, mounted **read-only** at `/family` | already, by `family-backup` |
| `immich-ml-cache` | downloaded CLIP/face models | no — regenerable |

`immich-uploads` is CephFS RWX because the server and machine-learning pods
both mount it.

**Every CephFS mount here depends on `ms_mode=prefer-crc`** — the mons only
speak msgr2 on :3300, and without it the kernel client fails with "no mds is
up". It is set **on the Rook operator**, `csi.cephFSKernelMountOptions` in
`cluster/applications/rook.yaml`, which Rook writes into the `ceph-csi-config`
ConfigMap that the node plugin reads at mount time.

Setting `kernelMountOptions` on the *StorageClass* does **not** work: the
provisioner never copies it into the PV's `volumeAttributes`, so the mount is
attempted without it (confirmed on a freshly provisioned PV, 2026-09-23).
Static PVs may carry it inline, and that does work. After changing the value,
restart `deploy/rook-ceph-operator` so it regenerates `ceph-csi-config` — no
PV recreation needed, since the option is applied per mount.

The backup excludes `thumbs/` and `encoded-video/` — derived data that
regenerates — so the mirror holds only irreplaceable bytes.

## Auth

Immich is **not** behind oauth2-proxy. Its mobile app cannot complete a
browser forward-auth redirect, and phone sync is the reason to run it. Instead
Immich is its own OIDC client (`immich` / "galaxy photos") against Dex, so
family sign in with the same Google or Microsoft account they use everywhere
else. Authorization is Immich's own user list, not an allowlist.

The client secret exists in two places that must stay equal:
`cluster/dex/config.secret.yaml` and `cluster/immich/secrets.secret.yaml`.
After changing either, re-seal **both** — and remember `sign.sh` skips any
`*.secret.yaml` whose sealed sibling already exists, so `rm` the `.yaml` first.
Dex also only reads its config at startup: `kubectl -n dex rollout restart
deploy/dex`.

## Users and sharing

The model, and the one irreversible choice:

- A dedicated **`family` account owns the external library** pointing at
  `/family`. An external library belongs to exactly one user, chosen at
  creation, and Immich's docs are explicit that it "can not be changed later"
  — so it must not be a personal account, or the shared archive is welded to
  one person's library forever.
- Everyone else (Chris, Erin, Jeanne, Jim, Kelly) has their **own account**
  with private uploads and their own phone sync.
- The archive reaches people as **shared albums** from the `family` account.
  Partner sharing is the wrong tool: it shares a whole library one-way.

Immich never writes to `/family`. Album membership and descriptions live in
its database; the files are untouched, and `/family` stays the source of truth
still mirrored to B2 by `family-backup`.

## Importing the archive

Adding the external library triggers a scan: thumbnailing and ML-indexing
thousands of files. That is the same sustained-IO shape that took Ceph down
during the original 327 GiB ingest (see the SMR notes in `docs/backups.md`).
Run it deliberately:

```bash
# watch while it scans; stop the job if slow ops appear
kubectl -n rook exec deploy/rook-ceph-tools -- ceph -s | grep -E 'slow ops|client:'
kubectl -n rook exec deploy/rook-ceph-tools -- ceph osd perf
```

Pause the scan (Administration → Jobs) if slow ops climb or OSD commit latency
holds above ~2.5 s. Idle tail latency on these disks is already 0.2–1.7 s, so
judge by slow ops rather than latency alone.

## Restore

- **Photos:** `rclone copy b2:galaxy-family/immich/current /path` — files as
  files, same as the family archive. Deletions are in `immich/deleted/<date>/`
  and B2 keeps hidden versions 90 days.
- **Database:** `docs/backups/pg-restore.sh immich acid-immich` — the same
  script and drill as every other cluster.
- Losing the database but keeping the uploads means re-importing and losing
  albums/faces, not losing photos.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `chart "immich" version X not found` | the version in the chart's git main is not published; check `helm search repo immich/immich --versions` |
| Server fails on vector extension | `CREATE EXTENSION vchord CASCADE` not run in the `immich` database, or `vchord` missing from `shared_preload_libraries` |
| CephFS PVC stuck, "no mds is up" | `ms_mode=prefer-crc` is missing — see Storage above. Check `kubectl -n rook get cm ceph-csi-config -o jsonpath='{.data.config\.json}'`; if `cephFS.kernelMountOptions` is empty, restart `deploy/rook-ceph-operator` |
| `pg_hba.conf rejects connection … no encryption` | Spilo requires TLS on network connections. `DB_SSL_MODE: require` — not `verify-*`, the cert is self-signed |
| Mobile app cannot log in | someone added an oauth2-proxy middleware to the Ingress — it must have none |
| Ceph slow ops during import | the library scan; pause it in Administration → Jobs |

## System settings are code, not UI

Immich reads `/config/immich-config.yaml`, mounted by the chart from the
`immich-config` Secret (`cluster/immich/config.secret.yaml`). **While a config
file is set, the entire settings UI is read-only** — not just the OAuth page.
That is deliberate: settings get reviewed in git and survive a rebuild.

What lives there:

| Setting | Why it is set the way it is |
|---|---|
| `oauth.*` | Dex issuer, `autoRegister: true` so a family member who can reach Dex gets an account on first login |
| `oauth.mobileOverrideEnabled` | Dex is strict about redirect URIs and rejects the `app.immich://` custom scheme; the override routes the mobile flow through the server. This is what makes phone sync work |
| `job.*.concurrency` | **The SMR throttle.** Defaults assume SSDs (`thumbnailGeneration: 3`, `metadataExtraction: 5`, `library: 5`); everything disk- or ML-heavy is pinned to 1 |
| `library.scan.cronExpression` | 06:00 UTC, clear of every backup window |
| `library.watch.enabled` | false — inotify over a 327 GiB tree is not worth it |
| `passwordLogin.enabled` | true, so the admin account can be created and recovered without depending on Dex |

Changing any of it: edit the secret, `rm cluster/immich/config.yaml`, re-run
`./sign.sh`, commit. Immich re-reads the file on restart —
`kubectl -n immich rollout restart deploy/immich-server`.

**What cannot be declared:** users and external libraries. The config file's
`user` key only holds `deleteDelay`, and libraries are database rows. Creating
the `family` account and pointing its external library at `/family` stays a
one-time step in the UI (or via the API).
