#!/usr/bin/env bash
# Configure Ombi through its settings API. Idempotent — re-running re-asserts
# every setting below, so this is the closest thing to config-as-code that Ombi
# allows: it keeps all of its configuration in its own database, not in files.
#
#   nix develop -c docs/ombi/bootstrap.sh
#
# What it sets, and why in this order:
#
#   1. the local admin account          (also CREATES the Ombi roles — nothing
#                                        below can reference a role until this
#                                        has run at least once)
#   2. Ombi's own API key
#   3. Plex, for "already in the library" badges          (skipped if no token)
#   4. Radarr / Sonarr / Lidarr, on the 1080p profiles    <- the resolution cap
#   5. the 4K movie feature + Radarr's 4K half
#   6. default roles for auto-created users               <- the approval gate
#   7. the Discord agent                                 (skipped if no webhook)
#   8. header auth                                        LAST, deliberately:
#      until this is on, nobody is auto-provisioned, so a failure in 2-7 cannot
#      strand a half-configured Ombi that is already creating family accounts.
#
# Nothing here is destructive. The one thing it CREATES outside Ombi is a 4K
# quality profile in Radarr, if none exists, because the 4K approval gate is
# meaningless without one — announced loudly when it happens, and deletable
# from Radarr's UI. Quality profiles are not GitOps-managed in this repo (the
# mega-media reconciler disclaims them by design).
set -euo pipefail

NS=media
OMBI_SVC=svc/mm-ombi
OMBI_PORT=3579
LOCAL_PORT="${LOCAL_PORT:-13579}"

# Resolved by NAME against the live *arr, not by hard-coded id — ids differ
# between instances and a wrong one silently caps quality at the wrong place.
PROFILE_MOVIE="${PROFILE_MOVIE:-HD-1080p}"
PROFILE_MOVIE_4K="${PROFILE_MOVIE_4K:-Ultra-HD}"
PROFILE_TV="${PROFILE_TV:-HD-1080p}"
PROFILE_MUSIC="${PROFILE_MUSIC:-Standard}"

# Music requests OFF by default. Lidarr keeps running for Chris's own use — this
# only removes music from Ombi's family-facing request flow. Ombi's SPA asks
# GET /Settings/lidarrenabled to decide whether to offer music at all, so
# disabling Lidarr here is what actually hides it; dropping the two music roles
# below means the API refuses even if some UI path survives. Set to true to
# restore both halves together.
ENABLE_MUSIC="${ENABLE_MUSIC:-false}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }

for bin in kubectl jq curl; do
  command -v "$bin" >/dev/null || { echo "need $bin — run this as: nix develop -c docs/ombi/bootstrap.sh" >&2; exit 1; }
done

# ---------------------------------------------------------------- secrets ----
# jq rather than -o jsonpath: jsonpath chokes on the hyphenated keys below, and
# `// ""` makes an absent key read as empty instead of failing the whole script.
sec() { kubectl -n "$NS" get secret "$1" -o json | jq -r --arg k "$2" '.data[$k] // "" | @base64d'; }

ADMIN_USER=$(sec ombi-bootstrap admin-username)
ADMIN_PASS=$(sec ombi-bootstrap admin-password)
DISCORD_URL=$(sec ombi-bootstrap discord-webhook-url || true)
PLEX_TOKEN=$(sec ombi-bootstrap plex-token || true)
OMBI_APIKEY=$(sec api-keys ombi)

[ -n "$ADMIN_USER" ] && [ -n "$ADMIN_PASS" ] || { echo "ombi-bootstrap secret is incomplete" >&2; exit 1; }
[ "${#OMBI_APIKEY}" -ge 16 ] || { echo "api-keys/ombi looks wrong (${#OMBI_APIKEY} chars)" >&2; exit 1; }

# --------------------------------------------------------- port-forward ------
# Ombi's own host is behind the oauth2-proxy family gate, which would bounce
# this script to a sign-in page. Talk to the Service directly instead.
kubectl -n "$NS" port-forward "$OMBI_SVC" "$LOCAL_PORT:$OMBI_PORT" >/dev/null 2>&1 &
PF_PID=$!
trap 'kill "$PF_PID" 2>/dev/null || true' EXIT
API="http://127.0.0.1:$LOCAL_PORT/api"

for _ in $(seq 1 60); do
  curl -sf -o /dev/null "http://127.0.0.1:$LOCAL_PORT/" && break
  sleep 1
done
curl -sf -o /dev/null "http://127.0.0.1:$LOCAL_PORT/" \
  || { echo "Ombi did not answer on 127.0.0.1:$LOCAL_PORT — is mm-ombi Ready?" >&2; exit 1; }

# ------------------------------------------------------------ *arr helpers ---
# The *arr containers ship curl, so query them in place rather than opening
# three more port-forwards.
arr() { # arr <name> <port> <apiver> <path>
  local name=$1 port=$2 ver=$3 path=$4
  local key; key=$(sec api-keys "$name")
  kubectl -n "$NS" exec "deploy/mm-$name" -c "$name" -- \
    curl -sS -H "X-Api-Key: $key" "http://localhost:$port/api/$ver/$path"
}
arr_post() { # arr_post <name> <port> <apiver> <path>   (body on stdin)
  local name=$1 port=$2 ver=$3 path=$4
  local key; key=$(sec api-keys "$name")
  kubectl -n "$NS" exec -i "deploy/mm-$name" -c "$name" -- \
    curl -sS -H "X-Api-Key: $key" -H 'Content-Type: application/json' \
      -X POST "http://localhost:$port/api/$ver/$path" -d @-
}

profile_id() { # profile_id <name> <port> <apiver> <profile name>  -> id or ""
  arr "$1" "$2" "$3" qualityprofile | jq -r --arg n "$4" '.[] | select(.name == $n) | .id' | head -1
}
profile_names() { arr "$1" "$2" "$3" qualityprofile | jq -r '.[].name' | paste -sd', '; }

# ------------------------------------------------------------ Ombi helpers ---
TOKEN=""
oget() { curl -sS -H "Authorization: Bearer $TOKEN" "$API/$1"; }
opost() { curl -sS -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
            -X POST "$API/$1" -d @-; }

# ============================================================== 1. admin =====
say "1. admin account"
wizard=$(jq -n --arg u "$ADMIN_USER" --arg p "$ADMIN_PASS" \
          '{username:$u, password:$p, usePlexAdminAccount:false}' \
        | curl -sS -H 'Content-Type: application/json' -X POST "$API/v1/Identity/Wizard" -d @-)
if [ "$(jq -r '.result' <<<"$wizard")" = "true" ]; then
  note "created the admin, and seeded Ombi's roles"
else
  note "already exists — $(jq -r '.errors[0] // "no change"' <<<"$wizard")"
fi

TOKEN=$(jq -n --arg u "$ADMIN_USER" --arg p "$ADMIN_PASS" \
          '{username:$u, password:$p, rememberMe:true}' \
        | curl -sS -H 'Content-Type: application/json' -X POST "$API/v1/token" -d @- \
        | jq -r '.access_token // empty')
[ -n "$TOKEN" ] || { echo "could not log in as the admin — is admin-password in sync with Ombi?" >&2; exit 1; }
note "authenticated"

# ============================================================ 2. api key =====
say "2. Ombi API key + general settings"
oget v1/Settings/ombi \
  | jq --arg k "$OMBI_APIKEY" '
      .apiKey = $k
      # Every request pings Discord, not only the ones needing approval.
      # SendNotificationRule gates NewRequest on whether the USER HOLDS
      # AutoApproveMovie -- not on whether this request was auto-approved -- so
      # setting this true would also mute the pending-4K ping we actually want.
      | .doNotSendNotificationsForAutoApprove = false
      | .hideRequestsUsers = false
      | .defaultLanguageCode = "en"' \
  | opost v1/Settings/ombi >/dev/null
note "api key set; auto-approve notifications left ON (see the comment in this script)"

# =============================================================== 3. Plex =====
say "3. Plex"
if [ -z "$PLEX_TOKEN" ]; then
  note "SKIPPED — plex-token is empty in the ombi-bootstrap secret."
  note "Ombi will fall back to asking Radarr/Sonarr what already exists."
else
  # Ombi normally learns the machine identifier from its "Load Servers" button,
  # which we are not clicking. Without it Ombi cannot match library content to
  # this server, so fetch it from Plex directly — /identity needs no token.
  # Asked via radarr because that container has curl and can reach the Service.
  PLEX_MID=$(kubectl -n "$NS" exec deploy/mm-radarr -c radarr -- \
    curl -sS -H 'Accept: application/json' \
      http://mm-plex.media.svc.cluster.local:32400/identity \
    | jq -r '.MediaContainer.machineIdentifier // empty')
  [ -n "$PLEX_MID" ] || { echo "could not read Plex's machineIdentifier — is mm-plex up?" >&2; exit 1; }

  # NB the top-level flag is `enable`, not `enabled` (PlexSettings.Enable).
  # plexSelectedLibraries left as-is: empty means "all libraries".
  oget v1/Settings/plex \
    | jq --arg t "$PLEX_TOKEN" --arg mid "$PLEX_MID" '
        .enable = true
        | .enableWatchlistImport = false
        | .servers = [ ((.servers[0] // {}) + {
            name: "galaxy",
            plexAuthToken: $t,
            machineIdentifier: $mid,
            ip: "mm-plex.media.svc.cluster.local",
            port: 32400,
            ssl: false,
            subDir: null,
            episodeBatchSize: 150,
            plexSelectedLibraries: ((.servers[0].plexSelectedLibraries) // [])
          }) ]' \
    | opost v1/Settings/plex >/dev/null
  note "pointed at mm-plex.media.svc.cluster.local:32400 (machine $PLEX_MID)"
fi

# ======================================================== 4. the *arrs =======
say "4. Radarr / Sonarr / Lidarr — the resolution cap"

RADARR_QP=$(profile_id radarr 7878 v3 "$PROFILE_MOVIE")
SONARR_QP=$(profile_id sonarr 8989 v3 "$PROFILE_TV")
[ -n "$RADARR_QP" ] || { echo "Radarr has no profile named '$PROFILE_MOVIE'. Have: $(profile_names radarr 7878 v3)" >&2; exit 1; }
[ -n "$SONARR_QP" ] || { echo "Sonarr has no profile named '$PROFILE_TV'. Have: $(profile_names sonarr 8989 v3)" >&2; exit 1; }

# Radarr wants the root folder as a PATH; Sonarr wants its ID. Getting these
# the wrong way round fails at request time, not here.
RADARR_ROOT=$(arr radarr 7878 v3 rootfolder | jq -r '.[0].path')
SONARR_ROOT_ID=$(arr sonarr 8989 v3 rootfolder | jq -r '.[0].id')
note "movies -> $PROFILE_MOVIE (qp $RADARR_QP) into $RADARR_ROOT"
note "tv     -> $PROFILE_TV (qp $SONARR_QP) into root id $SONARR_ROOT_ID"
if [ "$ENABLE_MUSIC" = "true" ]; then
  LIDARR_QP=$(profile_id lidarr 8686 v1 "$PROFILE_MUSIC")
  [ -n "$LIDARR_QP" ] || { echo "Lidarr has no profile named '$PROFILE_MUSIC'. Have: $(profile_names lidarr 8686 v1)" >&2; exit 1; }
  LIDARR_ROOT=$(arr lidarr 8686 v1 rootfolder | jq -r '.[0].path')
  note "music  -> $PROFILE_MUSIC (qp $LIDARR_QP) into $LIDARR_ROOT"
else
  LIDARR_QP=1; LIDARR_ROOT=""
  note "music  -> DISABLED in Ombi (ENABLE_MUSIC=false); Lidarr itself is untouched"
fi

# ---- 4K profile in Radarr, created only if absent -------------------------
RADARR_4K_QP=$(profile_id radarr 7878 v3 "$PROFILE_MOVIE_4K")
if [ -z "$RADARR_4K_QP" ]; then
  say "   Radarr has no '$PROFILE_MOVIE_4K' profile — CREATING one"
  note "the 4K approval gate needs somewhere to send an approved 4K request."
  note "delete it from Radarr > Settings > Profiles if you'd rather shape it yourself."
  created=$(arr radarr 7878 v3 qualityprofile/schema \
    | jq --arg n "$PROFILE_MOVIE_4K" '
        # 2160p, but deliberately NOT Remux-2160p. A 4K remux is 50-90 GB, and
        # sustained writes past ~25 GiB are what make these SMR OSDs flap and
        # stall Ceph (the SMR notes in CLAUDE.md). WEBDL/Bluray 2160p land
        # around 15-40 GB, which the array copes with.
        def mark:
          if has("quality") and (.quality != null) then
            (if (.quality.name | test("2160p")) and ((.quality.name | test("Remux")) | not)
             then .allowed = true else . end)
          else
            .items |= map(mark) | .allowed = ([.items[].allowed] | any)
          end;
        # Radarr rejects a cutoff that names a quality nested inside a GROUP
        # ("Cutoff must be an allowed quality or group") -- WEBDL-2160p lives in
        # the "WEB 2160p" group, so the cutoff has to be the id of the group itself.
        # Hence: pick the allowed TOP-LEVEL item that contains WEBDL-2160p.
        def top_id: if (.quality // null) != null then .quality.id else .id end;
        def has_webdl2160:
          ([ .. | objects | select(has("quality") and (.quality != null)) | .quality.name ]
           | index("WEBDL-2160p")) != null;
        .name = $n
        | .upgradeAllowed = true
        | .items |= map(mark)
        # Stop upgrading once a 4K WEB release is in hand rather than chasing
        # ever-larger files forever.
        | .cutoff = ( ( [ .items[] | select(.allowed) | select(has_webdl2160) | top_id ] | first )
                      // ( [ .items[] | select(.allowed) | top_id ] | min ) )' \
    | arr_post radarr 7878 v3 qualityprofile)
  RADARR_4K_QP=$(jq -r '.id // empty' <<<"$created")
  [ -n "$RADARR_4K_QP" ] \
    || { echo "could not create the 4K profile: $(jq -c '.' <<<"$created" | head -c 400)" >&2; exit 1; }
  note "created '$PROFILE_MOVIE_4K' as qp $RADARR_4K_QP"
fi

# Radarr and Radarr4K save together in one call (RadarrCombinedModel). Both
# point at the SAME Radarr instance: only the quality profile differs, so an
# approved 4K request grabs a 4K release of a film we don't already hold.
# (A second copy alongside a 1080p one would need a second Radarr — see
# docs/ombi.md.)
RADARR_KEY=$(sec api-keys radarr)
SONARR_KEY=$(sec api-keys sonarr)
LIDARR_KEY=$(sec api-keys lidarr)

jq -n --arg qp "$RADARR_QP" --arg qp4k "$RADARR_4K_QP" --arg root "$RADARR_ROOT" --arg key "$RADARR_KEY" '
  { radarr: { enabled: true, apiKey: $key,
              ip: "mm-radarr.media.svc.cluster.local", port: 7878, ssl: false, subDir: null,
              defaultQualityProfile: $qp, defaultRootPath: $root,
              minimumAvailability: "Released", addOnly: false,
              scanForAvailability: true, prioritizeArrAvailability: false, sendUserTags: true },
    radarr4K: { enabled: true, apiKey: $key,
              ip: "mm-radarr.media.svc.cluster.local", port: 7878, ssl: false, subDir: null,
              defaultQualityProfile: $qp4k, defaultRootPath: $root,
              minimumAvailability: "Released", addOnly: false,
              scanForAvailability: true, prioritizeArrAvailability: false, sendUserTags: true } }' \
  | opost v1/Settings/radarr >/dev/null
note "radarr + radarr4K saved"

# Sonarr: rootPath is an ID, not a path (TvSender does int.Parse on it).
jq -n --arg qp "$SONARR_QP" --arg root "$SONARR_ROOT_ID" --arg key "$SONARR_KEY" '
  { enabled: true, apiKey: $key,
    ip: "mm-sonarr.media.svc.cluster.local", port: 8989, ssl: false, subDir: null,
    qualityProfile: $qp, rootPath: $root, seasonFolders: true,
    qualityProfileAnime: $qp, rootPathAnime: $root,
    addOnly: false, scanForAvailability: true, prioritizeArrAvailability: false,
    sendUserTags: true, languageProfile: 1, languageProfileAnime: 1 }' \
  | opost v1/Settings/sonarr >/dev/null
note "sonarr saved"

jq -n --arg qp "$LIDARR_QP" --arg root "$LIDARR_ROOT" --arg key "$LIDARR_KEY" \
      --argjson on "$( [ "$ENABLE_MUSIC" = "true" ] && echo true || echo false )" '
  { enabled: $on, apiKey: $key,
    ip: "mm-lidarr.media.svc.cluster.local", port: 8686, ssl: false, subDir: null,
    defaultQualityProfile: $qp, defaultRootPath: $root, albumFolder: true }' \
  | opost v1/Settings/lidarr >/dev/null
note "lidarr saved (enabled=$ENABLE_MUSIC)"

# ======================================================= 5. 4K feature =======
say "5. 4K movie requests"
jq -n '{name:"Movie4KRequests", enabled:true}' \
  | curl -sS -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
      -X POST "$API/v2/Features/enable" -d @- >/dev/null
note "Movie4KRequests enabled — the 4K toggle now shows on movie pages"

# ===================================================== 6. default roles =====
say "6. default roles for auto-created users"
# Request4KMovie WITHOUT AutoApprove4KMovie is the whole approval gate: family
# members may ASK for 4K, and only Chris can say yes. Standard requests are
# auto-approved and capped at 1080p by the profiles set in step 4.
oget v1/Settings/UserManagement \
  | jq --argjson music "$( [ "$ENABLE_MUSIC" = "true" ] && echo true || echo false )" '
      .defaultRoles = (["RequestMovie","RequestTv","ManageOwnRequests",
                        "AutoApproveMovie","AutoApproveTv","Request4KMovie"]
                       + (if $music then ["RequestMusic","AutoApproveMusic"] else [] end))
      | .importPlexUsers = false
      | .importPlexAdmin = false
      | .movieRequestLimit = 10   | .movieRequestLimitType = 1
      | .episodeRequestLimit = 50 | .episodeRequestLimitType = 1
      | .musicRequestLimit = 20   | .musicRequestLimitType = 1
      | .defaultStreamingCountry = "US"' \
  | opost v1/Settings/UserManagement >/dev/null
note "auto-approve for standard requests; 4K movies require approval"
note "weekly limits: 10 movies / 50 episodes / 20 albums per person"

# ========================================================== 7. Discord =======
say "7. Discord"
if [ -z "$DISCORD_URL" ]; then
  note "SKIPPED — discord-webhook-url is empty in the ombi-bootstrap secret."
  note "Add it (edit cluster/media/ombi-bootstrap.secret.yaml, ./sign.sh, commit),"
  note "then re-run this script."
else
  # GET-patch-POST so Ombi's own seeded templates survive; we only touch the
  # ones we care about.
  #
  # ONLY NewRequest is enabled, and that is about which channel this lands in.
  # The webhook is currently the one alertmanager already uses, so these
  # messages share a channel with Ceph warnings and CRITICAL pages. In that
  # company, "now available" and "declined" are noise for the operator — who
  # declined it already knows — while NewRequest is the one line that asks for
  # an action. Give Ombi its own #media-requests webhook and turning the other
  # two on becomes reasonable.
  #
  # {RequestStatus} leads the subject so "Pending Approval" is legible at a
  # glance next to an alert; see docs/ombi.md for why every request notifies
  # rather than only the ones awaiting approval.
  oget v1/Settings/notifications/discord \
    | jq --arg u "$DISCORD_URL" '
        .enabled = true
        | .webhookUrl = $u
        | .username = "Ombi"
        # Ombi seeds a template per notification type and enables most of them
        # (Issue*, RequestApproved, PartiallyAvailable, RequestDeleted, ...).
        # Leaving those on would put all of it in the alerts channel, so this is
        # an allowlist, not a couple of targeted opt-outs: enable NewRequest and
        # nothing else. Widen it once Ombi has its own channel.
        | .notificationTemplates |= map(
            .enabled = (.notificationType == 0)
            | if .notificationType == 0 then            # NewRequest
                .subject = "{RequestStatus}: {Title}"
                | .message = "{Alias} requested **{Title}** ({Year}) — {Type}, {RequestStatus}. https://requests.chrismiller.xyz/requests"
              else . end)' \
    | opost v1/Settings/notifications/discord >/dev/null
  note "webhook set; NewRequest is the ONLY enabled template (allowlist, not opt-out)"
  note "subject leads with {RequestStatus} — 'Pending Approval' is the one to action"
fi

# ======================================================= 8. header auth ======
say "8. header auth (the Dex family tier handoff)"
oget v1/Settings/authentication \
  | jq '
      .enableHeaderAuth = true
      | .headerAuthVariable = "X-Auth-Request-Email"
      | .headerAuthCreateUser = true
      | .enableOAuth = false
      | .allowNoPassword = false
      | .requiredLength = 12
      | .requireDigit = false | .requireLowercase = false
      | .requireUppercase = false | .requireNonAlphanumeric = false' \
  | opost v1/Settings/authentication >/dev/null
note "Ombi now trusts X-Auth-Request-Email from oauth2-proxy-family"

say "done"
cat <<EOF

  Family members just open https://requests.chrismiller.xyz — they sign in with
  Google (or Microsoft) via Dex and Ombi creates their account on first visit.
  Nothing to send them: no password, no invite.

  You resolve to the admin account because ombi-bootstrap/admin-username is the
  same address the family allowlist admits. If you ever need the local login,
  the password is in the ombi-bootstrap Secret.

  Verify:
    kubectl -n $NS port-forward $OMBI_SVC $LOCAL_PORT:$OMBI_PORT
    curl -s -H "ApiKey: <api-keys/ombi>" http://127.0.0.1:$LOCAL_PORT/api/v1/Identity/Users | jq '.[] | {userName, roles: [.claims[]? | select(.enabled) | .value]}'
EOF
