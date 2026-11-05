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
a literal) as answering. `canary` (B1b) PASSes only when every pod's enforced
run passes every row in the repo's order, every dropped row has a deny line from
that pod to the row's literal destination or to a backend's EndpointSlice IP,
and `stray` passes: every deny line names a pod in a namespace that has a
NetworkPolicy.
"""

import json
import os
import re
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
echo "$*" >> "$F/calls.log"
case "$args" in
  *" networkpolicies "*) answer "$F/np.json" ;;
  *" endpointslices "*) ns=${args#* -n }; ns=${ns%% *}; svc=${args#*kubernetes.io/service-name=}; svc=${svc%% *}
    answer "$F/eps/${ns}_${svc}" ;;
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
PROBE = MATRIX.parent / "probe.sh"


NODE_IP = {"top": "192.168.0.240", "middle": "192.168.0.241", "bottom": "192.168.0.242"}


def own_bmcs() -> dict[str, str]:
    """probe.sh's NODE_BMCS default, node -> its own BMC (test_netpol_manifests pins it to bmcctl)."""
    m = re.search(r"NODE_BMCS:-([a-z0-9.= ]+)\}", PROBE.read_text())
    return dict(pair.split("=", 1) for pair in m.group(1).split())


def canary_row_count(node: str | None = None) -> int:
    """N for a node: the canary rows (scope all or canary) of the repo's matrix.txt, less the row that
    dials that node's own BMC. With no node, every canary row."""
    skip = own_bmcs().get(node) if node else None
    n = 0
    for raw in MATRIX.read_text().splitlines():
        row = raw.split()
        if row and not row[0].startswith("#") and row[1] in ("all", "canary") and row[2] != skip:
            n += 1
    return n


def probe_pod(name: str, node: str, phase: str = "Running", deleting: bool = False) -> dict:
    meta = {"name": name}
    if deleting:
        meta["deletionTimestamp"] = "2026-11-05T18:50:00Z"
    return {"metadata": meta, "spec": {"nodeName": node}, "status": {"phase": phase, "hostIP": NODE_IP.get(node, "192.168.0.240")}}


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
    pods = [probe_pod(f"netpol-probe-{node}", node) for node in NODES]
    execs = {f"netpol-probe-{node}": (probe_table(node, canary_row_count(node)), 0) for node in NODES}
    execs.update(overrides)
    return pods, execs


def test_matrix_row_count_is_derived():
    """N is whatever matrix.txt holds; the baseline, canary and runner checks print it (F8). Each node
    skips the one row that dials its own BMC."""
    assert canary_row_count() > 0
    for node in NODES:
        assert canary_row_count(node) == canary_row_count() - 1, node


def test_baseline_passes(tmp_path):
    r = baseline(baseline_fixtures(tmp_path, *three_probes()))
    assert r.returncode == 0, r.stdout + r.stderr
    deadline = int(re.search(r"^PROBE_DEADLINE_DEFAULT=(\d+)$", PROBE.read_text(), re.M).group(1))
    for node in NODES:
        n = canary_row_count(node)
        assert f"PASS {node} {n}/{n} rows answer" in r.stdout
        assert f"SUMMARY {n}/{n} baseline node={node}" in r.stdout, "each pod's table is printed"
        # Each node's exec timeout follows its N: every row and the WAN lookup at the deadline, plus 30 s.
        assert f"baseline: {node} {n} rows, exec timeout {(n + 1) * deadline + 30} s" in r.stdout
    counts = {canary_row_count(node) for node in NODES}
    assert len(counts) == 1, "every node skips exactly one BMC row"
    assert r.stdout.rstrip().splitlines()[-1] == f"baseline: PASS (3 nodes × {counts.pop()} rows answer)"


def test_baseline_each_node_checks_its_own_count(tmp_path):
    """middle reporting every row (it did not skip its own BMC: a stale ConfigMap) is a FAIL."""
    pods, execs = three_probes(**{"netpol-probe-middle": (probe_table("middle", canary_row_count()), 0)})
    r = baseline(baseline_fixtures(tmp_path, pods, execs))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL middle" in r.stdout
    assert "PASS top" in r.stdout and "PASS bottom" in r.stdout


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
    n = canary_row_count("middle")
    pods, execs = three_probes(**{"netpol-probe-middle": (probe_table("middle", n, fail=1), 1)})
    r = baseline(baseline_fixtures(tmp_path, pods, execs))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL dropped timeout 192.168.0.200:443 dead target" in r.stdout
    assert "FAIL middle" in r.stdout
    assert "baseline: PASS" not in r.stdout


def test_baseline_stale_matrix_fails(tmp_path):
    """A pod that ran one row fewer than the repo's matrix (a stale ConfigMap) is a FAIL."""
    n = canary_row_count("top")
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
    n = canary_row_count("top")
    pods, execs = three_probes(**{"netpol-probe-top": (probe_table("middle", n), 0)})
    r = baseline(baseline_fixtures(tmp_path, pods, execs))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL top" in r.stdout


# --- canary and stray (B1b) -----------------------------------------------------

WAN_IP = "152.44.247.88"
POLICED = [{"metadata": {"name": "default-deny", "namespace": "netpol-canary"}},
           {"metadata": {"name": "allow-dns", "namespace": "netpol-canary"}}]


def expand(node: str) -> list[list[str]]:
    """The rows probe.sh runs on NODE: its own probe_rows over the repo's matrix.txt."""
    env = dict(os.environ, NODE_NAME=node, HOST_IP=NODE_IP[node], WAN_IP=WAN_IP)
    r = subprocess.run(["bash", "-c", '. "$1"; probe_rows "$2" canary', "_", str(PROBE), str(MATRIX)],
                       env=env, capture_output=True, text=True, check=True)
    return [line.split() for line in r.stdout.splitlines()]


def backends() -> dict[str, list[str]]:
    """ns/svc -> fake EndpointSlice IPs, one distinct /32 per backend in the matrix."""
    names = sorted({row[4] for row in expand("top") if row[4] != "-"})
    return {b: [f"10.244.9.{i + 1}", f"10.244.8.{i + 1}"] for i, b in enumerate(names)}


def enforced_table(node: str, rows: list[list[str]], fail_refused: str | None = None) -> str:
    """What `probe.sh once enforced` prints when every row passes, or with one dropped row refused."""
    lines, npass = [], 0
    for expect, _scope, host, port, _backend, *note in rows:
        got = "timeout" if expect == "dropped" else "open"
        verdict = "PASS"
        if f"{host}:{port}" == fail_refused:
            got, verdict = "refused", "FAIL"
        npass += verdict == "PASS"
        lines.append(f"{verdict} {expect} {got} {host}:{port} {' '.join(note)}".rstrip())
    lines.append(f"SUMMARY {npass}/{len(rows)} enforced node={node}")
    return "\n".join(lines) + "\n"


def deny_line(src_pod: str, dst: str, dst_pod: str = "external") -> str:
    return (f'I1105 21:30:00.000000       1 verdictlog.go:179] "Packet verdict" id=7 verdict="deny" family="IPv4" '
            f'protocol="TCP" src="10.244.3.9:40000" dst="{dst}" srcPod="{src_pod}" dstPod="{dst_pod}" '
            f'direction="egress" evaluator="x"')


def deny_lines(pod_name: str, rows: list[list[str]], skip: str | None = None) -> list[str]:
    """One deny line per dropped row: the literal destination, or the backend's first endpoint IP
    on a translated port (the post-DNAT destination)."""
    eps, out = backends(), []
    for expect, _scope, host, port, backend, *_ in rows:
        if expect != "dropped" or f"{host}:{port}" == skip:
            continue
        dst = f"{host}:{port}" if backend == "-" else f"{eps[backend][0]}:8443"
        out.append(deny_line(f"netpol-canary/{pod_name}", dst))
    return out


def canary_fixtures(tmp: Path, *, nodes=NODES, tables=None, rcs=None, extra_log=(), skip=None, np_items=POLICED) -> Path:
    """Probe pods on NODES answering enforced runs, three agents whose logs hold the deny lines,
    EndpointSlices for every backend. tables/rcs: pod -> override; skip: (pod, host:port) not logged."""
    fixtures(tmp, ds_doc=ds(3, 3), pods=agents(3), np_items=np_items)
    f = tmp / "fx"
    (f / "probe-pods.json").write_text(json.dumps({"items": [probe_pod(f"netpol-probe-{n}", n) for n in nodes]}))
    (f / "exec").mkdir()
    (f / "eps").mkdir()
    for b, ips in backends().items():
        ns, svc = b.split("/")
        (f / "eps" / f"{ns}_{svc}").write_text("\n".join(ips) + "\n")
    lines = []
    for n in nodes:
        name, rows = f"netpol-probe-{n}", expand(n)
        (f / "exec" / name).write_text((tables or {}).get(name, enforced_table(n, rows)))
        (f / "exec" / f"{name}.rc").write_text(str((rcs or {}).get(name, 0)))
        lines += deny_lines(name, rows, skip=skip[1] if skip and skip[0] == name else None)
    lines += list(extra_log)
    # Spread over the three agents: canary reads every agent's log.
    for i, a in enumerate(agents(3)):
        (f / "logs" / a["metadata"]["name"]).write_text(LOG_START + "".join(x + "\n" for x in lines[i::3]))
    return tmp


def run(tmp: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(PATH=f"{tmp / 'bin'}:{env['PATH']}", NETPOL_FIXTURES=str(tmp / "fx"),
               KUBECONFIG=str(tmp / "no-such-kubeconfig"))
    return subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=120)


def dropped_count(node: str) -> int:
    return sum(r[0] == "dropped" for r in expand(node))


def test_canary_passes(tmp_path):
    r = run(canary_fixtures(tmp_path), "canary")
    assert r.returncode == 0, r.stdout + r.stderr
    for node in NODES:
        n, d = len(expand(node)), dropped_count(node)
        assert n == canary_row_count(node)
        assert f"{node} rows {n}/{n} deny-logged {d}/{d}" in r.stdout
    assert re.search(r"^stray: PASS \(\d+ deny lines, policed: netpol-canary\)$", r.stdout, re.M), r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == "canary: PASS (3 nodes)"
    calls = (tmp_path / "fx" / "calls.log").read_text()
    assert "--since=10m" in calls and "once enforced" in calls


def test_canary_unlogged_row_fails(tmp_path):
    """A row that timed out but that no agent logged as denied (a dead target, a route that never
    reached the enforcer) is a FAIL."""
    wan = f"{WAN_IP}:8585"
    r = run(canary_fixtures(tmp_path, skip=("netpol-probe-middle", wan)), "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    n, d = len(expand("middle")), dropped_count("middle")
    assert f"middle rows {n}/{n} deny-logged {d - 1}/{d}" in r.stdout
    assert f"NOT-LOGGED {wan}" in r.stdout
    assert "canary: PASS" not in r.stdout


def test_canary_backend_row_needs_an_endpoint_ip(tmp_path):
    """A deny line to the ClusterIP itself does not count: the enforcer sees the post-DNAT
    backend (design §3.4 item 1), so the row's backend IPs are what must appear."""
    rows = expand("top")
    t = canary_fixtures(tmp_path, skip=("netpol-probe-top", "10.96.0.1:443"),
                        extra_log=[deny_line("netpol-canary/netpol-probe-top", "10.96.0.1:443")])
    r = run(t, "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "NOT-LOGGED 10.96.0.1:443" in r.stdout
    assert any(row[2] == "10.96.0.1" for row in rows)


def test_canary_refused_row_fails(tmp_path):
    rows = expand("bottom")
    table = enforced_table("bottom", rows, fail_refused="192.168.0.1:22")
    r = run(canary_fixtures(tmp_path, tables={"netpol-probe-bottom": table}, rcs={"netpol-probe-bottom": 1}), "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL dropped refused 192.168.0.1:22" in r.stdout
    n = len(rows)
    assert f"bottom rows {n - 1}/{n} " in r.stdout


def test_canary_stale_matrix_fails(tmp_path):
    """A pod whose rows differ from the repo's matrix (a stale ConfigMap) is a FAIL, even if its own
    run passed."""
    rows = expand("top")
    stale = enforced_table("top", rows[:-1])
    r = run(canary_fixtures(tmp_path, tables={"netpol-probe-top": stale}), "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "canary: PASS" not in r.stdout


def test_canary_rows_differ_fails(tmp_path):
    """Same count, other rows: middle's pod ran top's expansion (top's BMC skipped, top's host IP).
    Only the row-by-row check against the repo catches it."""
    rows = expand("top")
    assert len(rows) == len(expand("middle")) and rows != expand("middle")
    table = enforced_table("middle", rows)
    r = run(canary_fixtures(tmp_path, tables={"netpol-probe-middle": table}), "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    assert re.search(r"^FAIL middle .*order=row ", r.stdout, re.M), r.stdout
    assert "canary: PASS" not in r.stdout


def test_canary_stray_line_fails(tmp_path):
    t = canary_fixtures(tmp_path, extra_log=[deny_line("media/mm-plex-0", "192.168.0.1:53")])
    r = run(t, "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    assert re.search(r"^STRAY .*srcPod=\"media/mm-plex-0\"", r.stdout, re.M), r.stdout
    assert "stray: FAIL" in r.stdout
    assert "canary: PASS" not in r.stdout


def test_canary_two_pods_is_exit_2(tmp_path):
    r = run(canary_fixtures(tmp_path, nodes=NODES[:2]), "canary")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "need exactly 3 Running netpol-probe pods on 3 nodes" in r.stderr
    assert "canary: PASS" not in r.stdout


def test_canary_one_node(tmp_path):
    r = run(canary_fixtures(tmp_path), "canary", "--node", "middle")
    assert r.returncode == 0, r.stdout + r.stderr
    n, d = len(expand("middle")), dropped_count("middle")
    assert f"middle rows {n}/{n} deny-logged {d}/{d}" in r.stdout
    assert "top rows" not in r.stdout and "bottom rows" not in r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == "canary: PASS (1 node)"


def test_canary_unknown_node_is_exit_2(tmp_path):
    r = run(canary_fixtures(tmp_path), "canary", "--node", "nowhere")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "need exactly 1 Running netpol-probe pod on nowhere" in r.stderr


def stray_fixtures(tmp: Path, lines: list[str], np_items=POLICED) -> Path:
    fixtures(tmp, ds_doc=ds(3, 3), pods=agents(3), np_items=np_items)
    for a in agents(3):
        (tmp / "fx" / "logs" / a["metadata"]["name"]).write_text(LOG_START + "".join(x + "\n" for x in lines))
        lines = []
    return tmp


def test_stray_passes(tmp_path):
    lines = [deny_line("netpol-canary/netpol-probe-x", "192.168.0.2:80"),
             # An ingress denial: the source is outside, the destination is a policed pod.
             deny_line("external", "10.244.3.7:9377", dst_pod="netpol-canary/netpol-echo-y")]
    r = run(stray_fixtures(tmp_path, lines), "stray")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.rstrip().splitlines()[-1] == "stray: PASS (2 deny lines, policed: netpol-canary)"
    assert "--since=15m" in (tmp_path / "fx" / "calls.log").read_text()


def test_stray_since(tmp_path):
    r = run(stray_fixtures(tmp_path, []), "stray", "--since", "2h")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--since=2h" in (tmp_path / "fx" / "calls.log").read_text()
    assert "stray: PASS (0 deny lines, policed: netpol-canary)" in r.stdout


@pytest.mark.parametrize("src,dst_pod", [
    ("media/mm-plex-0", "external"),        # a pod in a namespace with no policy
    ("external", "external"),               # a node IP or anything else nobody selected (#380 class)
    ("netpol-canary-x/pod", "external"),    # namespace prefix must not count
])
def test_stray_fails(tmp_path, src, dst_pod):
    lines = [deny_line("netpol-canary/netpol-probe-x", "192.168.0.2:80"), deny_line(src, "192.168.0.1:53", dst_pod=dst_pod)]
    r = run(stray_fixtures(tmp_path, lines), "stray")
    assert r.returncode == 1, r.stdout + r.stderr
    assert sum(line.startswith("STRAY ") for line in r.stdout.splitlines()) == 1
    assert r.stdout.rstrip().splitlines()[-1] == "stray: FAIL (1 of 2 deny lines stray, policed: netpol-canary)"


def test_stray_prints_at_most_ten(tmp_path):
    lines = [deny_line("media/p", f"192.168.0.1:{i}") for i in range(1, 13)]
    r = run(stray_fixtures(tmp_path, lines), "stray")
    assert r.returncode == 1, r.stdout + r.stderr
    assert sum(line.startswith("STRAY ") for line in r.stdout.splitlines()) == 10
    assert "stray: FAIL (12 of 12 deny lines stray" in r.stdout


def test_stray_no_policies_means_every_deny_is_stray(tmp_path):
    r = run(stray_fixtures(tmp_path, [deny_line("netpol-canary/p", "192.168.0.2:80")], np_items=()), "stray")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "policed: none" in r.stdout


def test_stray_no_agents_is_exit_2(tmp_path):
    fixtures(tmp_path, ds_doc=ds(0, 0), pods=[], np_items=POLICED)
    r = run(tmp_path, "stray")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "no kube-network-policies agent pods" in r.stderr


@pytest.mark.parametrize("args", [["stray", "--since"], ["stray", "15m"], ["canary", "--node"], ["canary", "top"], ["idle", "x"]])
def test_bad_arguments_are_exit_2(tmp_path, args):
    r = run(stray_fixtures(tmp_path, []), *args)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "usage:" in r.stderr
