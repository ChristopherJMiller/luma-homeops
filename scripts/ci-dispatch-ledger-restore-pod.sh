#!/bin/sh
# Runs inside the ledger-restore pod (the restic image, busybox sh), which
# scripts/ci-dispatch-ledger-restore.sh creates (docs/ci.md, Restoring the ledger).
#
# Puts the nightly copy $SNAP back as $DATA/ledger.db: from $DATA/snapshots when it's still there,
# else from restic snapshot $RID. Nothing moves until the replacement sits beside the ledger and
# starts with the SQLite header, so any failure (a misspelt name, an --include that matched nothing,
# which restic answers with exit 0) leaves the live ledger as it was. The old ledger.db and its
# -wal and -shm files go into before-restore-<time>/.
#
# Tests: scripts/test_ci_dispatch_ledger_restore.py (fake /data, stub restic).
set -eu
: "${SNAP:?SNAP: the snapshot file name}" "${RID:?RID: the restic snapshot id}"
DATA=${DATA:-/data}
WORK=${WORK:-/tmp/restore}

case $SNAP in
  ledger-*T*Z.db) ;;
  *)
    echo "restore: $SNAP is not a ledger snapshot name; nothing moved" >&2
    exit 2
    ;;
esac

cd "$DATA"
src=snapshots/$SNAP
if [ ! -s "$src" ]; then
  # The nightly job backs up the absolute path /data/snapshots, whatever $DATA is here.
  restic restore "$RID" --include "/data/snapshots/$SNAP" --target "$WORK" --no-cache --retry-lock 15m
  src=$WORK/data/snapshots/$SNAP
fi
if [ ! -s "$src" ]; then
  echo "restore: $SNAP is in neither $DATA/snapshots nor restic snapshot $RID; nothing moved" >&2
  exit 1
fi

rm -f ledger.db.part
cp "$src" ledger.db.part
magic=$(head -c 16 ledger.db.part | od -An -tx1 | tr -d ' \n')
if [ "$magic" != 53514c69746520666f726d6174203300 ]; then
  rm -f ledger.db.part
  echo "restore: $SNAP does not start with the SQLite header; nothing moved" >&2
  exit 1
fi

old=before-restore-$(date -u +%Y%m%dT%H%M%SZ)
mkdir "$old"
for x in ledger.db ledger.db-wal ledger.db-shm; do
  if [ -e "$x" ]; then
    mv "$x" "$old/"
  fi
done
mv ledger.db.part ledger.db
echo "restore: $SNAP is now ledger.db; the old files are in $old"
ls -l "$DATA" "$DATA/$old"
