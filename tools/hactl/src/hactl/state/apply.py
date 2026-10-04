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
