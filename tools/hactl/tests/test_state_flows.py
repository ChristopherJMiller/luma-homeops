import pytest

from hactl.errors import HactlError
from hactl.state import flows

GROUP_MENU = {"type": "menu", "flow_id": "f1", "step_id": "user", "menu_options": ["light", "cover"]}
GROUP_FORM = {"type": "form", "flow_id": "f1", "step_id": "cover", "errors": {}, "data_schema": [
    {"name": "name", "required": True},
    {"name": "entities", "required": True},
    {"name": "hide_members", "required": True, "default": False},
]}
CREATED = {"type": "create_entry", "flow_id": "f1", "title": "Bedroom Blinds", "result": {"entry_id": "e1"}}
OPTIONS_FORM = {"type": "form", "flow_id": "o1", "step_id": "cover", "data_schema": [
    {"name": "entities", "required": True, "description": {"suggested_value": ["cover.a", "cover.b"]}},
    {"name": "hide_members", "required": True, "default": False, "description": {"suggested_value": False}},
]}


class FakeFlow:
    def __init__(self, steps):
        self.steps, self.posts, self.deleted = list(steps), [], []

    def post(self, path, data=None, raw=False):
        self.posts.append((path, data))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def rest(self, method, path, data=None, raw=False, timeout=30):
        assert method == "DELETE"
        self.deleted.append(path)
        return {"message": "Flow aborted"}


def test_current_values_prefers_suggested_value():
    assert flows.current_values(OPTIONS_FORM) == {"entities": ["cover.a", "cover.b"], "hide_members": False}


def test_group_helper_via_menu_then_form():
    fake = FakeFlow([GROUP_MENU, GROUP_FORM, CREATED])
    out = flows.run_config_flow(fake, "group", {"name": "Bedroom Blinds", "entities": ["cover.a"], "unrelated": 1}, ["cover"])
    assert out["title"] == "Bedroom Blinds"
    assert fake.posts == [
        (flows.CONFIG_FLOW, {"handler": "group", "show_advanced_options": True}),
        (f"{flows.CONFIG_FLOW}/f1", {"next_step_id": "cover"}),
        (f"{flows.CONFIG_FLOW}/f1", {"name": "Bedroom Blinds", "entities": ["cover.a"]}),
    ]
    assert fake.deleted == []


def test_missing_answer_names_the_field_and_aborts():
    fake = FakeFlow([GROUP_MENU, GROUP_FORM])
    with pytest.raises(HactlError, match="needs entities"):
        flows.run_config_flow(fake, "group", {"name": "X"}, ["cover"])
    assert fake.deleted == [f"{flows.CONFIG_FLOW}/f1"]


def test_menu_without_declared_choice_aborts():
    fake = FakeFlow([GROUP_MENU])
    with pytest.raises(HactlError, match="declare create.menu"):
        flows.run_config_flow(fake, "group", {})
    assert fake.deleted == [f"{flows.CONFIG_FLOW}/f1"]


def test_failed_answer_aborts_the_flow():
    rejected = dict(GROUP_FORM, errors={"base": "invalid_entity"})
    fake = FakeFlow([GROUP_MENU, GROUP_FORM, rejected])
    with pytest.raises(HactlError, match="rejected the answers"):
        flows.run_config_flow(fake, "group", {"name": "X", "entities": ["cover.nope"]}, ["cover"])
    assert fake.deleted == [f"{flows.CONFIG_FLOW}/f1"]


def test_http_error_mid_flow_aborts():
    fake = FakeFlow([GROUP_MENU, HactlError("POST -> HTTP 400: bad data")])
    with pytest.raises(HactlError, match="HTTP 400"):
        flows.run_config_flow(fake, "group", {}, ["cover"])
    assert fake.deleted == [f"{flows.CONFIG_FLOW}/f1"]


def test_interactive_step_is_manual():
    fake = FakeFlow([{"type": "external", "flow_id": "f9", "step_id": "auth"}])
    with pytest.raises(flows.FlowManual):
        flows.run_config_flow(fake, "plex", {})
    assert fake.deleted == [f"{flows.CONFIG_FLOW}/f9"]


def test_options_flow_keeps_current_values_and_overrides_declared():
    fake = FakeFlow([OPTIONS_FORM, {"type": "create_entry", "flow_id": "o1"}])
    flows.run_options_flow(fake, "e1", {"hide_members": True})
    assert fake.posts[1] == (f"{flows.OPTIONS_FLOW}/o1", {"entities": ["cover.a", "cover.b"], "hide_members": True})


def test_read_options_aborts_and_tolerates_no_options_flow():
    fake = FakeFlow([OPTIONS_FORM])
    assert flows.read_options(fake, "e1") == {"step_id": "cover", "values": {"entities": ["cover.a", "cover.b"], "hide_members": False}}
    assert fake.deleted == [f"{flows.OPTIONS_FLOW}/o1"]
    assert flows.read_options(FakeFlow([HactlError("HTTP 400")]), "e2") is None
