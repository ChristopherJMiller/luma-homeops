# Edge authentication — trust tiers, and how to change who gets in

Public hosts are gated by **oauth2-proxy**, one instance per *trust tier*,
with identity brokered by **Dex** from Google or Microsoft. There is no admin
UI and no database: the configuration is this repo. Replaced authentik
2026-09 (see "History").

```
browser ─▶ Traefik ─▶ [errors, auth] ─▶ oauth2-proxy-<tier> ─▶ Dex ─┬─▶ Google
                            │                                       └─▶ Microsoft
                      401 ──┴──▶ 302 /oauth2/sign_in ──▶ … ──▶ callback ──▶ cookie
```

| Tier | Instance | Public host | Cookie | Allowlist |
|---|---|---|---|---|
| admin | `oauth2-proxy-admin` | `auth-admin.chrismiller.xyz` | `_oauth2_proxy_admin` | `cluster/oauth2-proxy/emails-admin.secret.yaml` |
| family | `oauth2-proxy-family` | `auth-family.chrismiller.xyz` | `_oauth2_proxy_family` | `cluster/oauth2-proxy/emails-family.secret.yaml` |

**The tier is the trust boundary.** Each instance has its own allowlist *and*
its own cookie name and cookie secret, so a session minted for the family tier
is not a valid session on the admin tier — a compromised family account cannot
reach sonarr/sabnzbd/prowlarr no matter what it does with its own cookie.
That is the whole point of running two instances instead of one with groups.

Allowlists are SealedSecrets rather than ConfigMaps: they are other people's
email addresses and this repo is public.

## Gate a host

Add to its Ingress, picking the tier:

```yaml
metadata:
  annotations:
    traefik.ingress.kubernetes.io/router.middlewares: >-
      oauth2-proxy-oauth2-admin-errors@kubernetescrd,oauth2-proxy-oauth2-admin-auth@kubernetescrd
```

Rules that are easy to get wrong:

1. **`errors` must come before `auth`.** The errors middleware wraps what
   follows it; reversed, the browser gets a bare 401 instead of a redirect to
   the sign-in page.
2. **Keep `traefik-strip-csp@kubernetescrd` last** where it was already
   present (the *arr stack, HA, plex).
3. The oauth2-proxy hosts themselves (`auth-admin`, `auth-family`) must stay
   **ungated** — gating them is a redirect loop.
4. Add the host to `.claude/skills/edge-ingress/edge-snapshot.sh`.
5. Gating is only half of it for an app with real users — see "How each app
   consumes the identity" below before assuming the app will know who arrived.
6. **If the backend itself ever answers 401, do not put `errors` on that path.**
   The `errors` middleware matches a status code and cannot tell the backend's
   401 apart from oauth2-proxy's, so it rewrites both into a 302 to Dex. For a
   document that is what you want; for an XHR it is fatal — a cross-origin
   redirect is not something `fetch` can follow, and the browser reports it as a
   confusing CORS error naming *Dex* rather than your app. Split the router:
   `/api` gets `auth` only, documents get `errors,auth`. `cluster/media/ombi-ingress.yaml`
   is the worked example. This weakens nothing — `auth` still gates the API, and a
   flat 401 is the right answer to an unauthenticated XHR.

## Add or remove a person

Edit the tier's `*.secret.yaml`, then:

```bash
rm cluster/oauth2-proxy/emails-<tier>.yaml   # sign.sh skips files that already exist
nix develop --command ./sign.sh
git commit -a && git push
```

oauth2-proxy watches the file and picks up changes without a restart, but the
Secret takes a minute or two to propagate to the pod's mount. Removing someone
does **not** invalidate a session they already hold — their cookie stays valid
until it expires (168h). To evict immediately, rotate that tier's
`cookie-secret-<tier>` in `google-oauth.secret.yaml`, which signs out everyone
on that tier.

Any address works as long as its owner can sign in with **either** Google or
Microsoft — Dex brokers both and the allowlist matches on the address, not on
who issued it. Google Workspace domains count (`kmiller.org` is one);
outlook/hotmail addresses go through the Microsoft connector.

The allowlist is the only gate: the Google consent screen is published, so
there is no test-user list to keep in sync, and the Microsoft side never had
one.

## Google OAuth client

One client (`galaxy-sso`), created by hand in the Google Cloud console —
Google exposes no API for creating "Web application" OAuth clients, so this
artifact cannot be Terraformed (the Microsoft equivalent *is*: `azure/entra/`).
**Dex is the client now**; the two `auth-*` URIs are leftovers from before the
Dex cutover and can be removed once you are confident in it. Redirect URIs:

- `https://dex.chrismiller.xyz/callback` ← the one in use
- `https://auth-admin.chrismiller.xyz/oauth2/callback` (legacy)
- `https://auth-family.chrismiller.xyz/oauth2/callback` (legacy)

The consent screen requires an authorized-domain home page, privacy policy
and terms link. Those are `galaxy.chrismiller.xyz` — `cluster/galaxy-site/`,
edit `content/*.html` and commit (the ConfigMap hash rolls the pods).

The consent screen is **published (Production)** as of 2026-09-23 — no review
was needed because the scopes requested (openid/email/profile) are
non-sensitive. That means no "unverified app" warning, no 7-day token expiry,
and **the allowlist is the only place people are managed** (no test-user list
to keep in sync).

Google required proof of domain ownership before it would accept the branding:
a Search Console **Domain** property for `chrismiller.xyz`, verified by the
`google-site-verification` TXT record in `cloudflare/dns/mail.tf`. A Domain
property covers every subdomain, so it will not need redoing for future hosts.

Client id/secret and the two per-tier cookie secrets live in
`cluster/oauth2-proxy/google-oauth.secret.yaml` (git-crypt → SealedSecret).

## Multi-provider: Dex

oauth2-proxy speaks to exactly one provider, so it points at **Dex**, which
brokers Google AND Microsoft. That is why Erin (hotmail) and Jeanne (gmail)
both work with the same allowlist: Dex passes the upstream email through and
the allowlist checks the address, not the issuer.

```
oauth2-proxy (tier) ──▶ Dex ──▶ Google
                          └────▶ Microsoft (Entra, personal accounts allowed)
```

Dex holds no users and no passwords — it brokers only. Its login page is a
connector chooser ("Log in with Google" / "Log in with Microsoft"), themeable
via `frontend.issuer` / `logoURL` / `theme`. State lives in Kubernetes CRDs,
so there is no database to run or back up.

- Config: `cluster/dex/config.secret.yaml` (a Secret: it carries both upstream
  client secrets and the per-tier static-client secrets).
- **Dex reads its config only at startup.** After changing it:
  `kubectl -n dex rollout restart deploy/dex`.
- One Dex staticClient per oauth2-proxy tier, so a tier can be rotated alone.
- The Microsoft app registration is terraform: `azure/entra/`
  (`./tf.sh plan`). `sign_in_audience = AzureADandPersonalMicrosoftAccount`
  plus access-token version 2 is what admits personal outlook/hotmail
  accounts; the connector uses `tenant: common`. Access control remains the
  oauth2-proxy allowlist, NOT the tenant — anyone with any Microsoft account
  can reach Dex, and is then refused unless their address is allowlisted.

## How each app consumes the identity

Gating a host answers "may this person in?". Some apps also need to know *who*
came in, and there are three different answers in this cluster. Reach for the
first one that works.

| Pattern | Apps | How |
|---|---|---|
| **Gate only** | the *arr stack, SABnzbd, share, mediadav | The app has no user model worth wiring, or a single shared login. oauth2-proxy is the whole story. |
| **Gate + header SSO** | Ombi | oauth2-proxy's `X-Auth-Request-Email` (already in `authResponseHeaders` on both `*-auth` middlewares) *is* the login. Ombi's header auth resolves or creates the user from it. See `docs/ombi.md`. |
| **Native OIDC, no gate** | Immich | The app is its own Dex client with its own user list and does the redirect itself. Necessary when a mobile app must authenticate — it cannot complete a forward-auth redirect. See `docs/immich.md`. |

The middle pattern is the cheapest way to get per-person identity into an app
that has users but no OIDC support, and it costs the mobile apps: anything
behind forward-auth is web-only. The third pattern is the escape hatch for when
that cost is unacceptable, and it means the app's own authorization — not an
allowlist here — decides who gets in, so it needs its own answer to "who may
register" (for Immich, `oauth.autoRegister: false`).

## Troubleshooting

| Symptom | Cause |
|---|---|
| Bare `401` instead of a login page | middleware order — `errors` must precede `auth` |
| Redirect loop | the host is gating itself, or `--whitelist-domain` doesn't cover it |
| "Found." link instead of redirecting | `statusRewrites: {"401": 302}` missing from the errors middleware |
| Logged in but the app 403s | app-level authz, not us — check `X-Auth-Request-Email` reaches it |
| Everyone locked out | allowlist file empty/unmounted — it fails closed by design |
| "Grant Access ... would like to view your email" on every login | oauth2-proxy is sending `approval_prompt=force`, which makes Dex ignore its own `skipApprovalScreen`. Fix is `--prompt=select_account` on oauth2-proxy; an empty `--approval-prompt=` does NOT work |
| Dex config change has no effect | Dex reads config only at startup: `kubectl -n dex rollout restart deploy/dex` |

Logs: `kubectl -n oauth2-proxy logs -l app.kubernetes.io/instance=admin`.

## Metrics

Both Dex and oauth2-proxy are scraped; the Grafana dashboard
`application-performance` has the panels (ConfigMap in
`cluster/grafana/dashboards/`).

| Source | Series | Notes |
|---|---|---|
| Dex `:5558` | `http_requests_total{handler,code,method}` | `/auth` = a login started, `/callback` = the IdP came back, `/token` = a code was exchanged, i.e. a **completed** sign-in |
| Dex | `request_duration_seconds` | histogram |
| oauth2-proxy `:44180` | `oauth2_proxy_requests_total{code}`, `oauth2_proxy_requests_in_flight`, `oauth2_proxy_response_duration_seconds` | needs `--metrics-address`, which is set |

Two traps here, both of which produce silence rather than errors:

- **`http_requests_total` is a generic name** shared with other exporters.
  Always scope it: `http_requests_total{namespace="dex", …}`.
- **`oauth2_proxy_requests_total` carries only a `code` label** — nothing says
  which tier served the request. The ServiceMonitor promotes the Service's
  `app.kubernetes.io/instance` via `targetLabels`, so queries can
  `sum by (app_kubernetes_io_instance)` to split admin from family. Without
  that the two tiers are indistinguishable.

And the cluster-wide one: **a ServiceMonitor without `release: prometheus` is
never selected** and collects nothing, silently — as is one whose selector
matches a Service with no labels. Both Services here had no labels at all until
2026-09-23. After adding a monitor, always confirm the target is actually up:

```bash
kubectl -n prometheus exec prometheus-prometheus-kube-prometheus-prometheus-0 -c prometheus -- \
  wget -qO- 'http://localhost:9090/api/v1/targets?state=active' \
  | jq -r '.data.activeTargets[] | select(.labels.namespace|test("dex|oauth2-proxy")) | "\(.labels.namespace)/\(.labels.service) \(.health)"'
```

## History

authentik ran here until 2026-09. Its entire configured state was one
application, one provider and two users — a forward-auth gate on seven admin
UIs — costing 7 workloads, 1.67 GB of RAM and 72 GiB of PVCs, and facing an
eight-hop upgrade path with irreversible Django migrations. It was replaced
rather than upgraded. The seven hosts moved one at a time (share first), with
`.claude/skills/edge-ingress/edge-snapshot.sh` diffed before and after as the
regression gate; the only changes were the seven expected redirect targets.
