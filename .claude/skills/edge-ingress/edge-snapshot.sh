#!/usr/bin/env bash
# Behavioural snapshot of every public host via Traefik (192.168.0.7): status,
# redirect target, auth challenge, CSP presence, served-cert CN.
# Usage: nix-shell -p openssl jq --run ./edge-snapshot.sh > before.txt
#        (change) ...
#        nix-shell -p openssl jq --run ./edge-snapshot.sh > after.txt
#        diff <(sed "s/ cert=.*//" before.txt) <(sed "s/ cert=.*//" after.txt)
# Add new public hosts to the list below when you add them.
set -u
hosts="$(printf '%s\n' chrismiller.xyz www.chrismiller.xyz ci attic auth-admin auth-family dex galaxy photos home lidarr plex prowlarr radarr sabnzbd share sonarr requests mediadav music dav.music lagrange dashboard rccl-tracker wordarena realliance.net www.realliance.net legacy.realliance.net family.werethemille.rs mcp)"
for h in $hosts; do
  # Fully-qualified if it carries one of our zone suffixes; bare names are
  # assumed to be chrismiller.xyz, which is where most hosts live.
  case $h in *.net|*.xyz|*.rs|*.art) fq=$h;; *) fq=$h.chrismiller.xyz;; esac
  for path in / /share/; do
    [ "$path" = /share/ ] && [ "$fq" != share.chrismiller.xyz ] && continue
    out=$(curl -sS -o /dev/null --max-time 15 --resolve "$fq:443:192.168.0.7" \
      -w '%{http_code} loc=%{redirect_url} ct=%{content_type}' "https://$fq$path" 2>&1 | sed 's/[?&]state=[^ &]*//; s/rd=[^ &]*/rd=…/')
    hdr=$(curl -sSI --max-time 15 --resolve "$fq:443:192.168.0.7" "https://$fq$path" 2>/dev/null | grep -iE '^(www-authenticate|content-security-policy|access-control-allow-origin|set-cookie: word-arena)' | cut -c1-60 | sort | tr '\n' '|')
    cn=$(echo | openssl s_client -connect 192.168.0.7:443 -servername "$fq" 2>/dev/null | openssl x509 -noout -subject 2>/dev/null | sed 's/.*CN=//')
    printf '%-28s %-8s %s hdr=[%s] cert=%s\n' "$fq$path" "" "$out" "$hdr" "$cn"
  done
done

# MCP front door (facade + gateway + Dex discovery). One line per probe:
#   METHOD host+path status loc=<redirect, state= stripped> [<www-authenticate>] body=<jq -cS of /.well-known/*>
# Bodies are printed on purpose: a diff shows THAT they changed, you must read WHAT.
while read -r method fq path; do
  [ -z "$method" ] && continue
  args=(-sS --max-time 15 --resolve "$fq:443:192.168.0.7" -X "$method")
  if [ "$method" = POST ]; then
    case $path in
      /oauth/register) args+=(-H 'content-type: application/json' --data '{}');;
      *) args+=(-H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
           --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"edge-snapshot","version":"0"}}}');;
    esac
  fi
  hf=$(mktemp); bf=$(mktemp)
  code=$(curl "${args[@]}" -D "$hf" -o "$bf" -w '%{http_code}|%{redirect_url}' "https://$fq$path" 2>&1)
  status=${code%%|*}
  loc=$(printf '%s' "${code#*|}" | sed 's/[?&]state=[^ &]*//')
  wa=$(grep -i '^www-authenticate:' "$hf" | tr -d '\r' | sed 's/^[^:]*: *//')
  body=""
  case $path in
    /.well-known/*)
      if jq -e . "$bf" >/dev/null 2>&1; then body=" body=$(jq -cS . "$bf")"; else body=" body=<non-json>"; fi;;
  esac
  printf '%s %s%s %s loc=%s [%s]%s\n' "$method" "$fq" "$path" "$status" "$loc" "$wa" "$body"
  rm -f "$hf" "$bf"
done <<'PROBES'
GET mcp.chrismiller.xyz /gramps/mcp
POST mcp.chrismiller.xyz /gramps/mcp
GET mcp.chrismiller.xyz /.well-known/oauth-protected-resource/gramps/mcp
GET mcp.chrismiller.xyz /.well-known/oauth-authorization-server
GET dex.chrismiller.xyz /.well-known/oauth-authorization-server
GET dex.chrismiller.xyz /.well-known/openid-configuration
GET mcp.chrismiller.xyz /oauth/register
POST mcp.chrismiller.xyz /oauth/register
GET mcp.chrismiller.xyz /oauth/authorize
PROBES
