"""swctl — TEG-30284 core switch as code (switch/README.md)."""
import sys

from swctl.commands import build_parser
from swctl.errors import SwctlError


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except SwctlError as e:
        print(f"swctl: {e}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
