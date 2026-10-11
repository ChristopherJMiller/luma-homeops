from swctl.plan import Op
from swctl.router import set_script, snmp_prelude


def test_prelude_keeps_credentials_off_the_command_line():
    s = snmp_prelude("defVersion 2c\ndefCommunity s3cret\n")
    assert "SNMPCONFPATH" in s and "<<'SWCTL_CONF'" in s
    assert [l for l in s.splitlines() if "s3cret" in l] == ["defCommunity s3cret"]   # only inside the heredoc


def test_set_script_quotes_and_numbers_ops():
    ops = [Op("1.3.6.1.2.1.17.7.1.4.3.1.1.20", "s", "my vlan; rm -rf /", "name"),
           Op("1.3.6.1.2.1.17.7.1.4.3.1.2.20", "x", "80 00 00 00 00 00 00 00", "egress")]
    s = set_script(ops, "192.168.99.2", stop_on_error=True)
    assert "'my vlan; rm -rf /'" in s and "'80 00 00 00 00 00 00 00'" in s
    assert 'echo "OK 0"' in s and 'echo "OK 1"' in s and s.count("exit 1") == 2
    assert "exit 1" not in set_script(ops, "192.168.99.2", stop_on_error=False)
