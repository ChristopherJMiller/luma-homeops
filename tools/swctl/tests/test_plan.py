import itertools

import pytest

from fakes import FakeSwitch, flat
from swctl.desired import from_doc
from swctl.errors import SwctlError
from swctl.model import PVID, VLAN_EGRESS, VLAN_STATUS, VLAN_UNTAGGED
from swctl.plan import changed_ports, check_management, diff


def converge(current, target):
    sw = FakeSwitch(current)
    for op in diff(current, target):
        sw.write(op)
    return sw.state()


def test_in_sync_is_empty():
    assert diff(flat(), flat()) == []


def test_move_port_to_new_vlan_order():
    target = flat({1: "DefaultVLAN", 99: "SWITCH-OOB"}, {2: ({99}, set(), 99)})
    ops = diff(flat(), target)
    kinds = [tuple(op.oid.rsplit(".", 1)) for op in ops]
    assert kinds[0] == (VLAN_STATUS, "99")                                  # create first, alone
    assert kinds.index((PVID, "2")) < kinds.index((VLAN_UNTAGGED, "1"))     # PVID moves before VLAN 1 lets go
    assert kinds.index((VLAN_UNTAGGED, "1")) < kinds.index((VLAN_EGRESS, "1"))
    assert converge(flat(), target) == target


def test_delete_vlan_comes_last():
    current = flat({1: "DefaultVLAN", 4000: "probe"}, {28: ({4000}, set(), 4000)})
    ops = diff(current, flat())
    assert ops[-1].oid == f"{VLAN_STATUS}.4000" and ops[-1].value == "6"
    assert converge(current, flat()) == flat()


def test_diff_fixes_double_untagged():
    current = flat({1: "DefaultVLAN", 20: "VOICE"}, {9: ({1, 20}, set(), 20)})
    target = flat({1: "DefaultVLAN", 20: "VOICE"}, {9: ({20}, set(), 20)})
    assert converge(current, target) == target


def test_add_tagged_vlan_to_uplink():
    target = flat({1: "DefaultVLAN", 20: "VOICE"}, {1: ({1}, {20}, 1), 9: ({20}, set(), 20)})
    assert converge(flat(), target) == target


@pytest.mark.parametrize("a,b", list(itertools.permutations([
    flat(),
    flat({1: "DefaultVLAN", 20: "VOICE"}, {1: ({1}, {20}, 1), 9: ({20}, set(), 20)}),
    flat({1: "DefaultVLAN", 99: "OOB", 4000: "x"}, {2: ({99}, set(), 99), 28: ({4000}, {99}, 4000)}),
], 2)))
def test_any_state_converges_to_any_other(a, b):
    assert converge(a, b) == b


def test_replaying_from_a_partial_apply_converges():
    target = flat({1: "DefaultVLAN", 99: "OOB"}, {2: ({99}, set(), 99)})
    sw = FakeSwitch(flat())
    for op in diff(flat(), target)[:3]:                                     # stopped half-way
        sw.write(op)
    partial = sw.state()
    for op in diff(partial, flat()):                                        # revert from where it really is
        sw.write(op)
    assert sw.state() == flat()


def desired(mgmt_port=1, mgmt_vlan=1):
    return from_doc({"management": {"port": mgmt_port, "vlan": mgmt_vlan, "address": "192.168.10.200"},
                     "vlans": {1: "DefaultVLAN"}, "ports": {p: {"untagged": 1} for p in range(1, 29)}})


def test_check_management_accepts_matching_live():
    check_management(desired(), flat())


def test_check_management_refuses_hand_changed_live():
    live = flat({1: "DefaultVLAN", 5: "x"}, {1: ({5}, set(), 5)})
    with pytest.raises(SwctlError, match="management port 1") as e:
        check_management(desired(), live)
    assert e.value.exit_code == 3


def test_changed_ports():
    target = flat({1: "DefaultVLAN", 20: "VOICE"}, {1: ({1}, {20}, 1), 9: ({20}, set(), 20)})
    assert changed_ports(flat(), target) == {1: False, 9: True}
