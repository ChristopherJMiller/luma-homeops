# cloudflare/dns — public DNS for the galaxy edge

Terraform-managed records in the zones that front cluster ingresses.
State lives in the shared Azure backend (`cloudflare-dns.tfstate`).

## Design

```
chrismiller.xyz     A      <WAN IP>          proxied   ┐
realliance.net      A      <WAN IP>          DNS-only  │ content kept current by
werethemille.rs     A      <WAN IP>          DNS-only  │ cloudflareddns (WAN is DHCP)
buttert.art         A      <WAN IP>          DNS-only  │
realliance.page     A      <WAN IP>          DNS-only  ┘
*.chrismiller.xyz   CNAME  chrismiller.xyz   DNS-only  ┐
*.realliance.net    CNAME  realliance.net    DNS-only  │ static — every hostname
*.werethemille.rs   CNAME  werethemille.rs   DNS-only  │ reaches Traefik, which
*.buttert.art       CNAME  buttert.art       DNS-only  │ routes by Host header
*.realliance.page   CNAME  realliance.page   DNS-only  ┘

docs.realliance.net  Workers Custom Domain → Worker realliance-docs   workers.tf
                     its own proxied record and certificate, on the workers token;
                     the exact name beats *.realliance.net, so galaxy never serves it
```

One name doesn't reach galaxy. `docs.realliance.net` is a Workers Custom
Domain on the Worker `realliance-docs`, so the Realliance docs keep serving
from Cloudflare's edge while galaxy or the uplink is down. The custom domain
brings its own DNS record and certificate. Don't add a `docs` record to
`records.tf`: it would block the custom domain.

If Cloudflare refuses the custom domain over the wildcard, a fallback is
pre-approved for that case only: an `AAAA docs 100::` record with
`proxied = true` in `records.tf`, plus a `cloudflare_workers_route` for
`docs.realliance.net/*`. The header of `workers.tf` has the swap.
`scripts/test_docs_site.py` (the `docs-site` hook) passes on exactly one of
the two.

Adding a public app is **one `Ingress` with a host**. No DNS change, no
annotation, no controller. Explicit records still beat the wildcard, so a
single host can be made proxied later by adding a record here.

Never add a TXT record on the `realliance.page` apex: the wildcard CNAME makes every owner's `_git-pages-*` challenge name resolve to it (`docs/pages.md`).

This replaced external-dns (retired 2026-09) — 47 identical per-host CNAMEs
managed by a controller with delete authority over public DNS, which wiped
them all on a routine minor bump (see git log for `cluster/dns/`, formerly `cluster/external-dns/`).

## Not managed here

- `ipv8.dev`, `mctherealm.net`, `smallturtle.house` — also in this Cloudflare
  account, but they don't front the galaxy edge. Adopt one the same way
  `werethemille.rs` and `buttert.art` were (2026-09-24): add it to
  `local.zones`, add an `import` block for its existing apex record, `plan`,
  `apply`. Also add it to `CF_HOSTS`/`CF_ZONES` in
  `cluster/dns/cloudflareddns-config.yaml` if its apex should track the WAN IP.
- Two pre-existing `werethemille.rs` records (`cams`, `home`) — see the note at
  the bottom of `records.tf`. Undeclared, so terraform leaves them alone. (Two
  external-dns TXT orphans in the same zone were deleted 2026-09-24.)
- Mail/verification TXT, MX etc. — created by hand; import if you want them
  under terraform (`terraform import 'cloudflare_dns_record.x' <zone>/<id>`).
- `_acme-challenge.*` TXT — transient, owned by cert-manager (DNS-01).

## Workflow

```bash
nix-shell shell.nix          # terraform + az
az login                     # state backend auth, if stale
./cloudflare/dns/tf.sh init
./cloudflare/dns/tf.sh plan  # must be empty after apply — drift check
./cloudflare/dns/tf.sh apply
```

`tf.sh` exports two tokens, each from its own git-crypt file, and never
prints either:

- `CLOUDFLARE_API_TOKEN`, the DNS token the cluster already uses
  (`cluster/dns/cloudflare-api-token.secret.yaml`), for the default provider
  and every record.
- `TF_VAR_workers_api_token`, the workers token
  (`cloudflare/dns/workers-api-token.secret.conf`), for the
  `cloudflare.workers` provider alias, which only Workers resources use. The
  cluster never gets it: `sign.sh` seals only `cluster/**/*.secret.yaml`.

`tf.sh` stops and names the workers file when it's missing, still encrypted,
or lacks exactly one `api-token=<value>` line.

### The workers token

Chris creates it in the Cloudflare dashboard, scoped to:

- the account that owns `realliance.net`: Workers, role Editor, all Workers
  (Custom Domains have no per-Worker roles)
- the zone `realliance.net` only: Workers Routes, Write

He then writes it without echoing it, from the repository root:

```bash
( umask 077; read -rsp 'workers token: ' t \
  && printf 'api-token=%s\n' "$t" > cloudflare/dns/workers-api-token.secret.conf )
git add cloudflare/dns/workers-api-token.secret.conf
git cat-file -p :cloudflare/dns/workers-api-token.secret.conf | head -c 10 | od -c  # \0 G I T C R Y P T
```

## Verify

```bash
dig +short anything-at-all.chrismiller.xyz @1.1.1.1   # -> WAN IP
dig +short www.realliance.net @1.1.1.1                # -> realliance.net. + WAN IP
dig +short docs.realliance.net @1.1.1.1               # -> Cloudflare addresses, never the WAN IP
```
