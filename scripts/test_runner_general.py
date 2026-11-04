"""Static tests for the general Forgejo Actions runner (cluster/forgejo-runner-general).

    python -m pytest -q -p no:cacheprovider scripts/test_runner_general.py

Runs as the `realliance-net` pre-commit hook. Everything is rendered with `kubectl kustomize`,
the same build Argo runs. These tests never read `*.secret.*` files: the kustomization renders
the SealedSecret, and only the paths named here are opened.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
GENERAL = "cluster/forgejo-runner-general"
GALAXY = "cluster/forgejo-runner"


def render(path: str) -> list[dict]:
    out = subprocess.run(
        ["kubectl", "kustomize", str(REPO / path)], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, f"kubectl kustomize {path} failed:\n{out.stderr}"
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def one(docs: list[dict], kind: str) -> dict:
    found = [d for d in docs if d.get("kind") == kind]
    assert len(found) == 1, f"expected exactly one {kind}, got {len(found)}"
    return found[0]


@pytest.fixture(scope="module")
def general() -> list[dict]:
    return render(GENERAL)


@pytest.fixture(scope="module")
def galaxy() -> list[dict]:
    return render(GALAXY)


def pod(docs: list[dict]) -> dict:
    return one(docs, "Deployment")["spec"]["template"]["spec"]


def test_namespace_restricted(general):
    ns = one(general, "Namespace")
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"


def test_pod_hardened(general):
    dep = one(general, "Deployment")
    assert dep["spec"]["replicas"] == 1
    assert dep["spec"]["strategy"]["type"] == "Recreate"
    spec = pod(general)
    assert spec["automountServiceAccountToken"] is False
    assert spec["enableServiceLinks"] is False
    assert not spec.get("hostNetwork") and not spec.get("hostPID") and not spec.get("hostIPC")
    sc = spec["securityContext"]
    assert sc["runAsNonRoot"] is True and sc["runAsUser"] == 1000
    assert sc["seccompProfile"]["type"] == "RuntimeDefault"
    assert not any("hostPath" in v for v in spec["volumes"])
    containers = spec["containers"]
    assert [c["name"] for c in containers] == ["runner"]
    for c in containers:
        csc = c["securityContext"]
        assert csc["allowPrivilegeEscalation"] is False
        assert csc["readOnlyRootFilesystem"] is True
        assert csc["capabilities"]["drop"] == ["ALL"]
        assert not csc.get("privileged")
    res = containers[0]["resources"]
    assert res["requests"] == {"cpu": "100m", "memory": "256Mi"}
    assert res["limits"] == {"cpu": "1", "memory": "2Gi"}


def test_image_pinned(general):
    image = pod(general)["containers"][0]["image"]
    assert re.fullmatch(
        r"ghcr\.io/christopherjmiller/forgejo-runner-general:sha-[0-9a-f]{7}@sha256:[0-9a-f]{64}", image
    ), image


def test_start_wipes_job_state(general):
    cmd = " ".join(pod(general)["containers"][0]["command"])
    assert "rm -rf /work/home /work/tmp /work/act" in cmd
    assert "/work/cache" not in json.dumps(general)


def test_pvc(general):
    pvc = one(general, "PersistentVolumeClaim")
    assert pvc["spec"]["storageClassName"] == "rook-ceph-block-ssd"
    assert pvc["spec"]["resources"]["requests"]["storage"] == "5Gi"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]


def test_nothing_shared_with_forgejo_runner(general, galaxy):
    assert one(general, "Namespace")["metadata"]["name"] != one(galaxy, "Namespace")["metadata"]["name"]
    assert (
        one(general, "PersistentVolumeClaim")["metadata"]["name"]
        != one(galaxy, "PersistentVolumeClaim")["metadata"]["name"]
    )
    assert one(general, "SealedSecret")["metadata"]["name"] != one(galaxy, "SealedSecret")["metadata"]["name"]
    assert (
        one(general, "Deployment")["spec"]["template"]["metadata"]["labels"]
        != one(galaxy, "Deployment")["spec"]["template"]["metadata"]["labels"]
    )
    vols = {v["name"] for v in pod(general)["volumes"]}
    assert not vols & {"forgejo-runner-work", "forgejo-runner-config"}
    img_g = pod(general)["containers"][0]["image"].split(":")[0]
    img_x = pod(galaxy)["containers"][0]["image"].split(":")[0]
    assert img_g != img_x


def test_app_registered():
    app = yaml.safe_load((REPO / "cluster/applications/forgejo-runner-general.yaml").read_text())
    ref = yaml.safe_load((REPO / "cluster/applications/forgejo-runner.yaml").read_text())
    assert app["spec"]["source"]["path"] == GENERAL
    assert app["spec"]["destination"]["namespace"] == "forgejo-runner-general"
    assert app["spec"]["syncPolicy"] == ref["spec"]["syncPolicy"]
    sp = app["spec"]["syncPolicy"]
    assert sp["automated"] == {"prune": True, "selfHeal": True, "allowEmpty": True}
    assert "CreateNamespace=true" in sp["syncOptions"]
    kust = yaml.safe_load((REPO / "cluster/applications/kustomization.yaml").read_text())
    assert "forgejo-runner-general.yaml" in kust["resources"]


def test_renovate_never_automerges_runner():
    rules = json.loads((REPO / ".renovaterc.json").read_text())["packageRules"]
    hits = [
        r
        for r in rules
        if "ghcr.io/christopherjmiller/forgejo-runner-general" in r.get("matchPackageNames", [])
    ]
    assert len(hits) == 1 and hits[0]["automerge"] is False
