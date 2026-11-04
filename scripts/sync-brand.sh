#!/usr/bin/env bash
# Sync the Realliance brand kit (git.realliance.net/realliance/brand) into the forge's custom assets.
#
#   scripts/sync-brand.sh <tag> [--rebuild]      e.g. scripts/sync-brand.sh v0.1.0
#
# 1. Fetches <tag> and verifies it: an annotated tag named <tag>, signed by a key in TRUSTED_SIGNERS
#    below (luma-homeops has no keyring of its own, so the full fingerprints are pinned here and the
#    public key comes from your GPG keyring; a missing key, another key or an unsigned tag fails).
# 2. Copies the files the forge uses from dist/ at that tag into cluster/forgejo/custom/public/assets/
#    {img,fonts}, each checked against dist/manifest.json (sha256). Hashed files keep the brand's
#    content hash: logo-mocha.<8 hex>.svg. Fixed names (logo.svg, favicon.*, ...) stay as they are.
# 3. Points the references at the new names: realliance-fonts.css (font and logo url()s), which then
#    gets a hashed copy realliance-fonts.<8 hex>.css; the <head> hook (preload + stylesheet); the
#    ConfigMap file lists in cluster/forgejo/kustomization.yaml. Deletes hashed (and formerly unhashed)
#    copies nothing references any more, records the sync in cluster/forgejo/custom/brand.lock.json,
#    and bumps realliance/templates-rev (the <head> hook is read at start: Forgejo restarts once).
# 4. --rebuild: also rebuilds the tag with Nix and fails unless it equals the committed dist/.
#
# Never deploys anything: review `git diff`, run the tests (pre-commit `forge-fonts`), commit, and
# deploy as cluster/forgejo/custom/BRAND.md says. Downloads are data: they go into a fresh mktemp dir
# and are only read by git, sha256sum and cp.
set -euo pipefail

BRAND_REPO=${BRAND_REPO:-https://git.realliance.net/realliance/brand.git}
TRUSTED_SIGNERS=(
  E9245FED84E0EC721C7B5C336BFB8037115ADE26 # Christopher Miller <git@chrismiller.xyz>
)

tag=${1:-}
rebuild=false
[ "${2:-}" = "--rebuild" ] && rebuild=true
if ! [[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { [ $# -gt 1 ] && ! $rebuild; } || [ $# -gt 2 ]; then
  echo "usage: $0 <vX.Y.Z> [--rebuild]" >&2
  exit 2
fi

HERE=$(cd "$(dirname "$0")/.." && pwd)
CUSTOM=$HERE/cluster/forgejo/custom
ASSETS=$CUSTOM/public/assets
KUST=$HERE/cluster/forgejo/kustomization.yaml
HEADER=$CUSTOM/templates/custom/header.tmpl
CSS_SRC=$ASSETS/css/realliance-fonts.css
RELEASE=$HERE/cluster/applications/forgejo-release.yaml
LOCK=$CUSTOM/brand.lock.json

# consumer path (under public/assets) <- dist logical name; "hashed" or "fixed" consumer name
MAP='
img/logo.svg                                fixed/forgejo/logo.svg                     fixed
img/logo.png                                fixed/forgejo/logo.png                     fixed
img/favicon.svg                             fixed/forgejo/favicon.svg                  fixed
img/favicon.png                             fixed/forgejo/favicon.png                  fixed
img/apple-touch-icon.png                    fixed/forgejo/apple-touch-icon.png         fixed
img/logo-mocha.svg                          img/mark-wide-mocha.svg                    hashed
img/logo-latte.svg                          img/mark-wide-latte.svg                    hashed
fonts/atkinson-hyperlegible-Regular.woff2   fonts/atkinson-hyperlegible-Regular.woff2  hashed
fonts/atkinson-hyperlegible-Italic.woff2    fonts/atkinson-hyperlegible-Italic.woff2   hashed
fonts/atkinson-hyperlegible-Bold.woff2      fonts/atkinson-hyperlegible-Bold.woff2     hashed
fonts/atkinson-hyperlegible-BoldItalic.woff2 fonts/atkinson-hyperlegible-BoldItalic.woff2 hashed
fonts/fredoka-latin.woff2                   fonts/fredoka-latin.woff2                  hashed
fonts/noto-sans-mono-latin.woff2            fonts/noto-sans-mono-latin.woff2           hashed
fonts/OFL-atkinson-hyperlegible.txt         fonts/OFL-atkinson-hyperlegible.txt        fixed
fonts/OFL-fredoka.txt                       fonts/OFL-fredoka.txt                      fixed
fonts/OFL-noto-sans-mono.txt                fonts/OFL-noto-sans-mono.txt               fixed
'

die() { echo "sync-brand: $*" >&2; exit 1; }
snapshot() { (cd "$CUSTOM" && find . -type f ! -name brand.lock.json -print0 | sort -z | xargs -0 sha256sum | sha256sum); }
# hashed_name <path> <sha256>: a/b/name.ext -> a/b/name.<8 hex>.ext
hashed_name() { local d b e; d=$(dirname "$1"); b=$(basename "$1"); e=${b##*.}; echo "$d/${b%.*}.${2:0:8}.$e"; }
# point every /<name>(.<8 hex>).<ext> reference in a file at <new basename>
repoint() { # file base ext newbasename
  B="$2" E="$3" N="$4" perl -0pi -e 's{(?<=/)\Q$ENV{B}\E(?:\.[0-9a-f]{8})?\.\Q$ENV{E}\E(?![\w.-])}{$ENV{N}}g' "$1"
}
# delete <dir>/<base>.<ext> and <dir>/<base>.<8 hex>.<ext> except <keep>
prune() { # dir base ext keep [keep-unhashed]
  local f
  for f in "$1/$2.$3" "$1/$2".????????."$3"; do
    [ -e "$f" ] || continue
    [ "$(basename "$f")" = "$4" ] && continue
    [ "${5:-}" = keep-unhashed ] && [ "$(basename "$f")" = "$2.$3" ] && continue
    [[ $(basename "$f") =~ ^$2(\.[0-9a-f]{8})?\.$3$ ]] || continue
    rm -- "$f"
    echo "removed ${f#"$HERE"/}"
  done
}

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
brand=$work/brand

# ---- 1. fetch and verify the signed tag
git init -q "$brand"
git -C "$brand" fetch -q --depth=1 --no-tags "$BRAND_REPO" "+refs/tags/$tag:refs/tags/$tag" || die "cannot fetch $tag from $BRAND_REPO"
[ "$(git -C "$brand" cat-file -t "refs/tags/$tag")" = tag ] || die "$tag is not an annotated (signed) tag"
[ "$(git -C "$brand" cat-file tag "refs/tags/$tag" | sed -n 's/^tag //p')" = "$tag" ] || die "tag object is not named $tag"
status=$(git -C "$brand" verify-tag --raw "refs/tags/$tag" 2>&1 >/dev/null) || die "signature of $tag does not verify (is the signer's public key in your keyring?)"
grep -qE '^\[GNUPG:\] (BADSIG|EXPKEYSIG|REVKEYSIG|EXPSIG|ERRSIG)' <<<"$status" && die "signature of $tag is bad, expired or revoked"
signer=$(awk '$2 == "VALIDSIG" {print $NF; exit}' <<<"$status")
sigkey=$(awk '$2 == "VALIDSIG" {print $3; exit}' <<<"$status")
ok=false
for f in "${TRUSTED_SIGNERS[@]}"; do [ "$f" = "$signer" ] || [ "$f" = "$sigkey" ] && ok=true; done
$ok || die "$tag is signed by $signer, which is not in TRUSTED_SIGNERS"
commit=$(git -C "$brand" rev-parse "refs/tags/$tag^{commit}")
tagobj=$(git -C "$brand" rev-parse "refs/tags/$tag")
git -C "$brand" -c advice.detachedHead=false checkout -q "$commit"
echo "verified $tag ($commit), signed by $signer"

manifest=$brand/dist/manifest.json
[ -f "$manifest" ] || die "$tag has no dist/manifest.json"

# ---- 4 (first, so nothing is touched if it fails). --rebuild: the tag's own build must equal its dist/
if $rebuild; then
  out=$(nix build --no-link --print-out-paths "git+file://$brand?rev=$commit&shallow=1#dist") || die "nix build of $tag failed"
  diff -r --brief "$out" "$brand/dist" || die "a fresh build of $tag differs from its committed dist/"
  echo "rebuilt $tag: identical to its committed dist/ ($out)"
fi

# ---- 2. copy, each file checked against the manifest
before=$(snapshot)
declare -A newname
lockfiles='{}'
while read -r dest logical mode; do
  [ -n "$dest" ] || continue
  file=$(jq -er --arg l "$logical" '.files[$l].file' "$manifest") || die "$logical is not in the manifest of $tag"
  sum=$(jq -er --arg l "$logical" '.files[$l].sha256' "$manifest")
  echo "$sum  $brand/dist/$file" | sha256sum -c --quiet - || die "dist/$file does not match its sha256 in manifest.json"
  target=$dest
  [ "$mode" = hashed ] && target=$(hashed_name "$dest" "$sum")
  install -m 0644 "$brand/dist/$file" "$ASSETS/$target"
  newname[$dest]=$(basename "$target")
  lockfiles=$(jq -c --arg t "$target" --arg l "$logical" --arg s "$sum" '. + {($t): {from: $l, sha256: $s}}' <<<"$lockfiles")
done <<<"$MAP"

# ---- 3. references, the hashed stylesheet, stale copies
while read -r dest logical mode; do
  [ "$mode" = hashed ] || continue
  b=$(basename "$dest"); base=${b%.*}; ext=${b##*.}
  for f in "$CSS_SRC" "$HEADER" "$KUST"; do repoint "$f" "$base" "$ext" "${newname[$dest]}"; done
  prune "$ASSETS/$(dirname "$dest")" "$base" "$ext" "${newname[$dest]}"
done <<<"$MAP"
grep -qE 'url\([^)]*(\.\./(fonts|img)/[^)"]+)' "$CSS_SRC" || die "realliance-fonts.css has no font or image url() left (unexpected)"
for u in $(grep -oE '\.\./(fonts|img)/[^)"]+' "$CSS_SRC" | sort -u); do
  [ -f "$ASSETS/css/$u" ] || die "realliance-fonts.css references $u, which does not exist"
done
css_sum=$(sha256sum "$CSS_SRC" | cut -d' ' -f1)
css_hashed=realliance-fonts.${css_sum:0:8}.css
install -m 0644 "$CSS_SRC" "$ASSETS/css/$css_hashed"
repoint "$HEADER" realliance-fonts css "$css_hashed"
repoint "$KUST" realliance-fonts css "$css_hashed"
prune "$ASSETS/css" realliance-fonts css "$css_hashed" keep-unhashed
lockfiles=$(jq -c --arg t "css/$css_hashed" --arg s "$css_sum" '. + {($t): {from: "cluster/forgejo/custom/public/assets/css/realliance-fonts.css", sha256: $s}}' <<<"$lockfiles")

# every hashed name the header and the ConfigMaps use must exist
for ref in $(grep -ohE '[A-Za-z0-9_-]+\.[0-9a-f]{8}\.(css|woff2|svg|png)' "$HEADER" "$KUST" | sort -u); do
  [ -n "$(find "$ASSETS" -name "$ref" -print -quit)" ] || die "a reference to $ref is left with no file"
done

after=$(snapshot)
if [ "$before" != "$after" ]; then
  rev=$(sed -nE 's/.*realliance\/templates-rev: "([0-9]+)".*/\1/p' "$RELEASE")
  [ -n "$rev" ] || die "realliance/templates-rev not found in $RELEASE"
  sed -i -E "s/(realliance\/templates-rev: \")$rev\"/\1$((rev + 1))\"/" "$RELEASE"
  echo "bumped realliance/templates-rev $rev -> $((rev + 1)) (Forgejo restarts once on deploy)"
fi

jq -n --arg repo "${BRAND_REPO%.git}" --arg tag "$tag" --arg tagobj "$tagobj" --arg commit "$commit" --arg signer "$signer" \
  --arg msum "$(sha256sum "$manifest" | cut -d' ' -f1)" --argjson files "$lockfiles" \
  '{about: "Written by scripts/sync-brand.sh: the realliance/brand release these assets come from. Do not edit.",
    repo: $repo, tag: $tag, tag_object: $tagobj, commit: $commit, signer: $signer, manifest_sha256: $msum,
    files: ($files | to_entries | sort_by(.key) | from_entries)}' > "$LOCK"
echo "synced realliance/brand $tag into ${ASSETS#"$HERE"/}; review with: git status && git diff --stat"
