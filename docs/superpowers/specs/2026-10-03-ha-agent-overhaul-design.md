# Home Assistant: agent toolkit and overhaul — design

**Status:** draft for review · **Date:** 2026-10-03 · **Scope:** toolkit, ops fixes, automations, dashboard

## 1. Intent

**What Chris asked for.** Overhaul how Home Assistant is run and designed,
and make it something an agent can work on properly: edit dashboards and
system config, then *see* the result (screenshots) and *query* it, the way
any UI work is done. Past agent contributions felt half-baked because there
were no good skills or systems for HA. Start with scripts; an MCP server may
come later. Keep the agent's HA token in the repo, git-crypt'd.

On behaviour, he asked for smarter fans, every light manageable, the bedroom
wall switch as the single master over its lamps (it currently fights them),
a clear rule for scenes versus adaptive lighting, sensible kitchen lighting,
and all of it tied to whether he is home.

**Decisions made in the design conversation**

| Topic | Decision |
|---|---|
| Sequencing | One spec, four phases, toolkit first (each phase uses the previous one). Implemented as five plans: Phase 1 splits into toolkit core and declarative state. |
| Infrastructure as code | Everything that can be described lives in git. HA's `.storage` state (areas, devices, entity overrides, helpers, integrations, storage dashboards, card resources) is described by manifests and converged with `hactl plan`/`apply` (§4.11). Custom integrations and cards are pinned and installed by the chart, not HACS (§5). No imperative "set it once" steps. |
| Toolkit shape | In-repo Python CLI (`hactl`), not an off-the-shelf stack, not an MCP server (yet). |
| Dashboard audience | Chris only: phone (Pixel 9 Pro XL) and desktop browser. |
| Dashboard style | Keep the add-on card stack (mushroom, layout-card, card-mod). |
| Household | Chris, plus occasional guests → a guest mode. |
| Fans / AC | Free cooling (vent fan), floor fan always on except while printing, AC #1 cools when hot. |
| Lights | Underlight behaviour, consistent control, one grouping model (fix Hue/HA overlap). |
| Scenes vs adaptive | A scene holds until Chris taps **Auto** (or the room is switched fully off). |
| Agent actuation | Anything reversible is free while testing; locks and phone notifications always need a yes. |

**Not in scope:** an MCP server; replacing the add-on cards; new sensors
(motion, lux); fixing hardware (dead blind batteries, AC #2's IR); HA
voice/Assist; Zigbee changes (z2m stays as it is).

**Done when** (detail in §9):

1. An agent can find any entity, render any template, read traces and logs,
   preview a dashboard change without committing, and screenshot any view at
   phone and desktop width, all through `hactl`.
2. Every git change to HA config is applied and verified automatically after
   Argo syncs, and drift (repo ≠ running) is detected.
3. Everything HA-side that can be declared is declared in git; `hactl plan`
   is empty, and a non-empty plan is reported as drift.
4. HA is on current stable, and the known silent failures (§2) are fixed.
5. The automation behaviours in §7 run with zero errored traces for a week.
6. The dashboard shows the new controls, the known visual bugs are gone, and
   before/after screenshots at 412 px and 1440 px have no error cards.

## 2. Starting state

Observed read-only on 2026-10-03.

| Thing | State |
|---|---|
| Deployment | Argo `home-assistant-release` (chart `ha-helm` v0.4.1), image **2026.7.2**; current stable is 2026.9.x. Renovate's argocd manager tracks the chart but **not `image.tag`**. |
| Config delivery | `cluster/home-assistant/{packages,dashboards,themes}` → kustomize ConfigMaps → mounted into `/config`. **No reload after a change.** The pod has run since 2026-07-26, so `packages/prometheus.yaml` (committed 2026-09-23) has **never loaded** (`/api/prometheus` → 404). |
| Agent access | HA's built-in MCP server (Assist intents only: read/call exposed entities, no config). Everything else was ad-hoc `kubectl exec` + recorder SQL. No screenshots, no traces, no way to preview. |
| Silent failures | `plant_blinds_smart_solar_control` erroring ~2,250× ("SwitchBot Cloud device is offline"). Air-quality template erroring (its source `sensor.u_s_air_quality_index` no longer exists). `sensor.active_commute` invalid state. Living Room hero says "1 lights on" with 6 on (`expand()` on a Hue room light). Weather label renders "Partlycloudy". |
| Bedroom loop | `Sync Bedroom Switch and Lights` is bidirectional across 4 devices, `mode: restart`, with a context-based loop guard that cannot work: Hue and z2m report state back with fresh contexts. 2026-10-03 08:21 local: ~88 state changes in 18 s. Also, the Hue zone "TV" (`light.tv`) contains the bedroom's Dresser Lamp, so Living Room TV scenes drive the bedroom. |
| Lights | Hue bridge (lamps, Hue rooms/zones, ~50 Hue scenes) + z2m (Sidetable Lamp, Desk Underlight, Kitchen Underlight, Leviton DG15S wall switch). Adaptive lighting covers 7 Hue lamps; the 3 z2m lights are outside it. Camp Lamp is a plug (`switch.camp_lamp`). |
| Climate | Living Room: Vent Fan (Sonoff S31ZB plug, on the slider-door insert), Floor Fan (Sonoff S40, oscillating, beside the 3D printer), AC #1 (`climate.air_conditioner`, SwitchBot cloud IR, on the insert), AC #2 (does not respond to IR). Slider contact open ⇔ insert installed (summer only). Indoor: `sensor.living_room_temperature`, `sensor.bedroom_temperature`. Outdoor: `weather.forecast_home` (met.no). No air-quality source. No fan automations. |
| Presence | `person.chris_m` ← `device_tracker.pixel_9_pro_xl` (GPS). Chris enabled the companion app's Wi-Fi connection and time-zone sensors on 2026-10-03 (not yet reported at time of writing). `sensor.current_city` infers Seattle/Toronto from a calendar. |
| Integrations missing | OctoPrint (satellite `octoprint`, `192.168.0.243`, reachable), air quality, Plex (entry points at a stale pod IP). |
| Registry | 567 entities (308 disabled), 60 devices, 3 areas. Stale `mail_*` set and a Pixel 6 Pro registration linger. |
| Batteries | `sensor.plant_blinds_battery` 0% every day for the 120 days of statistics (solar never charged it). `sensor.window_right_battery` 5%, falling. `binary_sensor.fridge_door_contact` unavailable since July. |
| Chart | `generate-config` writes the recorder `db_url` **with the Postgres password in cleartext** into `/config/configuration.yaml` on the PVC. |
| Repo | **Public.** Nothing personally identifying (SSIDs, tokens, keys) goes in clear. |

## 3. Approaches considered

| Option | Verdict |
|---|---|
| **In-repo CLI (`hactl`) + one verified deploy path** | **Chosen.** Encodes *our* layout (packages via ConfigMap, YAML dashboards, `check_config` quirks) in code instead of skill prose. Testable, versioned, usable from any subagent via Bash, costs no context until used. An MCP wrapper later is thin. |
| Off-the-shelf (`hass-cli` + chrome-devtools MCP + a community HA MCP server) | Rejected. None of it knows the GitOps layout, so the knowledge stays in prose — the thing that has been failing. Community HA MCP servers write config into HA's `.storage`, which fights GitOps, and would hold an admin token. |
| Our own MCP server now | Deferred. Same functions as `hactl`, but tool schemas ride every turn and it is harder to iterate. Revisit once the command set is stable. |
| Imperative `registry` / `flow` commands in `hactl` | Rejected (Chris, 2026-10-03). One-off writes into `.storage` that no file describes are drift by construction. Replaced by manifests + `plan`/`apply` (§4.11). |
| Reloader (stakater) for config changes | Rejected. A full restart for every package edit (HA down ~1–2 min, in-flight `for:` timers lost) and it still would not *verify* anything. |
| A dedicated `agent` user (declared in the chart, or made by hand) | Not needed. Users can't be declared (they live in `.storage/auth`, an internal format), and HA has no scoped tokens, so a hand-made user would only add logbook attribution. Chris's own account holds the tokens. |

## 4. Phase 1 — the toolkit

### 4.1 Layout and runtime

```
tools/hactl/
  bin/hactl              # wrapper: env -u LD_LIBRARY_PATH python -m hactl "$@"
  src/hactl/
    __main__.py          # argparse dispatch
    client.py            # REST + WebSocket client, token loading
    query.py             # find/state/history/stats/template/trace/log
    shot.py              # Playwright screenshots + render report
    preview.py           # !include resolution, claude-preview dashboard
    lint.py              # static + live checks, check_config runner
    health.py            # repairs, errors, failing automations, drift
    act.py               # call (with actuation policy)
    state/               # declarative .storage state: model, live read, plan, apply, import, flows (§4.11)
    deploy.py            # wait for Argo sync + hook, then verify
    revision.py          # config revision hash (pre-commit + hook share it)
  tests/                 # pytest, fixtures only (no live HA)
  agent.secret.yaml      # git-crypt: agent token (see 4.2)
  README.md
```

The flake dev shell gains `python313.withPackages (ps: [ ps.websockets
ps.pyyaml ps.playwright ps.pytest ])` (replacing bare `python313`),
`playwright-driver.browsers` with `PLAYWRIGHT_BROWSERS_PATH` pointing at it,
and `tools/hactl/bin` on `PATH`. Done through the `nix-shell-pin` skill.

The wrapper unsets `LD_LIBRARY_PATH`: the host exports a system alsa-lib built
against a newer glibc than the flake's, which kills the nix-built browser
(found in the spike).

Output is short human-readable text by default; every command takes `--json`.
`hactl` never prints a token, and redacts `Authorization` headers from errors.

### 4.2 Auth and tokens

- **Tokens live on Chris's own (owner) account** — his choice. HA has no
  scoped tokens, so a separate user would only add logbook attribution. Three
  long-lived tokens, separately revocable from his profile:
  - `hactl` → `tools/hactl/agent.secret.yaml` (`token: …`). git-crypt covers
    it via the existing `*.secret.yaml` rule; it lives outside `cluster/`, so
    `sign.sh` never seals it into the cluster.
  - `gitops-reload` → `cluster/home-assistant/ha-reload-token.secret.yaml`
    (raw `kind: Secret`), sealed by `sign.sh` for the in-cluster hook (§4.6).
  - `prometheus-scrape` → `cluster/home-assistant/ha-metrics-token.secret.yaml`,
    sealed, for Prometheus (§5 item 1). Placed in the namespace where the
    `ScrapeConfig` lives.
- Chris handed them over in `~/.config/galaxy/{ha-token,reload-token,
  prom-token}` (mode 600); implementation copies them into the files above.
- Token lookup order: `$HA_TOKEN`, then `tools/hactl/agent.secret.yaml`.
- Runbook for rotating the tokens goes in `docs/ha.md`.

### 4.3 Commands

| Command | Does | Backed by |
|---|---|---|
| `find <text>` | Entities matching id/name, with state, area, device, integration. Filters `--area --domain --integration --unavailable`. | `/api/states` + WS `config/entity_registry/list_for_display`, device/area registries |
| `state <id>` | Full state + attributes + registry entry. | REST + WS |
| `history <id…> --since 2h` | State changes; flags flapping (≥ N changes/min). | `/api/history/period` |
| `stats <id> --days 120` | Daily min/max/mean from long-term statistics. | WS `recorder/statistics_during_period` |
| `template '<jinja>'` / `-f file` | Render against live state. | `/api/template` |
| `trace <automation> [--last N]` | Last runs: trigger, each step's result, errors. | WS `trace/list`, `trace/get` |
| `log [--errors] [--since]` | Deduplicated error/warning summary with counts. | WS `system_log/list` |
| `shot <path…>` | Screenshots + render report (§4.5). | Playwright |
| `preview <file> [--view N]` | Push a dashboard to `claude-preview` and screenshot it (§4.4). | WS `lovelace/*` |
| `lint [--offline]` | §4.7. `--offline` is the subset CI runs. | local + live |
| `health` | Repairs issues, top log errors, automations whose last run errored, referenced entities that are unavailable, drift (§4.6). | WS + REST |
| `call <domain.service> [--data]` | Call an action, under the actuation policy (§4.8). | REST |
| `import` | Write the `state/` manifests from live HA (bootstrap, §4.11). | WS registries, config entries, lovelace |
| `plan` | Diff `state/` manifests against live HA. Exit 0 when empty, 2 when not. | same |
| `apply [--prune]` | Converge live HA to the manifests (§4.11). | WS registry/lovelace APIs, config and options flows |
| `scene capture <room> <name>` | Read current light states of a room group and write them as a git scene definition. | REST |
| `deploy` | After a push: wait for Argo, wait for the hook, `apply`, then `health` and `shot`. Read-only on the cluster. | `kubectl` (read) + REST/WS |
| `selftest` | Read-only end-to-end smoke test against live HA. | all of the above |

### 4.4 Preview without committing

`preview` loads a dashboard file with a YAML loader that resolves `!include`
(relative to the file) and rejects `!secret`, then saves the result to a
storage-mode dashboard **`claude-preview`** (`require_admin: true`, not in the
sidebar; created on first use via `lovelace/dashboards/create`) with
`lovelace/config/save`, and screenshots it. The HACS card resources are
global, so add-on cards render there exactly as on `home-ops`. Nothing in
git, the ConfigMaps or `home-ops` changes. When the design is right, the same
file is committed.

### 4.5 Screenshots

Proven in the spike (8 authenticated shots in 28 s):

- Playwright Chromium, a fresh browser context per run (the token never lands
  in a persistent profile).
- An init script sets `localStorage.hassTokens` to `{hassUrl, clientId,
  access_token, token_type: "Bearer", refresh_token: ""}` before the frontend
  boots, and `dockedSidebar` to `"always_hidden"`.
- Viewports: **phone 412×915 at DPR 2** (touch, mobile) and **desktop
  1440×900**. `--scheme dark|light`, default dark.
- Waits until the `ha-card` count is stable for 1.5 s, then grows the viewport
  to the view's scroll height so one PNG holds the whole view.
- Theme: shots must render with warm-minimal, as on Chris's devices. The
  mechanism (backend default theme or a per-browser selection) is
  settled during implementation; `shot` asserts a warm-minimal CSS variable
  is present and fails if not.
- The report (stdout/JSON) lists, per shot: card count, `hui-error-card`
  texts, console errors and page errors, and any card text reading
  "Unavailable"/"unknown" with the card's title.
- Files go to `$XDG_CACHE_HOME/hactl/shots/<timestamp>/` unless `--out`.

### 4.6 Deploy, reload and drift

**Config revision.** A pre-commit hook (`hactl revision --write`) hashes
`packages/*.yaml` (excluding its own output), `dashboards/*.yaml` and
`themes/*.yaml`, and writes `packages/config_revision.yaml`:

```yaml
# GENERATED by pre-commit (hactl revision). Do not edit.
template:
  - sensor:
      - name: HA Config Revision
        unique_id: ha_config_revision
        state: "<first 12 hex of sha256>"
```

It is listed in the `ha-packages` configMapGenerator like any package. The
hook fails the commit when the file is stale, like `mirror-family-list`.

**`ha-reload` PostSync hook** (in the `home-assistant` Argo app):

1. A Job (`python:3.13-alpine`, pinned by digest) mounts `ha-packages` and a
   script ConfigMap, and reads the expected revision from the mounted file.
2. Calls `homeassistant.reload_all` with the `gitops-reload` token, then polls
   `sensor.ha_config_revision`; repeats every 20 s until it matches. Kubelet
   needs up to ~2 min to update the pod's ConfigMap volume. Timeout 6 min.
3. Collects every top-level key declared across the packages and checks each
   against `/api/config` → `components`. A declared integration that is not
   loaded (e.g. `prometheus:`) needs a restart: the Job patches the
   Deployment's `restartedAt` annotation (Role: `get`/`patch` on
   `deployments/ha-home-assistant` only), waits for HA to answer, and checks
   again. At most one restart per run.
4. Any timeout or still-missing integration exits non-zero, so Argo shows a
   failed hook. `hook-delete-policy: BeforeHookCreation` keeps the last Job for
   inspection.

**Drift checks in `hactl health`:** loaded revision ≠ repo revision;
integration declared in packages but not loaded; `ha-reload` hook last
failed.

**`hactl deploy`:** after `git push`, waits until the `home-assistant` Argo
app reports the pushed commit synced and the hook Job succeeded, runs `apply`
for the `state/` manifests (§4.11), then `health` and (when dashboards
changed) `shot` on the affected views. It makes no cluster writes; Argo and
the hook do the cluster side. This is the same path a
human push takes, so a forgotten restart cannot recur.

### 4.7 Lint

`lint --offline` (no HA needed; CI runs this, replacing the inline logic in
`.github/workflows/ha-check-config.yaml`):

- Package filenames are slug-safe (underscores, no hyphens) and every
  `packages/*.yaml` / `dashboards/*.yaml` / `themes/*.yaml` is listed in
  `kustomization.yaml`.
- `'on'`/`'off'`/times are quoted where YAML 1.1 would coerce them.
- `config_revision.yaml` is current.
- `check_config` in the **deployed** image tag with the custom components the
  packages need, grepping for error markers (exit code is meaningless).

`lint` (live) additionally:

- Every `entity_id`-shaped reference in packages and dashboards exists in the
  registry or states (catches `switch.bedroom_light_switch`-class mistakes).
- Every Jinja template in the dashboards renders without error.

### 4.8 Actuation policy

`call` follows Chris's rule: **anything reversible is free**. It refuses,
unless `--confirmed` is passed after Chris says yes:

- `lock.*`
- `notify.*`, `tts.*`, `assist_satellite.*` (things that reach a person)
- `homeassistant.restart` / `homeassistant.stop`

The `ha-config` skill states the same rule in prose so it also governs the
HA MCP server's tools.

### 4.9 Skill and docs

- Rewrite `.claude/skills/ha-config/SKILL.md` around the loop: **find → edit →
  lint → plan → preview/shot → commit/push → deploy → verify** (health,
  traces, shots). Registry, helper and integration changes are made by
  editing `state/` manifests, never ad hoc. Keep the existing hard-won gotchas. Remove the manual
  `rollout restart` step.
- `docs/ha.md`: runbook (tokens, deploy path, what the hook
  does, how to read a failed hook, the `state/` manifests, and the short list
  of things that cannot be declared).
- `CLAUDE.md`: one line in the topology table pointing at `hactl` and the
  skill.

### 4.10 Testing

- pytest on fixtures for the pure logic: entity-reference extraction,
  `!include` resolution, revision hashing, every offline lint rule,
  actuation-policy gate, history flap detection.
- `hactl selftest` exercises every read path against live HA (no writes).
- The Phase 2 `prometheus` deploy is the end-to-end test of §4.6: the hook
  must detect the unloaded integration and restart exactly once.

### 4.11 Declarative HA state

Everything HA keeps in `.storage` that can be declared is declared, in
`cluster/home-assistant/state/`: plain YAML read by `hactl`, not a kustomize
input and not mounted into the pod. The model is the one `cloudflare/dns/`
uses with terraform: `hactl plan` diffs the manifests against live HA,
`hactl apply` converges, and `health` reports a non-empty plan as drift.
Pushing to git stays the only way config changes; `hactl deploy` runs `apply`
after Argo syncs.

| File | Describes | Matched by | Managed |
|---|---|---|---|
| `areas.yaml` | floors, labels, areas (name, floor, icon) | their ids | Fully: create/update; delete needs `--prune`. |
| `devices.yaml` | area, display name, labels, disabled | any `identifiers` pair (Zigbee IEEE, Hue id…), which survives renames | Listed devices only. |
| `entities.yaml` | entity_id, name, area, labels, hidden, disabled; plus a `remove:` list | `platform` + `unique_id`, which survives renames | Overrides only: unlisted entities are untouched. |
| `helpers.yaml` | config-entry helpers (`switch_as_x`, group helpers) | domain + title | Fully (`--prune` to delete). |
| `integrations.yaml` | integrations that must exist, their flow answers and options; interactive ones carry the manual instruction | domain + title | Presence and options; never deleted. |
| `dashboards.yaml` | storage dashboards (`claude-preview`, `map`, `lovelace`) and Lovelace resources | `url_path` / `url` | Fully (`--prune` to delete). Dashboard *contents* are not here: YAML dashboards are files, `claude-preview` is scratch. |
| `credentials.yaml` | secrets that flows need (API keys) | — | git-crypt via an explicit `.gitattributes` line (deliberately not `*.secret.yaml`, so `sign.sh` never tries to seal it). |

- `hactl import` writes the first version of every manifest from live state,
  so the repo starts as an accurate description. After that the files are
  edited like any other config.
- `plan` prints `+ create`, `~ update (field: old → new)`, `- delete (needs
  --prune)` and `! manual: <instruction>`.
- `apply` order: floors → labels → areas → helpers → integrations → devices →
  entities → dashboards/resources, so areas and renamed ids exist before
  anything refers to them.
- Config flows are driven generically: at each step `hactl` answers the
  step's schema fields from the manifest's `flow:` answers merged with
  `credentials.yaml`. A required field with no answer stops with a clear
  message. Interactive steps (Hue link button, Plex sign-in, OctoPrint key
  approval, phone app registration) print their instruction, and `plan` stays
  non-empty until they are done.
- Values that would otherwise be "set once by hand" are in git instead: the
  home SSID via `!secret` (§7.2) and the climate tunables via `initial:`
  (§7.6).
- **Not declarable**, and listed as such in `docs/ha.md`: users and
  long-lived tokens, the human step of interactive integrations, and runtime
  state (recorder history, restore-state).

## 5. Phase 2 — ops fixes

1. **Scrape HA's metrics.** The `prometheus` integration is loaded by the end
   of Phase 1 (its end-to-end test, §4.10), but nothing scrapes it: the
   endpoint needs a bearer token. Seal the `prometheus-scrape` token (§4.2)
   and add a `ScrapeConfig` for
   `ha-home-assistant.home-assistant.svc:8123/api/prometheus` carrying the
   `release: prometheus` label (unlabelled monitors are never picked up).
   Retarget the home-automation dashboard's 15 HA panels from
   `home_assistant_*` to the real `homeassistant_*` names and verify with
   `scripts/check-dashboards.sh`.
2. **HA 2026.7.2 → latest 2026.9.x patch.**
   - Before: fresh `pg_dumpall` of `acid-ha` (recorder schema migrations make
     a plain tag revert unsafe), `hactl shot` of every view, `hactl health`.
   - Update HACS components first if their release notes require it
     (adaptive_lighting, mail_and_packages, smartrent).
   - `check_config` in the new image, then bump `image.tag`.
   - After: the same shots and `health`; diff both.
   - Rollback: revert the tag and restore the dump.
3. **Renovate:** a `customManagers` regex entry for `image.tag` in
   `cluster/applications/home-assistant-release.yaml` (docker datasource
   `homeassistant/home-assistant`), no automerge.
4. **Chart secret:** `ha-helm` v0.5.0 renders `db_url: !env_var HA_DB_URL`
   and sets `HA_DB_URL` on the main and `check-config` containers from the
   Secret (`$(PGPASSWORD)` dependent env). The generated `configuration.yaml`
   then holds no password. The existing value has also been in the
   restic-encrypted config backups; the DB is ClusterIP-only, so no rotation.
   The same chart release adds two features:
   - **`secrets.yaml` from a Secret:** a sealed `ha-secrets` Secret mounted at
     `/config/secrets.yaml` (subPath), so packages can use `!secret` (first
     user: the home SSID, §7.2). `lint --offline` writes dummy values for
     every `!secret` name it finds so `check_config` still runs in CI.
   - **Pinned components instead of HACS:** `cluster/home-assistant/
     components.yaml` lists every custom integration (adaptive_lighting,
     mail_and_packages, smartrent) and card (mushroom, layout-card, card-mod,
     horizon-card, calendar-card-pro, bubble-card, ultra-card,
     weather-alerts-card) with its GitHub repo and exact release. A
     ConfigMap of it feeds a chart init container that installs exactly
     those versions into `/config/custom_components` and
     `/config/www/community`. `hacs: false`; HACS is removed. Lovelace
     resources move to `/local/community/…?v=<version>` URLs declared in
     `state/dashboards.yaml`. A Renovate custom manager (github-releases)
     proposes version bumps. Before/after screenshots of every view gate the
     switch.
5. **Guards:**
   - Plant blinds automation: condition on the cover not being `unavailable`
     and the battery above 0, `max_exceeded: silent`.
   - Air-quality template rewired to the new AirNow sensor, with an
     `availability:` template.
   - `sensor.active_commute`: `availability:` template.
6. **Integrations**, declared in `state/integrations.yaml` and created by
   `hactl apply`:
   - OctoPrint at `192.168.0.243:80` — Chris approves the app key in
     OctoPrint's UI.
   - AirNow — needs Chris's free API key.
   - Plex — re-pointed at `http://mm-plex.media.svc.cluster.local:32400`
     (Chris in the UI if plex.tv sign-in is required).
   - Camp Lamp as a light via a `switch_as_x` helper (`light.camp_lamp`),
     declared in `state/helpers.yaml`.
7. **Current city:** `sensor.current_city` uses the phone's time-zone sensor
   (`America/Toronto` → Toronto, else Seattle), falling back to the calendar
   when the sensor is unavailable.
8. **Registry cleanup:** `hactl health --registry` lists devices without an
   area, entities unavailable > 30 days, and orphaned entities. Chris
   approves the deletion list, which becomes the `remove:` list in
   `state/entities.yaml`; area assignments go into `state/devices.yaml`.
   Every device ends up in an area.

## 6. Hardware follow-ups (Chris, outside this spec)

- Plant blinds: solar has never charged (0% for 120 days).
- `window_right` blind at 5%; check its panel.
- AC #2 ignores the SwitchBot's IR: try another IR code set, or a smart plug
  if the unit resumes after power returns.
- Fridge door contact unavailable since July (re-pair).
- Hue app: remove Dresser Lamp from the "TV" zone, unless intentional.

## 7. Phase 3 — automations

### 7.1 Conventions

- One package per feature. Every automation has a stable slug `id`, `alias`,
  `description` and explicit `mode`.
- Automations that act on cloud or IR devices check the target is not
  `unavailable`/`unknown` first.
- No bidirectional sync automations: one source of truth per behaviour.
- Only `home_alerts` sends phone notifications.
- `health` must show zero errored runs.

### 7.2 Presence (`packages/presence.yaml`)

- `sensor.home_wifi_ssid`: a template sensor whose `state: !secret
  home_wifi_ssid` comes from the sealed `ha-secrets` (§5 item 4), so the SSID
  is declared in git but not readable in the public repo.
- `binary_sensor.chris_home` (template): on when `person.chris_m` is `home`
  **or** `sensor.pixel_9_pro_xl_wifi_connection` equals
  `sensor.home_wifi_ssid`;
  `delay_off: 5 min`. Wi-Fi gives a fast, indoor-reliable "home"; GPS covers
  the rest.
- `input_boolean.guest_mode` (manual; dashboard chip).
- `binary_sensor.house_occupied`: `chris_home` or `guest_mode`. Every away
  behaviour keys off this.
- Away (occupied → off): all light groups and the Camp Lamp off.
- Evening arrival (`chris_home` → on, sun below horizon):
  `script.room_auto(living_room, turn_on=true)`.

### 7.3 Lighting model (`packages/lighting.yaml`)

- **HA owns grouping.** YAML light groups (`light: - platform: group`) made of
  individual bulbs, never Hue groups:
  - `light.living_room_all`: Floor Lamp A, Floor Lamp B, Ground Spot, TV
    Accent, Sidetable Lamp, Desk Underlight, Camp Lamp.
  - `light.bedroom_all`: Nightstand Lamp, Dresser Lamp, Floor Lamp.
  - `light.kitchen_all`: Kitchen Underlight.

  Automations and the dashboard target only these. Hue rooms/zones stay for
  the Hue app. If the target HA version no longer accepts YAML light groups
  (`check_config` says so), they become group helpers created via `hactl flow`
  and recorded in `docs/ha.md`.
- **Adaptive lighting on every dimmable light**: add Sidetable Lamp, Desk
  Underlight and Kitchen Underlight to the `circadian` instance. The sleep-mode
  schedule (22:30 on, 10:00 fallback off) stays.
- **Room modes:** `input_select.<room>_light_mode` with options `Auto` plus
  that room's scenes.
  - `script.room_scene(room, scene)`: `scene.turn_on`, then
    `adaptive_lighting.set_manual_control` (`manual_control: true`) for the
    room's lights, then select the option.
  - `script.room_auto(room, turn_on=false)`: `set_manual_control: false` for
    the room's lights, `adaptive_lighting.apply` (with `turn_on_lights` set
    from `turn_on`), select `Auto`. The dashboard's Auto chip uses
    `turn_on=false`; switch and arrival automations use `turn_on=true`.
  - When a room group turns fully off, its mode resets to `Auto` (adaptive
    lighting already clears manual control on off).
- **Curated scenes in git** (`scene:` entries), captured from the current Hue
  scenes with `hactl scene capture`. Starting set: Living Room — Bright,
  Relax, Movie, Read; Bedroom — Bright, Relax, Read, Night; Kitchen — Bright,
  Night. Chris trims or renames during Phase 4 preview review. Dashboard chips
  stop pointing at Hue scenes.

### 7.4 Bedroom wall switch (`packages/bedroom.yaml`)

- Delete `Sync Bedroom Switch and Lights` (id `1741747401508`).
- New one-way automation, triggered **only** by
  `switch.bedroom_bedroom_light_switch` changing `off`→`on` or `on`→`off`
  (never from/to `unavailable`): on → `script.room_auto(bedroom,
  turn_on=true)`; off → `light.bedroom_all` off. Lamp changes never touch the switch, so
  no loop is possible.
- Accepted trade-off: if the lamps were turned on from the app while the
  switch is off, pressing the paddle "off" does nothing (the relay is already
  off); press on, then off.

### 7.5 Kitchen underlight (`packages/kitchen.yaml`)

- Sun below horizon and house occupied → `light.kitchen_all` on (bare
  `turn_on`; adaptive lighting sets level and warmth).
- Sleep mode on → it becomes the night light (adaptive lighting's sleep
  values).
- Off at sunrise and when the house is unoccupied.

### 7.6 Climate (`packages/climate.yaml`)

All of this sits behind `input_boolean.climate_auto` (on by default) so the
whole feature can be paused from the dashboard.

- `binary_sensor.window_insert_installed`: slider contact open, `delay_on:
  30 min`, `delay_off: 5 min`. When off, vent fan and AC #1 automations stand
  down.
- Tunables (`input_number` with `initial:` from git, so git is the source of
  truth; change them by editing git): `free_cooling_delta` 2 °F,
  `free_cooling_floor` 70 °F, `aqi_limit` 100, `ac_cool_threshold` 78 °F.
- **Free cooling** — `binary_sensor.free_cooling_wanted`: insert installed,
  outdoor (`weather.forecast_home` temperature) ≤ indoor (Living Room) −
  delta, indoor ≥ floor, AQI ≤ limit (AQI unavailable → allowed). Hysteresis
  via `delay_on`/`delay_off` 10 min. The automation mirrors it to
  `switch.vent_fan_switch`.
  `sensor.free_cooling_status` explains the state: `running`, `warmer
  outside`, `cool enough`, `smoke`, `insert out`, `paused`.
- **Floor fan:** printing starts (`binary_sensor.octoprint_printing` → on) →
  `switch.floor_fan` off; printing ends → on. OctoPrint `unavailable` → no
  action. No periodic enforcement, so a manual off sticks.
- **AC #1** — `binary_sensor.ac_cool_wanted`: insert installed, Chris home,
  indoor ≥ threshold, and free cooling not viable (outdoor > indoor − delta).
  On at threshold, off at threshold − 2 °F (hysteresis via `this.state`) or
  when Chris leaves. Automation sets `climate.air_conditioner` to `cool` /
  `off`. IR is one-way, so HA's state is assumed; the dashboard says so.

## 8. Phase 4 — dashboard

- Keep mushroom, layout-card, card-mod and the warm-minimal look.
- Split `dashboards/overview.yaml` into a root file whose `views:` are
  `!include`s of `overview_home.yaml`, `overview_living_room.yaml`,
  `overview_bedroom.yaml`, `overview_kitchen.yaml`, `overview_calendar.yaml`,
  `overview_energy.yaml` (flat names; ConfigMaps have no subdirectories).
- Move the repeated `card_mod` blocks (card radius, transparent mushroom icon
  shapes, title sizes) into the warm-minimal theme as card-mod theme rules;
  per-card `card_mod` only for one-offs.
- New:
  - Guest mode chip in the Home hero band.
  - Per-room mode chips: the room's scenes plus **Auto**, active one
    highlighted.
  - Climate strip (Living Room view, summary chip on Home): insert in/out,
    `free_cooling_status`, floor fan (incl. "paused for print"), AC #1
    (assumed state), the tunables (read-only).
  - Printer tile on Home while printing.
- Fixes:
  - Light counts use the HA groups.
  - Weather condition label map ("Partly cloudy", "Clear night", …).
  - Kitchen tile shows underlight state and leak sensors, not "Tap to open".
  - Now Playing works once Plex is back.
- Process: iterate with `hactl preview`; acceptance with `hactl shot`.

## 9. Acceptance

| Phase | Done when |
|---|---|
| 1 | `hactl selftest` passes. `shot` and `preview` produce warm-minimal PNGs at 412 and 1440 with a render report. `lint --offline` replaces the CI logic and passes. pytest passes. The `prometheus` deploy goes through `ha-reload`, which restarts HA once and finishes green; `/api/prometheus` returns 200. `hactl import` manifests committed and `hactl plan` empty. `ha-config` skill rewritten. |
| 2 | HA on latest 2026.9.x; before/after shots and `health` show no regressions. Prometheus has `homeassistant_*` series and the dashboard's HA panels show data. Renovate rule merged. No password in `/config/configuration.yaml`. OctoPrint, AirNow and Plex entities live. Every custom integration and card is installed from `components.yaml` by the chart; HACS is gone; screenshots unchanged. `hactl plan` empty. `health` shows zero errored automation runs and no log errors from the guarded automations/templates. Registry cleanup approved and applied. |
| 3 | Each behaviour in §7 is exercised once live (reversible actuation) with a clean trace. Bedroom switch on/off ×5 produces exactly one lamp transition each, with no flapping (`hactl history` shows no burst). Free cooling and floor-fan behaviours verified by trace when their conditions next occur. A week later, `health` shows zero errored runs. |
| 4 | `lint` clean. Final `shot` set (all views × 412/1440 × dark, plus one light-mode set) has zero error cards and no unexpected "Unavailable". Before/after set shown to Chris. |

## 10. Risks and rollback

| Risk | Mitigation |
|---|---|
| `ha-reload` restarts HA repeatedly | At most one restart per run; failure is loud (failed hook) instead of looping. |
| Bad package reaches HA | CI `lint --offline` (incl. `check_config`) gates merges; the chart's `check-config` init container still blocks a bad restart. |
| HA upgrade migrates the recorder schema | Fresh dump first; rollback = revert tag + restore. |
| New climate logic misbehaves | `input_boolean.climate_auto` pauses it; each automation verified by trace. |
| Agent token leaks | git-crypt at rest, never printed; each token revocable individually from Chris's profile; in-cluster tokens are separate. |
| Preview dashboard confuses Chris | Admin-only, not in the sidebar, named `claude-preview`. |
| `apply` deletes or renames something wrongly | Creates/updates only by default; deletions need `--prune` and always show in `plan` first; `entities.yaml` is overrides-only, so unlisted entities are never touched. |
| Pinned components break the UI on switch-over | Before/after screenshots of every view; the old HACS files stay on the PVC until the new set is verified. |

## 11. Chris's actions

1. ~~Mint the three tokens (§4.2).~~ Done 2026-10-03.
2. Approve the OctoPrint app key when `hactl flow` asks.
3. Get a free AirNow API key.
4. Plex sign-in if the re-add flow requires it.
5. Hardware follow-ups in §6, at leisure.
