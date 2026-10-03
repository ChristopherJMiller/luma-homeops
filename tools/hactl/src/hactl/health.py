"""One-shot health: drift, the reload hook, failing automations, repairs, log, unavailable refs."""
import json
import subprocess

from hactl import output, paths, query, refs, revision, yamlload

ERROR_EXECUTIONS = frozenset({"error", "unhandled_error", "aborted"})


def failing_automations(trace_list) -> list:
    """Automations whose most recent stored run errored. Cancelled/condition-failed runs are normal."""
    latest = {}
    for t in trace_list:
        start = (t.get("timestamp") or {}).get("start", "")
        if t["item_id"] not in latest or start > (latest[t["item_id"]].get("timestamp") or {}).get("start", ""):
            latest[t["item_id"]] = t
    return [
        {"item_id": k, "run_id": t["run_id"], "execution": t.get("script_execution"), "start": (t.get("timestamp") or {}).get("start")}
        for k, t in sorted(latest.items())
        if t.get("script_execution") in ERROR_EXECUTIONS
    ]


def declared_domains(packages: dict) -> set:
    return {k for p in packages.values() if isinstance(p, dict) for k in p}


def drift(repo_rev, loaded_rev, declared, components) -> list:
    out = []
    if loaded_rev is None:
        out.append("sensor.ha_config_revision missing: the revision package was never loaded")
    elif loaded_rev != repo_rev:
        out.append(f"HA runs config {loaded_rev}, the repo is {repo_rev}: not deployed yet, or not reloaded")
    missing = sorted(d for d in declared if d not in components)
    if missing:
        out.append(f"declared in packages but not loaded: {', '.join(missing)} (needs a restart, or failed to set up)")
    return out


def hook_status():
    """ha-reload Job outcome via kubectl (read-only). None when unknown."""
    try:
        r = subprocess.run(["kubectl", "-n", "home-assistant", "get", "job", "ha-reload", "-o", "json"],
                           capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    st = json.loads(r.stdout).get("status", {})
    return "failed" if st.get("failed") else "succeeded" if st.get("succeeded") else "running"


def collect(client, ha_dir=paths.HA_DIR) -> dict:
    states = {s["entity_id"]: s["state"] for s in client.get("/api/states")}
    components = set(client.get("/api/config")["components"])
    traces, log, repairs = client.ws(
        {"type": "trace/list", "domain": "automation"}, {"type": "system_log/list"}, {"type": "repairs/list_issues"})
    used = set()
    for sub in ("packages", "dashboards"):
        for f in sorted((ha_dir / sub).glob("*.yaml")):
            used |= refs.extract_refs(yamlload.load(f))
    return {
        "drift": drift(revision.compute(ha_dir), states.get("sensor.ha_config_revision"),
                       declared_domains(yamlload.load_dir(ha_dir / "packages")), components),
        "hook": hook_status(),
        "failing": failing_automations(traces),
        "repairs": [{"domain": i["domain"], "issue_id": i["issue_id"], "severity": i.get("severity")}
                    for i in repairs["issues"] if not i.get("ignored")],
        "log_errors": sorted(query.format_log(log, errors_only=True), key=lambda e: -e["count"])[:10],
        "unavailable": sorted(r for r in used if states.get(r) in ("unavailable", "unknown")),
    }


def render(r: dict) -> tuple:
    problem = bool(r["drift"] or r["failing"] or r["hook"] == "failed")
    lines = [f"health: {'PROBLEMS' if problem else 'ok'}"]

    def section(title, items, fmt=str):
        lines.append(f"{title}: none" if not items else f"{title}:")
        lines.extend(f"  - {fmt(i)}" for i in items)

    section("drift", r["drift"])
    lines.append(f"ha-reload hook: {r['hook'] or 'unknown (no KUBECONFIG, or no run yet)'}")
    section("failing automations", r["failing"], lambda f: f"{f['item_id']} ({f['execution']} at {f['start']}): hactl trace {f['item_id']}")
    section("repairs", r["repairs"], lambda i: f"{i['domain']}/{i['issue_id']} [{i['severity']}]")
    section("log errors, top 10 by count", r["log_errors"], lambda e: f"{e['count']}x {e['name']}: {e['message'][:140]}")
    section("referenced entities that are unavailable (warning)", r["unavailable"])
    return lines, problem


def _run(args) -> int:
    from hactl.client import Client

    report = collect(Client())
    lines, problem = render(report)
    output.emit(args, report, lines)
    return 1 if problem else 0


def register(sub) -> None:
    p = sub.add_parser("health", parents=[output.COMMON], help="drift, failing automations, repairs, log errors")
    p.set_defaults(func=_run)
