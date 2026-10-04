# HA Ops Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Home Assistant on galaxy correct and fully declared: zones/persons/default dashboard/storage-dashboard contents in the state engine, pinned components instead of HACS, no DB password in configs or logs, HA on 2026.9.4, Prometheus scraping HA with real Grafana panels, the silent failures guarded, the missing integrations added, and the registry cleaned.

**Architecture:** Two engine extensions to `hactl.state` (plans 1–2), one chart release (`ha-helm` v0.5.0, its own repo, `helm unittest`), then GitOps changes in this repo deployed with `hactl deploy` and verified with `hactl health`/`shot`/`plan`. Every live step is reversible or gated by an `AskUserQuestion`.

**Tech Stack:** Python 3.13 (hactl), Helm + helm-unittest (`~/Repos/ha-helm`, its own `shell.nix`), kustomize/Argo CD, Sealed Secrets, Prometheus Operator `ScrapeConfig`, Grafana dashboards-as-code, Renovate.

**Spec:** `docs/superpowers/specs/2026-10-03-ha-agent-overhaul-design.md` §5 (Phase 2 ops fixes), §4.11 (state engine), plus Chris's decisions recorded 2026-10-03: pinned components now; AirNow (key at `~/.config/galaxy/airnow-token`); upgrade to 2026.9.4 now; obvious dead entities removed without asking; `home-ops` becomes the default dashboard and the stale built-in Overview is retired; zones, persons and UI-made helpers come under the state engine.

## Global Constraints

- Work on `main` in `/home/chris/Repos/luma-homeops` (and `main` of `~/Repos/ha-helm` for Task 4). Commit with `nix develop --command git commit …` (ha-helm: `nix-shell --run 'git commit …'` there); never `--no-verify`. Trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Tests: `nix develop --command python -m pytest -q tools/hactl`; **check pytest's own exit code** (`> log; echo $?`), never through a `| tail` pipe. Chart: `cd ~/Repos/ha-helm && nix-shell --run 'helm unittest .'`.
- `export KUBECONFIG=/tmp/galaxy-kubeconfig` for live steps. No direct cluster writes (S2) — git → Argo → hook → `hactl apply`.
- Secrets: never print tokens or `credentials.yaml`; create secret files with `--from-file` or `$(cat …)` redirected to a file; verify every committed secret is `GITCRYPT`-encrypted with `git show HEAD:<file> | head -c 9 | od -c`.
- Actuation: anything reversible is free; locks/notify/restart need Chris. Integration deletes and anything ambiguous: `AskUserQuestion`.
- HA restarts happen through Argo (chart/image changes) or the ha-reload hook only; expect ~1–2 min downtime each; never during a degraded Ceph (`HEALTH_OK` or only muted/known warnings).
- Before/after evidence for every HA-wide change (components switch, upgrade): `hactl shot` of `/home-ops/0..5` phone+desktop, `hactl health`, `hactl plan`.

## Review Focus

1. **Pinned components switch-over breaks cards** (wrong file, wrong URL, `/hacsfiles` routes gone) → every view still renders with zero error cards; resources point at files that exist. Test: Task 5 `test_card_urls_resolve` (live HEAD checks) + before/after shots.
2. **Chart env ordering** (`$(DB_PASSWORD)` only expands if `DB_PASSWORD` is defined earlier in the same container's env) → HA must still reach Postgres after v0.5.0. Test: Task 4 unittest `env order` + live health after deploy.
3. **Default-dashboard change hides the old Overview content someone relies on** → Overview is replaced only with a pointer card; nothing else is deleted; contents remain recoverable from the restic `.storage` backup. Test: Task 3 shot of `/` lands on `home-ops`.
4. **Upgrade migrates the recorder schema** → a fresh `pg_dumpall` exists before the bump and the rollback is written down; custom components still load. Test: Task 6 health after upgrade lists no new `not_loaded` and `log --errors` has no import errors.
5. **Grafana panels that look alive but query nothing** → every rebuilt HA panel returns data. Test: Task 7 `scripts/check-dashboards.sh` + Prometheus query per panel expression.

---

### Task 1: State engine — zones, persons, UI-made helpers

**Files:**
- Modify: `tools/hactl/src/hactl/state/{model,live,diff,apply,importer}.py`, `tools/hactl/tests/state_fixtures.py`
- Test: `tools/hactl/tests/test_state_people.py`

**Interfaces:**
- Produces: manifest file `people.yaml` with sections `zones` (`id`, `name`, `latitude`, `longitude`; optional `radius` (default 100), `icon`, `passive` (default false)) and `persons` (`id`, `name`; optional `user_id`, `device_trackers` (list), `picture`); `Manifest.zones`, `Manifest.persons`; `Snapshot.zones`, `Snapshot.persons` (the `storage` list of `person/list`), `Snapshot.storage_helpers` (list of `(domain, id, name)` from `input_boolean|input_number|input_select|input_text|input_datetime|input_button|counter|timer|schedule /list`); `diff.diff_zones`, `diff.diff_persons`, `diff.diff_storage_helpers`; plan order: floors, labels, areas, **zones, persons, storage helpers**, helpers, integrations, devices, entities, dashboards, resources. Zones: fully managed (prune). Persons: never deleted (undeclared → manual). Storage helpers: any → manual "UI-made <domain> <name>: move it into a package, then delete it in HA". Apply: `zone/create|update|delete` (`zone_id`; id = slug of name, checked like registries), `person/create|update` (`person_id`).

- [ ] **Step 1: Write the failing tests** `tools/hactl/tests/test_state_people.py`:

```python
from state_fixtures import FakeHA, snapshot

from hactl.state import apply, diff, importer, live, model

WORK = {"id": "work", "name": "Work", "latitude": 47.64, "longitude": -122.13, "radius": 606, "icon": "mdi:microsoft-office"}
CHRIS = {"id": "chris_m", "name": "Chris", "user_id": "u1", "device_trackers": ["device_tracker.pixel_9_pro_xl"]}


def test_zone_and_person_in_sync_and_drift():
    snap = snapshot()
    assert diff.diff_zones([WORK], snap.zones) == []
    [c] = diff.diff_zones([dict(WORK, radius=300)], snap.zones)
    assert (c.action, c.data) == ("update", {"zone_id": "work", "radius": 300})
    [c] = diff.diff_persons([dict(CHRIS, device_trackers=["device_tracker.pixel_9_pro_xl"])], snap.persons)
    assert c.data == {"person_id": "chris_m", "device_trackers": ["device_tracker.pixel_9_pro_xl"]}  # drops pixel_6_pro


def test_undeclared_zone_prunable_person_never_deleted():
    snap = snapshot()
    assert [(c.action, c.prune) for c in diff.diff_zones([], snap.zones)] == [("delete", True)]
    [c] = diff.diff_persons([], snap.persons)
    assert c.action == "manual" and "not declared" in c.detail


def test_ui_made_helpers_are_manual():
    snap = snapshot()
    snap.storage_helpers = [("input_boolean", "guest", "Guest")]
    [c] = diff.diff_storage_helpers(snap.storage_helpers)
    assert c.action == "manual" and "move it into a package" in c.detail


def test_round_trip_and_apply(tmp_path):
    snap = snapshot()
    snap.options = {}
    importer.write(tmp_path, importer.build(snap))
    assert diff.plan(model.load(tmp_path), snap) == []
    fake = FakeHA(snap)
    m = model.load(tmp_path)
    m.zones.append({"id": "gym", "name": "Gym", "latitude": 47.6, "longitude": -122.3})
    result = apply.execute(fake, diff.plan(m, live.fetch(fake)), log=lambda s: None)
    assert result["errors"] == [] and any(z["id"] == "gym" for z in fake.zones)
    assert diff.plan(m, live.fetch(fake)) == []


def test_people_validation(tmp_path):
    (tmp_path / "people.yaml").write_text("zones:\n  - {id: w, name: W, latitude: north, longitude: 1}\npersons: []\n")
    import pytest
    from hactl.errors import HactlError
    with pytest.raises(HactlError, match="latitude must be a number"):
        model.load(tmp_path)
```

Extend `state_fixtures.snapshot()` with:

```python
        zones=[{"id": "work", "name": "Work", "latitude": 47.64, "longitude": -122.13, "radius": 606.0,
                "icon": "mdi:microsoft-office", "passive": False}],
        persons=[{"id": "chris_m", "name": "Chris", "user_id": "u1", "picture": None,
                  "device_trackers": ["device_tracker.pixel_6_pro", "device_tracker.pixel_9_pro_xl"]}],
        storage_helpers=[],
```

and `FakeHA` with `zones`, `persons` lists plus handlers for `zone/list|create|update|delete` (create: `{"id": slug(name), **args}`), `person/list` (returns `{"storage": persons, "config": []}`), `person/create|update`, and `<domain>/list` → `[]` for the nine storage-helper domains (pop ids once, before scanning).

- [ ] **Step 2: Run them to verify they fail.** `nix develop --command python -m pytest -q tools/hactl/tests/test_state_people.py > /tmp/claude-1000/t.log; echo $?` → non-zero; `AttributeError`/`TypeError` on the new fields.

- [ ] **Step 3: Implement.**

`model.py`: add to `SCHEMA`:

```python
    "people": {
        "zones": ({"id", "name", "latitude", "longitude"}, {"radius", "icon", "passive"}),
        "persons": ({"id", "name"}, {"user_id", "device_trackers", "picture"}),
    },
```

add `zones`/`persons` lists to `Manifest`; add `"zones": ("zone", ("id", "name", "icon"))` and `"persons": ("person", ("id", "name", "user_id", "picture"))` to `STRING_FIELDS`; in `check_types` add:

```python
    for z in m.zones:
        for f in ("latitude", "longitude", "radius"):
            if f in z and z[f] is not None and (isinstance(z[f], bool) or not isinstance(z[f], (int, float))):
                p.append(f"zone {z.get('id')}: {f} must be a number")
        if "passive" in z and not isinstance(z["passive"], bool):
            p.append(f"zone {z.get('id')}: passive must be true or false")
    for x in m.persons:
        if "device_trackers" in x and not _strlist(x["device_trackers"]):
            p.append(f"person {x.get('id')}: device_trackers must be a list of entity ids")
```

and in `check` add duplicate-id checks for zones and persons.

`live.py`: `STORAGE_HELPER_DOMAINS = ("input_boolean", "input_number", "input_select", "input_text", "input_datetime", "input_button", "counter", "timer", "schedule")`; add `zones`, `persons`, `storage_helpers` fields (default empty) to `Snapshot`; in `fetch` add `{"type": "zone/list"}`, `{"type": "person/list"}` and one `{"type": f"{d}/list"}` per helper domain to the same `client.ws(...)` call; set `persons = person_list["storage"]` and `storage_helpers = [(d, x.get("id"), x.get("name")) for d, items in zip(STORAGE_HELPER_DOMAINS, lists) for x in items]`.

`diff.py`:

```python
ZONE_FIELDS = {"name": "name", "latitude": "latitude", "longitude": "longitude", "radius": "radius", "icon": "icon", "passive": "passive"}
ZONE_DEFAULTS = {"radius": 100, "icon": None, "passive": False}
PERSON_FIELDS = {"name": "name", "user_id": "user_id", "device_trackers": "device_trackers", "picture": "picture"}


def diff_zones(declared, live) -> list:
    by_id = {z["id"]: z for z in live}
    out = []
    for d in declared:
        want = {f: d.get(f, ZONE_DEFAULTS.get(f)) for f in ZONE_FIELDS}
        cur = by_id.get(d["id"])
        if cur is None:
            out.append(Change("create", "zone", d["id"], repr(d["name"]), data={"id": d["id"], **want}))
            continue
        delta = {f: v for f, v in want.items() if not same(v, cur.get(f))}
        if delta:
            out.append(Change("update", "zone", d["id"], _describe(delta, cur), data={"zone_id": d["id"], **delta}))
    declared_ids = {d["id"] for d in declared}
    out += [Change("delete", "zone", z["id"], f"{z.get('name')!r} is not declared", data={"zone_id": z["id"]}, prune=True)
            for z in live if z["id"] not in declared_ids]
    return out


def diff_persons(declared, live) -> list:
    by_id = {p["id"]: p for p in live}
    out = []
    for d in declared:
        cur = by_id.get(d["id"])
        if cur is None:
            out.append(Change("create", "person", d["id"], repr(d["name"]),
                              data={"id": d["id"], **{f: d.get(f, [] if f == "device_trackers" else None) for f in PERSON_FIELDS}}))
            continue
        delta = {f: d.get(f, [] if f == "device_trackers" else None) for f in PERSON_FIELDS
                 if not same(d.get(f, [] if f == "device_trackers" else None), cur.get(f))}
        if delta:
            out.append(Change("update", "person", d["id"], _describe(delta, cur), data={"person_id": d["id"], **delta}))
    declared_ids = {d["id"] for d in declared}
    out += [Change("manual", "person", p["id"], "is not declared in people.yaml (declare it; hactl never deletes a person)")
            for p in live if p["id"] not in declared_ids]
    return out


def diff_storage_helpers(storage_helpers) -> list:
    return [Change("manual", "ui-helper", f"{d}.{i}", f"UI-made {d} {n!r}: move it into a package, then delete it in HA")
            for d, i, n in storage_helpers]
```

and splice into `plan()` after the area line: `+ diff_zones(m.zones, snap.zones) + diff_persons(m.persons, snap.persons) + diff_storage_helpers(snap.storage_helpers)`.

`apply.py` `run_change`, before the `device` branch:

```python
    if c.kind == "zone":
        if c.action == "create":
            got = client.ws({"type": "zone/create", **_without_empty({k: v for k, v in c.data.items() if k != "id"})})[0]["id"]
            if got != c.data["id"]:
                raise HactlError(f"HA created zone id {got!r}, the manifest says {c.data['id']!r}: set id: {got} in people.yaml")
        else:
            client.ws({"type": f"zone/{c.action}", **c.data})
        return ""
    if c.kind == "person":
        if c.action == "create":
            client.ws({"type": "person/create", **_without_empty({k: v for k, v in c.data.items() if k != "id"})})
        else:
            client.ws({"type": "person/update", **c.data})
        return ""
```

`importer.py`: `HEADERS["people"] = "Zones (fully managed; delete needs --prune) and persons (never deleted by hactl)."`; in `build` add

```python
    files["people"] = {
        "zones": [{**_compact({"id": z["id"], "name": z["name"], "icon": z.get("icon")}), "latitude": z["latitude"],
                   "longitude": z["longitude"], "radius": z.get("radius", 100), **({"passive": True} if z.get("passive") else {})}
                  for z in snap.zones],
        "persons": [_compact({"id": p["id"], "name": p["name"], "user_id": p.get("user_id"),
                              "device_trackers": p.get("device_trackers"), "picture": p.get("picture")}) for p in snap.persons],
    }
```

- [ ] **Step 4: Run all tests; pytest exit 0.**
- [ ] **Step 5: Commit** `hactl state: zones, persons, and UI-made helper detection`.

---

### Task 2: State engine — default dashboard and storage-dashboard contents

**Files:**
- Modify: `tools/hactl/src/hactl/state/{model,live,diff,apply}.py`, `state_fixtures.py`; `cli._import` gains `--only NAME` (repeatable)
- Test: `tools/hactl/tests/test_state_dashboards.py`

**Interfaces:**
- `dashboards.yaml` gains an optional top-level `default: <url_path>` (string) and each storage dashboard an optional `config: <file>` naming a YAML file in `cluster/home-assistant/state/dashboard_configs/` (a full Lovelace config: mapping with `views:`). `Manifest.default_dashboard: str | None`, `Manifest.dashboard_configs: dict[url_path, dict]`. `Snapshot.system_core: dict` (from `frontend/get_system_data {key: core}` → `value`), `Snapshot.dashboard_configs: dict[url_path, dict]` (fetched with `lovelace/config {url_path}` — `None` for `lovelace` — only for declared ones). Changes: `update default-dashboard core` → `frontend/set_system_data {key: "core", value: {**system_core, "default_panel": X}}`; `update dashboard-config <url_path>` → `lovelace/config/save {url_path (None for lovelace), config}`. `importer.write(state_dir, files, force, only=None)`: `only` restricts which files are written.

- [ ] **Step 1: Write the failing tests** `tools/hactl/tests/test_state_dashboards.py`:

```python
from state_fixtures import FakeHA, snapshot, write_manifests

from hactl.state import apply, diff, importer, live, model

RETIRED = {"views": [{"title": "Moved", "cards": [{"type": "markdown", "content": "Moved to **Home**."}]}]}


def test_default_dashboard_diff_and_apply(tmp_path):
    snap = snapshot()
    m = model.Manifest(default_dashboard="home-ops")
    [c] = diff.diff_default_dashboard(m.default_dashboard, snap.system_core)
    assert c.data == {"key": "core", "value": {"default_panel": "home-ops"}}
    fake = FakeHA(snap)
    apply.execute(fake, [c], log=lambda s: None)
    assert fake.system_core["default_panel"] == "home-ops"
    assert diff.diff_default_dashboard(None, snap.system_core) == []  # undeclared: unmanaged


def test_dashboard_config_from_file(tmp_path):
    state = write_manifests(tmp_path, {"dashboards.yaml": "default: home-ops\ndashboards:\n  - {url_path: lovelace, title: Overview, config: overview_retired.yaml}\nresources: []\n"})
    (state / "dashboard_configs").mkdir()
    (state / "dashboard_configs" / "overview_retired.yaml").write_text("views:\n  - title: Moved\n    cards:\n      - type: markdown\n        content: Moved to **Home**.\n")
    m = model.load(state)
    assert m.default_dashboard == "home-ops" and m.dashboard_configs["lovelace"] == RETIRED
    snap = snapshot()
    snap.dashboard_configs = {"lovelace": {"views": [{"title": "Old"}]}}
    [c] = diff.diff_dashboard_configs(m.dashboard_configs, snap.dashboard_configs)
    assert c.data == {"url_path": None, "config": RETIRED}  # the default dashboard's url_path is None on the API


def test_missing_config_file_is_a_problem(tmp_path):
    import pytest
    from hactl.errors import HactlError
    write_manifests(tmp_path, {"dashboards.yaml": "dashboards:\n  - {url_path: lovelace, title: O, config: nope.yaml}\n"})
    with pytest.raises(HactlError, match="nope.yaml"):
        model.load(tmp_path)


def test_import_only_writes_named_files(tmp_path):
    snap = snapshot()
    importer.write(tmp_path, importer.build(snap))
    (tmp_path / "areas.yaml").write_text("# hand edited\nareas: []\n")
    importer.write(tmp_path, importer.build(snap), only=["people"], force=True)
    assert (tmp_path / "areas.yaml").read_text().startswith("# hand edited")
```

Extend fixtures: `Snapshot.system_core={"default_panel": "lovelace"}`, `dashboard_configs={}`; `FakeHA.system_core` + handlers `frontend/get_system_data` (→ `{"value": dict(self.system_core)}`), `frontend/set_system_data` (replace), `lovelace/config` (→ stored config or `{"views": []}`), `lovelace/config/save` (store).

- [ ] **Step 2: Run them; non-zero exit (missing attributes).**
- [ ] **Step 3: Implement.** `model.load`: for `dashboards.yaml`, accept a top-level scalar `default` (must be a string) and per-dashboard optional `config` (add `"config"` to the optional keys); read each `config:` file from `state_dir / "dashboard_configs" / name` with `yaml.safe_load`, require a mapping with a `views` list (problem otherwise, naming the file), store in `Manifest.dashboard_configs[url_path]`. `live.fetch(client, want_options, want_configs=())`: add `{"type": "frontend/get_system_data", "key": "core"}` (`system_core = (result or {}).get("value") or {}`) and, for each wanted url_path, `{"type": "lovelace/config", "url_path": None if u == "lovelace" else u}`. `diff`:

```python
def diff_default_dashboard(declared, system_core) -> list:
    if declared is None or system_core.get("default_panel") == declared:
        return []
    return [Change("update", "default-dashboard", "core", f"default_panel {system_core.get('default_panel')!r} -> {declared!r}",
                   data={"key": "core", "value": {**system_core, "default_panel": declared}})]


def diff_dashboard_configs(declared, live) -> list:
    return [Change("update", "dashboard-config", u, "contents differ from the declared file",
                   data={"url_path": None if u == "lovelace" else u, "config": cfg})
            for u, cfg in sorted(declared.items()) if live.get(u) != cfg]
```

append both to `plan()` (after resources); `cli.compute` passes `want_configs=list(m.dashboard_configs)`. `apply.run_change`: `default-dashboard` → `client.ws({"type": "frontend/set_system_data", **c.data})`; `dashboard-config` → `client.ws({"type": "lovelace/config/save", **c.data})`. `importer.write(..., only=None)`: when `only` is given, write just those file stems (the overwrite check also applies only to them). `cli` `import` gets `--only` (`action="append"`).

- [ ] **Step 4: All tests pass (exit 0). Live read-only check:** `nix develop --command hactl plan` → `state: in sync` (nothing declared yet for the new fields).
- [ ] **Step 5: Commit** `hactl state: default dashboard and declared storage-dashboard contents; import --only`.

---

### Task 3: Retire Overview, default to Home, people.yaml, Camp Lamp helper (live)

**Files:** `cluster/home-assistant/state/{people,dashboards,helpers}.yaml`, `cluster/home-assistant/state/dashboard_configs/overview_retired.yaml`

- [ ] **Step 1: Import people.** `nix develop --command hactl import --only people --force` → writes `people.yaml` (Work zone, person Chris with both trackers). `hactl plan` → in sync.
- [ ] **Step 2: Declare the changes.**
  - `dashboards.yaml`: add top-level `default: home-ops`; on the `lovelace` entry add `config: overview_retired.yaml`.
  - `dashboard_configs/overview_retired.yaml`:

```yaml
# The pre-2026-07 Overview, retired 2026-10-03 (its contents were a stale copy of
# Home with dead entities). The built-in can't be deleted, so it points at Home.
# The old contents are in the restic .storage backup (lovelace.lovelace).
views:
  - title: Moved
    path: moved
    cards:
      - type: markdown
        content: |
          ## This dashboard moved
          Everything lives on **[Home](/home-ops/0)** now.
```

  - `helpers.yaml`: add

```yaml
- domain: switch_as_x
  title: Camp Lamp
  create:
    answers:
      entity_id: switch.camp_lamp
      target_domain: light
```

- [ ] **Step 3: Plan, apply, verify.** `hactl plan` must show exactly: `~ default-dashboard core`, `~ dashboard-config lovelace`, `+ helper switch_as_x/Camp Lamp`. `hactl apply` → applied 3, `hactl plan` → in sync. `hactl find camp` → `light.camp_lamp` exists. `hactl shot / --viewport phone` → report's URL/screenshot is the Home view (`home-ops`); `hactl shot /lovelace/0 --viewport phone` → the "moved" card. Read both PNGs.
- [ ] **Step 4: Commit + push + `hactl deploy`** (state in sync, health ok). Message: `home-assistant: Home is the default dashboard; Overview retired; Camp Lamp as a light; zones/persons declared`.

---

### Task 4: ha-helm v0.5.0 — no password in config/logs/args; pinned components installer

**Repo:** `~/Repos/ha-helm`. **Files:** `templates/deployment.yaml`, `values.yaml`, `Chart.yaml` (0.5.0), `README.md` (values table), `tests/v050_test.yaml`; regenerate `tests/__snapshot__` if the existing snapshot test changes.

**Interfaces (values):**

```yaml
components:
  enabled: false
  image: {repository: alpine, tag: "3.20", pullPolicy: IfNotPresent}
  # Each: name (dir under custom_components), repo (owner/name), version (git tag).
  integrations: []
  # Each: name (dir under www/community), repo, version, url (may contain {version}).
  cards: []
  # custom_components dirs to delete (e.g. hacs after its integration entry is removed).
  remove: []
```

`db_url` becomes `!env_var HA_DB_URL`; the main and `check-config` containers get `DB_PASSWORD` (secretKeyRef) **then** `HA_DB_URL = postgresql://<user>:$(DB_PASSWORD)@<host>:<port>/<db>`; `create-ha-db-if-missing` uses `PGPASSWORD` from the secret and never echoes it.

- [ ] **Step 1: Write the failing tests** `tests/v050_test.yaml`:

```yaml
suite: v0.5.0 secrets and components
templates:
  - deployment.yaml
set:
  externalPostgres: {host: acid-ha, port: 5432, username: postgres, passwordFromSecretKeyRef: {name: pg, key: password}}
  checkConfig: {enabled: true}
tests:
  - it: generated config references the env var, never the password
    asserts:
      - matchRegex: {path: "spec.template.spec.initContainers[?(@.name == 'generate-config')].command[2]", pattern: "db_url: !env_var HA_DB_URL"}
      - notMatchRegex: {path: "spec.template.spec.initContainers[?(@.name == 'generate-config')].command[2]", pattern: "DB_PASSWORD"}
  - it: main container defines DB_PASSWORD before HA_DB_URL (env order makes $(DB_PASSWORD) expand)
    asserts:
      - equal: {path: "spec.template.spec.containers[0].env[0].name", value: DB_PASSWORD}
      - equal: {path: "spec.template.spec.containers[0].env[1].name", value: HA_DB_URL}
      - equal: {path: "spec.template.spec.containers[0].env[1].value", value: "postgresql://postgres:$(DB_PASSWORD)@acid-ha:5432/homeassistant"}
  - it: check-config sees the same env
    asserts:
      - equal: {path: "spec.template.spec.initContainers[?(@.name == 'check-config')].env[1].name", value: HA_DB_URL}
  - it: create-db never echoes or interpolates the password
    asserts:
      - notMatchRegex: {path: "spec.template.spec.initContainers[?(@.name == 'create-ha-db-if-missing')].command[3]", pattern: "echo|DB_PASSWORD"}
      - equal: {path: "spec.template.spec.initContainers[?(@.name == 'create-ha-db-if-missing')].env[0].name", value: PGPASSWORD}
  - it: components installer renders pinned integrations, cards and removals; hacs off
    set:
      hacs: false
      components:
        enabled: true
        integrations: [{name: adaptive_lighting, repo: basnijholt/adaptive-lighting, version: v1.31.0}]
        cards: [{name: lovelace-mushroom, repo: piitaya/lovelace-mushroom, version: v5.1.1,
                 url: "https://github.com/piitaya/lovelace-mushroom/releases/download/{version}/mushroom.js"}]
        remove: [hacs]
    asserts:
      - matchRegex: {path: "spec.template.spec.initContainers[?(@.name == 'install-components')].command[2]", pattern: 'install_integration "adaptive_lighting" "basnijholt/adaptive-lighting" "v1.31.0"'}
      - matchRegex: {path: "spec.template.spec.initContainers[?(@.name == 'install-components')].command[2]", pattern: 'install_card "lovelace-mushroom" "https://github.com/piitaya/lovelace-mushroom/releases/download/\{version\}/mushroom.js" "v5.1.1"'}
      - matchRegex: {path: "spec.template.spec.initContainers[?(@.name == 'install-components')].command[2]", pattern: 'rm -rf "/config/custom_components/hacs"'}
      - notContains: {path: spec.template.spec.initContainers, content: {name: bootstrap-hacs-if-needed}, any: true}
```

(If `helm-unittest`'s JSONPath filter syntax differs in the installed version, use the container's index — read the rendered order with `helm template test . -f <values>` — and keep the assertions identical.)

- [ ] **Step 2:** `nix-shell --run 'helm unittest . -f tests/v050_test.yaml'` → failures (no env_var, no installer).
- [ ] **Step 3: Implement.**
  - `generate-config`: replace the `db_url:` line with `db_url: !env_var HA_DB_URL`; remove its `env:` block (it no longer needs the password).
  - `create-ha-db-if-missing`: command becomes `psql -U … -tc "SELECT 1 …" | grep -q 1 || psql -U … -c "CREATE DATABASE …"`, with `env: [{name: PGPASSWORD, valueFrom: {secretKeyRef: …}}]`.
  - Define a named template `home-assistant.dbEnv` emitting `DB_PASSWORD` (secretKeyRef) then `HA_DB_URL` (value `postgresql://{{ $db_user }}:$(DB_PASSWORD)@{{ $db_host }}:{{ $db_port }}/{{ .Values.database.name }}`), and include it as `env:` in the main container and `check-config`.
  - `install-components` init container (when `components.enabled`), placed after `bootstrap-configs-if-needed`, image `components.image`, mounting `persist-configs` at `/config`, command `sh -c` with:

```sh
set -eu
install_integration() {  # name repo version
  marker="/config/custom_components/$1/.pinned-version"
  if [ -f "$marker" ] && [ "$(cat "$marker")" = "$3" ]; then echo "$1 $3 present"; return; fi
  tmp=$(mktemp -d)
  wget -qO "$tmp/src.tgz" "https://github.com/$2/archive/refs/tags/$3.tar.gz"
  tar -xzf "$tmp/src.tgz" -C "$tmp"
  src=$(find "$tmp" -maxdepth 3 -type d -path "*/custom_components/$1" | head -n1)
  [ -n "$src" ] || { echo "no custom_components/$1 in $2@$3" >&2; exit 1; }
  mkdir -p /config/custom_components && rm -rf "/config/custom_components/$1"
  cp -r "$src" "/config/custom_components/$1" && echo "$3" > "$marker" && rm -rf "$tmp"
  echo "installed $1 $3"
}
install_card() {  # name url-with-{version} version
  dir="/config/www/community/$1"; marker="$dir/.pinned-version"
  if [ -f "$marker" ] && [ "$(cat "$marker")" = "$3" ]; then echo "$1 $3 present"; return; fi
  url=$(echo "$2" | sed "s/{version}/$3/g"); mkdir -p "$dir"
  wget -qO "$dir/$(basename "$url")" "$url" && echo "$3" > "$marker"
  echo "installed $1 $3"
}
```

    followed by one `install_integration "<name>" "<repo>" "<version>"` line per integration, one `install_card "<name>" "<url>" "<version>"` per card, and `rm -rf "/config/custom_components/<x>" && echo "removed <x>"` per `remove` entry (Helm `range`, values `quote`d).
  - `values.yaml`: the `components:` block above; README values table rows for it.
  - `Chart.yaml`: `version: 0.5.0`.
- [ ] **Step 4:** `nix-shell --run 'helm unittest . && helm lint .'` → all pass (update the existing snapshot with `-u` only if the diff is exactly the env/command changes above — read it first).
- [ ] **Step 5:** Commit `0.5.0: HA_DB_URL via !env_var (no password in config, logs or args); pinned components installer`, tag `v0.5.0`, push `main` and the tag (Chris's chart repo; plan-approved).

---

### Task 5: Switch galaxy to v0.5.0 with pinned components; drop HACS; Renovate

**Files:** `cluster/applications/home-assistant-release.yaml`, `cluster/home-assistant/state/{dashboards,integrations}.yaml`, `tools/hactl/src/hactl/lint.py` (custom components from the release values), `.renovaterc.json`, `tools/hactl/tests/test_lint.py`

- [ ] **Step 1: Failing test** in `test_lint.py`:

```python
def test_custom_components_come_from_release_values(tmp_path):
    f = tmp_path / "release.yaml"
    f.write_text("spec:\n  source:\n    helm:\n      valuesObject:\n        components:\n          integrations:\n"
                 "            - {name: adaptive_lighting, repo: basnijholt/adaptive-lighting, version: v1.31.0}\n")
    assert lint.custom_components(f) == [("adaptive_lighting", "basnijholt/adaptive-lighting", "v1.31.0")]
```

and make `check_config` clone each integration **at its pinned tag** (`git clone -q --depth 1 --branch <version>`), replacing the hard-coded `CUSTOM_COMPONENTS`.
- [ ] **Step 2: Before evidence.** `hactl shot /home-ops/0 … /home-ops/5` (phone+desktop) → keep the output dir path; `hactl health`; `hactl plan`.
- [ ] **Step 3: Release values.** In `home-assistant-release.yaml`: `targetRevision: v0.5.0`; `hacs: false`; add

```yaml
        components:
          enabled: true
          integrations:
            - name: adaptive_lighting
              repo: basnijholt/adaptive-lighting
              version: v1.31.0
            - name: mail_and_packages
              repo: moralmunky/Home-Assistant-Mail-And-Packages
              version: 0.5.16
            - name: smartrent
              repo: ZacheryThomas/homeassistant-smartrent
              version: v0.5.5
          cards:
            - {name: lovelace-mushroom, repo: piitaya/lovelace-mushroom, version: v5.1.1, url: "https://github.com/piitaya/lovelace-mushroom/releases/download/{version}/mushroom.js"}
            - {name: lovelace-horizon-card, repo: rejuvenate/lovelace-horizon-card, version: v1.5.3, url: "https://github.com/rejuvenate/lovelace-horizon-card/releases/download/{version}/lovelace-horizon-card.js"}
            - {name: calendar-card-pro, repo: alexpfau/calendar-card-pro, version: v3.2.0, url: "https://github.com/alexpfau/calendar-card-pro/releases/download/{version}/calendar-card-pro.js"}
            - {name: lovelace-card-mod, repo: thomasloven/lovelace-card-mod, version: v4.2.1, url: "https://raw.githubusercontent.com/thomasloven/lovelace-card-mod/{version}/card-mod.js"}
            - {name: Bubble-Card, repo: Clooos/Bubble-Card, version: v3.2.5, url: "https://raw.githubusercontent.com/Clooos/Bubble-Card/{version}/dist/bubble-card.js"}
            - {name: lovelace-layout-card, repo: thomasloven/lovelace-layout-card, version: v2.4.7, url: "https://raw.githubusercontent.com/thomasloven/lovelace-layout-card/{version}/layout-card.js"}
            - {name: Ultra-Card, repo: WJDDesigns/Ultra-Card, version: v3.5.0, url: "https://github.com/WJDDesigns/Ultra-Card/releases/download/{version}/ultra-card.js"}
            - {name: weather_alerts_card, repo: seevee/weather_alerts_card, version: v3.2.0, url: "https://github.com/seevee/weather_alerts_card/releases/download/{version}/weather-alerts-card.js"}
```

(versions = what HACS has installed now, verified 2026-10-03: zero behaviour change). In `dashboards.yaml` replace each `/hacsfiles/<dir>/<file>?hacstag=…` resource with `/local/community/<dir>/<file>?v=<version>` (type module).
- [ ] **Step 4: Renovate.** Add to `.renovaterc.json`:

```json
"customManagers": [
  {"customType": "regex", "description": "HA image tag in the Argo release",
   "managerFilePatterns": ["/^cluster/applications/home-assistant-release\\.yaml$/"],
   "matchStrings": ["image:\\s*\\n\\s*tag: (?<currentValue>\\d{4}\\.\\d+\\.\\d+)"],
   "depNameTemplate": "homeassistant/home-assistant", "datasourceTemplate": "docker"},
  {"customType": "regex", "description": "Pinned HA custom integrations/cards",
   "managerFilePatterns": ["/^cluster/applications/home-assistant-release\\.yaml$/"],
   "matchStrings": ["repo: (?<depName>[\\w.-]+/[\\w.-]+), version: (?<currentValue>\\S+?)[,}]", "repo: (?<depName>[\\w.-]+/[\\w.-]+)\\n\\s+version: (?<currentValue>\\S+)"],
   "datasourceTemplate": "github-tags"}
]
```

plus a `packageRules` entry `{"matchDatasources": ["github-tags"], "matchFileNames": ["cluster/applications/home-assistant-release.yaml"], "automerge": false}` and the same for `homeassistant/home-assistant`. Validate: `nix develop --command npx --yes --package renovate -- renovate-config-validator .renovaterc.json` → "Config validated successfully" (if `managerFilePatterns` is rejected by the installed validator, use `fileMatch` with the same regex).
- [ ] **Step 5: `test_card_urls_resolve`.** Add a live-only test (skipped unless `HACTL_LIVE=1`) that HEADs every card URL with `{version}` substituted and expects 200; run it with `HACTL_LIVE=1`.
- [ ] **Step 6: Commit, push, `hactl deploy --shot`** (the release app rolls HA; deploy waits for rollout). Then `hactl apply --prune` for the 8 old `/hacsfiles` resources (plan must list exactly those 8 deletes + 8 creates first). After evidence: same shots, zero error cards, every view visually identical to the before set (Read pairs); `hactl health` ok.
- [ ] **Step 7: Remove HACS.** `AskUserQuestion`: "Delete the HACS integration entry now? (the pinned installer replaced it)". On yes: Chris deletes it in the UI (Settings → Devices & services → HACS → Delete) — hactl never deletes integrations; remove the `hacs` entry from `integrations.yaml`; add `remove: [hacs]` under `components`; commit, push, deploy; `hactl plan` in sync.

---

### Task 6: Upgrade HA 2026.7.2 → 2026.9.4

- [ ] **Step 1: Preflight + backup.** Ceph healthy (as Global Constraints). `kubectl -n home-assistant exec acid-ha-0 -c postgres -- pg_dumpall -U postgres > ~/ha-pgdump-2026-10-03.sql` (local, not committed; `ls -l` it, expect > 10 MB). Before evidence (shots, health, `hactl log --errors`).
- [ ] **Step 2: Compatibility.** `nix develop --command bash -c 'sed -i "s/tag: 2026.7.2/tag: 2026.9.4/" cluster/applications/home-assistant-release.yaml && hactl lint'` → `check_config` runs in the 2026.9.4 image with the pinned components; must be clean. If a custom component fails, bump its pin to the newest release that supports 2026.9 (read its release notes), re-lint.
- [ ] **Step 3: Deploy.** Commit `home-assistant: 2026.7.2 -> 2026.9.4`, push, `hactl deploy --shot` (rollout + recorder migration; allow the full timeout).
- [ ] **Step 4: Verify.** `hactl health` ok; no new `not_loaded`; `hactl log --errors` shows no import/setup errors from custom components; shots match. **Rollback** (only if broken): revert the tag commit, push, and if the recorder won't start on the old version, restore the dump (`psql -U postgres -f ~/ha-pgdump-2026-10-03.sql` into a recreated DB — ask Chris first).

---

### Task 7: Prometheus scrapes HA; Grafana HA panels rebuilt on real series

**Files:** `cluster/home-assistant/ha-metrics-token.secret.yaml` (+ sealed `ha-metrics-token.yaml`), `cluster/home-assistant/ha-scrapeconfig.yaml`, `cluster/home-assistant/kustomization.yaml`, `cluster/grafana/dashboards/home-automation.yaml`

- [ ] **Step 1: Token + ScrapeConfig.** The secret must live in the `prometheus` namespace (where the ScrapeConfig and Prometheus are): `kubectl create secret generic ha-metrics-token -n prometheus --from-file=token=$HOME/.config/galaxy/prom-token --dry-run=client -o yaml > cluster/home-assistant/ha-metrics-token.secret.yaml`, `./sign.sh`, verify `kind: SealedSecret`. `ha-scrapeconfig.yaml` (namespace `prometheus`, label `release: prometheus`, `metricsPath: /api/prometheus`, `scrapeInterval: 60s`, `staticConfigs: [{targets: ["ha-home-assistant.home-assistant.svc:8123"]}]`, `authorization: {type: Bearer, credentials: {name: ha-metrics-token, key: token}}`), modelled on `cluster/ipmi-exporter/scrapeconfig.yaml`. Add both to `kustomization.yaml` `resources`. Note: the kustomization sets `namespace: home-assistant` — these two need `namespace: prometheus` explicitly and kustomize must not override it (if it does, move them to `cluster/prometheus`'s kustomization instead).
- [ ] **Step 2: Deploy, then prove the scrape:** `kubectl -n prometheus exec deploy/… -- wget -qO- 'http://localhost:9090/api/v1/query?query=count(homeassistant_entity_available)'` (or via the Grafana datasource) → a count > 0. Record which `homeassistant_*` families exist (`/api/v1/label/__name__/values` filtered).
- [ ] **Step 3: Rebuild the 15 HA panels** in `cluster/grafana/dashboards/home-automation.yaml` on series that exist: entity availability (`homeassistant_entity_available`), state changes (`rate(homeassistant_state_change_total[5m])`), automations triggered (`homeassistant_automation_triggered_count` if exported), temperatures/humidity (`homeassistant_sensor_temperature_celsius`/`…_humidity_percent`), light/switch states, battery levels (`homeassistant_sensor_battery_percent`). Drop panels for metrics HA does not export (grid/solar/database size/memory/api latency) — no fake zeros. Validate with `scripts/check-dashboards.sh` (must report no dead HA panels).
- [ ] **Step 4: Commit + push; Argo syncs Grafana; screenshot the dashboard in Grafana is optional (not in hactl's scope).**

---

### Task 8: Package guards and the city signal

**Files:** `cluster/home-assistant/packages/{plant_blinds,overview_brain}.yaml`

- [ ] **Step 1: Read current traces/logs** (`hactl trace automation.plant_blinds_smart_solar_control --last 1`, `hactl log --errors`) to confirm the failure modes.
- [ ] **Step 2: Plant blinds.** Add a top-level `conditions:` entry `{condition: template, value_template: "{{ states('cover.plant_blinds') not in ['unavailable','unknown'] and states('sensor.plant_blinds_battery') | int(0) > 0 }}"}` and `max_exceeded: silent`; replace both `state_attr('weather.forecast_home','forecast')[0].condition …` expressions with `states('weather.forecast_home')` (the `forecast` attribute was removed from weather entities in 2024; the current condition is what the logic needs).
- [ ] **Step 3: Template availability.** `sensor.active_temperature`, `sensor.active_commute`: add `availability:` templates that are false when the source entity is missing/unavailable (`{% set w = state_attr('sensor.current_city','weather_entity') %}{{ w is not none and states(w) not in ['unknown','unavailable'] }}`, same pattern for commute).
- [ ] **Step 4: City from the phone's time zone.** `sensor.current_city` state: `{% set tz = states('sensor.pixel_9_pro_xl_current_time_zone') %}{% if tz == 'America/Toronto' %}Toronto{% elif tz not in ['unknown','unavailable'] %}Seattle{% elif is_state('calendar.chris_is_not_in_seattle_calendar','on') %}Toronto{% else %}Seattle{% endif %}` (keep the attributes keyed off the same decision — compute it once with a variable at the top of each template). Verify each template with `hactl template -f` before editing.
- [ ] **Step 5:** `hactl lint` clean; commit, push, `hactl deploy`; `hactl log --errors --since 30m` has no `plant_blinds`/`active_*` errors; `hactl state sensor.current_city` = Seattle.

---

### Task 9: Integrations — AirNow, OctoPrint, Plex

- [ ] **Step 1: AirNow (declarative).** Put the key into `credentials.yaml` without printing it: `nix develop --command python3 -c "import pathlib,yaml; p=pathlib.Path('cluster/home-assistant/state/credentials.yaml'); d=yaml.safe_load(p.read_text()) or {}; d['airnow']={'api_key': pathlib.Path.home().joinpath('.config/galaxy/airnow-token').read_text().strip()}; p.write_text(p.read_text().split('{}')[0] + yaml.safe_dump(d))"`. Add to `integrations.yaml`: `{domain: airnow, title: <title HA will use>, create: {answers: {radius: 150}}, credentials: airnow}` — first start the flow read-only to learn the title HA assigns? (No: run `hactl apply`; if the created title differs, apply sets it to the declared one.) Use `title: AirNow`. `hactl plan` → `+ integration airnow/AirNow`; `hactl apply`; `hactl find air_quality` / `hactl find aqi` → AQI sensor id; commit (verify `credentials.yaml` blob is GITCRYPT).
- [ ] **Step 2: OctoPrint (manual: needs approval in OctoPrint).** Add `{domain: octoprint, title: OctoPrint, manual: "HA: Add integration -> OctoPrint, host 192.168.0.243, port 80, path /, ssl off; then approve the 'Home Assistant' app key in OctoPrint (Settings -> Application Keys)"}`. `AskUserQuestion`: "Add OctoPrint now? (you'll approve the key in OctoPrint's UI)". After Chris confirms it's added: `hactl plan` must show it present (fix `title:` to the real one if HA used another — check `hactl plan` output for the undeclared title), `hactl find octoprint` shows `binary_sensor.octoprint_printing`.
- [ ] **Step 3: Plex (manual).** `AskUserQuestion`: "Re-add Plex? Delete the stale entry (http://10.244.4.204:32400) and add Plex with manual URL http://mm-plex.media.svc.cluster.local:32400 (plex.tv sign-in or token)." After: update `integrations.yaml` title to the new entry's title; `hactl health` no longer lists plex in `not_loaded`.
- [ ] **Step 4:** commit, push, deploy; `hactl plan` in sync.

---

### Task 10: Registry cleanup

- [ ] **Step 1: Candidates.** One-off read-only listing: entities whose state is `unavailable`/missing for 30+ days (use `hactl stats`/recorder `states` last_changed) or whose `config_entry_id` no longer exists, plus the known set: the stale `mail_*` duplicates (live set is `sensor.hello_chrismiller_xyz_mail_*`), the orphaned template `sensor.show_commute`, the Pixel 6 Pro's entities. Classify: **obvious** (no config entry/device, or unavailable 30+ days and superseded) vs **ambiguous**.
- [ ] **Step 2: Obvious ones** (Chris pre-approved): add each to `entities.yaml` `remove:` with `about:`; drop `device_tracker.pixel_6_pro` from the person in `people.yaml`. **Ambiguous ones + deleting the Pixel 6 Pro mobile_app integration entry:** one `AskUserQuestion` listing them.
- [ ] **Step 3:** `hactl plan` (review every `- entity` line), `hactl apply`, `hactl plan` in sync; commit, push, deploy.

---

### Task 11: Docs, spec as-built, memory

- [ ] `docs/ha.md`: people.yaml, `default:`/`config:` in dashboards.yaml, pinned components (values in the release; Renovate bumps; how to add a card), Prometheus scrape token row in the Tokens table, Overview retired.
- [ ] Spec §5 as-built: components live in the release `valuesObject` (not a separate `components.yaml`), the generated config is a memory `emptyDir` (password was in it + init logs/args, now neither), Plex/OctoPrint were manual, Grafana HA panels rebuilt (not renamed).
- [ ] `ha-config` skill: pinned components + "never install through HACS".
- [ ] Memory: plan 3 shipped; remaining plans 4–5.
- [ ] Commit + push.
