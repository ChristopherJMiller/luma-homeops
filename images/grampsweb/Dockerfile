# Gramps Web, plus one patch: auto-promote a listed family address on first login.
#
# Why this image exists at all. Upstream creates every new OIDC account as
# ROLE_DISABLED, so a relative's first sign-in needs a hand-written UPDATE in
# Postgres before they can get in — and until then they see a bare
# "Internal Server Error", because the callback resolves PERMISSIONS[role]
# before it checks for a disabled role (gramps-web-api master, oidc.py:350,
# which is a separate upstream fix). Chris's mother hit exactly that on
# 2026-09-28. oidc-new-user-role.patch adds OIDC_NEW_USER_ROLE, granted ONLY to
# an address on OIDC_NEW_USER_ROLE_EMAILS_FILE — see cluster/gramps/config.yaml
# and docs/gramps.md.
#
# The base is pinned by digest and Renovate bumps it. On EVERY base bump:
#   1. check the new image's GRAMPS_VERSION env (60 today) — desktop Gramps Web
#      Sync breaks unless the server and the laptop agree on Gramps MAJOR.MINOR;
#   2. merge, let this workflow publish sha-<short>, THEN pin that tag in
#      cluster/gramps/grampsweb.yaml. Argo keeps running the old image until
#      that second commit lands, which is the safe direction to fail.
#
# The patch is applied to the installed package rather than built from source:
# upstream's own build is what produces the frontend bundle, the Gramps addons
# and the venv, and none of that should be reproduced here to change nine lines.
FROM ghcr.io/gramps-project/grampsweb:latest@sha256:ac4db1ba963f5d2604e6b9a34be50c5a2c37271699f5a6df93c9492ecc8870e5

COPY oidc-new-user-role.patch /tmp/oidc-new-user-role.patch

# --fuzz=0: a base bump that rewrites this function must FAIL the build rather
# than apply the hunk somewhere approximate. The import check afterwards is the
# proof that the result is still loadable Python, since `patch` is happy to
# produce a file that no longer parses.
RUN set -eux; \
    pkgdir="$(ls -d /venv/lib/python3.*/site-packages)"; \
    patch -p1 --fuzz=0 -d "$pkgdir" < /tmp/oidc-new-user-role.patch; \
    rm /tmp/oidc-new-user-role.patch; \
    python3 -c "from gramps_webapi.auth.oidc import _new_user_role"; \
    grep -q "OIDC_NEW_USER_ROLE_EMAILS_FILE" "$pkgdir/gramps_webapi/auth/oidc.py"
