#!/usr/bin/env python3
"""mcp-probe: stdlib-only OAuth/MCP probe for the Luma 0a verifications.

Never prints a token, a refresh token or an email.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DEFAULT_RESOURCE = "https://mcp.chrismiller.xyz/gramps/mcp"
SCOPE = "openid email profile offline_access"
MCP_PROTOCOL = "2025-11-25"


# ---------- pure functions ----------

def pkce_pair():
    verifier = secrets.token_urlsafe(64)  # 86 chars, [A-Za-z0-9_-]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def parse_bearer_challenge(h):
    out = {}
    for m in re.finditer(r'([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))', h or ""):
        out[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def as_metadata_urls(issuer):
    p = urllib.parse.urlsplit(issuer)
    base = f"{p.scheme}://{p.netloc}"
    path = p.path.rstrip("/")
    if not path:
        return [base + "/.well-known/oauth-authorization-server",
                base + "/.well-known/openid-configuration"]
    return [base + "/.well-known/oauth-authorization-server" + path,
            base + "/.well-known/openid-configuration" + path,
            base + path + "/.well-known/openid-configuration"]


def _jwt_claims(jwt):
    parts = jwt.split(".")
    if len(parts) < 2:
        raise ValueError("not a JWT")
    seg = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(seg))


def claims_summary(jwt, has_refresh):
    c = _jwt_claims(jwt)
    life = c["exp"] - c["iat"] if "exp" in c and "iat" in c else None
    return {"iss": c.get("iss"), "aud": c.get("aud"), "azp": c.get("azp"),
            "lifetime_s": life, "has_refresh_token": bool(has_refresh),
            "jti": "jti" in c, "sid": "sid" in c}


# ---------- http helpers ----------

def _http(method, url, body=None, headers=None, timeout=20):
    """Returns (status, headers, bytes); HTTP errors are returned, not raised."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _json(b):
    try:
        return json.loads(b)
    except Exception:
        return None


def _init_body():
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": MCP_PROTOCOL, "capabilities": {},
        "clientInfo": {"name": "mcp-probe", "version": "0"}}}).encode()


def _mcp_post(url, token=None):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        h["Authorization"] = "Bearer " + token
    return _http("POST", url, _init_body(), h)


def do_discover(resource):
    status, hdrs, _ = _mcp_post(resource)
    ch = parse_bearer_challenge(hdrs.get("www-authenticate", ""))
    prm_url = ch.get("resource_metadata")
    if not prm_url:
        p = urllib.parse.urlsplit(resource)
        prm_url = f"{p.scheme}://{p.netloc}/.well-known/oauth-protected-resource{p.path}"
    st, _, b = _http("GET", prm_url)
    prm = _json(b) if st == 200 else None
    out = {"resource_metadata": prm_url, "authorization_server": None, "issuer": None,
           "registration_endpoint": None, "authorization_endpoint": None,
           "token_endpoint": None, "agree": False}
    if not prm or not prm.get("authorization_servers"):
        out["error"] = f"PRM fetch failed: HTTP {st}"
        return out
    asrv = prm["authorization_servers"][0]
    out["authorization_server"] = asrv
    for u in as_metadata_urls(asrv):
        st, _, b = _http("GET", u)
        md = _json(b) if st == 200 else None
        if md:
            out["issuer"] = md.get("issuer")
            for k in ("registration_endpoint", "authorization_endpoint", "token_endpoint"):
                out[k] = md.get(k)
            out["as_metadata_url"] = u
            break
    out["agree"] = out["issuer"] is not None and out["issuer"] == asrv
    return out


# ---------- token storage ----------

def token_path(client_id):
    base = os.environ.get("XDG_RUNTIME_DIR") or str(Path.home() / ".cache")
    return Path(base) / "mcp-probe" / f"{client_id}.json"


def save_tokens(client_id, data):
    p = token_path(client_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(p, 0o600)


def load_tokens(client_id):
    return json.loads(token_path(client_id).read_text())


# ---------- commands ----------

def cmd_discover(a):
    out = do_discover(a.resource)
    print(json.dumps(out, indent=2))
    return 0 if out["agree"] else 1


def cmd_register(a):
    redirect = a.redirect or f"http://localhost:{a.port}/callback"
    endpoint = a.endpoint or do_discover(a.resource)["registration_endpoint"]
    if not endpoint:
        print("no registration endpoint", file=sys.stderr)
        return 1
    body = json.dumps({"client_name": a.name, "redirect_uris": [redirect],
                       "grant_types": ["authorization_code", "refresh_token"],
                       "response_types": ["code"], "token_endpoint_auth_method": "none",
                       "scope": SCOPE}).encode()
    st, _, b = _http("POST", endpoint, body, {"Content-Type": "application/json"})
    print(st)
    print(json.dumps(_json(b), indent=2) if _json(b) is not None else b.decode(errors="replace")[:500])
    return 0 if st == 201 else 1


def _wait_for_code(port, timeout):
    got = {}
    done = threading.Event()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            if u.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            got.update({k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"mcp-probe: login complete, you can close this tab.\n")
            done.set()

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", port), H)
    srv.timeout = 1
    end = time.time() + timeout
    while not done.is_set() and time.time() < end:
        srv.handle_request()
    srv.server_close()
    return got


def cmd_login(a):
    token_endpoint = a.token_endpoint
    auth_endpoint = a.authorize_url
    if not (token_endpoint and auth_endpoint):
        d = do_discover(a.resource)
        token_endpoint = token_endpoint or d["token_endpoint"]
        auth_endpoint = auth_endpoint or d["authorization_endpoint"]
    if not (token_endpoint and auth_endpoint):
        print("could not determine authorize/token endpoints", file=sys.stderr)
        return 1
    redirect = f"http://localhost:{a.port}/callback"
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    q = urllib.parse.urlencode({
        "response_type": "code", "client_id": a.client_id, "redirect_uri": redirect,
        "scope": SCOPE, "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "resource": a.resource})
    print("authorize_url: " + auth_endpoint + "?" + q, flush=True)
    got = _wait_for_code(a.port, a.timeout)
    if got.get("state") != state or "code" not in got:
        print("login failed: " + (got.get("error", "no/invalid callback") if got else "timeout"), flush=True)
        return 1
    form = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": got["code"], "redirect_uri": redirect,
        "client_id": a.client_id, "code_verifier": verifier, "resource": a.resource}).encode()
    st, _, b = _http("POST", token_endpoint, form, {"Content-Type": "application/x-www-form-urlencoded"})
    tok = _json(b)
    if st != 200 or not tok or "access_token" not in tok:
        err = (tok or {}).get("error", "") if isinstance(tok, dict) else ""
        print(f"token exchange failed: HTTP {st} {err}", flush=True)
        return 1
    tok["token_endpoint"] = token_endpoint
    save_tokens(a.client_id, tok)
    has_refresh = bool(tok.get("refresh_token"))
    try:
        s = claims_summary(tok["access_token"], has_refresh)
        s["source"] = "access_token"
    except Exception:
        s = claims_summary(tok["id_token"], has_refresh) if tok.get("id_token") else {}
        s["source"] = "id_token (access_token is opaque)"
    s["expires_in"] = tok.get("expires_in")
    print("claims_summary: " + json.dumps(s), flush=True)
    return 0


def cmd_call(a):
    token = None
    if not a.no_token:
        if not a.client_id:
            print("need --client-id or --no-token", file=sys.stderr)
            return 2
        token = load_tokens(a.client_id)["access_token"]
    st, hdrs, _ = _mcp_post(a.url or a.resource, token)
    print(json.dumps({"status": st, "www-authenticate": hdrs.get("www-authenticate")}))
    return 0


def cmd_refresh(a):
    t = load_tokens(a.client_id)
    form = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                                   "client_id": a.client_id}).encode()
    st, _, b = _http("POST", t["token_endpoint"], form, {"Content-Type": "application/x-www-form-urlencoded"})
    new = _json(b)
    if st != 200 or not new or "access_token" not in new:
        print(f"refresh failed: HTTP {st}")
        return 1
    rotated = bool(new.get("refresh_token")) and new["refresh_token"] != t["refresh_token"]
    new.setdefault("refresh_token", t["refresh_token"])
    new["token_endpoint"] = t["token_endpoint"]
    save_tokens(a.client_id, new)
    try:
        life = claims_summary(new["access_token"], True)["lifetime_s"]
    except Exception:
        life = new.get("expires_in")
    print(json.dumps({"lifetime_s": life, "refresh_rotated": rotated}))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mcp-probe")
    ap.add_argument("--resource", default=DEFAULT_RESOURCE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("discover").set_defaults(fn=cmd_discover)
    r = sub.add_parser("register")
    r.add_argument("--name", required=True)
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--port", type=int)
    g.add_argument("--redirect")
    r.add_argument("--endpoint")
    r.set_defaults(fn=cmd_register)
    lg = sub.add_parser("login")
    lg.add_argument("--client-id", required=True)
    lg.add_argument("--port", type=int, required=True)
    lg.add_argument("--authorize-url")
    lg.add_argument("--token-endpoint")
    lg.add_argument("--timeout", type=int, default=1800)
    lg.set_defaults(fn=cmd_login)
    c = sub.add_parser("call")
    c.add_argument("--client-id")
    c.add_argument("--no-token", action="store_true")
    c.add_argument("--url")
    c.set_defaults(fn=cmd_call)
    rf = sub.add_parser("refresh")
    rf.add_argument("--client-id", required=True)
    rf.set_defaults(fn=cmd_refresh)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
