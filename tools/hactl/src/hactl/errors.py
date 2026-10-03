"""Errors hactl reports to the operator (a message, never a traceback)."""


class HactlError(Exception):
    """A failure with a message meant for the operator. Exit code 1."""

    exit_code = 1


class PolicyError(HactlError):
    """Refused by the actuation policy: needs Chris's OK first. Exit code 3."""

    exit_code = 3
