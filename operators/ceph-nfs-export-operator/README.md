# ceph-nfs-export-operator

A small kopf reconciler that turns `CephNFSExport` custom resources into
`ceph nfs export apply` calls against a Rook-managed CephNFS cluster.

## Why this exists

Rook's `CephNFS` CRD provisions the Ganesha server but does NOT declare
which CephFS paths get exported — that's a separate `ceph nfs export
apply` API call. Before this operator, exports were applied by an Argo
`Sync` hook Job in `cluster/rook-cluster/`. That pattern has two failure
modes the user (Chris) hit in practice:

1. Argo's drift detection ignores hooks, so a failed hook never re-runs
   on its own. Once `backoffLimit` is hit, manual intervention is needed.
2. Adding a new satellite required editing a heredoc inside a Job script.

The operator replaces that with one CRD per export, owned by the team
that owns the consumer.

## CRD shape

```yaml
apiVersion: nfs.luma-homelab.io/v1alpha1
kind: CephNFSExport
metadata:
  name: octoprint
  namespace: rook
spec:
  cluster: satellites               # CephNFS cluster_id
  fs: fs-pool                       # CephFS filesystem name
  path: /satellites/octoprint       # path inside the CephFS (mkdir'd if missing)
  pseudo: /satellites/octoprint     # NFS pseudo-path mount clients see
  squash: no_root_squash            # optional (default: no_root_squash)
  accessType: NONE                  # optional default-access (default: NONE)
  clients:
    - addresses: ["192.168.0.243/32"]
      accessType: RW
      squash: no_root_squash
```

## Local dev

```bash
nix-shell shell.nix --run '
  cd operators/ceph-nfs-export-operator
  uv venv && . .venv/bin/activate
  uv pip install -r requirements.txt
  ruff check src/
'
```

Image is built via `.github/workflows/ceph-nfs-export-operator.yml` and
published to `ghcr.io/christopherjmiller/ceph-nfs-export-operator`.

## Deploy

In-cluster manifests live under `cluster/ceph-nfs-export-operator/`,
wired in via `cluster/applications/ceph-nfs-export-operator.yaml`.
