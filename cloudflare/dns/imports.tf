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
