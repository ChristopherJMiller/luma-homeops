"""A switch that stores what the TEG-30284 stores (per-VLAN egress/untagged
bitmaps, per-port PVID) and interprets swctl's SNMP writes the same way."""
from __future__ import annotations

from swctl import bitmap
from swctl.model import PORTS, PVID, VLAN_EGRESS, VLAN_NAME, VLAN_STATUS, VLAN_UNTAGGED, Port, SwitchState, parse_walk, state_from_walk


class FakeSwitch:
    def __init__(self, state: SwitchState):
        self.vlans = {vid: [name, *state.members(vid)] for vid, name in state.vlans.items()}  # vid -> [name, egress, untagged]
        self.pvid = {p: port.pvid for p, port in state.ports.items()}

    def write(self, op) -> None:
        col, idx = op.oid.rsplit(".", 1)
        idx = int(idx)
        if col == VLAN_STATUS and op.value == "4":
            if idx in self.vlans:
                raise RuntimeError(f"VLAN {idx} exists")
            self.vlans[idx] = ["", set(), set()]
        elif col == VLAN_STATUS and op.value == "6":
            if idx not in self.vlans:
                raise RuntimeError(f"no VLAN {idx}")
            del self.vlans[idx]
        elif idx not in self.vlans and col != PVID:
            raise RuntimeError(f"no VLAN {idx}")
        elif col == VLAN_NAME:
            self.vlans[idx][0] = op.value
        elif col == VLAN_EGRESS:
            self.vlans[idx][1] = bitmap.decode(op.value)
            self.vlans[idx][2] &= self.vlans[idx][1]
        elif col == VLAN_UNTAGGED:
            self.vlans[idx][2] = bitmap.decode(op.value)
            self.vlans[idx][1] |= self.vlans[idx][2]
        elif col == PVID:
            self.pvid[idx] = int(op.value)
        else:
            raise RuntimeError(f"unexpected write {op}")

    def walk_text(self) -> str:
        out = []
        for vid, (name, egress, untagged) in sorted(self.vlans.items()):
            hexname = " ".join(f"{b:02X}" for b in name.encode())
            out += [f'.{VLAN_NAME}.{vid} "{hexname}"', f'.{VLAN_EGRESS}.{vid} "{bitmap.encode(egress, 8)}"',
                    f'.{VLAN_UNTAGGED}.{vid} "{bitmap.encode(untagged, 8)}"', f".{VLAN_STATUS}.{vid} 1"]
        out += [f".{PVID}.{p} {v}" for p, v in sorted(self.pvid.items())]
        return "\n".join(out) + "\n"

    def state(self) -> SwitchState:
        return state_from_walk(parse_walk(self.walk_text()))


def flat(vlans=None, overrides=None) -> SwitchState:
    """All 28 ports untagged in VLAN 1, then per-port overrides {p: (untagged set, tagged set, pvid)}."""
    ports = {p: Port({1}, set(), 1) for p in range(1, PORTS + 1)}
    for p, (u, t, pv) in (overrides or {}).items():
        ports[p] = Port(set(u), set(t), pv)
    return SwitchState(vlans=vlans or {1: "DefaultVLAN"}, ports=ports)
