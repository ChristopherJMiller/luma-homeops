#!/usr/bin/env bash
# Run terraform for cloudflare/dns with the Cloudflare token sourced from the
# git-crypt secret the cluster already uses. Never prints the token.
#
#   ./tf.sh init
#   ./tf.sh plan
#   ./tf.sh apply
#
# Needs: git-crypt unlocked, `az login` (state backend), nix-shell toolchain.
set -euo pipefail

repo="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
secret="$repo/cluster/external-dns/cloudflare-api-token.secret.yaml"

if ! grep -q '^stringData:' "$secret" 2>/dev/null; then
  echo "tf.sh: $secret is not readable cleartext — is git-crypt unlocked?" >&2
  exit 1
fi

CLOUDFLARE_API_TOKEN="$(awk '/^  api-token:/ { gsub(/["'"'"']/, "", $2); print $2 }' "$secret")"
[ -n "$CLOUDFLARE_API_TOKEN" ] || { echo "tf.sh: could not read api-token from secret" >&2; exit 1; }
export CLOUDFLARE_API_TOKEN

cd "$repo/cloudflare/dns"
exec terraform "$@"
