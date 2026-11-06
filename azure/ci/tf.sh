#!/usr/bin/env bash
# Run terraform for azure/ci. Auth for the state backend and both Azure providers comes from
# `az login`; subscription_id and alert_emails come from ci.secret.auto.tfvars (git-crypt).
#
#   ./tf.sh init
#   ./tf.sh plan -no-color -out="$SCRATCH/ci.tfplan" | grep -E '^(Plan:|No changes|  # )'
#   ./tf.sh apply "$SCRATCH/ci.tfplan"        # Chris only
#   ./tf.sh plan -destroy -out="$SCRATCH/destroy.tfplan"   # rollback: Chris applies the saved plan
#   ./tf.sh --append <file> output -raw dispatcher_client_secret
#   ./tf.sh --to "$SCRATCH/plan.json" show -json "$SCRATCH/ci.tfplan"
#
# F14:
# - Every UUID-shaped string on stderr and stdout becomes <id>.
# - The raw data paths, `output -raw|-json` and `show -json`, need an explicit destination as the
#   first argument: `--to FILE` (truncate) or `--append FILE`. tf.sh opens FILE itself, 0600, and
#   writes the raw output only there. Without one they're refused, whatever stdout is: an agent's
#   transcript can be a regular file too.
# - Lock Info's `ID:` line keeps its UUID: it's the state lock id that `force-unlock -force` needs.
# - It refuses `state pull`, `state push`, `-chdir`, and any TF_CLI_ARGS* or TF_LOG* variable.
#   Terraform splices TF_CLI_ARGS* into argv after these checks, and a TF_LOG trace prints
#   request bodies, secrets included.
# - Files it writes are 0600 (umask 077), and the exit code is terraform's.
# - The filter works by line, so an interactive prompt doesn't show: apply a saved plan.
#
# Needs: `az login`, git-crypt unlocked, the nix toolchain.
set -euo pipefail
umask 077

refuse() { echo "tf.sh: $*" >&2; exit 2; }

# ${!prefix*} is core bash (the dev shell's bash has no compgen).
for v in ${!TF_CLI_ARGS*} ${!TF_LOG*}; do
  refuse "unset $v first: tf.sh can't vet what it adds"
done

# The destination for a raw form, only as the first argument.
dest="" mode=""
case "${1:-}" in
  --to | --append)
    [[ $# -ge 2 && -n ${2:-} ]] || refuse "$1 needs a file"
    mode="${1#--}" dest="$2"
    shift 2
    ;;
esac

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
  case "$a" in
    --to | --to=* | --append | --append=*) refuse "--to and --append go first: tf.sh --to FILE <terraform args>" ;;
  esac
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
if ((raw)) && [[ -z $dest ]]; then
  refuse "$cmd's raw output needs a destination: tf.sh --to FILE ... or tf.sh --append FILE ..."
fi
if [[ -n $dest ]] && ! ((raw)); then
  refuse "--$mode is only for output -raw|-json and show -json"
fi

# Resolve the destination against the caller's directory, then vet it: a new file or an existing
# regular file, not a link, not a device or /proc path, and not this script's own stdout or
# stderr (a transcript, say).
if [[ -n $dest ]]; then
  [[ $dest == /* ]] || dest="$PWD/$dest"
  [[ -L $dest ]] && refuse "--$mode: $dest is a symlink"
  [[ -e $dest && ! -f $dest ]] && refuse "--$mode: $dest is not a regular file"
  [[ -d "$(dirname -- "$dest")" ]] || refuse "--$mode: no directory for $dest"
  case "$(realpath -m -- "$dest")" in
    /dev/* | /proc/* | /sys/*) refuse "--$mode: $dest is not an ordinary file" ;;
  esac
  if [[ -e $dest ]] && { [[ $dest -ef /dev/stdout ]] || [[ $dest -ef /dev/stderr ]]; }; then
    refuse "--$mode: $dest is tf.sh's own stdout or stderr"
  fi
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
  # A line that is only Lock Info's `ID: <uuid>`, after any box, space or color prefix, keeps its
  # UUID. Every other UUID-shaped string becomes <id>.
  sed -E '/^(\x1b\[[0-9;]*m|[^[:alnum:]])*ID:[[:space:]]+[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}[[:space:]]*(\x1b\[[0-9;]*m)?$/!s/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/<id>/g'
}

# terraform's stderr goes through redact to the script's stderr. Its stdout goes to fd 3: the
# destination file for a raw form, else the script's stdout, through redact.
run() {
  terraform "$@" 2>&1 1>&3 3>&- | redact >&2
  return "${PIPESTATUS[0]}"
}

if [[ -n $dest ]]; then
  # tf.sh tightens an existing file first; a new one gets 0600 from the umask.
  [[ -e $dest ]] && chmod 600 -- "$dest"
fi

cd "$repo/azure/ci"
set +e
if [[ $mode == to ]]; then
  run "$@" 3>"$dest"
  status=$?
elif [[ $mode == append ]]; then
  run "$@" 3>>"$dest"
  status=$?
else
  run "$@" 3>&1 | redact
  status=${PIPESTATUS[0]}
fi
exit "$status"
