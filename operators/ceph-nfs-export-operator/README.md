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

## Troubleshooting

**A new export is applied (`status.applied: true`) but clients get "No such
file or directory".** The operator's `ceph nfs export apply` only writes the
export to RADOS; Ganesha picks it up through a RADOS watch on
`conf-nfs.<cluster>`. If Ganesha has lost that watch, the export is never
served until it restarts. Check both sides:

```bash
TOOLS=$(kubectl -n rook get pod -l app=rook-ceph-tools -o jsonpath='{.items[0].metadata.name}')
kubectl -n rook exec "$TOOLS" -- rados -p .nfs -N satellites listwatchers conf-nfs.satellites   # empty = no watch
NFS=$(kubectl -n rook get pod -l app=rook-ceph-nfs -o jsonpath='{.items[0].metadata.name}')
kubectl -n rook exec "$NFS" -c nfs-ganesha -- dbus-send --system --print-reply \
  --dest=org.ganesha.nfsd /org/ganesha/nfsd/ExportMgr org.ganesha.nfsd.exportmgr.ShowExports | grep 'string "/'
```

Fix: `kubectl -n rook rollout restart deploy/rook-ceph-nfs-satellites-a`
(confirm first: it's an NFS outage of a minute or two). Seen 2026-10-12, when
the watch had been gone since node maintenance a week earlier.

**The export root is `root:root 0755`.** `LibCephFS.mkdirs` runs as the
operator's Ceph client, so a new path isn't writable by uid 1000. Chown it
once from an NFS client with root (the exports are `no_root_squash`):
`sudo chown 1000:1000 /mnt/<path> && sudo chmod 2775 /mnt/<path>`.

**Reads return EIO but `ls` and writes work.** The client negotiated NFSv4.2
and is reading with `READ_PLUS`, which this Ganesha's replies don't survive.
Check `/proc/self/mountstats` on the client (`READ_PLUS` ops climbing, `READ`
at 0) and mount with `nfsvers=4.1`. Seen 2026-10-13.
