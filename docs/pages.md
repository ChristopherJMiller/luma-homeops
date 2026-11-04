# Realliance pages

Static hosting for the Realliance forge, like GitHub Pages. Push a `pages`
branch, get a site. Namespace `pages`, Argo app `pages`, config in
`cluster/pages/`. Plan: `docs/superpowers/plans/2026-11-03-realliance-pages.md`
(ledger beside it). Acceptance suite: `realliance/sso` `tests/forge/test_forge_pages.py`.

| Piece | What it does |
|---|---|
| Forgejo system webhook | Sends `push`, `delete` and `repository` events for the `pages` branch to the relay |
| `pages-relay` (`images/pages-relay`) | Checks the event, queues it, tells git-pages to fetch the repo |
| `git-pages` 0.10.1 | Clones the branch, stores the site in B2 (`realliance-pages`), serves it on `:3000` |
| Traefik `pages-sites`, `pages-apex` | Public GET and HEAD only (`cluster/pages/edge.yaml`) |

## Publishing

| What | Rule |
|---|---|
| Site URL | `https://<owner>.realliance.page/<repo>/`, all lower case |
| Root site | The `<owner>/pages` repo publishes `https://<owner>.realliance.page/` |
| Branch | `pages` only, for project and root sites |
| Which repos | Public repos in public owners. Private, internal and hidden-org repos publish nothing |
| New repos | The forge makes them private. Make the repo public first |
| Forks | Publish like any repo |
| Archived repos | Keep their last site until an Owner takes it down |
| Rename or transfer | Publishes under the new name on the next push. Forgejo 15 sends no event for the move, and the old URL stays until takedown |
| Pull mirrors | Publish on every sync that moves `pages` (checked in the Task 10 drill) |

**Names that get no site.**
- An owner that is not one valid DNS label (dots, underscores, over 63 characters) and the owner `www`.
- Any path containing `.git-pages`, in any case or encoding, including a repo whose name contains it. Traefik answers 404.
- `/.git-pages/health` works for the root site only. Traefik blocks a project site's health on purpose.
- A trailing-dot host (`<owner>.realliance.page.`) returns 404.
- `realliance.page` and `www.realliance.page` redirect to `https://realliance.realliance.page/` (302 for GET, 307 for other methods, path kept).
- Any method except GET and HEAD returns 404 from Traefik. Nothing can upload.

**Site files.** `_headers` may set only these: `Content-Security-Policy`, `Cross-Origin-Embedder-Policy`, `Cross-Origin-Opener-Policy`, `Cross-Origin-Resource-Policy`, `Access-Control-Allow-Origin`, `Permissions-Policy`, `X-Frame-Options`, `X-Robots-Tag`. `_redirects` and `404.html` work as in git-pages. The edge strips `Set-Cookie` and always sends `nosniff`, a strict referrer policy and HSTS without `includeSubDomains`.

**Latency.** A push is live in about 6 s (the Task 10 drill). git-pages caches a site for 60 s, so poll for a minute before you call it broken.

### Sample workflow (Forgejo Actions)

The automatic token can push `pages`. The Task 10 drill proved it. Pass it through `GIT_CONFIG_*` env so it never appears in argv.

```yaml
name: pages
on:
  push:
    branches: [main]
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build
        run: ./build.sh   # writes the site to ./public
      - name: Push to pages
        env:
          GIT_CONFIG_COUNT: "1"
          GIT_CONFIG_KEY_0: http.extraHeader
          GIT_CONFIG_VALUE_0: "Authorization: Bearer ${{ github.token }}"
        run: |
          cd public
          git init -q -b pages
          git config user.name  "pages bot"
          git config user.email "pages@users.noreply.git.realliance.net"
          git add -A
          git commit -q -m "publish ${GITHUB_SHA}"
          git push -f "${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}.git" pages
```

### Sample workflow (GitHub Actions, forge as the target)

Store a Forgejo access token as the GitHub secret `FORGE_TOKEN`. Forgejo tokens are not scoped to one repo, so use a bot user that has write access to that repo only.

```yaml
      - name: Push to pages
        env:
          GIT_CONFIG_COUNT: "1"
          GIT_CONFIG_KEY_0: http.extraHeader
          GIT_CONFIG_VALUE_0: "Authorization: token ${{ secrets.FORGE_TOKEN }}"
        run: |
          cd public
          git init -q -b pages
          git add -A
          git -c user.name=ci -c user.email=ci@example.com commit -q -m "publish ${GITHUB_SHA}"
          git push -f https://git.realliance.net/<owner>/<repo>.git pages
```

## Private-repo opt-in

Pages never reads a private repo. Have CI push the built site to a separate public repo (`<name>-pages`, branch `pages`). The site appears at `<owner>.realliance.page/<name>-pages/`. Do not put source in that repo.

## Quota

`max-site-size` is a hard cap at 128 MB per site.
- git-pages refuses a publish over the cap, and the previous version keeps serving.
- The delivery still shows `202` in Forgejo, because git-pages answers before it fetches.
- Only `PagesSiteUpdateFailing` reports it. The relay never sees an asynchronous refusal, so `PagesRelayUpstreamError` does not fire.
- Fix: shrink the site, then push again.

## Takedown

Deleting the `pages` branch or the repo does not unpublish. Only an Owner can, with the git-pages command-line tool.

1. Delete (project site, then root site):
   ```bash
   kubectl -n pages exec deploy/git-pages -- git-pages -config /etc/git-pages/config.toml -delete-site <owner>.realliance.page/<repo>
   kubectl -n pages exec deploy/git-pages -- git-pages -config /etc/git-pages/config.toml -delete-site <owner>.realliance.page/.index
   ```
   `-delete-site <domain>` alone panics in 0.10.1 ("nil project name"). A root site is `<domain>/.index`.
2. To stop republishing, `-freeze-domain <owner>.realliance.page`. Freeze after the delete: a frozen domain refuses deletes. A push to a frozen domain counts as `upstream_error`.
3. `-unfreeze-domain <owner>.realliance.page` undoes it.
4. The public edge returns 404 within about a minute (site cache).

`PagesTakedownRequested` fires 6 hours after a delete event for a repo that had a site. The relay logs the site for public repos. A private repo's request carries no names, so list sites with the git-pages listing command. Making a repo private sends no event at all.

## Flows

For the NetworkPolicy plan (N1).

| Direction | Flow |
|---|---|
| In | Traefik to `git-pages:3000` |
| In | Prometheus to `git-pages:3002` and `pages-relay:8080` |
| In | Namespace `forgejo` to `pages-relay:8080` (hook URL `http://pages-relay.pages.svc.cluster.local:8080/hook`) |
| In | `pages-relay` to `git-pages:3000` (forward and health) and `:3002` (the updates counter) |
| Out | DNS |
| Out | `git-pages` to `152.44.247.88:443` (git.realliance.net through the router hairpin) and B2 `:443` |

NetworkPolicy is not enforced yet. Without the relay to `:3002` rule, holds fall back to health polling and the deadline.

The WAN-IP hairpin is a dependency: if the WAN address changes and `cloudflareddns` lags, git-pages cannot clone until DNS catches up.

## The relay

`pages-relay` receives the system hook and answers fast, because Forgejo 15 never retries a delivery.

1. **Accept, then queue.** It verifies the HMAC, parses the event and answers `202`. Forwarding to git-pages happens later.
2. **Coalesce per site.** Pushes to one site while it stays busy collapse into one. The latest wins, and a site never has two updates at once.
3. **Hold after git-pages answers 202.** git-pages answers `202` when an update takes over 3 s. The relay holds that site until the update ends, so the next push cannot overlap. It sees the end as a change in the site's health `Last-Modified`. The `git_pages_site_updates` counter and a git-pages restart also end a hold. The deadline is 5 min. Polling stays at about 1 request a second.
4. **Aborts.** If git-pages restarts mid-update, the relay re-queues the push once (`aborted`). If that retry dies the same way, nothing forwards it again (`aborted_lost`): see Lost pushes.
5. **Shutdown.** `preStop` sleeps 5 s, then SIGTERM drains the queue for up to 110 s. The relay logs each push it could not forward as `not forwarded at shutdown` (`may not have been forwarded at shutdown` if the push was in flight).
6. **Limits.** Bodies up to 16 MiB (`too_large`, 413). A new site gets `busy` (503) only when 64 other sites already wait, or both body slots stay busy for 3 s. The relay trusts its ledger of forwarded updates only about 8 min after a start, so a failed update in that window holds its site for the full 5 min.

**Metrics** (port 8080):
- `pages_relay_events_total{outcome}`: `queued`, `coalesced`, `forwarded`, `upstream_error`, `too_large`, `busy`, `bad_request`, `bad_signature`, `unpublish_requested`, `ignored_event`, `ignored_name`, `ignored_no_site`, `ignored_private`, `ignored_ref`.
- `pages_relay_holds_total{outcome}`: `completed`, `timeout`, `released`, `aborted`, `aborted_lost`. Every `202` ends in exactly one.
- `pages_relay_sites_held`, `pages_relay_queue_depth`.

**Logs.** `hold` and `hold ended` (with `evidence`: `health`, `updates_counter`, `git_pages_restarted` or `shutdown`). The relay never names private or hidden repos in logs or labels. A request for a site with a push already queued, in flight or held logs `check: publish_in_progress`.

The apex and `www` route to `noop@internal`, so Traefik's access log and metrics do not show them.

## Alerts

All in `cluster/pages/monitoring.yaml` (`pages-alerts`), all warning. Except `PagesDown`, none has a `for:`.

| Alert | Meaning | Do |
|---|---|---|
| `PagesDown` | git-pages or the relay is not scraped for 10 min | `kubectl -n pages get pods`, read the logs |
| `PagesSiteUpdateFailing` | git-pages refused or failed a publish; the site keeps its last content. No site label | Check the 128 MB cap first. Find the site in `kubectl -n pages logs deploy/git-pages` |
| `PagesRelayUpstreamError` | A queued forward found git-pages unreachable or got a non-2xx answer | Check both logs, then redeliver (see Lost pushes) |
| `PagesBadSignatures` | HMAC mismatch | The secret in the hook and in `pages-relay-webhook` differ, or someone posts to the relay. A user can point a repo hook at the relay and land here too |
| `PagesRelayRejecting` | `too_large`, `busy` or `bad_request` | The push did not publish. Redeliver |
| `PagesTakedownRequested` | Someone deleted the branch or repo of a published site | Take the site down (see Takedown) |
| `PagesRelayQueueStuck` | Pushes wait 15 min and no worker has tried git-pages | `kubectl -n pages rollout restart deploy/pages-relay`, then redeliver what the shutdown log lists |
| `PagesRelayPushLost` | The retry of an aborted push also died (`aborted_lost`) | See Lost pushes |

`PagesRelayQueueStuck` and `PagesRelayPushLost` ship with the relay re-pin (`sha-0273bb3`). They are not live until that commit is on `main`.

## Lost pushes

Forgejo 15 does not retry a delivery, and the relay answers `202` before it forwards. A push can get a `202` and still not publish. The signals are `PagesRelayUpstreamError`, `PagesRelayRejecting`, `PagesRelayQueueStuck` and `PagesRelayPushLost`.

Redeliver it from Forgejo's own delivery history:
1. Sign in as a Forgejo administrator. Open Site Administration, then Integrations, then System Webhooks, and open the Pages hook.
2. Find the delivery for the repo and push time. `kubectl -n pages logs deploy/pages-relay | grep aborted_lost` names the site for public repos.
3. Press Redeliver. The relay treats it as a new push, and git-pages fetches the current head of `pages`.
4. Watch the site: the relay logs `hold ended`, and the page shows the new content within about a minute.

The push is not gone for good: the `pages` branch holds the truth. If the delivery has aged out, push an empty commit to `pages`.

## Operations

- **System hook.** One Forgejo system webhook points at the relay. `ensure_system_hook()` in the acceptance suite re-checks it and refuses to create one, because the administrator API only makes default hooks. To recreate a missing hook, an Owner runs the guarded database flip in the ledger (Tasks 6 and 11), then re-runs the suite. This needs `kubectl` access to the Forgejo database. Re-check it on the Forgejo 17 upgrade.
- **Rotate the secret.** Edit `PAGES_WEBHOOK_SECRET` in sso-secrets `forge/secrets.env` and in `cluster/pages/pages-relay-webhook.secret.yaml`, reseal with `./sign.sh`, update the hook, and re-run the suite.
- **B2.** The bucket key cannot delete versions. Hidden versions purge after 1 day. Unreachable blobs are never collected; `git-pages -trace-garbage` estimates them.
- **Sizing.** git-pages: request 64Mi, limit 1Gi. A 140 MiB refused push peaked at 22 MiB; idle is 11 MiB. Relay: request 32Mi, limit 128Mi, `GOMEMLIMIT=100MiB`.
- **Backups.** The `pages` branches in Forgejo hold the truth, and Forgejo has its own backups (`docs/backups.md`). B2 holds only derived state. To rebuild, push each `pages` branch again.

## Bumping git-pages

Renovate never merges git-pages or the relay by itself. To bump:
1. Read every changelog between the pinned version and the target. Look for config key renames (0.10 renamed `caddy` to `proxy`) and changes to health, `-delete-site` and the update path.
2. Take only a release at least 2 weeks old, unless Chris waives it for a security fix.
3. Update the `tag@sha256` pin (read the digest from the Codeberg registry).
4. Re-run the acceptance suite and the takedown steps in the Takedown section.
5. The relay depends on git-pages behavior (the `202` after 3 s, health `Last-Modified`, the updates counter). `images/pages-relay` has a contract test against the real binary: run it with `PAGES_RELAY_GIT_PAGES` pointing at the new build.

The relay image pin is `sha-<7>@sha256:<64>`. A merged Renovate PR for its base image deploys nothing until you re-pin.

## Public suffix list

`realliance.page` is not on the Public Suffix List yet. Until it joins, browsers treat every owner's subdomain as the same site. The edge strips `Set-Cookie` and HSTS has no `includeSubDomains`, but a script on one owner's site can still set a cookie for the whole domain with `document.cookie`, and other owners' sites would receive it. Do not rely on cookies on any Pages site.

Plan Task 13 is waiting on Chris to extend the domain registration to two years or more. The PSL asks for that. After that: pre-submission checks, Chris opens the pull request, then the `_psl` TXT record goes into `cloudflare/dns/records.tf`. Keep that record once it exists. Do not add any other TXT record on the apex.
