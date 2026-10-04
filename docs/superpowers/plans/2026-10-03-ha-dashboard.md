# HA Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put plan 4's behaviours on the Home dashboard (room modes with Auto, guest mode, climate strip, printer tile), fix what it shows wrong (light counts, weather labels, kitchen tile, commute tile), and make the YAML maintainable (one file per view, repeated styling in the theme), verified by screenshots at phone and desktop sizes.

**Architecture:** `cluster/home-assistant/dashboards/overview.yaml` (YAML-mode dashboard `home-ops`, mounted from the `ha-dashboards` ConfigMap) becomes a root file of `!include`d view files; repeated `card_mod` blocks move into the `Warm Minimal` theme as card-mod theme rules where that reproduces them pixel-identically. Every change is iterated with `hactl preview` (admin-only `/claude-preview`) and accepted with `hactl shot`; pixel diffs against a baseline gate the no-visual-change steps.

**Tech Stack:** Home Assistant 2026.9.4 Lovelace YAML mode, mushroom v5.1.1, layout-card v2.4.7, card-mod v4.2.1 (pinned in the release), hactl (preview, shot, lint), ImageMagick `compare`.

**Spec:** `docs/superpowers/specs/2026-10-03-ha-agent-overhaul-design.md` §8 (Phase 4 — dashboard) and §9 row 4 (acceptance); inputs from §7 as built (see the "As built (plan 4)" note).

## Global Constraints

- Work on `main` in `/home/chris/Repos/luma-homeops`. Commit with `nix develop --command git commit …`; never `--no-verify`; if a hook modifies files, `git add` and commit again. Trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- `export KUBECONFIG=/tmp/galaxy-kubeconfig` for deploys. git → Argo → ha-reload hook → `hactl deploy` (it waits for HA to load the committed revision).
- Keep mushroom, layout-card, card-mod and the warm-minimal look (spec §8). Amber is the only accent and marks state.
- Dashboard files are flat names in `cluster/home-assistant/dashboards/` (ConfigMaps have no subdirectories) and each must be listed in `kustomization.yaml` `ha-dashboards` (`hactl lint` checks).
- Never write the home Wi-Fi fragment, the SSID, or the Hue bridge's device name anywhere in git, docs, ledgers or summaries.
- Actuation while testing: reversible only (lights, scenes, fans, AC, input_* helpers, `script.room_*`); restore what you changed.
- Evidence: every task that should not change visuals ends with a pixel diff against the previous shot set (differences only in live values: clock, temperatures, light colours). Every task that changes visuals ends with `hactl preview` PNGs read and a deploy + `hactl shot`.

## Review Focus

1. **The include split silently drops or duplicates a card** → every view renders the same card count before and after (the shot report's card count), zero error cards. Test: Task 2 Step 4 card-count + pixel diff.
2. **Theme rules restyle cards they shouldn't** (other mushroom cards, calendar, todo) → only the targeted cards change; all other pixels identical. Test: Task 1 Step 5 pixel diff on all six views.
3. **Mode chips that call a scene the room doesn't have** → every chip's option exists in that room's `input_select` and `scene.<scene>_<room>`; tapping one sets the mode. Test: Task 4 Step 5 (lint entity refs + one live tap per room).
4. **Conditional tiles that leave a hole or never show** (printer tile, commute tile) → hidden cleanly when not applicable, shown when applicable. Test: Task 5 Step 6 preview with the condition forced via a preview-only copy.
5. **Light mode unreadable** (theme rules tuned only for dark) → the final light-mode set has no invisible text or icons. Test: Task 6 Step 2 light-mode shots read by eye.

---

### Task 1: Baseline, then repeated styling into the theme

**Files:** `cluster/home-assistant/themes/warm-minimal.yaml`, `cluster/home-assistant/dashboards/overview.yaml`.

- [ ] **Step 1: Baseline.** `hactl shot /home-ops/0 /home-ops/1 /home-ops/2 /home-ops/3 /home-ops/4 /home-ops/5 --viewport phone,desktop --out $SCRATCH/p5-base` (+ `--scheme light` into `$SCRATCH/p5-base-light`). Keep the report's card counts per view.
- [ ] **Step 2: Inventory.** The repeated blocks (2026-10-03): `&lighttile` (13 × mushroom-light-card: `ha-card { border-radius: 18px; }` + `mushroom-shape-icon { --shape-color: transparent; }`), `&roomhero` (3 room heroes: amber radial gradient, 24px radius, 44px icon, 20px/700 title), `&roomtile` (3 Home room tiles). One-offs (calendar 22px, todo max-height, the Home hero gradient) stay per card.
- [ ] **Step 3: Theme rule for the light tiles.** Add to `Warm Minimal` (top level, mode-independent):

```yaml
  # card-mod theme rules (card-mod v4): styling repeated on every tile of a kind.
  # Light tiles: 18px corners and a transparent icon shape (was &lighttile on 13 cards).
  card-mod-theme: Warm Minimal
  card-mod-card: |
    :host(.type-custom-mushroom-light-card) ha-card,
    ha-card.type-custom-mushroom-light-card { border-radius: 18px; }
    :host(.type-custom-mushroom-light-card) mushroom-shape-icon,
    ha-card.type-custom-mushroom-light-card mushroom-shape-icon { --shape-color: transparent; }
```

Deploy the theme change **alone** (cards still carry `&lighttile`, so nothing should change), then `hactl preview` a copy of the Living Room view with the `card_mod: *lighttile` lines removed and pixel-compare its lights column with the baseline. Identical → remove `*lighttile`/`&lighttile` from the dashboard. Not identical (the selector doesn't match in card-mod v4) → read the theme section of card-mod's README at the pinned tag (`curl -s https://raw.githubusercontent.com/thomasloven/lovelace-card-mod/v4.2.1/README.md`) and try the selector form it documents (e.g. `card-mod-card-yaml` with a `.: |` key); failing that, keep the per-card blocks, delete the theme rule, and ledger the ruling (the split in Task 2 then repeats the anchor once per view file).
- [ ] **Step 4:** Same procedure for `&roomhero` (rule scoped to the room hero cards — they are the only mushroom-template-cards with `--mush-icon-size: 44px`; if no selector can target exactly them, keep the anchor per file) and `&roomtile`.
- [ ] **Step 5:** Deploy; `hactl shot` all six views phone+desktop into `$SCRATCH/p5-t1`; pixel-diff against `p5-base` (`compare -metric AE -fuzz 8%`): only live-value differences; same card counts. Commit `dashboard: repeated tile styling moves into the Warm Minimal theme` (or the ledgered fallback).

### Task 2: One file per view

**Files:** `cluster/home-assistant/dashboards/overview.yaml` (root), create `overview_home.yaml`, `overview_living_room.yaml`, `overview_bedroom.yaml`, `overview_kitchen.yaml`, `overview_calendar.yaml`, `overview_energy.yaml`; `cluster/home-assistant/kustomization.yaml` (`ha-dashboards` files).

- [ ] **Step 1:** Move each top-level view mapping verbatim into its file (the file's content is the view mapping itself, not a list). Any anchor still used across views (Task 1 fallback) is defined at its first use in each file.
- [ ] **Step 2: Root file:**

```yaml
# Home dashboard (url home-ops, YAML mode). One file per view, flat names because
# ConfigMaps have no subdirectories; each is listed in kustomization.yaml.
views:
  - !include overview_home.yaml
  - !include overview_living_room.yaml
  - !include overview_bedroom.yaml
  - !include overview_kitchen.yaml
  - !include overview_calendar.yaml
  - !include overview_energy.yaml
```

- [ ] **Step 3:** Add the six files to `ha-dashboards`; `hactl lint` clean (kustomization, refs, templates); `hactl preview cluster/home-assistant/dashboards/overview.yaml --view 0` … `--view 5` renders (preview resolves `!include`).
- [ ] **Step 4 (Review Focus 1):** Commit `dashboard: one file per view`, push, `hactl deploy --shot`; full shot set into `$SCRATCH/p5-t2`; card counts per view equal to `p5-base`; pixel diff only live values.

### Task 3: Fixes — light counts, weather labels, kitchen tile, commute tile, now playing

**Files:** `overview_home.yaml`, `overview_living_room.yaml`, `overview_bedroom.yaml`, `overview_kitchen.yaml`, `cluster/home-assistant/packages/overview_brain.yaml`.

- [ ] **Step 1: Light counts and all-on/off use the HA groups.** Every `expand('light.living_room', 'light.sidetable_lamp', 'light.desk_underlight')`, `expand('light.bedroom')` etc. becomes `expand('light.<room>_all')`; the All on / All off chips target `light.<room>_all` (All off on the living room no longer needs `light.tv`). Check each rewritten template with `hactl template` against live state (count equals the number of on members).
- [ ] **Step 2: Weather label map.** In the Home hero (and anywhere else `states('weather.forecast_home') | replace('-', ' ') | title` appears) use:

```jinja
{% set w = states('weather.forecast_home') %}
{% set labels = {'clear-night': 'Clear night', 'cloudy': 'Cloudy', 'exceptional': 'Exceptional', 'fog': 'Fog',
  'hail': 'Hail', 'lightning': 'Lightning', 'lightning-rainy': 'Thunderstorms', 'partlycloudy': 'Partly cloudy',
  'pouring': 'Pouring', 'rainy': 'Rainy', 'snowy': 'Snowy', 'snowy-rainy': 'Sleet', 'sunny': 'Sunny',
  'windy': 'Windy', 'windy-variant': 'Windy'} %}
{{ labels.get(w, w | replace('-', ' ') | capitalize) }}
```

`hactl template` → the label for the live condition (e.g. `Partly cloudy`, never `Partlycloudy`).
- [ ] **Step 3: Kitchen tile** (Home rooms row): secondary shows the underlight and the leak sensors instead of "Tap to open":

```yaml
    secondary: >-
      Underlight {{ 'on' if is_state('light.kitchen_all', 'on') else 'off' }}
      · {{ 'Leak!' if is_state('binary_sensor.kitchen_sink_leak_sensor', 'on')
            or is_state('binary_sensor.water_heater_leak_sensor', 'on') else 'Dry' }}
    icon_color: "{{ 'red' if is_state('binary_sensor.kitchen_sink_leak_sensor', 'on') or is_state('binary_sensor.water_heater_leak_sensor', 'on') else ('amber' if is_state('light.kitchen_all', 'on') else 'disabled') }}"
```

- [ ] **Step 4: Commute tile hides when there is no commute.** `binary_sensor.show_commute` (overview_brain.yaml) gains `and states('sensor.active_commute') not in ['unknown', 'unavailable']` (Seattle has no commute source since Waze was removed). `hactl template` the new expression → `False` today.
- [ ] **Step 5: Now Playing.** Plex is loaded again (plan 3); its per-client `media_player` entities only exist while something plays, which the existing template handles (`Nothing playing`). Verify with `hactl template` that the expression renders `Nothing playing` now; no change unless it errors.
- [ ] **Step 6:** `hactl lint` clean; preview the four changed views (read the PNGs); commit `dashboard: HA light groups, weather labels, kitchen tile, commute tile`; push; `hactl deploy --shot`.

### Task 4: Room mode chips (scenes + Auto)

**Files:** `overview_living_room.yaml`, `overview_bedroom.yaml`, `overview_kitchen.yaml`.

- [ ] **Step 1: Chris's call on the TV section.** `AskUserQuestion`: "The Living Room 'Entertainment' block has a TV tile (the Hue 'TV' zone, which also contains the Dresser Lamp) and three Hue TV scene chips (TV Bright / TV Movie / Ambient). With room modes, Movie covers the TV look. Options: (a) drop the TV tile and TV chips (Recommended — one source of truth), (b) keep the TV tile, drop the chips, (c) keep both as they are." Apply the answer in Step 3.
- [ ] **Step 2: The chip pattern** (one per mode option; Auto first; the active option is amber):

```yaml
      - type: custom:mushroom-chips-card
        alignment: start
        chips:
          - type: template
            content: Auto
            icon: mdi:sun-clock
            icon_color: "{{ 'amber' if is_state('input_select.living_room_light_mode', 'Auto') else 'disabled' }}"
            tap_action:
              action: perform-action
              perform_action: script.room_auto
              data:
                room: living_room
          - type: template
            content: Relax
            icon: mdi:sofa-single
            icon_color: "{{ 'amber' if is_state('input_select.living_room_light_mode', 'Relax') else 'disabled' }}"
            tap_action:
              action: perform-action
              perform_action: script.room_scene
              data:
                room: living_room
                scene: Relax
```

Icons: Bright `mdi:brightness-7`, Relax `mdi:sofa-single`, Movie `mdi:movie-open`, Read `mdi:book-open-variant`, Night `mdi:weather-night`.
- [ ] **Step 3:** Replace the Hue scene chips: Living Room (Auto, Bright, Relax, Movie, Read) — the chips row that called `scene.living_room_*`; Bedroom (Auto, Bright, Relax, Read, Night) — replaces `scene.bedroom_*`; Kitchen (Auto, Bright, Night) — new row under its hero. TV section per Step 1. Afterwards `grep -n "scene\.\(living_room\|bedroom\|tv\)_" cluster/home-assistant/dashboards/*.yaml` → nothing (Hue scenes no longer referenced) unless Chris chose (c).
- [ ] **Step 4:** `hactl preview` each room view; read the PNGs (chips fit on one row at 412px; wrap acceptable, no overflow).
- [ ] **Step 5 (Review Focus 3):** `hactl lint` clean (entity refs); commit `dashboard: room mode chips (scenes + Auto) replace Hue scene chips`; push; `hactl deploy --shot`. Live: one tap per room via the API equivalent (`hactl call script.room_scene --data '{"room": "kitchen", "scene": "Bright"}'`, then `script.room_auto`) and a phone shot showing the active chip amber; restore Auto.

### Task 5: Guest chip, climate strip, printer tile

**Files:** `overview_home.yaml`, `overview_living_room.yaml`.

- [ ] **Step 1: Home hero band chips** (a `mushroom-chips-card` under the hero card, same grid area via a vertical-stack): Guest mode (toggle `input_boolean.guest_mode`; amber when on), Climate summary (`sensor.free_cooling_status`, icon by state; tap → `/home-ops/1`), Air quality (`sensor.airnow_air_quality_index`, `AQI {{ state }}`; amber > 50, red > 100).
- [ ] **Step 2: Printer tile on Home while printing** — a `conditional` card in the same vertical-stack:

```yaml
      - type: conditional
        conditions:
          - condition: state
            entity: binary_sensor.octoprint_printing
            state: "on"
        card:
          type: custom:mushroom-template-card
          primary: "Printing · {{ states('sensor.octoprint_job_percentage') | int(0) }}%"
          secondary: >-
            {{ states('sensor.octoprint_current_file') }}
            {% set t = states('sensor.octoprint_estimated_finish_time') %}
            {% if t not in ['unknown', 'unavailable'] %}· done {{ as_timestamp(t) | timestamp_custom('%-I:%M %p') }}{% endif %}
          icon: mdi:printer-3d-nozzle
          icon_color: amber
          tap_action:
            action: more-info
            entity: binary_sensor.octoprint_printing
```

- [ ] **Step 3: Climate strip on the Living Room view** — a new grid area `climate` (row after `lights`/`side`, full width; mobile after `side`) holding a title "Climate" and a chips row + one template card:
  - chips: Climate Auto (toggle `input_boolean.climate_auto`), Insert (`binary_sensor.window_insert_installed`: In/Out), Vent fan (`switch.vent_fan_switch`), Floor fan (`switch.floor_fan`; content `paused for print` when it is off and `binary_sensor.octoprint_printing` is on), AC #1 (`climate.air_conditioner` hvac mode + "assumed"), AQI.
  - template card: primary `Free cooling: {{ states('sensor.free_cooling_status') }}`; secondary the tunables read-only: `Δ {{ states('input_number.free_cooling_delta') }}° · floor {{ states('input_number.free_cooling_floor') }}° · AQI ≤ {{ states('input_number.aqi_limit') | int }} · AC at {{ states('input_number.ac_cool_threshold') }}°`; icon by status (`running` mdi:fan, `smoke` mdi:smoke, `insert out` mdi:window-closed-variant, `paused` mdi:pause, else mdi:fan-off).
- [ ] **Step 4:** `hactl lint` clean; `hactl preview` Home and Living Room (phone + desktop); read the PNGs.
- [ ] **Step 5:** Commit `dashboard: guest chip, climate strip, printer tile`; push; `hactl deploy --shot`.
- [ ] **Step 6 (Review Focus 4):** Conditional cards: preview a copy of the Home view whose printer condition is `state: "off"` (forces it visible) and whose commute condition is inverted, read the PNGs (no layout hole, content renders); production shots show neither while not printing / no commute.

### Task 6: Acceptance and the before/after for Chris

- [ ] **Step 1:** `hactl lint` clean.
- [ ] **Step 2 (Review Focus 5):** Final set: `hactl shot /home-ops/0 … /home-ops/5 --viewport phone,desktop --scheme dark` and `--scheme light` into `$SCRATCH/p5-final`. Zero error cards; no unexpected "Unavailable" (the known offline Fridge Door excepted). Read every PNG; fix any light-mode readability problem (own commit, then re-shoot).
- [ ] **Step 3: Show Chris.** Build a private before/after page (baseline `p5-base` vs `p5-final`, phone and desktop per view, dark, plus the light-mode set) as an Artifact (load the `artifact-design` skill first; images as files in the artifact), and give Chris the link. No Wi-Fi details, coordinates or tokens on it (screenshots show only dashboard content; check each image for anything sensitive before publishing).
- [ ] **Step 4:** `AskUserQuestion`: "Dashboard before/after is at <link>. Anything to change (scene names/trims, chip order, colours)?" — changes go in as their own commits with preview + shot; scene renames edit `scenes.yaml`, the mode `input_select` options and the chips together.

### Task 7: Docs, spec as-built, memory

- [ ] `docs/ha.md`: dashboard layout (root + view files), theme rules (or the fallback), how to add a view or a mode chip.
- [ ] Spec §8 as-built note (style of §5/§7): what moved into the theme, TV-section decision, chips/strip details, acceptance evidence.
- [ ] `ha-config` skill: "one file per view; shared tile styling lives in the theme; mode chips call `script.room_scene`/`room_auto`".
- [ ] Memory: overhaul complete (plans 1–5); pointer to docs/ha.md and the spec.
- [ ] Commit `docs: plan 5 (dashboard) runbook, as-built, skill`; push.
