# Home Assistant runbook

HA on galaxy: `https://home.chrismiller.xyz`, namespace `home-assistant`, Argo apps `home-assistant` (kustomize: config ConfigMaps, backups, the `ha-reload` hook) and `home-assistant-release` (helm chart `ChristopherJMiller/ha-helm`). Design: `docs/superpowers/specs/2026-10-03-ha-agent-overhaul-design.md`. Day-to-day workflow: the `ha-config` skill.

## hactl

`tools/hactl`, on PATH inside `nix develop`. `hactl --help` lists commands; each takes `--json`.

| Command | Use |
|---|---|
| `find`, `state`, `history`, `stats`, `template`, `trace`, `log` | Read live HA (history flags flapping; trace shows each step) |
| `shot PATH…` | Screenshots at phone 412 / desktop 1440, with a report (error cards, theme, unavailable cards, console errors) |
| `preview FILE` | Push a dashboard to `/claude-preview` (admin-only scratch) and screenshot it |
| `lint [--offline]` | Config rules + `check_config`; live adds entity-id and dashboard-template checks |
| `health` | Drift, hook status, failing automations, repairs, top log errors |
| `call DOMAIN.SERVICE` | Actions; locks/notify/tts/restart need `--confirmed` |
| `deploy [--shot]` | After a push: wait for Argo + hook + rollout, apply the committed `state/` manifests, then health (+ shots); refuses uncommitted `state/` edits |
| `revision`, `selftest` | Config revision hash; read-only end-to-end check |
| `import [--force]` | Write `state/` manifests from live HA (bootstrap; refuses to overwrite) |
| `plan` | Diff `state/` manifests against live HA; exit 2 when there are changes |
| `apply [--prune]` | Converge HA to the manifests; deletes only with `--prune`; never deletes integrations |

`deploy` and `health` read the cluster with kubectl: `export KUBECONFIG=/tmp/galaxy-kubeconfig` first.

Entities referenced on purpose before they exist (an integration still to be added) are listed, with a reason, in `cluster/home-assistant/lint-allow-missing.txt`; everything else unknown fails `lint`.

## Tokens

All three are long-lived tokens on Chris's own HA account (his choice; HA has no scoped tokens). Each is revocable on its own from his HA profile → Security.

| Token | Lives in | Used by |
|---|---|---|
| hactl | `tools/hactl/agent.secret.yaml` (git-crypt) | `hactl` (or `$HA_TOKEN`) |
| gitops-reload | `cluster/home-assistant/ha-reload-token.secret.yaml` (git-crypt) → sealed `ha-reload-token.yaml` | the `ha-reload` hook |
| prometheus-scrape | `cluster/prometheus-stack/ha-metrics-token.secret.yaml` (git-crypt) → sealed `ha-metrics-token.yaml` (namespace `prometheus`) | Prometheus (`ha-scrapeconfig.yaml`) |

Rotate: mint a new token in HA and save it to a mode-600 file (never paste it into a terminal or chat). For the sealed one, regenerate the `.secret.yaml` straight to disk (never let it print; don't `cat` it afterwards):

```bash
( umask 077 && kubectl create secret generic ha-reload-token -n home-assistant --from-file=token="$HOME/.config/galaxy/reload-token" --dry-run=client -o yaml > cluster/home-assistant/ha-reload-token.secret.yaml )
```

Then delete the old `ha-reload-token.yaml`, run `./sign.sh`, commit, push, and revoke the old token. For the Prometheus token, strip the file's trailing newline first (`tr -d '\n' < prom-token > tmp`, then `--from-file=token=tmp`, then delete tmp): `--from-file` keeps it, and Prometheus refuses the header (`invalid header field value for "Authorization"`). Symptom of a dead hactl token: `HA rejected the token (401)`, or `shot` failing with "HA showed its login page".

## How a change reaches HA

1. Pre-commit writes `packages/config_revision.yaml`: a hash of packages, dashboards and themes, exposed as `sensor.ha_config_revision`.
2. Argo syncs the ConfigMaps. Kubelet updates the pod's mounted files within ~2 min.
3. The **`ha-reload` PostSync Job** (`hooks/ha_reload.py`) calls `homeassistant.reload_all` every 20 s until the sensor shows the new revision (timeout 6 min). It then checks every top-level package key is a loaded integration. If one isn't, it restarts HA once (patches the Deployment's `restartedAt`; the strategy is `Recreate`) and waits until it is.
4. `hactl deploy` waits for all of that, then runs `health`.

A failed hook: `kubectl -n home-assistant logs job/ha-reload`. It keeps the last run until the next sync. Fix the cause in git and push; the next sync reruns it.

## Screenshots: how auth works

HA's frontend keeps OAuth tokens in `localStorage.hassTokens`, not a cookie. `hactl shot` plants the hactl token there before the frontend boots, in a throwaway browser context. The wrapper drops the host's `LD_LIBRARY_PATH` (a system alsa-lib built against a newer glibc kills the nix-built browser). HA's frontend logs a harmless "Subscription not found" rejection on most loads; the report filters it.

## Declarative state (`cluster/home-assistant/state/`)

HA's `.storage` parts are described in git and converged with `hactl apply` (terraform-style). `hactl deploy` runs `apply` after every push; `hactl health` reports a non-empty plan as drift.

| File | Holds | Matched by | Managed |
|---|---|---|---|
| `areas.yaml` | floors, labels, areas | id (HA's slug of the name at creation) | fully; deletes need `--prune` |
| `devices.yaml` | area, name, labels, disabled | one `identifiers`/`connections` pair (compared as strings) | listed devices and fields only |
| `entities.yaml` | entity_id, name, icon, area, labels, hidden, disabled; `remove:` list | platform + unique_id, plus `domain` when two domains share them (e.g. a template sensor and binary_sensor); an ambiguous match is a manual change | listed entities and fields only |
| `helpers.yaml` | config-entry helpers: `create` (menu + answers), `options` | domain + title | fully; deletes need `--prune` |
| `integrations.yaml` | integrations that must exist; `create`, `options`, `credentials`, `manual` | domain + title | presence + options; never deleted |
| `dashboards.yaml` | storage dashboards, Lovelace resources; `default:` (the dashboard `/` opens: `home-ops`); a dashboard's `config:` names a file in `dashboard_configs/` with its contents | url_path / url | fully; deletes need `--prune` |
| `people.yaml` | zones, persons (git-crypt: zone coordinates; the repo is public) | id | zones fully (deletes need `--prune`); persons never deleted |
| `system.yaml` | `http:` — HA's HTTP server config (`use_x_forwarded_for`, `trusted_proxies`), in HA storage since 2026.9 | — | drift only: `plan` shows a manual change; set it in Settings → System → Network (HA restarts into a trial that reverts unless promoted) |
| `credentials.yaml` | secrets for config flows (git-crypt; never print it) | key named by `credentials:` | — |

To change any of it: edit the manifest, `hactl plan`, then commit and push (deploy applies), or `hactl apply` directly. Never change these things in the HA UI or with ad-hoc API calls; the next plan flags them and apply reverts them. yamlfmt formats these files on commit; that is expected.

While a manifest is still git-crypt encrypted (CI, a fresh clone), `plan`/`apply` refuse to run: read as empty, its objects would look undeclared and `--prune` would delete them. `git-crypt unlock` first.

Cannot be declared: users and long-lived tokens; the human step of interactive integrations (Hue link button, OctoPrint app-key approval, phone app registration: `plan` shows their `manual:` text until done); runtime state (history, restore-state). Plex *is* declared: hactl creates it through its manual-setup flow on `mm-plex.media.svc.cluster.local:32400` (plex.tv sign-in would store the pod IP, which dies when the pod moves).

The pre-2026-07 Overview (`lovelace`) is retired: it shows a pointer to Home, which is the default dashboard; its old contents are in the restic `.storage` backup.

## Behaviours (packages)

One package per feature; every automation has an `id`, `alias`, `description` and `mode`, and only `home_alerts.yaml` notifies the phone (`hactl lint` enforces both).

- **Presence** (`presence.yaml`): `binary_sensor.chris_home` = the GPS zone, or the phone's SSID containing the home fragment (case-insensitive; sealed in `ha-secrets`, never in git), held 5 min on the way out. `input_boolean.guest_mode` keeps `binary_sensor.house_occupied` on. Away = `house_occupied` off: every room off. Arriving after sunset: living room on in Auto.
- **Lighting** (`lighting.yaml`, `scenes.yaml`): automations and the dashboard target `light.living_room_all` / `bedroom_all` / `kitchen_all`, never Hue rooms or zones. Adaptive lighting (`circadian`) runs every dimmable light; Camp Lamp is on/off only. A room is in Auto or holds a scene until Auto — adaptive lighting's global resets (sleep mode at 22:30/10:00, its main switch, restarts) are undone for held rooms by `lighting_hold_scenes_after_adaptive_reset`: `script.room_scene` (marks the lights manual *first* — adaptive lighting otherwise overrides a scene on lights that were off), `script.room_auto` (hands them back; `turn_on` to switch them on), mode in `input_select.<room>_light_mode`; a room that goes fully off returns to Auto. Scenes are `scene.<scene>_<room>` (the Hue scenes own `scene.<room>_<scene>`); refresh one from its Hue scene with `hactl scene capture --activate scene.<hue> --lights … --id … --name …` (it restores the lamps afterwards). Changing adaptive lighting's light list needs an HA restart (it reads its YAML only at startup).
- **Bedroom wall switch** (`bedroom.yaml`): the only automation on the switch. On = bedroom lamps on in Auto, or `script.start_your_day` while the morning routine is armed (`wake_up_pending`); off = lamps off. Lamps never drive the switch.
- **Kitchen underlight** (`kitchen.yaml`): on after sunset while occupied (adaptive level; the night light in sleep mode), off at sunrise or when the house empties. A manual off sticks until the next sunset or arrival.
- **Climate** (`climate.yaml`), behind `input_boolean.climate_auto`: slider open 30 min = insert in. Vent fan follows `binary_sensor.free_cooling_wanted` (outside ≥ delta cooler, room ≥ floor, AQI ≤ limit; unknown AQI allowed); floor fan off while OctoPrint prints; AC #1 cools when the insert is in, the house is occupied (guests count), the room ≥ threshold (holds to threshold − 2) and outside air can't help (±1 °F margin, 5 min either way); it is turned off only from `cool`, so a fan mode set by hand stays — IR is one-way, so HA's AC state is assumed. The floor fan is resumed after a print only if climate paused it (`input_boolean.floor_fan_paused_for_print`). `sensor.free_cooling_status` says why (`paused`, `insert out`, `running`, `starting`, `no data`, `smoke`, `warmer outside`, `cool enough`). Tunables are `input_number`s with `initial:` in git: change them there. Decision sensors are `unavailable` while an input is unknown (no command on missing data); the actuators resync on the first known value after a reload and when Climate Auto is switched back on. The insert sensor is trigger-based, so it survives reloads.

`hactl health --registry` adds area-less devices, registry entries no integration provides (restored-only: add them to `entities.yaml` `remove:`), and entities unavailable for all recorded history. Every device has an area; bridges, servers and accounts are in `Network`.

## Dashboard (`home-ops`)

YAML mode, declared in the release's `lovelace.dashboards`. `dashboards/overview.yaml` is a root of `!include`s, one file per view (`overview_home.yaml`, `overview_living_room.yaml`, `overview_bedroom.yaml`, `overview_kitchen.yaml`, `overview_calendar.yaml`, `overview_energy.yaml`); flat names because ConfigMaps have no subdirectories, and each must be listed in `kustomization.yaml` (`ha-dashboards`; lint checks). A new view = a new file + a root line + a kustomization line.

Shared tile styling lives in the Warm Minimal theme as card-mod rules (`card-mod-card`): light tiles by their type, Home room tiles and room-page heroes by a one-line `card_mod: {class: roomtile|roomhero}`. Per-card `card_mod` is for one-offs only. Any change to shared styling: deploy the theme rule first, then `hactl preview` the dashboard without the per-card block and pixel-compare it (`compare -metric AE -fuzz 8%`) with a same-time production `hactl shot`.

Room mode chips: one template chip per `input_select.<room>_light_mode` option, amber when active, tapping `script.room_auto` (Auto) or `script.room_scene` (a scene). Adding a mode = a `scenes.yaml` entry `scene.<mode>_<room>`, the option in the room's `input_select`, and a chip. Conditional tiles (printer, commute) are checked by previewing a copy with the condition inverted.

## Custom integrations and cards (pinned)

They live in the release, `cluster/applications/home-assistant-release.yaml` → `valuesObject.components`, at exact versions. The chart's `install-components` init container fetches them from GitHub before `check-config` runs and reinstalls one only when its version or source changes. A version or asset that cannot be downloaded fails the pod start (the installed copy stays on the volume, but HA is down until the release is fixed or reverted), so `cd tools/hactl && HACTL_LIVE=1 nix develop ../.. --command python -m pytest tests/test_components_live.py` checks every URL resolves before you push.

- **Integration**: `{name: <dir under custom_components>, repo: owner/name, version: <tag>}`. Add `url:` (the release zip, `{version}` substituted) for repos that HACS installs as `zip_release`: their CI stamps the version into the zip, and the source tree says `0.0.0-dev`.
- **Card**: `{name: <dir under www/community>, repo, version, url}`, and a Lovelace resource in `state/dashboards.yaml`: `/local/community/<name>/<file>?v=<version>` (type `module`). `hactl lint` fails until every card has exactly that resource, so a bump moves both lines. Reinstalling a card empties its directory: aiohttp serves a stale `<file>.gz` in preference to a new `<file>`.
- **Bumps**: Renovate opens one PR for HA core and one for the components (groups `home-assistant core` / `home-assistant components`, never automerged, kept out of the repo-wide non-major bundle). A card bump fails CI lint until its `?v=` resource line moves too: edit the PR. Merge, `hactl deploy`, then `hactl apply` for the resource line.

HACS is still installed (Chris kept it) but installs and updates nothing: never install or update through its UI. An update made there is overwritten on the next pinned bump, and any `/hacsfiles` resource it adds shows up as plan drift. Its "update available" entities just echo upstream releases.

## HA upgrades

Renovate bumps `image.tag` in the release. Before merging: read the release's breaking changes, `pg_dumpall` the `acid-ha` cluster to a local file, and run `hactl lint --offline` with the new tag (`check_config` runs in that image with the pinned integrations). After: `hactl deploy --shot`, `hactl health`, compare shots.

**Rollback** (only if HA is broken on the new version): revert the tag commit and push; HA restarts on the old image. The recorder migrates its schema forward on upgrade, so if the old version then refuses to start the recorder, restore the pre-upgrade dump into a recreated database — ask Chris first. From the local dump: `psql -U postgres -f <dump>` inside `acid-ha-0`; from the operator's nightly B2 dump: `docs/backups/pg-restore.sh home-assistant acid-ha acid-ha`. Delete the local dump once the upgrade has soaked (it holds the full recorder history, location trackers included).

2026.9 moved the `http:` config into storage (see `system.yaml`); the chart no longer emits an `http:` block.

## Metrics

`packages/prometheus.yaml` exports `homeassistant_*` at `/api/prometheus`; Prometheus scrapes it every 60 s (`cluster/prometheus-stack/ha-scrapeconfig.yaml`, job `home-assistant`). The Grafana *Home Automation* dashboard reads those series; after changing a panel run `scripts/check-dashboards.sh home-automation`.
