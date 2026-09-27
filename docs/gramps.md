# Gramps Web — the family tree, and the MCP endpoint over it

`family.werethemille.rs` is the genealogy app; `mcp.chrismiller.xyz/gramps/mcp` is
an AI-facing, read/write view of the same tree. Both went in 2026-09-25/27.

Almost nothing here is where a first guess would put it, and most of the
surprises cost an outage or a security hole to find, so this file leads with the
decisions and their reasons rather than a feature tour.

```
browser ─▶ Traefik ─▶ oauth2-proxy-family-rs ─▶ grampsweb ─▶ Dex ─┬─▶ Google
            (family.werethemille.rs)              │               └─▶ Microsoft
                                                  │
desktop Gramps ──(username+password, /api)────────┤   tree data ──▶ Postgres (acid-gramps)
                                                  │   media ──────▶ B2  s3://galaxy-family/gramps
AI client ─▶ Traefik ─▶ mcp-jwt-auth ─▶ mcp-gateway ─▶ gramps-mcp ─┘   tree dir ──▶ CephFS (1Gi)
            (mcp.chrismiller.xyz)      (OAuth+DCR)    (read/write)
```

| Piece | Where | Notes |
|---|---|---|
| App | `cluster/gramps/` | web + celery + valkey, Argo app `gramps` |
| Tree data, users, search index | `acid-gramps` (Zalando, PG 16) | 7th postgres cluster; stock Spilo |
| Media | B2 `galaxy-family/gramps/` | shares the family bucket and its key |
| Tree directory | CephFS 1Gi RWX | metadata only — `name.txt`, backend marker, lock |
| Shared cache | CephFS 10Gi RWX | uploads + thumbnails, **must** be shared |
| MCP server | `cluster/gramps/mcp.yaml` | Chris's patched cabout-me/gramps-mcp, read/write, no Ingress |
| MCP gateway | `cluster/mcp-gateway/` | OAuth + dynamic client registration |
| MCP authorization | `cluster/oauth2-proxy/mcp-jwt-auth.yaml` | our validator, `images/mcp-jwt-auth` |

## Storage: three decisions that are not obvious

**Tree data is in Postgres, and getting it there required multi-tree mode.**
`NEW_DB_BACKEND` is consulted *only* when `TREE: "*"`: `trees.py` is the sole code
path that passes the backend to `WebDbManager` and it aborts
`405 "Not allowed in single-tree setup"` otherwise, `WebDbManager` defaults to
`create_backend="sqlite"`, and `app.py:161` only pushes `POSTGRES_HOST/PORT` into
Gramps' own config in multi-tree mode. So single-tree mode silently creates
**SQLite** trees on the PVC and says nothing. The first deploy made two of them.

The tree now exists, so config is back to single-tree pinned by **`TREE_ID`**
(`app.py:139`: *"TREE_ID takes precedence: identify tree by dirname, never by
name"*) — which is what stops a name mismatch creating a fresh SQLite tree beside
the real one. Reverting was safe because the tree describes its own storage:
`settings.ini` inside the tree directory holds `dbname/host/port`, and
`database.txt` says `sharedpostgresql`.

*If you ever need to create a second tree, you must flip to `TREE: "*"` first, or
it will be SQLite.*

**Media is a prefix inside the existing bucket.** `MEDIA_BASE_DIR` is
`s3://galaxy-family/gramps`. The docs only ever show `s3://<bucket>`, but
`MediaHandlerS3` splits the URL — bucket is the first segment, prefix is the rest
— so this reuses the `galaxy-family` bucket and its existing bucket-scoped key,
alongside Immich's mirror. No new bucket, no new key, nothing needed from the B2
master key.

That key has **no `deleteFiles`**, which turns out to be a feature: B2 answers an
S3 `DeleteObject` by *hiding* the version, so deleting a photo in the UI removes
it from the app but leaves it recoverable under the bucket's 90-day rule.

**`/app/cache` and `/app/thumbnail_cache` must be shared** between the web and
celery pods (one RWX claim, subPaths). An upload lands in
`/app/cache/export/<uuid>.gramps` in whichever pod served the request and the
import runs as a celery task in *another* pod; with per-pod `emptyDir`s the worker
finds nothing and the API returns a bare `500 "Import failed"` with the real cause
only in the celery log. `run_import` swallows plugin failures into that generic
500 — **so always read the celery log, not the response.**

## Who gets in

Two independent layers, because each answers a different question:

| Layer | Question | Where |
|---|---|---|
| oauth2-proxy `family-rs` | may this person reach the site at all? | Ingress annotations |
| Gramps Web's own users | what may they do once inside? | roles, below |

The edge gate exists because **Dex does not filter** — no `hostedDomains`, no
allowlist; it brokers Google and Microsoft and will authenticate anyone. The
allowlist is `cluster/oauth2-proxy/emails-family.secret.yaml`, and
`oauth2-proxy-family-rs` is a *second instance* of the family tier purely because
a cookie cannot cross registrable domains (see `docs/auth.md`). Same list, same
Dex client; only the cookie name and domain differ.

The host splits three ways, and each split is load-bearing:

| Path | Gated | Why |
|---|---|---|
| `/` | errors + auth | the app; browsers, so a redirect is right |
| `/api` | **no** | desktop sync authenticates here with username+password |
| `/api/oidc` | auth only | account creation happens here; browser-only |
| `/oauth2` | no | the sign-in dance itself |

`/api` must stay open or sync breaks: `errors` matches on status code and would
rewrite the sync addon's own 401 into a cross-origin redirect to Dex that a
non-browser client cannot follow. `/api/oidc` is gated anyway because that is
where an account gets *created* — and `auth` without `errors`, because the login
page fetches `/api/oidc/config/` as an XHR and a redirect there surfaces as a
confusing CORS error naming Dex.

### Roles

| Role | Adds |
|---|---|
| guest (0) | edit own user |
| member (1) | + view **private** records |
| contributor (2) | + add objects |
| editor (3) | + edit, delete objects, name groups, filters |
| owner (4) | + manage users, import, reindex, repair tree |
| admin (5) | + cross-tree, settings, make-admin, quotas |

Editor includes **delete**, and sync is bidirectional, so a relative deleting a
person propagates to the laptop. Contributor is the line if you want additions
without rewrites.

### Adding a person

1. Add their address to `emails-family.secret.yaml`, `rm` the sealed sibling,
   `nix develop -c ./sign.sh`, commit. (This also admits them to the MCP endpoint.)
2. They sign in with Google/Microsoft at `family.werethemille.rs`.
3. **They will see `Internal Server Error`. This is expected.** New OIDC accounts
   are created with `ROLE_DISABLED` (-1), and the callback then mints a token,
   which calls `get_permissions()` → `PERMISSIONS[-1]`. That dict only covers
   guest..admin, so `auth/__init__.py:425` raises `KeyError: -1`. **The account is
   created regardless.**
4. Promote it, then have them sign in again:

```bash
nix develop -c kubectl -n gramps exec -i acid-gramps-0 -c postgres -- \
  psql -U postgres -d grampswebuser -c \
  "UPDATE users SET role=3, tree='<tree_id>' WHERE name='<email>' AND role=-1;"
```

Registration by username/password is off (`REGISTRATION_DISABLED`), and the
frontend link is hidden separately via `hideRegisterLink` in
`cluster/gramps/frontend-config.yaml` — the API setting does **not** hide the link,
so both are needed or people see an invitation that 405s.

Do **not** set `OIDC_DISABLE_LOCAL_AUTH`: password login is what desktop sync
uses (upstream gramps-web#1267).

Auto-assigning a role instead of promoting by hand is not practical:
`get_role_from_claims()` reads the *userinfo* response, Dex's userinfo carries no
claim with a predictable constant value, and half-configuring it is actively
dangerous — for an existing user the callback passes `role=role_from_claims` to
`modify_user`, so a present-but-unmatched claim **demotes a working admin to
disabled**, who then hits the `KeyError` on every login.

## Desktop sync

The Gramps Web Sync addon syncs a laptop tree with this one, media included, in
both directions.

- **Server and laptop must agree on Gramps MAJOR.MINOR.** The image pins
  `gramps[all]>=6.0.4,<6.1.0` (`GRAMPS_VERSION=60`); the laptop is 6.0.6. A
  `grampsweb` digest bump that moves Gramps to 6.1 breaks sync until the laptop
  follows — which is why Renovate gates that image.
- It authenticates with **username+password** at `/api/token/`, which is why `/api`
  is ungated and why local auth stays enabled.
- It is **not a backup**: a delete on one side deletes on the other.
- **Unresolved:** a sync attempt reported `unexpected errors, child_handles`. The
  server logged nothing, and that string appears nowhere in the addon, in Gramps
  6.0.8, or in the API — so it is client-side. The addon's error dialog has a
  **Details** expander with a copy button; that text is what will identify it.

## The MCP endpoint

`mcp.chrismiller.xyz/gramps/mcp` gives an AI client tool-based access to the tree.
Connect with **no header** — the OAuth flow runs itself:

```bash
claude mcp add --transport http gramps https://mcp.chrismiller.xyz/gramps/mcp
```

Hosted products work too, so relatives can use their own accounts: **Claude** on
all tiers including free; **ChatGPT** via Developer Mode on paid tiers; **Gemini**
via Spark → Connected Apps. All three need Streamable HTTP and a publicly-trusted
cert, which we have. No self-hosted model is involved.

### Why this needs three components

Dex has no dynamic client registration, and an MCP client expects to register
itself. `mcp-gateway` does it *for* Dex over Dex's **gRPC API** (mTLS, port 5557,
`edge-ca`-signed, no Ingress — that API creates and deletes OAuth clients, and
flannel does not enforce NetworkPolicy, so the client certificate is the only real
control). Registrations show up as `kubectl -n dex get oauth2clients`.

Traefik's own MCP middleware does not fit: Hub-only, needs the Hub agent and a SaaS
control plane, and per its docs is only a Resource Server — it validates tokens
issued elsewhere and implements no DCR.

**The gateway authenticates but does not authorize.** Its only check is
`jwt.ParseString(raw, jwt.WithKeySet(jwks))` — no audience, no user check — and Dex
authenticates anyone. Published that way on 2026-09-26, this endpoint made the
family tree world-readable until the route was pulled the next day. So
`images/mcp-jwt-auth` sits in front and requires the token's `email` claim to be on
the family allowlist, mounting the **same Secret** as the browser tiers.

> **Any new MCP path must carry `oauth2-proxy-mcp-jwt-auth@kubernetescrd`.** There
> is no catch-all `/` route, so a new server is simply unreachable until its path
> is added — that is on purpose, so forgetting the gate fails closed.

The bootstrap endpoints stay **ungated** and must: a client reads `/.well-known/*`
and POSTs `/oauth/register` *before* it can hold a token. Metadata is public by
spec, registration only mints a client, and the allowlist still decides whether a
resulting token opens anything.

The MCP server logs into Gramps as `gramps-mcp`, **role 3 (editor)** — family are
editors by decision (2026-09-27), so an assistant can add and change records. Two
consequences follow and neither is avoidable: `view_private` starts at MEMBER, so
an account that can edit can also read records flagged private, and every query
ships what it reads to OpenAI, Anthropic or Google. All MCP users share this one
account, so there are no per-person Gramps permissions on this path — the
allowlist decides *whether*, not *what*.

The server is Chris's patched build of cabout-me/gramps-mcp, from the flake's
`gramps-mcp-image` (built from the derivation in his nixos-configs, so the patches
are not forked into this repo). Upstream v1.1.0 is a year old and his fixes are
still open PRs, and they are what make writing usable: `create_family` silently
dropped children, `get_type(person)` crashed for anyone with notes, gender OTHER
was rejected at validation, and a process-wide httpx client was torn down by
whichever concurrent tool call finished first.

One patch lives HERE instead: `patches/gramps-mcp-transport-security.patch`. The
MCP Python SDK arms DNS-rebinding protection and then allows only localhost, so
every proxied request came back **421 Misdirected Request** — the gateway rewrites
Host to the Service address. It is not settable by environment (FastMCP passes the
field explicitly), and it belongs here rather than in nixos-configs because the
laptop runs the same package over stdio, where the localhost default is right.

## Backups

| What | How | Gap |
|---|---|---|
| Tree, users, search index | `enableLogicalBackup` → nightly `pg_dumpall` to B2 | up to 24 h |
| Media | B2 versioning, 90-day purge of hidden versions | **only copy** — see below |
| Tree directory | *(nothing)* | metadata only; recreatable from `settings.ini` semantics |
| Tree XML | nightly `GET /api/exporters/gramps/file` → `galaxy-family/gramps-exports/<date>.gramps`, 02:30 | date-stamped, never overwritten |

Media bytes exist **only** in B2 plus 90 days of versions; the Postgres dump covers
metadata, not files. A true second copy is an rclone server-side copy of that one
prefix — cheap, same region, not yet done.

The XML export is the undo for a bad edit, which matters now that family are
editors through MCP and sync is bidirectional. It runs as `gramps-backup`, role 1
(MEMBER), and the role is load-bearing rather than tidy: the exporter passes
`view_private=has_permissions({PERM_VIEW_PRIVATE})` (exporters.py:229), so a
guest-role account would produce a backup that silently omits every private
record. MEMBER is the lowest role that reads everything and still cannot write.
The job also refuses to upload anything that is not gzipped XML, so an error page
cannot quietly become that night's backup.

To restore: fetch the dated file from B2 and import it through the Admin page
(Import), or on the desktop with Gramps' own import. XML is the format that
survives a Gramps major-version change, which neither the Postgres dump nor the
media bucket does.

## Gotchas, condensed

| Symptom | Cause |
|---|---|
| `Internal Server Error` on someone's first OIDC login | expected — `KeyError: -1`, account still created, promote it |
| Tree data appearing in SQLite | single-tree mode ignores `NEW_DB_BACKEND`; check `database.txt` |
| `500 "Import failed"` | read the **celery** log; usually the shared cache volume |
| Register link visible though registration is off | `hideRegisterLink` is a separate frontend setting |
| CORS error naming Dex on the login page | `errors` middleware on an XHR path — use `auth` alone |
| MCP client cannot register | the bootstrap paths got gated |
| Sync fails after an image bump | `GRAMPS_VERSION` moved; match the laptop |
| A 422 from the register endpoint | it validates payload and tree *before* checking whether registration is disabled — not proof it is open |
| `421 Misdirected Request` from an MCP server | the SDK's DNS-rebinding check allows only localhost; the gateway sends the Service Host |
| A backup that is missing people | the exporting account lacks `view_private` — private records are dropped silently |

## Commands

```bash
export KUBECONFIG=/tmp/galaxy-kubeconfig

# who exists, and at what role
nix develop -c kubectl -n gramps exec acid-gramps-0 -c postgres -- \
  psql -U postgres -d grampswebuser -Atc "select name, role from users order by role desc;"

# which tree, and is it really Postgres
nix develop -c kubectl -n gramps exec deploy/grampsweb -c grampsweb -- \
  sh -c 'for d in /root/.gramps/grampsdb/*/; do cat "$d/name.txt"; cat "$d/database.txt"; done'

# MCP: who was denied, and why
nix develop -c kubectl -n oauth2-proxy logs -l app.kubernetes.io/name=mcp-jwt-auth --tail=20

# which MCP clients have registered
nix develop -c kubectl -n dex get oauth2clients

# edge regression gate, for ANY change to the hosts above
cd .claude/skills/edge-ingress && nix-shell -p openssl --run ./edge-snapshot.sh
```

## History

Deployed 2026-09-25 to 27. The tree was imported from a `.gramps` XML export (55
people at first import). Notable scars, all of which shaped the above: two empty
SQLite trees from single-tree mode; a `500 "Import failed"` that was a per-pod
`emptyDir`; a 422 on OIDC login from multi-tree mode; a callback 500 that is
upstream's handling of disabled accounts; a PKCE failure caused by the PWA's
service worker racing the document; and the MCP endpoint published with
authentication but no authorization, world-readable for about a day.
