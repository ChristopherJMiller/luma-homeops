# Azure log export: Data Collector API → Logs Ingestion API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move fluent-bit's Azure shipping off the retired HTTP Data Collector API and onto the Logs Ingestion API. That means a DCE, a DCR with a transformation, a DCR-based custom table and an Entra service principal. A verified dual-ship window comes before the old output is removed. At the end, the fluent-bit image is current, digest-pinned and visible to Renovate.

**Architecture:**
- **Azure side:** terraform in the existing `azure/logging/` module, with the same state key.
  - It adds `azapi`, because azurerm 3.x has no resource for a custom table.
  - It adds `azuread`, for the app registration.
  - It uses a **DCE**, because azurerm 3.x cannot create a `kind: Direct` DCR. A DCE costs nothing, and it is the pattern fluent-bit documents.
- **Side by side, not a table migration:** a new table `GalaxyLogs_CL` gets a small, explicit schema. The classic `luma_homelab_logs_CL` stays readable until its 90-day retention ages it out.
  - Migrating a classic table is one-way. It keeps the `_s`/`_d` column sprawl. Microsoft also does not recommend running the old API against a migrated table.
- **fluent-bit gets a second output,** `azure_logs_ingestion`, next to the old `azure` one.
  - Both outputs read the same filtered stream. So 0b's input exclusion and Traefik grep filter cover both by construction.
  - Verification compares the new table with fluent-bit's own per-output counters, not with the old table. The 1 GB/day cap truncates the old table.
- **Cutover:** remove the old output, then its Secret. Then disable shared-key ("local") auth on the workspace, so the retired path fails closed.
- **Image bump last:** 2.2.2 → 5.1.3, digest-pinned, with a Renovate customManager. It is its own change set.
- **The repeatable tests are scripts:**
  - `scripts/test-fluent-bit-routing.sh` runs the real fluent-bit config, with every output swapped for a file. It proves what each output receives, exactly once.
  - `scripts/test-logging-alerts.sh` runs `promtool test rules` on the alert rules.
  - `azure/logging/smoke-ingest.sh` sends one direct POST to the DCR and reads it back with KQL.
  - `azure/logging/verify-ingestion.sh` is the KQL + PromQL verification. Chris runs it.

**Tech Stack:**
- fluent-bit 2.2.2 on chart 0.47.10, moving to 5.1.3 on chart 0.58.3, with the `azure_logs_ingestion` output.
- Azure Monitor Logs Ingestion API (DCE + DCR; the plugin calls api-version `2021-11-01-preview`).
- Log Analytics workspace `luma-homelab-logs-workspace` (West US 3, PerGB2018) in `rg-luma-homelab-logging`.
- Terraform 1.16.1: azurerm `~> 3.0` (3.117.x), azuread `~> 3.0`, azapi `~> 2.0`.
- az CLI 2.89.1, kube-prometheus-stack 79.12.0 (Prometheus v3.8.0), Sealed Secrets, Argo CD.

**Spec:** this plan has no design doc of its own. It argues from the following sources.
- `docs/luma.md`, "Decisions after spec approval", then "Final review round (2026-10-19)", the **Azure log export** bullet.
- `docs/superpowers/specs/2026-10-18-luma-design.md` §13.3 Logs.
- `docs/superpowers/plans/2026-10-18-luma-0b-platform-prep.md`: open question 4, follow-up 4 and Tasks 2–3. 0b owns the exclusions and the router syslog output.
- The planning brief of 2026-10-19, whose requirements are copied into Global Constraints.

**Status:** drafted 2026-10-19 (UTC), not yet reviewed. Measurements are read-only, taken 03:15–03:45 UTC on 10-19.

## Global Constraints

- CLAUDE.md S1–S9 apply to every task. The S2 exceptions this plan uses are:
  - `kubectl rollout restart ds/fluent-bit`, only after a credential rotation (docs/logging.md);
  - throwaway canary pods in `default`, plus namespace **`luma-azli-canary`**, which Task 4 creates and deletes. Chris pre-approves that one namespace deletion (S4) when he signs off the plan.
- Spec, verbatim (luma.md): "**Azure log export:** migrate from the retired HTTP Data Collector API to the **Logs Ingestion API**, as its own plan, written now."
- Spec, verbatim (§13.3): "**fluent-bit excludes every `luma-*` namespace** from the Azure and syslog outputs (an input exclude on the namespace path)".
  - Keep 0b's `Exclude_Path /var/log/containers/*_luma-*_*.log` on the tail input, and 0b's grep filter that drops Traefik access lines for `luma-*` routers.
  - No output added here may bypass either of them.
- **The router syslog output stays, and 0b owns it** (the receiver, plus "warnings and above"). This plan never edits the `Name syslog` `[OUTPUT]` or any filter that 0b adds.
- **Data Collector API:** support ended **2026-09-14**. Ingestion still answered HTTP 200 at 03:2x UTC on 2026-10-19, and it may stop at any time.
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
  | PrometheusRule | `prometheus/logging-alerts`, group `fluent-bit` |
  | Canary markers | `azli<32 hex><role>`, with role one of `control`, `luma`, `burst`, `smoke` |

- **Decisions:** the values Chris picks in Task 1 (table plan, daily cap, budget, retention, labels) are recorded in Status, and override the defaults named here.
  - Defaults: plan `Basic`, 90 days total retention, no labels column.
  - The daily cap **stays at 1 GB until Task 5**.
- **Pinning:**
  - The image is pinned by tag plus index digest.
  - The chart is pinned by exact version.
  - After Task 6, never a floating tag or an `x` range.
- **Secrets:**
  - Never print any of these in chat, docs, commits or PRs: the client secret, the workspace customer id, the tenant and client ids, the DCR immutable id, the DCE URL.
  - The old output logs the workspace id on every flush, and the new one logs `dcr_id`. Grep fluent-bit logs for counts only.
  - Decode secrets only into `umask 077` scratch files, and delete those files afterwards.
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

1. **The daily cap silently truncates the new table.**
   - What happens: past the cap, Azure drops data but still answers with success. fluent-bit counts it as delivered, and nothing in the cluster can see the loss.
   - During dual-ship, both tables share the one 1 GB cap.
   - Expected: completeness is judged only in windows with no `OverQuota` event. Otherwise it reports INCONCLUSIVE, never PASS. Task 5 sets a cap that is sized from measurement.
   - Test: `verify-ingestion.sh` checks `cap` and `completeness` (Task 4), and the Checkpoint needs two clean windows.
2. **Records shipped twice.**
   - The risk: 0b's warnings-and-above syslog path may re-tag copies of records. The old output's `Match *` would catch those copies.
   - Expected: every container line reaches `GalaxyLogs_CL` exactly once.
   - Test: the routing test's exactly-once assertions on `info`, `warn` and `error` fixtures (Task 4), and the canary `lines == total == expected` in KQL (Task 4, step 9).
3. **A single flush over the API's 1 MB per-call limit.**
   - Microsoft's limit applies to "both compressed and uncompressed data". The plugin sends one fluent-bit chunk (up to about 2 MB of msgpack) per request, does not split it, and retries any non-2xx response until `Retry_Limit`, then drops it.
   - Expected: a burst of 3000 lines of about 950 B each arrives as exactly 3000 rows, with no `http_status=413`.
   - Test: the burst canary (Task 4, step 9).
4. **Credentials fail quietly:**
   - an RBAC role assignment that hasn't propagated yet (403s right after the apply);
   - an expired client secret (401s from 2028-10-01);
   - a rotation that never reseals, because `sign.sh` skips any `*.secret.yaml` that already has a sealed sibling.
   - Expected:
     - the smoke POST retries until it gets a 204 before fluent-bit ever uses the credential;
     - sustained auth failure fires `FluentBitAzureLogsStalled` within about 45 minutes;
     - the seal script deletes the sealed sibling first.
   - Test: the Task 2 smoke test, the promtool cases in Task 4, and the seal script's post-condition (Task 2).
5. **`luma-*` data, or Luma's Traefik lines with OAuth `code=`, leave the house through the new output.**
   - Expected: none at all, in any output.
   - Test: the routing-test fixtures, the live `luma-azli-canary` count of 0, and the 24-hour KQL `exclusion` check (Task 4).

---

## Measured baseline (2026-10-19 UTC, read-only)

**fluent-bit:**
- Argo `fluent-bit` is Synced/Healthy at chart 0.47.10.
- The image is `fluent/fluent-bit:2.2.2`, and three pods are 1/1 with about 14.1 days of uptime.
- The output section has `syslog` (`syslog.0`) and then `azure` (`azure.1`): Customer_ID/Shared_Key from Secret `logging/azure-log-analytics`, `Log_Type luma_homelab_logs`, `Match *`.

**Volume (sum of the three pods over about 14.1 days):**
- `tail.0` read 14.25 GB and 55.1 M records. That is about **1.0 GB/day and 3.9 M records/day** of raw container logs.
- `azure.1` processed **56.5 GB of msgpack, about 4.0 GB/day**. Enrichment roughly quadruples the bytes: the kubernetes map, plus `log_processed` duplicating every JSON line.
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

**Warnings:** the only warning class in 14 days, apart from syslog, is 406 lines of `[http_client] cannot increase buffer: current=32000 requested=64768`, on `bottom`'s pod. That comes from the kubernetes filter's `Buffer_Size`, and those records ship without metadata.

**Record shape:**
- Before the kubernetes filter, fluent-bit 5.1.3 run locally on a CRI fixture gives the keys `time`, `stream`, `_p`, `log`, `hostname`, `cluster`.
- In the cluster, the kubernetes filter adds `kubernetes`, and JSON lines also get `log_processed`.
- **`hostname` is the fluent-bit pod's name, not the node's.** The node is `kubernetes.host`.

**Pod metadata:** 217 pods, with an average labels JSON of 240 B and annotations of 56 B.

**Plugin facts (source read at v2.2.2 and v5.1.3):**
- The URL is `{dce_url}/dataCollectionRules/{dcr_id}/streams/Custom-{table_name}?api-version=2021-11-01-preview`.
- Auth is OAuth2 client credentials only: `tenant_id`, `client_id`, `client_secret`, plus `auth_url` in 5.x. There is no managed identity and no workload identity.
- `time_generated true` writes ISO 8601 with milliseconds (`…T03:27:58.123Z`) under `time_key`.
- There is no payload splitting, and any non-2xx response is retried.
- Every successful flush logs `http_status=…, dcr_id=…, table=…` at info level.
- The options are identical in 2.2.2 and 5.1.3, so **no upgrade is needed for the migration itself.**

**Why upgrade anyway:**
- **CVE-2024-4323** (CVSS 9.8) is memory corruption in the embedded HTTP server. It affects 2.0.7 to before 2.2.3, and 3.0.0 to before 3.0.4. **2.2.2 is affected**, and `HTTP_Server` listens on `0.0.0.0:2020` in every pod.
- The image sat at 2.2.2 because Renovate's argocd manager sees only the chart, never `valuesObject.image.tag`.
- Grouped Renovate PR #2662 (held for 0a) moves the chart `0.47.x → 0.58.x` and leaves the image alone.
- fluent-bit 5.1.3 was released 2026-10-01. It is chart 0.58.3's appVersion, and its docker.io index digest is `sha256:c5542543523c9678398dd78d927c05e8425ec15b038f226b67b3b019b1a70845`.
- The upgrade notes for 3.0 through 5.1 list nothing that touches this config: tail, systemd, kubernetes, grep, modify, syslog, both azure outputs, `/api/v1/*` or classic `.conf`.
  - Two notes come close: 5.0 makes `rewrite_tag` with filesystem storage pause at `storage.max_chunks_up`, and this config uses memory buffering.
  - The legacy `out_azure` still exists in 5.1.3.

**Chart 0.47.10 → 0.58.3:**
- Both versions support `image.digest` and `serviceMonitor.{selector,path}`.
- 0.58.3 changes the readiness default to `/api/v2/health`, which our override replaces.
- It adds `hotReload` securityContext (hotReload stays off) and an optional `automountServiceAccountToken`.

**Azure:**
- `azure/logging/` is azurerm `~> 3.0`, and its lock file is gitignored.
- The workspace is PerGB2018, with 90 days of retention, `daily_quota_gb = 1` and a $10 monthly budget.
- There is also an unused AMA syslog/event DCR.
- `azure/entra/` holds the Dex app (azuread 3.9.0, lock committed).

**Secrets:**
- `cluster/logging/azure-credentials.secret.yaml` (keys `workspace-id`, `shared-key`) is sealed into `azure-credentials.yaml`.
- Argo app `azure-log-analytics-secret` (path `cluster/logging`, prune on) deploys it.

**Prometheus:**
- fluent-bit is unscraped. Monitors need `release: prometheus`.
- Rules load today when they are PrometheusRule CRDs labelled `prometheus: kube-prometheus` + `role: alert-rules` in namespace `prometheus`. 0b Task 5 changes that to `release: prometheus`. A rule carrying all three labels loads in both states.

**Prices (Azure Retail Prices API, West US 3, 2026-10-19):**

| Meter | Price |
|---|---|
| Analytics ingestion | $2.30/GB after 5 GB/month free (per billing account) |
| Analytics retention beyond 31 days | $0.10/GB/month |
| Basic ingestion | $0.50/GB |
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

## Cost and retention

These are estimates from the fluent-bit counters above. **Task 1 replaces them with `Usage` numbers.**
- The new table keeps about 0.45 KB per row: `Log` plus seven short columns. Without labels that is about **1.75 GB/day**; with labels, about 2.7 GB/day.
- The incoming JSON is about 4.9 GB/day.

| Option | Billed GB/day | ≈ $/month | Complete? |
|---|---|---|---|
| **Today:** Analytics, classic table, 1 GB cap | 1.0 (the cap) | 57.5 ingestion + 5.9 retention ≈ **63** | No: the cap stops collection after roughly the first quarter of each day (est.). The budget is $10, so Chris may already be getting budget alerts. |
| Analytics, new table, uncapped | 1.75 | 109 + 10 ≈ 120 | Yes |
| **Basic, new table, cap 2.5 GB, 90 days total (recommended)** | 1.75 | 26 ingestion + 2 long-term retention + 2 transform excess + queries in cents ≈ **31** | Yes |
| Basic + a `Labels` column | 2.7 | 40 + 3 ≈ 44 | Yes |
| Auxiliary | 1.75 (processing on 4.9) | 3 + 15 + 2 ≈ 19 | Yes, but:<ul><li>it requires microsecond `TimeGenerated`, and the plugin sends milliseconds;</li><li>queries are slow;</li><li>there are no alerts;</li><li>there is no cap.</li></ul> |

Why Basic, on balance:
- Every query here is ad hoc forensics on recent data.
- It holds 30 days interactive, and days 31–90 sit in long-term retention, which a search job reaches.
- It supports full KQL, including joins with the Analytics `_LogOperation`.
- It costs about half of today, for complete logs instead of a quarter.

## Sequence at a glance

| task | touches | Chris "go" | user-visible blip |
|---|---|---|---|
| 1 Baseline and decisions | read-only (Chris: `az login`, KQL) | answers | — |
| 2 Azure resources, smoke test, sealed credentials | **Azure** (terraform), a new Secret in `logging` | **yes** (apply) | — |
| 3 Prometheus scrapes fluent-bit | ServiceMonitor, PrometheusRule | sign-off | — |
| 4 Dual-ship | fluent-bit DaemonSet roll | **yes**, plus the canary namespace delete | seconds of tail gap per node at the roll (state is an emptyDir) |
| — Checkpoint, at least 48 h | read-only | **go** | — |
| 5 Cutover | DaemonSet roll, Secret prune, **Azure** (terraform) | **yes** | roll gap |
| 6 fluent-bit 5.1.3, chart 0.58.3, Renovate | DaemonSet roll | sign-off | roll gap |
| 7 Exit verification and records | read-only, docs | — | — |

**Task 6 can run before Task 4.** Chris may want that because of the CVE (open question 5).
- Every task reads the image tag from the Application, so nothing else changes.
- Task 6's checks of `name="azure_logs_ingestion"` are skipped if the output doesn't exist yet.

## Preconditions (each one is a go/no-go)

| # | Check | Needed for |
|---|---|---|
| P1 | 0b Task 2 is live: the live ConfigMap holds the `Exclude_Path` line and the Traefik grep filter. | Task 4 |
| P2 | Nothing else is in flight on `fluent-bit.yaml` (0b Task 3 is either done or not started). | Tasks 3–6 |
| P3 | Chris has run `az login`. His account can create role assignments on `rg-luma-homelab-logging` (Owner or User Access Administrator) and Entra app registrations (it created `galaxy-dex`). | Tasks 1, 2, 5 |
| P4 | Argo `fluent-bit` is Synced/Healthy, and the DaemonSet is 3/3. | each fluent-bit task |
| P5 | `terraform plan` in `azure/logging` shows "No changes" before anything is added. | Task 2 |
| P6 | The state of #2662 is known: the chart may already be `0.58.x`. | Task 6 |

---

### Task 1: Baseline and Chris's decisions (read-only)

**Files:** none.

**Interfaces:**
- Produces these, recorded in Status:
  - daily billable GB per table over 30 days;
  - the times of `OverQuota` events, relative to the workspace's cap reset hour, and the reset hour itself;
  - the top 10 namespaces by billed MB;
  - whether `luma_homelab_logs_CL` is `Classic`;
  - P5;
  - Chris's answers to open questions 1–6.

- [ ] **Step 1: Cluster reads.**
  - Argo app, DaemonSet image and the per-pod counters, through `kubectl get --raw /api/v1/namespaces/logging/pods/<pod>:2020/proxy/api/v1/metrics/prometheus`.
  - `kubectl -n logging logs <pod> --since=30m | grep -c 'output:azure:azure.1.*HTTP status=200'`, which prints counts only.
  - Compare with the baseline. If any rate moved by more than 25%, redo the cost table.
- [ ] **Step 2: 0b state.**
  - From the live ConfigMap `logging/fluent-bit`, key `fluent-bit.conf`: `grep -c 'Exclude_Path /var/log/containers/\*_luma-\*_\*.log'`, and the count of `Name grep` sections.
  - Record P1 and P2.
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
- [ ] **Step 5: Decide.**
  - Recompute the cost table from Step 4.
  - Ask Chris open questions 1–6 in one AskUserQuestion batch.
  - Record the answers in Status. Any value he changes overrides the defaults in Global Constraints and in Tasks 2 and 5.

### Task 2: Azure resources in terraform, a direct-POST smoke test, and the sealed credentials — **Chris approval**

**Files:**
- Modify `azure/logging/main.tf`:
  - add `azuread = { source = "hashicorp/azuread", version = "~> 3.0" }` and `azapi = { source = "Azure/azapi", version = "~> 2.0" }` to `required_providers`;
  - add `provider "azuread" {}` and `provider "azapi" {}`.
- Create `azure/logging/ingestion.tf`.
- Modify `azure/logging/variables.tf` and `azure/logging/outputs.tf`.
- Modify `azure/logging/.gitignore`: drop the `.terraform.lock.hcl` line, then commit the lock file, as `azure/entra` does.
- Create the scripts `azure/logging/tf.sh`, `azure/logging/smoke-ingest.sh` and `azure/logging/seal-fluent-bit-secret.sh`.
- Create the Secret files `cluster/logging/azure-logs-ingestion.secret.yaml` (git-crypt) and `cluster/logging/azure-logs-ingestion.yaml` (the SealedSecret, generated). Modify `cluster/logging/kustomization.yaml` to add the latter.

**Interfaces:**
- Produces these variables:

  | variable | type | default | validation |
  |---|---|---|---|
  | `galaxy_logs_table_name` | string | `GalaxyLogs_CL` | — |
  | `galaxy_logs_table_plan` | string | `Basic` | `Basic` or `Analytics` |
  | `galaxy_logs_total_retention_days` | number | 90 | 30–730 |
  | `fluent_bit_secret_end_date` | string | `2028-10-01T00:00:00Z` | — |

- Produces these outputs:
  - `logs_ingestion_tenant_id`
  - `logs_ingestion_client_id`
  - `logs_ingestion_client_secret` (sensitive)
  - `logs_ingestion_dce_url`, the DCE's `logs_ingestion_endpoint`
  - `logs_ingestion_dcr_immutable_id`
  - `logs_ingestion_table_name`
  - `logs_ingestion_secret_end_date`
- Produces **the stream contract**, which Task 4's output must satisfy:
  - stream `Custom-GalaxyLogs_CL` with the columns `TimeGenerated:datetime`, `log:string`, `stream:string`, `kubernetes:dynamic`;
  - table columns, which are the transform's output: `TimeGenerated:dateTime`, then `Computer`, `Namespace`, `Pod`, `Container`, `Image`, `Stream` and `Log`, all strings.
- Produces `azure/logging/tf.sh <terraform args>`. It is entra's `tf.sh`, with the path changed: it checks `az account show`, `cd`s to the module, then `exec terraform "$@"`.
- Produces `azure/logging/smoke-ingest.sh`:
  - it prints `SMOKE POST <code>`, then `SMOKE PASS` or `SMOKE FAIL <reason>`, and exits 0 only on PASS;
  - it runs for up to 50 minutes, so run it under Monitor.
- Produces `azure/logging/seal-fluent-bit-secret.sh`, which (re)writes both Secret files and prints only the paths and key names.

- [ ] **Step 1: Write the smoke test first.** `smoke-ingest.sh`:
  1. Read the outputs with `./tf.sh output -json` into shell variables only. It never echoes them, and `set -x` is forbidden.
  2. Get a token: `POST https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`.
     - Form fields: `client_id`, `client_secret`, `scope=https://monitor.azure.com/.default`, `grant_type=client_credentials`.
     - The secret goes in through stdin (`--data @-`), never argv.
  3. POST one record, shaped exactly like a real enriched record, to `<dce>/dataCollectionRules/<dcr>/streams/Custom-GalaxyLogs_CL?api-version=2023-01-01`:
     `[{"TimeGenerated":"<now, ms, Z>","time":"<same>","stream":"stdout","_p":"F","log":"azli<hex>smoke","hostname":"smoke","cluster":"luma-homelab","kubernetes":{"pod_name":"azli-smoke","namespace_name":"azli-smoke","container_name":"smoke","host":"smoke","container_image":"smoke:0","labels":{"app":"smoke"}},"log_processed":{"k":"v"}}]`
     - On a 403, retry every 60 s for up to 30 minutes (RBAC propagation).
     - On a 400, print the response body (it holds no secret) and FAIL.
  4. Poll every 60 s, for up to 20 minutes (a new table takes 10–15 minutes), with `GalaxyLogs_CL | where Log == "<marker>" | project Namespace, Pod, Container, Computer, Image, Stream`. This goes through `/search` when the table's plan isn't Analytics; see Task 4 Step 6.
     - PASS = exactly 1 row equal to `azli-smoke, azli-smoke, smoke, smoke, smoke:0, stdout`.
- [ ] **Step 2: Run it.** Expected: `SMOKE FAIL` naming a missing output `logs_ingestion_dce_url`.
- [ ] **Step 3: Write `ingestion.tf`.**
  - **Header comment:**
    - why a DCE: azurerm 3.x has no `kind = "Direct"`;
    - why azapi: azurerm 3.x has no custom-table resource;
    - why the app lives here and not in `azure/entra`: its only permission is this DCR, so one apply creates or destroys the whole pipeline;
    - rotation is in `docs/logging.md`.
  - **`azapi_resource.galaxy_logs_table`:**
    - `type = "Microsoft.OperationalInsights/workspaces/tables@2022-10-01"`, `name = var.galaxy_logs_table_name`, `parent_id = azurerm_log_analytics_workspace.luma_homelab.id`.
    - `body.properties` holds `plan`, `totalRetentionInDays`, and `schema = { name, columns }` with the eight table columns.
    - It holds `retentionInDays = var.galaxy_logs_total_retention_days` **only when the plan is `Analytics`** (a `merge()` with a conditional). Basic's interactive 30 days are fixed.
  - **`azurerm_monitor_data_collection_endpoint.galaxy_logs`:** `galaxy-logs-dce`, in the logging resource group's location, with `public_network_access_enabled = true`.
  - **`azurerm_monitor_data_collection_rule.galaxy_logs`:**
    - name `galaxy-logs-dcr`, with `data_collection_endpoint_id` set;
    - `destinations.log_analytics { name = "galaxy-logs-workspace", workspace_resource_id = … }`;
    - a `stream_declaration` with the four stream columns;
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
    Log = tostring(log)
```

  - Comment on the transform:
    - `hostname` is the fluent-bit pod's name, so the node comes from `kubernetes.host`;
    - `log_processed`, `labels`, `annotations`, the ids and `_p`/`time` are deliberately dropped. Parse JSON at query time with `parse_json(Log)`.
  - **`azuread_application.fluent_bit`:** `galaxy-fluent-bit-logs`, with `owners = [data.azuread_client_config.current.object_id]`.
  - **`azuread_service_principal.fluent_bit`:** with `client_id` and the same owners.
  - **`azuread_application_password.fluent_bit`:** `display_name = "fluent-bit-logs-ingestion"`, `end_date = var.fluent_bit_secret_end_date`.
  - **`azurerm_role_assignment.fluent_bit_dcr_publisher`:**
    - `scope` = the DCR's id, `role_definition_name = "Monitoring Metrics Publisher"`;
    - the SP's `object_id`, with `principal_type = "ServicePrincipal"`, which avoids the "PrincipalNotFound" replication race.
  - `data "azuread_client_config" "current" {}`.
- [ ] **Step 4: Validate.** Run `./azure/logging/tf.sh init -input=false`, then `validate`, then `fmt -check`. Expected: `Success!`, and no fmt diff.
- [ ] **Step 5: Plan.**
  - Run `./azure/logging/tf.sh plan -out=/tmp/claude-…/azli.tfplan`, in the scratchpad.
  - Expected: **`7 to add, 0 to change, 0 to destroy`**: the table, DCE, DCR, application, service principal, password and role assignment.
  - Anything else is drift or a mistake. Stop.
- [ ] **Step 6: Apply, with Chris's OK.** Run `./azure/logging/tf.sh apply <plan>`.
- [ ] **Step 7: Smoke.**
  - Run `smoke-ingest.sh` under Monitor (its own deadlines total 50 minutes).
  - Expected: `SMOKE POST 204`, then `SMOKE PASS`.
  - **Fallback:** if the POST returns 400 naming undeclared properties, add exactly those top-level properties to `stream_declaration` (as string or dynamic), then re-plan and re-apply. Record the rejected names. If `_p` is rejected as a column name, stop and report.
- [ ] **Step 8: Seal.** `seal-fluent-bit-secret.sh`:
  1. `umask 077`.
  2. Render `cluster/logging/azure-logs-ingestion.secret.yaml`. It is a single `kind: Secret`, `name: azure-logs-ingestion`, `namespace: logging`, `type: Opaque`, and its `stringData` holds the five keys from the outputs.
  3. `rm -f cluster/logging/azure-logs-ingestion.yaml`, because `sign.sh` skips any secret that already has a sealed sibling.
  4. Run `./sign.sh` from the repo root.
  5. Assert that the sealed file exists and starts its document with `kind: SealedSecret`.
  6. Print `sealed cluster/logging/azure-logs-ingestion.yaml keys: tenant-id client-id client-secret dce-url dcr-id`.
  - Also add `azure-logs-ingestion.yaml` to `cluster/logging/kustomization.yaml`.
- [ ] **Step 9: Commit and watch.**

```bash
git add azure/logging/main.tf azure/logging/ingestion.tf azure/logging/variables.tf azure/logging/outputs.tf \
  azure/logging/.gitignore azure/logging/.terraform.lock.hcl azure/logging/tf.sh azure/logging/smoke-ingest.sh \
  azure/logging/seal-fluent-bit-secret.sh cluster/logging/azure-logs-ingestion.secret.yaml \
  cluster/logging/azure-logs-ingestion.yaml cluster/logging/kustomization.yaml
nix develop -c git commit -m "azure/logging: Logs Ingestion pipeline (DCE, DCR, GalaxyLogs_CL, fluent-bit SP) and its sealed credentials"
git push origin main
```

  - Argo `azure-log-analytics-secret` goes Synced.
  - `kubectl -n logging get secret azure-logs-ingestion -o json | jq -r '.data | keys[]'` prints exactly the five key names.
  - **Rollback:** revert the commit, then `./tf.sh destroy -target=…` the seven resources, with Chris's OK. Nothing in the cluster uses them yet.

### Task 3: Prometheus scrapes fluent-bit

**Files:**
- Modify `cluster/applications/fluent-bit.yaml`: `valuesObject.serviceMonitor`.
- Create `cluster/prometheus-stack/rules/logging-alerts.yaml`, and add it to `resources:` in `cluster/prometheus-stack/rules/kustomization.yaml`.
- Create `scripts/test-logging-alerts.sh`.

**Interfaces:**
- Produces series labelled `job="fluent-bit"`, `pod`, and `name` (the plugin instance or Alias), from `/api/v1/metrics/prometheus`.
- Produces PrometheusRule `prometheus/logging-alerts`, labelled `prometheus: kube-prometheus`, `role: alert-rules` and `release: prometheus`, with group `fluent-bit` (interval 60s).
- Produces `scripts/test-logging-alerts.sh`. It extracts `.spec` with yq into a temp rules file and runs `promtool test rules` against a test file that the script writes inline. Exit 0 means every case passes. Task 4 adds its cases.

- [ ] **Step 1: Write the test.** One case, `FluentBitNotScraped`:
  - with `up{job="fluent-bit"}` = 1 for 2 of 3 pods over 45 minutes, the alert fires at `eval_time: 35m`;
  - with 3 of 3 up, it never fires.
- [ ] **Step 2: Run it.** Expected: FAIL, because the rule file doesn't exist.
- [ ] **Step 3: Write the rule.**
  - `FluentBitNotScraped`: `expr: count(up{job="fluent-bit"} == 1) < 3 or absent(up{job="fluent-bit"} == 1)`, `for: 30m`, `severity: warning`.
  - The summary points to `docs/logging.md`.
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
  cluster/prometheus-stack/rules/kustomization.yaml scripts/test-logging-alerts.sh
nix develop -c git commit -m "logging: Prometheus scrapes fluent-bit; alert when it is not scraped"
git push origin main
```

- [ ] **Step 8: Live check.** Run these through the Prometheus pod (`kubectl exec … wget`, as `scripts/check-dashboards.sh` does):
  - `count(up{job="fluent-bit"} == 1)` = 3;
  - `count by (name) (fluentbit_output_proc_records_total)` lists `azure.1` and `syslog.0`;
  - group `fluent-bit` is `health: ok` in `/api/v1/rules`; or, if 0b has landed, `scripts/check-prometheus-rules.sh` PASSes;
  - `ALERTS{alertname="FluentBitNotScraped"}` is empty after 35 minutes;
  - the DaemonSet did not roll.

### Task 4: Dual-ship — the `azure_logs_ingestion` output beside the old one — **Chris approval**

**Files:**
- Modify `cluster/applications/fluent-bit.yaml`:
  - `valuesObject.env` gets 5 entries;
  - `valuesObject.config.outputs` gets one `[OUTPUT]` after the existing `azure` one.
- Create `scripts/test-fluent-bit-routing.sh`.
- Modify `cluster/prometheus-stack/rules/logging-alerts.yaml`, which gets 3 rules, and `scripts/test-logging-alerts.sh`, which gets their cases.
- Create `azure/logging/verify-ingestion.sh`.

**Interfaces:**
- Consumes Task 2's Secret keys and stream contract, and Task 3's scrape and rule file.
- Produces the metric label `name="azure_logs_ingestion"`, which stays stable when the old output is removed, because of the Alias.
- Produces `nix shell nixpkgs#yq-go -c scripts/test-fluent-bit-routing.sh`, where exit 0 means every case passes.
  - It reads the image tag (and digest, once present) from the Application, and it needs `docker`.
  - Tasks 5–7 re-run it.
- Produces `azure/logging/verify-ingestion.sh [--window-end YYYY-MM-DDTHH:00:00Z] [--canary NAME=MARKER:EXPECTED]...`.
  - Chris runs it, after `az login`, with `KUBECONFIG` set.
  - It prints one `CHECK <name> PASS|FAIL|INCONCLUSIVE <numbers>` line per check, then `VERIFY PASS` (exit 0), `VERIFY INCONCLUSIVE` (exit 2) or `VERIFY FAIL` (exit 1).
  - Its default window is the last full UTC hour that ended at least 30 minutes ago.

- [ ] **Step 1: Write the routing test.**
  - **Harness:**
    - Extract `config.inputs`, keeping only the `Name tail` section, with test overrides `Read_From_Head On` and `DB` pointed into a temp dir.
    - Extract every `[FILTER]` except `Name kubernetes`. That keeps 0b's grep and any severity or `rewrite_tag` filters.
    - Extract the `parsers`.
    - Extract every `[OUTPUT]`, rewritten to `Name file`, with its own `Match`/`Match_Regex`, `Path /out`, `Format plain`, and `File <Alias, else Name.index>.jsonl` (for example `syslog.0.jsonl`, `azure.1.jsonl`, `azure_logs_ingestion.jsonl`). Drop every other key.
    - Set `HOSTNAME` and every `AZURE_*` variable to dummies.
    - Run `docker run --rm fluent/fluent-bit:<tag>` for about 15 s, with the fixtures mounted at `/var/log/containers`.
  - **Fixtures:** CRI lines `<RFC3339> stdout F <payload>`, with alphanumeric markers.

    | file (`<id>` = 64 hex) | lines | expected in each azure-family output | expected in any output |
    |---|---|---|---|
    | `app-1_default_app-<id>.log` | `m1` plain; `{"level":"error","msg":"m2"}`; `level=warn m3`; `WARN m4` | each **exactly once** | — |
    | `x-1_notluma-x_c-<id>.log` | `m5` | once | — |
    | `core-1_luma-system_core-<id>.log` | `m6` | 0 | **0** |
    | `c-1_luma-azli-canary_c-<id>.log` | `m7` | 0 | **0** |
    | `traefik-1_traefik_traefik-<id>.log` | 0b's 5 CLF lines (3 `luma-…` routers, then `media-…plex…` and `notluma-x`) | the last 2 once each | the first 3: **0** |

  - The azure-family outputs are `azure_logs_ingestion` always, plus `azure.1` while it exists.
  - The script prints PASS or FAIL per assertion, and exits 1 on any FAIL.
- [ ] **Step 2: Run it.** Expected: FAIL, because `azure_logs_ingestion.jsonl` doesn't exist.
- [ ] **Step 3: Add the env and the output.**
  - `env` gets five `secretKeyRef` entries from `azure-logs-ingestion`, none of them optional:

    | variable | key |
    |---|---|
    | `AZURE_LI_TENANT_ID` | `tenant-id` |
    | `AZURE_LI_CLIENT_ID` | `client-id` |
    | `AZURE_LI_CLIENT_SECRET` | `client-secret` |
    | `AZURE_LI_DCE_URL` | `dce-url` |
    | `AZURE_LI_DCR_ID` | `dcr-id` |

  - A missing key then blocks the first pod with `CreateContainerConfigError`, and the other nodes keep their old pods.
  - The output, exactly:

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
    Retry_Limit 5
```

  - Comments above it:
    - `Match kube.*`, not `*`: the systemd input reads nothing on Talos, and a copy that another filter re-emits under a new tag must not reach Azure twice.
    - `compress` is mandatory: the API takes 1 MB per call, and the plugin doesn't split.
    - `Retry_Limit 5` bounds how long a failing credential holds chunks, to minutes.
    - The record shape is the DCR's stream contract (`azure/logging/ingestion.tf`), and `luma-*` exclusion happens at the input (0b).
- [ ] **Step 4: Run the routing test.**
  - Expected: PASS.
  - If 0b's `scripts/test-fluent-bit-exclusion.sh` exists, it must also PASS.
- [ ] **Step 5: Alert rules, tests first.** Add these cases to `test-logging-alerts.sh`, run it, and expect FAIL. Then add the rules and expect PASS.

  | rule | expr | for | case that fires | case that doesn't |
  |---|---|---|---|---|
  | `FluentBitAzureLogsStalled` | `sum by (pod) (rate(fluentbit_output_proc_records_total{name="azure_logs_ingestion"}[15m])) == 0 and on (pod) sum by (pod) (rate(fluentbit_input_records_total[15m])) > 0` | 30m | output flat, input `0+10x60`: fires at 50m | output `0+10x60`: never |
  | `FluentBitAzureLogsDropping` | `sum by (pod) (rate(fluentbit_output_dropped_records_total{name="azure_logs_ingestion"}[15m])) > 0` | 30m | `0+1x60`: fires at 50m | one step `0x20 1x40`: not at 50m |
  | `FluentBitAzureLogsOutputMissing` | `absent(fluentbit_output_proc_records_total{name="azure_logs_ingestion"})` | 30m | no series: fires at 31m | series present: never |

  - All three are `severity: warning`.
  - Each description names the likely cause and its fix: 401 means the secret expired or was rotated without a restart; 403 means RBAC; 413 means payload size; see `docs/logging.md`.
- [ ] **Step 6: Write `verify-ingestion.sh`.**
  - **Setup:**
    - It reads the customer id into a variable with `az monitor log-analytics workspace show … --query customerId -o tsv`, and never prints it.
    - It reads the table plan with `az monitor log-analytics workspace table show … -n GalaxyLogs_CL --query plan -o tsv`.
    - Queries that touch `GalaxyLogs_CL` on a non-Analytics plan go through `az rest --method post --resource https://api.loganalytics.io --url "https://api.loganalytics.io/v1/workspaces/$WS/search?timespan=P1D" --body @<file>`.
    - Analytics-only queries use `az monitor log-analytics query`.
    - PromQL goes through the Prometheus pod, as `scripts/check-dashboards.sh` does.
    - `{S}`/`{E}` are the window start and end; `{E_unix}` is the end as epoch seconds.
  - **The checks:**

    | check | PASS when |
    |---|---|
    | `freshness` | rows > 0 and p95 lag < 600 s |
    | `cap` | 0 events; otherwise `completeness` is INCONCLUSIVE |
    | `completeness` | KQL count / PromQL ∈ [0.98, 1.02]; INCONCLUSIVE if `cap` found events or `sum(resets(fluentbit_uptime[1h] @ {E_unix})) > 0` |
    | `shape` | `no_ns / rows < 0.01` and `bad_nodes == 0` |
    | `exclusion` | both counts 0 |
    | `canary:<NAME>` | `total == EXPECTED` and `lines == EXPECTED` (exactly once) |

```kusto
// freshness
GalaxyLogs_CL
| where TimeGenerated > ago(30m)
| summarize rows = count(), p95_lag_s = percentile(datetime_diff('second', ingestion_time(), TimeGenerated), 95)
```

```kusto
// cap (Analytics query API)
_LogOperation
| where TimeGenerated between (datetime({S}) - 1h .. datetime({E}))
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
| summarize luma_ns = countif(Namespace startswith "luma-"),
    luma_traefik = countif(Namespace == "traefik"
        and Log matches regex @'"(web-|websecure-)?luma-[a-z0-9-]+@kubernetes(crd)?"')
```

```kusto
// canary:<NAME>
GalaxyLogs_CL
| where TimeGenerated > ago(3h)
| where Log has "{MARKER}"
| summarize n = count() by Log
| summarize lines = count(), total = sum(n)
```

- [ ] **Step 7: Commit, with Chris's sign-off, and watch.**

```bash
git add cluster/applications/fluent-bit.yaml scripts/test-fluent-bit-routing.sh \
  cluster/prometheus-stack/rules/logging-alerts.yaml scripts/test-logging-alerts.sh azure/logging/verify-ingestion.sh
nix develop -c git commit -m "logging: fluent-bit dual-ships to the Logs Ingestion API (GalaxyLogs_CL) beside the retired Data Collector output"
git push origin main
```

  - The DaemonSet rolls one node at a time. Watch it to 3/3 with the safe-rollout skill.
  - If the first new pod is not Ready within 5 minutes (a config error, a crash, or `CreateContainerConfigError`), **revert the commit and stop.**
    - fluent-bit 2.2.2 can segfault on exit after a failed init (fixed upstream). Read `kubectl logs --previous` for the `[error]` line, as a count of error classes only.
- [ ] **Step 8: First 10 minutes, per pod (counts only).**
  - `grep -c 'azure_logs_ingestion.*http_status=20[0-9]'` > 0.
  - `grep -cE 'azure_logs_ingestion.*http_status=(4|5)[0-9][0-9]'` = 0.
  - PromQL: `rate(fluentbit_output_proc_records_total{name="azure_logs_ingestion"}[5m]) > 0` for all three pods.
  - `kubectl top pods -n logging`: each under 160 Mi (the limit is 200 Mi).
- [ ] **Step 9: Canaries.**
  1. Create namespace `luma-azli-canary` (if it already exists, stop and ask).
  2. Start three `busybox:1.36.1` pods with `nodeName: bottom`:
     - `azli-canary-control` in `default` echoes `azli<hex>control <i>` 60 times, every 5 s, then sleeps;
     - `azli-canary` in `luma-azli-canary` does the same with its own marker;
     - `azli-burst` in `default`: `p=$(printf '%0900d' 0); i=0; while [ $i -lt 3000 ]; do echo "azli<hex>burst $i $p"; i=$((i+1)); done; sleep 86400`. That is about 2.9 MB in under a second, so the chunk is at the 2 MB limit.
  3. All three are Running, and `kubectl logs` shows each marker. This proves the pods log.
  4. After 20 minutes, the agent checks `bottom`'s fluent-bit pod: `grep -c 'http_status=413'` = 0.
  5. Chris runs `verify-ingestion.sh --canary control=<m>:60 --canary luma=<m>:0 --canary burst=<m>:3000`.
     - Expected: `VERIFY PASS`.
     - It may also be `VERIFY INCONCLUSIVE`, but only with `completeness` INCONCLUSIVE because of `cap`. The Checkpoint then re-runs it in a post-reset hour.
     - **If `burst` is short, or 413s appear, stop.** The 1 MB limit then binds on uncompressed size. Record it, and take open question 7 to Chris before the Checkpoint.
- [ ] **Step 10: Clean up.**
  - Delete the three pods, then namespace `luma-azli-canary`. This is pre-approved under S4; confirm first that only the canary was ever in it.
  - Record the results in Status.
  - **Rollback:** revert the commit. The old output never stopped.

### Checkpoint: verification window (at least 48 h) — **Chris go**

- It starts when Task 4's roll completes. No other change lands on `fluent-bit.yaml` during it.
- **Two runs of `verify-ingestion.sh` must both print `VERIFY PASS`:**
  - each one with `--window-end` set to the end of the first full hour after a daily cap reset (or any hour if `cap` passes);
  - at least 20 hours apart.
- **No `FluentBitAzureLogs*` alert fired:** `count_over_time(ALERTS{alertname=~"FluentBitAzureLogs.*"}[48h])` is empty.
- **Memory:** `max_over_time(container_memory_working_set_bytes{namespace="logging",container="fluent-bit"}[48h])` is under 160 Mi on every pod.
- **Volume:** Chris records the `Usage` daily GB for `GalaxyLogs_CL` (Task 1's query), and confirms the cap and budget values for Task 5 from that number. Recommendation: cap = 1.4 × the measured daily GB, rounded up to 0.5 GB.

### Task 5: Cutover — remove the old output, its Secret, and shared-key auth — **Chris approval**

**Files:**
- Modify `cluster/applications/fluent-bit.yaml`: remove the `Name azure` `[OUTPUT]`, and the env `AZURE_WORKSPACE_ID` and `AZURE_SHARED_KEY`.
- Delete `cluster/logging/azure-credentials.yaml` and `cluster/logging/azure-credentials.secret.yaml`, and modify `cluster/logging/kustomization.yaml`.
- In `azure/logging/`:
  - `main.tf`: set `local_authentication_disabled = true` on `azurerm_log_analytics_workspace.luma_homelab`, with a comment;
  - `outputs.tf`: remove `workspace_primary_shared_key` and `workspace_secondary_shared_key`;
  - `terraform.tfvars`: set `daily_quota_gb` and `monthly_budget_usd` to Chris's values;
  - `README.md`: rewrite it to cover the module's contents, `tf.sh`, the outputs, and a pointer to the runbook.
- Create `docs/logging.md`.
- Modify `CLAUDE.md`: add a Topology row.

**Interfaces:**
- Consumes the routing test, `verify-ingestion.sh` and `tf.sh`.
- Produces the final config: one Azure output, plus 0b's syslog.
- Produces `docs/logging.md`, with these sections:
  - Pipeline
  - What never leaves the house
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
- [ ] **Step 3: Terraform, with Chris's OK.**
  - Make the three edits above. The comment says: local auth is the shared-key path (the Data Collector API); disabling it makes the retired path fail closed, and AAD ingestion and queries are unaffected.
  - `./tf.sh plan` expected:
    - `azurerm_log_analytics_workspace.luma_homelab` updated in place (`local_authentication_disabled` false → true, plus `daily_quota_gb`);
    - the budget updated in place, if its amount changed;
    - 0 to add, 0 to destroy, and two outputs removed.
  - Then apply.
  - `az monitor log-analytics workspace show … --query "{local: features.disableLocalAuth, cap: workspaceCapping.dailyQuotaGb}"` → `true` and the new cap.
- [ ] **Step 4: One hour later, Chris checks:**
  - `luma_homelab_logs_CL | where ingestion_time() > datetime(<Step 1 roll end + 5m>) | count` = 0;
  - `verify-ingestion.sh` gives `VERIFY PASS`.
- [ ] **Step 5: Docs.**
  - **`docs/logging.md`:**
    - **Pipeline:** tail, then kubernetes, modify and 0b's filters, then syslog (0b) and `azure_logs_ingestion`. A diagram is not needed.
    - **What never leaves the house:** 0b's `Exclude_Path` and the Traefik grep.
    - **Tables and columns:** this plan's schema. `Log` holds the raw line; use `parse_json(Log)`.
    - **KQL cookbook:**
      - one pod's logs over a time range;
      - error lines by namespace;
      - the `Usage` query and the `OverQuota` query;
      - a reminder that Basic tables need an explicit time range, at most 30 days, and a search job beyond that.
    - **Cost, cap and budget:** the final numbers.
    - **Alerts:** the four rules, and what each means.
    - **Credentials and rotation:**
      - the end date (`terraform output -raw logs_ingestion_secret_end_date`);
      - the gap-free procedure: add `azuread_application_password.fluent_bit_next`, apply, change the seal script's output source, reseal, commit, `kubectl -n logging rollout restart ds/fluent-bit`, verify, then remove the old password resource and apply again.
    - **Verify:** the three scripts.
    - **Rollback.**
    - **The old table:** `luma_homelab_logs_CL` is read-only from the cutover, and fully aged out at cutover + 90 days.
  - **`CLAUDE.md` Topology:** a row "Logging". fluent-bit (namespace `logging`, DaemonSet) ships to router syslog (warnings and above, 0b) and to Log Analytics `GalaxyLogs_CL` through the Logs Ingestion API. The Azure side is terraform in `azure/logging/`. The runbook is `docs/logging.md`.
- [ ] **Step 6: Commit C.**

```bash
git add azure/logging/main.tf azure/logging/outputs.tf azure/logging/terraform.tfvars azure/logging/README.md docs/logging.md CLAUDE.md
nix develop -c git commit -m "azure/logging: disable shared-key ingestion after the Logs Ingestion cutover; logging runbook"
git push origin main
```

  - **Rollback:**
    - Before Step 3, revert A and B.
    - After Step 3, first set `local_authentication_disabled = false` and apply, then revert A and B. The old API may not answer at all by then, so fixing forward is the realistic path. The router keeps its syslog copy.

### Task 6: fluent-bit 5.1.3 and chart 0.58.3, pinned and visible to Renovate

**Files:**
- Modify `cluster/applications/fluent-bit.yaml`: `spec.source.targetRevision`, and `valuesObject.image`.
- Modify `.renovaterc.json`: one `customManagers` entry and two `packageRules`.

**Interfaces:**
- Consumes the routing test, 0b's exclusion test (if present) and `verify-ingestion.sh`.
- Produces two Renovate dependencies in group `fluent-bit`:
  - `fluent/fluent-bit`, from the docker datasource, tag plus digest;
  - `fluent-bit`, the chart, from the argocd manager.

- [ ] **Step 1: Failing check.**
  - With node from the dev shell, apply the matchStrings regex below to `cluster/applications/fluent-bit.yaml`.
  - Expected: no match, because there is no `digest:` line.
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
  - Comment:
    - CVE-2024-4323 on 2.2.2;
    - Renovate reads this block only through the customManager in `.renovaterc.json`;
    - never use an `x` range or drop the digest.
- [ ] **Step 5: Renovate config.**
  - Append to `customManagers`:

```json
{
  "customType": "regex",
  "description": "fluent-bit image in the Argo release (valuesObject image.tag + image.digest). The argocd manager never reads it, which is why it sat at 2.2.2 from 2024 to 2026.",
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
  - Re-run Step 1's regex check. Expected: `depName=fluent/fluent-bit currentValue=5.1.3 currentDigest=sha256:c554…`.
- [ ] **Step 7: Tests on the new image.** The routing test PASSes against 5.1.3. 0b's exclusion test also PASSes, if it exists.
- [ ] **Step 8: Commit and watch.**

```bash
git add cluster/applications/fluent-bit.yaml .renovaterc.json
nix develop -c git commit -m "logging: fluent-bit 5.1.3, digest-pinned on chart 0.58.3; Renovate sees the image (CVE-2024-4323)"
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

### Task 7: Exit verification and records

**Files:**
- Modify `docs/luma.md`: the "Azure log export" bullet gets the plan path and "done <date>".
- Modify `docs/superpowers/plans/2026-10-18-luma-0b-platform-prep.md`, **only if 0b Task 2 has not run yet**. Its Step 7 item 5 KQL becomes `GalaxyLogs_CL | where TimeGenerated > ago(1h) | where Log has '<marker>' | count`, with an explicit time range (it is a Basic table).
- Update the memory notes.

- [ ] **Step 1: Re-run everything.**
  - The routing test, `test-logging-alerts.sh`, and `verify-ingestion.sh` (Chris) all PASS.
  - `./azure/logging/tf.sh plan` shows `No changes.`
  - The Argo apps `fluent-bit` and `azure-log-analytics-secret` are Synced/Healthy.
- [ ] **Step 2: Records.**
  - Fill in this plan's Status.
  - Update `docs/luma.md`, and the 0b plan if the condition above holds.
  - Write a new memory note `project_azure_logs_ingestion.md`, plus its line in `MEMORY.md`. It covers:
    - the table and columns;
    - Basic, the cap and the cost;
    - the secret end date 2028-10-01, and that rotation means a reseal plus a restart;
    - the `/search` and 30-day query rules;
    - the old table's age-out date.
- [ ] **Step 3: Commit** the docs (named paths), and push.

## Rollback (summary)

| From | Do | Notes |
|---|---|---|
| Task 2 | revert; `tf.sh destroy -target` the seven resources (Chris OK) | nothing consumes them |
| Task 3 | revert | monitor and rule only |
| Task 4 / Checkpoint | revert | the old output never stopped |
| Task 5, before terraform | revert A and B | the old Secret comes back from git (still git-crypt) |
| Task 5, after terraform | `local_authentication_disabled = false`, apply, then revert A and B | the old API may be gone; prefer fixing forward |
| Task 6 | revert | emptyDir state, no migration |

## Follow-ups and out of scope (surface, don't do)

1. **kubernetes filter `Buffer_Size`:** 406 "cannot increase buffer" warnings in 14 days on `bottom`. Those records reach Azure with an empty `Namespace`, and the `shape` check tolerates under 1%.
2. **The dead `systemd` input:** Talos has no journald, and it read 0 records in 14 days. Remove it.
3. **The tail state is an emptyDir,** so every rollout loses offsets: seconds of logs per node. A hostPath state dir would keep them.
4. **The unused AMA DCR** `luma-homelab-logs-workspace-dcr` (Syslog/Event, no associations) in `azure/logging/main.tf` can be deleted.
5. **Phase 4 NetworkPolicies:** fluent-bit's egress needs `login.microsoftonline.com:443`, the DCE host (`*.westus3-1.ingest.monitor.azure.com:443`), the API server, and the router's UDP 514.
6. **Cap truncation is invisible in the cluster,** because the API answers with success. An Azure log-search alert on the `OverQuota` query, with an email action group, costs about $0.50–1.50 a month. That is Chris's call.
7. **The Argo app name `azure-log-analytics-secret`** is now a misnomer. Renaming it means delete and create, so leave it.
8. **0b follow-up 5 still stands:** Traefik access lines carry OAuth `code=` query strings for non-Luma apps into `GalaxyLogs_CL`, as they did into the old table.
9. **Delete `luma_homelab_logs_CL`** once it is empty, at cutover + 90 days, or keep it: an empty table costs nothing.

## Open questions (only Chris can answer)

1. **Table plan:** Basic (recommended, about $31 a month for complete logs), Analytics (about $120 a month complete), or Auxiliary (about $19, with the caveats in the cost table)?
2. **Steady-state daily cap and monthly budget:** the recommendation is 1.4 × the measured GB/day, so about 2.5 GB, and a budget of about $40. Today's budget is $10, against an estimated bill of about $63. Are budget alerts already arriving?
3. **Retention:** keep 90 days total (30 interactive + 60 long-term)?
4. **A pod `Labels` column:** it would add about 50% to the volume, roughly $13 a month. The plan drops it; `Pod`, `Container` and `Image` identify every workload.
5. **The image bump's place:** after the cutover (the default), or first, because of CVE-2024-4323 on a pod-network-reachable `:2020`?
6. **Pre-approvals at sign-off:** deleting the canary namespace `luma-azli-canary` (S4), and disabling local (shared-key) auth on the workspace in Task 5.
7. **Only if the burst canary fails:** accept occasional loss on 2 MB bursts (with `Flush 1` lowering the steady-state chunk size), or pursue request splitting in the plugin upstream?

## Self-review (2026-10-19)

- **Spec coverage:**
  - Logs Ingestion API with DCE + DCR + custom table + SP → Task 2.
  - Terraform preferred, since the repo manages Azure with it → Tasks 2 and 5; `az` is read-only and Chris runs `az login` → Global Constraints, Tasks 1, 2 and 5.
  - Secrets through `*.secret.yaml` and `sign.sh` → Task 2 Step 8, including the skip-if-sealed trap.
  - 0b's `Exclude_Path` and Traefik grep preserved → Global Constraints, P1, the routing test, and the KQL `exclusion` check.
  - Router syslog untouched → Global Constraints.
  - Dual-ship window, then removal → Task 4, Checkpoint, Task 5.
  - Cost and retention → its own section, Task 1, the Checkpoint, Task 5.
  - KQL that Chris runs → Task 1 Step 4, Task 4 Step 6, Task 5 Step 4.
  - Rollback → per task, plus the summary.
  - Renovate and pinning → Task 6.
  - Version support → baseline: 2.2.2 already has the plugin with the same options, and the bump is for the CVE and Renovate visibility.
- **Names checked across tasks:**
  - `GalaxyLogs_CL`, `Custom-GalaxyLogs_CL`, `galaxy-logs-dce`/`-dcr`;
  - Secret `azure-logs-ingestion` and its five keys, the `AZURE_LI_*` env;
  - `Alias azure_logs_ingestion` / `name="azure_logs_ingestion"`;
  - `logging-alerts` and the four rules;
  - the `logs_ingestion_*` outputs;
  - the scripts `test-fluent-bit-routing.sh`, `test-logging-alerts.sh`, `smoke-ingest.sh`, `seal-fluent-bit-secret.sh`, `verify-ingestion.sh`, `tf.sh`;
  - canary namespace `luma-azli-canary`.
- **Review Focus → tests:**
  1. → `cap`/`completeness`, and the Checkpoint's two windows.
  2. → the routing test exactly-once, and canary `lines == total`.
  3. → the burst canary and the 413 grep.
  4. → the smoke retry, the promtool cases and the seal post-condition.
  5. → the fixtures, the luma canary and `exclusion`.
- **Unverified, so tested rather than assumed:**
  - whether the 1 MB limit binds before or after gzip → the burst canary;
  - whether undeclared top-level properties are dropped or rejected → the smoke record mirrors the real shape, with the fallback in Task 2 Step 7;
  - whether `az monitor log-analytics query` reaches Basic tables → the script uses `/search` for them.
