"""Install media: the Talos ISO for nodes/controlplane.yaml, served to the BMCs.

The ISO is derived, not stored: `machine.install.image` names the Image Factory
schematic and version, and the factory builds the matching ISO. It is cached
under ~/.cache and served read-only over HTTP by darkhttpd (flake-pinned
nixpkgs) in a systemd *user* unit. The only root step is the firewall: rowlett's
NixOS firewall admits nothing on the media port, so a root unit inserts an
ACCEPT for the three BMC addresses and removes it when it stops. Both units stop
themselves after `hours`.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from bmcctl.errors import BmcctlError
from bmcctl.nodes import NODES

PORT = 8740
HTTP_UNIT = "bmc-media-http"
FW_UNIT = "bmc-media-fw"
IPTABLES = "/run/current-system/sw/bin/iptables"
SLEEP = "/run/current-system/sw/bin/sleep"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "luma-homeops" / "install-media"

_INSTALLER = re.compile(r"^\s*image:\s*factory\.talos\.dev/installer/([0-9a-f]{64}):(v[0-9][\w.\-]*)", re.M)


@dataclass(frozen=True)
class Iso:
    schematic: str
    version: str

    @property
    def name(self) -> str:
        return f"talos-{self.version}-{self.schematic[:8]}-metal-amd64.iso"

    @property
    def url(self) -> str:
        return f"https://factory.talos.dev/image/{self.schematic}/{self.version}/metal-amd64.iso"

    @property
    def path(self) -> Path:
        return CACHE / self.name


def iso_for(machine_config: Path) -> Iso:
    """The ISO matching `machine.install.image` (the factory installer) in a machine config."""
    found = _INSTALLER.findall(machine_config.read_text())
    if len(found) != 1:
        raise BmcctlError(f"{machine_config}: expected one factory.talos.dev/installer image, found {len(found)}")
    return Iso(*found[0])


def is_iso(path: Path) -> bool:
    """ISO 9660 primary volume descriptor: 'CD001' at byte 0x8001."""
    try:
        with path.open("rb") as f:
            f.seek(0x8001)
            return f.read(5) == b"CD001"
    except OSError:
        return False


def fetch(iso: Iso) -> Path:
    if is_iso(iso.path):
        return iso.path
    CACHE.mkdir(parents=True, exist_ok=True)
    part = iso.path.with_suffix(".part")
    with urllib.request.urlopen(iso.url, timeout=60) as r, part.open("wb") as out:
        shutil.copyfileobj(r, out, 1 << 20)
    if not is_iso(part):
        part.unlink(missing_ok=True)
        raise BmcctlError(f"{iso.url} did not return an ISO image")
    part.replace(iso.path)
    return iso.path


def lan_address(towards: str) -> str:
    """The source address this host uses to reach `towards` (a BMC)."""
    out = subprocess.run(["ip", "-4", "route", "get", towards], capture_output=True, text=True, check=True).stdout
    m = re.search(r"\bsrc (\S+)", out)
    if not m:
        raise BmcctlError(f"cannot tell which address reaches {towards}: {out.strip()}")
    return m.group(1)


def media_url(iso: Iso) -> str:
    return f"http://{lan_address(NODES[0].bmc)}:{PORT}/{iso.name}"


def _darkhttpd(repo: Path) -> str:
    out = subprocess.run(["nix", "build", "--no-link", "--print-out-paths", "--inputs-from", str(repo),
                          "nixpkgs#darkhttpd"], capture_output=True, text=True)
    if out.returncode != 0:
        raise BmcctlError(f"building darkhttpd failed:\n{out.stderr.strip()}")
    return out.stdout.strip().splitlines()[-1] + "/bin/darkhttpd"


def _active(unit: str, user: bool) -> bool:
    cmd = ["systemctl", *(["--user"] if user else []), "is-active", "--quiet", unit]
    return subprocess.run(cmd).returncode == 0


def _sudo(args: list[str], why: str):
    """sudo via the desktop askpass (ksshaskpass), so it works without a terminal."""
    env = dict(os.environ)
    if not env.get("SUDO_ASKPASS") and env.get("SSH_ASKPASS"):
        env["SUDO_ASKPASS"] = env["SSH_ASKPASS"]
    if not env.get("SUDO_ASKPASS"):
        raise BmcctlError("no SUDO_ASKPASS/SSH_ASKPASS: run this from the desktop session")
    r = subprocess.run(["sudo", "-A", "-p", f"bmcctl: {why}. Password for %u: ", *args], env=env)
    if r.returncode != 0:
        raise BmcctlError(f"sudo {' '.join(args[:3])}… failed (exit {r.returncode})")


def fw_rule(op: str, port: int = PORT) -> str:
    sources = ",".join(n.bmc for n in NODES)
    pos = " 1" if op == "-I" else ""
    return f"{IPTABLES} {op} nixos-fw{pos} -p tcp --dport {port} -s {sources} -j nixos-fw-accept"


def serve(repo: Path, iso: Iso, hours: int) -> str:
    path = fetch(iso)
    addr = lan_address(NODES[0].bmc)
    # CollectMode: a unit that failed or timed out is unloaded, so the next serve can reuse the name.
    transient = [f"--property=RuntimeMaxSec={hours}h", "--property=CollectMode=inactive-or-failed"]
    if not _active(HTTP_UNIT, user=True):
        subprocess.run(["systemd-run", "--user", "--quiet", f"--unit={HTTP_UNIT}", *transient,
                        _darkhttpd(repo), str(path.parent), "--addr", addr, "--port", str(PORT),
                        "--no-listing", "--no-server-id"], check=True)
    if not _active(FW_UNIT, user=False):
        _sudo(["systemd-run", "--quiet", f"--unit={FW_UNIT}", *transient,
               f"--property=ExecStartPre={fw_rule('-I')}", f"--property=ExecStopPost={fw_rule('-D')}",
               SLEEP, "infinity"],
              f"open TCP {PORT} on {addr} to the node BMCs for {hours}h")
    return f"http://{addr}:{PORT}/{iso.name}"


def unserve():
    subprocess.run(["systemctl", "--user", "stop", HTTP_UNIT], capture_output=True)
    if _active(FW_UNIT, user=False):
        _sudo(["systemctl", "stop", FW_UNIT], f"close TCP {PORT} to the node BMCs")


def serving() -> dict:
    return {"http": _active(HTTP_UNIT, user=True), "firewall": _active(FW_UNIT, user=False)}
