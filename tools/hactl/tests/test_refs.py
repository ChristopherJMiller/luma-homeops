from hactl import refs

PACKAGE = {
    "automation": [{
        "id": "x",
        "alias": "Bedroom Switch Master",
        "triggers": [{"trigger": "state", "entity_id": ["switch.bedroom_bedroom_light_switch"], "to": "on"}],
        "conditions": [{
            "condition": "template",
            "value_template": "{{ is_state('person.chris_m', 'home') and states.light.floor_lamp.state == 'on' }}",
        }],
        "actions": [
            {"action": "light.turn_on", "target": {"entity_id": "light.bedroom_all"}},
            {"action": "script.room_auto", "data": {"room": "bedroom"}},
        ],
    }],
    "scene": [{"name": "Bedroom Night", "entities": {"light.nightstand_lamp": {"state": "on", "brightness": 3}}}],
    "template": [{"sensor": [{"name": "HA Config Revision", "state": "x"}]}],
    "input_boolean": {"guest_mode": {"name": "Guest mode"}},
    "script": {"room_auto": {"sequence": []}},
    "light": [{"platform": "group", "name": "Bedroom All", "entities": ["light.dresser_lamp"]}],
}


def test_extract_refs_finds_entities_not_services():
    assert refs.extract_refs(PACKAGE) == {
        "switch.bedroom_bedroom_light_switch",
        "person.chris_m",
        "light.floor_lamp",
        "light.bedroom_all",
        "light.nightstand_lamp",
        "light.dresser_lamp",
    }


def test_extract_refs_ignores_non_entities():
    card = {
        "type": "custom:mushroom-chips-card",
        "icon": "mdi:lock",
        "url": "https://home.chrismiller.xyz/x",
        "content": "{{ states.light | selectattr('state','eq','on') | list | count }}",
        "perform_action": "light.toggle",
    }
    assert refs.extract_refs(card) == set()


def test_slugify_matches_ha():
    assert refs.slugify("HA Config Revision") == "ha_config_revision"
    assert refs.slugify("Presence - Lights Off When Away") == "presence_lights_off_when_away"


def test_declared_entities():
    assert refs.declared_entities({"pkg": PACKAGE}) == {
        "automation.bedroom_switch_master",
        "scene.bedroom_night",
        "sensor.ha_config_revision",
        "input_boolean.guest_mode",
        "script.room_auto",
        "light.bedroom_all",
    }
