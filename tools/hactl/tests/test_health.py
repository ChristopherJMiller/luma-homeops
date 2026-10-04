from hactl import health


def t(item, run, execution, start):
    return {"item_id": item, "run_id": run, "script_execution": execution, "timestamp": {"start": start}}


def test_latest_error_run_is_failing():
    traces = [t("blinds", "1", "finished", "2026-10-03T10:00"), t("blinds", "2", "error", "2026-10-03T11:00")]
    assert [f["item_id"] for f in health.failing_automations(traces)] == ["blinds"]


def test_recovered_automation_is_not_failing():
    traces = [t("blinds", "1", "error", "2026-10-03T10:00"), t("blinds", "2", "finished", "2026-10-03T11:00")]
    assert health.failing_automations(traces) == []


def test_cancelled_runs_are_not_failures():
    traces = [t("bedroom_sync", "1", "cancelled", "2026-10-03T10:00"), t("night", "2", "failed_conditions", "2026-10-03T10:00")]
    assert health.failing_automations(traces) == []


def test_declared_domains():
    assert health.declared_domains({"a": {"automation": [], "prometheus": {}}, "b": {"template": []}, "c": None}) == {
        "automation", "prometheus", "template"}


def test_drift_clean():
    assert health.drift("abc", "abc", {"automation"}, {"automation", "light"}) == []


def test_drift_reports_each_problem():
    msgs = health.drift("abc", "old", {"automation", "prometheus"}, {"automation"})
    assert any("old" in m and "abc" in m for m in msgs)
    assert any("prometheus" in m for m in msgs)
    assert any("never loaded" in m for m in health.drift("abc", None, set(), set()))


def test_render_problem_flags():
    base = {"drift": [], "hook": "succeeded", "failing": [], "repairs": [], "log_errors": [], "unavailable": ["binary_sensor.fridge_door_contact"]}
    lines, problem = health.render(base)
    assert not problem  # unavailable entities are warnings, not problems
    assert any("fridge_door_contact" in line for line in lines)
    assert health.render(dict(base, hook="failed"))[1]
    assert health.render(dict(base, drift=["x"]))[1]


def test_aborted_by_a_condition_is_not_failing():
    # HA marks a run "aborted" when an action-sequence condition is false: a normal early exit.
    traces = [t("night", "1", "aborted", "2026-10-03T10:00")]
    detail = {"trace": {"action/0": [{"path": "action/0", "error": None, "result": {"result": False}}]}}
    assert health.failing_automations(traces, get_trace=lambda item, run: detail) == []


def test_aborted_with_an_error_is_failing():
    traces = [t("blinds", "1", "aborted", "2026-10-03T10:00")]
    detail = {"trace": {"action/0": [{"path": "action/0", "error": "UndefinedError: 'x' is undefined"}]}}
    assert [f["item_id"] for f in health.failing_automations(traces, get_trace=lambda item, run: detail)] == ["blinds"]


def test_state_drift_is_a_problem_not_loaded_is_a_warning():
    base = {"drift": [], "hook": "succeeded", "failing": [], "repairs": [], "log_errors": [], "unavailable": [],
            "state": [], "not_loaded": ["plex/Plex: setup_retry (timeout)"]}
    lines, problem = health.render(base)
    assert not problem and any("plex/Plex" in line for line in lines)
    lines, problem = health.render(dict(base, state=["+ area office: 'Office'"]))
    assert problem and any("+ area office" in line for line in lines)


def test_not_loaded_entries():
    entries = [{"domain": "plex", "title": "Plex", "state": "setup_retry", "reason": "timeout", "disabled_by": None},
               {"domain": "hue", "title": "Hue", "state": "loaded", "reason": None, "disabled_by": None},
               {"domain": "x", "title": "Off", "state": "not_loaded", "reason": None, "disabled_by": "user"}]
    assert health.not_loaded(entries) == ["plex/Plex: setup_retry (timeout)"]


def test_registry_report_lists_arealess_restored_and_long_unavailable():
    from hactl import health
    devices = [
        {"id": "d1", "name": "Plant Blinds", "area_id": None, "entry_type": None, "disabled_by": None},
        {"id": "d2", "name": "Lamp", "area_id": "bedroom", "entry_type": None, "disabled_by": None},
        {"id": "d3", "name": "Sun", "area_id": None, "entry_type": "service", "disabled_by": None},
        {"id": "d4", "name": "Old", "area_id": None, "entry_type": None, "disabled_by": "user"},
        {"id": "d5", "name": "Ghost", "area_id": None, "entry_type": None, "disabled_by": None},
    ]
    entities = [
        {"entity_id": "cover.plant_blinds", "device_id": "d1", "disabled_by": None},
        {"entity_id": "light.lamp", "device_id": "d2", "disabled_by": None},
        {"entity_id": "sensor.sun", "device_id": "d3", "disabled_by": None},
        {"entity_id": "sensor.ghost", "device_id": "d5", "disabled_by": "integration"},
        {"entity_id": "sensor.mail_old", "device_id": None, "disabled_by": None},
    ]
    states = {"sensor.mail_old": {"state": "unavailable", "attributes": {"restored": True}},
              "cover.plant_blinds": {"state": "open", "attributes": {}}}
    r = health.registry_report(devices, entities, states, ["sensor.fridge"])
    assert r == {"no_area": ["Plant Blinds"], "restored": ["sensor.mail_old"], "long_unavailable": ["sensor.fridge"]}


def test_long_unavailable_uses_history():
    from hactl import health

    class C:
        def get(self, path, raw=False):
            assert "filter_entity_id=sensor.a,sensor.b" in path and "no_attributes" in path
            assert "end_time=" in path  # HA defaults end_time to start + 1 day
            return [[{"entity_id": "sensor.a", "state": "unavailable"}],
                    [{"entity_id": "sensor.b", "state": "unavailable"}, {"state": "on"}]]
    assert health.long_unavailable(C(), ["sensor.a", "sensor.b"]) == ["sensor.a"]
    assert health.long_unavailable(C(), []) == []


def test_unknown_is_not_dead():
    # buttons, scenes, notify, tts, events sit at `unknown` until used: only `unavailable` means dead
    from hactl import health

    class C:
        def get(self, path, raw=False):
            return [[{"entity_id": "button.restart", "state": "unknown"}],
                    [{"entity_id": "sensor.fridge", "state": "unavailable"}]]
    assert health.long_unavailable(C(), ["button.restart", "sensor.fridge"]) == ["sensor.fridge"]
