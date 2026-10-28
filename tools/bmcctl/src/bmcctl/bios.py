"""The BIOS settings bmcctl reads and writes, by AMI attribute name.

Names and values come from the BMC's registry (BiosAttributeRegistry1AWQE.2.50.0,
BIOS P2.50). `LEGACY` is what all three nodes ran with until 2026-10: legacy/CSM
boot, under which the firmware publishes no ACPI VFCT table and the 5700G iGPU
fails amdgpu init (#2596). `UEFI` is the fix #2596 proposes; the ASPEED BMC VGA
stays primary, so the BMC console keeps working.
"""
from __future__ import annotations

SHOWN = {
    "CSM005": "CSM",
    "CSM006": "Boot option filter",
    "CSM008": "Launch Storage OpROM Policy",
    "CSM009": "Launch Video OpROM Policy",
    "GNB002": "Integrated Graphics",
    "GNB003": "UMA Frame Buffer Size",
    "PBS002": "Primary Graphics Adaptor",
    "SECB001": "Secure Boot",
}

LEGACY = {
    "CSM006": "CSM006UEFIandLegacy",
    "CSM008": "CSM008Legacyonly",
    "CSM009": "CSM009Legacyonly",
}

UEFI = {
    "CSM006": "CSM006UEFIonly",
    "CSM008": "CSM008UEFIonly",
    "CSM009": "CSM009UEFIonly",
}

PROFILES = {"legacy": LEGACY, "uefi": UEFI}


def pending(current: dict, staged: dict) -> dict:
    """Attributes the next POST will change: staged values that differ from current."""
    return {k: v for k, v in staged.items() if current.get(k) != v}


def changes(current: dict, wanted: dict) -> dict:
    """The subset of `wanted` not already in effect."""
    return {k: v for k, v in wanted.items() if current.get(k) != v}


def short(attr: str, value) -> str:
    """CSM006UEFIonly -> UEFIonly: AMI prefixes every enum value with its attribute name."""
    s = str(value)
    return s[len(attr):] if s.startswith(attr) else s
