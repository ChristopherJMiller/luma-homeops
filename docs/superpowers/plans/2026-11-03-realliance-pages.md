# Realliance Pages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A push to the `pages` branch of any public repo on git.realliance.net is served within about a minute at `https://<owner>.realliance.page/<repo>/`, or at `https://<owner>.realliance.page/` for `<owner>/pages`. TLS is valid, no cookies are set, and the forge enforces ownership.

**Architecture:** git-pages (one replica, state in a private Realliance B2 bucket) serves every site behind Traefik. Traefik routes only `GET`/`HEAD` for `*.realliance.page` and uses the one shared wildcard certificate, which gains two SANs. A git-pages webhook must be POSTed to the *site's own URL*, so one Forgejo system webhook cannot reach it directly. Instead it goes to `pages-relay`, a ~200-line stdlib Go service in `images/pages-relay/`. The relay verifies the HMAC, maps the push to `Host: <owner>.realliance.page` + `/<repo>/`, and forwards it to git-pages in-cluster. git-pages then re-derives the clone URL from that host and refetches the repo from the forge itself. DNS is the existing `cloudflare/dns/` module plus one zone.

**Tech Stack:** git-pages 0.10.1 (Go, S3 backend), Traefik v3 IngressRoute + Middleware CRDs, cert-manager v1.21.3 (`letsencrypt-prod`), Terraform `cloudflare/cloudflare ~> 5.0` (`cloudflare/dns/`), OpenTofu `Backblaze/b2` 0.14.1 (`realliance/sso` `b2/`), Forgejo 15.0.11 (chart 17.3.2), Argo CD, SealedSecrets, kube-prometheus-stack (ServiceMonitor, PrometheusRule), Go 1.26 stdlib, pytest + PyYAML + tomllib, GitHub Actions → GHCR.

**Spec:** `docs/superpowers/specs/2026-11-03-realliance-pages-design.md` (approved at 4addf3b9; amendment 9556a2d8, "same Cloudflare account"). The amendment overrides the spec's second-token, CNAME and separate-certificate text. Research: `.superpowers/pages-research/servers.md`.

## Global Constraints

### Pins (resolved read-only, 2026-11-03)

| What | Value | Source / note |
|---|---|---|
| git-pages | `codeberg.org/git-pages/git-pages:0.10.1@sha256:882733e3fc4b5cde8cb3f83b786f8b85854c9ad951662b64fca23cbd34f79dc9` | OCI index, 2026-10-28; pin-age rule waived by Chris (security release). |
| Relay builder stage | `golang:1.26-alpine@sha256:f85330846cde1e57ca9ec309382da3b8e6ae3ab943d2739500e08c86393a21b1` | A golang builder stage, pinned by digest. Old enough. |
| Relay runtime | `gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab` | uid 65532. The reproducible build has an epoch `created`, so its age cannot be read; this follows the mcp-jwt-auth precedent. |
| Relay image | `ghcr.io/christopherjmiller/pages-relay:sha-<7>@sha256:<64>` | Set in Task 4. Our own image is exempt from the age rule; its bases are not. |

Re-check a Codeberg digest: get an anonymous token from `https://codeberg.org/v2/token?service=container_registry&scope=repository:git-pages/git-pages:pull`, then `curl -sSI -H "Authorization: Bearer $tok" -H 'Accept: application/vnd.oci.image.index.v1+json' https://codeberg.org/v2/git-pages/git-pages/manifests/<tag>` and read `docker-content-digest`.

### Values fixed by the spec (verbatim) or by this plan

- Domain **`realliance.page`**. Sites: `https://<owner>.realliance.page/<repo>/`. Root site: repo **`<owner>/pages`**. Branch **`pages`** only, for project and root sites (`index-repo-branch = "pages"`).
- **Public repos only** in v1. A private repo publishes by having CI push to a separate public repo. Hidden orgs' repos are private, so they publish nothing.
- **Quota: `max-site-size` hard cap at the 128 MB default** (Chris; enforcement: Decision D3).
- **No `Set-Cookie`** from `realliance.page`. Always send `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, and `Strict-Transport-Security: max-age=31536000` with **no** `includeSubDomains`/`preload` until the PSL entry. (`.page` is HSTS-preloaded TLD-wide anyway.)
- **`www.realliance.page` and the apex redirect to `https://realliance.realliance.page/`.** Owners whose name is not one valid DNS label (dots, underscores, > 63 chars), and the owner `www`, get no site. Names are lower-cased.
- DNS (amendment): `realliance.page` joins `local.zones` in `cloudflare/dns/records.tf` (`proxied = false`) and `CF_HOSTS` in `cluster/dns/cloudflareddns-config.yaml`. Same token, same state, no CNAMEs. `./cloudflare/dns/tf.sh plan` must end empty.
- TLS (amendment): `realliance.page` and `'*.realliance.page'` join the dnsNames of **`traefik/wildcard-tls`**, issuer `letsencrypt-prod`. There is **no** `pages-wildcard-tls` and no `spec.tls`/`tls.secretName` on any pages route. The default TLSStore is untouched.
- Never put a TXT record on the `realliance.page` apex. `*.realliance.page` is a CNAME to the apex, so `_git-pages-challenge.<owner>.realliance.page` and `_git-pages-repository.<owner>.realliance.page` resolve to apex TXT. `_psl.realliance.page` is an explicit record and safe.
- Names: namespace and Argo app **`pages`**. Deployments and Services **`git-pages`** (ports `pages` 3000, `metrics` 3002) and **`pages-relay`** (`http` 8080). Secrets **`git-pages-s3`** (keys `PAGES_STORAGE_S3_ACCESS_KEY_ID`, `PAGES_STORAGE_S3_SECRET_ACCESS_KEY`) and **`pages-relay-webhook`** (key `webhook-secret`). Middlewares **`pages-headers`** and **`pages-apex-redirect`**. IngressRoutes **`pages-sites`** and **`pages-apex`**. PrometheusRule **`pages-alerts`**. B2 bucket and key **`realliance-pages`** (tofu outputs `forge_pages_key_id`, `forge_pages_key`). Canonical webhook secret: `PAGES_WEBHOOK_SECRET` in `~/Repos/realliance-sso-secrets/forge/secrets.env`.
- URLs: relay hook **`http://pages-relay.pages.svc.cluster.local:8080/hook`**; relay upstream **`http://git-pages.pages.svc.cluster.local:3000`**. B2 S3 endpoint `s3.us-east-005.backblazeb2.com`, region `us-east-005`.
- Images are pinned `tag@sha256`. Renovate never automerges git-pages or pages-relay. Read every git-pages changelog in the bump range.

### How changes land

- **luma-homeops** (public): edit, run that task's tests, then `nix develop --command git commit …`. pre-commit runs yamlfmt, kustomize-build, `netpol-manifests` and (from Task 5) `pages-manifests`; if a hook rewrites a file, `git add` it and commit again. Never use `--no-verify` or `--no-gpg-sign` (S6); signing follows the repo's config. Push to `main`. Every commit ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **`realliance/sso`** (`~/Repos/realliance-sso`, GitHub, public): `git commit -S`, then a branch and `gh pr create --fill`. Merge with `gh pr merge --squash --delete-branch` **only after Chris says merge**.
- **`realliance/sso-secrets`** (private): `git commit -S`, push `main`. Every shell that signs or uses ssh/gpg exports `DISPLAY=:10.0 XAUTHORITY=$HOME/.Xauthority`.
- **At execution start, one AskUserQuestion** gets Chris's OK for the routine pushes: luma-homeops docs, ledger, snapshot script, relay code, sizing, and sso-secrets. Every **STOP** below needs its own OK.
- **A push that changes what Argo renders is a deploy** (Argo syncs in ~3 min). It happens only after that task's STOP.
- **STOP — ask Chris** = one AskUserQuestion that states the change, the Gate G evidence, what you will watch and the rollback trigger. Options: "approve: push, and roll back at once if a trigger fires" / "hold". A rejected or interrupted call may already have run, so read live state before redoing anything.
- **Rollback:** `git revert` the task's commit. For the first pages push (Task 5), set `cluster/pages/kustomization.yaml` to `resources: []`; Argo prunes it (`allowEmpty: true`). **Never roll back by deleting an Application file.**
- **Ledger:** `docs/superpowers/plans/2026-11-03-realliance-pages.ledger.md`. Record answers, Gate G summaries, timestamps, measurements and rulings. No secrets, no hidden names.
- **Waits:** use `Monitor` for anything over 60 s, never a sleep loop.

### Gate G (before every STOP push, and again after)

- G1. No Talos/k8s hop, drain or Ceph recovery is running. `kubectl get nodes` shows 3 `Ready` nodes and no `SchedulingDisabled`; all Ceph PGs are `active+clean`.
- G2. Put a `cluster-snapshot` summary in the ledger. Baseline: Ceph `HEALTH_WARN` with BLUESTORE slow-op noise only; in Argo, only `rook`/`rook-cluster` are not Healthy.
- G3. Run the edge snapshot (Task 1's version): `cd .claude/skills/edge-ingress && nix-shell -p openssl jq --run ./edge-snapshot.sh > $SCRATCH/edge-<task>-<before|after>.txt`, then `diff <(sed 's/ cert=.*//' before) <(sed 's/ cert=.*//' after)`.
- G4. `kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded` is recorded.

### Cluster, secrets, compute

- `export KUBECONFIG=/tmp/galaxy-kubeconfig` (refresh from Talos per CLAUDE.md if rejected). Run kubectl/kubeseal/terraform/go via `nix develop --command …` in luma-homeops, and pytest/tofu in realliance-sso.
- Never `Read`/`cat`/diff a `*.secret.yaml` or `nodes/*`. There are only two token reads: `tf.sh`'s own, and Task 7's zone lookup, which copies `tf.sh`'s awk line into a variable. Secrets never reach argv, echo, assertion messages, logs, PR text or the ledger. Write them with pipes and `umask 077`. In git-crypt repos use `git diff --no-textconv`. Static tests open only the paths they name.
- Public repos: no secret values and no hidden org/team/group names anywhere, including relay logs (Decision D9).
- Local compute: `taskset -c 0-3`, `GOMAXPROCS=4`, `-p=4`. No local docker/kind. CI builds images.

## Decisions this plan makes (spec gaps and contradictions)

- **D1. Relay, not a direct system webhook.** git-pages selects the site of a webhook POST from the request's Host and path (`src/pages.go` `postPage` → `GetSite`), and the README says the URL "must be the root URL of the site". A Forgejo webhook has one fixed URL. So the spec's fallback applies, and the plan picks the relay over per-repo hooks: forge-bot's "repo defaults" job is deferred, and repo admins can delete per-repo hooks. Task 4 Step 1 re-confirms this before any code is written.
- **D2. No POST through Traefik at all.** The relay reaches git-pages by its Service. Forgejo's webhook would hairpin through the router and arrive with a router or node source IP, so a "pod network only" allow-list could not work anyway. `*.realliance.page` routes match `Method(GET) || Method(HEAD)` only; every other method gets Traefik's 404, from everyone. This is tighter than the spec. Traefik has no method middleware, so the "POST restriction" is the router matcher, and its static test pins it.
- **D3. The quota is a hard cap in git-pages (128 MB, its default — Chris 2026-11-03).** The spec's builder check says git-pages has no size limit; that is wrong. `[limits].max-site-size` exists, defaults to **128 MB**, and is enforced on fetch and on manifest size. If unset, sites would silently cap at 128 MB. The plan sets `max-site-size = "128MB"`. A push past it is refused and the site keeps its last content. The "quota alert" becomes `PagesSiteUpdateFailing`, and no per-site size exporter is built: blobs are content-addressed and shared, so there is no per-site B2 prefix to measure. Acceptance 5 becomes: "a publish that would pass 128 MB is refused, the site is unchanged, and the alert fires".
- **D4. Pin 0.10.1 from the start.** Chris waived the 2-week pin-age rule for this security release (2026-11-03); Task 5 ships it with `[server].proxy`; Task 12 is docs only.
- **D5. B2 key without `deleteFiles`.** git-pages calls S3 `RemoveObject` without a version (staged manifest on every commit, site delete, unfreeze). On B2 that only *hides* the file, and the forge suite already proved a hide works without `deleteFiles` (`test_forge_objects_key_hides_but_cannot_destroy`). The key gets `listBuckets` (BucketExists → HeadBucket), `listFiles`, `readFiles` and `writeFiles`. A lifecycle rule purges hidden versions after 1 day, since the state is rebuildable. If the first publish logs a RemoveObject `AccessDenied`, add `deleteFiles` and ledger the ruling (Task 6 Step 7).
- **D6. IngressRoute, not Ingress,** for `realliance.page`, because an Ingress cannot match methods. The routes carry no `tls` block: the websecure entrypoint has `http.tls=true` and the default TLSStore serves `wildcard-tls`, like every Ingress. The public route also excludes `/.git-pages/*` except `/.git-pages/health`. This blocks on-demand `archive.tar` (a CPU and uplink amplifier) and manifest enumeration.
- **D7. git-pages config choices:**
  - no `authorization` key, so forge-authorized PUT/PATCH/DELETE stay off (spec: no uploads in v1);
  - `features = []`, so `codeberg-pages-compat` is off;
  - `allowed-repository-url-prefixes = ["https://git.realliance.net/"]`;
  - `allowed-custom-headers` = COOP, COEP, CORP, CSP, `Access-Control-Allow-Origin`, `Permissions-Policy`, `X-Frame-Options`, `X-Robots-Tag`, and never `Set-Cookie`, HSTS, nosniff or Referrer-Policy;
  - `concurrent-uploads = 16` and `update-timeout = "5m"`, to bound memory and fit 128 MB over the uplink;
  - blob cache 64 MB.
- **D8. Unpublish is Owner-only, and the relay tells Owners.** git-pages can only delete a site through DNS-challenge or forge auth (both off) or its admin CLI. Deleting the `pages` branch or the repo therefore leaves the site up. The relay counts these as `unpublish_requested`, and `PagesTakedownRequested` asks an Owner to run the takedown. Making a repo private sends no webhook event; that is documented.
- **D9. The relay never logs or labels a private repo's names.** Hidden orgs' repos are private, and logs ship off-site.
- **D10. Acceptance suite = pytest in `realliance/sso` `tests/forge/test_forge_pages.py`, not a bash script.** Tests 1, 2, 4, 5 and 6 need two throwaway forge identities that sign in through SSO, create repos as themselves and are purged afterwards. That suite already has `forge_user`, `forge_login`, `forge_api(sudo=)`, `kube()`, `VPS` and the no-secrets-in-asserts guard. Acceptance 8's DNS-plan and edge-snapshot parts are luma-homeops verification steps (Task 10).
- **D11. Acceptance 8 after the amendment.** The realliance.net certificate *is* reissued (new SANs, new key). "Unaffected" means:
  - the same `wildcard-tls` with every prior SAN, CN `*.chrismiller.xyz` and issuer `letsencrypt-prod`;
  - an edge snapshot identical for every existing host;
  - unchanged realliance.net Ingresses;
  - an empty `tf.sh plan`.
- **D12. Namespace `pages` is PodSecurity `restricted`**, which both pods meet. The apex redirect is a `302` that keeps the path, because a `301` would be cached forever if a landing page ever replaces it.

## Review Focus

1. **Mixed-case repo or owner names** (`My-Site`). A reasonable person expects the site at the lower-cased URL `/my-site/`. Tests: Task 4 `TestRouteLowercases`; Task 3 `test_mixed_case_repo_publishes_lowercased`.
2. **Names that cannot be a site:** an owner with a dot, underscore or > 63 chars; the owner `www`; a repo starting with `.` (Forgejo's `.profile`). Expected: nothing published, no update-error alert, outcome `ignored_name`. Tests: Task 4 `TestRouteRejectsNames` (table); Task 3 `test_dot_repo_publishes_nothing`.
3. **Deleting the `pages` branch or the repo.** People expect the site to go away. Here it stays until an Owner takes it down (D8), and Owners are told. Tests: Task 4 `TestRouteUnpublish`; Task 3 `test_branch_delete_requests_takedown`.
4. **`www.realliance.page` served as owner `www`'s site instead of redirecting.** The wildcard regex also matches `www`, and Traefik's default priority favours the longer rule. Tests: Task 9 `test_apex_redirect_outranks_sites`; Task 3 `test_apex_and_www_redirect`.
5. **A publish that fails after Forgejo shows success.** git-pages answers `202 updating` after 3 s, so a large site that later fails or hits the cap looks green in the delivery history. Expected: site unchanged, `PagesSiteUpdateFailing` fires, even for the first error since a restart, when the counter series is new. Tests: Task 5 `test_update_failing_alert_catches_new_series`; Task 3 `test_oversize_site_refused_and_alerts`.

## Coordination with concurrent plans

**NetworkPolicy plan** (`docs/superpowers/plans/2026-11-03-networkpolicy-enforcement.md`, enforcer live on `bottom` since 2026-11-03T18:35Z):
- N1. This plan creates **no** NetworkPolicy. `pages` is adopted later through the netpol plan's Task 13 (its own STOP), only after this plan's Task 10 is green. `docs/pages.md` § Flows is the inventory that Task 13 Step 2 needs:
  - in: Traefik → git-pages:3000; Prometheus → git-pages:3002 and pages-relay:8080; ns `forgejo` → pages-relay:8080; pages-relay → git-pages:3000;
  - out: DNS; git-pages → `152.44.247.88`:443 (git.realliance.net via hairpin) and B2 :443.
- N2. Serialize STOPs. Never push a pages change between a netpol task's Gate G "before" and "after", or the reverse. Each plan's ledger records the other's pushes.
- N3. The edge-snapshot change (Task 1) is a docs-only commit. After it lands, both plans take their next "before" with the new script.
- N4. Do not edit `scripts/test_netpol_manifests.py`. Its `netpol-manifests` hook must stay green on every pages commit.

**forge-bot plan** (`docs/superpowers/plans/2026-11-02-forge-bot-and-ci.md`):
- F1. Both plans edit `cluster/applications/forgejo-release.yaml`; pages adds only a `webhook:` block. Rebase immediately before committing. Do not push during forge-bot Tasks 16–18 apply or drill windows: a Forgejo restart (Recreate) interrupts forge-bot cycles. Note the push in its ledger.
- F2. In `realliance/sso`, this plan adds `tests/pages_helpers.py` and `tests/forge/test_forge_pages.py` and extends `tests/test_b2.py`, `b2/pages.tf` and `b2/outputs.tf`. It never edits `forge_helpers.py`, `conftest.py` or `pytest.ini`; forge-bot rewrites those, and it reserves a `pages` pytest marker for *paging* tests. This plan uses only `slow` and the env gate `PAGES_DRILL`.
- F3. The Actions-token drill (Task 10) runs one probe job on `realliance/forge-bot`, the only repo on the galaxy runner's allowlist. Run it only when forge-bot has no CI job in flight. Never change the runner allowlist.
- F4. If forge-bot's future "repo defaults" job adds per-repo webhooks, it must not add pages hooks while the system hook exists (double delivery).

## File map

| Path | Responsibility | Task |
|---|---|---|
| `.claude/skills/edge-ingress/edge-snapshot.sh` | regression snapshot gains realliance.page + git.realliance.net | 1 |
| `docs/superpowers/plans/2026-11-03-realliance-pages.ledger.md` | ledger | 1 |
| `~/Repos/realliance-sso/b2/pages.tf`, `b2/outputs.tf`, `tests/test_b2.py` | bucket, key, their tests | 2 |
| `~/Repos/realliance-sso/tests/pages_helpers.py`, `tests/forge/test_forge_pages.py` | acceptance suite | 3, 10 |
| `images/pages-relay/{go.mod,main.go,relay.go,relay_test.go,Dockerfile}`, `.github/workflows/pages-relay.yml` | relay + image CI | 4 |
| `cluster/applications/pages.yaml`, `cluster/applications/kustomization.yaml` | Argo app | 5 |
| `cluster/pages/{kustomization,namespace,git-pages,pages-relay,monitoring}.yaml`, `config.toml`, `git-pages-s3{.secret,}.yaml`, `pages-relay-webhook{.secret,}.yaml` | the app | 5 |
| `cluster/pages/edge.yaml` | IngressRoutes + Middlewares | 9 |
| `scripts/test_pages_manifests.py`, `.pre-commit-config.yaml`, `.renovaterc.json` | static tests + hook + Renovate rules | 5–9 |
| `cluster/applications/forgejo-release.yaml` | `[webhook] ALLOWED_HOST_LIST` | 6 |
| `cloudflare/dns/records.tf`, `cluster/dns/cloudflareddns-config.yaml` | zone | 7 |
| `cluster/traefik/wildcard-cert.yaml` | two SANs | 8 |
| `docs/pages.md`, `CLAUDE.md`, `docs/forgejo.md`, `cloudflare/dns/README.md`, `.claude/skills/{edge-ingress,cloudflare-dns}/SKILL.md` | docs | 11 |

---

### Task 1: Baseline, ledger, edge-snapshot coverage

**Files:**
- Create: `docs/superpowers/plans/2026-11-03-realliance-pages.ledger.md`
- Modify: `.claude/skills/edge-ingress/edge-snapshot.sh`

**Interfaces:**
- Produces: the snapshot script every later Gate G3 uses. It adds host lines `git.realliance.net/`, `realliance.page/`, `www.realliance.page/` and `realliance.realliance.page/`, plus probe lines `POST realliance.realliance.page/` and `GET realliance.realliance.page/.git-pages/manifest.json`.

- [ ] **Step 1: Execution-start OK.** Ask Chris with one AskUserQuestion, covering the routine pushes listed under "How changes land". Ledger the answer.
- [ ] **Step 2: Gate G1, G2 and G4.** Ledger them, together with `kubectl describe nodes` memory requests per node. Expected: top 85 %, middle 85 %, bottom 86 % (2026-11-03), all within ±2 %.
- [ ] **Step 3: Snapshot with the current script.** Run: `nix-shell -p openssl jq --run ./edge-snapshot.sh > $SCRATCH/edge-old.txt`. Expected: ~33 host lines and 9 probe lines.
- [ ] **Step 4: Extend the script.**
  - Add `*.page` to the FQDN `case` (`*.net|*.xyz|*.rs|*.art|*.page) fq=$h;;`). Without it, `realliance.page` becomes `realliance.page.chrismiller.xyz`.
  - Insert `git.realliance.net` and then `realliance.page www.realliance.page realliance.realliance.page` after `legacy.realliance.net`.
  - Append the two probe lines to `PROBES`.
  - Leave the header `grep` unchanged, so existing lines do not change.
- [ ] **Step 5: Verify.** Run the new script to `$SCRATCH/edge-new.txt`, then `diff <(sed 's/ cert=.*//' $SCRATCH/edge-old.txt) <(grep -v -E 'realliance\.page|^git\.realliance\.net' $SCRATCH/edge-new.txt | sed 's/ cert=.*//')`. Expected: empty diff. The new `realliance.page` lines show `000` or a TLS error (no DNS, cert or route yet). `git.realliance.net/` shows `200`.
- [ ] **Step 6: Commit and push** (docs-only): `edge-snapshot: cover realliance.page and git.realliance.net (pages Task 1)`. Tell the netpol ledger that the snapshot format changed (N3).

### Task 2: B2 bucket and key (`realliance/sso` `b2/`)

**Files:**
- Create: `~/Repos/realliance-sso/b2/pages.tf`
- Modify: `~/Repos/realliance-sso/b2/outputs.tf`, `tests/test_b2.py`

**Interfaces:**
- Consumes: `_api`, `buckets`, `master`, `_forge_key`, `rclone("forge_<name>", …)` and `_authorize_forge` from `tests/test_b2.py`. Naming the outputs `forge_pages_*` makes these work unchanged.
- Produces: bucket `realliance-pages`; key `realliance-pages`; outputs `forge_pages_key_id` and `forge_pages_key` (`sensitive = true`).

- [ ] **Step 1: Write the failing tests** in `tests/test_b2.py`:

```python
def test_pages_bucket_private_encrypted_purges_hidden(buckets):
    b = buckets["realliance-pages"]
    assert b["bucketType"] == "allPrivate"
    assert b["defaultServerSideEncryption"]["value"]["mode"] == "SSE-B2"
    assert [(r["fileNamePrefix"], r["daysFromHidingToDeleting"]) for r in b["lifecycleRules"]] == [("", 1)]

def test_pages_key_scoped(master, buckets):
    keys = {k["keyName"]: k for k in _api(master, "b2_list_keys", {"accountId": master["accountId"]}).json()["keys"]}
    k = keys["realliance-pages"]
    assert k["bucketIds"] == [buckets["realliance-pages"]["bucketId"]]
    assert set(k["capabilities"]) == {"listBuckets", "listFiles", "readFiles", "writeFiles"}

def test_pages_key_hides_but_cannot_destroy(tmp_path, buckets, master):
    # same shape as test_forge_objects_key_hides_but_cannot_destroy, with rclone("forge_pages", …),
    # bucket realliance-pages and _authorize_forge("pages"); master-key cleanup in `finally`
```

- [ ] **Step 2: Run them to verify they fail.** Run: `nix develop --command pytest tests/test_b2.py -k pages -v`. Expected: FAIL with `KeyError: 'realliance-pages'`.
- [ ] **Step 3: Implement `b2/pages.tf`** using the shapes of `b2_bucket.forge_objects` and `b2_application_key.forge_objects` in `b2/forge.tf`. Add a header comment naming the spec and D5. Bucket: `allPrivate`, SSE-B2, one `lifecycle_rules { file_name_prefix = "", days_from_hiding_to_deleting = 1 }`. Key: capabilities from Step 1. Add the two outputs.
- [ ] **Step 4: Plan.** Run: `set -a; . ~/Repos/realliance-sso-secrets/b2/secrets.env; set +a; tofu -chdir=b2 plan`. Expected: `2 to add, 0 to change, 0 to destroy`.
- [ ] **Step 5: STOP — ask Chris.** The apply uses the Realliance B2 master key. Approve / hold.
- [ ] **Step 6: Apply.** Same env, `tofu -chdir=b2 apply`. Expected: `2 added, 0 changed, 0 destroyed`.
- [ ] **Step 7: Run the tests.** Run: `nix develop --command pytest tests/test_b2.py -v`. Expected: PASS for all, including the pre-existing tests.
- [ ] **Step 8: Commit, PR, merge after Chris says merge.** `git switch -c pages-b2`; `git commit -S -m "b2: realliance-pages bucket and hide-only key"`; `gh pr create --fill`.

### Task 3: Acceptance suite, written first (`realliance/sso`)

**Files:**
- Create: `~/Repos/realliance-sso/tests/pages_helpers.py`, `tests/forge/test_forge_pages.py`

**Interfaces:**
- Consumes: `forge_api`, `forge_login`, `forge_token`, `kube`, `galaxy_only`, `VPS`, `FORGE`, `FORGE_SECRETS` (`forge_helpers`) and the `forge_user` fixture.
- Produces, in `pages_helpers`:
  - `DOMAIN = "realliance.page"`, `RELAY_URL = "http://pages-relay.pages.svc.cluster.local:8080/hook"`.
  - `site_url(owner: str, repo: str) -> str`: lower-cases; `repo.lower() == "pages"` → `https://<owner>.realliance.page/`.
  - `make_repo(owner: str, name: str, *, private: bool) -> None`: `POST /api/v1/user/repos` with `sudo=owner`, `auto_init: true` and an explicit `private`, because the forge defaults to private.
  - `commit_files(owner: str, repo: str, files: dict[str, str | bytes], *, branch: str = "pages") -> str`: contents API `POST /repos/{o}/{r}/contents`, using `new_branch` when the branch does not exist yet. Returns the commit sha.
  - `wait_for_body(url: str, needle: str, timeout: float = 90) -> float`: seconds until a verified-TLS `GET` returns 200 containing `needle`. An `AssertionError` names only the URL.
  - `port_forward(svc: str, port: int) -> ContextManager[int]`: `kubectl -n pages port-forward svc/<svc> 0:<port>`; yields the local port.
  - `metrics(svc: str, port: int) -> dict[str, float]`: Prometheus text parsed into `{"name{labels}": value}`.
  - `takedown(site: str) -> str`: `kube -n pages exec deploy/git-pages -- sh -c 'head -c 10240 /dev/zero > /tmp/empty.tar && git-pages -config /etc/git-pages/config.toml -update-site "$0" /tmp/empty.tar' <site>`. Returns the last output line (`deleted` or `no-change`).
  - `freeze(domain: str) -> None` and `unfreeze(domain: str) -> None` (`-freeze-domain` / `-unfreeze-domain`).
  - `firing_alerts() -> set[str]`: `kube -n prometheus exec <pod -l app.kubernetes.io/name=prometheus> -c prometheus -- wget -qO- http://localhost:9090/api/v1/alerts`. Returns the names of alerts that are `firing`.
  - `post_relay(payload: dict, *, sig: str | None = None) -> requests.Response`: via `port_forward("pages-relay", 8080)`, with `X-Forgejo-Event: push`. It is signed with `PAGES_WEBHOOK_SECRET` unless `sig` is given.
  - `post_git_pages(*, host: str, path: str, payload: dict) -> requests.Response`: via `port_forward("git-pages", 3000)`, with `Host: host`, `X-Forgejo-Event: push` and `Content-Type: application/json`.
  - `from_vps_head(url: str) -> str`: `ssh VPS curl -sSI <url>`, returning the header text.
  - `ensure_system_hook() -> dict`: idempotent `GET`/`POST`/`PATCH /api/v1/admin/hooks` for `RELAY_URL`, with `type: forgejo`, `content_type: json`, events `push`, `delete` and `repository`, `branch_filter: pages`, `active: true`, and the secret `PAGES_WEBHOOK_SECRET` read from `FORGE_SECRETS`. Returns `{id, active, events, branch_filter}` only, never `config`.
- Fixture `published` (module): records sites and `takedown()`s each at teardown. Purging a forge user does not remove its sites.

- [ ] **Step 1: Write the tests.** Every git subprocess uses the `test_forge_storage.py` env: askpass, `GIT_CONFIG_GLOBAL=os.devnull`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`. Key assertions:

```python
def test_system_webhook_present():                       # D1: exactly one, pointed at the relay
    hooks = [h for h in forge_api("GET", "/api/v1/admin/hooks").json() if h["config"]["url"] == RELAY_URL]
    assert len(hooks) == 1 and hooks[0]["active"] and set(hooks[0]["events"]) == {"push", "delete", "repository"}

def test_push_publishes_and_updates(forge_user, published):              # Acceptance 1
    # public repo site-<hex>; commit index.html "v1-<nonce>" to pages
    assert wait_for_body(site_url(a, repo), "v1-" + n) <= 90
    # commit "v2-<nonce>"
    assert wait_for_body(site_url(a, repo), "v2-" + n) <= 90

def test_pages_repo_is_root_site(...):                                   # Acceptance 2: <a>/pages → https://<a>.realliance.page/
def test_other_branch_changes_nothing(...):                              # Acceptance 3
    # after v1 is live: commit v9 to main and to gh-pages, wait 60 s
    assert "v1-" + n in requests.get(url, timeout=10).text
    assert git_pages_updates_after == git_pages_updates_before            # sum of git_pages_site_updates
def test_private_repo_publishes_nothing(...):                            # Acceptance 4
    assert requests.get(site_url(a, priv), timeout=10).status_code == 404  # after 60 s
    assert relay["pages_relay_events_total{outcome=\"ignored_private\"}"] == before + 1

@pytest.mark.slow
def test_oversize_site_refused_and_alerts(...):                          # Acceptance 5 (D3) + Review Focus 5
    # v1 live; git push a 520 MiB os.urandom file to pages
    assert "v1-" + n in requests.get(url, timeout=10).text                # after git_pages_site_update_error +1
    assert "PagesSiteUpdateFailing" in firing_alerts()                     # within 5 min (Monitor-style poll, 15 s)

def test_other_user_cannot_publish_on_alice_host(forge_user, published):  # Acceptance 6
    # bob publishes bob/<same repo name> "bob-<nonce>": served on bob's host only
    assert "bob-" + n not in requests.get(site_url(a, repo), timeout=10).text
    # public POST of a push payload (clone_url = bob's repo) to alice's URL
    assert requests.post(site_url(a, repo), json=payload, timeout=10).status_code == 404   # D2
    # via port_forward("pages-relay", 8080): bad X-Forgejo-Signature
    assert post_relay(payload, sig="00" * 32).status_code == 401
    # via port_forward("git-pages", 3000) with Host alice: payload naming bob's clone URL
    assert post_git_pages(host=a_host, path=f"/{repo}/", payload=payload).status_code == 401
    assert "v1-" + n in requests.get(site_url(a, repo), timeout=10).text

def test_no_set_cookie_anywhere(...):                                    # Acceptance 7
    # site whose _headers sets "Set-Cookie: x=1" and "X-Content-Type-Options: none" on /*
    for u in (root, project, project + "missing", "https://realliance.page/", "https://www.realliance.page/"):
        for r in (requests.get(u, allow_redirects=False, timeout=10), requests.head(u, allow_redirects=False, timeout=10)):
            assert "set-cookie" not in {k.lower() for k in r.headers}
    assert r_proj.headers["X-Content-Type-Options"] == "nosniff"
    assert r_proj.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert r_proj.headers["Strict-Transport-Security"] == "max-age=31536000"
    assert "set-cookie" not in from_vps_head(project).lower()               # ssh VPS curl -sSI: the internet path

def test_realliance_net_hosts_unaffected():                              # Acceptance 8 (D11), green before and after
    for h in ("git.realliance.net", "realliance.net", "www.realliance.net"):
        assert requests.get(f"https://{h}/", timeout=10).status_code in (200, 301, 302, 308)
    hosts = kube("get", "ingress", "-A", "-o", "jsonpath={..host}").stdout.split()
    assert sorted(h for h in hosts if h.endswith("realliance.net")) == \
        ["git.realliance.net", "legacy.realliance.net", "realliance.net", "www.realliance.net"]

def test_mixed_case_repo_publishes_lowercased(...):   # RF1: repo "My-Site-<hex>" → site_url(a, "my-site-<hex>")
def test_dot_repo_publishes_nothing(...):             # RF2: ".profile" with a pages branch → ignored_name +1, no update error
def test_branch_delete_requests_takedown(...):        # RF3: DELETE the pages branch → site still 200; unpublish_requested +1
def test_apex_and_www_redirect():                     # RF4
    for u in ("https://realliance.page/x", "https://www.realliance.page/x"):
        r = requests.get(u, allow_redirects=False, timeout=10)
        assert (r.status_code, r.headers["Location"]) == (302, "https://realliance.realliance.page/x")
def test_takedown_and_freeze(...):                    # spec Operations: takedown() → 404; freeze, push v2 → still 404; unfreeze in finally
def test_pull_mirror_sync_publishes(...):             # builder check: POST /repos/migrate (mirror of <a>/src), push v2 to src,
                                                      # POST /repos/<a>/<m>/mirror-sync → site_url(a, m) serves v2 within 120 s
@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("PAGES_DRILL") != "actions-token", reason="drill; F3")
def test_actions_token_pushes_pages(...):             # builder check, see Task 10 Step 4
```

- [ ] **Step 2: Run to verify they fail.** Run: `nix develop --command pytest tests/forge/test_forge_pages.py -v -m "not slow"`. Expected: `test_realliance_net_hosts_unaffected` PASS; `test_system_webhook_present` FAIL (`assert 0 == 1`); the rest FAIL or ERROR on name resolution for `*.realliance.page`.
- [ ] **Step 3: Commit and open a PR (do not merge).** `git switch -c pages-acceptance`; `git commit -S -m "tests: Realliance Pages acceptance suite (red until deploy)"`; `gh pr create --fill --draft`.

### Task 4: `pages-relay`

**Files:**
- Create: `images/pages-relay/go.mod` (`module github.com/ChristopherJMiller/luma-homeops/images/pages-relay`, `go 1.26`, no requires), `relay.go`, `relay_test.go`, `main.go`, `Dockerfile`
- Create: `.github/workflows/pages-relay.yml`

**Interfaces:**
- Produces (`relay.go`):
  - `type Repo struct { Name string; Private bool; CloneURL string; OwnerLogin string }`. The JSON path for `OwnerLogin` is `repository.owner.login`.
  - `type Event struct { Name, Ref, After, RefType, Action string; Repository Repo }`. `Name` is `X-Forgejo-Event`.
  - `func ParseEvent(name string, body []byte) (Event, error)`
  - `type Target struct { Host, Path string }`
  - `type Outcome string` with constants `forwarded`, `ignored_event`, `ignored_ref`, `ignored_private`, `ignored_name`, `unpublish_requested`, `bad_signature`, `bad_request` and `upstream_error`.
  - `func Route(ev Event, domain, forge string) (Target, Outcome)` is pure. The Target is set only for `forwarded` and `unpublish_requested`.
  - `func Verify(body, secret []byte, sigHex string) bool`: HMAC-SHA256, `hmac.Equal`.
  - `type Counters struct{…}`: `Inc(Outcome)` and `WriteTo(io.Writer)`. Output is `pages_relay_events_total{outcome="<o>"} <n>` for **every** outcome, starting at 0.
  - `type Relay struct { Secret []byte; Upstream *url.URL; Domain, Forge string; Client *http.Client; Counters *Counters; Log *slog.Logger }`, with `ServeHTTP` on `POST /hook`.
- Produces (`main.go`): env `PAGES_RELAY_LISTEN` (`:8080`), `PAGES_RELAY_UPSTREAM`, `PAGES_RELAY_DOMAIN` (`realliance.page`), `PAGES_RELAY_FORGE` (`https://git.realliance.net/`) and `PAGES_RELAY_SECRET_FILE` (`/etc/pages-relay/webhook-secret`). Routes `/hook`, `/metrics` and `/healthz`. The relay exits non-zero with `PAGES_RELAY_SECRET_FILE` in the message if the file is missing or empty.

`Route` rules, in this order:
1. `Name` is not one of `push`/`delete`/`repository` → `ignored_event`.
2. `Private` → `ignored_private`.
3. `CloneURL` does not start with `forge` (case-insensitive) → `bad_request`.
4. `owner = lower(OwnerLogin)`. Unless `owner` matches `^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$` and is not `www` → `ignored_name`.
5. `repo = lower(Name)`. A leading `.` → `ignored_name`. `Path` is `/` if `repo == "pages"`, otherwise `/<repo>/`. `Host` is `<owner>.<domain>`.
6. By event:
   - `push`: `Ref != "refs/heads/pages"` → `ignored_ref`; `After` all zeros (40 or 64) → `unpublish_requested`; otherwise `forwarded`.
   - `delete`: `RefType == "branch"` and `Ref == "pages"` → `unpublish_requested`; otherwise `ignored_ref`.
   - `repository`: `Action == "deleted"` → `unpublish_requested`; otherwise `ignored_event`.

`ServeHTTP` behaviour:
- The body is read through `http.MaxBytesReader` at 16 MiB (git-pages' own cap); more → 413 `bad_request`.
- A bad signature → 401, with no parse and no upstream call.
- `forwarded`: `POST <upstream><Path>` with `req.Host = Host`, `Content-Type: application/json` exactly (git-pages rejects parameters), `X-Forgejo-Event: push`, and the body byte-identical. The relay answers with git-pages's status and its first 4 KiB of body. A transport error → 502 `upstream_error`.
- Every other outcome → 200 with `ignored: <outcome>` or the unpublish notice.
- Log one slog JSON line per event: `outcome`, and `host`/`path` only for public repos (D9).

- [ ] **Step 1: Re-confirm D1 before writing code.** Run: `grep -n 'GetSite\|Repository.CloneURL' $SCRATCH/gp-0.10.1/src/pages.go` (shallow clone of tag `v0.10.1` with `GIT_CONFIG_GLOBAL=/dev/null`). Expected: `postPage` calls `GetSite(r)` (Host + path; 0.10.1 replaced `getWebRoot`) and reads only `ref` and `repository.clone_url` from the body. Ledger: "D1 confirmed: relay". If git-pages ever routes by payload, stop and re-plan.
- [ ] **Step 2: Write the failing tests** in `relay_test.go`:
  - `TestRouteLowercases`: owner `Test-Ab12`, repo `My-Site` → `{test-ab12.realliance.page, /my-site/}`, `forwarded`. Repo `Pages` → path `/`.
  - `TestRouteRejectsNames` (table → `ignored_name`): owner `a.b`, `a_b`, 64×`a`, `www`, `-x`, `x-`; repo `.profile`. Also clone URL `https://evil.example/a/b.git` → `bad_request`.
  - `TestRouteIgnores`: ref `refs/heads/main` → `ignored_ref`; private → `ignored_private`; event `issues` → `ignored_event`; `repository` with action `created` → `ignored_event`.
  - `TestRouteUnpublish`: `delete` branch `pages`, `push` with `After` of 40 zeros, and `repository` with action `deleted` → `unpublish_requested` with its target.
  - `TestVerify`: valid lowercase hex → true; one flipped byte, empty and non-hex → false.
  - `TestServeForwards`: an `httptest` upstream asserts Host, path, `Content-Type == "application/json"`, `X-Forgejo-Event == "push"` and a byte-identical body. It returns 202 `updating`; the relay must answer 202 with body `updating`.
  - `TestServeBadSignature`: 401; the upstream is called 0 times; `bad_signature` is 1.
  - `TestServeBodyCap`: a 16 MiB + 1 byte body → 413, and the upstream is never called.
  - `TestPrivateLogsNoNames`: for a private repo `secret-org/hidden`, the captured log contains neither `secret-org` nor `hidden`.
  - `TestMetricsAllOutcomesStartAtZero`: 9 lines with ` 0` before any request.
  - `TestMainRefusesWithoutSecret`.
- [ ] **Step 3: Run to verify they fail.** Run: `cd images/pages-relay && taskset -c 0-3 nix develop ~/Repos/luma-homeops --command env GOMAXPROCS=4 go test -race -p=4 ./...`. Expected: FAIL (undefined: `Route`, …).
- [ ] **Step 4: Implement** `relay.go` and `main.go` with the stdlib only (`net/http`, `crypto/hmac`, `encoding/json`, `log/slog`).
- [ ] **Step 5: Run the tests.** Same command. Expected: `ok`, with no race reports.
- [ ] **Step 6: Dockerfile and CI.** The Dockerfile follows `images/mcp-jwt-auth/Dockerfile` with the two pinned bases from Global Constraints, `CGO_ENABLED=0`, `USER nonroot:nonroot`, `EXPOSE 8080` and `ENTRYPOINT ["/pages-relay"]`. The workflow is a copy of `.github/workflows/mcp-jwt-auth.yml` with these changes:
  - paths `images/pages-relay/**`;
  - `IMAGE: ghcr.io/${{ github.repository_owner }}/pages-relay`;
  - a `go test ./...` step before the build (`actions/setup-go` with `go-version-file: images/pages-relay/go.mod`);
  - a smoke test asserting that `docker run --rm "$img"` prints `PAGES_RELAY_SECRET_FILE`.
- [ ] **Step 7: Commit and push** (not cluster-affecting): `pages-relay: forward Forgejo pushes to git-pages by site (pages Task 4)`.
- [ ] **Step 8: Get the pin.** Monitor the workflow run (`gh run watch`). Expected: success. Then run `gh api /users/ChristopherJMiller/packages/container/pages-relay/versions --jq '.[0] | {name, tags: .metadata.container.tags}'`. Expected: digest `sha256:<64>` with tags `sha-<7>` and `latest`.
- [ ] **Step 9: Check that anonymous pull works.** Run: `curl -sS 'https://ghcr.io/token?scope=repository:christopherjmiller/pages-relay:pull' | jq -r .token | head -c 8`, then a manifest HEAD with that token. Expected: 200. On 401: **STOP — ask Chris** to set the package to public in the GitHub UI (there is no API for it), then retry.

### Task 5: The `pages` app (first Argo app)

**Files:**
- Create: `scripts/test_pages_manifests.py`, `cluster/applications/pages.yaml`, `cluster/pages/kustomization.yaml`, `namespace.yaml`, `config.toml`, `git-pages.yaml`, `pages-relay.yaml`, `monitoring.yaml`, `git-pages-s3.secret.yaml` + sealed `git-pages-s3.yaml`, `pages-relay-webhook.secret.yaml` + sealed `pages-relay-webhook.yaml`
- Modify: `cluster/applications/kustomization.yaml` (add `pages.yaml`), `.pre-commit-config.yaml` (hook `pages-manifests`), `.renovaterc.json`

**Interfaces:**
- Consumes: the relay pin (Task 4 Step 8); `forge_pages_key_id`/`forge_pages_key` (Task 2).
- Produces:
  - `render(path) -> list[dict]`: the `kustomize build` helper in `test_pages_manifests.py`, in the same shape as `test_netpol_manifests.py`'s.
  - Service DNS names `git-pages.pages.svc.cluster.local:3000` and `pages-relay.pages.svc.cluster.local:8080`.
  - Alerts `PagesDown`, `PagesSiteUpdateFailing`, `PagesRelayUpstreamError`, `PagesBadSignatures` and `PagesTakedownRequested`.

- [ ] **Step 1: Write the failing static tests** in `scripts/test_pages_manifests.py`. The docstring names the spec and states "never reads `*.secret.*`".

```python
GIT_PAGES_IMAGE = "codeberg.org/git-pages/git-pages:0.10.1@sha256:882733e3fc4b5cde8cb3f83b786f8b85854c9ad951662b64fca23cbd34f79dc9"
RELAY_IMAGE_RE = r"^ghcr\.io/christopherjmiller/pages-relay:sha-[0-9a-f]{7}@sha256:[0-9a-f]{64}$"

def test_app_registered():        # applications/pages.yaml: path cluster/pages, ns pages, prune+selfHeal+allowEmpty,
                                  # CreateNamespace=true, no resources-finalizer; listed in applications/kustomization.yaml
def test_namespace_restricted():  # labels["pod-security.kubernetes.io/enforce"] == "restricted"
def test_images_pinned():         # git-pages == GIT_PAGES_IMAGE; relay matches RELAY_IMAGE_RE; no other images
def test_pods_hardened():         # each Deployment: replicas 1; automountServiceAccountToken False; enableServiceLinks False;
                                  # pod runAsNonRoot True, runAsUser 65532, seccompProfile RuntimeDefault; every container
                                  # allowPrivilegeEscalation False, readOnlyRootFilesystem True, capabilities.drop == ["ALL"],
                                  # memory request+limit set; no hostPath/hostNetwork/privileged; git-pages mounts emptyDir
                                  # /tmp with sizeLimit "2Gi"
def test_git_pages_config():      # tomllib of the generated ConfigMap's config.toml:
    w = cfg["wildcard"]
    assert w == [{"domain": "realliance.page", "clone-url": "https://git.realliance.net/<user>/<project>.git",
                  "index-repo": "pages", "index-repo-branch": "pages"}]          # no "authorization" (D7)
    assert cfg["features"] == [] and cfg["server"] == {"pages": "tcp/:3000", "proxy": "-", "metrics": "tcp/:3002"}
    L = cfg["limits"]
    assert L["max-site-size"] == "128MB" and L["allowed-repository-url-prefixes"] == ["https://git.realliance.net/"]
    assert not L["allow-basic-auth"] and "set-cookie" not in {h.lower() for h in L["allowed-custom-headers"]}
    assert set(L["allowed-custom-headers"]) == ALLOWED_HEADERS                 # D7 list, exact
    assert cfg["storage"]["s3"]["bucket"] == "realliance-pages" and "secret-access-key" not in cfg["storage"]["s3"]
def test_relay_env():             # PAGES_RELAY_UPSTREAM/DOMAIN/FORGE/SECRET_FILE exact; secret mounted read-only from pages-relay-webhook
def test_monitors_and_rules_labelled():  # both ServiceMonitors and pages-alerts carry release: prometheus; alert names exact
def test_update_failing_alert_catches_new_series():  # RF5: PagesSiteUpdateFailing expr contains
                                  # "unless git_pages_site_update_error offset 30m" (a brand-new series has no increase())
def test_no_certificate_in_pages():      # no kind Certificate and no tls.secretName anywhere in cluster/pages
def test_renovate_never_automerges_pages():  # .renovaterc.json: rules for both images with automerge false
```

- [ ] **Step 2: Run to verify they fail.** Run: `nix develop --command python -m pytest -q -p no:cacheprovider scripts/test_pages_manifests.py`. Expected: FAIL (`kustomize build cluster/pages` fails: no such directory).
- [ ] **Step 3: Sizing pre-check (spec "Size").** Ledger `kubectl describe nodes` memory requests (Task 1 Step 2). Initial requests: git-pages `cpu 10m`, `memory 64Mi`, limit `memory 1Gi`; relay `cpu 5m`, `memory 16Mi`, limit `memory 64Mi`. That is 80Mi in total, about 0.26 % of one node's ~30.3 GiB allocatable, so no node moves more than 1 point. Task 10 Step 6 sets the final values from measurements.
- [ ] **Step 4: Write the secrets** without opening any `.secret.yaml`:
  - `git-pages-s3`: run `umask 077`, then pipe `printf 'PAGES_STORAGE_S3_ACCESS_KEY_ID=%s\nPAGES_STORAGE_S3_SECRET_ACCESS_KEY=%s\n'` with the two `tofu -chdir=b2 output -raw forge_pages_key_id|forge_pages_key` values (B2 env sourced as in Task 2) into `kubectl create secret generic git-pages-s3 -n pages --dry-run=client -o yaml --from-env-file=/dev/stdin > cluster/pages/git-pages-s3.secret.yaml`.
  - `pages-relay-webhook`: `openssl rand -hex 32` is appended as `PAGES_WEBHOOK_SECRET=…` to `~/Repos/realliance-sso-secrets/forge/secrets.env` (commit -S and push sso-secrets; covered by the start OK). Build `pages-relay-webhook` (key `webhook-secret`) from that file the same way.
  - Seal both: `KUBECONFIG=/tmp/galaxy-kubeconfig nix develop --command ./sign.sh`. Expected: it prints both paths, and `git-pages-s3.yaml` and `pages-relay-webhook.yaml` exist with `kind: SealedSecret`.
- [ ] **Step 5: Implement the manifests.**
  - `config.toml` (D3, D7) feeds a kustomize `configMapGenerator` `git-pages-config`, so any config change rolls the pod (git-pages reads config only at start):

```toml
log-format = "json"
features = []
[server]
pages = "tcp/:3000"
proxy = "-"
metrics = "tcp/:3002"
[[wildcard]]
domain = "realliance.page"
clone-url = "https://git.realliance.net/<user>/<project>.git"
index-repo = "pages"
index-repo-branch = "pages"
[storage]
type = "s3"
[storage.s3]
endpoint = "s3.us-east-005.backblazeb2.com"
region = "us-east-005"
bucket = "realliance-pages"
[storage.s3.blob-cache]
max-size = "64MB"
[limits]
max-site-size = "128MB"
update-timeout = "5m"
concurrent-uploads = 16
allowed-repository-url-prefixes = ["https://git.realliance.net/"]
allowed-custom-headers = ["Content-Security-Policy", "Cross-Origin-Embedder-Policy", "Cross-Origin-Opener-Policy", "Cross-Origin-Resource-Policy", "Access-Control-Allow-Origin", "Permissions-Policy", "X-Frame-Options", "X-Robots-Tag"]
allow-basic-auth = false
```

  - `git-pages.yaml`: a Deployment with `command: ["/bin/git-pages", "-config", "/etc/git-pages/config.toml"]`, `envFrom` from secret `git-pages-s3`, and env `TMPDIR=/tmp` and `HOME=/tmp`. Liveness is `httpGet :3002 /metrics`; readiness is `tcpSocket :3000`. The Service `git-pages` exposes ports `pages` and `metrics`.
  - `pages-relay.yaml`: a Deployment and a Service `pages-relay` (port `http` 8080). Probes `httpGet /healthz`.
  - `monitoring.yaml`: two ServiceMonitors (`release: prometheus`), for git-pages port `metrics` and pages-relay port `http` `/metrics`, plus PrometheusRule `pages-alerts` (`release: prometheus`, all `severity: warning`):
    - `PagesDown`: `up{namespace="pages"} == 0 or absent(up{namespace="pages",job="git-pages"}) or absent(up{namespace="pages",job="pages-relay"})`, `for: 10m`.
    - `PagesSiteUpdateFailing`: `(sum by (cause) (increase(git_pages_site_update_error[30m])) > 0) or (sum by (cause) (git_pages_site_update_error unless git_pages_site_update_error offset 30m) > 0)`. Annotation: often the 128 MB cap; see `kubectl -n pages logs deploy/git-pages`.
    - `PagesRelayUpstreamError`: `increase(pages_relay_events_total{outcome="upstream_error"}[30m]) > 0`.
    - `PagesBadSignatures`: `increase(pages_relay_events_total{outcome="bad_signature"}[30m]) > 0`.
    - `PagesTakedownRequested`: `increase(pages_relay_events_total{outcome="unpublish_requested"}[6h]) > 0`. Annotation points to `docs/pages.md` § Takedown.
  - `.renovaterc.json`: two `kubernetes` rules with `automerge: false` for `codeberg.org/git-pages/git-pages` (description: pre-1.0, read every changelog, ≥ 2 weeks old, re-run the pages suite) and `ghcr.io/christopherjmiller/pages-relay` (description: decides which host a push publishes to; bump by hand).
  - `.pre-commit-config.yaml`: hook `pages-manifests`, entry `python -m pytest -q -p no:cacheprovider scripts/test_pages_manifests.py`, files `^(cluster/(pages|traefik|dns|cert-manager|applications)/.*|cloudflare/dns/.*\.tf|scripts/test_pages_manifests\.py|\.renovaterc\.json|\.pre-commit-config\.yaml)$`.
- [ ] **Step 6: Run the tests.** Run: the Step 2 command plus `scripts/check-kustomizations.sh`. Expected: PASS and exit 0.
- [ ] **Step 7: Commit:** `pages: git-pages + pages-relay, internal only (pages Task 5)`.
- [ ] **Step 8: Gate G "before", then STOP — ask Chris (first Argo app for pages).** Watch: app `pages` Synced/Healthy and both pods Ready. Rollback triggers: CrashLoop, a B2 auth error in logs, or any change in Gate G4.
- [ ] **Step 9: Push.** Monitor `kubectl -n argo-cd get application pages` and `kubectl -n pages get pods` until done. Expected: Synced/Healthy, 2/2 pods `Running` and Ready.
- [ ] **Step 10: Verify internally.**
  - `kubectl -n pages logs deploy/git-pages | grep -ci error`. Expected: `0`. A B2 failure would abort the start at BucketExists.
  - `scripts/check-prometheus-rules.sh`. Expected: exit 0, and `pages-alerts` loaded.
  - Prometheus `up{namespace="pages"}`. Expected: two series, both `1`.
  - Gate G "after". Expected: identical to "before".

### Task 6: Forgejo → relay (system webhook)

**Files:**
- Modify: `cluster/applications/forgejo-release.yaml` (`gitea.config.webhook`), `scripts/test_pages_manifests.py`

**Interfaces:**
- Consumes: `RELAY_URL` and `ensure_system_hook()` (Task 3); the relay secret (Task 5).
- Produces: one active system hook for `push`, `delete` and `repository`, with branch filter `pages`.

- [ ] **Step 1: Write the failing test** `test_forgejo_webhook_allowlist`: `values["gitea"]["config"]["webhook"] == {"ALLOWED_HOST_LIST": "external,pages-relay.pages.svc.cluster.local"}`. Forgejo's default `external` blocks webhooks to private IPs. Run it. Expected: FAIL (`KeyError: 'webhook'`).
- [ ] **Step 2: Implement the `webhook:` block.** Rebase on `main` first (F1). Run the tests. Expected: PASS. Commit: `forgejo: allow webhooks to pages-relay (pages Task 6)`.
- [ ] **Step 3: Pre-create a probe repo** *before* the hook exists, to distinguish a system hook (all repos) from a default hook (new repos only). Run `forge_api("POST", "/api/v1/admin/users/forgejo-admin/repos", json={"name": "pages-probe", "private": False, "auto_init": True})`. Expected: 201. Use the break-glass local account, not the inventory-managed `realliance` org.
- [ ] **Step 4: Gate G and F1, then STOP — ask Chris.** One OK covers both actions: the forgejo-release push (Forgejo restarts, Recreate, ~1–2 min outage) and the system webhook creation. Rollback triggers: Forgejo not Ready in 10 min (revert); a hook delivery error (disable the hook).
- [ ] **Step 5: Push and Monitor** the Forgejo pod until it is Ready. Expected: `forgejo-…` 1/1 Ready, and `https://git.realliance.net/api/v1/version` returns 200.
- [ ] **Step 6: Create the hook.** Run: `cd ~/Repos/realliance-sso/tests && nix develop .. --command python -c 'from pages_helpers import ensure_system_hook; print(ensure_system_hook())'`. Expected: `{'id': N, 'active': True, 'events': [...push, delete, repository], 'branch_filter': 'pages'}`.
- [ ] **Step 7: Prove the internal path.**
  - Commit `index.html` "probe-<hex>" to `forgejo-admin/pages-probe` branch `pages` (contents API).
  - Port-forward `svc/git-pages 0:3000`, then `curl -sS -H 'Host: forgejo-admin.realliance.page' http://127.0.0.1:<p>/pages-probe/`. Expected: within 90 s the body contains `probe-<hex>`.
  - Relay `forwarded` is 1. The Forgejo admin UI → Webhooks → recent deliveries shows `200` or `202`.
  - git-pages logs show no `RemoveObject`/`AccessDenied` (D5). If they do: ledger it, add `deleteFiles` in Task 2's tf (another Task 2 STOP), and re-run.
  - Ledger: "system hook reaches pre-existing repos: yes; deleteFiles: not needed".
  - Clean up with `takedown("forgejo-admin.realliance.page/pages-probe")`. Expected: `deleted`. Keep the repo for re-tests.

### Task 7: DNS for `realliance.page`

**Files:**
- Modify: `cloudflare/dns/records.tf` (`local.zones`), `cluster/dns/cloudflareddns-config.yaml`, `scripts/test_pages_manifests.py`

**Interfaces:**
- Produces: `realliance.page A <WAN IP>` and `*.realliance.page CNAME realliance.page`, both DNS-only. The apex content is tracked by cloudflareddns.

- [ ] **Step 1: Look up the zone (read-only).** In `$SCRATCH/cf-zone.sh`, copy line 20 of `cloudflare/dns/tf.sh` verbatim into `T=…`, then run `printf 'header = "Authorization: Bearer %s"\n' "$T" | curl -sS --config - "https://api.cloudflare.com/client/v4/zones?name=realliance.page" | jq -r '.result[] | [.id,.status,.account.name] | @tsv'`. The token goes through curl's stdin config, never argv. Also list `/zones/<id>/dns_records` with `jq -r '.result[] | [.type,.name] | @tsv'`. Expected: exactly one zone line `<32-hex>  active  <account>` and no `A`/`AAAA`/`CNAME` record at the apex or `*`. If one exists, add an `import` block to `imports.tf` (existing pattern) and expect "1 to import" in Step 5. Ledger the zone id; it is not secret.
- [ ] **Step 2: Write the failing tests.**
  - `test_realliance_page_zone_declared`: in `records.tf`, `local.zones["realliance.page"]` has `id = "<that id>"` and `proxied = false`.
  - `test_cloudflareddns_tracks_realliance_page`: `CF_HOSTS`, `CF_ZONES` and `CF_RECORDTYPES` split on `;` have equal lengths, and `realliance.page` sits at the same index as its zone id with type `A`.
  - Expected: FAIL.
- [ ] **Step 3: Implement.** Add the zone after `realliance.net` with a comment (spec + amendment). Append `realliance.page;`, `<id>;` and `A;`. Run the tests. Expected: PASS. Commit: `dns: realliance.page zone (pages Task 7)`.
- [ ] **Step 4: Plan.** Run: `nix develop --command az account show >/dev/null && ./cloudflare/dns/tf.sh plan`. Expected: `Plan: 2 to add, 0 to change, 0 to destroy.` (`cloudflare_dns_record.apex["realliance.page"]` and `.wildcard["realliance.page"]`).
- [ ] **Step 5: Gate G, then STOP — ask Chris (DNS `tf apply` plus the cloudflareddns config push).**
- [ ] **Step 6: Apply.** Run: `./cloudflare/dns/tf.sh apply`. Expected: `Apply complete! Resources: 2 added, 0 changed, 0 destroyed.` Then `./cloudflare/dns/tf.sh plan`. Expected: `No changes.`
- [ ] **Step 7: Push and restart cloudflareddns.** Run: `git push`, then once Argo is Synced, `kubectl -n dns rollout restart deploy/cloudflareddns` (it reads its ConfigMap via envFrom). Monitor the rollout. Expected: 1/1 Ready, and the logs show `realliance.page` checked with no error.
- [ ] **Step 8: Verify.** `dig +short realliance.page @1.1.1.1`. Expected: `152.44.247.88` (current WAN IP). `dig +short anything.realliance.page @1.1.1.1`. Expected: `realliance.page.` then the WAN IP. Gate G "after": existing hosts identical.

### Task 8: Wildcard certificate gains `realliance.page`

**Files:**
- Modify: `cluster/traefik/wildcard-cert.yaml` (dnsNames and header comment), `scripts/test_pages_manifests.py`

- [ ] **Step 1: Write the failing tests.**
  - `test_wildcard_cert_sans`: dnsNames equal `["chrismiller.xyz", "*.chrismiller.xyz", "*.music.chrismiller.xyz", "realliance.net", "*.realliance.net", "werethemille.rs", "*.werethemille.rs", "buttert.art", "*.buttert.art", "realliance.page", "*.realliance.page"]`; `commonName == "*.chrismiller.xyz"`; issuer `letsencrypt-prod`/`ClusterIssuer`; `rotationPolicy == "Always"`.
  - `test_default_tlsstore_untouched`: `cluster/traefik/tlsstore.yaml` is the only TLSStore under `cluster/`, and `defaultCertificate.secretName == "wildcard-tls"`.
  - `test_one_certificate_covers_realliance_page`: across `cluster/**/*.yaml` (skipping `*.secret.yaml`), exactly one Certificate names `realliance.page`, and it is `traefik/wildcard-tls`.
  - Expected: `test_wildcard_cert_sans` FAILs; the other two PASS (they are guards).
- [ ] **Step 2: Implement.** Append the two names. Run the tests. Expected: PASS. Commit: `traefik: wildcard-tls covers realliance.page (pages Task 8)`.
- [ ] **Step 3: Pre-checks.**
  - `dig +short CAA realliance.page @1.1.1.1`. Expected: empty (no CAA blocks LE).
  - `kubectl -n traefik get certificate wildcard-tls -o jsonpath='{.status.revision} {.status.notAfter}'`. Ledger the result (2026-11-03: notAfter `2026-12-25T04:04:51Z`).
  - Task 7's apply proved that the shared token can write the zone.
- [ ] **Step 4: Gate G, then STOP — ask Chris.** This reissues the shared certificate for every public host. Watch: Certificate `Ready=True` with revision+1 and new SANs. Rollback trigger: an Order or Challenge `errored`/`invalid`, or not Ready within 20 min. Rollback is `git revert`; the current secret then satisfies the spec again, so cert-manager does not reissue.
- [ ] **Step 5: Push.** Monitor `kubectl -n traefik get certificate,certificaterequest,order,challenge` until `wildcard-tls` is Ready. Expected: `True`, revision+1.
- [ ] **Step 6: Verify.**
  - For each of `git.realliance.net`, `photos.chrismiller.xyz` and `realliance.page`: `echo | openssl s_client -connect 192.168.0.7:443 -servername <h> 2>/dev/null | openssl x509 -noout -ext subjectAltName -enddate`. Expected: all 11 SANs on every host, and a fresh `notAfter` about 90 days out.
  - Gate G3 diff. Expected: empty for every pre-existing host line. Only `realliance.page` lines may change (still 404, no route).

### Task 9: Edge routes and middlewares (public exposure)

**Files:**
- Create: `cluster/pages/edge.yaml`
- Modify: `cluster/pages/kustomization.yaml`, `scripts/test_pages_manifests.py`

**Interfaces:**
- Produces: Middleware `pages-headers`, Middleware `pages-apex-redirect`, IngressRoute `pages-sites` (priority 10) and IngressRoute `pages-apex` (priority 100), all in ns `pages`, entryPoints `[websecure]`, no `tls`.

Exact values:

```text
pages-sites  match:    HostRegexp(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.realliance\.page$`) && (Method(`GET`) || Method(`HEAD`)) && (!PathPrefix(`/.git-pages/`) || Path(`/.git-pages/health`))
             priority: 10   middlewares: [pages-headers]   services: [git-pages:3000]
pages-apex   match:    Host(`realliance.page`) || Host(`www.realliance.page`)
             priority: 100  middlewares: [pages-headers, pages-apex-redirect]   services: [git-pages:3000]
pages-headers        headers: customResponseHeaders {Set-Cookie: "", Server: ""}; contentTypeNosniff: true;
                     referrerPolicy: strict-origin-when-cross-origin; stsSeconds: 31536000;
                     stsIncludeSubdomains: false; stsPreload: false
pages-apex-redirect  redirectRegex: regex ^https?://(?:www\.)?realliance\.page/(.*)$
                     replacement https://realliance.realliance.page/${1}; permanent: false
```

- [ ] **Step 1: Write the failing tests.** Local helpers `route(name)`, `mw(name)`, `ingressroutes(ns)` and `all_pages_routes()` read `render("cluster/pages")`. `SITES_MATCH` is the exact `pages-sites` match string above.

```python
def test_sites_route_get_head_only():           # the POST restriction (D2)
    r = route("pages-sites"); assert r["match"] == SITES_MATCH and r["priority"] == 10
def test_apex_redirect_outranks_sites():        # RF4
    assert route("pages-apex")["priority"] > route("pages-sites")["priority"]
    assert route("pages-apex")["middlewares"] == [{"name": "pages-headers"}, {"name": "pages-apex-redirect"}]
def test_headers_strip_set_cookie():            # Acceptance 7, static half
    h = mw("pages-headers")["spec"]["headers"]
    assert h["customResponseHeaders"]["Set-Cookie"] == "" and h["contentTypeNosniff"] is True
    assert (h["stsSeconds"], h["stsIncludeSubdomains"], h["stsPreload"]) == (31536000, False, False)
    assert all(rt["middlewares"][0] == {"name": "pages-headers"} for rt in all_pages_routes())
def test_pages_routes_have_no_tls_secret():     # amendment: shared cert via the default TLSStore
    assert all("tls" not in ir["spec"] for ir in ingressroutes("pages"))
def test_nothing_else_routes_realliance_page(): # no networking.k8s.io Ingress host ending realliance.page anywhere in
                                                # cluster/, and no IngressRoute outside cluster/pages mentions realliance\.page
```

- [ ] **Step 2: Run to verify they fail.** Expected: FAIL (no route `pages-sites`).
- [ ] **Step 3: Implement `edge.yaml`** with the values above and add it to the kustomization. Run the tests. Expected: PASS. Commit: `pages: public GET/HEAD routes, headers, apex redirect (pages Task 9)`.
- [ ] **Step 4: Gate G "before", then STOP — ask Chris (Traefik middlewares and routes; `*.realliance.page` goes public).** Rollback triggers: any change in a non-`realliance.page` snapshot line, or Traefik `ERR` logs rising.
- [ ] **Step 5: Push.** Monitor until app `pages` is Synced. Then run `kubectl -n traefik logs -l app.kubernetes.io/name=traefik --since=10m | grep -c ERR`. Expected: `0`.
- [ ] **Step 6: Verify.**
  - Gate G3 diff. Expected: changes only on the four new host lines and two probes:
    - `realliance.page/` and `www.realliance.page/` → `302 loc=https://realliance.realliance.page/`;
    - `realliance.realliance.page/` → `404`;
    - `POST realliance.realliance.page/` → `404`;
    - `GET …/.git-pages/manifest.json` → `404`.
  - `curl -sSI https://realliance.realliance.page/`. Expected: no `set-cookie` and no `server` header; `strict-transport-security: max-age=31536000`.

### Task 10: Acceptance green, builder checks, sizing

**Files:**
- Modify: `~/Repos/realliance-sso/tests/forge/test_forge_pages.py` (only if a builder check rules otherwise), `cluster/pages/git-pages.yaml`, `cluster/pages/pages-relay.yaml` (requests), the ledger

- [ ] **Step 1: Run the suite.** Run: `cd ~/Repos/realliance-sso && nix develop --command pytest tests/forge/test_forge_pages.py -v -m "not slow"`. Expected: all PASS. Ledger the two publish latencies (Acceptance 1, ≤ 90 s).
- [ ] **Step 2: Run the slow test.** Tell Chris that `PagesSiteUpdateFailing` will fire once, then run `pytest tests/forge/test_forge_pages.py -v -k oversize`. Expected: PASS. Ledger git-pages' peak memory during the run (Step 6).
- [ ] **Step 3: Mirror ruling.** If `test_pull_mirror_sync_publishes` FAILs because no delivery arrives after `mirror-sync` (check the relay's `forwarded` count): rewrite it to assert the site is *not* updated, ledger "pull-mirror syncs fire no push event", and have `docs/pages.md` say mirrors do not publish. Re-run. Expected: PASS either way, with the ruling ledgered.
- [ ] **Step 4: Actions-token drill (F3). STOP — ask Chris** (it publishes briefly at `realliance.realliance.page/forge-bot/` and uses the shared runner).
  - The test calls `push_probe_workflow("realliance/forge-bot", "galaxy", script)`. `script` writes `index.html` "tok-<hex>" into a fresh repo and runs `git -c http.extraHeader="Authorization: token ${{ github.token }}" push https://git.realliance.net/realliance/forge-bot.git HEAD:refs/heads/pages`.
  - Asserts `wait_for_body("https://realliance.realliance.page/forge-bot/", "tok-<hex>", 180)`.
  - Teardown deletes the `pages` and probe branches and runs `takedown("realliance.realliance.page/forge-bot")`.
  - Run: `PAGES_DRILL=actions-token pytest tests/forge/test_forge_pages.py -v -k actions_token`. Expected: PASS → the sample workflow uses the automatic token.
  - If the push is refused (403): ledger it; the sample workflow then uses a repo-owner token secret, and the test asserts the 403.
- [ ] **Step 4b: State survives a pod loss (spec Goals).** Publish a fresh site with the Acceptance 1 test, then run `kubectl -n pages delete pod -l app.kubernetes.io/name=git-pages` (an S2 exception). Monitor until the new pod is Ready, then `curl -sS` the site. Expected: the same content, served by the new pod with no republish (state is in B2).
- [ ] **Step 5: Acceptance 8, luma side.**
  - `./cloudflare/dns/tf.sh plan`. Expected: `No changes.`
  - Gate G3 versus Task 1's `$SCRATCH/edge-old.txt` (with `grep -v` on the new lines). Expected: an empty diff, apart from lines another plan's ledger explains.
- [ ] **Step 6: Sizing (spec "Size").**
  - Query Prometheus over the test window: `max_over_time(container_memory_working_set_bytes{namespace="pages",container=~"git-pages|pages-relay"}[6h])` and `max_over_time(rate(container_cpu_usage_seconds_total{namespace="pages"}[5m])[6h:1m])`. Ledger both, and `kubectl top pod -n pages` at idle.
  - Set each memory request to `max(16Mi, idle working set × 1.25)` and each CPU request to `max(5m, idle × 1.25)`.
  - Keep limits at git-pages 1Gi (raise it to the oversize-run peak × 1.5 if that is higher) and relay 64Mi.
  - Run the static tests. Expected: PASS. Commit and push (routine, start OK): `pages: requests from measurement (pages Task 10)`. Monitor the rollout. Expected: Ready.
- [ ] **Step 7: Mark the PR ready.** Run `gh pr ready` and ask Chris to merge `pages-acceptance`. Merge only on his word.

### Task 11: Docs

**Files:**
- Create: `docs/pages.md`
- Modify: `CLAUDE.md` (topology: a new "Realliance Pages" row, and `realliance.page` in the Public DNS row), `docs/forgejo.md` (one-line pointer under "Where things live"), `cloudflare/dns/README.md` (design table), `.claude/skills/edge-ingress/SKILL.md` (five zones, 11 SANs, the `cluster/pages/edge.yaml` IngressRoute exception and why), `.claude/skills/cloudflare-dns/SKILL.md` (zone list)

- [ ] **Step 1: Write `docs/pages.md`** with these sections:
  - **Publishing:**
    - public repo, `pages` branch, URLs lower-cased;
    - the forge defaults new repos to *private*;
    - forks publish like any repo; archived repos keep their last site until an Owner takes it down;
    - a renamed or transferred repo publishes under its new name, and the old URL stays until takedown;
    - names that get no site;
    - `_headers` allowlist (D7), `_redirects`, `404.html`, `/.git-pages/health`;
    - Forgejo Actions sample workflow (per the Task 10 Step 4 ruling) and GitHub Actions variant (Forgejo token as a GitHub secret; caveat: Forgejo tokens are not repo-scoped);
    - pull-mirror behaviour (per the Task 10 Step 3 ruling).
  - **Private-repo opt-in:** CI pushes to a public `<name>-pages` repo.
  - **Quota:** the 128 MB cap; how a refused publish looks (`202` in the delivery, `PagesSiteUpdateFailing`).
  - **Takedown:**
    1. `kubectl -n pages exec deploy/git-pages -- sh -c 'head -c 10240 /dev/zero > /tmp/empty.tar && git-pages -config /etc/git-pages/config.toml -update-site <owner>.realliance.page[/<repo>] /tmp/empty.tar'`.
    2. `-freeze-domain <owner>.realliance.page` to stop republishing. Freeze *after* deleting: a frozen domain refuses deletes too.
    3. `-unfreeze-domain` to undo.
    4. `PagesTakedownRequested` → find the site in the relay logs.
  - **Flows:** the N1 inventory.
  - **Operations:** alerts; the system hook (`ensure_system_hook()` re-creates it; rotating the secret = sso-secrets + reseal + re-run); B2 hidden versions purge after 1 day; unreachable blobs are never collected (`git-pages -trace-garbage` estimates them); WAN-IP hairpin dependency.
  - **Bumping git-pages:** read every changelog in range; ≥ 2 weeks old; re-run the suite.
  - **PSL status.**
- [ ] **Step 2: Verify.** Run: `grep -rn 'realliance.page' CLAUDE.md docs/forgejo.md cloudflare/dns/README.md .claude/skills/*/SKILL.md | wc -l`. Expected: ≥ 5. Run `grep -rnE 'secret|token' docs/pages.md`. Expected: names only, no values.
- [ ] **Step 3: Commit and push** (docs-only): `docs: Realliance Pages runbook (pages Task 11)`.

### Task 12: (folded into Task 5) git-pages 0.10.1 notes

Chris waived the pin-age rule on 2026-11-03, so Task 5 ships 0.10.1 with `[server].proxy`. This task is now docs only: read the 0.10.0 and 0.10.1 release notes, ledger the breaking items (`caddy` → `proxy`; SIGHUP gone; project-site `_headers`/`_redirects` paths start with the project path; Forgejo redirects are not followed; `-delete-site` exists for takedown; sites are cached ~60 s, so tests poll), and add the `_headers`/`_redirects` note to `docs/pages.md`. No cluster change, no STOP.

### Task 13: Public Suffix List (last, gated)

**Files:**
- Modify: `cloudflare/dns/records.tf` (explicit `_psl` TXT), `docs/pages.md` (PSL status)

- [ ] **Step 1: STOP — ask Chris to extend the registration** to at least 2 years beyond the planned submission date (RDAP today: registered 2026-11-03, expires 2027-11-03). Then run `curl -sS https://pubapi.registry.google/rdap/domain/realliance.page | jq -r '.events[] | select(.eventAction=="expiration") | .eventDate'`. Expected: a date ≥ submission date + 2 years + 30 days.
- [ ] **Step 2: Pre-submission checks.** Task 10 is green and the service is live with at least one real site (the PSL declines test projects). Draft the PR body: the PRIVATE-section entry `realliance.page` with the header `// Realliance Pages : https://realliance.page` and `// Submitted by <name> <email>`; the use case (mutually untrusting owners' subdomains, as `codeberg.page`/`github.io`); the `_psl` TXT; ≥ 2 years registration. The name and email go into the public PSL, so Chris chooses them.
- [ ] **Step 3: STOP — Chris opens the PR** on `github.com/publicsuffix/list` from his account (`gh` acts as ChristopherJMiller), with the drafted text. Ledger the PR number `N`.
- [ ] **Step 4: Write the failing test.** `test_psl_record`: `records.tf` declares `cloudflare_dns_record.psl` with name `_psl.realliance.page`, type `TXT`, content `"https://github.com/publicsuffix/list/pull/N"`, `proxied = false`. Expected: FAIL. Implement it, run it (Expected: PASS) and commit.
- [ ] **Step 5: Plan.** `./cloudflare/dns/tf.sh plan`. Expected: `1 to add, 0 to change, 0 to destroy`.
- [ ] **Step 6: STOP — ask Chris (DNS tf apply).** Then apply. Expected: `1 added`. Then `dig +short TXT _psl.realliance.page @1.1.1.1`. Expected: the PR URL. A follow-up plan returns `No changes.`
- [ ] **Step 7: Record the status** in docs/pages.md (submitted date, PR, "record must stay"). Until browsers ship the list: no cookies, and HSTS without `includeSubDomains`. Commit and push.
