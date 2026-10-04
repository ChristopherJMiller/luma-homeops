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
