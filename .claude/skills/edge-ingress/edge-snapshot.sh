#!/usr/bin/env bash
# Behavioural snapshot of every public host via Traefik (192.168.0.7): status,
# redirect target, auth challenge, CSP presence, served-cert CN.
# Usage: nix-shell -p openssl --run ./edge-snapshot.sh > before.txt
#        (change) ...
#        nix-shell -p openssl --run ./edge-snapshot.sh > after.txt
#        diff <(sed "s/ cert=.*//" before.txt) <(sed "s/ cert=.*//" after.txt)
# Add new public hosts to the list below when you add them.
set -u
hosts="$(printf '%s\n' chrismiller.xyz www.chrismiller.xyz ci attic auth-admin auth-family dex home lidarr plex prowlarr radarr sabnzbd share sonarr mediadav music dav.music lagrange dashboard rccl-tracker wordarena realliance.net www.realliance.net legacy.realliance.net)"
for h in $hosts; do
  case $h in *.net|*.xyz) fq=$h;; *) fq=$h.chrismiller.xyz;; esac
  for path in / /share/; do
    [ "$path" = /share/ ] && [ "$fq" != share.chrismiller.xyz ] && continue
    out=$(curl -sS -o /dev/null --max-time 15 --resolve "$fq:443:192.168.0.7" \
      -w '%{http_code} loc=%{redirect_url} ct=%{content_type}' "https://$fq$path" 2>&1 | sed 's/[?&]state=[^ &]*//; s/rd=[^ &]*/rd=…/')
    hdr=$(curl -sSI --max-time 15 --resolve "$fq:443:192.168.0.7" "https://$fq$path" 2>/dev/null | grep -iE '^(www-authenticate|content-security-policy|access-control-allow-origin|set-cookie: word-arena)' | cut -c1-60 | sort | tr '\n' '|')
    cn=$(echo | openssl s_client -connect 192.168.0.7:443 -servername "$fq" 2>/dev/null | openssl x509 -noout -subject 2>/dev/null | sed 's/.*CN=//')
    printf '%-28s %-8s %s hdr=[%s] cert=%s\n' "$fq$path" "" "$out" "$hdr" "$cn"
  done
done
