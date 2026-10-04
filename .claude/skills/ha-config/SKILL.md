---
name: ha-config
description: Change or inspect Home Assistant on galaxy — automations, helpers, templates, scenes, dashboards, themes, custom integrations/cards, HA upgrades — through git and the hactl toolkit, and verify the result live (screenshots, traces, health). Use for ANY HA config or dashboard work, any "why did this automation do X", and any HA debugging. NEVER edit package automations or YAML dashboards in the HA UI. NEVER install or update anything through HACS. NEVER deploy without `hactl lint`. NEVER call lock/notify actions without Chris's OK. NEVER let a generic YAML formatter touch HA config.
---

# ha-config

HA runs in-cluster (Argo `home-assistant-release`, chart `~/Repos/ha-helm`). Its config is git: `cluster/home-assistant/{packages,dashboards,themes}` → kustomize ConfigMaps → `/config`. **`hactl`** (`tools/hactl`, on PATH inside `nix develop`) is how you look at HA, check a change, see it, and verify the deploy. Runbook: `docs/ha.md`.

## The loop (do every step)

1. **Look first.** `hactl find <text> [--area Bedroom] [--domain light] [--unavailable]`, `hactl state <id>`, `hactl history <id…> --since 6h` (flags flapping), `hactl trace automation.<x>`, `hactl log --errors`, `hactl health`. Never guess an entity_id: they are sticky after Zigbee renames and mangled by integrations.
2. **Edit** files under `cluster/home-assistant/`. A new file must be listed in `kustomization.yaml` (lint catches a miss). Package filenames: lowercase + underscores. Registry, area, label, helper, integration, dashboard-list and resource changes are edits to `cluster/home-assistant/state/*.yaml` (docs/ha.md), checked with `hactl plan` and applied by `hactl apply` or deploy.
3. **Check.** Render new templates against live state first: `hactl template -f snippet.j2`. Then `hactl lint`: unknown entity ids, broken dashboard templates, quoting, kustomization, revision, and HA's `check_config` in the deployed image. An entity referenced on purpose before it exists goes in `cluster/home-assistant/lint-allow-missing.txt` with a reason.
4. **See dashboards before committing.** `hactl preview cluster/home-assistant/dashboards/overview.yaml --view N` pushes to the admin-only `/claude-preview` dashboard and screenshots phone (412) + desktop (1440). **Read the PNGs.** Fix every error card, every unexpected "unavailable", every visual problem; iterate until right.
5. **Commit and push** (`nix develop --command git commit …`). Pre-commit regenerates `packages/config_revision.yaml`; if it stops the commit, `git add` it and commit again.
6. **Deploy and verify.** `hactl deploy --shot` (run it in the background) waits for Argo and the `ha-reload` hook, then runs `health` and screenshots. Then exercise the change for real: trigger it with reversible actions (`hactl call …`), read the run (`hactl trace …`), and look (`hactl shot …`). A change is done when you have seen it work, not when it is pushed.

## Actuation policy (Chris, 2026-10-03)

Anything reversible is free while testing: lights, scenes, fans, covers/blinds, the AC, adaptive-lighting switches, the preview dashboard. **Ask Chris first** for locks, phone notifications, speech/announcements, and HA restart/stop; `hactl call` refuses those without `--confirmed`. The same rule applies to the HA MCP server's tools.

## How a change reaches HA

push → Argo syncs the `home-assistant` app (ConfigMaps) → the **`ha-reload` PostSync Job** calls `homeassistant.reload_all` until `sensor.ha_config_revision` equals the committed revision, and restarts HA once if a top-level integration in the packages isn't loaded (e.g. a new `prometheus:`). A failed hook shows in Argo and in `hactl health`; read it with `kubectl -n home-assistant logs job/ha-reload`. There is no manual `rollout restart` step any more.

## Dashboards (YAML mode)

One file per view behind `dashboards/overview.yaml`'s `!include`s (docs/ha.md "Dashboard"); shared tile styling lives in the Warm Minimal theme (card-mod rules; tiles opt in with `card_mod: {class: …}`), never copied per card; room mode chips call `script.room_scene` / `script.room_auto`. Prove a styling refactor with a pixel diff of preview vs same-time production shots — and first check each selector exists in the pinned card version (mushroom v5 template cards are tile-style; card-mod `$` doesn't work in theme strings), since identical pixels also result when both versions do nothing.

YAML dashboards live in `cluster/home-assistant/dashboards/` and are declared in the chart's `lovelace.dashboards` value (`urlPath` must contain a hyphen). **Never flip global `lovelace: mode: yaml`** — it disables the UI resource registry and breaks every add-on card. `check_config` validates the `lovelace:` schema, not dashboard contents — that is what `hactl lint` (templates, entity ids) and `hactl preview`/`shot` (error cards, rendering) are for.

## Hard rules / gotchas

- **Never edit package automations in the HA UI** (read-only there by design).
- **yamlfmt corrupts HA YAML** (strips quotes; YAML 1.1 then turns `on` into a boolean and `03:00:00` into 10800). HA dirs are excluded from yamlfmt; keep them excluded. Quote `'on'`/`'off'`/times (lint enforces it).
- **`check_config` exits 0 even on errors**; `hactl lint` greps it. It also can't catch runtime template errors: guard template sensors with `availability:` and check `hactl log --errors` after deploy.
- **Custom integrations and cards are pinned in the release, never installed through HACS** (HACS is still there; it installs nothing). Add or bump one in `cluster/applications/home-assistant-release.yaml` → `valuesObject.components` (`name`, `repo`, `version`; `url` for cards, and for integrations whose repo ships a release zip). A card also needs its resource `/local/community/<name>/<file>?v=<version>` in `state/dashboards.yaml`: lint fails until the two agree. `HACTL_LIVE=1 pytest tools/hactl/tests/test_components_live.py` before pushing: a version or asset that can't be downloaded fails the pod start (HA down until fixed). `hactl lint` clones each integration at its pinned tag for `check_config`. Details: docs/ha.md "Custom integrations and cards".
- **HA upgrades**: `pg_dumpall` first, `hactl lint --offline` with the new tag, then deploy with before/after shots (docs/ha.md "HA upgrades").
- **The HTTP config (proxies) is not YAML any more** (2026.9): it is declared in `state/system.yaml`, and drift is applied by a person in Settings → System → Network.
- **Context-based loop guards don't work for Hue/z2m devices**: they report state back with fresh contexts. Never write bidirectional sync automations; one source of truth per behaviour.
- **Behaviours live in feature packages** (docs/ha.md "Behaviours"): target `light.<room>_all`, never Hue rooms/zones; change a room's look through `script.room_scene` / `scenes.yaml` (`hactl scene capture` refreshes one from Hue); one owner per actuator decision (the bedroom switch race: two automations acting on the same flip raced — the second became a script the first calls); only `home_alerts.yaml` notifies the phone and every automation has id/alias/description/mode (lint enforces). Adaptive lighting's light list needs an HA restart.
- **On/off conditions need a `binary_sensor`.** A template `sensor` returning a boolean has state `True`/`False`, never `on` (this hid the commute tile for months).
- **`.storage` state is declarative** (`cluster/home-assistant/state/`): never change areas, labels, devices, entity names/ids, zones, persons, helpers, integrations' options, the default dashboard, storage dashboards or resources in the HA UI or by ad-hoc API calls; edit the manifest and `hactl apply`. Deleting needs `--prune`; hactl never deletes integrations. `people.yaml` and `credentials.yaml` are git-crypt'd: plan/apply refuse while a manifest (`people.yaml`) is locked; a locked `credentials.yaml` only turns creates that need credentials into manual changes.
- **Package filenames must use underscores**: HA rejects hyphenated package slugs and silently skips the whole file (`Package will not be initialized`). Lint enforces it.
- **The chart's `check-config` init container blocks a bad restart**: if HA restarts (the hook, an upgrade, a node reboot) with a config `check_config` rejects, the pod stalls in `Init` and HA is down on its RWO volume. Read `kubectl -n home-assistant logs deploy/ha-home-assistant -c check-config`, fix in git (or `git revert`), push. Never edit the Deployment by hand.
- **The recorder excludes the `automation` and `update` domains**, so they never appear in history or the recorder DB; use `hactl trace` / `hactl state` for automations.
- **`.storage` recovery**: the manifests describe it, but its data (and anything undeclarable: users, tokens, interactive integrations' credentials) is recovered from the restic `.storage` backup. To export a storage dashboard: `json.load('/config/.storage/lovelace.lovelace')['data']['config']`.
- **Never `--no-verify`** (CLAUDE.md S6).
