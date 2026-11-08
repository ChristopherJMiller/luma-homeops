# SSO member invites implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a Realliance member hands someone a link, that person fills in a form, and within 10 minutes a signed commit to `ledger.yaml` in `sso-people` has become a guest account (SSO plus forge) that the SSO box created, with every rule enforced on the box.

**Architecture:**
- **The ledger:** `ledger.yaml` at the root of private `realliance/sso-people`, beside `people.yaml`. The box reads it from the checkout it already pulls; nothing new exists on GitHub.
- **The box check:** `sso-ledger`, one Python file in public `realliance/sso` that `sso-config-apply` runs before the people apply.
  - It walks the first-parent chain since its last good commit and checks each signer against the paths that commit changes: the ledger key may change `ledger.yaml` alone, an operator key any path. **On `sso-people` web-flow is never trusted**, so a GitHub merge-button commit is invalid; operators merge locally, signed.
  - Any invalid commit, a head that doesn't fast-forward, or a head it saw earlier that left the first-parent line freezes every `sso-people` apply until an operator-key commit carrying `Reviewed-Range` clears it.
  - On a valid chain it checks the ledger's content, quotas, caps and collisions on the box's clock, then hands the admitted guests to the people layer as a var-file.
  - It ships in `check` mode first, anchored at the commit Chris records at the changeover.
- **The service:** `sso-invites`, a small Go service on galaxy at `invite.realliance.net`. It signs members in through a new `invites` client and writes `ledger.yaml` alone in commits signed with the ledger key, pushed straight to `main` with a rebase-and-retry. It reads Keycloak through a view-only service account and alerts on outcomes it can see.
- **Forge access:** guests reach the forge last, after a Forgejo storage cap.

**Tech Stack:** the box runs Python 3.12 with PyYAML (the box's `/opt/ansible` virtual environment), GnuPG, git, bash and `systemd` on AlmaLinux 9; OpenTofu 1.12.6 with `keycloak/keycloak ~> 5.10.0`; `ansible`; `git-filter-repo`; Keycloak 26.8.1; Go 1.26 (toolchain go1.26.10) with `go-git/v5`, `ProtonMail/go-crypto`, `go.yaml.in/yaml/v3`, `coreos/go-oidc/v3`, `golang.org/x/oauth2`, `prometheus/client_golang`; ko plus distroless; Forgejo Actions on the `galaxy` runner; Argo CD, SealedSecrets, Traefik v3, kube-prometheus-stack; pytest with `pytest-testinfra` in `realliance/sso`.

**Spec:** `docs/superpowers/specs/2026-11-07-sso-member-invites-design.md` with both 2026-11-08 amendments: the ledger is a file in `sso-people` (`07710df0`), and `sso-people` never trusts web-flow (`795a3b86`). Read both amendment notes first, then the whole spec. Related specs: `2026-10-31-realliance-sso-design.md` (with its 2026-11-02 amendment), `2026-11-02-forge-bot-and-ci-design.md`, `2026-11-04-runner-classes-design.md`.

**Status (2026-11-08).** Tasks 0, 1 and 2 have finished; the SDD ledger at `.superpowers/sdd/2026-11-08-sso-member-invites/progress.md` holds their record. Task 2b is next.

## Global Constraints

### From the spec (verbatim values)

- **Git creates the account.** No reconciler, no Keycloak-first write, Organizations off. The service writes only `ledger.yaml`; Keycloak's `invite-user` sends the set-up mail (72 h link).
- **The ledger** is `ledger.yaml` at the root of `realliance/sso-people` *(Chris decided, 2026-11-08)*. The service writes it with a write deploy key on `sso-people`, operators edit it like any file there, and the box reads it from the checkout it already pulls. No org, no second repo, no second read-only deploy key.
- **Schema.** `version` (`1`, operator only); `links[]`: `id`, `inviter`, `created`, `expires` (at most 7 days after `created`), `token_sha256`, `state` (`pending`, then `redeemed` or `revoked`), `redeemed`, `revoked`, `guest` {`username`, `bootstrap` {`email`, `first_name`, `last_name?`}, `mfa` (`webauthn` or `totp`, never empty or `none`), `active`}. Unknown keys are refused, and no `groups` key exists at any level. Only an operator's erasure rewrite removes anything.
- **Who may change `ledger.yaml`:** the spec's transitions table. The ledger key may add a `pending` link, move `pending` to `revoked` or to `redeemed` with a `guest` block, and set `guest.active` from `true` to `false`. Everything else (reactivation, an immutable field, a removal) needs an operator key.
- **No web-flow on `sso-people`** *(Chris decided, 2026-11-08, after a Critical finding)*. GitHub's web-flow key signs every web-editor commit, for anyone, in any repo, so a compromised service could push one that changes `people.yaml`. `sso-people` trusts operator keys and the path-limited ledger key only; `realliance/sso` keeps web-flow.
- **Commits.** The box walks the first-parent chain from its last good commit to the head. The ledger key may change `ledger.yaml` alone, in a commit with one parent: any other path, a rename, a mode change or a submodule makes the commit invalid. An operator key may change any path, and its `ledger.yaml` changes count as operator edits. **GitHub's web-flow key, no signature, or any other key makes the commit invalid**, a merge-button merge and a web-editor commit made in any repo included. The box keeps no exemption for GitHub merges: every commit carries its own signature.
- **Merging.** Operators land every `sso-people` PR locally, as a squash or fast-forward commit signed with their own key, and push it to `main`. The GitHub merge button makes a web-flow commit, which freezes the box until an operator's `Reviewed-Range`.
- **A freeze.** An invalid commit since last good (a web-flow commit included), or a head that doesn't fast-forward from it, freezes `sso-people`: no `sso-people` apply at all, last good included, a failure status, and the service alerts. A compromised service, anyone holding its deploy key, or any org owner with write access can freeze member applies this way; none of them can land a change *(Chris decided: the accepted cost)*.
- **Recovery.** An operator reverts the bad change and, with an operator key (never web-flow or the ledger key), signs a commit carrying the trailer `Reviewed-Range: <last-good>..<tip>`, both full shas. The box accepts it only if `<last-good>` is its own last good, `<tip>` is an ancestor of that commit, and every invalid commit falls inside the range. It then checks the commits after `<tip>` as usual. A replayed old recovery commit fails. This is also the only way the box takes a history that doesn't fast-forward.
- **Changeover.** When the box first runs without web-flow trust on `sso-people`, it takes the current, already-applied head as last good, so the web-flow history before it is never walked. The host config records that commit as the anchor commit, which also anchors a rebuilt box.
- **Content.** On a valid chain the box validates `ledger.yaml` at the head. A schema, immutability, removal or transition failure refuses the ledger: guests stay at the last good ledger, later ledger changes wait, and `people.yaml` still applies. A bad new guest refuses the entry; over a rate cap defers it. **A content problem in a validly signed, path-correct commit never freezes `people.yaml`.** The remedy for a refused ledger is a commit signed with an operator key.
- **Quotas.** An active person in `/realliance`: 5 per rolling 7 days, a pending link or a redemption holding a slot for 7 days. Any other active person in `people.yaml`: 1 for life, a pending link or a redemption holding it for good. A revoked or expired link frees its slot; deactivating or promoting the guest doesn't. A guest or an inactive person: 0, and their pending links die at redemption. Everyone together: 10 redeemed links per rolling 24 h; one over the cap waits for a later run.
- **Modes.** `ledger_mode` reads `check` or `apply`, and only an operator-signed `realliance/sso` head changes it. In `check` the box applies `people.yaml` as today, but from the newest first-parent commit that's last good or an operator key signed, so a service push at the head never blocks it. It hands the people layer no guests and only logs what the walk and the content checks would decide, a freeze included. `check` runs live for at least 3 days, a week if possible. Deliberately bad test commits go on a separate branch that the check reads and never applies.
- **Members first.** In `check`, no failure in the ledger path ever blocks the `people.yaml` apply. In `apply`, only an invalid commit stops it; a refused ledger or entry never does. The box never drops a guest already in the people layer's state, and never re-checks one.
- **Statuses:** on each `sso-people` head it checks, the box sets `sso/apply` (failing on a freeze) and `sso/ledger` (failing when it refuses the ledger). In `check`, both report what `apply` would have done.
- **Clock and state:** caps and expiry count from when the box first saw each link and redemption. First-seen times, refusals, the last good commit and the last good ledger live in `/var/lib/sso-config-apply`, with no backup. A rebuild restarts last good at the newest first-parent commit an operator key signed after the anchor commit, or at the anchor itself, and re-validates every entry not yet in state; it can let at most one extra day's cap through.
- **Guest scope:** `/guests` and `/apps/forgejo` only, fixed in `guests.tf` in `sso-people`. `/apps/forgejo` comes only after the storage cap or its fallback.
- **Roles:** the `invites` client in `realm/apps.tf`. Role `invites.user` goes to `/apps/invites`, which the people layer adds to every `people.yaml` entry and never to a guest. Role `invites.member` maps to `/realliance`. No `groups` scope.
- **The service account:** a master-realm service account `invites` with `view-users` and `query-users` on `realliance`, nothing else.
- **The service:**
  - Go, one replica, namespace `sso-invites`, Ingress `invite.realliance.net`, ko plus distroless, a transport guard, `slog` audit lines with ids only, metrics, no database.
  - **Writes:** git over SSH only, with a write deploy key on `sso-people`. It changes `ledger.yaml` alone, checks that before signing, signs with the ledger key and pushes straight to `main`. On a non-fast-forward rejection it fetches, rebuilds its change on the new head from a fresh read of `ledger.yaml`, signs again and retries, **at most 5 times with backoff**, then gives up and alerts. **It never force-pushes.**
  - Its only GitHub API access reads commit statuses on `sso-people`. Its clone carries `people.yaml`; its code reads `ledger.yaml` alone.
  - Tokens are 256-bit and single use. The MFA choice offers `webauthn` (preselected) or `totp` and nothing else. After 20 min with no account, the status page tells the invitee to contact the member who invited them.
- **Alerts:**
  - an `sso-people` head whose `sso/apply` or `sso/ledger` status still fails, or is missing, more than 1 h after the push;
  - a redemption with no Keycloak user after 20 min;
  - failing or exhausted ledger pushes;
  - each guest deactivation;
  - orphans (guests whose `inviter` is no longer active).
- **Erasure order:** deactivate, then delete the Keycloak user; `tofu state rm` under the box's lock; rehearse the rewrite on a throwaway copy of `sso-people`; `git filter-repo` touching `ledger.yaml` blobs alone; an operator-key head whose `Reviewed-Range` names the box's last good and the rewritten tip, then the force-push; re-clone the box's checkout and both operators' clones, and rebase open PRs.
- **Storage cap:** a Forgejo quota for guests across git, LFS and packages, about 2 GiB in all, with the spec's three-step ladder.
- **Guest CI:** the `defaults` of any unlisted owner, with no guest tier and no `ci-dispatch` change *(Chris decided)*.
- **Mail:** the free tier of Resend, 100 mails a day, shared with resets and the forge.
- **Galaxy:** Argo GitOps, SealedSecrets, one Ingress with a host, `release: prometheus` on every ServiceMonitor, alerts as a `PrometheusRule`, images pinned by digest. **No secrets in `realliance/sso`**, not even encrypted.
- **Personal data:** no email, name or token in a log line, a metric label, a commit status, public `realliance/sso` or the Azure log table, Traefik's access log included.

### Execution rules (Chris, for this plan)

- Run it subagent-driven: one implementer and one reviewer per task. Task 2b's review is adversarial, on the most capable model.
- **Four threads** for local builds and tests:
  - Go: `GO="taskset -c 0-3 nix develop $HOME/Repos/luma-homeops --command env GOMAXPROCS=4 go"`, then `$GO test -race -p=4 ./...`;
  - pytest: `taskset -c 0-3 nix develop ~/Repos/realliance-sso-invites --command pytest …`;
  - nix builds: `--cores 4`.
  - No local Docker or kind: CI builds images.
- Never `--no-verify`, never `--no-gpg-sign`, **never force-push**. Any force-push a procedure needs (a drill, an erasure) is Chris's step.
- Never open `~/Repos/realliance-sso-secrets`, any `*.secret.*`, `nodes/*` or a key file, and exclude them from every grep (`grep -r --exclude='*.secret.*' --exclude-dir=nodes …`). Programs that consume them (pytest's `conftest`, `./sign.sh`, `kubeseal`, `ansible` on the box) may run; a `python -I` script writes a secret file and prints a byte count.
- No secret or personal data in argv, logs, test output or the progress ledger. Never print `ledger.yaml` or `people.yaml`, and never diff them in a transcript: a `python -I` script prints counts and link ids instead. In git-crypt repos use `git diff --no-textconv`.
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
| `realliance/sso` (GitHub, public) | worktree `~/Repos/realliance-sso-invites`. Tasks 2, 2b, 3 and 4 share branch `invites-ledger-core` and land as one PR in Task 4; each later task names its own branch | Signed commits, `gh pr create --fill`. **Chris** merges with `gh pr merge --squash --delete-branch`. The rule set requires a PR, so every head on `main` is web-flow-signed on Chris's merge, and **the box keeps trusting web-flow on `realliance/sso`** (the spec's accepted risk). That is what this plan calls an *operator-signed `realliance/sso` head*. A merge that changes `host/` or `realm/` is a **box-head STOP**; a tests-only merge is **Chris:** merges. |
| `realliance/sso-people` (GitHub, private) | worktree `~/Repos/realliance-sso-people-invites`, branch per task | Code and `people.yaml` changes go by PR. From Task 2b on, **Chris merges locally**, never with a GitHub button: `git switch main && git pull --ff-only && git merge --squash origin/<branch> && git commit -S && git push origin main`, or a fast-forward of a branch whose commits he signed. Never use "Update branch," never a PR base other than `main`. `ledger.yaml`-only commits signed with Chris's key go straight to `main` from a temp clone (Task 6's helper), only inside a STOP that approved them. **A `Reviewed-Range` commit is always pushed directly.** Bad test commits go only on branch `ledger-drills`. Never open or print `people.yaml` or `ledger.yaml`. |
| `realliance-ops/sso-invites` (forge, public) | clone `~/Repos/sso-invites`, branch `v1` | Signed commits; **Chris** merges fast-forward (`fj -H git.realliance.net pr merge realliance-ops/sso-invites#<n> -d`) and signs the tags. |
| `realliance-ops/forge` (forge, private) | `~/Repos/realliance-forge`, branch per task | A PR; **Chris** merges fast-forward. An apply or script run follows its STOP. |

- **STOP—ask Chris** is one AskUserQuestion. It states the change, the Gate G evidence where a task touches galaxy, what to watch and the rollback trigger. The options are `approve: do it, and roll back at once if a trigger fires` and `hold`. A rejected or interrupted call may already have run, so read live state before redoing anything.
- **Gate G** is phase 0–1's (`2026-11-04-runner-classes-phase-0-1.md`, G1–G4), before and after every galaxy deploy push.
- **Serialize STOPs** with every other plan in flight: no deploy here inside another plan's Gate G window.
- **Progress ledger:** `.superpowers/sdd/2026-11-08-sso-member-invites/progress.md` (git-ignored, the SDD ledger, by the controller's Task 0 ruling): answers, verdicts, timings, digests and rulings. It holds no secret, no email, no name and no hidden org, team or group name; link ids and usernames of throwaway guests only.
- `SCRATCH=~/Repos/luma-homeops/.superpowers/sdd/2026-11-08-sso-member-invites/scratch` (git-ignored). Define it in every shell that uses it.
- Waits over 60 s use `Monitor`, never a sleep loop. CLAUDE.md S1–S9 apply.

<!-- vale Google.Headings = NO -->
## Review Focus
<!-- vale Google.Headings = YES -->

1. **A merge or a recovery that looks right but freezes.** An operator presses GitHub's merge, squash or rebase button or "Update branch" on an `sso-people` PR (each makes web-flow commits), writes `Reviewed-Range` in a web-UI commit, uses short shas, names a last good the box has moved past, or lets the range miss an invalid commit or a seen head. Members stay frozen. Expected: the box freezes or rejects each, and the `sso/apply` description names the reason (`invalid:untrusted-signer@<sha7>`, `not-first-parent-descendant@<sha7>`, `recovery-not-operator-key@<sha7>`, `recovery-bad-trailer@<sha7>`, `recovery-stale-last-good@<sha7>`, `recovery-misses@<sha7>`) and prints the seen-heads list. Tests: Task 2b `test_merge_button_merge_freezes`, `test_squash_button_freezes`, `test_rebase_button_freezes`; Task 2 `test_recovery_must_be_operator_key`, `test_recovery_needs_full_shas`, `test_recovery_replayed_fails`, `test_recovery_must_cover_every_invalid_commit`; Task 3 `test_status_prints_seen_list`; Task 6 drills.
2. **A guest record Keycloak rejects, holding members hostage.** A name with a character Keycloak's `person-name-prohibited-characters` refuses (`<`, `$`, `;`, `{`), an over-long name, or an email its email check refuses makes `tofu apply` fail for the whole people layer. Expected: the box refuses the entry before handing it over. If Keycloak still rejects it, the apply runs again without the new guests, `people.yaml` applies, and the entry is refused `apply-failed`. Tests: Task 3 `test_entry_refused_for_keycloak_prohibited_name`; Task 4 `test_new_guest_apply_failure_retries_without_it`.
3. **YAML that means something other than it looks.** Duplicate keys (PyYAML keeps the last one), anchors and aliases, unquoted timestamps or `yes`/`no` turned into other types, a 19-digit number, a 50 MB file, a missing `ledger.yaml`. Expected: the box refuses the ledger with a schema reason (a missing file counts as an empty ledger), `people.yaml` keeps applying, and the service never writes such a file. Tests: Task 2 `test_duplicate_key_refused`, `test_alias_refused`, `test_unquoted_timestamp_refused`, `test_oversize_refused` and the 19-digit vector; Task 3 `test_missing_ledger_is_empty`; Task 8 `TestMarshalRoundTripsThroughBoxVectors`.
4. **Case, whitespace and doubles.** `Bob@Example.COM ` against an existing `bob@example.com`, or two links redeemed with the same username in one push. Expected: emails compare trimmed and in lower case everywhere, and the link the box saw first wins while the later one is refused. Tests: Task 3 `test_email_collision_case_insensitive`, `test_same_username_twice_first_wins`; Task 11 `TestRedeemNormalisesEmail`.
5. **The token leaks at the edge.** Traefik's access log is on with default fields, so a token in a path or query lands in the logs, and it would also reach browser history and `Referer` headers. Expected: the token rides only in the URL fragment and the POST body, every page sends `Referrer-Policy: no-referrer`, and no log line holds a token. Tests: Task 12 `TestTokenNeverInPathOrQuery`, `TestReferrerPolicyNoReferrer`, `TestNoTokenInLogs`.

---

## Chris's answers and rulings (2026-11-08)

Recorded in Task 0 and the SDD ledger; no question remains open.

- The ledger location question has its answer: `ledger.yaml` in `sso-people`.
- Defaults accepted: operators take a deactivated guest's Pages sites down, links last 7 days, and a promoted guest gets the new tier's quota.
- The operators' SSO usernames are `chris` and `connor`.
- Add the `invites-test` member (Task 19).
- **No web-flow on `sso-people`** (`795a3b86`): operator keys and the path-limited ledger key only; operators merge locally; a changeover anchor seeds last good.
- The controller's standing rulings: no ledger-content failure ever blocks a valid `people.yaml` apply; guest CI is user defaults; the storage-quota ladder stays.

## Decisions this plan makes

*(Plan's call, open to Chris's override at the first STOP that touches each.)*

- **D1. The box check is one Python file.**
  - Source: `host/roles/config_apply/files/sso_ledger.py`, installed as `/usr/local/sbin/sso-ledger` with shebang `#!/opt/ansible/bin/python3 -I`.
  - Runtime: the pull role's Python 3.12 virtual environment, which already carries PyYAML for ansible-core, so the box needs no new package. The code stays Python 3.12 compatible.
  - Tests: they import it from the file and run offline, with real git and GnuPG; `tests/ledger/__init__.py` keeps its `conftest.py` from shadowing `tests/conftest.py`.
- **D2. Two commands, two failure rules.** `sso-ledger walk` reads only git objects and signatures and never parses YAML. `sso-ledger content` parses the ledger and decides the guests.
  - In `apply`, a `walk` crash or timeout fails closed: no `sso-people` apply this run, `sso/apply` failure `walk failed`, and the alert. An unverified chain is never applied, or a crafted ledger that crashed the parser could carry a `people.yaml` change past the path check. A walk that raises writes no state: the old last good and the old seen-heads list stay.
  - A `content` crash or timeout never blocks: the people layer applies at the walked head with the last handed guests, and `sso/ledger` reports failure `content failed`.
  - In `check`, neither blocks: the people apply runs from the newest first-parent commit that's last good or an operator key signed. If `walk` died, bash finds that commit itself with `gnupg-people` and the operator fingerprints in `people-trust.json`, never with the pull keyring, which trusts web-flow.
- **D3. A separate keyring for `sso-people`, without web-flow.** `/etc/realliance-sso/gnupg-people` holds the operators' and the ledger's public keys only, and `people-trust.json` reads `{"operators": [...], "ledger": [...]}`, primary fingerprints, uppercase. Web-flow's key isn't in it, so a web-flow signature reads as untrusted. The pull keyring `/etc/realliance-sso/gnupg` doesn't change: it keeps web-flow for `realliance/sso`, and it never holds the ledger key.
- **D4. The path rule, exactly.** A ledger-key commit is valid only if it has one parent and `git diff-tree --raw --no-renames -r <parent> <sha>` prints exactly one line, `:100644 100644 <a> <b> M` for `ledger.yaml`. Codes:
  - `ledger-merge`: not exactly one parent;
  - `ledger-path`: any other path, or more than one line, so a rename (a delete plus an add) also lands here;
  - `ledger-mode`: any mode other than 100644 on either side, a symlink or submodule included (the absent side of an add or delete is ignored);
  - `ledger-status`: anything but `M`, so creating or deleting `ledger.yaml`, or an empty commit.
  - The rule holds for a ledger-key commit on **any** parent, not just the first-parent line.
- **D5. The walk and recovery** (as built in Task 2, then Task 2b).
  - **The range.** Every commit in head's history that last good's history lacks, from one complete `rev-list --topo-order --parents` walk: never `git rev-list A ^B`, which stops early under back-dated commits. After Task 2b every commit in the range, on any parent, is checked by its own signer; there's no merge exemption.
  - **Fast-forward.** The head fast-forwards if last good is in its history.
  - **Seen heads.** The box keeps every head it has seen since last good, in order. Each seen head that isn't in last good's history must sit on head's first-parent line, or the walk freezes with `not-first-parent-descendant@<the first offender>`. The freeze is sticky: the list grows by each frozen head and clears only when last good moves. A seen head gone from history freezes the same way.
  - **Recovery.** A recovery commit `R` is the newest chain commit an operator key signed whose `%(trailers:key=Reviewed-Range,valueonly)` is `<L>..<T>` with two 40-hex shas. `L` must equal last good and `T` must be an ancestor of `R`; the reviewed set is `T`'s history less `L`'s. Under a taken recovery, every commit outside the reviewed set is checked by its own signer, and every seen head still in head's history must be on the line or reviewed (`recovery-misses@<it>`).
  - **Reasons, in order:** the newest `Reviewed-Range` attempt's problem, then `not-fast-forward`, then the oldest invalid commit; `recovery-misses@<sha7>` names the missed commit. Also `shallow-repository` (the walk refuses a shallow checkout and records the head) and `not-first-parent-descendant@<sha7>`.
  - **Git hygiene.** `_git` drops every inherited `GIT_*` and sets `LC_ALL=C`, `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_NO_REPLACE_OBJECTS=1`, `GIT_GRAFT_FILE=/dev/null` and `GIT_TERMINAL_PROMPT=0`; log calls pass `--no-show-signature`; each call is capped at 30 s. The box's checkout sets `fetch.fsckObjects=true` and stays a full clone.
  - `last_good` must exist in the local checkout, so after an erasure the box's checkout is re-cloned only once it has taken the new history.
- **D6. The changeover anchor** *(spec: Changeover)*. `host/group_vars/sso.yml` gains `people_anchor_commit`: the `sso-people` head the box last applied with the old code, recorded by Chris in Task 4's box head. With no state (the first run, or a rebuild), `walk` sets last good to the newest first-parent commit after the anchor that an operator key signed, else the anchor itself, and the seen-heads list to `[]`. `ledger-drills` anchors at main's last good the first time the box sees it.
- **D7. Handing guests to the people layer.**
  - The box writes `/var/lib/sso-config-apply/ledger/guests.next.json` as `{"guests": {<username>: {active, mfa, bootstrap}}}` and runs `tofu apply -var-file=…`.
  - `guests.tf` declares `variable "guests"` (default `{}`) and `output "guests"` (sensitive).
  - **Admitted is append-only.** `state.json` `admitted` maps username to `{link, at, active}`, with no email, and nothing removes an entry but `sso-ledger forget`. The box hands every admitted guest on every run. Its `bootstrap` comes from the effective ledger, or `null` when an erasure removed the entry: `ignore_changes` covers the profile, so it no longer matters.
  - **Rebuild:** a rebuilt box seeds `admitted` from `tofu output -json guests`.
  - **Retry:** if the apply fails while new guests are in the set, it runs again with the previous set. If that run succeeds, the box marks the new ones refused (`apply-failed`).
- **D8. A new guest with `active: false` is never created.** This keeps the erasure order safe, and it lets Task 15 neutralise the check-window test entries before the switch.
- **D9. Erasure details.** The spec's order stands; this plan adds three things.
  - The Keycloak delete, the `tofu state rm` of both addresses and `sso-ledger forget <username>` run in one SSH session that holds the box's lock. Otherwise an apply between them would recreate the user.
  - Chris force-pushes with `--force-with-lease=main:<old-tip>`, after `sso/apply` shows success on `<old-tip>`, and names `<old-tip>` as `<last-good>`. A service push before his makes the lease fail, and he redoes the rewrite.
  - The callback is `scripts/erase_ledger_guest.py` in `realliance/sso`, a `--file-info-callback` that drops one link id from `ledger.yaml` blobs and returns every other blob unchanged. The anchor predates every guest, so an erasure never rewrites it.
- **D10. Check mode in detail.**
  - The people apply runs from the newest first-parent commit that's last good or an operator key signed. `walk` reports it as `newest_trusted`.
  - Last good and the seen-heads list advance in `check` exactly as `apply` would advance them, so the switch starts from a known anchor.
  - The box evaluates branch `ledger-drills` with `--dry` on every run in both modes, with its own state (last good, seen-heads list, ledger), and posts its statuses. It never applies anything from it.
- **D11. Operator-signed `realliance/sso` heads** are Chris's PR merges (How changes land). `ledger_mode` and `people_anchor_commit` live in `host/group_vars/sso.yml`, so only such a merge changes them.
- **D12. The link is `https://invite.realliance.net/i#<token>`.** The token is 32 random bytes as base64url without padding (43 characters) in the fragment, which never reaches Traefik. Every page sends `Referrer-Policy: no-referrer`.
- **D13. Service writes.**
  - The service clones `ssh://git@ssh.github.com:443/realliance/sso-people.git` into memory (go-git memory storage, no volume) against a pinned host key, so its egress is TCP 443 only.
  - It builds each commit as the parent's tree with only the `ledger.yaml` blob replaced, and checks that before signing.
  - Retries wait 1, 2, 4 and 8 s, each with up to 20 % jitter. The fifth refusal is `ErrExhausted`.
  - `Head` reads the `ledger.yaml` blob and no other blob.
- **D14. The service deploys after the apply switch**, first with `open: false`: members can sign in, and the watcher and alerts run, but nobody can make a link. It opens after `/apps/forgejo` (Task 19).
- **D15. Storage cap path 1:**
  - Forgejo 15's `[quota] DEFAULT_GROUPS = guests` caps every account and org that has no explicit quota mapping. A `guests` group holds the 2 GiB rule.
  - An idempotent `quota.sh` in `realliance-ops/forge` maps every org and every non-guest account to an unlimited `members` group. It runs again each time a member joins, at each promotion and for each new org. Until then the cap applies, the fail-closed direction.
  - **The facts:** `models/quota/group.go` `GetGroupsForUser` falls back to `setting.Quota.DefaultGroups` only when a user has no mapping, and orgs are users there.
  - A repo-storage jump alert ships on every path. Chris rules the path at Task 17's STOP.
- **D16. Latency.** `apply_interval` drops from 5min to 1min. `changed` mode skips the realm apply when `realliance/sso` hasn't changed.
- **D17. The service's operators** are `chris` and `connor`, listed in its config. They may revoke any link and deactivate any guest, and nothing more.
- **D18. Orphans come from Keycloak.** An active guest counts as an orphan when the inviting member's account is off (`enabled: false`) or missing there. A promoted guest whose inviting member left also counts; the runbook says to ignore it.
- **D19. Box queries use `tofu-plan`** (view-only), its secret rendered into `/etc/realliance-sso/ledger-lookup.secret`.
- **D20. Formats.**
  - Usernames: `^[a-z][a-z0-9-]{1,30}[a-z0-9]$` with no `--`.
  - Reserved names: a built-in list plus private `reserved.yaml` in `sso-people`, which the service's code never reads. The service config carries a public subset.
  - Emails: ASCII, at most 254 characters, compared trimmed and in lower case.
  - Names: 1–64 characters, none of Keycloak's prohibited characters, no control characters.
  - Ledger: at most 1 MiB, no aliases, no duplicate keys, no explicit tags, timestamps as quoted `YYYY-MM-DDTHH:MM:SSZ`, integers of at most 18 digits, no `null` values (an absent field is left out).
  - Link ids: `l-` plus 16 lowercase hex characters.
  - Expiry grace: 30 min.
- **D21. Mode scoping of box state.** `seen`, `last_good`, `seen_heads` and `last_good_ledger` persist in both modes; `admitted` and `refused` persist only in `apply`. A refusal clears when the entry's canonical hash changes.
- **D22. Quota order.** At redemption, the box admits a link if fewer of the inviting member's links hold a slot than the tier allows. Only links the box saw before this one count, so earlier links win.

## File map

| Path | Responsibility | Task |
|---|---|---|
| sso: `host/roles/config_apply/files/sso_ledger.py`, `tests/ledger/{__init__.py,conftest.py,test_schema.py,test_walk.py,vectors/}` | parse, schema, signers, path rule, walk, seen heads, recovery | 2 (done) |
| same files; sso-people `README.md` | no web-flow on `sso-people`; the merge rule | 2b |
| same file, `tests/ledger/{test_content.py,test_admission.py,test_cli.py}` | ledger verdict, admission, quotas, clock, state, command line | 3 |
| sso: `host/roles/config_apply/{files/sso-config-apply.sh,tasks/main.yml,templates/ledger.env.j2,templates/people-trust.json.j2}`, `host/group_vars/sso.yml`, `operators.yml`, `keys/ledger/sso-invites-ledger.asc`, `realm/groups.tf`, `tests/ledger/test_apply_script.py`, `tests/host/test_config_apply.py` | the box runs the check; the changeover anchor | 4 |
| sso-people: `guests.tf`, `main.tf`, `ledger.yaml`, `reserved.yaml`, `README.md`; sso: `tests/people/test_people.py` | the guest layer and the empty ledger | 5, 7, 18 |
| sso: `tests/invites_helpers.py`, `tests/invites/test_ledger_drills.py` | check-window drills | 6 |
| sso: `realm/modules/app/*`, `realm/apps.tf`, `realm/outputs.tf`, `realm/automation.tf`, `host/roles/master_realm/tasks/main.yml`, `tests/conftest.py`, `tests/realm/test_apps.py`, `tests/host/test_master_realm.py` | the `invites` client and service account | 7 |
| forge inventory `repos.tf`; service `go.mod`, `.ko.yaml`, `.forgejo/workflows/ci.yaml`, `deps_test.go`, `internal/{secret,ledger}/` | repo and ledger package | 8 |
| service `internal/gitstore/` | fetch, ledger-only signed commit, push with retry | 9 |
| service `internal/{keycloak,github}/` | guarded readers | 10 |
| service `internal/invites/`, `internal/audit/` | the rules | 11 |
| service `internal/web/`, `internal/limit/` | sign-in, pages, limits | 12 |
| service `internal/{watch,metrics,config}/`, `cmd/sso-invites/` | watcher, metrics, main, release | 13 |
| luma-homeops `cluster/sso-invites/*`, `cluster/applications/sso-invites.yaml`, `scripts/test_sso_invites_manifests.py`, `scripts/test-sso-invites-alerts.sh`, `.pre-commit-config.yaml`, `.renovaterc.json` | deploy | 14, 16, 19 |
| luma-homeops `cluster/applications/forgejo-release.yaml`, `cluster/forgejo/{prometheusrule.yaml,alerts_test.yaml,kustomization.yaml}`; forge `quota.sh`; sso `tests/forge/test_forge_quota.py` | storage cap | 17 |
| sso: `tests/invites/{test_invites.py,test_invites_box.py}` | acceptance | 19 |
| sso: `scripts/erase_ledger_guest.py`, `tests/ledger/test_erase.py`, `flake.nix` | the erasure tool and its rehearsal | 20 |
| luma-homeops `docs/sso-invites.md`, `docs/forgejo.md`, `CLAUDE.md`; sso `docs/runbook.md`, `README.md`, `WISHLIST.md`; forge and sso-people README files | runbooks | 21 |

## Coordination with concurrent plans

- **forge-bot** (`2026-11-02-forge-bot-and-ci.md`): until its `access` job runs in `apply`, guest offboarding prohibits login by hand (`docs/forgejo.md`). This plan writes nothing in forge-bot.
- **Docs site and Actions plans** (worktrees `luma-homeops-plan-docs`, `luma-homeops-plan-actions`): serialize STOPs; rebase right before any commit that touches `.pre-commit-config.yaml`, `.renovaterc.json` or `CLAUDE.md`.
- **Runner classes:** this plan only adds `deny:` entries for throwaway guests in Task 19 and removes them again.
- **Anyone merging `sso-people` PRs:** from Task 2b on, merge locally and signed (How changes land). Once Task 4 ships, a merge-button commit freezes member applies until a `Reviewed-Range`.

---

### Task 0: Setup and answers

**Status:** done (SDD ledger).

**Files:** the SDD ledger `.superpowers/sdd/2026-11-08-sso-member-invites/progress.md` (git-ignored).

**Interfaces:** produces the progress ledger, with Chris's 2026-11-08 answers.

- [x] **Step 1: Worktrees.** luma-homeops as How changes land says. Then `git -C ~/Repos/realliance-sso worktree add ~/Repos/realliance-sso-invites -b invites-ledger-core origin/main` and `git -C ~/Repos/realliance-sso-people worktree add ~/Repos/realliance-sso-people-invites -b guests origin/main`. Expected: clean trees.
- [x] **Step 2: Record the answers** listed under "Chris's answers and rulings".
- [x] **Step 3: Cross-plan reads.** forge-bot isn't deployed (its `access` job has no mode yet); NetworkPolicy B4 not before 2026-11-13; Task 8's forge apply must not overlap the docs-site plan's inventory apply.

### Task 1: Keys and tokens

**Status:** done (SDD ledger): `LEDGER_FPR` recorded, the write deploy key on `sso-people`, both status tokens narrowed to "Only select repositories," host secrets placed on the box.

**Files:** none in git; sso-secrets and GitHub settings only.

**Interfaces:**
- Produces:
  - the ledger key's fingerprint (`LEDGER_FPR`, in the SDD ledger);
  - `keys/ledger/sso-invites-ledger.asc` (public, not yet tracked in the sso worktree; Task 4 commits it);
  - sso-secrets paths for the service: `invites/ledger-signing.asc`, `invites/people-deploy-key` (and `.pub`), and `invites/secrets.env` with `INVITES_STATUS_TOKEN` and `INVITES_SESSION_KEY`;
  - sso-secrets `host/secrets.yml` keys `invites_client_secret` and `github_status_token` (*Commit statuses* read and write on `realliance/sso` and `realliance/sso-people` only).

- [x] **Step 1: STOP—ask Chris** (Chris's steps): the ledger key; the service's write deploy key on `sso-people`; the service's status-read token; `invites_client_secret` and `INVITES_SESSION_KEY`; sso-secrets committed and the host secrets placed.
- [x] **Step 2: Check the public key.** One `pub` line, EdDSA, capabilities `scSC` (sign and certify, no encryption), expiring 2028-11-06.
- [x] **Step 3: Check the keys and the box's own token, by count only.** The box had no `GITHUB_STATUS_TOKEN`; Chris created one (STOP, done).

### Task 2: The box check: parse, signers, path rule, walk and recovery

**Status:** built at `realliance-sso` `invites-ledger-core` `6c2dc13` (`25a5db8`, then fix rounds 1–5: `22ccaac`…`31ac4d6`, `a37e3e9`, `489a90c`, `9fadc09`, `6c2dc13`), signed and not yet pushed; one review, five fix rounds and their re-reviews. Task 2b replaces its web-flow class and its GitHub-merge exemption before anything ships; the tests below are the ones that stay true after it.

**Files (as built):** `host/roles/config_apply/files/sso_ledger.py`, `tests/ledger/{__init__.py,conftest.py,test_schema.py,test_walk.py}`, `tests/ledger/vectors/index.yaml` plus one `.yaml` per vector.

**Interfaces (as built; Task 2b changes the marked ones):**
- `MAX_LEDGER_BYTES = 1_048_576`; frozen `dataclass` types `Bootstrap`, `Guest`, `Link` and `Ledger`.
- `parse_ledger(data: bytes) -> Ledger`, raising `SchemaError` with `.code` ∈ {`too-large`, `yaml`, `duplicate-key`, `alias`, `unknown-key`, `missing-key`, `type`, `value`}. The loader drops every default resolver: a plain scalar resolves only to `true`/`false`, `null`, a decimal integer of at most 18 digits, or a string. Any other form a YAML 1.1 or 1.2 reader takes as non-string gives `type`; an explicit tag gives `yaml`; a key present with `null` gives `type`. `expires` must follow `created`, a `created` within 7 days of year 9999 gives `value`, `version` must be `1`, and a link id used twice gives `value`.
- `check_transition(before: Ledger, after: Ledger) -> None`, raising `TransitionError` with `.code` ∈ {`immutable`, `removal`, `reactivate`, `not-allowed`, `version`}: links matched by id (reordering allowed); a changed `revoked` time is `immutable`; an added link that isn't pending is `not-allowed`; a redeemed guest's `username`, `bootstrap` and `mfa` are immutable.
- `Trust` and `load_trust(path: Path) -> Trust` (**Task 2b:** two classes); `commit_signer(repo, sha, keyring, trust)` classifies by the primary fingerprint `%GP` when `%G?` is `G` (**Task 2b:** no web-flow class).
- `check_commit(repo, sha, signer) -> str | None`: D4's codes, `unsigned` or `untrusted-signer`.
- `parse_reviewed_range(trailer_values: str) -> tuple[str, str] | None`: exactly what `git log -1 --format=%(trailers:key=Reviewed-Range,valueonly)` prints; a whole commit message returns `None`.
- `WalkVerdict(head, last_good, valid, reason, chain, invalid, recovery, newest_trusted, seen_heads: list[str])`; `invalid` lists only commits that freeze; `recovery` is set only on a valid verdict; `last_good` is the head when valid, the input otherwise.
- `walk(repo, head, last_good, keyring, trust, seen_heads: list[str] | None = None) -> WalkVerdict` (D5). `seen_heads` of `None` or `[]` asks nothing; a bare string, a short commit id or anything else that isn't 40 hex raises `ValueError`. The verdict's `seen_heads` is `[]` when valid, else the old list plus this head (the shallow refusal included). **Task 2b** removes its web-flow merge exemption.
- `newest_trusted(...)` (**Task 2b:** last good or operator only).
- `_git(...)` with D5's hygiene, and `GitError`.
- **Contracts handed on:** Task 8's writer quotes every string a YAML 1.1 or 1.2 reader would take as another type and omits absent optional fields (never `null`); `vectors/index.yaml` uses `pad_to` (pad with `#`, spaces and a newline to N bytes) so a 1 MiB file stays out of git. Task 4's box git must support `%GP` and `%(trailers:…,valueonly)`. Task 6's drill recoveries never name main's last good.

- [x] **Step 1: Schema tests, RED then GREEN:** `test_minimal_ledger_parses`, `test_unknown_key_refused`, `test_duplicate_key_refused`, `test_alias_refused`, `test_unquoted_timestamp_refused`, `test_oversize_refused`, `test_mfa_required_and_enum`, `test_expires_at_most_seven_days`, `test_formats`, `test_ledger_key_transitions`, and `test_vectors` (the 19-digit integer and the year-9999 `created` included), plus a fuzz of about 3,000 vectors that raises only `SchemaError`.
- [x] **Step 2: Walk tests, RED then GREEN** (the post-2b expectations):
  - signers: `test_signer_classes` (operator, ledger; untrusted, unsigned, expired), `test_git_ignores_the_callers_git_environment`, `test_grafts_cannot_hide_commits`, `test_shallow_repository_refused`;
  - path rule: `test_ledger_key_path_rule` (`ledger-path`, `ledger-mode`, `ledger-status`, `ledger-merge`), `test_operator_may_change_any_path`, `test_unsigned_or_unknown_key_invalid`, the ledger-key path rule on a second parent;
  - chain: `test_valid_chain` (operator and ledger commits), `test_freeze_on_invalid_commit`, `test_freeze_on_non_fast_forward`, `test_freeze_reason_names_the_first_invalid_commit`, the rollback, date-skew and duplicate-entry-tree cases;
  - recovery: `test_recovery_accepted`, `test_recovery_replayed_fails`, `test_recovery_must_be_operator_key`, `test_recovery_needs_full_shas`, `test_recovery_tip_must_be_ancestor`, `test_recovery_must_cover_every_invalid_commit`, `test_commits_after_recovery_checked_as_usual`, `test_recovery_takes_non_fast_forward_history`, `test_trailer_from_another_key_never_masks_the_freeze`;
  - seen heads: `test_seen_heads_clear_when_last_good_moves`, `test_a_frozen_head_on_the_line_is_recorded_too`, `test_a_force_pushed_head_is_recorded_and_a_narrow_recovery_misses_it`, `test_recovery_outside_a_seen_heads_history_cannot_carry_it_in`, `test_a_seen_head_not_in_the_checkout`, `test_erasure_with_seen_heads`.
- [x] **Step 3: Run under the box runtime.** Python 3.12.14 with PyYAML 6.0.3: 213 pass offline, and no test is marked as an expected failure.

### Task 2b: Drop web-flow trust on `sso-people`

**Review:** its own, adversarial, on the most capable model (opus), not a sixth fix round of Task 2. The reviewer tries to land a `people.yaml` change past the walk with the service's deploy key and every GitHub web feature.

**Files:**
- Modify (sso, branch `invites-ledger-core`): `host/roles/config_apply/files/sso_ledger.py`, `tests/ledger/conftest.py`, `tests/ledger/test_walk.py`.
- Modify (sso-people, branch `merge-locally`): `README.md`.

**Interfaces:**
- Consumes Task 2's code at `6c2dc13`.
- Produces:
  - `Trust(operators: frozenset[str], ledger: frozenset[str])`; `load_trust` reads `{"operators": [...], "ledger": [...]}` and raises `ValueError` on any other key (`web_flow` included), a value that isn't 40 hex, or a fingerprint in both;
  - `commit_signer(...) -> Literal["operator", "ledger", "untrusted"]`: a web-flow signature is `untrusted`, since its key is absent from `gnupg-people`;
  - `check_commit`: an operator key may change any path; the ledger key keeps D4; `untrusted-signer` and `unsigned` stay invalid;
  - `walk`: no merge exemption: every commit in the range, on any parent, is checked by its own signer. The seen-heads rule and the strict pass under a recovery stay as defence in depth;
  - `newest_trusted(repo, head, last_good, keyring, trust) -> str | None`: the newest first-parent commit that is `last_good` or that an operator key signed;
  - `reanchor(repo, head, anchor, keyring, trust) -> str`: the newest first-parent commit on head's line after `anchor` that an operator key signed, else `anchor` (D6).
- The README rule (sso-people): merge PRs locally, signed with your own key, as a squash or a fast-forward of commits you signed, and push to `main`; never GitHub's merge, squash or rebase button; never "Update branch"; never a PR base other than `main`; pull with `--ff-only` or `--rebase`. Any web-flow commit freezes the box until a `Reviewed-Range`.

- [ ] **Step 1: Write the failing tests** in `tests/ledger/test_walk.py`, with the stand-in web-flow key from `conftest.py` kept as an untrusted signer:
  - `test_web_flow_commit_freezes`: a web-flow commit changing `README.md`, and one changing `people.yaml`, each give `invalid:untrusted-signer@<sha7>`;
  - `test_web_flow_editor_commit_from_another_repo_freezes`: the Critical finding's shape, a web-flow-signed commit on last good that changes `people.yaml`, pushed as the deploy key would: frozen, never applied;
  - `test_merge_button_merge_freezes`: a web-flow merge of operator-signed PR commits freezes, naming the merge;
  - `test_squash_button_freezes` and `test_rebase_button_freezes` (web-flow re-signed commits);
  - `test_update_branch_shape_freezes`: the signature on the merge freezes it, and the seen-heads rule still names the seen head when the merge is operator-signed;
  - `test_web_flow_recovery_rejected`: `recovery-not-operator-key@<sha7>`;
  - `test_operator_local_squash_is_valid` and `test_operator_fast_forward_of_signed_commits_is_valid`;
  - `test_operator_local_merge_of_unsigned_work_freezes`: no exemption under any merge;
  - `test_trust_file_refuses_web_flow_class`;
  - `test_newest_trusted_stops_at_last_good_or_operator`: the function skips web-flow and ledger commits nearer the head, and last good counts even when web-flow signed it (the anchor);
  - `test_reanchor_rules`: an operator commit after the anchor wins; none gives the anchor; an anchor off head's line gives the anchor, and the walk then freezes `not-fast-forward`;
  - `test_changeover_history_never_walked`: web-flow history before the anchor, last good at the anchor, operator and ledger commits after: valid;
  - every Task 2 test that used web-flow as a trusted signer is rewritten with an operator signer and keeps its assertion.
- [ ] **Step 2: Run; expect FAIL:** `taskset -c 0-3 nix develop ~/Repos/realliance-sso-invites --command pytest tests/ledger -q`.
- [ ] **Step 3: Implement.** Drop `web-flow` from `Signer`, `Trust` and `_TRUSTED`; delete the web-flow merge exemption so the strict per-commit check always applies; add `last_good` to `newest_trusted` and add `reanchor`. Update `walk`'s docstring to say what holds now.
- [ ] **Step 4: Run; expect PASS,** offline and under Python 3.12.14 with PyYAML 6.0.3. A mutation check: putting `web-flow` back into the trusted set makes at least the four button and editor tests fail.
- [ ] **Step 5: Commit signed** on `invites-ledger-core`: `sso-ledger: no web-flow on sso-people; every commit carries its own signature; changeover anchor`.
- [ ] **Step 6: The README rule.** Write it in sso-people's `README.md` on branch `merge-locally`, push, and open the PR. **Chris:** merges it locally as a signed squash, the first local merge (How changes land). The box still runs the old code, which accepts his signature.
- [ ] **Step 7: Review.** Dispatch the adversarial opus-level review with the spec's Amendment 2, this task and the diff `6c2dc13..HEAD`. Fix rounds follow the SDD rules; ledger the verdict.

### Task 3: The box check: ledger verdict, admission, quotas, clock and state

**Files:**
- Modify: `host/roles/config_apply/files/sso_ledger.py`.
- Create: `tests/ledger/test_content.py`, `tests/ledger/test_admission.py`, `tests/ledger/test_cli.py`.

**Interfaces:**
- Consumes Task 2's `parse_ledger`, `check_transition` and `WalkVerdict`, and Task 2b's `walk`, `newest_trusted` and `reanchor`.
- Produces:
  - `LedgerVerdict(effective: str, refused: bool, reason: str | None)` from `check_ledger(repo: Path, verdict: WalkVerdict, last_good_ledger: str | None) -> LedgerVerdict`. It parses `ledger.yaml` at the head, and checks each ledger-key commit in the range after `last_good_ledger` with `check_transition` against its parent. On any failure `effective` stays at `last_good_ledger` and `reason` reads `schema:<code>@<sha7>` or `transition:<code>@<sha7>`. A missing `ledger.yaml` is an empty ledger;
  - `People` with `tiers: dict[str, str]` (username to `realliance` | `member` | `guest` | `inactive`), `usernames: set[str]` and `emails: set[str]` (in lower case), from `load_people(repo: Path, sha: str) -> People`, which reads `people.yaml` at that commit;
  - `load_reserved(repo: Path, sha: str) -> tuple[frozenset[str], tuple[str, ...]]` (names and prefixes; built-ins plus `reserved.yaml` if present);
  - `class Directory(Protocol)` with `username_taken(u: str) -> bool` and `email_taken(e: str) -> bool`, which raise `LookupFailed`; `KeycloakDirectory(base_url: str, secret_file: Path)` uses client `tofu-plan` with 10 s timeouts;
  - `BoxState(version: int, last_good: str | None, seen_heads: list[str], last_good_ledger: str | None, seen: dict[str, dict], admitted: dict[str, dict], refused: dict[str, dict])`, `load_state(dir: Path) -> BoxState` and `save_state(dir: Path, s: BoxState) -> None` (atomic, 0600);
  - `Decision(admitted_now: list[str], would_admit: list[str], refused_now: dict[str, str], deferred: dict[str, str], guests: dict[str, dict])`;
  - `admit(ledger: Ledger, people: People, reserved, directory: Directory, state: BoxState, now: datetime, mode: str) -> Decision`;
  - `main(argv: list[str]) -> int` with commands `walk`, `content`, `settle`, `seed`, `forget` and `fallback` (flags in Step 8).
- **What `walk` stores.** With no state, it seeds last good with `reanchor(head, --anchor)` and `seen_heads = []` (D6); `last_good_ledger` starts at the same commit. After a returned verdict it stores `verdict.last_good` and `verdict.seen_heads`: `[]` after a valid walk (stored as `[]`, the key never deleted), the walk's own list after a freeze. **When `walk()` raises** (`GitError`, `ValueError`) or times out, the command exits 2 and writes nothing, so the old list and the old last good stay; it never stores the raw head. `ledger-drills` (`--dry`, its own state directory) keeps its own last good, list and ledger.
- Constants: `REALLIANCE_QUOTA = 5`, `REALLIANCE_WINDOW = timedelta(days=7)`, `MEMBER_QUOTA = 1`, `GLOBAL_CAP = 10`, `CAP_WINDOW = timedelta(hours=24)`, `MAX_LIFETIME = timedelta(days=7)`, `EXPIRY_GRACE = timedelta(minutes=30)`. Built-in reserved names: `admin root www sso git api mail invites invite guests forgejo forgejo-admin forge-bot forge-ci ops-ci ci-dispatch realliance realliance-ops realliance-ci realmic ghost`. Built-in reserved prefixes: `test- ci- admin-`.
- **Tier rules:** active with `/realliance` in groups is `realliance`. Active with `/guests` in groups is `guest`: an emergency-override entry. Any other active entry is `member`, an inactive one is `inactive`, and an absent username isn't a member.
- **Slot rule (D22):** for inviting member `I` and link `L`, count `I`'s other links that the box saw before `L` and that hold a slot now. A link holds a slot while pending and unexpired by the box clock. An admitted redemption holds one too: within 7 days of `at` for `realliance`, for good for `member`. Admit `L` if the count is below the tier's quota.
- **On time:** `seen[L].redeemed <= seen[L].link + min(expires − created, MAX_LIFETIME) + EXPIRY_GRACE`.

- [ ] **Step 1: Write the failing content tests** in `tests/ledger/test_content.py`:
  - `test_valid_ledger_moves_effective_to_head`.
  - `test_refused_ledger_keeps_last_good_ledger`: a ledger-key commit adding `groups` gives `refused`, `reason.startswith("schema:unknown-key@")` and `effective == last_good_ledger`.
  - `test_later_ledger_changes_wait`: a ledger-key deactivation after the bad commit leaves `effective` unchanged.
  - `test_operator_fix_clears_refusal`: an operator commit that reverts `ledger.yaml` moves `effective` to the head.
  - `test_operator_edit_may_break_transitions`: an operator removing a link passes the verdict (operator edit), and the schema still applies.
  - `test_missing_ledger_is_empty`: no `ledger.yaml` gives an empty ledger, not a refusal.
- [ ] **Step 2: Write the failing admission tests** in `tests/ledger/test_admission.py`, with a fake `Directory` and a fixed `now`:
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
- [ ] **Step 3: Run; expect FAIL.**
- [ ] **Step 4: Implement** `check_ledger`, `load_people`, `load_reserved`, `KeycloakDirectory`, `load_state`/`save_state` and `admit`. Candidates run in `(seen.redeemed, ledger index)` order. Name rules follow D20, rejecting `<>&"$%!#?§;*~/\|^=[]{}()` and every character with Unicode category `Cc`.
- [ ] **Step 5: Run; expect PASS.**
- [ ] **Step 6: Write the failing command-line tests** in `tests/ledger/test_cli.py`:
  - `test_walk_writes_verdict`: `main(["walk", …])` returns 0 and writes `walk.json` with the `WalkVerdict` fields plus `apply_commit`. That is the head on a valid chain in `apply`, `null` on a freeze, and `newest_trusted` in `check`.
  - `test_seen_heads_stored_empty_after_valid_walk`: `state.json` holds `"seen_heads": []`.
  - `test_seen_heads_stored_from_the_verdict_after_a_freeze`: the stored list equals `verdict.seen_heads`, never a list the command built from the raw head.
  - `test_walk_exception_keeps_state`: a `walk` that raises `GitError`, or a timeout, exits 2 and leaves `state.json` byte for byte unchanged.
  - `test_first_run_and_rebuild_reanchor_with_empty_list`: no state and `--anchor A` with an operator commit after `A` give last good at that commit and `seen_heads == []`; with none, last good is `A`.
  - `test_drills_state_separate`: `--dry` keeps its own last good, list and ledger, and never writes `guests.next.json` or `admitted`.
  - `test_status_prints_seen_list`: on a freeze the `sso/apply` description ends with `seen: <sha7>,<sha7> (+N)`, at most 140 characters, and `walk.json` holds the full list.
  - `test_content_writes_outputs`: `main(["content", …])` returns 0 and writes `guests.next.json`, `content.json` and `state.json`. `content.json` has the keys `mode`, `ledger`, `admitted`, `would_admit`, `refused_entries`, `deferred` and `statuses`. `statuses` holds `apply` and `ledger`, each `{state, description}`, prefixed `check:` in check mode.
  - `test_outputs_hold_ids_only`: no fixture email, first name or token, even in part, in `stdout`, `stderr` or any output file.
  - `test_descriptions_fit_a_status`: every description is at most 140 characters, even with 30 refused entries and 30 seen heads.
  - `test_settle_failed_new_refuses_them`: `settle --failed-new` moves this run's new admissions to `refused` as `apply-failed`.
  - `test_settle_applied_promotes_next_to_handed`.
  - `test_seed_from_output`: `seed --from` a `tofu output -json guests` file fills `admitted` by username.
  - `test_forget_removes_one`.
  - `test_fallback_copies_handed_or_empty`: `fallback` writes `handed.json`'s content, or `{"guests": {}}` without that file.
  - `test_parses_as_python_312`: `ast.parse(src, feature_version=(3, 12))`.
- [ ] **Step 7: Run; expect FAIL.**
- [ ] **Step 8: Implement `main`.**
  - `walk --mode {check,apply} --repo DIR --head SHA --state DIR --keyring DIR --trust FILE --anchor SHA [--dry]`
  - `content --mode {check,apply} --repo DIR --walk FILE --state DIR --keycloak-url URL --keycloak-secret-file FILE [--dry]`
  - `settle --state DIR (--applied | --failed-new)`, `seed --state DIR --from FILE`, `forget --state DIR USERNAME`, `fallback --state DIR`
  - Exit 0 on every verdict, a freeze and a refused ledger included, and 2 on a usage or internal error, a raising walk included. Print one summary line, ids only.
  - Description templates: `applied: N admitted, N refused, N deferred`; `frozen: <reason>; seen: …`; `ledger refused: <reason>`; `content failed`; each prefixed `check: would be ` in check mode.
- [ ] **Step 9: Run `tests/ledger`; expect PASS.**
- [ ] **Step 10: Commit:** `sso-ledger: ledger verdict, admission, quotas, caps, box clock, seen-heads state, anchors`.

### Task 4: The box runs the check, in `check` mode, from the changeover anchor

**Files:**
- Modify: `host/roles/config_apply/files/sso-config-apply.sh`, `host/roles/config_apply/tasks/main.yml`, `host/group_vars/sso.yml`, `operators.yml`, `realm/groups.tf`, `tests/host/test_config_apply.py`, `tests/realm/test_realm.py`.
- Create: `host/roles/config_apply/templates/ledger.env.j2`, `host/roles/config_apply/templates/people-trust.json.j2`, `keys/ledger/sso-invites-ledger.asc` (from Task 1), `tests/ledger/test_apply_script.py`.

**Interfaces:**
- Consumes Task 3's commands. Task 5's `var.guests` lands after this task; until then tofu only warns about the undeclared variable in the var-file.
- Produces:
  - `group_vars`: `ledger_mode: check`, `ledger_drill_ref: ledger-drills`, `apply_interval: 1min` and `people_anchor_commit: <40 hex>` (Step 7);
  - `operators.yml`: `ledger_signing_fingerprints: [<LEDGER_FPR>]`;
  - box paths:
    - `/etc/realliance-sso/gnupg-people` (keyring, D3: operators and ledger only), `/etc/realliance-sso/people-trust.json`, `/etc/realliance-sso/ledger.env` (0600: `LEDGER_MODE`, `LEDGER_DRILL_REF`, `PEOPLE_ANCHOR`) and `/etc/realliance-sso/ledger-lookup.secret` (0600);
    - `/var/lib/sso-config-apply/ledger/` (main state) and `/var/lib/sso-config-apply/ledger-drills/` (drill state);
  - script overrides, all defaulting to today's paths (for the harness and Task 19): `SSO_DIR`, `PEOPLE_DIR`, `STATE_DIR`, `SSO_ETC`, `SSO_LOCK`, `SSO_LEDGER_BIN`, `SSO_LEDGER_TIMEOUT` (default 120) and `PEOPLE_REF`.
- No new deploy key and no new checkout: the walk and the content check read the existing `/var/lib/sso-people`, which gains `fetch.fsckObjects=true` and must stay a full clone.
- This box head is the changeover: from it on, `sso-people` heads are no longer checked against the pull keyring.

- [ ] **Step 1: Write the failing harness tests** in `tests/ledger/test_apply_script.py`.
  - **The fixture root:** a temp tree with a signed `SSO_DIR` repo, a `PEOPLE_DIR` repo served from a bare remote with web-flow-signed history below an anchor, a keyring for each of Task 2's keys, and both modes as parameters.
  - **The stubs on `PATH`:** `tofu` records each `-chdir`, the people checkout's commit and each `-var-file`, and fails when `STUB_TOFU_FAIL_IF=<username>` appears in the var-file. `curl` records each status POST.
  - Tests:
    - `test_changeover_anchor_seeds_last_good`: the first run with `PEOPLE_ANCHOR` at the applied head walks valid, stores last good at the anchor and `seen_heads == []`, and never walks the web-flow history below it.
    - `test_merge_button_commit_freezes_apply_mode`: a web-flow merge after the anchor gives no people apply, exit 1, and `sso/apply` failure `frozen: invalid:untrusted-signer@…`.
    - `test_check_mode_applies_last_good_or_operator_below_a_web_flow_head`.
    - `test_check_mode_service_push_at_head_does_not_block`: a ledger-key head over an operator commit applies `people.yaml` at the operator commit, and posts `sso/apply` `check: would be applied…` on the head.
    - `test_check_mode_logs_a_freeze_and_still_applies`: a ledger-key commit touching `people.yaml` at the head applies the newest last-good-or-operator commit, and posts `check: would be frozen: invalid:ledger-path@…`.
    - `test_content_crash_never_blocks[check|apply]`: a `SSO_LEDGER_BIN` whose `content` fails gives exit 0, a people apply with the last handed guests, and `sso/ledger` failure `content failed`.
    - `test_content_timeout_never_blocks[check|apply]`: a `content` that sleeps 10 s with `SSO_LEDGER_TIMEOUT=2` gives the same.
    - `test_walk_crash_fails_closed_in_apply_and_keeps_state`: no people apply, exit 1, `sso/apply` failure `walk failed`, and `state.json` unchanged.
    - `test_walk_crash_does_not_block_check`: bash finds the newest last-good-or-operator commit itself, with `gnupg-people`, and applies it; a web-flow commit is never chosen.
    - `test_apply_mode_freeze_applies_nothing`: an invalid commit under a valid head gives no people `tofu` call at all (last good included), exit 1, and `sso/apply` failure `frozen: invalid:…`.
    - `test_seen_head_freeze_is_sticky`: an Update-branch shape freezes with `not-first-parent-descendant@…`, the next run on a later valid push stays frozen, and the status prints the list.
    - `test_apply_mode_recovery_resumes`: a `Reviewed-Range` commit gives a people apply at the head, moves last good and stores `seen_heads == []`.
    - `test_apply_mode_refused_ledger_still_applies_people`: a ledger-key `groups` commit gives `sso/ledger` failure, `sso/apply` success, and a people apply with the last handed guests.
    - `test_apply_mode_hands_admitted_guest`.
    - `test_new_guest_apply_failure_retries_without_it`: `STUB_TOFU_FAIL_IF=bad-guest` gives two people applies (with the guest, then without), exit 0, and the state refuses it as `apply-failed`.
    - `test_rebuild_reanchors_at_newest_operator_after_anchor`.
    - `test_drill_ref_never_applied`: a valid `ledger-drills` head with a new guest leaves `guests.next.json` unchanged, posts both statuses on the drill head, and keeps its own seen-heads list.
    - `test_changed_mode_skips_realm_when_sso_unchanged`: a second run after only an `sso-people` change calls `tofu -chdir=<people>` and not `<sso>/realm`.
    - `test_statuses_posted_once_per_verdict`: two runs over one head make one POST per context, to `/repos/realliance/sso-people/statuses/<sha>`.
    - `test_unsigned_people_head_never_applied[check|apply]`: `check` applies the commit below and exits 0; `apply` freezes and exits 1.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement the script.** Order:
  1. take the lock;
  2. verify `realliance/sso` as now (the pull keyring, web-flow included), and fetch `sso-people` `main` (and `ledger-drills` if it exists) with the existing read-only key. The old check of the `sso-people` head against the pull keyring goes: the walk replaces it;
  3. **the walk:** `timeout "$SSO_LEDGER_TIMEOUT" "$SSO_LEDGER_BIN" walk … --anchor "$PEOPLE_ANCHOR"` for `main`. In `apply`, a failure fails closed (D2). In `check`, a failure falls back to the newest first-parent commit that's the stored last good or carries an operator fingerprint from `people-trust.json`, found with `git log --first-parent --format='%H %G? %GP'` against `gnupg-people`;
  4. **the content check:** `content …` with the same timeout. On failure, log `ledger: content failed; people apply continues with the last handed guests`, run `sso-ledger fallback` (or write `{"guests": {}}` if that fails too), and post `sso/ledger` failure;
  5. **the drill ref:** `walk --dry --anchor <main's last good>` and `content --dry` for `ledger-drills`, any failure only logged;
  6. **changed mode** exits early only if the `sso` commit, the `sso-people` head and `sha256(guests.next.json)` all equal the stored ones;
  7. the **realm apply** runs only when the `sso` commit changed, or in `force`;
  8. the **people apply** runs at `apply_commit` (`git checkout -q --detach`), or not at all when it's `null`, with `-var-file=guests.next.json`. If it fails and `guests.next.json` differs from `handed.json`, it runs again with `handed.json`. If that passes, run `sso-ledger settle --failed-new`; on any success, `settle --applied`, and write the applied commit to `$STATE/people` as today;
  9. post `sso/apply` and `sso/ledger` from the JSON outputs (or the failure texts) on the head, each only when `(sha, state, description)` changed since the last post. `status()` gains a context argument.
- [ ] **Step 4: Implement the role.**
  - **The keyring (D3):** import the operators' keys and `keys/ledger/*.asc` into `gnupg-people`, delete every key not in `operators[].gpg_fingerprint` or `ledger_signing_fingerprints`, and set ultimate owner trust. Web-flow's key is never imported there. It runs only when the keys or the lists changed. `people-trust.json` lists the two classes as primary fingerprints, uppercase.
  - **The checkout:** `git config fetch.fsckObjects true` on `/var/lib/sso-people`; refuse to proceed on a shallow clone.
  - **The templates:** `ledger.env.j2` (with `PEOPLE_ANCHOR` from `people_anchor_commit`), `people-trust.json.j2` and the lookup secret from `tofu_plan_client_secret`, all `no_log`.
  - **The program:** install `sso_ledger.py` as `/usr/local/sbin/sso-ledger`, mode 0755.
  - **Guards:** `ansible.builtin.assert` that `ledger_mode in ['check', 'apply']` and that `people_anchor_commit` is 40 lowercase hex.
  - **The realm:** `realm/groups.tf` gains `resource "keycloak_group" "guests"` (name `guests`, top level, no roles).
- [ ] **Step 5: Run the harness and every offline test; expect PASS.** Run `ansible-lint host/` too; expected clean. Confirm the box's git supports `%GP` and `%(trailers:key=…,valueonly)` (`ssh root@23.163.0.48 git --version`, expected 2.39 or newer).
- [ ] **Step 6: Add the live tests.**
  - In `tests/host/test_config_apply.py`:
    - `test_people_keyring_holds_operators_and_ledger_only`: fingerprints equal operators plus ledger, and web-flow is absent;
    - `test_pull_keyring_unchanged`: it still holds web-flow for `realliance/sso` and lacks the ledger key;
    - `test_people_anchor_recorded`: `ledger.env`'s `PEOPLE_ANCHOR` equals `group_vars['people_anchor_commit']`;
    - `test_people_checkout_full_and_fsck`;
    - `test_ledger_mode_is` (equals `group_vars['ledger_mode']`);
    - `test_apply_timer_every_minute`;
    - `test_sso_ledger_installed` (`/usr/local/sbin/sso-ledger fallback --help` exits 0);
    - `test_people_head_has_both_statuses`: the `sso-people` head carries `sso/apply` and `sso/ledger`;
    - `test_unsigned_people_head_refused` becomes `test_unsigned_people_head_never_applied`: on the test branch, `check` applies the commit below it and `apply` freezes;
    - `test_box_plans_are_clean` changes: the people plan passes `-var-file=/var/lib/sso-config-apply/ledger/handed.json` when that file exists. Without it, the plan would try to destroy every guest once guests exist, and `prevent_destroy` would fail it.
  - In `tests/realm/test_realm.py`: `test_guests_group_exists_without_roles`.
- [ ] **Step 7: Record the anchor, then STOP—ask Chris (box head and changeover).**
  - Read `/var/lib/sso-config-apply/people` on the box and `git ls-remote origin main` for `sso-people`. They must match: that commit is the current, already-applied head.
  - Commit `people_anchor_commit: <it>` signed, push the branch and open the PR.
  - The STOP: Chris confirms the anchor and holds every `sso-people` push until Step 8 shows the box took it; a push before then freezes until a `Reviewed-Range` naming the anchor. The change: the walk and content checks in `check` mode with no web-flow on `sso-people`, a 1-minute apply timer, realm applies only on `realliance/sso` changes, and group `/guests`. What to watch: the walk line in `journalctl -u sso-config-apply-changed -n 50`, both statuses `check: would be applied…` on the `sso-people` head, and people applies continuing. Rollback trigger: any people apply failure, or `test_box_plans_are_clean` failing. Rollback: revert the PR (Chris merges the revert).
- [ ] **Step 8: Chris merges.** `Monitor` `sso-pull` until the head applies (about 5 min), then one apply run. Expected: `walk.json` shows `valid`, last good at the anchor, `seen_heads == []`. Then run `pytest tests/host tests/realm/test_realm.py --hosts=ssh://root@23.163.0.48`; expected PASS. Ledger the time (it starts the check-only clock) and the anchor's short id. Chris releases the `sso-people` hold.

### Task 5: The guest layer and the empty ledger in `sso-people`

**Files:**
- Create (sso-people): `guests.tf`, `ledger.yaml`, `reserved.yaml`.
- Modify (sso-people): `main.tf`, `README.md`.
- Test: sso `tests/people/test_people.py`, on branch `invites-people-tests` of the sso worktree.

**Interfaces:**
- Produces:
  - `ledger.yaml`: exactly `version: 1\nlinks: []\n`;
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
  - `test_ledger_file_is_inert_to_tofu`: the sandbox copy carries `ledger.yaml`, and the plan doesn't change because of it.
- [ ] **Step 2: Run; expect FAIL:** `SSO_PEOPLE_DIR=~/Repos/realliance-sso-people-invites taskset -c 0-3 nix develop ~/Repos/realliance-sso-invites --command pytest tests/people -q -k "guest or ledger"`.
- [ ] **Step 3: Implement.**
  - **One object shape:** `main.tf` normalises every `people.yaml` entry to `{active, groups, mfa, bootstrap}`, with `bootstrap = try({email, first_name, last_name = try(…, null)}, null)`, so `merge()` meets one type.
  - **Optional profile fields:** `email`, `first_name` and `last_name` read through `try(…, null)`.
  - **The precondition:** `lifecycle.precondition` on `keycloak_user.person` reads `each.value.bootstrap != null || contains(keys(var.guests), each.key)`, error message `bootstrap is required for a new person`.
  - **`guests.tf`** builds `guest_people` with `groups = local.guest_groups` and never reads a groups field.
  - **The README** gets a "Guests and the ledger" section beside Task 2b's merge rule: what `ledger.yaml` is and who writes it; the path rule and the freeze; that an operator pushes a `Reviewed-Range` commit directly; and the local plan recipe (`tofu output -json guests | jq '{guests: .}'` into a `umask 077` file, then `-var-file`).
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Plan against live state, read-only.** In the sso-people worktree, with `realm/secrets.env` loaded by the same env helper the tests use and `TF_VAR_keycloak_client_id=tofu-plan`, run `tofu plan -var-file=<(echo '{"guests": {}}') -detailed-exitcode`. Expected: exit 0, `No changes.` The refactor touches no member.
- [ ] **Step 6: Commit both repos signed** (sso-people: `people: guests layer (guests.tf), an empty ledger, one user map, reserved names`; sso: `tests: guest layer sandbox cases`), push, and open both PRs. **Chris:** merges sso-people locally as a signed squash. The box applies it within 2 min. Then `test_box_plans_are_clean` passes, and the head's `sso/ledger` reads `check: would be applied: 0 admitted…`. **Chris:** merges the tests PR.

### Task 6: The check-only run (at least 3 days)

**Files:** create `tests/invites_helpers.py` and `tests/invites/test_ledger_drills.py` (runs only with `INVITES_DRILL=check-window`).

**Interfaces:**
- Produces, in `invites_helpers.py`:
  - `PeopleClone(ref)`: a fresh full temp clone of `sso-people` under `tempfile.mkdtemp()`, removed on close, that never prints file content;
  - `.ledger()` and `.write_ledger(mutator)`, with the edits `add_link(inviter) -> link_id`, `redeem(link_id, username, email, first, last=None, mfa="webauthn")`, `revoke(id)` and `deactivate(id)`;
  - `.commit_and_push(signer: "operator" | "ledger" | "unsigned", message, extra: dict[str, bytes | None] | None = None, parents: int = 1) -> sha`. The `ledger` signer imports `SECRETS_DIR/invites/ledger-signing.asc` into a temp `GNUPGHOME` inside the test process; `unsigned` uses `git commit-tree` like `test_pull.py`'s fixture; `extra` adds the path violations;
  - `.recover(last_good, tip) -> sha`: an operator-key commit carrying `Reviewed-Range: <last_good>..<tip>`, pushed directly;
  - `web_flow_commit(ref, path, mutator, message) -> sha` through `gh api -X PUT repos/realliance/sso-people/contents/<path>` on `ledger-drills` only;
  - `merge_button(ref, head_branch, method) -> sha` through `gh api` on a PR whose base is `ledger-drills`, never `main`;
  - `wait_status(sha, context, timeout=300) -> (state, description)`;
  - `box_result(ref="main" | "drills")`, which reads that ref's `walk.json` and `content.json` over SSH.
- Drill usernames start with `chk-` and use `delivered+chk-<hex>@resend.dev`. The inviting member is `chris`. A taken username or email comes from a `user_factory` user, never a real person. Bad commits go only on `ledger-drills`, which starts from `main`; a drill recovery never names main's last good.

- [ ] **Step 1: STOP—ask Chris.** Approve, for the length of the window:
  - ledger-only commits signed with his key on `sso-people` `main`: hand entries with `chk-` guests, inert in `check`;
  - branch `ledger-drills`, which carries every deliberately bad commit, its web-flow commits and merge-button merges, and its recoveries, never applied.
- [ ] **Step 2: Write the drills**, each asserting the box's verdict through `wait_status` and `box_result`:
  - On `main`: `test_hand_entry_would_admit`, a description starting `check: would be applied: 1 …`.
  - On `ledger-drills`, freezes; after each check comes `.recover(…)` and its success:
    - `test_ledger_key_touches_people_yaml_freezes` (`invalid:ledger-path`);
    - `test_ledger_key_rename_freezes`, `test_ledger_key_mode_change_freezes`, `test_ledger_key_submodule_freezes` and `test_ledger_key_merge_freezes`;
    - `test_unsigned_commit_freezes`;
    - `test_web_flow_commit_freezes`: a web-editor commit through the contents API (`invalid:untrusted-signer`);
    - `test_merge_button_freezes` and `test_squash_button_freezes`, through a PR into `ledger-drills`.
  - On `ledger-drills`, recovery drills, each answered by a correct recovery:
    - `test_web_flow_recovery_rejected` (`recovery-not-operator-key`);
    - `test_short_sha_recovery_rejected` (`recovery-bad-trailer`);
    - `test_recovery_missing_a_commit_rejected` (`recovery-misses`);
    - `test_replayed_recovery_rejected` (`recovery-stale-last-good`).
  - On `ledger-drills`, content and entry drills, with no freeze:
    - `test_groups_key_refuses_ledger` (`sso/ledger` failure `schema:unknown-key`);
    - `test_redemption_without_mfa_refuses_ledger`;
    - `test_later_ledger_change_waits`;
    - `test_username_taken`, `test_username_reserved`, `test_email_taken_case_variant`, `test_name_prohibited_character` and `test_inviter_absent`.
  - The quota and cap cases stay offline (Task 3) and in Task 19's fixture runs, so no real member's slot is ever touched.
- [ ] **Step 3: Run them once:** `INVITES_DRILL=check-window taskset -c 0-3 nix develop ~/Repos/realliance-sso-invites --command pytest tests/invites/test_ledger_drills.py -q`. Expected: all pass, and `main`'s statuses stay success. Ledger each verdict (link ids, reason codes, short commit ids).
- [ ] **Step 4: STOP—ask Chris (Chris's step, optional).** The force-push drill on `ledger-drills`: Chris force-pushes a prepared ledger-signed commit that doesn't descend from the drill branch's last good. Expected: `frozen: not-fast-forward`, then `not-first-parent-descendant@…` for the earlier head while it stays in the list. The agent then pushes an operator-key recovery naming the drill branch's last good and a tip whose history holds every seen head still there, and expects success and an empty list.
- [ ] **Step 5: Watch the window.** Once a day (a `Monitor` armed for 24 h each time), ledger:
  - the day's walk and content lines for both refs;
  - `sso/apply` on any `sso-people` head merged that day, real people changes included, each merged locally;
  - `test_box_plans_are_clean`.

  Expected: every verdict as predicted, `main` never frozen, and people applies unaffected.
- [ ] **Step 6: Exit gate.** At least 3 days since Task 4 Step 8 (a week if no other task is waiting), every drill's verdict seen, zero unexpected verdicts, and `main`'s latest `sso/apply` is `check: would be applied…` with an empty seen-heads list. Ledger the summary. Commit the drills to branch `invites-drills`; **Chris:** merges (tests only).

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
- [ ] **Step 7: Land the people change.** Open the sso-people PR (`people: /apps/invites for every person, never a guest`). **Chris:** merges it locally as a signed squash. Verify `test_box_plans_are_clean`; the box's apply log shows only group-membership changes.

### Task 8: The service repo and its ledger package

**Files:**
- Modify (forge inventory, branch `sso-invites-repo`): `repos.tf`, adding `forgejo_repository.sso_invites` and `forgejo_branch_protection.sso_invites_main` in `ci_dispatch`'s shape.
- Create (service): `go.mod` (`module git.realliance.net/realliance-ops/sso-invites`, `go 1.26.0`, `toolchain go1.26.10`), `.ko.yaml` (forge-bot's base image digest), `.forgejo/workflows/ci.yaml` (forge-bot's, with `KO_DOCKER_REPO: git.realliance.net/realliance-ops/sso-invites` and `./cmd/sso-invites`), `LICENSE-MIT`, `LICENSE-APACHE`, `README.md`, `deps_test.go`, `internal/secret/secret.go` (forge-bot's at `9be4857`, header naming the source), `internal/ledger/{ledger.go,parse.go,mutate.go,ledger_test.go}`, and `internal/ledger/testdata/vectors/` (copied from sso `tests/ledger/vectors/` at Task 2's `6c2dc13`, with a `SOURCE` file naming it).

**Interfaces:**
- Produces (Go):
  - `type Ledger struct { Version int; Links []Link }`;
  - `type Link struct { ID, Inviter, Created, Expires, TokenSHA256, State, Redeemed, Revoked string; Guest *Guest }`;
  - `type Guest struct { Username string; Bootstrap Bootstrap; MFA string; Active bool }`;
  - `type Bootstrap struct { Email, FirstName, LastName string }`, where Marshal leaves out an empty `LastName`;
  - YAML keys exactly as in the schema;
  - `func Parse(b []byte) (*Ledger, error)`, returning `*SchemaError{Code string}` with the box's codes;
  - `func (l *Ledger) Marshal() ([]byte, error)`: canonical, fixed key order, quoted timestamps. Task 2's contract: it quotes every string a YAML 1.1 or 1.2 reader would take as another type, and leaves absent optional fields out (never `null`);
  - `func (l *Ledger) AddLink(id, inviter string, created time.Time, ttl time.Duration, tokenSHA256 string) error`;
  - `Revoke(id string, at time.Time) error`, `Redeem(id string, g Guest, at time.Time) error`, `Deactivate(id string) error`;
  - `ByTokenHash(h string) (*Link, bool)`, `ByID(id string) (*Link, bool)`;
  - `var ErrNotPending, ErrNotFound, ErrNotActive`.

- [ ] **Step 1: The repo.** Add the two resources to `repos.tf`, plan (expected `2 to add, 0 to change, 0 to destroy`), commit signed and open the inventory PR. **Chris:** merges fast-forward. **STOP—ask Chris**, then apply (expected `2 added`); not inside the docs-site plan's inventory apply. Clone to `~/Repos/sso-invites` and branch `v1` from `main`.
- [ ] **Step 2: Write the failing tests** in `internal/ledger/ledger_test.go`:
  - `TestParseMinimal`;
  - `TestParseRefusesUnknownKey`, `TestParseRefusesDuplicateKey`, `TestParseRefusesAlias`, `TestParseRefusesOversize` (1 MiB plus 1);
  - `TestMarshalCanonical`: a golden file `testdata/golden.yaml`, byte-equal;
  - `TestMarshalRoundTripsThroughBoxVectors`: every `ok` vector parses and re-marshals to bytes that parse equal; every refuse vector fails with the same `Code`; `pad_to` is honoured;
  - `TestMarshalQuotesAmbiguousStrings`: a first name `yes`, `null`, `0x1F`, `1_000` or `2026-11-08` comes out quoted and parses back as the same string;
  - `TestMarshalOmitsAbsentFields`: no `null` anywhere in the output;
  - `TestAddLinkExpiresSevenDays`: `Expires − Created == 168h`, and a TTL over 168h is refused;
  - `TestRedeemRequiresPending`, `TestDeactivateOnlyTrueToFalse`;
  - `TestMutationsNeverTouchOtherLinks`: marshal before and after differ only inside the target link.
  - In `deps_test.go`, `TestDirectRequires`: every direct require in `go.mod` is one of `go-git/v5`, `go-crypto`, `go.yaml.in/yaml/v3`, `go-oidc/v3`, `golang.org/x/oauth2` and `client_golang`. Later tasks add the rest of that set, and nothing outside it.
- [ ] **Step 3: Run; expect FAIL:** `cd ~/Repos/sso-invites && $GO test -race -p=4 ./...`.
- [ ] **Step 4: Implement.** The strict parse decodes into `yaml.Node` first, rejecting alias and anchor nodes, explicit tags, `null` values and duplicate mapping keys, and integers over 18 digits, then decodes with `KnownFields(true)` and runs the same schema checks as the box.
- [ ] **Step 5: Run; expect PASS.** `$GO vet ./...` clean.
- [ ] **Step 6: Commit signed and push `v1`** (each push runs CI on `galaxy`; it must stay green).

### Task 9: `internal/gitstore`

**Files:** create `internal/gitstore/{store.go,store_test.go}`.

**Interfaces:**
- Consumes `ledger.Parse` and `ledger.Marshal`.
- Produces:
  - `type Config struct { URL, Branch string; Auth transport.AuthMethod; Signer *openpgp.Entity; Name, Email string; Sleep func(time.Duration) }`;
  - `func Open(ctx context.Context, c Config) (*Store, error)`, with in-memory storage and a full history (no shallow fetch);
  - `func (s *Store) Head(ctx context.Context) (sha string, l *ledger.Ledger, err error)`: it fetches and reads the `ledger.yaml` blob alone (a missing file is an empty ledger);
  - `func (s *Store) Update(ctx context.Context, message string, mutate func(*ledger.Ledger) error) (sha string, err error)`;
  - `func SSHAuth(keyPEM []byte, knownHostsLine string) (transport.AuthMethod, error)`, using `ssh.FixedHostKey`;
  - `var ErrExhausted` (the fifth non-fast-forward refusal), `ErrPathCheck` and `ErrBadMessage`.
- **Messages:** they must match `^(link|revoke|redeem|deactivate) l-[0-9a-f]{16}$`.
- **Retries (D13):** each try fetches, reads `ledger.yaml` afresh, re-runs `mutate`, rebuilds the tree on the new head, signs and pushes. Waits are 1, 2, 4 and 8 s plus up to 20 % jitter, through `Config.Sleep` so tests run instantly. `mutate` must be safe to repeat.

- [ ] **Step 1: Write the failing tests** against a local bare repo created in `t.TempDir()`, seeded with an operator-signed root holding `people.yaml` and `ledger.yaml`:
  - `TestHeadReadsOnlyLedgerYAML`: a storage wrapper that records every blob read sees exactly one, `ledger.yaml`'s;
  - `TestUpdateCommitsSignedWithLedgerKey`: `git verify-commit` (`git` and `gpg` on `PATH`) with a `GNUPGHOME` holding the test ledger key passes, and `%GP` equals its fingerprint;
  - `TestCommitTouchesOnlyLedgerYAML`: `git diff-tree --raw --no-renames -r` prints exactly one `:100644 100644 … M` line for `ledger.yaml`, and the commit has one parent;
  - `TestPathCheckRefusesOtherChanges`: the pre-sign check, handed a tree that also changes `people.yaml` or a mode, returns `ErrPathCheck` and signs nothing;
  - `TestUpdateLosesRaceToOperatorMergeAndRebuilds`: an operator-signed squash commit that changes `people.yaml` lands between the store's fetch and push. The retry rebuilds on it, the history reads operator commit then service commit, and `people.yaml` holds the operator's content;
  - `TestUpdateGivesUpAfterFiveTries`: a racer that always wins gives `ErrExhausted` after exactly 5 pushes, with waits of 1, 2, 4 and 8 s within jitter;
  - `TestUpdateNeverForcePushes`: after a lost race the remote branch still holds the racer's commit, and every push uses a non-force ref spec;
  - `TestUpdateRefusesBadMessage`;
  - `TestSSHAuthPinsHostKey`: the callback rejects a different host key;
  - `TestNoPersonalDataInCommitMessage`.
- [ ] **Step 2: Run; expect FAIL:** `$GO test -race -p=4 ./internal/gitstore/...` (needs `git` and `gpg` on `PATH`; the system's are fine).
- [ ] **Step 3: Implement** with go-git's object API: copy the parent tree, replace the `ledger.yaml` entry, compare the two trees entry by entry before signing, and sign with `CommitOptions.SignKey`. Push without force, so a non-fast-forward comes back as `git.ErrNonFastForwardUpdate`, which triggers the retry.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit signed and push `v1`:** `gitstore: in-memory sso-people clone, ledger-only signed commits, bounded retry`.

### Task 10: Guarded read-only clients

**Files:** create `internal/keycloak/{client.go,guard.go,client_test.go}` and `internal/github/{status.go,guard.go,status_test.go}`.

**Interfaces:**
- Produces:
  - `keycloak.New(baseURL, realm, clientID string, secret secret.Value, hc *http.Client) *Client`, with:
    - `UsernameTaken(ctx, u string) (bool, error)`;
    - `EmailTaken(ctx, e string) (bool, error)`, which folds to lower case first;
    - `UserEnabled(ctx, u string) (exists, enabled bool, err error)`.
  - `github.NewStatusReader(repo string, tok secret.Value, hc *http.Client) *StatusReader`, with `Get(ctx, sha string) (map[string]Status, error)` holding contexts `sso/apply` and `sso/ledger` (absent ones left out), `Status{State, Description string}`.
- **Keycloak's guard** permits only:
  - `POST /realms/master/protocol/openid-connect/token`;
  - `GET /admin/realms/<realm>/users` with query keys from {`username`, `email`, `exact`, `briefRepresentation`, `max`}.
- **GitHub's guard** permits only `GET https://api.github.com/repos/realliance/sso-people/commits/<40 hex>/statuses`.
- Both copy forge-bot's wire-form checks, refuse redirects, and set `Proxy: nil`.

- [ ] **Step 1: Write the failing tests** (`httptest`):
  - `TestGuardAllowsOnlyListedCalls`, a table: group listings, `/users/{id}`, a PUT, `..`, `%2e` and another host are all refused before sending;
  - `TestUsernameTakenExact`, `TestEmailTakenLowercases`, `TestUserEnabled`;
  - `TestTokenCachedUntilExpiry`;
  - `TestErrorsNeverContainSecrets`: neither the client secret nor the token appears in `err.Error()` or the `%+v` output;
  - `TestNoRedirectFollow`;
  - `TestStatusReadsBothContexts`, `TestStatusGuardRefusesOtherRepos`, `TestStatusGuardRefusesWrites`.
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
  - errors `ErrClosed`, `ErrQuota`, `ErrNotYours`, `ErrNotPending`, `ErrExpired`, `ErrInviterGone`, `ErrUsernameUnavailable`, `ErrEmailUnavailable` and `ErrBusy` (the store's `ErrExhausted`);
  - `audit.Logger`, with `Event(kind, linkID string, fields map[string]string)`. The field keys come from a fixed allowlist with no email, name or token key.
- Values: the TTL is 168h. The token is 32 bytes from `crypto/rand`, as base64url without padding; the ledger stores only its SHA-256. `Remaining` uses the box's slot rule (D22) on the service's clock: 5 per 7 days for realliance and 1 for life for member.

- [ ] **Step 1: Write the failing tests**:
  - `TestTierFromRoles`;
  - `TestRemainingRealliance`: 5 at start, 0 after 5 links in 7 days, back to 1 when one redemption is 7 d and 1 s old;
  - `TestRemainingMemberLifetime`: a revoked link frees its slot, a redemption doesn't, and an expired link does;
  - `TestCreateLinkTokenIs256Bit`: `len(token) == 43`, and the decoded token is 32 bytes;
  - `TestCreateLinkStoresOnlyHash`: the marshalled ledger lacks the token and holds `sha256(token)`;
  - `TestCreateLinkRefusedWhenClosed`, `TestCreateLinkRefusedOverQuota`;
  - `TestRevokeOnlyOwnPending`: an operator may revoke any link;
  - `TestRedeemValidates`, a table: D20's username, email and name rules; MFA must be `webauthn` or `totp`, with `none` and `""` refused;
  - `TestRedeemNormalisesEmail`: the ledger stores `" Bob@Example.COM "` as `bob@example.com`;
  - `TestRedeemRefusesTakenUsernameAndEmail`, from Keycloak and from another ledger guest;
  - `TestRedeemRefusesWhenInviterDisabled`;
  - `TestRedeemRefusesExpiredOrUsed`: a second redemption with the same token gives `ErrNotPending`;
  - `TestRedeemRetriesOnRaceAndRevalidates`: a racer takes the username between attempts, giving `ErrUsernameUnavailable`;
  - `TestRedeemReportsBusyWhenPushExhausted`: `ErrExhausted` from the store becomes `ErrBusy`, and the invitee can try again;
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
  - `TestBusyAsks503WithRetry`: `ErrBusy` gives 503 with `Retry-After`;
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
- **Config** (`internal/config`): `public_url`, `open`, `oidc{issuer, client_id, client_secret_file}`, `keycloak{url, realm, client_id, client_secret_file}`, `ledger{url, branch, watch_refs, deploy_key_file, known_hosts_file, signing_key_file, status_repo, status_token_file}`, `session_key_file`, `operators[]` and `reserved[]`. `Load` refuses unknown keys and missing files.
- **Metrics** (on `:9090`):
  - `sso_invites_head_status_bad_seconds{ref,context}`, with `ref` in {`main`, `drills`} and `context` in {`apply`, `ledger`};
  - `sso_invites_redemptions_without_user`;
  - `sso_invites_ledger_push_failures_total` and `sso_invites_ledger_push_exhausted_total`;
  - `sso_invites_ledger_push_last_success_timestamp`;
  - `sso_invites_guest_deactivations_total`;
  - `sso_invites_orphan_guests`;
  - `sso_invites_watch_last_success_timestamp`;
  - `sso_invites_requests_total{route,code}`, with fixed label sets.
- **The watcher** (`watch.New(store, status, kc, m, aud, now)`, `Tick(ctx) error`):
  - it runs every 60 s over each `watch_refs` entry (`main`, `ledger-drills`), reading heads through the store;
  - **bad seconds** is now minus the first time the watcher saw the current head, while that context's status on it fails or is missing, and 0 once that status reads success;
  - **without a user** counts redeemed links older than 20 min whose username Keycloak lacks, up to 7 days after redemption;
  - **deactivated** counts each `active` `true` to `false` on `main` once, with an audit line;
  - **orphans** counts active guests whose inviting member's account is off or missing;
  - a failed tick keeps the last values.

- [ ] **Step 1: Write the failing tests**:
  - `TestHeadStatusBadSeconds`: missing for 61 min gives at least 3660; a failure gives the same; success gives 0; each context counts on its own;
  - `TestDrillRefLabelled`;
  - `TestRedemptionWithoutUserAfter20Min`: 19 min gives 0 and 21 min gives 1;
  - `TestDeactivationCountedOnce`;
  - `TestOrphansCountsDisabledInviters`;
  - `TestPushExhaustionCounted`;
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
- **Namespace and secret:** namespace `sso-invites` with Pod Security `restricted` (enforce, warn, audit). The SealedSecret `sso-invites` holds the keys `oidc-client-secret`, `keycloak-client-secret`, `people-deploy-key`, `ledger-signing-key`, `status-token` and `session-key`, projected 0440 at `/etc/sso-invites/secrets/`.
- **Deployment:** 1 replica; image pinned by digest; uid 65532; read-only root; every capability dropped; RuntimeDefault seccomp; no service account token; no service links; `GOMEMLIMIT=96MiB`; requests `10m`/`32Mi`, limit `128Mi`; ports `http` 8080 and `metrics` 9090; liveness and readiness on `/healthz`.
- **Edge:** the Ingress names host `invite.realliance.net` with no `spec.tls`, and its annotation `traefik.ingress.kubernetes.io/router.middlewares: sso-invites-ratelimit@kubernetescrd` points at the `Middleware` `ratelimit` (`average: 30`, `period: 1m`, `burst: 30`, `sourceCriterion.ipStrategy.depth: 0`, revisited at Task 16).
- **NetworkPolicy:** the `default-deny` and `allow-dns` components. In: namespace `traefik` on 8080, and namespace `prometheus` on 9090. Out: TCP 443 to `0.0.0.0/0`, except ci-dispatch's private ranges and `152.44.247.0/25`.
- **Alerts:**
  - `SSOInvitesHeadNotApplied`: `sso_invites_head_status_bad_seconds{ref="main"} > 3600`, with the `context` label kept;
  - `SSOInvitesRedemptionNoAccount`: over 0 for 5 m;
  - `SSOInvitesLedgerPushFailing`: any exhausted push in 15 m, or failures with no success in 15 m;
  - `SSOInvitesGuestDeactivated`: any increase over 10 m;
  - `SSOInvitesOrphanGuests`: over 0 for 1 h;
  - `SSOInvitesWatchStale`: over 600 s.

  All carry `severity: warning`. The drill ref raises no alert.
- **Config:** `config.yaml` sets `open: false`, `ledger.url: ssh://git@ssh.github.com:443/realliance/sso-people.git`, `ledger.watch_refs: [main, ledger-drills]`, `ledger.status_repo: realliance/sso-people`, `operators: [chris, connor]`, and a public `reserved` subset (bots, orgs, `www`). `known_hosts` holds `[ssh.github.com]:443` with `github_ssh_host_key`'s value from sso `group_vars`.

- [ ] **Step 1: Write the failing static tests** in `scripts/test_sso_invites_manifests.py`, in the shape of `test_ci_dispatch_manifests.py`, rendering with `kubectl kustomize` and never reading `*.secret.*`:
  - `test_namespace_restricted_psa`, `test_deployment_hardening`;
  - `test_image_pinned_by_digest`, pattern `^git\.realliance\.net/realliance-ops/sso-invites:sha-[0-9a-f]{7}@sha256:[0-9a-f]{64}$`;
  - `test_ingress_one_host_no_tls_no_dns_annotations`, `test_ratelimit_middleware`;
  - `test_metrics_port_not_routed`;
  - `test_networkpolicy_exact`;
  - `test_servicemonitor_release_label`;
  - `test_alert_names_and_main_only`;
  - `test_only_sealed_secrets_rendered`;
  - `test_config_values`;
  - `test_known_hosts_pins_github_key`;
  - `test_renovate_never_automerges_sso_invites`;
  - `test_hook_watches_every_input`;
  - `test_app_listed_only_when_deployed`, keyed on `DEPLOYED = False`.
- [ ] **Step 2: Write `alerts_test.yaml`**: each alert fires on its trigger and stays quiet on its healthy twin, and a drill-ref series alone stays quiet. Write `scripts/test-sso-invites-alerts.sh` as a copy of the ci-dispatch script.
- [ ] **Step 3: Run; expect FAIL.**
- [ ] **Step 4: Write the manifests.** Write `secrets.secret.yaml` with a `python -I` script that reads `invites/secrets.env` and the two key files from sso-secrets, writes the Secret and prints its byte count. Then `./sign.sh`.
- [ ] **Step 5: Run the static tests and `nix shell --inputs-from . nixpkgs#prometheus.cli nixpkgs#yq-go --command scripts/test-sso-invites-alerts.sh`; expect PASS.**
- [ ] **Step 6: Commit and push.** The Argo `Application` file isn't listed in `cluster/applications/kustomization.yaml`, so Argo renders nothing new and the push is routine. Expected: the hook passes.

### Task 15: Switch to `apply`, and a hand entry end to end

**Files:** modify sso `host/group_vars/sso.yml` (`ledger_mode: apply`) and `tests/host/test_config_apply.py`.

**Interfaces:** consumes Tasks 4 and 6 (the gate), and Task 6's helpers.

- [ ] **Step 1: Preconditions.**
  - Task 6's exit gate is in the progress ledger.
  - Push one ledger-only commit, signed with Chris's key, to `sso-people` `main`. It sets `active: false` on every `chk-` guest (D8) and revokes every drill link still `pending`, since a pending link would hold a slot of Chris's after the switch.
  - `box_result("main")` then shows a valid walk, `seen_heads == []` and `would_admit == []`.
- [ ] **Step 2: Commit, push and open the PR. STOP—ask Chris (the check-to-apply switch; box head).**
  - Evidence: the check-window summary, the days elapsed, and `main`'s current verdict.
  - What changes: the box starts handing admitted guests to the people layer, and an invalid commit on `main`, a merge-button commit included, now freezes member applies.
  - Watch: the first `apply` run's result is `applied: 0 admitted`, and `test_box_plans_are_clean` stays clean.
  - Rollback trigger: any people apply failure, or any guest admitted that wasn't expected. Rollback: a revert to `check`. Admitted guests stay handed, by D7.
- [ ] **Step 3: Chris merges.** `Monitor` the pull, then the first apply. Expected: both statuses on the `sso-people` head are success, `applied: 0 admitted, 0 refused, 0 deferred`.
- [ ] **Step 4: STOP—ask Chris (the first operator-signed hand entry).** A ledger-only commit signed with his key, pushed to `main`: one link, and one redemption by `hand-<hex>`, `delivered+hand-<hex>@resend.dev`, `mfa: totp`. The inviting member is `chris`, which uses one of his 5 slots for 7 days. Expected within 10 min:
  - a Keycloak user `hand-<hex>` with `enabled: true`, groups exactly `{guests}`, `mfa` `["totp"]`;
  - a Resend mail to that address (`tests/realm/test_enrolment.py`'s helper);
  - the head's `sso/ledger` reads `applied: 1 admitted`.

  Ledger the minutes from push to mail. Then a deactivation commit the same way: on the next run, `enabled is False`. Keep the guest for Task 20's erasure.

### Task 16: Deploy the service, closed

**Files:** modify `cluster/applications/kustomization.yaml` (list `sso-invites.yaml`) and `scripts/test_sso_invites_manifests.py` (`DEPLOYED = True`).

- [ ] **Step 1: Preconditions.** Task 14 pins Task 13's digest and is on `main`, and Gate G before is clean. Load the `edge-ingress` skill and take its snapshot (G3, before).
- [ ] **Step 2: STOP—ask Chris (deploy).** The change: a new public host `invite.realliance.net` with `open: false`, and a pod that holds the ledger signing key, a write key to `sso-people` and a view-only Keycloak reader. Watch: the rollout, `/healthz`, `sso_invites_watch_last_success_timestamp`, and the edge snapshot after. Rollback trigger: a crash loop, a deny line for its own traffic, or any edge snapshot change besides the new host. Rollback: revert the listing; Argo prunes.
- [ ] **Step 3: Push and `Monitor` the rollout** with the `safe-rollout` skill, then run Gate G after, including the edge snapshot diff.
- [ ] **Step 4: Read-only checks.**
  - Sign in as `chris`: the member page says invites aren't open yet.
  - `sso_invites_head_status_bad_seconds{ref="main"}` reads 0 for both contexts.
  - Prometheus has loaded the alerts: `kubectl -n sso-invites get prometheusrule` and Prometheus `/api/v1/rules`.
- [ ] **Step 5: Client IP evidence.** Find the latest request to `invite.realliance.net` in `kubectl -n traefik logs deploy/traefik` and read its `ClientHost` (an IP, no path). Traefik's Service uses `externalTrafficPolicy: Cluster`, so kube-proxy may rewrite the source. If it's a node or pod address, set the `Middleware` values `average` and `burst` to 120, rely on the service's per-token and global limits, and ledger the finding for Chris as an edge follow-up. Changing `externalTrafficPolicy` is out of scope here. If it's a WAN address, keep 30.

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
  - Ledger D15's source facts.
- [ ] **Step 2: STOP—ask Chris (the ladder ruling).**
  - **Path 1 (recommended):** `DEFAULT_GROUPS` caps unknown accounts from creation, and `quota.sh` maps members and orgs, rerun each time a member joins, at each promotion and for each new org.
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
- [ ] **Step 6: Chris merges locally** as a signed squash. After the box applies, `_groups_of(hand-<hex>) == {"guests", "forgejo"}` and `test_box_plans_are_clean` passes.

### Task 19: Open the service, and acceptance

**Files:** create sso `tests/invites/test_invites.py` and `tests/invites/test_invites_box.py`; modify `tests/invites_helpers.py`, luma-homeops `cluster/sso-invites/config.yaml` (`open: true`) and `scripts/test_sso_invites_manifests.py` (`test_config_values` expects `open: true`), and sso-people `people.yaml`, which gains `invites-test` (Chris's 2026-11-08 answer) through a PR the agent writes with a `python -I` script that appends the entry and prints nothing.

**Interfaces:**
- `invites_member()`: resets `invites-test`'s password through the administrator API, deletes its OTP credentials, and returns a `User` for TOTP enrolment at sign-in.
- `invites_login(user)`, in the shape of `forge_login_attempt`, returns a signed-in `requests` session.
- `box_fixture_run(commits, people, state=None) -> dict`: builds a temp repo locally, copies it and a fixture `people.yaml` (throwaway names only) to `/tmp/ledger-fixture-<hex>` on the box, runs `/usr/local/sbin/sso-ledger walk --dry` and `content --dry` against the real keyring and Keycloak, returns both results, and removes the directory.
- `erase_test_guest(username)`: in one SSH session holding the box's lock, the Keycloak delete, `tofu state rm` of both addresses and `sso-ledger forget` (D9). The ledger history keeps the deactivated throwaway entry, which holds no real person's data.

- [ ] **Step 1: Land `invites-test`.** **Chris:** merges the sso-people PR locally as a signed squash. Expected: the user exists with `/realliance` and `/apps/invites`.
- [ ] **Step 2: Commit the `open: true` change. Gate G, then STOP—ask Chris (deploy `open: true`).** Watch: one throwaway link per test, the Resend count (under 10 for the run), and the alerts. Rollback: `open: false`. On approval, push and `Monitor` the rollout.
- [ ] **Step 3: Write the acceptance tests**, mapped to the amended spec's "Success looks like":
  1. `test_member_link_redeem_mail_and_forge` (1). As `invites-test`, create a link and redeem it as `acc-<hex>` with `mfa: totp`, so the sign-in helper can enrol TOTP. Then:
     - the mail reaches Resend within 10 min, and the user has groups `{guests, forgejo}`;
     - after an administrator sets a password, `forge_login` succeeds with no team;
     - the `invites` client refuses the guest.
  2. `test_quotas_on_installed_code` (2), through `box_fixture_run`:
     - the second link of a one-for-life member is refused until the first is revoked;
     - a sixth link in 7 days is refused;
     - an eleventh redemption in 24 h is deferred `cap`.
  3. `test_ledger_refusals_while_people_apply` (3), live on `main` with the ledger key: a `groups` commit and a redemption without `mfa` each turn `sso/ledger` to failure. Meanwhile `sso/apply` stays success and `sso-config-apply force` exits 0. An over-quota entry, via `box_fixture_run`, is refused as an entry. Each ledger refusal ends with an operator-key fix.
  4. `test_bad_commits_freeze_and_recover` (4), on `ledger-drills`: a ledger-key commit that touches `people.yaml`, a rename, a mode change and a submodule; an unsigned commit; a web-flow commit and a merge-button merge; an Update-branch shape; and (Chris's step, optional) a force-push. Each gives `frozen`, and `sso_invites_head_status_bad_seconds{ref="drills",context="apply"}` climbs. A correct `Reviewed-Range` resumes and empties the list; a replayed one doesn't. `box_fixture_run` adds the web-flow `people.yaml` commit below a later operator commit, and the anchor case: web-flow history before the anchor never freezes. The real member freeze is Task 4's harness, run again here.
  5. `test_faults_and_service_push_leave_people_apply` (5): on the box, `SSO_LEDGER_BIN` that fails `content`, and `SSO_LEDGER_TIMEOUT=1`, each give exit 0 with people applied. The check-mode cases (faults and a service push at the head) are Task 4's harness, run again here.
  6. `test_push_race_rebuilds` (6): Task 9's `TestUpdateLosesRaceToOperatorMergeAndRebuilds`, run again here and cited, plus the live service's `sso_invites_ledger_push_failures_total` after an operator's local squash lands during a redemption.
  7. `test_refused_ledger_alerts_and_override_disables` (7, `@pytest.mark.slow`, over 65 min). A ledger refused on `main` raises `SSOInvitesHeadNotApplied{context="ledger"}`. While it stays refused, **Chris** pushes a signed override entry for `acc-<hex>` (`active: false`, `groups: [/guests]`, no `bootstrap`), and `enabled is False` on the next run. Then the fix, and the alert resolves.
  8. `test_deactivated_inviter_link_dies` (8), via `box_fixture_run` with the inviting member set inactive: `inviter-inactive`. Live: the orphan gauge counts `acc-<hex>` while the throwaway inviting member's account is off.
  9. `test_promotion_keeps_user_id` (9): Task 5's sandbox test, run again here and cited.
  10. `test_guest_ci_defaults_then_deny` (10):
      - `acc-<hex>` pushes a public repo with a `runs-on: tiny` workflow, and the job runs;
      - **Chris** merges a `deny:` entry `{name: acc-<hex>, id: <id>}`, and the next job holds as `denied` within one sweep (about 5 min after the merge);
      - **Chris** merges its removal.
  11. `test_hand_entry_needs_no_service` and `test_signin_needs_no_galaxy` (11): Task 15's hand entry stands as the proof. No authenticator config in the realm names a galaxy host, and `hand-<hex>` signs in to the account console.
  - (12) is Task 20.
  - Teardown: deactivate through the service, then `erase_test_guest`.
- [ ] **Step 4: Run them:** `taskset -c 0-3 nix develop ~/Repos/realliance-sso-invites --command pytest tests/invites --hosts=ssh://root@23.163.0.48 -q`, then again with `-m slow`. Expected: all pass, and `main` never froze. Ledger the results and the Resend count.
- [ ] **Step 5: Commit the tests, push and open the PR.** **Chris:** merges.

### Task 20: The erasure tool, its rehearsal and a real erasure

**Files:** create sso `scripts/erase_ledger_guest.py` and `tests/ledger/test_erase.py`; modify sso `flake.nix` (add `git-filter-repo`).

**Interfaces:**
- `scripts/erase_ledger_guest.py LINK_ID`: a `git filter-repo --file-info-callback` body. For a path equal to `ledger.yaml`, it drops the link with that id and re-marshals; it returns every other file, and every `ledger.yaml` blob without that id, unchanged.
- The live procedure is the spec's order with D9's additions.

- [ ] **Step 1: Write the failing tests** in `tests/ledger/test_erase.py`, on a fixture `sso-people`-shaped repo:
  - `test_link_gone_from_all_history`: `git log --all -S <link-id>` finds nothing after the rewrite;
  - `test_only_ledger_blobs_change`: through filter-repo's `commit-map`, the tree of each commit, without `ledger.yaml`, is identical before and after;
  - `test_commits_before_the_link_keep_their_ids`, the anchor included;
  - `test_rewritten_history_recovers_with_reviewed_range`: Task 2b's `walk` on the rewritten repo, with an operator head naming the old last good and the rewritten tip, gives `valid` and an empty seen-heads list.
- [ ] **Step 2: Run; expect FAIL. Step 3: Implement. Step 4: Run; expect PASS.** Commit on branch `invites-erase`, push, PR; **Chris:** merges (scripts and tests only).
- [ ] **Step 5: The rehearsal on a throwaway copy.** `git clone --mirror` `sso-people` into `$SCRATCH/erase-rehearsal` (mode 0700), run the callback for `hand-<hex>`'s link id, and run Step 1's checks against it with a `python -I` script that prints only counts and short commit ids. Expected: the link no longer appears, other files keep their content, and the walk recovers. Delete the copy. Push nothing.
- [ ] **Step 6: STOP—ask Chris (the real erasure of `hand-<hex>`).** In order:
  1. The guest is already deactivated (Task 15), and `sso/apply` is success on the current tip `<old-tip>`.
  2. The agent, in one SSH session holding the box's lock: the Keycloak delete, `tofu state rm` of both addresses, `sso-ledger forget hand-<hex>`.
  3. **Chris:** the rewrite on a fresh mirror clone, then an operator-key head carrying `Reviewed-Range: <old-tip>..<rewritten-tip>`, then `git push --force-with-lease=main:<old-tip>`.
  4. After the box's next run shows `sso/apply` success on the new head: re-clone `/var/lib/sso-people` on the box under the lock (a full clone, `fetch.fsckObjects=true`), and both operators' clones; rebase any open PR.

  Expected: no `hand-<hex>` in Keycloak, in `tofu state list` or in `state.json`, and `git log --all -S hand-` in a fresh clone finds nothing. Ledger the timings.

### Task 21: Runbooks and the first real invite

**Files:**
- Create: luma-homeops `docs/sso-invites.md`.
- Modify: luma-homeops `docs/forgejo.md` (Who gets in: `/guests`; guest offboarding; the quota step when a member joins) and `CLAUDE.md` (a topology row); sso `docs/runbook.md`, `README.md` and `WISHLIST.md` (Member invites: done, with links); forge `README.md` (New org: `quota.sh member <org>` on path 1); sso-people `README.md`.

- [ ] **Step 1: Write `docs/sso-invites.md`** (vale-clean), covering:
  - the shape, and the costs Chris accepted with the ledger in `sso-people`;
  - **merging `sso-people`, the operator rules** (the spec's Merging rule, and Task 2's hand-offs):
    - merge PRs locally as a signed squash or a fast-forward of commits you signed; never GitHub's merge, squash or rebase button;
    - never "Update branch" on an `sso-people` PR, and never a PR base other than `main`;
    - pull with `--ff-only` or `--rebase` (`git config pull.ff only` in the clone); a local merge of unsigned work freezes until a `Reviewed-Range`;
    - keep full clones; the box's checkout freezes with `shallow-repository` otherwise;
    - dismiss stale approvals when a PR branch gets new pushes, and no auto-merge: the deploy key can push to any branch;
    - after pushing a recovery, wait for `sso/apply` green on that head before merging anything;
    - never push on top of a frozen head except a recovery;
  - modes, switching them, and the changeover anchor;
  - reading `walk.json`, `content.json` and the two statuses, the seen-heads list included;
  - **a freeze:** find the invalid commit from `sso/apply`, revert it, then push an operator-key `Reviewed-Range` commit directly, with both shas in full and the box's current last good;
  - **a seen-head freeze** (`not-first-parent-descendant@<sha>`): the recovery's tip must hold every seen head still in `main`'s history, as the status lists them; a seen head gone from history needs no review;
  - **a refused ledger:** fix or revert `ledger.yaml` with an operator-key commit;
  - **the emergency override**, including during a freeze (the console change sticks; the recovery commit carries the entry);
  - **guest offboarding:** the ledger deactivation, Prohibit login until forge-bot's `access` runs in `apply`, `allow_bulk_until` for more than 3, the Pages takedown (Chris's answer), and the `deny:` entry;
  - **a member leaving**, the orphan alert, and its promoted-guest caveat (D18);
  - **promotion,** with `quota.sh member <name>` on path 1;
  - **erasure:** Task 20's procedure, where the rewrite and the force-push are Chris's;
  - **a box rebuild:** the re-anchor after the anchor commit, the seed, the empty seen-heads list, and the clock reset;
  - **rotating** the ledger key (overlap two fingerprints in `ledger_signing_fingerprints`), the service's deploy key and both status tokens, with the ledgered dates;
  - each alert's response.
- [ ] **Step 2: Vale every changed doc**; expected no errors. Commit each repo signed. luma-homeops docs are routine; **Chris:** merges the sso PR, and the sso-people PR locally as a signed squash.
- [ ] **Step 3: STOP—ask Chris (the first real invite).** A member of his choosing makes a link for a real person. Watch: both statuses on the new head, the mail inside 10 min, the guest's first forge sign-in, and no alert. Ledger the link id and the minutes only.
