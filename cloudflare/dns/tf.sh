#!/usr/bin/env bash
# Run terraform for cloudflare/dns with two Cloudflare tokens, each from its own
# git-crypt file. Never prints either token; both travel in the environment.
#
#   CLOUDFLARE_API_TOKEN       the DNS token the cluster already uses
#                              (cluster/dns/cloudflare-api-token.secret.yaml),
#                              for the default provider
#   TF_VAR_workers_api_token   the workers token, which the cluster never gets
#                              (cloudflare/dns/workers-api-token.secret.conf,
#                              one `api-token=<value>` line), for the
#                              `cloudflare.workers` alias only
#
#   ./tf.sh init
#   ./tf.sh plan
#   ./tf.sh apply
#
# Needs: git-crypt unlocked, `az login` (state backend), nix-shell toolchain.
set -euo pipefail

repo="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
secret="$repo/cluster/dns/cloudflare-api-token.secret.yaml"

if ! grep -q '^stringData:' "$secret" 2>/dev/null; then
  echo "tf.sh: $secret is not readable cleartext — is git-crypt unlocked?" >&2
  exit 1
fi

CLOUDFLARE_API_TOKEN="$(awk '/^  api-token:/ { gsub(/["'"'"']/, "", $2); print $2 }' "$secret")"
[ -n "$CLOUDFLARE_API_TOKEN" ] || { echo "tf.sh: could not read api-token from secret" >&2; exit 1; }
export CLOUDFLARE_API_TOKEN

# The workers token. Every refusal names the file and never shows its contents.
workers="cloudflare/dns/workers-api-token.secret.conf"
if [ ! -f "$repo/$workers" ]; then
  echo "tf.sh: $workers is missing; Chris writes it (README, Workflow)" >&2
  exit 1
fi
if [ "$(head -c 10 "$repo/$workers" | tr -d '\0')" = GITCRYPT ]; then
  echo "tf.sh: $workers is still encrypted — is git-crypt unlocked?" >&2
  exit 1
fi
if [ "$(grep -c '^api-token=' "$repo/$workers" || true)" != 1 ]; then
  echo "tf.sh: $workers must hold exactly one api-token=<value> line" >&2
  exit 1
fi
TF_VAR_workers_api_token="$(sed -n 's/^api-token=//p' "$repo/$workers" | tr -d '\r\t "'"'"'')"
[ -n "$TF_VAR_workers_api_token" ] || { echo "tf.sh: $workers has an empty api-token" >&2; exit 1; }
export TF_VAR_workers_api_token

cd "$repo/cloudflare/dns"
exec terraform "$@"
