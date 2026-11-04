"""Static guard: Renovate must never automerge the forgejo-helm chart.

    python -m pytest -q -p no:cacheprovider scripts/test_forgejo_renovate.py

Every chart bump changes the pod template or init scripts, so an automerge restarts
Forgejo at a random time, and Forgejo cannot start while SSO discovery is down
(docs/forgejo.md). The argocd manager names the chart by `forgejo-release.yaml`; the
rule matches on manager and file so it holds whatever depName and datasource Renovate
derives from the schemeless OCI `repoURL`. Reads only `.renovaterc.json`.
"""

import fnmatch
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RELEASE = "cluster/applications/forgejo-release.yaml"
# Names the argocd manager can produce for the chart (OCI or plain helm).
NAMES = ["forgejo", "code.forgejo.org/forgejo-helm/forgejo", "code.forgejo.org/forgejo-helm"]
DATASOURCES = ["helm", "docker"]


def rules() -> list[dict]:
    return json.loads((REPO / ".renovaterc.json").read_text())["packageRules"]


def _pattern_hits(patterns: list[str], value: str) -> bool:
    for p in patterns:
        if p.startswith("/") and p.endswith("/"):
            if re.search(p[1:-1], value):
                return True
        elif fnmatch.fnmatch(value, p):
            return True
    return False


def could_match_chart(rule: dict) -> bool:
    """Conservative: True unless a matcher in the rule provably excludes the chart."""
    if "matchManagers" in rule and "argocd" not in rule["matchManagers"]:
        return False
    if "matchDatasources" in rule and not set(rule["matchDatasources"]) & set(DATASOURCES):
        return False
    if "matchFileNames" in rule and not _pattern_hits(rule["matchFileNames"], RELEASE):
        return False
    for key in ("matchPackageNames", "matchDepNames"):
        if key in rule and not any(_pattern_hits(rule[key], n) for n in NAMES):
            return False
    return True


def guard_rules() -> list[dict]:
    return [
        r for r in rules()
        if r.get("automerge") is False
        and r.get("matchManagers") == ["argocd"]
        and RELEASE in r.get("matchFileNames", [])
        and "matchUpdateTypes" not in r
    ]


def test_forgejo_chart_has_a_no_automerge_rule():
    found = guard_rules()
    assert len(found) == 1, f"exactly one argocd/{RELEASE} automerge:false rule, got {len(found)}"
    assert "docs/forgejo.md" in found[0].get("description", ""), "description must point to docs/forgejo.md"


def test_no_automerge_true_rule_can_match_the_chart():
    for r in rules():
        if r.get("automerge") is True:
            assert not could_match_chart(r), f"automerge:true rule could match forgejo-helm: {r}"


def test_guard_comes_after_every_rule_that_sets_automerge():
    # Later packageRules win, and the presets (:automergeMinor) are applied before all of them.
    all_rules = rules()
    guard = all_rules.index(guard_rules()[0])
    for i, r in enumerate(all_rules):
        if "automerge" in r and i > guard:
            assert not could_match_chart(r), f"rule {i} after the guard sets automerge and could match"


def test_guard_helper_catches_a_broad_automerge_rule():
    assert could_match_chart({"automerge": True})
    assert could_match_chart({"matchManagers": ["argocd"], "automerge": True})
    assert not could_match_chart({"matchManagers": ["kubernetes"], "automerge": True})
    assert not could_match_chart({"matchFileNames": ["cluster/applications/traefik-release.yaml"]})
