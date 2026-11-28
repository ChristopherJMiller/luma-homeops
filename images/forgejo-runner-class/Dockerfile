# Forgejo Actions runner for the small/medium CI classes (one job per container, on ACI).
#
# Same five bases as images/forgejo-runner (a test keeps the FROM lines equal), but root,
# with make/curl/unzip, no `runner` user and a fixed entry script: the container is the
# isolation boundary (hypervisor-isolated container group), so there is nothing to drop to.
# No Docker, no Nix.
#
# Every FROM is tag@sha256. Renovate bumps the FROM lines; the consumer's pin
# (sha-<commit>@sha256) is a second, manual re-pin (CLAUDE.md sharp edge 6).

FROM data.forgejo.org/forgejo/runner:13.2.2@sha256:a8543a45fe62bebef0534d5c223cf485d3294e6dd2e42a82dbcbfd40c54a6a22 AS runner
FROM golang:1.27.4-trixie@sha256:430985542100b871e108ccc00e8dcc5eef08ca14ee5825e98ebf563d8ffec4ee AS go
FROM node:24.22.3-trixie-slim@sha256:4d00acb4bcc42c454c4eb801526f7ad7f772ea3bd4fd168c5cc2e384abcbb9d4 AS node
FROM ghcr.io/ko-build/ko:v0.19.1@sha256:504badc509e58f193a6b9f5527ae8885f8d61b38187b2d9580e15c34d027a082 AS ko

FROM debian:trixie-20261005-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f

# libstdc++6: the Node binary links against it. coreutils (timeout) is in the base.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git gcc libc6-dev libstdc++6 make curl unzip \
 && rm -rf /var/lib/apt/lists/*

COPY --from=runner /bin/forgejo-runner /usr/local/bin/forgejo-runner
COPY --from=go /usr/local/go /usr/local/go
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=ko /ko-app/ko /usr/local/bin/ko
COPY runner-small.yml runner-medium.yml /etc/ci-class/
COPY --chmod=0755 ci-class-entry /usr/local/bin/ci-class-entry

ENV PATH=/usr/local/go/bin:$PATH
WORKDIR /work
# <timeout> <class> <handle> <url> <uuid>; RUNNER_TOKEN comes from the environment.
ENTRYPOINT ["/usr/local/bin/ci-class-entry"]
