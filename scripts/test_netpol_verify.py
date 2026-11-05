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
(exit 1), never a vacuous PASS. `baseline` runs only against exactly three
Running probe pods on three nodes (else exit 2), and PASSes only when every pod
reports every canary row of the repo's matrix.txt (N rows, counted here, never
a literal) as answering.
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
  *" netpol-canary get pods "*) answer "$F/probe-pods.json" ;;
  *" exec "*) p=${args#* exec }; p=${p%% *}
    [ -f "$F/exec/$p.rc" ] || { echo "Error from server (NotFound): pods \"$p\" not found" >&2; exit 1; }
    cat "$F/exec/$p"; exit "$(cat "$F/exec/$p.rc")" ;;
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


# --- baseline ----------------------------------------------------------------------

MATRIX = SCRIPT.parent.parent / "cluster" / "netpol-canary" / "matrix.txt"


def canary_row_count() -> int:
    """N: the matrix rows the canary runs (scope all or canary), counted from the repo's matrix.txt."""
    n = 0
    for raw in MATRIX.read_text().splitlines():
        row = raw.split()
        if row and not row[0].startswith("#") and row[1] in ("all", "canary"):
            n += 1
    return n


def probe_pod(name: str, node: str, phase: str = "Running", deleting: bool = False) -> dict:
    meta = {"name": name}
    if deleting:
        meta["deletionTimestamp"] = "2026-11-05T18:50:00Z"
    return {"metadata": meta, "spec": {"nodeName": node}, "status": {"phase": phase}}


def probe_table(node: str, rows: int, fail: int = 0) -> str:
    """What `probe.sh once baseline` prints: one line per row, then SUMMARY."""
    lines = [f"PASS dropped open 192.168.0.{i}:443 row {i}" for i in range(rows - fail)]
    lines += [f"FAIL dropped timeout 192.168.0.{200 + i}:443 dead target" for i in range(fail)]
    lines.append(f"SUMMARY {rows - fail}/{rows} baseline node={node}")
    return "\n".join(lines) + "\n"


def baseline_fixtures(tmp: Path, pods: list[dict], execs: dict[str, tuple[str, int]]) -> Path:
    """Agent fixtures stay empty; probe pods and each pod's exec answer (stdout, exit code)."""
    fixtures(tmp, ds_doc=None, pods=[])
    f = tmp / "fx"
    (f / "probe-pods.json").write_text(json.dumps({"items": pods}))
    (f / "exec").mkdir()
    for name, (out, rc) in execs.items():
        (f / "exec" / name).write_text(out)
        (f / "exec" / f"{name}.rc").write_text(str(rc))
    return tmp


def baseline(tmp: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        PATH=f"{tmp / 'bin'}:{env['PATH']}",
        NETPOL_FIXTURES=str(tmp / "fx"),
        KUBECONFIG=str(tmp / "no-such-kubeconfig"),
    )
    return subprocess.run([str(SCRIPT), "baseline"], env=env, capture_output=True, text=True, timeout=60)


def three_probes(**overrides) -> tuple[list[dict], dict[str, tuple[str, int]]]:
    """Three Running probe pods on three nodes, each answering every row. overrides: pod -> (out, rc)."""
    n = canary_row_count()
    pods = [probe_pod(f"netpol-probe-{node}", node) for node in NODES]
    execs = {f"netpol-probe-{node}": (probe_table(node, n), 0) for node in NODES}
    execs.update(overrides)
    return pods, execs


def test_matrix_row_count_is_derived():
    """N is whatever matrix.txt holds; the baseline, canary and runner checks print it (F8)."""
    assert canary_row_count() > 0


def test_baseline_passes(tmp_path):
    n = canary_row_count()
    r = baseline(baseline_fixtures(tmp_path, *three_probes()))
    assert r.returncode == 0, r.stdout + r.stderr
    for node in NODES:
        assert f"PASS {node} {n}/{n} rows answer" in r.stdout
        assert f"SUMMARY {n}/{n} baseline node={node}" in r.stdout, "each pod's table is printed"
    assert r.stdout.rstrip().splitlines()[-1] == f"baseline: PASS (3 nodes × {n} rows answer)"


def test_baseline_no_probe_pods_is_exit_2(tmp_path):
    r = baseline(baseline_fixtures(tmp_path, [], {}))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "no netpol-probe pods" in r.stderr
    assert "baseline: PASS" not in r.stdout


def test_baseline_two_pods_is_exit_2(tmp_path):
    pods, execs = three_probes()
    r = baseline(baseline_fixtures(tmp_path, pods[:2], execs))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "exactly 3 Running netpol-probe pods on 3 nodes" in r.stderr


def test_baseline_two_nodes_is_exit_2(tmp_path):
    pods, execs = three_probes()
    pods[2]["spec"]["nodeName"] = pods[1]["spec"]["nodeName"]
    r = baseline(baseline_fixtures(tmp_path, pods, execs))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "exactly 3 Running netpol-probe pods on 3 nodes" in r.stderr


def test_baseline_pending_pod_is_exit_2(tmp_path):
    pods, execs = three_probes()
    pods[0]["status"]["phase"] = "Pending"
    r = baseline(baseline_fixtures(tmp_path, pods, execs))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "exactly 3 Running netpol-probe pods on 3 nodes" in r.stderr


def test_baseline_ignores_a_terminating_pod(tmp_path):
    """baseline neither counts a rollout's outgoing pod (deletionTimestamp set) nor runs anything in it."""
    pods, execs = three_probes()
    pods.append(probe_pod("netpol-probe-old", "top", deleting=True))
    r = baseline(baseline_fixtures(tmp_path, pods, execs))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "netpol-probe-old" not in r.stdout


def test_baseline_dead_target_fails(tmp_path):
    """One row that neither connects nor gets a refusal: the row Task 6 Step 8 exists to catch."""
    n = canary_row_count()
    pods, execs = three_probes(**{"netpol-probe-middle": (probe_table("middle", n, fail=1), 1)})
    r = baseline(baseline_fixtures(tmp_path, pods, execs))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL dropped timeout 192.168.0.200:443 dead target" in r.stdout
    assert "FAIL middle" in r.stdout
    assert "baseline: PASS" not in r.stdout


def test_baseline_stale_matrix_fails(tmp_path):
    """A pod that ran one row fewer than the repo's matrix (a stale ConfigMap) is a FAIL."""
    n = canary_row_count()
    pods, execs = three_probes(**{"netpol-probe-top": (probe_table("top", n - 1), 0)})
    r = baseline(baseline_fixtures(tmp_path, pods, execs))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL top" in r.stdout


def test_baseline_exec_error_fails(tmp_path):
    """No table at all (exec refused, probe.sh missing): a FAIL, never a vacuous PASS."""
    pods, execs = three_probes(**{"netpol-probe-bottom": ("", 1)})
    r = baseline(baseline_fixtures(tmp_path, pods, execs))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL bottom" in r.stdout


def test_baseline_summary_from_another_node_fails(tmp_path):
    """NODE_NAME in the pod must be the node the API says it runs on."""
    n = canary_row_count()
    pods, execs = three_probes(**{"netpol-probe-top": (probe_table("middle", n), 0)})
    r = baseline(baseline_fixtures(tmp_path, pods, execs))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL top" in r.stdout
