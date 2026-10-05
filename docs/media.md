# Media stack runbook (mega-media)

Sonarr / Radarr / Lidarr / Prowlarr / SABnzbd / Plex / Ombi, plus Recyclarr and
Maintainerr, all from the `mega-media` chart (`~/Repos/helm-mega-media`,
published at `realliance.github.io/helm-mega-media`). Site values:
`cluster/applications/mega-media-release.yaml`. Ingresses + sealed secrets:
`cluster/media/`. Databases: `acid-media` (Zalando, on `rook-ceph-block-ssd`).

Everything that touches `/media` is pinned to node `middle` (RWO media PVC).
Ombi, Maintainerr and the reconciler are API-only and run anywhere.

## Versions are pinned — bump them on purpose

Every *arr image is an explicit hotio tag (`release-<version>`). The chart's
floating tags plus `pullPolicy: IfNotPresent` meant nodes never re-pulled: on
2026-10-05 Sonarr was still on a Dec 2023 build. To bump:

1. `pg_dump -Fc` the app's databases from the acid-media leader
   (`kubectl -n media get pods -l galaxy=acid-media -L spilo-role`).
2. Read the release notes for majors (Radarr 6 / Prowlarr 2 dropped Basic auth —
   ours is `External`, behind oauth2-proxy, so it didn't matter).
3. Change `arrs.<svc>.tag`, commit, watch the FluentMigrator lines in the logs
   and `/api/v3/health` afterwards.

## Quality: TRaSH-Guides via Recyclarr (never in the UI)

`recyclarr.config` in the site values is the source of truth for quality
profiles, custom formats, quality sizes, naming and propers/repacks. It runs as
an Argo PostSync hook Job (`mm-recyclarr-sync`) on every sync; logs:
`kubectl -n media logs job/mm-recyclarr-sync`. UI edits to those settings are
overwritten on the next sync.

| Profile | App | Based on | Used by |
|---|---|---|---|
| `HD-1080p` | Sonarr | TRaSH WEB-1080p | all non-anime series, Ombi TV |
| `Anime-1080p` | Sonarr | TRaSH [Anime] Remux-1080p minus Remux | series type `anime` (K-ON!, Chiikawa) |
| `HD-1080p` | Radarr | TRaSH HD Bluray + WEB | all movies, Ombi movies |
| `Ultra-HD` | Radarr | unmanaged | Ombi's approval-gated 4K requests |

Deliberate deviations (SMR storage, no hardware transcode): upgrades stop at
score 1700 (TRaSH: 10000), 1080p sizes capped (Sonarr WEB 130, Radarr WEB 100 /
Bluray 130 MB/min), anime `min_format_score: 0` (Usenet WEB posts rarely match
TRaSH's fansub tiers). No Remux, no BR-DISK, no x265 at 1080p — Plex can't play
`.iso` and HEVC/DV/TrueHD forces CPU transcodes.

The guide is pinned (`settings.resource_providers[].reference`). To take TRaSH
updates: bump the sha, and first run a one-off Job with
`recyclarr sync --preview` against the live *arrs (same env/volumes as
`mm-recyclarr-sync`) to see what would change.

## Downloads (SABnzbd)

- Articles, repair and unpack happen on `/incomplete` (64Gi SSD PVC); finished
  jobs move to `/media/complete/{tv,movies,music}` on the HDD media volume, so
  imports into `/media/tvshows|movies` are same-filesystem moves.
- Direct Unpack is on and pinned (`direct_unpack_tested = 1`, so it isn't
  re-benchmarked every boot). The ini is regenerated from values on every start.
- New releases (aired < 14 days) are queued at High priority
  (`sabnzbd.downloadClient.recentPriority: 1`), so they overtake any backlog.
- **Bulk backfills must be throttled.** Every finished file is written to SMR
  HDDs x3; >~25 GiB of sustained writes collapses OSD latency
  (see the `known_smr_bulk_writes` incident). For anything bigger than a few
  episodes: set `bandwidth_max = 8M` in the ini via git, feed searches in small
  batches, watch `ceph osd perf` (pause SAB above ~1.5 s sustained), and set it
  back to `""` when the queue drains. Normal daily use runs at full speed.

## Plex

- Sonarr/Radarr/Lidarr have a "Plex" Connect target (reconciler, token from the
  sealed `ombi-bootstrap` secret), so imports/renames/deletes trigger a
  targeted scan.
- Server prefs set through the Plex API on 2026-10-05 (they live in
  `mm-plex-config`, covered by the restic Plex backup, **not** in git):
  `TranscoderHEVCEncoding=0`, `TranscoderHEVCOptimize=0` (HEVC encode needs a
  GPU Plex supports — ours is AMD), `GenerateBIFBehavior=never` (preview
  thumbnails cost CPU + full-file reads on SMR),
  `FSEventLibraryPartialScanEnabled=1`.
- Per-client: set **Burn subtitles → Only image formats**; otherwise ASS/PGS
  subtitles (anime) force a CPU transcode.

## Retention: Maintainerr

`https://maintainerr.chrismiller.xyz` (admin tier — it has no auth of its own).
Connections (Plex, Sonarr, Radarr, Ombi) were set through its API; its config is
SQLite on `mm-maintainerr-config`, not git.

Rule **"Leaving soon: old seasons"** (TV Shows library, season level): a season
matches when Sonarr says it is *not* part of the latest aired/airing season
**and** the show carries the Sonarr tag `current-season-only`. Matches sit in
that Plex collection for 14 days, then Sonarr unmonitors and deletes their
episodes. To put a show on this policy, tag it in Sonarr — nothing else. Before
changing a rule, test it with `POST /api/rules/test {mediaId, rulegroupId}`
(evaluate-only); `/api/swagger` lists the API.

## Gotchas

- An *arr silently ignores download-client field names it doesn't know
  (`recentTvPriority` sent to Radarr "succeeds" and does nothing). The
  reconciler's `SAB_FIELDS` table owns the per-app names.
- Radarr 6 Collections hold their own quality profile — they kept a deleted
  profile "in use" until moved.
- Deleting media: do it through Sonarr/Radarr (`/episodefile/bulk`,
  `/moviefile/bulk`; recycle bin is off) so their databases stay consistent,
  then `rmdir` empty folders.
