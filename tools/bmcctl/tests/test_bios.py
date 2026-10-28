from bmcctl import bios


def test_profiles_touch_the_same_attributes():
    assert bios.UEFI.keys() == bios.LEGACY.keys()


def test_changes_skips_values_already_in_effect():
    current = {"CSM006": "CSM006UEFIonly", "CSM008": "CSM008Legacyonly", "CSM009": "CSM009Legacyonly"}
    assert bios.changes(current, bios.UEFI) == {"CSM008": "CSM008UEFIonly", "CSM009": "CSM009UEFIonly"}


def test_pending_ignores_staged_copies_of_current_values():
    # The AMI SD object mirrors every attribute, not just the edited ones.
    current = {"CSM006": "CSM006UEFIandLegacy", "GNB003": "GNB00364M"}
    staged = {"CSM006": "CSM006UEFIonly", "GNB003": "GNB00364M"}
    assert bios.pending(current, staged) == {"CSM006": "CSM006UEFIonly"}


def test_short_strips_the_attribute_prefix():
    assert bios.short("CSM006", "CSM006UEFIonly") == "UEFIonly"
    assert bios.short("PCID001", True) == "True"
