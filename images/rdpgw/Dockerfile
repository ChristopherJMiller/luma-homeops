# rdpgw for kvm-rdp (images/rdpgw), built from one pinned upstream commit.
#
# Why our own build: no rdpgw release carries the fixes kvm-rdp needs, and
# upstream's :master image was a fresh clone plus `go mod tidy` at build time,
# so its digest named a build, not a commit. Spec:
# docs/superpowers/specs/2026-10-07-kvm-rdp-deployment-design.md §4.6.
#
# Pin: master 80612a9 (2026-11-06), not 16cdaaf, the commit kvm-rdp's census
# Leg C ran against (kvm-rdp docs/census.md; kvm-rdp spec §9.4), because:
# - go-jose v4 4.1.6 (7ba84de): at 4.1.4, `jwt.ParseSigned` validates an RSA
#   private key embedded in a token header before any signature check, at cubic
#   cost (GHSA-mgcc-cww5-fp2q, high). rdpgw parses the PAA token the client
#   sends on /remoteDesktopGateway/, which no auth gates, so 16cdaaf lets anyone
#   burn rdpgw's CPU.
# - ed58cb1: rdpgw closes its connection to the bridge when the channel ends.
#   Windows App on macOS opens a short-lived channel before the real one, and
#   at 16cdaaf each connect left a dangling connection on the bridge.
# The only rdpgw code change from 16cdaaf is ed58cb1; the rest is dependency
# bumps and upstream's own go.sum. Leg C's checks still re-run at stage 3.
# To bump: change RDPGW_COMMIT, replace go.sum with upstream's at the new
# commit, re-pin the Deployment, re-run stage 3.
#
# go.sum: upstream's own, committed at 80612a9, copied here byte for byte, and
# the build requires them to be equal. go.mod is upstream's, unchanged, so
# Renovate's gomod manager has nothing to bump here: dependency fixes arrive
# with a pin bump. go-jose v3 3.0.5, for which the go-jose advisories list no
# fix, is linked only through go-oidc, which runs only in openid mode;
# rdpgw-mac uses header mode.
#
# Builder: golang 1.26.9-trixie, the forgejo-runner pin (pushed 2026-10-10, past
# the 2-week rule; 1.26.10 and 1.26.11 are too new). Not alpine: the fetch
# needs git. Runtime: distroless static nonroot, the pages-relay pin.
FROM golang:1.26.9-trixie@sha256:f06b7122f888680048afb247c4e5f49c0069eb6c4c1ccae0f3f4834527a8d22c AS build

ENV RDPGW_COMMIT=80612a977d05d9cb4d2c339b8f1f306a3593195f \
    CGO_ENABLED=0 GOTOOLCHAIN=local GOFLAGS= GOWORK=off
WORKDIR /src

# 1. The pinned upstream source, by full SHA.
RUN set -eux; \
    git init -q .; \
    git remote add origin https://github.com/bolkedebruin/rdpgw; \
    git fetch -q --depth 1 origin "$RDPGW_COMMIT"; \
    git checkout -q FETCH_HEAD; \
    test "$(git rev-parse HEAD)" = "$RDPGW_COMMIT"

# 2. Our go.sum, which must equal upstream's. Every module is checked against
#    it, the build may not edit go.mod or go.sum (-mod=readonly), and the
#    binary must record the pinned commit with a clean tree. The else branch
#    serves a pin from before upstream tracked go.sum.
COPY go.sum /galaxy/go.sum
RUN set -eux; \
    if [ -e go.sum ]; then cmp go.sum /galaxy/go.sum; else cp /galaxy/go.sum go.sum; fi; \
    go mod download; \
    go mod verify; \
    go build -trimpath -mod=readonly -o /out/rdpgw ./cmd/rdpgw; \
    cmp go.sum /galaxy/go.sum; \
    go version -m /out/rdpgw | grep -E "^[[:space:]]+build[[:space:]]+vcs.revision=${RDPGW_COMMIT}\$"; \
    go version -m /out/rdpgw | grep -E '^[[:space:]]+build[[:space:]]+vcs.modified=false$'

# No templates: rdpgw falls back to an embedded page, and / is never routed.
FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
COPY --from=build /out/rdpgw /rdpgw
USER nonroot:nonroot
ENTRYPOINT ["/rdpgw"]
