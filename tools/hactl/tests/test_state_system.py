"""system.yaml `http:` — HA 2026.9 moved the http config from YAML into storage (Settings -> System -> Network)."""
import re

import pytest
from state_fixtures import FakeHA, snapshot, write_manifests

from hactl.errors import HactlError
from hactl.state import diff, live, model

LIVE = {"trusted_proxies": ["10.0.0.0/8"], "use_x_forwarded_for": True, "server_port": 8123, "ip_ban_enabled": True}


def test_http_is_loaded_from_system_yaml(tmp_path):
    m = model.load(write_manifests(tmp_path, {"system.yaml": (
        "http:\n  use_x_forwarded_for: true\n  trusted_proxies: [10.0.0.0/8]\n")}))
    assert m.http == {"use_x_forwarded_for": True, "trusted_proxies": ["10.0.0.0/8"]}


def test_http_undeclared_is_none(tmp_path):
    assert model.load(write_manifests(tmp_path, {})).http is None


@pytest.mark.parametrize("text,problem", [
    ("http: [10.0.0.0/8]\n", "http must be a mapping"),
    ("http:\n  trusted_proxy: [10.0.0.0/8]\n", "unknown key(s) trusted_proxy"),
    ("network: {}\n", "unknown section 'network'"),
])
def test_http_problems(tmp_path, text, problem):
    with pytest.raises(HactlError, match=re.escape(problem)):
        model.load(write_manifests(tmp_path, {"system.yaml": text}))


def test_http_in_sync_ignores_undeclared_keys():
    assert diff.diff_http({"use_x_forwarded_for": True, "trusted_proxies": ["10.0.0.0/8"]}, LIVE) == []
    assert diff.diff_http(None, LIVE) == []  # undeclared: unmanaged


def test_http_drift_is_a_manual_change_pointing_at_the_ui():
    [c] = diff.diff_http({"use_x_forwarded_for": True, "trusted_proxies": ["10.0.0.0/8", "192.168.0.0/24"]}, LIVE)
    assert c.action == "manual" and c.kind == "http"
    assert "trusted_proxies" in c.detail and "Settings -> System -> Network" in c.detail


def test_http_unreadable_is_a_manual_change():
    [c] = diff.diff_http({"use_x_forwarded_for": True}, {})
    assert c.action == "manual" and "could not read" in c.detail


def test_fetch_reads_the_stable_http_config():
    snap = snapshot()
    snap.http = dict(LIVE)
    assert live.fetch(FakeHA(snap)).http == LIVE


def test_fetch_tolerates_ha_without_the_http_config_api():
    class OldHA(FakeHA):
        def _one(self, c):
            if c["type"] == "http/config":
                raise HactlError("unknown command")
            return super()._one(c)
    assert live.fetch(OldHA(snapshot())).http == {}
