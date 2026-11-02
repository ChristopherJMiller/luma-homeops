# Realliance Forge (phases 2a + 2b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Forgejo 15 LTS at `https://git.realliance.net` on galaxy, sign-in only through Realliance SSO, git over SSH on WAN port 22, LFS and other binaries in Realliance B2, nightly off-site backups that either operator can restore, and the `realliance/forge` Terraform inventory owning orgs and teams.

**Architecture:** Three repos move together. `realliance/sso` gains the B2 buckets and keys, the `forgejo` OIDC client and two groups, plus the acceptance suite (`tests/forge/`). `luma-homeops` gains `cluster/forgejo/` (plumbing: namespace, two PVCs, Postgres, sealed secrets, backup CronJob), an Argo Helm release, and the router NAT/firewall rules. `realliance/forge` (private, on Forgejo itself) holds the Terraform inventory. Storage is tiered: Postgres and Forgejo's working data on `rook-ceph-block-ssd`, bare repos on `rook-ceph-block` (HDD), all binaries in B2.

**Tech Stack:** forgejo-helm 17.3.2 (Forgejo 15.0.11, rootless image), Argo CD v3.5.3, postgres-operator v1.11 (Spilo PG16), Rook-Ceph RBD, Traefik v3, MetalLB, VyOS via `router/ansible`, OpenTofu 1.12.6 with `Backblaze/b2` 0.14.1, `keycloak/keycloak` 5.10.0 and `svalabs/forgejo` 1.7.0, pytest (`realliance/sso` `nix develop`), rclone 1.75.1, git-lfs.

**Spec:** `docs/superpowers/specs/2026-11-02-realliance-forge-design.md` (read its 2026-11-02 amendment at the end).

## Global Constraints

- Forgejo **15 LTS**: chart `forgejo` **17.3.2** from OCI registry `code.forgejo.org/forgejo-helm` (appVersion **15.0.11**). One replica, strategy `Recreate`.
- Namespace **`forgejo`**; manifests under **`luma-homeops/cluster/forgejo/`**; Argo Applications **`forgejo`** (plumbing) and **`forgejo-release`** (Helm).
- Host **`https://git.realliance.net`**; SSH **`git@git.realliance.net:<org>/<repo>.git`** on port **22**, MetalLB IP **`192.168.0.6`**.
- Postgres: `postgres-operator` cluster **`acid-forgejo`**, version **'16'**, **2** instances, **10Gi**, `enableLogicalBackup: true`, storage class `rook-ceph-block-ssd`.
- B2 (Realliance account, region **`us-east-005`**, S3 endpoint `https://s3.us-east-005.backblazeb2.com`): objects bucket **`realliance-forge-objects`**, object-locked backups bucket **`realliance-forge-backups`**, both created in `realliance/sso` `b2/`.
- Storage tiers (Chris, 2026-11-02): Postgres and Forgejo's `/data` on **`rook-ceph-block-ssd`**; bare repos (`[repository] ROOT=/repos`) on **`rook-ceph-block`**; LFS, packages, attachments, avatars and archives in B2.
- OIDC: Forgejo auth source **`realliance`**, discovery `https://sso.realliance.net/realms/realliance/.well-known/openid-configuration`, redirect **`https://git.realliance.net/user/oauth2/realliance/callback`**, access group **`/apps/forgejo`**, admin group **`forgejo-admins`**, group claim **`groups`**; group→team map with removal for **non-Owners teams only**.
- app.ini (spec): `[service] DISABLE_REGISTRATION=false`, `ALLOW_ONLY_EXTERNAL_REGISTRATION=true`, `ENABLE_INTERNAL_SIGNIN=false`; `[oauth2_client] ENABLE_AUTO_REGISTRATION=true`, `ACCOUNT_LINKING=disabled`, `USERNAME=nickname`; `[session] SESSION_LIFE_TIME=604800`; `[repository] DEFAULT_PRIVATE=private`; user list hidden from anonymous visitors.
- Mail: Resend, a **separate sending-only key restricted to realliance.net**, sender **`git@realliance.net`**, notifications on.
- Break-glass local admin **`forgejo-admin`**, password in `realliance/sso-secrets`. No other Forgejo-local passwords.
- Router: `router/ansible` only, never hand-`set`; every `site.yml` run is inside the commit-confirm parachute; Chris approves the run (CLAUDE.md S4/S8). WAN IP **`152.44.247.88`** is hardcoded in reflection rules (inherited limitation).
- Hidden-project names never appear in `luma-homeops`, `realliance/sso` or any public config. The team map may stay in public chart values **only while every mapped org is public**.
- Secrets are never printed, echoed, put in argv, or put in assertion messages. `luma-homeops` and `realliance/sso` are public; `realliance/sso` holds no secrets, not even ciphertext. In git-crypt repos use `--no-textconv` for any diff that leaves the terminal.
- Signed commits everywhere; never `--no-verify` (S6). Export `DISPLAY=:10.0 XAUTHORITY=$HOME/.Xauthority` in every shell that signs or uses ssh.
- Images pinned `tag@sha256:digest`, versions at least two weeks old.
- forge-sync is deferred: offboarding is the two-step runbook, and the gap is documented, not hidden.
- Outward actions: at execution start, get Chris's OK (AskUserQuestion) for pushes to `luma-homeops` main, PR merges in `realliance/sso` and `realliance/sso-people`, and pushes to `realliance/sso-secrets`. The router run (Task 5) and the drill teardown (Task 9) each need their own OK.

## Decisions this plan makes (not in the spec)

- **Router SSH stays on all interfaces (spec correction).** The router runs Tailscale (`tailscale0`, `100.96.171.43`), so the spec's `service ssh listen-address 192.168.0.1` would cut off router SSH over the tailnet. The WAN destination-NAT for TCP 22 already sends every WAN SSH connection to Forgejo before the router's sshd sees it, which achieves the spec's intent.
- **Team map:** claim group `realliance` → team `realliance/members`; claim group `realliance-maintainers` → team `realliance/maintainers`. `realliance-maintainers` and `forgejo-admins` are public top-level realm groups.
- **Org structure is code:** `[admin] DISABLE_REGULAR_ORG_CREATION=true` and `[service] DEFAULT_ALLOW_CREATE_ORGANIZATION=false`.
- **No password auth anywhere:** `[service] ENABLE_BASIC_AUTHENTICATION=false` (tokens still work for git over HTTPS).
- **User list:** `[service.explore] DISABLE_USERS_PAGE=true`. Public orgs and repos stay listed in Explore.
- **Emails private by default:** `[service] DEFAULT_KEEP_EMAIL_PRIVATE=true`.
- **All non-repo storage in B2:** `[storage] STORAGE_TYPE=minio` as the default for every subsystem.
- **Objects key without `deleteFiles`:** Forgejo can hide objects but cannot destroy versions; a lifecycle rule purges hidden versions after 30 days. If B2 refuses S3 `DeleteObject` without `deleteFiles`, add it and ledger the ruling.
- **Repo backups:** a CronJob streams `forgejo dump` (repos, data, DB, app.ini; no LFS, attachments or packages) out of the running pod via `kubectl exec` into `realliance-forge-backups/dump/`. One self-contained artifact is what a restore on a new host needs.
- **App secrets pre-generated:** `SECRET_KEY`, `INTERNAL_TOKEN`, `JWT_SECRET` and `LFS_JWT_SECRET` are generated once and kept in `realliance/sso-secrets` `forge/secrets.env` (canonical copy, so Connor can restore), then sealed into `luma-homeops`.
- **Acceptance suite lives in `realliance/sso` `tests/forge/`.** It already has the Keycloak admin, user factory and TOTP machinery. Every test checks external behaviour (HTTP, SSH, B2, APIs), so either operator can run it.
- **Org-wide policy declarations wait for forge-bot (2c)**, their only consumer. 2b's inventory owns orgs, teams, Owners and critical repos.
- **`git gc` cron stays off** (`[cron.git_gc_repos] ENABLED=false`, Forgejo's default, made explicit): full repacks are the one bulk write repos make on the HDD pool.

## Review Focus

1. **Username collides with an existing org or user** (an sso-people username equal to an org name like `realliance`). Expected: sign-in fails with Forgejo's error, and nothing is linked or granted (`ACCOUNT_LINKING=disabled`). Test: Task 4 `test_username_collision_refused`.
2. **Email changed in Keycloak after first sign-in.** Expected: the same Forgejo account (matched by OIDC `sub`) keeps working; no second account. Test: Task 8 `test_email_change_keeps_account`.
3. **A person with `/apps/forgejo` but no community group.** Expected: they sign in, land in no team, and are not admin. Test: Task 8 `test_access_without_community_group_gets_no_team`.
4. **LFS object larger than 100 MiB** (Cloudflare's proxy limit, Traefik timeouts). Expected: the push succeeds because `git.realliance.net` is DNS-only and Traefik's `readTimeout` is 0. Test: Task 4 `test_lfs_object_lands_in_b2` uses 120 MiB.
5. **Member transfers an org repo to themselves and then deletes it** (the loophole around "members cannot delete"). Expected: transfer refused. Test: Task 7 `test_member_cannot_transfer_org_repo`.

---

## Shared test helpers (`realliance/sso`)

Task 2 adds these to `tests/conftest.py` and `tests/forge_helpers.py`. Later tasks consume them by these names.

- `conftest.totp(raw_secret: str, at: float | None = None) -> str`: moved from `tests/realm/test_mfa.py::_totp` (that test imports it from conftest afterwards).
- `conftest.user_factory` → `make(groups=(), required_actions=(), attributes=None, username=None, email=None) -> User`. The new `username`/`email` parameters default to the current random values.
- `forge_helpers.FORGE = "https://git.realliance.net"`, `FORGE_SECRETS = SECRETS_DIR / "forge/secrets.env"`, `VPS = "root@23.163.0.48"`, `WAN_IP = "152.44.247.88"`, `FORGE_LB = "192.168.0.6"`.
- `forge_helpers.forge_api(method: str, path: str, *, token: str | None = None, sudo: str | None = None, **kw) -> requests.Response`: `token=None` means the admin token `FORGEJO_ADMIN_TOKEN` from `FORGE_SECRETS`. `sudo` sets the `Sudo` header (the admin acting as a user).
- `forge_helpers.forge_login_attempt(user: User) -> tuple[requests.Session, requests.Response]`: starts at `{FORGE}/user/oauth2/realliance`, posts the password, completes TOTP enrolment (users are created with `attributes={"mfa": ["totp"]}`) using `totp()`, and follows redirects. Returns the final response without asserting.
- `forge_helpers.forge_login(user: User) -> requests.Session`: `forge_login_attempt` plus an assertion that the final URL is on `FORGE` and the page shows the signed-in user.
- `forge_helpers.forge_relogin(session: requests.Session, user: User) -> requests.Session`: signs in again through SSO (Forgejo re-reads groups only at sign-in). It must answer the TOTP prompt for an already-enrolled user, so `User` gains a mutable field `totp_secret: str | None = None` that `forge_login_attempt` fills at enrolment.
- `forge_helpers.forge_token(session: requests.Session, name: str, scopes: list[str]) -> str`: creates a personal access token through `/user/settings/applications` (CSRF form) and returns it.
- `forge_helpers.ssh_keyscan(host: str, *, via: str | None = None) -> str`: returns the ed25519 host key, run locally or, with `via=VPS`, on the SSO box.
- `forge_helpers.on_lan() -> bool`, plus `lan_only = pytest.mark.skipif(not on_lan(), ...)`.
- `forge_helpers.b2_names(bucket: str, prefix: str) -> list[dict]`: `b2_list_file_names` with the Realliance master key from `b2/secrets.env`. Returns name, size and uploadTimestamp only.
- `forge_helpers.set_groups(user: User, paths: list[str]) -> None` and `forge_helpers.set_email(user: User, email: str) -> None`: replace the Keycloak user's groups / email through the admin API (what an sso-people change does).
- `forge_helpers.LUMA = Path.home() / "Repos/luma-homeops"`.
- Fixture `forge_user` (in `tests/conftest.py`) → `make(groups=("/apps/forgejo",), email=None, username=None) -> User`: wraps `user_factory` with `attributes={"mfa": ["totp"]}`. Teardown deletes each Forgejo account (`DELETE /api/v1/admin/users/{name}?purge=true`, 404 tolerated) before `user_factory` deletes the Keycloak user.

---

### Task 1: B2 buckets and keys for the forge (`realliance/sso` `b2/`)

**Files:**
- Create: `~/Repos/realliance-sso/b2/forge.tf`
- Modify: `~/Repos/realliance-sso/b2/outputs.tf`
- Test: `~/Repos/realliance-sso/tests/test_b2.py`

**Interfaces:**
- Produces: buckets `realliance-forge-objects` and `realliance-forge-backups`; keys `realliance-forge-objects` (rw, no delete), `realliance-forge-objects-ro`, `realliance-forge-backup` (writes to the backups bucket, no delete). Outputs `forge_objects_key_id`/`forge_objects_key`, `forge_objects_ro_key_id`/`forge_objects_ro_key`, `forge_backup_key_id`/`forge_backup_key` (secret halves `sensitive = true`).

- [ ] **Step 1: Write the failing tests** in `tests/test_b2.py`. Follow the file's no-keys-in-argv rule, and source keys from `tofu -chdir=b2 output -json` inside a fixture without printing them.

```python
def test_forge_buckets_private_and_encrypted(buckets):
    for name in ("realliance-forge-objects", "realliance-forge-backups"):
        assert buckets[name]["bucketType"] == "allPrivate"
        assert buckets[name]["defaultServerSideEncryption"]["value"]["mode"] == "SSE-B2"

def test_forge_backups_object_lock_30_days(buckets):
    lock = buckets["realliance-forge-backups"]["fileLockConfiguration"]["value"]
    assert lock["isFileLockEnabled"] is True
    assert lock["defaultRetention"]["mode"] == "governance"
    assert lock["defaultRetention"]["period"] == {"duration": 30, "unit": "days"}

def test_forge_backups_lifecycle(buckets):
    rules = {r["fileNamePrefix"]: (r["daysFromUploadingToHiding"], r["daysFromHidingToDeleting"])
             for r in buckets["realliance-forge-backups"]["lifecycleRules"]}
    assert rules == {"spilo/": (30, 1), "dump/": (30, 1)}

def test_forge_objects_hidden_versions_kept_30_days(buckets):
    rules = [(r["fileNamePrefix"], r["daysFromHidingToDeleting"]) for r in buckets["realliance-forge-objects"]["lifecycleRules"]]
    assert rules == [("", 30)]

def test_forge_keys_scoped(master):
    # key name -> (bucket, capabilities)
    expected = {
        "realliance-forge-objects":    ("realliance-forge-objects", {"listBuckets", "listFiles", "readFiles", "writeFiles"}),
        "realliance-forge-objects-ro": ("realliance-forge-objects", {"listBuckets", "listFiles", "readFiles"}),
        "realliance-forge-backup":     ("realliance-forge-backups", {"listBuckets", "listFiles", "readFiles", "writeFiles"}),
    }
    keys = {k["keyName"]: k for k in _api(master, "b2_list_keys", {"accountId": master["accountId"]}).json()["keys"]}
    ids = {b["bucketName"]: b["bucketId"] for b in _api(master, "b2_list_buckets", {"accountId": master["accountId"]}).json()["buckets"]}
    for name, (bucket, caps) in expected.items():
        assert keys[name]["bucketIds"] == [ids[bucket]] and set(keys[name]["capabilities"]) == caps

def test_forge_objects_key_hides_but_cannot_destroy(forge_objects_rclone):
    # upload a probe object, `rclone deletefile` it (S3 DeleteObject → hide marker) → succeeds;
    # then deleting a specific version (rclone backend versions / --s3-version-at) → refused (AccessDenied)

def test_forge_backup_key_cannot_delete(forge_backup_rclone):
    # upload dump/probe-<hex>, then delete by version → refused; mirrors test_backup_key_cannot_delete_dumps
```

- [ ] **Step 2: Run to verify they fail**

Run: `nix develop --command pytest tests/test_b2.py -k forge -v`
Expected: FAIL with `KeyError: 'realliance-forge-objects'`

- [ ] **Step 3: Implement `b2/forge.tf`** with the same resource shapes as `b2_bucket.backups` and `b2_application_key.backup` in `b2/main.tf`. Objects bucket: no Object Lock, SSE-B2, one lifecycle rule `file_name_prefix = ""`, `days_from_hiding_to_deleting = 30`. Backups bucket: Object Lock governance 30 days, SSE-B2, rules for `spilo/` and `dump/` (30 → hide, 1 → delete). Add the six outputs to `b2/outputs.tf`.

- [ ] **Step 4: Apply as an operator** (the `b2/` layer is operator-applied)

Run: `set -a; . ~/Repos/realliance-sso-secrets/b2/secrets.env; set +a; tofu -chdir=b2 apply`
Expected: `5 added, 0 changed, 0 destroyed` (2 buckets, 3 keys)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `nix develop --command pytest tests/test_b2.py -v`
Expected: PASS (all, including the pre-existing SSO bucket tests). If `test_forge_objects_key_hides_but_cannot_destroy` fails on the hide step, add `deleteFiles` to the objects key, change that test to assert the hide works, and ledger the ruling.

- [ ] **Step 6: Commit, PR, merge**

```bash
git switch -c forge-b2 && git add b2/forge.tf b2/outputs.tf tests/test_b2.py
git commit -S -m "b2: forge objects and backups buckets with scoped keys"
gh pr create --fill && gh pr merge --squash --delete-branch
```

---

### Task 2: Realm side — `forgejo` client, groups, test helpers (`realliance/sso` `realm/` + `tests/`)

**Files:**
- Modify: `~/Repos/realliance-sso/realm/apps.tf`, `realm/groups.tf`, `realm/outputs.tf`, `README.md` (the "Adding an app" example becomes "the Forgejo block is the live example")
- Modify: `~/Repos/realliance-sso/tests/conftest.py`, `tests/realm/test_mfa.py` (import `totp`)
- Create: `~/Repos/realliance-sso/tests/forge_helpers.py`, `tests/forge/__init__.py` (empty), `tests/forge/test_forge_client.py`
- Modify (private): `~/Repos/realliance-sso-people/people.yaml` (Chris gets `/apps/forgejo`, `/forgejo-admins`)

**Interfaces:**
- Consumes: `module "app"` (`realm/modules/app`), `keycloak_openid_client_scope.groups`, `keycloak_group.apps`.
- Produces: client `forgejo` (CONFIDENTIAL), groups `/apps/forgejo`, `/forgejo-admins`, `/realliance-maintainers`; output `app_client_secrets.forgejo`. Also every helper listed under "Shared test helpers" above.

- [ ] **Step 1: Write the failing tests** `tests/forge/test_forge_client.py`

```python
def test_forgejo_client_confidential_with_exact_redirect():
    c = client_by_id("forgejo")
    assert c["publicClient"] is False and c["standardFlowEnabled"] is True
    assert c["directAccessGrantsEnabled"] is False
    assert c["redirectUris"] == ["https://git.realliance.net/user/oauth2/realliance/callback"]

def test_forgejo_client_gets_groups_scope():
    assert "groups" in client_by_id("forgejo")["defaultClientScopes"]

def test_forge_groups_exist():
    names = {g["name"] for g in realm_get("/groups")}
    assert {"forgejo-admins", "realliance-maintainers"} <= names
    assert group_id("/apps/forgejo")

def test_forgejo_flow_denies_without_role():
    assert client_by_id("forgejo")["authenticationFlowBindingOverrides"]["browser"] == flow_id("forgejo-browser")
```

- [ ] **Step 2: Run to verify they fail**

Run: `nix develop --command pytest tests/forge/test_forge_client.py -v`
Expected: FAIL (no client `forgejo`)

- [ ] **Step 3: Implement.** `realm/apps.tf`: `module "forgejo"` exactly as in the README example (CONFIDENTIAL is the module default; `extra_default_scopes = [keycloak_openid_client_scope.groups.name]`). `realm/groups.tf`: a `keycloak_group` for each of `forgejo-admins` and `realliance-maintainers`, top level, each with a comment saying membership lives in sso-people. `realm/outputs.tf`: `forgejo = module.forgejo.client_secret`. Then add the shared helpers (moving `_totp` into conftest as `totp`).

- [ ] **Step 4: Merge, let the box apply, verify**

```bash
git switch -c forge-realm && git add -A realm tests README.md && git commit -S -m "realm: forgejo client, forgejo-admins and realliance-maintainers groups"
gh pr create --fill && gh pr merge --squash --delete-branch
```
Wait for the box's 5-minute apply (commit status, or `journalctl -u sso-config-apply` on the box). Then:
Run: `nix develop --command pytest tests/forge/test_forge_client.py tests/realm -v`
Expected: PASS (realm suite still green: `test_mfa` now imports `totp` from conftest)

- [ ] **Step 5: Give Chris access (sso-people PR).** Add `/apps/forgejo` and `/forgejo-admins` to `chris`'s groups in `people.yaml`. Commit signed, open a PR, merge.
Run: `nix develop --command pytest tests/people -v`
Expected: PASS

---

### Task 3: Plumbing — namespace, volumes, Postgres, secrets (`luma-homeops` `cluster/forgejo/`)

**Files:**
- Create in `cluster/forgejo/`: `kustomization.yaml`, `namespace.yaml`, `pvcs.yaml`, `psql.yaml`, `patroni-restapi.secret.yaml` + `patroni-restapi.yaml`, `logical-backup-s3.secret.yaml` + `logical-backup-s3.yaml`, `app-ini.secret.yaml` + `app-ini.yaml`, `oidc.secret.yaml` + `oidc.yaml`, `admin.secret.yaml` + `admin.yaml`
- Create: `cluster/applications/forgejo.yaml`; modify `cluster/applications/kustomization.yaml`
- Create (private): `~/Repos/realliance-sso-secrets/forge/secrets.env`
- Test: `~/Repos/realliance-sso/tests/forge/test_forge_backups.py::test_db_dump_in_realliance_bucket`

**Interfaces:**
- Consumes: Task 1 outputs `forge_backup_key*` and `forge_objects_key*`; Task 2 output `app_client_secrets.forgejo`.
- Produces: PVCs `forgejo-data` (5Gi, `rook-ceph-block-ssd`) and `forgejo-repos` (50Gi, `rook-ceph-block`); Postgres service `acid-forgejo`, DB `forgejo` owned by user `forgejo`, password Secret `forgejo.acid-forgejo.credentials.postgresql.acid.zalan.do` key `password`. Secrets `forgejo-app-ini` (keys `security`, `oauth2`, `server`, `storage`, `mailer`, each an ini fragment for that section), `forgejo-oidc` (keys `key`, `secret`), `forgejo-admin` (keys `username`, `password`), `logical-backup-s3`. File `forge/secrets.env` with keys `FORGEJO_SECRET_KEY`, `FORGEJO_INTERNAL_TOKEN`, `FORGEJO_JWT_SECRET`, `FORGEJO_LFS_JWT_SECRET`, `FORGEJO_ADMIN_PASSWORD`, `RESEND_FORGEJO_KEY` (and `FORGEJO_ADMIN_TOKEN` from Task 4).

- [ ] **Step 1: Write the failing test** `tests/forge/test_forge_backups.py`

```python
def test_db_dump_in_realliance_bucket():
    dumps = [f for f in b2_names("realliance-forge-backups", "spilo/acid-forgejo/") if f["name"].endswith(".sql.gz")]
    assert dumps, "no acid-forgejo logical backup in the Realliance bucket"
    newest = max(f["uploadTimestamp"] for f in dumps) / 1000
    assert time.time() - newest < 26 * 3600
```

- [ ] **Step 2: Run to verify it fails**

Run: `nix develop --command pytest tests/forge/test_forge_backups.py::test_db_dump_in_realliance_bucket -v`
Expected: FAIL, `no acid-forgejo logical backup in the Realliance bucket`

- [ ] **Step 3: Canonical secrets (`sso-secrets`).** Generate `FORGEJO_SECRET_KEY`, `FORGEJO_INTERNAL_TOKEN`, `FORGEJO_JWT_SECRET` and `FORGEJO_LFS_JWT_SECRET` with `forgejo generate secret <NAME>` (`nix run nixpkgs#forgejo-lts -- generate secret …`), and `FORGEJO_ADMIN_PASSWORD` with `openssl rand -base64 30`. Write each straight into `forge/secrets.env` without echoing it. Create `RESEND_FORGEJO_KEY` with the full-access key in `resend.env`: `POST https://api.resend.com/api-keys` with `{"name":"forgejo","permission":"sending_access","domain_id":<realliance.net id from GET /domains>}`, response `.token` piped into the file. Verify `git check-attr filter forge/secrets.env` → `git-crypt`; commit signed; push.

- [ ] **Step 4: Plumbing manifests.** Copy the shapes of `cluster/royaltracker/psql.yaml` (Patroni REST env, `failsafe_mode`, `synchronous_commit: "off"`, postgres-exporter sidecar if royaltracker has one) and `cluster/immich/kustomization.yaml` (the `patroni-restapi` `Prune=false` patch). `psql.yaml`: `acid-forgejo`, `databases: {forgejo: forgejo}`, `users: {forgejo: []}`, 2 instances, `version: '16'`, `volume: {size: 10Gi, storageClass: rook-ceph-block-ssd}`, requests `cpu: 50m, memory: 256Mi`, limit `memory: 512Mi`, `enableLogicalBackup: true`.
  - `logical-backup-s3.secret.yaml`: `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` = forge backup key, plus `LOGICAL_BACKUP_S3_BUCKET: realliance-forge-backups`, `LOGICAL_BACKUP_S3_ENDPOINT: https://s3.us-east-005.backblazeb2.com`, `LOGICAL_BACKUP_S3_REGION: us-east-005`. The operator (v1.11 `k8sres.go` ~L2264) appends the namespace secret's env after its own, and the last duplicate wins.
  - `app-ini.secret.yaml`: `security` = `INSTALL_LOCK=true` + `SECRET_KEY`/`INTERNAL_TOKEN`; `oauth2` = `JWT_SECRET`; `server` = `LFS_JWT_SECRET`; `storage` = `MINIO_ACCESS_KEY_ID`/`MINIO_SECRET_ACCESS_KEY` (forge objects key); `mailer` = `PASSWD` (Resend key).
  - `oidc.secret.yaml`: `key: forgejo`, `secret` from `tofu -chdir=realm output -json app_client_secrets`. `admin.secret.yaml`: `username: forgejo-admin`, `password`.
  - `cluster/applications/forgejo.yaml`: the shape of `cluster/applications/immich.yaml`, path `cluster/forgejo`, namespace `forgejo`.
  - Seal with `./sign.sh`.

- [ ] **Step 5: Render and commit**

Run: `scripts/check-kustomizations.sh && kubectl kustomize cluster/forgejo | grep -c '^kind:'`
Expected: exit 0; count = 9 (Namespace, 2 PersistentVolumeClaim, postgresql, 5 SealedSecret)
```bash
git add cluster/forgejo cluster/applications/forgejo.yaml cluster/applications/kustomization.yaml
git commit -m "forgejo: plumbing (namespace, ssd/hdd volumes, acid-forgejo, sealed secrets)"
git push   # Chris authorises pushes to main at execution start
```

- [ ] **Step 6: Verify the deploy** (Monitor, not a sleep loop)

Run: `kubectl -n forgejo get pvc,postgresql,pods` (until `acid-forgejo` is `Running`, 2/2 pods Ready, both PVCs `Bound`)
Run: `kubectl -n forgejo get cronjob logical-backup-acid-forgejo -o json | jq -r '.spec.jobTemplate.spec.template.spec.containers[0].env[] | select(.name=="LOGICAL_BACKUP_S3_BUCKET") | .value // .valueFrom.secretKeyRef.name'`
Expected: two lines, `galaxy-cluster-backups` then `logical-backup-s3`. The secret's entry comes last, so it wins.
Run: `kubectl -n forgejo create job --from=cronjob/logical-backup-acid-forgejo lb-manual-1`, then watch it to `Complete`.

- [ ] **Step 7: Run the test to verify it passes**

Run: `nix develop --command pytest tests/forge/test_forge_backups.py::test_db_dump_in_realliance_bucket -v`
Expected: PASS

---

### Task 4: Forgejo release — sign-in, config, LFS, mail (`luma-homeops`)

**Files:**
- Modify: `cluster/argo-cd/values.yaml` (`configs.repositories.forgejo-helm: {url: code.forgejo.org/forgejo-helm, type: helm, enableOCI: "true", name: forgejo-helm}`)
- Create: `cluster/applications/forgejo-release.yaml`; modify `cluster/applications/kustomization.yaml`
- Test: `~/Repos/realliance-sso/tests/forge/test_forge_signin.py`, `tests/forge/test_forge_storage.py`

**Interfaces:**
- Consumes: Task 3 PVCs, secrets and DB; Task 2 helpers and groups.
- Produces: Forgejo at `https://git.realliance.net`; auth source `realliance`; SSH Service `forgejo-ssh` of type LoadBalancer at `192.168.0.6:22`; `FORGEJO_ADMIN_TOKEN` (scopes `all`, user `forgejo-admin`) in `sso-secrets/forge/secrets.env`.

- [ ] **Step 1: Write the failing tests** `tests/forge/test_forge_signin.py`

```python
def test_member_signin_creates_account(forge_user):
    u = forge_user()
    forge_login(u)
    acct = forge_api("GET", f"/api/v1/users/{u.name}").json()
    assert acct["login"] == u.name           # USERNAME=nickname falls back to preferred_username
    assert acct["is_admin"] is False

def test_admin_group_makes_site_admin(forge_user):
    u = forge_user(groups=("/apps/forgejo", "/forgejo-admins"))
    forge_login(u)
    assert forge_api("GET", f"/api/v1/users/{u.name}").json()["is_admin"] is True  # admin view shows is_admin

def test_no_forgejo_access_refused_at_sso(forge_user):
    u = forge_user(groups=("/realliance",))
    _, r = forge_login_attempt(u)
    assert urlparse(r.url).hostname == "sso.realliance.net" and r.status_code in (401, 403)
    assert forge_api("GET", f"/api/v1/users/{u.name}").status_code == 404

def test_no_local_password_forms():
    for path in ("/user/login", "/user/sign_up"):
        assert 'name="password"' not in requests.get(FORGE + path, timeout=20).text

def test_user_list_hidden_from_anonymous():
    assert requests.get(f"{FORGE}/explore/users", timeout=20).status_code in (403, 404)

def test_member_cannot_create_org(forge_user):
    u = forge_user(); forge_login(u)
    r = forge_api("POST", "/api/v1/orgs", sudo=u.name, json={"username": f"t-{secrets.token_hex(3)}"})
    assert r.status_code == 403

def test_username_collision_refused(forge_user):
    org = f"test-{secrets.token_hex(4)}"
    assert forge_api("POST", "/api/v1/orgs", json={"username": org, "visibility": "private"}).status_code == 201
    try:
        u = forge_user(username=org)
        _, r = forge_login_attempt(u)
        assert urlparse(r.url).hostname == "git.realliance.net" and "/user/logout" not in r.text   # not signed in
        assert forge_api("GET", f"/api/v1/orgs/{org}").json()["username"] == org   # still the org, no user
    finally:
        forge_api("DELETE", f"/api/v1/orgs/{org}")
```

`tests/forge/test_forge_storage.py`:

```python
def test_lfs_object_lands_in_b2(forge_user, tmp_path):
    # user repo via Sudo POST /api/v1/user/repos; token via forge_token(session, "lfs", ["write:repository"]);
    # 120 MiB os.urandom file tracked by git-lfs, pushed over HTTPS (token via GIT_ASKPASS env, never argv);
    # assert b2_names("realliance-forge-objects", f"lfs/{oid[0:2]}/{oid[2:4]}/{oid[4:]}") has size == 120 MiB

def test_notification_mail_sent_via_resend(forge_user):
    # u = forge_user(email=f"delivered+{hex}@resend.dev"); forge_login(u); repo via Sudo;
    # admin opens an issue in it; poll GET https://api.resend.com/emails (full-access key) ≤120 s
    # assert an email to that address with from containing "git@realliance.net"
```

- [ ] **Step 2: Run to verify they fail**

Run: `nix develop --command pytest tests/forge/test_forge_signin.py tests/forge/test_forge_storage.py -v`
Expected: FAIL (`git.realliance.net` has no backend yet, and `FORGEJO_ADMIN_TOKEN` is not yet in `forge/secrets.env`)

- [ ] **Step 3: Implement `cluster/applications/forgejo-release.yaml`** in the shape of `immich-release.yaml`: `repoURL: code.forgejo.org/forgejo-helm`, `chart: forgejo`, `targetRevision: 17.3.2`, `releaseName: forgejo`, destination namespace `forgejo`. The `valuesObject` pins:
  - `strategy.type: Recreate`; `image.rootless: true`; resources requests `cpu: 100m, memory: 512Mi`, limit `memory: 1Gi`.
  - `persistence: {enabled: true, create: false, claimName: forgejo-data}`; `extraVolumes: [{name: repos, persistentVolumeClaim: {claimName: forgejo-repos}}]`; `extraContainerVolumeMounts: [{name: repos, mountPath: /repos}]`.
  - `ingress: {enabled: true, className: traefik, hosts: [{host: git.realliance.net, paths: [{path: /, pathType: Prefix}]}], tls: []}`.
  - `service.ssh: {type: LoadBalancer, port: 22, annotations: {metallb.universe.tf/loadBalancerIPs: 192.168.0.6}}`.
  - `gitea.admin: {existingSecret: forgejo-admin, email: forgejo-admin@realliance.net, passwordMode: keepUpdated}`.
  - `gitea.oauth: [{name: realliance, provider: openidConnect, existingSecret: forgejo-oidc, autoDiscoverUrl: <discovery URL>, groupClaimName: groups, adminGroup: forgejo-admins, groupTeamMap: '{"realliance":{"realliance":["members"]},"realliance-maintainers":{"realliance":["maintainers"]}}', groupTeamMapRemoval: true, skip-local-2fa: true}]`. Flag keys must be spelled as the chart's `gitea.oauth_settings` flag list has them (`groupTeamMapRemoval`, `skip-local-2fa`); any other boolean key renders as `--key "true"` and breaks the init script.
  - `gitea.additionalConfigSources: [{secret: {secretName: forgejo-app-ini}}]`.
  - `gitea.additionalConfigFromEnvs: [{name: FORGEJO__DATABASE__PASSWD, valueFrom: {secretKeyRef: {name: forgejo.acid-forgejo.credentials.postgresql.acid.zalan.do, key: password}}}]`.
  - `gitea.config`: `APP_NAME: Realliance Forge`; `server: {ROOT_URL: https://git.realliance.net/, DOMAIN: git.realliance.net, SSH_DOMAIN: git.realliance.net, SSH_PORT: 22, SSH_LISTEN_PORT: 2222, LFS_START_SERVER: true}` (ROOT_URL must be explicit: the chart infers `http` when `ingress.tls` is empty); `database: {DB_TYPE: postgres, HOST: acid-forgejo:5432, NAME: forgejo, USER: forgejo, SSL_MODE: require}`; `repository: {ROOT: /repos, DEFAULT_PRIVATE: private}`; `service` with the spec's three keys plus `ENABLE_BASIC_AUTHENTICATION: false`, `ENABLE_NOTIFY_MAIL: true`, `DEFAULT_KEEP_EMAIL_PRIVATE: true`, `DEFAULT_ALLOW_CREATE_ORGANIZATION: false`; `service.explore: {DISABLE_USERS_PAGE: true}`; `admin: {DISABLE_REGULAR_ORG_CREATION: true}`; `oauth2_client: {ENABLE_AUTO_REGISTRATION: true, ACCOUNT_LINKING: disabled, USERNAME: nickname, REGISTER_EMAIL_CONFIRM: false}`; `session: {PROVIDER: db, SESSION_LIFE_TIME: 604800, COOKIE_SECURE: true}`; `queue: {TYPE: level}`; `cache: {ADAPTER: twoqueue}`; `storage: {STORAGE_TYPE: minio, MINIO_ENDPOINT: s3.us-east-005.backblazeb2.com, MINIO_LOCATION: us-east-005, MINIO_BUCKET: realliance-forge-objects, MINIO_USE_SSL: true}`; `mailer: {ENABLED: true, PROTOCOL: smtps, SMTP_ADDR: smtp.resend.com, SMTP_PORT: 465, USER: resend, FROM: "Realliance Forge <git@realliance.net>"}`; `cron.git_gc_repos: {ENABLED: false}`.
  - A comment block at the top states the storage tiers, and that the team map may stay here only while every mapped org is public (runbook gate, Task 8).

- [ ] **Step 4: Check memory headroom, then push.** Run `kubectl describe nodes | grep -A6 'Allocated resources'`. If any node's memory requests exceed 90 %, run the `docs/node-rebuild.md` §D step 5 rebalance first and re-check. Then render the chart locally (`helm template forgejo oci://code.forgejo.org/forgejo-helm/forgejo --version 17.3.2 -f <values extracted with yq>`) and confirm it has no errors and contains `--group-team-map-removal`.
```bash
git add cluster/argo-cd/values.yaml cluster/applications/forgejo-release.yaml cluster/applications/kustomization.yaml
git commit -m "forgejo: Helm release (Forgejo 15 LTS) behind Realliance SSO"
git push
```

- [ ] **Step 5: Verify the rollout** (Monitor). `kubectl -n forgejo rollout status deploy/forgejo` reaches success. Init logs show `Installing it now...` for oauth `realliance`. `curl -sI https://git.realliance.net/api/healthz` returns `200`. `kubectl -n forgejo get svc forgejo-ssh` shows EXTERNAL-IP `192.168.0.6`.

- [ ] **Step 6: Admin token** (break-glass path doubles as its test): `kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin user generate-access-token --username forgejo-admin --token-name acceptance-and-inventory --scopes all --raw`, piped straight into `FORGEJO_ADMIN_TOKEN=` in `sso-secrets/forge/secrets.env` without being echoed. Commit signed and push sso-secrets.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `nix develop --command pytest tests/forge/test_forge_signin.py tests/forge/test_forge_storage.py -v`
Expected: PASS. If `test_member_signin_creates_account` shows `login` ≠ the Keycloak username, add a Keycloak `nickname` mapper (user property `username`) to the `forgejo` client in `realm/apps.tf` rather than changing `USERNAME`, and ledger the ruling.

- [ ] **Step 8: Chris signs in** at `https://git.realliance.net` (Success 1). Confirm `is_admin` with `forge_api("GET", "/api/v1/users/chris")`.

---

### Task 5: Git over SSH from WAN and LAN (`luma-homeops` `router/ansible/`)

**Files:**
- Modify: `router/ansible/group_vars/vyos_routers.yml` (`nat.destination_rules`)
- Modify: `router/ansible/playbooks/firewall.yml` (OUTSIDE-IN chain)
- Test: `~/Repos/realliance-sso/tests/forge/test_forge_ssh.py`

**Interfaces:**
- Consumes: SSH LoadBalancer `192.168.0.6:22` (Task 4).
- Produces: WAN TCP 22 → `192.168.0.6:22`; LAN TCP 22 to `152.44.247.88` → `192.168.0.6:22`. Router SSH unchanged on LAN (`192.168.0.1`) and tailnet (`100.96.171.43`).

- [ ] **Step 1: Write the failing tests**

```python
@pytest.fixture(scope="module")
def forgejo_key():
    return ssh_keyscan(FORGE_LB)            # lan_only

@lan_only
def test_wan_ssh_reaches_forgejo(forgejo_key):
    assert ssh_keyscan("git.realliance.net", via=VPS) == forgejo_key

@lan_only
def test_lan_ssh_to_wan_ip_reaches_forgejo(forgejo_key):
    assert ssh_keyscan(WAN_IP) == forgejo_key

@lan_only
def test_router_ssh_on_lan_and_tailnet_is_vyos(forgejo_key):
    for host in ("192.168.0.1", "100.96.171.43"):
        k = ssh_keyscan(host)
        assert k and k != forgejo_key

@lan_only
def test_github_ssh_unaffected(forgejo_key):
    assert ssh_keyscan("github.com") not in ("", forgejo_key)

def test_clone_over_ssh_from_internet(forge_user):
    # u = forge_user(); forge_login(u); repo with one commit via Sudo API (auto_init);
    # keypair generated ON the VPS in a mktemp dir; its .pub registered via POST /api/v1/admin/users/{u}/keys;
    # VPS: GIT_SSH_COMMAND="ssh -i <tmp>/k -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=<tmp>/kh"
    #      git ls-remote git@git.realliance.net:<u>/<repo>.git → contains refs/heads/main; finally rm -rf <tmp>
```

- [ ] **Step 2: Run to verify they fail**

Run: `nix develop --command pytest tests/forge/test_forge_ssh.py -v`
Expected: the WAN, LAN-reflection and internet-clone tests FAIL (VyOS host key, or rate-limited drop); the router and github tests PASS

- [ ] **Step 3: Implement.** In `nat.destination_rules` add rule **400** (`description: 'SSH Port Forward (Forgejo)'`, `port: 22`, `protocol: tcp`, `inbound_interface: eth0`, `translation_address: 192.168.0.6`, `translation_port: 22`) and rule **410** (`description: 'NAT Reflection: SSH (Forgejo)'`, `destination_address: 152.44.247.88`, `port: 22`, `protocol: tcp`, `inbound_interface: eth2`, `translation_address: 192.168.0.6`, `translation_port: 22`), with a comment saying why router sshd keeps listening everywhere (tailnet). In `firewall.yml` add OUTSIDE-IN rule **30**: `action accept`, `destination address 192.168.0.6`, `destination port 22`, `inbound-interface group WAN`, `protocol tcp`. Source rule 110 already masquerades the reflection.

- [ ] **Step 4: Read live state, then ask Chris** (S1, S4/S8: firewall + NAT). No `--check` run: the parachute tasks are tagged `always` and must not be exercised half-way.

Run: `ssh chris@192.168.0.1 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show configuration commands' | grep -E 'nat destination rule|OUTSIDE-IN rule'`
Expected: rules 200/210/300/310 and OUTSIDE-IN 10/20 only, with nothing already at 400/410/30. Show Chris that output plus `git diff router/ansible` and get his explicit OK through AskUserQuestion before the run.

- [ ] **Step 5: Apply inside the parachute**

Run: `ansible-playbook site.yml --tags nat,firewall` (the commit-confirm arm/verify tasks are `always`)
Expected: verify passes, timer cancelled, config saved

- [ ] **Step 6: Run the tests to verify they pass**

Run: `nix develop --command pytest tests/forge/test_forge_ssh.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add router/ansible/group_vars/vyos_routers.yml router/ansible/playbooks/firewall.yml
git commit -m "router: WAN + reflected SSH 22 to Forgejo at .6"
git push
```

---

### Task 6: Repo backups — nightly `forgejo dump` to B2 (`luma-homeops`)

**Files:**
- Create: `cluster/forgejo/backup-cronjob.yaml`, `cluster/forgejo/backup-rbac.yaml`, `cluster/forgejo/backup-b2.secret.yaml` + `backup-b2.yaml`; modify `cluster/forgejo/kustomization.yaml`
- Test: `~/Repos/realliance-sso/tests/forge/test_forge_backups.py`

**Interfaces:**
- Consumes: forge backup key (Task 1); Deployment `forgejo`, container `forgejo` (Task 4).
- Produces: CronJob `forgejo-dump` (01:30, after the 00:30 logical backup) writing `realliance-forge-backups/dump/forgejo-<UTC %Y%m%dT%H%M%SZ>.tar.gz`.

- [ ] **Step 1: Write the failing tests**

```python
def test_repo_dump_fresh():
    dumps = b2_names("realliance-forge-backups", "dump/")
    assert dumps and time.time() - max(f["uploadTimestamp"] for f in dumps) / 1000 < 26 * 3600

def test_repo_dump_contents(tmp_path):
    # download the newest dump with the master key (rclone, env-only creds) into tmp_path; tarfile names only:
    names = ...
    assert any(n.startswith("repos/") for n in names)
    assert any(n.endswith("-db.sql") for n in names) and any(n.endswith("app.ini") for n in names)
    assert not any(n.startswith(("data/lfs/", "data/attachments/", "data/packages/")) for n in names)
```

- [ ] **Step 2: Run to verify they fail**

Run: `nix develop --command pytest tests/forge/test_forge_backups.py -v`
Expected: `test_repo_dump_*` FAIL (no `dump/` objects); `test_db_dump_in_realliance_bucket` PASS

- [ ] **Step 3: Implement.** RBAC: ServiceAccount `forgejo-dump`, and a Role with `get,list` on `pods` and `create` on `pods/exec` in `forgejo`, plus a RoleBinding. CronJob in the shape of `cluster/immich/backup-cronjob.yaml` (Forbid, history 3, non-root, drop ALL):
  - initContainer from `ghcr.io/rclone/rclone:1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5` copies `/usr/local/bin/rclone` into an emptyDir `/tools`.
  - Main container `alpine/k8s:<1.33.x, ≥2 weeks old>@sha256:<digest>` runs `set -euo pipefail; kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo dump --type tar.gz --file - --database postgres --skip-lfs-data --skip-attachment-data --skip-package-data --skip-log | /tools/rclone rcat b2:realliance-forge-backups/dump/forgejo-$(date -u +%Y%m%dT%H%M%SZ).tar.gz`.
  - Remote `b2` comes from `RCLONE_CONFIG_B2_*` env in Secret `forgejo-backup-b2` (type s3, provider Other, endpoint and region `us-east-005`, `no_check_bucket=true`, forge backup key).
  - A size gate after upload (`rclone size` of the new object ≥ 1 KiB, else exit 2) so an empty stream fails the Job.

- [ ] **Step 4: Render, commit, push, run once**

Run: `scripts/check-kustomizations.sh` → exit 0
```bash
git add cluster/forgejo && git commit -m "forgejo: nightly forgejo dump to the Realliance backups bucket" && git push
```
After Argo syncs: `kubectl -n forgejo create job --from=cronjob/forgejo-dump dump-manual-1`, watch it to `Complete` (Monitor) and read its log tail.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `nix develop --command pytest tests/forge/test_forge_backups.py tests/test_b2.py -k forge -v`
Expected: PASS

---

### Task 7: Bootstrap the `realliance` org and the `realliance/forge` inventory (2b)

**Files:**
- Create (new private repo `git.realliance.net/realliance/forge`, cloned at `~/Repos/realliance-forge`): `README.md`, `providers.tf`, `backend.tf`, `orgs.tf`, `teams.tf`, `repos.tf`, `.gitignore`
- Modify (private): `~/Repos/realliance-sso-secrets/forge/secrets.env` (`GITHUB_MIRROR_TOKEN`)
- Test: `~/Repos/realliance-sso/tests/forge/test_forge_inventory.py`

**Interfaces:**
- Consumes: `FORGEJO_ADMIN_TOKEN`; tfstate key in `sso-secrets/b2/secrets.env` (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`); team names from Task 4's map.
- Produces: org `realliance` (public); teams `members` (permission `write`, `can_create_org_repo = true`, `includes_all_repositories = true`) and `maintainers` (permission `admin`, every unit `admin`, `includes_all_repositories = true`); Owners membership `local.owners = ["chris"]`; repo `realliance/forge` (private, `prevent_destroy`) with branch protection on `main`.

- [ ] **Step 1: Write the failing tests**

```python
FORGE_REPO = Path.home() / "Repos/realliance-forge"

def test_inventory_has_no_drift():
    r = subprocess.run(["bash", "-c", "set -a; . ~/Repos/realliance-sso-secrets/forge/secrets.env; "
                        ". ~/Repos/realliance-sso-secrets/b2/secrets.env; set +a; "
                        "tofu plan -detailed-exitcode -input=false -no-color >/dev/null"], cwd=FORGE_REPO)
    assert r.returncode == 0

def test_realliance_org_public_to_anonymous():
    assert requests.get(f"{FORGE}/api/v1/orgs/realliance", timeout=20).json()["visibility"] == "public"

def test_teams_shape():
    teams = {t["name"]: t for t in forge_api("GET", "/api/v1/orgs/realliance/teams").json()}
    assert teams["members"]["permission"] == "write" and teams["members"]["can_create_org_repo"] is True
    assert teams["maintainers"]["permission"] == "admin"
    owners = forge_api("GET", f"/api/v1/teams/{teams['Owners']['id']}/members").json()
    assert "chris" in {m["login"] for m in owners}

def test_forge_repo_private_and_protected():
    assert forge_api("GET", "/api/v1/repos/realliance/forge").json()["private"] is True
    bp = forge_api("GET", "/api/v1/repos/realliance/forge/branch_protections/main").json()
    assert bp["enable_push"] is False and bp["require_signed_commits"] is True
    assert bp["required_approvals"] == 0          # 1 once Connor is active, like realliance/sso

def test_member_creates_repo_cannot_delete(forge_user):          # Success 6
    u = forge_user(groups=("/apps/forgejo", "/realliance")); forge_login(u)
    name = f"t-{secrets.token_hex(3)}"
    assert forge_api("POST", "/api/v1/orgs/realliance/repos", sudo=u.name, json={"name": name}).status_code == 201
    try:
        assert forge_api("DELETE", f"/api/v1/repos/realliance/{name}", sudo=u.name).status_code in (403, 404)
        assert forge_api("GET", f"/api/v1/repos/realliance/{name}").status_code == 200
    finally:
        forge_api("DELETE", f"/api/v1/repos/realliance/{name}")

def test_member_cannot_transfer_org_repo(forge_user):           # Review Focus 5
    # as above, then POST /repos/realliance/{name}/transfer {"new_owner": u.name} with sudo → 403/404; repo still in realliance

def test_private_org_invisible(forge_user):                     # Success 5
    # admin creates private org test-<hex>; anonymous GET /api/v1/orgs/<org> → 404; name absent from
    # requests.get(f"{FORGE}/explore/organizations").text; a logged-in non-member (sudo) GET → 404; finally delete

def test_public_repo_visible_to_anonymous():
    # admin creates public repo realliance/t-<hex>; anonymous GET /api/v1/repos/realliance/t-<hex> → 200; finally delete
```

- [ ] **Step 2: Run to verify they fail**

Run: `nix develop --command pytest tests/forge/test_forge_inventory.py -v`
Expected: FAIL (no org `realliance`, no `~/Repos/realliance-forge`)

- [ ] **Step 3: Bootstrap by hand (spec).** Using the admin token: `POST /api/v1/orgs` (`realliance`, public, full name "Realliance"), then `POST /api/v1/orgs/realliance/repos` (`forge`, private). Chris adds his GPG and SSH keys to his Forgejo account (needed for signed-commit protection). Clone to `~/Repos/realliance-forge`.

- [ ] **Step 4: Implement the inventory.**
  - `providers.tf`: `svalabs/forgejo` `= 1.7.0`, host `https://git.realliance.net`, token from env `FORGEJO_API_TOKEN` (set from `FORGEJO_ADMIN_TOKEN`).
  - `backend.tf`: s3 backend shaped like `realliance-sso/realm/backend.tf`, key `forge/terraform.tfstate`, `use_lockfile = false`.
  - `orgs.tf`: `import` block + `forgejo_organization.realliance` (`visibility = "public"`, `lifecycle { prevent_destroy = true }`).
  - `teams.tf`: `members` and `maintainers` as above; the Owners team imported (`import { to = forgejo_team.owners, id = "realliance/Owners" }`, permission `owner`, every unit `admin`) and a `forgejo_team_member` on it for each of `local.owners`; a comment that members/maintainers membership comes from SSO at sign-in, so the inventory never declares it.
  - `repos.tf`: imported `forgejo_repository.forge` (`private = true`, `prevent_destroy`) and `forgejo_branch_protection` on `main`.
  - `README.md`: what is owned here, how to apply (`set -a; . sso-secrets/forge/secrets.env; . sso-secrets/b2/secrets.env`), "no deletes: archive", and the hidden-org rule: the first hidden org needs the team map moved out of public config first (link to `docs/forgejo.md` in luma-homeops).
  - Then `tofu init && tofu apply`.

- [ ] **Step 5: GitHub private mirror.** Chris creates `github.com/realliance/forge` (private) and a fine-grained PAT (contents read/write on that repo only); it goes into `sso-secrets/forge/secrets.env` as `GITHUB_MIRROR_TOKEN` without being echoed. Add a Forgejo push mirror on `realliance/forge` via `POST /api/v1/repos/realliance/forge/push_mirrors` (`sync_on_commit: true`, `interval: 8h0m0s`), with the token in the JSON body only.

- [ ] **Step 6: Commit and push the inventory** (signed). The first push lands before branch protection, by design; later changes go by PR.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `nix develop --command pytest tests/forge/test_forge_inventory.py -v`
Expected: PASS; `gh api repos/realliance/forge/commits/main --jq .sha` equals the Forgejo `main` sha

---

### Task 8: Team sync, offboarding, runbook (`realliance/sso` tests + `luma-homeops` docs)

**Files:**
- Create: `~/Repos/realliance-sso/tests/forge/test_forge_access.py`
- Create: `luma-homeops/docs/forgejo.md` (runbook); modify `CLAUDE.md` (Topology rows: Forge and `.6` = Forgejo SSH)

**Interfaces:**
- Consumes: helpers (Task 2), teams (Task 7), auth source config (Task 4).

- [ ] **Step 1: Write the tests**

```python
def _team_members(team):
    tid = next(t["id"] for t in forge_api("GET", "/api/v1/orgs/realliance/teams").json() if t["name"] == team)
    return {m["login"] for m in forge_api("GET", f"/api/v1/teams/{tid}/members").json()}

def test_team_follows_groups_at_signin(forge_user):              # Success 4
    u = forge_user(groups=("/apps/forgejo", "/realliance")); s = forge_login(u)
    assert u.name in _team_members("members")
    set_groups(u, ["/apps/forgejo", "/realliance-maintainers"])  # Keycloak admin API; helper in forge_helpers
    forge_relogin(s, u)
    assert u.name in _team_members("maintainers") and u.name not in _team_members("members")

def test_access_without_community_group_gets_no_team(forge_user):  # Review Focus 3
    u = forge_user(); forge_login(u)
    assert all(u.name not in _team_members(t) for t in ("members", "maintainers", "Owners"))
    assert forge_api("GET", f"/api/v1/users/{u.name}").json()["is_admin"] is False

def test_admin_status_dropped_at_next_signin(forge_user):
    u = forge_user(groups=("/apps/forgejo", "/forgejo-admins")); s = forge_login(u)
    set_groups(u, ["/apps/forgejo"]); forge_relogin(s, u)
    assert forge_api("GET", f"/api/v1/users/{u.name}").json()["is_admin"] is False

def test_owners_never_in_team_map():
    values = yaml.safe_load((LUMA / "cluster/applications/forgejo-release.yaml").read_text())
    oauth = values["spec"]["source"]["helm"]["valuesObject"]["gitea"]["oauth"][0]
    teams = {t.lower() for orgs in json.loads(oauth["groupTeamMap"]).values() for ts in orgs.values() for t in ts}
    assert "owners" not in teams

def test_email_change_keeps_account(forge_user):                 # Review Focus 2
    u = forge_user(); s = forge_login(u); uid = forge_api("GET", f"/api/v1/users/{u.name}").json()["id"]
    set_email(u, f"{u.name}-new@example.invalid"); forge_relogin(s, u)
    assert forge_api("GET", f"/api/v1/users/{u.name}").json()["id"] == uid
    assert forge_api("GET", "/api/v1/admin/users", params={"login_name": u.name}).json().__len__() == 1

def test_offboarding_blocks_web_token_and_ssh_and_is_reversible(forge_user, tmp_path):   # Success 3
    # u signs in; token = forge_token(...); ssh key (local tmp) registered via admin API; user repo via Sudo.
    # Step 1 of runbook: disable in Keycloak (what sso-people active:false does).
    # Step 2: PATCH /api/v1/admin/users/{u} {"prohibit_login": true, "login_name": u.name, "source_id": <realliance id>}
    # assert: session GET /user/settings → not 200-signed-in; token GET /api/v1/user → 401/403;
    #         `git ls-remote` over ssh (FORGE_LB on LAN, else WAN) → nonzero exit.
    # Reverse both; assert all three work again (a fresh forge_login for the web).
```

- [ ] **Step 2: Run them**

Run: `nix develop --command pytest tests/forge/test_forge_access.py -v`
Expected: PASS. The behaviour is already configured (Tasks 4 and 7); these tests pin it. A failure here is a finding: debug it, don't edit the test to match.

- [ ] **Step 3: Write `docs/forgejo.md`**, a runbook in the shape of `docs/immich.md`. Sections:
  - architecture and storage tiers;
  - granting access (sso-people groups);
  - **offboarding, two steps** (sso-people `active: false` or removal from `/apps/forgejo`, **and** Forgejo "Prohibit login" in Site Administration → User Accounts or the API call above), with the known gap stated;
  - team changes apply at the person's next sign-in;
  - **Gate before the first hidden org:** move `groupTeamMap` out of public values (SealedSecret + PostSync Job, or forge-sync) first;
  - break-glass (`kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin user …`; password in `sso-secrets/forge/secrets.env`);
  - backups and restore (link to Task 9's drill);
  - the WAN-IP-in-reflection-rules limitation;
  - running the acceptance suite.
  Add the CLAUDE.md Topology row and update the MetalLB `.6` note.

- [ ] **Step 4: Commit**

```bash
cd ~/Repos/realliance-sso && git switch -c forge-access-tests && git add tests/forge tests/forge_helpers.py && git commit -S -m "tests: forge team sync and offboarding" && gh pr create --fill && gh pr merge --squash --delete-branch
cd ~/Repos/luma-homeops && git add docs/forgejo.md CLAUDE.md && git commit -m "docs: Forgejo runbook (offboarding, hidden-org gate, break-glass)" && git push
```

---

### Task 9: Restore drill (Success 9)

**Files:**
- Create: `luma-homeops/docs/forgejo/restore-drill.sh`, `docs/forgejo/drill-values.yaml`, `docs/forgejo/drill-psql.yaml`
- Modify: `docs/forgejo.md` (drill result + date), `docs/backups.md` (forge row in the tier table)

**Interfaces:**
- Consumes: newest `dump/` object (Task 6); objects RO key (Task 1); chart 17.3.2.
- Produces: namespace `forgejo-drill` (not Argo-managed) serving the restored instance on a port-forward.

- [ ] **Step 1: Write the drill script** `restore-drill.sh <dump-key>`, in this order:
  1. Create ns `forgejo-drill`, apply `drill-psql.yaml` (`acid-forgejo-drill`, 1 instance, 5Gi ssd, no logical backup, no Patroni REST env), and Secrets created from env (RO objects key, admin password).
  2. A Job downloads the dump with rclone into the drill PVCs (`forgejo-data`/`forgejo-repos` equivalents) and unpacks `repos/` → `/repos` and `data/` + `app.ini` → `/data/gitea/` (dump layout).
  3. Load `forgejo-db.sql` into `acid-forgejo-drill` with `psql`.
  4. `helm template` the chart with `drill-values.yaml` (same values minus ingress, LB, oauth and mailer; DB host `acid-forgejo-drill`; RO objects key; `ROOT_URL: http://localhost:3000/`) and `kubectl apply` it.
  5. Wait for Ready.

- [ ] **Step 2: Run the drill and verify**

Run: `docs/forgejo/restore-drill.sh dump/<newest>` then `kubectl -n forgejo-drill port-forward deploy/forgejo 3000` (background)
Expected:
  - `git ls-remote http://localhost:3000/realliance/forge.git` (admin token over HTTP) prints the same `main` sha as production;
  - `git lfs fetch` of the Task 4 test-repo object (keep one LFS fixture repo `chris/lfs-fixture` for this) completes;
  - `ssh-keyscan -p 2222` on the drill pod (via port-forward) equals the production host key.
  Record the date and timings in `docs/forgejo.md`.

- [ ] **Step 3: Tear down with confirmation.** Ask Chris (S4: namespace and PVC deletion), then `kubectl delete ns forgejo-drill` and release the Retain PVs.

- [ ] **Step 4: Commit**

```bash
git add docs/forgejo docs/forgejo.md docs/backups.md && git commit -m "forgejo: restore drill script and first drill result" && git push
```

- [ ] **Step 5: Update memory** `project_realliance_forge.md` (2a/2b live, URLs, ledger path, open items: forge-sync, the hidden-org gate, Connor's onboarding, phase 2c) and its MEMORY.md line.
