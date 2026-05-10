---
name: cluster-snapshot
description: Produce a structured health snapshot of the galaxy cluster — nodes, ceph, argo apps, not-ready pods, recent events. Use this before any cluster change or when the user asks "how is the cluster" / "what's broken" / "give me a health check".
---

# cluster-snapshot

Run a curated set of read-only checks against galaxy and return a structured summary. Does not touch state.

## Refresh kubeconfig if needed

```bash
nix-shell shell.nix --run 'TALOSCONFIG=$PWD/nodes/talosconfig \
  talosctl -n 192.168.0.5 -e 192.168.0.5 kubeconfig --force /tmp/galaxy-kubeconfig'
export KUBECONFIG=/tmp/galaxy-kubeconfig
```

Skip the refresh if `kubectl get nodes` already works.

## Gather

Run these in parallel where possible (Bash tool, multiple in one block):

```bash
# nodes + versions
kubectl get nodes -o wide

# argo applications: which are OutOfSync or unhealthy
kubectl -n argo-cd get applications.argoproj.io -o wide

# pods that should not be in a non-Running state
kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded

# ceph state via rook toolbox
TOOLS=$(kubectl -n rook get pod -l app=rook-ceph-tools -o jsonpath='{.items[0].metadata.name}')
kubectl -n rook exec "$TOOLS" -- ceph -s
kubectl -n rook exec "$TOOLS" -- ceph health detail

# recent cluster events (last 10 min)
kubectl get events -A --sort-by=.lastTimestamp | tail -30

# resource pressure (if metrics-server up)
kubectl top nodes
kubectl top pods -A --sort-by=memory | head -15
```

## Report shape

Reply to the user with these sections, in this order, terse:

1. **Versions**: Talos / k8s / number of nodes / control-plane all schedulable?
2. **Ceph**: `HEALTH_OK` / `HEALTH_WARN` / `HEALTH_ERR` + the one-line reason. If not OK, name the affected PGs / OSDs.
3. **Argo**: count Synced+Healthy, list the OutOfSync, list the unhealthy.
4. **Workload pods**: count not-Running. List up to 5 with namespace, name, reason. Note any pod with > 100 restarts.
5. **Recent events**: anything Warning level in the last 10 min that isn't routine (FailedScheduling we already know about, image pulls).
6. **Top resource users**: only if something is notably high (> 70% of limit).
7. **Sharp edges**: still-known issues from CLAUDE.md "Known sharp edges" section that are unresolved.

If everything is clean, say so in one sentence. Don't pad.

## Don't

- Don't run any write command. No `kubectl apply`, `delete`, `scale`, `rollout restart`. This skill is read-only.
- Don't propose fixes inline — that's a separate conversation. Just report.
- Don't dump raw command output into the response. Summarize.
