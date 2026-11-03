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
- Diff old vs new after normalisation: no removed or changed lines. The only additions are the six new rows. `git.realliance.net/` returns 200. The five `realliance.page` rows fail TLS (curl 60, no certificate for the name yet), as expected before DNS and cert. Their curl error text spans several lines, so filter the new rows by name.
