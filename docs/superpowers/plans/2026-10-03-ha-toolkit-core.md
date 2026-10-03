# HA Toolkit Core (`hactl`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give agents a tested CLI (`hactl`) to query, screenshot, preview, lint and health-check Home Assistant, plus an Argo PostSync hook that makes every git change to HA config load and verifies it.

**Architecture:** A stdlib-first Python package in `tools/hactl/` (PyYAML everywhere; `websockets` and Playwright imported lazily, only by the commands that need them), run through a bash wrapper on the flake dev shell's PATH. Pure logic is unit-tested on fixtures; live behaviour is checked against the real HA in each task. The in-cluster `ha-reload` Job is a separate stdlib-only script (`cluster/home-assistant/hooks/ha_reload.py`), tested by the same pytest suite.

**Tech Stack:** Python 3.13, PyYAML, websockets, Playwright (Chromium from nixpkgs), pytest, Nix flake dev shell, kustomize + Argo CD, Sealed Secrets.

**Spec:** `docs/superpowers/specs/2026-10-03-ha-agent-overhaul-design.md` (§4.1–§4.10). This is plan 1 of 5: it delivers the toolkit core. The declarative-state engine (§4.11: `import`/`plan`/`apply`) is plan 2; ops fixes, automations and the dashboard are plans 3–5.

## Global Constraints

- Work from the repo root `/home/chris/Repos/luma-homeops`, directly on `main` (Chris prefers direct pushes). Never `--no-verify` (CLAUDE.md S6).
- `pre-commit` exists only in the dev shell: commit with `nix develop --command git commit …`. Every commit message ends with the trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Test command (from repo root): `nix develop --command python -m pytest -q tools/hactl` (single test: append `/tests/test_x.py::test_name`).
- Cluster access: `export KUBECONFIG=/tmp/galaxy-kubeconfig`. If rejected, refresh: `nix develop --command talosctl --talosconfig nodes/talosconfig -n 192.168.0.5 -e 192.168.0.5 kubeconfig --force /tmp/galaxy-kubeconfig`.
- No direct cluster writes (S2): manifests go through git → Argo. `kubectl get/logs` are fine.
- HA: `https://home.chrismiller.xyz`; in-cluster `http://ha-home-assistant.home-assistant.svc:8123`; Deployment `ha-home-assistant` (strategy `Recreate`); Argo apps `home-assistant` (kustomize, `cluster/home-assistant`) and `home-assistant-release` (helm).
- Tokens (all minted under Chris's own account, his choice): `~/.config/galaxy/ha-token` (hactl), `~/.config/galaxy/reload-token` (hook), `~/.config/galaxy/prom-token` (plan 3). **Never print, echo, or commit a token in clear.** Shell recipes read them with `$(cat …)` or `--from-file`, never by pasting.
- Python modules import `websockets` and `playwright` **inside functions only**; CI installs just `pyyaml pytest`.
- Package filenames: lowercase + underscores. Quote `'on'`/`'off'`/`yes`/`no` and `"HH:MM:SS"` in HA YAML. HA dirs stay excluded from yamlfmt.
- `check_config` exit code is meaningless; grep its output.
- Screenshot viewports: phone 412×915 @ DPR 2 (mobile, touch); desktop 1440×900. Theme probe: CSS var `--ha-card-border-radius` = `22px` (warm-minimal).
- Preview dashboard: url `claude-preview`, title `Claude Preview`, `require_admin: true`, `show_in_sidebar: false`.
- Actuation policy: anything reversible is free; `lock.*`, `notify.*`, `tts.*`, `assist_satellite.*`, `homeassistant.restart`, `homeassistant.stop` need Chris's OK (`--confirmed`).
- Hook image: `docker.io/library/python:3.13-alpine@sha256:2dd78ad5cf13a0b68f5134dc49aa9950203a8cf4b7463431b9f3b398287c5059`.

## Review Focus

Failure modes the spec implies that a person will hit. Each has a test in the owning task:

1. **HA restarting mid-command** (Traefik 502, connection refused, websocket refused) → one-line `hactl:` error, exit 1, no traceback, no token in the text. Tests in Task 2 (`test_502_is_reported`, `test_ws_refused`).
2. **A push that doesn't touch the HA app** → Argo moves `sync.revision` but starts no sync; `hactl deploy` must finish, not time out. Test in Task 12 (`test_untouched_app_counts_as_synced`).
3. **The hook polling while HA is down or the ConfigMap hasn't propagated** → keep retrying until the deadline, never crash on the first error. Test in Task 11 (`test_reload_survives_ha_restarting`).
4. **Automations cancelled by their own `mode: restart`/`single`** (the bedroom sync does this constantly) → must not be reported as failing. Test in Task 9 (`test_cancelled_runs_are_not_failures`).
5. **HA-specific YAML tags** (`!input` in blueprints, `!include_dir_named`, `!env_var`, `!secret`) → lint, refs and health must load them without crashing. Test in Task 4 (`test_blueprint_input_tags_load`).

---

### Task 1: Toolchain and CLI skeleton

**Files:**
- Modify: `flake.nix` (devShell `packages` list: the `python313` line; add `shellHook`)
- Create: `tools/hactl/bin/hactl`
- Create: `tools/hactl/pyproject.toml`
- Create: `tools/hactl/src/hactl/__init__.py`, `__main__.py`, `errors.py`, `output.py`, `paths.py`
- Test: `tools/hactl/tests/test_cli.py`

**Interfaces:**
- Produces: `hactl.__main__.MODULES: list[str]` (each later task appends its module name); `hactl.__main__.build_parser()`, `main(argv) -> int`; `hactl.errors.HactlError` (exit 1), `hactl.errors.PolicyError(HactlError)` (exit 3); `hactl.output.COMMON` (argparse parent adding `--json`), `hactl.output.emit(args, data, lines)`; `hactl.paths.REPO`, `HA_DIR`, `RELEASE_FILE`, `TOKEN_FILE`, `rel(path) -> str`.
- Every command module exposes `register(sub)` where `sub` is the argparse subparsers object; commands call `set_defaults(func=…)`; `func(args)` returns an exit code (or `None` = 0).

- [ ] **Step 1: Invoke the `nix-shell-pin` skill** (required for any flake change) and follow its verification rules.

- [ ] **Step 2: Edit `flake.nix`.** In the devShell `packages` list, replace the bare `python313` line with:

```nix
            # tools/hactl (Home Assistant agent toolkit): websockets + pyyaml for
            # the HA API, playwright for screenshots, pytest for its tests. The
            # browsers below come from the same nixpkgs pin, so their versions
            # match. uv-managed venvs (the ceph-nfs-export-operator) are unaffected.
            (python313.withPackages (ps: [ ps.websockets ps.pyyaml ps.playwright ps.pytest ]))
            playwright-driver.browsers
```

Then, after the closing `];` of that `packages` list (just before the `};` that closes `mkShell`), add:

```nix
          # hactl: wrapper on PATH, and Playwright pointed at the nix-built
          # browsers (the ones it downloads itself don't run on NixOS).
          shellHook = ''
            export PATH="$(git rev-parse --show-toplevel 2>/dev/null || pwd)/tools/hactl/bin:$PATH"
            export PLAYWRIGHT_BROWSERS_PATH=${pkgs.playwright-driver.browsers}
            export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=true
          '';
```

- [ ] **Step 3: Create `tools/hactl/bin/hactl`** and make it executable (`chmod +x`):

```bash
#!/usr/bin/env bash
# hactl — Home Assistant agent toolkit. See tools/hactl/README.md.
#
# The host exports LD_LIBRARY_PATH (a system alsa-lib built against a newer
# glibc than the flake's), which kills the nix-built browser. Drop it.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec env -u LD_LIBRARY_PATH PYTHONPATH="$here/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m hactl "$@"
```

- [ ] **Step 4: Create `tools/hactl/pyproject.toml`:**

```toml
# hactl — Home Assistant agent toolkit. Not packaged; run via bin/hactl.
[project]
name = "hactl"
version = "0.1.0"
requires-python = ">=3.12"

[tool.pytest.ini_options]
pythonpath = ["src", "../../cluster/home-assistant/hooks"]
testpaths = ["tests"]
```

- [ ] **Step 5: Write the failing test** `tools/hactl/tests/test_cli.py`:

```python
import argparse

import pytest

import hactl.__main__ as hactl_main
from hactl.errors import HactlError


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        hactl_main.main(["--help"])
    assert e.value.code == 0
    assert "usage: hactl" in capsys.readouterr().out


def test_hactl_error_becomes_message_and_exit_code(monkeypatch, capsys):
    def boom(args):
        raise HactlError("nope")

    def fake_parser():
        p = argparse.ArgumentParser(prog="hactl")
        s = p.add_subparsers(dest="command", required=True)
        s.add_parser("boom").set_defaults(func=boom)
        return p

    monkeypatch.setattr(hactl_main, "build_parser", fake_parser)
    assert hactl_main.main(["boom"]) == 1
    assert capsys.readouterr().err == "hactl: nope\n"


@pytest.mark.parametrize("name", hactl_main.MODULES)
def test_every_module_registers(name):
    parser = hactl_main.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert sub.choices, f"{name} registered no command"
```

- [ ] **Step 6: Run it to verify it fails**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: FAIL — `ModuleNotFoundError: No module named 'hactl'`.

- [ ] **Step 7: Create the package files.**

`tools/hactl/src/hactl/__init__.py`:

```python
"""hactl — Home Assistant agent toolkit for galaxy."""
```

`tools/hactl/src/hactl/errors.py`:

```python
"""Errors hactl reports to the operator (a message, never a traceback)."""


class HactlError(Exception):
    """A failure with a message meant for the operator. Exit code 1."""

    exit_code = 1


class PolicyError(HactlError):
    """Refused by the actuation policy: needs Chris's OK first. Exit code 3."""

    exit_code = 3
```

`tools/hactl/src/hactl/output.py`:

```python
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
```

`tools/hactl/src/hactl/paths.py`:

```python
"""Repo locations hactl works with."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
HA_DIR = REPO / "cluster" / "home-assistant"
RELEASE_FILE = REPO / "cluster" / "applications" / "home-assistant-release.yaml"
TOKEN_FILE = REPO / "tools" / "hactl" / "agent.secret.yaml"


def rel(path) -> str:
    """Repo-relative path for messages."""
    try:
        return str(Path(path).resolve().relative_to(REPO))
    except ValueError:
        return str(path)
```

`tools/hactl/src/hactl/__main__.py`:

```python
"""hactl — Home Assistant agent toolkit (tools/hactl/README.md)."""
import argparse
import importlib
import sys

from hactl.errors import HactlError

# Each module exposes register(subparsers); commands set `func` via set_defaults.
MODULES: list[str] = []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hactl", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name in MODULES:
        importlib.import_module(f"hactl.{name}").register(sub)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except HactlError as e:
        print(f"hactl: {e}", file=sys.stderr)
        return e.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: `2 passed, 1 skipped` (the parametrized test has no modules yet).

- [ ] **Step 9: Verify the shell** (nix-shell-pin rule: build it and print versions):

Run: `nix develop --command bash -c 'python3 -c "import websockets, yaml, playwright; from importlib.metadata import version as v; print(v(\"websockets\"), yaml.__version__, v(\"playwright\"))"; echo "$PLAYWRIGHT_BROWSERS_PATH"; which hactl; hactl --help | head -3'`
Expected: three version numbers, a `/nix/store/…-playwright-browsers` path, `…/tools/hactl/bin/hactl`, and `usage: hactl`.

- [ ] **Step 10: Commit**

```bash
git add flake.nix tools/hactl
nix develop --command git commit -m "hactl: toolchain and CLI skeleton" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: HA client and token

**Files:**
- Create: `tools/hactl/src/hactl/client.py`
- Create: `tools/hactl/agent.secret.yaml` (git-crypt; from `~/.config/galaxy/ha-token`)
- Test: `tools/hactl/tests/test_client.py`

**Interfaces:**
- Consumes: `hactl.errors.HactlError`, `hactl.paths.TOKEN_FILE`, `hactl.paths.rel`.
- Produces: `load_token(env=os.environ, token_file=paths.TOKEN_FILE) -> str`; `class Client(url=None, token=None)` with `.url: str`, `.get(path, raw=False)`, `.post(path, data=None, raw=False)`, `.rest(method, path, data=None, raw=False, timeout=30)` (returns parsed JSON, or text when `raw=True` or the body isn't JSON), `.ws(*commands) -> list` (one authenticated websocket session; each command is a dict with `type` plus fields; returns their `result`s in order), `.bearer() -> str` (for the screenshot browser only).

- [ ] **Step 1: Write the failing tests** `tools/hactl/tests/test_client.py`:

```python
import http.server
import json
import threading

import pytest

from hactl.client import Client, load_token
from hactl.errors import HactlError

TOKEN = "aaa.bbb.ccc"


def test_env_token_wins(tmp_path):
    f = tmp_path / "agent.secret.yaml"
    f.write_text("token: from.file.x\n")
    assert load_token(env={"HA_TOKEN": "from.env.x"}, token_file=f) == "from.env.x"


def test_file_token(tmp_path):
    f = tmp_path / "agent.secret.yaml"
    f.write_text('token: "abc.def.ghi"\n')
    assert load_token(env={}, token_file=f) == "abc.def.ghi"


def test_locked_file_is_explained(tmp_path):
    f = tmp_path / "agent.secret.yaml"
    f.write_bytes(b"\x00GITCRYPT\x00junk")
    with pytest.raises(HactlError, match="git-crypt unlock"):
        load_token(env={}, token_file=f)


def test_missing_file_is_explained(tmp_path):
    with pytest.raises(HactlError, match="no HA token"):
        load_token(env={}, token_file=tmp_path / "nope.yaml")


@pytest.fixture
def server():
    """A tiny HA stand-in."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            routes = {
                "/ok": (200, json.dumps({"message": "API running."})),
                "/text": (200, "2"),
                "/echo": (500, "bad: " + self.headers["Authorization"]),
                "/deny": (401, "401: Unauthorized"),
                "/bad-gateway": (502, "Bad Gateway"),
            }
            code, body = routes[self.path]
            self.send_response(code)
            self.end_headers()
            self.wfile.write(body.encode())

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_get_json(server):
    assert Client(url=server, token=TOKEN).get("/ok") == {"message": "API running."}


def test_raw_keeps_text(server):
    assert Client(url=server, token=TOKEN).get("/text", raw=True) == "2"


def test_error_body_never_leaks_token(server):
    with pytest.raises(HactlError) as e:
        Client(url=server, token=TOKEN).get("/echo")
    assert TOKEN not in str(e.value)
    assert "<token>" in str(e.value)


def test_401_is_explained(server):
    with pytest.raises(HactlError, match="rejected the token"):
        Client(url=server, token=TOKEN).get("/deny")


def test_502_is_reported(server):
    with pytest.raises(HactlError, match="HTTP 502"):
        Client(url=server, token=TOKEN).get("/bad-gateway")


def test_unreachable():
    with pytest.raises(HactlError, match="cannot reach HA"):
        Client(url="http://127.0.0.1:9", token=TOKEN).get("/ok")


def test_ws_refused():
    pytest.importorskip("websockets")
    with pytest.raises(HactlError, match="websocket"):
        Client(url="http://127.0.0.1:9", token=TOKEN).ws({"type": "ping"})
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_client.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'hactl.client'`.

- [ ] **Step 3: Implement `tools/hactl/src/hactl/client.py`:**

```python
"""Home Assistant REST + websocket client.

The token comes from $HA_TOKEN or tools/hactl/agent.secret.yaml (git-crypt).
It is never printed: error messages are redacted.
"""
import asyncio
import json
import os
import re
import urllib.error
import urllib.request

from hactl import paths
from hactl.errors import HactlError

DEFAULT_URL = "https://home.chrismiller.xyz"
_TOKEN_LINE = re.compile(rb"^token:\s*[\"']?([A-Za-z0-9._-]+)", re.M)


def load_token(env=os.environ, token_file=paths.TOKEN_FILE) -> str:
    tok = env.get("HA_TOKEN", "").strip()
    if tok:
        return tok
    if not token_file.exists():
        raise HactlError(f"no HA token: set HA_TOKEN or create {paths.rel(token_file)} (see docs/ha.md)")
    raw = token_file.read_bytes()
    if raw.startswith(b"\x00GITCRYPT"):
        raise HactlError(f"{paths.rel(token_file)} is encrypted; run `git-crypt unlock`")
    m = _TOKEN_LINE.search(raw)
    if not m:
        raise HactlError(f"{paths.rel(token_file)} has no `token:` line")
    return m.group(1).decode()


class Client:
    def __init__(self, url=None, token=None):
        self.url = (url or os.environ.get("HA_URL") or DEFAULT_URL).rstrip("/")
        self._token = token or load_token()

    def bearer(self) -> str:
        """The token, for the screenshot browser only. Never print it."""
        return self._token

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<token>")

    def rest(self, method, path, data=None, raw=False, timeout=30):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            self.url + path, data=body, method=method,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read().decode()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise HactlError("HA rejected the token (401): expired or revoked? see docs/ha.md") from None
            detail = e.read().decode(errors="replace")[:500]
            raise HactlError(f"{method} {path} -> HTTP {e.code}: {self._redact(detail)}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise HactlError(f"cannot reach HA at {self.url}: {getattr(e, 'reason', e)}") from None
        if raw:
            return text
        try:
            return json.loads(text)
        except ValueError:
            return text

    def get(self, path, raw=False):
        return self.rest("GET", path, raw=raw)

    def post(self, path, data=None, raw=False):
        return self.rest("POST", path, data if data is not None else {}, raw=raw)

    def ws(self, *commands) -> list:
        """Run websocket commands in one authenticated session; return their results."""
        return asyncio.run(self._ws(commands))

    async def _ws(self, commands):
        import websockets

        uri = self.url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
        try:
            async with websockets.connect(uri, max_size=None, open_timeout=15) as w:
                await w.recv()  # auth_required
                await w.send(json.dumps({"type": "auth", "access_token": self._token}))
                if json.loads(await w.recv()).get("type") != "auth_ok":
                    raise HactlError("HA rejected the token on the websocket: expired or revoked?")
                results = []
                for i, cmd in enumerate(commands, start=1):
                    await w.send(json.dumps({"id": i, **cmd}))
                    while True:
                        msg = json.loads(await w.recv())
                        if msg.get("id") == i and msg.get("type") == "result":
                            break
                    if not msg.get("success"):
                        err = msg.get("error") or {}
                        raise HactlError(f"{cmd['type']}: {err.get('code')}: {self._redact(str(err.get('message')))}")
                    results.append(msg.get("result"))
                return results
        except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as e:
            raise HactlError(f"websocket to {uri} failed: {self._redact(str(e))}") from None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_client.py`
Expected: `11 passed`.

- [ ] **Step 5: Create the token file** without printing the token, and confirm git-crypt covers it:

```bash
( umask 077 && printf 'token: %s\n' "$(cat ~/.config/galaxy/ha-token)" > tools/hactl/agent.secret.yaml )
git check-attr filter -- tools/hactl/agent.secret.yaml
```
Expected: `tools/hactl/agent.secret.yaml: filter: git-crypt`.

- [ ] **Step 6: Live check** (read-only):

Run: `nix develop --command bash -c 'cd tools/hactl && PYTHONPATH=src python3 -c "from hactl.client import Client; c = Client(); print(c.get(\"/api/\")); print(c.ws({\"type\": \"auth/current_user\"})[0][\"name\"])"'`
Expected: `{'message': 'API running.'}` then `Chris`.

- [ ] **Step 7: Commit, then prove the token is encrypted in the commit**

```bash
git add tools/hactl/src/hactl/client.py tools/hactl/tests/test_client.py tools/hactl/agent.secret.yaml
nix develop --command git commit -m "hactl: HA REST/websocket client; token in git-crypt" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git show HEAD:tools/hactl/agent.secret.yaml | head -c 9 | od -c | head -1
```
Expected: the `od` line shows `\0   G   I   T   C   R   Y   P   T`. If it doesn't, STOP: `git reset --soft HEAD~1`, fix `.gitattributes`, and re-commit. Never push a clear token.

---

### Task 3: Config revision and its pre-commit hook

**Files:**
- Create: `tools/hactl/src/hactl/revision.py`
- Create (generated): `cluster/home-assistant/packages/config_revision.yaml`
- Modify: `cluster/home-assistant/kustomization.yaml` (`ha-packages` files list)
- Modify: `.pre-commit-config.yaml` (new local hook)
- Modify: `tools/hactl/src/hactl/__main__.py` (`MODULES`)
- Test: `tools/hactl/tests/test_revision.py`

**Interfaces:**
- Consumes: `hactl.output`, `hactl.paths.HA_DIR`.
- Produces: `revision.SOURCES = ("packages", "dashboards", "themes")`, `revision.REVISION_FILE = Path("packages/config_revision.yaml")`, `compute(ha_dir) -> str` (12 hex chars), `render(rev) -> str`, `parse(text) -> str | None`, `is_current(ha_dir) -> bool`, `write(ha_dir) -> bool` (True when it changed the file). CLI: `hactl revision [--write | --check]`. The file's sensor is `sensor.ha_config_revision`; its value line matches `^\s*state:\s*"([0-9a-f]{12})"` (the hook in Task 11 parses the same line).

- [ ] **Step 1: Write the failing tests** `tools/hactl/tests/test_revision.py`:

```python
import re

from hactl import revision


def make_ha(tmp_path):
    for sub in revision.SOURCES:
        (tmp_path / sub).mkdir()
    (tmp_path / "packages" / "a.yaml").write_text("automation: []\n")
    (tmp_path / "dashboards" / "overview.yaml").write_text("views: []\n")
    (tmp_path / "themes" / "t.yaml").write_text("T: {}\n")
    return tmp_path


def test_stable_and_shaped(tmp_path):
    ha = make_ha(tmp_path)
    assert revision.compute(ha) == revision.compute(ha)
    assert re.fullmatch(r"[0-9a-f]{12}", revision.compute(ha))


def test_dashboard_change_changes_revision(tmp_path):
    ha = make_ha(tmp_path)
    before = revision.compute(ha)
    (ha / "dashboards" / "overview.yaml").write_text("views: [{title: x}]\n")
    assert revision.compute(ha) != before


def test_theme_change_changes_revision(tmp_path):
    ha = make_ha(tmp_path)
    before = revision.compute(ha)
    (ha / "themes" / "t.yaml").write_text("T: {a: 1}\n")
    assert revision.compute(ha) != before


def test_revision_file_does_not_hash_itself(tmp_path):
    ha = make_ha(tmp_path)
    before = revision.compute(ha)
    revision.write(ha)
    assert revision.compute(ha) == before


def test_write_is_idempotent(tmp_path):
    ha = make_ha(tmp_path)
    assert revision.write(ha) is True
    assert revision.write(ha) is False
    assert revision.is_current(ha)


def test_parse_roundtrip():
    assert revision.parse(revision.render("0123456789ab")) == "0123456789ab"
    assert revision.parse("no revision here") is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_revision.py`
Expected: FAIL — `ImportError: cannot import name 'revision'`.

- [ ] **Step 3: Implement `tools/hactl/src/hactl/revision.py`:**

```python
"""Config revision: one hash over everything git ships to HA's /config.

The pre-commit hook writes it into packages/config_revision.yaml as a template
sensor. The ha-reload hook reloads HA until sensor.ha_config_revision shows it,
and `hactl health` compares it with the repo to detect drift.
"""
import hashlib
import re
from pathlib import Path

from hactl import output, paths

SOURCES = ("packages", "dashboards", "themes")
REVISION_FILE = Path("packages") / "config_revision.yaml"
_STATE = re.compile(r'^\s*state:\s*"([0-9a-f]{12})"', re.M)

TEMPLATE = """\
# GENERATED by pre-commit (hactl revision --write). Do not edit.
# A hash of packages/, dashboards/ and themes/. The ha-reload PostSync hook
# reloads Home Assistant until sensor.ha_config_revision shows this value, and
# `hactl health` compares it with the repo to detect drift.
template:
  - sensor:
      - name: HA Config Revision
        unique_id: ha_config_revision
        icon: mdi:source-commit
        state: "{rev}"
"""


def compute(ha_dir: Path = paths.HA_DIR) -> str:
    h = hashlib.sha256()
    for sub in SOURCES:
        for f in sorted((ha_dir / sub).glob("*.yaml")):
            relpath = f.relative_to(ha_dir)
            if relpath == REVISION_FILE:
                continue
            h.update(str(relpath).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:12]


def render(rev: str) -> str:
    return TEMPLATE.format(rev=rev)


def parse(text: str):
    m = _STATE.search(text)
    return m.group(1) if m else None


def is_current(ha_dir: Path = paths.HA_DIR) -> bool:
    f = ha_dir / REVISION_FILE
    return f.exists() and f.read_text() == render(compute(ha_dir))


def write(ha_dir: Path = paths.HA_DIR) -> bool:
    """Write the revision file. Returns True when it changed."""
    if is_current(ha_dir):
        return False
    (ha_dir / REVISION_FILE).write_text(render(compute(ha_dir)))
    return True


def _run(args) -> int:
    if args.write:
        if write():
            print(f"hactl: updated {REVISION_FILE} ({compute()}); `git add` it and commit again")
            return 1
        return 0
    if args.check:
        if not is_current():
            print(f"hactl: {REVISION_FILE} is stale; run `hactl revision --write`")
            return 1
        return 0
    rev = compute()
    output.emit(args, {"revision": rev}, rev)
    return 0


def register(sub) -> None:
    p = sub.add_parser("revision", parents=[output.COMMON], help="config revision hash (pre-commit runs --write)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--write", action="store_true", help="update packages/config_revision.yaml; exit 1 if it changed")
    g.add_argument("--check", action="store_true", help="exit 1 if packages/config_revision.yaml is stale")
    p.set_defaults(func=_run)
```

In `__main__.py` set `MODULES: list[str] = ["revision"]`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass (`test_every_module_registers[revision]` included).

- [ ] **Step 5: Generate the file and wire it in.** Run `nix develop --command hactl revision --write` (prints `updated …`, exits 1 — expected the first time). In `cluster/home-assistant/kustomization.yaml`, add to the `ha-packages` `files:` list, after `- packages/prometheus.yaml`:

```yaml
      - packages/config_revision.yaml
```

In `.pre-commit-config.yaml`, under `repos: - repo: local  hooks:`, after the `mirror-family-list` hook, add:

```yaml
      # Home Assistant config revision. A hash of packages/, dashboards/ and
      # themes/ written into packages/config_revision.yaml as a sensor; the
      # ha-reload PostSync hook reloads HA until the sensor matches, so every
      # change provably lands. Behaves like a formatter: if it rewrites the
      # file, the commit stops so you can `git add` it.
      - id: ha-config-revision
        name: HA config revision (packages/config_revision.yaml)
        language: system
        entry: tools/hactl/bin/hactl revision --write
        pass_filenames: false
        files: ^cluster/home-assistant/(packages|dashboards|themes)/.*\.yaml$
```

- [ ] **Step 6: Verify kustomize still renders and the file is in the ConfigMap**

Run: `nix develop --command bash -c 'kustomize build cluster/home-assistant | grep -c "config_revision.yaml"'`
Expected: `1` (or more).

- [ ] **Step 7: Commit** (if the revision hook rewrites the file, `git add` it and run the commit again)

```bash
git add tools/hactl cluster/home-assistant/packages/config_revision.yaml cluster/home-assistant/kustomization.yaml .pre-commit-config.yaml
nix develop --command git commit -m "hactl: config revision sensor + pre-commit hook" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: HA-aware YAML loading and entity references

**Files:**
- Create: `tools/hactl/src/hactl/yamlload.py`
- Create: `tools/hactl/src/hactl/refs.py`
- Test: `tools/hactl/tests/test_yamlload.py`, `tools/hactl/tests/test_refs.py`

**Interfaces:**
- Consumes: `hactl.errors.HactlError`.
- Produces: `yamlload.Tagged(tag: str, value)` (frozen dataclass); `yamlload.load(path, *, resolve_includes=False, allow_secret=True)`; `yamlload.load_dir(directory) -> dict[str, object]` (stem → data for `*.yaml`); `refs.ENTITY_DOMAINS`; `refs.extract_refs(obj) -> set[str]`; `refs.slugify(text) -> str`; `refs.declared_entities(packages: dict) -> set[str]`.

- [ ] **Step 1: Write the failing tests.**

`tools/hactl/tests/test_yamlload.py`:

```python
import pytest

from hactl import yamlload
from hactl.errors import HactlError


def test_include_kept_as_tag_by_default(tmp_path):
    (tmp_path / "root.yaml").write_text("views:\n  - !include view.yaml\n")
    data = yamlload.load(tmp_path / "root.yaml")
    assert data["views"][0] == yamlload.Tagged("!include", "view.yaml")


def test_include_resolved_relative_and_nested(tmp_path):
    (tmp_path / "root.yaml").write_text("views:\n  - !include view.yaml\n")
    (tmp_path / "view.yaml").write_text("title: Home\ncards:\n  - !include card.yaml\n")
    (tmp_path / "card.yaml").write_text("type: markdown\n")
    data = yamlload.load(tmp_path / "root.yaml", resolve_includes=True)
    assert data == {"views": [{"title": "Home", "cards": [{"type": "markdown"}]}]}


def test_missing_include_is_explained(tmp_path):
    (tmp_path / "root.yaml").write_text("views:\n  - !include nope.yaml\n")
    with pytest.raises(HactlError, match="nope.yaml"):
        yamlload.load(tmp_path / "root.yaml", resolve_includes=True)


def test_secret_allowed_or_refused(tmp_path):
    (tmp_path / "p.yaml").write_text("x: !secret home_wifi_ssid\n")
    assert yamlload.load(tmp_path / "p.yaml")["x"] == yamlload.Tagged("!secret", "home_wifi_ssid")
    with pytest.raises(HactlError, match="!secret"):
        yamlload.load(tmp_path / "p.yaml", allow_secret=False)


def test_blueprint_input_tags_load(tmp_path):
    (tmp_path / "bp.yaml").write_text(
        "blueprint:\n  name: x\ntriggers:\n  - trigger: state\n    entity_id: !input linked\n"
        "env: !env_var HOME\ndir: !include_dir_named packages/\n"
    )
    data = yamlload.load(tmp_path / "bp.yaml")
    assert data["triggers"][0]["entity_id"] == yamlload.Tagged("!input", "linked")
    assert data["env"] == yamlload.Tagged("!env_var", "HOME")
    assert data["dir"] == yamlload.Tagged("!include_dir_named", "packages/")


def test_load_dir(tmp_path):
    (tmp_path / "a.yaml").write_text("automation: []\n")
    (tmp_path / "b.yaml").write_text("template: []\n")
    assert yamlload.load_dir(tmp_path) == {"a": {"automation": []}, "b": {"template": []}}
```

`tools/hactl/tests/test_refs.py`:

```python
from hactl import refs

PACKAGE = {
    "automation": [{
        "id": "x",
        "alias": "Bedroom Switch Master",
        "triggers": [{"trigger": "state", "entity_id": ["switch.bedroom_bedroom_light_switch"], "to": "on"}],
        "conditions": [{
            "condition": "template",
            "value_template": "{{ is_state('person.chris_m', 'home') and states.light.floor_lamp.state == 'on' }}",
        }],
        "actions": [
            {"action": "light.turn_on", "target": {"entity_id": "light.bedroom_all"}},
            {"action": "script.room_auto", "data": {"room": "bedroom"}},
        ],
    }],
    "scene": [{"name": "Bedroom Night", "entities": {"light.nightstand_lamp": {"state": "on", "brightness": 3}}}],
    "template": [{"sensor": [{"name": "HA Config Revision", "state": "x"}]}],
    "input_boolean": {"guest_mode": {"name": "Guest mode"}},
    "script": {"room_auto": {"sequence": []}},
    "light": [{"platform": "group", "name": "Bedroom All", "entities": ["light.dresser_lamp"]}],
}


def test_extract_refs_finds_entities_not_services():
    assert refs.extract_refs(PACKAGE) == {
        "switch.bedroom_bedroom_light_switch",
        "person.chris_m",
        "light.floor_lamp",
        "light.bedroom_all",
        "light.nightstand_lamp",
        "light.dresser_lamp",
    }


def test_extract_refs_ignores_non_entities():
    card = {
        "type": "custom:mushroom-chips-card",
        "icon": "mdi:lock",
        "url": "https://home.chrismiller.xyz/x",
        "content": "{{ states.light | selectattr('state','eq','on') | list | count }}",
        "perform_action": "light.toggle",
    }
    assert refs.extract_refs(card) == set()


def test_slugify_matches_ha():
    assert refs.slugify("HA Config Revision") == "ha_config_revision"
    assert refs.slugify("Presence - Lights Off When Away") == "presence_lights_off_when_away"


def test_declared_entities():
    assert refs.declared_entities({"pkg": PACKAGE}) == {
        "automation.bedroom_switch_master",
        "scene.bedroom_night",
        "sensor.ha_config_revision",
        "input_boolean.guest_mode",
        "script.room_auto",
        "light.bedroom_all",
    }
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_yamlload.py tools/hactl/tests/test_refs.py`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `tools/hactl/src/hactl/yamlload.py`:**

```python
"""YAML loading with Home Assistant's custom tags.

HA config uses !include, !include_dir_*, !secret, !env_var and !input, which
plain yaml.safe_load rejects. `load` keeps them as Tagged placeholders, or
resolves !include when asked (dashboards pushed to the preview).
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from hactl.errors import HactlError


@dataclass(frozen=True)
class Tagged:
    tag: str
    value: Any


def _construct_any(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


def load(path, *, resolve_includes: bool = False, allow_secret: bool = True) -> Any:
    path = Path(path)

    class Loader(yaml.SafeLoader):
        pass

    def include(loader, node):
        name = loader.construct_scalar(node)
        if not resolve_includes:
            return Tagged("!include", name)
        target = path.parent / name
        if not target.is_file():
            raise HactlError(f"{path.name}: !include {name}: no such file")
        return load(target, resolve_includes=True, allow_secret=allow_secret)

    def secret(loader, node):
        name = loader.construct_scalar(node)
        if not allow_secret:
            raise HactlError(f"{path.name}: !secret {name} is not allowed here")
        return Tagged("!secret", name)

    def other(loader, suffix, node):
        return Tagged("!" + suffix, _construct_any(loader, node))

    Loader.add_constructor("!include", include)
    Loader.add_constructor("!secret", secret)
    Loader.add_multi_constructor("!", other)
    try:
        with path.open() as f:
            return yaml.load(f, Loader=Loader)
    except yaml.YAMLError as e:
        raise HactlError(f"{path}: {e}") from None


def load_dir(directory) -> dict:
    """Every *.yaml in a directory, keyed by file stem (like !include_dir_named)."""
    return {f.stem: load(f) for f in sorted(Path(directory).glob("*.yaml"))}
```

- [ ] **Step 4: Implement `tools/hactl/src/hactl/refs.py`:**

```python
"""Entity references in HA config, and the entities the config itself declares."""
import re

from hactl.yamlload import Tagged

ENTITY_DOMAINS = frozenset("""
alarm_control_panel automation binary_sensor button calendar camera climate
cover device_tracker event fan group image input_boolean input_button
input_datetime input_number input_select input_text light lock media_player
notify number person remote scene script select sensor siren sun switch text
time timer todo update vacuum valve water_heater weather zone
""".split())

# Values under these keys are actions (light.turn_on), not entities.
SERVICE_KEYS = frozenset({"action", "service", "perform_action"})

_DOMAINS = "|".join(sorted(ENTITY_DOMAINS, key=len, reverse=True))
_REF = re.compile(rf"(?<![\w.])({_DOMAINS})\.([a-z0-9_]+)(?![\w(])")
_FULL = re.compile(rf"^({_DOMAINS})\.[a-z0-9_]+$")
_TEMPLATE_PLATFORMS = frozenset({
    "sensor", "binary_sensor", "switch", "number", "select", "button", "image",
    "weather", "light", "fan", "cover", "lock", "alarm_control_panel", "vacuum", "event", "update",
})


def extract_refs(obj, _key=None) -> set:
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and _FULL.match(k):
                out.add(k)  # scene `entities:` and customize use entity ids as keys
            out |= extract_refs(v, k)
    elif isinstance(obj, list):
        for v in obj:
            out |= extract_refs(v, _key)
    elif isinstance(obj, Tagged):
        out |= extract_refs(obj.value, _key)
    elif isinstance(obj, str) and _key not in SERVICE_KEYS:
        # states.light.floor_lamp.state is a reference too
        text = obj.replace("states.", "")
        out |= {f"{d}.{o}" for d, o in _REF.findall(text)}
    return out


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _named(items, domain) -> set:
    return {
        f"{domain}.{slugify(i['name'])}"
        for i in items or []
        if isinstance(i, dict) and isinstance(i.get("name"), str) and "{" not in i["name"]
    }


def declared_entities(packages: dict) -> set:
    """Entity ids the packages create (so lint accepts refs to not-yet-deployed ones)."""
    out = set()
    for pkg in packages.values():
        if not isinstance(pkg, dict):
            continue
        for domain, body in pkg.items():
            if (domain.startswith("input_") or domain in ("timer", "counter", "schedule", "script")) and isinstance(body, dict):
                out |= {f"{domain}.{key}" for key in body}
            elif domain == "automation" and isinstance(body, list):
                out |= {f"automation.{slugify(a['alias'])}" for a in body if isinstance(a, dict) and a.get("alias")}
            elif domain == "scene" and isinstance(body, list):
                out |= _named(body, "scene")
            elif domain == "template" and isinstance(body, list):
                for block in body:
                    for kind, items in (block or {}).items():
                        if kind in _TEMPLATE_PLATFORMS:
                            out |= _named(items, kind)
            elif isinstance(body, list):  # platform lists: light: - platform: group
                out |= _named([i for i in body if isinstance(i, dict) and i.get("platform")], domain)
    return out
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass.

- [ ] **Step 6: Sanity-check against the real config**

Run: `nix develop --command bash -c 'cd tools/hactl && PYTHONPATH=src python3 -c "from hactl import yamlload, refs, paths; p = yamlload.load_dir(paths.HA_DIR / \"packages\"); print(len(refs.extract_refs(p)), \"refs\"); print(sorted(refs.declared_entities(p))[:5])"'`
Expected: a few dozen refs and a list of declared ids like `automation.…`, no exception.

- [ ] **Step 7: Commit**

```bash
git add tools/hactl
nix develop --command git commit -m "hactl: HA-tag YAML loader and entity reference extraction" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Offline lint, and CI runs it

**Files:**
- Create: `tools/hactl/src/hactl/lint.py`
- Modify: `tools/hactl/src/hactl/__main__.py` (`MODULES`)
- Modify: `cluster/home-assistant/packages/morning_routine.yaml:106` (quote the time)
- Modify: `.github/workflows/ha-check-config.yaml` (replace)
- Test: `tools/hactl/tests/test_lint.py`

**Interfaces:**
- Consumes: `revision.is_current`, `revision.REVISION_FILE`, `paths.*`, `output.*`.
- Produces: `lint.Finding(rule, path, line, message, severity="error")` (dataclass, `str()` renders one line); `check_filenames(ha_dir)`, `check_kustomization(ha_dir)`, `check_quoting(ha_dir)`, `check_revision(ha_dir)`, `check_config(ha_dir, tag)`, `deployed_tag(release_file) -> str`, `offline(ha_dir, run_check_config=True) -> list[Finding]`; `lint._run(args)` (Task 9 replaces it). CLI: `hactl lint [--offline] [--no-check-config]`.

- [ ] **Step 1: Write the failing tests** `tools/hactl/tests/test_lint.py`:

```python
from hactl import lint, revision


def make_ha(tmp_path, files):
    for sub in revision.SOURCES:
        (tmp_path / sub).mkdir(exist_ok=True)
    for rel, text in files.items():
        (tmp_path / rel).write_text(text)
    listed = "".join(f"      - {r}\n" for r in files if r.split("/")[0] in revision.SOURCES)
    (tmp_path / "kustomization.yaml").write_text(f"configMapGenerator:\n  - name: ha-packages\n    files:\n{listed}")
    return tmp_path


def rules(findings):
    return sorted((f.rule, f.line) for f in findings)


def test_hyphenated_package_filename(tmp_path):
    ha = make_ha(tmp_path, {"packages/morning-routine.yaml": "automation: []\n"})
    assert rules(lint.check_filenames(ha)) == [("filename", None)]


def test_unlisted_and_missing_kustomization_entries(tmp_path):
    ha = make_ha(tmp_path, {"packages/a.yaml": "automation: []\n"})
    (ha / "packages" / "b.yaml").write_text("automation: []\n")
    (ha / "kustomization.yaml").write_text(
        "configMapGenerator:\n  - name: ha-packages\n    files:\n      - packages/a.yaml\n      - packages/gone.yaml\n"
    )
    msgs = [f.message for f in lint.check_kustomization(ha)]
    assert any("never reach HA" in m for m in msgs)
    assert any("gone.yaml" in m for m in msgs)


def test_quoting(tmp_path):
    ha = make_ha(tmp_path, {"packages/a.yaml": (
        "automation:\n"
        "  - triggers:\n"
        "      - trigger: state\n"
        "        to: on\n"          # line 4: bad
        "        from: 'off'\n"     # fine
        "        for: 03:00:00\n"   # line 6: bad
        "    at: \"22:30:00\"\n"    # fine
    )})
    assert rules(lint.check_quoting(ha)) == [("quoting", 4), ("quoting", 6)]


def test_stale_revision(tmp_path):
    ha = make_ha(tmp_path, {"packages/a.yaml": "automation: []\n"})
    assert rules(lint.check_revision(ha)) == [("revision", None)]
    revision.write(ha)
    assert lint.check_revision(ha) == []


def test_deployed_tag(tmp_path):
    f = tmp_path / "release.yaml"
    f.write_text("valuesObject:\n  image:\n    tag: 2026.7.2\n")
    assert lint.deployed_tag(f) == "2026.7.2"


def test_finding_renders_location():
    assert "packages/a.yaml:4" in str(lint.Finding("quoting", "packages/a.yaml", 4, "x"))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_lint.py`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `tools/hactl/src/hactl/lint.py`:**

```python
"""Config lint: the rules that keep HA config deployable.

`lint --offline` needs no HA (CI runs it). Plain `lint` adds live checks.
"""
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from hactl import output, paths, revision
from hactl.errors import HactlError

_SLUG = re.compile(r"^[a-z0-9_]+\.yaml$")
_BOOLISH = re.compile(r"^\s*(?:-\s+)?[A-Za-z_][\w-]*:\s+(on|off|yes|no|On|Off|ON|OFF|Yes|No|YES|NO)\s*(?:#.*)?$")
_TIME = re.compile(r"^\s*(?:-\s+)?[A-Za-z_][\w-]*:\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:#.*)?$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_CHECK_MARKERS = re.compile(
    r"ERROR|Invalid config|could not be validated|will not be initialized|invalid slug|Setup of package .* failed|Failed to",
    re.I,
)
_SECRET = re.compile(r"!secret\s+([A-Za-z0-9_]+)")
# Custom integrations the packages configure; check_config needs their code.
CUSTOM_COMPONENTS = {"adaptive_lighting": "https://github.com/basnijholt/adaptive-lighting"}


@dataclass
class Finding:
    rule: str
    path: str
    line: int | None
    message: str
    severity: str = "error"

    def __str__(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line else self.path
        return f"{self.severity.upper():7} {self.rule:16} {loc}  {self.message}"


def check_filenames(ha_dir: Path) -> list:
    return [
        Finding("filename", paths.rel(f), None, "package filenames must be lowercase with underscores (HA silently skips others)")
        for f in sorted((ha_dir / "packages").glob("*.yaml"))
        if not _SLUG.match(f.name)
    ]


def check_kustomization(ha_dir: Path) -> list:
    k = yaml.safe_load((ha_dir / "kustomization.yaml").read_text()) or {}
    listed = {f for g in k.get("configMapGenerator", []) for f in g.get("files", [])}
    out = []
    for sub in revision.SOURCES:
        for f in sorted((ha_dir / sub).glob("*.yaml")):
            if f"{sub}/{f.name}" not in listed:
                out.append(Finding("kustomization", paths.rel(f), None,
                                   "not listed in kustomization.yaml configMapGenerator; it will never reach HA"))
    for relf in sorted(listed):
        if not (ha_dir / relf).exists():
            out.append(Finding("kustomization", paths.rel(ha_dir / "kustomization.yaml"), None, f"lists {relf}, which does not exist"))
    return out


def check_quoting(ha_dir: Path) -> list:
    out = []
    for sub in revision.SOURCES:
        for f in sorted((ha_dir / sub).glob("*.yaml")):
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if m := _BOOLISH.match(line):
                    out.append(Finding("quoting", paths.rel(f), n, f"unquoted {m.group(1)} becomes a boolean in YAML 1.1; write '{m.group(1)}'"))
                elif m := _TIME.match(line):
                    out.append(Finding("quoting", paths.rel(f), n, f'unquoted {m.group(1)} becomes a number in YAML 1.1; write "{m.group(1)}"'))
    return out


def check_revision(ha_dir: Path) -> list:
    if revision.is_current(ha_dir):
        return []
    return [Finding("revision", paths.rel(ha_dir / revision.REVISION_FILE), None, "stale; run `hactl revision --write`")]


def deployed_tag(release_file: Path = paths.RELEASE_FILE) -> str:
    m = re.search(r"tag:\s*(\d{4}\.\d+\.\d+)", release_file.read_text())
    if not m:
        raise HactlError(f"no HA image tag found in {paths.rel(release_file)}")
    return m.group(1)


def _docker_ok() -> bool:
    return bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def check_config(ha_dir: Path, tag: str) -> list:
    """HA's own check_config in the deployed image. Its exit code is meaningless; grep it."""
    if not _docker_ok():
        return [Finding("check_config", "", None, "docker unavailable: check_config was NOT run", "warning")]
    with tempfile.TemporaryDirectory(prefix="hactl-check-") as tmp:
        cfg = Path(tmp) / "config"
        shutil.copytree(ha_dir / "packages", cfg / "packages")
        shutil.copytree(ha_dir / "dashboards", cfg / "dashboards")
        if (ha_dir / "blueprints").exists():
            shutil.copytree(ha_dir / "blueprints", cfg / "blueprints")
        (cfg / "configuration.yaml").write_text("homeassistant:\n  packages: !include_dir_named packages/\n")
        names = sorted({n for f in (cfg / "packages").glob("*.yaml") for n in _SECRET.findall(f.read_text())})
        (cfg / "secrets.yaml").write_text("".join(f'{n}: "lint-dummy"\n' for n in names))
        components = cfg / "custom_components"
        components.mkdir()
        for name, repo in CUSTOM_COMPONENTS.items():
            src = Path(tmp) / f"{name}-src"
            subprocess.run(["git", "clone", "-q", "--depth", "1", repo, str(src)], check=True)
            shutil.copytree(src / "custom_components" / name, components / name)
        owner = f"{os.getuid()}:{os.getgid()}"
        r = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{cfg}:/config", "--entrypoint", "sh",
             f"docker.io/homeassistant/home-assistant:{tag}", "-c",
             f"python -m homeassistant --script check_config -c /config; chown -R {owner} /config"],
            capture_output=True, text=True,
        )
        text = _ANSI.sub("", r.stdout + r.stderr)
        return [Finding("check_config", f"(HA {tag})", None, line.strip()) for line in text.splitlines() if _CHECK_MARKERS.search(line)]


def offline(ha_dir: Path = paths.HA_DIR, run_check_config: bool = True) -> list:
    findings = check_filenames(ha_dir) + check_kustomization(ha_dir) + check_quoting(ha_dir) + check_revision(ha_dir)
    if run_check_config:
        findings += check_config(ha_dir, deployed_tag())
    return findings


def _run(args) -> int:
    findings = offline(paths.HA_DIR, run_check_config=not args.no_check_config)
    output.emit(args, [asdict(f) for f in findings], [str(f) for f in findings] or ["lint: clean"])
    return 1 if any(f.severity == "error" for f in findings) else 0


def register(sub) -> None:
    p = sub.add_parser("lint", parents=[output.COMMON], help="check HA config (CI runs --offline)")
    p.add_argument("--offline", action="store_true", help="only the checks that need no HA (what CI runs)")
    p.add_argument("--no-check-config", action="store_true", help="skip the docker check_config run")
    p.set_defaults(func=_run)
```

In `__main__.py`: `MODULES: list[str] = ["revision", "lint"]`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass.

- [ ] **Step 5: Lint the real config.** Run: `nix develop --command hactl lint --offline`
Expected: one error, `quoting … packages/morning_routine.yaml:106 unquoted 03:00:00 …` (plus the check_config result). Fix it: change line 106 of `cluster/home-assistant/packages/morning_routine.yaml` from `for: 03:00:00` to `for: "03:00:00"` (same meaning — YAML 1.1 read it as 10800 s). Then `nix develop --command hactl revision --write` and rerun lint.
Expected: `lint: clean`. If docker is unavailable locally you will see a `WARNING check_config … NOT run` line; that is acceptable locally (CI runs it). If check_config reports errors, fix them before continuing.

- [ ] **Step 6: Replace `.github/workflows/ha-check-config.yaml`** with:

```yaml
---
name: HA config check

# Pre-merge gate for git-tracked Home Assistant config and the hactl toolkit:
# hactl's unit tests, then `hactl lint --offline` (package filenames,
# kustomization listing, YAML 1.1 quoting, config revision, and HA's own
# check_config in the SAME image version Argo deploys).

on:
  pull_request:
    paths:
      - cluster/home-assistant/**
      - cluster/applications/home-assistant-release.yaml
      - tools/hactl/**
      - .github/workflows/ha-check-config.yaml
  push:
    branches: [main]
    paths:
      - cluster/home-assistant/**
      - cluster/applications/home-assistant-release.yaml
      - tools/hactl/**
      - .github/workflows/ha-check-config.yaml

jobs:
  check-config:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - name: Install hactl's offline dependencies
        run: pip install pyyaml pytest
      - name: hactl unit tests
        run: python -m pytest -q tools/hactl
      - name: hactl lint --offline (includes check_config)
        run: tools/hactl/bin/hactl lint --offline
```

- [ ] **Step 7: Commit** (yamlfmt may reformat the workflow; `git add` and commit again if it does)

```bash
git add tools/hactl cluster/home-assistant/packages .github/workflows/ha-check-config.yaml
nix develop --command git commit -m "hactl: offline lint; CI runs it; quote morning routine's 03:00:00" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Query commands

**Files:**
- Create: `tools/hactl/src/hactl/query.py`
- Modify: `tools/hactl/src/hactl/__main__.py` (`MODULES`)
- Test: `tools/hactl/tests/test_query.py`

**Interfaces:**
- Consumes: `client.Client` (`get`, `post(raw=True)`, `ws`), `output.*`, `errors.HactlError`.
- Produces: `query.Row` (dataclass: `entity_id, name, state, area, device, integration, disabled`); `build_index(states, entities, devices, areas) -> list[Row]`; `filter_rows(rows, text=None, area=None, domain=None, integration=None, unavailable=False, include_disabled=False) -> list[Row]`; `find_rows(client, **filters) -> list[Row]`; `parse_since(text) -> timedelta`; `fetch_history(client, ids, since) -> list[tuple[datetime, str, str]]`; `detect_flaps(events, window_s=60, threshold=6) -> list[dict]` (keys `entity_id, start, end, changes`); `fetch_stats(client, statistic_id, days) -> list[dict]`; `resolve_automation_id(client, ref) -> str`; `summarize_trace(trace) -> dict`; `format_log(entries, errors_only=False, since=None) -> list[dict]` (keys `count, level, name, message, last`). CLI: `find`, `state`, `history`, `stats`, `template`, `trace`, `log`.

- [ ] **Step 1: Write the failing tests** `tools/hactl/tests/test_query.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from hactl import query
from hactl.errors import HactlError

T0 = datetime(2026, 10, 3, 15, 21, 34, tzinfo=timezone.utc)

STATES = [
    {"entity_id": "light.floor_lamp", "state": "on", "attributes": {"friendly_name": "Floor Lamp"}},
    {"entity_id": "sensor.yaml_only", "state": "unavailable", "attributes": {"friendly_name": "YAML Only"}},
]
ENTITIES = [
    {"entity_id": "light.floor_lamp", "device_id": "d1", "area_id": None, "platform": "hue", "name": None,
     "original_name": "Floor lamp", "disabled_by": None},
    {"entity_id": "switch.old", "device_id": None, "area_id": "kitchen", "platform": "mqtt", "name": "Old",
     "disabled_by": "user"},
]
DEVICES = [{"id": "d1", "area_id": "bedroom", "name": "Hue lamp", "name_by_user": None}]
AREAS = [{"area_id": "bedroom", "name": "Bedroom"}, {"area_id": "kitchen", "name": "Kitchen"}]


def test_build_index_area_falls_back_to_device_and_keeps_yaml_entities():
    rows = {r.entity_id: r for r in query.build_index(STATES, ENTITIES, DEVICES, AREAS)}
    assert rows["light.floor_lamp"].area == "Bedroom"
    assert rows["light.floor_lamp"].name == "Floor Lamp"
    assert rows["sensor.yaml_only"].state == "unavailable"
    assert rows["switch.old"].disabled


def test_filter_rows():
    rows = query.build_index(STATES, ENTITIES, DEVICES, AREAS)
    assert [r.entity_id for r in query.filter_rows(rows, area="bedroom")] == ["light.floor_lamp"]
    assert [r.entity_id for r in query.filter_rows(rows, unavailable=True)] == ["sensor.yaml_only"]
    assert [r.entity_id for r in query.filter_rows(rows, text="old")] == []
    assert [r.entity_id for r in query.filter_rows(rows, text="old", include_disabled=True)] == ["switch.old"]


def test_parse_since():
    assert query.parse_since("90m") == timedelta(minutes=90)
    assert query.parse_since("3d") == timedelta(days=3)
    with pytest.raises(HactlError):
        query.parse_since("yesterday")


def test_detect_flaps_finds_the_bedroom_burst():
    events = [(T0 + timedelta(seconds=i), "switch.bedroom", "on" if i % 2 else "off") for i in range(18)]
    events += [(T0 + timedelta(hours=h), "light.calm", "on") for h in range(1, 6)]
    bursts = query.detect_flaps(events)
    assert len(bursts) == 1
    assert bursts[0]["entity_id"] == "switch.bedroom"
    assert bursts[0]["changes"] == 18


def test_detect_flaps_quiet():
    events = [(T0 + timedelta(minutes=10 * i), "light.x", "on") for i in range(10)]
    assert query.detect_flaps(events) == []


def test_summarize_trace_orders_steps_and_keeps_errors():
    trace = {
        "run_id": "r1", "script_execution": "error", "error": "SwitchBot Cloud device is offline",
        "timestamp": {"start": "2026-10-03T15:00:00+00:00"}, "trigger": "time pattern",
        "trace": {
            "action/0": [{"path": "action/0", "timestamp": "2026-10-03T15:00:02+00:00", "error": "offline",
                          "result": {"params": {"domain": "cover"}}}],
            "trigger/0": [{"path": "trigger/0", "timestamp": "2026-10-03T15:00:01+00:00"}],
        },
    }
    s = query.summarize_trace(trace)
    assert [st["path"] for st in s["steps"]] == ["trigger/0", "action/0"]
    assert s["steps"][1]["error"] == "offline"
    assert s["error"] == "SwitchBot Cloud device is offline"


def test_format_log():
    entries = [
        {"name": "a", "level": "WARNING", "message": ["w"], "count": 3, "timestamp": 100.0},
        {"name": "b", "level": "ERROR", "message": ["e\nmore"], "count": 9, "timestamp": 200.0},
    ]
    rows = query.format_log(entries, errors_only=True)
    assert [(r["name"], r["count"], r["message"]) for r in rows] == [("b", 9, "e more")]
    assert [r["name"] for r in query.format_log(entries)] == ["b", "a"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_query.py`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `tools/hactl/src/hactl/query.py`:**

```python
"""Read-only queries: find, state, history, stats, template, trace, log."""
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from hactl import output
from hactl.errors import HactlError


@dataclass
class Row:
    entity_id: str
    name: str
    state: str
    area: str
    device: str
    integration: str
    disabled: bool


def build_index(states, entities, devices, areas) -> list:
    area_names = {a["area_id"]: a["name"] for a in areas}
    devs = {d["id"]: d for d in devices}
    st = {s["entity_id"]: s for s in states}
    rows, seen = [], set()
    for e in entities:
        d = devs.get(e.get("device_id") or "", {})
        s = st.get(e["entity_id"], {})
        rows.append(Row(
            entity_id=e["entity_id"],
            name=e.get("name") or s.get("attributes", {}).get("friendly_name") or e.get("original_name") or "",
            state=s.get("state", "-"),
            area=area_names.get(e.get("area_id") or d.get("area_id"), ""),
            device=d.get("name_by_user") or d.get("name") or "",
            integration=e.get("platform", ""),
            disabled=bool(e.get("disabled_by")),
        ))
        seen.add(e["entity_id"])
    for s in states:  # YAML entities without a unique_id are not in the registry
        if s["entity_id"] not in seen:
            rows.append(Row(s["entity_id"], s.get("attributes", {}).get("friendly_name", ""), s["state"], "", "", "", False))
    return sorted(rows, key=lambda r: r.entity_id)


def filter_rows(rows, text=None, area=None, domain=None, integration=None, unavailable=False, include_disabled=False) -> list:
    out = []
    for r in rows:
        if r.disabled and not include_disabled:
            continue
        if text and text.lower() not in f"{r.entity_id} {r.name}".lower():
            continue
        if area and area.lower().replace("_", " ") != r.area.lower():
            continue
        if domain and not r.entity_id.startswith(domain + "."):
            continue
        if integration and integration != r.integration:
            continue
        if unavailable and r.state not in ("unavailable", "unknown"):
            continue
        out.append(r)
    return out


def find_rows(client, **filters) -> list:
    states = client.get("/api/states")
    entities, devices, areas = client.ws(
        {"type": "config/entity_registry/list"},
        {"type": "config/device_registry/list"},
        {"type": "config/area_registry/list"},
    )
    return filter_rows(build_index(states, entities, devices, areas), **filters)


_SINCE = re.compile(r"^(\d+)([smhd])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_since(text: str) -> timedelta:
    m = _SINCE.match(text or "")
    if not m:
        raise HactlError(f"bad duration {text!r}: use e.g. 30m, 2h, 3d")
    return timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})


def fetch_history(client, ids, since: timedelta) -> list:
    start = datetime.now(timezone.utc) - since
    data = client.get(
        f"/api/history/period/{quote(start.isoformat())}?filter_entity_id={','.join(ids)}"
        "&minimal_response&no_attributes"
    )
    events = []
    for series in data or []:
        if not series:
            continue
        eid = series[0]["entity_id"]  # minimal_response: only the first item names it
        events += [(datetime.fromisoformat(i["last_changed"]), eid, i["state"]) for i in series]
    return sorted(events)


def detect_flaps(events, window_s=60, threshold=6) -> list:
    """A flap: >= threshold changes of one entity within window_s seconds. Bursts are merged."""
    by_entity = defaultdict(list)
    for t, e, _ in events:
        by_entity[e].append(t)
    bursts = []
    for e, ts in sorted(by_entity.items()):
        ts.sort()
        mine, start = [], 0
        for j, t in enumerate(ts):
            while (t - ts[start]).total_seconds() > window_s:
                start += 1
            if j - start + 1 >= threshold:
                if mine and ts[start] <= mine[-1]["end"]:
                    mine[-1]["end"] = t
                else:
                    mine.append({"entity_id": e, "start": ts[start], "end": t})
        for b in mine:
            b["changes"] = sum(1 for t in ts if b["start"] <= t <= b["end"])
        bursts += mine
    return bursts


def fetch_stats(client, statistic_id, days) -> list:
    start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    res = client.ws({
        "type": "recorder/statistics_during_period", "start_time": start,
        "statistic_ids": [statistic_id], "period": "day", "types": ["min", "max", "mean"],
    })[0]
    return [
        {"date": datetime.fromtimestamp(r["start"] / 1000, timezone.utc).date().isoformat(),
         "min": r.get("min"), "max": r.get("max"), "mean": r.get("mean")}
        for r in res.get(statistic_id, [])
    ]


def resolve_automation_id(client, ref: str) -> str:
    """automation.<x> entity id, or a bare config id -> config id."""
    if not ref.startswith("automation."):
        return ref
    cid = client.get(f"/api/states/{ref}").get("attributes", {}).get("id")
    if not cid:
        raise HactlError(f"{ref} has no config id (only automations with an `id:` keep traces)")
    return cid


_RESULT_KEYS = ("result", "params", "enabled", "choice", "delay", "done", "wait")


def summarize_trace(trace: dict) -> dict:
    steps = []
    for path, entries in (trace.get("trace") or {}).items():
        for en in entries:
            r = en.get("result") or {}
            steps.append({"path": path, "at": en.get("timestamp"), "error": en.get("error"),
                          "result": {k: r[k] for k in _RESULT_KEYS if k in r}})
    steps.sort(key=lambda s: s["at"] or "")
    return {"run_id": trace.get("run_id"), "start": (trace.get("timestamp") or {}).get("start"),
            "trigger": trace.get("trigger"), "execution": trace.get("script_execution"),
            "error": trace.get("error"), "steps": steps}


def format_log(entries, errors_only=False, since=None) -> list:
    rows = []
    for e in sorted(entries, key=lambda e: e["timestamp"], reverse=True):
        if errors_only and e["level"] not in ("ERROR", "CRITICAL"):
            continue
        if since is not None and e["timestamp"] < since:
            continue
        msg = (e.get("message") or [""])[0].replace("\n", " ")
        rows.append({"count": e.get("count", 1), "level": e["level"], "name": e["name"], "message": msg[:200],
                     "last": datetime.fromtimestamp(e["timestamp"], timezone.utc).isoformat(timespec="seconds")})
    return rows


# ---- CLI ---------------------------------------------------------------------

def _client():
    from hactl.client import Client
    return Client()


def _find(args):
    rows = find_rows(_client(), text=args.text, area=args.area, domain=args.domain, integration=args.integration,
                     unavailable=args.unavailable, include_disabled=args.disabled)
    lines = [f"{r.entity_id:52} {r.state[:16]:16} {r.area[:14]:14} {r.integration[:16]:16} {r.name}" for r in rows]
    output.emit(args, [asdict(r) for r in rows], lines or ["no matches"])


def _state(args):
    client = _client()
    st = client.get(f"/api/states/{args.entity_id}")
    try:
        reg = client.ws({"type": "config/entity_registry/get", "entity_id": args.entity_id})[0]
    except HactlError:
        reg = None  # YAML entity without a unique_id
    lines = [f"{st['entity_id']} = {st['state']}  (changed {st['last_changed']})"]
    lines += [f"  {k}: {v}" for k, v in sorted(st.get("attributes", {}).items())]
    if reg:
        lines.append(f"  registry: platform={reg.get('platform')} unique_id={reg.get('unique_id')} "
                     f"area={reg.get('area_id')} labels={reg.get('labels')} disabled={reg.get('disabled_by')}")
    output.emit(args, {"state": st, "registry": reg}, lines)


def _history(args):
    events = fetch_history(_client(), args.entity_ids, parse_since(args.since))
    flaps = detect_flaps(events, window_s=args.window, threshold=args.threshold)
    lines = [f"{t.isoformat(timespec='seconds')}  {e}  {s}" for t, e, s in events[-args.tail:]]
    lines += [f"FLAPPING {b['entity_id']}: {b['changes']} changes {b['start']:%Y-%m-%d %H:%M:%S}..{b['end']:%H:%M:%S}"
              for b in flaps]
    data = {"events": [{"time": t, "entity_id": e, "state": s} for t, e, s in events], "flaps": flaps}
    output.emit(args, data, lines or ["no changes"])


def _stats(args):
    rows = fetch_stats(_client(), args.statistic_id, args.days)
    lines = [f"{r['date']}  min {r['min']}  max {r['max']}  mean {round(r['mean'], 2) if r['mean'] is not None else None}"
             for r in rows]
    output.emit(args, rows, lines or ["no statistics (not a measurement sensor?)"])


def _template(args):
    text = Path(args.file).read_text() if args.file else args.template
    if not text:
        raise HactlError("give a template, or -f FILE")
    result = _client().post("/api/template", {"template": text}, raw=True)
    output.emit(args, {"result": result}, result)


def _trace(args):
    client = _client()
    cid = resolve_automation_id(client, args.automation)
    runs = client.ws({"type": "trace/list", "domain": "automation", "item_id": cid})[0]
    runs = sorted(runs, key=lambda t: (t.get("timestamp") or {}).get("start", ""), reverse=True)[: args.last]
    if not runs:
        output.emit(args, [], f"no stored runs for {cid}")
        return
    details = client.ws(*[{"type": "trace/get", "domain": "automation", "item_id": cid, "run_id": r["run_id"]} for r in runs])
    summaries = [summarize_trace(d) for d in details]
    lines = []
    for s in summaries:
        lines.append(f"{s['start']}  {s['execution']}  trigger: {s['trigger']}" + (f"  ERROR: {s['error']}" if s["error"] else ""))
        for st in s["steps"]:
            lines.append(f"    {st['path']:22} {st['result'] or ''}" + (f"  ERROR: {st['error']}" if st["error"] else ""))
    output.emit(args, summaries, lines)


def _log(args):
    since = (datetime.now(timezone.utc) - parse_since(args.since)).timestamp() if args.since else None
    rows = format_log(_client().ws({"type": "system_log/list"})[0], errors_only=args.errors, since=since)
    lines = [f"{r['count']:>5}x {r['level']:7} {r['last']}  {r['name']}: {r['message']}" for r in rows]
    output.emit(args, rows, lines or ["log is clean"])


def register(sub) -> None:
    p = sub.add_parser("find", parents=[output.COMMON], help="find entities by id/name, area, domain, integration")
    p.add_argument("text", nargs="?")
    p.add_argument("--area")
    p.add_argument("--domain")
    p.add_argument("--integration")
    p.add_argument("--unavailable", action="store_true", help="only unavailable/unknown")
    p.add_argument("--disabled", action="store_true", help="include disabled entities")
    p.set_defaults(func=_find)

    p = sub.add_parser("state", parents=[output.COMMON], help="full state, attributes and registry entry")
    p.add_argument("entity_id")
    p.set_defaults(func=_state)

    p = sub.add_parser("history", parents=[output.COMMON], help="state changes; flags flapping")
    p.add_argument("entity_ids", nargs="+")
    p.add_argument("--since", default="2h")
    p.add_argument("--tail", type=int, default=200, help="print at most the last N changes")
    p.add_argument("--window", type=int, default=60, help="flap window, seconds")
    p.add_argument("--threshold", type=int, default=6, help="changes within the window that count as flapping")
    p.set_defaults(func=_history)

    p = sub.add_parser("stats", parents=[output.COMMON], help="daily min/max/mean from long-term statistics")
    p.add_argument("statistic_id")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=_stats)

    p = sub.add_parser("template", parents=[output.COMMON], help="render a Jinja template against live state")
    p.add_argument("template", nargs="?")
    p.add_argument("-f", "--file")
    p.set_defaults(func=_template)

    p = sub.add_parser("trace", parents=[output.COMMON], help="recent runs of an automation, step by step")
    p.add_argument("automation", help="automation.<x> or its config id")
    p.add_argument("--last", type=int, default=3)
    p.set_defaults(func=_trace)

    p = sub.add_parser("log", parents=[output.COMMON], help="HA's deduplicated error/warning log")
    p.add_argument("--errors", action="store_true", help="errors only")
    p.add_argument("--since", help="e.g. 2h")
    p.set_defaults(func=_log)
```

In `__main__.py`: `MODULES: list[str] = ["revision", "lint", "query"]`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass.

- [ ] **Step 5: Live checks** (all read-only):

```bash
nix develop --command bash -c '
hactl find lamp --domain light
hactl state switch.bedroom_bedroom_light_switch | head -5
hactl history switch.bedroom_bedroom_light_switch light.floor_lamp --since 1d --tail 5
hactl stats sensor.window_right_battery --days 7
hactl template "{{ states(\"sun.sun\") }}"
hactl trace automation.sync_bedroom_switch_and_lights --last 1
hactl log --errors | head -5'
```
Expected: lamp rows with areas; switch state + registry line; recent changes (a `FLAPPING` line if a burst happened in the last day); daily battery rows; `above_horizon` or `below_horizon`; a trace with steps; error log lines (smartrent, plant blinds).

- [ ] **Step 6: Commit**

```bash
git add tools/hactl
nix develop --command git commit -m "hactl: find/state/history/stats/template/trace/log" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Screenshots

**Files:**
- Create: `tools/hactl/src/hactl/shot.py`
- Modify: `tools/hactl/src/hactl/__main__.py` (`MODULES`)
- Test: `tools/hactl/tests/test_shot.py`

**Interfaces:**
- Consumes: `client.Client` (`.url`, `.bearer()`), `output.*`.
- Produces: `shot.VIEWPORTS` (`"phone"`, `"desktop"`), `THEME_VAR = "--ha-card-border-radius"`, `THEME_VALUE = "22px"`; `ShotSpec(path, viewport, scheme)` with `.filename`; `slug(path)`, `parse_viewports(text) -> list[str]`, `plan_shots(paths, viewports, schemes) -> list[ShotSpec]`, `default_out_dir() -> Path`, `auth_script(url, token) -> str`, `take(client, specs, out_dir) -> list[dict]` (keys `path, viewport, scheme, file, seconds, cards, error_cards, unavailable, theme_ok, console`), `report(results) -> tuple[list[str], bool]` (lines, bad). CLI: `hactl shot PATH… [--viewport phone,desktop] [--scheme dark|light …] [--out DIR]`.

- [ ] **Step 1: Write the failing tests** `tools/hactl/tests/test_shot.py`:

```python
import json

import pytest

from hactl import shot
from hactl.errors import HactlError


def test_slug_and_filename():
    assert shot.slug("/home-ops/0") == "home-ops-0"
    assert shot.ShotSpec("/home-ops/1", "phone", "dark").filename == "home-ops-1_phone_dark.png"


def test_parse_viewports():
    assert shot.parse_viewports("phone,desktop") == ["phone", "desktop"]
    with pytest.raises(HactlError, match="tablet"):
        shot.parse_viewports("phone,tablet")


def test_plan_shots_groups_by_viewport_then_path():
    specs = shot.plan_shots(["/a", "/b"], ["phone", "desktop"], ["dark"])
    assert [(s.viewport, s.path) for s in specs] == [("phone", "/a"), ("phone", "/b"), ("desktop", "/a"), ("desktop", "/b")]


def test_phone_viewport_is_pixel_sized():
    assert shot.VIEWPORTS["phone"]["width"] == 412
    assert shot.VIEWPORTS["desktop"]["width"] == 1440


def test_auth_script_sets_tokens_safely():
    js = shot.auth_script("https://ha.example", 'tok"en')
    assert "localStorage.setItem('hassTokens'" in js
    payload = json.loads(json.loads(js.split("localStorage.setItem('hassTokens', ")[1].split(");")[0]))
    assert payload["access_token"] == 'tok"en'
    assert payload["hassUrl"] == "https://ha.example"


def test_default_out_dir_uses_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert str(shot.default_out_dir()).startswith(str(tmp_path / "hactl" / "shots"))


def test_report_flags_error_cards_and_theme():
    ok = {"file": "a.png", "cards": 5, "seconds": 2.0, "error_cards": [], "unavailable": [], "theme_ok": True, "console": []}
    bad = dict(ok, file="b.png", error_cards=["Custom element doesn't exist: x"], theme_ok=False)
    lines, is_bad = shot.report([ok, bad])
    assert not shot.report([ok])[1]
    assert is_bad
    assert any("error card" in line for line in lines)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_shot.py`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `tools/hactl/src/hactl/shot.py`:**

```python
"""Headless screenshots of Home Assistant views, with a render report.

Auth: HA's frontend keeps OAuth tokens in localStorage["hassTokens"], not a
cookie. An init script plants the long-lived token there before the frontend
boots. Every run uses a fresh browser context, so the token never reaches a
persistent profile.
"""
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from hactl import output
from hactl.errors import HactlError

VIEWPORTS = {
    "phone": {"width": 412, "height": 915, "device_scale_factor": 2, "is_mobile": True, "has_touch": True},
    "desktop": {"width": 1440, "height": 900, "device_scale_factor": 1, "is_mobile": False, "has_touch": False},
}
THEME_VAR, THEME_VALUE = "--ha-card-border-radius", "22px"  # warm-minimal sets this in both modes

PROBE_JS = """() => {
  const all = [];
  const walk = (root) => { for (const el of root.querySelectorAll('*')) { all.push(el); if (el.shadowRoot) walk(el.shadowRoot); } };
  walk(document);
  const deepText = (node) => {
    let s = '';
    const visit = (n) => {
      if (n.nodeType === 3) s += n.textContent + ' ';
      if (n.shadowRoot) visit(n.shadowRoot);
      for (const c of n.childNodes) visit(c);
    };
    visit(node);
    return s.replace(/\\s+/g, ' ').trim();
  };
  const cards = all.filter(e => e.tagName === 'HA-CARD');
  const errors = all.filter(e => e.tagName === 'HUI-ERROR-CARD').map(e => deepText(e).slice(0, 200));
  const unavailable = [...new Set(cards.map(deepText)
    .filter(t => /\\b(unavailable|unknown)\\b/i.test(t)).map(t => t.slice(0, 120)))];
  const host = document.querySelector('home-assistant');
  const theme = [document.documentElement, host].filter(Boolean)
    .map(el => getComputedStyle(el).getPropertyValue('THEME_VAR').trim()).find(v => v) || '';
  const view = all.find(e => e.tagName === 'HUI-VIEW' || e.tagName === 'HUI-VIEW-CONTAINER');
  return {cards: cards.length, errors, unavailable, theme,
          height: Math.max(document.documentElement.scrollHeight, view ? view.scrollHeight : 0)};
}""".replace("THEME_VAR", THEME_VAR)


@dataclass(frozen=True)
class ShotSpec:
    path: str
    viewport: str
    scheme: str

    @property
    def filename(self) -> str:
        return f"{slug(self.path)}_{self.viewport}_{self.scheme}.png"


def slug(path: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-") or "root"


def parse_viewports(text: str) -> list:
    names = [v.strip() for v in text.split(",") if v.strip()]
    bad = [n for n in names if n not in VIEWPORTS]
    if bad or not names:
        raise HactlError(f"unknown viewport(s) {', '.join(bad) or '(none)'}; choose from {', '.join(VIEWPORTS)}")
    return names


def plan_shots(paths, viewports, schemes) -> list:
    return [ShotSpec(p, v, s) for s in schemes for v in viewports for p in paths]


def default_out_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "hactl" / "shots" / datetime.now().strftime("%Y%m%d-%H%M%S")


def auth_script(url: str, token: str) -> str:
    tokens = {"hassUrl": url, "clientId": url + "/", "access_token": token, "token_type": "Bearer",
              "refresh_token": "", "expires_in": 315360000, "expires": int(time.time() * 1000) + 315360000 * 1000}
    return (f"localStorage.setItem('hassTokens', {json.dumps(json.dumps(tokens))});"
            "localStorage.setItem('dockedSidebar', JSON.stringify('always_hidden'));")


def _settle(page, timeout_s=20.0) -> dict:
    """Wait until the ha-card count is stable for 1.5 s."""
    deadline = time.monotonic() + timeout_s
    last, since = None, time.monotonic()
    info = page.evaluate(PROBE_JS)
    while time.monotonic() < deadline:
        info = page.evaluate(PROBE_JS)
        if info["cards"] != last:
            last, since = info["cards"], time.monotonic()
        elif info["cards"] > 0 and time.monotonic() - since > 1.5:
            break
        page.wait_for_timeout(300)
    return info


def _shoot(ctx, base, spec, out_dir) -> dict:
    page = ctx.new_page()
    console = []
    page.on("console", lambda m: console.append(f"console: {m.text[:200]}") if m.type == "error" else None)
    page.on("pageerror", lambda e: console.append(f"pageerror: {str(e)[:200]}"))
    t0 = time.monotonic()
    page.goto(base + spec.path, wait_until="domcontentloaded", timeout=30000)
    info = _settle(page)
    if "/auth/authorize" in page.url:
        raise HactlError("HA showed its login page: the token was rejected (see docs/ha.md)")
    vp = VIEWPORTS[spec.viewport]
    if info["height"] > vp["height"]:  # HA scrolls inside its view; grow the viewport to hold it all
        page.set_viewport_size({"width": vp["width"], "height": min(info["height"] + 40, 8000)})
        page.wait_for_timeout(800)
    f = out_dir / spec.filename
    page.screenshot(path=str(f), full_page=True)
    page.close()
    return {"path": spec.path, "viewport": spec.viewport, "scheme": spec.scheme, "file": str(f),
            "seconds": round(time.monotonic() - t0, 1), "cards": info["cards"], "error_cards": info["errors"],
            "unavailable": info["unavailable"], "theme_ok": info["theme"] == THEME_VALUE, "console": console[:15]}


def take(client, specs, out_dir: Path) -> list:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    results, contexts = [], {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for spec in specs:
                key = (spec.viewport, spec.scheme)
                if key not in contexts:
                    vp = VIEWPORTS[spec.viewport]
                    ctx = browser.new_context(
                        viewport={"width": vp["width"], "height": vp["height"]},
                        device_scale_factor=vp["device_scale_factor"], is_mobile=vp["is_mobile"],
                        has_touch=vp["has_touch"], color_scheme=spec.scheme,
                    )
                    ctx.add_init_script(auth_script(client.url, client.bearer()))
                    contexts[key] = ctx
                results.append(_shoot(contexts[key], client.url, spec, out_dir))
        finally:
            browser.close()
    return results


def report(results) -> tuple:
    lines, bad = [], False
    for r in results:
        flags = []
        if r["error_cards"]:
            flags.append(f"{len(r['error_cards'])} error card(s)")
        if not r["theme_ok"]:
            flags.append("theme is NOT warm-minimal")
        if r["console"]:
            flags.append(f"{len(r['console'])} console error(s)")
        if r["unavailable"]:
            flags.append(f"{len(r['unavailable'])} card(s) show unavailable/unknown")
        bad |= bool(r["error_cards"]) or not r["theme_ok"]
        lines.append(f"{r['file']}  {r['cards']} cards  {r['seconds']}s  {'; '.join(flags) or 'clean'}")
        lines += [f"    error card: {x}" for x in r["error_cards"]]
        lines += [f"    unavailable: {x}" for x in r["unavailable"]]
        lines += [f"    {x}" for x in r["console"]]
    return lines, bad


def _run(args) -> int:
    from hactl.client import Client

    paths = [p if p.startswith("/") else "/" + p for p in args.paths]
    specs = plan_shots(paths, parse_viewports(args.viewport), args.scheme or ["dark"])
    results = take(Client(), specs, Path(args.out) if args.out else default_out_dir())
    lines, bad = report(results)
    output.emit(args, results, lines)
    return 1 if bad else 0


def register(sub) -> None:
    p = sub.add_parser("shot", parents=[output.COMMON], help="screenshot HA views at phone/desktop size")
    p.add_argument("paths", nargs="+", help="e.g. /home-ops/0")
    p.add_argument("--viewport", default="phone,desktop", help="comma list of: " + ", ".join(VIEWPORTS))
    p.add_argument("--scheme", action="append", choices=["dark", "light"], help="repeatable; default dark")
    p.add_argument("--out", help="output dir (default $XDG_CACHE_HOME/hactl/shots/<time>)")
    p.set_defaults(func=_run)
```

In `__main__.py`: `MODULES: list[str] = ["revision", "lint", "query", "shot"]`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass.

- [ ] **Step 5: Live check**

Run: `nix develop --command hactl shot /home-ops/0 /home-ops/1`
Expected: 4 lines (2 views × phone, desktop), each with a card count, exit 0, `theme_ok` implied by no "NOT warm-minimal" flag. Then **Read two of the PNGs** with the Read tool and confirm they show the warm-minimal Home and Living Room views (dark espresso background, amber accents). If the report says "theme is NOT warm-minimal" while the PNG visibly is warm-minimal, the theme variable lives on a different element: find it with a one-off `page.evaluate` on `document.querySelector('home-assistant').shadowRoot` children, add that element to the `[document.documentElement, host]` list in `PROBE_JS`, rerun.

- [ ] **Step 6: Commit**

```bash
git add tools/hactl
nix develop --command git commit -m "hactl: screenshots with render report (error cards, theme, unavailable)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Preview a dashboard without committing

**Files:**
- Create: `tools/hactl/src/hactl/preview.py`
- Modify: `tools/hactl/src/hactl/__main__.py` (`MODULES`)
- Test: `tools/hactl/tests/test_preview.py`

**Interfaces:**
- Consumes: `yamlload.load`, `yamlload.Tagged`, `shot.plan_shots`, `shot.parse_viewports`, `shot.take`, `shot.report`, `shot.default_out_dir`, `client.Client.ws`.
- Produces: `preview.PREVIEW_URL = "claude-preview"`; `resolve(path) -> dict` (JSON-safe dashboard config); `ensure_dashboard(client) -> bool` (True if it created it); `push(client, config)`. CLI: `hactl preview FILE [--view N …] [--viewport …] [--scheme …] [--out DIR]`.
- Note: plan 2 declares `claude-preview` in `state/dashboards.yaml`; until then `ensure_dashboard` bootstraps it (idempotent), and plan 2's `import` records it.

- [ ] **Step 1: Write the failing tests** `tools/hactl/tests/test_preview.py`:

```python
import pytest

from hactl import preview
from hactl.errors import HactlError


def test_resolve_includes(tmp_path):
    (tmp_path / "overview.yaml").write_text("views:\n  - !include overview_home.yaml\n")
    (tmp_path / "overview_home.yaml").write_text("title: Home\ncards: []\n")
    assert preview.resolve(tmp_path / "overview.yaml") == {"views": [{"title": "Home", "cards": []}]}


def test_secret_rejected(tmp_path):
    (tmp_path / "d.yaml").write_text("views:\n  - title: !secret x\n")
    with pytest.raises(HactlError, match="!secret"):
        preview.resolve(tmp_path / "d.yaml")


def test_not_a_dashboard(tmp_path):
    (tmp_path / "d.yaml").write_text("automation: []\n")
    with pytest.raises(HactlError, match="views"):
        preview.resolve(tmp_path / "d.yaml")


def test_unsupported_tag(tmp_path):
    (tmp_path / "d.yaml").write_text("views:\n  - title: !env_var HOME\n")
    with pytest.raises(HactlError, match="!env_var"):
        preview.resolve(tmp_path / "d.yaml")


def test_unquoted_date_explained(tmp_path):
    (tmp_path / "d.yaml").write_text("views:\n  - title: 2026-10-03\n")
    with pytest.raises(HactlError, match="quote"):
        preview.resolve(tmp_path / "d.yaml")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_preview.py`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `tools/hactl/src/hactl/preview.py`:**

```python
"""Preview a dashboard file on a scratch storage dashboard, then screenshot it.

Nothing in git, the ConfigMaps or home-ops changes. `!include`s are resolved
locally because a storage dashboard is one JSON document.
"""
import json
from pathlib import Path

from hactl import output, shot, yamlload
from hactl.errors import HactlError

PREVIEW_URL = "claude-preview"


def _tags(obj) -> set:
    if isinstance(obj, yamlload.Tagged):
        return {obj.tag}
    if isinstance(obj, dict):
        return set().union(*(_tags(v) for v in obj.values())) if obj else set()
    if isinstance(obj, list):
        return set().union(*(_tags(v) for v in obj)) if obj else set()
    return set()


def resolve(path) -> dict:
    path = Path(path)
    cfg = yamlload.load(path, resolve_includes=True, allow_secret=False)
    if not isinstance(cfg, dict) or not isinstance(cfg.get("views"), list):
        raise HactlError(f"{path}: not a dashboard (needs a top-level `views:` list)")
    leftover = _tags(cfg)
    if leftover:
        raise HactlError(f"{path}: unsupported tag(s) in a dashboard: {', '.join(sorted(leftover))}")
    try:
        return json.loads(json.dumps(cfg))
    except TypeError as e:
        raise HactlError(f"{path}: a value can't be sent to HA ({e}); quote it") from None


def ensure_dashboard(client) -> bool:
    existing = client.ws({"type": "lovelace/dashboards/list"})[0]
    if any(d["url_path"] == PREVIEW_URL for d in existing):
        return False
    client.ws({"type": "lovelace/dashboards/create", "url_path": PREVIEW_URL, "title": "Claude Preview",
               "icon": "mdi:flask-outline", "mode": "storage", "require_admin": True, "show_in_sidebar": False})
    return True


def push(client, config: dict) -> None:
    client.ws({"type": "lovelace/config/save", "url_path": PREVIEW_URL, "config": config})


def _run(args) -> int:
    from hactl.client import Client

    cfg = resolve(args.file)
    client = Client()
    created = ensure_dashboard(client)
    push(client, cfg)
    views = args.view if args.view else list(range(len(cfg["views"])))
    for v in views:
        if not 0 <= v < len(cfg["views"]):
            raise HactlError(f"--view {v}: the dashboard has views 0..{len(cfg['views']) - 1}")
    specs = shot.plan_shots([f"/{PREVIEW_URL}/{v}" for v in views], shot.parse_viewports(args.viewport), args.scheme or ["dark"])
    results = shot.take(client, specs, Path(args.out) if args.out else shot.default_out_dir())
    lines, bad = shot.report(results)
    head = f"pushed {args.file} to /{PREVIEW_URL}" + (" (created the preview dashboard)" if created else "")
    output.emit(args, {"pushed": str(args.file), "created": created, "shots": results}, [head] + lines)
    return 1 if bad else 0


def register(sub) -> None:
    p = sub.add_parser("preview", parents=[output.COMMON], help="push a dashboard file to /claude-preview and screenshot it")
    p.add_argument("file")
    p.add_argument("--view", type=int, action="append", help="view index to shoot (repeatable; default all)")
    p.add_argument("--viewport", default="phone,desktop")
    p.add_argument("--scheme", action="append", choices=["dark", "light"])
    p.add_argument("--out")
    p.set_defaults(func=_run)
```

In `__main__.py`: `MODULES: list[str] = ["revision", "lint", "query", "shot", "preview"]`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass.

- [ ] **Step 5: Live check** (writes only the scratch `claude-preview` dashboard)

Run: `nix develop --command hactl preview cluster/home-assistant/dashboards/overview.yaml --view 0 --viewport phone`
Expected: `pushed … to /claude-preview (created the preview dashboard)` and one clean shot. Read the PNG and compare with Task 7's `home-ops-0_phone_dark.png`: same Home view. Run it a second time: no "(created …)".

- [ ] **Step 6: Commit**

```bash
git add tools/hactl
nix develop --command git commit -m "hactl: preview dashboards on /claude-preview without committing" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Live lint and health

**Files:**
- Create: `tools/hactl/src/hactl/health.py`
- Modify: `tools/hactl/src/hactl/lint.py` (add live checks; replace `_run`)
- Modify: `tools/hactl/src/hactl/__main__.py` (`MODULES`)
- Test: `tools/hactl/tests/test_health.py`, extend `tools/hactl/tests/test_lint.py`

**Interfaces:**
- Consumes: `yamlload.load`, `yamlload.load_dir`, `refs.extract_refs`, `refs.declared_entities`, `revision.compute`, `query.format_log`, `client.Client`.
- Produces: `lint.load_allow_missing(path) -> set`, `lint.check_entity_refs(ha_dir, known: set, allow: set = frozenset()) -> list[Finding]`, `lint.check_dashboard_templates(ha_dir, render) -> list[Finding]` (`render(text) -> None`, raises `HactlError` on a broken template), `lint.template_strings(obj) -> list[str]`; `health.ERROR_EXECUTIONS`, `failing_automations(trace_list) -> list[dict]`, `declared_domains(packages) -> set`, `drift(repo_rev, loaded_rev, declared, components) -> list[str]`, `hook_status() -> str | None`, `collect(client, ha_dir) -> dict` (keys `drift, hook, failing, repairs, log_errors, unavailable`), `render(report) -> tuple[list[str], bool]` (lines, problem). CLI: `hactl health`; `hactl lint` now also runs live checks unless `--offline`.

- [ ] **Step 1: Write the failing tests.**

`tools/hactl/tests/test_health.py`:

```python
from hactl import health


def t(item, run, execution, start):
    return {"item_id": item, "run_id": run, "script_execution": execution, "timestamp": {"start": start}}


def test_latest_error_run_is_failing():
    traces = [t("blinds", "1", "finished", "2026-10-03T10:00"), t("blinds", "2", "error", "2026-10-03T11:00")]
    assert [f["item_id"] for f in health.failing_automations(traces)] == ["blinds"]


def test_recovered_automation_is_not_failing():
    traces = [t("blinds", "1", "error", "2026-10-03T10:00"), t("blinds", "2", "finished", "2026-10-03T11:00")]
    assert health.failing_automations(traces) == []


def test_cancelled_runs_are_not_failures():
    traces = [t("bedroom_sync", "1", "cancelled", "2026-10-03T10:00"), t("night", "2", "failed_conditions", "2026-10-03T10:00")]
    assert health.failing_automations(traces) == []


def test_declared_domains():
    assert health.declared_domains({"a": {"automation": [], "prometheus": {}}, "b": {"template": []}, "c": None}) == {
        "automation", "prometheus", "template"}


def test_drift_clean():
    assert health.drift("abc", "abc", {"automation"}, {"automation", "light"}) == []


def test_drift_reports_each_problem():
    msgs = health.drift("abc", "old", {"automation", "prometheus"}, {"automation"})
    assert any("old" in m and "abc" in m for m in msgs)
    assert any("prometheus" in m for m in msgs)
    assert any("never loaded" in m for m in health.drift("abc", None, set(), set()))


def test_render_problem_flags():
    base = {"drift": [], "hook": "succeeded", "failing": [], "repairs": [], "log_errors": [], "unavailable": ["binary_sensor.fridge_door_contact"]}
    lines, problem = health.render(base)
    assert not problem  # unavailable entities are warnings, not problems
    assert any("fridge_door_contact" in line for line in lines)
    assert health.render(dict(base, hook="failed"))[1]
    assert health.render(dict(base, drift=["x"]))[1]
```

Append to `tools/hactl/tests/test_lint.py`:

```python
def test_entity_refs_unknown_vs_declared(tmp_path):
    ha = make_ha(tmp_path, {
        "packages/a.yaml": (
            "input_boolean:\n  guest_mode: {}\n"
            "automation:\n  - alias: X\n    triggers:\n      - trigger: state\n"
            "        entity_id: input_boolean.guest_mode\n"
            "    actions:\n      - action: light.turn_on\n        target:\n          entity_id: light.missing_lamp\n"
        ),
        "dashboards/d.yaml": "views:\n  - cards:\n      - entity: sun.sun\n",
    })
    found = lint.check_entity_refs(ha, known={"sun.sun"})
    assert [(f.rule, f.message.split()[0], f.line) for f in found] == [("entity-ref", "light.missing_lamp", 11)]
    assert lint.check_entity_refs(ha, known={"sun.sun"}, allow={"light.missing_lamp"}) == []


def test_allow_missing_file(tmp_path):
    f = tmp_path / "lint-allow-missing.txt"
    f.write_text("# integrations still to add\nweather.toronto  # plan 3: met.no Toronto\n\nsensor.toronto_work_commute\n")
    assert lint.load_allow_missing(f) == {"weather.toronto", "sensor.toronto_work_commute"}
    assert lint.load_allow_missing(tmp_path / "absent.txt") == set()


def test_template_strings_and_render_errors(tmp_path):
    ha = make_ha(tmp_path, {"dashboards/d.yaml": (
        "views:\n  - cards:\n"
        "      - primary: \"{{ states('sun.sun') }}\"\n"
        "      - primary: \"{{ as_timestamp(None) }}\"\n"
        "      - primary: plain text\n"
    )})

    def render(text):
        if "None" in text:
            raise lint.HactlError("Template error: as_timestamp got invalid input")

    found = lint.check_dashboard_templates(ha, render)
    assert [(f.rule, f.line) for f in found] == [("dashboard-template", 4)]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_health.py tools/hactl/tests/test_lint.py`
Expected: FAIL — `ImportError` (health) and `AttributeError` (lint).

- [ ] **Step 3: Add live checks to `tools/hactl/src/hactl/lint.py`.** Add `from hactl import refs, yamlload` to the imports, then add these functions above `_run`:

```python
def _line_of(path: Path, needle: str):
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if needle in line:
            return n
    return None


ALLOW_MISSING = paths.HA_DIR / "lint-allow-missing.txt"


def load_allow_missing(path: Path = ALLOW_MISSING) -> set:
    """Entity ids that are referenced on purpose before they exist (one per line, # comments)."""
    if not path.exists():
        return set()
    return {line.split("#")[0].strip() for line in path.read_text().splitlines()} - {""}


def check_entity_refs(ha_dir: Path, known: set, allow: set = frozenset()) -> list:
    declared = refs.declared_entities(yamlload.load_dir(ha_dir / "packages"))
    out = []
    for sub in ("packages", "dashboards"):
        for f in sorted((ha_dir / sub).glob("*.yaml")):
            for ref in sorted(refs.extract_refs(yamlload.load(f))):
                if ref not in known and ref not in declared and ref not in allow:
                    out.append(Finding("entity-ref", paths.rel(f), _line_of(f, ref), f"{ref} does not exist in HA"))
    return out


def template_strings(obj) -> list:
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in template_strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in template_strings(v)]
    if isinstance(obj, str) and ("{{" in obj or "{%" in obj):
        return [obj]
    return []


def check_dashboard_templates(ha_dir: Path, render) -> list:
    out = []
    for f in sorted((ha_dir / "dashboards").glob("*.yaml")):
        for text in template_strings(yamlload.load(f)):
            try:
                render(text)
            except HactlError as e:
                first = text.strip().splitlines()[0][:60]
                out.append(Finding("dashboard-template", paths.rel(f), _line_of(f, first), str(e)[:300]))
    return out


def live(ha_dir: Path, client) -> list:
    states = client.get("/api/states")
    entities = client.ws({"type": "config/entity_registry/list"})[0]
    known = {s["entity_id"] for s in states} | {e["entity_id"] for e in entities}
    # Card templates may use the card's own variables; give them harmless values.
    variables = {"entity": "", "user": "", "config": {}}

    def render(text):
        client.post("/api/template", {"template": text, "variables": variables}, raw=True)

    allow = load_allow_missing(ha_dir / "lint-allow-missing.txt")
    return check_entity_refs(ha_dir, known, allow) + check_dashboard_templates(ha_dir, render)
```

Replace `_run` with:

```python
def _run(args) -> int:
    findings = offline(paths.HA_DIR, run_check_config=not args.no_check_config)
    if not args.offline:
        from hactl.client import Client

        findings += live(paths.HA_DIR, Client())
    output.emit(args, [asdict(f) for f in findings], [str(f) for f in findings] or ["lint: clean"])
    return 1 if any(f.severity == "error" for f in findings) else 0
```

- [ ] **Step 4: Implement `tools/hactl/src/hactl/health.py`:**

```python
"""One-shot health: drift, the reload hook, failing automations, repairs, log, unavailable refs."""
import json
import subprocess

from hactl import output, paths, query, refs, revision, yamlload

ERROR_EXECUTIONS = frozenset({"error", "unhandled_error", "aborted"})


def failing_automations(trace_list) -> list:
    """Automations whose most recent stored run errored. Cancelled/condition-failed runs are normal."""
    latest = {}
    for t in trace_list:
        start = (t.get("timestamp") or {}).get("start", "")
        if t["item_id"] not in latest or start > (latest[t["item_id"]].get("timestamp") or {}).get("start", ""):
            latest[t["item_id"]] = t
    return [
        {"item_id": k, "run_id": t["run_id"], "execution": t.get("script_execution"), "start": (t.get("timestamp") or {}).get("start")}
        for k, t in sorted(latest.items())
        if t.get("script_execution") in ERROR_EXECUTIONS
    ]


def declared_domains(packages: dict) -> set:
    return {k for p in packages.values() if isinstance(p, dict) for k in p}


def drift(repo_rev, loaded_rev, declared, components) -> list:
    out = []
    if loaded_rev is None:
        out.append("sensor.ha_config_revision missing: the revision package was never loaded")
    elif loaded_rev != repo_rev:
        out.append(f"HA runs config {loaded_rev}, the repo is {repo_rev}: not deployed yet, or not reloaded")
    missing = sorted(d for d in declared if d not in components)
    if missing:
        out.append(f"declared in packages but not loaded: {', '.join(missing)} (needs a restart, or failed to set up)")
    return out


def hook_status():
    """ha-reload Job outcome via kubectl (read-only). None when unknown."""
    try:
        r = subprocess.run(["kubectl", "-n", "home-assistant", "get", "job", "ha-reload", "-o", "json"],
                           capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    st = json.loads(r.stdout).get("status", {})
    return "failed" if st.get("failed") else "succeeded" if st.get("succeeded") else "running"


def collect(client, ha_dir=paths.HA_DIR) -> dict:
    states = {s["entity_id"]: s["state"] for s in client.get("/api/states")}
    components = set(client.get("/api/config")["components"])
    traces, log, repairs = client.ws(
        {"type": "trace/list", "domain": "automation"}, {"type": "system_log/list"}, {"type": "repairs/list_issues"})
    used = set()
    for sub in ("packages", "dashboards"):
        for f in sorted((ha_dir / sub).glob("*.yaml")):
            used |= refs.extract_refs(yamlload.load(f))
    return {
        "drift": drift(revision.compute(ha_dir), states.get("sensor.ha_config_revision"),
                       declared_domains(yamlload.load_dir(ha_dir / "packages")), components),
        "hook": hook_status(),
        "failing": failing_automations(traces),
        "repairs": [{"domain": i["domain"], "issue_id": i["issue_id"], "severity": i.get("severity")}
                    for i in repairs["issues"] if not i.get("ignored")],
        "log_errors": sorted(query.format_log(log, errors_only=True), key=lambda e: -e["count"])[:10],
        "unavailable": sorted(r for r in used if states.get(r) in ("unavailable", "unknown")),
    }


def render(r: dict) -> tuple:
    problem = bool(r["drift"] or r["failing"] or r["hook"] == "failed")
    lines = [f"health: {'PROBLEMS' if problem else 'ok'}"]

    def section(title, items, fmt=str):
        lines.append(f"{title}: none" if not items else f"{title}:")
        lines.extend(f"  - {fmt(i)}" for i in items)

    section("drift", r["drift"])
    lines.append(f"ha-reload hook: {r['hook'] or 'unknown (no KUBECONFIG, or no run yet)'}")
    section("failing automations", r["failing"], lambda f: f"{f['item_id']} ({f['execution']} at {f['start']}): hactl trace {f['item_id']}")
    section("repairs", r["repairs"], lambda i: f"{i['domain']}/{i['issue_id']} [{i['severity']}]")
    section("log errors, top 10 by count", r["log_errors"], lambda e: f"{e['count']}x {e['name']}: {e['message'][:140]}")
    section("referenced entities that are unavailable (warning)", r["unavailable"])
    return lines, problem


def _run(args) -> int:
    from hactl.client import Client

    report = collect(Client())
    lines, problem = render(report)
    output.emit(args, report, lines)
    return 1 if problem else 0


def register(sub) -> None:
    p = sub.add_parser("health", parents=[output.COMMON], help="drift, failing automations, repairs, log errors")
    p.set_defaults(func=_run)
```

In `__main__.py`: `MODULES: list[str] = ["revision", "lint", "query", "shot", "preview", "health"]`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass.

- [ ] **Step 6: Live checks**

Run: `export KUBECONFIG=/tmp/galaxy-kubeconfig; nix develop --command bash -c 'hactl lint --no-check-config; hactl health'`
Expected: `lint` prints any unknown entity refs. Handle each one: if it is a dead or misspelled id and `hactl find` shows the live replacement, fix the package/dashboard; if it names an entity that legitimately does not exist yet (an integration still to be added, e.g. `weather.toronto` and `sensor.toronto_work_commute`, which `packages/overview_brain.yaml` documents as "ADD"), list it in `cluster/home-assistant/lint-allow-missing.txt` with a `# why / which plan adds it` comment. Re-run until `lint: clean`. Report the list of fixes and allow-listed ids to Chris in the task summary. `health` exits 1 with `drift:` listing **`sensor.ha_config_revision missing`** and **`declared in packages but not loaded: prometheus`** — the known not-yet-deployed state, which Task 13 fixes. Log errors list smartrent/plant-blinds entries.

- [ ] **Step 7: Commit**

```bash
git add tools/hactl cluster/home-assistant
nix develop --command git commit -m "hactl: health (drift, failing automations, repairs) and live lint" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: `call` under the actuation policy

**Files:**
- Create: `tools/hactl/src/hactl/act.py`
- Modify: `tools/hactl/src/hactl/__main__.py` (`MODULES`)
- Test: `tools/hactl/tests/test_act.py`

**Interfaces:**
- Consumes: `errors.HactlError`, `errors.PolicyError`, `client.Client.post`.
- Produces: `act.CONFIRM_PREFIXES`, `act.CONFIRM_EXACT`, `check_actuation(service: str, confirmed: bool) -> None` (raises `PolicyError`); CLI `hactl call DOMAIN.SERVICE [--entity ID …] [--data JSON] [--confirmed]`.

- [ ] **Step 1: Write the failing tests** `tools/hactl/tests/test_act.py`:

```python
import pytest

from hactl import act
from hactl.errors import HactlError, PolicyError


@pytest.mark.parametrize("service", ["light.turn_on", "fan.turn_off", "cover.set_cover_position", "climate.set_hvac_mode",
                                     "scene.turn_on", "switch.toggle", "homeassistant.reload_all"])
def test_reversible_is_free(service):
    act.check_actuation(service, confirmed=False)


@pytest.mark.parametrize("service", ["lock.unlock", "lock.lock", "notify.mobile_app_pixel_9_pro_xl", "tts.speak",
                                     "assist_satellite.announce", "homeassistant.restart", "homeassistant.stop"])
def test_needs_chris(service):
    with pytest.raises(PolicyError, match="Chris"):
        act.check_actuation(service, confirmed=False)
    act.check_actuation(service, confirmed=True)


def test_malformed_service():
    with pytest.raises(HactlError, match="domain.service"):
        act.check_actuation("turn_on", confirmed=False)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_act.py`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `tools/hactl/src/hactl/act.py`:**

```python
"""Call HA actions under Chris's actuation policy (2026-10-03).

Anything reversible is free while testing. Things that reach a person or
can't be undone (locks, phone notifications, speech, restarts) need his OK
first: re-run with --confirmed once he has said yes.
"""
import json

from hactl import output
from hactl.errors import HactlError, PolicyError

CONFIRM_PREFIXES = ("lock.", "notify.", "tts.", "assist_satellite.")
CONFIRM_EXACT = frozenset({"homeassistant.restart", "homeassistant.stop"})


def check_actuation(service: str, confirmed: bool) -> None:
    if service.count(".") != 1:
        raise HactlError(f"{service!r}: expected domain.service, e.g. light.turn_on")
    if (service.startswith(CONFIRM_PREFIXES) or service in CONFIRM_EXACT) and not confirmed:
        raise PolicyError(f"{service} needs Chris's OK first (actuation policy); re-run with --confirmed once he says yes")


def _run(args) -> int:
    from hactl.client import Client

    check_actuation(args.service, args.confirmed)
    try:
        data = json.loads(args.data) if args.data else {}
    except ValueError as e:
        raise HactlError(f"--data is not JSON: {e}") from None
    if args.entity:
        data["entity_id"] = args.entity
    domain, service = args.service.split(".")
    changed = Client().post(f"/api/services/{domain}/{service}", data)
    ids = [s["entity_id"] for s in changed] if isinstance(changed, list) else []
    output.emit(args, changed, f"{args.service}: {len(ids)} state(s) changed" + (f": {', '.join(ids)}" if ids else ""))
    return 0


def register(sub) -> None:
    p = sub.add_parser("call", parents=[output.COMMON], help="call an HA action (locks/notify need --confirmed)")
    p.add_argument("service", help="domain.service, e.g. light.turn_on")
    p.add_argument("--entity", action="append", help="entity_id (repeatable)")
    p.add_argument("--data", help="JSON object of action data")
    p.add_argument("--confirmed", action="store_true", help="Chris has OK'd this specific action")
    p.set_defaults(func=_run)
```

In `__main__.py`: `MODULES: list[str] = ["revision", "lint", "query", "shot", "preview", "health", "act"]`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass.

- [ ] **Step 5: Live check** (reversible, allowed by the policy): toggle the Desk Underlight and restore it.

```bash
nix develop --command bash -c '
before=$(hactl state light.desk_underlight --json | python3 -c "import json,sys; print(json.load(sys.stdin)[\"state\"][\"state\"])")
hactl call light.toggle --entity light.desk_underlight
sleep 2; hactl state light.desk_underlight | head -1
hactl call light.toggle --entity light.desk_underlight
sleep 2; echo "before=$before"; hactl state light.desk_underlight | head -1
hactl call lock.unlock --entity lock.front_door_lock; echo "exit=$?"'
```
Expected: the state flips then returns to `before`; the lock call prints `hactl: lock.unlock needs Chris's OK first …` and `exit=3` (nothing is sent to the lock).

- [ ] **Step 6: Commit**

```bash
git add tools/hactl
nix develop --command git commit -m "hactl: call, gated by the actuation policy" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: The `ha-reload` PostSync hook

**Files:**
- Create: `cluster/home-assistant/hooks/ha_reload.py`
- Create: `cluster/home-assistant/ha-reload.yaml`
- Create: `cluster/home-assistant/ha-reload-token.secret.yaml` (git-crypt; raw Secret)
- Create (generated by `sign.sh`): `cluster/home-assistant/ha-reload-token.yaml` (SealedSecret)
- Modify: `cluster/home-assistant/kustomization.yaml`
- Test: `tools/hactl/tests/test_ha_reload.py`

**Interfaces:**
- Consumes: `packages/config_revision.yaml` format from Task 3 (`state: "<12 hex>"`).
- Produces: `ha_reload.expected_revision(packages: Path) -> str`, `declared_domains(packages: Path) -> set`, `make_ha(base, token) -> callable(method, path, body=None, timeout=60)`, `poll(attempt, timeout, interval, clock=time.monotonic, sleep=time.sleep) -> bool`, `reload_until_revision(ha, expected, timeout=360, interval=20, clock=…, sleep=…) -> bool`, `missing_integrations(ha, declared) -> set`, `restart_home_assistant()`, `main() -> int`. Kubernetes: ServiceAccount/Role/RoleBinding/Job `ha-reload`, ConfigMap `ha-reload-script` (key `ha_reload.py`), Secret `ha-reload-token` (key `token`).

- [ ] **Step 1: Write the failing tests** `tools/hactl/tests/test_ha_reload.py`:

```python
import urllib.error

import ha_reload


class FakeHA:
    def __init__(self, revisions, components=("homeassistant", "automation", "template")):
        self.revisions = list(revisions)
        self.components = list(components)
        self.calls = []

    def __call__(self, method, path, body=None, timeout=60):
        self.calls.append((method, path))
        if path == "/api/services/homeassistant/reload_all":
            return []
        if path == "/api/states/sensor.ha_config_revision":
            r = self.revisions.pop(0) if len(self.revisions) > 1 else self.revisions[0]
            if isinstance(r, Exception):
                raise r
            return {"state": r}
        if path == "/api/config":
            return {"components": self.components}
        raise AssertionError(path)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_expected_revision(tmp_path):
    (tmp_path / "config_revision.yaml").write_text('template:\n  - sensor:\n      - name: X\n        state: "0123456789ab"\n')
    assert ha_reload.expected_revision(tmp_path) == "0123456789ab"


def test_declared_domains_reads_top_level_keys_only(tmp_path):
    (tmp_path / "a.yaml").write_text("# comment: no\nautomation:\n  - id: a\n    nested: no\ntemplate: []\n")
    (tmp_path / "b.yaml").write_text("prometheus:\n  namespace: homeassistant\n")
    assert ha_reload.declared_domains(tmp_path) == {"automation", "template", "prometheus"}


def test_reload_until_match():
    ha, c = FakeHA(["old", "old", "abc"]), Clock()
    assert ha_reload.reload_until_revision(ha, "abc", timeout=360, interval=20, clock=c, sleep=c.sleep)
    assert c.t == 40
    assert ha.calls.count(("POST", "/api/services/homeassistant/reload_all")) == 3


def test_reload_survives_ha_restarting():
    ha, c = FakeHA([urllib.error.URLError("connection refused"), "abc"]), Clock()
    assert ha_reload.reload_until_revision(ha, "abc", clock=c, sleep=c.sleep)


def test_reload_times_out():
    ha, c = FakeHA(["old"]), Clock()
    assert not ha_reload.reload_until_revision(ha, "abc", timeout=60, interval=20, clock=c, sleep=c.sleep)


def test_missing_integrations_ignores_platform_entries():
    ha = FakeHA(["x"], components=["automation", "template", "template.sensor"])
    assert ha_reload.missing_integrations(ha, {"automation", "template", "prometheus"}) == {"prometheus"}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_ha_reload.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'ha_reload'`.

- [ ] **Step 3: Implement `cluster/home-assistant/hooks/ha_reload.py`:**

```python
#!/usr/bin/env python3
"""Argo PostSync hook: make the running Home Assistant match git.

1. Read the revision git just shipped (packages/config_revision.yaml).
2. Call homeassistant.reload_all until sensor.ha_config_revision shows it.
   Kubelet takes up to ~2 min to update the pod's ConfigMap volume.
3. Every top-level key in the packages is an integration. Any that HA has not
   loaded (e.g. a new `prometheus:`) needs a restart: patch the Deployment's
   restartedAt annotation once, then wait until everything declared is loaded.
Exits non-zero on failure, so Argo marks the hook failed.

Stdlib only (runs in plain python:alpine). Tested by
tools/hactl/tests/test_ha_reload.py. Runbook: docs/ha.md.
"""
import datetime
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REV_RE = re.compile(r'^\s*state:\s*"([0-9a-f]{12})"', re.M)
TOPKEY_RE = re.compile(r"^([a-z_][a-z0-9_]*):", re.M)
SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def log(msg):
    print(f"{datetime.datetime.now(datetime.timezone.utc):%H:%M:%S} {msg}", flush=True)


def expected_revision(packages: Path) -> str:
    m = REV_RE.search((packages / "config_revision.yaml").read_text())
    if not m:
        raise SystemExit("config_revision.yaml has no revision; was the pre-commit hook skipped?")
    return m.group(1)


def declared_domains(packages: Path) -> set:
    out = set()
    for f in sorted(packages.glob("*.yaml")):
        out |= set(TOPKEY_RE.findall(f.read_text()))
    return out


def make_ha(base: str, token: str):
    def call(method, path, body=None, timeout=60):
        req = urllib.request.Request(
            base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
        return json.loads(raw) if raw else None
    return call


def poll(attempt, timeout, interval, clock=time.monotonic, sleep=time.sleep) -> bool:
    """Call attempt() until it returns True or the timeout passes. HTTP/socket errors count as 'not yet'."""
    deadline = clock() + timeout
    while True:
        try:
            if attempt():
                return True
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            log(f"  not yet: {e}")
        if clock() >= deadline:
            return False
        sleep(interval)


def reload_until_revision(ha, expected, timeout=360, interval=20, clock=time.monotonic, sleep=time.sleep) -> bool:
    def attempt():
        ha("POST", "/api/services/homeassistant/reload_all", {})
        state = (ha("GET", "/api/states/sensor.ha_config_revision") or {}).get("state")
        log(f"  loaded revision: {state}")
        return state == expected
    return poll(attempt, timeout, interval, clock=clock, sleep=sleep)


def missing_integrations(ha, declared) -> set:
    components = set(ha("GET", "/api/config")["components"])
    return {d for d in declared if d not in components}


def restart_home_assistant(namespace="home-assistant", name="ha-home-assistant"):
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
    req = urllib.request.Request(
        f"https://{host}:{port}/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
        data=json.dumps(body).encode(), method="PATCH",
        headers={"Authorization": f"Bearer {(SA / 'token').read_text().strip()}",
                 "Content-Type": "application/strategic-merge-patch+json"},
    )
    urllib.request.urlopen(req, context=ssl.create_default_context(cafile=str(SA / "ca.crt")), timeout=30).read()


def main() -> int:
    packages = Path(os.environ.get("PACKAGES_DIR", "/packages"))
    ha = make_ha(os.environ.get("HA_URL", "http://ha-home-assistant.home-assistant.svc:8123"), os.environ["HA_TOKEN"].strip())
    expected = expected_revision(packages)
    declared = declared_domains(packages)
    log(f"expected revision {expected}; packages declare {sorted(declared)}")
    if not reload_until_revision(ha, expected):
        log("FAIL: HA never showed the expected revision (ConfigMap not propagated, or reload failing)")
        return 1
    missing = missing_integrations(ha, declared)
    if not missing:
        log("ok: revision loaded; every declared integration is loaded")
        return 0
    log(f"not loaded: {sorted(missing)} -> restarting Home Assistant once")
    restart_home_assistant()
    time.sleep(30)  # strategy Recreate: let the old pod go before polling
    if not poll(lambda: not missing_integrations(ha, declared), timeout=900, interval=15):
        log(f"FAIL: after the restart, still not loaded: {sorted(missing_integrations(ha, declared))} (check the HA log)")
        return 1
    log("ok: restarted once; every declared integration is loaded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass.

- [ ] **Step 5: Create `cluster/home-assistant/ha-reload.yaml`:**

```yaml
---
# ha-reload: Argo PostSync hook that makes the running Home Assistant match git
# after every sync of this app. It calls reload_all until
# sensor.ha_config_revision equals packages/config_revision.yaml, and restarts
# HA once if an integration declared in the packages isn't loaded.
# Script: hooks/ha_reload.py. Runbook: docs/ha.md.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ha-reload
  namespace: home-assistant
---
# Its only power: restart the one Deployment (patch its restartedAt annotation).
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: ha-reload
  namespace: home-assistant
rules:
  - apiGroups: [apps]
    resources: [deployments]
    resourceNames: [ha-home-assistant]
    verbs: [get, patch]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: ha-reload
  namespace: home-assistant
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: ha-reload
subjects:
  - kind: ServiceAccount
    name: ha-reload
    namespace: home-assistant
---
apiVersion: batch/v1
kind: Job
metadata:
  name: ha-reload
  namespace: home-assistant
  annotations:
    argocd.argoproj.io/hook: PostSync
    # Keep the last run (and its logs) until the next sync replaces it.
    argocd.argoproj.io/hook-delete-policy: BeforeHookCreation
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 1500
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ha-reload
    spec:
      serviceAccountName: ha-reload
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 65534
        runAsGroup: 65534
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: reload
          image: docker.io/library/python:3.13-alpine@sha256:2dd78ad5cf13a0b68f5134dc49aa9950203a8cf4b7463431b9f3b398287c5059
          command: [python3, /app/ha_reload.py]
          env:
            - name: HA_URL
              value: http://ha-home-assistant.home-assistant.svc:8123
            - name: HA_TOKEN
              valueFrom:
                secretKeyRef:
                  name: ha-reload-token
                  key: token
            - name: PYTHONDONTWRITEBYTECODE
              value: "1"
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: [ALL]
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              memory: 128Mi
          volumeMounts:
            - name: packages
              mountPath: /packages
              readOnly: true
            - name: script
              mountPath: /app
              readOnly: true
      volumes:
        - name: packages
          configMap:
            name: ha-packages
        - name: script
          configMap:
            name: ha-reload-script
```

- [ ] **Step 6: Wire it into `cluster/home-assistant/kustomization.yaml`.** Add to `resources:` (after `- mosquitto-servicemonitor.yaml`):

```yaml
  - ha-reload.yaml
  - ha-reload-token.yaml
```

and add a second generator to `configMapGenerator:` (after the `ha-themes` entry):

```yaml
  # The ha-reload PostSync hook's script (ha-reload.yaml).
  - name: ha-reload-script
    files:
      - hooks/ha_reload.py
```

- [ ] **Step 7: Create and seal the reload token** (never prints the token):

```bash
export KUBECONFIG=/tmp/galaxy-kubeconfig
( umask 077 && nix develop --command kubectl create secret generic ha-reload-token -n home-assistant \
    --from-file=token="$HOME/.config/galaxy/reload-token" --dry-run=client -o yaml \
    > cluster/home-assistant/ha-reload-token.secret.yaml )
git check-attr filter -- cluster/home-assistant/ha-reload-token.secret.yaml
nix develop --command ./sign.sh
grep -c '^kind: SealedSecret' cluster/home-assistant/ha-reload-token.yaml
```
Expected: `filter: git-crypt`; `sign.sh` lists the new file; `1`. (The hook strips the trailing newline `--from-file` keeps.)

- [ ] **Step 8: Verify the render** (no apply)

Run: `nix develop --command bash -c 'kustomize build cluster/home-assistant > /tmp/claude-ha-render.yaml && grep -E "^kind:|argocd.argoproj.io/hook|ha_reload.py:" /tmp/claude-ha-render.yaml | sort | uniq -c'`
Expected: lines for `kind: Job`, `kind: Role`, `kind: RoleBinding`, `kind: ServiceAccount`, `kind: SealedSecret` (≥2), `argocd.argoproj.io/hook: PostSync`, and `ha_reload.py: |` once.

- [ ] **Step 9: Commit (do not push yet — Task 13 deploys)**

```bash
git add cluster/home-assistant/hooks cluster/home-assistant/ha-reload.yaml cluster/home-assistant/ha-reload-token.secret.yaml cluster/home-assistant/ha-reload-token.yaml cluster/home-assistant/kustomization.yaml tools/hactl/tests/test_ha_reload.py
nix develop --command git commit -m "home-assistant: ha-reload PostSync hook (reload until revision matches; restart once for new integrations)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git show HEAD:cluster/home-assistant/ha-reload-token.secret.yaml | head -c 9 | od -c | head -1
```
Expected: the `od` line shows `GITCRYPT`. If not, STOP, `git reset --soft HEAD~1`, fix, re-commit.

---

### Task 12: `deploy` and `selftest`

**Files:**
- Create: `tools/hactl/src/hactl/deploy.py`
- Create: `tools/hactl/src/hactl/selftest.py`
- Modify: `tools/hactl/src/hactl/__main__.py` (`MODULES`)
- Test: `tools/hactl/tests/test_deploy.py`

**Interfaces:**
- Consumes: `health.collect`, `health.render`, `shot.*`, `query.find_rows`, `query.fetch_history`, `query.fetch_stats`, `client.Client`, `paths.REPO`.
- Produces: `deploy.argo_status() -> dict` (keys `sync_revision, sync, phase, op_revision, message`); `deploy.wait_synced(head, status=argo_status, timeout=900, interval=10, clock=time.monotonic, sleep=time.sleep, log=print) -> dict`; `deploy.DEFAULT_VIEWS`; CLI `hactl deploy [--timeout S] [--shot]`; CLI `hactl selftest`.

- [ ] **Step 1: Write the failing tests** `tools/hactl/tests/test_deploy.py`:

```python
import pytest

from hactl import deploy
from hactl.errors import HactlError

HEAD, OLD = "a" * 40, "b" * 40


def st(sync_revision, sync, phase, op_revision, message=""):
    return {"sync_revision": sync_revision, "sync": sync, "phase": phase, "op_revision": op_revision, "message": message}


def run(seq, timeout=900):
    seq = list(seq)
    clock = {"t": 0.0}

    def status():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def sleep(s):
        clock["t"] += s

    return deploy.wait_synced(HEAD, status=status, timeout=timeout, interval=10,
                              clock=lambda: clock["t"], sleep=sleep, log=lambda m: None)


def test_waits_through_out_of_sync_and_running():
    out = run([st(OLD, "Synced", "Succeeded", OLD), st(HEAD, "OutOfSync", "Succeeded", OLD),
               st(HEAD, "OutOfSync", "Running", HEAD), st(HEAD, "Synced", "Succeeded", HEAD)])
    assert out["op_revision"] == HEAD


def test_untouched_app_counts_as_synced():
    out = run([st(OLD, "Synced", "Succeeded", OLD), st(HEAD, "Synced", "Succeeded", OLD)])
    assert out["sync_revision"] == HEAD


def test_failed_hook_raises():
    with pytest.raises(HactlError, match="Failed"):
        run([st(HEAD, "Synced", "Failed", HEAD, "PostSync hook ha-reload failed")])


def test_timeout():
    with pytest.raises(HactlError, match="timed out"):
        run([st(OLD, "Synced", "Succeeded", OLD)], timeout=30)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `nix develop --command python -m pytest -q tools/hactl/tests/test_deploy.py`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement `tools/hactl/src/hactl/deploy.py`:**

```python
"""After a push: wait for Argo and the ha-reload hook, then verify.

Read-only on the cluster: Argo applies, the hook reloads/restarts; this waits
and checks. Argo polls git every ~3 min, so expect a short wait.
"""
import json
import subprocess
import time
from pathlib import Path

from hactl import health, output, paths, shot
from hactl.errors import HactlError

DEFAULT_VIEWS = ["/home-ops/0", "/home-ops/1", "/home-ops/2", "/home-ops/3"]


def _git(*args) -> str:
    return subprocess.run(["git", *args], cwd=paths.REPO, capture_output=True, text=True, check=True).stdout.strip()


def argo_status() -> dict:
    r = subprocess.run(["kubectl", "-n", "argo-cd", "get", "application", "home-assistant", "-o", "json"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise HactlError(f"kubectl get application failed: {r.stderr.strip()[:200]} (KUBECONFIG set?)")
    st = json.loads(r.stdout).get("status", {})
    op = st.get("operationState") or {}
    return {"sync_revision": (st.get("sync") or {}).get("revision"), "sync": (st.get("sync") or {}).get("status"),
            "phase": op.get("phase"), "op_revision": (op.get("syncResult") or {}).get("revision"),
            "message": op.get("message", "")}


def wait_synced(head, status=argo_status, timeout=900, interval=10, clock=time.monotonic, sleep=time.sleep, log=print) -> dict:
    """Done when Argo has compared `head`, is Synced, and no operation is running.

    A push that doesn't touch cluster/home-assistant moves sync_revision without
    starting an operation, so op_revision may stay older: that still counts.
    """
    deadline = clock() + timeout
    last = None
    while True:
        st = status()
        if st != last:
            log(f"argo: {st['sync']} / {st['phase']} (compared {str(st['sync_revision'])[:8]}, "
                f"last sync {str(st['op_revision'])[:8]}) {st['message'][:120]}")
            last = st
        if st["op_revision"] == head and st["phase"] in ("Failed", "Error"):
            raise HactlError(f"Argo sync of {head[:8]} ended {st['phase']}: {st['message']}")
        if st["sync_revision"] == head and st["sync"] == "Synced" and st["phase"] not in ("Running", "Terminating"):
            return st
        if clock() >= deadline:
            raise HactlError(f"timed out after {timeout}s waiting for Argo to sync {head[:8]}")
        sleep(interval)


def _run(args) -> int:
    from hactl.client import Client

    head = _git("rev-parse", "HEAD")
    if not _git("branch", "-r", "--contains", head):
        raise HactlError("HEAD is not pushed yet: git push first")
    wait_synced(head, timeout=args.timeout)
    client = Client()
    report = health.collect(client)
    lines, problem = health.render(report)
    bad = False
    if args.shot:
        results = shot.take(client, shot.plan_shots(DEFAULT_VIEWS, ["phone", "desktop"], ["dark"]), shot.default_out_dir())
        shot_lines, bad = shot.report(results)
        lines += shot_lines
    output.emit(args, report, [f"deployed {head[:8]}"] + lines)
    return 1 if problem or bad else 0


def register(sub) -> None:
    p = sub.add_parser("deploy", parents=[output.COMMON], help="after a push: wait for Argo + ha-reload, then health (+ shots)")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--shot", action="store_true", help="also screenshot the home-ops views")
    p.set_defaults(func=_run)
```

- [ ] **Step 4: Implement `tools/hactl/src/hactl/selftest.py`:**

```python
"""Read-only end-to-end check of every hactl read path against live HA."""
from datetime import timedelta

from hactl import health, output, query, shot


def checks(client):
    yield "REST /api/", lambda: client.get("/api/")["message"] == "API running."
    yield "websocket auth/current_user", lambda: bool(client.ws({"type": "auth/current_user"})[0]["name"])
    yield "find sun.sun", lambda: any(r.entity_id == "sun.sun" for r in query.find_rows(client, text="sun.sun"))
    yield "template", lambda: client.post("/api/template", {"template": "{{ 1 + 1 }}"}, raw=True).strip() == "2"
    yield "history 24h", lambda: isinstance(query.fetch_history(client, ["sun.sun"], timedelta(hours=24)), list)
    yield "stats 7d", lambda: isinstance(query.fetch_stats(client, "sensor.living_room_temperature", 7), list)
    yield "trace list", lambda: isinstance(client.ws({"type": "trace/list", "domain": "automation"})[0], list)
    yield "system log", lambda: isinstance(client.ws({"type": "system_log/list"})[0], list)
    yield "health collect", lambda: "drift" in health.collect(client)

    def screenshot():
        [r] = shot.take(client, shot.plan_shots(["/home-ops/0"], ["phone"], ["dark"]), shot.default_out_dir())
        return r["cards"] > 0 and r["theme_ok"] and not r["error_cards"]

    yield "screenshot /home-ops/0 (phone)", screenshot


def _run(args) -> int:
    from hactl.client import Client

    client = Client()
    results = []
    for name, check in checks(client):
        try:
            ok, err = bool(check()), ""
        except Exception as e:  # report every failure, keep going
            ok, err = False, f"{type(e).__name__}: {e}"
        results.append({"check": name, "ok": ok, "error": err})
    lines = [f"{'PASS' if r['ok'] else 'FAIL'}  {r['check']}" + (f"  ({r['error']})" if r["error"] else "") for r in results]
    output.emit(args, results, lines)
    return 0 if all(r["ok"] for r in results) else 1


def register(sub) -> None:
    p = sub.add_parser("selftest", parents=[output.COMMON], help="read-only end-to-end check against live HA")
    p.set_defaults(func=_run)
```

In `__main__.py`: `MODULES: list[str] = ["revision", "lint", "query", "shot", "preview", "health", "act", "deploy", "selftest"]`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `nix develop --command python -m pytest -q tools/hactl`
Expected: all pass.

- [ ] **Step 6: Live check**

Run: `export KUBECONFIG=/tmp/galaxy-kubeconfig; nix develop --command hactl selftest`
Expected: 10 `PASS` lines, exit 0.

- [ ] **Step 7: Commit**

```bash
git add tools/hactl
nix develop --command git commit -m "hactl: deploy (wait for Argo + hook, then verify) and selftest" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Go live — the `prometheus` deploy proves the loop

**Files:** none new (pushes Tasks 1–12).

**Interfaces:**
- Consumes: everything above. Expected outcome: the hook reloads HA (revision sensor appears), sees `prometheus` declared but not loaded, restarts HA once, and finishes green.

- [ ] **Step 1: Preflight (S1, S3).** Read current state; do not push into a degraded cluster:

```bash
export KUBECONFIG=/tmp/galaxy-kubeconfig
kubectl get nodes
kubectl -n rook get cephcluster rook-cluster -o jsonpath='{.status.ceph.health}{"\n"}'
kubectl -n argo-cd get application home-assistant home-assistant-release -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status
kubectl -n home-assistant get pods
```
Expected: 3 nodes Ready; Ceph `HEALTH_OK` or the known `HEALTH_WARN` (BLUESTORE_SLOW_OP noise — CLAUDE.md sharp edge 1); both apps Synced/Healthy; HA pod Running. If anything else is off, STOP and report to Chris.

- [ ] **Step 2: Final local gate**

Run: `nix develop --command bash -c 'python -m pytest -q tools/hactl && hactl lint && hactl revision --check'`
Expected: all pass, `lint: clean` (or only the docker WARNING), exit 0.

- [ ] **Step 3: Push**

Run: `git push origin main`

- [ ] **Step 4: Deploy and watch.** Start `hactl deploy --shot` in the background (Bash `run_in_background: true`): `export KUBECONFIG=/tmp/galaxy-kubeconfig; nix develop --command hactl deploy --shot`. While it runs, arm a `Monitor` on the hook's log (do not busy-loop kubectl): `kubectl -n home-assistant logs -f job/ha-reload` (retry until the Job exists). HA will be down ~1–2 min during the restart; that is expected.
Expected hook log: `expected revision <12 hex>` → one or more `loaded revision:` lines ending in the expected value → `not loaded: ['prometheus'] -> restarting Home Assistant once` → `ok: restarted once; every declared integration is loaded`. Then `hactl deploy` prints `deployed <sha>` and `health: ok`, plus clean shot lines.

- [ ] **Step 5: Verify the endpoint and drift**

```bash
curl -s -o /dev/null -w 'GET /api/prometheus -> %{http_code}\n' -H "Authorization: Bearer $(cat ~/.config/galaxy/ha-token)" https://home.chrismiller.xyz/api/prometheus
export KUBECONFIG=/tmp/galaxy-kubeconfig; nix develop --command hactl health
```
Expected: `-> 200`; `health: ok` with `drift: none` and `ha-reload hook: succeeded`.

- [ ] **Step 6: If it fails.** Hook log `FAIL: … never showed the expected revision` → `hactl log --errors` for a reload error, fix in git, push again (the next sync reruns the hook). `FAIL: … still not loaded` → `hactl log --errors | grep -i prometheus`; fix in git. HA pod stuck in `Init` (the chart's `check-config` init container) → `kubectl -n home-assistant logs deploy/ha-home-assistant -c check-config`, fix in git (`git revert` the offending commit if needed). Never `kubectl apply` or edit the Deployment by hand (S2). Report what happened to Chris either way.

---

### Task 14: Skill, runbook, README, CLAUDE.md

**Files:**
- Rewrite: `.claude/skills/ha-config/SKILL.md`
- Create: `docs/ha.md`
- Create: `tools/hactl/README.md`
- Modify: `CLAUDE.md` (Topology table)

**Interfaces:**
- Consumes: every command from Tasks 1–12; facts from Task 13.

- [ ] **Step 1: Rewrite `.claude/skills/ha-config/SKILL.md`** (full replacement):

````markdown
---
name: ha-config
description: Change or inspect Home Assistant on galaxy — automations, helpers, templates, scenes, dashboards, themes — through git and the hactl toolkit, and verify the result live (screenshots, traces, health). Use for ANY HA config or dashboard work, any "why did this automation do X", and any HA debugging. NEVER edit package automations or YAML dashboards in the HA UI. NEVER deploy without `hactl lint`. NEVER call lock/notify actions without Chris's OK. NEVER let a generic YAML formatter touch HA config.
---

# ha-config

HA runs in-cluster (Argo `home-assistant-release`, chart `~/Repos/ha-helm`). Its config is git: `cluster/home-assistant/{packages,dashboards,themes}` → kustomize ConfigMaps → `/config`. **`hactl`** (`tools/hactl`, on PATH inside `nix develop`) is how you look at HA, check a change, see it, and verify the deploy. Runbook: `docs/ha.md`.

## The loop (do every step)

1. **Look first.** `hactl find <text> [--area Bedroom] [--domain light] [--unavailable]`, `hactl state <id>`, `hactl history <id…> --since 6h` (flags flapping), `hactl trace automation.<x>`, `hactl log --errors`, `hactl health`. Never guess an entity_id: they are sticky after Zigbee renames and mangled by integrations.
2. **Edit** files under `cluster/home-assistant/`. A new file must be listed in `kustomization.yaml` (lint catches a miss). Package filenames: lowercase + underscores.
3. **Check.** Render new templates against live state first: `hactl template -f snippet.j2`. Then `hactl lint`: unknown entity ids, broken dashboard templates, quoting, kustomization, revision, and HA's `check_config` in the deployed image.
4. **See dashboards before committing.** `hactl preview cluster/home-assistant/dashboards/overview.yaml --view N` pushes to the admin-only `/claude-preview` dashboard and screenshots phone (412) + desktop (1440). **Read the PNGs.** Fix every error card, every unexpected "unavailable", every visual problem; iterate until right.
5. **Commit and push** (`nix develop --command git commit …`). Pre-commit regenerates `packages/config_revision.yaml`; if it stops the commit, `git add` it and commit again.
6. **Deploy and verify.** `hactl deploy --shot` (run in the background) waits for Argo and the `ha-reload` hook, then runs `health` and screenshots. Then exercise the change for real: trigger it with reversible actions (`hactl call …`), read the run (`hactl trace …`), and look (`hactl shot …`). A change is done when you have seen it work, not when it is pushed.

## Actuation policy (Chris, 2026-10-03)

Anything reversible is free while testing: lights, scenes, fans, covers/blinds, the AC, adaptive-lighting switches, the preview dashboard. **Ask Chris first** for locks, phone notifications, speech/announcements, and HA restart/stop; `hactl call` refuses those without `--confirmed`. The same rule applies to the HA MCP server's tools.

## How a change reaches HA

push → Argo syncs the `home-assistant` app (ConfigMaps) → the **`ha-reload` PostSync Job** calls `homeassistant.reload_all` until `sensor.ha_config_revision` equals the committed revision, and restarts HA once if a top-level integration in the packages isn't loaded (e.g. a new `prometheus:`). A failed hook shows in Argo and in `hactl health`; read it with `kubectl -n home-assistant logs job/ha-reload`. There is no manual `rollout restart` step any more.

## Dashboards (YAML mode)

YAML dashboards live in `cluster/home-assistant/dashboards/` and are declared in the chart's `lovelace.dashboards` value (`urlPath` must contain a hyphen). **Never flip global `lovelace: mode: yaml`** — it disables the UI resource registry and breaks every add-on card. `check_config` validates the `lovelace:` schema, not dashboard contents — that is what `hactl lint` (templates, entity ids) and `hactl preview`/`shot` (error cards, rendering) are for.

## Hard rules / gotchas

- **Never edit package automations in the HA UI** (read-only there by design).
- **yamlfmt corrupts HA YAML** (strips quotes; YAML 1.1 then turns `on` into a boolean and `03:00:00` into 10800). HA dirs are excluded from yamlfmt; keep them excluded. Quote `'on'`/`'off'`/times (lint enforces it).
- **`check_config` exits 0 even on errors**; `hactl lint` greps it. It also can't catch runtime template errors: guard template sensors with `availability:` and check `hactl log --errors` after deploy.
- **Custom-component packages need the component present** to validate: add new ones to `CUSTOM_COMPONENTS` in `tools/hactl/src/hactl/lint.py`.
- **Context-based loop guards don't work for Hue/z2m devices**: they report state back with fresh contexts. Never write bidirectional sync automations; one source of truth per behaviour.
- **`.storage` (integrations, entity/device/area registry, UI helpers, storage dashboards) is not in git yet** — plan 2 of the overhaul adds `state/` manifests with `hactl plan/apply`. Until then, ask Chris before changing any of it, and never change it ad hoc.
- **Never `--no-verify`** (CLAUDE.md S6).
````

- [ ] **Step 2: Create `docs/ha.md`:**

````markdown
# Home Assistant runbook

HA on galaxy: `https://home.chrismiller.xyz`, namespace `home-assistant`, Argo apps `home-assistant` (kustomize: config ConfigMaps, backups, the `ha-reload` hook) and `home-assistant-release` (helm chart `ChristopherJMiller/ha-helm`). Design: `docs/superpowers/specs/2026-10-03-ha-agent-overhaul-design.md`. Day-to-day workflow: the `ha-config` skill.

## hactl

`tools/hactl`, on PATH inside `nix develop`. `hactl --help` lists commands; each takes `--json`.

| Command | Use |
|---|---|
| `find`, `state`, `history`, `stats`, `template`, `trace`, `log` | Read live HA (history flags flapping; trace shows each step) |
| `shot PATH…` | Screenshots at phone 412 / desktop 1440, with a report (error cards, theme, unavailable cards) |
| `preview FILE` | Push a dashboard to `/claude-preview` (admin-only scratch) and screenshot it |
| `lint [--offline]` | Config rules + `check_config`; live adds entity-id and dashboard-template checks |
| `health` | Drift, hook status, failing automations, repairs, top log errors |
| `call DOMAIN.SERVICE` | Actions; locks/notify/tts/restart need `--confirmed` |
| `deploy [--shot]` | After a push: wait for Argo + hook, then health (+ shots) |
| `revision`, `selftest` | Config revision hash; read-only end-to-end check |

`deploy` and `health` read the cluster with kubectl: `export KUBECONFIG=/tmp/galaxy-kubeconfig` first.

## Tokens

All three are long-lived tokens on Chris's own HA account (his choice; HA has no scoped tokens). Each is revocable on its own from his HA profile → Security.

| Token | Lives in | Used by |
|---|---|---|
| hactl | `tools/hactl/agent.secret.yaml` (git-crypt) | `hactl` (or `$HA_TOKEN`) |
| gitops-reload | `cluster/home-assistant/ha-reload-token.secret.yaml` (git-crypt) → sealed `ha-reload-token.yaml` | the `ha-reload` hook |
| prometheus-scrape | (plan 3) | Prometheus |

Rotate: mint a new token in HA, replace the file's value (for the sealed one: regenerate the `.secret.yaml` with `kubectl create secret … --from-file … --dry-run=client -o yaml`, delete the old `ha-reload-token.yaml`, run `./sign.sh`), commit, push, then revoke the old token. Symptom of a dead hactl token: `HA rejected the token (401)`, or `shot` failing with "HA showed its login page".

## How a change reaches HA

1. Pre-commit writes `packages/config_revision.yaml`: a hash of packages, dashboards and themes, as `sensor.ha_config_revision`.
2. Argo syncs the ConfigMaps. Kubelet updates the pod's mounted files within ~2 min.
3. The **`ha-reload` PostSync Job** (`hooks/ha_reload.py`) calls `homeassistant.reload_all` every 20 s until the sensor shows the new revision (timeout 6 min), then checks every top-level package key is a loaded integration. If one isn't, it restarts HA once (patches the Deployment's `restartedAt`; strategy is `Recreate`) and waits until it is.
4. `hactl deploy` waits for all of that, then runs `health`.

A failed hook: `kubectl -n home-assistant logs job/ha-reload`. It keeps the last run until the next sync. Fix the cause in git and push; the next sync reruns it.

## Screenshots: how auth works

HA's frontend keeps OAuth tokens in `localStorage.hassTokens`, not a cookie. `hactl shot` plants the hactl token there before the frontend boots, in a throwaway browser context. The wrapper drops the host's `LD_LIBRARY_PATH` (a system alsa-lib built against a newer glibc kills the nix-built browser).

## Not in git (yet)

Integrations/config entries, the entity/device/area registries, UI helpers, storage dashboards (`lovelace`, `map`, `claude-preview`) and Lovelace resources live in HA's `.storage`. Plan 2 describes them in `cluster/home-assistant/state/` with `hactl import/plan/apply`. Users and tokens can never be declared.
````

- [ ] **Step 3: Create `tools/hactl/README.md`:**

```markdown
# hactl

Home Assistant agent toolkit for galaxy: query, screenshot, preview, lint,
health-check and deploy-verify HA. Runbook and command table: `docs/ha.md`.
Workflow: the `ha-config` skill.

- Run: `hactl …` inside `nix develop` (the dev shell puts `bin/` on PATH and
  points Playwright at the nix-built browsers).
- Test: `nix develop --command python -m pytest -q tools/hactl`
- Token: `$HA_TOKEN`, else `agent.secret.yaml` (git-crypt). Never printed.
- Layout: one module per command group in `src/hactl/`; each exposes
  `register(subparsers)` and is listed in `__main__.MODULES`. Heavy
  dependencies (websockets, playwright) are imported inside functions so CI
  needs only pyyaml + pytest.
- The in-cluster reload hook is `cluster/home-assistant/hooks/ha_reload.py`
  (stdlib only); its tests live here in `tests/test_ha_reload.py`.
```

- [ ] **Step 4: Add a row to the Topology table in `CLAUDE.md`**, after the `Storage` row:

```markdown
| Home Assistant | `home.chrismiller.xyz`, ns `home-assistant` | Config is git (`cluster/home-assistant/`), applied by Argo + the `ha-reload` PostSync hook. Look, preview, screenshot and verify with **`hactl`** (`tools/hactl`, in `nix develop`). Skill: `ha-config`. Runbook: `docs/ha.md`. |
```

- [ ] **Step 5: Verify the docs match reality**

Run: `nix develop --command bash -c 'hactl --help'` and check every command named in `docs/ha.md` and the skill appears. Run `nix develop --command hactl health` once more: `health: ok`.

- [ ] **Step 6: Commit and push**

```bash
git add .claude/skills/ha-config/SKILL.md docs/ha.md tools/hactl/README.md CLAUDE.md
nix develop --command git commit -m "ha-config skill rebuilt around hactl; HA runbook" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push origin main
```

- [ ] **Step 7: Update memory** — in `/home/chris/.claude/projects/-home-chris-Repos-luma-homeops/memory/project_ha_agent_tooling.md`, record: plan 1 shipped (date), `hactl` command list, hook behaviour, and that plans 2–5 remain. Update `reference_ha_zigbee_ops.md`'s "No stakater/reloader … run rollout restart" line to point at the `ha-reload` hook instead.
