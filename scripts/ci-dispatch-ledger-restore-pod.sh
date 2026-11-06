#!/bin/sh
# Runs inside the ledger-restore pod (the restic image, busybox sh), which
# scripts/ci-dispatch-ledger-restore.sh creates (docs/ci.md, Restoring the ledger).
#
# Puts the nightly copy $SNAP back as $DATA/ledger.db: from $DATA/snapshots when it's still there,
# else from restic snapshot $RID. Nothing moves until the replacement sits beside the ledger, equals
# its source byte for byte and starts with the SQLite header, so any failure (a misspelt name, an
# --include that matched nothing, which restic answers with exit 0, a short copy) leaves the live
# ledger as it was. The old ledger.db and its -wal and -shm files go into before-restore-<time>/;
# if one of them can't move, or the final rename fails, every file already moved goes back. Every
# step checks its own exit status rather than trusting `set -e` alone.
#
# Tests: scripts/test_ci_dispatch_ledger_restore.py (fake /data, stub restic, cp, mv and date).
set -eu
: "${SNAP:?SNAP: the snapshot file name}" "${RID:?RID: the restic snapshot id}"
DATA=${DATA:-/data}
WORK=${WORK:-/tmp/restore}

fail() {
  rm -f ledger.db.part
  echo "restore: $*" >&2
  exit 1
}

# put_back moves every file in $moved back from $old, and names any it can't.
put_back() {
  for y in $moved; do
    mv "$old/$y" "$y" || echo "restore: $y is still in $old: move it back by hand" >&2
  done
}

case $SNAP in
  ledger-*T*Z.db) ;;
  *)
    echo "restore: $SNAP is not a ledger snapshot name; nothing moved" >&2
    exit 2
    ;;
esac

cd "$DATA" || exit 1
src=snapshots/$SNAP
if [ ! -s "$src" ]; then
  # The nightly job backs up the absolute path /data/snapshots, whatever $DATA is here.
  restic restore "$RID" --include "/data/snapshots/$SNAP" --target "$WORK" --no-cache --retry-lock 15m ||
    fail "restic restore of $RID failed; nothing moved"
  src=$WORK/data/snapshots/$SNAP
fi
if [ ! -s "$src" ]; then
  fail "$SNAP is in neither $DATA/snapshots nor restic snapshot $RID; nothing moved"
fi

rm -f ledger.db.part
cp "$src" ledger.db.part || fail "copying $SNAP failed; nothing moved"
cmp -s "$src" ledger.db.part || fail "the copy of $SNAP differs from its source; nothing moved"
magic=$(head -c 16 ledger.db.part | od -An -tx1 | tr -d ' \n') || fail "reading the copy failed; nothing moved"
if [ "$magic" != 53514c69746520666f726d6174203300 ]; then
  fail "$SNAP does not start with the SQLite header; nothing moved"
fi

old=before-restore-$(date -u +%Y%m%dT%H%M%SZ) || fail "no timestamp; nothing moved"
mkdir "$old" || fail "can't create $old; nothing moved"
moved=
for x in ledger.db ledger.db-wal ledger.db-shm; do
  if [ -e "$x" ]; then
    if ! mv "$x" "$old/"; then
      put_back
      fail "can't move $x aside; put back:${moved:- nothing}; nothing replaced"
    fi
    moved="$moved $x"
  fi
done
if ! mv ledger.db.part ledger.db; then
  put_back
  fail "can't rename ledger.db.part to ledger.db; put back:${moved:- nothing}; nothing replaced"
fi
echo "restore: $SNAP is now ledger.db; the old files are in $old"
ls -l "$DATA" "$DATA/$old"
