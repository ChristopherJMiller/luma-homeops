"""Every pinned component URL resolves: the chart's installer fails the pod on a 404 (HA stays down)."""
import os
import urllib.request

import pytest
import yaml

from hactl import paths

pytestmark = pytest.mark.skipif(os.environ.get("HACTL_LIVE") != "1", reason="network: set HACTL_LIVE=1")


def _components(kind):
    values = yaml.safe_load(paths.RELEASE_FILE.read_text())["spec"]["source"]["helm"]["valuesObject"]
    return values["components"][kind]


def _status(url):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "hactl-test"})
    with urllib.request.urlopen(req, timeout=30) as r:  # follows the release-asset redirect
        return r.status


@pytest.mark.parametrize("card", _components("cards") if os.environ.get("HACTL_LIVE") == "1" else [], ids=lambda c: c["name"])
def test_card_urls_resolve(card):
    assert _status(card["url"].replace("{version}", str(card["version"]))) == 200


@pytest.mark.parametrize("integration", _components("integrations") if os.environ.get("HACTL_LIVE") == "1" else [],
                         ids=lambda i: i["name"])
def test_integration_sources_resolve(integration):
    """The release zip when one is declared (what the chart installs), else the tag's source tarball."""
    version = str(integration["version"])
    url = integration.get("url", f"https://github.com/{integration['repo']}/archive/refs/tags/{{version}}.tar.gz")
    assert _status(url.replace("{version}", version)) == 200
