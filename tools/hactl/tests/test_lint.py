from hactl import lint, revision


def make_ha(tmp_path, files):
    for sub in revision.SOURCES:
        (tmp_path / sub).mkdir(exist_ok=True)
    for rel, text in files.items():
        (tmp_path / rel).write_text(text)
    listed = "".join(f"      - {r}\n" for r in files if r.split("/")[0] in revision.SOURCES)
    (tmp_path / "kustomization.yaml").write_text(f"configMapGenerator:\n  - name: ha-packages\n    files:\n{listed}")
    return tmp_path


def rules(findings):
    return sorted((f.rule, f.line) for f in findings)


def test_hyphenated_package_filename(tmp_path):
    ha = make_ha(tmp_path, {"packages/morning-routine.yaml": "automation: []\n"})
    assert rules(lint.check_filenames(ha)) == [("filename", None)]


def test_unlisted_and_missing_kustomization_entries(tmp_path):
    ha = make_ha(tmp_path, {"packages/a.yaml": "automation: []\n"})
    (ha / "packages" / "b.yaml").write_text("automation: []\n")
    (ha / "kustomization.yaml").write_text(
        "configMapGenerator:\n  - name: ha-packages\n    files:\n      - packages/a.yaml\n      - packages/gone.yaml\n"
    )
    msgs = [f.message for f in lint.check_kustomization(ha)]
    assert any("never reach HA" in m for m in msgs)
    assert any("gone.yaml" in m for m in msgs)


def test_quoting(tmp_path):
    ha = make_ha(tmp_path, {"packages/a.yaml": (
        "automation:\n"
        "  - triggers:\n"
        "      - trigger: state\n"
        "        to: on\n"          # line 4: bad
        "        from: 'off'\n"     # fine
        "        for: 03:00:00\n"   # line 6: bad
        "    at: \"22:30:00\"\n"    # fine
    )})
    assert rules(lint.check_quoting(ha)) == [("quoting", 4), ("quoting", 6)]


def test_stale_revision(tmp_path):
    ha = make_ha(tmp_path, {"packages/a.yaml": "automation: []\n"})
    assert rules(lint.check_revision(ha)) == [("revision", None)]
    revision.write(ha)
    assert lint.check_revision(ha) == []


def test_deployed_tag(tmp_path):
    f = tmp_path / "release.yaml"
    f.write_text("valuesObject:\n  image:\n    tag: 2026.7.2\n")
    assert lint.deployed_tag(f) == "2026.7.2"


def test_finding_renders_location():
    assert "packages/a.yaml:4" in str(lint.Finding("quoting", "packages/a.yaml", 4, "x"))


def test_check_config_restores_ownership_portably(tmp_path):
    # Rootless docker maps container root to the host user; rootful maps it to
    # host root. Chowning to the mount's in-container owner is right for both;
    # chowning to the host uid (as seen from outside) is wrong under rootless.
    cmd = lint.check_config_command(tmp_path, "2026.7.2")
    script = cmd[-1]
    assert 'chown -R "$(stat -c %u:%g /config)" /config' in script
    assert f"{tmp_path}:/config" in cmd
    assert "docker.io/homeassistant/home-assistant:2026.7.2" in cmd
