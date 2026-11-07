# Slim general runner class, host mode: no Go, ko, gcc, Docker or Nix.
#
# Seed of the `tiny` class in docs/superpowers/specs/2026-11-04-runner-classes-design.md;
# first user is realliance/realliance.net (docs/superpowers/plans/2026-11-04-realliance-net-on-forge.md).
# Toolchain: Node (JS actions, netpol-gate), git, curl, unzip, tar, coreutils.
# No jq or python: use node for JSON. forgejo-runner 13.2.1 keeps one-job and --handle.
#
# Every FROM is tag@sha256. Renovate bumps the FROM lines; the Deployment pin
# (sha-<commit>@sha256) is a second, manual re-pin (CLAUDE.md sharp edge 6).

FROM data.forgejo.org/forgejo/runner:13.2.1@sha256:568cea8c15ddc01fc8f330124892f572af7db37fa28344a2c1914405793bf61d AS runner
FROM node:24.22.2-trixie-slim@sha256:0820eaa85e07d0490446dd01d14707e17e6108a36988cf374899cd057eb3812d AS node

FROM debian:trixie-20261005-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f

# libstdc++6: the Node binary links against it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git curl unzip libstdc++6 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -u 1000 -m runner

COPY --from=runner /bin/forgejo-runner /usr/local/bin/forgejo-runner
COPY --from=node /usr/local/bin/node /usr/local/bin/node

USER 1000:1000
WORKDIR /home/runner
ENTRYPOINT ["forgejo-runner"]
