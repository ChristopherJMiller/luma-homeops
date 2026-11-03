"""Static tests for the NetworkPolicy enforcer and the policies that use it.

    python -m pytest -q -p no:cacheprovider scripts/test_netpol_manifests.py

Runs as the `netpol-manifests` pre-commit hook. Everything is rendered with
`kustomize build`, the same build Argo runs, and checked against the values
the design fixes verbatim:
docs/superpowers/specs/2026-11-03-networkpolicy-enforcement-design.md §6.1.

These tests never read `*.secret.*` files: they open only the paths named here.
"""

import hashlib
import re
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent

ENFORCER_DIR = "cluster/kube-network-policies"
ENFORCER_APP = "cluster/applications/kube-network-policies.yaml"
UPSTREAM = REPO / ENFORCER_DIR / "upstream-install.yaml"

# install.yaml at upstream tag v1.1.3 (commit d3fc697c1acaa0c8872c262ff3e04d8299eac6b3),
# vendored byte-identical. Its own image line still says v1.1.2; daemonset-patch.yaml
# sets the real image.
UPSTREAM_SHA256 = "a40594785fce33ded02582117f3b78123af53c62f3b73d514f62c5541974984a"

# Never below v1.1.1 (#380: hostNetwork pods enrolled node IPs in the managed set).
ENFORCER_MIN_VERSION = (1, 1, 1)
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


def render(path: str) -> list[dict]:
    """`kustomize build <path>` (relative to the repo root), parsed into documents."""
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


def enforcer_daemonset() -> dict:
    return one(render(ENFORCER_DIR), "DaemonSet")


def test_upstream_vendored_byte_identical():
    assert hashlib.sha256(UPSTREAM.read_bytes()).hexdigest() == UPSTREAM_SHA256


def test_enforcer_daemonset():
    ds = enforcer_daemonset()
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

    # A3a only: Task 4 (A3b) drops the hostname and expects {"kubernetes.io/os": "linux"}.
    assert pod["nodeSelector"] == {"kubernetes.io/os": "linux", "kubernetes.io/hostname": "bottom"}


def test_enforcer_monitoring():
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


def test_enforcer_app():
    app = yaml.safe_load((REPO / ENFORCER_APP).read_text())
    assert app["kind"] == "Application"
    assert app["metadata"]["name"] == "kube-network-policies"
    assert app["spec"]["source"]["path"] == ENFORCER_DIR
    assert app["spec"]["destination"]["namespace"] == "kube-system"

    sync = app["spec"]["syncPolicy"]
    assert sync["automated"] == {"prune": True, "selfHeal": True, "allowEmpty": True}
    opts = sync.get("syncOptions", [])
    assert "ServerSideApply=true" in opts
    assert not [o for o in opts if o.startswith("CreateNamespace")], "kube-system is never Argo's to create"

    root = yaml.safe_load((REPO / "cluster/applications/kustomization.yaml").read_text())
    assert Path(ENFORCER_APP).name in root["resources"]
