import pytest
import yaml

from fakes import FakeSwitch, flat
from swctl import commands
from swctl.desired import from_doc
from swctl.errors import SwctlError

# The switch as it is since the 2026-10-11 cut-over: port 2 alone in VLAN 99.
VLANS = {1: "DefaultVLAN", 99: "SWITCH-OOB"}
BASE = flat(VLANS, {2: ({99}, set(), 99)})


class FakeRouter:
    """Router stand-in: SNMP reads/writes go to a FakeSwitch."""

    def __init__(self, state, fdb_text="", neigh="", responding=(), fail_at=None, lose_after_writes=()):
        self.switch = FakeSwitch(state)
        self.fdb_text, self.neigh = fdb_text, neigh
        self.responding, self.lose_after_writes = set(responding), set(lose_after_writes)
        self.fail_at, self.armed, self.revert_ops, self.writes = fail_at, False, None, 0

    def walk_text(self, oids):
        from swctl.inventory import FDB_PORT
        return self.fdb_text if oids == [FDB_PORT] else self.switch.walk_text()

    def walk(self, oids):
        from swctl.model import parse_walk
        return parse_walk(self.walk_text(oids))

    def run(self, script):
        return self.neigh if "neigh" in script else ""

    def apply(self, ops, stop_on_error=True):
        out = []
        for i, op in enumerate(ops):
            if self.fail_at is not None and self.writes == self.fail_at:
                self.fail_at = None
                out.append(f"FAIL {i} commitFailed")
                if stop_on_error:
                    break
                continue
            self.switch.write(op)
            self.writes += 1
            out.append(f"OK {i}")
        if self.lose_after_writes:
            self.responding -= self.lose_after_writes
        return out

    def deadman_armed(self):
        return self.armed

    def arm_deadman(self, revert_ops, minutes):
        self.armed, self.revert_ops = True, revert_ops

    def disarm_deadman(self):
        self.armed = False

    def responders(self, ips):
        return set(ips) & self.responding


def write_desired(tmp_path, state, mgmt=(2, 99), canaries=()):
    ports = {}
    for p, port in state.ports.items():
        (u,) = port.untagged
        ports[p] = {"untagged": u, **({"tagged": sorted(port.tagged)} if port.tagged else {})}
    doc = {"management": {"port": mgmt[0], "vlan": mgmt[1], "address": "192.168.99.2"},
           "canaries": list(canaries), "vlans": state.vlans, "ports": ports}
    f = tmp_path / "sw.yaml"
    f.write_text(yaml.safe_dump(doc))
    return f


def test_import_refuses_double_untagged(tmp_path, monkeypatch):
    live = flat({**VLANS, 20: "VOICE"}, {2: ({99}, set(), 99), 9: ({1, 20}, set(), 20)})
    monkeypatch.setattr(commands, "make_router", lambda address: FakeRouter(live))
    with pytest.raises(SwctlError, match=r"port 9 \[1, 20\]"):
        commands.main_for_tests(["import", "--file", str(tmp_path / "sw.yaml")])


def test_import_then_plan_is_in_sync(tmp_path, monkeypatch, capsys):
    live = flat({**VLANS, 20: "VOICE"}, {1: ({1}, {20}, 1), 2: ({99}, set(), 99)})
    monkeypatch.setattr(commands, "make_router", lambda address: FakeRouter(live))
    f = tmp_path / "sw.yaml"
    assert commands.main_for_tests(["import", "--file", str(f)]) == 0
    d = from_doc(yaml.safe_load(f.read_text()))
    assert d.state.ports[1].tagged == {20} and (d.management_port, d.management_vlan, d.address) == (2, 99, "192.168.99.2")
    assert commands.main_for_tests(["plan", "--file", str(f)]) == 0
    assert "in sync" in capsys.readouterr().out


def test_plan_exit_2_on_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "make_router", lambda address: FakeRouter(BASE))
    target = flat({**VLANS, 20: "VOICE"}, {1: ({1}, {20}, 1), 2: ({99}, set(), 99), 9: ({20}, set(), 20)})
    assert commands.main_for_tests(["plan", "--file", str(write_desired(tmp_path, target))]) == 2


def test_backup_writes_lines_without_trailing_whitespace(tmp_path, monkeypatch):
    fr = FakeRouter(BASE)
    fr.walk_text = lambda oids: '.1.3.6.1.2.1.1.1.0 "TEG-30284 "   \n.1.3.6.1.2.1.17.7.1.4.3.1.2.1 "FF FF FF F0 "  \n'
    monkeypatch.setattr(commands, "make_router", lambda address: fr)
    monkeypatch.setattr(commands, "BACKUP", tmp_path / "b.snmp")
    commands.main_for_tests(["backup", "--file", str(write_desired(tmp_path, BASE))])
    lines = (tmp_path / "b.snmp").read_text().splitlines()
    assert lines and all(l == l.rstrip() for l in lines)


VOICE = flat({**VLANS, 20: "VOICE"}, {1: ({1}, {20}, 1), 2: ({99}, set(), 99), 9: ({20}, set(), 20)})


def test_apply_converges_and_disarms(tmp_path, monkeypatch):
    fr = FakeRouter(BASE)
    monkeypatch.setattr(commands, "make_router", lambda address: fr)
    assert commands.main_for_tests(["apply", "--file", str(write_desired(tmp_path, VOICE))]) == 0
    assert fr.switch.state() == VOICE and fr.armed is False


def test_apply_refuses_when_deadman_armed(tmp_path, monkeypatch):
    fr = FakeRouter(BASE)
    fr.armed = True
    monkeypatch.setattr(commands, "make_router", lambda address: fr)
    with pytest.raises(SwctlError, match="already armed") as e:
        commands.main_for_tests(["apply", "--file", str(write_desired(tmp_path, VOICE))])
    assert e.value.exit_code == 3 and fr.writes == 0


def test_apply_reverts_partial_failure(tmp_path, monkeypatch):
    fr = FakeRouter(BASE, fail_at=3)
    monkeypatch.setattr(commands, "make_router", lambda address: fr)
    with pytest.raises(SwctlError, match="reverted"):
        commands.main_for_tests(["apply", "--file", str(write_desired(tmp_path, VOICE))])
    assert fr.switch.state() == BASE and fr.armed is False


def test_apply_reverts_when_a_canary_stops_answering(tmp_path, monkeypatch):
    fr = FakeRouter(BASE, responding={"192.168.0.240"}, lose_after_writes={"192.168.0.240"})
    monkeypatch.setattr(commands, "make_router", lambda address: fr)
    f = write_desired(tmp_path, VOICE, canaries=["192.168.0.240"])
    with pytest.raises(SwctlError, match="reverted"):
        commands.main_for_tests(["apply", "--file", str(f)])
    assert fr.switch.state() == BASE


def test_simulated_session_loss_leaves_deadman_armed(tmp_path, monkeypatch):
    fr = FakeRouter(BASE)
    monkeypatch.setattr(commands, "make_router", lambda address: fr)
    rc = commands.main_for_tests(["apply", "--file", str(write_desired(tmp_path, VOICE)), "--simulate-session-loss"])
    assert rc == 1 and fr.armed is True and fr.switch.state() == VOICE
    for op in fr.revert_ops:                                     # what the router would run
        fr.switch.write(op)
    assert fr.switch.state() == BASE


def test_apply_refuses_hand_changed_management(tmp_path, monkeypatch):
    fr = FakeRouter(flat(VLANS))                                 # port 2 put back in VLAN 1 by hand
    monkeypatch.setattr(commands, "make_router", lambda address: fr)
    with pytest.raises(SwctlError, match="management port 2"):
        commands.main_for_tests(["apply", "--file", str(write_desired(tmp_path, VOICE))])
    assert fr.writes == 0 and fr.armed is False
