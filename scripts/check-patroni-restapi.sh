#!/usr/bin/env bash
# Audit the Patroni REST API auth state of every acid-* Postgres cluster.
#
#   scripts/check-patroni-restapi.sh [--probe] [--cluster NS/NAME]
#
# Output:
#   operator image=ok|no
#   NS/POD STATUS env=.. secret=.. sts=.. file=.. daemon=.. ctl=.. get=.. [unauth=<http> auth=<http>]
#
# STATUS is AUTH-ON, OPEN or BROKEN. Exit 0 only if the operator image is the
# pinned one and every member is AUTH-ON. Uses KUBECONFIG from the environment.
#
# Without --probe the verdict is structural (config, file, daemon, patronictl);
# only --probe confirms the behaviour (unauth=401, auth=404). auth=401 means
# the credentials were rejected while auth is on, reported as BROKEN.
#
# Read-only, with one exception: --probe sends a no-op DELETE /restart to each
# member (once without credentials, once with), and only when GET /patroni shows
# no scheduled_restart. Chris approved that 2026-10-21; no other write to any
# Patroni API, ever. Secret values are never printed: credentials are compared
# and used inside the pod and only True/False or HTTP codes come back.
set -uo pipefail

: "${KUBECONFIG:=/tmp/galaxy-kubeconfig}"; export KUBECONFIG

probe=0
only=""
while [ $# -gt 0 ]; do
  case "$1" in
    --probe) probe=1; shift ;;
    --cluster) only="${2:-}"; [ -n "$only" ] || { echo "--cluster needs NS/NAME" >&2; exit 2; }; shift 2 ;;
    *) echo "usage: $0 [--probe] [--cluster NS/NAME]" >&2; exit 2 ;;
  esac
done

repo=$(cd "$(dirname "$0")/.." && pwd)
opfile="$repo/cluster/applications/postgres-operator.yaml"
rc=0

# --- operator image ---------------------------------------------------------
# Pinned digest: the sha256 in the `tag:` line of the chart's `image:` block.
pinned=$(awk '
  /^[[:space:]]*image:[[:space:]]*$/ { match($0, /^ */); ind = RLENGTH; inblk = 1; next }
  inblk {
    if ($0 ~ /^[[:space:]]*$/) next
    match($0, /^ */)
    if (RLENGTH <= ind) { inblk = 0; next }
    if ($0 ~ /^[[:space:]]*tag:/) { print; exit }
  }' "$opfile" 2>/dev/null | grep -oE 'sha256:[0-9a-f]{64}' | head -1)
# Every Ready operator pod must run the pinned digest (and at least one must be Ready).
img_ok=no
if [ -n "$pinned" ]; then
  img_ok=$(timeout 30 kubectl -n postgres-operator get pod -l app.kubernetes.io/name=postgres-operator -o json 2>/dev/null |
    jq -r --arg d "$pinned" '[.items[] | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))
      | all(.status.containerStatuses[]?; (.imageID // "") | endswith("@" + $d))] | if length > 0 and all then "ok" else "no" end' 2>/dev/null)
fi
if [ "$img_ok" = ok ]; then
  echo "operator image=ok"
else
  echo "operator image=no"; rc=1
fi

# --- per-member script, run inside the postgres container --------------------
# Prints key=value lines; never prints credentials.
read -r -d '' podscript <<'EOS'
CFG=/home/postgres/postgres.yml
RUN=/run/postgres.yml

echo "file=$(python3 -c '
import os, yaml
a = (yaml.safe_load(open("/run/postgres.yml")) or {}).get("restapi", {}).get("authentication") or {}
u, p = str(a.get("username") or ""), str(a.get("password") or "")
eu, ep = os.environ.get("PATRONI_RESTAPI_USERNAME", ""), os.environ.get("PATRONI_RESTAPI_PASSWORD", "")
if "$(" in u or "$(" in p:
    print("unexpanded")
else:
    print(bool(u and p and eu and ep and u == eu and p == ep))
' 2>/dev/null)"

pid=$(cat /etc/service/patroni/supervise/pid 2>/dev/null)
[ -n "$pid" ] && [ -r "/proc/$pid/cmdline" ] || pid=$(pgrep -o -f '^/usr/bin/python3 /usr/local/bin/patroni')
daemon=False
if [ -n "$pid" ] && [ -r "/proc/$pid/cmdline" ]; then
  cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline")
  et=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')   # ps before date (+1 s slack)
  now=$(date +%s)
  mt=$(stat -c %Y "$RUN" 2>/dev/null)
  case "$cmd" in
    *"$CFG "|*"$CFG")
      if [ -n "$et" ] && [ -n "$mt" ] && [ "$et" -le $((now - mt + 1)) ]; then daemon=True; fi ;;
  esac
fi
echo "daemon=$daemon"

echo "ctl=$(python3 -c '
from patroni.config import Config
from patroni.request import PatroniRequest
r = PatroniRequest(Config("/home/postgres/postgres.yml", validator=None))
print(any(str(k).lower() == "authorization" for k in r._pool.headers))
' 2>/dev/null)"

get=$(curl --max-time 5 -s -o /dev/null -w '%{http_code}' http://localhost:8008/patroni)
echo "get=$get"

if [ "$PROBE" = 1 ]; then
  # Only send the no-op DELETE when GET /patroni answered 200 with a body that
  # shows no scheduled_restart.
  resp=$(curl --max-time 5 -s -w '\n%{http_code}' http://localhost:8008/patroni)
  pcode=$(printf '%s\n' "$resp" | tail -n1)
  body=$(printf '%s\n' "$resp" | sed '$d')
  case "$pcode:$body" in
    200:) echo "unauth=skip"; echo "auth=skip" ;;
    200:*scheduled_restart*) echo "unauth=skip"; echo "auth=skip" ;;
    200:*)
      echo "unauth=$(curl --max-time 5 -s -o /dev/null -w '%{http_code}' -X DELETE http://localhost:8008/restart)"
      echo "auth=$(printf 'user = "%s:%s"\n' "$PATRONI_RESTAPI_USERNAME" "$PATRONI_RESTAPI_PASSWORD" |
        curl --max-time 5 -s -K - -o /dev/null -w '%{http_code}' -X DELETE http://localhost:8008/restart)" ;;
    *) echo "unauth=skip"; echo "auth=skip" ;;
  esac
fi
EOS

yn() { if [ "$1" = "True" ]; then echo ok; else echo no; fi; }

# --- clusters ---------------------------------------------------------------
crs=$(timeout 30 kubectl get postgresql -A -o json 2>/dev/null) || { echo "cannot list postgresql CRs" >&2; exit 1; }
[ "$(jq '.items | length' <<<"$crs" 2>/dev/null)" -gt 0 ] 2>/dev/null || { echo "no postgresql CRs found" >&2; exit 1; }
audited=0; matched=0
# shellcheck disable=SC2016 # literal $( ) is the expected value
want_auth='{"password":"$(PATRONI_RESTAPI_PASSWORD)","username":"$(PATRONI_RESTAPI_USERNAME)"}'

while IFS=/ read -r ns name; do
  [ -n "$ns" ] || continue
  if [ -n "$only" ] && [ "$only" != "$ns/$name" ]; then continue; fi
  matched=1

  env_ok=$(jq -r --arg ns "$ns" --arg n "$name" '
    def ref($var; $key): (.spec.env // []) | any(.name == $var
      and (.valueFrom.secretKeyRef.name == "patroni-restapi")
      and (.valueFrom.secretKeyRef.key == $key)
      and ((.valueFrom.secretKeyRef.optional // false) == false));
    .items[] | select(.metadata.namespace == $ns and .metadata.name == $n)
    | (ref("PATRONI_RESTAPI_USERNAME"; "username") and ref("PATRONI_RESTAPI_PASSWORD"; "password"))' <<<"$crs")

  sec=$(kubectl -n "$ns" get secret patroni-restapi -o json 2>/dev/null)
  secret_ok=False
  if [ -n "$sec" ] && [ "$(jq -c '.data | keys' <<<"$sec")" = '["password","username"]' ] \
     && [ "$(jq -r '.data | map(length > 0) | all' <<<"$sec")" = true ]; then
    secret_ok=True
  fi

  sts_ok=False
  sts=$(kubectl -n "$ns" get statefulset "$name" -o json 2>/dev/null)
  if [ -n "$sts" ]; then
    sts_ok=$(jq -r --argjson want "$want_auth" '
      (.spec.template.spec.containers[] | select(.name == "postgres") | .env // []) as $e
      | ($e | map(.name)) as $names
      | ($names | index("SPILO_CONFIGURATION")) as $sc
      | ($names | index("PATRONI_RESTAPI_USERNAME")) as $u
      | ($names | index("PATRONI_RESTAPI_PASSWORD")) as $p
      | ($sc != null and $u != null and $p != null and $u < $sc and $p < $sc
         and ((($e[] | select(.name == "SPILO_CONFIGURATION") | .value | fromjson | .restapi.authentication) // null) == $want))
    ' <<<"$sts" 2>/dev/null || echo False)
    [ "$sts_ok" = true ] && sts_ok=True || sts_ok=False
  fi
  [ "$env_ok" = true ] && env_ok=True || env_ok=False

  pods=$(kubectl -n "$ns" get pods -l "galaxy=$name,application=spilo" -o name 2>/dev/null | sed 's|^pod/||')
  [ -n "$pods" ] || { echo "$ns/$name NO-MEMBERS"; rc=1; continue; }

  for pod in $pods; do
    audited=$((audited + 1))
    if ! out=$(printf 'PROBE=%s\n%s\n' "$probe" "$podscript" |
      timeout 30 kubectl -n "$ns" exec -i -c postgres "$pod" -- sh -s 2>/dev/null) || [ -z "$out" ]; then
      echo "$ns/$pod BROKEN exec-failed"; rc=1; continue
    fi
    get_kv() { sed -n "s/^$1=//p" <<<"$out" | head -1; }
    file=$(get_kv file); daemon=$(get_kv daemon); ctl=$(get_kv ctl); get=$(get_kv get)
    unauth=$(get_kv unauth); auth=$(get_kv auth)

    get_ok=False; [ "$get" = 200 ] && get_ok=True
    fileword=$(yn "$file"); [ "$file" = unexpanded ] && fileword=unexpanded

    status=OPEN
    if [ "$get_ok" = False ] || { [ "$env_ok" = True ] && [ "$secret_ok" = False ]; } || [ "$file" = unexpanded ]; then
      status=BROKEN
    elif [ "$env_ok" = True ] && [ "$secret_ok" = True ] && [ "$sts_ok" = True ] && [ "$file" = True ] \
         && [ "$daemon" = True ] && [ "$ctl" = True ]; then
      status=AUTH-ON
      if [ "$probe" = 1 ] && { [ "$unauth" != 401 ] || [ "$auth" != 404 ]; }; then status=OPEN; fi
    fi
    # Credentials rejected by a member that demands auth: broken, not merely open.
    if [ "$probe" = 1 ] && [ "$auth" = 401 ]; then status=BROKEN; fi
    [ "$status" = AUTH-ON ] || rc=1

    line="$ns/$pod $status env=$(yn "$env_ok") secret=$(yn "$secret_ok") sts=$(yn "$sts_ok") file=$fileword daemon=$(yn "$daemon") ctl=$(yn "$ctl") get=$(yn "$get_ok")"
    [ "$probe" = 1 ] && line="$line unauth=${unauth:-?} auth=${auth:-?}"
    echo "$line"
  done
done < <(jq -r '.items[] | "\(.metadata.namespace)/\(.metadata.name)"' <<<"$crs")

if [ -n "$only" ] && [ "$matched" = 0 ]; then echo "--cluster $only matched no postgresql CR" >&2; exit 1; fi
if [ "$audited" -eq 0 ]; then echo "no members audited" >&2; exit 1; fi
exit "$rc"
