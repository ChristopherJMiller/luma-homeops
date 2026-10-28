"""bmcctl subcommands."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from bmcctl import bios as bios_mod
from bmcctl import media
from bmcctl.bmc import Bmc, load_creds
from bmcctl.errors import BmcctlError
from bmcctl.nodes import NODES, node

REPO = Path(__file__).resolve().parents[4]
MACHINE_CONFIG = REPO / "nodes/controlplane.yaml"
CREDS = REPO / "nodes/bmc.env"
BOOT_TARGETS = {"cd": "Cd", "disk": "Hdd", "clear": "None"}


def connect(name: str) -> Bmc:
    return Bmc(node(name).bmc, *load_creds(CREDS))


def _image_matches(image: dict, name: str) -> bool:
    got = str(image.get("image_name", ""))
    return got == name or got.endswith("/" + name)


def cmd_status(args):
    served = media.serving()
    print(f"media server: http={'up' if served['http'] else 'down'} firewall={'open' if served['firewall'] else 'closed'}")
    for name in args.nodes or [n.name for n in NODES]:
        n = node(name)
        with connect(name) as b:
            temps = b.thermal()
            boot, boot_next = b.boot_override()
            current, staged = b.bios()
            ms = b.media_settings()
            try:
                images, redirecting, media_error = b.remote_images(), b.active_redirections(), None
            except BmcctlError as e:
                images, redirecting, media_error = [], [], str(e)
        print(f"\n{n.name} (node {n.ip}, BMC {n.bmc}): power {b.power_state()}")
        print("  " + "  ".join(f"{k} {v}" for k, v in temps.items() if v is not None))
        print(f"  boot override: {_override(boot)}; staged for next boot: {_override(boot_next)}")
        due = bios_mod.pending(current, staged)
        for attr, label in bios_mod.SHOWN.items():
            line = f"  {label:<28} {bios_mod.short(attr, current.get(attr))}"
            if attr in due:
                line += f"  -> {bios_mod.short(attr, due[attr])} at next POST"
            print(line)
        other = {k: v for k, v in due.items() if k not in bios_mod.SHOWN}
        if other:
            print(f"  other staged BIOS changes: {other}")
        print(f"  remote media: support={ms.get('remote_media_support')} mount_cd={ms.get('mount_cd')} "
              f"type={ms.get('cd_remote_share_type') or '-'} path={ms.get('cd_remote_source_path') or '-'}")
        if media_error:
            print(f"    MEDIA SERVICE ERROR: {media_error}")
        for image in images:
            print(f"    configured: {image.get('image_name')}")
        for active in redirecting:
            kind = {1: "CD", 4: "HD"}.get(active.get("media_type"), active.get("media_type"))
            print(f"    REDIRECTING ({kind}): {active.get('image_name')} state={active.get('state')}")


def cmd_iso(args):
    iso = media.iso_for(MACHINE_CONFIG)
    path = media.fetch(iso)
    print(f"{path}  ({path.stat().st_size // 2**20} MiB, from {iso.url})")


def cmd_serve(args):
    url = media.serve(REPO, media.iso_for(MACHINE_CONFIG), args.hours)
    print(f"serving {url} to the node BMCs for up to {args.hours}h (bmcctl unserve to stop early)")


def cmd_unserve(args):
    media.unserve()
    print("media server stopped, firewall closed")


def cmd_mount(args):
    iso = media.iso_for(MACHINE_CONFIG)
    if not all(media.serving().values()):
        raise BmcctlError("the media server is not up: run `bmcctl serve` first")
    url = media.media_url(iso)
    with connect(args.node) as b:
        b.set_rmedia(True)
        b.configure_http_cd(url)
        image = b.wait_images(iso.name, matcher=_image_matches)
        b.start_cd(image)
        deadline = time.monotonic() + 30
        while True:
            active = [a for a in b.active_redirections() if _image_matches(a, iso.name) and a.get("state") == 1]
            if active:
                break
            if time.monotonic() > deadline:
                raise BmcctlError(f"{args.node}: redirection did not start: {json.dumps(b.active_redirections())}")
            time.sleep(2)
    print(f"{args.node}: virtual CD = {url} (the host sees it as a USB 'Virtual CDROM0')")


def cmd_eject(args):
    # Remote media itself stays enabled: toggling it is what upsets this firmware, and
    # an enabled BMC with nothing redirecting does not re-attach the image by itself
    # (not even across a BMC reset).
    with connect(args.node) as b:
        for active in b.active_redirections():
            if active.get("media_type") == 1:
                b.stop_cd(active)
                print(f"{args.node}: ejecting {active.get('image_name')}")
        deadline = time.monotonic() + 30
        while any(a.get("media_type") == 1 for a in b.active_redirections()):
            if time.monotonic() > deadline:
                raise BmcctlError(f"{args.node}: the virtual CD is still attached after 30s")
            time.sleep(2)
    print(f"{args.node}: no virtual CD attached")


POWER = {"on": "On", "restart": "ForceRestart", "off": "ForceOff"}


def cmd_power(args):
    with connect(args.node) as b:
        if args.action != "status":
            if args.action in ("restart", "off") and not args.force:
                raise BmcctlError(f"`power {args.action}` is a hard reset of the host. Shut a running node down "
                                  "with talosctl; pass --force for one that is wedged or sitting in firmware/ISO")
            b.reset(POWER[args.action])
            time.sleep(3)
        print(f"{args.node}: power {b.power_state()}")


def _override(boot: dict) -> str:
    return (f"{boot.get('BootSourceOverrideEnabled')} -> {boot.get('BootSourceOverrideTarget')}"
            f" ({boot.get('BootSourceOverrideMode') or 'mode unset'})")


def cmd_boot(args):
    with connect(args.node) as b:
        b.set_boot_override(BOOT_TARGETS[args.target])
        _, staged = b.boot_override()
    print(f"{args.node}: next boot {_override(staged)}")


def cmd_bios(args):
    with connect(args.node) as b:
        current, staged = b.bios()
        if args.set:
            wanted = bios_mod.changes(current, bios_mod.PROFILES[args.set])
            if not wanted:
                print(f"{args.node}: already {args.set}")
                return
            b.stage_bios(wanted)
            current, staged = b.bios()
    due = bios_mod.pending(current, staged)
    for attr, label in bios_mod.SHOWN.items():
        line = f"{label:<28} {bios_mod.short(attr, current.get(attr))}"
        if attr in due:
            line += f"  -> {bios_mod.short(attr, due[attr])} at next POST"
        print(line)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bmcctl", description="galaxy node BMCs: power, BIOS, boot, install media")
    sub = p.add_subparsers(required=True, metavar="command")
    names = [n.name for n in NODES]

    s = sub.add_parser("status", help="power, temps, boot override, boot-relevant BIOS, remote media")
    s.add_argument("nodes", nargs="*", metavar="node", help=f"default: all ({', '.join(names)})")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("iso", help="fetch the ISO matching nodes/controlplane.yaml's install.image")
    s.set_defaults(func=cmd_iso)

    s = sub.add_parser("serve", help="serve the ISO to the BMCs (asks for sudo once: firewall)")
    s.add_argument("--hours", type=int, default=6, help="stop serving after this long (default 6)")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("unserve", help="stop serving and close the firewall")
    s.set_defaults(func=cmd_unserve)

    s = sub.add_parser("mount", help="attach the served ISO as the node's virtual CD")
    s.add_argument("node", choices=names)
    s.set_defaults(func=cmd_mount)

    s = sub.add_parser("eject", help="detach the virtual CD and turn remote media off")
    s.add_argument("node", choices=names)
    s.set_defaults(func=cmd_eject)

    s = sub.add_parser("power", help="power status / on; restart and off are hard and need --force")
    s.add_argument("node", choices=names)
    s.add_argument("action", choices=["status", "on", "restart", "off"])
    s.add_argument("--force", action="store_true", help="allow the hard restart/off")
    s.set_defaults(func=cmd_power)

    s = sub.add_parser("boot", help="one-time UEFI boot override (applies at the next boot)")
    s.add_argument("node", choices=names)
    s.add_argument("target", choices=sorted(BOOT_TARGETS))
    s.set_defaults(func=cmd_boot)

    s = sub.add_parser("bios", help="show boot-relevant BIOS settings, or stage a profile for the next POST")
    s.add_argument("node", choices=names)
    s.add_argument("--set", choices=sorted(bios_mod.PROFILES), help="stage this boot profile")
    s.set_defaults(func=cmd_bios)
    return p
