import json

import pytest

from hactl import shot
from hactl.errors import HactlError


def test_slug_and_filename():
    assert shot.slug("/home-ops/0") == "home-ops-0"
    assert shot.ShotSpec("/home-ops/1", "phone", "dark").filename == "home-ops-1_phone_dark.png"


def test_parse_viewports():
    assert shot.parse_viewports("phone,desktop") == ["phone", "desktop"]
    with pytest.raises(HactlError, match="tablet"):
        shot.parse_viewports("phone,tablet")


def test_plan_shots_groups_by_viewport_then_path():
    specs = shot.plan_shots(["/a", "/b"], ["phone", "desktop"], ["dark"])
    assert [(s.viewport, s.path) for s in specs] == [("phone", "/a"), ("phone", "/b"), ("desktop", "/a"), ("desktop", "/b")]


def test_phone_viewport_is_pixel_sized():
    assert shot.VIEWPORTS["phone"]["width"] == 412
    assert shot.VIEWPORTS["desktop"]["width"] == 1440


def test_auth_script_sets_tokens_safely():
    js = shot.auth_script("https://ha.example", 'tok"en')
    assert "localStorage.setItem('hassTokens'" in js
    payload = json.loads(json.loads(js.split("localStorage.setItem('hassTokens', ")[1].split(");")[0]))
    assert payload["access_token"] == 'tok"en'
    assert payload["hassUrl"] == "https://ha.example"


def test_default_out_dir_uses_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert str(shot.default_out_dir()).startswith(str(tmp_path / "hactl" / "shots"))


def test_report_flags_error_cards_and_theme():
    ok = {"file": "a.png", "cards": 5, "seconds": 2.0, "error_cards": [], "unavailable": [], "theme_ok": True, "console": []}
    bad = dict(ok, file="b.png", error_cards=["Custom element doesn't exist: x"], theme_ok=False)
    lines, is_bad = shot.report([ok, bad])
    assert not shot.report([ok])[1]
    assert is_bad
    assert any("error card" in line for line in lines)


def test_console_summary_drops_benign_and_opaque_and_counts_repeats():
    raw = [
        'console: unhandled rejection: {"code":"not_found","message":"Subscription not found."}',
        "pageerror: Object",
        'console: unhandled rejection: {"code":"home_assistant_error","message":"boom"}',
        'console: unhandled rejection: {"code":"home_assistant_error","message":"boom"}',
        "pageerror: Cannot read properties of undefined (reading 'state')",
    ]
    assert shot.summarize_console(raw) == [
        'console: unhandled rejection: {"code":"home_assistant_error","message":"boom"} (x2)',
        "pageerror: Cannot read properties of undefined (reading 'state')",
    ]


def test_rejection_listener_reports_the_reason():
    assert "unhandledrejection" in shot.REJECTION_JS
    assert "unhandled rejection: " in shot.REJECTION_JS


def test_response_problem():
    assert shot.response_problem(200) is None
    assert "502" in shot.response_problem(502)
    assert "no response" in shot.response_problem(None)


def test_unreachable_ha_is_a_clean_error(tmp_path, monkeypatch):
    pytest.importorskip("playwright")
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)  # the host's breaks the nix browser (see bin/hactl)

    class Unreachable:
        url = "http://127.0.0.1:9"

        def bearer(self):
            return "x.y.z"

    with pytest.raises(HactlError, match="HA"):
        shot.take(Unreachable(), shot.plan_shots(["/home-ops/0"], ["phone"], ["dark"]), tmp_path)
