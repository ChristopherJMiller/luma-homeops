from datetime import datetime, timedelta, timezone

import pytest

from hactl import query
from hactl.errors import HactlError

T0 = datetime(2026, 10, 3, 15, 21, 34, tzinfo=timezone.utc)

STATES = [
    {"entity_id": "light.floor_lamp", "state": "on", "attributes": {"friendly_name": "Floor Lamp"}},
    {"entity_id": "sensor.yaml_only", "state": "unavailable", "attributes": {"friendly_name": "YAML Only"}},
]
ENTITIES = [
    {"entity_id": "light.floor_lamp", "device_id": "d1", "area_id": None, "platform": "hue", "name": None,
     "original_name": "Floor lamp", "disabled_by": None},
    {"entity_id": "switch.old", "device_id": None, "area_id": "kitchen", "platform": "mqtt", "name": "Old",
     "disabled_by": "user"},
]
DEVICES = [{"id": "d1", "area_id": "bedroom", "name": "Hue lamp", "name_by_user": None}]
AREAS = [{"area_id": "bedroom", "name": "Bedroom"}, {"area_id": "kitchen", "name": "Kitchen"}]


def test_build_index_area_falls_back_to_device_and_keeps_yaml_entities():
    rows = {r.entity_id: r for r in query.build_index(STATES, ENTITIES, DEVICES, AREAS)}
    assert rows["light.floor_lamp"].area == "Bedroom"
    assert rows["light.floor_lamp"].name == "Floor Lamp"
    assert rows["sensor.yaml_only"].state == "unavailable"
    assert rows["switch.old"].disabled


def test_filter_rows():
    rows = query.build_index(STATES, ENTITIES, DEVICES, AREAS)
    assert [r.entity_id for r in query.filter_rows(rows, area="bedroom")] == ["light.floor_lamp"]
    assert [r.entity_id for r in query.filter_rows(rows, unavailable=True)] == ["sensor.yaml_only"]
    assert [r.entity_id for r in query.filter_rows(rows, text="old")] == []
    assert [r.entity_id for r in query.filter_rows(rows, text="old", include_disabled=True)] == ["switch.old"]


def test_parse_since():
    assert query.parse_since("90m") == timedelta(minutes=90)
    assert query.parse_since("3d") == timedelta(days=3)
    with pytest.raises(HactlError):
        query.parse_since("yesterday")


def test_detect_flaps_finds_the_bedroom_burst():
    events = [(T0 + timedelta(seconds=i), "switch.bedroom", "on" if i % 2 else "off") for i in range(18)]
    events += [(T0 + timedelta(hours=h), "light.calm", "on") for h in range(1, 6)]
    bursts = query.detect_flaps(events)
    assert len(bursts) == 1
    assert bursts[0]["entity_id"] == "switch.bedroom"
    assert bursts[0]["changes"] == 18


def test_detect_flaps_quiet():
    events = [(T0 + timedelta(minutes=10 * i), "light.x", "on") for i in range(10)]
    assert query.detect_flaps(events) == []


def test_summarize_trace_orders_steps_and_keeps_errors():
    trace = {
        "run_id": "r1", "script_execution": "error", "error": "SwitchBot Cloud device is offline",
        "timestamp": {"start": "2026-10-03T15:00:00+00:00"}, "trigger": "time pattern",
        "trace": {
            "action/0": [{"path": "action/0", "timestamp": "2026-10-03T15:00:02+00:00", "error": "offline",
                          "result": {"params": {"domain": "cover"}}}],
            "trigger/0": [{"path": "trigger/0", "timestamp": "2026-10-03T15:00:01+00:00"}],
        },
    }
    s = query.summarize_trace(trace)
    assert [st["path"] for st in s["steps"]] == ["trigger/0", "action/0"]
    assert s["steps"][1]["error"] == "offline"
    assert s["error"] == "SwitchBot Cloud device is offline"


def test_format_log():
    entries = [
        {"name": "a", "level": "WARNING", "message": ["w"], "count": 3, "timestamp": 100.0},
        {"name": "b", "level": "ERROR", "message": ["e\nmore"], "count": 9, "timestamp": 200.0},
    ]
    rows = query.format_log(entries, errors_only=True)
    assert [(r["name"], r["count"], r["message"]) for r in rows] == [("b", 9, "e more")]
    assert [r["name"] for r in query.format_log(entries)] == ["b", "a"]
