"""Self-test for ci-dispatch's ledger restore, with no cluster and no restic repo.

    python -m pytest -q -p no:cacheprovider scripts/test_ci_dispatch_ledger_restore.py

Runs as the `ci-dispatch-ledger-restore` pre-commit hook (docs/ci.md, Restoring the ledger).

`ci-dispatch-ledger-restore-pod.sh` runs against a fake /data, with a stub `restic` first on PATH.
It runs under every POSIX shell found here: `sh`, plus `dash` and `busybox sh` when present (the pod
runs busybox). What it pins: the replacement is copied and checked for the SQLite header before
anything moves, so a missing file, an `--include` that matched nothing (restic exits 0 then), a
failed restore or a non-SQLite file all fail and leave ledger.db and its -wal and -shm files
byte-for-byte as they were.

`ci-dispatch-ledger-restore.sh` runs against a stub `kubectl`: it refuses bad names, any snapshot
id that isn't hex (`latest` included), and a dispatcher that isn't stopped, before any apply. Its
`--print` manifest is a Pod Security `restricted` pod with the restic jobs' label and image, the
ledger PVC read-write, no affinity, and the pod script byte-for-byte.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
POD = HERE / "ci-dispatch-ledger-restore-pod.sh"
WRAPPER = HERE / "ci-dispatch-ledger-restore.sh"
RESTIC_JOB = REPO / "cluster/music-streaming/restic-backup-cronjob.yaml"

SNAP = "ledger-20261120T030012Z.db"
RID = "1a2b3c4d"
SQLITE = b"SQLite format 3\x00" + b"\x10\x00\x02\x02" + b"restored ledger"
LIVE = {"ledger.db": SQLITE[:16] + b"live ledger", "ledger.db-wal": b"live wal", "ledger.db-shm": b"live shm"}

# The stub restic: logs its arguments, then acts by STUB_MODE. `file` restores the included path
# under --target; `empty` restores nothing and exits 0 (restic's answer to an --include that matches
# nothing); `fail` exits 1; `garbage` restores a file that isn't SQLite.
STUB_RESTIC = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB_LOG"
target= include=
while [ $# -gt 0 ]; do
  case $1 in
    --target) target=$2; shift ;;
    --include) include=$2; shift ;;
  esac
  shift
done
case $STUB_MODE in
  file|garbage)
    mkdir -p "$target$(dirname "$include")"
    if [ "$STUB_MODE" = file ]; then cat "$STUB_SOURCE"; else echo not a database; fi > "$target$include"
    ;;
  empty) ;;
  fail) echo "Fatal: no such snapshot" >&2; exit 1 ;;
esac
"""

STUB_KUBECTL = r"""#!/bin/sh
printf '%s\n' "$*" >> "$KLOG"
case "$*" in
  *"get statefulset ci-dispatch"*) printf '%s' "$STUB_REPLICAS" ;;
  *"get pod "*)
    for p in $STUB_PODS; do
      case "$*" in *"get pod $p "*) echo "pod/$p" ;; esac
    done
    ;;
  *"apply -f -"*) cat > "$KAPPLY" ;;
  *) echo "stub kubectl: unexpected $*" >&2; exit 9 ;;
esac
"""


def _shells():
    """Every POSIX shell to run the pod script under: its argv, and busybox for its applets."""
    out = [pytest.param(("sh",), None, id="sh")]
    if shutil.which("dash"):
        out.append(pytest.param(("dash",), None, id="dash"))
    bb = shutil.which("busybox")
    if bb:
        out.append(pytest.param(("busybox", "sh"), bb, id="busybox"))
    return out


SHELLS = _shells()


def _write_exec(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def world(tmp_path):
    data = tmp_path / "data"
    (data / "snapshots").mkdir(parents=True)
    for name, body in LIVE.items():
        (data / name).write_bytes(body)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    _write_exec(bin_ / "restic", STUB_RESTIC)
    source = tmp_path / "source.db"
    source.write_bytes(SQLITE)
    return {"tmp": tmp_path, "data": data, "bin": bin_, "log": tmp_path / "restic.log", "source": source}


def run_pod(world, shell, busybox, mode="file", snap=SNAP, rid=RID):
    bin_ = world["bin"]
    if busybox:
        # The pod's coreutils are busybox applets: put them ahead of the host's.
        for applet in ("head", "od", "tr", "cp", "mv", "mkdir", "rm", "ls", "date", "dirname", "cat"):
            link = bin_ / applet
            if not link.exists():
                link.symlink_to(busybox)
    env = {
        "PATH": f"{bin_}:{os.environ['PATH']}",
        "DATA": str(world["data"]),
        "WORK": str(world["tmp"] / "restore"),
        "STUB_LOG": str(world["log"]),
        "STUB_MODE": mode,
        "STUB_SOURCE": str(world["source"]),
        "HOME": str(world["tmp"]),
    }
    if snap is not None:
        env["SNAP"] = snap
    if rid is not None:
        env["RID"] = rid
    return subprocess.run([*shell, str(POD)], env=env, capture_output=True, text=True, timeout=60)


def assert_untouched(world):
    data = world["data"]
    for name, body in LIVE.items():
        assert (data / name).read_bytes() == body, name
    assert not list(data.glob("before-restore-*")), "something moved aside"
    assert not (data / "ledger.db.part").exists(), "a partial copy was left"


def restic_calls(world):
    return world["log"].read_text().splitlines() if world["log"].exists() else []


@pytest.mark.parametrize("shell,busybox", SHELLS)
class TestPodScript:
    def test_from_the_volume(self, world, shell, busybox):
        (world["data"] / "snapshots" / SNAP).write_bytes(SQLITE)
        r = run_pod(world, shell, busybox)
        assert r.returncode == 0, r.stderr
        assert (world["data"] / "ledger.db").read_bytes() == SQLITE
        (old,) = world["data"].glob("before-restore-*")
        for name, body in LIVE.items():
            assert (old / name).read_bytes() == body
        assert not (world["data"] / "ledger.db-wal").exists()
        assert not (world["data"] / "ledger.db-shm").exists()
        assert restic_calls(world) == [], "restic ran though the file was on the volume"

    def test_from_restic(self, world, shell, busybox):
        r = run_pod(world, shell, busybox)
        assert r.returncode == 0, r.stderr
        assert (world["data"] / "ledger.db").read_bytes() == SQLITE
        (call,) = restic_calls(world)
        assert call.split() == ["restore", RID, "--include", f"/data/snapshots/{SNAP}", "--target",
                                str(world["tmp"] / "restore"), "--no-cache", "--retry-lock", "15m"]

    def test_onto_an_empty_volume(self, world, shell, busybox):
        for name in LIVE:
            (world["data"] / name).unlink()
        r = run_pod(world, shell, busybox)
        assert r.returncode == 0, r.stderr
        assert (world["data"] / "ledger.db").read_bytes() == SQLITE

    def test_include_matched_nothing(self, world, shell, busybox):
        r = run_pod(world, shell, busybox, mode="empty")
        assert r.returncode == 1
        assert "nothing moved" in r.stderr
        assert len(restic_calls(world)) == 1
        assert_untouched(world)

    def test_restic_fails(self, world, shell, busybox):
        r = run_pod(world, shell, busybox, mode="fail")
        assert r.returncode != 0
        assert_untouched(world)

    def test_not_sqlite_from_restic(self, world, shell, busybox):
        r = run_pod(world, shell, busybox, mode="garbage")
        assert r.returncode == 1
        assert "SQLite header" in r.stderr
        assert_untouched(world)

    def test_not_sqlite_on_the_volume(self, world, shell, busybox):
        (world["data"] / "snapshots" / SNAP).write_bytes(b"SQLite format 2\x00 almost")
        r = run_pod(world, shell, busybox)
        assert r.returncode == 1
        assert_untouched(world)

    def test_empty_file_on_the_volume_falls_back_to_restic(self, world, shell, busybox):
        (world["data"] / "snapshots" / SNAP).write_bytes(b"")
        r = run_pod(world, shell, busybox, mode="empty")
        assert r.returncode == 1
        assert_untouched(world)

    @pytest.mark.parametrize("snap", ["ledger.db", "../ledger-20261120T030012Z.db.x", "snap"])
    def test_bad_name(self, world, shell, busybox, snap):
        r = run_pod(world, shell, busybox, snap=snap)
        assert r.returncode == 2
        assert restic_calls(world) == []
        assert_untouched(world)

    @pytest.mark.parametrize("missing", ["snap", "rid"])
    def test_missing_input(self, world, shell, busybox, missing):
        kw = {"snap": None} if missing == "snap" else {"rid": None}
        r = run_pod(world, shell, busybox, **kw)
        assert r.returncode != 0
        assert restic_calls(world) == []
        assert_untouched(world)

    def test_stale_part_file_is_replaced(self, world, shell, busybox):
        (world["data"] / "ledger.db.part").write_bytes(b"left over")
        r = run_pod(world, shell, busybox)
        assert r.returncode == 0, r.stderr
        assert (world["data"] / "ledger.db").read_bytes() == SQLITE


@pytest.fixture
def kube(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    _write_exec(bin_ / "kubectl", STUB_KUBECTL)
    return {"bin": bin_, "log": tmp_path / "kubectl.log", "apply": tmp_path / "applied.yaml"}


def run_wrapper(kube, *args, replicas="0", pods=""):
    env = {
        "PATH": f"{kube['bin']}:{os.environ['PATH']}",
        "KLOG": str(kube["log"]),
        "KAPPLY": str(kube["apply"]),
        "STUB_REPLICAS": replicas,
        "STUB_PODS": pods,
        "KUBECONFIG": "/nonexistent",
    }
    return subprocess.run(["bash", str(WRAPPER), *args], env=env, capture_output=True, text=True, timeout=60)


def kubectl_calls(kube):
    return kube["log"].read_text().splitlines() if kube["log"].exists() else []


@pytest.mark.parametrize("args", [
    (),
    (SNAP,),
    (SNAP, "latest"),
    (SNAP, "LATEST"),
    (SNAP, "1a2b3c"),
    (SNAP, "1a2b3c4d; rm -rf /"),
    ("ledger.db", RID),
    ("ledger-2026-11-20.db", RID),
    (f"../{SNAP}", RID),
    (SNAP, RID, "extra"),
])
def test_wrapper_refuses_bad_arguments(kube, args):
    r = run_wrapper(kube, *args)
    assert r.returncode == 2, r.stderr
    assert kubectl_calls(kube) == []


@pytest.mark.parametrize("replicas,pods", [("1", ""), ("", ""), ("0", "ci-dispatch-0"), ("0", "ledger-restore")])
def test_wrapper_refuses_a_running_dispatcher(kube, replicas, pods):
    r = run_wrapper(kube, SNAP, RID, replicas=replicas, pods=pods)
    assert r.returncode == 1, r.stderr
    assert not any("apply" in c for c in kubectl_calls(kube))


def test_wrapper_applies_once_stopped(kube):
    r = run_wrapper(kube, SNAP, RID)
    assert r.returncode == 0, r.stderr
    calls = kubectl_calls(kube)
    assert calls[-1] == "-n ci-dispatch apply -f -"
    printed = run_wrapper(kube, "--print", SNAP, RID)
    assert kube["apply"].read_text() == printed.stdout


def test_print_needs_no_cluster(kube):
    r = run_wrapper(kube, "--print", SNAP, RID)
    assert r.returncode == 0, r.stderr
    assert kubectl_calls(kube) == []


def test_manifest(kube):
    r = run_wrapper(kube, "--print", SNAP, RID)
    pod = yaml.safe_load(r.stdout)
    assert pod["kind"] == "Pod"
    assert pod["metadata"] == {"name": "ledger-restore", "namespace": "ci-dispatch",
                               "labels": {"app.kubernetes.io/name": "restic-backup"}}
    spec = pod["spec"]
    assert spec["restartPolicy"] == "Never"
    assert spec["automountServiceAccountToken"] is False
    assert "affinity" not in spec and "nodeName" not in spec
    assert spec["securityContext"] == {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532,
                                       "fsGroup": 65532, "seccompProfile": {"type": "RuntimeDefault"}}
    (c,) = spec["containers"]
    assert c["securityContext"] == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}}
    restic = yaml.safe_load(RESTIC_JOB.read_text())
    want = restic["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"]
    assert c["image"] == want, "the restore pod's restic image drifted from the restic jobs'"
    assert c["envFrom"] == [{"secretRef": {"name": "restic-config"}}]
    env = {e["name"]: e["value"] for e in c["env"]}
    assert env == {"SNAP": SNAP, "RID": RID, "HOME": "/tmp"}
    assert c["command"] == ["/bin/sh", "-c"]
    assert c["args"] == [POD.read_text()], "the pod must run the tested script byte-for-byte"
    assert {m["name"]: m["mountPath"] for m in c["volumeMounts"]} == {"data": "/data", "tmp": "/tmp"}
    assert not any(m.get("readOnly") for m in c["volumeMounts"])
    vols = {v["name"]: v for v in spec["volumes"]}
    assert vols["data"]["persistentVolumeClaim"] == {"claimName": "ci-dispatch-ledger"}
    assert vols["tmp"]["emptyDir"] == {"sizeLimit": "512Mi"}


def test_scripts_are_executable():
    for f in (POD, WRAPPER):
        assert os.access(f, os.X_OK), f
