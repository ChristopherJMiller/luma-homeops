#!/usr/bin/env bash
# Is every local file already in B2? Matches by content (MD5), not path, so
# renamed folders and files that moved between buckets still count. Use it
# before deleting a local copy of anything that is supposed to be mirrored.
#
#   docs/backups/verify-local-in-b2.sh fam:galaxy-family/current arc:galaxy-archive/current \
#     -- ~/ForMediaServer ~/Videos/"Old Videos"
#
# Remotes are ordinary rclone remotes (rclone.conf, or RCLONE_CONFIG_<NAME>_*
# env as in docs/backups.md). B2 keys are scoped to one bucket, so use one
# remote per bucket — e.g. fam: with a galaxy-family key, arc: with a
# galaxy-archive key.
#
# Every object rclone wrote carries an MD5, multipart ones included; an object
# without one can only cause a false MISSING, never a false match.
# Skipped: zero-byte files (nothing to lose) and Thumbs.db/.DS_Store (every
# mirror excludes them).
#
# Prints MISSING<TAB>bytes<TAB>path per local file with no copy, then a
# summary line. Exit 0 = nothing missing, 1 = something missing, 2 = usage
# error, a failed remote listing, or an internal count mismatch. Tests:
# docs/backups/verify-local-in-b2.test.sh (offline, fake rclone).
set -euo pipefail

remotes=()
while [ $# -gt 0 ] && [ "$1" != "--" ]; do remotes+=("$1"); shift; done
[ "${1:-}" = "--" ] && shift
if [ ${#remotes[@]} -eq 0 ] || [ $# -eq 0 ]; then
  echo "usage: $0 <remote:path>... -- <local dir>..." >&2
  exit 2
fi

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

for r in "${remotes[@]}"; do
  if ! rclone lsf -R --files-only --hash MD5 --format h "$r" >> "$work/listing"; then
    echo "remote listing failed: $r" >&2
    exit 2
  fi
done
# An empty bucket, or objects without an MD5, leave this list empty: that means
# "everything missing", never an abort and never a pass.
{ grep -E '^[0-9a-f]{32}$' "$work/listing" || true; } | sort -u > "$work/remote"

# md5sum -z: NUL-terminated records, no backslash-escaping of odd filenames.
find "$@" -type f -size +0 ! -name Thumbs.db ! -name .DS_Store -print0 > "$work/files"
xargs -0 -r md5sum -z < "$work/files" > "$work/local"

# The remote list is loaded in BEGIN, not as a first input file: with the
# NR==FNR idiom an empty remote list swallows the local records and reports
# "0 checked, 0 missing" (found in review 2026-10-13).
awk -v remote="$work/remote" '
     BEGIN { while ((getline h < remote) > 0) have[h] = 1; RS = "\0" }
     { n++; h = substr($0, 1, 32); p = substr($0, 35)
       if (!(h in have)) { printf "%s\0", p } }
     END { printf "%d", n > "/dev/stderr" }' \
  "$work/local" > "$work/missing" 2> "$work/count"

checked=$(cat "$work/count")
expected=$(tr -cd '\0' < "$work/files" | wc -c)
if [ "$checked" != "$expected" ]; then
  echo "internal error: checked $checked files but found $expected — refusing to report" >&2
  exit 2
fi
missing=0 bytes=0
while IFS= read -r -d '' p; do
  s=$(stat -c %s -- "$p")
  printf 'MISSING\t%s\t%s\n' "$s" "$p"
  missing=$((missing + 1)) bytes=$((bytes + s))
done < "$work/missing"

echo "summary: $checked files checked, $missing missing ($bytes bytes)"
[ "$missing" -eq 0 ]
