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

Rolling back the canary
-----------------------
Same rule and marker for cluster/netpol-canary/kustomization.yaml. Below its
header comment it becomes exactly the marker line, `apiVersion:`, `kind:` and
`resources: []`: delete everything after the `resources:` list (the ConfigMap
generator, and from Task 7 on its components and patches). Leave
cluster/applications/netpol-canary.yaml as it is. The canary tests then skip
the deployed-state checks.
"""

import ast
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

CANARY_DIR = "cluster/netpol-canary"
CANARY_APP = "cluster/applications/netpol-canary.yaml"
CANARY_NS = "netpol-canary"
CANARY_KINDS = sorted(
    ["ConfigMap", "DaemonSet", "Deployment", "Namespace", "PrometheusRule", "Service", "Service"] + ["NetworkPolicy"] * 5
)
CANARY_POLICIES = {"default-deny", "allow-dns", "egress-internet-only", "probe-egress-to-echo", "echo-ingress-from-probe"}
PROBE_LABELS = {"app.kubernetes.io/name": "netpol-probe"}
ECHO_LABELS = {"app.kubernetes.io/name": "netpol-echo"}
# Chris (2026-11-05): three missed 300 s intervals before it fires.
CANARY_ALERT_FOR = "15m"
CANARY_ALERT_EXPR = (
    'kube_daemonset_status_number_ready{namespace="netpol-canary",daemonset="netpol-probe"}'
    ' < kube_daemonset_status_desired_number_scheduled{namespace="netpol-canary",daemonset="netpol-probe"}'
    ' or absent(kube_daemonset_status_desired_number_scheduled{namespace="netpol-canary",daemonset="netpol-probe"})'
)

# The reusable policy components (design §6.2). Each renders one NetworkPolicy named after its
# directory, with no namespace: the consumer sets it.
COMPONENTS_DIR = "cluster/components/netpol"
COMPONENTS = ["default-deny", "allow-dns", "egress-internet-only"]

# §6.3's three egress rules, copied literally: DNS, the WAN /25 on TCP 443, and the internet less
# ten CIDRs. allow-dns and egress-internet-only together must hold exactly these.
SPEC_6_3_RULES = [
    {
        "to": [
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
            }
        ],
        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
    },
    {
        "to": [{"ipBlock": {"cidr": "152.44.247.0/25"}}],
        "ports": [{"protocol": "TCP", "port": 443}],
    },
    {
        "to": [
            {
                "ipBlock": {
                    "cidr": "0.0.0.0/0",
                    "except": [
                        "0.0.0.0/8",
                        "10.0.0.0/8",
                        "100.64.0.0/10",
                        "127.0.0.0/8",
                        "169.254.0.0/16",
                        "172.16.0.0/12",
                        "192.168.0.0/16",
                        "224.0.0.0/4",
                        "240.0.0.0/4",
                        "152.44.247.0/25",
                    ],
                }
            }
        ],
    },
]
# The form, not the value: Renovate automerges agnhost digests (:automergeDigest).
AGNHOST_RE = r"registry\.k8s\.io/e2e-test-images/agnhost:[0-9][0-9.]*@sha256:[0-9a-f]{64}"
PROBE_ENV = ["PROBE_MODE", "PROBE_INTERVAL", "HOST_IP", "NODE_NAME", "MATRIX"]
# Chris's pick (2026-11-03): one run every 300 s, Ready while /tmp/ready is under 11 minutes old.
PROBE_INTERVAL = "300"
READY_RE = r'test -n "\$\(find /tmp/ready -mmin -(\d+) 2>/dev/null\)"'
# agnhost connect --timeout=3s and dig +time=3: a run takes at most 3 s per row, plus the WAN lookup.
ROW_TIMEOUT_S = 3
DHCP_VARS = "router/ansible/group_vars/vyos_routers.yml"
# bmcctl's node -> BMC map, the record of which BMC belongs to which node.
BMCCTL_NODES = "tools/bmcctl/src/bmcctl/nodes.py"

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


def app_state(app_dir) -> str:
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
    if app_state(ENFORCER_DIR) == "rolled-back":
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


def app_copy(tmp_path: Path, app_dir: str, kustomization: str) -> Path:
    d = tmp_path / Path(app_dir).name
    shutil.copytree(REPO / app_dir, d)
    (d / "kustomization.yaml").write_text(kustomization)
    return d


def enforcer_copy(tmp_path: Path, kustomization: str) -> Path:
    return app_copy(tmp_path, ENFORCER_DIR, kustomization)


# --- always, deployed or rolled back -------------------------------------------


def test_upstream_vendored_byte_identical():
    assert hashlib.sha256(UPSTREAM.read_bytes()).hexdigest() == UPSTREAM_SHA256


def test_enforcer_state():
    """The tree is either deployed or in the marked rollback, never an unmarked empty render."""
    assert app_state(ENFORCER_DIR) in {"deployed", "rolled-back"}


def test_rollback_edit_is_recognised(tmp_path):
    d = enforcer_copy(tmp_path, rolled_back_text(deployed_kustomization()))
    assert app_state(d) == "rolled-back"
    assert render(d) == []


def test_unmarked_empty_render_fails(tmp_path):
    k = deployed_kustomization()
    d = enforcer_copy(tmp_path, rolled_back_text(k).replace(ROLLBACK_MARKER + "\n", ""))
    with pytest.raises(AssertionError, match="no rollback marker"):
        app_state(d)


def test_marker_on_a_rendering_kustomization_fails(tmp_path):
    k = deployed_kustomization()
    d = enforcer_copy(tmp_path, k.replace("\napiVersion:", f"\n{ROLLBACK_MARKER}\napiVersion:", 1))
    with pytest.raises(AssertionError, match="resources must be"):
        app_state(d)


def test_rollback_that_keeps_patches_fails(tmp_path):
    k = rolled_back_text(deployed_kustomization())
    d = enforcer_copy(tmp_path, k + "patches:\n  - path: daemonset-patch.yaml\n")
    with pytest.raises(AssertionError, match="keeps only"):
        app_state(d)


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
    # A later rule matters only if it could match the enforcer image: no package-name filter, or one
    # naming it. Rules pinned to other images (the site's automerge, 2026-11-05) cannot re-enable it.
    later = [
        r for r in rules[idx[0] + 1:]
        if r.get("automerge") is True
        and ("matchPackageNames" not in r or ENFORCER_IMAGE in r["matchPackageNames"])
    ]
    assert not later, "no later packageRule that could match the enforcer may turn automerge back on"

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


# --- the canary (cluster/netpol-canary, plan Tasks 6 and 7) ---------------------


def named(docs: list[dict], kind: str, name: str) -> dict:
    found = [d for d in docs if d.get("kind") == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"expected exactly one {kind}/{name}, got {len(found)}"
    return found[0]


def require_canary_deployed():
    if app_state(CANARY_DIR) == "rolled-back":
        pytest.skip(f"rolled back: {CANARY_DIR} renders nothing ({ROLLBACK_MARKER})")


def canary_rows() -> list[list[str]]:
    """The matrix.txt rows the canary runs (scope all or canary), split into columns."""
    rows = []
    for raw in (REPO / CANARY_DIR / "matrix.txt").read_text().splitlines():
        row = raw.split()
        if row and not row[0].startswith("#") and row[1] in ("all", "canary"):
            rows.append(row)
    return rows


def hardened_container(pod: dict, what: str) -> dict:
    """PodSecurity restricted, plus no token, no service links and no host namespaces.
    Returns the pod's one container."""
    sc = pod["securityContext"]
    assert sc["runAsNonRoot"] is True and sc["runAsUser"] == 1000, what
    assert sc["seccompProfile"] == {"type": "RuntimeDefault"}, what
    assert pod["automountServiceAccountToken"] is False, what
    assert pod["enableServiceLinks"] is False, what
    for key in ("hostNetwork", "hostPID", "hostIPC", "initContainers", "ephemeralContainers"):
        assert not pod.get(key), f"{what}: {key} must not be set"
    assert not [v for v in pod.get("volumes", []) if "hostPath" in v], f"{what}: no hostPath"
    (c,) = pod["containers"]
    assert re.fullmatch(AGNHOST_RE, c["image"]), f"{what}: {c['image']!r} is not a digest-pinned agnhost"
    csc = c["securityContext"]
    assert csc["allowPrivilegeEscalation"] is False, what
    assert csc["readOnlyRootFilesystem"] is True, what
    assert csc["capabilities"] == {"drop": ["ALL"]}, what
    assert "privileged" not in csc, what
    return c


def probe_container(docs: list[dict]) -> dict:
    return named(docs, "DaemonSet", "netpol-probe")["spec"]["template"]["spec"]["containers"][0]


def test_canary_state():
    """Deployed or in the marked rollback, never an unmarked empty render."""
    assert app_state(CANARY_DIR) in {"deployed", "rolled-back"}


def test_canary_rollback_edit_is_recognised(tmp_path):
    text = (REPO / CANARY_DIR / "kustomization.yaml").read_text()
    if has_marker(text):
        pytest.skip("the canary is rolled back already")
    d = app_copy(tmp_path, CANARY_DIR, rolled_back_text(text))
    assert app_state(d) == "rolled-back"
    assert render(d) == []


def test_canary_app():
    """Same in both states: a rollback never edits or deletes the Application."""
    app = yaml.safe_load((REPO / CANARY_APP).read_text())
    assert app["kind"] == "Application"
    assert app["metadata"] == {"name": "netpol-canary", "namespace": "argo-cd"}, "no finalizers"
    assert app["spec"]["source"]["path"] == CANARY_DIR
    assert app["spec"]["destination"] == {"server": "https://kubernetes.default.svc", "namespace": CANARY_NS}
    sync = app["spec"]["syncPolicy"]
    assert sync["automated"] == {"prune": True, "selfHeal": True, "allowEmpty": True}
    assert sync["syncOptions"] == ["CreateNamespace=true", "PrunePropagationPolicy=foreground", "PruneLast=true"]

    root = yaml.safe_load((REPO / "cluster/applications/kustomization.yaml").read_text())
    assert Path(CANARY_APP).name in root["resources"]


def test_canary_kustomization_sets_no_namespace():
    """F5: a kustomization-level namespace would move Task 7's PrometheusRule out of `prometheus`.
    Every object names netpol-canary itself (test_canary_workloads checks the render)."""
    require_canary_deployed()
    k = yaml.safe_load((REPO / CANARY_DIR / "kustomization.yaml").read_text())
    assert "namespace" not in k


def test_canary_workloads():
    require_canary_deployed()
    docs = render(CANARY_DIR)
    assert sorted(d["kind"] for d in docs) == CANARY_KINDS

    ns = one(docs, "Namespace")
    assert ns["metadata"]["name"] == CANARY_NS
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    for d in docs:
        # The PrometheusRule lives in `prometheus` (test_canary_alert).
        if d["kind"] not in ("Namespace", "PrometheusRule"):
            assert d["metadata"].get("namespace") == CANARY_NS, f"{d['kind']}/{d['metadata']['name']}"

    workloads = {"netpol-probe": named(docs, "DaemonSet", "netpol-probe"), "netpol-echo": named(docs, "Deployment", "netpol-echo")}
    for name, w in workloads.items():
        labels = {"app.kubernetes.io/name": name}
        assert labels.items() <= w["metadata"]["labels"].items(), name
        assert w["spec"]["selector"] == {"matchLabels": labels}, name
        assert labels.items() <= w["spec"]["template"]["metadata"]["labels"].items(), name

    # The probe.
    pod = workloads["netpol-probe"]["spec"]["template"]["spec"]
    c = hardened_container(pod, "netpol-probe")
    assert c["command"] == ["bash", "/etc/netpol-probe/probe.sh", "loop"]
    assert "args" not in c
    assert sorted(e["name"] for e in c["env"]) == sorted(PROBE_ENV)
    env = {e["name"]: e for e in c["env"]}
    # B1b (Task 7): the policies are in, so must-drop rows must time out.
    assert env["PROBE_MODE"]["value"] == "enforced"
    assert env["PROBE_INTERVAL"]["value"] == PROBE_INTERVAL
    assert env["HOST_IP"]["valueFrom"] == {"fieldRef": {"fieldPath": "status.hostIP"}}
    assert env["NODE_NAME"]["valueFrom"] == {"fieldRef": {"fieldPath": "spec.nodeName"}}
    assert env["MATRIX"]["value"] == "/etc/netpol-probe/matrix.txt"
    assert c["resources"] == {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"memory": "128Mi"}}
    rp = c["readinessProbe"]
    cmd = rp["exec"]["command"]
    assert len(cmd) == 3 and cmd[:2] == ["sh", "-c"] and re.fullmatch(READY_RE, cmd[2]), cmd
    assert rp["periodSeconds"] == 15
    # A failing canary is the signal, not something to restart.
    assert "livenessProbe" not in c and "startupProbe" not in c

    mounts = {m["mountPath"]: m for m in c["volumeMounts"]}
    vols = {v["name"]: v for v in pod["volumes"]}
    assert set(mounts) == {"/etc/netpol-probe", "/tmp"}
    assert "emptyDir" in vols[mounts["/tmp"]["name"]]
    assert mounts["/etc/netpol-probe"]["readOnly"] is True
    cm_name = vols[mounts["/etc/netpol-probe"]["name"]]["configMap"]["name"]
    assert re.fullmatch(r"netpol-probe-[a-z0-9]+", cm_name), "the generator's hashed name: a matrix change rolls the pods"
    cm = named(docs, "ConfigMap", cm_name)
    assert cm["data"] == {f: (REPO / CANARY_DIR / f).read_text() for f in ("probe.sh", "matrix.txt")}

    # The echo server.
    echo = workloads["netpol-echo"]
    assert echo["spec"]["replicas"] == 1
    e = hardened_container(echo["spec"]["template"]["spec"], "netpol-echo")
    assert e["args"] == ["porter"] and "command" not in e
    assert e["env"] == [{"name": "SERVE_PORT_9376", "value": "open"}, {"name": "SERVE_PORT_9377", "value": "closed"}]
    assert e["readinessProbe"]["tcpSocket"] == {"port": 9376}
    assert "livenessProbe" not in e and "startupProbe" not in e
    assert e["resources"] == {"requests": {"cpu": "5m", "memory": "16Mi"}, "limits": {"memory": "64Mi"}}
    assert sorted(p["containerPort"] for p in e["ports"]) == [9376, 9377]

    sel = {"app.kubernetes.io/name": "netpol-echo"}
    svc = named(docs, "Service", "netpol-echo")
    assert svc["spec"].get("type", "ClusterIP") == "ClusterIP" and svc["spec"].get("clusterIP") != "None"
    assert svc["spec"]["selector"] == sel
    assert sorted((p["port"], p.get("protocol", "TCP")) for p in svc["spec"]["ports"]) == [(9376, "TCP"), (9377, "TCP")]
    # Headless: its name resolves to the echo pod's own IP, for the matrix row that dials the pod directly.
    pod_svc = named(docs, "Service", "netpol-echo-pod")
    assert pod_svc["spec"]["clusterIP"] == "None"
    assert pod_svc["spec"]["selector"] == sel


def test_canary_matrix_targets_exist():
    """Rows naming a netpol-canary Service name one this kustomization renders. The row through
    the headless Service dials the echo pod's IP directly (no DNAT) on a port the echo neither
    serves nor names, so the pod refuses it at baseline. echo-ingress-from-probe accepts TCP 9378
    from the probe (test_canary_policies), so the row isolates the probe's egress."""
    require_canary_deployed()
    docs = render(CANARY_DIR)
    services = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Service"}
    echo = named(docs, "Deployment", "netpol-echo")["spec"]["template"]["spec"]["containers"][0]
    served = {p["containerPort"] for p in echo["ports"]}
    rows = [r for r in canary_rows() if r[2].endswith(f".{CANARY_NS}.svc.cluster.local")]
    assert rows, "the matrix has no echo rows"
    headless = 0
    for expect, scope, host, port, backend, *_ in rows:
        svc = host.split(".")[0]
        assert svc in services, f"{host} is not a Service in {CANARY_DIR}"
        assert scope == "canary", f"{host}: only the canary can reach the echo server"
        if services[svc]["spec"].get("clusterIP") == "None":
            headless += 1
            assert expect == "dropped" and int(port) not in served, f"{host}:{port}"
            assert backend == f"{CANARY_NS}/{svc}"
    assert headless == 1, "exactly one row dials the echo pod's IP"


def test_probe_readiness_window_covers_interval():
    """F9: Ready must outlast one interval plus one full run, and at least two intervals.

    The probe touches /tmp/ready after each passing run and starts runs max(5, interval - run)
    apart, so two healthy touches are at most interval + run apart. A run is bounded by every row
    and the WAN lookup at their 3 s timeouts. Preflight F9 proposed 3 intervals, calibrated on the
    60 s option; Chris's 300 s pick came with an 11-minute window (2.2 intervals), so 2 is the floor.
    """
    require_canary_deployed()
    c = probe_container(render(CANARY_DIR))
    interval = int({e["name"]: e for e in c["env"]}["PROBE_INTERVAL"]["value"])
    window = int(re.fullmatch(READY_RE, c["readinessProbe"]["exec"]["command"][2]).group(1)) * 60
    run = ROW_TIMEOUT_S * (len(canary_rows()) + 1)
    assert window >= 2 * interval, f"readiness window {window}s < 2 x {interval}s"
    assert window > interval + run, f"readiness window {window}s <= interval {interval}s + run {run}s"


def test_probe_node_ips_match_dhcp_reservations():
    """probe.sh's default NODE_IPS ($PEER_IP rows) is top, middle, bottom, as the router reserves them."""
    defaults = re.findall(r"NODE_IPS:-([0-9. ]+)\}", (REPO / CANARY_DIR / "probe.sh").read_text())
    assert len(defaults) == 1, "probe.sh must default NODE_IPS in exactly one place"
    dhcp = yaml.safe_load((REPO / DHCP_VARS).read_text())["dhcp"]["static_mappings"]
    ips = {m["name"]: m["ip"] for m in dhcp}
    assert defaults[0].split() == [ips["top"], ips["middle"], ips["bottom"]]


def bmcctl_nodes() -> dict[str, tuple[str, str]]:
    """name -> (node IP, BMC IP) from bmcctl's NODES, read with ast (this imports and runs nothing)."""
    tree = ast.parse((REPO / BMCCTL_NODES).read_text())
    nodes = {}
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "Node":
            name, ip, bmc = (ast.literal_eval(a) for a in call.args)
            nodes[name] = (ip, bmc)
    assert nodes, f"no Node(...) entries in {BMCCTL_NODES}"
    return nodes


def test_probe_own_bmcs_match_bmcctl():
    """probe.sh skips the probe node's own BMC: middle cannot reach its own (its NC-SI port drops
    unicast from the host, 2026-10-30), so that row would time out at baseline and then pass as
    "dropped" without proving anything. The NODE_BMCS default must be bmcctl's map, and its BMCs the matrix's BMC
    rows, so each BMC is still dialled from the other two nodes."""
    defaults = re.findall(r"NODE_BMCS:-([a-z0-9.= ]+)\}", (REPO / CANARY_DIR / "probe.sh").read_text())
    assert len(defaults) == 1, "probe.sh must default NODE_BMCS in exactly one place"
    own = dict(pair.split("=", 1) for pair in defaults[0].split())
    nodes = bmcctl_nodes()
    assert own == {name: bmc for name, (_, bmc) in nodes.items()}

    bmc_rows = {r[2] for r in canary_rows() if "BMC" in r[5:]}
    assert bmc_rows == set(own.values()), f"matrix BMC rows {sorted(bmc_rows)} != bmcctl BMCs {sorted(own.values())}"
    # The NODE_IPS default (the $PEER_IP ring) is bmcctl's node IPs in the same order.
    ring = re.findall(r"NODE_IPS:-([0-9. ]+)\}", (REPO / CANARY_DIR / "probe.sh").read_text())[0].split()
    assert ring == [nodes[n][0] for n in ("top", "middle", "bottom")]


# --- policy components and the canary's policies (plan Task 7) ------------------


def policies(docs: list[dict]) -> dict[str, dict]:
    nps = [d for d in docs if d.get("kind") == "NetworkPolicy"]
    by_name = {d["metadata"]["name"]: d for d in nps}
    assert len(by_name) == len(nps), "two NetworkPolicies share a name"
    return by_name


def test_components_match_spec():
    """Each component renders one NetworkPolicy named after its directory, selecting every pod,
    with no namespace (the consumer sets it). Together they hold exactly §6.3's egress rules."""
    rules = []
    for name in COMPONENTS:
        d = f"{COMPONENTS_DIR}/{name}"
        k = yaml.safe_load((REPO / d / "kustomization.yaml").read_text())
        assert k == {
            "apiVersion": "kustomize.config.k8s.io/v1alpha1",
            "kind": "Component",
            "resources": ["networkpolicy.yaml"],
        }, d
        text = (REPO / d / "networkpolicy.yaml").read_text()
        head = text.split("\napiVersion:", 1)[0]
        assert re.search(r"^# \S", head, re.M), f"{d}: say what the pod needs in a comment above the policy (§6.2)"

        (np,) = [x for x in render(d) if x["kind"] == "NetworkPolicy"]
        assert len(render(d)) == 1, f"{d} renders only its NetworkPolicy"
        assert np["metadata"] == {"name": name}, f"{d}: no namespace, labels or annotations"
        spec = np["spec"]
        assert spec["podSelector"] == {}, d
        assert "ingress" not in spec, f"{d}: these components only restrict or allow egress"
        if name == "default-deny":
            assert spec == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
        else:
            assert spec["policyTypes"] == ["Egress"], d
            rules += spec["egress"]
    assert policies_egress(render(f"{COMPONENTS_DIR}/allow-dns")) == SPEC_6_3_RULES[:1]
    assert policies_egress(render(f"{COMPONENTS_DIR}/egress-internet-only")) == SPEC_6_3_RULES[1:]
    assert rules == SPEC_6_3_RULES


def policies_egress(docs: list[dict]) -> list[dict]:
    return [r for d in docs if d["kind"] == "NetworkPolicy" for r in d["spec"].get("egress", [])]


def test_canary_policies():
    """The canary consumes the three components, plus its own two policies. probe-egress-to-echo
    allows 9376 and 9377, so the 9377 drop can only be the echo's ingress default-deny (§9.2).
    echo-ingress-from-probe allows 9376 and the unserved 9378 (option a, Task 6 review), so only
    the probe's egress can drop the direct pod-IP row."""
    require_canary_deployed()
    k = yaml.safe_load((REPO / CANARY_DIR / "kustomization.yaml").read_text())
    assert k["components"] == [f"../components/netpol/{c}" for c in COMPONENTS]
    keys = list(k)
    # F5 and the rollback edit: rolled_back_text drops everything after the resources list.
    assert keys.index("resources") < keys.index("components") and keys.index("resources") < keys.index("patches")

    docs = render(CANARY_DIR)
    nps = policies(docs)
    assert set(nps) == CANARY_POLICIES
    for name, np in nps.items():
        assert np["metadata"]["namespace"] == CANARY_NS, name
    for c in COMPONENTS:
        (want,) = render(f"{COMPONENTS_DIR}/{c}")
        assert nps[c]["spec"] == want["spec"], f"{c}: the canary must use the component unchanged"

    probe_tpl = named(docs, "DaemonSet", "netpol-probe")["spec"]["template"]["metadata"]["labels"]
    echo_tpl = named(docs, "Deployment", "netpol-echo")["spec"]["template"]["metadata"]["labels"]
    assert PROBE_LABELS.items() <= probe_tpl.items() and ECHO_LABELS.items() <= echo_tpl.items()

    out = nps["probe-egress-to-echo"]["spec"]
    assert out["podSelector"] == {"matchLabels": PROBE_LABELS}
    assert out["policyTypes"] == ["Egress"]
    (rule,) = out["egress"]
    assert rule["to"] == [{"podSelector": {"matchLabels": ECHO_LABELS}}]
    assert [(p["protocol"], p["port"]) for p in rule["ports"]] == [("TCP", 9376), ("TCP", 9377)]

    inn = nps["echo-ingress-from-probe"]["spec"]
    assert inn["podSelector"] == {"matchLabels": ECHO_LABELS}
    assert inn["policyTypes"] == ["Ingress"]
    (rule,) = inn["ingress"]
    assert rule["from"] == [{"podSelector": {"matchLabels": PROBE_LABELS}}]
    assert [(p["protocol"], p["port"]) for p in rule["ports"]] == [("TCP", 9376), ("TCP", 9378)]

    # The echo serves 9377, which the policy keeps out; the policy lets 9378 in, which nothing serves.
    echo = named(docs, "Deployment", "netpol-echo")["spec"]["template"]["spec"]["containers"][0]
    served = {p["containerPort"] for p in echo["ports"]}
    assert 9377 in served and 9378 not in served


def test_canary_alert():
    """NetpolCanaryFailing: a PrometheusRule (ConfigMap rules never load) in `prometheus`, labelled
    `release: prometheus` or the ruleSelector never picks it up."""
    require_canary_deployed()
    pr = named(render(CANARY_DIR), "PrometheusRule", "netpol-canary")
    assert pr["metadata"]["namespace"] == "prometheus"
    assert pr["metadata"]["labels"]["release"] == "prometheus"
    (group,) = pr["spec"]["groups"]
    assert group["name"] == "netpol-canary"
    (rule,) = group["rules"]
    assert rule["alert"] == "NetpolCanaryFailing"
    assert rule["expr"] == CANARY_ALERT_EXPR
    assert rule["for"] == CANARY_ALERT_FOR
    assert rule["labels"] == {"severity": "warning"}
    assert rule["annotations"]["summary"] and rule["annotations"]["description"]


# Namespaces where no NetworkPolicy may select pods (Global Constraints; design §6.2).
# dns and rabbitmq-system leave this set only through plan Task 14.
FORBIDDEN = {
    "kube-system",
    "rook",
    "metallb-system",
    "prometheus",
    "argo-cd",
    "telephony",
    "home-assistant",
    "dns",
    "rabbitmq-system",
}


def policy_violations(docs: list[dict]) -> list[str]:
    """One message per NetworkPolicy with no namespace (it would land wherever Argo's destination
    points) or in a FORBIDDEN namespace."""
    out = []
    for d in docs:
        if d.get("kind") != "NetworkPolicy":
            continue
        meta = d.get("metadata") or {}
        name, ns = meta.get("name", "?"), meta.get("namespace")
        if not ns:
            out.append(f"NetworkPolicy {name} has no namespace; set it in the consumer (F5)")
        elif ns in FORBIDDEN:
            out.append(f"NetworkPolicy {ns}/{name}: no policy may select pods in {ns}")
    return out


def test_forbidden_guard_detects():
    docs = [
        {"kind": "NetworkPolicy", "metadata": {"name": "bad", "namespace": "rook"}},
        {"kind": "NetworkPolicy", "metadata": {"name": "nowhere"}},
        {"kind": "NetworkPolicy", "metadata": {"name": "fine", "namespace": CANARY_NS}},
        {"kind": "ConfigMap", "metadata": {"name": "not-a-policy", "namespace": "rook"}},
    ]
    found = policy_violations(docs)
    assert len(found) == 2, found
    assert any("rook/bad" in v for v in found) and any("nowhere" in v for v in found)


def policy_dirs() -> list[str]:
    """Every kustomization directory under cluster/, except the components themselves, holding a
    non-secret YAML file that mentions a NetworkPolicy or a netpol component."""
    dirs = []
    for k in sorted((REPO / "cluster").rglob("kustomization.yaml")):
        d = k.parent
        rel = d.relative_to(REPO).as_posix()
        if rel == COMPONENTS_DIR.rsplit("/", 1)[0] or rel.startswith(COMPONENTS_DIR.rsplit("/", 1)[0] + "/"):
            continue
        for f in sorted(d.rglob("*")):
            if not f.is_file() or f.suffix not in (".yaml", ".yml") or ".secret." in f.name:
                continue
            text = f.read_text(errors="replace")
            if "kind: NetworkPolicy" in text or "components/netpol" in text:
                dirs.append(rel)
                break
    return dirs


def test_no_policy_in_forbidden_namespaces():
    dirs = policy_dirs()
    assert CANARY_DIR in dirs, "the canary renders policies, so the scan must find it"
    found = [v for d in dirs for v in policy_violations(render(d))]
    assert found == []
