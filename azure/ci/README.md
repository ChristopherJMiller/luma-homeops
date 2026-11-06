# azure/ci: the paid-CI fence

Terraform for the Azure side of paid CI on Azure Container Instances (runner classes phase 2):

- two resource groups;
- both budgets;
- the `realliance-ci-fence` Policy initiative;
- the dispatcher's role and service principal;
- the sweeper and its lock.

Every number lives in `fence.json`, and `scripts/test_azure_ci.py` pins it. Spec: `docs/superpowers/specs/2026-11-04-runner-classes-design.md`.

Chris applies; agents stop at plan.

```sh
./azure/ci/tf.sh init
./azure/ci/tf.sh plan -no-color -out="$SCRATCH/ci.tfplan" | grep -E '^(Plan:|No changes|  # )'
./azure/ci/tf.sh apply "$SCRATCH/ci.tfplan"
```

Roll back with a saved destroy plan, read it, then apply that file:

```sh
./azure/ci/tf.sh plan -destroy -out="$SCRATCH/destroy.tfplan" | grep -E '^(Plan:|No changes|  # )'
./azure/ci/tf.sh apply "$SCRATCH/destroy.tfplan"
```

What `tf.sh` does to output:

- It swaps every UUID-shaped string for `<id>`, on both streams.
- `output -raw|-json` and `show -json` need an explicit destination as the first argument. `tf.sh` opens that file itself with mode 0600 (it tightens an existing one) and writes the raw output only there:

  ```sh
  ./azure/ci/tf.sh --to "$SCRATCH/plan.json" show -json "$SCRATCH/ci.tfplan"
  ./azure/ci/tf.sh --append forge/secrets.env output -raw dispatcher_client_secret
  ```

  Without `--to` or `--append` it refuses them, whatever stdout is: an agent's transcript can be a regular file. It also refuses a destination that is a link, a device, a `/proc` path, or its own stdout or stderr.

  That last check compares only `tf.sh`'s own stdout and stderr. Behind a pipe, the caller's transcript is another file and passes. You must never name a transcript or a capture file, or any file someone else reads, as the destination.
- It refuses `state pull`, `state push`, `-chdir`, and any `TF_CLI_ARGS*` or `TF_LOG*` variable. Terraform adds `TF_CLI_ARGS*` to the command line after the script's checks, and a `TF_LOG` trace prints request bodies, secrets included.
- It leaves Lock Info's `ID:` line alone. That's the state lock id, not an Azure id. After a crashed run, `./azure/ci/tf.sh force-unlock -force <ID>` takes it; `-force` skips the prompt the filter would hide.
- The filter works by line, so a prompt doesn't show, and `destroy` or an unsaved `apply` would wait unseen. Always apply a saved plan.

## The sweeper

`ci-sweeper` is a Logic App in the guard group (`sweeper.tf`, `sweeper/workflow.json.tftpl`). Every 10 minutes it:

- **locks first** when the CI budget's spend reaches $85, or a lock is already in place or pending;
- otherwise deletes any container group older than 90 minutes, and any at or over the 10 GB egress cap, and adds up the burn: each group's shape price plus its egress rate;
- **locks** when that burn is over $0.60 an hour for a second sweep in a row. The tag `ci-sweeper-over-at` holds the previous sweep's scheduled slot, so a slow or queued run still counts.

Locking deletes the dispatcher's role assignment first, then every container group. The tag `ci-sweeper-lock` remembers the lock by the deleted assignment's `createdOn`. Every sweep retries the delete until the assignment is gone, and keeps compute at zero after that. Only a newer assignment ends the lock, and only an apply can create one. The sweeper then removes the tag itself.

Each outcome has its own email to `alert_emails`. `Locked` and `LockFailed` are never masked; `Degraded` can hide under a lock (check the next sweep after a restore):

| Outcome | Run ends | Alert |
|---|---|---|
| `Locked`: the assignment is gone. Every sweep repeats this until an apply restores it. | `Failed`, code `Locked` | `ci-sweeper-locked`, from the CI group's Activity Log when anyone deletes the assignment |
| `LockFailed`: the assignment delete failed. The groups went anyway, and every sweep retries. | `Cancelled`; the `Lock_failed` step says why | `ci-sweeper-lock-failed` (runs cancelled) |
| `Degraded`: a read, delete, or tag write failed, or a list had a second page. The other rules still ran. | `Failed`, code `Degraded` | `ci-sweeper-failed` (runs failed, which `Locked` runs also raise) |

To unlock:

1. Find the cause first: the run's error, and the dispatcher's log.
2. Plan: it reads `1 to add, 0 to change`, the assignment under its fixed name. Chris applies.
3. Then check that the next sweep ends `Succeeded`. If it ends `Locked` with no cause, a sweep that read the old state raced the apply: apply again.

A budget lock holds until the month rolls over or Chris raises the budget: while `currentSpend` stays at $85 or more, the next sweep locks the restored assignment too.

Notes:

- **The dispatcher's own gate is $0.48 an hour** (4 `small` or 2 `medium` jobs; Chris, 2026-11-06). The sweeper's $0.60 counts compute plus egress, which leaves about $0.12 an hour, about 380 KB/s across all groups, for traffic before two sweeps lock.
- The egress term fails open. An unread metric (a 404 for a group deleted mid-sweep, say) adds nothing to the burn and skips the cap, and the run ends `Degraded`. Task 4 checks what a metric read returns for a deleted group.
- The shape price reads only CPU and memory, and ignores `gpu`, `sku` and `limits`. The `aci-shape` Policy denies all three today; widen the price if that rule ever changes.
- The first sweeps after the first apply can fail with 403 until the sweeper's role assignment propagates. Expect one `Degraded` email then.

## The budgets end on 2030-12-31

`realliance-ci` ($85) and `realliance-ci-guard` ($5) both end on 2030-12-31. After that date:

- the sweeper's budget trip has no `currentSpend` to read, so only the rate trip and the 90-minute age limit remain, and every sweep ends `Degraded`;
- nobody gets the 80 % and 100 % emails.

Before December 2030:

1. move `end_date` in both `time_period` blocks of `main.tf`, and the date in this heading;
2. plan.

If the plan replaces a budget instead of updating it, also set `start_date` to the current month first, because Azure refuses a start month in the past.
