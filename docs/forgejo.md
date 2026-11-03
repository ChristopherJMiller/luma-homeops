# Forgejo — the Realliance forge (git.realliance.net)

Forgejo 15 LTS for Realliance (Chris's friend group and collaborators): new projects live here
first, GitHub is a mirror. Sign-in is **only** through Realliance SSO (`sso.realliance.net`,
Keycloak on the CrownCloud VPS, repos `realliance/sso*`).

- Spec: `docs/superpowers/specs/2026-11-02-realliance-forge-design.md` (+ its amendment)
- Plan + ledger: `docs/superpowers/plans/2026-11-02-realliance-forge.md`
- Manifests: `cluster/forgejo/` (Application `forgejo`: namespace, volumes, Postgres, secrets,
  backups) and `cluster/applications/forgejo-release.yaml` (the Helm release, chart 17.3.2)
- Structure (orgs, teams, owners, critical repos): `git.realliance.net/realliance/forge`
  (OpenTofu, private; local clone `~/Repos/realliance-forge`)
- Acceptance suite: `~/Repos/realliance-sso` `tests/forge/` — external checks only (HTTP, SSH, B2,
  APIs), so either operator can run it: `nix develop --command pytest tests/forge -q`

## Where things live

| Data | Where | Why |
|---|---|---|
| Users, orgs, teams, issues, PRs, notifications | `acid-forgejo` (PG16, 2 pods) on `rook-ceph-block-ssd` | fsync-heavy |
| `/data`: queues, search indexes, merge scratch, SSH host key, avatars, `app.ini` | PVC `forgejo-data`, 5Gi, `rook-ceph-block-ssd` | small synced writes |
| Bare repos (all git history), `[repository] ROOT=/repos` | PVC `forgejo-repos`, 50Gi, `rook-ceph-block` (HDD) | grows, read-mostly |
| LFS, packages + container images, attachments, archives | B2 `realliance-forge-objects` (Realliance account), served **directly** from B2 (`SERVE_DIRECT`) | binaries never touch the SMR pool or the home uplink |

"minio" in the config is Forgejo's name for any S3 server; there is no MinIO. Backblaze needs
`MINIO_CHECKSUM_ALGORITHM=md5`. The objects key cannot delete versions: Forgejo deletes by hiding,
and B2 purges hidden versions after 30 days.

**Network:** HTTPS via Traefik (`*.realliance.net` wildcard cert, DNS-only so no Cloudflare 100 MiB
limit). SSH on MetalLB `192.168.0.6:22`; the router forwards WAN TCP 22 there (NAT 400) and
reflects LAN connections to the WAN IP (NAT 410), OUTSIDE-IN rule 30 accepts it. The router's own
sshd is reachable on the LAN (`192.168.0.1`) and the tailnet only — WAN :22 is always Forgejo.
Inherited limitation: the WAN IP (DHCP) is hardcoded in the reflection rules, as for 80/443.
Forgejo's built-in SSH server has an RSA host key only (`gitea.rsa`).

## Who gets in

Realliance SSO decides everything; never grant access in Forgejo by hand.

| SSO group (in `realliance/sso-people`) | Effect in Forgejo |
|---|---|
| `/apps/forgejo` | may sign in at all (others are refused at the SSO); account created on first sign-in |
| `/forgejo-admins` | site admin |
| `/realliance` | team `realliance/members`: write to every repo, create repos |
| `/realliance-maintainers` | team `realliance/maintainers`: admin on every repo |

- Usernames come from the SSO (`preferred_username`). An SSO username equal to an existing org or
  user is refused (`ACCOUNT_LINKING=disabled`) — no silent linking.
- Admin status and team membership sync **at each sign-in**, never in between. A group change
  takes effect the next time the person signs in (sessions last 7 days).
- **Owners** of an org are declared in `realliance/forge` (`teams.tf`, `local.owners`) and are
  never in the group→team map (Forgejo refuses to remove a last owner, and the sign-in would fail).
- Members and maintainers **cannot delete or transfer** repos; only Owners can. Org creation is
  admin-only (orgs are code).
- **Email:** Forgejo keeps its own copy from the first sign-in. If someone changes their email in
  the SSO, they also change it in Forgejo (Settings → Account → Emails; no confirmation mail needed).
- People without a last name are fine (SSO last name is optional).

### Removing someone (two steps — both, every time)

forge-sync (periodic reconciliation) is deferred, so the SSO change alone is **not** enough: a
person who can no longer sign in never triggers the sign-in sync, and their SSH keys and access
tokens keep working.

1. SSO: `active: false` for them in `realliance/sso-people` (or remove `/apps/forgejo`), merge.
2. Forgejo: Site Administration → User Accounts → the person → **Prohibit login** (or
   `PATCH /api/v1/admin/users/<name>` with `prohibit_login: true`, `login_name`, `source_id`).
   This blocks their web session ("Account is suspended"), API tokens and SSH immediately.

Reversing both restores everything. Until step 2 is done, access lingers — that is the known gap.
`tests/forge/test_forge_access.py::test_offboarding_blocks_web_token_and_ssh_and_is_reversible`
proves the procedure.

### Gate before the first hidden org

The group→team map (`groupTeamMap` in `forgejo-release.yaml`) is public config. It may name only
public orgs. **Before creating any hidden org**, move the map out of this public repo (a
SealedSecret applied by a PostSync Job, or build forge-sync) — and keep the hidden org's name out
of every public repo, including this one.

## Mail

Resend (realliance.net, sender `git@realliance.net`, its own sending-only key). **Notification mail
is off** (`ENABLE_NOTIFY_MAIL=false`, `DEFAULT_EMAIL_NOTIFICATIONS=disabled`, Chris 2026-11-01):
Resend's free tier is 100 mails/day, shared with the SSO's invites and password resets. People get
in-app notifications. Planned: Matrix (org webhooks) or a daily digest (forge-bot, phase 2c).
The SSO test suite creates its throwaway users disabled-then-enabled so it never sends mail.

## Break-glass

The internal sign-in form is off; the only local account is `forgejo-admin` (password and an
all-scopes API token in `realliance/sso-secrets` `forge/secrets.env`).

```bash
kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin user list
kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin user change-password --username forgejo-admin --password …
```

If the SSO is down: existing sessions (7 days), tokens and SSH keep working; nobody new can sign in.

## Backups and restore

| What | When | Where (Realliance B2, object-locked 30 days, write-only keys) |
|---|---|---|
| `pg_dumpall` of `acid-forgejo` (operator logical backup) | 00:30 | `realliance-forge-backups/spilo/acid-forgejo/…` |
| `forgejo dump` — repos, DB (postgres SQL), `app.ini`, `custom/`, `data/` (no LFS/attachments/packages/archives/indexes) | 01:30, CronJob `forgejo-dump` | `realliance-forge-backups/dump/forgejo-<UTC>.tar.gz` |
| LFS, packages, attachments | continuously | already in `realliance-forge-objects` (versions kept 30 days after a delete) |

The namespace's `logical-backup-s3` secret points the operator's CronJob at the Realliance bucket
(the operator appends the secret's env after its own; the last duplicate wins). The dump streams
out of the running pod (`kubectl exec … forgejo dump --file -` → `rclone rcat`), so it writes
nothing to a volume. App secrets (`SECRET_KEY`, `INTERNAL_TOKEN`, JWT secrets) have canonical
copies in `sso-secrets` `forge/secrets.env`: a restore on a new host needs them (they decrypt stored
2FA and OAuth secrets) — never rotate them.

**Restoring:** repos, `data/` (SSH host key, avatars) and `app.ini` come from the newest `dump/`
archive; the **database comes from the `spilo/` pg_dumpall**, loaded the way
`docs/backups/pg-restore.sh` does it (keep the target's role passwords, load with errors allowed,
re-apply the passwords). The `forgejo-db.sql` inside a `forgejo dump` is a convenience copy only:
it creates tables in an order that ignores Forgejo 15's foreign keys and does not load as-is.

Restore drill: `docs/forgejo/restore-drill.sh fixture|up|check|down` — restores the newest backups
into namespace `forgejo-drill` (read-only key on the live objects bucket, no mail, no mirrors) and
compares it with production (repo heads, an LFS object served from B2, the SSH host key).

**Drill 2026-11-02: PASSED** (`up --fresh` then `check`; restore ~90 s; log in `docs/backups.md`).
The fixture is `realliance/restore-drill-fixture` (one 1 MiB LFS object) — keep it.

## Changing the structure

Orgs, teams, Owners and critical repos: PR to `realliance/forge` (signed commits; direct pushes to
`main` are blocked), then an operator applies (README there). The provider cannot import orgs, so
orgs are always created by that repo. Personal and everyday repos are created freely.

`realliance/forge` is mirrored to `github.com/realliance/forge` (private) by a Forgejo SSH push
mirror whose key is a write deploy key on that one GitHub repo (no token). The provider has no
push-mirror resource; it was set up by hand at bootstrap.
