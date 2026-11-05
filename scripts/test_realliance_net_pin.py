"""Static tests for the Realliance website pin (cluster/realliance.net).

    python -m pytest -q -p no:cacheprovider scripts/test_realliance_net_pin.py

Runs in the `realliance-net` pre-commit hook. The image is built by Forgejo Actions in
realliance/realliance.net and re-pinned by hand; nothing else in the Deployment may drift.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SITE = "cluster/realliance.net"
IMAGE_NAME = "git.realliance.net/realliance/realliance.net"
IMAGE_RE = re.compile(r"^git\.realliance\.net/realliance/realliance\.net:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$")


@pytest.fixture(scope="module")
def docs() -> list[dict]:
    out = subprocess.run(
        ["kubectl", "kustomize", str(REPO / SITE)], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, f"kubectl kustomize {SITE} failed:\n{out.stderr}"
    return [d for d in yaml.safe_load_all(out.stdout) if d]


@pytest.fixture(scope="module")
def container(docs: list[dict]) -> dict:
    dep = [d for d in docs if d["kind"] == "Deployment"]
    assert len(dep) == 1
    cs = dep[0]["spec"]["template"]["spec"]["containers"]
    assert [c["name"] for c in cs] == ["home"]
    return cs[0]


def test_site_image_pinned_to_forge(container: dict) -> None:
    assert IMAGE_RE.match(container["image"]), container["image"]


def test_rest_of_the_deployment_unchanged(docs: list[dict], container: dict) -> None:
    dep = next(d for d in docs if d["kind"] == "Deployment")
    spec = dep["spec"]
    pod = spec["template"]["spec"]
    assert spec["replicas"] == 2
    assert spec["strategy"] == {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1}}
    assert [p["containerPort"] for p in container["ports"]] == [8080]
    assert container["resources"] == {
        "requests": {"memory": "5Mi", "cpu": "5m"},
        "limits": {"memory": "5Mi", "cpu": "5m"},
    }
    assert pod["securityContext"] == {"runAsUser": 101, "runAsGroup": 101}
    assert container["securityContext"] == {
        "readOnlyRootFilesystem": True,
        "runAsUser": 101,
        "runAsGroup": 101,
    }
    assert pod["volumes"] == [{"name": "tmp", "emptyDir": {"medium": "Memory"}}]
    assert container["volumeMounts"] == [{"mountPath": "/tmp", "name": "tmp"}]
    assert container["livenessProbe"]["httpGet"] == {"path": "/health", "port": 8080}
    assert container["lifecycle"]["preStop"]["exec"]["command"] == ["/usr/sbin/nginx", "-s", "quit"]

    svc = next(d for d in docs if d["kind"] == "Service")
    assert svc["spec"]["selector"] == {"app": "home"}
    assert svc["spec"]["ports"][0]["port"] == 80 and svc["spec"]["ports"][0]["targetPort"] == 8080

    ings = [d for d in docs if d["kind"] == "Ingress"]
    hosts = sorted(r["host"] for i in ings for r in i["spec"]["rules"])
    assert hosts == ["realliance.net", "www.realliance.net"]
    for i in ings:
        ann = i["metadata"]["annotations"]
        assert ann["traefik.ingress.kubernetes.io/router.middlewares"] == "realliance-net-set-csp@kubernetescrd"


def test_renovate_automerges_site_releases_only() -> None:
    """Chris chose automerge of new X.Y.Z releases (2026-11-05); sha-* and other tags never match."""
    rules = json.loads((REPO / ".renovaterc.json").read_text())["packageRules"]
    mine = [
        r for r in rules
        if r.get("matchManagers") == ["kubernetes"] and r.get("matchPackageNames") == [IMAGE_NAME]
    ]
    assert len(mine) == 1, "need exactly one rule for the site image"
    assert mine[0].get("automerge") is True
    assert mine[0].get("allowedVersions") == "/^[0-9]+\\.[0-9]+\\.[0-9]+$/"
