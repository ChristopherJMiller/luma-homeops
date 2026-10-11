"""Switch state (VLANs, per-port membership) as the Q-BRIDGE MIB stores it."""
from __future__ import annotations

from dataclasses import dataclass

from swctl import bitmap

PORTS = 28                                   # TEG-30284: 24x1G + 4x10G SFP+ (25-28)
QB = "1.3.6.1.2.1.17.7.1.4"                  # Q-BRIDGE-MIB dot1qVlan
VLAN_NAME = f"{QB}.3.1.1"
VLAN_EGRESS = f"{QB}.3.1.2"                  # every member, tagged or untagged
VLAN_UNTAGGED = f"{QB}.3.1.4"
VLAN_STATUS = f"{QB}.3.1.5"                  # RowStatus: 4 createAndGo, 6 destroy
PVID = f"{QB}.5.1.1"
STATE_OIDS = [f"{QB}.3.1", PVID]


@dataclass
class Port:
    untagged: set[int]                       # the switch allows several; a desired port has one
    tagged: set[int]
    pvid: int


@dataclass
class SwitchState:
    vlans: dict[int, str]                    # vid -> name
    ports: dict[int, Port]                   # 1..PORTS
    bitmap_len: int = 8                      # bytes in this switch's PortLists

    def members(self, vid: int) -> tuple[set[int], set[int]]:
        """(egress, untagged) port sets of a VLAN, the way the switch stores them."""
        untagged = {p for p, port in self.ports.items() if vid in port.untagged}
        tagged = {p for p, port in self.ports.items() if vid in port.tagged}
        return untagged | tagged, untagged


def parse_walk(text: str) -> dict[str, str]:
    """`snmpbulkwalk -On -Oq -Ox -Oe` output -> {oid: value}. Octet strings stay
    hex; net-snmp's continuation lines (hex wrapped at 16 bytes) are joined."""
    values: dict[str, str] = {}
    last = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("."):
            oid, _, value = line.partition(" ")
            last = oid[1:]
            values[last] = value
        elif line and last is not None:
            values[last] += " " + line
    return {oid: " ".join(v.replace('"', " ").split()) for oid, v in values.items()}


def rows(values: dict[str, str], prefix: str) -> dict[int, str]:
    """The single-integer-indexed rows of one column."""
    p = prefix + "."
    return {int(k[len(p):]): v for k, v in values.items() if k.startswith(p) and k[len(p):].isdigit()}


def state_from_walk(values: dict[str, str], nports: int = PORTS) -> SwitchState:
    egress_hex = rows(values, VLAN_EGRESS)
    egress = {vid: bitmap.decode(h) for vid, h in egress_hex.items()}
    untagged = {vid: bitmap.decode(h) for vid, h in rows(values, VLAN_UNTAGGED).items()}
    names = rows(values, VLAN_NAME)
    pvids = {p: int(v) for p, v in rows(values, PVID).items()}
    vids = set(rows(values, VLAN_STATUS)) | set(egress)
    vlans = {vid: bytes.fromhex(names.get(vid, "").replace(" ", "")).decode(errors="replace") for vid in vids}
    ports = {}
    for p in range(1, nports + 1):
        u = {vid for vid, members in untagged.items() if p in members}
        t = {vid for vid, members in egress.items() if p in members} - u
        ports[p] = Port(untagged=u, tagged=t, pvid=pvids.get(p, 0))
    lengths = {len(h.split()) for h in egress_hex.values()}
    return SwitchState(vlans=vlans, ports=ports, bitmap_len=max(lengths, default=8))
