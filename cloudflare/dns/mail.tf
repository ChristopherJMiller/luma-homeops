# Mail and domain-verification records for chrismiller.xyz.
#
# Mail is Fastmail (messagingengine.com). These records existed by hand before
# this module and are adopted via imports.tf — see README. Two of them were
# wrong when adopted, and are corrected here:
#
#   1. MX pointed at in1/in2-smtp; Fastmail's current hosts are us1/us2-smtp.
#   2. The DKIM CNAMEs were `proxied = true`, which BREAKS them — Cloudflare
#      answers with its own anycast IPs instead of the fmhosted.com target, so
#      nothing can fetch the public key and DKIM never verifies. Fastmail's
#      setup instructions call this out ("disable Cloudflare Proxy"). With
#      DMARC at p=reject, unsigned mail is a deliverability problem.
#
# Anything a mail provider needs to look up must be DNS-only. The proxy only
# understands HTTP.

locals {
  cm_zone = local.zones["chrismiller.xyz"].id
}

# ---------------------------------------------------------------- MX

resource "cloudflare_dns_record" "mx_primary" {
  zone_id  = local.cm_zone
  name     = "chrismiller.xyz"
  type     = "MX"
  content  = "us1-smtp.messagingengine.com"
  priority = 10
  ttl      = 1
  comment  = "Fastmail primary."
}

resource "cloudflare_dns_record" "mx_secondary" {
  zone_id  = local.cm_zone
  name     = "chrismiller.xyz"
  type     = "MX"
  content  = "us2-smtp.messagingengine.com"
  priority = 20
  ttl      = 1
  comment  = "Fastmail secondary."
}

# ---------------------------------------------------------------- DKIM

# fm1/fm2/fm3._domainkey CNAME fmN.chrismiller.xyz.dkim.fmhosted.com
# MUST be DNS-only (see header).
resource "cloudflare_dns_record" "dkim" {
  for_each = toset(["fm1", "fm2", "fm3"])

  zone_id = local.cm_zone
  name    = "${each.key}._domainkey.chrismiller.xyz"
  type    = "CNAME"
  content = "${each.key}.chrismiller.xyz.dkim.fmhosted.com"
  proxied = false # never true: proxying hides the key from verifiers
  ttl     = 1
  comment = "Fastmail DKIM. Must stay DNS-only."
}

# ---------------------------------------------------------------- SPF / DMARC

resource "cloudflare_dns_record" "spf" {
  zone_id = local.cm_zone
  name    = "chrismiller.xyz"
  type    = "TXT"
  content = "\"v=spf1 include:spf.messagingengine.com -all\""
  ttl     = 1
  comment = "Fastmail SPF. -all: anything else is a forgery."
}

resource "cloudflare_dns_record" "dmarc" {
  zone_id = local.cm_zone
  name    = "_dmarc.chrismiller.xyz"
  type    = "TXT"
  content = "\"v=DMARC1; p=reject; rua=mailto:postmaster@chrismiller.xyz\""
  ttl     = 1
  comment = "p=reject — which is why DKIM being broken actually mattered."
}

# ---------------------------------------------------- domain verification

# Proves domain ownership to Google Search Console, which is what the OAuth
# consent screen checks before it will publish the app out of Testing mode
# (docs/auth.md). A Search Console *Domain* property covers every subdomain,
# so galaxy.chrismiller.xyz is verified by this one record.
resource "cloudflare_dns_record" "google_site_verification" {
  zone_id = local.cm_zone
  name    = "chrismiller.xyz"
  type    = "TXT"
  content = "\"google-site-verification=Ii_XfY8Kuq8YKA1W-HMz_dUWXEyb5MmEfp_wZ7iCRDw\""
  ttl     = 1
  comment = "Google Search Console domain property; gates OAuth consent-screen branding."
}
