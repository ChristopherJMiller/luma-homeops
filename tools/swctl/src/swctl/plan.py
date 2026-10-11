"""Desired vs live -> ordered SNMP writes; the management-path guard."""
from __future__ import annotations

from dataclasses import dataclass

from swctl import bitmap
from swctl.errors import SwctlError
from swctl.model import PVID, VLAN_EGRESS, VLAN_NAME, VLAN_STATUS, VLAN_UNTAGGED, SwitchState

CREATE_AND_GO, DESTROY = 4, 6


@dataclass(frozen=True)
class Op:
    oid: str
    type: str                                # snmpset type letter: i, u, s, x
    value: str
    why: str


def _ports(ps: set[int]) -> str:
    return ",".join(map(str, sorted(ps))) or "none"


def diff(current: SwitchState, target: SwitchState) -> list[Op]:
    """The writes that turn `current` into `target`, ordered so no port is ever
    without an untagged VLAN: create VLANs, add members, set PVIDs, remove
    members, delete VLANs. Every write is absolute, so a diff from a half-applied
    state converges too."""
    n = current.bitmap_len
    ops: list[Op] = []
    for vid in sorted(set(target.vlans) - set(current.vlans)):
        ops.append(Op(f"{VLAN_STATUS}.{vid}", "i", str(CREATE_AND_GO), f"create VLAN {vid}"))
    for vid, name in sorted(target.vlans.items()):
        if current.vlans.get(vid, "") != name:
            ops.append(Op(f"{VLAN_NAME}.{vid}", "s", name, f"VLAN {vid}: name {name!r}"))
    removals: list[Op] = []
    for vid in sorted(target.vlans):
        cur_e, cur_u = current.members(vid) if vid in current.vlans else (set(), set())
        tgt_e, tgt_u = target.members(vid)
        mid_e, mid_u = cur_e | tgt_e, cur_u | tgt_u
        if mid_e != cur_e:
            ops.append(Op(f"{VLAN_EGRESS}.{vid}", "x", bitmap.encode(mid_e, n), f"VLAN {vid}: add ports {_ports(mid_e - cur_e)}"))
        if mid_u != cur_u:
            ops.append(Op(f"{VLAN_UNTAGGED}.{vid}", "x", bitmap.encode(mid_u, n), f"VLAN {vid}: ports {_ports(mid_u - cur_u)} untagged"))
        if tgt_u != mid_u:
            removals.append(Op(f"{VLAN_UNTAGGED}.{vid}", "x", bitmap.encode(tgt_u, n), f"VLAN {vid}: ports {_ports(mid_u - tgt_u)} no longer untagged"))
        if tgt_e != mid_e:
            removals.append(Op(f"{VLAN_EGRESS}.{vid}", "x", bitmap.encode(tgt_e, n), f"VLAN {vid}: remove ports {_ports(mid_e - tgt_e)}"))
    for p, port in sorted(target.ports.items()):
        if current.ports[p].pvid != port.pvid:
            ops.append(Op(f"{PVID}.{p}", "u", str(port.pvid), f"port {p}: PVID {current.ports[p].pvid} -> {port.pvid}"))
    ops += removals
    for vid in sorted(set(current.vlans) - set(target.vlans)):
        ops.append(Op(f"{VLAN_STATUS}.{vid}", "i", str(DESTROY), f"delete VLAN {vid}"))
    return ops


def check_management(d, live: SwitchState) -> None:
    """The live management path must be exactly what the file declares; the
    file itself is validated to keep it (desired.from_doc)."""
    mp, mv = d.management_port, d.management_vlan
    port = live.ports.get(mp)
    if mv in live.vlans and port is not None and port.untagged == {mv} and port.pvid == mv:
        return
    seen = f"untagged {sorted(port.untagged)}, PVID {port.pvid}" if port else "no such port"
    raise SwctlError(
        f"the live switch does not have management port {mp} untagged in VLAN {mv} with PVID {mv} ({seen}). "
        "The management path changes only by hand (switch/README.md); refusing to write.", exit_code=3)


def changed_ports(current: SwitchState, target: SwitchState) -> dict[int, bool]:
    """port -> True when its untagged VLAN changes (its devices are expected to move)."""
    out = {}
    for p, t in sorted(target.ports.items()):
        c = current.ports[p]
        if (c.untagged, c.tagged, c.pvid) != (t.untagged, t.tagged, t.pvid):
            out[p] = c.untagged != t.untagged
    return out
