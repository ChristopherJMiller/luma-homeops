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
