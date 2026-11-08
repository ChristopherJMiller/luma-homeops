"""Static tests for `docs.realliance.net` in cloudflare/dns/ (Realliance docs site, Task 9).

    python -m pytest -q -p no:cacheprovider scripts/test_docs_site.py

Runs as the `docs-site` pre-commit hook. The design is the "DNS" part of
docs/superpowers/specs/2026-11-07-realliance-docs-site-design.md, as planned in
docs/superpowers/plans/2026-11-08-realliance-docs-site.md (Global Constraints, D11, Task 9).

The name takes exactly one of two shapes, and anything in between fails:

- **custom domain** (the design): `cloudflare_workers_custom_domain.docs` in workers.tf on the
  `cloudflare.workers` provider, its account id read from `data.cloudflare_zone.realliance_net`, and
  no `docs` record anywhere, since the custom domain brings its own;
- **fallback**, pre-approved for one case only, Cloudflare refusing the custom domain over the
  `*.realliance.net` wildcard: no custom domain, a `proxied = true` `AAAA` record `docs` at `100::`
  in records.tf on the default provider, and one `cloudflare_workers_route` for
  `docs.realliance.net/*` to `realliance-docs` on the `cloudflare.workers` provider.

To take the fallback, delete the custom domain from workers.tf and add the record and the route in
one commit; `test_shape_checker` shows both shapes in miniature.

These tests never open a `*.secret.*` file. The real workers token file is checked only through
`git check-attr`. The tf.sh test runs a copy of tf.sh in a scratch repo, with a fake token file and a
stub `terraform` that reports whether it got the token, never the token.
"""

import hashlib
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
DNS = REPO / "cloudflare/dns"
TOKEN_FILE = "cloudflare/dns/workers-api-token.secret.conf"

ZONE = 'local.zones["realliance.net"].id'
WORKERS = "cloudflare.workers"
ACCOUNT = "data.cloudflare_zone.realliance_net.account.id"
HOSTNAME = "docs.realliance.net"
SERVICE = "realliance-docs"
DOCS_NAMES = ('"docs"', f'"{HOSTNAME}"')

HOOK_FILES_MUST_MATCH = [
    "cloudflare/dns/workers.tf",
    "cloudflare/dns/main.tf",
    "cloudflare/dns/records.tf",
    "cloudflare/dns/tf.sh",
    "cloudflare/dns/README.md",
    TOKEN_FILE,
    "scripts/test_docs_site.py",
    ".gitattributes",
    ".pre-commit-config.yaml",
]

HEX32 = re.compile(r"\b[0-9a-f]{32}\b", re.I)


def clean_env(**extra: str) -> dict[str, str]:
    """os.environ without GIT_*. Under `git commit`, hooks inherit GIT_DIR and GIT_INDEX_FILE, and a
    `git init` in a scratch dir would then re-initialise this repository."""
    return {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, **extra}


# --- a small HCL reader (enough for this module: no heredocs) ---------------------------------------


def _body(text: str, start: int) -> str:
    """The text between the `{` that ends at `start` and its matching `}`; skips strings and comments."""
    depth, i, in_str = 1, start, False
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "#":
            i = text.find("\n", i)
            if i < 0:
                break
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i]
        i += 1
    raise AssertionError("unbalanced braces")


def _depth0(body: str) -> str:
    """A block body with its nested blocks' contents dropped, so only its own attributes remain."""
    keep, depth, i, in_str = [], 0, 0, False
    while i < len(body):
        c = body[i]
        if in_str:
            if c == "\\":
                if depth == 0:
                    keep.append(body[i:i + 2])
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "#":
            j = body.find("\n", i)
            i = len(body) if j < 0 else j
            continue
        elif c in "{}":
            depth += 1 if c == "{" else -1
            i += 1
            continue
        if depth == 0:
            keep.append(c)
        i += 1
    return "".join(keep)


def attrs(body: str) -> dict[str, str]:
    """{key: value} for a block's own `key = value` lines; a repeated key fails."""
    found: dict[str, str] = {}
    for line in _depth0(body).splitlines():
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.*?)\s*$", line)
        if m:
            assert m.group(1) not in found, f"{m.group(1)} is set twice in one block"
            found[m.group(1)] = m.group(2)
    return found


def blocks(text: str, kind: str, *labels: str) -> list[tuple[tuple[str, ...], str]]:
    """[(labels, body)] for every top-level `kind "l1" ... {` block whose first labels are `labels`."""
    n = {"resource": 2, "data": 2, "provider": 1, "variable": 1}[kind]
    pat = re.compile(rf'^{kind}' + r'\s+"([^"]+)"' * n + r"\s*\{", re.M)
    out = []
    for m in pat.finditer(text):
        if m.groups()[:len(labels)] == labels:
            out.append((m.groups(), _body(text, m.end())))
    return out


def named(text: str, kind: str, rtype: str) -> dict[str, str]:
    """{name: body} for `kind "rtype" "name"` blocks; a duplicate name fails."""
    found: dict[str, str] = {}
    for (_, name), body in blocks(text, kind, rtype):
        assert name not in found, f'{kind} "{rtype}" "{name}" is declared twice'
        found[name] = body
    return found


def module(root: Path = DNS) -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(root.glob("*.tf"))}


def joined(files: dict[str, str]) -> str:
    return "\n".join(files.values())


# --- the two shapes ----------------------------------------------------------------------------------


def docs_records(files: dict[str, str]) -> dict[str, tuple[str, dict[str, str]]]:
    """{resource name: (file, attrs)} for every cloudflare_dns_record named `docs` or its FQDN."""
    out = {}
    for fname, text in files.items():
        for name, body in named(text, "resource", "cloudflare_dns_record").items():
            a = attrs(body)
            if a.get("name") in DOCS_NAMES:
                out[name] = (fname, a)
    return out


def custom_domain_problems(files: dict[str, str]) -> list[str]:
    tf, p = joined(files), []
    cds = named(tf, "resource", "cloudflare_workers_custom_domain")
    if list(cds) != ["docs"]:
        return [f"want exactly one cloudflare_workers_custom_domain, `docs`; found {sorted(cds) or 'none'}"]
    if "docs" not in named(files.get("workers.tf", ""), "resource", "cloudflare_workers_custom_domain"):
        p.append("cloudflare_workers_custom_domain.docs belongs in workers.tf")
    want = {"provider": WORKERS, "account_id": ACCOUNT, "zone_id": ZONE,
            "hostname": f'"{HOSTNAME}"', "service": f'"{SERVICE}"'}
    if (got := attrs(cds["docs"])) != want:
        p.append(f"cloudflare_workers_custom_domain.docs is {got}, want exactly {want}")
    zones = named(tf, "data", "cloudflare_zone")
    if "realliance_net" not in zones:
        p.append('data "cloudflare_zone" "realliance_net" is missing (ruling C3)')
    else:
        z = attrs(zones["realliance_net"])
        if z.get("zone_id") != ZONE or set(z) - {"zone_id", "provider"} or z.get("provider", WORKERS) != WORKERS:
            p.append(f"data.cloudflare_zone.realliance_net is {z}, want zone_id = {ZONE} (and at most the workers provider)")
    if recs := docs_records(files):
        p.append(f"a docs record would block the custom domain: {sorted(recs)}")
    if routes := named(tf, "resource", "cloudflare_workers_route"):
        p.append(f"a custom domain needs no cloudflare_workers_route: {sorted(routes)}")
    return p


def fallback_problems(files: dict[str, str]) -> list[str]:
    tf, p = joined(files), []
    if cds := named(tf, "resource", "cloudflare_workers_custom_domain"):
        p.append(f"the fallback has no custom domain: {sorted(cds)}")
    recs = docs_records(files)
    if len(recs) != 1:
        p.append(f"want exactly one docs record; found {sorted(recs) or 'none'}")
    else:
        (fname, a), = recs.values()
        want = {"zone_id": ZONE, "type": '"AAAA"', "content": '"100::"', "proxied": "true"}
        if fname != "records.tf":
            p.append(f"the docs record belongs in records.tf, not {fname}")
        if {k: a.get(k) for k in want} != want or set(a) - {*want, "name", "ttl", "comment"}:
            p.append(f"the docs record is {a}, want {want} on the default provider (ttl and comment allowed)")
    routes = named(tf, "resource", "cloudflare_workers_route")
    if len(routes) != 1:
        p.append(f"want exactly one cloudflare_workers_route; found {sorted(routes) or 'none'}")
    else:
        want = {"provider": WORKERS, "zone_id": ZONE, "pattern": f'"{HOSTNAME}/*"', "script": f'"{SERVICE}"'}
        if (got := attrs(next(iter(routes.values())))) != want:
            p.append(f"the workers route is {got}, want exactly {want}")
    return p


def domain_shape(files: dict[str, str]) -> str:
    """'custom-domain' or 'fallback'; fails unless exactly one shape holds."""
    cd, fb = custom_domain_problems(files), fallback_problems(files)
    assert bool(cd) != bool(fb), (
        "docs.realliance.net must take exactly one shape.\n"
        f"custom domain: {cd or 'holds'}\nfallback: {fb or 'holds'}")
    return "fallback" if cd else "custom-domain"


def test_docs_domain_shape():
    assert domain_shape(module()) in ("custom-domain", "fallback")


# Miniature modules: the checker accepts both shapes and nothing in between.
_PROVIDERS = 'provider "cloudflare" {}\n'
_ZONE_DATA = 'data "cloudflare_zone" "realliance_net" {\n  zone_id = local.zones["realliance.net"].id\n}\n'
_CUSTOM = (
    'resource "cloudflare_workers_custom_domain" "docs" {\n'
    "  provider   = cloudflare.workers\n"
    "  account_id = data.cloudflare_zone.realliance_net.account.id\n"
    '  zone_id    = local.zones["realliance.net"].id\n'
    '  hostname   = "docs.realliance.net"\n'
    '  service    = "realliance-docs"\n'
    "}\n")
_RECORD = (
    'resource "cloudflare_dns_record" "docs" {\n'
    '  zone_id = local.zones["realliance.net"].id\n'
    '  name    = "docs"\n'
    '  type    = "AAAA"\n'
    '  content = "100::"\n'
    "  proxied = true\n"
    "  ttl     = 1\n"
    '  comment = "docs site: the Worker route answers; 100:: is a placeholder."\n'
    "}\n")
_ROUTE = (
    'resource "cloudflare_workers_route" "docs" {\n'
    "  provider = cloudflare.workers\n"
    '  zone_id  = local.zones["realliance.net"].id\n'
    '  pattern  = "docs.realliance.net/*"\n'
    '  script   = "realliance-docs"\n'
    "}\n")


@pytest.mark.parametrize("workers, records, want", [
    (_ZONE_DATA + _CUSTOM, "", "custom-domain"),
    ("", _RECORD, None),  # record without its route
    (_ROUTE, _RECORD, "fallback"),
    (_ZONE_DATA + _ROUTE, _RECORD, "fallback"),  # a leftover data source is harmless
    (_ZONE_DATA + _CUSTOM + _ROUTE, _RECORD, None),  # both at once
    (_ZONE_DATA + _CUSTOM, _RECORD, None),  # the record blocks the custom domain
    (_CUSTOM, "", None),  # no data source to read the account id from
    (_ZONE_DATA + _CUSTOM.replace("  provider   = cloudflare.workers\n", ""), "", None),
    (_ZONE_DATA + _CUSTOM.replace("data.cloudflare_zone.realliance_net.account.id", '"0123"'), "", None),
    (_ROUTE, _RECORD.replace("proxied = true", "proxied = false"), None),
    (_ROUTE, _RECORD.replace('"100::"', '"192.0.2.1"').replace('"AAAA"', '"A"'), None),
    (_ROUTE, _RECORD.replace("  ttl     = 1\n", "  ttl     = 1\n  provider = cloudflare.workers\n"), None),
    (_ROUTE.replace("  provider = cloudflare.workers\n", ""), _RECORD, None),
    (_ROUTE.replace('"docs.realliance.net/*"', '"docs.realliance.net/a/*"'), _RECORD, None),
    ("", "", None),
])
def test_shape_checker(workers, records, want):
    files = {"main.tf": _PROVIDERS, "records.tf": records, "workers.tf": workers}
    if want is None:
        with pytest.raises(AssertionError):
            domain_shape(files)
    else:
        assert domain_shape(files) == want


# --- no account id in the module ---------------------------------------------------------------------

ACCOUNT_LINE = re.compile(r'^\s*"?account_id"?\s*[=:]\s*(.*?)\s*(?:#.*)?$', re.M)
ACCOUNT_REF = re.compile(r"data\.cloudflare_zone\.[A-Za-z0-9_]+\.account\.id")


def account_id_problems(files: dict[str, str]) -> list[str]:
    """Every account_id is read from a zone data source; no literal, local or variable carries one."""
    p = []
    for fname, text in files.items():
        for m in ACCOUNT_LINE.finditer(text):
            if not ACCOUNT_REF.fullmatch(m.group(1)):
                p.append(f"{fname}: account_id = {m.group(1)[:12]}…, want data.cloudflare_zone.<zone>.account.id")
        for (name,), _ in blocks(text, "variable"):
            if "account" in name:
                p.append(f'{fname}: variable "{name}": the account id comes from the zone, not a variable')
    if HEX32.search(files.get("workers.tf", "")):
        p.append("workers.tf holds a 32-hex id; read ids through local.zones and the zone data source")
    return p


def test_no_account_id_in_tf():
    assert account_id_problems(module()) == []


@pytest.mark.parametrize("snippet", [
    '  account_id = "' + "0" * 32 + '"\n',
    '  account_id = "x"\n',
    '  "account_id" = "x"\n',
    "  account_id = local.account_id\n",
    "  account_id = var.cloudflare_account\n",
    '  account_id = "${data.cloudflare_zone.realliance_net.account.id}"\n',
    'locals {\n  account_id = "' + "a" * 32 + '"\n}\n',
    'variable "cloudflare_account_id" {\n  type = string\n}\n',
    '# zone ' + "b" * 32 + "\n",
])
def test_account_id_scan_catches(snippet):
    assert account_id_problems({"workers.tf": snippet})


def test_account_id_scan_accepts_the_zone_reference():
    assert account_id_problems({"workers.tf": _ZONE_DATA + _CUSTOM}) == []


# --- the workers provider ----------------------------------------------------------------------------


def test_workers_provider():
    files = module()
    providers = [attrs(body) for _, body in blocks(joined(files), "provider", "cloudflare")]
    assert {} in providers, "the default provider keeps its env auth (CLOUDFLARE_API_TOKEN, the cluster's DNS token)"
    want = [{}, {"alias": '"workers"', "api_token": "var.workers_api_token"}]
    assert sorted(sorted(p.items()) for p in providers) == sorted(sorted(p.items()) for p in want), (
        f"want exactly the default provider and the workers alias; found {providers}")
    main = files["main.tf"]
    assert any(attrs(b).get("alias") == '"workers"' for _, b in blocks(main, "provider", "cloudflare")), (
        "the workers alias belongs in main.tf")
    variables = {name: body for (name,), body in blocks(main, "variable")}
    assert "workers_api_token" in variables, 'variable "workers_api_token" belongs in main.tf'
    v = attrs(variables["workers_api_token"])
    assert v.get("type") == "string" and v.get("sensitive") == "true", v
    assert "default" not in v, "no default: tf.sh supplies the token, and an empty one would fall back to the env token"
    # Every Workers resource goes through the workers token, and nothing else does: it holds no DNS write.
    tf = joined(files)
    for m in re.finditer(r'^resource\s+"([^"]+)"\s+"([^"]+)"\s*\{', tf, re.M):
        prov = attrs(_body(tf, m.end())).get("provider")
        if m.group(1).startswith("cloudflare_workers_"):
            assert prov == WORKERS, f"{m.group(1)}.{m.group(2)} must use provider = {WORKERS}"
        else:
            assert prov != WORKERS, f"{m.group(1)}.{m.group(2)} must stay on the default provider"


# --- tf.sh -------------------------------------------------------------------------------------------


@pytest.fixture
def tf_rig(tmp_path):
    """tf.sh in a scratch repo with fake token files and a stub `terraform` first on PATH.

    The stub prints its args, whether it sees each token, each token's length, and whether the workers
    token equals $STUB_WANT_SHA's preimage. It never prints a token, and touches $STUB_MARK, so a
    refused run provably never reached it.
    """
    root = tmp_path / "repo"
    (root / "cloudflare/dns").mkdir(parents=True)
    (root / "cluster/dns").mkdir(parents=True)
    shutil.copy2(DNS / "tf.sh", root / "cloudflare/dns/tf.sh")
    cluster_tok = f"cluster-{secrets.token_hex(12)}"
    (root / "cluster/dns/cloudflare-api-token.secret.yaml").write_text(
        "apiVersion: v1\nkind: Secret\nmetadata:\n  name: cloudflare-api-token\n"
        f"stringData:\n  api-token: {cluster_tok}\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True, cwd=tmp_path, env=clean_env())
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "terraform").write_text(
        "#!/bin/sh\n"
        'echo "args=$*"\n'
        'if [ -n "${TF_VAR_workers_api_token+x}" ]; then w=set; else w=unset; fi\n'
        'if [ -n "${CLOUDFLARE_API_TOKEN+x}" ]; then c=set; else c=unset; fi\n'
        'sha=$(printf %s "${TF_VAR_workers_api_token:-}" | sha256sum | cut -d" " -f1)\n'
        'if [ "$sha" = "$STUB_WANT_SHA" ]; then match=yes; else match=no; fi\n'
        'echo "workers=$w len=${#TF_VAR_workers_api_token} match=$match"\n'
        'echo "cluster=$c len=${#CLOUDFLARE_API_TOKEN}"\n'
        'echo "stub stderr" >&2\n'
        ': > "$STUB_MARK"\n'
        'exit "${STUB_RC:-0}"\n')
    (bin_ / "terraform").chmod(0o755)
    mark = tmp_path / "ran"
    token = root / TOKEN_FILE

    def run(*args, **extra):
        mark.unlink(missing_ok=True)
        env = clean_env(PATH=f"{bin_}:{os.environ['PATH']}", STUB_MARK=str(mark), **extra)
        env.pop("TF_VAR_workers_api_token", None)
        env.pop("CLOUDFLARE_API_TOKEN", None)
        r = subprocess.run([str(root / "cloudflare/dns/tf.sh"), *args], capture_output=True, text=True,
                           env=env, cwd=tmp_path)
        r.ran = mark.exists()
        return r

    return run, token, cluster_tok


def no_token_shown(r, *tokens: str) -> None:
    for tok in tokens:
        for stream in (r.stdout, r.stderr):
            if tok in stream:
                pytest.fail("a token reached tf.sh's stdout or stderr")


@pytest.mark.parametrize("line", ["api-token={t}\n", "api-token={t}", "api-token={t}\r\n", 'api-token="{t}"\n'],
                         ids=["lf", "no-eol", "crlf", "quoted"])
@pytest.mark.parametrize("rc", [0, 3])
def test_tf_sh_workers_token(tf_rig, line, rc):
    """D11: the workers token reaches terraform as TF_VAR_workers_api_token, never on argv or a stream."""
    run, token, cluster_tok = tf_rig
    fake = f"tok-{secrets.token_hex(16)}"
    token.write_text(line.format(t=fake))
    r = run("plan", "-no-color", STUB_RC=str(rc), STUB_WANT_SHA=hashlib.sha256(fake.encode()).hexdigest())
    no_token_shown(r, fake, cluster_tok)
    assert r.returncode == rc and r.ran, r.stderr
    assert f"workers=set len={len(fake)} match=yes" in r.stdout, r.stdout
    assert f"cluster=set len={len(cluster_tok)}" in r.stdout, "tf.sh still exports the cluster's DNS token"
    assert "args=plan -no-color\n" in r.stdout


@pytest.mark.parametrize("content, why", [
    (None, "missing"),
    (b"\x00GITCRYPT\x00" + b"\x8f\x01" * 24, "encrypted"),
    (b"", "exactly one"),
    (b"api-token=\n", "empty"),
    (b"api-token=  \n", "empty"),
    (b"token=tok-abc\n", "exactly one"),
    (b"api-token=tok-one\napi-token=tok-two\n", "exactly one"),
], ids=["missing", "encrypted", "empty-file", "no-value", "blank-value", "wrong-key", "two-lines"])
def test_tf_sh_refuses_a_bad_token_file(tf_rig, content, why):
    """Missing, encrypted, empty or ambiguous: exit 1, name the file and the reason, never run terraform
    or show a value."""
    run, token, cluster_tok = tf_rig
    if content is not None:
        token.write_bytes(content)
    r = run("plan", STUB_WANT_SHA="x")
    assert r.returncode == 1 and not r.ran, (r.returncode, r.stdout)
    assert TOKEN_FILE in r.stderr and why in r.stderr, r.stderr
    no_token_shown(r, "tok-abc", "tok-one", "tok-two", cluster_tok)


def test_tf_sh_never_traces():
    sh = (DNS / "tf.sh").read_text()
    assert sh.startswith("#!/usr/bin/env bash\n") and os.access(DNS / "tf.sh", os.X_OK)
    assert "set -euo pipefail" in sh
    assert not re.search(r"set -[a-z]*x|xtrace|BASH_XTRACEFD", sh), "a trace would print the tokens"
    assert "-var" not in sh, "tokens travel in the environment, never on argv"


# --- the token file ----------------------------------------------------------------------------------


def test_workers_token_file_is_git_crypted():
    """Checked without opening the file. sign.sh seals only cluster/**/*.secret.yaml, so the cluster never gets it."""
    attr = subprocess.run(["git", "-C", str(REPO), "check-attr", "filter", "--", TOKEN_FILE],
                          capture_output=True, text=True, check=True, env=clean_env()).stdout
    assert attr.strip() == f"{TOKEN_FILE}: filter: git-crypt"
    assert not TOKEN_FILE.startswith("cluster/") and not TOKEN_FILE.endswith(".secret.yaml")


def test_docs_site_hook_wired():
    cfg = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text())
    hooks = {h["id"]: h for repo in cfg["repos"] for h in repo["hooks"]}
    h = hooks["docs-site"]
    assert h["entry"] == "python -m pytest -q -p no:cacheprovider scripts/test_docs_site.py"
    assert h["language"] == "system" and h["pass_filenames"] is False
    for path in HOOK_FILES_MUST_MATCH:
        assert re.search(h["files"], path), f"docs-site must run when {path} changes"
    assert not re.search(h["files"], "cluster/dns/cloudflareddns-config.yaml")
