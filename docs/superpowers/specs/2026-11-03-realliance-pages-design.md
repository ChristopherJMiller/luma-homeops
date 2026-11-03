# Realliance Pages — static sites on realliance.page

Status: design, 2026-11-03 (brainstorm with Chris). Research: `.superpowers/pages-research/servers.md`
(git-pages and alternatives) and `tiny-runner.md` (deferred, see Non-goals).

## Intent

A GitHub-Pages-like service for the Realliance forge (Forgejo 15 at `git.realliance.net`). Anyone with a
forge account can publish a static site from their own repos: project docs, Realliance project and game
sites, and friends' personal pages. GitHub Pages is the model Chris likes.

A site goes live when its **`pages` branch** changes. "Publishing from a pipeline" is not a separate
mechanism: any CI (a person's own runner, GitHub Actions on a mirror, later the Azure runners) builds the
site and **pushes the output to the `pages` branch**. The pages server only ever serves branches.

## Decisions (Chris, 2026-11-03)

1. **Domain:** `realliance.page`, bought for this, in the **Realliance Cloudflare account** (not the account
   that holds `realliance.net`). Never `site.realliance.net`: a page on a `realliance.net` subdomain could
   toss cookies into `git.realliance.net` and `sso.realliance.net` (neither uses `__Host-` cookies).
2. **Layout:** project sites at `https://<owner>.realliance.page/<repo>/`; the user/org root site at
   `https://<owner>.realliance.page/` comes from the repo **`<owner>/pages`**.
3. **Publishing:** branch-serving only, branch name `pages`. No upload API in v1.
4. **Who:** anyone on the forge, for any repo they own (users and orgs). No opt-in for public repos.
5. **Public repos only in v1.** git-pages cannot clone private repos, so a private repo publishes by having its CI push the built site to a separate public repo (e.g. `alice/site-pages`). Proper private-repo publishing comes with the upload flow, once runners exist (Chris, after the builder check).
6. **Quota:** git-pages' own `max-site-size` hard cap at its **128 MB** default: a publish over it is refused and the previous version keeps serving (Chris, after the plan found the setting; supersedes 500 MB + monitoring).
7. **Out of v1:** private (SSO-gated) pages, custom domains, PR previews, uploads, and the shared runner.

## Goals

- A push to `pages` on any repo is live within about a minute, with valid TLS.
- Ownership is enforced by the forge: nobody can publish to another owner's host.
- No server-side code execution; pure static serving.
- Runs on galaxy under GitOps like everything else; state survives a pod or node loss.

## Non-goals and anti-goals

- **Not** hosting anything on `*.realliance.net`.
- **Not** a build system. Builds happen wherever the author likes and push to `pages`.
- **No cookies** on `realliance.page` from our side, at least until the domain is on the Public Suffix List
  (below). The server must not set any.
- Anti-goal: a design where a compromised or careless page can affect the forge, the SSO, or another
  owner's site beyond what the PSL caveat below admits.

## Constraints and facts

> **Amendment (2026-11-03, after approval):** `realliance.page` is in the **same** Cloudflare account
> ("Realliance") as `realliance.net`, and Chris extended the existing token to edit it. So pages uses the
> existing pattern for every zone, and the "second account" items below are superseded:
> - **DNS:** add `realliance.page` to `cloudflare/dns/` `local.zones` (apex A + `*` wildcard, DNS-only,
>   like `realliance.net`), and add it to `cloudflareddns` `CF_HOSTS`. No CNAMEs, no second state.
> - **TLS:** add `realliance.page` and `*.realliance.page` to the one `traefik/wildcard-tls`
>   Certificate (existing issuer and token). Pages Ingresses carry no `spec.tls`, like every other app;
>   no separate certificate.

- **Server: git-pages** (codeberg.org/git-pages/git-pages), pinned by digest to **0.10.1** (2026-10-28, a security release; Chris waived the 2-week pin-age rule
  for it on 2026-11-03). It is pre-1.0 with breaking changes and three security-fix releases in 2026, so
  every bump reads the changelog and Renovate never automerges it. It is the only maintained server found that
  serves branches via a Forgejo webhook and has a `[[wildcard]]` mode for `<owner>.<domain>/<repo>`; its
  state can live in S3-compatible storage. Codeberg's older pages-server is in maintenance mode.
- **TLS** terminates at Traefik with a dedicated wildcard certificate for `realliance.page` and
  `*.realliance.page` (git-pages' own ACME is only planned). cert-manager's existing `letsencrypt-prod`
  issuer uses a Cloudflare token for the *other* account, so this needs a token from the Realliance
  account (DNS edit on `realliance.page` only) and either a second ClusterIssuer or a `dnsZones`-selected
  solver. The existing default TLSStore cert (`traefik/wildcard-tls`) is unchanged.
- **DNS** *(my call)*: `realliance.page` and `*.realliance.page` are DNS-only CNAMEs to `realliance.net`
  (Cloudflare flattens the apex), so `cloudflareddns` keeps tracking one WAN IP and no second DDNS is
  needed. Managed by Terraform with the Realliance account's token, kept beside `cloudflare/dns/` in
  luma-homeops (a separate state; `plan` must be empty, like `cloudflare/dns/`). Exposure is the same
  path as every public app: WAN :443 → Traefik `.7`.
- **Webhook:** one Forgejo **system webhook** (all repos) for push events → git-pages, with a shared
  secret. git-pages acts only on pushes to `pages`. The receiver is reachable from Forgejo in-cluster.
- **Reading repos:** public repos only; git-pages needs no forge credential.
- **Storage:** git-pages state in a new private B2 bucket in the Realliance B2 account (as the forge's
  objects), application key limited to that bucket, no `deleteFiles` unless git-pages needs it for
  site updates (the builder checks and states which). Never the SMR Ceph pool for site content.
- **Quota:** git-pages' `max-site-size` at its 128 MB default (decision 6). A publish over it is
  refused and the previous version keeps serving; a failed update raises an alert.
- **Headers:** no `Set-Cookie`; a conservative default set (`X-Content-Type-Options: nosniff`,
  `Referrer-Policy`, HSTS on `realliance.page` without `includeSubDomains` preload until the PSL entry).
  Sites may add their own headers only if git-pages supports it safely.
- **Hidden orgs:** a hidden org's repos are private, so they cannot publish in v1; the
  wildcard cert and wildcard DNS keep hostnames out of CT logs and DNS until a site is actually served.

## Public Suffix List

Until `realliance.page` is on the PSL, browsers treat all `<owner>.realliance.page` hosts as one site:
one owner's page can set a `Domain=realliance.page` cookie that another owner's page receives, and
SameSite does not separate them. Static sites rarely use cookies, so this is accepted for v1 and
documented for site authors. Submission (a PR to github.com/publicsuffix/list plus a `_psl` TXT record
that stays in place) needs **≥ 2 years of registration remaining** — the domain is registered for 1 year,
so **Chris extends it first**. Submit once the service is running (the PSL declines test projects);
browsers pick it up over weeks to months.

## Operations

- Runbook `docs/pages.md`: publishing (including a sample Forgejo Actions / GitHub Actions workflow that
  builds and pushes to `pages`), the private-repo opt-in, quota, how an Owner takes a site down
  (git-pages' admin delete or a deny entry — whichever it supports), PSL status, and bumping git-pages.
- Monitoring: git-pages up + webhook delivery failures visible (Forgejo shows webhook delivery history;
  a Prometheus scrape if git-pages exposes metrics).
- Backups: the `pages` branches are the source of truth, so B2 state is rebuildable; no extra backup.
- CLAUDE.md topology row and a forge runbook pointer.

## Acceptance

1. A push to `pages` in a public repo `alice/site` serves `https://alice.realliance.page/site/` with
   valid TLS within ~1 minute; a second push updates it.
2. `alice/pages` serves `https://alice.realliance.page/`.
3. A push to any other branch changes nothing.
4. A private repo's `pages` branch publishes nothing.
5. A publish over 128 MB is refused, the previous version keeps serving, and the update-failure alert fires.
6. User B cannot make content appear on `alice.realliance.page` (no write access to Alice's repos ⇒ no
   site); verified, not assumed.
7. No response from `*.realliance.page` carries `Set-Cookie`.
8. `realliance.net` hosts are unaffected (their cert, Ingresses, and DNS plan unchanged).

## Calls made after the builder check (mine, 2026-11-03)

Facts from git-pages' README: webhooks are a `POST` of a forge webhook payload to the site URL, with
no signature check mentioned; `_headers` only applies headers that config allowlists; there is no size
limit setting; private-repo clones with credentials are not mentioned (hence decisions 5 and 6).

- **Webhook exposure.** A forged `POST` can at most make git-pages refetch a real repo's `pages`
  branch from the forge (content never comes from the payload), so it is a rate-limit concern, not an
  integrity one. Traefik allows `POST` on `*.realliance.page` only from the cluster's pod network
  (where Forgejo's webhook originates); everyone else gets `GET`/`HEAD` only. The builder confirms how
  one Forgejo system webhook reaches per-site URLs; if it can't, a per-repo webhook created by a later
  forge-bot "repo defaults" job or a tiny relay is the fallback.
- **Headers.** git-pages' `_headers` allowlist excludes `Set-Cookie` (and anything security-relevant
  we set ourselves); a Traefik headers middleware also removes `Set-Cookie` and sets `nosniff`,
  `Referrer-Policy` and HSTS (no `includeSubDomains`/preload before the PSL entry).
- **Names.** An owner whose name is not a valid single DNS label (dots, underscores, > 63 chars) gets
  no site; documented. Names are lower-cased. `www.realliance.page` and the apex redirect to
  `https://realliance.realliance.page/` (the Realliance org's own site).
- **Forks, mirrors, archived repos** publish like any repo when their `pages` branch receives a push
  event (the builder checks whether pull-mirror syncs fire one). Archived repos keep their last site
  until an Owner takes it down. The forge is invite-only (Realliance SSO), so "anyone" means invited
  members; takedown is an Owner deleting the site in git-pages (its admin interface or CLI).
- **TLS.** A separate Certificate `pages-wildcard-tls` (namespace `pages`) for `realliance.page` and
  `*.realliance.page`, referenced by the pages Ingress's `spec.tls`; Traefik picks it by SNI. The
  default TLSStore and `traefik/wildcard-tls` are untouched, so a Realliance-account token problem
  can never block renewal of the main wildcard. The edge regression snapshot gains a
  `realliance.page` host.
- **DNS state.** Same Terraform backend as `cloudflare/dns/`, separate state key and wrapper; the
  Realliance account's DNS token lives the way `cloudflare/dns/`'s does (git-crypt), and the
  cert-manager copy is a SealedSecret.
- **Size.** One replica with small requests (static serving; B2 holds state, so a reschedule recovers
  in minutes). Memory requests are tight on galaxy (~84 %), so the builder measures before sizing.
- **PSL** is the plan's last, gated phase: Chris extends the registration to ≥ 2 years, then the PR
  and `_psl` record.


## Left to the builder

- git-pages configuration details (wildcard mode, index repo name `pages`, clone URL template, webhook
  secret handling, quota setting), and which version to pin.
- ClusterIssuer vs solver selector for the second Cloudflare account; Ingress/IngressRoute shape for
  `*.realliance.page` routing to git-pages.
- Exact B2 key capabilities git-pages needs.
- Whether Forgejo's automatic Actions token can push to the repo's own `pages` branch (for the sample
  workflow), and the GitHub-mirror variant.

## Deferred (own designs later)

- **Runner classes** (Chris): `tiny` on galaxy, `small`/`medium`/`large` on Azure. The tiny class's
  research (`tiny-runner.md`) recommends an ephemeral per-job controller, not a long-lived shared
  runner, and depends on the NetworkPolicy egress lock-down.
- Private (SSO-group) pages via oauth2-proxy; custom domains (git-pages' DNS TXT challenge); PR previews;
  upload-based publishing.
