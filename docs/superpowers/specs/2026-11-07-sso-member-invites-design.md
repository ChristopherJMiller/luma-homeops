# Realliance SSO member invites: design

- **Date:** 2026-11-07.
- **Status:** draft for Chris's review. Nothing built.
- **Research:** the 2026-11-07 invite options report, kept local and out of git. This doc carries
  every finding a builder needs. The wish sits in `realliance/sso` `WISHLIST.md` under "Member
  invites."
- **Related:** SSO spec `2026-10-31-realliance-sso-design.md` and its 2026-11-02 amendment;
  forge-bot spec `2026-11-02-forge-bot-and-ci-design.md`; runner classes spec
  `2026-11-04-runner-classes-design.md`; `docs/ci.md`, `docs/pages.md`, `docs/forgejo.md`.
- **Markers:** *(Chris decided)* marks a decision Chris made on 2026-11-07 or 2026-11-08.
  *(Claude's call)* marks a default this doc chose, open to override.

## Intent

Today every Realliance account takes an operator: a PR to `realliance/sso-people` that Chris or
Connor merges. Members want to bring a friend onto the forge without asking an operator each time.

This design keeps the SSO's founding rule, that git creates accounts and nobody configures Keycloak
by hand, and removes the operator from one narrow case. A member hands someone a link, that person
fills in a form, and the form becomes a signed commit in a private ledger that the SSO box applies.
The person becomes a *guest*: an SSO account and a forge account, nothing more. Making a guest a
member stays an operator PR. Keycloak Organizations could send invites natively, but only with
realm-wide organization rights, and they create users before git knows about them: the
Keycloak-first shape Chris rejected for Gramps. Organizations stay off, as decided on 2026-11-02.

## Goals

1. A member creates a link with no operator involved, the invitee submits a form, and Keycloak's
   set-up mail arrives within 10 minutes.
2. Git creates the account: every link, redemption, revocation and deactivation is a signed ledger
   commit, applied like `people.yaml`.
3. The rules hold on the box. A compromised invite service can create no more guests than the
   quotas allow, and can grant nothing beyond the guest groups.
4. A bad ledger never blocks a `people.yaml` change.
5. Promotion is the existing `sso-people` PR, keeping the Keycloak user and copying no personal data.
6. A leaver's pending links stop working at once, with no revoke job, and operators can erase a
   guest's data on request.

## Non-goals

- Keycloak Organizations, open registration, or any Keycloak-first write.
- Invites into `/realliance`, a hidden project, or any app but the forge.
- Per-invite approval. The service could later hold one tier's links for approval, as a setting
  rather than a trust change.
- Mail from the service, and invites while galaxy is down. Sign-in never depends on the service.
- Fixing the accepted GitHub risks (Constraints).
- A guest CI tier or any `ci-dispatch` change: guests run CI as any unlisted owner does *(Chris
  decided)*.

## Anti-goals

Each of these makes the design a failure, even if it works:

- **A reconciler:** anything that writes Keycloak first and git after, or polls to correct drift.
- **A service that grants power:** any path from the service or ledger to a group beyond `/guests`
  and `/apps/forgejo`, or to more accounts than the quotas allow.
- **A ledger the `realliance` GitHub org can read**, or one that accepts a web-flow commit.
- **Invite chains:** a guest who can invite.
- **Members held hostage:** a refused or unreachable ledger that stops a `people.yaml` apply.
- **Personal data in the open:** an invitee's email or name in a log line, a metric label, a commit
  status, the public `realliance/sso` repo, or the Azure log table.
- **Hidden-project names reaching the service** through its tokens.
- **A mail outage:** invites that spend the shared Resend budget and break password resets.

## Constraints

- **The box.** Keycloak 26.8.x in the 3 GB CrownCloud container. Every 5 min `sso-config-apply`
  takes one `flock`, checks each repo's head against one GnuPG home that trusts the operators and
  GitHub's web-flow key, and runs `tofu apply` for `realm/` and `sso-people`, with state on the
  Realliance B2 account. The `enrol` workflow (`user_created` → `invite-user`) mails a 72 h link to
  verify the email and set a password. No staging SSO box exists: the production box is the only
  place the ledger path can run.
- **The `sso-people` schema.** An entry has `username` (permanent: lowercase letters, digits,
  dashes), `active`, `groups`, optional `mfa` (`webauthn` or `totp`), and `bootstrap` (`email`,
  `first_name`, optional `last_name`), read only at creation. Users carry `prevent_destroy`,
  `ignore_changes` covers the profile and attributes, and memberships are exact.
- **GitHub Free.** A private repo gets no branch protection, so anyone with write access can push
  anything, force-pushes included. Every rule must hold on the apply side.
- **Accepted risks, unchanged** *(Chris decided)*. The box trusts web-flow, so any owner of the
  `realliance` GitHub org can land a trusted commit on `sso-people`, and every org member can read
  its private repos. The ledger inherits neither. One limit remains: the box's code, ledger check
  included, comes from `realliance/sso`, whose head the box also accepts web-flow-signed. Whoever
  can change that repo can already grant themselves anything, so the ledger opens no new path, but
  its guarantees are only as strong as that code.
- **Mail.** The Resend free tier, 100 mails a day, shared by enrolment, password resets and the
  forge.
- **Galaxy.** Argo GitOps, SealedSecrets, one Ingress with a host (the wildcard DNS and certificate
  cover `invite.realliance.net`), a `release: prometheus` label on every ServiceMonitor, alerts as a
  `PrometheusRule`, images pinned by digest. **No secrets in `realliance/sso`**, not even
  encrypted: canonical copies live in `realliance/sso-secrets`.

## Decided

*(Chris decided)*, 2026-11-07 unless dated:

- The git-native flow: a link, a form, a signed ledger commit that the box applies, then Keycloak's
  `invite-user` sends the set-up mail. No reconciler, no Keycloak-first writes, Organizations off.
- The ledger lives in a **new Owners-only GitHub org**, never the `realliance` org. Chris creates
  the org and the repo.
- For the ledger the box trusts only the ledger's own signing key and the operators' keys, never
  web-flow.
- **Guest scope** (2026-11-08): an SSO account plus a normal, unrestricted forge account
  (`/apps/forgejo`). Guests can create repos, run CI and publish Pages sites, with no `/realliance`
  membership and no other app. Promotion stays a PR to `sso-people`.
- **Guest CI** (2026-11-08): the same as today's default for an unlisted owner, bounded only by the
  global caps. No guest tier and no `ci-dispatch` change; the exposure is an accepted risk (Known
  limits).

*(Claude's call)*: links apply automatically within the quotas; the quotas in "Rules on the box";
invitee emails in private git history, with an erasure procedure; a small Go service on galaxy,
separate from forge-bot, so nobody can create or redeem a link while galaxy is down; a leaver's
pending links die at redemption while their guests stay, flagged; and operator-owned code on the
box enforces every rule. After the builder check: every guest must pick an MFA method, the box
check ships check-only first, its state has no backup, a refused head freezes the ledger, and the
storage cap has a fallback.

## Design

### Shape

```
member ─(1) SSO sign-in, client "invites"─▶ invite.realliance.net (Go service, galaxy, ns sso-invites)
       ◀─(2) https://invite.realliance.net/i/<256-bit token>
              commit: link {id, inviter, created, expires, token_sha256, state: pending}
member ─(3) passes the link on out of band─▶ invitee
invitee ─(4) username, email, first name, [last name], MFA choice─▶ service
              reads Keycloak (view only): username and email free? inviter still enabled?
              commit: state redeemed + guest {…}, signed with the ledger key,
              pushed over SSH with the repo's write deploy key
                                   ▼
            <ledger-org>/sso-invites (private): ledger.yaml
                                   │ (5) box fetches with a read-only deploy key
                                   ▼
SSO box, sso-config-apply, one flock:
  verify every new commit: ledger key or operator, never web-flow
  → fast-forward from the last good commit, or an operator-signed head
  → check: schema, transitions, inviter active in people.yaml, quotas, caps, collisions
  → admitted guest set → tofu apply sso-people (people.yaml ∪ guests)
  → Keycloak user_created → invite-user → Resend: verify email + set password (72 h)
```

### The ledger

A private repo, `sso-invites`, in an Owners-only GitHub org: its only members are the two
operators, both owners, with base permission none, private forks off, and no GitHub Apps. It holds
`ledger.yaml`, a README and the schema. The service writes with a write deploy key on this repo
alone, the operators write as owners, and the box reads with its own read-only deploy key.

**Schema**, field names only. The ledger check refuses unknown keys, and no `groups` key exists at
any level.

| Field | Set by | Mutable |
|---|---|---|
| `version` | `1` | operator only |
| `links[].id` | service: random, unique | no |
| `links[].inviter` | service: the member's username | no |
| `links[].created`, `links[].expires` | service; `expires` at most 7 days after `created` *(Claude's call)* | no |
| `links[].token_sha256` | service. The token never reaches git, and a hash can't redeem | no |
| `links[].state` | `pending`, then `redeemed` or `revoked`. A pending link past `expires` counts as expired; nothing writes that | by transition |
| `links[].redeemed`, `links[].revoked` | service, with the transition | no |
| `links[].guest`: `username`, `bootstrap{email, first_name, last_name?}` | invitee, at redemption | no |
| `links[].guest.mfa` | invitee, at redemption. Required: `webauthn` (the form's default) or `totp`, never empty or `none`, matching members' first sign-in *(Claude's call)* | no |
| `links[].guest.active` | `true` at redemption | by transition |

Revoked and expired links stay as history; only an operator's erasure rewrite removes anything.

| Change | Ledger key may sign | Operator may sign |
|---|---|---|
| Add a `pending` link | yes | yes |
| `pending` to `revoked`, or to `redeemed` with a `guest` block | yes | yes |
| `guest.active` from `true` to `false` | yes | yes |
| `guest.active` from `false` to `true` | no | yes |
| Anything else: an immutable field, a removal, a history rewrite | no | yes |

### Rules on the box

A new ledger check in `sso-config-apply`, operator-owned and tested code in public `realliance/sso`,
runs before the people apply. It verifies against a second GnuPG home that holds only the ledger key
and the operators' keys, so web-flow can't sign for the ledger.

| Check | On failure |
|---|---|
| Every commit since the last good one verifies with the ledger keyring | refuse the head |
| The head fast-forwards from the last good commit, or an operator signed it | refuse the head |
| Schema, immutable fields, no removals, and the changes in each commit allowed for whoever signed it | refuse the head |
| A new guest: username well formed, not reserved, and free in `people.yaml`, the ledger and Keycloak; the email free the same way | refuse the entry |
| A new guest: `inviter` is an `active` person in `people.yaml` (never a guest) with quota left, and the link was pending and unexpired by the box's clock | refuse the entry |
| A new guest over a rate cap, counted by the box's clock | defer the entry to a later run |

- **Modes** *(Claude's call)*. A host setting, `ledger_mode`, reads `check` or `apply`, and only an
  operator-signed `realliance/sso` head changes it. In `check` the box fetches, verifies and
  validates the ledger and logs what it would apply, ids only, but hands the people layer no
  guests. With no staging box, the check ships in `check` and runs live there for at least 3 days,
  a week if possible, before an operator switches it to `apply`.
- **Members first.** No failure in the ledger path ever blocks the `people.yaml` apply. In `check`, a
  crash, a timeout or a failed fetch logs and moves on. In `apply`, a refused head falls back to
  the last good ledger. The box never drops a guest already in the people layer's state from the
  set it hands that layer, so nothing trips `prevent_destroy`, and the box never re-checks such a
  guest. Tests prove both modes.
- **Statuses.** The box sets `sso/apply` on each ledger head it checks: success when it applied the
  head, or in `check` would have, and failure with the reason when it refused it.
- **A refused head freezes the ledger.** Everything pushed after it waits, including any guest
  deactivation. That's intended: the remedy is an operator-signed head that fixes or reverts the bad
  commit. The service alerts after 1 h, and a guest who must go at once gets the emergency override
  (Promotion and offboarding).
- **Clock and state** *(Claude's call)*. The box counts caps and expiry from when it first saw each
  link and redemption, never from the service's timestamps, with a short expiry grace for apply
  latency. First-seen times, refusals and the last good commit live in `/var/lib/sso-config-apply`,
  with no backup. A rebuild loses them: the cap and expiry clocks restart, and the box re-validates
  every entry not yet in state from the ledger, taking the newest commit whose history passes as
  last good. That means a rebuild can let at most one extra day's cap through. Otherwise a refusal
  stays until an operator clears it with a signed ledger change.

**Quotas** *(Claude's call)*. The box takes each member's tier from `people.yaml`; the service shows
the same tier from token roles.

| Who invites | Quota | What holds a slot | Refund |
|---|---|---|---|
| An active person in `/realliance` | 5 per rolling 7 days | a pending link, or a redemption for 7 days | a revoked or expired link frees its slot |
| Any other active person in `people.yaml` | 1 for life | a pending link, or a redemption for good | a revoked or expired link frees it; deactivating or promoting the guest doesn't |
| A guest, or an inactive person | 0, and their pending links die at redemption | | |
| Everyone together | 10 redeemed links per rolling 24 h | | one over the cap waits for a later run |

Ten a day keeps invites near a tenth of the Resend budget.

### The invite service

Go, one replica, namespace `sso-invites` on galaxy, Ingress `invite.realliance.net`. It stays out of
forge-bot, a reconciler with `manage-users` and no inbound UI, but reuses its patterns: a ko and
distroless image, a transport guard that allows only the listed calls, `slog` audit lines with ids
only, metrics. It keeps no database: the ledger is its state.

- **Sign-in.** An OIDC client `invites` in `realm/apps.tf`. Role `invites.user` goes to
  `/apps/invites`, which the people layer adds to every `people.yaml` entry and never to a guest;
  role `invites.member` maps to `/realliance`, a small extension to the app module. No `groups`
  scope. Guests hold neither role, so the client's default-deny flow refuses them. Members find the
  service under "Applications" in their account console.
- **Pages.** A member's page creates, lists and revokes links, and deactivates their guests. The link
  page holds the form, whose MFA choice offers `webauthn`, preselected, or `totp`, and nothing
  else. A status page reads Keycloak, and after 20 min with no account it tells the invitee to
  contact the member who invited them.
- **Writes.** Git over SSH only: fetch, commit signed with the ledger key, push fast-forward, retry
  on a race. Its only GitHub API access is a token that reads commit statuses on `sso-invites`.
- **Reads.** A master-realm service account `invites` with `view-users` and `query-users` on
  `realliance`, nothing else: username and email availability, and whether the `inviter` account is
  still enabled. That credential can read every profile, as forge-bot's can, so the transport guard
  limits the code to those calls.
- **Abuse.** Tokens are 256-bit and single use. A per-IP rate limit at Traefik guards the form. A
  leaked link yields one guest, and each redemption sends one Keycloak mail.
- **Alerts.** The box has no metrics exporter, so the service watches the outcome end to end: a
  ledger head that the box refused, or that still lacks a success status, more than 1 h after its
  push; a redemption with no Keycloak user after 20 min; failing ledger pushes; each guest
  deactivation (the forge and Pages steps follow by hand); and guests whose `inviter` is no longer
  active (orphans).

### What a guest gets

*(Chris decided, 2026-11-08)*: an SSO account in `/guests` and `/apps/forgejo`
only, fixed in a new `guests.tf` in the people layer, and a normal, unrestricted Forgejo account. A
guest can create repos, push packages, run Actions, publish Pages sites, and open issues and PRs. A
guest can't create orgs (`DISABLE_REGULAR_ORG_CREATION`), joins no `realliance` team, can't read
private `realliance` repos, and meets default-deny at every other app.

**What that exposes**, from `ci.yaml`, `docs/ci.md` and `docs/pages.md` as of 2026-11-07. Chris
accepted the CI and kernel-escape parts (Known limits).

- **CI limits.** A guest can't make an org, so each guest is one CI owner, and with no `owners:`
  entry the `defaults` apply: `tiny` 1 job at once and 120 min a day, each launch reserving 20. Both
  switches have `small` and `medium` on, so paid CI is open: $10 a UTC month, 2 jobs at once. Only
  the global caps bound the total: $80 a month, $0.48 an hour, one `tiny` pod at a time for
  everyone. Eight guests could spend the month's whole paid cap and leave members' paid jobs held at
  `global-cap`; a dozen busy guests could keep the single `tiny` slot full all day.
- **Kernel escape.** A `tiny` job runs as a pod on a galaxy node, and every node runs etcd and Ceph.
  The runner classes spec accepted a kernel or runc escape as its largest risk on the premise "who:
  members only." Guests break that premise: code from people no operator vetted would run on the
  nodes that hold everything on galaxy. Pod Security `restricted`, the PID limit, the netpol gate and
  the egress lockdown still hold. Chris declined gVisor.
- **Pages.** Any public guest repo publishes to `<guest>.realliance.page`: any HTML and script,
  128 MB a site, no limit on the number of sites. A look-alike of the SSO sign-in page under a
  Realliance domain becomes possible, though `realliance.page` can't read `realliance.net` cookies.
  Takedown is Owner-only and manual.
- **B2 shared fate.** Guest packages, LFS objects and Pages traffic count against the Realliance B2
  daily download cap, which on 2026-11-07 took the registry, LFS, Pages and tofu state down
  together. The SSO's own state sits on that account, so a trip also stalls SSO applies.
- **Storage.** Guest repos land on galaxy's SMR disks with no Forgejo quota. One large push is the
  bulk-write pattern that makes Ceph flap. Members can do this today; guests widen who can.

**Guardrails that still hold:** default-deny at every other app; no org creation; private-by-default
repos; the hidden users page; `ACCOUNT_LINKING=disabled`; forge notification mail off; CI limits
bound to numeric ids, with ceilings in code, the `deny` list and the class switches; `tiny`'s pod
hardening; Pages' size cap, GET-only edge and header allowlist; the invite quotas and caps.

**Extra guardrails** *(Claude's call)*. Each is small, and the storage cap, or its fallback, ships
before `guests.tf` grants `/apps/forgejo`:

1. **A storage cap:** a Forgejo quota for guests across git, LFS and packages, about 2 GiB in all.
   Best, Forgejo 15 applies it from account creation. If it can't, forge-bot puts each new guest in
   a guest quota group through Forgejo's `/api/v1/admin` API right after their first sign-in, as
   one more guarded write. If Forgejo 15 can't do that either, guests ship without a quota, with an
   alert on the size of guest-owned repos and a runbook step, and the gap goes under Known limits.
2. **Pages at deactivation:** deactivating a guest also deletes and freezes
   `<guest>.realliance.page` (`docs/pages.md`, Takedown), by hand for now, prompted by the
   deactivation alert.
3. **One switch for all guests:** dropping `/apps/forgejo` from `guests.tf` blocks every guest's next
   sign-in, and forge-bot's `access` job, once it runs in `apply`, prohibits them within about 10 min.
4. **CI levers that need no code.** To cut one guest off, an Owner adds the guest's numeric id to
   `deny:` in `ci.yaml`: from the next sweep, about 5 min after the merge, every class holds that
   owner's jobs as `denied`, `tiny` included, with no deploy. For the paid budget,
   `CIDispatchSpendHigh` already pages around the clock at 80 and 95 percent of the period's cap
   ($64 and $76), and the usage API then shows which owners spent it (`docs/ci.md`, Reading paid
   spend). Guest offboarding adds the same `deny:` entry, since a prohibited account's scheduled
   workflows may keep queuing jobs (check on Forgejo 15).

### Promotion and offboarding

- **Promotion.** An operator PR to `sso-people` adds `username`, `active` and `groups` with no
  `bootstrap`, and the code takes the bootstrap from the ledger. People and guests share one
  `keycloak_user.person` map with `people.yaml` winning, so the Keycloak user stays and only the
  groups change; from then on `people.yaml` alone decides. The ledger entry stays as the record of
  who invited them. A promoted guest gets their new tier's quota; the member who invited them gets
  no slot back.
- **Guest offboarding.** The inviting member or an operator sets `guest.active: false` through the
  service, or an operator signs the commit, and Keycloak disables the account. On the forge,
  forge-bot's `access` job prohibits login once it runs in `apply`; until then, Prohibit login by
  hand (`docs/forgejo.md`). More than 3 at once trips forge-bot's breaker: set `allow_bulk_until`.
  Then the Pages takedown (guardrail 2) and the `deny:` entry (guardrail 4).
- **Emergency override.** An operator can deactivate a guest whatever the ledger says, even behind
  a refused head. They merge a `people.yaml` entry for the guest with `active: false`,
  `groups: [/guests]` and no `bootstrap`, the shape of a promotion. `people.yaml` wins over the
  ledger and always applies, so the account stays off from the next apply on. For effect
  within seconds, a master-realm break-glass account also disables the user in the Keycloak
  console, and the merged entry keeps the account off. The forge, Pages and `deny:` steps follow as
  for any guest.
- **A member leaves.** `active: false` in `people.yaml`. The service and the box both check the
  `inviter`, so that member's pending links die at redemption. Their guests stay, and the orphan
  alert lists them for an operator to keep or deactivate.

### Personal data and erasure

Per guest, the ledger holds a username, email, first name, optional last name, the MFA choice, the
`inviter` and timestamps; pronouns stay self-service. Copies also live in Keycloak and its backups,
tofu state on B2, and operators' clones. The service and the box log ids, never an email, a name, or
a link token, and that includes Traefik's access log. Erasure on request:

1. Set `guest.active: false`, then an operator deletes the Keycloak user.
2. An operator rewrites the entry out of history with `git filter-repo` and force-pushes a head they
   signed, which the ledger check accepts.
3. `tofu state rm` the user in the people layer.
4. Re-clone the box's ledger checkout and both operators' clones, and let the backups age out.

If that proves too weak, encrypting `bootstrap.email` to a key on the box costs about a day.

## Known limits (accepted)

- **A rebuild resets the box's clocks.** With no backup of `/var/lib/sso-config-apply`, a rebuilt
  box can let at most one extra day's cap through, and open links' expiry clocks restart.
- **A refused head freezes the ledger,** each deactivation included, until an operator signs a new
  head. The 1 h alert and the emergency override cover it.
- **No guest storage quota,** but only if Forgejo 15 allows neither path in guardrail 1. The size
  alert and the runbook step then stand in.
- **Guest CI is open, as for any unlisted owner** *(Chris decided)*. Each guest gets `tiny` 1 job at
  once and 120 min a day, and paid `small` and `medium` at $10 a period, 2 at once, bounded only by
  the global caps: $80 a period, $0.48 an hour, and the shared `tiny` concurrency. Roughly eight
  active guests could use up a period's paid budget, and members' paid CI would then hold too.
  Guardrail 4's levers are the response.
- **`tiny` carries the kernel-escape exposure for guests** *(Chris decided)*. An escape from a
  guest's job gives root on a galaxy node that runs etcd and Ceph.

## Operator steps

1. **Chris** creates the Owners-only GitHub org and the private repo `sso-invites`, set up as in "The
   ledger," and names the org.
2. Two deploy keys there: read-only for the box, write for the service. Canonical copies go to
   `sso-secrets`; a SealedSecret in `cluster/sso-invites/` carries the service's.
3. The ledger signing key: OpenPGP, no passphrase, with an expiry. The public half joins the box's
   ledger keyring in `realliance/sso`; the private half goes to `sso-secrets` and a SealedSecret.
4. Two fine-grained tokens on `sso-invites`, limited to commit statuses: write for the box, read for
   the service.
5. **Chris** signs the `realliance/sso` heads that carry the host-role changes, which roll out the
   existing way through `ansible-pull`, and after the check-only run, the head that sets
   `ledger_mode: apply`.
6. Answers to the open points.

**Build order.** First `/guests` in `realm/`, `guests.tf` in `sso-people`, and the ledger check with
its tests, shipped in `check` mode. It runs live for at least 3 days, a week if possible, on
operator-signed test entries, good and bad, with the expected verdicts in its log and no change to
`people.yaml` applies. Only then does an operator switch it to `apply`, and an operator-signed hand
entry proves a guest end to end. The `invites` client, service account and service can take shape
during that window. Then the storage cap, or its fallback, and only after it `/apps/forgejo` in
`guests.tf`. Acceptance tests (`realliance/sso` `tests/invites/`) and runbooks come last.

## Success looks like

1. A member makes a link and the invitee redeems it. The set-up mail arrives within 10 min, the
   guest signs in to the forge with no team, and every other app refuses them, the invite service
   included.
2. A one-for-life member can't make a second link until they revoke the first; a member's sixth link
   in 7 days fails; an eleventh redemption in 24 h waits for the next day.
3. Holding the ledger key, a commit that adds `groups`, a redemption without `mfa`, a commit over
   quota, a web-flow commit and a force-push each fail at the box, while a `people.yaml` change in
   the same window applies.
4. In `check` mode, a ledger check that crashes, times out or can't fetch leaves the `people.yaml`
   apply unchanged, and the same holds in `apply` mode. An explicit test proves each.
5. A head refused for over 1 h raises the alert, and the emergency override disables a guest while
   that head stays refused.
6. A deactivated member's pending link fails at redemption, and the orphan alert names their guests.
7. A promotion PR without `bootstrap` keeps the Keycloak user id and changes only the groups.
8. A guest's CI runs under `defaults` like any unlisted owner's, and a `deny:` entry for the guest's
   id holds each of their jobs as `denied` within one sweep.
9. With galaxy off, sign-in works, and an operator-signed hand entry still creates a guest.
10. The erasure procedure runs on a throwaway guest.

## Left to the builder

- Signing and verification: go-git with the OpenPGP key, the second GnuPG home, key rotation.
- How the box hands the admitted guest set to the people layer, and what the `sso-people` PR plan
  job sees.
- Latency toward the 10-minute goal: for example, `git ls-remote` on the ledger every minute, and
  skipping the realm apply when `realliance/sso` hasn't changed.
- The service's UI, sessions, CSRF protection, rate-limit values, and where the token sits in the
  link.
- The reserved-name list (bots, Forgejo orgs, `www`, hidden orgs), kept in private `sso-people`.
- Username length, the expiry grace, and a size limit on the ledger.
- Which storage-cap path Forgejo 15 allows.
- Where `ledger_mode` sits in the host config, and the test entries for the check-only run.
- Where the service's code lives. `realliance-ops/sso-invites` on the forge matches forge-bot.
- Updates to `WISHLIST.md`, `docs/forgejo.md` and the SSO runbook.

## Open points for Chris

1. **Pages at deactivation:** take a deactivated guest's sites down, or keep them?
2. **The org:** a new dedicated one, or the planned `realliance-ops` GitHub mirror org, if its only
   members are the two operators and no mirror credential reaches `sso-invites`.
3. **Link lifetime** of 7 days, and whether a promoted guest should get invites at all.
