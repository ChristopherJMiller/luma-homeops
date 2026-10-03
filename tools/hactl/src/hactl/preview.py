"""Preview a dashboard file on a scratch storage dashboard, then screenshot it.

Nothing in git, the ConfigMaps or home-ops changes. `!include`s are resolved
locally because a storage dashboard is one JSON document.
"""
import json
from pathlib import Path

from hactl import output, shot, yamlload
from hactl.errors import HactlError

PREVIEW_URL = "claude-preview"


def _tags(obj) -> set:
    if isinstance(obj, yamlload.Tagged):
        return {obj.tag}
    if isinstance(obj, dict):
        return set().union(*(_tags(v) for v in obj.values())) if obj else set()
    if isinstance(obj, list):
        return set().union(*(_tags(v) for v in obj)) if obj else set()
    return set()


def resolve(path) -> dict:
    path = Path(path)
    cfg = yamlload.load(path, resolve_includes=True, allow_secret=False)
    if not isinstance(cfg, dict) or not isinstance(cfg.get("views"), list):
        raise HactlError(f"{path}: not a dashboard (needs a top-level `views:` list)")
    leftover = _tags(cfg)
    if leftover:
        raise HactlError(f"{path}: unsupported tag(s) in a dashboard: {', '.join(sorted(leftover))}")
    try:
        return json.loads(json.dumps(cfg))
    except TypeError as e:
        raise HactlError(f"{path}: a value can't be sent to HA ({e}); quote it") from None


def ensure_dashboard(client) -> bool:
    existing = client.ws({"type": "lovelace/dashboards/list"})[0]
    if any(d["url_path"] == PREVIEW_URL for d in existing):
        return False
    client.ws({"type": "lovelace/dashboards/create", "url_path": PREVIEW_URL, "title": "Claude Preview",
               "icon": "mdi:flask-outline", "mode": "storage", "require_admin": True, "show_in_sidebar": False})
    return True


def push(client, config: dict) -> None:
    client.ws({"type": "lovelace/config/save", "url_path": PREVIEW_URL, "config": config})


def _run(args) -> int:
    from hactl.client import Client

    cfg = resolve(args.file)
    client = Client()
    created = ensure_dashboard(client)
    push(client, cfg)
    views = args.view if args.view else list(range(len(cfg["views"])))
    for v in views:
        if not 0 <= v < len(cfg["views"]):
            raise HactlError(f"--view {v}: the dashboard has views 0..{len(cfg['views']) - 1}")
    specs = shot.plan_shots([f"/{PREVIEW_URL}/{v}" for v in views], shot.parse_viewports(args.viewport), args.scheme or ["dark"])
    results = shot.take(client, specs, Path(args.out) if args.out else shot.default_out_dir())
    lines, bad = shot.report(results)
    head = f"pushed {args.file} to /{PREVIEW_URL}" + (" (created the preview dashboard)" if created else "")
    output.emit(args, {"pushed": str(args.file), "created": created, "shots": results}, [head] + lines)
    return 1 if bad else 0


def register(sub) -> None:
    p = sub.add_parser("preview", parents=[output.COMMON], help="push a dashboard file to /claude-preview and screenshot it")
    p.add_argument("file")
    p.add_argument("--view", type=int, action="append", help="view index to shoot (repeatable; default all)")
    p.add_argument("--viewport", default="phone,desktop")
    p.add_argument("--scheme", action="append", choices=["dark", "light"])
    p.add_argument("--out")
    p.set_defaults(func=_run)
