import pytest
from state_fixtures import snapshot

from hactl.errors import HactlError
from hactl.state import diff, importer, model


def test_round_trip_plan_is_empty(tmp_path):
    snap = snapshot()
    importer.write(tmp_path, importer.build(snap))
    assert diff.plan(model.load(tmp_path), snap) == []


def test_build_records_overrides_not_defaults():
    files = importer.build(snapshot())
    assert files["devices"]["devices"] == [
        {"match": {"identifiers": ["mqtt", "zigbee2mqtt_0x1"]}, "about": "0x1 · Leviton DG15S", "name": "Bedroom Light Switch"},
        {"match": {"identifiers": ["hue", "lamp-1"]}, "about": "Dresser Lamp · Signify Hue color lamp", "area": "bedroom"},
    ]
    assert files["entities"] == {"entities": [], "remove": []}
    assert [d["url_path"] for d in files["dashboards"]["dashboards"]] == ["claude-preview", "lovelace"]


def test_helpers_get_menu_and_options_integrations_get_manual():
    files = importer.build(snapshot())
    assert files["helpers"]["helpers"] == [{
        "domain": "group", "title": "Bedroom Blinds",
        "create": {"menu": ["cover"], "answers": {"name": "Bedroom Blinds"}},
        "options": {"entities": ["cover.window_left", "cover.window_right"], "hide_members": False},
    }]
    assert [(i["domain"], "Add integration" in i["manual"]) for i in files["integrations"]["integrations"]] == [
        ("hue", True), ("plex", True)]


def test_import_refuses_to_overwrite(tmp_path):
    importer.write(tmp_path, importer.build(snapshot()))
    with pytest.raises(HactlError, match="refusing to overwrite"):
        importer.write(tmp_path, importer.build(snapshot()))
    importer.write(tmp_path, importer.build(snapshot()), force=True)


def test_credentials_never_overwritten(tmp_path):
    (tmp_path / "credentials.yaml").write_text("airnow: {api_key: keep-me}\n")
    importer.write(tmp_path, importer.build(snapshot()))
    assert "keep-me" in (tmp_path / "credentials.yaml").read_text()


def test_numeric_unique_ids_stay_strings(tmp_path):
    snap = snapshot()
    snap.entities.append({"entity_id": "light.x", "platform": "hue", "unique_id": "1741747401508", "name": "X",
                          "icon": None, "area_id": None, "labels": [], "hidden_by": None, "disabled_by": None})
    importer.write(tmp_path, importer.build(snap))
    m = model.load(tmp_path)
    assert m.entities[0]["match"]["unique_id"] == "1741747401508"
    assert diff.plan(m, snap) == []
