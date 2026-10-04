"""After a push: wait for Argo, the ha-reload hook and the HA rollout, then verify.

Read-only on the cluster: Argo applies, the hook reloads/restarts; this waits
and checks:
  1. `home-assistant` (config ConfigMaps + the hook) has synced HEAD;
  2. the app-of-apps root `applications` has synced HEAD, and if that changed
     an Application (e.g. home-assistant-release.yaml: image tag, chart
     values), `home-assistant-release` has reconciled since and is Synced.
     Its revision is the ha-helm chart repo's, never this repo's HEAD;
  3. the Deployment has rolled out, and HA reports RUNNING.
Argo polls git every ~3 min, so expect a short wait.
"""
import json
import subprocess
import time

from hactl import health, output, paths, shot
from hactl.errors import HactlError
from hactl.state import cli as state_cli

DEFAULT_VIEWS = ["/home-ops/0", "/home-ops/1", "/home-ops/2", "/home-ops/3"]
CONFIG_APP, ROOT_APP, RELEASE_APP = "home-assistant", "applications", "home-assistant-release"


def _git(*args) -> str:
    return subprocess.run(["git", *args], cwd=paths.REPO, capture_output=True, text=True, check=True).stdout.strip()


def argo_status(app="home-assistant") -> dict:
    r = subprocess.run(["kubectl", "-n", "argo-cd", "get", "application", app, "-o", "json"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise HactlError(f"kubectl get application failed: {r.stderr.strip()[:200]} (KUBECONFIG set?)")
    st = json.loads(r.stdout).get("status", {})
    op = st.get("operationState") or {}
    return {"sync_revision": (st.get("sync") or {}).get("revision"), "sync": (st.get("sync") or {}).get("status"),
            "phase": op.get("phase"), "op_revision": (op.get("syncResult") or {}).get("revision"),
            "message": op.get("message", ""), "finished_at": op.get("finishedAt"), "reconciled_at": st.get("reconciledAt")}


def wait_synced(head, status=argo_status, timeout=900, interval=10, clock=time.monotonic, sleep=time.sleep, log=print) -> dict:
    """Done when Argo has compared `head`, is Synced, and no operation is running.

    A push that doesn't touch cluster/home-assistant moves sync_revision without
    starting an operation, so op_revision may stay older: that still counts.
    """
    deadline = clock() + timeout
    last = None
    while True:
        st = status()
        if st != last:
            log(f"argo: {st['sync']} / {st['phase']} (compared {str(st['sync_revision'])[:8]}, "
                f"last sync {str(st['op_revision'])[:8]}) {st['message'][:120]}")
            last = st
        if st["op_revision"] == head and st["phase"] in ("Failed", "Error"):
            raise HactlError(f"Argo sync of {head[:8]} ended {st['phase']}: {st['message']}")
        if st["sync_revision"] == head and st["sync"] == "Synced" and st["phase"] not in ("Running", "Terminating"):
            return st
        if clock() >= deadline:
            raise HactlError(f"timed out after {timeout}s waiting for Argo to sync {head[:8]}")
        sleep(interval)


def release_settled(st: dict, since) -> bool:
    """The release app is Synced, idle, and (if `since`) has reconciled after the root app applied a change."""
    if st.get("sync") != "Synced" or st.get("phase") in ("Running", "Terminating"):
        return False
    return since is None or (st.get("reconciled_at") or "") >= since


def rollout_done(dep: dict) -> bool:
    """The Deployment runs its current spec: observed, every replica updated and ready, no old pod left."""
    want = dep.get("spec", {}).get("replicas", 1)
    st = dep.get("status", {})
    return (st.get("observedGeneration", 0) >= dep["metadata"]["generation"]
            and st.get("updatedReplicas", 0) == want
            and st.get("readyReplicas", 0) == want
            and st.get("replicas", 0) == want)


def deployment_status() -> dict:
    r = subprocess.run(["kubectl", "-n", "home-assistant", "get", "deployment", "ha-home-assistant", "-o", "json"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise HactlError(f"kubectl get deployment failed: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


def wait_until(check, what, timeout, interval=10, clock=time.monotonic, sleep=time.sleep, log=print) -> bool:
    """Poll check() until true. HactlError (HA or kubectl briefly away) counts as 'not yet'."""
    deadline = clock() + timeout
    while True:
        try:
            if check():
                return True
        except HactlError as e:
            log(f"  waiting for {what}: {e}")
        if clock() >= deadline:
            raise HactlError(f"timed out after {timeout}s waiting for {what}")
        sleep(interval)


def wait_for_rollout(head, timeout, status=argo_status, deployment=deployment_status, ha_running=None,
                     clock=time.monotonic, sleep=time.sleep, log=print) -> None:
    if ha_running is None:
        from hactl.client import Client

        client = Client()
        ha_running = lambda: client.get("/api/config").get("state") == "RUNNING"  # noqa: E731
    timing = {"clock": clock, "sleep": sleep, "log": log}
    wait_synced(head, status=lambda: status(CONFIG_APP), timeout=timeout, **timing)
    root = wait_synced(head, status=lambda: status(ROOT_APP), timeout=timeout, **timing)
    since = root.get("finished_at") if root.get("op_revision") == head else None  # root changed an Application
    wait_until(lambda: release_settled(status(RELEASE_APP), since), f"{RELEASE_APP} to sync", timeout, **timing)
    wait_until(lambda: rollout_done(deployment()), "the HA Deployment to roll out", timeout, **timing)
    wait_until(ha_running, "HA to report RUNNING", timeout, **timing)


def _run(args) -> int:
    from hactl.client import Client

    head = _git("rev-parse", "HEAD")
    if not _git("branch", "-r", "--contains", head):
        raise HactlError("HEAD is not pushed yet: git push first")
    wait_for_rollout(head, args.timeout)
    client = Client()
    state_lines, state_ok = state_cli.converge(client, prune=False, log=lambda m: None)
    report = health.collect(client)
    lines, problem = health.render(report)
    lines = state_lines + lines
    problem = problem or not state_ok
    bad = False
    if args.shot:
        try:
            results = shot.take(client, shot.plan_shots(DEFAULT_VIEWS, ["phone", "desktop"], ["dark"]), shot.default_out_dir())
            shot_lines, bad = shot.report(results)
        except HactlError as e:  # keep the health report; say why there are no shots
            shot_lines, bad = [f"screenshots failed: {e}"], True
        lines += shot_lines
    output.emit(args, report, [f"deployed {head[:8]}"] + lines)
    return 1 if problem or bad else 0


def register(sub) -> None:
    p = sub.add_parser("deploy", parents=[output.COMMON], help="after a push: wait for Argo + ha-reload, then health (+ shots)")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--shot", action="store_true", help="also screenshot the home-ops views")
    p.set_defaults(func=_run)
