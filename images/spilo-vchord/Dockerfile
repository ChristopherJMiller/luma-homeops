# Spilo 16 + VectorChord, for the Immich database only.
#
# Immich >= 3.0 requires the `vchord` extension; stock Spilo ships pgvector
# (0.7.0, which is inside VectorChord's supported >=0.7,<0.9 range) but not
# VectorChord. The published .deb drops vchord.so straight into
# /usr/lib/postgresql/16/lib, which is exactly Spilo's pkglibdir, so nothing
# needs building from source.
#
# This image is used by ONE cluster — `acid-immich` sets spec.dockerImage.
# Every other postgresql cluster stays on stock Spilo, so a bad build here
# cannot take down authentik-style shared infrastructure. Keeping Immich
# inside the operator is what earns it the nightly logical backup for free.
#
# When bumping the base tag, check VectorChord is still within the range
# Immich accepts (>=0.3,<2.0) — see docs/immich.md.
FROM ghcr.io/zalando/spilo-16:3.3-p3@sha256:0a59aa400727c0a6fac9635f17b09aece9a7ae5e58f0979b1bb0b7e30fb82665

# Checksum-pinned: ADD would otherwise silently accept a re-cut release asset.
ADD --checksum=sha256:0109f45cc84e91927f1d8782a2fbe3149a25f03ace7697970fd4860944e7199d \
    https://github.com/supervc-stack/VectorChord/releases/download/1.1.1/postgresql-16-vchord_1.1.1-1_amd64.deb \
    /tmp/vchord.deb

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends /tmp/vchord.deb; \
    rm -f /tmp/vchord.deb; \
    rm -rf /var/lib/apt/lists/*; \
    test -f /usr/lib/postgresql/16/lib/vchord.so

# No USER / ENTRYPOINT / CMD overrides: the base image leaves USER unset and
# the operator supplies spiloRunAsUser/Group; CMD stays /launch.sh init.
