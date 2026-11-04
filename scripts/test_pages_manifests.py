"""Static tests for the `pages` Argo app: git-pages and pages-relay (Realliance Pages).

    python -m pytest -q -p no:cacheprovider scripts/test_pages_manifests.py

Runs as the `pages-manifests` pre-commit hook. Everything is rendered with
`kustomize build`, the same build Argo runs, and checked against the values the
design fixes: docs/superpowers/specs/2026-11-03-realliance-pages-design.md, as
planned in docs/superpowers/plans/2026-11-03-realliance-pages.md (Global
Constraints; Decisions D3, D7, D8, D12; Task 5).

These tests never read `*.secret.*` files: they open only the paths named here,
and the copies the rollback tests make leave `*.secret.*` behind.

Rolling back the first pages push
---------------------------------
Global Constraints: roll back by rendering nothing, never by deleting the
Application. The rollback is one commit that edits cluster/pages/kustomization.yaml
so that, below its header comment, it reads exactly:

    # pages: rolled back (Global Constraints)
    apiVersion: kustomize.config.k8s.io/v1beta1
    kind: Kustomization
    resources: []

That is: add the marker line above `apiVersion:`, replace the `resources:` list
with `[]`, and delete the `namespace:` and `configMapGenerator:` keys. Leave the
other files and cluster/applications/pages.yaml as they are. Argo then prunes
every object, the Namespace included (`allowEmpty: true`).

In that state these tests assert that the build renders nothing and that the
Application is unchanged, and skip the deployed-state checks. `resources: []`
WITHOUT the marker fails: an accidental empty render must never pass. To
redeploy, `git revert` the rollback commit.
"""

import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

PAGES_DIR = "cluster/pages"
PAGES_APP = "cluster/applications/pages.yaml"
CONFIG_TOML = REPO / PAGES_DIR / "config.toml"
RELAY_SOURCE = REPO / "images/pages-relay/relay.go"
NAMESPACE = "pages"

ROLLBACK_MARKER = "# pages: rolled back (Global Constraints)"
ROLLED_BACK_BODY = f"""\
{ROLLBACK_MARKER}
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources: []
"""

# git-pages 0.10.1 (security release; Chris waived the pin-age rule, D4). Its
# OCI index digest, resolved read-only on 2026-11-03.
GIT_PAGES_IMAGE = "codeberg.org/git-pages/git-pages:0.10.1@sha256:882733e3fc4b5cde8cb3f83b786f8b85854c9ad951662b64fca23cbd34f79dc9"
RELAY_IMAGE_RE = r"^ghcr\.io/christopherjmiller/pages-relay:sha-[0-9a-f]{7}@sha256:[0-9a-f]{64}$"

# The deployed kustomization's shape. test_pages_kustomization keeps it equal to the
# real file; the rollback tests use it while the real file is rolled back.
DEPLOYED_KUSTOMIZATION = """\
---
# pages (deployed shape; the real file carries the full header)
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: pages
resources:
  - namespace.yaml
  - git-pages-s3.yaml
  - pages-relay-webhook.yaml
  - git-pages.yaml
  - pages-relay.yaml
  - monitoring.yaml
configMapGenerator:
  - name: git-pages-config
    files:
      - config.toml
"""
# Internal only until Task 9 adds the IngressRoutes and Middlewares; no
# NetworkPolicy until the netpol plan adopts the namespace (its Task 13).
PAGES_KINDS = [
    "ConfigMap",
    "Deployment",
    "Deployment",
    "Namespace",
    "PrometheusRule",
    "SealedSecret",
    "SealedSecret",
    "Service",
    "Service",
    "ServiceMonitor",
    "ServiceMonitor",
]

SEALED_KEYS = {
    "git-pages-s3": {"PAGES_STORAGE_S3_ACCESS_KEY_ID", "PAGES_STORAGE_S3_SECRET_ACCESS_KEY"},
    "pages-relay-webhook": {"webhook-secret"},
}

# D7: the only headers a site's `_headers` may set. Never Set-Cookie, HSTS,
# nosniff or Referrer-Policy (Traefik owns those from Task 9).
ALLOWED_HEADERS = {
    "Content-Security-Policy",
    "Cross-Origin-Embedder-Policy",
    "Cross-Origin-Opener-Policy",
    "Cross-Origin-Resource-Policy",
    "Access-Control-Allow-Origin",
    "Permissions-Policy",
    "X-Frame-Options",
    "X-Robots-Tag",
}

# The whole git-pages config (D3, D7): anything added or dropped fails here.
EXPECTED_CONFIG = {
    "log-format": "json",
    "features": [],
    "server": {"pages": "tcp/:3000", "proxy": "-", "metrics": "tcp/:3002"},
    "wildcard": [
        {
            "domain": "realliance.page",
            "clone-url": "https://git.realliance.net/<user>/<project>.git",
            "index-repo": "pages",
            "index-repo-branch": "pages",
        }
    ],
    "storage": {
        "type": "s3",
        "s3": {
            "endpoint": "s3.us-east-005.backblazeb2.com",
            "region": "us-east-005",
            "bucket": "realliance-pages",
            "blob-cache": {"max-size": "64MB"},
        },
    },
    "limits": {
        "max-site-size": "128MB",
        "update-timeout": "5m",
        "concurrent-uploads": 16,
        "allowed-repository-url-prefixes": ["https://git.realliance.net/"],
        "allowed-custom-headers": sorted(ALLOWED_HEADERS),
        "allow-basic-auth": False,
    },
}

GIT_PAGES_COMMAND = ["/bin/git-pages", "-config", "/etc/git-pages/config.toml"]

RELAY_ENV = {
    "PAGES_RELAY_UPSTREAM": "http://git-pages.pages.svc.cluster.local:3000",
    "PAGES_RELAY_DOMAIN": "realliance.page",
    "PAGES_RELAY_FORGE": "https://git.realliance.net/",
    "PAGES_RELAY_SECRET_FILE": "/etc/pages-relay/webhook-secret",
    # Under the 128Mi limit, so the GC works hard before the kernel OOM-kills
    # (two 16 MiB bodies in flight peak near 33 MiB; Task 4 fix round 2).
    "GOMEMLIMIT": "100MiB",
}

# Initial sizing (plan Task 5 Step 3; relay values per the Task 4 fix-round-2
# measurement). Task 10 Step 6 moves them from measurements.
RESOURCES = {
    "git-pages": {"requests": {"cpu": "10m", "memory": "64Mi"}, "limits": {"memory": "1Gi"}},
    "pages-relay": {"requests": {"cpu": "5m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}},
}

SERVICES = {
    "git-pages": {"pages": 3000, "metrics": 3002},
    "pages-relay": {"http": 8080},
}

PAGES_DOWN_EXPR = (
    'up{namespace="pages"} == 0'
    ' or absent(up{namespace="pages",job="git-pages"})'
    ' or absent(up{namespace="pages",job="pages-relay"})'
)
UPDATE_FAILING_EXPR = (
    "(sum by (cause) (increase(git_pages_site_update_error[30m])) > 0)"
    " or (sum by (cause) (git_pages_site_update_error unless git_pages_site_update_error offset 30m) > 0)"
)
ALERT_EXPRS = {
    "PagesDown": PAGES_DOWN_EXPR,
    "PagesSiteUpdateFailing": UPDATE_FAILING_EXPR,
    "PagesRelayUpstreamError": 'increase(pages_relay_events_total{outcome="upstream_error"}[30m]) > 0',
    "PagesBadSignatures": 'increase(pages_relay_events_total{outcome="bad_signature"}[30m]) > 0',
    "PagesRelayRejecting": 'sum by (outcome) (increase(pages_relay_events_total{outcome=~"too_large|busy|bad_request"}[30m])) > 0',
    "PagesTakedownRequested": 'increase(pages_relay_events_total{outcome="unpublish_requested"}[6h]) > 0',
}
# Outcomes that must raise an alert, each in exactly one alert (Task 4 rulings):
# a rejected delivery is never retried by Forgejo 15, so it must be seen.
ALERTED_OUTCOMES = {"upstream_error", "bad_signature", "too_large", "busy", "bad_request", "unpublish_requested"}
KNOWN_METRICS = {"up", "git_pages_site_update_error", "pages_relay_events_total"}
PROMQL_WORDS = {"increase", "sum", "by", "absent", "or", "unless", "offset", "and"}

RENOVATE_IMAGES = ["codeberg.org/git-pages/git-pages", "ghcr.io/christopherjmiller/pages-relay"]

HOOK_FILES_MUST_MATCH = [
    "cluster/pages/kustomization.yaml",
    "cluster/pages/config.toml",
    "cluster/applications/pages.yaml",
    "cluster/applications/kustomization.yaml",
    "cluster/traefik/wildcard-cert.yaml",
    "cluster/dns/cloudflareddns-config.yaml",
    "cloudflare/dns/records.tf",
    "scripts/test_pages_manifests.py",
    "images/pages-relay/relay.go",
    ".renovaterc.json",
    ".pre-commit-config.yaml",
]


def render(path) -> list[dict]:
    """`kustomize build <path>` (relative to the repo root, or absolute), parsed into documents."""
    out = subprocess.run(
        ["kustomize", "build", str(REPO / path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, f"kustomize build {path} failed:\n{out.stderr}"
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def of_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def named(docs: list[dict], kind: str, name: str) -> dict:
    found = [d for d in of_kind(docs, kind) if d["metadata"]["name"] == name]
    assert len(found) == 1, f"expected exactly one {kind}/{name}, got {len(found)}"
    return found[0]


def pod_spec(docs: list[dict], name: str) -> dict:
    return named(docs, "Deployment", name)["spec"]["template"]["spec"]


def container(docs: list[dict], name: str) -> dict:
    (c,) = pod_spec(docs, name)["containers"]
    return c


def env_of(c: dict) -> dict:
    env = c.get("env", [])
    assert all(set(e) == {"name", "value"} for e in env), "env must be literal values (secrets come via envFrom/volumes)"
    return {e["name"]: e["value"] for e in env}


def has_marker(text: str) -> bool:
    return any(line.strip() == ROLLBACK_MARKER for line in text.splitlines())


def pages_state(app_dir=PAGES_DIR) -> str:
    """'deployed' or 'rolled-back' (the marked rollback in the module docstring).

    Fails on anything else: an empty render without the marker, the marker on a
    kustomization that still renders, or a rollback that kept a generator or namespace.
    """
    text = (REPO / app_dir / "kustomization.yaml").read_text()
    k = yaml.safe_load(text) or {}
    if has_marker(text):
        assert k.get("resources") == [], f"{ROLLBACK_MARKER!r} is present, so resources must be []"
        extra = sorted(set(k) - {"apiVersion", "kind", "resources"})
        assert not extra, f"a rolled-back kustomization keeps only resources: []; remove {extra}"
        assert render(app_dir) == [], "rolled back, yet the build still renders objects"
        return "rolled-back"
    assert k.get("resources"), (
        f"{app_dir}/kustomization.yaml renders nothing but has no rollback marker. If this is "
        f"the pre-approved rollback, add the line {ROLLBACK_MARKER!r} (see scripts/test_pages_manifests.py)."
    )
    return "deployed"


def require_deployed():
    if pages_state() == "rolled-back":
        pytest.skip(f"rolled back: {PAGES_DIR} renders nothing ({ROLLBACK_MARKER})")


def rolled_back_text(text: str) -> str:
    """The documented rollback edit, applied to a deployed kustomization.yaml: keep the
    header comment, replace everything from `apiVersion:` on with ROLLED_BACK_BODY."""
    head, sep, _ = text.partition("\napiVersion:")
    assert sep, "kustomization.yaml no longer has the expected shape"
    return head + "\n" + ROLLED_BACK_BODY


def deployed_kustomization() -> str:
    """The real kustomization.yaml while deployed (so the documented edit is proven on
    the real file); its deployed shape while the real file is rolled back."""
    text = (REPO / PAGES_DIR / "kustomization.yaml").read_text()
    return DEPLOYED_KUSTOMIZATION if has_marker(text) else text


def pages_copy(tmp_path: Path, kustomization: str) -> Path:
    """A copy of cluster/pages WITHOUT any *.secret.* file, with a replacement kustomization."""
    d = tmp_path / "pages"
    shutil.copytree(REPO / PAGES_DIR, d, ignore=shutil.ignore_patterns("*.secret.*"))
    (d / "kustomization.yaml").write_text(kustomization)
    return d


def relay_outcomes() -> set[str]:
    """Every Outcome the relay can count (images/pages-relay/relay.go)."""
    found = set(re.findall(r'^\s*[A-Z]\w*\s+Outcome\s*=\s*"([a-z_]+)"', RELAY_SOURCE.read_text(), re.M))
    assert found, "no Outcome constants found in images/pages-relay/relay.go"
    return found


def outcomes_matched(expr: str) -> set[str]:
    """The relay outcomes a pages_relay_events_total selector in `expr` matches."""
    matched = set()
    for op, value in re.findall(r'pages_relay_events_total\{outcome(=~|=)"([^"]*)"\}', expr):
        matched |= set(value.split("|")) if op == "=~" else {value}
    return matched


def metrics_used(expr: str) -> set[str]:
    """Metric names in a PromQL expr: every bare identifier once label matchers,
    ranges and `by (...)` clauses are gone, minus the operators and functions."""
    bare = re.sub(r"\{[^}]*\}|\[[^\]]*\]|\bby \([^)]*\)", " ", expr)
    return set(re.findall(r"\b[a-zA-Z_]\w*\b", bare)) - PROMQL_WORDS


def alert_rules(docs: list[dict]) -> dict[str, dict]:
    pr = named(docs, "PrometheusRule", "pages-alerts")
    rules = [r for g in pr["spec"]["groups"] for r in g["rules"]]
    assert all("alert" in r for r in rules), "pages-alerts holds alerting rules only"
    names = [r["alert"] for r in rules]
    assert len(names) == len(set(names)), f"duplicate alert names: {names}"
    return {r["alert"]: r for r in rules}


def walk(node, path=()):
    """Yield (path, value) for every value in a nested dict/list."""
    yield path, node
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, path + (i,))


# --- always, deployed or rolled back -------------------------------------------


def test_pages_state():
    """The tree is either deployed or in the marked rollback, never an unmarked empty render."""
    assert pages_state() in {"deployed", "rolled-back"}


def test_rollback_edit_is_recognised(tmp_path):
    d = pages_copy(tmp_path, rolled_back_text(deployed_kustomization()))
    assert pages_state(d) == "rolled-back"
    assert render(d) == []


def test_unmarked_empty_render_fails(tmp_path):
    d = pages_copy(tmp_path, rolled_back_text(deployed_kustomization()).replace(ROLLBACK_MARKER + "\n", ""))
    with pytest.raises(AssertionError, match="no rollback marker"):
        pages_state(d)


def test_marker_on_a_rendering_kustomization_fails(tmp_path):
    k = deployed_kustomization()
    d = pages_copy(tmp_path, k.replace("\napiVersion:", f"\n{ROLLBACK_MARKER}\napiVersion:", 1))
    with pytest.raises(AssertionError, match="resources must be"):
        pages_state(d)


def test_rollback_that_keeps_the_generator_fails(tmp_path):
    k = rolled_back_text(deployed_kustomization())
    d = pages_copy(tmp_path, k + "configMapGenerator:\n  - name: git-pages-config\n    files:\n      - config.toml\n")
    with pytest.raises(AssertionError, match="keeps only"):
        pages_state(d)


def test_app_registered():
    """Same in both states: a rollback never edits or deletes the Application."""
    app = yaml.safe_load((REPO / PAGES_APP).read_text())
    assert app["kind"] == "Application"
    assert app["metadata"]["name"] == "pages"
    assert app["metadata"]["namespace"] == "argo-cd"
    # With no resources-finalizer, deleting the Application would orphan everything
    # and leave it running, so rollback renders nothing instead.
    assert "finalizers" not in app["metadata"]
    assert app["spec"]["source"]["repoURL"] == "https://github.com/ChristopherJMiller/luma-homeops.git"
    assert app["spec"]["source"]["path"] == PAGES_DIR
    assert app["spec"]["destination"] == {"server": "https://kubernetes.default.svc", "namespace": NAMESPACE}

    sync = app["spec"]["syncPolicy"]
    assert sync["automated"] == {"prune": True, "selfHeal": True, "allowEmpty": True}
    assert "CreateNamespace=true" in sync.get("syncOptions", [])

    root = yaml.safe_load((REPO / "cluster/applications/kustomization.yaml").read_text())
    assert root["resources"].count(Path(PAGES_APP).name) == 1


def test_renovate_never_automerges_pages():
    rules = json.loads((REPO / ".renovaterc.json").read_text())["packageRules"]
    for image in RENOVATE_IMAGES:
        idx = [i for i, r in enumerate(rules) if image in r.get("matchPackageNames", [])]
        assert len(idx) == 1, f"exactly one packageRule names {image}"
        rule = rules[idx[0]]
        assert rule["automerge"] is False, f"{image} must never automerge"
        assert "kubernetes" in rule.get("matchManagers", []), f"{image}: the Deployment pin is the kubernetes manager's"
        assert rule.get("description"), f"{image}: say why in a description"
        later = [r for r in rules[idx[0] + 1:] if r.get("automerge") is True and image in r.get("matchPackageNames", [image])]
        assert not later, f"a later packageRule turns automerge back on for {image}"
    desc = {image: next(r for r in rules if image in r.get("matchPackageNames", []))["description"] for image in RENOVATE_IMAGES}
    for must in ("pre-1.0", "changelog", "2 weeks", "suite"):
        assert must in desc[RENOVATE_IMAGES[0]], f"git-pages rule description must mention {must!r}"
    assert "by hand" in desc[RENOVATE_IMAGES[1]]


def test_pages_hook_wired():
    cfg = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text())
    hooks = {h["id"]: h for repo in cfg["repos"] for h in repo["hooks"]}
    h = hooks["pages-manifests"]
    assert h["entry"] == "python -m pytest -q -p no:cacheprovider scripts/test_pages_manifests.py"
    assert h["language"] == "system" and h["pass_filenames"] is False
    for path in HOOK_FILES_MUST_MATCH:
        assert re.search(h["files"], path), f"pages-manifests must run when {path} changes"
    assert not re.search(h["files"], "cluster/media/kustomization.yaml")


# --- deployed state only ---------------------------------------------------------


def test_pages_kustomization():
    require_deployed()
    k = yaml.safe_load((REPO / PAGES_DIR / "kustomization.yaml").read_text())
    assert yaml.safe_load(DEPLOYED_KUSTOMIZATION) == k, "keep DEPLOYED_KUSTOMIZATION equal to the real file"
    # Argo and these tests read only the sealed siblings, never the cleartext source.
    assert not [r for r in k["resources"] if ".secret." in r]


def test_pages_renders_exactly():
    require_deployed()
    docs = render(PAGES_DIR)
    assert sorted(d["kind"] for d in docs) == PAGES_KINDS
    for d in docs:
        if d["kind"] != "Namespace":
            assert d["metadata"]["namespace"] == NAMESPACE, f"{d['kind']}/{d['metadata']['name']} is outside {NAMESPACE}"


def test_namespace_restricted():
    require_deployed()
    ns = named(render(PAGES_DIR), "Namespace", NAMESPACE)
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"


def test_sealed_secrets_scoped():
    """Strict-scoped (name + namespace), and carrying exactly the keys the pods read.
    Key names are not secret; the values stay sealed."""
    require_deployed()
    docs = render(PAGES_DIR)
    assert {d["metadata"]["name"] for d in of_kind(docs, "SealedSecret")} == set(SEALED_KEYS)
    for name, keys in SEALED_KEYS.items():
        s = named(docs, "SealedSecret", name)
        assert set(s["spec"]["encryptedData"]) == keys
        assert all(not k.startswith("sealedsecrets.bitnami.com/") for k in s["metadata"].get("annotations", {}))
        assert s["spec"]["template"]["metadata"]["name"] == name
        assert s["spec"]["template"]["metadata"]["namespace"] == NAMESPACE


def test_images_pinned():
    require_deployed()
    docs = render(PAGES_DIR)
    assert container(docs, "git-pages")["image"] == GIT_PAGES_IMAGE
    assert re.fullmatch(RELAY_IMAGE_RE, container(docs, "pages-relay")["image"])
    images = [c["image"] for d in of_kind(docs, "Deployment") for key in ("initContainers", "containers")
              for c in d["spec"]["template"]["spec"].get(key, [])]
    assert len(images) == 2, f"no other images: {images}"


def test_pods_hardened():
    require_deployed()
    docs = render(PAGES_DIR)
    assert {d["metadata"]["name"] for d in of_kind(docs, "Deployment")} == set(RESOURCES)
    for d in of_kind(docs, "Deployment"):
        name = d["metadata"]["name"]
        assert d["spec"]["replicas"] == 1, name
        pod = d["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False, name
        assert pod["enableServiceLinks"] is False, name
        psc = pod["securityContext"]
        assert psc["runAsNonRoot"] is True and psc["runAsUser"] == 65532, name
        assert psc["seccompProfile"] == {"type": "RuntimeDefault"}, name
        for key in ("hostNetwork", "hostPID", "hostIPC", "initContainers", "ephemeralContainers"):
            assert key not in pod, f"{name}: {key} must not be set"
        assert not [v for v in pod.get("volumes", []) if "hostPath" in v], f"{name}: no hostPath"
        (c,) = pod["containers"]
        sc = c["securityContext"]
        assert sc["allowPrivilegeEscalation"] is False, name
        assert sc["readOnlyRootFilesystem"] is True, name
        assert sc["capabilities"] == {"drop": ["ALL"]}, name
        assert not sc.get("privileged"), name
        assert c["resources"]["requests"]["memory"] and c["resources"]["limits"]["memory"], name
        assert c["resources"] == RESOURCES[name], f"{name}: resources (Task 10 Step 6 moves them)"

    gp = pod_spec(docs, "git-pages")
    tmp = [v for v in gp["volumes"] if v["name"] == "tmp"]
    assert tmp == [{"name": "tmp", "emptyDir": {"sizeLimit": "2Gi"}}]
    assert {"name": "tmp", "mountPath": "/tmp"} in container(docs, "git-pages")["volumeMounts"]


def test_git_pages_deployment():
    require_deployed()
    docs = render(PAGES_DIR)
    c = container(docs, "git-pages")
    assert c["command"] == GIT_PAGES_COMMAND
    assert "args" not in c
    assert c["envFrom"] == [{"secretRef": {"name": "git-pages-s3"}}]
    assert env_of(c) == {"TMPDIR": "/tmp", "HOME": "/tmp"}
    assert {p["name"]: p["containerPort"] for p in c["ports"]} == SERVICES["git-pages"]
    assert c["livenessProbe"]["httpGet"] == {"path": "/metrics", "port": 3002}
    assert c["readinessProbe"]["tcpSocket"] == {"port": 3000}

    # The config is the generated, content-hashed ConfigMap, so a config change rolls the pod.
    (cm,) = of_kind(docs, "ConfigMap")
    assert re.fullmatch(r"git-pages-config-[a-z0-9]{10}", cm["metadata"]["name"])
    vols = {v["name"]: v for v in pod_spec(docs, "git-pages")["volumes"]}
    assert vols["config"]["configMap"]["name"] == cm["metadata"]["name"]
    assert {"name": "config", "mountPath": "/etc/git-pages", "readOnly": True} in c["volumeMounts"]
    assert set(vols) == {"config", "tmp"}


def test_git_pages_config():
    require_deployed()
    (cm,) = of_kind(render(PAGES_DIR), "ConfigMap")
    assert set(cm["data"]) == {"config.toml"}
    assert cm["data"]["config.toml"] == CONFIG_TOML.read_text()
    cfg = tomllib.loads(cm["data"]["config.toml"])

    w = cfg["wildcard"]
    assert w == [{"domain": "realliance.page", "clone-url": "https://git.realliance.net/<user>/<project>.git",
                  "index-repo": "pages", "index-repo-branch": "pages"}]  # no "authorization" (D7)
    assert cfg["features"] == [] and cfg["server"] == {"pages": "tcp/:3000", "proxy": "-", "metrics": "tcp/:3002"}
    # 0.10.0 renamed [server].caddy to [server].proxy and rejects unknown keys at start.
    assert "caddy" not in cfg["server"] and not re.search(r"(?mi)^\s*caddy\s*=", CONFIG_TOML.read_text())
    assert not [p for p, _ in walk(cfg) if p and p[-1] == "caddy"], "no `caddy` key anywhere"
    L = cfg["limits"]
    assert L["max-site-size"] == "128MB" and L["allowed-repository-url-prefixes"] == ["https://git.realliance.net/"]
    assert not L["allow-basic-auth"] and "set-cookie" not in {h.lower() for h in L["allowed-custom-headers"]}
    assert set(L["allowed-custom-headers"]) == ALLOWED_HEADERS  # D7 list, exact
    s3 = cfg["storage"]["s3"]
    assert s3["bucket"] == "realliance-pages"
    assert "secret-access-key" not in s3 and "access-key-id" not in s3, "credentials come from git-pages-s3 only"
    assert "acme" not in cfg and "secrets" not in cfg

    cfg["limits"]["allowed-custom-headers"] = sorted(L["allowed-custom-headers"])
    assert cfg == EXPECTED_CONFIG


def test_relay_env():
    require_deployed()
    docs = render(PAGES_DIR)
    c = container(docs, "pages-relay")
    assert env_of(c) == RELAY_ENV
    assert "envFrom" not in c and "command" not in c and "args" not in c
    assert c["volumeMounts"] == [{"name": "webhook-secret", "mountPath": "/etc/pages-relay", "readOnly": True}]
    (vol,) = pod_spec(docs, "pages-relay")["volumes"]
    assert vol["name"] == "webhook-secret"
    assert vol["secret"]["secretName"] == "pages-relay-webhook"
    assert vol["secret"]["items"] == [{"key": "webhook-secret", "path": "webhook-secret"}]
    assert {p["name"]: p["containerPort"] for p in c["ports"]} == SERVICES["pages-relay"]
    for probe in ("livenessProbe", "readinessProbe"):
        assert c[probe]["httpGet"] == {"path": "/healthz", "port": "http"}, probe


def test_services_internal():
    """git-pages.pages.svc.cluster.local:3000 and pages-relay.pages.svc.cluster.local:8080, ClusterIP only."""
    require_deployed()
    docs = render(PAGES_DIR)
    assert {s["metadata"]["name"] for s in of_kind(docs, "Service")} == set(SERVICES)
    for name, ports in SERVICES.items():
        svc = named(docs, "Service", name)
        assert svc["spec"].get("type", "ClusterIP") == "ClusterIP", f"{name} is internal only until Task 9"
        assert {p["name"]: p["port"] for p in svc["spec"]["ports"]} == ports
        assert all(p["targetPort"] == p["name"] for p in svc["spec"]["ports"])
        labels = named(docs, "Deployment", name)["spec"]["template"]["metadata"]["labels"]
        assert svc["spec"]["selector"] and svc["spec"]["selector"].items() <= labels.items()
        assert named(docs, "Deployment", name)["spec"]["selector"]["matchLabels"] == svc["spec"]["selector"]


def test_monitors_and_rules_labelled():
    require_deployed()
    docs = render(PAGES_DIR)
    monitors = of_kind(docs, "ServiceMonitor")
    assert {m["metadata"]["name"] for m in monitors} == set(SERVICES)
    for m in monitors:
        assert m["metadata"]["labels"]["release"] == "prometheus", m["metadata"]["name"]
    assert named(docs, "PrometheusRule", "pages-alerts")["metadata"]["labels"]["release"] == "prometheus"
    assert set(alert_rules(docs)) == set(ALERT_EXPRS)


def test_service_monitors_select_services():
    require_deployed()
    docs = render(PAGES_DIR)
    want = {"git-pages": [("metrics", "/metrics")], "pages-relay": [("http", "/metrics")]}
    for name, endpoints in want.items():
        m, svc = named(docs, "ServiceMonitor", name), named(docs, "Service", name)
        sel = m["spec"]["selector"]
        assert not sel.get("matchExpressions") and sel["matchLabels"], "an empty selector would scrape every Service"
        assert sel["matchLabels"].items() <= svc["metadata"]["labels"].items()
        assert m["spec"]["namespaceSelector"] == {"matchNames": [NAMESPACE]}
        assert [(e["port"], e.get("path", "/metrics")) for e in m["spec"]["endpoints"]] == endpoints
        # No jobLabel: the job is the Service name, which PagesDown's absent() relies on.
        assert "jobLabel" not in m["spec"]
    # Selectors must not overlap, or one Service is scraped twice under two jobs.
    for name in want:
        sel = named(docs, "ServiceMonitor", name)["spec"]["selector"]["matchLabels"]
        hits = [s["metadata"]["name"] for s in of_kind(docs, "Service") if sel.items() <= s["metadata"]["labels"].items()]
        assert hits == [name]


def test_alerts():
    require_deployed()
    rules = alert_rules(render(PAGES_DIR))
    outcomes = relay_outcomes()
    for name, r in rules.items():
        assert " ".join(r["expr"].split()) == ALERT_EXPRS[name], name
        assert r["labels"] == {"severity": "warning"}, name
        assert r["annotations"]["summary"] and r["annotations"]["description"], name
        used = metrics_used(r["expr"])
        assert used <= KNOWN_METRICS, f"{name} uses unknown metrics {used - KNOWN_METRICS}"
        assert outcomes_matched(r["expr"]) <= outcomes, f"{name} matches outcomes the relay never counts"
    assert rules["PagesDown"]["for"] == "10m"
    # F23: event alerts fire on the first event; a `for:` would hide a one-off.
    assert all("for" not in r for n, r in rules.items() if n != "PagesDown")
    assert "128 MB" in rules["PagesSiteUpdateFailing"]["annotations"]["description"]
    assert "kubectl -n pages logs deploy/git-pages" in rules["PagesSiteUpdateFailing"]["annotations"]["description"]
    assert "docs/pages.md" in rules["PagesTakedownRequested"]["annotations"]["description"]
    assert "Takedown" in rules["PagesTakedownRequested"]["annotations"]["description"]


def test_update_failing_alert_catches_new_series():
    """RF5: a brand-new git_pages_site_update_error{cause} series (first error since a
    restart) has no increase(), so the `unless ... offset` branch must catch it."""
    require_deployed()
    expr = " ".join(alert_rules(render(PAGES_DIR))["PagesSiteUpdateFailing"]["expr"].split())
    assert "unless git_pages_site_update_error offset 30m" in expr
    assert "increase(git_pages_site_update_error[30m])" in expr


def test_relay_outcomes_alerted_once():
    """Every outcome in ALERTED_OUTCOMES raises exactly one alert; ignored_* and forwarded none."""
    require_deployed()
    rules = alert_rules(render(PAGES_DIR))
    assert ALERTED_OUTCOMES <= relay_outcomes()
    per_alert = {n: outcomes_matched(r["expr"]) for n, r in rules.items()}
    for o in sorted(ALERTED_OUTCOMES):
        hits = [n for n, m in per_alert.items() if o in m]
        assert len(hits) == 1, f"outcome {o!r} must raise exactly one alert, got {hits}"
    assert set().union(*per_alert.values()) == ALERTED_OUTCOMES


def test_no_certificate_in_pages():
    """One shared wildcard cert (traefik/wildcard-tls): no Certificate, no tls.secretName here."""
    require_deployed()
    docs = render(PAGES_DIR)
    assert not of_kind(docs, "Certificate")
    for d in docs:
        for path, _ in walk(d):
            assert not (len(path) >= 2 and path[-2:] == ("tls", "secretName")), f"{d['kind']}: tls.secretName at {path}"
    assert not of_kind(docs, "Ingress") and not of_kind(docs, "IngressRoute"), "no public route until Task 9"


def test_forgejo_webhook_allowlist():
    """Forgejo's default `external` blocks webhooks to private IPs; the relay is in-cluster (Task 6)."""
    app = yaml.safe_load((REPO / "cluster/applications/forgejo-release.yaml").read_text())
    values = app["spec"]["source"]["helm"]["valuesObject"]
    assert values["gitea"]["config"]["webhook"] == {"ALLOWED_HOST_LIST": "external,pages-relay.pages.svc.cluster.local"}
