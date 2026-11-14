# Forgejo Actions runner for the small/medium CI classes (one job per container, on ACI).
#
# Same five bases as images/forgejo-runner (a test keeps the FROM lines equal), but root,
# with make/curl/unzip, no `runner` user and a fixed entry script: the container is the
# isolation boundary (hypervisor-isolated container group), so there is nothing to drop to.
# No Docker, no Nix.
#
# Every FROM is tag@sha256. Renovate bumps the FROM lines; the consumer's pin
# (sha-<commit>@sha256) is a second, manual re-pin (CLAUDE.md sharp edge 6).

FROM data.forgejo.org/forgejo/runner:13.2.1@sha256:568cea8c15ddc01fc8f330124892f572af7db37fa28344a2c1914405793bf61d AS runner
FROM golang:1.27.4-trixie@sha256:b54b2639de8c1bfe538ec5ffc45f29b1932235ac5f6d63c059853646677112d9 AS go
FROM node:24.22.3-trixie-slim@sha256:af02d57b57719b9657a45199a79264a9437940cd55b3c7b3fc99615654f124a0 AS node
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
