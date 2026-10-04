"""One-shot health: drift, the reload hook, failing automations, repairs, log, unavailable refs."""
import json
import subprocess

from hactl import output, paths, query, refs, revision, yamlload
from hactl.errors import HactlError
from hactl.state import cli as state_cli

ERROR_EXECUTIONS = frozenset({"error", "unhandled_error"})
# "aborted" is also what HA records when an action-sequence condition is false
# or a wait times out: normal. It is a failure only if some step errored.
ABORTED = "aborted"


def _trace_errored(trace: dict) -> bool:
    if trace.get("error"):
        return True
    return any(step.get("error") for steps in (trace.get("trace") or {}).values() for step in steps)


def failing_automations(trace_list, get_trace=None) -> list:
    """Automations whose most recent stored run errored. Cancelled/condition-failed runs are normal.

    get_trace(item_id, run_id) -> full trace; needed to judge "aborted" runs.
    """
    latest = {}
    for t in trace_list:
        start = (t.get("timestamp") or {}).get("start", "")
        if t["item_id"] not in latest or start > (latest[t["item_id"]].get("timestamp") or {}).get("start", ""):
            latest[t["item_id"]] = t
    def failed(item, t):
        execution = t.get("script_execution")
        if execution in ERROR_EXECUTIONS:
            return True
        return execution == ABORTED and get_trace is not None and _trace_errored(get_trace(item, t["run_id"]))

    return [
        {"item_id": k, "run_id": t["run_id"], "execution": t.get("script_execution"), "start": (t.get("timestamp") or {}).get("start")}
        for k, t in sorted(latest.items())
        if failed(k, t)
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


def not_loaded(entries) -> list:
    """Config entries that should be running but aren't (disabled ones are intentional)."""
    return [f"{e['domain']}/{e['title']}: {e['state']}" + (f" ({e['reason']})" if e.get("reason") else "")
            for e in entries if e.get("state") != "loaded" and not e.get("disabled_by")]


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


GONE = ("unavailable", "unknown")


def registry_report(devices, entities, states, long_unavailable_ids) -> dict:
    """Area-less devices (enabled, physical or integration-made, with an enabled entity), registry entries
    no integration provides any more (HA only restored them), and entities unavailable for 30+ days."""
    live_devices = {e.get("device_id") for e in entities if not e.get("disabled_by")}
    no_area = sorted(d.get("name_by_user") or d.get("name") or d["id"] for d in devices
                     if not d.get("area_id") and d.get("entry_type") != "service" and not d.get("disabled_by")
                     and d["id"] in live_devices)
    restored = sorted(e["entity_id"] for e in entities
                      if (states.get(e["entity_id"]) or {}).get("attributes", {}).get("restored"))
    return {"no_area": no_area, "restored": restored, "long_unavailable": sorted(long_unavailable_ids)}


def long_unavailable(client, entity_ids, days=30) -> list:
    """Of these entities, the ones that were `unavailable` throughout the last `days` days (`unknown` is the normal
    state of buttons, scenes, notify and event entities, so it doesn't count)."""
    if not entity_ids:
        return []
    from datetime import datetime, timedelta, timezone
    from urllib.parse import quote

    now = datetime.now(timezone.utc)
    start, end = quote((now - timedelta(days=days)).isoformat()), quote(now.isoformat())
    # end_time is required: HA defaults it to start + 1 day. The recorder keeps fewer days than this
    # window (purge_keep_days), so in practice this is "unavailable for all recorded history".
    hist = client.get(f"/api/history/period/{start}?end_time={end}&filter_entity_id={','.join(entity_ids)}"
                      "&minimal_response&no_attributes")
    return [series[0]["entity_id"] for series in hist if series and all(s.get("state") == "unavailable" for s in series)]


def collect_registry(client) -> dict:
    devices, entities = client.ws({"type": "config/device_registry/list"}, {"type": "config/entity_registry/list"})
    states = {s["entity_id"]: s for s in client.get("/api/states")}
    candidates = [e["entity_id"] for e in entities if not e.get("disabled_by")
                  and (states.get(e["entity_id"]) or {}).get("state") == "unavailable"
                  and not states[e["entity_id"]].get("attributes", {}).get("restored")]
    return registry_report(devices, entities, states, long_unavailable(client, candidates))


def collect(client, ha_dir=paths.HA_DIR) -> dict:
    states = {s["entity_id"]: s["state"] for s in client.get("/api/states")}
    components = set(client.get("/api/config")["components"])
    traces, log, repairs = client.ws(
        {"type": "trace/list", "domain": "automation"}, {"type": "system_log/list"}, {"type": "repairs/list_issues"})
    used = set()
    for sub in ("packages", "dashboards"):
        for f in sorted((ha_dir / sub).glob("*.yaml")):
            used |= refs.extract_refs(yamlload.load(f))
    try:
        _, snap, changes = state_cli.compute(client, ha_dir / "state")
        state, entries = [str(c) for c in changes], snap.entries
    except HactlError as e:
        state, entries = [f"could not plan: {e}"], client.ws({"type": "config_entries/get"})[0]
    return {
        "drift": drift(revision.compute(ha_dir), states.get("sensor.ha_config_revision"),
                       declared_domains(yamlload.load_dir(ha_dir / "packages")), components),
        "hook": hook_status(),
        "failing": failing_automations(traces, get_trace=lambda item, run: client.ws(
            {"type": "trace/get", "domain": "automation", "item_id": item, "run_id": run})[0]),
        "repairs": [{"domain": i["domain"], "issue_id": i["issue_id"], "severity": i.get("severity")}
                    for i in repairs["issues"] if not i.get("ignored")],
        "log_errors": sorted(query.format_log(log, errors_only=True), key=lambda e: -e["count"])[:10],
        "unavailable": sorted(r for r in used if states.get(r) in ("unavailable", "unknown")),
        "state": state,
        "not_loaded": not_loaded(entries),
    }


def render(r: dict) -> tuple:
    problem = bool(r["drift"] or r["failing"] or r["hook"] == "failed" or r.get("state"))
    lines = [f"health: {'PROBLEMS' if problem else 'ok'}"]

    def section(title, items, fmt=str):
        lines.append(f"{title}: none" if not items else f"{title}:")
        lines.extend(f"  - {fmt(i)}" for i in items)

    section("drift", r["drift"])
    section("state plan (hactl plan; hactl apply converges)", r.get("state", []))
    section("integrations not loaded (warning)", r.get("not_loaded", []))
    lines.append(f"ha-reload hook: {r['hook'] or 'unknown (no KUBECONFIG, or no run yet)'}")
    section("failing automations", r["failing"], lambda f: f"{f['item_id']} ({f['execution']} at {f['start']}): hactl trace {f['item_id']}")
    section("repairs", r["repairs"], lambda i: f"{i['domain']}/{i['issue_id']} [{i['severity']}]")
    section("log errors, top 10 by count", r["log_errors"], lambda e: f"{e['count']}x {e['name']}: {e['message'][:140]}")
    section("referenced entities that are unavailable (warning)", r["unavailable"])
    if "registry" in r:
        g = r["registry"]
        section("devices without an area", g["no_area"])
        section("registry entries no integration provides (restored; remove: in entities.yaml)", g["restored"])
        section("unavailable for all recorded history (up to 30 days)", g["long_unavailable"])
        problem = problem or bool(g["restored"])
    return lines, problem


def _run(args) -> int:
    from hactl.client import Client

    client = Client()
    report = collect(client)
    if args.registry:
        report["registry"] = collect_registry(client)
    lines, problem = render(report)
    output.emit(args, report, lines)
    return 1 if problem else 0


def register(sub) -> None:
    p = sub.add_parser("health", parents=[output.COMMON], help="drift, failing automations, repairs, log errors")
    p.add_argument("--registry", action="store_true", help="also list area-less devices and dead registry entries")
    p.set_defaults(func=_run)
