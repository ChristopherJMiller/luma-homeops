import urllib.error

import ha_reload


class FakeHA:
    def __init__(self, revisions, components=("homeassistant", "automation", "template")):
        self.revisions = list(revisions)
        self.components = list(components)
        self.calls = []

    def __call__(self, method, path, body=None, timeout=60):
        self.calls.append((method, path))
        if path == "/api/services/homeassistant/reload_all":
            return []
        if path == "/api/states/sensor.ha_config_revision":
            r = self.revisions.pop(0) if len(self.revisions) > 1 else self.revisions[0]
            if isinstance(r, Exception):
                raise r
            return {"state": r}
        if path == "/api/config":
            return {"components": self.components}
        raise AssertionError(path)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_expected_revision(tmp_path):
    (tmp_path / "config_revision.yaml").write_text('template:\n  - sensor:\n      - name: X\n        state: "0123456789ab"\n')
    assert ha_reload.expected_revision(tmp_path) == "0123456789ab"


def test_declared_domains_reads_top_level_keys_only(tmp_path):
    (tmp_path / "a.yaml").write_text("# comment: no\nautomation:\n  - id: a\n    nested: no\ntemplate: []\n")
    (tmp_path / "b.yaml").write_text("prometheus:\n  namespace: homeassistant\n")
    assert ha_reload.declared_domains(tmp_path) == {"automation", "template", "prometheus"}


def test_reload_until_match():
    ha, c = FakeHA(["old", "old", "abc"]), Clock()
    assert ha_reload.reload_until_revision(ha, "abc", timeout=360, interval=20, clock=c, sleep=c.sleep)
    assert c.t == 40
    assert ha.calls.count(("POST", "/api/services/homeassistant/reload_all")) == 3


def test_reload_survives_ha_restarting():
    ha, c = FakeHA([urllib.error.URLError("connection refused"), "abc"]), Clock()
    assert ha_reload.reload_until_revision(ha, "abc", clock=c, sleep=c.sleep)


def test_reload_times_out():
    ha, c = FakeHA(["old"]), Clock()
    assert not ha_reload.reload_until_revision(ha, "abc", timeout=60, interval=20, clock=c, sleep=c.sleep)


def test_missing_integrations_ignores_platform_entries():
    ha = FakeHA(["x"], components=["automation", "template", "template.sensor"])
    assert ha_reload.missing_integrations(ha, {"automation", "template", "prometheus"}) == {"prometheus"}


class FakeConfigs:
    """/api/config answers in sequence (the last one repeats)."""

    def __init__(self, configs):
        self.configs = list(configs)

    def __call__(self, method, path, body=None, timeout=60):
        assert path == "/api/config"
        c = self.configs.pop(0) if len(self.configs) > 1 else self.configs[0]
        if isinstance(c, Exception):
            raise c
        return c


def test_missing_is_judged_only_once_running():
    # During STARTING the component list is incomplete; judging then would restart HA for nothing.
    ha, c = FakeConfigs([{"state": "STARTING", "components": ["automation"]},
                         {"state": "RUNNING", "components": ["automation", "prometheus"]}]), Clock()
    assert ha_reload.missing_when_running(ha, {"automation", "prometheus"}, clock=c, sleep=c.sleep) == set()


def test_missing_when_running_reports_real_gaps():
    ha, c = FakeConfigs([{"state": "RUNNING", "components": ["automation"]}]), Clock()
    assert ha_reload.missing_when_running(ha, {"automation", "prometheus"}, clock=c, sleep=c.sleep) == {"prometheus"}


def test_never_running_is_none():
    ha, c = FakeConfigs([{"state": "STARTING", "components": []}]), Clock()
    assert ha_reload.missing_when_running(ha, {"automation"}, timeout=60, interval=15, clock=c, sleep=c.sleep) is None


def test_poll_survives_a_cut_off_response():
    import http.client
    ha, c = FakeConfigs([http.client.IncompleteRead(b"{"), {"state": "RUNNING", "components": ["automation"]}]), Clock()
    assert ha_reload.missing_when_running(ha, {"automation"}, clock=c, sleep=c.sleep) == set()
