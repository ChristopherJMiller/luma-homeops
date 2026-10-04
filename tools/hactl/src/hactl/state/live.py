"""A snapshot of the .storage parts of HA that the state manifests describe."""
from dataclasses import dataclass, field

from hactl.state import flows

STORAGE_HELPER_DOMAINS = ("input_boolean", "input_number", "input_select", "input_text", "input_datetime",
                          "input_button", "counter", "timer", "schedule")


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
    zones: list = field(default_factory=list)
    persons: list = field(default_factory=list)  # person/list "storage" entries
    storage_helpers: list = field(default_factory=list)  # (domain, id, name) of UI-made helpers


def fetch(client, want_options=frozenset()) -> Snapshot:
    floors, labels, areas, devices, entities, entries, dashboards, resources, zones, persons, *helper_lists = client.ws(
        {"type": "config/floor_registry/list"}, {"type": "config/label_registry/list"},
        {"type": "config/area_registry/list"}, {"type": "config/device_registry/list"},
        {"type": "config/entity_registry/list"}, {"type": "config_entries/get"},
        {"type": "lovelace/dashboards/list"}, {"type": "lovelace/resources"},
        {"type": "zone/list"}, {"type": "person/list"},
        *[{"type": f"{d}/list"} for d in STORAGE_HELPER_DOMAINS],
    )
    storage_helpers = [(d, x.get("id"), x.get("name")) for d, items in zip(STORAGE_HELPER_DOMAINS, helper_lists) for x in items or []]
    options = {}
    for e in entries:
        key = (e["domain"], e["title"])
        if key in want_options:
            options[key] = flows.read_options(client, e["entry_id"])
    return Snapshot(floors, labels, areas, devices, entities, entries, dashboards, resources, options,
                    zones, (persons or {}).get("storage", []), storage_helpers)
