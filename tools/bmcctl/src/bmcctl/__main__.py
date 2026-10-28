"""bmcctl — galaxy node BMCs: power, BIOS, boot override, install media (tools/bmcctl/README.md)."""
import sys

from bmcctl.commands import build_parser
from bmcctl.errors import BmcctlError


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except BmcctlError as e:
        print(f"bmcctl: {e}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
