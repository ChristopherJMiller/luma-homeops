---
name: talos-upgrade
description: Drive a single staged Talos OS or Kubernetes version bump on galaxy with safety gates. Use when the user wants to advance Talos or k8s by one minor version. Handles preflight checks (Ceph healthy, Argo apps healthy, etcd quorum room), per-node sequencing, and verification gates between steps. NEVER skips a minor version, NEVER upgrades multiple nodes concurrently, NEVER proceeds past a failed gate.
---

# talos-upgrade

Drive one step of the Talos+k8s upgrade plan (see issue #2506) safely. **One minor at a time. One node at a time. Verify between every step.**

## Hard rules

1. **No minor skipping.** Talos and k8s both forbid it. If user asks for a jump of >1 minor, refuse and ask for the intermediate.
2. **One node at a time** for Talos OS upgrades. `talosctl` will refuse if it would lose quorum, but we don't even try.
3. **Ceph must be HEALTH_OK** (or matching its prior known-baseline state) before any node touch. Use `ceph-doctor` skill to verify if unclear.
4. **No GitOps merges during the upgrade window.** Argo's self-heal can fight an in-progress upgrade.
5. **Stop on the first failed gate.** If a verification check fails, surface and ask — don't auto-proceed.

## Preflight (run every time, before any node touch)

```bash
nix-shell shell.nix --run 'TALOSCONFIG=$PWD/nodes/talosconfig \
  talosctl -n 192.168.0.5 -e 192.168.0.5 kubeconfig --force /tmp/galaxy-kubeconfig'
export KUBECONFIG=/tmp/galaxy-kubeconfig

TOOLS=$(kubectl -n rook get pod -l app=rook-ceph-tools -o jsonpath='{.items[0].metadata.name}')

# 1. cluster surface
kubectl get nodes -o wide

# 2. all argo apps Synced + Healthy
kubectl -n argo-cd get applications.argoproj.io -o wide | awk '$2 != "Synced" || $3 != "Healthy"'

# 3. nothing not-Running outside of known noise
kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded

# 4. ceph health
kubectl -n rook exec "$TOOLS" -- ceph -s
kubectl -n rook exec "$TOOLS" -- ceph health detail

# 5. etcd member health (all 3 should report Running/Healthy)
TALOSCONFIG=nodes/talosconfig talosctl -n 192.168.0.240,192.168.0.241,192.168.0.242 service etcd

# 6. take an etcd snapshot before any k8s upgrade (dedicated path)
mkdir -p ~/etcd-snapshots
TALOSCONFIG=nodes/talosconfig talosctl -n 192.168.0.5 etcd snapshot \
  ~/etcd-snapshots/galaxy-$(date +%Y%m%d-%H%M).db
ls -la ~/etcd-snapshots/ | tail -5  # confirm it landed
```

Snapshots accumulate; manually trim when there are >10 and you have a known-good cluster.

If anything above is not green, **stop**. Surface to user.

## Talos OS upgrade (one node)

For each node in order (`top` → `middle` → `bottom`):

```bash
NODE_IP=192.168.0.240   # top

# ⚠ ALWAYS the Image Factory installer with our schematic — NEVER
# ghcr.io/siderolabs/installer, which strips the amdgpu-firmware/amd-ucode
# extensions and breaks middle's GPU (see nodes/controlplane.yaml comment).
# The schematic ID is version-independent; only the tag changes per hop.
NEW_INSTALLER=factory.talos.dev/installer/173d42f096f07f5cc709f795178189dc64aa76ccad1a2193e5b3473f73f02a3e:v1.X.Y

# Reference check (fail fast on a bad tag; doesn't warm the system namespace cache)
TALOSCONFIG=nodes/talosconfig talosctl -n "$NODE_IP" image pull "$NEW_INSTALLER"

# Trigger the upgrade. Reboot is implicit (there is no --reboot flag);
# machine config is preserved by default (--preserve is deprecated since 1.13).
TALOSCONFIG=nodes/talosconfig talosctl upgrade \
  -n "$NODE_IP" \
  --image "$NEW_INSTALLER" \
  --wait
```

Talos will auto-cordon, drain, install, reboot, rejoin, uncordon. Refuses if quorum would break.

### Wait + verify, per node

Use `Monitor` (or background bash) — **don't** busy-loop:

1. **Node Ready**: `kubectl wait --for=condition=Ready node/<name> --timeout=10m`
2. **Talos version**: `talosctl -n $NODE_IP version` returns the new version
3. **Extensions survived**: `talosctl -n $NODE_IP get extensions` still lists amdgpu/amd-ucode (critical on `middle`)
4. **etcd member**: `talosctl -n $NODE_IP service etcd` is Running/Healthy
5. **Pods back**: `kubectl get pods -A -o wide --field-selector spec.nodeName=<name>` — count matches pre-upgrade, none Pending
6. **Ceph settles**: `ceph -s` returns to its prior baseline. For `middle` specifically, both osd.4 and osd.6 must be `up` and any PGs that were `degraded` during the reboot must be back to clean.

Only after all 6 pass: proceed to the next node.

If any of these fails for > 15 min, stop and surface.

## Kubernetes upgrade (cluster-wide)

After all 3 Talos nodes are on the new minor, you can do one k8s minor bump:

```bash
# Dry-run first
TALOSCONFIG=nodes/talosconfig talosctl -n 192.168.0.5 upgrade-k8s --to 1.X.0 --dry-run

# Real run
TALOSCONFIG=nodes/talosconfig talosctl -n 192.168.0.5 upgrade-k8s --to 1.X.0
```

This upgrades apiserver, controller-manager, scheduler, proxy, kubelet, coredns in sequence, with health checks between.

### Wait + verify (after k8s upgrade)

1. All nodes on the new k8s version: `kubectl get nodes` shows new version
2. All system pods Running: `kubectl -n kube-system get pods`
3. All Argo apps Synced + Healthy: `kubectl -n argo-cd get applications.argoproj.io -o wide`
4. Run a deprecated-API scan (next minor's removals). Tools: `kubent`, or k8s API server logs for `deprecated_apis` warnings
5. Wait 24h before the next minor — soak time catches latent breakage

## Hop-specific hazards (researched 2026-07, Talos 1.9→1.13)

- **1.8→1.9**: amdgpu moved from base image to extension; factory auto-migrates our schematic, but verify `middle`'s GPU (`get extensions` + dmesg) after the hop.
- **1.10→1.11**: **etcd 3.5→3.6 major bump** rides along. Mandatory fresh etcd snapshot before node 1; scrutinize etcd health between every node.
- **1.11→1.12**: kernel 6.12→6.18 (watch amdgpu + SATA controllers on middle), `module.sig_enforce=1`, stricter KSPP sysctls. `.machine.registries` (our docker.io pull-through mirror) deprecated — still works, migrate to `RegistryMirrorConfig` doc later.
- **1.13**: use the latest patch (≥1.13.3) — 1.13.2 had a k8s-1.36 scheduler-config rendering bug (siderolabs/talos#13350). Use a talosctl ≥1.13 client.
- **k8s must reach 1.31 before Talos 1.13** (its minimum). Full sequence: Talos 1.9 → 1.10 → k8s 1.31 → 1.32 → 1.33 → Talos 1.11 → k8s 1.34 → Talos 1.12 → 1.13 → k8s 1.35 → 1.36.
- **After every Talos hop**: bump the `install.image` tag in `nodes/controlplane.yaml` and `talosctl apply-config` (no reboot for that field) — `talosctl upgrade --image` alone does not persist.
- `upgrade-k8s` rewrites component image tags in machineconfig itself — no manual tag edits needed.

## Repo-side updates after upgrade

The machineConfig on each node will have been updated automatically (kubelet image, etc.). The repo's `nodes/controlplane.yaml` will drift. After each major milestone (e.g., end of Phase 1), refresh the repo:

```bash
TALOSCONFIG=nodes/talosconfig talosctl -n 192.168.0.240 get machineconfig -o yaml > nodes/controlplane.yaml.new
# diff, sanity-check, then replace and commit
```

Don't sweat per-step drift — let it accumulate and reconcile at phase boundaries.

## When to stop and surface

- Any node fails to come back Ready in 15 min
- Ceph goes to HEALTH_ERR (vs HEALTH_WARN) during/after an upgrade
- Any Argo app moves to `OutOfSync` or `Degraded` and doesn't self-heal in 10 min
- `talosctl upgrade` returns a quorum-loss refusal (means previous node didn't fully rejoin)
- Any disk on `middle` shows new ATA/SCSI errors in `talosctl dmesg`

Default response: pause, gather state, write up what you see, ask user how to proceed. Do not roll back unless explicitly told.

## What this skill does NOT do

- Multi-step orchestration across many minor versions. One step per invocation.
- Operator chart upgrades. Those are separate PRs that should land BEFORE the relevant k8s minor (per #2506 plan).
- Schema migrations or pre-removal API rewriting. Audit and fix in the repo first.
- Decisions about whether to upgrade. The user owns scheduling.
