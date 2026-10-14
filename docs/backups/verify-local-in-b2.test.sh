#!/usr/bin/env bash
# Offline tests for verify-local-in-b2.sh: a fake `rclone` stands in for B2, so
# this runs anywhere, in seconds, with no credentials.
#
#   docs/backups/verify-local-in-b2.test.sh
#
# The cases that matter are the ones that must NOT report "nothing missing":
# an empty bucket, objects without an MD5, and an rclone that fails.
set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd)
verify="$here/verify-local-in-b2.sh"
t=$(mktemp -d)
trap 'rm -rf -- "$t"' EXIT

# Fake rclone: `rclone lsf ... <remote>` prints $t/remote-<remote with / and : as _>.txt;
# FAKE_RCLONE_RC makes it fail.
mkdir -p "$t/bin"
cat > "$t/bin/rclone" <<'EOF'
#!/usr/bin/env bash
[ "${FAKE_RCLONE_RC:-0}" -ne 0 ] && { echo "fake rclone: failing" >&2; exit "$FAKE_RCLONE_RC"; }
r=${!#}; f="$FAKE_DIR/remote-$(printf '%s' "$r" | tr '/:' '__').txt"
[ -f "$f" ] && cat "$f"
exit 0
EOF
chmod +x "$t/bin/rclone"
export PATH="$t/bin:$PATH" FAKE_DIR="$t"

# Local tree: 4 eligible files (one with a space and a '#'), plus things the
# verifier must skip: a zero-byte file and Thumbs.db.
src="$t/src"; mkdir -p "$src/sub dir"
printf 'alpha' > "$src/a.txt"
printf 'bravo' > "$src/b.txt"
printf 'charlie' > "$src/sub dir/c #1.jpg"
printf 'delta' > "$src/d.bin"
: > "$src/empty.txt"
printf 'junk' > "$src/Thumbs.db"
md5() { printf '%s' "$1" | md5sum | cut -c1-32; }

pass=0 fail=0
check() { # check <name> <expected exit> <expected summary regex> [extra grep]
  local name=$1 want_rc=$2 want_sum=$3 extra=${4:-} out rc
  out=$("$verify" fake:bucket/current -- "$src" 2>&1); rc=$?
  if [ "$rc" = "$want_rc" ] && grep -qE "$want_sum" <<<"$out" && { [ -z "$extra" ] || grep -qF -- "$extra" <<<"$out"; }; then
    echo "PASS $name"; pass=$((pass + 1))
  else
    echo "FAIL $name (exit $rc, want $want_rc)"; sed 's/^/     /' <<<"$out"; fail=$((fail + 1))
  fi
}

R="$t/remote-fake_bucket_current.txt"

: > "$R"
check "empty bucket: every file missing" 1 '^summary: 4 files checked, 4 missing'

printf '\n\n' > "$R"
check "objects without MD5: every file missing" 1 '^summary: 4 files checked, 4 missing'

{ md5 alpha; md5 bravo; md5 charlie; md5 delta; } > "$R"
check "all present: nothing missing" 0 '^summary: 4 files checked, 0 missing'

{ md5 alpha; md5 delta; } > "$R"
check "two missing, odd path printed intact" 1 '^summary: 4 files checked, 2 missing' "$src/sub dir/c #1.jpg"

{ md5 alpha; md5 bravo; md5 charlie; md5 delta; md5 junk; md5 ''; } > "$R"
check "zero-byte and Thumbs.db are not counted" 0 '^summary: 4 files checked, 0 missing'

FAKE_RCLONE_RC=1 check "failing rclone: never a green result" 2 'remote listing failed'

echo "== $pass passed, $fail failed"
[ "$fail" -eq 0 ]
