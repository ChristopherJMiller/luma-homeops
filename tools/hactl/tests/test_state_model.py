import pytest

from hactl.errors import HactlError
from hactl.state import model


def write(d, files):
    for name, text in files.items():
        (d / name).write_text(text)
    return d


GOOD = {
    "areas.yaml": (
        "floors:\n  - {id: main, name: Main, level: 0}\n"
        "labels:\n  - {id: lighting, name: Lighting, color: amber}\n"
        "areas:\n  - {id: bedroom, name: Bedroom, floor: main, icon: mdi:bed, labels: [lighting]}\n"
    ),
    "devices.yaml": "devices:\n  - match: {identifiers: [hue, abc]}\n    area: bedroom\n    about: Dresser Lamp\n",
    "entities.yaml": (
        "entities:\n  - match: {platform: mqtt, unique_id: '0x1_switch'}\n    entity_id: switch.bedroom_light_switch\n    hidden: false\n"
        "remove:\n  - {platform: mail_and_packages, unique_id: old_1}\n"
    ),
    "helpers.yaml": (
        "helpers:\n  - domain: group\n    title: Bedroom Blinds\n    create: {menu: [cover], answers: {name: Bedroom Blinds}}\n"
        "    options: {entities: [cover.window_left], hide_members: false}\n"
    ),
    "integrations.yaml": (
        "integrations:\n  - {domain: hue, title: Hue Bridge, manual: press the link button}\n"
        "  - domain: airnow\n    title: AirNow\n    create: {answers: {radius: 150}}\n    credentials: airnow\n"
    ),
    "dashboards.yaml": (
        "dashboards:\n  - {url_path: claude-preview, title: Claude Preview, require_admin: true, show_in_sidebar: false}\n"
        "  - {url_path: map, title: Map}\n"
        "resources:\n  - {url: /local/x.js, type: module}\n"
    ),
    "credentials.yaml": "airnow: {api_key: not-a-real-key}\n",
}


def test_empty_dir_is_an_empty_manifest(tmp_path):
    m = model.load(tmp_path)
    assert m.areas == [] and m.integrations == [] and m.credentials == {} and not m.credentials_locked


def test_good_manifests_load(tmp_path):
    m = model.load(write(tmp_path, GOOD))
    assert [a["id"] for a in m.areas] == ["bedroom"]
    assert m.devices[0]["match"] == {"identifiers": ["hue", "abc"]}
    assert m.remove == [{"platform": "mail_and_packages", "unique_id": "old_1"}]
    assert m.credentials["airnow"]["api_key"] == "not-a-real-key"


def test_all_problems_reported_together(tmp_path):
    bad = dict(GOOD)
    bad["areas.yaml"] = "floors: []\nlabels: []\nareas:\n  - {id: bedroom, name: Bedroom, floor: upstairs, colour: red}\nrooms: []\n"
    bad["devices.yaml"] = "devices:\n  - match: {identifiers: [hue]}\n    area: garage\n    labels: [lighting]\n"
    bad["dashboards.yaml"] = "dashboards:\n  - {url_path: preview, title: P}\nresources:\n  - {url: /x.js, type: script}\n"
    with pytest.raises(HactlError) as e:
        model.load(write(tmp_path, bad))
    msg = str(e.value)
    for needle in ["unknown section 'rooms'", "unknown key(s) colour", "floor 'upstairs' is not declared",
                   "match must be", "area 'garage' is not declared", "url_path must contain a hyphen",
                   "type must be one of", "label(s) lighting not declared"]:
        assert needle in msg, needle


def test_entity_in_both_lists_and_wrong_file(tmp_path):
    bad = dict(GOOD)
    bad["entities.yaml"] = (
        "entities:\n  - match: {platform: mqtt, unique_id: u1}\n    name: X\n"
        "remove:\n  - {platform: mqtt, unique_id: u1}\n"
    )
    bad["integrations.yaml"] = "integrations:\n  - {domain: group, title: G, manual: x}\n"
    with pytest.raises(HactlError) as e:
        model.load(write(tmp_path, bad))
    assert "mqtt/u1 is declared more than once" in str(e.value)
    assert "belongs in helpers.yaml" in str(e.value)


def test_missing_credentials_reference(tmp_path):
    bad = dict(GOOD, **{"credentials.yaml": "other: {x: 1}\n"})
    with pytest.raises(HactlError, match="credentials 'airnow' not in credentials.yaml"):
        model.load(write(tmp_path, bad))


def test_locked_credentials_skip_reference_checks(tmp_path):
    write(tmp_path, {k: v for k, v in GOOD.items() if k != "credentials.yaml"})
    (tmp_path / "credentials.yaml").write_bytes(b"\x00GITCRYPT\x00ciphertext")
    m = model.load(tmp_path)
    assert m.credentials_locked and m.credentials == {}


def test_type_mistakes_are_problems_not_tracebacks(tmp_path):
    bad = dict(GOOD)
    bad["areas.yaml"] = "floors: []\nlabels: []\nareas:\n  - {id: bedroom, name: Bedroom, floor: [x]}\n"
    bad["dashboards.yaml"] = "dashboards:\n  - {url_path: a-b, title: {a: b}}\nresources:\n  - {url: /x.js, type: [module]}\n"
    bad["integrations.yaml"] = "integrations:\n  - {domain: hue, title: Hue, credentials: {k: v}}\n"
    with pytest.raises(HactlError) as e:
        model.load(write(tmp_path, bad))
    msg = str(e.value)
    for needle in ["floor must be a string", "title must be a string", "type must be a string", "credentials must be a string"]:
        assert needle in msg, needle


def test_unquoted_dates_in_answers_are_problems(tmp_path):
    bad = dict(GOOD)
    bad["integrations.yaml"] = "integrations:\n  - domain: workday\n    title: W\n    create: {answers: {add_holidays: [2026-12-24]}}\n"
    with pytest.raises(HactlError, match="quote it"):
        model.load(write(tmp_path, bad))


def test_credentials_syntax_error_never_echoes_the_secret(tmp_path):
    write(tmp_path, {k: v for k, v in GOOD.items() if k != "credentials.yaml"})
    (tmp_path / "credentials.yaml").write_text("airnow:\n  api_key: SECRETVALUE: oops\n")
    with pytest.raises(HactlError) as e:
        model.load(tmp_path)
    assert "SECRETVALUE" not in str(e.value) and "not valid YAML" in str(e.value)


def test_encrypted_manifest_is_skipped_and_flagged(tmp_path):
    write(tmp_path, GOOD)
    (tmp_path / "people.yaml").write_bytes(b"\x00GITCRYPT\x00ciphertext")
    m = model.load(tmp_path)
    assert m.locked_files == ["people.yaml"] and m.zones == [] and m.persons == []
