#!/usr/bin/env bash

export SEALED_SECRETS_CONTROLLER_NAMESPACE=sealed-secrets
export SEALED_SECRETS_CONTROLLER_NAME=sealed-secrets

for f in ./cluster/**/*.secret.yaml; do
  if [ ! -f "${f%.secret.yaml}.yaml" ]; then
    echo "$f"
    kubeseal -o yaml < "$f" > "${f%.secret.yaml}.yaml"
  fi
done
