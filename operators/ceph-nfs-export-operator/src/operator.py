"""kopf-based reconciler for CephNFSExport custom resources.

Each CephNFSExport CR declares an NFS export on a Rook-managed CephNFS
cluster. The operator translates the CR into the equivalent
`ceph nfs export apply` API call (the same operation the Rook tools pod
runs by hand), and removes the export on CR deletion.

The image bundles the ceph + cephfs-shell CLIs (built on the ceph base
image) and bind-mounts the same `rook-ceph-mon` Secret + `rook-ceph-mon-
endpoints` ConfigMap as the rook-ceph-tools Deployment, so authentication
is identical to what an operator would have in the Rook toolbox.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any

import kopf

GROUP = "nfs.luma-homelab.io"
VERSION = "v1alpha1"
PLURAL = "cephnfsexports"

CEPH_CONF_DIR = "/etc/ceph"
MON_ENDPOINTS = "/etc/rook/mon-endpoints"
MON_SECRET = "/var/lib/rook-ceph-mon/secret.keyring"


def _bootstrap_ceph_conf() -> None:
    """Write /etc/ceph/{keyring,ceph.conf} from the mounted Rook secrets.

    Mirrors the bootstrap that rook-ceph-tools' entrypoint does. Runs once
    at startup; if the mon endpoints change while the operator is running
    (rare), the pod is restarted by the Deployment controller via the
    rolling update of the mon ConfigMap.
    """
    username = os.environ["ROOK_CEPH_USERNAME"]
    with open(MON_SECRET, "r") as f:
        key = f.read().strip()
    with open(MON_ENDPOINTS, "r") as f:
        raw = f.read().strip()
    # mon-endpoints is `<name>=<ip:port>,<name>=<ip:port>` — strip names.
    mon_hosts = ",".join(part.split("=", 1)[1] for part in raw.split(",") if "=" in part)

    os.makedirs(CEPH_CONF_DIR, exist_ok=True)
    with open(os.path.join(CEPH_CONF_DIR, "keyring"), "w") as f:
        f.write(f"[{username}]\nkey = {key}\n")
    with open(os.path.join(CEPH_CONF_DIR, "ceph.conf"), "w") as f:
        f.write(
            f"[global]\nmon_host = {mon_hosts}\n\n"
            f"[client.admin]\nkeyring = {CEPH_CONF_DIR}/keyring\n"
        )


def _run(argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


# libcephfs's Python binding (python3-cephfs) in the ceph base image is
# compiled against Python 3.9 only. The operator itself runs on Python
# 3.12 (kopf >=1.39 requires it), so we shell out to /usr/bin/python3
# (3.9) to do the mkdir via libcephfs. cephfs-shell isn't packaged in the
# ceph image, so this is the cleanest path that doesn't pull a kernel
# mount or ceph-fuse into the container.
_MKDIR_PY = """
import sys, cephfs
fs_name = sys.argv[1]
path = sys.argv[2].encode()
c = cephfs.LibCephFS(conffile='/etc/ceph/ceph.conf')
c.init()
c.mount(b'/', filesystem_name=fs_name)
try:
    c.mkdirs(path, 0o755)
finally:
    c.unmount()
    c.shutdown()
"""


def _ensure_dir(fs_name: str, path: str, logger: logging.Logger) -> None:
    """mkdir -p the export path inside the CephFS via libcephfs.

    `ceph nfs export apply` requires the path to pre-exist. Uses the
    system Python 3.9 (which has the python3-cephfs C extension) as a
    one-shot script.
    """
    cp = subprocess.run(
        ["/usr/bin/python3", "-c", _MKDIR_PY, fs_name, path],
        capture_output=True,
        text=True,
        check=False,
    )
    if cp.returncode != 0:
        raise kopf.TemporaryError(
            f"libcephfs mkdir failed (rc={cp.returncode}): {cp.stderr.strip()}",
            delay=30,
        )
    logger.info("ensured %s on fs %s", path, fs_name)


def _export_payload(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": spec["path"],
        "pseudo": spec["pseudo"],
        "cluster_id": spec["cluster"],
        "access_type": spec.get("accessType", "NONE"),
        "squash": spec.get("squash", "no_root_squash"),
        "protocols": spec.get("protocols", [4]),
        "transports": spec.get("transports", ["TCP"]),
        "fsal": {"name": "CEPH", "fs_name": spec["fs"]},
        "clients": [
            {
                "addresses": c["addresses"],
                "access_type": c.get("accessType", "RW"),
                "squash": c.get("squash", "no_root_squash"),
            }
            for c in spec.get("clients", [])
        ],
    }


def _apply_export(payload: dict[str, Any], logger: logging.Logger) -> None:
    cluster_id = payload["cluster_id"]
    cp = _run(
        ["ceph", "nfs", "export", "apply", cluster_id, "-i", "-"],
        stdin=json.dumps(payload),
    )
    if cp.returncode != 0:
        raise kopf.TemporaryError(
            f"ceph nfs export apply failed (rc={cp.returncode}): {cp.stderr.strip()}",
            delay=30,
        )
    logger.info("export applied: %s", cp.stdout.strip())


def _rm_export(cluster_id: str, pseudo: str, logger: logging.Logger) -> None:
    cp = _run(["ceph", "nfs", "export", "rm", cluster_id, pseudo])
    blob = (cp.stderr + cp.stdout).lower()
    if cp.returncode != 0 and "not found" not in blob and "does not exist" not in blob:
        raise kopf.TemporaryError(
            f"ceph nfs export rm failed (rc={cp.returncode}): {cp.stderr.strip()}",
            delay=30,
        )
    logger.info("export removed: %s/%s", cluster_id, pseudo)


@kopf.on.startup()
async def startup(settings: kopf.OperatorSettings, logger: logging.Logger, **_: Any) -> None:
    settings.persistence.finalizer = f"{GROUP}/finalizer"
    settings.posting.level = logging.INFO
    _bootstrap_ceph_conf()
    logger.info("ceph config bootstrapped at %s", CEPH_CONF_DIR)


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
@kopf.on.resume(GROUP, VERSION, PLURAL)
async def reconcile(
    spec: dict[str, Any],
    name: str,
    namespace: str,
    patch: kopf.Patch,
    logger: logging.Logger,
    **_: Any,
) -> dict[str, Any]:
    cluster_id = spec["cluster"]
    fs_name = spec["fs"]
    path = spec["path"]
    pseudo = spec["pseudo"]
    logger.info(
        "reconcile %s/%s cluster=%s fs=%s path=%s pseudo=%s",
        namespace, name, cluster_id, fs_name, path, pseudo,
    )
    _ensure_dir(fs_name, path, logger)
    payload = _export_payload(spec)
    _apply_export(payload, logger)
    patch.status["applied"] = True
    return {"cluster": cluster_id, "pseudo": pseudo, "path": path}


@kopf.on.delete(GROUP, VERSION, PLURAL)
async def delete(
    spec: dict[str, Any],
    name: str,
    namespace: str,
    logger: logging.Logger,
    **_: Any,
) -> None:
    cluster_id = spec["cluster"]
    pseudo = spec["pseudo"]
    logger.info("delete %s/%s cluster=%s pseudo=%s", namespace, name, cluster_id, pseudo)
    _rm_export(cluster_id, pseudo, logger)
