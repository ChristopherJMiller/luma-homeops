# Edge authentication — trust tiers, and how to change who gets in

Public hosts are gated by **oauth2-proxy**, one instance per *trust tier*,
with identity from Google. There is no admin UI and no database: the
configuration is this repo. Replaced authentik 2026-09 (see "History").

```
browser ──▶ Traefik ──▶ [errors, auth] middlewares ──▶ oauth2-proxy-<tier> ──▶ Google
                                    │
                              401 ──┴──▶ 302 to /oauth2/sign_in ──▶ Google ──▶ callback ──▶ cookie
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

Non-Gmail addresses work if they are Google identities (Workspace counts —
`kmiller.org` is Workspace-managed). A plain Microsoft/Outlook address does
not; see "Multi-provider" below.

## Google OAuth client

One client (`galaxy-sso`) shared by both tiers, created by hand in the Google
Cloud console — Google exposes no API for creating "Web application" OAuth
clients, so this artifact cannot be Terraformed. Its redirect URIs:

- `https://auth-admin.chrismiller.xyz/oauth2/callback`
- `https://auth-family.chrismiller.xyz/oauth2/callback`
- `https://dex.chrismiller.xyz/callback` (for the multi-provider work)

The consent screen is in **Testing** mode, so only accounts listed as test
users can complete a login — a second gate in front of the allowlist. Adding
someone means adding them in *both* places.

Client id/secret and the two per-tier cookie secrets live in
`cluster/oauth2-proxy/google-oauth.secret.yaml` (git-crypt → SealedSecret).

## Multi-provider (planned)

oauth2-proxy supports exactly one provider per instance, so Microsoft/Outlook
accounts cannot use the Google-backed instances. The fix is **Dex** brokering
both, with oauth2-proxy switched from `--provider=google` to `--provider=oidc`
pointing at Dex. Tiers, middlewares, allowlists, cookies and ingresses are
unchanged by that swap — Dex passes the upstream email through, so the
allowlists keep matching real addresses.

Dex brings its own login page (a connector chooser), themeable via
`frontend.issuer` / `logoURL` / `theme`, and stores state in Kubernetes CRDs —
no extra database. The Entra app registration for the Microsoft side *is*
Terraformable (`azuread_application`), unlike the Google client.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Bare `401` instead of a login page | middleware order — `errors` must precede `auth` |
| Redirect loop | the host is gating itself, or `--whitelist-domain` doesn't cover it |
| "Found." link instead of redirecting | `statusRewrites: {"401": 302}` missing from the errors middleware |
| Logged in but the app 403s | app-level authz, not us — check `X-Auth-Request-Email` reaches it |
| Everyone locked out | allowlist file empty/unmounted — it fails closed by design |

Logs: `kubectl -n oauth2-proxy logs -l app.kubernetes.io/instance=admin`.

## History

authentik ran here until 2026-09. Its entire configured state was one
application, one provider and two users — a forward-auth gate on seven admin
UIs — costing 7 workloads, 1.67 GB of RAM and 72 GiB of PVCs, and facing an
eight-hop upgrade path with irreversible Django migrations. It was replaced
rather than upgraded. The seven hosts moved one at a time (share first), with
`.claude/skills/edge-ingress/edge-snapshot.sh` diffed before and after as the
regression gate; the only changes were the seven expected redirect targets.
