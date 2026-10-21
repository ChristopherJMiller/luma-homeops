#!/usr/bin/env bash
# Behavioural test for the fluent-bit exclusions in cluster/applications/fluent-bit.yaml:
# the tail input's Exclude_Path (luma-sandbox*/luma-browser* namespaces) and the
# grep filter that drops Luma's Traefik access lines. Runs the pinned fluent-bit
# image against CRI-format fixture files and checks what reaches stdout.
# Needs docker and yq:  nix shell nixpkgs#yq-go -c scripts/test-fluent-bit-exclusion.sh
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
app="$root/cluster/applications/fluent-bit.yaml"
tmp=$(mktemp -d)
# fluent-bit runs as root and writes its DB into $tmp/state; clean that as root too.
trap 'docker run --rm -v "$tmp/state:/s" busybox:1.36.1 sh -c "rm -rf /s/*" >/dev/null 2>&1 || true; rm -rf "$tmp"' EXIT
chmod 755 "$tmp"

tag=$(yq '.spec.source.helm.valuesObject.image.tag' "$app")
yq '.spec.source.helm.valuesObject.config.inputs' "$app" >"$tmp/inputs.txt"
yq '.spec.source.helm.valuesObject.config.filters' "$app" >"$tmp/filters.txt"

# Print the sections of file $1 (split at lines starting with '[') that contain
# a line equal to $2 (e.g. "Name tail").
sections() {
  awk -v want="$2" '
    function flush() { if (buf != "" && hit) printf "%s", buf; buf = ""; hit = 0 }
    /^\[/ { flush() }
    { buf = buf $0 "\n"; l = $0; gsub(/^[ \t]+|[ \t]+$/, "", l); if (l == want) hit = 1 }
    END { flush() }' "$1"
}

mkdir -p "$tmp/logs" "$tmp/state"
{
  printf '[SERVICE]\n    Flush 1\n    Log_Level info\n'
  # Test-only overrides: read fixtures from the start, keep the DB in the temp dir.
  sections "$tmp/inputs.txt" "Name tail" | grep -v '^[[:space:]]*DB ' |
    sed 's|^\[INPUT\]$|[INPUT]\n    Read_From_Head On\n    DB /state/test.db|'
  sections "$tmp/filters.txt" "Name grep"
  printf '[OUTPUT]\n    Name stdout\n    Match *\n'
} >"$tmp/fluent-bit.conf"

id=$(printf 'a%.0s' $(seq 64))
ts=2026-10-21T00:00:00.000000000Z
cri() { printf '%s stdout F %s\n' "$ts" "$1"; }

# fixture stem (<pod>_<namespace>_<container>) | expected
cases=(
  "app-1_default_app|ship"
  "luma-core-1_default_core|ship"
  "web-1_default_luma-agent|ship"
  "x-1_luma_c|ship"
  "x-1_notluma-x_c|ship"
  "core-1_luma-system_core|ship"
  "c-1_luma-0b-canary_c|ship"
  "luma-sandbox-1_default_c|ship"
  "sb-1_default_luma-sandbox|ship"
  "x-1_notluma-sandbox_c|ship"
  "sb-1_luma-sandbox_shell|drop"
  "br-1_luma-browser_chrome|drop"
  "c-1_luma-sandbox-0b-canary_c|drop"
)
for c in "${cases[@]}"; do
  stem=${c%%|*}
  cri "MARK_${stem}" >"$tmp/logs/${stem}-${id}.log"
done

# Traefik: five CLF lines; each marker carries its line number.
t="$tmp/logs/traefik-1_traefik_traefik-${id}.log"
routers=(
  "websecure-luma-system-luma-luma-chrismiller-xyz@kubernetes|drop"
  "web-luma-system-luma-luma-chrismiller-xyz@kubernetes|drop"
  "luma-system-share@kubernetescrd|drop"
  "websecure-media-mm-plex-plex-chrismiller-xyz@kubernetes|ship"
  "websecure-notluma-x-a-b@kubernetes|ship"
)
: >"$t"
n=0
for r in "${routers[@]}"; do
  n=$((n + 1))
  cri "10.0.0.1 - - [21/Oct/2026:00:00:00 +0000] \"GET /?code=x HTTP/2.0\" 200 5 \"-\" \"-\" ${n} \"${r%%|*}\" \"http://10.1.1.1:80\" 3ms MARK_traefik_${n}" >>"$t"
done

echo "running fluent/fluent-bit:${tag} for 15s..." >&2
timeout 15 docker run --rm \
  -v "$tmp/logs:/var/log/containers:ro" -v "$tmp/state:/state" \
  -v "$tmp/fluent-bit.conf:/fluent-bit/etc/fluent-bit.conf:ro" \
  "fluent/fluent-bit:${tag}" >"$tmp/out.txt" 2>&1 || true

fail=0
check() { # marker expected label
  local present=0
  if grep -q "$1\b" "$tmp/out.txt"; then present=1; fi
  if { [ "$2" = ship ] && [ "$present" = 1 ]; } || { [ "$2" = drop ] && [ "$present" = 0 ]; }; then
    echo "PASS $3 ($2)"
  else
    echo "FAIL $3 (expected $2, marker present=$present)"
    fail=1
  fi
}
for c in "${cases[@]}"; do check "MARK_${c%%|*}" "${c##*|}" "${c%%|*}"; done
n=0
for r in "${routers[@]}"; do
  n=$((n + 1))
  check "MARK_traefik_${n}" "${r##*|}" "traefik line $n"
done
exit "$fail"
