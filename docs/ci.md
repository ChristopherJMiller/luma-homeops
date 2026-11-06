# Forge CI: ci-dispatch and the runner classes

`ci-dispatch` starts one fresh runner for each waiting Forgejo Actions job on `git.realliance.net`,
within each owner's limits. Argo app `ci-dispatch` (`cluster/ci-dispatch/`) runs it in namespace
`ci-dispatch`, and its runner Jobs in `ci-tiny`. Code: `git.realliance.net/realliance-ops/ci-dispatch`
(v0.1.0). Spec: `docs/superpowers/specs/2026-11-04-runner-classes-design.md`. Plan and ledger:
`docs/superpowers/plans/2026-11-04-runner-classes-phase-0-1.md` and `.ledger.md` beside it.

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
kubectl -n ci-dispatch wait --for=delete pod/ci-dispatch-0 --timeout=90s   # timed out: STOP
kubectl -n ci-tiny delete jobs --all       # each Job takes its Secret and pod
kubectl -n ci-tiny get jobs,pods,secrets   # repeat until it lists nothing: pods take up to 30 s
```

Wait for the pod before deleting: a launch in flight runs on for up to 20 s after the scale and can
still create a Job. If the `wait` times out, on an unreachable node say, the dispatcher may still be
running: stop there, and see node loss under Known limits. Delete every Job, not only the labelled
ones: a compromised dispatcher's Kubernetes credentials can create Jobs with any labels.

Then make it last in git: pause `tiny`, which passes the hooks. Only once that change is on `main`,
restore Argo: patch `applications` back to
`{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true,"allowEmpty":true}}}}`. It then
re-applies the policy of `ci-dispatch` from git, which scales the dispatcher back up. The runner rows
of the deleted Jobs can take no other job; the reaper removes them 10 min after the dispatcher starts
again.

## Classes and modes

| `runs-on` | Phase 1 |
|---|---|
| `tiny` | A pod in `ci-tiny`: 1 CPU, 2 GiB, `/work` a 1 GiB memory volume, 4096 tasks (`podPidsLimit` on every node, `docs/node-rebuild.md`). Runner timeout 15 min, Job deadline 20 min. Each launch reserves 20 of the owner's daily minutes |
| `small`, `medium`, `large`, each alone or with `docker` | No backend until phase 2. `config.Load` refuses any mode but `off` |
| Two size labels, `docker` alone, `[tiny, docker]` | Hold `label`. Forgejo ends the job after 24 h |
| `galaxy`, `general`, or any other label | Never listed: the dispatcher asks Forgejo only for jobs whose labels all fall within `tiny`, `small`, `medium`, `large` and `docker` |

| Mode | Effect |
|---|---|
| `off` | The class's jobs wait (`class-off`). Nothing launches |
| `dry-run` | Decides, logs each job once per change, counts `ci_dispatch_launches_total{outcome="dry-run"}`, may write holds. Launches nothing |
| `on` | Launches |

A class launches only when both switches allow it: its mode in `config.yaml` (Chris's switch, a
deploy) and `classes` in `ci.yaml` (Owner-signed, no deploy). Move a class from `dry-run` to `on` in
`config.yaml`, then watch the decision log for a day.

The reaper and the reconcile run in every mode, `dry-run` included. They act only on the dispatcher's
own name, `ci-tiny-<job>-<attempt>-<try>`: a Job or an ephemeral instance-scope runner with no active
launch for 10 min, and a Job past its `ci-dispatch/expires-at`. A drill's `ci-probe-*` runner, or a
member's repo or org runner, is never in scope. Even in `dry-run`, then, the dispatcher's token
deletes runners of that one shape.

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
| `paid_usd_per_period`, `paid_concurrent` (defaults, owners), `global.paid_usd_per_hour` | Paid classes, phase 2 | $100 a period, 20 at once, $0.60 an hour |
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
kubectl -n ci-dispatch logs ci-dispatch-0 | grep "\"key\":\"$KEY\""   # since the pod started
kubectl -n ci-tiny get jobs,pods -l ci-dispatch/key=$KEY               # gone once the Job ends
```

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
until curl -so /dev/null 127.0.0.1:18081/; do sleep 1; done   # any answer, even 401, means it listens
T=$(grep '^CI_DISPATCH_USAGE_TOKEN=' ~/Repos/realliance-sso-secrets/forge/secrets.env | cut -d= -f2-)
u() { printf 'Authorization: Bearer %s\n' "$T" | curl -fsS -H @- "http://127.0.0.1:18081/v1/owners/$1"; }
u '52/jobs?period=current&limit=200' | jq '.jobs[] | select(.job_id == 203)'
u '52/usage?period=current' | jq '{tiny, holds, source: .paid.limit_source}'
kill %%   # the port-forward
```

`period` is `current`, `previous` or `YYYY-MM`, in UTC calendar months. `limit` is 50 by default and
200 at most, newest first. Each launch try is a row, with a `state` (`ended`, `undone`, `reaped`, or
active) and an `outcome` (such as `finished`, `pickup-timeout`, `held` or `error: <step>`).

| Reason | Kind | Means |
|---|---|---|
| `class-off`, `owner-busy`, `class-full`, `not-ready` | wait | The class is off in `config.yaml` or `ci.yaml`; the owner's or the class's `tiny` slots are in use; the dispatcher hasn't read its stored config yet |
| `backoff` | wait | A recent launch was undone: Forgejo held the job (a concurrency group or `needs:`; no try, no charge), or a launch step failed (a try). Waits 1, 2, 5, then 10 min; from the sixth launch that Forgejo held, 60 min |
| `label`, `denied`, `owner-limit` | hold | A label combination no class takes; a denied owner; out of `tiny` minutes for the day |
| `invalid` | hold | The job's id, attempt or owner id is zero or less |
| `launch-failing` | hold | Three tries made no runner, and the key stays held. Fix the cause; the member's rerun gets a new key |

## Alerts

The nine rules in `cluster/ci-dispatch/dispatcher/prometheusrule.yaml` are all `severity: warning`.
`CIDispatchOrphanCompute` routes by name to `critical-alerts` (Discord, around the clock) since
0db39a77; the rest go to `homelab-default` (Discord, weekdays).

| Alert | Means | Do |
|---|---|---|
| `CIDispatchStale` | No loop with every read OK for 5 min, or no metric | `kubectl -n ci-dispatch get pod ci-dispatch-0`, its log, `ci_dispatch_errors_total` by `api`. A Forgejo or API outage never restarts the pod. See Known limits for node loss and the job listing |
| `CIDispatchLaunchFailing`, `reason: held` | A job holds as `launch-failing` | `kubectl -n ci-tiny get events`, the pod's `netpol-gate` log. Then the member reruns the job |
| `CIDispatchLaunchFailing`, `reason: errors` | Launch steps fail for 15 min | The `launch failed` log lines name the step |
| `CIDispatchConfigUntrusted` | The head of `realliance-ops/forge` `main` has not verified for 15 min, or Forgejo was unreachable at each reload | See "A head that fails" |
| `CIDispatchStoredConfigRejected` | At start, the ledger's copy of `ci.yaml` failed its proof. The common cause: its signer left the keyring (a rotated or expired key) | Once `ci_dispatch_config_verified` is 1, `kubectl -n ci-dispatch delete pod ci-dispatch-0` clears it. With no key rotated or expired, treat it as tampering |
| `CIDispatchConfigFindings` | For 30 min, an owner name in `ci.yaml` no longer matches its id, or looking up the names failed | Fix the name in `ci.yaml`. The limits still bind to the ids |
| `CIDispatchLaunchesUndone` | Over 7 undone launches an hour, for an hour | Jobs that Forgejo holds keep taking the slot: look for `held` rows in `jobs`. `for: 1h` is load-bearing, never shorten it |
| `CIDispatchOrphanCompute` | For 15 min, a `ci-tiny` Job or runner with no launch, or an expired Job still there | `ci_dispatch_orphans` by `kind`. A `tiny` orphan pod dies at its own 20 min deadline; delete a Job that outlives it by hand (`kubectl -n ci-tiny get jobs`). A `runner refused` log line is a `ci-tiny-*` runner that failed the uuid or scope check: treat it as an incident |
| `CITinyQuotaFull` | `ci-tiny` at its 3 pods for 30 min | `kubectl -n ci-tiny get pods`. With global `tiny_concurrent` 1, three pods means leftovers. A reaped pod may run out its 30 s grace while its replacement starts; the quota bounds that |

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
  `realliance-ops/ci-dispatch` waits until then.

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
4. Revoke the old token with the guarded delete below. STOP: ask Chris (a database write). Then run
   realliance/sso `tests/forge/test_forge_ci_dispatch.py`.

**The guarded token delete.** It runs on the `acid-forgejo` leader, as done for forge-ci's token on
2026-11-04 (`docs/superpowers/plans/2026-11-04-bots-to-realliance-ops.ledger.md`): one transaction
that raises unless it deleted exactly one row. `names` lists the account's token names, so run it
before, to pick `OLD`, and after. `docs/forgejo.md` uses the same block for `realliance-ci`. Never use
the API route `DELETE /api/v1/users/<account>/tokens/<name>`: the token routes take only password
basic authentication, which this forge turns off.

```bash
ACCT=ci-dispatch OLD=ci-dispatch   # the account, and the old token's name (the first is ci-dispatch)
L=$(kubectl -n forgejo get pod -l application=spilo,galaxy=acid-forgejo,spilo-role=master -o name)
names() { kubectl -n forgejo exec "$L" -c postgres -- psql -U postgres -d forgejo -Atc \
  "SELECT name FROM access_token WHERE uid = (SELECT id FROM \"user\" WHERE lower_name = '$ACCT')"; }
names
kubectl -n forgejo exec -i "$L" -c postgres -- psql -U postgres -d forgejo -v ON_ERROR_STOP=1 <<SQL
DO \$\$ DECLARE n int; BEGIN
  DELETE FROM access_token WHERE name = '$OLD'
    AND uid = (SELECT id FROM "user" WHERE lower_name = '$ACCT');
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 1 THEN RAISE EXCEPTION 'expected 1 row, deleted %', n; END IF;
END \$\$;
SQL
names
```

### The usage token (`CI_DISPATCH_USAGE_TOKEN`)

The bearer token of the usage API. Secret `ci-dispatch-usage`, key `usage-token`, source
`cluster/ci-dispatch/dispatcher/usage-token.secret.yaml`. It must be one printable line with no
whitespace, or the process refuses to start. The process reads it once, at start.

1. A new token in sso-secrets, checked by counts only:

   ```bash
   cd ~/Repos/realliance-sso-secrets
   openssl rand -hex 32 | { read -r t && [ -n "$t" ] && sed -i '/^CI_DISPATCH_USAGE_TOKEN=/d' forge/secrets.env \
     && printf 'CI_DISPATCH_USAGE_TOKEN=%s\n' "$t" >> forge/secrets.env; }
   grep '^CI_DISPATCH_USAGE_TOKEN=' forge/secrets.env | cut -d= -f2- | wc -l                         # 1
   grep '^CI_DISPATCH_USAGE_TOKEN=' forge/secrets.env | cut -d= -f2- | LC_ALL=C grep -c '[^[:graph:]]'  # 0
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
   yq '.data."config.yml" | @base64d | from_yaml | .server.connections | keys' $f   # names only
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

## Weekly check

The plan's D19. Until forge-bot's `ForgeUnexpectedAdmin` ships, compare three lists by hand each
week. A stolen dispatcher token's easiest backdoor is a new local administrator, or a runner that
receives other repos' jobs.

```bash
cd ~/Repos/realliance-sso && nix develop --command pytest tests/forge/test_forge_ci_dispatch.py -k site_admins
T=$(grep '^FORGEJO_ADMIN_TOKEN=' ~/Repos/realliance-sso-secrets/forge/secrets.env | cut -d= -f2-)
get() {  # every page of an admin listing: get '<path>?<query>'
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
kubectl delete namespace ci-probe   # S4: ask Chris
```

- Run one drill at a time. Its leftover sweep has no lock, so a second drill can delete the first
  one's runners.
- No real drill has used its 10 s release bound yet, so the first one may need it calibrated.
- The dispatcher's reaper never touches the drill's `ci-probe-*` runners.

## Known limits (accepted)

- **The job listing is one read, capped at 16 MiB.** Forgejo 15's
  `/api/v1/admin/actions/runners/jobs` does not page. Past about 80k waiting jobs that fit the five
  class labels, every tick fails and nothing launches for anyone (`CIDispatchStale`). Cancel the flood,
  or turn off Actions on its repo. Paging is on the fork's wish list.
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
  what is running.
- **`scripts/netpol-verify.sh stray`.** A `--since 24h` window reads `SHORT` for about a day after an
  agent's log rotates, which happens every two days or so. `ci-tiny` pods churn, and over a long
  window a stale mapping hides as `GONE` once two pods have held one IP. Use `--since 30m` around
  `tiny`.

## Rollback

- **Normal: pause `tiny`** (see Pausing `tiny`): `tiny: "off"` and its test expectation, in one
  signed commit.
- **A bad config or image:** revert the commit and push, which is a deploy. If the rollout stalled on
  a pod that never got `Ready`, delete `ci-dispatch-0` by hand once the revert has synced.
- **Remove it all:** `resources: []` in `cluster/ci-dispatch/kustomization.yaml`, with
  `scripts/test_ci_dispatch_manifests.py` updated in the same signed commit. On an empty render the
  hook `ci-dispatch-manifests` fails `test_renders_with_namespaces_set`, `test_namespaces_restricted`,
  `test_rbac_exact` and `test_no_plain_secrets`. Never `--no-verify` (S6). Argo prunes the rest but
  keeps the Namespace `ci-dispatch` and the PVC (`Prune=false,Delete=false`), so the ledger survives.
  Never delete `cluster/applications/ci-dispatch.yaml`: the app carries no `resources-finalizer`, so
  that orphans everything and leaves it running.
