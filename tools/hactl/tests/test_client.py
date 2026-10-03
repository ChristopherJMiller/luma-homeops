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


def test_response_cut_off_mid_body_is_reported():
    # HA restarting can drop the connection mid-response (IncompleteRead).
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{\"partial\":")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    with pytest.raises(HactlError, match="cannot reach HA|dropped"):
        Client(url=f"http://127.0.0.1:{port}", token=TOKEN).get("/ok")
    srv.close()
