# Realliance SSO Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up Keycloak at `https://sso.realliance.net` on the CrownCloud VPS — passkey-first, invite-only, optional linked logins, delegated group/app admins, a `groups` claim for Forgejo — fully rebuildable from `realliance/sso` + `realliance/sso-secrets` + a B2 dump by either operator.

**Architecture:** Two new repos. Public `realliance/sso` holds `host/` (Ansible, applied by `ansible-pull` on the box with commit-signature verification), `realm/` (OpenTofu `keycloak/keycloak` provider, applied by GitHub Actions through an approval-gated `production` environment), `dns/` (OpenTofu, the `sso` record only) and `tests/` (pytest: testinfra for the host, `requests` against Keycloak's admin REST/OIDC endpoints for the realm). Private `realliance/sso-secrets` (git-crypt, Chris + Connor GPG keys) holds every secret value and break-glass credential. The box runs Keycloak 26.8.1 as a systemd service behind Caddy, with PostgreSQL 16 local and nightly `pg_dump` → B2.

**Tech Stack:** AlmaLinux 9.8 (OpenVZ 7 container, cgroup v1, 3 GB), Keycloak 26.8.1, OpenJDK 21, PostgreSQL 16 (AppStream module), Caddy (COPR `@caddy/caddy`), Ansible (collections `community.general`, `ansible.posix`, `community.postgresql`), OpenTofu ≥ 1.10 with `keycloak/keycloak` 5.10.0 and `cloudflare/cloudflare` 5.x, rclone, git-crypt, GitHub Actions, pytest + pytest-testinfra + requests, Resend (SMTP), B2 (S3 API).

**Spec:** `docs/superpowers/specs/2026-10-31-realliance-sso-design.md` (luma-homeops, commit fc6a6de4). Executors read both.

## Global Constraints

- Hostname `sso.realliance.net`; issuer `https://sso.realliance.net/realms/realliance`; one realm `realliance`; `master` holds only break-glass admins and automation clients, never apps.
- Keycloak **26.8.x** (pin 26.8.1), plain systemd service from the official archive, pinned version + checksum, no containers. Java 21 + PostgreSQL 16 from AppStream.
- Memory budget in 3 GB: Keycloak heap `-Xms256m -Xmx896m`; PostgreSQL ≈ 256–384 MB; nothing else heavy.
- OS is AlmaLinux 9 (systemd 252 — never anything needing systemd ≥ 256).
- SSH: key-only, both operators' ed25519 keys; password auth off; firewall open on 22, 80, 443 only.
- `realliance/sso` is **public** and lives on GitHub permanently. **No secrets in it, not even ciphertext.** Realm secrets = GitHub Actions environment secrets; host secrets = root-only `/etc/realliance-sso/secrets.yml` on the box; master copies of everything in private git-crypt `realliance/sso-secrets`.
- Hidden projects never touch `realliance/sso`: not their groups, not their apps' clients, not delegation over them.
- Workflows use `pull_request`, never `pull_request_target`.
- `ansible-pull` applies only commits whose signature verifies against the operators' keys or GitHub's web-flow key.
- OpenTofu state on B2 via the S3 backend with `use_lockfile = true`; never the homelab `azurerm` backend. Nothing depends on Azure.
- `dns/` manages **only** the `sso` A record. Other `realliance.net` records (Resend's) go in `luma-homeops/cloudflare/dns/` via the `cloudflare-dns` skill.
- Backups: nightly `pg_dump` to B2 with a key lacking `deleteFiles`; 30 dumps kept by bucket lifecycle.
- Default deny per client; `groups` claim only on clients that request the `groups` scope; group admins get `view` + `manage-membership` only, never `manage-members`; app owners get `view` + `map-roles` on their client plus `manage-membership` on its access group.
- Linked login (Google/GitHub/Microsoft/Discord) is a full sign-in for apps; the admin console additionally requires passkey or OTP; first broker login never creates an account and never auto-links by email.
- Operators: **Chris and Connor.** Commits to both repos are GPG-signed.
- Go-live gate: ≥ 99.5 % uptime over 14 days, no outage > 1 h, no OOM kills.

## Review Focus

1. **Admin power through a password-only grant.** Someone holding only a member's password (or a linked Google session) uses `admin-cli` direct grant to get an admin-API token and skips the passkey/OTP step-up. Expected: refused. → Task 10 test `test_admin_cli_password_grant_refused`.
2. **A GitHub-UI merge that the box silently refuses.** A PR squash-merged in the web UI is signed by GitHub's web-flow key; if that key is missing from the box's keyring, `ansible-pull` fails every 15 min and the host drifts unnoticed. Expected: web-flow-signed commits apply; a stale last-successful pull fails loudly in `gate-check`. → Task 7 test `test_pull_accepts_web_flow_signed`; Task 14 test `test_gate_check_reports_pull_age`.
3. **Backups that stop without anyone noticing** (expired key, `no_check_bucket` missing, disk full). Expected: `gate-check` fails when the newest B2 dump is older than 26 h. → Task 8 test `test_backup_newest_object_fresh` + Task 14 `gate-check`.
4. **A console-made hidden-project group colliding with OpenTofu.** An operator creates a hidden group in the console; the next `tofu apply` must neither delete it nor show drift. Expected: `tofu plan -detailed-exitcode` exits 0 with the group present. → Task 11 test `test_console_group_untouched_by_tofu`.
5. **A delegate learning hidden-project names through a user's group list.** Expected: a delegated admin viewing a user who is in a hidden group does not see that group. → Task 12 test `test_delegate_cannot_see_hidden_group_via_user`.

Also covered in owning tasks: journald capped (the old box had a 4.1 GB journal) — Task 1 `test_journald_capped`; Keycloak rebuild after a build-option change — Task 4 `test_build_options_persisted`.

## Prerequisites (inputs gathered as the owning task reaches them)

| Input | From | First needed |
|-------|------|--------------|
| Chris's and Connor's ed25519 SSH public keys | Chris, Connor | Task 1 (Connor's may be added later by re-running `base`) |
| Chris's and Connor's GPG public keys; Connor's GitHub username | Chris, Connor | Task 2 |
| B2 buckets + keys (commands in Task 8 / Task 9) | Chris (master key) | Tasks 8, 9 |
| Cloudflare API token, Zone → DNS → Edit on `realliance.net` only | Chris | Task 5 |
| Resend account + `realliance.net` domain + sending-only API key | Chris | Task 9 |
| OAuth apps: Google, GitHub (under the `realliance` org), Microsoft (Entra, personal accounts), Discord | Chris / Connor | Task 10 |

Every secret value lands first in `~/Repos/realliance-sso-secrets` (unlocked), then wherever it is used.

## File Structure

`~/Repos/realliance-sso` → `github.com/realliance/sso` (public)

```
README.md                    tools, layout, link to the spec, runbook pointer
flake.nix, flake.lock        devShell: opentofu ansible ansible-lint git-crypt gnupg jq rclone gh
                             python3 with pytest pytest-testinfra requests
keys/                        chris.asc, connor.asc, github-web-flow.asc (public keys trusted by appliers)
host/
  ansible.cfg
  inventory.yml              group sso: host sso, ansible_host 23.163.0.48, user root (push mode)
  inventory-pull.yml         group sso: localhost, ansible_connection local (pull mode)
  requirements.yml           pinned collections
  site.yml                   roles in order: secrets base postgresql keycloak caddy master_realm pull backup
  group_vars/sso.yml         every non-secret value below (versions, sizes, schedules)
  roles/<role>/{tasks,handlers,templates,files,defaults}/
scripts/gate-check.sh        go-live/health gate (Task 14)
realm/
  backend.tf providers.tf variables.tf outputs.tf
  realm.tf                   realm, SMTP, WebAuthn policies, required actions, events
  flows.tf                   realliance-browser, realliance-admin-browser, realliance-link-only
  builtin.tf                 import + override of security-admin-console and admin-cli
  idps.tf                    google, github, microsoft, discord
  scopes.tf                  `groups` client scope
  groups.tf                  /realmic /realliance /apps /admins
  apps.tf                    module instances (acceptance-tests now; forgejo in project 2)
  automation.tf              tofu-plan read-only roles on realliance-realm (master)
  delegation.tf              FGAP v2 permissions + policies
  modules/app/{main,variables,outputs}.tf
dns/
  backend.tf providers.tf records.tf
tests/
  conftest.py                shared fixtures (admin token, realm URL, test users)
  host/test_base.py test_postgresql.py test_keycloak.py test_tls.py test_master_realm.py
       test_pull.py test_backup.py
  realm/test_realm.py test_brokering.py test_apps.py test_claims.py test_delegation.py
docs/runbook.md              restore, onboard an app, invite a person, key rotation, yearly runway check
.github/workflows/realm.yml  plan (PR, env `plan`) / apply (main, env `production`)
```

`~/Repos/realliance-sso-secrets` → `github.com/realliance/sso-secrets` (private, git-crypt)

```
.gitattributes               everything except .gitattributes and README.md → filter=git-crypt
README.md                    what lives here, how to unlock
host/secrets.yml             copied to /etc/realliance-sso/secrets.yml on the box
realm/secrets.env            TF_VAR_* and provider creds (mirrors GitHub environment secrets)
dns/secrets.env              CLOUDFLARE_API_TOKEN, state creds
break-glass.md               root password, master-realm admin passwords, CrownCloud + B2 + Resend logins
```

### `host/secrets.yml` keys (Interfaces for every host task)

```yaml
postgres_keycloak_password:      # Task 3
keycloak_bootstrap_admin_password: # Task 4 (temporary admin, deleted in Task 6)
tofu_apply_client_secret:        # Task 6
tofu_plan_client_secret:         # Task 6
break_glass_chris_password:      # Task 6
break_glass_connor_password:     # Task 6
b2_backup_key_id:                # Task 8
b2_backup_application_key:       # Task 8
```

### `tests/conftest.py` helpers (Interfaces for every test)

| Helper | Signature | Added in |
|--------|-----------|----------|
| `SECRETS`, `secrets` fixture | dict loaded from `~/Repos/realliance-sso-secrets/host/secrets.yml` + `realm/secrets.env` | Task 1 |
| `GROUP_VARS`, `OPERATOR_SSH_KEYS` | parsed `host/group_vars/sso.yml` | Task 1 |
| `get/put/post(path, token, json=None) -> requests.Response` | path relative to `https://sso.realliance.net` | Task 6 |
| `admin_token(client="tofu-apply") -> str` | client-credentials against `master` | Task 6 |
| `password_grant(realm, client_id, username, password) -> requests.Response` | direct grant | Task 6 |
| `find_user(realm, username, token) -> dict`, `totp_configured(user) -> bool` | admin REST | Task 6 |
| `realm_get(suffix) -> dict \| list` | `GET /admin/realms/realliance{suffix}` with `admin_token()` | Task 9 |
| `flow_executions(alias) -> list[dict]`, `flow_id(alias) -> str`, `client_by_id(client_id) -> dict` | admin REST | Tasks 9–10 |
| `user_factory(groups: list[str]) -> User(id, name, pw)` | creates a realliance user with a password and no required actions; deletes at teardown | Task 10 |
| `create_group(name) -> id`, `delete_group(id)` | top-level group, admin REST | Task 11 |
| `acceptance_clients` fixture | enables `acceptance-tests` + `acceptance-tests-noscope` for the session, disables at teardown | Task 11 |
| `decode(jwt) -> dict` | unverified payload decode | Task 11 |
| `delegate` fixture -> `Delegate(id, token)`; `target_user` fixture | a user in `/admins/apps-acceptance-tests`, token via `password_grant` on `acceptance-tests` | Task 12 |
| `realm_management_role(name) -> Role(client, rep)`, `invite_via_organization(token, email) -> Response`, `group_id(path) -> str` | admin REST | Task 12 |
| `tofu_output(name) -> str` and constants `ACCESS_GROUP_ID`, `REALLIANCE_GROUP_ID`, `OTHER_CLIENT_UUID`, `CHRIS_USERNAME`, `CONNOR_USERNAME` | `tofu -chdir=realm output -raw`; usernames from `realm/terraform.tfvars` | Task 12 |

### `host/group_vars/sso.yml` (non-secret values, pinned)

```yaml
sso_hostname: sso.realliance.net
keycloak_version: "26.8.1"
keycloak_sha256: "<computed in Task 4 after verifying the .asc signature>"
keycloak_heap_opts: "-Xms256m -Xmx896m"
keycloak_db_pool_max: 20
postgresql_stream: "16"
postgresql_shared_buffers: "128MB"
postgresql_max_connections: 40
journald_system_max_use: "500M"
pull_repo_url: https://github.com/realliance/sso.git
pull_branch: main
pull_interval: 15min
backup_time_utc: "11:00"
backup_bucket: realliance-sso-backups
b2_s3_endpoint: https://s3.us-west-004.backblazeb2.com
b2_region: us-west-004
operator_ssh_keys: [ "<chris ed25519 pub>", "<connor ed25519 pub>" ]
```

---

### Task 1: Scaffold the repos and harden the box

The box currently accepts root password logins; this goes first.

**Files:**
- Create: `~/Repos/realliance-sso/{README.md,flake.nix,.gitignore}`, `host/{ansible.cfg,inventory.yml,requirements.yml,site.yml,group_vars/sso.yml}`, `host/roles/secrets/tasks/main.yml`, `host/roles/base/{tasks/main.yml,handlers/main.yml,templates/sshd-realliance.conf.j2,templates/journald-realliance.conf.j2}`
- Create: `~/Repos/realliance-sso-secrets/{.gitattributes,README.md,host/secrets.yml,break-glass.md}` (local only until Task 2)
- Test: `tests/conftest.py`, `tests/host/test_base.py`

**Interfaces:**
- Produces: role `secrets` — slurps `/etc/realliance-sso/secrets.yml` (root 0600) and sets every key above as a fact; fails with `"missing /etc/realliance-sso/secrets.yml — copy it from realliance/sso-secrets/host/secrets.yml"` if absent. Every later role reads secrets only through these facts.
- Produces: `pytest tests/host --hosts=ssh://root@23.163.0.48` as the host test command (testinfra `host` fixture).

- [ ] **Step 1: Initialise both repos locally.** `git init` each; set `commit.gpgsign true` and Chris's signing key in both; in `realliance-sso-secrets` run `git-crypt init` and `git-crypt add-gpg-user <chris key id>`; `.gitattributes` = `* filter=git-crypt diff=git-crypt` plus `.gitattributes !filter !diff` and `README.md !filter !diff`. `flake.nix` devShell as listed in File Structure (pin nixpkgs in `flake.lock`). README links the spec at `https://github.com/ChristopherJMiller/luma-homeops/blob/main/docs/superpowers/specs/2026-10-31-realliance-sso-design.md`.
- [ ] **Step 2: Put the current root password and fresh secrets in `sso-secrets`.** Move the contents of `luma-homeops/.crownpanel.vm` into `break-glass.md` (old root password), generate a new root password and `postgres_keycloak_password`/`keycloak_bootstrap_admin_password` (`openssl rand -base64 32`), write `host/secrets.yml` with the keys known so far. Verify `git-crypt status` lists both files as encrypted, then commit.
- [ ] **Step 3: Write the failing tests** in `tests/host/test_base.py`:

```python
def test_password_auth_refused():
    r = subprocess.run(["ssh", "-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password",
                        "-o", "BatchMode=yes", "root@23.163.0.48", "true"], capture_output=True)
    assert r.returncode != 0 and b"Permission denied (publickey)" in r.stderr

def test_firewall_services(host):
    out = host.check_output("firewall-cmd --list-services").split()
    assert sorted(out) == ["http", "https", "ssh"]

def test_template_extras_removed(host):
    for pkg in ["httpd", "sendmail", "cockpit"]:
        assert not host.package(pkg).is_installed

def test_journald_capped(host):
    assert "SystemMaxUse=500M" in host.file("/etc/systemd/journald.conf.d/realliance.conf").content_string

def test_operator_keys_installed(host):
    keys = host.file("/root/.ssh/authorized_keys").content_string
    assert all(k.split()[1] in keys for k in OPERATOR_SSH_KEYS)  # from group_vars

def test_secrets_file_root_only(host):
    f = host.file("/etc/realliance-sso/secrets.yml")
    assert f.user == "root" and f.mode == 0o600

def test_clock_close_to_operator(host):
    assert abs(int(host.check_output("date +%s")) - int(time.time())) < 5
```

- [ ] **Step 4: Run them to see them fail.** First get a session: `SSH_ASKPASS=<script cat'ing break-glass root pw> SSH_ASKPASS_REQUIRE=force ssh -o PubkeyAuthentication=no -o ControlMaster=auto -o ControlPath=/tmp/claude-1000/cm-rv -o ControlPersist=30m root@23.163.0.48 true`. Run: `nix develop -c pytest tests/host/test_base.py --hosts=ssh://root@23.163.0.48 --ssh-config=<file with ControlPath>`. Expected: FAIL on password auth, firewall, extras, journald, secrets file.
- [ ] **Step 5: Implement roles `secrets` and `base`.** `base`: install `operator_ssh_keys` as root's `authorized_keys` (exclusive); drop-in `/etc/ssh/sshd_config.d/10-realliance.conf` with `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `PermitRootLogin prohibit-password`; remove `httpd sendmail cockpit*`; firewalld services exactly `ssh http https`; journald drop-in `SystemMaxUse=500M`; `dnf-automatic` with `apply_updates = yes` for security updates; set root password from `break-glass.md`'s new value via `ansible.builtin.user` with `password_hash('sha512')`. Copy `host/secrets.yml` to `/etc/realliance-sso/secrets.yml` (0600) **before** the first run — a one-off `scp` documented in `docs/runbook.md` ("placing host secrets").
- [ ] **Step 6: Apply in push mode and keep the session open until the key works.** `nix develop -c ansible-playbook -i host/inventory.yml host/site.yml --tags secrets,base` (password via the ControlPath session). Then verify key login from a new terminal *before* closing the old session: `ssh -o ControlPath=none root@23.163.0.48 true`.
- [ ] **Step 7: Run the tests to see them pass.** Same command as Step 4 without the ControlPath. Expected: 7 passed.
- [ ] **Step 8: Commit** (signed) in `realliance-sso`: `git add -A && git commit -S -m "host: base hardening (key-only SSH, firewalld, journald cap)"`. In luma-homeops, `rm .crownpanel.vm` once `break-glass.md` holds the value.

### Task 2: Publish both repos with their guardrails

**Files:**
- Create: `keys/chris.asc`, `keys/connor.asc`, `keys/github-web-flow.asc` (from `https://github.com/web-flow.gpg`)
- Create: `scripts/github-guardrails.sh` — idempotent `gh api` calls for the ruleset and environments
- Test: `tests/test_github_guardrails.py`

**Interfaces:**
- Produces: GitHub environments `plan` (no reviewers, all branches) and `production` (required reviewers: Chris + Connor; deployment branches: `main` only). Task 13 consumes them.
- Produces: ruleset `main-protection` on `realliance/sso`.

- [ ] **Step 1: Write the failing test** `tests/test_github_guardrails.py` (uses `gh api`):

```python
def test_sso_is_public():           assert gh("repos/realliance/sso")["visibility"] == "public"
def test_secrets_repo_private():    assert gh("repos/realliance/sso-secrets")["visibility"] == "private"
def test_ruleset_rules():
    rules = {r["type"]: r for r in ruleset("realliance/sso", "main-protection")["rules"]}
    assert {"pull_request", "required_signatures", "non_fast_forward", "deletion"} <= rules.keys()
    assert rules["pull_request"]["parameters"]["required_approving_review_count"] == 0  # raised to 1 in Task 15
def test_production_env_reviewers():
    env = gh("repos/realliance/sso/environments/production")
    assert {r["reviewer"]["login"] for p in env["protection_rules"] if p["type"] == "required_reviewers"
            for r in p["reviewers"]} == {"ChristopherJMiller", CONNOR_GH}
def test_secrets_repo_encrypted_without_key(tmp_path):
    clone("realliance/sso-secrets", tmp_path)          # fresh clone, no git-crypt unlock
    assert not (tmp_path / "host/secrets.yml").read_bytes().startswith(b"postgres")
```

- [ ] **Step 2: Run to see it fail.** `nix develop -c pytest tests/test_github_guardrails.py`. Expected: FAIL (repos missing).
- [ ] **Step 3: Create the repos and push.** `gh repo create realliance/sso --public --source . --push`; `gh repo create realliance/sso-secrets --private --source ~/Repos/realliance-sso-secrets --push`. Add Connor as a collaborator with `write` on both. `git-crypt add-gpg-user <connor key id>` in sso-secrets, commit, push.
- [ ] **Step 4: Implement `scripts/github-guardrails.sh`** (ruleset: target `~DEFAULT_BRANCH`, rules `pull_request` with `required_approving_review_count: 0`, `required_signatures`, `non_fast_forward`, `deletion`; environments as in Interfaces) and run it.
- [ ] **Step 5: Run to see it pass.** Expected: 5 passed.
- [ ] **Step 6: Commit** via a branch + PR (the ruleset now applies): `git switch -c guardrails && git commit -S -am "github: ruleset and environments as code" && gh pr create --fill && gh pr merge --squash`.

### Task 3: PostgreSQL

**Files:**
- Create: `host/roles/postgresql/{tasks/main.yml,templates/postgresql-realliance.conf.j2,templates/pg_hba.conf.j2,handlers/main.yml}`
- Test: `tests/host/test_postgresql.py`

**Interfaces:**
- Consumes: fact `postgres_keycloak_password` (Task 1 `secrets`).
- Produces: database `keycloak`, role `keycloak` (scram-sha-256), listening on `127.0.0.1:5432` only. Task 4 connects with `jdbc:postgresql://127.0.0.1:5432/keycloak`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_pg16_running(host):
    assert host.service("postgresql").is_running
    assert host.check_output("sudo -u postgres psql -tAc 'show server_version'").startswith("16.")
def test_listens_localhost_only(host):
    assert host.socket("tcp://127.0.0.1:5432").is_listening
    assert not host.socket("tcp://0.0.0.0:5432").is_listening
def test_keycloak_role_can_connect(host, secrets):
    host.run_expect([0], f"PGPASSWORD='{secrets['postgres_keycloak_password']}' psql -h 127.0.0.1 -U keycloak -d keycloak -c 'select 1'")
def test_memory_settings(host):
    q = lambda s: host.check_output(f"sudo -u postgres psql -tAc 'show {s}'")
    assert q("shared_buffers") == "128MB" and q("max_connections") == "40"
```

(`secrets` fixture in `conftest.py` reads `~/Repos/realliance-sso-secrets/host/secrets.yml`.)
- [ ] **Step 2: Run to see them fail.** `pytest tests/host/test_postgresql.py --hosts=ssh://root@23.163.0.48`. Expected: FAIL.
- [ ] **Step 3: Implement role `postgresql`.** `dnf module enable postgresql:16`, install `postgresql-server postgresql-contrib python3-psycopg2`, `postgresql-setup --initdb` (creates guard), drop-in conf (`listen_addresses='127.0.0.1'`, values from group_vars, `password_encryption=scram-sha-256`), `pg_hba` line `host keycloak keycloak 127.0.0.1/32 scram-sha-256`, create role/db with `community.postgresql`.
- [ ] **Step 4: Apply and run tests.** `ansible-playbook -i host/inventory.yml host/site.yml --tags secrets,postgresql`; tests → 4 passed.
- [ ] **Step 5: Commit** via PR: `"host: postgresql 16 for keycloak"`.

### Task 4: Keycloak service

**Files:**
- Create: `host/roles/keycloak/{tasks/main.yml,handlers/main.yml,templates/keycloak.conf.j2,templates/keycloak.env.j2,templates/keycloak.service.j2}`
- Test: `tests/host/test_keycloak.py`

**Interfaces:**
- Consumes: Task 3 database; facts `postgres_keycloak_password`, `keycloak_bootstrap_admin_password`.
- Produces: `keycloak.service` (user `keycloak`), HTTP on `127.0.0.1:8080`, management (health, metrics) on port 9000, install at `/opt/keycloak-26.8.1` with symlink `/opt/keycloak`. Temporary master admin `temp-admin` created by `KC_BOOTSTRAP_ADMIN_USERNAME/PASSWORD` on first start (removed in Task 6). Handler chain: config change → `kc.sh build` → restart.

- [ ] **Step 1: Pin the archive.** Download `keycloak-26.8.1.tar.gz` and `.asc`, import Keycloak's release signing key, `gpg --verify`, then `sha256sum` → write `keycloak_sha256` into `group_vars/sso.yml`.
- [ ] **Step 2: Write the failing tests:**

```python
def test_service_running(host):
    assert host.service("keycloak").is_running and host.service("keycloak").is_enabled
def test_ready(host):
    assert '"status": "UP"' in host.check_output("curl -s http://127.0.0.1:9000/health/ready")
def test_http_bound_to_localhost(host):
    assert host.socket("tcp://127.0.0.1:8080").is_listening
    assert not host.socket("tcp://0.0.0.0:8080").is_listening
def test_heap(host):
    assert "-Xmx896m" in host.check_output("ps -o args= -C java")
def test_build_options_persisted(host):
    out = host.check_output("sudo -u keycloak /opt/keycloak/bin/kc.sh show-config")
    assert any(l.split()[:1] == ["kc.db"] and "postgres" in l for l in out.splitlines())  # optimized build used db=postgres
def test_env_file_private(host):
    f = host.file("/etc/keycloak/keycloak.env"); assert f.mode == 0o600 and f.user == "keycloak"
```

- [ ] **Step 3: Run to see them fail.** Expected: FAIL.
- [ ] **Step 4: Implement role `keycloak`.** Install `java-21-openjdk-headless`; `get_url` with `checksum: "sha256:{{ keycloak_sha256 }}"`; unpack; `keycloak.conf`: `db=postgres`, `db-url=jdbc:postgresql://127.0.0.1:5432/keycloak`, `db-username=keycloak`, `db-pool-max-size={{ keycloak_db_pool_max }}`, `hostname=https://{{ sso_hostname }}`, `http-enabled=true`, `http-host=127.0.0.1`, `http-port=8080`, `proxy-headers=xforwarded`, `health-enabled=true`, `metrics-enabled=true`. Env file: `KC_DB_PASSWORD`, `KC_BOOTSTRAP_ADMIN_USERNAME=temp-admin`, `KC_BOOTSTRAP_ADMIN_PASSWORD`, `JAVA_OPTS_KC_HEAP={{ keycloak_heap_opts }}`. Unit: `ExecStart=/opt/keycloak/bin/kc.sh start --optimized`, `Restart=on-failure`, `EnvironmentFile=/etc/keycloak/keycloak.env`. Confirm passkey support is on by default in 26.8.1 (`/admin/serverinfo` → `profileInfo.disabledFeatures` lacks `PASSKEYS`); if not, add `features=passkeys` to the conf (a build option → the build handler covers it).
- [ ] **Step 5: Apply, then run tests.** Expected: 6 passed. Note the idle RSS (`ps -o rss= -C java`) in the commit message.
- [ ] **Step 6: Commit** via PR: `"host: keycloak 26.8.1 service"`.

### Task 5: DNS record and TLS (Caddy)

**Files:**
- Create: `dns/{backend.tf,providers.tf,records.tf}`; `host/roles/caddy/{tasks/main.yml,templates/Caddyfile.j2,handlers/main.yml}`
- Create in sso-secrets: `dns/secrets.env` (`CLOUDFLARE_API_TOKEN`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` for the state bucket)
- Test: `tests/host/test_tls.py`

**Interfaces:**
- Consumes: Task 4 Keycloak on `127.0.0.1:8080`; B2 state bucket `realliance-sso-tfstate` (created in Step 1).
- Produces: `https://sso.realliance.net/` → Keycloak. State key `dns/terraform.tfstate`.

- [ ] **Step 1: [Chris] Create the state bucket and key.** With the B2 master key: bucket `realliance-sso-tfstate` (private, SSE-B2, lifecycle `daysFromHidingToDeleting: 90`), application key `realliance-sso-tfstate` scoped to it with `listBuckets,listFiles,readFiles,writeFiles,deleteFiles` (lock files need delete). Cloudflare token: Zone → DNS → Edit, zone `realliance.net` only. Store all in `sso-secrets/dns/secrets.env` and `realm/secrets.env`.
- [ ] **Step 2: Write the failing tests:**

```python
def test_dns_points_at_box():
    assert socket.gethostbyname("sso.realliance.net") == "23.163.0.48"
def test_discovery_over_tls():
    r = requests.get("https://sso.realliance.net/realms/master/.well-known/openid-configuration", timeout=10)
    assert r.json()["issuer"] == "https://sso.realliance.net/realms/master"
def test_http_redirects():
    r = requests.get("http://sso.realliance.net/", allow_redirects=False, timeout=10)
    assert r.status_code in (301, 308) and r.headers["location"].startswith("https://")
def test_management_not_public():
    for port in (8080, 9000):
        with pytest.raises(OSError): socket.create_connection(("sso.realliance.net", port), timeout=3)
```

- [ ] **Step 3: Run to see them fail.**
- [ ] **Step 4: Implement.** `dns/backend.tf`: `backend "s3" { bucket = "realliance-sso-tfstate", key = "dns/terraform.tfstate", region = "us-west-004", endpoints = { s3 = "https://s3.us-west-004.backblazeb2.com" }, use_lockfile = true, skip_credentials_validation = true, skip_region_validation = true, skip_requesting_account_id = true, skip_s3_checksum = true, use_path_style = true }`. `records.tf`: one `cloudflare_dns_record` `sso` A `23.163.0.48`, `proxied = false`, `ttl = 300`. Confirm luma-homeops `cloudflare/dns/` does not declare `sso.realliance.net` (it doesn't today — keep it that way). Caddy: enable COPR `@caddy/caddy`, install, Caddyfile `{{ sso_hostname }} { reverse_proxy 127.0.0.1:8080 }`.
- [ ] **Step 5: Apply.** `cd dns && set -a && . ~/Repos/realliance-sso-secrets/dns/secrets.env && tofu init && tofu apply`; then `ansible-playbook ... --tags caddy`. Run tests → 4 passed.
- [ ] **Step 6: Commit** via PR: `"dns+caddy: sso.realliance.net with ACME TLS"`.

### Task 6: Master-realm bootstrap (automation clients, break-glass admins)

**Files:**
- Create: `host/roles/master_realm/tasks/main.yml`
- Modify: `sso-secrets/host/secrets.yml` (add `tofu_apply_client_secret`, `tofu_plan_client_secret`, `break_glass_*_password`), `sso-secrets/break-glass.md`
- Test: `tests/host/test_master_realm.py`

**Interfaces:**
- Produces in `master`: client `tofu-apply` (confidential, service account only, realm role `admin`); client `tofu-plan` (confidential, service account only, **no roles yet** — Task 9 grants view roles on `realliance-realm`); users `chris-admin`, `connor-admin` (realm role `admin`, required action `CONFIGURE_TOTP`). `temp-admin` absent.
- Produces: token helper used by later tests — `conftest.admin_token(client="tofu-apply") -> str` (client-credentials grant against master).

- [ ] **Step 1: Write the failing tests:**

```python
def test_tofu_apply_can_admin():
    t = admin_token("tofu-apply"); assert get("/admin/realms", t).status_code == 200
def test_tofu_plan_cannot_create_realm():
    t = admin_token("tofu-plan"); assert post("/admin/realms", t, {"realm": "x"}).status_code == 403
def test_temp_admin_gone():
    assert password_grant("master", "admin-cli", "temp-admin", SECRETS["keycloak_bootstrap_admin_password"]).status_code == 401
def test_break_glass_admins_need_totp():
    for u in ("chris-admin", "connor-admin"):
        user = find_user("master", u, admin_token("tofu-apply"))
        assert "CONFIGURE_TOTP" in user["requiredActions"] or totp_configured(user)
```

- [ ] **Step 2: Run to see them fail.**
- [ ] **Step 3: Implement role `master_realm`.** Obtain a token with `ansible.builtin.uri` (client-credentials as `tofu-apply`; on 401 fall back to password grant as `temp-admin` — first run only) and pass it as `token:` to `community.general.keycloak_client` / `keycloak_user` / `keycloak_user_rolemapping`. Client secrets come from facts. Delete `temp-admin` last, only after a `tofu-apply` token succeeds. Remove `KC_BOOTSTRAP_ADMIN_*` from `keycloak.env` afterwards (no restart needed — read only at first boot).
- [ ] **Step 4: Apply and run tests** → 4 passed. Chris and Connor each sign in once at `https://sso.realliance.net/admin/master/console/` and enrol TOTP.
- [ ] **Step 5: Commit** via PR: `"host: master realm automation clients and break-glass admins"`.

### Task 7: `ansible-pull` with signature verification

**Files:**
- Create: `host/inventory-pull.yml`, `host/roles/pull/{tasks/main.yml,templates/sso-pull.service.j2,templates/sso-pull.timer.j2,templates/sso-pull-daily.timer.j2}`
- Test: `tests/host/test_pull.py`

**Interfaces:**
- Consumes: `keys/*.asc` (Task 2).
- Produces: `sso-pull.service` (oneshot): `GNUPGHOME=/etc/realliance-sso/gnupg ansible-pull -U {{ pull_repo_url }} -C {{ pull_branch }} -d /var/lib/sso-pull --verify-commit --only-if-changed -i host/inventory-pull.yml host/site.yml`; `sso-pull.timer` every `{{ pull_interval }}`; `sso-pull-daily.timer` runs the same unit with `--only-if-changed` removed (drift correction, 04:00 UTC). Task 14 reads `systemctl show sso-pull.service -p ExecMainExitTimestamp,Result`.

- [ ] **Step 1: Write the failing tests:**

```python
def test_timer_active(host):
    assert host.service("sso-pull.timer").is_enabled and host.service("sso-pull.timer").is_running
def test_keyring_has_operators_and_web_flow(host):
    out = host.check_output("GNUPGHOME=/etc/realliance-sso/gnupg gpg --list-keys --with-colons")
    for fpr in OPERATOR_FPRS + [GITHUB_WEB_FLOW_FPR]: assert fpr in out
def test_pull_accepts_web_flow_signed(host):
    host.run_expect([0], "systemctl start sso-pull.service")   # main's head is a GitHub squash-merge
    assert host.check_output("systemctl show sso-pull.service -p Result") == "Result=success"
def test_pull_rejects_unsigned(host):
    # branch `pull-test-unsigned` holds one unsigned commit, pushed by the test fixture
    r = host.run("GNUPGHOME=/etc/realliance-sso/gnupg ansible-pull -U {url} -C pull-test-unsigned -d /tmp/pt --verify-commit host/site.yml")
    assert r.rc != 0 and "signature" in (r.stderr + r.stdout).lower()
```

(`pull-test-unsigned` is created by the test fixture with `git -c commit.gpgsign=false` and deleted at teardown; the ruleset's `required_signatures` applies only to `main`.)
- [ ] **Step 2: Run to see them fail.**
- [ ] **Step 3: Implement role `pull`.** Install `ansible-core git` from AppStream; `ansible-galaxy collection install -r host/requirements.yml -p /usr/share/ansible/collections`; create `/etc/realliance-sso/gnupg` (0700) and import `keys/*.asc` with ultimate trust; units as in Interfaces. Keep playbooks compatible with the AppStream `ansible-core` version (check `ansible --version` on the box and note it in `requirements.yml`).
- [ ] **Step 4: Apply in push mode once, then run tests** → 4 passed.
- [ ] **Step 5: Commit** via PR (squash-merge in the UI, which itself exercises `test_pull_accepts_web_flow_signed`): `"host: ansible-pull with signature verification"`.

### Task 8: Nightly backups and restore

**Files:**
- Create: `host/roles/backup/{tasks/main.yml,templates/sso-backup.service.j2,templates/sso-backup.timer.j2,templates/rclone.env.j2,files/sso-backup.sh,files/sso-restore.sh}`
- Test: `tests/host/test_backup.py`

**Interfaces:**
- Consumes: facts `b2_backup_key_id`, `b2_backup_application_key`.
- Produces: `/usr/local/sbin/sso-backup` (dump `keycloak` with `pg_dump -Fc` to `/var/backups/sso/keycloak-<UTC date>.dump`, keep 7 locally, `rclone copy` to `b2:{{ backup_bucket }}/keycloak/`); `/usr/local/sbin/sso-restore <dump-path-or-b2-object> [--target-db NAME]` (default target `keycloak`: stops `keycloak`, drops/recreates the DB, `pg_restore`, starts `keycloak`; with `--target-db` restores into that DB without touching the service). Timer at `{{ backup_time_utc }}` UTC.

- [ ] **Step 1: [Chris] Create the bucket and key.** Bucket `realliance-sso-backups` (private, SSE-B2, lifecycle on `keycloak/`: `daysFromUploadingToHiding: 30`, `daysFromHidingToDeleting: 1`); key `realliance-sso-backup` on that bucket with `listBuckets,listFiles,readFiles,writeFiles` (**no** `deleteFiles`). Add both values to `sso-secrets/host/secrets.yml`; re-copy the file to the box.
- [ ] **Step 2: Write the failing tests:**

```python
def test_backup_run_succeeds(host):
    host.run_expect([0], "systemctl start sso-backup.service")
    assert host.check_output("systemctl show sso-backup.service -p Result") == "Result=success"
def test_backup_newest_object_fresh(host):
    newest = host.check_output("set -a; . /etc/realliance-sso/rclone.env; rclone lsf b2:realliance-sso-backups/keycloak/ | sort | tail -1")
    assert newest == f"keycloak-{datetime.utcnow():%Y-%m-%d}.dump"
def test_restore_into_scratch_db_matches(host):
    host.run_expect([0], "sso-restore /var/backups/sso/$(ls -t /var/backups/sso | head -1) --target-db keycloak_restore_test")
    count = lambda db: host.check_output(f"sudo -u postgres psql -d {db} -tAc 'select count(*) from user_entity'")
    assert count("keycloak_restore_test") == count("keycloak")
    host.run("sudo -u postgres dropdb keycloak_restore_test")
def test_key_cannot_delete(host):
    r = host.run("set -a; . /etc/realliance-sso/rclone.env; "
                 "rclone deletefile b2:realliance-sso-backups/keycloak/$(rclone lsf b2:realliance-sso-backups/keycloak/ | sort | tail -1)")
    assert r.rc != 0   # key lacks deleteFiles
```

(rclone config via `RCLONE_CONFIG_B2_*` env from `/etc/realliance-sso/rclone.env`, `type=s3 provider=Other no_check_bucket=true`, endpoint/region from group_vars — same pattern as `luma-homeops/docs/backups.md`.)
- [ ] **Step 3: Run to see them fail.**
- [ ] **Step 4: Implement role `backup`** (install `rclone` from EPEL; the two scripts with `set -euo pipefail`; units).
- [ ] **Step 5: Apply, run tests** → 4 passed.
- [ ] **Step 6: Commit** via PR: `"host: nightly pg_dump to B2 and sso-restore"`.

### Task 9: Realm foundation (realm, SMTP, passkey-first sign-in)

**Files:**
- Create: `realm/{backend.tf,providers.tf,variables.tf,realm.tf,flows.tf,automation.tf,outputs.tf}`
- Create in sso-secrets: `realm/secrets.env` (`KEYCLOAK_CLIENT_SECRET` for `tofu-apply`, state creds, `TF_VAR_smtp_password`)
- Modify (luma-homeops, via the `cloudflare-dns` skill): `cloudflare/dns/` — Resend records for `realliance.net`
- Test: `tests/realm/test_realm.py`

**Interfaces:**
- Consumes: `tofu-apply`/`tofu-plan` clients (Task 6); state bucket (Task 5).
- Produces: `keycloak_realm.realliance` (exported `realm_id`); flows `realliance-browser` (realm browser binding) and `realliance-admin-browser`; output `browser_flow_alias`. Provider config: `url = "https://sso.realliance.net"`, `realm = "master"`, `client_id = "tofu-apply"`, secret from `KEYCLOAK_CLIENT_SECRET`. State key `realm/terraform.tfstate`.

- [ ] **Step 1: [Chris] Resend.** Add domain `realliance.net`, copy its DKIM (`resend._domainkey`) and bounce (`send` MX + SPF TXT) records into luma-homeops `cloudflare/dns/` (new file `realliance-mail.tf`), `./cloudflare/dns/tf.sh plan` shows only those adds, apply, wait for Resend "verified". Create an API key with **sending access to `realliance.net` only** → `TF_VAR_smtp_password`.
- [ ] **Step 2: Write the failing tests** (`conftest.realm_get(path)` uses a `tofu-apply` token):

```python
def test_realm_settings():
    r = realm_get("")
    assert r["registrationAllowed"] is False and r["verifyEmail"] is True
    assert r["duplicateEmailsAllowed"] is False and r["resetPasswordAllowed"] is True
    assert r["adminPermissionsEnabled"] is True and r["organizationsEnabled"] is True
def test_smtp():
    s = realm_get("")["smtpServer"]
    assert s["host"] == "smtp.resend.com" and s["port"] == "465" and s["ssl"] == "true"
    assert s["from"] == "sso@realliance.net" and s["fromDisplayName"] == "Realliance SSO"
def test_webauthn_passwordless_policy():
    r = realm_get("")
    assert r["webAuthnPolicyPasswordlessRpId"] == "sso.realliance.net"
    assert r["webAuthnPolicyPasswordlessUserVerificationRequirement"] == "required"
    assert r["webAuthnPolicyPasswordlessRequireResidentKey"] == "Yes"
def test_browser_flow_bound():
    assert realm_get("")["browserFlow"] == "realliance-browser"
def test_password_path_requires_otp():
    ex = flow_executions("realliance-browser")
    assert any(e["providerId"] == "auth-otp-form" and e["requirement"] == "REQUIRED" for e in ex)
def test_recovery_codes_enabled():
    ra = {a["alias"]: a for a in realm_get("/authentication/required-actions")}
    assert ra["CONFIGURE_RECOVERY_AUTHN_CODES"]["enabled"] is True
def test_tofu_plan_has_view_only():
    t = admin_token("tofu-plan")
    assert get("/admin/realms/realliance", t).status_code == 200
    assert put("/admin/realms/realliance", t, {"displayName": "x"}).status_code == 403
```

- [ ] **Step 3: Run to see them fail.** `nix develop -c pytest tests/realm/test_realm.py`. Expected: FAIL (realm missing).
- [ ] **Step 4: Implement.** `realm.tf`: realm `realliance`, `display_name = "Realliance"`, settings per tests, `admin_permissions_enabled = true`, `organizations_enabled = true`, `smtp_server` with `auth { username = "resend", password_wo = var.smtp_password, password_wo_version = "v1" }`, `web_authn_passwordless_policy` per tests, `keycloak_required_action` for `CONFIGURE_TOTP`, `CONFIGURE_RECOVERY_AUTHN_CODES`, `webauthn-register-passwordless` (enabled). `flows.tf`: `realliance-browser` = cookie (ALTERNATIVE) → IdP redirector (ALTERNATIVE) → forms subflow (ALTERNATIVE) containing username-password form with passkey conditional UI (REQUIRED) then a conditional subflow "password used" → OTP form (REQUIRED); bind as realm browser flow via `keycloak_authentication_bindings`. `realliance-admin-browser` = same, but OTP-or-passkey REQUIRED after any first factor including an IdP login (used in Task 10). `automation.tf`: grant `tofu-plan`'s service account the `realliance-realm` client roles `view-realm view-clients view-users view-identity-providers view-authorization query-groups query-clients` in `master`.
- [ ] **Step 5: Apply from the laptop and run tests.** `cd realm && set -a && . ~/Repos/realliance-sso-secrets/realm/secrets.env && tofu init && tofu apply`; tests → 7 passed. Send a test email from the realm (admin console → Email → Test) to Chris's address; confirm DKIM pass in the received headers.
- [ ] **Step 6: Commit** via PR: `"realm: realliance realm, SMTP, passkey-first browser flow"`.

### Task 10: Linked logins, first-broker-login guard, admin step-up

**Files:**
- Create: `realm/idps.tf`, `realm/builtin.tf`; add `realliance-link-only` to `realm/flows.tf`
- Modify (luma-homeops, optional): `azure/entra/realliance-sso.tf` following `azure/entra/dex.tf` for the Microsoft app
- Test: `tests/realm/test_brokering.py`

**Interfaces:**
- Consumes: `realliance-admin-browser` (Task 9).
- Produces: IdP aliases `google`, `github`, `microsoft`, `discord`; redirect URI pattern for OAuth apps: `https://sso.realliance.net/realms/realliance/broker/<alias>/endpoint`. TF vars `idp_<alias>_client_id`, `idp_<alias>_client_secret`.

- [ ] **Step 1: [Chris/Connor] Register the four OAuth apps** with the redirect URIs above (GitHub OAuth app owned by the `realliance` org; Microsoft with personal accounts allowed, tenant `common`; Discord scopes `identify email`). Store IDs/secrets in `sso-secrets/realm/secrets.env` as `TF_VAR_idp_*`.
- [ ] **Step 2: Write the failing tests:**

```python
def test_idps_use_link_only_flow():
    for alias in ("google", "github", "microsoft", "discord"):
        idp = realm_get(f"/identity-provider/instances/{alias}")
        assert idp["firstBrokerLoginFlowAlias"] == "realliance-link-only" and idp["trustEmail"] is False
def test_link_only_flow_never_creates_or_autolinks():
    ids = [e["providerId"] for e in flow_executions("realliance-link-only")]
    assert "idp-create-user-if-unique" not in ids and "idp-auto-link" not in ids
    assert ids[0] == "idp-detect-existing-broker-user"
    assert "idp-username-password-form" in ids   # re-authentication proves ownership
def test_admin_console_step_up():
    c = client_by_id("security-admin-console")
    assert c["authenticationFlowBindingOverrides"]["browser"] == flow_id("realliance-admin-browser")
def test_admin_cli_password_grant_refused():
    r = password_grant("realliance", "admin-cli", TEST_USER, TEST_PASSWORD)
    assert r.status_code == 400 and r.json()["error"] == "unauthorized_client"
```

(`TEST_USER` is created and deleted by a `conftest` fixture via the admin API.)
- [ ] **Step 3: Run to see them fail.**
- [ ] **Step 4: Implement.** `idps.tf`: `keycloak_oidc_google_identity_provider`, `keycloak_oidc_github_identity_provider`, `keycloak_oidc_microsoft_identity_provider` (tenant `common`), and Discord as `keycloak_oidc_identity_provider` with `provider_id = "oauth2"`, authorize `https://discord.com/oauth2/authorize`, token `https://discord.com/api/oauth2/token`, userinfo `https://discord.com/api/users/@me`, `default_scopes = "identify email"`. If the provider rejects `oauth2`, stop and tell Chris (Discord is the one optional provider that may wait). All: `first_broker_login_flow_alias = "realliance-link-only"`, `trust_email = false`, `sync_mode = "IMPORT"`. `realliance-link-only`: detect-existing-broker-user (REQUIRED) → re-authentication subflow (REQUIRED: username-password form + OTP-or-passkey). `builtin.tf`: OpenTofu `import` blocks for `security-admin-console` and `admin-cli` in `realliance`; set the console's browser override to `realliance-admin-browser`; set `admin-cli` `direct_access_grants_enabled = false`.
- [ ] **Step 5: Apply, run tests** → 4 passed. Manual: Chris links and unlinks GitHub on his own account in the account console.
- [ ] **Step 6: Commit** via PR: `"realm: linked logins with link-only first-broker flow; admin step-up"`.

### Task 11: Groups, the `groups` claim, and the app module (default deny)

**Files:**
- Create: `realm/{scopes.tf,groups.tf,apps.tf}`, `realm/modules/app/{main.tf,variables.tf,outputs.tf}`
- Test: `tests/realm/test_apps.py`, `tests/realm/test_claims.py`

**Interfaces:**
- Produces module `app` — inputs: `realm_id string`, `client_id string`, `name string`, `redirect_uris list(string)`, `roles list(string) = ["user"]`, `extra_default_scopes list(string) = []` (e.g. `["groups"]`), `access_type string = "CONFIDENTIAL"`, `direct_access_grants bool = false`, `enabled bool = true`, `apps_parent_group_id string`, `admins_parent_group_id string`; outputs: `client_uuid`, `access_group_id` (group `/apps/<client_id>`, granted role `<client_id>.user`), `admin_group_id` (group `/admins/apps-<client_id>`), `deny_flow_alias` (`<client_id>-browser`: a copy of `realliance-browser` ending in a conditional subflow — *Condition - user role* on `<client_id>.user`, negated → *Deny access*; bound as the client's browser override, and as direct-grant override when `direct_access_grants`).
- Produces: client scope `groups` (group-membership mapper, claim `groups`, `full_path = false`, in ID token, access token, userinfo), **not** a realm default scope. Groups `/realmic`, `/realliance`, `/apps`, `/admins`. Instance `module.acceptance_tests` (`client_id = "acceptance-tests"`, `direct_access_grants = true`, `enabled = false`, `extra_default_scopes = ["groups"]`) plus `module.acceptance_tests_noscope` (same, no `groups` scope). Project 2 adds `module.forgejo` the same way.
- Consumes in tests: fixture `acceptance_clients` enables both clients via admin API for the session and disables them at teardown.

- [ ] **Step 1: Write the failing tests:**

```python
def test_user_without_role_denied(acceptance_clients, user_factory):
    u = user_factory(groups=[])
    assert password_grant("realliance", "acceptance-tests", u.name, u.pw).status_code == 401
def test_user_with_access_group_gets_token(acceptance_clients, user_factory):
    u = user_factory(groups=["/apps/acceptance-tests"])
    assert password_grant("realliance", "acceptance-tests", u.name, u.pw).status_code == 200
def test_groups_claim_flat_array(acceptance_clients, user_factory):
    u = user_factory(groups=["/apps/acceptance-tests", "/realliance"])
    claims = decode(password_grant("realliance", "acceptance-tests", u.name, u.pw).json()["id_token"])
    assert sorted(claims["groups"]) == ["acceptance-tests", "realliance"]
def test_no_groups_claim_without_scope(acceptance_clients, user_factory):
    u = user_factory(groups=["/apps/acceptance-tests-noscope", "/realliance"])
    claims = decode(password_grant("realliance", "acceptance-tests-noscope", u.name, u.pw).json()["id_token"])
    assert "groups" not in claims
def test_groups_scope_not_realm_default():
    assert "groups" not in [s["name"] for s in realm_get("/default-default-client-scopes")]
def test_console_group_untouched_by_tofu():
    gid = create_group("hidden-test-project")
    try: assert subprocess.run(["tofu", "-chdir=realm", "plan", "-detailed-exitcode"]).returncode == 0
    finally: delete_group(gid)
```

(Test users get a password and `requiredActions=[]` so direct grant works; OTP is not in the direct-grant flow. The acceptance clients stay `enabled = false` outside test runs.)
- [ ] **Step 2: Run to see them fail.**
- [ ] **Step 3: Implement** the module and instances. No authoritative group-membership or default-group resources in `realm/` **except** `keycloak_group_memberships` on the `/admins/*` groups (Task 12) — those groups are code-owned; every other group's membership is console-managed and must not be touched.
- [ ] **Step 4: Apply, run tests** → 6 passed; `tofu plan -detailed-exitcode` → 0 after the test session (the fixture restored `enabled = false`).
- [ ] **Step 5: Commit** via PR: `"realm: groups claim scope, app module with default deny"`.

### Task 12: Delegation (fine-grained admin permissions v2) and inviting people

**Files:**
- Create: `realm/delegation.tf`; extend `realm/modules/app/main.tf` with owner permissions
- Test: `tests/realm/test_delegation.py`

**Interfaces:**
- Consumes: `module.app` outputs `client_uuid`, `access_group_id`, `admin_group_id`; data source `keycloak_openid_client.admin_permissions` (`client_id = "admin-permissions"`, created by `admin_permissions_enabled`).
- Produces per app: `keycloak_openid_client_group_policy` "members of `/admins/apps-<client_id>`"; `keycloak_group_admin_permissions` on `access_group_id` with scopes `view`, `manage-membership`; `keycloak_openid_client_admin_permissions` on `client_uuid` with scopes `view`, `map-roles`; realm-wide `keycloak_users_admin_permissions` with scope `view` only for the union of admin-group policies (needed to find people to add). Never `manage`, `manage-members`, `reset-password`, `impersonate`.
- Produces for community groups: the same group pattern via `var.community_group_admins` (map group → admin group), for `/realmic` and `/realliance`.
- Produces `/admins/realm`: group granted `realm-management` → `realm-admin`, members Chris's and Connor's realliance usernames — the operators' realm-admin power, reached only through the step-up admin console.
- Produces admin-group membership **as code**: `var.admin_group_members` (map admin-group name → list of usernames) → one `keycloak_group_memberships` per `/admins/*` group, resolving users with `data "keycloak_user"`. A person must exist before the PR that makes them an admin is applied (documented in the runbook).

- [ ] **Step 1: Write the failing tests** (delegate tokens come from `password_grant` on `acceptance-tests`, whose service is enabled by the fixture; admin REST accepts realm tokens carrying FGAP v2 permissions):

```python
def test_delegate_can_add_member(delegate, target_user):
    assert put(f"/admin/realms/realliance/users/{target_user.id}/groups/{ACCESS_GROUP_ID}", delegate.token).status_code == 204
def test_delegate_cannot_reset_password(delegate, target_user):
    assert put(f"/admin/realms/realliance/users/{target_user.id}/reset-password", delegate.token,
               {"type": "password", "value": "x", "temporary": False}).status_code == 403
def test_delegate_cannot_touch_other_groups(delegate):
    assert put(f"/admin/realms/realliance/groups/{REALLIANCE_GROUP_ID}", delegate.token, {"name": "y"}).status_code == 403
def test_delegate_cannot_change_realm_or_clients(delegate):
    assert put("/admin/realms/realliance", delegate.token, {"displayName": "x"}).status_code == 403
    assert put(f"/admin/realms/realliance/clients/{OTHER_CLIENT_UUID}", delegate.token, {"enabled": False}).status_code == 403
def test_delegate_cannot_grant_self_realm_admin(delegate):
    rm = realm_management_role("realm-admin")
    assert post(f"/admin/realms/realliance/users/{delegate.id}/role-mappings/clients/{rm.client}", delegate.token, [rm.rep]).status_code == 403
def test_delegate_cannot_see_hidden_group_via_user(delegate, user_factory):
    gid = create_group("hidden-test-project"); u = user_factory(groups=["/hidden-test-project"])
    try:
        names = [g["name"] for g in get(f"/admin/realms/realliance/users/{u.id}/groups", delegate.token).json()]
        assert "hidden-test-project" not in names
        assert all(g["name"] != "hidden-test-project" for g in get("/admin/realms/realliance/groups", delegate.token).json())
    finally: delete_group(gid)
def test_operators_are_realm_admins():
    members = {u["username"] for u in realm_get(f"/groups/{group_id('/admins/realm')}/members")}
    assert members == {CHRIS_USERNAME, CONNOR_USERNAME}
def test_delegate_invite_path(delegate):
    r = invite_via_organization(delegate.token, "invitee+test@realliance.net")
    assert r.status_code in (201, 204, 403)   # records which path the runbook documents (see Step 3)
```

- [ ] **Step 2: Run to see them fail.**
- [ ] **Step 3: Implement**, then settle the invite path. Try Keycloak Organizations: create organization `realliance` (`keycloak_organization`, domain `realliance.net`, unmanaged members), and the 26.7+ delegated org-admin permission for each admin group; a delegate invites through `POST /admin/realms/realliance/organizations/{id}/members/invite-user`. If that returns 2xx **and** `test_delegate_cannot_reset_password` still passes, keep it and tighten the test to `in (201, 204)`. Otherwise remove the org-admin grant, assert `== 403`, and document in `docs/runbook.md` that new accounts are created by Chris or Connor on request. Either outcome is acceptable to the spec; leaving it untested is not.
- [ ] **Step 4: Apply, run tests** → 8 passed. (Chris and Connor create their own `realliance` accounts first — passkey enrolment in the browser — so `/admins/realm` can name them.)
- [ ] **Step 5: Commit** via PR: `"realm: FGAP v2 delegation for app owners and community group admins"`.

### Task 13: CI for the realm layer

**Files:**
- Create: `.github/workflows/realm.yml`
- Test: a no-op PR (manual, recorded in the PR)

**Interfaces:**
- Consumes: environments `plan`, `production` (Task 2).
- Environment secrets — `plan`: `KEYCLOAK_CLIENT_ID=tofu-plan`, `KEYCLOAK_CLIENT_SECRET` (tofu-plan), state creds, every `TF_VAR_*`; `production`: the same with `tofu-apply`.

- [ ] **Step 1: Populate both environments' secrets** from `sso-secrets/realm/secrets.env` (`gh secret set --env plan -f …`, then `--env production` with the apply client).
- [ ] **Step 2: Implement `realm.yml`.** Triggers: `pull_request` (paths `realm/**`) and `push` to `main` (paths `realm/**`); never `pull_request_target`. Job `plan` (env `plan`, skipped when `github.event.pull_request.head.repo.fork`): checkout, OpenTofu ≥ 1.10, `tofu init`, `tofu plan -lock=false -no-color`, post the plan as a PR comment. Job `apply` (env `production`, `if: github.ref == 'refs/heads/main'`): checkout with full history, import `keys/*.asc`, `git verify-commit HEAD` (fail the job otherwise), `tofu init && tofu apply -auto-approve`. `concurrency: realm` so applies never overlap.
- [ ] **Step 3: Verify with a no-op PR** (add a comment line in `realm/realm.tf`): plan job posts "No changes."; after merge, `apply` waits for approval; approve; it succeeds with "0 added, 0 changed, 0 destroyed". Then open a PR from a fork (Connor's account or a throwaway fork) and confirm `plan` is skipped and no secret is exposed.
- [ ] **Step 4: Commit** is the PR itself: `"ci: plan on PR, gated apply on main for realm/"`.

### Task 14: Monitoring and the go-live gate

**Files:**
- Create: `scripts/gate-check.sh`
- Modify (luma-homeops, GitOps, its own branch/commit): add `prometheus-blackbox-exporter` (Argo Application, chart pinned) + `Probe` for `https://sso.realliance.net/realms/realliance/.well-known/openid-configuration` with label `release: prometheus` + `PrometheusRule` `RealllianceSSODown` (`probe_success == 0` for 10m). Use the labels the existing ServiceMonitors/PrometheusRules use (see the `known_servicemonitor_release_label` and `known_alert_rules_not_loading` notes in luma-homeops memory).
- Test: `tests/host/test_pull.py::test_gate_check_reports_pull_age` plus a gate-check self-test

**Interfaces:**
- Produces: `scripts/gate-check.sh` prints one `PASS`/`FAIL <reason>` line per check and exits non-zero on any FAIL. Checks: (1) external monitor uptime over the window — pass the figure as `--uptime-pct` (from UptimeRobot) ≥ 99.5 and `--longest-outage-min` ≤ 60; (2) no OOM: every `failcnt` column in `/proc/user_beancounters` for `physpages`, `oomguarpages`, `privvmpages` is 0, and `systemctl show -p NRestarts keycloak postgresql` are both 0; (3) last successful `sso-pull.service` < 1 h ago; (4) newest B2 dump < 26 h old; (5) Keycloak ready.

- [ ] **Step 1: [Chris] Create the UptimeRobot monitor** (free tier, HTTPS keyword monitor on the discovery URL, keyword `"issuer"`, 5-min interval). Start of the 14-day window = the date it goes green.
- [ ] **Step 2: Write the failing test:**

```python
def test_gate_check_reports_pull_age(host):
    out = host.check_output("/usr/local/sbin/gate-check --uptime-pct 100 --longest-outage-min 0")
    assert "PASS pull" in out and "PASS backup" in out and "PASS oom" in out
```

- [ ] **Step 3: Run to see it fail**, implement `gate-check.sh` (installed to `/usr/local/sbin/gate-check` by role `backup`), apply, run → pass. Merge the luma-homeops blackbox change; confirm `probe_success{instance=~".*sso.realliance.net.*"} == 1` in Prometheus.
- [ ] **Step 4: Commit** via PR: `"ops: gate-check"`; luma-homeops commit `"monitoring: blackbox probe for sso.realliance.net"`.

### Task 15: Runbook, restore drill (Connor), go-live

**Files:**
- Create: `docs/runbook.md`
- Modify: `scripts/github-guardrails.sh` (`required_approving_review_count: 1`)

**Interfaces:**
- Consumes everything above.

- [ ] **Step 1: Write `docs/runbook.md`** with these sections, each a numbered procedure a second operator can follow cold: *Restore onto a new machine* (fresh AlmaLinux 9 → clone both repos → unlock sso-secrets → place `/etc/realliance-sso/secrets.yml` → `ansible-playbook` push run → `sso-restore <newest B2 dump>` → `dns/ tofu apply` with the new IP → verify); *Onboard an app*; *Invite a person* (the path Task 12 settled); *Recover a locked-out person* (self-service recovery codes / email reset first; total loss → a realm admin verifies them out of band — voice or in person — removes their credentials and sends a set-up email; never a group admin); *Make someone an admin* (they must already have an account; then a PR to `var.admin_group_members`); *Hidden project* (console-only group + client, never in git); *Rotate a secret* (where each lives: sso-secrets, the box file, GitHub env); *Upgrade Keycloak* (new version + checksum via PR, pull applies, build handler runs); *Yearly runway check* (CrownCloud still on OpenVZ 7 / cgroup v1? AlmaLinux 9 support date); *Revoke the CrownPanel API key* (one-off).
- [ ] **Step 2: Restore drill — run by Connor, without Chris's help.** Target: a throwaway AlmaLinux 9 VM (local libvirt/VirtualBox or a short-lived cloud VM). Use a hosts-file override for `sso.realliance.net` instead of changing public DNS. Pass criteria: Connor's existing passkey signs him in on the drill instance; elapsed time ≈ 1 h. Record the date and duration in the runbook. Fix any runbook gap he hits in the same PR.
- [ ] **Step 3: Raise review count to 1** (`scripts/github-guardrails.sh`), run it, update `test_ruleset_rules` to `== 1`, run the test → pass.
- [ ] **Step 4: Go-live acceptance (manual, recorded as a checklist in the go-live PR):**
  1. Chris and one friend sign in with passkeys; the friend was brought in by a delegated group admin (or by Connor if Task 12 settled on realm-admin invites).
  2. A user links and unlinks GitHub on their own account; signing in with an unlinked Google account does not create an account and does not attach without re-authentication.
  3. Signing into the realliance admin console via a linked Google login alone is refused without passkey/OTP.
  4. `gate-check --uptime-pct <UptimeRobot 14-day figure> --longest-outage-min <longest>` → all PASS.
  5. During a planned galaxy maintenance window (or with the galaxy probe disabled), sign-in still works from outside the house.
- [ ] **Step 5: Commit** via PR (Connor approves): `"docs: runbook; go-live"`.

---

## Notes for the executor

- **Signed commits on Chris's machine:** `gpg-agent` may refuse to sign non-interactively (it did for SSH). If `git commit -S` fails, ask Chris to unlock once (`! echo ok | gpg --clearsign >/dev/null`) and retry; never commit unsigned to `main`.
- **Pre-Task-2 commits** are local only; from Task 2 on, every change goes branch → PR → squash-merge in the UI, which `ansible-pull` accepts via the web-flow key.
- **Order matters for the box:** Task 1 before anything is left running unattended; Task 7 before Chris stops push-running playbooks (afterwards, push runs are for emergencies and must come from a commit already on `main`).
- **SMR/homelab:** nothing in this plan writes to galaxy's Ceph except the small blackbox exporter in Task 14.

---

## Amendment — 2026-11-02 (follows the spec amendment of the same date)

Tasks 1–9 are done (ledger). Task 9 absorbed: password-first + required MFA, admin-cli lockdown,
admin events, FGAP/organizations off. The remaining tasks are replaced as follows; Global
Constraints change where the spec amendment says so (no linked logins, no delegation, no human
admin roles, the box applies Keycloak config, GitHub CI is plan-only).

### Task 10 (amended): DEFERRED — linked logins
No work now. When revived: IdPs, link-only first-broker-login flow, admin step-up post-broker flow.

### Task 11 (amended): Groups, `groups` claim, app module with default deny
As originally written, minus admin groups. Public `realm/` owns: community groups `/realmic`,
`/realliance`; parent `/apps`; client scope `groups` (flat array, not a realm default); module
`app` (client, client roles, access group `/apps/<client_id>` granted `<client_id>.user`, per-client
deny flow); instances `acceptance-tests` and `acceptance-tests-noscope` (`enabled = false` except
during tests). No group memberships in `realm/` at all — membership belongs to sso-people.
Tests: original Task 11 tests; `test_console_group_untouched_by_tofu` now guards against a group
created outside realm/'s state (sso-people creates hidden groups).

### Task 12 (amended): `realliance/sso-people` — people as code + enrolment workflow
- Public `realm/`: `keycloak_workflow` "enrol" on `user_created` with step `invite-user` (verify
  email, set password; MFA enrolment is forced at first sign-in by the browser flow).
- New private repo `realliance/sso-people` (operators write; others fork): `people.yaml`
  (`username`, `email`, `first_name`, `last_name`, `active`, `groups` as paths) + `hidden_groups`;
  `people/` OpenTofu (state key `people/terraform.tfstate`): hidden groups; `keycloak_user` per
  person (`enabled = active`, `lifecycle { prevent_destroy = true }`); `keycloak_user_groups`
  per person, `exhaustive = true`.
- Tests: adding a person creates a disabled-until-enrolled-free user and Keycloak logs a
  SEND_EXECUTE_ACTIONS-style event / Resend shows the email; `active: false` → user disabled and
  sessions revoked; removing an entry → `tofu plan` fails on prevent_destroy; memberships exact;
  no user holds a realm-management role (already in test_realm).

### Task 13 (amended): the box applies Keycloak config; CI is plan-only
- Role `config_apply` (runs from `ansible-pull`): pinned OpenTofu (checksum), read-only deploy key
  for sso-people, head-commit signature check for both repos, `tofu apply` for `realm/` then
  `people/` with credentials from `/etc/realliance-sso/secrets.yml` (tofu-apply secret, tfstate
  key, Resend key), `sso/apply` commit status via a token limited to commit statuses.
  `pull_interval` becomes `5min`; the daily full run re-applies everything (self-heals drift).
- GitHub Actions in both repos: `plan` only, on PRs from branches, with the read-only `tofu-plan`
  secret; skipped for fork PRs.
- Tests: an unsigned head is not applied; a merged change appears in Keycloak within one interval;
  commit status is set; GitHub holds no `tofu-apply` secret.

### Tasks 14–15
Unchanged, plus: gate-check also requires the last `config_apply` < 1 h ago and successful;
runbook gains "add/deactivate a person (PR)", "break-glass console", "fresh install: apply
-target=keycloak_realm.realliance first", "provider 5.10.0 cannot flip realm booleans to false".
