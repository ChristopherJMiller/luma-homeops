#!/usr/bin/env bash
# Get forge repos back from the nightly `forgejo dump` in B2: no Forgejo, no cluster
# (docs/forgejo.md, "Getting one repo back without Forgejo").
#
#     repo-from-backup.sh <owner>/<repo> [dest]   that repo and its wiki, as working copies
#     repo-from-backup.sh --all [dest]            every bare repo, for when Forgejo won't come back
#     repo-from-backup.sh --list                  the repos in the dump
#     --dump forgejo-<UTC>.tar.gz                 an older dump than the newest
#
# Needs git, tar and `rclone`, not the `b2` command-line tool. The one credential is the forge
# backup key, from FORGE_BACKUP_KEY_ID and FORGE_BACKUP_KEY in the environment or, when unset,
# from realliance-sso's B2 OpenTofu outputs (nix and the sso-secrets checkout, as restore-drill.sh).
# It reaches `rclone` through env only. The dump streams through tar and never touches the disk;
# tar keeps only the repos/ paths asked for, never app.ini or the database.
set -euo pipefail
umask 077

BUCKET=realliance-forge-backups
FORGE=https://git.realliance.net
SSO_REPO=${SSO_REPO:-$HOME/Repos/realliance-sso}
SSO_SECRETS=${SSO_SECRETS:-$HOME/Repos/realliance-sso-secrets}
DUMP=

die() { printf 'repo-from-backup: %s\n' "$*" >&2; exit 1; }
log() { printf '%s %s\n' "$(date +%T)" "$*" >&2; }
usage() { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; }

work=$(mktemp -d "${TMPDIR:-/tmp}/repo-from-backup.XXXXXX")
trap 'rm -rf "$work"' EXIT

load_key() { # the forge backup key into rclone's env config; never argv, never output
  if [ -z "${FORGE_BACKUP_KEY_ID:-}" ] || [ -z "${FORGE_BACKUP_KEY:-}" ]; then
    { [ -f "$SSO_SECRETS/b2/secrets.env" ] && command -v nix >/dev/null; } ||
      die "export FORGE_BACKUP_KEY_ID and FORGE_BACKUP_KEY, or check out realliance-sso + sso-secrets and install nix"
    local keys
    # shellcheck source=/dev/null
    keys=$(cd "$SSO_REPO" && set -a && . "$SSO_SECRETS/b2/secrets.env" && set +a &&
      nix develop "$SSO_REPO" --command sh -c \
        'tofu -chdir=b2 output -raw forge_backup_key_id && echo && tofu -chdir=b2 output -raw forge_backup_key && echo' \
        2>/dev/null | tail -2) || keys=
    FORGE_BACKUP_KEY_ID=${keys%%$'\n'*} FORGE_BACKUP_KEY=${keys#*$'\n'}
    [ -n "$FORGE_BACKUP_KEY_ID" ] && [ -n "$FORGE_BACKUP_KEY" ] && [ "$keys" != "$FORGE_BACKUP_KEY" ] ||
      die "no forge backup key from realliance-sso's b2 outputs (run 'tofu -chdir=b2 init' there first?)"
  fi
  export RCLONE_CONFIG_FORGEBAK_TYPE=s3 RCLONE_CONFIG_FORGEBAK_PROVIDER=Other \
    RCLONE_CONFIG_FORGEBAK_REGION=us-east-005 RCLONE_CONFIG_FORGEBAK_NO_CHECK_BUCKET=true \
    RCLONE_CONFIG_FORGEBAK_ENDPOINT=https://s3.us-east-005.backblazeb2.com \
    RCLONE_CONFIG_FORGEBAK_ACCESS_KEY_ID="$FORGE_BACKUP_KEY_ID" \
    RCLONE_CONFIG_FORGEBAK_SECRET_ACCESS_KEY="$FORGE_BACKUP_KEY"
  unset FORGE_BACKUP_KEY
}

rcl() { rclone --config /dev/null "$@"; }
# tar on stdin, then drain it: a tar that stops at the end marker (bsdtar) must not fail rclone
untar() { local rc=0; tar "$@" || rc=$?; cat >/dev/null; return "$rc"; }

pick_dump() { # newest dump/ object unless --dump named one
  if [ -z "$DUMP" ]; then
    local names
    names=$(rcl lsf "forgebak:$BUCKET/dump/") || die "cannot list $BUCKET/dump/ with this key"
    DUMP=$(printf '%s\n' "$names" | grep -E '^forgejo-[0-9]{8}T[0-9]{6}Z\.tar\.gz$' | sort | tail -1) || true
    [ -n "$DUMP" ] || die "no dump/forgejo-*.tar.gz in $BUCKET"
  fi
  local size
  size=$(rcl lsf --format s "forgebak:$BUCKET/dump/$DUMP") && [ -n "$size" ] || die "no dump/$DUMP in $BUCKET"
  log "dump/$DUMP, $((size / 1048576)) MiB: repos as of that UTC time"
}

# Stream the dump through tar into <dir>, keeping only <member>s, minus their leading repos/.
# A member missing from the dump is fine here: the caller checks what arrived.
extract() { # <dir> <member>…
  local dir=$1 rcs; shift
  set +e
  rcl cat "forgebak:$BUCKET/dump/$DUMP" | untar -xzf - -C "$dir" --strip-components=1 "$@" 2>"$work/tar.err"
  rcs="${PIPESTATUS[*]}"
  set -e
  if [ "${rcs##* }" != 0 ]; then
    if [ ! -s "$work/tar.err" ] || grep -qv -e 'Not found in archive' -e 'previous errors' "$work/tar.err"; then
      cat "$work/tar.err" >&2; die "tar failed on dump/$DUMP (exit ${rcs##* })"
    fi
  fi
  [ "${rcs%% *}" = 0 ] || die "streaming dump/$DUMP failed (rclone exit ${rcs%% *})"
}

check_bare() { # <bare repo>: fsck, then its branches
  git -C "$1" fsck --full --no-dangling --no-progress >"$work/fsck.log" 2>&1 ||
    { cat "$work/fsck.log" >&2; die "git fsck failed: $1"; }
  log "fsck clean: ${1##*/}, branches:"
  git -C "$1" for-each-ref --format='  %(refname:short)  %(objectname:short)  %(committerdate:short)' refs/heads
}

cmd_repo() { # <owner>/<repo> [dest]
  local spec=$1 owner repo dest
  [[ $spec =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*$ && $spec != *..* ]] ||
    die "not an <owner>/<repo>: '$spec'"
  owner=$(printf '%s' "${spec%%/*}" | tr '[:upper:]' '[:lower:]') # Forgejo's on-disk names are lowercase
  repo=$(printf '%s' "${spec#*/}" | tr '[:upper:]' '[:lower:]')
  repo=${repo%.git}
  dest=${2:-$repo}
  [ ! -e "$dest" ] || die "$dest already exists"
  load_key; pick_dump
  mkdir "$work/repos"
  log "streaming it through tar for $owner/$repo.git and $owner/$repo.wiki.git"
  extract "$work/repos" "repos/$owner/$repo.git" "repos/$owner/$repo.wiki.git"
  [ -d "$work/repos/$owner/$repo.git" ] || die "$owner/$repo is not in dump/$DUMP (see --list)"
  local name to url
  for name in "$repo" "$repo.wiki"; do
    [ -d "$work/repos/$owner/$name.git" ] || continue
    check_bare "$work/repos/$owner/$name.git"
    to=$dest; [ "$name" = "$repo" ] || to=$dest.wiki
    url=$FORGE/${spec%%/*}/${spec#*/}; url=${url%.git}
    [ "$name" = "$repo" ] || url=$url.wiki
    # LFS stays as pointer files: the objects are in realliance-forge-objects, not the dump.
    GIT_LFS_SKIP_SMUDGE=1 git clone -q "$work/repos/$owner/$name.git" "$to"
    git -C "$to" remote set-url origin "$url.git"
    log "working copy: $to (origin/* = the forge as of the dump; origin is $url.git)"
    if git -C "$to" grep -q 'filter=lfs' HEAD -- '*.gitattributes' 2>/dev/null; then
      log "  uses LFS: fetch its objects as in docs/forgejo.md"
    fi
  done
}

cmd_all() { # [dest]
  local dest=${1:-forgejo-repos} n=0 d
  [ ! -e "$dest" ] || [ -z "$(ls -A "$dest")" ] || die "$dest is not empty"
  mkdir -p "$dest"
  load_key; pick_dump
  log "streaming every repo into $dest"
  extract "$dest" --exclude=repos/lost+found repos
  for d in "$dest"/*/*.git; do
    [ -d "$d" ] || continue
    git -C "$d" fsck --full --no-dangling --no-progress >/dev/null 2>&1 || die "git fsck failed: $d"
    n=$((n + 1))
  done
  [ "$n" -gt 0 ] || die "no repos in dump/$DUMP"
  log "$n bare repos in $dest, all fsck clean (wikis are <repo>.wiki.git)"
}

cmd_list() {
  load_key; pick_dump
  rcl cat "forgebak:$BUCKET/dump/$DUMP" | untar -tzf - | sed -nE 's#^repos/([^/]+/[^/]+)\.git(/.*)?$#\1#p' | sort -u
}

args=()
while [ $# -gt 0 ]; do
  case $1 in
    -h | --help) usage; exit 0 ;;
    --dump)
      [ $# -ge 2 ] || die "--dump needs forgejo-<UTC>.tar.gz"
      DUMP=${2#dump/}; shift 2
      [[ $DUMP =~ ^forgejo-[0-9]{8}T[0-9]{6}Z\.tar\.gz$ ]] || die "not a dump name: '$DUMP'" ;;
    *) args+=("$1"); shift ;;
  esac
done
[ ${#args[@]} -ge 1 ] || { usage >&2; exit 2; }
case ${args[0]} in
  --list) [ ${#args[@]} -eq 1 ] || die "--list takes no arguments"; cmd_list ;;
  --all) [ ${#args[@]} -le 2 ] || die "--all takes one dest"; cmd_all "${args[@]:1}" ;;
  -*) die "unknown option ${args[0]}" ;;
  *) [ ${#args[@]} -le 2 ] || die "too many arguments"; cmd_repo "${args[@]}" ;;
esac
