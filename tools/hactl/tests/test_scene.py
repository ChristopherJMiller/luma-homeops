from hactl import scene


def st(eid, state, **attrs):
    return {"entity_id": eid, "state": state, "attributes": attrs}


def test_color_temp_light_keeps_kelvin_and_brightness():
    e = scene.light_entry(st("light.floor_lamp_a", "on", brightness=143, color_mode="color_temp",
                             color_temp_kelvin=2702, xy_color=[0.46, 0.41]))
    assert e == {"state": "on", "brightness": 143, "color_temp_kelvin": 2702}


def test_xy_light_keeps_xy():
    e = scene.light_entry(st("light.desk_underlight", "on", brightness=77, color_mode="xy", xy_color=[0.5612, 0.4042]))
    assert e == {"state": "on", "brightness": 77, "xy_color": [0.5612, 0.4042]}


def test_off_and_onoff_lights():
    assert scene.light_entry(st("light.ground_spot", "off")) == {"state": "off"}
    assert scene.light_entry(st("light.camp_lamp", "on", color_mode="onoff")) == {"state": "on"}


def test_unavailable_light_is_refused():
    import pytest
    from hactl.errors import HactlError
    with pytest.raises(HactlError, match="light.x is unavailable"):
        scene.scene_entry("s", "S", [st("light.x", "unavailable")])


def test_render_quotes_on_off():
    out = scene.render(scene.scene_entry("relax_living_room", "Relax (Living Room)",
                                         [st("light.a", "on", brightness=10, color_mode="brightness"), st("light.b", "off")]))
    assert out == ("- id: relax_living_room\n  name: Relax (Living Room)\n  entities:\n"
                   "    light.a:\n      state: 'on'\n      brightness: 10\n"
                   "    light.b:\n      state: 'off'\n")


class FakeClient:
    def __init__(self, states):
        self.states, self.calls = states, []

    def post(self, path, data=None, raw=False):
        self.calls.append((path, data))
        return []

    def get(self, path, raw=False):
        return self.states[path.rsplit("/", 1)[1]]


def test_capture_activates_reads_then_restores():
    fake = FakeClient({"light.a": st("light.a", "on", brightness=5, color_mode="brightness")})
    entry = scene.capture(fake, ["light.a"], "x", "X", activate="scene.hue_x", settle=0)
    assert entry["entities"]["light.a"] == {"state": "on", "brightness": 5}
    assert [c[0] for c in fake.calls] == ["/api/services/scene/create", "/api/services/scene/turn_on",
                                          "/api/services/scene/turn_on"]
    assert fake.calls[0][1] == {"scene_id": "hactl_capture_restore", "snapshot_entities": ["light.a"]}
    assert fake.calls[1][1] == {"entity_id": "scene.hue_x"}
    assert fake.calls[2][1] == {"entity_id": "scene.hactl_capture_restore"}
