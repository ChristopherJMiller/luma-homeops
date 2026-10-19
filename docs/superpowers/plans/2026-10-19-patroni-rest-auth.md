# Patroni REST API auth for every `acid-*` cluster Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** not started. Revised 2026-10-19 for Chris's "option B" (patched operator only, stock Spilo).

**Goal:** Every Patroni member of the seven `acid-*` clusters rejects unauthenticated writes on :8008, while the postgres-operator, Patroni peers (failsafe) and `patronictl` keep working.

**Architecture:**
- **Credentials:** one sealed Secret `patroni-restapi` per cluster namespace. Each CR opts in by listing `PATRONI_RESTAPI_USERNAME` / `PATRONI_RESTAPI_PASSWORD` (`secretKeyRef` → `patroni-restapi`) in `spec.env`.
- **Operator patch, half 1 (pods):** for an opted-in CR, the operator emits those two env vars *before* `SPILO_CONFIGURATION` and adds `restapi.authentication: {username: "$(PATRONI_RESTAPI_USERNAME)", password: "$(PATRONI_RESTAPI_PASSWORD)"}` to the generated `SPILO_CONFIGURATION`. The kubelet expands `$(VAR)`. Stock Spilo merges it into `/run/postgres.yml`, which the Patroni daemon, `patronictl` and `inplace_upgrade.py` all read. A CR without the vars gets a byte-identical StatefulSet, so deploying the operator rolls nothing.
- **Operator patch, half 2 (client):** the operator's own Patroni client adds Basic auth to its writes. It reads the same Secret per request, with a timeout.
- **Rollout:** operator image → Secrets → operator → canary → the rest, one cluster at a time. `acid-chrismillerxyz` also gets its pg_hba hardened in its own roll, after its two clients move to TLS.

**Tech Stack:**
- postgres-operator v1.11.0 (ckotzbauer chart 1.12.2)
- Spilo stays stock: `spilo-16:3.2-p3` (Patroni 3.2.2, PG 16), plus immich's existing `spilo-vchord` pin
- Go: the newest supported release, falling back to upstream's 1.21.7 (Task 2)
- GitHub Actions → GHCR
- Sealed Secrets, Argo CD
- Rust `diesel-async` for the two chrismillerxyz clients (Task 6)

**Spec:** `docs/luma.md`, "Final review round (2026-10-19)": "**Patroni REST API (8008):** verify whether it has auth. If it's open, **fix it now** for every `acid-*` cluster, as its own small plan". Also, under "Logs and Postgres (2026-10-19)": "**Patroni REST auth:** fixed by patching the **operator only**. Spilo stays stock. `acid-chrismillerxyz`'s pg_hba hardening is folded into that plan's chrismillerxyz step". This plan also answers open question 2 of `docs/superpowers/plans/2026-10-18-luma-phase1-deploy.md`.

## Verified state (2026-10-19, read-only)

| Fact | Evidence |
|---|---|
| **No auth, allowlist or TLS on any member** | `/run/postgres.yml` → `restapi` on all 7 primaries is exactly `{listen: ':8008', connect_address: '<podIP>:8008'}`. There is no `authentication`, `allowlist*`, `certfile`, `verify_client` or `ctl`. The Patroni process has no `PATRONI_*` env. |
| **Patroni accepts every write** | In Patroni 3.2.2 every unsafe handler is `@check_access`. That covers `PATCH/PUT /config` and `POST /reload`, `/failsafe`, `/restart`, `/reinitialize`, `/failover`, `/switchover`, plus `DELETE /restart` and `/switchover`. With no allowlist and no auth key, `check_access` returns True. When auth is on, a rejected request gets a 401 with body `no auth header received` or `not authenticated`. |
| **Any pod can reach it** | `GET /patroni` and `GET /cluster` from `acid-attic-0` (ns attic) to `acid-gramps-1` (ns gramps) both return 200. There is no NetworkPolicy in any acid namespace, and flannel 0.26.7 enforces none. The LAN can't route to the pod CIDR (the request timed out). Exposure is every pod and every `hostNetwork` process. |
| **Impact** | Any pod can switch over, fail over, restart, `reinitialize` a replica (wipe and re-clone), or PATCH the DCS config. That includes `standby_cluster`, which demotes the primary to follow an arbitrary host, and whose `restore_command` is a shell command. The results: an outage, divergent data, or code execution as `postgres`. |
| **Pod env alone does nothing** | `/etc/runit/runsvdir/default/patroni/run` unsets every variable except `KUBERNETES_(SERVICE\|PORT\|ROLE)*`, `POD_IP`, `POD_NAMESPACE`, `HOSTNAME`, `PATH`, `PGHOME`, `LC_ALL` and `ENABLE_PG_MON`. Spilo's `restapi` template has no auth placeholder. A CR `env` entry can't override `SPILO_CONFIGURATION`, because `appendEnvVars` keeps the first entry with a given name. So `PATRONI_RESTAPI_*` in a CR never reaches the daemon unless the operator also writes the auth into `SPILO_CONFIGURATION` (D3). |
| **How `SPILO_CONFIGURATION` lands** | `configure_spilo.py` runs `deep_update(SPILO_CONFIGURATION, template)`. The user's dicts merge recursively and the user's values win, so `restapi.authentication` sits next to the template's `listen`/`connect_address`. The result is written to `/run/postgres.yml` (`spilo_commons.PATRONI_CONFIG_FILE`). `/home/postgres/postgres.yml` → `/run/postgres.yml`, and `~/.config/patroni/patronictl.yaml` → `/home/postgres/postgres.yml`. The runit `run` script starts `patroni /home/postgres/postgres.yml`. The daemon's PID is in `/etc/service/patroni/supervise/pid` (37 on `acid-royaltracker-0`). |
| **The operator can't authenticate** | `cluster.patroni = patroni.New(cluster.logger, nil)` builds a plain `http.Client{Timeout: 30s}` against `http://<podIP>:8008`, with no headers. That holds in v1.11.0 and in master (v2.0.3). Its writes are `POST /failover` with `{leader, member}` (the switchover in rolling updates and node migration), `POST /restart` (pending restart) and `PATCH /config` (CR `patroni:`/`parameters:` changes). All go through `httpPostOrPatch` → `httpClient.Do`. Its reads (`GET /config`, `/patroni`, `/cluster`) go through `httpClient.Get`. A failed write surfaces as `patroni returned '<body>'`, so an auth failure reads `… 'no auth header received'` or `… 'not authenticated'`, never "401". |
| **Operator sync order** | `syncStatefulSet` runs `syncPatroniConfig` (`PATCH /config` on the first pod that answers) *before* `recreatePods`. If that PATCH fails, it logs `postpone pod recreation until next sync because of errors during config sync`. Parameters not in `isBootstrapOnlyParameter`'s list go to `bootstrap.dcs`, which `compareSpiloConfiguration` ignores. So a change to such a parameter is a `PATCH /config` with **no roll**. Spilo's DCS default has `log_min_duration_statement: 500` and no `log_min_error_statement`. |
| **DCS settings** | All 7 clusters have `failsafe_mode: true`, ttl 30, loop_wait 10. With failsafe on, a leader that can't reach the K8s API POSTs `/failsafe` to every member, and keeps leading only if **all** of them answer 200 (`check_failsafe_topology` → `all(results)`). That endpoint is auth-checked. |
| **Other users of :8008** | kubelet readiness probes `GET /readiness`, a safe endpoint that auth doesn't touch. No ServiceMonitor or PodMonitor targets 8008. `patronictl` and Spilo's `inplace_upgrade.py` (`Config(PATRONI_CONFIG_FILE)`) both read `/run/postgres.yml`. Patroni's `Config` also merges `PATRONI_*` env, which exec'd processes inherit. |
| **Leader placement** | Every two-member cluster has its replica on `top` and its leader on `middle`. `acid-attic` and `acid-ha` are single-member, on `top`. A roll recreates the replica first and then switches over to it, so **each roll moves that cluster's leader to `top`**. Top's NVMe (#2661, open) stalls under write bursts. The hourly peak of the 5-minute average write latency today: top 0.041 s, middle 0.066 s, bottom 0.030 s. |
| **acid-chrismillerxyz pg_hba and clients** | It is the only CR with a `patroni.pg_hba` override: `host`/`hostnossl all all all trust`. The operator also mirrors that list into the DCS's top-level `pg_hba` key. The other six run Spilo's default, which ends `hostnossl all all all reject` / `hostssl all all all md5`. Clients: `mysite` (`chrismiller.xyz:2.5.0`) and CronJob `notion-sync` (`chatterbox:1.3.0`) log in as `postgres`, with the password from `postgres.acid-chrismillerxyz.credentials…`, over **non-SSL** (`pg_stat_ssl.ssl = f` for the mysite pods). Both use `diesel-async` 0.2's `AsyncPgConnection::establish`, which connects with `tokio_postgres::NoTls`. Logical backup uses `PGSSLMODE=require`. The exporter sidecar uses localhost with a password. |
| **Baseline** | 12 members. All 7 StatefulSets are at generation 1, except attic and ha at 2. No `zalando-postgres-operator-rolling-update-required` flags, and no scheduled restarts. The operator log has warnings only, and its image is `postgres-operator:v1.11.0@sha256:20ba5dbb…`. Pod label key is `galaxy=<cluster>`. Logical-backup pods carry it too, so select Spilo pods with `application=spilo`. `master_pod_move_timeout: 20m`, PDBs on. The operator SA can `get secrets` in the cluster namespaces. |

## Design decisions

- **D1: Basic auth, not `allowlist`.** An allowlist filters by source IP. The operator's pod IP changes on every restart, and in-cluster traffic isn't SNATed, so there's no stable address to allow. `allowlist_include_members` would block the operator.
- **D2: One Secret per cluster namespace, all named `patroni-restapi`** (keys `username`, `password`), following the `logical-backup-s3` precedent.
  - With a shared credential, anyone who can read one app namespace's Secrets, or exec into one Spilo pod, could drive every cluster.
  - Per-namespace credentials keep today's boundary, which is the superuser Secret.
- **D3: The credentials go into the config file, not just env.**
  - Runit strips env before Patroni starts. `patronictl` and `inplace_upgrade.py` read the config file. So the only path that reaches all three is `restapi.authentication` in `/run/postgres.yml`, through `SPILO_CONFIGURATION`.
  - The operator writes `$(PATRONI_RESTAPI_USERNAME)` / `$(PATRONI_RESTAPI_PASSWORD)` there, and the kubelet expands them because the two vars come earlier in the env list. The StatefulSet holds only the references, never the password.
  - **Consequence:** `/run/postgres.yml` **does** show `restapi.authentication` once a member is fixed, so checking the file is valid (Task 1, and the Luma plan).
- **D4: Patch the operator only (Chris, 2026-10-19).** One image we build instead of three, and no per-CR Spilo re-pins. Injection is opt-in per CR, and a golden test proves CRs that don't opt in are untouched.
  - The rejected alternative was `HTTP_PROXY` plus a header-injecting proxy sidecar. The chart can't add env or sidecars, and a static header means one shared credential (against D2).
  - It's two small patches, both upstreamable.
- **D5: No TLS on :8008.**
  - The operator hardcodes `http://`, so Basic credentials cross the flannel VXLAN in clear.
  - Reading that traffic needs node or L2 access, which already gives far more.
- **D6: Not NetworkPolicy.** flannel enforces none. Enforcement is Luma phase 4 (spec D25). When it lands, a :8008 policy is extra defence, not a replacement.
- **D7: Fail closed on the Secret: no `optional: true`.**
  - With `optional: true` and the Secret missing, the kubelet leaves `$(PATRONI_RESTAPI_PASSWORD)` unexpanded. Patroni would then turn auth on with that literal string, which is public in the CR, while the operator (Secret not found → no header) gets 401. That is not fail-open; it's a known password plus a locked-out operator.
  - Fail-closed only bites at pod *creation*: running pods are unaffected. That's guarded three ways: Task 3's gate (all Secrets exist before any CR references them), `Prune=false` on the SealedSecrets, and a DR note in `docs/backups.md`.
  - The operator side stays lenient: Secret not found → send no header. That is upstream behaviour, for clusters not yet migrated.
- **D8: Keep primaries off `top` while #2661 is open.** After each two-member roll, if the leader landed on `top` and top's NVMe swap isn't done, switch it back with `patronictl` from inside the new leader. That's one extra blip of a few seconds per cluster, and it also exercises authenticated `patronictl`. Once top's NVMe is swapped, leave leaders where they land.
- **D9: acid-chrismillerxyz drops its `pg_hba` override.** Spilo's default then applies: the same list as the other six, `hostssl all all all md5` for TCP. This ships in the same commit and roll as its auth change, after its two clients connect with TLS (Task 6). The stale top-level `pg_hba` key in its DCS is inert: Patroni reads `postgresql.pg_hba`, and the local config wins. Leave it.

## Global Constraints

- CLAUDE.md S1–S9 apply to every task.
- **S2 (GitOps):** every cluster change is a commit synced by Argo. Direct `kubectl` is limited to:
  - reads: `get`, plus `exec` for `patronictl list`, `GET` on :8008, `psql` reads and `/proc` reads;
  - `kubectl delete pod`, during rollback and rotation only;
  - `patronictl switchover`, run inside a Spilo pod, for D8, rollback and rotation;
  - the Task 1 `--probe` writes, after Chris's OK.
- **Hand-sent :8008 writes are limited to the `--probe` `DELETE /restart`.** It goes only to a member whose `GET /patroni` shows no `scheduled_restart`, which makes it a no-op. Never hand-send `POST`, `PATCH` or `PUT` with curl.
- **Never print a password (S9):**
  - Generate passwords straight into the `.secret.yaml`.
  - Show Secrets as keys only (`jq '.data | keys'`).
  - Authenticated curls run *inside* the Spilo container. They pass the credentials on stdin with `curl -K -`, fed by the shell builtin `printf 'user = "%s:%s"\n' "$PATRONI_RESTAPI_USERNAME" "$PATRONI_RESTAPI_PASSWORD"`, inside `sh -c '…'`. Single quotes stop the local shell expanding them, and the password never appears in `/proc/*/cmdline`.
  - Compare credentials in-pod and print only `True`/`False`.
  - Use `git diff --no-textconv` for anything that leaves the terminal.
- **Credential format:** username `patroni`. Password `openssl rand -hex 32`, hex only so it needs no quoting in JSON or YAML, distinct per cluster. Secret `patroni-restapi`, keys `username` and `password`; the operator patch hardcodes that name.
- **CR env block (verbatim, every cluster):**

  ```yaml
    env: # Patroni REST API auth — docs/postgres.md
      - name: PATRONI_RESTAPI_USERNAME
        valueFrom: {secretKeyRef: {name: patroni-restapi, key: username}}
      - name: PATRONI_RESTAPI_PASSWORD
        valueFrom: {secretKeyRef: {name: patroni-restapi, key: password}}
  ```
- **Upstream pins:**
  - operator source `v1.11.0` = commit `0f96eb20bfcc7381dacf0ec673e7d0824f442570`;
  - operator runtime base `registry.opensource.zalan.do/acid/postgres-operator:v1.11.0@sha256:20ba5dbb3f2d3566eddb4cd0616379cc73e55bacce398301e577d4f6353ad4e9` (what runs today);
  - Spilo is unchanged: the operator's `docker_image` `ghcr.io/zalando/spilo-16:3.2-p3@sha256:b59493aaba6e35c017f22954fa09548060616c8669c58ab2b368689f0ee21884`, and immich's `spilo-vchord:sha-46fc125@sha256:0f7e4b42…`. **No CR's `dockerImage` changes in this plan.**
- **Our image:** `ghcr.io/christopherjmiller/postgres-operator`, pinned as `:sha-<short>@sha256:<digest>`. Version string `v1.11.0-galaxy.1`. It must be anonymously pullable.
- **Order is load-bearing:** operator image → all 7 Secrets present → operator → canary → the rest. Rollback runs in exact reverse.
- **Never revert the operator while any cluster has auth on. Never upgrade or replace the operator image without porting both patches.** The upstream operator would regenerate every opted-in StatefulSet without `restapi`, which rolls them. Each of those rolls would then stall at a switchover it can't authenticate, leaving that cluster half auth-on.
- **One cluster in flight at a time.** Don't roll while any of these holds:
  - a node is cordoned or draining, or Talos/etcd work is under way (S5);
  - Ceph is outside its usual state;
  - the K8s API is flapping. While a roll is mixed (new replica auth-on, old leader auth-off), the leader's failsafe POSTs get 401, so an API outage in that window demotes the leader;
  - top's NVMe is degraded: the 1 h peak of its 5-min average write latency is ≥ 0.1 s while #2661's top swap is open. Query: `max_over_time((rate(node_disk_write_time_seconds_total{instance="192.168.0.240:9100",device="nvme0n1"}[5m]) / clamp_min(rate(node_disk_writes_completed_total{instance="192.168.0.240:9100",device="nvme0n1"}[5m]),1))[1h:1m])`;
  - it is 00:30–02:00 UTC, the logical-backup window (`30 00 * * *`).
- **Watch each roll** with the `safe-rollout` skill or Monitor, never a sleep loop. Detect operator auth failures with `grep -E 'not authenticated|no auth header|patroni-restapi'` on the operator log.
- **`acid-attic` and `acid-ha` are single-member.** A roll is a full restart outage, so each needs Chris's go at the time.

## Review Focus

1. **An operator write to a member that requires auth.** Every rolling switchover in this plan goes to a leader that still has auth off, so the rolls prove nothing. Task 5 Step 6 commits a reload-only parameter, so the operator sends `PATCH /config` to an auth-on member. Task 2's tests pin that `/failover`, `/restart` and `PATCH /config` all go through the one `Do`.
2. **Deploying the operator must not change any StatefulSet.** If a CR that hasn't opted in gets a different StatefulSet, all 7 clusters roll at once. Task 2's `TestGalaxyGoldenStatefulSets` compares patched output with golden files that the *unpatched* source wrote in the same build. Task 4 Step 3 checks that every StatefulSet's generation is unchanged.
3. **The Secret missing or misreferenced.** A missing non-optional Secret gives `CreateContainerConfigError`, an outage on attic or ha. `optional: true` gives a literal `$(…)` credential. Task 3 gates on all 7 Secrets. Task 1 reports `BROKEN` for a missing Secret or an unexpanded `$(`. Task 2's `TestPatroniRESTAPIAuthEnv` ignores refs to other Secrets, plain values and half pairs.
4. **A new `postgresql` CR without the env block** (`acid-luma`, the `acid-drill` restore cluster) stays open. Task 1 audits every CR it finds, not a fixed list. Task 9 fixes `acid-drill.yaml` and the Luma plan, and proposes an admission check.
5. **Credential drift: rotation, or a Secret read that hangs.** The operator reads the Secret per request (`TestAuthClientDoReadsSecretPerRequest`), bounded by `secretGetTimeout` (`TestAuthClientDoSecretReadTimesOut`). Pods keep the old value until they restart, so Task 9's runbook gives the rotation sequence.

## File map

- `scripts/check-patroni-restapi.sh`: audit, read-only by default; `--probe` adds the no-op write check. *(Task 1)*
- `images/postgres-operator/{Dockerfile, patches/0001-patroni-restapi-basic-auth.patch, patches/0002-patroni-restapi-spilo-configuration.patch, golden/galaxy_golden_test.go}`, `.github/workflows/postgres-operator-image.yml` *(Task 2)*
- `cluster/<ns>/patroni-restapi.secret.yaml` plus the generated `patroni-restapi.yaml`, and `cluster/<ns>/kustomization.yaml`, for ns ∈ {attic, chrismillerxyz, gramps, home-assistant, immich, media, royaltracker}; `docs/backups.md` *(Task 3)*
- `cluster/applications/postgres-operator.yaml` *(Task 4)*
- `cluster/royaltracker/psql.yaml` *(Task 5)*
- the repos `ChristopherJMiller/chrismiller.xyz` and `ChristopherJMiller/chatterbox`; `cluster/chrismillerxyz/{site,sync}.yaml` *(Task 6)*
- `cluster/{gramps,media,immich,chrismillerxyz}/psql.yaml` *(Task 7)*
- `cluster/{attic,home-assistant}/psql.yaml` *(Task 8)*
- `docs/postgres.md` (new), `.renovaterc.json`, `CLAUDE.md`, `docs/backups/acid-drill.yaml`, `docs/superpowers/plans/2026-10-18-luma-phase1-deploy.md` *(Task 9)*

---

### Task 1: Audit script

**Files:**
- Create: `scripts/check-patroni-restapi.sh`

**Interfaces:**
- Produces: `scripts/check-patroni-restapi.sh [--probe] [--cluster NS/NAME]`, using the `KUBECONFIG` from the environment.
  - First line: `operator image=ok|no`. It's `ok` when the running operator pod's `imageID` ends in the `@sha256:` digest pinned in `cluster/applications/postgres-operator.yaml`'s `image.tag`.
  - Then one line per member: `NS/POD STATUS env=ok|no secret=ok|no sts=ok|no file=ok|no daemon=ok|no ctl=ok|no get=ok|no`. With `--probe`, it adds ` unauth=<http> auth=<http>`.
  - `STATUS` is `AUTH-ON`, `OPEN` or `BROKEN`. Exit code 0 means `operator image=ok` and every member is `AUTH-ON`.

- [ ] **Step 1: Write the script.** It discovers clusters from `kubectl get postgresql -A`, and pods with `-l galaxy=<name>,application=spilo`, so new CRs are covered and logical-backup pods are not. Checks per member, in the `postgres` container:
  - `env`: the CR's `.spec.env` has both names, each a `secretKeyRef` to `patroni-restapi` with key `username`/`password` and no `optional: true`.
  - `secret`: `kubectl -n NS get secret patroni-restapi -o json | jq -c '.data|keys'` equals `["password","username"]`.
  - `sts`: in the StatefulSet's `postgres` container env, both names come before `SPILO_CONFIGURATION`, and its JSON `.restapi.authentication` equals `{"password":"$(PATRONI_RESTAPI_PASSWORD)","username":"$(PATRONI_RESTAPI_USERNAME)"}`.
  - `file`: in-pod `python3`, which prints only `True`/`False`, checks `/run/postgres.yml` → `restapi.authentication`:
    - `username` and `password` are non-empty;
    - neither contains `$(`;
    - they equal `$PATRONI_RESTAPI_USERNAME` and `$PATRONI_RESTAPI_PASSWORD`.
  - `daemon`: the PID from `/etc/service/patroni/supervise/pid` (fall back to `pgrep -o -f '^/usr/bin/python3 /usr/local/bin/patroni'`) passes two checks:
    - its `/proc/PID/cmdline` ends in `/home/postgres/postgres.yml`;
    - it started after the file was last written: `ps -o etimes= -p PID` ≤ `$(date +%s)` − `stat -c %Y /run/postgres.yml`. That proves the running daemon loaded the current file.
  - `ctl`: inside the pod, `python3 -c` builds `PatroniRequest(Config("/home/postgres/postgres.yml", validator=None))` and prints whether its `_pool.headers` has an `authorization` key. Compare case-insensitively, because urllib3's `make_headers` lowercases it. `True` proves that `patronictl` and `inplace_upgrade.py` will send auth.
  - `get`: unauthenticated `curl -s -o /dev/null -w '%{http_code}' http://localhost:8008/patroni` returns `200`. This is the readiness path.
  - With `--probe`, only if `GET /patroni` has no `scheduled_restart`:
    - unauthenticated `curl -X DELETE http://localhost:8008/restart` must return `401`;
    - authenticated (`curl -K -` as in Global Constraints) must return `404` ("no restarts are scheduled").
  - Status:
    - `BROKEN` if `get` is `no`; or `env` is `ok` while `secret` is `no`; or the `file` check found a `$(`. Report the `$(` case as `file=unexpanded`;
    - `AUTH-ON` if every check is `ok` (and, under `--probe`, the codes are 401 and 404);
    - `OPEN` otherwise.
- [ ] **Step 2: Run it against today's cluster (the failing test).** Run `KUBECONFIG=/tmp/galaxy-kubeconfig scripts/check-patroni-restapi.sh; echo "exit=$?"`. Don't use `--probe` here. Expected:
  - `operator image=no`;
  - 12 lines `OPEN env=no secret=no sts=no file=no daemon=ok ctl=no get=ok`;
  - `exit=1`.
- [ ] **Step 3: Run `shellcheck scripts/check-patroni-restapi.sh`.** Expected: clean.
- [ ] **Step 4: Commit.** `git add scripts/check-patroni-restapi.sh && git commit -m "scripts: audit Patroni REST API auth on every acid cluster"`.

### Task 2: Operator image that authenticates and opts clusters in

**Files:**
- Create: `images/postgres-operator/patches/0001-patroni-restapi-basic-auth.patch`, containing `pkg/util/patroni/auth_client.go`, `pkg/util/patroni/auth_client_test.go` and a one-line change to `pkg/cluster/cluster.go:150`.
- Create: `images/postgres-operator/patches/0002-patroni-restapi-spilo-configuration.patch`, containing `pkg/cluster/patroni_restapi_auth.go`, `pkg/cluster/patroni_restapi_auth_test.go` and edits to `pkg/cluster/k8sres.go`.
- Create: `images/postgres-operator/golden/galaxy_golden_test.go`. It is not a patch: the build copies it into `pkg/cluster/` before *and* after patching.
- Create: `images/postgres-operator/Dockerfile`, `.github/workflows/postgres-operator-image.yml`

**Interfaces:**
- Produces in package `patroni` (0001):
  - `const PatroniRESTAPISecretName = "patroni-restapi"`;
  - `var secretGetTimeout = 5 * time.Second`, a var so the test can shorten it;
  - `func NewAuthClient(secrets corev1.SecretsGetter, namespace string) httpclient.HTTPClient`, where `corev1` is `k8s.io/client-go/kubernetes/typed/core/v1`.
- Produces in package `cluster` (0002):
  - `const patroniRESTAPIUsernameEnv = "PATRONI_RESTAPI_USERNAME"` and `patroniRESTAPIPasswordEnv = "PATRONI_RESTAPI_PASSWORD"`;
  - `func patroniRESTAPIAuthEnv(env []v1.EnvVar) []v1.EnvVar`. It returns `[username entry, password entry]`, copied verbatim, when both names are present (exact case) and each is a `secretKeyRef` with `Name == patroni.PatroniRESTAPISecretName`. Otherwise it returns `nil`;
  - ``type spiloRestAPI struct { Authentication map[string]string `json:"authentication"` }``, and a new **last** field on `spiloConfiguration`: ``RestAPI *spiloRestAPI `json:"restapi,omitempty"` ``. Being last and `omitempty` keeps the JSON of a CR that doesn't opt in byte-identical;
  - `func generateSpiloJSONConfigurationWithRESTAPIAuth(pg *acidv1.PostgresqlParam, patroni *acidv1.Patroni, opConfig *config.Config, restAPIAuth bool, logger *logrus.Entry) (string, error)`. It holds the original body. When `restAPIAuth` is set, it fills `RestAPI` with `{"username": "$(PATRONI_RESTAPI_USERNAME)", "password": "$(PATRONI_RESTAPI_PASSWORD)"}` before the marshal. The old `generateSpiloJSONConfiguration` keeps its signature and calls it with `false`, so upstream's tests stay untouched.
- Produces the image `ghcr.io/christopherjmiller/postgres-operator:sha-<short>@sha256:<digest>`. Task 4 consumes it.

- [ ] **Step 1: Make a scratch checkout and pick Go.**
  - In the scratchpad: `git clone https://github.com/zalando/postgres-operator && git -C postgres-operator checkout 0f96eb20bfcc7381dacf0ec673e7d0824f442570`.
  - On the **unpatched** source, run upstream's test prerequisites (whatever `make test` needs, e.g. `make mocks`), then `go vet ./pkg/... && go test ./pkg/...` with the newest supported Go (≥ 1.26, `nix shell nixpkgs#go`).
  - If it passes, that Go is the build Go. If it fails for toolchain reasons, use `go_1_21` (1.21.7, upstream's) and note why in the Dockerfile header.
- [ ] **Step 2: Write the failing 0001 tests in `pkg/util/patroni/auth_client_test.go`.**
  - Setup: `httptest.NewServer` records each request's `Authorization` header and counts requests. Use `k8s.io/client-go/kubernetes/fake`. Fixture: Secret `patroni-restapi` in ns `royaltracker`, data `username=patroni`, `password=s3cret`.
  - `TestAuthClientDoAddsBasicAuth`: `Do` on a POST → header `== "Basic cGF0cm9uaTpzM2NyZXQ="`.
  - `TestAuthClientDoWithoutSecretSendsNoAuth`: empty clientset → header `== ""`, `err == nil`, 1 request.
  - `TestAuthClientDoNilSecretsGetterSendsNoAuth`: `NewAuthClient(nil, "royaltracker")` → header `== ""`, `err == nil`. Upstream's tests build clusters from `k8sutil.KubernetesClient{}`.
  - `TestAuthClientDoSecretInOtherNamespaceIgnored`: Secret in `gramps`, client for `royaltracker` → header `== ""`.
  - `TestAuthClientDoIncompleteSecretFails`: Secret with only `username` → `err != nil`, the message contains `patroni-restapi`, 0 requests.
  - `TestAuthClientDoSecretReadErrorFails`: a `PrependReactor("get","secrets",…)` returns an error → `err != nil`, 0 requests.
  - `TestAuthClientDoSecretReadTimesOut`:
    - setup: `secretGetTimeout = 50 * time.Millisecond`, and a `SecretsGetter` whose `Get(ctx, …)` blocks on `<-ctx.Done()` and returns `ctx.Err()`. Embed the fake's `SecretInterface` and override `Get`;
    - assert: `errors.Is(err, context.DeadlineExceeded)`, elapsed < 1 s, 0 requests.
  - `TestAuthClientDoReadsSecretPerRequest`: after the first `Do`, update the password to `rotated` → the second header `== "Basic cGF0cm9uaTpyb3RhdGVk"`.
  - `TestAuthClientGetNeverAuthenticates`: Secret present → `Get(url)` sends an empty header, and the clientset records no `get secrets` action.
  - `TestPatroniWritesUseDo`: `New(logger, recorder)`, where the recorder implements `HTTPClient`, records method + path, and returns a canned 200 `OK` without touching the network (`apiURL` hardcodes :8008). Use a pod with a valid `PodIP`. Expected: `Switchover` → `POST /failover`; `Restart` → `POST /restart`; `SetConfig` and `SetPostgresParameters` → `PATCH /config`. Each makes exactly one `Do` call and no `Get` call.
- [ ] **Step 3: Run `go test ./pkg/util/patroni/ -run 'AuthClient|WritesUseDo' -v`.** Expected: FAIL, `undefined: NewAuthClient`.
- [ ] **Step 4: Implement `NewAuthClient` in `pkg/util/patroni/auth_client.go`.** Wrap `&http.Client{Timeout: timeout}`, reusing the package's existing 30 s `timeout` const.
  - `Do` reads the Secret on every call, with `ctx, cancel := context.WithTimeout(req.Context(), secretGetTimeout)`:
    - nil getter or `apierrors.IsNotFound` → send the request unchanged;
    - any other error, or an empty key → return an error that names `patroni-restapi` and wraps the cause, and send nothing;
    - otherwise → `req.SetBasicAuth`.
  - `Get` passes straight through: Patroni never requires auth for GET, and the 1 s polling would hammer the API.
  - Header comment: `galaxy patch — luma-homeops docs/postgres.md`.
- [ ] **Step 5: Wire it in.** In `pkg/cluster/cluster.go:150`, change it to `cluster.patroni = patroni.New(cluster.logger, patroni.NewAuthClient(kubeClient.SecretsGetter, pgSpec.Namespace))`.
- [ ] **Step 6: Run the tests.** `go vet ./pkg/util/patroni/ ./pkg/cluster/ && go test ./pkg/...`. Expected: the 10 new tests and all upstream tests PASS. Then `git -C postgres-operator add pkg/util/patroni/auth_client.go pkg/util/patroni/auth_client_test.go pkg/cluster/cluster.go && git -C postgres-operator commit -m "patroni: basic auth for REST API writes from per-namespace Secret" && git -C postgres-operator format-patch -1 --stdout > images/postgres-operator/patches/0001-patroni-restapi-basic-auth.patch`.
- [ ] **Step 7: Write the golden test `images/postgres-operator/golden/galaxy_golden_test.go`** (package `cluster`).
  - Use only symbols that exist in unpatched v1.11.0. Name package-level helpers `galaxyLogger`/`galaxyRecorder`, because upstream's test files already define `logger` and `eventRecorder`.
  - Flag `-galaxy.update`; the directory comes from env `GALAXY_GOLDEN_DIR`.
  - `TestGalaxyGoldenStatefulSets` builds `New(Config{OpConfig: …}, k8sutil.KubernetesClient{}, acidv1.Postgresql{…fixed name, namespace, UID…}, …)` and calls `generateStatefulSet(&spec)` for six fixtures:
    - `plain`: royaltracker's CR shape, no env;
    - `sidecar-hba`: chrismillerxyz's shape, with the exporter sidecar, `pg_hba` and `parameters`;
    - `env-other`: env `[FOO=bar]`;
    - `env-username-only`: only the username `secretKeyRef`;
    - `env-plain-values`: both names as plain `value:`;
    - `env-other-secret`: both names as `secretKeyRef`s to Secret `foo`.
  - OpConfig: what upstream's `generateStatefulSet` tests use, plus `ClusterNameLabel: "galaxy"`, `PodRoleLabel: "spilo-role"`, `EnablePgVersionEnvVar: true`, `EnableReadinessProbe: true`, and `DockerImage` = the stock Spilo pin.
  - It writes `json.Marshal(sts)` to `<dir>/<fixture>.json` under `-galaxy.update`. Otherwise it asserts `bytes.Equal` with the file, and on mismatch prints the first differing byte offset and a 200-byte window.
  - Check it on **pristine** source: a second worktree of the scratch clone at the pinned commit (`git -C postgres-operator worktree add ../pgo-base 0f96eb20bfcc7381dacf0ec673e7d0824f442570`). Copy the file into `../pgo-base/pkg/cluster/`, then run `GALAXY_GOLDEN_DIR="$SCRATCH/golden-out" go test ./pkg/cluster/ -run TestGalaxyGoldenStatefulSets -args -galaxy.update` there, then the same without `-galaxy.update`. Expected: 6 files written, then PASS.
- [ ] **Step 8: Write the failing 0002 tests in `pkg/cluster/patroni_restapi_auth_test.go`.**
  - `TestPatroniRESTAPIAuthEnv` (table): both refs to `patroni-restapi` (listed password first) → `[USERNAME, PASSWORD]` in that order. Each of these → `nil`:
    - only one name;
    - plain values;
    - refs to Secret `foo`;
    - lowercase names;
    - no env.
  - `TestGenerateStatefulSetPatroniRESTAPIAuth`: the spec env is `[FOO=bar, PASSWORD ref, USERNAME ref]`. Assertions on the postgres container env:
    - index(USERNAME) < index(PASSWORD) < index(SPILO_CONFIGURATION);
    - each name appears exactly once;
    - `FOO` is still present, and the two entries' `ValueFrom` equal the CR's;
    - decoded `SPILO_CONFIGURATION` has `restapi == {"authentication": {"password": "$(PATRONI_RESTAPI_PASSWORD)", "username": "$(PATRONI_RESTAPI_USERNAME)"}}`, and nothing else under `restapi`.
  - `TestCompareSpiloConfigurationSeesRESTAPI`: `compareSpiloConfiguration(withAuth, withoutAuth) == false` and `compareSpiloConfiguration(withAuth, withAuth) == true`, so opting in or out rolls the cluster.
  Run `go test ./pkg/cluster/ -run 'PatroniRESTAPI|SeesRESTAPI' -v`. Expected: FAIL, `undefined: patroniRESTAPIAuthEnv`.
- [ ] **Step 9: Implement 0002** per the Interfaces block, in two places:
  - `generateStatefulSet` calls `generateSpiloJSONConfigurationWithRESTAPIAuth(…, patroniRESTAPIAuthEnv(spec.Env) != nil, …)`;
  - `generateSpiloPodEnvVars` does `envVars = append(envVars, patroniRESTAPIAuthEnv(spec.Env)...)` immediately before the `SPILO_CONFIGURATION` append. The later `appendEnvVars(envVars, spec.Env...)` then skips them (first wins).
  - Header comment as in 0001.
- [ ] **Step 10: Run everything, including the golden comparison against the patched code.** Run `go vet ./pkg/util/patroni/ ./pkg/cluster/ && go test ./pkg/...`, then `cp …/galaxy_golden_test.go pkg/cluster/ && GALAXY_GOLDEN_DIR="$SCRATCH/golden-out" go test ./pkg/cluster/ -run TestGalaxyGoldenStatefulSets`. Expected: all PASS, and the six fixtures are byte-identical. `rm pkg/cluster/galaxy_golden_test.go` (it ships separately, not in a patch). Then commit only `pkg/cluster/patroni_restapi_auth.go`, `patroni_restapi_auth_test.go` and `k8sres.go` (generated mocks stay out), and `format-patch -1` to `0002-patroni-restapi-spilo-configuration.patch`. `$SCRATCH` is the scratchpad directory holding the clone.
- [ ] **Step 11: Write the `Dockerfile`.**
  - Build stage: the Go chosen in Step 1 (`golang:<ver>-bookworm`), pinned by the digest from `docker buildx imagetools inspect`. In order:
    1. clone and check out the commit, then assert `git rev-parse HEAD` equals `0f96eb20…`;
    2. run the Step 1 test prerequisites, copy in the golden test, then `GALAXY_GOLDEN_DIR=/golden go test ./pkg/cluster/ -run TestGalaxyGoldenStatefulSets -args -galaxy.update`. This runs against the **unpatched** source;
    3. `git apply --check patches/*.patch`, then `git apply patches/*.patch`;
    4. `go test ./pkg/util/patroni/ ./pkg/cluster/`. This includes the golden comparison, so a patch that changes an un-opted StatefulSet fails the build;
    5. `CGO_ENABLED=0 go build -o /out/postgres-operator -ldflags "-X=main.version=v1.11.0-galaxy.1" cmd/main.go`.
  - Final stage: `FROM` the pinned upstream v1.11.0 image, then `COPY --from=build /out/postgres-operator /postgres-operator`. Keep its user, entrypoint and curl.
  - Header comment:
    - why the image exists;
    - "bump the upstream ref and the runtime base together, and port both patches";
    - "never deploy an operator without them while any CR has the env block";
    - a pointer to D4.
- [ ] **Step 12: Build and check locally.**
  - `docker build -t pgo-test images/postgres-operator`. Expected: build succeeds, and the build-stage golden test and unit tests pass.
  - `docker run --rm --entrypoint /bin/sh pgo-test -c 'grep -c v1.11.0-galaxy.1 /postgres-operator'`. Expected: `≥1`.
- [ ] **Step 13: Write the workflow.** Base it on `.github/workflows/spilo-vchord.yml`, with these changes:
  - paths `images/postgres-operator/**` and the workflow file itself; context `images/postgres-operator`; `IMAGE: ghcr.io/${{ github.repository_owner }}/postgres-operator`;
  - **every `uses:` is pinned to a full commit SHA with a `# vX.Y.Z` comment**, resolved with `gh api repos/<owner>/<repo>/commits/<tag> --jq .sha`;
  - the verify step runs the `grep -c v1.11.0-galaxy.1` check on the pushed tag;
  - a last step, `docker logout ghcr.io && docker manifest inspect "$img"`, which fails unless the package is anonymously pullable.
- [ ] **Step 14: Commit, push to main, and record the digest.** `git add images/postgres-operator .github/workflows/postgres-operator-image.yml && git commit -m "images: postgres-operator v1.11.0 with Patroni REST basic auth"`, then push. Expected: the workflow is green, including the anonymous-pull step. If only that step fails, the package is private: ask Chris to set it to Public in the GHCR package settings, then re-run. Record `sha-<short>@sha256:<digest>`.

### Task 3: One `patroni-restapi` Secret per cluster namespace

**Files:**
- Create: `cluster/<ns>/patroni-restapi.secret.yaml` and the generated `cluster/<ns>/patroni-restapi.yaml`, for the 7 namespaces
- Modify: `cluster/<ns>/kustomization.yaml`. Add `- patroni-restapi.yaml` after `- logical-backup-s3.yaml`, plus this patch (none of the 7 has a `patches:` key today):

  ```yaml
  patches:
    - target: {kind: SealedSecret, name: patroni-restapi}
      patch: |- # fail-closed dependency of every Spilo pod (D7): never prune it
        apiVersion: bitnami.com/v1alpha1
        kind: SealedSecret
        metadata:
          name: patroni-restapi
          annotations:
            argocd.argoproj.io/sync-options: Prune=false
  ```

  It's a strategic merge, so it survives every reseal and merges with any annotations kubeseal adds. Editing the generated file would not survive a reseal.
- Modify: `docs/backups.md` "## Postgres" → "Gotchas"

**Interfaces:**
- Produces a Secret `patroni-restapi` in each ns, keys `username` (`patroni`) and `password` (64 hex characters, unique). Tasks 4–8 consume it.

- [ ] **Step 1: Generate the 7 files without displaying them.**
  - One loop, `umask 077`, `pw=$(openssl rand -hex 32)`, then a heredoc to `cluster/$ns/patroni-restapi.secret.yaml`.
  - Each file holds one `kind: Secret` with `metadata: {name: patroni-restapi, namespace: $ns}` and `stringData: {username: patroni, password: $pw}`, matching the `logical-backup-s3.secret.yaml` style.
  - A comment line points to `docs/postgres.md`.
  - Never `cat` the files.
- [ ] **Step 2: Check encryption before sealing.** `git check-attr filter -- cluster/*/patroni-restapi.secret.yaml`. Expected: 7 × `filter: git-crypt`.
- [ ] **Step 3: Seal.** `nix develop --command ./sign.sh`. Expected: 7 paths printed, no `FAIL` or `SKIP`, and 7 `patroni-restapi.yaml` files with `kind: SealedSecret`.
- [ ] **Step 4: Add the kustomization entries and patch, then check.**
  - `for ns in …; do kubectl kustomize cluster/$ns | grep -A6 'kind: SealedSecret' | grep -c 'Prune=false'; done`. Expected: 1 for each ns.
  - `git diff --no-textconv --stat` lists exactly 7×2 new files and 7 kustomizations.
  - Pre-commit passes.
- [ ] **Step 5: Add the DR note to `docs/backups.md`.** Under the "## Postgres" gotchas, add three points:
  - Every `acid-*` CR references Secret `patroni-restapi` (non-optional, D7), so a Spilo pod **cannot start** without it.
  - It's a SealedSecret marked `Prune=false`. In a cold start, the sealed-secrets controller (and its key) must be back before any Postgres pod can be created.
  - Never re-create the Secret by hand with a different password while pods run. Use the rotation in `docs/postgres.md`.
- [ ] **Step 6: Commit, push, and wait for Argo.** `git commit -m "postgres: per-cluster patroni-restapi Secrets (unreferenced, Prune=false)"`. Watch the 7 apps to Synced/Healthy.
- [ ] **Step 7: Verify (the gate for Tasks 4–8).**
  - Run `scripts/check-patroni-restapi.sh`. Expected: 12 × `OPEN … secret=ok`.
  - `kubectl -n <ns> get sealedsecret patroni-restapi -o jsonpath='{.metadata.annotations}'` shows `Prune=false`.
  - No Spilo pod restarted: ages from `kubectl get pods -A -l application=spilo` are unchanged.

### Task 4: Deploy the patched operator

**Files:**
- Modify: `cluster/applications/postgres-operator.yaml`. Add `valuesObject.image: {registry: ghcr.io, repository: christopherjmiller/postgres-operator, tag: "sha-<short>@sha256:<digest>"}` from Task 2. The comment explains three things:
  - the two patches;
  - it stays at v1.11.0, in lockstep with `logical_backup_docker_image`;
  - "never revert or upgrade without porting the patches while any CR has auth on".

**Interfaces:**
- Consumes: the Task 2 image and the Task 3 Secrets.
- Produces: an operator that sends Basic auth on Patroni writes to every cluster with the Secret, and injects `restapi.authentication` for opted-in CRs. None is opted in yet.

- [ ] **Step 1: Preflight (S1).**
  - The Argo app `postgres-operator` is Synced/Healthy.
  - No pod has `zalando-postgres-operator-rolling-update-required`.
  - Record the 12 members' `.status.startTime` and `containerStatuses[0].imageID`, and the 7 StatefulSets' `.metadata.generation`.
  - Anonymous pull works: `docker logout ghcr.io; docker manifest inspect ghcr.io/christopherjmiller/postgres-operator:sha-<short>` succeeds.
  - `kubectl auth can-i get secrets -n royaltracker --as=system:serviceaccount:postgres-operator:postgres-operator` → `yes`.
  - Get Chris's go: an operator restart re-syncs all 7 clusters.
- [ ] **Step 2: Commit, push, and watch the rollout.** `git commit -m "postgres-operator: run v1.11.0-galaxy.1 (Patroni REST auth)"`. Use `safe-rollout` on `deploy/postgres-operator`. Expected: 1/1 Ready.
- [ ] **Step 3: Verify for 10 minutes** (two repair cycles) with Monitor.
  - The audit's first line reads `operator image=ok`. That's a digest match against the pin, not a prefix match.
  - `kubectl -n postgres-operator logs deploy/postgres-operator | grep 'Spilo operator v1.11.0-galaxy.1'` matches.
  - No `level=error`, and no match for `not authenticated|no auth header|patroni-restapi`.
  - Against the Step 1 record: all 12 start times and imageIDs, and all 7 StatefulSet generations, are unchanged. No rolling-update flags appear.
  - **Rollback trigger:** any StatefulSet generation bump, any Spilo pod recreated, or any error naming `patroni-restapi` → revert this commit. That is safe here, because no cluster has auth on yet.

### Task 5: Canary `acid-royaltracker`, plus proof that the operator authenticates

**Files:**
- Modify: `cluster/royaltracker/psql.yaml`. Add the Global Constraints env block (commit 1). Then add `log_min_error_statement: "error"` under `postgresql.parameters` (commit 2), with a comment: "explicit PG default; its PATCH proved operator auth — docs/postgres.md".

**Interfaces:**
- Consumes: the Task 1 script, the Task 3 Secret, the Task 4 operator.
- Produces: the verified env block, which Tasks 7–8 copy verbatim, and the D8 switch-back procedure.

- [ ] **Step 1: Preflight.**
  - `scripts/check-patroni-restapi.sh --cluster royaltracker/acid-royaltracker` → `operator image=ok`, 2 × `OPEN … secret=ok`.
  - `patronictl list` shows Leader plus Replica `streaming`, lag 0. Record the leader's node.
  - Check the constraints' "don't roll" list, including the top-NVMe query and #2661's state.
  - Get Chris's go.
- [ ] **Step 2: Commit the env block, push, and watch the roll** with Monitor on the pods (`-l galaxy=acid-royaltracker,application=spilo -L spilo-role -o wide`) and the operator log. Commit: `postgres(royaltracker): Patroni REST auth`. The expected sequence:
  1. the replica is recreated and Ready;
  2. `Switchover … Successfully switched over`, to the old, auth-free leader;
  3. the old leader is recreated and Ready.

  Abort signatures: `CreateContainerConfigError`, `CrashLoopBackOff`, `could not switch over`, a match for `not authenticated|no auth header`, top's write latency > 1 s, or more than 15 minutes.
- [ ] **Step 3: Verify.**
  - The script shows 2 × `AUTH-ON`: the file, daemon and `ctl` checks all pass.
  - `patronictl list` shows one Leader and one Replica `streaming`, lag 0.
  - The royaltracker app's pods are Ready, and its logs since the roll have no persistent DB errors.
- [ ] **Step 4: Probe (needs Chris's OK).** `scripts/check-patroni-restapi.sh --probe --cluster royaltracker/acid-royaltracker` → both members show `unauth=401 auth=404`.
- [ ] **Step 5: Leader placement (D8).** If the leader is now on `top` and #2661's top swap is open, run `kubectl -n royaltracker exec <leader> -c postgres -- patronictl switchover --leader <leader> --candidate <replica> --force`. This is authenticated from the pod's own config.
  - It may print `503, status unknown` and still complete, so re-check `patronictl list` after about 15 s. Expected: the leader is back on `middle`, and the replica is `streaming`.
  - A `401` here means the `ctl` path is broken: stop.
- [ ] **Step 6: Mandatory: an operator write to an auth-on member.** Commit 2 (`postgres(royaltracker): explicit log_min_error_statement`), then push. It is a reload-only parameter: not bootstrap-only and not in the StatefulSet diff, so the operator sends `PATCH /config` to a member and doesn't roll. It's absent from the DCS today, and `error` is PG's default, so behaviour doesn't change. Don't use `log_min_duration_statement: "-1"`: Spilo's DCS has it at 500, so `-1` would turn off slow-query logging. Within 5 minutes, check:
  - The operator log shows `making PATCH http request: http://<royaltracker pod IP>:8008/config` (debug logging is on), with no match for `not authenticated|no auth header|patroni-restapi` and no `postpone pod recreation`.
  - In-pod `curl -s localhost:8008/config` (a GET) shows `postgresql.parameters.log_min_error_statement == "error"`, and `psql -tAc 'show log_min_error_statement'` → `error`.
  - Pod start times are unchanged, and there's no new Switchover event.
  - **If the log shows `patroni returned 'no auth header received'` or `'not authenticated'`:** the operator patch is broken. Revert commit 2 so the sync stops failing: a failing config sync also blocks every later roll of this cluster. Then stop. Don't revert the operator.
- [ ] **Step 7: Bake for at least 1 hour.** The operator log stays clean and the app stays healthy. Report to Chris before Task 7.

### Task 6: chrismillerxyz's clients connect with TLS (prerequisite for its pg_hba change)

**Files:**
- Modify (repo `ChristopherJMiller/chrismiller.xyz`): `Cargo.toml`, `src/main.rs`, `src/models/mod.rs`
- Modify (repo `ChristopherJMiller/chatterbox`): `Cargo.toml`, `src/main.rs`
- Modify: `cluster/chrismillerxyz/site.yaml` and `sync.yaml`, image tags only

**Interfaces:**
- Produces: `mysite` and `notion-sync` images whose async Postgres connections use TLS with a password. Task 7's chrismillerxyz step depends on this.

- [ ] **Step 1: Record the failing state.** On the chrismillerxyz leader: `psql -tAc "select a.client_addr, s.ssl from pg_stat_activity a join pg_stat_ssl s using (pid) where a.backend_type='client backend' and a.client_addr is not null"`. Expected today: the mysite pod IPs with `f`.
- [ ] **Step 2: Add TLS to both apps.**
  - Build the `tokio_postgres` client with a TLS connector (`postgres-native-tls`). Use `danger_accept_invalid_certs(true)`: libpq `sslmode=require` semantics, because Spilo's server certificate is self-signed.
  - Wrap the client into an `AsyncPgConnection`:
    - for chrismiller.xyz's bb8 pool, through diesel-async's custom-setup hook (`ManagerConfig::custom_setup`), bumping `diesel-async` from 0.2.2 to the first release that has it;
    - for chatterbox's single connection, build the same TLS client and wrap it with diesel-async's client-to-connection constructor (`try_from`, or `try_from_client_and_connection`, depending on the version).
  - Append `sslmode=require` to the URL that `get_db_url()` (chrismiller.xyz) and `main.rs` (chatterbox) build, so libpq migrations also refuse plaintext. Leave a provided `DATABASE_URL` untouched.
  - Release each app with a semver tag. Their `docker-publish.yml` builds on `*.*.*` tags.
- [ ] **Step 3: Pin the new tags** in `site.yaml` and `sync.yaml`, then commit and push (`chrismillerxyz: clients connect over TLS`). The pg_hba is still trust here, so nothing can fail on auth yet.
- [ ] **Step 4: Verify.**
  - Step 1's query shows `t` for every mysite pod IP, and the site responds.
  - After the next `notion-sync` run (`0 */6 * * *`), the job succeeded. The leader's CSV log (`log_connections` is on) shows its connection with `SSL enabled`.

### Task 7: The remaining two-member clusters, one at a time

**Files:**
- Modify: `cluster/gramps/psql.yaml`, `cluster/media/psql.yaml`, `cluster/immich/psql.yaml`, `cluster/chrismillerxyz/psql.yaml`. Add the Global Constraints env block. `dockerImage` is unchanged everywhere: immich keeps its `spilo-vchord` pin.
- For `cluster/chrismillerxyz/psql.yaml` only: in the **same commit**, delete the whole `patroni.pg_hba` block and its comment (D9).

- [ ] **Step 1: Run the Task 5 Steps 1–5 cycle for each cluster, in this order:** `acid-gramps` → `acid-media` → `acid-immich` → `acid-chrismillerxyz`. chrismillerxyz goes last because it waits on Task 6.
  - Each cluster is its own commit (`postgres(<ns>): Patroni REST auth`) and is fully verified, including the D8 switch-back, before the next.
  - Check each cluster's app afterwards: Gramps Web loads; Sonarr, Radarr and Lidarr have no DB errors; Immich loads.
  - For immich only: `psql -tAc "select extversion from pg_extension where extname='vchord'"` still returns its version.
- [ ] **Step 2: acid-chrismillerxyz extras.**
  - **Before its commit:** Task 6 is done, and Step 1 of Task 6 shows only `ssl = t` for non-localhost clients.
  - **After the roll:**
    - `/run/postgres.yml` → `postgresql.pg_hba` on both members equals acid-royaltracker's (compare in-pod, print `True`);
    - `select count(*) from pg_hba_file_rules where error is not null` → `0`.
  - **Non-trust test,** from the replica pod to the leader's pod IP over TCP:
    - `env -u PGPASSWORD PGPASSFILE=/dev/null psql -w "host=<leaderIP> user=postgres dbname=postgres sslmode=require connect_timeout=5" -c 'select 1'` → `fe_sendauth: no password supplied`;
    - the same with `sslmode=disable` → `pg_hba.conf rejects connection … no encryption`.
  - **Apps:** the site responds, the next `notion-sync` run succeeds, and the exporter reports `pg_up 1`.
  - **Rollback:** reverting the commit restores the trust lines along with removing auth.
- [ ] **Step 3: Check the four together.** The script shows 10 × `AUTH-ON`: these four clusters plus royaltracker.

### Task 8: The single-member clusters (outage), each on Chris's go

**Files:**
- Modify: `cluster/attic/psql.yaml`, `cluster/home-assistant/psql.yaml`. Add the Global Constraints env block.

- [ ] **Step 1: `acid-attic`.**
  - Chris's go → commit, push, Monitor.
  - Expected operator log: `cannot perform switch over before re-creating the pod: no replicas`. The pod is back Ready in about 1–2 minutes, because the image is unchanged and cached.
  - Verify: the script (with `--probe` if Chris approved probes) shows `AUTH-ON`, and the attic server pod is Ready with no persistent DB errors.
- [ ] **Step 2: `acid-ha`.** Same steps, at a quiet time Chris picks. Afterwards, `hactl` health is clean, and there are no recorder errors in the HA log after the pod is back.
- [ ] **Step 3: Full audit.** `scripts/check-patroni-restapi.sh; echo "exit=$?"` → `operator image=ok`, 12 × `AUTH-ON` (14 if `acid-luma` exists), `exit=0`. If `acid-luma` exists and is still `OPEN`, run it through Task 7's cycle, with its own Secret.

### Task 9: Runbook and guardrails

**Files:**
- Create: `docs/postgres.md`
- Modify: `.renovaterc.json`, `CLAUDE.md` (sharp edge #6), `docs/backups/acid-drill.yaml`, `docs/superpowers/plans/2026-10-18-luma-phase1-deploy.md` (Task 4 Step 9, the acid-luma CR, open question 2)

- [ ] **Step 1: Write the "Patroni REST API auth" section of `docs/postgres.md`.** It covers:
  - D3/D4/D7 in three lines;
  - who holds the credentials: `/run/postgres.yml`, which serves the daemon, its peers' failsafe calls, `patronictl` and `inplace_upgrade.py`; and the operator, from the Secret per request;
  - the audit command;
  - **new-cluster checklist:** Secret (with the kustomize `Prune=false` patch), env block, then the audit. A CR without the block stays open;
  - **operator rules:** never revert while any cluster has auth on; never upgrade or replace the image without porting both patches. Port means re-running Task 2 against the new upstream ref, golden test included;
  - **when operator auth fails** (the log matches `not authenticated|no auth header|patroni-restapi`, or `Switchover … FAILED`). Rolls and config syncs stall, and a node drain hangs on the master PDB for up to `master_pod_move_timeout` (20m). For each master on the draining node, move it by hand: `kubectl exec <leader> -c postgres -- patronictl switchover --leader <leader> --candidate <replica> --force`, then re-check `patronictl list` after about 15 s;
  - **rotation**, in this order:
    1. `rm cluster/<ns>/patroni-restapi.yaml`, because `sign.sh` skips a file that already has a sealed sibling;
    2. write the new password into the `.secret.yaml` without displaying it;
    3. `./sign.sh`, commit, push, and wait for Argo. The operator now has the new password and gets 401 until the pods restart;
    4. delete the replica pod. For a two-member cluster, there's now a mixed-credential window, so don't do this while the API flaps;
    5. in the *old* leader pod, which still accepts its own old password, `patronictl switchover --candidate <replica> --force`;
    6. delete the old leader pod. For a single-member cluster, step 4 is just "delete the pod";
  - **Defaulting new clusters** (not in this plan): once the audit is all `AUTH-ON`, consider making auth the default. The operator-config route doesn't work as-is, so it would take a third patch:
    - `pod_environment_secret` appends its vars *after* `spec.Env`, where the opt-in doesn't look;
    - it fails the sync when the Secret is missing.

    The recommendation is instead a ValidatingAdmissionPolicy (GA in k8s 1.33) that rejects a `postgresql` CR without the two env entries. It's fail-closed at apply time, catches `acid-luma` and drill restores, and needs no operator code;
  - the Rollback section below, copied.
- [ ] **Step 2: Update `.renovaterc.json`.**
  - Add `{matchFileNames: ["images/postgres-operator/**"], groupName: "postgres-operator image deps", automerge: false}`. Its description: "PRs stay open as a CVE signal. A golang bump may merge once the build (golden test included) is green. A postgres-operator base bump means porting both patches to the matching source ref first, never merging the base alone." Don't disable Renovate here.
  - Extend the existing argocd operators rule's description: the chart never moves the operator image (it's overridden to our build), and an operator upgrade = port the patches (`docs/postgres.md`).
  - Run `npx --yes --package renovate -- renovate-config-validator .renovaterc.json`. Expected: valid.
- [ ] **Step 3: Update `CLAUDE.md` sharp edge #6** to list `postgres-operator` among the images we build. Add one line: "Patroni REST writes need auth (since 2026-10); never revert or upgrade the operator without porting its patches: `docs/postgres.md`."
- [ ] **Step 4: Add the env block to `docs/backups/acid-drill.yaml`,** with a comment saying that every acid namespace has `patroni-restapi`. `kubectl apply --dry-run=client -f docs/backups/acid-drill.yaml` passes.
- [ ] **Step 5: Update the Luma phase-1 plan.**
  - In Task 4 Step 9, the `/run/postgres.yml` file check is valid again (D3). Expected output is `restapi_auth True` once this plan's Task 4 operator runs and `acid-luma`'s CR has the env block. Also run `scripts/check-patroni-restapi.sh --cluster luma-system/acid-luma` → `AUTH-ON`. Drop the "it would print False even after the fix" reasoning.
  - Give `acid-luma`'s CR the env block, plus `cluster/luma/patroni-restapi.secret.yaml` with its sealed sibling and the kustomize `Prune=false` patch, in the CR's own commit. This is fail-closed, so the Secret ships together with the reference.
  - Update the answered open question 2: "operator-only patch (option B), Spilo stock; `docs/superpowers/plans/2026-10-19-patroni-rest-auth.md`".
- [ ] **Step 6: Commit.** `git commit -m "docs(postgres): Patroni REST auth runbook; renovate, drill CR and luma plan follow-ups"`.

---

## Rollback

- **One cluster (Tasks 5–8):** `git revert` that cluster's commit(s). The patched operator regenerates the StatefulSet without the env or `restapi` and rolls it. The switchover goes, with auth, to the auth-on leader. For chrismillerxyz, the revert also restores its trust pg_hba.
  - **If that switchover fails** (the operator patch is broken): in the leader pod, `patronictl switchover --candidate <replica> --force`; its config holds the credentials. It may print `503, status unknown` but still complete, so re-check `patronictl list` after about 15 s. Then `kubectl delete pod` the old leader, to clear the known in-container `invalid permissions` restart.
- **Operator (Task 4):** revert *only* while the audit shows no member with `file=ok`. With auth on anywhere, fix forward: rebuild from the patches. To stop a misbehaving operator meanwhile, turn off the Argo app's auto-sync and scale `deploy/postgres-operator` to 0 (S2, incident response). Patroni keeps every cluster running without it; only rolls, config sync and drain-time master moves stop.
- **Secrets (Task 3):** harmless to leave. Remove one only after no CR references it (D7).
- **Image (Task 2):** nothing to undo while it's unreferenced.
- **Triggers:**
  - `CreateContainerConfigError` or `CrashLoopBackOff` on a Spilo pod;
  - an event `Switchover … FAILED`;
  - an operator log match for `not authenticated|no auth header|patroni-restapi`, or `postpone pod recreation`;
  - app DB errors that persist more than 5 minutes after a roll;
  - top's NVMe write latency > 1 s during a roll.

## Self-review notes

- **Spec coverage:**
  - "Verify" is the Verified-state table. "Fix it now for every `acid-*` cluster" is Tasks 5–8, covering all 7.
  - "Operator only, Spilo stock" is D4: no task touches a Spilo image or a `dockerImage`.
  - The chrismillerxyz pg_hba hardening is D9 + Task 6 + Task 7 Step 2.
  - Task 1's audit discovers clusters dynamically, which also covers `acid-luma`.
- **Placeholders:** `<short>`/`<digest>` are recorded in Task 2 and consumed in Task 4. The `golang` digest and the Action SHAs are resolved in Task 2. The diesel-async version is resolved in Task 6.
- **Consistency:**
  - The Secret name, keys, env names, the `$(…)` strings, the image name and the version string all match Global Constraints.
  - `PatroniRESTAPISecretName` (0001) is what `patroniRESTAPIAuthEnv` (0002) and every CR reference.
  - The audit's `sts` and `file` checks assert the exact JSON that Task 2 produces.
- **Proportion:** about 8k words, up from 4.8k. Two things account for the growth: the golden-test design, which is what makes "deploying the operator rolls nothing" checkable, and the chrismillerxyz TLS prerequisite that the pg_hba decision turned up. The old Spilo task is gone. No function bodies are written out, only signatures, test names and assertions. About a third is evidence and rationale. The rest is safety gates around stateful rolls (S3–S5), which is where this change can actually hurt.

## Open questions (Chris)

1. **Probes:** OK for the audit's `--probe` to send `DELETE /restart` to each member, once unauthenticated (expect 401) and once authenticated (expect 404)? It only runs when nothing is scheduled, which makes it a no-op. It's the only direct proof that writes are refused.
2. **Canary:** is `acid-royaltracker` the right canary? Its proof is now a reload-only parameter (`log_min_error_statement: "error"`, the PG default, so no behaviour change) instead of a second roll. Is the D8 switch-back blip, a few seconds per two-member cluster while #2661 is open, acceptable?
3. **chrismillerxyz TLS:** `mysite` and `notion-sync` connect without TLS today (`diesel-async` `NoTls`), so `hostssl … md5` would lock them out. The plan ships TLS in both apps first (Task 6: two app releases). The alternative is `host all all all md5` now (password, no TLS requirement), with TLS later. Which?
4. **Outage windows:** when may `acid-attic` and `acid-ha` take their roughly 1–2-minute restarts?
5. **Upstream:** file the two operator PRs (client auth; opt-in `restapi.authentication`) after this lands?
