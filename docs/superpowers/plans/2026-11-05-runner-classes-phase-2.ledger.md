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

### Task 3 review round 2 (0/0/3, 2026-11-06)

- **N1:** a simulator case for an assignment answering 200 without `createdOn` while a lock is pending: `Degraded`, no DELETE, the tag kept. It fails with `If_degraded`'s `created == ''` check removed.
- **N2:** the plan's "Limits" line now reads as the dispatcher's $0.48 ceiling (compute plus egress) beside the sweeper's $0.60 line, with no separate sub-bullet. Task 21's rollback trigger is the sweeper's $0.60/h line, measured on `ci_dispatch_burn_usd_per_hour`.
- **N3:** the README says `Locked` and `LockFailed` are never masked and `Degraded` can hide under a lock; the gate note drops "of compute".
- **Plan:** still `Plan: 28 to add, 0 to change, 0 to destroy.` Tests: static `220 passed, 4 skipped`; with `AZURE_CI_PLAN_JSON`, `224 passed`.

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

- **Step 1:** Chris authorised the apply of the saved plan (2026-11-06). Apply complete: 28 added, 0 changed, 0 destroyed. `apply_done` 2026-11-06T03:17:23Z. The ABAC condition on the sweeper's assignment was accepted (nothing dropped).
- **Step 2, re-plan (F33):** `No changes. Your infrastructure matches the configuration.` The server kept the workflow definition as sent, so the template needs no edit and there's no re-apply.
- **Step 2, providers:** `Microsoft.ContainerInstance`, `Logic`, `Insights`, `CostManagement`, `Consumption`, `Authorization` and `Resources` all `Registered`.
- **Step 2, API versions:** the pins all exist as GA. Newer GA versions exist for some types, and none were taken. The sweeper is applied with the pins and validated, so moving one is a template change and a re-apply, which is a controller call:
  - `containerGroups`: 2026-07-01, 2025-09-01 (pin 2023-05-01);
  - Insights `metrics`: 2024-02-01 (pin 2023-10-01);
  - CostManagement `Query`: 2026-08-01, 2026-06-01, 2025-03-01 (pin 2023-11-01);
  - Consumption `Budgets`: 2026-06-01 (pin 2023-05-01);
  - Resources `resourceGroups`: 2026-06-01, and `tags`: 2023-07-01 (pin 2021-04-01);
  - Logic `workflows` 2019-05-01 and `roleAssignments` 2022-04-01 are the newest GA.
- **Step 2, role assignments** on `rg-realliance-ci` (`roleDefinitionName` only): `Cost Management Reader`, `Realliance CI dispatcher`, `Realliance CI sweeper`, and nothing else.
- **Step 2, sweeper runs:** the first run (slot 03:14:58Z, during the apply) already ended `Succeeded`, code none, and every read action answered OK. The 403 window the carry expected never showed, and there was no `Degraded` run or email. Next run (slot 03:24:58Z): `Succeeded`. First `Succeeded`: 2026-11-06T03:15:02Z.
- **Runtime facts (Task 3 carries):**
  - (a) `trigger().scheduledTime` is set: **yes.** The run's trigger object has `code`, `correlation`, `endTime`, `name`, `originHistoryName`, `scheduledTime`, `startTime` and `status`. A Recurrence trigger has no outputs (no `outputsLink`). The `Slot` action's output equals `scheduledTime` truncated to seconds, and the slots fall exactly 10 min apart.
  - (b) The dispatcher assignment GET (`2022-04-01`, by its fixed name) returns `properties.createdOn`: **yes**, also `updatedOn`, `createdBy` and `updatedBy`.
  - (c) `RunsCancelled` is in the workflow's metric definitions: **yes,** with `RunsStarted`, `RunsCompleted`, `RunsSucceeded` and `RunsFailed`. A cancelled run counting there is Task 5's drill.
  - (d) The Activity Log alert `ci-sweeper-locked` is enabled, `global`, scoped to the CI group (`/subscriptions/<id>/resourceGroups/rg-realliance-ci`). Its condition is `category = Administrative`, `operationName = Microsoft.Authorization/roleAssignments/delete`, `status = Succeeded`. An RG-scope assignment delete is an Administrative event in that group, so the scope fits; **the live delete is Task 5's drill.**
  - `nextLink` spelling: the Logic Apps run-actions list pages at 30 actions and carries `nextLink`, lower camel case.
- **Step 3, credentials:** the loop stopped on no error (each `tf.sh` exit was 0). Count-only checks before the commit: `grep -cE '^CI_DISPATCH_AZURE_[A-Z_]+=$'` printed `0`; each of the four keys counted once; the three ids are GUID-shaped (count 3) and the secret is longer than 30 characters with no space. The committed blob is git-crypt. Signed commit `0a9acda` in `realliance/sso-secrets` (`forge: ci-dispatch's Azure credentials (CI_DISPATCH_AZURE_*)`), pushed to `main`.
  - Client secret created 2026-11-06 (03:09Z). **Expires 2027-05-05**, creation + 180 d. **Rotation STOP falls due 2027-04-05**, creation + 150 d, `time_rotating`'s `rotation_rfc3339`. The first apply on or after that date replaces it (F26).
- **Step 4, the smoke** (2026-11-06, as Chris's identity, from 03:48Z, over 30 min after the apply). Five `small` groups, all with the compliant body, differing only in name and command:
  - **Form (ruling, 2026-11-06):** `az container create` has no `--tags` in az 2.89.1, and the fence denies a group without both tags. So every create was `az rest --method put` at `2023-05-01`, `{subscriptionId}` URL form, with the dispatcher's wire body in a 0600 file. The token went as a `secureValue` only, and each command line through a file. The plan's Task 4 Step 4 and the Step 5 table now say so.
  - **The fence passes the compliant body:** every PUT answered 201 `Pending`. No capacity refusal and no `ServiceUnavailable` anywhere.
  - **Timings,** PUT → `Running` (container `startTime`) → `Terminated` (`finishTime`):

    | Group | Command | → Running | → Terminated | Exit |
    |---|---|---|---|---|
    | 1 | `ci-class-entry 35m small smokehandle <forge> <zero uuid>` | 52 s (pull 32 s) | 58 s | 1, `unauthenticated: unregistered runner` |
    | 2 | `df -h / /run; nproc; free -g; curl github.com; sleep 150` | 37 s (pull 20 s) | 190 s | 0 |
    | 3 | `curl` 100 MB from speed.cloudflare.com | under 60 s | under 60 s | 22 (the endpoint answers 403, from here too) |
    | 4 | `curl` both Go 1.26.9 tarballs, 101_097_347 bytes in 2.3 s | 64 s (pull 47 s) | 69 s | 0 |
    | 5 | `sleep 120`, the same two tarballs, `sleep 60` | 42 s | deleted at 03:59:36Z during its `sleep 60` tail | n/a |

    Create → `Running` stays at or under 64 s, under `aci.PullAllowance` (5 min), so Task 13 keeps 5 min (F32). The image is pulled cold each time, in 20 to 47 s. Group 1 shows the entry refuses a bad token at once: a bad registration costs about a minute, not the 35 min timeout.
  - **Group 2's output:** `/` and `/run` are one `overlay`, 50G in size with **47G free**. `nproc` is 2. The image has no `free` (no procps), so `free -g` printed `not found`.
  - **Nonce:** group 1's `az container show` and `az container logs` hold no match (`grep -F -f`), and nor does any PUT answer, list, GET or log. `RUNNER_TOKEN` comes back with neither `value` nor `secureValue`.
  - **Image digests:** the pin `sha256:fa7be53e…` (`sha-4fb0c3b`) is an OCI image index. ACI pulls its linux/amd64 child `sha256:6bf028e1eb672e81b8c788f80a2e4a77e947ae0ee614f96e7bdaeb6c23071ff4`, and only the `Pulling`/`Pulled` events name it. The list and the GET both return `image` exactly as sent, the index digest. The index's other child is the attestation manifest.
  - **The ACI list** (`2023-05-01`, saved 0600): **no `instanceView`**, neither the group's `state` nor the container's `currentState`, `exitCode` or `finishTime`. `2025-09-01` lacks it too. `provisioningState` reads `Succeeded` for exited groups, and there's no `nextLink` at 4 groups. So F16's fallback holds: `Groups()` GETs each own group. A group GET carries `instanceView.state` (`Failed` for exit 1, `Succeeded` for exit 0) and `currentState` with `state`, `exitCode`, `startTime` and `finishTime`. The Task 7 fixture `internal/azure/testdata/aci-list.json` (4 groups, every GUID zeroed, count-only check 0) is copied into `ci-dispatch-v2`, uncommitted. Zeroed raw GETs of a failed and a completed group stay in the controller's scratch.
  - **Egress metric (E17):**
    - `list-definitions` lists `NetworkBytesTransmittedPerSecond`, and `interval=FULL` is accepted. FULL returns one point whose grain is the span floored to whole minutes, aligned to the start minute: 2, 10, 11, 23, 37 and 95-minute spans each gave one point. A span that isn't whole minutes adds a second, partial bucket.
    - Before a group has data, the answer is one timeseries whose points have no `average`.
    - **The metric is blind for about the first 90 s of a container's life.** Group 2's `curl` at start-up never showed: its first points were 03:51 and 03:52, about 90 s after its 03:49:32 start. Groups 1, 3 and 4 (each alive under 70 s; group 4 received 101 MB) never reported a single point. Points that do arrive land within about 90 s of their minute.
    - **Idle reads a number, not "no data":** once group 2's points began, its idle minutes read transmitted 2 and 1 B/s and received 0.
    - **Deleted group:** a metric read straight after the delete answers **404 `ResourceNotFound`**. The sweeper's egress term fails open (`Degraded`), as the README says. Metrics are not queryable by id after a delete, which undercuts the Activity Log spike in "Open items" (item 2).
    - **I3, egress bytes:**
      - group 2 (`curl` github.com): **unmeasured**, inside the blind window; its measurable idle tail was about 1–2 B/s;
      - group 4 (101 MB in 2.3 s, alive 5 s): **0**, never reported;
      - group 5 (the same 101 MB at 03:57:49–52Z, 120 s after start): **0 visible.** Received reads 0 (average and maximum) in the 03:57 and 03:58 minutes, and transmitted reads 2 then 0 B/s.
      - So **the metric misses a short burst even 2 minutes into a container's life.** It behaves like a sampled rate, not a byte counter. The ACK overhead of a 100 MB download can't be measured with it, and average × duration undercounts any bursty transfer. A sustained, rate-limited transfer may register; that's untested, because another group would have taken the smoke past its $0.02.
  - **Cost:** about 10 group-minutes of `small`, from pull start to finish or delete, about $0.02.
  - **State lag:** group 5's GET still read `Running` about 40 s after its last command should have ended (seen once).
  - Every smoke group deleted by name; `az container list -g rg-realliance-ci --query "length(@)"` reached `0` at 03:59:38Z, and `az resource list -g rg-realliance-ci` counts 0 too.
- **Step 5, Policy negatives** (each the compliant body with exactly one change, PUT the same way, fresh `ci-neg-<hex>` name). All answered `RequestDisallowedByPolicy` with exactly one `policyDefinitionReferenceId`, the expected one; none was created:

  | Change | Denying `policyDefinitionReferenceId` |
  |---|---|
  | `requests` 1 vCPU / 2 GiB | `aci-shape` |
  | `image: busybox` | `aci-image` |
  | the class image as `tag@digest` (`:sha-4fb0c3b@sha256:…`) | `aci-image` |
  | `ipAddress` Public, port 80 | `aci-isolation` |
  | `identity` SystemAssigned | `aci-isolation` |
  | `location: westus2` | `location` |
  | tags without `forge-ci` | `tags` |
  | `restartPolicy: Always` (n-b) | `aci-isolation` |
  | a free NSG, `westus3`, both tags (n-b, `types`-only) | `types` |

  This meets Task 2's carry, a Policy deny confirmed, though the dispatcher's secret reached sso-secrets earlier the same morning (Step 3, before the smoke window opened). It hasn't been resealed or deployed.
- **Step 6, region:** no capacity refusal. `westus3` stays.
- **Retail prices, for Task 6's M6** (public reads, 2026-11-06): the pinned filter answers 4 items on one page, with no `NextPageLink`. At `2023-01-01-preview` a page holds up to 1000 items; Container Instances across all regions is 871, still one page. A filter with more items (Virtual Machines in `westus3`) pages, and the live link reads `https://prices.azure.com:443/api/retail/prices?api-version=2023-01-01-preview&$filter=<%20- and %27-encoded filter>&$skip=1000`: host with `:443`, a literal `$`, and `$skip` in steps of 1000. Two live pages are saved in the controller's scratch, with ids zeroed (count-only check 0).
- **Rulings (controller, 2026-11-06):**
  - The smoke and the negatives use `az rest --method put` with the dispatcher's wire body, in a 0600 file, because `az container create` has no `--tags`. The plan's Task 4 Step 4 and the Step 5 table now say so, with the two n-b rows.
  - **Carry → Tasks 13 and 14:** the pin is an OCI index. ACI pulls its linux/amd64 child, and only the pull events name the child digest; the list and the GET return the image exactly as sent. The guard and the sweeper compare the image as sent, so match on the sent string and never on an event's digest.
  - **Carry → Task 3's README (Minor):** `interval=FULL` floors the grain to whole minutes from the start minute. A span that isn't whole minutes gets a second, partial bucket, and the sweeper reads `data[0]` only. For a group 1–2 min old, that first bucket is the empty pre-create minute, so it reads 0; past that, the newest partial minute is left out.
- **Concerns for the controller:**
  - **E17's egress term, as designed, doesn't measure egress.** `NetworkBytes*PerSecond` is blind for about the first 90 s of a container's life. It also missed a 101 MB, 2.3 s burst two minutes in, both received and transmitted.
    - The sweeper's 10 GB cap and egress rate, and the dispatcher's planned egress pass, all read average × time, so they can undercount by any factor. Only the budget bounds egress today.
    - That widens "Open items" item 1, and item 2's Activity Log spike can't help, because metrics 404 after a delete.
    - Before Task 13 builds on E17, it needs a decision: test a sustained, rate-limited upload (cheap, in Task 5's drill), and if the metric still misses it, drop the egress term or replace it.
  - **The secret reached sso-secrets before the first Policy deny** (Step 3 ran during the propagation wait, as the brief's order put it). It isn't resealed or deployed, and the denies are now confirmed.
  - **Newer GA API versions exist** (Step 2 list); the pins stay unless the controller rules otherwise.

## Task 5

- **Step 1:** Chris pre-approved Step 1's whole list (2026-11-06): groups as his identity in the CI group (about 2.5 h, about $0.65), the sweeper removing the dispatcher's assignment, `TestLiveLocked` while locked, and his restore apply. The controller added one measurement, a sustained upload for E17 (Task 4 concern 1).
- **Form:** every group was `az rest --method put` at `2023-05-01` with the compliant body (Task 4's ruling), a fresh nonce as the only `secureValue`. No PUT answer held the nonce.
- **Step 2, age kill:** `ci-drill-age-2713b1`, `small`, `["sleep","7200"]`, `expires-at` 3 h out. ARM `createdTime` 04:29:27Z. The 06:05:05Z sweep's `Delete_old` ran 06:05:09.8–06:05:11.4Z, and the group read gone at 06:05:32Z: **95.7 min** after creation (expected 90–102). Every sweep from 04:35Z to 05:55Z ended `Succeeded`, and none set `ci-sweeper-over-at`.
- **Egress upload (E17, Task 4 concern 1):** `ci-egress-0808fb`, `small`, alongside Step 2. The container started 04:30:07Z, slept 120 s, then sent `/dev/zero` to `https://speed.cloudflare.com/__up`: 30 POSTs of 10_000_000 bytes, `curl --limit-rate 1M`, every answer 200. **300_000_000 bytes, 04:32:07–04:36:53Z.** It exited 0 at 04:41:54Z; the drill deleted it at 04:43:06Z.
  - `NetworkBytesTransmittedPerSecond` **counts a sustained upload.** At PT1M the five full minutes read 1_053_6xx–1_054_6xx B/s, the curl rate (1 MiB/s) plus 0.5 %.
  - Ratios against the bytes sent:

    | Reading | Bytes | Ratio |
    |---|---|---|
    | Σ(PT1M `average` × 60) | 318_583_140 | 1.06 |
    | Σ(every sample) × 30 s | 294_504_660 | 0.98 |
    | FULL over 04:28–04:43Z, `average` × 900 s | 384_136_513 | 1.28 |
    | the sweeper's form: FULL from `createdTime` − 1 min, `data[0].average` × (now − `createdTime`) | 397_684_626 | 1.33 |

  - **The metric is a sampled rate, about 2 samples a minute** (PT1M `count` 1–3). FULL's `average` is the mean of the samples, not time-weighted, and the blind start (about 90 s, no samples) isn't in it. The sweeper's average × age thus reads high for a young group, the safe direction. A sample is a short-window rate, which also explains Task 4's missed 2.3 s burst: unbiased over many samples, noisy for one burst.
  - **Latency:** minute stamps run about 60–65 s behind the wall clock (the 04:32 minute, with 53 s of upload, read 1 B/s; the last bytes landed in the 04:38 point), and a point publishes within about a minute after that. The first non-zero point was readable at most 120 s after the upload began. A sweep's 10-min rate window misses roughly the newest 2 min.
  - **Verdict:** far over 1 %, so the metric supports the 10 GB cap and the egress rate for sustained traffic.
  - Received over the same 15 min averaged 1_758 B/s (ACKs).
- **Step 3, rate trip:**
  - five `medium` groups `ci-drill-rate-b7d818-{1..5}` (20 cores, $1.165 an hour) PUT 06:05:59–06:06:11Z, each 201 `Pending`. `StandardCores` in `westus3` read 0 of a limit of **20** before.
  - **Azure refused the sixth, `small`: HTTP 400 Bad Request, code `ContainerGroupQuotaReached`**, the only code in the body; the message names `StandardCores`. Not created.
  - The 06:15:05Z sweep, the first over the ceiling, ran `Mark_over` (`OK`) and set `ci-sweeper-over-at`; the run ended `Succeeded`.
  - **The 06:25:06Z sweep locked**, 19.1 min after the last create (expected within 22). F7's order held: `Delete_dispatcher_assignment` 06:25:14.88–06:25:17.12Z (`OK`); then `Delete_all_groups` from 06:25:17.21Z, its five `Delete_group` repetitions starting 06:25:17.35Z or later, all `OK`; `Write_lock_tag` `OK`. The run ended 06:25:19Z.
  - **The run reads `status` `Failed`, `code` `Terminated`, `error.code` `Locked`.** The README's "code `Locked`" is the run's `error.code`.
  - The 06:35:06Z sweep ended the same way, as designed while locked.
  - The failed-run email is Chris's to confirm.
- **Step 4, the lock:**
  - Role names on `rg-realliance-ci`: `Cost Management Reader` and `Realliance CI sweeper`, no `Realliance CI dispatcher`.
  - Tag names on the CI group: `ci-sweeper-lock` present (name only), and `ci-sweeper-over-at` still set.
  - The newest succeeded run (06:15:05Z) has `Read_budget` `Succeeded` (`OK`), as has the locking run.
  - **Alerts,** by state through an id-free Alerts Management query: `ci-sweeper-failed` (RunsFailed) **Fired** 06:29:00Z; `ci-sweeper-locked` (Activity Log, Administrative) **Fired** 06:31:25Z, about 6 min after the delete, which is runtime fact (d) live; `ci-sweeper-lock-failed` (RunsCancelled) did not fire. The `Lock_result` action reads `Cancelled` because `Terminate` ends it, but the run doesn't count as cancelled. The emails are Chris's to confirm.
  - **`TestLiveLocked` failed:** `Groups = nil (code "") after 30m0s: not locked` (started 06:35:45Z). A fresh process at 07:11:48Z, with a new token, read the same after 5 min.
  - Diagnosis, read-only: the service principal's only assignments left are `Cost Management Reader` on the CI group and `Realliance CI sweeper history reader` on the workflow. It's in no group, and neither role holds `containerGroups/read`.
  - A temporary probe test (untracked, deleted after) as the service principal at 07:17Z: `Groups` nil with 0 groups; `Group("ci-small-0-0-2")` **`ErrLocked`, code `AuthorizationFailed`**; `SweeperRuns` nil. At 07:18Z, the same GET first and then `CreateGroup("ci-small-0-0-2", …)`: **`ErrLocked`, code `AuthorizationFailed`**, stopped at its GET-first, so no PUT went out. A list as Chris afterwards: 0 groups.
  - **The lock locks**: single-resource reads and creates answer 403 `AuthorizationFailed`, which confirms `TestErrorClasses`'s 403 row. But **a locked collection list answers 200 with an empty `value`**: ARM filters the list to what the caller may read. `Groups()` can't see a lock, and `TestLiveLocked`'s gate never opens.
  - A `python -I` script wrote the credential files from sso-secrets, printing byte counts only; `rm -r` removed them straight after each run.
- **Step 5:** the restore plan, grep form: `# azurerm_role_assignment.dispatcher will be created`, `Plan: 1 to add, 0 to change, 0 to destroy.` The workflow shows no change (F6). Saved 0600 in the controller's scratch; Chris applies.
- **Step 5, restore:** Chris applied the saved plan (1 added, 0 changed, 0 destroyed) after re-checking the plan line. The last locked sweep was 07:15:07Z (`Failed`, `Terminated`/`Locked`). **The first sweep after the apply, 07:25:07Z, ended `Succeeded`.** `Read_dispatcher_assignment` returned `OK`, `Delete_over_tag` returned `OK` and `Delete_lock_tag` returned `OK`, and both `ci-sweeper-lock` and `ci-sweeper-over-at` are gone (tag names only). Role names on `rg-realliance-ci` are back to exactly `Cost Management Reader`, `Realliance CI dispatcher` and `Realliance CI sweeper`, with 0 groups. At 07:26Z `ci-sweeper-failed` still read Fired: the metric alert resolves once its 15-min window passes the last locked run. `ci-sweeper-locked` is an Activity Log alert, so it stays `New` until someone closes it. This meets the done-when: a drill with an over-ceiling resource trips the sweeper and locks.
- **Cost, estimated** from group-minutes at list price: age 95.7 min `small`, $0.19; egress 12.4 min `small`, $0.02, plus 0.3 GB, about $0.03; rate trip 5 × 19.2 min `medium`, $0.37. **About $0.61.** Cost Management's figure for 2026-11-06 comes the next day (F25, for Task 22).
- **Rulings (controller, 2026-11-06):**
  - The quota refusal is **HTTP 400 `ContainerGroupQuotaReached` (`StandardCores`), not 409**. Treat it as definitely not created, and as a quota class distinct from capacity. **Carry → Task 7 Step 6 and Task 13** (`TestErrorClasses`, E11). Today the table has the code at 409 only, and a bare 400 is `ErrRejected`.
  - FULL's `average` is per sample, and the sweeper's average × age reads high for a young group. **Carry → Task 20's README.** Task 3's files stay as they are.
- **Carries:**
  - **Task 7:** `TestLiveLocked` must gate on a single-group GET of a fixed absent name (404 open, 403 locked), not on `Groups`.
  - **Task 14 and Task 18:** a locked dispatcher's `Groups()` returns an empty list, not `ErrLocked`. Lock detection (`CIDispatchLocked`, `TestACILockedWaitsNotReady`) needs a GET probe or the 403 from a create. Settle-on-absence must not read a locked empty list as "all groups gone": after a manual assignment delete, groups run until the next sweep, up to 10 min.
  - **Task 20's README:** a locked run's code is `error.code` `Locked` under `code` `Terminated`.

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

## Task 9: money-alert route (2026-11-06)

- Ruling (Chris): no new receiver; no email receiver exists. Money alerts route by name, any severity, to the existing `critical-alerts` (Discord, 24/7, repeat 30m): `CIDispatch(SpendHigh|Locked|OrphanCompute|Drift|PaidNotReady)|CISweeperStale`, placed before the weekday-only `homelab-default` route.
- Gate G-lite before: `prometheus-stack-config` Synced/Healthy, Alertmanager pod 2/2, 0 restarts.
- Pushed 2026-11-06T04:29Z as `0db39a77`. Argo Synced/Healthy at that commit; pod 2/2 Running, 0 restarts; live `/api/v2/status` routing tree shows the new route to `critical-alerts`.
- Tests: `scripts/test-alertmanager-routes.sh` (14 rows, all ok); `amtool` added to the dev shell (flake.nix).
- Still open: Step 5 drill (Chris, outside weekday hours): `amtool alert add alertname=CIDispatchLocked severity=critical drill=true --end=+10m`.
- Note: the python rewrite of the secret dropped its YAML comments.
