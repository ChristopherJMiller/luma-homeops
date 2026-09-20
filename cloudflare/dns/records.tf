locals {
  # Zones that front cluster ingresses. buttert.art / werethemille.rs only
  # carry a cloudflareddns-maintained apex and are intentionally unmanaged.
  zones = {
    "chrismiller.xyz" = {
      id      = "dc0498d92beeae3805bd4c34631126e7"
      proxied = true # apex sits behind the Cloudflare proxy (as before)
    }
    "realliance.net" = {
      id      = "797612475ed6a8f7193a623ffc107f07"
      proxied = false
    }
  }

  # Seed value only. The WAN interface is DHCP, so cloudflareddns (dns ns)
  # owns the *content* of the apex records; terraform owns everything else.
  wan_ip = "152.44.247.88"
}

# Apex: <zone> A <WAN IP>
resource "cloudflare_dns_record" "apex" {
  for_each = local.zones

  zone_id = each.value.id
  name    = each.key
  type    = "A"
  content = local.wan_ip
  proxied = each.value.proxied
  ttl     = 1 # automatic
  comment = "galaxy edge. IP maintained by cloudflareddns; terraform ignores content."

  lifecycle {
    ignore_changes = [content]
  }
}

# Wildcard: *.<zone> CNAME <zone>
#
# DNS-only on purpose: Plex, attic (multi-GB pushes) and WebDAV need to
# bypass the Cloudflare proxy, and an explicit per-host record can still
# opt a single name back in later (explicit beats wildcard). Cloudflare
# flattens a DNS-only CNAME to a same-zone proxied apex to the origin IP,
# which is the pre-existing behaviour for every subdomain.
resource "cloudflare_dns_record" "wildcard" {
  for_each = local.zones

  zone_id = each.value.id
  name    = "*.${each.key}"
  type    = "CNAME"
  content = each.key
  proxied = false
  ttl     = 1
  comment = "galaxy edge: every hostname routes to Traefik, which selects by Host header."
}
