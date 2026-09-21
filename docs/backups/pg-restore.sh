#!/usr/bin/env bash
# Restore a postgres-operator logical backup (pg_dumpall | gzip on B2) into a
# running postgresql cluster. This is the ONLY restore path for the Postgres
# tier — the operator does not restore logical backups itself.
#
#   docs/backups/pg-restore.sh <namespace> <source-cluster> [target-cluster] [dump-key]
#
#   namespace       where both clusters live (e.g. home-assistant)
#   source-cluster  whose dumps to read       (e.g. acid-ha)
#   target-cluster  where to load them        (default: acid-drill — see acid-drill.yaml)
#   dump-key        exact B2 key; default = newest under spilo/<source-cluster>/
#
# Needs: backblaze-b2 (authorized: `backblaze-b2 account authorize` with the
# galaxy-operator-laptop key or the master key), kubectl, gunzip. All in the
# repo's nix shell. Run from the repo root.
#
# What it does, in order:
#   1. pick + download the dump
#   2. wait for the target cluster to be Running
#   3. snapshot the target's operator-managed role passwords (its
#      *.credentials.postgresql.acid.zalan.do Secrets)
#   4. psql the dump in as postgres (ON_ERROR_STOP=0: "role already exists"
#      and friends are expected noise — the count is printed)
#   5. re-apply the passwords from step 3. pg_dumpall carries ALTER ROLE ...
#      PASSWORD for every role, which overwrites the target's passwords with
#      the source's hashes; without this step Patroni (postgres/standby) and
#      the apps (their user Secrets) can no longer log in.
#   6. print per-database table/row counts for target and source so you can
#      eyeball the drill.
#
# For a REAL restore of cluster X: scale its app to 0, delete the postgresql
# CR + its PVCs (S4), let Argo recreate it empty, then run this with
# target-cluster = X. See docs/backups.md → "Postgres".
set -euo pipefail

ns=${1:?namespace}
src=${2:?source cluster}
tgt=${3:-acid-drill}
key=${4:-}
bucket=galaxy-cluster-backups

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
tpsql() { kubectl -n "$ns" exec -i "${tgt}-0" -c postgres -- psql -U postgres -v ON_ERROR_STOP=0 -qAt "$@"; }
spsql() { kubectl -n "$ns" exec -i "${src}-0" -c postgres -- psql -U postgres -qAt "$@"; }

# 1. dump
say "locating dump for $src"
if [ -z "$key" ]; then
  key=$(backblaze-b2 ls --recursive --json "b2://$bucket/spilo/$src/" \
        | jq -r 'map(select(.fileName|endswith(".sql.gz"))) | sort_by(.uploadTimestamp) | last | .fileName')
  [ -n "$key" ] && [ "$key" != null ] || { echo "no dumps under spilo/$src/ in $bucket" >&2; exit 1; }
fi
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
echo "dump: $key"
backblaze-b2 file download --no-progress "b2://$bucket/$key" "$tmp/dump.sql.gz" >/dev/null
ls -l "$tmp/dump.sql.gz" | awk '{print "size:", $5, "bytes"}'

# 2. target ready
say "waiting for postgresql/$tgt in $ns to be Running"
for _ in $(seq 1 60); do
  st=$(kubectl -n "$ns" get postgresql "$tgt" -o jsonpath='{.status.PostgresClusterStatus}' 2>/dev/null || true)
  [ "$st" = Running ] && kubectl -n "$ns" get pod "${tgt}-0" -o jsonpath='{.status.containerStatuses[?(@.name=="postgres")].ready}' 2>/dev/null | grep -q true && break
  sleep 5
done
[ "$st" = Running ] || { echo "target not Running (status: $st)" >&2; exit 1; }

# 3. remember the target's own passwords
say "capturing $tgt role passwords from operator Secrets"
declare -A pw
while IFS=$'\t' read -r u p; do pw["$u"]=$p; done < <(
  kubectl -n "$ns" get secret -o json \
    | jq -r --arg t ".$tgt.credentials.postgresql.acid.zalan.do" \
        '.items[] | select(.metadata.name|endswith($t)) | [(.data.username|@base64d), (.data.password|@base64d)] | @tsv')
echo "roles: ${!pw[*]}"

# 4. load
say "loading dump into $tgt (errors like 'already exists' are expected)"
errs=$(gunzip -c "$tmp/dump.sql.gz" | tpsql 2>&1 | grep -c '^ERROR' || true)
echo "psql ERROR lines: $errs"

# 5. put the passwords back
say "re-applying $tgt role passwords"
for u in "${!pw[@]}"; do
  printf 'ALTER ROLE "%s" PASSWORD %s;\n' "$u" "$(printf "%s" "${pw[$u]}" | sed "s/'/''/g; s/^/'/; s/\$/'/")" | tpsql >/dev/null \
    && echo "  $u: ok" || echo "  $u: FAILED" >&2
done

# 6. show your work
say "row counts (live tuple estimates) — target vs source"
counts() { # psql-fn dbname
  "$1" -d "$2" -c "select count(*)||' tables, '||coalesce(sum(n_live_tup),0)||' rows' from pg_stat_user_tables"
}
for db in $(tpsql -c "select datname from pg_database where datallowconn and datname not in ('postgres','template0','template1')"); do
  printf '  %-24s target: %-28s' "$db" "$(counts tpsql "$db" 2>/dev/null || echo '?')"
  if kubectl -n "$ns" get pod "${src}-0" >/dev/null 2>&1; then
    printf 'source: %s' "$(counts spsql "$db" 2>/dev/null || echo '?')"
  fi
  echo
done
say "done. ANALYZE has not run on the target; estimates settle after a few minutes."
