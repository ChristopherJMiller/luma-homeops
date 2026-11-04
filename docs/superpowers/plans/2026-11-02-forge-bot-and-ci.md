# forge-bot and minimal Forgejo CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Moved (2026-11-04):** since 2026-11-04 the repo and image live at `realliance-ops/forge-bot` (plan `2026-11-04-bots-to-realliance-ops.md`). The Go module path stays `git.realliance.net/realliance/forge-bot` until the next reviewed release. CI pushes images as `ops-ci` (team `realliance-ops/ci`, packages only) with the `realliance-ops` org secret `REGISTRY_TOKEN`. Steps before Task 16 keep the paths they ran with.

**Goal:** forge-bot, a stateless Go reconciler, keeps git.realliance.net's access, admin status, non-Owners teams and profiles in step with Realliance SSO every ~5 min. It writes avatars back to SSO `picture`, it fails safe, and it is built by Forgejo Actions on one unprivileged in-cluster runner.

**Architecture:** Five repos move together.
- `realliance/sso` gains the master-realm `forge-bot` client, the `picture` profile attribute and the acceptance tests (`tests/forge/test_forge_bot.py`, `test_forge_ci.py`).
- The inventory moves to the Owners-only org `realliance-ops`, which declares `realliance/forge-bot`, team `ci`, `teams.yaml` and `forge-bot.yaml`.
- `luma-homeops` gains the runner image (`images/forgejo-runner/`), `[actions]` in the Forgejo release, and two Argo apps: `forgejo-runner` and `forge-bot`.
- `realliance/forge-bot` (new, public) holds the Go code and its `.forgejo/workflows/ci.yaml`.

**Tech Stack:**
- Forgejo 15.0.11 LTS (chart 17.3.2), forgejo-runner v13.2.1 in host mode.
- Go 1.26 with stdlib `net/http`, `log/slog`, `github.com/ProtonMail/go-crypto/openpgp`, `github.com/prometheus/client_golang` and `go.yaml.in/yaml/v3`; images built with ko.
- Keycloak 26.8 admin REST, and OpenTofu with `svalabs/forgejo` 1.7.0 and `keycloak/keycloak`.
- Ansible, Argo CD, SealedSecrets, kube-prometheus-stack (PrometheusRule, ServiceMonitor, Alertmanager), and pytest.

**Spec:** `docs/superpowers/specs/2026-11-02-forge-bot-and-ci-design.md` (commit 767764c3). Read it first; this plan argues from it.

## Global Constraints

**Repos and how changes land**

| Repo (local path) | Visibility | Commits | Lands by |
|---|---|---|---|
| `luma-homeops` (`~/Repos/luma-homeops`) | public | unsigned, always `nix develop --command git commit …` (pre-commit: yamlfmt, end-of-file, kustomize build) | direct push to `main`; Chris OKs pushes once at execution start |
| `realliance/sso` (`~/Repos/realliance-sso`, GitHub) | public | `git commit -S` | branch → `gh pr create --fill` → `gh pr merge --squash --delete-branch` **only after Chris says merge**. The SSO box applies `main` within ~5 min |
| `realliance/sso-secrets` (`~/Repos/realliance-sso-secrets`) | private, git-crypt | `git commit -S` | push `main` |
| inventory, `realliance/forge` → `realliance-ops/forge` (`~/Repos/realliance-forge`) | private | `git commit -S` | branch → PR on git.realliance.net → **Chris** merges fast-forward (`fj -H git.realliance.net pr merge <org>/forge#N -d`) → operator `tofu apply` (below) |
| `realliance/forge-bot` (`~/Repos/forge-bot`, created in Task 4) | public | `git commit -S` | branch `v1` → PR → **Chris** merges fast-forward |

- Inventory apply, from `~/Repos/realliance-forge`: `set -a; . ~/Repos/realliance-sso-secrets/b2/secrets.env; . ~/Repos/realliance-sso-secrets/forge/secrets.env; set +a; export FORGEJO_API_TOKEN=$FORGEJO_ADMIN_TOKEN; nix develop ~/Repos/realliance-sso --command tofu plan` (then `apply` after Chris's OK).
- Every shell that signs or uses ssh/gpg exports `DISPLAY=:10.0 XAUTHORITY=$HOME/.Xauthority` (pinentry). Signing and pushing to realliance repos need Chris at his display. The plan says "**Chris:**" where he must act himself.
- Never `--no-verify` or `--no-gpg-sign` (S6).

**Shell and cluster**

- Cluster access: `export KUBECONFIG=/tmp/galaxy-kubeconfig`. If it is rejected, refresh it from Talos (CLAUDE.md "Quick commands"). `kubectl`, `kubeseal` and `amtool` come from `cd ~/Repos/luma-homeops && nix develop --command …`; pytest and tofu from `cd ~/Repos/realliance-sso && nix develop --command …`.
- Go, always pinned to 4 threads: `GO="taskset -c 0-3 nix develop $HOME/Repos/luma-homeops --command env GOMAXPROCS=4 go"`, then `$GO test -race -p=4 ./...`. That shell has go1.26.7 and a C compiler, which `-race` needs. Never run docker or kind locally; CI builds the images.
- CLAUDE.md S1–S9 apply:
  - read live state before any write;
  - GitOps only (no `kubectl apply`, except reads, `rollout restart` and `exec`);
  - use `Monitor` for anything longer than 60 s, never sleep-loops;
  - this plan touches no router, Talos, Ceph or DNS config.

**Secrets**

- Never print, echo, `cat`, `Read` or diff a secret; never put one in argv, assertion messages, logs, PR text or the ledger. Write them with redirects from `sso-secrets`, `umask 077` scratch files or stdin. Use `git diff --no-textconv` in git-crypt repos.
- Never open `*.secret.yaml` or `nodes/*`. Static tests skip `*.secret.yaml`.
- New cluster secret: write `cluster/<app>/<name>.secret.yaml` (one Secret per file) with a script that reads the canonical value from `sso-secrets`, then `KUBECONFIG=/tmp/galaxy-kubeconfig nix develop --command ./sign.sh` to produce the SealedSecret `<name>.yaml`. Commit both files.
- Canonical copies:
  - `forge/secrets.env`: `FORGEJO_ADMIN_TOKEN`, `FORGE_BOT_FORGEJO_TOKEN`, `FORGE_CI_PACKAGE_TOKEN`;
  - `forge/runner.env`: `RUNNER_FORGE_BOT_SECRET`;
  - `host/secrets.yml`: `forge_bot_client_secret`.

**Names and visibility**

- Hidden org, team or group names never appear in `luma-homeops`, `realliance/sso`, `realliance/forge-bot`, metric labels or the ledger (this repo is public).
- Names:
  - namespaces and Argo apps `forge-bot` and `forgejo-runner`;
  - org `realliance-ops`;
  - local Forgejo accounts `forge-bot` (site admin) and `forge-ci` (not admin);
  - team `realliance/ci`;
  - Keycloak master client `forge-bot`;
  - Go module `git.realliance.net/realliance/forge-bot`;
  - image `git.realliance.net/realliance-ops/forge-bot:sha-<7>@sha256:…` (`realliance/forge-bot` until 2026-11-04).

**Versions**

- Forgejo stays on 15.0.11 LTS (chart 17.3.2); forgejo-runner is **v13.2.1**.
- Every image is pinned `tag@sha256:digest` and is at least 2 weeks old; Go dependencies also at least 2 weeks old.
- Actions are pinned by 40-hex SHA. `DEFAULT_ACTIONS_URL: https://github.com`.

**Pods**

- Pod Security `baseline`; flannel does not enforce NetworkPolicy. The forge-bot NetworkPolicy is documented intent, and the runner gets none.
- Pods: non-root, `allowPrivilegeEscalation: false`, all capabilities dropped, `RuntimeDefault` seccomp, `automountServiceAccountToken: false`, `enableServiceLinks: false`. No hostPath, Docker socket, DinD or privileged pods.

**forge-bot behaviour (spec, verbatim values)**

- Cycle at start, then every 5 min ± 30 s; cycles never overlap.
- Every job (`access`, `admin`, `teams`, `profile`, `avatar`) defaults to `dry-run`. Apply order: access, admin, teams, profile, avatar.
- Breaker per cycle:
  - losses ≤ 3 people and ≤ ⅓ of those with access (with the floor in "Decisions");
  - grants ≤ 10;
  - profile + avatar ≤ 25.
- `allow_bulk_until`: RFC 3339, ≤ 24 h ahead, only from an Owner-signed inventory head.
- No writes to local accounts or Owners teams; Owners are never auto-prohibited or demoted.
- The Keycloak package's only write is `SetPicture`.

**Process**

- Ledger: `docs/superpowers/plans/2026-11-02-forge-bot-and-ci.ledger.md` (SDD). Record rulings and drill results there; no secrets or hidden names.
- STOP steps: the controller asks Chris (AskUserQuestion) and waits. Every irreversible, outward or security-sensitive step is marked **STOP — ask Chris**.

## Decisions this plan makes (not in the spec, or reconciling it)

1. **Loss-breaker floor:** at most `min(3, max(1, ⌊N/3⌋))` people per cycle, where N is the number of realliance-source accounts not prohibited before the cycle. Without the floor, with N ≤ 2, no single offboarding could ever apply.
2. **Breaker counts people, not changes,** in each category (loss / grant / profile+avatar). An Owner's suppressed loss is not counted.
3. **`ReallianceSSOUnreadable` uses `for: 20m`.** The spec says "failing for 30 min", but Success 10 needs it to fire "in ≤ 30 min": the first failed cycle takes ≤ 5.5 min, plus 20 min, plus up to 1.5 min of scrape and eval.
4. **Self-check pre-images.**
   - forge-bot compares its *own* writes with the pre-image `SetPicture` read just before its PUT.
   - It compares every other forge-bot-client event with its previous snapshot.
   - The spec's previous-snapshot-only rule would page 24/7 whenever sso-people changed someone's email between the snapshot and the write.
   - Nothing persists across restarts: the first complete read after a start is the baseline, and earlier events are not examined. Success 7's operator test covers that gap.
5. **Metrics added to the spec's list** (no name labels): `forge_bot_owner_needs_action`, `forge_bot_unexpected_sso_writes_total` and `forge_bot_inventory_ok{check="signature|teams|bot_config"}`. The new weekday alert `ForgeBotInventoryInvalid` is the "alert" the spec asks for when `teams.yaml` is skipped.
6. **Success semantics.**
   - `forge_bot_cycle_last_success_timestamp` is the end of the last cycle whose three reads (sso, forgejo, inventory) all completed, so `ForgeBotStale` also covers persistent read failures.
   - `/healthz` uses a separate internal "last completed cycle" (any outcome), so an SSO outage does not restart the pod.
7. **Commit binding.** forge-bot rebuilds the raw commit from Forgejo's `payload` + `signature` and checks that its SHA-1 equals the head sha before verifying the signature. Files are then read with `?ref=<sha>`. Forgejo is not otherwise distrusted: a compromised Forgejo already owns the state forge-bot reconciles.
8. **Startup source check.** forge-bot refuses to start (exits, so `ForgeBotStale` fires) unless `source_id` (1) lists ≥ 1 account and every one of those accounts has a UUID `login_name` (an OIDC subject).
9. **Secrets are files** (`/etc/forge-bot/secrets/*`), not env vars.
10. **Runner image:** a Dockerfile, **no Nix** in v1 (YAGNI; forge-bot needs Go + ko only). It includes `gcc`/`libc6-dev` (for `go test -race`) and Node 24 (for JS actions).
11. **Registry credentials stay per job.** CI sets `DOCKER_CONFIG` to a per-job temp dir, so `ko login` never writes to the shared work volume.
12. **Paging tests are opt-in.** Tests that fire a 24/7 alert are marked `pages` and skipped unless `FORGE_PAGES_OK=1` (tell Connor first). Operator drills are marked `drill` and run only with `FORGE_DRILL=<name>`.
13. **More tests need rewriting than the spec lists.** Beyond the spec's three, `test_admin_group_makes_site_admin`, `_maintainer()` in `test_forge_inventory.py` and `test_offboarding_blocks_web_token_and_ssh_and_is_reversible` also depend on sign-in syncing (the old manual two-step would race forge-bot). They are rewritten too.
14. **`realliance-ops` is `visibility = "private"`.** `forgejo-admin` stays in its Owners team, because the creator of an org is an owner.
15. **Runner labels live in the sealed `config.yml`,** so "no `ubuntu-latest`" is checked through the runners API and Success 15's behaviour, not by static parsing.
16. **Success 16's expected scope follows B0.** With a repo-specific token, `realliance/<other>` must be refused. With the fallback org token, `realliance/*` is allowed by design and only other owners are refused.
17. **Transitional double writer.** In cutover step 4, the login-source map and forge-bot both write (spec cutover step 4). This is bounded to the first zero-change cycle, after which Task 19 clears the map. It is the only exception to the "two writers" anti-goal.

## Review Focus

1. **Tiny population.** With 1–2 people with access, removing one person must still prohibit them within a cycle (not trip the breaker). Test: Task 13 `TestGuardSingleLossAlwaysAllowed`.
2. **Profile churn from formatting.** A missing or whitespace last name, or an email differing only in case, must produce no write, so the steady state stays at zero writes. Test: Task 12 `TestProfileFullNameNoLastName`, `TestProfileEmailCaseInsensitive`.
3. **User without `attributes` in Keycloak** (fresh users have none). `SetPicture` must add `attributes.picture` and not panic or drop fields. Test: Task 10 `TestSetPictureCreatesAttributes`.
4. **Registry credential left on the shared runner volume** by `ko login` (the default `$HOME/.docker`). Expected: none survives a CI job. Test: Task 16 `test_no_registry_credential_left_on_runner`.
5. **`teams.yaml` with a group written as a path (`/realliance`), a duplicate key or an `owners` team in any case.** Expected: the whole teams job is skipped with `ForgeBotInventoryInvalid`; never a partial map. Test: Task 9 `TestParseTeamsRejects`.

## Success criteria → tests

| Success | Test (task) |
|---|---|
| 1 offboarding | `test_offboarding_within_two_cycles`, `test_removed_from_access_group_loses_access` (17) |
| 2 admin | `test_admin_follows_group_without_signin` (18) |
| 3 teams | `test_team_follows_groups_without_signin`, `test_owners_and_unlisted_teams_untouched` (18) |
| 4 sign-in writes nothing | `test_signin_writes_nothing` (19) |
| 5 profile | `test_profile_follows_sso`, `test_unverified_email_never_synced` (17) |
| 6 avatar | `test_avatar_picture_set_and_served` (17) |
| 7 steady state | `test_steady_state_zero_writes` (drill), `test_forge_bot_admin_events_change_only_picture` (18) |
| 8 breaker | `test_breaker_holds_bulk_loss` + drill `bulk` (17) |
| 9 lockout | `test_lockout_not_prohibited` (17) |
| 10 SSO outage | drill `sso-outage` (18) |
| 11 untrusted head | drill `untrusted` (18) |
| 12 foreign write | drill `foreign-write` (18) |
| 13 protected | `test_local_accounts_never_prohibited`, `test_owner_never_auto_prohibited` (17) |
| 14 CI | `test_main_push_builds_and_galaxy_runs_pin` (16) |
| 15 allowlist | `test_allowlist_jobs_wait` (7) |
| 16 package scope | `test_package_token_scope` (5) |
| 17 hardening | static tests (7, 16), `test_member_cannot_see_inventory` (3), `test_member_cannot_push_forge_bot_main` (4), `test_runner_pod_holds_no_bot_or_admin_secrets` (7) |

## Shared test helpers (`realliance/sso`)

Later tasks consume these by name, and the task in brackets adds each one. Existing helpers (`forge_api`, `forge_login`, `forge_token`, `set_groups`, `set_email`, `b2_names`, `lan_only`, `LUMA`, `FORGE_SECRETS`, `_load_env`, `admin_token`, `get/put/post`, `user_factory`, `forge_user`, `browser_login`, `password_grant`, `decode`) keep their signatures.

- `conftest.CLIENT_SECRETS["forge-bot"] = "forge_bot_client_secret"` [1].
- `forge_helpers` constants [3]: `INVENTORY = "realliance-ops/forge"`, `FORGE_BOT_REPO = "realliance/forge-bot"`, `CYCLE_WAIT = 2 * 300 + 60`.
- `forge_helpers.wait_for(pred: Callable[[], bool], timeout: float = CYCLE_WAIT, every: float = 10) -> bool` [3]: True as soon as `pred()` is true, False at timeout. It never raises on a falsy result.
- `forge_helpers.team_members(org: str, team: str) -> set[str]` [3]: paged; replaces `_team_members` in `test_forge_access.py`.
- `forge_helpers.kc_groups_named(name: str) -> list[str]` [4]: paths of every Keycloak group (any depth) whose name equals `name`.
- `forge_helpers.oci_push(path: str, tag: str, user: str, token: str) -> int` [5]:
  - gets a `/v2/token` with basic auth (credentials in the request body or headers only);
  - uploads a minimal config blob and one tiny gzip layer, then PUTs an OCI manifest;
  - returns the manifest PUT status, or the first 401/403 met on the way.
- `forge_helpers.anonymous_manifest(path: str, ref: str) -> requests.Response` [5]: anonymous pull token, then a manifest GET.
- `PACKAGE_TOKEN_REPO_SCOPED: bool` [5]: set from B0's result.
- `forge_helpers.kube(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess` [7]: runs `kubectl` with `KUBECONFIG` (default `/tmp/galaxy-kubeconfig`) and captures output. Also `galaxy_only = pytest.mark.skipif(<no kubectl or cluster unreachable>)`.
- `forge_helpers.manifests(d: Path) -> list[dict]` [7]: every YAML document in `d/*.yaml`, **skipping `*.secret.yaml`** (decrypted on this machine).
- `forge_helpers.push_probe_workflow(repo: str, runs_on: str, script: str, branch: str | None = None) -> tuple[str, str]` [7]:
  - writes `.forgejo/workflows/probe.yaml` (`on: push` for branch `ci-probe-*`, one job, `runs-on: <runs_on>`, `run: <script>`) through the contents API as the admin;
  - uses a new branch `ci-probe-<hex>`, or appends a commit to `branch`;
  - returns `(branch, commit_sha)`. Also `delete_branch(repo, branch)`.
- `forge_helpers.runs_for(repo: str, sha: str) -> list[dict]` [7]: `GET /repos/{repo}/actions/runs?head_sha=`.
- `forge_helpers.prom_query(expr: str) -> list[dict]` [16]: `kube("get", "--raw", "/api/v1/namespaces/prometheus/services/prometheus-kube-prometheus-prometheus:9090/proxy/api/v1/query?query=<urlencoded>")` → `data.result`. Also `firing(alertname) -> bool` via `ALERTS{alertstate="firing"}`.
- `forge_helpers.next_cycle(timeout=CYCLE_WAIT) -> float` [19]: waits until `forge_bot_cycle_last_success_timestamp` increases and returns it.
- `forge_helpers.kc_user(u) -> dict`, `kc_update(u, *, attributes: dict | None = None, **fields) -> None` (attributes merged, not replaced), `set_enabled(u, enabled: bool) -> None` (moved from `test_forge_access._set_kc_enabled`), and `bad_passwords(u, n: int) -> None` (n × `browser_login(u.name, <wrong>)`) [17].
- `conftest.module_forge_users` [17]: module-scoped twin of `forge_user`, with the same teardown order (Forgejo purge before the Keycloak delete). Extract the shared body into `_make_forge_user` / `_purge`.
- `pytest.ini` markers: `slow` [7], `pages` [17], `drill` [17]. `tests/conftest.py` gets a `pytest_collection_modifyitems` hook that skips `pages` without `FORGE_PAGES_OK=1` and `drill("<name>")` unless `FORGE_DRILL` lists that name [17]. Also `conftest.drill_enabled(name: str) -> bool` (comma-separated `FORGE_DRILL`) [17].
- `kubectl` is added to the `realliance/sso` `flake.nix` devShell packages [7].

---

### Task 1: SSO side — `forge-bot` master client, its grants, `picture` in the profile (A1)

**Files:**
- Modify: `~/Repos/realliance-sso/host/roles/master_realm/tasks/main.yml` (the "Automation clients" loop)
- Modify: `realm/automation.tf`, `realm/user_profile.tf`, `realm/github.tf` (line 2 comment: "forge-sync" → "forge-bot"), `README.md` (Lockdown)
- Modify: `tests/conftest.py` (`CLIENT_SECRETS`), `tests/host/test_master_realm.py`, `tests/realm/test_profile.py`
- Modify (private): `~/Repos/realliance-sso-secrets/host/secrets.yml` (+ `forge_bot_client_secret`)

**Interfaces:**
- Produces:
  - master client `forge-bot` (confidential, service account only), holding exactly `view-users`, `query-users`, `query-groups`, `manage-users` and `view-events` on `realliance-realm`;
  - realm attribute `picture` (view: admin, user; edit: admin);
  - `admin_token("forge-bot")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/host/test_master_realm.py
FORGE_BOT_ROLES = {"view-users", "query-users", "query-groups", "manage-users", "view-events"}

def test_forge_bot_holds_exactly_its_roles():
    t = admin_token("tofu-apply")
    c = get("/admin/realms/master/clients", t, params={"clientId": "forge-bot"}).json()[0]
    sa = get(f"/admin/realms/master/clients/{c['id']}/service-account-user", t).json()
    mgmt = get("/admin/realms/master/clients", t, params={"clientId": "realliance-realm"}).json()[0]
    roles = get(f"/admin/realms/master/users/{sa['id']}/role-mappings/clients/{mgmt['id']}", t).json()
    assert {r["name"] for r in roles} == FORGE_BOT_ROLES
    assert "admin" not in {r["name"] for r in get(f"/admin/realms/master/users/{sa['id']}/role-mappings/realm", t).json()}

def test_forge_bot_reads_but_cannot_administer():
    t = admin_token("forge-bot")
    assert get("/admin/realms/realliance/users", t, params={"max": 1}).status_code == 200
    assert get("/admin/realms/realliance/admin-events", t, params={"max": 1}).status_code == 200
    assert post("/admin/realms/realliance/clients", t, {"clientId": "should-not-exist"}).status_code == 403
    assert post("/admin/realms", t, {"realm": "should-not-exist"}).status_code == 403

# tests/realm/test_profile.py
def test_picture_viewable_admin_edit_only():
    a = _attrs()["picture"]
    assert a["permissions"]["edit"] == ["admin"] and "user" in a["permissions"]["view"] and "required" not in a

def test_profile_scope_keeps_picture_mapper():
    scope = next(s for s in realm_get("/client-scopes") if s["name"] == "profile")
    assert any(m["config"].get("claim.name") == "picture" for m in scope["protocolMappers"])

def test_picture_in_id_token(acceptance_clients, user_factory):
    u = user_factory(groups=["/apps/acceptance-tests"])
    # set attributes.picture = "https://git.realliance.net/<name>.png" with admin_token() (read-modify-write)
    claims = decode(password_grant("realliance", "acceptance-tests", u.name, u.pw).json()["id_token"])
    assert claims["picture"] == f"https://git.realliance.net/{u.name}.png"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd ~/Repos/realliance-sso && nix develop --command pytest tests/host/test_master_realm.py tests/realm/test_profile.py -k "forge_bot or picture" -v --hosts=ssh://root@23.163.0.48`
Expected: FAIL (`IndexError` for client `forge-bot`; `KeyError: 'picture'`)

- [ ] **Step 3: Implement**
  - Ansible: add `- { client_id: forge-bot, secret_var: forge_bot_client_secret }` to the "Automation clients" loop.
  - `realm/automation.tf`: add `data "keycloak_openid_client" "forge_bot"` (master, `forge-bot`) and `resource "keycloak_openid_client_service_account_role" "forge_bot"`, with `for_each` over the five roles, shaped exactly like `tofu_plan_view`. Comment it: the client lives in master, so `test_no_user_holds_a_management_role` stays true; `manage-users` is accepted full user-admin power (the mitigations are in luma-homeops `docs/forgejo.md`).
  - `realm/user_profile.tf`:
    - add an `attribute` `picture`: `display_name = "Avatar URL"`, view `["admin","user"]`, edit `["admin"]`, `length` max 255. Comment it: written only by forge-bot, the Forgejo avatar URL.
    - reword the pronouns comment: "forge-bot copies it into Forgejo's pronouns field".
  - README "Lockdown": a bullet listing the master automation clients (`tofu-apply` admin, `tofu-plan` view, `forge-bot` view/query users + groups, `manage-users`, `view-events` on `realliance`) and saying forge-bot's only write is `picture`.
  - conftest: `CLIENT_SECRETS["forge-bot"] = "forge_bot_client_secret"`.

- [ ] **Step 4: Generate the secret (sso-secrets, signed)**

```bash
cd ~/Repos/realliance-sso-secrets
printf 'forge_bot_client_secret: "%s"\n' "$(openssl rand -hex 32)" >> host/secrets.yml
git add host/secrets.yml && git commit -S -m "host: forge-bot client secret" && git push
```
Expected: `git check-attr filter host/secrets.yml` → `git-crypt`

- [ ] **Step 5: STOP — ask Chris** to place the secrets on the box. Ansible fails on an undefined `forge_bot_client_secret`, so this comes before the merge. Command: `scp host/secrets.yml root@sso.realliance.net:/etc/realliance-sso/secrets.yml && ssh root@sso.realliance.net chmod 0600 /etc/realliance-sso/secrets.yml`.

- [ ] **Step 6: Commit, PR, then STOP — ask Chris to approve the merge** (this grants `manage-users`)

```bash
cd ~/Repos/realliance-sso && git switch -c forge-bot-client
git add host/roles/master_realm realm tests README.md && git commit -S -m "sso: forge-bot master client (realliance read + manage-users), picture attribute"
gh pr create --fill   # merge only on Chris's word: gh pr merge --squash --delete-branch
```

- [ ] **Step 7: After the box applies (~5 min, Monitor `journalctl -u sso-pull` on the box), run the tests**

Run: `nix develop --command pytest tests/host/test_master_realm.py tests/realm -v --hosts=ssh://root@23.163.0.48`
Expected: PASS, including `test_no_user_holds_a_management_role`

---

### Task 2: Forgejo local accounts `forge-bot` and `forge-ci`, and forge-bot's token (A2)

**Files:**
- Create: `~/Repos/realliance-sso/tests/forge/test_forge_bot.py` (first tests)
- Modify (private): `~/Repos/realliance-sso-secrets/forge/secrets.env` (+ `FORGE_BOT_FORGEJO_TOKEN`)

**Interfaces:**
- Produces: local accounts `forge-bot` (site admin, email `forge-bot@realliance.net`) and `forge-ci` (not admin, `forge-ci@realliance.net`); `FORGE_BOT_FORGEJO_TOKEN` with scopes `read:admin,write:admin,write:organization,read:repository,read:user`.

- [ ] **Step 1: Write the failing tests**

```python
def test_bot_accounts_are_local():
    for login, admin in (("forge-bot", True), ("forge-ci", False)):
        a = forge_api("GET", f"/api/v1/users/{login}").json()
        assert (a["source_id"], a["is_admin"], a["prohibit_login"]) == (0, admin, False)

def test_forge_bot_token_scopes():
    tok = _load_env(FORGE_SECRETS)["FORGE_BOT_FORGEJO_TOKEN"]
    assert forge_api("GET", "/api/v1/admin/users", token=tok, params={"limit": 1}).status_code == 200
    assert forge_api("POST", "/api/v1/user/repos", token=tok, json={"name": "nope"}).status_code == 403
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command pytest tests/forge/test_forge_bot.py -v`
Expected: FAIL (404 for `forge-bot`)

- [ ] **Step 3: Create the accounts on the pod** (S1: first run `forgejo admin user list`)

```bash
kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin user create --username forge-bot --email forge-bot@realliance.net --admin --random-password --must-change-password=false >/dev/null
kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin user create --username forge-ci --email forge-ci@realliance.net --random-password --must-change-password=false >/dev/null
```

- [ ] **Step 4: STOP — ask Chris, then create the token** straight into sso-secrets

```bash
cd ~/Repos/realliance-sso-secrets
kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin user generate-access-token --username forge-bot \
  --token-name forge-bot --scopes "read:admin,write:admin,write:organization,read:repository,read:user" --raw \
  | { read -r t; printf 'FORGE_BOT_FORGEJO_TOKEN=%s\n' "$t" >> forge/secrets.env; }
git add forge/secrets.env && git commit -S -m "forge: forge-bot API token" && git push
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `nix develop --command pytest tests/forge/test_forge_bot.py -v`
Expected: PASS

- [ ] **Step 6: Commit** (in `realliance/sso`, signed, branch `forge-bot-tests`, PR; merge on Chris's word)

---

### Task 3: Inventory moves to the Owners-only org `realliance-ops` (A2)

**Files (inventory, `~/Repos/realliance-forge`):**
- Modify: `orgs.tf`, `teams.tf`, `repos.tf`, `README.md`

**Files (`realliance/sso`):**
- Modify: `tests/forge_helpers.py` (`INVENTORY`, `FORGE_BOT_REPO`, `CYCLE_WAIT`, `wait_for`, `team_members`)
- Modify: `tests/forge/test_forge_inventory.py`, `tests/forge/test_forge_access.py` (use `team_members`)

**Files (`luma-homeops`):**
- Modify: `docs/forgejo.md` (path `realliance-ops/forge` everywhere it names the inventory)

**Interfaces:**
- Produces:
  - org `realliance-ops` (private), team Owners only;
  - repo `realliance-ops/forge` at tofu addresses `forgejo_repository.inventory` and `forgejo_branch_protection.inventory_main`;
  - `forgejo_team.ops_owners`;
  - `local.ops_owners`.

- [ ] **Step 1: Gate — Connor's account.** Run `forge_api("GET", "/api/v1/users/connorjc")`. If it returns 404, use `local.ops_owners = ["chris"]` and ledger "connorjc added after his first sign-in" (a follow-up PR adds him and his GPG key to forge-bot's keyring).

- [ ] **Step 2: Write the failing tests** (`test_forge_inventory.py`; repoint every `realliance/forge` to `INVENTORY`)

```python
def test_ops_org_owners_only():
    assert forge_api("GET", "/api/v1/orgs/realliance-ops").json()["visibility"] == "private"
    teams = forge_api("GET", "/api/v1/orgs/realliance-ops/teams").json()
    assert [t["name"] for t in teams] == ["Owners"]
    owners = team_members("realliance-ops", "Owners")
    assert "chris" in owners and owners <= {"chris", "connorjc", "forgejo-admin"}

def test_forge_repo_private_and_protected():   # existing asserts, now on INVENTORY
    r = forge_api("GET", f"/api/v1/repos/{INVENTORY}").json()
    assert r["full_name"] == INVENTORY and r["private"] is True
    # branch protection asserts unchanged (enable_push False, signed, Owners merge/approval whitelists)

def test_member_cannot_see_inventory(forge_user):          # Success 17
    u = _member(forge_user)
    assert forge_api("GET", f"/api/v1/repos/{INVENTORY}", sudo=u.name).status_code == 404

def test_inventory_mirror_current():
    sha = forge_api("GET", f"/api/v1/repos/{INVENTORY}/branches/main").json()["commit"]["id"]
    gh = subprocess.run(["gh", "api", "repos/realliance/forge/commits/main", "--jq", ".sha"],
                        capture_output=True, text=True).stdout.strip()
    assert gh == sha
```

- [ ] **Step 3: Run them to verify they fail**

Run: `nix develop --command pytest tests/forge/test_forge_inventory.py -v`
Expected: FAIL (404 `realliance-ops`)

- [ ] **Step 4: PR 1 — create the org.** In `orgs.tf`, add `forgejo_organization.realliance_ops` (`name = "realliance-ops"`, `full_name = "Realliance Ops"`, `visibility = "private"`, description "Owners-only: the forge inventory", `prevent_destroy`). The provider cannot import orgs, so the org always comes from here. Commit signed on branch `ops-org`, push, and open the PR.
  - **Chris:** merges it fast-forward.
  - **STOP — ask Chris,** then apply. Expected plan: `1 to add, 0 to change, 0 to destroy`.

- [ ] **Step 5: STOP — ask Chris (repo transfer).** Transfer `realliance/forge` to `realliance-ops` as the admin: `forge_api("POST", "/api/v1/repos/realliance/forge/transfer", json={"new_owner": "realliance-ops"})` → 202. Then run `git -C ~/Repos/realliance-forge remote set-url origin ssh://git@git.realliance.net/realliance-ops/forge.git`. Do not plan or apply in between.

- [ ] **Step 6: PR 2 — state surgery** (branch `ops-move`, signed)
  - `teams.tf`:
    - add `local.ops_owners`;
    - `import { to = forgejo_team.ops_owners, id = "realliance-ops/Owners" }`, a resource shaped like `realliance_owners`, and `forgejo_team_member.ops_owners` (`for_each = toset(local.ops_owners)`);
    - fix the header comment (members/maintainers membership comes from forge-bot's `teams.yaml`).
  - `repos.tf`:
    - `removed { from = forgejo_repository.forge  lifecycle { destroy = false } }` and the same for `forgejo_branch_protection.forge_main`;
    - `import { to = forgejo_repository.inventory, id = "realliance-ops/forge" }` with the same arguments as the old block, but `owner = forgejo_organization.realliance_ops.name`;
    - `import { to = forgejo_branch_protection.inventory_main, id = "realliance-ops/forge/main" }` with the old block's arguments (whitelists `["Owners"]`, now the new org's).
  - README: the new path, Owners-only org, and the "What lives where" row "teams.yaml → forge-bot" (finished in Task 4).
  - Expected plan: imports of the team, repo and branch protection, two "removed from state" (not destroyed), any whitelist changes in place, and **0 to destroy**.
  - **Chris:** merges `realliance-ops/forge#N`. **STOP — ask Chris,** then apply.

- [ ] **Step 7: STOP — ask Chris (GitHub deploy key, outward). Re-create the push mirror.**
  - List the existing mirrors with `GET /api/v1/repos/realliance-ops/forge/push_mirrors` and delete the old one.
  - `POST …/push_mirrors` `{"remote_address": "git@github.com:realliance/forge.git", "use_ssh": true, "sync_on_commit": true, "interval": "8h0m0s"}`, keeping the response's `public_key`.
  - Swap the key: `gh repo deploy-key add <(printf %s "$pub") -R realliance/forge -w -t forgejo-ops`, then delete the old deploy key (`gh repo deploy-key list/delete`).
  - `POST …/push_mirrors-sync`.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `nix develop --command pytest tests/forge/test_forge_inventory.py tests/forge/test_forge_access.py -v`
Expected: PASS (`test_inventory_has_no_drift` too)

- [ ] **Step 9: Commit** the sso tests (branch `forge-inventory-ops`, PR, merge on Chris's word) and the luma-homeops docs (`docs: inventory now at realliance-ops/forge`, push).

---

### Task 4: Inventory declares `realliance/forge-bot`, team `ci`, `teams.yaml`, `forge-bot.yaml` (A2)

**Files:**
- Modify (inventory): `repos.tf`, `teams.tf`, `README.md`; Create: `teams.yaml`, `forge-bot.yaml`
- Modify (`realliance/sso`): `tests/forge_helpers.py` (`kc_groups_named`), `tests/forge/test_forge_inventory.py`
- Create (local clone): `~/Repos/forge-bot`

**Interfaces:**
- Consumes: `forge-ci` (Task 2), `realliance-ops/forge` (Task 3).
- Produces:
  - repo `realliance/forge-bot` (public, Actions and packages on, FF-only, protected `main`, `v*` tags protected for Owners);
  - team `realliance/ci` (`units_map = {"repo.packages" = "write"}`, member `forge-ci`);
  - `teams.yaml` and `forge-bot.yaml` at the inventory root.

- [ ] **Step 1: Read the live map (S1).** `kubectl -n forgejo exec $(kubectl -n forgejo get pod -l spilo-role=master,galaxy=acid-forgejo -o name) -c postgres -- psql -U postgres -d forgejo -Atc "select cfg::json->>'GroupTeamMap', cfg::json->>'GroupTeamMapRemoval', cfg::json->>'AdminGroup' from login_source where id = 1"`. Select only these fields: `cfg` also holds the client secret. Ledger the map (public names only; the map names only the public `realliance` org today).

- [ ] **Step 2: Write the failing tests**

```python
def test_forge_bot_repo_public_and_protected():
    r = forge_api("GET", f"/api/v1/repos/{FORGE_BOT_REPO}").json()
    assert (r["private"], r["has_actions"], r["has_packages"]) == (False, True, True)
    assert r["default_merge_style"] == "fast-forward-only"
    bp = forge_api("GET", f"/api/v1/repos/{FORGE_BOT_REPO}/branch_protections/main").json()
    assert bp["enable_push"] is False and bp["require_signed_commits"] is True
    assert bp["merge_whitelist_teams"] == ["Owners"] and bp["approvals_whitelist_teams"] == ["Owners"]
    tags = forge_api("GET", f"/api/v1/repos/{FORGE_BOT_REPO}/tag_protections").json()
    assert [(t["name_pattern"], t["whitelist_teams"]) for t in tags] == [("v*", ["Owners"])]

def test_member_cannot_push_forge_bot_main(forge_user):   # Success 17
    u = _member(forge_user)
    r = forge_api("POST", f"/api/v1/repos/{FORGE_BOT_REPO}/contents/probe.txt", sudo=u.name,
                  json={"content": "eA==", "message": "probe", "branch": "main"})
    assert r.status_code in (403, 422)

def test_ci_team_packages_only():
    t = next(t for t in forge_api("GET", "/api/v1/orgs/realliance/teams").json() if t["name"] == "ci")
    assert {k: v for k, v in t["units_map"].items() if v != "none"} == {"repo.packages": "write"}
    assert team_members("realliance", "ci") == {"forge-ci"}

def test_teams_yaml_valid():
    teams = yaml.safe_load(forge_api("GET", f"/api/v1/repos/{INVENTORY}/raw/teams.yaml", params={"ref": "main"}).text)["teams"]
    assert teams
    for key, groups in teams.items():
        org, name = key.split("/")
        assert name.lower() != "owners" and name in {t["name"] for t in forge_api("GET", f"/api/v1/orgs/{org}/teams").json()}
        assert all("/" not in g and len(kc_groups_named(g)) == 1 for g in groups)
```

- [ ] **Step 3: Run them to verify they fail**

Run: `nix develop --command pytest tests/forge/test_forge_inventory.py -k "forge_bot or ci_team or teams_yaml" -v`
Expected: FAIL (404 `realliance/forge-bot`)

- [ ] **Step 4: Implement** (inventory branch `forge-bot-decl`, signed)
  - `repos.tf`, `forgejo_repository.forge_bot`:
    - owner `realliance`, `private = false`, description "Realliance bot: keeps the forge in step with Realliance SSO (Go)";
    - `has_actions = true`, `has_packages = true`;
    - the same FF-only merge settings as `inventory`, `prevent_destroy`.
  - `repos.tf`, `forgejo_branch_protection.forge_bot_main`: `enable_push = false`, `require_signed_commits = true`, `required_approvals = 0`, merge and approval whitelists `["Owners"]`.
  - `teams.tf`, `forgejo_team.realliance_ci`: `permission = "read"`, `units_map = { "repo.packages" = "write" }`, `includes_all_repositories = false`, `can_create_org_repo = false`; plus `forgejo_team_member.realliance_ci` for `forge-ci`.
  - `teams.yaml`: exactly the spec's format, with the Step 1 map (expected `realliance/members: [realliance]` and `realliance/maintainers: [realliance-maintainers]`).
  - `forge-bot.yaml`: a comment (signed Owner commits only; RFC 3339, ≤ 24 h ahead; `null` = none) and `allow_bulk_until: null`.
  - README: the critical-repos list (`forge`, `forge-bot`), `teams.yaml` (owned by forge-bot, every cycle; never Owners), `forge-bot.yaml`, and "New org: add its non-Owners teams to `teams.yaml`".
  - **Chris:** merges. **STOP — ask Chris,** then apply. Expected: `4 to add, 0 to destroy`. If the provider leaves `includes_all_repositories = false` unusable for package pushes, Task 5 ledgers it and flips it.

- [ ] **Step 5: Tag protection by hand** (the provider has no resource): `forge_api("POST", "/api/v1/repos/realliance/forge-bot/tag_protections", json={"name_pattern": "v*", "whitelist_teams": ["Owners"]})` → 201. Then clone: `git clone ssh://git@git.realliance.net/realliance/forge-bot.git ~/Repos/forge-bot`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `nix develop --command pytest tests/forge/test_forge_inventory.py -v`
Expected: PASS (no drift)

- [ ] **Step 7: Commit** the sso tests (branch, PR, merge on Chris's word)

---

### Task 5: B0 — package-token prototype and the registry secret

**Files:**
- Modify (`realliance/sso`): `tests/forge_helpers.py` (`oci_push`, `anonymous_manifest`, `PACKAGE_TOKEN_REPO_SCOPED`)
- Create (`realliance/sso`): `tests/forge/test_forge_ci.py`
- Modify (private): `forge/secrets.env` (+ `FORGE_CI_PACKAGE_TOKEN`)

**Interfaces:**
- Consumes: `forge-ci`, team `ci`, `realliance/forge-bot`.
- Produces: the Actions secret `REGISTRY_TOKEN` (registry user `forge-ci`), as a repo secret on `realliance/forge-bot` (outcome A) or an org secret on `realliance` (outcome B). Also the ledgered B0 result, including the `SERVE_DIRECT` observation.

- [ ] **Step 1: Write the failing tests**

```python
def test_package_token_scope():                                   # Success 16
    tok = _load_env(FORGE_SECRETS)["FORGE_CI_PACKAGE_TOKEN"]
    tag = f"probe-{secrets.token_hex(3)}"
    try:
        assert oci_push("realliance/forge-bot", tag, "forge-ci", tok) == 201
        assert anonymous_manifest("realliance/forge-bot", tag).status_code == 200   # public with the org
        assert oci_push("forge-bot/b0-probe", tag, "forge-ci", tok) in (401, 403)   # another owner
        other = oci_push("realliance/b0-other", tag, "forge-ci", tok)
        assert other in (401, 403) if PACKAGE_TOKEN_REPO_SCOPED else other == 201
    finally:
        for name in ("forge-bot", "b0-other"):
            forge_api("DELETE", f"/api/v1/packages/realliance/container/{name}/{tag}")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `nix develop --command pytest tests/forge/test_forge_ci.py::test_package_token_scope -v`
Expected: FAIL (`KeyError: 'FORGE_CI_PACKAGE_TOKEN'`)

- [ ] **Step 3: Find a way to make a repository-specific token** (read-only checks).
  - `kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin user generate-access-token --help`: is there a repositories option?
  - The swagger `CreateAccessTokenOption.repositories` exists, but `POST /users/forge-ci/tokens` needs basic auth, and `ENABLE_BASIC_AUTHENTICATION=false`. Try it as the admin with `Sudo: forge-ci` and expect a refusal.
  - If neither works: outcome B.

- [ ] **Step 4: STOP — ask Chris, then create the token** into `forge/secrets.env` as `FORGE_CI_PACKAGE_TOKEN`, the same pipe-without-echo way as Task 2.
  - Outcome A: repo-specific (`realliance/forge-bot`), scope `write:package`.
  - Outcome B: plain `write:package`.
  - Set `PACKAGE_TOKEN_REPO_SCOPED` to match. Commit sso-secrets signed and push.

- [ ] **Step 5: Run the prototype** (= the test).
  - Expected (A): PASS with `realliance/b0-other` refused. If A pushes but other packages are also accepted, it is outcome B: re-create the token as B and ledger it.
  - If the push to `realliance/forge-bot` is refused because team `ci` lacks repo access, set `includes_all_repositories = true` on team `ci` (inventory PR, Chris merges, apply) and ledger it.
  - Also record whether `GET /v2/realliance/forge-bot/blobs/<digest>` answers 307 to B2 (`SERVE_DIRECT`); either answer is fine.

- [ ] **Step 6: STOP — ask Chris, then set the Actions secret.**
  - Outcome A: `PUT /api/v1/repos/realliance/forge-bot/actions/secrets/REGISTRY_TOKEN`. Outcome B: `PUT /api/v1/orgs/realliance/actions/secrets/REGISTRY_TOKEN`.
  - Body `{"data": <token>}` built in Python from the env file; never in argv. Expected: 201 or 204.
  - Ledger the outcome (the runbook text comes in Task 19).

- [ ] **Step 7: Commit** the sso tests and helpers (branch, PR, merge on Chris's word)

---

### Task 6: Runner image `images/forgejo-runner/` (B1)

**Files (`luma-homeops`):**
- Create: `images/forgejo-runner/Dockerfile`, `.github/workflows/forgejo-runner-image.yml`

**Interfaces:**
- Produces: `ghcr.io/christopherjmiller/forgejo-runner:sha-<7>@sha256:…` (public, amd64): forgejo-runner v13.2.1, git, Go 1.26.x, ko, Node 24, gcc + libc6-dev, uid 1000 `runner`, no Docker, no Nix.

- [ ] **Step 1: Write the smoke test first** (the last step of the workflow, which runs the pushed image)

```bash
img="$(echo '${{ steps.meta.outputs.tags }}' | head -1)"
docker run --rm --entrypoint sh "$img" -c '
  set -eux
  forgejo-runner --version | grep -F 13.2.1
  go version | grep -F go1.26.
  node --version | grep -E "^v24\."
  ko version
  gcc --version >/dev/null
  test "$(id -u)" = 1000
  ! command -v docker && ! command -v nix'
```

- [ ] **Step 2: Write the Dockerfile.**
  - A multi-stage build, every `FROM` pinned `tag@sha256` (at least 2 weeks old):
    - `data.forgejo.org/forgejo/runner:13.2.1` (copy `/bin/forgejo-runner`; confirm the path);
    - `golang:1.26.<x>-trixie` (copy `/usr/local/go`);
    - `node:24.<x>-trixie-slim` (copy `/usr/local/bin/node`);
    - `ghcr.io/ko-build/ko:<v>` (copy the `ko` binary, or download the release and check its sha256);
    - final stage `debian:trixie-slim`.
  - `apt-get install --no-install-recommends ca-certificates git gcc libc6-dev`, `useradd -u 1000 -m runner`, `ENV PATH=/usr/local/go/bin:$PATH`, `USER 1000:1000`, `ENTRYPOINT ["forgejo-runner"]`.
  - Header comment: built here because the runner must exist before the forge can build anything (spec "Bootstrap"); Renovate bumps the `FROM` lines; re-pin by hand (sharp edge 6).

- [ ] **Step 3: Write the workflow.** Copy `.github/workflows/mcp-jwt-auth.yml`: `paths: images/forgejo-runner/**`, `IMAGE: ghcr.io/${{ github.repository_owner }}/forgejo-runner`, `platforms: linux/amd64`, and the Step 1 smoke test as its last step (`if: github.event_name != 'pull_request'`).

- [ ] **Step 4: Commit, push, watch**

```bash
nix develop --command git add images/forgejo-runner .github/workflows/forgejo-runner-image.yml
nix develop --command git commit -m "images: forgejo-runner (v13.2.1, host-mode toolchain: Go, ko, Node; no Nix)"
git push
gh run watch "$(gh run list --workflow forgejo-runner-image.yml -L1 --json databaseId -q '.[0].databaseId')" --exit-status
```
Expected: the run succeeds, and the smoke test prints `13.2.1`, `go1.26.`, `v24.`.

- [ ] **Step 5: Verify the anonymous pull.** `curl -s "https://ghcr.io/token?scope=repository:christopherjmiller/forgejo-runner:pull"` returns a token, and the `sha-<7>` manifest GET with it returns 200. Record the digest for Task 7.

---

### Task 7: Actions on, runner deployed and registered for `realliance/forge-bot` only (B2)

**Files (`luma-homeops`):**
- Modify: `cluster/applications/forgejo-release.yaml` (`gitea.config.actions`), `cluster/applications/kustomization.yaml`
- Create in `cluster/forgejo-runner/`: `kustomization.yaml`, `namespace.yaml`, `pvc.yaml`, `deployment.yaml`, `runner-config.secret.yaml` → `runner-config.yaml`
- Create: `cluster/applications/forgejo-runner.yaml`

**Files (`realliance/sso`):**
- Modify: `flake.nix` (+ `kubectl`), `pytest.ini` (`slow`), `tests/forge_helpers.py` (`kube`, `galaxy_only`, `manifests`, `push_probe_workflow`, `delete_branch`, `runs_for`)
- Extend: `tests/forge/test_forge_ci.py`

**Files (private):** `forge/runner.env` (`RUNNER_FORGE_BOT_SECRET`)

**Interfaces:**
- Consumes: the runner image digest (Task 6), `realliance/forge-bot` (Task 4).
- Produces: Deployment `forgejo-runner/forgejo-runner` with label `galaxy` (host). Work volume layout under `/work`:
  - `act/`: per-job workdirs, `host.workdir_parent`;
  - `cache/`: actcache, go-build, go-mod, gopath;
  - `home/`, `tmp/`, `.runner`.

- [ ] **Step 1: Write the failing tests** (`test_forge_ci.py`)

```python
RUNNER_DIR = LUMA / "cluster/forgejo-runner"

def test_runner_manifests_unprivileged():                         # Success 17
    docs = manifests(RUNNER_DIR)
    ns = next(x for x in docs if x["kind"] == "Namespace")
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "baseline"
    for spec in (x["spec"]["template"]["spec"] for x in docs if x["kind"] == "Deployment"):
        assert spec["automountServiceAccountToken"] is False and spec["enableServiceLinks"] is False
        assert not any(spec.get(k) for k in ("hostNetwork", "hostPID", "hostIPC"))
        assert not any("hostPath" in v for v in spec.get("volumes", []))
        assert spec["securityContext"]["runAsNonRoot"] is True
        assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        for c in spec["containers"] + spec.get("initContainers", []):
            sc = c["securityContext"]
            assert sc.get("privileged") is not True and sc["allowPrivilegeEscalation"] is False
            assert sc["capabilities"] == {"drop": ["ALL"]} and "docker.sock" not in yaml.safe_dump(c)

def test_runner_registered_with_galaxy_label_only():
    rs = forge_api("GET", f"/api/v1/repos/{FORGE_BOT_REPO}/actions/runners").json()
    assert rs and all({l.split(":")[0] for l in r["labels"]} == {"galaxy"} for r in rs)

def test_probe_job_hardened_and_workspace_wiped():
    # script: test "$(id -u)" = 1000; test ! -e /var/run/docker.sock; test ! -e /var/run/secrets/kubernetes.io;
    #         go version; ko version; node --version;
    #         test -z "$(find /work/act -name 'leftover-*' 2>/dev/null)"; touch "$GITHUB_WORKSPACE/leftover-$GITHUB_RUN_NUMBER"
    # push it twice (two commits on one probe branch); both runs reach status "success" within CYCLE_WAIT.
    # The second run passing proves the first run's workspace was wiped. Finally: delete_branch.

@pytest.mark.slow
def test_allowlist_jobs_wait():                                   # Success 15
    # (a) scratch repo realliance/ci-probe-<hex> (admin-created) with a runs-on: galaxy probe
    # (b) probe branch in FORGE_BOT_REPO with runs-on: ubuntu-latest
    # after 600 s: every run of both has status "waiting" (never running/success/failure)
    # finally: delete the scratch repo and the probe branch (Forgejo's abandoned-job cron cancels (b) after 24 h)

def test_actions_logs_in_b2():
    assert b2_names("realliance-forge-objects", "actions_log/")

@galaxy_only
def test_runner_pod_holds_no_bot_or_admin_secrets():              # Success 17
    env = _load_env(FORGE_SECRETS)
    values = [env["FORGEJO_ADMIN_TOKEN"], env["FORGE_BOT_FORGEJO_TOKEN"], SECRETS["forge_bot_client_secret"]]
    base = ("-n", "forgejo-runner", "exec", "-i", "deploy/forgejo-runner", "--")
    assert kube(*base, "true").returncode == 0
    for v in values:   # the value travels on stdin only; never in the message
        r = kube(*base, "sh", "-c", "grep -rqsF -f /dev/stdin /etc /work /home /tmp /proc/1/environ", stdin=v)
        assert r.returncode != 0, "a bot/admin secret is readable in the runner pod"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command pytest tests/forge/test_forge_ci.py -k "not package_token" -v`
Expected: FAIL (no `cluster/forgejo-runner`; no runners)

- [ ] **Step 3: Capacity and storage reads (S1).**
  - `kubectl -n rook exec deploy/rook-ceph-tools -- ceph df`: the SSD pool must have ≥ 40 GiB free after 15 GiB.
  - `kubectl describe nodes | grep -A8 "Allocated resources"`: `middle` or `bottom` must stay < 90 % memory requested after +768Mi.
  - If either fails: **STOP — ask Chris.** Ledger the numbers.

- [ ] **Step 4: Enable Actions.** In `forgejo-release.yaml` under `gitea.config`, add `actions: {ENABLED: true, DEFAULT_ACTIONS_URL: https://github.com, ARTIFACT_RETENTION_DAYS: 30}`, with a comment that logs and artifacts go to B2 through the default `[storage]`.

- [ ] **Step 5: STOP — ask Chris. Register the runner offline** (this mints a runner credential).
  - In sso-secrets: `printf 'RUNNER_FORGE_BOT_SECRET=%s\n' "$(openssl rand -hex 20)" >> forge/runner.env`, signed commit, push.
  - Register: `(. forge/runner.env; printf %s "$RUNNER_FORGE_BOT_SECRET") | kubectl -n forgejo exec -i deploy/forgejo -c forgejo -- forgejo forgejo-cli actions register --name galaxy-forge-bot --scope realliance/forge-bot --labels galaxy --secret-stdin`. Confirm `--scope owner/repo` and `--secret-stdin` with `--help`; never pass `--secret` in argv. Expected: a runner UUID.
  - Build the connection entry in a `umask 077` scratch dir with `forgejo-runner create-runner-file --instance https://git.realliance.net --name galaxy-forge-bot` with the secret on stdin (any forgejo-runner ≥ 12.7, e.g. `nix shell nixpkgs#forgejo-runner`). Copy its `uuid`/`token` into the `server.connections` format that `forgejo-runner generate-config` (v13.2.1) prints.

- [ ] **Step 6: Write the manifests.**
  - `namespace.yaml`: `forgejo-runner`, `pod-security.kubernetes.io/enforce: baseline`.
  - `pvc.yaml`: `forgejo-runner-work`, 15Gi, `rook-ceph-block-ssd`, RWO.
  - `runner-config.secret.yaml`: Secret `forgejo-runner-config`, key `config.yml`, with:
    - `runner: {file: /work/.runner, capacity: 1, timeout: 1h, labels: ["galaxy:host"]}`;
    - `cache: {enabled: true, dir: /work/cache/actcache}`;
    - `host: {workdir_parent: /work/act}`;
    - the single connection from Step 5.
    - Seal with `./sign.sh`.
  - `deployment.yaml`:
    - 1 replica, `Recreate`, image from Task 6 pinned by digest;
    - command `sh -c 'mkdir -p /work/act /work/cache /work/home /work/tmp && exec forgejo-runner daemon --config /etc/forgejo-runner/config.yml'`;
    - pod `runAsUser/runAsGroup/fsGroup: 1000`, `runAsNonRoot`, `RuntimeDefault`, `automountServiceAccountToken: false`, `enableServiceLinks: false`;
    - container: no escalation, `drop: [ALL]`, `readOnlyRootFilesystem: true`;
    - env `HOME=/work/home`, `TMPDIR=/work/tmp`, `GOMAXPROCS=2`, `GOFLAGS=-p=2`, `GOCACHE=/work/cache/go-build`, `GOMODCACHE=/work/cache/go-mod`, `GOPATH=/work/cache/gopath`;
    - requests `250m/768Mi`, limits `cpu: 2, memory: 3Gi`;
    - `preferredDuringSchedulingIgnoredDuringExecution` node affinity `kubernetes.io/hostname NotIn [top]` (thermals, #2603);
    - volumes: the PVC at `/work`, the secret at `/etc/forgejo-runner` (readOnly).
  - `cluster/applications/forgejo-runner.yaml`: the shape of `immich.yaml` (path `cluster/forgejo-runner`), listed in `cluster/applications/kustomization.yaml`.

- [ ] **Step 7: STOP — ask Chris (Forgejo restarts on the config change). Commit and push.**

```bash
nix develop --command bash -c 'scripts/check-kustomizations.sh && kubectl kustomize cluster/forgejo-runner | grep -c "^kind:"'   # expect 4: Namespace, PVC, SealedSecret, Deployment
nix develop --command git add cluster/forgejo-runner cluster/applications/forgejo-runner.yaml cluster/applications/kustomization.yaml cluster/applications/forgejo-release.yaml
nix develop --command git commit -m "forgejo: Actions on; forgejo-runner (host mode, galaxy label, realliance/forge-bot only)"
git push
```
Monitor until `forgejo` and `forgejo-runner` are Ready (safe-rollout skill).

- [ ] **Step 8: Run the tests to verify they pass**

Run: `nix develop --command pytest tests/forge/test_forge_ci.py -v -k "not package_token"`
Expected: PASS. If `test_probe_job_hardened_and_workspace_wiped` fails on the leftover check, the per-job wipe (a spec mitigation) is not met: **STOP — ask Chris** with the finding; don't paper over it.

- [ ] **Step 9: Commit** the sso tests, helpers, flake and pytest.ini (branch, PR, merge on Chris's word)

---

### Task 8: forge-bot scaffold — module, model, config, CI workflow (C1)

**Files (`~/Repos/forge-bot`, branch `v1` from `main`):**
- Create: `go.mod`, `LICENSE-MIT`, `LICENSE-APACHE`, `README.md`, `.ko.yaml`, `.forgejo/workflows/ci.yaml`
- Create: `cmd/forge-bot/main.go`, `internal/model/model.go`, `internal/config/config.go`, `internal/config/config_test.go`

**Interfaces — Produces** (every later task uses these exact names):

```go
// internal/model
type Mode int // ModeOff=0, ModeDryRun=1, ModeApply=2 (also the forge_bot_job_mode value)
func ParseMode(s string) (Mode, error)          // "off" | "dry-run" | "apply"
const JobAccess, JobAdmin, JobTeams, JobProfile, JobAvatar = "access", "admin", "teams", "profile", "avatar"
var JobOrder = []string{JobAccess, JobAdmin, JobTeams, JobProfile, JobAvatar}
const SourceSSO, SourceForgejo, SourceInventory = "sso", "forgejo", "inventory"
type SSOUser struct {
    ID, Username, Email, FirstName, LastName, Pronouns, Picture string // Pronouns/Picture: first attribute value or ""
    EmailVerified, Enabled, TemporarilyLocked bool
    RequiredActions []string
}
type SSO struct {
    Users  map[string]SSOUser         // by Keycloak id (= Forgejo login_name)
    Groups map[string]map[string]bool // key: group path ("/apps/forgejo") or flat teams.yaml name → member ids
}
type ForgeUser struct {
    Login, LoginName, Email, FullName, Pronouns, Visibility string
    SourceID int64
    IsAdmin, ProhibitLogin bool
}
type Team struct { ID int64; Org, Name string; Members map[string]bool }
type Forge struct {
    Users  []ForgeUser        // every account, any source
    Teams  map[string]Team    // "org/team" for each teams.yaml key
    Owners map[string]bool    // logins in any org's Owners team
}
type Inventory struct {
    HeadSHA        string
    Trusted        bool
    Teams          map[string][]string // nil unless trusted and valid
    TeamsErr       error
    AllowBulkUntil *time.Time
}
type Snapshot struct { SSO SSO; Forge Forge; Inv Inventory; OK map[string]bool }
type Policy struct { SourceID int64; AccessGroup, AdminGroup, PublicURL string }
type Kind string // KindLoss="loss", KindGrant="grant", KindProfile="profile"
type Change struct {
    Job string; Kind Kind
    Login, LoginName, UserID string
    Field string // prohibit_login | admin | team | email | full_name | pronouns | picture
    TeamKey string; TeamID int64
    Old, New, Reason string
}
type Finding struct { Job, Kind, Login, Detail string } // Kind: owner_needs_action | email_conflict | unexpected_sso_write
type Limits struct { LossPeople int; LossFraction float64; Grants, Profile int }

// internal/config
type Config struct {
    SSO       struct{ URL, Realm, ClientID, ClientSecretFile, AccessGroup, AdminGroup string }
    Forgejo   struct{ URL, PublicURL, TokenFile string; SourceID int64 }
    Inventory struct{ Repo, Branch, Keyring, Fingerprints string } // Repo "owner/name"
    Interval, Jitter time.Duration
    Modes  map[string]model.Mode
    Limits model.Limits
    Listen string
}
func Load(path string) (Config, error)
func ReadSecret(path string) (string, error) // trimmed; error text never contains the content
```

- [ ] **Step 1: Write the failing tests** (`internal/config/config_test.go`)
  - `TestLoadDefaults`: a minimal YAML (only `sso`, `forgejo`, `inventory`) gives all five modes `ModeDryRun`, `Interval` 5m, `Jitter` 30s, `Limits{3, 1.0/3, 10, 25}`, `Listen ":8080"`.
  - `TestLoadRejectsUnknownMode` (`apply-ish`), `TestLoadRejectsUnknownKeys` (strict decode), `TestLoadRejectsBadInventoryRepo` (`forge`, no `/`), `TestLoadRejectsUnknownJob` (`modes: {mirror: apply}`).
  - `TestReadSecretTrims` (`"tok\n"` → `"tok"`); `TestReadSecretMissing` (the error mentions the path, not content).
  - `TestModeValues` (`ModeOff=0`, `ModeDryRun=1`, `ModeApply=2`; `ParseMode("dry-run")`).
  - YAML keys are snake_case: `client_secret_file`, `access_group`, `source_id`, `loss_people`, `loss_fraction`, ….

- [ ] **Step 2: Run them to verify they fail**

Run: `cd ~/Repos/forge-bot && $GO test -race -p=4 ./internal/config/`
Expected: FAIL (package does not compile)

- [ ] **Step 3: Implement** `model.go` (types above plus `ParseMode`) and `config.go` (`go.yaml.in/yaml/v3` with `KnownFields(true)`).
  - `cmd/forge-bot/main.go`: flag `--config` (default `/etc/forge-bot/config/config.yaml`); `Load`, or exit 1 with the error; the loop arrives in Task 14.
  - `go.mod`: `module git.realliance.net/realliance/forge-bot`, `go 1.26`.
  - Licences: MIT OR Apache-2.0.
  - README: purpose, the five jobs, the safety rules table (link the spec in luma-homeops), modes, "the only SSO write is `picture`", build (`ko`), and that releases are pinned by hand in luma-homeops after review.
  - `.ko.yaml`: `defaultBaseImage: gcr.io/distroless/static-debian12:nonroot@sha256:<digest>`, `defaultPlatforms: [linux/amd64]`, build `./cmd/forge-bot` with `-trimpath` and ldflags `-s -w`.

- [ ] **Step 4: Write `.forgejo/workflows/ci.yaml`**
  - `on: {push: {branches: [main], tags: ["v*"]}, pull_request: {}}`.
  - Job `test`: `runs-on: galaxy`; steps `actions/checkout@<40-hex sha of a v5 release ≥ 2 weeks old>  # v5.x.y`, `go vet ./...`, `go test -race ./...`.
  - Job `image`: `needs: test`, `if: github.event_name == 'push'`, `runs-on: galaxy`; steps: checkout (same sha), then one `run` step with env `REGISTRY_TOKEN: ${{ secrets.REGISTRY_TOKEN }}` and `KO_DOCKER_REPO: git.realliance.net/realliance/forge-bot`:
    - `export DOCKER_CONFIG="$(mktemp -d)"`;
    - `ko login git.realliance.net -u forge-ci --password-stdin <<<"$REGISTRY_TOKEN"`;
    - `ref=$(ko build --bare --tags "sha-${GITHUB_SHA::7}" ./cmd/forge-bot)`;
    - `echo "$ref"`, appended to `$GITHUB_STEP_SUMMARY` when set;
    - `rm -rf "$DOCKER_CONFIG"`.
  - Header comment: the Forgejo 15 differences that matter here (no `permissions`, no environments, cache per runner), the accepted branch-push risk, and "never approve fork-PR runs".

- [ ] **Step 5: Run the tests to verify they pass**

Run: `$GO vet ./... && $GO test -race -p=4 ./...`
Expected: PASS

- [ ] **Step 6: Commit, push the branch, open the PR**

```bash
git switch -c v1 && git add -A && git commit -S -m "forge-bot: module, model, config, CI workflow"
git push -u origin v1   # then open PR v1 → main (web UI, fj, or POST /repos/realliance/forge-bot/pulls with Sudo: chris)
```
Expected: `GET /api/v1/repos/realliance/forge-bot/actions/runs?event=pull_request` shows the newest run `success`, on runner `galaxy-forge-bot`. If no runner picks it up within 5 min, check the runner pod log before anything else.

---

### Task 9: `internal/inventory` — commit signature verification and the two parsers (C1)

**Files:**
- Create: `internal/inventory/verify.go`, `verify_test.go`, `parse.go`, `parse_test.go`
- Create: `internal/inventory/testdata/gen-git-fixture.sh` and its outputs `git-commit.raw`, `git-commit.sha`, `git-signer.asc`

**Interfaces:**
- Consumes: `model` (Task 8).
- Produces:

```go
var ErrUnsigned, ErrUnknownSigner, ErrNotAllowed, ErrBadSignature, ErrSHAMismatch error
var ErrNoTeams, ErrBadTeamKey, ErrOwnersTeam, ErrBadGroupName, ErrOverrideTooFar error
type Keyring struct{ /* entities + allow-listed primary fingerprints */ }
func ParseFingerprints(r io.Reader) ([]string, error)        // hex; spaces ignored; '#' comments; upper-cased
func LoadKeyring(armored io.Reader, fingerprints []string) (*Keyring, error)
func ReconstructCommit(payload, signature string) []byte     // raw git commit object (no "commit <n>\0" header)
func CommitSHA1(raw []byte) string                           // sha1("commit " + len + "\x00" + raw), hex
func (k *Keyring) VerifyCommit(sha, payload, signature string, now time.Time) (fingerprint string, err error)
func ParseTeams(b []byte) (map[string][]string, error)
func ParseBotConfig(b []byte, now time.Time) (*time.Time, error) // nil = no override
```

- [ ] **Step 1: Generate the real-git fixture.** `testdata/gen-git-fixture.sh` uses a temp `GNUPGHOME` (`gpg --batch --passphrase '' --quick-gen-key 'Fixture <fixture@invalid>' ed25519 sign never`) and a temp repo with one signed commit (`git -c user.signingkey=… commit -S --allow-empty -m fixture`). It writes `git cat-file commit HEAD` → `git-commit.raw`, `git rev-parse HEAD` → `git-commit.sha`, and `gpg --armor --export` → `git-signer.asc`. Run it once; commit the outputs (the throwaway key is public test data).

- [ ] **Step 2: Write the failing tests**
  - `TestReconstructCommitMatchesGit`: split `git-commit.raw` into payload (the raw object without its `gpgsig` header and continuation lines) and signature (the unfolded header value). `ReconstructCommit(payload, sig)` must equal the raw bytes, and `CommitSHA1(raw)` must equal `git-commit.sha`. This pins the format: insert `gpgsig ` + the signature lines joined with `"\n "` as the last header, before the blank line.
  - `TestVerifyCommitOwnerSigned`: `git-signer.asc` with its fingerprint allow-listed returns that fingerprint.
  - Synthetic commits (keys made in `TestMain` with `openpgp.NewEntity`; payload `tree …\nauthor …\ncommitter …\n\nmsg\n`; sha from `CommitSHA1(ReconstructCommit(…))`):
    - `TestVerifyCommitForgejoSigned`: signed by a key in the armored file but not allow-listed → `ErrNotAllowed`;
    - `TestVerifyCommitUnknownKey`: a stranger → `ErrUnknownSigner`;
    - `TestVerifyCommitUnsigned`: `""` → `ErrUnsigned`;
    - `TestVerifyCommitTamperedPayload`: one payload byte changed, recomputed sha → `ErrBadSignature`;
    - `TestVerifyCommitSHAMismatch`: a valid signature, but `sha` is another commit's → `ErrSHAMismatch`.
  - `TestParseFingerprints`: spaces and lower case normalised, comments skipped.
  - `TestParseTeamsValid`: the spec's example gives `{"realliance/members": ["realliance"], "realliance/maintainers": ["realliance-maintainers"]}`.
  - `TestParseTeamsRejects` (table; Review Focus 5):
    - empty file and `teams: {}` → `ErrNoTeams`;
    - unknown top-level key → error;
    - key `members` (no `/`) → `ErrBadTeamKey`;
    - `realliance/Owners` and `x/owners` → `ErrOwnersTeam`;
    - group `/realliance` → `ErrBadGroupName`;
    - an empty group list → error;
    - a duplicate key → error.
  - `TestParseBotConfig`:
    - `allow_bulk_until: null` or absent → nil;
    - now + 2 h → that time;
    - now + 25 h → `ErrOverrideTooFar`;
    - now − 1 h → nil (expired);
    - `2026-11-02` (not RFC 3339) → error.

- [ ] **Step 3: Run them to verify they fail**

Run: `$GO test -race -p=4 ./internal/inventory/`
Expected: FAIL (undefined: `ReconstructCommit`, …)

- [ ] **Step 4: Implement.**
  - `VerifyCommit` order: empty signature → `ErrUnsigned`; then `CommitSHA1(ReconstructCommit(payload, sig)) != sha` → `ErrSHAMismatch`; then `openpgp.CheckArmoredDetachedSignature(keyring, payload, sig, &packet.Config{Time: func() time.Time { return now }})`; then the signer's **primary** fingerprint against the allow-list.
  - It never reads Forgejo's `verified`.
  - `ParseTeams` and `ParseBotConfig` use strict YAML decoding.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `$GO test -race -p=4 ./internal/inventory/`
Expected: PASS

- [ ] **Step 6: Commit** (`git commit -S -m "inventory: owner-signed head verification; teams.yaml and forge-bot.yaml parsers"`, `git push`; the PR run must stay green)

---

### Task 10: `internal/keycloak` — reads, and `SetPicture` as the only write (C1)

**Files:**
- Create: `internal/keycloak/client.go` (token + reads), `picture.go` (`SetPicture`), `events.go`, `client_test.go`, `picture_test.go`, `writes_test.go`

**Interfaces:**
- Consumes: `model.SSOUser`.
- Produces:

```go
var ErrIncomplete, ErrGroupMissing, ErrGroupAmbiguous error
type Client struct{ /* baseURL, realm, clientID, secret, hc, cached token */ }
func New(baseURL, realm, clientID, secret string, hc *http.Client) *Client
func (c *Client) Users(ctx context.Context) (map[string]model.SSOUser, error)       // paged briefRepresentation=false; total must equal users/count
func (c *Client) ResolveGroup(ctx context.Context, flatName string) (path string, err error)
func (c *Client) GroupMembers(ctx context.Context, path string) (map[string]bool, error) // via group-by-path, then paged members
func (c *Client) TemporarilyLocked(ctx context.Context, userID string) (bool, error)    // attack-detection/brute-force/users/{id} "disabled"
func (c *Client) AdminEvents(ctx context.Context, since time.Time) ([]AdminEvent, error)  // authRealm=master&authUser=<sub of own token>, paged, time > since
func (c *Client) SetPicture(ctx context.Context, userID, value string) (pre Identity, err error) // value "" removes
type AdminEvent struct { Time time.Time; OperationType, ResourceType, ResourcePath, Representation string }
type Identity struct { Username, Email string; Enabled, EmailVerified bool; RequiredActions []string }
func IdentityOf(rep map[string]any) Identity
```

- [ ] **Step 1: Write the failing tests** (an `httptest` fake Keycloak that records every request)
  - `TestUsersPagesAndCountMatch` (count 5, `max` 2 → 3 pages, 5 users); `TestUsersShortPageIsIncomplete` (count 6, 5 served → `ErrIncomplete`).
  - `TestUsersParsesAttributes`: `pronouns`, `picture`, `emailVerified`, `requiredActions`.
  - `TestResolveGroup`: a nested match gives its path; none → `ErrGroupMissing`; two with the same name → `ErrGroupAmbiguous`.
  - `TestGroupMembersPaged`; `TestTemporarilyLocked`.
  - `TestAdminEventsOwnClientOnly`: the query has `authRealm=master` and `authUser=<sub from the fake token>`; events at or before `since` are dropped.
  - `TestSetPictureChangesOnlyPicture`: the fake GET body has unknown fields (`access`, `federationLink`, `attributes.pronouns`, `attributes.mfa`). The PUT body decoded into `map[string]any` equals the GET body except `attributes.picture == [value]`.
  - `TestSetPictureCreatesAttributes` (Review Focus 3): a GET body without `attributes` → PUT has `attributes == {"picture": [value]}`; every other field is unchanged.
  - `TestSetPictureEmptyRemoves`: `""` deletes the `picture` key and leaves other attributes.
  - `TestSetPictureReturnsPreImage`: `pre == IdentityOf(GET body)`.
  - `TestOnlyAllowedRequests` (runtime): after `Users`, `ResolveGroup`, `GroupMembers`, `TemporarilyLocked`, `AdminEvents` and `SetPicture`, every recorded request is GET, or POST to `/realms/master/protocol/openid-connect/token`, or PUT to `/admin/realms/realliance/users/{id}`.
  - `TestNoOtherWritesInSource` (static; `go/parser` over this package's non-test files):
    - the strings `"POST"`, `"PUT"`, `"PATCH"`, `"DELETE"` never appear;
    - `http.MethodPut` appears exactly once, inside `SetPicture`;
    - `http.MethodPost` exactly once, inside the token function;
    - `http.MethodPatch`, `http.MethodDelete`, `http.Post` and `http.PostForm` never appear.
  - `TestErrorsNeverContainSecret`: a token endpoint answering 401 gives an error whose text lacks the secret.

- [ ] **Step 2: Run them to verify they fail**

Run: `$GO test -race -p=4 ./internal/keycloak/`
Expected: FAIL (package does not compile)

- [ ] **Step 3: Implement.**
  - Requests go through one helper, and its method is always a constant.
  - The token comes from `client_credentials` in master and is cached until 30 s before expiry; `sub` is read from the JWT payload, unverified (it is our own token).
  - `SetPicture` GETs `/admin/realms/{realm}/users/{id}` into `map[string]any`, edits only `attributes.picture` and PUTs the map back. It never round-trips through a struct.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$GO test -race -p=4 ./internal/keycloak/`
Expected: PASS

- [ ] **Step 5: Commit** (`git commit -S -m "keycloak: complete paged reads; SetPicture is the only write"`, push)

---

### Task 11: `internal/forgejo` — the Forgejo client (C1)

**Files:**
- Create: `internal/forgejo/client.go`, `client_test.go`

**Interfaces:**
- Consumes: `model.ForgeUser`, `model.Team`.
- Produces:

```go
var ErrIncomplete, ErrNotFound, ErrEmailConflict error
type Client struct{ /* baseURL, token, hc */ }
func New(baseURL, token string, hc *http.Client) *Client
func (c *Client) Users(ctx context.Context) ([]model.ForgeUser, error)                  // GET /api/v1/admin/users, paged; total == X-Total-Count
func (c *Client) Team(ctx context.Context, org, name string) (model.Team, error)       // ErrNotFound; members paged
func (c *Client) Owners(ctx context.Context) (map[string]bool, error)                  // /admin/orgs paged → each org's Owners members
func (c *Client) BranchHead(ctx context.Context, owner, repo, branch string) (string, error)
func (c *Client) CommitSignature(ctx context.Context, owner, repo, sha string) (payload, signature string, err error) // .commit.verification
func (c *Client) FileAt(ctx context.Context, owner, repo, path, sha string) ([]byte, error)  // GET /repos/{o}/{r}/raw/{path}?ref={sha}; ErrNotFound
func (c *Client) PatchUser(ctx context.Context, u model.ForgeUser, fields map[string]any) error // adds login_name + source_id; 422 "e-mail" → ErrEmailConflict
func (c *Client) AddTeamMember(ctx context.Context, teamID int64, login string) error
func (c *Client) RemoveTeamMember(ctx context.Context, teamID int64, login string) error
```

- [ ] **Step 1: Write the failing tests** (an `httptest` fake Forgejo)
  - `TestUsersPagedTotalCount`; `TestUsersShortListIsIncomplete` (`X-Total-Count: 7`, 6 served).
  - `TestUsersFields`: `login_name`, `source_id`, `pronouns`, `visibility`, `prohibit_login`, `is_admin`.
  - `TestTeamMembersPaged`; `TestTeamNotFound`; `TestOwnersAcrossOrgs` (2 orgs; one Owners team paged).
  - `TestCommitSignature`: reads `commit.verification.payload`/`.signature` and ignores `verified`.
  - `TestFileAtUsesRef` (the query carries `ref=<sha>`); `TestFileAtMissing` → `ErrNotFound`.
  - `TestPatchUserSendsLoginNameAndSourceID`: the body is `fields` plus `login_name` and `source_id`.
  - `TestPatchUserEmailConflict`: 422 with "e-mail already used" → `ErrEmailConflict`.
  - `TestTeamMemberWrites` (PUT/DELETE paths); `TestTokenNotInErrors`.

- [ ] **Step 2: Run them to verify they fail**

Run: `$GO test -race -p=4 ./internal/forgejo/`
Expected: FAIL (package does not compile)

- [ ] **Step 3: Implement** (stdlib `net/http`, `Authorization: token …`, `limit=50`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$GO test -race -p=4 ./internal/forgejo/`
Expected: PASS

- [ ] **Step 5: Commit** (`git commit -S -m "forgejo: paged admin reads, pinned file reads, user and team writes"`, push)

---

### Task 12: `internal/plan` — the five jobs as pure functions (C1)

**Files:**
- Create: `internal/plan/plan.go` (`Job`, `Jobs`, `HasAccess`, `FullName`), `access.go`, `admin.go`, `teams.go`, `profile.go`, `avatar.go`, plus `*_test.go` for each

**Interfaces:**
- Consumes: `model` (Task 8).
- Produces:

```go
type Job interface {
    Name() string
    Needs() []string // sources whose complete read the job requires
    Plan(s *model.Snapshot, p model.Policy) ([]model.Change, []model.Finding)
}
func Jobs() []Job                                                // in model.JobOrder
func HasAccess(u model.SSOUser, s *model.SSO, p model.Policy) bool // (Enabled || TemporarilyLocked) && member of p.AccessGroup
func FullName(first, last string) string                           // strings.TrimSpace(first + " " + last), inner whitespace kept
```

Shared rules:
- Only accounts with `SourceID == p.SourceID` are planned; local accounts never.
- An account's SSO user is `s.SSO.Users[acct.LoginName]`.
- Owners (`s.Forge.Owners[login]`) get no loss changes (prohibit, admin removal, managed-team removal); each suppressed loss becomes `Finding{Kind: "owner_needs_action"}`.
- `Needs`:
  - access, admin, profile, avatar: `sso` + `forgejo`;
  - teams: `sso` + `forgejo` + `inventory`, and it returns nothing unless `s.Inv.Trusted && s.Inv.Teams != nil`.

- [ ] **Step 1: Write the failing tests** (table-driven, from a tiny snapshot builder in `plan_test.go`)
  - Access:
    - `TestAccessProhibitsDisabled`, `TestAccessProhibitsWithoutAccessGroup`, `TestAccessLiftsWhenBack` (Kind grant);
    - `TestAccessTemporaryLockKeepsAccess` (`Enabled=false, TemporarilyLocked=true` → no change);
    - `TestAccessAbsentSubjectProhibited`, `TestAccessLocalAccountsUntouched`, `TestAccessOwnerLossBecomesFinding`, `TestAccessSteadyStateNoChanges`.
  - Admin: `TestAdminFollowsGroup` (grant and loss), `TestAdminIgnoresAccess` (a disabled member of `/forgejo-admins` stays admin), `TestAdminOwnerDemotionBecomesFinding`.
  - Teams:
    - `TestTeamsUnionOfGroups` (two groups → one team);
    - `TestTeamsOnlyPeopleWithAccounts`;
    - `TestTeamsUnlistedUntouched` (a team absent from `Inv.Teams` gets no changes);
    - `TestTeamsLocalMembersNeverRemoved` (`forge-ci` in a managed team stays);
    - `TestTeamsOwnerRemovalBecomesFinding`;
    - `TestTeamsNothingWhenUntrustedOrInvalid`.
  - Profile:
    - `TestProfileEmailOnlyIfVerified`, `TestProfilePronouns`, `TestProfileOnlyWithAccess`;
    - `TestProfileFullNameNoLastName` (Review Focus 2: `("Ada", "")` and `("Ada", "  ")` → `"Ada"`, and no change when Forgejo has `"Ada"`);
    - `TestProfileEmailCaseInsensitive` (Review Focus 2: `Ada@X.org` vs `ada@x.org` → no change);
    - `TestProfileEmailConflictSkipped` (another account holds the email → no change, `Finding{Kind: "email_conflict"}`).
  - Avatar:
    - `TestAvatarPublicWithAccessSetsURL` (`New == "https://git.realliance.net/<login>.png"`, Kind profile, `UserID` set);
    - `TestAvatarLimitedOrPrivateRemoves`, `TestAvatarNoAccessRemoves`, `TestAvatarNoWriteWhenEqual`.

- [ ] **Step 2: Run them to verify they fail**

Run: `$GO test -race -p=4 ./internal/plan/`
Expected: FAIL (package does not compile)

- [ ] **Step 3: Implement** the five jobs. Kinds:
  - prohibit, admin `true→false` and team removal are `KindLoss`;
  - lifting a prohibit, admin `false→true` and team add are `KindGrant`;
  - email, full_name, pronouns and picture are `KindProfile`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$GO test -race -p=4 ./internal/plan/`
Expected: PASS

- [ ] **Step 5: Commit** (`git commit -S -m "plan: access, admin, teams, profile, avatar as pure functions"`, push)

---

### Task 13: `internal/guard` (circuit breaker) and `internal/selfcheck` (C1)

**Files:**
- Create: `internal/guard/guard.go`, `guard_test.go`, `internal/selfcheck/selfcheck.go`, `selfcheck_test.go`

**Interfaces:**
- Consumes: `model.Change`, `model.Limits`, `keycloak.AdminEvent`, `keycloak.Identity`.
- Produces:

```go
// guard
type Verdict struct { Allowed []model.Change; Tripped map[model.Kind]bool }
func Guard(changes []model.Change, withAccess int, l model.Limits, override bool) Verdict
func LossCap(withAccess int, l model.Limits) int // min(l.LossPeople, max(1, floor(withAccess*l.LossFraction)))
func TrippedJobs(changes []model.Change, v Verdict) map[string]bool

// selfcheck
type WriteRecord struct { UserID string; At time.Time; Pre keycloak.Identity }
type Checker struct{ /* prev map[string]keycloak.Identity, own []WriteRecord, since time.Time, baselined bool */ }
func New() *Checker
func (c *Checker) Since() time.Time                 // events newer than this are examined next
func (c *Checker) RecordWrite(w WriteRecord)
func (c *Checker) Check(events []keycloak.AdminEvent, users map[string]model.SSOUser, now time.Time) []model.Finding
```

Guard rules:
- Count distinct `Login` per Kind (people, not changes).
- loss > `LossCap`, grant > `l.Grants` or profile > `l.Profile` → that Kind applies nothing and `Tripped[kind] = true`.
- Kinds are independent. `override` bypasses all caps.

Self-check rules (per event, oldest first):
- Not `ResourceType == "USER" && OperationType == "UPDATE"` with `ResourcePath` `users/<id>` → `unexpected_sso_write`.
- Otherwise, if an unconsumed own `WriteRecord` for that user lies within ±120 s, consume it and compare `IdentityOf(representation)` with its `Pre`.
- Else compare with `prev[userID]`; any difference in username, email, enabled, emailVerified or requiredActions → finding.
- Afterwards `prev` = identities of `users`, and `since` = the newest event time (or `now` on the first call).
- The first call only baselines: no findings, and earlier events are ignored (Decision 4).

- [ ] **Step 1: Write the failing tests**
  - Guard:
    - `TestGuardLossesOverThreePeopleBlockAll` (N = 30, 4 people → none allowed, `Tripped[loss]`);
    - `TestGuardLossesOverThird` (N = 9: 3 allowed, 4 blocked; N = 6: 2 allowed, 3 blocked);
    - `TestGuardSingleLossAlwaysAllowed` (Review Focus 1: N = 1 and N = 2, 1 loss allowed);
    - `TestGuardGrantsCap10` (11 → blocked);
    - `TestGuardProfileCap25` (26 people → blocked);
    - `TestGuardCountsPeopleNotChanges` (one person, prohibit + admin + two teams = 1);
    - `TestGuardOverrideAllowsAll`;
    - `TestGuardCategoriesIndependent` (losses tripped, grants still allowed);
    - `TestTrippedJobs`.
  - Self-check:
    - `TestSelfCheckFirstCallBaselinesOnly`;
    - `TestSelfCheckOwnPictureWriteClean`;
    - `TestSelfCheckOwnWriteDuringConcurrentEmailChange` (pre-image has the new email, prev has the old → no finding);
    - `TestSelfCheckForeignEmailChangeFlagged`;
    - `TestSelfCheckForeignPictureOnlyIgnored`;
    - `TestSelfCheckNonUserOrNonUpdateFlagged` (DELETE user, CREATE user, `GROUP_MEMBERSHIP`, `CLIENT` UPDATE);
    - `TestSelfCheckEventsBeforeSinceIgnored`.

- [ ] **Step 2: Run them to verify they fail**

Run: `$GO test -race -p=4 ./internal/guard/ ./internal/selfcheck/`
Expected: FAIL (packages do not compile)

- [ ] **Step 3: Implement** as specified above.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$GO test -race -p=4 ./internal/guard/ ./internal/selfcheck/`
Expected: PASS

- [ ] **Step 5: Commit** (`git commit -S -m "guard: per-category breaker with floor and override; selfcheck: forge-bot's own admin events"`, push)

---

### Task 14: `internal/cycle`, `audit`, `metrics` and `main` — the running bot (C1)

**Files:**
- Create: `internal/audit/audit.go` (+ test), `internal/metrics/metrics.go` (+ test), `internal/cycle/cycle.go`, `internal/cycle/fakes_test.go`, `internal/cycle/cycle_test.go`
- Modify: `cmd/forge-bot/main.go`

**Interfaces:**
- Consumes: everything above.
- Produces:

```go
// cycle — interfaces whose method sets match keycloak.Client / forgejo.Client exactly
type SSO interface {
    Users(context.Context) (map[string]model.SSOUser, error)
    ResolveGroup(context.Context, string) (string, error)
    GroupMembers(context.Context, string) (map[string]bool, error)
    TemporarilyLocked(context.Context, string) (bool, error)
    AdminEvents(context.Context, time.Time) ([]keycloak.AdminEvent, error)
    SetPicture(context.Context, string, string) (keycloak.Identity, error)
}
type Forge interface { /* Users, Team, Owners, BranchHead, CommitSignature, FileAt, PatchUser, AddTeamMember, RemoveTeamMember */ }
type Runner struct{ /* cfg, sso, forge, keyring, metrics, audit, selfcheck, last completed */ }
func NewRunner(cfg config.Config, sso SSO, forge Forge, kr *inventory.Keyring, m *metrics.Metrics, a *audit.Logger, sc *selfcheck.Checker) *Runner
func (r *Runner) CheckSource(ctx context.Context) error // Decision 8
func (r *Runner) RunCycle(ctx context.Context, now time.Time) Result
func (r *Runner) Loop(ctx context.Context)              // cycle now, then every Interval ± Jitter; never overlapping
func (r *Runner) Healthy(now time.Time) bool            // a cycle completed within 3 × Interval
type Result struct { ReadOK map[string]bool; Planned, Applied map[string]int; Tripped map[string]bool; Findings []model.Finding }

// audit — one JSON line per change or finding, to stdout
func New(w io.Writer) *Logger
func (l *Logger) Change(cycle string, c model.Change, mode model.Mode, err error) // {"kind":"audit","cycle","job","account","field","old","new","reason","mode","applied","error"}
func (l *Logger) Finding(cycle string, f model.Finding)

// metrics
func New(reg prometheus.Registerer) *Metrics
```

Metrics (exact names; label values only from fixed sets):
- `forge_bot_cycle_last_success_timestamp`
- `forge_bot_read_ok{source}`
- `forge_bot_planned_changes{job}`
- `forge_bot_applied_changes_total{job,kind}`
- `forge_bot_errors_total{job}` (job ∈ the five jobs + `read` + `selfcheck`)
- `forge_bot_breaker_tripped{job}`
- `forge_bot_job_mode{job}` (0/1/2)
- `forge_bot_owner_needs_action`
- `forge_bot_unexpected_sso_writes_total`
- `forge_bot_inventory_ok{check}` (`signature`, `teams`, `bot_config`)

`RunCycle` order (the inventory comes first because the other reads depend on `teams.yaml`):
1. **Inventory.**
   - `BranchHead`, `CommitSignature`, then `VerifyCommit`.
   - Only if trusted: `FileAt(teams.yaml)` → `ParseTeams`, and `FileAt(forge-bot.yaml)` → `ParseBotConfig` (`ErrNotFound` = no override).
   - Set `inventory_ok`. When the read fails, leave the gauges unchanged.
2. **Forgejo read.** `Users` (zero source-1 accounts counts as a failure), `Owners`, and `Team` for each `teams.yaml` key.
3. **SSO read.**
   - `Users`; for each user with `Enabled == false`, `TemporarilyLocked`.
   - `GroupMembers` for the access and admin paths.
   - Resolve and read every `teams.yaml` group, but only when the inventory is trusted and valid.
   - Empty access group membership counts as a failure.
4. **Self-check:** `AdminEvents(sc.Since())` → `Check`.
5. **Plan** every job whose `Needs` are all OK and whose mode is not off.
6. **Guard** with `withAccess` = source-1 accounts not prohibited, and `override` = trusted and `AllowBulkUntil` in the future.
7. **Apply** in `JobOrder`, only for `apply`-mode jobs. Dry-run audits with `applied=false`. Each picture write calls `sc.RecordWrite`.

Record metrics; `last_success` is set only when all three reads were OK.

- [ ] **Step 1: Write the failing tests** (in-memory fakes in `fakes_test.go` that record writes)
  - `TestCycleSSOReadFailsWritesNothing` (`read_ok{sso}=0`, zero writes, `last_success` unchanged); `TestCycleEmptyAccessGroupIsFailure`; `TestCycleNoSourceAccountsIsFailure`.
  - `TestCycleUntrustedHeadSkipsTeamsAndIgnoresOverride`: `inventory_ok{signature}=0`; other jobs still apply; with `allow_bulk_until` set, the breaker still trips.
  - `TestCycleInvalidTeamsYamlSkipsOnlyTeams` (table: unparsable `teams.yaml`, an unknown team (`Team` → `ErrNotFound`), a missing group (`ErrGroupMissing`), an ambiguous group (`ErrGroupAmbiguous`) → teams job writes nothing, `inventory_ok{teams}=0`, the other four jobs still apply; a team that was removed from the file is never emptied).
  - `TestCycleDryRunAuditsWithoutWrites`; `TestCycleOffModeSkipsJob`.
  - `TestCycleApplyOrder`: one person needing every job → the write log is access, admin, teams, profile, avatar.
  - `TestCycleSteadyStateZeroWrites`: the second cycle on the same fakes writes nothing.
  - `TestCycleFailedWriteRetriedNextCycle`.
  - `TestCycleBreakerSetsMetricPerJob`; `TestCycleOwnerFindingsGauge`; `TestCycleUnexpectedWriteCounter`.
  - `TestLoopNeverOverlaps` (a slow fake; concurrent cycles ≤ 1); `TestHealthyAfterThreeIntervals`.
  - `TestCheckSourceRejectsNonUUIDLoginNames`.
  - `TestMetricsLabelValuesFixed` (gather; every label value is in the fixed sets); `TestAuditLineShape`.

- [ ] **Step 2: Run them to verify they fail**

Run: `$GO test -race -p=4 ./...`
Expected: FAIL (cycle, audit, metrics undefined)

- [ ] **Step 3: Implement**, and wire `main.go`:
  - load the config; `ReadSecret` both files;
  - load the keyring (`ParseFingerprints` + `LoadKeyring`); build the clients with `http.Client{Timeout: 30 * time.Second}`;
  - `CheckSource` or exit 1;
  - an HTTP server on `Listen` with `/metrics` (promhttp) and `/healthz` (`Healthy` → 200, else 503);
  - `Loop` until SIGTERM. Logging is `slog` JSON on stdout; audit lines share stdout.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `$GO vet ./... && $GO test -race -p=4 ./...`
Expected: PASS; the PR run on galaxy is green

- [ ] **Step 5: Commit** (`git commit -S -m "forge-bot: cycle, apply, audit, metrics, main"`, push)

---

### Task 15: Alertmanager — urgent forge-bot alerts route 24/7 (C2)

**Files (`luma-homeops`):**
- Modify: `cluster/prometheus-stack/alertmanager-config.secret.yaml` and regenerate `alertmanager-config.yaml`

**Interfaces:**
- Produces: receiver `forge-urgent` (a Discord webhook both Owners see) and a first route `alertname=~"ForgeBot(BreakerTripped|OwnerNeedsAction|UnexpectedSSOWrite)|ReallianceSSOUnreadable"` → `forge-urgent`. It has no `active_time_intervals`, `repeat_interval: 4h` and `continue: false`.

- [ ] **Step 1: STOP — ask Chris.** He supplies the webhook for a channel both Owners see, as a file path, never pasted, and approves editing the sealed config (spec: only Chris edits it).

- [ ] **Step 2: Write the route tests and watch them fail** (scratch copy, as in the luma 0b plan Task 4)
  - `umask 077; t=$(mktemp -d)`, then `nix develop --command bash -c "yq '.stringData[\"alertmanager.yaml\"]' cluster/prometheus-stack/alertmanager-config.secret.yaml > $t/am.yaml"`.
  - For each row, `amtool config routes test --config.file $t/am.yaml --verify.receivers=<receiver> <labels>`:

  | labels | receiver |
  |---|---|
  | `alertname=ForgeBotBreakerTripped severity=critical` | `forge-urgent` |
  | `alertname=ReallianceSSOUnreadable severity=critical` | `forge-urgent` |
  | `alertname=ForgeBotOwnerNeedsAction severity=critical` | `forge-urgent` |
  | `alertname=ForgeBotUnexpectedSSOWrite severity=critical` | `forge-urgent` |
  | `alertname=ForgeBotStale severity=warning` | `homelab-default` |
  | `alertname=ForgeBotNotConverging severity=warning` | `homelab-default` |
  | `alertname=ForgeBotInventoryUntrusted severity=warning` | `homelab-default` |
  | `alertname=KubeAPIDown severity=critical` | `critical-alerts` (unchanged) |

  Expected now: the first four FAIL.

- [ ] **Step 3: Edit** the secret: the new receiver (webhook read from Chris's file, never echoed) and the route as the first child route. Re-run the matrix; expected: all pass. `rm -r "$t"`.

- [ ] **Step 4: Reseal.**
  - `rm cluster/prometheus-stack/alertmanager-config.yaml && KUBECONFIG=/tmp/galaxy-kubeconfig nix develop --command ./sign.sh` exits 0.
  - `yq '.spec.encryptedData | keys'` is `["alertmanager.yaml"]`.
  - `git status --short cluster/prometheus-stack/` shows exactly two ` M` lines.

- [ ] **Step 5: Commit and push** (`alertmanager: forge-bot urgent alerts route 24/7 to forge-urgent`), then Monitor the Argo sync of `prometheus-stack-config`.

- [ ] **Step 6: Drill (Chris, outside weekday hours).** With `kubectl -n prometheus port-forward svc/alertmanager-operated 9093` running: `amtool alert add alertname=ForgeBotBreakerTripped severity=critical drill=true --end=+10m --alertmanager.url=http://127.0.0.1:9093`. Both Owners confirm receipt; ledger the time.

---

### Task 16: Deploy forge-bot in dry-run; first image reviewed and pinned (C1 end + C2)

**Files (`luma-homeops`):**
- Create in `cluster/forge-bot/`:
  - `kustomization.yaml`, `namespace.yaml`, `config.yaml` (a configMapGenerator source), `keyring/owners.asc`, `keyring/fingerprints.txt`;
  - `forgejo-token.secret.yaml` → `forgejo-token.yaml`, `keycloak.secret.yaml` → `keycloak.yaml`;
  - `deployment.yaml`, `service.yaml`, `servicemonitor.yaml`, `prometheusrule.yaml`, `networkpolicy.yaml`.
- Create: `cluster/applications/forge-bot.yaml`; Modify: `cluster/applications/kustomization.yaml`

**Files (`realliance/sso`):**
- Modify: `tests/forge_helpers.py` (`prom_query`, `firing`)
- Extend: `tests/forge/test_forge_bot.py`, `tests/forge/test_forge_ci.py`

**Interfaces:**
- Consumes: Tasks 8–14 merged, Task 15, `FORGE_BOT_FORGEJO_TOKEN`, `forge_bot_client_secret`.
- Produces: Deployment `forge-bot/forge-bot` (all modes `dry-run`), ConfigMaps `forge-bot-config` and `forge-bot-keyring` (hash-suffixed, so a mode change restarts the pod), PrometheusRule `forge-bot-alerts`.

- [ ] **Step 1: Chris merges PR `v1` into `main` (fast-forward).** The `main` run builds `git.realliance.net/realliance-ops/forge-bot:sha-<7>`. Expected: run `success`, with the job log ending in `…@sha256:<digest>`. Fallback (spec "Bootstrap") if the runner cannot publish: an Owner runs the same `ko build` locally from that commit with the `ops-ci` token (`OPS_CI_PACKAGE_TOKEN`), with `DOCKER_CONFIG` in a temp dir. Ledger which path built the image.

- [ ] **Step 2: STOP: Chris reviews the release.** `git -C ~/Repos/forge-bot fetch && git verify-commit <sha> && git log -p <initial-commit>..<sha>` (later re-pins review `<pinned>..<new>`). Proceed only on his OK. Link the package to the repo: `POST /api/v1/packages/realliance-ops/container/forge-bot/-/link/forge-bot`.

- [ ] **Step 3: Write the failing tests**

```python
BOT_DIR = LUMA / "cluster/forge-bot"

def test_forge_bot_manifests_hardened():                          # Success 17
    # same pod/container checks as test_runner_manifests_unprivileged, plus readOnlyRootFilesystem True,
    # requests {cpu: 20m, memory: 32Mi}, limits.memory 128Mi, replicas 1, strategy Recreate,
    # image matches r"^git\.realliance\.net/realliance-ops/forge-bot:sha-[0-9a-f]{7}@sha256:[0-9a-f]{64}$"

def test_forge_bot_networkpolicy_present():
    np = next(x for x in manifests(BOT_DIR) if x["kind"] == "NetworkPolicy")
    assert np["spec"]["policyTypes"] == ["Egress"]
    text = yaml.safe_dump(np["spec"]["egress"])
    assert "23.163.0.48/32" in text and "3000" in text and "53" in text

def test_forge_bot_config_public_safe():
    cfg = yaml.safe_load((BOT_DIR / "config.yaml").read_text())
    assert set(cfg["modes"].values()) <= {"off", "dry-run", "apply"}
    assert cfg["inventory"]["repo"] == "realliance-ops/forge" and cfg["forgejo"]["source_id"] == 1

def test_workflow_triggers_and_pins():                            # Success 17 static
    wf = yaml.safe_load(requests.get(f"{FORGE}/{FORGE_BOT_REPO}/raw/branch/main/.forgejo/workflows/ci.yaml", timeout=20).text)
    on = wf[True] if True in wf else wf["on"]                     # PyYAML reads `on:` as True
    assert set(on) == {"push", "pull_request"} and on["push"] == {"branches": ["main"], "tags": ["v*"]}
    assert all(j["runs-on"] == "galaxy" for j in wf["jobs"].values())
    uses = [s["uses"] for j in wf["jobs"].values() for s in j["steps"] if "uses" in s]
    assert uses and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u) for u in uses)

@galaxy_only
def test_main_push_builds_and_galaxy_runs_pin():                  # Success 14
    head = forge_api("GET", f"/api/v1/repos/{FORGE_BOT_REPO}/branches/main").json()["commit"]["id"]
    assert any(r["status"] == "success" for r in runs_for(FORGE_BOT_REPO, head))
    pinned = re.search(r"forge-bot:(sha-[0-9a-f]{7})@(sha256:[0-9a-f]{64})", (BOT_DIR / "deployment.yaml").read_text())
    m = anonymous_manifest(FORGE_BOT_REPO, pinned.group(1))
    assert m.status_code == 200 and m.headers["Docker-Content-Digest"] == pinned.group(2)
    pod = json.loads(kube("-n", "forge-bot", "get", "pods", "-o", "json").stdout)["items"][0]
    assert pod["status"]["containerStatuses"][0]["imageID"].endswith(pinned.group(2))

@galaxy_only
def test_no_registry_credential_left_on_runner():                 # Review Focus 4
    r = kube("-n", "forgejo-runner", "exec", "deploy/forgejo-runner", "--", "sh", "-c",
             "find /work -path '*docker*' -name config.json | head -1")
    assert r.returncode == 0 and r.stdout.strip() == ""

@galaxy_only
def test_dry_run_cycle_healthy():
    assert {x["metric"]["source"]: x["value"][1] for x in prom_query("forge_bot_read_ok")} == {"sso": "1", "forgejo": "1", "inventory": "1"}
    assert {x["value"][1] for x in prom_query("forge_bot_job_mode")} == {"1"}
    assert prom_query('forge_bot_inventory_ok{check="signature"}')[0]["value"][1] == "1"
```

- [ ] **Step 4: Run them to verify they fail**

Run: `nix develop --command pytest tests/forge/test_forge_bot.py tests/forge/test_forge_ci.py -k "manifests_hardened or networkpolicy or config_public or workflow or main_push or registry_credential or dry_run" -v`
Expected: FAIL (no `cluster/forge-bot`; no pod). `test_workflow_triggers_and_pins` may already pass.

- [ ] **Step 5: Confirm the source id (S1).** `kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin auth list`: the `realliance` row must have ID 1, or else use its ID everywhere below and ledger it.

- [ ] **Step 6: Write the manifests.**
  - `namespace.yaml`: `forge-bot`, `enforce: baseline`.
  - `config.yaml`: values per the Task 8 `Config` (snake_case):
    - `sso`: `https://sso.realliance.net`, realm `realliance`, client `forge-bot`, `client_secret_file: /etc/forge-bot/secrets/keycloak-client-secret`, `access_group: /apps/forgejo`, `admin_group: /forgejo-admins`;
    - `forgejo`: `url: http://forgejo-http.forgejo.svc.cluster.local:3000`, `public_url: https://git.realliance.net`, `token_file: /etc/forge-bot/secrets/forgejo-token`, `source_id: 1`;
    - `inventory`: `realliance-ops/forge`, `main`, `/etc/forge-bot/keyring/owners.asc`, `/etc/forge-bot/keyring/fingerprints.txt`;
    - all five modes `dry-run`, the default limits, `listen: ":8080"`.
  - `keyring/owners.asc`: copy of `~/Repos/realliance-sso/keys/chris.asc`. `keyring/fingerprints.txt`: `E9245FED84E0EC721C7B5C336BFB8037115ADE26  # chris`; Connor's key is added when he supplies it. Header comment: the keyring lives here, not in the inventory (spec "Trusted inventory").
  - Secrets (each written by a script from `sso-secrets`, then `./sign.sh`):
    - `forge-bot-forgejo`, key `forgejo-token`;
    - `forge-bot-keycloak`, key `keycloak-client-secret`.
  - `deployment.yaml`:
    - 1 replica, `Recreate`, the pinned image, args `--config=/etc/forge-bot/config/config.yaml`;
    - pod `runAsNonRoot`, `runAsUser: 65532`, `RuntimeDefault`, `automountServiceAccountToken: false`, `enableServiceLinks: false`;
    - container: no escalation, `drop: [ALL]`, `readOnlyRootFilesystem: true`, port `http` 8080;
    - `livenessProbe` `httpGet /healthz` (`initialDelaySeconds: 30, periodSeconds: 60, failureThreshold: 2`);
    - requests `20m/32Mi`, limit `memory: 128Mi`;
    - mounts at `/etc/forge-bot/config`, `/etc/forge-bot/keyring` and `/etc/forge-bot/secrets` (a projected volume of both secrets, `defaultMode: 0400`).
  - `service.yaml`: port `http`. `servicemonitor.yaml`: label `release: prometheus`, `interval: 60s`.
  - `networkpolicy.yaml`: `policyTypes: [Egress]`; egress to:
    - kube-dns (ns `kube-system`, `k8s-app: kube-dns`, 53 UDP/TCP);
    - Forgejo pods (ns `forgejo`, `app.kubernetes.io/name: forgejo`, `app.kubernetes.io/instance: forgejo`, TCP 3000);
    - `ipBlock 23.163.0.48/32` TCP 443.
    - Comment: flannel does not enforce this; it records intent (spec, Chris 2026-11-02).
  - `prometheusrule.yaml`: labels `prometheus: kube-prometheus`, `role: alert-rules`, `release: prometheus`; group `forge-bot`, interval 60s; summaries name the runbook section `docs/forgejo.md#forge-bot`:

  | alert | expr | for | severity |
  |---|---|---|---|
  | ForgeBotBreakerTripped | `max by (job) (forge_bot_breaker_tripped) > 0` | 0m | critical |
  | ReallianceSSOUnreadable | `forge_bot_read_ok{source="sso"} == 0` | 20m | critical |
  | ForgeBotOwnerNeedsAction | `forge_bot_owner_needs_action > 0` | 0m | critical |
  | ForgeBotUnexpectedSSOWrite | `increase(forge_bot_unexpected_sso_writes_total[15m]) > 0` | 0m | critical |
  | ForgeBotStale | `time() - forge_bot_cycle_last_success_timestamp > 1800 or absent(forge_bot_cycle_last_success_timestamp)` | 5m | warning |
  | ForgeBotNotConverging | `(forge_bot_planned_changes > 0) and on (job) (forge_bot_job_mode == 2)` | 15m | warning |
  | ForgeBotInventoryUntrusted | `forge_bot_inventory_ok{check="signature"} == 0` | 5m | warning |
  | ForgeBotInventoryInvalid | `forge_bot_inventory_ok{check=~"teams\|bot_config"} == 0` | 10m | warning |

  - `kustomization.yaml`: the resources plus the two configMapGenerators (hash suffix kept). `cluster/applications/forge-bot.yaml`: the shape of `immich.yaml`, listed in `cluster/applications/kustomization.yaml`.

- [ ] **Step 7: STOP — ask Chris (deploy). Then commit and push.**

```bash
nix develop --command bash -c 'scripts/check-kustomizations.sh'
nix develop --command git add cluster/forge-bot cluster/applications/forge-bot.yaml cluster/applications/kustomization.yaml
nix develop --command git commit -m "forge-bot: deploy sha-<7> in dry-run (reviewed <initial>..<sha>, verify-commit OK)"
git push
```
Monitor the rollout to Ready, then `scripts/check-prometheus-rules.sh`. Expected: `forge-bot-alerts` loaded, no FAIL.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `nix develop --command pytest tests/forge/test_forge_bot.py tests/forge/test_forge_ci.py -v`
Expected: PASS. Pod logs show dry-run audit lines and no errors (read them in the terminal only; they name people).

- [ ] **Step 9: Commit** the sso tests (branch, PR, merge on Chris's word)

---

### Task 17: Dry-run gate, then profile → avatar → access to `apply` (C3)

**Files:**
- Modify (`luma-homeops`): `cluster/forge-bot/config.yaml` (three commits)
- Modify (`realliance/sso`): `tests/conftest.py` (`module_forge_users`, marker hook), `pytest.ini` (`pages`, `drill`), `tests/forge_helpers.py` (`kc_user`, `kc_update`, `set_enabled`, `bad_passwords`)
- Extend: `tests/forge/test_forge_bot.py`
- Modify: `tests/forge/test_forge_access.py` (delete `test_offboarding_blocks_web_token_and_ssh_and_is_reversible` and `_set_kc_enabled`/`_prohibit_login`; the manual two-step would race forge-bot)

**Interfaces:**
- Consumes: Task 16.
- Produces: `profile`, `avatar` and `access` in `apply`.

- [ ] **Step 1: Write the tests** (they fail while the jobs are in dry-run)

```python
@pytest.fixture(scope="module")
def world(module_forge_users, tmp_path_factory):
    """One batch, so one wait covers many tests. At most 2 people lose access in it (the breaker allows
    min(3, max(1, N//3)))."""
    w = {k: module_forge_users() for k in ("off", "rm", "prof", "unver", "lock", "new")}
    sessions = {k: forge_login(u) for k, u in w.items()}
    w["session"] = sessions["off"]
    w["token"] = forge_token(sessions["off"], "off", ["read:user", "write:repository"])
    # also, for "off": a private repo via Sudo and an ssh key via the admin API (as the old offboarding test did)
    set_enabled(w["off"], False)
    set_groups(w["rm"], [])
    kc_update(w["prof"], firstName="Ada", lastName="Lovelace", email=f"{w['prof'].name}-new@example.invalid",
              emailVerified=True, attributes={"pronouns": ["they/them"]})
    kc_update(w["unver"], email=f"{w['unver'].name}-unv@example.invalid", emailVerified=False)
    # 10 failures lock the user (realm: max_login_failures 10), but only for 60 s. 150 keep the lock for
    # min(60 s × 150/10, 900 s) = 900 s, so it spans two cycles.
    bad_passwords(w["lock"], 150)
    w["t0"] = time.time()
    return w

def settle(w):  # nothing-happens tests: wait until two full cycles have passed since the batch
    time.sleep(max(0, w["t0"] + CYCLE_WAIT - time.time()))

def test_offboarding_within_two_cycles(world):                    # Success 1
    # wait_for (web, api, ssh) == (False, False, False); then set_enabled(off, True) and
    # wait_for all three True again (web via a fresh forge_login)

def test_removed_from_access_group_loses_access(world):           # Success 1
    assert wait_for(lambda: forge_api("GET", f"/api/v1/users/{world['rm'].name}").json()["prohibit_login"])

def test_profile_follows_sso(world):                              # Success 5
    p = world["prof"]
    assert wait_for(lambda: (lambda a: (a["full_name"], a["email"], a["pronouns"]) ==
                    ("Ada Lovelace", f"{p.name}-new@example.invalid", "they/them"))(forge_api("GET", f"/api/v1/users/{p.name}").json()))

def test_unverified_email_never_synced(world):                    # Success 5
    settle(world)
    assert forge_api("GET", f"/api/v1/users/{world['unver'].name}").json()["email"] == f"{world['unver'].name}@example.invalid"

def test_lockout_not_prohibited(world):                           # Success 9
    u = world["lock"]
    assert get(f"/admin/realms/realliance/attack-detection/brute-force/users/{u.id}", admin_token()).json()["disabled"] is True
    settle(world)
    assert forge_api("GET", f"/api/v1/users/{u.name}").json()["prohibit_login"] is False

def test_avatar_picture_set_and_served(world, acceptance_clients):  # Success 6
    u = world["new"]
    url = f"{FORGE}/{u.name}.png"
    assert wait_for(lambda: kc_user(u).get("attributes", {}).get("picture") == [url])
    r = requests.get(url, timeout=20)
    assert r.status_code == 200 and r.headers["Content-Type"].startswith("image/")
    set_groups(u, ["/apps/forgejo", "/apps/acceptance-tests"])
    assert decode(password_grant("realliance", "acceptance-tests", u.name, u.pw).json()["id_token"])["picture"] == url

def test_local_accounts_never_prohibited():                       # Success 13
    for login in ("forgejo-admin", "forge-bot", "forge-ci"):
        a = forge_api("GET", f"/api/v1/users/{login}").json()
        assert a["source_id"] == 0 and a["prohibit_login"] is False

@pytest.mark.pages
def test_owner_never_auto_prohibited(forge_user):                 # Success 13; fires ForgeBotOwnerNeedsAction
    # scratch org test-<hex> (admin); add u to its Owners team; set_groups(u, []);
    # after CYCLE_WAIT: u not prohibited and firing("ForgeBotOwnerNeedsAction"); finally delete the org

@pytest.mark.pages
@pytest.mark.slow
def test_breaker_holds_bulk_loss(forge_user):                     # Success 8; fires ForgeBotBreakerTripped
    us = [forge_user() for _ in range(4)]
    for u in us: forge_login(u)
    for u in us: set_groups(u, [])
    time.sleep(CYCLE_WAIT)
    assert not any(forge_api("GET", f"/api/v1/users/{u.name}").json()["prohibit_login"] for u in us)
    assert firing("ForgeBotBreakerTripped")
    if drill_enabled("bulk"):   # an Owner merges a signed forge-bot.yaml with allow_bulk_until = now + 2 h
        print("DRILL bulk: merge a signed allow_bulk_until commit to realliance-ops/forge now")
        assert wait_for(lambda: all(forge_api("GET", f"/api/v1/users/{u.name}").json()["prohibit_login"] for u in us),
                        timeout=CYCLE_WAIT + 1800)
```

- [ ] **Step 2: Run them to verify they fail** (all modes still dry-run)

Run: `nix develop --command pytest tests/forge/test_forge_bot.py -v`
Expected: FAIL on offboarding, removal, profile and avatar; PASS on lockout and local accounts (nothing changes in dry-run).

- [ ] **Step 3: Dry-run gate (≥ 24 h after Task 16's deploy).**
  - `prom_query("min_over_time(forge_bot_read_ok[24h])")` is all `1`, and `sum(increase(forge_bot_errors_total[24h]))` is `0`.
  - Every planned admin or team change in the dry-run audit lines is real drift (check each against sso-people and the Forgejo UI).
  - Ledger counts and reasons only: no names, no teams.
  - If the gate fails: **STOP — ask Chris.**

- [ ] **Step 4: `profile` → `apply`.** One commit (`forge-bot: profile → apply`), push, then Monitor one cycle:
  - `increase(forge_bot_applied_changes_total{job="profile"}[10m])` equals the previously planned count;
  - the next cycle's `forge_bot_planned_changes{job="profile"}` is `0`;
  - `forge_bot_errors_total{job="profile"}` is flat.

- [ ] **Step 5: `avatar` → `apply`.** Same check for `job="avatar"`. Also check Keycloak's admin events: the forge-bot events are `USER` `UPDATE` only.

- [ ] **Step 6: STOP — ask Chris, then `access` → `apply`.** Same check. Anyone it prohibits must be someone the SSO already disables; otherwise revert to `dry-run` and report.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `nix develop --command pytest tests/forge/test_forge_bot.py tests/forge/test_forge_access.py -v` (then, after telling Connor, `FORGE_PAGES_OK=1 FORGE_DRILL=bulk` with Chris doing the bulk drill)
Expected: PASS. Ledger the bulk-drill time and the signed revert that resets `allow_bulk_until: null`.

- [ ] **Step 8: Commit** the sso tests (branch, PR, merge on Chris's word)

---

### Task 18: `admin` and `teams` to `apply`; drills 10, 11, 12 and steady state (C4)

**Files:**
- Modify (`luma-homeops`): `cluster/forge-bot/config.yaml` (two commits; the SSO-outage drill adds and then reverts `hostAliases` in `deployment.yaml`)
- Extend (`realliance/sso`): `tests/forge/test_forge_bot.py`
- Modify: `tests/forge/test_forge_access.py` (delete `test_demotion_needs_the_forgejo_side_step`; demotion is now one SSO step, covered below)

**Interfaces:**
- Consumes: Task 17. Precondition: `forge_bot_inventory_ok{check="signature"} == 1` and `{check="teams"} == 1` (the inventory move and the signed-head check are live).

- [ ] **Step 1: Write the tests**

```python
def test_admin_follows_group_without_signin(forge_user):          # Success 2
    u = forge_user(); forge_login(u)
    set_groups(u, ["/apps/forgejo", "/forgejo-admins"])
    assert wait_for(lambda: forge_api("GET", f"/api/v1/users/{u.name}").json()["is_admin"] is True)
    set_groups(u, ["/apps/forgejo"])
    assert wait_for(lambda: forge_api("GET", f"/api/v1/users/{u.name}").json()["is_admin"] is False)

def test_team_follows_groups_without_signin(forge_user):          # Success 3
    u = forge_user(groups=("/apps/forgejo", "/realliance")); forge_login(u)
    assert wait_for(lambda: u.name in team_members("realliance", "members"))
    set_groups(u, ["/apps/forgejo", "/realliance-maintainers"])
    assert wait_for(lambda: u.name in team_members("realliance", "maintainers")
                    and u.name not in team_members("realliance", "members"))

def test_owners_and_unlisted_teams_untouched(forge_user):         # Success 3
    # scratch org test-<hex> with team "crew" (admin) holding u; owners0 = team_members("realliance", "Owners")
    # set_groups(u, ["/apps/forgejo", "/realliance"]) and wait_for u in realliance/members (proves a cycle ran)
    # assert u in crew and team_members("realliance", "Owners") == owners0; finally delete the org

def test_forge_bot_admin_events_change_only_picture():            # Success 7
    # all realm admin events (paged, tofu-apply token) sorted by time; forge-bot's = authDetails.userId == its SA id
    # each forge-bot event: USER / UPDATE / users/<id>; its identity fields (username, email, enabled, emailVerified,
    # requiredActions, firstName, lastName, attributes minus picture) equal the previous event's for that user (any
    # client) when one exists; assert at least one event was checked

@pytest.mark.drill("steady")
def test_steady_state_zero_writes():                              # Success 7 (operator: an hour with no SSO changes or tests)
    assert prom_query("sum(increase(forge_bot_applied_changes_total[1h]))")[0]["value"][1] in ("0", "0.0")
    # and no forge-bot admin events in the last hour

@pytest.mark.drill("untrusted")
def test_untrusted_head_drill():                                  # Success 11 (an Owner edits forge-bot.yaml in the web UI → FF merge)
    assert wait_for(lambda: firing("ForgeBotInventoryUntrusted"), timeout=CYCLE_WAIT + 600)
    print("DRILL untrusted: now merge a signed revert")
    assert wait_for(lambda: prom_query('forge_bot_inventory_ok{check="signature"}')[0]["value"][1] == "1", timeout=CYCLE_WAIT + 1800)

@pytest.mark.drill("foreign-write")
@pytest.mark.pages
def test_foreign_write_fires_alert(user_factory):                 # Success 12 (Chris)
    u = user_factory()
    t = admin_token("forge-bot")
    rep = get(f"/admin/realms/realliance/users/{u.id}", t).json(); rep["lastName"] = "Changed"
    assert put(f"/admin/realms/realliance/users/{u.id}", t, rep).status_code == 204
    assert wait_for(lambda: firing("ForgeBotUnexpectedSSOWrite"), timeout=CYCLE_WAIT + 120)

@pytest.mark.drill("sso-outage")
@pytest.mark.pages
def test_sso_outage_drill():                                      # Success 10 (Chris: hostAliases commit is live)
    t0 = time.time()
    assert wait_for(lambda: firing("ReallianceSSOUnreadable"), timeout=1800)
    assert time.time() - t0 <= 1800
    assert prom_query("sum(increase(forge_bot_applied_changes_total[30m]))")[0]["value"][1] in ("0", "0.0")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command pytest tests/forge/test_forge_bot.py -k "admin_follows or team_follows or untouched or only_picture" -v`
Expected: `test_admin_follows_group_without_signin` and `test_team_follows_groups_without_signin` FAIL (dry-run, no sign-in); the others PASS.

- [ ] **Step 3: STOP — ask Chris, then `admin` → `apply`** (one commit). Monitor one cycle as in Task 17 Step 4. With the login source still mapping, the planned count should be 0 or real drift only.

- [ ] **Step 4: STOP — ask Chris, then `teams` → `apply`** (one commit). Same check.

- [ ] **Step 5: Run the non-drill tests to verify they pass**

Run: `nix develop --command pytest tests/forge/test_forge_bot.py tests/forge/test_forge_access.py tests/forge/test_forge_inventory.py -v`
Expected: PASS

- [ ] **Step 6: Drills.**
  - **`untrusted` (an Owner):** a web-UI edit of `forge-bot.yaml` on a branch, a PR, then an FF merge (the head is Forgejo-signed); run the test; then a signed revert.
  - **`foreign-write` (Chris):** run the test (it uses the SA secret from `host/secrets.yml`).
  - **`sso-outage` (Chris; STOP — ask Chris first):**
    - commit `hostAliases: [{ip: 192.0.2.1, hostnames: [sso.realliance.net]}]` on the forge-bot pod and push;
    - run the test;
    - revert the commit and push; Monitor until `forge_bot_read_ok{source="sso"} == 1`.
  - **`steady`:** after an hour without SSO changes or test runs.
  - Run each with `FORGE_DRILL=<name>` (and `FORGE_PAGES_OK=1` where marked), after telling Connor. Ledger each result and time.

- [ ] **Step 7: Commit** the sso tests (branch, PR, merge on Chris's word)

---

### Task 19: Clear the login-source map, finish the tests, docs (C5–6)

**Files:**
- Modify (`realliance/sso`): `tests/forge_helpers.py` (`next_cycle`), `tests/forge/test_forge_access.py`, `test_forge_signin.py`, `test_forge_inventory.py`, `README.md` (any remaining "forge-sync")
- Modify (`luma-homeops`): `cluster/applications/forgejo-release.yaml` (comments), `docs/forgejo.md`, `CLAUDE.md` (sharp edge 6: forge-bot and forgejo-runner are images we build and pin by hand)
- Modify (inventory): `README.md` (if anything is left from Tasks 3–4)
- Wiki: `github.com/realliance/sso/wiki` pages Forge-Operations, Gotchas, Accounts-and-Profiles

**Interfaces:**
- Consumes: Task 18 (admin and teams in `apply`, with a cycle planning zero admin or team changes).

- [ ] **Step 1: Write the tests**
  - `test_signin_writes_nothing(forge_user)` (Success 4):
    1. u is in `/realliance` and `wait_for` puts them in members.
    2. `next_cycle()`, then within the window: remove u from `realliance/members` through the API, `forge_relogin`, and assert u is **not** in members.
    3. `wait_for` puts u back in members.
  - Rewrites:
    - `test_admin_group_makes_site_admin`: `wait_for(is_admin)` after `forge_login`;
    - `_maintainer()` in `test_forge_inventory.py`: `wait_for(u in maintainers)`;
    - delete `test_team_follows_groups_at_signin` and `test_admin_status_dropped_at_next_signin` (replaced by Task 18's tests);
    - keep `test_team_map_not_in_public_config` and `test_self_set_profile_attributes_survive_apply`.

- [ ] **Step 2: Run to verify `test_signin_writes_nothing` fails** (the map still writes at sign-in)

Run: `nix develop --command pytest tests/forge/test_forge_access.py::test_signin_writes_nothing -v`
Expected: FAIL (u is back in members straight after sign-in)

- [ ] **Step 3: Gate.** `prom_query('forge_bot_planned_changes{job=~"admin|teams"}')` is all `0`. Record the "before" state: the Task 4 Step 1 query (non-secret fields only), copied into `docs/forgejo.md`.

- [ ] **Step 4: STOP — Chris clears the map on the pod.**
  - `kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin auth update-oauth --id 1 --group-team-map '' --group-team-map-removal=false --admin-group ''`. Confirm the flags with `--help` first; keep `--group-claim-name groups`.
  - Re-run the Step 3 query and record "after" plus the exact rollback command, `update-oauth --id 1 --admin-group forgejo-admins --group-team-map '<before>' --group-team-map-removal`.

- [ ] **Step 5: Run the whole suite to verify it passes**

Run: `nix develop --command pytest tests/forge -v --hosts=ssh://root@23.163.0.48`
Expected: PASS (drills and paging tests skipped)

- [ ] **Step 6: Docs.**
  - `forgejo-release.yaml` header and `oauth` comments: drop the map. Sign-in only creates accounts; forge-bot owns admin and teams (`teams.yaml` in the private inventory).
  - `docs/forgejo.md`:
    - "Who gets in": access, admin and teams sync within ~5 min; removal is one SSO step; email syncs; the hidden-org gate is met; the inventory is at `realliance-ops/forge`.
    - The manual two-step becomes "fallback while forge-bot is down or its breaker has tripped".
    - A new **forge-bot** section: jobs, modes and how to flip one, `allow_bulk_until` (signed Owner commit, ≤ 24 h), alerts and their routes, `picture` (edit profiles in the SSO, suspend with `active: false`), token and secret rotation, the re-pin review (`git verify-commit` + `git log -p <pinned>..<new>`, the range named in the pin commit), and rollback (modes → `dry-run` + the recorded command).
    - A new **CI** section: Actions settings, the runner and its allowlist (registration per repo, rotation by re-registering), the `galaxy` label only, the B0 outcome and `REGISTRY_TOKEN` (since 2026-11-04 the `realliance-ops` org secret, `ops-ci`), never approving fork PRs, the accepted branch-push risk and its revisit trigger, the Forgejo 15 Actions differences.
    - Clearing runner state: the work PVC outlives the pod, so empty `/work/cache` and `/work/act` with `kubectl exec`, then restart the pod.
  - `realliance/sso` README: no "forge-sync" left.
  - Wiki (STOP — ask Chris; public): the three pages get the same changes.
  - `CLAUDE.md` sharp edge 6: add `forgejo-runner` (ghcr) and `forge-bot` (built on the forge, `git.realliance.net`).

- [ ] **Step 7: Commit**

```bash
cd ~/Repos/realliance-sso && git switch -c forge-bot-cutover && git add tests README.md && git commit -S -m "tests: forge-bot owns admin and teams; sign-in writes nothing" && gh pr create --fill   # merge on Chris's word
cd ~/Repos/luma-homeops && nix develop --command git add cluster/applications/forgejo-release.yaml docs/forgejo.md CLAUDE.md && nix develop --command git commit -m "forgejo: forge-bot owns access, admin and teams; CI runbook" && git push
```

- [ ] **Step 8: Memory.** Update `project_realliance_forge.md`: project 3 live, forge-bot modes, B0 outcome, `realliance-ops/forge`, open items (Connor's key, later jobs). Update its line in MEMORY.md.
