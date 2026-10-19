import base64
import hashlib
import json
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_probe import (as_metadata_urls, claims_summary,  # noqa: E402
                       parse_bearer_challenge, pkce_pair)


def _unsigned(claims):
    def b(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return b({"alg": "none"}) + "." + b(claims) + "."


def test_pkce_pair_s256():
    v, c = pkce_pair()
    assert 43 <= len(v) <= 128 and set(v) <= set(string.ascii_letters + string.digits + "-._~")
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def test_parse_bearer_challenge():
    h = 'Bearer error="invalid_token", resource_metadata="https://m/.well-known/oauth-protected-resource/gramps/mcp"'
    assert parse_bearer_challenge(h) == {"error": "invalid_token",
        "resource_metadata": "https://m/.well-known/oauth-protected-resource/gramps/mcp"}
    assert parse_bearer_challenge('Bearer scope="a b"')["scope"] == "a b"


def test_as_metadata_urls():
    want = ["https://a.example/.well-known/oauth-authorization-server",
            "https://a.example/.well-known/openid-configuration"]
    assert as_metadata_urls("https://a.example") == want
    assert as_metadata_urls("https://a.example/") == want
    assert as_metadata_urls("https://a.example/t") == [
        "https://a.example/.well-known/oauth-authorization-server/t",
        "https://a.example/.well-known/openid-configuration/t",
        "https://a.example/t/.well-known/openid-configuration"]


def test_claims_summary_redacts():
    tok = _unsigned({"iss": "i", "aud": "c", "exp": 110, "iat": 100, "jti": "j", "email": "x@y"})
    s = claims_summary(tok, has_refresh=True)
    assert s == {"iss": "i", "aud": "c", "azp": None, "lifetime_s": 10,
                 "has_refresh_token": True, "jti": True, "sid": False}
