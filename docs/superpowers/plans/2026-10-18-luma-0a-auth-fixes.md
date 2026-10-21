# Luma phase 0a: live auth fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four live security problems in spec §2 (one-click token theft through open DCR, 24 h unrevocable Dex tokens, an unauthenticated gramps-mcp, inconsistent MCP metadata) without taking Chris's Gramps MCP connection offline for longer than one reconnect.

**Architecture:** Dex stays the only token issuer. `images/mcp-jwt-auth` (our Go forward-auth validator) grows into the 0a MCP facade. It checks audiences on every `/gramps/mcp` call, trusting an `mcp-` client only after a cached Dex `ListClients` check proves the facade made it. It also takes over the OAuth bootstrap from hyprmcp: protected-resource metadata, the RFC 8414 document for Dex's issuer, DCR with an exact-redirect allowlist, rate limit and cap, and the authorize scope shim. hyprmcp shrinks to a token-checking reverse proxy. Dex 2.46.0 enforces consent for every client, with remembered consent (the experimental sessions flag), and names the redirect host because the facade writes that host into each client's display name. gramps-mcp checks the same token itself. **The 2026-10-18 stopgap that answers 403 on `/oauth/register` stays up until the facade's allowlist and Dex's global consent are both live and verified. A separate commit lifts it (Task 10).**

**Tech Stack:** Go 1.25 (go-oidc v3, Dex `api/v2` gRPC, `golang.org/x/time/rate`), Python 3.13 (PyJWT plus cryptography, Starlette ASGI) applied as a flake patch, Dex v2.46.0, Traefik v3 Ingress, Argo CD, Sealed Secrets, git-crypt, kube-prometheus-stack.

**Spec:** `docs/superpowers/specs/2026-10-18-luma-design.md`: §2 (live security problems), §8.1, §9.1, §9.3 (0a part only), §14 phase 0a, §17 (rows 1 and 2). Phase 2 work (luma-auth, per-user Gramps, mcp-edge v1 replacing hyprmcp) is out of scope.

---

## Findings that shaped this plan (verified 2026-10-18, read-only)

1. **The consent screen has to live in Dex, not the facade.** Dex's `/auth` is public. Anyone holding a `client_id` can send a victim straight to `https://dex.chrismiller.xyz/auth?...` and skip any page the facade shows, so a consent page in the facade protects nothing. **Rejected.**
2. **Dex 2.46.0 has no per-client consent skip.** `server/consent/consent.go` `Satisfied()` asks only `ForceApprovalPrompt`, then the global `SkipApproval`, then the stored consents in `UserIdentity`. **The read happens whatever the flag says, but the write does not.** `handleApproval` stores consents only `if h.Sessions.Enabled()`, and `authflow/finalize.go` creates the `UserIdentity` only then. Without `DEX_SESSIONS_ENABLED=true`, consent shows at every interactive login. With the flag off after it was on, consents already stored are still honoured.
3. **Consent mechanism (Chris, 2026-10-18): turn the skip off globally, turn on Dex sessions for remembered consent, and have the facade write the redirect host into each DCR client's `Name`.** Dex's approval template renders `{{ .Client }} would like to:` from `client.Name` (HTML-escaped). A Dex patch was rejected: it would turn the house IdP into an image we build (sharp edge #6). The cost of this choice is that each person approves each static app once.
4. **Dex access tokens carry `aud` = the client id**, in v2.44 (`newAccessToken` calls `newIDToken`) and in v2.46 (`SignAccessToken` calls `SignIDToken`). `azp` appears only when there are several audiences. Task 1 proves this live; Branch B covers a failure.
5. **The 401 that clients see comes from mcp-jwt-auth**: `www-authenticate: Bearer error="invalid_token"`, with no `resource_metadata`. The PRM says `authorization_servers: ["https://mcp.chrismiller.xyz/"]`, but the AS document on that host says `issuer: "https://dex.chrismiller.xyz"`.
6. **hyprmcp's DCR rate limit does nothing here.** `httprate.LimitByRealIP(3, 10m)` trusts spoofable headers, and Traefik's Service is `externalTrafficPolicy: Cluster`, so real client IPs never arrive. The facade therefore uses a **global** limiter.
7. **Only MCP clients use Dex tokens after login.** oauth2-proxy has no `--cookie-refresh`, so its 168 h cookie never consults Dex. Immich and Gramps exchange the code once. The refresh that matters is the MCP clients' `refresh_token` grant, which needs `offline_access`. **The cluster held zero Dex refresh tokens on 2026-10-18**, so every reconnect today is a full browser sign-in.
8. **Live DCR clients, both Chris's (confirmed 2026-10-18):** `7QITXD4SBM2ZFXDP5TLPRJGZZ3` ("ChatGPT", `https://chatgpt.com/connector/oauth/G_DaC-OGWUOC`), which **stays grandfathered**, and `M4HNWEXI6EFRW7EXBJFYENJVL2` ("Claude Code (gramps)", `http://localhost:64336/callback`), retired in Task 12 after the fixed-port reconnect. `claude mcp add` has `--callback-port` and `--client-id`.
9. In the pinned nixpkgs, mcp is 1.29.0 and pyjwt is 2.13.0, **with no `cryptography` in the closure**, so PyJWT cannot verify RS256. `gramps-mcp-image.yml` does not trigger on `patches/**`.
10. **Renovate PR #2662** ("update all non-major dependencies", open) bundles Dex v2.44.0 → v2.46.0, oauth2-proxy v7.13.0 → v7.15.5, go-oidc v3.16 → v3.21 and `golang:1.27-alpine` for `images/mcp-jwt-auth`, among others. The existing oauth2-proxy `automerge: false` rule keeps the package *in* the group, so the rule only stops the whole bundle auto-merging. Dex has no rule at all. **Chris must not merge #2662 until 0a is done.**
11. **Traefik priority (C1).** The default priority is the rule's length. The stopgap's `Host(...) && PathPrefix(/oauth/register)` is 60, a facade `Path(/oauth/register)` would be 54, and hyprmcp's `PathPrefix(/oauth)` is 51. So the stopgap outranks an Exact facade route, and "Exact beats Prefix" is false here. The repo already uses explicit `router.priority` (`cluster/media/ombi-ingress.yaml`).
12. **hyprmcp PRM poisoning (H1).** On *any* upstream 401, `oauth/misc.go` stores the 401's `resource_metadata` URL (or the upstream-derived URL if there is none) in a package-global map keyed by path. From then on, `protected_resource.go` proxies that URL with `http.DefaultClient`, which has no timeout. Only a restart clears the map. The same code splits `WWW-Authenticate` on spaces and trims only quotes, so `resource_metadata` must be the **last** parameter or a trailing comma gets cached as part of the URL. hyprmcp forwards `Authorization` upstream (`httputil.ReverseProxy`).
13. **Dex 2.46 config facts.**
    - It refuses to start with a `sessions:` block but no `DEX_SESSIONS_ENABLED=true` (`cmd/dex/config.go` `Validate`).
    - `cookieEncryptionKey` is taken as raw bytes, and must be 16, 24 or 32 of them.
    - `DEX_CONTINUE_ON_CONNECTOR_FAILURE` defaults to **true**.
    - `oauth2.pkce.codeChallengeMethodsSupported` defaults to `[S256, plain]`, and `enforce` to false.
    - `web.headers` supports `X-Frame-Options` and `Content-Security-Policy`.
    - The gRPC identity/consent RPCs (`RevokeConsent`, `DeleteUserIdentity`) need `DEX_API_SESSIONS_IDENTITIES_CRUD`, which stays off. Revocation is therefore `kubectl` on `useridentities.dex.coreos.com` (S4).
    - Dex keeps **one refresh token per (user, connector, client)**: a new login for the same client replaces the old refresh token.
14. **A public Dex client with an empty `RedirectURIs` accepts any loopback redirect, and OOB** (`authflow/request.go` `validateRedirectURI`). The facade must never create one, and must distrust one.
15. **Dex metrics.** Plain `/auth` `GET 200` arrives about 180 times an hour from an unknown poller, so it is no login signal. Real signals: `handler="/auth/{connector}"` `302` (an upstream login started), `handler="/callback"` `303` (the IdP came back), and `/token` `POST 200` (a code exchange or refresh).
16. **Live state.**
    - Dex: 2 replicas (top and bottom), no PDB, no explicit strategy.
    - The hyprmcp Dex client cert `mcp-gateway-dex-client` is valid until **2027-09-26T05:00:19Z**. Dex trusts any `edge-ca` client cert for its whole API.
    - Argo CD has local admin enabled (`argocd-initial-admin-secret` exists) and does not sign in through Dex.
    - Snapshot today: `GET`/`POST /oauth/register` = **403** (the stopgap). `/oauth/authorize` = 302 to `https://dex.chrismiller.xyz/auth?scope=openid+profile+email`.
    - Dex discovery: grant types `authorization_code, refresh_token, device_code, token-exchange`; PKCE `[S256, plain]`.

## Global Constraints

- Dex target: `ghcr.io/dexidp/dex:v2.46.0`. The index digest on 2026-10-18 was `sha256:933fcd3f523338c847b88ef84a7145fb2fcb7b9917f08418612afeaaa2001777`; re-resolve it before pinning.
- Expiry: "about 1 h for ID and access tokens, with rotating refresh tokens and an idle limit". Values: `expiry.idTokens: 1h` (it governs access tokens too); refresh rotation on, `reuseInterval: 3s`, **`validIfNotUsedFor: 720h` (30 d), `absoluteLifetime: 2160h` (90 d)**.
- Grant types: exactly `authorization_code` and `refresh_token`. Device code and token exchange are dropped; nothing uses them. Spec §8.1: "**Never use Dex as an STS.**"
- PKCE: `codeChallengeMethodsSupported: ["S256"]`. Set `enforce: true` only once Immich and Gramps are seen sending S256 (Task 9 records it; Task 12 decides).
- Consent: "dynamically registered clients **must** show a consent screen that names the redirect host". `skipApprovalScreen: false` globally, plus `DEX_SESSIONS_ENABLED: "true"` (Chris, 2026-10-18).
- DCR: "**exact redirect URIs** from an allowlist (claude.ai, chatgpt.com and Gemini callback URLs) … Rate-limited."
  - Loopback is exactly `loopback-port 8976 /callback`, with no port range. Claude Code reconnects with `--callback-port 8976`, and `tools/mcp-probe` uses the same port.
  - **Gemini ships commented out** until the first refused registration logs its real callback.
- **Registration stays blocked** (the stopgap 403) until the facade allowlist (Task 6) and Dex consent (Task 9) are verified. A dedicated commit lifts it (Task 10), before any client reconnects.
- Audience: "`aud` or `azp` must be a facade-created client (existing ids grandfathered; new ones prefixed `mcp-`)". "Tokens without a verifiable audience are rejected." An `mcp-` id counts as facade-created at the edge only when Dex `ListClients` shows it as `Public`, with no `TrustedPeers`, with 1–3 redirect URIs, and with every one of them matching the rules.
- Grandfathered ids have **one source**, `cluster/oauth2-proxy/mcp-grandfathered-clients.txt`, mirrored into `cluster/gramps/` by a pre-commit hook.
- Allowlists and id lists parse identically in Go and Python: cut at the first `#`, trim, drop empty entries, and lowercase emails only. Shared vectors live in `images/mcp-jwt-auth/testdata/list-vectors.json`.
- Metadata: "the 401 carries `resource_metadata`", and PRM `authorization_servers` equals the AS metadata `issuer`. PRM names whoever signs the bearer tokens (Dex on Path A, the facade on Branch B). `resource_metadata` is always the **last** auth-param (finding 12). The mcp-host `/.well-known/oauth-authorization-server` keeps being served, for old-style clients.
- gramps-mcp: "ASGI middleware validates the bearer (401 otherwise) … validating Dex JWKS: issuer, expiry, `aud`/`azp` ∈ facade clients, plus the allowlist". `algorithms=["RS256"]` only. Any verification error is a 401, never a 500.
- Never switch the Microsoft connector to generic OIDC. Never give a static Dex client an id starting `mcp-`.
- Repo rules:
  - S2: change git, never live state. `kubectl rollout restart` is allowed.
  - S4: ask Chris before deleting any OAuth2Client, UserIdentity, RefreshToken, Secret or Certificate, and before patching an OAuth2Client.
  - S5: no Dex rollout (Tasks 4, 9, 11) during node maintenance (memtest on `middle` is pending), a drain or a Ceph repair. `kubectl get nodes` must show all three Ready and none SchedulingDisabled. **Tasks 4 and 9 happen on different days.**
  - S6: never `--no-verify` or `--no-gpg-sign`. Signing is off, so no `-S`.
  - S9: never print values from `cluster/dex/config.secret.yaml`, emails, tokens or keys. Use `git diff --no-textconv` for anything that leaves the terminal.
- Images we build (sharp edge #6): CI publishes `:sha-<7 chars>`. Compute the local tag with `git rev-parse --short=7 HEAD`. For gramps-mcp, take the tag and digest from the workflow's "Tag and push" log. The `:sha-…@sha256:…` pin is a **second, manual commit**.
- Workflow:
  - Commit straight to `main` with `nix develop -c git commit -m …`. Check that `gh api user --jq .login` is `ChristopherJMiller` before `git push origin main`.
  - Toolchain commands run as `nix develop -c …`, with `KUBECONFIG=/tmp/galaxy-kubeconfig`.
  - Waits longer than 60 s use `Monitor`, never a sleep loop.
  - Don't merge while Ceph is HEALTH_ERR.
- Rollback path, independent of Dex: `git revert` + push. Then, to skip the 180 s reconcile, log in to Argo as local admin over a port-forward and sync the app. The login is `nix develop -c argocd login --port-forward --port-forward-namespace argo-cd --username admin`; the password comes from `argocd-initial-admin-secret` and is never printed. The sync is `argocd app sync <app> --port-forward --port-forward-namespace argo-cd`.
- Edge:
  - One Ingress per host and path. No per-host cert. No external-dns or cert-manager annotation.
  - Facade Ingresses carry `traefik.ingress.kubernetes.io/router.priority: "200"`. The stopgap carries `"300"` until it is deleted.
  - Run `edge-snapshot.sh` before and after every edge change.
- Monitoring: an alert is a `PrometheusRule` in namespace `prometheus` labelled `release: prometheus` (the selector since 0b Task 5, 121ece15), plus the legacy `prometheus: kube-prometheus, role: alert-rules`. A ServiceMonitor needs `release: prometheus`. Non-critical alerts route to Discord (`homelab-default`).

## Review Focus

1. **Redirect-URI look-alikes** must be refused at registration, so a code is never delivered anywhere but an allowlisted origin. The class: userinfo `@`, suffix hosts, case, a trailing dot, query, fragment, a trailing slash, percent-encoding, `[::1]`, the wrong loopback port, a leading-zero port, `..`. Pinned by Task 5 `TestMatchRedirect_LookAlikes`.
2. **A hostile `client_name`** (bidi overrides, newlines, 10k characters, a fake host in the name) must never move the real redirect host off the front of the consent text. Pinned by Task 5 `TestConsentName_Hostile`.
3. **An `mcp-` client the facade did not make** must not open the tree at the edge. Examples: not `Public`, with `TrustedPeers`, with no redirects, or with an off-rule redirect, perhaps created by a leftover edge-ca key or a typo'd static client. Pinned by Task 5 `TestClientCache_Invariants`, plus the Task 0 hook vectors.
4. **Hostile token headers at gramps-mcp** (`alg: none`, HS256 signed with the RSA public key, an unknown `kid`, an ES256 token, JWKS unreachable) must yield 401. A 500 or a pass is a failure. Pinned by Task 3 `test_alg_none_is_401`, `test_hs256_key_confusion_is_401`, `test_unknown_kid_is_401`, `test_es256_is_401` and `test_jwks_unreachable_is_401`.
5. **A registration storm** must return 429 with `Retry-After`, never a 5xx. Reaching the cap must return a clear 403. A Dex `AlreadyExists` must never return somebody else's id. Pinned by Task 5 `TestRegister_RateLimited`, `TestRegister_CapReached` and `TestRegister_AlreadyExists`. The 80% alert is the operational half (Task 6).

---

## File map

| Path | Change | Responsibility |
|---|---|---|
| `.renovaterc.json` | modify | own groups, no automerge: Dex, oauth2-proxy, `images/mcp-jwt-auth` deps |
| `.pre-commit-config.yaml` | modify | `no-static-mcp-client` (Task 0); `mirror-mcp-grandfathered` (Task 2) |
| `.claude/skills/edge-ingress/edge-snapshot.sh`, `SKILL.md` | modify | MCP/OAuth probes in the regression snapshot |
| `tools/mcp-probe/mcp_probe.py`, `tools/mcp-probe/tests/test_mcp_probe.py` | create | stdlib OAuth/MCP probe used by every live verification |
| `cluster/oauth2-proxy/mcp-grandfathered-clients.txt`, `cluster/gramps/mcp-grandfathered-clients.txt`, `scripts/mirror-mcp-grandfathered.sh` | create | one grandfathered list, mirrored |
| `images/mcp-jwt-auth/{config,lists,verify}.go` (+ `_test`), `testdata/list-vectors.json` | create (split from `main.go`) | config, shared list parsing, bearer verification and challenges |
| `images/mcp-jwt-auth/{redirects,clients,register,oauth_authorize,metadata,metrics,dextls}.go` (+ `_test`) | create | the facade |
| `images/mcp-jwt-auth/main.go`, `Dockerfile`, `go.mod`, `go.sum` | modify | wiring; `COPY *.go testdata`; `go test` in the build |
| `patches/gramps-mcp-inbound-auth.patch` | create | adds `src/gramps_mcp/inbound_auth.py` and `tests/test_inbound_auth.py`; wraps the HTTP app |
| `flake.nix`, `.github/workflows/gramps-mcp-image.yml` | modify | patch, cryptography, `gramps-mcp-src`, check, CI triggers and smoke tests |
| `cluster/oauth2-proxy/{mcp-jwt-auth.yaml,kustomization.yaml,mcp-redirects.rules,mcp-jwt-auth-dex-client.yaml,servicemonitors.yaml}` | modify/create | env, pins, volumes, gRPC cert, rules, Ingresses, scrape |
| `cluster/prometheus-stack/rules/mcp-facade-alerts.yaml`, `rules/kustomization.yaml` | create/modify | cap, violation and new-registration alerts |
| `cluster/mcp-gateway/{config/config.yaml,deployment.yaml,kustomization.yaml}`, `dex-client-cert.yaml` | modify/delete | stopgap priority (Task 6), strip hyprmcp (Task 8), lift the block (Task 10) |
| `cluster/gramps/{mcp.yaml,kustomization.yaml}` | modify | inbound-auth env, list mounts, pin |
| `cluster/dex/dex.yaml`, `config.secret.yaml` → `config.yaml` | modify/reseal | v2.46.0, flags, HA shape, consent, sessions, grants, PKCE, headers, expiry |
| `docs/auth.md`, `docs/gramps.md`, `CLAUDE.md` | modify | runbooks, folded into each task |

---

### Task 0: Preflight, guardrails and the regression baseline

**Files:**
- Modify: `.renovaterc.json`, `.pre-commit-config.yaml`, `.claude/skills/edge-ingress/edge-snapshot.sh`, `.claude/skills/edge-ingress/SKILL.md`

**Interfaces:**
- Produces: `edge-snapshot.sh` output ends with one line per MCP probe: `<METHOD> <host><path> <status> loc=<url> [<www-authenticate>] body=<jq -cS of /.well-known/* bodies>`. Later tasks diff against `$SCRATCH/edge-0.txt`. The usage becomes `nix-shell -p openssl jq --run ./edge-snapshot.sh`.
- Produces: the pre-commit hook id `no-static-mcp-client`.

- [ ] **Step 1: Cluster gate.**
  - Run the `cluster-snapshot` skill. Ceph must show its usual HEALTH_WARN noise or better; on HEALTH_ERR, stop and ask Chris.
  - Argo `dex`, `oauth2-proxy`, `mcp-gateway`, `gramps`, `traefik` and `prometheus-stack` must be Synced/Healthy.
  - `gh pr view 2662 --json state --jq .state` must be `OPEN`, not `MERGED`.
- [ ] **Step 2: Record the DCR clients and refresh tokens, without secrets.**
  - `nix develop -c kubectl -n dex get oauth2clients -o json | jq -r '.items[] | [.metadata.name,.id,.name,.public,(.redirectURIs|join(",")),((.trustedPeers//[])|join(","))] | @tsv'`. Expected: exactly the two ids in finding 8. If there are more, show them to Chris before continuing.
  - `kubectl -n dex get refreshtokens.dex.coreos.com --no-headers | wc -l` should be `0`, or the count noted.
- [ ] **Step 3: Renovate gates.** Edit by hand, keeping the file's formatting (renovate-triage skill, "Adding a gate").
  - Add a rule `{"matchManagers": ["kubernetes"], "matchPackageNames": ["ghcr.io/dexidp/dex"], "groupName": "dex", "groupSlug": "dex", "automerge": false, "description": …}`. The description says: the house IdP; 0a runs the experimental `DEX_SESSIONS_ENABLED` flag; read the release notes for sessions, consent and feature-flag changes and run the Task 4 local check before merging.
  - Add `"groupName": "oauth2-proxy", "groupSlug": "oauth2-proxy"` to the existing oauth2-proxy rule.
  - Add a rule `{"matchManagers": ["gomod", "dockerfile"], "matchFileNames": ["images/mcp-jwt-auth/**"], "groupName": "mcp-jwt-auth deps", "groupSlug": "mcp-jwt-auth-deps", "automerge": false, "description": …}`. The description says the facade is the MCP authorization decision.
  - Check the syntax with `jq empty .renovaterc.json`.
- [ ] **Step 4: Static-client guard.**
  - Add a local hook `no-static-mcp-client` with `language: pygrep`, `files: ^cluster/dex/config\.secret\.yaml$`, and, in YAML single-quoted form, `entry: '(?:^|[\s{,-])(?:id\s*:\s*["'']?mcp-|idEnv\s*:)'` (the doubled `''` is one quote). The comment explains that `mcp-` is reserved for the facade, and that `idEnv` is refused because the hook could not see the id.
  - Prove the regex in `nix develop -c python3 -`. It must match `      - id: mcp-x`, `        id: mcp-x` (not the first key), `      - id: "mcp-x"`, `      - id: 'mcp-x'`, `      - {id: mcp-x, name: y}` and `        idEnv: CLIENT_ID`. It must not match `      - id: oauth2-proxy-admin`, `          clientID: mcp-thing` or `      - id: google`.
  - Then `nix develop -c pre-commit run no-static-mcp-client --all-files` must pass.
- [ ] **Step 5: Extend the snapshot.**
  - Append a `while read -r method fq path` loop over a heredoc of these probes, each curled via `--resolve "$fq:443:192.168.0.7"`:
    - `GET mcp.chrismiller.xyz /gramps/mcp`
    - `POST mcp.chrismiller.xyz /gramps/mcp`, with a JSON-RPC `initialize` body and both MCP accept types
    - `GET mcp.chrismiller.xyz /.well-known/oauth-protected-resource/gramps/mcp`
    - `GET mcp.chrismiller.xyz /.well-known/oauth-authorization-server`
    - `GET dex.chrismiller.xyz /.well-known/oauth-authorization-server`
    - `GET dex.chrismiller.xyz /.well-known/openid-configuration`
    - `GET mcp.chrismiller.xyz /oauth/register`
    - `POST mcp.chrismiller.xyz /oauth/register`, with body `{}` and `content-type: application/json`
    - `GET mcp.chrismiller.xyz /oauth/authorize`
  - For each, print the status and the redirect with `state=` stripped, and the full `www-authenticate` header (no `cut`). For `/.well-known/*`, also print the body as `jq -cS .`, or `body=<non-json>` when it isn't JSON.
  - Update the usage line in the script and in `SKILL.md`. Add one sentence under "Regression test": the MCP block exists, and its bodies must be read, not just diffed.
- [ ] **Step 6: Capture the baseline.**
  - Run `cd .claude/skills/edge-ingress && nix-shell -p openssl jq --run ./edge-snapshot.sh > $SCRATCH/edge-0.txt`.
  - Expected MCP lines:
    - both `/gramps/mcp` lines: 401 with `Bearer error="invalid_token"`;
    - PRM: 200 with `"authorization_servers":["https://mcp.chrismiller.xyz/"]` and no `scopes_supported`;
    - the mcp AS document: 200 with `"issuer":"https://dex.chrismiller.xyz"`;
    - the dex AS document: 404;
    - `openid-configuration`: 200 with four grant types and `["S256","plain"]`;
    - **register: GET 403 and POST 403**;
    - authorize: 302 to `https://dex.chrismiller.xyz/auth?scope=openid+profile+email`.
- [ ] **Step 7: Commit and push.** `git add .renovaterc.json .pre-commit-config.yaml .claude/skills/edge-ingress/ && nix develop -c git commit -m "0a guardrails: own Renovate groups for dex/oauth2-proxy/mcp-jwt-auth, reserve mcp- client ids, MCP edge snapshot"`. Check the gh user, then push.
- [ ] **Step 8: Confirm the split (non-blocking; must hold before Task 4).** After Renovate's next run, `gh pr view 2662 --json files --jq '.files[].path'` must list no `cluster/dex/`, `cluster/oauth2-proxy/` or `images/mcp-jwt-auth/` file. Their new PRs exist and are **not merged**; Task 4 does Dex by hand, and the oauth2-proxy bump waits until after 0a.

---

### Task 1: `mcp-probe`, and the audience spike (decides Path A or Branch B)

**Files:**
- Create: `tools/mcp-probe/mcp_probe.py` (stdlib only), `tools/mcp-probe/tests/test_mcp_probe.py`

**Interfaces:**
- Produces this CLI, used by Tasks 2–12: `python3 tools/mcp-probe/mcp_probe.py <cmd>`, with `--resource` defaulting to `https://mcp.chrismiller.xyz/gramps/mcp`.
  - `discover` prints JSON `{resource_metadata, authorization_server, issuer, registration_endpoint, authorization_endpoint, token_endpoint, agree}`. It exits 1 when `issuer != authorization_servers[0]` (exact string compare).
  - `register --name N (--port P | --redirect URI) [--endpoint URL]` prints the status and the response JSON, and exits 1 on anything but 201. `--endpoint` defaults to the discovered `registration_endpoint`; a port-forward URL goes here while registration is blocked publicly.
  - `login --client-id ID --port P [--authorize-url URL]` runs PKCE S256 with `scope=openid email profile offline_access`. It prints the authorize URL, captures the code on `http://localhost:P/callback`, and exchanges it at `token_endpoint`.
    - Tokens are stored at `${XDG_RUNTIME_DIR:-~/.cache}/mcp-probe/<ID>.json` (dir 0700, file 0600).
    - It prints `claims_summary`.
    - `--authorize-url https://dex.chrismiller.xyz/auth` skips the discovered endpoint. That is the only way to log in a client the facade's shim refuses.
  - `call (--client-id ID | --no-token) [--url URL]` POSTs JSON-RPC `initialize` to `--url` (default: the resource), then prints the status and `www-authenticate`.
  - `refresh --client-id ID` runs the refresh grant, stores the result, and prints `{lifetime_s, refresh_rotated}`.
  - Pure functions: `pkce_pair() -> tuple[str, str]`, `parse_bearer_challenge(h: str) -> dict[str, str]`, `as_metadata_urls(issuer: str) -> list[str]`, `claims_summary(jwt: str, has_refresh: bool) -> dict`.
  - It never prints a token, an email or a refresh token.

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

- [ ] **Step 2: Run the tests and watch them fail.** `nix develop -c python -m pytest -q tools/mcp-probe`. Expected: ImportError.
- [ ] **Step 3: Implement `mcp_probe.py`** with `urllib.request`, `http.server` (a one-shot loopback handler), `secrets`, `hashlib` and `json`. `discover` follows the MCP 2025-11-25 order:
  1. an unauthenticated POST;
  2. `resource_metadata` from the challenge, falling back to the path-insert PRM URL;
  3. the PRM;
  4. `as_metadata_urls(authorization_servers[0])`, where the first 200 wins.
- [ ] **Step 4: Run the tests and watch them pass.** Same command. Expected: 4 passed.
- [ ] **Step 5: Commit and push.** `nix develop -c git commit -m "tools: mcp-probe, a stdlib OAuth/MCP probe for the 0a verifications"`.
- [ ] **Step 6: Reproduce live problem 4.** `nix develop -c python3 tools/mcp-probe/mcp_probe.py discover`. Expected: exit 1, with `authorization_server: "https://mcp.chrismiller.xyz/"` and `issuer: "https://dex.chrismiller.xyz"`.
- [ ] **Step 7: The spike, with no registration (M1; Chris does the browser step).**
  - Run `login --client-id M4HNWEXI6EFRW7EXBJFYENJVL2 --port 64336 --authorize-url https://dex.chrismiller.xyz/auth`. That is the existing public client, on its registered loopback port; Dex accepts only that exact redirect (finding 14).
  - Note: Dex keeps one refresh token per (user, client), so this login replaces any refresh token Claude Code holds for M4HN. Finding 7 says there are none.
- [ ] **Step 8: Decide.** Take **Path A** and continue with Task 2 if `aud == "M4HNWEXI6EFRW7EXBJFYENJVL2"` (a string or a one-element list), or if `aud` has several entries with `azp` equal to it. Otherwise take **Branch B** (below). Expected: Path A.
  - Also record `lifetime_s` (≈86400) and `has_refresh_token`. If the latter is false, Google refused offline access; stop and look into it before Task 11.
- [ ] **Step 9: Prove the probe end to end.** `call --client-id M4HNWEXI6EFRW7EXBJFYENJVL2` should return 200. The negative check ("a token for another client is rejected") needs a non-facade token, and none can be had without registering, so it waits for Task 12, when M4HN is retired.

---

### Branch B (only if Task 1 Step 8 fails): minimal mcp-edge re-minting audience-bound tokens

Branch B replaces the audience rule in Tasks 2 and 3 and the metadata in Tasks 5 and 6. Everything else (consent, DCR allowlist, the client cache, expiry, the stopgap ordering) is unchanged. Order: B1, then B2 (deployed with Task 6), then the reconnects (Task 10), then B3.

- **B1, token endpoint (`images/mcp-jwt-auth/token.go`):**
  - `newTokenHandler(dexTokenURL string, idv *oidc.IDTokenVerifier, signer jose.Signer, check facadeChecker, list *allowlist) http.Handler` serves `POST /oauth/token` for the `authorization_code` and `refresh_token` grants. It forwards the form unchanged to Dex's in-cluster `/token`.
  - It verifies the returned `id_token`: its audience must pass `check`, and its email must be on the allowlist. It then mints an ES256 JWT: `iss=https://mcp.chrismiller.xyz`, `aud=https://mcp.chrismiller.xyz/gramps/mcp`, `sub`, `email`, `client_id`, `jti`, `exp=now+1h`.
  - It returns `{access_token: <minted>, token_type: "Bearer", expires_in: 3600, refresh_token: <Dex's, unchanged>, scope}`, never Dex's access or ID token. `GET /oauth/jwks` publishes the public key.
  - The key lives in `cluster/oauth2-proxy/mcp-edge-signing.secret.yaml`, sealed with `./sign.sh`.
  - Tests: `TestToken_ReMintsAudienceBoundToken`, `TestToken_RejectsNonFacadeIDToken`, `TestToken_RefreshTokenPassesThrough`, `TestToken_NeverReturnsDexTokens`.
- **B2, metadata:** the AS document is served on **mcp.chrismiller.xyz** only, with `issuer: "https://mcp.chrismiller.xyz"` and with `token_endpoint` and `jwks_uri` on the facade. The PRM says `authorization_servers: ["https://mcp.chrismiller.xyz"]`. There is no dex-host Ingress. Test: `TestAgreement` with the facade as issuer.
- **B3, resource rule:**
  - mcp-jwt-auth and gramps-mcp verify against the facade JWKS: `iss=https://mcp.chrismiller.xyz`, `aud == https://mcp.chrismiller.xyz/gramps/mcp`, and the allowlist. They reject every Dex token. gramps-mcp's `algorithms` becomes `["ES256"]`.
  - In hyprmcp, set `proxy[0].authentication.enabled: false`, because its signature check uses Dex's JWKS.
  - The test tables in Tasks 2 and 3 swap the client-id rows for resource-audience rows.

---

### Task 2: mcp-jwt-auth requires a facade audience and points 401s at the resource metadata

**Files:**
- Create: `images/mcp-jwt-auth/config.go`, `config_test.go`, `lists.go`, `lists_test.go`, `verify.go`, `verify_test.go`, `testdata/list-vectors.json`
- Create: `cluster/oauth2-proxy/mcp-grandfathered-clients.txt`. It holds both ids from Task 0, one per line, each with a `#` comment naming the client and its fate (ChatGPT kept; Claude Code to be retired in Task 12). Its header says this is the source of record.
- Create: `scripts/mirror-mcp-grandfathered.sh` (writes `cluster/gramps/mcp-grandfathered-clients.txt` with a `# GENERATED … do not edit` header; exits 1 if it changed the file, formatter-style); `cluster/gramps/mcp-grandfathered-clients.txt` (generated)
- Modify: `.pre-commit-config.yaml`. Add hook `mirror-mcp-grandfathered` (`language: system`, `pass_filenames: false`, `files: ^cluster/(oauth2-proxy|gramps)/mcp-grandfathered-clients\.txt$`), next to `mirror-family-list`.
- Modify: `images/mcp-jwt-auth/main.go` (wiring only); `Dockerfile` (`COPY *.go ./` and `COPY testdata ./testdata`, then `RUN CGO_ENABLED=0 go test ./...` before `go build`)
- Modify, in a second commit:
  - `cluster/oauth2-proxy/kustomization.yaml` (`configMapGenerator: mcp-grandfathered-clients` from `grandfathered=mcp-grandfathered-clients.txt`);
  - `cluster/gramps/kustomization.yaml` (`configMapGenerator: gramps-mcp-grandfathered-clients` from the generated copy; unused until Task 7);
  - `cluster/oauth2-proxy/mcp-jwt-auth.yaml` (pin, env, volume).
- Modify: `docs/gramps.md` (the "Why this needs three components" paragraphs and Commands)

**Interfaces:**
- Produces (package `main`):
  - `func parseList(text string, fold bool) map[string]struct{}` cuts each line at the first `#`, trims it, drops empty entries, and lowercases only when `fold`. The `allowlist` (moved from `main.go`) uses it with `fold=true`, and `permits(email)` trims and lowercases the claim.
  - `type config struct{ Issuer, EmailsFile, GrandfatheredFile, Addr, PublicBaseURL, ClientPrefix string; Resources []string; Grandfathered map[string]struct{} }`, plus the Task 5 fields.
  - `func loadConfig(lookup func(string) (string, bool), readFile func(string) ([]byte, error)) (config, error)` reads this env:
    - `OIDC_ISSUER` (required; the error text is `OIDC_ISSUER is required`, which CI greps);
    - `EMAILS_FILE`; `ADDR`;
    - `PUBLIC_BASE_URL` (required; one trailing `/` is trimmed);
    - `MCP_RESOURCES` (a comma list, default `/gramps/mcp`);
    - `CLIENT_ID_PREFIX` (default `mcp-`; **set but empty is an error**);
    - `GRANDFATHERED_CLIENTS_FILE` (default `/etc/mcp-clients/grandfathered`). The file is parsed with `fold=false`. An unreadable file logs an error and yields an empty set, which fails closed.
  - `type facadeChecker func(ctx context.Context, clientID string) (ok bool, reason string)`. `func prefixChecker(c config) facadeChecker` returns true iff the id is grandfathered, or iff `strings.HasPrefix(id, c.ClientPrefix) && len(id) > len(c.ClientPrefix)`. It is case-sensitive. Task 5 replaces it at the edge.
  - `func clientIDOf(aud []string, azp string) (string, bool)` mirrors Dex `GetClientID`: one audience gives `aud[0]`; several give `azp`, which must be non-empty; none gives false.
  - `type identity struct{ Email, ClientID string }`; `var errNoToken, errInvalidToken, errWrongAudience, errNotAllowlisted error`.
  - `func authorizeBearer(ctx context.Context, v *oidc.IDTokenVerifier, check facadeChecker, list *allowlist, header string) (identity, error)`. The verifier keeps `SkipClientIDCheck: true`; the audience rule replaces it.
  - `func (c config) resourceMetadataURL(resource string) string` returns `PublicBaseURL + "/.well-known/oauth-protected-resource" + resource`.
  - `func challenge(cfg config, forwardedURI string, err error) (status int, wwwAuthenticate string)`. When it emits `resource_metadata`, that parameter comes **last**.
- Produces: `testdata/list-vectors.json`, shape `{"cases":[{"name","fold","text","want":[…]}],"permits":[{"list","email","want"}]}`. Task 3's Python tests consume the same file.

- [ ] **Step 1: Write the failing tests.** Tokens are signed in-test (RS256) with go-jose. The verifier is `oidc.NewVerifier(iss, &oidc.StaticKeySet{PublicKeys: …}, &oidc.Config{SkipClientIDCheck: true})`.

```go
// list-vectors.json cases (TestListVectors iterates them):
// "lowercase-trim" fold  "  Chris@Example.COM \n"         → ["chris@example.com"]
// "inline-comment" fold  "a@x.test # Erin\n"              → ["a@x.test"]
// "comments-blank" fold  "# family\n\n  \n"               → []
// "crlf"           fold  "a@x.test\r\nb@x.test\r\n"       → ["a@x.test","b@x.test"]
// "empty"          fold  ""                               → []
// "ids-keep-case"  !fold "M4HNWEXI6EFRW7EXBJFYENJVL2  # Claude Code\n" → ["M4HNWEXI6EFRW7EXBJFYENJVL2"]
// permits: list "a@x.test\n", email " A@X.test " → true; email "a@x.test.evil" → false; list "" any → false
// TestClientIDOf
{[]string{"mcp-a"}, "", "mcp-a", true}, {[]string{"immich"}, "", "immich", true},
{[]string{"immich", "mcp-x"}, "immich", "immich", true}, {nil, "", "", false},
{[]string{"a", "b"}, "", "", false},
// TestPrefixChecker (prefix "mcp-", grandfathered {7QITXD4SBM2ZFXDP5TLPRJGZZ3, M4HNWEXI6EFRW7EXBJFYENJVL2})
"mcp-ABC"→true, "M4HNWEXI6EFRW7EXBJFYENJVL2"→true, "m4hnwexi6efrw7exbjfyenjvl2"→false,
"immich"→false, "oauth2-proxy-family"→false, "MCP-abc"→false, "mcp-"→false
// TestAuthorizeBearer: name → want
"facade-allowlisted"→identity{Email, "mcp-a"}, "grandfathered"→ok,
"aud-immich"→errWrongAudience, "multi-aud-hides-facade" (aud [immich mcp-x], azp immich)→errWrongAudience,
"expired"→errInvalidToken, "wrong-issuer"→errInvalidToken, "bad-signature"→errInvalidToken,
"not-allowlisted"→errNotAllowlisted, "email-unverified"→errNotAllowlisted,
"empty-allowlist"→errNotAllowlisted, "no-header"→errNoToken, "bearer-empty"→errNoToken
// TestChallenge (base https://mcp.chrismiller.xyz, resources [/gramps/mcp])
errNoToken, "/gramps/mcp?x=1" → 401, `Bearer scope="openid email profile offline_access", resource_metadata="https://mcp.chrismiller.xyz/.well-known/oauth-protected-resource/gramps/mcp"`
errWrongAudience, "/gramps/mcp" → 401, `Bearer error="invalid_token", resource_metadata="…/gramps/mcp"`
errNotAllowlisted → 403, ""          errInvalidToken, "/other" → 401, `Bearer error="invalid_token"`
// TestLoadConfig: no OIDC_ISSUER → "OIDC_ISSUER is required"; CLIENT_ID_PREFIX="" → error;
// PUBLIC_BASE_URL "https://mcp.chrismiller.xyz/" → "https://mcp.chrismiller.xyz"; unreadable grandfathered file → empty set, no error
```

- [ ] **Step 2: Run the tests and watch them fail.** `nix develop -c sh -c 'cd images/mcp-jwt-auth && go test ./...'`. Expected: undefined symbols.
- [ ] **Step 3: Implement.**
  - `/auth` calls `authorizeBearer` with `prefixChecker(cfg)`. On success it returns 202 with `X-Auth-Request-Email`.
  - On error it writes `challenge(...)` and a JSON `{"error": …}`. It reads `X-Forwarded-Uri` only to choose among the configured resources.
  - Log the decision, the reason and the client id. Never log the token or the email.
- [ ] **Step 4: Run the tests and watch them pass.** Same command.
- [ ] **Step 5: Mirror check.** Run `scripts/mirror-mcp-grandfathered.sh` twice. The first run writes the gramps copy and exits 1. The second run exits 0 and leaves `git status` clean for that file.
- [ ] **Step 6: Commit and publish.**
  - `git add images/mcp-jwt-auth scripts/mirror-mcp-grandfathered.sh cluster/oauth2-proxy/mcp-grandfathered-clients.txt cluster/gramps/mcp-grandfathered-clients.txt .pre-commit-config.yaml`
  - `nix develop -c git commit -m "mcp-jwt-auth: require a facade-created audience; 401s carry resource_metadata; one grandfathered list"`, then push.
  - Wait for `mcp-jwt-auth image` with `Monitor` (`gh run watch`).
  - Resolve the digest of `sha-$(git rev-parse --short=7 HEAD)`. Get an anonymous GHCR token from `https://ghcr.io/token?scope=repository:christopherjmiller/mcp-jwt-auth:pull`, `curl -sI` the manifest with `Accept: application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json`, and read `docker-content-digest`.
- [ ] **Step 7: Pin and configure (second commit).**
  - Add the two configMapGenerators.
  - In `mcp-jwt-auth.yaml`:
    - set `image: …:sha-<7>@sha256:<digest>`;
    - add env `PUBLIC_BASE_URL=https://mcp.chrismiller.xyz`, `MCP_RESOURCES=/gramps/mcp`, `CLIENT_ID_PREFIX=mcp-` and `GRANDFATHERED_CLIENTS_FILE=/etc/mcp-clients/grandfathered`;
    - mount the `mcp-grandfathered-clients` ConfigMap read-only at `/etc/mcp-clients`;
    - fix the header comment, since "Audience is deliberately NOT checked" is no longer true.
  - `nix develop -c git commit -m "mcp: pin mcp-jwt-auth sha-<7> (audience rule)"`, then push. Watch the rollout with `Monitor` until 2/2 Ready.
- [ ] **Step 8: Verify.**
  - `call --no-token` returns 401, and its challenge ends with `resource_metadata="https://mcp.chrismiller.xyz/.well-known/oauth-protected-resource/gramps/mcp"`.
  - `call --client-id M4HNWEXI6EFRW7EXBJFYENJVL2` returns 200, and the logs show `allowed … client_id=M4HN…`.
  - Chris's Claude Code runs one Gramps read tool, and it works.
  - Re-run the snapshot to `edge-2.txt` and diff it against `edge-0.txt`. Only the two `/gramps/mcp` challenge lines may change.
- [ ] **Step 9: Docs.** In `docs/gramps.md`, replace "Audience is deliberately unchecked" with the facade-client rule: the grandfathered file and its mirror, the `mcp-` prefix, the azp rule, and that Task 5 adds the Dex-side check. Add `tools/mcp-probe` to Commands. Commit `docs: MCP audience rule`.

---

### Task 3: gramps-mcp authenticates its own callers (code and image only; deployed in Task 7)

**Files:**
- Create: `patches/gramps-mcp-inbound-auth.patch`.
  - It adds `src/gramps_mcp/inbound_auth.py` and `tests/test_inbound_auth.py`.
  - In `src/gramps_mcp/server.py`, it replaces the HTTP branch's `app.run(transport="streamable-http")` with `uvicorn.run(InboundAuth(app.streamable_http_app(), InboundAuthConfig.from_env(os.environ)), host="0.0.0.0", port=8000)`.
  - Generate it against the tree with every current patch applied, transport-security included.
- Modify: `flake.nix`:
  - append `./patches/gramps-mcp-inbound-auth.patch` after the transport-security patch;
  - in the same `overrideAttrs`, add `postInstall = (old.postInstall or "") + ''wrapProgram "$out/bin/gramps-mcp" --prefix PYTHONPATH : ${pkgs.python3Packages.makePythonPath [ pkgs.python3Packages.cryptography ]}'';`;
  - add `packages.gramps-mcp-src = pkgs.applyPatches { name = "gramps-mcp-src"; src = gramps-mcp.src; patches = gramps-mcp.patches; }`;
  - add `checks = forAllSystems (pkgs: { gramps-mcp-inbound-auth = pkgs.runCommand … })`. It runs `python -m pytest -q -p no:cacheprovider tests/test_inbound_auth.py` inside `gramps-mcp-src`, with `python3.withPackages (ps: [ps.pytest ps.pyjwt ps.cryptography ps.starlette ps.httpx ps.anyio])` and `MCP_LIST_VECTORS=${./images/mcp-jwt-auth/testdata/list-vectors.json}`.
- Modify: `.github/workflows/gramps-mcp-image.yml`:
  - add `patches/**` and `images/mcp-jwt-auth/testdata/**` to both `paths` lists;
  - before the image build, add a step `nix build .#checks.x86_64-linux.gramps-mcp-inbound-auth -L`;
  - run the probe container with `-e MCP_OIDC_ISSUER=https://dex.chrismiller.xyz -e MCP_EMAILS_FILE=/dev/null -e MCP_RESOURCE_METADATA_URL=https://mcp.chrismiller.xyz/.well-known/oauth-protected-resource/gramps/mcp`, and assert that `POST /mcp` returns `401` with a `www-authenticate:` line ending in `resource_metadata="…"`;
  - add a second run without `MCP_OIDC_ISSUER` that must exit non-zero, with `MCP_OIDC_ISSUER is required` in its log.

**Interfaces:**
- Consumes: the Task 2 rule and `list-vectors.json`. The semantics are identical; they are restated here because the code is Python.
- Produces (`inbound_auth.py`, with no package-relative imports, so tests import it via `sys.path.insert(0, "src/gramps_mcp")`):
  - `def parse_list(text: str, fold: bool) -> frozenset[str]`, the same rule as Go `parseList`.
  - `@dataclass(frozen=True) class InboundAuthConfig: issuer: str; jwks_url: str; emails_file: str; client_prefix: str; grandfathered: frozenset[str]; resource_metadata_url: str | None; leeway_s: int`.
  - `InboundAuthConfig.from_env(env: Mapping[str, str]) -> InboundAuthConfig` reads:
    - `MCP_OIDC_ISSUER` (required, otherwise `SystemExit("MCP_OIDC_ISSUER is required")`);
    - `MCP_OIDC_JWKS_URL` (default `<issuer>/keys`);
    - `MCP_EMAILS_FILE` (default `/etc/gramps-mcp/emails`);
    - `MCP_CLIENT_ID_PREFIX` (default `mcp-`; empty means SystemExit);
    - `MCP_GRANDFATHERED_CLIENTS_FILE` (default `/etc/gramps-mcp-clients/grandfathered`; parsed with `fold=False`; unreadable means empty);
    - `MCP_RESOURCE_METADATA_URL`;
    - `MCP_TOKEN_LEEWAY_SECONDS` (default 60).
  - `def client_id_of(claims: Mapping) -> str | None`.
  - `class Allowlist(path: str, ttl_s: float = 30)` with `.permits(email: str) -> bool`. It re-reads when stale. An unreadable or empty file denies everyone.
  - `class InboundAuth(app, config, key_resolver: Callable[[str], Any] | None = None, clock: Callable[[], float] = time.time)` is an ASGI callable.
    - `lifespan` and other non-HTTP scopes pass straight through.
    - HTTP requests need `Authorization: Bearer`. A missing token, or **any** exception during verification, gets 401 `{"error":"invalid_token"}` with `WWW-Authenticate: Bearer error="invalid_token"`, plus `, resource_metadata="<url>"` **last** when configured. The log records only the exception class.
    - A valid token whose subject is not permitted gets 403 `{"error":"forbidden"}`.
    - Verification calls `jwt.decode(..., algorithms=["RS256"], issuer=..., leeway=leeway_s, options={"require": ["exp","iat","iss","aud"], "verify_aud": False})`, then the facade rule (grandfathered, or the prefix), then the allowlist. It runs in `anyio.to_thread.run_sync`.
    - The default `key_resolver` wraps `jwt.PyJWKClient(jwks_url, lifespan=3600, timeout=5).get_signing_key_from_jwt(t).key`.
    - At import, the module raises `RuntimeError` unless `jwt.algorithms.has_crypto` is true, so a missing `cryptography` fails closed at start.
  - gramps-mcp checks only the prefix and the grandfathered list, and holds no Dex API credential. The edge's `ListClients` check (Task 5) is the public line; this is the in-cluster line.

- [ ] **Step 1: Get the base tree.** Add the `gramps-mcp-src` output first, before the patch is in the list. Then `nix build .#gramps-mcp-src -o $SCRATCH/gms-base`, and `cp -r` it to `$SCRATCH/a` and `$SCRATCH/b`.
- [ ] **Step 2: Write the failing tests** in `$SCRATCH/b/tests/test_inbound_auth.py`. Use an in-test RSA key, a `key_resolver` returning its public key, a fake clock, and `starlette.testclient.TestClient` around an inner app that returns 200.

```python
def test_missing_bearer_is_401_with_resource_metadata():
    r = client().post("/mcp", json={})
    assert r.status_code == 401
    assert r.headers["www-authenticate"].endswith('resource_metadata="https://mcp.chrismiller.xyz/.well-known/oauth-protected-resource/gramps/mcp"')
def test_list_vectors():                  # every case in $MCP_LIST_VECTORS (pytest.fail if unset)
def test_facade_client_passes():          assert call(aud="mcp-a").status_code == 200
def test_grandfathered_client_passes():   assert call(aud="M4HNWEXI6EFRW7EXBJFYENJVL2").status_code == 200
def test_other_client_is_401():           assert call(aud="immich").status_code == 401
def test_multi_aud_hiding_facade_client_is_401(): assert call(aud=["immich", "mcp-x"], azp="immich").status_code == 401
def test_wrong_issuer_is_401():           assert call(iss="https://evil.example").status_code == 401
def test_expired_within_leeway_passes():  assert call(exp=NOW - 30).status_code == 200
def test_expired_beyond_leeway_is_401():  assert call(exp=NOW - 61).status_code == 401
def test_alg_none_is_401():               assert raw_call(unsigned_token(aud="mcp-a")).status_code == 401
def test_hs256_key_confusion_is_401():    assert raw_call(hs256_token(secret=PUBLIC_PEM, aud="mcp-a")).status_code == 401
def test_es256_is_401():                  assert raw_call(es256_token(aud="mcp-a")).status_code == 401
def test_unknown_kid_is_401():            assert client(resolver=raises(jwt.PyJWKClientError)).post("/mcp", headers=bearer()).status_code == 401
def test_jwks_unreachable_is_401():       assert client(resolver=raises(jwt.PyJWKClientConnectionError)).post("/mcp", headers=bearer()).status_code == 401
def test_resolver_crash_is_401():         assert client(resolver=raises(RuntimeError)).post("/mcp", headers=bearer()).status_code == 401
def test_not_allowlisted_is_403():        assert call(email="stranger@example.com").status_code == 403
def test_email_unverified_is_403():       assert call(email_verified=False).status_code == 403
def test_empty_allowlist_denies():        assert call(allowlist="").status_code == 403
def test_lifespan_passes_through():       # TestClient context manager: inner lifespan startup ran
def test_from_env_requires_issuer():      pytest.raises(SystemExit, InboundAuthConfig.from_env, {})
def test_empty_prefix_refused():          pytest.raises(SystemExit, InboundAuthConfig.from_env, {"MCP_OIDC_ISSUER": "x", "MCP_CLIENT_ID_PREFIX": ""})
```

- [ ] **Step 3: Generate a test-only patch and confirm it fails.**
  - `cd $SCRATCH && diff -ruN a b > ~/Repos/luma-homeops/patches/gramps-mcp-inbound-auth.patch` (paths `a/…` and `b/…`, applied with `-p1`).
  - Add it to `flake.nix`, and `git add` both, because flake paths must be tracked.
  - `nix build .#checks.x86_64-linux.gramps-mcp-inbound-auth -L`. Expected: FAIL with `ModuleNotFoundError: inbound_auth`.
- [ ] **Step 4: Implement** `b/src/gramps_mcp/inbound_auth.py` and the `server.py` wiring. Regenerate the patch, add the `postInstall` wrap, and rebuild the check. Expected: PASS, 21 tests.
- [ ] **Step 5: Smoke-test locally.**
  - `nix build .#gramps-mcp && MCP_OIDC_ISSUER=https://dex.chrismiller.xyz MCP_EMAILS_FILE=/dev/null ./result/bin/gramps-mcp &`.
  - `curl -si -X POST localhost:8000/mcp -d '{}'` should show `HTTP/1.1 401`.
  - Without `MCP_OIDC_ISSUER`, it should exit non-zero with the message.
  - Kill the server afterwards.
- [ ] **Step 6: Commit and publish (no deploy).**
  - `git add patches/gramps-mcp-inbound-auth.patch flake.nix .github/workflows/gramps-mcp-image.yml && nix develop -c git commit -m "gramps-mcp: validate the bearer itself (Dex JWKS RS256, facade audience, family allowlist)"`, then push.
  - Wait for the workflow with `Monitor`. Record the tag and digest from its "Tag and push" step for Task 7. **Do not pin yet** (finding 12: hyprmcp still owns the public PRM).

---

### Task 4: Dex v2.44.0 → v2.46.0 (image, strict flags, HA shape) — its own day

**Files:**
- Modify: `cluster/dex/dex.yaml`:
  - image `ghcr.io/dexidp/dex:v2.46.0@sha256:<re-resolved>`;
  - env `DEX_CONFIG_DISALLOW_UNKNOWN_FIELDS: "true"` and `DEX_CONTINUE_ON_CONNECTOR_FAILURE: "false"`. The comment explains: a typo or a broken connector fails the *new* pod, while `maxUnavailable: 0` keeps the old ones serving. The cost is that a WAN outage during a restart keeps Dex down rather than up without Google;
  - `strategy: {type: RollingUpdate, rollingUpdate: {maxUnavailable: 0, maxSurge: 1}}`;
  - `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution` on `kubernetes.io/hostname` for `app.kubernetes.io/name: dex`. It is preferred, not required, so a cordoned node can't stall the surge pod;
  - a new document: `policy/v1 PodDisruptionBudget` `dex`, `minAvailable: 1`, with the same selector.
- Modify: `docs/auth.md` ("Multi-provider: Dex": the version, both flags, the PDB, the Renovate group, the local check and the post-rollout check)

**Interfaces:**
- Produces: Dex 2.46 running with the unchanged config. The CRDs `useridentities.dex.coreos.com` and `authsessions.dex.coreos.com` exist (Dex creates them).
- Produces two named procedures that Tasks 9, 11 and 12 reuse.
  - **"Dex local check."** Run the pinned 2.46 image against a scratch copy of the real config, with `storage` swapped to memory and `grpc` removed:
    1. Write `$SCRATCH/dex-local.yaml` under `umask 077` with `nix develop -c python3`. Never print it.
    2. `docker run --rm -d --name dex-local -p 127.0.0.1:15556:5556 -e DEX_CONFIG_DISALLOW_UNKNOWN_FIELDS=true -e DEX_CONTINUE_ON_CONNECTOR_FAILURE=false [-e DEX_SESSIONS_ENABLED=true] -v $SCRATCH/dex-local.yaml:/etc/dex/config.yaml:ro <image> dex serve /etc/dex/config.yaml`.
    3. Pass: the logs show `config connector` for `google` and `microsoft`, `listening on` for `http`, and no `level=ERROR`. `curl -s 127.0.0.1:15556/.well-known/openid-configuration` returns 200.
    4. Then `docker rm -f dex-local && shred -u $SCRATCH/dex-local.yaml`.
  - **"Dex post-rollout check."**
    - For **each** pod: its logs have no `level=ERROR`, and they list both connectors.
    - Through `kubectl -n dex port-forward pod/<p> 15557:5556`, `curl -s 'http://127.0.0.1:15557/auth?client_id=oauth2-proxy-admin&redirect_uri=https%3A%2F%2Fauth-admin.chrismiller.xyz%2Foauth2%2Fcallback&response_type=code&scope=openid&state=probe' | grep -oE 'Log in with (Google|Microsoft)' | sort -u` prints both lines.
    - `kubectl -n dex get pdb dex` shows `ALLOWED DISRUPTIONS 1`.

- [ ] **Step 1: Gate.**
  - Task 0 Step 8 holds: #2662 has been split and nothing is merged.
  - Run `cluster-snapshot`. All nodes are Ready and none is SchedulingDisabled (S5).
  - This is not the day of Task 9.
- [ ] **Step 2: Re-resolve the digest.** Use the anonymous GHCR token for `dexidp/dex`, with an index Accept header, on `manifests/v2.46.0`. Expect `sha256:933fcd3f…2001777`; if it differs, tell Chris.
- [ ] **Step 3: Run the Dex local check** without the sessions flag. It must pass.
- [ ] **Step 4: Edit, commit and push.** `dex: v2.46.0 (strict config, fail on connector error, maxUnavailable 0, PDB)`. Watch `rollout status deploy/dex` with `Monitor`. If a new pod CrashLoops, the old pods keep serving. Read its log, fix the cause (resealing per Task 9 Step 3), and do not roll back blindly.
- [ ] **Step 5: Verify.**
  - Run the Dex post-rollout check. Neither pod's log may warn that `grpc.tlsClientCA` is unset.
  - `kubectl get crd | grep -E 'useridentities|authsessions'` lists both.
  - Chris signs in **in a private window** (oauth2-proxy's 168 h cookie skips Dex otherwise) at:
    - an admin host (auth-admin, Google);
    - a family host (auth-family);
    - `family.werethemille.rs` (the family-rs gate, then the Gramps OIDC button);
    - `photos.chrismiller.xyz`;
    - and, with **a Microsoft account**, any one host. An allowlist 403 after Dex returns still proves the connector.
  - A Claude Code Gramps tool call works (a one-off re-auth is acceptable).
  - `discover` is unchanged (it still disagrees).
  - The snapshot diff against `edge-2.txt` may change only the `dex.chrismiller.xyz/` line and the `openid-configuration` body.
- [ ] **Step 6: Rollback, if needed.** Revert the commit and sync dex through Argo local admin (see Global Constraints). The new CRDs are harmless to 2.44.
- [ ] **Step 7: Docs, and commit.**

---

### Task 5: The facade (code): redirect rules, the client cache, DCR, authorize shim, PRM, AS metadata, metrics

**Files:**
- Create: `images/mcp-jwt-auth/redirects.go`, `clients.go`, `register.go`, `oauth_authorize.go`, `metadata.go`, `metrics.go` and `dextls.go`, each with a `_test.go`.
- Modify:
  - `images/mcp-jwt-auth/config.go` (new env);
  - `main.go` (routes; the gRPC dial; the edge uses `newFacadeChecker` instead of `prefixChecker`);
  - `go.mod`/`go.sum` (`github.com/dexidp/dex/api/v2@v2.4.0`, `google.golang.org/grpc`, `golang.org/x/time`; run `go mod tidy` in `nix develop`).

**Interfaces:**
- Consumes: `config`, `facadeChecker`, `parseList`, `authorizeBearer` (Task 2).
- Produces:
  - `type redirectRule struct{ Kind, Value string; Port int }`, where Kind is `exact`, `segment` or `loopback-port`.
  - `func parseRedirectRules(r io.Reader) ([]redirectRule, error)` takes one rule per line, `<kind> <args>`, and allows a trailing `# comment`.
    - `exact` takes an `https://` URL with a host and no query or fragment.
    - `segment` takes an `https://` prefix with a host, ending in `/`.
    - `loopback-port <port> <path>` takes a port of 1024–65535 and a path starting with `/`.
    - An unknown kind or a bad argument is an error, so the new pod fails and the old pods keep serving.
  - `func matchRedirect(rules []redirectRule, raw string) (displayHost string, ok bool)`:
    - `exact` is byte equality with `raw`.
    - `segment` requires `strings.HasPrefix(raw, v)` and a remainder matching `^[A-Za-z0-9_-]{1,128}$`.
    - `loopback-port` requires that `url.Parse` succeeds, and that the URL has scheme `http`, no userinfo, no query, fragment or opaque part, and hostname exactly `localhost` or `127.0.0.1`. It also requires `u.Port() == strconv.Itoa(rule.Port)`, a path equal to the value, **and** `raw == fmt.Sprintf("http://%s:%d%s", host, port, path)`.
    - The display host is the URL host for `exact` and `segment`, and `localhost (this computer)` for `loopback-port`.
  - `func consentName(clientName string, hosts []string) string` returns `strings.Join(dedup(hosts), ", ") + " — " + clean(clientName)`. `clean` drops Unicode Cc and Cf (bidi controls included), collapses whitespace, caps at 48 runes plus `…`, and falls back to `MCP client` when empty.
  - `type clientRegistry interface` is the `CreateClient` and `ListClients` subset of `api.DexClient`.
  - `func newClientCache(reg clientRegistry, rules []redirectRule, prefix string, ttl, maxStale time.Duration, now func() time.Time, m *facadeMetrics, log *slog.Logger) *clientCache`, with three methods:
    - `check(ctx, id string) (ok bool, reason string)`, for prefixed ids.
      - It refreshes `ListClients` when the cached list is older than `ttl`, singleflight. An unknown id forces at most one refresh per 5 s.
      - ok iff the client exists, is `Public`, has `len(TrustedPeers) == 0`, has 1–3 `RedirectUris`, and every redirect URI passes `matchRedirect`.
      - A violation increments `mcp_facade_client_violations_total`, logs ERROR once per id per hour, and returns `invariant:<field>`.
      - If the list is older than `maxStale` and the refresh fails, it returns `(false, "dex-unavailable")`.
    - `facadeCount(ctx) (int, error)` counts all prefixed clients, valid or not.
    - `invalidate()`.
  - `func newFacadeChecker(cfg config, cache *clientCache) facadeChecker`: a grandfathered id is ok without calling Dex; a prefixed id goes to `cache.check`; anything else is false.
  - `func newRegisterHandler(reg clientRegistry, cache *clientCache, rules []redirectRule, lim *rate.Limiter, maxClients int, prefix string, m *facadeMetrics, log *slog.Logger) http.Handler`.
    - Results map to metric labels `created`, `invalid_redirect_uri`, `invalid_client_metadata`, `rate_limited`, `cap_reached`, `dex_error` and `dex_unavailable`.
    - `CreateClientResp.AlreadyExists == true` is `dex_error`, answered 502 `{"error":"server_error"}` with no id in the body.
    - On success, it calls `cache.invalidate()`.
  - `func newAuthorizeHandler(dexAuthURL string, check facadeChecker) http.Handler`.
  - `func newPRMHandler(publicBase, issuer string, resources []string) http.Handler`.
  - `func newASMetadataHandler(fetch func(context.Context) (map[string]any, error), publicBase string, ttl time.Duration) http.Handler`. It ignores `Host`, so one route serves both `dex.chrismiller.xyz` (RFC 8414) and `mcp.chrismiller.xyz` (the legacy document).
  - `type facadeMetrics struct`, with `func (m *facadeMetrics) ServeHTTP` writing a hand-written Prometheus text exposition:
    - `mcp_facade_registrations_total{result}`
    - `mcp_facade_clients` (from `facadeCount`)
    - `mcp_facade_clients_max`
    - `mcp_facade_client_violations_total`
  - `func dexTransportCredentials(certFile, keyFile, caFile, serverName string) (credentials.TransportCredentials, error)`. `GetClientCertificate` re-reads the key pair whenever either file's mtime changes, because cert-manager renews it in place. `RootCAs` is loaded once from `caFile`. The server name is `dex.dex.svc.cluster.local`.
  - Config env, with defaults:
    - `DEX_DISCOVERY_URL`: `http://dex.dex.svc.cluster.local:5556/.well-known/openid-configuration`
    - `DEX_GRPC_ADDR`: `dex.dex.svc.cluster.local:5557`
    - `DEX_GRPC_CERT`/`DEX_GRPC_KEY`/`DEX_GRPC_CA`: `/etc/dex-client/tls.crt|tls.key|ca.crt`
    - `REDIRECT_RULES_FILE`: `/etc/mcp-redirects/rules`
    - `MAX_FACADE_CLIENTS`: 20; `REGISTER_BURST`: 5; `REGISTER_EVERY`: 10m
    - `CLIENT_CACHE_TTL`: 60s; `CLIENT_CACHE_MAX_STALE`: 15m
  - Routes on one mux: `/auth`, `/healthz`, `GET /metrics`, `POST /oauth/register`, `GET /oauth/authorize`, `GET /.well-known/oauth-protected-resource/<resource>` and `GET /.well-known/oauth-authorization-server`.
  - If Dex gRPC is unreachable, registration answers 503 (`dex_unavailable`) and the process keeps running. Grandfathered clients keep working, and `mcp-` clients work until `maxStale`, after which they fail closed.

- [ ] **Step 1: Write the failing tests.**

```go
// TestParseRedirectRules: "loopback-port 8976 /callback" → {loopback-port, "/callback", 8976};
// "exact https://claude.ai/api/mcp/auth_callback  # Anthropic docs" → comment stripped;
// errors: "loopback-port 80 /callback", "loopback-port 8976 callback", "segment http://x/", "segment https://x/a" (no trailing /), "bogus x"
// TestMatchRedirect (rules: exact claude.ai, exact claude.com, exact chatgpt legacy,
// segment https://chatgpt.com/connector/oauth/, loopback-port 8976 /callback)
ok: "https://claude.ai/api/mcp/auth_callback"→"claude.ai", "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect", "https://chatgpt.com/connector/oauth/G_DaC-OGWUOC"→"chatgpt.com",
    "http://localhost:8976/callback"→"localhost (this computer)", "http://127.0.0.1:8976/callback"
// TestMatchRedirect_LookAlikes — every one refused:
"https://claude.ai.evil.example/api/mcp/auth_callback", "https://claude.ai@evil.example/api/mcp/auth_callback",
"https://CLAUDE.AI/api/mcp/auth_callback", "https://claude.ai./api/mcp/auth_callback", "…auth_callback?x=1",
"…auth_callback#f", "…auth_callback/", "http://claude.ai/api/mcp/auth_callback",
"https://chatgpt.com/connector/oauth/", "https://chatgpt.com/connector/oauth/a/b", "https://chatgpt.com/connector/oauth/%2e%2e",
"https://chatgpt.com/connector/oauth/x?y", "http://localhost.evil.example:8976/callback", "http://localhost:64336/callback",
"http://localhost/callback", "http://localhost:08976/callback", "http://localhost:8976/callback/../x",
"http://localhost:8976/Callback", "http://[::1]:8976/callback", "https://localhost:8976/callback",
"http://user@localhost:8976/callback", "http://LOCALHOST:8976/callback"
// TestConsentName_Hostile — for each name, HasPrefix(got, "chatgpt.com — ") and no rune in Cc/Cf:
"Claude at claude.ai", "‮moc.ia.edualc", "line1\nline2", strings.Repeat("x", 10000), "<b>x</b>"
consentName("", []string{"chatgpt.com"}) == "chatgpt.com — MCP client"
consentName("Claude Code (gramps)", []string{"localhost (this computer)"}) == "localhost (this computer) — Claude Code (gramps)"
// TestClientCache_Invariants (fake ListClients):
"mcp-ok" (public, no peers, [claude.ai exact]) → ok; "mcp-private" (public=false) → "invariant:public";
"mcp-peers" (trusted_peers [immich]) → "invariant:trusted_peers"; "mcp-noredirect" ([]) → "invariant:redirect_uris";
"mcp-offrule" ([https://evil.example/cb]) → "invariant:redirect_uris"; violations counter == 4;
"mcp-unknown" → false, ListClients called once more, and not again within 5 s;
registry error, list age < maxStale → "mcp-ok" still ok; registry error, age > maxStale → (false, "dex-unavailable")
// TestFacadeChecker: grandfathered → ok, zero ListClients calls; "immich" → false, zero calls
// TestAuthorizeBearer_ViolatorDenied: token aud "mcp-private" → errWrongAudience
// TestRegister (fake registry records CreateClientReq)
valid → 201, Content-Type application/json; Id HasPrefix "mcp-" && len == 30; Public; Name == consentName(...);
        len(RedirectUris) >= 1; Secret == "" && TrustedPeers == nil && LogoUrl == ""; body token_endpoint_auth_method "none",
        scope "openid email profile offline_access", grant_types [authorization_code refresh_token]; next check() refreshes
"https://evil.example/cb" → 400 {"error":"invalid_redirect_uri"}, CreateClient not called
0 or 4 redirect_uris → 400 invalid_redirect_uri; token_endpoint_auth_method "client_secret_basic" → 400 invalid_client_metadata;
grant_types ["client_credentials"] → 400 invalid_client_metadata; 9 KiB body → 400; GET → 405; text/plain → 400
// TestRegister_RateLimited: limiter burst 2 → 3rd → 429, Retry-After > 0, CreateClient called twice
// TestRegister_CapReached: 20 "mcp-*" + statics, max 20 → 403 {"error":"access_denied"}; 16 → 201
// TestRegister_AlreadyExists: resp.AlreadyExists → 502, body lacks "mcp-"
// TestRegister_DexError: CreateClient error → 502, body not containing the gRPC error text
// TestAuthorize: facade id + S256 → 302 Location host dex.chrismiller.xyz path /auth,
//   scope == "openid email profile offline_access" (request had "mcp audience:server:client_id:immich"),
//   state/redirect_uri/code_challenge/resource preserved; grandfathered id → 302; "immich" → 400;
//   "mcp-private" → 400; method plain → 400; no code_challenge → 400; response_type token → 400
// TestPRM: 200 {"resource":"https://mcp.chrismiller.xyz/gramps/mcp","authorization_servers":["https://dex.chrismiller.xyz"],
//   "scopes_supported":["openid","email","profile","offline_access"],"bearer_methods_supported":["header"]},
//   Access-Control-Allow-Origin "*"; /other/mcp → 404; POST → 405
// TestASMetadata (fake Dex doc): issuer unchanged; authorization_endpoint "https://mcp.chrismiller.xyz/oauth/authorize";
//   registration_endpoint "https://mcp.chrismiller.xyz/oauth/register"; token_endpoint and jwks_uri unchanged;
//   code_challenge_methods_supported ["S256"]; token_endpoint_auth_methods_supported contains "none";
//   no "client_id_metadata_document_supported" key; identical body for Host dex.… and mcp.…; fetch error → 503;
//   two GETs within ttl → fetch called once
// TestAgreement: PRM authorization_servers[0] == AS metadata issuer for the same config (live problem 4)
// TestMetrics: after one created and one rate_limited: body contains
//   `mcp_facade_registrations_total{result="created"} 1`, `…{result="rate_limited"} 1`, `mcp_facade_clients_max 20`
// TestDexTLS_ReloadsClientCert: GetClientCertificate returns cert A; rewrite files with cert B (mtime bump) → returns B
```

- [ ] **Step 2: Run the tests and watch them fail.** `nix develop -c sh -c 'cd images/mcp-jwt-auth && go test ./...'`.
- [ ] **Step 3: Implement.**
  - Registration reads its body through `http.MaxBytesReader(8<<10)`. The client id is `prefix + rand.Text()`.
  - Log each refused redirect as scheme+host+path, without the query, so Chris can add Gemini's real form.
  - CORS `*` applies to the two metadata GETs only.
  - `/metrics` is never routed by an Ingress.
- [ ] **Step 4: Run the tests and watch them pass.** Same command.
- [ ] **Step 5: Commit and publish.** `nix develop -c git commit -m "mcp-jwt-auth: OAuth facade (exact-redirect DCR, Dex-verified mcp- clients, global rate limit and cap, scope shim, PRM + RFC 8414, metrics)"`, then push. Use `Monitor` until CI is green and the `sha-<7>` digest resolves. **Do not deploy yet.**

---

### Task 6: Roll out the facade (A1 behind a port-forward, then A2 public Ingresses), with its alerts

**Files:**
- Create: `cluster/oauth2-proxy/mcp-redirects.rules`. Each line carries a comment naming its source:
  - `exact https://claude.ai/api/mcp/auth_callback`
  - `exact https://claude.com/api/mcp/auth_callback`
  - `exact https://chatgpt.com/connector_platform_oauth_redirect`
  - `segment https://chatgpt.com/connector/oauth/`
  - `loopback-port 8976 /callback` (Claude Code `--callback-port 8976`, and `tools/mcp-probe`)
  - `# segment https://oauth-redirect.googleusercontent.com/r/user_bound_custom-mcp-`: Gemini, commented out until a refusal logs the real form.
- Create: `cluster/oauth2-proxy/mcp-jwt-auth-dex-client.yaml`. Certificate `mcp-jwt-auth-dex-client`, ClusterIssuer `edge-ca`, CN `mcp-jwt-auth`, `client auth`, ECDSA 256, 8760h/720h; the same shape as hyprmcp's.
- Create: `cluster/prometheus-stack/rules/mcp-facade-alerts.yaml`, a PrometheusRule `mcp-facade-alerts` in namespace `prometheus` with labels `release: prometheus` (required since 0b Task 5) plus the legacy `prometheus: kube-prometheus, role: alert-rules` and group `mcp-facade`:
  - `MCPFacadeClientsNearCap`: `max(mcp_facade_clients) >= 0.8 * max(mcp_facade_clients_max)` for 5m, `severity: warning`. The description points at the pruning procedure in `docs/auth.md`.
  - `MCPFacadeClientInvariantViolation`: `increase(mcp_facade_client_violations_total[10m]) > 0`, `severity: warning`.
  - `MCPFacadeClientRegistered`: `increase(mcp_facade_registrations_total{result="created"}[10m]) > 0`, `severity: info`. It is the detective control for the shared-callback limit (Step 8).
- Modify: `cluster/prometheus-stack/rules/kustomization.yaml` (add the file to `resources`)
- Modify: `cluster/oauth2-proxy/servicemonitors.yaml`. Add ServiceMonitor `mcp-jwt-auth` (`release: prometheus`, selector `app.kubernetes.io/name: mcp-jwt-auth`, port `http`, path `/metrics`, 30s).
- Modify: `cluster/oauth2-proxy/kustomization.yaml` (the cert resource; `configMapGenerator: mcp-redirect-rules` from `rules=mcp-redirects.rules`, so a rule change rolls the pods)
- Modify, in **A1**: `cluster/oauth2-proxy/mcp-jwt-auth.yaml`. Pin the Task 5 image. The Task 5 env defaults match these mounts, so set only `MAX_FACADE_CLIENTS: "20"`, which keeps the cap visible in git. Mount the Certificate's Secret at `/etc/dex-client` and the rules ConfigMap at `/etc/mcp-redirects`. Neither goes under `/etc/mcp-jwt-auth` (the emails volume) or `/etc/mcp-clients`.
- Modify, in **A1**: `cluster/mcp-gateway/deployment.yaml`. Give the `mcp-gateway-register-blocked` Ingress `traefik.ingress.kubernetes.io/router.priority: "300"`, with a comment saying it must outrank the facade's `"200"` until Task 10 deletes it. Correct both stopgap comments: the sentence "Clients that are already registered keep working, because token refresh goes straight to Dex" becomes "Clients that are already registered can still sign in again (authorize and token are not blocked). There is no refresh path to lean on: the cluster held zero Dex refresh tokens on 2026-10-18, so every reconnect is a browser sign-in."
- Modify, in **A2**: `cluster/oauth2-proxy/mcp-jwt-auth.yaml`. Add two Ingresses, both annotated `traefik.ingress.kubernetes.io/router.priority: "200"`, both routing to `mcp-jwt-auth:4181`:
  - `mcp-oauth`: host `mcp.chrismiller.xyz`, `pathType: Exact` for `/oauth/register`, `/oauth/authorize`, `/.well-known/oauth-protected-resource/gramps/mcp` and `/.well-known/oauth-authorization-server`;
  - `mcp-as-metadata`: host `dex.chrismiller.xyz`, Exact `/.well-known/oauth-authorization-server`.
  - A comment gives the priority ladder: stopgap 300 > facade 200 > every default rule (< 100). Dex `/auth` stays unreachable through the facade.
- Modify: `docs/auth.md` (a new "MCP OAuth facade" subsection), `docs/gramps.md` (connect instructions, gotchas)

**Interfaces:**
- Consumes: the Task 5 image and routes.
- Produces:
  - the facade serving publicly at `/oauth/authorize`, both AS documents, and a PRM pointing at Dex;
  - `/oauth/register` **still 403 publicly**, and live in-cluster at `http://127.0.0.1:14181` through `kubectl -n oauth2-proxy port-forward deploy/mcp-jwt-auth 14181:4181`;
  - `PROBE_ID`, a facade client.

- [ ] **Step 1: Snapshot before.** Save `edge-5.txt`.
- [ ] **Step 2: Commit A1.** It holds the cert, rules, pin, env, volumes, ServiceMonitor, alert rule and stopgap priority: `mcp: deploy the OAuth facade behind the registration block (no new public routes)`. Push. Use `Monitor` until `kubectl -n oauth2-proxy get certificate mcp-jwt-auth-dex-client` is Ready and the rollout is 2/2.
- [ ] **Step 3: Verify A1 in-cluster.** Start the port-forward with `run_in_background`.
  - `curl -s 127.0.0.1:14181/.well-known/oauth-protected-resource/gramps/mcp | jq -c .` matches TestPRM.
  - `curl -s 127.0.0.1:14181/.well-known/oauth-authorization-server | jq -r .issuer` prints `https://dex.chrismiller.xyz`.
  - `register --endpoint http://127.0.0.1:14181/oauth/register --name evil --redirect https://evil.example/callback` returns **400 `invalid_redirect_uri`**, and `oauth2clients` has no new entry.
  - `register --endpoint http://127.0.0.1:14181/oauth/register --name mcp-probe --port 8976` returns 201 with an id `mcp-…`; record it as `PROBE_ID`. `oauth2clients` shows name `localhost (this computer) — mcp-probe`.
  - `login --client-id $PROBE_ID --port 8976 --authorize-url https://dex.chrismiller.xyz/auth` (Chris; consent is still skipped until Task 9) shows `aud == PROBE_ID` and `has_refresh_token: true`. Then `call --client-id $PROBE_ID` returns 200 through the edge, which proves the client cache validated a facade client.
  - `curl -s 127.0.0.1:14181/metrics | grep -E '^mcp_facade_(clients|clients_max) '` prints `1` and `20`.
  - Prometheus shows target `oauth2-proxy/mcp-jwt-auth` `up` (the `docs/auth.md` targets command), and `/api/v1/rules` lists group `mcp-facade`.
  - Memory: `kubectl -n oauth2-proxy top pods -l app.kubernetes.io/name=mcp-jwt-auth` is under the 128Mi limit; note the number.
  - The public snapshot is identical to `edge-5.txt`.
- [ ] **Step 4: Commit A2 (Ingresses).** `mcp: the facade takes over authorize and metadata publicly (registration still blocked)`. Push and watch the Argo sync with `Monitor`.
- [ ] **Step 5: Verify A2.**
  - `discover` exits **0**: `authorization_server` and `issuer` are both `https://dex.chrismiller.xyz`, and `registration_endpoint` is `https://mcp.chrismiller.xyz/oauth/register`.
  - **Public `GET` and `POST /oauth/register` still answer 403** (C1: the stopgap at 300 beats the facade at 200).
  - `curl -s -o /dev/null -w '%{redirect_url}' "https://mcp.chrismiller.xyz/oauth/authorize?client_id=$PROBE_ID&response_type=code&redirect_uri=http%3A%2F%2Flocalhost%3A8976%2Fcallback&code_challenge=x&code_challenge_method=S256&scope=mcp"` points at `dex.chrismiller.xyz/auth` with `scope=openid+email+profile+offline_access`. The same request with `client_id=immich` answers 400.
  - `login --client-id $PROBE_ID --port 8976` through the discovered (facade) authorize endpoint, then `call`, returns 200.
  - Claude Code (grandfathered) still works.
  - The snapshot diff against `edge-5.txt` has exactly these changes:
    - the PRM body: `authorization_servers` is Dex, plus `scopes_supported` and `bearer_methods_supported`;
    - the dex AS document: 404 → 200;
    - the mcp AS document body: now the facade's;
    - `/oauth/authorize`: 302 → 400.
  - Rollback is to revert A2; hyprmcp still has every route.
- [ ] **Step 6: Docs.** `docs/auth.md`, "MCP OAuth facade":
  - the rules file and its three kinds (and why loopback is one port);
  - the global limiter (5 burst per replica, one per 10 minutes, cap 20, alert at 16) and why it is global (ETP=Cluster);
  - `mcp-` is reserved, with the pre-commit hook;
  - the client-cache invariants, and that the edge fails closed after 15 minutes without Dex;
  - the priority ladder;
  - how to add Gemini from the refusal log;
  - why PRM names Dex, and why the mcp-host AS document stays;
  - **pruning when the cap nears.** List the facade clients with `kubectl -n dex get oauth2clients -o json | jq -r '.items[] | select(.id|startswith("mcp-")) | [.metadata.name,.id,.name,.metadata.creationTimestamp] | @tsv'`. Count refresh tokens per client with `kubectl -n dex get refreshtokens.dex.coreos.com -o json | jq -r '[.items[].clientID] | group_by(.) | map("\(.[0]) \(length)") | .[]'`. A client with no refresh token that is older than 7 days has not been used since it registered. Delete it with `kubectl -n dex delete oauth2client <metadata.name>`, after Chris's OK (S4); the cache notices within 60 s;
  - **the inherent limit.** The consent screen cannot tell two tenants of a shared callback apart. Anyone can register `https://claude.ai/api/mcp/auth_callback` under a name of their choosing, and a family member who approves "claude.ai — …" for an attacker-started flow hands the code to the attacker's claude.ai account. What limits this: consent is per client id, tokens only work for allowlisted family, and `MCPFacadeClientRegistered` tells Chris about every new client.
  - `docs/gramps.md`: the facade sits in front.
  - Commit `docs: MCP OAuth facade`.

---

### Task 7: Deploy gramps-mcp self-auth (now that the facade owns the PRM)

**Files:**
- Modify: `cluster/gramps/mcp.yaml` (env, list mounts, header comment, pin)
- Modify: `docs/gramps.md` (MCP section, gotchas)

**Interfaces:**
- Consumes: the Task 3 image (tag and digest), the `gramps-mcp-grandfathered-clients` ConfigMap (Task 2), and the facade-served PRM (Task 6 A2).

- [ ] **Step 1: H1 gate.** The public PRM body must contain `scopes_supported`, which means the facade served it and hyprmcp's PRM handler is off the public path. If it doesn't, stop.
- [ ] **Step 2: Pin and configure.** In `cluster/gramps/mcp.yaml`:
  - set the image to the Task 3 `:sha-<7>@sha256:<digest>`;
  - add env `MCP_OIDC_ISSUER=https://dex.chrismiller.xyz`, `MCP_OIDC_JWKS_URL=http://dex.dex.svc.cluster.local:5556/keys`, `MCP_EMAILS_FILE=/etc/gramps-mcp/emails`, `MCP_CLIENT_ID_PREFIX=mcp-`, `MCP_GRANDFATHERED_CLIENTS_FILE=/etc/gramps-mcp-clients/grandfathered` and `MCP_RESOURCE_METADATA_URL=https://mcp.chrismiller.xyz/.well-known/oauth-protected-resource/gramps/mcp`;
  - add volume `secret: gramps-new-user-emails`, `items: [{key: emails, path: emails}]`, mounted at `/etc/gramps-mcp` **without `subPath`**. It is the generated mirror of the one family list;
  - add volume `configMap: gramps-mcp-grandfathered-clients`, mounted at `/etc/gramps-mcp-clients`;
  - rewrite the header comment, since "no inbound auth of its own" and "anything in-cluster can read AND WRITE" are no longer true.
  - Commit `gramps: pin gramps-mcp sha-<7> (inbound auth)`, push, and watch the rollout (Recreate, one replica) with `Monitor`.
- [ ] **Step 3: Verify.**
  - The in-cluster POST without a token must print `401` (exit criterion): `nix develop -c kubectl -n gramps run mcp-unauth-probe --rm -i --restart=Never --image=docker.io/curlimages/curl:8.22.0@sha256:58adaa4e8dca9c988bae2aba4ab3434a0bb2da16bbe3f92dec39ec7785166777 -- -s -o /dev/null -w '%{http_code}\n' -X POST http://gramps-mcp.gramps.svc.cluster.local:8000/mcp -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'`.
  - A Claude Code Gramps tool call (grandfathered M4HN) works through the edge, and so does `call --client-id $PROBE_ID`. That proves gramps-mcp accepts what the edge accepts.
  - `kubectl -n gramps logs deploy/gramps-mcp --since=10m | grep -c ' 401 '` shows no 401s for edge traffic.
  - The public PRM is still 200 with the facade body. If it is not, run `kubectl -n mcp-gateway rollout restart deploy/mcp-gateway`, which clears hyprmcp's cache (finding 12).
  - The snapshot diff against the post-A2 snapshot is empty.
- [ ] **Step 4: Docs.** In `docs/gramps.md`:
  - the MCP section: there are now four checks (the gateway's signature check, mcp-jwt-auth, gramps-mcp, the Gramps role), and "anything in-cluster can read it" is gone;
  - the second patch kept in this repo, and why `cryptography` is wrapped onto `PYTHONPATH`;
  - gotchas rows: "gramps-mcp 401 while the edge accepts → the grandfathered mirror is stale, or a Dex key rotated faster than the JWKS cache"; and "hyprmcp caches any upstream 401's `resource_metadata`; harmless while the facade owns the public PRM; restart mcp-gateway to clear it".
  - Commit.

---

### Task 8: Strip hyprmcp to a proxy

**Files:**
- Modify: `cluster/mcp-gateway/config/config.yaml`:
  - `server: https://dex.chrismiller.xyz` (no slash);
  - `serverMetadataProxyEnabled: false`, `authorizationProxyEnabled: false` and `dynamicClientRegistration.enabled: false`;
  - delete `dexGRPCClient`;
  - keep `proxy[0].authentication.enabled: true` (Task 12 decides).
- Modify: `cluster/mcp-gateway/deployment.yaml`. Delete the `dex-client` volume and mount, and the `mcp-gateway-oauth` Ingress. The comments move the rate-limit and bootstrap claims to the facade. **Keep** the stopgap Middleware and Ingress.
- Modify: `cluster/mcp-gateway/kustomization.yaml` (drop the cert). Delete: `cluster/mcp-gateway/dex-client-cert.yaml`.
- Modify: `docs/auth.md`, `docs/gramps.md`

- [ ] **Step 1: Commit and push.** `mcp-gateway: proxy only; registration and metadata belong to the facade`. Watch with `Monitor`.
- [ ] **Step 2: Verify.**
  - The hyprmcp pods start. They read Dex metadata from the public dex host: the facade's document first, then `openid-configuration`.
  - The `/gramps/mcp` lines are unchanged, Claude Code works, and the probe `call` returns 200.
  - The snapshot diff is empty, since every probed OAuth path is already the facade's or the stopgap's.
- [ ] **Step 3: The orphaned credential (ask Chris, S4).** Once Argo has pruned the Certificate, ask before running `kubectl -n mcp-gateway delete secret mcp-gateway-dex-client`. cert-manager leaves the Secret behind. Its cert is valid until 2027-09-26, and Dex trusts any edge-ca client cert with its whole API, so until it is deleted it can create or delete Dex clients.
- [ ] **Step 4: Docs, and commit.**

---

### Task 9: Dex consent, remembered per person (sessions), grants, PKCE and headers — not the day of Task 4

**Files:**
- Modify: `cluster/dex/config.secret.yaml`, by key path only (S9; never print the file), in `stringData.config.yaml`:
  - `oauth2.skipApprovalScreen: false`, with the comment rewritten: consent is enforced in Dex because the facade can be bypassed, and remembered consent makes it a once-per-person-per-app cost;
  - `oauth2.grantTypes: [authorization_code, refresh_token]`;
  - `oauth2.pkce.codeChallengeMethodsSupported: [S256]` (`enforce` is left unset; Task 12 decides);
  - `web.headers: {X-Frame-Options: DENY, Content-Security-Policy: "frame-ancestors 'none'"}`;
  - a new `sessions:` block with `cookieEncryptionKey`, `absoluteLifetime: 24h`, `validIfNotUsedFor: 1h` and `ssoSharedWithDefault: none`.
- Regenerate: `cluster/dex/config.yaml` (`rm` it, then `nix develop -c ./sign.sh`)
- Modify: `cluster/dex/dex.yaml`. Add env `DEX_SESSIONS_ENABLED: "true"`, with a comment: experimental; it must be removed **in the same commit** as the `sessions:` block, because 2.46 refuses to start with the block and without the flag.
- Modify: `docs/auth.md`:
  - a consent section;
  - the troubleshooting row "Grant Access on every login", rewritten: expected once per app per person, and every time if the sessions flag is off;
  - **revocation (L3).** List consents with `kubectl -n dex get useridentities.dex.coreos.com -o json | jq -r '.items[] | [.metadata.name,.connectorID,((.consents//{})|keys|join(","))] | @tsv'`. Revoke one person's consent for one client with `kubectl -n dex patch useridentity <name> --type=json -p '[{"op":"remove","path":"/consents/<client-id>"}]'`. Kill a client's refresh tokens by deleting the `refreshtokens.dex.coreos.com` objects whose `.clientID` matches. Remove a person entirely by deleting their `useridentity`. Every one of these asks Chris first (S4). The gRPC `RevokeConsent` stays unavailable, because `DEX_API_SESSIONS_IDENTITIES_CRUD` would hand the facade's cert identity powers.
- Modify: `CLAUDE.md`. One line under Known sharp edges #4: Dex runs the experimental sessions flag for remembered consent; re-check it on every Dex bump, and the flag and the `sessions:` block come and go together.

**Interfaces:**
- Consumes: facade-named DCR clients (Task 6).
- Produces: a Dex approval page whose first line is `<redirect host> — <client name> would like to:` for every facade client. A `useridentities` object per person, holding their consents. A staged rollback branch `rollback/dex-sessions`.

- [ ] **Step 1: Gate.**
  - This is not the day of Task 4.
  - S5: nodes Ready, none SchedulingDisabled, no Ceph repair running.
  - Chris has told Erin and Jeanne that a one-time "Grant Access" screen will appear for each app.
- [ ] **Step 2: Edit without echoing.**
  - Put the literal placeholder `cookieEncryptionKey: __GENERATE__` in the file.
  - Replace it in place with `nix develop -c python3 -c`: read the file, swap the placeholder for `secrets.token_hex(16)` (32 bytes, AES-256), and write it back. Print nothing.
- [ ] **Step 3: Reseal.**
  - `rm cluster/dex/config.yaml && nix develop -c ./sign.sh` must print the file name and no `FAIL`.
  - `git add` both files. `git show :cluster/dex/config.secret.yaml | head -c 10 | od -c` must show `\0GITCRYPT`.
  - Review with `git diff --cached --no-textconv --stat` only.
- [ ] **Step 4: Run the Dex local check** (Task 4) **with** `-e DEX_SESSIONS_ENABLED=true`. It must log `config sessions`, and its discovery must show exactly two grant types and `["S256"]`. Run it once more without the flag; it must refuse to start with `sessions config requires sessions to be enabled`. That proves the rollback rule.
- [ ] **Step 5: Stage the rollback before pushing.**
  - Commit the change: `dex: consent for every client, remembered per person (sessions); no device or token-exchange grants; S256 only; no framing`.
  - Then create local branch `rollback/dex-sessions` on top of it. That branch removes **both** the `sessions:` block and `DEX_SESSIONS_ENABLED`, keeps consent on, is resealed, and is committed but not pushed. Switch back to `main`.
- [ ] **Step 6: Push and restart.**
  - Record the Secret's `.metadata.resourceVersion`, push, and use `Monitor` until it changes, which means the controller has unsealed the new config.
  - Then `nix develop -c kubectl -n dex rollout restart deploy/dex`, because the env change may have rolled the pods before the Secret updated. Watch with `Monitor`.
- [ ] **Step 7: Verify Dex.**
  - Run the Dex post-rollout check.
  - The logs contain `config sessions` and do not contain `config skipping approval screen`.
  - In `openid-configuration`, `grant_types_supported` is exactly `["authorization_code","refresh_token"]` and `code_challenge_methods_supported` is `["S256"]`.
  - `curl -sI` of the `/auth` page shows `X-Frame-Options: DENY` and the CSP.
- [ ] **Step 8: Verify facade consent (registration is still blocked publicly, so register in-cluster).**
  - Start the port-forward as in Task 6 Step 3, then `register --endpoint http://127.0.0.1:14181/oauth/register --name mcp-probe-consent --port 8976`; record the id as `CONSENT_ID`.
  - Then `login --client-id $CONSENT_ID --port 8976` (Chris). **The Dex page reads `localhost (this computer) — mcp-probe-consent would like to:`** (exit criterion "consent screen naming its redirect host"). Approve it, and `call` returns 200.
  - The `useridentities` list command shows Chris's identity with `$CONSENT_ID` in its consents.
  - A second `login` shows no approval page (remembered).
- [ ] **Step 9: Verify static clients (Chris, private windows).**
  - Sign in at an admin host, a family host, `family.werethemille.rs` (the family tier, then Gramps), `photos.chrismiller.xyz`, and once with a Microsoft account.
  - Each shows "<app name> would like to:" once, and a repeat in a new private window shows nothing.
  - While each Dex page is open, record PKCE use with `kubectl -n dex get authrequests.dex.coreos.com -o json | jq -r '.items[] | [.clientID,(.code_challenge_method//"none")] | @tsv'`. Expected: S256 for the oauth2-proxy tiers, which set `--code-challenge-method=S256`. Record what `immich` and `gramps` send.
  - If Immich asks every time, it is sending `prompt=consent`. Record that in `docs/auth.md`; it is a finding, not a blocker.
- [ ] **Step 10: Rollback, if needed.**
  - Sessions trouble: cherry-pick `rollback/dex-sessions` onto main, push, wait for the Secret, then `rollout restart`. Consent stays on and shows at every login. Stored consents are still honoured, because `Satisfied()` reads them regardless of the flag.
  - `skipApprovalScreen: true` again, only with Chris's explicit OK: while the stopgap is still up, it returns to the pre-0a posture. **After Task 10 it would reopen live problem 1, so re-add the stopgap first.**
- [ ] **Step 11: Docs, and commit.**

---

### Task 10: Lift the registration block, then reconnect Claude Code and ChatGPT

**Files:**
- Modify: `cluster/mcp-gateway/deployment.yaml`. Delete the `deny-all` Middleware, the `mcp-gateway-register-blocked` Ingress and their comments.
- Modify: `docs/auth.md`, `docs/gramps.md` (connect instructions: `claude mcp add --transport http --callback-port 8976 …`, and the recovery path)

**Interfaces:**
- Consumes: Task 6 (allowlist verified), Task 9 (consent verified).
- Produces: public DCR on the facade, and Chris's clients on facade ids.

- [ ] **Step 1: Gate.** Task 6 Step 5 and Task 9 Steps 7–9 have passed, and the alert group `mcp-facade` is loaded.
- [ ] **Step 2: Commit and push** the lift on its own: `mcp: lift the registration block (facade allowlist + Dex consent verified)`. Watch the Argo sync with `Monitor`.
- [ ] **Step 3: Verify publicly.**
  - `GET /oauth/register` answers 405.
  - `register --name evil --redirect https://evil.example/callback` answers **400 `invalid_redirect_uri`**, and no client is created (exit criterion "foreign-redirect registration refused").
  - The snapshot diff against the post-Task-8 snapshot changes only register: GET 403 → 405, POST 403 → 400.
- [ ] **Step 4: Reconnect Claude Code with a fixed port (Chris).**
  1. `claude mcp add --help` lists `--callback-port` and `--client-id`.
  2. `claude mcp add --transport http --callback-port 8976 gramps-0a https://mcp.chrismiller.xyz/gramps/mcp`, at the scope the old `gramps` entry uses.
  3. Run `/mcp` and authenticate. Dex shows `localhost (this computer) — <Claude Code's client name> would like to:`; approve it.
  4. A Gramps read tool under `gramps-0a` returns data, and `oauth2clients` shows a new `mcp-…` client with redirect `http://localhost:8976/callback`.
  5. Only then, `claude mcp remove gramps`. If the `mcp__gramps__` tool prefix matters to any settings, re-add it as `gramps` with the same port (it re-registers once) and remove `gramps-0a`.
  - **Recovery while M4HN is still grandfathered:** `claude mcp add --transport http --client-id M4HNWEXI6EFRW7EXBJFYENJVL2 --callback-port 64336 gramps https://mcp.chrismiller.xyz/gramps/mcp`. Write it into `docs/gramps.md`.
- [ ] **Step 5: Reconnect ChatGPT (Chris).**
  - First ask Chris (S4: patching an OAuth2Client) to rename the grandfathered client's display name, so its consent screen names the host: `kubectl -n dex patch oauth2client g5iusvcyiq2fgqsngjnemwceka2vitcqkjfeows2gpf7fhheqqrcgji --type merge -p '{"name":"chatgpt.com — ChatGPT"}'`.
  - Chris reconnects the ChatGPT connector. Consent shows `chatgpt.com — …`, and a Gramps read works.
  - Record whether ChatGPT reused `7QITXD4SBM2ZFXDP5TLPRJGZZ3` or registered a new `mcp-` client (redirect under `https://chatgpt.com/connector/oauth/`). If it registered a new one, ask Chris whether 7QIT can be retired in Task 12.
- [ ] **Step 6: Docs, and commit.**

---

### Task 11: Expiry, with a canary first, then 1 h

**Files:**
- Modify: `cluster/dex/config.secret.yaml`, key `expiry`:
  - `idTokens`: canary `10m`, then `1h`;
  - `refreshTokens`: `disableRotation: false`, `reuseInterval: 3s`, `validIfNotUsedFor: 720h`, `absoluteLifetime: 2160h`.
  - Reseal `cluster/dex/config.yaml` per Task 9 Step 3. There is no new secret value, so no generation step.
- Modify: `docs/auth.md` (the expiry block; the refresh model per app; "short expiry is not revocation", pointing at the Task 9 revocation commands)

**Interfaces:**
- Consumes: MCP clients that hold refresh tokens (the Task 6 shim, the Task 10 reconnects).
- Produces: 1 h ID and access tokens; rotating refresh tokens with a 30-day idle and 90-day absolute limit.

- [ ] **Step 0: Refresh pre-test, at the current 24 h lifetime, before anything is lowered.**
  - `grep -c -- '--cookie-refresh' cluster/oauth2-proxy/{admin,family,family-rs}.yaml` must print `0` for each file. If so, the 168 h cookie governs the session and Dex's expiry is never consulted. If any file has the flag, stop and work out its refresh path first.
  - `refresh --client-id $CONSENT_ID` must return `refresh_rotated: true`, and `call` then returns 200. That proves the MCP refresh grant (Dex refresh, then Google refresh) works before it starts to matter hourly.
  - `kubectl -n dex get refreshtokens.dex.coreos.com -o json | jq -r '[.items[].clientID] | group_by(.) | map("\(.[0]) \(length)") | .[]'` shows tokens for Claude Code's new client. If it doesn't, Claude Code is not asking for `offline_access`; stop.
  - If any refresh fails, stop: lowering would mean hourly re-logins for MCP clients.
- [ ] **Step 1: Canary.**
  - Gate: S5, and Chris is available (open question 1).
  - Set `idTokens: 10m` and the refresh policy, reseal, and run the Dex local check.
  - Commit `dex: 10-minute tokens for the refresh canary (raised to 1h within the hour)`. Push, wait for the Secret, restart, and use `Monitor` until Ready. Run the Dex post-rollout check. Call this time T0.
- [ ] **Step 2: Open sessions at T0 (Chris).**
  - In the browser: an admin host, a family host, `family.werethemille.rs` (Gramps web) and photos web.
  - On the phone: open the Immich app.
  - Run `login --client-id $CONSENT_ID --port 8976`; it must show `lifetime_s≈600` and `has_refresh_token: true`.
  - Make one Claude Code Gramps call.
  - Record `sum(http_requests_total{namespace="dex",handler="/callback"})` and `sum(http_requests_total{namespace="dex",handler="/auth/{connector}"})` (exact matchers; the braces are literal) with the Prometheus exec command from `docs/auth.md` "Metrics". Plain `/auth` is polled about 3 times a minute and means nothing (finding 15).
- [ ] **Step 3: Wait** with `Monitor` (an until-loop on the clock) to T0+15 min.
- [ ] **Step 4: Verify (exit criterion "Dex refresh works for each app").**
  - Every browser app reloads and works with no Dex page, and the `/callback` and `/auth/{connector}` counts have not moved since Step 2.
  - Immich mobile browses.
  - Gramps desktop sync is unaffected (it uses a password).
  - Probe: `call` with the old token returns 401 `invalid_token`; `refresh` returns `lifetime_s≈600` and `refresh_rotated: true`; `call` then returns 200.
  - A Claude Code Gramps call succeeds **without a browser prompt**.
  - **If any app forces a login, go back to 24h**, record which app and why, and fix it before Step 5.
- [ ] **Step 5: Set 1 h.**
  - `idTokens: 1h`, reseal, local check. Commit `dex: 1h tokens, rotating refresh tokens (30d idle, 90d absolute)`, push, restart, `Monitor`, and the post-rollout check.
  - After 75 minutes (`Monitor`), confirm that a Claude Code call and a probe `refresh` + `call` both work.
- [ ] **Step 6: Docs, and commit.**

---

### Task 12: Exit verification and cleanup

**Files:**
- Modify: `cluster/oauth2-proxy/mcp-grandfathered-clients.txt` (and its mirror): retire M4HN.
- Modify, possibly: `cluster/dex/config.secret.yaml` (`oauth2.pkce.enforce: true`), `cluster/mcp-gateway/config/config.yaml`.
- Modify: `docs/auth.md` (spike result: "Dex access tokens carry `aud` = client id → Path A", with the date)

- [ ] **Step 1: Retire M4HN** (after Task 10 Step 4 is proven, with Chris's OK). Remove its line, run the mirror hook, and commit `mcp: retire the pre-facade Claude Code client`. Push and use `Monitor` for both rollouts (mcp-jwt-auth, then gramps-mcp). Claude Code (the new client) still works. The recovery path in `docs/gramps.md` is now gone; delete that paragraph.
- [ ] **Step 2: Negative check (exit criterion "a token for another client is rejected").**
  - `login --client-id M4HNWEXI6EFRW7EXBJFYENJVL2 --port 64336 --authorize-url https://dex.chrismiller.xyz/auth`. It goes straight to Dex, because the facade shim now refuses this id.
  - Then `call --client-id M4HNWEXI6EFRW7EXBJFYENJVL2` must return **401 `invalid_token`**, and the mcp-jwt-auth logs show `denied … client_id=M4HN…`.
- [ ] **Step 3: H1 live check.**
  - Port-forward `svc/mcp-gateway 19000:9000` (in the background).
  - `call --client-id M4HNWEXI6EFRW7EXBJFYENJVL2 --url http://127.0.0.1:19000/gramps/mcp`. hyprmcp accepts the signature and gramps-mcp answers 401, so hyprmcp caches the URL.
  - Then the public PRM must still be 200 with the facade body. If it is not, run `kubectl -n mcp-gateway rollout restart deploy/mcp-gateway` and record a finding.
- [ ] **Step 4: Re-run every exit check and paste the evidence into the task notes.**

| Exit criterion | Command | Pass |
|---|---|---|
| Foreign-redirect registration refused | `register --name x --redirect https://evil.example/callback` | 400 `invalid_redirect_uri`, no new client |
| Consent names the redirect host | Task 9 Step 8, with a fresh public `register --port 8976` | page starts `localhost (this computer) — …` |
| Token for another client rejected | Step 2 | 401 `invalid_token` |
| Unauthenticated in-cluster POST to gramps-mcp | Task 7 Step 3 `kubectl run` | `401` |
| Claude Code works after reconnect | one Gramps read tool | data returned |
| Edge snapshot diff clean | final snapshot vs `edge-0.txt` | only the lines enumerated in Tasks 2, 4, 6, 9 and 10 differ; every other host is byte-identical |
| Dex refresh works per app | Task 11 Step 4 evidence | all rows pass |
| Metadata agree; 401 has `resource_metadata` | `discover`; `call --no-token` | exit 0; the header ends with `resource_metadata="…"` |

- [ ] **Step 5: Decisions to record.**
  - **hyprmcp `authentication.enabled`: keep `true`.** The check is a cheap third signature check, and turning it off would let any in-cluster caller with no token trigger finding 12's cache through gramps-mcp's 401. Revisit when mcp-edge v1 replaces hyprmcp in phase 2.
  - **PKCE `enforce`:** if Task 9 Step 9 showed S256 from all four static clients, set `oauth2.pkce.enforce: true`. Reseal, run the local check, commit `dex: require PKCE`, restart, run the post-rollout check, and repeat one private-window sign-in per app. Otherwise record which app lacks PKCE in `docs/auth.md` and leave it off.
- [ ] **Step 6: Cleanup (ask Chris first, S4).**
  - Delete the probe Dex clients (`mcp-probe`, `mcp-probe-consent` and any later ones): find each `metadata.name` by its `.id` with jq, then `kubectl -n dex delete oauth2client <name>`.
  - Delete the M4HN client object.
  - Delete 7QIT only if Task 10 Step 5 showed it unused and Chris agrees.
  - Delete `mcp-gateway-dex-client` if Task 8 Step 3 did not.
- [ ] **Step 7: Final docs commit,** and a last snapshot to `edge-final.txt`.

---

## Decisions and open questions

**Answered (2026-10-18):**
- Registration model: open DCR with an exact-redirect allowlist plus consent. The stopgap stays until both are verified, and a separate commit lifts it (Chris).
- `DEX_SESSIONS_ENABLED=true` for remembered consent (Chris).
- The ChatGPT client `7QITXD4SBM2ZFXDP5TLPRJGZZ3` is Chris's: it stays grandfathered and reconnects through the facade in Task 10. `M4HNWEXI6EFRW7EXBJFYENJVL2` is his Claude Code (Chris).
- Gemini: the rule ships commented out until a refused registration shows its real callback (designer).
- Refresh tokens: 30 d idle, 90 d absolute (designer).
- Device-code and token-exchange grants are dropped; nothing uses them (designer).
- Claude Code reconnects with fixed `--callback-port 8976`. The only loopback rule is `loopback-port 8976 /callback` (designer).
- #2662 is not merged until 0a is done; Task 0 splits Dex, oauth2-proxy and the mcp-jwt-auth deps out of it (Chris and designer).

**Still open (only Chris can answer):**
1. **(Task 11) Canary window.** About 30 minutes with 10-minute tokens, with you at a browser and on your phone (Immich). When?
2. **(Tasks 4 and 9) Microsoft test account.** Do you have a Microsoft account to sign in with in a private window, or should Erin (hotmail) do those two sign-ins?
3. **(Task 10) Renaming the ChatGPT client's display name** to `chatgpt.com — ChatGPT` with one `kubectl patch`, so its consent screen names the host. OK?
4. **(Not blocking) Something requests Dex `/auth` about 3 times a minute** (`GET 200`). Do you know what it is? If not, it is worth a look after 0a.

## Self-review notes

- **Spec coverage.**
  - §14 0a bullets:
    - the Dex upgrade (Task 4);
    - expiry and refresh tests (Task 11);
    - consent (Task 9, on facade naming from Tasks 5–6);
    - the DCR allowlist and rate limit (Tasks 5, 6 and 10);
    - the `mcp-jwt-auth` audience and allowlist, with the spike first (Tasks 1, 2 and 5; Branch B);
    - gramps-mcp self-auth (Tasks 3 and 7);
    - the metadata fixes (Task 2 for the 401; Tasks 5–6 for agreement).
  - Docs and the snapshot are folded into each task. Every 0a exit criterion is a row in Task 12.
  - §17 rows 1 and 2 map to the Task 1 decision and finding 3.
  - §9.1's "allowlist comes from the registry" is phase 2; 0a keeps the one family list.
- **Review fixes.** Each is placed in a task:
  - C1 (priorities 300/200; in-cluster DCR; the lift commit; 403 baselines): Tasks 0, 6 and 10.
  - H1 (pin after A2; the PRM check; `resource_metadata` last): Tasks 3, 7 and 12.
  - H3 (alert, pruning, fixed port, the tenant limit): Task 6.
  - H4 (paired rollback; consents persist; revocation by kubectl): Task 9.
  - M1 (spike without registering): Task 1.
  - M3 (client cache, AlreadyExists, broadened hook): Tasks 0 and 5.
  - M4 (shared list vectors): Tasks 2 and 3.
  - M5 (hostile-header tests, RS256 only, JWKS lifespan): Task 3.
  - L1 and L2 (headers, S256): Task 9; PKCE `enforce`: Task 12.
  - L3 (revocation commands): Task 9 docs.
  - L4 (cert reload, orphaned cert validity): Tasks 5 and 8.
  - L5 (one grandfathered source): Task 2.
  - Rollout fixes:
    - flags, PDB, anti-affinity and the local check: Task 4;
    - post-rollout checks and private-window sign-ins: Tasks 4 and 9;
    - 7-character tags: Global Constraints;
    - A1/A2: Task 6;
    - staged rollback and the useridentity check: Task 9;
    - the two-name reconnect and the recovery path: Task 10;
    - the legacy AS document: Tasks 5–6;
    - hyprmcp authentication: Task 12;
    - verified metric series: Task 11;
    - telling Erin and Jeanne: Task 9;
    - the Argo admin rollback: Global Constraints.
- **Type consistency.** Task 2's `facadeChecker` and `parseList` are what Task 5's `newFacadeChecker`, `newAuthorizeHandler` and the cache use. `CONSENT_ID` and `PROBE_ID` are defined in Tasks 9 and 6 before Task 11 uses them.
- **Not done in 0a, on purpose:**
  - `ContextVar`/`stateless_http` in gramps-mcp (phase 2);
  - CIMD (never advertised, because Claude Code ≥2.1.80 breaks on it);
  - renaming mcp-jwt-auth to mcp-edge (phase 2);
  - client GC beyond the documented manual prune;
  - `DEX_API_SESSIONS_IDENTITIES_CRUD`.
