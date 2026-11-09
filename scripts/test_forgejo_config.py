"""Static tests for the forge's app.ini values (cluster/applications/forgejo-release.yaml).

    python -m pytest -q -p no:cacheprovider scripts/test_forgejo_config.py

Runs in the `forgejo-config` pre-commit hook.
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
RELEASE = REPO / "cluster/applications/forgejo-release.yaml"


@pytest.fixture(scope="module")
def config() -> dict:
    app = yaml.safe_load(RELEASE.read_text())
    return app["spec"]["source"]["helm"]["valuesObject"]["gitea"]["config"]


def test_token_permissions_default_explicit(config: dict) -> None:
    """The fork reads [actions] DEFAULT_TOKEN_PERMISSIONS; upstream ignores it. Never leave it implicit."""
    assert config["actions"]["DEFAULT_TOKEN_PERMISSIONS"] in ("permissive", "restricted")


@pytest.mark.xfail(strict=True, reason="Gate O: org keys ship in a later commit under Task 16")
def test_org_creation_open(config: dict) -> None:
    assert config["service"]["DEFAULT_ALLOW_CREATE_ORGANIZATION"] is True
    assert config["admin"]["DISABLE_REGULAR_ORG_CREATION"] is False
