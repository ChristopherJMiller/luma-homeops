#!/usr/bin/env bash
# Runs a logical-backup dump.sh against a stubbed Kubernetes API, Postgres and
# S3, and checks which pod each case dumps from. The build runs it on the
# galaxy dump.sh; run it on upstream's to watch the dead-node cases fail:
#   nix shell nixpkgs#jq nixpkgs#pigz --command images/logical-backup/test/run.sh <dump.sh>
set -euo pipefail

script=${1:?usage: run.sh <dump.sh>}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin" "$work/pg/16/bin" "$work/sa"
echo token > "$work/sa/token"
touch "$work/sa/ca.crt"

# dump.sh reads the service account from a fixed path; point it at the stub.
sed "s|/var/run/secrets/kubernetes.io/serviceaccount|$work/sa|g" "$script" > "$work/dump.sh"
chmod +x "$work/dump.sh"

# The API: the backup pod itself runs on bottom; label and node selectors are
# applied to $FIXTURE the way the API server would.
cat > "$work/bin/curl" <<'EOF'
#!/usr/bin/env bash
url=$1
if [[ $url == *fieldSelector=metadata.name* ]]; then
  echo '{"items":[{"spec":{"nodeName":"bottom"}}]}'
  exit 0
fi
role=$(sed -n 's/.*spilo-role%3D\([a-z]*\).*/\1/p' <<<"$url")
node=$(sed -n 's/.*spec\.nodeName%3D\([a-z0-9-]*\).*/\1/p' <<<"$url")
jq --arg role "$role" --arg node "$node" \
  '{items: [.items[] | select(.metadata.labels["spilo-role"] == $role)
                     | select($node == "" or .spec.nodeName == $node)]}' "$FIXTURE"
EOF
cat > "$work/pg/16/bin/pg_dumpall" <<'EOF'
#!/usr/bin/env bash
echo "${PGHOST:-}" > "$CHOSEN"
echo "-- dump"
EOF
printf '#!/usr/bin/env bash\necho 1000\n' > "$work/pg/16/bin/psql"
printf '#!/usr/bin/env bash\ncat > /dev/null\n' > "$work/bin/aws"
chmod +x "$work/bin/"* "$work/pg/16/bin/"*

# pod NAME ROLE NODE IP READY DELETING
pod() {
  jq -n --arg n "$1" --arg r "$2" --arg node "$3" --arg ip "$4" --arg ready "$5" --arg del "$6" '{
    metadata: ({name: $n, labels: {"cluster-name": "acid-media", "spilo-role": $r}}
               + (if $del == "yes" then {deletionTimestamp: "2026-11-13T17:37:13Z"} else {} end)),
    spec: {nodeName: $node},
    status: {podIP: $ip, conditions: [{type: "Ready", status: $ready}]}}'
}

fail=0
# check NAME EXPECTED_IP POD...
check() {
  local name=$1 want=$2
  shift 2
  printf '%s\n' "$@" | jq -s '{items: .}' > "$work/pods.json"
  : > "$work/chosen"
  if ! env -i PATH="$work/bin:$PATH" HOME="$work" FIXTURE="$work/pods.json" CHOSEN="$work/chosen" \
      KUBERNETES_SERVICE_HOST=10.96.0.1 POD_NAMESPACE=media HOSTNAME=logical-backup-test \
      CLUSTER_NAME_LABEL=cluster-name SCOPE=acid-media PG_DIR="$work/pg" PG_VERSION=16 \
      LOGICAL_BACKUP_PROVIDER=s3 LOGICAL_BACKUP_S3_BUCKET=test LOGICAL_BACKUP_S3_BUCKET_SCOPE_SUFFIX= \
      LOGICAL_BACKUP_S3_ENDPOINT= LOGICAL_BACKUP_S3_REGION= LOGICAL_BACKUP_S3_SSE= \
      LOGICAL_BACKUP_S3_RETENTION_TIME= \
      "$work/dump.sh" > "$work/log" 2>&1; then
    echo "FAIL $name: dump.sh exited non-zero"; sed 's/^/    /' "$work/log"; fail=1; return
  fi
  local got
  got=$(cat "$work/chosen")
  if [ "$got" = "$want" ]; then echo "ok   $name -> $got"; else echo "FAIL $name: dumped from '$got', want '$want'"; fail=1; fi
}

master=$(pod acid-media-0 master bottom 10.244.0.55 True no)
dead_replica=$(pod acid-media-1 replica top 10.244.3.57 False yes)

# The 2026-11-13 outage: the replica is on a node with no network.
check "dead replica (evicted)" 10.244.0.55 "$master" "$dead_replica"
check "dead replica (first 5 min, not Ready yet evicted)" 10.244.0.55 \
  "$master" "$(pod acid-media-1 replica top 10.244.3.57 False no)"
check "replica being deleted but still Ready" 10.244.0.55 \
  "$master" "$(pod acid-media-1 replica middle 10.244.4.9 True yes)"
check "stale master listed first" 10.244.0.55 \
  "$(pod acid-media-1 master top 10.244.3.54 False yes)" "$master"
# Unchanged upstream behaviour.
check "healthy replica preferred" 10.244.4.9 \
  "$master" "$(pod acid-media-1 replica middle 10.244.4.9 True no)"
check "replica on the backup pod's node preferred" 10.244.0.77 \
  "$master" "$(pod acid-media-1 replica middle 10.244.4.9 True no)" \
  "$(pod acid-media-2 replica bottom 10.244.0.77 True no)"

exit $fail
