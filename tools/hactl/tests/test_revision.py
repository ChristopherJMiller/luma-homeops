import re

from hactl import revision


def make_ha(tmp_path):
    for sub in revision.SOURCES:
        (tmp_path / sub).mkdir()
    (tmp_path / "packages" / "a.yaml").write_text("automation: []\n")
    (tmp_path / "dashboards" / "overview.yaml").write_text("views: []\n")
    (tmp_path / "themes" / "t.yaml").write_text("T: {}\n")
    return tmp_path


def test_stable_and_shaped(tmp_path):
    ha = make_ha(tmp_path)
    assert revision.compute(ha) == revision.compute(ha)
    assert re.fullmatch(r"[0-9a-f]{12}", revision.compute(ha))


def test_dashboard_change_changes_revision(tmp_path):
    ha = make_ha(tmp_path)
    before = revision.compute(ha)
    (ha / "dashboards" / "overview.yaml").write_text("views: [{title: x}]\n")
    assert revision.compute(ha) != before


def test_theme_change_changes_revision(tmp_path):
    ha = make_ha(tmp_path)
    before = revision.compute(ha)
    (ha / "themes" / "t.yaml").write_text("T: {a: 1}\n")
    assert revision.compute(ha) != before


def test_revision_file_does_not_hash_itself(tmp_path):
    ha = make_ha(tmp_path)
    before = revision.compute(ha)
    revision.write(ha)
    assert revision.compute(ha) == before


def test_write_is_idempotent(tmp_path):
    ha = make_ha(tmp_path)
    assert revision.write(ha) is True
    assert revision.write(ha) is False
    assert revision.is_current(ha)


def test_parse_roundtrip():
    assert revision.parse(revision.render("0123456789ab")) == "0123456789ab"
    assert revision.parse("no revision here") is None
