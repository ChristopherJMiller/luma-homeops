# forge-bot and minimal Forgejo CI — design

Date: 2026-11-02 · Status: draft; Chris answered the open questions 2026-11-02 · Project 3 of 3
(SSO → forge → **CI**), plus the forge's phase 2c, re-scoped. Related: SSO spec
`2026-10-31-realliance-sso-design.md`, forge spec `2026-11-02-realliance-forge-design.md`
(+ amendment), runbook `docs/forgejo.md`.

## Why

Forgejo reads the SSO only when someone signs in. That leaves three gaps:

- **Removals and demotions take two manual steps.** SSH keys, tokens and 7-day sessions never
  trigger a sync.
- **SSO profiles never reach Forgejo.** Email and name changes don't arrive, pronouns have no
  consumer, and avatars exist only in Forgejo.
- **The group → team map can't name a hidden org.** Since the 2026-11-03 outage it lives only in
  the login source in Forgejo's database (`gitea.oauth: []`). Public comments in
  `forgejo-release.yaml` still spell it out.

forge-bot closes all three by reconciling the forge against the SSO every few minutes. As the first
project built on the forge, it also brings the smallest useful CI: Forgejo Actions with one
unprivileged runner on galaxy, publishing to the forge's own registry.

## Goals

1. **Access follows the SSO continuously.** Someone disabled, or removed from `/apps/forgejo`,
   loses web, token and SSH access within ~10 min, and gets it back within ~10 min of returning.
2. **Admin status and non-Owners team membership follow SSO groups continuously.** There is one
   writer, forge-bot, and the map lives in the private inventory.
3. **Profiles flow both ways.** Email, display name and pronouns go SSO → Forgejo. The avatar goes
   Forgejo → SSO `picture`.
4. **It fails safe.** An SSO outage, a partial read, bad config or an inventory commit not signed
   by an Owner changes nothing. Large changes need an explicit acknowledgement.
5. **forge-bot is built by Forgejo Actions** and pulled by galaxy from `git.realliance.net`.
6. **One in-cluster runner, for allowlisted repos only.** It is unprivileged and runs no DinD.

## Non-goals (each gets its own spec later)

- **Later forge-bot jobs:** GitHub mirroring (using the GitHub username linked in the SSO; each Forgejo
  org maps one-to-one to a configured GitHub org, e.g. `realliance-ops` to its own GitHub org — Chris,
  2026-11-03), repo defaults, a nightly `repos.yaml` snapshot, and the CI runner scaler.
- **The "forge CI" project:**
  - Azure small/medium/large runner VMs and their scaler (a warm-standby start/stop watcher, or
    KEDA + AKS), and ephemeral runners.
  - Windows and macOS runners: a Mac Studio Homebrew runner and a Windows community build, with
    GitHub mirror Actions as the fallback.
  - Moving conduit-moba's 11 workflows.
- **Package pushes with the job token.** This needs Forgejo v16 "Authorized Integrations", first
  in an LTS with v19 (April 2027). Workflow OIDC `id-token` arrives with Azure.
- **Applying the inventory from Actions** *(my call)*. It needs the admin token and the tfstate
  keys on a host-mode runner, so it waits for ephemeral runners.
- **Also out:** Renovate on forge repos, Matrix notifications or a digest, and leaving Forgejo 15
  LTS.

## Anti-goals

- **forge-bot as a source of truth.** It keeps no database and no memory between cycles that
  decides access.
- **A mass prohibit or demotion** triggered by an empty, failed or partial read.
- **Prohibiting someone for a failed password.** Being locked out after failed passwords is not
  being disabled.
- **Two writers for one fact** (the login-source map and forge-bot).
- **Hidden org, team or group names** in public code, in luma-homeops config or in metric labels.
- **Privileged CI:** privileged pods, DinD, Unconfined seccomp or AppArmor, hostPath, a Docker
  socket.
- **`runs-on: ubuntu-latest` silently landing in a host-mode pod** on a control-plane node.
- **CI holding galaxy or GitHub power.** No Kubernetes token, kubeconfig or deploy key goes on the
  runner.

## Constraints

| Area | Constraint |
|---|---|
| Forgejo | 15.0.11 LTS (chart 17.3.2), and it stays on 15. The job token cannot push packages (`perm.go` TODO). Owners teams are never mapped: Forgejo refuses to remove a last owner. |
| galaxy | 3 control-plane nodes, no worker pool (S5). Pod Security `baseline`. **flannel does not enforce NetworkPolicy.** Memory ~84 % requested. SMR HDD pool. The SSD pool has ~155 GiB usable, shared with every Postgres. The NVMe is the Talos system disk plus etcd. |
| GitOps | Argo, rule S2. Images pinned `:sha-<short>@sha256:…`, and a rebuild needs a manual re-pin (sharp edge 6). Secrets go through git-crypt + SealedSecrets, with canonical copies in `realliance/sso-secrets`. |
| SSO | One small, unmonitored VPS, with one outage so far. Fine-grained admin permissions are off (`admin_permissions_enabled = false`, decided). No `realliance`-realm account may hold a management role. Master-realm automation clients are created by Ansible (`host/roles/master_realm`). |
| sso-people | Applies every ~5 min. Since #7 it ignores `attributes` after creation, so `pronouns` survive (realliance/sso #30 tests it), and forge-bot is the only writer of `picture`. |
| Visibility | Public: forge-bot, `realliance/sso`, luma-homeops. Private: the inventory, sso-people, sso-secrets. |
| People | Owners: Chris, and Connor (`connorjc`) after his first sign-in. Only Chris edits galaxy (luma-homeops), including the sealed Alertmanager config. |

## Decided (by Chris)

**Earlier decisions**

- **One Realliance bot; no forge-sync.** forge-bot is written in **Go**, in a **public** repo on
  `git.realliance.net`. It is the first repo developed on the forge and will later drive infra
  tooling. It supersedes the forge spec's "forge-sync deferred" and re-scopes phase 2c.
- **It reconciles every ~5 minutes.** The jobs:
  1. **Access.** Prohibit login for anyone disabled in the SSO or lacking `/apps/forgejo`; lift it
     when they're back.
  2. **Admin and teams.** Site admin from `forgejo-admins` and non-Owners team membership from
     SSO groups, continuously. This takes over from Forgejo's sign-in-time mapping, which is then
     removed from the login source.
  3. **Profile.** Email, display name and pronouns into Forgejo.
  4. **Avatars.** Each person's Forgejo avatar URL becomes their SSO `picture`.
- **The map is `teams.yaml` in the private inventory**, read through the Forgejo API every cycle.
- **CI is Forgejo Actions.** `DEFAULT_ACTIONS_URL` is `https://github.com`; sensitive repos pin
  actions by SHA.
- **The first build runs on one tiny in-cluster runner.** forgejo-runner v13.2.1 in **host mode**,
  toolchains baked in (Go, ko, Node, Nix; the builder may trim), no DinD, nothing privileged,
  daemonless builds.
- **Registry pushes:**
  - First, prototype whether a Forgejo 15 *repository-specific* token with package write can push,
    and only to its own repo's packages.
  - If yes, use per-repo tokens. If not, use a dedicated bot account's package-write-only token,
    stored as an org secret.
  - Switch to the job token at v19.
- **Deferred:** Azure, Windows and macOS runners, and conduit-moba's CI.

**Answers, 2026-11-02**

1. **Avatars keep the `manage-users` service account.** Chris accepts that this is **full
   user-admin power over every Realliance account**: it could reset any account's email or
   credentials. The required mitigations:
   - the secret exists only as a SealedSecret plus its canonical copy in sso-secrets;
   - a code-level allow-list: forge-bot's only SSO write is the `picture` attribute,
     read-modify-write of that one attribute, unit-tested;
   - every SSO write is audit-logged;
   - an alert fires on any SSO write that isn't `picture`;
   - a NetworkPolicy limits egress.
2. **Inventory trust.** **Before the teams job goes to `apply`**, `realliance/forge` moves to a new
   Owners-only org (owners Chris and Connor). forge-bot then acts only on an inventory `main` whose
   head commit is signed by an Owner's GPG key, verified the way the SSO box's `ansible-pull
   --verify-commit` works.
3. **The forge-bot repo stays at `realliance/forge-bot`** (public); only the inventory moves to
   the new org. It is a declared critical repo in the inventory (`prevent_destroy`, protected
   `main`, signed commits, Owners-only fast-forward merges). The workflow runs on `main`, tags and
   PRs. Chris reviews the diff since the last pin before every re-pin into luma-homeops. The risk
   this accepts is stated under "Security model".
4. **Connor becomes an org Owner** (after his first sign-in) and co-handles forge-bot, including
   acknowledging bulk changes. Urgent forge-bot alerts route **24/7**, not just in the weekday
   `homelab-default` window.
5. **Defaults stand:** a per-repo runner allowlist (forge-bot first), and Owners exempt from
   automatic prohibit or demote (an alert instead).
6. **NetworkPolicy is documented intent only.** On 2026-11-02 Chris confirmed that `kube-system`
   runs only kube-flannel (no Cilium, Calico or kube-router), so policies are not enforced. He
   accepts the egress policy on that basis.

## Design — forge-bot

**Shape** *(my call)*. One Go binary, one replica, stateless. Each cycle:

1. **Read** the SSO, Forgejo and the inventory.
2. **Plan**, as a pure function: (snapshots, map, policy) → changes.
3. **Guard** the plan with the safety rules below.
4. **Apply** in order: access, admin, teams, profile, avatar.

It runs a cycle at start, then every 5 min ± 30 s. Jobs share one interface, so later jobs slot in.
Thin hand-written HTTP clients cover the ~15 endpoints used: the fakes stay simple, and there are no
SDK gaps (`login_name`/`source_id`, `pronouns`).

**Identity and reads.**

- **SSO accounts.** An account is an SSO account when its `source_id` is the `realliance` login
  source (1, from config, checked at start). Its `login_name` is the OIDC subject, which is also the
  Keycloak user id.
- **Local accounts** on any other source are never touched: `forgejo-admin`, `forge-bot`,
  `forge-ci`.
- **Keycloak reads:**
  - every user, paged, `briefRepresentation=false`; the total must equal `users/count`;
  - the paged members of `/apps/forgejo`, `/forgejo-admins` and every group in `teams.yaml`;
  - its own client's admin events (see Mitigations).
- **Forgejo reads:**
  - `GET /api/v1/admin/users?source_id=1`, paged;
  - the members of managed teams and of Owners teams;
  - the inventory's `teams.yaml` and `forge-bot.yaml`, at the verified head sha.

| Job | Desired state, per `realliance`-source account | Write |
|---|---|---|
| access | `prohibit_login = !(enabled && in /apps/forgejo)`. **`enabled` is the user's flag only — a temporary brute-force lockout is not "disabled".** A subject absent from a *complete* listing has no access. | `PATCH /api/v1/admin/users/{u}` (+ `login_name`, `source_id`) |
| admin | `admin = in forgejo-admins` | same `PATCH` |
| teams | Each team in `teams.yaml` = the union of its groups' members who have accounts. Unlisted teams are untouched. | `PUT`/`DELETE /api/v1/teams/{id}/members/{u}` |
| profile | With access: `email` (only if `emailVerified`), `full_name` = "First Last" trimmed, `pronouns` | same `PATCH` |
| avatar | With access and public visibility: SSO `picture` = `https://git.realliance.net/<username>.png`; otherwise removed | Keycloak `PUT /users/{id}`, changing only `attributes.picture` |

My calls on the jobs:

- **Admin and teams follow groups, not access.** A disabled person stays in their teams but is
  prohibited, so offboarding is one change.
- **The avatar URL is the stable `/<username>.png`.** A new avatar then needs no write, and
  usernames are permanent. If the URL doesn't serve anonymously for limited or private profiles,
  those people get no `picture`.
- **Forgejo-side edits are reverted.** Email, name and pronouns changed in Forgejo, or a prohibit
  set by hand on someone the SSO allows, revert within a cycle. The runbook says to edit profiles
  in the SSO and to suspend with `active: false`.
- **Email conflicts are skipped.** An email already used by another account is skipped and counted
  as an error.
- **Lockout handling.** If Keycloak 26.8 ever reports a temporarily locked user as
  `enabled: false`, forge-bot must check `attack-detection/brute-force/users/{id}` and treat the
  lock as enabled. Otherwise anyone could get someone prohibited by failing their password ten
  times.
  *Amendment 2026-11-03 (forge-bot Task 10 re-review, Keycloak 26.8.1 source):* a temporary lock
  never sets `enabled=false` on 26.8.1, so the condition never holds and forge-bot does **not**
  consult brute-force state: `HasAccess = enabled && in /apps/forgejo`. An unconditional
  `enabled || temporarilyLocked` would let a *disabled* user keep a lock window open (wrong password
  every <=15 min) and regain Forgejo access. Re-check this on every Keycloak minor upgrade.

**The inventory moves to an Owners-only org** (Chris's answer 2; the workstream steps are my
calls). The new org is **`realliance-ops`**, with an Owners team only (`chris`, `connorjc`) and no
`members` or `maintainers` teams. It holds the inventory, a private repo.

1. **Create the org.** Add `realliance-ops` to `orgs.tf` and its Owners team (import, as in
   `teams.tf`) with `local.owners = ["chris", "connorjc"]`. Connor must have signed in once first.
2. **Transfer `realliance/forge` → `realliance-ops/forge`** by hand through
   `POST /repos/realliance/forge/transfer`. Then state surgery, because the provider would replace
   a `prevent_destroy` repo on an owner change:
   - a `removed { lifecycle { destroy = false } }` block for the old address, then an `import` of
     `realliance-ops/forge`;
   - the same for `forgejo_branch_protection.forge_main`.
   The builder confirms the provider's import ids.
3. **Redo the repo's protections and mirror.** Re-apply branch protection with the new org's Owners
   in the merge and approval whitelists. Re-create the GitHub SSH push mirror: a new key, swapped
   in as the deploy key on `github.com/realliance/forge`. Verify that a push lands.
4. **Repoint every reference.**
   - local clone remotes;
   - the inventory README, `docs/forgejo.md`, and `tests/forge/test_forge_inventory.py`;
   - forge-bot's config, `inventory: realliance-ops/forge`.
   Forgejo redirects the old path, but nothing should rely on that.

**forge-bot stays at `realliance/forge-bot`** (decided). Declare it in the inventory as
`forgejo_repository.forge_bot` (`prevent_destroy`, public). Branch protection on `main`: no direct
push, signed commits, Owners-only merge and approval whitelists, fast-forward only. Protect `v*`
tags for Owners, by hand if the provider can't. Members can still push other branches; the
resulting risk is accepted under "Security model".

**Trusted inventory** (Chris's answer 2; the mechanics are my calls):

- **Verification.** Each cycle forge-bot resolves the head sha of the inventory's `main`. It fetches
  the commit's signature and payload (`GET /repos/{o}/{r}/git/commits/{sha}`, `verification`) and
  **verifies them locally** with OpenPGP against its own keyring.
- **Never Forgejo's word.** It ignores Forgejo's `verified` flag and never trusts Forgejo's
  signing key, so a web edit or a Forgejo merge does not count.
- **Pinned reads.** It reads `teams.yaml` and `forge-bot.yaml` at that exact sha.
- **The keyring.** It holds the Owners' armored public keys plus a fingerprint allow-list, and
  lives in forge-bot's luma-homeops ConfigMap, **not in the inventory**. Whoever controls the repo
  must not also control who may sign it — the same split as the SSO box's `trusted.txt`. Chris adds
  Connor's key.
- **When the head isn't Owner-signed:** the teams job is skipped, `allow_bulk_until` is ignored,
  and `ForgeBotInventoryUntrusted` fires.

**`teams.yaml`** (format my call):

```yaml
# SSO group -> Forgejo team, read by forge-bot every cycle. Teams not listed are never touched.
teams:
  realliance/members:     [realliance]              # flat SSO group names, as in the groups claim
  realliance/maintainers: [realliance-maintainers]
```

Any of these skips the teams job for that cycle, with an alert:

- a parse error or an empty map;
- an unknown team, or an `Owners` team;
- an SSO group that is missing or ambiguous.

The other jobs still run. A team removed from the file becomes unmanaged; it is never emptied.
`forge-bot.yaml` holds only `allow_bulk_until`, so either Owner can acknowledge a bulk change with
a signed commit.

**SSO side** (PRs to `realliance/sso`). Both points are my calls, and they correct the earlier "like
`tofu-plan`" shorthand:

- **The `forge-bot` client** is created the way `tofu-plan` is. Add
  `{ client_id: forge-bot, secret_var: forge_bot_client_secret }` to the "Automation clients" loop
  in `host/roles/master_realm`, with the secret in sso-secrets beside the `tofu-*` ones. In
  `realm/automation.tf`, add a `keycloak_openid_client` data source plus
  `keycloak_openid_client_service_account_role` grants on `realliance-realm`, exactly like
  `tofu_plan_view`. The grants are `view-users`, `query-users`, `query-groups`, `manage-users`, and
  `view-events` (for the self-check). Because the client lives in master, the realliance-realm
  lockdown (`test_no_user_holds_a_management_role`) stays true.
- **Declare `picture` in `realm/user_profile.tf`.** Users may view it and only admins edit it,
  because Keycloak drops undeclared attributes. Confirm the `profile` scope keeps the stock
  `picture` mapper.

**Mitigations for `manage-users`** (decided; mechanics my calls):

- **Secret handling.** The secret exists only as a SealedSecret in `forge-bot` plus its canonical
  copy in sso-secrets. It is never in env dumps or logs.
- **One-field allow-list.** The Keycloak client package exports exactly one write,
  `SetPicture(userID, value)`. It GETs the user, changes only `attributes.picture`, and PUTs.
  - A unit test asserts that the PUT body equals the GET body apart from `attributes.picture`.
  - A test fails the build if the package grows any other non-GET request.
- **Audit.** Every SSO write is a JSON audit line. Keycloak's admin events record it as well.
- **Self-check alert.** Each cycle forge-bot reads the admin events of its own client. It raises
  `ForgeBotUnexpectedSSOWrite` for anything other than a `USER` `UPDATE` on `users/<id>`, or for an
  `UPDATE` whose email, username, enabled, emailVerified or required actions differ from forge-bot's
  previous snapshot of that user.
  - This catches bugs and a secret stolen and used elsewhere.
  - A compromised pod could silence it, so the operator-run acceptance test (Success 7) is the
    independent check.
- **NetworkPolicy** (decided). Egress is allowed only to DNS, the Forgejo pods in `forgejo`
  (port 3000) and `sso.realliance.net` (23.163.0.48/32, port 443). forge-bot needs no Kubernetes
  API, and it runs with no service-account token.
  - **Plainly: galaxy runs only kube-flannel, which does not enforce NetworkPolicy** (verified
    2026-11-02; see also `docs/gramps.md` and `docs/telephony.md`). This object records intent and
    is not a control. Chris accepts that; it becomes a control only if the CNI ever changes.

**Safety rules.**

| Rule | Behaviour |
|---|---|
| Complete reads or nothing | A job writes only if every read it needs fully succeeded: all pages, matching counts, every group resolved, and a verified inventory head for the teams job. Any error, timeout, 5xx, auth failure or short page means those jobs write nothing that cycle; a metric and a log line record it. |
| Empty is a failure | `/apps/forgejo` with no enabled members, or Forgejo listing no `realliance`-source accounts, counts as a failed read. |
| Circuit breaker | Per cycle, **losses** (prohibit, admin or team removal) may touch at most **3 people and at most ⅓ of those with access**. Grants are capped at 10, and profile + avatar at 25. Over a limit, that category applies **nothing** and `ForgeBotBreakerTripped` fires. Override: `allow_bulk_until: <RFC 3339>` (≤ 24 h ahead, self-expiring), in a signed inventory commit. The numbers and the Success-8 mechanics are the builder's calls. |
| Protected | No writes to local accounts or Owners teams. An Owners-team member is never prohibited or demoted; `ForgeBotOwnerNeedsAction` fires instead (decided). |
| Modes | Each job is `off`, `dry-run` or `apply`, set in the luma-homeops ConfigMap; the default is `dry-run`. |
| Level-triggered | Compare, then write: a steady state means zero writes. Failed writes retry next cycle. Cycles never overlap. |
| Audit | One JSON line per change or planned change (cycle, job, account, field, old → new, reason), going stdout → fluent-bit → Azure Log Analytics. |

**Cutover from sign-in mapping, with no churn and no gap.**

1. Make `teams.yaml` equal the login source's current map (`forgejo admin auth list` on the pod).
2. Run every job in `dry-run` for ≥ 24 h. Gate: no read errors, and every planned admin or team
   change is real drift, noted in the ledger.
3. Move `profile`, then `avatar`, then `access` to `apply`: one commit each, watching one cycle
   after each.
4. **Only after the inventory move and the signed-head check are live**, move `admin` and `teams`
   to `apply` while the login source still maps. Both writers read the same SSO state and agree.
5. Once a cycle plans zero admin or team changes, Chris clears the map, the removal flag and the
   admin group with `forgejo admin auth update-oauth --id 1 …`. Record before and after in the
   runbook.
6. Drop the map from the `forgejo-release.yaml` comments, then update the tests and docs.

**Rollback:** set the jobs back to `dry-run` and restore the map with the recorded command. Two
writers never hold different maps, and there is never no writer. **Accepted cost:** a new person
gets their teams within one cycle of first sign-in, not at sign-in.

**Deployment** *(my calls)*.

- **Namespace and app:** `forge-bot` (baseline), Argo app `forge-bot` (`cluster/forge-bot/` +
  `cluster/applications/forge-bot.yaml`).
- **Pod:**
  - 1 replica, `Recreate`;
  - distroless, non-root, `readOnlyRootFilesystem`, all capabilities dropped, no service-account
    token;
  - requests 20m/32Mi, limit 128Mi.
- **Endpoints:** `/metrics` and `/healthz`. Liveness fails after 3 intervals without a completed
  cycle.
- **Image:** `git.realliance.net/realliance/forge-bot:sha-<short>@sha256:…`.

| Identity | Rights | Secret (canonical in sso-secrets) |
|---|---|---|
| Forgejo local `forge-bot`, site admin, created on the pod like `forgejo-admin` *(my call: not the break-glass account)* | token: `read:admin`, `write:admin`, `write:organization`, `read:repository`, `read:user` | `FORGE_BOT_FORGEJO_TOKEN` → SealedSecret |
| Keycloak master client `forge-bot` | `realliance-realm` view/query users and groups, `manage-users`, `view-events` | `forge_bot_client_secret` → SealedSecret |
| Forgejo local `forge-ci`, **non-admin** *(my call: a site admin bypasses package ownership)* | in `realliance` team `ci`, `repo.packages: write` only (declared in the inventory) | `FORGE_CI_PACKAGE_TOKEN` → Actions secret (repo or org, per B0) |
| Runner registrations | one allowlisted repo each | `forge/runner.env` → SealedSecret in `forgejo-runner` |

**Observability.** A ServiceMonitor labelled `release: prometheus`, and a PrometheusRule labelled
`prometheus: kube-prometheus`, `role: alert-rules`, `release: prometheus`.

- **Metrics:** `forge_bot_cycle_last_success_timestamp`, `forge_bot_read_ok{source}`,
  `forge_bot_planned_changes{job}`, `forge_bot_applied_changes_total{job,kind}`,
  `forge_bot_errors_total{job}`, `forge_bot_breaker_tripped{job}`, `forge_bot_job_mode{job}`. No
  user, org or team names appear in labels.
- **Urgent alerts, routed 24/7 to a receiver both Owners see** (decided):
  - `ForgeBotBreakerTripped`;
  - `ReallianceSSOUnreadable` (SSO reads failing for 30 min — galaxy's only watch on the SSO box);
  - `ForgeBotOwnerNeedsAction`;
  - `ForgeBotUnexpectedSSOWrite` *(my addition)*.
- **Weekday alerts, to `homelab-default`:**
  - `ForgeBotStale` (no completed cycle in 30 min);
  - `ForgeBotNotConverging` (a job in `apply` has had planned changes for 3 cycles);
  - `ForgeBotInventoryUntrusted`.
- **Step for Chris:** the route lives in the sealed `cluster/prometheus-stack/alertmanager-config`,
  which only Chris edits. Match `alertname=~"ForgeBot(BreakerTripped|OwnerNeedsAction|UnexpectedSSOWrite)|ReallianceSSOUnreadable"`
  to a receiver with no time window. Prove it with a test alert outside weekday hours.

## Design — minimal CI

**Forgejo config.** In `forgejo-release.yaml` under `gitea.config.actions`: `ENABLED: true` and
`DEFAULT_ACTIONS_URL: https://github.com`. Logs and artifacts stay on B2, with
`ARTIFACT_RETENTION_DAYS: 30` *(my call)*.

**The runner** *(my calls unless marked)*:

| Item | Choice |
|---|---|
| Image | luma-homeops `images/forgejo-runner/`, built by the existing GitHub Actions pattern → ghcr `:sha-…@sha256`. Contents: forgejo-runner **v13.2.1** (decided), git, Go, ko, Node (host-mode JS actions need it), and Nix (the first thing to trim; if kept, its store lives on the work volume). |
| Labels | Host mode (decided), with the single label `galaxy:host`. **No** `ubuntu-latest`/`docker` labels, so GitHub-style workflows wait. |
| Placement | Namespace `forgejo-runner` (baseline), 1 replica, `Recreate`, preferred anti-affinity to `top` (thermals, #2603). |
| Pod | uid 1000, `runAsNonRoot`, no privilege escalation, capabilities dropped, `RuntimeDefault` seccomp, no service-account token, `enableServiceLinks: false`. |
| Limits | `capacity: 1`, `runner.timeout: 1h`. Requests 250m/768Mi, limits 2 CPU / 3Gi. `GOMAXPROCS=2`, `GOFLAGS=-p=2`, nix `--cores 2`. |
| Storage | PVC `forgejo-runner-work`, 15Gi on `rook-ceph-block-ssd` (`discard`): workspaces, the built-in cache, `GOCACHE`/`GOMODCACHE`. Never the SMR pool; no big emptyDir on the etcd NVMe. Check `ceph df` first. |
| Config | `config.yml` is a SealedSecret: its `server.connections` hold tokens. |
| Updates | Renovate bumps the Dockerfile → CI builds the image → manual re-pin. |

**Allowlist** (decided). Since v12.7, forgejo-runner holds several registrations in one daemon
(`server.connections`). Each allowlisted repo gets an offline registration:
`forgejo forgejo-cli actions register --scope <owner>/<repo> --secret <40 hex>` on the Forgejo pod,
plus a matching connection entry. The builder confirms the `--scope` form. **The repo must exist
before it is registered.** v1 allowlists only `realliance/forge-bot`.

**Security model, plainly.** Jobs run as the runner's uid in its pod. From there they can:

- read the connection tokens. A leaked one lets someone impersonate the runner and receive its
  jobs and secrets.
- poison the shared caches;
- reach whatever a pod reaches: every ClusterIP Service, and the LAN via SNAT, including the BMCs
  and the KVM's unpatched port.

**Accepted risk (decided by Chris).** Any `realliance` member can push a branch to
`realliance/forge-bot`. The workflow in that branch then runs on galaxy's runner and can read the
repo's Actions secrets, including the registry push token. Forgejo 15 has no environments or
protected secrets, so nothing prevents this.

**Mitigations:**

- **No image auto-deploys.** Images reach galaxy only after Chris reviews the diff and re-pins by
  digest in luma-homeops, so a rogue image is never deployed automatically.
- **The registry token is narrow.** It can push packages only (no repo or admin scopes), and it is
  rotated if misuse is suspected.
- **The runner holds no other credentials.** It has no Forgejo admin token and no Keycloak secret;
  those live only in the forge-bot Deployment's SealedSecrets.
- **State doesn't persist.** Every job gets a fresh workspace (the builder makes sure it is wiped),
  and the runner pod can be recreated to clear everything else.
- **The rest:**
  - the allowlist;
  - no GitHub-style labels;
  - no Kubernetes token;
  - SHA-pinned actions;
  - rotating the registration by re-registering with a new secret suffix;
  - never approving fork-PR workflows.
- **Revisit trigger.** If anyone not fully trusted ever gets `/realliance`, this is revisited.

The runner gets no NetworkPolicy, because flannel would not enforce one. The real fix is
ephemeral VMs, in the forge CI project.

**Package pushes: prototype B0** (decided; details my call).

1. On `forge-ci`, create a *specific repositories* token for `realliance/forge-bot` with
   `write:package`.
2. With `crane` or `skopeo`, push a tiny image to `git.realliance.net/realliance/forge-bot`. It
   must succeed.
3. Push to `realliance/other` and to `<someone>/x`. Both must be refused.
4. If all three behave, the token becomes forge-bot's **repo** secret `REGISTRY_TOKEN`. If not,
   `forge-ci` gets a plain `write:package` token as an **org** secret; team `ci` confines it to
   `realliance`.
5. Record the result in the ledger and the runbook.

Images are public with the org, so the Talos nodes pull them anonymously over LAN reflection.
Check whether container blobs redirect to B2 (`SERVE_DIRECT`); either answer is fine.

**forge-bot's pipeline** (`.forgejo/workflows/ci.yaml`, `runs-on: galaxy`, SHA-pinned actions;
the triggers are decided):

- **Triggers:** `on: push` for `main` and `v*` tags, and `pull_request`. Fork PRs are never
  approved. This does not stop a member's branch that edits its own workflow; see the accepted
  risk.
- **Steps:** `go vet` and `go test -race ./...` on every run. On `main` and tags, also
  `ko build --bare` → `:sha-<short>`, with the base image digest-pinned in `.ko.yaml` and the
  digest printed in the job summary.
- **Re-pin (decided):** before each luma-homeops re-pin, Chris runs `git verify-commit` on the new
  sha and reviews `git log -p <pinned>..<new>`. The pin commit names the reviewed range.

**Bootstrap.** The runner's image comes from luma-homeops' existing GitHub pipeline, so the runner
exists first and builds forge-bot's first image. Fallback: an Owner runs `ko build` with the
`forge-ci` token.

**Forgejo 15 Actions differences** for workflow authors:

- no environments or required reviewers, and no `permissions`;
- job-level `concurrency` is ignored;
- the cache is per runner;
- artifacts v4 only via `data.forgejo.org/forgejo/{upload,download}-artifact@v4`;
- no Kubernetes executor; build images with ko, or nix + skopeo — never DinD or rootless BuildKit.

Matrix, `needs` outputs, `schedule`, `workflow_dispatch`, reusable and composite workflows,
`services` and LFS all work. The registration, ephemeral and waiting-jobs APIs exist for the later
scaler.

## Order of work

| Step | What | Approval |
|---|---|---|
| A1 | SSO: `forge-bot` client (Ansible loop + `automation.tf` grants), `picture` in the user profile | PRs to sso; Chris and Connor merge |
| A2 | Forgejo: the `forge-bot` and `forge-ci` accounts and tokens. Inventory: org `realliance-ops`, transfer `forge` (state surgery, mirror, protection), `realliance/forge-bot` declared critical, team `ci`, `teams.yaml`, `forge-bot.yaml` | Owners |
| B0 | Package-token prototype (needs the forge-bot repo from A2) | — |
| B1–2 | Runner image, `[actions]` config, runner deployed and registered for `realliance/forge-bot` (**the repo must already exist**) | Chris |
| C1 | forge-bot code and unit tests; first CI image; diff reviewed and pinned | Chris |
| C2 | Alertmanager 24/7 route (sealed config); forge-bot deployed, all jobs `dry-run` | Chris |
| C3–6 | Enable profile → avatar → access; then admin + teams (after A2); clear the login-source map; tests; docs | Chris clears the map |

Nothing touches the router, Talos, Ceph or DNS. Rollback is reverting two new Argo apps.

## Tests

**Acceptance tests** go in `realliance/sso` `tests/forge/` (`test_forge_bot.py`,
`test_forge_ci.py`). They are external and black-box.

- **Waiting** *(my call)*: `wait_for(pred, 2 × interval + 60 s)`, with fixtures batched.
- **Rewrite** the sign-in-sync tests to read "within a cycle, without sign-in":
  `test_team_follows_groups_at_signin`, `test_admin_status_dropped_at_next_signin`,
  `test_demotion_needs_the_forgejo_side_step`.
- **Keep** `test_team_map_not_in_public_config`, which already passes, and
  `test_self_set_profile_attributes_survive_apply` (#30).
- **Static checks** on the luma-homeops manifests and the forge-bot workflow:
  - nothing privileged, no Docker socket, no hostPath;
  - no `ubuntu-latest` label, no service-account token;
  - the forge-bot NetworkPolicy is present;
  - the workflow triggers are only `main`, `v*` and `pull_request`.

**Unit tests** in forge-bot, all under `-race`:

- **Planner:** every job and edge case, including a temporary lockout that keeps `enabled`.
- **Guards:** partial or empty reads; each breaker limit; override expiry.
- **Signature check:** fixtures signed by an Owner, by Forgejo, by an unknown key, and unsigned.
- **`teams.yaml` parser.**
- **The `picture`-only writer:** the body-diff test, plus the no-other-writes test.
- **Self-check rules.**
- **Clients:** against `httptest` fakes.

## Docs to update

- **`docs/forgejo.md`:** access, admin and teams now sync within ~5 min, and removal is one SSO step,
  with manual fallback while forge-bot is down or its breaker has tripped. Email syncs. The
  hidden-org gate is met. The inventory lives at `realliance-ops/forge`. New "forge-bot" and "CI"
  sections cover modes, override, alerts, rotation, rollback, the allowlist, the fork-PR rule and
  the re-pin review.
- **`forgejo-release.yaml`:** update the comments.
- **`realliance/sso`:** the Lockdown README and the comments that say forge-sync.
- **Wiki:** Forge-Operations, Gotchas and Accounts-and-Profiles.
- **Inventory README:** its new path, `teams.yaml`, and the critical repos.

## Success looks like

Each item has a test or a named drill. "≤ 10 min" means two cycles.

1. **Offboarding.** Someone set inactive, or removed from `/apps/forgejo`, loses web, token and SSH
   access in ≤ 10 min. Reversing it restores all three in ≤ 10 min.
2. **Admin.** A change to `forgejo-admins` flips `is_admin` in ≤ 10 min, without a sign-in.
3. **Teams.** A change to `/realliance` or `/realliance-maintainers` moves team membership in
   ≤ 10 min. Owners teams and unlisted teams never change.
4. **Sign-in writes nothing.** With teams deliberately set wrong, a sign-in leaves them wrong, and
   the next cycle fixes them.
5. **Profile.** A verified SSO email, name or pronouns change appears in Forgejo in ≤ 10 min. An
   unverified email never does.
6. **Avatar.** Within ≤ 10 min of first sign-in, `picture` = `https://git.realliance.net/<user>.png`.
   The URL serves anonymously and appears in an ID token with `profile`.
7. **Steady state.** An hour without SSO changes produces zero writes. Every `forge-bot` admin event
   in Keycloak is a `USER` `UPDATE` that changes only `picture`.
8. **Breaker.** If 4 test users lose access in one cycle, none is prohibited. With
   `allow_bulk_until` set in a signed commit, all four are prohibited by the next cycle.
9. **Lockout.** A test user locked by 10 wrong passwords is **not** prohibited after two cycles.
10. **SSO outage drill** (Chris). With the SSO URL made unreachable, there are zero changes and
    `ReallianceSSOUnreadable` fires in ≤ 30 min.
11. **Untrusted-head drill** (an Owner). A `forge-bot.yaml` edit made in the Forgejo web UI (signed
    by Forgejo) is ignored and `ForgeBotInventoryUntrusted` fires. A signed revert clears it.
12. **Foreign-write drill** (Chris, with the SA secret). A non-`picture` change to a test user
    made with forge-bot's credentials fires `ForgeBotUnexpectedSSOWrite` within a cycle.
13. **Protected accounts.** `forgejo-admin`, `forge-bot` and `forge-ci` are never prohibited, and no
    Owner is prohibited or demoted automatically.
14. **CI.** A push to `realliance/forge-bot` `main` runs the tests on the galaxy runner and
    publishes `:sha-<short>`, which galaxy pulls and runs once Chris has reviewed and re-pinned it.
15. **Allowlist.** `runs-on: galaxy` in a non-allowlisted repo, and `runs-on: ubuntu-latest` in an
    allowlisted one, are both still waiting after 10 min.
16. **Package scope.** The registry token cannot push outside its scope (per B0).
17. **Hardening.**
    - The static checks pass and `forgejo-runner` enforces baseline.
    - A non-Owner cannot push to `realliance-ops/forge` or to `realliance/forge-bot` `main`.
    - The runner pod's environment and files contain no Forgejo admin token and no Keycloak secret.

## Left to the builder

- Go layout and libraries (stdlib HTTP preferred), logging, config format, page sizes and
  timeouts.
- Breaker numbers and the mechanics of the Success-8 test.
- How the self-check holds its previous snapshot across restarts.
- The runner image: a Dockerfile or a Nix build, and whether Nix stays.
- PVC size.
- Package cleanup and package-to-repo linking.
- Whether team `ci` needs `includes_all_repositories`.
- The licence (default MIT OR Apache-2.0).

## Open questions for Chris

None. All were answered on 2026-11-02:

- the repo path stays `realliance/forge-bot`, with its risk accepted;
- NetworkPolicy is accepted as documented intent.
