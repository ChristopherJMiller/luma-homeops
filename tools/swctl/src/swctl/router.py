"""Everything that touches the switch runs on the router, the only host on the
switch's management network: one `ssh … bash -s` per call, the script on stdin."""
from __future__ import annotations

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
        return self.sh(f"systemctl is-active --quiet {DEADMAN}.timer").returncode == 0

    def arm_deadman(self, revert_ops: list[Op], minutes: int) -> None:
        revert = ("#!/bin/bash\n# swctl dead-man: put the switch back as it was before an apply.\n"
                  f'logger -t swctl "dead-man firing: {len(revert_ops)} reverting writes"\n'
                  + snmp_prelude(self.snmp_conf)
                  + set_script(revert_ops, self.address, stop_on_error=False)
                  + f"rm -f {REVERT_FILE}\n")
        self.run("set -e\n"
                 f"if systemctl is-active --quiet {DEADMAN}.timer; then echo 'dead-man already armed' >&2; exit 3; fi\n"
                 f"sudo systemctl reset-failed {DEADMAN}.service {DEADMAN}.timer 2>/dev/null || true\n"
                 f"umask 077\ncat > {REVERT_FILE} <<'SWCTL_REVERT'\n{revert}SWCTL_REVERT\n"
                 f"sudo systemd-run --quiet --unit={DEADMAN} --on-active={int(minutes)}min /bin/bash {REVERT_FILE}\n"
                 f"systemctl is-active --quiet {DEADMAN}.timer\n")

    def disarm_deadman(self) -> None:
        self.run(f"sudo systemctl stop {DEADMAN}.timer 2>/dev/null || true\nrm -f {REVERT_FILE}\n"
                 f"! systemctl is-active --quiet {DEADMAN}.timer\n")

    def responders(self, ips: list[str]) -> set[str]:
        """Which hosts answer ARP from the router (L2 reachability; ping can be filtered)."""
        body = ""
        for ip in ips:
            ip = str(ipaddress.IPv4Address(ip))
            body += (f'( dev=$(ip -o route get {ip} | sed -n "s/.* dev \\([^ ]*\\).*/\\1/p"); '
                     f'sudo arping -q -c 2 -w 3 -I "$dev" {ip} && echo {ip} ) &\n')
        return set(self.sh(body + "wait\n").stdout.split()) if body else set()

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
