"""Read-only queries: find, state, history, stats, template, trace, log."""
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from hactl import output
from hactl.errors import HactlError


@dataclass
class Row:
    entity_id: str
    name: str
    state: str
    area: str
    device: str
    integration: str
    disabled: bool


def build_index(states, entities, devices, areas) -> list:
    area_names = {a["area_id"]: a["name"] for a in areas}
    devs = {d["id"]: d for d in devices}
    st = {s["entity_id"]: s for s in states}
    rows, seen = [], set()
    for e in entities:
        d = devs.get(e.get("device_id") or "", {})
        s = st.get(e["entity_id"], {})
        rows.append(Row(
            entity_id=e["entity_id"],
            name=e.get("name") or s.get("attributes", {}).get("friendly_name") or e.get("original_name") or "",
            state=s.get("state", "-"),
            area=area_names.get(e.get("area_id") or d.get("area_id"), ""),
            device=d.get("name_by_user") or d.get("name") or "",
            integration=e.get("platform", ""),
            disabled=bool(e.get("disabled_by")),
        ))
        seen.add(e["entity_id"])
    for s in states:  # YAML entities without a unique_id are not in the registry
        if s["entity_id"] not in seen:
            rows.append(Row(s["entity_id"], s.get("attributes", {}).get("friendly_name", ""), s["state"], "", "", "", False))
    return sorted(rows, key=lambda r: r.entity_id)


def filter_rows(rows, text=None, area=None, domain=None, integration=None, unavailable=False, include_disabled=False) -> list:
    out = []
    for r in rows:
        if r.disabled and not include_disabled:
            continue
        if text and text.lower() not in f"{r.entity_id} {r.name}".lower():
            continue
        if area and area.lower().replace("_", " ") != r.area.lower():
            continue
        if domain and not r.entity_id.startswith(domain + "."):
            continue
        if integration and integration != r.integration:
            continue
        if unavailable and r.state not in ("unavailable", "unknown"):
            continue
        out.append(r)
    return out


def find_rows(client, **filters) -> list:
    states = client.get("/api/states")
    entities, devices, areas = client.ws(
        {"type": "config/entity_registry/list"},
        {"type": "config/device_registry/list"},
        {"type": "config/area_registry/list"},
    )
    return filter_rows(build_index(states, entities, devices, areas), **filters)


_SINCE = re.compile(r"^(\d+)([smhd])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_since(text: str) -> timedelta:
    m = _SINCE.match(text or "")
    if not m:
        raise HactlError(f"bad duration {text!r}: use e.g. 30m, 2h, 3d")
    return timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})


def fetch_history(client, ids, since: timedelta) -> list:
    start = datetime.now(timezone.utc) - since
    data = client.get(
        f"/api/history/period/{quote(start.isoformat())}?filter_entity_id={','.join(ids)}"
        "&minimal_response&no_attributes"
    )
    events = []
    for series in data or []:
        if not series:
            continue
        eid = series[0]["entity_id"]  # minimal_response: only the first item names it
        events += [(datetime.fromisoformat(i["last_changed"]), eid, i["state"]) for i in series]
    return sorted(events)


def detect_flaps(events, window_s=60, threshold=6) -> list:
    """A flap: >= threshold changes of one entity within window_s seconds. Bursts are merged."""
    by_entity = defaultdict(list)
    for t, e, _ in events:
        by_entity[e].append(t)
    bursts = []
    for e, ts in sorted(by_entity.items()):
        ts.sort()
        mine, start = [], 0
        for j, t in enumerate(ts):
            while (t - ts[start]).total_seconds() > window_s:
                start += 1
            if j - start + 1 >= threshold:
                if mine and ts[start] <= mine[-1]["end"]:
                    mine[-1]["end"] = t
                else:
                    mine.append({"entity_id": e, "start": ts[start], "end": t})
        for b in mine:
            b["changes"] = sum(1 for t in ts if b["start"] <= t <= b["end"])
        bursts += mine
    return bursts


def fetch_stats(client, statistic_id, days) -> list:
    start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    res = client.ws({
        "type": "recorder/statistics_during_period", "start_time": start,
        "statistic_ids": [statistic_id], "period": "day", "types": ["min", "max", "mean"],
    })[0]
    return [
        {"date": datetime.fromtimestamp(r["start"] / 1000, timezone.utc).date().isoformat(),
         "min": r.get("min"), "max": r.get("max"), "mean": r.get("mean")}
        for r in res.get(statistic_id, [])
    ]


def resolve_automation_id(client, ref: str) -> str:
    """automation.<x> entity id, or a bare config id -> config id."""
    if not ref.startswith("automation."):
        return ref
    cid = client.get(f"/api/states/{ref}").get("attributes", {}).get("id")
    if not cid:
        raise HactlError(f"{ref} has no config id (only automations with an `id:` keep traces)")
    return cid


_RESULT_KEYS = ("result", "params", "enabled", "choice", "delay", "done", "wait")


def summarize_trace(trace: dict) -> dict:
    steps = []
    for path, entries in (trace.get("trace") or {}).items():
        for en in entries:
            r = en.get("result") or {}
            steps.append({"path": path, "at": en.get("timestamp"), "error": en.get("error"),
                          "result": {k: r[k] for k in _RESULT_KEYS if k in r}})
    steps.sort(key=lambda s: s["at"] or "")
    return {"run_id": trace.get("run_id"), "start": (trace.get("timestamp") or {}).get("start"),
            "trigger": trace.get("trigger"), "execution": trace.get("script_execution"),
            "error": trace.get("error"), "steps": steps}


def format_log(entries, errors_only=False, since=None) -> list:
    rows = []
    for e in sorted(entries, key=lambda e: e["timestamp"], reverse=True):
        if errors_only and e["level"] not in ("ERROR", "CRITICAL"):
            continue
        if since is not None and e["timestamp"] < since:
            continue
        msg = (e.get("message") or [""])[0].replace("\n", " ")
        rows.append({"count": e.get("count", 1), "level": e["level"], "name": e["name"], "message": msg[:200],
                     "last": datetime.fromtimestamp(e["timestamp"], timezone.utc).isoformat(timespec="seconds")})
    return rows


# ---- CLI ---------------------------------------------------------------------

def _client():
    from hactl.client import Client
    return Client()


def _find(args):
    rows = find_rows(_client(), text=args.text, area=args.area, domain=args.domain, integration=args.integration,
                     unavailable=args.unavailable, include_disabled=args.disabled)
    lines = [f"{r.entity_id:52} {r.state[:16]:16} {r.area[:14]:14} {r.integration[:16]:16} {r.name}" for r in rows]
    output.emit(args, [asdict(r) for r in rows], lines or ["no matches"])


def _state(args):
    client = _client()
    st = client.get(f"/api/states/{args.entity_id}")
    try:
        reg = client.ws({"type": "config/entity_registry/get", "entity_id": args.entity_id})[0]
    except HactlError:
        reg = None  # YAML entity without a unique_id
    lines = [f"{st['entity_id']} = {st['state']}  (changed {st['last_changed']})"]
    lines += [f"  {k}: {v}" for k, v in sorted(st.get("attributes", {}).items())]
    if reg:
        lines.append(f"  registry: platform={reg.get('platform')} unique_id={reg.get('unique_id')} "
                     f"area={reg.get('area_id')} labels={reg.get('labels')} disabled={reg.get('disabled_by')}")
    output.emit(args, {"state": st, "registry": reg}, lines)


def _history(args):
    events = fetch_history(_client(), args.entity_ids, parse_since(args.since))
    flaps = detect_flaps(events, window_s=args.window, threshold=args.threshold)
    lines = [f"{t.isoformat(timespec='seconds')}  {e}  {s}" for t, e, s in events[-args.tail:]]
    lines += [f"FLAPPING {b['entity_id']}: {b['changes']} changes {b['start']:%Y-%m-%d %H:%M:%S}..{b['end']:%H:%M:%S}"
              for b in flaps]
    data = {"events": [{"time": t, "entity_id": e, "state": s} for t, e, s in events], "flaps": flaps}
    output.emit(args, data, lines or ["no changes"])


def _stats(args):
    rows = fetch_stats(_client(), args.statistic_id, args.days)
    lines = [f"{r['date']}  min {r['min']}  max {r['max']}  mean {round(r['mean'], 2) if r['mean'] is not None else None}"
             for r in rows]
    output.emit(args, rows, lines or ["no statistics (not a measurement sensor?)"])


def _template(args):
    text = Path(args.file).read_text() if args.file else args.template
    if not text:
        raise HactlError("give a template, or -f FILE")
    result = _client().post("/api/template", {"template": text}, raw=True)
    output.emit(args, {"result": result}, result)


def _trace(args):
    client = _client()
    cid = resolve_automation_id(client, args.automation)
    runs = client.ws({"type": "trace/list", "domain": "automation", "item_id": cid})[0]
    runs = sorted(runs, key=lambda t: (t.get("timestamp") or {}).get("start", ""), reverse=True)[: args.last]
    if not runs:
        output.emit(args, [], f"no stored runs for {cid}")
        return
    details = client.ws(*[{"type": "trace/get", "domain": "automation", "item_id": cid, "run_id": r["run_id"]} for r in runs])
    summaries = [summarize_trace(d) for d in details]
    lines = []
    for s in summaries:
        lines.append(f"{s['start']}  {s['execution']}  trigger: {s['trigger']}" + (f"  ERROR: {s['error']}" if s["error"] else ""))
        for st in s["steps"]:
            lines.append(f"    {st['path']:22} {st['result'] or ''}" + (f"  ERROR: {st['error']}" if st["error"] else ""))
    output.emit(args, summaries, lines)


def _log(args):
    since = (datetime.now(timezone.utc) - parse_since(args.since)).timestamp() if args.since else None
    rows = format_log(_client().ws({"type": "system_log/list"})[0], errors_only=args.errors, since=since)
    lines = [f"{r['count']:>5}x {r['level']:7} {r['last']}  {r['name']}: {r['message']}" for r in rows]
    output.emit(args, rows, lines or ["log is clean"])


def register(sub) -> None:
    p = sub.add_parser("find", parents=[output.COMMON], help="find entities by id/name, area, domain, integration")
    p.add_argument("text", nargs="?")
    p.add_argument("--area")
    p.add_argument("--domain")
    p.add_argument("--integration")
    p.add_argument("--unavailable", action="store_true", help="only unavailable/unknown")
    p.add_argument("--disabled", action="store_true", help="include disabled entities")
    p.set_defaults(func=_find)

    p = sub.add_parser("state", parents=[output.COMMON], help="full state, attributes and registry entry")
    p.add_argument("entity_id")
    p.set_defaults(func=_state)

    p = sub.add_parser("history", parents=[output.COMMON], help="state changes; flags flapping")
    p.add_argument("entity_ids", nargs="+")
    p.add_argument("--since", default="2h")
    p.add_argument("--tail", type=int, default=200, help="print at most the last N changes")
    p.add_argument("--window", type=int, default=60, help="flap window, seconds")
    p.add_argument("--threshold", type=int, default=6, help="changes within the window that count as flapping")
    p.set_defaults(func=_history)

    p = sub.add_parser("stats", parents=[output.COMMON], help="daily min/max/mean from long-term statistics")
    p.add_argument("statistic_id")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=_stats)

    p = sub.add_parser("template", parents=[output.COMMON], help="render a Jinja template against live state")
    p.add_argument("template", nargs="?")
    p.add_argument("-f", "--file")
    p.set_defaults(func=_template)

    p = sub.add_parser("trace", parents=[output.COMMON], help="recent runs of an automation, step by step")
    p.add_argument("automation", help="automation.<x> or its config id")
    p.add_argument("--last", type=int, default=3)
    p.set_defaults(func=_trace)

    p = sub.add_parser("log", parents=[output.COMMON], help="HA's deduplicated error/warning log")
    p.add_argument("--errors", action="store_true", help="errors only")
    p.add_argument("--since", help="e.g. 2h")
    p.set_defaults(func=_log)
