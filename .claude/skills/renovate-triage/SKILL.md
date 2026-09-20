---
name: renovate-triage
description: Review a Renovate PR before it merges (or after it auto-merged and something broke). Use when asked "is this Renovate PR safe", when a dependency bump touches a component with delete/upgrade authority (external-dns-style controllers, cert-manager, Argo CD, MetalLB, Traefik, rook/ceph, terraform providers, kustomize refs), when adding a packageRules gate, or when investigating an outage that started at a merge time. Encodes the 2026-09-18 lesson: the PR title is not the version you get. NEVER merges a minor/major bump of a controller that can delete cluster or DNS state without reading the release notes for the version that actually ships.
---

# renovate-triage

`.renovaterc.json` automerges every non-major bump (`:automergeMinor`, `:automergeDigest`, `group:allNonMajor`, `:skipStatusChecks`). That is fine for 95% of dependencies and catastrophic for the other 5%. This skill is for the 5%.

## The lesson (2026-09-18, #2657)

Renovate PR: *"Update dependency kubernetes-sigs/external-dns to v0.23.0"*. Automerged. Five seconds after the pod restarted, every public CNAME in two zones was deleted.

What actually happened:
1. The ref bumped `v0.22.0 → v0.23.0`, but upstream's `kustomize/kustomization.yaml` pins the **image one tag behind the ref**. The binary went `v0.21 → v0.22`.
2. v0.22.0's release notes, first bullet, with a ⚠️: *default annotation prefix changed, no fallback*. All 47 ingresses used the old prefix → desired records = 0 → `--policy=sync` reconciled them away.
3. Upstream even added a tripwire (`--policy` became mandatory) — but the repo already set it explicitly, so it never fired.

Every step was visible in advance. Nobody looked, because it was "a minor bump".

## Triage procedure

**1. What does this component have authority over?**

| Authority | Examples here | Default posture |
|---|---|---|
| Deletes external state (DNS, cloud resources) | *(external-dns — retired)*, terraform providers | **Gate.** Human reads notes, plans before merge. |
| Deletes/replaces cluster state | Argo CD, rook operator, cert-manager, MetalLB, sealed-secrets, Traefik | **Gate minor+major.** Patch can automerge. |
| Data-path daemons with on-disk format | Ceph image, Postgres (acid-*), etcd (via Talos) | **Gate all.** Sequential, snapshot first, one node/replica at a time. |
| Stateless app images (arr stack, HA, grafana) | most of `cluster/` | Automerge fine; watch Argo health after. |

If it's in the top three rows and the rule isn't already in `.renovaterc.json`, add it (below) *before* the next PR arrives.

**2. Which version actually ships?** Don't trust the title.
- kustomize remote ref → open `kustomize/kustomization.yaml` at that tag and read `images[].newTag`. It may lag the ref.
- Helm chart → `appVersion` in `Chart.yaml` at that chart version; chart minors routinely carry app majors.
- Docker digest bumps → the tag didn't change, only the digest; safe unless it's a `latest`-style floating tag.

```bash
# kustomize ref: what image does it really pin?
curl -sfL https://raw.githubusercontent.com/<org>/<repo>/<ref>/kustomize/kustomization.yaml | grep -A1 'name:'
# helm: chart -> app version
nix-shell shell.nix --run 'helm show chart <repo>/<chart> --version <v> | grep appVersion'
```

**3. Read the release notes for every version between current and shipping.**

```bash
gh api repos/<org>/<repo>/releases/tags/<tag> --jq .body | grep -niE 'breaking|⚠|warning|deprecat|remov|default.*(chang|now)|migrat|no fallback'
```

Anything matching → decide explicitly: config change first (preferred), or `packageRules.allowedVersions` pin with a comment saying why and when to revisit.

**4. Does the repo's config survive the change?** Grep for every flag/annotation/CRD field the notes mention. The external-dns case: `grep -r external-dns.alpha` would have found 52 hits.

**5. Merge posture.** Gated components: merge during working hours, watch the rollout (`safe-rollout`), have the revert ready (`git revert <sha>` — Argo undoes it in ~1 min for stateless things; for DNS/state you also need the recovery procedure).

## Adding a gate

```json
{
  "matchPackageNames": ["<pkg>"],
  "groupName": "<pkg>",
  "description": "Own group so the stricter policy never blocks group:allNonMajor."
},
{
  "matchPackageNames": ["<pkg>"],
  "matchUpdateTypes": ["minor", "major"],
  "automerge": false,
  "description": "<why — cite the incident or the authority it has>"
}
```

The separate `groupName` matters: without it the non-automerging rule stalls the whole shared non-major PR. Keep the existing file's formatting (short arrays inline) — `jq` reflows everything and makes a noisy diff; edit by hand.

## Investigating "it broke around <time>"

```bash
git log --format='%h %cd %s' --date=iso-strict --since='<time> - 2h' --until='<time> + 10m'
kubectl -n <ns> get pods -o custom-columns='NAME:.metadata.name,STARTED:.status.startTime,IMAGE:.spec.containers[0].image'
kubectl -n <ns> logs <pod> | head -50     # the first minute after a restart is where controllers do damage
```

A pod start time within a minute of a Renovate merge is the answer until proven otherwise. Then step 2 above: what version is *actually* running vs what the title said.

## Related

- `safe-rollout` — watching the rollout after merging a gated bump.
- `cloudflare-dns` — terraform provider majors rename resource types; `plan` on the branch first.
- `talos-upgrade` — Talos/k8s are never Renovate-driven; that skill owns their sequence.
