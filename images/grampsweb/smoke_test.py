"""Behavioural test for oidc-new-user-role.patch, run against the built image.

The patch decides who gets into the family tree without a human in the loop, so
what matters is not that it imports but that it fails CLOSED: every case below
that is not "a listed, verified address and a sane role" must come back
ROLE_DISABLED, which is exactly upstream's behaviour.

Run: docker run --rm --entrypoint /venv/bin/python3 <image> /tmp/smoke_test.py
"""

import sys
from pathlib import Path

from flask import Flask

from gramps_webapi.auth.const import ROLE_ADMIN, ROLE_DISABLED, ROLE_EDITOR
from gramps_webapi.auth.oidc import _new_user_role

LIST = Path("/tmp/allowlist")
LIST.write_text("# a comment line\n\nLISTED@Example.COM\nother@example.com\n")

GRANTING = {
    "OIDC_NEW_USER_ROLE": ROLE_EDITOR,
    "OIDC_NEW_USER_ROLE_EMAILS_FILE": str(LIST),
}
LISTED = {"email": "listed@example.com"}

CASES = [
    ("a listed address gets the role", GRANTING, LISTED, ROLE_EDITOR),
    ("case and surrounding space do not matter", GRANTING,
     {"email": "  LiStEd@Example.com  "}, ROLE_EDITOR),
    ("an explicitly verified address still passes", GRANTING,
     {"email": "listed@example.com", "email_verified": True}, ROLE_EDITOR),
    ("a role given as a string (which is how env vars arrive) works",
     {**GRANTING, "OIDC_NEW_USER_ROLE": "3"}, LISTED, ROLE_EDITOR),

    ("an unlisted address stays disabled", GRANTING,
     {"email": "stranger@example.com"}, ROLE_DISABLED),
    ("no email claim stays disabled", GRANTING, {}, ROLE_DISABLED),
    ("an address the provider calls unverified stays disabled", GRANTING,
     {"email": "listed@example.com", "email_verified": False}, ROLE_DISABLED),
    ("a comment line is not an address", GRANTING,
     {"email": "# a comment line"}, ROLE_DISABLED),
    ("a missing allowlist file fails closed",
     {**GRANTING, "OIDC_NEW_USER_ROLE_EMAILS_FILE": "/tmp/does-not-exist"},
     LISTED, ROLE_DISABLED),
    ("a directory where the file should be fails closed",
     {**GRANTING, "OIDC_NEW_USER_ROLE_EMAILS_FILE": "/tmp"},
     LISTED, ROLE_DISABLED),
    ("no allowlist configured fails closed",
     {**GRANTING, "OIDC_NEW_USER_ROLE_EMAILS_FILE": None}, LISTED, ROLE_DISABLED),
    ("a role above editor is refused",
     {**GRANTING, "OIDC_NEW_USER_ROLE": ROLE_ADMIN}, LISTED, ROLE_DISABLED),
    ("a role below guest is refused",
     {**GRANTING, "OIDC_NEW_USER_ROLE": -2}, LISTED, ROLE_DISABLED),
    ("a non-numeric role fails closed",
     {**GRANTING, "OIDC_NEW_USER_ROLE": "editor"}, LISTED, ROLE_DISABLED),
    ("unconfigured is upstream behaviour", {}, LISTED, ROLE_DISABLED),
]

app = Flask(__name__)
failures = 0

for description, config, userinfo, expected in CASES:
    app.config.update(
        {"OIDC_NEW_USER_ROLE": ROLE_DISABLED, "OIDC_NEW_USER_ROLE_EMAILS_FILE": None}
    )
    app.config.update(config)
    with app.app_context():
        got = _new_user_role(userinfo)
    ok = got == expected
    failures += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  {description}: expected {expected}, got {got}")

print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
sys.exit(1 if failures else 0)
