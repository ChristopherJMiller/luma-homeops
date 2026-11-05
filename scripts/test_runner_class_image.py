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


# ---- behavioural: run the entry script under a POSIX sh (dash, as in the image) -----------------

import os
import shutil
import subprocess

import pytest

SH = shutil.which("dash") or shutil.which("sh")
GOOD = ["30m", "small", "h_1-X", "https://git.example/", "0a1b2c"]


@pytest.fixture()
def rig(tmp_path):
    """The entry script with /run/ci and /work/act under tmp_path; a stub `timeout` that prints
    its argv and its own environment (it is what the script exec's)."""
    src = (IMG / "ci-class-entry").read_text()
    entry = tmp_path / "entry"
    entry.write_text(src.replace("/run/ci", f"{tmp_path}/run/ci").replace("/work/act", f"{tmp_path}/act"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "timeout"
    stub.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$STUB_OUT/argv"\n'
        'env > "$STUB_OUT/env"\ncat "$STUB_TOKEN" > "$STUB_OUT/tok"\n'
    )
    stub.chmod(0o755)

    def run(args, token="tok-secret"):
        env = {"PATH": f"{bindir}:{os.environ['PATH']}", "STUB_OUT": str(tmp_path),
               "STUB_TOKEN": f"{tmp_path}/run/ci/token"}
        if token is not None:
            env["RUNNER_TOKEN"] = token
        return subprocess.run([SH, str(entry), *args], env=env, capture_output=True, text=True)

    run.tmp = tmp_path
    return run


def test_entry_exec_contract(rig):
    r = rig(GOOD)
    assert r.returncode == 0, r.stderr
    argv = (rig.tmp / "argv").read_text().replace(f"{rig.tmp}/run/ci", "/run/ci").split("\n")[:-1]
    assert argv == [
        "--kill-after=30s", "30m", "forgejo-runner", "one-job", "--config",
        "/etc/ci-class/runner-small.yml", "--wait", "--handle", "h_1-X", "--url",
        "https://git.example/", "--uuid", "0a1b2c", "--token-url", "file:///run/ci/token",
        "--label", "small:host",
    ]
    assert "RUNNER_TOKEN" not in (rig.tmp / "env").read_text()
    assert (rig.tmp / "tok").read_text() == "tok-secret"
    assert oct((rig.tmp / "run/ci/token").stat().st_mode & 0o777) == "0o600"
    assert oct((rig.tmp / "run/ci").stat().st_mode & 0o777) == "0o700"


BAD = {
    "class": [GOOD[0], "large", *GOOD[2:]],
    "handle-space": [GOOD[0], "small", "a b", *GOOD[3:]],
    "handle-newline": [GOOD[0], "small", "ok\nx", *GOOD[3:]],
    "handle-empty": [GOOD[0], "small", "", *GOOD[3:]],
    "handle-129": [GOOD[0], "small", "a" * 129, *GOOD[3:]],
    "timeout-text": ["abm", *GOOD[1:]],
    "timeout-3digits": ["300m", *GOOD[1:]],
    "timeout-newline": ["3m\nx", *GOOD[1:]],
    "timeout-0": ["0m", *GOOD[1:]],
    "timeout-00": ["00m", *GOOD[1:]],
    "url-empty": [*GOOD[:3], "", GOOD[4]],
    "uuid-empty": [*GOOD[:4], ""],
    "url-newline": [*GOOD[:3], "u\nv", GOOD[4]],
    "uuid-ctrl": [*GOOD[:4], "a\x01b"],
    "four-args": GOOD[:4],
    "six-args": [*GOOD, "x"],
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_entry_refuses(rig, name):
    r = rig(BAD[name])
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert not (rig.tmp / "argv").exists(), "stub ran: the script exec'd despite a bad input"


@pytest.mark.parametrize("tok", ["", None])
def test_entry_refuses_empty_token(rig, tok):
    r = rig(GOOD, token=tok)
    assert r.returncode == 2
    assert not (rig.tmp / "argv").exists()


def test_entry_handle_129_boundary(rig):
    assert rig([GOOD[0], "small", "a" * 128, *GOOD[3:]]).returncode == 0


def test_entry_statement_order():
    text = (IMG / "ci-class-entry").read_text()
    i = [text.index(s) for s in ("\numask 077\n", "printf '%s' \"$RUNNER_TOKEN\"", "\nunset RUNNER_TOKEN\n", "\nexec timeout")]
    assert i == sorted(i)
