"""Who is behind each port: the switch's MAC table joined with the router's
neighbour table and DHCP leases."""
from __future__ import annotations

from dataclasses import dataclass

FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"      # dot1qTpFdbPort, index fdbId.mac[6]
IF_OPER = "1.3.6.1.2.1.2.2.1.8"              # 1 up, 2 down
IF_SPEED = "1.3.6.1.2.1.31.1.1.1.15"         # ifHighSpeed, Mb/s
NEIGH_CMD = "ip -4 neigh show"
LEASES_CMD = "/opt/vyatta/bin/vyatta-op-cmd-wrapper show dhcp server leases"


@dataclass(frozen=True)
class Device:
    mac: str
    ip: str = ""
    name: str = ""

    def label(self) -> str:
        where = f".{self.ip.rsplit('.', 1)[1]}" if self.ip.startswith("192.168.0.") else self.ip
        return " ".join(x for x in (self.name or self.mac, where) if x)


def fdb(values: dict[str, str]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    prefix = FDB_PORT + "."
    for oid, port in values.items():
        parts = oid[len(prefix):].split(".") if oid.startswith(prefix) else []
        if len(parts) == 7 and port.isdigit():
            out.setdefault(int(port), []).append(":".join(f"{int(x):02x}" for x in parts[1:]))
    return out


def neighbours(text: str) -> dict[str, list[str]]:
    """mac -> every IPv4 the router has seen it answer for (nodes also answer
    for MetalLB/VIP addresses)."""
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        f = line.split()
        if "lladdr" in f and f.index("lladdr") + 1 < len(f):
            out.setdefault(f[f.index("lladdr") + 1].lower(), []).append(f[0])
    return out


def lease_names(text: str) -> dict[str, str]:
    """VyOS `show dhcp server leases` -> ip -> hostname, by header column position."""
    lines = text.splitlines()
    header = next((l for l in lines if l.startswith("IP Address")), "")
    if "Hostname" not in header:
        return {}
    start, end = header.index("Hostname"), (header.index("Origin") if "Origin" in header else None)
    out = {}
    for line in lines:
        ip = line.split()[0] if line.split() else ""
        name = line[start:end].strip() if len(line) > start else ""
        if ip[:1].isdigit() and name:
            out[ip] = name
    return out


def device(mac: str, neigh: dict[str, list[str]], names: dict[str, str]) -> Device:
    """A MAC with its own address: the one with a DHCP lease, else the first seen."""
    ips = neigh.get(mac, [])
    ip = next((i for i in ips if i in names), ips[0] if ips else "")
    return Device(mac, ip, names.get(ip, ""))


def devices_by_port(fdb_ports: dict[int, list[str]], neigh: dict[str, list[str]], names: dict[str, str]) -> dict[int, list[Device]]:
    return {port: [device(mac, neigh, names) for mac in sorted(macs)] for port, macs in fdb_ports.items()}
