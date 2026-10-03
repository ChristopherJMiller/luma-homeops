# HA Declarative State (`hactl import / plan / apply`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Describe every declarable piece of Home Assistant's `.storage` state in git (`cluster/home-assistant/state/*.yaml`) and converge live HA to it with `hactl plan` / `hactl apply`, terraform-style.

**Architecture:** A `hactl.state` subpackage: `model` loads and validates manifests; `live` snapshots HA's registries, config entries and Lovelace collections over the websocket API; `flows` drives config and options flows from declared answers; `diff` turns (manifest, snapshot) into an ordered list of `Change`s; `apply` executes them (creates/updates in order, prunes in reverse, only with `--prune`); `importer` writes the first manifests from live state. `plan` is pure and exhaustively unit-tested; `apply` is tested against an in-memory fake HA. `hactl deploy` converges after every push, and `hactl health` reports a non-empty plan as drift.

**Tech Stack:** Python 3.13, PyYAML, the `hactl` client from plan 1 (REST + websocket), pytest.

**Spec:** `docs/superpowers/specs/2026-10-03-ha-agent-overhaul-design.md` §4.11 (plus §4.3 command table, §4.6 deploy, §9 Phase 1 acceptance "`hactl import` manifests committed and `hactl plan` empty"). Plan 1 (`docs/superpowers/plans/2026-10-03-ha-toolkit-core.md`) is shipped; this builds on its modules.

## Global Constraints

- Work from `/home/chris/Repos/luma-homeops`, directly on `main` (Chris's preference). Commit with `nix develop --command git commit …`; never `--no-verify`. Trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Tests: `nix develop --command python -m pytest -q tools/hactl` (single file: append `/tests/<file>.py`). Live commands: `nix develop --command hactl …` with `export KUBECONFIG=/tmp/galaxy-kubeconfig`.
- Manifests live in `cluster/home-assistant/state/`: `areas.yaml`, `devices.yaml`, `entities.yaml`, `helpers.yaml`, `integrations.yaml`, `dashboards.yaml`, and `credentials.yaml` (git-crypt via an explicit `.gitattributes` line — never `*.secret.yaml`, so `sign.sh` ignores it). They are not kustomize inputs and are not mounted into the pod.
- Matching keys: floors/labels/areas by `id` (= HA's slug of the name at creation, e.g. `Hactl Probe` → `hactl_probe`); devices by one `identifiers` or `connections` pair; entities by `platform` + `unique_id`; helpers/integrations by `domain` + `title`; dashboards by `url_path`; resources by `url`.
- Fully managed (omitted field = empty; undeclared live object = delete **only with `--prune`**): floors, labels, areas, helpers, storage dashboards, resources. Overrides-only (only listed objects and listed fields): devices, entities. Integrations: presence and declared options only; **never deleted** by hactl.
- Built-in dashboards `lovelace` and `map` are never created or deleted. YAML dashboards (`home-ops`) belong to the chart, not `dashboards.yaml`.
- `plan` exit codes: 0 in sync, 2 changes, 1 error. `apply`: 0 converged, 1 otherwise.
- Never print secrets: `credentials.yaml` contents and declared flow answers never appear in output (plan `--json` omits `data`).
- No ad-hoc registry/config-entry writes: every `.storage` change goes through a manifest + `hactl apply`.
- Python modules import `websockets`/`playwright` only inside functions (CI installs `pyyaml pytest`).

## Review Focus

1. **A mistake in a manifest** (unknown key, an area pointing at an undeclared floor, a bad device match) → one error listing every problem, before any API call; never a traceback or a half-applied change. Tests: Task 1 `test_all_problems_reported_together`, Task 8 `test_converge_refuses_an_invalid_manifest_before_touching_ha`.
2. **HA rejecting one change mid-apply** (a flow answer it doesn't accept, a registry error) → that change is reported as failed, the rest still apply, exit non-zero, and no config flow is left half-open. Tests: Task 2 `test_failed_answer_aborts_the_flow`, Task 6 `test_one_failure_does_not_stop_the_rest`.
3. **Running `hactl import` over existing manifests** → refuses without `--force`; `credentials.yaml` is never overwritten. Tests: Task 7 `test_import_refuses_to_overwrite`, `test_credentials_never_overwritten`.
4. **Things HA grows on its own** (an integration added in the UI, a new storage dashboard) → reported by `plan`; integrations are never deleted; other undeclared objects are deleted only with `--prune`. Tests: Task 5 `test_undeclared_integration_is_manual_never_deleted`, Task 6 `test_deletes_only_with_prune`.
5. **`credentials.yaml` still git-crypt-locked** (CI, a fresh clone) → lint passes (references unchecked); a create that needs credentials becomes a clear manual item. Tests: Task 1 `test_locked_credentials_skip_reference_checks`, Task 5 `test_create_needing_locked_credentials_is_manual`.

---

### Task 1: Manifest model and validation

**Files:**
- Create: `tools/hactl/src/hactl/state/__init__.py`, `tools/hactl/src/hactl/state/model.py`
- Modify: `.gitattributes` (add the credentials line)
- Test: `tools/hactl/tests/test_state_model.py`

**Interfaces:**
- Consumes: `hactl.paths.HA_DIR`, `hactl.errors.HactlError`.
- Produces: `model.STATE_DIR`, `model.CREDENTIALS = "credentials.yaml"`, `model.HELPER_DOMAINS`, `model.BUILTIN_DASHBOARDS`, `model.RESOURCE_TYPES`, `model.SCHEMA`, `model.Manifest` (fields `floors, labels, areas, devices, entities, remove, helpers, integrations, dashboards, resources` — lists of dicts — plus `credentials: dict`, `credentials_locked: bool`), `model.load(state_dir) -> Manifest` (raises `HactlError("state manifests have problems:\n  …")`), `model.check(m) -> list[str]`.

- [ ] **Step 1: Write the failing tests** `tools/hactl/tests/test_state_model.py`:

```python
import pytest

from hactl.errors import HactlError
from hactl.state import model


def write(d, files):
    for name, text in files.items():
        (d / name).write_text(text)
    return d


GOOD = {
    "areas.yaml": (
        "floors:\n  - {id: main, name: Main, level: 0}\n"
        "labels:\n  - {id: lighting, name: Lighting, color: amber}\n"
        "areas:\n  - {id: bedroom, name: Bedroom, floor: main, icon: mdi:bed, labels: [lighting]}\n"
    ),
    "devices.yaml": "devices:\n  - match: {identifiers: [hue, abc]}\n    area: bedroom\n    about: Dresser Lamp\n",
    "entities.yaml": (
        "entities:\n  - match: {platform: mqtt, unique_id: '0x1_switch'}\n    entity_id: switch.bedroom_light_switch\n    hidden: false\n"
        "remove:\n  - {platform: mail_and_packages, unique_id: old_1}\n"
    ),
    "helpers.yaml": (
        "helpers:\n  - domain: group\n    title: Bedroom Blinds\n    create: {menu: [cover], answers: {name: Bedroom Blinds}}\n"
        "    options: {entities: [cover.window_left], hide_members: false}\n"
    ),
    "integrations.yaml": (
        "integrations:\n  - {domain: hue, title: Hue Bridge, manual: press the link button}\n"
        "  - domain: airnow\n    title: AirNow\n    create: {answers: {radius: 150}}\n    credentials: airnow\n"
    ),
    "dashboards.yaml": (
        "dashboards:\n  - {url_path: claude-preview, title: Claude Preview, require_admin: true, show_in_sidebar: false}\n"
        "  - {url_path: map, title: Map}\n"
        "resources:\n  - {url: /local/x.js, type: module}\n"
    ),
    "credentials.yaml": "airnow: {api_key: not-a-real-key}\n",
}


def test_empty_dir_is_an_empty_manifest(tmp_path):
    m = model.load(tmp_path)
    assert m.areas == [] and m.integrations == [] and m.credentials == {} and not m.credentials_locked


def test_good_manifests_load(tmp_path):
    m = model.load(write(tmp_path, GOOD))
    assert [a["id"] for a in m.areas] == ["bedroom"]
    assert m.devices[0]["match"] == {"identifiers": ["hue", "abc"]}
    assert m.remove == [{"platform": "mail_and_packages", "unique_id": "old_1"}]
    assert m.credentials["airnow"]["api_key"] == "not-a-real-key"


def test_all_problems_reported_together(tmp_path):
    bad = dict(GOOD)
    bad["areas.yaml"] = "floors: []\nlabels: []\nareas:\n  - {id: bedroom, name: Bedroom, floor: upstairs, colour: red}\nrooms: []\n"
    bad["devices.yaml"] = "devices:\n  - match: {identifiers: [hue]}\n    area: garage\n    labels: [lighting]\n"
    bad["dashboards.yaml"] = "dashboards:\n  - {url_path: preview, title: P}\nresources:\n  - {url: /x.js, type: script}\n"
    with pytest.raises(HactlError) as e:
        model.load(write(tmp_path, bad))
    msg = str(e.value)
    for needle in ["unknown section 'rooms'", "unknown key(s) colour", "floor 'upstairs' is not declared",
                   "match must be", "area 'garage' is not declared", "url_path must contain a hyphen",
                   "type must be one of", "label(s) lighting not declared"]:
        assert needle in msg, needle


def test_entity_in_both_lists_and_wrong_file(tmp_path):
    bad = dict(GOOD)
    bad["entities.yaml"] = (
        "entities:\n  - match: {platform: mqtt, unique_id: u1}\n    name: X\n"
        "remove:\n  - {platform: mqtt, unique_id: u1}\n"
    )
    bad["integrations.yaml"] = "integrations:\n  - {domain: group, title: G, manual: x}\n"
    with pytest.raises(HactlError) as e:
        model.load(write(tmp_path, bad))
    assert "mqtt/u1 is declared more than once" in str(e.value)
    assert "belongs in helpers.yaml" in str(e.value)


def test_missing_credentials_reference(tmp_path):
    bad = dict(GOOD, **{"credentials.yaml": "other: {x: 1}\n"})
    with pytest.raises(HactlError, match="credentials 'airnow' not in credentials.yaml"):
        model.load(write(tmp_path, bad))


def test_locked_credentials_skip_reference_checks(tmp_path):
    write(tmp_path, {k: v for k, v in GOOD.items() if k != "credentials.yaml"})
    (tmp_path / "credentials.yaml").write_bytes(b"\x00GITCRYPT\x00ciphertext")
    m = model.load(tmp_path)
    assert m.credentials_locked and m.credentials == {}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_state_model.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'hactl.state'`.

- [ ] **Step 3: Implement.** `tools/hactl/src/hactl/state/__init__.py`:

```python
"""Declarative Home Assistant state: the .storage parts of HA described in
cluster/home-assistant/state/*.yaml, diffed (`hactl plan`) and converged
(`hactl apply`) through HA's own APIs. Spec §4.11; runbook docs/ha.md."""
```

`tools/hactl/src/hactl/state/model.py`:

```python
"""Load and validate the state manifests in cluster/home-assistant/state/."""
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from hactl import paths
from hactl.errors import HactlError

STATE_DIR = paths.HA_DIR / "state"
CREDENTIALS = "credentials.yaml"

# Config-entry domains that are helpers (fully managed, prunable). Every other
# config entry is an integration (presence + declared options, never deleted).
HELPER_DOMAINS = frozenset({
    "derivative", "filter", "generic_hygrostat", "generic_thermostat", "group", "history_stats",
    "integration", "min_max", "mold_indicator", "random", "statistics", "switch_as_x",
    "template", "threshold", "tod", "trend", "utility_meter",
})
# Dashboards HA ships: updatable, never created or deleted by hactl.
BUILTIN_DASHBOARDS = frozenset({"lovelace", "map"})
RESOURCE_TYPES = frozenset({"module", "js", "css", "html"})

# file -> section -> (required keys, optional keys)
SCHEMA = {
    "areas": {
        "floors": ({"id", "name"}, {"level", "icon", "aliases"}),
        "labels": ({"id", "name"}, {"color", "icon", "description"}),
        "areas": ({"id", "name"}, {"floor", "icon", "labels", "aliases"}),
    },
    "devices": {"devices": ({"match"}, {"about", "name", "area", "labels", "disabled"})},
    "entities": {
        "entities": ({"match"}, {"about", "entity_id", "name", "icon", "area", "labels", "hidden", "disabled"}),
        "remove": ({"platform", "unique_id"}, {"about"}),
    },
    "helpers": {"helpers": ({"domain", "title", "create"}, {"options", "credentials"})},
    "integrations": {"integrations": ({"domain", "title"}, {"create", "options", "credentials", "manual"})},
    "dashboards": {
        "dashboards": ({"url_path", "title"}, {"icon", "require_admin", "show_in_sidebar"}),
        "resources": ({"url", "type"}, set()),
    },
}


@dataclass
class Manifest:
    floors: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    areas: list = field(default_factory=list)
    devices: list = field(default_factory=list)
    entities: list = field(default_factory=list)
    remove: list = field(default_factory=list)
    helpers: list = field(default_factory=list)
    integrations: list = field(default_factory=list)
    dashboards: list = field(default_factory=list)
    resources: list = field(default_factory=list)
    credentials: dict = field(default_factory=dict)
    credentials_locked: bool = False


def _load_credentials(path: Path, problems: list):
    if not path.exists():
        return {}, False
    raw = path.read_bytes()
    if raw.startswith(b"\x00GITCRYPT"):
        return {}, True  # CI / fresh clone: references can't be checked
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        problems.append(f"{CREDENTIALS}: {e}")
        return {}, False
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        problems.append(f"{CREDENTIALS}: expected a mapping of name -> mapping of flow answers")
        return {}, False
    return data, False


def load(state_dir: Path = STATE_DIR) -> Manifest:
    sections, problems = {}, []
    for name, file_sections in SCHEMA.items():
        path = state_dir / f"{name}.yaml"
        data = {}
        if path.exists():
            try:
                data = yaml.safe_load(path.read_text()) or {}
            except yaml.YAMLError as e:
                problems.append(f"{name}.yaml: {e}")
            if not isinstance(data, dict):
                problems.append(f"{name}.yaml: expected a mapping")
                data = {}
        for key in data:
            if key not in file_sections:
                problems.append(f"{name}.yaml: unknown section {key!r} (expected: {', '.join(file_sections)})")
        for section, (required, optional) in file_sections.items():
            items = data.get(section) or []
            if not isinstance(items, list):
                problems.append(f"{name}.yaml: {section} must be a list")
                items = []
            good = []
            for i, item in enumerate(items):
                where = f"{name}.yaml {section}[{i}]"
                if not isinstance(item, dict):
                    problems.append(f"{where}: expected a mapping")
                    continue
                if missing := sorted(required - item.keys()):
                    problems.append(f"{where}: missing {', '.join(missing)}")
                if unknown := sorted(item.keys() - required - optional):
                    problems.append(f"{where}: unknown key(s) {', '.join(unknown)}")
                good.append(item)
            sections[section] = good
    creds, locked = _load_credentials(state_dir / CREDENTIALS, problems)
    m = Manifest(**sections, credentials=creds, credentials_locked=locked)
    problems += check(m)
    if problems:
        raise HactlError("state manifests have problems:\n  " + "\n  ".join(problems))
    return m


def _dupes(values) -> list:
    seen, out = set(), set()
    for v in values:
        (out if v in seen else seen).add(v)
    return sorted(out, key=str)


def _strlist(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _name(x) -> str:
    return str(x.get("id") or x.get("about") or x.get("match"))


def _device_key(x):
    mt = x.get("match")
    if isinstance(mt, dict) and len(mt) == 1:
        (kind, pair), = mt.items()
        if kind in ("identifiers", "connections") and _strlist(pair) and len(pair) == 2:
            return (kind, *pair)
    return None


def check(m: Manifest) -> list:
    p = []
    floor_ids = {f.get("id") for f in m.floors}
    label_ids = {lab.get("id") for lab in m.labels}
    area_ids = {a.get("id") for a in m.areas}
    for kind, items in (("floor", m.floors), ("label", m.labels), ("area", m.areas)):
        p += [f"duplicate {kind} id {d!r}" for d in _dupes([x.get("id") for x in items])]
    for a in m.areas:
        if a.get("floor") is not None and a["floor"] not in floor_ids:
            p.append(f"area {_name(a)}: floor {a['floor']!r} is not declared")
    for kind, items in (("area", m.areas), ("device", m.devices), ("entity", m.entities)):
        for x in items:
            labels = x.get("labels", [])
            if not _strlist(labels):
                p.append(f"{kind} {_name(x)}: labels must be a list of label ids")
            elif missing := sorted(set(labels) - label_ids):
                p.append(f"{kind} {_name(x)}: label(s) {', '.join(missing)} not declared")
    for kind, items in (("device", m.devices), ("entity", m.entities)):
        for x in items:
            if x.get("area") is not None and x["area"] not in area_ids:
                p.append(f"{kind} {_name(x)}: area {x['area']!r} is not declared")
            for flag in ("disabled", "hidden"):
                if flag in x and not isinstance(x[flag], bool):
                    p.append(f"{kind} {_name(x)}: {flag} must be true or false")
    device_keys = []
    for x in m.devices:
        key = _device_key(x)
        if key is None:
            p.append(f"device {_name(x)}: match must be {{identifiers: [domain, id]}} or {{connections: [type, value]}}")
        else:
            device_keys.append(key)
    p += [f"device {'/'.join(d)} is declared twice" for d in _dupes(device_keys)]
    entity_keys = []
    for x in m.entities:
        mt = x.get("match")
        if isinstance(mt, dict) and set(mt) == {"platform", "unique_id"} and all(isinstance(v, str) for v in mt.values()):
            entity_keys.append((mt["platform"], mt["unique_id"]))
        else:
            p.append(f"entity {_name(x)}: match must be {{platform: ..., unique_id: '...'}} (quote numeric ids)")
    for r in m.remove:
        if isinstance(r.get("platform"), str) and isinstance(r.get("unique_id"), str):
            entity_keys.append((r["platform"], r["unique_id"]))
        else:
            p.append(f"remove {r}: platform and unique_id must be strings (quote numeric ids)")
    p += [f"entity {d[0]}/{d[1]} is declared more than once (entities/remove)" for d in _dupes(entity_keys)]
    for kind, items in (("helper", m.helpers), ("integration", m.integrations)):
        p += [f"{kind} {d[0]}/{d[1]} is declared twice" for d in _dupes([(x.get("domain"), x.get("title")) for x in items])]
        for x in items:
            who = f"{kind} {x.get('domain')}/{x.get('title')}"
            if (x.get("domain") in HELPER_DOMAINS) != (kind == "helper"):
                p.append(f"{who}: belongs in {'helpers' if x.get('domain') in HELPER_DOMAINS else 'integrations'}.yaml")
            c = x.get("create")
            if c is not None and not (isinstance(c, dict) and set(c) <= {"menu", "answers"}
                                      and _strlist(c.get("menu", [])) and isinstance(c.get("answers", {}), dict)):
                p.append(f"{who}: create must be {{menu: [step ids], answers: {{field: value}}}}")
            if "options" in x and not isinstance(x["options"], dict):
                p.append(f"{who}: options must be a mapping")
            cred = x.get("credentials")
            if cred is not None and not m.credentials_locked and cred not in m.credentials:
                p.append(f"{who}: credentials {cred!r} not in {CREDENTIALS}")
    p += [f"dashboard {d!r} is declared twice" for d in _dupes([x.get("url_path") for x in m.dashboards])]
    for x in m.dashboards:
        u = str(x.get("url_path", ""))
        if u not in BUILTIN_DASHBOARDS and "-" not in u:
            p.append(f"dashboard {u!r}: url_path must contain a hyphen (HA rejects others)")
    p += [f"resource {d!r} is declared twice" for d in _dupes([x.get("url") for x in m.resources])]
    for x in m.resources:
        if x.get("type") not in RESOURCE_TYPES:
            p.append(f"resource {x.get('url')!r}: type must be one of {', '.join(sorted(RESOURCE_TYPES))}")
    return p
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass (plan-1 tests included).

- [ ] **Step 5: git-crypt the credentials file before it can exist.** Append to `.gitattributes`:

```
cluster/home-assistant/state/credentials.yaml filter=git-crypt diff=git-crypt
```

Run: `git check-attr filter -- cluster/home-assistant/state/credentials.yaml`
Expected: `cluster/home-assistant/state/credentials.yaml: filter: git-crypt`.

- [ ] **Step 6: Commit**

```bash
git add .gitattributes tools/hactl/src/hactl/state tools/hactl/tests/test_state_model.py
nix develop --command git commit -m "hactl state: manifest model and validation; git-crypt credentials.yaml" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Config and options flow driver

**Files:**
- Create: `tools/hactl/src/hactl/state/flows.py`
- Test: `tools/hactl/tests/test_state_flows.py`

**Interfaces:**
- Consumes: `client.post(path, data)`, `client.rest("DELETE", path)`; `HactlError`.
- Produces: `flows.CONFIG_FLOW`, `flows.OPTIONS_FLOW`, `flows.FlowManual(HactlError)`, `flows.field_names(step)`, `flows.current_values(step) -> dict`, `flows.next_payload(step, answers, menu, keep_current=False) -> dict`, `flows.run_config_flow(client, domain, answers, menu=()) -> dict` (the `create_entry` step), `flows.run_options_flow(client, entry_id, options) -> dict`, `flows.read_options(client, entry_id) -> {"step_id", "values"} | None`. Every path that fails aborts the flow (`DELETE …/flow/<id>`).
- Facts (probed live 2026-10-03): options flows report current values as `description.suggested_value` (group) or `default` (hue); the group helper's config flow starts with a `menu` step (`menu_options: [binary_sensor, …, cover, …]`); `switch_as_x`'s `user` form asks `entity_id`, `invert` (default false), `target_domain`; `DELETE` returns `{"message": "Flow aborted"}`.

- [ ] **Step 1: Write the failing tests** `tools/hactl/tests/test_state_flows.py`:

```python
import pytest

from hactl.errors import HactlError
from hactl.state import flows

GROUP_MENU = {"type": "menu", "flow_id": "f1", "step_id": "user", "menu_options": ["light", "cover"]}
GROUP_FORM = {"type": "form", "flow_id": "f1", "step_id": "cover", "errors": {}, "data_schema": [
    {"name": "name", "required": True},
    {"name": "entities", "required": True},
    {"name": "hide_members", "required": True, "default": False},
]}
CREATED = {"type": "create_entry", "flow_id": "f1", "title": "Bedroom Blinds", "result": {"entry_id": "e1"}}
OPTIONS_FORM = {"type": "form", "flow_id": "o1", "step_id": "cover", "data_schema": [
    {"name": "entities", "required": True, "description": {"suggested_value": ["cover.a", "cover.b"]}},
    {"name": "hide_members", "required": True, "default": False, "description": {"suggested_value": False}},
]}


class FakeFlow:
    def __init__(self, steps):
        self.steps, self.posts, self.deleted = list(steps), [], []

    def post(self, path, data=None, raw=False):
        self.posts.append((path, data))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def rest(self, method, path, data=None, raw=False, timeout=30):
        assert method == "DELETE"
        self.deleted.append(path)
        return {"message": "Flow aborted"}


def test_current_values_prefers_suggested_value():
    assert flows.current_values(OPTIONS_FORM) == {"entities": ["cover.a", "cover.b"], "hide_members": False}


def test_group_helper_via_menu_then_form():
    fake = FakeFlow([GROUP_MENU, GROUP_FORM, CREATED])
    out = flows.run_config_flow(fake, "group", {"name": "Bedroom Blinds", "entities": ["cover.a"], "unrelated": 1}, ["cover"])
    assert out["title"] == "Bedroom Blinds"
    assert fake.posts == [
        (flows.CONFIG_FLOW, {"handler": "group", "show_advanced_options": True}),
        (f"{flows.CONFIG_FLOW}/f1", {"next_step_id": "cover"}),
        (f"{flows.CONFIG_FLOW}/f1", {"name": "Bedroom Blinds", "entities": ["cover.a"]}),
    ]
    assert fake.deleted == []


def test_missing_answer_names_the_field_and_aborts():
    fake = FakeFlow([GROUP_MENU, GROUP_FORM])
    with pytest.raises(HactlError, match="needs entities"):
        flows.run_config_flow(fake, "group", {"name": "X"}, ["cover"])
    assert fake.deleted == [f"{flows.CONFIG_FLOW}/f1"]


def test_menu_without_declared_choice_aborts():
    fake = FakeFlow([GROUP_MENU])
    with pytest.raises(HactlError, match="declare create.menu"):
        flows.run_config_flow(fake, "group", {})
    assert fake.deleted == [f"{flows.CONFIG_FLOW}/f1"]


def test_failed_answer_aborts_the_flow():
    rejected = dict(GROUP_FORM, errors={"base": "invalid_entity"})
    fake = FakeFlow([GROUP_MENU, GROUP_FORM, rejected])
    with pytest.raises(HactlError, match="rejected the answers"):
        flows.run_config_flow(fake, "group", {"name": "X", "entities": ["cover.nope"]}, ["cover"])
    assert fake.deleted == [f"{flows.CONFIG_FLOW}/f1"]


def test_http_error_mid_flow_aborts():
    fake = FakeFlow([GROUP_MENU, HactlError("POST -> HTTP 400: bad data")])
    with pytest.raises(HactlError, match="HTTP 400"):
        flows.run_config_flow(fake, "group", {}, ["cover"])
    assert fake.deleted == [f"{flows.CONFIG_FLOW}/f1"]


def test_interactive_step_is_manual():
    fake = FakeFlow([{"type": "external", "flow_id": "f9", "step_id": "auth"}])
    with pytest.raises(flows.FlowManual):
        flows.run_config_flow(fake, "plex", {})
    assert fake.deleted == [f"{flows.CONFIG_FLOW}/f9"]


def test_options_flow_keeps_current_values_and_overrides_declared():
    fake = FakeFlow([OPTIONS_FORM, {"type": "create_entry", "flow_id": "o1"}])
    flows.run_options_flow(fake, "e1", {"hide_members": True})
    assert fake.posts[1] == (f"{flows.OPTIONS_FLOW}/o1", {"entities": ["cover.a", "cover.b"], "hide_members": True})


def test_read_options_aborts_and_tolerates_no_options_flow():
    fake = FakeFlow([OPTIONS_FORM])
    assert flows.read_options(fake, "e1") == {"step_id": "cover", "values": {"entities": ["cover.a", "cover.b"], "hide_members": False}}
    assert fake.deleted == [f"{flows.OPTIONS_FLOW}/o1"]
    assert flows.read_options(FakeFlow([HactlError("HTTP 400")]), "e2") is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_state_flows.py`
Expected: FAIL — `ImportError: cannot import name 'flows'`.

- [ ] **Step 3: Implement `tools/hactl/src/hactl/state/flows.py`:**

```python
"""Drive Home Assistant config and options flows from declared answers.

A flow is a sequence of steps: `menu` (pick next_step_id), `form` (submit
fields), `create_entry` (done), `abort`, or an interactive step that needs a
person (`external`, `progress`). Any failure aborts the flow so nothing is
left half-open in HA.
"""
from hactl.errors import HactlError

CONFIG_FLOW = "/api/config/config_entries/flow"
OPTIONS_FLOW = "/api/config/config_entries/options/flow"
MAX_STEPS = 12
INTERACTIVE = frozenset({"external", "external_done", "progress", "progress_done", "show_progress"})


class FlowManual(HactlError):
    """The flow needs a person (link button, OAuth sign-in, approval)."""


def field_names(step) -> list:
    return [f["name"] for f in step.get("data_schema") or []]


def current_values(step) -> dict:
    out = {}
    for f in step.get("data_schema") or []:
        desc = f.get("description") or {}
        if "suggested_value" in desc:
            out[f["name"]] = desc["suggested_value"]
        elif "default" in f:
            out[f["name"]] = f["default"]
    return out


def next_payload(step, answers: dict, menu: list, keep_current: bool = False) -> dict:
    kind, sid = step.get("type"), step.get("step_id")
    if kind == "menu":
        options = step.get("menu_options") or []
        if not menu:
            raise HactlError(f"flow step {sid!r} is a menu {options}; declare create.menu")
        choice = menu.pop(0)
        if choice not in options:
            raise HactlError(f"flow step {sid!r}: menu choice {choice!r} is not one of {options}")
        return {"next_step_id": choice}
    if kind == "form":
        if step.get("errors"):
            raise HactlError(f"HA rejected the answers at step {sid!r}: {step['errors']}")
        names = field_names(step)
        payload = current_values(step) if keep_current else {}
        payload.update({k: v for k, v in answers.items() if k in names})
        missing = [f["name"] for f in step.get("data_schema") or []
                   if f.get("required") and f["name"] not in payload and "default" not in f]
        if missing:
            raise HactlError(f"flow step {sid!r} needs {', '.join(missing)}: add them to the declared answers")
        return payload
    if kind in INTERACTIVE:
        raise FlowManual(f"flow step {sid!r} needs a person (HA: Settings -> Devices & services)")
    if kind == "abort":
        raise HactlError(f"HA aborted the flow: {step.get('reason')}")
    raise HactlError(f"unexpected flow step type {kind!r}")


def _abort(client, base, flow_id) -> None:
    if flow_id:
        try:
            client.rest("DELETE", f"{base}/{flow_id}")
        except HactlError:
            pass  # already finished or gone


def _drive(client, base, step, answers, menu, keep_current) -> dict:
    menu = list(menu)
    flow_id = step.get("flow_id")
    try:
        for _ in range(MAX_STEPS):
            if step.get("type") == "create_entry":
                return step
            payload = next_payload(step, answers, menu, keep_current)
            step = client.post(f"{base}/{flow_id}", payload)
        raise HactlError(f"flow did not finish within {MAX_STEPS} steps")
    except HactlError:
        _abort(client, base, flow_id)
        raise


def run_config_flow(client, domain, answers, menu=()) -> dict:
    step = client.post(CONFIG_FLOW, {"handler": domain, "show_advanced_options": True})
    return _drive(client, CONFIG_FLOW, step, answers, menu, keep_current=False)


def run_options_flow(client, entry_id, options) -> dict:
    step = client.post(OPTIONS_FLOW, {"handler": entry_id, "show_advanced_options": True})
    return _drive(client, OPTIONS_FLOW, step, options, (), keep_current=True)


def read_options(client, entry_id):
    """Current options from the options flow's first form, or None. Always aborts the flow."""
    try:
        step = client.post(OPTIONS_FLOW, {"handler": entry_id, "show_advanced_options": True})
    except HactlError:
        return None  # this entry has no options flow
    try:
        if step.get("type") != "form":
            return None
        return {"step_id": step.get("step_id"), "values": current_values(step)}
    finally:
        _abort(client, OPTIONS_FLOW, step.get("flow_id"))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass.

- [ ] **Step 5: Live check (read-only; the options flow is aborted).**

Run: `nix develop --command bash -c 'cd tools/hactl && PYTHONPATH=src python3 -c "
from hactl.client import Client
from hactl.state import flows
c = Client()
e = next(x for x in c.ws({\"type\": \"config_entries/get\"})[0] if x[\"domain\"] == \"group\")
print(flows.read_options(c, e[\"entry_id\"]))
print(\"in progress:\", [f[\"handler\"] for f in c.ws({\"type\": \"config_entries/flow/progress\"})[0]])"'`
Expected: `{'step_id': 'cover', 'values': {'entities': ['cover.window_left', 'cover.window_right'], 'hide_members': False}}` and `in progress: []`.

- [ ] **Step 6: Commit**

```bash
git add tools/hactl/src/hactl/state/flows.py tools/hactl/tests/test_state_flows.py
nix develop --command git commit -m "hactl state: config/options flow driver (always aborts on failure)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Live snapshot and shared test fixtures

**Files:**
- Create: `tools/hactl/src/hactl/state/live.py`
- Create: `tools/hactl/tests/state_fixtures.py` (shared by Tasks 3–9)
- Test: `tools/hactl/tests/test_state_live.py`

**Interfaces:**
- Consumes: `client.ws(*commands)`, `flows.read_options`.
- Produces: `live.Snapshot` (fields `floors, labels, areas, devices, entities, entries, dashboards, resources` — lists of HA dicts — and `options: dict[(domain, title)] -> {"step_id", "values"} | None`); `live.fetch(client, want_options=frozenset()) -> Snapshot` (reads options only for wanted keys). Test helpers: `state_fixtures.snapshot() -> Snapshot`, `state_fixtures.FakeHA(snapshot)` (in-memory registries, devices, entities, config entries, dashboards, resources; `post` raises `HactlError` = no flows; `rest("DELETE", "/api/config/config_entries/entry/<id>")` removes an entry), `state_fixtures.write_manifests(dir, files: dict[str, str])`.

- [ ] **Step 1: Create `tools/hactl/tests/state_fixtures.py`:**

```python
"""Shared fixtures for the state tests: a realistic live snapshot and an in-memory HA."""
import re

from hactl.errors import HactlError
from hactl.state.live import Snapshot


def snapshot() -> Snapshot:
    return Snapshot(
        floors=[],
        labels=[],
        areas=[
            {"area_id": "bedroom", "name": "Bedroom", "floor_id": None, "icon": "mdi:bed", "labels": [], "aliases": []},
            {"area_id": "kitchen", "name": "Kitchen", "floor_id": None, "icon": "mdi:fridge", "labels": [], "aliases": []},
        ],
        devices=[
            {"id": "d1", "identifiers": [["hue", "lamp-1"]], "connections": [], "name": "Dresser Lamp", "name_by_user": None,
             "area_id": "bedroom", "labels": [], "disabled_by": None, "manufacturer": "Signify", "model": "Hue color lamp"},
            {"id": "d2", "identifiers": [["mqtt", "zigbee2mqtt_0x1"]], "connections": [], "name": "0x1", "name_by_user": "Bedroom Light Switch",
             "area_id": None, "labels": [], "disabled_by": None, "manufacturer": "Leviton", "model": "DG15S"},
            {"id": "d3", "identifiers": [], "connections": [["mac", "aa:bb"]], "name": "Router", "name_by_user": None,
             "area_id": None, "labels": [], "disabled_by": None, "manufacturer": None, "model": None},
        ],
        entities=[
            {"entity_id": "switch.bedroom_bedroom_light_switch", "platform": "mqtt", "unique_id": "0x1_switch", "name": None,
             "icon": None, "area_id": None, "labels": [], "hidden_by": None, "disabled_by": None},
            {"entity_id": "sensor.old_mail", "platform": "mail_and_packages", "unique_id": "old_1", "name": None,
             "icon": None, "area_id": None, "labels": [], "hidden_by": None, "disabled_by": None},
            {"entity_id": "sensor.diag", "platform": "hue", "unique_id": "diag_1", "name": None,
             "icon": None, "area_id": None, "labels": [], "hidden_by": None, "disabled_by": "integration"},
        ],
        entries=[
            {"entry_id": "e1", "domain": "group", "title": "Bedroom Blinds", "state": "loaded", "reason": None, "disabled_by": None},
            {"entry_id": "e2", "domain": "hue", "title": "Hue Bridge", "state": "loaded", "reason": None, "disabled_by": None},
            {"entry_id": "e3", "domain": "plex", "title": "Plex", "state": "setup_retry", "reason": "timeout", "disabled_by": None},
        ],
        dashboards=[
            {"id": "lovelace", "url_path": "lovelace", "title": "Overview", "icon": "mdi:view-dashboard", "mode": "storage",
             "require_admin": False, "show_in_sidebar": True},
            {"url_path": "home-ops", "title": "Home", "icon": "mdi:home", "mode": "yaml", "filename": "dashboards/overview.yaml",
             "require_admin": False, "show_in_sidebar": True},
            {"id": "claude_preview", "url_path": "claude-preview", "title": "Claude Preview", "icon": "mdi:flask-outline",
             "mode": "storage", "require_admin": True, "show_in_sidebar": False},
        ],
        resources=[{"id": "r1", "url": "/hacsfiles/mushroom.js", "type": "module"}],
        options={("group", "Bedroom Blinds"): {"step_id": "cover",
                                               "values": {"entities": ["cover.window_left", "cover.window_right"], "hide_members": False}}},
    )


def write_manifests(d, files: dict):
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text)
    return d


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class FakeHA:
    """In-memory HA: registries, devices, entities, config entries, dashboards, resources. No flows."""

    REGISTRIES = {"config/floor_registry": ("floors", "floor_id"), "config/label_registry": ("labels", "label_id"),
                  "config/area_registry": ("areas", "area_id")}

    def __init__(self, snap):
        for name in ("floors", "labels", "areas", "devices", "entities", "entries", "dashboards", "resources"):
            setattr(self, name, [dict(x) for x in getattr(snap, name)])
        self.calls = []

    def ws(self, *commands):
        return [self._one(c) for c in commands]

    def _one(self, c):
        t = c["type"]
        self.calls.append(t)
        args = {k: v for k, v in c.items() if k != "type"}
        for base, (attr, idk) in self.REGISTRIES.items():
            items = getattr(self, attr)
            if t == f"{base}/list":
                return [dict(x) for x in items]
            if t == f"{base}/create":
                x = {idk: _slug(args["name"]), **args}
                items.append(x)
                return dict(x)
            if t == f"{base}/update":
                x = next(i for i in items if i[idk] == args[idk])
                x.update(args)
                return dict(x)
            if t == f"{base}/delete":
                items[:] = [i for i in items if i[idk] != args[idk]]
                return None
        if t == "config/device_registry/list":
            return [dict(x) for x in self.devices]
        if t == "config/device_registry/update":
            x = next(d for d in self.devices if d["id"] == args.pop("device_id"))
            x.update(args)
            return dict(x)
        if t == "config/entity_registry/list":
            return [dict(x) for x in self.entities]
        if t == "config/entity_registry/update":
            x = next(e for e in self.entities if e["entity_id"] == args.pop("entity_id"))
            new_id = args.pop("new_entity_id", None)
            x.update(args)
            if new_id:
                x["entity_id"] = new_id
            return dict(x)
        if t == "config/entity_registry/remove":
            self.entities = [e for e in self.entities if e["entity_id"] != args["entity_id"]]
            return None
        if t == "config_entries/get":
            return [dict(x) for x in self.entries]
        if t == "lovelace/dashboards/list":
            return [dict(x) for x in self.dashboards]
        if t == "lovelace/dashboards/create":
            x = {"id": args["url_path"].replace("-", "_"), **args}
            self.dashboards.append(x)
            return dict(x)
        if t == "lovelace/dashboards/update":
            x = next(d for d in self.dashboards if d.get("id") == args.pop("dashboard_id"))
            x.update(args)
            return dict(x)
        if t == "lovelace/dashboards/delete":
            self.dashboards = [d for d in self.dashboards if d.get("id") != args["dashboard_id"]]
            return None
        if t == "lovelace/resources":
            return [dict(x) for x in self.resources]
        if t == "lovelace/resources/create":
            x = {"id": f"r{len(self.resources) + 100}", "type": args["res_type"], "url": args["url"]}
            self.resources.append(x)
            return dict(x)
        if t == "lovelace/resources/update":
            x = next(r for r in self.resources if r["id"] == args["resource_id"])
            x.update({"type": args["res_type"], "url": args["url"]})
            return dict(x)
        if t == "lovelace/resources/delete":
            self.resources = [r for r in self.resources if r["id"] != args["resource_id"]]
            return None
        raise AssertionError(f"FakeHA has no {t}")

    def post(self, path, data=None, raw=False):
        raise HactlError("FakeHA has no flows")

    def rest(self, method, path, data=None, raw=False, timeout=30):
        if method == "DELETE" and path.startswith("/api/config/config_entries/entry/"):
            entry_id = path.rsplit("/", 1)[1]
            self.entries = [e for e in self.entries if e["entry_id"] != entry_id]
            return {"require_restart": False}
        raise HactlError(f"FakeHA has no {method} {path}")
```

- [ ] **Step 2: Write the failing tests** `tools/hactl/tests/test_state_live.py`:

```python
from state_fixtures import FakeHA, snapshot

from hactl.state import live


class Recorder(FakeHA):
    def __init__(self, snap):
        super().__init__(snap)
        self.option_reads = []

    def post(self, path, data=None, raw=False):
        self.option_reads.append(data["handler"])
        return {"type": "form", "flow_id": "o1", "step_id": "cover",
                "data_schema": [{"name": "hide_members", "default": False}]}

    def rest(self, method, path, data=None, raw=False, timeout=30):
        return {"message": "Flow aborted"}


def test_fetch_reads_every_collection():
    snap = live.fetch(FakeHA(snapshot()))
    assert [a["area_id"] for a in snap.areas] == ["bedroom", "kitchen"]
    assert len(snap.devices) == 3 and len(snap.entries) == 3 and len(snap.resources) == 1
    assert snap.options == {}


def test_fetch_reads_options_only_when_wanted():
    fake = Recorder(snapshot())
    snap = live.fetch(fake, want_options={("group", "Bedroom Blinds")})
    assert fake.option_reads == ["e1"]
    assert snap.options[("group", "Bedroom Blinds")] == {"step_id": "cover", "values": {"hide_members": False}}
```

- [ ] **Step 3: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_state_live.py`
Expected: FAIL — `ImportError: cannot import name 'live'` (raised while importing `state_fixtures`).

- [ ] **Step 4: Implement `tools/hactl/src/hactl/state/live.py`:**

```python
"""A snapshot of the .storage parts of HA that the state manifests describe."""
from dataclasses import dataclass, field

from hactl.state import flows


@dataclass
class Snapshot:
    floors: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    areas: list = field(default_factory=list)
    devices: list = field(default_factory=list)
    entities: list = field(default_factory=list)
    entries: list = field(default_factory=list)
    dashboards: list = field(default_factory=list)
    resources: list = field(default_factory=list)
    options: dict = field(default_factory=dict)  # (domain, title) -> {"step_id", "values"} | None


def fetch(client, want_options=frozenset()) -> Snapshot:
    floors, labels, areas, devices, entities, entries, dashboards, resources = client.ws(
        {"type": "config/floor_registry/list"}, {"type": "config/label_registry/list"},
        {"type": "config/area_registry/list"}, {"type": "config/device_registry/list"},
        {"type": "config/entity_registry/list"}, {"type": "config_entries/get"},
        {"type": "lovelace/dashboards/list"}, {"type": "lovelace/resources"},
    )
    options = {}
    for e in entries:
        key = (e["domain"], e["title"])
        if key in want_options:
            options[key] = flows.read_options(client, e["entry_id"])
    return Snapshot(floors, labels, areas, devices, entities, entries, dashboards, resources, options)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass. (`tests/` is already on pytest's rootdir path, so `from state_fixtures import …` resolves.)

- [ ] **Step 6: Commit**

```bash
git add tools/hactl/src/hactl/state/live.py tools/hactl/tests/state_fixtures.py tools/hactl/tests/test_state_live.py
nix develop --command git commit -m "hactl state: live snapshot + in-memory FakeHA for tests" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Diff — registries, devices, entities

**Files:**
- Create: `tools/hactl/src/hactl/state/diff.py`
- Test: `tools/hactl/tests/test_state_diff.py`

**Interfaces:**
- Consumes: `model.BUILTIN_DASHBOARDS`, `model.HELPER_DOMAINS` (Task 5 uses them).
- Produces: `diff.Change(action, kind, key, detail="", data={}, prune=False)` with `str()` (`+`/`~`/`-`/`!` prefix, `(needs --prune)` suffix) and `.public() -> dict` (no `data`); `diff.norm(v)`, `diff.same(a, b)`; `diff.REGISTRY`; `diff.diff_registry(kind, declared, live)`; `diff.find_device(match, devices)`; `diff.diff_devices(declared, devices)`; `diff.diff_entities(declared, remove, entities)`. Change `data` uses HA field names, ready for the API: registry create `{"id", <live fields>}`, update `{<id_key>: id, <changed fields>}`, delete `{<id_key>: id}`; device update `{"device_id", …}`; entity update `{"entity_id", …, "new_entity_id"?}`; entity remove `{"entity_id"}`. Actions: `create`, `update`, `delete` (prune), `remove` (declared, not prune), `manual`.

- [ ] **Step 1: Write the failing tests** `tools/hactl/tests/test_state_diff.py`:

```python
from state_fixtures import snapshot

from hactl.state import diff


def actions(changes):
    return [(c.action, c.kind, c.key) for c in changes]


def test_norm_treats_empty_as_none_and_sorts_lists():
    assert diff.same(None, []) and diff.same("", None)
    assert diff.same(["b", "a"], ["a", "b"])
    assert not diff.same(False, None)  # False is a value, not "empty"


def test_registry_in_sync():
    snap = snapshot()
    declared = [{"id": "bedroom", "name": "Bedroom", "icon": "mdi:bed"}, {"id": "kitchen", "name": "Kitchen", "icon": "mdi:fridge"}]
    assert diff.diff_registry("area", declared, snap.areas) == []


def test_registry_create_update_and_prunable_delete():
    snap = snapshot()
    declared = [{"id": "bedroom", "name": "Bedroom", "icon": "mdi:bed-king"}, {"id": "office", "name": "Office"}]
    changes = diff.diff_registry("area", declared, snap.areas)
    assert actions(changes) == [("update", "area", "bedroom"), ("create", "area", "office"), ("delete", "area", "kitchen")]
    assert changes[0].data == {"area_id": "bedroom", "icon": "mdi:bed-king"}
    assert changes[1].data["id"] == "office" and changes[1].data["name"] == "Office"
    assert changes[2].prune and str(changes[2]).endswith("(needs --prune)")


def test_registry_omitted_field_is_enforced_empty():
    snap = snapshot()
    changes = diff.diff_registry("area", [{"id": "bedroom", "name": "Bedroom"}, {"id": "kitchen", "name": "Kitchen", "icon": "mdi:fridge"}], snap.areas)
    assert [(c.key, c.data) for c in changes] == [("bedroom", {"area_id": "bedroom", "icon": None})]


def test_devices_overrides_only():
    snap = snapshot()
    declared = [
        {"match": {"identifiers": ["hue", "lamp-1"]}, "area": "bedroom", "about": "Dresser Lamp"},   # in sync
        {"match": {"identifiers": ["mqtt", "zigbee2mqtt_0x1"]}, "area": "bedroom", "disabled": False},  # area changes
        {"match": {"connections": ["mac", "aa:bb"]}, "name": "Router"},                               # name_by_user set
        {"match": {"identifiers": ["hue", "gone"]}, "area": "bedroom"},                               # missing
    ]
    changes = diff.diff_devices(declared, snap.devices)
    assert [(c.action, c.data) for c in changes] == [
        ("update", {"device_id": "d2", "area_id": "bedroom"}),
        ("update", {"device_id": "d3", "name_by_user": "Router"}),
        ("manual", {}),
    ]


def test_entities_rename_flags_and_remove():
    snap = snapshot()
    declared = [
        {"match": {"platform": "mqtt", "unique_id": "0x1_switch"}, "entity_id": "switch.bedroom_light_switch", "hidden": True},
        {"match": {"platform": "hue", "unique_id": "diag_1"}, "disabled": True},   # disabled by integration counts as disabled
        {"match": {"platform": "hue", "unique_id": "gone"}, "name": "x"},
    ]
    remove = [{"platform": "mail_and_packages", "unique_id": "old_1"}, {"platform": "x", "unique_id": "already_gone"}]
    changes = diff.diff_entities(declared, remove, snap.entities)
    assert [(c.action, c.key) for c in changes] == [
        ("update", "switch.bedroom_bedroom_light_switch"), ("manual", "hue/gone"), ("remove", "sensor.old_mail")]
    assert changes[0].data == {"entity_id": "switch.bedroom_bedroom_light_switch",
                               "hidden_by": "user", "new_entity_id": "switch.bedroom_light_switch"}
    assert not changes[2].prune


def test_public_view_hides_data():
    c = diff.Change("create", "integration", "airnow/AirNow", data={"answers": {"api_key": "secret"}})
    assert "data" not in c.public() and "secret" not in str(c)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_state_diff.py`
Expected: FAIL — `ImportError: cannot import name 'diff'`.

- [ ] **Step 3: Implement `tools/hactl/src/hactl/state/diff.py`:**

```python
"""Compare declared state (model.Manifest) with live HA (live.Snapshot) -> ordered Changes."""
from dataclasses import dataclass, field

SYMBOL = {"create": "+", "update": "~", "delete": "-", "remove": "-", "manual": "!"}


@dataclass
class Change:
    action: str  # create | update | delete (prune) | remove (declared) | manual
    kind: str
    key: str
    detail: str = ""
    data: dict = field(default_factory=dict)  # API payload; may hold flow answers: never print
    prune: bool = False

    def __str__(self) -> str:
        s = f"{SYMBOL[self.action]} {self.kind} {self.key}"
        if self.detail:
            s += f": {self.detail}"
        return s + (" (needs --prune)" if self.prune else "")

    def public(self) -> dict:
        return {"action": self.action, "kind": self.kind, "key": self.key, "detail": self.detail, "prune": self.prune}


def norm(v):
    if v is None or v == "" or v == []:
        return None
    if isinstance(v, (list, tuple)):
        return sorted(v, key=str)
    return v


def same(a, b) -> bool:
    return norm(a) == norm(b)


def _describe(delta: dict, current: dict) -> str:
    return ", ".join(f"{k} {current.get(k)!r} -> {v!r}" for k, v in delta.items())


# kind -> (HA id field, {manifest field: HA field})
REGISTRY = {
    "floor": ("floor_id", {"name": "name", "level": "level", "icon": "icon", "aliases": "aliases"}),
    "label": ("label_id", {"name": "name", "color": "color", "icon": "icon", "description": "description"}),
    "area": ("area_id", {"name": "name", "floor": "floor_id", "icon": "icon", "labels": "labels", "aliases": "aliases"}),
}


def diff_registry(kind, declared, live) -> list:
    """Fully managed: declared fields enforced (omitted = empty); undeclared live ones are prunable."""
    id_key, fields = REGISTRY[kind]
    by_id = {x[id_key]: x for x in live}
    out = []
    for d in declared:
        want = {lf: d.get(mf) for mf, lf in fields.items()}
        cur = by_id.get(d["id"])
        if cur is None:
            out.append(Change("create", kind, d["id"], repr(d["name"]), data={"id": d["id"], **want}))
            continue
        delta = {lf: v for lf, v in want.items() if not same(v, cur.get(lf))}
        if delta:
            out.append(Change("update", kind, d["id"], _describe(delta, cur), data={id_key: d["id"], **delta}))
    declared_ids = {d["id"] for d in declared}
    out += [Change("delete", kind, x[id_key], f"{x.get('name')!r} is not declared", data={id_key: x[id_key]}, prune=True)
            for x in live if x[id_key] not in declared_ids]
    return out


DEVICE_FIELDS = {"name": "name_by_user", "area": "area_id", "labels": "labels"}
ENTITY_FIELDS = {"name": "name", "icon": "icon", "area": "area_id", "labels": "labels"}


def _override_delta(d, cur, fields) -> dict:
    """Only fields present in the manifest entry are managed."""
    delta = {lf: d[mf] for mf, lf in fields.items() if mf in d and not same(d[mf], cur.get(lf))}
    for mf, lf in (("disabled", "disabled_by"), ("hidden", "hidden_by")):
        if mf in d and d[mf] != (cur.get(lf) is not None):
            delta[lf] = "user" if d[mf] else None
    return delta


def find_device(match, devices):
    (kind, pair), = match.items()
    for dev in devices:
        if list(pair) in [list(x) for x in dev.get(kind) or []]:
            return dev
    return None


def diff_devices(declared, devices) -> list:
    out = []
    for d in declared:
        dev = find_device(d["match"], devices)
        key = d.get("about") or "/".join(next(iter(d["match"].values())))
        if dev is None:
            out.append(Change("manual", "device", key, "not found in HA (removed or re-paired?): update its match"))
            continue
        delta = _override_delta(d, dev, DEVICE_FIELDS)
        if delta:
            out.append(Change("update", "device", key, _describe(delta, dev), data={"device_id": dev["id"], **delta}))
    return out


def diff_entities(declared, remove, entities) -> list:
    by_key = {(e["platform"], str(e["unique_id"])): e for e in entities}
    out = []
    for d in declared:
        e = by_key.get((d["match"]["platform"], d["match"]["unique_id"]))
        if e is None:
            out.append(Change("manual", "entity", d.get("about") or f"{d['match']['platform']}/{d['match']['unique_id']}",
                              "not found in HA: update its match"))
            continue
        delta = _override_delta(d, e, ENTITY_FIELDS)
        if "entity_id" in d and d["entity_id"] != e["entity_id"]:
            delta["new_entity_id"] = d["entity_id"]
        if delta:
            out.append(Change("update", "entity", e["entity_id"], _describe(delta, {**e, "new_entity_id": e["entity_id"]}),
                              data={"entity_id": e["entity_id"], **delta}))
    for r in remove:
        e = by_key.get((r["platform"], r["unique_id"]))
        if e is not None:
            out.append(Change("remove", "entity", e["entity_id"], "listed under remove:", data={"entity_id": e["entity_id"]}))
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tools/hactl/src/hactl/state/diff.py tools/hactl/tests/test_state_diff.py
nix develop --command git commit -m "hactl state: diff for floors/labels/areas, devices, entities" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Diff — helpers, integrations, dashboards, resources; `plan()`

**Files:**
- Modify: `tools/hactl/src/hactl/state/diff.py` (append)
- Test: `tools/hactl/tests/test_state_diff.py` (append)

**Interfaces:**
- Consumes: `model.HELPER_DOMAINS`, `model.BUILTIN_DASHBOARDS`, `model.Manifest`, `live.Snapshot`.
- Produces: `diff.diff_config_entries(kind, declared, entries, options, credentials, credentials_locked=False)`; `diff.DASHBOARD_DEFAULTS`; `diff.diff_dashboards(declared, live)`; `diff.diff_resources(declared, live)`; `diff.plan(manifest, snapshot) -> list[Change]` (order: floors, labels, areas, helpers, integrations, devices, entities, dashboards, resources); `diff.options_wanted(manifest) -> set[(domain, title)]`. Change data: config-entry create `{"domain", "title", "answers", "menu"}` (answers = options ∪ credentials ∪ create.answers, later wins); update `{"entry_id", "options"}`; helper delete `{"entry_id"}`; dashboard create `{"url_path", "mode": "storage", "title", "icon", "require_admin", "show_in_sidebar"}`, update `{"dashboard_id", …}`, delete `{"dashboard_id"}`; resource create `{"res_type", "url"}`, update `{"resource_id", "res_type", "url"}`, delete `{"resource_id"}`.

- [ ] **Step 1: Append the failing tests** to `tools/hactl/tests/test_state_diff.py`:

```python
from hactl.state import model

GROUP = {"domain": "group", "title": "Bedroom Blinds", "create": {"menu": ["cover"], "answers": {"name": "Bedroom Blinds"}},
         "options": {"entities": ["cover.window_left", "cover.window_right"], "hide_members": False}}


def entries(kind, declared, credentials=None, locked=False):
    snap = snapshot()
    return diff.diff_config_entries(kind, declared, snap.entries, snap.options, credentials or {}, locked)


def test_helper_in_sync():
    assert entries("helper", [GROUP]) == []


def test_helper_option_drift_is_an_update():
    changed = dict(GROUP, options={"entities": ["cover.window_left"], "hide_members": False})
    [c] = entries("helper", [changed])
    assert (c.action, c.data) == ("update", {"entry_id": "e1", "options": changed["options"]})


def test_missing_helper_is_created_with_merged_answers_and_undeclared_is_prunable():
    new = {"domain": "switch_as_x", "title": "Camp Lamp", "create": {"answers": {"entity_id": "switch.camp_lamp", "target_domain": "light"}}}
    changes = entries("helper", [new])
    assert [(c.action, c.key, c.prune) for c in changes] == [
        ("create", "switch_as_x/Camp Lamp", False), ("delete", "group/Bedroom Blinds", True)]
    assert changes[0].data == {"domain": "switch_as_x", "title": "Camp Lamp", "menu": [],
                               "answers": {"entity_id": "switch.camp_lamp", "target_domain": "light"}}


def test_integration_create_merges_credentials_and_manual_otherwise():
    declared = [
        {"domain": "hue", "title": "Hue Bridge", "manual": "press the link button"},
        {"domain": "plex", "title": "Plex"},
        {"domain": "airnow", "title": "AirNow", "create": {"answers": {"radius": 150}}, "credentials": "airnow"},
        {"domain": "octoprint", "title": "OctoPrint", "manual": "approve the app key in OctoPrint"},
    ]
    changes = entries("integration", declared, credentials={"airnow": {"api_key": "k"}})
    assert [(c.action, c.key) for c in changes] == [("create", "airnow/AirNow"), ("manual", "octoprint/OctoPrint")]
    assert changes[0].data["answers"] == {"api_key": "k", "radius": 150}
    assert "approve the app key" in str(changes[1])


def test_undeclared_integration_is_manual_never_deleted():
    changes = entries("integration", [{"domain": "hue", "title": "Hue Bridge", "manual": "x"}])
    assert [(c.action, c.key, c.prune) for c in changes] == [("manual", "plex/Plex", False)]
    assert "not declared" in changes[0].detail


def test_create_needing_locked_credentials_is_manual():
    declared = [{"domain": "airnow", "title": "AirNow", "create": {"answers": {}}, "credentials": "airnow"},
                {"domain": "hue", "title": "Hue Bridge", "manual": "x"}, {"domain": "plex", "title": "Plex", "manual": "x"}]
    [c] = entries("integration", declared, locked=True)
    assert c.action == "manual" and "git-crypt unlock" in c.detail


def test_unreadable_options_are_manual():
    snap = snapshot()
    snap.options[("group", "Bedroom Blinds")] = None
    [c] = diff.diff_config_entries("helper", [GROUP], snap.entries, snap.options, {})
    assert c.action == "manual" and "could not be read" in c.detail


def test_dashboards_builtins_yaml_and_prune():
    snap = snapshot()
    declared = [{"url_path": "lovelace", "title": "Overview", "icon": "mdi:view-dashboard"},
                {"url_path": "map", "title": "Map"},
                {"url_path": "energy-board", "title": "Energy", "icon": "mdi:flash"}]
    changes = diff.diff_dashboards(declared, snap.dashboards)
    assert [(c.action, c.key, c.prune) for c in changes] == [
        ("manual", "map", False), ("create", "energy-board", False), ("delete", "claude-preview", True)]
    assert changes[1].data == {"url_path": "energy-board", "mode": "storage", "title": "Energy", "icon": "mdi:flash",
                               "require_admin": False, "show_in_sidebar": True}


def test_resources():
    snap = snapshot()
    changes = diff.diff_resources([{"url": "/hacsfiles/mushroom.js", "type": "js"}, {"url": "/local/x.js", "type": "module"}],
                                  snap.resources)
    assert [(c.action, c.data) for c in changes] == [
        ("update", {"resource_id": "r1", "res_type": "js", "url": "/hacsfiles/mushroom.js"}),
        ("create", {"res_type": "module", "url": "/local/x.js"})]


def test_plan_orders_kinds_and_lists_wanted_options():
    m = model.Manifest(areas=[{"id": "office", "name": "Office"}], helpers=[GROUP],
                       integrations=[{"domain": "hue", "title": "Hue Bridge", "manual": "x"}, {"domain": "plex", "title": "Plex", "manual": "x"}],
                       dashboards=[{"url_path": "lovelace", "title": "Overview", "icon": "mdi:view-dashboard"},
                                   {"url_path": "claude-preview", "title": "Claude Preview", "icon": "mdi:flask-outline",
                                    "require_admin": True, "show_in_sidebar": False}],
                       resources=[{"url": "/hacsfiles/mushroom.js", "type": "module"}])
    kinds = [c.kind for c in diff.plan(m, snapshot())]
    assert kinds == ["area", "area", "area"]  # create office, delete bedroom + kitchen
    assert diff.options_wanted(m) == {("group", "Bedroom Blinds")}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_state_diff.py`
Expected: FAIL — `AttributeError: module 'hactl.state.diff' has no attribute 'diff_config_entries'`.

- [ ] **Step 3: Append to `tools/hactl/src/hactl/state/diff.py`** (and add `from hactl.state.model import BUILTIN_DASHBOARDS, HELPER_DOMAINS` to its imports):

```python
def diff_config_entries(kind, declared, entries, options, credentials, credentials_locked=False) -> list:
    """Helpers: fully managed (prunable). Integrations: presence + options, never deleted."""
    helper = kind == "helper"
    by_key = {}
    for e in entries:
        if (e["domain"] in HELPER_DOMAINS) == helper:
            by_key.setdefault((e["domain"], e["title"]), []).append(e)
    out = []
    for d in declared:
        key, name = (d["domain"], d["title"]), f"{d['domain']}/{d['title']}"
        found = by_key.get(key, [])
        if len(found) > 1:
            out.append(Change("manual", kind, name, f"{len(found)} entries share this domain and title; rename one in HA"))
            continue
        if not found:
            if d.get("create") is None:
                out.append(Change("manual", kind, name, d.get("manual") or "add it in HA (Settings -> Devices & services)"))
            elif d.get("credentials") and credentials_locked:
                out.append(Change("manual", kind, name, "needs credentials.yaml, which is encrypted: git-crypt unlock"))
            else:
                answers = {**d.get("options", {}), **credentials.get(d.get("credentials"), {}), **d["create"].get("answers", {})}
                out.append(Change("create", kind, name, "via its config flow",
                                  data={"domain": d["domain"], "title": d["title"], "answers": answers,
                                        "menu": d["create"].get("menu", [])}))
            continue
        if "options" in d:
            cur = options.get(key)
            if cur is None:
                out.append(Change("manual", kind, name, "its options could not be read (no options flow?)"))
                continue
            delta = {k: v for k, v in d["options"].items() if not same(v, cur["values"].get(k))}
            if delta:
                out.append(Change("update", kind, name, _describe(delta, cur["values"]),
                                  data={"entry_id": found[0]["entry_id"], "options": d["options"]}))
    declared_keys = {(d["domain"], d["title"]) for d in declared}
    for (domain, title), found in sorted(by_key.items()):
        if (domain, title) in declared_keys:
            continue
        for e in found:
            if helper:
                out.append(Change("delete", kind, f"{domain}/{title}", "is not declared", data={"entry_id": e["entry_id"]}, prune=True))
            else:
                out.append(Change("manual", kind, f"{domain}/{title}", "is not declared in integrations.yaml (declare it, or delete it in HA)"))
    return out


DASHBOARD_DEFAULTS = {"icon": None, "require_admin": False, "show_in_sidebar": True}


def diff_dashboards(declared, live) -> list:
    storage = {x["url_path"]: x for x in live if x.get("mode") == "storage"}
    out = []
    for d in declared:
        want = {"title": d["title"], **{k: d.get(k, v) for k, v in DASHBOARD_DEFAULTS.items()}}
        cur = storage.get(d["url_path"])
        if cur is None:
            if d["url_path"] in BUILTIN_DASHBOARDS:
                out.append(Change("manual", "dashboard", d["url_path"], "built-in dashboard is missing; HA recreates it"))
            else:
                out.append(Change("create", "dashboard", d["url_path"], repr(d["title"]),
                                  data={"url_path": d["url_path"], "mode": "storage", **want}))
            continue
        delta = {k: v for k, v in want.items() if not same(v, cur.get(k))}
        if delta:
            out.append(Change("update", "dashboard", d["url_path"], _describe(delta, cur), data={"dashboard_id": cur["id"], **delta}))
    declared_paths = {d["url_path"] for d in declared}
    out += [Change("delete", "dashboard", u, "is not declared", data={"dashboard_id": x["id"]}, prune=True)
            for u, x in storage.items() if u not in declared_paths and u not in BUILTIN_DASHBOARDS]
    return out


def diff_resources(declared, live) -> list:
    by_url = {x["url"]: x for x in live}
    out = []
    for d in declared:
        cur = by_url.get(d["url"])
        if cur is None:
            out.append(Change("create", "resource", d["url"], d["type"], data={"res_type": d["type"], "url": d["url"]}))
        elif cur.get("type") != d["type"]:
            out.append(Change("update", "resource", d["url"], f"type {cur.get('type')!r} -> {d['type']!r}",
                              data={"resource_id": cur["id"], "res_type": d["type"], "url": d["url"]}))
    declared_urls = {d["url"] for d in declared}
    out += [Change("delete", "resource", x["url"], "is not declared", data={"resource_id": x["id"]}, prune=True)
            for x in live if x["url"] not in declared_urls]
    return out


def plan(m, snap) -> list:
    """Every change needed to make HA match the manifests, in apply order."""
    return (diff_registry("floor", m.floors, snap.floors)
            + diff_registry("label", m.labels, snap.labels)
            + diff_registry("area", m.areas, snap.areas)
            + diff_config_entries("helper", m.helpers, snap.entries, snap.options, m.credentials, m.credentials_locked)
            + diff_config_entries("integration", m.integrations, snap.entries, snap.options, m.credentials, m.credentials_locked)
            + diff_devices(m.devices, snap.devices)
            + diff_entities(m.entities, m.remove, snap.entities)
            + diff_dashboards(m.dashboards, snap.dashboards)
            + diff_resources(m.resources, snap.resources))


def options_wanted(m) -> set:
    return {(x["domain"], x["title"]) for x in m.helpers + m.integrations if "options" in x}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tools/hactl/src/hactl/state/diff.py tools/hactl/tests/test_state_diff.py
nix develop --command git commit -m "hactl state: diff for helpers, integrations, dashboards, resources; plan()" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Apply

**Files:**
- Create: `tools/hactl/src/hactl/state/apply.py`
- Test: `tools/hactl/tests/test_state_apply.py`

**Interfaces:**
- Consumes: `diff.Change`, `flows.run_config_flow`, `flows.run_options_flow`, `client.ws`, `client.rest`.
- Produces: `apply.run_change(client, change) -> str` (a note, usually `""`), `apply.execute(client, changes, prune=False, log=print) -> dict` with keys `applied`, `skipped`, `manual`, `errors`, `notes` (lists of strings). Creates/updates/removes run in plan order; deletes run in reverse plan order and only with `prune=True`; a failing change is recorded and the rest continue.

- [ ] **Step 1: Write the failing tests** `tools/hactl/tests/test_state_apply.py`:

```python
from state_fixtures import FakeHA, snapshot

from hactl.errors import HactlError
from hactl.state import apply, diff, live, model


def plan_for(fake, m):
    return diff.plan(m, live.fetch(fake))


def base_manifest(**over):
    m = model.Manifest(
        areas=[{"id": "bedroom", "name": "Bedroom", "icon": "mdi:bed"}, {"id": "kitchen", "name": "Kitchen", "icon": "mdi:fridge"}],
        integrations=[{"domain": "hue", "title": "Hue Bridge", "manual": "x"}, {"domain": "plex", "title": "Plex", "manual": "x"}],
        helpers=[{"domain": "group", "title": "Bedroom Blinds", "create": {"menu": ["cover"], "answers": {}}}],
        dashboards=[{"url_path": "lovelace", "title": "Overview", "icon": "mdi:view-dashboard"},
                    {"url_path": "claude-preview", "title": "Claude Preview", "icon": "mdi:flask-outline",
                     "require_admin": True, "show_in_sidebar": False}],
        resources=[{"url": "/hacsfiles/mushroom.js", "type": "module"}])
    for k, v in over.items():
        setattr(m, k, v)
    return m


def test_registry_and_device_entity_changes_converge():
    fake = FakeHA(snapshot())
    m = base_manifest(
        labels=[{"id": "lighting", "name": "Lighting", "color": "amber"}],
        areas=[{"id": "bedroom", "name": "Bedroom", "icon": "mdi:bed", "labels": ["lighting"]},
               {"id": "kitchen", "name": "Kitchen", "icon": "mdi:fridge"}, {"id": "office", "name": "Office"}],
        devices=[{"match": {"connections": ["mac", "aa:bb"]}, "area": "office", "name": "Router"}],
        entities=[{"match": {"platform": "mqtt", "unique_id": "0x1_switch"}, "entity_id": "switch.bedroom_light_switch"}],
        remove=[{"platform": "mail_and_packages", "unique_id": "old_1"}])
    result = apply.execute(fake, plan_for(fake, m), log=lambda s: None)
    assert result["errors"] == []
    assert plan_for(fake, m) == []
    assert fake.calls.index("config/label_registry/create") < fake.calls.index("config/area_registry/update")


def test_deletes_only_with_prune():
    fake = FakeHA(snapshot())
    m = base_manifest(areas=[{"id": "bedroom", "name": "Bedroom", "icon": "mdi:bed"}])  # kitchen undeclared
    result = apply.execute(fake, plan_for(fake, m), log=lambda s: None)
    assert result["skipped"] == ["- area kitchen: 'Kitchen' is not declared (needs --prune)"]
    assert any(a["area_id"] == "kitchen" for a in fake.areas)
    apply.execute(fake, plan_for(fake, m), prune=True, log=lambda s: None)
    assert not any(a["area_id"] == "kitchen" for a in fake.areas)


def test_one_failure_does_not_stop_the_rest():
    fake = FakeHA(snapshot())
    changes = [diff.Change("update", "area", "nope", data={"area_id": "nope", "icon": "x"}),  # not in FakeHA -> error
               diff.Change("update", "area", "bedroom", data={"area_id": "bedroom", "icon": "mdi:bed-king"})]
    result = apply.execute(fake, changes, log=lambda s: None)
    assert len(result["errors"]) == 1 and len(result["applied"]) == 1
    assert next(a for a in fake.areas if a["area_id"] == "bedroom")["icon"] == "mdi:bed-king"


def test_registry_id_mismatch_is_an_error():
    fake = FakeHA(snapshot())
    c = diff.Change("create", "label", "lights", data={"id": "lights", "name": "Lighting"})  # HA would make 'lighting'
    result = apply.execute(fake, [c], log=lambda s: None)
    assert "set id: lighting" in result["errors"][0]


def test_manual_changes_are_reported_not_run():
    result = apply.execute(FakeHA(snapshot()), [diff.Change("manual", "integration", "octoprint/OctoPrint", "approve the key")],
                           log=lambda s: None)
    assert result["manual"] == ["! integration octoprint/OctoPrint: approve the key"] and result["applied"] == []


def test_helper_create_runs_its_config_flow(monkeypatch):
    calls = []
    monkeypatch.setattr(apply.flows, "run_config_flow",
                        lambda client, domain, answers, menu: calls.append((domain, answers, menu)) or {"title": "Camp Lamp"})
    c = diff.Change("create", "helper", "switch_as_x/Camp Lamp",
                    data={"domain": "switch_as_x", "title": "Camp Lamp", "answers": {"entity_id": "switch.camp_lamp"}, "menu": []})
    result = apply.execute(FakeHA(snapshot()), [c], log=lambda s: None)
    assert calls == [("switch_as_x", {"entity_id": "switch.camp_lamp"}, [])] and result["notes"] == []


def test_created_title_mismatch_is_a_note(monkeypatch):
    monkeypatch.setattr(apply.flows, "run_config_flow", lambda *a: {"title": "Camp Lamp (light)"})
    c = diff.Change("create", "helper", "switch_as_x/Camp Lamp",
                    data={"domain": "switch_as_x", "title": "Camp Lamp", "answers": {}, "menu": []})
    assert "Camp Lamp (light)" in apply.execute(FakeHA(snapshot()), [c], log=lambda s: None)["notes"][0]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_state_apply.py`
Expected: FAIL — `ImportError: cannot import name 'apply'`.

- [ ] **Step 3: Implement `tools/hactl/src/hactl/state/apply.py`:**

```python
"""Execute a plan against HA: creates/updates/removes in plan order; deletes in
reverse plan order, and only with prune. A failing change is recorded; the
rest still run."""
from hactl.errors import HactlError
from hactl.state import flows

REGISTRY = {"floor": ("config/floor_registry", "floor_id"), "label": ("config/label_registry", "label_id"),
            "area": ("config/area_registry", "area_id")}


def _without_empty(data: dict) -> dict:
    return {k: v for k, v in data.items() if v is not None}


def run_change(client, c) -> str:
    """Apply one change. Returns a note for the operator ('' when none)."""
    if c.kind in REGISTRY:
        base, id_key = REGISTRY[c.kind]
        if c.action == "create":
            payload = _without_empty({k: v for k, v in c.data.items() if k != "id"})
            got = client.ws({"type": f"{base}/create", **payload})[0][id_key]
            if got != c.data["id"]:
                raise HactlError(f"HA created {c.kind} id {got!r}, the manifest says {c.data['id']!r}: set id: {got} in areas.yaml")
        else:
            client.ws({"type": f"{base}/{c.action}", **c.data})
        return ""
    if c.kind == "device":
        client.ws({"type": "config/device_registry/update", **c.data})
        return ""
    if c.kind == "entity":
        verb = "remove" if c.action == "remove" else "update"
        client.ws({"type": f"config/entity_registry/{verb}", **c.data})
        return ""
    if c.kind in ("helper", "integration"):
        if c.action == "create":
            title = flows.run_config_flow(client, c.data["domain"], c.data["answers"], c.data["menu"]).get("title")
            return f"created with title {title!r}: set title: to that in the manifest" if title and title != c.data["title"] else ""
        if c.action == "update":
            flows.run_options_flow(client, c.data["entry_id"], c.data["options"])
            return ""
        if c.action == "delete":
            client.rest("DELETE", f"/api/config/config_entries/entry/{c.data['entry_id']}")
            return ""
    if c.kind == "dashboard":
        data = _without_empty(c.data) if c.action == "create" else c.data
        client.ws({"type": f"lovelace/dashboards/{c.action}", **data})
        return ""
    if c.kind == "resource":
        client.ws({"type": f"lovelace/resources/{c.action}", **c.data})
        return ""
    raise HactlError(f"don't know how to {c.action} a {c.kind}")


def execute(client, changes, prune=False, log=print) -> dict:
    result = {"applied": [], "skipped": [], "manual": [], "errors": [], "notes": []}
    result["manual"] = [str(c) for c in changes if c.action == "manual"]
    forward = [c for c in changes if c.action in ("create", "update", "remove")]
    deletes = list(reversed([c for c in changes if c.action == "delete"]))
    if not prune:
        result["skipped"] = [str(c) for c in reversed(deletes)]
        deletes = []
    for c in forward + deletes:
        try:
            note = run_change(client, c)
        except (HactlError, StopIteration, KeyError) as e:  # StopIteration/KeyError: object vanished mid-apply
            result["errors"].append(f"{c}: {e}")
            log(f"FAILED   {c}: {e}")
            continue
        result["applied"].append(str(c))
        if note:
            result["notes"].append(f"{c.kind} {c.key}: {note}")
        log(f"applied  {c}")
    return result
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tools/hactl/src/hactl/state/apply.py tools/hactl/tests/test_state_apply.py
nix develop --command git commit -m "hactl state: apply (ordered, prune-gated, failure-isolated)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Import

**Files:**
- Create: `tools/hactl/src/hactl/state/importer.py`
- Test: `tools/hactl/tests/test_state_import.py`

**Interfaces:**
- Consumes: `live.Snapshot`, `model.*`, `diff.plan`.
- Produces: `importer.build(snapshot) -> dict[file stem, data]`, `importer.render(name, data) -> str`, `importer.write(state_dir, files, force=False) -> list[Path]` (refuses existing files unless `force`; creates `credentials.yaml` as an empty mapping only if absent, never overwrites it). Import is exact: `plan(load(write(build(snap))), snap) == []`.

- [ ] **Step 1: Write the failing tests** `tools/hactl/tests/test_state_import.py`:

```python
import pytest
from state_fixtures import snapshot

from hactl.errors import HactlError
from hactl.state import diff, importer, model


def test_round_trip_plan_is_empty(tmp_path):
    snap = snapshot()
    importer.write(tmp_path, importer.build(snap))
    assert diff.plan(model.load(tmp_path), snap) == []


def test_build_records_overrides_not_defaults():
    files = importer.build(snapshot())
    assert files["devices"]["devices"] == [
        {"match": {"identifiers": ["mqtt", "zigbee2mqtt_0x1"]}, "about": "0x1 · Leviton DG15S", "name": "Bedroom Light Switch"},
        {"match": {"identifiers": ["hue", "lamp-1"]}, "about": "Dresser Lamp · Signify Hue color lamp", "area": "bedroom"},
    ]
    assert files["entities"] == {"entities": [], "remove": []}
    assert [d["url_path"] for d in files["dashboards"]["dashboards"]] == ["claude-preview", "lovelace"]


def test_helpers_get_menu_and_options_integrations_get_manual():
    files = importer.build(snapshot())
    assert files["helpers"]["helpers"] == [{
        "domain": "group", "title": "Bedroom Blinds",
        "create": {"menu": ["cover"], "answers": {"name": "Bedroom Blinds"}},
        "options": {"entities": ["cover.window_left", "cover.window_right"], "hide_members": False},
    }]
    assert [(i["domain"], "Add integration" in i["manual"]) for i in files["integrations"]["integrations"]] == [
        ("hue", True), ("plex", True)]


def test_import_refuses_to_overwrite(tmp_path):
    importer.write(tmp_path, importer.build(snapshot()))
    with pytest.raises(HactlError, match="refusing to overwrite"):
        importer.write(tmp_path, importer.build(snapshot()))
    importer.write(tmp_path, importer.build(snapshot()), force=True)


def test_credentials_never_overwritten(tmp_path):
    (tmp_path / "credentials.yaml").write_text("airnow: {api_key: keep-me}\n")
    importer.write(tmp_path, importer.build(snapshot()))
    assert "keep-me" in (tmp_path / "credentials.yaml").read_text()


def test_numeric_unique_ids_stay_strings(tmp_path):
    snap = snapshot()
    snap.entities.append({"entity_id": "light.x", "platform": "hue", "unique_id": "1741747401508", "name": "X",
                          "icon": None, "area_id": None, "labels": [], "hidden_by": None, "disabled_by": None})
    importer.write(tmp_path, importer.build(snap))
    m = model.load(tmp_path)
    assert m.entities[0]["match"]["unique_id"] == "1741747401508"
    assert diff.plan(m, snap) == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_state_import.py`
Expected: FAIL — `ImportError: cannot import name 'importer'`.

- [ ] **Step 3: Implement `tools/hactl/src/hactl/state/importer.py`:**

```python
"""Write the state manifests from live HA: the bootstrap for `hactl plan`."""
from pathlib import Path

import yaml

from hactl.errors import HactlError
from hactl.state.model import CREDENTIALS, HELPER_DOMAINS

HEADERS = {
    "areas": "Floors, labels and areas. Fully managed: `hactl apply` creates and updates them;\n"
             "# deleting one needs `hactl apply --prune`. An id is HA's slug of the name when it was created.",
    "devices": "Device overrides (area, name, labels, disabled), matched by one hardware identifier\n"
               "# that survives renames. Only listed devices, and only listed fields, are managed.",
    "entities": "Entity overrides, matched by platform + unique_id (survives entity_id renames).\n"
                "# Unlisted entities are untouched. `remove:` deletes registry entries (dead entities).",
    "helpers": "Config-entry helpers (group, switch_as_x, ...): created by their config flow from `create`,\n"
               "# `options` enforced, undeclared ones deleted with --prune.",
    "integrations": "Integrations that must exist. hactl never deletes one. `create` answers its config flow\n"
                    "# (secrets via `credentials:` from credentials.yaml); `manual` says what a person must do.",
    "dashboards": "Storage-mode dashboards (YAML ones are the chart's lovelace.dashboards) and Lovelace\n"
                  "# resources. Fully managed; deleting needs --prune. lovelace and map are HA built-ins.",
}
CREDENTIALS_TEMPLATE = ("# Secrets that config flows need, referenced by `credentials:` in helpers/integrations.\n"
                        "# git-crypt encrypted (.gitattributes). Never print or paste this file.\n{}\n")


def _compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, [], "", {})}


def _about(d) -> str:
    make = " ".join(x for x in (d.get("manufacturer"), d.get("model")) if x)
    return " · ".join(x for x in (d.get("name"), make) if x)


def build(snap) -> dict:
    files = {"areas": {
        "floors": [_compact({"id": f["floor_id"], "name": f["name"], "level": f.get("level"), "icon": f.get("icon"),
                             "aliases": f.get("aliases")}) for f in snap.floors],
        "labels": [_compact({"id": x["label_id"], "name": x["name"], "color": x.get("color"), "icon": x.get("icon"),
                             "description": x.get("description")}) for x in snap.labels],
        "areas": [_compact({"id": a["area_id"], "name": a["name"], "floor": a.get("floor_id"), "icon": a.get("icon"),
                            "labels": a.get("labels"), "aliases": a.get("aliases")}) for a in snap.areas],
    }}
    devices = []
    for d in sorted(snap.devices, key=lambda d: (d.get("name_by_user") or d.get("name") or "").lower()):
        if not (d.get("area_id") or d.get("name_by_user") or d.get("labels") or d.get("disabled_by") == "user"):
            continue
        kind = "identifiers" if d.get("identifiers") else "connections"
        if not d.get(kind):
            continue
        item = {"match": {kind: [str(x) for x in d[kind][0]]}, "about": _about(d),
                **_compact({"name": d.get("name_by_user"), "area": d.get("area_id"), "labels": d.get("labels")})}
        if d.get("disabled_by") == "user":
            item["disabled"] = True
        devices.append(item)
    files["devices"] = {"devices": devices}
    entities = []
    for e in sorted(snap.entities, key=lambda e: e["entity_id"]):
        fields = _compact({"name": e.get("name"), "icon": e.get("icon"), "area": e.get("area_id"), "labels": e.get("labels")})
        flags = {k: True for k, f in (("hidden", "hidden_by"), ("disabled", "disabled_by")) if e.get(f) == "user"}
        if fields or flags:
            entities.append({"match": {"platform": e["platform"], "unique_id": str(e["unique_id"])},
                             "about": e["entity_id"], **fields, **flags})
    files["entities"] = {"entities": entities, "remove": []}
    helpers, integrations = [], []
    for e in sorted(snap.entries, key=lambda e: (e["domain"], e["title"])):
        if e["domain"] in HELPER_DOMAINS:
            opts = snap.options.get((e["domain"], e["title"]))
            create = {"answers": {"name": e["title"]}}
            if opts and opts.get("step_id") not in (None, "init"):
                create = {"menu": [opts["step_id"]], **create}  # e.g. group: the helper type is the step
            item = {"domain": e["domain"], "title": e["title"], "create": create}
            if opts is not None:
                item["options"] = opts["values"]
            helpers.append(item)
        else:
            integrations.append({"domain": e["domain"], "title": e["title"],
                                 "manual": f"re-add it in HA: Settings -> Devices & services -> Add integration -> {e['domain']}"})
    files["helpers"] = {"helpers": helpers}
    files["integrations"] = {"integrations": integrations}
    files["dashboards"] = {
        "dashboards": [{**_compact({"url_path": x["url_path"], "title": x["title"], "icon": x.get("icon")}),
                        "require_admin": bool(x.get("require_admin")), "show_in_sidebar": bool(x.get("show_in_sidebar", True))}
                       for x in sorted(snap.dashboards, key=lambda x: x["url_path"]) if x.get("mode") == "storage"],
        "resources": [{"url": r["url"], "type": r["type"]} for r in snap.resources],
    }
    return files


def render(name, data) -> str:
    return (f"# {HEADERS[name]}\n# Written by `hactl import`, then edited by hand; converge with `hactl apply`.\n"
            + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000))


def write(state_dir: Path, files: dict, force=False) -> list:
    existing = [f"{n}.yaml" for n in files if (state_dir / f"{n}.yaml").exists()]
    if existing and not force:
        raise HactlError(f"refusing to overwrite {', '.join(existing)} (hand edits would be lost); use --force")
    state_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, data in files.items():
        path = state_dir / f"{name}.yaml"
        path.write_text(render(name, data))
        written.append(path)
    cred = state_dir / CREDENTIALS
    if not cred.exists():
        cred.write_text(CREDENTIALS_TEMPLATE)
        written.append(cred)
    return written
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass. If `test_build_records_overrides_not_defaults` fails only on device order, the sort key (case-insensitive `name_by_user or name`) is the contract: "0x1"'s `name_by_user` is "Bedroom Light Switch" (b) before "Dresser Lamp" (d).

- [ ] **Step 5: Commit**

```bash
git add tools/hactl/src/hactl/state/importer.py tools/hactl/tests/test_state_import.py
nix develop --command git commit -m "hactl state: import (exact round trip; never overwrites)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: CLI (`import`, `plan`, `apply`) and lint

**Files:**
- Create: `tools/hactl/src/hactl/state/cli.py`
- Modify: `tools/hactl/src/hactl/__main__.py` (`MODULES` += `"state.cli"`)
- Modify: `tools/hactl/src/hactl/lint.py` (`check_state`, called from `offline`)
- Test: `tools/hactl/tests/test_state_cli.py`, extend `tools/hactl/tests/test_lint.py`

**Interfaces:**
- Consumes: Tasks 1–7.
- Produces: `cli.compute(client, state_dir=model.STATE_DIR) -> (Manifest, Snapshot, [Change])` (raises `HactlError` on an invalid manifest before any API call); `cli.converge(client, prune=False, state_dir=model.STATE_DIR, log=print) -> (lines, ok)` (ok = no errors and nothing left to do); commands `hactl import [--force]`, `hactl plan` (exit 0/2; `--json` uses `Change.public()`), `hactl apply [--prune]` (exit 0/1); `lint.check_state(ha_dir) -> [Finding]`.

- [ ] **Step 1: Write the failing tests** `tools/hactl/tests/test_state_cli.py`:

```python
import argparse
import json

import pytest
from state_fixtures import FakeHA, snapshot, write_manifests

from hactl.errors import HactlError
from hactl.state import cli, importer


def imported(tmp_path, snap):
    importer.write(tmp_path, importer.build(snap))
    return tmp_path


def test_converge_in_sync(tmp_path):
    snap = snapshot()
    snap.options = {}
    fake = FakeHA(snap)
    state = imported(tmp_path, snap)
    assert cli.converge(fake, state_dir=state, log=lambda s: None) == (["state: in sync"], True)


def test_converge_applies_then_reports_leftovers(tmp_path):
    snap = snapshot()
    snap.options = {}
    fake = FakeHA(snap)
    state = imported(tmp_path, snap)
    text = (state / "areas.yaml").read_text().replace("icon: mdi:bed\n", "icon: mdi:bed-king\n")
    (state / "areas.yaml").write_text(text + "- id: office\n  name: Office\n")  # safe_dump block style: items at column 0
    lines, ok = cli.converge(fake, state_dir=state, log=lambda s: None)
    assert ok and lines[0] == "state: applied 2, failed 0"
    assert any(a["area_id"] == "office" for a in fake.areas)


def test_converge_refuses_an_invalid_manifest_before_touching_ha(tmp_path):
    fake = FakeHA(snapshot())
    write_manifests(tmp_path, {"areas.yaml": "areas:\n  - {id: x}\n"})
    with pytest.raises(HactlError, match="missing name"):
        cli.converge(fake, state_dir=tmp_path, log=lambda s: None)
    assert fake.calls == []


def test_plan_json_hides_answers(tmp_path, monkeypatch, capsys):
    snap = snapshot()
    snap.options = {}
    state = imported(tmp_path, snap)
    (state / "credentials.yaml").write_text("airnow: {api_key: super-secret}\n")
    integ = (state / "integrations.yaml").read_text()
    (state / "integrations.yaml").write_text(integ + "- domain: airnow\n  title: AirNow\n  create: {answers: {}}\n  credentials: airnow\n")
    monkeypatch.setattr(cli, "_client", lambda: FakeHA(snap))
    monkeypatch.setattr(cli.model, "STATE_DIR", state)
    rc = cli._plan(argparse.Namespace(json=True))
    out = capsys.readouterr().out
    assert rc == 2 and "super-secret" not in out
    assert json.loads(out)[0]["key"] == "airnow/AirNow"
```

Append to `tools/hactl/tests/test_lint.py`:

```python
def test_state_manifest_problems_are_lint_findings(tmp_path):
    ha = make_ha(tmp_path, {"packages/a.yaml": "automation: []\n"})
    (ha / "state").mkdir()
    (ha / "state" / "areas.yaml").write_text("areas:\n  - {id: x, name: X, floor: nowhere}\n")
    found = lint.check_state(ha)
    assert [f.rule for f in found] == ["state"] and "floor 'nowhere' is not declared" in found[0].message
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_state_cli.py tools/hactl/tests/test_lint.py`
Expected: FAIL — `ImportError: cannot import name 'cli'` and `AttributeError: … has no attribute 'check_state'`.

- [ ] **Step 3: Implement `tools/hactl/src/hactl/state/cli.py`:**

```python
"""hactl import / plan / apply: declarative HA state (spec §4.11)."""
from hactl import output, paths
from hactl.state import apply as applier
from hactl.state import diff, importer, live, model


def _client():
    from hactl.client import Client
    return Client()


def compute(client, state_dir=None):
    m = model.load(state_dir or model.STATE_DIR)  # raises before any API call if the manifests are wrong
    snap = live.fetch(client, want_options=diff.options_wanted(m))
    return m, snap, diff.plan(m, snap)


def converge(client, prune=False, state_dir=None, log=print):
    """Plan, apply, re-plan. ok = no errors and nothing left to do."""
    _, _, changes = compute(client, state_dir)
    if not changes:
        return ["state: in sync"], True
    result = applier.execute(client, changes, prune=prune, log=log)
    _, _, left = compute(client, state_dir)
    lines = [f"state: applied {len(result['applied'])}, failed {len(result['errors'])}"]
    lines += [f"  error: {e}" for e in result["errors"]]
    lines += [f"  note: {n}" for n in result["notes"]]
    lines += [f"  still to do: {c}" for c in left]
    return lines, not result["errors"] and not left


def _plan(args) -> int:
    _, _, changes = compute(_client())
    output.emit(args, [c.public() for c in changes], [str(c) for c in changes] or ["state: in sync"])
    return 2 if changes else 0


def _apply(args) -> int:
    lines, ok = converge(_client(), prune=args.prune)
    output.emit(args, {"ok": ok, "lines": lines}, lines)
    return 0 if ok else 1


def _import(args) -> int:
    client = _client()
    entries = client.ws({"type": "config_entries/get"})[0]
    want = {(e["domain"], e["title"]) for e in entries if e["domain"] in model.HELPER_DOMAINS}
    written = importer.write(model.STATE_DIR, importer.build(live.fetch(client, want_options=want)), force=args.force)
    lines = [f"wrote {paths.rel(p)}" for p in written] + ["next: review the files, then `hactl plan` (should say in sync)"]
    output.emit(args, [str(p) for p in written], lines)
    return 0


def register(sub) -> None:
    p = sub.add_parser("import", parents=[output.COMMON], help="write state/ manifests from live HA (bootstrap)")
    p.add_argument("--force", action="store_true", help="overwrite existing manifests (credentials.yaml is never touched)")
    p.set_defaults(func=_import)
    p = sub.add_parser("plan", parents=[output.COMMON], help="diff state/ manifests against live HA (exit 2 = changes)")
    p.set_defaults(func=_plan)
    p = sub.add_parser("apply", parents=[output.COMMON], help="converge live HA to the state/ manifests")
    p.add_argument("--prune", action="store_true", help="also delete undeclared floors/labels/areas/helpers/dashboards/resources")
    p.set_defaults(func=_apply)
```

In `__main__.py`, append `"state.cli"` to `MODULES` (after `"selftest"`).

In `tools/hactl/src/hactl/lint.py`, add above `def offline(`:

```python
def check_state(ha_dir: Path) -> list:
    from hactl.state import model  # local: model imports paths, which lint already uses

    try:
        model.load(ha_dir / "state")
    except HactlError as e:
        return [Finding("state", paths.rel(ha_dir / "state"), None, line.strip()) for line in str(e).splitlines()[1:]]
    return []
```

and make `offline` include it:

```python
    findings = (check_filenames(ha_dir) + check_kustomization(ha_dir) + check_quoting(ha_dir)
                + check_revision(ha_dir) + check_state(ha_dir))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass (`test_every_module_registers[state.cli]` included).

- [ ] **Step 5: Live check (read-only, before any manifests exist).**

Run: `nix develop --command bash -c 'hactl plan | head -5; echo "exit=${PIPESTATUS[0]}"'`
Expected: lines like `- area bedroom: 'Bedroom' is not declared (needs --prune)` and `! integration hue/…: is not declared …`, exit 2 — the empty state dir declares nothing yet. Do **not** run `apply` here.

- [ ] **Step 6: Commit**

```bash
git add tools/hactl
nix develop --command git commit -m "hactl: import/plan/apply commands; lint validates state manifests" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: `health` reports state drift; `deploy` converges

**Files:**
- Modify: `tools/hactl/src/hactl/health.py` (`collect`, `render`)
- Modify: `tools/hactl/src/hactl/deploy.py` (`_run`)
- Test: extend `tools/hactl/tests/test_health.py`, `tools/hactl/tests/test_deploy.py`

**Interfaces:**
- Consumes: `cli.compute`, `cli.converge`.
- Produces: `health.collect` adds keys `state` (list of plan lines, or `["could not plan: …"]`) and `not_loaded` (config entries whose state isn't `loaded`, excluding disabled ones); `health.render` treats a non-empty `state` as a problem and lists `not_loaded` as a warning. `deploy._run` runs `cli.converge(client, prune=False)` after the rollout wait and before health; a failed converge makes the exit code 1.

- [ ] **Step 1: Write the failing tests.** Append to `tools/hactl/tests/test_health.py`:

```python
def test_state_drift_is_a_problem_not_loaded_is_a_warning():
    base = {"drift": [], "hook": "succeeded", "failing": [], "repairs": [], "log_errors": [], "unavailable": [],
            "state": [], "not_loaded": ["plex/Plex: setup_retry (timeout)"]}
    lines, problem = health.render(base)
    assert not problem and any("plex/Plex" in line for line in lines)
    lines, problem = health.render(dict(base, state=["+ area office: 'Office'"]))
    assert problem and any("+ area office" in line for line in lines)


def test_not_loaded_entries():
    entries = [{"domain": "plex", "title": "Plex", "state": "setup_retry", "reason": "timeout", "disabled_by": None},
               {"domain": "hue", "title": "Hue", "state": "loaded", "reason": None, "disabled_by": None},
               {"domain": "x", "title": "Off", "state": "not_loaded", "reason": None, "disabled_by": "user"}]
    assert health.not_loaded(entries) == ["plex/Plex: setup_retry (timeout)"]
```

Append to `tools/hactl/tests/test_deploy.py`, and add `monkeypatch.setattr(deploy.state_cli, "converge", lambda client, prune=False, log=None: (["state: in sync"], True))` to the existing `test_failed_screenshots_keep_the_health_report` (its body otherwise unchanged):

```python
def test_deploy_converges_state_and_fails_on_leftovers(monkeypatch, capsys):
    import argparse

    monkeypatch.setattr(deploy, "_git", lambda *a: HEAD if a[0] == "rev-parse" else "origin/main")
    monkeypatch.setattr(deploy, "wait_for_rollout", lambda head, timeout: None)
    monkeypatch.setattr(deploy.state_cli, "converge",
                        lambda client, prune=False, log=None: (["state: applied 0, failed 1", "  error: boom"], False))
    monkeypatch.setattr(deploy.health, "collect", lambda client: {})
    monkeypatch.setattr(deploy.health, "render", lambda report: (["health: ok"], False))
    monkeypatch.setattr("hactl.client.Client", lambda: object())
    rc = deploy._run(argparse.Namespace(timeout=10, shot=False, json=False))
    out = capsys.readouterr().out
    assert rc == 1 and "error: boom" in out and "health: ok" in out
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_health.py tools/hactl/tests/test_deploy.py`
Expected: FAIL — `AttributeError: module 'hactl.health' has no attribute 'not_loaded'` and `… 'hactl.deploy' has no attribute 'state_cli'`.

- [ ] **Step 3: Implement.** In `tools/hactl/src/hactl/health.py` add `from hactl.errors import HactlError` and `from hactl.state import cli as state_cli` to the imports, then add:

```python
def not_loaded(entries) -> list:
    """Config entries that should be running but aren't (disabled ones are intentional)."""
    return [f"{e['domain']}/{e['title']}: {e['state']}" + (f" ({e['reason']})" if e.get("reason") else "")
            for e in entries if e.get("state") != "loaded" and not e.get("disabled_by")]
```

In `collect`, before the `return`, add:

```python
    try:
        _, snap, changes = state_cli.compute(client, ha_dir / "state")
        state, entries = [str(c) for c in changes], snap.entries
    except HactlError as e:
        state, entries = [f"could not plan: {e}"], client.ws({"type": "config_entries/get"})[0]
```

and add to the returned dict:

```python
        "state": state,
        "not_loaded": not_loaded(entries),
```

In `render`, change the first line and add two sections after `drift`:

```python
    problem = bool(r["drift"] or r["failing"] or r["hook"] == "failed" or r.get("state"))
```

```python
    section("state plan (hactl plan; hactl apply converges)", r.get("state", []))
    section("integrations not loaded (warning)", r.get("not_loaded", []))
```

In `tools/hactl/src/hactl/deploy.py` add `from hactl.state import cli as state_cli` to the imports, and in `_run` replace

```python
    client = Client()
    report = health.collect(client)
    lines, problem = health.render(report)
```

with

```python
    client = Client()
    state_lines, state_ok = state_cli.converge(client, prune=False, log=lambda m: None)
    report = health.collect(client)
    lines, problem = health.render(report)
    lines = state_lines + lines
    problem = problem or not state_ok
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tools/hactl
nix develop --command git commit -m "hactl: health reports state drift and unloaded integrations; deploy converges state" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Bootstrap the manifests from live HA and prove the loop

**Files:**
- Create (generated, then reviewed): `cluster/home-assistant/state/{areas,devices,entities,helpers,integrations,dashboards}.yaml`, `cluster/home-assistant/state/credentials.yaml` (git-crypt)

**Interfaces:**
- Consumes: everything above. Acceptance (spec §9 Phase 1): manifests committed, `hactl plan` in sync.

- [ ] **Step 1: Import.**

Run: `export KUBECONFIG=/tmp/galaxy-kubeconfig; nix develop --command hactl import`
Expected: `wrote cluster/home-assistant/state/<file>.yaml` × 7 and the "next" line.

- [ ] **Step 2: Review what was written** (none of these files hold secrets; `credentials.yaml` is the empty template — do not print it after it has real content).

Run: `wc -l cluster/home-assistant/state/*.yaml; sed -n 1,40p cluster/home-assistant/state/areas.yaml; grep -c "match:" cluster/home-assistant/state/devices.yaml; grep -E "^- domain|^  title" cluster/home-assistant/state/helpers.yaml cluster/home-assistant/state/integrations.yaml | head -40`
Expected: 3 areas; ~28 devices (the ones with an area or custom name); 1 helper (`group/Bedroom Blinds` with `menu: [cover]` and its two window covers); 28 integrations; 3 storage dashboards (`claude-preview`, `lovelace`, `map`); 8 resources. Sanity-read the device `about:` lines.

- [ ] **Step 3: Plan must be in sync.**

Run: `nix develop --command bash -c 'hactl plan; echo "exit=$?"; hactl lint --offline --no-check-config'`
Expected: `state: in sync`, `exit=0`; `lint: clean`. If plan is not empty, the importer and diff disagree about some field: that is a bug — fix it with a failing test in `test_state_import.py` first (systematic-debugging), never by hand-editing the manifest to paper over it.

- [ ] **Step 4: Prove create → update → prune live with a label** (reversible, allowed by the actuation policy). Append to `cluster/home-assistant/state/areas.yaml` under `labels:` (replace `labels: []` with):

```yaml
labels:
- id: hactl_probe
  name: Hactl Probe
  color: red
```

Run: `nix develop --command bash -c 'hactl plan; hactl apply; hactl plan'`
Expected: `+ label hactl_probe: 'Hactl Probe'`; then `state: applied 1, failed 0`; then `state: in sync`.

Change `color: red` to `color: amber` and run `hactl apply` → `applied 1`, then `hactl plan` → in sync. Now restore `labels: []` and run:

Run: `nix develop --command bash -c 'hactl plan; hactl apply; echo "exit=$?"; hactl apply --prune; hactl plan'`
Expected: `- label hactl_probe: … (needs --prune)`; plain `apply` leaves it and exits 1 (`still to do: - label hactl_probe …`); `apply --prune` deletes it; final plan `state: in sync`.

- [ ] **Step 5: Commit, confirm encryption, push, deploy.**

```bash
git add cluster/home-assistant/state
nix develop --command git commit -m "home-assistant: state manifests imported from live HA (hactl plan: in sync)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git show HEAD:cluster/home-assistant/state/credentials.yaml | head -c 9 | od -c | head -1
git push origin main
export KUBECONFIG=/tmp/galaxy-kubeconfig; nix develop --command hactl deploy
```
Expected: the `od` line shows `\0 G I T C R Y P T` (if not, STOP: `git reset --soft HEAD~1` and fix `.gitattributes`); `deploy` prints `state: in sync`, then `health: ok` with `state plan …: none` and `integrations not loaded (warning)` listing `plex/…: setup_retry` and `waze_travel_time/…: setup_retry` (known; ops-fixes plan).

---

### Task 11: Docs, skill, spec as-built, memory

**Files:**
- Modify: `docs/ha.md`, `.claude/skills/ha-config/SKILL.md`, `docs/superpowers/specs/2026-10-03-ha-agent-overhaul-design.md` (§4.11 as built)
- Modify: memory `project_ha_agent_tooling.md`

- [ ] **Step 1: `docs/ha.md`.** Add rows to the hactl table:

```markdown
| `import [--force]` | Write `state/` manifests from live HA (bootstrap; refuses to overwrite) |
| `plan` | Diff `state/` manifests against live HA; exit 2 when there are changes |
| `apply [--prune]` | Converge HA to the manifests; deletes only with `--prune`; never deletes integrations |
```

Replace the "Not in git (yet)" section with:

```markdown
## Declarative state (`cluster/home-assistant/state/`)

HA's `.storage` parts are described in git and converged with `hactl apply` (terraform-style). `hactl deploy` runs `apply` after every push; `hactl health` reports a non-empty plan as drift.

| File | Holds | Matched by | Managed |
|---|---|---|---|
| `areas.yaml` | floors, labels, areas | id (HA's slug of the name at creation) | fully; deletes need `--prune` |
| `devices.yaml` | area, name, labels, disabled | one `identifiers`/`connections` pair | listed devices and fields only |
| `entities.yaml` | entity_id, name, icon, area, labels, hidden, disabled; `remove:` list | platform + unique_id | listed entities and fields only |
| `helpers.yaml` | config-entry helpers: `create` (menu + answers), `options` | domain + title | fully; deletes need `--prune` |
| `integrations.yaml` | integrations that must exist; `create`, `options`, `credentials`, `manual` | domain + title | presence + options; never deleted |
| `dashboards.yaml` | storage dashboards, Lovelace resources | url_path / url | fully; deletes need `--prune` |
| `credentials.yaml` | secrets for config flows (git-crypt; never print it) | key named by `credentials:` | — |

To change any of it: edit the manifest, `hactl plan`, commit, push (deploy applies), or `hactl apply` directly. Never change these things in the HA UI or with ad-hoc API calls — the next apply reverts or flags them.

Cannot be declared: users and long-lived tokens; the human step of interactive integrations (Hue link button, Plex sign-in, phone app registration — `plan` shows their `manual:` text until done); runtime state (history, restore-state).
```

- [ ] **Step 2: `.claude/skills/ha-config/SKILL.md`.** In "The loop", step 2, append: "Registry, area, label, helper, integration, dashboard-list and resource changes are edits to `cluster/home-assistant/state/*.yaml` (see docs/ha.md), checked with `hactl plan` and applied by `hactl apply` / deploy." Replace the `.storage` hard-rule bullet with: "**`.storage` state is declarative** (`cluster/home-assistant/state/`): never change areas, labels, devices, entity names/ids, helpers, integrations' options, storage dashboards or resources in the HA UI or by ad-hoc API calls — edit the manifest and `hactl apply`. Deleting needs `--prune`; integrations are never deleted by hactl. To export a storage dashboard's *contents*: `json.load('/config/.storage/lovelace.lovelace')['data']['config']`."

- [ ] **Step 3: Spec §4.11 as built.** Under the §4.11 table add: "As built (plan 2, 2026-10-03): `create` is `{menu: [step ids], answers: {…}}`; creation answers are options ∪ credentials ∪ `create.answers`. Built-in dashboards `lovelace` and `map` are never created or deleted. `plan` exits 0/2/1; `apply` exits 0 only when converged. Options are read from each entry's options-flow form (`suggested_value`/`default`) and the flow is aborted."

- [ ] **Step 4: Verify and commit.**

Run: `nix develop --command bash -c 'python -m pytest -q tools/hactl | tail -1; hactl --help | grep -E "import|plan|apply"'`
Expected: all pass; three command lines.

```bash
git add docs/ha.md .claude/skills/ha-config/SKILL.md docs/superpowers/specs/2026-10-03-ha-agent-overhaul-design.md
nix develop --command git commit -m "docs: declarative HA state (state/ manifests, import/plan/apply)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push origin main
```

- [ ] **Step 5: Memory.** In `project_ha_agent_tooling.md`, record: plan 2 shipped (date, commits), `state/` manifests are the source of truth for `.storage`, deploy converges them, and plans 3–5 remain.
