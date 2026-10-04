"""hactl import / plan / apply: declarative HA state (spec §4.11)."""
from hactl import output, paths
from hactl.state import apply as applier
from hactl.state import diff, importer, live, model


def _client():
    from hactl.client import Client
    return Client()


def compute(client, state_dir=None):
    m = model.load(state_dir or model.STATE_DIR)  # raises before any API call if the manifests are wrong
    snap = live.fetch(client, want_options=diff.options_wanted(m))
    return m, snap, diff.plan(m, snap)


def converge(client, prune=False, state_dir=None, log=print):
    """Plan, apply, re-plan. ok = no errors and nothing left to do."""
    _, _, changes = compute(client, state_dir)
    if not changes:
        return ["state: in sync"], True
    result = applier.execute(client, changes, prune=prune, log=log)
    _, _, left = compute(client, state_dir)
    lines = [f"state: applied {len(result['applied'])}, failed {len(result['errors'])}"]
    lines += [f"  error: {e}" for e in result["errors"]]
    lines += [f"  note: {n}" for n in result["notes"]]
    lines += [f"  still to do: {c}" for c in left]
    return lines, not result["errors"] and not left


def _plan(args) -> int:
    _, _, changes = compute(_client())
    output.emit(args, [c.public() for c in changes], [str(c) for c in changes] or ["state: in sync"])
    return 2 if changes else 0


def _apply(args) -> int:
    lines, ok = converge(_client(), prune=args.prune)
    output.emit(args, {"ok": ok, "lines": lines}, lines)
    return 0 if ok else 1


def _import(args) -> int:
    client = _client()
    entries = client.ws({"type": "config_entries/get"})[0]
    want = {(e["domain"], e["title"]) for e in entries if e["domain"] in model.HELPER_DOMAINS}
    written = importer.write(model.STATE_DIR, importer.build(live.fetch(client, want_options=want)), force=args.force)
    lines = [f"wrote {paths.rel(p)}" for p in written] + ["next: review the files, then `hactl plan` (should say in sync)"]
    output.emit(args, [str(p) for p in written], lines)
    return 0


def register(sub) -> None:
    p = sub.add_parser("import", parents=[output.COMMON], help="write state/ manifests from live HA (bootstrap)")
    p.add_argument("--force", action="store_true", help="overwrite existing manifests (credentials.yaml is never touched)")
    p.set_defaults(func=_import)
    p = sub.add_parser("plan", parents=[output.COMMON], help="diff state/ manifests against live HA (exit 2 = changes)")
    p.set_defaults(func=_plan)
    p = sub.add_parser("apply", parents=[output.COMMON], help="converge live HA to the state/ manifests")
    p.add_argument("--prune", action="store_true", help="also delete undeclared floors/labels/areas/helpers/dashboards/resources")
    p.set_defaults(func=_apply)
