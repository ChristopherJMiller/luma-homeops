"""Repo-level guards for things hactl's own workflow can break."""
import re
import subprocess

from hactl import paths


def test_no_bytecode_is_tracked():
    tracked = subprocess.run(["git", "ls-files"], cwd=paths.REPO, capture_output=True, text=True, check=True).stdout
    assert [f for f in tracked.splitlines() if "__pycache__" in f or f.endswith(".pyc")] == []


def test_runbook_never_prints_a_secret():
    # `kubectl create secret ... -o yaml` without a redirect prints the base64 token (CLAUDE.md S9).
    text = (paths.REPO / "docs" / "ha.md").read_text()
    for line in text.splitlines():
        if "create secret" in line and "-o yaml" in line:
            assert re.search(r"-o yaml\s*>\s*\S", line), f"secret command prints to stdout: {line.strip()}"


def test_skill_keeps_the_hard_won_gotchas():
    # Spec §4.9: the ha-config rewrite keeps the gotchas from the old skill.
    text = (paths.REPO / ".claude" / "skills" / "ha-config" / "SKILL.md").read_text()
    for needle in ["check-config", "recorder", "automation` and `update", "silently skips", "lovelace: mode: yaml",
                   ".storage", "lovelace.lovelace", "underscores"]:
        assert needle in text, f"skill lost the gotcha mentioning {needle!r}"
