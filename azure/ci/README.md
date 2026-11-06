# azure/ci: the paid-CI fence

Terraform for the Azure side of paid CI on Azure Container Instances (runner classes phase 2):

- two resource groups;
- both budgets;
- the `realliance-ci-fence` Policy initiative;
- the dispatcher's role and service principal;
- from Task 3, the sweeper.

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
- It refuses `state pull`, `state push`, `-chdir`, and any `TF_CLI_ARGS*` or `TF_LOG*` variable. Terraform adds `TF_CLI_ARGS*` to the command line after the script's checks, and a `TF_LOG` trace prints request bodies, secrets included.
- It leaves Lock Info's `ID:` line alone. That's the state lock id, not an Azure id. After a crashed run, `./azure/ci/tf.sh force-unlock -force <ID>` takes it; `-force` skips the prompt the filter would hide.
- The filter works by line, so a prompt doesn't show, and `destroy` or an unsaved `apply` would wait unseen. Always apply a saved plan.

## The budgets end on 2030-12-31

`realliance-ci` ($85) and `realliance-ci-guard` ($5) both end on 2030-12-31. After that date:

- the sweeper's budget trip has no `currentSpend` to read, so only the rate trip and the 90-minute age limit remain;
- nobody gets the 80 % and 100 % emails.

Before December 2030:

1. move `end_date` in both `time_period` blocks of `main.tf`, and the date in this heading;
2. plan.

If the plan replaces a budget instead of updating it, also set `start_date` to the current month first, because Azure refuses a start month in the past.
