"""Capture light states as a git `scene:` entry (e.g. from a Hue scene): hactl scene capture."""
import time

import yaml

from hactl import output
from hactl.errors import HactlError

RESTORE = "hactl_capture_restore"


def light_entry(state: dict) -> dict:
    if state["state"] != "on":
        return {"state": "off"}
    a = state["attributes"]
    out = {"state": "on"}
    if a.get("brightness") is not None:
        out["brightness"] = a["brightness"]
    mode = a.get("color_mode")
    if mode == "color_temp" and a.get("color_temp_kelvin"):
        out["color_temp_kelvin"] = a["color_temp_kelvin"]
    elif mode in ("xy", "hs", "rgb", "rgbw", "rgbww") and a.get("xy_color"):
        out["xy_color"] = [round(v, 4) for v in a["xy_color"]]
    return out


def scene_entry(scene_id: str, name: str, states: list) -> dict:
    bad = [s["entity_id"] for s in states if s["state"] in ("unavailable", "unknown")]
    if bad:
        raise HactlError(f"{', '.join(bad)} is unavailable: fix it before capturing")
    return {"id": scene_id, "name": name, "entities": {s["entity_id"]: light_entry(s) for s in states}}


class _Dumper(yaml.SafeDumper):
    """Quotes 'on'/'off' (YAML 1.1 would read them back as booleans)."""


_Dumper.add_representer(str, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v,
                                                            style="'" if v in ("on", "off") else None))


def render(entry: dict) -> str:
    return yaml.dump([entry], Dumper=_Dumper, sort_keys=False, default_flow_style=False, width=1000)


def capture(client, lights: list, scene_id: str, name: str, activate=None, settle=4.0) -> dict:
    if activate:
        client.post("/api/services/scene/create", {"scene_id": RESTORE, "snapshot_entities": lights})
        client.post("/api/services/scene/turn_on", {"entity_id": activate})
        time.sleep(settle)
    try:
        return scene_entry(scene_id, name, [client.get(f"/api/states/{e}") for e in lights])
    finally:
        if activate:
            client.post("/api/services/scene/turn_on", {"entity_id": f"scene.{RESTORE}"})


def _run(args) -> int:
    from hactl.client import Client

    lights = [x.strip() for x in args.lights.split(",") if x.strip()]
    entry = capture(Client(), lights, args.id, args.name, activate=args.activate, settle=args.settle)
    output.emit(args, entry, [render(entry).rstrip("\n")])
    return 0


def register(sub) -> None:
    p = sub.add_parser("scene", help="scene tools")
    s = p.add_subparsers(dest="scene_cmd", required=True)
    c = s.add_parser("capture", parents=[output.COMMON],
                     help="print a git scene entry from the lights' current (or a scene's) state")
    c.add_argument("--lights", required=True, help="comma list of light entity ids")
    c.add_argument("--id", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--activate", help="scene to turn on first (the lights are restored afterwards)")
    c.add_argument("--settle", type=float, default=4.0, help="seconds to wait after activating")
    c.set_defaults(func=_run)
