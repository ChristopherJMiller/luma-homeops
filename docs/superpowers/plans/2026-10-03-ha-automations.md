# HA Automations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the house behave: HA-owned light groups with adaptive lighting everywhere and scenes that hold until Auto, a one-way bedroom wall switch, kitchen underlight, presence with guest mode, and a climate brain (free cooling, print-aware floor fan, AC #1), all as git packages verified live.

**Architecture:** Feature packages in `cluster/home-assistant/packages/` (one per feature), two `hactl` additions (`scene capture`, `health --registry`), one chart feature (`ha-helm` v0.6.0: `secrets.yaml` from a Secret) for the home Wi-Fi match, and the state engine for device areas. Every behaviour is exercised live with reversible actions and read back with `hactl trace`.

**Tech Stack:** Home Assistant 2026.9.4 YAML packages (template, light group, adaptive_lighting 1.31.0, input_*, script, scene, automation), Python 3.13 (hactl, pytest), Helm + helm-unittest (`~/Repos/ha-helm`), Sealed Secrets, Argo CD.

**Spec:** `docs/superpowers/specs/2026-10-03-ha-agent-overhaul-design.md` §7 (Phase 3 — automations), plus the two items plan 3 carried forward (§5.4 `ha-secrets` mount; §5.8 `hactl health --registry` and every device in an area — see the "As built (plan 3)" note at the end of §5).

## Global Constraints

- Work on `main` in `/home/chris/Repos/luma-homeops` (and `main` of `~/Repos/ha-helm` for Task 1). Commit with `nix develop --command git commit …` (ha-helm: `nix-shell --run 'git commit …'`); never `--no-verify`. If yamlfmt or the revision hook modifies files, `git add` them and commit again. Trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Tests: `cd tools/hactl && nix develop ../.. --command python -m pytest -q > LOG; echo $?` — read pytest's own exit code, never through a pipe.
- `export KUBECONFIG=/tmp/galaxy-kubeconfig` for live steps. No direct cluster writes: git → Argo → ha-reload hook → `hactl deploy`.
- Secrets: never print the SSID, tokens or `credentials.yaml`; secret files are written through pipes or `--from-file`; verify every committed secret is GITCRYPT (`git show HEAD:<file> | head -c 9 | od -c`). The repo is public.
- Actuation (Chris, 2026-10-03): anything reversible is free while testing — lights, scenes, fans, the AC, the bedroom wall switch relay, input_* helpers. Locks, phone notifications, speech and HA restart/stop need Chris (`hactl call` enforces it).
- §7.1 conventions for every automation touched or added: slug `id`, `alias`, `description`, explicit `mode`; cloud/IR targets checked for `unavailable`/`unknown` first; no bidirectional sync; only `home_alerts` notifies the phone.
- Before/after evidence for HA-wide changes: `hactl shot /home-ops/0 … /home-ops/5` phone+desktop, `hactl health`, `hactl plan`.
- Live facts this plan relies on (checked 2026-10-03): the phone's SSID sensor is `sensor.pixel_9_pro_xl_wi_fi_connection` (spec says `…_wifi_connection`); AC #1 is `climate.air_conditioner` (`cool`/`off` supported, IR via SwitchBot cloud); the insert's contact is `binary_sensor.slider_door_contact`; vent fan `switch.vent_fan_switch`, floor fan `switch.floor_fan`; AQI `sensor.airnow_air_quality_index`; indoor `sensor.living_room_temperature` (°F); outdoor `state_attr('weather.forecast_home','temperature')` (°F). Adaptive lighting is the YAML `circadian` instance: main switch `switch.circadian_adaptive_lighting_circadian`, sleep `switch.adaptive_lighting_circadian_adaptive_lighting_sleep_mode_circadian`. Desk/Kitchen Underlight are xy-only, Sidetable Lamp brightness-only, Camp Lamp on/off only (not adaptive). Hue scene entity ids (`scene.living_room_relax`, …) are taken, so git scenes are named `<Scene> (<Room>)` → `scene.relax_living_room`.

## Review Focus

1. **Bedroom switch vs morning routine both fire on the same `off→on`** → while `input_boolean.wake_up_pending` is on, only the morning routine acts; otherwise only the bedroom automation acts. Test: Task 7 Step 4 exercises both states and reads both traces.
2. **A scene that never lets go** (manual control not cleared, mode stuck off `Auto`) → turning a room fully off resets its mode to Auto and adaptive lighting takes the lights back on the next on. Test: Task 5 Step 7.
3. **Climate flapping or acting on stale/unavailable inputs** (weather/AQI/OctoPrint/AC unavailable, insert just removed) → no command sent; hysteresis holds. Test: Task 9 what-if cases + Step 6 trigger checks.
4. **The home Wi-Fi match string leaking into the public repo or logs** → only the sealed Secret and the git-crypt'd `.secret.yaml` hold it. Test: Task 2 Step 5 grep of the committed tree + GITCRYPT check.
5. **Away turns off lights a guest is using** → `house_occupied` stays on while guest mode is on; away keys off `house_occupied`, not `person`. Test: Task 6 Step 5.

---

### Task 1: ha-helm v0.6.0 — `secrets.yaml` from a Secret

**Repo:** `~/Repos/ha-helm`. **Files:** `templates/deployment.yaml`, `values.yaml`, `Chart.yaml` (0.6.0), `README.md`, `tests/v060_test.yaml`.

**Interfaces:**
- Produces (values): `secretsFile: {enabled: false, secretName: ha-secrets, key: secrets.yaml}`. When enabled, volume `ha-secrets` (Secret `secretName`, item `key` → path `secrets.yaml`) mounted at `/config/secrets.yaml` (`subPath: secrets.yaml`, readOnly) in the main container **and** `check-config`.

- [ ] **Step 1: Failing tests** `tests/v060_test.yaml`:

```yaml
suite: v0.6.0 secrets file
templates:
  - deployment.yaml
set:
  checkConfig:
    enabled: true
tests:
  - it: no secrets mount by default
    asserts:
      - notContains:
          path: spec.template.spec.volumes
          content:
            name: ha-secrets
          any: true
  - it: mounts secrets.yaml from the Secret into HA and check-config
    set:
      secretsFile:
        enabled: true
        secretName: ha-secrets
        key: secrets.yaml
    asserts:
      - contains:
          path: spec.template.spec.volumes
          content:
            name: ha-secrets
            secret:
              secretName: ha-secrets
              items:
                - key: secrets.yaml
                  path: secrets.yaml
      - contains:
          path: spec.template.spec.containers[0].volumeMounts
          content:
            name: ha-secrets
            mountPath: /config/secrets.yaml
            subPath: secrets.yaml
            readOnly: true
      - contains:
          path: spec.template.spec.initContainers[?(@.name == "check-config")].volumeMounts
          content:
            name: ha-secrets
            mountPath: /config/secrets.yaml
            subPath: secrets.yaml
            readOnly: true
```

- [ ] **Step 2:** `cd ~/Repos/ha-helm && nix-shell --run 'helm unittest . -f tests/v060_test.yaml'` → 1 pass (default), 1 fail (no volume).
- [ ] **Step 3: Implement.** In `templates/deployment.yaml` add, after the `extraConfigMounts` volumeMounts block of **both** the main container and `check-config`:

```yaml
            {{- if .Values.secretsFile.enabled }}
            - name: ha-secrets
              mountPath: /config/secrets.yaml
              subPath: secrets.yaml
              readOnly: true
            {{- end }}
```

and in `volumes:` (before `extraConfigMounts` volumes):

```yaml
        {{- if .Values.secretsFile.enabled }}
        - name: ha-secrets
          secret:
            secretName: {{ .Values.secretsFile.secretName }}
            items:
              - key: {{ .Values.secretsFile.key }}
                path: secrets.yaml
        {{- end }}
```

`values.yaml` (after `checkConfig`):

```yaml
# /config/secrets.yaml from a Secret, so packages can use `!secret name` with the
# values kept out of git (seal the Secret). Mounted read-only into HA and check-config.
secretsFile:
  enabled: false
  secretName: ha-secrets
  key: secrets.yaml
```

README: a "### Secrets for `!secret`" section with that snippet. `Chart.yaml`: `version: 0.6.0`.
- [ ] **Step 4:** `nix-shell --run 'helm unittest . && helm lint .'` → all pass.
- [ ] **Step 5:** Commit `0.6.0: secrets.yaml from a Secret (secretsFile)`, tag `v0.6.0`, push `main` and the tag; `gh run list -L 1` green.

### Task 2: `ha-secrets` with the home Wi-Fi match; release on v0.6.0

**Files:** `cluster/home-assistant/ha-secrets.secret.yaml` (git-crypt) + sealed `cluster/home-assistant/ha-secrets.yaml`, `cluster/home-assistant/kustomization.yaml`, `cluster/applications/home-assistant-release.yaml`.

**Interfaces:**
- Consumes: Task 1 `secretsFile`.
- Produces: `!secret home_wifi_match` resolvable in packages (live and in `check-config`; `hactl lint --offline` already writes a dummy for every `!secret` name). Chris, 2026-10-03: home = the phone's SSID **contains** this string, case-insensitively (he has two access points); the string is still a secret because the repo is public.

- [ ] **Step 1: The match string.** Chris gave it (2026-10-03): a lower-case fragment contained in every home access point's SSID. It is kept in `~/.config/galaxy/home-wifi-match` (mode 600, no newline) and never written into git, the plan, the ledger or chat summaries.
- [ ] **Step 2: Write the Secret from that file** (nothing printed):

```bash
cd /home/chris/Repos/luma-homeops
( umask 077
  nix develop --command python3 -c '
import json, pathlib
m = (pathlib.Path.home() / ".config/galaxy/home-wifi-match").read_text().strip().lower()
assert m, "empty match"
print(json.dumps({"apiVersion": "v1", "kind": "Secret",
                  "metadata": {"name": "ha-secrets", "namespace": "home-assistant"},
                  "type": "Opaque",
                  "stringData": {"secrets.yaml": "home_wifi_match: " + json.dumps(m) + "\n"}}))
' > cluster/home-assistant/ha-secrets.secret.yaml )
nix develop --command ./sign.sh
grep -c '^kind: SealedSecret' cluster/home-assistant/ha-secrets.yaml   # expect 1
```

(JSON is valid YAML; yamlfmt may reformat it on commit.)
- [ ] **Step 3:** Add `- ha-secrets.yaml` to `resources:` in `cluster/home-assistant/kustomization.yaml`. In the release: `targetRevision: v0.6.0` and

```yaml
        # /config/secrets.yaml for `!secret` in packages (home Wi-Fi match), sealed in
        # cluster/home-assistant/ha-secrets.yaml.
        secretsFile:
          enabled: true
          secretName: ha-secrets
          key: secrets.yaml
```

- [ ] **Step 4:** `hactl lint --offline` clean (nothing uses `!secret` yet; this proves the release still renders). Commit `home-assistant: ha-secrets (home Wi-Fi match, sealed) mounted as secrets.yaml (ha-helm v0.6.0)`, push, `hactl deploy` (pod restarts: chart change). Verify: `kubectl -n home-assistant exec deploy/ha-home-assistant -c home-assistant -- sh -c 'test -s /config/secrets.yaml && echo present'` → `present` (never cat it).
- [ ] **Step 5: Leak check.** `git show HEAD:cluster/home-assistant/ha-secrets.secret.yaml | head -c 9 | od -c` shows GITCRYPT. And the match string appears in no SSID-related line of the cleartext tree: `git grep -l -i -F -f ~/.config/galaxy/home-wifi-match -- cluster docs tools .claude ':!*.secret.yaml' ':!cluster/home-assistant/ha-secrets.yaml'` — read the file list (names only); a hit is acceptable only where the fragment occurs as an unrelated ordinary word, never in a Wi-Fi/SSID context.

### Task 3: `hactl scene capture`

**Files:** Create `tools/hactl/src/hactl/scene.py`, `tools/hactl/tests/test_scene.py`; Modify `tools/hactl/src/hactl/__main__.py` (`MODULES` gains `"scene"`).

**Interfaces:**
- Produces: `scene.light_entry(state: dict) -> dict` (one light's scene values from its state object); `scene.scene_entry(scene_id: str, name: str, states: list[dict]) -> dict`; `scene.render(entry: dict) -> str` (YAML list item, `state` quoted); CLI `hactl scene capture --lights L1,L2… --id ID --name NAME [--activate scene.X] [--settle 4]` — with `--activate`, snapshots the lights (`scene.create`, `scene_id: hactl_capture_restore`), turns the source scene on, waits `--settle` seconds, reads the lights, restores the snapshot, and prints the YAML entry.

- [ ] **Step 1: Failing tests** `tools/hactl/tests/test_scene.py`:

```python
from hactl import scene


def st(eid, state, **attrs):
    return {"entity_id": eid, "state": state, "attributes": attrs}


def test_color_temp_light_keeps_kelvin_and_brightness():
    e = scene.light_entry(st("light.floor_lamp_a", "on", brightness=143, color_mode="color_temp",
                             color_temp_kelvin=2702, xy_color=[0.46, 0.41]))
    assert e == {"state": "on", "brightness": 143, "color_temp_kelvin": 2702}


def test_xy_light_keeps_xy():
    e = scene.light_entry(st("light.desk_underlight", "on", brightness=77, color_mode="xy", xy_color=[0.5612, 0.4042]))
    assert e == {"state": "on", "brightness": 77, "xy_color": [0.5612, 0.4042]}


def test_off_and_onoff_lights():
    assert scene.light_entry(st("light.ground_spot", "off")) == {"state": "off"}
    assert scene.light_entry(st("light.camp_lamp", "on", color_mode="onoff")) == {"state": "on"}


def test_unavailable_light_is_refused():
    import pytest
    from hactl.errors import HactlError
    with pytest.raises(HactlError, match="light.x is unavailable"):
        scene.scene_entry("s", "S", [st("light.x", "unavailable")])


def test_render_quotes_on_off():
    out = scene.render(scene.scene_entry("relax_living_room", "Relax (Living Room)",
                                         [st("light.a", "on", brightness=10, color_mode="brightness"), st("light.b", "off")]))
    assert out == ("- id: relax_living_room\n  name: Relax (Living Room)\n  entities:\n"
                   "    light.a:\n      state: 'on'\n      brightness: 10\n"
                   "    light.b:\n      state: 'off'\n")


class FakeClient:
    def __init__(self, states):
        self.states, self.calls = states, []

    def post(self, path, data=None, raw=False):
        self.calls.append((path, data))
        return []

    def get(self, path, raw=False):
        return self.states[path.rsplit("/", 1)[1]]


def test_capture_activates_reads_then_restores():
    fake = FakeClient({"light.a": st("light.a", "on", brightness=5, color_mode="brightness")})
    entry = scene.capture(fake, ["light.a"], "x", "X", activate="scene.hue_x", settle=0)
    assert entry["entities"]["light.a"] == {"state": "on", "brightness": 5}
    assert [c[0] for c in fake.calls] == ["/api/services/scene/create", "/api/services/scene/turn_on",
                                          "/api/services/scene/turn_on"]
    assert fake.calls[0][1] == {"scene_id": "hactl_capture_restore", "snapshot_entities": ["light.a"]}
    assert fake.calls[1][1] == {"entity_id": "scene.hue_x"}
    assert fake.calls[2][1] == {"entity_id": "scene.hactl_capture_restore"}
```

- [ ] **Step 2:** run `tests/test_scene.py` → fails (`No module named 'hactl.scene'`).
- [ ] **Step 3: Implement** `tools/hactl/src/hactl/scene.py`:

```python
"""Capture light states as a git `scene:` entry (e.g. from a Hue scene): hactl scene capture."""
import time

import yaml

from hactl import output
from hactl.errors import HactlError

RESTORE = "hactl_capture_restore"


def light_entry(state: dict) -> dict:
    if state["state"] != "on":
        return {"state": "off"}
    a = state["attributes"]
    out = {"state": "on"}
    if a.get("brightness") is not None:
        out["brightness"] = a["brightness"]
    mode = a.get("color_mode")
    if mode == "color_temp" and a.get("color_temp_kelvin"):
        out["color_temp_kelvin"] = a["color_temp_kelvin"]
    elif mode in ("xy", "hs", "rgb", "rgbw", "rgbww") and a.get("xy_color"):
        out["xy_color"] = [round(v, 4) for v in a["xy_color"]]
    return out


def scene_entry(scene_id: str, name: str, states: list) -> dict:
    bad = [s["entity_id"] for s in states if s["state"] in ("unavailable", "unknown")]
    if bad:
        raise HactlError(f"{', '.join(bad)} is unavailable: fix it before capturing")
    return {"id": scene_id, "name": name, "entities": {s["entity_id"]: light_entry(s) for s in states}}


class _OnOff(yaml.SafeDumper):
    pass


_OnOff.add_representer(str, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style="'" if v in ("on", "off") else None))


def render(entry: dict) -> str:
    return yaml.dump([entry], Dumper=_OnOff, sort_keys=False, default_flow_style=False, width=1000)


def capture(client, lights: list, scene_id: str, name: str, activate=None, settle=4.0) -> dict:
    if activate:
        client.post("/api/services/scene/create", {"scene_id": RESTORE, "snapshot_entities": lights})
        client.post("/api/services/scene/turn_on", {"entity_id": activate})
        time.sleep(settle)
    try:
        return scene_entry(scene_id, name, [client.get(f"/api/states/{e}") for e in lights])
    finally:
        if activate:
            client.post("/api/services/scene/turn_on", {"entity_id": f"scene.{RESTORE}"})


def _run(args) -> int:
    from hactl.client import Client

    lights = [x.strip() for x in args.lights.split(",") if x.strip()]
    entry = capture(Client(), lights, args.id, args.name, activate=args.activate, settle=args.settle)
    output.emit(args, entry, [render(entry).rstrip("\n")])
    return 0


def register(sub) -> None:
    p = sub.add_parser("scene", help="scene tools")
    s = p.add_subparsers(dest="scene_cmd", required=True)
    c = s.add_parser("capture", parents=[output.COMMON], help="print a git scene entry from the lights' current (or a scene's) state")
    c.add_argument("--lights", required=True, help="comma list of light entity ids")
    c.add_argument("--id", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--activate", help="scene to turn on first (lights are restored afterwards)")
    c.add_argument("--settle", type=float, default=4.0, help="seconds to wait after activating")
    c.set_defaults(func=_run)
```

Add `"scene"` to `MODULES` in `__main__.py`.
- [ ] **Step 4:** `tests/test_scene.py` → 6 pass; full suite green; `hactl scene capture --help` shows the options.
- [ ] **Step 5:** Commit `hactl: scene capture (light states, or a Hue scene's, as a git scene entry)`.

### Task 4: `hactl health --registry`

**Files:** Modify `tools/hactl/src/hactl/health.py`; Test `tools/hactl/tests/test_health.py`.

**Interfaces:**
- Produces: `health.registry_report(devices, entities, states, long_unavailable_ids) -> dict` with `no_area` (names of enabled, non-service devices that have an enabled entity and no area), `restored` (entity ids whose state has `restored: true`, i.e. no integration provides them), `long_unavailable` (passed through, sorted); `health.long_unavailable(client, entity_ids, days=30) -> list` (ids with no state other than `unavailable`/`unknown` in the last `days`, from `/api/history/period`); CLI `hactl health --registry` prints the three sections after the normal report and counts them as problems only for `restored`.

- [ ] **Step 1: Failing tests** (append to `tests/test_health.py`):

```python
def test_registry_report_lists_arealess_restored_and_long_unavailable():
    from hactl import health
    devices = [
        {"id": "d1", "name": "Plant Blinds", "area_id": None, "entry_type": None, "disabled_by": None},
        {"id": "d2", "name": "Lamp", "area_id": "bedroom", "entry_type": None, "disabled_by": None},
        {"id": "d3", "name": "Sun", "area_id": None, "entry_type": "service", "disabled_by": None},
        {"id": "d4", "name": "Old", "area_id": None, "entry_type": None, "disabled_by": "user"},
        {"id": "d5", "name": "Ghost", "area_id": None, "entry_type": None, "disabled_by": None},
    ]
    entities = [
        {"entity_id": "cover.plant_blinds", "device_id": "d1", "disabled_by": None},
        {"entity_id": "light.lamp", "device_id": "d2", "disabled_by": None},
        {"entity_id": "sensor.sun", "device_id": "d3", "disabled_by": None},
        {"entity_id": "sensor.ghost", "device_id": "d5", "disabled_by": "integration"},
        {"entity_id": "sensor.mail_old", "device_id": None, "disabled_by": None},
    ]
    states = {"sensor.mail_old": {"state": "unavailable", "attributes": {"restored": True}},
              "cover.plant_blinds": {"state": "open", "attributes": {}}}
    r = health.registry_report(devices, entities, states, ["sensor.fridge"])
    assert r == {"no_area": ["Plant Blinds"], "restored": ["sensor.mail_old"], "long_unavailable": ["sensor.fridge"]}


def test_long_unavailable_uses_history():
    from hactl import health

    class C:
        def get(self, path, raw=False):
            assert "filter_entity_id=sensor.a,sensor.b" in path and "no_attributes" in path
            return [[{"entity_id": "sensor.a", "state": "unavailable"}],
                    [{"entity_id": "sensor.b", "state": "unavailable"}, {"state": "on"}]]
    assert health.long_unavailable(C(), ["sensor.a", "sensor.b"]) == ["sensor.a"]
    assert health.long_unavailable(C(), []) == []
```

- [ ] **Step 2:** run them → `AttributeError: … registry_report`.
- [ ] **Step 3: Implement** in `health.py`:

```python
GONE = ("unavailable", "unknown")


def registry_report(devices, entities, states, long_unavailable_ids) -> dict:
    live_devices = {e.get("device_id") for e in entities if not e.get("disabled_by")}
    no_area = sorted(d.get("name_by_user") or d.get("name") or d["id"] for d in devices
                     if not d.get("area_id") and d.get("entry_type") != "service" and not d.get("disabled_by")
                     and d["id"] in live_devices)
    restored = sorted(e["entity_id"] for e in entities
                      if (states.get(e["entity_id"]) or {}).get("attributes", {}).get("restored"))
    return {"no_area": no_area, "restored": restored, "long_unavailable": sorted(long_unavailable_ids)}


def long_unavailable(client, entity_ids, days=30) -> list:
    if not entity_ids:
        return []
    from datetime import datetime, timedelta, timezone
    start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    hist = client.get(f"/api/history/period/{start}?filter_entity_id={','.join(entity_ids)}"
                      "&minimal_response&no_attributes")
    out = []
    for series in hist:
        if series and all(s.get("state") in GONE for s in series):
            out.append(series[0]["entity_id"])
    return out
```

In `_run`, when `args.registry`: fetch `config/device_registry/list` + `config/entity_registry/list` (one `client.ws`), the states map, compute `long_unavailable(client, [ids currently unavailable/unknown, not restored, not disabled])`, add `report["registry"] = registry_report(…)`, and append to the rendered lines three `section(...)` blocks: "devices without an area", "registry entries no integration provides (restored)", "unavailable for 30+ days"; `problem |= bool(registry["restored"])`. Register `p.add_argument("--registry", action="store_true", help="also list area-less devices and dead registry entries")`.
- [ ] **Step 4:** suite green; live: `hactl health --registry` prints the three sections (expect the 9 area-less devices of 2026-10-03 minus any since assigned; `restored` empty after plan 3's cleanup, except the old bedroom sync automation once Task 7 lands).
- [ ] **Step 5:** Commit `hactl: health --registry (area-less devices, restored-only and long-unavailable entities)`.

### Task 5: Lighting model — groups, adaptive lighting everywhere, scenes, room modes

**Files:** Rewrite `cluster/home-assistant/packages/lighting.yaml`; Create `cluster/home-assistant/packages/scenes.yaml`; Modify `kustomization.yaml` (`packages/scenes.yaml`), `packages/morning_routine.yaml`, `packages/presence.yaml` (targets only; Task 6 rewrites it).

**Interfaces:**
- Consumes: Task 3 `hactl scene capture`.
- Produces: `light.living_room_all`, `light.bedroom_all`, `light.kitchen_all`; scenes `scene.<scene>_<room>` for Living Room {bright, relax, movie, read}, Bedroom {bright, relax, read, night}, Kitchen {bright, night}; `input_select.<room>_light_mode` (`Auto` + that room's scene names, capitalised); `script.room_scene(room, scene)`, `script.room_auto(room, turn_on=false)`; automation `lighting_mode_reset_when_off`.

- [ ] **Step 1: Capture the Hue scenes** (reversible; the command restores the lights). For each row run `hactl scene capture --activate <hue scene> --lights <lights> --id <id> --name "<name>"` and keep the printed entries:

| id | name | --activate | --lights |
|---|---|---|---|
| bright_living_room | Bright (Living Room) | scene.living_room_bright | light.floor_lamp_a,light.floor_lamp_b,light.ground_spot,light.tv_accent |
| relax_living_room | Relax (Living Room) | scene.living_room_relax | same |
| movie_living_room | Movie (Living Room) | scene.living_room_dimmed | same |
| read_living_room | Read (Living Room) | scene.living_room_read | same |
| bright_bedroom | Bright (Bedroom) | scene.bedroom_bright | light.nightstand_lamp,light.dresser_lamp,light.floor_lamp |
| relax_bedroom | Relax (Bedroom) | scene.bedroom_relax | same |
| read_bedroom | Read (Bedroom) | scene.bedroom_read | same |
| night_bedroom | Night (Bedroom) | scene.bedroom_nightlight | same |

- [ ] **Step 2: Write `packages/scenes.yaml`**: header comment (curated scenes, captured 2026-10-03 from the Hue scenes named in each `# from:` comment, Chris trims/renames in the plan-5 dashboard review; never edit in the HA UI), `scene:` with the eight captured entries, each living-room entry extended with the non-Hue lights, plus the two kitchen scenes:

```yaml
    # non-Hue living room lights, per scene:
    #   bright: sidetable 255; desk 255 xy [0.4596, 0.4105]; camp on
    #   relax:  sidetable 128; desk  77 xy [0.5612, 0.4042]; camp on
    #   movie:  sidetable off; desk  26 xy [0.5612, 0.4042]; camp off
    #   read:   sidetable 255; desk off;                     camp on
  - id: bright_kitchen
    name: Bright (Kitchen)
    entities:
      light.kitchen_underlight:
        state: 'on'
        brightness: 255
        xy_color: [0.4596, 0.4105]
  - id: night_kitchen
    name: Night (Kitchen)
    entities:
      light.kitchen_underlight:
        state: 'on'
        brightness: 13
        xy_color: [0.5612, 0.4042]
```

(e.g. the Relax entry's added lines: `light.sidetable_lamp: {state: 'on', brightness: 128}`, `light.desk_underlight: {state: 'on', brightness: 77, xy_color: [0.5612, 0.4042]}`, `light.camp_lamp: {state: 'on'}` — written in block style like the captured lines.)
- [ ] **Step 3: Rewrite `packages/lighting.yaml`:**

```yaml
---
# Lighting model (spec §7.3). HA owns grouping: automations and the dashboard
# target only the three room groups below, never Hue rooms/zones (those stay
# for the Hue app). Adaptive lighting runs every dimmable light. A room is in
# Auto (adaptive) or holds one of its scenes (scenes.yaml) until Auto is chosen
# again or the room goes fully off.
#
# GitOps source of truth (see bedroom.yaml header for the mount mechanism).

light:
  - platform: group
    name: Living Room All
    unique_id: living_room_all
    entities:
      - light.floor_lamp_a
      - light.floor_lamp_b
      - light.ground_spot
      - light.tv_accent
      - light.sidetable_lamp
      - light.desk_underlight
      - light.camp_lamp
  - platform: group
    name: Bedroom All
    unique_id: bedroom_all
    entities:
      - light.nightstand_lamp
      - light.dresser_lamp
      - light.floor_lamp
  - platform: group
    name: Kitchen All
    unique_id: kitchen_all
    entities:
      - light.kitchen_underlight

# Creates switch.circadian_adaptive_lighting_circadian (main) and
# switch.adaptive_lighting_circadian_adaptive_lighting_sleep_mode_circadian.
# Camp Lamp is on/off only, so it is not adapted.
adaptive_lighting:
  - name: circadian
    lights:
      - light.desk_underlight
      - light.dresser_lamp
      - light.floor_lamp
      - light.floor_lamp_a
      - light.floor_lamp_b
      - light.ground_spot
      - light.kitchen_underlight
      - light.nightstand_lamp
      - light.sidetable_lamp
      - light.tv_accent
    min_color_temp: 2200
    max_color_temp: 5500
    transition: 45
    take_over_control: true       # respect manual changes (e.g. the morning warm dim)
    detect_non_ha_changes: false

input_select:
  living_room_light_mode:
    name: Living Room Light Mode
    icon: mdi:sofa
    options: [Auto, Bright, Relax, Movie, Read]
  bedroom_light_mode:
    name: Bedroom Light Mode
    icon: mdi:bed
    options: [Auto, Bright, Relax, Read, Night]
  kitchen_light_mode:
    name: Kitchen Light Mode
    icon: mdi:countertop
    options: [Auto, Bright, Night]

script:
  room_scene:
    alias: Room Scene
    description: Put a room in one of its scenes and hold it there (adaptive lighting paused for its lights) until Auto.
    mode: queued
    fields:
      room:
        description: living_room, bedroom or kitchen
        required: true
        selector:
          select:
            options: [living_room, bedroom, kitchen]
      scene:
        description: One of the room's mode options, e.g. Relax
        required: true
        selector:
          text: {}
    variables: &room_lights
      al_lights: >-
        {{ {'living_room': ['light.floor_lamp_a', 'light.floor_lamp_b', 'light.ground_spot', 'light.tv_accent',
                            'light.sidetable_lamp', 'light.desk_underlight'],
            'bedroom': ['light.nightstand_lamp', 'light.dresser_lamp', 'light.floor_lamp'],
            'kitchen': ['light.kitchen_underlight']}[room] }}
    sequence:
      - action: scene.turn_on
        target:
          entity_id: "scene.{{ scene | lower }}_{{ room }}"
      - action: adaptive_lighting.set_manual_control
        data:
          entity_id: switch.circadian_adaptive_lighting_circadian
          lights: "{{ al_lights }}"
          manual_control: true
      - action: input_select.select_option
        target:
          entity_id: "input_select.{{ room }}_light_mode"
        data:
          option: "{{ scene }}"

  room_auto:
    alias: Room Auto
    description: Hand a room back to adaptive lighting; with turn_on, also switch its lights on.
    mode: queued
    fields:
      room:
        description: living_room, bedroom or kitchen
        required: true
        selector:
          select:
            options: [living_room, bedroom, kitchen]
      turn_on:
        description: Also turn the room's adaptive lights on
        default: false
        selector:
          boolean: {}
    variables: *room_lights
    sequence:
      - action: adaptive_lighting.set_manual_control
        data:
          entity_id: switch.circadian_adaptive_lighting_circadian
          lights: "{{ al_lights }}"
          manual_control: false
      - action: adaptive_lighting.apply
        data:
          entity_id: switch.circadian_adaptive_lighting_circadian
          lights: "{{ al_lights }}"
          turn_on_lights: "{{ turn_on | default(false) | bool }}"
      - action: input_select.select_option
        target:
          entity_id: "input_select.{{ room }}_light_mode"
        data:
          option: Auto

automation:
  - id: lighting_mode_reset_when_off
    alias: Lighting - Mode Back to Auto When Room Off
    description: A room whose lights are all off forgets its scene (adaptive lighting already cleared manual control).
    triggers:
      - trigger: state
        entity_id:
          - light.living_room_all
          - light.bedroom_all
          - light.kitchen_all
        from: "on"
        to: "off"
    actions:
      - action: input_select.select_option
        target:
          entity_id: "input_select.{{ trigger.entity_id.split('.')[1] | replace('_all', '') }}_light_mode"
        data:
          option: Auto
    mode: queued
```

(YAML anchors work inside one package file. If `check_config` rejects the `&room_lights` anchor under `variables:`, repeat the mapping verbatim in `room_auto` instead.)
- [ ] **Step 4: Retarget existing users** of Hue groups: `morning_routine.yaml` Phase 1 `target: {area_id: bedroom}` → `target: {entity_id: light.bedroom_all}`; Phase 2's `light.turn_on light.living_room` → `action: script.room_auto` with `data: {room: living_room, turn_on: true}`, and add after re-enabling circadian `action: script.room_auto` with `data: {room: bedroom, turn_on: true}`. `presence.yaml` away target → `[light.living_room_all, light.bedroom_all, light.kitchen_all]`; arrival → `script.room_auto` `{room: living_room, turn_on: true}` (Task 6 replaces the triggers).
- [ ] **Step 5:** Render the room-lights mapping: `hactl template '{% set room = "bedroom" %}{{ {"living_room": [1], "bedroom": [2,3]}[room] }}'` → `[2, 3]`. `hactl lint` clean. Commit `home-assistant: HA-owned light groups, adaptive lighting on every dimmable light, git scenes + room modes`, push, `hactl deploy --shot` (adaptive_lighting light list → restart via the hook is fine).
- [ ] **Step 6: Exercise (reversible):** `hactl call script.room_scene --data '{"room": "living_room", "scene": "Relax"}'` → `hactl state input_select.living_room_light_mode` = `Relax`; `hactl state light.floor_lamp_a` brightness/kelvin ≈ the captured Relax values; wait 2 min, unchanged (adaptive lighting is not taking it back). `hactl call script.room_auto --data '{"room": "living_room"}'` → mode `Auto`; within one AL interval the lamps move to the adaptive values (`hactl history light.floor_lamp_a --since 5m`).
- [ ] **Step 7: Reset-on-off (Review Focus 2):** `hactl call script.room_scene --data '{"room": "bedroom", "scene": "Read"}'` → `hactl call light.turn_off --entity light.bedroom_all` → mode `Auto` (trace of `lighting_mode_reset_when_off`) → `hactl call light.turn_on --entity light.bedroom_all` → lamps come up at adaptive values, not Read. Restore whatever was on before (note it at the start of the step).

### Task 6: Presence and guest mode

**Files:** Rewrite `cluster/home-assistant/packages/presence.yaml`.

**Interfaces:**
- Consumes: Task 2 `!secret home_wifi_match`; Task 5 groups and `script.room_auto`.
- Produces: `sensor.home_wifi_match`, `binary_sensor.chris_home`, `input_boolean.guest_mode`, `binary_sensor.house_occupied`; automations `presence_away_lights_off`, `presence_arrival_evening_lights`.

- [ ] **Step 1: Check the inputs live** (`hactl template`, never printing the SSID):
`{{ is_state('person.chris_m', 'home') }} / {{ states('sensor.pixel_9_pro_xl_wi_fi_connection') not in ['unknown', 'unavailable', '<not connected>'] }}` → `True / True`.
- [ ] **Step 2: Write `packages/presence.yaml`:**

```yaml
---
# Presence (spec §7.2). chris_home = GPS zone OR the phone on the home Wi-Fi
# (fast and indoor-reliable), held 5 min on the way out. house_occupied adds
# guest mode; every away behaviour keys off house_occupied, so guests keep
# their lights. The SSID comes from the sealed ha-secrets (never in git).
#
# GitOps source of truth (see bedroom.yaml header for the mount mechanism).

template:
  - sensor:
      # Lower-case fragment every home access point's SSID contains (sealed in ha-secrets).
      - name: Home Wifi Match
        unique_id: home_wifi_match
        icon: mdi:wifi-lock
        state: !secret home_wifi_match
  - binary_sensor:
      - name: Chris Home
        unique_id: chris_home
        device_class: presence
        delay_off: "00:05:00"
        state: >-
          {% set match = states('sensor.home_wifi_match') | lower %}
          {{ is_state('person.chris_m', 'home')
             or (match not in ['unknown', 'unavailable', '']
                 and match in states('sensor.pixel_9_pro_xl_wi_fi_connection') | lower) }}
      - name: House Occupied
        unique_id: house_occupied
        device_class: occupancy
        state: >-
          {{ is_state('binary_sensor.chris_home', 'on') or is_state('input_boolean.guest_mode', 'on') }}

input_boolean:
  guest_mode:
    name: Guest Mode
    icon: mdi:account-multiple

automation:
  - id: presence_away_lights_off
    alias: Presence - Lights Off When Away
    description: Every room off once nobody is home (chris_home already waits 5 min; guest mode keeps the house occupied).
    triggers:
      - trigger: state
        entity_id: binary_sensor.house_occupied
        from: "on"
        to: "off"
    actions:
      - action: light.turn_off
        target:
          entity_id:
            - light.living_room_all
            - light.bedroom_all
            - light.kitchen_all
    mode: single

  - id: presence_arrival_evening_lights
    alias: Presence - Evening Arrival Lights
    description: Chris arriving after sunset turns the living room on in Auto (adaptive lighting sets the warmth).
    triggers:
      - trigger: state
        entity_id: binary_sensor.chris_home
        from: "off"
        to: "on"
    conditions:
      - condition: state
        entity_id: sun.sun
        state: below_horizon
    actions:
      - action: script.room_auto
        data:
          room: living_room
          turn_on: true
    mode: single
```

- [ ] **Step 3:** `hactl lint` clean; commit `home-assistant: presence = zone or home Wi-Fi, guest mode, house_occupied`; push; `hactl deploy`.
- [ ] **Step 4:** `hactl state binary_sensor.chris_home` and `hactl state binary_sensor.house_occupied` → both `on`; `hactl template "{{ states('sensor.home_wifi_match') | lower in states('sensor.pixel_9_pro_xl_wi_fi_connection') | lower }}"` → `True` (never print either value).
- [ ] **Step 5: Guest mode (Review Focus 5):** `hactl template` with the house_occupied expression where chris_home is forced off — `{{ false or is_state('input_boolean.guest_mode','on') }}` — after `hactl call input_boolean.turn_on --entity input_boolean.guest_mode` → `True`; turn guest mode off again. (The away automation itself is exercised only by real departures; its trace is checked in Task 10's health pass.)

### Task 7: Bedroom wall switch is the master (one-way)

**Files:** Rewrite `cluster/home-assistant/packages/bedroom.yaml`; Modify `cluster/home-assistant/state/entities.yaml` (`remove:` the old sync automation's registry entry).

**Interfaces:**
- Consumes: Task 5 `script.room_auto`, `light.bedroom_all`; `input_boolean.wake_up_pending` (morning routine).
- Produces: automation `bedroom_switch_master`.

- [ ] **Step 1: Write `packages/bedroom.yaml`:**

```yaml
---
# Bedroom wall switch (spec §7.4). One way only: the switch drives the lamps;
# the lamps never drive the switch, so the old switch<->lamp flapping cannot
# happen. Trade-off (accepted): lamps turned on from the app while the switch
# is off need on-then-off at the paddle to turn off.
#
# GitOps source of truth. Rendered into /config/packages/bedroom.yaml in the HA
# pod via the `ha-packages` ConfigMap (kustomization.yaml) + the chart's
# extraConfigMounts. HA loads it via `homeassistant.packages`. Edit HERE, not in
# the HA UI (the UI editor is read-only for package-defined automations).
automation:
  - id: bedroom_switch_master
    alias: Bedroom - Wall Switch Drives the Lamps
    description: Switch on = bedroom lamps on in Auto; switch off = lamps off. Never the other way round.
    triggers:
      - trigger: state
        entity_id: switch.bedroom_bedroom_light_switch
        from: "off"
        to: "on"
        id: "on"
      - trigger: state
        entity_id: switch.bedroom_bedroom_light_switch
        from: "on"
        to: "off"
        id: "off"
    actions:
      - choose:
          - conditions:
              - condition: trigger
                id: "on"
              # While armed, "start your day" (morning_routine.yaml) owns this flip.
              - condition: state
                entity_id: input_boolean.wake_up_pending
                state: "off"
            sequence:
              - action: script.room_auto
                data:
                  room: bedroom
                  turn_on: true
          - conditions:
              - condition: trigger
                id: "off"
            sequence:
              - action: light.turn_off
                target:
                  entity_id: light.bedroom_all
    mode: queued
```

- [ ] **Step 2:** Append to `state/entities.yaml` `remove:`

```yaml
  - about: automation.sync_bedroom_switch_and_lights (the old two-way sync, replaced by bedroom_switch_master)
    platform: automation
    unique_id: '1741747401508'
```

- [ ] **Step 3:** `hactl lint` clean; commit `home-assistant: bedroom wall switch is the master (one-way); old sync removed`; push; `hactl deploy`; `hactl plan` in sync (the old entry removed).
- [ ] **Step 4: Exercise both paths (Review Focus 1)** — note the switch and lamp states first and restore them after:
  - `wake_up_pending` off: `hactl call switch.turn_off --entity switch.bedroom_bedroom_light_switch` → lamps off; `switch.turn_on` → lamps on, `input_select.bedroom_light_mode` = `Auto`; `hactl trace automation.bedroom_wall_switch_drives_the_lamps --last 2` shows the `on` branch ran `script.room_auto`. Then `hactl call light.turn_off --entity light.dresser_lamp`: the switch stays `on` (`hactl history switch.bedroom_bedroom_light_switch --since 5m` has no change) — no loop.
  - `wake_up_pending` on: `hactl call input_boolean.turn_on --entity input_boolean.wake_up_pending`, switch off → on: the bedroom trace shows the `on` branch skipped (condition false) and `automation.morning_start_your_day` ran; `wake_up_pending` is off afterwards.

### Task 8: Kitchen underlight

**Files:** Create `cluster/home-assistant/packages/kitchen.yaml`; Modify `kustomization.yaml` (`packages/kitchen.yaml`).

- [ ] **Step 1: Write `packages/kitchen.yaml`:**

```yaml
---
# Kitchen underlight (spec §7.5): on after sunset while someone is home, off
# at sunrise or when the house empties. A bare turn_on: adaptive lighting sets
# level and warmth, and in sleep mode that makes it the night light. A manual
# off sticks until the next sunset or arrival.
#
# GitOps source of truth (see bedroom.yaml header for the mount mechanism).
automation:
  - id: kitchen_underlight_on
    alias: Kitchen - Underlight On After Sunset
    description: Below the horizon with the house occupied, the underlight is on.
    triggers:
      - trigger: sun
        event: sunset
      - trigger: state
        entity_id: binary_sensor.house_occupied
        from: "off"
        to: "on"
    conditions:
      - condition: state
        entity_id: sun.sun
        state: below_horizon
      - condition: state
        entity_id: binary_sensor.house_occupied
        state: "on"
    actions:
      - action: light.turn_on
        target:
          entity_id: light.kitchen_all
    mode: single

  - id: kitchen_underlight_off
    alias: Kitchen - Underlight Off at Sunrise or When Away
    description: Off when the sun is up or nobody is home.
    triggers:
      - trigger: sun
        event: sunrise
      - trigger: state
        entity_id: binary_sensor.house_occupied
        from: "on"
        to: "off"
    actions:
      - action: light.turn_off
        target:
          entity_id: light.kitchen_all
    mode: single
```

- [ ] **Step 2:** `hactl lint` clean; commit `home-assistant: kitchen underlight follows sunset and occupancy`; push; `hactl deploy`.
- [ ] **Step 3: Exercise:** with the sun below the horizon, `hactl call automation.trigger --entity automation.kitchen_underlight_on_after_sunset --data '{"skip_condition": false}'` → `light.kitchen_underlight` on; `hactl state` shows adaptive brightness/colour (sleep mode values after 22:30). Note and restore the prior state.

### Task 9: Climate

**Files:** Create `cluster/home-assistant/packages/climate.yaml`; Modify `kustomization.yaml` (`packages/climate.yaml`).

**Interfaces:**
- Consumes: `binary_sensor.chris_home` (Task 6), `binary_sensor.octoprint_printing`, `sensor.airnow_air_quality_index`.
- Produces: `input_boolean.climate_auto`; `input_number.{free_cooling_delta,free_cooling_floor,aqi_limit,ac_cool_threshold}`; `binary_sensor.{window_insert_installed,free_cooling_wanted,ac_cool_wanted}`; `sensor.free_cooling_status` (`paused`, `insert out`, `running`, `starting`, `smoke`, `warmer outside`, `cool enough`, `no data`); automations `climate_vent_fan_free_cooling`, `climate_floor_fan_print_pause`, `climate_ac_follows_cool_wanted`.

- [ ] **Step 1: What-if cases first.** Write `$SCRATCH/fc.j2` — the free-cooling decision with literal inputs — and render each case with `hactl template -f` (edit the four `set` lines per row):

```jinja
{% set insert, indoor, outdoor, aqi = true, 80.6, 62, 19 %}
{% set delta, floor, limit = 2, 70, 100 %}
{{ insert and indoor is not none and outdoor is not none and outdoor <= indoor - delta
   and indoor >= floor and (aqi is none or aqi <= limit) }}
```

| insert | indoor | outdoor | aqi | expect |
|---|---|---|---|---|
| true | 80.6 | 62 | 19 | True |
| true | 80.6 | 79 | 19 | False (warmer outside) |
| true | 69 | 60 | 19 | False (cool enough) |
| true | 80.6 | 62 | 151 | False (smoke) |
| true | 80.6 | 62 | none | True (AQI unknown allowed) |
| false | 80.6 | 62 | 19 | False (insert out) |
| true | none | 62 | 19 | False (no data) |

and the AC decision (`$SCRATCH/ac.j2`):

```jinja
{% set insert, home, indoor, outdoor, was_on = true, true, 79, 82, false %}
{% set threshold, delta = 78, 2 %}
{{ insert and home and indoor is not none and indoor >= (threshold - 2 if was_on else threshold)
   and (outdoor is none or outdoor > indoor - delta) }}
```

| insert | home | indoor | outdoor | was_on | expect |
|---|---|---|---|---|---|
| true | true | 79 | 82 | false | True |
| true | true | 77 | 82 | false | False (below threshold) |
| true | true | 77 | 82 | true | True (hysteresis holds to 76) |
| true | true | 75.5 | 82 | true | False |
| true | true | 79 | 70 | false | False (free cooling viable) |
| true | false | 79 | 82 | false | False (away) |
| false | true | 79 | 82 | false | False (insert out) |

- [ ] **Step 2: Write `packages/climate.yaml`** (the templates are the what-if expressions with live inputs):

```yaml
---
# Climate (spec §7.6), all behind input_boolean.climate_auto (Climate Auto on
# the dashboard). The slider insert carries the vent fan and AC #1, so the
# slider being open means the insert is in. Tunables are input_numbers whose
# `initial:` makes git the source of truth (edit them here, not in the UI).
#
# GitOps source of truth (see bedroom.yaml header for the mount mechanism).

input_boolean:
  climate_auto:
    name: Climate Auto
    icon: mdi:thermostat-auto

input_number:
  free_cooling_delta:
    name: Free Cooling Delta
    min: 0
    max: 10
    step: 0.5
    unit_of_measurement: °F
    initial: 2
    mode: box
  free_cooling_floor:
    name: Free Cooling Floor
    min: 60
    max: 80
    step: 0.5
    unit_of_measurement: °F
    initial: 70
    mode: box
  aqi_limit:
    name: AQI Limit
    min: 0
    max: 300
    step: 5
    initial: 100
    mode: box
  ac_cool_threshold:
    name: AC Cool Threshold
    min: 70
    max: 90
    step: 0.5
    unit_of_measurement: °F
    initial: 78
    mode: box

template:
  - binary_sensor:
      - name: Window Insert Installed
        unique_id: window_insert_installed
        icon: mdi:window-shutter-open
        delay_on: "00:30:00"
        delay_off: "00:05:00"
        state: "{{ is_state('binary_sensor.slider_door_contact', 'on') }}"

      - name: Free Cooling Wanted
        unique_id: free_cooling_wanted
        icon: mdi:weather-windy
        delay_on: "00:10:00"
        delay_off: "00:10:00"
        state: >-
          {% set indoor = states('sensor.living_room_temperature') | float(none) %}
          {% set outdoor = state_attr('weather.forecast_home', 'temperature') | float(none) %}
          {% set aqi = states('sensor.airnow_air_quality_index') | float(none) %}
          {{ is_state('binary_sensor.window_insert_installed', 'on')
             and indoor is not none and outdoor is not none
             and outdoor <= indoor - states('input_number.free_cooling_delta') | float(2)
             and indoor >= states('input_number.free_cooling_floor') | float(70)
             and (aqi is none or aqi <= states('input_number.aqi_limit') | float(100)) }}

      - name: AC Cool Wanted
        unique_id: ac_cool_wanted
        icon: mdi:snowflake
        state: >-
          {% set indoor = states('sensor.living_room_temperature') | float(none) %}
          {% set outdoor = state_attr('weather.forecast_home', 'temperature') | float(none) %}
          {% set threshold = states('input_number.ac_cool_threshold') | float(78) %}
          {% set delta = states('input_number.free_cooling_delta') | float(2) %}
          {{ is_state('binary_sensor.window_insert_installed', 'on')
             and is_state('binary_sensor.chris_home', 'on')
             and indoor is not none
             and indoor >= (threshold - 2 if this.state == 'on' else threshold)
             and (outdoor is none or outdoor > indoor - delta) }}

  - sensor:
      - name: Free Cooling Status
        unique_id: free_cooling_status
        icon: mdi:fan-auto
        state: >-
          {% set indoor = states('sensor.living_room_temperature') | float(none) %}
          {% set outdoor = state_attr('weather.forecast_home', 'temperature') | float(none) %}
          {% set aqi = states('sensor.airnow_air_quality_index') | float(none) %}
          {% if is_state('input_boolean.climate_auto', 'off') %}paused
          {% elif is_state('binary_sensor.window_insert_installed', 'off') %}insert out
          {% elif is_state('binary_sensor.free_cooling_wanted', 'on') %}running
          {% elif indoor is none or outdoor is none %}no data
          {% elif aqi is not none and aqi > states('input_number.aqi_limit') | float(100) %}smoke
          {% elif outdoor > indoor - states('input_number.free_cooling_delta') | float(2) %}warmer outside
          {% elif indoor < states('input_number.free_cooling_floor') | float(70) %}cool enough
          {% else %}starting{% endif %}

automation:
  - id: climate_vent_fan_free_cooling
    alias: Climate - Vent Fan Follows Free Cooling
    description: While the insert is in, the vent fan runs exactly when free cooling is wanted.
    triggers:
      - trigger: state
        entity_id: binary_sensor.free_cooling_wanted
        not_from: [unavailable, unknown]
        not_to: [unavailable, unknown]
    conditions:
      - condition: state
        entity_id: input_boolean.climate_auto
        state: "on"
      - condition: state
        entity_id: binary_sensor.window_insert_installed
        state: "on"
      - condition: template
        value_template: "{{ states('switch.vent_fan_switch') not in ['unavailable', 'unknown'] }}"
    actions:
      - action: "switch.turn_{{ trigger.to_state.state }}"
        target:
          entity_id: switch.vent_fan_switch
    mode: single

  - id: climate_floor_fan_print_pause
    alias: Climate - Floor Fan Off While Printing
    description: The oscillating fan next to the printer warps prints. Off while printing, on after; a manual change sticks.
    triggers:
      - trigger: state
        entity_id: binary_sensor.octoprint_printing
        from: "off"
        to: "on"
        id: printing
      - trigger: state
        entity_id: binary_sensor.octoprint_printing
        from: "on"
        to: "off"
        id: done
    conditions:
      - condition: state
        entity_id: input_boolean.climate_auto
        state: "on"
      - condition: template
        value_template: "{{ states('switch.floor_fan') not in ['unavailable', 'unknown'] }}"
    actions:
      - action: "{{ 'switch.turn_off' if trigger.id == 'printing' else 'switch.turn_on' }}"
        target:
          entity_id: switch.floor_fan
    mode: queued

  - id: climate_ac_follows_cool_wanted
    alias: Climate - AC Follows Cooling Need
    description: >-
      AC #1 (in the insert, IR through SwitchBot: one-way, so HA's state is
      assumed) cools while the insert is in, Chris is home, the room is hot
      and outside air can't do it.
    triggers:
      - trigger: state
        entity_id: binary_sensor.ac_cool_wanted
        not_from: [unavailable, unknown]
        not_to: [unavailable, unknown]
    conditions:
      - condition: state
        entity_id: input_boolean.climate_auto
        state: "on"
      - condition: state
        entity_id: binary_sensor.window_insert_installed
        state: "on"
      - condition: template
        value_template: "{{ states('climate.air_conditioner') not in ['unavailable', 'unknown'] }}"
    actions:
      - action: climate.set_hvac_mode
        target:
          entity_id: climate.air_conditioner
        data:
          hvac_mode: "{{ 'cool' if trigger.to_state.state == 'on' else 'off' }}"
    mode: single
```

- [ ] **Step 3:** `hactl lint` clean (unit `°F` unquoted is fine for HA; lint's quoting rule covers `on`/`off`/times only). Commit `home-assistant: climate — insert detection, free cooling, print-aware floor fan, AC #1`; push; `hactl deploy`.
- [ ] **Step 4: Turn Climate Auto on once** (a new input_boolean starts off and then restores; no `initial:` so a pause survives restarts): `hactl call input_boolean.turn_on --entity input_boolean.climate_auto`.
- [ ] **Step 5:** `hactl state` each of `binary_sensor.window_insert_installed`, `binary_sensor.free_cooling_wanted`, `binary_sensor.ac_cool_wanted`, `sensor.free_cooling_status` — consistent with the live inputs per the Step 1 tables (note `delay_on` 30/10 min after deploy).
- [ ] **Step 6: Guards (Review Focus 3).** The decisions are proven by the Step 1 tables; the guards are proven by the conditions: `hactl call automation.trigger --entity automation.climate_ac_follows_cooling_need --data '{"skip_condition": false}'` with `climate_auto` turned off first → the trace shows `condition/0` false and no action (turn `climate_auto` back on). The first real `free_cooling_wanted`/`ac_cool_wanted` transitions after deploy are read with `hactl trace` in Task 10's health pass; record in the ledger which were observed.

### Task 10: §7.1 conventions — lint rule, notifications, zero errored runs

**Files:** Modify `tools/hactl/src/hactl/lint.py`, `tools/hactl/tests/test_lint.py`; Move the permit-join automation from `packages/zigbee_hygiene.yaml` into `packages/home_alerts.yaml` (delete `zigbee_hygiene.yaml`, drop it from `kustomization.yaml`).

**Interfaces:**
- Produces: `lint.check_automations(ha_dir) -> list[Finding]` (rule `automation-shape`): every package automation has `id`, `alias`, `description`, `mode`; and a `notify.*` action outside `home_alerts.yaml` is a finding (rule `notify-outside-alerts`). Wired into `lint.offline`.

- [ ] **Step 1: Failing tests** (append to `test_lint.py`):

```python
def test_automation_shape_and_notify_rules(tmp_path):
    ha = make_ha(tmp_path, {
        "packages/good.yaml": "automation:\n  - id: a\n    alias: A\n    description: d\n    mode: single\n    actions: []\n",
        "packages/bad.yaml": "automation:\n  - id: b\n    alias: B\n    actions:\n      - action: notify.mobile_app_x\n",
        "packages/home_alerts.yaml": "automation:\n  - id: c\n    alias: C\n    description: d\n    mode: single\n"
                                     "    actions:\n      - action: notify.mobile_app_x\n",
    })
    msgs = sorted((f.rule, f.message) for f in lint.check_automations(ha))
    assert msgs == [("automation-shape", "B: missing description, mode"),
                    ("notify-outside-alerts", "B: notify.mobile_app_x (only home_alerts.yaml notifies the phone)")]
```

- [ ] **Step 2:** run → `AttributeError: check_automations`.
- [ ] **Step 3: Implement:**

```python
def _actions(node):
    """Every `action:` value anywhere under an automation's actions (choose/if/sequence nest)."""
    if isinstance(node, dict):
        if isinstance(node.get("action"), str):
            yield node["action"]
        for v in node.values():
            yield from _actions(v)
    elif isinstance(node, list):
        for v in node:
            yield from _actions(v)


def check_automations(ha_dir: Path) -> list:
    out = []
    for f in sorted((ha_dir / "packages").glob("*.yaml")):
        body = (yamlload.load(f) or {}).get("automation")
        for a in body if isinstance(body, list) else []:
            if not isinstance(a, dict):
                continue
            name = a.get("alias") or a.get("id") or "?"
            if missing := [k for k in ("id", "alias", "description", "mode") if not a.get(k)]:
                out.append(Finding("automation-shape", paths.rel(f), None, f"{name}: missing {', '.join(missing)}"))
            if f.name != "home_alerts.yaml":
                out += [Finding("notify-outside-alerts", paths.rel(f), None,
                                f"{name}: {act} (only home_alerts.yaml notifies the phone)")
                        for act in _actions(a.get("actions")) if act.startswith("notify.")]
    return out
```

Add `+ check_automations(ha_dir)` to `offline()`.
- [ ] **Step 4:** suite green. `hactl lint --offline` now flags the zigbee notification (and any automation without `description`/`mode`) — fix them in the packages: move `zigbee_permit_join_auto_off` (same `id`) into `home_alerts.yaml` under a `# Zigbee pairing window` comment, delete `zigbee_hygiene.yaml` and its kustomization line; add a `description:` to every automation lint names. Re-run → clean.
- [ ] **Step 5:** Commit `hactl lint: automation shape + only home_alerts notifies; zigbee alert moved`; push; `hactl deploy`. `hactl health` → `failing automations: none`. If any errored runs appear for the new automations, `hactl trace` them and fix (each fix its own commit).

### Task 11: Every device in an area

**Files:** `cluster/home-assistant/state/devices.yaml` (and `areas.yaml` if a new area is created).

- [ ] **Step 1:** `hactl health --registry` → the "devices without an area" list (2026-10-03: Pixel 9 Pro XL, hello@chrismiller.xyz (IMAP), OctoPrint, plex.chrismiller.xyz, the Hue Bridge, Plant Blinds, Window Left, Window Right, Zigbee2MQTT Bridge).
- [ ] **Step 2:** `AskUserQuestion` (multiSelect off, one question per kind if more than 4 options are needed): physical devices — proposed areas: Plant Blinds, Window Left, Window Right → Living Room; OctoPrint → Living Room (next to the floor fan); Hue Bridge → Living Room; Zigbee2MQTT Bridge (runs on the router) → a new `Network` area; virtual ones (phone, IMAP, Plex) → `Network` too, or exempt them. Options: "As proposed (Recommended)", "Exempt virtual devices", "I'll list areas".
- [ ] **Step 3:** For each device add a `devices.yaml` entry (`match` by one identifier, read from `config/device_registry/list`; `about:` with name and model), `area:` per Chris's answer; a new area goes into `areas.yaml` `areas:` (`id: network`, `name: Network`, `icon: mdi:lan`). Exempted devices (if Chris chooses that) are listed in docs/ha.md as intentionally area-less, and remain in the report.
- [ ] **Step 4:** `hactl plan` lists only the area assignments (and the new area); `hactl apply`; `hactl health --registry` → `devices without an area: none` (or only the exempted ones). Commit `home-assistant: every device in an area`, push, `hactl deploy`.

### Task 12: Docs, spec as-built, skill, memory

- [ ] `docs/ha.md`: a "Behaviours" section — presence (chris_home/guest_mode/house_occupied), lighting model (groups, room modes, `script.room_scene`/`room_auto`, scenes in git, `hactl scene capture` to refresh one), bedroom switch rule, kitchen, climate (Climate Auto, tunables in git, status meanings), and `hactl health --registry`. Note the SSID lives only in `ha-secrets`.
- [ ] Spec §7 as-built note (same style as §5's): sensor name `wi_fi_connection`; scene ids `scene.<scene>_<room>`; Climate Auto has no `initial` (pause survives restarts); `free_cooling_status` gained `starting` and `no data`; insert removal stands both fan and AC down (no command); anything else ruled in the ledger.
- [ ] `ha-config` skill: "behaviours live in feature packages; target `light.<room>_all`, never Hue groups; change a room's look through `script.room_scene`/`scenes.yaml`; only `home_alerts` notifies (lint enforces)".
- [ ] Memory: plan 4 shipped; plan 5 next (dashboard, spec §8) — the room-mode chips, guest chip, Climate Auto chip and free-cooling status are its inputs; the commute tile must hide when `sensor.active_commute` is unavailable.
- [ ] Commit `docs: plan 4 (automations) runbook, as-built, skill`; push.
