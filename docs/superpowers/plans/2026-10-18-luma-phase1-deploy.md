# Luma phase 1 on galaxy: deploy and wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** draft v2, 2026-10-18. It is revised after the adversarial reviews, with Chris's decisions applied. Revised again for the final review round (2026-10-19; `docs/luma.md`):
- the `middle` exclusions are dropped;
- login takes Google and Microsoft;
- the Patroni REST fix moves to its own plan;
- the root-Spilo branch for WAL-G cron is written out.

Not approved, not executed.

**Goal:** Luma phase 1 ("Lean chat") runs in `luma-system` at `https://luma.chrismiller.xyz` for Chris only, which gives:
- its own SSD Postgres with PITR and a synchronous standby;
- Dex BFF login;
- an agentgateway holding the only Anthropic workspace keys;
- smokescreen `web` for WebFetch;
- nightly regression tests on `luma-ci`;
- a nightly reconcile against Anthropic's Usage & Cost API;
- alerts and a cost dashboard.

It then meets every phase-1 exit criterion.

**Architecture:**
- **Two Argo apps, following the repo's pair pattern** (`telephony-release` + `telephony`):
  - `luma-release` pins the published `luma` chart. It renders Core, the conductor, agentgateway, smokescreen `web`, the eval CronJobs and the usage-reconcile CronJob, and pins their images by digest.
  - `luma` (path `cluster/luma`) holds the site's pieces: the namespace, `acid-luma` and its WAL-G canary, SealedSecrets, the agents/models/evals ConfigMaps, the Ingress, the PodMonitor and the PrometheusRules (in namespace `prometheus`).
- **Elsewhere in this repo:** Dex gains one static client, the dashboard lives in `cluster/grafana`, and the runbooks and skills learn maintenance mode and `shed`.

**Tech Stack:** Argo CD, Helm chart `luma` from `https://christopherjmiller.github.io/luma`, Zalando postgres-operator v1.11 (Spilo 16) with WAL-G to B2, Sealed Secrets + git-crypt, Dex v2.46 (after phase 0a), Traefik v3, kube-prometheus-stack.

**Spec:** `docs/superpowers/specs/2026-10-18-luma-design.md`. This plan covers §14 phase 1 and draws on §5.2.5, §5.2.6, §5.2.8, §12.4, §13.2, §13.3, §13.4 and §17.
- **Product plan (the code):** `~/Repos/luma/docs/plans/phase-1-lean-chat.md`. **The seam is its contracts C6 (process env), C8 (chart values, with the "Rendered as" column) and C10 (internal endpoints).** Product task numbers below refer to that plan.
- **House style:** `docs/superpowers/plans/2026-10-15-patchbay-deploy.md`.
- **Chris's decisions** are recorded in `docs/luma.md` under "Decisions after spec approval".

## Global Constraints

**Safety rules (CLAUDE.md)**
- **S1:** read live state before every write: `kubectl get/describe`, Argo app status, VyOS `show`.
- **S2:** everything goes through git. These are **the only exceptions**:
  1. `kubectl rollout restart deploy/dex` (Task 5);
  2. the hand-applied live-spike Job (Task 3), deleted after;
  3. the restore-drill CR `acid-drill` (Task 12), deleted after;
  4. throwaway `netshoot` probe pods (`kubectl run --rm`);
  5. one-off Jobs created from CronJobs with `kubectl create job --from=cronjob/…`: evals (nightly, gate, guard), `luma-usage-reconcile` and `walg-canary-acid-luma`. Each has `ttlSecondsAfterFinished`;
  6. the rc eval-gate Job rendered with `helm template --show-only … | kubectl apply` ("Upgrading Luma");
  7. `kubectl exec` of `/luma-core maintenance`, read-only `psql`/`patronictl`/`wal-g`, `pg_switch_wal()` and the first manual base backup;
  8. the one drill marker row (Task 12).
- **S3:** use `Monitor` for anything over 60 s.
- **S4:** confirm with Chris before deleting a PVC, PV, Namespace or StatefulSet, and before an Argo app delete.
- **S5:** no Luma rollout during a drain or Talos hop. **Every drain sheds Luma first** (H11).
- **S6:** never `--no-verify`. **S7:** Ceph is look-only.
- **S9:** never print, paste or commit-message a secret.
  - Secrets are described **by file, Secret name and key only**.
  - Use `git diff --no-textconv` / `git show --no-textconv` for anything that leaves the terminal.
  - **Only Chris enters Anthropic and B2 key values.** He pastes them in `$EDITOR` over named placeholders, in the files this plan names. They never pass through chat, a shell variable the agent sets, or a temp file.
  - **Generated values** (internal tokens, OIDC client secret, signing key) are produced and written **in one `bash -c 'set -euo pipefail; …'` shell**:
    - from `head -c32 /dev/urandom` (`od -An -tx1` for hex, `base64 -w0` for base64), because **`openssl` is not on rowlett's PATH or in `nix develop`**;
    - with `: "${X:?}"` and length guards;
    - printing only key names, lengths, booleans or sha256 hashes.

**Images (sharp edge #6)**
- The chart release pins the Luma images by digest, so this repo pins **only the chart version**.
- A Renovate chart bump **is** a deploy. It never automerges, and it waits for the eval gate (Task 10).

**Fixed values (spec and Chris)**
- **Namespace:** `luma-system`.
- **Postgres:** `acid-luma`.
  - Placement: `rook-ceph-block-ssd`, with **the two members on two different nodes**, any of the three (H10). No node is excluded. **memtest not run; Chris accepted the RAM risk 2026-10-19 (see docs/luma.md).**
  - Durability: `synchronous_commit=on`, plus Patroni **`synchronous_mode: true` (non-strict)**.
  - Backups: WAL-G to B2 with a **dedicated key** (`namePrefix spilo/acid-luma/`), the nightly logical dump, and a restore drill into `acid-drill`.
- **Anthropic** *(Chris)*:
  - workspaces `luma-interactive` (**$150**) and `luma-ci` (**$20**), plus an org **Admin key** for the reconcile;
  - workspace keys live only in the gateway's Secret, and the Admin key only in the reconcile CronJob's;
  - Core's global cap is **$150** (`core.budget.monthlyUsd`).
- **agentgateway:** **2 replicas + PDB**, ClusterIP only, and a separate bearer per route (`GATEWAY_TOKEN_INTERACTIVE`, `GATEWAY_TOKEN_CI`).
- **Monitoring:** ServiceMonitors and PodMonitors carry `release: prometheus`.
  - PrometheusRules live in namespace `prometheus`, labelled `release: prometheus` (0b's convention), plus the legacy `prometheus: kube-prometheus` and `role: alert-rules`.
  - The deploy check is 0b's `scripts/check-prometheus-rules.sh`.
- **smokescreen `web` deny list:**
  - our domains, each apex and `*.`;
  - **`discord.com` / `discordapp.com`** *(Chris)*;
  - the **WAN IP as a `/32` range**;
  - the chart defaults: RFC1918, CGNAT, link-local, IPv6 ULA/link-local/mapped, NAT64, 6to4, Teredo, and the curated `exfil-deny` list. Paste and file-drop **reads** are blocked too, which Chris accepts.
- **Upstream headers:** `x-luma-*` carry **opaque ids only** (`u_<10 hex>`; product P21) *(Chris)*.
- **Priorities** *(Chris)*: Core, conductor, gateway and smokescreen at **0**; evals and reconcile at `luma-batch` **−5**.
- **Ingress:** `luma.chrismiller.xyz`, one Ingress with a host and **no oauth2-proxy middleware** (D39). It has no `spec.tls` and no DNS/cert annotations. Its only annotation turns Traefik access logs off for the router.
- **Logs:** fluent-bit excludes `luma-*` and drops Traefik access-log lines for `luma-*` routers (both phase 0b; verified here).
- **Repo:** the luma repo is public; nothing from this repo's secrets reaches it.
- **Expected cost:** about **$8–20/month** for Chris's chat, plus about **$3–5/month** of evals on `luma-ci`.

## Review Focus

These are the five failure modes most likely to bite, which no step below would otherwise catch. Each has its check in the named task.

1. **The WAN IP changes (DHCP)** and the smokescreen `/32` goes stale. A model-chosen fetch to `http(s)://<new WAN IP>/` then hairpins through NAT reflection rules 210/310 into Traefik. It is pinned by `scripts/check-wan-ip.sh` plus a pre-commit hook comparing it with `router/ansible/group_vars/vyos_routers.yml` (Task 7). Task 7 tests ports 443 and 80, and the runbook covers a live `dig` check.
2. **`sign.sh` silently skipping a file that already has a sealed sibling.** The Dex client never reaches the cluster, and login fails with "Unregistered client" while git looks right. Task 5 deletes `cluster/dex/config.yaml` before sealing, waits for the Secret's `resourceVersion` to change, compares live and git config by hash, and probes `/token`.
3. **WAL-G failing quietly** (the dedicated key's scope, the path, or cron not running in a uid-101 Spilo) until `pg_wal` fills the 10 Gi SSD volume and Postgres PANICs. Task 4 verifies the following, and Task 9 routes `failed_count`, WAL size and the canary to **critical** (never weekday-muted):
   - a pushed segment;
   - the envdir;
   - cron;
   - a manual base backup, plus a **second, cron-made one the next day**;
   - a daily `wal-g backup-list` canary that fails when the newest backup is over 36 h old.
4. **Monitoring objects that are inert:** a monitor without `release: prometheus`, or a rule outside `prometheus` (known: ruleSelector, servicemonitor release label). Task 9 checks `/api/v1/targets` and `/api/v1/rules`, not the YAML.
5. **The conductor's 2 GiB request landing on a node without headroom.** It would sit Pending, or squeeze BestEffort household pods. 0b's exit gives at least two nodes ≥ 4096 Mi of free requests and every node ≥ 2048 Mi, counting all three nodes. Task 7 re-reads per-node allocation before syncing: Core + conductor + an `acid-luma` member + the nightly eval Job on one node. No Luma pod has a node preference.

## 0. Preconditions (each is a go/no-go, and gates only the tasks named)

| # | Check | State 2026-10-18 | Needed | Gates |
|---|---|---|---|---|
| P1 | Ceph at its usual WARN noise | PG 1.c repaired 2026-10-19 UTC | ceph-doctor run clean (S7) | 4, 7, 12 |
| P2 | Phase 0a done | Dex `v2.44.0` live | Dex v2.46.0 with `DEX_CONFIG_DISALLOW_UNKNOWN_FIELDS=true`, sessions on, `skipApprovalScreen: false` (0a Tasks 4, 7) | 5 |
| P3 | Phase 0b done (`docs/superpowers/plans/2026-10-18-luma-0b-platform-prep.md`) | requests at 89–91 % per node | All of 0b's exit outputs (referenced here, not re-specified): (a) the per-node gate over all three nodes, **at least two nodes ≥ 4096 Mi of free memory requests and every node ≥ 2048 Mi**; (b) the live fluent-bit `luma-*` input exclusion **and** its grep filter dropping Traefik access-log lines for `luma-*` routers; (c) `scripts/check-prometheus-rules.sh` PASS; (d) 0b's canary namespace **`luma-0b-canary` is gone**. | (b, d) 2, 3; (a) 4, 7; (c) 9 |
| P4 | A luma chart release exists | none (product Task 29) | `v0.1.0-rc.1` with `ci-ok` green. `helm search repo luma --devel` finds it, and `gh attestation verify` passes for the three image digests in its `values.yaml` | **6, 7 only** |
| P5 | Product Spike G is recorded | not started (product Task 3) | `~/Repos/luma/test/gateway/agentgateway.yaml` committed and `docs/spikes/phase-1.md` §G written | 3 |
| P6 | Namespace `luma-system` **has never existed** | absent | `kubectl get ns luma-system` → NotFound, and no `luma-system` Argo app or rule in history (0b no longer uses it) | 2 |
| P7 | Argo otherwise healthy | (re-check) | no app OutOfSync or Degraded beyond the known `rook`/`rook-cluster` baseline | every push |
| P8 | No IPv6 on the WAN | no IPv6 config in `router/ansible` | `ssh chris@192.168.0.1 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show interfaces'` shows no global IPv6 on eth0. If one exists, add its prefix to `egress.web.extraDenyRanges`. | 7 |
| P9 | Traefik ≥ v3.3.0 (router `observability` annotations) | chart 41.x | `kubectl -n traefik get deploy traefik -o jsonpath='{.spec.template.spec.containers[0].image}'` tag ≥ `v3.3.0` | 8 (the annotation only) |

**Cross-plan order:**
1. Tasks 1–3 run after product **Task 3** (P5) and in parallel with product stages 1B–1D.
2. Task 3's results go back into the product plan **before its Task 26b**.
3. Tasks 4–5 need only P1–P3.
4. Tasks 6–7 wait for P4.

## Decisions this plan makes

| # | Decision | Why |
|---|---|---|
| H1 | `luma-system` enforces PSS **`baseline`**, with `warn`/`audit` at `restricted`. Luma's own pods are restricted-compliant (product chart test). | Spilo pods from the operator do not set the restricted fields; `acid-gramps`' namespace is `baseline` for the same reason. |
| H2 | WAL-G config is per cluster, through `spec.env`: `WAL_BUCKET_SCOPE_PREFIX=""` and `WAL_BUCKET_SCOPE_SUFFIX=/wal-g-1`, so the path is `s3://galaxy-cluster-backups/spilo/acid-luma/wal-g-1/`. Bump the suffix for any in-place rebuild. **The credentials are a dedicated B2 key** (`galaxy-acid-luma`: bucket `galaxy-cluster-backups`, `namePrefix spilo/acid-luma/`, no `deleteFiles`), stored as luma-system's `logical-backup-s3`. That Secret is the one the operator's logical backup, WAL-G, the canary and the drill all read. | A luma-system compromise can then read and write only Luma's own backups, not every cluster's dumps. The operator adds `/<uid>` only for operator-wide buckets; the `spilo/` lifecycle (hidden at 30 days) gives the spec's "≤ 30 days". |
| H3 | `archive_timeout: 300s` | 5-minute RPO on a quiet DB. |
| H4 | `BACKUP_SCHEDULE: "15 5 * * *"`, `BACKUP_NUM_TO_RETAIN: "30"`, **full base backups only** (no `WALG_DELTA_MAX_STEPS`), and `WALG_PREVENT_WAL_OVERWRITE=true`. A daily canary fails when the newest base backup is over 36 h old. | The lifecycle hides `spilo/` objects at 30 days. A delta chain, or a backup cron that silently stopped, would leave nothing restorable. |
| H5 | The `exfil-deny` list is the chart default. Our domains, Discord and the WAN `/32` are site values in `luma-release.yaml`. The ACL and config are chart-rendered, so a change rolls smokescreen. | A generic list belongs to the product; house names belong here. |
| H6 | The live halves of the §17 spikes run **before** the stack: a hand-applied Job (Task 3) with the pinned agentgateway as a native sidecar (`ci` route only) and a curl probe, on `luma-ci`. | They cost cents, and they feed the product plan before its chart is final. |
| H7 | The registry holds Chris only, with **two connector/email pairs**: his Google address under connector `google`, and his Microsoft address under connector `microsoft`. The Dex client has **`allowedConnectors: [google, microsoft]`** *(Chris, 2026-10-19)*. **The connector stays pinned:** an address is accepted only when it arrives through the connector it is registered under (product `Registry.Lookup(connectorID, email)`). The registry is a SealedSecret, because the repo is public. | §8.2 pilot. The key is `json:"allowedConnectors"` in Dex v2.46 `storage.Client`, verified. Both connector ids exist in today's Dex config (`google`, `microsoft`). |
| H8 | `x-luma-*` headers reach Anthropic carrying an **opaque person ref** and product enums (agent, tier, source), never a name. Unknown values are labelled `other` at the gateway. | *(Chris)* |
| H9 | Patroni `synchronous_mode: true`, `synchronous_mode_strict: false`, `max_slot_wal_keep_size: 4GB`. | With `synchronous_commit=on`, an acknowledged write is on two nodes. Non-strict keeps the primary writable when the standby is down, and the slot cap stops a dead standby filling `pg_wal`. |
| H10 | **Placement:** no node is excluded, and no Luma pod has a node preference. `acid-luma`'s two members should sit on **two different nodes**. Phase 1 gets that softly, and verifies it (Task 4 Step 6):<br>- The operator's v1.11 `postgresql` CR has no per-cluster pod anti-affinity. Its `enable_pod_antiaffinity` is operator-wide, and turning it on would roll every `acid-*` cluster, so it is not used.<br>- If the CRD at execution has a `topologySpreadConstraints` field, the soft spread goes there (Task 4 Step 2).<br>- Otherwise the scheduler's default soft spreading applies, and the placement is verified. | Chris assumed middle's memtest is fine (2026-10-19). memtest not run; Chris accepted the RAM risk 2026-10-19 (see docs/luma.md). A sync standby on the primary's node would share its failure domain. |
| H11 | **Shedding is a commit.** Before any drain: maintenance on → wait for 0 running turns → commit `shed: true` in `luma-release` → drain. After: `shed: false` → Ready → maintenance off. | selfHeal reverts a hand scale-down, and a drain cannot place 3 GiB of Luma requests (§14 capacity). |
| H12 | **Rollback without resources-finalizers.** The Namespace and the `postgresql` CR carry `argocd.argoproj.io/sync-options: Prune=false,Delete=false`. A rollback first empties the workloads (`enabled: false` plus an emptied `cluster/luma`), then removes the apps. | No app here has `resources-finalizer.argocd.argoproj.io`, so removing an Application orphans its resources. Data must survive by design. |
| H13 | The **Admin key** lives in its own SealedSecret `luma-anthropic-admin`, mounted only by CronJob `luma-usage-reconcile` (product P22). | *(Chris)* An Admin key can manage the org; Core stays without it. |

## File map

| File | New? | What |
|---|---|---|
| `cluster/applications/luma.yaml` | new | Argo app: path `cluster/luma` → `luma-system`, automated, prune, selfHeal |
| `cluster/applications/luma-release.yaml` | new | Argo app: chart `luma`, `targetRevision` pinned, `valuesObject` (Task 7) |
| `cluster/applications/kustomization.yaml` | edit | + both apps |
| `cluster/luma/kustomization.yaml` | new | **no `namespace:` transformer** (the rules go to `prometheus`); `configMapGenerator` `luma-config`, `luma-evals` (`disableNameSuffixHash: true`) |
| `cluster/luma/namespace.yaml` | new | H1, H12 |
| `cluster/luma/psql.yaml` | new | `acid-luma` (Task 4), H12 |
| `cluster/luma/postgres-podmonitor.yaml` | new | copy of `cluster/home-assistant/postgres-podmonitor.yaml`, selector `galaxy: acid-luma` |
| `cluster/luma/walg-canary-cronjob.yaml` | new | daily `wal-g backup-list` age check (H4) |
| `cluster/luma/logical-backup-s3.{secret.yaml,yaml}` | new | Secret `logical-backup-s3`: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (the dedicated key, **Chris pastes**) |
| `cluster/luma/anthropic.{secret.yaml,yaml}` | new | Secret `luma-anthropic`: `ANTHROPIC_KEY_INTERACTIVE`, `ANTHROPIC_KEY_CI` (**Chris pastes**) |
| `cluster/luma/anthropic-admin.{secret.yaml,yaml}` | new | Secret `luma-anthropic-admin`: `ANTHROPIC_ADMIN_KEY` (**Chris pastes**) |
| `cluster/luma/internal.{secret.yaml,yaml}` | new | Secret `luma-internal`: `GATEWAY_TOKEN_INTERACTIVE`, `GATEWAY_TOKEN_CI` (generated) |
| `cluster/luma/core.{secret.yaml,yaml}` | new | Secret `luma-core`: `OIDC_CLIENT_SECRET`, `SESSION_SIGNING_KEY` (generated) |
| `cluster/luma/registry.{secret.yaml,yaml}` | new | Secret `luma-registry`: `registry.yaml` (H7; Chris fills his Google and Microsoft addresses) |
| `cluster/luma/agents/{luma,deep-think}/AGENT.md`, `cluster/luma/agents/*/evals/*.yaml`, `cluster/luma/models.yaml` | new | copied from the luma repo at the pinned tag, plus site branches |
| `cluster/luma/ingress.yaml` | new | Task 8 |
| `cluster/luma/prometheusrule.yaml` | new | `metadata.namespace: prometheus` (Task 9) |
| `cluster/grafana/dashboards/luma.yaml` (+ `cluster/grafana/kustomization.yaml`) | new / edit | Task 9 |
| `cluster/dex/config.secret.yaml` (+ resealed `config.yaml`) | edit | static client `luma-core` (Task 5) |
| `docs/luma/spike-live-job.yaml`, `docs/luma/spike-live.sh` | new | hand-applied, never Argo (Task 3) |
| `docs/backups/acid-drill-luma.yaml` | new | the PITR clone drill, hand-applied (Task 12) |
| `scripts/check-wan-ip.sh`, `.pre-commit-config.yaml` | new / edit | Review Focus 1 (Task 7) |
| `.claude/skills/edge-ingress/edge-snapshot.sh` | edit | add `luma` |
| `.claude/skills/talos-upgrade/SKILL.md`, `.claude/skills/ceph-ssd-pool/SKILL.md` | edit | maintenance + `shed` around node work (H11) |
| `.renovaterc.json` | edit | the `luma` chart never automerges |
| `docs/luma.md`, `docs/backups.md`, `CLAUDE.md` | edit | Task 11 |

There is no `luma-vault` Secret in phase 1. The connections vault key arrives with the vault, in phase 3.

---

### Task 1: Anthropic and B2 credentials (manual: Chris, in the consoles)

**Files:** none in git.

**Interfaces:**
- Produces, **in Chris's password manager only** (no file on rowlett, nothing in chat):
  - API key `luma-gateway-interactive` (workspace `luma-interactive`);
  - API key `luma-gateway-ci` (workspace `luma-ci`);
  - org Admin key `luma-usage-reconcile`;
  - B2 application key `galaxy-acid-luma`.

  Tasks 2 and 4 name each one by these labels.

- [ ] **Step 1 (Chris): create the workspaces.** In console.anthropic.com → Workspaces, create `luma-interactive` with a **monthly spend limit of $150** and `luma-ci` with **$20**.
- [ ] **Step 2 (Chris): create one API key per workspace**, named `luma-gateway-interactive` and `luma-gateway-ci`, and save each in the password manager.
- [ ] **Step 3 (Chris): create the org Admin key** `luma-usage-reconcile` (Console → Settings → Admin keys; this needs the org admin role), and save it in the password manager. It is mounted by one CronJob only (H13).
- [ ] **Step 4 (Chris): create the B2 key** `galaxy-acid-luma`:
  - bucket `galaxy-cluster-backups`, name prefix `spilo/acid-luma/`;
  - capabilities `listBuckets,listFiles,readFiles,writeFiles`, **without `deleteFiles`**;
  - either in the B2 web UI, or `nix develop --command backblaze-b2 key create --bucket galaxy-cluster-backups --name-prefix spilo/acid-luma/ galaxy-acid-luma listBuckets,listFiles,readFiles,writeFiles` in Chris's own terminal.

  Its output shows the secret once; it goes into the password manager.
- [ ] **Step 5 (Chris): verify.** The Console shows the limits as $150 and $20, and both key names under the right workspaces. B2 lists `galaxy-acid-luma` with the prefix. Nothing was pasted into chat.

### Task 2: Namespace, the Anthropic and internal Secrets, and Argo app `luma`

**Files:**
- Create: `cluster/luma/{kustomization.yaml,namespace.yaml,anthropic.secret.yaml,anthropic.yaml,anthropic-admin.secret.yaml,anthropic-admin.yaml,internal.secret.yaml,internal.yaml}`, `cluster/applications/luma.yaml`
- Modify: `cluster/applications/kustomization.yaml`

**Interfaces:**
- Consumes: Task 1's keys, which Chris pastes.
- Produces (names and keys exactly as product C8):

  | Secret | Keys | Values from |
  |---|---|---|
  | `luma-anthropic` | `ANTHROPIC_KEY_INTERACTIVE`, `ANTHROPIC_KEY_CI` | Chris pastes `luma-gateway-interactive` and `luma-gateway-ci` |
  | `luma-anthropic-admin` | `ANTHROPIC_ADMIN_KEY` | Chris pastes `luma-usage-reconcile` |
  | `luma-internal` | `GATEWAY_TOKEN_INTERACTIVE`, `GATEWAY_TOKEN_CI` | generated: 64 hex chars each, distinct |

  Also the Argo app `luma`. Later tasks only add files to `cluster/luma/`.

- [ ] **Step 1 (S1, P3 b/d, P6): read the current state.**
  - `kubectl get ns luma-system luma-0b-canary`: both NotFound.
  - `kubectl -n argo-cd get applications.argoproj.io -o wide`: the P7 baseline.
  - `kubectl -n logging get cm -o yaml | grep -c -e 'Exclude_Path /var/log/containers/\*_luma-\*_\*.log' -e 'luma-\[a-z0-9-\]+@kubernetes'`: ≥ 2. This is 0b's input exclusion plus its Traefik grep filter, the outputs of 0b Task 2.
- [ ] **Step 2: Write the namespace and the app.**
  - `namespace.yaml`: `luma-system`, labelled `pod-security.kubernetes.io/enforce: baseline`, `warn: restricted` and `audit: restricted`, and **annotated `argocd.argoproj.io/sync-options: Prune=false,Delete=false`** (H12).
  - `cluster/applications/luma.yaml`: `mcp-gateway.yaml`'s shape, with path `cluster/luma`, destination `luma-system`, and `syncOptions: [CreateNamespace=true, PrunePropagationPolicy=foreground, PruneLast=true]`.
  - `kustomization.yaml` gets the comment "No `namespace:` transformer on purpose: prometheusrule.yaml lands in `prometheus`".
- [ ] **Step 3: Write the two key files with placeholders only** (heredocs; no secret passes through the agent):
  ```bash
  cat > cluster/luma/anthropic.secret.yaml <<'EOF'
  apiVersion: v1
  kind: Secret
  metadata: {name: luma-anthropic, namespace: luma-system}
  stringData:
    ANTHROPIC_KEY_INTERACTIVE: __PASTE_luma-gateway-interactive__
    ANTHROPIC_KEY_CI: __PASTE_luma-gateway-ci__
  EOF
  cat > cluster/luma/anthropic-admin.secret.yaml <<'EOF'
  apiVersion: v1
  kind: Secret
  metadata: {name: luma-anthropic-admin, namespace: luma-system}
  stringData:
    ANTHROPIC_ADMIN_KEY: __PASTE_luma-usage-reconcile__
  EOF
  ```
- [ ] **Step 4 (Chris only): paste the keys.** Open each file in `$EDITOR`, replace each `__PASTE_<label>__` with the key of that label from the password manager, and save. From here the agent never opens, `cat`s or diffs these two files.
- [ ] **Step 5: Verify without printing a value.**
  ```bash
  ! grep -l "__PASTE_" cluster/luma/anthropic.secret.yaml cluster/luma/anthropic-admin.secret.yaml
  yq '.stringData | to_entries | .[] | .key + " len=" + (.value | length | tostring) + " shape_ok=" + (.value | test("^sk-ant-api[0-9]+-[A-Za-z0-9_-]+$") | tostring)' cluster/luma/anthropic.secret.yaml
  yq '.stringData | to_entries | .[] | .key + " len=" + (.value | length | tostring) + " shape_ok=" + (.value | test("^sk-ant-admin[0-9]+-[A-Za-z0-9_-]+$") | tostring)' cluster/luma/anthropic-admin.secret.yaml
  ```
  Expected:
  - the `grep` lists no file;
  - three lines with `len` > 40 and `shape_ok=true`. That shape rejects whitespace, a pasted newline or quote, and a key of the wrong kind.

  Anything else means Chris re-checks his paste. No value is printed.
- [ ] **Step 6: Generate the internal tokens, in one shell.**
  ```bash
  bash -c 'set -euo pipefail
  rnd() { head -c32 /dev/urandom | od -An -tx1 | tr -d " \n"; }
  GTI=$(rnd); GTC=$(rnd)
  : "${GTI:?}" "${GTC:?}"; [ ${#GTI} -eq 64 ]; [ ${#GTC} -eq 64 ]; [ "$GTI" != "$GTC" ]
  export GTI GTC
  yq -n ".apiVersion=\"v1\" | .kind=\"Secret\" | .metadata.name=\"luma-internal\" | .metadata.namespace=\"luma-system\"
         | .stringData.GATEWAY_TOKEN_INTERACTIVE=strenv(GTI) | .stringData.GATEWAY_TOKEN_CI=strenv(GTC)" > cluster/luma/internal.secret.yaml
  yq ".stringData | to_entries | .[] | .key + \" len=\" + (.value|length|tostring)" cluster/luma/internal.secret.yaml'
  ```
  Expected: two lines, each `len=64`. The variables die with the shell.
- [ ] **Step 7: Seal.** Run `KUBECONFIG=/tmp/galaxy-kubeconfig nix develop --command ./sign.sh`.
  - Expected: it prints the three `cluster/luma/*.secret.yaml` paths and no `FAIL`, and `cluster/luma/{anthropic,anthropic-admin,internal}.yaml` exist with `kind: SealedSecret`.
  - Then `git add` each pair, and check `git show :cluster/luma/anthropic.secret.yaml | head -c 10 | od -c` (and the other two). Each must show `\0GITCRYPT`.
  - No intermediate key file exists in this flow. If Chris staged a key anywhere else, he runs `shred -u` on it.
- [ ] **Step 8: Gate on pre-commit.** Run `nix develop --command pre-commit run --files cluster/luma/* cluster/applications/*`. Expected: PASS, including `kustomize-build`.
- [ ] **Step 9: Commit and push to main.**
  ```bash
  git add cluster/luma/kustomization.yaml cluster/luma/namespace.yaml cluster/luma/anthropic.secret.yaml cluster/luma/anthropic.yaml \
          cluster/luma/anthropic-admin.secret.yaml cluster/luma/anthropic-admin.yaml cluster/luma/internal.secret.yaml cluster/luma/internal.yaml \
          cluster/applications/luma.yaml cluster/applications/kustomization.yaml
  git commit -m "luma: luma-system namespace, gateway key, admin key and internal-token SealedSecrets"
  git push
  ```
- [ ] **Step 10: Verify** with `Monitor` until done.
  - `kubectl -n argo-cd get app luma` reaches Synced/Healthy.
  - Then `kubectl -n luma-system get secret luma-anthropic luma-anthropic-admin luma-internal -o json | jq -r '.items[] | .metadata.name + ": " + (.data | keys | join(","))'`. Expected: the key names in the table. **Never `.data` values.**

### Task 3: Live spikes, the real-API halves of §17 (H6)

**Files:**
- Create: `docs/luma/spike-live-job.yaml`, `docs/luma/spike-live.sh`
- Modify (luma repo): `docs/spikes/phase-1.md` §L

**Interfaces:**
- Consumes:
  - P5's `test/gateway/agentgateway.yaml`, as ConfigMap `luma-spike-gateway`: `yq 'del(.routes[] | select(.name == "interactive"))'`, with the fake `hostOverride` removed;
  - `luma-anthropic` key `ANTHROPIC_KEY_CI` and `luma-internal` key `GATEWAY_TOKEN_CI` only.
- Produces: five recorded results with their branches applied. **Every line prints names and numbers only**, except L5's API error string, which is not content.

**The Job** (`ttlSecondsAfterFinished: 600`, `activeDeadlineSeconds: 900`, `backoffLimit: 0`, `restartPolicy: Never`, `automountServiceAccountToken: false`) runs one pod:
- **`gateway`, a native sidecar** (`initContainers` with `restartPolicy: Always`). Its image is luma `Makefile`'s `AGENTGATEWAY_IMAGE`. It mounts `ANTHROPIC_KEY_CI` as the file `/run/secrets/anthropic/ci` and takes `GATEWAY_TOKEN_CI` from `secretKeyRef`.
- **`probe`:** `nicolaka/netshoot` at its newest `vX.Y` tag at least two weeks old, pinned by digest (`nix shell nixpkgs#crane -c crane digest nicolaka/netshoot:<tag>`). It runs with an **explicit `runAsUser: 65532`**, `runAsNonRoot: true`, every capability dropped and RuntimeDefault seccomp, and runs `spike-live.sh` against `http://127.0.0.1:8080/ci`.

The pod finishes when the probe exits. The script builds a synthetic system prompt of about 6,000 tokens of generated filler, and uses streamed `claude-haiku-5-5` requests.

- [ ] **Step 1: Write the Job and script.** Each line is `SPIKE <id> PASS|FAIL <numbers>`.
  - **L1 `ttl_1h`:** the request carries `cache_control: {type: ephemeral, ttl: "1h"}`, `max_tokens: 16`. PASS if `message_start.message.usage.cache_creation.ephemeral_1h_input_tokens > 0`.
  - **L2 `identical_read`:** the same body sent again. PASS if `cache_read_input_tokens > 0`.
  - **L3 `detect_usage`** (informational: **Spike G alone decides the route type**): curl `127.0.0.1:15020/metrics` and print which `gen_ai_token_type` values appeared.
  - **L4 `effort`:** the base is effort `low`, the 6K system prompt, and a synthetic history over 5K tokens with the 1h breakpoint on the last user message. Warm it twice. Then send one new user question that invites reasoning (`max_tokens: 1024`), in three arms:
    - `control_low`: unchanged effort;
    - `arm_system`: an appended `{"role":"system","content":[],"output_config":{"effort":"medium"}}` with `anthropic-beta: mid-conversation-output-config-2026-07-01`;
    - `arm_toplevel`: the top-level `output_config.effort` changed to `medium`.

    For each arm, print `cache_read_input_tokens` and output tokens, including any thinking tokens reported. PASS for an arm if `cache_read ≥ 0.9 ×` the warm prefix **and** its output tokens exceed `control_low`'s.
  - **L5 `thinking_binding`:**
    1. **Capture:** a question that elicits brief thinking (`max_tokens: 512`). FAIL if the reply has no `thinking` block with a non-empty `signature`.
    2. **Honest replay** `[user, assistant(verbatim), user "ok"]`, with `anthropic-beta: thinking-binding-controls-2026-08-01`, `thinking.block_binding.prefix_mismatch_behavior: "error"`, `tool_choice: {type: "none"}` and `max_tokens: 64`. PASS if 200 and `input_transformations` is absent or empty.
    3. **Edited replay** (one character of the first user message changed). PASS if 400. Print the error's `type` and its `message`.
    4. **`count_tokens` pre-pass** of the edited replay with the same beta: print the status.
- [ ] **Step 2: Run it.**
  - `kubectl apply -f docs/luma/spike-live-job.yaml`, then `Monitor` on `kubectl -n luma-system logs -f job/luma-spike-live -c probe` until it exits.
  - Then `kubectl delete -f docs/luma/spike-live-job.yaml` (the TTL is the backstop).
  - Expected: five `SPIKE` groups, at a cost under $0.10 on `luma-ci`.
- [ ] **Step 3: Record and branch.** Write the results into luma `docs/spikes/phase-1.md` §L, and apply each branch in the luma repo **before its Task 26b**.

  | Result | Branch |
  |---|---|
  | L1 FAIL | Check Spike G's `TestHeadersForwarded` for the TTL beta. If the gateway is at fault, use product Task 3b. Otherwise accept 5m (`cache_ttl: 5m` in `models.yaml`, Task 6) and record the cost-model change in `docs/luma.md`. |
  | L2 FAIL | **Stop.** A key or gateway misconfiguration: nothing in phase 1 works without cache reads. |
  | L3 | Record only. Core is the SLO source (product P10). |
  | L4 (any arm) | Phase 1 ships no effort control either way (product P28). Record which arm, if any, preserved the cache and raised output, next to product Spike S15's note of what `applyFlagSettings` sends. This is phase 2's input. |
  | L5 capture or honest replay FAIL | **Stop.** The nightly replay design is wrong. Fix product Task 27 before its release. |
  | L5 edited replay not 400 | Record it. The nightly replay then cannot see drops, and the gateway's main-class 400 alert (Task 9) is the only detector; note it in `docs/luma.md`. |
  | L5 error message | Copy it verbatim into product fakeanthropic `BindingErrorText`, and into this plan's `LumaThinkingBinding` annotation. |
  | L5 `count_tokens` not 400 on the edited replay | Product Task 27 drops the free pre-pass and keeps only the paid replay. |
- [ ] **Step 4: Commit** the Job and script here (`git add docs/luma/ && git commit -m "luma: live spike job (TTL, cache read, detect usage, effort arms, thinking binding)"`), and the spike record in the luma repo.

### Task 4: `acid-luma`, Postgres with a sync standby, WAL-G PITR, a canary and a logical backup

**Files:**
- Create: `cluster/luma/{psql.yaml,postgres-podmonitor.yaml,walg-canary-cronjob.yaml,logical-backup-s3.secret.yaml,logical-backup-s3.yaml}`
- Modify: `cluster/luma/kustomization.yaml`

**Interfaces:**
- Consumes: Task 1's `galaxy-acid-luma` B2 key, which Chris pastes.
- Produces:
  - Service `acid-luma` (the primary), database `luma` owned by `luma_core`, and role `luma_conductor`.
  - Credential Secrets `luma-core.acid-luma.credentials.postgresql.acid.zalan.do` and `luma-conductor.acid-luma.credentials.postgresql.acid.zalan.do`, each with keys **`username` and `password`** (the product chart reads those names).
  - WAL and base backups in `s3://galaxy-cluster-backups/spilo/acid-luma/wal-g-1/`, and dumps in `spilo/acid-luma/<uid>/logical_backups/`.
  - CronJob `walg-canary-acid-luma`.

- [ ] **Step 1: Write the B2 Secret with placeholders; Chris pastes.**
  ```bash
  cat > cluster/luma/logical-backup-s3.secret.yaml <<'EOF'
  apiVersion: v1
  kind: Secret
  metadata: {name: logical-backup-s3, namespace: luma-system}
  stringData:
    AWS_ACCESS_KEY_ID: __PASTE_galaxy-acid-luma_keyID__
    AWS_SECRET_ACCESS_KEY: __PASTE_galaxy-acid-luma_applicationKey__
  EOF
  ```
  - (Chris only) Replace both placeholders in `$EDITOR`.
  - Verify with `grep -c __PASTE_ cluster/luma/logical-backup-s3.secret.yaml`, which must print `0`.
  - Run `yq '.stringData | to_entries | .[] | .key + " len=" + (.value | length | tostring)' cluster/luma/logical-backup-s3.secret.yaml`. Expected: lengths 25 and 31 (B2's key-id and key formats). Otherwise Chris re-checks.
- [ ] **Step 2: Write `psql.yaml`.** It has `cluster/gramps/psql.yaml`'s shape, with these differences:
  - **Identity:** `name: acid-luma`, `namespace: luma-system`, `numberOfInstances: 2`, `enableLogicalBackup: true`, `spiloRunAsUser: 101`, `spiloRunAsGroup: 103`, `spiloFSGroup: 103`.
  - **Argo:** `metadata.annotations` `argocd.argoproj.io/sync-options: Prune=false,Delete=false` (H12).
  - **Placement:** no `nodeAffinity`, because no node is excluded (H10). Check first: `kubectl get crd postgresqls.acid.zalan.do -o json | jq '[.spec.versions[] | .schema.openAPIV3Schema.properties.spec.properties | has("topologySpreadConstraints")]'`.
    - **If it prints `true`:** set `topologySpreadConstraints: [{maxSkew: 1, topologyKey: kubernetes.io/hostname, whenUnsatisfiable: ScheduleAnyway, labelSelector: {matchLabels: {galaxy: acid-luma}}}]`. The label key is the operator's `cluster_name_label`, which is `galaxy`. Comment: a soft spread of the two members (H10).
    - **If `false`** (expected on v1.11): add nothing. Placement is verified in Step 6 instead.
  - **Roles and database:** `users: {luma_core: [], luma_conductor: []}`, `databases: {luma: luma_core}`.
  - **`postgresql.parameters`:**
    - `synchronous_commit: "on"` and `archive_timeout: "300s"` (H3);
    - `max_slot_wal_keep_size: "4GB"` (H9);
    - `log_parameter_max_length: "0"` and `log_parameter_max_length_on_error: "0"`, so bind parameters, which carry message text, never reach Postgres logs.

    Comment: this is the SSD pool, and approvals and memory writes must not be lost.
  - **`patroni`:** `{failsafe_mode: true, synchronous_mode: true, synchronous_mode_strict: false}` (H9).
  - **Volume and resources:** `volume: {size: 10Gi, storageClass: rook-ceph-block-ssd}`; requests 50m/256Mi, limits 1/1Gi; the same postgres-exporter sidecar.
  - **`spec.env`** (uppercase names; `spec.env` overrides the operator's pod env):
    ```yaml
    env:
      - {name: USE_WALG_BACKUP, value: "true"}
      - {name: USE_WALG_RESTORE, value: "true"}
      - {name: WAL_S3_BUCKET, value: galaxy-cluster-backups}
      - {name: WAL_BUCKET_SCOPE_PREFIX, value: ""}
      - {name: WAL_BUCKET_SCOPE_SUFFIX, value: /wal-g-1}
      - {name: AWS_ENDPOINT, value: https://s3.us-west-004.backblazeb2.com}
      - {name: AWS_REGION, value: us-west-004}
      - {name: AWS_S3_FORCE_PATH_STYLE, value: "true"}
      - {name: BACKUP_SCHEDULE, value: "15 5 * * *"}
      - {name: BACKUP_NUM_TO_RETAIN, value: "30"}
      - {name: WALG_PREVENT_WAL_OVERWRITE, value: "true"}
      - name: AWS_ACCESS_KEY_ID
        valueFrom: {secretKeyRef: {name: logical-backup-s3, key: AWS_ACCESS_KEY_ID}}
      - name: AWS_SECRET_ACCESS_KEY
        valueFrom: {secretKeyRef: {name: logical-backup-s3, key: AWS_SECRET_ACCESS_KEY}}
    ```
- [ ] **Step 3: Write `postgres-podmonitor.yaml`**, the home-assistant copy with `galaxy: acid-luma`, `namespaceSelector: [luma-system]` and `release: prometheus`.
- [ ] **Step 4: Write `walg-canary-cronjob.yaml`.**
  - **Schedule and history:** `walg-canary-acid-luma`, `schedule: "0 9 * * *"`, `timeZone: Etc/UTC`, `concurrencyPolicy: Forbid`, `failedJobsHistoryLimit: 3`, `successfulJobsHistoryLimit: 1`, `ttlSecondsAfterFinished: 86400`.
  - **Pod:** `automountServiceAccountToken: false`, `runAsUser: 101`, and requests 10m/64Mi, limit 256Mi.
  - **Image:** the operator's configured Spilo image, pinned by the same digest (read it from `cluster/applications/postgres-operator.yaml` or the operator ConfigMap at execution).
  - **Env:** `WALG_S3_PREFIX=s3://galaxy-cluster-backups/spilo/acid-luma/wal-g-1`, the `AWS_*` from Step 2.
  - **Command:** `wal-g backup-list --json`, piped to `python3`, which prints only the newest backup's age in hours and exits 1 if there is no backup or the newest is older than 36 h.
- [ ] **Step 5: Seal, pre-commit, commit, push.**
  - `rm -f cluster/luma/logical-backup-s3.yaml && KUBECONFIG=/tmp/galaxy-kubeconfig nix develop --command ./sign.sh`, then the `\0GITCRYPT` check, then `pre-commit run --files cluster/luma/*`.
  - Commit:
    ```bash
    git add cluster/luma/psql.yaml cluster/luma/postgres-podmonitor.yaml cluster/luma/walg-canary-cronjob.yaml \
            cluster/luma/logical-backup-s3.secret.yaml cluster/luma/logical-backup-s3.yaml cluster/luma/kustomization.yaml
    git commit -m "luma: acid-luma on SSD, sync standby, WAL-G PITR with a dedicated key, backup canary"
    git push
    ```
- [ ] **Step 6: Verify the cluster** with `Monitor`.
  - `kubectl -n luma-system get postgresql acid-luma` → `Running`.
  - `kubectl -n luma-system get pods -l galaxy=acid-luma -o wide` → two pods on **two different nodes**. Any of the three may be used (H10).
    - **If both pods share a node:** the sync standby shares the primary's failure domain. Record it, and with Chris's OK, `kubectl -n luma-system delete pod <the replica>` (S2 exception). Delete the replica only, found with `-l galaxy=acid-luma,spilo-role=replica`, and never the primary.
    - Its RBD volume can attach anywhere, so the scheduler's spreading re-places it. `Monitor` until it is Running, then re-check that the nodes differ.
    - If it lands on the same node again, stop and report: the other nodes lack room (P3a).
  - PVCs are Bound on `rook-ceph-block-ssd`.
- [ ] **Step 7: Find the primary and check the sync standby.** Every later exec uses `$P`, never `acid-luma-0` by name.
  - Run `P=$(kubectl -n luma-system get pod -l galaxy=acid-luma,spilo-role=master -o name)`.
  - Run `kubectl -n luma-system exec "$P" -c postgres -- patronictl list`. Expected: one `Leader` and one **`Sync Standby`**, with lag 0.
- [ ] **Step 8: Verify WAL-G.** The container already runs as uid 101, so there is **no `su`**.
  - **Archiving:** `kubectl -n luma-system exec "$P" -c postgres -- psql -U postgres -Atc "select pg_switch_wal(); select archived_count, failed_count from pg_stat_archiver"`. Expected: `archived_count ≥ 1` within a minute, and `failed_count = 0`.
  - **Envdir:** `kubectl -n luma-system exec "$P" -c postgres -- sh -c 'ls /run/etc/wal-e.d/env; cat /run/etc/wal-e.d/env/WALG_S3_PREFIX; cat /run/etc/wal-e.d/env/WALG_DELTA_MAX_STEPS 2>/dev/null || echo delta=unset'`. Expected:
    - names only, plus the path `s3://galaxy-cluster-backups/spilo/acid-luma/wal-g-1`;
    - `WALG_PREVENT_WAL_OVERWRITE` is listed;
    - `delta=unset`, or `0`.

    If `WALG_PREVENT_WAL_OVERWRITE` is missing, Spilo does not pass it through; record it, and set it through the operator's `pod_environment_configmap` as a follow-up.
  - **Cron:** `kubectl -n luma-system exec "$P" -c postgres -- sh -c 'ls /etc/service; ps -eo comm | grep -c "^cron"'`. Expected: `cron` is listed and the count is ≥ 1.
    - **Branch, if cron is not running under uid 101: run Spilo as root for `acid-luma`** *(Chris, 2026-10-19; pre-approved, so no new question)*. Non-root Spilo cannot run cron, so the base backup at `BACKUP_SCHEDULE` would never happen. The namespace's PSS `baseline` (H1) admits a root container.
      1. **Record before:** `kubectl -n luma-system exec "$P" -c postgres -- stat -c '%u:%g' /home/postgres/pgdata/pgroot/data`. Expected: `101:103`.
      2. **Edit `psql.yaml`:** remove `spiloRunAsUser`, `spiloRunAsGroup` and `spiloFSGroup` from `acid-luma` only, with a comment: "root so Spilo's cron runs `BACKUP_SCHEDULE` (Chris, 2026-10-19); Spilo still runs Postgres as `postgres`, uid 101". Leave the canary CronJob at `runAsUser: 101`, because it runs no cron.
      3. **Commit and push:** `git commit -m "luma: run acid-luma's Spilo as root so WAL-G base-backup cron runs"`. The operator rolls the StatefulSet: the replica first, then a switchover, then the former primary. `Monitor` until both pods are Running and `patronictl list` again shows a `Leader` and a `Sync Standby` with lag 0 (S3; don't delete pods to hurry it).
      4. **Verify,** with `P` re-read (the primary has moved):
         - `kubectl -n luma-system exec "$P" -c postgres -- sh -c 'id -u; ps -eo user,comm | awk "\$2==\"cron\"{print \$1}" | sort -u; ps -eo user,comm | awk "\$2==\"postgres\"{print \$1}" | sort -u; crontab -l -u postgres | grep -c postgres_backup.sh; stat -c %u:%g /home/postgres/pgdata/pgroot/data'`. Expected, in order:
           - `0`: the container is root;
           - `root`: cron runs;
           - `postgres` only: the server is not root;
           - `1`: the backup entry is installed;
           - `101:103`: data ownership is unchanged.
         - Then the archiving check above again: `failed_count = 0` after a `pg_switch_wal()`.
         - Then the envdir check above again.
         - Pod admission: `kubectl -n luma-system get events --field-selector reason=FailedCreate` is empty. `warn: restricted` warnings on apply are expected and fine.
      5. **The real proof** is Step 11's cron-made base backup the next morning. If it is missing even as root, stop and report: the cause is not the uid.
      6. **Rollback:** revert the commit. The operator rolls back the same way.
  - **First base backup**, by hand: `kubectl -n luma-system exec "$P" -c postgres -- envdir /run/etc/wal-e.d/env /scripts/postgres_backup.sh /home/postgres/pgdata/pgroot/data`, then `… -- envdir /run/etc/wal-e.d/env wal-g backup-list`. Expected: one `base_…` line.
  - **From the laptop,** with Chris's existing B2 auth: `nix develop --command backblaze-b2 ls --recursive b2://galaxy-cluster-backups/spilo/acid-luma/wal-g-1/ | head`. Expected: `basebackups_005/` and `wal_005/` objects.
- [ ] **Step 9: Check Patroni REST auth (read-only).**
  - Run `kubectl -n luma-system exec "$P" -c postgres -- python3 -c 'import yaml; c = yaml.safe_load(open("/run/postgres.yml")); print("restapi_auth", "authentication" in c.get("restapi", {}))'`.
  - **If it prints `False`:** any pod can call Patroni's write endpoints on :8008 (switchover, restart, config). That is a **finding for every `acid-*` cluster**, not only Luma's.
    - Chris decided (2026-10-19) to **fix it now, as its own plan**: `docs/superpowers/plans/2026-10-19-patroni-rest-auth.md`. Do not fix it inline here.
    - Record the result, `True` or `False`, in that plan's Status, and in `docs/luma.md` "Operations (phase 1)" (Task 11).
    - **Phase-1 deploy proceeds regardless.** This step gates nothing, and `acid-luma` gets the fix along with the other clusters when that plan runs.
  - **If it prints `True`:** record it the same way. The separate plan decides what, if anything, remains for the other `acid-*` clusters.
- [ ] **Step 10: Verify the CronJobs and the canary.**
  - `kubectl -n luma-system get cronjob logical-backup-acid-luma walg-canary-acid-luma`: both exist.
  - `kubectl -n luma-system create job --from=cronjob/walg-canary-acid-luma walg-canary-first`, then `Monitor` to `Complete`. Its log is one line with the age in hours.
- [ ] **Step 11 (the next day): the second base backup.** `wal-g backup-list` shows **two** `base_…` lines, the newer made by cron after 05:15 UTC. Also check that the first nightly `logical-backup-acid-luma` run succeeded. A missing second backup is a Review Focus 3 failure: stop and fix it before Task 12.

### Task 5: Core's Secrets and the Dex static client `luma-core`

**Files:**
- Create: `cluster/luma/{core.secret.yaml,core.yaml,registry.secret.yaml,registry.yaml}`
- Modify: `cluster/dex/config.secret.yaml`; delete and regenerate `cluster/dex/config.yaml`; `cluster/luma/kustomization.yaml`

**Interfaces:**
- Consumes: P2 (Dex 2.46, strict config, sessions and consent).
- Produces:
  - The Dex static client `luma-core` (confidential):
    ```yaml
    - id: luma-core
      name: Luma
      secret: <generated: equals luma-core OIDC_CLIENT_SECRET>
      redirectURIs: [https://luma.chrismiller.xyz/auth/callback]
      allowedConnectors: [google, microsoft]   # H7 (Chris, 2026-10-19); key verified against v2.46 storage.Client
    ```
  - Secret `luma-core`: `OIDC_CLIENT_SECRET` (64 hex chars) and `SESSION_SIGNING_KEY` (base64 of 32 random bytes; 44 chars).
  - Secret `luma-registry` with key `registry.yaml`. Each address is pinned to its connector (H7):
    ```yaml
    people:
      chris: {display_name: Chris, tier: admin, household: true,
              emails: {google: [__CHRIS_GOOGLE_ADDRESS__], microsoft: [__CHRIS_MICROSOFT_ADDRESS__]}}
    ```

- [ ] **Step 1 (S1): read Dex's live state, without secrets.**
  - `kubectl -n dex get secret dex-config -o jsonpath='{.data.config\.yaml}' | base64 -d | yq '.staticClients[].id'`. Expected: the ids in git (`oauth2-proxy-admin, immich, oauth2-proxy-family, gramps`, plus any 0a added).
  - `kubectl -n dex get deploy dex -o jsonpath='{.spec.template.spec.containers[0].image} {.spec.template.spec.containers[0].env[?(@.name=="DEX_CONFIG_DISALLOW_UNKNOWN_FIELDS")].value}'`. Expected: `…dex:v2.46.0@sha256:… true`. With strict config, a misspelt key crashloops the **new** pod while the old ones keep serving.
  - `kubectl -n dex get secret dex-config -o jsonpath='{.data.config\.yaml}' | base64 -d | yq '[.connectors[].id] | sort'`. Expected: the list includes both `google` and `microsoft` (ids only, never config). If `microsoft` is missing, stop: `allowedConnectors` would name a connector that does not exist.
  - Record the pre-edit structural hash, which is a hash only:
    ```bash
    yq '.stringData."config.yaml"' cluster/dex/config.secret.yaml | yq -o=json -I0 '.' | sha256sum > /tmp/luma-dex-before.sha
    ```
    The file holds a hash only, never config.
- [ ] **Step 2: Generate both secrets and write all three places, in one shell.**
  ```bash
  nix develop --command bash -c 'set -euo pipefail
  OIDC=$(head -c32 /dev/urandom | od -An -tx1 | tr -d " \n"); SK=$(head -c32 /dev/urandom | base64 -w0)
  : "${OIDC:?}" "${SK:?}"; [ ${#OIDC} -eq 64 ]; [ ${#SK} -eq 44 ]
  export OIDC SK
  python3 - cluster/dex/config.secret.yaml <<PY
  import os, sys
  p = sys.argv[1]; s = open(p).read()
  anchor = "\n    connectors:\n"
  assert s.count(anchor) == 1, "anchor"
  assert "id: luma-core" not in s, "already present"
  block = ("      - id: luma-core\n        name: Luma\n        secret: " + os.environ["OIDC"] + "\n"
           "        redirectURIs:\n          - https://luma.chrismiller.xyz/auth/callback\n"
           "        allowedConnectors: [google, microsoft]\n\n")
  open(p, "w").write(s.replace(anchor, "\n" + block + "    connectors:\n"))
  PY
  yq -n ".apiVersion=\"v1\" | .kind=\"Secret\" | .metadata.name=\"luma-core\" | .metadata.namespace=\"luma-system\"
         | .stringData.OIDC_CLIENT_SECRET=strenv(OIDC) | .stringData.SESSION_SIGNING_KEY=strenv(SK)" > cluster/luma/core.secret.yaml
  cmp -s <(yq ".stringData.\"config.yaml\"" cluster/dex/config.secret.yaml | yq ".staticClients[] | select(.id == \"luma-core\") | .secret") \
         <(yq ".stringData.OIDC_CLIENT_SECRET" cluster/luma/core.secret.yaml) && echo "dex vs core client secret: match"
  yq ".stringData | to_entries | .[] | .key + \" len=\" + (.value|length|tostring)" cluster/luma/core.secret.yaml'
  ```
  Expected: `dex vs core client secret: match`, `OIDC_CLIENT_SECRET len=64` and `SESSION_SIGNING_KEY len=44`. `python3` comes from `nix develop`, because rowlett has none.
- [ ] **Step 3: Write the registry.**
  - The agent writes `cluster/luma/registry.secret.yaml` (Secret `luma-registry`, key `registry.yaml`) with the placeholder above.
  - **Chris** replaces both placeholders in `$EDITOR`:
    - `__CHRIS_GOOGLE_ADDRESS__`: the address he uses at Google sign-in, as in `cluster/oauth2-proxy/emails-admin.secret.yaml`;
    - `__CHRIS_MICROSOFT_ADDRESS__`: the address of the Microsoft account he signs in with. Dex's `microsoft` connector reports the account's `userPrincipalName` as the email, which for a personal account is its sign-in address.
  - Verify, printing names and booleans only:
    - `grep -c __CHRIS_ cluster/luma/registry.secret.yaml` → `0`;
    - `yq '.stringData."registry.yaml" | from_yaml | .people | keys'` → `["chris"]`;
    - `yq '.stringData."registry.yaml" | from_yaml | .people.chris.emails | to_entries | .[] | .key + " n=" + (.value | length | tostring)'` → `google n=1` and `microsoft n=1`.
- [ ] **Step 4: Seal** (Review Focus 2).
  - Run `rm cluster/dex/config.yaml && KUBECONFIG=/tmp/galaxy-kubeconfig nix develop --command ./sign.sh`. Expected: it prints `cluster/dex/config.secret.yaml`, `cluster/luma/core.secret.yaml` and `cluster/luma/registry.secret.yaml`, with no `FAIL`.
  - `git add` each pair and do the `\0GITCRYPT` check.
- [ ] **Step 5: Check the edit structurally, printing hashes only.**
  - Run `yq '.stringData."config.yaml"' cluster/dex/config.secret.yaml | yq -o=json -I0 'del(.staticClients[] | select(.id == "luma-core"))' | sha256sum | cmp - /tmp/luma-dex-before.sha && echo unchanged`. Expected: `unchanged`, so nothing else in the Dex config changed.
  - Run `git diff --cached --no-textconv --stat`. That is the only diff view used.
- [ ] **Step 6: Pre-commit, commit, push, and wait for the unseal.**
  - Run `RV=$(kubectl -n dex get secret dex-config -o jsonpath='{.metadata.resourceVersion}')`.
  - Stage the explicit paths and commit `luma: Dex confidential client luma-core; Core and registry SealedSecrets`, then push.
  - `Monitor` until the Secret's `resourceVersion` ≠ `$RV` (0a's pattern).
  - Then compare live with git by hash: `kubectl -n dex get secret dex-config -o jsonpath='{.data.config\.yaml}' | base64 -d | yq -o=json -I0 . | sha256sum` against `yq '.stringData."config.yaml"' cluster/dex/config.secret.yaml | yq -o=json -I0 . | sha256sum`. Expected: equal.
- [ ] **Step 7: Restart Dex.** It reads its config only at start; this is the S2 exception.
  - Run `kubectl -n dex rollout restart deploy/dex`, then `Monitor` `kubectl -n dex rollout status deploy/dex`. Expected: `successfully rolled out`.
  - If the new pod crashloops on an unknown field, the old pods keep serving. Fix the field, or take the revert path below. **Do not roll back blindly.**
- [ ] **Step 8: Verify Dex's behaviour, not the file.**
  - **Authorize:**
    ```bash
    curl -s -o /dev/null -w '%{http_code}\n' \
     'https://dex.chrismiller.xyz/auth?client_id=luma-core&redirect_uri=https%3A%2F%2Fluma.chrismiller.xyz%2Fauth%2Fcallback&response_type=code&scope=openid+email&state=x'
    ```
    Expected: `200` (the connector page) or `302`, and no "Unregistered" or "Invalid client" page. With `redirect_uri=https%3A%2F%2Fevil.example%2F`, expected: a 400 error page.
  - **Token:** the secret goes through curl's stdin config, never argv, and is never printed:
    ```bash
    bash -c 'set -euo pipefail
    S=$(yq ".stringData.OIDC_CLIENT_SECRET" cluster/luma/core.secret.yaml); : "${S:?}"
    printf "user = \"luma-core:%s\"\n" "$S" | curl -s --config - \
      -d grant_type=authorization_code -d code=bogus -d redirect_uri=https://luma.chrismiller.xyz/auth/callback \
      https://dex.chrismiller.xyz/token | jq -r .error'
    ```
    Expected: `invalid_grant` (or another non-client error), which means the client authenticated. **`invalid_client` means the secret or the client is wrong: stop.**
- [ ] **Step 9: Check nothing else broke.** Chris does one browser login each to oauth2-proxy, Immich and Gramps, and the edge snapshot diff is empty for those hosts.
- [ ] **Revert path** (if needed):
  1. Remove the block in one `nix develop --command python3` edit, which asserts that exactly one `id: luma-core` exists.
  2. `rm cluster/dex/config.yaml && ./sign.sh`, then commit and push.
  3. Wait for the `resourceVersion` to change, then `rollout restart`.
  4. Re-run the Step 8 authorize probe. Expected: "Unregistered client".

### Task 6: Site config (agents, models, evals)

**Files:**
- Create: `cluster/luma/agents/luma/AGENT.md`, `cluster/luma/agents/deep-think/AGENT.md`, `cluster/luma/agents/luma/evals/*.yaml`, `cluster/luma/agents/deep-think/evals/*.yaml`, `cluster/luma/models.yaml`
- Modify: `cluster/luma/kustomization.yaml`

**Interfaces:**
- Consumes: the luma repo at the chart's tag (`agents/`, `agents/models.yaml`; product Task 6), and the Task 3 branches.
- Produces:
  - ConfigMap `luma-config`, with flat keys `luma.agent.md`, `deep-think.agent.md` and `models.yaml`.
  - ConfigMap `luma-evals`, one key per eval file, `<agent>-<name>.yaml`. These are the golden tasks: 10–20 `luma` fast tasks, the `smart` weekly set, and `deep-think`.

- [ ] **Step 1 (P4): Copy at the tag.** For each file, run `git -C ~/Repos/luma show v0.1.0-rc.1:agents/luma/AGENT.md > cluster/luma/agents/luma/AGENT.md`, and likewise for `deep-think`, every eval and `models.yaml`. Apply any Task 3 branch (for example L1's TTL) to `models.yaml`, and list every site edit in `docs/luma.md` "Site branches" (Task 11).
- [ ] **Step 2: Add the generators** to `kustomization.yaml`, with `namespace: luma-system` and `options: {disableNameSuffixHash: true}`.
  - `luma-config`: `files: [luma.agent.md=agents/luma/AGENT.md, deep-think.agent.md=agents/deep-think/AGENT.md, models.yaml]`.
  - `luma-evals`: one `<agent>-<name>.yaml=agents/<agent>/evals/<name>.yaml` entry per file.

  A stable name lets the kubelet update the mount, so Core's watcher reloads and new sessions pick up the new version (§11).
- [ ] **Step 3: Commit.** Run pre-commit, `git add` the explicit paths, then `git commit -m "luma: agents, models.yaml and evals ConfigMaps"` and push. Verify with `kubectl -n luma-system get cm luma-config luma-evals`. Expected: both exist.

### Task 7: Argo app `luma-release` and the WAN-IP check

**Files:**
- Create: `cluster/applications/luma-release.yaml`, `scripts/check-wan-ip.sh`
- Modify: `cluster/applications/kustomization.yaml`, `.renovaterc.json`, `.pre-commit-config.yaml`

**Interfaces:**
- Consumes: the chart `luma` `0.1.0-rc.1` (P4), every Secret and ConfigMap above, and product C8.
- Produces:
  - **Deployments:** `luma-core`, `luma-conductor`, `luma-gateway` (×2), `luma-egress-web`.
  - **Services:** `luma-core:8080`, `luma-core-internal:8081/9090`, `luma-gateway:8080/15020`, `luma-egress-web:4750/9810`.
  - **CronJobs:** `luma-evals-nightly`, `luma-evals-weekly`, `luma-usage-reconcile`.
  - **Identity:** the six ServiceAccounts, and ClusterRoleBinding `luma-core-tokenreview`.
  - **Also:** ServiceMonitors, and PriorityClass `luma-batch` (−5).
- `scripts/check-wan-ip.sh` exits non-zero unless these are equal:
  - `luma-release.yaml` → `egress.web.extraDenyRanges[0]` minus `/32`;
  - `router/ansible/group_vars/vyos_routers.yml` → `nat.destination_rules[] | select(.rule==210).destination_address`;
  - the same for rule 310.

  With `--live`, it also compares against `nix shell nixpkgs#dig -c dig +short chrismiller.xyz @1.1.1.1`.

- [ ] **Step 1 (S1, Review Focus 5): read per-node allocation and placement.**
  - `kubectl describe node top middle bottom | grep -A8 'Allocated resources'`;
  - `kubectl -n luma-system get pods -l galaxy=acid-luma -o wide`.

  Expected: a node (any of the three) with ≥ 3.1 GiB of memory-request room. That covers conductor 2 Gi, Core 256 Mi and the nightly eval Job's 768 Mi on the node that doesn't already carry the busier `acid-luma` member. Otherwise **stop** (P3a).
- [ ] **Step 2: Write `luma-release.yaml`**, in `telephony-release.yaml`'s shape:
  - `repoURL: https://christopherjmiller.github.io/luma`, `chart: luma`, `targetRevision: 0.1.0-rc.1`, `releaseName: luma`, destination `luma-system`;
  - automated sync with prune and selfHeal, and **no `CreateNamespace`** (the `luma` app owns the namespace);
  - `valuesObject`:
    ```yaml
    enabled: true
    shed: false                     # H11: true before every drain (docs/luma.md)
    core:
      publicUrl: https://luma.chrismiller.xyz
      oidc: {issuer: https://dex.chrismiller.xyz, clientId: luma-core}
      timezone: America/Los_Angeles
      budget: {monthlyUsd: 150, warnRatio: 0.8}           # Chris: global cap = the interactive workspace limit
      webfetchDenyDomains: [chrismiller.xyz, realliance.net, werethemille.rs, buttert.art]
      secretName: luma-core
      registrySecretName: luma-registry
      database: {host: acid-luma, port: 5432, name: luma, sslmode: require,
                 credentialsSecret: luma-core.acid-luma.credentials.postgresql.acid.zalan.do, conductorRole: luma_conductor}
      config: {existingConfigMap: luma-config}
      resources: {requests: {cpu: 50m, memory: 256Mi}, limits: {memory: 768Mi}}
    conductor:
      maxHosts: 4                  # §6: 4 in phase 1
      idleMinutes: 10
      database: {credentialsSecret: luma-conductor.acid-luma.credentials.postgresql.acid.zalan.do}
      resources: {requests: {cpu: 200m, memory: 2Gi}, limits: {memory: 3Gi}}   # includes the 512Mi memory emptyDir
    internalSecretName: luma-internal
    gateway:
      replicas: 2
      anthropicSecretName: luma-anthropic
      resources: {requests: {cpu: 50m, memory: 128Mi}, limits: {memory: 512Mi}}
    egress:
      web:
        # [0] is the WAN IP and must equal router/ansible/group_vars/vyos_routers.yml NAT rules 210/310
        # destination_address (scripts/check-wan-ip.sh, pre-commit). Reflection would hairpin it to Traefik.
        extraDenyRanges: ["152.44.247.88/32"]
        acl:
          extraGlobalDenyList: [chrismiller.xyz, "*.chrismiller.xyz", realliance.net, "*.realliance.net",
                                werethemille.rs, "*.werethemille.rs", buttert.art, "*.buttert.art",
                                discord.com, "*.discord.com", discordapp.com, "*.discordapp.com"]   # Discord: Chris
        resources: {requests: {cpu: 20m, memory: 64Mi}, limits: {memory: 256Mi}}
    evals:
      enabled: true
      existingConfigMap: luma-evals
      nightlySchedule: "40 5 * * *"
      weeklySchedule: "10 6 * * 1"
    usageReconcile:
      enabled: true
      schedule: "30 7 * * *"
      timeZone: Etc/UTC
      adminKeySecretName: luma-anthropic-admin
      workspaces: [luma-interactive, luma-ci]
    priorityClass: {create: true, name: luma-batch, value: -5}
    serviceMonitor: {enabled: true, labels: {release: prometheus}}
    ```
    Add P8's IPv6 prefix to `extraDenyRanges` (after `[0]`) if there is one. Core and the conductor carry no `affinity`: no node is excluded (H10).
- [ ] **Step 3: Write `check-wan-ip.sh`** and a local pre-commit hook `wan-ip-consistent`, whose `files:` matches the two source files. Test it on a scratch copy with the IP changed: the script exits 1 and names both files. Then run it for real: exit 0.
- [ ] **Step 4: Add the Renovate rule** to `.renovaterc.json`: `matchManagers: ["argocd"]`, `matchPackageNames: ["luma"]`, `automerge: false`, for **every** update type. Its description: "A luma chart bump changes the Agent SDK, CLI, gateway or prompts. It waits for the eval gate (docs/luma.md, Upgrading Luma)."
- [ ] **Step 5: Commit and push.** Run pre-commit, then `git add cluster/applications/luma-release.yaml cluster/applications/kustomization.yaml .renovaterc.json .pre-commit-config.yaml scripts/check-wan-ip.sh`, then `git commit -m "luma: deploy chart 0.1.0-rc.1; WAN IP consistency check"`, and push.
- [ ] **Step 6: Watch the rollout** (safe-rollout skill, `Monitor`).
  - `kubectl -n argo-cd get app luma-release` reaches Synced/Healthy, and `kubectl -n luma-system get pods -o wide` shows every pod Ready. Expected:
    - the conductor is on a node that had the Step 1 room (any node, `middle` included);
    - the two gateway pods are on different nodes;
    - `kubectl -n luma-system logs deploy/luma-conductor | grep -c '"msg":"core link up"'` ≥ 1, which proves the projected token passed TokenReview;
    - Core's `/readyz` is 200;
    - `kubectl get clusterrolebinding luma-core-tokenreview` exists.
  - An image pull on a cold start can take minutes (S3); don't kill pods.
- [ ] **Step 7: In-cluster checks from a throwaway pod.** Create it with the pinned netshoot image and an explicit restricted securityContext:
  ```bash
  kubectl -n luma-system run tmp --rm -i --restart=Never --image=<netshoot tag@digest> --overrides='{"spec":{"automountServiceAccountToken":false,
    "securityContext":{"runAsNonRoot":true,"runAsUser":65532,"seccompProfile":{"type":"RuntimeDefault"}},
    "containers":[{"name":"tmp","image":"<netshoot tag@digest>","command":["sh"],"stdin":true,
      "securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}}}]}}'
  ```
  Then:
  - **Gateway:**
    - `curl -s -o /dev/null -w '%{http_code}' -X POST http://luma-gateway:8080/ci/v1/messages -d '{}'` → `401` (no bearer).
    - `curl -s -o /dev/null -w '%{http_code}' http://luma-gateway:8080/ci/v1/models` → `404` (unlisted path).
  - **Egress, over CONNECT:** `curl -s -o /dev/null -w '%{http_connect}' -x http://luma-egress-web:4750 https://<target>/` for `192.168.0.1`, `152.44.247.88`, `home.chrismiller.xyz`, `discord.com` and `webhook.site`. Expected: each prints `407`, and `-D -` shows `X-Smokescreen-Error`.
  - **Egress, plain HTTP on port 80:** `curl -s -o /dev/null -w '%{http_code}' -x http://luma-egress-web:4750 http://152.44.247.88/` → `407`.
  - **Egress, allowed:** `curl -s -o /dev/null -w '%{http_code}' -x http://luma-egress-web:4750 https://example.com/` → `200`.
  - **Core ports:**
    - `curl -s -o /dev/null -w '%{http_code}' http://luma-core:8080/internal/v1/conductor` → `404`.
    - `curl -s -o /dev/null -w '%{http_code}' 'http://luma-core-internal:8081/internal/v1/agents/luma/spec?tier=fast'` → `401` (no ServiceAccount token).
- [ ] **Step 8: The WebFetch guard, by scripted conversation** (product `guard` suite; recorder-answered, so no Anthropic spend):
  ```bash
  kubectl -n luma-system create job --from=cronjob/luma-evals-nightly luma-evals-guard-1 --dry-run=client -o yaml \
    | yq '.spec.template.spec.containers[0].args = ["/app/dist/evals/main.js","run","--suite","guard"]' \
    | kubectl apply -f -
  ```
  The `args` are **replaced, not appended**. `Monitor` to completion. Expected: exit 0, and every guard target reported as denied (ids only).
- [ ] **Step 9: First usage reconcile.**
  - Run `kubectl -n luma-system create job --from=cronjob/luma-usage-reconcile luma-usage-reconcile-first`, then `Monitor` to Complete.
  - Then, with `P=$(kubectl -n luma-system get pod -l galaxy=acid-luma,spilo-role=master -o name)`, run `kubectl -n luma-system exec "$P" -c postgres -- psql -U postgres -d luma -Atc "select day, workspace, api_usd, ledger_usd from usage_reconciliations order by day desc limit 2"`.
  - Expected: rows for `luma-interactive` (≈ 0) and `luma-ci` (≈ Task 3's spike cost). Chris compares `luma-ci` with the Console's daily cost, which confirms the Admin API units.

### Task 8: Ingress `luma.chrismiller.xyz` and the edge snapshot

**Files:**
- Create: `cluster/luma/ingress.yaml`
- Modify: `cluster/luma/kustomization.yaml`, `.claude/skills/edge-ingress/edge-snapshot.sh`

**Interfaces:**
- Produces an Ingress `luma` in `luma-system`:
  - `ingressClassName: traefik`, host `luma.chrismiller.xyz`, path `/` Prefix → Service `luma-core` port 8080;
  - **one annotation**, `traefik.ingress.kubernetes.io/router.observability.accesslogs: "false"` (P9; Traefik ≥ 3.3). 0b's fluent-bit grep filter is the backstop for shipping;
  - no oauth2-proxy middleware (Core is the BFF), no `spec.tls`, and no DNS or cert annotations.

  WebSockets need nothing extra on Traefik.

- [ ] **Step 1: Snapshot before** (edge-ingress skill): `cd .claude/skills/edge-ingress && nix-shell -p openssl --run ./edge-snapshot.sh > /tmp/before.txt`. Check P9; if Traefik is older than v3.3.0, omit the annotation and record that access logs keep the luma paths (metadata only).
- [ ] **Step 2: Write the Ingress and add `luma`** to `edge-snapshot.sh`'s `hosts=` list. **Do not add a DNS record** (the wildcard covers it). An explicit `luma` record would also break the later `share.luma` wildcard synthesis (§12.3).
- [ ] **Step 3: Commit and push** with `git commit -m "luma: public ingress luma.chrismiller.xyz (BFF auth in Core, no access logs)"`. Wait for Argo.
- [ ] **Step 4: Snapshot after** and diff: `diff <(sed 's/ cert=.*//' /tmp/before.txt) <(sed 's/ cert=.*//' /tmp/after.txt)`. Expected: exactly one added line, for `luma` (200, with Core's `Content-Security-Policy`), and nothing else.
- [ ] **Step 5: Edge checks.**
  - `curl -s -o /dev/null -w '%{http_code}' https://luma.chrismiller.xyz/v1/me` → `401`.
  - `curl -sI https://luma.chrismiller.xyz/ | grep -i -E 'content-security-policy|strict-transport'`: both headers present.
  - `curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Origin: https://evil.example' https://luma.chrismiller.xyz/v1/sessions` → `403`.
  - `./cloudflare/dns/tf.sh plan`: empty.
  - `kubectl -n traefik logs -l app.kubernetes.io/name=traefik --since=10m | grep -c 'luma.chrismiller.xyz'` → `0` (the router's access logs are off).
- [ ] **Step 6: First login (Chris), through both connectors.**
  - Open `https://luma.chrismiller.xyz` on the laptop and sign in with **Google**. **Expect Dex's consent page ("Luma would like to:") the first time**, because 0a turned the skip off and remembers consent. Approve it.
  - Expected: the chat loads, and `/v1/me` shows `chris`. A second Google login shows no consent page.
  - Sign out, then sign in with **Microsoft**. Expect its own consent page once.
  - Expected: `/v1/me` again shows `chris`, the same person and the same history.
    - If Microsoft gives "This account isn't invited to Luma.", the registered address differs from the one Dex reports. Chris reads it himself (not in chat): `kubectl -n dex logs deploy/dex --since=10m | grep 'login successful' | grep microsoft | tail -1`. He corrects the `microsoft` address in `registry.secret.yaml`, then reseals it (Task 5 Step 4; `rm cluster/luma/registry.yaml` first), commits and pushes. Core watches the registry, so it needs no restart.
  - Then sign in with an address that is not in the registry. Expected: the "This account isn't invited to Luma." page.
  - Connector pinning (an address accepted only through its registered connector) is covered by product Task 8's tests. It cannot be exercised live with real accounts.

### Task 9: Observability: rules, dashboard, and what Prometheus really loaded

**Files:**
- Create: `cluster/luma/prometheusrule.yaml`, `cluster/grafana/dashboards/luma.yaml`
- Modify: `cluster/luma/kustomization.yaml`, `cluster/grafana/kustomization.yaml`

**Interfaces:**
- Consumes:
  - product C7 metric names;
  - **the gateway request counter's exact name and labels, as recorded in product `docs/spikes/phase-1.md` §G** (written `<gw_requests>{<status>, request_class}` below);
  - `agentgateway_gen_ai_client_token_usage{gen_ai_token_type}`;
  - postgres-exporter's `pg_stat_archiver_*` and `pg_wal_size_bytes`;
  - kube-state-metrics.
- Produces: PrometheusRule `luma-alerts` in `prometheus`, labelled `release: prometheus` plus the legacy `prometheus: kube-prometheus` and `role: alert-rules`, with group `luma`.

  `SUPPRESS` below stands for `unless on() (max(luma_maintenance_enabled) == 1 or max(kube_deployment_spec_replicas{namespace="luma-system",deployment="luma-core"}) == 0)`. It quiets availability alerts in maintenance mode and while shed.

| Alert | Expr (sketch) | For | Severity |
|---|---|---|---|
| `LumaCoreDown` | `absent(up{namespace="luma-system",service="luma-core-internal"} == 1) SUPPRESS` | 5m | warning |
| `LumaConductorDisconnected` | `max(luma_conductor_connected) == 0 SUPPRESS` | 5m | warning |
| `LumaGatewayDegraded` | `(count(up{namespace="luma-system",service="luma-gateway"} == 1) or vector(0)) < 2 SUPPRESS` | 15m | warning |
| `LumaSmokescreenDown` | `(count(up{namespace="luma-system",service="luma-egress-web"} == 1) or vector(0)) < 1 SUPPRESS` | 10m | warning |
| `LumaGateway5xxRatio` | `sum(rate(<gw_requests>{namespace="luma-system",<status>=~"5.."}[10m])) / sum(rate(<gw_requests>{namespace="luma-system"}[10m])) > 0.05 and sum(rate(<gw_requests>{namespace="luma-system"}[10m])) > 0.01` | 10m | warning |
| `LumaThinkingBinding` | `sum(increase(<gw_requests>{namespace="luma-system",<status>="400",request_class="main"}[1h])) > 0 or increase(luma_thinking_binding_errors_total[1h]) > 0`. The annotation quotes L5's error text. The CLI strips thinking and retries, so only the gateway sees these. | 0m | warning |
| `LumaBudget80` | `max(luma_budget_spent_usd) / max(luma_budget_cap_usd) >= 0.8` | 0m | warning |
| `LumaCacheSLO` | `sum(increase(luma_main_request_tokens_total{slo="included",type="cache_read"}[24h])) / sum(increase(luma_main_request_tokens_total{slo="included"}[24h])) < 0.8`, with the denominator > 200000 | 1h | warning |
| `LumaLedgerDrift` | `max(abs(luma_usage_reconcile_drift_ratio) > 0.10 and abs(luma_usage_reconcile_drift_usd) > 0.50)` | 0m | warning |
| `LumaUsageReconcileStale` | `time() - max(luma_usage_reconcile_last_success_timestamp_seconds) > 36*3600` | 1h | warning |
| `LumaUsageAnomaly` | `increase(luma_usage_anomalies_total[1h]) > 0` | 0m | warning |
| `LumaUnboundModel` | `increase(luma_unbound_model_requests_total[1h]) > 0` | 0m | warning |
| `LumaCostBasisUnexpected` | `increase(luma_cost_basis_unexpected_total[1h]) > 0` (only if product Spike S6 kept the field) | 0m | warning |
| `LumaEvalFailing` | `min(luma_eval_last_run_success{suite="nightly"}) == 0` | 0m | warning |
| `LumaEvalStale` | `time() - max(luma_eval_last_run_timestamp_seconds{suite="nightly"}) > 36*3600` | 1h | warning |
| `LumaPostgresArchiveFailing` | `increase(pg_stat_archiver_failed_count{namespace="luma-system"}[30m]) > 0` | 5m | **critical** |
| `LumaPostgresWalGrowing` | `max(pg_wal_size_bytes{namespace="luma-system"}) > 3e9` (10 Gi volume) | 10m | **critical** |
| `LumaWalgBackupStale` | `time() - max(kube_cronjob_status_last_successful_time{namespace="luma-system",cronjob="walg-canary-acid-luma"}) > 37*3600 or max(kube_job_failed{namespace="luma-system",job_name=~"walg-canary-acid-luma-.*",condition="true"}) == 1` | 10m | **critical** |
| `LumaMaintenanceLeftOn` | `max(luma_maintenance_enabled) == 1` | 6h | warning |
| `LumaShedLeftOn` | `max(kube_deployment_spec_replicas{namespace="luma-system",deployment="luma-core"}) == 0` | 6h | warning |

- **Routing:**
  - **Critical** alerts route to `critical-alerts` at all hours. The weekday `active_time_intervals` mutes only non-critical alerts.
  - The names avoid `Disk`, `Storage`, `Volume` and `Ceph`, so warnings go to `homelab-default`.
- **What is not alerted:** `pg_stat_archiver_last_archive_age` is deliberately not used. An idle database switches no WAL, so it would page every quiet night; failed count, WAL size and the canary cover the failure.

**Dashboard** `luma`: a ConfigMap labelled `grafana_dashboard: '1'`, datasource `"Prometheus"`, uid `luma`. Panels:
- **Dollars per person per day:** bars, `sum by (person) (increase(luma_cost_usd_total[1d]))`, interval 1d.
- **Month to date vs the $150 cap:** stat plus gauge.
- **Ledger vs Usage API drift by workspace:** `luma_usage_reconcile_drift_usd`, `…_ratio`.
- **Cache-read share by request class** (gateway):
  ```
  sum by (request_class) (rate(agentgateway_gen_ai_client_token_usage_sum{gen_ai_token_type="input_cache_read"}[1h]))
  / sum by (request_class) (rate(agentgateway_gen_ai_client_token_usage_sum{gen_ai_token_type="input"}[1h]))
  ```
- **SLO** (Core, `slo="included"`), with an 80 % threshold line, plus the exclusions by reason.
- **Main requests over 100K by tier.**
- **Cache-creation tokens per hour**, to investigate spikes (§12.1.8).
- **Searches per day.**
- **Turns and errors by outcome**, and **gateway 5xx ratio**.
- **Nightly eval status and cost.**
- **Open CLIs, and RSS per CLI:** `sum(luma_conductor_cli_rss_bytes) / clamp_min(sum(luma_conductor_hosts{state=~"ready|running"}), 1)`. Summing first drops the `state` label. This is the "about 400 MiB, measured in phase 1" figure.

- [ ] **Step 1: Write both files.**
  - Run `scripts/check-dashboards.sh` on the new dashboard. Expected: no `OR on() vector(0)` (the `or vector(0)` inside the count alerts is intended and lives in rules only), and every query returns series once data exists.
  - Check `curl -s localhost:9090/api/v1/series --data-urlencode 'match[]=pg_wal_size_bytes{namespace="luma-system"}' | jq '.data|length'`, which must be ≥ 1. If it is 0, enable the exporter's `wal` collector in `psql.yaml`'s sidecar args; don't drop the alert.
- [ ] **Step 2: Commit and push** with `git commit -m "luma: alert rules (critical WAL path), cost/cache/drift dashboard"`.
- [ ] **Step 3: Verify what loaded** (Review Focus 4).
  - First run `scripts/check-prometheus-rules.sh`. Expected: PASS, with `prometheus/luma-alerts` selected and its group loaded.
  - Then port-forward Prometheus and run:
    - `curl -s localhost:9090/api/v1/rules | jq '[.data.groups[] | select(.name=="luma") | .rules[].name]'`. Expected: every name in the table (20, or 19 without `LumaCostBasisUnexpected`).
    - `curl -s localhost:9090/api/v1/targets | jq -r '.data.activeTargets[] | select(.labels.namespace=="luma-system") | "\(.labels.job) \(.health)"'`. Expected: luma-core, luma-conductor, luma-gateway (×2), luma-egress-web and postgres acid-luma (×2), all `up`.

### Task 10: Evals: the first runs, and the eval-gate procedure

**Files:**
- Modify: `docs/luma.md` (written in Task 11; this task supplies its "Upgrading Luma" section)

**Interfaces:**
- Consumes:
  - the CronJobs `luma-evals-nightly` and `luma-evals-weekly` (chart), whose args are `["/app/dist/evals/main.js","run","--suite","<s>"]`;
  - `luma-evals` (Task 6);
  - the rc chart's `templates/evals-gate-job.yaml` (product C8 `evals.gateJob.render`).
- Produces: green `nightly` and `gate` runs in `eval_runs`, and `luma_eval_last_run_success{suite="nightly"} == 1`.

- [ ] **Step 1: Run nightly now.**
  - Run `kubectl -n luma-system create job --from=cronjob/luma-evals-nightly luma-evals-first`, then `Monitor` the job to completion.
  - Expected: exit 0, and the log shows `cache.identical`, `cache.cross_conversation`, `ttl.one_hour`, `replay.fresh`, `replay.resumed` and `replay.forked` PASS, ids and numbers only.
- [ ] **Step 2: Run the gate suite once,** with the args **replaced**:
  ```bash
  kubectl -n luma-system create job --from=cronjob/luma-evals-nightly luma-evals-gate-first --dry-run=client -o yaml \
    | yq '.spec.template.spec.containers[0].args = ["/app/dist/evals/main.js","run","--suite","gate"]' | kubectl apply -f -
  ```
  Expected: every golden task and every guard target PASS, at a cost under $0.50.
- [ ] **Step 3: Check the gauges.** `luma_eval_last_run_success{suite="nightly"}` is 1, and `/v1/me` budget `spent_usd` includes the eval cost (product P12).
- [ ] **Step 4: Write the "Upgrading Luma" procedure** for `docs/luma.md`:
  1. **Read the change.** A Renovate PR or a new rc `vX.Y.Z-rc.N` arrives in the luma repo. Read its release notes, and run `git -C ~/Repos/luma diff vOLD..vNEW -- agents/ charts/luma/values.yaml`.
  2. **Gate against the new stack.** Render and run the **rc's** gate Job:
     ```bash
     nix develop --command bash -c 'set -euo pipefail
     helm repo add luma https://christopherjmiller.github.io/luma >/dev/null 2>&1 || true; helm repo update luma
     helm template luma luma/luma --version X.Y.Z-rc.N -n luma-system \
       -f <(yq ".spec.source.helm.valuesObject" cluster/applications/luma-release.yaml) \
       --set evals.gateJob.render=true --show-only templates/evals-gate-job.yaml | kubectl apply -f -'
     ```
     This uses the **new** conductor/evals image and the **new** gateway image (a native sidecar carrying only the `ci` route and key), against the live Core's agent spec. `Monitor` it to completion; it reports to Core, and its TTL cleans it up.
  3. **If it is green, change one commit:** bump `targetRevision`, **and** re-copy `agents/`, `agents/models.yaml` and the evals from the new tag into `cluster/luma/`, re-applying the "Site branches" in `docs/luma.md`. That way the chart and the agents move together.
  4. **Watch.** Follow the rollout (safe-rollout). Then run the nightly suite at once (`create job --from=cronjob/luma-evals-nightly`), so the new agents are covered under the new Core.
  5. **Never** bump during a drain, a Talos hop or a shed (S5). If the gate fails, leave the PR open and record why.
  6. **When `luma-ci` hits its $20 limit:** evals fail, and `LumaEvalFailing` fires; interactive chat is unaffected until the $150 cap. Raise the limit in the Console, or wait for the month to turn. Don't move evals to `luma-interactive`.

### Task 11: Runbook, CLAUDE.md, and the maintenance and shed hooks

**Files:**
- Modify:
  - `docs/luma.md`, the existing entry page: add an "Operations (phase 1)" section, and set the phase-status row for phase 1;
  - `CLAUDE.md` (topology table and S5);
  - `.claude/skills/talos-upgrade/SKILL.md`, `.claude/skills/ceph-ssd-pool/SKILL.md`;
  - `docs/backups.md`.

**Interfaces:**
- Consumes: product `docs/operations.md` (commands, error codes, phone checklist, rotation), and Tasks 2–10.
- Produces:
  - **`docs/luma.md` "Operations (phase 1)"**:
    - what runs where;
    - **every Secret by name and key, with who writes it.** Chris pastes `luma-anthropic`, `luma-anthropic-admin` and `logical-backup-s3`. The agent generates `luma-internal` and `luma-core` in one shell. Chris fills `luma-registry`;
    - the WAN-IP rule and `check-wan-ip.sh --live`;
    - maintenance mode (`kubectl -n luma-system exec deploy/luma-core -- /luma-core maintenance on|off|status`);
    - **"Before any drain or node power-off"** (H11);
    - "Upgrading Luma" (Task 10), "Site branches" (Task 6), and the restore runbook (Task 12);
    - the phase-1 burn-in log and the phone checklist;
    - "If the WAN IP changes" (router reflection rules and the `/32` together, via vyos-deploy plus this repo);
    - the Patroni REST result (Task 4 Step 9), with a link to `docs/superpowers/plans/2026-10-19-patroni-rest-auth.md`;
    - the two login connectors (Google and Microsoft) and the connector-pinned registry (H7);
    - **rotation:**
      - **Anthropic keys:** Chris edits the `.secret.yaml` in `$EDITOR`, then `rm` the sealed sibling and `./sign.sh`. The gateway watches its key files, so no restart.
      - **Gateway tokens:** maintenance on → regenerate both in one shell → reseal → `rollout restart` the gateway and conductor → maintenance off.
      - **OIDC client secret:** Task 5 Step 2 again, with the resourceVersion wait and a Dex restart.
      - **Admin key:** reseal; the next CronJob run reads it.
      - **B2 key:** reseal, then restart the replica pod, `patronictl switchover`, then restart the former primary, one pod at a time (S3).
  - **CLAUDE.md:**
    - **The topology row:** "Luma | `luma.chrismiller.xyz`, ns `luma-system` | chart `luma` (Argo `luma-release` + `luma`); runbook `docs/luma.md`; **maintenance + `shed: true` before any drain**."
    - **S5 gains one line:** "Drains: Luma first: maintenance on, then commit `shed: true` (docs/luma.md)."
  - **`talos-upgrade` skill:**
    - **Preflight:** run `… /luma-core maintenance on --reason talos-upgrade`, wait for `running_turns=0` (120 s at most), then commit `shed: true` and wait until the Luma Deployments show 0/0.
    - **After the last node:** `shed: false`, Ready, then `maintenance off`.
    - **When to stop:** maintenance not at 0 running turns within 120 s.
  - **`ceph-ssd-pool` skill:** the same two hooks around its per-node `kubectl drain` / power-off step.
  - **`docs/backups.md`:**
    - the Postgres tier row becomes "eight clusters";
    - a WAL-G note for `acid-luma`: the only PITR cluster; path H2; the **dedicated** key; full base backups only; the daily canary; the 30-day lifecycle; and whether Spilo runs as root (the Task 4 Step 8 branch, and why);
    - the drill row (Task 12).

- [ ] **Step 1: Write the docs.**
- [ ] **Step 2: Test maintenance mode.** It touches no Ceph or etcd.
  - Run `kubectl -n luma-system exec deploy/luma-core -- /luma-core maintenance on --reason drill`. Expected: `status` prints `enabled=true running_turns=0 open_hosts=0`, and the PWA shows "Luma is down for maintenance. Try again later."
  - Then `maintenance off`. Expected: a new message works.
- [ ] **Step 3: Test `shed` once, so the drain procedure is proven.**
  1. Commit `shed: true` and push. Expected: Argo scales every Luma Deployment to 0, the CronJobs suspend, `acid-luma` keeps running, and `LumaShedLeftOn` stays pending.
  2. Commit `shed: false`. Expected: every pod is Ready again, and `core link up` is logged.
- [ ] **Step 4: Commit and push** with `git commit -m "luma: runbook, CLAUDE.md topology and S5, maintenance + shed in node procedures, backups doc"`.

### Task 12: `acid-luma` restore drill (PITR clone and logical)

**Files:**
- Create: `docs/backups/acid-drill-luma.yaml` (hand-applied, never Argo)
- Modify: `docs/backups.md` (drill log)

**Interfaces:**
- Produces: a timestamped drill-log row proving both restore paths.

- [ ] **Step 1: Write a marker and note the time.**
  - Set `P=$(kubectl -n luma-system get pod -l galaxy=acid-luma,spilo-role=master -o name)`, the current primary, which may have moved since Task 4.
  - Run `kubectl -n luma-system exec "$P" -c postgres -- psql -U postgres -d luma -c "insert into settings(key,value,updated_at) values ('drill-2026-MM-DD', '{}', now())"`. It is the only write, and the key is inert.
  - Note T = now (UTC) + 1 minute. `Monitor` until `pg_stat_archiver.last_archived_time` > T. That takes at most 5 min: the insert is WAL activity, so `archive_timeout` switches.
- [ ] **Step 2: Write `acid-drill-luma.yaml`.** It has `docs/backups/acid-drill.yaml`'s shape, in `luma-system`, labelled `backup-drill: "true"`, with **no `WAL_S3_BUCKET`** (the drill never archives) and no node affinity (H10):
  ```yaml
  spec:
    clone:
      cluster: acid-luma
      timestamp: "<T as 2026-MM-DDTHH:MM:SS+00:00>"   # +00:00, not Z (CRD regex)
      s3_wal_path: s3://galaxy-cluster-backups/spilo/acid-luma/wal-g-1/wal
      s3_endpoint: https://s3.us-west-004.backblazeb2.com
      s3_force_path_style: true
    env:
      - {name: CLONE_AWS_REGION, value: us-west-004}
      - name: CLONE_AWS_ACCESS_KEY_ID
        valueFrom: {secretKeyRef: {name: logical-backup-s3, key: AWS_ACCESS_KEY_ID}}
      - name: CLONE_AWS_SECRET_ACCESS_KEY
        valueFrom: {secretKeyRef: {name: logical-backup-s3, key: AWS_SECRET_ACCESS_KEY}}
    volume: {size: 10Gi, storageClass: rook-ceph-block-ssd}
  ```
  `CLONE_TARGET_INCLUSIVE` defaults to true on Spilo 3.2.
- [ ] **Step 3: Restore by PITR.**
  - Run `kubectl apply -f docs/backups/acid-drill-luma.yaml`, then `Monitor` `kubectl -n luma-system get postgresql acid-drill` until Running.
  - In `acid-drill-0`, run `psql -U postgres -d luma -Atc "select count(*) from settings where key like 'drill-%'"`. Expected: `1`.
  - Then, on both clusters, run `select count(*) from sessions where created_at <= 'T'` and the same for `messages`, `events` and `memory_writes`. Expected: equal counts. Production keeps writing after T, so only rows up to T are compared.
- [ ] **Step 4: Restore logically** (Chris confirms deletes, S4).
  1. Delete the drill CR, then the PVC `pgdata-acid-drill-0`.
  2. Re-apply the drill **without** the `clone:` block, as an empty cluster.
  3. Run `docs/backups/pg-restore.sh luma-system acid-luma acid-drill`. Expected: the target and source row counts agree as of the latest dump. The first dump exists after one night.
- [ ] **Step 5: Clean up** (S4; confirm each).
  1. `kubectl delete -f docs/backups/acid-drill-luma.yaml && kubectl -n luma-system delete pvc pgdata-acid-drill-0`.
  2. The StorageClass is Retain, so list `kubectl get pv | grep -E 'Released.*luma-system/pgdata-acid-drill'`, and **delete those PVs** after Chris confirms.
- [ ] **Step 6: Log the drill and commit.** Add the drill-log row: date, both paths, T, and the counts. Then `git add docs/backups/acid-drill-luma.yaml docs/backups.md && git commit -m "backups: acid-luma PITR and logical restore drill"`.

### Task 13: Phase-1 exit criteria (§14)

**Files:** Modify `docs/luma.md` (burn-in log), and the luma repo's `docs/spikes/phase-1.md` (final status).

- [ ] **Step 1: A week of daily use on Android (Chris).**
  - Install from Chrome, then run the product checklist on the real phone:
    - force-stop the app: still signed in;
    - background it for 60 s mid-answer: complete, with no duplicates;
    - camera and PDF attachments;
    - airplane mode, then back: it reconnects.
  - One line per day in the burn-in log: what broke, and that day's cost.
- [ ] **Step 2: Check the SLO.** Run `sum(increase(luma_main_request_tokens_total{slo="included",type="cache_read"}[7d])) / sum(increase(luma_main_request_tokens_total{slo="included"}[7d]))`. Expected: ≥ 0.80. Below that, investigate `cache_creation` spikes (§12.1.8) before declaring phase 1 done.
- [ ] **Step 3: Check the regression tests.** Seven consecutive nightly runs passed, and no `LumaThinkingBinding` alert has fired.
- [ ] **Step 4: Check the data path.**
  - The restore drill is logged (Task 12).
  - The WAL-G canary has been green for 7 days, and there are ≥ 7 base backups.
  - `patronictl list` still shows a `Sync Standby`.
  - The first nightly `logical-backup-acid-luma` run succeeded (`BackupCronJobStale` stays quiet).
- [ ] **Step 5: Confirm the edge snapshot includes `luma`** (Task 8).
- [ ] **Step 6: Confirm the spikes are resolved.** Product Spike G and Spike S, and live L1–L5, are each recorded with the branch taken (gateway byte-diff, SessionStore resume prefix, TTL env var, thinking replay).
- [ ] **Step 7: Measure and right-size.**
  - Read per-CLI RSS from the dashboard, then set `conductor.resources.requests.memory` to ≈ 4 × measured + 200 Mi + 256 Mi (emptyDir).
  - `usage_reconciliations`: 7 days, each within 10 % or $0.50 of the ledger. Chris compares one month-to-date figure with the Console, which is ground truth (§12.4).
- [ ] **Step 8: Close out.** Write "Phase 1 done" with the date in `docs/luma.md`, then `git commit -m "luma: phase 1 exit criteria met"`.

---

## What to watch, and rollback

- **Rollback (full), in this order (H12):**
  1. **Empty the workloads first.** In one commit:
     - set `enabled: false` in `luma-release.yaml`'s `valuesObject`, so the chart renders nothing;
     - cut `cluster/luma/kustomization.yaml` down to `namespace.yaml` and `psql.yaml`.

     Push, then `Monitor` until `kubectl -n luma-system get deploy,cronjob,ingress` lists only the operator's `logical-backup-acid-luma`.
  2. **Then remove the apps.** Take `luma.yaml` and `luma-release.yaml` out of `cluster/applications/kustomization.yaml`, and push. There is no resources-finalizer, so this deletes only the Application objects.
  3. **`acid-luma` and the namespace remain** (`Prune=false,Delete=false`). Deleting them, then their Retain PVCs and PVs, needs Chris's word (S4).
  4. **Dex and the edge:** take Task 5's revert path, and remove the `luma` line from `edge-snapshot.sh`.
  5. **Keys:** Chris deletes the two workspace keys, the Admin key and the B2 key in their consoles.
- **Rollback (chart only):** revert the commit that bumped `targetRevision`. It also carries the agents copy (Task 10 Step 4.3). A conductor restart fails any open turn visibly; conversations resume from the store (§6).
- **During a drain, Talos hop or node power-off:** maintenance on, then `shed: true` (H11), with the hooks in the talos-upgrade and ceph-ssd-pool skills and CLAUDE.md S5.
- **Cost runaway:** the workspace hard limits ($150/$20) stop it at Anthropic, and Core refuses new turns at its $150 cap. To freeze Luma immediately, use maintenance mode; to stop everything, `shed: true`.
- **What is NOT changed:** Traefik's configuration, DNS, the router, other namespaces' workloads, the oauth2-proxy tiers, and the MCP gateway.

## Spec coverage and self-review

| Spec item | Task(s) |
|---|---|
| §5.2.5 ledger, Usage API reconcile, 80 % warning | 1 (Admin key), 2, 7 (reconcile CronJob, first run), 9 (drift and anomaly alerts, dashboard), 13 |
| §5.2.6 maintenance; Luma down during every drain | 11 (runbook, both skills, CLAUDE.md S5, the `shed` drill), H11 |
| §5.2.7 retention (backups ≤ ~31 days) | 4 (H2, H4 lifecycle, full base backups), 11 (backups doc) |
| §5.2.8 `acid-luma`: SSD, `synchronous_commit`, PITR, drill | 4, 9, 12, H9, H10 |
| §8.1 static client `luma-core` | 5 |
| §8.2 registry (pilot) | 5, H7 |
| §12.4 gateway: 2 replicas + PDB, keys only there, workspaces, rules in `/api/v1/rules` | 1, 2, 7, 9 |
| §13.2 smokescreen `web` deny list | 7 (values and checks), H5, Review Focus 1 |
| §13.3 logs stay home | P3 (b), 8 (access logs off), 4 (`log_parameter_max_length*`) |
| §13.4 evals in-cluster on `luma-ci`, gate | 3, 10 |
| §14 phase-1 exit criteria | 13 |
| §17 phase-1 live spikes | 3 (L1–L5) |

**Self-review notes:**
- **Seam:** every Secret name, key, chart value and env name used here matches product C6, C8 and C10. The two names this plan supplies at execution are the gateway request counter (from product §G) and L5's error text (from Task 3).
- **Placeholders:** they are only the `__PASTE_…__` and `__CHRIS_…__` markers, which Chris replaces, and digests resolved at execution. Each has a verification step that fails while it is still present.
- **Proportion:** about 10.9k words against the spec's 11.9k. The growth from the review draft (6.9k) is the secret-handling, WAL-G, Dex-verification and rollback findings.
- **Final review round (2026-10-19):**
  - The `NotIn [middle]` affinities on `acid-luma`, Core, the conductor and the drill are gone. The two members' spread is verified, and uses `topologySpreadConstraints` only if the CRD has the field.
  - Spilo pods are selected with the operator's `cluster_name_label`, `galaxy=acid-luma`, as in `docs/media.md`. The earlier `cluster-name=` selectors matched nothing.
  - The Dex client and the registry take `google` and `microsoft`, connector-pinned, and both are logged in live at Task 8.
  - Patroni REST is verify-only, pointing at its own plan.
  - The root-Spilo branch has its own verification, and Step 11 is its real proof.

## Open questions (Chris)

**Answered in the final review round (2026-10-19):**
- **Registry and `allowedConnectors`:** Google **and** Microsoft. Both pairs are pinned in the registry, and `allowedConnectors: [google, microsoft]` (H7, Task 5).
- **Patroni REST without auth:** fix it now for every `acid-*` cluster, in its own plan, `docs/superpowers/plans/2026-10-19-patroni-rest-auth.md`. This plan only verifies, and proceeds regardless (Task 4 Step 9).
- **Spilo's cron under uid 101:** if it doesn't run, start `acid-luma` as root. The branch is written out in Task 4 Step 8.
- **`middle`:** assume its memtest is fine. Every placement exclusion is dropped (H10). memtest not run; Chris accepted the RAM risk 2026-10-19 (see docs/luma.md).

**Still open:**
1. **The drift alert threshold:** 10 % **and** $0.50 per workspace-day (Task 9). Tighter or looser?
