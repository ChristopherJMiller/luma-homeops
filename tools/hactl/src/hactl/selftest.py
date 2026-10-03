"""Read-only end-to-end check of every hactl read path against live HA."""
from datetime import timedelta

from hactl import health, output, query, shot


def checks(client):
    yield "REST /api/", lambda: client.get("/api/")["message"] == "API running."
    yield "websocket auth/current_user", lambda: bool(client.ws({"type": "auth/current_user"})[0]["name"])
    yield "find sun.sun", lambda: any(r.entity_id == "sun.sun" for r in query.find_rows(client, text="sun.sun"))
    yield "template", lambda: client.post("/api/template", {"template": "{{ 1 + 1 }}"}, raw=True).strip() == "2"
    yield "history 24h", lambda: isinstance(query.fetch_history(client, ["sun.sun"], timedelta(hours=24)), list)
    yield "stats 7d", lambda: isinstance(query.fetch_stats(client, "sensor.living_room_temperature", 7), list)
    yield "trace list", lambda: isinstance(client.ws({"type": "trace/list", "domain": "automation"})[0], list)
    yield "system log", lambda: isinstance(client.ws({"type": "system_log/list"})[0], list)
    yield "health collect", lambda: "drift" in health.collect(client)

    def screenshot():
        [r] = shot.take(client, shot.plan_shots(["/home-ops/0"], ["phone"], ["dark"]), shot.default_out_dir())
        return r["cards"] > 0 and r["theme_ok"] and not r["error_cards"]

    yield "screenshot /home-ops/0 (phone)", screenshot


def _run(args) -> int:
    from hactl.client import Client

    client = Client()
    results = []
    for name, check in checks(client):
        try:
            ok, err = bool(check()), ""
        except Exception as e:  # report every failure, keep going
            ok, err = False, f"{type(e).__name__}: {e}"
        results.append({"check": name, "ok": ok, "error": err})
    lines = [f"{'PASS' if r['ok'] else 'FAIL'}  {r['check']}" + (f"  ({r['error']})" if r["error"] else "") for r in results]
    output.emit(args, results, lines)
    return 0 if all(r["ok"] for r in results) else 1


def register(sub) -> None:
    p = sub.add_parser("selftest", parents=[output.COMMON], help="read-only end-to-end check against live HA")
    p.set_defaults(func=_run)
