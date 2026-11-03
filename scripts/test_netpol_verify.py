"""Self-test for `scripts/netpol-verify.sh idle`, with no cluster.

    python -m pytest -q -p no:cacheprovider scripts/test_netpol_verify.py

Runs as the `netpol-verify-selftest` pre-commit hook. Each case puts a stub
`kubectl` first on PATH. The stub answers from fixture files shaped like what
the live cluster returned on 2026-11-03: the DaemonSet status, the pod list
reduced to the fields `idle` reads, the agent's /metrics and its start-up log.
KUBECONFIG points nowhere, so a call that misses the stub fails instead of
reaching galaxy.

What it pins: `idle` PASSes only when every scheduled agent is Ready, listed and
judged PASS. A partial pod list, a failed parse or empty jq output is a FAIL
(exit 1), never a vacuous PASS.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "netpol-verify.sh"

# kubectl get --raw .../pods/<pod>:9080/proxy/metrics, v1.1.3 on bottom, 2026-11-03.
METRICS_IDLE = """\
kube_network_policies_nfqueue_packet_id{queue="98"} 0
kube_network_policies_nfqueue_queue_dropped{queue="98"} 0
kube_network_policies_nfqueue_queue_total{queue="98"} 0
kube_network_policies_nfqueue_user_dropped{queue="98"} 0
kube_network_policies_packet_process_duration_microseconds{quantile="0.5"} NaN
kube_network_policies_packet_process_duration_microseconds{quantile="0.9"} NaN
kube_network_policies_packet_process_duration_microseconds{quantile="0.99"} NaN
kube_network_policies_packet_process_duration_microseconds_sum 0
kube_network_policies_packet_process_duration_microseconds_count 0
kube_network_policies_verdict_log_dropped 0
"""

# The agent's log up to its first nftables sync (feature-gate lines dropped).
LOG_START = """\
I1103 18:35:10.032842       1 main.go:62] "called" args=[]
I1103 18:35:10.032985       1 main.go:65] "flag" name="disable-nri" value="true"
I1103 18:35:10.032998       1 main.go:65] "flag" name="fail-open" value="true"
I1103 18:35:10.033002       1 main.go:65] "flag" name="hostname-override" value="bottom"
I1103 18:35:10.033045       1 main.go:65] "flag" name="log-verdicts" value="deny"
I1103 18:35:10.033056       1 main.go:65] "flag" name="metrics-bind-address" value=":9080"
I1103 18:35:10.033067       1 main.go:65] "flag" name="nfqueue-id" value="98"
I1103 18:35:10.033073       1 main.go:65] "flag" name="strict-mode" value="true"
I1103 18:35:10.033078       1 main.go:65] "flag" name="v" value="1"
I1103 18:35:10.033660       1 cmd.go:110] kube-network-policies go go1.27.3 build: d3fc697c1acaa0c8872c262ff3e04d8299eac6b3 time: 2026-11-02T18:41:54Z
I1103 18:35:10.534261       1 controller.go:189] "Policy engine is ready."
I1103 18:35:10.534308       1 metrics.go:72] Registering metrics
I1103 18:35:10.534438       1 controller.go:505] "Syncing nftables rules" logger="nftables-sync"
I1103 18:35:10.535603       1 controller.go:508] "Syncing nftables rules" logger="nftables-sync" elapsed="1.152329ms"
"""

STUB = r"""#!/usr/bin/env bash
# Test stub for kubectl: answers from $NETPOL_FIXTURES, never from a cluster.
F=$NETPOL_FIXTURES
answer() { [ -f "$1" ] || { echo "Error from server (NotFound): stub has no $1" >&2; exit 1; }; cat "$1"; }
args=" $* "
case "$args" in
  *" networkpolicies "*) answer "$F/np.json" ;;
  *" daemonset "*) answer "$F/ds.json" ;;
  *" get pods "*) answer "$F/pods.json" ;;
  *" --raw "*) p=${args#*/pods/}; answer "$F/metrics/${p%%:*}" ;;
  *" logs "*) p=${args#* logs }; answer "$F/logs/${p%% *}" ;;
  *) echo "stub kubectl: unexpected call: $*" >&2; exit 1 ;;
esac
"""

NODES = ["bottom", "top", "middle"]


def pod(name: str, node: str, ready: bool = True, restarts: int = 0) -> dict:
    """One agent pod, reduced to the fields `idle` reads (shape captured live)."""
    return {
        "metadata": {"name": name},
        "spec": {"nodeName": node},
        "status": {
            "conditions": [
                {"type": "Initialized", "status": "True"},
                {"type": "Ready", "status": "True" if ready else "False"},
                {"type": "PodScheduled", "status": "True"},
            ],
            "containerStatuses": [{"name": "kube-network-policies", "restartCount": restarts}],
        },
    }


def ds(desired: int, ready: int) -> dict:
    return {"status": {"desiredNumberScheduled": desired, "numberReady": ready,
                       "currentNumberScheduled": desired, "numberAvailable": ready}}


def fixtures(tmp: Path, *, ds_doc, pods, np_items=(), metrics=METRICS_IDLE, log=LOG_START) -> Path:
    """Write the stub and its answers. ds_doc/pods may be a str to send raw bytes."""
    (tmp / "bin").mkdir()
    stub = tmp / "bin" / "kubectl"
    stub.write_text(STUB)
    stub.chmod(0o755)
    f = tmp / "fx"
    (f / "metrics").mkdir(parents=True)
    (f / "logs").mkdir()
    (f / "np.json").write_text(json.dumps({"items": list(np_items)}))
    if ds_doc is not None:
        (f / "ds.json").write_text(ds_doc if isinstance(ds_doc, str) else json.dumps(ds_doc))
    (f / "pods.json").write_text(pods if isinstance(pods, str) else json.dumps({"items": pods}))
    for p in pods if isinstance(pods, list) else []:
        n = p["metadata"]["name"]
        (f / "metrics" / n).write_text(metrics)
        (f / "logs" / n).write_text(log)
    return tmp


def idle(tmp: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        PATH=f"{tmp / 'bin'}:{env['PATH']}",
        NETPOL_FIXTURES=str(tmp / "fx"),
        KUBECONFIG=str(tmp / "no-such-kubeconfig"),
    )
    return subprocess.run([str(SCRIPT), "idle"], env=env, capture_output=True, text=True, timeout=60)


def agents(n: int) -> list[dict]:
    return [pod(f"kube-network-policies-{i}", NODES[i]) for i in range(n)]


def test_one_agent_idle_passes(tmp_path):
    r = idle(fixtures(tmp_path, ds_doc=ds(1, 1), pods=agents(1)))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS bottom kube-network-policies-0 packet_id=0 packets=0 restarts=0 flags=ok" in r.stdout
    assert r.stdout.rstrip().endswith("idle: PASS (1 agents, 0 NetworkPolicies)")


def test_three_agents_idle_passes(tmp_path):
    r = idle(fixtures(tmp_path, ds_doc=ds(3, 3), pods=agents(3)))
    assert r.returncode == 0, r.stdout + r.stderr
    assert sum(line.startswith("PASS ") for line in r.stdout.splitlines()) == 3
    assert "idle: PASS (3 agents, 0 NetworkPolicies)" in r.stdout


def test_missing_pod_fails(tmp_path):
    """Desired 3, two pods exist (the third never created): partial coverage is a FAIL."""
    r = idle(fixtures(tmp_path, ds_doc=ds(3, 2), pods=agents(2)))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "idle: PASS" not in r.stdout
    assert "FAIL coverage" in r.stdout


def test_pod_list_short_of_ready_count_fails(tmp_path):
    """The DaemonSet says 3/3 but only 2 pods are listed: still a FAIL."""
    r = idle(fixtures(tmp_path, ds_doc=ds(3, 3), pods=agents(2)))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL coverage" in r.stdout


def test_empty_jq_output_fails(tmp_path):
    """Items that are not pod objects: jq yields no rows. That must not PASS vacuously."""
    r = idle(fixtures(tmp_path, ds_doc=ds(1, 1), pods='{"items": ["not-a-pod"]}'))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "idle: PASS" not in r.stdout
    assert "FAIL parse" in r.stdout


def test_unparseable_daemonset_fails(tmp_path):
    r = idle(fixtures(tmp_path, ds_doc="this is not json", pods=agents(1)))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL parse" in r.stdout


def test_daemonset_without_counts_fails(tmp_path):
    r = idle(fixtures(tmp_path, ds_doc={"status": {}}, pods=agents(1)))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL parse" in r.stdout


def test_not_ready_agent_fails(tmp_path):
    r = idle(fixtures(tmp_path, ds_doc=ds(1, 0), pods=[pod("kube-network-policies-0", "bottom", ready=False)]))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL bottom kube-network-policies-0 not-ready" in r.stdout


def test_queued_packet_fails(tmp_path):
    m = METRICS_IDLE.replace('packet_id{queue="98"} 0', 'packet_id{queue="98"} 17')
    r = idle(fixtures(tmp_path, ds_doc=ds(1, 1), pods=agents(1), metrics=m))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "packet_id=17" in r.stdout


@pytest.mark.parametrize("ds_doc", [ds(1, 1), None])
def test_policies_exist_is_exit_2(tmp_path, ds_doc):
    r = idle(fixtures(tmp_path, ds_doc=ds_doc, pods=agents(1), np_items=[{"metadata": {"name": "x"}}]))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "policies exist: idle check meaningless" in r.stderr


def test_no_agent_pods_is_exit_2(tmp_path):
    r = idle(fixtures(tmp_path, ds_doc=ds(0, 0), pods=[]))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "no kube-network-policies agent pods" in r.stderr


def test_selftest_passes():
    """Offline: probe.sh, matrix.txt, deny_match and netpol-connect.js (needs node and bash from the dev shell)."""
    r = subprocess.run([str(SCRIPT), "selftest"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "selftest: PASS" in r.stdout
