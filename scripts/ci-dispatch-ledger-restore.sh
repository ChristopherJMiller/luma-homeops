#!/usr/bin/env bash
# Put a nightly copy of ci-dispatch's ledger back as /data/ledger.db (docs/ci.md, Restoring the
# ledger). Run it with the dispatcher stopped: it refuses unless statefulset/ci-dispatch is at 0
# replicas and pod ci-dispatch-0 is gone, since the volume is RWO and the live process holds the
# ledger.
#
#   scripts/ci-dispatch-ledger-restore.sh <ledger-YYYYMMDDTHHMMSSZ.db> <restic snapshot id>
#   scripts/ci-dispatch-ledger-restore.sh --print <file> <id>   (the pod manifest only; no cluster)
#
# The snapshot id is required, never `latest`: read it with `restic snapshots --host <host>` and
# `restic ls <id> /data/snapshots`. The pod (ledger-restore, in ci-dispatch) runs
# scripts/ci-dispatch-ledger-restore-pod.sh, which moves nothing unless the replacement reads as
# SQLite. Pod Security `restricted`; the restic jobs' label, so their NetworkPolicy lets it reach
# B2; no podAffinity, since the dispatcher is stopped. It leaves the pod for you to read and delete.
#
# Tests: scripts/test_ci_dispatch_ledger_restore.py.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
pod_script=$here/ci-dispatch-ledger-restore-pod.sh
ns=ci-dispatch
image=ghcr.io/restic/restic:0.19.1@sha256:2f0373803493361f9304a57150d464677f69a9dad487afec202105aafb2592f2

print=0
if [ "${1:-}" = --print ]; then
  print=1
  shift
fi
if [ $# -ne 2 ]; then
  echo "usage: $0 [--print] <ledger-YYYYMMDDTHHMMSSZ.db> <restic snapshot id>" >&2
  exit 2
fi
snap=$1
rid=$2
if ! [[ $snap =~ ^ledger-[0-9]{8}T[0-9]{6}Z\.db$ ]]; then
  echo "$0: $snap is not a ledger snapshot name (ledger-YYYYMMDDTHHMMSSZ.db)" >&2
  exit 2
fi
if ! [[ $rid =~ ^[0-9a-f]{8,64}$ ]]; then
  echo "$0: $rid is not a restic snapshot id (8 to 64 hex digits; never latest)" >&2
  exit 2
fi

manifest() {
  cat <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ledger-restore
  namespace: $ns
  labels:
    app.kubernetes.io/name: restic-backup
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  enableServiceLinks: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 65532
    runAsGroup: 65532
    fsGroup: 65532
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: restore
      image: $image
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: [ALL]
      envFrom:
        - secretRef:
            name: restic-config
      env:
        - {name: SNAP, value: "$snap"}
        - {name: RID, value: "$rid"}
        - {name: HOME, value: /tmp}
      command: [/bin/sh, -c]
      args:
        - |
$(sed '/./s/^/          /' "$pod_script")
      volumeMounts:
        - {name: data, mountPath: /data}
        - {name: tmp, mountPath: /tmp}
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: ci-dispatch-ledger
    - name: tmp
      emptyDir:
        sizeLimit: 512Mi
EOF
}

if [ "$print" = 1 ]; then
  manifest
  exit 0
fi

replicas=$(kubectl -n "$ns" get statefulset ci-dispatch -o jsonpath='{.spec.replicas}')
if [ "$replicas" != 0 ]; then
  echo "$0: statefulset/ci-dispatch has ${replicas:-no} replicas; stop it first (docs/ci.md)" >&2
  exit 1
fi
for p in ci-dispatch-0 ledger-restore; do
  if [ -n "$(kubectl -n "$ns" get pod "$p" --ignore-not-found -o name)" ]; then
    echo "$0: pod $p exists; wait for ci-dispatch-0 to go, or read and delete an old ledger-restore" >&2
    exit 1
  fi
done
manifest | kubectl -n "$ns" apply -f -
echo "Watch kubectl -n $ns get pod ledger-restore until Completed or Error, read its log, then delete it."
