import pytest
from state_fixtures import FakeHA, snapshot, write_manifests

from hactl.errors import HactlError
from hactl.state import apply, diff, importer, model

RETIRED = {"views": [{"title": "Moved", "cards": [{"type": "markdown", "content": "Moved to **Home**."}]}]}


def test_default_dashboard_diff_and_apply():
    snap = snapshot()
    [c] = diff.diff_default_dashboard("home-ops", snap.system_core)
    assert c.data == {"key": "core", "value": {"default_panel": "home-ops"}}
    fake = FakeHA(snap)
    apply.execute(fake, [c], log=lambda s: None)
    assert fake.system_core["default_panel"] == "home-ops"
    assert diff.diff_default_dashboard(None, snap.system_core) == []  # undeclared: unmanaged
    assert diff.diff_default_dashboard("lovelace", snap.system_core) == []


def test_dashboard_config_from_file(tmp_path):
    state = write_manifests(tmp_path, {"dashboards.yaml": (
        "default: home-ops\ndashboards:\n  - {url_path: lovelace, title: Overview, config: overview_retired.yaml}\nresources: []\n")})
    (state / "dashboard_configs").mkdir()
    (state / "dashboard_configs" / "overview_retired.yaml").write_text(
        "views:\n  - title: Moved\n    cards:\n      - type: markdown\n        content: Moved to **Home**.\n")
    m = model.load(state)
    assert m.default_dashboard == "home-ops" and m.dashboard_configs["lovelace"] == RETIRED
    [c] = diff.diff_dashboard_configs(m.dashboard_configs, {"lovelace": {"views": [{"title": "Old"}]}})
    assert c.data == {"url_path": None, "config": RETIRED}  # the default dashboard's url_path is None on the API
    assert diff.diff_dashboard_configs(m.dashboard_configs, {"lovelace": RETIRED}) == []
    fake = FakeHA(snapshot())
    apply.execute(fake, [c], log=lambda s: None)
    assert fake.lovelace_configs[None] == RETIRED


def test_missing_config_file_is_a_problem(tmp_path):
    write_manifests(tmp_path, {"dashboards.yaml": "dashboards:\n  - {url_path: lovelace, title: O, config: nope.yaml}\n"})
    with pytest.raises(HactlError, match="nope.yaml"):
        model.load(tmp_path)


def test_default_must_be_a_string(tmp_path):
    write_manifests(tmp_path, {"dashboards.yaml": "default: [home-ops]\ndashboards: []\n"})
    with pytest.raises(HactlError, match="default must be a dashboard url_path"):
        model.load(tmp_path)


def test_import_only_writes_named_files(tmp_path):
    snap = snapshot()
    importer.write(tmp_path, importer.build(snap))
    (tmp_path / "areas.yaml").write_text("# hand edited\nareas: []\n")
    importer.write(tmp_path, importer.build(snap), only=["people"], force=True)
    assert (tmp_path / "areas.yaml").read_text().startswith("# hand edited")
