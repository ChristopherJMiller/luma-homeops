import pytest

from hactl import preview
from hactl.errors import HactlError


def test_resolve_includes(tmp_path):
    (tmp_path / "overview.yaml").write_text("views:\n  - !include overview_home.yaml\n")
    (tmp_path / "overview_home.yaml").write_text("title: Home\ncards: []\n")
    assert preview.resolve(tmp_path / "overview.yaml") == {"views": [{"title": "Home", "cards": []}]}


def test_secret_rejected(tmp_path):
    (tmp_path / "d.yaml").write_text("views:\n  - title: !secret x\n")
    with pytest.raises(HactlError, match="!secret"):
        preview.resolve(tmp_path / "d.yaml")


def test_not_a_dashboard(tmp_path):
    (tmp_path / "d.yaml").write_text("automation: []\n")
    with pytest.raises(HactlError, match="views"):
        preview.resolve(tmp_path / "d.yaml")


def test_unsupported_tag(tmp_path):
    (tmp_path / "d.yaml").write_text("views:\n  - title: !env_var HOME\n")
    with pytest.raises(HactlError, match="!env_var"):
        preview.resolve(tmp_path / "d.yaml")


def test_unquoted_date_explained(tmp_path):
    (tmp_path / "d.yaml").write_text("views:\n  - title: 2026-10-03\n")
    with pytest.raises(HactlError, match="quote"):
        preview.resolve(tmp_path / "d.yaml")
