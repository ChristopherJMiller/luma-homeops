# Realliance Forge — design

Date: 2026-11-02 · Status: approved by Chris 2026-11-02 · Project 2 of 3 (SSO → **forge** → CI)

## Why

Realliance (Chris's friend group and collaborators) wants its own code forge: new projects live
there first, GitHub becomes a mirror (résumé visibility, off-site copy) rather than the home, and
membership comes from Realliance SSO (`sso.realliance.net`, project 1, live). Chris's own personal
projects move here too (his namespace on `git.realliance.net` replaces the earlier
`git.chrismiller.xyz` idea), including the LFS-heavy game-sources monorepo later. Existing GitHub
repos stay on GitHub; imports are rare and deliberate.

## Goals

1. **Forgejo at `https://git.realliance.net`**, sign-in only through Realliance SSO.
2. **Access and teams follow SSO**: who may use the forge and who is on which team is decided in
   `realliance/sso-people` + `realliance/forge`, never by hand in Forgejo — and removals take
   effect within minutes, for the web UI, SSH keys and tokens alike.
3. **Structure is code, repos are free**: orgs, teams, permissions and org-wide policies live in a
   Terraform inventory; anyone allowed creates repos instantly (web UI, `fj`/forgejo-cli, API).
4. **Every repo can be mirrored to GitHub with matching visibility**, self-serve (phase 2c).
5. Recoverable by either operator from off-site backups.

## Non-goals (this spec)

- CI runners (project 3).
- Migrating the ~179 existing GitHub repos.
- Federation; forking or patching Forgejo.

## Anti-goals

- A second identity system: no Forgejo-local passwords for people (one break-glass local admin).
- Hidden-project names (orgs, groups, team maps) in any public repo or public config.
- A repo deletable by an ordinary member; a GitHub mirror that overwrites anything without opt-in.
- Access that *silently* outlives a person's removal: until forge-sync exists, offboarding has a
  documented second step (prohibit login in Forgejo) and the gap is named, not hidden.
- Heavy write bursts on the SMR-backed Ceph pool from LFS or nightly full dumps of large data.

## Decided (by Chris)

- **Forgejo 15 LTS** (17 is the short-lived track; 17.0.0 shipped with a push/merge regression).
- **Hosted on the galaxy cluster** (Argo CD, Ceph, B2 backups). A forge outage is tolerable —
  everyone has clones and GitHub mirrors — while the SSO stays off-site.
- **IaC split:** `realliance/forge` (private) Terraform owns orgs, org visibility, teams +
  permissions, org-wide policies and explicitly declared critical repos (`prevent_destroy`);
  everyday repos are created freely; ordinary team permissions exclude repo deletion.
- **forge-bot**, one companion service (phase 2c, own spec): self-serve GitHub mirroring at
  `mirror.git.realliance.net` (users connect GitHub through a GitHub App; the bot pushes with
  short-lived tokens and creates GitHub repos with matching visibility; org owners set org
  targets), enforcement of org defaults on new repos, and a nightly `repos.yaml` snapshot into
  `realliance/forge`. First repo built in Forgejo; first app behind Realliance SSO.
- **Customisation** through Forgejo's `custom/` templates + CSS and companion services only.
- **CLI:** `fj` (forgejo-cli).
- **Git over SSH on WAN port 22**: `git@git.realliance.net:org/repo.git` works everywhere.
- **Day-one org:** `realliance` (public, GitHub target `github.com/realliance`); everyone has a
  personal namespace. `forgejo-admins`: Chris.
- **forge-sync deferred**: teams and admin status sync at sign-in (Forgejo's own OIDC group
  mapping); offboarding is a manual two-step runbook until forge-sync is built.
- **`realliance/forge` lives on Forgejo itself** (`git.realliance.net/realliance/forge`, private,
  mirrored privately to GitHub); the `realliance` org, the repo and the admin token are bootstrapped
  by hand at 2a go-live, then adopted by the inventory with `prevent_destroy`.
- **Connor is an app co-admin** (in `forgejo-admins`, edits `realliance/forge` by PR); galaxy
  infrastructure stays Chris's. Backups land in the **Realliance B2 account** so either operator
  can restore onto a new host.
- **Chris's personal forge folds in** (his namespace here; LFS sized for hundreds of GiB on B2).

## Phases

| Phase | Delivers | Spec |
|-------|----------|------|
| **2a** | Forgejo on galaxy + Realliance SSO + backups + SSH | this document |
| **2b** | `realliance/forge` inventory (orgs, teams, policies, critical repos) | this document |
| 2c | forge-bot (mirroring, defaults, snapshots) | its own spec, after 2b |
| 2d | Realliance theming and integration panels | its own spec |

## Design — 2a: Forgejo on galaxy

*(my calls unless marked)*

- **Deploy:** official `forgejo-helm` chart (pinned, appVersion 15.x LTS) as an Argo Application,
  namespace `forgejo`, files under `luma-homeops/cluster/forgejo/`. One replica, `Recreate`.
- **Database:** `postgres-operator` cluster `acid-forgejo` (Postgres 16, 2 instances, 10Gi,
  `enableLogicalBackup: true`). The namespace's `logical-backup-s3` secret overrides the
  operator-wide bucket so the nightly dump goes to the **Realliance** B2 account (operator v1.11
  applies the namespace secret's `LOGICAL_BACKUP_S3_*` after its own).
- **Storage:** repositories on one RWO `rook-ceph-block` PVC (start 50Gi; Forgejo cannot keep repos
  in object storage). **LFS, packages and attachments in object storage** — a new
  `realliance-forge-objects` bucket in the Realliance B2 account (S3 API, created in
  `realliance/sso` `b2/` with a scoped key), so large binaries never hit the SMR pool and are
  protected by B2 versioning. Queue `level`, cache `twoqueue`, sessions in the DB — no Redis.
- **HTTP:** one Ingress `git.realliance.net` on Traefik; the existing `*.realliance.net` wildcard
  certificate and DNS cover it (the galaxy edge pattern).
- **SSH:** a LoadBalancer Service on MetalLB `192.168.0.6` port 22; `SSH_DOMAIN=git.realliance.net`,
  `SSH_PORT=22`. Router changes in `router/ansible/group_vars/vyos_routers.yml`, applied with
  commit-confirm (CLAUDE.md S4/S8 — Chris approves at execution): (1) `service ssh listen-address
  192.168.0.1` — VyOS SSH stays reachable from the LAN (rowlett: `ssh chris@192.168.0.1`) and stops
  listening on the WAN; (2) a WAN forward like rules 200/300: in `eth0`, TCP 22 → `192.168.0.6:22`;
  (3) a reflection rule like 210/310: in `eth2`, **destination address `152.44.247.88`** (the WAN
  IP), TCP 22 → `192.168.0.6:22`. Pinning the destination means only SSH aimed at the WAN IP is
  redirected — `ssh git@github.com`, router SSH and tailnet SSH (Tailscale runs on the hosts; VyOS
  has no tailnet interface) are untouched. Inherited limitation: the WAN IP is DHCP and hardcoded in
  the reflection rules (as for 80/443); `cloudflareddns` updates DNS but not these rules.
- **Memory:** Forgejo ≈ 300–500 MiB + two Postgres pods ≈ 200 MiB each. galaxy is ~84 % requested, so the deploy includes the rebalance from
  `known_middle_mem_requests` before Forgejo's requests land.
- **Backups:** Postgres as above; repositories by a nightly CronJob that `git bundle`s every repo
  (or a `forgejo dump --skip-lfs-data --skip-attachment-data --skip-package-data`, streamed) into an
  object-locked `realliance-forge-backups` bucket — small, because LFS/packages/attachments are
  already in B2. Restore drill in Success.
- **Email:** Resend (already verified for realliance.net), a **separate** sending-only key
  restricted to realliance.net, sender `git@realliance.net`; notifications on.
- **Config** (`app.ini` via chart values; secrets as SealedSecrets): `[service]
  DISABLE_REGISTRATION=false`, `ALLOW_ONLY_EXTERNAL_REGISTRATION=true`, `ENABLE_INTERNAL_SIGNIN=false`;
  `[oauth2_client] ENABLE_AUTO_REGISTRATION=true`, `ACCOUNT_LINKING=disabled`, `USERNAME=nickname`
  (falls back to `preferred_username`); `[session] SESSION_LIFE_TIME=604800` (7 days);
  `[repository] DEFAULT_PRIVATE=private`; `[service.explore]` hides the user list from anonymous
  visitors. Public orgs/repos are visible to anonymous visitors.

### Sign-in and access

- **realm side** (`realliance/sso` `realm/apps.tf`): `module "forgejo"` (CONFIDENTIAL,
  `extra_default_scopes = ["groups"]`, redirect
  `https://git.realliance.net/user/oauth2/realliance/callback`). Access group `/apps/forgejo`:
  only its members can sign in (default deny), so Forgejo auto-registration is safe. Public group
  `forgejo-admins` (realm `groups.tf`), membership in sso-people.
- **Forgejo side:** one OAuth2 auth source `realliance` (OIDC discovery URL
  `https://sso.realliance.net/realms/realliance/.well-known/openid-configuration`), group claim
  `groups`, `--admin-group forgejo-admins`, `--group-team-map` with `--group-team-map-removal`
  for **non-Owners** teams only (never map an Owners team: Forgejo refuses to remove a last owner
  and the sign-in fails). Admin status and teams therefore sync **at each sign-in** (Forgejo 15
  re-reads groups only then).
- **Team map:** with only the public `realliance` org, the map is ordinary chart config in
  luma-homeops. **Before the first hidden org**, the map must move out of public config (a
  SealedSecret applied by a PostSync Job, or the deferred forge-sync) — recorded as a gate in the
  runbook.
- **Removing someone (manual for now — decided by Chris; forge-sync deferred):** sign-in sync never
  runs for a person who can no longer sign in, and their SSH keys and access tokens keep working.
  So the runbook's offboarding is two steps: `active: false` in sso-people (or removal from
  `/apps/forgejo`) **and** "prohibit login" on their Forgejo account (which blocks web, API tokens
  and SSH). Known gap, accepted: access lingers until the second step is done. forge-sync
  (read-only SSO service account + periodic reconciliation) is the planned fix.
- **Break-glass:** one local admin `forgejo-admin` (password in `sso-secrets`), used via
  `forgejo admin user` on the pod (Chris) — and Connor, as a Forgejo admin, can act in the UI.

## Design — 2b: the `realliance/forge` inventory

- **Repo:** `realliance/forge`, **private** (hidden orgs, team map). Terraform with the
  `svalabs/forgejo` provider, state in the Realliance B2 tfstate bucket (`forge/terraform.tfstate`),
  hosted on Forgejo itself (`git.realliance.net/realliance/forge`).
- **Owns:** orgs (name, visibility, description), teams per org (`members`: write + create repos,
  no admin; `maintainers`: admin, which in Forgejo 15 still excludes repo deletion), each org's
  Owners team (Chris/Connor via `forgejo-admins` policy), declared critical repos
  (`prevent_destroy`, branch protection), and org-wide policy declarations that forge-bot enforces.
- **Applied by:** an operator from a laptop at first; Forgejo Actions in-cluster once project 3
  lands. Same guardrails as the SSO: signed commits, PR review, no delete authority (archive
  instead).
- **Personal repos** (`<user>/<repo>`) are not in the inventory; their owners manage them and opt
  into mirroring via forge-bot.

## Success looks like

1. Chris signs in at `git.realliance.net` with Realliance SSO (password + MFA), an account is
   created on the spot, and he is a Forgejo admin through `forgejo-admins`.
2. A Realliance member not in `/apps/forgejo` is refused at SSO, never reaching Forgejo.
3. Offboarding per the runbook (sso-people `active: false` + Forgejo "prohibit login") blocks their
   web session, API tokens **and SSH pushes**; reversing both restores access.
4. Changing someone's groups in sso-people moves them between (non-Owners) teams at their next
   sign-in.
5. A public org and repo are visible to a logged-out visitor; a private org is invisible to
   non-members, and its name appears nowhere public.
6. A member creates a repo in `realliance` in seconds and cannot delete it.
7. `git clone git@git.realliance.net:realliance/<repo>.git` works from the internet and the LAN;
   the router's own SSH answers only on the LAN.
8. An LFS push lands in `realliance-forge-objects`, not on the cluster volume.
9. Restore drill: a fresh namespace restored from the B2 backups serves the same repos and LFS.

## Left to the builder

Chart version pin; PVC sizes beyond the starting point; Ingress annotations; bundle-vs-dump for repo backups; `custom/`
placeholders for 2d; `svalabs/forgejo` resource layout.
