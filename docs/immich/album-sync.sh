#!/usr/bin/env bash
# One shared album per top-level folder of the family archive, shared with
# everyone on the instance.
#
#   docs/immich/album-sync.sh [--dry-run]
#
# Idempotent by design: albums are matched by name and only assets that are
# missing get added, so this is safe to re-run after every nightly library
# scan. That is the point — new files under /family land in the right album
# without anyone opening the UI.
#
# Why a script and not config: albums, album membership and sharing are
# database state. cluster/immich/config.secret.yaml owns Immich's *settings*
# and cannot express any of this (see docs/immich.md).
#
# Auth: acts as the FAMILY account. An album can only hold assets its owner
# owns, and the archive belongs to family@chrismiller.xyz — so the albums must
# be family's too. See docs/immich.md "the family account has no login".
set -euo pipefail

DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT

HOST=photos.chrismiller.xyz
API="https://$HOST/api"
R=(--resolve "$HOST:443:192.168.0.7" -sS --max-time 120)

KEY=$(kubectl -n immich get secret immich-accounts -o jsonpath='{.data.family-api-key}' | base64 -d)
[ -n "$KEY" ] || { echo "no family-api-key in the immich-accounts secret — see docs/immich.md" >&2; exit 1; }
ADMIN=$(kubectl -n immich get secret immich-accounts -o jsonpath='{.data.api-key}' | base64 -d)

fam()  { curl "${R[@]}" -H "x-api-key: $KEY"   -H 'Content-Type: application/json' "$@"; }
adm()  { curl "${R[@]}" -H "x-api-key: $ADMIN" -H 'Content-Type: application/json' "$@"; }

# --- who owns what -----------------------------------------------------------
me=$(fam "$API/users/me" | jq -r '.email')
[ "$me" = "family@chrismiller.xyz" ] || { echo "family-api-key belongs to $me, not the family account" >&2; exit 1; }

# Viewer, not editor: the archive is read-only truth on disk and nobody should
# be able to restructure it from a phone.
adm "$API/admin/users" | jq -r '.[] | select(.email != "family@chrismiller.xyz") | .id' > "$WORK/share"
echo "sharing with $(wc -l < "$WORK/share") user(s)"

# --- every archive asset, bucketed by top-level folder ------------------------
page=1
: > "$WORK/assets"
while [ -n "$page" ]; do
  fam -X POST "$API/search/metadata" -d "{\"size\":1000,\"page\":$page}" > "$WORK/page.json"
  jq -r '.assets.items[]
         | select((.originalPath | split("/")) as $p | $p[1] == "family" and ($p | length) > 3)
         | [(.originalPath | split("/")[2]), .id] | @tsv' "$WORK/page.json" >> "$WORK/assets"
  page=$(jq -r '.assets.nextPage // ""' "$WORK/page.json")
done
echo "found $(wc -l < "$WORK/assets") assets under /family"
[ -s "$WORK/assets" ] || { echo "nothing to do — has the library scan finished?" >&2; exit 1; }

fam "$API/albums" > "$WORK/albums.json"

# --- one album per folder -----------------------------------------------------
cut -f1 "$WORK/assets" | sort -u | while read -r folder; do
  [ -n "$folder" ] || continue
  awk -F'\t' -v f="$folder" '$1==f{print $2}' "$WORK/assets" | sort -u > "$WORK/want"

  id=$(jq -r --arg n "$folder" 'map(select(.albumName==$n)) | .[0].id // ""' "$WORK/albums.json")
  if [ -z "$id" ]; then
    if [ "$DRY" = 1 ]; then echo "would create '$folder' ($(wc -l < "$WORK/want") assets)"; continue; fi
    users=$(jq -R . < "$WORK/share" | jq -s 'map({userId: ., role: "viewer"})')
    id=$(fam -X POST "$API/albums" \
           -d "$(jq -n --arg n "$folder" --argjson u "$users" '{albumName:$n, albumUsers:$u, assetIds:[]}')" \
         | jq -r '.id')
    : > "$WORK/have"
  else
    # Diff against what the album already holds so a re-run is nearly free.
    fam "$API/albums/$id" | jq -r '.assets[].id' | sort -u > "$WORK/have"
  fi

  comm -23 "$WORK/want" "$WORK/have" > "$WORK/add"
  n=$(wc -l < "$WORK/add")
  echo "$folder: $(wc -l < "$WORK/want") in archive, $n to add"
  [ "$n" -gt 0 ] || continue
  if [ "$DRY" = 1 ]; then continue; fi

  # Batched; Immich caps the request size and 500 is comfortably under it.
  split -l 500 "$WORK/add" "$WORK/batch-"
  for b in "$WORK"/batch-*; do
    fam -X PUT "$API/albums/$id/assets" -d "$(jq -R . < "$b" | jq -s '{ids: .}')" >/dev/null
    rm -f "$b"
  done
done

echo
fam "$API/albums" | jq -r 'sort_by(.albumName)[] | "  \(.albumName): \(.assetCount) assets -> \(.albumUsers|length) viewer(s)"'
