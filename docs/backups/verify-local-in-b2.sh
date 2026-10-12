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
# summary line. Exit 0 = nothing missing, 1 = something missing, 2 = usage.
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

# || true: an empty bucket means "everything missing", not an abort; a failing
# rclone still fails the script through pipefail.
for r in "${remotes[@]}"; do
  rclone lsf -R --files-only --hash MD5 --format h "$r"
done | { grep -E '^[0-9a-f]{32}$' || true; } | sort -u > "$work/remote"

# md5sum -z: NUL-terminated records, no backslash-escaping of odd filenames.
find "$@" -type f -size +0 ! -name Thumbs.db ! -name .DS_Store -print0 |
  xargs -0 -r md5sum -z > "$work/local"

awk 'FNR == NR { have[$0] = 1; next }
     { n++; h = substr($0, 1, 32); p = substr($0, 35)
       if (!(h in have)) { printf "%s\0", p } }
     END { printf "%d", n > "/dev/stderr" }' \
  "$work/remote" RS='\0' "$work/local" > "$work/missing" 2> "$work/count"

checked=$(cat "$work/count")
missing=0 bytes=0
while IFS= read -r -d '' p; do
  s=$(stat -c %s -- "$p")
  printf 'MISSING\t%s\t%s\n' "$s" "$p"
  missing=$((missing + 1)) bytes=$((bytes + s))
done < "$work/missing"

echo "summary: $checked files checked, $missing missing ($bytes bytes)"
[ "$missing" -eq 0 ]
