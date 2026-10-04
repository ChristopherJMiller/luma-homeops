from state_fixtures import FakeHA, snapshot

from hactl.state import apply, diff, live, model


def plan_for(fake, m):
    return diff.plan(m, live.fetch(fake))


def base_manifest(**over):
    m = model.Manifest(
        areas=[{"id": "bedroom", "name": "Bedroom", "icon": "mdi:bed"}, {"id": "kitchen", "name": "Kitchen", "icon": "mdi:fridge"}],
        integrations=[{"domain": "hue", "title": "Hue Bridge", "manual": "x"}, {"domain": "plex", "title": "Plex", "manual": "x"}],
        helpers=[{"domain": "group", "title": "Bedroom Blinds", "create": {"menu": ["cover"], "answers": {}}}],
        dashboards=[{"url_path": "lovelace", "title": "Overview", "icon": "mdi:view-dashboard"},
                    {"url_path": "claude-preview", "title": "Claude Preview", "icon": "mdi:flask-outline",
                     "require_admin": True, "show_in_sidebar": False}],
        resources=[{"url": "/hacsfiles/mushroom.js", "type": "module"}])
    for k, v in over.items():
        setattr(m, k, v)
    return m


def test_registry_and_device_entity_changes_converge():
    fake = FakeHA(snapshot())
    m = base_manifest(
        labels=[{"id": "lighting", "name": "Lighting", "color": "amber"}],
        areas=[{"id": "bedroom", "name": "Bedroom", "icon": "mdi:bed", "labels": ["lighting"]},
               {"id": "kitchen", "name": "Kitchen", "icon": "mdi:fridge"}, {"id": "office", "name": "Office"}],
        devices=[{"match": {"connections": ["mac", "aa:bb"]}, "area": "office", "name": "Router"}],
        entities=[{"match": {"platform": "mqtt", "unique_id": "0x1_switch"}, "entity_id": "switch.bedroom_light_switch"}],
        remove=[{"platform": "mail_and_packages", "unique_id": "old_1"}])
    result = apply.execute(fake, plan_for(fake, m), log=lambda s: None)
    assert result["errors"] == []
    assert plan_for(fake, m) == []
    assert fake.calls.index("config/label_registry/create") < fake.calls.index("config/area_registry/update")


def test_deletes_only_with_prune():
    fake = FakeHA(snapshot())
    m = base_manifest(areas=[{"id": "bedroom", "name": "Bedroom", "icon": "mdi:bed"}])  # kitchen undeclared
    result = apply.execute(fake, plan_for(fake, m), log=lambda s: None)
    assert result["skipped"] == ["- area kitchen: 'Kitchen' is not declared (needs --prune)"]
    assert any(a["area_id"] == "kitchen" for a in fake.areas)
    apply.execute(fake, plan_for(fake, m), prune=True, log=lambda s: None)
    assert not any(a["area_id"] == "kitchen" for a in fake.areas)


def test_one_failure_does_not_stop_the_rest():
    fake = FakeHA(snapshot())
    changes = [diff.Change("update", "area", "nope", data={"area_id": "nope", "icon": "x"}),  # not in FakeHA -> error
               diff.Change("update", "area", "bedroom", data={"area_id": "bedroom", "icon": "mdi:bed-king"})]
    result = apply.execute(fake, changes, log=lambda s: None)
    assert len(result["errors"]) == 1 and len(result["applied"]) == 1
    assert next(a for a in fake.areas if a["area_id"] == "bedroom")["icon"] == "mdi:bed-king"


def test_registry_id_mismatch_is_an_error():
    fake = FakeHA(snapshot())
    c = diff.Change("create", "label", "lights", data={"id": "lights", "name": "Lighting"})  # HA would make 'lighting'
    result = apply.execute(fake, [c], log=lambda s: None)
    assert "set id: lighting" in result["errors"][0]


def test_manual_changes_are_reported_not_run():
    result = apply.execute(FakeHA(snapshot()), [diff.Change("manual", "integration", "octoprint/OctoPrint", "approve the key")],
                           log=lambda s: None)
    assert result["manual"] == ["! integration octoprint/OctoPrint: approve the key"] and result["applied"] == []


def test_helper_create_runs_its_config_flow(monkeypatch):
    calls = []
    monkeypatch.setattr(apply.flows, "run_config_flow",
                        lambda client, domain, answers, menu: calls.append((domain, answers, menu)) or {"title": "Camp Lamp"})
    c = diff.Change("create", "helper", "switch_as_x/Camp Lamp",
                    data={"domain": "switch_as_x", "title": "Camp Lamp", "answers": {"entity_id": "switch.camp_lamp"}, "menu": []})
    result = apply.execute(FakeHA(snapshot()), [c], log=lambda s: None)
    assert calls == [("switch_as_x", {"entity_id": "switch.camp_lamp"}, [])] and result["notes"] == []


def test_created_title_mismatch_is_a_note(monkeypatch):
    monkeypatch.setattr(apply.flows, "run_config_flow", lambda *a: {"title": "Camp Lamp (light)"})
    c = diff.Change("create", "helper", "switch_as_x/Camp Lamp",
                    data={"domain": "switch_as_x", "title": "Camp Lamp", "answers": {}, "menu": []})
    assert "Camp Lamp (light)" in apply.execute(FakeHA(snapshot()), [c], log=lambda s: None)["notes"][0]
