#!/usr/bin/env bash

export SEALED_SECRETS_CONTROLLER_NAMESPACE=sealed-secrets
export SEALED_SECRETS_CONTROLLER_NAME=sealed-secrets

echo
printf '\e[38;2;255;255;255m\e[48;2;255;0;0m!!!   !!!   !!!     WARNING     !!!   !!!   !!!\n\e[0m'
echo "       YOU PROBABLY DON'T WANT TO DO THIS     "
echo "  ONLY PROCEED IF YOU KNOW WHAT YOU'RE DOING  "
printf '\e[38;2;255;255;255m\e[48;2;255;0;0m!!!   !!!   !!!     WARNING     !!!   !!!   !!!\n\e[0m'

echo
read -p "Continue? (y/n) " -n 1 -r
echo

if [[ $REPLY =~ ^[Yy]$ ]]; then
  for f in ./cluster/**/*.secret.yaml; do
    echo "$f"
    kubeseal -o yaml < "$f" > "${f%.secret.yaml}.yaml"
  done
fi
