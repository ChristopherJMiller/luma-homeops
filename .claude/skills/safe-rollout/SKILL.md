---
name: safe-rollout
description: Drive a Kubernetes Deployment/StatefulSet/DaemonSet rollout (or an Argo Application sync) and watch it to a definitive outcome — Ready, Failed, or Timeout — using the Monitor tool. Use whenever the user asks to deploy, restart, scale, or sync something that takes more than a few seconds, or whenever you've just merged a PR that changes a workload spec. NEVER busy-loops `kubectl get` in Bash. NEVER force-deletes pods to "speed it up". NEVER declares success without confirming all replicas Ready.
---

# safe-rollout

Watch a workload rollout (or Argo sync) to completion without polling-loop antipatterns. The point is to not declare victory before the cluster has actually settled.

## Hard rules (CLAUDE.md S3)

- **Use `Monitor`** for any rollout watch > 60s. Never `until kubectl get …; do sleep 5; done` in foreground.
- **Surface failure modes**, not just success. The watch must emit a line on Failed/Timeout, not just on Ready.
- **Patience**: image pulls on cold start, RBD volume attach on a node that wasn't running it, and PDB-blocked drains are all *normal slow*. Don't kill pods to escape waiting.
- **No `--force --grace-period=0`** unless explicitly authorized.
- **Don't restart pods that are already Running** to "see if it helps" — that's fishing, not diagnosis.

## Refresh kubeconfig if needed

```bash
nix-shell shell.nix --run 'TALOSCONFIG=$PWD/nodes/talosconfig \
  talosctl -n 192.168.0.5 -e 192.168.0.5 kubeconfig --force /tmp/galaxy-kubeconfig'
export KUBECONFIG=/tmp/galaxy-kubeconfig
```

## Pattern A — Deployment / StatefulSet / DaemonSet rollout

```bash
NS=argo-cd
KIND=deploy
NAME=argo-install-argocd-server

# Capture pre-rollout state
kubectl -n $NS get $KIND $NAME -o jsonpath='{.spec.replicas} desired, {.status.readyReplicas} ready' && echo

# Trigger (whatever the change is — restart, scale, image bump applied)
kubectl -n $NS rollout restart $KIND/$NAME

# Watch with Monitor — emits one line per event, fails out on rollout failure
# Use the Monitor tool with this command:
#   kubectl -n NS rollout status KIND/NAME --watch --timeout=15m
# Monitor will emit "deployment NAME rolled out" and exit when done,
# or "error: timed out waiting for the condition" on timeout.
```

For DaemonSets (per-node rollouts), expect 1 update per node × ~30s each. For 3-node DS, ~90s minimum.

## Pattern B — Argo Application sync

```bash
APP=argo-cd

# Inspect first
kubectl -n argo-cd get application $APP -o jsonpath='{.status.sync.status},{.status.health.status},{.status.operationState.phase}'

# Trigger sync (this is rare — Argo self-heal usually handles it)
nix-shell shell.nix --run 'argocd --grpc-web --core app sync '"$APP"

# Watch via Monitor — emit on every status change
# Monitor command (manual loop, since argocd CLI doesn't have --watch):
#   prev=""
#   while true; do
#     cur=$(kubectl -n argo-cd get application '$APP' \
#       -o jsonpath='{.status.sync.status}|{.status.health.status}|{.status.operationState.phase}')
#     [ "$cur" != "$prev" ] && echo "[$(date +%T)] $cur"
#     prev="$cur"
#     case "$cur" in
#       "Synced|Healthy|Succeeded"*) echo "DONE"; exit 0 ;;
#       *"Failed"*|*"Degraded"*) echo "FAILED"; exit 1 ;;
#     esac
#     sleep 10
#   done
```

## Pattern C — Node drain (e.g., before reboot)

```bash
NODE=middle

# Cordon first so nothing new schedules
kubectl cordon $NODE

# Drain — this can take 5-30 min if there are stateful workloads
# Use Monitor: emits a line per pod evicted; exits when complete or timeout
#   kubectl drain $NODE --ignore-daemonsets --delete-emptydir-data --timeout=30m
```

If the drain stalls on a specific pod, **inspect** before forcing:
```bash
kubectl get pod -A --field-selector spec.nodeName=$NODE,status.phase=Running
# For each blocking pod: is it owned by a workload that allows eviction (PDB)?
# Most homelab workloads here have PDBs configured (argo, repo-server, redis).
# If a PDB is blocking, that's by design — wait for the workload to schedule
# its replacement on a different node first.
```

## Verification gates (apply to all patterns)

After the Monitor exits successfully:

1. **Replica match**: `kubectl -n $NS get $KIND $NAME` shows `desired = ready = up-to-date`
2. **No pods crashing**: `kubectl -n $NS get pods` — all `Running`/`Completed`, no `CrashLoopBackOff`
3. **No recent failures**: `kubectl -n $NS get events --sort-by=.lastTimestamp | tail -10` — no Warning events on this workload
4. **Probes green**: for HTTP services, optionally `kubectl exec` curl the readiness path

If gate 1 says ready but gate 2 has a flapping pod, **the rollout is not actually safe** even though `rollout status` exited 0. Surface and pause.

## When to abort and surface

- Monitor's command times out (default 15min for `rollout status`)
- More than 3 restarts on any new pod within 5 min
- Argo Application enters `Degraded` / `OutOfSync` after the sync attempt
- Cluster events show `FailedScheduling`, `FailedAttachVolume`, `FailedMount` for the workload
- Ceph goes from HEALTH_OK to HEALTH_WARN/ERR during the rollout (volume operations are stressing storage)

Default response: stop, gather state, write up what you see, ask the user. Do not delete the failing pods to "let them retry".

## What this skill does NOT do

- Auto-rollback. If a rollout fails, surface and ask. The user owns rollback decisions.
- `kubectl delete pod --force` to "unstick" things.
- Edit Deployment specs to add/remove probes, resources, or labels mid-rollout.
- Anti-affinity gymnastics. Workload placement is configured in the manifests; if pods aren't scheduling, fix the manifest, don't override at runtime.
