# ci-dispatch v0.5.0 rollout (luma-homeops) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release ci-dispatch v0.5.0 and deploy it on galaxy with its control token, the stuck-queue alert routed 24/7, and the runbook updated.

**Architecture:** One release in `realliance-ops/ci-dispatch` (site config synced, signed tag, image by CI). Then signed commits in luma-homeops: a new SealedSecret `ci-dispatch-control` mounted beside the usage token, a `control` port 8082 open only to Forgejo's pods, the image pin, the new and changed alert rules with their promtool tests, the Alertmanager matcher, and `docs/ci.md`. The push is the deploy (Argo app `ci-dispatch`).

**Tech Stack:** Kustomize + Argo CD, Sealed Secrets (`./sign.sh`), PrometheusRule + promtool, Alertmanager (amtool), pytest manifest tests (pre-commit).

**Spec:** `docs/superpowers/specs/2026-11-17-ci-stale-jobs-design.md`. Plan 2 of 4; consumes plan 1 (`docs/superpowers/plans/2026-11-17-ci-dispatch-v050-stale-jobs.md`): config keys `control_listen` / `control_token_file`, metrics `ci_dispatch_oldest_waiting_seconds{class,reason}`, `ci_dispatch_azure_unavailable`, `ci_dispatch_releases_total{via}`, route `POST /v1/holds/{job_id}/{attempt}/release`.

## Global Constraints

- Chris authorized the rollout on 2026-11-18 ("lets get this all fixed and rolled out!"): tag, pin and deploy go ahead, with S1 reads before writes and Monitor on the rollout.
- Never print or paste a secret. Read tokens only into shell variables from `~/Repos/realliance-sso-secrets/forge/secrets.env`; `git diff/show/grep` with `--no-textconv` and `':!*.secret.*'`. Secret steps run in the controller, never in a subagent.
- Commits signed through pre-commit: `nix develop -c git commit -S`. Never `--no-verify`. Direct pushes to luma-homeops `main` are allowed.
- The control token is 32 random bytes hex (`openssl rand -hex 32`), stored once as `CI_DISPATCH_CONTROL_TOKEN` in `forge/secrets.env`; plan 3 seals the same value into Forgejo.
- Control port: name `control`, 8082, reachable only from Forgejo's pods (the existing Forgejo ingress rule), no Ingress.
- `CIDispatchJobWaiting`: `max by (class) (max_over_time(ci_dispatch_oldest_waiting_seconds{reason=~"launch-failing|backoff|not-ready"}[15m])) > 1800 unless on (class) sum by (class) (increase(ci_dispatch_launches_total{outcome="launched"}[30m])) > 0`, `for: 5m`, `severity: warning`, routed by name to `critical-alerts`.
- `CIDispatchLaunchFailing{reason="held"}` gets `for: 5m`.
- v0.5.0 migrates the ledger to schema v3; v0.4.0 then refuses it. Rollback below v0.5.0 means restoring a pre-upgrade ledger snapshot.

## Review Focus

1. The Forgejo pod cannot reach 8082 because the NetworkPolicy or Service misses the port: plan 3's release calls fail. Task 2 pins both in the manifest test and Task 5 checks the port from inside the Forgejo namespace.
2. `config.yaml` names the control token file but the projected volume lacks it: the pod crash-loops on `readSecret`. Task 2's test pins the projected source; Task 5 watches the rollout.
3. The new alert fires on a queue that is merely busy. The promtool test in Task 3 has a busy-but-launching quiet case.
4. Alertmanager drops the by-name route on reseal (wrong matcher syntax): `scripts/test-alertmanager-routes.sh` in Task 3 must show `CIDispatchJobWaiting -> critical-alerts`.
5. A paid launch in flight during the restart: the pod restarts once; v0.5.0 adopts or undoes it as `restart` (no try). Task 5 checks the log for `restart` undos and holds after start.

---

### Task 1: Release v0.5.0 (ci-dispatch repo)

**Files (ci-dispatch):**
- Modify: `internal/config/testdata/site-config.yaml` (add the two control keys exactly as Task 2 writes them into luma's `config.yaml`)

- [ ] **Step 1:** On the `stale-jobs` branch, after plan 1's whole-branch review, add to `site-config.yaml` after `usage_token_file`:

```yaml
control_listen: ":8082"
control_token_file: /etc/ci-dispatch/secrets/control-token
```
Run `nix shell nixpkgs#go nixpkgs#gcc -c env CGO_ENABLED=1 GOMAXPROCS=4 taskset -c 0-3 go test -race -p=4 ./internal/config/` → PASS. Commit signed: `config: the site config names the control listener and token`.

- [ ] **Step 2:** Merge the plan 1 PR into `main` on the forge (merge commit, CI green). Fetch; set `REL` to the merge commit.

- [ ] **Step 3: The three-copies check** (`docs/ci.md`, Releasing a dispatcher) with item 3 compared against Task 2's local `config.yaml` (it reaches `origin/main` with the pin): no `DIFFERS`, count 6, constants unchanged (v0.5.0 changes none).

- [ ] **Step 4:** `git -C ~/Repos/ci-dispatch-stale-jobs tag -s v0.5.0 -m "ci-dispatch v0.5.0" $REL && git push origin v0.5.0`. Watch the tag's CI run (`ci.yaml`, `image` job) to success; read the pushed image `git.realliance.net/realliance-ops/ci-dispatch:sha-<REL7>` and its digest with `crane digest` (auth from `REALLIANCE_FORGEJO_REGISTRY_TOKEN`, piped, never printed). Record `sha-<REL7>@sha256:<digest>` in the ledger.

### Task 2: The control token, port and pin

**Files (luma-homeops):**
- Create: `cluster/ci-dispatch/dispatcher/control-token.secret.yaml` (git-crypt source), `cluster/ci-dispatch/dispatcher/control-token.yaml` (SealedSecret, from `sign.sh`)
- Modify: `cluster/ci-dispatch/dispatcher/kustomization.yaml:7-16`, `statefulset.yaml` (pin comment `:65-73`, image `:74`, ports `:84-88`, projected sources `:163-176`), `service.yaml:2,12-18`, `networkpolicy.yaml:4-7,37-48`, `config.yaml:5-7,80-82`, `scripts/test_ci_dispatch_manifests.py` (`RELEASE_IMAGE :79-82`, ports `:343,:836`, secrets map `:375-382`, `test_config_values :522-533`, `DISPATCHER_INGRESS :684-693`, `test_dispatcher_policy :786-792`, `test_no_plain_secrets :926-931`)

**Interfaces:**
- Produces: Secret `ci-dispatch-control`, key `control-token`; Service port `control` 8082; plan 3 uses `http://ci-dispatch.ci-dispatch.svc.cluster.local:8082`.

- [ ] **Step 1: Write the failing test changes** in `test_ci_dispatch_manifests.py`: ports `{"http": 8080, "usage": 8081, "control": 8082}` (container and Service); secrets map gains `"ci-dispatch-control": [("control-token", "control-token")]`; `test_config_values` expects `cfg["control_listen"] == ":8082"` and `cfg["control_token_file"] == "/etc/ci-dispatch/secrets/control-token"`; the Forgejo ingress rule's ports `{("TCP", 8081), ("TCP", 8082)}`; `test_no_plain_secrets` gains `"ci-dispatch-control": {"control-token"}`; `RELEASE_IMAGE` is Task 1's pin.

- [ ] **Step 2:** Run `nix develop -c python -m pytest -q -p no:cacheprovider scripts/test_ci_dispatch_manifests.py` → FAIL on exactly those assertions.

- [ ] **Step 3 (controller only): the token.** `T=$(openssl rand -hex 32)`; append `CI_DISPATCH_CONTROL_TOKEN=$T` to `forge/secrets.env` (check with `grep -c '^CI_DISPATCH_CONTROL_TOKEN=' …` → 1); write `control-token.secret.yaml` as a `kind: Secret` `ci-dispatch-control` in `ci-dispatch` with `stringData.control-token` from `strenv(T)` via `yq`; `nix develop -c bash -O globstar ./sign.sh`; confirm `control-token.yaml` is `kind: SealedSecret` with only `encryptedData.control-token`.

- [ ] **Step 4: Manifests.** Kustomization entry `- control-token.yaml # SealedSecret ci-dispatch-control: the bearer token for the control route` (and "five SealedSecrets" in the header); the third projected source `ci-dispatch-control # SealedSecret control-token.yaml`, key/path `control-token`; container port `control` 8082; Service port `control` 8082 → `control`; port 8082 added to the Forgejo rule with its comment; `config.yaml` keys as in Task 1 Step 1 and the header's "three tokens"; the image pin and its comment block.

- [ ] **Step 5:** Run the pytest again → PASS. `nix develop -c kustomize build cluster/ci-dispatch >/dev/null` → exit 0.

- [ ] **Step 6: Commit** signed: `ci-dispatch: v0.5.0 with the control route (port 8082, token ci-dispatch-control)`.

### Task 3: Alerts

**Files:**
- Modify: `cluster/ci-dispatch/dispatcher/prometheusrule.yaml` (header `:2-10`, held rule `:36-44`, a new rule in group `ci-dispatch`), `alerts_test.yaml` (held cases `:48-62,:78-86`, header `:2-5`), `scripts/test-ci-dispatch-alerts.sh:2-3`, `scripts/test_ci_dispatch_manifests.py` (`ALERTS :103-120`, a `V050_RULES` dict merged into the loop at `:851`, the count at `:845`), `cluster/prometheus-stack/alertmanager-config.secret.yaml` (matcher, line 8 of the cleartext) → resealed `alertmanager-config.yaml`, `scripts/test-alertmanager-routes.sh:16`

- [ ] **Step 1: Write the failing tests.** `alerts_test.yaml`: the held case now expects `[]` at 5m and the alert at 10m; `CIDispatchJobWaiting` firing (series `oldest_waiting_seconds{class="small",reason="not-ready"}` rising past 1800 with `launches_total{class="small",outcome="launched"}` flat → fires 5m after 1800) and two quiet cases (rising past 1800 while `launched` increases every 10 min; `reason="no-budget"` past 1800). Manifest test: `ALERTS["CIDispatchJobWaiting"] = "warning"`, `V050_RULES = {("CIDispatchLaunchFailing", "held"): ('max(ci_dispatch_holds{reason="launch-failing"}) > 0', "5m"), ("CIDispatchJobWaiting", None): (<the Global Constraints expr>, "5m")}`. Route test: `CIDispatchJobWaiting` in the 24/7 loop.

- [ ] **Step 2:** Run `nix shell --inputs-from . nixpkgs#prometheus.cli nixpkgs#yq-go --command scripts/test-ci-dispatch-alerts.sh` and the pytest → FAIL.

- [ ] **Step 3: Rules.** Add `for: 5m` to the held rule; add `CIDispatchJobWaiting` with summary "CI jobs in {{ $labels.class }} have waited over 30 minutes with no launch", a description naming the reasons and `CIDispatchSpendHigh` for budgets, and `runbook_url` `…docs/ci.md#alerts`; header counts 12 + 9 = 21 and the 24/7 list.

- [ ] **Step 4 (controller only): Alertmanager.** Extend the matcher to `alertname=~"CIDispatch(SpendHigh|Locked|OrphanCompute|Drift|PaidNotReady|JobWaiting)|CISweeperStale"` with `yq -i` on the `.secret.yaml` (no printing), `rm cluster/prometheus-stack/alertmanager-config.yaml && nix develop -c ./sign.sh`.

- [ ] **Step 5:** Run the alert script, the pytest, and `nix develop -c scripts/test-alertmanager-routes.sh` → all `ok`, including `alertname=CIDispatchJobWaiting severity=warning -> critical-alerts`.

- [ ] **Step 6: Commit** signed: `ci-dispatch: CIDispatchJobWaiting (24/7) and a 5 min hold on LaunchFailing`.

### Task 4: Runbook

**Files:**
- Modify: `docs/ci.md` (`:6,9-11` version, `:27` Ports row, the job-record section `:340-391`, Alerts `:406-438`, hold reasons table `:391-401`, credential rotation `:501-633`, Rollback `:887-914`)

- [ ] **Step 1: Write.**
  - Version `(v0.5.0)` and the overview paragraph: outages are no try, the breaker, the 60 s async create, releases.
  - Ports row: `8082: the control route, Forgejo's pods only`.
  - The hold table: `launch-failing` row says the dispatcher retries a paid one once after an hour and a member can release it (`/retry`, the Retry button, the API); add the `unavailable` and `restart` outcomes as no try (20 free); the breaker as a `not-ready` wait with `ci_dispatch_azure_unavailable`.
  - "Releasing a held job" after Finding a job's record: the port-forward `18082:8082`, `C=$(grep '^CI_DISPATCH_CONTROL_TOKEN=' …)`, a `curl -X POST` piping the header like the `u()` helper, the three answers and the 429 cap.
  - Alerts: 21 rules, the matcher with `JobWaiting`, a `CIDispatchJobWaiting` row ("Do": read `ci_dispatch_oldest_waiting_seconds` by reason; `not-ready` with `azure_unavailable` 1 means an Azure outage the breaker is riding out; `launch-failing` means release it), and the held row's paid path.
  - Rotating: a "Control token" subsection after the usage token's, same four steps, two copies (dispatcher `control-token.secret.yaml`, Forgejo `ci-usage.secret.yaml` `CONTROL_TOKEN=` from plan 3).
  - Rollback: "Below v0.5.0": v0.4.0 refuses a v3 ledger; pause the paid classes and keep v0.5.0, or restore the pre-upgrade snapshot (Task 5 Step 1) by the ledger restore procedure, losing launches since.
- [ ] **Step 2:** `nix-shell -p vale --run 'vale --config ~/.config/vale/.vale.ini docs/ci.md'`: no new errors beyond product-name spelling.
- [ ] **Step 3: Commit** signed: `docs(ci): v0.5.0 — releases, the breaker, the control token, CIDispatchJobWaiting`.

### Task 5: Roll out

- [ ] **Step 1: Pre-checks (S1).** `kubectl -n ci-dispatch get pod ci-dispatch-0 -o wide` Ready; Argo app `ci-dispatch` Synced/Healthy; the newest `/data/snapshots/ledger-*.db` is from last night's 03:00 UTC run (`kubectl -n ci-dispatch exec ci-dispatch-0 -- ls -l /data/snapshots`), recorded in the ledger as the rollback point; no paid launch active (`ci_dispatch_jobs{state="running"}` for small/medium 0, or accept Review Focus 5).
- [ ] **Step 2: Push** Tasks 2–4 together to `origin/main`.
- [ ] **Step 3: Watch** with Monitor until Argo has synced and `ci-dispatch-0` runs the new image and is Ready (expect one restart; launches pause about a minute). On a crash loop: fix forward if the cause is clear; to go back to v0.4.0, first downgrade the ledger to v2 (`docs/ci.md` Rollback, Below v0.5.0: v0.4.0 refuses a v3 ledger), then revert, push, and delete `ci-dispatch-0` after sync.
- [ ] **Step 4: Verify.**
  - The log shows the ledger at version 3 and no `read failed` / `write failed` after start.
  - `/metrics` (port-forward 18080) has `ci_dispatch_oldest_waiting_seconds`, `ci_dispatch_azure_unavailable 0`, `ci_dispatch_releases_total`.
  - Control route via port-forward 18082: no token → 401; token → `POST /v1/holds/1/1/release` 200 `not-held`.
  - From the Forgejo namespace (`kubectl -n forgejo exec deploy/forgejo -c forgejo -- wget -qO- --header 'Authorization: Bearer x' --post-data '{}' http://ci-dispatch.ci-dispatch.svc.cluster.local:8082/v1/holds/1/1/release`) → 401 (the port answers).
  - Prometheus loaded `CIDispatchJobWaiting` (rules API) and Alertmanager's config has the matcher (`amtool config routes` against the live config, or the Alertmanager status page).
- [ ] **Step 5:** Record the result and the rollback point in the ledger.
