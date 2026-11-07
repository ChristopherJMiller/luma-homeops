# Forgejo Actions runner for git.realliance.net, host mode (no Docker, no Nix).
#
# Built HERE, by GitHub Actions, because the runner has to exist before the forge
# can build anything (spec "Bootstrap"). Toolchain for the forge CI jobs: Go, ko,
# Node (JS actions), git, and gcc + libc (cgo / go test -race).
#
# Every FROM is tag@sha256. Renovate bumps the FROM lines; the Deployment pin
# (sha-<commit>@sha256) is a second, manual re-pin (CLAUDE.md sharp edge 6).

FROM data.forgejo.org/forgejo/runner:13.2.1@sha256:568cea8c15ddc01fc8f330124892f572af7db37fa28344a2c1914405793bf61d AS runner
FROM golang:1.27.3-trixie@sha256:af1bfc2614ebb1dbd31af514b78740fd678850e76498762b425ecee4c5c6ea80 AS go
FROM node:24.22.2-trixie-slim@sha256:0820eaa85e07d0490446dd01d14707e17e6108a36988cf374899cd057eb3812d AS node
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
