"""Everything that touches the switch runs on the router, the only host on the
switch's management network: one `ssh … bash -s` per call, the script on stdin."""
from __future__ import annotations

import base64
import ipaddress
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from swctl.errors import SwctlError
from swctl.model import parse_walk
from swctl.plan import Op

ROUTER = "chris@192.168.0.1"
DEADMAN = "swctl-deadman"
REVERT_FILE = "/tmp/swctl-revert.sh"
FIRED_MARKER = "/tmp/swctl-deadman-fired"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def load_snmp_conf(path: Path) -> str:
    try:
        conf = yaml.safe_load(path.read_bytes())["snmp_conf"]
    except (OSError, KeyError, TypeError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise SwctlError(f"{path}: cannot read snmp_conf ({e.__class__.__name__}) — is git-crypt unlocked?")
    return str(conf)


def snmp_prelude(snmp_conf: str) -> str:
    """Shell that points net-snmp at a private config holding the credentials,
    so they never reach a command line (ps) or a log."""
    return ("SWCTL_SNMP=$(mktemp -d); trap 'rm -rf \"$SWCTL_SNMP\"' EXIT\n"
            "cat > \"$SWCTL_SNMP/snmp.conf\" <<'SWCTL_CONF'\n"
            f"{snmp_conf.rstrip()}\n"
            "SWCTL_CONF\n"
            'export SNMPCONFPATH="$SWCTL_SNMP"\n')


def set_script(ops: list[Op], address: str, stop_on_error: bool) -> str:
    """One snmpset per op, reporting `OK i` or `FAIL i <error>` per line."""
    lines = []
    for i, op in enumerate(ops):
        cmd = f"snmpset -On -t3 -r1 {address} {op.oid} {op.type} {shlex.quote(op.value)}"
        on_fail = f'echo "FAIL {i} $(echo $out)"' + ("; exit 1" if stop_on_error else "")
        lines.append(f'if out=$({cmd} 2>&1); then echo "OK {i}"; else {on_fail}; fi')
    return "\n".join(lines) + "\n"


@dataclass
class Router:
    address: str                             # the switch, as the router reaches it
    snmp_conf: str
    host: str = ROUTER

    def __post_init__(self):
        try:
            self.address = str(ipaddress.IPv4Address(self.address))   # it goes unquoted into scripts
        except ValueError:
            raise SwctlError(f"switch address {self.address!r} is not an IPv4 address")

    def sh(self, script: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([*SSH, self.host, "bash", "-s"], input=script,
                                  capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            raise SwctlError(f"ssh {self.host}: timed out")

    def run(self, script: str) -> str:
        r = self.sh(script)
        if r.returncode != 0:
            raise SwctlError(f"on {self.host}: {(r.stderr or r.stdout).strip()[-400:]}")
        return r.stdout

    def walk_text(self, oids: list[str]) -> str:
        body = "".join(f"snmpbulkwalk -On -Oq -Ox -Oe -t3 -r1 {self.address} {o}\n" for o in oids)
        return self.run("set -e\n" + snmp_prelude(self.snmp_conf) + body)

    def walk(self, oids: list[str]) -> dict[str, str]:
        return parse_walk(self.walk_text(oids))

    def apply(self, ops: list[Op], stop_on_error: bool = True) -> list[str]:
        r = self.sh(snmp_prelude(self.snmp_conf) + set_script(ops, self.address, stop_on_error))
        return [l for l in r.stdout.splitlines() if l.startswith(("OK ", "FAIL "))]

    def deadman_armed(self) -> bool:
        """Armed = its timer is waiting or its revert is running."""
        r = self.sh(f"if systemctl is-active --quiet {DEADMAN}.timer || systemctl is-active --quiet {DEADMAN}.service; "
                    "then echo ARMED; else echo IDLE; fi\n")
        state = r.stdout.strip()
        if r.returncode != 0 or state not in ("ARMED", "IDLE"):
            raise SwctlError(f"cannot tell whether a dead-man is armed: {(r.stderr or r.stdout).strip()[-300:]}")
        return state == "ARMED"

    def arm_deadman(self, revert_ops: list[Op], minutes: int) -> None:
        # The first thing the revert does is leave a marker, so disarm can tell it fired.
        revert = ("#!/bin/bash\n"
                  f"date -u +%FT%TZ > {FIRED_MARKER}\n"
                  "# swctl dead-man: put the switch back as it was before an apply.\n"
                  f'logger -t swctl "dead-man firing: {len(revert_ops)} reverting writes"\n'
                  + snmp_prelude(self.snmp_conf)
                  + set_script(revert_ops, self.address, stop_on_error=False)
                  + f"rm -f {REVERT_FILE}\n")
        # Shipped base64: switch data (VLAN names) can't end a heredoc or reach the shell.
        payload = base64.b64encode(revert.encode()).decode()
        self.run("set -e\n"
                 f"if systemctl is-active --quiet {DEADMAN}.timer || systemctl is-active --quiet {DEADMAN}.service; "
                 "then echo 'dead-man already armed' >&2; exit 3; fi\n"
                 f"sudo systemctl reset-failed {DEADMAN}.service {DEADMAN}.timer 2>/dev/null || true\n"
                 f"sudo rm -f {FIRED_MARKER}\n"
                 f"umask 077\necho {payload} | base64 -d > {REVERT_FILE}\n"
                 f"sudo systemd-run --quiet --unit={DEADMAN} --on-active={int(minutes)}min /bin/bash {REVERT_FILE}\n"
                 f"systemctl is-active --quiet {DEADMAN}.timer\n")

    def disarm_deadman(self) -> None:
        """Stop the timer; if the revert already ran (or is running), say so loudly."""
        out = self.run(f"sudo systemctl stop {DEADMAN}.timer 2>/dev/null || true\n"
                       f"for i in $(seq 1 30); do systemctl is-active --quiet {DEADMAN}.service || break; sleep 1; done\n"
                       f"if [ -e {FIRED_MARKER} ]; then echo FIRED; sudo rm -f {FIRED_MARKER}; else echo DISARMED; fi\n"
                       f"rm -f {REVERT_FILE}\n"
                       f"! systemctl is-active --quiet {DEADMAN}.timer\n")
        if "FIRED" in out:
            raise SwctlError("the dead-man already fired: the router reverted the switch to its pre-apply state. "
                             "Check `swctl plan`.", exit_code=5)

    def responders(self, ips: list[str]) -> set[str]:
        """Which hosts answer ARP from the router (L2 reachability; ping can be filtered)."""
        body = ""
        for ip in ips:
            ip = str(ipaddress.IPv4Address(ip))
            body += (f'( dev=$(ip -o route get {ip} | sed -n "s/.* dev \\([^ ]*\\).*/\\1/p"); '
                     f'sudo arping -q -c 2 -w 3 -I "$dev" {ip} && echo {ip} ) &\n')
        if not body:
            return set()
        r = self.sh(body + "wait\necho SWCTL_CANARIES_DONE\n")
        words = r.stdout.split()
        if r.returncode != 0 or "SWCTL_CANARIES_DONE" not in words:
            raise SwctlError(f"canary check failed on the router: {(r.stderr or r.stdout).strip()[-300:]}")
        return set(words) - {"SWCTL_CANARIES_DONE"}

    def mgmt_mac(self) -> str:
        """The MAC of the router interface that reaches the switch."""
        out = self.run(f'dev=$(ip -o route get {self.address} | sed -n "s/.* dev \\([^ ]*\\).*/\\1/p")\n'
                       'cat "/sys/class/net/$dev/address"\n').strip().lower()
        if len(out.split(":")) != 6:
            raise SwctlError(f"cannot read the router's MAC towards {self.address}: {out!r}")
        return out

    def cli(self, commands: list[str], password: str) -> str:
        """Run whitelisted CLI commands on the switch via the router's python3."""
        src = Path(__file__).with_name("cli_remote.py").read_text()
        prelude = f"PASSWORD = {password!r}\nCOMMANDS = {commands!r}\nHOST_OVERRIDE = {self.address!r}\n"
        try:
            r = subprocess.run([*SSH, self.host, "python3", "-"], input=prelude + src,
                               capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise SwctlError(f"ssh {self.host}: CLI session timed out")
        if r.returncode != 0:
            raise SwctlError((r.stderr or r.stdout).strip()[-400:])
        return r.stdout
