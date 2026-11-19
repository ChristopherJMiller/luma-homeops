# Stale CI jobs: self-healing holds and member retry

Status: draft, 2026-11-17. Touches `realliance-ops/ci-dispatch` (v0.4.0 to v0.5.0), the Forgejo
fork `realliance/forgejo` (one new patch), `fj` (an overlay in `ChristopherJMiller/nixos-configs`)
and luma-homeops (alerts, runbook, Secrets, pins).

## Why

On 2026-11-17 Azure Container Instances in westus3 browned out from about 16:00 to 22:10 UTC.
Creating a container group normally answers in 1–2 s; in waves it took 20–40 s or failed with
`InternalServerError` and `GatewayTimeout`. Azure Service Health showed no event. Five conduit
jobs held as `launch-failing` around 22:00Z and were still held seven hours later: the game
`release` run 12, pak-tool's main push and its `v2026.11.1` tag, and platform PRs #24 and #25.
Nothing alerted on how long they had waited. The only way to restart them was the web UI's
Re-run, so the agent handling the incident could not.

The causes, all in ci-dispatch v0.4.0:

- Every 5xx, 429, timeout, failed token or lookup call, and launch left by a dispatcher restart
  counts as one of a key's three tries (`internal/loop/acibackend.go:348-360`,
  `internal/loop/reap.go:370-378`). With back-off at 1 and 2 min, an outage of about 4 min
  holds every paid job that tries during it.
- A `launch-failing` hold never expires. Releasing one means editing the money ledger by hand.
- A create gets at most 7.5 s (the step's Azure share, `acibackend.go:140,222`), below Azure's
  response time during the brownout. Azure cancelled the groups it finished after that, and their
  compute never reached the ledger.

The money cost is small: a held key reserves nothing and takes no slot. The real cost is stalled
work and people's time. The design aims at one outcome: no job waits silently, and anyone with
write access can restart a held job from wherever they are.

## Goals

1. An Azure outage never holds a job permanently. Queued paid jobs launch on their own once Azure
   recovers.
2. A job that waits over 30 min without a launch raises an alert.
3. A member with write access to a repo's Actions can release a held job by commenting `/retry`
   on the PR, through the API, with `fj`, or with a button on the held-job note. Runs that are not
   PRs (pushes, tags, `workflow_dispatch`) work too.
4. Automatic and manual retries have caps, so a broken job cannot spend without limit.
5. The ledger books compute for groups Azure created after the dispatcher gave up.

## Non-goals

- Rerunning finished (failed or cancelled) jobs through the API. The UI's Re-run stays the way to
  do that (decided 2026-11-17: held jobs only).
- A second Azure region or failover.
- Changing Forgejo's `ABANDONED_JOB_TIMEOUT` (24 h, swept every 6 h).
- ChatOps in forge-bot, which polls and is not deployed.

## Anti-goals

- A retry loop that spends money on a job that can never start.
- The dispatcher reachable from outside the cluster. It holds the Azure credentials and the ledger.
- The dispatcher deciding who may retry. Forgejo checks permissions; the dispatcher trusts only its
  token.
- Paid-class trouble delaying `tiny` jobs (`internal/loop/acifix_test.go:24-58` guards this today).

## Constraints

- The fork is a patch queue on upstream `v15.0.11` (`patches/series`). Fork CI refuses database
  migrations. Each patch ends with an `Upstream:` trailer. An Owner pushes the signed tag
  `v15.0.11-realliance.N`; Chris reviews and pins the digest in
  `cluster/applications/forgejo-release.yaml`.
- ci-dispatch runs as one StatefulSet replica on galaxy. Step budget 20 s, wedge limit 120 s,
  liveness 30 s times 3, SIGTERM grace 30 s. The ledger is SQLite with `schema_v1.sql` and
  `schema_v2.sql`.
- The usage API on `:8081` is read-only by design (`internal/usage/usage.go:1-15`). Forgejo reaches
  it at `http://ci-dispatch.ci-dispatch.svc.cluster.local:8081` with the bearer token in
  `[realliance.ci_usage]`.
- Local builds and tests use four threads (`taskset -c 0-3`, `GOMAXPROCS=4`, `-p=4`); kind and
  docker e2e stay in CI.
- Tags, image pins and deploys stop for Chris.
- Galaxy runs on two nodes until top's link returns (`docs/incidents/2026-11-13-switch-outage.md`).

## Approaches for the retry path

| | How a retry reaches the dispatcher | Trade-offs |
|---|---|---|
| A. Dispatcher only | The dispatcher polls PR comments for `/retry` and serves a public release endpoint that takes the member's own Forgejo token | No fork change. Puts the money component on the internet, makes it judge permissions, and widens its token to read and write comments |
| **B. Forgejo fronts it** (chosen) | A fork patch adds an API route, a button on the held note and a `/retry` comment handler. Each checks Actions write in Forgejo, then calls a new dispatcher control route over the link the held note already uses | The dispatcher stays in-cluster and Forgejo's own permissions and tokens apply. Covers PR and non-PR runs. One more patch to carry |
| C. Webhook receiver | A small service like `pages-relay` receives `issue_comment` and calls the dispatcher | Forgejo 15 never retries a failed delivery, so the receiver loses any comment sent while it restarts. Covers comments only. Another service to run |

Chris left the choice open ("whichever would fix this issue"). B fixes it: three of the five
stuck runs were not PRs, so a comment-only path would have left them stuck.

## Design

### 1. Azure failures are not tries (ci-dispatch)

Launch failures get classified. A new undo outcome (the builder names it; `unavailable` below)
covers ARM 5xx, 429, transport errors and timeouts, a failed token request, a failed GET before
the PUT, and a launch row left by a dispatcher restart. `qTries` excludes it the way it excludes
`held` and `locked` (`internal/ledger/ledger.go:124`), and `ledger/index_test.go` keeps passing.
4xx stays a try, except the capacity and lock refusals that are already not tries.

A key stops counting `unavailable` as free after 20 of them; later ones count as tries. The
breaker (2) makes reaching 20 take hours.

### 2. Paid-class breaker with one probe

After an `unavailable` failure the Azure backend opens. Every paid class waits as `not-ready` (a
log detail says Azure is unavailable) for 1, 2, 5, then 10 min, capped at 10. When the wait ends,
one launch goes through: the first paid job in the gate's fair-share order, the order every
launch uses (amended 2026-11-18; the spec first said the oldest waiting job, and a probe-only
ordering wasn't worth its own path in the gate). A successful create closes the breaker and the
queue launches as usual. Any other answer re-arms the wait, and an Azure-side failure also
lengthens it. Only a successful create closes it, because on 2026-11-17 reads kept working
while creates failed.

A gauge exposes the breaker state and a counter its transitions
(`ci_dispatch_azure_breaker_transitions_total{to="open|closed"}`). `CIDispatchPaidNotReady` already
fires after 30 min not ready.

### 3. Creates leave the step and get 60 s

The PUT, with its token request and GET, runs in its own goroutine. It gets a context detached from
the step (precedent `reap.go:482`), a 60 s bound, and an HTTP timeout to match. Its launch row gets
an in-flight mark, so the absence and restart checks do not undo it mid-PUT. The outcome comes back
to the loop under the step lock on a later tick. In-flight creates have a small cap (the builder
picks it; 2 is enough today).

If SIGTERM lands mid-PUT, the row stays registered. On restart the existing adoption
(`reap.go:172-188`) claims a group that appeared, and the orphan reaper deletes an unclaimed one
after 10 min.

An unclear answer, where the 60 s bound runs out or a 5xx comes back after the PUT went out, keeps
v0.4.0's rule from review I1 (`reap.go:466-486`): the dispatcher deletes the group and the runner
registration at once and never adopts the group, so a late group cannot take the job. The undo is
`unavailable` (1), not a try, and the late group's compute gets booked (7). Adopting late groups
would save cents and reverse a reviewed safety rule; the 60 s bound already covers a create that is
slow but succeeds. *(Builder check; Claude's call.)*

`tiny` launches are not delayed by a slow PUT; a test with a fake ARM that answers in 60 s proves it.

### 4. Broken groups fail fast; pickup timeouts back off

The reaper checks a paid group for a visible failure (state `Failed`, or an image pull or start
error in the instance view) before its runner registers, not only after (`reap.go:173`). Such a launch is
undone at once as an `error:` outcome, which is a try.

`pickup-timeout` reaps join the back-off that `error:` outcomes get (`ledger.go:125`), so three
cannot burn back to back.

### 5. One automatic second round

Once a `launch-failing` hold is 60 min old, the dispatcher releases it one time itself, through
the same path as a manual release (6), with the dispatcher as actor. That allows three more tries, then
the hold stays until someone retries.

Paid classes only. A `tiny` hold usually means a real fault such as the netpol gate, and an
automatic round would hold `tiny`'s single slot for about 9 min. *(Builder check; Claude's call.)*

### 6. Release in the dispatcher

A control route joins `:8081`: `POST /v1/holds/{job_id}/{attempt}/release` with a JSON body naming
the actor (Forgejo user id and name) and the channel (`api`, `button`, `comment` or `auto`). It
takes a bearer control token, a new Secret distinct from the usage token. The usage handlers stay
read-only; the control handler is its own package.

Ledger schema v3 adds a release record (key, time, actor, channel). `qTries` counts only tries
after a key's latest release. A release also clears the key's back-off.

Releasable: `launch-failing` and a key waiting in `backoff`. Not releasable, with the reason in the
response: budget holds (`no-budget`, `owner-limit`, `global-cap`), `label`, `denied`, `invalid` and
`concurrency`. Releasing a key that is not held answers success with its state, so retries are
idempotent. A key takes at most 3 manual releases a day; after that the route refuses with the
reason. Manual release covers every class, `tiny` included: a person chose to spend the slot, and the
daily cap bounds it. The usage API's job listing shows releases, so the held note and Usage tab can say who
released what.

### 7. The ledger books late groups

When the reaper deletes a group that belonged to an undone launch, the ledger books its compute
from the dispatcher's own created-at tag (set just before the PUT) to the first poll that finds the
group gone. That is within one 30 s poll of Azure's times and needs no new Azure read *(Claude's
call)*. With the booking, `DayCharged` (`internal/ledger/money.go:45`) counts it and the monthly
drift check stops seeing it as drift (`docs/ci.md`, the line on undo charges).

Realliance absorbs this compute (Chris, 2026-11-17). It counts in the global spend, `global-cap` and
the drift check, never in an owner's `PaidCommitted`, `owner-limit` or Usage tab, so a brownout
cannot push an org into a budget hold. Settled launch rows stay unchanged; the booking is its own
record that the per-owner reads skip.

### 8. Visibility

- A gauge for the oldest waiting job per class: the age of the oldest job Forgejo lists as waiting
  with no active launch, with the hold or wait reason as a label.
- A new alert, `CIDispatchJobWaiting`: a class's oldest job waiting for `launch-failing`,
  `backoff` or `not-ready` is over 30 min old (the maximum over 15 min, so a probe tick that
  briefly drops the job doesn't reset it) and the class has launched nothing in the last 30 min,
  for 5 min. Queueing behind limits (`owner-busy`, `class-full`) doesn't count: paid jobs run 40–55
  min, so a legitimate queue can wait that long. Reasons a person chose or a budget explains don't
  count either (`CIDispatchSpendHigh` covers budgets). *(Builder check and the final v0.5.0
  review, 2026-11-18; Claude's call.)* It routes to `critical-alerts` around the clock, next to the CI money
  alerts (Chris, 2026-11-17): add it to the 0db39a77 matcher and to
  `scripts/test-alertmanager-routes.sh`.
- `CIDispatchLaunchFailing{reason="held"}` gets `for: 5m`.
- `docs/ci.md`: the paid-class path for `launch-failing`, the breaker, the release channels and
  their caps, and the `curl` form of the API call.

### 9. Fork patch: release a held job

One patch, `Upstream: not-needed` (ci-dispatch is Realliance's own):

- `services/realliance/ciusage` gains a `Release` call with the control token, read from a new
  `CONTROL_TOKEN` key in `[realliance.ci_usage]`.
- API: `POST /api/v1/repos/{owner}/{repo}/actions/runs/{run_id}/release`. It needs a token with
  `write:repository` and Actions write on the repo, like the web Re-run route
  (`reqRepoActionsWriter`). It releases every held job in the run's latest attempt and returns each
  job's result.
- UI: a Retry button on the held note for users with Actions write.
- Comments: a notifier on new PR comments. A comment whose first line is `/retry`, from a user with
  Actions write on the base repo, releases the held jobs of the PR's runs at its head commit. The
  `ci-dispatch` account replies in one line: what it released, that nothing was held, or that the
  commenter lacks access.
- No migration. Integration tests cover the permission matrix with a fake ci-dispatch.

### 10. fj

An overlay in `ChristopherJMiller/nixos-configs` patches `fj` 0.6.0 with `fj actions retry <run>`,
which calls the route in 9. Others use `curl` (8) or `/retry`.

## Delivery order

1. ci-dispatch v0.5.0: parts 1–8 including the control route. This alone fixes the
   2026-11-17 failure.
2. luma-homeops: the control token Secret, alerts, runbook. Chris pins the image.
3. The fork patch, then an Owner's tag and Chris's pin.
4. The `fj` overlay.

Each stops before its tag, pin, or deploy.

## Testing

- ci-dispatch: a classification table test; breaker transitions; an async PUT against a slow fake
  ARM with no wedge and no `tiny` delay; a timed-out PUT that deletes its group and runner without
  using a try; release rules and caps; the
  v2 to v3 migration on a copy of a v2 ledger; booking of late groups; the new metrics.
- Fork: integration tests for reader (refused), writer (released), nothing held, and ci-dispatch
  unreachable.
- After deploy: release a real held job through each channel.

## Left to the builder

Outcome, metric and route-detail names; the in-flight create cap; the v3 schema's shape; whether
to add a per-job release route next to the per-run one; how to inject Azure faults for a drill
after deploy; the `fj` patch's internals.
