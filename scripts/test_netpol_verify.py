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
from datetime import datetime, timedelta, timezone
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
ns=${args#* -n }; ns=${ns%% *}
case "$args" in
  *" networkpolicies "*) answer "$F/np.json" ;;
  *" get prometheusrule "*) answer "$F/pr.json" ;;
  *" --raw /api/v1/namespaces/prometheus/services/"*) answer "$F/rules.json" ;;
  *" get service "*) svc=${args#* get service }; answer "$F/svc/${ns}_${svc%% *}" ;;
  *" endpointslices "*) svc=${args#*kubernetes.io/service-name=}; answer "$F/eps/${ns}_${svc%% *}" ;;
  *" get pods -A "*) answer "$F/allpods.json" ;;
  *" netpol-canary get pods "*) answer "$F/probe-pods.json" ;;
  *" forgejo-runner get pods "*) answer "$F/runner-pods.json" ;;
  *" exec "*) p=${args#* exec }; p=${p%% *}
    [ -f "$F/exec/$p.rc" ] || { echo "Error from server (NotFound): pods \"$p\" not found" >&2; exit 1; }
    cat "$F/exec/$p"; exit "$(cat "$F/exec/$p.rc")" ;;
  *" daemonset "*) answer "$F/ds.json" ;;
  *" get pods "*) answer "$F/pods.json" ;;
  *" --raw "*) p=${args#*/pods/}; answer "$F/metrics/${p%%:*}" ;;
  *" logs "*) p=${args#* logs }; f=$F/logs/${p%% *}
    case "$args" in *" --previous "*) f=$f.previous ;; esac
    [ -f "$f" ] || { echo "Error from server (BadRequest): previous terminated container not found" >&2; exit 1; }
    case "$args" in *" --limit-bytes="*) n=${args#* --limit-bytes=}; head -c "${n%% *}" "$f"; exit 0 ;; esac
    # Read n (from 0) of a log is its .read<n> file if there is one; from the second read on, a
    # .rerun file (if any) stands for the logs after a re-run.
    c=$F/reads.${p%% *}; n=$(cat "$c" 2> /dev/null || echo 0); echo $((n + 1)) > "$c"
    if [ -f "$f.read$n" ]; then f=$f.read$n; elif [ "$n" -ge 1 ] && [ -f "$f.rerun" ]; then f=$f.rerun; fi
    cat "$f" ;;
  *) echo "stub kubectl: unexpected call: $*" >&2; exit 1 ;;
esac
"""

NODES = ["bottom", "top", "middle"]


def pod(name: str, node: str, ready: bool = True, restarts: int = 0, started: str = "2026-11-03T18:35:10Z") -> dict:
    """One agent pod, reduced to the fields `idle` and `stray` read (shape captured live). The default
    start matches LOG_START's first line."""
    return {
        "metadata": {"name": name},
        "spec": {"nodeName": node},
        "status": {
            "conditions": [
                {"type": "Initialized", "status": "True"},
                {"type": "Ready", "status": "True" if ready else "False"},
                {"type": "PodScheduled", "status": "True"},
            ],
            "containerStatuses": [{"name": "kube-network-policies", "restartCount": restarts,
                                   "state": {"running": {"startedAt": started}}}],
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
ECHO_POD = "netpol-echo-5b67965c7c-rmnfq"
ECHO_IP = "10.244.0.201"
PROBE_IP = {"top": "10.244.3.10", "middle": "10.244.4.10", "bottom": "10.244.0.10"}
OLD = "2026-11-03T00:00:00Z"  # when the fixtures' long-lived pods started
ECHO_9377 = "netpol-echo.netpol-canary.svc.cluster.local:9377"
ECHO_9378 = "netpol-echo-pod.netpol-canary.svc.cluster.local:9378"


def iso_ago(s: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def expand(node: str) -> list[list[str]]:
    """The rows probe.sh runs on NODE: its own probe_rows over the repo's matrix.txt."""
    env = dict(os.environ, NODE_NAME=node, HOST_IP=NODE_IP[node], WAN_IP=WAN_IP)
    r = subprocess.run(["bash", "-c", '. "$1"; probe_rows "$2" canary', "_", str(PROBE), str(MATRIX)],
                       env=env, capture_output=True, text=True, check=True)
    return [line.split() for line in r.stdout.splitlines()]


def backend_ports() -> dict[str, set[int]]:
    """ns/svc -> the row ports the matrix dials it on."""
    out: dict[str, set[int]] = {}
    for row in expand("top"):
        if row[4] != "-":
            out.setdefault(row[4], set()).add(int(row[3]))
    return out


def backends(live_like: bool = False) -> dict[str, tuple[dict, dict]]:
    """ns/svc -> (Service, EndpointSlice list) for every backend in the matrix.

    By default each backend has its own fake pod IPs and a target port 1000 above the Service port
    (DNAT). Both echo Services always sit on the one echo pod IP with 9376/9377. live_like: also what
    galaxy has for default/kubernetes (2026-11-05), the three node IPs on 6443, which other rows dial."""
    out = {}
    for i, (b, ports) in enumerate(sorted(backend_ports().items())):
        ips = [f"10.244.9.{i + 1}", f"10.244.8.{i + 1}"]
        svc_ports = [{"name": f"p{p}", "port": p, "targetPort": p + 1000} for p in sorted(ports)]
        eps_ports = [{"name": f"p{p}", "port": p + 1000} for p in sorted(ports)]
        cluster_ip = f"10.96.9.{i + 1}"
        if b == "netpol-canary/netpol-echo-pod":
            cluster_ip = "None"
        if live_like and b == "default/kubernetes":
            ips = ["192.168.0.240", "192.168.0.241", "192.168.0.242"]
            svc_ports, eps_ports = [{"name": "https", "port": 443, "targetPort": 6443}], [{"name": "https", "port": 6443}]
        if b.startswith("netpol-canary/"):
            ips = [ECHO_IP]
            svc_ports = [{"name": "open", "port": 9376, "targetPort": "open"}, {"name": "closed", "port": 9377, "targetPort": "closed"}]
            eps_ports = [{"name": "closed", "port": 9377}, {"name": "open", "port": 9376}]
        svc = {"spec": {"clusterIP": cluster_ip, "ports": svc_ports}}
        eps = {"items": [{"ports": eps_ports, "endpoints": [{"addresses": [ip]} for ip in ips]}]}
        out[b] = (svc, eps)
    return out


def resolve(objs: dict, backend: str, port: int) -> list[str]:
    """The post-DNAT IP:PORTs of a dial to BACKEND on PORT, as the test expects them."""
    svc, eps = objs[backend]
    ips = [e["addresses"][0] for s in eps["items"] for e in s["endpoints"]]
    if svc["spec"]["clusterIP"] == "None":
        return [f"{ip}:{port}" for ip in ips]
    (name,) = [p["name"] for p in svc["spec"]["ports"] if p["port"] == port]
    (tp,) = [p["port"] for p in eps["items"][0]["ports"] if p["name"] == name]
    return [f"{ip}:{tp}" for ip in ips]


def clock(offset_s: int = 0) -> str:
    """The pod's clock reading canary_exec prints, "MMDD HH:MM:SS" (UTC), OFFSET_S from now."""
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).strftime("%m%d %H:%M:%S")


def enforced_table(node: str, rows: list[list[str]], fail_refused: str | None = None, clocks: bool = True) -> str:
    """What canary's exec prints: the pod's clock (T0), `probe.sh once enforced`'s table when every
    row passes (or with one dropped row refused), the clock again (T1). The window opens 2 s ago."""
    lines, npass = ([f"T0 {clock(-2)}"] if clocks else []), 0
    for expect, _scope, host, port, _backend, *note in rows:
        got = "timeout" if expect == "dropped" else "open"
        verdict = "PASS"
        if f"{host}:{port}" == fail_refused:
            got, verdict = "refused", "FAIL"
        npass += verdict == "PASS"
        lines.append(f"{verdict} {expect} {got} {host}:{port} {' '.join(note)}".rstrip())
    lines.append(f"SUMMARY {npass}/{len(rows)} enforced node={node}")
    if clocks:
        lines.append(f"T1 {clock(90)}")
    return "\n".join(lines) + "\n"


def klog_now(ago_s: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=ago_s)).strftime("I%m%d %H:%M:%S.%f")


def deny_line(src_pod: str, dst: str, dst_pod: str = "external", direction: str = "egress",
              src: str = "10.244.3.9:40000", ago_s: float = 0) -> str:
    return (f'{klog_now(ago_s)}       1 verdictlog.go:172] "Packet verdict" id=7 verdict="deny" family="IPv4" '
            f'protocol="TCP" src="{src}" dst="{dst}" srcPod="{src_pod}" dstPod="{dst_pod}" '
            f'direction="{direction}" evaluator="StandardNetworkPolicy"')


# One dropped row to the next, live (2026-11-06): 3.020 to 3.073 s, the pair 3.022 s.
ROW_GAP = 3.022


def deny_lines(pod_name: str, rows: list[list[str]], objs: dict, skip: str | None = None, ago_s: float = 0,
               shift: dict[str, float] | None = None, port_base: int = 41000, src_pod: str | None = None,
               src_ip: str | None = None) -> list[str]:
    """Each dropped row's attempt as the enforcer logs it: one source port per row, three SYNs (at +0,
    +1 and +3 s) to the literal destination or to the backend's post-DNAT IP:PORTs in turn
    (kube-proxy picks a backend per SYN); ingress at the echo for the 9377 row, egress otherwise.
    The rows run in order, ROW_GAP apart, the first one 1 s ago (plus AGO_S). shift: host:port ->
    seconds later for that row's attempt. src_pod/src_ip: the source (default the canary probe)."""
    out, j = [], 0
    for i, (expect, _scope, host, port, backend, *_) in enumerate(rows):
        if expect != "dropped":
            continue
        start = ago_s + 1 - j * ROW_GAP - (shift or {}).get(f"{host}:{port}", 0)
        j += 1
        if f"{host}:{port}" == skip:
            continue
        dsts = [f"{host}:{port}"] if backend == "-" else resolve(objs, backend, int(port))
        src = f"{src_ip or PROBE_IP.get(pod_name.removeprefix('netpol-probe-'), '10.244.3.9')}:{port_base + i}"
        who = src_pod or f"netpol-canary/{pod_name}"
        for syn, off in enumerate((0, 1, 3)):
            dst = dsts[syn % len(dsts)]
            if f"{host}:{port}" == ECHO_9377:
                out.append(deny_line(who, dst, dst_pod=f"netpol-canary/{ECHO_POD}",
                                     direction="ingress", src=src, ago_s=start - off))
            else:
                out.append(deny_line(who, dst, src=src, ago_s=start - off))
    return out


def write_backends(f: Path, objs: dict) -> None:
    (f / "svc").mkdir(exist_ok=True)
    (f / "eps").mkdir(exist_ok=True)
    for b, (svc, eps) in objs.items():
        ns, name = b.split("/")
        (f / "svc" / f"{ns}_{name}").write_text(json.dumps(svc))
        (f / "eps" / f"{ns}_{name}").write_text(json.dumps(eps))


def k8s_pod(ref: str, ip: str, start: str = OLD, phase: str = "Running", host_network: bool = False,
            ready_to_start: str | None = None, ready_to_start_status: str = "True") -> dict:
    """A pod as `kubectl get pods -A` lists it. ready_to_start: its PodReadyToStartContainers
    condition last changed then, to ready_to_start_status (True: the sandbox, and so the IP, came up)."""
    ns, name = ref.split("/")
    status = {"podIP": ip, "startTime": start, "phase": phase}
    if ready_to_start is not None:
        status["conditions"] = [{"type": "PodReadyToStartContainers", "status": ready_to_start_status,
                                 "lastTransitionTime": ready_to_start}]
    return {"metadata": {"namespace": ns, "name": name}, "spec": {"hostNetwork": host_network}, "status": status}


def write_pods(f: Path, pods: list[dict]) -> None:
    """Every pod now, as `kubectl get pods -A` answers."""
    (f / "allpods.json").write_text(json.dumps({"items": pods}))


def canary_fixtures(tmp: Path, *, nodes=NODES, tables=None, rcs=None, extra_log=(), skip=None, np_items=POLICED,
                    live_like=False, drop_lines=(), rerun_log=None, read_logs=None) -> Path:
    """Probe pods on NODES answering enforced runs, three agents whose logs hold the deny lines,
    each backend's Service and EndpointSlices, and the pods now. tables/rcs: pod -> override; skip:
    (pod, host:port) not logged; drop_lines: substrings whose deny lines are removed; rerun_log: what
    the agents' logs say from the second read on (after a re-run); read_logs: n -> what they say at
    read n (from 1), before rerun_log."""
    fixtures(tmp, ds_doc=ds(3, 3), pods=agents(3), np_items=np_items)
    f = tmp / "fx"
    objs = backends(live_like)
    write_backends(f, objs)
    write_pods(f, [k8s_pod(f"netpol-canary/netpol-probe-{n}", PROBE_IP[n]) for n in nodes]
               + [k8s_pod(f"netpol-canary/{ECHO_POD}", ECHO_IP)])
    (f / "probe-pods.json").write_text(json.dumps({"items": [probe_pod(f"netpol-probe-{n}", n) for n in nodes]}))
    (f / "exec").mkdir()
    lines = []
    for n in nodes:
        name, rows = f"netpol-probe-{n}", expand(n)
        (f / "exec" / name).write_text((tables or {}).get(name, enforced_table(n, rows)))
        (f / "exec" / f"{name}.rc").write_text(str((rcs or {}).get(name, 0)))
        lines += deny_lines(name, rows, objs, skip=skip[1] if skip and skip[0] == name else None)
    lines = [x for x in lines if not any(s in x for s in drop_lines)] + list(extra_log)
    # Spread over the three agents: canary reads every agent's log.
    for i, a in enumerate(agents(3)):
        (f / "logs" / a["metadata"]["name"]).write_text(LOG_START + "".join(x + "\n" for x in lines[i::3]))
        if rerun_log is not None:
            (f / "logs" / f"{a['metadata']['name']}.rerun").write_text(LOG_START + "".join(x + "\n" for x in rerun_log[i::3]))
        for n, logged in (read_logs or {}).items():
            (f / "logs" / f"{a['metadata']['name']}.read{n}").write_text(LOG_START + "".join(x + "\n" for x in logged[i::3]))
    return tmp


def run(tmp: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(PATH=f"{tmp / 'bin'}:{env['PATH']}", NETPOL_FIXTURES=str(tmp / "fx"),
               KUBECONFIG=str(tmp / "no-such-kubeconfig"))
    return subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=120)


def dropped_count(node: str) -> int:
    return sum(r[0] == "dropped" for r in expand(node))


@pytest.mark.parametrize("live_like", [False, True])
def test_canary_passes(tmp_path, live_like):
    r = run(canary_fixtures(tmp_path, live_like=live_like), "canary")
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
    t = canary_fixtures(tmp_path, skip=("netpol-probe-top", "10.96.0.1:443"),
                        extra_log=[deny_line("netpol-canary/netpol-probe-top", "10.96.0.1:443")])
    r = run(t, "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "NOT-LOGGED 10.96.0.1:443" in r.stdout


def test_canary_backend_row_needs_its_target_port(tmp_path):
    """The right endpoint IP on another port is some other row's packet (review I1): a backend row
    matches IP:PORT, the Service port's target, port anchored."""
    objs = backends()
    (ip_port,) = resolve(objs, "forgejo/forgejo-http", 3000)[:1]
    ip = ip_port.rsplit(":", 1)[0]
    t = canary_fixtures(tmp_path, skip=("netpol-probe-top", "forgejo-http.forgejo.svc.cluster.local:3000"),
                        extra_log=[deny_line("netpol-canary/netpol-probe-top", f"{ip}:9999")])
    r = run(t, "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    (line,) = [x for x in r.stdout.splitlines() if x.startswith("NOT-LOGGED ")]
    assert line.startswith("NOT-LOGGED forgejo-http.forgejo.svc.cluster.local:3000 (no egress deny of its own from "
                           "netpol-canary/netpol-probe-top to forgejo/forgejo-http: "), line
    assert ip_port in line and f"{ip}:9999" not in line


def test_canary_clusterip_row_not_satisfied_by_node_ip_rows(tmp_path):
    """Live shapes (review I1): default/kubernetes is the three node IPs, which the $HOST_IP,
    $PEER_IP and .240 rows also dial. With no deny line for top's 10.96.0.1:443 row, those rows'
    lines (other ports) must not stand in for it."""
    r = run(canary_fixtures(tmp_path, live_like=True, skip=("netpol-probe-top", "10.96.0.1:443")), "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    n, d = len(expand("top")), dropped_count("top")
    assert f"top rows {n}/{n} deny-logged {d - 1}/{d}" in r.stdout
    assert "NOT-LOGGED 10.96.0.1:443" in r.stdout


def test_canary_node_ip_row_not_satisfied_by_the_clusterip_row(tmp_path):
    """The counterpart (review I1): 10.96.0.1:443 can land on 192.168.0.240:6443, the literal row's
    own destination. With no deny line from the literal row's attempt, the ClusterIP row's lines
    cannot prove both rows: each needs an attempt (a source port) of its own."""
    r = run(canary_fixtures(tmp_path, live_like=True, skip=("netpol-probe-bottom", "192.168.0.240:6443")), "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    n, d = len(expand("bottom")), dropped_count("bottom")
    assert f"bottom rows {n}/{n} deny-logged {d - 1}/{d}" in r.stdout
    assert "canary: PASS" not in r.stdout


def test_canary_clusterip_row_all_syns_to_the_same_node_is_paired(tmp_path):
    """kube-proxy may send all three SYNs of the ClusterIP attempt to .240, the literal row's own
    destination (1 in 27). Before review round 3 that read AMBIGUOUS, and canary ran the pod again.
    With no other run in the window, the two attempts sit one row timeout apart in the probe's
    order, so the earlier is the ClusterIP row's (R3-1): PASS, no re-run."""
    objs = backends(True)
    lines = []
    for n in NODES:
        rows = expand(n)
        lines += [x.replace("192.168.0.241:6443", "192.168.0.240:6443").replace("192.168.0.242:6443", "192.168.0.240:6443")
                  for x in deny_lines(f"netpol-probe-{n}", rows, objs)]
    t = canary_fixtures(tmp_path, live_like=True, drop_lines=("Packet verdict",), extra_log=lines)
    r = run(t, "canary")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RERUN" not in r.stdout and "AMBIGUOUS" not in r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == "canary: PASS (3 nodes)"


def test_canary_echo_9377_not_satisfied_by_9378(tmp_path):
    """Live shapes (review I1): both echo Services are the one pod IP. With no deny line for
    middle's 9377 row, the 9378 row's line must not stand in for it."""
    r = run(canary_fixtures(tmp_path, live_like=True, skip=("netpol-probe-middle", ECHO_9377)), "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    n, d = len(expand("middle")), dropped_count("middle")
    assert f"middle rows {n}/{n} deny-logged {d - 1}/{d}" in r.stdout
    assert f"NOT-LOGGED {ECHO_9377}" in r.stdout


def test_canary_echo_9377_must_be_an_ingress_deny(tmp_path):
    """§9.2: the 9377 drop proves the echo's ingress default-deny. An egress deny on the right
    five-tuple would mean the probe's egress dropped it, which the policies say cannot happen."""
    rows = expand("bottom")
    (dst,) = resolve(backends(True), "netpol-canary/netpol-echo", 9377)
    t = canary_fixtures(tmp_path, live_like=True, skip=("netpol-probe-bottom", ECHO_9377),
                        extra_log=[deny_line("netpol-canary/netpol-probe-bottom", dst, direction="egress")])
    r = run(t, "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"NOT-LOGGED {ECHO_9377} (no ingress deny" in r.stdout
    assert any(f"{r_[2]}:{r_[3]}" == ECHO_9377 for r_ in rows)


def test_canary_echo_9378_must_be_an_egress_deny(tmp_path):
    """The 9378 row isolates the probe's egress (option a): an ingress deny there means the echo's
    ingress dropped it, so the probe's egress let it through."""
    (dst,) = resolve(backends(True), "netpol-canary/netpol-echo-pod", 9378)
    assert dst == "10.244.0.201:9378", "headless: no DNAT, the row's own port"
    t = canary_fixtures(tmp_path, live_like=True, skip=("netpol-probe-top", ECHO_9378),
                        extra_log=[deny_line("netpol-canary/netpol-probe-top", dst, dst_pod=f"netpol-canary/{ECHO_POD}",
                                             direction="ingress")])
    r = run(t, "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"NOT-LOGGED {ECHO_9378} (no egress deny" in r.stdout


def test_canary_ignores_lines_before_its_run(tmp_path):
    """A deny line from before this run (an earlier loop, a pod since deleted) proves nothing about it."""
    wan = f"{WAN_IP}:8585"
    t = canary_fixtures(tmp_path, skip=("netpol-probe-top", wan),
                        extra_log=[deny_line("netpol-canary/netpol-probe-top", wan, ago_s=600)])
    r = run(t, "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"NOT-LOGGED {wan}" in r.stdout


def test_row_directions_name_matrix_rows():
    """ROW_INGRESS in netpol-verify.sh names canary rows that exist and are must-drop: exactly the
    9377 echo row. Every other dropped row, 9378 included, wants an egress deny."""
    m = re.search(r"^ROW_INGRESS=\(([^)]*)\)$", SCRIPT.read_text(), re.M)
    assert m, "netpol-verify.sh must define ROW_INGRESS=(...) on one line"
    keys = m.group(1).split()
    assert keys == [ECHO_9377]
    dropped = {f"{r[2]}:{r[3]}" for r in expand("top") if r[0] == "dropped"}
    assert set(keys) <= dropped and ECHO_9378 in dropped


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


STRAY_PODS = [k8s_pod("netpol-canary/netpol-probe-x", "10.244.3.9"), k8s_pod("netpol-canary/netpol-echo-y", "10.244.3.7"),
              k8s_pod("netpol-canary/p", "10.244.3.5")]


def stray_fixtures(tmp: Path, lines: list[str], np_items=POLICED, pods_now=STRAY_PODS, agent_pods=None,
                   logs=None, previous=None) -> Path:
    """Agents, their logs (the deny lines go to the first agent), and the pods now.
    agent_pods/logs/previous: override the agents, a log per agent, a previous container's log."""
    ap = agent_pods or agents(3)
    fixtures(tmp, ds_doc=ds(3, 3), pods=ap, np_items=np_items)
    write_pods(tmp / "fx", list(pods_now))
    for i, a in enumerate(ap):
        name = a["metadata"]["name"]
        text = (logs or {}).get(name, LOG_START + ("".join(x + "\n" for x in lines) if i == 0 else ""))
        (tmp / "fx" / "logs" / name).write_text(text)
        if previous and name in previous:
            (tmp / "fx" / "logs" / f"{name}.previous").write_text(previous[name])
    return tmp


def test_stray_passes(tmp_path):
    lines = [deny_line("netpol-canary/netpol-probe-x", "192.168.0.2:80"),
             # An ingress denial: the source is outside, the destination is a policed pod.
             deny_line("external", "10.244.3.7:9377", dst_pod="netpol-canary/netpol-echo-y", direction="ingress")]
    r = run(stray_fixtures(tmp_path, lines), "stray")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.rstrip().splitlines()[-1] == "stray: PASS (2 deny lines, policed: netpol-canary)"
    assert "--since=15m" in (tmp_path / "fx" / "calls.log").read_text()
    assert sum(line.startswith("window ") and ": covered, log from " in line for line in r.stdout.splitlines()) == 3


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
    assert "STRAY [no pod in a policed namespace] " in r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == "stray: FAIL (1 of 2 deny lines stray, policed: netpol-canary)"


def test_stray_deleted_job_pod_is_gone_not_stray(tmp_path):
    """A Job pod deleted when it finished (ci-tiny, ttlSecondsAfterFinished: 0), its IP unused since:
    its deny lines are GONE, information only (review round 2, N1)."""
    lines = [deny_line("netpol-canary/ci-job-abc", "192.168.0.2:80", src="10.244.4.77:40000", ago_s=600)]
    r = run(stray_fixtures(tmp_path, lines), "stray")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "GONE [netpol-canary/ci-job-abc is gone] " in r.stdout
    assert "STRAY " not in r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == "stray: PASS (1 deny lines, 1 from pods since gone or moved, policed: netpol-canary)"


def test_stray_ip_reused_by_an_earlier_pod_is_stray(tmp_path):
    """The line names a deleted pod, but its IP belongs to a media pod that started before the line:
    at the time, the IP was already the media pod's. A stale IP-to-pod mapping."""
    lines = [deny_line("netpol-canary/ci-job-abc", "192.168.0.2:80", src="10.244.4.77:40000", ago_s=600)]
    pods = STRAY_PODS + [k8s_pod("media/mm-plex-0", "10.244.4.77", start=iso_ago(3600))]
    r = run(stray_fixtures(tmp_path, lines, pods_now=pods), "stray")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STRAY [netpol-canary/ci-job-abc named with ip 10.244.4.77, which media/mm-plex-0 has held since " in r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == "stray: FAIL (1 of 1 deny lines stray, policed: netpol-canary)"


def test_stray_ip_reused_by_a_later_pod_is_gone(tmp_path):
    """The deleted pod's IP went to a pod that started after the line: the mapping was right then."""
    lines = [deny_line("netpol-canary/ci-job-abc", "192.168.0.2:80", src="10.244.4.77:40000", ago_s=600)]
    pods = STRAY_PODS + [k8s_pod("media/mm-plex-0", "10.244.4.77", start=iso_ago(300))]
    r = run(stray_fixtures(tmp_path, lines, pods_now=pods), "stray")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "GONE [netpol-canary/ci-job-abc is gone] " in r.stdout


def test_stray_holder_since_is_when_it_got_its_ip(tmp_path):
    """Review round 3, R3-2: a pod whose sandbox the kubelet recreated (a node reboot) keeps its old
    startTime, but got its IP when PodReadyToStartContainers last turned True. That was after the
    line, so the deleted pod the line names had the IP then: GONE, not STRAY."""
    lines = [deny_line("netpol-canary/ci-job-abc", "192.168.0.2:80", src="10.244.4.77:40000", ago_s=600)]
    holder = k8s_pod("media/mm-plex-0", "10.244.4.77", start=iso_ago(86400), ready_to_start=iso_ago(300))
    r = run(stray_fixtures(tmp_path, lines, pods_now=STRAY_PODS + [holder]), "stray")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "GONE [netpol-canary/ci-job-abc is gone] " in r.stdout


@pytest.mark.parametrize("ready_to_start", [None, 3600])
def test_stray_holder_since_falls_back_or_holds(tmp_path, ready_to_start):
    """Without the condition, startTime stands in; with it turned True before the line, the IP was
    the holder's already. Either way the holder had the IP before the line: STRAY."""
    lines = [deny_line("netpol-canary/ci-job-abc", "192.168.0.2:80", src="10.244.4.77:40000", ago_s=600)]
    holder = k8s_pod("media/mm-plex-0", "10.244.4.77", start=iso_ago(86400),
                     ready_to_start=None if ready_to_start is None else iso_ago(ready_to_start))
    r = run(stray_fixtures(tmp_path, lines, pods_now=STRAY_PODS + [holder]), "stray")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "which media/mm-plex-0 has held since" in r.stdout


def test_stray_holder_since_ignores_a_false_condition(tmp_path):
    """Review of the pre-work, M1: a PodReadyToStartContainers condition that is False (no sandbox now)
    says nothing of when the IP came; startTime stands in. Here that is before the line: STRAY."""
    lines = [deny_line("netpol-canary/ci-job-abc", "192.168.0.2:80", src="10.244.4.77:40000", ago_s=600)]
    holder = k8s_pod("media/mm-plex-0", "10.244.4.77", start=iso_ago(86400), ready_to_start=iso_ago(300),
                     ready_to_start_status="False")
    r = run(stray_fixtures(tmp_path, lines, pods_now=STRAY_PODS + [holder]), "stray")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "which media/mm-plex-0 has held since" in r.stdout


def test_stray_finished_pod_does_not_hold_its_ip(tmp_path):
    """A Succeeded pod keeps podIP in its status after the IP is free: it is not the IP's holder."""
    lines = [deny_line("netpol-canary/ci-job-abc", "192.168.0.2:80", src="10.244.4.77:40000", ago_s=600)]
    pods = STRAY_PODS + [k8s_pod("media/old-job", "10.244.4.77", start=iso_ago(3600), phase="Succeeded")]
    r = run(stray_fixtures(tmp_path, lines, pods_now=pods), "stray")
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("holder_start,stray", [(3600, True), (300, False), (None, False)])
def test_stray_pod_that_moved_ip(tmp_path, holder_start, stray):
    """A DaemonSet pod keeps its name across a node reboot and gets a new IP. Its old lines name the
    old IP: STRAY only if another pod held that IP before the line, else GONE."""
    lines = [deny_line("netpol-canary/netpol-probe-x", "192.168.0.2:80", src="10.244.3.66:40000", ago_s=600)]
    pods = list(STRAY_PODS)
    if holder_start is not None:
        pods.append(k8s_pod("media/mm-plex-0", "10.244.3.66", start=iso_ago(holder_start)))
    r = run(stray_fixtures(tmp_path, lines, pods_now=pods), "stray")
    if stray:
        assert r.returncode == 1, r.stdout + r.stderr
        assert "which media/mm-plex-0 has held since" in r.stdout
    else:
        assert r.returncode == 0, r.stdout + r.stderr
        assert "GONE [netpol-canary/netpol-probe-x now has ip 10.244.3.9] " in r.stdout


def test_stray_node_ip_on_policed_side_fails(tmp_path):
    """A policed pod named with a node IP (a hostNetwork pod enrolled, the #380 class)."""
    lines = [deny_line("netpol-canary/netpol-probe-x", "192.168.0.2:80", src="192.168.0.241:41000"),
             deny_line("external", "192.168.0.242:9376", dst_pod="netpol-canary/netpol-echo-y", direction="ingress")]
    r = run(stray_fixtures(tmp_path, lines), "stray")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "STRAY [netpol-canary/netpol-probe-x has ip 192.168.0.241, outside 10.244.0.0/16] " in r.stdout
    assert "STRAY [netpol-canary/netpol-echo-y has ip 192.168.0.242, outside 10.244.0.0/16] " in r.stdout


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


def log_from(ago_s: int) -> str:
    """An agent log whose first line is AGO_S seconds old."""
    return f'{klog_now(ago_s)}       1 main.go:62] "called" args=[]\n{klog_now(ago_s)}       1 controller.go:505] "Syncing nftables rules"\n'


def test_stray_short_window_fails(tmp_path):
    """An agent that started 5 minutes ago with no earlier container (the DaemonSet replaced the pod):
    its log covers 300 s of the 900 s asked (review M3). Silence there is not a pass."""
    ap = agents(3)
    ap[1] = pod("kube-network-policies-1", NODES[1], started=iso_ago(300))
    r = run(stray_fixtures(tmp_path, [], agent_pods=ap, logs={"kube-network-policies-1": log_from(300)}), "stray")
    assert r.returncode == 1, r.stdout + r.stderr
    assert re.search(r"^window top kube-network-policies-1: SHORT, 3\d\d s of 900 s ", r.stdout, re.M), r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == "stray: FAIL (0 of 0 deny lines stray, 1 agent logs short of 15m, policed: netpol-canary)"


def test_stray_rotated_log_fails(tmp_path):
    """The container has run 2 h, but its current log file starts 5 min ago (kubelet rotated it)."""
    ap = agents(3)
    ap[2] = pod("kube-network-policies-2", NODES[2], started=iso_ago(7200))
    r = run(stray_fixtures(tmp_path, [], agent_pods=ap, logs={"kube-network-policies-2": log_from(300)}), "stray")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "window middle kube-network-policies-2: SHORT" in r.stdout


def test_stray_reads_the_previous_container(tmp_path):
    """The agent restarted in place 5 min ago: the previous container's log covers the rest of the
    window, and a stray line there counts."""
    ap = agents(3)
    ap[0] = pod("kube-network-policies-0", NODES[0], restarts=1, started=iso_ago(300))
    prev = log_from(7200) + deny_line("media/mm-plex-0", "192.168.0.1:53", ago_s=600) + "\n"
    t = stray_fixtures(tmp_path, [], agent_pods=ap, logs={"kube-network-policies-0": log_from(300)},
                       previous={"kube-network-policies-0": prev})
    r = run(t, "stray")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "window bottom kube-network-policies-0: covered, log from " in r.stdout
    assert "STRAY [no pod in a policed namespace] " in r.stdout and "media/mm-plex-0" in r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == "stray: FAIL (1 of 1 deny lines stray, policed: netpol-canary)"
    assert "--previous" in (tmp_path / "fx" / "calls.log").read_text()


@pytest.mark.parametrize("args", [["stray", "--since"], ["stray", "15m"], ["canary", "--node"], ["canary", "top"],
                                  ["idle", "x"], ["alert", "x"]])
def test_bad_arguments_are_exit_2(tmp_path, args):
    r = run(stray_fixtures(tmp_path, []), *args)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "usage:" in r.stderr


def test_concurrent_loop_run_is_ambiguous_never_pass(tmp_path):
    """Review round 2, probe 3: the ClusterIP path never logs, in the exec run or in the pod's own loop
    run, but the loop run overlaps canary's run, so its literal .240:6443 attempt (another source port)
    is in the window. Two literal attempts must not prove both rows: AMBIGUOUS, a second run, and
    exit 2 when that is the same."""
    loop = [deny_line("netpol-canary/netpol-probe-top", "192.168.0.240:6443", src=f"{PROBE_IP['top']}:50001")
            for _ in range(3)]
    r = run(canary_fixtures(tmp_path, live_like=True, skip=("netpol-probe-top", "10.96.0.1:443"), extra_log=loop), "canary")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "canary: PASS" not in r.stdout
    for k in (1, 2, 3):
        assert f"RERUN top netpol-probe-top ({k} of 3): overlapping rows ambiguous, one more run" in r.stdout
    assert "AMBIGUOUS 10.96.0.1:443 (egress to default/kubernetes: " in r.stdout
    n, d = len(expand("top")), dropped_count("top")
    assert f"top rows {n}/{n} deny-logged {d - 1}/{d} ambiguous 1" in r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == "canary: AMBIGUOUS (1 of 3 nodes still ambiguous after 4 runs; run it again)"


def test_overlapping_loop_run_is_rerun_and_then_passes(tmp_path):
    """The pod's loop run overlaps canary's run and both are logged: the overlapping pair shows four
    attempts for two rows, another run in the window. One more run, with a fresh window, settles it."""
    objs = backends(True)
    normal = [x for n in NODES for x in deny_lines(f"netpol-probe-{n}", expand(n), objs)]
    loop = [x.replace(f'src="{PROBE_IP["middle"]}:41', f'src="{PROBE_IP["middle"]}:51')
            for x in deny_lines("netpol-probe-middle", expand("middle"), objs) if ":6443" in x]
    r = run(canary_fixtures(tmp_path, live_like=True, extra_log=loop, rerun_log=normal), "canary")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RERUN middle netpol-probe-middle (1 of 3): overlapping rows ambiguous, one more run" in r.stdout
    assert "(2 of 3)" not in r.stdout
    assert "4 attempts for 2 overlapping rows: another run is in the window" in r.stdout
    n, d = len(expand("middle")), dropped_count("middle")
    assert f"middle rows {n}/{n} deny-logged {d}/{d}" in r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == "canary: PASS (3 nodes)"


def test_loop_run_before_the_window_is_excluded(tmp_path):
    """The pod's previous loop run began before canary's run did (T0, on the pod's clock): its attempts
    are outside the window and cannot stand in for this run's rows."""
    loop = [deny_line("netpol-canary/netpol-probe-top", "192.168.0.240:6443", src=f"{PROBE_IP['top']}:50001", ago_s=10)
            for _ in range(3)]
    r = run(canary_fixtures(tmp_path, live_like=True, skip=("netpol-probe-top", "10.96.0.1:443"), extra_log=loop), "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "NOT-LOGGED 10.96.0.1:443" in r.stdout
    assert "RERUN" not in r.stdout


def test_shared_source_port_between_overlapping_rows_is_a_fail_never_a_pass(tmp_path):
    """Review round 2, probe 4: the kernel may give the ClusterIP attempt and the literal .240:6443
    attempt the same source port (about 1 in 14-28 k). The merged attempt reached .241 too, so it is
    not the literal row's: a false FAIL, never a PASS."""
    objs = backends(True)
    lines = []
    for n in NODES:
        for x in deny_lines(f"netpol-probe-{n}", expand(n), objs):
            if n == "top" and ":6443" in x and "192.168.0.5:" not in x:
                x = re.sub(r'src="[0-9.]+:[0-9]+"', f'src="{PROBE_IP["top"]}:55555"', x)
            lines.append(x)
    r = run(canary_fixtures(tmp_path, live_like=True, drop_lines=("Packet verdict",), extra_log=lines), "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "NOT-LOGGED 192.168.0.240:6443" in r.stdout
    assert "canary: PASS" not in r.stdout


def test_overlap_on_two_runs_resolves_on_the_third(tmp_path):
    """Review round 3, R3-1: the pod's loop run overlapped canary's first run and its re-run (both
    windows show its attempts), which used to end AMBIGUOUS with exit 2. Up to three re-runs: the
    third run is clean, so canary PASSes."""
    objs = backends(True)
    normal = [x for n in NODES for x in deny_lines(f"netpol-probe-{n}", expand(n), objs)]
    loop = deny_lines("netpol-probe-middle", expand("middle"), objs, ago_s=-10.5, port_base=51000)
    r = run(canary_fixtures(tmp_path, live_like=True, extra_log=loop, read_logs={1: normal + loop, 2: normal}), "canary")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RERUN middle netpol-probe-middle (1 of 3)" in r.stdout and "RERUN middle netpol-probe-middle (2 of 3)" in r.stdout
    assert "(3 of 3)" not in r.stdout
    n, d = len(expand("middle")), dropped_count("middle")
    assert f"middle rows {n}/{n} deny-logged {d}/{d}" in r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == "canary: PASS (3 nodes)"


def test_one_row_offset_run_cannot_stand_in_for_a_clusterip_defect(tmp_path):
    """The ClusterIP path never logs (a real defect), and another run sits exactly one row ahead, so
    its literal .240:6443 attempt lands one row timeout before this run's: two all-.240 attempts
    3.022 s apart, the shape pairing accepts. The other run also adds attempts to every other row,
    so pairing stays off: AMBIGUOUS while it lasts, and NOT-LOGGED once a run is clean. Never PASS."""
    objs = backends(True)
    defect = [x for n in NODES for x in deny_lines(f"netpol-probe-{n}", expand(n), objs,
                                                    skip="10.96.0.1:443" if n == "top" else None)]
    ahead = deny_lines("netpol-probe-top", expand("top"), objs, skip="10.96.0.1:443", ago_s=ROW_GAP, port_base=51000)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    # Only the overlapped runs: AMBIGUOUS after four runs, exit 2.
    r = run(canary_fixtures(tmp_path / "a", live_like=True, drop_lines=("Packet verdict",), extra_log=defect + ahead), "canary")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "canary: PASS" not in r.stdout
    assert "top rows 25/25 deny-logged 20/21 ambiguous 1" in r.stdout
    # The re-run is clean: NOT-LOGGED, exit 1.
    r = run(canary_fixtures(tmp_path / "b", live_like=True, drop_lines=("Packet verdict",), extra_log=defect + ahead,
                            read_logs={1: defect}), "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "RERUN top netpol-probe-top (1 of 3)" in r.stdout
    assert "NOT-LOGGED 10.96.0.1:443" in r.stdout


def test_a_clusterip_defect_still_fails_without_an_overlap(tmp_path):
    """No other run in the window and no ClusterIP attempt: one attempt for two rows. Pairing needs
    one per row, so the ClusterIP row is NOT-LOGGED on the first run."""
    r = run(canary_fixtures(tmp_path, live_like=True, skip=("netpol-probe-bottom", "10.96.0.1:443")), "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "NOT-LOGGED 10.96.0.1:443" in r.stdout and "RERUN" not in r.stdout


def test_pairing_needs_the_row_timeout_spacing(tmp_path):
    """Two all-.240 attempts 1 s apart are not one run's two rows: no pairing, AMBIGUOUS."""
    objs = backends(True)
    lines = []
    for n in NODES:
        rows = expand(n)
        shift = {"10.96.0.1:443": 2.0} if n == "top" else None
        lines += [x.replace("192.168.0.241:6443", "192.168.0.240:6443").replace("192.168.0.242:6443", "192.168.0.240:6443")
                  if n == "top" else x for x in deny_lines(f"netpol-probe-{n}", rows, objs, shift=shift)]
    r = run(canary_fixtures(tmp_path, live_like=True, drop_lines=("Packet verdict",), extra_log=lines), "canary")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "AMBIGUOUS 10.96.0.1:443" in r.stdout
    assert "canary: PASS" not in r.stdout


def test_canary_needs_the_pods_clock(tmp_path):
    """Without the T0/T1 readings there is no window to judge the run's own lines by: a FAIL."""
    table = enforced_table("top", expand("top"), clocks=False)
    r = run(canary_fixtures(tmp_path, tables={"netpol-probe-top": table}), "canary")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "order=the pod printed no T0/T1 clock reading" in r.stdout


# --- idle: the nftables sync-error check (review M1) ---------------------------

SYNC_ERROR = 'I1105 22:00:00.000000       1 controller.go:976] "syncing nftables rules" logger="nftables-sync" error="netlink receive: no such file or directory"'


@pytest.mark.parametrize("line", [
    SYNC_ERROR,                                                   # upstream's own shape (controller.go:976, v1.1.3)
    SYNC_ERROR.replace('"syncing nftables', '"Syncing nftables'),  # the case the start/elapsed lines use
])
def test_idle_nftables_sync_error_fails(tmp_path, line):
    r = idle(fixtures(tmp_path, ds_doc=ds(1, 1), pods=agents(1), log=LOG_START + line + "\n"))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "nftables-sync-error" in r.stdout


def test_idle_nftables_sync_error_on_a_long_log_fails(tmp_path):
    """The error first, then 20 000 sync lines: a `| grep -q` would end early and SIGPIPE the grep
    before it, and the pipeline would read as no error."""
    ok_line = 'I1105 22:00:01.000000       1 controller.go:508] "Syncing nftables rules" logger="nftables-sync" elapsed="1.1ms"\n'
    r = idle(fixtures(tmp_path, ds_doc=ds(1, 1), pods=agents(1), log=LOG_START + SYNC_ERROR + "\n" + ok_line * 20000))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "nftables-sync-error" in r.stdout


# --- alert: NetpolCanaryFailing loaded, by name (review M5) ---------------------

PR_UID = "20c3e774-98d0-463e-b0af-d2ce96f98526"
_SEL = '{namespace="netpol-canary",daemonset="netpol-probe"}'
# The object's expr, as cluster/netpol-canary/prometheusrule.yaml writes it.
ALERT_EXPR = (f"kube_daemonset_status_number_ready{_SEL} < kube_daemonset_status_desired_number_scheduled{_SEL}"
              f" or absent(kube_daemonset_status_desired_number_scheduled{_SEL})"
              f" or kube_daemonset_status_desired_number_scheduled{_SEL} == 0")
# What Prometheus reports as the rule's query (live, 2026-11-06): matchers sorted.
LOADED = ALERT_EXPR.replace(_SEL, '{daemonset="netpol-probe",namespace="netpol-canary"}')
# The expression before review M5, loaded under the same uid file.
LOADED_OLD = LOADED.rsplit(" or ", 1)[0]


def rule_group(file_uid=PR_UID, health="ok", duration=900, query=LOADED, name="netpol-canary") -> dict:
    return {"name": name, "file": f"/etc/prometheus/rules/prometheus-netpol-canary-{file_uid}.yaml",
            "rules": [{"name": "NetpolCanaryFailing", "health": health, "state": "inactive", "duration": duration,
                       "query": query}]}


def alert_fixtures(tmp: Path, groups=None, uid=PR_UID, pr=True) -> Path:
    fixtures(tmp, ds_doc=None, pods=[])
    f = tmp / "fx"
    if pr:
        (f / "pr.json").write_text(json.dumps({"metadata": {"uid": uid}, "spec": {"groups": [{"name": "netpol-canary", "rules": [
            {"alert": "NetpolCanaryFailing", "for": "15m", "expr": ALERT_EXPR}]}]}}))
    if groups is None:
        groups = [rule_group()]
    (f / "rules.json").write_text(json.dumps({"status": "success", "data": {"groups": groups}}))
    return tmp


def test_alert_passes(tmp_path):
    r = run(alert_fixtures(tmp_path), "alert")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (f"PASS group netpol-canary from prometheus-netpol-canary-{PR_UID}.yaml: NetpolCanaryFailing health=ok "
            "state=inactive for=900s, query matches the object's expr") in r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == "alert: PASS"
    assert "rule_name[]=NetpolCanaryFailing" in (tmp_path / "fx" / "calls.log").read_text()


@pytest.mark.parametrize("groups", [
    [],                                         # not loaded
    [rule_group(file_uid="0000")],              # a stale file
    [rule_group(health="err")],                 # broken
    [rule_group(duration=600)],                 # another for:
    [rule_group(name="other")],                 # another group
    [rule_group(query=LOADED_OLD)],             # the old expression, still loaded (review round 2, N4)
], ids=["missing", "stale-file", "health", "for", "group", "old-query"])
def test_alert_fails(tmp_path, groups):
    r = run(alert_fixtures(tmp_path, groups=groups), "alert")
    assert r.returncode == 1, r.stdout + r.stderr
    assert r.stdout.rstrip().splitlines()[-1] == "alert: FAIL"


def test_alert_without_the_object_is_exit_2(tmp_path):
    r = run(alert_fixtures(tmp_path, pr=False), "alert")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "cannot read PrometheusRule prometheus/netpol-canary" in r.stderr


# --- runner (B2) ------------------------------------------------------------------

RUNNER_POD = "forgejo-runner-5f48b6bcd7-f56hv"
RUNNER_IP = "10.244.4.169"
RUNNER_NODE = "middle"
RUNNER_POLICED = POLICED + [{"metadata": {"name": "default-deny", "namespace": "forgejo-runner"}}]


def expand_runner(node: str = RUNNER_NODE) -> list[list[str]]:
    """The rows the runner dials on NODE: probe_rows over matrix.txt, scope runner (the all rows),
    with the node's host IP, less its own BMC."""
    env = dict(os.environ, NODE_NAME=node, HOST_IP=NODE_IP[node], WAN_IP=WAN_IP)
    r = subprocess.run(["bash", "-c", '. "$1"; probe_rows "$2" runner', "_", str(PROBE), str(MATRIX)],
                       env=env, capture_output=True, text=True, check=True)
    return [line.split() for line in r.stdout.splitlines()]


def epoch_ms(offset_s: float = 0) -> int:
    return int((datetime.now(timezone.utc) + timedelta(seconds=offset_s)).timestamp() * 1000)


def runner_raw(rows: list[list[str]], got: dict[str, str] | None = None) -> str:
    """What runner_exec reads back from the pod: node's clock before the run (T0), one R line per row
    with the row's start (epoch ms), node's clock after (T1). Dropped row j starts ROW_GAP after the
    one before, 50 ms before its first SYN in deny_lines."""
    out, j = [f"T0 {epoch_ms(-2)}"], 0
    for expect, _scope, host, port, *_ in rows:
        mode = "dns" if expect == "dns" else "tcp"
        g = (got or {}).get(f"{host}:{port}", "timeout" if expect == "dropped" else "open")
        start = epoch_ms(-1 + j * ROW_GAP - 0.05) if expect == "dropped" else epoch_ms(-1.5)
        j += expect == "dropped"
        out.append(f"R {mode} {host} {port if mode == 'tcp' else '-'} {g} {start}")
    out.append(f"T1 {epoch_ms(90)}")
    return "\n".join(out) + "\n"


def runner_fixtures(tmp: Path, *, ready: bool = True, raw: str | None = None, lines=None, skip=None,
                    live_like: bool = True, extra_log=()) -> Path:
    fixtures(tmp, ds_doc=ds(3, 3), pods=agents(3), np_items=RUNNER_POLICED)
    f = tmp / "fx"
    objs = backends(live_like)
    write_backends(f, objs)
    write_pods(f, [k8s_pod(f"forgejo-runner/{RUNNER_POD}", RUNNER_IP)])
    pod = {"metadata": {"name": RUNNER_POD}, "spec": {"nodeName": RUNNER_NODE},
           "status": {"phase": "Running", "hostIP": NODE_IP[RUNNER_NODE],
                      "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}
    (f / "runner-pods.json").write_text(json.dumps({"items": [pod]}))
    (f / "exec").mkdir()
    rows = expand_runner()
    (f / "exec" / RUNNER_POD).write_text(raw if raw is not None else runner_raw(rows))
    (f / "exec" / f"{RUNNER_POD}.rc").write_text("0")
    if lines is None:
        lines = deny_lines(RUNNER_POD, rows, objs, skip=skip, src_pod=f"forgejo-runner/{RUNNER_POD}", src_ip=RUNNER_IP)
    lines = list(lines) + list(extra_log)
    for i, a in enumerate(agents(3)):
        (f / "logs" / a["metadata"]["name"]).write_text(LOG_START + "".join(x + "\n" for x in lines[i::3]))
    return tmp


def run_runner(tmp: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PATH=f"{tmp / 'bin'}:{os.environ['PATH']}", NETPOL_FIXTURES=str(tmp / "fx"),
               KUBECONFIG=str(tmp / "no-such-kubeconfig"), NETPOL_WAN_IP=WAN_IP)
    return subprocess.run([str(SCRIPT), "runner"], env=env, capture_output=True, text=True, timeout=120)


def test_runner_passes(tmp_path):
    r = run_runner(runner_fixtures(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    rows = expand_runner()
    n, d = len(rows), sum(x[0] == "dropped" for x in rows)
    assert f"{RUNNER_NODE} rows {n}/{n} deny-logged {d}/{d}" in r.stdout
    assert re.search(r"^stray: PASS \(\d+ deny lines, policed: forgejo-runner,netpol-canary\)$", r.stdout, re.M), r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == f"runner: PASS ({n} rows, {d}/{d} deny-logged)"


def test_runner_row_counts_come_from_the_matrix():
    """On middle the runner dials the all rows less middle's own BMC; $HOST_IP and $PEER_IP stay in
    (Review Focus 1: a pod dialling its own node). The plan's literal "19 rows, 16/16" predates the
    matrix's growth in Task 6."""
    rows = expand_runner()
    all_rows = [r for r in (x.split() for x in MATRIX.read_text().splitlines()) if r and not r[0].startswith("#") and r[1] == "all"]
    assert len(rows) == len(all_rows) - 1
    hosts = {f"{r[2]}:{r[3]}" for r in rows}
    assert "192.168.0.117:443" not in hosts, "middle skips its own BMC"
    assert "192.168.0.241:50000" in hosts and "192.168.0.242:50000" in hosts and "192.168.0.242:10250" in hosts


def test_runner_dials_every_row_in_one_exec(tmp_path):
    """One exec runs every row back to back (review of the pre-work, T8-2); its arguments carry the
    script and the JS, so the call spans several lines of the stub's log."""
    r = run_runner(runner_fixtures(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    calls = (tmp_path / "fx" / "calls.log").read_text()
    assert calls.count(f"-n forgejo-runner exec {RUNNER_POD} -c runner -- sh -c") == 1
    assert "dns kubernetes.default.svc.cluster.local -" in calls and "tcp 192.168.0.241 50000" in calls
    assert "192.168.0.117" not in calls and f"tcp {WAN_IP} 8585" in calls
    assert 'node -e "process.stdout.write(String(Date.now()))"' in calls, "the pod clock times each row"


def test_runner_before_enforcement_fails(tmp_path):
    """Before the policies (Step 4): the dropped rows connect or are refused, and nothing is logged."""
    rows = expand_runner()
    got = {f"{x[2]}:{x[3]}": "open" for x in rows if x[0] == "dropped"}
    r = run_runner(runner_fixtures(tmp_path, raw=runner_raw(rows, got), lines=[]))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL dropped open 192.168.0.1:22" in r.stdout
    assert "runner: FAIL" in r.stdout


def test_runner_unlogged_row_fails(tmp_path):
    r = run_runner(runner_fixtures(tmp_path, skip="192.168.0.2:80"))
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"NOT-LOGGED 192.168.0.2:80 (no egress deny of its own from forgejo-runner/{RUNNER_POD}" in r.stdout


def test_runner_clusterip_all_to_240_is_paired(tmp_path):
    """The runner has no loop of its own, so its one run is alone in the window: pairing settles the
    1-in-27 case."""
    objs = backends(True)
    lines = [x.replace("192.168.0.241:6443", "192.168.0.240:6443").replace("192.168.0.242:6443", "192.168.0.240:6443")
             for x in deny_lines(RUNNER_POD, expand_runner(), objs, src_pod=f"forgejo-runner/{RUNNER_POD}", src_ip=RUNNER_IP)]
    r = run_runner(runner_fixtures(tmp_path, lines=lines))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RERUN" not in r.stdout


def test_runner_not_ready_is_exit_2(tmp_path):
    r = run_runner(runner_fixtures(tmp_path, ready=False))
    assert r.returncode == 2, r.stdout + r.stderr
    assert f"forgejo-runner pod {RUNNER_POD} is Running, Ready=False" in r.stderr


def runner_lines(skip: str | None = None) -> list[str]:
    return deny_lines(RUNNER_POD, expand_runner(), backends(True), skip=skip, src_pod=f"forgejo-runner/{RUNNER_POD}",
                      src_ip=RUNNER_IP)


def runner_other_run(ago_s: float) -> list[str]:
    """Another run of the same rows from the runner pod (a CI job, say), AGO_S earlier."""
    return deny_lines(RUNNER_POD, expand_runner(), backends(True), ago_s=ago_s, port_base=51000,
                      src_pod=f"forgejo-runner/{RUNNER_POD}", src_ip=RUNNER_IP)


def test_runner_other_run_in_the_window_is_timed_out_of_it(tmp_path):
    """Another run's attempts in the window would turn rhythm pairing off. The runner knows each row's
    start on the pod's clock, so the overlapping rows take the attempt in their own slot: PASS."""
    r = run_runner(runner_fixtures(tmp_path, lines=runner_lines() + runner_other_run(-10.5)))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RERUN" not in r.stdout and "AMBIGUOUS" not in r.stdout


def test_runner_two_attempts_in_one_slot_is_ambiguous(tmp_path):
    """Another run 0.3 s off: both attempts fall in the overlapping rows' slots. AMBIGUOUS, and with
    nothing changing between runs, exit 2. Never PASS."""
    r = run_runner(runner_fixtures(tmp_path, lines=runner_lines() + runner_other_run(-0.3)))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "attempts in its slot on the pod clock" in r.stdout
    assert r.stdout.rstrip().splitlines()[-1].startswith("runner: AMBIGUOUS (")


def test_runner_clusterip_defect_fails_even_beside_another_run(tmp_path):
    """The ClusterIP path never logs. Another run's literal attempt is in the window, but not in the
    ClusterIP row's slot: NOT-LOGGED."""
    r = run_runner(runner_fixtures(tmp_path, lines=runner_lines(skip="10.96.0.1:443") + runner_other_run(-10.5)))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "NOT-LOGGED 10.96.0.1:443" in r.stdout
