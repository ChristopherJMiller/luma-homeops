from swctl.model import parse_walk, state_from_walk

# Shape of `snmpbulkwalk -On -Oq -Ox -Oe` on the switch (2026-10-11), plus a VLAN 20.
WALK = """\
.1.3.6.1.2.1.17.7.1.4.3.1.1.1 "44 65 66 61 75 6C 74 56 4C 41 4E "
.1.3.6.1.2.1.17.7.1.4.3.1.1.20 "56 4F 49 43 45 "
.1.3.6.1.2.1.17.7.1.4.3.1.2.1 "FF FF FF E0 00 00 00 00 "
.1.3.6.1.2.1.17.7.1.4.3.1.2.20 "80 00 00 10 00 00 00 00 "
.1.3.6.1.2.1.17.7.1.4.3.1.3.1 "00 00 00 00 00 00 00 00 "
.1.3.6.1.2.1.17.7.1.4.3.1.4.1 "FF FF FF E0 00 00 00 00 "
.1.3.6.1.2.1.17.7.1.4.3.1.4.20 "00 00 00 10 00 00 00 00 "
.1.3.6.1.2.1.17.7.1.4.3.1.5.1 1
.1.3.6.1.2.1.17.7.1.4.3.1.5.20 1
""" + "".join(f".1.3.6.1.2.1.17.7.1.4.5.1.1.{p} {20 if p == 28 else 1}\n" for p in range(1, 37))


def test_state_from_walk():
    s = state_from_walk(parse_walk(WALK))
    assert s.vlans == {1: "DefaultVLAN", 20: "VOICE"}
    assert s.ports[1].untagged == {1} and s.ports[1].tagged == {20}
    assert s.ports[28].untagged == {20} and s.ports[28].tagged == set() and s.ports[28].pvid == 20
    assert s.ports[5].untagged == {1} and s.ports[5].tagged == set()
    assert len(s.ports) == 28 and s.bitmap_len == 8


def test_members():
    s = state_from_walk(parse_walk(WALK))
    egress, untagged = s.members(20)
    assert egress == {1, 28} and untagged == {28}


def test_parse_walk_joins_wrapped_hex():
    # net-snmp wraps hex strings longer than 16 bytes onto continuation lines.
    text = ('.1.3.6.1.2.1.17.7.1.4.3.1.1.30 "61 62 63 64 65 66 67 68 69 6A 6B 6C 6D 6E 6F 70 \n'
            '71 72 "\n.1.3.6.1.2.1.17.7.1.4.3.1.5.30 1\n')
    v = parse_walk(text)
    assert v["1.3.6.1.2.1.17.7.1.4.3.1.1.30"].split() == "61 62 63 64 65 66 67 68 69 6A 6B 6C 6D 6E 6F 70 71 72".split()
    assert v["1.3.6.1.2.1.17.7.1.4.3.1.5.30"] == "1"


def test_empty_name_and_double_untagged_port_survive_parsing():
    walk = (WALK.replace('"56 4F 49 43 45 "', '""')
            .replace('.4.3.1.4.1 "FF FF FF E0', '.4.3.1.4.1 "FF FF FF F0'))
    s = state_from_walk(parse_walk(walk))
    assert s.vlans[20] == ""
    assert s.ports[28].untagged == {1, 20}
