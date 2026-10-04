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


def test_declared_zone_coordinates_stay_encrypted():
    # people.yaml is git-crypt'd because the repo is public; a zone's coordinates must not
    # reappear in cleartext elsewhere (fixtures, docs). Reports file names only, never values.
    import pytest
    import yaml

    people = paths.REPO / "cluster" / "home-assistant" / "state" / "people.yaml"
    raw = people.read_bytes()
    if raw.startswith(b"\x00GITCRYPT"):
        pytest.skip("people.yaml is encrypted (no git-crypt key here)")
    zones = (yaml.safe_load(raw) or {}).get("zones") or []
    crypted = subprocess.run(["git", "ls-files", "-z"], cwd=paths.REPO, capture_output=True, check=True).stdout
    files = [f for f in crypted.decode().split("\0") if f]
    attrs = subprocess.run(["git", "check-attr", "filter", "--stdin", "-z"], cwd=paths.REPO, input="\0".join(files).encode(),
                           capture_output=True, check=True).stdout.decode().split("\0")
    encrypted = {attrs[i] for i in range(0, len(attrs) - 2, 3) if attrs[i + 2] == "git-crypt"}
    leaks = []
    for z in zones:
        lat, lon = f"{float(z['latitude']):.2f}", f"{float(z['longitude']):.2f}"
        for f in files:
            p = paths.REPO / f
            if f in encrypted or not p.is_file() or p.stat().st_size > 2_000_000:
                continue
            try:
                text = p.read_text()
            except UnicodeDecodeError:
                continue
            if lat in text and lon in text:
                leaks.append(f"{f} (zone {z['id']})")
    assert leaks == [], f"zone coordinates in cleartext files: {leaks}"


def test_ha_updates_get_their_own_renovate_groups():
    # Without a groupName they join group:allNonMajor: the HA core bump (pg_dumpall + breaking
    # changes first) gets bundled with unrelated updates, and its automerge:false blocks the bundle.
    import json

    rules = json.loads((paths.REPO / ".renovaterc.json").read_text())["packageRules"]
    core = [r for r in rules if "homeassistant/home-assistant" in r.get("matchPackageNames", [])]
    components = [r for r in rules if "cluster/applications/home-assistant-release.yaml" in r.get("matchFileNames", [])
                  and "github-tags" in r.get("matchDatasources", [])]
    assert core and components
    assert all(r.get("groupName") for r in core + components)
    assert {r["groupName"] for r in core}.isdisjoint({r["groupName"] for r in components})
