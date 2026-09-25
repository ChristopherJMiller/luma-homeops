---
name: cloudflare-dns
description: Read or change public DNS for chrismiller.xyz / realliance.net / werethemille.rs / buttert.art via the terraform module in cloudflare/dns/. Use when a hostname doesn't resolve, when a host needs the Cloudflare proxy or a non-wildcard record, when adding MX/TXT/verification records, or when checking for drift. Covers the state-backend login dance, the apex ignore_changes rule, and what "plan is not empty" means. NEVER edits records in the Cloudflare dashboard or via ad-hoc API calls when terraform owns them. NEVER re-introduces external-dns. NEVER removes the wildcard records.
---

# cloudflare-dns

Public DNS is code: `cloudflare/dns/{main,records,imports}.tf`, state in the shared Azure backend (`cloudflare-dns.tfstate`). The design is deliberately tiny — read `cloudflare/dns/README.md` first.

```
chrismiller.xyz    A      <WAN IP>          proxied   ┐ content maintained by
realliance.net     A      <WAN IP>          DNS-only  │ cloudflareddns (WAN is DHCP)
werethemille.rs    A      <WAN IP>          DNS-only  │
buttert.art        A      <WAN IP>          DNS-only  ┘
*.chrismiller.xyz  CNAME  chrismiller.xyz   DNS-only  ┐
*.realliance.net   CNAME  realliance.net    DNS-only  │ static; every public
*.werethemille.rs  CNAME  werethemille.rs   DNS-only  │ hostname resolves to the edge
*.buttert.art      CNAME  buttert.art       DNS-only  ┘
```

## Ground rules

- **terraform owns records; hand edits are drift.** If `plan` is not empty and you didn't change `.tf`, someone (or something) edited Cloudflare directly. Find out what before `apply` reverts it.
- **The apex A records are half-owned.** terraform owns existence / `proxied` / `ttl` / `comment`; `lifecycle.ignore_changes = [content]` leaves the IP to `cloudflareddns` (ns `dns`), because the WAN interface is DHCP. Don't remove that block "to make plan cleaner" — it would fight the DDNS updater.
- **Never re-add external-dns.** It's a controller with delete authority over public DNS that wiped every record on a routine minor bump (2026-09-18). 47 identical `<host> CNAME <apex>` records were never a job for a controller.
- **All four edge zones are managed as of 2026-09-24**: `chrismiller.xyz`, `realliance.net`, `werethemille.rs`, `buttert.art` (the last two adopted that day — apex imported, wildcard created). Two pre-existing `werethemille.rs` records (`cams`, `home`) are still undeclared on purpose (two external-dns TXT orphans there were deleted 2026-09-24) — see the note at the bottom of `records.tf`. `ipv8.dev`, `mctherealm.net` and `smallturtle.house` are in the account but outside this module.

## Workflow

```bash
nix-shell shell.nix                 # terraform + az (pinned nixpkgs — the host channel's azure-cli was broken)
./cloudflare/dns/tf.sh init         # first time, or after a provider bump
./cloudflare/dns/tf.sh plan         # read this. every line.
./cloudflare/dns/tf.sh apply
./cloudflare/dns/tf.sh plan         # must be empty afterwards
```

`tf.sh` exports `CLOUDFLARE_API_TOKEN` from `cluster/dns/cloudflare-api-token.secret.yaml` (git-crypt; the same token cloudflareddns and cert-manager use). It never prints the token. Don't echo it either (S9).

**State backend auth** — if `init`/`plan` fails with `retrieving Storage Account … AADSTS…` the az session is stale:

```bash
az logout
az login --tenant ee0a571a-7499-4994-b349-164a9150b094 --use-device-code
```

(`--use-device-code` works from the nix shell; the browser flow doesn't.) Chris runs this — it needs his browser.

## Common tasks

**A host must go through the Cloudflare proxy** (explicit beats wildcard):

```hcl
resource "cloudflare_dns_record" "myapp_proxied" {
  zone_id = local.zones["chrismiller.xyz"].id
  name    = "myapp.chrismiller.xyz"
  type    = "CNAME"
  content = "chrismiller.xyz"
  proxied = true
  ttl     = 1
  comment = "proxied on purpose: <why>"
}
```

Remember the proxy breaks large uploads, WebDAV, and anything non-HTTP. Plex, attic, mediadav must stay DNS-only.

**Adopt a hand-made record** (MX, SPF/DKIM TXT, site verification): write the resource, then add an `import` block to `imports.tf` with `<zone_id>/<record_id>` — get the id with a read-only API call:

```bash
T=$(awk '/^  api-token:/ {gsub(/["'"'"']/,"",$2); print $2}' cluster/dns/cloudflare-api-token.secret.yaml)
curl -sS -H "Authorization: Bearer $T" \
  "https://api.cloudflare.com/client/v4/zones/dc0498d92beeae3805bd4c34631126e7/dns_records?name=myapp.chrismiller.xyz" \
  | jq -r '.result[] | "\(.id) \(.type) \(.name) proxied=\(.proxied)"'; unset T
```

Zone ids: chrismiller.xyz `dc0498d92beeae3805bd4c34631126e7`, realliance.net `797612475ed6a8f7193a623ffc107f07`.

**`_acme-challenge.*` TXT records** are transient — cert-manager's DNS-01 solver creates and deletes them. Never import them; if one is stuck, check `kubectl get challenge -A` first.

## Diagnosing "host doesn't resolve"

```bash
dig +short myapp.chrismiller.xyz @1.1.1.1      # authoritative-ish answer
dig +short myapp.chrismiller.xyz @192.168.0.1  # what LAN clients see (router forwarder)
```

- Empty at 1.1.1.1 → the wildcard is gone or an explicit empty record shadows it. `tf.sh plan` will show which.
- OK at 1.1.1.1, empty at the router → the router's negative cache (SOA minimum is 1800 s). It clears itself; `reset dns forwarding all` on VyOS if you can't wait. A LAN client that hammered the name during an outage is the usual cause.
- Resolves but the app 404s → it's an Ingress problem, not DNS. See `edge-ingress`.

## Renovate

The provider (`cloudflare/cloudflare ~> 5.0`) is pinned in `.terraform.lock.hcl` and Renovate will PR bumps. A **major** provider bump can rename resource types (v4→v5 did: `cloudflare_record` → `cloudflare_dns_record`). Run `plan` on the PR branch before merging; it must show zero changes.

## Related

- `edge-ingress` — the other half of "make this app public".
- `renovate-triage` — before merging a provider bump.
