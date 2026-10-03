"""Drive Home Assistant config and options flows from declared answers.

A flow is a sequence of steps: `menu` (pick next_step_id), `form` (submit
fields), `create_entry` (done), `abort`, or an interactive step that needs a
person (`external`, `progress`). Any failure aborts the flow so nothing is
left half-open in HA.
"""
from hactl.errors import HactlError

CONFIG_FLOW = "/api/config/config_entries/flow"
OPTIONS_FLOW = "/api/config/config_entries/options/flow"
MAX_STEPS = 12
INTERACTIVE = frozenset({"external", "external_done", "progress", "progress_done", "show_progress"})


class FlowManual(HactlError):
    """The flow needs a person (link button, OAuth sign-in, approval)."""


def field_names(step) -> list:
    return [f["name"] for f in step.get("data_schema") or []]


def current_values(step) -> dict:
    out = {}
    for f in step.get("data_schema") or []:
        desc = f.get("description") or {}
        if "suggested_value" in desc:
            out[f["name"]] = desc["suggested_value"]
        elif "default" in f:
            out[f["name"]] = f["default"]
    return out


def next_payload(step, answers: dict, menu: list, keep_current: bool = False) -> dict:
    kind, sid = step.get("type"), step.get("step_id")
    if kind == "menu":
        options = step.get("menu_options") or []
        if not menu:
            raise HactlError(f"flow step {sid!r} is a menu {options}; declare create.menu")
        choice = menu.pop(0)
        if choice not in options:
            raise HactlError(f"flow step {sid!r}: menu choice {choice!r} is not one of {options}")
        return {"next_step_id": choice}
    if kind == "form":
        if step.get("errors"):
            raise HactlError(f"HA rejected the answers at step {sid!r}: {step['errors']}")
        names = field_names(step)
        payload = current_values(step) if keep_current else {}
        payload.update({k: v for k, v in answers.items() if k in names})
        missing = [f["name"] for f in step.get("data_schema") or []
                   if f.get("required") and f["name"] not in payload and "default" not in f]
        if missing:
            raise HactlError(f"flow step {sid!r} needs {', '.join(missing)}: add them to the declared answers")
        return payload
    if kind in INTERACTIVE:
        raise FlowManual(f"flow step {sid!r} needs a person (HA: Settings -> Devices & services)")
    if kind == "abort":
        raise HactlError(f"HA aborted the flow: {step.get('reason')}")
    raise HactlError(f"unexpected flow step type {kind!r}")


def _abort(client, base, flow_id) -> None:
    if flow_id:
        try:
            client.rest("DELETE", f"{base}/{flow_id}")
        except HactlError:
            pass  # already finished or gone


def _drive(client, base, step, answers, menu, keep_current) -> dict:
    menu = list(menu)
    flow_id = step.get("flow_id")
    try:
        for _ in range(MAX_STEPS):
            if step.get("type") == "create_entry":
                return step
            payload = next_payload(step, answers, menu, keep_current)
            step = client.post(f"{base}/{flow_id}", payload)
        raise HactlError(f"flow did not finish within {MAX_STEPS} steps")
    except HactlError:
        _abort(client, base, flow_id)
        raise


def run_config_flow(client, domain, answers, menu=()) -> dict:
    step = client.post(CONFIG_FLOW, {"handler": domain, "show_advanced_options": True})
    return _drive(client, CONFIG_FLOW, step, answers, menu, keep_current=False)


def run_options_flow(client, entry_id, options) -> dict:
    step = client.post(OPTIONS_FLOW, {"handler": entry_id, "show_advanced_options": True})
    return _drive(client, OPTIONS_FLOW, step, options, (), keep_current=True)


def read_options(client, entry_id):
    """Current options from the options flow's first form, or None. Always aborts the flow."""
    try:
        step = client.post(OPTIONS_FLOW, {"handler": entry_id, "show_advanced_options": True})
    except HactlError:
        return None  # this entry has no options flow
    try:
        if step.get("type") != "form":
            return None
        return {"step_id": step.get("step_id"), "values": current_values(step)}
    finally:
        _abort(client, OPTIONS_FLOW, step.get("flow_id"))
