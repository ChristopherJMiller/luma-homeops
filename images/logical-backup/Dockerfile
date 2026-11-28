# logical-backup v1.11.0 + one change to dump.sh: it dumps only from a pod
# that is Ready and not terminating.
#
# Why: dump.sh connects to the first pod labelled spilo-role=replica, else
# master. A pod on a lost node keeps that label and its podIP until the node
# returns, so through the 2026-11-13 outage every nightly dump of acid-immich,
# acid-media and acid-royaltracker timed out against a dead replica on top.
# Upstream still selects this way: #3069 (2026-04) only waits for the host it
# already chose, so upgrading would not fix it. Runbook: docs/backups.md.
#
# Lockstep: this image follows the operator's version (v1.11.0, see
# cluster/applications/postgres-operator.yaml). A base bump fails the checksum
# below until dump.sh is re-ported from the new upstream file.

FROM registry.opensource.zalan.do/acid/logical-backup:v1.15.0@sha256:94c73e8054ebdda9612afae32e153331f4d5054af079ce275a1b8210901d6bea AS upstream
RUN echo "f32ff06f6d3eaa81458cee3b6f1a65ac94a650d8d667ce67d02ae8e8141b5c5d  /dump.sh" | sha256sum -c -

# Build gate: the real dump.sh against a stubbed API, Postgres and S3, with the
# image's own jq. The final stage copies from here, so the gate always runs.
FROM upstream AS test
COPY --chmod=0755 dump.sh /galaxy/dump.sh
COPY --chmod=0755 test/run.sh /galaxy/test/run.sh
RUN /galaxy/test/run.sh /galaxy/dump.sh

FROM upstream
COPY --from=test --chmod=0755 /galaxy/dump.sh /dump.sh
