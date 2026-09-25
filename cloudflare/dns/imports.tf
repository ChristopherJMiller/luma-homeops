# One-time adoption of the pre-existing apex records (created by hand /
# cloudflareddns long before this module). Safe to leave in place: an
# import block for a resource already in state is a no-op.
import {
  to = cloudflare_dns_record.apex["chrismiller.xyz"]
  id = "dc0498d92beeae3805bd4c34631126e7/3ba9aafbe5e1448968d3db8a1803d3a2"
}

import {
  to = cloudflare_dns_record.apex["realliance.net"]
  id = "797612475ed6a8f7193a623ffc107f07/6079876047bd5fbc698f345e207bffc7"
}

import {
  to = cloudflare_dns_record.apex["werethemille.rs"]
  id = "820af0c68573859bd1d0b6bd41185188/f02a760d032f30111976dddc77cec52b"
}

import {
  to = cloudflare_dns_record.apex["buttert.art"]
  id = "d8b2dca633086f6ea526c979b2082276/a18c124d12fae3102c4a23bf80c93355"
}

# Mail + verification records (mail.tf). All pre-existed by hand; adopting
# them is what lets terraform correct the two that were wrong (MX host names,
# and DKIM being proxied).
import {
  to = cloudflare_dns_record.mx_primary
  id = "dc0498d92beeae3805bd4c34631126e7/9b88f5ebbc68d67ee3e24c86f77feda2"
}

import {
  to = cloudflare_dns_record.mx_secondary
  id = "dc0498d92beeae3805bd4c34631126e7/c3b25de584c9b64fd890f0459b8048a7"
}

import {
  to = cloudflare_dns_record.dkim["fm1"]
  id = "dc0498d92beeae3805bd4c34631126e7/929fcc2c867343d5eb3e31b65f63163b"
}

import {
  to = cloudflare_dns_record.dkim["fm2"]
  id = "dc0498d92beeae3805bd4c34631126e7/3df80f5f67251e1cb685b8d30b5680a3"
}

import {
  to = cloudflare_dns_record.dkim["fm3"]
  id = "dc0498d92beeae3805bd4c34631126e7/80b9d58cf1802242b33943249ea278f4"
}

import {
  to = cloudflare_dns_record.spf
  id = "dc0498d92beeae3805bd4c34631126e7/8e86d2eba2af646e3aa819f8746030a2"
}

import {
  to = cloudflare_dns_record.dmarc
  id = "dc0498d92beeae3805bd4c34631126e7/8c727c3d948abf2421a307d8c453da47"
}
