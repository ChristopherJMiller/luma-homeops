# CI caching for the runner classes: design

> **Amends** the runner-classes spec (`2026-11-04-runner-classes-design.md`): its non-goal of a shared
> actions cache or any cache service, the `tiny` row's cache off, and the ACI object row. It also
> amends the phase 2 plan: the class image contract (Task 1), the ARM guard's PUT body (Task 7), and the
> money numbers *(Chris, 2026-11-05)*. The global paid cap goes from $100 to $80, in the binary's ceiling
> and in `ci.yaml`. The CI group budget goes from $105 to $85, in `fence.json` (Task 2) and in the
> plan's tests.

- **Date:** 2026-11-05.
- **Status:** approved by Chris, 2026-11-05. Nothing built.
- **Research:** `ci-cache-research.md` (session scratchpad, out of git); this doc restates every finding
  it relies on. Also a read of forgejo-runner v13.2.1 (`act/cacheproxy/handler.go`,
  `act/artifactcache/{handler,caches,mac,model}.go`, `internal/app/run/runner.go`,
  `internal/pkg/config/config.go`), Go 1.26's `net/http/httputil`, and the Azure retail price feed for
  `westus3` on 2026-11-05.
- **Related:** runner-classes spec; phase 2 plan `2026-11-05-runner-classes-phase-2.md`; NetworkPolicy
  spec §6.2 and §6.3.
- **Markers:** *(Chris)* marks a decision Chris made on 2026-11-05. *(Claude's call)* marks a default
  this doc chose; each one is open to change.

## Design summary

| Piece | Runs on | Serves | Holds |
|---|---|---|---|
| `ci-cache` host | galaxy, namespace `ci-cache` | `tiny` | `actions/cache` entries, per owner and repo |
| `ci-cache` host | Azure App Service B1, group `rg-realliance-ci-cache` | `small`, `medium` | the same, a separate store |
| goproxy | galaxy, `ci-cache` | `tiny` | Go modules, read-only to jobs |
| Verdaccio | galaxy, `ci-cache` | `tiny` | npm packages, read-only to jobs |

At launch, the dispatcher derives a secret for the job from the location's master key and four
numbers: owner id, repo id, epoch, quota. It writes the secret and the host's URL, with the four
numbers in its query string, into that job's runner config. forgejo-runner's built-in cache proxy
signs every request with the secret. The host derives the same secret from the query and checks the
MAC. A job can reach only the entries its own secret unlocks.

## Intent

Cold builds are slow, and the first real consumer shows it. The fork's image build takes 15 to 21 min
on `small` with no cache; Go compile and webpack take most of that. Chris wants `actions/cache`, and the
`setup-go` and `setup-node` defaults, to work on every class the way they do on GitHub *(Chris)*. The
runner-classes spec ruled caching out because one tenant could poison the next. Chris accepts
poisoning inside an owner, since a host-mode job can always read its own secret *(Chris)*. This design
keeps the wall between owners hard, makes it a wall between repos in practice, and keeps release
builds out of the cache entirely.

## Goals

1. Every owner's repos get `actions/cache` on `tiny`, `small` and `medium` with no opt-in *(Chris)*.
2. No job can read or write another repo's entries, even as root holding its own secret.
3. A leaked secret stops working within 7 days and 2 hours, with no one acting. An emergency rotation
   ends every secret within minutes.
4. One owner can't fill a disk or starve others: a quota per owner, a total cap, and rate limits.
5. Release builds never restore or save a cache.
6. The cache is best-effort. A host that is down means a miss, never a failed build.
7. galaxy memory requests grow by at most 300 MiB. The Azure host has a fixed price, and the spec's
   worst case stays under $150 (the $80 cap, *(Chris, 2026-11-05)*).
8. Hit, miss and size numbers per owner, by id, outside Prometheus labels; totals as metrics, with alerts.

## Non-goals

- Isolation between branches, or between a pull request and its base, against a hostile job. Host mode
  makes it bypassable. The host keeps upstream's pull request isolation for honest clients.
- A cache shared across locations. A `tiny` job and a `medium` job in one repo don't share entries.
- Caching for VMs (phase 3), the `galaxy` and `general` runners, or Docker layers.
- Proxies for the Azure classes, which reach upstream registries directly. No Deno, crates, apt or image
  mirrors (research: low value now).
- Cache numbers on the fork's usage page. The stats endpoint is its future source.

## Anti-goals

- One secret shared across repos, owners or locations, which is upstream's model.
- A storage path or filename built from any client string.
- bbolt, SQLite, `flock` or `mmap` on the Azure store, which sits on SMB-backed storage.
- The dispatcher calling a cache host, or gaining a Kubernetes verb or an ARM request type for caching.
- Owner ids or names in metric labels (the spec's anti-goal).
- An administrator or purge endpoint on the network.
- Cache data on the SMR HDD pool.
- An Azure host billed per request, or reachable from the whole internet.

## Constraints

| Fact (forgejo-runner v13.2.1 unless noted) | The design |
|---|---|
| The cache proxy runs inside the runner process; the job sees only the proxy's one-time URL. The proxy adds `Forgejo-Cache-Repo` (the `owner/repo` full name), `-RunNumber` (the run id), `-Timestamp` (job start), `-MAC`, `-WriteIsolationKey` (only for `pull_request`), `-RunId` and `-Host` | the host trusts nothing but what the MAC and the key derivation cover |
| MAC = hex(HMAC-SHA256(secret, repo `>` run `>` timestamp `>` isolation key)). One proxy has one secret (`cache.secret`, or `cache.secret_url` with `file:` only). The upstream server rejects only future timestamps, so a MAC never expires | one derived secret per job; the host also rejects timestamps over 2 h old |
| The proxy calls `SetURL(external_server)`, then sets the path to `/_apis/artifactcache/...`. **That drops any base path.** Scheme, host and port stay. **The query string stays**, put before the job's own query (Go's `rewriteRequestURL`) | route by query, never by path |
| Requests carry names, never an owner id or repo id | the dispatcher puts ids in the query, bound into the key |
| Upstream server: bbolt; eviction by age only (7 d unused, 30 d old, 5 min incomplete), no size limit; its dedup pass groups by key and version across repos and isolation keys; entries are mutable (newest wins); a DB error kills the process | a new host (next section) |
| In host mode the job can read the runner config, so it can read the secret | accepted *(Chris)*; the secret unlocks one repo |
| The job listing gives owner id, repo id and `runs_on`, but no event, ref or commit | the dispatcher can't tell a tag build from any other job in the repo |
| `tiny`'s `/work` is a 1 GiB memory `emptyDir`; a restore needs the archive and the extracted tree at once | galaxy entries are at most 256 MiB |
| The kube guard checks a Secret POST's name, namespace and type, and a Job POST's suspend, deadline and token automount; neither looks at data keys or volumes | `tiny` needs no guard change |
| The ARM guard allows exactly one environment variable, `RUNNER_TOKEN`, as a `secureValue` | it changes (see The dispatcher's part) |
| galaxy runs at 84 to 89 % of memory requested; `block-pool-ssd` stores 13 GiB of about 150 usable (`ceph df`, 2026-11-05), to stay under 75 % | small requests; at most 36 GiB of SSD claims |
| ACI groups have no VNet, so they reach any host over the internet | the Azure host is public HTTPS behind a source filter |

## The host

### Options

| | (a) A new Go service | (b) A supervisor with a `forgejo-runner cache-server` child per owner | (c) A front that re-signs for one upstream `cache-server` |
|---|---|---|---|
| Routing | by query | by query, to a child's port (the proxy drops paths) | by query |
| Rotation grace | accepts epoch N and N−1 | none: a child takes one secret, and two children can't open one bbolt file | the front accepts both |
| Quota | per owner, LRU eviction inside the owner | refuse at quota only; it can't evict, so an owner stays on stale entries for up to 30 days | as (b), with no per-owner count |
| Tenants meet in | one process; validated integers pick the partition | separate processes | one bbolt file. Upstream's dedup lets one owner's entry evict another's, and one DB error stops every owner |
| Azure store | SMB-safe format | bbolt on SMB, unsafe | bbolt on SMB, unsafe |
| Memory | one process, about 30 MiB | about 30 to 50 MiB per active child | one process |
| Code to own | about 1,000 lines, stdlib only | about 300 lines plus supervision | about 300 lines |

**Pick: (a), one host per location serving every owner** *(Claude's call)*. Per-owner processes on
galaxy would need the dispatcher to create Deployments and claims, and in Azure they cost $12 or more
each. Of the shared options, only (a) evicts per owner, rotates without dropping in-flight jobs, and
stores safely on SMB. Its risks:

1. **Protocol drift.** Upstream's proxy and server are a matched pair, and the new server replaces one half.
   Mitigation: request fixtures recorded from the real proxy, C2's acceptance jobs, and a Renovate rule
   that holds any forgejo-runner bump until they re-run.
2. **One bug can reach every tenant.** Mitigation: only integers that passed key derivation and the MAC
   pick a partition, and tests sweep every route across owners and repos.
3. **More code to own** than (b) or (c), in the CI trust path.

**Where it lives** *(Claude's call)*: `images/ci-cache/` in luma-homeops, with its own `go.mod` and stdlib
only, as `images/pages-relay` is. GitHub Actions builds it to `ghcr.io/christopherjmiller/ci-cache`
(public, so App Service pulls it anonymously), and Renovate never automerges it. Deployments pin
`sha-<7>@sha256:<64>`, and each re-pin is a manual commit (CLAUDE.md sharp edge 6). The dispatcher's
derivation code is a copy. Both repos carry one test-vector file, and a test compares the two byte for
byte.

### Behaviour

- **Routes:** upstream's six (find, reserve, upload, commit, download, clean; clean is a no-op), with
  upstream's semantics. The host lowercases keys. Each key matches exactly first, then by prefix, and the
  newest entry wins. A request with an isolation key reads its own scope first, then the unisolated
  scope, and writes only its own.
- **Entries are immutable** *(Claude's call)*. A reserve for a key, version and scope that already
  exists returns 409, as on GitHub. A reserve without `cacheSize` returns 400.
- **Checks, in order, before touching storage.** Any failure returns 403 or 429 with no detail.
  1. The route, and body caps: reserve 4 KiB, upload chunk 64 MiB.
  2. The query holds exactly one each of `owner`, `repo`, `epoch` and `quota`, as canonical decimals,
     with `quota` at most the host's ceiling. The host refuses a duplicate, since a job can append its own
     `owner=` to the proxy's query.
  3. The epoch is in the window (next subsection).
  4. The MAC matches under the derived key, compared in constant time. The timestamp is under 2 h old
     and at most 5 min ahead.
  5. The owner's rate limits.
- **Storage:** `<root>/<owner_id>/<repo_id>/`, blob names from a hash and an entry id, and a JSON metadata
  file per entry, written to a temp file and renamed. The index lives in memory and is rebuilt from disk
  at start. Uploads stream to disk with offset writes.
- **`archiveLocation`** echoes `Forgejo-Cache-Host`, as upstream does, so downloads go back through the
  job's own proxy.

### Key derivation and rotation

```
secret = hex(HMAC-SHA256(master[location],
         "ci-cache v1" 0x00 location 0x00 epoch 0x00 owner_id 0x00 repo_id 0x00 quota_mib))
URL    = <base>?owner=<owner_id>&repo=<repo_id>&epoch=<epoch>&quota=<quota_mib>
```

Integers are canonical decimals, and `location` is `galaxy` or `azure`. The proxy uses the 64 hex
characters as its HMAC key.

- **The key binds the repo** *(Claude's call)*. Chris accepted poisoning inside an owner. Binding the repo
  narrows that to inside a repo, at no cost, since upstream never shares entries across repos either.
  The quota still binds the owner. Dropping `repo_id` gives per-owner keys.
- **The quota sits inside the key,** so per-owner quotas live in the signed `ci.yaml` beside the other
  limits. A job can't raise its quota, because a changed `quota` changes the key.
- **A master per location** *(Claude's call)*. An Azure host compromise yields nothing on galaxy.
- **Epoch: weekly** *(Claude's call)*: whole weeks since Monday 1970-01-05 00:00 UTC. The host accepts
  N; N−1 until 2 h after the boundary (the longest hard stop is 75 min); and N+1 from 5 min before it,
  for clock skew. A daily epoch would cost the same. Weekly matches how often anyone offboards.
- **Entries survive rotation** *(Claude's call)*. Storage keys on ids, never on the epoch, so a new
  epoch means new keys, not a cold cache. The cost: what an ex-member wrote stays until it ages out
  (30 days at most) or someone purges it.
- **Emergency rotation** replaces a master. On galaxy, reseal both copies in one commit. In Azure, Chris
  runs `terraform apply -replace` on the master, then reseals the dispatcher's copy. Jobs in flight
  miss for the rest of their run. Masters also rotate yearly.

### Limits

Every value is *(Claude's call)* and set in the host's config.

| | galaxy | Azure |
|---|---|---|
| Store | 24 GiB claim on `rook-ceph-block-ssd` | the plan's `/home`, 10 GB |
| Total cap | 20 GiB | 8 GiB |
| Per-owner quota | 2 GiB by default, 4 GiB for `realliance`, ceiling 4 GiB in code on both sides | the same |
| Largest entry | 256 MiB (`tiny`'s `/work`) | 4 GiB |
| Open reservations per owner | 8 | 8 |
| Uploads per owner per UTC day | 8 GiB | 16 GiB |
| Requests per owner | 50 a second, burst 200 | the same |

At the quota, a reserve evicts that owner's least recently used entries until the new one fits.
Reserved bytes count from the reserve. At the total cap, eviction picks the least recently used across
all owners, and each owner's quota bounds its share. Age eviction keeps upstream's numbers.

## The dispatcher's part

The dispatcher holds the masters, derives a secret at launch, and writes it into the job's runner
config. It never calls a cache host *(Claude's call)*.

- **Inputs.**
  - Public ConfigMap: `cache.galaxy` and `cache.azure`, each a `mode` (`on` or `off`, the rollback) and
    a base `url`.
  - SealedSecret `ci-dispatch-cache`, keys `master-galaxy` and `master-azure`. The canonical copies are
    `CI_CACHE_MASTER_{GALAXY,AZURE}` in sso-secrets.
  - Signed `ci.yaml`: `defaults.cache_quota_mib`, `owners.<name>.cache_quota_mib`, and
    `cache.release_repos: [{name, id}]`.
- **Per job.** A repo in `release_repos`, or a location set to `off`, gets `cache.enabled: false` and no
  secret. Every other job gets one. Denied owners never launch.
- **`tiny`.** The per-job Secret gains a key `config.yml`, the whole runner config rendered by the
  dispatcher: `runner.timeout`, `runner.envs` (the proxies), and a `cache` block with `enabled: true`,
  `external_server` (the URL from Key derivation), `secret` and `host: 127.0.0.1`. The Job mounts it in place of the
  ConfigMap `ci-tiny-runner`, which retires. **The kube guard, its 7 shapes and the Role don't change**;
  the builder's and manifests' tests do.
- **ACI.** The group gains a second secure variable, `CI_CACHE`, holding `<url> <secret>`.
  `ci-class-entry` checks it against a strict pattern, writes the secret to `/run/ci/cache-secret`
  (mode 0600), and renders `/run/ci/runner.yml`: the baked config plus the cache block, with
  `secret_url: file:///run/ci/cache-secret`. It runs `unset CI_CACHE` and passes the new config to the
  runner. A malformed value exits 2. Without `CI_CACHE`, nothing changes, and the baked configs keep
  `cache.enabled: false`.
- **The ARM guard change, exactly:**
  - `environmentVariables` is `[RUNNER_TOKEN]` or `[RUNNER_TOKEN, CI_CACHE]`, in that order, each with
    only `name` and a non-empty `secureValue`;
  - `CI_CACHE` matches `^<cache.azure.url>\?owner=<id>&repo=<id>&epoch=<n>&quota=<n> [0-9a-f]{64}$`;
  - its `owner` equals the group's `ci-owner-id` tag;
  - `TestGuardPutBody` and `TestErrorsNeverContainSecrets` gain cases.

  The 7 requests, the command's 6 elements, and the Policy fence stay as they are.
- **Metric:** `ci_dispatch_cache_keys_total{class,decision}`, with decision `issued`, `release` or `off`.

## galaxy

Every row is *(Claude's call)*, except that the proxies exist *(Chris)*.

| Item | Setting |
|---|---|
| Namespace | `ci-cache`, Pod Security `restricted`, Argo app `ci-cache` (`cluster/ci-cache/`) |
| Cache host | StatefulSet, 1 replica, distroless, non-root, read-only root. Requests 50m and 64 MiB, limits 1 CPU and 256 MiB. Port 8080 for jobs, 9090 for metrics and stats. Master from a SealedSecret |
| goproxy | `goproxy/goproxy` (lighter than Athens), upstream `https://proxy.golang.org` only, so it never runs git. Requests 20m and 64 MiB, limit 256 MiB; 6 GiB SSD claim |
| Verdaccio | uplink `registry.npmjs.org`; `publish` and `unpublish` set to `$nobody`; web UI off. Requests 20m and 160 MiB, limit 384 MiB; 6 GiB SSD claim |
| Images | pinned by digest; Renovate never automerges them |
| Memory | 288 MiB of requests, about 0.3 points of the 90 GiB allocatable. After 10 days, re-size to ceil64(1.25 × the maximum working set) |
| Policies | `default-deny`, `allow-dns`, `allow-from-prometheus` (9090 only). Ingress from `ci-tiny` pods (`app.kubernetes.io/name: ci-tiny`) on 8080, 8081 and 4873. The proxies get `egress-internet-only` on 443; the cache host gets no egress |
| `ci-tiny` side | one egress rule to `ci-cache` pods on those three ports, in the class's policy (NetworkPolicy B2) |
| Job env | `GOPROXY=http://goproxy.ci-cache.svc.cluster.local:8081\|https://proxy.golang.org,direct`; `GOPRIVATE=git.realliance.net`, which keeps private and hidden-org module paths away from both proxies; `NPM_CONFIG_REGISTRY=http://verdaccio.ci-cache.svc.cluster.local:4873/`, read by npm and Deno |

The `|` in `GOPROXY` makes the Go proxy optional: on any error, Go falls through to upstream. npm
can't fall back, so a Verdaccio outage fails `tiny` npm installs until it recovers (`CIProxyDown`). npm
rewrites lockfile URLs on `registry.npmjs.org` to the configured registry and still checks every
`integrity` hash. The Go command checks `go.sum` and the sumdb. Neither proxy can forge a package.

## Azure

The host sits **outside the CI group**, where the sweeper deletes anything older than 90 min
*(Claude's call)*. It lives in `rg-realliance-ci-cache` with a $15 budget that emails at 80 % and 100 %.
The dispatcher and the sweeper hold no rights there.

Prices are `westus3` list prices from 2026-11-05.

| Option | Monthly | TLS | Who can reach it | Worst case |
|---|---|---|---|---|
| ACI, 0.5 vCPU and 1 GiB, always on, Caddy sidecar ($0.0405 a vCPU-hour, $0.00445 a GiB-hour) | about $18, plus about $2 for Azure Files | the sidecar, with a Let's Encrypt name | everyone: no NSG without a VNet, and a VNet group gets no public IP | fixed, about $20 |
| Container Apps, 0.25 vCPU and 0.5 GiB, one replica at most, scaling to zero ($0.000024 a vCPU-second active, $0.000003 a GiB-second, $0.40 a million requests) | about $0 to 3 inside the free grant (180k vCPU-s, 360k GiB-s; not in the feed), plus about $2 for Azure Files | built in | IP ranges only, in an environment without a VNet | about $14 if busy all month, plus requests with no cap: a 1,000-a-second flood costs about $35 a day |
| **App Service B1 Linux**, custom container ($0.017 an hour) | **$12.41 fixed**, with `/home` 10 GB included | built in | access restrictions with service tags | **$12.41, fixed** |

**Pick: App Service B1** *(Claude's call)*. Only B1 has a fixed price, built-in TLS and a source
filter, so it needs no lock and no change to the sweeper.

- **Source filter:** allow the service tag `AzureCloud.WestUS3` (where ACI's outbound addresses should
  be; C3 checks) and the WAN /25 (for Prometheus), and deny everything else. That keeps downloads
  in-region, where bandwidth should be free (C3 checks the meter). The fallback is `AzureCloud`, which
  bills inter-region transfer at cents a GB.
- **App settings:** HTTPS only, TLS 1.2 or later, Always On, `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true`,
  and health check `/healthz`. The image comes from ghcr by digest. Logs go to the App Service log
  stream only, never Log Analytics.
- **Secrets:** the master is a terraform `random_password` in an app setting, plus a sensitive output
  that Chris copies to sso-secrets. The ops bearer token works the same way.
- **Terraform:** `azure/ci/cache.tf` in the existing root and state, which Chris applies. It registers
  `Microsoft.Web` if needed. Tests: no role assignment scopes to the cache group, and the dispatcher's
  role stays as it is.

**Budget arithmetic.** Logging, state and the sweeper come to about $12 a month, and the host to
$12.41. "Attacker" is the spec's row: a stolen dispatcher credential, kept under the rate trip. The
$0.60 an hour now counts egress too (phase 2's egress cap), so the 48 h term still bounds it.

| | Paid cap | CI budget | Typical ceiling (margin under $150) | Attacker row (left under $150) |
|---|---|---|---|---|
| Spec before this doc | $100 | $105 | $112 ($38) | $105 + 48 h × $0.60 + $12 = $145.80 ($4.20) |
| The host, old numbers | $100 | $105 | $124.41 ($25.59) | $158.21: over $150, so the subscription stops |
| **The host, decided** *(Chris, 2026-11-05)* | **$80** | **$85** | **$104.41 ($45.59)** | **$85 + $28.80 + $12 + $12.41 = $138.21 ($11.79)** |

## Release builds: a hard rule

**A release build never restores or saves any cache.** That covers `actions/cache`, the `setup-go` and
`setup-node` defaults (which need `cache: false`), and any GOCACHE or `node_modules` seed from an
earlier run. A release build may use the read-only proxies *(Chris)*. `release_repos` holds
`realliance/forgejo` now. `realliance/forge-bot` and `realliance-ops/ci-dispatch` join at phase 2b,
when they move onto `small` *(Chris, 2026-11-05)*. The fork's build runs on `medium`, which can't
reach galaxy, so it uses upstream registries. Toolchains and seeds baked into the class image come from the image build, not from a
tenant run, so they're allowed.

Enforcement has two layers, each enough alone *(Claude's call)*:

1. **The dispatcher withholds the secret from every job of a repo in `cache.release_repos`.** Such a job
   gets `cache.enabled: false`, so `ACTIONS_CACHE_URL` doesn't exist, and a forgotten `cache: true` only
   logs a warning. The listing has no event or ref, so the dispatcher can't pick out tag builds, and
   the whole repo goes without a cache *(Chris, 2026-11-05)*.
2. **A workflow lint in the fork,** as a required check on its protected branches and as a pre-commit
   hook. It fails if any workflow uses `actions/cache` or its `restore` or `save`, mentions
   `ACTIONS_CACHE_URL`, or has a `setup-go` or `setup-node` step without `cache: false`.

## Security, plainly

**A job can:**
- read its own secret, and so read, add to and poison its own repo's entries in its location, until
  the epoch ends;
- do that from anywhere that reaches the host while the secret lives: any `tiny` pod on galaxy, and
  `westus3` or the WAN /25 in Azure;
- fill its owner's quota, which evicts its owner's older entries.

**A job can't:**
- read or write another repo's or owner's entries, since their keys need the master;
- raise its quota, or change its owner, repo or epoch, since each changes the key;
- use a secret in the other location, which has another master;
- use a secret after its epoch's grace, or a captured MAC after 2 h;
- read the host's metrics or stats (galaxy: port 9090, closed to jobs; Azure: behind the ops token), or
  publish to a proxy.

**Residual risks:**
1. **Poisoning inside a repo** by anyone who can run a job there: members with push access, and approved
   fork pull requests. Approving a fork's workflow gives it write access to that repo's cache, and the
   member guide says so. Pull request isolation stops only honest clients.
2. **An ex-member keeps a secret** until the epoch ends, at most 7 days and 2 hours, unless someone
   rotates the master.
3. **A host compromise** exposes every entry in that location, and its master. A dispatcher compromise
   exposes both masters, which is less than its site administrator token already gives.
4. **Entries hold private repos' build output,** readable only with that repo's key.

## Abuse, offboarding and cost

| Case | What happens |
|---|---|
| One owner fills its quota | LRU eviction inside that owner; the other owners don't notice |
| Many owners fill the total | global LRU; `CICacheNearCap` fires at 85 % |
| An upload flood | the per-owner daily upload cap, 8 open reservations, 64 MiB chunks; then 429 |
| A request flood | a per-owner token bucket; a failed MAC costs one HMAC. Azure's flat price means a flood degrades service and costs nothing |
| A member leaves | the existing two steps (SSO, Forgejo prohibit login). The cache secret dies when the epoch ends. If they could read private repos' caches, rotate the master now |
| Removing an owner | `deny` in `ci.yaml` stops new secrets; purge the owner's directory (next row) |
| Suspected poisoning | purge that repo's directory. On galaxy: `kubectl exec ci-cache-0 -- /ci-cache purge --owner N [--repo M]`, after confirming (S4). In Azure: delete it under `/home` in the Kudu console |
| Money | galaxy costs nothing. Azure is the plan's flat price, and in-region traffic should be free (C3 checks) |

## Observability

*(Claude's call)* throughout.

- **Scraping.** Prometheus scrapes galaxy's host on 9090 through a ServiceMonitor. It scrapes the Azure
  host's `/metrics` every 5 min, with the ops token, through a `ScrapeConfig`. Both need the
  `release: prometheus` label, or Prometheus silently skips them.
- **Metrics**, labelled by `location` and never by owner: `ci_cache_requests_total{route,code}`,
  `ci_cache_lookups_total{result}`, `ci_cache_bytes_stored`, `ci_cache_bytes_cap`, `ci_cache_entries`,
  `ci_cache_evictions_total{reason}` (`age`, `owner_quota`, `total_cap`, `incomplete`),
  `ci_cache_transfer_bytes_total{direction}`, `ci_cache_auth_failures_total{reason}` (`params`,
  `epoch`, `mac`, `age`), `ci_cache_rate_limited_total`, `ci_cache_owners_active`, `ci_cache_epoch`,
  and `ci_cache_owner_quota_ratio_max` (the fullest owner's ratio, unlabelled).
- **Per owner, by id:** daily rows of entries, bytes, quota, hits, misses, bytes in and out, and
  evictions, kept 35 days in a JSON file per owner. `GET /v1/stats` serves them behind the ops token.
- **Alerts** go to the weekday route, since the cache is best-effort. They ship as PrometheusRule
  objects, since a ConfigMap rule is never loaded.

| Alert | Fires when |
|---|---|
| `CICacheDown` | a host's scrape fails for 15 min (galaxy) or 30 min (Azure) |
| `CICacheNearCap` | bytes stored pass 85 % of the total cap for 1 h |
| `CICacheAuthFailures` | failed checks pass 1 a second for 15 min |
| `CICacheErrors` | 5xx answers pass 5 % for 15 min |
| `CIProxyDown` | goproxy or Verdaccio has no ready pod for 15 min |
| `CIProxyDiskHigh` | a proxy's claim passes 80 % (proxies don't evict; clearing one is a runbook step) |

## Phasing

| Phase | Delivers | Needs | Done when |
|---|---|---|---|
| C1, proxies | the `ci-cache` namespace, goproxy, Verdaccio and their policies, plus `runner.envs` in the `ci-tiny-runner` ConfigMap. No dispatcher change | `tiny: on` (phase 1 Task 19), right after it *(Chris)* | a `tiny` Go build and an `npm ci` show proxy hits; `go mod verify` passes; the §6.3 matrix still passes from a job |
| C2, galaxy cache | `images/ci-cache`, the galaxy host, and in the dispatcher: derivation, rendered `tiny` configs, `release_repos`, and the `ci.yaml` keys | C1; a dispatcher release, placed by the phase 2 plan's gates | the list below |
| C3, Azure cache | `cache.tf`, `CI_CACHE` in the class image, the ARM guard change, and the scrape | phase 2 paid `on`, with the $80 cap and the $85 budget applied | a `medium` job restores what an earlier one saved; a request from outside the filter gets 403; after a week, the group's bandwidth meter reads $0 |

**C2 acceptance:**
1. Two jobs in one repo: the first saves, and the second restores.
2. A job in another repo of the same owner, and one in another owner, both miss the first repo's key.
   Direct calls with their own secrets and the first repo's ids get 403.
3. A query with a swapped or repeated `owner` gets 403.
4. A job in a release repo has no `ACTIONS_CACHE_URL`.
5. Unit tests with an injected clock: N−1 holds for 2 h after a boundary, and N+1 for 5 min before one.
   One owner's quota eviction never touches another owner's entries.
6. Before the host first starts, a job that holds a secret passes, with a cache warning.

## Left to the builder

The Go layout and on-disk format, inside the anti-goals; how the index persists; capturing fixtures
from a real proxy; exact rate numbers; goproxy and Verdaccio config and images; claim sizes within the
SSD budget; StatefulSet or Deployment for the proxies; the `ScrapeConfig` and a dashboard; the fork's
lint code; terraform names (App Service names are global, so check availability); the purge command's
flags; adding `zstd` to the `tiny` and class images (smaller, faster entries).

## Egress

ACI internet egress is outside the spec's compute-only money model. That gap is decided and fixed by
the phase 2 plan, with a per-job egress cap *(Chris, 2026-11-05)*. The cache host adds no second
channel, thanks to its source filter.

## Decided (Chris, 2026-11-05)

1. **Funding:** the global paid cap is $80 a period and the CI group budget $85. The Azure host costs
   $12.41, and the attacker row comes to $138.21.
2. **Release repos:** `realliance/forgejo` now, with the cache withheld from the whole repo.
   `realliance/forge-bot` and `realliance-ops/ci-dispatch` join at phase 2b.
3. **Egress:** a per-job cap in phase 2. The watcher and the sweeper read each container group's
   network-bytes metric, kill a group past the cap, and count egress in the burn.

## Builder check

Read as a builder, the draft had gaps, now fixed in this doc: where the server code lives and how it
ships; how quotas reach the host without a public file of owner ids; exactly what the ARM guard admits;
what happens to `ci-tiny-runner`; how release jobs lose `ACTIONS_CACHE_URL`; what the host does with a
repeated `owner` key; the largest entry `tiny` can hold; how Prometheus reaches a filtered Azure host; a C2
acceptance step that would have needed a StatefulSet scaled to 0 (S4).

Still open, to settle during the build:
1. Does App Service take an `@sha256:` image reference for a custom container? If not, pin the tag and
   have the host check its own digest at start.
2. Do ACI's outbound addresses fall inside `AzureCloud.WestUS3`, and is in-region traffic free? C3 tests
   both, and the Azure section gives the fallbacks.
3. Do the actions members use (`actions/cache`, `setup-go`, `setup-node` from code.forgejo.org) all speak
   this protocol and send `cacheSize`? C2's acceptance jobs confirm it.
4. Which dispatcher release carries C2: a v0.1.x before phase 2, or v0.2.0? The phase 2 plan's gates
   decide.
5. The fork's repo id for `release_repos`, read from the API at C2.
