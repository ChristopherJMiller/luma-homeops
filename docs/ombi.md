# Ombi — family media requests

`requests.chrismiller.xyz`. Family members search for a film or show, Ombi hands
the request to Radarr/Sonarr/Lidarr on a quality profile *we* pick, so resolution
is capped by configuration rather than by trust. Normal requests go through
without anyone being asked; 4K movies wait for approval and ping Discord.

Deployed by the mega-media chart (`ombi.enabled` in
`cluster/applications/mega-media-release.yaml`); gated, sealed and configured
from this repo (`cluster/media/ombi-*`, `docs/ombi/bootstrap.sh`).

---

## How the login works, and why it isn't OIDC

**Ombi has no OIDC client.** Its only authentication modes are local accounts,
Plex OAuth, and *header SSO*. So Ombi is not registered with Dex the way Immich
is; instead the family tier authenticates at the edge and hands Ombi an
already-trusted identity in a header.

```
family member ─▶ requests.chrismiller.xyz
   └─▶ Traefik [oauth2-family-errors, oauth2-family-auth]
         └─▶ oauth2-proxy-family ─▶ Dex ─┬─▶ Google
                                          └─▶ Microsoft
   ◀── back through Traefik carrying X-Auth-Request-Email ──┐
   └─▶ Ombi's login page POSTs /api/v1/token/header_auth ───┘
         └─▶ finds the user by UserName, or creates them with the default roles
```

Three consequences worth internalising:

- **The auth middleware is not optional decoration.** It is where Ombi's identity
  comes from. Remove it and Ombi does not become "open" — it becomes broken, with
  a login page that cannot authenticate anybody.
- **Chris resolves to the admin account** only because
  `ombi-bootstrap/admin-username` is the same address the family allowlist
  admits. If those ever drift, Chris gets auto-provisioned as an ordinary family
  user and Ombi has no administrator. Keep them in sync.
- **The Ombi mobile apps cannot sign in.** They can't complete a browser
  forward-auth redirect. Web only. (This is exactly why Immich talks OIDC
  directly instead — its phone sync is the point of running it. Ombi has no
  equivalent reason.)

## Roles, and what the cap really is

Auto-created users get `UserManagementSettings.DefaultRoles`:

| Role | Granted | Effect |
|---|---|---|
| `RequestMovie` / `RequestTv` / `RequestMusic` | yes | may ask for things |
| `AutoApproveMovie` / `AutoApproveTv` / `AutoApproveMusic` | yes | standard requests need no nod |
| `ManageOwnRequests` | yes | can cancel their own |
| `Request4KMovie` | **yes** | may ask for 4K |
| `AutoApprove4KMovie` | **no** | …but cannot grant it to themselves |

That last row *is* the approval gate. Everything else is the cap:

- **Standard movies** → Radarr on `HD-1080p`.
- **TV** → Sonarr on `HD-1080p`. Note Ombi has no `Request4KTv` role at all, so
  TV has no 4K tier and no per-request approval path short of removing
  `AutoApproveTv`.
- **Approved 4K movies** → Radarr on `Ultra-HD`, which `bootstrap.sh` creates if
  absent. That profile deliberately **excludes Remux-2160p** and cuts off at
  `WEBDL-2160p`: a 4K remux is 50-90 GB, and sustained writes past ~25 GiB are
  what make these SMR OSDs flap and stall Ceph.
- **Weekly per-person limits** as a backstop: 10 movies, 50 episodes, 20 albums.

Ombi's own settings all live in its database, so `docs/ombi/bootstrap.sh` is the
source of truth for the above, not any YAML file. It is idempotent; re-running it
re-asserts every setting.

## Discord

Ombi's Discord agent is notification-only — you approve in Ombi's web UI, not
from the message. The `NewRequest` template leads with `{RequestStatus}`, so a
line reading **Pending Approval** is one you need to action and
*Processing Request* is one that already went through.

**It pings for every request, not only the ones needing approval**, and that is
deliberate rather than an oversight. `SendNotificationRule` decides whether to
send `NewRequest` by asking whether the *user holds* `AutoApproveMovie` — not
whether this particular request was auto-approved. So switching
`DoNotSendNotificationsForAutoApprove` on would also mute the pending-4K ping,
which is the one that matters. It stays off.

If the noise grates, the one-line fix is dropping `AutoApproveMovie` from
`defaultRoles` in `bootstrap.sh`: then every movie needs a nod and every ping is
actionable, at the cost of friction for the family.

## Running the bootstrap

```sh
nix-shell shell.nix --run docs/ombi/bootstrap.sh
```

It port-forwards to `mm-ombi` (Ombi's public host is behind the family gate,
which would bounce a script to a sign-in page), then applies everything in a
deliberate order: the admin account first, because creating it is also what
seeds Ombi's roles, and **header auth last**, because until that is on nobody is
auto-provisioned — so a failure midway cannot strand a half-configured Ombi that
is already minting family accounts.

Two inputs start empty in `cluster/media/ombi-bootstrap.secret.yaml`, and the
script says so and skips their step rather than guessing:

| Key | Get it from | Without it |
|---|---|---|
| `discord-webhook-url` | Discord → Server Settings → Integrations → Webhooks | no notifications at all |
| `plex-token` | an `X-Plex-Token` from any Plex web request | no "already in the library" badges; Ombi asks the *arrs instead |

To fill one in: edit the `.secret.yaml`, `./sign.sh`, commit, let Argo apply it,
then re-run the script.

## Day 2

**Someone has the wrong roles.** `defaultRoles` only applies at creation, so
changing it does not retro-fit existing users. Fix the individual in Ombi →
Users, or delete them and let their next visit recreate them.

**Approve a 4K request.** Ombi → Requests → the pending one → Approve. Radarr
picks it up on the `Ultra-HD` profile. If nothing downloads, see the next point.

**An approved 4K request downloads nothing.** Expected, if we already hold the
film. `MovieSender.SendToRadarr` returns *"Movie is already monitored"* when the
TMDB id is already in Radarr and monitored — it does **not** re-target the
quality profile. So the 4K path only grabs for titles not already in the library.
Keeping a 4K copy *alongside* a 1080p one needs a second Radarr instance
(`radarr4k` added to the tuple in the chart's `arr-deployments.yaml`) with its own
root folder and Plex library. Not built.

**Ombi won't start after a chart change.** Check the init containers first:
`wait-for-db`, then three `create-ombi-*-if-missing`, then `init-database-json`.
Ombi needs **three** Postgres databases (`ombi_main`, `ombi_settings`,
`ombi_external`) because it runs three independent EF Core contexts, each
migrating on its own connection — two sharing a database fight over
`__EFMigrationsHistory`.

**Postgres is Ombi's least-trodden path.** It exists in Ombi's source
(`src/Ombi.Store/Context/Postgres/`, selected by `"Type": "Postgres"` in
`/config/database.json`) but Ombi's own docs list only SQLite/MySQL/MariaDB. If
first-boot migrations ever wedge, the escape hatch is deleting `database.json`
from the config PVC, which drops Ombi back to SQLite on that volume.

**Locked out.** The local admin password is in the `ombi-bootstrap` Secret.
Ombi's login page auto-submits header auth when it is enabled, so to reach the
password form you need to get at Ombi with no `X-Auth-Request-Email` header —
`kubectl -n media port-forward svc/mm-ombi 13579:3579` and browse
`127.0.0.1:13579`.

## Not built

- **Approve from Discord.** Would need a small bot: Ombi's generic Webhook agent
  POSTs an in-cluster service, which posts an embed with Approve/Deny buttons and
  calls `POST /api/v1/Request/movie/approve` on the press. A gateway bot keeps it
  outbound-only, so no new public endpoint. `reconciler/` in the chart repo is the
  precedent for where such a service would live and how it would be built.
- **A 4K library separate from the 1080p one** (see Day 2 above).
- **Metrics.** Ombi exposes no Prometheus endpoint, so there is no ServiceMonitor.
  `/health` exists and aggregates Plex + every *arr — useful for a blackbox probe,
  and the reason the pod's own probes hit `/` instead: a Radarr blip must not take
  the request UI NotReady.
