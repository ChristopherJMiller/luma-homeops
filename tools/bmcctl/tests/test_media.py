from pathlib import Path

import pytest

from bmcctl import media
from bmcctl.errors import BmcctlError

SCHEMATIC = "173d42f096f07f5cc709f795178189dc64aa76ccad1a2193e5b3473f73f02a3e"


def config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "controlplane.yaml"
    p.write_text(body)
    return p


def test_iso_for_reads_the_factory_installer(tmp_path):
    p = config(tmp_path, f"""machine:
  kubelet:
    image: ghcr.io/siderolabs/kubelet:v1.33.13
  install:
    disk: /dev/nvme0n1 # The disk used for installations.
    image: factory.talos.dev/installer/{SCHEMATIC}:v1.10.9
""")
    iso = media.iso_for(p)
    assert (iso.schematic, iso.version) == (SCHEMATIC, "v1.10.9")
    assert iso.url == f"https://factory.talos.dev/image/{SCHEMATIC}/v1.10.9/metal-amd64.iso"
    assert iso.name == "talos-v1.10.9-173d42f0-metal-amd64.iso"


def test_iso_for_refuses_a_non_factory_installer(tmp_path):
    p = config(tmp_path, "machine:\n  install:\n    image: ghcr.io/siderolabs/installer:v1.10.9\n")
    with pytest.raises(BmcctlError):
        media.iso_for(p)


def test_is_iso_checks_the_volume_descriptor(tmp_path):
    good, bad = tmp_path / "good.iso", tmp_path / "bad.iso"
    good.write_bytes(b"\0" * 0x8001 + b"CD001" + b"\0" * 10)
    bad.write_bytes(b"<html>not found</html>")
    assert media.is_iso(good)
    assert not media.is_iso(bad)
    assert not media.is_iso(tmp_path / "missing.iso")


def test_fw_rule_admits_only_the_bmcs():
    insert, delete = media.fw_rule("-I"), media.fw_rule("-D")
    assert " -I nixos-fw 1 " in insert and " -D nixos-fw " in delete
    assert "-s 192.168.0.119,192.168.0.117,192.168.0.151" in insert
    assert insert.replace(" -I nixos-fw 1 ", " X ") == delete.replace(" -D nixos-fw ", " X ")
