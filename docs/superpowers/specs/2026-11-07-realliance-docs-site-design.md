# Realliance docs site: design

> **Amendment (2026-11-08, builder check).** Chris moved the deploy and its token to an Owners-only
> repository, `realliance-ops/docs-deploy`, and made the recovery page a status page. Claude's rulings
> on the builder's questions fixed the versions, the account id, repository creation, the brand
> script, the wildcard fallback, the first-release content, and dropped the token's IP filter. The text
> below follows them.

- **Date:** 2026-11-07, amended 2026-11-08.
- **Status:** draft for Chris's review. Nothing built.
- **Research:** Claude's platform report and framework spike of 2026-11-07 (session scratchpad, not
  kept), plus one size measurement taken while writing this. Constraints restates what the design
  rests on.
- **Related:** shared-actions spec `2026-11-07-realliance-shared-actions-design.md` (the Vale action and
  job, the pin rule, Renovate on the forge); runner classes spec `2026-11-04-runner-classes-design.md`;
  Pages spec `2026-11-03-realliance-pages-design.md`; `docs/ci.md`; `docs/forgejo.md`;
  `cloudflare/dns/README.md`; the threat model atop `realliance/forgejo`'s `.forgejo/workflows/image.yml`.
- **Markers:** *(Chris decided)* marks a decision Chris made on 2026-11-07 or 2026-11-08. *(Claude's
  call)* marks a default this doc chose; Chris may override any of them.

## Design summary

| Piece | Design | Marker |
|---|---|---|
| Site | Public member docs: Forgejo Actions and runner classes, Pages, the fork's `jj` support, other member operations; a `Mintlify`-like look | *(Chris decided)* |
| Stack, hosting | `Fumadocs` on React Router and `Vite`, built static, not Next.js; Cloudflare Workers static assets, not `git-pages` | *(Chris decided)* |
| Brand | Palette, fonts, logo and favicon from `realliance/brand` at a signed tag *(Chris decided)*; no Google Fonts, checked in CI; Catppuccin code themes *(Claude's call)* | both |
| Source | `realliance/docs` *(Claude's call)*, public; outsiders open fork PRs, a member approves each run. It builds and lints, and holds no secret | *(Chris decided)* |
| Gate | The shared Vale action from the first PR: errors block, warnings print | *(Chris decided)* |
| Deploy | `realliance-ops/docs-deploy`, Owners-only, holds the token. Every 10 minutes, or on dispatch, it builds `realliance/docs` `main` if it changed and deploys it | *(Chris decided)* |
| Name | `docs.realliance.net`, a Workers Custom Domain owned by `cloudflare/dns/` | *(Claude's call)* |
| CI | Every job on `tiny` | *(Claude's call)* |
| Repositories | Both created, and their `main` protected, through OpenTofu in `realliance-ops/forge` | *(Claude's call)* |
| Content | About 25 pages, 11 in the first release, drafted by the builder and reviewed by Chris; prose in component children; `jj` pages beta *(Claude's call)*; recovery as a status page *(Chris decided)* | both |
| Operations | No extra mirror, a laptop `wrangler deploy` as fallback; PR previews as artifacts; hand PRs until Renovate on the forge | *(Claude's call)* |

## Intent

Realliance members need one place that explains how to use the forge. Today that knowledge sits in
operator runbooks in the private `luma-homeops` repository (`docs/ci.md` alone runs to 954 lines), in
design specs, and in Chris's head. Members can't read it, and outsiders who open fork PRs even less. A
public site gives them the member-facing half: how to write a pipeline, pick a runner class, publish a
Page, and work with `jj`.

The site has to stay up when the home servers don't: a page titled "If the forge is down" that goes
down with the forge is worse than none. That rules out `git-pages` and any galaxy pod *(Chris
decided)* and picks Cloudflare's edge, where only a deploy needs the forge. `Fumadocs` gives the
`Mintlify` look with no SaaS and with search in the browser, and its React Router build fits `tiny`
where its Next.js export doesn't. The Realliance brand makes it read as part of the same service as the
forge and the SSO, and the Vale gate holds it to the standard Chris applies to his own writing.

## Goals

1. A member finds how to do a forge task in under a minute, by navigation or search.
2. `docs.realliance.net` serves while the forge, galaxy or the home uplink is down.
3. A reviewed merge to `main` goes live within 15 minutes with no manual step, or at once by dispatch.
4. The site matches the forge and the SSO: same palette in both themes, same fonts, logo and favicon,
   from one brand tag.
5. Nothing merges with a Vale error, and Vale sees every sentence a reader sees.
6. Every CI job fits `tiny` with room to spare, and no member or outsider can reach the deploy token.

## Non-goals

- Pages for members only, sign-in, or private content. Whatever isn't public stays in `luma-homeops`.
- Versioned docs, translations, generated API reference, comments, feedback widgets or analytics.
- External link checks in CI; CI checks internal links only.
- A social preview image per page; the brand's tile logo serves every page *(Claude's call)*.
- A GitHub mirror, `Mintlify`'s hosting, or `git-pages`.

## Anti-goals

Any of these makes the design a failure, even if the site looks right:

- Docs that are down when the forge is down.
- A request from a reader's browser to any third-party origin: fonts, scripts, search or analytics.
- The deploy token where a member can reach it, or in a job that runs the site's build code.
- A deploy, by the pipeline, of anything but reviewed `main`.
- Operator secrets, key stores, account or zone ids, home network names or addresses on a public page.
- Prose that CI can't lint, such as a paragraph in a `description="…"` prop.
- A page whose text needs JavaScript to read.
- A cookie on `docs.realliance.net`, which shares its site with `git` and `sso` (see Credential).

## Constraints

| Fact | The design |
|---|---|
| `tiny` (`docs/ci.md`): 1 CPU, 2 GiB, 15 min; `/work` a 1 GiB memory volume inside the 2 GiB; egress on 80 and 443; `node` 24.21.0 and no `npm` | short jobs; a pinned, checksummed `npm` |
| `tiny` limits (`ci.yaml`): 120 minutes a UTC day per owner by default, one pod at a time across the forge. A check every 10 minutes is 144 launches a day | `realliance-ops` gets a higher daily limit; a tick with no change launches one pod |
| Measured 2026-11-07 on the spike's sources: `npm ci --ignore-scripts` works, with 418 MB of `node_modules` and a 157 MB cache; the build takes 6 s and writes 89 files, 2.2 MB | install scripts off; the cache goes before the build, so `/work` peaks under 0.6 GiB |
| Spike at 1 CPU and 2 GiB: the Next export peaks at 2.0 GiB; React Router and `Vite` at 0.58 GiB | React Router and `Vite` *(Chris decided)*; phase 1 measures again |
| The `Fumadocs` theme toggle sets a `dark` class, default `system`; the brand's `realliance-palette.css` keys on `prefers-color-scheme` | `--color-fd-*` generated from `palette.json`, per class |
| Both scaffolds load Google Fonts | removed; a CI check and the CSP keep them out |
| Vale skips front matter and string props, and lints JSX children | prose in children; a check bans `description=` props |
| Workers static assets: file requests are free and unlimited; 20,000 files, 25 MiB each; `_headers` and `_redirects` apply | an assets-only Worker |
| A Workers Custom Domain brings its own DNS record and certificate, can't sit on a CNAME, and needs the zone in the Worker's account. Changing one takes Editor plus the zone's Workers Routes write, with no per-Worker scope. The zone data source exposes its account's id | the Worker lives in the account that owns `realliance.net`; `cloudflare/dns/` creates the domain with an operator token |
| Editor on one Worker can deploy and roll back, not create, delete or route it; creating takes the product-level `Admin` role | the deploy token can't move the name; Chris's first deploy creates the Worker |
| Forgejo 15: a push or same-repo PR job token writes the whole repository, pushes refs outside `refs/heads/` and `refs/tags/` that no rule protects, and dispatches workflows. A fork PR's token only reads, with no secrets. Members have write on every `realliance` repository and none on `realliance-ops` | the token lives in `realliance-ops`; the residual risk under Credential |
| The inventory's provider, `svalabs/forgejo` 1.7.0, has `forgejo_branch_protection` with status checks, approvals, stale dismissal and rejected-review blocks, and no tag-protection resource | branch rules in `repos.tf`; tag rules set by an Owner and asserted by the SSO tests |

## Repositories

**`realliance/docs`**, public, created in `repos.tf` like `realliance/forgejo-upstream`:

| Path | Holds |
|---|---|
| `content/docs/<section>/*.mdx`, `meta.json` | the pages, by the outline's sections; `meta.json` sets order and titles |
| `app/` | the React Router app: root, routes, layout, the MDX component map, the search dialog |
| `app/app.css`, `app/brand.css` | Tailwind and the `Fumadocs` preset; `brand.css`, which `sync-brand.sh` writes from `brand/palette.json` |
| `brand/palette.json`, `public/brand/`, `brand.lock.json`, `keys/`, `operators.yml` | brand files with their hashed names and licences; the release in use; the operators' keys and fingerprints its tag verifies against |
| `public/_headers`, `public/_redirects` | response headers; redirects for moved pages |
| `wrangler.jsonc` | for `wrangler dev` previews only, kept equal to `docs-deploy`'s by hand |
| `package.json`, `package-lock.json`, `tool.lock` | exact versions (below); the `npm` release, URL and `sha512`, until the shared `fetch-tool` action exists |
| `scripts/` | `sync-brand.sh`, `fetch-npm.sh`, `check-content.sh`, `check-build.sh`, `lint-docs` |
| `.forgejo/workflows/site.yml`, `.vale/vocab/` | the `vale` and `build` jobs; the repository's own words, per the shared-actions spec |

**`realliance-ops/docs-deploy`**, public like the bots, Owners-only, created in `repos.tf`:

| Path | Holds |
|---|---|
| `.forgejo/workflows/deploy.yml` | the `check`, `build` and `deploy` jobs |
| `wrangler.jsonc` | the Worker's production shape, so no member can add a script or a route |
| `package.json`, `package-lock.json` | `wrangler` alone, pinned exactly |
| `scripts/check-output.sh` | the output checks, an Owner-held copy of those in `check-build.sh` |
| `README.md` | what it does; the laptop fallback; how to hold a rollback |

Pages sit at the root with no `/docs` prefix, and the Welcome page is `/`. Each page links "Edit on the
forge" to its source, in place of the scaffold's upstream link *(Claude's call)*.

**Versions** *(Claude's call)*. The spike's sources aren't kept, so these exact top-level versions are
the record: `fumadocs-core` 16.18.1, `fumadocs-mdx` 15.5.0, `fumadocs-ui` as
`npm:@fumadocs/base-ui@16.18.1`; `react-router`, `@react-router/node` and `@react-router/dev` 8.5.1;
`react` and `react-dom` 19.3.1; `vite` 8.3.6; `tailwindcss` and `@tailwindcss/vite` 4.3.5;
`typescript` 7.0.2; `lucide-react` 1.70.0. No `takumi-js`, since social images are out. `wrangler` is
the newest release at least 14 days old; both repositories pin that one exactly.

## CI and deploy

Every `run` step blanks `GITHUB_TOKEN`, `FORGEJO_TOKEN` and `ACTIONS_RUNTIME_TOKEN` in its own `env:`,
and every job checks out anonymously, as `image.yml` does. No `actions/checkout`,
`pull_request_target`, `always()` or `continue-on-error`.

**`realliance/docs`** runs `site.yml` on `pull_request` and on `push` to `main`. It deploys nothing.

| Job | Does | Holds |
|---|---|---|
| `vale` | the shared-actions spec's job, `paths: content README.md CONTRIBUTING.md` | the job token, cleared by the action |
| `build` | fetch `npm` by `tool.lock`; `npm ci --ignore-scripts`, cache under `$RUNNER_TEMP`, deleted after; `npm run build`; both checks; upload `build/client` as artifact `site` | the job token, in the upload step only |

**`realliance-ops/docs-deploy`** runs `deploy.yml`:

```yaml
on:
  schedule: [{cron: "*/10 * * * *"}]
  workflow_dispatch: {}
concurrency: {group: docs-deploy, cancel-in-progress: false}
```

| Job | Does | Holds |
|---|---|---|
| `check` | `git ls-remote` of `realliance/docs` `main`, anonymously; read the live `https://docs.realliance.net/build.json`; output the `main` commit, and `changed` when it differs from the served one or the run is a dispatch. Installs nothing | no secret |
| `build` | `if: changed`. Fetch `realliance/docs` at that commit, anonymously; `npm` by its `tool.lock`; `npm ci --ignore-scripts` from its committed `package-lock.json`; `npm run build`; write `build.json` with the commit; `scripts/check-output.sh`; tar the output, output its `sha256`; upload it | the job token, in the upload step only |
| `deploy` | `needs: [check, build]`. Download the tar, match its `sha256`; `npm ci --ignore-scripts` of this repository's `wrangler`; deploy | the secret, last step only |

```yaml
      - name: Deploy           # the repository's only secret reference
        env:
          GITHUB_TOKEN: ""
          FORGEJO_TOKEN: ""
          ACTIONS_RUNTIME_TOKEN: ""
          CLOUDFLARE_ACCOUNT_ID: ${{ vars.CLOUDFLARE_ACCOUNT_ID }}
          CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_DEPLOY_TOKEN }}
          COMMIT: ${{ needs.check.outputs.commit }}
        run: node_modules/.bin/wrangler deploy --tag "$COMMIT" --message "$COMMIT"
```

Each deployment's tag is its `main` commit, and `build.json` serves the same commit, so `check` needs
no secret and no install. A tick with no change launches one short pod; the concurrency group keeps
ticks from overlapping.

**Checks.** In `realliance/docs`, `check-content.sh` fails on a `description=` prop, and on front matter
`description` over 160 characters, which Vale skips. `check-build.sh` and `docs-deploy`'s
`check-output.sh` fail on `googleapis` or `gstatic` in the output; an `http(s)://` URL in a `<link>`,
`<script>` or CSS `url()`; a broken internal link; a missing `404.html`; a page whose HTML lacks its
text; and the Workers file limits. `check-build.sh` also fails when `app/brand.css` is out of step with
`palette.json`. A change to the docs copy can't loosen the deploy job's checks.

**PR previews.** A reviewer downloads `site` and runs `npm run preview`, which serves it through
`wrangler dev` with production's headers, redirects, and 404s.

**Worker.** `docs-deploy`'s `wrangler.jsonc`, assets only, and no routes, since `cloudflare/dns/` owns
the name:

```jsonc
{ "name": "realliance-docs", "compatibility_date": "2026-11-07",
  "workers_dev": false, "preview_urls": false,
  "assets": { "directory": "./site", "not_found_handling": "404-page",
              "html_handling": "auto-trailing-slash" } }
```

`_headers` sets `nosniff`, a referrer policy, HSTS without `includeSubDomains`, and a CSP on
`default-src 'self'` and `frame-ancestors 'none'` that never names another origin. Hashed files under
`/assets/` and `/brand/` cache for a year; `build.json` doesn't cache. Nothing sets a cookie.

**DNS.** `cloudflare/dns/workers.tf` *(Claude's call)*. The account id comes from the zone, with no new
variable or secret:

```hcl
data "cloudflare_zone" "realliance_net" {
  zone_id = local.zones["realliance.net"].id
}

resource "cloudflare_workers_custom_domain" "docs" {
  provider   = cloudflare.workers
  account_id = data.cloudflare_zone.realliance_net.account.id
  zone_id    = local.zones["realliance.net"].id
  hostname   = "docs.realliance.net"
  service    = "realliance-docs"
}
```

A custom domain, not an explicit record: it brings its own DNS record and certificate, which an
explicit `docs` record would only block, and its record leaves `plan` empty since the provider manages
what the module declares. The exact name beats the `*.realliance.net` wildcard; every other name still
reaches Traefik. If Cloudflare refuses the custom domain over the wildcard, the fallback is
pre-approved: a `proxied = true` `AAAA` record for `docs` at `100::` in `records.tf` plus a
`cloudflare_workers_route`, both in Terraform. The `cloudflare.workers` alias reads a second token,
kept in its own git-crypt file that `tf.sh` exports and the cluster never gets. It holds Workers Routes
write on `realliance.net` and Workers Editor for the account. The cluster's DNS token stays unchanged.

**Forge down.** The site keeps serving; ticks fail at `check` and deploy nothing. For an urgent change,
Chris builds from clones of both repositories and runs `wrangler deploy` from `docs-deploy` under his
own login. To hold a laptop deploy or a `wrangler rollback`, an Owner first disables `deploy.yml`, or
the next tick redeploys `main`.

## Credential

| Item | Design *(Claude's call)* |
|---|---|
| Token | account-owned; "Specified Workers" = `realliance-docs`, role Editor; no zone permissions; a year's expiry. No IP filter: the WAN is DHCP, and a filter stops no misuse from a CI job. Chris makes it (a rollout step) |
| Stored | Actions secret `CLOUDFLARE_DEPLOY_TOKEN` on `realliance-ops/docs-deploy`; the account id in variable `CLOUDFLARE_ACCOUNT_ID` there *(Chris decided)* |
| Read by | `docs-deploy`'s `deploy` job, last step. `realliance/docs` holds no secret |
| Rotated | at expiry, and when an Owner changes *(Chris decided)*: `docs/forgejo.md`'s Owner offboarding gains it |

**Protection** *(Claude's call)*, in `repos.tf` where the provider allows:

| Repository, rule | Setting |
|---|---|
| `realliance/docs` `main` | no pushes, PR merges only; one approval, stale approvals dismissed, blocked on a rejected review; status checks `vale` and `build` |
| `realliance/docs` tags | `*` for Owners and maintainers, set by an Owner through the API |
| `realliance-ops/docs-deploy` `main` | as for `forge-bot`: PRs only, signed commits, Owners approve and merge, fast-forward only |
| Detection | the SSO repository's `tests/forge/`: assertions for both `main` rules and the docs tag rule; `docs-deploy` joins `OPS_REPOS`; `test_inventory_declares_no_realliance_repo` widens to the `realliance` repositories the inventory now declares |

The pipeline thus deploys `main` alone, and `main` changes only through a reviewed PR that passed Vale
and the build.

**Residual risk.** Members no longer reach the token: they have no write on `realliance-ops`, and
nothing in `realliance/docs` sees it. What remains:

1. **Who can read it:** Owners, and site administrators, the administrator bots included.
2. **The deploy runs reviewed `main`.** `docs-deploy` builds `realliance/docs` at `main`, with its
   scripts and every npm package its `package-lock.json` names, in an Owners-only context. On Forgejo
   15 the build job's upload step holds `docs-deploy`'s write-capable job token after that code has
   run, so hostile code there could push a ref with its own workflow to `docs-deploy`, dispatch it,
   and read the token. The trust rests on review of `main`: a PR that adds or bumps a package, or
   changes `scripts/` or the build setup, deserves the care of one that changes `.forgejo/`. Exact pins
   and the lock file's integrity hashes keep a pinned version from changing, and `--ignore-scripts`
   and the step blanks stop install hooks and code that reads its environment. This is `image.yml`'s
   accepted risk, now reachable only through reviewed `main`.
3. **The token's reach:** anything on `docs.realliance.net` until an Owner notices. That name shares its
   site with `git` and `sso`, so a hostile deploy can phish under a Realliance name and set
   `Domain=realliance.net` cookies that both receive. It can't change DNS, the custom domain or the
   fallback route, reach another Worker, or delete this one.
4. **Recovery:** an Owner disables `deploy.yml`, runs `wrangler rollback`, revokes the token, and looks
   in `wrangler deployments list` for a version whose tag isn't a `main` commit. A revert on `main`
   then lets the schedule resume.

## Brand

The docs pin **`v0.1.0`**, a signed tag and the only one (2026-11-07); the forge and the SSO pin it
too. `scripts/sync-brand.sh <tag>` copies `realliance-sso/scripts/sync-brand.sh`, with its file map and
paths changed for this site. It verifies the tag's signature against the operators' fingerprints in
`operators.yml`, using their public keys in `keys/` (exported from `https://git.realliance.net/<user>.gpg`),
checks each file against `dist/manifest.json`, copies it under its hashed name, writes `app/brand.css`,
and records the release and signer in `brand.lock.json`. A change to `keys/` or `operators.yml` goes
through the same review as any PR.

| From `dist/` | Used for |
|---|---|
| `css/realliance-fonts.<hash>.css`, `fonts/*.woff2`, `fonts/OFL-*.txt` | Atkinson Hyperlegible body, Fredoka headings, Noto Sans Mono code, through Tailwind's `--font-sans` and `--font-mono` and a heading rule |
| `img/mark-wide-latte`, `img/mark-wide-mocha` | the header logo, one per theme class. Not `mark-wide-auto`, which follows the OS and would ignore the toggle |
| `img/icon.svg`, `img/icon-32.png`, `img/apple-touch-icon-180.png`; `img/logo-tile-512.png` | favicons; the social preview image |
| `palette.json` | the input for `app/brand.css`, not shipped |

`realliance-palette.css` stays unused, since it ignores the class toggle. A first visit still follows
the OS, as the brand intends, until the reader toggles. The `Fumadocs` preset `catppuccin.css` is the
base; `app/brand.css` overrides every token, Latte values on `:root` and Mocha values on `.dark`:

| `--color-fd-*` | Palette key |
|---|---|
| `background` | `bg` |
| `foreground` and the `*-foreground` tokens of `card`, `popover`, `secondary` and `accent` | `text` |
| `muted-foreground` | `text-muted` |
| `card`, `popover` | `surface` |
| `muted`, `secondary`, `accent`, and the `#nd-sidebar` overrides | `surface-hover` |
| `border` | `border` |
| `primary`, `ring`; `primary-foreground` | `primary`; `primary-contrast` |
| `info`, `success`, `warning`, `error` | the keys of the same names |
| `idea` | `warning`, since `spark` falls under 3:1 on Latte |

The `Fumadocs` `accent` is a hover background, so it takes `surface-hover`, not the brand's `accent`.
Code blocks use `Shiki`'s `catppuccin-latte` and `catppuccin-mocha`, switched by the same class.

## Search

`Orama` in static mode. The build indexes each page's headings and text into one static file. The
search dialog (`Ctrl+K`) fetches it from the same origin on first open and runs queries in the browser,
so no query leaves the reader's machine and search works with the forge down. The first release's
index should stay under 200 KB.

## Content

The research outline, about 25 pages. **v1** marks the 11 in the first release:

| Section | Pages |
|---|---|
| Start | **v1** Welcome and how Realliance works; **v1** Sign in with SSO; Your first repository |
| Forge | Teams and access; Mirrors and visibility; Mail notifications |
| Forgejo Actions | **v1** Pipelines in 10 minutes (syntax, triggers, secrets, full-URL actions, anonymous checkout); Reusable workflows and actions; Debugging a failed run |
| Runner classes | **v1** Choosing `tiny`, `small` or `medium`; **v1** Limits, minutes and why a job waits (holds, the Usage tab); Labels that hold a job |
| Pages | **v1** Publish a site; **v1** URL, naming, size, headers and redirects; Takedown and troubleshooting |
| Shared actions | `pages-publish` reference; Pinning and versioning, after `v1.1.0` |
| `jj` (beta) | **v1** Using the forge with `jj` (change ids, conflict labels, the `jj` pill); **v1** Reviewing a `jj` PR round by round |
| Recovery | **v1** If the forge is down: what's down, what keeps working, and that operators are on it |
| Contributing | **v1** Writing these docs: style, Vale, opening a PR |

The builder drafts the v1 pages from the `luma-homeops` runbooks and specs, within the table below,
and Chris reviews them before launch.

| On the site | Stays in `luma-homeops` |
|---|---|
| What a member does and sees: sign-in, repositories, workflows, classes, Pages, `jj` | How operators run it: break-glass, backup and restore, rotation, Azure, the sweeper and ledger, alerts, the Pages relay, bumps, the fork image |
| Limits a member meets: class shapes, daily `tiny` minutes, the default paid limit, the Pages cap; that a Realliance-wide cap exists | Global cap values, ceilings, backstops; threat analyses and residual risks |
| During an outage: what's down, and that operators are on it | Contact routes, restore steps, secret names and locations, key stores, account and zone ids, tokens |
| "Realliance's own hardware" and "the cloud" | Node names, addresses, the LAN, the WAN address |

**Recovery** is a status page *(Chris decided)*: what stops when the forge is down (Git hosting, CI,
Pages publishing, the registry), what keeps working (this site, members' own clones), and that
operators are on it. It names no contact channel and no restore steps.

**`jj` pages** carry "Beta" in their sidebar title and a warning box at the top: the fork's `jj`
features may change at the 19 LTS rebase. Rebase merges and divergent changes wait for the fork.

**Components.** The MDX map registers `Card`, `Tabs`, `Steps`, `Callout` and `Accordion`. Prose goes in
children, as in `<Card title="Publish a site" href="/pages/publish/">Push a pages branch…</Card>`;
titles and tab labels stay short props.

## Rollout

| Phase | Delivers | Done when |
|---|---|---|
| 0 | the shared-actions spec's phase 1: `vale` at `v1.1.0` | its own done line |
| 1 | first, the build measured again under a 1 CPU, 2 GiB `cgroup` and in a real `tiny` run. Then both repositories and their `main` rules in `repos.tf`, the docs tag rule, the SSO test changes, the skeleton site, brand sync, the `vale` and `build` jobs | the build peaks under 1.5 GiB, or the work stops and reports; an error blocks a merge; a fork PR waits for approval; `site` uploads |
| 2 | the Worker, `workers.tf`, the tokens, `docs-deploy`'s jobs, the `ci.yaml` limit, lines in `docs/forgejo.md` and `CLAUDE.md` | a merge goes live by the schedule and by dispatch; `plan` is empty |
| 3 | the 11 first-release pages, real content, reviewed by Chris | the acceptance checks pass |
| later | the other pages; Renovate pins once the forge's bot runs | |

Phase 2 runs in this order; steps 1 to 4 are Chris's:

1. From a laptop, `wrangler login`, then `wrangler deploy` of a local build from `docs-deploy`. This
   creates the Worker and doubles as the first fallback drill.
2. Create the Terraform workers token and its git-crypt file; `tf.sh plan`, `apply`, then `plan` again,
   empty.
3. Create the deploy token from the Credential table; add the secret and the variable to
   `realliance-ops/docs-deploy`.
4. Raise `realliance-ops`'s `tiny_minutes_per_day` in `ci.yaml` with an Owner-signed commit, to cover
   144 checks a day and the builds, about 300.
5. Enable `deploy.yml`, merge a docs PR, and watch the next tick put it live.

## Acceptance

1. `https://docs.realliance.net/` serves with a valid certificate from Cloudflare's edge, and
   `tf.sh plan` is empty. `www`, `git` and `sso.realliance.net` still resolve to the WAN address.
2. A first visit follows the OS theme. The toggle switches palette, logo and code theme together, and
   each `--color-fd-*` value equals its `palette.json` source.
3. Loading any page and running a search makes no request to another origin.
4. Search finds a word that appears only in a page's body.
5. A Vale error blocks a merge; warnings alone don't. A fork PR's run waits for a member's approval.
   `realliance/docs` has no Actions secret, and a member can't push to `realliance-ops/docs-deploy`.
6. A merge to `main` is live within 15 minutes, and a dispatch deploys at once. `build.json` and the
   deployment's tag both show the commit. A tick with no change launches `check` alone.
7. The deploy token can't add a route or a custom domain: a `wrangler deploy` that names one fails.
   This holds with the custom domain or with the fallback route, since Terraform owns both.
8. Each `build` and `deploy` finishes within 5 minutes on `tiny`, with `/work` under 0.8 GiB; a `check`
   finishes within a minute.
9. An unknown path returns 404 with the branded page. No response sets a cookie, and every header in
   `_headers` arrives.
10. Every page reads with JavaScript off, client navigation works with it on, and the layout holds at
    360 pixels wide with no sideways scroll.
11. A text contrast check passes on both themes, code blocks included.

## Left to the builder

- **`Fumadocs` on React Router:** a `404.html` rendered at build time; whether Workers serves the `.data`
  files and the search index with types the client accepts (else rename the index `/search.json`);
  removing the image renderer; keeping `llms.txt` or not; the pinned version's `--color-fd-*` set.
- **Contrast:** if Latte's stock comment and yellow code tokens fall under 4.5:1, override those two.
- **`tiny`:** the `tool.lock` format, later `fetch-tool`; `realliance-ops`'s owner id for `ci.yaml`.
- **Wrangler:** that a deploy without `routes` leaves the custom domain alone; that `--tag` takes a full
  commit id (else a prefix, with the full id in `--message`); that `--ignore-scripts` installs a working
  `wrangler`; that the Editor token can keep `workers_dev` off.
- **Cloudflare:** the Free plan's certificate; that the default provider can read the zone data source;
  that `check` reads a fresh `build.json`, not a cached one.
- **Forgejo 15:** `schedule` triggers and their timing; that a job skipped by `if:` launches no pod; the
  `vars` context; workflow `concurrency`; where Actions artifacts live (on B2, one download a deploy);
  whether repository administrators bypass `main` protection, and the setting that stops them; check
  names and protected file patterns, per the shared-actions spec.
- **The CSP** that hydration and the theme script need.
