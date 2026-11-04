# Realliance Pages ledger

## Baseline (2026-11-03, before Task 2)

- G1: 3 nodes Ready, none SchedulingDisabled. Ceph 145 pgs: 144 active+clean plus 1 active+clean+scrubbing+deep, so none are outside active+clean.
- G2: Ceph HEALTH_WARN with `MDS_CLIENT_RECALL` plus the muted `AUTH_INSECURE_*` checks. Argo: only `rook` and `rook-cluster` are Degraded. The OSD-latency check in `scripts/ceph-gate.sh` is known SMR noise and is not a criterion.
- G3: edge snapshot with the old script (before this task's change): 31 host rows and 9 probe rows; saved to scratch `edge-old.txt`.
- G4: no pods outside Running/Succeeded.
- Memory requests (kubectl describe nodes): top 85 %, middle 85 %, bottom 86 %. These match the expected 85/85/86 %.

## Answers

- 2026-11-03: Chris answered "Yes, start" to executing this plan. This pre-approves the routine pushes: docs, ledger, test and snapshot scripts, the relay's code and image, and sso-secrets. Every STOP still needs its own OK.

## Task 1 - edge-snapshot coverage

- `edge-snapshot.sh` now covers `git.realliance.net`, `realliance.page`, `www.realliance.page` and `realliance.realliance.page`. `*.page` joined the FQDN `case`. Two probes were added: `POST realliance.realliance.page/` and `GET realliance.realliance.page/.git-pages/manifest.json`.
- New snapshot: 35 host rows (+4) and 11 probe rows (+2); saved to scratch `edge-new.txt`.
- Gate G comparisons must first normalise Dex's random per-request values with `sed -E 's/(code_challenge|state|nonce)=[^& ]*/\1=X/g'`.
- Diff old vs new after normalisation: no removed or changed lines. The only additions are the six new rows. `git.realliance.net/` returns 200. The five `realliance.page` rows show `000 err=60` (no certificate for the name yet), as expected before DNS and cert. The script prints exactly one line per row whatever the outcome: a curl failure collapses to `000 err=<curl exit code>` and curl's own text is discarded.

## Task 5 - the `pages` app (local commit ce1cc569, before the STOP)

- Sizing pre-check (2026-11-04, ~01:00Z, `kubectl describe nodes`): allocatable memory is 30,829 MiB on each node. Memory requests: top 26,345 Mi (85.5 %), middle 26,339 Mi (85.4 %), bottom 27,066 Mi (87.8 %). Task 5 adds 96 Mi of requests: git-pages 64 Mi and the relay 32 Mi. The relay's 32 Mi replaces the plan's 16 Mi, following the Task 4 measurement. Worst case, with both pods on bottom, bottom goes from 87.8 % to 88.1 % (+0.3 points). CPU requests add 15m. Limits are git-pages 1 Gi and relay 128 Mi (`GOMEMLIMIT=100MiB`). Task 10 Step 6 sets the final values from measurements.
- git-pages release notes (0.10.0 of 2026-10-25 and 0.10.1 of 2026-10-28), read for the breaking items:
  - Administrators:
    - `[server].caddy` is now `[server].proxy`. The config uses `proxy = "-"`, and an unknown key aborts the start.
    - The SIGHUP reload is gone, so a config change needs a restart. The hashed `git-pages-config` ConfigMap rolls the pod.
    - `-size-histogram` is now `-analyze-storage text`.
    - The `-audit-log` columns were reordered, and `-reason` was added.
    - Caddy will later leave the image; the `proxy` endpoint stays.
  - End users:
    - On a project site (`/<repo>/`), a `_headers` source path must start with the project path, and so must a `_redirects` target with a 2xx/4xx code.
    - A PATCH carrying `_redirects` or `_headers` now replaces them instead of appending. PATCH is off in v1 (D7).
  - 0.10.1 (security): Forgejo-level repository redirects are no longer followed. A renamed or transferred repo publishes only under its new name, and the old site stays until it is taken down.
  - Also in 0.10.x: `-delete-site` performs the takedown, and sites are cached for about 60 s.
- Gate G before (2026-11-04T01:17Z):
  - G1: 3 nodes Ready, none cordoned or tainted. Ceph HEALTH_OK; the only checks are the muted `AUTH_INSECURE_*` ones, and the `MDS_CLIENT_RECALL` warn has cleared. 145 pgs: 144 active+clean plus 1 active+clean+scrubbing+deep. 9/9 OSDs up/in, and there is no recovery.
  - G2: 59 Argo apps. Only `rook` is not Healthy (Synced/Degraded); `rook-cluster` is Healthy now.
  - G3: the edge snapshot has 46 lines (scratch `edge-t5-before.txt`). It is identical to Task 1's `edge-new.txt` once `cert=` is stripped and Dex values are normalised. The `realliance.page` rows still show `000 err=60`.
  - G4: no pods outside Running/Succeeded.
- Cross-plan: no netpol Gate G window is open (that plan is paused until 2026-11-04T18:35Z), and forge-bot is in pre-T16 code review, so no apply or drill window is open.

### Task 5 - deploy (Steps 9-10)

- 2026-11-04: Chris approved the Task 5 STOP. `git pull --rebase` was a no-op (origin unchanged), and a fresh G1/G4 at 01:30:01Z was clean. Pushed ce1cc569 and 77ff5062 at 01:30:15Z.
- At 01:30:42Z app `pages` was Synced/Healthy and both pods were Running 1/1 with 0 restarts, both on `bottom`. That is about 27 s from the push.
  - Still 0 restarts at 5m20s.
  - Idle use: git-pages 7Mi, relay 2Mi.
- git-pages log: `memlimit: was 8.0 EB now 512.0 MB`, then `s3: enumerate manifests`, `existence: refreshed with 0 sites` and `serve: ready`. 0 lines match `error`, and B2 auth works.
- `scripts/check-prometheus-rules.sh` exited 0. `pages-alerts` is loaded: all 6 rules have health ok and state inactive.
- `up{namespace="pages"}` returns two series, git-pages (endpoint metrics) and pages-relay (endpoint http), both 1.
- Memory requests after the deploy: top 86 %, middle 85 %, bottom 87 %. Other pods moved in the meantime, so bottom's figure is lower than before the deploy.
- Gate G after (01:31:19Z):
  - G1: 3 nodes Ready. Ceph HEALTH_OK, with 145 pgs active+clean (two of them scrubbing).
  - G2: 60 apps (+`pages`, Synced/Healthy). Only `rook` is Degraded.
  - G3: `edge-t5-after.txt` is identical to `edge-t5-before.txt` after normalisation.
  - G4: no pods outside Running/Succeeded.
- No rollback trigger fired.

## Task 6: Forgejo to relay (2026-11-04) - BLOCKED at Step 7

- RED/GREEN: `test_forgejo_webhook_allowlist` failed (`KeyError: 'webhook'`), then 25/25 passed. Commit 17770e59 pushed to main 01:38:44Z.
- Gate G before (matches Task 5 after): 3 nodes Ready; Ceph HEALTH_OK, 144 active+clean plus 1 active+clean+scrubbing+deep; only `rook` Degraded in Argo; no non-Running pods; edge snapshot identical to Task 5 after (modulo random PKCE code_challenge values). CI runner `idle`, no running forge-bot runs (stale `waiting` runs 3/7/11/15 are old).
- Forgejo restart: old pod Terminating 01:38:50Z, new pod 1/1 and `/api/v1/version` 200 at 01:39:10Z (about 20 s). `[webhook] ALLOWED_HOST_LIST = external,pages-relay.pages.svc.cluster.local` confirmed in app.ini.
- Probe repo `forgejo-admin/pages-probe` created (201, public) before the hook.
- Hook created via `POST /api/v1/admin/hooks`: id 1, active, events delete/push/repository, branch_filter `pages`.
- FINDING: Forgejo 15 stores that hook with `is_system_webhook = f` (repo_id 0) - a DEFAULT webhook, not a system one. `GET /api/v1/admin/hooks` (system list) returns `[]` even with the hook present, so `ensure_system_hook` is NOT idempotent (it would create duplicates). The swagger has no type parameter for the admin hook create.
- Probe commit to `pages` (6f46ec35, token probe-5afdfae0): no delivery (hook_task empty, relay counters all 0, no webhook log lines), site 404 after 90 s. Answers the question the probe exists for: this hook does NOT reach pre-existing repos.
- Action taken: hook 1 disabled (active=false). Nothing else changed. Forgejo, the allowlist and the probe repo/branch stay. No takedown needed (no site was published). deleteFiles: not exercised (no publish); undecided.
- Needs a ruling: create a true system webhook (admin UI "System Webhooks" in Forgejo 15, or set `is_system_webhook` in the DB - a prod DB write), then re-run Step 7.
