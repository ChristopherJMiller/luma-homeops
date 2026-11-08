# docs.realliance.net: the Realliance docs, served by the assets-only Worker
# `realliance-docs` from Cloudflare's edge, so the site keeps serving while
# galaxy or the home uplink is down. Design: "DNS" in
# docs/superpowers/specs/2026-11-07-realliance-docs-site-design.md.
#
# A Workers Custom Domain brings its own proxied DNS record and certificate.
# The exact name beats the *.realliance.net wildcard; every other name still
# reaches Traefik. Never add a `docs` record in records.tf: it would block
# the custom domain.
#
# Fallback, pre-approved for one case only, Cloudflare refusing the custom
# domain over the wildcard: delete the resource below and add, in one commit,
# a proxied `AAAA docs 100::` record in records.tf (default provider) plus a
# `cloudflare_workers_route` for `docs.realliance.net/*` to `realliance-docs`
# on the workers provider. scripts/test_docs_site.py accepts exactly one of
# the two shapes.

# The account id comes from the zone, so no file holds it and no variable or
# secret carries it.
data "cloudflare_zone" "realliance_net" {
  zone_id = local.zones["realliance.net"].id
}

resource "cloudflare_workers_custom_domain" "docs" {
  provider   = cloudflare.workers
  account_id = data.cloudflare_zone.realliance_net.account.id
  zone_id    = local.zones["realliance.net"].id
  hostname   = "docs.realliance.net"
  service    = "realliance-docs"
}
