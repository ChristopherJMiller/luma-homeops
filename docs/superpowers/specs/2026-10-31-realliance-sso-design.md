# Realliance SSO — design

Date: 2026-10-31 · Status: approved by Chris 2026-10-31 · Project 1 of 3 (SSO → forge → CI)

## Why

Realliance is Chris's friend group / collaborator community (the realmic and realliance
teammates, plus people who want to build projects with him). It is about to get its own code
forge at `git.realliance.net` (project 2), and with it a need for a shared notion of *who is in
Realliance and which groups they belong to*.

Today the only identity layer is Dex (`dex.chrismiller.xyz`), which brokers personal Google and
Microsoft accounts for the family realm. Dex has no accounts of its own and cannot emit groups
for personal accounts, so it cannot carry Realliance membership.

Chris wants a **neutral** identity: a Realliance account that belongs to Realliance, not to Google,
GitHub, Discord or Microsoft. Those providers may be linked to a Realliance account as optional
sign-in methods; none of them may ever be required. Any trusted member should be able to put an app they run behind Realliance SSO and decide who
gets into it. Chris's friend Connor co-runs the SSO itself as the second operator.

## Goals

1. Every person has a **Realliance account**, signing in with a passkey (primary) or a password
   plus second factor (fallback). Self-service recovery (recovery codes, email) comes first; a
   person who has lost everything is restored by a realm admin.
2. Google, GitHub, Microsoft and Discord are **optional linked logins** that a user can add or
   remove on their own account.
3. **Invite-only.** No open sign-up. Trusted members can bring new people into the groups they
   administer.
4. **Delegated administration.** A trusted member manages who is in *their* groups and who
   can use *their* apps, in the UI, without being a realm admin.
5. **Apps are registered as code.** A new OIDC client is a pull request against the SSO repo.
6. Forgejo receives a flat `groups` array claim it can map to teams.
7. Runs **off the homelab**: a homelab outage, node maintenance or the pending Talos/k8s upgrade
   series never blocks Realliance sign-in.
8. **Rebuildable anywhere** from git plus the latest backup in about an hour, by either of **two
   operators**.

## Non-goals

- Multi-site / replicated HA. Researched and rejected: Keycloak's multi-site mode needs synchronous
  DB replication over a low-latency link; the products that replicate easily (Kanidm, Rauthy) fail
  the federation and delegation requirements. An outage only blocks *new* sign-ins — Forgejo
  sessions last up to 31 days, oauth2-proxy's a week.
- Self-service (dynamic) client registration. Disabled; clients arrive by PR.
- Replacing Dex or touching the family realm. Family apps (Immich, gramps, oauth2-proxy tiers)
  stay on Dex.
- SCIM provisioning to apps (available in Keycloak 26.8; revisit when an app needs it).

## Anti-goals (would be failure even if it "works")

- Any big provider becoming load-bearing for a Realliance account's *admin* power.
- Config that only exists in a UI (the reason authentik was removed in 2026-09) — with one
  deliberate exception, hidden-project groups (see below).
- A social login silently attaching itself to an existing account (account takeover by email
  match — the exact class of Zitadel's 2026-09 critical advisory).
- A delegated admin able to reach realm-admin powers, or to take over another member's account.
- A hidden project's name visible to anyone outside that project (except realm admins).
- Friends' sign-in depending on Chris's house being online, or on Chris personally being around.

## Decided (by Chris)

- **Keycloak** (26.8.x line). Chosen over Zitadel because Zitadel cannot yet emit a flat groups
  claim Forgejo can read (fix merged upstream, unreleased) and had two critical 2026 advisories in
  exactly the external-IdP-linking and passkey-enrollment paths this design uses. Keycloak's one
  delegation gap — a non-admin cannot *create* clients — is fine because clients come by PR.
- **Hosting: Chris's existing CrownCloud VPS** (`OVZ-C-3072`, Los Angeles, $2.50/mo, in service
  since 2014; panel label `vps.realliance.net`). It is wipeable; today it runs only a default
  Apache page.
- **Configuration management: Ansible** (Chris already runs Ansible for the VyOS router). NixOS is
  not possible on this box (see Host).
- **Not on the Azure credits.** The subscription is a Visual Studio (MSDN) credit with a spending
  cap; terms are dev/test only, and a capped month stops (or makes read-only) everything in it.
  Those credits are reserved for ephemeral CI (project 3). Nothing in this design depends on Azure.
- **Backups on B2**, matching `docs/backups.md`.
- **Separate from the family realm.**
- Registering an app via PR is acceptable for app owners.
- **Hidden-project names are hidden from everyone outside that project** (realm admins excepted),
  matching Forgejo private orgs.
- **Two operators: Chris and Connor.** Both are realm admins with write access to the repo; either
  can deploy and restore without the other, and each reviews the other's changes.
- **Account recovery for a fully locked-out person is realm admins only.** Group admins cannot
  reset credentials.
- **A linked login is a full sign-in for ordinary use**; anything with admin power (realm or
  delegated admin console) additionally requires a Realliance passkey or OTP.

## Design

### Shape

- Hostname **`sso.realliance.net`** *(my call)*. Issuer:
  `https://sso.realliance.net/realms/realliance`. The hostname is permanent — passkeys are bound to
  it, so a rename means every user re-enrolls.
- One realm, **`realliance`**, for all people and apps. The `master` realm holds only the two
  operators' break-glass admin accounts and is never used by apps.
- **Realm admins** = the two operators.

### Host

Measured on the box (2026-10-31):

| | |
|-|-|
| Virtualization | **OpenVZ 7** container (ploop disk, cgroup **v1**; `uname` reports a virtualized `5.4.0`; the real host kernel is the OpenVZ 7 3.10 line — inferred) |
| CPU / RAM / swap | 7 vCPU (shared) / **3 GB** / 2 GB vswap |
| Disk | 50 GB (15 GB used by the current install) |
| OS | **AlmaLinux 9.8 reinstalled 2026-10-31** (old Ubuntu 20.04 install wiped): systemd 252, cgroup v1 confirmed, `java-21-openjdk-headless` 21.0.13 and PostgreSQL module streams 15/16/18 available. Template extras to remove: `httpd` on :80, `sendmail` on localhost:25, `cockpit` in firewalld. The first plan task installs the operators' SSH keys, disables password auth, and rotates the root password. |
| Bandwidth | 4 TB/mo |

What that means:

- **A container, not a VM**: no own kernel or bootloader, so no NixOS. (Docker *has* run here —
  the old install ran game-server containers — but it is not needed.) Keycloak runs as a **plain
  systemd service** from the official release archive (pinned version + checksum) on the distro's
  Java 21, with the distro's PostgreSQL alongside: fewer moving parts in 3 GB than a container
  runtime.
- **OS: AlmaLinux 9** *(my call)*, installed from the provider's template. The panel offers Ubuntu
  22.04, Debian 11, AlmaLinux 8/9 and Rocky 8. The host's cgroup v1 caps the guest at
  **systemd ≤ 255** (systemd 256+ dropped cgroup v1), and AlmaLinux 9 (systemd 252) is supported
  until **2032-05**, the longest runway on offer with no release upgrade. Java 21
  (`java-21-openjdk-headless`) and PostgreSQL 16 (module stream) come from AppStream. Rejected:
  Debian 11 (already end of life); Ubuntu 22.04 (standard support ends 2027-04, and stretching it
  needs an Ubuntu Pro token tied to one person's account, or an in-container upgrade to 24.04 that
  the provider does not offer); AlmaLinux/Rocky 8 (2029).
- **Runway** is now bounded by CrownCloud's OpenVZ 7 host platform rather than the guest OS. If
  CrownCloud retires that platform, the move is a restore elsewhere (see recovery). Record a
  yearly runbook check.
- **Memory budget** for 3 GB: Keycloak heap ~768 MB–1 GB (tuned for ~30 users; official baseline
  1.25 GB assumes 10k cached sessions), PostgreSQL ~256–384 MB, OS + proxy the rest. Java reads the
  cgroup v1 memory limit correctly.
- **Kernel patching is CrownCloud's.** A shared-kernel host means trusting the provider to patch;
  accepted at this scale.
- **Provider API:** this box lives on CrownCloud's legacy panel; the CrownPanel API
  (`api.crownpanel.com`) does not list it, and in any case covers only info/power/snapshots — no
  create, reinstall or rDNS. No Terraform provider; the reinstall is a one-time manual step.
- Firewall (firewalld or nftables, whatever the container permits): 22 (key-only, both operators'
  ed25519 keys; password auth off), 80, 443.

### Repository and deploys *(my calls)*

A **public** repo, **`realliance/sso` on GitHub — permanently** (both decided by Chris). The
forge signs people in *through* this SSO, so the SSO's source of truth must not live on the
forge: a broken SSO or a down homelab must never block merging its own fix (the same reason
`luma-homeops` stays on GitHub for Argo). Once Forgejo exists it may keep a **pull mirror** of this
repo as a backup copy. It is *not* part of `luma-homeops`, which is Chris's homelab. This
design stays in `luma-homeops/docs/superpowers/specs/`; the `realliance/sso` README links to it
(one copy, no drift).

Public is safe here because the design already keeps the sensitive parts out of git, and it buys
features GitHub Free withholds from private repos: rulesets (protected `main`, required review,
required signed commits), deployment environments with required reviewers, and unlimited Actions
minutes. The operators work in branches and PRs inside the repo; any other contributor uses the
normal open-source fork-and-PR route. `ansible-pull` needs no
credential to fetch.

| Path | What | Applied by |
|------|------|-----------|
| `host/` | Ansible: OS hardening, Java, PostgreSQL, Keycloak service + build, reverse proxy + TLS (ACME), backups, monitoring agent | **`ansible-pull` on the box** (decided by Chris): a systemd timer fetches `main` from GitHub, verifies the head commit's signature, and applies it. No root SSH key or host secret ever sits in GitHub. Either operator can also run the playbook from a laptop. |
| `realm/` | OpenTofu, official `keycloak/keycloak` provider: realm settings, auth flows, identity providers, client scopes, clients, client roles, non-hidden groups, admin groups, fine-grained admin permissions | **GitHub Actions**: `apply` on push to `main` through a `production` environment whose required reviewers are the two operators, authenticating to `https://sso.realliance.net` with a dedicated Keycloak service-account client. `plan` runs on PRs from branches in the repo (same-repo PRs receive secrets); fork PRs get no secrets, so the reviewing operator reads the HCL or plans locally. Workflows use `pull_request`, never `pull_request_target`. |
| `dns/` | OpenTofu, Cloudflare provider with a token scoped to the `realliance.net` zone: **only** the `sso` record | Either operator from a laptop (changes only during a restore) |

- **No secrets in git, not even ciphertext** *(my call)*. Public ciphertext is forever: one leaked
  key later decrypts every historical secret in every fork. Instead:
  - `realm/` secrets (Keycloak service-account secret, Google/GitHub/Microsoft/Discord OAuth app
    secrets, B2 state credentials) are **GitHub Actions secrets on the `production` environment**,
    readable only by approved deploy runs.
  - `host/` secrets (DB password, SMTP credential, B2 backup key, Keycloak bootstrap admin) live
    in a **root-only file on the box**, placed once by an operator from `realliance/sso-secrets` and
    re-placed on restore. The playbook reads it and fails clearly if it is missing.
- **Ruleset on `main`:** pull request required, one approving review from an operator, signed
  commits required, no force-push or deletion. Write access = Chris and Connor.
- **`ansible-pull` signature check:** trust both operators' signing keys **and** GitHub's web-flow
  key (PR merges made in the GitHub UI are signed by GitHub). The ruleset is what guarantees an
  operator approved them. *Builder:* if trusting web-flow is unacceptable, operators instead land
  PRs as locally signed commits via a ruleset bypass — pick one and document it.
- **Hidden projects never touch this repo** — not their groups, and not clients for their apps.
  A hidden project's client is created in the console by a realm admin, like its group.
- **Why Ansible, not Nix, on this host:** NixOS needs its own kernel, which a container lacks.
  Nix-on-another-distro (numtide `system-manager`) supports only NixOS, Ubuntu and Debian —
  AlmaLinux is "unsupported and untested" — and would layer a second package manager over an
  imperative base, losing most of what makes NixOS worth it. If NixOS ever becomes a priority, the
  honest route is a KVM VPS, not Nix inside this container.
- **OpenTofu state on B2** via the S3 backend (custom endpoint, native lockfile locking) — not the
  homelab's `azurerm` backend, so Realliance never depends on the capped Azure credit or on Chris's
  Azure login.
- The `sso` record lives here, not in `luma-homeops/cloudflare/dns/`, so the second operator can
  repoint it during a restore. `luma-homeops` must not also declare it.

**What is code and what is not:**

- *Code:* clients, client roles, scopes, auth flows, non-hidden groups (e.g. `/realmic`,
  `/realliance`), per-app access groups, admin groups **and their membership** (granting admin
  power is a reviewable change; Realliance usernames being public is accepted), and every
  delegation policy that does not name a hidden project.
- *Console only:* ordinary membership of groups; **hidden-project groups, their apps' clients, and
  any delegation over them**, created by realm admins so their names never reach the public repo.
  This is the one deliberate exception to "everything as code". *(Builder: give the realm layer a
  way to ignore console-managed groups so `tofu plan` stays clean.)*

### Sign-in

- **Browser flow:** passkey first (conditional UI), with username + password + OTP as fallback.
  Recovery codes enabled. Email verification required.
- **Linked logins:** Google, GitHub, Microsoft (built-in brokers) and Discord (generic OAuth2
  broker). Each needs an OAuth app registered under a Realliance-controlled account; credentials
  reach the realm layer as GitHub Actions environment secrets. A linked login is a complete sign-in for
  ordinary apps.
- **Admin step-up:** the admin console client (`security-admin-console`) gets a client flow
  override requiring a Realliance passkey or OTP, so a hijacked Google account cannot exercise
  realm or delegated admin power.
- **First-broker-login flow (critical):** an external identity may only attach to an account after
  the user proves they own that account with its own credentials. It must never create an account
  (invite-only) and never auto-link on email match. Users link providers from the account console
  while already signed in.
- **Default deny per client** *(my call)*: every client gets a flow/policy that refuses a user who
  holds none of that client's roles. A Realliance account alone gets you into nothing. The realm
  module applies this to every client automatically.
- **Recovery:** self-service via recovery codes and email reset. Full loss → a realm admin
  verifies the person out of band and issues a set-up link.
- **Email** *(my call)*: a transactional SMTP service with a sending credential scoped to
  `realliance.net` (builder picks; needs SMTP and a free tier well above Keycloak's volume), with
  its DKIM/SPF records added to the `realliance.net` zone alongside Fastmail's. Not Chris's
  personal Fastmail account, whose app password could send as any of his identities if the VPS
  were compromised.

### Groups, roles and claims

- **Community groups** (`/realmic`, `/realliance`, …) model membership; **hidden-project groups**
  are console-only (above).
- **Per-app access groups** *(my call)*: each app gets a group (e.g. `/apps/<app>`) administered by
  the app's owner and mapped in code to the app's client role. The owner grants access to the app by
  adding people to that group. Mapping a shared community group to an app (e.g. "all of
  `/realliance` can use it") is a realm-admin change by PR.
- **Admin groups** (e.g. `/admins/<group>`) hold delegated admin power; policies target the admin
  group, never a username.
- The **`groups` claim** (Group Membership mapper, full path off, flat array) lives in a dedicated
  client scope assigned **only to clients that need it**, starting with Forgejo. Other apps see
  only their own client roles, so hidden-project names never reach them.

### Delegation (fine-grained admin permissions v2)

- **Group admins** get `view` + `manage-membership` on the groups they administer: add and remove
  existing realm users. They do **not** get `manage-members` (which would let them reset
  credentials or change emails of any member, and through that reach the member's other groups).
- **Inviting new people** without `manage-members`: the builder must verify on 26.8 how a group
  admin can bring in a new person without gaining power over existing users. Keycloak
  Organizations invitations (26.5+) are the first candidate. If no clean path exists, new accounts
  are created by either realm admin on request (still not dependent on Chris alone).
- **App owners** get `view` + `map-roles` on their client, plus `manage-membership` on the app's
  access group.
- Keycloak had a group-admin → realm-admin escalation advisory in 2026-06. Stay on a patched 26.8.x
  and include escalation attempts in acceptance testing.

### Onboarding an app (the app-owner flow)

The owner may be an operator (Chris, Connor) or any other trusted member.

1. The owner opens a PR (a branch for operators, a fork for anyone else) adding his client (redirect URIs, client roles), its
   access group, and himself to that group's admin group.
2. An operator reviews and merges it; the `production` environment asks an operator to approve
   the deploy, and GitHub Actions applies it.
3. The owner receives the client secret out of band.
4. The owner adds people to his app's access group in the admin console (passkey/OTP required).
   Nobody else needs to be involved again.

### Operators

- Both operators: write access to `realliance/sso` and approver on its `production` environment,
  commit-signing key trusted by `ansible-pull`, ed25519 SSH key on the box, master-realm
  break-glass admin, B2 restore credentials, the `realliance.net`-scoped Cloudflare token.
- **Break-glass store: a private companion repo, `realliance/sso-secrets`, encrypted with
  git-crypt** and unlocked by Chris's and Connor's GPG keys (decided by Chris). It holds what must
  survive either person: master-realm admin passwords, the host secrets file, the B2 and Cloudflare
  tokens, and the values of the GitHub environment secrets. Private so the ciphertext of the SSO's
  master keys is never public; it runs no Actions, so GitHub Free's private-repo limits do not
  matter. A restore is: clone both repos, unlock `sso-secrets`, follow the runbook.
- The CrownCloud account and billing stay with Chris; if they lapse, the restore path is the
  fallback.

### Backups, monitoring, recovery

- Nightly `pg_dump` to a new B2 bucket using a no-delete application key, with local retention,
  following `docs/backups.md`'s tiers.
- Uptime: an external HTTP check on `/realms/realliance/.well-known/openid-configuration`, plus a
  blackbox probe from galaxy's Prometheus.
- **Go-live gate** *(my call)*: before friends depend on it, the rebuilt box shows ≥99.5% uptime
  over 14 days with no single outage over 1 hour. Otherwise restore it onto another provider
  first (a KVM VPS or Hetzner Cloud; Hetzner has an official Terraform provider).
- **Restore drill**, before go-live (run by the **second operator**) and quarterly after: a fresh
  AlmaLinux 9 machine anywhere → run the `host/` playbook → restore the latest dump → repoint
  `sso` via `dns/` → an existing user signs in with an existing passkey. The drill target can be a
  throwaway VM; it doubles as rehearsal for an eventual move off this host.
- Every consuming app keeps a local admin account (break-glass on the app side).

## Success looks like

1. Chris and one friend sign in with passkeys; the friend was brought in by a delegated group
   admin, not by Chris.
2. A user links and unlinks GitHub on their own account. Signing in with an unlinked Google
   account cannot create or attach to an account.
3. A test client added by PR appears after apply. Its owner can add people to its access group
   and nothing else: he cannot open realm settings, other groups or other clients, cannot reset
   any member's credentials, and escalation attempts fail.
4. A realm user with no role in a client is refused at that client.
5. Forgejo's token contains a flat `groups` array; a test client without the scope does not.
6. A hidden-project group exists in Keycloak and appears nowhere in the repo; a non-member
   delegate cannot see it in the console.
7. Signing into the admin console via a linked Google login alone is refused without a passkey or
   OTP.
8. The second operator completes the restore drill in about an hour without Chris, and existing
   passkeys still work.
9. Powering off galaxy has no effect on Realliance sign-in.
10. Keycloak + PostgreSQL run within the 3 GB container without OOM kills over the 14-day gate.

## Left to the builder

Reverse proxy choice (Caddy or nginx + certbot); heap and PostgreSQL tuning; Ansible role layout;
Keycloak version pinning and upgrade procedure; OpenTofu module structure; backup tooling
(`pg_dump` + `rclone` vs restic); how automation authenticates to the Keycloak admin API
(service-account client in `master`); SMTP provider; login-page theming with
Realliance branding.

## Open (not blocking the plan)

- The CrownPanel API key Chris created is not useful for this box; revoke it unless wanted for
  something else.

## Downstream notes for project 2 (forge)

- Forgejo's OIDC source uses the `groups` scope; `--group-team-map` maps Realliance groups to
  teams. The mapping for hidden projects lives in the forge's private config, readable only by
  realm admins, for the same reason hidden groups are console-only here.
- Team membership updates at sign-in, so removing someone takes full effect at their next login;
  suspend them in Keycloak to cut them off immediately.

---

## Amendment — 2026-11-02 (decided by Chris during implementation)

These decisions supersede the sections above where they conflict.

1. **Sign-in is password first, MFA required.** `realliance-browser` = username + password, then a
   required second factor: TOTP, a WebAuthn authenticator (FIDO2 security key or passkey) or a
   recovery code. A user with no second factor is forced to enrol TOTP at sign-in (a
   `conditional-sub-flow-executed` branch); WebAuthn keys are added from the account console.
   Passkey-first (passwordless) sign-in is off. `admin-cli` password grants are disabled (they
   would mint tokens without MFA).
2. **Linked logins (Google, GitHub, Microsoft, Discord) are deferred.** The first-broker-login and
   admin step-up designs return when they do.
3. **People are code, in a private repo.** `realliance/sso-people` (private) holds `people.yaml`
   (username, email, names, `active`, groups, plus hidden-project group definitions) and an
   OpenTofu layer that makes users and memberships. Private so members' emails and hidden-project
   names are never public. A new user is enrolled by a Keycloak 26.8 **workflow** (`user-created`
   → `invite-user`) that emails the set-up link. `active: false` disables an account (sessions
   end, data kept); deleting an entry is refused (`prevent_destroy`).
4. **No console delegation.** Fine-grained admin permissions and per-group admins are dropped;
   whoever may change membership does it by PR, approved by an operator. **No human account
   holds an admin role in `realliance`**; only the master-realm break-glass admins (password +
   TOTP) do. The admin console stays public, but nobody's everyday account can do anything there.
5. **The box applies Keycloak config itself**, extending `ansible-pull`: every 5 minutes it pulls
   `realliance/sso` and `realliance/sso-people` (read-only deploy key), verifies each head commit's
   signature (operators or GitHub web-flow), runs the host playbook, then `tofu apply` for
   `realm/` and the people layer. The `tofu-apply` secret never leaves the box. It reports
   `sso/apply` commit statuses to GitHub with a token limited to commit statuses. GitHub Actions
   only posts read-only `tofu-plan` plans on PRs. The daily full run re-applies everything,
   self-healing drift; admin events are logged.
6. **Email is Resend** (sending-only key restricted to realliance.net); its DNS records live in
   `realliance/sso` `dns/` with the `sso` record. **State** stays on the Realliance B2 account
   (no lockfile locking on B2; serialised by the single applier on the box).
