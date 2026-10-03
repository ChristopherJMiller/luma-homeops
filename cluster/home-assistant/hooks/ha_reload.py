#!/usr/bin/env python3
"""Argo PostSync hook: make the running Home Assistant match git.

1. Read the revision git just shipped (packages/config_revision.yaml).
2. Call homeassistant.reload_all until sensor.ha_config_revision shows it.
   Kubelet takes up to ~2 min to update the pod's ConfigMap volume.
3. Every top-level key in the packages is an integration. Once HA reports
   RUNNING (during STARTING its component list is still incomplete), any that
   it has not loaded (e.g. a new `prometheus:`) needs a restart: patch the
   Deployment's restartedAt annotation once, then wait until everything
   declared is loaded.
Exits non-zero on failure, so Argo marks the hook failed.

Stdlib only (runs in plain python:alpine). Tested by
tools/hactl/tests/test_ha_reload.py. Runbook: docs/ha.md.
"""
import datetime
import http.client
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REV_RE = re.compile(r'^\s*state:\s*"([0-9a-f]{12})"', re.M)
TOPKEY_RE = re.compile(r"^([a-z_][a-z0-9_]*):", re.M)
SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def log(msg):
    print(f"{datetime.datetime.now(datetime.timezone.utc):%H:%M:%S} {msg}", flush=True)


def expected_revision(packages: Path) -> str:
    m = REV_RE.search((packages / "config_revision.yaml").read_text())
    if not m:
        raise SystemExit("config_revision.yaml has no revision; was the pre-commit hook skipped?")
    return m.group(1)


def declared_domains(packages: Path) -> set:
    out = set()
    for f in sorted(packages.glob("*.yaml")):
        out |= set(TOPKEY_RE.findall(f.read_text()))
    return out


def make_ha(base: str, token: str):
    def call(method, path, body=None, timeout=60):
        req = urllib.request.Request(
            base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
        return json.loads(raw) if raw else None
    return call


def poll(attempt, timeout, interval, clock=time.monotonic, sleep=time.sleep) -> bool:
    """Call attempt() until it returns True or the timeout passes. HTTP/socket errors count as 'not yet'."""
    deadline = clock() + timeout
    while True:
        try:
            if attempt():
                return True
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, KeyError) as e:
            log(f"  not yet: {e}")
        if clock() >= deadline:
            return False
        sleep(interval)


def reload_until_revision(ha, expected, timeout=360, interval=20, clock=time.monotonic, sleep=time.sleep) -> bool:
    def attempt():
        ha("POST", "/api/services/homeassistant/reload_all", {})
        state = (ha("GET", "/api/states/sensor.ha_config_revision") or {}).get("state")
        log(f"  loaded revision: {state}")
        return state == expected
    return poll(attempt, timeout, interval, clock=clock, sleep=sleep)


def missing_integrations(ha, declared) -> set:
    components = set(ha("GET", "/api/config")["components"])
    return {d for d in declared if d not in components}


def missing_when_running(ha, declared, timeout=600, interval=15, clock=time.monotonic, sleep=time.sleep):
    """Declared integrations HA hasn't loaded, judged only once HA reports RUNNING.

    None if HA never reached RUNNING within the timeout.
    """
    result = {}

    def attempt():
        cfg = ha("GET", "/api/config")
        if cfg.get("state") != "RUNNING":
            log(f"  HA state: {cfg.get('state')}")
            return False
        result["missing"] = {d for d in declared if d not in set(cfg["components"])}
        return True

    return result["missing"] if poll(attempt, timeout, interval, clock=clock, sleep=sleep) else None


def restart_home_assistant(namespace="home-assistant", name="ha-home-assistant"):
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
    req = urllib.request.Request(
        f"https://{host}:{port}/apis/apps/v1/namespaces/{namespace}/deployments/{name}",
        data=json.dumps(body).encode(), method="PATCH",
        headers={"Authorization": f"Bearer {(SA / 'token').read_text().strip()}",
                 "Content-Type": "application/strategic-merge-patch+json"},
    )
    urllib.request.urlopen(req, context=ssl.create_default_context(cafile=str(SA / "ca.crt")), timeout=30).read()


def main() -> int:
    packages = Path(os.environ.get("PACKAGES_DIR", "/packages"))
    ha = make_ha(os.environ.get("HA_URL", "http://ha-home-assistant.home-assistant.svc:8123"), os.environ["HA_TOKEN"].strip())
    expected = expected_revision(packages)
    declared = declared_domains(packages)
    log(f"expected revision {expected}; packages declare {sorted(declared)}")
    if not reload_until_revision(ha, expected):
        log("FAIL: HA never showed the expected revision (ConfigMap not propagated, or reload failing)")
        return 1
    missing = missing_when_running(ha, declared)
    if missing is None:
        log("FAIL: HA never reported RUNNING")
        return 1
    if not missing:
        log("ok: revision loaded; every declared integration is loaded")
        return 0
    log(f"not loaded: {sorted(missing)} -> restarting Home Assistant once")
    restart_home_assistant()
    time.sleep(30)  # strategy Recreate: let the old pod go before polling
    missing = missing_when_running(ha, declared, timeout=900)
    if missing is None:
        log("FAIL: after the restart, HA never reported RUNNING (check the pod and its check-config init container)")
        return 1
    if missing:
        log(f"FAIL: after the restart, still not loaded: {sorted(missing)} (check the HA log)")
        return 1
    log("ok: restarted once; every declared integration is loaded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
