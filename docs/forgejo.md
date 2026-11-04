# Forgejo — the Realliance forge (git.realliance.net)

Forgejo 15 LTS for Realliance (Chris's friend group and collaborators): new projects live here
first, GitHub is a mirror. Sign-in is **only** through Realliance SSO (`sso.realliance.net`,
Keycloak on the CrownCloud VPS, repos `realliance/sso*`).

- Spec: `docs/superpowers/specs/2026-11-02-realliance-forge-design.md` (+ its amendment)
- Plan + ledger: `docs/superpowers/plans/2026-11-02-realliance-forge.md`
- Manifests: `cluster/forgejo/` (Application `forgejo`: namespace, volumes, Postgres, secrets,
  backups) and `cluster/applications/forgejo-release.yaml` (the Helm release, chart 17.3.2)
- Structure (orgs, teams, owners, critical repos): `git.realliance.net/realliance-ops/forge`
  (OpenTofu, private, in the Owners-only org `realliance-ops`; local clone `~/Repos/realliance-forge`)
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
- Admin status and team membership sync **at each sign-in**, never in between. Granting takes
  effect at their next sign-in. **Taking away** may never take effect on its own — SSH keys and
  tokens never trigger a sync, and a session stays alive while it is used — so demotions are two
  steps, like removals (below).
- **Owners** of an org are declared in `realliance-ops/forge` (`teams.tf`, `local.owners` and
  `local.ops_owners`) and are never in the group→team map (Forgejo refuses to remove a last owner,
  and the sign-in would fail).
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

### Taking away admin or a team (two steps)

1. SSO: remove the group (`/forgejo-admins`, `/realliance`, `/realliance-maintainers`) in
   `realliance/sso-people`, merge.
2. Forgejo: untick **Administrator** on the user (Site Administration → User Accounts), and/or
   remove them from the team (org → Teams). Their next sign-in agrees with this and changes nothing.

`test_demotion_needs_the_forgejo_side_step` proves step 1 alone leaves the access in place and
that step 2 sticks.

### Gate before the first hidden org

On Forgejo both preconditions are met (2026-11-03). The group→team map left this public repo: it
lives in Forgejo's database, and forge-bot's `teams.yaml` in the inventory takes it over. The
inventory moved from `realliance/forge` to `realliance-ops/forge`, an org with the Owners team only,
so `realliance` members can't read it and maintainers can't edit its branch protection.

**Still open — the GitHub mirror.** The inventory is push-mirrored to the private GitHub repo
`realliance/forge`, and the GitHub `realliance` org's base permission is **read**, so every GitHub
org member can read it (and `teams.yaml` will name hidden orgs). **Decided (Chris, 2026-11-03):** GitHub mirroring maps each Forgejo org one-to-one to a configured
GitHub org (the later forge-bot mirroring job), so `realliance-ops` mirrors into its own GitHub org,
not `realliance`. Before the first hidden org, move the inventory's push mirror to that org; the
`realliance` base permission stays as it is. Always: keep every hidden org's name out of every public repo,
including this one.

## forge-bot

> **Stub.** The full runbook (each alert, mode changes, the bulk override, offboarding with
> forge-bot) is written by the forge-bot plan's Task 19. The alert summaries link here.

forge-bot reconciles Forgejo with the SSO every 5 min ± 30 s: login (`access`), site admin
(`admin`), mapped teams (`teams`), email, name and pronouns (`profile`) and the SSO picture
(`avatar`). Code, README (what it writes, safety rules, metrics) and alert rules:
`git.realliance.net/realliance/forge-bot`. Spec:
`docs/superpowers/specs/2026-11-02-forge-bot-and-ci-design.md`.

- **Manifests:** `cluster/forge-bot/` (Application `forge-bot`). `config.yaml` holds the job
  modes; every job starts in `dry-run` (plan, audit, meter, write nothing). Move one job at a time
  to `apply` and watch a cycle. A config change restarts the pod (hash-suffixed ConfigMap).
- **One bot, never two:** a StatefulSet (`forge-bot-0`), because two bots would race each other's
  writes. On node loss forge-bot stays down until the node returns, or an operator force-deletes
  the pod after confirming the node is off (S4); ForgeBotStale fires meanwhile. A rollout whose new
  pod never becomes Ready stalls: revert the change, then delete `forge-bot-0`.
- **Trust root:** `cluster/forge-bot/keyring/`: the Owners' public keys and their fingerprints.
  Only an inventory head signed by one of them is trusted. It lives here, not in the inventory.
- **Image:** pinned `sha-<7>@sha256:…` in `statefulset.yaml`. CI builds every `main` push and
  tag; nothing deploys until a reviewed re-pin. Renovate never automerges this image
  (`.renovaterc.json`, guarded by the `forge-bot-renovate` hook): its PR for a moved `sha-` tag
  is a rebuild or a tamper, never a merge. After a re-pin, run
  `nix develop --command scripts/sync-forge-bot-alerts.sh` (the PrometheusRule is a copy of the
  release's `internal/metrics/alerts.yaml`); the `forge-bot-alerts` pre-commit hook runs promtool
  lint and forge-bot's own alert tests and fails until the copy matches the pinned commit.
- **Logs:** stdout is the audit trail, with old and new emails and names. It goes only to the
  private sinks (router syslog, Azure Log Analytics). Read it in a terminal; don't paste it.
- **Network:** default-deny both ways. In: Prometheus's namespace on :8080. Out: DNS, the Forgejo
  pods on :3000 and TCP 443 to public addresses (the SSO is behind Cloudflare; the WAN /25 is
  excepted). The policy is enforced only on nodes where the kube-network-policies agent runs.
- **Look:** `kubectl -n forge-bot logs forge-bot-0`; metrics `forge_bot_*` (README table). The
  scrape keeps only `forge_bot_*`, `go_*`, `process_*` and `promhttp_*` series.

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

| What | When | Where (Realliance B2, object-locked 30 days, no-delete keys) |
|---|---|---|
| `pg_dumpall` of `acid-forgejo` (operator logical backup) | 00:30 | `realliance-forge-backups/spilo/acid-forgejo/…` |
| `forgejo dump` — repos, DB (postgres SQL), `app.ini`, `custom/`, `data/` (no LFS/attachments/packages/archives/indexes) | 01:30, CronJob `forgejo-dump` | `realliance-forge-backups/dump/forgejo-<UTC>.tar.gz` |
| LFS, packages, attachments | continuously | already in `realliance-forge-objects` (versions kept 30 days after a delete) |

The namespace's `logical-backup-s3` secret points the operator's CronJob at the Realliance bucket
(the operator appends the secret's env after its own; the last duplicate wins). The dump streams
out of the running pod (`kubectl exec … forgejo dump --file -` → `rclone rcat`), so it writes
nothing to a volume. App secrets (`SECRET_KEY`, `INTERNAL_TOKEN`, JWT secrets) have canonical
copies in `sso-secrets` `forge/secrets.env`: a restore on a new host needs them (they decrypt stored
2FA and OAuth secrets) — never rotate them. Every dump carries `app.ini` **in plaintext**, i.e. those
secrets plus the objects key and the Resend key: read access to `realliance-forge-backups` (the
forge backup key, the Realliance master key) is read access to all of the forge's secrets.

**Restoring:** repos, `data/` (SSH host key, avatars) and `app.ini` come from the newest `dump/`
archive; the **database comes from the `spilo/` pg_dumpall**, loaded the way
`docs/backups/pg-restore.sh` does it (keep the target's role passwords, load with errors allowed,
re-apply the passwords). The `forgejo-db.sql` inside a `forgejo dump` is a convenience copy only:
it creates tables in an order that ignores Forgejo 15's foreign keys and does not load as-is.

**A real restore** (production lost, or onto a new host), in order:

1. Argo: let `forgejo` (plumbing) create the namespace, volumes and an empty `acid-forgejo`; keep
   `forgejo-release` at 0 replicas (`kubectl -n forgejo scale deploy/forgejo --replicas=0`, S2
   exception — revert by letting Argo re-sync).
2. Database: newest `spilo/acid-forgejo/…/*.sql.gz` into `acid-forgejo`, the pg-restore.sh way.
3. Volumes: from the newest `dump/forgejo-*.tar.gz` (whose time is close to the DB dump's) —
   `repos/` → `forgejo-repos`, `data/` → `forgejo-data`, `app.ini` → `forgejo-data:gitea/conf/app.ini`
   (owner uid 1000), via a helper pod running the pinned Forgejo image — exactly what
   `restore-drill.sh up` does into `forgejo-drill`.
4. Scale Forgejo back to 1 (Argo), then on the pod:
   `forgejo admin regenerate hooks` and `forgejo doctor check --all`.
5. Check: sign in, `git ls-remote` a few repos, fetch an LFS object, compare the SSH host key.

On a brand-new host without galaxy, the same artefacts suffice: the Realliance B2 buckets, the
chart values in `cluster/applications/forgejo-release.yaml` and the app secrets in `sso-secrets`.

Restore drill: `docs/forgejo/restore-drill.sh fixture|up|check|down` — restores the newest backups
into namespace `forgejo-drill` (read-only key on the live objects bucket, no mail, no mirrors) and
compares it with production (repo heads, an LFS object served from B2, the SSH host key).

**Drill 2026-11-02: PASSED** (`up --fresh` then `check`; restore ~90 s; log in `docs/backups.md`).
The fixture is `realliance/restore-drill-fixture` (one 1 MiB LFS object) — keep it.

## Changing the structure

Orgs, teams, Owners and critical repos: PR to `realliance-ops/forge` (signed commits; direct pushes
to `main` are blocked; only Owners may merge or approve; fast-forward is the only merge style, so the
signed commits land as they are), then an operator applies (README there). From a laptop:
`fj -H git.realliance.net pr merge realliance-ops/forge#<n> -d`.

**Forgejo's signing key** "Realliance Forge <git@realliance.net>" (ID `50ACEA5F43099C00`; private
key in `sso-secrets` `forge/signing-key.asc`, sealed as `forgejo-signing`) signs merges and web
edits. Without it Forgejo refuses *every* merge into a branch that requires signed commits
("won't sign: nokey"). `[repository.signing]` drops Forgejo's default 2FA condition — MFA is at the SSO.

**`fj` (forgejo-cli) login:** `fj -H git.realliance.net auth login` signs in through the SSO in a
browser. It needs the public OAuth app "forgejo-cli (fj)" (owned by `forgejo-admin`, redirect
`http://127.0.0.1:26218/`) and this line in `~/.config/forgejo-cli/client_ids`:
`git.realliance.net 41ec7227-eca9-435c-a904-1785bcae60b9` (a public client id, not a secret). The provider cannot import orgs, so
orgs are always created by that repo. Personal and everyday repos are created freely.

`realliance-ops/forge` is mirrored to `github.com/realliance/forge` (private) by a Forgejo SSH push
mirror whose key is a write deploy key on that one GitHub repo (no token). The provider has no
push-mirror resource; it is set up by hand, last re-created on 2026-11-03 after the move (GitHub
deploy key `forgejo-ops`).

## Template overrides (custom/)

Forgejo has no plugin API; small UI changes are copies of upstream templates under
`cluster/forgejo/custom/templates/`, shipped as ConfigMap `forgejo-custom-templates` and mounted
into `$GITEA_CUSTOM/templates/` (see `extraContainerVolumeMounts` in `forgejo-release.yaml`).

| Override | Upstream file | Change |
|---|---|---|
| `user-settings-navbar.tmpl` | `templates/user/settings/navbar.tmpl` (15.0.11) | "Realliance account ↗" link to the SSO account page, top of the settings menu |

**On every Forgejo upgrade:** re-diff each override against the new upstream file
(`curl -sL https://codeberg.org/forgejo/forgejo/raw/tag/v<version>/templates/user/settings/navbar.tmpl | diff - <(tail -n +4 cluster/forgejo/custom/templates/user/settings/navbar.tmpl)`
should show only the added lines), carry the change over, and bump `realliance/templates-rev`
(Forgejo reads templates at start). `tests/forge/test_forge_signin.py::test_settings_link_to_realliance_account`
catches a lost override.
