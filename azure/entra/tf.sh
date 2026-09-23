#!/usr/bin/env bash
# Run terraform for azure/entra. Auth for both the state backend and the
# azuread provider comes from `az login` — no tokens in this script.
#
#   ./tf.sh init
#   ./tf.sh plan
#   ./tf.sh apply
#   ./tf.sh output -raw dex_client_secret   # to seal into the cluster
#
# Needs: `az login`, nix-shell toolchain.
set -euo pipefail

repo="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
az account show >/dev/null 2>&1 || { echo "tf.sh: not logged in — run: az login" >&2; exit 1; }

cd "$repo/azure/entra"
exec terraform "$@"
