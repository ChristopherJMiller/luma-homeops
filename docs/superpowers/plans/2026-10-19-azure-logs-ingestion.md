# Azure log export: Data Collector API → Logs Ingestion API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move fluent-bit's Azure shipping off the retired HTTP Data Collector API and onto the Logs Ingestion API, as a lean 30-day **debugging store**. That means trimmed records, then a DCE, a DCR with a transformation, a Basic-plan custom table and an Entra service principal. A verified dual-ship window comes before the old output is removed. At the end, the fluent-bit image is 5.1.3, digest-pinned and visible to Renovate.

**Architecture:**
- **Purpose (Chris, 2026-10-19): "It's there just for debugging, 30d storage is probably fine."** Azure Log Analytics is kept for occasional ad-hoc KQL. That fixes the Basic plan, 30 days of total retention, and the rule that no dashboard queries the table.
- **Lean records come first (Task 2).** The kubernetes filter stops adding pod labels, annotations and the `log_processed` copy of every JSON line. Its API buffer becomes unbounded, and fluent-bit stops shipping its own per-flush info lines. That is about 3× fewer bytes per record, for the old output and the new one alike.
- **Azure side:** terraform in the existing `azure/logging/` module, with the same state key.
  - It adds `azapi`, because azurerm 3.x has no resource for a custom table.
  - It adds `azuread`, for the app registration.
  - It uses a **DCE**, because azurerm 3.x cannot create a `kind: Direct` DCR. A DCE costs nothing, and it is the pattern fluent-bit documents.
- **Side by side, not a table migration:** a new table `GalaxyLogs_CL` gets a small, explicit schema. The classic `luma_homelab_logs_CL` stays readable until its 90-day retention ages it out.
  - Migrating a classic table is one-way. It keeps the `_s`/`_d` column sprawl. Microsoft also does not recommend running the old API against a migrated table.
- **fluent-bit gets a second output,** `azure_logs_ingestion`, next to the old `azure` one, with `Workers 1`. The plugin fetches its OAuth token synchronously, and without a worker that fetch runs on the engine's main thread.
  - Both outputs read the same filtered stream. So 0b's input exclusion and Traefik grep filter cover both by construction.
  - Verification compares the new table with fluent-bit's own per-output counters, not with the old table. The 1 GB/day cap truncates the old table.
- **Cutover:** remove the old output, then its Secret. Then disable shared-key ("local") auth on the workspace, so the retired path fails closed.
- **Image:** **2.2.3 is already live.** The CVE-2024-4323 hotfix was shipped as its own change (`7850cbe6`, 2026-10-19, digest-pinned through `image.digest`). Task 7 is only the later move to 5.1.3 on chart 0.58.3, plus a Renovate customManager.
- **Rejected:**
  - **Auxiliary plan:** it has no daily cap and no alerts, and it needs microsecond `TimeGenerated`.
  - **Analytics plan:** about 4× Basic's price, for the same ad-hoc use.
  - **Entra workload identity federation:** it is not possible here. The cluster's service-account issuer is private and its JWKS is node-specific, so Entra has no stable discovery document to validate tokens against. The plugin takes client credentials only, too.
  - **Filesystem buffering** (`storage.type filesystem` with `storage.total_limit_size` on the output). A WAN outage longer than the roughly 5-minute retry budget (`Retry_Limit 5`) drops that span from Azure. That is accepted for a debugging store:
    - the state dir is an emptyDir on the worn NVMe system disks (#2661), so buffering would add a full extra write of every chunk there, and still lose it on every roll;
    - drops are counted and alerted (`FluentBitAzureLogsDropping`);
    - router syslog keeps warnings and above.
    - Revisit with Follow-up 3 (a hostPath state dir) after the NVMe swaps.
- **The repeatable tests are scripts:**
  - `scripts/test-fluent-bit-routing.sh` runs the real fluent-bit config with every output swapped for a file. It proves what each output receives, exactly once. Its `stall` case proves a blackholed token endpoint doesn't freeze the engine.
  - `scripts/test-logging-alerts.sh` runs `promtool test rules` on the alert rules.
  - `azure/logging/smoke-ingest.sh` sends direct POSTs to the DCR, one record and one 1.5 MB size probe, and reads them back with KQL.
  - `azure/logging/verify-ingestion.sh` is the KQL + PromQL verification, plus the volume projection. Chris runs it.

**Tech Stack:**
- fluent-bit 2.2.3 (digest-pinned) on chart 0.47.10, moving to 5.1.3 on chart 0.58.3, with the `azure_logs_ingestion` output.
- Azure Monitor Logs Ingestion API (DCE + DCR; the plugin calls api-version `2021-11-01-preview`).
- Log Analytics workspace `luma-homelab-logs-workspace` (West US 3, PerGB2018) in `rg-luma-homelab-logging`.
- Terraform 1.16.1: azurerm `~> 3.0` (3.117.x), azuread `~> 3.0`, azapi `~> 2.0`.
- az CLI 2.89.1, kube-prometheus-stack 79.12.0 (Prometheus v3.8.0), Sealed Secrets (kubeseal), Argo CD.

**Spec:** this plan has no design doc of its own. It argues from the following sources.
- `docs/luma.md`, "Decisions after spec approval": the **Azure log export** bullet of "Final review round (2026-10-19)", and "Logs and Postgres (2026-10-19)".
- `docs/superpowers/specs/2026-10-18-luma-design.md` §13.3 Logs, superseded in part by the ruling in Global Constraints.
- `docs/superpowers/plans/2026-10-18-luma-0b-platform-prep.md`: Tasks 2–3 and follow-up 4. 0b owns the exclusions and the router syslog output.
- The planning brief of 2026-10-19, and the Azure review of 2026-10-19 (findings C1, H2, M1–M6 and Low, mapped in Self-review).

**Status:**
- Drafted 2026-10-19 (UTC). Revised 2026-10-19 after the Azure review and Chris's decisions:
  - 2.2.3 is live;
  - Luma's logs ship, except `luma-sandbox` and `luma-browser`;
  - Azure is a debugging store, with 30 days of retention;
  - the usage check is a sanity check, not a gate.
- Not yet re-reviewed.
- Measurements are read-only, taken 03:15–03:45 UTC on 10-19. 2.2.3 was confirmed live (3/3) shortly after 04:16 UTC.

## Global Constraints

- CLAUDE.md S1–S9 apply to every task. The S2 exceptions this plan uses are:
  - `kubectl rollout restart ds/fluent-bit`, only after a credential rotation (docs/logging.md);
  - throwaway canary pods in `default`, plus namespace **`luma-sandbox-azli-canary`**, which Task 5 creates and deletes. Chris pre-approves that one namespace deletion (S4) when he signs off the plan.
  - Never create, label or delete `luma-system`, `luma-sandbox` or `luma-browser`. Phases 1 and 4 own them.
- Spec, verbatim (luma.md): "**Azure log export:** migrate from the retired HTTP Data Collector API to the **Logs Ingestion API**, as its own plan, written now."
- Spec, verbatim (§13.3): "**fluent-bit excludes every `luma-*` namespace** from the Azure and syslog outputs (an input exclude on the namespace path)".
- **Ruling (Chris, 2026-10-19; docs/luma.md "Logs and Postgres"). It supersedes the §13.3 line above:**
  - "**Luma's logs go to Azure.**"
  - Designer's refinement, recorded: `luma-system` ships, because Core and the conductor log metadata only (§13.3). `luma-sandbox` and `luma-browser` stay excluded, because they carry raw user content from untrusted code and browsing.
  - 0b Task 2 owns the input exclusion `Exclude_Path /var/log/containers/*_luma-sandbox*_*.log,/var/log/containers/*_luma-browser*_*.log`. The prefix form also covers any future `luma-sandbox-…` namespace, and it lets the canary namespace above match without creating a phase-4 namespace.
  - 0b also owns the grep filter that drops Traefik access lines for `luma-*` routers, because those carry OAuth `code=` query strings. It stays as it is.
  - No output added here may bypass either of them.
- **The router syslog output stays, and 0b owns it** (the receiver, plus "warnings and above"). This plan never edits the `Name syslog` `[OUTPUT]`, or any filter or Lua script that 0b adds.
- **Data Collector API:** support ended **2026-09-14**. Ingestion still answered HTTP 200 at 03:2x UTC on 2026-10-19, and it may stop at any time.
- **Debugging store: no Grafana panel, dashboard variable or alert rule queries `GalaxyLogs_CL`.**
  - Basic tables bill $0.005 per GB scanned.
  - A panel refreshing every 5 minutes over 24 hours scans about 400 GB a day, which is about $2 a day.
  - Query it ad hoc: in the portal, with `az`, or with this plan's scripts.
- **Dual-ship:** the old `azure` output stays live until the Checkpoint passes. Removing it is its own task, and Chris approves it.
- **Azure changes go only through terraform in `azure/logging/`** (state key `logging.tfstate`).
  - `az` is used read-only.
  - Chris runs `az login` himself.
  - Every `terraform apply` needs Chris's go at the time.
- **Fixed names:**

  | Thing | Name |
  |---|---|
  | Table | `GalaxyLogs_CL` |
  | Stream | `Custom-GalaxyLogs_CL` |
  | DCE | `galaxy-logs-dce` |
  | DCR | `galaxy-logs-dcr` |
  | Entra app | `galaxy-fluent-bit-logs` |
  | Kubernetes Secret | `logging/azure-logs-ingestion`, keys `tenant-id`, `client-id`, `client-secret`, `dce-url`, `dcr-id` |
  | fluent-bit output | `Alias azure_logs_ingestion` (the metric label `name="azure_logs_ingestion"`) |
  | PrometheusRules | `prometheus/logging-alerts` (group `fluent-bit`); `prometheus/azure-secret-expiry` (group `azure-secrets`) |
  | Azure alert | scheduled query rule `galaxy-logs-over-quota`, action group `galaxy-logging-alerts` |
  | Canary namespace | `luma-sandbox-azli-canary` |
  | Canary markers | `azli<32 hex><role>`, with role one of `control`, `sandbox`, `burst`, `smoke`, `smokebig` |

- **Decisions (fixed, Chris 2026-10-19):**
  - table plan **Basic**;
  - **30 days total retention**: Basic's included interactive period, with no long-term tier;
  - no labels column: `Labels Off` at the source.
- **Values decided later:**
  - The daily cap **stays at 1 GB until Task 6**, unless Chris approves the temporary Checkpoint raise (open question 2).
  - The steady-state cap and budget come from the Checkpoint's projection, by the formula in "Cost and retention".
- **Pinning:**
  - The image is pinned by tag plus index digest.
  - The chart is pinned by exact version.
  - After Task 7, never a floating tag or an `x` range.
- **Secrets:**
  - Never print any of these in chat, docs, commits or PRs: the client secret, the workspace customer id, the tenant and client ids, the DCR immutable id, the DCE URL.
  - The workspace customer id is already committed in plaintext, in `cluster/grafana/azure-datasource.yaml` (`logAnalyticsDefaultWorkspace`). Seeing it there is not a leak, but never repeat it.
  - Until Task 2, the old output logs the workspace id on every flush. The new output logs `dcr_id` only at info, which `Log_Level warn` suppresses. Count successes from metrics, and grep fluent-bit logs for counts only.
  - Decode secrets only into `umask 077` scratch files, and delete those files afterwards.
  - **Seal only with `azure/logging/seal-fluent-bit-secret.sh`,** which runs kubeseal on its one file. Never run `./sign.sh` from this plan: it seals every unsealed `*.secret.yaml` in the tree, including any of Chris's in-progress ones.
- **Only one change set is in flight on `cluster/applications/fluent-bit.yaml` at a time.** 0b Tasks 2 and 3 edit the same file. Watch every rollout with the safe-rollout skill or Monitor, and never use a Bash sleep loop.
- **Toolchain:**
  - `nix develop -c …`, with `KUBECONFIG=/tmp/galaxy-kubeconfig` (refresh it as CLAUDE.md describes).
  - `yq` via `nix shell nixpkgs#yq-go`, and `promtool` via `nix shell nixpkgs#prometheus`.
  - `docker` is on rowlett, and is also in the dev shell.
- **Git:**
  - Commit straight to `main` with `nix develop -c git commit …`, and pre-commit must pass.
  - Pass no `-S`, and never `--no-gpg-sign` or `--no-verify`.
  - `git add` named paths only: the tree carries Chris's unrelated edits.
  - Push as `ChristopherJMiller` (check with `gh api user --jq .login`).
  - Use `git diff --no-textconv` for anything that leaves the terminal.
- **Execution:** subagent-driven (Chris, 2026-10-19). An adversarial review of this plan comes before execution.

## Review Focus

1. **The daily cap truncates the new table, and nothing in the cluster can see it.**
   - What happens: past the cap, Azure drops data but still answers 2xx, so fluent-bit counts it as delivered.
   - Azure itself does show it: a portal banner, an `OverQuota` row in `_LogOperation`, and eventually the budget mail. It is silent only to the cluster.
   - During dual-ship, both tables share the one 1 GB cap. The new table's `Usage` is then truncated too, and must not be used to size the cap.
   - Expected:
     - completeness is judged only in windows with no `OverQuota` event since that day's cap reset; otherwise it reports INCONCLUSIVE, never PASS;
     - the cap is sized from a projection the cap cannot truncate: average `_BilledSize` in clean windows × fluent-bit's own 24-hour record count;
     - an email arrives when the cap is hit.
   - Test: `verify-ingestion.sh` checks `cap` (which looks back to the last reset), `completeness` and `--project` (Task 5); the `galaxy-logs-over-quota` alert, proven by the daily hit during dual-ship (Task 5, Checkpoint).
2. **A hung token endpoint stalls the whole engine.**
   - The plugin fetches its token synchronously: `flb_oauth2` disables async mode on its own upstream. The plugin declares no workers, so the fetch runs on the main thread. Source: v2.2.3.
   - So a blackholed `login.microsoftonline.com` freezes every input and output for about 10 s per attempt. That is the default connect timeout, and `net.*` options never reach that upstream.
   - Expected:
     - with `Workers 1`, a parallel file output keeps its throughput while the token host is blackholed;
     - a stalled output alerts even when its input stops too. A blocked output pauses tail at `Mem_Buf_Limit`, so an input-based guard would mute itself.
   - Test: the routing test's `stall` case (Task 5), and the promtool cases for `FluentBitAzureLogsStalled`, including the flat-input case (Task 5).
3. **A single flush over the API's 1 MB per-call limit.**
   - Microsoft's limit applies to "both compressed and uncompressed data". The plugin sends one fluent-bit chunk (up to about 2 MB of msgpack) per request, does not split it, and retries any non-2xx response until `Retry_Limit`, then drops it.
   - Expected, settled before dual-ship: the smoke test posts a 1.5 MB body gzipped to about 100 KB.
     - A 204 means the limit binds on compressed bytes, and `compress true` covers every chunk.
     - A 413 means it binds on uncompressed bytes, and open question 4 goes to Chris before Task 5.
   - Then a burst of 3000 lines of about 950 B each arrives as exactly 3000 rows, with no non-2xx or `http_do=` line.
   - Test: `SMOKE BIG` (Task 3), and the burst canary (Task 5).
4. **Credentials fail quietly:**
   - an RBAC role assignment that hasn't propagated yet (403s right after the apply);
   - an expired client secret (401s from 2028-10-01), or Dex's expired Entra secret (2028-01-01);
   - a rotation that never reseals.
   - Expected:
     - the smoke POST retries until it gets a 204 before fluent-bit ever uses the credential;
     - both secrets alert 30 days before they expire;
     - sustained auth failure fires `FluentBitAzureLogsStalled` within about 45 minutes;
     - the seal script always rewrites its one sealed file.
   - Test: the Task 3 smoke test and the seal script's post-condition, the Task 4 expiry promtool cases, and the Task 5 stall cases.
5. **Routing: excluded content leaves the house, or a record ships twice.**
   - Expected:
     - no `luma-sandbox*` or `luma-browser*` line, and no Traefik line for a `luma-…` router, reaches any output;
     - `luma-system` ships, by the ruling;
     - every other container line reaches `GalaxyLogs_CL` exactly once.
   - Test: the routing-test fixtures (Task 2, extended in Task 5); the live `luma-sandbox-azli-canary` count of 0, plus `control` and `burst` exactly once (Task 5); and the 24-hour KQL `exclusion` check.

---

## Measured baseline (2026-10-19 UTC, read-only)

**fluent-bit:**
- Argo `fluent-bit` is Synced/Healthy at chart 0.47.10.
- At measurement time the image was `fluent/fluent-bit:2.2.2`. **Since 04:16 UTC on 10-19 it is `2.2.3@sha256:4d5e3b6bfd717447687eb8bb09ec57edfebd66344c12e343f97686dd4b2574e4`**: commit `7850cbe6`, the CVE hotfix shipped as its own change. All three pods re-rolled and are 1/1.
- The output section has `syslog` (`syslog.0`) and then `azure` (`azure.1`): Customer_ID/Shared_Key from Secret `logging/azure-log-analytics`, `Log_Type luma_homelab_logs`, `Match *`.
- The kubernetes filter today has:
  - `Merge_Log On` with `Merge_Log_Key log_processed`, and `K8S-Logging.Parser On`;
  - `Labels` and `Annotations` at their default (On), and `Buffer_Size` at its default (32k).
  - No workload in the repo sets a `fluentbit.io/*` annotation (grep, 2026-10-19).

**Volume (sum of the three pods over about 14.1 days):**
- `tail.0` read 14.25 GB and 55.1 M records. That is about **1.0 GB/day and 3.9 M records/day** of raw container logs.
- `azure.1` processed **56.5 GB of msgpack: about 4.0 GB/day, or about 1.0 KB per record**. Enrichment roughly quadruples the bytes: the kubernetes map with labels and annotations, plus `log_processed` duplicating every JSON line.
- **About 19% of rows are fluent-bit's own logs:** one info line per flush per output. The old output's line carries the workspace id; the new one's would carry `dcr_id`.
- `azure.1` per pod:

  | counter | per pod |
  |---|---|
  | errors | 0 |
  | retries | 102–123 |
  | retries_failed | 0–6 |
  | dropped | 0–16 |

- `syslog.0`: about 415–439k retries and 1.5–3.4 M dropped per pod. That is 0b's black hole, not this plan's.
- `systemd.1`: **0 records on every node**. Talos has no journald, so the `host.*` tag carries nothing.

**The Data Collector API still answers:** in the last 30 minutes, every `azure.1` flush logged `HTTP status=200` (about 3.8k flushes per pod).

**Warnings:**
- `kubectl logs` holds only about the **last 5 hours** per pod, not 14 days. The kubelet rotates container logs at 10 MiB, and the per-flush info lines fill that quickly.
- In that window, the only warning class apart from syslog was 406 lines of `[http_client] cannot increase buffer: current=32000 requested=64768`, on `bottom`'s pod. That is **about 80 an hour**.
- It comes from the kubernetes filter's 32k `Buffer_Size`. Records from pods whose API object is bigger ship with no `kubernetes` map at all.
- The pods restarted at 04:16 UTC for 2.2.3, so Task 1 re-measures.

**Record shape:**
- Before the kubernetes filter, fluent-bit run locally on a CRI fixture gives the keys `time`, `stream`, `_p`, `log`, `hostname`, `cluster`. 0b Task 3's Lua adds `syslog_severity`, plus `syslog_appname` and `syslog_procid` on `kube.*` records.
- In the cluster, the kubernetes filter adds `kubernetes`. Today JSON lines also get `log_processed`.
- After Task 2, `kubernetes` holds only `pod_name`, `namespace_name`, `pod_id`, `host`, `container_name`, `docker_id`, `container_hash` and `container_image`.
- **`hostname` is the fluent-bit pod's name, not the node's.** The node is `kubernetes.host`.

**Pod metadata:** 217 pods, with an average labels JSON of 240 B and annotations of 56 B.

**Plugin facts (source read at v2.2.3 and v5.1.3):**
- The URL is `{dce_url}/dataCollectionRules/{dcr_id}/streams/Custom-{table_name}?api-version=2021-11-01-preview`.
- Auth is OAuth2 client credentials only: `tenant_id`, `client_id`, `client_secret`, plus `auth_url` in 5.x. There is no managed identity and no workload identity.
- **The token fetch is synchronous:**
  - `flb_oauth2_create` builds its own upstream, then calls `flb_stream_disable_async_mode` on it.
  - `flb_output_upstream_set`, which applies the output's `net.*` options, is called only on the DCE upstream.
  - The plugin struct sets no `.workers`, so without `Workers` every flush, and every token fetch, runs on the engine's main thread.
- `time_generated true` writes ISO 8601 with milliseconds (`…T03:27:58.123Z`) under `time_key`.
- There is no payload splitting, and any non-2xx response is retried.
- **Log levels:**
  - success is `http_status=2xx, dcr_id=…, table=…` at **info**;
  - non-2xx is `http_status=<code>:` plus the response body, `http_do=<n>`, or `retrying payload bytes=…`, all at **warn**.
  - So `Log_Level warn` on the output silences successes (and the id) and keeps every failure.
  - The `[oauth2] …` lines are global, not per output. They log at the service level, about once per token refresh, and hold no secret.
- `log_level`, `workers`, `retry_limit` and `storage.total_limit_size` are generic output properties (`src/flb_output.c`).
- The options are identical in 2.2.x and 5.1.3, so **no upgrade is needed for the migration itself.**

**Why move to 5.1.3 anyway (Task 7):**
- CVE-2024-4323 is fixed by 2.2.3, which is live. But 2.2.3 is the last 2.2 release (2024-05), and no later fix reaches that line.
- The image sat at 2.2.2 because Renovate's argocd manager sees only the chart, never `valuesObject.image`.
- Grouped Renovate PR #2662 (held for 0a) moves the chart `0.47.x → 0.58.x` and leaves the image alone.
- fluent-bit 5.1.3 was released 2026-10-01. It is chart 0.58.3's appVersion, and its docker.io index digest is `sha256:c5542543523c9678398dd78d927c05e8425ec15b038f226b67b3b019b1a70845`.
- The upgrade notes for 3.0 through 5.1 list nothing that touches this config: tail, systemd, kubernetes, grep, modify, lua, syslog, both azure outputs, `/api/v1/*` or classic `.conf`.
  - Two notes come close: 5.0 makes `rewrite_tag` with filesystem storage pause at `storage.max_chunks_up`, and this config uses memory buffering.
  - The legacy `out_azure` still exists in 5.1.3.

**Chart 0.47.10 → 0.58.3:**
- Both versions support `image.digest` and `serviceMonitor.{selector,path}`.
- 0.58.3 changes the readiness default to `/api/v2/health`, which our override replaces.
- It adds `hotReload` securityContext (hotReload stays off) and an optional `automountServiceAccountToken`.

**Azure:**
- `azure/logging/` is azurerm `~> 3.0`, and its lock file is gitignored.
- The workspace is PerGB2018, with 90 days of retention, `daily_quota_gb = 1` and a $10 monthly budget that mails `var.alert_emails` at 80% and 100%. `terraform.tfvars` is committed and sets that list.
- There is also an unused AMA syslog/event DCR.
- `azure/entra/` holds the Dex app (azuread 3.9.0, lock committed). Its password `dex-oidc-connector` ends **2028-01-01T00:00:00Z**.
- `cluster/grafana/dashboards/azure-cost-usage.yaml` refreshes every 5 minutes.
  - It queries only `Usage`, `LAQueryLogs` and `ConsumptionResources`, none of them a Basic table.
  - It prices every DataType at a hardcoded `* 2.3` (in five panels), and the budget at a literal `'10 USD'`.
- **CoreDNS** runs Talos's default Corefile, which has `log . { class error }`. So every NXDOMAIN or SERVFAIL lookup is logged with the client pod's IP and the queried name, and ships.

**Secrets:**
- `cluster/logging/azure-credentials.secret.yaml` (keys `workspace-id`, `shared-key`) is sealed into `azure-credentials.yaml`.
- Argo app `azure-log-analytics-secret` (path `cluster/logging`, prune on) deploys it.
- `sign.sh` seals with the controller `sealed-secrets/sealed-secrets`.

**Prometheus:**
- fluent-bit is unscraped. Monitors need `release: prometheus`.
- Rules load today when they are PrometheusRule CRDs labelled `prometheus: kube-prometheus` + `role: alert-rules` in namespace `prometheus`. 0b Task 5 changes that to `release: prometheus`. A rule carrying all three labels loads in both states.

**Prices (Azure Retail Prices API, West US 3, 2026-10-19):**

| Meter | Price |
|---|---|
| Analytics ingestion | $2.30/GB after 5 GB/month free (per billing account) |
| Analytics retention beyond 31 days | $0.10/GB/month |
| Basic ingestion (includes its 30 days of interactive retention) | $0.50/GB |
| Auxiliary | $0.05/GB ingestion, plus $0.10/GB processing |
| Long-term retention | $0.02/GB/month |
| Basic/Auxiliary queries | $0.005/GB scanned |
| "Logs Processed" | $0.10/GB |

**Platform facts (Microsoft Learn, 2026-10):**
- Logs Ingestion API calls are limited to 1 MB per call, "both compressed and uncompressed data". Fields are truncated at 64 KB.
- The daily cap counts data after transformation. It applies to Analytics and Basic tables, not Auxiliary.
- A transformation that drops more than 50% of the incoming data bills the excess as processing.
- Queries that include a Basic table cover at most 30 days, and over REST they go through `/search`.
- A table's plan can switch at most once a week.

## Cost and retention (recomputed 2026-10-19)

These are estimates from the fluent-bit counters. **The Checkpoint's projection replaces them.**
- After Task 2 and `Log_Level warn`, rows drop to about 3.9 M × 0.81 ≈ **3.2 M a day**.
- The new table keeps `Log`, seven short columns and `Severity`: about **1.4 GB a day** billed. The dropped fluent-bit lines were short.
- The incoming JSON is about 2 GB a day, 3× less than before. The transform drops less than half of it, so there is no processing charge.

| Option | Billed GB/day | ≈ $/month | Verdict |
|---|---|---|---|
| **Today:** Analytics, classic table, 1 GB cap, 90 days | 1.0 (the cap) | 57.5 ingestion + 5.9 retention ≈ **63** | Truncated after roughly the first quarter of each day (est.). The budget is $10, so Chris may already be getting budget alerts. |
| **Basic, lean records, 30 days total (chosen)** | ≈ 1.4 | ingestion 21 + retention 0 + processing 0 + ad-hoc queries under 1 ≈ **21** | Complete. |
| Analytics, lean records, 30 days | ≈ 1.4 | (42.6 − 5 free) × 2.30 ≈ 86 | Rejected: the same use at 4× the price. |
| Auxiliary | — | — | Rejected: no cap, no alerts, and it needs microsecond `TimeGenerated`. |

- **Query cost:**
  - an ad-hoc query over 24 hours scans about 1.4 GB, which is $0.007;
  - one over 30 days scans about 42 GB, which is $0.21;
  - one `verify-ingestion.sh` run scans about 2 GB, which is $0.01.
  - Only a dashboard can make this expensive (Global Constraints).
- **Steady-state cap:** 1.4 × the projected GB/day, rounded up to 0.5 GB. That is about 2.0 GB.
- **Budget:** 1.3 × the projected monthly cost, rounded up to $5. That is about $30.
- Both are confirmed at the Checkpoint (open question 1).

## Sequence at a glance

| task | touches | Chris "go" | user-visible blip |
|---|---|---|---|
| 1 Baseline, usage sanity check and decisions | read-only (Chris: `az login`, KQL) | answers | — |
| 2 Lean records, and the routing harness | DaemonSet roll | sign-off | seconds of tail gap per node at the roll (state is an emptyDir) |
| 3 Azure resources, smoke test, sealed credentials | **Azure** (terraform), a new Secret in `logging` | **yes** (apply) | — |
| 4 Prometheus scrapes fluent-bit; secret-expiry alerts | ServiceMonitor, PrometheusRules | sign-off | — |
| 5 OverQuota alert, then dual-ship | **Azure** (terraform), DaemonSet roll | **yes**, plus the canary namespace delete | roll gap |
| — Checkpoint, at least 48 h | read-only (the optional cap raise is an **Azure** apply) | **go** | — |
| 6 Cutover | DaemonSet roll, Secret prune, **Azure** (terraform), dashboard | **yes** | roll gap |
| 7 fluent-bit 5.1.3, chart 0.58.3, Renovate | DaemonSet roll | sign-off | roll gap |
| 8 Exit verification and records | read-only, docs | — | — |

## Preconditions (each one is a go/no-go)

| # | Check | Needed for |
|---|---|---|
| P1 | 0b Task 2 is live. The live ConfigMap's `Exclude_Path` names `_luma-sandbox*_` and `_luma-browser*_`, not the superseded `_luma-*_`, and the Traefik grep filter is present. | Tasks 2, 5 |
| P2 | Nothing else is in flight on `fluent-bit.yaml` (0b Task 3 is either done or not started). | Tasks 2, 4–7 |
| P3 | Chris has run `az login`. His account can create role assignments on `rg-luma-homelab-logging` (Owner or User Access Administrator) and Entra app registrations (it created `galaxy-dex`). | Tasks 1, 3, 5, 6 |
| P4 | Argo `fluent-bit` is Synced/Healthy, the DaemonSet is 3/3, and its image is `2.2.3@sha256:4d5e…74e4` (or Task 7's). | each fluent-bit task |
| P5 | `terraform plan` in `azure/logging` shows "No changes" before anything is added. | Task 3 |
| P6 | The state of #2662 is known: the chart may already be `0.58.x`. | Task 7 |

---

### Task 1: Baseline, usage sanity check, and Chris's decisions (read-only)

**Files:** none.

**Interfaces:**
- Produces these, recorded in Status:
  - `azure.1` bytes per record and records per day, from the metrics endpoint. These are Task 2's before-numbers.
  - The `cannot increase buffer` rate per pod, and how far back `kubectl logs` reaches.
  - Daily billable GB per table over 30 days.
  - The times of `OverQuota` events, relative to the workspace's cap reset hour, and the reset hour itself.
  - The top 10 namespaces by billed MB.
  - Whether `luma_homelab_logs_CL` is `Classic`.
  - The 90-day query and usage summary.
  - P1, P2, P4 and P5.
  - Chris's answers to open questions 1–3.

- [ ] **Step 1: Cluster reads.**
  - Argo app, and the DaemonSet image (P4).
  - The per-pod counters, through `kubectl get --raw /api/v1/namespaces/logging/pods/<pod>:2020/proxy/api/v1/metrics/prometheus`:
    - read `fluentbit_output_proc_bytes_total` and `fluentbit_output_proc_records_total` for `name="azure.1"` twice, 10 minutes apart;
    - record Δbytes/Δrecords (expected about 1.0 KB), and Δrecords × 144 as records per day;
    - `fluentbit_output_errors_total{name="azure.1"}` shows Δ = 0.
  - Per pod: `kubectl -n logging logs <pod> --since=1h | grep -c 'cannot increase buffer'`, and the oldest timestamp held, `kubectl -n logging logs <pod> | grep -m1 -oE '^\[[0-9/]+ [0-9:]+\]'`. Both print no log content.
  - If any rate moved by more than 25% from the baseline, redo the cost table.
- [ ] **Step 2: 0b state (P1, P2).** From the live ConfigMap `logging/fluent-bit`, key `fluent-bit.conf`, record:
  - `grep -c '_luma-sandbox\*_'` ≥ 1 and `grep -c '_luma-browser\*_'` ≥ 1;
  - `grep -c '_luma-\*_'` = 0, so the superseded pattern is gone;
  - the count of `Name grep` sections, and whether a `Name lua` filter exists (0b Task 3).
- [ ] **Step 3: Terraform state (P5).**
  - After Chris's `az login`, run `cd azure/logging && nix develop -c terraform init -input=false && nix develop -c terraform plan -input=false`.
  - Expected: `No changes.`
  - Any diff means drift. Stop and report it (S1), and do not "fix" it in this plan.
- [ ] **Step 4: Chris runs the KQL.**
  - Read the workspace id without echoing it: `WS=$(nix develop -c az monitor log-analytics workspace show -g rg-luma-homelab-logging -n luma-homelab-logs-workspace --query customerId -o tsv)`.
  - Then run each query with `az monitor log-analytics query -w "$WS" --analytics-query "<q>" -o table`:

```kusto
Usage
| where TimeGenerated > ago(30d) and IsBillable
| summarize GB = round(sum(Quantity) / 1000., 2) by Day = bin(TimeGenerated, 1d), DataType
| order by Day desc
```

```kusto
_LogOperation
| where TimeGenerated > ago(30d)
| where Category =~ "Ingestion" and Detail contains "OverQuota"
| summarize first_hit = min(TimeGenerated) by Day = bin(TimeGenerated, 1d)
```

```kusto
luma_homelab_logs_CL
| where TimeGenerated > ago(1d)
| summarize MB = round(sum(_BilledSize) / 1e6, 1), rows = count()
    by ns = tostring(column_ifexists("kubernetes_namespace_name_s", ""))
| top 10 by MB
```

  - Plus `az monitor log-analytics workspace show … --query "{cap: workspaceCapping.dailyQuotaGb, reset: workspaceCapping.quotaNextResetTime, retention: retentionInDays, localAuthDisabled: features.disableLocalAuth}"`.
  - Plus `az monitor log-analytics workspace table list -g rg-luma-homelab-logging --workspace-name luma-homelab-logs-workspace --query "[?schema.tableSubType=='Classic'].name" -o tsv`, expected to include `luma_homelab_logs_CL`.
- [ ] **Step 5: Usage sanity check (Chris, 90 days; it informs sizing, and gates nothing).**

```kusto
LAQueryLogs
| where TimeGenerated > ago(90d)
| where QueryText has "luma_homelab_logs_CL"
| summarize queries = count(), users = dcount(AADObjectId), last = max(TimeGenerated) by RequestClientApp
```

```kusto
Usage
| where TimeGenerated > ago(90d) and IsBillable
| summarize GB = round(sum(Quantity) / 1000., 1), days = dcount(bin(TimeGenerated, 1d)) by DataType
| order by GB desc
```

  - If `LAQueryLogs` fails as an unknown table, query auditing is off. Record "auditing off" and move on.
  - Record the result in Status. It is not a migrate-or-drop gate: Chris keeps Azure as the debugging store.
  - **What to look for:** a `RequestClientApp` other than the portal or `az`, for example Grafana, querying the custom table on a schedule. That would be expensive on Basic: list it for Chris, and remove it before Task 6.
- [ ] **Step 6: Decide.**
  - Recompute the cost table from Steps 1, 4 and 5.
  - Ask Chris open questions 1–3 in one AskUserQuestion batch.
  - Record the answers in Status. Any value he changes overrides the defaults in Global Constraints and in Tasks 3, 5 and 6.

### Task 2: Lean records, and the routing harness

**Files:**
- Modify `cluster/applications/fluent-bit.yaml`:
  - the `Name kubernetes` `[FILTER]`;
  - the `Name azure` `[OUTPUT]`, which gets `Log_Level warn`.
- Create `scripts/test-fluent-bit-routing.sh`.

**Interfaces:**
- Consumes 0b's `Exclude_Path` and Traefik grep filter (P1). It also consumes 0b's `valuesObject.luaScripts` and `Name lua` filter, when they are live.
- Produces `nix shell nixpkgs#yq-go -c scripts/test-fluent-bit-routing.sh [--keep DIR]`, where exit 0 means every case passes.
  - `--keep DIR` leaves each output's `.jsonl` file in `DIR`; Task 3 captures its smoke record from there.
  - It reads the image tag and digest from the Application, and it needs `docker`.
  - Tasks 5–8 re-run it. Task 5 adds the `azure_logs_ingestion` assertions and the `stall` case.
- Produces **the lean record shape**, which Task 3's stream contract relies on:
  - no `log_processed`;
  - a `kubernetes` map without `labels` or `annotations`;
  - `log` untouched.

- [ ] **Step 1: Write the routing test.**
  - **Harness:**
    - Extract `config.inputs`, keeping only the `Name tail` section, with test overrides `Read_From_Head On` and `DB` pointed into a temp dir.
    - Extract every `[FILTER]` except `Name kubernetes`. That keeps 0b's grep, and its Lua filter if present.
    - Mount each `valuesObject.luaScripts` entry at `/fluent-bit/scripts/<name>`.
    - Extract the `parsers`.
    - Extract every `[OUTPUT]`, rewritten to `Name file`, with its own `Match`/`Match_Regex`, `Path /out`, `Format plain`, and `File <Alias, else Name.index>.jsonl` (for example `syslog.0.jsonl`, `azure.1.jsonl`, `azure_logs_ingestion.jsonl`). Drop every other key.
    - Set `HOSTNAME` and every `AZURE_*` variable to dummies.
    - Run `docker run --rm fluent/fluent-bit:<tag>@<digest>` for about 15 s, with the fixtures mounted at `/var/log/containers`.
    - **Config check:** also run the same image with `--dry-run` on the full, unmodified config, with all sections and dummy env. It must print `configuration test is successful`. This catches a typo in the kubernetes filter, which the harness otherwise drops.
  - **Fixtures:** CRI lines `<RFC3339> stdout F <payload>`, with alphanumeric markers.

    | file (`<id>` = 64 hex) | lines | expected in each azure-family output | expected in any output |
    |---|---|---|---|
    | `app-1_default_app-<id>.log` | `m1` plain; `{"level":"error","msg":"m2"}`; `level=warn m3`; `WARN m4` | each **exactly once** | — |
    | `x-1_notluma-x_c-<id>.log` | `m5` | once | — |
    | `core-1_luma-system_core-<id>.log` | `m6` | **once** (the ruling) | — |
    | `sb-1_luma-sandbox_shell-<id>.log` | `m7` | 0 | **0** |
    | `br-1_luma-browser_chrome-<id>.log` | `m8` | 0 | **0** |
    | `c-1_luma-sandbox-azli-canary_c-<id>.log` | `m9` | 0 | **0** |
    | `traefik-1_traefik_traefik-<id>.log` | 0b's 5 CLF lines (3 `luma-…` routers, then `media-…plex…` and `notluma-x`) | the last 2 once each | the first 3: **0** |

  - The azure-family outputs are `azure.1` until Task 6, plus `azure_logs_ingestion` from Task 5.
  - The script prints PASS or FAIL per assertion, and exits 1 on any FAIL.
- [ ] **Step 2: Run it.**
  - Expected: PASS, including the dry run.
  - This task changes no routing. The harness is the regression guard for Tasks 5–7, and the lean change is proven live in Step 5.
- [ ] **Step 3: Edit the kubernetes filter and the old output.**
  - The kubernetes `[FILTER]` keeps `Name`, `Match`, `Kube_URL`, `Kube_CA_File`, `Kube_Token_File` and `K8S-Logging.Exclude Off`. It drops `Merge_Log_Key`, and it sets:

    | key | value | comment |
    |---|---|---|
    | `Merge_Log` | `Off` | `log_processed` duplicated every JSON line. Parse at query time with `parse_json(Log)`. `log` itself is untouched, and it is what 0b's severity Lua reads. |
    | `K8S-Logging.Parser` | `Off` | It acts only through Merge_Log, and no workload sets `fluentbit.io/parser`. |
    | `Labels` | `Off` | Pod labels stop leaving the house, and the records lose about half their bytes. `Pod`, `Container` and `Image` identify every workload. |
    | `Annotations` | `Off` | The same, for annotations. |
    | `Buffer_Size` | `0` | The 32k default failed about 80 times an hour on `bottom`, and those records then shipped with no metadata. `0` grows as needed, bounded by the size of the pod's API object. |

  - The `Name azure` `[OUTPUT]` gets `Log_Level warn`. Comment: its per-flush success lines were about 19% of all rows and carry the workspace id; failures log at warn and still show.
  - The old table loses its `log_processed_*`, `kubernetes_labels_*` and `kubernetes_annotations_*` columns for new rows. Nothing in the repo queries them (grep, 2026-10-19).
- [ ] **Step 4: Run the routing test.**
  - Expected: PASS, including the dry run.
  - If 0b's `scripts/test-fluent-bit-exclusion.sh` exists, it must also PASS.
- [ ] **Step 5: Commit and watch.**

```bash
git add cluster/applications/fluent-bit.yaml scripts/test-fluent-bit-routing.sh
nix develop -c git commit -m "logging: lean fluent-bit records (no labels, annotations or log_processed; unbounded kube buffer; quiet azure output); routing harness"
git push origin main
```

  - The DaemonSet rolls to 3/3. Watch it with the safe-rollout skill.
  - **Live checks, per pod, counts only:**
    - Through the API-server pod proxy, twice, 10 minutes apart: Δbytes/Δrecords for `azure.1` is at most **0.5 ×** Task 1's figure (expected 0.35–0.45 KB), and Δ`errors` = 0.
    - `kubectl -n logging logs <pod> --since=15m | grep -c 'customer_id='` = 0.
    - One hour after the roll, `grep -c 'cannot increase buffer'` over the last hour = 0.
    - `kubectl top pods -n logging`: each pod is under 160 Mi.
  - Record the before and after figures in Status.
  - **Rollback:** revert the commit.

### Task 3: Azure resources in terraform, a direct-POST smoke test, and the sealed credentials — **Chris approval**

**Files:**
- Modify `azure/logging/main.tf`:
  - add `azuread = { source = "hashicorp/azuread", version = "~> 3.0" }` and `azapi = { source = "Azure/azapi", version = "~> 2.0" }` to `required_providers`;
  - add `provider "azuread" {}` and `provider "azapi" {}`.
- Create `azure/logging/ingestion.tf`.
- Modify `azure/logging/variables.tf` and `azure/logging/outputs.tf`.
- Modify `azure/logging/.gitignore`: drop the `.terraform.lock.hcl` line, then commit the lock file, as `azure/entra` does.
- Create the scripts `azure/logging/tf.sh`, `azure/logging/smoke-ingest.sh` and `azure/logging/seal-fluent-bit-secret.sh`, and the template `azure/logging/smoke-record.json`.
- Create the Secret files `cluster/logging/azure-logs-ingestion.secret.yaml` (git-crypt) and `cluster/logging/azure-logs-ingestion.yaml` (the SealedSecret, generated). Modify `cluster/logging/kustomization.yaml` to add the latter.

**Interfaces:**
- Consumes `scripts/test-fluent-bit-routing.sh --keep` from Task 2.
- Produces these variables:

  | variable | type | default | validation |
  |---|---|---|---|
  | `galaxy_logs_table_name` | string | `GalaxyLogs_CL` | — |
  | `galaxy_logs_table_plan` | string | `Basic` | `Basic` or `Analytics` |
  | `galaxy_logs_total_retention_days` | number | 30 | 30–730 |
  | `fluent_bit_secret_end_date` | string | `2028-10-01T00:00:00Z` | — |

- Produces these outputs:
  - `logs_ingestion_tenant_id`
  - `logs_ingestion_client_id`
  - `logs_ingestion_client_secret` (sensitive)
  - `logs_ingestion_dce_url`, the DCE's `logs_ingestion_endpoint`
  - `logs_ingestion_dcr_immutable_id`
  - `logs_ingestion_table_name`
  - `logs_ingestion_secret_end_date`
- Produces **the stream contract**, which Task 5's output must satisfy:
  - stream `Custom-GalaxyLogs_CL` with the columns `TimeGenerated:datetime`, `log:string`, `stream:string`, `kubernetes:dynamic`, `syslog_severity:string`;
  - table columns, which are the transform's output: `TimeGenerated:dateTime`, then `Computer`, `Namespace`, `Pod`, `Container`, `Image` and `Stream` as strings, then `Severity:int`, then `Log:string`.
- Produces `azure/logging/tf.sh <terraform args>`. It is entra's `tf.sh`, with the path changed: it checks `az account show`, `cd`s to the module, then `exec terraform "$@"`.
- Produces `azure/logging/smoke-ingest.sh`:
  - it prints `SMOKE POST <code>`, then `SMOKE BIG <code> LIMIT compressed|uncompressed`, then `SMOKE PASS` or `SMOKE FAIL <reason>`, and exits 0 only on PASS;
  - it runs for up to 50 minutes, so run it under Monitor.
- Produces `azure/logging/seal-fluent-bit-secret.sh`. It (re)writes both Secret files with kubeseal, and prints only the paths and key names.

- [ ] **Step 1: Capture the smoke record.**
  - Run `scripts/test-fluent-bit-routing.sh --keep <scratch>/rt`, and take the `m1` record from `azure.1.jsonl`.
  - Replace its `time` and `log` values with the placeholders `__TIME__` and `__MARKER__`, and set `hostname` to `smoke`.
  - Add a `kubernetes` map in Task 2's lean shape: `{"pod_name":"azli-smoke","namespace_name":"azli-smoke","pod_id":"00000000-0000-0000-0000-000000000000","host":"smoke","container_name":"smoke","docker_id":"<64 zeros>","container_hash":"smoke@sha256:<64 zeros>","container_image":"smoke:0"}`.
  - If 0b Task 3 is not live, add `"syslog_severity":"6"` by hand; if it is live, the key is already there.
  - Save the result as `azure/logging/smoke-record.json`. It holds fixture text and dummies, no secret.
- [ ] **Step 2: Write the smoke test.** `smoke-ingest.sh`:
  1. Read the outputs with `./tf.sh output -json` into shell variables only. It never echoes them, and `set -x` is forbidden.
  2. Get a token: `POST https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`.
     - Form fields: `client_id`, `client_secret`, `scope=https://monitor.azure.com/.default`, `grant_type=client_credentials`.
     - The secret goes in through stdin (`--data @-`), never argv.
  3. POST a one-element array to `<dce>/dataCollectionRules/<dcr>/streams/Custom-GalaxyLogs_CL?api-version=2023-01-01`. The element is `smoke-record.json`, with `__TIME__` set to now (milliseconds, `Z`), the same value added as `TimeGenerated`, and `__MARKER__` set to `azli<hex>smoke`.
     - On a 403, retry every 60 s for up to 30 minutes (RBAC propagation).
     - On a 400, print the response body (it holds no secret) and FAIL.
  4. **Size probe.** POST 1500 copies of the template, with `Content-Encoding: gzip`.
     - Each copy carries the marker `azli<hex>smokebig <i>`, and its `log` is padded to about 1000 characters of one repeated letter. That is about 1.5 MB of JSON, and about 100 KB gzipped.
     - On 204, print `SMOKE BIG 204 LIMIT compressed`. On 413, print `SMOKE BIG 413 LIMIT uncompressed`. Anything else is a FAIL.
  5. Poll every 60 s, for up to 20 minutes (a new table takes 10–15 minutes), with `GalaxyLogs_CL | where Log == "<marker>" | project Namespace, Pod, Container, Computer, Image, Stream, Severity`. This goes through `/search` when the table's plan isn't Analytics; see Task 5 Step 7.
     - PASS = exactly 1 row equal to `azli-smoke, azli-smoke, smoke, smoke, smoke:0, stdout, 6`.
     - After a 204 size probe, `GalaxyLogs_CL | where Log has "<hex>smokebig" | count` must also be 1500.
- [ ] **Step 3: Run it.** Expected: `SMOKE FAIL` naming a missing output `logs_ingestion_dce_url`.
- [ ] **Step 4: Write `ingestion.tf`.**
  - **Header comment:**
    - why a DCE: azurerm 3.x has no `kind = "Direct"`;
    - why azapi: azurerm 3.x has no custom-table resource;
    - why the app lives here and not in `azure/entra`: its only permission is this DCR, so one apply creates or destroys the whole pipeline;
    - why Basic and 30 days: a debugging store (Chris, 2026-10-19);
    - rotation is in `docs/logging.md`.
  - **`azapi_resource.galaxy_logs_table`:**
    - `type = "Microsoft.OperationalInsights/workspaces/tables@2022-10-01"`, `name = var.galaxy_logs_table_name`, `parent_id = azurerm_log_analytics_workspace.luma_homelab.id`.
    - `body.properties` holds `plan`, `totalRetentionInDays`, and `schema = { name, columns }` with the nine table columns.
    - It holds `retentionInDays = var.galaxy_logs_total_retention_days` **only when the plan is `Analytics`** (a `merge()` with a conditional). Basic's interactive 30 days are fixed.
  - **`azurerm_monitor_data_collection_endpoint.galaxy_logs`:** `galaxy-logs-dce`, in the logging resource group's location, with `public_network_access_enabled = true`.
  - **`azurerm_monitor_data_collection_rule.galaxy_logs`:**
    - name `galaxy-logs-dcr`, with `data_collection_endpoint_id` set;
    - `destinations.log_analytics { name = "galaxy-logs-workspace", workspace_resource_id = … }`;
    - a `stream_declaration` with the five stream columns;
    - one `data_flow` with `streams` and `output_stream = "Custom-${var.galaxy_logs_table_name}"`, and `transform_kql` exactly as below;
    - no `data_sources`, and `depends_on = [azapi_resource.galaxy_logs_table]`, because the table must exist first.

```kusto
source
| project
    TimeGenerated,
    Computer = tostring(kubernetes.host),
    Namespace = tostring(kubernetes.namespace_name),
    Pod = tostring(kubernetes.pod_name),
    Container = tostring(kubernetes.container_name),
    Image = tostring(kubernetes.container_image),
    Stream = tostring(stream),
    Severity = toint(syslog_severity),
    Log = tostring(log)
```

  - Comment on the transform:
    - `hostname` is the fluent-bit pod's name, so the node comes from `kubernetes.host`;
    - `Severity` is 0b Task 3's derived syslog severity (0–7), and stays null until that task is live;
    - the ids, `_p`, `time` and `cluster` are deliberately dropped. Parse JSON at query time with `parse_json(Log)`.
  - **`azuread_application.fluent_bit`:** `galaxy-fluent-bit-logs`, with `owners = [data.azuread_client_config.current.object_id]`.
  - **`azuread_service_principal.fluent_bit`:** with `client_id` and the same owners.
  - **`azuread_application_password.fluent_bit`:** `display_name = "fluent-bit-logs-ingestion"`, `end_date = var.fluent_bit_secret_end_date`.
  - **`azurerm_role_assignment.fluent_bit_dcr_publisher`:**
    - `scope` = the DCR's id, `role_definition_name = "Monitoring Metrics Publisher"`;
    - the SP's `object_id`, with `principal_type = "ServicePrincipal"`, which avoids the "PrincipalNotFound" replication race.
  - `data "azuread_client_config" "current" {}`.
- [ ] **Step 5: Validate.** Run `./azure/logging/tf.sh init -input=false`, then `validate`, then `fmt -check`. Expected: `Success!`, and no fmt diff.
- [ ] **Step 6: Plan.**
  - Run `./azure/logging/tf.sh plan -out=/tmp/claude-…/azli.tfplan`, in the scratchpad.
  - Expected: **`7 to add, 0 to change, 0 to destroy`**: the table, DCE, DCR, application, service principal, password and role assignment.
  - Anything else is drift or a mistake. Stop.
- [ ] **Step 7: Apply, with Chris's OK.** Run `./azure/logging/tf.sh apply <plan>`.
- [ ] **Step 8: Smoke.**
  - Run `smoke-ingest.sh` under Monitor (its own deadlines total 50 minutes).
  - Expected: `SMOKE POST 204`, `SMOKE BIG 204 LIMIT compressed`, then `SMOKE PASS`.
  - **On `SMOKE BIG 413 LIMIT uncompressed`:** record it, and take open question 4 to Chris before Task 5. The single-record path still has to PASS.
  - **Fallback:** if a POST returns 400 naming undeclared properties, add exactly those top-level properties to `stream_declaration` (as string or dynamic), then re-plan and re-apply. The likely ones are `time`, `_p`, `hostname`, `cluster`, and 0b's `syslog_appname`/`syslog_procid`. Record the rejected names. If `_p` is rejected as a column name, stop and report.
- [ ] **Step 9: Seal.** `seal-fluent-bit-secret.sh`:
  1. `umask 077`.
  2. Render `cluster/logging/azure-logs-ingestion.secret.yaml`. It is a single `kind: Secret`, `name: azure-logs-ingestion`, `namespace: logging`, `type: Opaque`, and its `stringData` holds the five keys from the outputs.
  3. Run `kubeseal -o yaml --controller-namespace sealed-secrets --controller-name sealed-secrets < <secret> > <tmp>`. These are the controller settings `sign.sh` uses. Never run `./sign.sh` itself.
  4. Assert that `<tmp>` is non-empty, holds one document, has `kind: SealedSecret`, and that `.spec.encryptedData | keys` is exactly the five keys.
  5. `mv <tmp> cluster/logging/azure-logs-ingestion.yaml`. It always overwrites, so a rotation can never skip the reseal.
  6. Print `sealed cluster/logging/azure-logs-ingestion.yaml keys: tenant-id client-id client-secret dce-url dcr-id`.
  - **Post-condition:** `git status --short cluster/` lists only the two new logging files plus `cluster/logging/kustomization.yaml`. That proves no other secret was sealed.
  - Also add `azure-logs-ingestion.yaml` to `cluster/logging/kustomization.yaml`.
- [ ] **Step 10: Commit and watch.**

```bash
git add azure/logging/main.tf azure/logging/ingestion.tf azure/logging/variables.tf azure/logging/outputs.tf \
  azure/logging/.gitignore azure/logging/.terraform.lock.hcl azure/logging/tf.sh azure/logging/smoke-ingest.sh \
  azure/logging/smoke-record.json azure/logging/seal-fluent-bit-secret.sh \
  cluster/logging/azure-logs-ingestion.secret.yaml cluster/logging/azure-logs-ingestion.yaml cluster/logging/kustomization.yaml
nix develop -c git commit -m "azure/logging: Logs Ingestion pipeline (DCE, DCR, Basic GalaxyLogs_CL, fluent-bit SP) and its sealed credentials"
git push origin main
```

  - Argo `azure-log-analytics-secret` goes Synced.
  - `kubectl -n logging get secret azure-logs-ingestion -o json | jq -r '.data | keys[]'` prints exactly the five key names.
  - **Rollback:** revert the commit, then `./tf.sh destroy -target=…` the seven resources, with Chris's OK. Nothing in the cluster uses them yet.

### Task 4: Prometheus scrapes fluent-bit; secret-expiry alerts

**Files:**
- Modify `cluster/applications/fluent-bit.yaml`: `valuesObject.serviceMonitor`.
- Create `cluster/prometheus-stack/rules/logging-alerts.yaml` and `cluster/prometheus-stack/rules/azure-secret-expiry.yaml`, and add both to `resources:` in `cluster/prometheus-stack/rules/kustomization.yaml`.
- Create `scripts/test-logging-alerts.sh`.

**Interfaces:**
- Produces series labelled `job="fluent-bit"`, `pod`, and `name` (the plugin instance or Alias), from `/api/v1/metrics/prometheus`.
- Produces these PrometheusRules, each labelled `prometheus: kube-prometheus`, `role: alert-rules` and `release: prometheus`:
  - `prometheus/logging-alerts`, group `fluent-bit` (interval 60s);
  - `prometheus/azure-secret-expiry`, group `azure-secrets` (interval 1h).
- Produces `scripts/test-logging-alerts.sh`.
  - It extracts each `.spec` with yq into a temp rules file, and runs `promtool test rules` against two test files that the script writes inline: `fluent-bit` (`evaluation_interval: 1m`) and `azure-secrets` (`evaluation_interval: 1d`, so a test can reach 2028 in a few thousand steps).
  - Exit 0 means every case passes. Task 5 adds its cases.

- [ ] **Step 1: Write the tests.**
  - `FluentBitNotScraped`:
    - with `up{job="fluent-bit"}` = 1 for 2 of 3 pods over 45 minutes, the alert fires at `eval_time: 35m`;
    - with 3 of 3 up, it never fires.
  - `AzureLogsIngestionSecretExpiring`: silent at `eval_time: 514248h` (31 days before 2028-10-01), firing at `514320h` (28 days before).
  - `DexEntraSecretExpiring`: silent at `507672h` (31 days before 2028-01-01), firing at `507744h` (28 days before).
  - With a 1-day step and `for: 1h`, the rule first becomes true 29 days out and is still pending there, so the firing case is the next step, 28 days out. Every eval time is a multiple of 24 h.
- [ ] **Step 2: Run it.** Expected: FAIL, because the rule files don't exist.
- [ ] **Step 3: Write the rules.**
  - `FluentBitNotScraped`: `expr: count(up{job="fluent-bit"} == 1) < 3 or absent(up{job="fluent-bit"} == 1)`, `for: 30m`, `severity: warning`. The summary points to `docs/logging.md`.
  - `AzureLogsIngestionSecretExpiring`: `expr: vector(time()) > 1853971200 - 30 * 86400`, `for: 1h`, `severity: warning`.
    - The epoch is `fluent_bit_secret_end_date` (2028-10-01T00:00:00Z) in `azure/logging/variables.tf`.
    - The description: rotate with the gap-free procedure in `docs/logging.md`.
  - `DexEntraSecretExpiring`: `expr: vector(time()) > 1830297600 - 30 * 86400`, `for: 1h`, `severity: warning`.
    - The epoch is `azuread_application_password.dex.end_date` (2028-01-01T00:00:00Z) in `azure/entra/dex.tf`.
    - The description: rotate it in `azure/entra`, then reseal Dex's secret.
  - The header comment of `azure-secret-expiry.yaml`: **change the epoch in the same commit as any rotation.**
- [ ] **Step 4: Run it.** Expected: PASS.
- [ ] **Step 5: ServiceMonitor values.**
  - `serviceMonitor: {enabled: true, selector: {release: prometheus}, path: /api/v1/metrics/prometheus, interval: 60s}`.
  - Comment:
    - without `release: prometheus`, a monitor is silently never scraped;
    - v1 is used because these metric names were verified live on 2026-10-19.
- [ ] **Step 6: Render check.**
  - Run `nix develop -c helm template fluent-bit fluent/fluent-bit --version 0.47.10 -f <(nix shell nixpkgs#yq-go -c yq '.spec.source.helm.valuesObject' cluster/applications/fluent-bit.yaml)`. Add the repo first with `helm repo add fluent https://fluent.github.io/helm-charts`.
  - The ServiceMonitor carries `release: prometheus` and the v1 path.
  - The rendered DaemonSet is byte-identical to the render from `HEAD`'s values, so there is no rollout.
- [ ] **Step 7: Commit and watch.**

```bash
git add cluster/applications/fluent-bit.yaml cluster/prometheus-stack/rules/logging-alerts.yaml \
  cluster/prometheus-stack/rules/azure-secret-expiry.yaml cluster/prometheus-stack/rules/kustomization.yaml \
  scripts/test-logging-alerts.sh
nix develop -c git commit -m "logging: Prometheus scrapes fluent-bit; alerts for an unscraped fluent-bit and expiring Azure secrets"
git push origin main
```

- [ ] **Step 8: Live check.** Run these through the Prometheus pod (`kubectl exec … wget`, as `scripts/check-dashboards.sh` does):
  - `count(up{job="fluent-bit"} == 1)` = 3;
  - `count by (name) (fluentbit_output_proc_records_total)` lists `azure.1` and `syslog.0`;
  - groups `fluent-bit` and `azure-secrets` are `health: ok` in `/api/v1/rules`; or, if 0b has landed, `scripts/check-prometheus-rules.sh` PASSes;
  - `ALERTS{alertname=~"FluentBitNotScraped|.*SecretExpiring"}` is empty after 35 minutes;
  - the DaemonSet did not roll.

### Task 5: The OverQuota alert, then dual-ship — the `azure_logs_ingestion` output beside the old one — **Chris approval**

**Files:**
- Create `azure/logging/alerts.tf`.
- Modify `cluster/applications/fluent-bit.yaml`:
  - `valuesObject.env` gets 5 entries;
  - `valuesObject.config.outputs` gets one `[OUTPUT]` after the existing `azure` one.
- Modify `scripts/test-fluent-bit-routing.sh`: the `azure_logs_ingestion` assertions and the `stall` case.
- Modify `cluster/prometheus-stack/rules/logging-alerts.yaml`, which gets 3 rules, and `scripts/test-logging-alerts.sh`, which gets their cases.
- Create `azure/logging/verify-ingestion.sh`.

**Interfaces:**
- Consumes Task 2's harness, Task 3's Secret keys and stream contract, and Task 4's scrape and rule file.
- Produces the Azure alert `galaxy-logs-over-quota`, which mails `var.alert_emails` through action group `galaxy-logging-alerts`.
- Produces the metric label `name="azure_logs_ingestion"`, which stays stable when the old output is removed, because of the Alias.
- Produces `azure/logging/verify-ingestion.sh [--window-end YYYY-MM-DDTHH:00:00Z] [--canary NAME=MARKER:EXPECTED]... [--project]`.
  - Chris runs it, after `az login`, with `KUBECONFIG` set.
  - It prints one `CHECK <name> PASS|FAIL|INCONCLUSIVE <numbers>` line per check, then `VERIFY PASS` (exit 0), `VERIFY INCONCLUSIVE` (exit 2) or `VERIFY FAIL` (exit 1).
  - Its default window is the last full UTC hour that ended at least 30 minutes ago.
  - `--project` instead prints `PROJECTION gb_per_day=<x> avg_billed_bytes=<b> records_24h=<n> clean_hours=<k> hours_of_day=<h>`. It exits 0, or 2 (INCONCLUSIVE) when `clean_hours < 6` or `hours_of_day < 4`.

- [ ] **Step 1: The OverQuota alert, first** (Chris's OK for the apply).
  - `azure/logging/alerts.tf`:
    - **`azurerm_monitor_action_group.logging_alerts`:** name `galaxy-logging-alerts`, `short_name = "galaxylogs"`, and a `dynamic "email_receiver"` over `var.alert_emails`, the list the budget already mails.
    - **`azurerm_monitor_scheduled_query_rules_alert_v2.over_quota`:** name `galaxy-logs-over-quota`, `scopes = [azurerm_log_analytics_workspace.luma_homelab.id]`, `evaluation_frequency = "PT1H"`, `window_duration = "PT2H"`, `severity = 2`, `mute_actions_after_alert_duration = "PT12H"`, and `action { action_groups = [<the group's id>] }`.
      - Its `criteria`: `query` = `_LogOperation | where Category =~ "Ingestion" and Detail contains "OverQuota"`, `time_aggregation_method = "Count"`, `operator = "GreaterThan"`, `threshold = 0`.
      - Its description: the cap stops ingestion while Azure still answers 2xx, so the cluster can't see it; resize the cap per `docs/logging.md`.
  - `./tf.sh plan`: expected `2 to add, 0 to change, 0 to destroy`. Then apply.
  - Commit it on its own (`git add azure/logging/alerts.tf`; `azure/logging: mail when the daily cap stops ingestion`), and push.
  - Expected: the action group's "you've been added" mail arrives.
  - **End-to-end test:** the old table still hits the 1 GB cap every day. So the alert fires within about 2 hours of the next daily hit, and Chris confirms one email. During dual-ship it fires once a day, by design.
- [ ] **Step 2: Write the tests.**
  - **Routing:** `azure_logs_ingestion.jsonl` joins the azure family in every Task 2 assertion.
  - **`stall` case:**
    - The config is `[SERVICE] Flush 1`, `[INPUT] Name dummy, Tag kube.stall, Rate 50`, and `[OUTPUT] Name file, Match kube.stall, Path /out, File stall.jsonl`. Next to them goes the Application's `azure_logs_ingestion` `[OUTPUT]` block **verbatim**, with only its `Match` changed to `kube.stall`. The `AZURE_LI_*` dummies point `dce_url` at `https://dce.invalid`.
    - Run it for 40 s with `docker run --add-host login.microsoftonline.com:10.255.255.1`.
    - **Pre-check:** from a throwaway container on the same docker network, a TCP connect to `10.255.255.1:443` must still be pending after 3 s. If it is refused or reset instead, print `FAIL stall: no blackhole`, so the case can never pass vacuously.
    - **Assertions:**
      - the fluent-bit log has at least 1 `[oauth2] could not get an upstream connection` line and 0 `[oauth2] HTTP Status` lines, so the token host really was unreachable;
      - `stall.jsonl` holds at least 90% of `Rate × (seconds − 4)` lines.
- [ ] **Step 3: Run them.** Expected: FAIL, because `azure_logs_ingestion.jsonl` doesn't exist and the stall case has no output block to copy.
- [ ] **Step 4: Add the env and the output, still without `Workers`.**
  - `env` gets five `secretKeyRef` entries from `azure-logs-ingestion`, none of them optional:

    | variable | key |
    |---|---|
    | `AZURE_LI_TENANT_ID` | `tenant-id` |
    | `AZURE_LI_CLIENT_ID` | `client-id` |
    | `AZURE_LI_CLIENT_SECRET` | `client-secret` |
    | `AZURE_LI_DCE_URL` | `dce-url` |
    | `AZURE_LI_DCR_ID` | `dcr-id` |

  - A missing key then blocks the first pod with `CreateContainerConfigError`, and the other nodes keep their old pods.
  - Add the output block below, minus its `Workers` and `net.connect_timeout` lines.
  - Run the tests. Expected: the routing cases PASS, and **`stall` FAILs**. That proves the test sees the main-thread stall. Record the stall numbers in Status.
- [ ] **Step 5: Add `Workers 1` and `net.connect_timeout 5`.** The output, exactly:

```
[OUTPUT]
    Name azure_logs_ingestion
    Alias azure_logs_ingestion
    Match kube.*
    tenant_id ${AZURE_LI_TENANT_ID}
    client_id ${AZURE_LI_CLIENT_ID}
    client_secret ${AZURE_LI_CLIENT_SECRET}
    dce_url ${AZURE_LI_DCE_URL}
    dcr_id ${AZURE_LI_DCR_ID}
    table_name GalaxyLogs_CL
    time_generated true
    time_key TimeGenerated
    compress true
    Workers 1
    net.connect_timeout 5
    Log_Level warn
    Retry_Limit 5
```

  - Comments above it:
    - `Workers 1`: the plugin's OAuth token fetch is synchronous (`flb_oauth2` disables async), and the plugin declares no workers. Without a worker, a hung `login.microsoftonline.com` would freeze every input and output; the routing test's `stall` case proves it.
    - `net.connect_timeout 5` bounds the DCE connection. The token upstream keeps its own default of 10 s, which `net.*` cannot reach; the worker is what isolates it.
    - `Log_Level warn`: successes log at info and carry `dcr_id`; every failure logs at warn.
    - `Match kube.*`, not `*`: the systemd input reads nothing on Talos, and a copy that another filter re-emits under a new tag must not reach Azure twice.
    - `compress` is mandatory: the API takes 1 MB per call, and the plugin doesn't split.
    - `Retry_Limit 5` gives about 5 minutes of retries, then a drop. Filesystem buffering was considered and rejected; see Architecture.
    - The record shape is the DCR's stream contract (`azure/logging/ingestion.tf`). The `luma-sandbox*`/`luma-browser*` exclusion happens at the input (0b).
  - Run the tests. Expected: everything PASSes, including `stall` and the dry run. If 0b's exclusion test exists, it must also PASS.
- [ ] **Step 6: Alert rules, tests first.** Add these cases to `test-logging-alerts.sh`, run it, and expect FAIL. Then add the rules and expect PASS.

  | rule | expr | for | cases that fire | case that doesn't |
  |---|---|---|---|---|
  | `FluentBitAzureLogsStalled` | `(count(sum by (pod) (rate(fluentbit_output_proc_records_total{name="azure_logs_ingestion"}[15m])) > 0) or vector(0)) < 3` | 30m | (a) pod a's output `0x60`, b and c `0+10x60`: fires at 35m. (b) **all three outputs `0x60` and all three `fluentbit_input_records_total` `0x60`**: fires at 35m. | all three `0+10x60`: never |
  | `FluentBitAzureLogsDropping` | `sum by (pod) (rate(fluentbit_output_dropped_records_total{name="azure_logs_ingestion"}[15m])) > 0` | 30m | `0+1x60`: fires at 50m | one step `0x20 1x40`: not at 50m |
  | `FluentBitAzureLogsOutputMissing` | `absent(fluentbit_output_proc_records_total{name="azure_logs_ingestion"})` | 30m | no series: fires at 31m | series present: never |

  - All three are `severity: warning`.
  - `FluentBitAzureLogsStalled` depends on no input series. A blocked output pauses tail at `Mem_Buf_Limit`, so a guard like "and the input is still flowing" would mute the alert in exactly the case it exists for. Case (b) pins that.
  - Each description names the likely cause and its fix: 401 means the secret expired or was rotated without a restart; 403 means RBAC; 413 means payload size; a stall with no HTTP lines means the token endpoint; see `docs/logging.md`.
- [ ] **Step 7: Write `verify-ingestion.sh`.**
  - **Setup:**
    - It reads the customer id into a variable with `az monitor log-analytics workspace show … --query customerId -o tsv`, and never prints it.
    - It reads the table plan with `az monitor log-analytics workspace table show … -n GalaxyLogs_CL --query plan -o tsv`.
    - Queries that touch `GalaxyLogs_CL` on a non-Analytics plan go through `az rest --method post --resource https://api.loganalytics.io --url "https://api.loganalytics.io/v1/workspaces/$WS/search?timespan=P1D" --body @<file>`.
    - Analytics-only queries use `az monitor log-analytics query`.
    - PromQL goes through the Prometheus pod, as `scripts/check-dashboards.sh` does.
    - `{S}`/`{E}` are the window start and end; `{E_unix}` is the end as epoch seconds.
    - `{R}` is **the last daily-cap reset at or before `{S}`**: the reset time of day comes from `workspaceCapping.quotaNextResetTime`. A cap hit earlier that day keeps dropping data until the next reset, so a window that only looks back an hour could pass while truncated.
  - **The checks:**

    | check | PASS when |
    |---|---|
    | `freshness` | rows > 0 and p95 lag < 600 s |
    | `cap` | 0 events in `[R, E]`; otherwise `completeness` is INCONCLUSIVE |
    | `completeness` | KQL count / PromQL ∈ [0.98, 1.02]; INCONCLUSIVE if `cap` found events or `sum(resets(fluentbit_uptime[1h] @ {E_unix})) > 0` |
    | `shape` | `no_ns / rows < 0.001` and `bad_nodes == 0` (Task 2's `Buffer_Size 0` should leave almost no record without metadata) |
    | `exclusion` | `excluded_ns == 0` and `luma_traefik == 0`. It prints `luma_system` for the record, with no verdict on it. |
    | `canary:<NAME>` | `total == EXPECTED` and `lines == EXPECTED` (exactly once) |

```kusto
// freshness
GalaxyLogs_CL
| where TimeGenerated > ago(30m)
| summarize rows = count(), p95_lag_s = percentile(datetime_diff('second', ingestion_time(), TimeGenerated), 95)
```

```kusto
// cap (Analytics query API); {R} = the last daily-cap reset at or before {S}
_LogOperation
| where TimeGenerated between (datetime({R}) .. datetime({E}))
| where Category =~ "Ingestion" and Detail contains "OverQuota"
| count
```

```kusto
// completeness, KQL side; PromQL side: sum(increase(fluentbit_output_proc_records_total{name="azure_logs_ingestion"}[1h] @ {E_unix}))
GalaxyLogs_CL
| where TimeGenerated >= datetime({S}) and TimeGenerated < datetime({E})
| count
```

```kusto
// shape
GalaxyLogs_CL
| where TimeGenerated >= datetime({S}) and TimeGenerated < datetime({E})
| summarize rows = count(), no_ns = countif(isempty(Namespace)),
    bad_nodes = countif(isnotempty(Computer) and Computer !in ("top", "middle", "bottom"))
```

```kusto
// exclusion (24 h)
GalaxyLogs_CL
| where TimeGenerated > ago(24h)
| summarize excluded_ns = countif(Namespace startswith "luma-sandbox" or Namespace startswith "luma-browser"),
    luma_traefik = countif(Namespace == "traefik"
        and Log matches regex @'"(web-|websecure-)?luma-[a-z0-9-]+@kubernetes(crd)?"'),
    luma_system = countif(Namespace == "luma-system")
```

```kusto
// canary:<NAME>
GalaxyLogs_CL
| where TimeGenerated > ago(3h)
| where Log has "{MARKER}"
| summarize n = count() by Log
| summarize lines = count(), total = sum(n)
```

  - **`--project`** (Review Focus 1). It never sizes from `Usage`, which the cap truncates.
    - A **clean hour** is a full hour in the last 72 hours with no `OverQuota` event between its day's reset and the hour's end.
    - `avg_billed_bytes` = `GalaxyLogs_CL | where bin(TimeGenerated, 1h) in (<clean hours>) | summarize avg(_BilledSize)`.
    - `records_24h` = `sum(increase(fluentbit_output_proc_records_total{name="azure_logs_ingestion"}[24h]))`.
    - `gb_per_day` = `avg_billed_bytes × records_24h / 1e9`.
    - `hours_of_day` is the number of distinct UTC hours among the clean hours. The spread guards against sizing from one time of day, such as the backup window.
- [ ] **Step 8: Commit, with Chris's sign-off, and watch.**

```bash
git add cluster/applications/fluent-bit.yaml scripts/test-fluent-bit-routing.sh \
  cluster/prometheus-stack/rules/logging-alerts.yaml scripts/test-logging-alerts.sh azure/logging/verify-ingestion.sh
nix develop -c git commit -m "logging: fluent-bit dual-ships to the Logs Ingestion API (GalaxyLogs_CL) beside the retired Data Collector output"
git push origin main
```

  - The DaemonSet rolls one node at a time. Watch it to 3/3 with the safe-rollout skill.
  - If the first new pod is not Ready within 5 minutes (a config error, a crash, or `CreateContainerConfigError`), **revert this commit and stop.**
    - Read `kubectl logs --previous` for the `[error]` line, as a count of error classes only.
- [ ] **Step 9: First 10 minutes, per pod (counts only; successes come from metrics).**
  - PromQL: `rate(fluentbit_output_proc_records_total{name="azure_logs_ingestion"}[5m]) > 0` for all three pods.
  - `kubectl -n logging logs <pod> --since=10m | grep -cE 'azure_logs_ingestion.*(http_status=|http_do=|retrying payload)'` = 0. At warn level, every such line is a failure.
  - `grep -c 'dcr_id='` = 0, which proves the success lines are suppressed.
  - `kubectl top pods -n logging`: each under 160 Mi (the limit is 200 Mi).
- [ ] **Step 10: Canaries.**
  1. Create namespace `luma-sandbox-azli-canary` (if it already exists, stop and ask).
  2. Start three `busybox:1.36.1` pods with `nodeName: bottom`:
     - `azli-canary-control` in `default` echoes `azli<hex>control <i>` 60 times, every 5 s, then sleeps;
     - `azli-canary` in `luma-sandbox-azli-canary` does the same with its own marker (role `sandbox`);
     - `azli-burst` in `default`: `p=$(printf '%0900d' 0); i=0; while [ $i -lt 3000 ]; do echo "azli<hex>burst $i $p"; i=$((i+1)); done; sleep 86400`. That is about 2.9 MB in under a second, so the chunk is at the 2 MB limit.
  3. All three are Running, and `kubectl logs` shows each marker. This proves the pods log.
  4. After 20 minutes, on `bottom`'s fluent-bit pod: `grep -cE 'azure_logs_ingestion.*(http_status=|http_do=|retrying payload)'` = 0. That covers any non-2xx, not only 413.
  5. Chris runs `verify-ingestion.sh --canary control=<m>:60 --canary sandbox=<m>:0 --canary burst=<m>:3000`.
     - Expected: `VERIFY PASS`.
     - It may also be `VERIFY INCONCLUSIVE`, but only with `completeness` INCONCLUSIVE because of `cap`. The Checkpoint then re-runs it in a clean hour.
     - **If `burst` is short, or a failure line appears, stop.** Record it, and take open question 4 to Chris before the Checkpoint.
- [ ] **Step 11: Clean up.**
  - Delete the three pods, then namespace `luma-sandbox-azli-canary`. This is pre-approved under S4; confirm first that only the canary was ever in it.
  - Record the results in Status.
  - **Rollback:** revert the Step 8 commit. The old output never stopped. The OverQuota alert (Step 1's commit) can stay.

### Checkpoint: verification window (at least 48 h) — **Chris go**

- It starts when Task 5's roll completes. No other change lands on `fluent-bit.yaml` during it.
- **Optional cap raise (open question 2).** If Chris approves it, set `daily_quota_gb = 4` at the start, and apply.
  - Every hour is then clean, and `Usage` measures the new table directly.
  - The steady-state value replaces it in Task 6.
- **Two runs of `verify-ingestion.sh` must both print `VERIFY PASS`:**
  - each with `--window-end` set to a clean hour;
  - at least 20 hours apart, and at least 4 hours apart in hour of day.
- **Projection:** `verify-ingestion.sh --project` exits 0, with `clean_hours ≥ 6` across `hours_of_day ≥ 4`. If the cap was raised, `Usage` for `GalaxyLogs_CL` over one full UTC day must agree with `gb_per_day` within ±20%; otherwise, stop and find out why.
- **No `FluentBitAzureLogs*` alert fired:** `count_over_time(ALERTS{alertname=~"FluentBitAzureLogs.*"}[48h])` is empty.
- **The OverQuota path works:** Chris received at least one `galaxy-logs-over-quota` email. If the cap was raised before the first daily hit, Task 5 Step 1's email is the proof.
- **Memory:** `max_over_time(container_memory_working_set_bytes{namespace="logging",container="fluent-bit"}[48h])` is under 160 Mi on every pod.
- **Values for Task 6:** Chris confirms the cap and budget from `gb_per_day`, by the formula in "Cost and retention".

### Task 6: Cutover — remove the old output, its Secret, and shared-key auth — **Chris approval**

**Files:**
- Modify `cluster/applications/fluent-bit.yaml`: remove the `Name azure` `[OUTPUT]`, and the env `AZURE_WORKSPACE_ID` and `AZURE_SHARED_KEY`.
- Delete `cluster/logging/azure-credentials.yaml` and `cluster/logging/azure-credentials.secret.yaml`, and modify `cluster/logging/kustomization.yaml`.
- In `azure/logging/`:
  - `main.tf`: set `local_authentication_disabled = true` on `azurerm_log_analytics_workspace.luma_homelab`, with a comment;
  - `outputs.tf`: remove `workspace_primary_shared_key` and `workspace_secondary_shared_key`;
  - `terraform.tfvars`: set `daily_quota_gb` and `monthly_budget_usd` to the Checkpoint's values;
  - `README.md`: rewrite it to cover the module's contents, `tf.sh`, the outputs, the alert, and a pointer to the runbook.
- Modify `cluster/grafana/dashboards/azure-cost-usage.yaml`: the prices.
- Create `docs/logging.md`.
- Modify `CLAUDE.md`: add a Topology row.

**Interfaces:**
- Consumes the routing test, `verify-ingestion.sh` and `tf.sh`.
- Produces the final config: one Azure output, plus 0b's syslog.
- Produces `docs/logging.md`, with these sections:
  - Pipeline
  - What never leaves the house, and what does
  - Tables and columns
  - KQL cookbook
  - Cost, cap and budget
  - Alerts
  - Credentials and rotation
  - Verify
  - Rollback
  - The old table

- [ ] **Step 1: Commit A, the output.**
  - Remove the old `[OUTPUT]` and the two env entries.
  - The routing test PASSes, and its `azure.1` assertions drop out by themselves.
  - Commit `logging: fluent-bit stops using the retired Data Collector API`, push, and watch the roll to 3/3.
  - Then `count by (name) (fluentbit_output_proc_records_total)` no longer lists `azure.1`, and `verify-ingestion.sh` `freshness` PASSes.
- [ ] **Step 2: Commit B, the Secret.** This comes after Step 1's roll, so that no pod ever references a pruned Secret.
  - `git rm` both old files, and leave only `azure-logs-ingestion.yaml` in the kustomization.
  - Commit `logging: drop the Log Analytics shared-key Secret`, and push.
  - Then Argo `azure-log-analytics-secret` prunes the old SealedSecret, `kubectl -n logging get secret azure-log-analytics` returns NotFound, and the fluent-bit restart counts are unchanged.
- [ ] **Step 3: Gate before disabling local auth (Chris runs it).**

```kusto
Usage
| where TimeGenerated > ago(7d)
| summarize GB = round(sum(Quantity) / 1000., 3), last = max(TimeGenerated) by DataType
```

```kusto
Heartbeat
| where TimeGenerated > ago(30d)
| count
```

  - PASS when:
    - the only DataTypes are `GalaxyLogs_CL`, `luma_homelab_logs_CL` (its `last` no later than Step 1's roll end) and Azure's own diagnostic types (for example `LAQueryLogs`);
    - and `Heartbeat` counts 0, meaning no legacy agent that authenticates with the workspace key.
  - Anything else ingests with the shared key: stop and ask.
- [ ] **Step 4: Terraform, with Chris's OK.**
  - Make the three edits above. The comment says: local auth is the shared-key path (the Data Collector API); disabling it makes the retired path fail closed, and AAD ingestion and queries are unaffected.
  - `./tf.sh plan` expected:
    - `azurerm_log_analytics_workspace.luma_homelab` updated in place (`local_authentication_disabled` false → true, plus `daily_quota_gb`);
    - the budget updated in place, if its amount changed;
    - 0 to add, 0 to destroy, and two outputs removed.
  - Then apply.
  - `az monitor log-analytics workspace show … --query "{local: features.disableLocalAuth, cap: workspaceCapping.dailyQuotaGb}"` → `true` and the new cap.
- [ ] **Step 5: One hour later, Chris checks:**
  - `luma_homelab_logs_CL | where ingestion_time() > datetime(<Step 1 roll end + 5m>) | count` = 0;
  - `verify-ingestion.sh` gives `VERIFY PASS`.
- [ ] **Step 6: Docs and the dashboard.**
  - **`docs/logging.md`:**
    - **Pipeline:** tail, then kubernetes (lean: no labels, annotations or merged JSON), modify and 0b's filters, then syslog (0b) and `azure_logs_ingestion` (one worker). A diagram is not needed.
    - **What never leaves the house, and what does:**
      - never: `luma-sandbox*` and `luma-browser*` (0b's `Exclude_Path`), Traefik lines for `luma-*` routers (0b's grep), and pod labels and annotations;
      - does: `luma-system` (the 2026-10-19 ruling), Traefik's `code=` query strings for every other app (0b follow-up 5), and CoreDNS's failed lookups with the pod IP and name.
    - **Tables and columns:** this plan's schema. `Log` holds the raw line; use `parse_json(Log)`. `Severity` is 0b's derived syslog severity.
    - **KQL cookbook:**
      - one pod's logs over a time range;
      - error lines by namespace (`Severity <= 3`);
      - the `Usage` query and the `OverQuota` query;
      - a reminder that Basic tables need an explicit time range, at most 30 days. Retention is 30 days, so nothing older exists.
    - **Cost, cap and budget:** the final numbers, the projection formula, and the no-dashboards rule with its $2-a-day example.
    - **Alerts:** `FluentBitNotScraped`, the three `FluentBitAzureLogs*` rules, the two `*SecretExpiring` rules, and `galaxy-logs-over-quota`, with what each one means.
    - **Credentials and rotation:**
      - the end date (`terraform output -raw logs_ingestion_secret_end_date`);
      - the gap-free procedure: add `azuread_application_password.fluent_bit_next`, apply, change the seal script's output source, reseal, **update the epoch in `azure-secret-expiry.yaml`**, commit, `kubectl -n logging rollout restart ds/fluent-bit`, verify, then remove the old password resource and apply again.
    - **Verify:** the scripts.
    - **Rollback.**
    - **The old table:** `luma_homelab_logs_CL` is read-only from the cutover, and fully aged out at cutover + 90 days.
  - **The dashboard:** in `azure-cost-usage.yaml`, replace each `* 2.3` with a per-table price, `iff(DataType == "GalaxyLogs_CL", 0.5, 2.3)`, and replace the `'10 USD'` literal with the new budget. No panel may query `GalaxyLogs_CL` itself.
  - **`CLAUDE.md` Topology:** a row "Logging". fluent-bit (namespace `logging`, DaemonSet) ships to router syslog (warnings and above, 0b) and to Log Analytics `GalaxyLogs_CL` (Basic, 30 days, a debugging store) through the Logs Ingestion API. The Azure side is terraform in `azure/logging/`. The runbook is `docs/logging.md`.
- [ ] **Step 7: Commit C.**

```bash
git add azure/logging/main.tf azure/logging/outputs.tf azure/logging/terraform.tfvars azure/logging/README.md \
  cluster/grafana/dashboards/azure-cost-usage.yaml docs/logging.md CLAUDE.md
nix develop -c git commit -m "azure/logging: disable shared-key ingestion after the Logs Ingestion cutover; logging runbook; per-table prices"
git push origin main
```

  - **Rollback:**
    - Before Step 4, revert A and B.
    - After Step 4, first set `local_authentication_disabled = false` and apply, then revert A and B. The old API may not answer at all by then, so fixing forward is the realistic path. The router keeps its syslog copy.

### Task 7: fluent-bit 5.1.3 and chart 0.58.3, pinned and visible to Renovate

2.2.3 is live (`7850cbe6`, digest-pinned through `image.digest`), so this task is not urgent. It moves off the 2.2 line, which gets no further fixes, and gives Renovate sight of the image.

**Files:**
- Modify `cluster/applications/fluent-bit.yaml`: `spec.source.targetRevision`, and `valuesObject.image`.
- Modify `.renovaterc.json`: one `customManagers` entry and two `packageRules`.

**Interfaces:**
- Consumes the routing test, 0b's exclusion test (if present) and `verify-ingestion.sh`.
- Produces two Renovate dependencies in group `fluent-bit`:
  - `fluent/fluent-bit`, from the docker datasource, tag plus digest;
  - `fluent-bit`, the chart, from the argocd manager.

- [ ] **Step 1: Failing check.**
  - `jq '[.customManagers[] | select(.managerFilePatterns[]? | test("fluent-bit"))] | length' .renovaterc.json` prints `0`.
  - With node from the dev shell, apply the matchStrings regex below to `cluster/applications/fluent-bit.yaml`. Expected: `currentValue=2.2.3 currentDigest=sha256:4d5e…`. The hotfix already put `repository`, `tag` and `digest` on adjacent lines, which the regex requires.
- [ ] **Step 2: Resolve the digest.**
  - Run `docker buildx imagetools inspect fluent/fluent-bit:5.1.3 --format '{{json .Manifest}}' | jq -r .digest`.
  - Expected: `sha256:c5542543523c9678398dd78d927c05e8425ec15b038f226b67b3b019b1a70845`, as observed on 10-19. A different value means the tag moved: stop and ask.
- [ ] **Step 3: Render diff.**
  - Render `helm template` twice: the live chart (`0.47.10`, or whatever #2662 left) with the current values, and `0.58.3` with the new values. Then `diff` the two.
  - Expected differences, and only these:
    - the `helm.sh/chart`/`app.kubernetes.io/version` labels;
    - the image string;
    - the config checksum, if any;
    - chart-default additions that render nothing with hotReload off.
  - Anything else: stop, and list it for review.
- [ ] **Step 4: Edit the Application.**
  - `targetRevision: 0.58.3`: exact, replacing the `0.47.x` range or #2662's `0.58.x`.
  - `image: {repository: fluent/fluent-bit, tag: 5.1.3, digest: sha256:c554…0845}`, with the full digest.
  - **The comment goes above `image:`**, never between its keys, because the regex needs `repository:`, `tag:` and `digest:` on adjacent lines. It says:
    - 2.2.3 fixed CVE-2024-4323 (`7850cbe6`); 5.1.3 leaves the unmaintained 2.2 line;
    - Renovate reads this block only through the customManager in `.renovaterc.json`;
    - never use an `x` range or drop the digest, and keep the three keys adjacent.
- [ ] **Step 5: Renovate config.**
  - Append to `customManagers`:

```json
{
  "customType": "regex",
  "description": "fluent-bit image in the Argo release (valuesObject image.tag + image.digest). The argocd manager never reads it, which is why it sat at 2.2.2 from 2024 to 2026. repository/tag/digest must stay on adjacent lines.",
  "managerFilePatterns": ["/^cluster/applications/fluent-bit\\.yaml$/"],
  "matchStrings": [
    "repository: (?<depName>fluent/fluent-bit)\\n\\s+tag: \"?(?<currentValue>\\d+\\.\\d+\\.\\d+)\"?\\n\\s+digest: \"?(?<currentDigest>sha256:[a-f0-9]{64})\"?"
  ],
  "datasourceTemplate": "docker"
}
```

  - Append to `packageRules`, after the existing ones:

```json
{
  "matchPackageNames": ["fluent-bit", "fluent/fluent-bit"],
  "groupName": "fluent-bit",
  "description": "Chart (argocd manager) and image (customManager) in one PR. The image is pinned apart from the chart's appVersion, so a chart bump alone never changes the binary. Verify with scripts/test-fluent-bit-routing.sh and azure/logging/verify-ingestion.sh (docs/logging.md)."
},
{
  "matchPackageNames": ["fluent-bit", "fluent/fluent-bit"],
  "matchUpdateTypes": ["minor", "major"],
  "automerge": false,
  "description": "fluent-bit carries every log off the cluster (router syslog and Azure). Patch and digest updates automerge; minor/major wait for a human. Read docs.fluentbit.io/manual/installation/upgrade-notes and run the routing test against the new tag first."
}
```

- [ ] **Step 6: Validate.**
  - `nix develop -c npx --yes --package renovate -- renovate-config-validator --strict .renovaterc.json` → `Config validated successfully`.
  - Re-run Step 1's checks. Expected: the jq count is `1`, and the regex gives `depName=fluent/fluent-bit currentValue=5.1.3 currentDigest=sha256:c554…`.
- [ ] **Step 7: Tests on the new image.** The routing test PASSes against 5.1.3, including `stall` and the dry run. 0b's exclusion test also PASSes, if it exists.
- [ ] **Step 8: Commit and watch.**

```bash
git add cluster/applications/fluent-bit.yaml .renovaterc.json
nix develop -c git commit -m "logging: fluent-bit 5.1.3, digest-pinned on chart 0.58.3; Renovate sees the image"
git push origin main
```

  - The roll reaches 3/3 (safe-rollout).
  - The DaemonSet image is `fluent/fluent-bit:5.1.3@sha256:c554…`.
  - `count(up{job="fluent-bit"} == 1)` = 3.
  - `fluentbit_output_proc_records_total` still carries `name="azure_logs_ingestion"` and `name="syslog.0"` on the v1 path.
  - The per-pod warn and error classes, counted and redacted, show no new class beyond the baseline's.
  - If 0b Task 3 is live, `retries_total{name="syslog.0"}` stays flat.
  - One hour later, Chris runs `verify-ingestion.sh` and gets `VERIFY PASS`.
  - After the next Renovate run, Dependency Dashboard #4 lists `fluent/fluent-bit`, and #2662 no longer carries fluent-bit.
  - **Rollback:** revert the commit. The tail state is an emptyDir, so there is no DB migration.

### Task 8: Exit verification and records

**Files:**
- Modify `docs/luma.md`: the "Azure log export" bullet gets the plan path and "done <date>".
- Update the memory notes.

- [ ] **Step 1: Re-run everything.**
  - The routing test, `test-logging-alerts.sh`, and `verify-ingestion.sh` (Chris) all PASS.
  - `./azure/logging/tf.sh plan` shows `No changes.`
  - The Argo apps `fluent-bit` and `azure-log-analytics-secret` are Synced/Healthy.
- [ ] **Step 2: Records.**
  - Fill in this plan's Status.
  - Update `docs/luma.md`.
  - Write a new memory note `project_azure_logs_ingestion.md`, plus its line in `MEMORY.md`. It covers:
    - the table and columns;
    - Basic, 30 days, a debugging store, and no dashboards on it;
    - the cap, the projection and the cost;
    - the secret end dates (2028-10-01, and Dex's 2028-01-01), the expiry alerts, and that rotation means a reseal, an epoch edit and a restart;
    - `Workers 1` and why;
    - the `/search` and 30-day query rules;
    - the old table's age-out date.
- [ ] **Step 3: Commit** the docs (named paths), and push.

## Rollback (summary)

| From | Do | Notes |
|---|---|---|
| Task 2 | revert | labels, annotations and `log_processed` come back to the old table |
| Task 3 | revert; `tf.sh destroy -target` the seven resources (Chris OK) | nothing consumes them |
| Task 4 | revert | monitor and rules only |
| Task 5 / Checkpoint | revert the Step 8 commit; undo a cap raise | the old output never stopped; the OverQuota alert (its own commit) may stay |
| Task 6, before terraform | revert A and B | the old Secret comes back from git (still git-crypt) |
| Task 6, after terraform | `local_authentication_disabled = false`, apply, then revert A and B | the old API may be gone; prefer fixing forward |
| Task 7 | revert | emptyDir state, no migration |

## Follow-ups and out of scope (surface, don't do)

1. **Metadata misses after `Buffer_Size 0`.** If `shape` ever shows `no_ns / rows ≥ 0.001`, add `Path_Key filename` on the tail input and an `extract()` fallback for `Namespace`/`Pod`/`Container` in the transform. It was left out because, during dual-ship, it adds about 120 B per row to the capped old table.
2. **The dead `systemd` input:** Talos has no journald, and it read 0 records in 14 days. Remove it.
3. **The tail state is an emptyDir,** so every rollout loses offsets: seconds of logs per node. A hostPath state dir would keep them. Revisit filesystem buffering together with it, after the NVMe swaps (#2661).
4. **The unused AMA DCR** `luma-homelab-logs-workspace-dcr` (Syslog/Event, no associations) in `azure/logging/main.tf` can be deleted.
5. **Phase 4 NetworkPolicies:** fluent-bit's egress needs `login.microsoftonline.com:443`, the DCE host (`*.westus3-1.ingest.monitor.azure.com:443`), the API server, and the router's UDP 514.
6. **CoreDNS** (Talos's default `log . { class error }`) ships every failed lookup with the client pod's IP and the name. Once phase 4's sandbox and browser pods resolve through kube-dns, their failed lookups reveal destinations. Decide in phase 4 whether to drop those lines or turn the log plugin off.
7. **`luma-system`'s other tenants ship too:** `acid-luma` (Postgres error lines carry statement text; parameter values are off by default), smokescreen (egress hosts) and agentgateway. Phase 1's deploy plan should verify they log metadata only. Its "Logs" bullet still says "excludes `luma-*`", which the 2026-10-19 ruling supersedes.
8. **The Argo app name `azure-log-analytics-secret`** is now a misnomer. Renaming it means delete and create, so leave it.
9. **0b follow-up 5 still stands:** Traefik access lines carry OAuth `code=` query strings for non-Luma apps into `GalaxyLogs_CL`, as they did into the old table.
10. **Delete `luma_homelab_logs_CL`** once it is empty, at cutover + 90 days, or keep it: an empty table costs nothing. Lowering the workspace's 90-day retention to 30 would age it out sooner and save about $6 in total; it deletes the old table's older rows, so it is Chris's call.

## Open questions (only Chris can answer)

**Answered (2026-10-19):**
- Table plan: **Basic**. Retention: **30 days total**, a debugging store.
- No labels column: `Labels Off` and `Annotations Off` at the source.
- Image: 2.2.3 shipped separately; 5.1.3 comes last (Task 7).
- Luma's logs: they ship, except `luma-sandbox` and `luma-browser` (the ruling).
- The usage check is a sanity check (Task 1 Step 5), not a migrate-or-drop gate.

**Still open:**
1. **Steady-state cap and budget:** approve the formula now? The cap is 1.4 × the projected GB/day, rounded up to 0.5 GB (about 2 GB). The budget is 1.3 × the projected monthly cost, rounded up to $5 (about $30, against $10 today). The Checkpoint confirms the numbers.
2. **A temporary cap raise to 4 GB for the Checkpoint:** about $4 extra over 48 hours. Every hour becomes a clean window, and `Usage` measures the new table directly. Recommended. Or keep 1 GB and size by the projection alone?
3. **Pre-approvals at sign-off:** deleting the canary namespace `luma-sandbox-azli-canary` (S4), and disabling local (shared-key) auth in Task 6, after its Step 3 gate.
4. **Only if `SMOKE BIG` returns 413, or the burst canary fails:** accept occasional loss on 2 MB bursts (with `Flush 1` lowering the steady-state chunk size), or pursue request splitting in the plugin upstream?

## Self-review (2026-10-19, revised)

- **Spec coverage:**
  - Logs Ingestion API with DCE + DCR + custom table + SP → Task 3.
  - Terraform preferred, since the repo manages Azure with it → Tasks 3, 5 and 6; `az` is read-only and Chris runs `az login` → Global Constraints, Tasks 1, 3, 5 and 6.
  - Secrets through `*.secret.yaml`, sealed with kubeseal on one file → Task 3 Step 9.
  - The ruling (Luma ships; sandbox/browser excluded; Luma's Traefik lines dropped) → Global Constraints, P1, the routing fixtures, the live canary, and the KQL `exclusion` check.
  - Router syslog untouched → Global Constraints.
  - Dual-ship window, then removal → Task 5, Checkpoint, Task 6.
  - Debugging store, Basic, 30 days, cost → "Cost and retention", Tasks 1, 3 and 6, and the Checkpoint.
  - The usage sanity check → Task 1 Step 5.
  - KQL that Chris runs → Task 1 Steps 4–5, Task 5 Step 7, Task 6 Steps 3 and 5.
  - Rollback → per task, plus the summary.
  - Renovate and pinning → Task 7; 2.2.3 noted as live.
- **Azure review findings → tasks:**
  - C1 (synchronous OAuth on the main thread) → `Workers 1` + `net.connect_timeout 5` (Task 5 Step 5), the `stall` case (Steps 2 and 4), and a stall alert with no input guard (Step 6, case b).
  - H2 (sizing from a truncated measurement) → `--project`, clean hours spread across the day, the optional cap raise, the OverQuota alert in Task 5, and Azure's own signals noted in Review Focus 1.
  - M1 (fluent-bit's own lines) → `Log_Level warn` on both Azure outputs (Tasks 2 and 5); successes counted from metrics.
  - M2 (labels, annotations, Merge_Log) → Task 2. 0b's Lua reads `log` only, which Merge_Log Off leaves untouched. Cost recomputed.
  - M3 (buffer warnings, baseline window) → `Buffer_Size 0` (Task 2), the `shape` threshold, Follow-up 1, and the baseline corrected to about 5 hours of `kubectl logs`.
  - M4 (secret expiry) → Task 4.
  - M5 (repo-wide `sign.sh`) → Task 3 Step 9, and Global Constraints.
  - M6 (filesystem buffering) → considered and rejected (Architecture), revisited in Follow-up 3.
  - Low:
    - the failure grep covers any non-2xx (Task 5 Steps 9–10);
    - the 1 MB question is settled by `SMOKE BIG` (Task 3);
    - the smoke record is captured from the harness (Task 3 Step 1);
    - the cap check looks back to the reset (Task 5 Step 7);
    - local auth is gated on `Usage` and `Heartbeat` (Task 6 Step 3);
    - the no-dashboards rule, and the dashboard price fix (Global Constraints, Task 6);
    - the Renovate key adjacency (Task 7 Step 4);
    - the workspace id in the Grafana datasource (Global Constraints);
    - CoreDNS (baseline, Follow-up 6);
    - workload identity and Auxiliary, rejected (Architecture).
- **Names checked across tasks:**
  - `GalaxyLogs_CL`, `Custom-GalaxyLogs_CL`, `galaxy-logs-dce`/`-dcr`, and the nine columns including `Severity`;
  - Secret `azure-logs-ingestion` and its five keys, the `AZURE_LI_*` env;
  - `Alias azure_logs_ingestion` / `name="azure_logs_ingestion"`;
  - `logging-alerts` (four rules), `azure-secret-expiry` (two rules), `galaxy-logs-over-quota` and `galaxy-logging-alerts`;
  - the `logs_ingestion_*` outputs;
  - the scripts `test-fluent-bit-routing.sh` (`--keep`, `stall`), `test-logging-alerts.sh`, `smoke-ingest.sh`, `seal-fluent-bit-secret.sh`, `verify-ingestion.sh` (`--project`), `tf.sh`, and the template `smoke-record.json`;
  - canary namespace `luma-sandbox-azli-canary`, with roles `control`, `sandbox`, `burst`, `smoke` and `smokebig`.
- **Review Focus → tests:**
  1. → `cap`/`completeness`/`--project`, and the OverQuota mail.
  2. → `stall`, and Stalled case (b).
  3. → `SMOKE BIG`, and the burst canary.
  4. → the smoke retry, the expiry cases, the seal post-condition, and the stall cases.
  5. → the fixtures, the sandbox canary, and `exclusion`.
- **Unverified, so tested rather than assumed:**
  - whether the 1 MB limit binds before or after gzip → `SMOKE BIG`;
  - whether undeclared top-level properties are dropped or rejected → the harness-captured smoke record, with the fallback in Task 3 Step 8;
  - whether `az monitor log-analytics query` reaches Basic tables → the script uses `/search` for them;
  - whether `10.255.255.1` blackholes from rowlett's docker → the `stall` pre-check.
