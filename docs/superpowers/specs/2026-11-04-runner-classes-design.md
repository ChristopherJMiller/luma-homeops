# Runner classes for the Realliance forge: design

> **Amendment (2026-11-04, pre-flight).** The plan's pre-flight changed four points, and the text below follows them:
>
> - credit periods are UTC calendar months, not the subscription's billing date *(Chris)*;
> - `ci-dispatch` is a StatefulSet, not a `Recreate` Deployment;
> - the Spot probe runs in a throwaway resource group, not the CI group;
> - the dispatcher's Role also holds the Job `patch` verb, to resume a suspended Job.

- **Date:** 2026-11-04.
- **Status:** draft for Chris's review. Nothing built.
- **Research:** `.superpowers/runners-research/findings.md` and
  `.superpowers/pages-research/tiny-runner.md`. Both stay local and out of git; the first names
  subscription ids.
- **Related:**
  - forge-bot spec `2026-11-02-forge-bot-and-ci-design.md`, "Security model, plainly";
  - NetworkPolicy spec `2026-11-03-networkpolicy-enforcement-design.md`, §6.3;
  - Forgejo 17 spec `2026-11-04-forgejo-17-upgrade-design.md`: the forge stays on 15 LTS until 19;
  - plan `2026-11-04-realliance-net-on-forge.md`: the `general` runner.
- **Markers:** *(Chris)* marks a decision Chris made on 2026-11-04. *(Claude's call)* marks a default
  this doc chose; each one is open to change.

## Design summary

One controller, `ci-dispatch`, runs every class. Every 5 s it lists the forge's queued jobs and gates
each one on its owner's limits. For a job that passes, it writes a reservation to its ledger, registers
an ephemeral runner bound to that one job, and starts fresh compute that runs
`forgejo-runner one-job --handle` once. When the job ends, it deletes the compute.

| Class | `runs-on` | Runs on | Shape | Open to | Owner pays |
|---|---|---|---|---|---|
| `tiny` | `tiny` | galaxy, one pod per job, host mode | 1 CPU, 2 GiB, 15 min | every forge member *(Chris)* | nothing; 120 minutes a day |
| `small` | `small` | ACI, host mode | 2 vCPU, 8 GiB, 30 min | everyone, under the cap *(Chris)* | out of $10 a period |
| `small` with Docker | `[small, docker]` | VM, job container | same | same | same |
| `medium` | `medium`, or `[medium, docker]` | ACI, or a VM | 4 vCPU, 16 GiB, 45 min | same | same |
| `large` | `large` | VM, job container | 8 vCPU, 32 GiB, 60 min | same | same |

Money: a global CI cap of $100 per credit period, and $10 per owner by default *(Chris)*. The
dispatcher refuses any launch it can't pay for at worst case. Azure-side backstops bound what a
dispatcher bug or a stolen credential can spend.

## Intent

Chris wants hosted CI for everyone on the forge, the way GitHub offers it, inside galaxy's limits and
the $150 monthly credit. Today two persistent runners serve two trusted repos. Each new repo would need
its own runner, and every job on a persistent runner can read the runner's token, its shared HOME, and
its caches. Forgejo 15 has no label permissions and no minute quotas, so something outside Forgejo has
to decide who runs where, and for how much.

A per-job ephemeral runner on per-job compute solves the sharing problem. One dispatcher solves the
who-and-how-much problem for every class.

## Goals

1. A member's repo can use `runs-on: tiny` with no setup, and the paid classes up to its owner's limit.
2. One job gets one ephemeral runner and one fresh pod, VM, or container group. Nothing outlives the
   job, and no job can read another job's files, caches, or credentials.
3. CI never spends more than $100 in a credit period, and nothing CI does can exhaust the credit.
   Exhausting it would also stop the DNS terraform state and logging.
4. A signed inventory commit changes owners' limits. A usage API serves the numbers to the planned
   Forgejo fork page.
5. The ledger accounts for every launch (owner, class, minutes, cost) and agrees with Azure's bill
   within 10 %.
6. Every compute has an expiry, and two reapers that share neither code nor platform enforce it.

## Non-goals

- Windows, macOS, and GPU runners.
- A shared actions cache or any cache service, which would let one tenant poison the next tenant's
  build.
- Warm capacity, until the dispatcher has measured start times *(Claude's call)*.
- Workflow-job webhooks, which arrive in Forgejo 17. The forge stays on 15 LTS until 19, so the
  dispatcher polls.
- The Forgejo fork's usage page. It gets its own patch; this doc defines its data source.
- Moving CI to another subscription *(Chris)*.
- Commit statuses on held jobs, which need `write:repository`.
- Moving conduit-moba's workflows. They can use the classes later.

## Anti-goals

- A long-lived runner credential that job code can read, as in KEDA's documented Forgejo setup.
- Docker, privileged pods, or a Docker socket on galaxy.
- An `ubuntu-latest` alias, or any GitHub-style label *(Claude's call)*. A mirrored GitHub workflow
  would start spending its owner's money without anyone choosing that.
- Learning about spend from the Azure bill. The dispatcher prevents it before launch.
- Per-owner data in metric labels, and hidden org names in any public file.
- Azure compute with an Azure identity, an inbound port, or a path to galaxy or the LAN.
- The dispatcher's token in any pod but the dispatcher's, or folded into forge-bot *(Chris)*.
- Per-job volumes on a `Retain` storage class.

## Constraints

Facts from the research, and what each one forces.

| Fact | The design |
|---|---|
| forgejo-runner v13.2.1 `one-job --wait --handle <h>` (Forgejo 15 and later) holds one connection, and Forgejo gives that runner only the attempt whose handle it presents | binds each runner to one job without repo-scoped registration |
| Ephemeral runners run at most one job, then Forgejo deletes them. One that never gets a job lingers | reaps runners whose job never came |
| Registration: `POST /{admin,orgs/{org},user,repos/{o}/{r}}/actions/runners` with `{name, description, ephemeral}` returns `{id, uuid, token}`. It has no labels field, since the runner declares its labels. No ephemeral-only credential exists, and instance scope needs a site administrator's `write:admin` | runs as a site administrator bot, `ci-dispatch` *(Chris)* |
| `GET /admin/actions/runners/jobs?labels=a,b` returns waiting and running jobs whose `runs_on` ⊆ the labels, with id, attempt, handle, repo id, owner id, `runs_on`, status, and task id. It has no run id, no commit, and no pagination | polls once per loop; bills by `owner_id` *(Chris)*; can't post a commit status without more calls and scopes |
| Forgejo 15 has no label permissions, no minute quotas, and no cancel API. An unclaimed job ends after 24 h (`ABANDONED_JOB_TIMEOUT`) | makes the dispatcher the only gate; a held job waits at most 24 h |
| The forge stays on 15 LTS until 19, in spring 2027 | polls now; moves to job webhooks at 19 and keeps polling as a backstop |
| kube-network-policies enforces only for pod IPs its agent has seen. Without NRI, a new pod starts unenforced (NetworkPolicy spec §3.4, §6.3). Today only `bottom` runs the agent (step A3a), and the runner policy (step B2) hasn't landed | gives every job pod the `netpol-gate` init container, and opens `tiny` only after B2 |
| `rook-ceph-block-ssd` has reclaim policy `Retain`, so per-job claims would leak volumes | gives `tiny` a memory-backed work directory |
| galaxy nodes run at 84 to 89 % of memory requested; `NodeMemoryRequestsHigh` fires over 90 % | keeps `tiny` requests small and caps the class with a quota |
| The Realliance subscription is a Visual Studio credit of $150 a month with the spending limit on. The DNS terraform state and logging (about $10 a month) share it, and reaching the limit stops them too. The offer's docs say no Spot, yet the quota shows 20 low-priority cores. Each region allows 20 VM vCPU, 20 ACI cores, and 0 ACI Spot cores. `Microsoft.App` isn't registered | caps CI at $100 *(Chris)*; uses ACI and pay-as-you-go VMs *(Chris)*; tries Spot only after a probe |
| The credit period is the calendar month *(Chris)* | counts every limit over the UTC calendar month |
| Azure budgets stop nothing. Cost data arrives 8 to 24 h late, and budget actions up to 48 h late | enforces in the dispatcher, with backstops faster than the budget |
| NetworkPolicy has no FQDN rules | can at best limit the dispatcher's Azure traffic to the internet on 443 |
| The forge has internal sign-in and basic auth off, and tokens still work. `prohibit_login` blocks tokens too; forge-bot relies on that | gives `ci-dispatch` no web sign-in by construction, and never sets `prohibit_login` on it |
| New Azure subnets get no default outbound access, and a NAT gateway costs about $32 a month | gives each job VM its own Standard public IP |

## Classes

### Labels and routing

Every row here is *(Claude's call)*.

| `runs-on` | Goes to |
|---|---|
| `tiny` | a `tiny` pod on galaxy |
| `small`, `medium` | an ACI container group, host mode |
| `[small, docker]`, `[medium, docker]` | a VM, job container with the VM's Docker socket |
| `large`, `[large, docker]` | a VM, the same way |
| `galaxy`, `general` | their existing runners; the dispatcher never touches these labels |
| anything else, two size labels, `[tiny, docker]` | nothing: the dispatcher holds it, and Forgejo ends it after 24 h |

The listing shows only `runs_on`, so a job's need for Docker has to live there. `docker` is an ordinary
Forgejo label. The VM runner declares it beside its size label, and Forgejo's own "every label" rule
does the matching.

**Docker on VMs** *(Claude's call)*: steps run in a job container (the workflow's `container:`, or a
pinned default image) with the VM's Docker socket mounted. That is forgejo-runner's normal Docker mode,
and `container:`, `services:`, and `docker build` then work as on GitHub's hosted runners. The socket
makes the job root on its VM. The VM serves one job, carries no Azure identity, and goes away
afterwards.

### Shapes, limits, and prices

The shapes, timeouts, and `tiny` limits are *(Claude's call)*; the dollar limits are *(Chris)*.

The prices come from Azure's retail list for 2026-11-04 (`westus2` or `westus3`, Linux, USD), and the
dispatcher refreshes them daily. VM prices include the public IP and the OS disk.

| Class, backend | vCPU, memory | Runner timeout | Hard stop after launch | $ an hour | Reserved per job |
|---|---|---|---|---|---|
| `tiny`, galaxy pod | 1, 2 GiB | 15 min | 20 min | none | 20 of the owner's daily minutes |
| `small`, ACI | 2, 8 GiB | 30 min | 40 min | 0.117 | $0.08 |
| `medium`, ACI | 4, 16 GiB | 45 min | 55 min | 0.233 | $0.21 |
| `small`, VM D2as_v6 | 2, 8 GiB | 30 min | 45 min | 0.099 | $0.07 |
| `medium`, VM D4as_v6 | 4, 16 GiB | 45 min | 60 min | 0.190 | $0.19 |
| `large`, VM D8as_v6 | 8, 32 GiB | 60 min | 75 min | 0.371 | $0.46 |

| Limit | Default | Set in |
|---|---|---|
| Per owner, paid | $10 a period; 2 paid jobs at once | signed config, with overrides per org or user *(Chris)* |
| Per owner, `tiny` | 1 job at once; 120 minutes a UTC day | signed config |
| Global, paid | $100 a period; at most $0.60 an hour of running compute | ceilings in code; config may lower them |
| Global, `tiny` | 2 pods | config, and the namespace quota |

$10 buys about 85 hours of `small` on ACI, or 27 hours of `large`. The $0.60 an hour ceiling comes from
the backstop arithmetic (see Budget enforcement). It allows about five `small` jobs at once, or one
`large` and two `small`.

**Dasv6 for VMs, 4 GiB per vCPU** *(Claude's call)*. Out-of-memory kills cause the most confusing CI
failures (linkers, bundlers, `docker build`). 4 GiB per vCPU also matches GitHub's hosted Linux runner
for public repos (4 vCPU, 16 GiB), so mirrored workflows behave the same. Dalsv6 would save about 11 %
a minute, which comes to cents per job; the owner's dollar limit controls spend, not the shape. ACI gets
the same memory, so a class behaves the same on either backend.

At 4 GiB per vCPU, ACI costs about 18 % more an hour than a pay-as-you-go VM ($0.117 against $0.099 for
`small`). ACI starts faster (30 to 90 s, against 90 to 130 s for a VM) and has no billing floor, while
VMs may bill at least 5 minutes (unverified). Chris's split stands, and the start-time metric shows
whether it should change.

### `tiny` on galaxy

| Item | Setting |
|---|---|
| Namespace | `ci-tiny`, Pod Security `restricted`, a ResourceQuota of 3 pods (requests 750m CPU and 1.5 GiB, limits 6 GiB) |
| Object | one batch Job per job: `backoffLimit: 0`, `activeDeadlineSeconds: 1200`, `ttlSecondsAfterFinished: 0`. Finished pods must not linger, because without NRI their IPs confuse the enforcer (kube-network-policies #414) |
| Init | `netpol-gate` from the NetworkPolicy spec §6.3, unchanged |
| Runner | `one-job --wait --handle <h> --label tiny:host`, capacity 1, timeout 15 min, cache off. The credential comes from a per-job Secret that the Job owns |
| Image | the slim `general` runner image (forgejo-runner, node, git, curl, unzip), pinned by digest. It becomes the `tiny` image |
| Pod | uid 1000, read-only root, all capabilities dropped, `RuntimeDefault` seccomp, no service account token, no service links, no host path, no host network |
| Resources | requests 250m and 512 MiB; limits 1 CPU and 2 GiB. `/work` (workspace, HOME, TMPDIR) is a memory `emptyDir` of at most 1 GiB inside that limit *(Claude's call)* |
| Placement | prefers nodes other than `top` (heat, #2603), and spreads across nodes |
| Egress | `default-deny`, `allow-dns`, the forge at the WAN /25 on 443, and the internet on TCP 80 and 443 only. The last is a new component, `egress-internet-web`: `egress-internet-only` with ports *(Claude's call)* |

**Why memory, not disk.** A `Retain` class leaks a volume per job, and a node `emptyDir` lands on the
etcd NVMe. A `Delete`-class ephemeral volume adds an RBD attach per job on the pool that every Postgres
shares. If real jobs need more than 1 GiB, add a `Delete` SSD class then.

**Why ports 80 and 443 only.** `tiny` traffic leaves from the home WAN address. Closing every other port
stops SMTP spam, SSH scanning, and most mining pools, and costs builds nothing.

### `small` and `medium` on ACI

| Item | Setting |
|---|---|
| Object | one container group per job; restart policy `Never`; no IP address, so no inbound; no VNet; no managed identity |
| Runner | `one-job --wait --handle <h> --label <class>:host`, with the credential as a secure environment value. A `timeout` wrapper stops the group at its hard stop even when galaxy is down |
| Image | a class image (runner, git, node, Go, ko), built like the existing runner images and pulled from ghcr, so no Azure pull crosses the home uplink. ACI caches nothing, so the image stays small |
| Egress | open, because ACI outside a VNet has no NSG. Azure blocks outbound SMTP |

### Docker jobs and `large` on VMs

| Item | Setting |
|---|---|
| Object | one VM per job, with its NIC, public IP, and OS disk; all deleted together; never reused |
| Image | a stock Ubuntu LTS image; cloud-init installs Docker and a checksummed forgejo-runner. A gallery image only if measured start times call for one *(Claude's call)* |
| Credential | in cloud-init custom data, written to a root-only file |
| Network | one VNet and subnet. The NSG denies all inbound (no SSH; boot diagnostics only) and allows outbound DNS and TCP 80 and 443 *(Claude's call)* |
| Identity | none |
| End | the runner exits, cloud-init powers the VM off, and the dispatcher sees it stopped and deletes the set |
| Spot | off. If the probe in Decision 1 passes: Spot first (eviction deletes), with pay-as-you-go as fallback. An eviction fails the job and costs the owner nothing; Forgejo 15 has no rerun API, so the user reruns |

### Coexistence with `galaxy` and `general`

Both persistent runners stay as they are, with labels that no dispatcher runner ever carries *(Chris)*.
Each retires into a class once that class has proven itself (see Phasing) *(Claude's call)*.

| Runner | Serves | Retires into | How |
|---|---|---|---|
| `general` | `realliance/realliance.net` | `tiny`, same image | the workflow says `runs-on: tiny`; then remove the Deployment, its claim (S4), and its registration |
| `galaxy` | `realliance/forge-bot`, and `realliance/ci-dispatch` from phase 1 | `small` on ACI, with Go and ko in the image | both workflows say `runs-on: small`; then remove the Deployment and its cache claim (S4). About a cent a build, charged to `realliance` |

Retiring `galaxy` removes the last persistent runner from galaxy, and with it the cache-poisoning path
into two administrator bots. While the sweeper's lock holds, an Owner running `ko build` stays the
fallback for a forge-bot or `ci-dispatch` image.

## The dispatcher

### Shape

Every item here is *(Claude's call)*.

- **Code:** `realliance/ci-dispatch`, a public forge repo. Go with only the standard library
  (`net/http`) and hand-written clients for Forgejo, Kubernetes, and Azure Resource Manager; no SDKs.
  That keeps the supply chain small beside a site administrator token, and the fakes simple, as in
  forge-bot.
- **Build:** ko on the `galaxy` runner, through a second repo-scoped registration, until `galaxy`
  retires. This goes against the realliance.net plan's rule F3 but adds no new attacker: any member who
  can push a branch to `ci-dispatch` can already push one to forge-bot. Images go to
  `git.realliance.net/realliance/ci-dispatch`, and Chris reviews the diff before every re-pin, as for
  forge-bot.
- **Deploy:** Argo app `ci-dispatch` (`cluster/ci-dispatch/`, namespaces `ci-dispatch` and `ci-tiny`,
  both Pod Security `restricted`). One replica as a StatefulSet, distroless, non-root, read-only root. The
  ledger is SQLite on a 1 GiB `rook-ceph-block-ssd` claim; `Retain` suits a ledger.
- **Kubernetes rights:** a Role in `ci-tiny` only, for Jobs (create, read, patch, delete), Secrets (create,
  delete), and Pods (read).
- **Azure identity:** a service principal with a custom role on the CI resource group only. The role
  can create and delete container groups, VMs, NICs, disks, and public IPs, join the one subnet, and
  read. It also gets Cost Management Reader on that group, and read on the sweeper's run history. Its
  credential lasts at most 6 months and lives as a SealedSecret in `ci-dispatch`, with its canonical
  copy in sso-secrets. Workload identity federation would remove the long-lived credential, but Azure
  would need galaxy's service-account issuer keys at a public URL, and publishing them means a Talos
  machine-config change (S4). That comes later, if ever.
- **Azure as code:** terraform in `azure/ci/`, with state beside `azure/logging`. Chris applies it.

### Loop

Every 5 s, with a full sweep every 5 min:

1. **Read** the job listing for `tiny,small,medium,large,docker`, the ledger, and live compute: Jobs in
   `ci-tiny`, and resources tagged `forge-ci` in the CI resource group.
2. **Reconcile** each launch as running, ended, or gone.
3. **Gate** each waiting job that has no launch (next table).
4. **Launch** in fair order: the oldest job first, round-robin across owners.
   1. Write the reservation to the ledger, keyed by job id and attempt, so one key can't launch twice.
   2. Register an ephemeral runner at instance scope, named `ci-<class>-<job>-<attempt>`.
   3. Create the compute, tagged with `forge-ci`, the key, the owner id, and `expires-at`.

   Any failure undoes the earlier steps and frees the reservation.
5. **Reap** (see Reaper).
6. **Settle** each finished launch: cost from the create and delete times of the compute (VMs at least
   5 minutes), then free the rest of the reservation.

**Instance scope plus the handle** *(Claude's call)*. The handle already binds the runner to one
attempt on the server. Repo-scoped registration would add `write:repository` to the token, which means
write access to every repo, for little extra safety.

### Gates

| Gate | When it fails |
|---|---|
| The class is `on` in the luma-homeops ConfigMap and enabled in the signed config | wait |
| `runs-on` maps to exactly one class, and `docker` appears only with a paid size | hold: `label` |
| The owner isn't denylisted, and for paid classes the owner's limit is more than zero | hold: `denied` or `no-budget` |
| The owner has fewer jobs running than its concurrent limit | wait |
| Class capacity: 2 `tiny` pods; paid compute stays at or under $0.60 an hour | wait |
| The owner's remaining `tiny` minutes or dollars cover the reservation | hold: `owner-limit` |
| The global dollars cover it | hold: `global-cap` |
| Prices are under 7 days old, the ledger takes writes, and a config has verified at least once | wait, with an alert |

A wait and a hold look the same in Forgejo: the job stays queued, and the dispatcher checks it again
every loop. A freed slot, a raised limit, or a new period then starts it. A hold also gets one log line
and shows in the usage API with its reason. After 24 h, Forgejo ends the job.

**Held jobs** *(Claude's call)*: no commit status in v1. Posting "held: over limit" would need
`write:repository` on the token, plus a lookup of the run's commit. The fork's runner page shows holds
instead.

### Ledger

Every item here is *(Claude's call)*.

- **SQLite** with one writer: one replica as a StatefulSet, and an RWO claim. Cluster Postgres would add an
  HA cluster's memory to nodes near 85 % for no gain.
- **Rows:** launches (key, owner, repo, class, backend, compute and runner ids, state, timestamps,
  reservation, cost, outcome), holds, prices, periods, and the verified config commit. Money in whole
  micro-dollars.
- **Backup:** nightly, in the restic config tier (`docs/backups.md`). Losing it loses one period's
  accounting, which the Azure backstops bound. Until a restore, paid classes stay off.

### Limits as signed config

- `ci.yaml` lives in the private inventory `realliance-ops/forge`. The dispatcher reads it only at an
  Owner-signed head, checked the way forge-bot checks its files: locally, with OpenPGP, against a
  keyring in the dispatcher's luma-homeops ConfigMap, and never by Forgejo's `verified` flag.
- The binary holds the ceilings: $100 a period and $0.60 an hour. Config may lower them, never raise
  them. Raising one takes a code change and a re-pin *(Claude's call)*.
- An unsigned or broken head keeps the last verified config and raises an alert. Before any config has
  verified, `tiny` runs on built-in defaults and the paid classes stay off.

```yaml
# realliance-ops/forge: ci.yaml. Names resolve to ids at each load; an unknown name fails the load.
defaults:
  paid_usd_per_period: 10
  paid_concurrent: 2
  tiny_concurrent: 1
  tiny_minutes_per_day: 120
global:
  paid_usd_per_period: 100   # ceiling in code
  paid_usd_per_hour: 0.60    # ceiling in code
  tiny_concurrent: 2
classes: {tiny: on, small: on, medium: on, large: on}
owners:
  realliance: {paid_usd_per_period: 30, tiny_concurrent: 2}
deny: []                     # every class, tiny included
```

### Reaper

The reaper runs inside the dispatcher on every loop. It needs only Kubernetes and Azure, so it keeps
working while Forgejo is down.

| Finds | Does |
|---|---|
| compute past `expires-at` | deletes it |
| compute, a Job, or a `ci-*` runner with no ledger row | deletes it after 10 min |
| a launch whose job still waits after the pickup timeout (`tiny` 3 min, ACI 6, VM 10) | deletes compute and runner, and launches again; after 3 failures it holds the job as `launch-failing` and alerts |
| a finished job with compute still up, or a stopped VM | deletes the compute set |

The cloud sweeper (see Budget enforcement) is the second, independent reaper.

### Failure modes

| Failure | Behavior |
|---|---|
| Forgejo down or returning errors | no launches; running jobs carry on, and the reaper keeps deleting by expiry |
| Azure errors, no capacity, or quota | the job waits with backoff and deletes retry; the cloud sweeper deletes anything past 90 min |
| dispatcher crash mid-launch | the ledger row comes first; on start, the dispatcher reconciles ledger, compute, and runners before it launches anything |
| galaxy down | `tiny` pods go with it, ACI groups stop at their hard stop, and the cloud sweeper deletes VMs |
| ledger lost | paid classes stay off until a restore |
| price feed down | the last prices serve for up to 7 days, then paid jobs wait |
| config untrusted | the last verified config, with an alert |
| a second dispatcher | the StatefulSet, the RWO claim and a file lock prevent it, and per-key uniqueness catches it anyway |
| a job cancelled before pickup, or held by a concurrency group | the pickup timeout reaps the launch; phase 0 checks how the listing shows a job that a concurrency group holds back |

**Charging** *(Claude's call)*: the owner pays for every launch made for their job, even a wasted one.
The cost comes from the create and delete times of its compute. That matches the bill and resists
queue-and-cancel abuse, and a wasted ACI launch costs about a cent.

### Usage API

The data source for the fork's runner page *(Chris)*. It listens on a ClusterIP Service only, never on
Traefik, behind a read-only bearer token that only Forgejo holds.

`GET /v1/owners/{owner_id}/usage?period=current|previous|<YYYY-MM>`

```json
{
  "owner_id": 42,
  "period": {"start": "2026-11-01", "end": "2026-11-30"},
  "paid": {
    "limit_usd": "10.00", "limit_source": "default",
    "used_usd": "3.42", "reserved_usd": "0.21", "remaining_usd": "6.37",
    "concurrent": {"running": 1, "limit": 2},
    "classes": {"small": {"jobs": 31, "minutes": 212, "used_usd": "0.41"}}
  },
  "tiny": {"minutes_today": 34, "minutes_per_day": 120, "jobs_this_period": 88},
  "holds": [{"job_id": 9001, "repo_id": 17, "class": "large", "reason": "owner-limit",
             "since": "2026-11-20T10:02:00Z"}],
  "global": {"paid": "open"}
}
```

`GET /v1/owners/{owner_id}/jobs?period=<p>&limit=50` returns recent launches: job, repo, class, backend,
queued, started, and ended times, minutes, cost, and outcome.

- Ids, not names. Money as decimal strings.
- The fork decides who sees what: a user sees their own usage, and an org's owners see the org's. The
  dispatcher trusts the token and nothing else.
- `global.paid` (`open`, `near-cap`, `closed`) tells an owner why a job waits, without showing other
  owners' usage.

## Budget enforcement and backstops

The dispatcher reserves before it launches. Every later layer exists for when the dispatcher is wrong
or compromised. Azure runs in two resource groups *(Claude's call)*. The **CI group** holds job
compute, and the dispatcher can write only there. The **guard group** holds the watchers, and the
dispatcher has no rights in it.

| # | Layer | Identity | Acts | Stops |
|---|---|---|---|---|
| 1 | reservation per launch | dispatcher | before launch | owner and global caps, and the $0.60 an hour ceiling |
| 2 | ceilings in code | binary | always | a config typo |
| 3 | hard stop per compute | the Kubernetes deadline, or the `timeout` wrapper in ACI | at the stop | a hung job |
| 4 | reaper | dispatcher | each loop | expired and orphaned compute |
| 5 | the custom role and Azure Policy on the CI group | Chris's terraform | at create | other regions, sizes, shapes, and resource types |
| 6 | regional quota | Azure | at create | more than 20 VM vCPU, or 20 ACI cores |
| 7 | cloud sweeper: a Logic App in the guard group, every 10 min | its managed identity | within 10 to 20 min | deletes anything older than 90 min; over $0.60 an hour for two sweeps in a row, deletes all compute and **locks** |
| 8 | a $105 budget on the CI group, over the calendar month | Azure, then the sweeper's lock | 8 to 48 h late | deletes all compute and locks |
| 9 | the $120 subscription budget (exists: `azure/logging/`, 80 % and 100 % alerts) | Azure | late | nothing; it emails Chris |

**Lock** means the sweeper removes the dispatcher's role assignment on the CI group. Only Chris
restores it, with a terraform apply. The sweeper also emails on a failed run, and the dispatcher alerts
when the sweeper's run history goes stale.

The sweeper costs about $2 a month. With logging and the state at about $10, the margin under $150 is
about $38.

**The budget is $105, not $110** *(Claude's call)*, for the second row below.

| Scenario | What holds | Worst CI spend in a period |
|---|---|---|
| a dispatcher bug ignores its own caps | the sweeper's rate trip, within 20 min | a dollar or two over $100 |
| an attacker holds the dispatcher pod or its Azure credential, and stays under the rate trip | the budget: $105, plus 48 h at $0.60 an hour | about $134; about $146 with logging, state, and the sweeper |
| the sweeper breaks | the dispatcher's caps, then the budget | $100 |
| galaxy goes down | ACI groups stop themselves; the sweeper deletes VMs past 90 min | the running jobs' hard stops |
| a compromised dispatcher and a broken sweeper at once | only quota and the budget | can reach the spending limit: quota allows about $2 an hour, and the budget can act 48 h late |

The $0.60 an hour ceiling exists for the second row: no faster burn fits under $150 within the
budget's 48 h lag. The last row is the residual risk. The sweeper's failure emails and the
dispatcher's staleness alert keep its window short.

## Security, plainly

### A `tiny` job

**Can:**
- run any code for up to 15 minutes, with 1 CPU and 2 GiB, as uid 1000 in a pod on a galaxy
  control-plane node;
- reach the internet on TCP 80 and 443 from the home WAN address, and the forge at its public address;
- read its own repo's and owner's Actions secrets and its job token, which its author controls anyway;
- read its own runner credential, which can only ever take this job and dies with it;
- spend its owner's `tiny` minutes.

**Can't:**
- reach pods, Services, the Kubernetes API, Ceph, NFS, LoadBalancer addresses, the LAN, BMCs, the KVM,
  the router, the switch, the tailnet, or router services on the WAN address;
- take another job, or see another job's files, credentials, or caches: every pod starts fresh, and no
  volume mounts in two pods;
- leave anything behind;
- reach the dispatcher or its tokens.

**Residual risk:**
1. **A kernel or runc escape gives root on a node that runs etcd and Ceph.** Every member can run
   `tiny`, which makes this the largest new risk. Chris declined gVisor (Decision 2), so pod hardening and the egress lockdown carry it.
2. **Abuse from the home address,** such as scraping, floods, and the complaints that follow. The port
   limit, the minutes, and the concurrency bound it.
3. **Enforcement gaps** of a few seconds when the agent on a node restarts (fail-open; NetworkPolicy
   spec §6.3).
4. **Who:** members only. The forge is invite-only through the SSO, Actions stays on by default for new
   repos, and fork pull requests from read-only contributors still need approval.

### A paid job

**Can:**
- run any code until its hard stop: as root in a hypervisor-isolated ACI group, or as root on its own
  VM through the Docker socket;
- reach the internet from an Azure address: any port from ACI (Azure blocks SMTP), and TCP 80 and 443
  from a VM;
- read its own secrets, job token, and runner credential, as on `tiny`;
- spend its owner's money, up to the reservation.

**Can't:**
- reach galaxy or the LAN: it has no VPN and no peering, and it sees the forge at its public address
  like anyone else;
- use any Azure identity, since the compute carries none;
- reach other jobs: separate groups and VMs, and no inbound;
- outlive its hard stop, or spend past its owner's limit.

**Residual risk:**
1. **Abuse from a paid job lands on the Realliance subscription.** A suspension would also stop the DNS
   terraform state and logging, which share it. Invite-only membership, the owner limits, and the VM
   port rules bound it; ACI egress stays open.
2. **A runner credential read before pickup** (from custom data or the ACI definition) takes that one
   job only. Only the dispatcher's credential and the subscription's Owners can read those.

### The administrator token

`ci-dispatch` is a site administrator with `write:admin` *(Chris)*. Whoever gets its token, or runs code
in its pod, can do what a site administrator can. That means reading every private repo, creating
administrators and local accounts, and registering runners that receive other repos' jobs and secrets.
Next to `forgejo-admin` and forge-bot, the token ranks as the forge's most powerful credential.

What limits the risk *(Claude's call)*:
- **Where it lives:** a SealedSecret in `ci-dispatch`, and its canonical copy in sso-secrets. Never in
  logs.
- **Scopes:** `read:admin` and `write:admin`, plus read-only `read:repository`, `read:user`, and
  `read:organization` for names. No `write:repository`.
- **Code allow-list:** the Forgejo client exports exactly two writes, register a runner and delete a
  runner. A test fails the build if any other non-GET request appears, as in forge-bot.
- **Pod:** distroless, non-root, read-only root, Pod Security `restricted`, and three standard-library
  clients.
- **NetworkPolicy:** egress to DNS, the Forgejo pods on 3000, the API server, and the internet on 443
  for Azure. The last is the widest hole: without FQDN rules, a compromised pod can send the token out
  over 443. Ingress only from Prometheus and, once the fork page lands, the Forgejo pods.
- **No web sign-in:** the forge has internal sign-in and basic auth off, and the account's password is
  random and thrown away. Never set `prohibit_login` on it, since that also stops its token.
- **Detection from outside:** forge-bot gains one alert, `ForgeUnexpectedAdmin`. It fires for any site
  administrator or local account outside its expected list (`forgejo-admin`, `forge-bot`, `forge-ci`,
  `ci-dispatch`), and for any non-ephemeral runner outside the known registrations. A stolen token's
  easiest backdoor is a new local administrator, and forge-bot ignores local accounts today.
- **Rotation:** yearly, and at once on suspicion.

## Operations and observability

- **Runbook:** `docs/ci.md`, new. It covers pausing a class, changing a limit (a signed inventory
  commit), finding a job's record, unlocking after a trip, rotating the three credentials, and stopping
  everything. A member's guide (labels, limits, `docker`, where to see usage) goes on the forge wiki.
- **Modes:** each class is `off`, `dry-run`, or `on` in the luma-homeops ConfigMap, which is Chris's
  switch. `dry-run` gates and logs, and launches nothing.
- **Logs:** one JSON line per decision (key, owner, repo, class, decision, reason, reservation, settled
  cost), through fluent-bit to Azure Log Analytics.
- **Stopping everything (break-glass):** scale `ci-dispatch` to 0, which S2 allows during an incident.
  Then delete everything tagged `forge-ci` in the CI group, and the Jobs in `ci-tiny`.

Metrics carry class, backend, outcome, and reason labels, never owners:
`ci_dispatch_loop_last_success_timestamp`, `ci_dispatch_jobs{class,state}`,
`ci_dispatch_holds{class,reason}`, `ci_dispatch_launches_total{class,backend,outcome}`,
`ci_dispatch_start_seconds{class,backend}` (queued to running), `ci_dispatch_burn_usd_per_hour`,
`ci_dispatch_period_usd{kind}`, `ci_dispatch_reaped_total{reason}`, `ci_dispatch_errors_total{api}`,
`ci_dispatch_config_verified`, `ci_dispatch_price_age_seconds`,
`ci_dispatch_sweeper_last_run_timestamp`, and `ci_dispatch_drift_ratio`.

| Alert | Fires when | Route |
|---|---|---|
| `CIDispatchSpendHigh` | global period spend reaches 80 %, and again at 95 % | 24/7, forge-bot's urgent receiver |
| `CIDispatchLocked` | the sweeper or a budget locked Azure | 24/7 |
| `CIDispatchOrphanCompute` | compute without a ledger row, or past expiry, for 15 min | 24/7 |
| `CISweeperStale` | the sweeper hasn't run for 30 min | 24/7 |
| `CIDispatchStale` | no completed loop for 5 min | weekday |
| `CIDispatchLaunchFailing` | a job held as `launch-failing`, or launch errors for 15 min | weekday |
| `CIDispatchConfigUntrusted` | an inventory head without an Owner's signature | weekday |
| `CIDispatchDrift` | the ledger and Cost Management differ by more than 10 % for a closed day | weekday |
| `CITinyQuotaFull` | `ci-tiny` sits at its quota for 30 min | weekday |

**Start times** decide warm capacity *(Claude's call)*. Revisit it if a class's median from queued to
running stays over 2 minutes for a month. The known prices: an ACI standby pool runs about $72 a month
for one `small` group, too much under this cap, and a warm Spot VM about $22 a month, if Spot works.

## Phasing

| Phase | Delivers | Needs | Done when |
|---|---|---|---|
| 0, proofs | on the live forge: register an ephemeral runner, run `one-job --handle` in a throwaway pod, see the runner row vanish and the token fail afterwards, and see a second queued job stay untouched; the `labels=` subset rule with `docker`; how the listing shows jobs that `needs:` or a concurrency group holds back. Also the `ci-dispatch` account and token, `realliance/ci-dispatch` in the inventory, and its `galaxy` registration | nothing | each result sits in the plan's ledger |
| 1, `tiny` | the dispatcher with Forgejo and Kubernetes backends, the ledger, signed config, the usage API, `ci-tiny` and its policies, alerts, and `docs/ci.md`; 48 h in `dry-run`, then `on` | NetworkPolicy B2 done | a member's repo runs `runs-on: tiny`; the caps hold; the §6.3 matrix passes from inside a job; for a week, no pod or runner outlives its job |
| 1b | `general` retires into `tiny` | phase 1 stable for a week | realliance.net builds on `tiny` |
| 2, ACI | `azure/ci/` (both groups, the role, Policy, the service principal, both budgets, the sweeper and its lock), the price feed, the ACI backend, and paid accounting; the Spot probe, if approved | phase 1; Chris applies the terraform | paid jobs run; reservations hold; a drill with an over-ceiling resource trips the sweeper and locks; the first closed period drifts under 10 % |
| 2b | `galaxy` retires into `small` | phase 2 stable for 2 weeks | forge-bot and `ci-dispatch` images build on `small` |
| 3, VMs | `docker`, the VM backend, and `large`; Spot if the probe passed; a gallery image only if starts measure over 3 min | phase 2 | a job with `docker build` and `services:` passes; `large` passes; no VM outlives its hard stop |
| later | the fork's usage page; job webhooks at 19 LTS, with polling kept as a backstop; warm capacity on measured need | | |

## Left to the plan

The Go layout and schema; loop intervals and pickup timeouts; ARM API versions; the region (`westus2`
or `westus3`, by size availability); image contents and the default job image; cloud-init; the Policy
definitions and the role's exact actions; the sweeper's definition and its price table; the API server
rule, checked against deny logs.

## Decisions for Chris

Answered 2026-11-04:

1. **Probe Spot once: yes.** One D2as_v6 Spot VM in a throwaway resource group for about 5 minutes, as a phase 0 proof.
   A pass adds Spot to phase 3 as the first choice for VM jobs, with pay-as-you-go as the fallback and an
   off switch; a fail keeps VMs on pay-as-you-go. Treat a pass as something that can stop working.
2. **gVisor for `tiny`: no.** Pod hardening, the netpol gate and the egress lockdown carry the escape risk.
3. **Subscription budget:** the existing $120 budget (80 % and 100 %) is the subscription-wide alert.

Approved by Chris on 2026-11-04.
