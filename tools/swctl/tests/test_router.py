import base64
import subprocess

import pytest

from swctl.errors import SwctlError
from swctl.plan import Op
from swctl.router import Router, set_script, snmp_prelude


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


def test_router_rejects_a_non_ipv4_switch_address():
    with pytest.raises(SwctlError, match="not an IPv4 address"):
        Router("192.168.99.2; id", "defCommunity x")


def test_arm_deadman_ships_the_revert_script_base64(monkeypatch):
    seen = []
    r = Router("192.168.99.2", "defCommunity s3cret")
    monkeypatch.setattr(r, "run", lambda script: seen.append(script) or "")
    evil = Op("1.3.6.1.2.1.17.7.1.4.3.1.1.20", "s", "x\nSWCTL_REVERT\ntouch /tmp/PWNED", "name")
    r.arm_deadman([evil], 5)
    script = seen[0]
    assert "PWNED" not in script and "s3cret" not in script and "<<" not in script
    payload = next(w for w in script.split() if len(w) > 40)
    revert = base64.b64decode(payload).decode()
    assert revert.splitlines()[1].startswith("date") and "PWNED" in revert   # marker first, name only inside


def test_disarm_reports_a_deadman_that_already_fired(monkeypatch):
    r = Router("192.168.99.2", "")
    monkeypatch.setattr(r, "run", lambda script: "FIRED\n")
    with pytest.raises(SwctlError, match="already fired") as e:
        r.disarm_deadman()
    assert e.value.exit_code == 5


def test_deadman_armed_raises_when_ssh_fails(monkeypatch):
    r = Router("192.168.99.2", "")
    monkeypatch.setattr(r, "sh", lambda script: subprocess.CompletedProcess([], 255, "", "ssh: connect"))
    with pytest.raises(SwctlError, match="cannot tell"):
        r.deadman_armed()


def test_responders_raise_when_ssh_fails(monkeypatch):
    r = Router("192.168.99.2", "")
    monkeypatch.setattr(r, "sh", lambda script: subprocess.CompletedProcess([], 255, "", "ssh: connect"))
    with pytest.raises(SwctlError, match="canary check"):
        r.responders(["192.168.0.240"])
