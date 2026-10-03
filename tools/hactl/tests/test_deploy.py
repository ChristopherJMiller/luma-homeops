import pytest

from hactl import deploy
from hactl.errors import HactlError

HEAD, OLD = "a" * 40, "b" * 40


def st(sync_revision, sync, phase, op_revision, message=""):
    return {"sync_revision": sync_revision, "sync": sync, "phase": phase, "op_revision": op_revision, "message": message}


def run(seq, timeout=900):
    seq = list(seq)
    clock = {"t": 0.0}

    def status():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def sleep(s):
        clock["t"] += s

    return deploy.wait_synced(HEAD, status=status, timeout=timeout, interval=10,
                              clock=lambda: clock["t"], sleep=sleep, log=lambda m: None)


def test_waits_through_out_of_sync_and_running():
    out = run([st(OLD, "Synced", "Succeeded", OLD), st(HEAD, "OutOfSync", "Succeeded", OLD),
               st(HEAD, "OutOfSync", "Running", HEAD), st(HEAD, "Synced", "Succeeded", HEAD)])
    assert out["op_revision"] == HEAD


def test_untouched_app_counts_as_synced():
    out = run([st(OLD, "Synced", "Succeeded", OLD), st(HEAD, "Synced", "Succeeded", OLD)])
    assert out["sync_revision"] == HEAD


def test_failed_hook_raises():
    with pytest.raises(HactlError, match="Failed"):
        run([st(HEAD, "Synced", "Failed", HEAD, "PostSync hook ha-reload failed")])


def test_timeout():
    with pytest.raises(HactlError, match="timed out"):
        run([st(OLD, "Synced", "Succeeded", OLD)], timeout=30)
