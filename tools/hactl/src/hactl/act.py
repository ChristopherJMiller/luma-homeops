"""Call HA actions under Chris's actuation policy (2026-10-03).

Anything reversible is free while testing. Things that reach a person or
can't be undone (locks, phone notifications, speech, restarts) need his OK
first: re-run with --confirmed once he has said yes.
"""
import json

from hactl import output
from hactl.errors import HactlError, PolicyError

CONFIRM_PREFIXES = ("lock.", "notify.", "tts.", "assist_satellite.")
CONFIRM_EXACT = frozenset({"homeassistant.restart", "homeassistant.stop"})


def check_actuation(service: str, confirmed: bool) -> None:
    if service.count(".") != 1:
        raise HactlError(f"{service!r}: expected domain.service, e.g. light.turn_on")
    if (service.startswith(CONFIRM_PREFIXES) or service in CONFIRM_EXACT) and not confirmed:
        raise PolicyError(f"{service} needs Chris's OK first (actuation policy); re-run with --confirmed once he says yes")


def _run(args) -> int:
    from hactl.client import Client

    check_actuation(args.service, args.confirmed)
    try:
        data = json.loads(args.data) if args.data else {}
    except ValueError as e:
        raise HactlError(f"--data is not JSON: {e}") from None
    if args.entity:
        data["entity_id"] = args.entity
    domain, service = args.service.split(".")
    changed = Client().post(f"/api/services/{domain}/{service}", data)
    ids = [s["entity_id"] for s in changed] if isinstance(changed, list) else []
    output.emit(args, changed, f"{args.service}: {len(ids)} state(s) changed" + (f": {', '.join(ids)}" if ids else ""))
    return 0


def register(sub) -> None:
    p = sub.add_parser("call", parents=[output.COMMON], help="call an HA action (locks/notify need --confirmed)")
    p.add_argument("service", help="domain.service, e.g. light.turn_on")
    p.add_argument("--entity", action="append", help="entity_id (repeatable)")
    p.add_argument("--data", help="JSON object of action data")
    p.add_argument("--confirmed", action="store_true", help="Chris has OK'd this specific action")
    p.set_defaults(func=_run)
