locals {
  # Every zone in the account that fronts cluster ingresses. All four are
  # managed here as of 2026-09-24.
  zones = {
    "chrismiller.xyz" = {
      id      = "dc0498d92beeae3805bd4c34631126e7"
      proxied = true # apex sits behind the Cloudflare proxy (as before)
    }
    "realliance.net" = {
      id      = "797612475ed6a8f7193a623ffc107f07"
      proxied = false
    }
    # Adopted 2026-09-24 so the family side of the house is managed like the
    # rest. DNS-only, which is how its apex already was — and the right
    # default for a zone meant to carry media uploads (the proxy caps
    # request bodies at 100 MB on this plan).
    "werethemille.rs" = {
      id      = "820af0c68573859bd1d0b6bd41185188"
      proxied = false
    }
    # Adopted 2026-09-24 as well. Nothing in it but the apex, so there was
    # nothing to reconcile — the wildcard below is the only new record.
    "buttert.art" = {
      id      = "d8b2dca633086f6ea526c979b2082276"
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

# ------------------------------- werethemille.rs: two records left undeclared
#
# Two records already in that zone are deliberately NOT declared here. The
# provider only manages what it is told about — it does not delete strangers —
# so leaving them out keeps `plan` empty. Both want a decision:
#
#   cams  A 152.44.247.29   same ISP /25 as our WAN, but not our current
#                           address — a stale former DHCP lease, which means
#                           it now points at someone else's line
#   home  A 35.138.137.227  a Spectrum residential address, not ours
#
# Ids, for whichever way each goes (adopt as an explicit resource, or delete):
#   cams  f366bf784954207d8b41438e9107d48f
#   home  824755a73206c9ff6fa55b027a3fd5e7
#
# The wildcard above does not fight either of them: an explicit record always
# beats `*`, so both keep resolving exactly as they do today.
#
# Also removed from this zone on 2026-09-24: `watch` and `a-watch` TXT, both
# external-dns registry orphans naming ingress/media/cm-acme-http-solver-2xdgj.
# The records they tracked were already gone — debris from the 2026-09-18 wipe.
