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
