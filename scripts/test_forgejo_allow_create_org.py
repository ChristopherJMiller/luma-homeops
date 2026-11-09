"""Tests for scripts/forgejo-allow-create-org.py (no network).

    python -m pytest -q -p no:cacheprovider scripts/test_forgejo_allow_create_org.py
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "forgejo-allow-create-org.py"
spec = importlib.util.spec_from_file_location("allow_create_org", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def user(login: str, source_id: int, allowed: bool, **extra) -> dict:
    return {"login": login, "login_name": f"{login}-sso", "source_id": source_id,
            "allow_create_organization": allowed, **extra}


def test_skips_local_and_already_allowed() -> None:
    users = [user("local", 0, False), user("done", 1, True), user("todo", 1, False)]
    plan = mod.plan_patches(users, 1)
    assert [p["login"] for p in plan] == ["todo"]


def test_skips_other_sources() -> None:
    assert mod.plan_patches([user("x", 2, False)], 1) == []


def test_keeps_prohibited_sso_accounts() -> None:
    """R11: a prohibited account is still patched, so its flag is right if it comes back."""
    plan = mod.plan_patches([user("gone", 1, False, prohibit_login=True)], 1)
    assert [p["login"] for p in plan] == ["gone"]


def test_body_is_exactly_three_keys() -> None:
    (p,) = mod.plan_patches([user("todo", 1, False, email="a@b", is_admin=True)], 1)
    assert p["body"] == {"login_name": "todo-sso", "source_id": 1, "allow_create_organization": True}


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], input="", capture_output=True, text=True)


def test_refuses_token_in_argv() -> None:
    r = run("--source-id", "1", "--token", "abc")
    assert r.returncode != 0


def test_requires_source_id() -> None:
    r = run()
    assert r.returncode != 0
