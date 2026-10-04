"""Shared fixtures for the state tests: a realistic live snapshot and an in-memory HA."""
import re

from hactl.errors import HactlError
from hactl.state.live import STORAGE_HELPER_DOMAINS, Snapshot


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
        zones=[{"id": "work", "name": "Work", "latitude": 47.64, "longitude": -122.13, "radius": 606.0,
                "icon": "mdi:microsoft-office", "passive": False}],
        persons=[{"id": "chris_m", "name": "Chris", "user_id": "u1", "picture": None,
                  "device_trackers": ["device_tracker.pixel_6_pro", "device_tracker.pixel_9_pro_xl"]}],
        storage_helpers=[],
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
        for name in ("floors", "labels", "areas", "devices", "entities", "entries", "dashboards", "resources",
                     "zones", "persons"):
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
        if t == "zone/list":
            return [dict(x) for x in self.zones]
        if t == "zone/create":
            x = {"id": _slug(args["name"]), **args}
            self.zones.append(x)
            return dict(x)
        if t == "zone/update":
            zone_id = args.pop("zone_id")
            x = next(z for z in self.zones if z["id"] == zone_id)
            x.update(args)
            return dict(x)
        if t == "zone/delete":
            self.zones = [z for z in self.zones if z["id"] != args["zone_id"]]
            return None
        if t == "person/list":
            return {"storage": [dict(x) for x in self.persons], "config": []}
        if t == "person/create":
            x = {"id": _slug(args["name"]), **args}
            self.persons.append(x)
            return dict(x)
        if t == "person/update":
            person_id = args.pop("person_id")
            x = next(q for q in self.persons if q["id"] == person_id)
            x.update(args)
            return dict(x)
        if t.split("/")[0] in STORAGE_HELPER_DOMAINS and t.endswith("/list"):
            return []
        if t == "config/device_registry/list":
            return [dict(x) for x in self.devices]
        if t == "config/device_registry/update":
            device_id = args.pop("device_id")  # pop once: inside the generator it would pop per device
            x = next(d for d in self.devices if d["id"] == device_id)
            x.update(args)
            return dict(x)
        if t == "config/entity_registry/list":
            return [dict(x) for x in self.entities]
        if t == "config/entity_registry/update":
            entity_id = args.pop("entity_id")
            x = next(e for e in self.entities if e["entity_id"] == entity_id)
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
        if t == "config_entries/update":
            entry_id = args.pop("entry_id")
            x = next(e for e in self.entries if e["entry_id"] == entry_id)
            x.update(args)
            return {"config_entry": dict(x)}
        if t == "lovelace/dashboards/list":
            return [dict(x) for x in self.dashboards]
        if t == "lovelace/dashboards/create":
            x = {"id": args["url_path"].replace("-", "_"), **args}
            self.dashboards.append(x)
            return dict(x)
        if t == "lovelace/dashboards/update":
            dashboard_id = args.pop("dashboard_id")
            x = next(d for d in self.dashboards if d.get("id") == dashboard_id)
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
