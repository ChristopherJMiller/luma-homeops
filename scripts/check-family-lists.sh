#!/usr/bin/env bash
# The family allowlist exists in two files; they must hold the same addresses.
#
# cluster/oauth2-proxy/emails-family.secret.yaml is the list of RECORD — it
# decides who may reach the family hosts at all, on both registrable domains.
# cluster/gramps/new-user-emails.secret.yaml is a copy that exists only because a
# Secret cannot be mounted across namespaces: Gramps Web needs the same list in
# its own namespace to decide which first-time OIDC login is created as an editor
# instead of landing disabled (images/grampsweb/oidc-new-user-role.patch).
#
# Drift always fails safe — one direction means a relative is created disabled
# and needs a manual promotion (which is what the copy exists to avoid), the
# other means they never reach the host at all — but both are confusing to
# debug, and making them impossible is cheaper than documenting them.
#
# Both files are git-crypt encrypted at rest and cleartext in the working tree,
# so this only works where the key is unlocked. If it cannot read them it fails
# rather than passing an empty comparison. It reports HOW MANY addresses differ
# and on which side, never which ones.
set -uo pipefail

edge=cluster/oauth2-proxy/emails-family.secret.yaml
app=cluster/gramps/new-user-emails.secret.yaml

# The block scalar under `emails: |`, minus comments and blank lines, lowercased
# and sorted — so ordering and case cannot be the difference.
addresses() {
  awk '/^  emails: \|/{found=1;next} found && /^[^[:space:]]/{exit} found' "$1" \
    | sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
    | tr '[:upper:]' '[:lower:]' \
    | grep . \
    | sort -u
}

for f in "$edge" "$app"; do
  if [ ! -f "$f" ]; then
    echo "check-family-lists: $f is missing." >&2
    exit 1
  fi
done

edge_list=$(mktemp); app_list=$(mktemp)
trap 'rm -f "$edge_list" "$app_list"' EXIT
addresses "$edge" > "$edge_list"
addresses "$app" > "$app_list"

for pair in "$edge:$edge_list" "$app:$app_list"; do
  f=${pair%:*}; l=${pair#*:}
  if [ ! -s "$l" ]; then
    echo "check-family-lists: no addresses found in $f." >&2
    echo "     If this is a fresh clone, unlock git-crypt first: git-crypt unlock" >&2
    exit 1
  fi
done

only_edge=$(comm -23 "$edge_list" "$app_list" | wc -l)
only_app=$(comm -13 "$edge_list" "$app_list" | wc -l)

if [ "$only_edge" -eq 0 ] && [ "$only_app" -eq 0 ]; then
  exit 0
fi

echo "check-family-lists: the two family allowlists disagree." >&2
[ "$only_edge" -gt 0 ] && echo "     $only_edge address(es) in $edge are not in $app" >&2
[ "$only_app" -gt 0 ] && echo "     $only_app address(es) in $app are not in $edge" >&2
echo "     The oauth2-proxy file is the list of record; mirror it into the gramps one." >&2
exit 1
