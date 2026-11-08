# SSO member invites implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a Realliance member hands someone a link, that person fills in a form, and within 10 minutes a signed ledger commit has become a guest account (SSO plus forge) that the SSO box created, with every rule enforced on the box.

**Architecture:**
- **The ledger:** a private `sso-invites` repo in a new Owners-only GitHub org, holding `ledger.yaml`.
- **The box check:** `sso-ledger`, one Python file in public `realliance/sso` that `sso-config-apply` runs before the people apply. It verifies every new ledger commit against its own keyring (the ledger key and the operators, never web-flow) and checks schema, transitions, quotas, caps and collisions on the box's clock. It hands the admitted guests to the `sso-people` layer as a var-file, and it ships in `check` mode first.
- **The service:** `sso-invites`, a small Go service on galaxy at `invite.realliance.net`. It signs members in through a new `invites` client, writes each link and redeemed link as a commit signed with the ledger key, reads Keycloak through a view-only service account, and alerts on outcomes it can see.
- **Forge access:** guests reach the forge last, after a Forgejo storage cap.

**Tech Stack:** the box runs Python 3.12 with PyYAML (the box's `/opt/ansible` virtual environment), GnuPG, git, bash and `systemd` on AlmaLinux 9; OpenTofu 1.12.6 with `keycloak/keycloak ~> 5.10.0`; `ansible`; Keycloak 26.8.1; Go 1.26 (toolchain go1.26.10) with `go-git/v5`, `ProtonMail/go-crypto`, `go.yaml.in/yaml/v3`, `coreos/go-oidc/v3`, `golang.org/x/oauth2`, `prometheus/client_golang`; ko plus distroless; Forgejo Actions on the `galaxy` runner; Argo CD, SealedSecrets, Traefik v3, kube-prometheus-stack; pytest with `pytest-testinfra` in `realliance/sso`.

**Spec:** `docs/superpowers/specs/2026-11-07-sso-member-invites-design.md` (approved by Chris 2026-11-08). Read it with this plan. Related specs: `2026-10-31-realliance-sso-design.md` (with its 2026-11-02 amendment), `2026-11-02-forge-bot-and-ci-design.md`, `2026-11-04-runner-classes-design.md`.

## Global Constraints

### From the spec (verbatim values)

- **Git creates the account.** No reconciler, no Keycloak-first write, Organizations off. The service writes only to the ledger; Keycloak's `invite-user` sends the set-up mail (72 h link).
- **The ledger** lives in a new Owners-only GitHub org, never the `realliance` org: the two operators as owners, base permission none, private forks off, no GitHub Apps. The service writes with a write deploy key on this repo alone, operators write as owners, and the box reads with its own read-only deploy key. For the ledger the box trusts only the ledger's own signing key and the operators' keys, **never web-flow**.
- **Schema.** `version` (`1`, operator only); `links[]`: `id`, `inviter`, `created`, `expires` (at most 7 days after `created`), `token_sha256`, `state` (`pending`, then `redeemed` or `revoked`), `redeemed`, `revoked`, `guest` {`username`, `bootstrap` {`email`, `first_name`, `last_name?`}, `mfa` (`webauthn` or `totp`, never empty or `none`), `active`}. Unknown keys are refused, and no `groups` key exists at any level. Only an operator's erasure rewrite removes anything.
- **Who may sign what:** the spec's "Change" table. The ledger key may add a `pending` link, move `pending` to `revoked` or to `redeemed` with a `guest` block, and set `guest.active` from `true` to `false`. Everything else (reactivation, an immutable field, a removal, a history rewrite) is operator only.
- **Box checks and outcomes:** the spec's "Rules on the box" table. A bad signature, a non-fast-forward head that no operator signed, or a schema or transition error refuses the head. A bad new guest refuses the entry; over a rate cap defers it.
- **Quotas.** An active person in `/realliance`: 5 per rolling 7 days, a pending link or a redemption holding a slot for 7 days. Any other active person in `people.yaml`: 1 for life, a pending link or a redemption holding it for good. A revoked or expired link frees its slot; deactivating or promoting the guest doesn't. A guest or an inactive person: 0, and their pending links die at redemption. Everyone together: 10 redeemed links per rolling 24 h; one over the cap waits for a later run.
- **Modes.** `ledger_mode` reads `check` or `apply`. `check` runs live for at least 3 days, a week if possible, before the switch. In `check` the box logs what it would apply, ids only, and hands the people layer no guests.
- **Members first.** No failure in the ledger path ever blocks the `people.yaml` apply, in either mode. The box never drops a guest already in the people layer's state, and never re-checks one.
- **Statuses:** `sso/apply` on each ledger head the box checks. A refused head freezes the ledger until an operator-signed head fixes or reverts it.
- **Clock and state:** caps and expiry count from when the box first saw each link and redemption. State lives in `/var/lib/sso-config-apply` with no backup; a rebuild can let at most one extra day's cap through.
- **Guest scope:** `/guests` and `/apps/forgejo` only, fixed in `guests.tf` in `sso-people`. `/apps/forgejo` comes only after the storage cap or its fallback.
- **Roles:** the `invites` client in `realm/apps.tf`. Role `invites.user` goes to `/apps/invites`, which the people layer adds to every `people.yaml` entry and never to a guest. Role `invites.member` maps to `/realliance`. No `groups` scope.
- **The service account:** a master-realm service account `invites` with `view-users` and `query-users` on `realliance`, nothing else.
- **The service:**
  - Go, one replica, namespace `sso-invites`, Ingress `invite.realliance.net`, ko plus distroless, a transport guard, `slog` audit lines with ids only, metrics, no database.
  - Git over SSH only for writes. Its only GitHub API access reads commit statuses on `sso-invites`.
  - Tokens are 256-bit and single use. The MFA choice offers `webauthn` (preselected) or `totp` and nothing else.
  - After 20 min with no account, the status page tells the invitee to contact the member who invited them.
- **Alerts:**
  - a ledger head that the box refused, or that still lacks a success status, more than 1 h after its push;
  - a redemption with no Keycloak user after 20 min;
  - failing ledger pushes;
  - each guest deactivation;
  - orphans (guests whose `inviter` is no longer active).
- **Storage cap:** a Forgejo quota for guests across git, LFS and packages, about 2 GiB in all, with the spec's three-step ladder.
- **Mail:** the free tier of Resend, 100 mails a day, shared with resets and the forge.
- **Galaxy:** Argo GitOps, SealedSecrets, one Ingress with a host, `release: prometheus` on every ServiceMonitor, alerts as a `PrometheusRule`, images pinned by digest. **No secrets in `realliance/sso`**, not even encrypted.
- **Personal data:** no email, name or token in a log line, a metric label, a commit status, public `realliance/sso` or the Azure log table, Traefik's access log included.

### Execution rules (Chris, for this plan)

- Run it subagent-driven: one implementer and one reviewer per task.
- **Four threads** for local builds and tests:
  - Go: `GO="taskset -c 0-3 nix develop $HOME/Repos/luma-homeops --command env GOMAXPROCS=4 go"`, then `$GO test -race -p=4 ./...`;
  - pytest: `taskset -c 0-3 nix develop ~/Repos/realliance-sso-invites --command pytest …`;
  - nix builds: `--cores 4`.
  - No local Docker or kind: CI builds images.
- Never `--no-verify`, never `--no-gpg-sign`, **never force-push**. Any force-push a procedure needs (a drill, an erasure) is Chris's step.
- Never open `~/Repos/realliance-sso-secrets`, any `*.secret.*`, `nodes/*` or a key file, and exclude them from every grep (`grep -r --exclude='*.secret.*' --exclude-dir=nodes …`). Programs that consume them (pytest's `conftest`, `./sign.sh`, `kubeseal`, `ansible` on the box) may run; a `python -I` script writes a secret file and prints a byte count.
- No secret or personal data in argv, logs, test output or the progress ledger. Never print `ledger.yaml` or `people.yaml`: a `python -I` script prints counts and link ids instead. In git-crypt repos use `git diff --no-textconv`.
- Every commit ends with these two lines:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01VEim3itci5Wu1hTGS8MxW4
  ```
- Docs prose passes vale (`nix shell nixpkgs#vale -c vale <files>`).

### How changes land

| Repo | Where | How |
|---|---|---|
| luma-homeops | worktree `~/Repos/luma-homeops-invites`, branch `sso-invites` | Create it with the git-crypt recipe: `git -C ~/Repos/luma-homeops worktree add --no-checkout ~/Repos/luma-homeops-invites -b sso-invites origin/main`, copy `.git/git-crypt` into `.git/worktrees/luma-homeops-invites/`, then `checkout`. Commit with `nix develop -c git commit -S`; land with `git fetch origin && git rebase origin/main && git push origin HEAD:main`. A push that changes what Argo renders is a deploy, and **every deploy is a STOP**. Docs, the progress ledger and `scripts/` pushes are routine. |
| `realliance/sso` (GitHub, public) | worktree `~/Repos/realliance-sso-invites`. Tasks 2–4 share branch `invites-ledger-core` and land as one PR in Task 4; each later task names its own branch | Signed commits, `gh pr create --fill`. **Chris** merges with `gh pr merge --squash --delete-branch`. The rule set requires a PR, so every head on `main` is web-flow-signed on Chris's merge. That is what this plan calls an *operator-signed `realliance/sso` head*, which the box accepts as before (the spec's accepted risk). A merge that changes `host/` or `realm/` is a **box-head STOP**; a tests-only merge is **Chris:** merges. |
| `realliance/sso-people` (GitHub, private) | worktree `~/Repos/realliance-sso-people-invites`, branch per task | The same PR flow; **Chris** merges. Never open or print `people.yaml`. |
| The ledger, `<ledger-org>/sso-invites` (GitHub, private) | clone `~/Repos/sso-invites-ledger` | Chris's key signs operator commits, which go straight to `main` (no branch protection on GitHub Free); each push is a step of its task, and the first real one is a STOP. |
| `realliance-ops/sso-invites` (forge, public) | clone `~/Repos/sso-invites`, branch `v1` | Signed commits; **Chris** merges fast-forward (`fj -H git.realliance.net pr merge realliance-ops/sso-invites#<n> -d`) and signs the tags. |
| `realliance-ops/forge` (forge, private) | `~/Repos/realliance-forge`, branch per task | A PR; **Chris** merges fast-forward. An apply or script run follows its STOP. |

- `<ledger-org>` is the org Chris names at Task 0. Replace the placeholder everywhere once it's known.
- **STOP—ask Chris** is one AskUserQuestion. It states the change, the Gate G evidence where a task touches galaxy, what to watch and the rollback trigger. The options are `approve: do it, and roll back at once if a trigger fires` and `hold`. A rejected or interrupted call may already have run, so read live state before redoing anything.
- **Gate G** is phase 0–1's (`2026-11-04-runner-classes-phase-0-1.md`, G1–G4), before and after every galaxy deploy push.
- **Serialize STOPs** with every other plan in flight: no deploy here inside another plan's Gate G window.
- **Progress ledger:** `docs/superpowers/plans/2026-11-08-sso-member-invites.ledger.md`: answers, verdicts, timings, digests and rulings. It holds no secret, no email, no name and no hidden org, team or group name; link ids and usernames of throwaway guests only.
- `SCRATCH=~/Repos/luma-homeops/.superpowers/sdd/2026-11-08-sso-member-invites/scratch` (git-ignored). Define it in every shell that uses it.
- Waits over 60 s use `Monitor`, never a sleep loop. CLAUDE.md S1–S9 apply.

<!-- vale Google.Headings = NO -->
## Review Focus
<!-- vale Google.Headings = YES -->

1. **A guest record Keycloak rejects, holding members hostage.** A name with a character Keycloak's `person-name-prohibited-characters` refuses (`<`, `$`, `;`, `{`), an over-long name, or an email its email check refuses makes `tofu apply` fail for the whole people layer. Expected: the box refuses the entry before handing it over. If Keycloak still rejects it, the apply runs again without the new guests, `people.yaml` applies, and the entry is refused `apply-failed`. Tests: Task 3 `test_entry_refused_for_keycloak_prohibited_name`; Task 4 `test_new_guest_apply_failure_retries_without_it`.
2. **YAML that means something other than it looks.** Duplicate keys (PyYAML keeps the last one), anchors and aliases, unquoted timestamps or `yes`/`no` turned into other types, a 50 MB file. Expected: the box refuses the head with a schema reason, and the service never writes such a file. Tests: Task 2 `test_duplicate_key_refused`, `test_alias_refused`, `test_unquoted_timestamp_refused`, `test_oversize_refused`; Task 8 `TestMarshalRoundTripsThroughBoxVectors`.
3. **Case, whitespace and doubles.** `Bob@Example.COM ` against an existing `bob@example.com`, or two links redeemed with the same username in one push. Expected: emails compare trimmed and in lower case everywhere, and the link the box saw first wins while the later one is refused. Tests: Task 3 `test_email_collision_case_insensitive`, `test_same_username_twice_first_wins`; Task 11 `TestRedeemNormalisesEmail`.
4. **The token leaks at the edge.** Traefik's access log is on with default fields, so a token in a path or query lands in the logs, and it would also reach browser history and `Referer` headers. Expected: the token rides only in the URL fragment and the POST body, every page sends `Referrer-Policy: no-referrer`, and no log line holds a token. Tests: Task 12 `TestTokenNeverInPathOrQuery`, `TestReferrerPolicyNoReferrer`, `TestNoTokenInLogs`.
5. **The per-IP limit sees one IP.** Traefik's Service uses `externalTrafficPolicy: Cluster`, so kube-proxy may rewrite the source to a node address. One abuser could then throttle everyone, or the limit could never bite. Expected: the service also limits per token (5 attempts an hour) and globally (60 redeem attempts a minute), and Task 16 checks the access log's `ClientHost` before it trusts the Traefik limit. Tests: Task 12 `TestPerTokenAttemptLimit`, `TestGlobalRedeemLimit`; Task 16 Step 5 (read-only evidence).

---

## Decisions this plan makes

*(Plan's call, open to Chris's override at the first STOP that touches each.)*

- **D1. The box check is one Python file.**
  - Source: `host/roles/config_apply/files/sso_ledger.py`, installed as `/usr/local/sbin/sso-ledger` with shebang `#!/opt/ansible/bin/python3 -I`.
  - Runtime: the pull role's Python 3.12 virtual environment, which already carries PyYAML for ansible-core, so the box needs no new package. The code stays Python 3.12 compatible.
  - Tests: they import it from the file and run offline, with real git and GnuPG.
- **D2. Handing guests to the people layer.**
  - The box writes `/var/lib/sso-config-apply/ledger/guests.next.json` as `{"guests": {<username>: {active, mfa, bootstrap}}}` and runs `tofu apply -var-file=…`.
  - `guests.tf` declares `variable "guests"` (default `{}`) and `output "guests"` (sensitive).
  - **Admitted is append-only.** `state.json` `admitted` maps username to `{link, at, active}`, with no email, and nothing removes an entry but `sso-ledger forget`. The box hands every admitted guest on every run. Its `bootstrap` comes from the ledger at the effective head, or `null` when an erasure removed the entry: `ignore_changes` covers the profile, so it no longer matters.
  - **Rebuild:** a rebuilt box seeds `admitted` from `tofu output -json guests`.
  - **Retry:** if the apply fails while new guests are in the set, it runs again with the previous set. If that run succeeds, the box marks the new ones refused (`apply-failed`).
- **D3. Two ways a head passes.**
  - *Ledger-signed head:* it must fast-forward from the last good commit, with no merge commit. Every commit since then verifies, and the signer of each commit may make the change it carries.
  - *Operator-signed head:* a snapshot. Its tree must pass the schema, and the change from the last good tree counts as one operator change, whatever lies between. This is the spec's remedy for a refused head, and it also accepts an erasure rewrite.
  - *No last good commit* (first run, rebuild): walk the first-parent chain from the root and take the newest commit that passes. The chain must start at an operator-signed commit.
- **D4. The ledger key may change only `ledger.yaml`.** The box refuses a ledger-signed commit that touches any other path (`transition:path`).
- **D5. A new guest with `active: false` is never created.** This makes the erasure order safe, and it lets Task 15 neutralise the check-window test entries before the switch.
- **D6. Erasure order** refines the spec's steps 1–4, because deleting a Keycloak user that is still in tofu state makes the next apply recreate it:
  1. Set `active: false` through the ledger and wait for its `sso/apply`.
  2. On the box, holding the lock, run `sso-ledger forget <username>`, then `tofu state rm` for both addresses, then delete the Keycloak user.
  3. **Chris** runs `git filter-repo` and force-pushes a head he signed.
  4. Re-clone the box's checkout and both operators' clones.
- **D7. Operator-signed `realliance/sso` heads** are Chris's PR merges (How changes land). `ledger_mode` lives in `host/group_vars/sso.yml`, so only such a merge changes it.
- **D8. The link is `https://invite.realliance.net/i#<token>`.** The token is 32 random bytes as base64url without padding (43 characters) in the fragment, which never reaches Traefik. Every page sends `Referrer-Policy: no-referrer`.
- **D9. The service keeps the ledger in memory** (go-git memory storage, no volume) and pushes over `ssh://git@ssh.github.com:443/<ledger-org>/sso-invites.git` against a pinned host key, so its egress is TCP 443 only.
- **D10. The service deploys after the apply switch**, first with `open: false`: members can sign in, and the watcher and alerts run, but nobody can make a link. It opens after `/apps/forgejo` (Task 19).
- **D11. Storage cap path 1:**
  - Forgejo 15's `[quota] DEFAULT_GROUPS = guests` caps every account and org that has no explicit quota mapping. A `guests` group holds the 2 GiB rule.
  - An idempotent `quota.sh` in `realliance-ops/forge` maps every org and every non-guest account to an unlimited `members` group. It runs again each time a member joins and at each promotion. Until then the cap applies to a new member, the fail-closed direction.
  - **The facts:** `models/quota/group.go` `GetGroupsForUser` falls back to `setting.Quota.DefaultGroups` only when a user has no mapping, and orgs are users there.
  - A repo-storage jump alert ships on every path. Chris rules the path at Task 17's STOP.
- **D12. Latency.** `apply_interval` drops from 5min to 1min. `changed` mode skips the realm apply when `realliance/sso` hasn't changed, and runs the people apply when `sso-people`, the guest set or `realliance/sso` changed.
- **D13. The service's config lists its operators** by username. They may revoke any link and deactivate any guest, and nothing more.
- **D14. Orphans come from Keycloak.** An active guest counts as an orphan when the inviting member's account is off (`enabled: false`) or missing there. A promoted guest whose inviting member left also counts; the runbook says to ignore it.
- **D15. Box queries use `tofu-plan`** (view-only), its secret rendered into `/etc/realliance-sso/ledger-lookup.secret`.
- **D16. Formats.**
  - Usernames: `^[a-z][a-z0-9-]{1,30}[a-z0-9]$` with no `--`.
  - Reserved names: a built-in list plus private `reserved.yaml` in `sso-people`, which the service never sees. The service gets a public subset.
  - Emails: ASCII, at most 254 characters, compared trimmed and in lower case.
  - Names: 1–64 characters, none of Keycloak's prohibited characters, no control characters.
  - Ledger: at most 1 MiB, no aliases, no duplicate keys, timestamps as quoted `YYYY-MM-DDTHH:MM:SSZ`.
  - Link ids: `l-` plus 16 lowercase hex characters.
  - Expiry grace: 30 min.
- **D17. Mode scoping of box state.** `seen` and `last_good` persist in both modes; `admitted` and `refused` persist only in `apply`. A refusal clears when the entry's canonical hash changes.
- **D18. Quota order.** At redemption, the box admits a link if fewer of the inviting member's links hold a slot than the tier allows. Only links the box saw before this one count, so earlier links win.

## File map

| Path | Responsibility | Task |
|---|---|---|
| ledger repo: `ledger.yaml`, `README.md`, `schema.json` | the ledger | 1 |
| sso: `host/roles/config_apply/files/sso_ledger.py`, `tests/ledger/{conftest.py,test_schema.py,test_history.py,vectors/}` | parse, schema, signers, history | 2 |
| same file, `tests/ledger/{test_admission.py,test_cli.py}` | admission, quotas, clock, state, command line | 3 |
| sso: `host/roles/config_apply/{files/sso-config-apply.sh,tasks/main.yml,templates/ledger.env.j2,templates/ledger-trust.json.j2}`, `host/group_vars/sso.yml`, `operators.yml`, `keys/ledger/sso-invites-ledger.asc`, `realm/groups.tf`, `tests/ledger/test_apply_script.py`, `tests/host/test_config_apply.py` | the box runs the check | 4 |
| sso-people: `guests.tf`, `main.tf`, `reserved.yaml`, `README.md`; sso: `tests/people/test_people.py` | the guest layer | 5, 7, 18 |
| sso: `tests/invites_helpers.py`, `tests/invites/test_ledger_drills.py` | check-window drills | 6 |
| sso: `realm/modules/app/*`, `realm/apps.tf`, `realm/outputs.tf`, `realm/automation.tf`, `host/roles/master_realm/tasks/main.yml`, `tests/conftest.py`, `tests/realm/test_apps.py`, `tests/host/test_master_realm.py` | the `invites` client and service account | 7 |
| forge inventory `repos.tf`; service `go.mod`, `.ko.yaml`, `.forgejo/workflows/ci.yaml`, `deps_test.go`, `internal/{secret,ledger}/` | repo and ledger package | 8 |
| service `internal/gitstore/` | fetch, signed commit, push | 9 |
| service `internal/{keycloak,github}/` | guarded readers | 10 |
| service `internal/invites/`, `internal/audit/` | the rules | 11 |
| service `internal/web/`, `internal/limit/` | sign-in, pages, limits | 12 |
| service `internal/{watch,metrics}/`, `cmd/sso-invites/` | watcher, metrics, main, release | 13 |
| luma-homeops `cluster/sso-invites/*`, `cluster/applications/sso-invites.yaml`, `scripts/test_sso_invites_manifests.py`, `scripts/test-sso-invites-alerts.sh`, `.pre-commit-config.yaml`, `.renovaterc.json` | deploy | 14, 16, 19 |
| luma-homeops `cluster/applications/forgejo-release.yaml`, `cluster/forgejo/{prometheusrule.yaml,alerts_test.yaml,kustomization.yaml}`; forge `quota.sh`; sso `tests/forge/test_forge_quota.py` | storage cap | 17 |
| sso: `tests/invites/{test_invites.py,test_invites_box.py}` | acceptance | 19 |
| luma-homeops `docs/sso-invites.md`, `docs/forgejo.md`, `CLAUDE.md`; sso `docs/runbook.md`, `README.md`, `WISHLIST.md`; sso-people and ledger README files | runbooks | 20 |

## Coordination with concurrent plans

- **forge-bot** (`2026-11-02-forge-bot-and-ci.md`): until its `access` job runs in `apply`, guest offboarding prohibits login by hand (`docs/forgejo.md`). This plan writes nothing in forge-bot.
- **Docs site and Actions plans** (worktrees `luma-homeops-plan-docs`, `luma-homeops-plan-actions`): serialize STOPs; rebase right before any commit that touches `.pre-commit-config.yaml`, `.renovaterc.json` or `CLAUDE.md`.
- **Runner classes:** this plan only adds `deny:` entries for throwaway guests in Task 19 and removes them again.

---

### Task 0: Setup and answers

**Files:** create `docs/superpowers/plans/2026-11-08-sso-member-invites.ledger.md` (sections Answers, Baseline and one per task).

**Interfaces:** produces `<ledger-org>`, Chris's answers to the spec's open points, and the operators' SSO usernames (D13).

- [ ] **Step 1: Worktrees.** luma-homeops as How changes land says. Then `git -C ~/Repos/realliance-sso worktree add ~/Repos/realliance-sso-invites -b invites-ledger-core origin/main` and `git -C ~/Repos/realliance-sso-people worktree add ~/Repos/realliance-sso-people-invites -b guests origin/main`. Expected: clean trees.
- [ ] **Step 2: Cross-plan reads.** Ledger: whether `cluster/forge-bot` is on `main` and its `access` job's mode; NetworkPolicy enforcement state; open STOP windows in other plans.
- [ ] **Step 3: STOP—ask Chris** (Chris's step). One question set:
  1. The org: a new dedicated one (recommended), or the planned `realliance-ops` GitHub mirror org if its only members are the two operators and no mirror credential reaches `sso-invites`. Its name.
  2. He creates it and the private repo `sso-invites` as "The ledger" says: owners only, base permission none, private forks off, no GitHub Apps, fine-grained tokens allowed.
  3. Open point 1: take a deactivated guest's Pages sites down (default) or keep them.
  4. Open point 3: a 7-day link lifetime (default), and whether a promoted guest gets their new tier's quota (default yes, as "Promotion" says).
  5. The operators' SSO usernames for the service.
  6. A dedicated test member `invites-test` in `people.yaml` (Task 19) with `/realliance`, its password randomised at each test run.
- [ ] **Step 4: Read the org's settings back.** Run `gh api orgs/<ledger-org> --jq '{p: .default_repository_permission, f: .members_can_fork_private_repositories}'`, which should print `none` and `false`. Then check `gh api orgs/<ledger-org>/members --jq length` (the operators only), `gh api orgs/<ledger-org>/installations --jq .total_count` (`0`) and `gh api repos/<ledger-org>/sso-invites --jq .private` (`true`). Ledger the outputs.
- [ ] **Step 5: Commit and push** the progress ledger and this plan to luma-homeops `main` (docs only): `docs: SSO member invites plan and progress ledger (Task 0)`.

### Task 1: Keys, tokens and the ledger's first commit

**Files:** ledger repo `ledger.yaml`, `README.md`, `schema.json`.

**Interfaces:**
- Consumes `<ledger-org>` (Task 0).
- Produces:
  - the ledger key's fingerprint (`LEDGER_FPR`, ledgered);
  - `keys/ledger/sso-invites-ledger.asc` (public, in the sso worktree);
  - sso-secrets paths for the service: `invites/ledger-signing.asc`, `invites/ledger-deploy-key` (and `.pub`), and `invites/secrets.env` with `INVITES_STATUS_TOKEN`, `INVITES_SESSION_KEY`;
  - sso-secrets `host/secrets.yml` keys `ledger_status_token` and `invites_client_secret`.

- [ ] **Step 1: STOP—ask Chris** (Chris's steps, none of whose output reaches the transcript):
  1. **The ledger signing key:** OpenPGP ed25519, sign-only, no passphrase, expiring in 2 years, made in a throwaway `GNUPGHOME`: `gpg --batch --passphrase '' --quick-gen-key 'Realliance invites ledger <invites-ledger@realliance.net>' ed25519 sign 2y`. He exports the secret half to `invites/ledger-signing.asc` in sso-secrets and the public half to `~/Repos/realliance-sso-invites/keys/ledger/sso-invites-ledger.asc`.
  2. **The service's write deploy key:** `ssh-keygen -t ed25519 -N '' -C 'sso-invites service' -f <sso-secrets>/invites/ledger-deploy-key`, with its `.pub` registered on `sso-invites` as a deploy key with write access.
  3. **Two fine-grained tokens** on `<ledger-org>/sso-invites` only, permission *Commit statuses*, expiring in 1 year: write for the box (`ledger_status_token` in `host/secrets.yml`) and read for the service (`INVITES_STATUS_TOKEN`).
  4. **`invites_client_secret`** (`openssl rand -hex 32`) in `host/secrets.yml`, and `INVITES_SESSION_KEY` (`openssl rand -hex 32`) in `invites/secrets.env`.
  5. He commits sso-secrets signed and places `host/secrets.yml` on the box (`docs/runbook.md`, Placing host secrets).
- [ ] **Step 2: Check the public key.** `gpg --show-keys --with-colons keys/ledger/sso-invites-ledger.asc`. Expected: exactly one `pub` line, algorithm 22 (EdDSA), capability `s`, an expiry date about 2 years out, and no `sec` line. Ledger the fingerprint and both token expiry dates (the rotation dates).
- [ ] **Step 3: Write the ledger's first files.**
  - `ledger.yaml`: exactly `version: 1\nlinks: []\n`.
  - `schema.json`: a JSON Schema (draft 2020-12) of the spec's schema, `additionalProperties: false` at every level. It says the box's check is authoritative.
  - `README.md` (one screen, vale-clean): what the repo is, who writes (service, operators), the "Change" table, never edit on the web (web-flow commits freeze the ledger), and where erasure lives (`docs/sso-invites.md`).
- [ ] **Step 4: Commit signed and push.** `git -C ~/Repos/sso-invites-ledger commit -S -m 'ledger: version 1, empty'` then `git push origin HEAD:main`. Expected: `git log -1 --format=%G?%x20%GF` prints `G` and Chris's fingerprint `E9245FED84E0EC721C7B5C336BFB8037115ADE26`.

### Task 2: The box check: parse, signers and history

**Files:**
- Create: `host/roles/config_apply/files/sso_ledger.py`, `tests/ledger/conftest.py`, `tests/ledger/test_schema.py`, `tests/ledger/test_history.py`, `tests/ledger/vectors/index.yaml` plus one `.yaml` per vector.

**Interfaces:**
- Produces (Python):
  - `MAX_LEDGER_BYTES = 1_048_576`;
  - frozen `dataclass` types `Bootstrap(email: str, first_name: str, last_name: str | None)`, `Guest(username: str, bootstrap: Bootstrap, mfa: str, active: bool)`, `Link(id, inviter, created, expires, token_sha256, state: str, redeemed: str | None, revoked: str | None, guest: Guest | None)` and `Ledger(version: int, links: tuple[Link, ...])`;
  - `class SchemaError(Exception)` with `.code` ∈ {`too-large`, `yaml`, `duplicate-key`, `alias`, `unknown-key`, `missing-key`, `type`, `value`};
  - `parse_ledger(data: bytes) -> Ledger`;
  - `Trust(ledger: frozenset[str], operators: frozenset[str])` and `load_trust(path: Path) -> Trust`;
  - `commit_signer(repo: Path, sha: str, keyring: Path, trust: Trust) -> Literal["ledger", "operator", "untrusted"]`;
  - `class TransitionError(Exception)` with `.code` ∈ {`path`, `immutable`, `removal`, `reactivate`, `not-allowed`, `version`} and `.link_id`;
  - `check_transition(before: Ledger | None, after: Ledger, signer: str, paths: set[str]) -> None`;
  - `HeadVerdict(head: str, effective: str | None, refused: bool, reason: str | None)`, where `reason` looks like `untrusted-signer@<sha7>`, `not-fast-forward`, `merge-commit@<sha7>`, `schema:<code>@<sha7>`, `transition:<code>@<sha7>` or `no-operator-root`;
  - `evaluate_head(repo: Path, head: str, last_good: str | None, keyring: Path, trust: Trust) -> HeadVerdict`.
- Test fixtures: session-scoped `keys` (operator, ledger, an untrusted "web-flow" key and an expired ledger key, each in its own `GNUPGHOME` under `tempfile.mkdtemp(dir="/tmp")`, because gpg-agent's socket path must stay under 108 bytes); a verifying keyring holding only the operator and ledger public keys with ultimate owner trust; `ledger_repo(tmp_path)` with `commit(tree: dict[str, bytes], signer: str | None, parents=…) -> sha`.

- [ ] **Step 1: Write the failing schema tests** in `tests/ledger/test_schema.py`:
  - `test_minimal_ledger_parses`: `parse_ledger(b"version: 1\nlinks: []\n") == Ledger(1, ())`.
  - `test_unknown_key_refused`: a `groups` key at the top, in a link and in a guest each raise `SchemaError` with `code == "unknown-key"`.
  - `test_duplicate_key_refused`: `code == "duplicate-key"`, at the top and inside a guest.
  - `test_alias_refused`: an anchor and alias anywhere gives `code == "alias"`.
  - `test_unquoted_timestamp_refused`: `created: 2026-11-08T00:00:00Z` unquoted gives `code == "type"`; `active: yes` gives `code == "type"`.
  - `test_oversize_refused`: 1_048_577 bytes gives `code == "too-large"`.
  - `test_mfa_required_and_enum`: missing gives `missing-key`; `"none"`, `""` and `"sms"` give `value`.
  - `test_expires_at_most_seven_days`: `created` plus 7 d passes; plus 7 d 1 s gives `value`.
  - `test_formats`: link id `l-` plus 16 hex; `token_sha256` 64 lowercase hex; `state` among its three values; `redeemed` present if and only if `state == "redeemed"`, and the same for `revoked`; `guest` present if and only if redeemed. Anything else gives `value`.
  - `test_vectors`: parametrised over `vectors/index.yaml` (`{file, expect: ok | <code>}`). Accept vectors: an empty ledger, one with a pending, a revoked and a redeemed link, and one with no `last_name`. Refuse vectors cover each schema code listed earlier.
- [ ] **Step 2: Run them; expect FAIL** (module missing): `taskset -c 0-3 nix develop ~/Repos/realliance-sso-invites --command pytest tests/ledger/test_schema.py -q`.
- [ ] **Step 3: Implement `parse_ledger`** with a `yaml.SafeLoader` subclass. It refuses duplicate keys in `construct_mapping`, refuses `AliasEvent` and anchors, and drops the implicit timestamp resolver so timestamps stay strings, which the schema then checks against `^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$`. The size check runs before parsing.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Write the failing history tests** in `tests/ledger/test_history.py`:
  - `test_signer_classification`: operator gives `"operator"`, ledger gives `"ledger"`, the untrusted key, an unsigned commit and the expired ledger key each give `"untrusted"`.
  - `test_ledger_key_transitions`, a table run through `check_transition(…, "ledger", {"ledger.yaml"})`:
    - allowed: add a pending link; `pending` to `revoked`; `pending` to `redeemed` with a guest whose `active` is `true`; `active` `true` to `false`;
    - refused: `false` to `true` (`reactivate`), any immutable field (`immutable`), dropping a link (`removal`), changing `version` (`version`), redeeming with `active: false` (`not-allowed`), `revoked` to anything (`not-allowed`), and `paths == {"ledger.yaml", "README.md"}` (`path`).
  - `test_operator_may_change_anything_the_schema_allows`: every refused row in the preceding table passes with `"operator"`.
  - `test_fast_forward_ledger_commits_accepted`: an operator root, then three ledger commits, gives `effective == head` and `refused is False`.
  - `test_untrusted_commit_refuses_head`: an unsigned commit after `last_good`, then a ledger commit, gives `refused`, `reason.startswith("untrusted-signer@")` and `effective == last_good`.
  - `test_non_fast_forward_ledger_head_refused`: `reason == "not-fast-forward"`.
  - `test_merge_commit_refused_unless_operator_head`: a merge gives a `merge-commit@` reason; an operator commit on top of it passes.
  - `test_operator_head_accepted_over_bad_commits`: an unsigned commit then an operator commit gives `effective == head`.
  - `test_operator_head_after_rewrite_accepted`: `last_good` missing from the repository (a re-clone) with an operator head gives `effective == head`; a ledger head there is refused `not-fast-forward`.
  - `test_no_last_good_walks_from_root`: a ledger-signed root gives `no-operator-root` and `effective is None`. An operator root, two ledger commits, then a bad commit gives `effective` at the second ledger commit and `refused`.
  - `test_schema_error_in_any_commit_refuses_head`: a malformed intermediate commit gives a `schema:` reason.
- [ ] **Step 6: Run; expect FAIL.**
- [ ] **Step 7: Implement `commit_signer`, `check_transition` and `evaluate_head`.**
  - **The signer:** `git -C repo log -1 --format=%G?%x00%GP <sha>` with `GNUPGHOME=keyring`. The result is `ledger` or `operator` only for `%G? == "G"` with `%GP` in the matching set.
  - **The commits and their paths:** `git rev-list --reverse --first-parent` and `git rev-list --merges` over `last_good..head`, then `git diff-tree --no-commit-id --name-only -r <sha>` for the paths of each commit.
  - **The rule:** D3, exactly, with `ledger.yaml` read by `git show <sha>:ledger.yaml`. Every git call gets `timeout=30` and `env` with `LC_ALL=C`.
- [ ] **Step 8: Run; expect PASS.** Also run `tests/test_no_secret_in_asserts.py`.
- [ ] **Step 9: Commit:** `git add host/roles/config_apply/files/sso_ledger.py tests/ledger && nix develop -c git commit -S -m "sso-ledger: strict ledger parse, signer classes, head history (check-only core)"`.

### Task 3: The box check: admission, quotas, clock and state

**Files:**
- Modify: `host/roles/config_apply/files/sso_ledger.py`.
- Create: `tests/ledger/test_admission.py`, `tests/ledger/test_cli.py`.

**Interfaces:**
- Consumes Task 2's `parse_ledger`, `evaluate_head` and `Trust`.
- Produces:
  - `People` with `tiers: dict[str, str]` (username to `realliance` | `member` | `guest` | `inactive`), `usernames: set[str]` and `emails: set[str]` (in lower case), from `load_people(people_dir: Path) -> People` (reads `people.yaml`);
  - `load_reserved(people_dir: Path) -> tuple[frozenset[str], tuple[str, ...]]` (names and prefixes; built-ins plus `reserved.yaml` if present);
  - `class Directory(Protocol)` with `username_taken(u: str) -> bool` and `email_taken(e: str) -> bool`, which raise `LookupFailed`; `KeycloakDirectory(base_url: str, secret_file: Path)` uses client `tofu-plan` with 10 s timeouts;
  - `BoxState(version: int, last_good: str | None, seen: dict[str, dict], admitted: dict[str, dict], refused: dict[str, dict])`, `load_state(dir: Path) -> BoxState` and `save_state(dir: Path, s: BoxState) -> None` (atomic, 0600);
  - `Decision(admitted_now: list[str], would_admit: list[str], refused_now: dict[str, str], deferred: dict[str, str], guests: dict[str, dict])`;
  - `admit(ledger: Ledger, people: People, reserved, directory: Directory, state: BoxState, now: datetime, mode: str) -> Decision`;
  - `main(argv: list[str]) -> int` with commands `check`, `settle`, `seed`, `forget` and `fallback` (flags in Step 7).
- Constants: `REALLIANCE_QUOTA = 5`, `REALLIANCE_WINDOW = timedelta(days=7)`, `MEMBER_QUOTA = 1`, `GLOBAL_CAP = 10`, `CAP_WINDOW = timedelta(hours=24)`, `MAX_LIFETIME = timedelta(days=7)`, `EXPIRY_GRACE = timedelta(minutes=30)`. Built-in reserved names: `admin root www sso git api mail invites invite guests forgejo forgejo-admin forge-bot forge-ci ops-ci ci-dispatch realliance realliance-ops realliance-ci realmic ghost`. Built-in reserved prefixes: `test- ci- admin-`.
- **Tier rules:** active with `/realliance` in groups is `realliance`. Active with `/guests` in groups is `guest`: an emergency-override entry. Any other active entry is `member`, an inactive one is `inactive`, and an absent username isn't a member.
- **Slot rule (D18):** for inviting member `I` and link `L`, count `I`'s other links that the box saw before `L` and that hold a slot now. A link holds a slot while pending and unexpired by the box clock. An admitted redemption holds one too: within 7 days of `at` for `realliance`, for good for `member`. Admit `L` if the count is below the tier's quota.
- **On time:** `seen[L].redeemed <= seen[L].link + min(expires − created, MAX_LIFETIME) + EXPIRY_GRACE`.

- [ ] **Step 1: Write the failing tests** in `tests/ledger/test_admission.py`, with a fake `Directory` and a fixed `now`:
  - `test_admits_good_redemption`: `admitted_now == [username]`, and `guests[username] == {"active": True, "mfa": "webauthn", "bootstrap": {...}}`.
  - `test_check_mode_hands_no_new_guests`: `mode="check"` gives `would_admit == [username]`, `guests == {}`, and an unchanged `state.admitted`.
  - `test_username_rules`: `Ab`, `a`, `a--b`, `-ab`, `ab-` and a 33-character name give `username-format`. `www`, `forge-bot`, `test-x` and a name from `reserved.yaml` give `username-reserved`. A `people.yaml` username, a Keycloak-taken name and another ledger guest's name give `username-taken`.
  - `test_email_collision_case_insensitive`: `Bob@Example.COM ` against Keycloak's `bob@example.com`, and against a `people.yaml` bootstrap email, give `email-taken`. A malformed address and a 255-character one give `email-format`.
  - `test_same_username_twice_first_wins`: two redeemed links with one username, seen in one run: the earlier ledger index is admitted, the later one is refused `username-taken`.
  - `test_entry_refused_for_keycloak_prohibited_name`: first names `Ann<`, `$x`, `a;b`, `{x}`, a tab character and 65 characters each give `name-format`; `last_name` `None` passes.
  - `test_inviter_rules`: absent gives `inviter-not-member`, inactive gives `inviter-inactive`, an override entry with `/guests` gives `inviter-not-member`, and a guest from the ledger gives `inviter-not-member`.
  - `test_realliance_quota_five_per_seven_days`: five admitted within 7 d, then a sixth, gives `quota`; with one of the five admitted 7 d and 1 s ago, the sixth is admitted.
  - `test_member_quota_one_for_life`:
    - refused `quota`: the first link admitted 400 d ago; the first link pending and unexpired; the first guest later deactivated;
    - admitted: the first link revoked; the first link expired.
  - `test_pending_links_hold_slots_earlier_first`: a `member` with two pending links that both redeem: the first seen is admitted, the second is refused `quota`.
  - `test_global_cap_defers_eleventh`: ten admitted in the last 24 h gives `deferred == {id: "cap"}` with the entry not refused; at `now + 24 h` the box admits it.
  - `test_expiry_uses_box_first_seen_plus_grace`: link seen at `T`, lifetime 7 d. The box admits a redemption seen at `T + 7 d + 29 min`, and refuses one seen at `T + 7 d + 31 min` as `expired`. A `created` 30 days before `T` doesn't matter.
  - `test_inactive_new_guest_never_created`: a redemption with `active: false` is in neither `guests` nor `admitted`, with no refusal.
  - `test_admitted_guest_never_dropped_or_rechecked`: an admitted guest whose name Keycloak now reports taken is still handed. An admitted guest whose entry was erased is handed as `{"active": <stored>, "mfa": "webauthn", "bootstrap": None}`.
  - `test_deactivation_reaches_admitted_guest`: `active` `true` to `false` in the ledger hands `active: False` and updates `admitted[u].active`.
  - `test_refusal_persists_until_entry_changes`: in `apply` the refusal sticks across runs with Keycloak now free, and clears when an operator changes the entry.
  - `test_lookup_failure_defers`: `LookupFailed` gives `deferred == {id: "lookup-failed"}`.
  - `test_check_mode_persists_seen_not_admitted`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement** `load_people`, `load_reserved`, `KeycloakDirectory`, `load_state`/`save_state` and `admit`. Candidates run in `(seen.redeemed, ledger index)` order. Name rules follow D16, rejecting `<>&"$%!#?§;*~/\|^=[]{}()` and every character with Unicode category `Cc`.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Write the failing command-line tests** in `tests/ledger/test_cli.py`:
  - `test_check_writes_outputs`: `main(["check", …])` returns 0 and writes `guests.next.json`, `result.json` and `state.json`. `result.json` has the keys `mode`, `head`, `effective`, `refused`, `reason`, `admitted`, `would_admit`, `refused_entries`, `deferred` and `description`.
  - `test_outputs_hold_ids_only`: no fixture email, first name or token, even in part, in `stdout`, `stderr` or `result.json`.
  - `test_description_fits_a_status`: `len(description) <= 140` with 30 refused entries.
  - `test_settle_failed_new_refuses_them`: `settle --failed-new` moves this run's new admissions to `refused` as `apply-failed`.
  - `test_settle_applied_promotes_next_to_handed`.
  - `test_seed_from_output`: `seed --from` a `tofu output -json guests` file fills `admitted` by username.
  - `test_forget_removes_one`.
  - `test_fallback_copies_handed_or_empty`: `fallback` writes `handed.json`'s content, or `{"guests": {}}` without that file.
  - `test_parses_as_python_312`: `ast.parse(src, feature_version=(3, 12))`.
- [ ] **Step 6: Run; expect FAIL.**
- [ ] **Step 7: Implement `main`.**
  - `check --mode {check,apply} --repo DIR --head SHA --people DIR --state DIR --keyring DIR --trust FILE --keycloak-url URL --keycloak-secret-file FILE`
  - `settle --state DIR (--applied | --failed-new)`, `seed --state DIR --from FILE`, `forget --state DIR USERNAME`, `fallback --state DIR`
  - Exit 0 on every verdict, a refused head included, and 2 on a usage or internal error. Print one summary line, ids only.
  - Description templates: `check: N would admit, N refused, N deferred`; `apply: N admitted, N refused, N deferred`; `refused: <reason>` (failure).
- [ ] **Step 8: Run `tests/ledger`; expect PASS.**
- [ ] **Step 9: Commit:** `sso-ledger: admission, quotas, caps, box clock and state`.

### Task 4: The box runs the check, in `check` mode

**Files:**
- Modify: `host/roles/config_apply/files/sso-config-apply.sh`, `host/roles/config_apply/tasks/main.yml`, `host/group_vars/sso.yml`, `operators.yml`, `realm/groups.tf`, `tests/host/test_config_apply.py`, `tests/realm/test_realm.py`.
- Create: `host/roles/config_apply/templates/ledger.env.j2`, `host/roles/config_apply/templates/ledger-trust.json.j2`, `keys/ledger/sso-invites-ledger.asc` (from Task 1), `tests/ledger/test_apply_script.py`.

**Interfaces:**
- Consumes Task 3's command line. Task 5's `var.guests` lands after this task; until then tofu only warns about the undeclared variable in the var-file.
- Produces:
  - `group_vars`: `ledger_mode: check`, `ledger_repo: git@github.com:<ledger-org>/sso-invites.git`, `ledger_status_repo: <ledger-org>/sso-invites` and `apply_interval: 1min`;
  - `operators.yml`: `ledger_signing_fingerprints: [<LEDGER_FPR>]`;
  - box paths:
    - `/etc/realliance-sso/gnupg-ledger` (keyring), `/etc/realliance-sso/ledger-trust.json`, `/etc/realliance-sso/ledger.env` (0600: `LEDGER_MODE`, `LEDGER_REPO`, `LEDGER_STATUS_REPO`, `LEDGER_STATUS_TOKEN`) and `/etc/realliance-sso/ledger-lookup.secret` (0600);
    - `/etc/realliance-sso/ledger_deploy_key` (ed25519, generated on the box);
    - `/var/lib/sso-ledger` (checkout) and `/var/lib/sso-config-apply/ledger/` (state);
  - script overrides, all defaulting to today's paths (for the harness and Task 19): `SSO_DIR`, `PEOPLE_DIR`, `STATE_DIR`, `LEDGER_DIR`, `SSO_ETC`, `SSO_LOCK`, `SSO_LEDGER_BIN`, `SSO_LEDGER_TIMEOUT` (default 120) and `PEOPLE_REF`.

- [ ] **Step 1: Write the failing harness tests** in `tests/ledger/test_apply_script.py`.
  - **The fixture root:** a temp tree with signed `SSO_DIR` and `PEOPLE_DIR` repos, a ledger bare repo, and a keyring for each key from Task 2.
  - **The stubs on `PATH`:** `tofu` records each `-chdir` and copies each `-var-file` to a log, and fails when `STUB_TOFU_FAIL_IF=<username>` appears in the var-file. `curl` records each status POST.
  - Tests:
    - `test_people_apply_runs_when_ledger_crashes[check|apply]`: `SSO_LEDGER_BIN=/bin/false` gives exit 0 and one people apply with the fallback set.
    - `test_people_apply_runs_when_ledger_times_out[check|apply]`: a `SSO_LEDGER_BIN` that sleeps 10 s with `SSO_LEDGER_TIMEOUT=2` gives exit 0 and a people apply.
    - `test_people_apply_runs_when_fetch_fails[check|apply]`: an unreachable `LEDGER_REPO` gives exit 0, a people apply, and a log line `ledger: fetch failed`.
    - `test_check_mode_hands_no_new_guests`: the var-file has no guests, and `result.json` lists the id under `would_admit`.
    - `test_apply_mode_hands_admitted_guest`.
    - `test_new_guest_apply_failure_retries_without_it`: `STUB_TOFU_FAIL_IF=bad-guest` gives two people applies (with the guest, then without), exit 0, and the state refuses it as `apply-failed`.
    - `test_refused_head_people_still_applies`.
    - `test_changed_mode_skips_realm_when_sso_unchanged`: a second run after only a ledger change calls `tofu -chdir=<people>` and not `<sso>/realm`.
    - `test_ledger_status_posted_once_per_verdict`: two runs over one head make one POST, to `/repos/<ledger_status_repo>/statuses/<sha>` with context `sso/apply`.
    - `test_unsigned_people_head_still_refused`: today's behaviour holds.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement the script** without changing what it does today when no ledger exists. Order:
  1. take the lock;
  2. verify `realliance/sso`, then fetch and verify `sso-people`, as now;
  3. the **ledger step**, a function that never exits the script:
     - clone if `LEDGER_DIR` is absent, then run `timeout 60 git fetch -q origin main` with the ledger deploy key;
     - if `handed.json` is absent, seed it: `tofu -chdir=$PEOPLE_DIR init`, then `output -json guests` into `sso-ledger seed`, and `{"guests": {}}` when there are no outputs;
     - run `timeout "$SSO_LEDGER_TIMEOUT" "$SSO_LEDGER_BIN" check …`;
     - on any failure: log `ledger: <step> failed; people apply continues with the last handed guests` and run `sso-ledger fallback` (or write `{"guests": {}}` if that fails too);
  4. **changed mode** exits early only if the `sso` commit, the `sso-people` commit and `sha256(guests.next.json)` all equal the stored ones;
  5. the **realm apply** runs only when the `sso` commit changed, or in `force`;
  6. the **people apply** runs with `-var-file=guests.next.json`. If it fails and `guests.next.json` differs from `handed.json`, it runs again with `handed.json`. If that passes, run `sso-ledger settle --failed-new`; on any success, `settle --applied`;
  7. post the ledger status only when `(head, state, description)` changed since the last post.
- [ ] **Step 4: Implement the role.**
  - **The keyring:** import `keys/*.asc` and `keys/ledger/*.asc` into `gnupg-ledger`, delete every key not in `operators[].gpg_fingerprint` plus `ledger_signing_fingerprints` (web-flow is never trusted here), and set ultimate owner trust. This mirrors the pull role's rebuild, and runs only when the keys or the list changed.
  - **The deploy key:** `ssh-keygen` with `creates:`, as for the people key.
  - **The templates:** `ledger.env.j2`, `ledger-trust.json.j2` and the lookup secret from `tofu_plan_client_secret`, all `no_log`.
  - **The program:** install `sso_ledger.py` as `/usr/local/sbin/sso-ledger`, mode 0755.
  - **A guard:** `ansible.builtin.assert` that `ledger_mode in ['check', 'apply']`.
  - **The realm:** `realm/groups.tf` gains `resource "keycloak_group" "guests"` (name `guests`, top level, no roles).
- [ ] **Step 5: Run the harness and every offline test; expect PASS.** Run `ansible-lint host/` too; expected clean.
- [ ] **Step 6: Add the live tests.**
  - In `tests/host/test_config_apply.py`:
    - `test_ledger_keyring_holds_only_ledger_and_operator_keys`: fingerprints equal operators plus ledger, and web-flow is absent;
    - `test_ledger_mode_is` (equals `group_vars['ledger_mode']`);
    - `test_apply_timer_every_minute`;
    - `test_sso_ledger_installed` (`/usr/local/sbin/sso-ledger fallback --help` exits 0);
    - `test_ledger_deploy_key_registered_read_only`, skipped until Step 9;
    - `test_ledger_head_status_reported`: the ledger head carries `sso/apply`.
    - `test_box_plans_are_clean` changes: the people plan passes `-var-file=/var/lib/sso-config-apply/ledger/handed.json` when that file exists. Without it, the plan would try to destroy every guest once guests exist, and `prevent_destroy` would fail it.
  - In `tests/realm/test_realm.py`: `test_guests_group_exists_without_roles`.
- [ ] **Step 7: Commit signed, push the branch and open the PR.** Then **STOP—ask Chris (box head).** The change: the check in `check` mode, a 1-minute apply timer, realm applies only on `realliance/sso` changes, and group `/guests`. What to watch: `journalctl -u sso-config-apply-changed -n 50` shows `ledger: fetch failed` (expected until Step 9), and `sso/apply` success on the next `sso-people` head. Rollback trigger: any people apply failure, or `test_box_plans_are_clean` failing. Rollback: revert the PR (Chris merges the revert).
- [ ] **Step 8: Chris merges.** `Monitor` `sso-pull` until the head applies (about 5 min), then one apply run. Then run `pytest tests/host tests/realm/test_realm.py --hosts=ssh://root@23.163.0.48`; expected all pass except the skipped deploy-key test.
- [ ] **Step 9: STOP—ask Chris (Chris's step).** Show `ssh root@23.163.0.48 cat /etc/realliance-sso/ledger_deploy_key.pub` (a public key). Chris registers it on `sso-invites` as a **read-only** deploy key. Then the next run fetches, and the ledger head shows `sso/apply` success `check: 0 would admit, 0 refused, 0 deferred`. Un-skip and run the deploy-key test. Ledger the time: it starts the check-only clock.

### Task 5: The guest layer in `sso-people`

**Files:**
- Create (sso-people): `guests.tf`, `reserved.yaml`.
- Modify (sso-people): `main.tf`, `README.md`.
- Test: sso `tests/people/test_people.py`, on branch `invites-people-tests` of the sso worktree.

**Interfaces:**
- Produces:
  - `variable "guests"`: `map(object({ active = bool, mfa = optional(string, "webauthn"), bootstrap = optional(object({ email = string, first_name = string, last_name = optional(string) })) }))`, default `{}`;
  - `output "guests"` (sensitive, `= var.guests`);
  - `local.guest_groups = ["/guests"]`;
  - one `keycloak_user.person` map over `local.everyone = merge(local.guest_people, local.people)`, where `people.yaml` wins.
- `reserved.yaml`: `{reserved: [...], reserved_prefixes: [...]}`. It starts with no hidden name; Chris adds those himself.

- [ ] **Step 1: Write the failing sandbox tests** in `tests/people/test_people.py`. They reuse `sandbox`, `_tofu` and `_user`, with throwaway names and `delivered+…@resend.dev` addresses, and a var-file written with `umask 077`.
  - `test_guest_gets_exactly_guest_groups`: apply with `{"guests": {g: {...mfa: "totp"}}}`. Expected: the user has `enabled: true`, `_groups_of == {"guests"}` and the `mfa` attribute is `["totp"]`.
  - `test_guest_groups_ignore_input`: a guest object that also carries `groups: ["/realliance"]` yields groups `{"guests"}` or a plan error, never `realliance`.
  - `test_guest_deactivated`: `active: false` gives `enabled is False`.
  - `test_people_entry_wins_over_guest_and_keeps_id`: add a `people.yaml` entry for `g` with no `bootstrap` and groups `[/realliance]`. Expected: the same user id, and groups `{"realliance"}`.
  - `test_entry_without_bootstrap_needs_a_guest`: a `people.yaml` entry with no `bootstrap` that is not in `var.guests` fails the plan with `bootstrap is required for a new person`.
  - `test_guests_output_round_trips`: `tofu output -json guests` equals the var-file's `guests`.
- [ ] **Step 2: Run; expect FAIL:** `SSO_PEOPLE_DIR=~/Repos/realliance-sso-people-invites taskset -c 0-3 nix develop ~/Repos/realliance-sso-invites --command pytest tests/people -q -k guest`.
- [ ] **Step 3: Implement.**
  - **One object shape:** `main.tf` normalises every `people.yaml` entry to `{active, groups, mfa, bootstrap}`, with `bootstrap = try({email, first_name, last_name = try(…, null)}, null)`, so `merge()` meets one type.
  - **Optional profile fields:** `email`, `first_name` and `last_name` read through `try(…, null)`.
  - **The precondition:** `lifecycle.precondition` on `keycloak_user.person` reads `each.value.bootstrap != null || contains(keys(var.guests), each.key)`, error message `bootstrap is required for a new person`.
  - **`guests.tf`** builds `guest_people` with `groups = local.guest_groups` and never reads a groups field.
  - **The README** says the box passes the guest set, and gives the local plan recipe: `tofu output -json guests | jq '{guests: .}'` into a `umask 077` file, then `-var-file`.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Plan against live state, read-only.** In the sso-people worktree, with `realm/secrets.env` loaded by the same env helper the tests use and `TF_VAR_keycloak_client_id=tofu-plan`, run `tofu plan -var-file=<(echo '{"guests": {}}') -detailed-exitcode`. Expected: exit 0, `No changes.` The refactor touches no member.
- [ ] **Step 6: Commit both repos signed** (sso-people: `people: guests layer (guests.tf), one user map, reserved names`; sso: `tests: guest layer sandbox cases`), push, and open both PRs. **Chris:** merges sso-people. The box applies it within 2 min; then `pytest tests/host/test_config_apply.py::test_box_plans_are_clean --hosts=ssh://root@23.163.0.48` passes. **Chris:** merges the tests PR.

### Task 6: The check-only run (at least 3 days)

**Files:** create `tests/invites_helpers.py` and `tests/invites/test_ledger_drills.py` (runs only with `INVITES_DRILL=check-window`).

**Interfaces:**
- Produces, in `invites_helpers.py`:
  - `Ledger(repo_dir)`, with `.add_link(inviter) -> link_id`, `.redeem(link_id, username, email, first, last=None, mfa="webauthn")`, `.revoke(id)`, `.deactivate(id)` and `.raw(mutator)`;
  - `.commit_and_push(signer: "operator" | "ledger" | "unsigned", message) -> sha`. The `ledger` signer imports `SECRETS_DIR/invites/ledger-signing.asc` into a temp `GNUPGHOME` inside the test process; `unsigned` uses `git commit-tree` like `test_pull.py`'s fixture;
  - `web_flow_commit(mutator) -> sha` through `gh api -X PUT repos/<ledger-org>/sso-invites/contents/ledger.yaml`;
  - `wait_status(sha, timeout=600) -> (state, description)`;
  - `box_result()`, which reads `/var/lib/sso-config-apply/ledger/result.json` over SSH.
- Drill usernames start with `chk-` and use `delivered+chk-<hex>@resend.dev`. The inviting member is Chris, with his own `people.yaml` username (from Task 0's answers), never another member's. A taken username or email comes from a `user_factory` user, never a real person.

- [ ] **Step 1: Write the drills**, each asserting the box's verdict through `wait_status` and `box_result`:
  - `test_good_entry_would_admit`: a description starting `check: 1 would admit`.
  - Entry refusals, each a fresh link: `test_username_taken`, `test_username_reserved`, `test_email_taken_case_variant`, `test_name_prohibited_character` and `test_inviter_absent`. The quota and cap cases stay offline (Task 3) and in Task 19's fixture runs, so no real member's slot is ever touched.
  - Head refusals, each followed by an operator-signed revert that the next `wait_status` shows as success:
    - `test_ledger_key_adds_groups_refused` (`schema:unknown-key`);
    - `test_redemption_without_mfa_refused` (`schema:missing-key`);
    - `test_web_flow_commit_refused` (`untrusted-signer`);
    - `test_unsigned_commit_refused` (`untrusted-signer`);
    - `test_freeze_holds_later_commits`: a ledger-key deactivation pushed behind a refused head leaves `box_result()["effective"]` at the last good commit.
- [ ] **Step 2: Run them once:** `INVITES_DRILL=check-window taskset -c 0-3 nix develop ~/Repos/realliance-sso-invites --command pytest tests/invites/test_ledger_drills.py -q`. Expected: all pass. Ledger each verdict (link ids, reason codes).
- [ ] **Step 3: STOP—ask Chris (Chris's step, optional).** The force-push drill: Chris force-pushes a prepared ledger-signed commit that doesn't descend from the head. Expected: `refused: not-fast-forward`. The agent then pushes an operator-signed fix on top, fast-forward, and expects success.
- [ ] **Step 4: Watch the window.** Once a day (a `Monitor` armed for 24 h each time), ledger the day's result lines, `sso/apply` on any `sso-people` head merged that day, and `test_box_plans_are_clean`. Expected: every verdict as predicted, no unexpected refusal, and people applies unaffected.
- [ ] **Step 5: Exit gate.** At least 3 days since Task 4 Step 9 (a week if no other task is waiting), every drill's verdict seen, and zero unexpected verdicts. Ledger the summary. Commit the drills to branch `invites-drills`; **Chris:** merges (tests only).

### Task 7: The `invites` client and its service account

**Files:**
- Modify: `realm/modules/app/variables.tf`, `realm/modules/app/main.tf`, `realm/apps.tf`, `realm/outputs.tf`, `realm/automation.tf`, `host/roles/master_realm/tasks/main.yml`, `tests/conftest.py`, `tests/realm/test_apps.py`, `tests/host/test_master_realm.py`; sso-people `main.tf`; sso `tests/people/test_people.py`.

**Interfaces:**
- Produces:
  - the app module's `variable "role_groups"` (`map(string)`, role name to existing group id, default `{}`), each a `keycloak_group_roles` with `exhaustive = false`;
  - `module "invites"`: client `invites`, name `Invites`, redirect `https://invite.realliance.net/auth/callback`, roles `["user", "member"]`, `role_groups = { member = keycloak_group.community["realliance"].id }`, `home_url = "https://invite.realliance.net"`, `always_display_in_console = true`, no extra scopes;
  - `keycloak_openid_user_client_role_protocol_mapper.invites_roles`: claim `roles`, a list, for client `invites`, ID token only;
  - `app_client_secrets.invites`;
  - the master client `invites` with `invites_client_secret`, and roles `view-users` and `query-users` on `realliance-realm`;
  - `conftest.browser_login(…, redirect_uri=None)` and `CLIENT_SECRETS["invites"]`;
  - in the people layer, `/apps/invites` appended to every `people.yaml` entry whose groups lack `/guests`.

- [ ] **Step 1: Write the failing tests.**
  - `test_invites_client`: confidential, PKCE S256, exactly one redirect, `groups` not among its default scopes, and roles `{user, member}`.
  - `test_invites_member_role_granted_to_realliance`: `/realliance`'s client-role mappings for `invites` are `["member"]`.
  - `test_invites_id_token_roles_claim`, through `GET /admin/realms/realliance/clients/{id}/evaluate-scopes/generate-example-id-token?userId=…`: a `user_factory` user in `/apps/invites` and `/realliance` gets `sorted(roles) == ["member", "user"]`; one in `/apps/invites` only gets `["user"]`.
  - `test_guest_refused_by_invites`: a user in `/guests` only, through `browser_login(client_id="invites", redirect_uri=…)`, gets no code back.
  - `test_invites_holds_exactly_its_roles` (master realm, modelled on forge-bot's): `{view-users, query-users}`.
  - `test_people_get_apps_invites` and `test_override_entry_gets_no_invites` (sandbox): a `people.yaml` person in `/realliance` has `invites` in their groups; an entry with groups `[/guests]`, and every guest, doesn't.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.** In the people layer, append to the normalised `groups`, and keep `public_names` derived from `local.everyone`.
- [ ] **Step 4: Run the offline checks** (`tofu -chdir=realm validate`, `ansible-lint`) and the sandbox tests. Commit signed on branch `invites-client`, push, and open the sso PR. Then **STOP—ask Chris (box head)**: a new client, a role on `/realliance` and a master service account with read rights. Watch: the realm apply succeeds, and `test_no_user_holds_a_management_role` stays green. Rollback: revert.
- [ ] **Step 5: Chris merges.** `Monitor` the pull and the apply, then run `pytest tests/realm/test_apps.py tests/host/test_master_realm.py --hosts=…`; expected PASS.
- [ ] **Step 6: STOP—ask Chris (Chris's step).** He pipes `tofu -chdir=realm output -json app_client_secrets | jq -r .invites` into `INVITES_OIDC_CLIENT_SECRET` in `invites/secrets.env` and commits sso-secrets signed. The master `invites` client's secret is already `invites_client_secret`; he copies it to `INVITES_KEYCLOAK_CLIENT_SECRET` the same way.
- [ ] **Step 7: Land the people change.** Open the sso-people PR (`people: /apps/invites for every person, never a guest`). **Chris:** merges. Verify `test_box_plans_are_clean`; the box's apply log shows only group-membership changes.

### Task 8: The service repo and its ledger package

**Files:**
- Modify (forge inventory, branch `sso-invites-repo`): `repos.tf`, adding `forgejo_repository.sso_invites` and `forgejo_branch_protection.sso_invites_main` in `ci_dispatch`'s shape.
- Create (service): `go.mod` (`module git.realliance.net/realliance-ops/sso-invites`, `go 1.26.0`, `toolchain go1.26.10`), `.ko.yaml` (forge-bot's base image digest), `.forgejo/workflows/ci.yaml` (forge-bot's, with `KO_DOCKER_REPO: git.realliance.net/realliance-ops/sso-invites` and `./cmd/sso-invites`), `LICENSE-MIT`, `LICENSE-APACHE`, `README.md`, `deps_test.go`, `internal/secret/secret.go` (forge-bot's at `9be4857`, header naming the source), `internal/ledger/{ledger.go,parse.go,mutate.go,ledger_test.go}`, and `internal/ledger/testdata/vectors/` (copied from sso `tests/ledger/vectors/` at Task 2's commit, with a `SOURCE` file naming it).

**Interfaces:**
- Produces (Go):
  - `type Ledger struct { Version int; Links []Link }`;
  - `type Link struct { ID, Inviter, Created, Expires, TokenSHA256, State, Redeemed, Revoked string; Guest *Guest }`;
  - `type Guest struct { Username string; Bootstrap Bootstrap; MFA string; Active bool }`;
  - `type Bootstrap struct { Email, FirstName, LastName string }`, where Marshal leaves out an empty `LastName`;
  - YAML keys exactly as in the schema;
  - `func Parse(b []byte) (*Ledger, error)`, returning `*SchemaError{Code string}` with the box's codes;
  - `func (l *Ledger) Marshal() ([]byte, error)`: canonical, fixed key order, quoted timestamps;
  - `func (l *Ledger) AddLink(id, inviter string, created time.Time, ttl time.Duration, tokenSHA256 string) error`;
  - `Revoke(id string, at time.Time) error`, `Redeem(id string, g Guest, at time.Time) error`, `Deactivate(id string) error`;
  - `ByTokenHash(h string) (*Link, bool)`, `ByID(id string) (*Link, bool)`;
  - `var ErrNotPending, ErrNotFound, ErrNotActive`.

- [ ] **Step 1: The repo.** Add the two resources to `repos.tf`, plan (expected `2 to add, 0 to change, 0 to destroy`), commit signed and open the inventory PR. **Chris:** merges fast-forward. **STOP—ask Chris**, then apply (expected `2 added`). Clone to `~/Repos/sso-invites` and branch `v1` from `main`.
- [ ] **Step 2: Write the failing tests** in `internal/ledger/ledger_test.go`:
  - `TestParseMinimal`;
  - `TestParseRefusesUnknownKey`, `TestParseRefusesDuplicateKey`, `TestParseRefusesAlias`, `TestParseRefusesOversize` (1 MiB plus 1);
  - `TestMarshalCanonical`: a golden file `testdata/golden.yaml`, byte-equal;
  - `TestMarshalRoundTripsThroughBoxVectors`: every `ok` vector parses and re-marshals to bytes that parse equal; every refuse vector fails with the same `Code`;
  - `TestAddLinkExpiresSevenDays`: `Expires − Created == 168h`, and a TTL over 168h is refused;
  - `TestRedeemRequiresPending`, `TestDeactivateOnlyTrueToFalse`;
  - `TestMutationsNeverTouchOtherLinks`: marshal before and after differ only inside the target link.
  - In `deps_test.go`, `TestDirectRequires`: every direct require in `go.mod` is one of `go-git/v5`, `go-crypto`, `go.yaml.in/yaml/v3`, `go-oidc/v3`, `golang.org/x/oauth2` and `client_golang`. Later tasks add the rest of that set, and nothing outside it.
- [ ] **Step 3: Run; expect FAIL:** `cd ~/Repos/sso-invites && $GO test -race -p=4 ./...`.
- [ ] **Step 4: Implement.** The strict parse decodes into `yaml.Node` first, rejecting alias and anchor nodes and duplicate mapping keys, then decodes with `KnownFields(true)` and runs the same schema checks as the box.
- [ ] **Step 5: Run; expect PASS.** `$GO vet ./...` clean.
- [ ] **Step 6: Commit signed and push `v1`** (each push runs CI on `galaxy`; it must stay green).

### Task 9: `internal/gitstore`

**Files:** create `internal/gitstore/{store.go,store_test.go}`.

**Interfaces:**
- Consumes `ledger.Parse` and `ledger.Marshal`.
- Produces:
  - `type Config struct { URL, Branch string; Auth transport.AuthMethod; Signer *openpgp.Entity; Name, Email string }`;
  - `func Open(ctx context.Context, c Config) (*Store, error)`, with in-memory storage;
  - `func (s *Store) Head(ctx context.Context) (sha string, l *ledger.Ledger, err error)`, which fetches;
  - `func (s *Store) Update(ctx context.Context, message string, mutate func(*ledger.Ledger) error) (sha string, err error)`;
  - `func SSHAuth(keyPEM []byte, knownHostsLine string) (transport.AuthMethod, error)`, using `ssh.FixedHostKey`;
  - `var ErrConflict`, after 3 non-fast-forward retries, and `ErrBadMessage`.
- **Messages:** they must match `^(link|revoke|redeem|deactivate) l-[0-9a-f]{16}$`. **Retries:** each one re-fetches and re-runs `mutate`, so `mutate` must be safe to repeat.

- [ ] **Step 1: Write the failing tests** against a local bare repo created in `t.TempDir()`, seeded with an operator-signed root from a test key:
  - `TestHeadReadsLedgerAtTip`;
  - `TestUpdateCommitsSignedWithLedgerKey`: `git verify-commit` (`git` and `gpg` on `PATH`) with a `GNUPGHOME` holding the test ledger key passes, and `%GP` equals its fingerprint;
  - `TestUpdateTouchesOnlyLedgerYAML`: `git diff-tree --name-only` equals `ledger.yaml`;
  - `TestUpdateRaceTwoStores`: two stores update at once; both succeed in turn and the history stays linear;
  - `TestUpdateGivesUpAfterThreeConflicts`: a hook-less racer that always wins gives `ErrConflict`;
  - `TestUpdateRefusesBadMessage`;
  - `TestSSHAuthPinsHostKey`: the callback rejects a different host key;
  - `TestNoPersonalDataInCommitMessage`.
- [ ] **Step 2: Run; expect FAIL:** `$GO test -race -p=4 ./internal/gitstore/...` (needs `git` and `gpg` on `PATH`; the system's are fine).
- [ ] **Step 3: Implement** with go-git's `CommitOptions.SignKey`. Push without force, so a non-fast-forward comes back as `git.ErrNonFastForwardUpdate`, which triggers the retry.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit signed and push `v1`:** `gitstore: in-memory ledger clone, signed fast-forward updates`.

### Task 10: Guarded read-only clients

**Files:** create `internal/keycloak/{client.go,guard.go,client_test.go}` and `internal/github/{status.go,guard.go,status_test.go}`.

**Interfaces:**
- Produces:
  - `keycloak.New(baseURL, realm, clientID string, secret secret.Value, hc *http.Client) *Client`, with:
    - `UsernameTaken(ctx, u string) (bool, error)`;
    - `EmailTaken(ctx, e string) (bool, error)`, which folds to lower case first;
    - `UserEnabled(ctx, u string) (exists, enabled bool, err error)`.
  - `github.NewStatusReader(repo string, tok secret.Value, hc *http.Client) *StatusReader`, with `Get(ctx, sha string) (state, description string, err error)` for context `sso/apply` (`""` when absent).
- **Keycloak's guard** permits only:
  - `POST /realms/master/protocol/openid-connect/token`;
  - `GET /admin/realms/<realm>/users` with query keys from {`username`, `email`, `exact`, `briefRepresentation`, `max`}.
- **GitHub's guard** permits only `GET https://api.github.com/repos/<repo>/commits/<40 hex>/statuses`.
- Both copy forge-bot's wire-form checks, refuse redirects, and set `Proxy: nil`.

- [ ] **Step 1: Write the failing tests** (`httptest`):
  - `TestGuardAllowsOnlyListedCalls`, a table: group listings, `/users/{id}`, a PUT, `..`, `%2e` and another host are all refused before sending;
  - `TestUsernameTakenExact`, `TestEmailTakenLowercases`, `TestUserEnabled`;
  - `TestTokenCachedUntilExpiry`;
  - `TestErrorsNeverContainSecrets`: neither the client secret nor the token appears in `err.Error()` or the `%+v` output;
  - `TestNoRedirectFollow`;
  - `TestStatusReadsOnlySSOApplyContext`, `TestStatusGuardRefusesOtherRepos`, `TestStatusGuardRefusesWrites`.
- [ ] **Step 2: Run; expect FAIL:** `$GO test -race -p=4 ./internal/keycloak/... ./internal/github/...`.
- [ ] **Step 3: Implement** both clients and guards, copying forge-bot's `guardTransport` shape.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit signed and push:** `keycloak, github: guarded read-only clients`.

### Task 11: The rules (`internal/invites`)

**Files:** create `internal/invites/{invites.go,validate.go,quota.go,invites_test.go}` and `internal/audit/{audit.go,audit_test.go}`.

**Interfaces:**
- Consumes the `ledger` package, plus the store from Task 9 and the Keycloak client from Task 10 through two interfaces this package declares:
  - `type Store interface { Head(ctx context.Context) (string, *ledger.Ledger, error); Update(ctx context.Context, message string, mutate func(*ledger.Ledger) error) (string, error) }`;
  - `type Directory interface { UsernameTaken(ctx context.Context, u string) (bool, error); EmailTaken(ctx context.Context, e string) (bool, error); UserEnabled(ctx context.Context, u string) (exists, enabled bool, err error) }`.
- Produces:
  - `type Tier int` with `TierRealliance` and `TierMember`, and `func TierFrom(roles []string) Tier` (`member` in roles means realliance);
  - `type Member struct { Username string; Tier Tier; Operator bool }`;
  - `type GuestInput struct { Username, Email, FirstName, LastName, MFA string }`;
  - `func Normalise(in GuestInput, reserved []string) (ledger.Guest, error)`, returning `*InputError{Field string}`;
  - `func Remaining(l *ledger.Ledger, inviter string, t Tier, now time.Time) int`;
  - `type Service struct`, built by `New(store Store, kc Directory, aud *audit.Logger, open bool, now func() time.Time)`, with:
    - `CreateLink(ctx, m Member) (id, token string, err error)`;
    - `Revoke(ctx, m Member, id string) error`;
    - `Redeem(ctx, token string, in GuestInput) (id string, err error)`;
    - `Status(ctx, token string) (StatusView, error)`;
    - `Deactivate(ctx, m Member, id string) error`;
    - `MyLinks(ctx, m Member) ([]LinkView, error)`;
  - `StatusView{State, Message, Inviter string}`, where `State` is one of `pending`, `created`, `waiting` or `contact-inviter`;
  - errors `ErrClosed`, `ErrQuota`, `ErrNotYours`, `ErrNotPending`, `ErrExpired`, `ErrInviterGone`, `ErrUsernameUnavailable` and `ErrEmailUnavailable`;
  - `audit.Logger`, with `Event(kind, linkID string, fields map[string]string)`. The field keys come from a fixed allowlist with no email, name or token key.
- Values: the TTL is 168h. The token is 32 bytes from `crypto/rand`, as base64url without padding; the ledger stores only its SHA-256. `Remaining` uses the box's slot rule (D18) on the service's clock: 5 per 7 days for realliance and 1 for life for member.

- [ ] **Step 1: Write the failing tests**:
  - `TestTierFromRoles`;
  - `TestRemainingRealliance`: 5 at start, 0 after 5 links in 7 days, back to 1 when one redemption is 7 d and 1 s old;
  - `TestRemainingMemberLifetime`: a revoked link frees its slot, a redemption doesn't, and an expired link does;
  - `TestCreateLinkTokenIs256Bit`: `len(token) == 43`, and the decoded token is 32 bytes;
  - `TestCreateLinkStoresOnlyHash`: the marshalled ledger lacks the token and holds `sha256(token)`;
  - `TestCreateLinkRefusedWhenClosed`, `TestCreateLinkRefusedOverQuota`;
  - `TestRevokeOnlyOwnPending`: an operator may revoke any link;
  - `TestRedeemValidates`, a table: D16's username, email and name rules; MFA must be `webauthn` or `totp`, with `none` and `""` refused;
  - `TestRedeemNormalisesEmail`: the ledger stores `" Bob@Example.COM "` as `bob@example.com`;
  - `TestRedeemRefusesTakenUsernameAndEmail`, from Keycloak and from another ledger guest;
  - `TestRedeemRefusesWhenInviterDisabled`;
  - `TestRedeemRefusesExpiredOrUsed`: a second redemption with the same token gives `ErrNotPending`;
  - `TestRedeemRetriesOnRaceAndRevalidates`: a racer takes the username between attempts, giving `ErrUsernameUnavailable`;
  - `TestDeactivateOwnGuestsOnly`;
  - `TestStatusAfter20MinutesSaysContactInviter`: 19 min with no user gives `waiting`, 21 min gives `contact-inviter` with the inviting member's username, and a user existing gives `created`;
  - `TestAuditLinesIdsOnly`: captured lines contain no fixture email, name or token.
- [ ] **Step 2: Run; expect FAIL:** `$GO test -race -p=4 ./internal/invites/... ./internal/audit/...`.
- [ ] **Step 3: Implement**, with fakes for `Store` and `Directory` in `fakes_test.go`. `Redeem` re-runs every check inside the `mutate` it hands to `Update`, so a retry after a race re-validates against the newer ledger.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit signed and push:** `invites: tiers, quotas, links, redemption, status`.

### Task 12: The web layer

**Files:** create `internal/web/{web.go,auth.go,session.go,pages.go,templates/*.html,static/invite.js,web_test.go}` and `internal/limit/{limit.go,limit_test.go}`.

**Interfaces:**
- Consumes `invites.Service`.
- Produces:
  - `web.New(cfg Config, svc *invites.Service, oidc OIDC, keys SessionKeys, lim *limit.Limiter, aud *audit.Logger) http.Handler`, where:
    - `type Config struct { PublicURL string; Open bool; Operators []string }`;
    - `type OIDC interface { AuthURL(state, nonce, verifier string) string; Exchange(ctx context.Context, code, verifier string) (Claims, error) }`, with `Claims{Username, Nonce string; Roles []string}` (`Username` from `preferred_username`);
    - `type SessionKeys struct { Key []byte }`;
    - `limit.Limiter` has `AllowToken(tokenHash string) bool` and `AllowGlobal() bool`.
  - Routes on `:8080` only:
    - `GET /` (member page);
    - `GET /auth/login` and `GET /auth/callback`;
    - `POST /links`, `POST /links/{id}/revoke` and `POST /guests/{id}/deactivate` (form plus CSRF);
    - `GET /i` (the invitee page; the token stays in the fragment, and `invite.js` posts it);
    - `POST /api/redeem` and `POST /api/status` (JSON body with `token`);
    - `GET /healthz`.
  - `limit.New(perToken int, perTokenWindow time.Duration, global int, globalWindow time.Duration)`, with values 5 per hour per token and 60 per minute globally.
- **Sessions:** cookie `__Host-sso-invites` (Secure, HttpOnly, SameSite=Lax, Path=/, 1 h) holding `{username, tier, operator, exp}` with an HMAC (SHA-256) under `INVITES_SESSION_KEY`.
- **Sign-in:** the auth cookie `__Host-sso-invites-auth` (5 min) holds `state`, `nonce` and the PKCE verifier.
- **Every POST** needs `Origin: https://invite.realliance.net`. Form POSTs also need `csrf = HMAC(key, "csrf|" + session)`.
- **Headers on every response:** `Referrer-Policy: no-referrer`, `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'; form-action 'self'`, `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`.

- [ ] **Step 1: Write the failing tests** (a fake OIDC provider on `httptest` issuing ID tokens with a `roles` claim):
  - `TestLoginRedirectsWithPKCEAndState`;
  - `TestCallbackRejectsBadStateOrNonce`;
  - `TestCallbackRequiresUserRole`: an ID token without `user` in `roles` gives 403;
  - `TestSessionCookieAttributes`;
  - `TestPostsNeedCSRFAndOrigin`: a missing or wrong `csrf`, or a foreign `Origin`, gives 403;
  - `TestTokenNeverInPathOrQuery`: the created link matches `^https://invite\.realliance\.net/i#[A-Za-z0-9_-]{43}$`, and no route reads a token from the path or query;
  - `TestReferrerPolicyNoReferrer`, together with the other three headers on every route;
  - `TestNoTokenInLogs`: `slog` output captured across create, redeem and status holds no token;
  - `TestPerTokenAttemptLimit`: the sixth attempt within an hour gives 429;
  - `TestGlobalRedeemLimit`: attempt number 61 in a minute gives 429;
  - `TestFormOffersOnlyWebauthnAndTotp`: the radio values are exactly `webauthn` (checked) and `totp`;
  - `TestMetricsNotOnPublicMux`: `/metrics` on the web handler gives 404;
  - `TestClosedShowsNotOpenYet`: with `open: false` the member page shows no create button and `POST /links` gives 409.
- [ ] **Step 2: Run; expect FAIL:** `$GO test -race -p=4 ./internal/web/... ./internal/limit/...`.
- [ ] **Step 3: Implement.** The real `OIDC` uses go-oidc's verifier (issuer `https://sso.realliance.net/realms/realliance`, audience `invites`) and `oauth2` with `S256ChallengeOption`. Templates come from `embed.FS`; `invite.js` reads `location.hash`, clears it with `history.replaceState`, and POSTs JSON.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit signed and push:** `web: sign-in, sessions, member and invitee pages, limits`.

### Task 13: Watcher, metrics, `main` and the release

**Files:** create `internal/watch/{watch.go,watch_test.go}`, `internal/metrics/{metrics.go,metrics_test.go}`, `cmd/sso-invites/{main.go,main_test.go}` and `internal/config/{config.go,config_test.go}`.

**Interfaces:**
- **Config** (`internal/config`): `public_url`, `open`, `oidc{issuer, client_id, client_secret_file}`, `keycloak{url, realm, client_id, client_secret_file}`, `ledger{url, branch, deploy_key_file, known_hosts_file, signing_key_file, status_repo, status_token_file}`, `session_key_file`, `operators[]` and `reserved[]`. `Load` refuses unknown keys and missing files.
- **Metrics** (on `:9090`):
  - `sso_invites_ledger_head_unapplied_seconds`;
  - `sso_invites_ledger_head_refused` (0 or 1);
  - `sso_invites_redemptions_without_user`;
  - `sso_invites_ledger_push_failures_total`;
  - `sso_invites_ledger_push_last_success_timestamp`;
  - `sso_invites_guest_deactivations_total`;
  - `sso_invites_orphan_guests`;
  - `sso_invites_watch_last_success_timestamp`;
  - `sso_invites_requests_total{route,code}`, with fixed label sets.
- **The watcher** (`watch.New(store, status, kc, m, aud, now)`, `Tick(ctx) error`):
  - it runs every 60 s;
  - **not applied** is now minus the first-seen time of the first head seen after the last head that had success, or 0 while the current head has success;
  - **refused** is 1 while the current head's status is `failure`;
  - **without a user** counts redeemed links older than 20 min whose username Keycloak lacks, up to 7 days after redemption;
  - **deactivated** counts each `active` `true` to `false` once, with an audit line;
  - **orphans** counts active guests whose inviting member's account is off or missing;
  - a failed tick keeps the last values.

- [ ] **Step 1: Write the failing tests**:
  - `TestHeadUnappliedSeconds`: pending for 61 min gives at least 3660, then success gives 0;
  - `TestRefusedHeadGauge`;
  - `TestRedemptionWithoutUserAfter20Min`: 19 min gives 0 and 21 min gives 1;
  - `TestDeactivationCountedOnce`;
  - `TestOrphansCountsDisabledInviters`;
  - `TestWatchFailureKeepsGauges`;
  - `TestMetricsHaveNoPersonalLabels`: every label value comes from a fixed set;
  - `TestConfigRefusesUnknownKeys`;
  - `TestMainRefusesToStartWithoutSecrets`;
  - `TestHealthz`: 503 after three intervals with no successful tick.
- [ ] **Step 2: Run; expect FAIL:** `$GO test -race -p=4 ./...`.
- [ ] **Step 3: Implement** `watch`, `metrics` (`client_golang`, its own registry), `config` and `main`. `main` serves the web handler on `:8080` and `/metrics` plus `/healthz` on `:9090`, and finishes the request in flight on SIGTERM within 20 s.
- [ ] **Step 4: Run; expect PASS.** `$GO vet ./...` is clean, and the branch's CI run (with `govulncheck`) is green.
- [ ] **Step 5: Release.**
  - Open the PR from `v1`. **Chris:** merges fast-forward and signs tag `v0.1.0`.
  - The `main` CI run prints `git.realliance.net/realliance-ops/sso-invites:sha-<7>@sha256:<64>`. Ledger the digest.
  - **STOP—ask Chris to review the release:** `git verify-tag v0.1.0` and `git log -p` of the branch. Ledger his approval.
  - Link the package to the repo, as forge-bot's was.

### Task 14: The manifests (not deployed)

**Files:**
- Create (luma-homeops): `cluster/sso-invites/{kustomization.yaml,namespace.yaml,deployment.yaml,service.yaml,ingress.yaml,networkpolicy.yaml,servicemonitor.yaml,prometheusrule.yaml,alerts_test.yaml,config.yaml,known_hosts,secrets.secret.yaml,secrets.yaml}`, `cluster/applications/sso-invites.yaml`, `scripts/test_sso_invites_manifests.py` and `scripts/test-sso-invites-alerts.sh`.
- Modify: `.pre-commit-config.yaml` (hook `sso-invites-manifests`) and `.renovaterc.json`.

**Interfaces:**
- **Namespace and secret:** namespace `sso-invites` with Pod Security `restricted` (enforce, warn, audit). The SealedSecret `sso-invites` holds the keys `oidc-client-secret`, `keycloak-client-secret`, `ledger-deploy-key`, `ledger-signing-key`, `status-token` and `session-key`, projected 0440 at `/etc/sso-invites/secrets/`.
- **Deployment:** 1 replica; image pinned by digest; uid 65532; read-only root; every capability dropped; RuntimeDefault seccomp; no service account token; no service links; `GOMEMLIMIT=96MiB`; requests `10m`/`32Mi`, limit `128Mi`; ports `http` 8080 and `metrics` 9090; liveness and readiness on `/healthz`.
- **Edge:** the Ingress names host `invite.realliance.net` with no `spec.tls`, and its annotation `traefik.ingress.kubernetes.io/router.middlewares: sso-invites-ratelimit@kubernetescrd` points at the `Middleware` `ratelimit` (`average: 30`, `period: 1m`, `burst: 30`, `sourceCriterion.ipStrategy.depth: 0`, revisited at Task 16).
- **NetworkPolicy:** the `default-deny` and `allow-dns` components. In: namespace `traefik` on 8080, and namespace `prometheus` on 9090. Out: TCP 443 to `0.0.0.0/0`, except ci-dispatch's private ranges and `152.44.247.0/25`.
- **Alerts:** `SSOInvitesHeadNotApplied` (two rules with `reason="pending"` for not-applied seconds over 3600, and `reason="refused"` for refused equal to 1 for 1 h), `SSOInvitesRedemptionNoAccount` (over 0 for 5 m), `SSOInvitesLedgerPushFailing`, `SSOInvitesGuestDeactivated` (any increase over 10 m), `SSOInvitesOrphanGuests` (over 0 for 1 h) and `SSOInvitesWatchStale` (over 600 s). All carry `severity: warning`.
- **Config:** `config.yaml` sets `open: false`, `ledger.url: ssh://git@ssh.github.com:443/<ledger-org>/sso-invites.git`, the operators from Task 0, and a public `reserved` subset (bots, orgs, `www`). `known_hosts` holds `[ssh.github.com]:443` with `github_ssh_host_key`'s value from sso `group_vars`.

- [ ] **Step 1: Write the failing static tests** in `scripts/test_sso_invites_manifests.py`, in the shape of `test_ci_dispatch_manifests.py`, rendering with `kubectl kustomize` and never reading `*.secret.*`:
  - `test_namespace_restricted_psa`, `test_deployment_hardening`;
  - `test_image_pinned_by_digest`, pattern `^git\.realliance\.net/realliance-ops/sso-invites:sha-[0-9a-f]{7}@sha256:[0-9a-f]{64}$`;
  - `test_ingress_one_host_no_tls_no_dns_annotations`, `test_ratelimit_middleware`;
  - `test_metrics_port_not_routed`;
  - `test_networkpolicy_exact`;
  - `test_servicemonitor_release_label`;
  - `test_alert_names`;
  - `test_only_sealed_secrets_rendered`;
  - `test_config_values`;
  - `test_known_hosts_pins_github_key`;
  - `test_renovate_never_automerges_sso_invites`;
  - `test_hook_watches_every_input`;
  - `test_app_listed_only_when_deployed`, keyed on `DEPLOYED = False`.
- [ ] **Step 2: Write `alerts_test.yaml`**: each alert fires on its trigger and stays quiet on its healthy twin. Write `scripts/test-sso-invites-alerts.sh` as a copy of the ci-dispatch script.
- [ ] **Step 3: Run; expect FAIL.**
- [ ] **Step 4: Write the manifests.** Write `secrets.secret.yaml` with a `python -I` script that reads `invites/secrets.env` and the two key files from sso-secrets, writes the Secret and prints its byte count. Then `./sign.sh`.
- [ ] **Step 5: Run the static tests and `nix shell --inputs-from . nixpkgs#prometheus.cli nixpkgs#yq-go --command scripts/test-sso-invites-alerts.sh`; expect PASS.**
- [ ] **Step 6: Commit and push.** The Argo `Application` file isn't listed in `cluster/applications/kustomization.yaml`, so Argo renders nothing new and the push is routine. Expected: the hook passes.

### Task 15: Switch to `apply`, and a hand entry end to end

**Files:** modify sso `host/group_vars/sso.yml` (`ledger_mode: apply`) and `tests/host/test_config_apply.py`.

**Interfaces:** consumes Tasks 4 and 6 (the gate), and Task 6's helpers.

- [ ] **Step 1: Preconditions.** Task 6's exit gate is in the progress ledger. Push one operator-signed commit that sets `active: false` on every `chk-` guest (D5) and revokes every drill link still `pending`, since a pending link would hold a slot of Chris's after the switch. Then read `box_result()`. Expected: `would_admit == []`.
- [ ] **Step 2: Commit, push and open the PR. STOP—ask Chris (the check-to-apply switch; box head).** Evidence: the check-window summary and the days elapsed. What changes: the box starts handing admitted guests to the people layer. Watch: the first `apply` run's result is `apply: 0 admitted`, and `test_box_plans_are_clean` stays clean. Rollback trigger: any people apply failure, or any guest admitted that wasn't expected. Rollback: a revert to `check`. Admitted guests stay handed, by D2.
- [ ] **Step 3: Chris merges.** `Monitor` the pull, then the first apply. Expected: the head status `apply: 0 admitted, 0 refused, 0 deferred`.
- [ ] **Step 4: STOP—ask Chris (the first operator-signed hand entry).** One link, and one redemption by `hand-<hex>`, `delivered+hand-<hex>@resend.dev`, `mfa: totp`. The inviting member is Chris, by his own `people.yaml` username, which uses one of his 5 slots for 7 days. Expected within 10 min:
  - a Keycloak user `hand-<hex>`, enabled, groups exactly `{guests}`, `mfa` `["totp"]`;
  - a Resend mail to that address (`tests/realm/test_enrolment.py`'s helper);
  - the head status `apply: 1 admitted`.

  Ledger the minutes from push to mail. Then an operator-signed deactivation: on the next run, `enabled is False`. Keep the guest for Task 20's erasure rehearsal.

### Task 16: Deploy the service, closed

**Files:** modify `cluster/applications/kustomization.yaml` (list `sso-invites.yaml`) and `scripts/test_sso_invites_manifests.py` (`DEPLOYED = True`).

- [ ] **Step 1: Preconditions.** Task 14 pins Task 13's digest and is on `main`, and Gate G before is clean. Load the `edge-ingress` skill and take its snapshot (G3, before).
- [ ] **Step 2: STOP—ask Chris (deploy).** The change: a new public host `invite.realliance.net` with `open: false`, a pod that holds the ledger signing key, and a view-only Keycloak reader. Watch: the rollout, `/healthz`, `sso_invites_watch_last_success_timestamp`, and the edge snapshot after. Rollback trigger: a crash loop, a deny line for its own traffic, or any edge snapshot change besides the new host. Rollback: revert the listing; Argo prunes.
- [ ] **Step 3: Push and `Monitor` the rollout** with the `safe-rollout` skill, then run Gate G after, including the edge snapshot diff.
- [ ] **Step 4: Read-only checks.**
  - Sign in as Chris: the member page says invites aren't open yet.
  - `sso_invites_ledger_head_unapplied_seconds == 0`.
  - Prometheus has loaded the alerts: `kubectl -n sso-invites get prometheusrule` and Prometheus `/api/v1/rules`.
- [ ] **Step 5: Client IP evidence (Review Focus 5).** Find the latest request to `invite.realliance.net` in `kubectl -n traefik logs deploy/traefik` and read its `ClientHost` (an IP, no path). If it's a node or pod address, set the `Middleware` values `average` and `burst` to 120, rely on the service's own limits, and ledger the finding for Chris as an edge follow-up. Changing `externalTrafficPolicy` is out of scope here. If it's a WAN address, keep 30.

### Task 17: The storage cap ladder

**Files:**
- Modify: `cluster/applications/forgejo-release.yaml` (`gitea.config.quota: {ENABLED: true, DEFAULT_GROUPS: guests}`) and `cluster/forgejo/kustomization.yaml`.
- Create: `cluster/forgejo/prometheusrule.yaml`, `cluster/forgejo/alerts_test.yaml`; forge `quota.sh`; sso `tests/forge/test_forge_quota.py`.

**Interfaces:**
- **`quota.sh [all | member <name>]`** (idempotent, with the administrator token from the README's env). It calls only the `/admin/quota` routes that Forgejo 15 registers in `routers/api/v1/api.go` (rules, groups and user mappings). Take the exact paths from that file in `~/Repos/realliance-forgejo/upstream` and ledger them.
  - rule `guest-2gib`: subjects `[size:all]`, limit `2147483648`;
  - rule `unlimited`: `[size:all]`, limit `-1`;
  - group `members` holds `[unlimited]` and group `guests` holds `[guest-2gib]`;
  - `all` maps every org and every user account to `members`, and creates group `guests` **last**, once nothing unmapped remains except guests;
  - `member <name>` maps one account or org: a new member after first sign-in, a promoted guest, or a new org from `orgs.tf`.
- **Alert** `ForgeRepoStorageJump`: `increase(kubelet_volume_stats_used_bytes{namespace="forgejo",persistentvolumeclaim="forgejo-repos"}[1h]) > 2147483648` for 5 m, `severity: warning`.

- [ ] **Step 1: Facts, read-only.**
  - Confirm `kubelet_volume_stats_used_bytes{persistentvolumeclaim="forgejo-repos"}` returns a series.
  - Through the administrator API, ledger the largest org and account sizes as numbers, without names. They show whether any existing owner is near 2 GiB.
  - Ledger D11's source facts.
- [ ] **Step 2: STOP—ask Chris (the ladder ruling).**
  - **Path 1 (recommended):** `DEFAULT_GROUPS` caps unknown accounts from creation, and `quota.sh` maps members and orgs, rerun each time a member joins and at each promotion.
  - **Path 2:** forge-bot maps each new guest after first sign-in. That needs a forge-bot change, and this plan would stop at the alert until that plan ships it.
  - **Path 3:** no quota, the alert and a runbook step, and the gap goes under the spec's Known limits.

  Every path ships the alert. Ledger the ruling.
- [ ] **Step 3: Write the failing tests** in `tests/forge/test_forge_quota.py`:
  - `test_unmapped_account_gets_guest_cap`: a throwaway forge account's quota, read through the administrator API, shows group `guests` and limit 2147483648;
  - `test_members_and_orgs_unlimited`;
  - `test_cap_is_enforced`: a throwaway account mapped to a temporary `test-1mib` group gets its 2 MiB push refused;
  - `test_quota_script_idempotent`: a second `all` run changes nothing.

  Add an `alerts_test.yaml` case for `ForgeRepoStorageJump` too.
- [ ] **Step 4: Implement.**
  - **Path 1:** `quota.sh`, its inventory PR (**Chris:** merges fast-forward), the `forgejo-release.yaml` quota settings, and the alert.
  - **Path 2 or 3:** the alert only.
- [ ] **Step 5: Gate G, then STOP—ask Chris (deploy; Forgejo restarts).**
  - **Path 1:** Forgejo starts with quota on and no `guests` group, so no limit applies yet. Then, after its own approval in this STOP, run `quota.sh all`: it maps everything first and creates `guests` last.
  - Rollback trigger: any member or org push refused, or Forgejo not Ready in 10 min. Rollback: delete group `guests` through the API (unmapped accounts fall back to no limit), then revert.
- [ ] **Step 6: Push, `Monitor`, Gate G after, and run the tests; expect PASS.** Ledger the outcome as the gate for Task 18.

### Task 18: Guests reach the forge

**Files:** modify sso-people `guests.tf` (`local.guest_groups = ["/guests", "/apps/forgejo"]`) and sso `tests/people/test_people.py`.

- [ ] **Step 1: Precondition.** Task 17 has finished on the ruled path: on path 1, `test_unmapped_account_gets_guest_cap` passes live; on path 3, Prometheus has loaded `ForgeRepoStorageJump`.
- [ ] **Step 2: Write the failing test** `test_guest_groups_include_forgejo` (sandbox): groups are exactly `{guests, forgejo}`.
- [ ] **Step 3: Run; expect FAIL:** `SSO_PEOPLE_DIR=~/Repos/realliance-sso-people-invites taskset -c 0-3 nix develop ~/Repos/realliance-sso-invites --command pytest tests/people -q -k guest`.
- [ ] **Step 4: Implement** the one-line `local.guest_groups` change. **Run again; expect PASS.**
- [ ] **Step 5: Commit, push and open the PR. STOP—ask Chris (granting `/apps/forgejo` to guests).** Evidence: the ladder outcome. Watch: `hand-<hex>`, still turned off, gains `forgejo`; nothing else changes. Rollback: revert, which is guardrail 3: one switch for all guests.
- [ ] **Step 6: Chris merges.** After the box applies, `_groups_of(hand-<hex>) == {"guests", "forgejo"}` and `test_box_plans_are_clean` passes.

### Task 19: Open the service, and acceptance

**Files:** create sso `tests/invites/test_invites.py` and `tests/invites/test_invites_box.py`; modify `tests/invites_helpers.py`, luma-homeops `cluster/sso-invites/config.yaml` (`open: true`) and `scripts/test_sso_invites_manifests.py` (`test_config_values` expects `open: true`), and sso-people `people.yaml`, which gains `invites-test` through a PR the agent writes with a `python -I` script that appends the entry and prints nothing.

**Interfaces:**
- `invites_member()`: resets `invites-test`'s password through the administrator API, deletes its OTP credentials, and returns a `User` for TOTP enrolment at sign-in.
- `invites_login(user)`, in the shape of `forge_login_attempt`, returns a signed-in `requests` session.
- `box_fixture_run(commits, people, state=None) -> dict`: builds a temp ledger repo locally, copies it and a fixture `people.yaml` (throwaway names only) to `/tmp/ledger-fixture-<hex>` on the box, runs `/usr/local/sbin/sso-ledger check --mode check` against the real keyring and Keycloak, returns `result.json`, and removes the directory.
- `erase_test_guest(username)`: under the box lock, `sso-ledger forget`, `tofu state rm` and a Keycloak delete (D6 step 2). The ledger history keeps the deactivated throwaway entry, which holds no real person's data.

- [ ] **Step 1: Land `invites-test`.** **Chris:** merges the sso-people PR. Expected: the user exists with `/realliance` and `/apps/invites`.
- [ ] **Step 2: Commit the `open: true` change. Gate G, then STOP—ask Chris (deploy `open: true`).** Watch: one throwaway link per test, the Resend count (under 10 for the run), and the alerts. Rollback: `open: false`. On approval, push and `Monitor` the rollout.
- [ ] **Step 3: Write the acceptance tests**, mapped to the spec's "Success looks like":
  1. `test_member_link_redeem_mail_and_forge` (1). As `invites-test`, create a link and redeem it as `acc-<hex>` with `mfa: totp`, so the sign-in helper can enrol TOTP. Then:
     - the mail reaches Resend within 10 min, and the user has groups `{guests, forgejo}`;
     - after an administrator sets a password, `forge_login` succeeds with no team;
     - the `invites` client refuses the guest.
  2. `test_quotas_on_installed_code` (2), through `box_fixture_run`:
     - the second link of a one-for-life member is refused until the first is revoked;
     - a sixth link in 7 days is refused;
     - an eleventh redemption in 24 h is deferred `cap`.
  3. `test_bad_heads_refused_while_people_apply` (3):
     - live, with the ledger key: a `groups` commit and a redemption without `mfa` are refused;
     - an over-quota and a non-fast-forward head via `box_fixture_run`, and a web-flow commit via `web_flow_commit`;
     - while a head is refused, `sso-config-apply force` exits 0 with `sso/apply` success on the `sso-people` head.
     - Each refused head ends with an operator-signed fix.
  4. `test_ledger_faults_leave_people_apply[check|apply]` (4): on the box, `SSO_LEDGER_BIN=/bin/false`, `SSO_LEDGER_TIMEOUT=1` and `LEDGER_DIR=/nonexistent` each give exit 0 and people applied. The `apply` case runs live; the `check` case through the harness from Task 4.
  5. `test_refused_head_alerts_and_override_disables` (5, `@pytest.mark.slow`, over 65 min): a refused head raises `SSOInvitesHeadNotApplied{reason="refused"}`. While it stays refused, **Chris** merges an override entry for `acc-<hex>` (`active: false`, `groups: [/guests]`, no `bootstrap`), and `enabled is False` on the next run. Then the fix head, and the alert resolves.
  6. `test_deactivated_inviter_link_dies` (6), via `box_fixture_run` with the inviting member set inactive: `inviter-inactive`. Live: the orphan gauge counts `acc-<hex>` while the throwaway inviting member's account is off.
  7. `test_promotion_keeps_user_id` (7): Task 5's sandbox test, run again here and cited.
  8. `test_guest_ci_defaults_then_deny` (8):
     - `acc-<hex>` pushes a public repo with a `runs-on: tiny` workflow, and the job runs;
     - **Chris** merges a `deny:` entry `{name: acc-<hex>, id: <id>}`, and the next job holds as `denied` within one sweep (about 5 min after the merge);
     - **Chris** merges its removal.
  9. `test_hand_entry_needs_no_service` and `test_signin_needs_no_galaxy` (9): Task 15's hand entry stands as the proof. No authenticator config in the realm names a galaxy host, and `hand-<hex>` signs in to the account console.
  - (10) is Task 20's rehearsal.
  - Teardown: deactivate through the service, then `erase_test_guest`.
- [ ] **Step 4: Run them:** `taskset -c 0-3 nix develop ~/Repos/realliance-sso-invites --command pytest tests/invites --hosts=ssh://root@23.163.0.48 -q`, then again with `-m slow`. Expected: all pass. Ledger the results and the Resend count.
- [ ] **Step 5: Commit the tests, push and open the PR.** **Chris:** merges.

### Task 20: Runbooks, the erasure rehearsal and the first real invite

**Files:**
- Create: luma-homeops `docs/sso-invites.md`.
- Modify: luma-homeops `docs/forgejo.md` (Who gets in: `/guests`; guest offboarding; the quota step when a member joins) and `CLAUDE.md` (a topology row); sso `docs/runbook.md`, `README.md` and `WISHLIST.md` (Member invites: done, with links); forge `README.md` (New org: `quota.sh member <org>` on path 1); the sso-people and ledger README files.

- [ ] **Step 1: Write `docs/sso-invites.md`** (vale-clean), covering:
  - the shape;
  - modes and switching them;
  - reading `result.json` and the statuses;
  - **a refused head:** find the bad commit, then push an operator-signed fix or revert;
  - **the emergency override;**
  - **guest offboarding:** the ledger deactivation, Prohibit login until forge-bot's `access` runs in `apply`, `allow_bulk_until` for more than 3, the Pages takedown per Chris's open-point answer, and the `deny:` entry;
  - **a member leaving**, the orphan alert, and its promoted-guest caveat (D14);
  - **promotion,** with `quota.sh member <name>` on path 1;
  - **erasure (D6),** where steps 3 and 4 are Chris's;
  - **a box rebuild:** the seed, and the clock reset;
  - **rotating** the ledger key (overlap two fingerprints in `ledger_signing_fingerprints`), the deploy keys and both status tokens, with the ledgered dates;
  - each alert's response.
- [ ] **Step 2: Vale every changed doc**; expected no warnings or errors. Commit each repo signed. luma-homeops docs are routine; **Chris:** merges the sso and sso-people PRs.
- [ ] **Step 3: STOP—ask Chris (the erasure rehearsal, success 10).** Run D6 on `hand-<hex>`: steps 1–2 by the agent, steps 3 (`git filter-repo` plus a force-push of a head he signs) and 4 (re-clones) by Chris. Expected after the next run:
  - the head status is success;
  - no `hand-<hex>` in Keycloak, in `tofu state list` or in `state.json`;
  - `git log --all -S hand-` in a fresh clone finds nothing.

  Ledger the timings.
- [ ] **Step 4: STOP—ask Chris (the first real invite).** A member of his choosing makes a link for a real person. Watch: the head status, the mail inside 10 min, the guest's first forge sign-in, and no alert. Ledger the link id and the minutes only.
