import argparse
import json

import pytest
from state_fixtures import FakeHA, snapshot, write_manifests

from hactl.errors import HactlError
from hactl.state import cli, importer


def imported(tmp_path, snap):
    importer.write(tmp_path, importer.build(snap))
    return tmp_path


def test_converge_in_sync(tmp_path):
    snap = snapshot()
    snap.options = {}
    fake = FakeHA(snap)
    state = imported(tmp_path, snap)
    assert cli.converge(fake, state_dir=state, log=lambda s: None) == (["state: in sync"], True)


def test_converge_applies_then_reports_leftovers(tmp_path):
    snap = snapshot()
    snap.options = {}
    fake = FakeHA(snap)
    state = imported(tmp_path, snap)
    text = (state / "areas.yaml").read_text().replace("icon: mdi:bed\n", "icon: mdi:bed-king\n")
    (state / "areas.yaml").write_text(text + "- id: office\n  name: Office\n")  # safe_dump block style: items at column 0
    lines, ok = cli.converge(fake, state_dir=state, log=lambda s: None)
    assert ok and lines[0] == "state: applied 2, failed 0"
    assert any(a["area_id"] == "office" for a in fake.areas)


def test_converge_refuses_an_invalid_manifest_before_touching_ha(tmp_path):
    fake = FakeHA(snapshot())
    write_manifests(tmp_path, {"areas.yaml": "areas:\n  - {id: x}\n"})
    with pytest.raises(HactlError, match="missing name"):
        cli.converge(fake, state_dir=tmp_path, log=lambda s: None)
    assert fake.calls == []


def test_plan_json_hides_answers(tmp_path, monkeypatch, capsys):
    snap = snapshot()
    snap.options = {}
    state = imported(tmp_path, snap)
    (state / "credentials.yaml").write_text("airnow: {api_key: super-secret}\n")
    integ = (state / "integrations.yaml").read_text()
    (state / "integrations.yaml").write_text(integ + "- domain: airnow\n  title: AirNow\n  create: {answers: {}}\n  credentials: airnow\n")
    monkeypatch.setattr(cli, "_client", lambda: FakeHA(snap))
    monkeypatch.setattr(cli.model, "STATE_DIR", state)
    rc = cli._plan(argparse.Namespace(json=True))
    out = capsys.readouterr().out
    assert rc == 2 and "super-secret" not in out
    assert json.loads(out)[0]["key"] == "airnow/AirNow"
