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


def test_failed_screenshots_keep_the_health_report(monkeypatch, capsys):
    import argparse

    monkeypatch.setattr(deploy, "_git", lambda *a: HEAD if a[0] == "rev-parse" else "origin/main")
    monkeypatch.setattr(deploy, "wait_for_rollout", lambda head, timeout: None)
    monkeypatch.setattr(deploy.health, "collect", lambda client: {"marker": True})
    monkeypatch.setattr(deploy.health, "render", lambda report: (["health: ok"], False))

    def boom(*a, **k):
        raise HactlError("HA answered HTTP 502 for /home-ops/0: restarting?")

    monkeypatch.setattr(deploy.shot, "take", boom)
    monkeypatch.setattr("hactl.client.Client", lambda: object())
    rc = deploy._run(argparse.Namespace(timeout=10, shot=True, json=False))
    out = capsys.readouterr().out
    assert rc == 1
    assert "health: ok" in out and "screenshots failed" in out


def dep(generation=2, observed=2, replicas=1, updated=1, ready=1, current=1):
    return {"metadata": {"generation": generation}, "spec": {"replicas": replicas},
            "status": {"observedGeneration": observed, "updatedReplicas": updated, "readyReplicas": ready, "replicas": current}}


def test_rollout_done():
    assert deploy.rollout_done(dep())
    assert not deploy.rollout_done(dep(observed=1))          # controller hasn't seen the new spec
    assert not deploy.rollout_done(dep(updated=0))           # new pod not created yet
    assert not deploy.rollout_done(dep(ready=0))             # new pod not ready (init containers, startup)
    assert not deploy.rollout_done(dep(current=2))           # old pod still around


def test_wait_until_retries_errors_and_times_out():
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s

    answers = [HactlError("down"), False, True]
    assert deploy.wait_until(lambda: (lambda a: (_ for _ in ()).throw(a) if isinstance(a, Exception) else a)(answers.pop(0)),
                             "x", timeout=100, interval=10, clock=lambda: clock["t"], sleep=sleep, log=lambda m: None)
    with pytest.raises(HactlError, match="timed out after 30s waiting for HA to report RUNNING"):
        deploy.wait_until(lambda: False, "HA to report RUNNING", timeout=30, interval=10,
                          clock=lambda: clock["t"], sleep=sleep, log=lambda m: None)


def test_release_settled():
    ok = {"sync": "Synced", "phase": "Succeeded", "reconciled_at": "2026-10-03T23:30:00Z"}
    assert deploy.release_settled(ok, since=None)
    assert deploy.release_settled(ok, since="2026-10-03T23:29:00Z")
    assert not deploy.release_settled(ok, since="2026-10-03T23:31:00Z")       # hasn't seen the new spec yet
    assert not deploy.release_settled(dict(ok, phase="Running"), since=None)
    assert not deploy.release_settled(dict(ok, sync="OutOfSync"), since=None)


def test_rollout_wait_does_not_need_the_chart_repo_to_match_head():
    # home-assistant-release tracks the ha-helm repo: its revision is never this repo's HEAD.
    chart = "c" * 40
    apps = {
        "home-assistant": st(HEAD, "Synced", "Succeeded", HEAD),
        "applications": dict(st(HEAD, "Synced", "Succeeded", HEAD), finished_at="2026-10-03T23:00:00Z"),
        "home-assistant-release": dict(st(chart, "Synced", "Succeeded", chart), reconciled_at="2026-10-03T23:00:05Z"),
    }
    deploy.wait_for_rollout(HEAD, timeout=30, status=lambda app: apps[app], deployment=lambda: dep(),
                            ha_running=lambda: True, clock=lambda: 0.0, sleep=lambda s: None, log=lambda m: None)
