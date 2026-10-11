"""switch/teg-30284.yaml <-> desired switch state."""
from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from swctl.errors import SwctlError
from swctl.model import PORTS, Port, SwitchState

HEADER = """\
# TRENDnet TEG-30284 core switch: desired VLAN/port state (switch/README.md).
# `swctl plan` diffs it against the switch; `swctl apply` converges it. Every VLAN
# and every port 1-28 is listed, and a port is in exactly the VLANs named here.
# `management` is the router's path to the switch: swctl never changes it.
# `canaries` must still answer ARP after every apply."""


@dataclass
class Desired:
    management_port: int
    management_vlan: int
    address: str
    state: SwitchState
    desc: dict[int, str]
    canaries: list[str] = field(default_factory=list)


def load(path: Path) -> Desired:
    try:
        doc = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise SwctlError(f"{path}: {e}")
    return from_doc(doc, str(path))


def from_doc(doc, where: str = "desired state") -> Desired:
    if not isinstance(doc, dict):
        raise SwctlError(f"{where}: not a mapping")
    problems: list[str] = []
    vlans: dict[int, str] = {}
    for vid, name in (doc.get("vlans") or {}).items():
        if not isinstance(vid, int) or not 1 <= vid <= 4094:
            problems.append(f"VLAN id {vid!r} is not 1-4094")
            continue
        name = "" if name is None else str(name)
        if len(name) > 32:
            problems.append(f"VLAN {vid} name longer than 32 characters")
        vlans[vid] = name
    if 1 not in vlans:
        problems.append("VLAN 1 must exist")

    ports_doc = doc.get("ports") or {}
    want = set(range(1, PORTS + 1))
    if set(ports_doc) != want:
        missing = sorted(want - set(ports_doc))
        unknown = sorted(map(str, set(ports_doc) - want))
        problems.append(f"ports must be exactly 1-{PORTS} (missing {missing}, unknown {unknown})")
    ports: dict[int, Port] = {}
    desc: dict[int, str] = {}
    for p, spec in ports_doc.items():
        if not isinstance(p, int) or not isinstance(spec, dict):
            continue
        u, tagged = spec.get("untagged"), spec.get("tagged") or []
        if not isinstance(u, int) or u not in vlans:
            problems.append(f"port {p}: untagged VLAN {u!r} is not in vlans")
        if not isinstance(tagged, list) or any(not isinstance(t, int) or t not in vlans for t in tagged):
            problems.append(f"port {p}: tagged {tagged!r} names a VLAN not in vlans")
            tagged = []
        if u in tagged:
            problems.append(f"port {p}: VLAN {u} is both untagged and tagged")
        ports[p] = Port(untagged={u}, tagged=set(tagged) - {u}, pvid=u if isinstance(u, int) else 0)
        desc[p] = str(spec.get("desc") or "")

    mgmt = doc.get("management") or {}
    mp, mv, address = mgmt.get("port"), mgmt.get("vlan"), mgmt.get("address")
    if mp not in ports:
        problems.append(f"management.port {mp!r} is not a port")
    elif ports[mp].untagged != {mv}:
        problems.append(f"management port {mp} must be untagged in the management VLAN {mv}")
    if not isinstance(address, str) or not address:
        problems.append("management.address is missing")
    if isinstance(mv, int) and mv != 1:
        others = sorted(p for p, port in ports.items() if p != mp and mv in port.untagged | port.tagged)
        if others:
            problems.append(f"management VLAN {mv} must be on port {mp} only, also on {others}")

    canaries = [str(c) for c in doc.get("canaries") or []]
    for c in canaries:
        try:
            ipaddress.IPv4Address(c)
        except ValueError:
            problems.append(f"canary {c!r} is not an IPv4 address")

    if problems:
        raise SwctlError(f"{where}:\n  " + "\n  ".join(problems))
    return Desired(mp, mv, address, SwitchState(vlans=vlans, ports=ports), desc, canaries)


def dump(d: Desired) -> str:
    lines = [HEADER,
             f"management: {{port: {d.management_port}, vlan: {d.management_vlan}, address: {d.address}}}",
             f"canaries: [{', '.join(d.canaries)}]",
             "vlans:"]
    lines += [f"  {vid}: {json.dumps(name)}" for vid, name in sorted(d.state.vlans.items())]
    lines.append("ports:")
    for p, port in sorted(d.state.ports.items()):
        (u,) = port.untagged
        parts = [f"untagged: {u}"]
        if port.tagged:
            parts.append(f"tagged: [{', '.join(map(str, sorted(port.tagged)))}]")
        if d.desc.get(p):
            parts.append(f"desc: {json.dumps(d.desc[p])}")
        lines.append(f"  {p}: {{{', '.join(parts)}}}")
    return "\n".join(lines) + "\n"
