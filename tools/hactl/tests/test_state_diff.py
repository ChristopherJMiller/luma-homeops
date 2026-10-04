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


from hactl.state import model  # noqa: E402

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
    assert changes[0].data == {"domain": "switch_as_x", "title": "Camp Lamp", "menu": [], "manual": None,
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


def test_emptying_list_fields_sends_empty_lists():
    snap = snapshot()
    snap.areas[0]["labels"] = ["lighting"]
    snap.areas[0]["aliases"] = ["master"]
    [c] = diff.diff_registry("area", [{"id": "bedroom", "name": "Bedroom", "icon": "mdi:bed"},
                                      {"id": "kitchen", "name": "Kitchen", "icon": "mdi:fridge"}], snap.areas)
    assert c.data == {"area_id": "bedroom", "labels": [], "aliases": []}  # HA rejects None for list fields


def test_missing_helper_next_to_an_undeclared_one_of_its_domain_is_manual():
    # A UI rename looks like "declared one missing + undeclared one present": don't create a duplicate.
    renamed = dict(GROUP, title="Blinds")
    changes = entries("helper", [renamed])
    assert [(c.action, c.key) for c in changes] == [("manual", "group/Blinds"), ("delete", "group/Bedroom Blinds")]
    assert "renamed" in changes[0].detail
