import argparse

import pytest

import hactl.__main__ as hactl_main
from hactl.errors import HactlError


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        hactl_main.main(["--help"])
    assert e.value.code == 0
    assert "usage: hactl" in capsys.readouterr().out


def test_hactl_error_becomes_message_and_exit_code(monkeypatch, capsys):
    def boom(args):
        raise HactlError("nope")

    def fake_parser():
        p = argparse.ArgumentParser(prog="hactl")
        s = p.add_subparsers(dest="command", required=True)
        s.add_parser("boom").set_defaults(func=boom)
        return p

    monkeypatch.setattr(hactl_main, "build_parser", fake_parser)
    assert hactl_main.main(["boom"]) == 1
    assert capsys.readouterr().err == "hactl: nope\n"


@pytest.mark.parametrize("name", hactl_main.MODULES)
def test_every_module_registers(name):
    parser = hactl_main.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert sub.choices, f"{name} registered no command"
