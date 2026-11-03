#!/usr/bin/env bash
# Restore drill for the Realliance forge (spec Success 9; docs/forgejo.md "Backups and restore").
#
#   docs/forgejo/restore-drill.sh fixture         # once: realliance/restore-drill-fixture with an LFS object
#   docs/forgejo/restore-drill.sh up [--fresh|dump-key]  # forgejo-drill from the newest (or given) dump/
#                                                 # object; --fresh first runs both backup jobs now
#   docs/forgejo/restore-drill.sh check           # drill vs production: repo heads, LFS from B2, SSH host key
#   docs/forgejo/restore-drill.sh down            # delete forgejo-drill and its volumes (asks first)
#
# The restore uses only what a new host would have: the nightly `forgejo dump` in the Realliance
# backups bucket, the read-only objects key, and this chart. It runs from an operator laptop with
# the unlocked realliance/sso-secrets checkout and the galaxy kubeconfig, inside `nix develop`.
# Secrets travel in env and files under a private temp dir, never in argv or output.
set -euo pipefail

NS=forgejo-drill
HERE=$(cd "$(dirname "$0")" && pwd)
SSO_REPO=${SSO_REPO:-$HOME/Repos/realliance-sso}
SSO_SECRETS=${SSO_SECRETS:-$HOME/Repos/realliance-sso-secrets}
CHART=oci://code.forgejo.org/forgejo-helm/forgejo
CHART_VERSION=17.3.2
FORGE=https://git.realliance.net
FIXTURE=realliance/restore-drill-fixture
: "${KUBECONFIG:=/tmp/galaxy-kubeconfig}"; export KUBECONFIG

work=$(mktemp -d); chmod 700 "$work"; trap 'rm -rf "$work"' EXIT
log() { printf '%s %s\n' "$(date +%T)" "$*" >&2; }

admin_token() { grep '^FORGEJO_ADMIN_TOKEN=' "$SSO_SECRETS/forge/secrets.env" | cut -d= -f2-; }

b2_outputs() { # realliance/sso's pinned OpenTofu reads the B2 layer's outputs (keys stay in memory)
  (cd "$SSO_REPO" && set -a && . "$SSO_SECRETS/b2/secrets.env" && set +a &&
    nix develop "$SSO_REPO" --command tofu -chdir=b2 output -json 2>/dev/null)
}

# git over HTTPS with a token, without the token in argv: GIT_ASKPASS reads it from env.
git_with_token() { # <token> <git args…>
  local token=$1; shift
  printf '#!/bin/sh\ncase "$1" in Username*) echo forgejo-admin;; *) echo "$DRILL_TOKEN";; esac\n' > "$work/askpass"
  chmod 700 "$work/askpass"
  DRILL_TOKEN=$token GIT_ASKPASS="$work/askpass" GIT_TERMINAL_PROMPT=0 git "$@"
}

cmd_fixture() {
  local token; token=$(admin_token)
  printf 'Authorization: token %s\n' "$token" > "$work/auth" # curl -H @file: the token stays out of argv
  if curl -fsS -o /dev/null -H @"$work/auth" "$FORGE/api/v1/repos/$FIXTURE" 2>/dev/null; then
    log "$FIXTURE already exists"; return
  fi
  curl -fsS -o /dev/null -X POST -H @"$work/auth" -H 'Content-Type: application/json' \
    -d '{"name":"restore-drill-fixture","private":true,"default_branch":"main","description":"Restore-drill fixture: one LFS object (docs/forgejo/restore-drill.sh in luma-homeops)"}' \
    "$FORGE/api/v1/orgs/realliance/repos"
  local w="$work/fixture"; mkdir "$w"
  git -C "$w" init -q -b main && git -C "$w" lfs install --local >/dev/null && git -C "$w" lfs track '*.bin' >/dev/null
  head -c $((1024 * 1024)) /dev/urandom > "$w/object.bin"
  printf 'Restore-drill fixture. object.bin is an LFS object in realliance-forge-objects.\n' > "$w/README.md"
  git -C "$w" add .gitattributes object.bin README.md
  git -C "$w" -c user.name='Realliance forge drill' -c user.email=git@realliance.net -c commit.gpgsign=false commit -qm 'fixture: one LFS object'
  git_with_token "$token" -C "$w" push -q "$FORGE/$FIXTURE.git" main
  log "created $FIXTURE"
}

run_backup_job() { # <cronjob>: run it now and wait (prod namespace forgejo)
  local job="drill-$1-$(date +%s)"
  kubectl -n forgejo create job --from="cronjob/$1" "$job" >/dev/null
  kubectl -n forgejo wait --for=condition=complete "job/$job" --timeout=1800s >/dev/null
  log "backup job $job complete"
}

cmd_up() {
  local key=${1:-}
  if [ "$key" = --fresh ]; then
    key=""
    run_backup_job logical-backup-acid-forgejo
    run_backup_job forgejo-dump
  fi
  local out; out=$(b2_outputs)
  # Download the dump with the backup key (it can read the backups bucket, never delete).
  export RCLONE_CONFIG_B_TYPE=s3 RCLONE_CONFIG_B_PROVIDER=Other RCLONE_CONFIG_B_REGION=us-east-005 \
    RCLONE_CONFIG_B_ENDPOINT=https://s3.us-east-005.backblazeb2.com RCLONE_CONFIG_B_NO_CHECK_BUCKET=true
  RCLONE_CONFIG_B_ACCESS_KEY_ID=$(jq -r .forge_backup_key_id.value <<<"$out"); export RCLONE_CONFIG_B_ACCESS_KEY_ID
  RCLONE_CONFIG_B_SECRET_ACCESS_KEY=$(jq -r .forge_backup_key.value <<<"$out"); export RCLONE_CONFIG_B_SECRET_ACCESS_KEY
  if [ -z "$key" ]; then
    key=dump/$(rclone --config /dev/null lsf b:realliance-forge-backups/dump/ | grep -E '^forgejo-.*\.tar\.gz$' | sort | tail -1)
  fi
  log "restoring from $key"
  rclone --config /dev/null copyto "b:realliance-forge-backups/$key" "$work/dump.tar.gz"
  mkdir "$work/d" && tar -C "$work/d" -xzf "$work/dump.tar.gz"
  [ -d "$work/d/repos" ] && [ -f "$work/d/app.ini" ] || { log "dump is missing repos/ or app.ini"; exit 1; }

  kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl label namespace "$NS" pod-security.kubernetes.io/enforce=baseline --overwrite >/dev/null
  # [storage] with the READ-ONLY objects key: the drill can serve LFS from the live bucket, never write it.
  {
    printf 'MINIO_ACCESS_KEY_ID=%s\n' "$(jq -r .forge_objects_ro_key_id.value <<<"$out")"
    printf 'MINIO_SECRET_ACCESS_KEY=%s\n' "$(jq -r .forge_objects_ro_key.value <<<"$out")"
  } > "$work/storage"
  kubectl -n "$NS" create secret generic forgejo-drill-ini --from-file=storage="$work/storage" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n "$NS" apply -f "$HERE/drill-resources.yaml" >/dev/null
  # A re-run reloads the volumes: the drill's Forgejo must let go of them first (RWO).
  if kubectl -n "$NS" get deploy forgejo >/dev/null 2>&1; then
    kubectl -n "$NS" scale deploy/forgejo --replicas=0 >/dev/null
    kubectl -n "$NS" wait --for=delete pod -l app.kubernetes.io/name=forgejo --timeout=300s >/dev/null 2>&1 || true
  fi

  log "waiting for acid-forgejo-drill"
  for _ in $(seq 1 60); do
    [ "$(kubectl -n "$NS" get postgresql acid-forgejo-drill -o jsonpath='{.status.PostgresClusterStatus}' 2>/dev/null)" = Running ] && break
    sleep 10
  done
  kubectl -n "$NS" wait --for=condition=Ready pod/acid-forgejo-drill-0 --timeout=600s >/dev/null

  # The database comes from the operator's pg_dumpall (spilo/), the repo's one Postgres restore path
  # (docs/backups/pg-restore.sh): `forgejo dump`'s own SQL creates tables in an order that ignores
  # Forgejo 15's foreign keys and cannot be loaded as-is.
  local pgkey
  pgkey=$(rclone --config /dev/null lsf -R --files-only b:realliance-forge-backups/spilo/acid-forgejo/ |
    grep '\.sql\.gz$' | awk -F/ '{print $NF"\t"$0}' | sort -n | tail -1 | cut -f2)
  [ -n "$pgkey" ] || { log "no pg_dumpall of acid-forgejo in the backups bucket"; exit 1; }
  log "database from spilo/acid-forgejo/$pgkey"
  rclone --config /dev/null copyto "b:realliance-forge-backups/spilo/acid-forgejo/$pgkey" "$work/db.sql.gz"
  dpsql() { kubectl -n "$NS" exec -i acid-forgejo-drill-0 -c postgres -- psql -U postgres -v ON_ERROR_STOP=0 -qAt "$@"; }
  # the drill's own role passwords: pg_dumpall's ALTER ROLE … PASSWORD would overwrite them
  kubectl -n "$NS" get secret -o json | jq -r '.items[] | select(.metadata.name|endswith(".acid-forgejo-drill.credentials.postgresql.acid.zalan.do"))
    | [(.data.username|@base64d), (.data.password|@base64d)] | @tsv' > "$work/roles.tsv"
  printf 'DROP DATABASE IF EXISTS forgejo WITH (FORCE);\nCREATE DATABASE forgejo OWNER forgejo;\n' | dpsql >/dev/null
  gunzip -c "$work/db.sql.gz" | dpsql 2>&1 | grep '^ERROR' > "$work/db-errors.log" || true
  log "pg_dumpall loaded ($(wc -l < "$work/db-errors.log") ERROR lines, 'already exists' expected): $(
    sed -E 's/"[^"]+"/"…"/g' "$work/db-errors.log" | sort | uniq -c | sort -rn | head -3 | tr -s ' ' | tr '\n' ';')"
  while IFS=$'\t' read -r u pw; do
    printf 'ALTER ROLE "%s" PASSWORD %s;\n' "$u" "$(printf '%s' "$pw" | sed "s/'/''/g; s/^/'/; s/\$/'/")" | dpsql >/dev/null
  done < "$work/roles.tsv"
  local tables; tables=$(dpsql -d forgejo -c "select count(*) from pg_tables where schemaname='public'")
  [ "${tables:-0}" -gt 50 ] || { log "only ${tables:-0} tables in forgejo after the load"; exit 1; }
  log "forgejo database: $tables tables"

  log "loading repos/ and data/ into the drill volumes"
  local image; image=$(kubectl -n forgejo get pods -l app.kubernetes.io/name=forgejo \
    -o jsonpath='{.items[0].status.containerStatuses[0].imageID}')
  kubectl -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata: {name: drill-loader}
spec:
  restartPolicy: Never
  securityContext: {runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000, runAsNonRoot: true, seccompProfile: {type: RuntimeDefault}}
  containers:
    - name: loader
      image: ${image#docker-pullable://}
      command: [sleep, "3600"]
      securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}
      volumeMounts: [{name: data, mountPath: /data}, {name: repos, mountPath: /repos}]
  volumes:
    - {name: data, persistentVolumeClaim: {claimName: forgejo-drill-data}}
    - {name: repos, persistentVolumeClaim: {claimName: forgejo-drill-repos}}
EOF
  kubectl -n "$NS" wait --for=condition=Ready pod/drill-loader --timeout=300s >/dev/null
  tar -C "$work/d/repos" -cf - . | kubectl -n "$NS" exec -i drill-loader -- tar -C /repos -xf -
  [ -d "$work/d/data" ] && tar -C "$work/d/data" -cf - . | kubectl -n "$NS" exec -i drill-loader -- tar -C /data -xf -
  kubectl -n "$NS" exec -i drill-loader -- sh -c 'mkdir -p /data/gitea/conf && cat > /data/gitea/conf/app.ini' < "$work/d/app.ini"
  kubectl -n "$NS" delete pod drill-loader --wait=true >/dev/null

  log "installing Forgejo $CHART_VERSION"
  helm template forgejo "$CHART" --version "$CHART_VERSION" -n "$NS" -f "$HERE/drill-values.yaml" |
    kubectl -n "$NS" apply -f - >/dev/null
  kubectl -n "$NS" scale deploy/forgejo --replicas=1 >/dev/null
  kubectl -n "$NS" rollout status deploy/forgejo --timeout=900s
  log "up: run '$0 check'"
}

cmd_check() {
  local token; token=$(admin_token)
  kubectl -n "$NS" port-forward svc/forgejo-http 13000:3000 >/dev/null 2>&1 & PF1=$!
  kubectl -n "$NS" port-forward svc/forgejo-ssh 12222:22 >/dev/null 2>&1 & PF2=$!
  trap 'kill "$PF1" "$PF2" 2>/dev/null; rm -rf "$work"' EXIT
  for _ in $(seq 1 30); do # both forwards answering (HTTP health, SSH banner)
    curl -fsS -o /dev/null http://localhost:13000/api/healthz 2>/dev/null &&
      timeout 3 bash -c 'exec 3<>/dev/tcp/localhost/12222 && head -c 4 <&3' 2>/dev/null | grep -q SSH && break
    sleep 2
  done
  local fail=0 repo prod drill
  for repo in realliance/forge "$FIXTURE"; do
    prod=$(git_with_token "$token" ls-remote "$FORGE/$repo.git" refs/heads/main | cut -f1) || prod=
    drill=$(git_with_token "$token" ls-remote "http://localhost:13000/$repo.git" refs/heads/main | cut -f1) || drill=
    if [ -n "$prod" ] && [ "$prod" = "$drill" ]; then log "ok   $repo main = ${prod:0:12}"; else log "FAIL $repo main: prod=${prod:0:12} drill=${drill:0:12}"; fail=1; fi
  done
  # LFS: clone the fixture from the drill; the object comes straight from B2 (read-only key).
  git_with_token "$token" clone -q "http://localhost:13000/$FIXTURE.git" "$work/clone" 2>/dev/null || true
  local oid sum
  oid=$(git -C "$work/clone" lfs ls-files -l 2>/dev/null | awk '{print $1}' | head -1) || oid=
  sum=$(sha256sum "$work/clone/object.bin" 2>/dev/null | cut -d' ' -f1) || sum=
  if [ -n "$oid" ] && [ "$oid" = "$sum" ]; then log "ok   LFS object.bin from B2 matches its oid"; else log "FAIL LFS object.bin (oid=${oid:0:12} got=${sum:0:12})"; fail=1; fi
  # SSH host key restored with data/ (no known_hosts churn after a real restore).
  local k1 k2
  k1=$(ssh-keyscan -T 10 -t rsa 192.168.0.6 2>/dev/null | awk '{print $3}') || k1=
  k2=$(ssh-keyscan -T 10 -t rsa -p 12222 localhost 2>/dev/null | awk '{print $3}') || k2=
  if [ -n "$k1" ] && [ "$k1" = "$k2" ]; then log "ok   SSH host key matches production"; else log "FAIL SSH host key differs"; fail=1; fi
  [ "$fail" -eq 0 ] && log "drill PASSED" || log "drill FAILED"
  return "$fail"
}

cmd_down() {
  read -r -p "Delete namespace $NS and its volumes (PVs are Retain; they are deleted too)? [y/N] " ok
  [ "$ok" = y ] || { log "left as is"; return; }
  local pvs; pvs=$(kubectl get pv -o json | jq -r --arg ns "$NS" '.items[] | select(.spec.claimRef.namespace==$ns) | .metadata.name')
  kubectl delete namespace "$NS" --wait=true
  for pv in $pvs; do kubectl delete pv "$pv"; done
}

case "${1:-}" in
  fixture) cmd_fixture ;;
  up) shift; cmd_up "${1:-}" ;;
  check) cmd_check ;;
  down) cmd_down ;;
  *) sed -n '2,8p' "$0"; exit 2 ;;
esac
