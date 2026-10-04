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


def test_entity_refs_unknown_vs_declared(tmp_path):
    ha = make_ha(tmp_path, {
        "packages/a.yaml": (
            "input_boolean:\n  guest_mode: {}\n"
            "automation:\n  - alias: X\n    triggers:\n      - trigger: state\n"
            "        entity_id: input_boolean.guest_mode\n"
            "    actions:\n      - action: light.turn_on\n        target:\n          entity_id: light.missing_lamp\n"
        ),
        "dashboards/d.yaml": "views:\n  - cards:\n      - entity: sun.sun\n",
    })
    found = lint.check_entity_refs(ha, known={"sun.sun"})
    assert [(f.rule, f.message.split()[0], f.line) for f in found] == [("entity-ref", "light.missing_lamp", 11)]
    assert lint.check_entity_refs(ha, known={"sun.sun"}, allow={"light.missing_lamp"}) == []


def test_allow_missing_file(tmp_path):
    f = tmp_path / "lint-allow-missing.txt"
    f.write_text("# integrations still to add\nweather.toronto  # plan 3: met.no Toronto\n\nsensor.toronto_work_commute\n")
    assert lint.load_allow_missing(f) == {"weather.toronto", "sensor.toronto_work_commute"}
    assert lint.load_allow_missing(tmp_path / "absent.txt") == set()


def test_template_strings_and_render_errors(tmp_path):
    ha = make_ha(tmp_path, {"dashboards/d.yaml": (
        "views:\n  - cards:\n"
        "      - primary: \"{{ states('sun.sun') }}\"\n"
        "      - primary: \"{{ as_timestamp(None) }}\"\n"
        "      - primary: plain text\n"
    )})

    def render(text):
        if "None" in text:
            raise lint.HactlError("Template error: as_timestamp got invalid input")

    found = lint.check_dashboard_templates(ha, render)
    assert [(f.rule, f.line) for f in found] == [("dashboard-template", 4)]


def test_state_manifest_problems_are_lint_findings(tmp_path):
    ha = make_ha(tmp_path, {"packages/a.yaml": "automation: []\n"})
    (ha / "state").mkdir()
    (ha / "state" / "areas.yaml").write_text("areas:\n  - {id: x, name: X, floor: nowhere}\n")
    found = lint.check_state(ha)
    assert [f.rule for f in found] == ["state"] and "floor 'nowhere' is not declared" in found[0].message


def test_custom_components_come_from_release_values(tmp_path):
    f = tmp_path / "release.yaml"
    f.write_text("spec:\n  source:\n    helm:\n      valuesObject:\n        components:\n          integrations:\n"
                 "            - {name: adaptive_lighting, repo: basnijholt/adaptive-lighting, version: v1.31.0}\n")
    assert lint.custom_components(f) == [("adaptive_lighting", "basnijholt/adaptive-lighting", "v1.31.0")]


def test_no_components_block_means_none(tmp_path):
    f = tmp_path / "release.yaml"
    f.write_text("spec:\n  source:\n    helm:\n      valuesObject:\n        hacs: true\n")
    assert lint.custom_components(f) == []


def test_components_are_cloned_at_their_pinned_tag(tmp_path):
    assert lint.clone_command("basnijholt/adaptive-lighting", "v1.31.0", tmp_path / "src") == [
        "git", "-c", "advice.detachedHead=false", "clone", "-q", "--depth", "1", "--branch", "v1.31.0",
        "https://github.com/basnijholt/adaptive-lighting", str(tmp_path / "src")]


RELEASE_WITH_CARD = ("spec:\n  source:\n    helm:\n      valuesObject:\n        components:\n          cards:\n"
                     '            - {name: lovelace-mushroom, repo: piitaya/lovelace-mushroom, version: v5.1.1,'
                     ' url: "https://github.com/piitaya/lovelace-mushroom/releases/download/{version}/mushroom.js"}\n')


def test_card_resource_matches_pinned_version(tmp_path):
    rel = tmp_path / "release.yaml"
    rel.write_text(RELEASE_WITH_CARD)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "dashboards.yaml").write_text(
        "resources:\n  - url: /local/community/lovelace-mushroom/mushroom.js?v=v5.1.1\n    type: module\n")
    assert lint.check_card_resources(tmp_path, rel) == []


def test_card_bumped_without_its_resource_is_a_finding(tmp_path):
    rel = tmp_path / "release.yaml"
    rel.write_text(RELEASE_WITH_CARD.replace("v5.1.1", "v5.2.0"))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "dashboards.yaml").write_text(
        "resources:\n  - url: /local/community/lovelace-mushroom/mushroom.js?v=v5.1.1\n    type: module\n")
    msgs = [f.message for f in lint.check_card_resources(tmp_path, rel)]
    assert any("/local/community/lovelace-mushroom/mushroom.js?v=v5.2.0" in m for m in msgs)  # the resource to declare
    assert any("?v=v5.1.1" in m for m in msgs)  # the stale one
