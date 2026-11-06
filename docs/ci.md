# Forge CI: ci-dispatch and the runner classes

`ci-dispatch` starts one fresh runner for each waiting Forgejo Actions job on `git.realliance.net`,
within each owner's limits. Argo app `ci-dispatch` (`cluster/ci-dispatch/`) runs it in namespace
`ci-dispatch`, and its runner Jobs in `ci-tiny`. Code: `git.realliance.net/realliance-ops/ci-dispatch`
(v0.2.0). Spec: `docs/superpowers/specs/2026-11-04-runner-classes-design.md`. Plan and ledger:
`docs/superpowers/plans/2026-11-04-runner-classes-phase-0-1.md` and `.ledger.md` beside it.

Phase 2 adds the paid classes `small` and `medium` on Azure Container Instances (ACI): plan
`docs/superpowers/plans/2026-11-05-runner-classes-phase-2.md` and its ledger, and the Azure side in
`azure/ci/` (its `README.md`). They ship in ci-dispatch **v0.3.0**, the first release with ledger
schema v2. The current build, v0.2.0 (v0.1.0 plus blocked-job holds and the attempt on holds), runs
`tiny` only and refuses any paid mode but `off`. Until the v0.3.0 deploy (phase 2 Task 19), the paid
sections below describe what that deploy brings.

| Piece | Where |
|---|---|
| Dispatcher | StatefulSet `ci-dispatch`, one pod `ci-dispatch-0`, a distroless image: nothing can `exec` into it |
| Modes and timings | `cluster/ci-dispatch/dispatcher/config.yaml`, ConfigMap `ci-dispatch-config`. Hash-suffixed, so any edit restarts the pod |
| Limits, denies, class switches | `ci.yaml` on `main` of `realliance-ops/forge`, read only at an Owner-signed head |
| Owner keyring | `cluster/ci-dispatch/dispatcher/keyring/` |
| Ledger | PVC `ci-dispatch-ledger` (1Gi SSD, `Prune=false,Delete=false`): `/data/ledger.db`, and a nightly copy from 03:00 UTC in `/data/snapshots` (newest 7) |
| `tiny` Jobs | `ci-tiny` (`cluster/ci-dispatch/tiny/`): Pod Security `restricted`, a quota of 3 pods, egress `egress-internet-web` |
| Ports | 8080: `/metrics`, `/healthz`. 8081: the usage API, with no ingress (`kubectl port-forward` only) |
| Logs | JSON on stdout, ids only, never a name or a credential. Shipped to the Azure table `luma_homelab_logs_CL` (31 days) |
| Tests | `scripts/test_ci_dispatch_manifests.py` (pre-commit), `scripts/test-ci-dispatch-alerts.sh`; realliance/sso `tests/forge/test_forge_ci_dispatch.py` and `test_forge_ci_tiny.py` (`CI_TINY_ACCEPTANCE=1`) |
| Image | Pinned by digest. Renovate never automerges it, so its PR is the alarm. Re-pin by hand after review |
| Paid compute | One ACI container group per job in `rg-realliance-ci` (`westus3`), behind the Policy fence `realliance-ci-fence`. The sweeper `ci-sweeper` sits in `rg-realliance-ci-guard`. Terraform in `azure/ci/`, which only Chris applies |
| Azure credential | SealedSecret `ci-dispatch-azure` (`dispatcher/azure-credentials.yaml`): four files under `/etc/ci-dispatch/azure`, read once at start. Canonical copy: `CI_DISPATCH_AZURE_*` in sso-secrets `forge/secrets.env` |
| Ledger backup | CronJob `restic-ci-dispatch`, 03:50 UTC: `/data/snapshots` into the config tier's restic repo, host `ci-dispatch-ledger` (`docs/backups.md`) |

A push that changes what Argo renders here is a deploy, and every deploy is a STOP: ask Chris first.
To read a metric, run `kubectl -n ci-dispatch port-forward svc/ci-dispatch 18080:8080` in another
terminal, then `curl -fsS 127.0.0.1:18080/metrics | grep ^ci_dispatch_`.

## Pausing `tiny`

This is also the normal rollback.

1. Set `tiny: "off"` under `classes` in `cluster/ci-dispatch/dispatcher/config.yaml`, and the same in
   `test_config_values` (`scripts/test_ci_dispatch_manifests.py`), in one signed commit.
2. STOP: ask Chris. Push. The pod restarts on the new ConfigMap.
3. New launches stop. Running jobs carry on until they end, 20 min at most. Queued jobs wait
   (`class-off`), and Forgejo ends them after 24 h.
4. Confirm: `ci_dispatch_class_mode{class="tiny"}` reads 0, and `kubectl -n ci-tiny get jobs,pods`
   lists nothing.

Resume with `on` the same way; that is a deploy and a STOP too.

**Without a deploy**, an Owner can sign a `ci.yaml` with `tiny: off` in `classes` (see Changing a
limit). The dispatcher reads it at the next sweep. To confirm, wait over 5 min after the merge:
waiting `tiny` jobs then log `"reason":"class-off"` with `"mode":"on"`, since `mode` shows the
ConfigMap, and `ci-tiny` drains within 20 min. No metric shows this pause. A restart keeps it, even
one that rejects the stored copy, because the same reload verifies the head, which still says
`tiny: off`. The built-in limits put `tiny` back on only when that head fails too, or Forgejo is
unreachable at the restart.

**Before any enforcer restart, Talos hop or kube-network-policies bump** (N5): pause `tiny` with the
ConfigMap, and confirm `ci-tiny` has no pods. The enforcer fails open while its agent restarts, and a
`tiny` job runs a member's code.

## Pausing `small` and `medium`

The same two switches as `tiny`, and the normal rollback for the paid classes too.

1. Set `small: "off"` and `medium: "off"` under `classes` in `config.yaml`, and the same in
   `test_config_values`, in one signed commit.
2. STOP: ask Chris. Push. The pod restarts on the new ConfigMap.
3. New paid launches stop, and queued paid jobs wait (`class-off`). A running job keeps its container
   group until the job ends, at its hard stop at the latest (40 min `small`, 55 min `medium`). The
   reaper and the reconcile run in every mode, so the dispatcher still deletes, settles and charges it.
4. Confirm: `ci_dispatch_class_mode{class=~"small|medium"}` reads 0, and within 55 min
   `az container list -g rg-realliance-ci --query "length(@)"` reads 0.

**Without a deploy**, an Owner signs a `ci.yaml` with `small: off` and `medium: off` in `classes`, as
for `tiny`. **To stop the running groups too**, use the break-glass.

## Break-glass: stop everything now

Ask Chris first (S4: this scales a StatefulSet to 0). Argo self-heals the StatefulSet, and the root
app `applications` self-heals the Argo app `ci-dispatch`, so pause both or Argo scales the
dispatcher straight back up. Pausing the root freezes only the app definitions: every other app keeps
healing its own workloads.

```bash
export KUBECONFIG=/tmp/galaxy-kubeconfig
off='{"spec":{"syncPolicy":{"automated":null}}}'
kubectl -n argo-cd patch application applications --type merge -p "$off"
kubectl -n argo-cd patch application ci-dispatch --type merge -p "$off"
kubectl -n ci-dispatch scale statefulset ci-dispatch --replicas=0
kubectl -n ci-dispatch wait --for=delete pod/ci-dispatch-0 --timeout=90s
kubectl -n ci-tiny delete jobs --all
kubectl -n ci-tiny get jobs,pods,secrets
```

Wait for the pod before deleting: a launch in flight runs on for up to 20 s after the scale and can
still create a Job. If the `wait` times out, on an unreachable node say, the dispatcher may still be
running, so stop there and see node loss under Known limits. Delete every Job, not only the labelled
ones: a compromised dispatcher's Kubernetes credentials can create Jobs with any labels. Each Job
takes its Secret and pod with it. Repeat the last `get` until it lists nothing: pods take up to 30 s.
The code blocks in this file carry no trailing comments, because Chris's shell (`zsh`) has
`interactivecomments` off, and a pasted `# …` would become arguments.

**Paid classes:** scaling the dispatcher to 0 leaves every container group running until its hard
stop. Once the pod has terminated, delete each group from the CI group, inside `nix develop`, with
`az account show --query name -o tsv` printing `Realliance`:

```bash
for n in $(az container list -g rg-realliance-ci --query "[].name" -o tsv); do
  az container delete -g rg-realliance-ci -n "$n" --yes -o none
done
az container list -g rg-realliance-ci --query "[].{name:name,state:provisioningState}" -o tsv
```

It deletes every group, whatever its tags: the fence checks only that `forge-ci` exists, not its
value, and the CI group holds nothing but job compute. Repeat the last `list` until it prints
nothing: a group stays listed while it reads `Deleting`. Their reservations settle once the
dispatcher runs again and lists the groups as gone.

Then make it last in git: pause `tiny`, and `small` and `medium` too, which passes the hooks. Only
once that change is on `main`, restore Argo: patch `applications` back to
`{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true,"allowEmpty":true}}}}`. It then
re-applies the policy of `ci-dispatch` from git, which scales the dispatcher back up. The runner rows
of the deleted Jobs and groups can take no other job; the reaper removes them 10 min after the
dispatcher starts again.

## Classes and modes

| `runs-on` | Backend |
|---|---|
| `tiny` | A pod in `ci-tiny`: 1 CPU, 2 GiB, `/work` a 1 GiB memory volume, 4096 tasks (`podPidsLimit` on every node, `docs/node-rebuild.md`). Runner timeout 15 min, Job deadline 20 min. Each launch reserves 20 of the owner's daily minutes |
| `small`, `medium` | One ACI container group per job, from v0.3.0 on (see Paid classes). Before it, as `large` |
| `small` or `medium` with `docker` | Wait `class-off`: Docker needs a VM, which arrives with phase 3 |
| `large`, alone or with `docker` | No backend until phase 3. `config.Load` refuses any mode but `off` |
| Two size labels, `docker` alone, `[tiny, docker]` | Hold `label`. Forgejo ends the job after 24 h |
| `galaxy`, `general`, or any other label | Never listed: the dispatcher asks Forgejo only for jobs whose labels all fall within `tiny`, `small`, `medium`, `large` and `docker` |

| Mode | Effect |
|---|---|
| `off` | The class's jobs wait (`class-off`). Nothing launches |
| `dry-run` | Decides, logs each job once per change, counts `ci_dispatch_launches_total{outcome="dry-run"}`, may write holds. Launches nothing |
| `on` | Launches |

A class launches only when both switches allow it: its mode in `config.yaml` (Chris's switch, a
deploy) and `classes` in `ci.yaml` (Owner-signed, no deploy). Move `tiny` from `dry-run` to `on` in
`config.yaml`, then watch the decision log for a day. The paid classes go `on` once their probes
pass, with no day of waiting (Chris, 2026-11-06; see Paid classes).

The reaper and the reconcile run in every mode, `dry-run` included. They act only on the dispatcher's
own name, `ci-<class>-<job>-<attempt>-<try>`: a Job, a container group, or an ephemeral
instance-scope runner with no active launch for 10 min, and a Job past its `ci-dispatch/expires-at`
label or a group past its `expires-at` tag. Kubernetes Jobs are `ci-tiny-*` only. A drill's
`ci-probe-*` runner or `ci-drill-*` group, or a member's repo or org runner, is never in scope. Even
in `dry-run`, then, the dispatcher's token deletes runners of that one shape.

## Paid classes

| Class | Shape | Runner timeout, hard stop | Price an hour | Reserved per launch |
|---|---|---|---|---|
| `small` | 2 vCPU, 8 GiB | 30 min, 40 min | $0.117 | $0.078 |
| `medium` | 4 vCPU, 16 GiB | 45 min, 55 min | $0.233 | $0.2136 |

- Each job gets a fresh container group in `rg-realliance-ci`, named like its runner,
  `ci-<class>-<job>-<attempt>-<try>`, with the tags `forge-ci=1`, `ci-class`, `ci-key`, `ci-try`,
  `ci-owner-id`, `created-at` and `expires-at`.
- The image is `ghcr.io/christopherjmiller/forgejo-runner-class`, by digest only (`paid.image` in
  `config.yaml`). Like every image built here, a merged base bump doesn't ship it: re-pin by hand.
- The job runs as root, with open internet egress, no Docker and no cache, and the runner credential
  only as a secure value. A `timeout` wrapper ends it 5 min before its hard stop, even with galaxy
  down. The pickup timeout is 6 min.
- Prices come from Azure's retail feed daily, never below the table's (2026-11-04). Once the newest
  is 7 days old (`paid.price_max_age`), paid jobs wait `not-ready`.

Run `az` inside `nix develop`, with `az account show --query name -o tsv` printing `Realliance`, and
write ARM URLs with the `{subscriptionId}` placeholder that `az` fills in, never the id.

**Money.** A launch reserves its worst case, the hourly price over the whole hard stop, before it
creates the group. Once Azure confirms the group gone (Known limits), the owner pays the compute
from creation to stop in whole seconds, capped at the reservation, plus the group's internet egress
(The egress cap), and the reservation frees. The owner pays for every launch made for their job, even
a wasted one. A launch that Forgejo held after the group existed pays its compute; an Azure capacity
or quota refusal made nothing and costs nothing.

**The gate.** A paid job passes the checks `tiny` has (Finding a job's record), then these in order:

| Check | Fails as |
|---|---|
| The owner's and the global paid limits are over 0 (checked right after the deny list) | hold `no-budget` |
| The owner runs fewer paid jobs than its `paid_concurrent` | wait `owner-busy` |
| The burn, plus this launch, stays at or under the hourly cap | wait `class-full` |
| The owner's used and reserved money this period, plus this reservation, stays within its limit | hold `owner-limit` |
| The same for the global cap | hold `global-cap` |
| Ready: a verified config, the ledger and the money read, Azure listed this step with no lock, prices under 7 days old, an egress pass in the last 5 min | wait `not-ready` |

The burn, `ci_dispatch_burn_usd_per_hour`, adds the hourly price of every active paid launch, the
price of every listed group that no launch claims (orphans, groups still deleting, a drill's), and
every group's recent egress rate.

**Moving to `on`** takes the probes of phase 2 Task 19 Step 4, then a deploy, with no 24 h dry-run
(Chris, 2026-11-06), then the acceptance suite of Task 21.

### Paid limits and the caps owner

Change them in `ci.yaml` (Changing a limit). The code of v0.3.0 holds the ceilings; `ci.yaml`
may lower them, never raise them.

| Limit | Ceiling in code | `ci.yaml` |
|---|---|---|
| `global.paid_usd_per_period`, all owners in a UTC calendar month | $80 | $80 |
| `global.paid_usd_per_hour`, the burn: compute plus egress | $0.48: 4 `small` or 2 `medium` at once | $0.48 |
| `paid_usd_per_period` of one owner | $80 | $10 by default; `realliance` (52) $30; `forgejo-admin` (1) $0.15 |
| `paid_concurrent` of one owner | 20 | 2 by default |

- $10 buys about 85 h of `small`. A launch counts in the period of its creation. A head over the
  ceilings reads `untrusted`.
- **The caps owner** is `forgejo-admin` (id 1), the owner of the paid acceptance suite's jobs. $0.15
  fits one `small` reservation ($0.078), not two, and never a `medium`, so the suite proves the money
  gate at almost no cost. Keep it at $0.15.
- Azure's own limits never read `ci.yaml`: the sweeper's $0.60 an hour and the $85 budget
  (`azure/ci/README.md`).

### Reading paid spend

**One owner,** through the usage API, with the port-forward and `u` from Finding a job's record:

```bash
u '52/usage?period=current' | jq '{paid, global}'
u '52/jobs?period=current&limit=200' | jq '.jobs[] | select(.backend == "aci") | {job_id, class, minutes, cost_usd, state, outcome}'
```

`used_usd` counts settled launches and `reserved_usd` those in flight. `global.paid` reads `open`,
`near-cap` (80 % of the cap) or `closed` (no paid class on and ready, a lock included).

**The month, every owner:** `ci_dispatch_period_usd` with `kind` `used`, `reserved` and `cap`, and
`ci_dispatch_burn_usd_per_hour` for now. `CIDispatchSpendHigh` fires at 80 % and 95 % of the cap, $64
and $76. **Azure's own figure,** the CI group's budget, trails by 8 to 48 h:

```bash
az rest --url 'https://management.azure.com/subscriptions/{subscriptionId}/resourceGroups/rg-realliance-ci/providers/Microsoft.Consumption/budgets/realliance-ci?api-version=2023-05-01' \
  --query '{spent:properties.currentSpend.amount,limit:properties.amount}'
```

### The egress cap

Azure bills a container group's internet egress, so each group may send 10 GB (10^10 bytes) over its
life (E17). Its egress costs its owner $0.087 a GB, outside the reservation and uncapped, since Azure
bills what left, and it counts in the owner's committed money. The price ignores Azure's free tier,
the safe side.

- The dispatcher reads each group's `NetworkBytesTransmittedPerSecond` every 60 s. It deletes its own
  group at the cap: the log line `reaped` with reason `egress-cap`, the `jobs` row's outcome
  `egress-cap`, and `CIDispatchEgressCapHit`. The owner pays the bytes. Each group's rate over the
  last 5 min joins the burn, so a heavy sender stops new launches (`class-full`).
- The sweeper reads the same metric with its own identity each run. It deletes any group at or over
  the cap, whatever its name, and adds each group's 10-min rate to its burn.
- **How the metric reads** (`azure/ci/README.md`, Notes): sustained traffic reads high, while a
  short burst, or anything in a group's first 90 s or so, can read low or 0. It lags about 2 min, so
  a crossing shows within about 5 min, and a fast job can send tens of GB past the cap first; the
  burn term stops new launches meanwhile.
- **Changing the cap or the price** takes both sides at once: `egress` in `azure/ci/fence.json` (the
  sweeper, Chris's apply) and `limits.EgressCapBytes` or `limits.EgressMicroPerGB` in ci-dispatch
  (the dispatcher, a release and a re-pin). The release checklist compares them.

## Changing a limit

Edit `ci.yaml` in `realliance-ops/forge` (clone `~/Repos/realliance-forge`): a signed commit on a
branch, then a PR. Chris merges fast-forward, so the head of `main` stays Owner-signed. The
dispatcher reads the new head at its next sweep, so wait over 5 min after the merge.
`ci_dispatch_config_verified` already reads 1 for the old head, so it can't show that the new one
arrived; a 0 after the wait means the new head failed. The owner's usage (see Finding a job's
record) shows `tiny.minutes_per_day`, the paid limits and `limit_source`. Nothing reports
`tiny_concurrent`, for an owner or global: confirm it by behavior, as a job that used to wait
`owner-busy` or `class-full` gets a `launch` decision while another runs, and `ci-tiny` holds two
runner pods.

| Key | Means | Ceiling in code |
|---|---|---|
| `defaults.tiny_concurrent`, `owners.<name>.tiny_concurrent` | `tiny` jobs at once for one owner | 3 |
| `defaults.tiny_minutes_per_day`, `owners.<name>.tiny_minutes_per_day` | `tiny` minutes a UTC day | 1440 |
| `global.tiny_concurrent` | `tiny` pods at once, all owners | 3 (the pod quota) |
| `paid_usd_per_period`, `paid_concurrent` (defaults, owners), `global.paid_usd_per_period`, `global.paid_usd_per_hour` | Paid classes (Paid limits and the caps owner) | v0.3.0: $80 a period, 20 at once, $0.48 an hour. v0.2.0: $100 and $0.60 |
| `classes` | Turns a class off for everyone | none |
| `owners.<name>: {id: N, …}`, `deny: [{name, id}]` | Overrides and denies | none |

- Limits and denies bind to the numeric `id`. The name is a label: one that no longer matches its id
  raises `CIDispatchConfigFindings`, never a load failure.
- Each user and each org is an owner with its own defaults, so a member with five orgs gets five times
  the per-owner limits. The global caps bound the total. Against abuse, deny the extra ids.
- `global.tiny_concurrent` is 1. Raise it to 2 after a clean day of `tiny`, with an Owner-signed
  commit. Each `tiny` pod adds 512Mi of requests, about 1.7 points of one node
  (`NodeMemoryRequestsHigh`).

**A head that fails.** The dispatcher checks the head of `main`, whatever that commit changed.

| Head | Result |
|---|---|
| Unsigned; a key outside the keyring, expired or revoked; `ci.yaml` missing or invalid; or a committer time over 2 min ahead | State `untrusted`: the last verified config stays in force, `ci_dispatch_config_verified` reads 0, and `CIDispatchConfigUntrusted` fires after 15 min |
| Signed, but its committer time is not later than the last verified head's | Rollback: the same, and no deny lifts |
| Nothing has verified yet (a fresh ledger) | Built-in limits: `tiny` 1 at once and 120 min a day, global 1, paid classes off, no denies |

An Owner signing a new head fixes it. The log line with `"api":"inventory","op":"reload"` gives the
class: `untrusted`, `rollback`, or `unavailable` (Forgejo unreachable).

### Owner signing keys

The keyring sits in this repo, so whoever can change the inventory can't change who signs it. A
keyring edit is a deploy.

- **Refresh or revoke a key by replacing its armored block** in `keyring/owners.asc` with the new
  export. Never append a second copy: the loader refuses a duplicate primary key (`ErrDuplicateKey`),
  and the pod won't start.
- **Rotate to a new key in this order,** or `CIDispatchStoredConfigRejected` fires at the next
  restart:
  1. add the new key to `keyring/owners.asc` and `keyring/fingerprints.txt` (keep forge-bot's keyring
     the same), and deploy;
  2. sign a new head of `realliance-ops/forge` with it, wait over 5 min (one sweep) after the merge,
     then confirm `ci_dispatch_config_verified` reads 1. A 0 means the new head failed: stop, keep the
     old key, and fix the head;
  3. remove the old key, and deploy.
- **After extending a key's expiry** (N4): replace its block with the refreshed export and deploy,
  then sign a new head with it, wait one sweep, and confirm `ci_dispatch_config_verified` reads 1. A
  0 means the new head failed: stop, keep the old key, and fix the head. Without the new head, the
  stored copy keeps its old verification time. Once the key lapses, a restart rejects that copy, the
  dispatcher falls back to the built-in limits, and the denies lift.

## Finding a job's record

A job attempt's key is `<job id>-<attempt>`, and a rerun gets a new key. The image is distroless, so
nothing can `exec` into the pod: read the log, `ci-tiny` and the usage API.

```bash
export KUBECONFIG=/tmp/galaxy-kubeconfig KEY=203-1
kubectl -n ci-dispatch logs ci-dispatch-0 | grep "\"key\":\"$KEY\""
kubectl -n ci-tiny get jobs,pods -l ci-dispatch/key=$KEY
```

The log covers the time since the pod started, and the Job disappears once it ends.

**The decision log in Azure,** by `key`: 31 days, and on a day that hits the 0.25 GB cap the later
lines are missing. The `log-analytics` extension of `az` fails to install under nix, so query through
`az rest`, inside `nix develop`:

```bash
WS=$(az monitor log-analytics workspace show -g rg-luma-homelab-logging -n luma-homelab-logs-workspace \
  --query customerId -o tsv)
Q='luma_homelab_logs_CL
| where TimeGenerated > ago(30d) and kubernetes_namespace_name_s == "ci-dispatch"
| extend j = parse_json(log_s) | where tostring(j.key) == "KEY"
| project TimeGenerated, msg = tostring(j.msg), action = tostring(j.action), reason = tostring(j.reason),
    mode = tostring(j.mode), state = tostring(j.state), outcome = tostring(j.outcome)
| order by TimeGenerated asc'
az rest --method post --resource https://api.loganalytics.io \
  --url "https://api.loganalytics.io/v1/workspaces/$WS/query" \
  --body "$(jq -cn --arg q "${Q/KEY/$KEY}" '{query: $q}')" --query 'tables[0].rows' -o tsv
```

A `decision` line appears once per change of action or reason. `launched`, `launch failed`,
`reaped` and `settled` (state, outcome, charged minutes) follow a launch.

**The ledger, through the `jobs` endpoint of the usage API,** for the job's owner id (`.owner.id` of
`GET /api/v1/repos/<owner>/<repo>`). The bearer is `CI_DISPATCH_USAGE_TOKEN` in sso-secrets
`forge/secrets.env`. A 503 means the dispatcher hasn't finished starting.

```bash
kubectl -n ci-dispatch port-forward svc/ci-dispatch 18081:8081 >/dev/null &
until curl -so /dev/null 127.0.0.1:18081/; do sleep 1; done
T=$(grep '^CI_DISPATCH_USAGE_TOKEN=' ~/Repos/realliance-sso-secrets/forge/secrets.env | cut -d= -f2-)
u() { printf 'Authorization: Bearer %s\n' "$T" | curl -fsS -H @- "http://127.0.0.1:18081/v1/owners/$1"; }
u '52/jobs?period=current&limit=200' | jq '.jobs[] | select(.job_id == 203)'
u '52/usage?period=current' | jq '{tiny, holds, source: .paid.limit_source}'
kill %%
```

The `until` loop waits for the port-forward: any answer, even a 401, means it listens. `kill %%` ends
the port-forward. `period` is `current`, `previous` or `YYYY-MM`, in UTC calendar months. `limit` is 50 by default and
200 at most, newest first. Each launch try is a row, with a `state` (`ended`, `undone`, `reaped`, or
active) and an `outcome` (such as `finished`, `pickup-timeout`, `held` or `error: <step>`).

| Reason | Kind | Means |
|---|---|---|
| `class-off`, `owner-busy`, `class-full`, `not-ready` | wait | The class is off in `config.yaml` or `ci.yaml`; the owner's or the class's `tiny` slots are in use; the dispatcher hasn't read its stored config yet |
| `backoff` | wait | A recent launch was undone: Forgejo held the job (a concurrency group or `needs:`; no try, no charge), or a launch step failed (a try). Waits 1, 2, 5, then 10 min; from the sixth launch that Forgejo held, 60 min |
| `label`, `denied`, `owner-limit` | hold | A label combination no class takes; a denied owner; out of `tiny` minutes for the day |
| `invalid` | hold | The job's id, attempt or owner id is zero or less |
| `launch-failing` | hold | Three tries made no runner, and the key stays held. Fix the cause; the member's rerun gets a new key |
| `concurrency` | hold | Forgejo lists the job `blocked`: its concurrency group holds it behind an earlier run. Nothing launches until it waits again, and a launch already made for it is undone as held at once. Needs a forge with realliance/forgejo#14; on stock Forgejo the job shows `waiting` and the pickup timeout catches it as `backoff` |
| `no-budget`, `global-cap` | hold | Paid only: the owner's or the global paid limit is 0; the global money this period can't cover the reservation |

For a paid job, `owner-busy`, `class-full`, `owner-limit` and `not-ready` read as in Paid classes'
gate: paid slots, the hourly cap, the owner's dollars, and readiness.

## Alerts

The nine rules in `cluster/ci-dispatch/dispatcher/prometheusrule.yaml` are all `severity: warning`.
`CIDispatchOrphanCompute` routes by name to `critical-alerts` (Discord, around the clock) since
0db39a77; the rest go to `homelab-default` (Discord, weekdays).

**From the v0.3.0 deploy** the file holds 18 rules: the group `ci-dispatch-paid` adds nine, and
`CIDispatchOrphanCompute` turns `critical`. The 0db39a77 route sends
`CIDispatch(SpendHigh|Locked|OrphanCompute|Drift|PaidNotReady)` and `CISweeperStale` to
`critical-alerts` by name, whatever the severity, so `CIDispatchDrift` and `CIDispatchPaidNotReady`
page around the clock although they're `warning`. `CIDispatchEgressCapHit` and
`CIDispatchAzureFailing` go to `homelab-default`.

| Alert | Means | Do |
|---|---|---|
| `CIDispatchStale` | No loop with every read OK for 5 min, or no metric | `kubectl -n ci-dispatch get pod ci-dispatch-0`, its log, `ci_dispatch_errors_total` by `api`. A Forgejo or API outage never restarts the pod. See Known limits for node loss and the job listing |
| `CIDispatchLaunchFailing`, `reason: held` | A job holds as `launch-failing` | `kubectl -n ci-tiny get events`, the pod's `netpol-gate` log. Then the member reruns the job |
| `CIDispatchLaunchFailing`, `reason: errors` | Launch steps fail for 15 min | The `launch failed` log lines name the step |
| `CIDispatchConfigUntrusted` | The head of `realliance-ops/forge` `main` has not verified for 15 min, or Forgejo was unreachable at each reload | See "A head that fails" |
| `CIDispatchStoredConfigRejected` | At start, the ledger's copy of `ci.yaml` failed its proof. The common cause: its signer left the keyring (a rotated or expired key) | Once `ci_dispatch_config_verified` is 1, `kubectl -n ci-dispatch delete pod ci-dispatch-0` clears it. With no key rotated or expired, treat it as tampering |
| `CIDispatchConfigFindings` | For 30 min, an owner name in `ci.yaml` no longer matches its id, or looking up the names failed. From v0.3.0, also `paid-class-unlaunchable`: a paid class is on, but `global.paid_usd_per_hour` is unset or 0, or no owner has a `paid_concurrent` over 0 | Fix the name in `ci.yaml`. The limits still bind to the ids. For `paid-class-unlaunchable`, check those two keys |
| `CIDispatchLaunchesUndone` | Over 7 undone launches an hour, for an hour | Jobs that Forgejo holds keep taking the slot: look for `held` rows in `jobs`. `for: 1h` is load-bearing, never shorten it |
| `CIDispatchOrphanCompute` | For 15 min, a `ci-tiny` Job or runner with no launch, or an expired Job still there | `ci_dispatch_orphans` by `kind`. A `tiny` orphan pod dies at its own 20 min deadline; delete a Job that outlives it by hand (`kubectl -n ci-tiny get jobs`). A `runner refused` log line is a `ci-tiny-*` runner that failed the uuid or scope check: treat it as an incident. From v0.3.0, `kind="group"` is a container group in `rg-realliance-ci` that bills until it's gone: if the reaper hasn't removed it, delete it as the break-glass does |
| `CITinyQuotaFull` | `ci-tiny` at its 3 pods for 30 min | `kubectl -n ci-tiny get pods`. With global `tiny_concurrent` 1, three pods means leftovers. A reaped pod may run out its 30 s grace while its replacement starts; the quota bounds that |
| `CIDispatchSpendHigh`, `level` 80 or 95 | The period's used plus reserved money is at 80 % ($64) or 95 % ($76) of the $80 cap, for 5 min | Reading paid spend. The gate stops at the cap by itself; pause the paid classes if the spend looks wrong |
| `CIDispatchLocked` | Azure answered with the sweeper's lock, for 2 min | After a trip |
| `CISweeperStale`, `reason: stale` | The sweeper has no finished run in 30 min | Its runs (After a trip, step 1). With the sweeper turned off or hung, only the budget bounds Azure compute: pause `small` and `medium` until it runs again |
| `CISweeperStale`, `reason: absent` | No sweeper timestamp for 30 min: the watcher can't read the run history, or the dispatcher isn't running | `CIDispatchStale` covers a missing dispatcher. Expect it during a break-glass longer than 30 min, or a node loss: silence it then |
| `CIDispatchDrift` | The ledger and Azure differ by over 10 % for a closed day or period | The month-end drift check. The gauge holds the ratio, so the alert repeats every 30 min until the next one: silence it once read |
| `CIDispatchPaidNotReady` | A paid class in `dry-run` or `on` not ready for 30 min (the `class` label) | The log's `"api":"azure"` and `"api":"prices"` lines, `ci_dispatch_azure_locked`, `ci_dispatch_price_age_seconds`. In `dry-run` it's a rollback trigger |
| `CIDispatchEgressCapHit` | The dispatcher deleted a group at the 10 GB egress cap in the last hour | The `reaped` line's `key` names the job. The owner paid the bytes; ask whether the job meant to send them |
| `CIDispatchAzureFailing` | Azure calls keep failing for 30 min, with no lock | The common cause: an expired or rotated client secret, with an AADSTS code in the log. Rotate (The Azure credential), else wait out an Azure outage |

## After a trip: the Azure lock

The sweeper's lock (`azure/ci/README.md`) shows as `CIDispatchLocked`, `ci_dispatch_azure_locked` 1,
paid jobs waiting `not-ready`, and the sweeper's emails. `tiny` carries on.

1. **Find out why,** before anything else. The sweeper's newest runs, id-free fields only:

   ```bash
   az rest --url 'https://management.azure.com/subscriptions/{subscriptionId}/resourceGroups/rg-realliance-ci-guard/providers/Microsoft.Logic/workflows/ci-sweeper/runs?api-version=2019-05-01&$top=6' \
     --query 'value[].{start:properties.startTime,status:properties.status,code:properties.code,error:properties.error.code,why:properties.error.message}'
   ```

   A run that locked reads `status` `Failed`, `code` `Terminated` and `error.code` `Locked`, and so
   does every sweep after it until the restore. `Cancelled` is `LockFailed`: the assignment delete
   failed, the groups went anyway, and every sweep retries. `error.code` `Degraded` is a failed read,
   delete or tag write, not a lock. Which rule tripped:

   - **the budget:** the budget query in Reading paid spend shows `spent` at $85 or more. That lock
     holds until the month rolls over or Chris raises the budget in `azure/ci/`: while the spend stays
     there, the next sweep locks a restored assignment again;
   - **the rate:** `az group show -n rg-realliance-ci --query tags` lists `ci-sweeper-over-at`, which
     the sweep before the lock set;
   - **neither:** someone deleted the assignment by hand. The CI group's Activity Log names the
     caller of the delete that raised `ci-sweeper-locked`.

   Then the dispatcher's side: `kubectl -n ci-dispatch logs ci-dispatch-0 | grep '"api":"azure"'`, and
   the history of `ci_dispatch_burn_usd_per_hour`, `ci_dispatch_orphans{kind="group"}` and
   `ci_dispatch_reaped_total{reason="egress-cap"}`. A burn under $0.48 while the sweeper saw over
   $0.60 points at egress, or at groups the dispatcher never listed.
2. **Fix the cause,** and pause the paid classes until it's fixed.
3. **Only Chris unlocks.** In a luma-homeops checkout, with `SCRATCH` a directory only he can read:

   ```bash
   ./azure/ci/tf.sh plan -no-color -out="$SCRATCH/ci.tfplan" | grep -E '^(Plan:|No changes|  # )'
   ./azure/ci/tf.sh apply "$SCRATCH/ci.tfplan"
   ```

   The plan must read `# azurerm_role_assignment.dispatcher will be created` and
   `Plan: 1 to add, 0 to change, 0 to destroy.`: the assignment, under its fixed name. **From the
   rotation due date (2027-04-05) on,** it also creates `time_rotating.dispatcher` and replaces
   `azuread_application_password.dispatcher`: `Plan: 3 to add, 0 to change, 1 to destroy.` That
   apply destroys the old client secret, so in the same STOP run The Azure credential, steps 2 to 5.
   Anything else: stop.
4. **Check.** The next sweep ends `Succeeded` and removes both `ci-sweeper-*` tags. If it ends
   `Locked` with no cause, a sweep that read the old state raced the apply: apply again.
   `ci_dispatch_azure_locked` clears within one poll (30 s) of the assignment taking effect, and
   `ci_dispatch_paid_ready` returns to 1.
5. **Resume** `small` and `medium` as you paused them, a deploy with its own STOP.

While locked, the dispatcher can't read the groups, so launches in flight keep their reservations
until the first good list after the unlock settles them.

## Rotating credentials

Rotate the dispatcher token yearly (first minted 2026-11-04). Values never go in argv, chat, logs, or
commits. Run the luma-homeops steps in a worktree, inside `nix develop` for `kubeseal`, with
`KUBECONFIG` set. `yq` (the Go one, v4) comes from the user profile (`/etc/profiles/per-user/chris/bin`);
the flake doesn't ship it. To reseal, delete the sealed file first, since `sign.sh` skips a file whose sealed
sibling exists, and run `bash -O globstar ./sign.sh`, because without `globstar` it misses
`cluster/ci-dispatch/dispatcher/`. Each luma-homeops push here is a deploy, so STOP and ask Chris.

**On suspicion, revoke first.** A planned rotation runs the steps in order, so nothing goes down.
On suspicion:

- **Dispatcher token:** break-glass, then its step 4 (revoke), then steps 1 and 2, then commit and
  push. Argo stays paused from the break-glass, so the new Secret reaches the cluster only when you
  restore Argo (Break-glass, last paragraph). The dispatcher then starts and reads it, with no pod
  delete.
- **Runner secret:** its step 4 (delete the old row) first, then steps 1 to 3. The break-glass
  doesn't pause the Argo app `forgejo-runner`, so step 3 runs as written. The CI of
  `realliance-ops/ci-dispatch` waits until then. Nobody has tested deleting a row while the runner
  still holds that connection, so the CI of `galaxy-forge-bot` may stall until step 3 as well.
- **Azure credential:** the whole rotation at once, planned with `-replace=time_rotating.dispatcher`,
  which restarts the 150 days and replaces the secret (The Azure credential). The old secret dies at
  Chris's apply.

### The dispatcher token (`CI_DISPATCH_FORGEJO_TOKEN`)

The API token of the site administrator `ci-dispatch`: next to `forgejo-admin`'s, the forge's most
powerful credential. Secret `ci-dispatch-forgejo`, key `forgejo-token`, source
`cluster/ci-dispatch/dispatcher/forgejo-token.secret.yaml`. The process reads it once, at start.

1. Mint a new token under a new name, straight into sso-secrets. If the mint prints nothing, nothing
   changes:

   ```bash
   cd ~/Repos/realliance-sso-secrets
   kubectl -n forgejo exec deploy/forgejo -c forgejo -- forgejo admin user generate-access-token \
     --username ci-dispatch --token-name "ci-dispatch-$(date +%F)" --raw \
     --scopes "read:admin,write:admin,read:repository,read:user,read:organization" \
     | { read -r t && [ -n "$t" ] && sed -i '/^CI_DISPATCH_FORGEJO_TOKEN=/d' forge/secrets.env \
         && printf 'CI_DISPATCH_FORGEJO_TOKEN=%s\n' "$t" >> forge/secrets.env; }
   git commit -S -am "forge: rotate the ci-dispatch API token" && git push
   ```

2. Copy it into the source, and reseal:

   ```bash
   f=cluster/ci-dispatch/dispatcher/forgejo-token
   T=$(grep '^CI_DISPATCH_FORGEJO_TOKEN=' ~/Repos/realliance-sso-secrets/forge/secrets.env | cut -d= -f2-) \
     yq -i '.stringData."forgejo-token" = strenv(T)' $f.secret.yaml
   rm $f.yaml && bash -O globstar ./sign.sh && git diff --no-textconv --stat
   ```

3. Commit signed. STOP: ask Chris. Push. Once Argo has synced, run
   `kubectl -n ci-dispatch delete pod ci-dispatch-0`, and wait for `Ready` and
   `ci_dispatch_config_verified` 1.
4. Revoke the old token. STOP: ask Chris (a database write). Set the two names, then run
   [the guarded token delete](#the-guarded-token-delete). `OLD` is the old token's name, from
   `names`; until the first rotation, that name is `ci-dispatch`.

   ```bash
   ACCT=ci-dispatch OLD=ci-dispatch
   ```

   Then run realliance/sso `tests/forge/test_forge_ci_dispatch.py`.

### The guarded token delete

It runs on the `acid-forgejo` leader, as done for forge-ci's token on 2026-11-04
(`docs/superpowers/plans/2026-11-04-bots-to-realliance-ops.ledger.md`): one transaction that raises
unless it deleted exactly one row. The block sets nothing itself: the caller sets `ACCT` and `OLD`
first, and an unset one aborts it. `names` lists the account's token names before and after, and the
delete runs only if `OLD` is among them. `docs/forgejo.md` uses the same block for `realliance-ci`.
Never use the API route `DELETE /api/v1/users/<account>/tokens/<name>`: the token routes take only
password basic authentication, which this forge turns off.

```bash
: "${ACCT:?set ACCT, the account}" "${OLD:?set OLD, the old token name}"
L=$(kubectl -n forgejo get pod -l application=spilo,galaxy=acid-forgejo,spilo-role=master -o name)
names() { kubectl -n forgejo exec "$L" -c postgres -- psql -U postgres -d forgejo -Atc \
  "SELECT name FROM access_token WHERE uid = (SELECT id FROM \"user\" WHERE lower_name = '$ACCT')"; }
names
if names | grep -qx -- "$OLD"; then
kubectl -n forgejo exec -i "$L" -c postgres -- psql -U postgres -d forgejo -v ON_ERROR_STOP=1 <<SQL
DO \$\$ DECLARE n int; BEGIN
  DELETE FROM access_token WHERE name = '$OLD'
    AND uid = (SELECT id FROM "user" WHERE lower_name = '$ACCT');
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 1 THEN RAISE EXCEPTION 'expected 1 row, deleted %', n; END IF;
END \$\$;
SQL
else echo "STOP: $ACCT holds no token named $OLD"; fi
names
```

### The usage token (`CI_DISPATCH_USAGE_TOKEN`)

The bearer token of the usage API. Secret `ci-dispatch-usage`, key `usage-token`, source
`cluster/ci-dispatch/dispatcher/usage-token.secret.yaml`. It must be one printable line with no
whitespace, or the process refuses to start. The process reads it once, at start.

1. A new token in sso-secrets, checked by counts only: the two counts must read 1 and 0.

   ```bash
   cd ~/Repos/realliance-sso-secrets
   openssl rand -hex 32 | { read -r t && [ -n "$t" ] && sed -i '/^CI_DISPATCH_USAGE_TOKEN=/d' forge/secrets.env \
     && printf 'CI_DISPATCH_USAGE_TOKEN=%s\n' "$t" >> forge/secrets.env; }
   grep '^CI_DISPATCH_USAGE_TOKEN=' forge/secrets.env | cut -d= -f2- | wc -l
   grep '^CI_DISPATCH_USAGE_TOKEN=' forge/secrets.env | cut -d= -f2- | LC_ALL=C grep -c '[^[:graph:]]'
   git commit -S -am "forge: rotate the ci-dispatch usage token" && git push
   ```

2. Copy it into the source, and reseal:

   ```bash
   f=cluster/ci-dispatch/dispatcher/usage-token
   T=$(grep '^CI_DISPATCH_USAGE_TOKEN=' ~/Repos/realliance-sso-secrets/forge/secrets.env | cut -d= -f2-) \
     yq -i '.stringData."usage-token" = strenv(T)' $f.secret.yaml
   rm $f.yaml && bash -O globstar ./sign.sh && git diff --no-textconv --stat
   ```

3. Step 3 of the dispatcher token. The old token stops working at the restart. Check the new one
   with a single call: `u '1/usage?period=current' | jq .owner_id` prints 1 (Finding a job's record).

### The `galaxy-ci-dispatch` runner secret (`RUNNER_CI_DISPATCH_SECRET`)

The `galaxy` runner's repo-scoped connection for `realliance-ops/ci-dispatch`: sso-secrets
`forge/runner.env`, and the `galaxy-ci-dispatch` entry of `server.connections` in
`cluster/forgejo-runner/runner-config.secret.yaml`. Rotate it while the runner is idle, with no
`running` or `waiting` task on `realliance-ops/ci-dispatch` or `realliance-ops/forge-bot`.

1. A new secret, and a new registration. It prints the new runner's UUID.

   ```bash
   cd ~/Repos/realliance-sso-secrets
   openssl rand -hex 20 | { read -r t && [ -n "$t" ] && sed -i '/^RUNNER_CI_DISPATCH_SECRET=/d' forge/runner.env \
     && printf 'RUNNER_CI_DISPATCH_SECRET=%s\n' "$t" >> forge/runner.env; }
   git commit -S -am "forge: rotate the galaxy-ci-dispatch runner secret" && git push
   (. forge/runner.env; printf %s "$RUNNER_CI_DISPATCH_SECRET") | kubectl -n forgejo exec -i deploy/forgejo \
     -c forgejo -- forgejo forgejo-cli actions register --name galaxy-ci-dispatch \
     --scope realliance-ops/ci-dispatch --labels galaxy --secret-stdin=-
   ```

2. Set that connection's `uuid` and `token` in the source. The UUID is the bytes of the secret's
   first 16 characters, so the check line compares the two:

   ```bash
   f=cluster/forgejo-runner/runner-config.secret.yaml
   S=$(grep '^RUNNER_CI_DISPATCH_SECRET=' ~/Repos/realliance-sso-secrets/forge/runner.env | cut -d= -f2-)
   U=<the UUID step 1 printed>
   [ "$(printf %s "$U" | tr -d -)" = "$(printf %s "${S:0:16}" | od -An -tx1 | tr -d ' \n')" ] || echo MISMATCH
   S=$S U=$U yq -i '.data."config.yml" |= (@base64d | from_yaml
     | .server.connections."galaxy-ci-dispatch".uuid = strenv(U)
     | .server.connections."galaxy-ci-dispatch".token = strenv(S) | to_yaml | @base64)' $f
   yq '.data."config.yml" | @base64d | from_yaml | .server.connections | keys' $f
   unset S
   ```

   The `keys` check must list `galaxy-forge-bot` and `galaxy-ci-dispatch`, plus any comment inside
   `connections`. A lone `galaxy-ci-dispatch` means the decode failed and the rest of the config is
   gone: `git checkout` the file and start again. Never use `create-runner-file`, which takes the
   secret in argv.
3. `rm cluster/forgejo-runner/runner-config.yaml`, reseal, commit signed. STOP: ask Chris. Push. A
   secret-only change doesn't restart the runner, so run
   `kubectl -n forgejo-runner rollout restart deploy/forgejo-runner`. Its log shows
   `declared successfully` for both connections.
4. Delete the old row (row 18 until the first rotation) as an administrator, with the
   `FORGEJO_ADMIN_TOKEN` header as in the weekly check:
   `DELETE /api/v1/repos/realliance-ops/ci-dispatch/actions/runners/<old id>`. Then run realliance/sso
   `test_galaxy_serves_forge_bot_and_ci_dispatch_only`.

### The Azure credential (`CI_DISPATCH_AZURE_*`)

The first apply of `azure/ci/` on or after **2027-04-05** (then every 150 days; the comment in
`azure/ci/fence.tf`) replaces the client secret and destroys the old one at once, so from that apply
until the pod restarts on the new one, the paid classes wait `not-ready`. Run it as one STOP, back to
back, inside the 30 min before `CIDispatchPaidNotReady` pages. Only the client secret changes.
Without an apply, the secret still dies on **2027-05-05**, 180 days after its creation, and the paid
classes stop with it (`CIDispatchAzureFailing`).

1. STOP: ask Chris. He plans and applies, in a luma-homeops checkout. The plan must change only
   `time_rotating.dispatcher` and `azuread_application_password.dispatcher`:

   ```bash
   ./azure/ci/tf.sh plan -no-color -out="$SCRATCH/ci.tfplan" | grep -E '^(Plan:|No changes|  # )'
   ./azure/ci/tf.sh apply "$SCRATCH/ci.tfplan"
   ```

2. In the same checkout, the new secret straight into sso-secrets. The count must print 1:

   ```bash
   E=~/Repos/realliance-sso-secrets/forge/secrets.env
   sed -i '/^CI_DISPATCH_AZURE_CLIENT_SECRET=/d' $E
   printf 'CI_DISPATCH_AZURE_CLIENT_SECRET=' >> $E
   ./azure/ci/tf.sh --append $E output -raw dispatcher_client_secret
   echo >> $E
   grep -c '^CI_DISPATCH_AZURE_CLIENT_SECRET=.' $E
   git -C ~/Repos/realliance-sso-secrets commit -S -am "forge: rotate the ci-dispatch Azure client secret"
   git -C ~/Repos/realliance-sso-secrets push
   ```

3. Copy it into the source, and reseal. The `del` keeps the key in one place, whichever form the
   source holds it in:

   ```bash
   f=cluster/ci-dispatch/dispatcher/azure-credentials
   T=$(grep '^CI_DISPATCH_AZURE_CLIENT_SECRET=' ~/Repos/realliance-sso-secrets/forge/secrets.env | cut -d= -f2-) \
     yq -i 'del(.data."client-secret") | .stringData."client-secret" = strenv(T)' $f.secret.yaml
   rm $f.yaml && bash -O globstar ./sign.sh && git diff --no-textconv --stat
   ```

4. Commit signed, and push: the deploy this STOP covers. Once Argo has synced, run
   `kubectl -n ci-dispatch delete pod ci-dispatch-0`, because the process reads the credential once,
   at start. Then wait for `Ready`, `ci_dispatch_paid_ready` 1 for both classes, and a flat
   `ci_dispatch_errors_total{api="azure"}`.
5. Ledger the next due date: the day of the apply plus 150 days.

## Weekly check

The plan's D19. Until forge-bot's `ForgeUnexpectedAdmin` ships, compare three lists by hand each
week. A stolen dispatcher token's easiest backdoor is a new local administrator, or a runner that
receives other repos' jobs.

```bash
cd ~/Repos/realliance-sso && nix develop --command pytest tests/forge/test_forge_ci_dispatch.py -k site_admins
T=$(grep '^FORGEJO_ADMIN_TOKEN=' ~/Repos/realliance-sso-secrets/forge/secrets.env | cut -d= -f2-)
get() {
  for p in $(seq 1 10); do
    printf 'Authorization: token %s\n' "$T" \
      | curl -fsS -H @- "https://git.realliance.net/api/v1$1&limit=50&page=$p"
  done | jq -s 'map(. // []) | add'
}
get '/admin/users?' | jq -r '.[] | select(.is_admin) | .login'
get '/admin/users?' | jq -r '.[] | select(.source_id == 0) | .login'
get '/admin/actions/runners?visible=true' \
  | jq -r '.[] | select(.ephemeral | not) | "\(.id) \(.name) owner=\(.owner_id) repo=\(.repo_id)"'
```

`get '<path>?<query>'` reads every page of an administrator listing.

| List | Expected |
|---|---|
| Site administrators | `forgejo-admin`, `forge-bot`, `ci-dispatch`, and the members of team `Owners` in `realliance-ops` |
| Local accounts (`source_id` 0) | `forgejo-admin`, `forge-bot`, `forge-ci` (retired, kept for history), `ops-ci`, `ci-dispatch`, `realliance-ci` (the package pusher of realliance.net) |
| Non-ephemeral runners | `galaxy-forge-bot owner=0 repo=94` (`realliance-ops/forge-bot`), `galaxy-ci-dispatch owner=0 repo=240` (`realliance-ops/ci-dispatch`), `general-realliance-net owner=0 repo=229` (`realliance/realliance.net`) |

A runner a member registered on their own repo or org is theirs. Treat any of these as an incident
(break-glass, then rotate the dispatcher token): an unknown administrator or local account; an
instance-scope runner (`owner=0 repo=0`); an unknown runner scoped to org `realliance` (52) or
`realliance-ops` (208), or to one of their repos.

## Runner drill at every Forgejo upgrade (D14)

Everything here rests on Forgejo's `--handle` semantics. Re-run the phase 0 drill at each Forgejo
upgrade, the 19 LTS hop included, from `~/Repos/realliance-sso`:

```bash
kubectl create namespace ci-probe
kubectl label namespace ci-probe pod-security.kubernetes.io/enforce=restricted
CI_DRILL=runner-proofs CI_DRILL_REPORT=<scratch>/runner-proofs.json \
  nix develop --command pytest tests/forge/test_forge_runner_proofs.py -v
kubectl delete namespace ci-probe
```

Deleting the namespace is an S4 ask: ask Chris first.

- Run one drill at a time. Its leftover sweep has no lock, so a second drill can delete the first
  one's runners.
- No real drill has used its 10 s release bound yet, so the first one may need it calibrated.
- The dispatcher's reaper never touches the drill's `ci-probe-*` runners.

## Sweeper drill after a change to `azure/ci/sweeper*`

`scripts/test_azure_ci.py` runs the sweeper's workflow in a simulator, which proves its logic but
not Azure's answers. After any applied change to `azure/ci/sweeper.tf` or `azure/ci/sweeper/`,
re-run phase 2 Task 5's drill. It first passed on 2026-11-06, and the phase 2 ledger holds its
timings, its group body and its cost, about $0.61.

1. STOP: ask Chris to pre-approve the whole list: groups as his identity in the CI group for about
   2.5 h, the sweeper deleting the dispatcher's assignment, `TestLiveLocked` while locked, and his
   restore apply.
2. Pause `small` and `medium` first. The dispatcher counts the drill's groups in its burn, and the lock
   pages `CIDispatchLocked` and `CIDispatchPaidNotReady` around the clock: expect both.
3. Run the plan's Task 5, Steps 2 to 5: the age limit, the rate trip, the lock, and Chris's restore
   (After a trip, step 3). The reaper never touches a `ci-drill-*` group.

## The month-end drift check

The watcher compares the ledger's paid charges with Cost Management's for the CI group: each day
from 06:00 UTC for the day before yesterday, once Azure's figure for it reaches $0.50, and from the
third day of each month for the whole month before, once per process, with no floor. On the third,
read `ci_dispatch_drift_ratio{window="period"}`: under 0.10 meets the spec. Over 0.10 fires
`CIDispatchDrift`. Then:

1. Read the ledger's daily rows from last night's snapshot, on the laptop, with the restic
   environment of `docs/backups.md` (Inspect / restore a file):

   ```bash
   nix develop --command restic restore latest --host ci-dispatch-ledger --target ./out
   ls ./out/data/snapshots
   nix shell nixpkgs#sqlite --command sqlite3 ./out/data/snapshots/<newest file> \
     'SELECT day, ledger_micro, azure_micro FROM drift ORDER BY day'
   ```

2. Compare them with the portal's Cost analysis for `rg-realliance-ci`, by day, and find the day that
   moved.
3. The likely causes: rounding; the tail from a group's stop, where the ledger stops charging, to its
   delete, where Azure stops billing; a launch undone after its PUT; egress, which reads high on
   sustained traffic and low on a short burst, ignores the free tier, and misses the last 2 min or
   so before a delete; and groups no launch owns, such as a drill's. Fix the cause, and check again
   next month.

## Releasing a dispatcher

The review, the Owner-signed tag and the re-pin by hand stay as before (phase 1 Task 14, phase 2
Task 16). Before the tag, three copies must agree. Fetch both repos, then set `REL` to the commit to
tag:

```bash
REL=<the commit to tag>
for f in $(git -C ~/Repos/luma-homeops ls-tree --name-only origin/main azure/ci/policies/); do
  cmp <(git -C ~/Repos/luma-homeops show "origin/main:$f") \
    <(git -C ~/Repos/ci-dispatch show "$REL:internal/aci/testdata/policies/${f##*/}") || echo "DIFFERS: $f"
done
git -C ~/Repos/ci-dispatch ls-tree --name-only "$REL" internal/aci/testdata/policies/ | wc -l
```

1. **The Policy rules** in `internal/aci/testdata/policies/` (`fence_test.go`) are byte-for-byte
   copies of `azure/ci/policies/*.json`: no `DIFFERS` line, and a count of 6.
2. **The constants** (F19): the shape prices, 117_000 and 233_000 micro-dollars an hour and 1_000_000
   for any other shape, and the egress cap and price, 10_000_000_000 bytes and 87_000 a GB, must be
   equal in `internal/prices`, `internal/limits` and `azure/ci/fence.json`. The same goes for the $80
   period cap, `limits.CeilingPeriod` and `paid_cap_usd`.
3. **The config** (F9): `internal/config/testdata/site-config.yaml` equals
   `cluster/ci-dispatch/dispatcher/config.yaml`, apart from the class image digest.

## Known limits (accepted)

- **The job listing is one read, capped at 16 MiB.** Forgejo 15's
  `/api/v1/admin/actions/runners/jobs` does not page. Past about 80k listed jobs that fit the five
  class labels, every tick fails and nothing launches for anyone, paid classes included
  (`CIDispatchStale`). Running jobs keep their hard stops, and the reaper and the sweeper keep
  deleting, since neither needs Forgejo. Cancel the flood, or turn off Actions on its repo. The fix,
  a paged listing, is on the fork's `WISHLIST.md`.
- **A paid charge trails its delete.** A paid launch settles only once a list lacks its group and a
  GET confirms it gone, never on an accepted DELETE. A spend figure can trail the delete by one poll
  (30 s) plus the group's `Deleting` phase.
- **Egress just before a delete is never charged.** The metric lags about 2 min, and a deleted
  group's metric answers not found, so the last 2 min or so of its egress reach neither the owner nor
  the burn. The 10 GB cap bounds it.
- **A compromised dispatcher can outrun the egress backstops** (Chris accepted it, 2026-11-05, over a
  VNet and NAT gateway at about $32 a month). The sweeper's egress and rate rules never see groups
  that live shorter than one sweep, 10 min. Then only the $85 budget bounds them, 8 to 48 h late, and
  egress can cost more than the $0.60 an hour the spec's worst case assumes.
- **A charge can be off once, by up to 20 min,** in three crash or failed-read windows of
  milliseconds: a held launch settles `lost` (about 3 min charged, a try counted); a job resumed
  just before a crash settles `restart` (0 charged though member code ran); an expiry reap on a tick
  whose ledger read failed settles `finished`, `lost` or `restart`, though
  `ci_dispatch_reaped_total{reason="expired"}` counted it.
- **Never add a "LAN = trusted" middleware or IP allowlist while `tiny` exists.** A `tiny` job's 443
  to a public host on galaxy hairpins through the router, so Traefik's backends see 192.168.0.1 or a
  node address. The real fix is the router follow-up in section 10 of the NetworkPolicy spec.
- **Node loss leaves the dispatcher down** until the node returns, or until Chris confirms the node is
  off and the pod is force-deleted (S4). The StatefulSet never runs two. The Jobs' own deadlines bound
  what is running, and so do the paid groups' `timeout` wrappers and the sweeper's 90 min.
- **`scripts/netpol-verify.sh stray`.** A `--since 24h` window reads `SHORT` for about a day after an
  agent's log rotates, which happens every two days or so. `ci-tiny` pods churn, and over a long
  window a stale mapping hides as `GONE` once two pods have held one IP. Use `--since 30m` around
  `tiny`.

## Rollback

- **Normal: pause `tiny`** (see Pausing `tiny`): `tiny: "off"` and its test expectation, in one
  signed commit. For the paid classes, pause `small` and `medium` the same way.
- **A bad config or image:** revert the commit and push, which is a deploy. If the rollout stalled on
  a pod that never got `Ready`, delete `ci-dispatch-0` by hand once the revert has synced.
- **v0.3.0 itself** (E13). It moves the ledger to schema v2 at start, in one transaction, and every
  earlier release (v0.1.0, v0.2.0) refuses a v2 ledger and crash-loops. The pod's
  `kubectl -n ci-dispatch logs ci-dispatch-0 --previous` tells which case applies. The process opens
  the ledger right after it loads `config.yaml`:
  - a `config.yaml` error, or `ledger: migrate to version 2: …`, leaves the ledger at v1. Revert the
    whole deploy, every commit of it, so the pin, `config.yaml`, the Azure credential and the restic
    job go back together. Push, and once Argo has synced, run
    `kubectl -n ci-dispatch delete pod ci-dispatch-0`: a StatefulSet doesn't replace a pod that never
    got `Ready`;
  - anything later leaves it at v2. Pause `small` and `medium` and keep v0.3.0. Go below it only by
    Downgrading below v0.3.0.
- **Remove it all:** `resources: []` in `cluster/ci-dispatch/kustomization.yaml`, with
  `scripts/test_ci_dispatch_manifests.py` updated in the same signed commit. On an empty render the
  hook `ci-dispatch-manifests` fails `test_renders_with_namespaces_set`, `test_namespaces_restricted`,
  `test_rbac_exact` and `test_no_plain_secrets`. Never `--no-verify` (S6). Argo prunes the rest but
  keeps the Namespace `ci-dispatch` and the PVC (`Prune=false,Delete=false`), so the ledger survives.
  Never delete `cluster/applications/ci-dispatch.yaml`: the app carries no `resources-finalizer`, so
  that orphans everything and leaves it running.

### Restoring the ledger

The spec's rule: **after losing the ledger, the paid classes stay `off` until a restore.** An empty
ledger forgets every reservation and charge, so each owner could spend their limit again. `tiny` may
run meanwhile, at the cost of at most a day of its minutes.

Each night from 03:00 UTC the dispatcher writes a whole, consistent copy,
`/data/snapshots/ledger-<YYYYMMDD>T<HHMMSS>Z.db` (`VACUUM INTO`), and keeps the newest seven. From the
v0.3.0 deploy on, `restic-ci-dispatch` copies that directory to restic host `ci-dispatch-ledger` at
03:50.

1. STOP: ask Chris (S4: this scales a StatefulSet to 0). Pause the paid classes in git first if
   they're on.
2. Stop the dispatcher as the break-glass does: pause both Argo apps, scale to 0, and wait for the pod
   to go. Expect `CIDispatchStale` at once, and `CISweeperStale` `absent` (24/7) after 30 min.
3. Choose the file and the restic snapshot that holds it, on the laptop (`docs/backups.md`,
   Inspect / restore a file): `restic snapshots --host ci-dispatch-ledger`, then
   `restic ls <id> /data/snapshots`.
4. With the break-glass's `KUBECONFIG=/tmp/galaxy-kubeconfig` still exported, run
   `scripts/ci-dispatch-ledger-restore.sh <file> <id>`. It prints the kubectl context it uses. It
   refuses on any kubectl error, while either Argo app still syncs automatically, unless the
   StatefulSet is at 0, and while its pod exists; then it starts pod `ledger-restore`. The pod takes
   the file from the volume when it's still there, else from that restic snapshot, and checks the
   copy against its source and for the SQLite header before it moves `ledger.db` and its `-wal` and
   `-shm` files into `/data/before-restore-<time>/`.
   Watch `kubectl -n ci-dispatch get pod ledger-restore` until it reads `Completed`: the volume may
   take minutes to move between nodes, so wait (S3). Read its log, then delete the pod. `Error` means
   nothing moved, since any file it had set aside goes back; only if the log says "move it back by
   hand" did one stay in `before-restore-*`. Fix the file or the id, delete the pod, and run it again.
5. Patch `applications` back as the break-glass's last paragraph says, with no pause commit. That
   restores the policy of `ci-dispatch` from git, which scales the dispatcher back up on the restored
   ledger. Its reconcile and reaper remove what no row knows: a group or runner launched after the
   snapshot goes after 10 min.
6. Every launch after the snapshot drops out of the accounting, `tiny` minutes included. Ledger the
   restore, then turn the paid classes back on, a deploy with its own STOP.

### Downgrading below v0.3.0

F10: v0.1.0 and v0.2.0 refuse schema v2, so going back to v0.2.0, the pin before the v0.3.0 deploy,
takes a **v1** copy of the ledger. Its source is restic host **`ci-dispatch-ledger-pre-v0.3.0`**: one
snapshot of `/data/snapshots`, taken after the v0.3.0 deploy and before the next 03:00 UTC copy
(phase 2 Task 19). The migration rewrites only `ledger.db`, so until then the directory holds only
v0.2.0's v1 copies. `restic forget --group-by host` keeps a host's only snapshot under every keep
rule, so it stays. The paid classes stay `off`, since v0.2.0 has none.

1. On the laptop, with the restic environment of `docs/backups.md`: `restic snapshots --host
   ci-dispatch-ledger-pre-v0.3.0` gives the id, and `restic ls <id> /data/snapshots` the files. Take
   the newest file whose schema version prints 1:

   ```bash
   nix develop --command restic restore <id> --include /data/snapshots/<the file> --target ./out
   nix shell nixpkgs#sqlite --command sqlite3 ./out/data/snapshots/<the file> 'SELECT schema_version FROM meta'
   ```

2. Run Restoring the ledger, steps 1 to 4, with that file and id. Before step 5, revert the whole
   v0.3.0 deploy in git, every commit of it, and push, so that Argo brings back the v0.2.0 pin.
3. Once the pod runs v0.2.0, confirm `ci_dispatch_config_verified` 1, and delete any paid group still
   listed, as the break-glass does: v0.2.0 can't see them. Its reaper knows only `ci-tiny-*`, so the
   paid runs' runner rows stay, harmless.

The downgrade loses everything since that copy, `tiny` minutes included, and the loss grows with every
day after the deploy.
