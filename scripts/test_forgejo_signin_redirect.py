"""Static tests for the Forgejo sign-in redirect (cluster/forgejo/signin-redirect.yaml).

    python -m pytest -q -p no:cacheprovider scripts/test_forgejo_signin_redirect.py

Runs as the `forgejo-signin-redirect` pre-commit hook. Checks the contract that keeps the
redirect safe: GET only, flash-cookie and ?local guards present, redirect never permanent,
the query carried over, the chart's Ingress left to serve everything else, and the objects
wired into the kustomization. Traefik v3.7 rule syntax was checked against a real Traefik
(`Query(`local`)` does NOT match `?local=1`; `QueryRegexp` does), so the test pins that.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
FILE = REPO / "cluster/forgejo/signin-redirect.yaml"
KUSTOMIZATION = REPO / "cluster/forgejo/kustomization.yaml"


@pytest.fixture(scope="module")
def objs():
    return {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(FILE.read_text()) if d}


@pytest.fixture(scope="module")
def route(objs):
    routes = objs[("IngressRoute", "forgejo-signin")]["spec"]["routes"]
    assert len(routes) == 1
    return routes[0]


def test_objects_present(objs):
    assert set(objs) == {("Middleware", "signin-to-sso"), ("IngressRoute", "forgejo-signin")}


def test_wired_into_kustomization():
    k = yaml.safe_load(KUSTOMIZATION.read_text())
    assert "signin-redirect.yaml" in k["resources"]


def test_route_matches_only_get_login_on_the_forge(route):
    m = " ".join(route["match"].split())
    assert "Host(`git.realliance.net`)" in m
    assert "Path(`/user/login`)" in m  # exact path: never a prefix
    assert "Method(`GET`)" in m  # POST stays Forgejo's 403
    assert "PathPrefix" not in m


def test_route_guards(route):
    m = " ".join(route["match"].split())
    # ?local, any value. Query(`local`) misses `local=1` on Traefik v3.7.
    assert "!QueryRegexp(`local`, `.*`)" in m
    assert "!Query(`local`)" not in m
    # a failed SSO callback sets the `flash` cookie: skip, or the user loops through Keycloak
    assert re.search(r"!HeaderRegexp\(`Cookie`, `\(\^\|;\\s\*\)flash=`\)", m)


def test_route_outranks_chart_ingress_and_targets_forgejo(route):
    assert route["priority"] >= 1000
    assert route["services"] == [{"name": "forgejo-http", "port": 3000}]
    assert route["middlewares"] == [{"name": "signin-to-sso"}]


def test_entrypoint_and_no_tls(objs):
    spec = objs[("IngressRoute", "forgejo-signin")]["spec"]
    assert spec["entryPoints"] == ["websecure"]
    assert "tls" not in spec  # the default TLSStore serves wildcard-tls


def test_redirect_regex(objs):
    rr = objs[("Middleware", "signin-to-sso")]["spec"]["redirectRegex"]
    assert rr["permanent"] is False  # a 301 would be cached by browsers
    rx = re.compile(rr["regex"])
    repl = rr["replacement"].replace("${1}", r"\1")
    go = lambda url: rx.sub(repl, url)
    base = "https://git.realliance.net/user/oauth2/realliance"
    assert go("https://git.realliance.net/user/login") == base
    assert go("https://git.realliance.net/user/login?redirect_to=%2Fx") == base + "?redirect_to=%2Fx"
    # only the login page itself is rewritten
    for other in ("https://git.realliance.net/user/login/x", "https://git.realliance.net/user/loginx"):
        assert not rx.match(other)
