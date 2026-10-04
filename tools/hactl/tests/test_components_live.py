"""Every pinned component URL resolves: the chart's installer fails the pod on a 404 (HA stays down)."""
import os
import urllib.request

import pytest
import yaml

from hactl import lint, paths

pytestmark = pytest.mark.skipif(os.environ.get("HACTL_LIVE") != "1", reason="network: set HACTL_LIVE=1")


def _cards():
    values = yaml.safe_load(paths.RELEASE_FILE.read_text())["spec"]["source"]["helm"]["valuesObject"]
    return values["components"]["cards"]


def _status(url):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "hactl-test"})
    with urllib.request.urlopen(req, timeout=30) as r:  # follows the release-asset redirect
        return r.status


@pytest.mark.parametrize("card", _cards() if os.environ.get("HACTL_LIVE") == "1" else [], ids=lambda c: c["name"])
def test_card_urls_resolve(card):
    assert _status(card["url"].replace("{version}", str(card["version"]))) == 200


@pytest.mark.parametrize("name,repo,version", lint.custom_components() if os.environ.get("HACTL_LIVE") == "1" else [])
def test_integration_tarballs_resolve(name, repo, version):
    assert _status(f"https://github.com/{repo}/archive/refs/tags/{version}.tar.gz") == 200
