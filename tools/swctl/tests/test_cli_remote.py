from swctl import cli_remote


def test_whitelist():
    assert cli_remote.allowed("save") and cli_remote.allowed("show switch")
    for bad in ("reboot", "reset", "reset system", "config ipif 99 ipaddress 1.2.3.4 255.0.0.0", "save ; reboot",
                "", "show switch\nreboot"):
        assert not cli_remote.allowed(bad)


def test_more_prompt_and_ansi_are_stripped():
    raw = "\x1b[7mCTRL+C\x1b[0m ESC q Quit SPACE n Next Page ENTER Next Entry a All\nSystem Up For: 1 day"
    assert cli_remote.clean(raw) == "System Up For: 1 day"
