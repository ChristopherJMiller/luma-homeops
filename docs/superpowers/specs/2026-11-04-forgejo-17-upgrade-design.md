# Forgejo 17 upgrade for the Realliance forge: design

> **Decision (Chris, 2026-11-04): option B — stay on Forgejo 15 LTS, take 15.0.x patches, and upgrade once to 19 LTS
> (released ~2027-04-15; pinnable two weeks after a 19.0.y; 15 EOL 2027-07-15).** This document's analysis stands as
> the 15 → 17 research; the execution plan is re-targeted at 19 LTS and written in spring 2027 against 19's notes.
> Carried now, independent of the target: the restore runbook's `forgejo admin regenerate hooks` breaks on ≥ 16; the
> admin API can create a true system hook with `config.is_system_webhook = "true"` (both 15 and 17); pin
> `[service].ADD_MEMBERS_BY_INVITATIONS = false` before any ≥ 16 hop; Catppuccin needs the oklch shim on ≥ 17;
> 15.0.12 (unreleased) fixes the access-table regression #14980 present since 15.0.8. Research digest:
> `.superpowers/forgejo17-research/findings.md` (git-ignored scratch).

- **Date:** 2026-11-04.
- **Status:** draft for Chris's review. Nothing has been executed.
- **Research:** `.superpowers/forgejo17-research/findings.md` (local). It holds the per-release
  notes, the affects-us table with evidence, and the LTS analysis.
- **Related:**
  - forge spec `2026-11-02-realliance-forge-design.md`, which decided "Forgejo 15 LTS";
  - forge-bot spec `2026-11-02-forge-bot-and-ci-design.md`;
  - pages spec `2026-11-03-realliance-pages-design.md`;
  - NetworkPolicy spec `2026-11-03-networkpolicy-enforcement-design.md`;
  - runbook `docs/forgejo.md`.

## Recommendation

**The upgrade, in one step.**
- Go from **15.0.11 straight to 17.0.x**. 16 is not a required stop, and it has been EOL since
  2026-10-29.
- Stay on **chart 17.3.2** and set the image tag explicitly. The chart has no 17 release, by
  policy.
- **Rehearse on a restored copy** in `forgejo-drill` first.

**What happens after 17.** Stay on the stable track only as far as **19 LTS** (April 2027), then
park there. That means two more forced upgrades, in January and April 2027.

**The one open decision** is whether that cost is worth it, rather than staying on 15 LTS and
jumping to 19 in mid-2027. It is the only entry under Decisions for Chris. My answer is yes, for
the reasons given there.

## Intent

Chris asked for "an upgrade to forgejo 17". The forge has run 15.0.11 LTS since 2026-11-02, by a
deliberate choice in the forge spec ("17 is the short-lived track; 17.0.0 shipped with a push/merge
regression"). This doc covers three things:
- what moving to 17 changes for the forge and for everything that talks to it;
- how to do the move so that it can be undone;
- what being off LTS costs from now on.

The research found the regression. It was #14866, "cannot lock ref" on merge, and it is fixed in
17.0.1, so we never run 17.0.0.

## Goals

1. `git.realliance.net` runs **Forgejo 17.0.x**, deployed by Argo from this repo. Every consumer
   keeps working without a code change:
   - forge-bot;
   - pages-relay and git-pages 0.10.1;
   - forgejo-runner v13.2.1;
   - the OpenTofu inventory;
   - fj;
   - the GitHub push mirror;
   - both backup jobs.
2. **Proven twice:**
   - on restored data before production is touched;
   - on production afterwards, by the realliance/sso `tests/forge/` suite plus four small new
     checks.
3. **A rollback to the exact pre-upgrade state** (DB, repos, `/data`) from immutable backups, in
   under 30 minutes.
4. **The restore runbook and the restore drill work on 17.**
5. **The cadence after 17 is written down:** when 18 and 19 happen, and by what rule.

## Non-goals

- **New 16/17 features.** This covers Authorized Integrations, workflow-job webhooks, dynamic OIDC
  group maps, `USERNAME=preferred_username`, team invitations and job summaries.
  - Authorized Integrations for forge-bot's package push is the **first follow-up**, as its own
    small change *(my call)*.
- **Upstream's optional cleanups** *(my call: skip; revisit at 19)*:
  - deleting per-repo `hooks/` and `description` files, which have been dead since 16's
    centralised hooks;
  - deleting migrated-PR `pulls/` files.
  Neither has any functional effect.
- **Bumping the chart major** (none exists for 17), **the runner** (13.2.1 is current and speaks
  17's protocol), **git-pages**, or **fj on laptops** (0.7.0 is optional).
- **Forgejo 18 and 19.** Each gets a short addendum that reuses this doc's checklist (see Cadence).
- **A Realliance soft fork.**

## Anti-goals

- Running any `x.0.0` release. 17.0.0 blocked merges.
- A rollback that needs the forge to be up, or that rests on backups taken after the upgrade
  started.
- Two changes in one window. That rules out an upgrade during any of these:
  - a Talos/k8s hop (#2506);
  - a NetworkPolicy Gate G window or soak;
  - a node drain;
  - Ceph PGs outside `active+clean`;
  - forge-bot newly in `apply`.
- Hand changes to live state that Argo doesn't know about (S2). The exceptions are the
  `forgejo-drill` namespace, which is already outside Argo, and the commands the runbook already
  uses.
- Hidden-org names in any public file: this doc, the ledger, logs pasted into either, or test
  names.
- Themes that load but look wrong. The Catppuccin themes must render callouts and danger buttons
  correctly, not merely appear in the theme list.

## Constraints

| Area | Constraint |
|---|---|
| Forgejo today | 15.0.11 LTS, rootless image pinned by digest. Chart `forgejo-helm` 17.3.2 (appVersion 15.0.11). One replica, `Recreate` (RWO volumes). Login source and group→team map live in the DB. |
| Data | Small: `/data` 896 KiB (SSD), `/repos` 4.9 MiB (HDD), `acid-forgejo` PG16 with 2 pods (SSD). LFS, packages, attachments and Actions logs are in B2 `realliance-forge-objects`. |
| Backups | `pg_dumpall` at 00:30 and `forgejo dump` at 01:30 → `realliance-forge-backups`, object-locked 30 days. The restore drill passed on 2026-11-02 (restore ~90 s). No `VolumeSnapshotClass` exists on galaxy. |
| Upstream | Migrations are one-way: "it will refuse to start because it would damage the content of the database". 17 adds 17 additive migrations over 15.0.11. |
| Pin age | Images and charts must be ≥ 2 weeks old. 17.0.1 and chart 17.3.2 are both from 2026-10-26, so both are eligible on **2026-11-09**. |
| GitOps and hygiene | S2. Signed commits, pre-commit. Secrets stay in SealedSecrets and `sso-secrets`; none change here. |
| galaxy | S3, S5. Memory requests ~85–88 %, so the drill adds pods only for the rehearsal. The SSD pool is shared with every Postgres. |
| In flight | **forge-bot:** the T16 deploy is held until the NetworkPolicy soak ends (2026-11-04T18:35Z), then runs in dry-run. **Pages:** Task 10 (acceptance) and Task 11 (docs) remain. **NetworkPolicy:** phases after A3 remain. |
| People | Chris operates galaxy. Connor is the forge co-admin. The users are a small invited group. |

## What changes for us

The research diffed every code path our consumers use between tags `v15.0.11` and `v17.0.1`.

**Unchanged** (evidence in findings §4):
- the admin hooks API;
- `EditUserOption` (pointer fields; `login_name` and `source_id` sent together);
- `X-Total-Count` on all four endpoints forge-bot pages through;
- push, delete and repository webhook payloads;
- `X-Forgejo-Signature` (lowercase hex HMAC-SHA256), and no retry on non-2xx;
- commit verification and Forgejo's signing code;
- `[webhook] ALLOWED_HOST_LIST`;
- `forgejo dump` flags;
- the runner protocol. Forgejo 17.0.1 vendors actions-proto v0.8.0, the same as runner 13.2.1.

What does change:

| # | Change upstream | Effect here | Response |
|---|---|---|---|
| C1 | forgejo-helm stays on Forgejo LTS by default (issue #1639; PR #1800 for Forgejo 17 was closed "we stay on lts"). The maintainer says the chart supports "the current lts and the latest version" via `image.tag`. | Our values take the tag from appVersion (15.0.11). | Add an explicit `image.tag: "17.0.x"` plus the digest on chart 17.3.2. The chart is version-agnostic, and its selector labels exclude `version`. |
| C2 | `templates/user/settings/navbar.tmpl` gains an "Authorized integrations" item (!12530, 16.0). | Our override would hide that item. | Re-copy the upstream 17.0.x file and add the Realliance line again. |
| C3 | !13224 (17.0): `--color-thin-*` are now built from `--thin-lightness`, `--regular-chroma` and `--hue-*`, with the old fallbacks removed. | Catppuccin v1.0.2 defines none of these. Under every Catppuccin theme, including the default `catppuccin-sapphire-auto`, these lose their colour: markdown callouts (`[!NOTE]`, `[!WARNING]` …), red danger buttons and form error text. catppuccin/gitea has not released since 2025-04-15. | A small shim *(my call)*; see Shape of the change. |
| C4 | !10397 (16.0): centralised git hooks, rewritten into Forgejo's git home at every start. `forgejo admin regenerate hooks` no longer exists; v17 has only `regenerate keys`. | Step 4 of `docs/forgejo.md` "A real restore" would fail. | Drop that command for ≥ 16 and keep `forgejo doctor check --all`. |
| C5 | !12845 (16.0): `PUT /teams/{id}/members/{u}` goes through `InviteOrAddTeamMember`. If `[service].ADD_MEMBERS_BY_INVITATIONS=true` and the user is not an org member, it *invites* instead of adding, and may return 409. The default is `false`. | forge-bot's teams job uses this path. | Pin `ADD_MEMBERS_BY_INVITATIONS: false` explicitly *(my call)*, so that a later default flip can never turn forge-bot's adds into invitations. |
| C6 | minio-go 7.0.99 → 7.1.0 ("Add new AWS checksums"). | The B2 write path changed. It is unverified with `MINIO_CHECKSUM_ALGORITHM=md5`. | Re-verify an LFS upload and the transfer of Actions logs. |
| C7 | 17 additive DB migrations (11 in v16a–e, 6 in v17a). | One-way. | Backups, the rehearsal, and rollback by restore. |
| C8 | !12307 (17.0): JWTs need an `exp` claim; !13129 and !13490: mirrors don't follow redirects or use `http://`. | fj login uses Forgejo's OAuth2 provider. The pages test pull-mirrors over `https://`, and the push mirror uses SSH. | No change expected. Re-verify. |
| C9 | !11403 (16.0): fast-forward-only merges no longer need Forgejo's signing key. | Helps `realliance-ops/forge` (FF-only). | None. Keep the key, which signs CRUD_ACTIONS and other merge styles. |

A finding worth knowing, though it changes nothing in this upgrade:
- **Two Forgejo versions behave the same way here.** Both 15 and 17 accept
  `"config": {"is_system_webhook": "true"}` on `POST /api/v1/admin/hooks` (`routers/api/v1/utils/hook.go`
  L174–181). That would have created the pages hook as a true system hook without the DB flip. The
  pages helper `ensure_system_hook` can use it, if the hook ever has to be re-created.
- **A current sharp edge, not caused by this upgrade:**
  - The access-table regression #14980 is already in 15.0.11 (backported in 15.0.8). It drops
    explicit access rows for public repos, so members' dashboards and pickers miss new repos.
  - The fix is in 15.0.12 and 17.0.2, both unreleased.
  - Repairing old rows takes a team-membership change.

## Upgrade path: direct, 15.0.11 → 17.0.x

**Why direct.**
- **Upstream supports it.** The v17.0 upgrade guide says "upgrade straight to the latest released
  Forgejo version … (the upgrade procedure will take care of migrations)". It presents stepping
  through each series only as a way to narrow down a failed upgrade.
- **16 is EOL** since 2026-10-29. A stop there would put production on an unsupported release and
  add a second outage, while testing nothing that 17 doesn't also test.
- **Our API surface didn't move** in 16 or 17. The things that did move (C1–C9) are all present in
  17, so the 17 rehearsal covers them.
- **If the rehearsal fails, bisect in the drill only:** 15.0.11 → 16.0.7 → 17.0.x, as upstream
  suggests. Production never sees 16.

**Target version** *(my call)*.
- Take the newest 17.0.x that is at least 14 days old on the day: **17.0.1 from 2026-11-09**, or
  17.0.2 if it has aged by then. If 17.0.2 is a security release, it can be taken under Chris's
  usual waiver.
- Never 17.0.0.
- The `17.0.1-rootless` index digest was `sha256:5ef02fbc…88dc8d3` on 2026-11-03. Re-read it from
  the registry on the day.

**Shape of the change.** One signed commit for config and one for docs, or a single commit (left
to the builder):

1. **`cluster/applications/forgejo-release.yaml`**:
   - `image.tag: "17.0.x"` plus `image.digest`;
   - `targetRevision` stays `17.3.2`;
   - `gitea.config.service.ADD_MEMBERS_BY_INVITATIONS: false` (C5);
   - `realliance/templates-rev` "2" → "3";
   - comments: "15 LTS" → "17", and drop "the chart derives the tag from appVersion".
2. **`cluster/forgejo/custom/templates/user/settings/navbar.tmpl`**: the upstream 17.0.x file plus
   the Realliance line. Its header names 17.0.x (C2).
3. **Catppuccin shim** *(my call)*:
   - Ship `custom/templates/custom/header.tmpl` with one `<style>` block. Forgejo 17 still includes
     `custom/header` in `<head>`, after the theme CSS.
   - The block defines `--thin-lightness`, `--thin-lightness-highlight`, `--regular-chroma` and
     `--hue-{green,red,purple,violet,orange,blue}`, scoped to `:root[data-theme^="catppuccin-"]`.
     Forgejo sets `data-theme` on `<html>`.
   - Values come from Forgejo 17's own themes:
     - dark: 0.68 / 0.75 / 0.19;
     - light: 0.53 / 0.17;
     - hues: 145 / 27 / 298 / 293 / 41 / 260 deg.
   - Latte flavours take the light values. Frappé, macchiato and mocha take the dark values. The
     `-auto` themes switch with `prefers-color-scheme`.
   - It is a new key in ConfigMap `forgejo-custom-templates` with a second `subPath` mount.
   - Remove it when catppuccin/gitea ships a 17-aware release.
4. **The drill moves to 17 in the same commit**: `docs/forgejo/drill-values.yaml` (tag and digest)
   and `docs/forgejo/restore-drill.sh` (the loader image and comments). A 17 database needs a 17
   binary, so this cannot land earlier.
5. **Renovate** *(my call)*:
   - Add `forgejo` to the `argocd` no-automerge list. Chart minors can change the init scripts.
   - Add a regex `customManager` for `forgejo/forgejo` tag plus digest in `forgejo-release.yaml`
     and `drill-values.yaml`, as the chart README documents, with `automerge: false`. Patch
     releases then show up as PRs that follow this doc's checklist.
6. **Docs:** see Docs to update.

## Pre-flight snapshot

The restore point is **two immutable B2 objects**. They are taken minutes before the swap and
proven loadable the same day.

1. **Gates are green** (see Order of work).
2. **Freeze writes for about 1 hour.**
   - Announce it to Realliance and to Connor.
   - forge-bot must be in `dry-run`.
   - The runner must be idle, with no queued or running jobs.
   - Nobody pushes.
3. **Capture read-only baselines** for the before/after comparison, in the ledger:
   - `GET /api/v1/version`;
   - the user count (`forgejo admin user list`);
   - `GET /api/v1/admin/hooks` (hook 1: events, `branch_filter`), plus `is_system_webhook` via the
     sso helper's SELECT;
   - the runner list;
   - `ssh-keyscan -t rsa 192.168.0.6`;
   - `git ls-remote` heads of the drill fixture, the inventory and forge-bot;
   - object counts per prefix in `realliance-forge-objects`, using the read-only key;
   - forge-bot's latest dry-run plan (its metrics);
   - the edge snapshot.
4. **Flush the queues:** `kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo manager
   flush-queues`. The upgrade guide asks for this: the LevelDB queues on `/data` "are not
   guaranteed to be backward compatible".
5. **Run both backup jobs now.** Create one Job from each CronJob, `logical-backup-acid-forgejo`
   and `forgejo-dump`, as `restore-drill.sh up --fresh` does. Write both object keys into the
   ledger:
   - `spilo/acid-forgejo/…/<ts>.sql.gz`;
   - `dump/forgejo-<UTC>.tar.gz`.

   The object lock keeps both for 30 days.
6. **Prove them.** Run `restore-drill.sh up dump/forgejo-<UTC>.tar.gz` on 15.0.11, then `check`.
   It checks:
   - repo heads equal production;
   - the LFS object comes from B2;
   - the SSH host key is identical.

   This tests the rollback artefact itself.
7. **Deliberately not snapshotted** *(my call)*:
   - **The PVCs.** `forgejo dump` already holds `repos/` and `data/` (SSH host key, avatars),
     plus `app.ini` and `custom/`, about 6 MB in total. Galaxy has no CSI snapshot class, and an
     RBD snapshot would be a Ceph write (S7) duplicating that.
   - **The search indexes.** They can be rebuilt.
   - **B2 objects.** The upgrade's migrations are DB-only, so no object is rewritten. The bucket is
     versioned, and its key cannot delete versions. Objects written during a window that is later
     rolled back become harmless orphans. Record the counts only.
   - **App secrets.** They are unchanged, and their canonical copies are in `sso-secrets`.

## Order of work

**Gates before the production swap.** All of them must hold:
- **G-pages:** pages Task 10 acceptance is green on 15. That is the baseline for the webhook path.
- **G-bot:** forge-bot is deployed (T16) and has run in `dry-run` for at least 24 h on 15 with
  clean reads, which gives a baseline plan. If forge-bot is already in `apply` by then, an Owner
  flips it back to `dry-run` with a signed inventory commit for the window *(my call)*. It returns
  to `apply` only after the post-upgrade plan matches the baseline.
- **G-netpol:** no NetworkPolicy Gate G window or soak is open.
- **G-cluster:**
  - three nodes are Ready;
  - Ceph PGs are `active+clean`;
  - no Talos/k8s hop (#2506) and no drain is in progress.
- **G-age:** the target is at least 14 days old.
- **G-backups:** last night's two backup jobs succeeded.

The earliest date is **2026-11-09**.

**Steps:**

1. **Rehearsal, with no production change:**
   - Run `restore-drill.sh up --fresh`, then `check`, on 15.
   - Re-template **the drill only** with the 17 image (`helm template … -f drill-values.yaml` with
     `image.tag` and `image.digest` overridden, then `kubectl apply`, as the script does).
   - Read the `configure-gitea` log: the migrations must run to the v17a set.
   - Run `forgejo doctor check --all --log-file /tmp/doctor.log` and keep the output as the
     reference for production.
   - Run `check` again.
   - Look for config, template or theme errors in the first minutes of logs (!14408 makes config
     errors loud).
   - Run `down`.
   - **This decides go or no-go for production.**
2. **Pre-flight snapshot**, as above, on the same day.
3. **Swap.**
   - Push the signed commit. Argo syncs `forgejo-release`.
   - `Recreate` stops the old pod. The new pod pulls the image, runs `gitea migrate` in the
     `configure-gitea` init container, then becomes Ready.
   - Watch with Monitor (the safe-rollout skill). Never kill a slow image pull (S3).
4. **Verify** against the re-verify list, and run `forgejo doctor check --all`. Compare it with the
   rehearsal's output.
5. **Decide.**
   - On a pass: lift the freeze. forge-bot continues its own plan (steps C3–C6 of the forge-bot
     spec, not this doc's C-items).
   - On a fail, before the freeze is lifted: roll back.
6. **Afterwards:**
   - run both backup jobs, then `restore-drill.sh up --fresh` and `check` with the 17 pins;
   - watch the next nightly cycle.

   Then the Authorized Integrations follow-up.

## Re-verify list

The "Kind" column says where each check comes from: *existing* means it already exists in
realliance/sso `tests/forge/`, and *new* means it gets added *(my call)*. Run the whole suite
against 15 before the swap, so that a test that was already red is not blamed on 17.

| # | What could break (why) | Check | Kind |
|---|---|---|---|
| V1 | Startup, migrations, config parsing (C7, !10273, !14408) | `/api/v1/version` = 17.0.x. The `configure-gitea` log shows the migrations run without error. `forgejo doctor check --all` matches the rehearsal. No `ERROR` lines in the first 10 minutes. | manual |
| V2 | SSO sign-in, admin group, refusal at SSO, local forms off, user list hidden, org creation, username collision | `test_forge_signin.py` (all) | existing |
| V3 | Navbar override (C2) | `test_settings_link_to_realliance_account`, plus an assert that `/user/settings` links to `/user/settings/authorized-integrations` | existing + new assert |
| V4 | Themes (C3) | `test_catppuccin_themes_offered` and `test_default_theme_is_catppuccin_sapphire_auto`. Screenshots of a README with `[!NOTE]` and `[!WARNING]`, and of a red Delete button, under catppuccin-sapphire-auto in light and dark mode, on 15 (before) and 17 (after) | existing + manual |
| V5 | `EditUserOption`, `login_name` + `source_id`, offboarding and demotion | `test_forge_access.py` (all; offboarding uses the admin `PATCH`) | existing |
| V6 | `X-Total-Count`, every forge-bot read, `EmailUsed` mapping | First forge-bot dry-run cycle after the swap: `forge_bot_read_ok{source="forgejo"}` is 1, the startup source check passes, and the **planned change set equals the pre-upgrade baseline**, so zero diff | existing (forge-bot metrics) |
| V7 | forge-bot accounts and token scopes | `test_forge_bot.py` | existing |
| V8 | Team-member add path (C5) | On a scratch team in a scratch org, `PUT /teams/{id}/members/{u}` for a test user returns 204, the user is listed, and no invitation row is created. `DELETE` returns 204. | new |
| V9 | System webhook, payloads, signature, no retry, `ALLOWED_HOST_LIST`, mirrors (C8) | `test_forge_pages.py`: `system_webhook_present`, `push_publishes_and_updates`, `other_branch_changes_nothing`, `private_repo_publishes_nothing`, `branch_delete_requests_takedown`, `private_then_deleted_still_requests_takedown`, `pull_mirror_sync_publishes`. The relay's `bad_signature` and `bad_request` counters do not move. | existing |
| V10 | SSH: host key, WAN, LAN | `test_forge_ssh.py`, plus the host key equals the baseline keyscan | existing + baseline |
| V11 | B2 writes through minio-go 7.1.0 (C6) | `test_forge_storage.py::test_lfs_object_lands_in_b2`; `test_forge_ci.py::test_actions_logs_in_b2` after one CI run | existing |
| V12 | Runner protocol and CI (server-side `if`, skipped checks) | `test_forge_ci.py`. The `slow` allowlist test runs too. One forge-bot `main` CI run is green and publishes `:sha-…` with `REGISTRY_TOKEN`. | existing + one run |
| V13 | Inventory provider, push mirror, permissions | `test_forge_inventory.py`: no drift (`svalabs/forgejo` 1.7.0 plan empty), mirror current, member and maintainer cannot delete or transfer, private org invisible | existing |
| V14 | Forgejo's own signing (C9) | Web-edit a file in a scratch repo whose branch requires signed commits. The commit's verification names key `50ACEA5F43099C00`. A fast-forward-only merge of a scratch PR succeeds. | new |
| V15 | fj (C8) | `fj -H git.realliance.net auth login` (OAuth, JWT `exp`), `fj repo view`, and the V14 merge done with `fj pr merge` | manual |
| V16 | Backups and restore on 17 | Both jobs run; `test_forge_backups.py`; the drill on the 17 pins (`up --fresh` and `check`) | existing |
| V17 | Edge | `edge-snapshot.sh`: the `git.realliance.net` rows are identical after normalising | existing |

## Rollback

**Triggers.** Any one of these starts a rollback:
- Forgejo is not Ready 15 minutes after the swap, with no slow pull to explain it;
- `doctor` reports a problem the rehearsal did not;
- V2, V5, V6, V9, V10, V11 or V13 fails, and the cause is not a test-harness HTML scrape;
- repo heads or the SSH host key differ from the baseline.

**Point of no return: lifting the write freeze.** After that, a rollback loses users' writes, so we
fix forward unless the forge is unusable.

**Restore-based only.** An image revert alone cannot work: 15 refuses a 17 database. This is the
runbook's "real restore", aimed at the recorded artefacts instead of "newest". **STOP:** Chris
approves before step 3, because steps 3 and 4 are destructive on the production DB and volumes
(S4).

1. **Signed commit:** the image goes back to the 15.0.11 digest, `replicaCount: 0`, and the drill
   pins go back. Argo scales Forgejo to 0, so GitOps does it rather than a hand `kubectl scale`.
2. **Wait** for the pod to go and the RBD volumes to detach (S3; Monitor).
3. **Database.**
   - On the `acid-forgejo` leader: `DROP DATABASE forgejo WITH (FORCE); CREATE DATABASE forgejo
     OWNER forgejo;`
   - Load the recorded `spilo/…sql.gz` the `docs/backups/pg-restore.sh` way: keep the role
     passwords, allow errors, then re-apply the passwords. These are the same steps
     `restore-drill.sh` runs.
4. **Volumes.**
   - Run a loader pod with the 15.0.11 image that mounts `forgejo-data` and `forgejo-repos`.
   - Empty both volumes, then untar `repos/` and `data/` from the recorded
     `dump/forgejo-<UTC>.tar.gz`.
   - Write `app.ini` to `/data/gitea/conf/app.ini`, owned by uid 1000.
   - This also removes 17's central hooks and any repo config 17 rewrote (!13661 touches
     `receive.hideRefs`).
5. **Signed commit:** `replicaCount: 1`.
   - 15.0.11 starts.
   - Run `forgejo doctor check --all`.
   - Re-run V2, V9, V10 and V13.
   - Then lift the freeze.
6. **Afterwards.**
   - Tell users that nothing written after the freeze started survives. There should be nothing.
   - B2 objects that 17 wrote, if any, are orphans. Leave them.

**About 20–30 minutes** in total. The artefacts will already have loaded twice that day, in
pre-flight step 6 and in the rehearsal.

## Expected downtime

- **Outage, reads and writes: about 1–3 minutes** while the pod swaps.
  - A plain restart took about 20 s on 2026-11-04 (01:38:50Z → 01:39:10Z).
  - This one adds the first pull of the 17 image from `code.forgejo.org` and seconds of
    migrations.
  - If the pull is slow, wait (S3).
- **Write freeze: about 1 hour**, from the pre-flight dumps to the end of verification.
  - It is announced in advance.
  - Web reads, clones and pages serving continue throughout, except during the swap.
  - git-pages serves from B2 and never stops.
- **With a rollback, add 20–30 minutes** of outage.
- **Side effects:**
  - forge-bot loses one or two read cycles. That is fail-safe and causes no writes.
  - The runner is idle by gate.
  - Webhooks: nothing is pushed during the freeze, so nothing is dropped.

## Cadence after 17

This section applies if Decision 1 is A.

**18.**
- 18.0 is released on 2027-01-14, and 17 reaches EOL on 2027-01-28.
- Hop to the first 18.0.y with y ≥ 1 that is at least 14 days old. That should be early to mid
  February 2027.
- So we run an **EOL 17 for up to about 2 weeks** *(my call: keep the pin-age rule and prefer x.0.1)*.
- If a security release lands in that gap, Chris's usual case-by-case waiver applies.

**19 LTS.**
- 19.0 is released on 2027-04-15, and 18 reaches EOL on 2027-04-29.
- The same rule puts the hop at about mid-May 2027.
- **Then stay on 19 LTS** (supported until 2028-07-13) and take 23 LTS in spring 2028.

**Every hop:**
- write a one-page addendum to this doc: read the release notes and record the delta to the
  affects-us table;
- run the rehearsal;
- take the same pre-flight snapshot;
- run the same re-verify list.

18 brings !14148, which moves internal communication to a UNIX socket and removes
`INTERNAL_TOKEN`. Chart 17.3.2 already handles it, and the runbook's list of app secrets must
change.

**A soft fork** (a patch queue on upstream tags) is cut on 19 LTS, never on the stable track.

## Docs to update

These ship in the upgrade commit or right after it.

- **`docs/forgejo.md`:**
  - "Forgejo 17";
  - "A real restore", step 4: no `regenerate hooks` from 16 on;
  - the template-overrides table: 17.0.x, plus the header shim;
  - a short "Upgrading Forgejo" section that points here;
  - the drill pins.
- **CLAUDE.md**, Realliance forge row: "Forgejo 17 (stable track until 19 LTS)".
- **Comments:**
  - `forgejo-release.yaml`, `drill-values.yaml` and `restore-drill.sh`;
  - `cluster/pages/pages-relay.yaml` and `monitoring.yaml`: "Forgejo 15 never retries" becomes
    "Forgejo (15–17) never retries". It is still true.
- **Forge spec:** an amendment under "Decided: Forgejo 15 LTS".
- **forge-bot:** `internal/forgejo` says "Forgejo 15 API client". That is cosmetic, so change it
  with forge-bot's next change.

## Left to the builder

- The exact shim CSS, and which flavours count as light.
- The shape of the three new checks (V3 assert, V8, V14) and where they live in `tests/forge/`.
- The Renovate regex.
- Whether config and docs go in one commit or two.
- How the freeze is announced.

## Decisions for Chris

**Answered 2026-11-04: B** (see the banner at the top).

**1. Which track: 17 now, stay on 15 LTS until 19, or 16?**

This answer changes the pinned version, the drill pins, the runbook and the 2027 calendar.

- **A. 17 now, then 18 (about February 2027) and 19 LTS (about May 2027), then LTS again.**
  **My recommendation.**
- **B. Stay on 15 LTS.**
  - We take 15.0.x patches only. 15.0.12 is being prepared and carries the access-table fix.
  - We jump 15 → 19 LTS once a 19.0.y is two weeks old, about May–June 2027. 15 reaches EOL on
    2027-07-15.
  - That is one upgrade instead of three.
- **C. 16.** Not viable. It has been EOL since 2026-10-29, and it is not a required stop.

**Why A:**
- **The cost of each hop is small and mostly fixed right now.**
  - The code paths our consumers use are unchanged in source from 15.0.11 to 17.0.1.
  - The data is about 6 MB, and a restore takes 90 s.
  - The acceptance suites are fresh.

  Every later jump carries more users and more repos.
- **Smaller jumps are easier to handle.** Three hops of about 550 merged PRs each are easier to
  read, rehearse and bisect than one jump of about 2,200 PRs in mid-2027.
- **It closes a documented risk six months sooner.** v16's Authorized Integrations can replace
  forge-bot's static `REGISTRY_TOKEN` with a short-lived JWT bound to `realliance/forge-bot`,
  `ci.yaml`, `main` and tags, and `push`. That closes the forge-bot spec's accepted risk: a
  member's branch workflow can read the push token. The spec planned to switch "at v19".
- **More bug fixes.** Stable releases get every bug fix; LTS gets only critical ones. Actions is
  where we are building next: the runner scaler and the rerun, cancel and job-log APIs.

**What A costs, plainly:**
- **Two forced upgrades, each in a fixed two-week window:** 17 → 18 by 2027-01-28, and 18 → 19 by
  2027-04-29.
  - Under the 2-week pin-age rule there is **zero slack**. The day 18.0.0 may be pinned is the
    day 17 reaches EOL.
  - Taking x.0.1 (17.0.0 needed one) means running an **EOL 17, and later an EOL 18, for up to
    about 2 weeks each**, unless the rule is waived.
- **Each hop takes about half a day of operator time and a 1-hour write freeze.** Chris or Connor
  must be free in late January and again in late April.
- **Staying on the stable track past 19 multiplies this.** It means four majors a year. A soft
  fork's patch queue would be rebased onto about 550 PRs every quarter instead of once a year, so
  the fork belongs on 19 LTS.

**Choose B** if Authorized Integrations and the newer Actions APIs can wait until mid-2027, or if
the January and April upgrade slots cannot be promised.
