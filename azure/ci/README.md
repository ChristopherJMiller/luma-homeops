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

`tf.sh` swaps every UUID-shaped string for `<id>`, except on the stdout of `output -raw|-json` and `show -json`. Pipe those, never print them.

## The budgets end on 2030-12-31

`realliance-ci` ($85) and `realliance-ci-guard` ($5) both end on 2030-12-31. After that date:

- the sweeper's budget trip has no `currentSpend` to read, so only the rate trip and the 90-minute age limit remain;
- nobody gets the 80 % and 100 % emails.

Before December 2030:

1. move `end_date` in both `time_period` blocks of `main.tf`, and the date in this heading;
2. plan.

If the plan replaces a budget instead of updating it, also set `start_date` to the current month first, because Azure refuses a start month in the past.
