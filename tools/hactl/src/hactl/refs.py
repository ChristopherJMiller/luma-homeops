"""Entity references in HA config, and the entities the config itself declares."""
import re

from hactl.yamlload import Tagged

ENTITY_DOMAINS = frozenset("""
alarm_control_panel automation binary_sensor button calendar camera climate
cover device_tracker event fan group image input_boolean input_button
input_datetime input_number input_select input_text light lock media_player
notify number person remote scene script select sensor siren sun switch text
time timer todo update vacuum valve water_heater weather zone
""".split())

# Values under these keys are actions (light.turn_on), not entities.
SERVICE_KEYS = frozenset({"action", "service", "perform_action"})

_DOMAINS = "|".join(sorted(ENTITY_DOMAINS, key=len, reverse=True))
_REF = re.compile(rf"(?<![\w.])({_DOMAINS})\.([a-z0-9_]+)(?![\w(])")
_FULL = re.compile(rf"^({_DOMAINS})\.[a-z0-9_]+$")
_TEMPLATE_PLATFORMS = frozenset({
    "sensor", "binary_sensor", "switch", "number", "select", "button", "image",
    "weather", "light", "fan", "cover", "lock", "alarm_control_panel", "vacuum", "event", "update",
})


def extract_refs(obj, _key=None) -> set:
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and _FULL.match(k):
                out.add(k)  # scene `entities:` and customize use entity ids as keys
            out |= extract_refs(v, k)
    elif isinstance(obj, list):
        for v in obj:
            out |= extract_refs(v, _key)
    elif isinstance(obj, Tagged):
        out |= extract_refs(obj.value, _key)
    elif isinstance(obj, str) and _key not in SERVICE_KEYS:
        # states.light.floor_lamp.state is a reference too
        text = obj.replace("states.", "")
        out |= {f"{d}.{o}" for d, o in _REF.findall(text)}
    return out


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _named(items, domain) -> set:
    return {
        f"{domain}.{slugify(i['name'])}"
        for i in items or []
        if isinstance(i, dict) and isinstance(i.get("name"), str) and "{" not in i["name"]
    }


def declared_entities(packages: dict) -> set:
    """Entity ids the packages create (so lint accepts refs to not-yet-deployed ones)."""
    out = set()
    for pkg in packages.values():
        if not isinstance(pkg, dict):
            continue
        for domain, body in pkg.items():
            if (domain.startswith("input_") or domain in ("timer", "counter", "schedule", "script")) and isinstance(body, dict):
                out |= {f"{domain}.{key}" for key in body}
            elif domain == "automation" and isinstance(body, list):
                out |= {f"automation.{slugify(a['alias'])}" for a in body if isinstance(a, dict) and a.get("alias")}
            elif domain == "scene" and isinstance(body, list):
                out |= _named(body, "scene")
            elif domain == "template" and isinstance(body, list):
                for block in body:
                    for kind, items in (block or {}).items():
                        if kind in _TEMPLATE_PLATFORMS:
                            out |= _named(items, kind)
            elif isinstance(body, list):  # platform lists: light: - platform: group
                out |= _named([i for i in body if isinstance(i, dict) and i.get("platform")], domain)
    return out
