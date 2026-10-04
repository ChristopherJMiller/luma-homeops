import pytest
from state_fixtures import FakeHA, snapshot

from hactl.errors import HactlError
from hactl.state import apply, diff, importer, live, model

WORK = {"id": "work", "name": "Work", "latitude": 10.5, "longitude": 20.25, "radius": 300, "icon": "mdi:briefcase"}
CHRIS = {"id": "chris_m", "name": "Chris", "user_id": "u1", "device_trackers": ["device_tracker.pixel_9_pro_xl"]}


def test_zone_and_person_in_sync_and_drift():
    snap = snapshot()
    assert diff.diff_zones([WORK], snap.zones) == []
    [c] = diff.diff_zones([dict(WORK, radius=450)], snap.zones)
    assert (c.action, c.data) == ("update", {"zone_id": "work", "radius": 450})
    [c] = diff.diff_persons([CHRIS], snap.persons)
    assert c.data == {"person_id": "chris_m", "device_trackers": ["device_tracker.pixel_9_pro_xl"]}  # drops pixel_6_pro


def test_undeclared_zone_prunable_person_never_deleted():
    snap = snapshot()
    assert [(c.action, c.prune) for c in diff.diff_zones([], snap.zones)] == [("delete", True)]
    [c] = diff.diff_persons([], snap.persons)
    assert c.action == "manual" and "not declared" in c.detail


def test_ui_made_helpers_are_manual():
    snap = snapshot()
    snap.storage_helpers = [("input_boolean", "guest", "Guest")]
    [c] = diff.diff_storage_helpers(snap.storage_helpers)
    assert c.action == "manual" and "move it into a package" in c.detail


def test_round_trip_and_apply(tmp_path):
    snap = snapshot()
    snap.options = {}
    importer.write(tmp_path, importer.build(snap))
    assert diff.plan(model.load(tmp_path), snap) == []
    fake = FakeHA(snap)
    m = model.load(tmp_path)
    m.zones.append({"id": "gym", "name": "Gym", "latitude": 47.6, "longitude": -122.3})
    result = apply.execute(fake, diff.plan(m, live.fetch(fake)), log=lambda s: None)
    assert result["errors"] == [] and any(z["id"] == "gym" for z in fake.zones)
    assert diff.plan(m, live.fetch(fake)) == []


def test_people_validation(tmp_path):
    (tmp_path / "people.yaml").write_text("zones:\n  - {id: w, name: W, latitude: north, longitude: 1}\npersons: []\n")
    with pytest.raises(HactlError, match="latitude must be a number"):
        model.load(tmp_path)


def test_non_slug_person_id_is_flagged_not_recreated_on_every_apply():
    fake = FakeHA(snapshot())
    declared = [CHRIS, {"id": "erin_m", "name": "Erin"}]   # HA will create Erin as id "erin"
    for _ in range(3):
        changes = diff.diff_persons(declared, live.fetch(fake).persons)
        apply.execute(fake, changes, log=lambda s: None)
    assert [p["name"] for p in fake.persons].count("Erin") == 1
    [c] = diff.diff_persons(declared, live.fetch(fake).persons)
    assert c.action == "manual" and "set id: erin" in c.detail


def test_non_slug_zone_id_is_flagged_not_recreated():
    fake = FakeHA(snapshot())
    gym = {"id": "gym_1", "name": "Gym", "latitude": 1.0, "longitude": 2.0, "radius": 100}
    for _ in range(3):
        apply.execute(fake, diff.diff_zones([WORK, gym], live.fetch(fake).zones), log=lambda s: None)
    assert [z["name"] for z in fake.zones].count("Gym") == 1
    changes = diff.diff_zones([WORK, gym], live.fetch(fake).zones)
    assert [(c.action, "set id: gym" in c.detail) for c in changes] == [("manual", True)]
