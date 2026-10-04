import pytest
from state_fixtures import FakeHA, snapshot

from hactl.errors import HactlError
from hactl.state import apply, diff, importer, live, model

WORK = {"id": "work", "name": "Work", "latitude": 47.64, "longitude": -122.13, "radius": 606, "icon": "mdi:microsoft-office"}
CHRIS = {"id": "chris_m", "name": "Chris", "user_id": "u1", "device_trackers": ["device_tracker.pixel_9_pro_xl"]}


def test_zone_and_person_in_sync_and_drift():
    snap = snapshot()
    assert diff.diff_zones([WORK], snap.zones) == []
    [c] = diff.diff_zones([dict(WORK, radius=300)], snap.zones)
    assert (c.action, c.data) == ("update", {"zone_id": "work", "radius": 300})
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
