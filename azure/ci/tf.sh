#!/usr/bin/env bash
# Run terraform for azure/ci. Auth for the state backend and both Azure providers comes from
# `az login`; subscription_id and alert_emails come from ci.secret.auto.tfvars (git-crypt).
#
#   ./tf.sh init
#   ./tf.sh plan -no-color -out="$SCRATCH/ci.tfplan" | grep -E '^(Plan:|No changes|  # )'
#   ./tf.sh apply "$SCRATCH/ci.tfplan"        # Chris only
#   ./tf.sh output -raw dispatcher_client_secret | <pipe>   # never to a terminal
#
# Needs: `az login`, git-crypt unlocked, the nix toolchain.
set -euo pipefail

repo="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
az account show >/dev/null 2>&1 || { echo "tf.sh: not logged in — run: az login" >&2; exit 1; }

secret="$repo/azure/ci/ci.secret.auto.tfvars"
if ! grep -q '^subscription_id' "$secret" 2>/dev/null; then
  echo "tf.sh: $secret is not readable cleartext — is git-crypt unlocked?" >&2
  exit 1
fi

cd "$repo/azure/ci"
exec terraform "$@"
