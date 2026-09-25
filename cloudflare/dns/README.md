# cloudflare/dns — public DNS for the galaxy edge

Terraform-managed records in the zones that front cluster ingresses.
State lives in the shared Azure backend (`cloudflare-dns.tfstate`).

## Design

```
chrismiller.xyz     A      <WAN IP>          proxied   ┐
realliance.net      A      <WAN IP>          DNS-only  │ content kept current by
werethemille.rs     A      <WAN IP>          DNS-only  │ cloudflareddns (WAN is DHCP)
buttert.art         A      <WAN IP>          DNS-only  ┘
*.chrismiller.xyz   CNAME  chrismiller.xyz   DNS-only  ┐
*.realliance.net    CNAME  realliance.net    DNS-only  │ static — every hostname
*.werethemille.rs   CNAME  werethemille.rs   DNS-only  │ reaches Traefik, which
*.buttert.art       CNAME  buttert.art       DNS-only  ┘ routes by Host header
```

Adding a public app is **one `Ingress` with a host**. No DNS change, no
annotation, no controller. Explicit records still beat the wildcard, so a
single host can be made proxied later by adding a record here.

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

`tf.sh` exports `CLOUDFLARE_API_TOKEN` from the git-crypt secret the cluster
already uses (`cluster/dns/cloudflare-api-token.secret.yaml`) and
never prints it.

## Verify

```bash
dig +short anything-at-all.chrismiller.xyz @1.1.1.1   # -> WAN IP
dig +short www.realliance.net @1.1.1.1                # -> realliance.net. + WAN IP
```
