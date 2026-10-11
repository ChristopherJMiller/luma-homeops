"""swctl subcommands."""
from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path

import yaml

from swctl import desired as desired_mod
from swctl.desired import Desired
from swctl.errors import SwctlError
from swctl.inventory import FDB_PORT, IF_OPER, IF_SPEED, LEASES_CMD, NEIGH_CMD, device, devices_by_port, fdb, lease_names, neighbours
from swctl.model import STATE_OIDS, Port, SwitchState, rows, state_from_walk
from swctl.plan import changed_ports, check_management, diff
from swctl.router import Router, load_snmp_conf

REPO = Path(__file__).resolve().parents[4]
DESIRED = REPO / "switch/teg-30284.yaml"
CREDENTIALS = REPO / "switch/credentials.secret.yaml"
BACKUP = REPO / "switch/backups/teg-30284.snmp"
# The management path since the 2026-10-11 cut-over: router eth3 <-> port 2, VLAN 99.
DEFAULT_MANAGEMENT = (2, 99, "192.168.99.2")
BACKUP_OIDS = ["1.3.6.1.2.1.1.1", "1.3.6.1.2.1.31.1.1.1.1", "1.3.6.1.2.1.31.1.1.1.18", "1.3.6.1.2.1.17.7.1.4"]


def make_router(address: str) -> Router:
    return Router(address, load_snmp_conf(CREDENTIALS))


def read_state(r) -> SwitchState:
    return state_from_walk(r.walk(STATE_OIDS))


def _devices(r) -> dict:
    return devices_by_port(fdb(r.walk([FDB_PORT])), neighbours(r.run(NEIGH_CMD)), lease_names(r.run(LEASES_CMD)))


def _address(path: Path) -> str:
    return desired_mod.load(path).address if path.exists() else DEFAULT_MANAGEMENT[2]


def cmd_status(args) -> int:
    r = make_router(_address(args.file))
    live = read_state(r)
    link = r.walk([IF_OPER, IF_SPEED])
    oper, speed = rows(link, IF_OPER), rows(link, IF_SPEED)
    devs = _devices(r)
    print(f"{'port':>4}  {'link':<9} {'untagged':<9} {'pvid':<5} {'tagged':<11} devices")
    for p, port in sorted(live.ports.items()):
        mbps = int(speed.get(p, "0") or 0)
        up = oper.get(p) == "1"
        state = (f"up {mbps // 1000}G" if mbps >= 1000 else f"up {mbps}M") if up else "down"
        names = ", ".join(d.label() for d in devs.get(p, []))
        print(f"{p:>4}  {state:<9} {','.join(map(str, sorted(port.untagged))):<9} {port.pvid:<5} "
              f"{','.join(map(str, sorted(port.tagged))) or '-':<11} {names}")
    print("vlans: " + ", ".join(f"{vid} {name!r}" for vid, name in sorted(live.vlans.items())))
    return 0


def cmd_import(args) -> int:
    old = desired_mod.load(args.file) if args.file.exists() else None
    mp, mv, address = (old.management_port, old.management_vlan, old.address) if old else DEFAULT_MANAGEMENT
    r = make_router(address)
    live = read_state(r)
    bad = {p: sorted(port.untagged) for p, port in live.ports.items() if len(port.untagged) != 1}
    if bad:
        raise SwctlError("ports not untagged in exactly one VLAN — fix on the switch first: "
                         + "; ".join(f"port {p} {v}" for p, v in sorted(bad.items())))
    for p, port in sorted(live.ports.items()):
        if {port.pvid} != port.untagged:
            print(f"note: port {p} PVID {port.pvid} differs from its untagged VLAN {sorted(port.untagged)}; "
                  "the file uses the untagged VLAN, so `swctl plan` will fix the PVID")
    if old:
        desc = old.desc
    else:
        desc = {}
        for p, ds in _devices(r).items():
            labels = [d.label() for d in ds]
            desc[p] = ", ".join(labels[:3]) + (f" +{len(labels) - 3} more" if len(labels) > 3 else "")
    ports = {p: Port(set(port.untagged), set(port.tagged), next(iter(port.untagged))) for p, port in live.ports.items()}
    d = Desired(mp, mv, address, SwitchState(vlans=live.vlans, ports=ports), desc, old.canaries if old else [])
    text = desired_mod.dump(d)
    desired_mod.from_doc(yaml.safe_load(text), "imported state")       # must round-trip
    args.file.parent.mkdir(parents=True, exist_ok=True)
    args.file.write_text(text)
    print(f"wrote {args.file}")
    return 0


def cmd_plan(args) -> int:
    d = desired_mod.load(args.file)
    r = make_router(d.address)
    live = read_state(r)
    check_management(d, live)
    ops = diff(live, d.state)
    if not ops:
        print("switch: in sync")
        return 0
    for op in ops:
        print(f"  {op.why}")
    print(f"switch: {len(ops)} writes (swctl apply)")
    return 2


def cmd_backup(args) -> int:
    d = desired_mod.load(args.file)
    raw = make_router(d.address).walk_text(BACKUP_OIDS)
    text = "".join(line.rstrip() + "\n" for line in raw.splitlines())   # net-snmp pads hex; the repo's hooks don't
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    BACKUP.write_text("# swctl backup: sysDescr, ifName, ifAlias, Q-BRIDGE dot1qVlan (snmpbulkwalk -On -Oq -Ox -Oe)\n" + text)
    print(f"wrote {BACKUP}")
    return 0


def admin_password() -> str:
    try:
        return str(yaml.safe_load(CREDENTIALS.read_bytes())["admin_password"])
    except (OSError, KeyError, TypeError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise SwctlError(f"{CREDENTIALS}: cannot read admin_password ({e.__class__.__name__}) — is git-crypt unlocked?")


def cmd_save(args) -> int:
    d = desired_mod.load(args.file)
    r = make_router(d.address)
    if diff(read_state(r), d.state):
        raise SwctlError("the switch does not match the file; save only a verified state (swctl plan)")
    print(r.cli(["save"], admin_password()))
    return 0


def cmd_disarm(args) -> int:
    r = make_router(_address(args.file))
    if not r.deadman_armed():
        print("no dead-man armed")
        return 0
    r.disarm_deadman()
    print("dead-man disarmed — check `swctl plan` shows what you expect")
    return 0


def canary_ips(r, d: Desired, changed: dict[int, bool]) -> list[str]:
    """The file's canaries plus every device on a changed port that should not move."""
    ips = set(d.canaries)
    staying = [p for p, moves in changed.items() if not moves]
    if staying:
        ports, neigh, names = fdb(r.walk([FDB_PORT])), neighbours(r.run(NEIGH_CMD)), lease_names(r.run(LEASES_CMD))
        ips |= {dev.ip for p in staying for mac in ports.get(p, []) if (dev := device(mac, neigh, names)).ip}
    return sorted(ips, key=ipaddress.IPv4Address)


def revert_to(r, snapshot: SwitchState) -> None:
    """Converge back to `snapshot` from wherever the switch really is."""
    for _ in range(2):
        ops = diff(read_state(r), snapshot)
        if not ops:
            print("reverted: the switch matches its state before this apply")
            return
        r.apply(ops, stop_on_error=False)
    raise SwctlError("REVERT INCOMPLETE: the switch differs from its pre-apply state. The dead-man is still "
                     "armed and will retry; check `swctl plan`.", exit_code=4)


def cmd_apply(args) -> int:
    d = desired_mod.load(args.file)
    r = make_router(d.address)
    if r.deadman_armed():
        raise SwctlError("a dead-man is already armed: an earlier apply did not finish. "
                         "Check `swctl status`/`plan`, then `swctl disarm`.", exit_code=3)
    live = read_state(r)
    check_management(d, live)
    ops = diff(live, d.state)
    if not ops:
        print("switch: in sync")
        return 0
    for op in ops:
        print(f"  {op.why}")
    canaries = canary_ips(r, d, changed_ports(live, d.state))
    answering = r.responders(canaries)
    print(f"canaries answering ARP: {', '.join(sorted(answering, key=ipaddress.IPv4Address)) or 'none'}")
    r.arm_deadman(diff(d.state, live), args.deadman)
    print(f"dead-man armed: the router reverts these writes in {args.deadman} min unless this run confirms")
    results = r.apply(ops)
    if args.simulate_session_loss:
        print("simulated session loss: leaving the dead-man armed")
        return 1
    problems = []
    for line in results:
        if line.startswith("FAIL"):
            _, i, *msg = line.split(" ", 2)
            problems.append(f"write failed: {ops[int(i)].why}: {' '.join(msg)}")
    if len(results) < len(ops) and not problems:
        problems.append(f"only {len(results)} of {len(ops)} writes reported (ssh?)")
    if not problems:
        left = diff(read_state(r), d.state)
        if left:
            problems.append("the switch does not match the file: " + "; ".join(op.why for op in left))
        lost = answering - r.responders(sorted(answering, key=ipaddress.IPv4Address))
        if lost:
            problems.append(f"no longer answering ARP: {', '.join(sorted(lost))}")
    if problems:
        print("verify FAILED:\n  " + "\n  ".join(problems) + "\nreverting now")
        revert_to(r, live)
        r.disarm_deadman()
        raise SwctlError("apply reverted; the switch is as it was")
    r.disarm_deadman()
    print(f"switch: {len(ops)} writes applied and verified, dead-man disarmed. "
          "Not saved: `swctl save` once it has settled (a reboot returns to the saved config).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swctl", description="TEG-30284 core switch as code (switch/README.md)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, func, text in [("status", cmd_status, "ports, links, VLANs and the devices behind each port"),
                             ("import", cmd_import, "write the desired-state file from the live switch"),
                             ("plan", cmd_plan, "diff the file against the switch (exit 2 = changes)"),
                             ("backup", cmd_backup, "raw SNMP dump to switch/backups/"),
                             ("disarm", cmd_disarm, "stop a dead-man left by a crashed apply"),
                             ("save", cmd_save, "save the running config (only when it matches the file)")]:
        p = sub.add_parser(name, help=text)
        p.add_argument("--file", type=Path, default=DESIRED, help="desired-state file")
        p.set_defaults(func=func)
    p = sub.add_parser("apply", help="converge the switch to the file, with a router dead-man")
    p.add_argument("--file", type=Path, default=DESIRED, help="desired-state file")
    p.add_argument("--deadman", type=int, default=5, metavar="MIN", help="router reverts after MIN minutes unless confirmed")
    p.add_argument("--simulate-session-loss", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_apply)
    return parser


def main_for_tests(argv) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args) or 0
