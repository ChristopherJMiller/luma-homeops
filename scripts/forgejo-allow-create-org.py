#!/usr/bin/env python3
"""Let existing SSO accounts create organizations (Task 16, after Gate O).

    echo "$ADMIN_TOKEN" | scripts/forgejo-allow-create-org.py --source-id 1 [--apply]

The administrator token comes from stdin, never argv. The default is a dry run that prints the
count and the logins. --apply patches each account, lists again, and exits non-zero unless no
account is left to patch.
"""

import argparse
import json
import sys
import urllib.request

BASE = "https://git.realliance.net/api/v1"


def plan_patches(users: list[dict], source_id: int) -> list[dict]:
    """One patch per account of this source that may not create orgs. Prohibited accounts stay in (R11)."""
    return [
        {
            "login": u["login"],
            "body": {"login_name": u["login_name"], "source_id": source_id, "allow_create_organization": True},
        }
        for u in users
        if u.get("source_id") == source_id and not u.get("allow_create_organization")
    ]


def call(method: str, path: str, token: str, body: dict | None = None):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"token {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r) if r.status != 204 and r.length != 0 else None, r.headers


def list_users(token: str) -> list[dict]:
    users: list[dict] = []
    page = 1
    while True:
        batch, headers = call("GET", f"/admin/users?limit=50&page={page}", token)
        users += batch
        total = int(headers["X-Total-Count"])
        if len(users) >= total or not batch:
            break
        page += 1
    if len(users) != total:
        sys.exit(f"listed {len(users)} users, X-Total-Count says {total}")
    return users


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-id", type=int, required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    token = sys.stdin.read().strip()
    if not token:
        print("no administrator token on stdin", file=sys.stderr)
        return 2
    plan = plan_patches(list_users(token), args.source_id)
    print(f"{len(plan)} account(s) to patch: {', '.join(p['login'] for p in plan)}")
    if not args.apply:
        return 0
    for p in plan:
        call("PATCH", f"/admin/users/{p['login']}", token, p["body"])
    left = plan_patches(list_users(token), args.source_id)
    if left:
        print(f"still not allowed: {', '.join(p['login'] for p in left)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
