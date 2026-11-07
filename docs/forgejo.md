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

**The bots live in `realliance-ops`** (since 2026-11-04, plan
`docs/superpowers/plans/2026-11-04-bots-to-realliance-ops.md`): `realliance-ops/forge-bot` (id 94)
and `realliance-ops/ci-dispatch` (id 240), both public, beside the private inventory. Only Owners
push them, so only Owners start a run on the `galaxy` runner, which serves these two repos and
nothing else. Their images are `git.realliance.net/realliance-ops/<bot>`, which CI pushes as the
local account `ops-ci` with the org secret `REGISTRY_TOKEN`. forge-bot's Go module path stays
`git.realliance.net/realliance/forge-bot` until its next reviewed release. A runner connection
matches jobs by repo id; to re-register one, use `--scope realliance-ops/<repo>`.
The old package pusher `forge-ci` retired on 2026-11-04: it cannot sign in, holds no token
and sits in no team. The account stays, so history keeps its author. Team `realliance/ci` went with it and
returned on 2026-11-04 for `realliance-ci`, the site's package pusher (see realliance.net, below).

See also `docs/pages.md` (Realliance Pages: the system webhook, the relay, `*.realliance.page`).

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

### Removing someone (two steps, plus a third for `realliance` members)

forge-sync (periodic reconciliation) is deferred, so the SSO change alone is **not** enough: a
person who can no longer sign in never triggers the sign-in sync, and their SSH keys and access
tokens keep working.

1. SSO: `active: false` for them in `realliance/sso-people` (or remove `/apps/forgejo`), merge.
2. Forgejo: Site Administration → User Accounts → the person → **Prohibit login** (or
   `PATCH /api/v1/admin/users/<name>` with `prohibit_login: true`, `login_name`, `source_id`).
   This blocks their web session ("Account is suspended"), API tokens and SSH immediately.

3. If the person was a `realliance` member, rotate both `realliance-ci` tokens (see Rotating `realliance-ci`'s tokens under realliance.net). Any member can read them through a branch-push workflow, and a registry push with the site token deploys to galaxy.

Reversing steps 1 and 2 restores everything (step 3 is not reversible). Until step 2 is done, access lingers — that is the known gap.
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
inventory moved from `realliance/forge` to `realliance-ops/forge`. `realliance-ops` holds the
Owners, plus team `ci` (packages only, no repos; its one member is `ops-ci`). `realliance`
members can't read the inventory, maintainers can't edit its branch protection, and neither
group can push either bot.

**Still open — the GitHub mirror.** The inventory is push-mirrored to the private GitHub repo
`realliance/forge`, and the GitHub `realliance` org's base permission is **read**, so every GitHub
org member can read it (and `teams.yaml` will name hidden orgs). **Decided (Chris, 2026-11-03):** GitHub mirroring maps each Forgejo org one-to-one to a configured
GitHub org (the later forge-bot mirroring job), so `realliance-ops` mirrors into its own GitHub org,
not `realliance`. Before the first hidden org, move the inventory's push mirror to that org; the
`realliance` base permission doesn't change. The same mapping puts forge-bot's and ci-dispatch's
mirrors in the `realliance-ops` GitHub org, public there, beside the private inventory mirror.
Neither bot has a mirror today. Always: keep every hidden org's name out of every public repo,
including this one.

## Mail

Resend (realliance.net, sender `git@realliance.net`, its own sending-only key). **Notification mail
is off** (`ENABLE_NOTIFY_MAIL=false`, `DEFAULT_EMAIL_NOTIFICATIONS=disabled`, Chris 2026-11-01):
Resend's free tier is 100 mails/day, shared with the SSO's invites and password resets. People get
in-app notifications. Planned: Matrix (org webhooks) or a daily digest (forge-bot, phase 2c).
The SSO test suite creates its throwaway users disabled-then-enabled so it never sends mail.

## Sign-in redirect

Sign-in goes straight to the Realliance SSO (Chris, 2026-11-05). Forgejo 15 has no setting for it
(upstream [#3342](https://codeberg.org/forgejo/forgejo/issues/3342)), so an `IngressRoute` in
`cluster/forgejo/signin-redirect.yaml` redirects `GET /user/login` to `/user/oauth2/realliance`,
keeping `?redirect_to=`. It never restarts Forgejo. This is the one edge exception to the Ingress-only rule,
because an Ingress cannot read a cookie.

- A `flash` cookie turns it off. A failed SSO callback sets that cookie, so the user sees Forgejo's
  error page instead of looping through Keycloak.
- `?local=1` (any `?local`) turns it off and shows the stock login page, which has only the SSO button.
- `POST /user/login`, the API, git and `/v2` never match. Forgejo still answers the POST with 403.
- Rollback: revert the commit. Argo prunes both objects.
- Test: `scripts/test_forgejo_signin_redirect.py` (pre-commit). After an edge change, run
  `edge-snapshot.sh`: its probes cover the login page, the API and the registry.

## Break-glass

The internal sign-in form is off (`ENABLE_INTERNAL_SIGNIN=false`) and so is basic authentication, so
**no password signs in anywhere**, `forgejo-admin`'s included. The password form stays off. Forgejo has no
web sign-in for the local administrator. With the SSO down, three things still work:

1. **The API token of the administrator.** The key `FORGEJO_ADMIN_TOKEN` in `realliance/sso-secrets`
   `forge/secrets.env` belongs to `forgejo-admin` and has all scopes. Send it as
   `Authorization: token …`:

   ```bash
   T=$(grep '^FORGEJO_ADMIN_TOKEN=' forge/secrets.env | cut -d= -f2-)
   curl -s -H "Authorization: token $T" https://git.realliance.net/api/v1/user   # login: forgejo-admin
   ```

2. **The Forgejo command-line tool on the pod.** It talks to the database directly:

   ```bash
   kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin user list --admin
   kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin user generate-access-token --username forgejo-admin --scopes all --token-name breakglass-$(date +%F)
   ```

   Use it to mint a new token if you lose or revoke the stored one, and to inspect users. Store
   any token you keep in `sso-secrets`. Revoke one you no longer need.
3. **Existing sessions (7 days), tokens and SSH** keep working. Nobody new can sign in.

A real web path would mean setting `ENABLE_INTERNAL_SIGNIN=true` in `forgejo-release.yaml`, which
restarts the pod. Give `forgejo-admin` TOTP first, and sign in at `/user/login?local=1`. Not done:
the decision is to keep the form off.

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
   `forgejo admin regenerate hooks` and `forgejo doctor check --all`. The hooks step applies on 15
   only. Forgejo 16 and later centralise hooks and rewrite them at start, so drop it from 16 on.
5. Check: sign in, `git ls-remote` a few repos, fetch an LFS object, compare the SSH host key.

On a brand-new host without galaxy, the same artefacts suffice: the Realliance B2 buckets, the
chart values in `cluster/applications/forgejo-release.yaml` and the app secrets in `sso-secrets`.

Restore drill: `docs/forgejo/restore-drill.sh fixture|up|check|down` — restores the newest backups
into namespace `forgejo-drill` (read-only key on the live objects bucket, no mail, no mirrors) and
compares it with production (repo heads, an LFS object served from B2, the SSH host key).

**Drill 2026-11-02: PASSED** (`up --fresh` then `check`; restore ~90 s; log in `docs/backups.md`).
The fixture is `realliance-ops/restore-drill-fixture` (one 1 MiB LFS object; private, not in the
inventory; moved from `realliance` on 2026-11-04, same repo id and LFS object) — keep it.

### Getting one repo back without Forgejo

When the forge is down and someone needs a repo now, an Owner gets it from the nightly dump. No
cluster, no Forgejo:

```bash
docs/forgejo/repo-from-backup.sh realliance-ops/restore-drill-fixture
```

The script streams the newest `dump/forgejo-<UTC>.tar.gz` through `tar` and keeps only
`repos/<owner>/<repo>.git` and its `.wiki.git`. The archive never touches the disk, and neither do
`app.ini` and the database. It runs `git fsck`, lists the branches and clones a working copy into
`./<repo>` (or the second argument). `origin` points at the forge, `origin/*` holds the branches as
of the dump. `--list` names the repos in the dump, `--dump forgejo-<UTC>.tar.gz` picks an older one.

It needs git, tar (GNU or `bsdtar`) and `rclone`, not the `b2` command-line tool. The one credential
is the forge backup key. The script reads it from realliance-sso's `tofu -chdir=b2 output` when the
realliance-sso and `sso-secrets` checkouts and nix are present. Elsewhere, export
`FORGE_BACKUP_KEY_ID` and `FORGE_BACKUP_KEY` first (`read -rs` keeps the key out of the history).

- **Freshness:** up to a day old, as of the last 01:30 dump (the UTC time in its name). Later pushes
  exist only in people's clones.
- **Not in the dump:** LFS objects, attachments and packages, which live in `realliance-forge-objects`.
  Issues, PRs, releases and the wiki as Forgejo pages need a real restore.
- **Forgejo gone for good:** `--all [dest]` extracts every bare repo and checks each with `git fsck`.
  Move one to a new host with
  `git -C <owner>/<repo>.git push <url> 'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'`.

**LFS objects.** The working copy keeps LFS files as pointers. Each object is in
`realliance-forge-objects` at `lfs/<oid[0:2]>/<oid[2:4]>/<oid[4:]>`. Fetch them with git-lfs and
the read-only objects key (`forge_objects_ro_key_id` and `forge_objects_ro_key` in the same b2
outputs). In the working copy, paste the key ID, Enter, then the key, Enter:

```bash
read -rs RCLONE_CONFIG_FORGEOBJ_ACCESS_KEY_ID
read -rs RCLONE_CONFIG_FORGEOBJ_SECRET_ACCESS_KEY
export RCLONE_CONFIG_FORGEOBJ_ACCESS_KEY_ID RCLONE_CONFIG_FORGEOBJ_SECRET_ACCESS_KEY
export RCLONE_CONFIG_FORGEOBJ_TYPE=s3 RCLONE_CONFIG_FORGEOBJ_PROVIDER=Other RCLONE_CONFIG_FORGEOBJ_ENDPOINT=https://s3.us-east-005.backblazeb2.com
git lfs ls-files --all --long | while read -r oid _; do rclone copyto "forgeobj:realliance-forge-objects/lfs/${oid:0:2}/${oid:2:2}/${oid:4}" ".git/lfs/objects/${oid:0:2}/${oid:2:2}/$oid"; done
git lfs checkout
git lfs fsck
```

**Drill 2026-11-06 passed:** the fixture came back in 4 s with `main` equal to production, and its
LFS object matched its SHA-256. Log in `docs/backups.md`.

## Changing the structure

Orgs, teams, Owners and critical repos: PR to `realliance-ops/forge` (signed commits; direct pushes
to `main` are blocked; only Owners may merge or approve; fast-forward is the only merge style, so the
signed commits land as they are), then an operator applies (README there). From a laptop:
`fj -H git.realliance.net pr merge realliance-ops/forge#<n> -d`.

**What the inventory owns.** Everything in `realliance-ops`, plus the `realliance` org and its teams. It
declares no repo of `realliance`: the five shared repos (`brand`, `realliance.net`, `forgejo`, `pages`,
`actions`) are member-run, and since 2026-11-05 their settings and `main` protection live in the
Forgejo UI, where maintainers own them. Chris's starting point: PR-only `main`, any member merges,
fast-forward by default or squash. Tofu forgot them with `removed` blocks and `destroy = false`, so
tofu destroyed nothing. Everything else in `realliance` also belongs to its members: `members` can create
repos (tested in `realliance/sso`), and tofu never touches a repo it doesn't declare. Every member has
write access to those repos, unprotected unless their creator adds protection.

**Signing policy (2026-11-05).** `realliance-ops` repos (`forge`, `forge-bot`, `ci-dispatch`): signed commits, fast-forward only. The five member-run `realliance` repos take any commits on `main`, fast-forward or squash. Their release tags carry a protection for Owners and maintainers: `v*` on `brand`, `forgejo` and `actions`, `[0-9]*` on `realliance.net`. The API sets them, because the provider has no tag-protection resource. Consumers trust the signed tag (`git verify-tag`, then its tree), never `main`'s commits. The realliance.net release review checks the tag signature plus the range, not every commit. The SSO tests pin the `brand` and `actions` tag protections, and nothing else about the five repos.

**Forgejo's signing key** "Realliance Forge <git@realliance.net>" (ID `50ACEA5F43099C00`; private
key in `sso-secrets` `forge/signing-key.asc`, sealed as `forgejo-signing`) signs merges and web
edits. Without it Forgejo refuses *every* merge into a branch that requires signed commits (now only the ops repos)
("won't sign: nokey"). `[repository.signing]` drops Forgejo's default 2FA condition — MFA is at the SSO.

**First merge into a repo imported from GitHub (ops repos only now; the `realliance` repos need no signatures):** when the base head carries GitHub's web-flow signature, which Forgejo can't verify, a branch with `require_signed_commits` refuses even a fast-forward (`won't sign: basesigned`). The fix is an inventory PR that turns `require_signed_commits` off, the merge, then the revert and apply at once; after that the head is Owner-signed (realliance.net, 2026-11-04, open for under a minute).

**`fj` (forgejo-cli) login:** `fj -H git.realliance.net auth login` signs in through the SSO in a
browser. It needs the public OAuth app "forgejo-cli (fj)" (owned by `forgejo-admin`, redirect
`http://127.0.0.1:26218/`) and this line in `~/.config/forgejo-cli/client_ids`:
`git.realliance.net 41ec7227-eca9-435c-a904-1785bcae60b9` (a public client id, not a secret). The provider cannot import orgs, so
orgs are always created by that repo. Personal and everyday repos are created freely.

`realliance-ops/forge` is mirrored to `github.com/realliance/forge` (private) by a Forgejo SSH push
mirror whose key is a write deploy key on that one GitHub repo (no token). The provider has no
push-mirror resource; it is set up by hand, last re-created on 2026-11-03 after the move (GitHub
deploy key `forgejo-ops`).

## realliance.net and the general runner

This forge builds and publishes the Realliance website. Renovate re-pins galaxy to each new `X.Y.Z` release and automerges it.

**Where things live.**

- Source: `git.realliance.net/realliance/realliance.net`, one of the five shared repos. GitHub holds an archived copy with a pointer.
- Workflow: `.forgejo/workflows/ci.yaml`, run on the `general` runner. It builds with Deno and crane, with no Docker, and pushes as `realliance-ci`.
- Image: `git.realliance.net/realliance/realliance.net`, tags `X.Y.Z` and `sha-<7>`. CI never moves a tag.
- Pin: `cluster/realliance.net/home.yaml`, as `X.Y.Z@sha256:…`. `scripts/test_realliance_net_pin.py` guards the rest of the Deployment.
- Argo app `realliance-net` (`cluster/realliance.net/`), serving `realliance.net` and `www`.
- Runner: ns `forgejo-runner-general` (`cluster/forgejo-runner-general/`).

Who may change `main`, and the signing rules, are in the paragraphs **What the inventory owns** and **Signing policy** under Changing the structure. Only Owners and maintainers can push release tags (`[0-9]*`).

**Release.**

1. Merge a PR into `main`. Any member merges; direct pushes are off.
2. An Owner pushes a signed tag, `git tag -s X.Y.Z`. CI publishes `X.Y.Z` and `sha-<7>`.
3. Renovate opens the re-pin PR in luma-homeops (`X.Y.Z@sha256:…`) and automerges it; Argo rolls `deploy/home`. Pre-pulling on each node (`talosctl image pull`) is optional; without it, new pods need Forgejo up.
4. To check a release by hand: `git verify-tag X.Y.Z` and the range from the previous tag.
5. If anyone deletes a package version from the registry (any member can, including the live pin and the rollback `2.3`), rebuild the tag locally and push it: the build is reproducible, so it gives the same digest.

**Rollback.** Roll forward to the old pin; do not `git revert` the re-pin (it conflicts with later comment edits, and it can bring back the Quay line). Renovate would also re-propose the bad release within the hour, so pause it in the same commit.

1. Edit `cluster/realliance.net/home.yaml`: set `image:` to `git.realliance.net/realliance/realliance.net:2.3@sha256:7405276fcd58b6ade36c76147a23dfaba6eac6287880c14e37160d032961e359`.
2. In the same commit, add a temporary rule to `.renovaterc.json`, **after** the site rule (a later rule wins): `{ "matchManagers": ["kubernetes"], "matchPackageNames": ["git.realliance.net/realliance/realliance.net"], "enabled": false, "description": "TEMPORARY: rolled back from <bad version>; remove once the fix is released." }`. This pauses every site update, so a fixed release waits too. The softer option is `"dependencyDashboardApproval": true` plus `"automerge": false` in the same rule: Renovate still opens PRs, but nothing deploys until Chris approves one.
3. Push. Argo rolls `deploy/home` back; watch it with `kubectl -n realliance-net rollout status deploy/home`.
4. Once the fix ships and you have reviewed it, **remove the temporary rule** in the commit that re-pins to the fixed `X.Y.Z`. A rule left behind silently stops the site from ever updating.

The registry copy of `2.3` has the same digest as Quay's. Quay no longer serves as a source: nothing pulls from it, and the repo stays until Chris deletes it.

**Runbook: the 5Mi trap.** The `home` pods have a 5Mi memory limit (kept on purpose). **Never `kubectl exec` into them:** the exec OOM-kills the pod. Inspect from outside: `kubectl logs`, `kubectl describe`, `curl` against the site. An OOMKilled that you did not cause is a rollback trigger.

**Brand sync.** `scripts/sync-brand.sh --tag vX.Y.Z` pulls a signed `realliance/brand` tag. `brand/uses.txt` lists the assets in use, and `brand/release-signers.asc` plus `.fpr` hold the trusted signers (now Chris). Add a signer by committing both their key and their fingerprint.

**The general runner.**

- Allowlist: one repo-scoped connection per repo, today `realliance/realliance.net` only.
- Add `realliance/brand` later: a new secret, then `forgejo-cli … register --scope realliance/brand --labels general --secret-stdin=-`, the derived connection in the runner config, reseal, push. STOP and ask Chris before the push.
- Rotate: register again with a new secret, reseal, push, then delete the old runner row.
- Never register it for `realliance-ops/forge-bot`. The two runners share nothing.
- NetworkPolicy: none yet. It lands with the realliance.net plan's Part C, after the NetworkPolicy plan's B2.
- Accepted risk: any `realliance` member can push a branch and run a workflow here. That workflow reads `SITE_REGISTRY_TOKEN` and can publish any `realliance` package. Renovate automerges new `X.Y.Z` registry tags, so that member's push also deploys to galaxy. Chris accepted this on 2026-11-05. The fix, if anyone not fully trusted joins `/realliance`: gate the automerge on a provenance check (signed git tag + reproducible rebuild digest).
- Not offered: ephemeral one-job runners, per-job volumes, quotas or a job deadline. The pod is long-lived, and its `HOME` persists between jobs until a restart.
- Retiring it, in this order (the site's publishing and the runner are separate things; a move to another runner class still needs the token and secret):
  1. Change `runs-on: general` in the site's `ci.yaml` first, or jobs wait forever and nothing publishes.
  2. Drop the allowlisted connection and delete its runner row.
  3. Remove `cluster/forgejo-runner-general/`, its Argo wiring, `images/forgejo-runner-general/` and the GitHub image workflow. In the same commit drop `scripts/test_runner_general.py` and its `realliance-net` pre-commit hook entry (the test cannot render without the directory), and the Renovate rule for the runner image.
  4. Argo prunes the namespace and the PVC; the storage class keeps the volume as a Released PV. That delete is an S4 ask, then a manual PV delete.
  5. Remove the sso test `test_forge_runner_general.py`.
  6. Only if the site stops publishing too: delete the repo secret `SITE_REGISTRY_TOKEN`, revoke the `realliance-ci` token and drop its `sso-secrets` entry (this also fails the sso `test_realliance_ci_tokens` and `test_site_registry_token_scope` tests, so update them with it).

**Tokens.** `SITE_REGISTRY_TOKEN` is a repo secret holding a `write:package` token for `realliance-ci`, a local account in team `realliance/ci` (packages only, no repos). Forgejo 15 cannot narrow it: it reaches every `realliance` package. Rotate it when any member leaves (a member can read it through a branch-push workflow), and when it may have leaked. The source is `REALLIANCE_NET_REGISTRY_TOKEN` in `sso-secrets`. A second token, `realliance-forgejo-registry`, is the Forgejo fork's repo secret `REGISTRY_TOKEN` (source `REALLIANCE_FORGEJO_REGISTRY_TOKEN`); it publishes the fork image, which only a hand-reviewed digest pin puts on the live forge. Both rotate together, as in Rotating `realliance-ci`'s tokens, below.

**Rotating `realliance-ci`'s tokens.** The sso test `tests/forge/test_forge_ci.py` expects `realliance-ci` to hold exactly two tokens, each `write:package`, and Forgejo token names are unique per user. Rotate each with the steps below, using its row; the old token goes first, and that publisher is down until the new one is in. Do it when no site or fork image CI run is in flight, and never put a token value in chat or a commit message.

| Token name | Repo and secret | `sso-secrets` key |
|---|---|---|
| `realliance-net-registry` | `realliance/realliance.net`, `SITE_REGISTRY_TOKEN` | `REALLIANCE_NET_REGISTRY_TOKEN` |
| `realliance-forgejo-registry` | `realliance/forgejo`, `REGISTRY_TOKEN` | `REALLIANCE_FORGEJO_REGISTRY_TOKEN` |

1. Delete the old token: set `ACCT=realliance-ci` and `OLD=` the row's token name, then run [the guarded token delete](ci.md#the-guarded-token-delete) in `docs/ci.md`. A database write: STOP and ask Chris first. Every copy stops working now. The API route `DELETE /api/v1/users/realliance-ci/tokens/…` can't do it here: the token routes take only password basic authentication, which this forge turns off.
2. Mint the new one with the same name and scope `write:package` (`forgejo admin user generate-access-token --username realliance-ci --token-name <token name> --scopes write:package --raw`), and pipe it straight into the next two steps.
3. Set the row's repo secret: `PUT /api/v1/repos/<repo>/actions/secrets/<secret>`.
4. Store it in `sso-secrets` under the row's key, then commit that repo.
5. Run the sso token tests (`tests/forge/test_forge_ci.py -k 'realliance_ci_tokens or site_registry_token_scope'`), then re-run that repo's latest CI job from the forge UI and check that it publishes.

**Dependencies.** New pods cannot pull while Forgejo is down. A WAN-IP change breaks node pulls until the router's reflection rules follow (the inherited hairpin limit).

## Fork image

The forge's own image is `realliance/forgejo`'s patch queue on top of upstream 15.0.11: `git.realliance.net/realliance/forgejo`. Plan: `docs/superpowers/plans/2026-11-06-forgejo-fork-image.md`. Research and evidence: `docs/superpowers/specs/2026-11-06-forgejo-fork-image-research.md`.

**The pin.** `cluster/applications/forgejo-release.yaml` sets `image.fullOverride` to `git.realliance.net/realliance/forgejo:v15.0.11-realliance.N@sha256:<amd64 digest>`. The upstream `digest:` line stays below it as the fallback; the chart ignores that line while `fullOverride` has a value. The first release, `v15.0.11-realliance.1`, went live on 2026-11-07. No Renovate manager sees the pin, so Chris reviews each one. `scripts/test_forgejo_image.py` (pre-commit hook `forgejo-image`) checks the pin's form, the fallback line, the keep-warm DaemonSet and the selective sync below. The hook pulls the chart from code.forgejo.org on each run, so an outage there blocks every commit that touches `forgejo-release.yaml`; skip it (`SKIP=forgejo-image`) only with Chris's word.

**Build and release.**

1. Changes reach the fork's `main` as patches through a PR. CI refuses a patch that adds a database migration, so the same-version upstream image always boots on the same database.
2. An Owner pushes a signed tag `v15.0.11-realliance.N` on `main`. The `image` workflow builds on the paid `medium` class and pushes the one tag `v15.0.11-realliance.N` as `realliance-ci`. It never moves a tag.
3. The job prints the pin line. Chris reviews the digest, then the pin goes in as one signed commit. A `workflow_dispatch` build gets only `sha-<7>` and a `dev` version: a pipeline test, never a pin.

**Deploy order.**

1. `ci-dispatch` first: the live dispatcher must carry #3 (blocked jobs) before the forge runs an image with the `blocked` patch.
2. No Actions job may run: every runner reads Idle or Offline under Site administration, Actions, Runners. A runner mid-job loses the forge, and a paid one keeps billing.
3. Push the pin. Argo syncs wave -1 first, the keep-warm DaemonSet, on all three nodes. Only once it reads 3/3 Ready does Forgejo's Recreate start: 1 to 3 minutes of downtime. Watch with `Monitor` (the safe-rollout skill), and watch each keep-warm pod's state, not only the 3/3 count.

**Fork config.** The patches read two app.ini sections that upstream ignores, so the fallback boots with them in place.

- `[realliance] ACCOUNT_URL`: the "Realliance account ↗" link at the top of the user settings menu. Empty hides it.
- `[realliance.ci_usage]`: the Usage tab under Settings, Actions, and the held note on a queued job. `URL` is ci-dispatch's usage API on its ClusterIP Service, port 8081, which ci-dispatch's NetworkPolicy opens to Forgejo's pods only. `TOKEN` comes from Secret `forgejo-ci-usage` through `additionalConfigSources`; rotate it with ci-dispatch's copy (`docs/ci.md`, "The usage token"). An empty `URL` turns the feature off.
- `initPreScript` deletes the empty `templates/user/settings/navbar.tmpl` that the old menu override's `subPath` mount left on `/data`. Forgejo would load that file as an empty menu.

**The keep-warm DaemonSet.** `forgejo-keepwarm` (namespace `forgejo`, Argo sync wave -1, from the chart's `extraDeploy`) runs `{{ include "gitea.image" . }}`, the Deployment's own image with the fallback included, as `/app/gitea/gitea --version && exec sleep 2147483647` on every node.

- Why: with `Recreate` the old pod stops before the node pulls the new image, and the manifest and its token come from Forgejo. A node without the image cached sits in `ImagePullBackOff` with the forge down, and nothing recovers by itself.
- The DaemonSet pulls the image while the old forge still serves it, and it keeps the image in use, so image garbage collection leaves it alone. The cached copies outlive a drain and a deleted package version. A rebuilt node starts empty and pulls again from the running forge.
- It doubles as a start check: a binary that fails `--version` crash-loops in wave -1, the sync waits, and the old forge stays up. `kubectl -n forgejo logs <pod> --previous` shows the error of a crash-looping pod.
- Each pod requests 1m CPU and 8Mi, with a 128Mi limit. `--version` needs about 40Mi of its own, and the page cache it reads counts too: under a 64Mi limit, one pod reached it.

**When the sync waits at wave -1.** Argo touches Forgejo only after the DaemonSet turns healthy. This Argo has no sync timeout, and it never reads a DaemonSet as Degraded, so the operation stays Running with `waiting for healthy state of apps/DaemonSet/forgejo-keepwarm`, and nothing alerts. While it runs, Argo skips auto-sync, so a new commit waits too. First read the keep-warm pods with `kubectl -n forgejo get pods -l app.kubernetes.io/name=forgejo-keepwarm -o wide`. The cause decides the fix.

**A keep-warm pod fails on the new image** (`CrashLoopBackOff`, `OOMKilled`, `Error`, `ErrImagePull` or `ImagePullBackOff`). The gate caught a bad image. **Never sync Forgejo now**, not even the selective sync below: it would roll the failing image into Forgejo's Recreate. Read `kubectl -n forgejo logs <pod> --previous`, then:

1. `git revert` the pin commit, signed, and push. That puts back the image and config Forgejo runs now, because the waiting sync never reached Forgejo.
2. End the waiting operation, from this repo:

```bash
cp /tmp/galaxy-kubeconfig /tmp/galaxy-kubeconfig-argo
export KUBECONFIG=/tmp/galaxy-kubeconfig-argo
kubectl config set-context --current --namespace argo-cd
nix develop --command argocd --core app terminate-op forgejo-release
nix develop --command argocd --core app wait forgejo-release --operation --timeout 120
export KUBECONFIG=/tmp/galaxy-kubeconfig
```

Auto-sync then takes the revert: wave -1 goes back to the old image, and Forgejo doesn't restart.

**A node is `NotReady`**, and on every `Ready` node the keep-warm pod reads `Running` and 1/1 on the new image, with no recent restarts. The image is fine; the sync waits only for the down node, and it carries on once the node returns. To roll Forgejo before that, end the operation and sync Forgejo's Deployment together with the chart's three Secrets, which skips the wave. Forgejo reads its config from those Secrets when it starts, and only a change to the Deployment restarts it, so a Deployment synced alone would run on the old config with nothing to restart it later.

```bash
cp /tmp/galaxy-kubeconfig /tmp/galaxy-kubeconfig-argo
export KUBECONFIG=/tmp/galaxy-kubeconfig-argo
kubectl config set-context --current --namespace argo-cd
nix develop --command argocd --core app terminate-op forgejo-release
nix develop --command argocd --core app wait forgejo-release --operation --timeout 120
nix develop --command argocd --core app sync forgejo-release --resource :Secret:forgejo --resource :Secret:forgejo-inline-config --resource :Secret:forgejo-init --resource apps:Deployment:forgejo
export KUBECONFIG=/tmp/galaxy-kubeconfig
```

- `terminate-op` only asks for the stop. `app wait --operation` returns once the operation has ended, so the sync doesn't fail on an operation still in progress.
- After the selective sync, self-heal starts a sync of the rest, and it waits at wave -1 again until the node returns. That wait is normal.
- Once the node is back, `forgejo-release` must read Synced. If it doesn't, `argocd --core app get forgejo-release`, run with the kubeconfig from above, lists what differs.

**Fallback to upstream.** Trigger: Forgejo's pod is in `ErrImagePull`, `ImagePullBackOff` or `CrashLoopBackOff` on the fork image.

1. Delete the `fullOverride:` line in `cluster/applications/forgejo-release.yaml`, commit, and push to GitHub `main`. The chart then renders `code.forgejo.org/forgejo/forgejo:15.0.11-rootless@sha256:3cd6bc2b…`. Argo reads GitHub, and both the chart and that image come from code.forgejo.org, so this works with the forge down. `kubectl set image` doesn't: self-heal reverts it.
2. Argo rolls the DaemonSet to upstream first, then Forgejo. If the sync waits at wave -1, see When the sync waits at wave -1, but never revert this fallback commit: that re-applies the broken fork. If a keep-warm pod can't pull upstream, use the node case there, or wait for code.forgejo.org. Watch with `Monitor`.
3. `gitea migrate` is a no-op, and the data stays intact. Lost while on upstream: the jj UI and the PR-round views, the `blocked` status (ci-dispatch falls back to guessing), the Usage tab and the account link (to keep the link, restore the old `navbar.tmpl` override and its mount from git history, commit 4af18b65). Round refs stay hidden from fetches, because `transfer.hideRefs` persists in Forgejo's git config file on `/data`. They still count in commit counts and graphs, and a mirror-mode push mirror could push `refs/realliance/*`.
4. Roll forward only with the keep-warm DaemonSet in place, or while Forgejo is up with the image cached on every node.

**Before a drain of Forgejo's node** (a Talos hop, a rebuild): check that the other nodes hold the image. The `talos-upgrade` skill carries the check.

## Template overrides (custom/)

Forgejo has no plugin API. A UI change is a patch in the fork image (Fork image), or a file under
`cluster/forgejo/custom/templates/`, shipped as ConfigMap `forgejo-custom-templates` and mounted into
`$GITEA_CUSTOM/templates/` (see `extraContainerVolumeMounts` in `forgejo-release.yaml`).

| Override | Upstream file | Change |
|---|---|---|
| `custom-header.tmpl` | `templates/custom/header.tmpl`, an empty `<head>` hook | loads `realliance-fonts.css` (Typography, below) |

**Never copy a whole upstream template here.** A copy shadows every patch to that file: the old
user settings menu copy would have hidden the fork's Usage tab. Its "Realliance account ↗" link moved
into the patch queue on 2026-11-07. `tests/forge/test_forge_signin.py::test_settings_link_to_realliance_account`
in realliance-sso catches a lost link. Removing a `subPath` mount leaves its empty mount point on `/data`,
which Forgejo then loads as an empty template: delete it in `initPreScript`, as for `navbar.tmpl`.
Forgejo reads templates at start, so after an edit bump `realliance/templates-rev`.

## Typography (fonts)

Body Atkinson Hyperlegible, headings Fredoka, code Noto Sans Mono; self-hosted woff2 (OFL), never a CDN.
One stylesheet (`realliance-fonts.css`) is loaded on every page by the `custom/header.tmpl` override, so
it applies to every theme. Sources, pins, sizes and the rebuild script: `cluster/forgejo/custom/FONTS.md`.
Fredoka is on real headings only (`:where(h1,h2,h3,h4):not(.ui.header)`, `.markup h1-h6`, the repo title);
Fomantic `.ui.header` section titles stay Atkinson. The same sheet sets corner radius variant B
(`--border-radius` 8px, `--border-radius-medium` 12px, plus the Fomantic rules that hard-code 4.57px; avatars stay 4px,
pills and counters are untouched). Test: `scripts/test_forge_fonts.py` (pre-commit hook `forge-fonts`).
Deploying it restarts Forgejo once (new volume and mounts).

**Deploy order with the SSO theme (never both in one window).** The forge refuses to start while SSO discovery
is down, and the SSO theme change restarts Keycloak. So: (1) merge realliance/sso (login + account themes) first,
wait for ansible-pull and the Keycloak restart (about 5-7 min), confirm a real sign-in and that
`pytest tests/realm/test_login_theme.py` passes in that repo; (2) only then merge this change. Not while the pages
acceptance suite or forge-bot tests run. Merge-time notes: sync the `forgejo` Application (ConfigMaps) before
`forgejo-release` so the new mounts exist when the pod starts; `custom/header.tmpl` is a subPath mount, so editing
it needs a `realliance/templates-rev` bump (CSS and fonts are directory mounts and update live in about a minute);
`/assets/*` is cached up to 6 h and font files carry no version, so after a later CSS edit hard-reload or rename the
file.

**On a Forgejo upgrade (15 to 17):** re-check that the `custom/header.tmpl` hook still exists, that
`--fonts-proportional`, `--fonts-monospace`, `--fonts-regular`, `--border-radius`, `--border-radius-medium` and
`AssetVersion` still exist, and re-screenshot (Fomantic radius rules are hard-coded and may move).

## 19 LTS hop carry list

Decision 2026-11-04: stay on 15 LTS, hop once to 19 LTS about May 2027
(`docs/superpowers/specs/2026-11-04-forgejo-17-upgrade-design.md`). Renovate never merges the
chart by itself (`.renovaterc.json`, guarded by `scripts/test_forgejo_renovate.py`), because a bump restarts Forgejo.
For the hop:

- Pin `[service].ADD_MEMBERS_BY_INVITATIONS=false` before the hop, not now (15 lacks the setting, and it restarts Forgejo).
- Port the Catppuccin oklch shim.
- Rebase the fork's patch queue onto 19 (the account link and the Usage tab live in `templates/user/settings/navbar.tmpl` patches now, not a copy here).
- The administrator API can create true system hooks with `config.is_system_webhook`.
- Add a Renovate custom manager for `image.tag`/`image.digest`: no manager sees them inside `valuesObject`.
- `INTERNAL_TOKEN` no longer exists in 18 and later.
