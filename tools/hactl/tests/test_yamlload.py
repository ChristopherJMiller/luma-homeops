import pytest

from hactl import yamlload
from hactl.errors import HactlError


def test_include_kept_as_tag_by_default(tmp_path):
    (tmp_path / "root.yaml").write_text("views:\n  - !include view.yaml\n")
    data = yamlload.load(tmp_path / "root.yaml")
    assert data["views"][0] == yamlload.Tagged("!include", "view.yaml")


def test_include_resolved_relative_and_nested(tmp_path):
    (tmp_path / "root.yaml").write_text("views:\n  - !include view.yaml\n")
    (tmp_path / "view.yaml").write_text("title: Home\ncards:\n  - !include card.yaml\n")
    (tmp_path / "card.yaml").write_text("type: markdown\n")
    data = yamlload.load(tmp_path / "root.yaml", resolve_includes=True)
    assert data == {"views": [{"title": "Home", "cards": [{"type": "markdown"}]}]}


def test_missing_include_is_explained(tmp_path):
    (tmp_path / "root.yaml").write_text("views:\n  - !include nope.yaml\n")
    with pytest.raises(HactlError, match="nope.yaml"):
        yamlload.load(tmp_path / "root.yaml", resolve_includes=True)


def test_secret_allowed_or_refused(tmp_path):
    (tmp_path / "p.yaml").write_text("x: !secret home_wifi_ssid\n")
    assert yamlload.load(tmp_path / "p.yaml")["x"] == yamlload.Tagged("!secret", "home_wifi_ssid")
    with pytest.raises(HactlError, match="!secret"):
        yamlload.load(tmp_path / "p.yaml", allow_secret=False)


def test_blueprint_input_tags_load(tmp_path):
    (tmp_path / "bp.yaml").write_text(
        "blueprint:\n  name: x\ntriggers:\n  - trigger: state\n    entity_id: !input linked\n"
        "env: !env_var HOME\ndir: !include_dir_named packages/\n"
    )
    data = yamlload.load(tmp_path / "bp.yaml")
    assert data["triggers"][0]["entity_id"] == yamlload.Tagged("!input", "linked")
    assert data["env"] == yamlload.Tagged("!env_var", "HOME")
    assert data["dir"] == yamlload.Tagged("!include_dir_named", "packages/")


def test_load_dir(tmp_path):
    (tmp_path / "a.yaml").write_text("automation: []\n")
    (tmp_path / "b.yaml").write_text("template: []\n")
    assert yamlload.load_dir(tmp_path) == {"a": {"automation": []}, "b": {"template": []}}
