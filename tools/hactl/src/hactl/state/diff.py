"""Compare declared state (model.Manifest) with live HA (live.Snapshot) -> ordered Changes."""
from dataclasses import dataclass, field

from hactl.state.model import BUILTIN_DASHBOARDS, HELPER_DOMAINS

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


LIST_FIELDS = frozenset({"labels", "aliases"})  # HA's schema wants [] for these, never None


def diff_registry(kind, declared, live) -> list:
    """Fully managed: declared fields enforced (omitted = empty); undeclared live ones are prunable."""
    id_key, fields = REGISTRY[kind]
    by_id = {x[id_key]: x for x in live}
    out = []
    for d in declared:
        want = {lf: (d.get(mf) or []) if mf in LIST_FIELDS else d.get(mf) for mf, lf in fields.items()}
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


ZONE_FIELDS = ("name", "latitude", "longitude", "radius", "icon", "passive")
ZONE_DEFAULTS = {"radius": 100, "icon": None, "passive": False}
PERSON_FIELDS = ("name", "user_id", "device_trackers", "picture")


def diff_zones(declared, live) -> list:
    """Fully managed: undeclared zones are prunable."""
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


def _person_value(d, f):
    return d.get(f, [] if f == "device_trackers" else None)


def diff_persons(declared, live) -> list:
    """Persons are never deleted by hactl: an undeclared one is manual."""
    by_id = {p["id"]: p for p in live}
    out = []
    for d in declared:
        cur = by_id.get(d["id"])
        if cur is None:
            out.append(Change("create", "person", d["id"], repr(d["name"]),
                              data={"id": d["id"], **{f: _person_value(d, f) for f in PERSON_FIELDS}}))
            continue
        delta = {f: _person_value(d, f) for f in PERSON_FIELDS if not same(_person_value(d, f), cur.get(f))}
        if delta:
            out.append(Change("update", "person", d["id"], _describe(delta, cur), data={"person_id": d["id"], **delta}))
    declared_ids = {d["id"] for d in declared}
    out += [Change("manual", "person", p["id"], "is not declared in people.yaml (declare it; hactl never deletes a person)")
            for p in live if p["id"] not in declared_ids]
    return out


def diff_storage_helpers(storage_helpers) -> list:
    return [Change("manual", "ui-helper", f"{d}.{i}", f"UI-made {d} {n!r}: move it into a package, then delete it in HA")
            for d, i, n in storage_helpers]


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
    """Manifests hold identifiers as strings; some integrations report ints (SmartRent: ['id', 676365])."""
    (kind, pair), = match.items()
    want = [str(x) for x in pair]
    for dev in devices:
        if want in [[str(y) for y in x] for x in dev.get(kind) or []]:
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


def diff_config_entries(kind, declared, entries, options, credentials, credentials_locked=False) -> list:
    """Helpers: fully managed (prunable). Integrations: presence + options, never deleted."""
    helper = kind == "helper"
    by_key = {}
    for e in entries:
        if (e["domain"] in HELPER_DOMAINS) == helper:
            by_key.setdefault((e["domain"], e["title"]), []).append(e)
    declared_keys = {(d["domain"], d["title"]) for d in declared}
    out = []
    for d in declared:
        key, name = (d["domain"], d["title"]), f"{d['domain']}/{d['title']}"
        found = by_key.get(key, [])
        if len(found) > 1:
            out.append(Change("manual", kind, name, f"{len(found)} entries share this domain and title; rename one in HA"))
            continue
        strays = sorted(t for (dom, t) in by_key if dom == d["domain"] and (dom, t) not in declared_keys)
        if not found and helper and strays:
            out.append(Change("manual", kind, name, f"not found, but undeclared {d['domain']} helper(s) exist "
                              f"({', '.join(strays)}): if one is this helper renamed in HA, set title: to its name; "
                              "otherwise remove the stray with --prune"))
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
                                        "menu": d["create"].get("menu", []), "manual": d.get("manual")}))
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


def diff_default_dashboard(declared, system_core) -> list:
    if declared is None or system_core.get("default_panel") == declared:
        return []
    return [Change("update", "default-dashboard", "core", f"default_panel {system_core.get('default_panel')!r} -> {declared!r}",
                   data={"key": "core", "value": {**system_core, "default_panel": declared}})]


def diff_dashboard_configs(declared, live) -> list:
    return [Change("update", "dashboard-config", u, "contents differ from the declared file",
                   data={"url_path": None if u == "lovelace" else u, "config": cfg})
            for u, cfg in sorted(declared.items()) if live.get(u) != cfg]


def plan(m, snap) -> list:
    """Every change needed to make HA match the manifests, in apply order."""
    return (diff_registry("floor", m.floors, snap.floors)
            + diff_registry("label", m.labels, snap.labels)
            + diff_registry("area", m.areas, snap.areas)
            + diff_zones(m.zones, snap.zones)
            + diff_persons(m.persons, snap.persons)
            + diff_storage_helpers(snap.storage_helpers)
            + diff_config_entries("helper", m.helpers, snap.entries, snap.options, m.credentials, m.credentials_locked)
            + diff_config_entries("integration", m.integrations, snap.entries, snap.options, m.credentials, m.credentials_locked)
            + diff_devices(m.devices, snap.devices)
            + diff_entities(m.entities, m.remove, snap.entities)
            + diff_dashboards(m.dashboards, snap.dashboards)
            + diff_resources(m.resources, snap.resources)
            + diff_default_dashboard(m.default_dashboard, snap.system_core)
            + diff_dashboard_configs(m.dashboard_configs, snap.dashboard_configs))


def options_wanted(m) -> set:
    return {(x["domain"], x["title"]) for x in m.helpers + m.integrations if "options" in x}
