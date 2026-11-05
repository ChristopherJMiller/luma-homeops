"""Static tests for the small/medium runner class image (images/forgejo-runner-class).

    python -m pytest -q -p no:cacheprovider scripts/test_runner_class_image.py

Runs as the `runner-class` pre-commit hook. Reads only the image directory, the galaxy runner's
Dockerfile and .renovaterc.json; never a `*.secret.*` file.
"""

import json
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
IMG = REPO / "images/forgejo-runner-class"
GALAXY = REPO / "images/forgejo-runner/Dockerfile"


def froms(path: Path) -> list[str]:
    return [l.strip() for l in path.read_text().splitlines() if l.startswith("FROM ")]


def test_froms_equal_galaxy_runner():
    mine, theirs = froms(IMG / "Dockerfile"), froms(GALAXY)
    assert len(theirs) == 5
    assert mine == theirs


def test_runs_as_root():
    text = (IMG / "Dockerfile").read_text()
    assert not re.search(r"^\s*USER\s", text, re.M)


def test_toolchain_installed():
    text = (IMG / "Dockerfile").read_text().replace("\\\n", " ")
    m = re.search(r"apt-get install\b[^&]*", text)
    assert m
    pkgs = set(m.group(0).split())
    assert {"ca-certificates", "git", "gcc", "libc6-dev", "libstdc++6", "make", "curl", "unzip"} <= pkgs


def test_runner_configs():
    for cls, timeout in (("small", "30m"), ("medium", "45m")):
        cfg = yaml.safe_load((IMG / f"runner-{cls}.yml").read_text())
        assert cfg["runner"]["capacity"] == 1
        assert cfg["runner"]["timeout"] == timeout
        assert cfg["cache"]["enabled"] is False
        assert cfg["host"]["workdir_parent"] == "/work/act"
        assert "connections" not in (cfg.get("server") or {})


def test_entry_contract():
    p = IMG / "ci-class-entry"
    text = p.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert re.search(r"^set -eu$", text, re.M)
    assert not re.search(r"\beval\b", text)
    assert p.stat().st_mode & 0o111
    assert text.index("unset RUNNER_TOKEN") < text.index("exec timeout")
    assert re.search(r"^umask 077$", text, re.M)
    # the handle is only ever one quoted argv element
    assert len(re.findall(r'\$\{?3\}?', text)) == len(re.findall(r'"\$3"', text))
    assert '--handle "$3"' in text
    assert "--token-url file:///run/ci/token" in text


def test_renovate_never_automerges_class_image():
    rules = json.loads((REPO / ".renovaterc.json").read_text())["packageRules"]
    name = "ghcr.io/christopherjmiller/forgejo-runner-class"
    idx = [i for i, r in enumerate(rules) if name in r.get("matchPackageNames", [])]
    assert idx
    assert rules[idx[-1]]["automerge"] is False
    # after every rule that could match it: no later rule turns automerge back on
    # (a rule without matchPackageNames matches everything)
    for r in rules[idx[-1] + 1 :]:
        if r.get("automerge") is True:
            assert r.get("matchPackageNames") and name not in r["matchPackageNames"], r
