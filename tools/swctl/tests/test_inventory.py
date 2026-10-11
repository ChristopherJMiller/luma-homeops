from swctl.inventory import FDB_PORT, Device, devices_by_port, fdb, lease_names, neighbours

LEASES = """\
IP Address     MAC address        State    Lease start          Lease expiration     Remaining    Pool    Hostname                  Origin
-------------  -----------------  -------  -------------------  -------------------  -----------  ------  ------------------------  --------
192.168.0.2    12:35:74:d5:7a:71  active   2026/10/11 15:58:50  2026/10/12 15:58:50  18:03:24     LAN     kvm                       local
192.168.0.58   00:18:dd:09:93:7c  active   2026/10/11 15:58:50  2026/10/12 15:58:50  18:03:24     LAN                               local
"""


def test_fdb_parses_index_into_mac():
    v = {f"{FDB_PORT}.1.0.24.221.9.147.124": "10", f"{FDB_PORT}.1.18.53.116.213.122.113": "8"}
    assert fdb(v) == {10: ["00:18:dd:09:93:7c"], 8: ["12:35:74:d5:7a:71"]}


def test_neighbours_skip_failed():
    text = "192.168.0.45 dev eth2 FAILED\n192.168.0.58 dev eth2 lladdr 00:18:DD:09:93:7C REACHABLE\n"
    assert neighbours(text) == {"00:18:dd:09:93:7c": ["192.168.0.58"]}


def test_lease_names_by_column():
    assert lease_names(LEASES) == {"192.168.0.2": "kvm"}


def test_devices_and_labels():
    devs = devices_by_port({8: ["12:35:74:d5:7a:71"], 10: ["00:18:dd:09:93:7c"], 1: ["20:7c:14:f2:f4:fb"]},
                           {"12:35:74:d5:7a:71": ["192.168.0.2"], "00:18:dd:09:93:7c": ["192.168.0.58"]},
                           {"192.168.0.2": "kvm"})
    assert [d.label() for d in devs[8]] == ["kvm .2"]
    assert [d.label() for d in devs[10]] == ["00:18:dd:09:93:7c .58"]
    assert devs[1] == [Device("20:7c:14:f2:f4:fb")] and devs[1][0].label() == "20:7c:14:f2:f4:fb"


def test_device_prefers_its_leased_address():
    # A node also answers for MetalLB / VIP addresses; its own address is the leased one.
    neigh = neighbours("192.168.0.7 dev eth2 lladdr 9c:6b:00:70:58:bd REACHABLE\n"
                       "192.168.0.240 dev eth2 lladdr 9c:6b:00:70:58:bd STALE\n")
    assert neigh == {"9c:6b:00:70:58:bd": ["192.168.0.7", "192.168.0.240"]}
    devs = devices_by_port({27: ["9c:6b:00:70:58:bd"]}, neigh, {"192.168.0.240": "top"})
    assert devs[27][0] == Device("9c:6b:00:70:58:bd", "192.168.0.240", "top") and devs[27][0].label() == "top .240"
