# Runner classes phase 2: ledger

Plan: `2026-11-05-runner-classes-phase-2.md`. Controller's SDD notes live in `.superpowers/sdd/2026-11-05-runner-classes-phase-2/progress.md`. This file holds no secret, no Azure id and no hidden org, team or group name.

Phase 1 ledger cited below: `.superpowers/sdd/2026-11-04-runner-classes-phase-0-1/progress.md` (lines as of 2026-11-05).

## Answers

### Chris (2026-11-05)

- Phase 2 ships everyone at once (price feed, paid accounting, member access together). It starts in parallel with phase 1. Chris applies all terraform.
- Spot is unavailable on this subscription (phase 1 ledger line 16).
- Decided: `realliance` limit $30, other owners $10; the forge fork builds on `medium`; held undos back off to 60 min after 6.
- Plan approved: group (a) Tasks 0-9 now; group (b) after phase 1 Task 14; group (c) after phase 1 Task 18.
- F1: a paid held undo after compute existed charges `CostOf(compute_at -> stop)`, settles on absence, no try. `ErrNoCapacity` stays 0.
- F2-F39 adopt the scan's proposed ruling, first option where one is offered.
- Cache design: paid CI cap $80 per period (CI-group budget $85); release repo `realliance/forgejo` first; per-job egress cap in phase 2.
- Accepted risk: a compromised dispatcher churning groups shorter than one sweep evades the egress rule; only the budget bounds it. Task 20 records it as spec amendment (h).

### Chris (2026-11-06)

- I3 (Task 3 review): the dispatcher's hourly gate drops to $0.48 (480_000 µ$), 4 `small` or 2 `medium` at once. The sweeper's $0.60 line, compute plus egress, stays, so the worst-case arithmetic is unchanged and legitimate egress gets about $0.12 an hour of headroom. The plan carries it in E4, Values, Review Focus 1, Decided, and Tasks 4, 10, 12, 13, 14, 17 and 20 (amendment (i)).

### Carried phase 1 rulings (phase 1 `progress.md` line)

- 4: credit period is the UTC calendar month; ledger is `modernc.org/sqlite`.
- 16: Spot unavailable; VM classes pay-as-you-go only.
- 20: concurrency-held jobs hold and back off 1/2/5/10 min; paid VM classes revisit in phase 2.
- 24: bots live in the Owners-only `realliance-ops` org.
- 40: enforcement keys on numeric ids; names are labels.
- 41: store write equals dispatcher compromise; code ceilings bound spend.
- 45: reservation checks owner and global budgets; a 0 global means no paid.
- 54-55: `DeleteRunner(ctx, id, uuid)` reads first and refuses non-ours; guard checks the POST body.
- 61, 68-69: http.Client literals allowed in `guarded`; Job POST guard rules; fail closed on token faults.
- 81, 90-91: `Reserve` assigns Try; ledger caps and refuses negatives; `Tries` excludes held undos.
- 117-119: fair share by minutes used; paid-gate stub is replaced, never deleted (line 119).
- 144, 156, 158: step budget 20 s; the reaper name grammar covers `ci-tiny-` only, so phase 2 extends `ownName`.
- 154: Task 18 precondition N1 relaxed; Task 19 keeps the full gate.
- 174-178, 185: fix-round rulings (verify at now first, back-off after 6 held undos grows to 60 min).
- Carry (line 175): M6 settle on observed absence goes to phase 2.
- Carry (line 82): hold history grows unbounded; "revisit in phase 2" (see open items).

### Pre-flight rulings

F1 Chris; F2-F39 adopted. Table is in the plan under "Pre-flight rulings applied".

### Open items for Chris (F36), raised at Task 16's review

Two phase 1 carries address phase 2 and this plan does not claim them:

1. Phase 1 Task 12 fix round: "M8 -> Chris at Task 19/phase 2" (phase 1 ledger line 147).
2. The hold table's growth: soft-cleared rows are never pruned, "revisit in phase 2" (phase 1 ledger line 82).

### Open items for Chris, raised at Task 3's review (2026-11-06)

1. **Alternating sweeps evade the rate trip,** not only groups shorter than one sweep. A batch created just after sweep k−1 and deleted just before k+1 lives about 20 min and only sweep k sees it, so sweep k+1 clears the tag. The next batch repeats that, close to 100 % of the time, with no trip and no age delete. The fixed recurrence and the history reader make sweep times predictable to the second. Only the budget bounds it (8–48 h late).
2. **A possible partial fix, to spike:** the sweeper reads the CI group's Activity Log for container-group writes and deletes since the last slot, and counts compute and egress for groups that churned between sweeps. That needs metrics to stay queryable by id after a delete (unverified). A churned group would then count one sweep late, instead of never. No code yet.

## Baseline

Taken 2026-11-05.

- `az`: logged in. Inside `nix develop`, `az account show --query '{name:name,state:state}' -o tsv` prints `Realliance`, `Enabled` (Task 2, 2026-11-05). The earlier "not logged in" came from a shell without the flake, where `az` isn't on PATH.
- Phase 1 Task 13: complete.
- Phase 1 Task 14: at Step 2. Chris merges ci-dispatch PR #1 and tags `v0.1.0`. `git ls-remote --tags origin v0.1.0` on ci-dispatch printed nothing, so the tag does not exist yet.
- Phase 1 Task 18: not started.
- forge-bot Task 15: not complete. Its ledger has no Task 15 report, only carries addressed to it (add `ForgeBotOwnersUnknown` and `ForgeBotSelfCheckBlind` to the `forge-urgent` route). `forge-urgent` appears nowhere in `cluster/` on main, only in plans, so the route does not exist yet.
- NetworkPolicy plan: Task 6 (B1a) complete (`B1a_at=2026-11-05T21:21:17Z`). Task 7 (B1b) is running now; its 24 h soak follows, with Gate G excluding ci-dispatch and ci-tiny.
- Worktrees: `~/Repos/luma-homeops-ci2` (`ci-phase2`, from `origin/main` 31c25fcc) and `~/Repos/ci-dispatch-v2` (`v2`, from `origin/v1` 9055e60), both clean.

## Task 1

## Task 2

`azure/ci/`, the fence. Written and planned 2026-11-05; nothing applied (Chris applies in Task 4).

- **Step 1, aliases** (`az provider show -n Microsoft.ContainerInstance --expand resourceTypes/aliases`, 251 names on `containerGroups`). Each E7 rule:
  - present: `ipAddress`, `subnetIds`, `osType`, `restartPolicy`, `priority` (Spot), `volumes`, `imageRegistryCredentials`, `initContainers`, `containers[*]`, `containers[*].image`, `containers[*].resources.{requests,limits}.{cpu,memoryInGB,gpu}`, `sku`.
  - `identity`: no alias. The rule uses Policy's built-in field `identity.type` instead, so it stays.
  - Dropped: none (`dropped_rules: []`).
- **Step 1, registration:** `Microsoft.ContainerInstance` Registered, `Microsoft.Logic` Registered. The module declares no `azurerm_resource_provider_registration`, and a comment in `main.tf` records both.
- **Providers locked:** `hashicorp/azurerm` 4.81.0 (2026-07-14, the last 4.x; v5 has shipped), `hashicorp/azuread` 3.9.0, `hashicorp/time` 0.14.2. All are older than 2 weeks.
- **Plan's calls:**
  - `aci-shape` also denies a GPU request, a limit beyond the shape, and a SKU other than Standard. The rule bounds each number from both sides, so 2 and 2.0 compare the same.
  - The fence denies a missing `restartPolicy` (ACI defaults to `Always`).
  - Every definition is mode `Indexed`, so role assignments, budgets and the group's own tags stay outside the fence.
  - The dispatcher's assignment `depends_on` the Policy assignment.
  - The password is `create_before_destroy`.
- **Plan:** `Plan: 19 to add, 0 to change, 0 to destroy.` Addresses:
  - `azurerm_resource_group.{ci,guard}`
  - `azurerm_consumption_budget_resource_group.{ci,guard}`
  - `azurerm_policy_definition.fence["aci-image"|"aci-isolation"|"aci-shape"|"location"|"tags"|"types"]`
  - `azurerm_policy_set_definition.fence`, `azurerm_resource_group_policy_assignment.fence`
  - `azurerm_role_definition.dispatcher`, `azurerm_role_assignment.{dispatcher,dispatcher_cost_reader}`
  - `azuread_application.dispatcher`, `azuread_service_principal.dispatcher`, `azuread_application_password.dispatcher`, `time_rotating.dispatcher`
- **Tests:** `scripts/test_azure_ci.py`, 17 static tests pass. With `AZURE_CI_PLAN_JSON`, 20 pass and 1 skips (`test_sweeper_takes_dispatcher_assignment_id`, which Task 3 makes unconditional). 13 mutations each fail at least one test.

### Task 2 fix round 1 (review 0/2/6, 2026-11-05)

- **I1, plan text amended:** the plan's Task 2 (`test_dispatcher_assignment_named`) and Task 5 Step 5 now say the sweeper takes its id from the deterministic local `local.dispatcher_assignment_id`, not from the resource. A postcondition checks that Azure's id matches.
- **m1 (controller's call):** `aci-isolation` also denies `networkProfile.id`, `containerGroupProfile` and `standbyPoolProfile`, beyond E7. All three aliases exist (read 2026-11-05).
- **m2 (controller's call):** `image_like` is `ghcr.io/christopherjmiller/forgejo-runner-class@sha256:*`, the digest-only form the dispatcher sends (F16), with no tag and no other package. `containers[*].command` stays unfenced, as the spec accepts.
- **O2:** Renovate never automerges `azure/ci/**` (its own group, last rule). Don't merge PR #2620 (azurerm v5) during phase 2 without re-running the static and plan tests.
- **Plan:** still `Plan: 19 to add, 0 to change, 0 to destroy.`, with the same addresses.

### Task 2 fix round 2 (review 0/1/4, 2026-11-05)

- **N1, plan amended (controller's ruling T2-N1):** the class image is always `ghcr.io/christopherjmiller/forgejo-runner-class@sha256:<64>`, never `tag@digest`. The amendment touches the plan's F16 row, a new T2-N1 row, the Pins row, E7, Task 2's Interfaces block, Task 4 Steps 4 and 5 (the smoke uses the digest-only form, and Policy must deny a `tag@digest` negative), Task 13 (refuses a tag) and Task 18 (a digest-only regex). Task 7's client already refuses `tag@digest`.
- **n4, plan amended:** the Task 4 rollback is `./azure/ci/tf.sh plan -destroy -out=$SCRATCH/destroy.tfplan`, then Chris applies that saved plan.
- **n1 to n3:** `tf.sh` writes raw output only to a regular file, refuses `state pull|push` and `-chdir`, and keeps Lock Info's `ID:` line. The grant scan also bans template deployments, azapi Authorization and deployment types, `module` blocks, provisioners and `data "external"`.
- **Plan:** still `Plan: 19 to add, 0 to change, 0 to destroy.`, with the same addresses.

### Task 2 fix round 3 (review 0/2/4, 2026-11-05)

- **R1, plan amended (controller's ruling):**
  - Task 4 Step 4 spells out one compliant body: the digest-only image, `--cpu 2 --memory 8 --os-type Linux --restart-policy Never --tags forge-ci=1 expires-at=<…>`, `westus3`, and no IP, identity, registry credential or volume.
  - Every Step 5 negative is that body with exactly one change, and Step 5 ledgers each denying `policyDefinitionReferenceId` against the expected rule.
  - Steps 4 and 5 start 30 min after the apply, and Step 5 waits for a successful smoke.
- **R2, plan amended:** raw `tf.sh` forms need `--to FILE` or `--append FILE`, written by `tf.sh` at 0600. `tf.sh` refuses them without one, whatever stdout is (an agent's transcript can be a regular file). Task 4 Step 3's loop uses `--append forge/secrets.env`, and Task 2 Step 4 uses `--to $SCRATCH/plan.json`.
- **m-a to m-d:**
  - `tf.sh` refuses any `TF_CLI_ARGS*`, and also `TF_LOG*` (plan's call: a trace prints request bodies).
  - The static suite refuses override files, and fails on a duplicate block name.
  - The README uses `force-unlock -force`.
  - The plan's Task 2 text (the hook regex, the `tf.sh` description) is current.
- **Plan:** still `Plan: 19 to add, 0 to change, 0 to destroy.`, with the same addresses.

## Task 3

- **Carry from Task 2's review:** `azure/ci/tests/restore.tftest.hcl`'s restore run must assert that the sweeper's body (the `azapi_resource` workflow definition) is known, so a restore plan leaves the sweeper unchanged.
  - Done (2026-11-05): the restore run asserts that `jsonencode` of the whole body is known, that the lock's DELETE targets the deterministic id, and that the state is `Enabled`.

`azure/ci/sweeper.tf` and `sweeper/workflow.json.tftpl`, the sweeper and its lock. Written and planned 2026-11-05; nothing applied (Chris applies Tasks 2 and 3 together in Task 4).

- **Provider locked:** `Azure/azapi` 2.13.0 (released 2026-09-28, the newest 2.x), with `skip_provider_registration = true`.
- **Trips** (any one): burn over 600_000 µ$ an hour with the tag under 12 min old; the CI budget's `currentSpend` at $85 or more; the dispatcher's assignment already missing (a GET answers 404).
  - The trip DELETEs the assignment first, then every listed group whatever that DELETE did (`runAfter` Succeeded, Failed, TimedOut).
  - The run then ends `Failed`: code `Locked` when the DELETE answered 2xx or 404, else `LockFailed`.
- **Plan's calls:**
  - The third trip, "already locked": while locked the dispatcher can't delete its own groups, so every sweep deletes them all. That covers a group made during RBAC propagation.
  - `LockFailed` is a second terminal code, so a lock that didn't take never reads as `Locked`.
  - Any failed read, delete or tag write, or a list with a `nextLink`, ends the run `Failed`, code `Degraded`. The other rules still run first. A budget read failure (after 2030-12-31, say) never blocks the age kill.
  - Every HTTP action has `limit.timeout` PT1M and `inputs.retryPolicy` fixed, 1 retry after PT5S. DELETEs set `DisableAsyncPattern`. On a DELETE, 404 counts as done.
  - The tag holds `yyyy-MM-ddTHH:mm:ssZ` and is compared as a string, so a garbage tag can't make the trip expression throw.
  - The egress-total query starts 1 min before `createdTime`, so a brand-new group never asks for an empty span. Bytes = average × seconds since `createdTime` (E17).
  - The history reader's assignable scope is the guard group (a custom role can't take a single resource); its one assignment is on the workflow.
  - The ABAC condition stays in: the plan doesn't refuse it. If the apply does, drop `condition` and `condition_version` and ledger it here.
- **Azure check:** the Logic Apps `validate` endpoint (it creates nothing) accepts the rendered definition with placeholder ids. A body naming an undefined action is refused, so it checks references. The first try caught `retryPolicy` on the action instead of in `inputs`.
- **Plan:** `Plan: 26 to add, 0 to change, 0 to destroy.` Task 2's 19, plus:
  - `azapi_resource.sweeper`
  - `azurerm_role_definition.{sweeper,history_reader}`, `azurerm_role_assignment.{sweeper,history_reader}`
  - `azurerm_monitor_action_group.sweeper_failed`, `azurerm_monitor_metric_alert.sweeper_failed`
- **Tests:** static `196 passed, 4 skipped`; with `AZURE_CI_PLAN_JSON`, `200 passed`. The suite runs the template in a small Logic Apps simulator against a fake ARM. 20 of 20 mutations fail it, among them the assignment DELETE after the groups, any 4xx counted as locked, and the dropped timeout.

### Task 3 fix round 1 (review 0/3/8, 2026-11-06)

- **I1, design A (controller's ruling):** the tag `ci-sweeper-lock` holds the `createdOn` (seconds) of the assignment instance a lock aims at.
  - Pending while that instance or an older one exists, or none does: every sweep trips first and retries the DELETE, F7 order.
  - Restored only by a newer instance, which only Chris's apply can create; the sweeper then removes the tag.
  - An unreadable assignment is neither, and the run is `Degraded`.
  - The tag write runs after the DELETE, in parallel with the group deletes, and only when no lock tag exists or after a restore. No new grant; the tag joins `ignore_changes`, so the restore plan stays 1 to add, 0 to change.
- **I2:** the rate tag stores and compares the recurrence slot (`trigger().scheduledTime`, falling back to the trigger's start, then the clock). The metric windows and the age kill keep `utcNow()`.
- **I3 (Chris):** the plan amendments listed under "Chris (2026-11-06)".
- **M1:** the budget, already-locked and pending trips decide right after the reads and skip the egress and age work.
- **M2:** an assignment that reads 404 counts as `Locked`, and no DELETE goes.
- **M3:** the trip deletes the union of the ACI list and the RG list's container groups, minus those deleted this run.
- **M5:** `LockFailed` ends the run `Cancelled` (Azure refuses `runError` on Cancelled, so a `Lock_failed` step holds the detail). Three alerts: `ci-sweeper-failed` (RunsFailed: Degraded, and Locked runs), `ci-sweeper-lock-failed` (RunsCancelled), `ci-sweeper-locked` (Activity Log, the assignment's delete). Logic Apps exposes only two failing run statuses as metrics, hence the third channel.
- **M4, M6, M7, M8:** README notes (check the next sweep after unlocking; the egress term fails open; a budget lock holds for the month; the shape price ignores `gpu`, `sku` and `limits`).
- **Found while testing:** `Clear_lock` running after a failed rate-tag write masked that failure; `If_degraded` now checks `Over_ceiling` itself.
- **Azure check:** `validate` accepts the 60-action definition (placeholder ids). It refused `runError` on a Cancelled terminate, and accepted `trigger()?['scheduledTime']`, `Select` and `union`.
- **Plan:** `Plan: 28 to add, 0 to change, 0 to destroy.` The 26 before, plus `azurerm_monitor_metric_alert.sweeper_lock_failed` and `azurerm_monitor_activity_log_alert.sweeper_locked`.
- **Tests:** static `219 passed, 4 skipped`; with `AZURE_CI_PLAN_JSON`, `223 passed`. Against the pre-fix code the new suite fails 46 cases (22 tests). 34 of 34 mutations fail it, the review's five lock mutations among them.

## Task 4

- **Carry from Task 2 (review m3):** confirm a Policy deny before the dispatcher secret ships.
- **Carries from Task 3:**
  - The first sweeps after the apply may fail with 403 until the sweeper's assignment propagates: expect one `Degraded` email. Step 2's "two newest runs show `Succeeded`" comes after that.
  - F33: if the plan shows the workflow `1 to change`, match the template to the server's normalised form.
  - Step 4's egress check also confirms two runtime assumptions: an idle running group reports average 0, not "no data", and a group with no data yet answers with an empty `timeseries` or a point without `average`. Both feed average × seconds.
  - The apply may refuse the ABAC condition on the sweeper's assignment: drop it and ledger it.
- **Carries from Task 3's review (2026-11-06):**
  - I3: measure a real `small` job's transmitted bytes in the smoke (the total and the highest 10-min average). All groups together may send about 380 KB/s under the $0.48 gate before two sweeps lock.
  - M6: what a metric read returns for a group deleted a moment before. On a 404 the egress term fails open and the run is `Degraded`.
  - I2: the first runs confirm that `trigger().scheduledTime` is set on a Recurrence run; the tag `ci-sweeper-over-at` then reads a whole 10-min slot.

## Task 5

## Task 6

## Task 7

## Task 8

## Task 9

## Task 10

## Task 11

## Task 12

## Task 13

## Task 14

## Task 15

## Task 16

## Task 17

## Task 18

## Task 19

## Task 20

## Task 21

## Task 22
