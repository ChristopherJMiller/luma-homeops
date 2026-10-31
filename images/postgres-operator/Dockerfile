# postgres-operator v1.11.0 + galaxy's Patroni REST API auth patches.
#
# Why: Patroni's REST API (:8008) accepts unauthenticated writes from any pod.
# Turning auth on needs the operator to (a) send Basic auth on its own writes
# (switchover, restart, PATCH /config) — patches/0001, credentials from the
# Secret `patroni-restapi` in each cluster's namespace — and (b) inject
# restapi.authentication into SPILO_CONFIGURATION for a CR that opts in via
# spec.env — patches/0002. Spilo stays stock: this is decision D4 ("patch the
# operator only") in docs/superpowers/plans/2026-10-19-patroni-rest-auth.md.
# Runbook: docs/postgres.md.
#
# Bump the upstream ref (step 1) and the runtime base (runtime-base) together,
# and port both patches to the new source; step 1 fails the build when the
# runtime base is not the source's own release. Never deploy an operator without
# them while any CR has the PATRONI_RESTAPI_* env block: upstream compares env
# by name and ignores restapi, so a revert rolls nothing, but every operator
# write then fails authentication and the next unrelated roll stalls at its
# switchover.
#
# Build gate: golden/galaxy_golden_test.go writes the StatefulSets of six CR
# shapes that do not opt in from the UNPATCHED source, then compares the
# patched output byte for byte. A patch that would change a non-opted
# StatefulSet (and roll that cluster on deploy) fails the build.
#
# Go: newest supported release. Unpatched v1.11.0 passes `go vet ./pkg/...`
# and `go test ./pkg/...` with Go 1.26.8 (2026-10-22), so 1.26.8 builds it
# instead of upstream's 1.21.7. go.mod and go.sum stay exactly as upstream.

# Upstream's v1.11.0 image, the one this replaces: the final stage keeps its
# alpine base, curl, ca-certificates, USER 1000:1000 and ENTRYPOINT
# /postgres-operator, and only swaps the binary.
FROM registry.opensource.zalan.do/acid/postgres-operator:v1.11.0@sha256:20ba5dbb3f2d3566eddb4cd0616379cc73e55bacce398301e577d4f6353ad4e9 AS runtime-base

FROM golang:1.27.3-bookworm@sha256:d9b6a1beb9474adc2a1ce842573639b44b2a0dd49b6669f51d3821baf9ec37c9 AS build

ENV CGO_ENABLED=0 GOTOOLCHAIN=local GOFLAGS=
WORKDIR /src

# 1. The pinned upstream source: v1.11.0.
RUN set -eux; \
    git init -q .; \
    git remote add origin https://github.com/zalando/postgres-operator; \
    git fetch -q --depth 1 origin 0f96eb20bfcc7381dacf0ec673e7d0824f442570; \
    git checkout -q FETCH_HEAD; \
    test "$(git rev-parse HEAD)" = 0f96eb20bfcc7381dacf0ec673e7d0824f442570
#    The runtime base must be the source's own release, so a base-only bump
#    fails here: the version its binary was built with (-X=main.version, from
#    its build info) must equal the image tag the source's own deployment
#    manifest pins.
COPY --from=runtime-base /postgres-operator /tmp/runtime-base-operator
RUN set -eux; \
    src="$(sed -n 's|^ *image: registry.opensource.zalan.do/acid/postgres-operator:\(v[0-9][0-9.]*\)$|\1|p' manifests/postgres-operator.yaml)"; \
    base="$(go version -m /tmp/runtime-base-operator | sed -n 's|^[[:space:]]*build[[:space:]]*-ldflags=-X=main.version=\(v[0-9][0-9.]*\)$|\1|p')"; \
    echo "source version: ${src:-none}; runtime base version: ${base:-none}"; \
    test -n "$src"; \
    test "$base" = "$src"; \
    echo "$src" > /source-version; \
    rm /tmp/runtime-base-operator

# 2. Upstream CI's test prerequisites (mocks need mockgen v1.6.0 and vendor/),
#    without `make deps`, which would rewrite go.mod/go.sum. Then the golden
#    StatefulSets, from the UNPATCHED source.
RUN set -eux; \
    go install github.com/golang/mock/mockgen@v1.6.0; \
    go mod vendor; \
    go generate ./...; \
    git diff --exit-code go.mod go.sum
COPY golden/galaxy_golden_test.go pkg/cluster/
RUN set -eux; \
    GALAXY_GOLDEN_DIR=/golden go test -count=1 -v ./pkg/cluster/ -run TestGalaxyGoldenStatefulSets -args -galaxy.update; \
    test "$(find /golden -name '*.json' | wc -l)" -eq 6

# 3. The patches.
COPY patches/ /patches/
RUN set -eux; \
    git apply --check /patches/*.patch; \
    git apply /patches/*.patch

# 4. Patched tests, including the golden comparison.
RUN set -eux; \
    GALAXY_GOLDEN_DIR=/golden go test -count=1 ./pkg/util/patroni/ ./pkg/cluster/; \
    GALAXY_GOLDEN_DIR=/golden go test -count=1 -v ./pkg/cluster/ -run TestGalaxyGoldenStatefulSets

# 5. The binary, built like upstream's docker/build_operator.sh. Its version
#    must be <source version>-galaxy.N.
RUN set -eux; \
    version=v1.11.0-galaxy.1; \
    case "$version" in "$(cat /source-version)-galaxy."*) ;; *) echo "version $version is not $(cat /source-version)-galaxy.N" >&2; exit 1 ;; esac; \
    CGO_ENABLED=0 go build -o /out/postgres-operator -ldflags "-X=main.version=$version" cmd/main.go

FROM runtime-base
COPY --from=build /out/postgres-operator /postgres-operator
