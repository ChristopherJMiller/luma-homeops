---
name: edge-ingress
description: Expose a cluster service publicly, or change anything on the Traefik edge (Ingress, middleware, TLS, the Traefik chart). Use when adding a public hostname for an app, gating an app behind SSO, changing CSP/CORS behaviour, or touching cluster/traefik/. Encodes the post-2026-09 design (one Ingress with a host; wildcard DNS + wildcard cert; no per-host DNS/cert/annotation) and the 25-host behavioural snapshot that is the regression test for every edge change. NEVER adds external-dns or cert-manager annotations to an Ingress. NEVER adds a per-host Certificate for a public hostname. NEVER changes Traefik chart values without a before/after snapshot diff.
---

# edge-ingress

How public traffic reaches galaxy, and how to change it without breaking the other 24 hosts.

```
*.chrismiller.xyz  ──CNAME──▶ chrismiller.xyz ──A──▶ WAN IP ──NAT 80/443──▶ Traefik .7 ──Host header──▶ Ingress ──▶ Service
*.realliance.net   ──CNAME──▶ realliance.net  ─┤                                │
*.werethemille.rs  ──CNAME──▶ werethemille.rs ─┤                    wildcard-tls (default TLSStore)
*.buttert.art      ──CNAME──▶ buttert.art     ─┘
```

Three things make a new app trivial, and all three are invariants — don't erode them:

1. **DNS is one wildcard record per zone** (terraform, `cloudflare/dns/`) across the four edge zones: `chrismiller.xyz`, `realliance.net`, `werethemille.rs`, `buttert.art`. Any `<name>` in any of them already resolves to the edge. No record per app.
2. **TLS is one wildcard cert** (`traefik/wildcard-tls`, SANs `chrismiller.xyz, *.chrismiller.xyz, *.music.chrismiller.xyz, realliance.net, *.realliance.net, werethemille.rs, *.werethemille.rs, buttert.art, *.buttert.art`) served as Traefik's default TLSStore. No `spec.tls`, no `Certificate` per app. Adding a zone means adding its two names here — and nothing else.
3. **Traefik is the default IngressClass.** `ingressClassName` is optional; set it anyway for grep-ability.

## Add a public app (the whole procedure)

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: myapp
  namespace: myapp
  # annotations:
  #   traefik.ingress.kubernetes.io/router.middlewares: >-
  #     oauth2-proxy-oauth2-admin-errors@kubernetescrd,oauth2-proxy-oauth2-admin-auth@kubernetescrd
spec:
  ingressClassName: traefik
  rules:
    - host: myapp.chrismiller.xyz
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: myapp
                port:
                  number: 80
```

Commit, push, Argo syncs. Then add the host to `edge-snapshot.sh` (next to this file) so it's covered by the regression test. That is all.

**Helm-rendered ingresses** (argo-cd, home-assistant, plex, webdav, grafana, royaltracker): same content via chart values — `className: traefik` (or `ingressClassName:` for kps/argo-cd), no `tls:` block, middleware annotation if needed. Check the chart's ingress template if unsure how it spells things; `webdav` and `royaltracker` use `tls.enabled: false`.

## Middlewares (the only knobs)

Reference as `<namespace>-<name>@kubernetescrd`, comma-separated, in `traefik.ingress.kubernetes.io/router.middlewares`. Order matters: auth first.

| Middleware | Lives in | Use for |
|---|---|---|
| `oauth2-proxy-oauth2-<tier>-errors`, `oauth2-proxy-oauth2-<tier>-auth` | `cluster/oauth2-proxy/middlewares.yaml` | Gate behind SSO (Google). ALWAYS reference **both**, errors first — reversed gives a bare 401 instead of a login redirect. Tiers: `admin` (you), `family` (relatives). See `docs/auth.md`. |
| `traefik-strip-csp` | `cluster/traefik/middlewares.yaml` (ns traefik) | Apps whose own CSP breaks behind a proxy (the *arr stack, HA, plex). |
| `chrismillerxyz-cors` | `cluster/traefik/middlewares-extra.yaml` | The static site; mirrors nginx `enable-cors`. |
| `realliance-net-set-csp` | `cluster/traefik/middlewares-extra.yaml` | Sets a strict CSP on realliance.net. |

New per-app middleware → add to `middlewares-extra.yaml` in the app's namespace (Traefik has `allowCrossNamespace: true`).

Sticky sessions / WebSockets: WS is native. Sticky goes on the **Service** as `traefik.ingress.kubernetes.io/service.sticky.cookie*` annotations (see `cluster/word-arena/backend.yaml`), not the Ingress. HTTPS backend: `traefik.ingress.kubernetes.io/service.serversscheme: https` on the Service (argo-cd); `insecureSkipVerify` is chart-wide.

## The escape hatches (rare)

- **Host needs the Cloudflare proxy** (DDoS, caching): add an explicit *proxied* record in `cloudflare/dns/records.tf`. Explicit beats wildcard. Note the wildcard is DNS-only on purpose — Plex, attic multi-GB pushes and WebDAV break behind the proxy.
- **Host needs a different cert**: add `spec.tls` with its own secret; Traefik prefers an explicit SNI match over the default store. You almost certainly don't need this.
- **Path split on one host** (share.chrismiller.xyz): two Ingresses, same host, different `path:`; Traefik's longest-prefix routing does the rest. Public path gets no auth middleware.

## Regression test — run it for ANY edge change

`edge-snapshot.sh` hits every public host through `.7` directly (`--resolve`, bypassing DNS/NAT) and records status, redirect target, auth challenge, CSP/CORS headers, and served-cert CN.

```bash
cd .claude/skills/edge-ingress
nix-shell -p openssl --run ./edge-snapshot.sh > /tmp/before.txt
# make the change, wait for Argo
nix-shell -p openssl --run ./edge-snapshot.sh > /tmp/after.txt
diff <(sed 's/ cert=.*//' /tmp/before.txt) <(sed 's/ cert=.*//' /tmp/after.txt)
```

Empty diff = no behavioural change. Any line that differs is either your intended change or a regression — decide which before moving on. This is what proved the 2026-09-20 canonicalization of 25 ingresses was byte-for-byte identical.

Also check: `kubectl -n traefik logs -l app.kubernetes.io/name=traefik --since=10m | grep -c ERR` (0). A burst of `Cannot create service: "service not found"` means an Ingress points at a Service that doesn't exist.

## Traefik chart changes (`cluster/applications/traefik-release.yaml`)

The chart's `values.schema.json` is strict — `ports.web.http.redirections`, top-level `accessLog`. `readTimeout: 0` on both entrypoints is deliberate (attic pushes, WebDAV uploads); don't "fix" it. Two replicas + PDB; a chart bump rolls one at a time. Snapshot before/after.

## What you will be tempted to do, and shouldn't

- Add `external-dns.kubernetes.io/target` — nothing reads it. external-dns is retired and must stay retired (it deleted every public record on a routine minor bump, 2026-09-18).
- Add `cert-manager.io/cluster-issuer` or a per-host `Certificate` — the wildcard already covers it; you'd just add a DNS-01 challenge every 60 days for nothing.
- Add nginx annotations — there is no nginx.
- Reach for ingress-nginx compatibility snippets — translate to a Middleware instead.

## Related

- `vyos-deploy` — if the change needs a router NAT/firewall rule (it almost never does; 80/443 already forward to .7).
- `cloudflare-dns` — for the explicit-record escape hatch.
- `safe-rollout` — for watching the Traefik Deployment after a chart bump.
