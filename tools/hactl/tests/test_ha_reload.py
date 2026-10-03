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
