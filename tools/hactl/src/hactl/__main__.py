"""hactl — Home Assistant agent toolkit (tools/hactl/README.md)."""
import argparse
import importlib
import sys

from hactl.errors import HactlError

# Each module exposes register(subparsers); commands set `func` via set_defaults.
MODULES: list[str] = ["revision", "lint", "query", "shot", "preview", "health", "act"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hactl", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name in MODULES:
        importlib.import_module(f"hactl.{name}").register(sub)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except HactlError as e:
        print(f"hactl: {e}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
