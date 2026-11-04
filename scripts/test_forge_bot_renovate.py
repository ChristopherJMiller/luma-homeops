"""forge-bot's image never reaches galaxy without review (Task 16 review C1).

    python -m pytest -q -p no:cacheprovider scripts/test_forge_bot_renovate.py

Runs as the `forge-bot-renovate` pre-commit hook. The repo extends `:automergeDigest` and
`:skipStatusChecks`, so without a rule of its own a moved `sha-<7>` tag (a CI re-run on the same
commit, or anyone who can push an image as that tag) would be merged and rolled out by Argo
unseen, into the pod that holds the Forgejo site-admin token and the SSO manage-users secret.

The guard: one dedicated packageRule names the image exactly and sets `automerge: false`, and NO
packageRule that could match the image (exact name, glob or regex, or no name constraint at all)
sets `automerge: true`. "Could match" is decided conservatively: a rule is assumed to match
unless one of its constraints certainly excludes the image (another manager, datasource or file,
a name list that misses it, an exclusion that names it).

These tests open only .renovaterc.json, .pre-commit-config.yaml and the forge-bot StatefulSet.
"""

import fnmatch
import json
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
IMAGE = "git.realliance.net/realliance/forge-bot"
PIN_FILE = "cluster/forge-bot/statefulset.yaml"
MANAGER, DATASOURCE = "kubernetes", "docker"
DESCRIPTION_MUST = "re-pin only after Chris reviews the release (docs/forgejo.md#forge-bot)"


def _rules() -> list[dict]:
    return json.loads((REPO / ".renovaterc.json").read_text())["packageRules"]


def _hits(pattern: str, name: str) -> bool:
    """One Renovate name pattern (without a leading '!'): /regex/ or /regex/i, a glob, or exact.
    fnmatch's '*' also crosses '/', so a glob here matches at least what minimatch would."""
    m = re.fullmatch(r"/(.*)/(i?)", pattern)
    if m:
        return re.search(m[1], name, re.I if m[2] else 0) is not None
    if any(c in pattern for c in "*?[{"):
        return fnmatch.fnmatchcase(name, pattern)
    return pattern == name


def _list_matches(patterns: list[str], name: str) -> bool:
    """matchPackageNames semantics: any matching '!negation' excludes; otherwise the positive
    patterns are OR'ed, and a list of negations only means "everything else"."""
    if any(_hits(p[1:], name) for p in patterns if p.startswith("!")):
        return False
    positive = [p for p in patterns if not p.startswith("!")]
    return not positive or any(_hits(p, name) for p in positive)


def could_match(rule: dict, name: str = IMAGE) -> bool:
    for key in ("matchPackageNames", "matchDepNames"):
        if key in rule and not _list_matches(rule[key], name):
            return False
    for key in ("matchPackagePatterns", "matchDepPatterns"):
        if key in rule and not any(p == "*" or re.search(p, name) for p in rule[key]):
            return False
    for key in ("matchPackagePrefixes", "matchDepPrefixes"):
        if key in rule and not any(name.startswith(p) for p in rule[key]):
            return False
    for key in ("excludePackageNames", "excludeDepNames"):
        if any(_hits(p, name) for p in rule.get(key, [])):
            return False
    if any(re.search(p, name) for p in rule.get("excludePackagePatterns", [])):
        return False
    if any(name.startswith(p) for p in rule.get("excludePackagePrefixes", [])):
        return False
    if "matchManagers" in rule and MANAGER not in rule["matchManagers"]:
        return False
    if "matchDatasources" in rule and DATASOURCE not in rule["matchDatasources"]:
        return False
    if "matchFileNames" in rule and not any(_hits(p, PIN_FILE) for p in rule["matchFileNames"]):
        return False
    return True


def test_could_match_is_not_vacuous():
    for rule in ({"matchPackageNames": [IMAGE]}, {"matchPackageNames": ["git.realliance.net/**"]},
                 {"matchPackageNames": ["/forge-bot/"]}, {"matchPackageNames": ["/FORGE-BOT$/i"]},
                 {"matchPackageNames": ["!ghcr.io/**"]}, {"matchPackagePatterns": ["^git\\.realliance\\."]},
                 {"matchPackagePatterns": ["*"]}, {"matchPackagePrefixes": ["git.realliance.net/"]},
                 {"matchUpdateTypes": ["digest"]}, {"matchManagers": ["kubernetes"]},
                 {"matchDatasources": ["docker"]}, {"matchFileNames": ["cluster/**"]}):
        assert could_match(rule), rule
    for rule in ({"matchManagers": ["argocd"]}, {"matchDatasources": ["github-tags"]},
                 {"matchPackageNames": ["ghcr.io/christopherjmiller/forgejo-runner"]},
                 {"matchPackageNames": [f"!{IMAGE}"]}, {"matchPackageNames": ["git.realliance.net/**", f"!{IMAGE}"]},
                 {"matchPackagePrefixes": ["ghcr.io/"]}, {"excludePackageNames": [IMAGE]},
                 {"matchFileNames": ["images/**"]}, {"matchFileNames": ["cluster/kube-network-policies/upstream-install.yaml"]}):
        assert not could_match(rule), rule


def test_dedicated_rule_never_automerges():
    rules = _rules()
    mine = [r for r in rules if r.get("matchPackageNames") == [IMAGE]]
    assert len(mine) == 1, f"exactly one packageRule must name {IMAGE} on its own"
    rule = mine[0]
    assert rule["automerge"] is False
    assert rule["matchManagers"] == [MANAGER], "the StatefulSet pin is the kubernetes manager's"
    assert DESCRIPTION_MUST in rule["description"]
    notes = " ".join(rule.get("prBodyNotes", []))
    assert "never merge" in notes.lower() and "sync-forge-bot-alerts.sh" in notes


def test_no_rule_that_could_match_automerges():
    rules = _rules()
    bad = [(i, r.get("description", r)) for i, r in enumerate(rules) if could_match(r) and r.get("automerge") is True]
    assert not bad, f"packageRules that could automerge {IMAGE}: {bad}"


def test_rule_names_the_pinned_image():
    images = re.findall(r"^\s*image:\s*(\S+)$", (REPO / PIN_FILE).read_text(), re.M)
    assert len(images) == 1 and images[0].startswith(IMAGE + ":"), images


def test_renovate_hook_wired():
    cfg = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text())
    hooks = {h["id"]: h for repo in cfg["repos"] for h in repo["hooks"]}
    h = hooks["forge-bot-renovate"]
    assert h["entry"] == "python -m pytest -q -p no:cacheprovider scripts/test_forge_bot_renovate.py"
    assert h["language"] == "system" and h["pass_filenames"] is False
    for path in (".renovaterc.json", PIN_FILE, "scripts/test_forge_bot_renovate.py", ".pre-commit-config.yaml"):
        assert re.search(h["files"], path), f"forge-bot-renovate must run when {path} changes"
