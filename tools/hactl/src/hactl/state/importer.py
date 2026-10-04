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
