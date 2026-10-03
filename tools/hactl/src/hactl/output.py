"""Shared CLI plumbing: the --json flag and printing."""
import argparse
import json
import sys

COMMON = argparse.ArgumentParser(add_help=False)
COMMON.add_argument("--json", action="store_true", help="machine-readable JSON output")


def emit(args, data, lines) -> None:
    """Print `data` as JSON when --json was given, else the human `lines`."""
    if getattr(args, "json", False):
        json.dump(data, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return
    for line in [lines] if isinstance(lines, str) else lines:
        print(line)
