#!/usr/bin/env bash
#
# Seal every *.secret.yaml that does not already have a sealed sibling.
#
# Two failure modes this guards against, both of which have bitten and both of
# which are SILENT — the sealed file looks present, Argo applies it, and
# nothing breaks until a pod restarts and finds its Secret missing:
#
#   1. kubeseal failing (controller unreachable, cert expired) while the shell
#      redirect has already created an empty output file.
#   2. a *.secret.yaml containing MORE THAN ONE document — kubeseal reads only
#      the first, so the rest are dropped and Argo prunes those Secrets.
#
set -uo pipefail

export SEALED_SECRETS_CONTROLLER_NAMESPACE=sealed-secrets
export SEALED_SECRETS_CONTROLLER_NAME=sealed-secrets

fail=0

for f in ./cluster/**/*.secret.yaml; do
  [ -e "$f" ] || continue
  out="${f%.secret.yaml}.yaml"
  [ -f "$out" ] && continue

  docs=$(grep -c '^kind: Secret' "$f" || true)
  if [ "$docs" -gt 1 ]; then
    echo "SKIP $f: $docs Secret documents in one file — kubeseal only reads the first." >&2
    echo "     Split it: one Secret per file." >&2
    fail=1
    continue
  fi

  echo "$f"
  tmp=$(mktemp)
  if ! kubeseal -o yaml < "$f" > "$tmp"; then
    echo "FAIL $f: kubeseal returned non-zero; leaving $out absent." >&2
    rm -f "$tmp"; fail=1; continue
  fi
  if [ ! -s "$tmp" ] || ! grep -q '^kind: SealedSecret' "$tmp"; then
    echo "FAIL $f: kubeseal produced no SealedSecret; leaving $out absent." >&2
    rm -f "$tmp"; fail=1; continue
  fi
  mv "$tmp" "$out"
done

[ "$fail" -eq 0 ] || { echo "sign.sh: one or more secrets were not sealed." >&2; exit 1; }
