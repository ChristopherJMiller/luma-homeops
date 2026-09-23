---
name: immich-api
description: Drive Immich (photos.chrismiller.xyz) through its REST API — create users, create and scan external libraries, inspect and pause jobs, read statistics — for the things that cannot be expressed in cluster/immich/config.secret.yaml. Use when asked to add someone to Immich, import or rescan the family archive, check why photos are not appearing, or throttle/pause Immich work. Encodes the auth dance (password login is DISABLED), the permanent library-ownership rule, and the SMR guard that must accompany any scan. NEVER triggers a library scan without a Ceph watch armed. NEVER creates an external library owned by a personal account. NEVER changes settings through the API that belong in the config file.
---

# immich-api

Immich's system settings are **config-as-code** (`cluster/immich/config.secret.yaml`,
read-only in the UI — see `docs/immich.md`). The API is for the things that
are *database state* and therefore cannot live in git: users, external
libraries, jobs, and statistics.

Rule of thumb: **if it appears in the config file, change it in git, not
here.** Changing a config-file-managed setting through the API either fails or
is silently reverted on the next restart.

## Getting a token

Password login is **disabled** (OIDC only), so there is no interactive login
for automation. Two options:

```bash
# Preferred: an API key, created once in the UI (Account Settings -> API Keys)
# and stored in cluster/immich/accounts.secret.yaml.
API=https://photos.chrismiller.xyz/api
H="x-api-key: $(kubectl -n immich get secret immich-accounts -o jsonpath='{.data.api-key}' | base64 -d)"

# Break-glass: flip passwordLogin true in config.secret.yaml, re-seal, restart,
# then POST /auth/login. Only when OIDC itself is what is broken.
```

From a workstation the host resolves through the edge, so add
`--resolve photos.chrismiller.xyz:443:192.168.0.7` to every curl if split-DNS
is not in play. In-cluster, use
`http://immich-server.immich.svc.cluster.local:2283/api` and skip the resolve.

Check any token before relying on it:

```bash
curl -sS "$API/users/me" -H "$H" | jq -c '{email, isAdmin}'
```

## Users

Family members need **no provisioning** — `oauth.autoRegister` creates an
account on first sign-in through Dex. Create a user by hand only for a
non-person (a service/shared account).

```bash
# list
curl -sS "$API/admin/users" -H "$H" | jq -r '.[] | "\(.email) admin=\(.isAdmin)"'

# create (non-person only)
curl -sS -X POST "$API/admin/users" -H "$H" -H 'Content-Type: application/json' \
  -d '{"email":"…","password":"…","name":"…","shouldChangePassword":false}'
```

**Admin comes from the email, not a claim.** Immich links an OIDC login to an
existing account by email address, so an account created with someone's
Google/Microsoft address is what their SSO login lands on. Immich's
`immich_role` claim is *not* usable here — Dex cannot inject a static custom
claim per user.

## External libraries

An external library indexes files **in place**; Immich never writes to them.
`/family` is mounted read-only into the server for exactly this.

> **An external library belongs to one user, permanently.** Immich's docs:
> it "can not be changed later". The `/family` library is owned by
> `family@chrismiller.xyz` — a deliberate non-person account, so the shared
> archive is not welded to somebody's personal library. Never create one owned
> by a personal account.

```bash
curl -sS "$API/libraries" -H "$H" | jq -c '.[] | {id, name, ownerId, importPaths}'

# create (ownerId must be the family account)
curl -sS -X POST "$API/libraries" -H "$H" -H 'Content-Type: application/json' -d '{
  "ownerId":"<family-uuid>", "name":"Family Archive",
  "importPaths":["/family"],
  "exclusionPatterns":["**/.DAV/**","**/Thumbs.db","**/.DS_Store"]}'

# prove the path is readable from inside the server before scanning
curl -sS -X POST "$API/libraries/<id>/validate" -H "$H" -H 'Content-Type: application/json' \
  -d '{"importPaths":["/family"]}' | jq -c '.importPaths'
```

## Scanning — never without a guard

A scan thumbnails and ML-indexes every file. That is the same sustained-IO
shape that collapsed Ceph during the original 327 GiB ingest: OSD commit
latency into seconds, OSDs marked down, Prometheus restarting. The config file
pins every disk- and ML-heavy queue to `concurrency: 1`, which is the primary
defence — confirm it is actually in effect first:

```bash
curl -sS "$API/system-config" -H "$H" | jq -c '.job | {library, metadataExtraction, thumbnailGeneration, smartSearch, faceDetection}'
```

Then trigger, **with a Monitor watching Ceph that can pause the work**:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$API/libraries/<id>/scan" -H "$H"   # 204
```

The watch must be able to act, not just report. Pause is per-queue:

```bash
for j in library metadataExtraction thumbnailGeneration smartSearch faceDetection; do
  curl -sS -o /dev/null -X PUT "$API/jobs/$j" -H "$H" -H 'Content-Type: application/json' \
    -d '{"command":"pause","force":false}'
done
# resume: same call with {"command":"resume","force":false}
```

Guard thresholds that match this hardware: trip on **≥10 slow ops** or
**≥3 s max OSD commit latency**, sustained over 3 checks. Idle tail latency on
these SMR disks is already 0.2–1.7 s, so latency alone is a poor signal —
`ceph health`'s slow-ops count is the reliable one.

```bash
kubectl -n rook exec deploy/rook-ceph-tools -- ceph health | grep -oE '[0-9]+ slow ops'
kubectl -n rook exec deploy/rook-ceph-tools -- ceph osd perf
```

## Jobs and progress

```bash
curl -sS "$API/jobs" -H "$H" | jq -r 'to_entries[] | "\(.key): active=\(.value.jobCounts.active) waiting=\(.value.jobCounts.waiting) failed=\(.value.jobCounts.failed)"'
curl -sS "$API/server/statistics" -H "$H" | jq -c '{photos, videos, usage}'
curl -sS "$API/server/version" -H "$H" | jq -c .
curl -sS "$API/server/features" | jq -c '{oauth, passwordLogin, configFile}'   # unauthenticated
```

A scan is finished when the asset count stops rising **and** the `library`,
`metadataExtraction` and `thumbnailGeneration` queues are all zero. Asset count
alone plateaus mid-scan while thumbnails are still being generated.

## What not to do here

- **Don't change settings the config file owns** (oauth, job concurrency,
  library scan schedule, passwordLogin). Edit
  `cluster/immich/config.secret.yaml`, `rm cluster/immich/config.yaml`,
  re-run `./sign.sh`, commit, then
  `kubectl -n immich rollout restart deploy/immich-server`.
- **Don't delete assets via the API to "clean up" an import.** External-library
  assets are re-created on the next scan; deleting them loses album membership
  and face data for no gain.
- **Don't write to `/family`.** It is mounted read-only and is mirrored to B2
  by `family-backup`; Immich is a reader of that archive, not its owner.

## Related

- `docs/immich.md` — the runbook: database, storage, auth, bootstrap history.
- `docs/backups.md` — where Immich's uploads and database are backed up.
- `ceph-doctor` — if a scan has already hurt the cluster.
