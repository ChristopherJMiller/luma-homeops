# Luma phase 0a: live auth fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four live security problems in spec §2 (one-click token theft through open DCR, 24 h unrevocable Dex tokens, an unauthenticated gramps-mcp, inconsistent MCP metadata) without taking Chris's Gramps MCP connection offline for longer than one reconnect.

**Architecture:** Dex stays the only token issuer. `images/mcp-jwt-auth` (our Go forward-auth validator) grows into the 0a MCP facade. It checks audiences on every `/gramps/mcp` call, and takes over the OAuth bootstrap from hyprmcp: protected-resource metadata, the RFC 8414 document for Dex's issuer, DCR with an exact redirect allowlist and rate limit, and the authorize scope shim. hyprmcp shrinks to a token-checking reverse proxy. Dex 2.46.0 enforces consent for every client, with remembered consent so each person approves each client once, and names the redirect host because the facade writes that host into the client's display name. gramps-mcp checks the same token itself.

**Tech Stack:** Go 1.25 (go-oidc v3, Dex `api/v2` gRPC, `golang.org/x/time/rate`), Python 3.13 (PyJWT plus cryptography, Starlette ASGI) applied as a flake patch, Dex v2.46.0, Traefik v3 Ingress, Argo CD, Sealed Secrets, git-crypt.

**Spec:** `docs/superpowers/specs/2026-10-18-luma-design.md`, §2 (live security problems), §8.1, §9.1, §9.3 (0a part only), §14 phase 0a, §17 (rows 1 and 2). Phase 2 work (luma-auth, per-user Gramps, mcp-edge v1 replacing hyprmcp) is out of scope.

---

## Findings that shaped this plan (verified 2026-10-18, read-only)

1. **The consent screen has to live in Dex, not the facade.** Dex's `/auth` is public. Anyone holding a `client_id` can send a victim straight to `https://dex.chrismiller.xyz/auth?...` and skip any page the facade shows. A consent page in hyprmcp's `/oauth/authorize`, or in a minimal mcp-edge that still lets Dex issue the code, therefore protects nothing. **Rejected.**
2. **Dex 2.46.0 has no per-client consent skip.** `server/consent/consent.go` `Satisfied()` asks only `ForceApprovalPrompt`, then the global `SkipApproval`, then the stored consents. **Remembered consent also sits behind the experimental `DEX_SESSIONS_ENABLED=true` flag** (`pkg/featureflags/set.go`, default false). `handleApproval` stores consents only `if h.Sessions.Enabled()`. Without the flag, consent shows on *every* interactive login.
3. **Consent mechanism chosen: switch the skip off globally, turn on Dex sessions for remembered consent, and have the facade write the redirect host into each DCR client's `Name`.** Dex's approval template renders `{{ .Client }} would like to:` from `client.Name`, and the facade is the only thing that creates DCR clients, so the host shows with no Dex patch. We rejected a Dex patch (make consent mandatory for `Public` clients) because it would turn the house IdP into an image we build (sharp edge #6). Every Dex security release would then wait on a manual rebuild and re-pin, and 2.46 alone carries a GHSA. The cost of our choice: each person approves each static app once (oauth2-proxy tiers, Immich, Gramps), and we run an experimental flag that a single env var turns off. If the flag is off, consent stays on and shows at every login.
4. **Dex access tokens carry `aud` = the client id**, in both v2.44 (`server/oauth2.go:306`: `newAccessToken` calls `newIDToken`, which sets `aud`) and v2.46 (`server/tokens/issuer.go` `SignAccessToken` calls `SignIDToken`, which uses `GetAudience`). `azp` appears only when there are several audiences. Task 1 still proves this live. Branch B covers the case where it fails.
5. **The 401 that clients see comes from mcp-jwt-auth, not from the gateway.** It is `www-authenticate: Bearer error="invalid_token"`, with no `resource_metadata`. The live PRM says `authorization_servers: ["https://mcp.chrismiller.xyz/"]`, but the AS document on that host says `issuer: "https://dex.chrismiller.xyz"`.
6. **hyprmcp's DCR rate limit does nothing here.** It uses `httprate.LimitByRealIP(3, 10m)`, which trusts `True-Client-IP`/`X-Real-IP` (spoofable). Traefik's Service is also `externalTrafficPolicy: Cluster`, so Traefik never sees real client IPs anyway. The facade uses a **global** limiter instead. hyprmcp's DCR also accepts any `redirect_uris`, and writes its 201 before it sets `Content-Type`.
7. **Only MCP clients use Dex tokens after login.** oauth2-proxy has no `--cookie-refresh`, so its 168 h cookie never consults the ID-token expiry (`pkg/middleware/stored_session.go` v7.13.0). Immich and Gramps exchange the code once and then run their own sessions. The refresh that matters is the MCP clients' `refresh_token` grant, which needs `offline_access`. Dex's Google connector then sends Google `prompt=consent`.
8. Live DCR clients: `7QITXD4SBM2ZFXDP5TLPRJGZZ3` ("ChatGPT", 2026-09-28, `https://chatgpt.com/connector/oauth/G_DaC-OGWUOC`) and `M4HNWEXI6EFRW7EXBJFYENJVL2` ("Claude Code (gramps)", 2026-09-27, `http://localhost:64336/callback`). Claude Code uses `http://localhost:<random port>/callback`, and the port can be pinned with `--callback-port`.
9. In the pinned nixpkgs, mcp is 1.29.0 and pyjwt is 2.13.0, **with no `cryptography` in the closure**, so PyJWT cannot verify RS256. `gramps-mcp-image.yml` does not trigger on `patches/**`.
10. `.renovaterc.json` extends `:automergeMinor` and has no Dex rule, so a Dex minor bump would auto-merge, experimental flag and all.

## Global Constraints

- Dex target: `ghcr.io/dexidp/dex:v2.46.0`. Index digest on 2026-10-18 was `sha256:933fcd3f523338c847b88ef84a7145fb2fcb7b9917f08418612afeaaa2001777`; re-resolve it before pinning.
- Expiry: "about 1 h for ID and access tokens, with rotating refresh tokens and an idle limit". In Dex, `expiry.idTokens` governs both kinds of token.
- "dynamically registered clients **must** show a consent screen that names the redirect host. Static first-party clients may skip it."
- DCR: "**exact redirect URIs** from an allowlist (claude.ai, chatgpt.com and Gemini callback URLs). Loopback only if Chris needs it for Claude Code. Rate-limited." He does need it (finding 8).
- Audience: "`aud` or `azp` must be a facade-created client (existing ids grandfathered; new ones prefixed `mcp-`)". "Tokens without a verifiable audience are rejected; they never fall back to the allowlist alone." The family allowlist check stays.
- Metadata: "the 401 carries `resource_metadata`", and PRM `authorization_servers` equals the AS metadata `issuer`. The rule used here: **PRM names whoever signs the bearer tokens the resource accepts.** That is Dex on Path A and the facade on Branch B.
- gramps-mcp: "ASGI middleware validates the bearer (401 otherwise) … validating Dex JWKS: issuer, expiry, `aud`/`azp` ∈ facade clients, plus the allowlist", still on the shared Gramps account.
- "**Never use Dex as an STS.**" Never switch the Microsoft connector to generic OIDC. Never give a static Dex client an id starting `mcp-`.
- Repo rules: S2 (change git, never live state; `kubectl rollout restart` is allowed); S4 (ask Chris before deleting any OAuth2Client, Secret or Certificate); S6 (never `--no-verify` or `--no-gpg-sign`; signing is off, so no `-S`); S9 (never print values from `cluster/dex/config.secret.yaml`; use `git diff --no-textconv` for anything that leaves the terminal).
- Images we build (sharp edge #6): CI publishes `:sha-<short>`. Pinning `:sha-<short>@sha256:…` in the Deployment is a **second, manual commit**. Select the tag that matches the commit you pushed, not `sort | tail -1`.
- Workflow: commit straight to `main` with `nix develop -c git commit -m …`, then check `gh api user --jq .login` is `ChristopherJMiller` before `git push origin main`. Toolchain commands go through `nix develop -c …` with `KUBECONFIG=/tmp/galaxy-kubeconfig`. Waits longer than 60 s use `Monitor`, never a sleep loop. Don't merge while Ceph is HEALTH_ERR.
- Edge: one Ingress per host and path, no per-host cert, no external-dns or cert-manager annotation. Run `edge-snapshot.sh` before and after every edge change.

## Review Focus

1. **Redirect-URI look-alikes** (userinfo `@`, suffix hosts, case, trailing dot, query, fragment, trailing slash, percent-encoding, `[::1]`, ports 0/80/65536, `..`) must be refused at registration, so a code is never delivered anywhere but an allowlisted origin. Pinned by Task 5 `TestMatchRedirect_LookAlikes`.
2. **A hostile `client_name`** (bidi overrides, newlines, 10k characters, a fake host in the name) must never move the real redirect host off the front of the consent text. Pinned by Task 5 `TestConsentName_Hostile`.
3. **A multi-audience token** that hides a facade client among its audiences (`aud=[immich, mcp-x]`, `azp=immich`) is a token *for immich* and must be rejected, at the edge and at gramps-mcp. Pinned by Task 2 `TestAuthorizeBearer` row `multi-aud-hides-facade` and Task 3 `test_multi_aud_hiding_facade_client_is_401`.
4. **A registration storm** (Claude Code's random-port re-registration, reconnect loops) must return 429 with `Retry-After`, never a 5xx. Hitting the client cap must return a clear 403. Pinned by Task 5 `TestRegister_RateLimited` and `TestRegister_CapReached`.
5. **Edge/gramps-mcp disagreement at the edge of expiry.** A token that mcp-jwt-auth accepted a moment ago must not 401 at gramps-mcp, so gramps-mcp allows 60 s of leeway. Pinned by Task 3 `test_expired_within_leeway_passes` and `test_expired_beyond_leeway_is_401`.

---

## File map

| Path | Change | Responsibility |
|---|---|---|
| `.claude/skills/edge-ingress/edge-snapshot.sh`, `SKILL.md` | modify | add MCP and OAuth path probes to the regression snapshot |
| `tools/mcp-probe/mcp_probe.py`, `tools/mcp-probe/tests/test_mcp_probe.py` | create | stdlib OAuth/MCP probe used by every live verification |
| `images/mcp-jwt-auth/{config,allowlist,verify}.go` (+ `_test`) | create (split from `main.go`) | config, allowlist, bearer verification and challenges |
| `images/mcp-jwt-auth/{redirects,register,oauth_authorize,metadata}.go` (+ `_test`) | create | facade: redirect rules, DCR, authorize shim, PRM and AS metadata |
| `images/mcp-jwt-auth/main.go`, `Dockerfile`, `go.mod`, `go.sum` | modify | wiring; `COPY *.go`; `go test` in the build |
| `patches/gramps-mcp-inbound-auth.patch` | create | adds `src/gramps_mcp/inbound_auth.py` and `tests/test_inbound_auth.py`; wraps the HTTP app |
| `flake.nix` | modify | patch list, cryptography on `PYTHONPATH`, `packages.gramps-mcp-src`, `checks.*.gramps-mcp-inbound-auth` |
| `.github/workflows/gramps-mcp-image.yml` | modify | trigger on `patches/**`, run the check, smoke-test 401 and fail-closed start |
| `cluster/oauth2-proxy/mcp-jwt-auth.yaml`, `kustomization.yaml`, `mcp-redirects.rules`, `mcp-jwt-auth-dex-client.yaml` | modify/create | env, pins, gRPC cert, redirect rules ConfigMap, two Exact-path Ingresses |
| `cluster/mcp-gateway/{config/config.yaml,deployment.yaml,kustomization.yaml}`, `dex-client-cert.yaml` | modify/delete | strip hyprmcp to a proxy |
| `cluster/gramps/mcp.yaml` | modify | inbound-auth env, allowlist mount, pin |
| `cluster/dex/dex.yaml`, `config.secret.yaml` → `config.yaml` | modify/reseal | v2.46.0, flags, consent, sessions, grant types, expiry |
| `.renovaterc.json`, `.pre-commit-config.yaml` | modify | Dex no-automerge rule; refuse static `mcp-*` Dex client ids |
| `docs/auth.md`, `docs/gramps.md`, `CLAUDE.md` | modify | runbooks, folded into each task |

---

### Task 0: Preflight and the regression baseline

**Files:**
- Modify: `.claude/skills/edge-ingress/edge-snapshot.sh` (append a second loop after the host loop)
- Modify: `.claude/skills/edge-ingress/SKILL.md` (one sentence under "Regression test": the MCP block exists, and its bodies must be read, not just diffed)

**Interfaces:**
- Produces: `edge-snapshot.sh` output now ends with one line per MCP path, `<host><path> <status> loc=<url> [<www-authenticate>] body=<compact JSON for /.well-known/*>`. Later tasks diff against `$SCRATCH/edge-0.txt`.

- [ ] **Step 1: Cluster gate.** Run the `cluster-snapshot` skill. Ceph must show its usual HEALTH_WARN noise or better (if it shows HEALTH_ERR, stop and ask Chris). Argo `dex`, `oauth2-proxy`, `mcp-gateway`, `gramps`, `traefik` must be Synced/Healthy.
- [ ] **Step 2: Record the DCR clients, without secrets.**
  `nix develop -c kubectl -n dex get oauth2clients -o json | jq -r '.items[] | [.metadata.name,.id,.name,.public,(.redirectURIs|join(","))] | @tsv'`
  Expected: the two ids in finding 8. Write the ids into the task notes as `GRANDFATHERED`. If there are more than two, show them to Chris before continuing.
- [ ] **Step 3: Extend the snapshot.** Append a `while read -r fq path` loop over this heredoc: `mcp.chrismiller.xyz /gramps/mcp`, `… /.well-known/oauth-protected-resource/gramps/mcp`, `… /.well-known/oauth-authorization-server`, `dex.chrismiller.xyz /.well-known/oauth-authorization-server`, `dex.chrismiller.xyz /.well-known/openid-configuration`, `mcp.chrismiller.xyz /oauth/register`, `mcp.chrismiller.xyz /oauth/authorize`. Each pair is curled via `--resolve "$fq:443:192.168.0.7"`. Print the status and redirect (with `state=` stripped), the full `www-authenticate` header (no `cut`), and for `/.well-known/*` the body with `tr -d '\n' | head -c 900`. Use curl and coreutils only, so `nix-shell -p openssl --run` still works.
- [ ] **Step 4: Capture the baseline.** `cd .claude/skills/edge-ingress && nix-shell -p openssl --run ./edge-snapshot.sh > $SCRATCH/edge-0.txt`. Expected MCP lines: `/gramps/mcp` 401 with `Bearer error="invalid_token"`; PRM 200 with `"authorization_servers":["https://mcp.chrismiller.xyz/"]`; the mcp AS document 200 with `"issuer":"https://dex.chrismiller.xyz"`; the dex AS document 404; register (GET) 400; authorize 302 to `https://dex.chrismiller.xyz/auth?scope=openid+profile+email`.
- [ ] **Step 5: Commit.** `git add .claude/skills/edge-ingress/ && nix develop -c git commit -m "edge-snapshot: cover the MCP OAuth bootstrap paths"`. Check the gh user, then push.

---

### Task 1: `mcp-probe`, and the audience spike (decides Path A or Branch B)

**Files:**
- Create: `tools/mcp-probe/mcp_probe.py` (stdlib only)
- Create: `tools/mcp-probe/tests/test_mcp_probe.py`

**Interfaces:**
- Produces this CLI, used by Tasks 2–9: `python3 tools/mcp-probe/mcp_probe.py <cmd>` with `--resource` defaulting to `https://mcp.chrismiller.xyz/gramps/mcp`.
  - `discover`: prints JSON `{resource_metadata, authorization_server, issuer, registration_endpoint, authorization_endpoint, token_endpoint, agree}` and exits 1 when `issuer != authorization_servers[0]` (exact string compare).
  - `register --name N (--port P | --redirect URI)`: prints the HTTP status and the response JSON; exits 1 on anything but 201.
  - `login --client-id ID --port P [--authorize-url URL]`: PKCE S256 with `scope=openid email profile offline_access`. It prints the authorize URL, captures the code on `http://localhost:P/callback`, and exchanges it at `token_endpoint`. The tokens are stored at `${XDG_RUNTIME_DIR:-~/.cache}/mcp-probe/<ID>.json` (dir 0700, file 0600). It prints `claims_summary`. `--authorize-url https://dex.chrismiller.xyz/auth` skips the discovered endpoint; after Task 6 that is the only way to log in a non-facade client such as `SPIKE_ID`, because the facade's shim refuses it.
  - `call --client-id ID | --no-token`: POSTs JSON-RPC `initialize` to the resource and prints the status and `www-authenticate`.
  - `refresh --client-id ID`: runs the refresh grant, stores the result, and prints `{lifetime_s, refresh_rotated}`.
  - Pure functions: `pkce_pair() -> tuple[str, str]`, `parse_bearer_challenge(h: str) -> dict[str, str]`, `as_metadata_urls(issuer: str) -> list[str]`, `claims_summary(jwt: str, has_refresh: bool) -> dict`.
  - Never prints a token, an email or a refresh token.

- [ ] **Step 1: Write the failing tests.**

```python
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
```

- [ ] **Step 2: Run them and watch them fail.** `nix develop -c python -m pytest -q tools/mcp-probe`. Expected: ImportError.
- [ ] **Step 3: Implement `mcp_probe.py`** using `urllib.request`, `http.server` (one-shot loopback handler), `secrets`, `hashlib` and `json`. `discover` follows MCP 2025-11-25 order: an unauthenticated POST, then `resource_metadata` from the challenge (falling back to the path-insert PRM URL), then the PRM, then `as_metadata_urls(authorization_servers[0])` with the first 200 winning.
- [ ] **Step 4: Run the tests and watch them pass.** Same command. Expected: 4 passed.
- [ ] **Step 5: Commit.** `nix develop -c git commit -m "tools: mcp-probe, a stdlib OAuth/MCP probe for the 0a verifications"`, then push.
- [ ] **Step 6: Reproduce live problem 4.** `nix develop -c python3 tools/mcp-probe/mcp_probe.py discover`. Expected: exit 1, `authorization_server: "https://mcp.chrismiller.xyz/"`, `issuer: "https://dex.chrismiller.xyz"`.
- [ ] **Step 7: The spike (Chris completes the browser step).** `register --name mcp-probe-spike --port 8976` should return 201 with a 26-character id and **no** `mcp-` prefix (hyprmcp generated it); record it as `SPIKE_ID`. Then `login --client-id $SPIKE_ID --port 8976`.
- [ ] **Step 8: Decide.** If `aud == SPIKE_ID` (a string or a one-element list), or `aud` has several entries with `azp == SPIKE_ID`, take **Path A** and continue with Task 2. Otherwise take **Branch B** (below). Expected: Path A. Also record `has_refresh_token`; if it is false, hyprmcp's shim dropped `offline_access`, and Task 5's shim fixes that.
- [ ] **Step 9: Reproduce live problem 1.** `call --client-id $SPIKE_ID` should return 200: any new DCR client's token opens the tree today. Keep the token file, because Task 2 reuses it as "a token for another client".

---

### Branch B (only if Task 1 Step 8 fails): minimal mcp-edge re-minting audience-bound tokens

Branch B replaces the audience rule in Tasks 2 and 3 and the metadata in Tasks 5 and 6. Everything else (consent, DCR allowlist, expiry) is unchanged. Order: B1, then B2 (deployed with Task 6), then Chris reconnects, then B3.

- **B1, token endpoint (`images/mcp-jwt-auth/token.go`):**
  - `newTokenHandler(dexTokenURL string, idv *oidc.IDTokenVerifier, signer jose.Signer, cfg config) http.Handler` serves `POST /oauth/token` for the `authorization_code` and `refresh_token` grants. It forwards the form unchanged to Dex's in-cluster `/token`.
  - It verifies the returned `id_token` (its audience is a facade client and its email is on the allowlist), then mints an ES256 JWT: `iss=https://mcp.chrismiller.xyz`, `aud=https://mcp.chrismiller.xyz/gramps/mcp`, `sub`, `email`, `client_id`, `jti`, `exp=now+1h`.
  - It returns `{access_token: <minted>, token_type: "Bearer", expires_in: 3600, refresh_token: <Dex's, unchanged>, scope}` and never returns Dex's access or ID token. `GET /oauth/jwks` publishes the public key.
  - The key lives in `cluster/oauth2-proxy/mcp-edge-signing.secret.yaml`, sealed with `./sign.sh`.
  - Tests: `TestToken_ReMintsAudienceBoundToken`, `TestToken_RejectsNonFacadeIDToken`, `TestToken_RefreshTokenPassesThrough`, `TestToken_NeverReturnsDexTokens`.
- **B2, metadata:** the AS document is served on **mcp.chrismiller.xyz** with `issuer: "https://mcp.chrismiller.xyz"`, `token_endpoint` and `jwks_uri` on the facade, and PRM `authorization_servers: ["https://mcp.chrismiller.xyz"]`. There is no dex-host Ingress. Test: `TestAgreement` with the facade as issuer.
- **B3, resource rule:** mcp-jwt-auth and gramps-mcp verify against the facade JWKS with `iss=https://mcp.chrismiller.xyz` and `aud == https://mcp.chrismiller.xyz/gramps/mcp`, plus the allowlist. They reject every Dex token. hyprmcp `proxy[0].authentication.enabled: false`, because its signature check uses Dex's JWKS. The test tables in Tasks 2 and 3 swap the client-id rows for resource-audience rows.

---

### Task 2: mcp-jwt-auth requires a facade audience and points 401s at the resource metadata

**Files:**
- Create: `images/mcp-jwt-auth/config.go`, `config_test.go`, `allowlist.go` (moved unchanged from `main.go`), `verify.go`, `verify_test.go`
- Modify: `images/mcp-jwt-auth/main.go` (wiring only), `Dockerfile` (`COPY *.go ./`; `RUN CGO_ENABLED=0 go test ./...` before `go build`)
- Modify, in a second commit: `cluster/oauth2-proxy/mcp-jwt-auth.yaml` (pin and env)
- Modify: `docs/gramps.md` (the "Why this needs three components" paragraphs and Commands)

**Interfaces:**
- Produces (package `main`):
  - `type config struct{ Issuer, EmailsFile, Addr, PublicBaseURL, ClientPrefix string; Resources []string; Grandfathered map[string]struct{} }` plus the Task 5 fields.
  - `func loadConfig(lookup func(string) (string, bool)) (config, error)`. Env: `OIDC_ISSUER` (required, error text `OIDC_ISSUER is required`, which CI greps); `EMAILS_FILE`; `ADDR`; `PUBLIC_BASE_URL` (required; one trailing `/` is trimmed); `MCP_RESOURCES` (comma list, default `/gramps/mcp`); `CLIENT_ID_PREFIX` (default `mcp-`; **set but empty is an error**); `GRANDFATHERED_CLIENT_IDS` (comma list, may be empty).
  - `func (c config) isFacadeClient(id string) bool` is true iff `id` is grandfathered, or `strings.HasPrefix(id, c.ClientPrefix) && len(id) > len(c.ClientPrefix)`. It is case-sensitive.
  - `func clientIDOf(aud []string, azp string) (string, bool)` mirrors Dex `GetClientID`: one audience gives `aud[0]`; several give `azp`, which must be non-empty; none gives false.
  - `type identity struct{ Email, ClientID string }`; `var errNoToken, errInvalidToken, errWrongAudience, errNotAllowlisted error`.
  - `func authorizeBearer(ctx context.Context, v *oidc.IDTokenVerifier, cfg config, list *allowlist, header string) (identity, error)`. The verifier keeps `SkipClientIDCheck: true`; the audience rule replaces it.
  - `func (c config) resourceMetadataURL(resource string) string` returns `PublicBaseURL + "/.well-known/oauth-protected-resource" + resource`.
  - `func challenge(cfg config, forwardedURI string, err error) (status int, wwwAuthenticate string)`.

- [ ] **Step 1: Write the failing tests.** Tokens are signed in-test with go-jose. The verifier is `oidc.NewVerifier(iss, &oidc.StaticKeySet{PublicKeys: …}, &oidc.Config{SkipClientIDCheck: true})`.

```go
// TestClientIDOf
{[]string{"mcp-a"}, "", "mcp-a", true}, {[]string{"immich"}, "", "immich", true},
{[]string{"immich", "mcp-x"}, "immich", "immich", true}, {nil, "", "", false},
{[]string{"a", "b"}, "", "", false},
// TestIsFacadeClient (prefix "mcp-", grandfathered M4HNWEXI6EFRW7EXBJFYENJVL2)
"mcp-ABC"→true, "M4HNWEXI6EFRW7EXBJFYENJVL2"→true, "immich"→false,
"oauth2-proxy-family"→false, "MCP-abc"→false, "mcp-"→false
// TestAuthorizeBearer: name → want
"facade-allowlisted"→identity{Email, "mcp-a"}, "grandfathered"→ok,
"aud-immich"→errWrongAudience, "multi-aud-hides-facade" (aud [immich mcp-x], azp immich)→errWrongAudience,
"expired"→errInvalidToken, "wrong-issuer"→errInvalidToken, "bad-signature"→errInvalidToken,
"not-allowlisted"→errNotAllowlisted, "email-unverified"→errNotAllowlisted,
"empty-allowlist"→errNotAllowlisted, "no-header"→errNoToken, "bearer-empty"→errNoToken
// TestChallenge (base https://mcp.chrismiller.xyz, resources [/gramps/mcp])
errNoToken, "/gramps/mcp?x=1" → 401, `Bearer resource_metadata="https://mcp.chrismiller.xyz/.well-known/oauth-protected-resource/gramps/mcp", scope="openid email profile offline_access"`
errWrongAudience, "/gramps/mcp" → 401, `Bearer error="invalid_token", resource_metadata="…/gramps/mcp"`
errNotAllowlisted → 403, ""          errInvalidToken, "/other" → 401, `Bearer error="invalid_token"`
// TestLoadConfig: no OIDC_ISSUER → "OIDC_ISSUER is required"; CLIENT_ID_PREFIX="" → error;
// PUBLIC_BASE_URL "https://mcp.chrismiller.xyz/" → "https://mcp.chrismiller.xyz"
```

- [ ] **Step 2: Run the tests and watch them fail.** `nix develop -c sh -c 'cd images/mcp-jwt-auth && go test ./...'`. Expected: undefined symbols.
- [ ] **Step 3: Implement.** `/auth` calls `authorizeBearer`. On success it returns 202 with `X-Auth-Request-Email`. On error it writes `challenge(...)` (reading `X-Forwarded-Uri` only to pick among the configured resources) and a JSON `{"error": …}`. Log the reason and the client id, never the token.
- [ ] **Step 4: Run the tests and watch them pass.** Same command.
- [ ] **Step 5: Commit and publish.** `nix develop -c git commit -m "mcp-jwt-auth: require a facade-created audience; 401s carry resource_metadata"`, then push. Wait for `mcp-jwt-auth image` with `Monitor` (`gh run watch`). Resolve the digest of `sha-$(git rev-parse --short HEAD)`: get an anonymous GHCR token from `https://ghcr.io/token?scope=repository:christopherjmiller/mcp-jwt-auth:pull`, then `curl -sI` the manifest with `Accept: application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json` and read `docker-content-digest`.
- [ ] **Step 6: Pin and configure (second commit).** In `mcp-jwt-auth.yaml`, set `image: …:sha-<short>@sha256:<digest>` and add env `PUBLIC_BASE_URL=https://mcp.chrismiller.xyz`, `MCP_RESOURCES=/gramps/mcp`, `CLIENT_ID_PREFIX=mcp-`, `GRANDFATHERED_CLIENT_IDS=<Task 0 ids, comma-joined>`. Fix the header comment ("Audience is deliberately NOT checked" is no longer true). `nix develop -c git commit -m "mcp: pin mcp-jwt-auth sha-<short> (audience rule)"`, then push. Watch the rollout with `Monitor` until 2/2 Ready.
- [ ] **Step 7: Verify.**
  - `call --no-token` returns 401 and its challenge contains `resource_metadata=`.
  - `call --client-id $SPIKE_ID` returns **401 `invalid_token`** (exit criterion "a token for another client is rejected"). The spike token lives 24 h. If Task 1 was more than a day ago, run `refresh` (or `login` again) first, so the 401 is about the audience and not about expiry.
  - Chris's Claude Code runs one Gramps read tool and it works (grandfathered).
  - The logs show `denied … client_id=<SPIKE_ID>`.
  - Re-run the snapshot to `edge-2.txt` and `diff` it against `edge-0.txt`. The only change allowed is the `/gramps/mcp` challenge line.
  - Note: from here until Task 6, new hyprmcp registrations cannot be used (their ids are not facade ids). That is the intended freeze.
- [ ] **Step 8: Docs.** In `docs/gramps.md`, replace "Audience is deliberately unchecked" with the facade-client rule (grandfathered ids, the `mcp-` prefix, the azp rule) and add `tools/mcp-probe` to Commands. Commit `docs: MCP audience rule`.

---

### Task 3: gramps-mcp authenticates its own callers

**Files:**
- Create: `patches/gramps-mcp-inbound-auth.patch`. It adds `src/gramps_mcp/inbound_auth.py` and `tests/test_inbound_auth.py`, and in `src/gramps_mcp/server.py` replaces the HTTP branch's `app.run(transport="streamable-http")` with `uvicorn.run(InboundAuth(app.streamable_http_app(), InboundAuthConfig.from_env(os.environ)), host="0.0.0.0", port=8000)`. Generate it against the tree with every current patch applied, transport-security included.
- Modify: `flake.nix`:
  - append `./patches/gramps-mcp-inbound-auth.patch` after the transport-security patch;
  - in the same `overrideAttrs`, add `postInstall = (old.postInstall or "") + ''wrapProgram "$out/bin/gramps-mcp" --prefix PYTHONPATH : ${pkgs.python3Packages.makePythonPath [ pkgs.python3Packages.cryptography ]}'';`;
  - add `packages.gramps-mcp-src = pkgs.applyPatches { name = "gramps-mcp-src"; src = gramps-mcp.src; patches = gramps-mcp.patches; }`;
  - add `checks = forAllSystems (pkgs: { gramps-mcp-inbound-auth = pkgs.runCommand … })`, which runs `python -m pytest -q -p no:cacheprovider tests/test_inbound_auth.py` inside `gramps-mcp-src` with `python3.withPackages (ps: [ps.pytest ps.pyjwt ps.cryptography ps.starlette ps.httpx ps.anyio])`.
- Modify: `.github/workflows/gramps-mcp-image.yml`:
  - add `patches/**` to both `paths` lists;
  - before the image build, add a step `nix build .#checks.x86_64-linux.gramps-mcp-inbound-auth -L`;
  - change the probe to run with `-e MCP_OIDC_ISSUER=https://dex.chrismiller.xyz -e MCP_EMAILS_FILE=/dev/null -e MCP_RESOURCE_METADATA_URL=https://mcp.chrismiller.xyz/.well-known/oauth-protected-resource/gramps/mcp`, and assert that `POST /mcp` returns `401` with a `www-authenticate:` line containing `resource_metadata=`;
  - add a second run without `MCP_OIDC_ISSUER` that must exit non-zero, with `MCP_OIDC_ISSUER is required` in its log.
- Modify, in a second commit: `cluster/gramps/mcp.yaml` (env, allowlist volume, header comment, pin)
- Modify: `docs/gramps.md` (MCP section, gotchas)

**Interfaces:**
- Consumes: the Task 2 rule (identical semantics, restated here because the code is Python).
- Produces (`inbound_auth.py`, no package-relative imports, so tests import it via `sys.path.insert(0, "src/gramps_mcp")`):
  - `@dataclass(frozen=True) class InboundAuthConfig: issuer: str; jwks_url: str; emails_file: str; client_prefix: str; grandfathered: frozenset[str]; resource_metadata_url: str | None; leeway_s: int`.
  - `InboundAuthConfig.from_env(env: Mapping[str, str]) -> InboundAuthConfig` reads `MCP_OIDC_ISSUER` (required; otherwise `SystemExit("MCP_OIDC_ISSUER is required")`), `MCP_OIDC_JWKS_URL` (default `<issuer>/keys`), `MCP_EMAILS_FILE` (default `/etc/gramps-mcp/emails`), `MCP_CLIENT_ID_PREFIX` (default `mcp-`; empty means SystemExit), `MCP_GRANDFATHERED_CLIENT_IDS`, `MCP_RESOURCE_METADATA_URL` and `MCP_TOKEN_LEEWAY_SECONDS` (default 60).
  - `def client_id_of(claims: Mapping) -> str | None`.
  - `class Allowlist(path: str, ttl_s: float = 30)` with `.permits(email: str) -> bool`. It re-reads when stale; an unreadable or empty file denies everyone.
  - `class InboundAuth(app, config, key_resolver: Callable[[str], Any] | None = None, clock: Callable[[], float] = time.time)`, an ASGI callable.
    - `lifespan` and other non-HTTP scopes pass straight through.
    - HTTP requests need `Authorization: Bearer`. A missing or invalid token gets 401 `{"error":"invalid_token"}`, with `WWW-Authenticate: Bearer error="invalid_token"` plus `, resource_metadata="<url>"` when configured. A valid token whose subject is not permitted gets 403 `{"error":"forbidden"}`.
    - Verification uses `jwt.decode(..., algorithms=["RS256","ES256"], issuer=..., leeway=leeway_s, options={"require": ["exp","iat","iss","aud"], "verify_aud": False})`, followed by the facade rule and the allowlist. It runs in `anyio.to_thread.run_sync`.
    - The default `key_resolver` is `jwt.PyJWKClient(jwks_url).get_signing_key_from_jwt(t).key`.
    - At import, the module raises `RuntimeError` unless `jwt.algorithms.has_crypto` is true, so a missing `cryptography` fails closed at start.

- [ ] **Step 1: Get the base tree.** `nix build .#gramps-mcp-src -o $SCRATCH/gms-base` (add the output first, before the patch is in the list), then `cp -r` it to `$SCRATCH/a` and `$SCRATCH/b`.
- [ ] **Step 2: Write the failing tests** in `$SCRATCH/b/tests/test_inbound_auth.py`. Use an in-test RSA key, a `key_resolver` returning its public key, a fake clock, and `starlette.testclient.TestClient` around an inner app that returns 200.

```python
def test_missing_bearer_is_401_with_resource_metadata():
    r = client().post("/mcp", json={})
    assert r.status_code == 401
    assert 'resource_metadata="https://mcp.chrismiller.xyz/.well-known/oauth-protected-resource/gramps/mcp"' in r.headers["www-authenticate"]
def test_facade_client_passes():          assert call(aud="mcp-a").status_code == 200
def test_grandfathered_client_passes():   assert call(aud="M4HNWEXI6EFRW7EXBJFYENJVL2").status_code == 200
def test_other_client_is_401():           assert call(aud="immich").status_code == 401
def test_multi_aud_hiding_facade_client_is_401(): assert call(aud=["immich", "mcp-x"], azp="immich").status_code == 401
def test_wrong_issuer_is_401():           assert call(iss="https://evil.example").status_code == 401
def test_expired_within_leeway_passes():  assert call(exp=NOW - 30).status_code == 200
def test_expired_beyond_leeway_is_401():  assert call(exp=NOW - 61).status_code == 401
def test_not_allowlisted_is_403():        assert call(email="stranger@example.com").status_code == 403
def test_email_unverified_is_403():       assert call(email_verified=False).status_code == 403
def test_empty_allowlist_denies():        assert call(allowlist="").status_code == 403
def test_lifespan_passes_through():       # TestClient context manager: inner lifespan startup ran
def test_from_env_requires_issuer():      pytest.raises(SystemExit, InboundAuthConfig.from_env, {})
def test_empty_prefix_refused():          pytest.raises(SystemExit, InboundAuthConfig.from_env, {"MCP_OIDC_ISSUER": "x", "MCP_CLIENT_ID_PREFIX": ""})
```

- [ ] **Step 3: Generate a test-only patch and confirm it fails.** `cd $SCRATCH && diff -ruN a b > ~/Repos/luma-homeops/patches/gramps-mcp-inbound-auth.patch` (paths `a/…`, `b/…`, applied with `-p1`). Add it to `flake.nix` and `git add` both (flake paths must be tracked). Run `nix build .#checks.x86_64-linux.gramps-mcp-inbound-auth -L`. Expected: FAIL, `ModuleNotFoundError: inbound_auth`.
- [ ] **Step 4: Implement** `b/src/gramps_mcp/inbound_auth.py` and the `server.py` wiring, regenerate the patch, add the `postInstall` wrap, and rebuild the check. Expected: PASS, 14 tests.
- [ ] **Step 5: Smoke-test locally.** `nix build .#gramps-mcp && MCP_OIDC_ISSUER=https://dex.chrismiller.xyz MCP_EMAILS_FILE=/dev/null ./result/bin/gramps-mcp &`. Then `curl -si -X POST localhost:8000/mcp -d '{}'` should show `HTTP/1.1 401`. Without `MCP_OIDC_ISSUER`, it should exit non-zero with the message. Kill the server afterwards.
- [ ] **Step 6: Commit and publish.** `git add patches/gramps-mcp-inbound-auth.patch flake.nix .github/workflows/gramps-mcp-image.yml && nix develop -c git commit -m "gramps-mcp: validate the bearer itself (Dex JWKS, facade audience, family allowlist)"`, then push. Wait for the workflow with `Monitor`. The digest is the last line of its "Tag and push" step.
- [ ] **Step 7: Pin and configure (second commit).** In `cluster/gramps/mcp.yaml`:
  - set the image to `:sha-<short>@sha256:<digest>`;
  - add env `MCP_OIDC_ISSUER=https://dex.chrismiller.xyz`, `MCP_OIDC_JWKS_URL=http://dex.dex.svc.cluster.local:5556/keys`, `MCP_EMAILS_FILE=/etc/gramps-mcp/emails`, `MCP_CLIENT_ID_PREFIX=mcp-`, `MCP_GRANDFATHERED_CLIENT_IDS=<same as Task 2>`, `MCP_RESOURCE_METADATA_URL=https://mcp.chrismiller.xyz/.well-known/oauth-protected-resource/gramps/mcp`;
  - add volume `secret: gramps-new-user-emails`, `items: [{key: emails, path: emails}]`, mounted at `/etc/gramps-mcp` **without `subPath`** (the generated mirror of the one family list);
  - rewrite the header comment ("no inbound auth of its own" is no longer true).
  Commit `gramps: pin gramps-mcp sha-<short> (inbound auth)`, push, and watch the rollout with `Monitor`.
- [ ] **Step 8: Verify.**
  - The in-cluster POST without a token must print `401` (exit criterion): `nix develop -c kubectl -n gramps run mcp-unauth-probe --rm -i --restart=Never --image=docker.io/curlimages/curl:8.22.0@sha256:58adaa4e8dca9c988bae2aba4ab3434a0bb2da16bbe3f92dec39ec7785166777 -- -s -o /dev/null -w '%{http_code}\n' -X POST http://gramps-mcp.gramps.svc.cluster.local:8000/mcp -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'`.
  - A Claude Code Gramps tool call works through the edge, which proves gramps-mcp accepts what the edge accepts.
  - `kubectl -n gramps logs deploy/gramps-mcp --since=10m | grep -c ' 401 '` shows no 401s for edge traffic.
  - The snapshot diff against `edge-2.txt` is empty.
- [ ] **Step 9: Docs.** `docs/gramps.md`:
  - the MCP section: there are now four checks (the gateway's signature check, mcp-jwt-auth, gramps-mcp, the Gramps role), and "anything in-cluster can read it" is gone;
  - the second patch kept in this repo, and why `cryptography` is wrapped onto `PYTHONPATH`;
  - a gotchas row: "gramps-mcp 401 while the edge accepts → the two grandfathered lists drifted".
  Commit.

---

### Task 4: Dex v2.44.0 → v2.46.0 (image only)

**Files:**
- Modify: `cluster/dex/dex.yaml` (image `ghcr.io/dexidp/dex:v2.46.0@sha256:<re-resolved>`; env `DEX_CONFIG_DISALLOW_UNKNOWN_FIELDS: "true"` with a comment explaining that a typo now fails the new pod while RollingUpdate with maxUnavailable 0 keeps the old ones serving)
- Modify: `.renovaterc.json` (a packageRule `matchManagers: ["kubernetes"]`, `matchPackageNames: ["ghcr.io/dexidp/dex"]`, `automerge: false`, with a description: the house IdP; read the release notes for sessions/consent changes, since `DEX_SESSIONS_ENABLED` is experimental)
- Modify: `docs/auth.md` ("Multi-provider: Dex": version, the strict-config flag, the Renovate rule)

**Interfaces:**
- Produces: Dex 2.46 running with the unchanged config. The CRDs `useridentities.dex.coreos.com` and `authsessions.dex.coreos.com` exist (Dex creates them itself; `crdHandling` defaults to `ensure`).

- [ ] **Step 1: Re-resolve the digest.** Use the anonymous GHCR token for `dexidp/dex` with an index Accept header on `manifests/v2.46.0`. Expect `sha256:933fcd3f…2001777` unless upstream re-pushed; if it differs, tell Chris.
- [ ] **Step 2: Edit, commit and push.** `dex: v2.46.0 (strict config; no automerge)`. Watch `rollout status deploy/dex` with `Monitor`. If the new pod CrashLoops on an unknown field, the old pods keep serving: fix the field (a reseal; see Task 7 Step 2 for the mechanics) and do not roll back blindly.
- [ ] **Step 3: Verify.**
  - The logs show no `level=ERROR`, and no warning that `grpc.tlsClientCA` is unset.
  - `kubectl get crd | grep -E 'useridentities|authsessions'` lists both.
  - (Chris) a fresh sign-in works at an admin host via auth-admin, at a family host via auth-family, at `family.werethemille.rs` (the family-rs gate, then the Gramps OIDC button), and at `photos.chrismiller.xyz` on web.
  - A Claude Code Gramps tool call works (a one-off re-auth is acceptable).
  - `discover` is unchanged (still disagrees).
  - The snapshot diff against `edge-2.txt` may change only the `dex.chrismiller.xyz/` line and the `openid-configuration` body.
- [ ] **Step 4: Rollback, if needed.** Revert the image line. The new CRDs are harmless to 2.44.
- [ ] **Step 5: Docs, and commit.**

---

### Task 5: The facade (code): redirect rules, DCR, authorize shim, PRM and AS metadata

**Files:**
- Create: `images/mcp-jwt-auth/redirects.go`, `redirects_test.go`, `register.go`, `register_test.go`, `oauth_authorize.go`, `oauth_authorize_test.go`, `metadata.go`, `metadata_test.go`
- Modify: `images/mcp-jwt-auth/config.go` (new env), `main.go` (routes, gRPC dial with mTLS), `go.mod`/`go.sum` (`github.com/dexidp/dex/api/v2@v2.4.0`, `google.golang.org/grpc`, `golang.org/x/time`; run `go mod tidy` in `nix develop`)

**Interfaces:**
- Consumes: `config`, `isFacadeClient` (Task 2).
- Produces:
  - `type redirectRule struct{ Kind, Value string }`, where Kind is `exact`, `segment` or `loopback`.
  - `func parseRedirectRules(r io.Reader) ([]redirectRule, error)`. Each non-comment line is `<kind> <value>`. A `segment` value must be `https://` with a host. A `loopback` value is a path starting `/`.
  - `func matchRedirect(rules []redirectRule, raw string) (displayHost string, ok bool)`:
    - `exact` is byte equality with `raw`;
    - `segment` requires `strings.HasPrefix(raw, v)` and a remainder matching `^[A-Za-z0-9_-]{1,128}$`;
    - `loopback` requires `url.Parse` OK, scheme `http`, no userinfo, query, fragment or opaque part, hostname exactly `localhost` or `127.0.0.1`, a port of 1024–65535 with no leading zero, path == value, **and** `raw == fmt.Sprintf("http://%s:%d%s", host, port, path)`;
    - the display host is the URL host for `exact`/`segment`, and `localhost (this computer)` for `loopback`.
  - `func consentName(clientName string, hosts []string) string` returns `strings.Join(dedup(hosts), ", ") + " — " + clean(clientName)`. `clean` drops Unicode Cc/Cf (bidi controls included), collapses whitespace, caps at 48 runes plus `…`, and falls back to `MCP client` when empty.
  - `type clientRegistry interface` is the `CreateClient` and `ListClients` subset of `api.DexClient`.
  - `func newRegisterHandler(reg clientRegistry, rules []redirectRule, lim *rate.Limiter, maxClients int, prefix string, log *slog.Logger) http.Handler`.
  - `func newAuthorizeHandler(dexAuthURL string, isFacade func(string) bool) http.Handler`.
  - `func newPRMHandler(publicBase, issuer string, resources []string) http.Handler`.
  - `func newASMetadataHandler(fetch func(context.Context) (map[string]any, error), publicBase string, ttl time.Duration) http.Handler`.
  - Config env: `DEX_DISCOVERY_URL` (default `http://dex.dex.svc.cluster.local:5556/.well-known/openid-configuration`), `DEX_GRPC_ADDR` (`dex.dex.svc.cluster.local:5557`), `DEX_GRPC_CERT`/`DEX_GRPC_KEY`/`DEX_GRPC_CA` (`/etc/dex-client/tls.crt|tls.key|ca.crt`), `REDIRECT_RULES_FILE` (`/etc/mcp-redirects/rules`), `MAX_FACADE_CLIENTS` (20), `REGISTER_BURST` (5), `REGISTER_EVERY` (10m).
  - Routes on the same mux: `POST /oauth/register`, `GET /oauth/authorize`, `GET /.well-known/oauth-protected-resource/<resource>`, `GET /.well-known/oauth-authorization-server`. If Dex gRPC is unreachable, registration answers 503 and the process does not exit; auth stays fail-closed.

- [ ] **Step 1: Write the failing tests.**

```go
// TestMatchRedirect (rules: exact claude.ai, exact claude.com, exact chatgpt legacy,
// segment https://chatgpt.com/connector/oauth/, loopback /callback)
ok: "https://claude.ai/api/mcp/auth_callback"→"claude.ai", "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect", "https://chatgpt.com/connector/oauth/G_DaC-OGWUOC"→"chatgpt.com",
    "http://localhost:64336/callback"→"localhost (this computer)", "http://127.0.0.1:8976/callback"
// TestMatchRedirect_LookAlikes — every one refused:
"https://claude.ai.evil.example/api/mcp/auth_callback", "https://claude.ai@evil.example/api/mcp/auth_callback",
"https://CLAUDE.AI/api/mcp/auth_callback", "https://claude.ai./api/mcp/auth_callback", "…auth_callback?x=1",
"…auth_callback#f", "…auth_callback/", "http://claude.ai/api/mcp/auth_callback",
"https://chatgpt.com/connector/oauth/", "https://chatgpt.com/connector/oauth/a/b", "https://chatgpt.com/connector/oauth/%2e%2e",
"https://chatgpt.com/connector/oauth/x?y", "http://localhost.evil.example:1234/callback", "http://localhost:80/callback",
"http://localhost:0/callback", "http://localhost:65536/callback", "http://localhost:01234/callback",
"http://localhost:1234/callback/../x", "http://localhost:1234/Callback", "http://[::1]:1234/callback",
"https://localhost:1234/callback", "http://user@localhost:1234/callback", "http://LOCALHOST:1234/callback"
// TestConsentName_Hostile — for each name, HasPrefix(got, "chatgpt.com — ") and no rune in Cc/Cf:
"Claude at claude.ai", "‮moc.ia.edualc", "line1\nline2", strings.Repeat("x", 10000), "<b>x</b>"
consentName("", []string{"chatgpt.com"}) == "chatgpt.com — MCP client"
consentName("Claude Code (gramps)", []string{"localhost (this computer)"}) == "localhost (this computer) — Claude Code (gramps)"
// TestRegister (fake registry records CreateClientReq)
valid → 201, Content-Type application/json; Id HasPrefix "mcp-" && len == 30; Public; Name == consentName(...);
        Secret == "" && TrustedPeers == nil && LogoUrl == ""; body token_endpoint_auth_method "none",
        scope "openid email profile offline_access", grant_types [authorization_code refresh_token]
"https://evil.example/cb" → 400 {"error":"invalid_redirect_uri"}, CreateClient not called
0 or 4 redirect_uris → 400 invalid_redirect_uri; token_endpoint_auth_method "client_secret_basic" → 400 invalid_client_metadata;
grant_types ["client_credentials"] → 400 invalid_client_metadata; 9 KiB body → 400; GET → 405; text/plain → 400
// TestRegister_RateLimited: limiter burst 2 → 3rd → 429, Retry-After > 0, CreateClient called twice
// TestRegister_CapReached: ListClients returns 20 "mcp-*" + statics, max 20 → 403 {"error":"access_denied"}
// TestRegister_DexError: CreateClient error → 502, body not containing the gRPC error text
// TestAuthorize: facade id + S256 → 302 Location host dex.chrismiller.xyz path /auth,
//   scope == "openid email profile offline_access" (request had "mcp audience:server:client_id:immich"),
//   state/redirect_uri/code_challenge/resource preserved; "immich" → 400; method plain → 400;
//   no code_challenge → 400; response_type token → 400
// TestPRM: 200 {"resource":"https://mcp.chrismiller.xyz/gramps/mcp","authorization_servers":["https://dex.chrismiller.xyz"],
//   "scopes_supported":["openid","email","profile","offline_access"],"bearer_methods_supported":["header"]},
//   Access-Control-Allow-Origin "*"; /other/mcp → 404; POST → 405
// TestASMetadata (fake Dex doc): issuer unchanged; authorization_endpoint "https://mcp.chrismiller.xyz/oauth/authorize";
//   registration_endpoint "https://mcp.chrismiller.xyz/oauth/register"; token_endpoint and jwks_uri unchanged;
//   code_challenge_methods_supported ["S256"]; token_endpoint_auth_methods_supported contains "none";
//   no "client_id_metadata_document_supported" key; fetch error → 503; two GETs within ttl → fetch called once
// TestAgreement: PRM authorization_servers[0] == AS metadata issuer for the same config (live problem 4)
```

- [ ] **Step 2: Run the tests and watch them fail.** `nix develop -c sh -c 'cd images/mcp-jwt-auth && go test ./...'`.
- [ ] **Step 3: Implement.** Registration uses `http.MaxBytesReader(8<<10)`. The client id is `prefix + rand.Text()`. Log each refused redirect as scheme+host+path with no query, so Chris can add Gemini's real form. CORS `*` applies to the two metadata GETs only.
- [ ] **Step 4: Run the tests and watch them pass.** Same command.
- [ ] **Step 5: Commit and publish.** `nix develop -c git commit -m "mcp-jwt-auth: OAuth facade (exact-redirect DCR, global rate limit, scope shim, PRM + RFC 8414 for Dex)"`, push, then use `Monitor` until CI is green and the digest resolves. **Do not deploy yet.**

---

### Task 6: Roll out the facade, then strip hyprmcp

**Files:**
- Create: `cluster/oauth2-proxy/mcp-redirects.rules`:
  - `exact https://claude.ai/api/mcp/auth_callback`
  - `exact https://claude.com/api/mcp/auth_callback`
  - `exact https://chatgpt.com/connector_platform_oauth_redirect`
  - `segment https://chatgpt.com/connector/oauth/`
  - `loopback /callback`
  - Gemini, commented out until observed: `# segment https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-`. Each line carries a comment naming its source.
- Create: `cluster/oauth2-proxy/mcp-jwt-auth-dex-client.yaml` (Certificate `mcp-jwt-auth-dex-client`, ClusterIssuer `edge-ca`, CN `mcp-jwt-auth`, `client auth`, ECDSA 256, 8760h/720h, the same shape as hyprmcp's)
- Modify: `cluster/oauth2-proxy/kustomization.yaml` (the cert resource; `configMapGenerator: mcp-redirect-rules` from `rules=mcp-redirects.rules`, so a rule change rolls the pods)
- Modify: `cluster/oauth2-proxy/mcp-jwt-auth.yaml`:
  - pin the Task 5 image;
  - mount the volumes at `/etc/dex-client` and `/etc/mcp-redirects` (**not** under `/etc/mcp-jwt-auth`, which is the read-only emails volume);
  - add Ingress `mcp-oauth`: host `mcp.chrismiller.xyz`, **`pathType: Exact`** for `/oauth/register`, `/oauth/authorize` and `/.well-known/oauth-protected-resource/gramps/mcp`, to `mcp-jwt-auth:4181`;
  - add Ingress `mcp-as-metadata`: host `dex.chrismiller.xyz`, Exact `/.well-known/oauth-authorization-server`, to `mcp-jwt-auth:4181`.
  Exact paths win over the old Prefix routes, so the old and new Ingresses never tie mid-sync, and `/auth` stays unreachable from outside.
- Modify: `.pre-commit-config.yaml` (local hook `no-static-mcp-client`, `language: pygrep`, `entry: '^\s*- id: mcp-'`, `files: ^cluster/dex/config\.secret\.yaml$`)
- Second commit:
  - modify `cluster/mcp-gateway/config/config.yaml`: `server: https://dex.chrismiller.xyz` (no slash); `serverMetadataProxyEnabled: false`; `authorizationProxyEnabled: false`; `dynamicClientRegistration.enabled: false`; delete `dexGRPCClient`;
  - modify `cluster/mcp-gateway/deployment.yaml`: delete the `dex-client` volume and mount, and the whole `mcp-gateway-oauth` Ingress; update the comments, since the rate-limit claim moves to the facade;
  - modify `cluster/mcp-gateway/kustomization.yaml` (drop the cert);
  - delete `cluster/mcp-gateway/dex-client-cert.yaml`.
- Modify: `docs/auth.md` (a new "MCP OAuth facade" subsection), `docs/gramps.md` (connect instructions, gotchas)

**Interfaces:**
- Consumes: the Task 5 image and routes.
- Produces: public `registration_endpoint` and `authorization_endpoint` on mcp.chrismiller.xyz, the AS document at `https://dex.chrismiller.xyz/.well-known/oauth-authorization-server`, and PRM pointing at Dex.

- [ ] **Step 1: Snapshot before.** Save `edge-5.txt`.
- [ ] **Step 2: Commit A.** Cert, rules, pin, env, volumes, Ingresses and the hook: `mcp: the OAuth facade takes over registration, authorize and metadata`. Push. Use `Monitor` until `kubectl -n oauth2-proxy get certificate mcp-jwt-auth-dex-client` is Ready and the rollout is 2/2.
- [ ] **Step 3: Verify A.**
  - `discover` exits **0**: `authorization_server` and `issuer` are both `https://dex.chrismiller.xyz`, and `registration_endpoint` is `https://mcp.chrismiller.xyz/oauth/register`.
  - `register --name evil --redirect https://evil.example/callback` returns **400 `invalid_redirect_uri`**, and `oauth2clients` has no new entry (exit criterion "foreign-redirect registration refused").
  - `register --name mcp-probe --port 8976` returns 201 with an id `mcp-…`, recorded as `PROBE_ID`. `oauth2clients` shows name `localhost (this computer) — mcp-probe`.
  - `login --client-id $PROBE_ID --port 8976` (Chris; consent is still skipped until Task 7) prints `aud == PROBE_ID` and `has_refresh_token: true`. Then `call` returns 200 through the edge and gramps-mcp.
  - Claude Code (grandfathered) still works.
  - The snapshot diff against `edge-5.txt` has exactly these changes: the PRM body (`authorization_servers` is now Dex), the dex AS document going 404 → 200, `/oauth/register` going 400 → 405, and `/oauth/authorize` going 302 → 400. The mcp AS document is still 200, because hyprmcp still serves `/.well-known`.
  - `kubectl -n oauth2-proxy top pods -l app.kubernetes.io/name=mcp-jwt-auth` is under the 128Mi limit. Note the number in the task.
- [ ] **Step 4: Commit B (strip hyprmcp).** `mcp-gateway: proxy only; registration and metadata belong to the facade`. Push and watch with `Monitor`.
  - Verify: the mcp AS document is 404 (expected), the `/gramps/mcp` line is unchanged, Claude Code works, and the probe `call` returns 200.
  - Once Argo has pruned the Certificate, **ask Chris (S4)** before running `kubectl -n mcp-gateway delete secret mcp-gateway-dex-client`. cert-manager leaves the key behind, and that key can still create Dex clients.
- [ ] **Step 5: Docs.** `docs/auth.md`:
  - how the facade works: the rules file and its three kinds; the global limiter (5 burst per replica, one per 10 minutes, cap 20) and why it is global (ETP=Cluster);
  - that `mcp-` is reserved, with the pre-commit hook;
  - how to add Gemini from the refusal log;
  - why PRM names Dex.
  `docs/gramps.md`: the facade sits in front; and "a hyprmcp 401 cache only matters if hyprmcp's `/.well-known` is ever routed again". Commit.

---

### Task 7: Dex consent, with remembered consent (needs Chris's OK on the experimental flag)

**Files:**
- Modify: `cluster/dex/config.secret.yaml`, by key path only (S9; never print the file):
  - `stringData.config.yaml` → `oauth2.skipApprovalScreen: false`, with the comment rewritten: consent is enforced in Dex because the facade can be bypassed, and remembered consent makes it a once-per-person-per-app cost;
  - `oauth2.grantTypes: [authorization_code, refresh_token]` (spec §8.1: never an STS; this drops device code and token exchange);
  - a new `sessions:` block with `cookieEncryptionKey`, `absoluteLifetime: 24h`, `validIfNotUsedFor: 1h`, `ssoSharedWithDefault: none`.
- Regenerate: `cluster/dex/config.yaml` (`rm` it, then `nix develop -c ./sign.sh`)
- Modify: `cluster/dex/dex.yaml` (env `DEX_SESSIONS_ENABLED: "true"`, with a comment: experimental, and removing it turns consent into an every-login screen, which is still safe)
- Modify: `docs/auth.md` (consent section; the troubleshooting row "Grant Access on every login" is rewritten: expected once per app per person, and every time if the sessions flag is off), `CLAUDE.md` (one line under Known sharp edges #4: Dex runs the experimental sessions flag for remembered consent; re-check on every Dex bump)

**Interfaces:**
- Consumes: facade-named DCR clients (Task 6).
- Produces: a Dex approval page whose first line is `<redirect host> — <client name> would like to:` for every DCR client created by the facade.

- [ ] **Step 1: Gate.** Chris answers open question 1. If he says no, skip the `sessions:` block and the env var. Everything else in this task stays, and the verification changes "once" to "every login".
- [ ] **Step 2: Edit without echoing.**
  - Put the literal placeholder `cookieEncryptionKey: __GENERATE__` in the file.
  - Replace it in place with `nix develop -c python3 -c` (read the file, swap the placeholder for `secrets.token_hex(16)`, a 32-byte AES-256 key, and write it back). Print nothing.
  - `rm cluster/dex/config.yaml && nix develop -c ./sign.sh` must print the file and no `FAIL`.
  - `git add` both files. `git show :cluster/dex/config.secret.yaml | head -c 10 | od -c` must show `\0GITCRYPT`.
  - Review with `git diff --cached --no-textconv --stat` only.
- [ ] **Step 3: Commit and push.** `dex: consent for every client, remembered per person (sessions); no device or token-exchange grants`.
  - Record the Secret's `.metadata.resourceVersion` before the push. Use `Monitor` until it changes, which means the controller has unsealed the new config.
  - Then run `nix develop -c kubectl -n dex rollout restart deploy/dex`. Dex reads its config only at startup, and the env change may have rolled the pods before the Secret updated. Watch with `Monitor`.
- [ ] **Step 4: Verify.**
  - The Dex logs contain `config sessions` with no errors. In `openid-configuration`, `grant_types_supported` is exactly `["authorization_code","refresh_token"]`.
  - (Chris) `register --name mcp-probe-consent --port 8977`, then `login`. **The Dex page reads `localhost (this computer) — mcp-probe-consent would like to:`** (exit criterion "consent screen naming its redirect host"). Approve it. `call` returns 200. A second `login` with the same client shows no approval page (remembered).
  - (Chris) Sign out of an admin host (`/oauth2/sign_out`) and back in: `galaxy admin would like to:` appears once, and a repeat shows nothing. Do the same for `photos.chrismiller.xyz` and `family.werethemille.rs` (two clients: the family tier, then Gramps). If Immich asks every time, it is sending `prompt=consent`; record that in `docs/auth.md`, and treat it as a finding, not a blocker.
  - **(Chris) Reconnect Claude Code** (exit criterion):
    1. `claude mcp remove gramps` (with the scope it was added under);
    2. `claude mcp add --transport http gramps https://mcp.chrismiller.xyz/gramps/mcp`;
    3. `/mcp`, then authenticate. Dex shows `localhost (this computer) — Claude Code (gramps) would like to:`; approve it;
    4. a Gramps read tool returns data;
    5. `oauth2clients` shows a new `mcp-…` client.
  - The snapshot diff against the post-Task-6 snapshot changes only the `openid-configuration` body (grant types, plus `end_session_endpoint` because sessions are on).
- [ ] **Step 5: Rollback.**
  - Removing `DEX_SESSIONS_ENABLED` keeps consent on, shown every login.
  - Setting `skipApprovalScreen: true` again **reopens live problem 1 for the hosted confused-deputy route.** Only do it with Chris's explicit OK.
- [ ] **Step 6: Docs, and commit.**

---

### Task 8: Expiry, with a canary first, then 1 h

**Files:**
- Modify: `cluster/dex/config.secret.yaml` key `expiry`:
  - `expiry.idTokens` (canary `10m`, then `1h`);
  - `expiry.refreshTokens.disableRotation: false`;
  - `.reuseInterval: 3s`;
  - `.validIfNotUsedFor: 720h`;
  - `.absoluteLifetime: 2160h`.
  Then reseal `cluster/dex/config.yaml` the same way as Task 7 Step 2. There is no new secret value, so no generation step.
- Modify: `docs/auth.md` (the expiry block, the refresh model per app, and "short expiry is not revocation; revoke with Dex `RevokeRefresh`/`RevokeConsent`")

**Interfaces:**
- Consumes: MCP clients that hold refresh tokens (Task 6 shim, Task 7 reconnect).
- Produces: 1 h ID and access tokens, rotating refresh tokens with a 30-day idle and 90-day absolute limit.

- [ ] **Step 0: Refresh pre-test, at the current 24 h lifetime, before anything is lowered.**
  - `grep -c -- '--cookie-refresh' cluster/oauth2-proxy/{admin,family,family-rs}.yaml` must print `0` for each file. If so, the 168 h cookie governs the session and Dex's expiry is never consulted. If any file has the flag, stop and work out its refresh path first.
  - `refresh --client-id <mcp-probe-consent>` must return `refresh_rotated: true`; then `call` returns 200. That proves the MCP refresh grant (Dex refresh, then Google refresh) works before it starts to matter hourly.
  - If the refresh fails, stop: lowering would mean hourly re-logins for MCP clients.
- [ ] **Step 1: Canary.** Set `idTokens: 10m` and the refresh policy, reseal, then commit `dex: 10-minute tokens for the refresh canary (raised to 1h within the hour)`. Push, wait for the Secret, restart, and use `Monitor` until Ready. Call this time T0.
- [ ] **Step 2: Open sessions at T0 (Chris).**
  - In the browser: an admin host, a family host, `family.werethemille.rs` (Gramps web) and photos web.
  - On the phone: open the Immich app.
  - `login --client-id <mcp-probe-consent>`; it must show `lifetime_s≈600` and `has_refresh_token: true`.
  - One Claude Code Gramps call.
  - Record `sum(http_requests_total{namespace="dex",handler="/auth"})` with the Prometheus exec command from `docs/auth.md` "Metrics".
- [ ] **Step 3: Wait** with `Monitor` (until-loop on the clock) to T0+15 min.
- [ ] **Step 4: Verify (exit criterion "Dex refresh works for each app").**
  - Every browser app reloads and works, and the `/auth` count has not moved since Step 2.
  - Immich mobile browses.
  - Gramps desktop sync is unaffected (it uses a password).
  - Probe: `call` with the old token returns 401 `invalid_token`; `refresh` returns `lifetime_s≈600` and `refresh_rotated: true`; `call` then returns 200.
  - A Claude Code Gramps call succeeds **without a browser prompt**.
  - **If any app forces a login, go back to 24h**, record which app and why, and fix it before Step 5.
- [ ] **Step 5: Set 1 h.** `idTokens: 1h`, reseal, commit `dex: 1h tokens, rotating refresh tokens (30d idle, 90d absolute)`, push, restart, `Monitor`. After 75 minutes (`Monitor`), confirm that a Claude Code call and a probe `refresh` + `call` work.
- [ ] **Step 6: Docs, and commit.**

---

### Task 9: Exit verification and cleanup

**Files:**
- Modify: `cluster/oauth2-proxy/mcp-jwt-auth.yaml`, `cluster/gramps/mcp.yaml` (empty the grandfathered lists, only after Chris's OK)
- Modify: `docs/auth.md` (spike result: "Dex access tokens carry `aud` = client id → Path A", with the date)

- [ ] **Step 1: Re-run every exit check and paste the evidence into the task notes.**

| Exit criterion | Command | Pass |
|---|---|---|
| Foreign-redirect registration refused | `register --name x --redirect https://evil.example/callback` | 400 `invalid_redirect_uri`, no new client |
| Consent names the redirect host | Task 7 Step 4 with a fresh `register` | page starts `localhost (this computer) — …` |
| Token for another client rejected | `call --client-id $SPIKE_ID` after a `refresh` (or `login --authorize-url https://dex.chrismiller.xyz/auth`), so it is not merely expired | 401 `invalid_token` |
| Unauthenticated in-cluster POST to gramps-mcp | Task 3 Step 8 `kubectl run` | `401` |
| Claude Code works after reconnect | one Gramps read tool | data returned |
| Edge snapshot diff clean | final snapshot vs `edge-0.txt` | only the lines enumerated in Tasks 2, 4, 6 and 7 differ; every other host is byte-identical |
| Dex refresh works per app | Task 8 Step 4 evidence | all rows pass |
| Metadata agree; 401 has `resource_metadata` | `discover`; `call --no-token` | exit 0; header contains `resource_metadata=` |

- [ ] **Step 2: Cleanup (ask Chris first, S4).** Delete the probe and spike Dex clients: find each `metadata.name` by its `.id` with jq, then `kubectl -n dex delete oauth2client <name>`.
- [ ] **Step 3: Retire the grandfathered ids** once Chris confirms Claude Code reconnected and has decided about the ChatGPT client (open question 2). Empty both lists in one commit, `mcp: retire grandfathered MCP clients`. Push and use `Monitor`. Confirm Claude Code still works, then delete the old Dex client objects with his OK.
- [ ] **Step 4: Final docs commit,** and a last snapshot to `edge-final.txt`.

---

## Open questions for Chris (answer before the named task)

1. **(Task 7) Enable Dex's experimental `DEX_SESSIONS_ENABLED=true`?** It gives remembered consent, so each person sees "Grant Access" once per app. Without it, consent shows on every interactive login to every app, which brings back the click removed with `--prompt=select_account` in 2026-09. Recommendation: enable it. Turning it off is one env var, and consent stays on either way.
2. **(Task 9) The ChatGPT client `7QITXD4SBM2ZFXDP5TLPRJGZZ3`** (2026-09-28): is it yours, and still in use? It is grandfathered until then.
3. **(Task 6) Gemini.** No official consumer callback URL is published. A forum report shows the Google relay `https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-…`. Plan default: ship that rule commented out and add the exact form from the first refused-registration log. Do you want it enabled now on the inferred prefix instead?
4. **(Task 8) Refresh limits.** Proposed 30-day idle and 90-day absolute (an MCP client unused for 30 days reconnects). OK?
5. **(Task 8) Canary window.** About 30 minutes with 10-minute tokens, needing you at a browser and on your phone (Immich). When?
6. **(Task 7) Dropping Dex's device-code and token-exchange grants** (spec §8.1 "never an STS"). Nothing uses them as far as the repo shows. Confirm.

## Self-review notes

- **Spec coverage.**
  - §14 0a bullets: the Dex upgrade (Task 4), expiry and refresh tests (Task 8), consent (Task 7, on facade naming from Tasks 5–6), the DCR allowlist and rate limit (Tasks 5–6), the `mcp-jwt-auth` audience and allowlist with the spike first (Tasks 1–2, Branch B), gramps-mcp self-auth (Task 3), and the metadata fixes (Task 2 for the 401, Tasks 5–6 for agreement). Docs and the snapshot are folded into each task.
  - Every 0a exit criterion is a row in Task 9.
  - §17 rows 1 and 2 map to the Task 1 decision and finding 3.
- **Not done in 0a, on purpose:** PKCE enforcement inside Dex (the facade enforces S256 for its own clients); `ContextVar`/`stateless_http` in gramps-mcp (phase 2); CIMD (never advertised, because Claude Code ≥2.1.80 breaks on it); renaming mcp-jwt-auth to mcp-edge (phase 2).
