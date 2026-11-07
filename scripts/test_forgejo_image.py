"""Static tests for the forge's image and its keep-warm DaemonSet (cluster/applications/forgejo-release.yaml).

    python -m pytest -q -p no:cacheprovider scripts/test_forgejo_image.py

Runs in the `forgejo-image` pre-commit hook. It renders the Application's chart with `helm template`,
so it needs `helm` and network: it pulls the chart version the Application names from its OCI
registry, once per run.

Forgejo runs with `strategy: Recreate`, and the fork image lives in the forge's own registry, so the
old pod is gone before the new one pulls. `forgejo-keepwarm` runs the same image on every node, one
Argo sync wave earlier: each node holds the image before Forgejo stops, and a binary that won't start
stops the sync first (docs/forgejo.md, "Fork image").
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
RELEASE = REPO / "cluster/applications/forgejo-release.yaml"
DOCS = REPO / "docs/forgejo.md"
KEEPWARM = "forgejo-keepwarm"
# The plan's pattern. Use it with fullmatch: `$` alone lets a trailing newline through, and re.ASCII
# keeps `\d` to 0-9.
FORK_PIN_RE = re.compile(r"^git\.realliance\.net/realliance/forgejo:v15\.0\.11-realliance\.\d+@sha256:[0-9a-f]{64}$", re.ASCII)
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# Well-formed pins that exist nowhere. SAMPLE_PIN renders the fullOverride path; PINNED stands in
# for the pin Task 5 commits, so every render test also runs against the file as it reads then.
SAMPLE_PIN = "git.realliance.net/realliance/forgejo:v15.0.11-realliance.1@sha256:" + "0" * 64
PINNED = "git.realliance.net/realliance/forgejo:v15.0.11-realliance.7@sha256:" + "a" * 64
WAVE = "argocd.argoproj.io/sync-wave"


@pytest.fixture(scope="module")
def app() -> dict:
    return yaml.safe_load(RELEASE.read_text())


@pytest.fixture(scope="module")
def repo_values(app: dict) -> dict:
    return app["spec"]["source"]["helm"]["valuesObject"]


@pytest.fixture(scope="module", params=["today", "pinned"])
def values(request: pytest.FixtureRequest, repo_values: dict) -> dict:
    """The values as committed, and as they read once a fork pin is in (simulated)."""
    if request.param == "today":
        return repo_values
    return {**repo_values, "image": {**repo_values["image"], "fullOverride": PINNED}}


@pytest.fixture(scope="module")
def chart(app: dict, tmp_path_factory: pytest.TempPathFactory) -> Path:
    src = app["spec"]["source"]
    out = tmp_path_factory.mktemp("chart")
    ref = f"oci://{src['repoURL']}/{src['chart']}"
    r = subprocess.run(
        ["helm", "pull", ref, "--version", src["targetRevision"], "-d", str(out)],
        capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, f"helm pull {ref} {src['targetRevision']} failed:\n{r.stderr}"
    (tgz,) = out.glob("*.tgz")
    return tgz


def render(app: dict, chart: Path, values: dict, tmp: Path) -> list[dict]:
    vals = tmp / "values.yaml"
    vals.write_text(yaml.safe_dump(values))
    release = app["spec"]["source"]["helm"]["releaseName"]
    ns = app["spec"]["destination"]["namespace"]
    r = subprocess.run(
        ["helm", "template", release, str(chart), "-n", ns, "-f", str(vals)],
        capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, f"helm template failed:\n{r.stderr}"
    return [d for d in yaml.safe_load_all(r.stdout) if d]


@pytest.fixture(scope="module")
def docs(app: dict, chart: Path, values: dict, tmp_path_factory: pytest.TempPathFactory) -> list[dict]:
    return render(app, chart, values, tmp_path_factory.mktemp("as-is"))


def without_override(values: dict) -> dict:
    """The fallback: the same values with the fullOverride line deleted (docs/forgejo.md, "Fork image")."""
    return {**values, "image": {k: v for k, v in values["image"].items() if k != "fullOverride"}}


@pytest.fixture(scope="module")
def docs_upstream(app: dict, chart: Path, values: dict, tmp_path_factory: pytest.TempPathFactory) -> list[dict]:
    return render(app, chart, without_override(values), tmp_path_factory.mktemp("upstream"))


@pytest.fixture(scope="module")
def docs_fork(app: dict, chart: Path, values: dict, tmp_path_factory: pytest.TempPathFactory) -> list[dict]:
    base = without_override(values)
    forked = {**base, "image": {**base["image"], "fullOverride": SAMPLE_PIN}}
    return render(app, chart, forked, tmp_path_factory.mktemp("fork"))


def one(docs: list[dict], kind: str, name: str) -> dict:
    found = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"want exactly one {kind}/{name}, got {len(found)}"
    return found[0]


def pod_images(obj: dict) -> list[str]:
    pod = obj["spec"]["template"]["spec"]
    return [c["image"] for c in pod.get("initContainers", []) + pod["containers"]]


def test_keepwarm_wave_before_forgejo(docs: list[dict]) -> None:
    ds = one(docs, "DaemonSet", KEEPWARM)
    assert ds["metadata"]["namespace"] == "forgejo"
    ann = ds["metadata"].get("annotations") or {}
    assert ann.get(WAVE) == "-1"
    # A sync hook is deleted and re-created around each sync; the image must stay in use between them.
    assert not [k for k in ann if k in ("argocd.argoproj.io/hook", "helm.sh/hook")], ann
    dep = one(docs, "Deployment", "forgejo")
    wave = (dep["metadata"].get("annotations") or {}).get(WAVE)
    assert wave is None or int(wave) >= 0, wave
    # Nothing else may join wave -1 or earlier: the wave is the keep-warm gate and nothing more.
    early = [
        f"{d['kind']}/{d['metadata']['name']}" for d in docs
        if int((d["metadata"].get("annotations") or {}).get(WAVE, "0")) < 0
    ]
    assert early == [f"DaemonSet/{KEEPWARM}"], early


@pytest.mark.parametrize("variant", ["as-is", "upstream", "fork"])
def test_keepwarm_same_image(
    variant: str, docs: list[dict], docs_upstream: list[dict], docs_fork: list[dict], values: dict
) -> None:
    rendered = {"as-is": docs, "upstream": docs_upstream, "fork": docs_fork}[variant]
    dep = one(rendered, "Deployment", "forgejo")
    ds = one(rendered, "DaemonSet", KEEPWARM)
    (main,) = [c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == "forgejo"]
    (warm,) = ds["spec"]["template"]["spec"]["containers"]
    assert warm["image"] == main["image"]
    assert set(pod_images(dep)) == {main["image"]}, "every Forgejo container runs the one image"
    assert warm.get("imagePullPolicy") == main.get("imagePullPolicy")
    # The chart sets the Deployment's from gitea.images.pullSecrets; the keep-warm pod has to pull alike.
    ds_pod, dep_pod = ds["spec"]["template"]["spec"], dep["spec"]["template"]["spec"]
    assert ds_pod.get("imagePullSecrets") == dep_pod.get("imagePullSecrets")
    if variant == "fork":
        assert main["image"] == SAMPLE_PIN
    elif variant == "upstream":
        assert main["image"].endswith("@" + values["image"]["digest"]), main["image"]
        assert main["image"].startswith("code.forgejo.org/forgejo/forgejo:"), main["image"]


def test_keepwarm_runs_version_first(docs: list[dict]) -> None:
    pod = one(docs, "DaemonSet", KEEPWARM)["spec"]["template"]["spec"]
    assert "initContainers" not in pod
    (warm,) = pod["containers"]
    assert warm["command"] == ["sh", "-c", "/app/gitea/gitea --version && exec sleep 2147483647"]
    assert "args" not in warm


def test_keepwarm_minimal(docs: list[dict]) -> None:
    ds = one(docs, "DaemonSet", KEEPWARM)
    pod = ds["spec"]["template"]["spec"]
    (warm,) = pod["containers"]
    assert warm["resources"] == {"requests": {"cpu": "1m", "memory": "8Mi"}, "limits": {"memory": "128Mi"}}
    psc, csc = pod.get("securityContext") or {}, warm.get("securityContext") or {}
    merged = {**psc, **csc}
    assert merged.get("runAsNonRoot") is True
    assert merged.get("runAsUser") == 1000
    assert csc.get("readOnlyRootFilesystem") is True
    assert csc.get("allowPrivilegeEscalation") is False
    assert csc.get("capabilities") == {"drop": ["ALL"]}
    assert pod.get("automountServiceAccountToken") is False
    # All three nodes are control planes; today none is tainted, and a future taint must not drop one.
    assert {"operator": "Exists"} in pod.get("tolerations", []), pod.get("tolerations")
    assert "nodeSelector" not in pod and "affinity" not in pod
    # Nothing to serve or store: no ports, no volumes, no host namespaces.
    assert "ports" not in warm and "volumeMounts" not in warm and "volumes" not in pod
    assert not any(pod.get(k) for k in ("hostNetwork", "hostPID", "hostIPC"))


def test_keepwarm_outside_forgejo_selectors(docs: list[dict]) -> None:
    """The keep-warm pods must never receive Forgejo's traffic, nor count as Forgejo's pods."""
    ds = one(docs, "DaemonSet", KEEPWARM)
    labels = ds["spec"]["template"]["metadata"]["labels"]
    assert ds["spec"]["selector"]["matchLabels"].items() <= labels.items()
    selectors = [d["spec"].get("selector") or {} for d in docs if d["kind"] == "Service"]
    selectors.append(one(docs, "Deployment", "forgejo")["spec"]["selector"]["matchLabels"])
    for sel in selectors:
        assert sel and not sel.items() <= labels.items(), f"{sel} selects the keep-warm pods"


@pytest.mark.parametrize(
    "pin, ok",
    [
        (SAMPLE_PIN, True),
        ("git.realliance.net/realliance/forgejo:v15.0.11-realliance.1", False),
        ("git.realliance.net/realliance/forgejo@sha256:" + "0" * 64, False),
        ("git.realliance.net/realliance/forgejo:sha-abcdef1@sha256:" + "0" * 64, False),
        ("ghcr.io/realliance/forgejo:v15.0.11-realliance.1@sha256:" + "0" * 64, False),
        (SAMPLE_PIN + "\n", False),
        (SAMPLE_PIN.replace("realliance.1@", "realliance.\u0661@"), False),
    ],
)
def test_fork_pin_pattern(pin: str, ok: bool) -> None:
    """The pin guard rejects a tag without a digest, a sha- tag, any other registry, a trailing newline
    and a non-ASCII digit."""
    assert bool(FORK_PIN_RE.fullmatch(pin)) is ok


def test_fork_pin(repo_values: dict) -> None:
    image = repo_values["image"]
    if image.get("fullOverride"):
        assert FORK_PIN_RE.fullmatch(image["fullOverride"]), image["fullOverride"]
    # The upstream line stays: the chart ignores it under fullOverride, and deleting fullOverride
    # alone falls back to it (docs/forgejo.md, "Fork image").
    assert DIGEST_RE.match(image.get("digest", "")), image.get("digest")
    assert image.get("rootless") is True
    assert "tag" not in image and "repository" not in image and "registry" not in image


ACCOUNT_URL = "https://sso.realliance.net/realms/realliance/account/"
USAGE_URL = "http://ci-dispatch.ci-dispatch.svc.cluster.local:8081"
USAGE_SECRET = "forgejo-ci-usage"
NAVBAR_STUB = "/data/gitea/templates/user/settings/navbar.tmpl"


def test_fork_config(docs: list[dict], repo_values: dict) -> None:
    """The fork's own app.ini sections (realliance/forgejo#16): the account link and the usage API's
    URL inline; the usage TOKEN only from a SealedSecret, never in this public file; the ci-dispatch
    Service really serves the usage port at that URL; and no user settings menu override, since the
    fork's patch carries the link and an override would hide the Usage tab."""
    gitea = repo_values["gitea"]
    assert gitea["config"]["realliance"] == {"ACCOUNT_URL": ACCOUNT_URL}
    assert gitea["config"]["realliance.ci_usage"] == {"URL": USAGE_URL}, "TOKEN never inline"
    assert {"secret": {"secretName": USAGE_SECRET}} in gitea["additionalConfigSources"]

    sealed = yaml.safe_load((REPO / "cluster/forgejo/ci-usage.yaml").read_text())
    assert sealed["kind"] == "SealedSecret" and sealed["metadata"] == {"name": USAGE_SECRET, "namespace": "forgejo"}
    assert set(sealed["spec"]["encryptedData"]) == {"realliance.ci_usage"}, "one key: the section name"
    kust = yaml.safe_load((REPO / "cluster/forgejo/kustomization.yaml").read_text())
    assert "ci-usage.yaml" in kust["resources"]

    svc = yaml.safe_load((REPO / "cluster/ci-dispatch/dispatcher/service.yaml").read_text())
    host = f"{svc['metadata']['name']}.ci-dispatch.svc.cluster.local"
    usage_port = {p["name"]: p["port"] for p in svc["spec"]["ports"]}["usage"]
    assert USAGE_URL == f"http://{host}:{usage_port}"

    (gen,) = [g for g in kust["configMapGenerator"] if g["name"] == "forgejo-custom-templates"]
    assert not [f for f in gen["files"] if "navbar" in f], gen["files"]
    assert not (REPO / "cluster/forgejo/custom/templates/user").exists()
    dep = one(docs, "Deployment", "forgejo")
    (main,) = [c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == "forgejo"]
    assert not [m for m in main["volumeMounts"] if m["mountPath"].startswith("/data/gitea/templates/user/")]


def run_pre(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-euo", "pipefail", "-c", script], capture_output=True, text=True, check=False)


def test_navbar_stub_cleanup(docs: list[dict], repo_values: dict, tmp_path: Path) -> None:
    """The old override's subPath mount left an empty navbar.tmpl on /data, which Forgejo would load as
    an empty menu. initPreScript removes it when it is empty, keeps any real file, and never fails the
    init container (it runs under the chart's `set -euo pipefail`)."""
    init = one(docs, "Secret", "forgejo-init")
    script = (init.get("stringData") or {}).get("init_directory_structure.sh", "")
    assert NAVBAR_STUB in script, "initPreScript reaches the init-directories script"
    pre = repo_values["initPreScript"].replace("/data/", f"{tmp_path}/")
    stub = Path(str(tmp_path) + NAVBAR_STUB.removeprefix("/data"))
    stub.parent.mkdir(parents=True)
    for content, kept in (("", False), ("{{/* a real override */}}\n", True)):
        stub.write_text(content)
        r = run_pre(pre)
        assert r.returncode == 0, r.stderr
        assert stub.exists() is kept, (content, r.stdout)
    stub.unlink()
    r = run_pre(pre)
    assert r.returncode == 0 and r.stdout == "", "a missing stub is the steady state: silent, no error"
    stub.write_text("")
    stub.parent.chmod(0o555)
    try:
        r = run_pre(pre)
        assert r.returncode == 0, "a failed rm must not stop the forge from starting"
    finally:
        stub.parent.chmod(0o755)


def code_blocks(path: Path) -> list[str]:
    return re.findall(r"^```[a-z]*\n(.*?)^```", path.read_text(), re.S | re.M)


def test_runbook_selective_sync(docs: list[dict]) -> None:
    """docs/forgejo.md, "When the sync waits at wave -1". The one selective sync names the Deployment and
    every Secret the chart renders: the pod's checksum/config comes from those Secrets, so a
    Deployment-only sync would start Forgejo on the old config. terminate-op only asks for the stop, so
    an `app wait --operation` sits between it and anything after it."""
    blocks = code_blocks(DOCS)
    syncs = [b for b in blocks if "app sync forgejo-release" in b]
    assert len(syncs) == 1, f"want one selective sync block, got {len(syncs)}"
    lines = [ln for ln in syncs[0].splitlines() if "argocd" in ln]
    needles = ("app terminate-op forgejo-release", "app wait forgejo-release --operation", "app sync forgejo-release")
    order = [next((i for i, ln in enumerate(lines) if n in ln), -1) for n in needles]
    assert -1 not in order and order == sorted(order), f"want {needles} in order, got {lines}"
    sync = lines[order[2]]
    want = {f":Secret:{d['metadata']['name']}" for d in docs if d["kind"] == "Secret"} | {"apps:Deployment:forgejo"}
    assert set(re.findall(r"--resource (\S+)", sync)) == want, sync
    for b in blocks:
        bl = b.splitlines()
        for i, ln in enumerate(bl):
            if "app terminate-op forgejo-release" in ln:
                assert any("app wait forgejo-release --operation" in later for later in bl[i + 1:]), b
