import pytest

from hactl import act
from hactl.errors import HactlError, PolicyError


@pytest.mark.parametrize("service", ["light.turn_on", "fan.turn_off", "cover.set_cover_position", "climate.set_hvac_mode",
                                     "scene.turn_on", "switch.toggle", "homeassistant.reload_all"])
def test_reversible_is_free(service):
    act.check_actuation(service, confirmed=False)


@pytest.mark.parametrize("service", ["lock.unlock", "lock.lock", "notify.mobile_app_pixel_9_pro_xl", "tts.speak",
                                     "assist_satellite.announce", "homeassistant.restart", "homeassistant.stop"])
def test_needs_chris(service):
    with pytest.raises(PolicyError, match="Chris"):
        act.check_actuation(service, confirmed=False)
    act.check_actuation(service, confirmed=True)


def test_malformed_service():
    with pytest.raises(HactlError, match="domain.service"):
        act.check_actuation("turn_on", confirmed=False)


@pytest.mark.parametrize("service", ["Lock.unlock", "LOCK.UNLOCK", "Notify.mobile_app_x", "HomeAssistant.Restart"])
def test_gate_is_case_insensitive(service):
    # HA lowercases domain and service before dispatch, so the gate must too.
    with pytest.raises(PolicyError):
        act.check_actuation(service, confirmed=False)
