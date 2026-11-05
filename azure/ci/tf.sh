#!/usr/bin/env bash
# Run terraform for azure/ci. Auth for the state backend and both Azure providers comes from
# `az login`; subscription_id and alert_emails come from ci.secret.auto.tfvars (git-crypt).
#
#   ./tf.sh init
#   ./tf.sh plan -no-color -out="$SCRATCH/ci.tfplan" | grep -E '^(Plan:|No changes|  # )'
#   ./tf.sh apply "$SCRATCH/ci.tfplan"        # Chris only
#   ./tf.sh plan -destroy -out="$SCRATCH/destroy.tfplan"   # rollback: Chris applies the saved plan
#   ./tf.sh output -raw dispatcher_client_secret >> <0600 file>
#   ./tf.sh show -json "$SCRATCH/ci.tfplan" > "$SCRATCH/plan.json"
#
# F14:
# - Every UUID-shaped string on stderr, and on stdout too, becomes <id>.
# - Two exceptions. The raw data paths, `output -raw|-json` and `show -json`, keep stdout exact,
#   and run only when stdout is a regular file, never a terminal, a pipe, or a transcript.
#   Lock Info's `ID:` line also stays: it's the state lock id that `force-unlock` needs.
# - `state pull` and `state push` are refused, and so is `-chdir`: this script owns the directory.
# - Files it writes are 0600 (umask 077), and the exit code is terraform's.
# - The filter works by line, so an interactive prompt doesn't show: apply a saved plan.
#
# Needs: `az login`, git-crypt unlocked, the nix toolchain.
set -euo pipefail
umask 077

refuse() { echo "tf.sh: $*" >&2; exit 2; }

# Go's flag package takes -x, --x, -x=true, --x=1 and so on; this reads them all the same way.
flag_on() { # arg name
  local a="${1#-}"
  a="${a#-}"
  case "$a" in
    "$2") return 0 ;;
    "$2"=*) case "${a#*=}" in 1 | t | T | true | TRUE | True) return 0 ;; esac ;;
  esac
  return 1
}

# The subcommand is the first argument that isn't a global option.
cmd="" sub="" raw=0
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  a="${args[i]}"
  if [[ -z $cmd ]]; then
    case "$a" in
      -chdir | -chdir=* | --chdir | --chdir=*) refuse "tf.sh runs terraform in azure/ci; drop -chdir" ;;
      -*) continue ;;
      *) cmd="$a"; continue ;;
    esac
  fi
  [[ -z $sub && $a != -* ]] && sub="$a"
  case "$cmd" in
    output) flag_on "$a" raw && raw=1; flag_on "$a" json && raw=1 ;;
    show) flag_on "$a" json && raw=1 ;;
  esac
done
if [[ $cmd == state && ($sub == pull || $sub == push) ]]; then
  refuse "state $sub is refused: it moves the whole state, secrets included, outside terraform"
fi
if ((raw)) && ! [ -f /dev/stdout ]; then
  refuse "$cmd's raw output goes only to a regular file (> or >>), never a terminal or a pipe"
fi

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
  # A line that is only Lock Info's `ID: <uuid>`, after any box, space or colour prefix, keeps its
  # UUID. Every other UUID-shaped string becomes <id>.
  sed -E '/^(\x1b\[[0-9;]*m|[^[:alnum:]])*ID:[[:space:]]+[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}[[:space:]]*(\x1b\[[0-9;]*m)?$/!s/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/<id>/g'
}

# terraform's stderr goes through redact to the script's stderr. Its stdout goes to fd 3, which
# is the script's stdout.
run() {
  terraform "$@" 2>&1 1>&3 3>&- | redact >&2
  return "${PIPESTATUS[0]}"
}

cd "$repo/azure/ci"
set +e
if ((raw)); then
  run "$@" 3>&1
  status=$?
else
  run "$@" 3>&1 | redact
  status=${PIPESTATUS[0]}
fi
exit "$status"
