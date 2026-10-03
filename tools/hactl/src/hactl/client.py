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
