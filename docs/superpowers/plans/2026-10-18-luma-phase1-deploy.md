# Luma phase 1 on galaxy: deploy and wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** plan, 2026-10-18. Not approved, not executed.

**Goal:** Luma phase 1 ("Lean chat") runs in `luma-system` at `https://luma.chrismiller.xyz` for Chris only, which gives:
- its own SSD Postgres with PITR;
- Dex BFF login;
- an agentgateway holding the only Anthropic keys;
- smokescreen `web` for WebFetch;
- nightly regression tests on `luma-ci`;
- alerts and a cost dashboard.

It then meets every phase-1 exit criterion.

**Architecture:**
- **Two Argo apps, following the repo's pair pattern** (`telephony-release` + `telephony`):
  - `luma-release` pins the published `luma` chart, which renders Core, the conductor, agentgateway, smokescreen `web` and the eval CronJobs. Its images are pinned by digest inside the chart.
  - `luma` (path `cluster/luma`) holds the site's pieces: namespace, `acid-luma`, SealedSecrets, the agents/models/evals ConfigMaps, the Ingress, the PodMonitor and the PrometheusRules (in namespace `prometheus`).
- **Elsewhere in this repo:** Dex gains one static client, the dashboard lives in `cluster/grafana`, and the runbooks and skills learn maintenance mode.

**Tech Stack:** Argo CD, Helm chart `luma` from `https://christopherjmiller.github.io/luma`, Zalando postgres-operator v1.11 (Spilo 16) with WAL-G to B2, Sealed Secrets + git-crypt, Dex (v2.46 after phase 0a), Traefik, kube-prometheus-stack.

**Spec:** `docs/superpowers/specs/2026-10-18-luma-design.md`. This plan covers §14 phase 1 and draws on §5.2.8, §12.4, §13.2, §13.3, §13.4 and §17.
- **Product plan (the code):** `~/Repos/luma/docs/plans/phase-1-lean-chat.md`. Its contracts C6 (Core env) and C8 (chart values) are what this plan sets.
- **House style:** `docs/superpowers/plans/2026-10-15-patchbay-deploy.md`.

## Global Constraints

**Safety rules (CLAUDE.md)**
- **S1:** read live state before every write: `kubectl get/describe`, Argo app status, VyOS `show`.
- **S2:** everything goes through git. The exceptions are `rollout restart`, the hand-applied spike and drill objects (never Argo, deleted after), and `kubectl exec` of `luma-core maintenance`.
- **S3:** use `Monitor` for anything over 60 s.
- **S4:** confirm with Chris before deleting a PVC, PV, Namespace or StatefulSet, and before an Argo app delete.
- **S5:** no Luma rollout during a drain or Talos hop.
- **S6:** never `--no-verify`.
- **S7:** Ceph is look-only.
- **S9:** never print, paste or commit-message a secret.
  - Secrets are described **by file and key path only**.
  - Use `git diff --no-textconv` / `git show --no-textconv` for anything that leaves the terminal, because the unlocked repo prints decrypted `*.secret.*`.
  - Generated values go straight from a shell variable into the file (`yq … strenv(X)`) and are then `unset`.

**Images (sharp edge #6)**
- Luma images are ones we build. The chart release pins them by digest, so this repo pins **only the chart version**.
- A Renovate chart bump **is** a deploy. It never automerges, and it waits for the eval gate (Task 10).

**Fixed values (spec)**
- **Namespace:** `luma-system`.
- **Postgres:** `acid-luma` on `rook-ceph-block-ssd`, `synchronous_commit=on`, WAL-G archiving to B2 for PITR plus the nightly logical dump, and a restore drill into `acid-drill`.
- **Anthropic workspaces:**
  - `luma-interactive` (limit **$150**) and `luma-ci` (limit **$20**). Workspace limits must sum to ≤ $200 with the later `luma-house` ($10) and `luma-background` ($10).
  - Keys live only in the gateway's Secret.
  - The conductor↔gateway credential is a static internal bearer (§12.4).
- **agentgateway:** **2 replicas + PDB**, ClusterIP only, Traefik untouched. ServiceMonitors carry `release: prometheus`.
- **PrometheusRules:** in namespace `prometheus`, then checked in `/api/v1/rules` (§12.4).
  - **Labels follow the 0b convention:** `release: prometheus`. Phase 0b flips `ruleSelector` to `{matchLabels: {release: prometheus}}` with `ruleNamespaceSelector: {}`.
  - The legacy `prometheus: kube-prometheus` / `role: alert-rules` labels are carried as well, so the rule loads whether or not the flip has landed.
  - The deploy check is 0b's `scripts/check-prometheus-rules.sh`.
- **smokescreen `web` deny list:**
  - our domains: `chrismiller.xyz`, `realliance.net`, `werethemille.rs` and `buttert.art`, each apex and `*.`;
  - RFC1918, `100.64.0.0/10`, `169.254.0.0/16`;
  - the **WAN IP**;
  - IPv6 `fc00::/7`, `fe80::/10`, `::ffff:0:0/96`, plus NAT64, 6to4 and Teredo;
  - the curated `exfil-deny` list.
- **Ingress:** `luma.chrismiller.xyz` is one Ingress with a host and **no oauth2-proxy middleware**, because Core is the BFF (D39). It has no `spec.tls` and no DNS/cert annotations (edge-ingress skill).
- **Logs:** fluent-bit excludes `luma-*` (phase 0b; verified here).
- **Maintenance:** Luma is shed during every drain. The talos-upgrade procedure turns maintenance mode on first.
- **Repo:** the luma repo is public; nothing from this repo's secrets reaches it.

## Review Focus

These are the five failure modes most likely to bite, which no step below would otherwise catch. Each has its check in the named task.

1. **The WAN IP changes (DHCP)** and the smokescreen `denyAddresses` entry goes stale, so a model-chosen fetch to `http://<new WAN IP>/` hairpins through NAT reflection rules 210/310 into Traefik. Pinned by `scripts/check-wan-ip.sh` plus a pre-commit hook comparing it with `router/ansible/group_vars/vyos_routers.yml` (Task 6). The runbook covers a live `dig` check.
2. **`sign.sh` silently skipping a file that already has a sealed sibling.** The Dex client then never reaches the cluster, and login fails with "Unregistered client" while git looks right. Task 5 deletes `cluster/dex/config.yaml` before sealing, and verifies Dex's behaviour, not the file.
3. **WAL-G archiving failing quietly** (B2 auth, path, endpoint) until `pg_wal` fills the 10 Gi SSD volume and Postgres PANICs. Task 4 verifies a pushed segment and a base backup in B2. Task 9 alerts on `pg_stat_archiver_failed_count` and the age of the last archive.
4. **Monitoring objects that are inert:** a ServiceMonitor or PodMonitor without `release: prometheus`, or a rule outside `prometheus` (known: ruleSelector, servicemonitor release label). Task 9 checks `/api/v1/targets` and `/api/v1/rules`, not the YAML.
5. **The conductor's 1.8 GiB request landing on a node without headroom.** Nodes sit at 89–91 % requested today (2026-10-18). That leaves it Pending, or squeezes BestEffort household pods. P3 gates on phase 0b's headroom; Task 7 reads per-node allocation before syncing and prefers nodes other than `middle`.

## 0. Preconditions (each is a go/no-go)

| # | Check | State 2026-10-18 | Needed |
|---|---|---|---|
| P1 | Ceph back to its usual WARN noise | PG 1.c repair started today (spec §2) | ceph-doctor run clean (S7) |
| P2 | Phase 0a done: Dex v2.46 with the expiry block, and consent | Dex `v2.44.0` live | 0a exit criteria met. If not, Task 5 omits `allowedConnectors` (a v2.46 key) and everything else stands. |
| P3 | Phase 0b done (`docs/superpowers/plans/2026-10-18-luma-0b-platform-prep.md`): headroom measured, ruleSelector fix, fluent-bit `luma-*` exclusion | requests 27.7–28.3 GiB per node (89–91 %); no `Exclude_Path` in `cluster/applications/fluent-bit.yaml` | ≥ 3.3 GiB of new request room, with ≥ 2.2 GiB on one non-`middle` node for the conductor plus Core. The live fluent-bit ConfigMap contains `Exclude_Path /var/log/containers/*_luma-*_*.log` (0b Task 2, with its canary proof). `scripts/check-prometheus-rules.sh` passes. 0b's throwaway `luma-system` namespace is gone. |
| P4 | A luma chart release exists | none (product plan Task 29) | `v0.1.0-rc.1` with `ci-ok` green. `helm search repo luma --devel` finds it, and `gh attestation verify` passes for the four image digests in its `values.yaml`. |
| P5 | Namespace `luma-system` absent | absent (checked 2026-10-18) | re-check |
| P6 | Argo otherwise healthy | (re-check) | no app OutOfSync or Degraded beyond the known `rook`/`rook-cluster` baseline |
| P7 | No IPv6 on the WAN | no IPv6 config in `router/ansible` | `ssh chris@192.168.0.1 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show interfaces'` shows no global IPv6 on eth0. If one exists, add its prefix to `egress.web.extraDenyRanges` (Task 7). |

## Decisions this plan makes

| # | Decision | Why |
|---|---|---|
| H1 | `luma-system` enforces PSS **`baseline`**, with `warn`/`audit` at `restricted`. Luma's own pods are restricted-compliant (the product chart test checks this). | Spilo pods from the operator do not set the restricted fields; `acid-gramps`' namespace is `baseline` for the same reason. |
| H2 | WAL-G config is per cluster, through `spec.env`: `WAL_BUCKET_SCOPE_PREFIX=""` and `WAL_BUCKET_SCOPE_SUFFIX=/wal-g-1`, so the path is `s3://galaxy-cluster-backups/spilo/acid-luma/wal-g-1/wal/16/`. Bump the suffix (`/wal-g-2`) for any in-place rebuild so it never overwrites the old history. It reuses the existing no-delete key `galaxy-pg-cluster` (`logical-backup-s3`). | The operator adds `/<uid>` only for operator-wide buckets. Spilo's default per-cluster prefix is `<namespace>-`. The bucket's `spilo/` lifecycle (hidden at 30 days) gives the spec's "≤ 30 days". |
| H3 | `archive_timeout: 300s` (Spilo's default is 1800 s). | 5-minute RPO on a quiet DB, without forcing a 16 MiB segment switch every minute on the shared SSD pool. |
| H4 | Base backups use `BACKUP_SCHEDULE: "15 5 * * *"` (the default is 01:00, inside the restic window) and `BACKUP_NUM_TO_RETAIN: "30"`. The retention delete may fail with the no-delete key, and the backup still runs (Spilo's `postgres_backup.sh`); the lifecycle rule ages them out. | Keeps clear of the 00:30–04:30 backup window. |
| H5 | The `exfil-deny` list is the chart's default. Our domains, **`discord.com`/`discordapp.com`** (the spec's "policy call") and the WAN IP are site values in `luma-release.yaml`. The ACL is chart-rendered, so a change rolls smokescreen; it has no hot reload. | A generic list belongs to the product; house names belong here. |
| H6 | The live halves of the §17 spikes run **before** the full stack, as a hand-applied Job (Task 3): the pinned agentgateway plus a curl probe on `luma-ci`. | The spikes come first, and they cost cents. |
| H7 | Registry phase 1 holds Chris only, with his Google address under connector `google`. It is a SealedSecret, because the repo is public. | §8.2 (pilot). The generator arrives in phase 2. |
| H8 | `x-luma-*` request headers are forwarded to Anthropic. They carry pseudonymous person, agent and tier ids. | agentgateway reads metric labels from the request as sent upstream (product plan Task 3). |

## File map

| File | New? | What |
|---|---|---|
| `cluster/applications/luma.yaml` | new | Argo app: path `cluster/luma` → `luma-system`, automated, prune, selfHeal |
| `cluster/applications/luma-release.yaml` | new | Argo app: chart `luma`, `targetRevision` pinned, `valuesObject` (Task 7) |
| `cluster/applications/kustomization.yaml` | edit | + both apps |
| `cluster/luma/kustomization.yaml` | new | **no `namespace:` transformer** (the rules go to `prometheus`); `configMapGenerator` `luma-config`, `luma-evals` (`disableNameSuffixHash: true`) |
| `cluster/luma/namespace.yaml` | new | H1 |
| `cluster/luma/psql.yaml` | new | `acid-luma` (Task 4) |
| `cluster/luma/postgres-podmonitor.yaml` | new | copy of `cluster/home-assistant/postgres-podmonitor.yaml`, selector `galaxy: acid-luma` |
| `cluster/luma/logical-backup-s3.{secret.yaml,yaml}` | new | the same two keys as `cluster/gramps/logical-backup-s3.secret.yaml`, in `luma-system` |
| `cluster/luma/anthropic.{secret.yaml,yaml}` | new | Secret `luma-anthropic`: `ANTHROPIC_KEY_INTERACTIVE`, `ANTHROPIC_KEY_CI` |
| `cluster/luma/internal.{secret.yaml,yaml}` | new | Secret `luma-internal`: `CONDUCTOR_TOKEN`, `GATEWAY_TOKEN`, `EVAL_TOKEN` |
| `cluster/luma/core.{secret.yaml,yaml}` | new | Secret `luma-core`: `OIDC_CLIENT_SECRET`, `SESSION_SIGNING_KEY` |
| `cluster/luma/vault.{secret.yaml,yaml}` | new | Secret `luma-vault`: `VAULT_KEY`. §5.2.8 creates it now; Core first reads it in phase 3. |
| `cluster/luma/registry.{secret.yaml,yaml}` | new | Secret `luma-registry`: `registry.yaml` (H7) |
| `cluster/luma/agents/{luma,deep-think}/AGENT.md`, `cluster/luma/agents/*/evals/*.yaml`, `cluster/luma/models.yaml` | new | copied from the luma repo at the pinned tag |
| `cluster/luma/ingress.yaml` | new | Task 8 |
| `cluster/luma/prometheusrule.yaml` | new | `metadata.namespace: prometheus` (Task 9) |
| `cluster/grafana/dashboards/luma.yaml` (+ `cluster/grafana/kustomization.yaml`) | new / edit | Task 9 |
| `cluster/dex/config.secret.yaml` (+ resealed `config.yaml`) | edit | static client `luma-core` (Task 5) |
| `docs/luma/spike-live-job.yaml`, `docs/luma/spike-live.sh` | new | hand-applied, never Argo (Task 3) |
| `docs/backups/acid-drill-luma.yaml` | new | the PITR clone drill, hand-applied (Task 12) |
| `scripts/check-wan-ip.sh`, `.pre-commit-config.yaml` | new / edit | H5, Review Focus 1 |
| `.claude/skills/edge-ingress/edge-snapshot.sh` | edit | add `luma` |
| `.claude/skills/talos-upgrade/SKILL.md` | edit | maintenance mode on and off around node work |
| `.renovaterc.json` | edit | the `luma` chart never automerges |
| `docs/luma.md` (the existing entry page), `docs/backups.md`, `CLAUDE.md` | edit | Task 11 |

---

### Task 1: Anthropic workspaces and keys (manual: Chris, in the Console)

**Files:** none in git.

**Interfaces:**
- Produces two key files on rowlett, outside the repo: `~/.config/luma/anthropic-interactive.key` and `~/.config/luma/anthropic-ci.key` (mode 600). Task 2 reads them; nothing else does.

- [ ] **Step 1 (Chris): create the workspaces.** In console.anthropic.com → Workspaces, create `luma-interactive` with a **monthly spend limit of $150** and `luma-ci` with **$20**.
- [ ] **Step 2 (Chris): create one API key per workspace**, named `luma-gateway-interactive` and `luma-gateway-ci`. Save each into its file (`install -m600 /dev/stdin ~/.config/luma/anthropic-<ws>.key`, pasting into stdin) and into the password manager. **Never into the chat.**
- [ ] **Step 3: Verify without printing the keys.** Run `stat -c '%a %s' ~/.config/luma/anthropic-*.key`. Expected: `600` and a non-zero size for both. Chris confirms in the Console that the limits read $150 and $20.

### Task 2: Namespace, the two gateway Secrets, and Argo app `luma`

**Files:**
- Create: `cluster/luma/{kustomization.yaml,namespace.yaml,anthropic.secret.yaml,anthropic.yaml,internal.secret.yaml,internal.yaml}`, `cluster/applications/luma.yaml`
- Modify: `cluster/applications/kustomization.yaml`

**Interfaces:**
- Consumes: Task 1's key files.
- Produces:
  - Secrets `luma-anthropic` and `luma-internal` in `luma-system`, with the key names in C8 of the product plan.
  - The Argo app `luma`. Its later tasks only add files to `cluster/luma/`.

- [ ] **Step 1 (S1): read the current state.** Run `kubectl get ns luma-system`. Expected: NotFound. Run `kubectl -n argo-cd get applications.argoproj.io -o wide`. Expected: the P6 baseline.
- [ ] **Step 2: Write the namespace and the app.**
  - `namespace.yaml`: `luma-system`, with labels `pod-security.kubernetes.io/enforce: baseline`, `warn: restricted`, `audit: restricted`.
  - `cluster/applications/luma.yaml`: `mcp-gateway.yaml`'s shape, with path `cluster/luma`, destination `luma-system`, `syncOptions: [CreateNamespace=true, PrunePropagationPolicy=foreground, PruneLast=true]`.
  - `kustomization.yaml` gets the comment "No `namespace:` transformer on purpose: prometheusrule.yaml lands in `prometheus`" (the `cluster/backups` precedent).
- [ ] **Step 3: Write the Secrets without echoing anything.**
  ```bash
  K_INT=$(cat ~/.config/luma/anthropic-interactive.key) K_CI=$(cat ~/.config/luma/anthropic-ci.key) \
    yq -n '.apiVersion="v1" | .kind="Secret" | .metadata.name="luma-anthropic" | .metadata.namespace="luma-system"
           | .stringData.ANTHROPIC_KEY_INTERACTIVE=strenv(K_INT) | .stringData.ANTHROPIC_KEY_CI=strenv(K_CI)' \
    > cluster/luma/anthropic.secret.yaml
  CT=$(openssl rand -hex 32) GT=$(openssl rand -hex 32) ET=$(openssl rand -hex 32) \
    yq -n '... luma-internal ... .stringData.CONDUCTOR_TOKEN=strenv(CT) | .stringData.GATEWAY_TOKEN=strenv(GT) | .stringData.EVAL_TOKEN=strenv(ET)' \
    > cluster/luma/internal.secret.yaml
  ```
- [ ] **Step 4: Seal.** Run `KUBECONFIG=/tmp/galaxy-kubeconfig nix develop --command ./sign.sh`. Expected: it prints both paths, and `cluster/luma/{anthropic,internal}.yaml` exist with `kind: SealedSecret`.
- [ ] **Step 5: Gate on pre-commit.** Run `nix develop --command pre-commit run --files cluster/luma/* cluster/applications/*`. Expected: PASS, including `kustomize-build`.
- [ ] **Step 6: Commit and push to main** (Chris's preference; Argo syncs main).
  ```bash
  git add cluster/luma/kustomization.yaml cluster/luma/namespace.yaml cluster/luma/anthropic.secret.yaml cluster/luma/anthropic.yaml \
          cluster/luma/internal.secret.yaml cluster/luma/internal.yaml cluster/applications/luma.yaml cluster/applications/kustomization.yaml
  git commit -m "luma: luma-system namespace, gateway key and internal-token SealedSecrets"
  git push
  ```
- [ ] **Step 7: Verify** with `Monitor` until done: `kubectl -n argo-cd get app luma` reaches Synced/Healthy, then `kubectl -n luma-system get secret luma-anthropic luma-internal -o name`. Expected: both exist. **Do not print `.data`.**

### Task 3: Live spikes, the real-API halves of §17 (H6)

**Files:**
- Create: `docs/luma/spike-live-job.yaml`, `docs/luma/spike-live.sh`
- Modify (luma repo): `docs/spikes/phase-1.md` §L

**Interfaces:**
- Consumes:
  - the luma repo's `test/gateway/agentgateway.yaml` at the commit Spike G recorded, as a ConfigMap, with its fake `hostOverride` removed;
  - `luma-anthropic` and `luma-internal`.
- Produces: four recorded results with their branches applied. **Each line prints names and numbers only, never content.**

The Job is one pod with two containers:
- `gateway`, from the image pinned in luma `Makefile` `AGENTGATEWAY_IMAGE`. It mounts `luma-anthropic` as files at `/run/secrets/anthropic/` (`items`: `ANTHROPIC_KEY_INTERACTIVE` → `interactive`, `ANTHROPIC_KEY_CI` → `ci`) and takes `GATEWAY_TOKEN` from `luma-internal` (`secretKeyRef`), the same layout the chart uses;
- `probe`, from `nicolaka/netshoot` pinned by tag and digest (it has curl and jq). The probe runs `spike-live.sh` against `http://127.0.0.1:8080/ci` with the `GATEWAY_TOKEN` bearer.

The script builds a synthetic system prompt (about 6,000 tokens of generated filler, well over Haiku 5.5's minimum cacheable prefix) and sends streamed `claude-haiku-5-5` requests with `max_tokens: 16`.

- [ ] **Step 1: Write the Job and script.**
  - `activeDeadlineSeconds: 600`, `backoffLimit: 0`, `restartPolicy: Never`, a restricted securityContext.
  - It prints `SPIKE <id> PASS|FAIL <numbers>` lines:
    - **L1 `ttl_1h`:** the request carries `cache_control: {type: ephemeral, ttl: "1h"}`. PASS if `message_start.message.usage.cache_creation.ephemeral_1h_input_tokens > 0`.
    - **L2 `identical_read`:** the same body sent again. PASS if `cache_read_input_tokens > 0`.
    - **L3 `detect_usage`:** curl `127.0.0.1:15020/metrics`. PASS if `agentgateway_gen_ai_client_token_usage` has `gen_ai_token_type="input_cache_read"` samples under the `detect` route. Then print which token types appeared.
    - **L4 `effort_per_message`:** L2's body plus an assistant turn and a user turn, followed by `{"role":"system","content":[],"output_config":{"effort":"medium"}}`, with `anthropic-beta: mid-conversation-output-config-2026-07-01`. PASS if `cache_read_input_tokens ≥` L2's.
- [ ] **Step 2: Run it.** `kubectl apply -f docs/luma/spike-live-job.yaml`, then `Monitor` on `kubectl -n luma-system logs -f job/luma-spike-live -c probe` until it exits. Then `kubectl delete -f docs/luma/spike-live-job.yaml`. Expected: four `SPIKE` lines. Cost is under $0.05 on `luma-ci`.
- [ ] **Step 3: Record and branch.** Write the results into luma `docs/spikes/phase-1.md` §L and apply the branch in the luma repo before its release.

  | Result | Branch |
  |---|---|
  | L1 FAIL | Check Spike G's `TestHeadersForwarded` for the TTL beta. If the gateway is at fault, use product Task 3b. Otherwise accept 5m: set `cache_ttl: 5m` in `models.yaml` (Task 6) and record the cost-model change in `docs/luma.md`. |
  | L2 FAIL | **Stop.** A key or gateway misconfiguration: nothing in phase 1 works without cache reads. |
  | L3 shows no input or cache types | If Spike G found `messages` byte-identical, switch `/v1/messages` to `messages`. Otherwise accept: Core is the SLO source (product P10), and the dashboard's per-class panel shows output only. |
  | L4 FAIL | Escalation is the `deep-think` subagent and "continue in Smart" forks only (§17). The PWA ships no mid-session effort control. |
- [ ] **Step 4: Commit** the Job and script here (`git add docs/luma/ && git commit -m "luma: live spike job (TTL, cache read, detect usage, per-message effort)"`), and the spike record in the luma repo.

### Task 4: `acid-luma`, Postgres with WAL-G PITR and a logical backup

**Files:**
- Create: `cluster/luma/{psql.yaml,postgres-podmonitor.yaml,logical-backup-s3.secret.yaml,logical-backup-s3.yaml}`
- Modify: `cluster/luma/kustomization.yaml`

**Interfaces:**
- Produces:
  - Service `acid-luma` (the primary), database `luma` owned by `luma_core`, role `luma_conductor`;
  - credential Secrets `luma-core.acid-luma.credentials.postgresql.acid.zalan.do` and `luma-conductor.acid-luma.credentials.postgresql.acid.zalan.do` (the operator turns `_` into `-`);
  - WAL in `s3://galaxy-cluster-backups/spilo/acid-luma/wal-g-1/wal/16/`;
  - dumps in `spilo/acid-luma/<uid>/logical_backups/`.

- [ ] **Step 1: Copy the logical-backup Secret without printing values.**
  ```bash
  yq '.metadata.namespace="luma-system"' cluster/gramps/logical-backup-s3.secret.yaml > cluster/luma/logical-backup-s3.secret.yaml
  ```
  Check `grep -c '^kind: Secret' cluster/luma/logical-backup-s3.secret.yaml`. Expected: `1`.
- [ ] **Step 2: Write `psql.yaml`**, `cluster/gramps/psql.yaml`'s shape with these differences:
  - `name: acid-luma`, `namespace: luma-system`, `numberOfInstances: 2`, `enableLogicalBackup: true`;
  - `users: {luma_core: [], luma_conductor: []}`, `databases: {luma: luma_core}`;
  - `postgresql.parameters: {synchronous_commit: "on", archive_timeout: "300s"}` (H3), with a comment that this is the SSD pool and that approvals and memory writes must not be lost;
  - `patroni.failsafe_mode: true`;
  - `volume: {size: 10Gi, storageClass: rook-ceph-block-ssd}`;
  - `resources`: requests 50m/256Mi, limits 1/1Gi;
  - the same postgres-exporter sidecar;
  - `spec.env`:
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
      - name: AWS_ACCESS_KEY_ID
        valueFrom: {secretKeyRef: {name: logical-backup-s3, key: AWS_ACCESS_KEY_ID}}
      - name: AWS_SECRET_ACCESS_KEY
        valueFrom: {secretKeyRef: {name: logical-backup-s3, key: AWS_SECRET_ACCESS_KEY}}
    ```
    Names are uppercase, because Spilo reads `os.environ` exactly. `spec.env` takes precedence over the operator's pod environment.
- [ ] **Step 3: Write `postgres-podmonitor.yaml`**, the home-assistant copy with `galaxy: acid-luma`, `namespaceSelector: [luma-system]` and `release: prometheus`.
- [ ] **Step 4: Seal, pre-commit, commit, push.** Run `./sign.sh` (it seals `logical-backup-s3`), then `pre-commit run --files cluster/luma/*`, then:
  ```bash
  git add cluster/luma/psql.yaml cluster/luma/postgres-podmonitor.yaml cluster/luma/logical-backup-s3.secret.yaml cluster/luma/logical-backup-s3.yaml cluster/luma/kustomization.yaml
  git commit -m "luma: acid-luma on the SSD pool, synchronous_commit on, WAL-G PITR to B2"
  git push
  ```
- [ ] **Step 5: Verify the cluster** with `Monitor`. Run `kubectl -n luma-system get postgresql acid-luma`. Expected: `Running`. Run `kubectl -n luma-system get pods -l cluster-name=acid-luma -o wide`. Expected: two pods on two different nodes, and PVCs Bound on `rook-ceph-block-ssd`.
- [ ] **Step 6: Verify archiving.**
  - Run `kubectl -n luma-system exec acid-luma-0 -c postgres -- psql -U postgres -Atc "select pg_switch_wal(); select archived_count, failed_count from pg_stat_archiver"`. Expected: `archived_count ≥ 1` within a minute, and `failed_count = 0`.
  - Take the first base backup by hand:
    ```bash
    kubectl -n luma-system exec acid-luma-0 -c postgres -- su postgres -c \
      'envdir /run/etc/wal-e.d/env /scripts/postgres_backup.sh /home/postgres/pgdata/pgroot/data'
    ```
  - Run `kubectl -n luma-system exec acid-luma-0 -c postgres -- su postgres -c 'envdir /run/etc/wal-e.d/env wal-g backup-list'`. Expected: one `base_…` line.
  - Laptop check with the operator key: `backblaze-b2 ls --recursive b2://galaxy-cluster-backups/spilo/acid-luma/wal-g-1/wal/16/ | head`. Expected: `basebackups_005/` and `wal_005/` objects.
- [ ] **Step 7: Verify the logical backup CronJob.** Run `kubectl -n luma-system get cronjob logical-backup-acid-luma`. Expected: it exists. Its first nightly run is checked in Task 13.

### Task 5: Core's Secrets and the Dex static client `luma-core`

**Files:**
- Create: `cluster/luma/{core.secret.yaml,core.yaml,vault.secret.yaml,vault.yaml,registry.secret.yaml,registry.yaml}`
- Modify: `cluster/dex/config.secret.yaml`; delete and regenerate `cluster/dex/config.yaml`; `cluster/luma/kustomization.yaml`

**Interfaces:**
- Produces:
  - The Dex static client `luma-core` (confidential):
    ```yaml
    - id: luma-core
      name: Luma
      secret: <generated: equals luma-core OIDC_CLIENT_SECRET>
      redirectURIs: [https://luma.chrismiller.xyz/auth/callback]
      allowedConnectors: [google, microsoft]   # v2.46 key — omit if P2 is not met
    ```
  - Secret `luma-core` with keys `OIDC_CLIENT_SECRET` and `SESSION_SIGNING_KEY` (base64 of 32 random bytes).
  - Secret `luma-vault` with `VAULT_KEY` (base64 of 32 random bytes).
  - Secret `luma-registry` with `registry.yaml`:
    ```yaml
    people:
      chris: {display_name: Chris, tier: admin, household: true,
              emails: {google: [<Chris's Google address, as in cluster/oauth2-proxy/emails-admin.secret.yaml>]}}
    ```

- [ ] **Step 1 (S1): read Dex's live client ids, without the secrets.** Run `kubectl -n dex get secret dex-config -o jsonpath='{.data.config\.yaml}' | base64 -d | yq '.staticClients[].id'`. Expected: `oauth2-proxy-admin, immich, oauth2-proxy-family, gramps`, matching git.
- [ ] **Step 2: Generate the client secret once and write it into both files.** It goes in after the `gramps` client, before `connectors:`, then the variable is unset:
  ```bash
  export OIDC=$(openssl rand -hex 32)
  nix develop --command python3 - cluster/dex/config.secret.yaml <<'EOF'   # rowlett has no system python3
  import os, sys; p = sys.argv[1]; s = open(p).read()
  block = ("      - id: luma-core\n        name: Luma\n        secret: " + os.environ["OIDC"] + "\n"
           "        redirectURIs:\n          - https://luma.chrismiller.xyz/auth/callback\n"
           "        allowedConnectors: [google, microsoft]\n\n")
  assert s.count("\n    connectors:\n") == 1; open(p, "w").write(s.replace("\n    connectors:\n", "\n" + block + "    connectors:\n"))
  EOF
  SK=$(openssl rand -base64 32) yq -n '… luma-core … .stringData.OIDC_CLIENT_SECRET=strenv(OIDC) | .stringData.SESSION_SIGNING_KEY=strenv(SK)' > cluster/luma/core.secret.yaml
  VK=$(openssl rand -base64 32) yq -n '… luma-vault … .stringData.VAULT_KEY=strenv(VK)' > cluster/luma/vault.secret.yaml
  unset OIDC
  ```
  `registry.secret.yaml` is written by hand in `$EDITOR`, with Chris confirming the address.
- [ ] **Step 3: Seal** (Review Focus 2). Run `rm cluster/dex/config.yaml && KUBECONFIG=/tmp/galaxy-kubeconfig nix develop --command ./sign.sh`. Expected: it prints `cluster/dex/config.secret.yaml` and the three new `cluster/luma/*.secret.yaml` files.
- [ ] **Step 4: Diff without leaking secrets.** Run `git diff --no-textconv --stat`. Then `git diff cluster/dex/config.secret.yaml | grep '^[+-]' | grep -v secret:`. Expected: only the `luma-core` lines. This stays in the terminal and is never pasted.
- [ ] **Step 5: Pre-commit, commit, push.** Stage the explicit paths. `git commit -m "luma: Dex confidential client luma-core; Core, vault and registry SealedSecrets"`, then push.
- [ ] **Step 6: Restart Dex.** It reads its config at start; this is a rollout restart, allowed under S2. Run `kubectl -n dex rollout restart deploy/dex`, then `Monitor` `kubectl -n dex rollout status deploy/dex`. Expected: `successfully rolled out`.
- [ ] **Step 7: Verify Dex's behaviour, not the file.**
  ```bash
  curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' \
   'https://dex.chrismiller.xyz/auth?client_id=luma-core&redirect_uri=https%3A%2F%2Fluma.chrismiller.xyz%2Fauth%2Fcallback&response_type=code&scope=openid+email&state=x'
  ```
  Expected: `200` (the connector chooser) or `302` to a connector. It must **not** be a 400 page containing "Unregistered" or "Invalid client". Repeat with `redirect_uri=https%3A%2F%2Fevil.example%2F`. Expected: a 400 error page.
- [ ] **Step 8: Check nothing else broke.** Re-check oauth2-proxy, Immich and Gramps sign-in: one browser login each (Chris), and the edge snapshot diff is empty for those hosts.

### Task 6: Site config (agents, models, evals) and the WAN-IP consistency check

**Files:**
- Create: `cluster/luma/agents/luma/AGENT.md`, `cluster/luma/agents/deep-think/AGENT.md`, `cluster/luma/agents/luma/evals/*.yaml`, `cluster/luma/agents/deep-think/evals/*.yaml`, `cluster/luma/models.yaml`, `scripts/check-wan-ip.sh`
- Modify: `cluster/luma/kustomization.yaml`, `.pre-commit-config.yaml`

**Interfaces:**
- Consumes: the luma repo at the chart's tag (`agents/`, `agents/models.yaml`; product Task 6) and the Task 3 branches.
- Produces: ConfigMap `luma-config`, with keys `luma.agent.md`, `deep-think.agent.md` and `models.yaml`, flat because Core's loader reads `*.agent.md`. ConfigMap `luma-evals` holds one key per eval file, `<agent>-<name>.yaml`.
- `scripts/check-wan-ip.sh` exits non-zero unless three values are equal:
  - `cluster/applications/luma-release.yaml` → `egress.web.denyAddresses[0]`;
  - `router/ansible/group_vars/vyos_routers.yml` → `nat.destination_rules[] | select(.rule==210).destination_address`;
  - the same for rule 310.

  With `--live` it also compares against `dig +short chrismiller.xyz @1.1.1.1`.

- [ ] **Step 1: Copy the agents** with `git -C ~/Repos/luma show v0.1.0-rc.1:agents/luma/AGENT.md > cluster/luma/agents/luma/AGENT.md`, and the same for `deep-think`, the evals and `models.yaml`. Apply any Task 3 branch (for example L1's TTL) to `models.yaml` here.
- [ ] **Step 2: Add the generators** to `kustomization.yaml`:
  ```yaml
  configMapGenerator:
    - name: luma-config
      namespace: luma-system
      options: {disableNameSuffixHash: true}
      files: [luma.agent.md=agents/luma/AGENT.md, deep-think.agent.md=agents/deep-think/AGENT.md, models.yaml]
    - name: luma-evals
      namespace: luma-system
      options: {disableNameSuffixHash: true}
      files: [luma-basics.yaml=agents/luma/evals/basics.yaml, …]
  ```
  `disableNameSuffixHash` keeps the name stable, so the kubelet updates the mount and Core's watcher reloads. New sessions pick up the new version (§11).
- [ ] **Step 3: Write `check-wan-ip.sh`** and a local pre-commit hook `wan-ip-consistent` whose `files:` matches the two source files. Test it by temporarily editing the IP in a scratch copy. Expected: the script exits 1 and names both files. Then run it for real. Expected: exit 0.
- [ ] **Step 4: Commit.** Run pre-commit, `git add` the explicit paths, then `git commit -m "luma: agents, models.yaml and evals ConfigMaps; WAN IP consistency check"` and push. Verify with `kubectl -n luma-system get cm luma-config luma-evals`. Expected: both exist.

### Task 7: Argo app `luma-release`, which deploys the chart

**Files:**
- Create: `cluster/applications/luma-release.yaml`
- Modify: `cluster/applications/kustomization.yaml`, `.renovaterc.json`

**Interfaces:**
- Consumes: the chart `luma` `0.1.0-rc.1` (P4), every Secret and ConfigMap above, and the product plan's C8 key names.
- Produces: Deployments `luma-core`, `luma-conductor`, `luma-gateway` (×2) and `luma-egress-web`; Services `luma-core:8080`, `luma-core-internal:8081/9090`, `luma-gateway:8080/15020` and `luma-egress-web:4750/9810`; CronJobs `luma-evals-nightly` and `luma-evals-weekly`; ServiceMonitors; PriorityClass `luma-control-plane` (−5).

- [ ] **Step 1 (S1, Review Focus 5): read per-node allocation.** Run `kubectl describe node top middle bottom | grep -A6 'Allocated resources'`. Expected: at least one non-`middle` node with ≥ 2.2 GiB of memory request room. Otherwise **stop** (P3).
- [ ] **Step 2: Write `luma-release.yaml`**, `telephony-release.yaml`'s shape:
  - `repoURL: https://christopherjmiller.github.io/luma`, `chart: luma`, `targetRevision: 0.1.0-rc.1`, `releaseName: luma`, destination `luma-system`;
  - automated sync with prune and selfHeal, **no `CreateNamespace`** (the `luma` app owns the namespace);
  - `valuesObject`:
    ```yaml
    core:
      publicUrl: https://luma.chrismiller.xyz
      oidc: {issuer: https://dex.chrismiller.xyz, clientId: luma-core}
      timezone: America/Los_Angeles
      budget: {monthlyUsd: 200, warnRatio: 0.8}
      secretName: luma-core
      internalSecretName: luma-internal
      registrySecretName: luma-registry
      database: {host: acid-luma, port: 5432, name: luma, sslmode: require,
                 credentialsSecret: luma-core.acid-luma.credentials.postgresql.acid.zalan.do, conductorRole: luma_conductor}
      config: {existingConfigMap: luma-config}
      resources: {requests: {cpu: 50m, memory: 256Mi}, limits: {memory: 768Mi}}
    conductor:
      maxHosts: 4        # §6: 4 in phase 1
      idleMinutes: 10
      database: {credentialsSecret: luma-conductor.acid-luma.credentials.postgresql.acid.zalan.do}
      resources: {requests: {cpu: 200m, memory: 1800Mi}, limits: {memory: 3Gi}}
      affinity:   # memtest suspect, 88% requested: prefer elsewhere
        nodeAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              preference: {matchExpressions: [{key: kubernetes.io/hostname, operator: NotIn, values: [middle]}]}
    gateway:
      replicas: 2
      anthropicSecretName: luma-anthropic
      resources: {requests: {cpu: 50m, memory: 128Mi}, limits: {memory: 512Mi}}
    egress:
      web:
        # The WAN IP. Must equal router/ansible/group_vars/vyos_routers.yml NAT rules 210/310
        # destination_address (scripts/check-wan-ip.sh, pre-commit). Reflection would hairpin it to Traefik.
        denyAddresses: ["152.44.247.88"]
        acl:
          extraGlobalDenyList: [chrismiller.xyz, "*.chrismiller.xyz", realliance.net, "*.realliance.net",
                                werethemille.rs, "*.werethemille.rs", buttert.art, "*.buttert.art",
                                discord.com, "*.discord.com", discordapp.com, "*.discordapp.com"]   # discord: policy call (§13.2)
        resources: {requests: {cpu: 20m, memory: 64Mi}, limits: {memory: 256Mi}}
    evals:
      enabled: true
      existingConfigMap: luma-evals
      nightlySchedule: "40 5 * * *"    # after the backup window
      weeklySchedule: "10 6 * * 1"
    serviceMonitor: {enabled: true, labels: {release: prometheus}}
    ```
    Add P7's IPv6 prefix to `egress.web.extraDenyRanges`, if there is one.
- [ ] **Step 3: Add the Renovate rule** in `.renovaterc.json`: `matchManagers: ["argocd"]`, `matchPackageNames: ["luma"]`, `automerge: false`, for **every** update type. Its description: "A luma chart bump changes the Agent SDK, CLI or prompts; it waits for the eval gate (docs/luma.md)". `renovate-triage` applies.
- [ ] **Step 4: Commit and push.** Run pre-commit, `git add cluster/applications/luma-release.yaml cluster/applications/kustomization.yaml .renovaterc.json`, then `git commit -m "luma: deploy chart 0.1.0-rc.1 (core, conductor, gateway, smokescreen web, evals)"` and push.
- [ ] **Step 5: Watch the rollout** (safe-rollout skill, `Monitor`): `kubectl -n argo-cd get app luma-release` reaches Synced/Healthy, and `kubectl -n luma-system get pods -o wide` shows every pod Ready. Expected:
  - the conductor is off `middle`;
  - the two gateway pods are on different nodes;
  - `kubectl -n luma-system logs deploy/luma-conductor | grep -c '"msg":"core link up"'` ≥ 1, and Core's `/readyz` is 200.

  An image pull on a cold start can take minutes (S3); don't kill pods.
- [ ] **Step 6: In-cluster checks** from a throwaway pod using the same pinned netshoot image as Task 3's probe (`kubectl run -n luma-system tmp --rm -it --image=<that tag@digest> -- sh`):
  - `curl -s -o /dev/null -w '%{http_code}' http://luma-gateway:8080/ci/v1/models`. Expected: `401` without the bearer.
  - `curl -s -x http://luma-egress-web:4750 -o /dev/null -w '%{http_code}' https://192.168.0.1/`, the same for `https://152.44.247.88/` and `https://home.chrismiller.xyz/`. Expected: `407` with header `X-Smokescreen-Error`, for each.
  - `curl -s -x http://luma-egress-web:4750 -o /dev/null -w '%{http_code}' https://example.com/`. Expected: `200`.
  - `curl -s -x http://luma-egress-web:4750 -o /dev/null -w '%{http_code}' https://webhook.site/`. Expected: `407`.
  - `curl -s -o /dev/null -w '%{http_code}' http://luma-core:8080/internal/v1/conductor`. Expected: `404` (internal routes are not on the public port).

### Task 8: Ingress `luma.chrismiller.xyz` and the edge snapshot

**Files:**
- Create: `cluster/luma/ingress.yaml`
- Modify: `cluster/luma/kustomization.yaml`, `.claude/skills/edge-ingress/edge-snapshot.sh`

**Interfaces:**
- Produces: an Ingress `luma` in `luma-system` with `ingressClassName: traefik`, host `luma.chrismiller.xyz`, path `/` Prefix → Service `luma-core` port 8080. It has **no annotations** (no oauth2-proxy middleware: Core is the BFF) and no `spec.tls`. WebSockets need nothing extra on Traefik.

- [ ] **Step 1: Snapshot before** (edge-ingress skill): `cd .claude/skills/edge-ingress && nix-shell -p openssl --run ./edge-snapshot.sh > /tmp/before.txt`.
- [ ] **Step 2: Write the Ingress and add `luma`** to `edge-snapshot.sh`'s `hosts=` list. **Do not add a DNS record** (the wildcard covers it). An explicit `luma` record would also break the later `share.luma` wildcard synthesis (§12.3).
- [ ] **Step 3: Commit and push** with `git commit -m "luma: public ingress luma.chrismiller.xyz (BFF auth in Core)"`. Wait for Argo.
- [ ] **Step 4: Snapshot after** and diff them: `diff <(sed 's/ cert=.*//' /tmp/before.txt) <(sed 's/ cert=.*//' /tmp/after.txt)`. Expected: exactly one added line, for `luma` (200, with Core's `Content-Security-Policy`). There are no other changes.
- [ ] **Step 5: Edge checks.**
  - `curl -s -o /dev/null -w '%{http_code}' https://luma.chrismiller.xyz/v1/me`. Expected: `401`.
  - `curl -sI https://luma.chrismiller.xyz/ | grep -i -E 'content-security-policy|strict-transport'`. Expected: both headers present.
  - `./cloudflare/dns/tf.sh plan`. Expected: empty.
  - `kubectl -n traefik logs -l app.kubernetes.io/name=traefik --since=10m | grep -c ERR`. Expected: `0`.
- [ ] **Step 6: First login (Chris).** Open `https://luma.chrismiller.xyz` on the laptop and sign in with Google. Expected: the chat loads and `/v1/me` shows `chris`. Then sign in with an address that is not in the registry. Expected: the "This account isn't invited to Luma." page.

### Task 9: Observability: rules, dashboard, and what Prometheus really loaded

**Files:**
- Create: `cluster/luma/prometheusrule.yaml`, `cluster/grafana/dashboards/luma.yaml`
- Modify: `cluster/luma/kustomization.yaml`, `cluster/grafana/kustomization.yaml`

**Interfaces:**
- Consumes: product plan C7 metric names; agentgateway's `agentgateway_gen_ai_client_token_usage{gen_ai_token_type=input|output|input_cache_read|input_cache_write}`, where `input` includes cache tokens; postgres-exporter's `pg_stat_archiver_*`.
- Produces: PrometheusRule `luma-alerts` in `prometheus`, labelled `release: prometheus` (the 0b convention) plus the legacy `prometheus: kube-prometheus` and `role: alert-rules`, with group `luma`:

| Alert | Expr (sketch) | For | Severity |
|---|---|---|---|
| `LumaCoreDown` | `absent(up{namespace="luma-system",service="luma-core-internal"} == 1)` | 5m | warning |
| `LumaConductorDisconnected` | `max(luma_conductor_connected) == 0` | 5m | warning |
| `LumaGatewayDegraded` | `count(up{namespace="luma-system",service="luma-gateway"} == 1) < 2` | 15m | warning |
| `LumaBudget80` | `max(luma_budget_spent_usd) / max(luma_budget_cap_usd) >= 0.8` | 0m | warning |
| `LumaCacheSLO` | `sum(increase(luma_main_request_tokens_total{first="false",type="cache_read"}[24h])) / sum(increase(luma_main_request_tokens_total{first="false"}[24h])) < 0.8`, and the denominator > 200000 | 1h | warning |
| `LumaThinkingBoundToOtherConversation` | `increase(luma_thinking_binding_errors_total[1h]) > 0` | 0m | warning |
| `LumaEvalFailing` | `min(luma_eval_last_run_success{suite="nightly"}) == 0` | 0m | warning |
| `LumaEvalStale` | `time() - max(luma_eval_last_run_timestamp_seconds{suite="nightly"}) > 36*3600` | 1h | warning |
| `LumaPostgresArchiveFailing` | `increase(pg_stat_archiver_failed_count{namespace="luma-system"}[30m]) > 0` or `pg_stat_archiver_last_archive_age{namespace="luma-system"} > 900` | 10m | warning |
| `LumaGateway5xx` | `sum(rate(agentgateway_requests_total{namespace="luma-system",status=~"5.."}[10m])) > 0.05` | 10m | warning |
| `LumaMaintenanceLeftOn` | `max(luma_maintenance_enabled) == 1` | 6h | warning |

Alert names avoid Volume, Storage and Ceph, so they route to `homelab-default`, as the backup alerts do.

**Dashboard** `luma`: ConfigMap labelled `grafana_dashboard: '1'`, datasource `"Prometheus"`, uid `luma`. Panels:
- **Dollars per person per day:** bars, `sum by (person) (increase(luma_cost_usd_total[1d]))`, interval 1d.
- **Month to date vs cap:** stat plus gauge.
- **Cache-read share by request class** (gateway):

  ```
  sum by (request_class) (rate(agentgateway_gen_ai_client_token_usage_sum{gen_ai_token_type="input_cache_read"}[1h]))
  / sum by (request_class) (rate(agentgateway_gen_ai_client_token_usage_sum{gen_ai_token_type="input"}[1h]))
  ```
- **SLO, main excluding first requests** (Core, the `LumaCacheSLO` expr), with an 80 % threshold line.
- **Cache-creation tokens per hour**, to investigate spikes (§12.1.8).
- **Web searches per day.**
- **Turns and errors by outcome.**
- **Nightly eval status and cost.**
- **Open CLIs, and CLI RSS per CLI:** `luma_conductor_cli_rss_bytes / clamp_min(luma_conductor_hosts{state="ready"},1)`. This is the "about 400 MiB, measured in phase 1" figure.

- [ ] **Step 1: Write both files.** Then run `scripts/check-dashboards.sh` on the new dashboard. Expected: no `OR on() vector(0)`, and every query returns series once data exists.
- [ ] **Step 2: Commit and push** with `git commit -m "luma: alert rules and cost/cache dashboard"`.
- [ ] **Step 3: Verify what loaded** (Review Focus 4). First run `scripts/check-prometheus-rules.sh` (from 0b). Expected: PASS, with `prometheus/luma-alerts` selected and its group loaded. Then port-forward Prometheus and run:
  - `curl -s localhost:9090/api/v1/rules | jq '[.data.groups[] | select(.name=="luma") | .rules[].name]'`. Expected: all 11 names.
  - `curl -s localhost:9090/api/v1/targets | jq -r '.data.activeTargets[] | select(.labels.namespace=="luma-system") | "\(.labels.job) \(.health)"'`. Expected: luma-core, luma-conductor, luma-gateway (×2), luma-egress-web and postgres acid-luma (×2), all `up`.

### Task 10: Evals: the first runs, and the eval-gate procedure

**Files:**
- Modify: `docs/luma.md` (written in Task 11; this task supplies its "Upgrading Luma" section)

**Interfaces:**
- Consumes: CronJobs `luma-evals-nightly` and `luma-evals-weekly` (chart), `luma-evals` (Task 6), and Core's `/internal/v1/eval-runs`.
- Produces: green `nightly` and `gate` runs recorded in `eval_runs`, and `luma_eval_last_run_success{suite="nightly"} == 1`.

- [ ] **Step 1: Run nightly now.** `kubectl -n luma-system create job --from=cronjob/luma-evals-nightly luma-evals-first`, then `Monitor` the job to completion. Expected: exit 0. The log shows `cache.identical PASS`, `cache.cross_conversation PASS`, `replay.thinking PASS` and `ttl.one_hour PASS`, ids and numbers only.
- [ ] **Step 2: Run the gate suite once.** `kubectl -n luma-system create job --from=cronjob/luma-evals-nightly luma-evals-gate-first`, overriding `args` with `--suite gate` (patch the Job spec before create: `kubectl create job … --dry-run=client -o yaml | yq '.spec.template.spec.containers[0].args += ["--suite","gate"]' | kubectl apply -f -`). Expected: every golden task PASS, and cost < $0.50.
- [ ] **Step 3: Check the gauges.** `luma_eval_last_run_success{suite="nightly"}` is 1, and `/v1/me` budget `spent_usd` includes the eval cost (product P12).
- [ ] **Step 4: Write the "Upgrading Luma" procedure** for `docs/luma.md`:
  1. Renovate PR or new rc in the luma repo.
  2. Run a one-off gate Job with the rc's **evals image** (`--image` override in the same `create job … | yq | apply` form).
  3. If it is green, bump `targetRevision`.
  4. Watch the rollout and the next nightly run.
  5. Never bump during a drain or Talos hop (S5).

### Task 11: Runbook, CLAUDE.md, and maintenance hooks in the procedures

**Files:**
- Modify:
  - `docs/luma.md`, the existing entry page: add an "Operations (phase 1)" section and set the phase-status row for phase 1;
  - `CLAUDE.md` (topology table), `.claude/skills/talos-upgrade/SKILL.md`, `docs/backups.md`.

**Interfaces:**
- Consumes: product `docs/operations.md` (commands, error codes), and Tasks 2–10.
- Produces:
  - **`docs/luma.md` "Operations (phase 1)"** covers:
    - what runs where; the Secrets by name and key; the WAN-IP rule and `check-wan-ip.sh --live`;
    - maintenance mode;
    - "Upgrading Luma" (Task 10);
    - the restore runbook (Task 12);
    - the phase-1 burn-in log; the phone checklist (product `docs/operations.md`);
    - "If the WAN IP changes" (router reflection rules and `denyAddresses` together, via vyos-deploy plus this repo);
    - rotation procedures (gateway keys: edit `anthropic.secret.yaml` → `rm anthropic.yaml && ./sign.sh`; the key files are watched by the gateway).
  - **CLAUDE.md row** "Luma | `luma.chrismiller.xyz`, ns `luma-system` | chart `luma` (Argo `luma-release` + `luma`); runbook `docs/luma.md`; maintenance mode before any drain."
  - **`talos-upgrade` skill**, in its preflight and after the last node: `kubectl -n luma-system exec deploy/luma-core -- /luma-core maintenance on --reason talos-upgrade`, and later `… maintenance off`. Its "When to stop" section: if maintenance does not report 0 running turns within 120 s, surface it.
  - **`docs/backups.md`:** the Postgres tier row becomes "eight clusters"; a WAL-G note for `acid-luma` (the only PITR cluster; path H2; 30-day lifecycle); the drill row (Task 12).

- [ ] **Step 1: Write the docs.**
- [ ] **Step 2: Test maintenance mode** (it touches no Ceph or etcd).
  - `kubectl -n luma-system exec deploy/luma-core -- /luma-core maintenance on --reason drill`. Expected: `status` prints `enabled, running turns: 0`, the PWA shows "Luma is down for maintenance. Try again later.", and `luma_conductor_hosts` drops to 0.
  - Then `maintenance off`. Expected: a new message works.
- [ ] **Step 3: Commit and push** with `git commit -m "luma: runbook, CLAUDE.md topology, maintenance mode in talos-upgrade, backups doc"`.

### Task 12: `acid-luma` restore drill (PITR clone and logical)

**Files:**
- Create: `docs/backups/acid-drill-luma.yaml` (hand-applied, never Argo)
- Modify: `docs/backups.md` (drill log)

**Interfaces:**
- Produces: a timestamped drill-log row proving both restore paths.

- [ ] **Step 1: Write a marker and note the time.**
  - `kubectl -n luma-system exec acid-luma-0 -c postgres -- psql -U postgres -d luma -c "insert into settings(key,value) values ('drill-2026-MM-DD', '{}')"`. This is the only write; the key is inert.
  - Note T = now (UTC) + 1 minute. `Monitor` until `pg_stat_archiver.last_archived_time` > T, which takes up to 5 min (H3).
- [ ] **Step 2: Write `acid-drill-luma.yaml`.** It is `docs/backups/acid-drill.yaml`'s shape, in `luma-system`, labelled `backup-drill: "true"`, with **no `WAL_S3_BUCKET`** (the drill never archives):
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
- [ ] **Step 3: Restore by PITR.** `kubectl apply -f docs/backups/acid-drill-luma.yaml`, then `Monitor` `kubectl -n luma-system get postgresql acid-drill` until Running. Then run `psql -d luma -Atc "select count(*) from settings where key like 'drill-%'"` in `acid-drill-0`. Expected: `1`. Also compare `select count(*)` of `sessions`, `messages`, `events` and `memory_profiles` with `acid-luma`. Expected: equal for anything not written after T.
- [ ] **Step 4: Restore logically** (Chris confirms deletes, S4).
  1. Delete the drill CR, then the PVC `pgdata-acid-drill-0`.
  2. Re-apply the drill **without** the `clone:` block (an empty cluster).
  3. Run `docs/backups/pg-restore.sh luma-system acid-luma acid-drill`. Expected: the row counts printed for target and source agree, as of the latest dump. The first dump exists after one night.
- [ ] **Step 5: Clean up** (S4, confirm). `kubectl delete -f docs/backups/acid-drill-luma.yaml && kubectl -n luma-system delete pvc pgdata-acid-drill-0`.
- [ ] **Step 6: Log the drill and commit.** Add the drill-log row: date, both paths, T, counts. Then `git add docs/backups/acid-drill-luma.yaml docs/backups.md && git commit -m "backups: acid-luma PITR and logical restore drill"`.

### Task 13: Phase-1 exit criteria (§14)

**Files:** Modify `docs/luma.md` (burn-in log), and the luma repo's `docs/spikes/phase-1.md` (final status).

- [ ] **Step 1: A week of daily use on Android (Chris).**
  - Install from Chrome, then run the product checklist on the real phone:
    - force-stop the app: still signed in;
    - background it for 60 s mid-answer: complete, with no duplicates;
    - camera and PDF attachments;
    - airplane mode, then back: it reconnects.
  - One line per day in the burn-in log: what broke, cost that day.
- [ ] **Step 2: Check the SLO.** In Prometheus, `sum(increase(luma_main_request_tokens_total{first="false",type="cache_read"}[7d])) / sum(increase(luma_main_request_tokens_total{first="false"}[7d]))`. Expected: ≥ 0.80. Below that, investigate `cache_creation` spikes (§12.1.8) before declaring phase 1 done.
- [ ] **Step 3: Check the regression tests are green.** Seven consecutive nightly runs passed (`eval_runs`, or the dashboard). No `LumaThinkingBoundToOtherConversation` has fired.
- [ ] **Step 4: Confirm the restore drill is done** (Task 12 row present).
- [ ] **Step 5: Confirm the edge snapshot includes `luma`** (Task 8).
- [ ] **Step 6: Confirm the spikes are resolved.** Product Spike G and Spike S, and live L1–L4, are each recorded with the branch taken (gateway byte-diff, SessionStore resume prefix, TTL env var).
- [ ] **Step 7: Measure and right-size.**
  - Per-CLI RSS from the dashboard. Set `conductor.resources.requests.memory` to ≈ 4 × measured + 200 Mi.
  - Compare the ledger month-to-date with the Console for `luma-interactive` and `luma-ci`; they should agree within 5 % (the Console is ground truth, §12.4).
  - The first nightly `logical-backup-acid-luma` run succeeded (`BackupCronJobStale` stays quiet).
- [ ] **Step 8: Close out.** Write "Phase 1 done" with the date in `docs/luma.md`, and `git commit -m "luma: phase 1 exit criteria met"`.

---

## What to watch, and rollback

- **Rollback (full):**
  1. Remove `luma.yaml` and `luma-release.yaml` from `cluster/applications/kustomization.yaml` and push. Argo prunes the workloads, Ingress, rules and ConfigMaps.
  2. `acid-luma`'s PVCs are retained (StorageClass Retain, and `enable_persistent_volume_claim_deletion: false`); delete them only on Chris's word (S4).
  3. Remove the `luma-core` Dex client (reseal and restart Dex), and the `luma` line from `edge-snapshot.sh`.
  4. Delete the two Anthropic keys in the Console.
- **Rollback (chart only):** revert `targetRevision`. A conductor restart fails any open turn visibly; the conversations resume from the store (§6).
- **During a drain or Talos hop:** maintenance on, first (Task 11). Luma is expected to be down; the drain shortfall is about 21 GiB (§14 capacity).
- **Cost runaway:** the workspace hard limits ($150/$20) stop it at Anthropic. Core refuses new turns at the $200 global cap. To freeze Luma immediately, put it in maintenance mode.
- **What is NOT changed:** Traefik, DNS, the router, other namespaces' workloads, the oauth2-proxy tiers (no new allowlist entries), and the MCP gateway.

## Open questions (Chris)

1. Is **`discord.com`** on the deny list (H5)? Yes is drafted.
2. Should the **nightly Usage API reconcile** (§5.2.5) run? It needs an org **Admin** API key in the cluster. Until then, the ledger is checked against the Console by hand (Task 13 step 7).
3. Is a **Microsoft address** listed for you in the registry, or Google only (H7)?
