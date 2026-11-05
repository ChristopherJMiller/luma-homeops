#!/usr/bin/env bash
# Run terraform for azure/ci. Auth for the state backend and both Azure providers comes from
# `az login`; subscription_id and alert_emails come from ci.secret.auto.tfvars (git-crypt).
#
#   ./tf.sh init
#   ./tf.sh plan -no-color -out="$SCRATCH/ci.tfplan" | grep -E '^(Plan:|No changes|  # )'
#   ./tf.sh apply "$SCRATCH/ci.tfplan"        # Chris only
#   ./tf.sh output -raw dispatcher_client_secret | <pipe>   # never to a terminal
#
# F14: every UUID-shaped string on stderr, and on stdout too, becomes <id>. The exceptions are
# `output -raw|-json` and `show -json`, whose stdout is data meant for a pipe or a 0600 file.
# Files it writes are 0600 (umask 077). The exit code is terraform's. The filter works by line, so
# an interactive prompt doesn't show: apply a saved plan.
#
# Needs: `az login`, git-crypt unlocked, the nix toolchain.
set -euo pipefail
umask 077

repo="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
az account show >/dev/null 2>&1 || { echo "tf.sh: not logged in — run: az login" >&2; exit 1; }

secret="$repo/azure/ci/ci.secret.auto.tfvars"
if ! grep -q '^subscription_id' "$secret" 2>/dev/null; then
  echo "tf.sh: $secret is not readable cleartext — is git-crypt unlocked?" >&2
  exit 1
fi

redact() {
  # Ignore Ctrl-C here: terraform finishes its writes after an interrupt, and a dead filter would
  # stop terraform mid-apply through SIGPIPE.
  trap '' INT TERM
  sed -E 's/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/<id>/g'
}

raw_stdout() {
  local cmd="${1:-}" a
  shift || true
  for a in "$@"; do
    case "$cmd:$a" in
      output:-raw | output:-json | show:-json) return 0 ;;
    esac
  done
  return 1
}

# terraform's stderr goes through redact to the script's stderr. Its stdout goes to fd 3, which
# is the script's stdout.
run() {
  terraform "$@" 2>&1 1>&3 3>&- | redact >&2
  return "${PIPESTATUS[0]}"
}

cd "$repo/azure/ci"
set +e
if raw_stdout "$@"; then
  run "$@" 3>&1
  status=$?
else
  run "$@" 3>&1 | redact
  status=${PIPESTATUS[0]}
fi
exit "$status"
