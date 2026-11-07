# Forgejo Actions runner for git.realliance.net, host mode (no Docker, no Nix).
#
# Built HERE, by GitHub Actions, because the runner has to exist before the forge
# can build anything (spec "Bootstrap"). Toolchain for the forge CI jobs: Go, ko,
# Node (JS actions), git, and gcc + libc (cgo / go test -race).
#
# Every FROM is tag@sha256. Renovate bumps the FROM lines; the Deployment pin
# (sha-<commit>@sha256) is a second, manual re-pin (CLAUDE.md sharp edge 6).

FROM data.forgejo.org/forgejo/runner:13.2.1@sha256:568cea8c15ddc01fc8f330124892f572af7db37fa28344a2c1914405793bf61d AS runner
FROM golang:1.26.9-trixie@sha256:f06b7122f888680048afb247c4e5f49c0069eb6c4c1ccae0f3f4834527a8d22c AS go
FROM node:26.13.0-trixie-slim@sha256:5adad64f9a1fca2d51972caa65983634c8a774eb300d72b20f7f2a0f60368072 AS node
FROM ghcr.io/ko-build/ko:v0.19.1@sha256:504badc509e58f193a6b9f5527ae8885f8d61b38187b2d9580e15c34d027a082 AS ko

FROM debian:trixie-20261005-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f

# libstdc++6: the Node binary links against it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git gcc libc6-dev libstdc++6 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -u 1000 -m runner

COPY --from=runner /bin/forgejo-runner /usr/local/bin/forgejo-runner
COPY --from=go /usr/local/go /usr/local/go
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=ko /ko-app/ko /usr/local/bin/ko

ENV PATH=/usr/local/go/bin:$PATH
USER 1000:1000
WORKDIR /home/runner
ENTRYPOINT ["forgejo-runner"]
