# CLAUDE.md — luma-homeops operator guide

This is `galaxy`, a 3-node Talos Kubernetes cluster behind a VyOS router. You and the user (Chris) are co-head cloud engineers for it. This file is your operating manual: how the system is shaped, what is fragile, and the safety rules that override everything else.

When this guide and a tool/agent default conflict, **this guide wins**.

---

## Topology

| Piece | Where | Notes |
|-------|-------|-------|
| Router | VyOS 1.5-rolling, `192.168.0.1` | SSH `chris@192.168.0.1`, `~/.ssh/id_ed25519` (must be in agent — `ssh-add` if needed). Op-mode commands need `/opt/vyatta/bin/vyatta-op-cmd-wrapper`. Config-as-code in `router/ansible/`. |
| Control-plane VIP | `192.168.0.5:6443` | Talos KubePrism + shared VIP across all 3 nodes |
| Nodes | `top` .240, `middle` .241, `bottom` .242 | All control-plane, all schedulable. Talos v1.7.6, k8s v1.30.3. |
| Ingress LB | `192.168.0.6` | MetalLB pool `default` is `192.168.0.6-.8` (3 IPs — tight, for public ingress) |
| LAN-internal LB | `192.168.0.230-.239` | MetalLB pool `lan-internal` (autoAssign=false). For cluster services exposed to LAN but not WAN — currently CephNFS at `.230`. |
| WAN | `eth0`, `152.44.247.88/25` | Port-forward 80/443 → .6 (ingress) |
| Storage | Rook-Ceph (RBD + CephFS) | OSDs across all 3 nodes, `rook-ceph-block` is default SC, reclaim=Retain. CephNFS export at `192.168.0.230` for satellites. |
| GitOps | Argo CD app-of-apps | Root: `cluster/applications/applications.yaml`. Self-heal + prune ON. |
| Talos | `nodes/talosconfig`, `nodes/controlplane.yaml` | Encrypted at rest via git-crypt (see "Secrets") |
| Secrets | git-crypt + Sealed Secrets + agenix | `*.secret.yaml` + `nodes/*` + `satellites/.keys/*` are git-crypt encrypted (`.gitattributes`). For runtime cluster secrets, edit `*.secret.yaml`, run `./sign.sh` → SealedSecret for Argo. For satellite secrets, agenix (recipients in `satellites/secrets/recipients.nix`). |
| Satellites | NixOS edge devices at `192.168.0.243-.254` ("upper orbit") | Pull-based GitOps via comin from `satellites/`. NOT Argo-managed. See `satellites/README.md` and the Satellites section below. |

`shell.nix` provides the toolchain. Run anything that needs `talosctl`/`helm`/`argocd` through `nix-shell shell.nix --run '…'`. There is no flake — `nix develop` will not work.

---

## Safety rules (read before every cluster touch)

These exist because cluster ops often *look* fast but aren't. Volume detach, node drain, Ceph repair, image pulls on a saturated link — any of these can take minutes or hours, and impatient retries cause cascading damage.

### S1. Read the current state before changing it
Before *any* write to the cluster or router, read what is there now:
- `kubectl get -n NS …` / `kubectl describe …`
- `talosctl -n NODE get …`
- VyOS `show configuration commands` before any commit
Don't assume the repo matches reality — Argo can be OutOfSync, manual edits happen, and Renovate PRs land.

### S2. GitOps is the source of truth — write to the repo, not the cluster
For anything Argo-managed (almost everything in `cluster/`), make changes by editing files and committing. Do **not** `kubectl apply` / `helm upgrade` directly to live state — Argo will fight you on the next reconcile and the diff vanishes.

Exceptions where direct `kubectl` is correct: ad-hoc inspection, `kubectl rollout restart` of a Deployment (not its spec), `kubectl delete pod` to force a restart, scaling a Deployment temporarily during incident response (then revert via repo).

### S3. Patience over panic on slow ops
The cluster operates at a scale where some operations *are slow*. Do not retry, force-delete, or escalate to destructive commands just because a thing is taking time.

| Op | Typical duration | What to do |
|----|------------------|------------|
| RBD volume detach on node failure | 6 min default (`controller-publish-readonly-on-fs-resize` timeout) | Wait. Use `Monitor` to watch, don't loop sleep. |
| PVC deletion when pod still attached | Indefinite — pod must terminate first | Find the pod, terminate it cleanly. Don't `--force --grace-period=0` unless you know the consequences. |
| Node drain with stateful workloads | 5–30 min | Watch `kubectl get pods -A -o wide --field-selector spec.nodeName=NODE`. Don't `--force`. |
| Ceph PG repair | minutes to hours | Let it run. `ceph -s` to watch. |
| Talos node upgrade | 5–15 min including reboot | One node at a time. Wait for `Ready` + all PGs `active+clean` before next. |
| Image pull on cold start | minutes | Don't kill the pod. |

**Default to `Monitor`** for any op > 60s. Never busy-loop a `kubectl get` in Bash.

### S4. Confirm before destructive actions
Always pause and ask before:
- `kubectl delete` of: PVC, PV, Namespace, StatefulSet, anything in `kube-system`/`rook`/`argo-cd`
- `helm uninstall`, `argocd app delete`, `kubectl delete -k`
- Any `talosctl reset`, `apply-config --mode=reboot`, `upgrade`, `bootstrap`
- VyOS `commit` of changes that touch firewall, NAT, or interfaces
- `git push --force`, `git reset --hard` on a branch with uncommitted intent
- Scaling a StatefulSet to 0 (data path may not survive)
- Editing `nodes/controlplane.yaml` (machine config — wrong field can brick a node)

The "are you sure" prompt is cheap. A wedged cluster is not.

### S5. Stagger control-plane changes
This cluster has 3 control-plane nodes and **no separate worker pool** — every node is also etcd + API server. Lose 2 at once and the cluster is read-only. So:
- Talos upgrades: one node, full health check, then next.
- Reboots: one at a time.
- Drains: one at a time, and check Ceph regains `HEALTH_OK` (or returns to its prior state) between each.
- Argo full app-of-apps re-sync: avoid during any of the above.

### S6. Never bypass commit hooks or skip signing
The repo has `.pre-commit-config.yaml` and `sign.sh`. Never `--no-verify`, `--no-gpg-sign`, or skip pre-commit unless Chris explicitly says so. If a hook fails, fix the cause.

### S7. Ceph: look, don't poke
The cluster's data is on Ceph. Read commands (`ceph -s`, `ceph health detail`, `ceph osd tree`, `ceph pg dump`) are always safe. Repair commands (`ceph pg repair`, `ceph osd out`, `ceph osd down`, `ceph osd destroy`) can move or lose data — confirm with Chris first, even if the doctor agrees with the diagnosis.

### S8. VyOS: use commit-confirm for risky changes
For any router config change that touches firewall, NAT, interfaces, or DHCP, deploy with `commit-confirm 10` (auto-rollback after 10 min) so a mistake that locks you out heals itself. The router README documents this — follow it.

### S9. Don't paste secrets, don't echo them
Files matching `*.secret.yaml` and everything in `nodes/` are git-crypt encrypted at rest, but appear cleartext to you locally because the key is unlocked. Treat their contents as sensitive: don't echo them in chat, in PR descriptions, in tool outputs you forward elsewhere, or in commit messages. When in doubt, redact.

The deploy flow for runtime secrets:
1. Edit `cluster/<app>/foo.secret.yaml` (raw `kind: Secret` — this is the cleartext source-of-truth, encrypted by git-crypt on commit)
2. Run `./sign.sh` (or `./sign-all.sh` to re-seal everything — the latter requires confirmation)
3. Commit both the `.secret.yaml` (encrypted) and the generated `.yaml` (SealedSecret, plaintext but only the controller's private key can open it)
4. Argo applies the SealedSecret; the in-cluster controller writes the real Secret.

---

## Satellites (NixOS edge devices)

`satellites/` is a separate subsystem from `cluster/`. Where Argo CD pushes from this repo into the cluster, **comin pulls from this repo onto each satellite** every ~60s. Same GitOps mindset, opposite direction.

Each Pi (or other SBC) runs a `nixosConfigurations.<hostname>` from `satellites/flake.nix`, selected by its `networking.hostName`. SD image is built once on the dev machine via aarch64 binfmt emulation; after first boot the device is hands-off. Per-device SSH host keys live in `satellites/.keys/` (git-crypt encrypted); agenix uses them to decrypt per-device secrets.

Hardware support comes from `nvmd/nixos-raspberrypi` (Pi Zero 2 W, Pi 3/3B, Pi 4, Pi 5). The older `nix-community/raspberry-pi-nix` does NOT support BCM2837 — do not switch back to it without checking.

### Satellite safety rules

**SAT1. Pull, never push.** comin owns satellite filesystems. Don't `ssh ... nixos-rebuild` to a satellite — your change will be overwritten on next poll. Edit `satellites/hosts/<h>/`, sign-tag, push.

**SAT2. Sign your commits.** Devices verify against `services.comin.gpgPublicKeyPaths` baked into the image. Unsigned tags do not deploy.

**SAT3. Touch `satellites/modules/base.nix` carefully.** A bad networking change there can leave a device unable to phone home — only remedy is re-flash. Treat it like `nodes/controlplane.yaml`.

**SAT4. Don't widen `lan-internal` MetalLB without shrinking DHCP first.** The range `192.168.0.230-.239` is carved out of what was DHCP space. If you grow it down, edit `router/ansible/group_vars/vyos_routers.yml` and apply ansible BEFORE the MetalLB change syncs.

**SAT5. Verify rollback before you need it.** Pin the comin branch to a known-good prior tag, confirm the device reverts. Test once on `octoprint`; trust it on the rest.

### Quick commands (satellites)

```bash
# Build an SD image for a satellite (requires aarch64 binfmt on dev machine)
nix build path:./satellites#sdImage-<hostname>

# Scaffold a new host
./satellites/scripts/new-host.sh <hostname> <board>

# Inspect a satellite over SSH (read-only ops — don't modify state)
ssh admin@192.168.0.243 systemctl status comin
ssh admin@192.168.0.243 nixos-rebuild list-generations
```

---

## Known sharp edges (current state, 2026-07)

These are real problems in the current cluster — flag, don't silently fix:

1. **Ceph on SMR HDDs**: OSDs are ST8000DM004 (SMR) with BlueStore data+DB+WAL all on the spinners — chronic `BLUESTORE_SLOW_OP_ALERT` HEALTH_WARN noise is expected and non-blocking unless it escalates to ERR or PGs leave active+clean. Postgres fsync pain mitigated in software 2026-07 (#2587: `synchronous_commit=off` + patroni `failsafe_mode`). Real fix needs added flash per node (the NVMe is the Talos system disk + etcd — not usable). Buy CMR drives for replacements.
2. **`ingress-nginx` is past EOL** (maintenance ended March 2026). Migration target: **Traefik on the Ingress API** (#2507 — full design + 26-ingress inventory done 2026-07). Side-by-side prep on MetalLB `.7` can proceed; final WAN cutover via router NAT flip after early #2506 hops.
3. **Talos v1.8.4 / k8s v1.30.3**: k8s is >1 year past EOL. Hop sequence (no skips, one node at a time, Ceph healthy between): Talos 1.9 → 1.10 → k8s 1.31 → 1.32 → 1.33 → Talos 1.11 (etcd 3.5→3.6!) → k8s 1.34 → Talos 1.12 → 1.13 → k8s 1.35 → 1.36. See #2506 + the talos-upgrade skill (corrected 2026-07: factory installer image, not ghcr).
4. **authentik 2024.12.3, chart pinned `2024.x.x`** — Renovate never offers 2025.x+. Upgrade path is strictly sequential (8 hops to 2026.5); per-hop pg_dump is non-negotiable (Django migrations don't roll back). No automated backup of acid-auth exists (`enableLogicalBackup` unset).
5. **kube-prometheus-stack 79.12.0** — one stepwise hop to ~84.x remains (#2515).

---

## Working agreements

- **Be opinionated.** Chris wants design proposals, not yes-man answers. Disagree when warranted.
- **Plan, then execute.** For any multi-step cluster change (upgrade, migration, refactor), produce a written plan first — what changes, in what order, what to watch, what triggers rollback. Get sign-off, then execute.
- **Use `Monitor` for long-running ops** (rollouts, drains, ceph operations). Don't poll-loop in Bash.
- **Use `nix-shell shell.nix --run '…'`** for anything needing talosctl/helm/argocd. Long-form commands in a single quoted heredoc; export `KUBECONFIG=/tmp/galaxy-kubeconfig` when using a refreshed kubeconfig.
- **Refresh kubeconfig from Talos** when the in-tree one rejects credentials: `talosctl --talosconfig nodes/talosconfig -n 192.168.0.5 -e 192.168.0.5 kubeconfig --force /tmp/galaxy-kubeconfig`.
- **For repo changes that affect runtime**: open a branch, edit, commit, push, let Argo (or Renovate) pick it up. Don't merge during a known-bad cluster state (Ceph degraded, ongoing upgrade, etc.).
- **Surface findings, don't auto-remediate.** If you discover something broken (Ceph error, OOMing pod), report and propose. Don't quietly run repair commands.
- **Ask when blast radius is unclear.** Cheap to ask, expensive to undo.

---

## Quick commands

```bash
# Refresh kubeconfig from Talos (when admin@galaxy auth is rejected)
nix-shell shell.nix --run 'talosctl --talosconfig nodes/talosconfig \
  -n 192.168.0.5 -e 192.168.0.5 kubeconfig --force /tmp/galaxy-kubeconfig'
export KUBECONFIG=/tmp/galaxy-kubeconfig

# Cluster health snapshot
kubectl get nodes -o wide
kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded
kubectl -n rook get cephcluster rook-cluster -o jsonpath='{.status.ceph}' | jq

# Argo
nix-shell shell.nix --run 'kubectl -n argo-cd get applications.argoproj.io -o wide'

# Talos node info
nix-shell shell.nix --run 'TALOSCONFIG=$PWD/nodes/talosconfig \
  talosctl -n 192.168.0.240 dmesg --tail | tail -50'

# VyOS op-mode (note the wrapper)
ssh chris@192.168.0.1 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show interfaces'
```
