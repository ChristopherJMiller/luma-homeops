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
