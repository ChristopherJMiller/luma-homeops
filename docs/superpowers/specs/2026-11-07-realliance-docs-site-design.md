# Realliance docs site: design

- **Date:** 2026-11-07.
- **Status:** draft for Chris's review. Nothing built.
- **Research:** Claude's platform report and framework spike of 2026-11-07 (session scratchpad, not
  kept), plus one size measurement taken while writing this. Constraints restates what the design
  rests on.
- **Related:** shared-actions spec `2026-11-07-realliance-shared-actions-design.md` (the Vale action and
  job, the pin rule, Renovate on the forge); runner classes spec `2026-11-04-runner-classes-design.md`;
  Pages spec `2026-11-03-realliance-pages-design.md`; `docs/ci.md`; `docs/forgejo.md`;
  `cloudflare/dns/README.md`; the threat model atop `realliance/forgejo`'s `.forgejo/workflows/image.yml`.
- **Markers:** *(Chris decided)* marks a decision Chris made on 2026-11-07. *(Claude's call)* marks a
  default this doc chose; Chris may override any of them.

## Design summary

| Piece | Design | Marker |
|---|---|---|
| Site | Public member docs: Forgejo Actions and runner classes, Pages, the fork's `jj` support, other member operations; a `Mintlify`-like look | *(Chris decided)* |
| Stack, hosting | `Fumadocs` on React Router and `Vite`, built static, not Next.js; Cloudflare Workers static assets, not `git-pages` | *(Chris decided)* |
| Brand | Palette, fonts, logo and favicon from `realliance/brand` at a signed tag *(Chris decided)*; no Google Fonts, checked in CI; Catppuccin code themes *(Claude's call)* | both |
| Source | `realliance/docs` *(Claude's call)*, public; outsiders open fork PRs, a member approves each run | *(Chris decided)* |
| Gate | The shared Vale action from the first PR: errors block, warnings print | *(Chris decided)* |
| Name | `docs.realliance.net`, a Workers Custom Domain owned by `cloudflare/dns/` | *(Claude's call)* |
| CI | Vale, build and deploy, each a `tiny` job | *(Claude's call)* |
| Credential | A token for this one Worker, an Actions secret, read only in the last step of a `main`-only deploy job | *(Claude's call)* |
| Content | About 25 pages, 11 in the first release; prose in component children; recovery as a public-safe outline; `jj` pages beta | *(Claude's call)* |
| Operations | No extra mirror, a laptop `wrangler deploy` as fallback; PR previews as artifacts; hand PRs until Renovate on the forge | *(Claude's call)* |

## Intent

Realliance members need one place that explains how to use the forge. Today that knowledge sits in
operator runbooks in the private `luma-homeops` repository (`docs/ci.md` alone runs to 954 lines), in
design specs, and in Chris's head. Members can't read it, and outsiders who open fork PRs even less. A
public site gives them the member-facing half: how to write a pipeline, pick a runner class, publish a
Page, work with `jj`, and get their work back when the forge is down.

The site has to stay up when the home servers don't: a page titled "If the forge is down" that goes
down with the forge is worse than none. That rules out `git-pages` and any galaxy pod *(Chris
decided)* and picks Cloudflare's edge, where only a deploy needs the forge. `Fumadocs` gives the
`Mintlify` look with no SaaS and with search in the browser, and its React Router build fits `tiny`
where its Next.js export doesn't. The Realliance brand makes it read as part of the same service as the
forge and the SSO, and the Vale gate holds it to the standard Chris applies to his own writing.

## Goals

1. A member finds how to do a forge task in under a minute, by navigation or search.
2. `docs.realliance.net` serves while the forge, galaxy or the home uplink is down.
3. A reviewed merge to `main` goes live within 5 minutes, with no manual step.
4. The site matches the forge and the SSO: same palette in both themes, same fonts, logo and favicon,
   from one brand tag.
5. Nothing merges with a Vale error, and Vale sees every sentence a reader sees.
6. Every CI job fits `tiny` with room to spare, and an outsider's PR can't reach a secret.

## Non-goals

- Pages for members only, sign-in, or private content. Whatever isn't public stays in `luma-homeops`.
- Versioned docs, translations, generated API reference, comments, feedback widgets or analytics.
- External link checks in CI; CI checks internal links only.
- A social preview image per page. The build drops the scaffold's image renderer, a native module, and
  uses the brand's tile logo everywhere *(Claude's call)*.
- A GitHub mirror, `Mintlify`'s hosting, or `git-pages`.

## Anti-goals

Any of these makes the design a failure, even if the site looks right:

- Docs that are down when the forge is down.
- A request from a reader's browser to any third-party origin: fonts, scripts, search or analytics.
- The deploy secret in a job that runs the site's build code or reads PR text.
- A deploy, by the pipeline, of anything but reviewed `main`.
- Operator secrets, key stores, account or zone ids, home network names or addresses on a public page.
- Prose that CI can't lint, such as a paragraph in a `description="…"` prop.
- A page whose text needs JavaScript to read.
- A cookie on `docs.realliance.net`, which shares its site with `git` and `sso` (see Credential).

## Constraints

| Fact | The design |
|---|---|
| `tiny` (`docs/ci.md`): 1 CPU, 2 GiB, 15 min; `/work` a 1 GiB memory volume inside the 2 GiB; egress on 80 and 443 from the home WAN address; `node` 24.21.0 and no `npm` | three short jobs; a pinned, checksummed `npm` |
| Measured 2026-11-07 on the spike's sources: `npm ci --ignore-scripts` works, with 418 MB of `node_modules` and a 157 MB cache; the build takes 6 s and writes 89 files, 2.2 MB | install scripts off; the cache goes before the build, so `/work` peaks under 0.6 GiB |
| Spike at 1 CPU and 2 GiB: the Next export peaks at 2.0 GiB; React Router and `Vite` at 0.58 GiB | React Router and `Vite` *(Chris decided)* |
| The `Fumadocs` theme toggle sets a `dark` class, default `system`; the brand's `realliance-palette.css` keys on `prefers-color-scheme` | `--color-fd-*` generated from `palette.json`, per class |
| Both scaffolds load Google Fonts | removed; a CI check and the CSP keep them out |
| Vale skips front matter and string props, and lints JSX children | prose in children; a check bans `description=` props |
| Workers static assets: file requests are free and unlimited; 20,000 files, 25 MiB each; `_headers` and `_redirects` apply | an assets-only Worker |
| A Workers Custom Domain brings its own DNS record and certificate, can't sit on a CNAME, and needs the zone in the Worker's account. Changing one takes Editor plus the zone's Workers Routes write, with no per-Worker scope | the Worker lives beside `realliance.net`; `cloudflare/dns/` creates the domain with an operator token |
| Editor on one Worker can deploy and roll back, not create, delete or route it; creating takes the product-level `Admin` role | the deploy token can't move the name; Chris's first deploy creates the Worker |
| Forgejo 15: a push or same-repo PR job token writes the whole repository, pushes refs outside `refs/heads/` and `refs/tags/` that no rule protects, and dispatches workflows. A fork PR's token only reads, with no secrets. Members have write on every `realliance` repository | the residual risk under Credential |

## Repository layout

| Path | Holds |
|---|---|
| `content/docs/<section>/*.mdx`, `meta.json` | the pages, by the outline's sections; `meta.json` sets order and titles |
| `app/` | the React Router app: root, routes, layout, the MDX component map, the search dialog |
| `app/app.css`, `app/brand.css` | Tailwind and the `Fumadocs` preset; `brand.css`, which `sync-brand.sh` writes from `brand/palette.json` |
| `brand/palette.json`, `public/brand/`, `brand.lock.json`, `keys/` | brand files with their hashed names and licences; the release in use; the keys its tag verifies against |
| `public/_headers`, `public/_redirects` | response headers; redirects for moved pages |
| `wrangler.jsonc` | the Worker: name and assets settings, no routes |
| `package.json` and lock; `deploy/package.json` and lock | exact versions from the spike's set; `wrangler` alone, for the deploy job |
| `tool.lock` | the `npm` release, URL and `sha512`, until the shared `fetch-tool` action exists |
| `scripts/` | `sync-brand.sh`, `fetch-npm.sh`, `check-content.sh`, `check-build.sh`, `lint-docs` |
| `.forgejo/workflows/site.yml`, `.vale/vocab/` | the three jobs; the repository's own words, per the shared-actions spec |

Pages sit at the root with no `/docs` prefix, and the Welcome page is `/`. Each page links "Edit on the
forge" to its source, in place of the scaffold's upstream link *(Claude's call)*.

## CI and deploy

One workflow, `site.yml`, on `pull_request`, `push` to `main`, and `workflow_dispatch` to redeploy
`main`. Every `run` step blanks `GITHUB_TOKEN`, `FORGEJO_TOKEN` and `ACTIONS_RUNTIME_TOKEN` in its own
`env:`, and every job checks out anonymously at `$GITHUB_SHA`, as `image.yml` does. No `actions/checkout`,
`pull_request_target`, `always()` or `continue-on-error`.

| Job | When | Does | Holds |
|---|---|---|---|
| `vale` | every event | the shared-actions spec's job, `paths: content README.md CONTRIBUTING.md` | the job token, cleared by the action |
| `build` | every event | fetch `npm` by `tool.lock`; `npm ci --ignore-scripts`, cache under `$RUNNER_TEMP`, deleted after; `npm run build`; both checks; tar `build/client`, output its `sha256`; upload artifact `site` | the job token, in the upload step only |
| `deploy` | `needs: [vale, build]`; `github.ref` is `refs/heads/main` on `push` or `workflow_dispatch`; `concurrency` group `deploy` | download `site`, match its `sha256`; pass without deploying if `main` has moved on (anonymous `git ls-remote`); install `deploy/` with `--ignore-scripts`; deploy | the secret, last step only |

```yaml
      - name: Deploy           # the repository's only secret reference
        env:
          GITHUB_TOKEN: ""
          FORGEJO_TOKEN: ""
          ACTIONS_RUNTIME_TOKEN: ""
          CLOUDFLARE_ACCOUNT_ID: ${{ vars.CLOUDFLARE_ACCOUNT_ID }}
          CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_DEPLOY_TOKEN }}
        run: deploy/node_modules/.bin/wrangler deploy --message "$GITHUB_SHA"
```

Each deployment's message is its commit, so `wrangler deployments list` maps every live version to `main`.

**Checks.** `check-content.sh` fails on a `description=` prop, and on front matter `description` over
160 characters, which Vale skips. `check-build.sh` fails on `googleapis` or `gstatic` in the output; an
`http(s)://` URL in a `<link>`, `<script>` or CSS `url()`; a broken internal link; a missing
`404.html`; a page whose HTML lacks its text; `app/brand.css` out of step with `palette.json`; and the
Workers file limits.

**PR previews.** A reviewer downloads `site` and runs `npm run preview`, which serves it through
`wrangler dev` with production's headers, redirects, and 404s.

**Worker.** Assets only, and no routes, since `cloudflare/dns/` owns the name:

```jsonc
{ "name": "realliance-docs", "compatibility_date": "2026-11-07",
  "workers_dev": false, "preview_urls": false,
  "assets": { "directory": "./build/client", "not_found_handling": "404-page",
              "html_handling": "auto-trailing-slash" } }
```

`_headers` sets `nosniff`, a referrer policy, HSTS without `includeSubDomains`, and a CSP on
`default-src 'self'` and `frame-ancestors 'none'` that never names another origin. Hashed files under
`/assets/` and `/brand/` cache for a year. Nothing sets a cookie.

**DNS.** `cloudflare/dns/workers.tf` *(Claude's call)*:

```hcl
resource "cloudflare_workers_custom_domain" "docs" {
  provider   = cloudflare.workers
  account_id = local.realliance_account_id
  zone_id    = local.zones["realliance.net"].id
  hostname   = "docs.realliance.net"
  service    = "realliance-docs"
}
```

A custom domain, not an explicit record: it brings its own DNS record and certificate, which an
explicit `docs` record would only block, and its record leaves `plan` empty since the provider manages
what the module declares. The exact name beats the `*.realliance.net` wildcard; every other name still
reaches Traefik. The `cloudflare.workers` alias reads a second token, kept in its own git-crypt file
that `tf.sh` exports and the cluster never gets. It holds Workers Routes write on `realliance.net` and
Workers Editor for the account. The cluster's DNS token stays unchanged.

**Forge down.** The site keeps serving; deploys wait. For an urgent change, Chris builds from any clone
and runs `wrangler deploy` under his own login, and `wrangler rollback` undoes a bad deploy.

## Credential

| Item | Design *(Claude's call)* |
|---|---|
| Token | account-owned; "Specified Workers" = `realliance-docs`, role Editor; no zone permissions; a year's expiry; client IP filter on the home WAN prefix. Chris makes it (a rollout step) |
| Stored | Actions secret `CLOUDFLARE_DEPLOY_TOKEN` on `realliance/docs`; the account id in variable `CLOUDFLARE_ACCOUNT_ID`, so the public tree names no account |
| Read by | the `deploy` job's last step, the only reference on `main` |
| Rotated | yearly, and when a `realliance` member leaves: `docs/forgejo.md`'s offboarding list gains it beside `SITE_REGISTRY_TOKEN` |

**Protection.** `realliance/docs` is member-run like `brand` and `pages`, so maintainers own its
settings in the Forgejo UI:

| Rule | Setting |
|---|---|
| `main` | no pushes, PR merges only; one approval from a member other than the author, stale approvals dismissed, blocked on a rejected review; `vale` and `build` required; no force push or deletion |
| Tags | `*` for Owners and maintainers; no workflow triggers on a tag, so hygiene, not a boundary |
| Detection | the SSO repository's forge tests assert both, as they assert `brand`'s tag rule |

The pipeline thus deploys `main` alone, `main` changes only through a reviewed PR that passed Vale and
the build, and a deploy runs both again on `main` first.

**Residual risk.** Protection binds the pipeline, not the people who can edit it.

1. **Anyone with write on `realliance/docs` can read the token:** every `realliance` member, maintainer
   and Owner, and site administrators, the administrator bots included. Each can push a branch, or a
   ref outside `refs/heads/` and `refs/tags/`, with a workflow that reads the secret, and start it by
   push or dispatch. No Forgejo 15 rule closes this.
2. **Code in a push or same-repo PR job can too.** A hostile package in the build's tree can alter what
   the upload step runs later, and that step holds the write-capable job token, enough to push a ref
   with its own workflow and dispatch it. `--ignore-scripts`, exact pins, and the step blanks stop
   install hooks and code that reads its environment, not a targeted package: `image.yml`'s accepted
   risk, unchanged. Fork PR runs can't, with a read-only token and no secrets.
3. **The token's reach:** anything on `docs.realliance.net` until an Owner notices. That name shares its
   site with `git` and `sso`, so a hostile deploy can phish under a Realliance name and set
   `Domain=realliance.net` cookies that both receive. It can't change DNS or the custom domain, reach
   another Worker, or delete this one. Off the home WAN a copied token is useless, so using it takes a
   `tiny` job, which takes write on a `realliance` repository.
4. **Recovery:** an Owner runs `wrangler rollback`, revokes the token, and looks in
   `wrangler deployments list` for a version whose message isn't a `main` commit.

A stronger option, not proposed: deploy from an Owners-only repository in `realliance-ops` that fetches
`realliance/docs` at `main` anonymously, builds it, and holds the token. That narrows point 1 to Owners,
at the cost of a second repository and a slower path to live.

## Brand

The docs pin **`v0.1.0`**, the only tag (2026-11-07), which the forge and the SSO pin too.
`scripts/sync-brand.sh <tag>` works like the SSO's: it verifies the tag's signature against `keys/`,
checks each file against `dist/manifest.json`, copies it under its hashed name, writes `app/brand.css`,
and records the release in `brand.lock.json`. Any member can change `keys/`, so the real gates are the
brand's tag protection and the sync PR's reviewer, who checks the signer in `brand.lock.json`.

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
| Recovery | **v1** If the forge is down: getting your work back, what to expect, who to contact |
| Contributing | **v1** Writing these docs: style, Vale, opening a PR |

| On the site | Stays in `luma-homeops` |
|---|---|
| What a member does and sees: sign-in, repositories, workflows, classes, Pages, `jj`, recovery from a clone | How operators run it: break-glass, backup internals, rotation, Azure, the sweeper and ledger, alerts, the Pages relay, bumps, the fork image |
| Limits a member meets: class shapes, daily `tiny` minutes, the default paid limit, the Pages cap; that a Realliance-wide cap exists | Global cap values, ceilings, backstops; threat analyses and residual risks |
| Who to contact, as a role and a channel | Secret names and locations, key stores, account and zone ids, tokens |
| "Realliance's own hardware" and "the cloud" | Node names, addresses, the LAN, the WAN address |

**Recovery** publishes an outline: a clone holds the whole history, how to push it elsewhere, where
outages get announced, and who to contact. Owner steps that need backup keys stay in `docs/forgejo.md`;
the public page says only that Owners can restore from backups.

**`jj` pages** carry "Beta" in their sidebar title and a warning box at the top: the fork's `jj`
features may change at the 19 LTS rebase. Rebase merges and divergent changes wait for the fork.

**Components.** The MDX map registers `Card`, `Tabs`, `Steps`, `Callout` and `Accordion`. Prose goes in
children, as in `<Card title="Publish a site" href="/pages/publish/">Push a pages branch…</Card>`;
titles and tab labels stay short props.

## Rollout

| Phase | Delivers | Done when |
|---|---|---|
| 0 | the shared-actions spec's phase 1: `vale` at `v1.1.0` | its own done line |
| 1 | `realliance/docs`, the skeleton site, brand sync, the `vale` and `build` jobs, protection | an error blocks a merge; a fork PR waits for approval; `site` uploads |
| 2 | the Worker, `workers.tf`, the deploy token and job, lines in `docs/forgejo.md` and `CLAUDE.md` | a merge goes live; `plan` is empty |
| 3 | the 11 first-release pages | the acceptance checks pass |
| later | the other pages; Renovate pins once the forge's bot runs | |

Phase 2 runs in this order; steps 1 to 3 are Chris's:

1. From a laptop, `wrangler login`, then `wrangler deploy` of a local build. This creates the Worker
   and doubles as the first fallback drill.
2. Create the Terraform token and its git-crypt file; `tf.sh plan`, `apply`, then `plan` again, empty.
3. Create the deploy token from the Credential table; add the secret and the variable to
   `realliance/docs`.
4. Merge a PR and watch `deploy` put it live.

## Acceptance

1. `https://docs.realliance.net/` serves with a valid certificate from Cloudflare's edge, and
   `tf.sh plan` is empty. `www`, `git` and `sso.realliance.net` still resolve to the WAN address.
2. A first visit follows the OS theme. The toggle switches palette, logo and code theme together, and
   each `--color-fd-*` value equals its `palette.json` source.
3. Loading any page and running a search makes no request to another origin.
4. Search finds a word that appears only in a page's body.
5. A Vale error blocks a merge; warnings alone don't. A fork PR's run waits for a member's approval,
   and its jobs have no secret.
6. A merge to `main` is live within 5 minutes, and `wrangler deployments list` shows its commit. A PR
   run deploys nothing and leaves `site` on the run.
7. The deploy token can't add a route: a `wrangler deploy` that names one fails.
8. `build` and `deploy` each finish within 5 minutes on `tiny`, with `/work` under 0.8 GiB.
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
- **`tiny`:** `/work` and peak memory on a real pod; the `tool.lock` format, later `fetch-tool`.
- **Wrangler:** that a deploy without `routes` leaves the custom domain alone, that `--ignore-scripts`
  installs a working `wrangler`, and that the Editor token can keep `workers_dev` off.
- **Cloudflare:** that the wildcard CNAME doesn't block the custom domain; if it does, a
  `proxied = true` `AAAA` record for `docs` at `100::` in `records.tf` plus a
  `cloudflare_workers_route`. The Free plan's certificate; the egress address for the IP filter.
- **Forgejo 15:** the `vars` context; job `concurrency`; where Actions artifacts live (on B2, one
  download a deploy); whether repository administrators bypass `main` protection, and the setting that
  stops them; check names and protected file patterns, per the shared-actions spec.
- **The CSP** that hydration and the theme script need.
- **Chris:** the contact channel the recovery page names.
