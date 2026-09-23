#!/usr/bin/env bash
# Provision an Immich account so a person can sign in with SSO.
#
#   docs/immich/add-user.sh <email> [display name]
#   docs/immich/add-user.sh millerjj7332@gmail.com "Jeanne"
#
# This is REQUIRED before anyone can log in. `oauth.autoRegister` is false
# (cluster/immich/config.secret.yaml), so an OIDC login can only ever link to
# an account that already exists — it never creates one. That is what stops
# the whole internet from registering on a public photos.chrismiller.xyz.
#
# The email MUST be the exact address on their Google or Microsoft account:
# Immich matches the OIDC login to the account by email, and a mismatch just
# looks like "access denied" to them.
#
# A long random password is set and deliberately NOT kept anywhere. They sign
# in through Dex; nobody needs it. If it is ever needed, an admin resets it
# with PUT /api/admin/users/<id> rather than digging up a stored one.
set -euo pipefail

EMAIL="${1:?usage: add-user.sh <email> [display name]}"
NAME="${2:-$EMAIL}"

HOST=photos.chrismiller.xyz
API="https://$HOST/api"
R=(--resolve "$HOST:443:192.168.0.7" -sS --max-time 60)

KEY=$(kubectl -n immich get secret immich-accounts -o jsonpath='{.data.api-key}' | base64 -d)
H=(-H "x-api-key: $KEY" -H 'Content-Type: application/json')

if curl "${R[@]}" "${H[@]}" "$API/admin/users" | jq -e --arg e "$EMAIL" 'any(.[]; .email == $e)' >/dev/null; then
  echo "$EMAIL already has an account — nothing to do."
  exit 0
fi

# Built with jq so the password never lands in a process argument or the shell
# history, and never reaches stdout.
#
# /dev/urandom, NOT openssl: openssl only exists inside the nix shell, and a
# missing one expands to an EMPTY password that Immich cheerfully accepts —
# creating a live account anyone could log into. Hence the guard below; this
# already happened once (2026-09-23) and the four accounts had to be reset.
PW=$(head -c 32 /dev/urandom | base64 | tr -d '\n')
[ "${#PW}" -ge 32 ] || { echo "refusing to create a user: password generation failed" >&2; exit 1; }

body=$(jq -n --arg e "$EMAIL" --arg n "$NAME" --arg p "$PW" \
  '{email:$e, name:$n, password:$p, shouldChangePassword:false, notify:false}')
unset PW

printf '%s' "$body" | curl "${R[@]}" "${H[@]}" -X POST "$API/admin/users" -d @- \
  | jq -r 'if .id then "created \(.email)  id=\(.id)" else "FAILED: \(.message // .)" end'

cat <<EOF

They can now sign in at https://$HOST with "Sign in with galaxy", choosing the
account for $EMAIL. Nothing else to send them — no password, no invite.

To give them the family archive, re-run:  docs/immich/album-sync.sh
EOF
