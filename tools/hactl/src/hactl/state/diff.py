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
