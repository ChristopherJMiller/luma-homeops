"""Static tests for the NetworkPolicy enforcer and the policies that use it.

    python -m pytest -q -p no:cacheprovider scripts/test_netpol_manifests.py

Runs as the `netpol-manifests` pre-commit hook. Everything is rendered with
`kustomize build`, the same build Argo runs, and checked against the values
the design fixes verbatim:
docs/superpowers/specs/2026-11-03-networkpolicy-enforcement-design.md §6.1.
Everything privileged (RBAC, securityContext, host namespaces, hostPaths) must
equal the vendored upstream manifest; only daemonset-patch.yaml changes it.

These tests never read `*.secret.*` files: they open only the paths named here.

Rolling back the enforcer
-------------------------
Global Constraints: roll back by rendering nothing, never by deleting the
Application. The pre-approved rollback is one commit that edits
cluster/kube-network-policies/kustomization.yaml so that, below its header
comment, it reads exactly:

    # netpol: rolled back (Global Constraints)
    apiVersion: kustomize.config.k8s.io/v1beta1
    kind: Kustomization
    resources: []

That is: add the marker line above `apiVersion:`, replace the `resources:` list
with `[]`, and delete the `patches:` block. Leave upstream-install.yaml, the
other files and cluster/applications/kube-network-policies.yaml as they are.
Argo then prunes every object (`allowEmpty: true`).

In that state these tests assert that the build renders nothing and that the
Application is unchanged, keep checking the vendored sha256, and skip the
deployed-state invariants. `resources: []` WITHOUT the marker fails: an
accidental empty render must never pass. To redeploy, `git revert` the
rollback commit.
"""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

ENFORCER_DIR = "cluster/kube-network-policies"
ENFORCER_APP = "cluster/applications/kube-network-policies.yaml"
UPSTREAM = REPO / ENFORCER_DIR / "upstream-install.yaml"

# install.yaml at upstream tag v1.1.3 (commit d3fc697c1acaa0c8872c262ff3e04d8299eac6b3),
# vendored byte-identical. Its own image line still says v1.1.2; daemonset-patch.yaml
# sets the real image.
UPSTREAM_SHA256 = "a40594785fce33ded02582117f3b78123af53c62f3b73d514f62c5541974984a"

ROLLBACK_MARKER = "# netpol: rolled back (Global Constraints)"

ENFORCER_RESOURCES = ["upstream-install.yaml", "podmonitor.yaml", "prometheusrule.yaml"]
ENFORCER_PATCHES = [{"path": "daemonset-patch.yaml"}]
# The deployed kustomization's shape. test_enforcer_kustomization keeps it equal to
# the real file; the rollback tests use it while the real file is rolled back.
DEPLOYED_KUSTOMIZATION = """\
---
# kube-network-policies (deployed shape; the real file carries the full header)
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - upstream-install.yaml
  - podmonitor.yaml
  - prometheusrule.yaml
patches:
  - path: daemonset-patch.yaml
"""
ENFORCER_KINDS = ["ClusterRole", "ClusterRoleBinding", "DaemonSet", "PodMonitor", "PrometheusRule", "ServiceAccount"]

# Never below v1.1.1 (#380: hostNetwork pods enrolled node IPs in the managed set).
ENFORCER_MIN_VERSION = (1, 1, 1)
ENFORCER_IMAGE = "registry.k8s.io/networking/kube-network-policies"
ENFORCER_IMAGE_RE = r"registry\.k8s\.io/networking/kube-network-policies:v(\d+)\.(\d+)\.(\d+)@sha256:[0-9a-f]{64}"

ENFORCER_ARGS = [
    "/bin/netpol",
    "--hostname-override=$(MY_NODE_NAME)",
    "--v=1",
    "--log-verdicts=deny",
    "--fail-open=true",
    "--nfqueue-id=98",
    "--metrics-bind-address=:9080",
    "--disable-nri=true",
]

ENFORCER_ALERTS = {
    "KubeNetworkPoliciesAgentNotReady",
    "KubeNetworkPoliciesQueueDrops",
    "KubeNetworkPoliciesVerdictLogDropped",
}

# Every kube_network_policies_* series v1.1.3 registers (pkg/dataplane/metrics.go,
# pkg/networkpolicy/metrics.go). An alert on any other name would never fire.
ENFORCER_METRICS = {
    "kube_network_policies_packet_process_time",
    "kube_network_policies_packet_process_duration_microseconds",
    "kube_network_policies_packet_count",
    "kube_network_policies_nfqueue_queue_total",
    "kube_network_policies_nfqueue_queue_dropped",
    "kube_network_policies_nfqueue_user_dropped",
    "kube_network_policies_nfqueue_packet_id",
    "kube_network_policies_verdict_log_dropped",
    "kube_network_policies_unresolved_ip_count",
}

# pre-commit hooks that rewrite files; none may touch the vendored manifest.
REWRITING_HOOKS = {"yamlfmt", "end-of-file-fixer", "trailing-whitespace", "mixed-line-ending", "fix-byte-order-marker"}


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


def one(docs: list[dict], kind: str) -> dict:
    found = [d for d in docs if d.get("kind") == kind]
    assert len(found) == 1, f"expected exactly one {kind}, got {len(found)}"
    return found[0]


def upstream_docs() -> list[dict]:
    return [d for d in yaml.safe_load_all(UPSTREAM.read_text()) if d]


def has_marker(text: str) -> bool:
    return any(line.strip() == ROLLBACK_MARKER for line in text.splitlines())


def enforcer_state(app_dir=ENFORCER_DIR) -> str:
    """'deployed' or 'rolled-back' (the marked rollback in the module docstring).

    Fails on anything else: an empty render without the marker, the marker on a
    kustomization that still renders, or a rollback that kept patches/components.
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
        f"the pre-approved rollback, add the line {ROLLBACK_MARKER!r} (see scripts/test_netpol_manifests.py)."
    )
    return "deployed"


def require_deployed():
    if enforcer_state() == "rolled-back":
        pytest.skip(f"rolled back: {ENFORCER_DIR} renders nothing ({ROLLBACK_MARKER})")


def rolled_back_text(text: str) -> str:
    """The documented rollback edit, applied to a deployed kustomization.yaml."""
    head, sep, _ = text.partition("\nresources:\n")
    assert sep and "\napiVersion:" in head, "kustomization.yaml no longer has the expected shape"
    return head.replace("\napiVersion:", f"\n{ROLLBACK_MARKER}\napiVersion:", 1) + "\nresources: []\n"


def deployed_kustomization() -> str:
    """The real kustomization.yaml while deployed (so the documented edit is proven on
    the real file); its deployed shape while the real file is rolled back."""
    text = (REPO / ENFORCER_DIR / "kustomization.yaml").read_text()
    return DEPLOYED_KUSTOMIZATION if has_marker(text) else text


def enforcer_copy(tmp_path: Path, kustomization: str) -> Path:
    d = tmp_path / "kube-network-policies"
    shutil.copytree(REPO / ENFORCER_DIR, d)
    (d / "kustomization.yaml").write_text(kustomization)
    return d


# --- always, deployed or rolled back -------------------------------------------


def test_upstream_vendored_byte_identical():
    assert hashlib.sha256(UPSTREAM.read_bytes()).hexdigest() == UPSTREAM_SHA256


def test_enforcer_state():
    """The tree is either deployed or in the marked rollback, never an unmarked empty render."""
    assert enforcer_state() in {"deployed", "rolled-back"}


def test_rollback_edit_is_recognised(tmp_path):
    d = enforcer_copy(tmp_path, rolled_back_text(deployed_kustomization()))
    assert enforcer_state(d) == "rolled-back"
    assert render(d) == []


def test_unmarked_empty_render_fails(tmp_path):
    k = deployed_kustomization()
    d = enforcer_copy(tmp_path, rolled_back_text(k).replace(ROLLBACK_MARKER + "\n", ""))
    with pytest.raises(AssertionError, match="no rollback marker"):
        enforcer_state(d)


def test_marker_on_a_rendering_kustomization_fails(tmp_path):
    k = deployed_kustomization()
    d = enforcer_copy(tmp_path, k.replace("\napiVersion:", f"\n{ROLLBACK_MARKER}\napiVersion:", 1))
    with pytest.raises(AssertionError, match="resources must be"):
        enforcer_state(d)


def test_rollback_that_keeps_patches_fails(tmp_path):
    k = rolled_back_text(deployed_kustomization())
    d = enforcer_copy(tmp_path, k + "patches:\n  - path: daemonset-patch.yaml\n")
    with pytest.raises(AssertionError, match="keeps only"):
        enforcer_state(d)


def test_enforcer_app():
    """Same in both states: a rollback never edits or deletes the Application."""
    app = yaml.safe_load((REPO / ENFORCER_APP).read_text())
    assert app["kind"] == "Application"
    assert app["metadata"]["name"] == "kube-network-policies"
    # The rollback relies on this: with no finalizer, deleting the Application would
    # orphan the DaemonSet and leave it running, so rollback renders nothing instead.
    assert "finalizers" not in app["metadata"]
    assert app["spec"]["source"]["path"] == ENFORCER_DIR
    assert app["spec"]["destination"] == {"server": "https://kubernetes.default.svc", "namespace": "kube-system"}

    sync = app["spec"]["syncPolicy"]
    assert sync["automated"] == {"prune": True, "selfHeal": True, "allowEmpty": True}
    opts = sync.get("syncOptions", [])
    assert "ServerSideApply=true" in opts
    assert "PrunePropagationPolicy=foreground" in opts
    assert not [o for o in opts if o.startswith("CreateNamespace")], "kube-system is never Argo's to create"

    root = yaml.safe_load((REPO / "cluster/applications/kustomization.yaml").read_text())
    assert Path(ENFORCER_APP).name in root["resources"]


def test_enforcer_renovate():
    rules = json.loads((REPO / ".renovaterc.json").read_text())["packageRules"]
    idx = [i for i, r in enumerate(rules) if ENFORCER_IMAGE in r.get("matchPackageNames", [])]
    assert len(idx) == 1, "exactly one packageRule names the enforcer image"
    g = rules[idx[0]]
    assert g["automerge"] is False and g["groupName"] == "kube-network-policies"
    notes = " ".join(g.get("prBodyNotes", []))
    for must in ("install.yaml", "UPSTREAM_SHA256", "release notes", "canary"):
        assert must in notes, f"prBodyNotes must mention {must!r}"
    later = [r for r in rules[idx[0] + 1:] if r.get("automerge") is True]
    assert not later, "no later packageRule may turn automerge back on"

    vendored = f"{ENFORCER_DIR}/upstream-install.yaml"
    off = [i for i, r in enumerate(rules) if r.get("matchFileNames") == [vendored]]
    assert len(off) == 1 and rules[off[0]]["enabled"] is False
    assert off[0] > idx[0], "the vendored-file rule must come after the group rule to win"


def test_vendored_file_exempt_from_rewriting_hooks():
    """A rewritten vendored file would fail the sha test in a confusing hook loop."""
    cfg = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text())
    hooks = {h["id"]: h for repo in cfg["repos"] for h in repo["hooks"]}
    vendored = f"{ENFORCER_DIR}/upstream-install.yaml"
    for hid in sorted(REWRITING_HOOKS):
        assert hid in hooks, f"hook {hid} is gone; update REWRITING_HOOKS"
        exclude = hooks[hid].get("exclude", "^$")
        assert re.search(exclude, vendored), f"{hid} would rewrite {vendored}"
        assert not re.search(exclude, f"{ENFORCER_DIR}/daemonset-patch.yaml"), f"{hid} must still format our own files"


# --- deployed state only ---------------------------------------------------------


def test_enforcer_kustomization():
    require_deployed()
    k = yaml.safe_load((REPO / ENFORCER_DIR / "kustomization.yaml").read_text())
    assert set(k) == {"apiVersion", "kind", "resources", "patches"}, "no namespace, components, images or other transformers"
    assert k["resources"] == ENFORCER_RESOURCES
    assert k["patches"] == ENFORCER_PATCHES
    assert yaml.safe_load(DEPLOYED_KUSTOMIZATION) == k, "keep DEPLOYED_KUSTOMIZATION equal to the real file"


def test_enforcer_renders_exactly():
    require_deployed()
    assert sorted(d["kind"] for d in render(ENFORCER_DIR)) == ENFORCER_KINDS


def test_privileged_objects_match_upstream():
    """RBAC, the SA, host namespaces, hostPaths and securityContext are upstream's, unwidened."""
    require_deployed()
    docs, up = render(ENFORCER_DIR), upstream_docs()
    for kind in ("ServiceAccount", "ClusterRole", "ClusterRoleBinding"):
        assert one(docs, kind) == one(up, kind), f"{kind} differs from upstream-install.yaml"

    pod, up_pod = one(docs, "DaemonSet")["spec"]["template"]["spec"], one(up, "DaemonSet")["spec"]["template"]["spec"]
    for key in ("hostPID", "hostIPC", "shareProcessNamespace", "initContainers", "ephemeralContainers"):
        assert key not in pod, f"{key} must not be set"
    assert pod["serviceAccountName"] == up_pod["serviceAccountName"]
    (c,), (up_c,) = pod["containers"], up_pod["containers"]
    assert c["securityContext"] == up_c["securityContext"]
    assert pod["volumes"] == [v for v in up_pod["volumes"] if v["name"] == "netns"]
    assert c["volumeMounts"] == [m for m in up_c["volumeMounts"] if m["name"] == "netns"]


def test_enforcer_daemonset():
    require_deployed()
    ds = one(render(ENFORCER_DIR), "DaemonSet")
    assert ds["metadata"]["namespace"] == "kube-system"
    spec = ds["spec"]
    pod = spec["template"]["spec"]
    (c,) = pod["containers"]

    m = re.fullmatch(ENFORCER_IMAGE_RE, c["image"])
    assert m, f"image {c['image']!r} is not a digest-pinned kube-network-policies release"
    assert tuple(int(x) for x in m.groups()) >= ENFORCER_MIN_VERSION

    assert c["args"] == ENFORCER_ARGS
    assert {v["name"] for v in pod["volumes"]} == {"netns"}
    assert {v["mountPath"] for v in c["volumeMounts"]} == {"/var/run/netns"}

    assert pod["hostNetwork"] is True
    assert pod["priorityClassName"] == "system-node-critical"
    assert pod["tolerations"] == [{"operator": "Exists"}]
    assert spec["updateStrategy"]["rollingUpdate"]["maxUnavailable"] == 1

    assert c["resources"] == {
        "requests": {"cpu": "50m", "memory": "64Mi"},
        "limits": {"memory": "256Mi"},
    }
    metrics = [p for p in c.get("ports", []) if p.get("name") == "metrics"]
    assert len(metrics) == 1 and metrics[0]["containerPort"] == 9080
    assert {"name": "MY_NODE_NAME", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}} in c["env"]

    # One agent on every node since A3b (Task 4); A3a ran on bottom only.
    assert pod["nodeSelector"] == {"kubernetes.io/os": "linux"}


def test_enforcer_monitoring():
    require_deployed()
    docs = render(ENFORCER_DIR)

    pm = one(docs, "PodMonitor")
    assert pm["metadata"]["namespace"] == "kube-system"
    assert pm["metadata"]["labels"]["release"] == "prometheus"
    assert pm["spec"]["selector"] == {"matchLabels": {"app": "kube-network-policies"}}
    assert [e["port"] for e in pm["spec"]["podMetricsEndpoints"]] == ["metrics"]

    pr = one(docs, "PrometheusRule")
    assert pr["metadata"]["namespace"] == "prometheus"
    assert pr["metadata"]["labels"]["release"] == "prometheus"
    rules = [r for g in pr["spec"]["groups"] for r in g["rules"]]
    assert {r["alert"] for r in rules} == ENFORCER_ALERTS
    for r in rules:
        assert r["labels"]["severity"] == "warning", r["alert"]
        used = set(re.findall(r"\bkube_network_policies_[a-z_]+", r["expr"]))
        assert used <= ENFORCER_METRICS, f"{r['alert']} uses unknown metrics {used - ENFORCER_METRICS}"


def test_podmonitor_selects_enforcer_pods():
    """The PodMonitor must select the rendered pod template, on a port the container has."""
    require_deployed()
    docs = render(ENFORCER_DIR)
    ds, pm = one(docs, "DaemonSet"), one(docs, "PodMonitor")
    labels = ds["spec"]["template"]["metadata"]["labels"]
    sel = pm["spec"]["selector"]

    assert not sel.get("matchExpressions")
    assert sel["matchLabels"], "an empty selector would scrape every pod"
    assert sel["matchLabels"].items() <= labels.items()
    assert ds["metadata"]["namespace"] in pm["spec"]["namespaceSelector"]["matchNames"]

    ports = {p["name"] for c in ds["spec"]["template"]["spec"]["containers"] for p in c.get("ports", [])}
    assert {e["port"] for e in pm["spec"]["podMetricsEndpoints"]} <= ports


def test_canary_matrix_bmcs_match_ipmi_scrapeconfig():
    """Every BMC the canary expects to be dropped is a BMC we scrape (D13): drift fails loudly.

    The matrix rows for port 443 whose note says BMC are the dropped-BMC rows; their IPs are
    copied from cluster/ipmi-exporter/scrapeconfig.yaml.
    """
    sc = yaml.safe_load((REPO / "cluster/ipmi-exporter/scrapeconfig.yaml").read_text())
    scraped = {t for cfg in sc["spec"]["staticConfigs"] for t in cfg["targets"]}
    assert scraped, "scrapeconfig has no targets"

    bmcs = set()
    for raw in (REPO / "cluster/netpol-canary/matrix.txt").read_text().splitlines():
        row = raw.split()
        if row and not row[0].startswith("#") and "BMC" in row[5:]:
            bmcs.add(row[2])
    assert bmcs, "matrix has no BMC rows"
    assert bmcs == scraped, f"matrix BMCs {sorted(bmcs)} != scraped {sorted(scraped)}"
