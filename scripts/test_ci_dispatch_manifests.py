"""Static tests for ci-dispatch and the `tiny` runner class (cluster/ci-dispatch).

    python -m pytest -q -p no:cacheprovider scripts/test_ci_dispatch_manifests.py

Runs as the `ci-dispatch-manifests` pre-commit hook. Everything is rendered with
`kubectl kustomize`, the same build Argo runs, and checked against the values the
runner classes plan fixes (docs/superpowers/plans/2026-11-04-runner-classes-phase-0-1.md:
Global Constraints, D5, D7, D16, Task 17).

These tests never read `*.secret.*` files: they render the SealedSecrets, and open
only the paths named here.

Deployed
--------
DEPLOYED is True: Task 18 listed the Application in
cluster/applications/kustomization.yaml, and test_app_registered requires the
listing. The image is v0.1.0 (Task 14); test_dispatcher_image_pinned holds the
exact pin.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

DEPLOYED = True

APP_FILE = "cluster/applications/ci-dispatch.yaml"
APPS_KUSTOMIZATION = "cluster/applications/kustomization.yaml"
ROOT = "cluster/ci-dispatch"
DISPATCHER = f"{ROOT}/dispatcher"
TINY = f"{ROOT}/tiny"
EGRESS_WEB = "cluster/components/netpol/egress-internet-web"
EGRESS_ONLY = "cluster/components/netpol/egress-internet-only"
GENERAL_RUNNER = REPO / "cluster/forgejo-runner-general/deployment.yaml"
FORGE_BOT_KEYRING = REPO / "cluster/forge-bot/keyring"
RENOVATE = REPO / ".renovaterc.json"
IMAGE = "git.realliance.net/realliance-ops/ci-dispatch"
IMAGE_RE = r"^git\.realliance\.net/realliance-ops/ci-dispatch:sha-[0-9a-f]{7}@sha256:[0-9a-f]{64}$"
# Tag v0.2.0 (signed) = main 19b416f, built by ci-dispatch's CI; Chris approved it for
# deploy (2026-11-06; v0.1.0 was Task 14 Step 4). A new pin is a reviewed commit that changes this line too.
RELEASE_IMAGE = (
    "git.realliance.net/realliance-ops/ci-dispatch:sha-19b416f"
    "@sha256:cbd4c59a5150ba18a7a5ac56cfc798957976fd735682ee7c25ef8aeda2eb2d3b"
)
# ci-dispatch v0.1.0's shutdown (internal/loop, cmd/ci-dispatch): the step in flight, then the servers.
SHUTDOWN_GRACE_SECONDS = 20
SERVER_SHUTDOWN_SECONDS = 5
PSA = "pod-security.kubernetes.io/enforce"
KEEP = "Prune=false,Delete=false"

# The ten not-internet ranges of the NetworkPolicy spec (section 6.3), the WAN /25 last.
NOT_INTERNET = [
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
]
NODES = ["192.168.0.240/32", "192.168.0.241/32", "192.168.0.242/32"]
ALERTS = {
    "CIDispatchStale",
    "CIDispatchLaunchFailing",
    "CIDispatchConfigUntrusted",
    "CIDispatchStoredConfigRejected",
    "CIDispatchConfigFindings",
    "CIDispatchLaunchesUndone",
    "CIDispatchOrphanCompute",
    "CITinyQuotaFull",
}
# internal/metrics FindingKinds of v0.1.0, less stored-config-rejected (its own counter alerts).
FINDING_KINDS = {"owner-name-stale", "owner-name-missing", "owner-names-unchecked"}
# One Forgejo-held key's whole back-off schedule (3 min pickup, then 1/2/5/10/10 min) fits six
# undos in its first hour, then one an hour (the 60 min wait after the sixth). increase() reads
# six as about 6.05 at the window's edges, so the threshold is 7, with room to spare.
UNDONE_PER_HOUR = 7


def render(path: str) -> list[dict]:
    out = subprocess.run(
        ["kubectl", "kustomize", str(REPO / path)], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, f"kubectl kustomize {path} failed:\n{out.stderr}"
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def load(path: str) -> dict:
    return yaml.safe_load((REPO / path).read_text())


def of(docs: list[dict], kind: str, name: str | None = None) -> list[dict]:
    return [
        d
        for d in docs
        if d.get("kind") == kind and (name is None or d["metadata"]["name"] == name)
    ]


def one(docs: list[dict], kind: str, name: str | None = None) -> dict:
    found = of(docs, kind, name)
    assert len(found) == 1, f"expected exactly one {kind} {name or ''}, got {len(found)}"
    return found[0]


@pytest.fixture(scope="module")
def dispatcher() -> list[dict]:
    return render(DISPATCHER)


@pytest.fixture(scope="module")
def tiny() -> list[dict]:
    return render(TINY)


@pytest.fixture(scope="module")
def whole() -> list[dict]:
    return render(ROOT)


def dispatcher_config(docs: list[dict]) -> dict:
    cm = next(d for d in of(docs, "ConfigMap") if d["metadata"]["name"].startswith("ci-dispatch-config-"))
    return yaml.safe_load(cm["data"]["config.yaml"])


def runner_config(docs: list[dict]) -> dict:
    cm = one(docs, "ConfigMap", "ci-tiny-runner")
    return yaml.safe_load(cm["data"]["config.yml"])


def egress_rules(policy: dict) -> list[dict]:
    return policy["spec"]["egress"]


def ports(rule: dict) -> set[tuple[str, int]]:
    return {(p["protocol"], p["port"]) for p in rule["ports"]}


def internet_rule(policy: dict) -> dict:
    rules = [r for r in egress_rules(policy) if r["to"][0].get("ipBlock", {}).get("cidr") == "0.0.0.0/0"]
    assert len(rules) == 1
    return rules[0]


def wan_rule(policy: dict) -> dict:
    rules = [
        r for r in egress_rules(policy) if r["to"][0].get("ipBlock", {}).get("cidr") == "152.44.247.0/25"
    ]
    assert len(rules) == 1
    return rules[0]


def test_app_registered():
    app = load(APP_FILE)
    assert app["kind"] == "Application" and app["metadata"]["name"] == "ci-dispatch"
    assert app["metadata"]["namespace"] == "argo-cd"
    assert "finalizers" not in app["metadata"], "no resources-finalizer: deleting the Application must orphan, not prune"
    spec = app["spec"]
    assert spec["source"]["path"] == "cluster/ci-dispatch"
    assert spec["source"]["repoURL"] == "https://github.com/ChristopherJMiller/luma-homeops.git"
    assert spec["syncPolicy"]["automated"] == {"prune": True, "selfHeal": True, "allowEmpty": True}
    opts = spec["syncPolicy"]["syncOptions"]
    assert "CreateNamespace=false" in opts, "the namespaces are manifests"
    assert not any(o.startswith("CreateNamespace=true") for o in opts)
    text = (REPO / APP_FILE).read_text()
    assert "roll back by rendering nothing, never by deleting this file" in text
    listed = "  - ci-dispatch.yaml" in (REPO / APPS_KUSTOMIZATION).read_text().splitlines()
    assert listed == DEPLOYED, (
        "the Application is listed iff DEPLOYED: Task 17 does not deploy, Task 18 lists it and flips DEPLOYED"
    )


def test_renders_with_namespaces_set(whole, dispatcher, tiny):
    """The `kubectl kustomize` render: both trees build, and every namespaced object lands where it belongs."""
    for docs, ns in ((dispatcher, "ci-dispatch"), (tiny, "ci-tiny")):
        assert docs, "renders something"
        for d in docs:
            if d["kind"] != "Namespace":
                got = d["metadata"].get("namespace")
                assert got == ns, f"{d['kind']}/{d['metadata']['name']} is in {got}"
    assert len(whole) == len(dispatcher) + len(tiny)


def test_namespaces_restricted(whole):
    """The kube guard leaves pod privilege fields (hostNetwork, privileged, capabilities) to Pod
    Security on ci-tiny (Task 9 ruling), so ci-tiny must never lose `restricted`."""
    names = {}
    for ns in of(whole, "Namespace"):
        names[ns["metadata"]["name"]] = ns
    assert set(names) == {"ci-dispatch", "ci-tiny"}
    for name, ns in names.items():
        labels = ns["metadata"]["labels"]
        assert labels[PSA] == "restricted", f"{name} must enforce restricted"
        assert labels[PSA.replace("enforce", "warn")] == "restricted"
        assert labels[PSA.replace("enforce", "audit")] == "restricted"
        assert PSA + "-version" not in labels, "an enforce-version pin would freeze the policy at an old version"


def test_dispatcher_pod(dispatcher):
    sts = one(dispatcher, "StatefulSet", "ci-dispatch")
    assert sts["spec"]["replicas"] == 1
    assert sts["spec"]["serviceName"] == "ci-dispatch"
    spec = sts["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "ci-dispatch"
    assert spec["automountServiceAccountToken"] is False
    assert spec["enableServiceLinks"] is False
    assert spec["terminationGracePeriodSeconds"] == 30
    sc = spec["securityContext"]
    assert sc["runAsNonRoot"] is True
    assert sc["runAsUser"] == 65532 and sc["runAsGroup"] == 65532 and sc["fsGroup"] == 65532
    assert sc["seccompProfile"] == {"type": "RuntimeDefault"}
    (c,) = spec["containers"]
    assert c["image"].startswith(f"{IMAGE}:sha-")
    csc = c["securityContext"]
    assert csc["allowPrivilegeEscalation"] is False
    assert csc["readOnlyRootFilesystem"] is True
    assert csc["capabilities"] == {"drop": ["ALL"]}
    assert c["resources"] == {"requests": {"cpu": "10m", "memory": "48Mi"}, "limits": {"memory": "256Mi"}}
    env = {e["name"]: e["value"] for e in c["env"]}
    assert env["GOMEMLIMIT"] == "192MiB"
    assert env["TMPDIR"] == "/tmp"
    assert c["args"] == ["--config=/etc/ci-dispatch/config/config.yaml"]
    assert {p["name"]: p["containerPort"] for p in c["ports"]} == {"http": 8080, "usage": 8081}
    for probe in ("startupProbe", "livenessProbe"):
        assert c[probe]["httpGet"] == {"path": "/healthz", "port": "http"}, probe
    # /healthz reads 200 while Start is in flight, so the startup probe passes once main listens
    # and covers only the time before that; liveness (3 misses, 30 s apart) catches a wedged Start,
    # and Healthy's in-flight limit scales with start_budget by itself. Exact pins, no relation.
    timing = ("periodSeconds", "timeoutSeconds", "failureThreshold")
    assert {k: c["startupProbe"][k] for k in timing} == {"periodSeconds": 10, "timeoutSeconds": 5, "failureThreshold": 30}
    assert {k: c["livenessProbe"][k] for k in timing} == {"periodSeconds": 30, "timeoutSeconds": 5, "failureThreshold": 3}
    # SIGTERM: loop.ShutdownGrace (20 s), then the servers' 5 s shutdown, inside the grace period.
    assert spec["terminationGracePeriodSeconds"] >= SHUTDOWN_GRACE_SECONDS + SERVER_SHUTDOWN_SECONDS
    assert "readinessProbe" not in c, "nothing routes to it but the scrape; a not-ready pod would hide it from Prometheus"

    vols = {v["name"]: v for v in spec["volumes"]}
    mounts = {m["name"]: m for m in c["volumeMounts"]}
    assert set(vols) == set(mounts), "every volume is mounted, every mount has a volume"
    assert vols["ledger"]["persistentVolumeClaim"]["claimName"] == "ci-dispatch-ledger"
    assert mounts["ledger"]["mountPath"] == "/data"
    assert vols["tmp"]["emptyDir"] == {"sizeLimit": "64Mi"}
    assert mounts["tmp"]["mountPath"] == "/tmp"
    for name in ("config", "keyring", "secrets", "kube"):
        assert mounts[name]["readOnly"] is True, name

    # The API token: automount is off, so a projected one with an hour's life, plus the CA bundle.
    sources = vols["kube"]["projected"]["sources"]
    tokens = [s["serviceAccountToken"] for s in sources if "serviceAccountToken" in s]
    assert len(tokens) == 1 and tokens[0]["expirationSeconds"] == 3600 and tokens[0]["path"] == "token"
    cas = [s["configMap"] for s in sources if "configMap" in s]
    assert len(cas) == 1 and cas[0]["name"] == "kube-root-ca.crt"
    assert cas[0]["items"] == [{"key": "ca.crt", "path": "ca.crt"}]

    # Both secrets as 0400 files.
    proj = vols["secrets"]["projected"]
    assert proj["defaultMode"] == 0o400
    secrets = {s["secret"]["name"]: [(i["key"], i["path"]) for i in s["secret"]["items"]] for s in proj["sources"]}
    assert secrets == {
        "ci-dispatch-forgejo": [("forgejo-token", "forgejo-token")],
        "ci-dispatch-usage": [("usage-token", "usage-token")],
    }


def test_dispatcher_image_pinned(dispatcher):
    sts = one(dispatcher, "StatefulSet", "ci-dispatch")
    image = sts["spec"]["template"]["spec"]["containers"][0]["image"]
    assert re.match(IMAGE_RE, image), f"image is not a sha-<7>@sha256:<64> pin: {image}"
    assert image == RELEASE_IMAGE


def test_pvc_kept(dispatcher):
    pvc = one(dispatcher, "PersistentVolumeClaim", "ci-dispatch-ledger")
    assert pvc["spec"]["storageClassName"] == "rook-ceph-block-ssd"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "1Gi"
    assert pvc["metadata"]["annotations"]["argocd.argoproj.io/sync-options"] == KEEP
    ns = one(dispatcher, "Namespace", "ci-dispatch")
    assert ns["metadata"]["annotations"]["argocd.argoproj.io/sync-options"] == KEEP, (
        "pruning a Namespace deletes its PVC whatever the PVC says (D5)"
    )


def test_rbac_exact(whole):
    assert of(whole, "ClusterRole") == [] and of(whole, "ClusterRoleBinding") == []
    role = one(whole, "Role")
    assert role["metadata"]["namespace"] == "ci-tiny"
    rules = {(tuple(r["apiGroups"]), tuple(r["resources"])): sorted(r["verbs"]) for r in role["rules"]}
    assert rules == {
        (("batch",), ("jobs",)): sorted(["create", "get", "list", "patch", "delete"]),
        (("",), ("secrets",)): sorted(["create", "delete"]),
        (("",), ("pods",)): sorted(["get", "list"]),
    }
    assert len(role["rules"]) == 3
    rb = one(whole, "RoleBinding")
    assert rb["metadata"]["namespace"] == "ci-tiny"
    assert rb["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": role["metadata"]["name"]}
    assert rb["subjects"] == [{"kind": "ServiceAccount", "name": "ci-dispatch", "namespace": "ci-dispatch"}]
    sa = one(whole, "ServiceAccount", "ci-dispatch")
    assert sa["metadata"]["namespace"] == "ci-dispatch"
    assert sa["automountServiceAccountToken"] is False


def test_quota_exact(tiny):
    quota = one(tiny, "ResourceQuota")
    assert quota["metadata"]["namespace"] == "ci-tiny"
    assert quota["spec"]["hard"] == {
        "pods": "3",
        "requests.cpu": "750m",
        "requests.memory": "1536Mi",
        "limits.memory": "6Gi",
    }


def test_tiny_runner_config(tiny, dispatcher):
    cfg = runner_config(tiny)
    assert cfg["runner"]["capacity"] == 1
    assert cfg["runner"]["timeout"] == "15m"
    assert cfg["cache"]["enabled"] is False
    assert cfg["host"]["workdir_parent"] == "/work/act"
    assert "server" not in cfg or "connections" not in cfg["server"]
    # The dispatcher names this ConfigMap, so it must keep a stable name (no hash suffix).
    assert dispatcher_config(dispatcher)["tiny"]["runner_config_map"] == "ci-tiny-runner"
    # Compare as durations, not strings: 15m == 900s.
    assert _seconds(cfg["runner"]["timeout"]) == _seconds(dispatcher_config(dispatcher)["tiny"]["runner_timeout"])


def _seconds(s: str) -> int:
    total = 0
    for n, unit in re.findall(r"(\d+)([hms])", s):
        total += int(n) * {"h": 3600, "m": 60, "s": 1}[unit]
    assert total > 0, s
    return total


def test_config_values(dispatcher):
    cfg = dispatcher_config(dispatcher)
    assert cfg["classes"] == {"tiny": "on", "small": "off", "medium": "off", "large": "off"}
    assert cfg["forgejo"]["url"] == "http://forgejo-http.forgejo.svc.cluster.local:3000"
    assert cfg["forgejo"]["public_url"] == "https://git.realliance.net"
    assert cfg["inventory"]["repo"] == "realliance-ops/forge"
    if GENERAL_RUNNER.exists():
        dep = yaml.safe_load(GENERAL_RUNNER.read_text())
        runner = dep["spec"]["template"]["spec"]["containers"][0]["image"]
        assert cfg["tiny"]["image"] == runner, "tiny runs the general runner's image, consumed unchanged"
    else:
        assert re.match(r"^ghcr\.io/christopherjmiller/forgejo-runner-general:sha-[0-9a-f]{7}@sha256:[0-9a-f]{64}$", cfg["tiny"]["image"])
    assert cfg["kube"]["namespace"] == "ci-tiny"
    assert _seconds(cfg["tiny"]["hard_stop"]) == 1200, "the spec's 20 min; config.Load caps it at 1h"
    assert _seconds(cfg["tiny"]["hard_stop"]) > _seconds(cfg["tiny"]["runner_timeout"])
    # Every path the config names is a path the pod mounts.
    sts = one(dispatcher, "StatefulSet", "ci-dispatch")
    mounts = [m["mountPath"] for m in sts["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]]
    for key in (
        cfg["forgejo"]["token_file"],
        cfg["usage_token_file"],
        cfg["inventory"]["keyring"],
        cfg["inventory"]["fingerprints"],
        cfg["kube"]["token_file"],
        cfg["kube"]["ca_file"],
        cfg["ledger"],
        cfg["snapshots"],
    ):
        assert any(key == m or key.startswith(m + "/") for m in mounts), f"{key} is not under any mount"
    assert cfg["ledger"].startswith("/data/") and cfg["snapshots"].startswith("/data/")


def test_keyring_matches_forge_bot(dispatcher):
    """F3: the trust root is forge-bot's, byte for byte."""
    if not FORGE_BOT_KEYRING.is_dir():
        pytest.skip("cluster/forge-bot/keyring does not exist on this branch yet (forge-bot deploys later)")
    for name in ("owners.asc", "fingerprints.txt"):
        mine = (REPO / DISPATCHER / "keyring" / name).read_bytes()
        assert mine == (FORGE_BOT_KEYRING / name).read_bytes(), f"{name} differs from forge-bot's"
    cm = next(d for d in of(dispatcher, "ConfigMap") if d["metadata"]["name"].startswith("ci-dispatch-keyring-"))
    assert set(cm["data"]) == {"owners.asc", "fingerprints.txt"}


def canon(rules: list[dict], key: str) -> list[str]:
    """Each rule as sorted JSON, so the union compares as an exact multiset with no tolerance."""
    out = []
    for r in rules:
        out.append(json.dumps(r, sort_keys=True))
    return sorted(out)


def union(docs: list[dict], namespace: str, direction: str, pod_labels: dict | None = None) -> list[str]:
    """Every rule of every NetworkPolicy in `namespace` whose podSelector selects a pod with `pod_labels`
    (None: the policy must select all pods), for `direction` ('ingress' or 'egress')."""
    rules = []
    for p in of(docs, "NetworkPolicy"):
        if p["metadata"]["namespace"] != namespace:
            continue
        sel = p["spec"]["podSelector"].get("matchLabels", {})
        if not all((pod_labels or {}).get(k) == v for k, v in sel.items()):
            continue
        rules += p["spec"].get(direction, [])
    return canon(rules, direction)


TINY_EGRESS = [
    {  # DNS: CoreDNS pods only, port 53 UDP and TCP only
        "to": [
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
            }
        ],
        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
    },
    {  # the forge through the WAN /25 reflection: 443 only
        "to": [{"ipBlock": {"cidr": "152.44.247.0/25"}}],
        "ports": [{"protocol": "TCP", "port": 443}],
    },
    {  # the public internet: the not-internet ranges excepted, web ports only
        "to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": NOT_INTERNET}}],
        "ports": [{"protocol": "TCP", "port": 80}, {"protocol": "TCP", "port": 443}],
    },
]

DISPATCHER_EGRESS = [
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
        "to": [
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "forgejo"}},
                "podSelector": {
                    "matchLabels": {"app.kubernetes.io/name": "forgejo", "app.kubernetes.io/instance": "forgejo"}
                },
            }
        ],
        "ports": [{"protocol": "TCP", "port": 3000}],
    },
    {
        "to": [{"ipBlock": {"cidr": c}} for c in NODES],
        "ports": [{"protocol": "TCP", "port": 6443}],
    },
]
DISPATCHER_INGRESS = [
    {
        "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "prometheus"}}}],
        "ports": [{"protocol": "TCP", "port": 8080}],
    }
]


def test_tiny_egress_union_exact(tiny):
    """I1: the union of every rule that applies to a ci-tiny pod, peers and ports and excepts, exact.
    A widening of any component (allow-dns to every pod, a dropped port list, one more port, a dropped
    except) fails here, whoever owns the component's own test."""
    assert union(tiny, "ci-tiny", "egress") == canon(TINY_EGRESS, "egress")
    assert union(tiny, "ci-tiny", "ingress") == []
    for p in of(tiny, "NetworkPolicy"):
        if p["metadata"]["name"] == "allow-dns":
            assert p["spec"]["policyTypes"] == ["Egress"]


def test_dispatcher_union_exact(dispatcher):
    labels = {"app.kubernetes.io/name": "ci-dispatch"}
    assert union(dispatcher, "ci-dispatch", "egress", labels) == canon(DISPATCHER_EGRESS, "egress")
    assert union(dispatcher, "ci-dispatch", "ingress", labels) == canon(DISPATCHER_INGRESS, "ingress")
    for p in of(dispatcher, "NetworkPolicy"):
        if p["metadata"]["name"] == "allow-dns":
            assert p["spec"]["policyTypes"] == ["Egress"]


def test_tiny_pod_cannot_raise_its_own_limits(tiny, whole):
    """I2 (static part): the pod template is built by ci-dispatch's Go builder, so what this repo can
    pin is the surroundings. Pod Security restricted refuses privileged, capabilities and host
    namespaces (test_namespaces_restricted); the runner config offers no container or option
    passthrough; the only RBAC in ci-tiny is the dispatcher's; no ServiceAccount token reaches a pod;
    a LimitRange caps each container at the shape's own limits. The per-pod PID limit is the
    kubelet's (a node-level gate, Task 19)."""
    cfg = runner_config(tiny)
    assert set(cfg) == {"log", "runner", "cache", "host"}
    assert set(cfg["host"]) == {"workdir_parent"}
    assert set(cfg["runner"]) == {"capacity", "timeout"}
    assert "container" not in cfg and "envs" not in cfg["runner"] and "env_file" not in cfg["runner"]
    for rb in of(whole, "RoleBinding"):
        assert rb["subjects"] == [{"kind": "ServiceAccount", "name": "ci-dispatch", "namespace": "ci-dispatch"}]
    assert of(whole, "ClusterRoleBinding") == []
    assert one(tiny, "ServiceAccount", "default")["automountServiceAccountToken"] is False
    lr = one(tiny, "LimitRange")
    assert lr["spec"]["limits"] == [{"type": "Container", "max": {"cpu": "1", "memory": "2Gi"}}]


def test_tiny_policies(tiny):
    pols = of(tiny, "NetworkPolicy")
    assert {p["metadata"]["name"] for p in pols} == {"default-deny", "allow-dns", "egress-internet-web"}
    assert len(pols) == 3
    for p in pols:
        assert p["metadata"]["namespace"] == "ci-tiny"
        assert p["spec"]["podSelector"] == {}
        assert "ingress" not in p["spec"], "nothing may dial a job pod"
    deny = one(pols, "NetworkPolicy", "default-deny")
    assert deny["spec"]["policyTypes"] == ["Ingress", "Egress"]


def test_egress_internet_web():
    web = one(_component(EGRESS_WEB), "NetworkPolicy", "egress-internet-web")
    only = one(_component(EGRESS_ONLY), "NetworkPolicy", "egress-internet-only")
    assert web["spec"]["policyTypes"] == ["Egress"]
    assert wan_rule(web) == wan_rule(only)
    assert ports(wan_rule(web)) == {("TCP", 443)}
    web_net, only_net = internet_rule(web), internet_rule(only)
    assert web_net["to"] == only_net["to"], "same except list as egress-internet-only"
    assert web_net["to"][0]["ipBlock"]["except"] == NOT_INTERNET
    assert ports(web_net) == {("TCP", 80), ("TCP", 443)}
    assert len(egress_rules(web)) == 2


def _component(path: str) -> list[dict]:
    """Render a Component by wrapping a copy of it in a throwaway kustomization (it cannot be built alone)."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shutil.copytree(REPO / path, root / "component")
        (root / "kustomization.yaml").write_text(
            "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nnamespace: x\ncomponents:\n  - component\n"
        )
        out = subprocess.run(["kubectl", "kustomize", str(root)], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        return [d for d in yaml.safe_load_all(out.stdout) if d]


def test_dispatcher_policy(dispatcher):
    pols = of(dispatcher, "NetworkPolicy")
    assert {p["metadata"]["name"] for p in pols} == {"default-deny", "allow-dns", "ci-dispatch"}
    assert len(pols) == 3
    for p in pols:
        assert p["metadata"]["namespace"] == "ci-dispatch"
    pol = one(pols, "NetworkPolicy", "ci-dispatch")
    assert pol["spec"]["podSelector"] == {"matchLabels": {"app.kubernetes.io/name": "ci-dispatch"}}
    assert pol["spec"]["policyTypes"] == ["Ingress", "Egress"]
    (ing,) = pol["spec"]["ingress"]
    assert ing["from"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "prometheus"}}}]
    assert ports(ing) == {("TCP", 8080)}, "the usage port is not for the scrape; clients reach it through the Service by policy-visible pods only if added later"
    egress = pol["spec"]["egress"]
    assert len(egress) == 2
    forgejo = next(r for r in egress if "podSelector" in r["to"][0])
    assert forgejo["to"] == [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "forgejo"}},
            "podSelector": {
                "matchLabels": {"app.kubernetes.io/name": "forgejo", "app.kubernetes.io/instance": "forgejo"}
            },
        }
    ]
    assert ports(forgejo) == {("TCP", 3000)}
    api = next(r for r in egress if "ipBlock" in r["to"][0])
    assert [t["ipBlock"]["cidr"] for t in api["to"]] == NODES, "D16: the post-DNAT API server endpoints"
    assert all("except" not in t["ipBlock"] for t in api["to"])
    assert ports(api) == {("TCP", 6443)}
    # Each rule carries a comment: the policy says what the pod needs.
    text = (REPO / DISPATCHER / "networkpolicy.yaml").read_text().splitlines()
    for marker in ("ingress:", "egress:"):
        assert any(line.strip() == marker for line in text)
    rule_starts = [i for i, line in enumerate(text) if line.startswith("    - ")]
    assert len(rule_starts) == 3
    for i in rule_starts:
        assert text[i - 1].strip().startswith("#"), f"rule at line {i + 1} has no comment above it"


def test_monitoring(dispatcher):
    sm = one(dispatcher, "ServiceMonitor", "ci-dispatch")
    assert sm["metadata"]["labels"]["release"] == "prometheus"
    (ep,) = sm["spec"]["endpoints"]
    assert ep["port"] == "http" and ep["path"] == "/metrics"
    keep = [r for r in ep["metricRelabelings"] if r["action"] == "keep"]
    assert len(keep) == 1 and keep[0]["regex"].split("|")[0] == "ci_dispatch_.*"
    assert sm["spec"]["selector"]["matchLabels"] == {"app.kubernetes.io/name": "ci-dispatch"}
    svc = one(dispatcher, "Service", "ci-dispatch")
    assert {p["name"]: p["port"] for p in svc["spec"]["ports"]} == {"http": 8080, "usage": 8081}
    assert svc["spec"]["selector"] == {"app.kubernetes.io/name": "ci-dispatch"}

    rule = one(dispatcher, "PrometheusRule", "ci-dispatch-alerts")
    # The ruleSelector picks `release: prometheus`; a rule object without it never loads.
    for pr in of(dispatcher, "PrometheusRule"):
        assert pr["metadata"]["labels"]["release"] == "prometheus", pr["metadata"]["name"]
    rules = [r for g in rule["spec"]["groups"] for r in g["rules"]]
    assert {r["alert"] for r in rules} == ALERTS
    assert len(rules) == len(ALERTS) + 1, "CIDispatchLaunchFailing has two rules"
    for r in rules:
        assert r["labels"]["severity"] == "warning", f"{r['alert']}: D7, every phase 1 alert is weekday"
    launch = [r for r in rules if r["alert"] == "CIDispatchLaunchFailing"]
    assert sorted(r["labels"]["reason"] for r in launch) == ["errors", "held"]
    by_name = {r["alert"]: r for r in rules if r["alert"] != "CIDispatchLaunchFailing"}
    # A counter that only grows in a process: any rejection since start, not a recent increase.
    assert by_name["CIDispatchStoredConfigRejected"]["expr"] == "ci_dispatch_store_rejected_total > 0"
    # The findings alert names its kinds and leaves stored-config-rejected to the counter's alert.
    findings = by_name["CIDispatchConfigFindings"]["expr"]
    assert set(re.search(r'kind=~"([^"]+)"', findings).group(1).split("|")) == FINDING_KINDS
    assert "stored-config-rejected" not in findings
    undone = by_name["CIDispatchLaunchesUndone"]
    assert 'increase(ci_dispatch_launches_total{outcome="undone"}[1h])' in undone["expr"]
    assert undone["expr"].endswith(f"> {UNDONE_PER_HOUR}") and undone["for"] == "1h"


def test_renovate_never_automerges_ci_dispatch():
    rules = json.loads(RENOVATE.read_text())["packageRules"]
    idx = [i for i, r in enumerate(rules) if IMAGE in r.get("matchPackageNames", [])]
    assert len(idx) == 1, "exactly one dedicated rule"
    rule = rules[idx[0]]
    assert rule["automerge"] is False
    # After every rule that could match it: a later rule that names no package, or names this
    # one, or matches this file could re-enable automerge.
    statefulset = f"{DISPATCHER}/statefulset.yaml"
    for later in rules[idx[0] + 1 :]:
        names = later.get("matchPackageNames")
        files = later.get("matchFileNames")
        narrow = (names is not None and IMAGE not in names) or (
            files is not None and statefulset not in files and not any(f.endswith("/**") and statefulset.startswith(f[:-2]) for f in files)
        )
        assert narrow, f"a later rule could match ci-dispatch and override automerge: {later.get('description', later)[:80]}"


def test_no_plain_secrets(whole):
    assert of(whole, "Secret") == [], "the render holds SealedSecrets only"
    sealed = {d["metadata"]["name"]: d for d in of(whole, "SealedSecret")}
    assert set(sealed) == {"ci-dispatch-forgejo", "ci-dispatch-usage"}
    for name, key in (("ci-dispatch-forgejo", "forgejo-token"), ("ci-dispatch-usage", "usage-token")):
        s = sealed[name]
        assert s["metadata"]["namespace"] == "ci-dispatch"
        assert set(s["spec"]["encryptedData"]) == {key}
        assert len(s["spec"]["encryptedData"][key]) > 200, "a sealed value, not a placeholder"
        assert s["spec"]["template"]["metadata"]["namespace"] == "ci-dispatch"
