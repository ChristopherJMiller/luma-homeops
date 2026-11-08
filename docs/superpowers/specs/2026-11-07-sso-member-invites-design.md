# Realliance SSO member invites: design

> **Amendment (2026-11-08, Chris decided).** The ledger is a file, `ledger.yaml`, at the root of
> `realliance/sso-people`, not a repo in a new GitHub org. Chris chose it with its costs in view:
> the service's write key reaches `people.yaml`, a bad push can stall member applies, and erasure
> rewrites `sso-people` history. The box now trusts commits by path: a ledger-key commit may change
> `ledger.yaml` alone, and any invalid commit freezes `sso-people` until an operator signs a
> `Reviewed-Range` commit. Where Chris didn't decide a detail, it's *(Claude's call)*. Changed:
> Intent, Goals, Anti-goals, Constraints, Decided, Shape, The ledger, Rules on the box, The invite
> service, Promotion and offboarding, Personal data and erasure, Known limits, Operator steps,
> Success looks like, Left to the builder, Open points.

- **Date:** 2026-11-07, amended 2026-11-08.
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
fills in a form, and the form becomes a signed commit to a ledger file in `sso-people` that the SSO
box applies. The person becomes a *guest*: an SSO account and a forge account, nothing more. Making
a guest a member stays an operator PR. Keycloak Organizations could send invites natively, but only
with realm-wide organization rights, and they create users before git knows about them: the
Keycloak-first shape Chris rejected for Gramps. Organizations stay off, as decided on 2026-11-02.

## Goals

1. A member creates a link with no operator involved, the invitee submits a form, and Keycloak's
   set-up mail arrives within 10 minutes.
2. Git creates the account: every link, redemption, revocation and deactivation is a signed ledger
   commit, applied with `people.yaml`.
3. The rules hold on the box. A compromised invite service can create no more guests than the
   quotas allow, and can grant nothing beyond the guest groups.
4. A bad ledger entry never blocks a `people.yaml` change. Only an invalid commit stops member
   applies, and an operator can always clear it.
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
- **A ledger key with reach:** any ledger-key commit the box accepts while it changes a path other
  than `ledger.yaml`.
- **Invite chains:** a guest who can invite.
- **Members held hostage by content:** a ledger entry or ledger file problem that stops a
  `people.yaml` apply. An invalid commit is different: it freezes applies on purpose (Rules on the
  box).
- **Personal data in the open:** an invitee's email or name in a log line, a metric label, a commit
  status, the public `realliance/sso` repo, or the Azure log table.
- **Hidden-project names reaching the service's code** through its tokens or its reads. Its clone
  of `sso-people` holds them (Known limits), and its code reads `ledger.yaml` alone.
- **A mail outage:** invites that spend the shared Resend budget and break password resets.

## Constraints

- **The box.** Keycloak 26.8.x in the 3 GB CrownCloud container. Every 5 min `sso-config-apply`
  takes one `flock`, checks each repo's head against one GnuPG home that trusts the operators and
  GitHub's web-flow key, and runs `tofu apply` for `realm/` and `sso-people`, with state on the
  Realliance B2 account. It already pulls `sso-people` with a read-only deploy key and records the
  last `sso-people` commit it applied. The `enrol` workflow (`user_created` → `invite-user`) mails a
  72 h link to verify the email and set a password. No staging SSO box exists: the production box is
  the only place the ledger path can run.
- **The `sso-people` schema.** An entry has `username` (permanent: lowercase letters, digits,
  dashes), `active`, `groups`, optional `mfa` (`webauthn` or `totp`), and `bootstrap` (`email`,
  `first_name`, optional `last_name`), read only at creation. Users carry `prevent_destroy`,
  `ignore_changes` covers the profile and attributes, and memberships are exact.
- **GitHub Free.** A private repo gets no branch protection, so anyone with write access can push
  anything to `main`, force-pushes included, and GitHub can't limit a deploy key to one path. Every
  rule must hold on the apply side.
- **Accepted risks, unchanged** *(Chris decided)*. The box trusts web-flow, so any owner of the
  `realliance` GitHub org can land a trusted commit on `sso-people`, and every org member can read
  its private repos. With the ledger in `sso-people` it inherits both: a web-flow commit that edits
  `ledger.yaml` counts as an operator edit, and every org member can read invitee data. One limit
  remains: the box's code, ledger checks included, comes from `realliance/sso`, whose head the box
  also accepts web-flow-signed. Whoever can change that repo can already grant themselves anything,
  so the ledger's guarantees are only as strong as that code.
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
- **Ledger location** (2026-11-08, replacing the 2026-11-07 "new Owners-only GitHub org"):
  `ledger.yaml` at the root of `realliance/sso-people`, with the costs in the amendment note.
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
check ships check-only first, its state has no backup, a refused ledger freezes later ledger
changes, and the storage cap has a fallback. After the ledger move: trust by path, a freeze behind
any invalid commit with `Reviewed-Range` recovery, and a bounded rebase-and-retry for the service's
pushes.

## Design

### Shape

```
member ─(1) SSO sign-in, client "invites"─▶ invite.realliance.net (Go service, galaxy, ns sso-invites)
       ◀─(2) https://invite.realliance.net/i/<256-bit token>
              commit: link {id, inviter, created, expires, token_sha256, state: pending}
member ─(3) passes the link on out of band─▶ invitee
invitee ─(4) username, email, first name, [last name], MFA choice─▶ service
              reads Keycloak (view only): username and email free? inviter still enabled?
              commit touching ledger.yaml alone, signed with the ledger key, pushed
              straight to main with a write deploy key; on a race, rebase and retry, never force
                                   ▼
            realliance/sso-people (private): people.yaml + ledger.yaml
                                   │ (5) box fetches, as it does today
                                   ▼
SSO box, sso-config-apply, one flock:
  walk every first-parent commit since last good:
      ledger key → ledger.yaml only; operator key or web-flow → any path; else invalid
  → any invalid commit or a non-fast-forward: apply nothing for sso-people, alert
  → check ledger.yaml: schema, transitions, inviter active in people.yaml, quotas, caps, collisions
  → admitted guest set → tofu apply sso-people (people.yaml ∪ guests)
  → Keycloak user_created → invite-user → Resend: verify email + set password (72 h)
```

### The ledger

`ledger.yaml` sits at the root of private `realliance/sso-people`, beside `people.yaml`. The service
writes it with a write deploy key on `sso-people`, operators edit it like any file there, and the box
reads it from the checkout it already pulls. Nothing new on GitHub: no org, no second repo, no
second read-only deploy key.

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

| Change to `ledger.yaml` | Ledger key may sign | Operator key or web-flow may sign |
|---|---|---|
| Add a `pending` link | yes | yes |
| `pending` to `revoked`, or to `redeemed` with a `guest` block | yes | yes |
| `guest.active` from `true` to `false` | yes | yes |
| `guest.active` from `false` to `true` | no | yes |
| Anything else: an immutable field, a removal | no | yes |

A history rewrite also needs an operator-signed `Reviewed-Range` head (Rules on the box).

### Rules on the box

A new step in `sso-config-apply`, operator-owned and tested code in public `realliance/sso`, runs
before the people apply. It checks commits first, then the ledger's content *(Claude's call)*.

**Commits.** The box walks the first-parent chain from its last good commit to the head and checks
the signer of each commit against the paths it changes. A merge counts as one change against its
first parent, so commits inside a merged PR need no signature of their own, as today.

| Signer | May change |
|---|---|
| The ledger key | `ledger.yaml` alone, in a commit with one parent. Any other path, a rename, a mode change or a submodule makes the commit invalid |
| An operator key, or GitHub's web-flow key | any path, as today. Its `ledger.yaml` changes count as operator edits: hand entries, overrides, erasure |
| No signature, or any other key | nothing: the commit is invalid |

- **A valid chain** applies `people.yaml` and the admitted guests at the head, as today.
- **A freeze.** An invalid commit anywhere since last good, or a head that doesn't fast-forward from
  it, freezes `sso-people`. The box then runs no `sso-people` apply at all, last good included, so it
  never applies a `people.yaml` that sits on an invalid commit. It sets a failure status, and the
  service alerts. A compromised service, or anyone holding its deploy key, can freeze member applies
  this way until an operator acts *(Chris decided, 2026-11-08: the accepted cost)*.
- **Recovery.** An operator reverts the bad change, and with an operator key, never web-flow or the
  ledger key, signs a commit whose message carries the trailer
  `Reviewed-Range: <last-good>..<tip>`, both full shas. The box accepts it only if `<last-good>` is
  its own last good, `<tip>` is an ancestor of that commit, and every invalid commit falls inside the
  range. It then treats the range as reviewed, checks the commits after `<tip>` as usual, and
  resumes. A replayed old recovery commit names a stale last good and fails. This trailer on an
  operator-signed head is also the only way the box takes a history that doesn't fast-forward, as
  after an erasure.

**Content.** On a valid chain, the box validates `ledger.yaml` at the head:

| Check | On failure |
|---|---|
| Schema, immutable fields, no removals, and the changes in each ledger-key commit within the transitions table | refuse the ledger: guests stay at the last good ledger, later ledger changes wait, `people.yaml` still applies |
| A new guest: username well formed, not reserved, and free in `people.yaml`, the ledger and Keycloak; the email free the same way | refuse the entry |
| A new guest: `inviter` is an `active` person in `people.yaml` (never a guest) with quota left, and the link was pending and unexpired by the box's clock | refuse the entry |
| A new guest over a rate cap, counted by the box's clock | defer the entry to a later run |

A content problem in a validly signed, path-correct commit never freezes `people.yaml`.

- **Modes** *(Claude's call)*. A host setting, `ledger_mode`, reads `check` or `apply`, and only an
  operator-signed `realliance/sso` head changes it. In `check` the box applies `people.yaml` as
  today, but from the newest first-parent commit an operator key or web-flow signed, so a service
  push at the head never blocks it. It hands the people layer no guests, and only logs what the walk
  and the content checks would decide, a freeze included. With no staging box, the check ships in
  `check` and runs live there for at least 3 days, a week if possible, before an operator switches it
  to `apply`. Deliberately bad test commits go on a separate branch that the check reads and never
  applies.
- **Members first.** In `check`, no failure in the ledger path ever blocks the `people.yaml` apply:
  a crash, a timeout or a failed read logs and moves on. In `apply`, only an invalid commit stops it;
  a refused ledger or entry never does. The box never drops a guest already in the people layer's
  state from the set it hands that layer, so nothing trips `prevent_destroy`, and the box never
  re-checks such a guest. Tests prove both modes.
- **Statuses.** The box sets two statuses on each `sso-people` head it checks: `sso/apply`, failing
  on a freeze, and `sso/ledger`, failing when it refuses the ledger. In `check`, both report what
  `apply` would have done.
- **A refused ledger freezes later ledger changes.** Everything pushed to `ledger.yaml` after it
  waits, including any guest deactivation, while `people.yaml` keeps applying. The remedy is a
  commit, from an operator key or web-flow, that fixes or reverts `ledger.yaml`. The service alerts
  after 1 h, and a guest who must go at once gets the emergency override (Promotion and offboarding).
- **Clock and state** *(Claude's call)*. The box counts caps and expiry from when it first saw each
  link and redemption, never from the service's timestamps, with a short expiry grace for apply
  latency. First-seen times, refusals, the last good commit and the last good ledger live in
  `/var/lib/sso-config-apply`, with no backup. The last `sso-people` commit the box applied before
  this ships seeds last good. A rebuild loses the state: the cap and expiry clocks restart, last
  good restarts at the newest first-parent commit an operator key or web-flow signed, and the box
  re-validates every entry not yet in state. That means a rebuild can let at most one extra day's
  cap through. Otherwise a refusal stays until an operator clears it with a ledger change.

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
- **Writes** *(Claude's call)*. Git over SSH only, with a write deploy key on `sso-people`. It
  changes `ledger.yaml` alone, checks that before signing, signs with the ledger key and pushes
  straight to `main`. On a non-fast-forward rejection, as after an operator's merge, it fetches,
  rebuilds its change on the new head from a fresh read of `ledger.yaml`, signs again and retries,
  at most 5 times with backoff, then gives up and alerts. It never force-pushes. Its only GitHub API
  access is a token that reads commit statuses on `sso-people`.
- **Reads.** A master-realm service account `invites` with `view-users` and `query-users` on
  `realliance`, nothing else: username and email availability, and whether the `inviter` account is
  still enabled. That credential can read every profile, as forge-bot's can, so the transport guard
  limits the code to those calls. Its clone of `sso-people` carries `people.yaml` too; the code
  reads `ledger.yaml` alone.
- **Abuse.** Tokens are 256-bit and single use. A per-IP rate limit at Traefik guards the form. A
  leaked link yields one guest, and each redemption sends one Keycloak mail.
- **Alerts.** The box has no metrics exporter, so the service watches the outcome end to end: an
  `sso-people` head whose `sso/apply` or `sso/ledger` status still fails, or is missing, more than
  1 h after the push; a redemption with no Keycloak user after 20 min; failing or exhausted ledger
  pushes; each guest deactivation (the forge and Pages steps follow by hand); and guests whose
  `inviter` is no longer active (orphans).

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
  service, or an operator commits the change, and Keycloak disables the account. On the forge,
  forge-bot's `access` job prohibits login once it runs in `apply`; until then, Prohibit login by
  hand (`docs/forgejo.md`). More than 3 at once trips forge-bot's breaker: set `allow_bulk_until`.
  Then the Pages takedown (guardrail 2) and the `deny:` entry (guardrail 4).
- **Emergency override.** An operator can deactivate a guest whatever the ledger says. Behind a
  refused ledger, they merge a `people.yaml` entry for the guest with `active: false`,
  `groups: [/guests]` and no `bootstrap`, the shape of a promotion. `people.yaml` wins over the
  ledger and still applies, so the account stays off from the next apply on. For effect within
  seconds, a master-realm break-glass account also disables the user in the Keycloak console, and
  the merged entry keeps the account off. During a freeze no `sso-people` apply runs, so the console
  change sticks until recovery, and the `Reviewed-Range` commit carries the `people.yaml` entry. The
  forge, Pages and `deny:` steps follow as for any guest.
- **A member leaves.** `active: false` in `people.yaml`. The service and the box both check the
  `inviter`, so that member's pending links die at redemption. Their guests stay, and the orphan
  alert lists them for an operator to keep or deactivate.

### Personal data and erasure

Per guest, the ledger holds a username, email, first name, optional last name, the MFA choice, the
`inviter` and timestamps; pronouns stay self-service. Copies also live in Keycloak and its backups,
tofu state on B2, and every clone of `sso-people`, which any `realliance` GitHub org member can
make. The service and the box log ids, never an email, a name, or a link token, and that includes
Traefik's access log. Erasure on request:

1. Set `guest.active: false`, then an operator deletes the Keycloak user.
2. `tofu state rm` the user in the people layer, under the box's lock, so the rewritten ledger can't
   trip `prevent_destroy`.
3. Rehearse the rewrite on a throwaway copy of `sso-people` first.
4. Rewrite history with `git filter-repo`, touching `ledger.yaml` blobs alone. Every commit id
   from the guest's first ledger commit on changes, members' `people.yaml` history included, but no
   other file's content does, and the rewritten commits lose their signatures.
5. An operator adds a head signed with their key whose `Reviewed-Range` names the box's last good
   and the rewritten tip, then force-pushes. The box takes the new history only through that head.
6. Re-clone the box's checkout and both operators' clones, rebase any open PR, and let the backups
   age out. Clones held by `realliance` org members are out of reach (Known limits).

If that proves too weak, encrypting `bootstrap.email` to a key on the box costs about a day.

## Known limits (accepted)

- **A compromised service can freeze member applies** *(Chris decided, 2026-11-08)*. Its deploy key
  can push any commit to `sso-people`. The box refuses to apply past an invalid one, so members wait
  until an operator signs a `Reviewed-Range` commit. The 1 h alert covers it.
- **The service's key reaches every path in `sso-people`** *(Chris decided)*: write to the whole
  repo, limited only by the box's path check, and read of `people.yaml`, members' emails and
  hidden-project group names included.
- **Invitee data reaches the `realliance` GitHub org** *(accepted risk)*. Every org member can read
  invitee emails and names, as they read members' own data in `people.yaml`, and their clones keep
  them after an erasure.
- **Erasure rewrites `sso-people` history** *(Chris decided)*: commit shas change, content doesn't,
  and every clone and open PR must follow.
- **A rebuild resets the box's clocks.** With no backup of `/var/lib/sso-config-apply`, a rebuilt
  box can let at most one extra day's cap through, and open links' expiry clocks restart.
- **A refused ledger freezes later ledger changes,** each deactivation included, until a commit
  fixes `ledger.yaml`. The 1 h alert and the emergency override cover it.
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

1. A write deploy key for the service on `realliance/sso-people`. Its canonical copy goes to
   `sso-secrets`, and a SealedSecret in `cluster/sso-invites/` carries it.
2. The ledger signing key: OpenPGP, no passphrase, with an expiry. The public half joins the box's
   keyring in `realliance/sso`, marked as the ledger key; the private half goes to `sso-secrets` and
   a SealedSecret.
3. A fine-grained token that reads commit statuses on `sso-people`, for the service. The box keeps
   its existing status token.
4. **Chris** signs the `realliance/sso` heads that carry the host-role changes, which roll out the
   existing way through `ansible-pull`, and after the check-only run, the head that sets
   `ledger_mode: apply`.
5. Answers to the open points.

**Build order.** First `/guests` in `realm/`, `guests.tf` and an empty `ledger.yaml` in
`sso-people`, and the box's commit and content checks with their tests, shipped in `check` mode. It
runs live for at least 3 days, a week if possible: operator hand entries on `main`, bad commits on
the test branch, the expected verdicts in its log, and no change to `people.yaml` applies. Only then
does an operator switch it to `apply`, and an operator-signed hand entry proves a guest end to end.
The `invites` client, service account and service can take shape during that window. Then the
storage cap, or its fallback, and only after it `/apps/forgejo` in `guests.tf`. Acceptance tests
(`realliance/sso` `tests/invites/`) and runbooks come last.

## Success looks like

1. A member makes a link and the invitee redeems it. The set-up mail arrives within 10 min, the
   guest signs in to the forge with no team, and every other app refuses them, the invite service
   included.
2. A one-for-life member can't make a second link until they revoke the first; a member's sixth link
   in 7 days fails; an eleventh redemption in 24 h waits for the next day.
3. Holding the ledger key, a commit that adds `groups`, a redemption without `mfa` or a commit over
   quota: the box refuses each at the ledger or the entry, while a `people.yaml` change in the same
   window applies.
4. A ledger-key commit that changes any other path, a rename, a mode change or a submodule
   included, an unsigned commit, or a force-push each freeze every `sso-people` apply and raise the
   alert. An operator's `Reviewed-Range` commit resumes them, and a replayed old one doesn't.
5. In `check` mode, a ledger check that crashes, times out or fails a read, or a service push at the
   head, leaves the `people.yaml` apply unchanged. An explicit test proves it, and another proves
   `apply` mode keeps applying `people.yaml` behind a refused ledger.
6. The service's push loses a race to an operator's merge, rebuilds on the new head, and lands
   without a force-push.
7. A ledger refused for over 1 h raises the alert, and the emergency override disables a guest while
   it stays refused.
8. A deactivated member's pending link fails at redemption, and the orphan alert names their guests.
9. A promotion PR without `bootstrap` keeps the Keycloak user id and changes only the groups.
10. A guest's CI runs under `defaults` like any unlisted owner's, and a `deny:` entry for the guest's
   id holds each of their jobs as `denied` within one sweep.
11. With galaxy off, sign-in works, and an operator-signed hand entry still creates a guest.
12. The erasure procedure runs on a throwaway copy of `sso-people`, then on a throwaway guest.

## Left to the builder

- Signing and verification: go-git with the OpenPGP key, how the box tells signers apart by
  fingerprint, key rotation.
- How the box hands the admitted guest set to the people layer, and what the `sso-people` PR plan
  job sees.
- Latency toward the 10-minute goal: for example, `git ls-remote` on `sso-people` every minute, and
  skipping the realm apply when `realliance/sso` hasn't changed.
- The service's UI, sessions, CSRF protection, rate-limit values, push backoff, and where the token
  sits in the link.
- The reserved-name list (bots, Forgejo orgs, `www`, hidden orgs), kept in private `sso-people`.
- Username length, the expiry grace, and a size limit on the ledger.
- Which storage-cap path Forgejo 15 allows.
- Where `ledger_mode` sits in the host config, the test branch's name, and the test commits.
- The `filter-repo` callback for an erasure.
- Where the service's code lives. `realliance-ops/sso-invites` on the forge matches forge-bot.
- Updates to `WISHLIST.md`, `docs/forgejo.md`, the SSO runbook and the `sso-people` README.

## Open points for Chris

1. **Pages at deactivation:** take a deactivated guest's sites down, or keep them?
2. **Link lifetime** of 7 days, and whether a promoted guest should get invites at all.
