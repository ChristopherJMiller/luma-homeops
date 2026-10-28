"""galaxy's nodes and their BMCs (ASRock Rack X570D4I-2T, AST2500, AMI MegaRAC 1.90)."""
from __future__ import annotations

from dataclasses import dataclass

from bmcctl.errors import BmcctlError


@dataclass(frozen=True)
class Node:
    name: str
    ip: str    # Talos / Kubernetes address (DHCP reservation by MAC, router/ansible)
    bmc: str   # BMC address


NODES = (
    Node("top", "192.168.0.240", "192.168.0.119"),
    Node("middle", "192.168.0.241", "192.168.0.117"),
    Node("bottom", "192.168.0.242", "192.168.0.151"),
)


def node(name: str) -> Node:
    for n in NODES:
        if n.name == name:
            return n
    raise BmcctlError(f"unknown node {name!r} (known: {', '.join(n.name for n in NODES)})")
