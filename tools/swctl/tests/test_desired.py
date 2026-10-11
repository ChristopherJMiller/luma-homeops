import pytest
import yaml

from swctl.desired import dump, from_doc
from swctl.errors import SwctlError


def doc(**over):
    d = {
        "management": {"port": 1, "vlan": 1, "address": "192.168.10.200"},
        "canaries": ["192.168.0.240"],
        "vlans": {1: "DefaultVLAN", 20: "VOICE"},
        "ports": {p: {"untagged": 1} for p in range(1, 29)},
    }
    d["ports"][1] = {"untagged": 1, "tagged": [20], "desc": "VyOS eth2 uplink"}
    d.update(over)
    return d


def test_valid_doc():
    d = from_doc(doc())
    assert d.management_port == 1 and d.address == "192.168.10.200"
    assert d.state.ports[1].untagged == {1} and d.state.ports[1].tagged == {20} and d.state.ports[1].pvid == 1
    assert d.desc[1] == "VyOS eth2 uplink" and d.canaries == ["192.168.0.240"]


def test_dump_round_trips():
    d = from_doc(doc())
    again = from_doc(yaml.safe_load(dump(d)))
    assert again.state == d.state and again.desc == d.desc and again.canaries == d.canaries


def test_every_port_must_be_listed():
    bad = doc()
    del bad["ports"][7]
    with pytest.raises(SwctlError, match=r"missing \[7\]"):
        from_doc(bad)


def test_unknown_vlan_and_untagged_also_tagged():
    bad = doc()
    bad["ports"][3] = {"untagged": 30}
    bad["ports"][4] = {"untagged": 20, "tagged": [20]}
    with pytest.raises(SwctlError) as e:
        from_doc(bad)
    assert "port 3: untagged VLAN 30" in str(e.value) and "port 4: VLAN 20 is both" in str(e.value)


def test_vlan_1_required():
    with pytest.raises(SwctlError, match="VLAN 1 must exist"):
        from_doc(doc(vlans={20: "VOICE"}))


def test_management_port_must_be_untagged_in_management_vlan():
    bad = doc()
    bad["ports"][1] = {"untagged": 20}
    with pytest.raises(SwctlError, match="management port 1 must be untagged in the management VLAN 1"):
        from_doc(bad)


def test_non_default_management_vlan_must_be_on_its_port_only():
    bad = doc(management={"port": 2, "vlan": 99, "address": "192.168.99.2"}, vlans={1: "DefaultVLAN", 99: "OOB"})
    bad["ports"][1] = {"untagged": 1}
    bad["ports"][2] = {"untagged": 99}
    bad["ports"][5] = {"untagged": 1, "tagged": [99]}
    with pytest.raises(SwctlError, match=r"management VLAN 99 must be on port 2 only, also on \[5\]"):
        from_doc(bad)


def test_long_name_and_bad_canary():
    with pytest.raises(SwctlError) as e:
        from_doc(doc(vlans={1: "x" * 33}, canaries=["not-an-ip"]))
    assert "longer than 32" in str(e.value) and "canary 'not-an-ip'" in str(e.value)
