"""Static tests for azure/ci/, the fence around paid CI on Azure Container Instances.

    python -m pytest -q -p no:cacheprovider scripts/test_azure_ci.py

Runs as the `azure-ci` pre-commit hook (runner classes phase 2, Tasks 2 and 3). Every value a test
pins comes from azure/ci/fence.json, and test_fence_values pins fence.json itself.

The plan-level tests run only when AZURE_CI_PLAN_JSON names the output of

    ./azure/ci/tf.sh --to "$SCRATCH/plan.json" show -json "$SCRATCH/ci.tfplan"

That file holds the subscription id and other Azure ids. tf.sh writes it 0600, and it stays in a mode-0700
scratch dir.
Plan-level checks compare ids silently and fail through pytest.fail with an id-free message, so a
failure never prints one. These tests never open a `*.secret.*` file.

test_terraform_tests runs azure/ci/tests/*.tftest.hcl (mocked providers, nothing reaches Azure) from
a copy of the module without the secret tfvars. It needs `./azure/ci/tf.sh init` to have fetched
the providers, and skips with that hint when they're missing.
"""

import fnmatch
import json
import os
import re
import shutil
import subprocess
import uuid
from collections import ChainMap
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
CI = REPO / "azure/ci"
CG = "Microsoft.ContainerInstance/containerGroups"

# The fence's values (plan Task 2, Interfaces). Chris lowered the budget to $85 and the paid cap to
# $80 on 2026-11-05. Task 3 adds the `sweeper` key, which test_fence_values leaves out.
EXPECTED_FENCE = {
    "region": "westus3",
    "ci_group": "rg-realliance-ci",
    "guard_group": "rg-realliance-ci-guard",
    "budget_ci_usd": 85,
    "budget_guard_usd": 5,
    "paid_cap_usd": 80,
    "shapes": [{"cpu": 2, "memory_gb": 8}, {"cpu": 4, "memory_gb": 16}],
    "image_like": "ghcr.io/christopherjmiller/forgejo-runner-class@sha256:*",
    "image_contains": "@sha256:",
    "dropped_rules": [],
    "dispatcher_actions": [
        "Microsoft.ContainerInstance/containerGroups/read",
        "Microsoft.ContainerInstance/containerGroups/write",
        "Microsoft.ContainerInstance/containerGroups/delete",
        "Microsoft.Insights/metrics/read",
    ],
    "egress": {"cap_bytes": 10000000000, "micro_per_gb": 87000, "metric": "NetworkBytesTransmittedPerSecond"},
}

POLICY_FILES = {"types", "location", "aci-shape", "aci-isolation", "aci-image", "tags"}

# E7's isolation rules, one deny condition each. Task 2 Step 1 found every alias on 2026-11-05;
# `identity` has no alias and uses Policy's built-in `identity.type` field. fence.json's `dropped_rules`
# lists a rule whose alias goes missing, and the dispatcher's guard still enforces that rule. The last
# three go beyond E7 (Task 2 review m1, controller's call): a legacy network profile, and a profile or
# standby pool whose compute the shape and image rules never see.
ISOLATION = {
    "ipAddress": {"field": f"{CG}/ipAddress", "exists": "true"},
    "subnetIds": {"count": {"field": f"{CG}/subnetIds[*]"}, "greater": 0},
    "identity": {"allOf": [{"field": "identity.type", "exists": "true"},
                           {"field": "identity.type", "notEquals": "None"}]},
    "osType": {"field": f"{CG}/osType", "notEquals": "Linux"},
    "restartPolicy": {"field": f"{CG}/restartPolicy", "notEquals": "Never"},
    "priority": {"allOf": [{"field": f"{CG}/priority", "exists": "true"},
                           {"field": f"{CG}/priority", "notEquals": "Regular"}]},
    "volumes": {"count": {"field": f"{CG}/volumes[*]"}, "greater": 0},
    "imageRegistryCredentials": {"count": {"field": f"{CG}/imageRegistryCredentials[*]"}, "greater": 0},
    "initContainers": {"count": {"field": f"{CG}/initContainers[*]"}, "greater": 0},
    "networkProfile": {"field": f"{CG}/networkProfile.id", "exists": "true"},
    "containerGroupProfile": {"field": f"{CG}/containerGroupProfile", "exists": "true"},
    "standbyPoolProfile": {"field": f"{CG}/standbyPoolProfile", "exists": "true"},
}

TYPE_GATE = {"field": "type", "equals": CG}

# Every resource instance the plan creates (F30: for_each and count expand). The last seven are
# Task 3's sweeper.
EXPECTED_ADDRESSES = {
    "azurerm_resource_group.ci",
    "azurerm_resource_group.guard",
    "azurerm_consumption_budget_resource_group.ci",
    "azurerm_consumption_budget_resource_group.guard",
    *(f'azurerm_policy_definition.fence["{p}"]' for p in POLICY_FILES),
    "azurerm_policy_set_definition.fence",
    "azurerm_resource_group_policy_assignment.fence",
    "azurerm_role_definition.dispatcher",
    "azurerm_role_assignment.dispatcher",
    "azurerm_role_assignment.dispatcher_cost_reader",
    "azuread_application.dispatcher",
    "azuread_service_principal.dispatcher",
    "time_rotating.dispatcher",
    "azuread_application_password.dispatcher",
    "azapi_resource.sweeper",
    "azurerm_role_definition.sweeper",
    "azurerm_role_assignment.sweeper",
    "azurerm_role_definition.history_reader",
    "azurerm_role_assignment.history_reader",
    "azurerm_monitor_action_group.sweeper_failed",
    "azurerm_monitor_metric_alert.sweeper_failed",
}

SP = "azuread_service_principal.dispatcher.object_id"
SWEEPER_MSI = "azapi_resource.sweeper.identity[0].principal_id"
CI_GROUP = "azurerm_resource_group.ci.id"
WORKFLOW = "azapi_resource.sweeper.id"

# Every role assignment in azure/ci, anywhere in any .tf file: {name: (principal, role, scope)}. A new
# one fails test_role_assignments_allow_listed until it's added here on purpose.
ROLE_ASSIGNMENTS = {
    "dispatcher": (SP, ("role_definition_id", "azurerm_role_definition.dispatcher.role_definition_resource_id"),
                   CI_GROUP),
    "dispatcher_cost_reader": (SP, ("role_definition_name", '"Cost Management Reader"'), CI_GROUP),
    # Task 3: the sweeper's own identity on the CI group, and the dispatcher's read of its run history.
    "sweeper": (SWEEPER_MSI, ("role_definition_id", "azurerm_role_definition.sweeper.role_definition_resource_id"),
                CI_GROUP),
    "history_reader": (SP, ("role_definition_id",
                            "azurerm_role_definition.history_reader.role_definition_resource_id"), WORKFLOW),
}

# Where the dispatcher's service principal may hold any role (E8): the CI group, and the sweeper
# workflow for the history reader.
DISPATCHER_SCOPES = {CI_GROUP, WORKFLOW}

GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


# --- helpers ---------------------------------------------------------------------------------------


def clean_env(**extra: str) -> dict[str, str]:
    """os.environ without GIT_*. Under `git commit`, hooks inherit GIT_DIR and GIT_INDEX_FILE, and a
    `git init` in a scratch dir would then re-initialise this repository (it once set core.bare)."""
    return {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, **extra}


def fence() -> dict:
    return json.loads((CI / "fence.json").read_text())


def tf(name: str) -> str:
    return (CI / name).read_text()


def all_tf() -> str:
    return "\n".join(p.read_text() for p in sorted(CI.glob("*.tf")))


def _body(text: str, start: int) -> str:
    """The text between the `{` that ends at `start` and its matching `}`.

    Skips braces inside strings and `#` comments. Good enough for this module, which has no heredoc.
    """
    depth, i, in_str = 1, start, False
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "#":
            i = text.find("\n", i)
            if i < 0:
                break
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i]
        i += 1
    raise AssertionError("unbalanced braces")


def depth0(body: str) -> str:
    """A block body with its nested blocks' contents dropped, so only its own attributes remain."""
    keep, depth, i, in_str = [], 0, 0, False
    while i < len(body):
        c = body[i]
        if in_str:
            if c == "\\":
                if depth == 0:
                    keep.append(body[i:i + 2])
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "#":
            j = body.find("\n", i)
            i = len(body) if j < 0 else j
            continue
        elif c in "{}":
            depth += 1 if c == "{" else -1
            i += 1
            continue
        if depth == 0:
            keep.append(c)
        i += 1
    return "".join(keep)


def _unique(pat: re.Pattern, text: str, what: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for m in pat.finditer(text):
        # Terraform merges an override file's block into the original, so a second block with the
        # same name changes the first unseen (round 3 m-b). Here it fails instead.
        assert m.group(1) not in found, f'{what} "{m.group(1)}" is declared twice (an override file?)'
        found[m.group(1)] = _body(text, m.end())
    return found


def blocks(text: str, kind: str, rtype: str) -> dict[str, str]:
    """{name: body} for every `kind "rtype" "name" {` block in text; a duplicate name fails."""
    pat = re.compile(rf'^{kind}\s+"{re.escape(rtype)}"\s+"([^"]+)"\s*\{{', re.M)
    return _unique(pat, text, f"{kind} {rtype}")


def block(text: str, kind: str, rtype: str, name: str) -> str:
    found = blocks(text, kind, rtype)
    assert name in found, f'{kind} "{rtype}" "{name}" not found'
    return found[name]


def labelled(text: str, kind: str) -> dict[str, str]:
    """{label: body} for every one-label block (`output "x" {`, `provider "x" {`, `variable "x" {`)."""
    pat = re.compile(rf'^{kind}\s+"([^"]+)"\s*\{{', re.M)
    return _unique(pat, text, kind)


def outputs(text: str) -> dict[str, str]:
    return labelled(text, "output")


def has(body: str, key: str, value: str) -> bool:
    """True when body sets `key = value` on a line of its own (value is literal HCL)."""
    return re.search(rf"^\s*{re.escape(key)}\s*=\s*{re.escape(value)}\s*$", body, re.M) is not None


def policy(name: str) -> dict:
    return json.loads((CI / "policies" / f"{name}.json").read_text())


def canon(items: list) -> list[str]:
    return sorted(json.dumps(i, sort_keys=True) for i in items)


def shape_ok(s: dict) -> dict:
    """One container whose requests are exactly the shape, limits within it, and no GPU."""
    r = f"{CG}/containers[*].resources"
    cpu, mem = s["cpu"], s["memory_gb"]
    return {"allOf": [
        {"field": f"{r}.requests.cpu", "greaterOrEquals": cpu},
        {"field": f"{r}.requests.cpu", "lessOrEquals": cpu},
        {"field": f"{r}.requests.memoryInGB", "greaterOrEquals": mem},
        {"field": f"{r}.requests.memoryInGB", "lessOrEquals": mem},
        {"anyOf": [{"field": f"{r}.limits.cpu", "exists": "false"},
                   {"field": f"{r}.limits.cpu", "lessOrEquals": cpu}]},
        {"anyOf": [{"field": f"{r}.limits.memoryInGB", "exists": "false"},
                   {"field": f"{r}.limits.memoryInGB", "lessOrEquals": mem}]},
        {"field": f"{r}.requests.gpu", "exists": "false"},
        {"field": f"{r}.limits.gpu", "exists": "false"},
    ]}


# --- fence.json ------------------------------------------------------------------------------------


def test_fence_values():
    f = fence()
    f.pop("sweeper", None)
    assert f == EXPECTED_FENCE
    assert f["budget_ci_usd"] == 85 and f["paid_cap_usd"] == 80
    # The budget must exceed the code's period ceiling, or the lock fires on legitimate use (F30).
    assert f["budget_ci_usd"] > f["paid_cap_usd"]
    assert set(f["dropped_rules"]) <= set(ISOLATION)


# --- providers, groups, budgets (layer 8) ----------------------------------------------------------


def test_providers_and_backend():
    main = tf("main.tf")
    assert re.search(r'backend\s+"azurerm"\s*\{[^}]*key\s*=\s*"realliance-ci\.tfstate"', main, re.S)
    provider = labelled(main, "provider")["azurerm"]
    assert has(provider, "resource_provider_registrations", '"none"')
    assert has(provider, "subscription_id", "var.subscription_id")
    assert re.search(r'source\s*=\s*"hashicorp/azuread"\s*\n\s*version\s*=\s*"3\.9\.0"', main)
    assert re.search(r'source\s*=\s*"hashicorp/azurerm"\s*\n\s*version\s*=\s*"~> 4\.0"', main)
    assert re.search(r'source\s*=\s*"hashicorp/time"\s*\n\s*version\s*=\s*"~> 0\.12"', main)
    assert re.search(r'source\s*=\s*"Azure/azapi"\s*\n\s*version\s*=\s*"~> 2\.0"', main)
    # azapi registers nothing either, and runs in the same subscription as azurerm.
    azapi = labelled(main, "provider")["azapi"]
    assert has(azapi, "skip_provider_registration", "true")
    assert has(azapi, "subscription_id", "var.subscription_id")
    assert has(main, "fence", 'jsondecode(file("${path.module}/fence.json"))')
    # Step 1 read Microsoft.ContainerInstance and Microsoft.Logic as Registered, so nothing registers.
    assert "azurerm_resource_provider_registration" not in re.sub(r"#.*", "", all_tf())


def test_resource_groups():
    main = tf("main.tf")
    ci = block(main, "resource", "azurerm_resource_group", "ci")
    guard = block(main, "resource", "azurerm_resource_group", "guard")
    assert has(ci, "name", "local.fence.ci_group") and has(guard, "name", "local.fence.guard_group")
    assert has(ci, "location", "local.fence.region") and has(guard, "location", "local.fence.region")
    # The sweeper alone writes its two-sweeps state into this tag; terraform must not fight it.
    assert has(ci, "ignore_changes", '[tags["ci-sweeper-over-at"]]')


def test_budgets():
    main = tf("main.tf")
    found = blocks(main, "resource", "azurerm_consumption_budget_resource_group")
    assert set(found) == {"ci", "guard"}
    for key, group, amount, name in (("ci", "ci", "budget_ci_usd", "realliance-ci"),
                                     ("guard", "guard", "budget_guard_usd", "realliance-ci-guard")):
        b = found[key]
        assert has(b, "name", f'"{name}"')
        assert has(b, "resource_group_id", f"azurerm_resource_group.{group}.id")
        assert has(b, "amount", f"local.fence.{amount}")
        assert has(b, "time_grain", '"Monthly"')
        thresholds = sorted(int(t) for t in re.findall(r"^\s*threshold\s*=\s*(\d+)\s*$", b, re.M))
        assert thresholds == [80, 100], key
        assert len(re.findall(r"^\s*contact_emails\s*=\s*var\.alert_emails\s*$", b, re.M)) == 2
        # Both notifications on, as literals (review m4): `enabled = false` or a variable fails here.
        assert re.findall(r"^\s*enabled\s*=\s*(.+?)\s*$", b, re.M) == ["true", "true"], key
    # The budgets stop at one end date, which the README's reminder names (review m6).
    ends = set(re.findall(r'^\s*end_date\s*=\s*"(\d{4}-\d{2}-\d{2})T', main, re.M))
    assert len(ends) == 1
    end = ends.pop()
    assert f"## The budgets end on {end}" in (CI / "README.md").read_text()


# --- the dispatcher's identity and role (layer 5) --------------------------------------------------


def test_dispatcher_assignment_named():
    """F6 and review I1: a fixed uuidv5 name, and an id built from it, never read from the resource.

    The output and the sweeper read local.dispatcher_assignment_id, so a restore plan after a lock
    knows the id (the CI group still exists) and changes nothing but the assignment.
    """
    text = tf("fence.tf")
    loc = _body(text, re.search(r"^locals\s*\{", text, re.M).end())
    assert has(loc, "dispatcher_assignment_name", 'uuidv5("url", "realliance-ci/dispatcher-assignment")')
    assert has(loc, "dispatcher_assignment_id", '"${azurerm_resource_group.ci.id}/providers/Microsoft.Authorization'
                                                '/roleAssignments/${local.dispatcher_assignment_name}"')
    body = block(text, "resource", "azurerm_role_assignment", "dispatcher")
    assert has(body, "name", "local.dispatcher_assignment_name")
    assert has(body, "role_definition_id", "azurerm_role_definition.dispatcher.role_definition_resource_id")
    assert has(body, "condition", "lower(self.id) == lower(local.dispatcher_assignment_id)")
    out = outputs(tf("outputs.tf"))["dispatcher_assignment_id"]
    assert has(out, "value", "local.dispatcher_assignment_id")
    # Nothing reads the assignment's computed id, which is unknown in a restore plan.
    assert "azurerm_role_assignment.dispatcher.id" not in re.sub(r"#.*", "", all_tf())


def test_sweeper_takes_dispatcher_assignment_id():
    """The sweeper's template gets the lock's target from the deterministic local (Task 3)."""
    assert re.search(r"dispatcher_assignment_id\s*=\s*local\.dispatcher_assignment_id\b", tf("sweeper.tf"))
    assert template_inputs()["dispatcher_assignment_id"] == "local.dispatcher_assignment_id"


def test_terraform_tests(tmp_path):
    """azure/ci/tests/*.tftest.hcl with mocked providers (review I1).

    The restore scenario: with the CI group's id known and the assignment's unknown, plan time
    already knows the lock's target, and the sweeper's whole workflow body (Task 3), so a restore
    plan leaves the sweeper unchanged. The postcondition accepts the deterministic id in any case
    and refuses another. Runs from a copy without ci.secret.auto.tfvars, which terraform test would
    otherwise load.
    """
    data = CI / ".terraform"
    if not (data / "providers").is_dir():
        pytest.skip("run ./azure/ci/tf.sh init first: terraform test needs the providers")
    mod = tmp_path / "ci"
    mod.mkdir()
    for src in [*CI.glob("*.tf"), CI / "fence.json", CI / ".terraform.lock.hcl"]:
        shutil.copy2(src, mod / src.name)
    for d in ("policies", "tests", "sweeper"):
        shutil.copytree(CI / d, mod / d)
    assert list((mod / "tests").glob("*.tftest.hcl"))
    r = subprocess.run(["terraform", "test", "-no-color"], cwd=mod, capture_output=True, text=True,
                       env=clean_env(TF_DATA_DIR=str(data)))
    if r.returncode != 0:
        pytest.fail("terraform test failed:\n" + GUID.sub("<id>", r.stdout + r.stderr)[-4000:])
    assert "0 failed" in r.stdout


def test_dispatcher_role_exact():
    f = fence()
    body = block(tf("fence.tf"), "resource", "azurerm_role_definition", "dispatcher")
    assert has(body, "name", '"Realliance CI dispatcher"')
    assert has(body, "scope", "azurerm_resource_group.ci.id")
    assert has(body, "actions", "local.fence.dispatcher_actions")
    for empty in ("not_actions", "data_actions", "not_data_actions"):
        assert has(body, empty, "[]"), empty
    assert has(body, "assignable_scopes", "[azurerm_resource_group.ci.id]")
    # Exactly one permissions block, written out (review I2): a second block or a dynamic one could
    # widen the role past these lines.
    assert len(re.findall(r"^\s*permissions\s*\{", body, re.M)) == 1
    assert not re.search(r'dynamic\s+"permissions"', body)
    # E8: no exec, logs, start, stop, Authorization or wildcard right; compute writes stay in ACI.
    for a in f["dispatcher_actions"]:
        assert "*" not in a and not a.startswith("Microsoft.Authorization/"), a
        assert not re.search(r"/(exec|logs|start|stop)\b", a, re.I), a


def test_password_lifetime():
    """F26: 150-day rotation inside a 180-day credential (the spec's 6 months at most)."""
    text = tf("fence.tf")
    rot = block(text, "resource", "time_rotating", "dispatcher")
    assert has(rot, "rotation_days", "150")
    pw = block(text, "resource", "azuread_application_password", "dispatcher")
    assert has(pw, "end_date", 'timeadd(time_rotating.dispatcher.id, "4320h")')
    assert 4320 / 24 == 180
    assert re.search(r"rotate_when_changed\s*=\s*\{\s*rotation\s*=\s*time_rotating\.dispatcher\.id\s*\}", pw)
    assert has(pw, "application_id", "azuread_application.dispatcher.id")


def test_assignments_scoped_to_ci_group():
    """fence.tf and sweeper.tf grant nothing at subscription or management-group scope."""
    for name in ("fence.tf", "sweeper.tf"):
        code = re.sub(r"#.*", "", tf(name))
        for bad in ("/subscriptions/", "azurerm_subscription", "subscription_id", "management_group"):
            assert bad not in code, (name, bad)


def test_role_assignments_allow_listed():
    """Review I2: every role assignment in every .tf file is in ROLE_ASSIGNMENTS, exactly.

    The dispatcher's service principal holds roles only at DISPATCHER_SCOPES. An assignment
    declared in main.tf, or any new one, fails until it's added on purpose.
    """
    check_role_assignments(re.sub(r"#.*", "", all_tf()))
    # terraform also loads *.tf.json, which all_tf() doesn't read; there must be none.
    assert not list(CI.glob("*.tf.json"))


OVERRIDE = re.compile(r"(^|_)override\.tf(\.json)?$")


def override_files(root: Path) -> list[str]:
    """Terraform override files under root: override.tf, *_override.tf, and their .tf.json forms."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                  if p.is_file() and ".terraform" not in p.relative_to(root).parts and OVERRIDE.search(p.name))


def test_no_override_files():
    """Round 3 m-b: an override file merges into a block the allow-list already passed, so none may exist."""
    assert override_files(CI) == []


def test_override_scan_catches(tmp_path):
    names = ["override.tf", "a_override.tf", "dispatcher_override.tf", "x_override.tf.json", "override.tf.json",
             "tests/deep_override.tf"]
    for n in names + ["overrides.tf", "notoverride.tf", "override.tftpl"]:
        (tmp_path / n).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / n).write_text("")
    assert override_files(tmp_path) == sorted(names)


OVERRIDE_SNIPPET = 'resource "azurerm_role_assignment" "dispatcher" {\n  scope = "/subscriptions/${var.subscription_id}"\n}\n'


@pytest.mark.parametrize("where", ["before", "after"])
def test_override_block_fails_the_allow_list(where):
    """The reviewer's case: a dispatcher_override.tf (sorting before or after fence.tf) holding only
    a subscription scope for the dispatcher's assignment. The duplicate block fails the scan."""
    code = re.sub(r"#.*", "", all_tf())
    code = OVERRIDE_SNIPPET + code if where == "before" else code + OVERRIDE_SNIPPET
    with pytest.raises(AssertionError, match="declared twice"):
        check_role_assignments(code)


def check_role_assignments(code: str) -> None:
    found = blocks(code, "resource", "azurerm_role_assignment")
    assert set(found) == set(ROLE_ASSIGNMENTS)
    for name, body in found.items():
        principal, (role_key, role), scope = ROLE_ASSIGNMENTS[name]
        assert has(body, "principal_id", principal), name
        assert has(body, role_key, role), name
        assert has(body, "scope", scope), name
        assert not re.search(r"^\s*(count|for_each)\s*=", body, re.M), name
        assert has(body, "principal_type", '"ServicePrincipal"'), name
        if principal == SP:
            assert scope in DISPATCHER_SCOPES, name
        # Only the sweeper's assignment carries a condition: the ABAC limit on its delete right.
        assert ("condition" in re.findall(r"^\s*(\w+)\s*=", depth0(body), re.M)) == (name == "sweeper"), name
    assert grant_findings(code) == []


# Resource types that grant rights or can create a role assignment that ROLE_ASSIGNMENTS never
# sees: Graph roles and permissions, PIM, and ARM template deployments (review I2, round 2 n3).
GRANTING_TYPES = re.compile(
    r"azuread_app_role_assignment|azuread_directory_role_assignment|azuread_directory_role_member"
    r"|azuread_service_principal_delegated_permission_grant|azurerm_role_management_policy|azurerm_pim_\w+"
    r"|azurerm_(resource_group|subscription|management_group|tenant)_template_deployment|azurerm_template_deployment"
)

# The lines of code that may name Microsoft.Authorization: the lock target's id (fence.tf), and the
# ABAC condition that limits the sweeper's delete right to the dispatcher's role (sweeper.tf).
SWEEPER_LOCK_CONDITION = (
    "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR "
    "(@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals "
    "{${azurerm_role_definition.dispatcher.role_definition_id}}))")
AUTHORIZATION_OK = {
    'dispatcher_assignment_id = "${azurerm_resource_group.ci.id}/providers/'
    'Microsoft.Authorization/roleAssignments/${local.dispatcher_assignment_name}"',
    f'sweeper_lock_condition = "{SWEEPER_LOCK_CONDITION}"',
}

# azapi can PUT, PATCH or POST anything ARM takes, so its only use is the one workflow (Task 3).
AZAPI_OK = {("azapi_resource", "sweeper"): "Microsoft.Logic/workflows@2019-05-01"}


def grant_findings(code: str) -> list[str]:
    """Every construct in code (comments stripped) that could grant a right outside ROLE_ASSIGNMENTS."""
    found = [f"resource {m.group(1)}" for m in re.finditer(r'^\s*resource\s+"([^"]+)"', code, re.M)
             if GRANTING_TYPES.fullmatch(m.group(1))]
    # Any azapi resource or data source other than the sweeper's workflow, or that workflow with
    # another type.
    for m in re.finditer(r'^\s*(resource|data|ephemeral)\s+"(azapi_\w+)"\s+"([^"]+)"\s*\{', code, re.M):
        want = AZAPI_OK.get((m.group(2), m.group(3))) if m.group(1) == "resource" else None
        types = re.findall(r'^\s*type\s*=\s*"([^"]*)"\s*$', depth0(_body(code, m.end())), re.M)
        if want is None or types != [want]:
            found.append(f"azapi {m.group(1)} {m.group(2)}.{m.group(3)}")
    found += [f"module {m.group(1)}" for m in re.finditer(r'^\s*module\s+"([^"]+)"', code, re.M)]
    # A shell-out can run `az role assignment create` behind terraform's back.
    found += [f"provisioner {m.group(1)}" for m in re.finditer(r'^\s*provisioner\s+"([^"]+)"', code, re.M)]
    found += ["data external" for _ in re.finditer(r'^\s*data\s+"external"', code, re.M)]
    # azapi (any resource type) against Authorization or ARM deployments.
    found += [f"azapi type {m.group(1)}" for m in
              re.finditer(r'type\s*=\s*"(Microsoft\.(?:Authorization|Resources/deployments)[^"]*)"', code, re.I)]
    for line in code.splitlines():
        if "microsoft.authorization" in line.lower() and " ".join(line.split()) not in AUTHORIZATION_OK:
            found.append("Microsoft.Authorization outside the lock target's id")
    if "required_resource_access" in code:
        found.append("Graph permissions (required_resource_access)")
    return found


GRANT_SNIPPETS = {
    "rg template deployment": 'resource "azurerm_resource_group_template_deployment" "x" {\n  name = "x"\n}\n',
    "subscription template deployment": 'resource "azurerm_subscription_template_deployment" "x" {\n}\n',
    "azapi role assignment": ('resource "azapi_resource" "x" {\n'
                              '  type = "Microsoft.Authorization/roleAssignments@2022-04-01"\n}\n'),
    "azapi deployment": 'resource "azapi_resource" "x" {\n  type = "Microsoft.Resources/deployments@2021-04-01"\n}\n',
    "azapi action on an assignment": ('resource "azapi_resource_action" "x" {\n'
                                      '  resource_id = "${azurerm_resource_group.ci.id}/providers/'
                                      'Microsoft.Authorization/roleAssignments/x"\n}\n'),
    "module": 'module "grants" {\n  source = "./grants"\n}\n',
    "local-exec": 'resource "terraform_data" "x" {\n  provisioner "local-exec" {\n    command = "true"\n  }\n}\n',
    "data external": 'data "external" "x" {\n  program = ["true"]\n}\n',
    "Graph app role": 'resource "azuread_app_role_assignment" "x" {\n}\n',
    "Graph permission": 'resource "azuread_application" "y" {\n  required_resource_access {\n  }\n}\n',
    # Task 3: azapi beyond the one workflow.
    "azapi second resource": 'resource "azapi_resource" "x" {\n  type = "Microsoft.Logic/workflows@2019-05-01"\n}\n',
    "azapi update": 'resource "azapi_update_resource" "x" {\n  type = "Microsoft.Logic/workflows@2019-05-01"\n}\n',
    "azapi action": 'resource "azapi_resource_action" "x" {\n  type = "Microsoft.Logic/workflows@2019-05-01"\n}\n',
    "azapi data source": 'data "azapi_resource" "x" {\n  type = "Microsoft.Logic/workflows@2019-05-01"\n}\n',
    "a second ABAC-looking line": 'locals {\n  x = "Microsoft.Authorization/roleAssignments/write"\n}\n',
}


def test_grant_scan_catches_a_retyped_sweeper():
    """The workflow keeps its one type; any other type on azapi_resource.sweeper is a finding."""
    code = re.sub(r"#.*", "", all_tf())
    assert grant_findings(code) == []
    retyped = re.sub(r'(\btype\s*=\s*)"Microsoft\.Logic/workflows@2019-05-01"',
                     r'\1"Microsoft.Authorisation/roleAssignments@2022-04-01"', code)
    assert retyped != code
    assert "azapi resource azapi_resource.sweeper" in grant_findings(retyped)


@pytest.mark.parametrize("name", sorted(GRANT_SNIPPETS))
def test_grant_scan_catches(name):
    """Round 2 n3: each granting construct, added to the real code, is a finding."""
    code = re.sub(r"#.*", "", all_tf())
    assert grant_findings(code) == []
    assert grant_findings(code + "\n" + GRANT_SNIPPETS[name])


def test_outputs_sensitive():
    out = outputs(tf("outputs.tf"))
    assert set(out) == {"dispatcher_client_id", "tenant_id", "dispatcher_client_secret",
                        "dispatcher_assignment_id"}
    for name, body in out.items():
        assert has(body, "sensitive", "true"), name


# --- Policy (layer 5) ------------------------------------------------------------------------------


def test_policies_deny():
    f = fence()
    assert {p.stem for p in (CI / "policies").glob("*.json")} == POLICY_FILES
    for name in POLICY_FILES:
        p = policy(name)
        assert set(p) == {"displayName", "description", "mode", "policyRule"}, name
        assert p["policyRule"]["then"] == {"effect": "deny"}, name
        assert p["mode"] == "Indexed", name

    assert policy("types")["policyRule"]["if"] == {"field": "type", "notIn": [CG]}
    assert policy("location")["policyRule"]["if"] == {"field": "location", "notIn": [f["region"]]}

    shape = policy("aci-shape")["policyRule"]["if"]
    assert shape["allOf"][0] == TYPE_GATE and len(shape["allOf"]) == 2
    assert canon(shape["allOf"][1]["anyOf"]) == canon([
        {"count": {"field": f"{CG}/containers[*]"}, "notEquals": 1},
        {"count": {"field": f"{CG}/containers[*]", "where": {"anyOf": [shape_ok(s) for s in f["shapes"]]}},
         "notEquals": 1},
        {"allOf": [{"field": f"{CG}/sku", "exists": "true"}, {"field": f"{CG}/sku", "notEquals": "Standard"}]},
    ])

    iso = policy("aci-isolation")["policyRule"]["if"]
    assert iso["allOf"][0] == TYPE_GATE and len(iso["allOf"]) == 2
    kept = [ISOLATION[k] for k in ISOLATION if k not in f["dropped_rules"]]
    assert canon(iso["allOf"][1]["anyOf"]) == canon(kept)

    assert f["image_like"].count("*") == 1 and f["image_like"].endswith("@sha256:*")
    assert policy("aci-image")["policyRule"]["if"] == {"allOf": [TYPE_GATE, {
        "count": {"field": f"{CG}/containers[*]", "where": {"anyOf": [
            {"field": f"{CG}/containers[*].image", "notLike": f["image_like"]},
            {"field": f"{CG}/containers[*].image", "notContains": f["image_contains"]},
        ]}},
        "greater": 0,
    }]}

    assert policy("tags")["policyRule"]["if"] == {"anyOf": [
        {"field": "tags['forge-ci']", "exists": "false"},
        {"field": "tags['expires-at']", "exists": "false"},
    ]}


def like(value: str, pattern: str) -> bool:
    """Azure Policy's `like`: case-insensitive, `*` is the only wildcard."""
    return re.fullmatch(".*".join(re.escape(p) for p in pattern.split("*")), value, re.I) is not None


def test_image_rule_admits_digest_form_only():
    """Review m2 (controller's call): only `forgejo-runner-class@sha256:…`, the form the dispatcher
    sends (F16). Evaluates the rule in policies/aci-image.json against sample images."""
    where = policy("aci-image")["policyRule"]["if"]["allOf"][1]["count"]["where"]["anyOf"]
    pattern = next(c["notLike"] for c in where if "notLike" in c)
    needle = next(c["notContains"] for c in where if "notContains" in c)

    def denied(image: str) -> bool:
        return not like(image, pattern) or needle not in image

    digest = "@sha256:" + "0" * 64
    repo = "ghcr.io/christopherjmiller/forgejo-runner-class"
    assert not denied(repo + digest)
    for image in (
        repo + ":sha-1234567" + digest,  # tag and digest: ACI refuses it, and the fence does too
        repo + ":latest",
        repo,
        repo + "-evil" + digest,  # another package in the same namespace
        "ghcr.io/christopherjmiller/forgejo-runner" + digest,
        "ghcr.io/someone-else/forgejo-runner-class" + digest,
        "docker.io/library/alpine" + digest,
    ):
        assert denied(image), image


def test_initiative_assigned_to_ci_group():
    text = tf("fence.tf")
    defs = block(text, "resource", "azurerm_policy_definition", "fence")
    assert has(defs, "for_each", "local.policies")
    assert has(defs, "policy_rule", "jsonencode(each.value.policyRule)")
    assert has(defs, "mode", "each.value.mode")
    init = block(text, "resource", "azurerm_policy_set_definition", "fence")
    assert has(init, "name", '"realliance-ci-fence"')
    assert has(init, "for_each", "azurerm_policy_definition.fence")
    a = block(text, "resource", "azurerm_resource_group_policy_assignment", "fence")
    assert has(a, "resource_group_id", "azurerm_resource_group.ci.id")
    assert has(a, "policy_definition_id", "azurerm_policy_set_definition.fence.id")
    assert has(a, "enforce", "true")


# --- the sweeper (layer 7, Task 3) -----------------------------------------------------------------
#
# The tests render sweeper/workflow.json.tftpl the way sweeper.tf does, with stand-ins for the three
# ids, then check it two ways: statically (structure, values, references) and by running it in a
# small simulator of the Logic Apps runtime against a fake ARM. The simulator is deliberately
# strict where the runtime's behaviour is uncertain: it evaluates every function argument eagerly,
# refuses equals() between an integer and a float, and fails on outputs of a skipped action. A
# template that runs here doesn't lean on any of those.

TPL = CI / "sweeper/workflow.json.tftpl"
ARM = "https://management.azure.com"
MSI = {"type": "ManagedServiceIdentity", "audience": "https://management.azure.com/"}
RETRY = {"type": "fixed", "count": 1, "interval": "PT5S"}

# Stand-ins for the ids sweeper.tf passes in: id-shaped, GUID-free, and never real.
SUB = "/subscriptions/sub-test"
CI_ID = f"{SUB}/resourceGroups/rg-realliance-ci"
LOCK_ID = f"{CI_ID}/providers/Microsoft.Authorization/roleAssignments/dispatcher-test"
PLACEHOLDERS = {
    "azurerm_resource_group.ci.id": CI_ID,
    "local.dispatcher_assignment_id": LOCK_ID,
    "azurerm_consumption_budget_resource_group.ci.name": "realliance-ci",
}
ACI_LIST = f"{CI_ID}/providers/{CG}"
TAGS = f"{CI_ID}/providers/Microsoft.Resources/tags/default"
BUDGET = f"{CI_ID}/providers/Microsoft.Consumption/budgets/realliance-ci"

# The brief's sweeper block (plan Task 3, Interfaces).
EXPECTED_SWEEPER = {
    "interval_minutes": 10, "max_age_minutes": 90, "ceiling_micro_per_hour": 600000,
    "repeat_window_minutes": 12, "action_timeout": "PT1M",
    "price_micro_per_hour": {"2/8": 117000, "4/16": 233000, "other": 1000000},
    "delete_types": [CG],
    "actions": [f"{CG}/read", f"{CG}/delete",
                "Microsoft.Resources/subscriptions/resourceGroups/read",
                "Microsoft.Resources/subscriptions/resourceGroups/resources/read",
                "Microsoft.Resources/tags/read", "Microsoft.Resources/tags/write", "Microsoft.Consumption/budgets/read",
                "Microsoft.Authorization/roleAssignments/read", "Microsoft.Authorization/roleAssignments/delete",
                "Microsoft.Insights/metrics/read"],
    "egress_rate_window_minutes": 10,
}


def template_inputs() -> dict[str, str]:
    """{name: HCL expression} of the map sweeper.tf hands templatefile()."""
    text = tf("sweeper.tf")
    m = re.search(r'templatefile\(\s*"\$\{path\.module\}/sweeper/workflow\.json\.tftpl"\s*,\s*\{', text)
    assert m, "sweeper.tf must render sweeper/workflow.json.tftpl with templatefile()"
    pairs = re.findall(r"^\s*(\w+)\s*=\s*(.+?)\s*$", _body(text, m.end()), re.M)
    assert len({k for k, _ in pairs}) == len(pairs), "a template input is passed twice"
    return dict(pairs)


def tf_value(expr: str):
    """One template input's value, for the little HCL sweeper.tf may use there."""
    if expr in PLACEHOLDERS:
        return PLACEHOLDERS[expr]
    m = re.fullmatch(r"one\((.+)\)", expr)
    if m:
        v = tf_value(m.group(1))
        assert isinstance(v, list) and len(v) == 1, f"{expr}: one() takes exactly one element"
        return v[0]
    m = re.fullmatch(r'local\.(fence|sweeper)((?:\.\w+|\["[^"]+"\])*)', expr)
    if m:
        v = fence() if m.group(1) == "fence" else fence()["sweeper"]
        for dot, key in re.findall(r'\.(\w+)|\["([^"]+)"\]', m.group(2)):
            v = v[dot or key]
        return v
    raise AssertionError(f"sweeper.tf passes {expr!r} to the template, which this test can't evaluate")


def render(inputs: dict | None = None) -> dict:
    """The workflow definition, rendered as templatefile() would, with PLACEHOLDERS for the ids."""
    tpl = TPL.read_text()
    vals = inputs if inputs is not None else {k: tf_value(e) for k, e in template_inputs().items()}
    assert "%{" not in tpl, "the template uses no directives"
    used = set(re.findall(r"\$\{(\w+)\}", tpl))
    assert used == set(vals), f"template inputs unused or missing: {sorted(used ^ set(vals))}"
    for v in vals.values():
        assert isinstance(v, str) or (isinstance(v, int) and not isinstance(v, bool)), v
    out = re.sub(r"\$\{(\w+)\}", lambda m: str(vals[m.group(1)]), tpl)
    assert "${" not in out
    return json.loads(out)


def definition() -> dict:
    return render()


def walk(actions: dict, path: tuple = ()):
    """(path, action) for every action at any depth: If branches and Foreach bodies included."""
    for name, a in actions.items():
        yield path + (name,), a
        yield from walk(a.get("actions", {}), path + (name,))
        yield from walk(a.get("else", {}).get("actions", {}), path + (name, "else"))


def find(d: dict, name: str) -> dict:
    hits = [a for p, a in walk(d["actions"]) if p[-1] == name]
    assert len(hits) == 1, f"{name}: {len(hits)} actions"
    return hits[0]


def http_actions(d: dict) -> list[tuple[tuple, dict]]:
    return [(p, a) for p, a in walk(d["actions"]) if a["type"] == "Http"]


def strings(v) -> list[str]:
    """Every string anywhere inside v."""
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [s for x in v.values() for s in strings(x)]
    if isinstance(v, list):
        return [s for x in v for s in strings(x)]
    return []


# --- a Logic Apps expression evaluator (the subset the template uses) ---

class WdlError(Exception):
    """An expression the Logic Apps runtime would fail on, or might: the simulator assumes the worst."""


_TOK = re.compile(r"\s*(?:(?P<num>-?\d+(?:\.\d+)?)|(?P<str>'(?:[^']|'')*')|(?P<name>[A-Za-z_]\w*)"
                  r"|(?P<op>\?\[|[(),\[\]]))")


def wdl_parse(src: str):
    """Parse one expression into ('lit', v) | ('call', name, [args]) | ('index', target, key, safe)."""
    toks, pos, src = [], 0, src.rstrip()
    while pos < len(src):
        m = _TOK.match(src, pos)
        assert m and m.end() > pos, f"can't read the expression at {src[pos:pos + 40]!r}"
        toks.append((m.lastgroup, m.group(m.lastgroup)))
        pos = m.end()
    i = 0

    def peek():
        return toks[i][1] if i < len(toks) else None

    def take(want=None):
        nonlocal i
        assert i < len(toks), f"expression ends early: {src!r}"
        kind, val = toks[i]
        assert want is None or val == want, f"expected {want!r}, got {val!r} in {src!r}"
        i += 1
        return kind, val

    def expr():
        kind, val = take()
        if kind == "num":
            node = ("lit", float(val) if "." in val else int(val))
        elif kind == "str":
            node = ("lit", val[1:-1].replace("''", "'"))
        elif kind == "name" and peek() == "(":
            take("(")
            args = []
            if peek() != ")":
                args.append(expr())
                while peek() == ",":
                    take(",")
                    args.append(expr())
            take(")")
            node = ("call", val, args)
        elif kind == "name" and val in ("true", "false", "null"):
            node = ("lit", {"true": True, "false": False, "null": None}[val])
        else:
            raise AssertionError(f"unexpected {val!r} in {src!r}")
        while peek() in ("?[", "["):
            safe = take()[1] == "?["
            key = expr()
            take("]")
            node = ("index", node, key, safe)
        return node

    node = expr()
    assert i == len(toks), f"trailing input in {src!r}"
    return node


def _num(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise WdlError(f"not a number: {x!r}")
    return x


def _cmp(op):
    def f(a, b):
        if not (isinstance(a, str) and isinstance(b, str)):
            _num(a), _num(b)
        return op(a, b)
    return f


def _equals(a, b):
    if (isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool)
            and not isinstance(b, bool) and type(a) is not type(b)):
        raise WdlError(f"equals({a!r}, {b!r}): an integer against a float")
    return a == b


def _div(a, b):
    _num(a), _num(b)
    if b == 0:
        raise WdlError("division by zero")
    if isinstance(a, int) and isinstance(b, int):  # integer division truncates, as in .NET
        q = abs(a) // abs(b)
        return q if (a >= 0) == (b > 0) else -q
    return a / b


def _bool(x):
    if not isinstance(x, bool):
        raise WdlError(f"not a boolean: {x!r}")
    return x


def _typed(t, f):
    def g(x, *rest):
        if not isinstance(x, t):
            raise WdlError(f"{f.__name__}: wrong type {type(x).__name__}")
        return f(x, *rest)
    return g


def _first(x):
    if isinstance(x, list):
        return x[0] if x else None
    if isinstance(x, str):
        return x[:1]
    raise WdlError(f"first() of {type(x).__name__}")


def _contains(c, v):
    if isinstance(c, (list, dict)) or (isinstance(c, str) and isinstance(v, str)):
        return v in c
    raise WdlError("contains() of a wrong type")


def _string(x):
    if isinstance(x, bool) or not isinstance(x, (str, int, float)):
        raise WdlError(f"string() of {x!r}")
    return str(x)


_ISO = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{1,7}))?Z")
_EPOCH = datetime(1, 1, 1)


def ticks(s) -> int:
    m = _ISO.fullmatch(s) if isinstance(s, str) else None
    if not m:
        raise WdlError(f"not an ISO 8601 UTC time: {s!r}")
    dt = datetime(*map(int, m.groups()[:6]))
    return (dt - _EPOCH) // timedelta(microseconds=1) * 10 + int((m.group(7) or "").ljust(7, "0"))


def iso(t: int) -> str:
    """The runtime's own format, as utcNow() and addMinutes() return it."""
    return (_EPOCH + timedelta(microseconds=t // 10)).strftime("%Y-%m-%dT%H:%M:%S") + f".{t % 10_000_000:07d}Z"


def _format(s, fmt):
    assert fmt == "yyyy-MM-ddTHH:mm:ssZ", f"the simulator knows one date format, not {fmt!r}"
    return iso(ticks(s))[:19] + "Z"


MINUTE = 600_000_000  # ticks
WDL = {
    "if": lambda c, a, b: a if _bool(c) else b,
    "and": lambda *xs: all([_bool(x) for x in xs]),
    "or": lambda *xs: any([_bool(x) for x in xs]),
    "not": lambda x: not _bool(x),
    "equals": _equals,
    "greater": _cmp(lambda a, b: a > b),
    "greaterOrEquals": _cmp(lambda a, b: a >= b),
    "less": _cmp(lambda a, b: a < b),
    "lessOrEquals": _cmp(lambda a, b: a <= b),
    "coalesce": lambda *xs: next((x for x in xs if x is not None), None),
    "first": _first,
    "length": _typed((list, str), len),
    "json": _typed(str, json.loads),
    "toLower": _typed(str, str.lower),
    "contains": _contains,
    "add": lambda a, b: _num(a) + _num(b),
    "sub": lambda a, b: _num(a) - _num(b),
    "mul": lambda a, b: _num(a) * _num(b),
    "div": _div,
    "string": _string,
    "float": _typed(str, float),
    "concat": lambda *xs: "".join(_typed(str, str)(x) for x in xs),
    "join": lambda xs, sep: sep.join(_typed(str, str)(x) for x in _typed(list, list)(xs)),
    "ticks": ticks,
    "addMinutes": lambda s, n: iso(ticks(s) + _num(n) * MINUTE),
    "formatDateTime": _format,
}

def interpolations(v: str) -> list[tuple[int, int]]:
    """(start, end) of each '@{...}' in a workflow string; braces inside '...' literals don't count."""
    spans, i = [], 0
    while (j := v.find("@{", i)) >= 0:
        k, depth, quoted = j + 2, 1, False
        while depth:
            assert k < len(v), f"unbalanced @{{ in {v!r}"
            c = v[k]
            if c == "'":
                quoted = not quoted
            elif not quoted and c in "{}":
                depth += 1 if c == "{" else -1
            k += 1
        spans.append((j, k))
        i = k
    return spans


TIMEOUT = object()  # a fake ARM answer: the request hung past limit.timeout
NOW = "2026-11-05T12:00:00.0000000Z"


class Terminated(Exception):
    def __init__(self, status: str, code: str):
        super().__init__(status, code)
        self.status, self.code = status, code


class Run:
    """One run of the workflow against `respond(method, uri, queries, body) -> (code, body) | TIMEOUT`.

    runAfter, If, Foreach, variables and Terminate as the runtime documents them. A scope (the run,
    a branch, one loop iteration) fails when an action in it failed or timed out and no later action
    in the same scope runs after that status: an unhandled failure, as in a try/catch.
    """

    def __init__(self, d: dict, respond, now: str = NOW):
        self.d, self.respond, self.now = d, respond, now
        self.vars: dict = {}
        self.results = ChainMap()
        self.loops: dict = {}
        self.item = None

    def go(self) -> dict:
        try:
            return {"status": self.scope(self.d["actions"]), "code": None}
        except Terminated as t:
            return {"status": t.status, "code": t.code}

    # expressions

    def ev(self, node):
        kind = node[0]
        if kind == "lit":
            return node[1]
        if kind == "index":
            target, key, safe = self.ev(node[1]), self.ev(node[2]), node[3]
            if target is None:
                if safe:
                    return None
                raise WdlError("property of null")
            if isinstance(target, dict):
                if key in target:
                    return target[key]
                if safe:
                    return None
                raise WdlError(f"no property {key!r}")
            if isinstance(target, list) and isinstance(key, int) and not isinstance(key, bool):
                if 0 <= key < len(target):
                    return target[key]
                raise WdlError("index out of range")
            raise WdlError(f"can't index {type(target).__name__} with {key!r}")
        name, args = node[1], [self.ev(a) for a in node[2]]
        if name in WDL:
            try:
                return WDL[name](*args)
            except (TypeError, ValueError, KeyError) as e:
                raise WdlError(f"{name}{tuple(args)!r}: {e}") from e
        return self.ref(name, args)

    def ref(self, name, args):
        if name == "utcNow":
            return self.now
        if name == "variables":
            return self.vars[args[0]]
        if name == "items":
            return self.loops[args[0]]
        if name == "item":
            return self.item
        r = self.results[args[0]]  # KeyError: referenced before it ran (test_references_are_upstream)
        if name == "actions":
            return {"status": r["status"], "outputs": r.get("outputs")}
        if r["status"] == "Skipped":
            raise WdlError(f"outputs of skipped action {args[0]}")
        if name == "outputs":
            return r.get("outputs")
        if name == "body":
            return r.get("body")
        raise AssertionError(f"the simulator doesn't know {name}()")

    def value(self, v):
        """A workflow value: '@expr' keeps its type, '@{expr}' interpolates into a string, '@@' escapes."""
        if isinstance(v, dict):
            return {k: self.value(x) for k, x in v.items()}
        if isinstance(v, list):
            return [self.value(x) for x in v]
        if not isinstance(v, str):
            return v
        if v.startswith("@@"):
            return v[1:]
        if v.startswith("@") and not v.startswith("@{"):
            return self.ev(wdl_parse(v[1:]))
        out, i = [], 0
        for j, k in interpolations(v):
            x = self.ev(wdl_parse(v[j + 2:k - 1]))
            out += [v[i:j], "" if x is None else _string(x)]
            i = k
        return "".join(out) + v[i:]

    def condition(self, e) -> bool:
        if isinstance(e, str):
            return _bool(self.value(e))
        (op, arg), = e.items()
        if op in ("and", "or"):
            got = [self.condition(x) for x in arg]  # eager, like every other function here
            return all(got) if op == "and" else any(got)
        if op == "not":
            return not self.condition(arg)
        a, b = (self.value(x) for x in arg)
        return {"equals": _equals, "contains": _contains, **{k: WDL[k] for k in
                ("greater", "greaterOrEquals", "less", "lessOrEquals")}}[op](a, b)

    # actions

    def scope(self, actions: dict) -> str:
        status, pending = {}, dict(actions)
        while pending:
            ready = [n for n, a in pending.items() if all(p in status for p in a.get("runAfter", {}))]
            assert ready, f"runAfter names an unknown or circular predecessor among {sorted(pending)}"
            for n in ready:
                a = pending.pop(n)
                ok = all(status[p] in want for p, want in a.get("runAfter", {}).items())
                status[n] = self.action(n, a) if ok else "Skipped"
                if not ok:
                    self.results[n] = {"status": "Skipped"}
        for n, st in status.items():
            if st in ("Failed", "TimedOut") and not any(st in b.get("runAfter", {}).get(n, []) for b in actions.values()):
                return "Failed"
        return "Succeeded"

    def action(self, name: str, a: dict) -> str:
        kind = a["type"]
        rec: dict = {"status": "Succeeded"}
        try:
            if kind == "Http":
                inp = self.value(a["inputs"])
                r = self.respond(inp["method"], inp["uri"], inp.get("queries", {}), inp.get("body"))
                if r is TIMEOUT:
                    rec = {"status": "TimedOut", "outputs": None, "body": None}
                else:
                    code, body = r
                    rec = {"status": "Succeeded" if 200 <= code < 300 else "Failed",
                           "outputs": {"statusCode": code, "body": body}, "body": body}
            elif kind == "Compose":
                v = self.value(a["inputs"])
                rec.update(outputs=v, body=v)
            elif kind == "Query":
                src = self.value(a["inputs"]["from"])
                if not isinstance(src, list):
                    raise WdlError("Query from a non-array")
                kept = []
                for self.item in src:
                    if _bool(self.value(a["inputs"]["where"])):
                        kept.append(self.item)
                self.item = None
                rec.update(outputs={"body": kept}, body=kept)
            elif kind == "InitializeVariable":
                (var,) = a["inputs"]["variables"]
                want = {"float": (int, float), "integer": int, "array": list, "string": str}[var["type"]]
                assert isinstance(var["value"], want), var
                self.vars[var["name"]] = list(var["value"]) if var["type"] == "array" else var["value"]
            elif kind == "IncrementVariable":
                self.vars[a["inputs"]["name"]] = _num(self.vars[a["inputs"]["name"]]) + _num(self.value(a["inputs"]["value"]))
            elif kind == "AppendToArrayVariable":
                self.vars[a["inputs"]["name"]].append(self.value(a["inputs"]["value"]))
            elif kind == "Terminate":
                inp = self.value(a["inputs"])
                raise Terminated(inp["runStatus"], inp.get("runError", {}).get("code"))
            elif kind == "If":
                branch = a.get("actions", {}) if self.condition(a["expression"]) else a.get("else", {}).get("actions", {})
                rec["status"] = self.scope(branch)
            elif kind == "Foreach":
                src = self.value(a["foreach"])
                if not isinstance(src, list):
                    raise WdlError("Foreach over a non-array")
                worst = "Succeeded"
                for it in src:
                    self.loops[name] = it
                    self.results = self.results.new_child()
                    try:
                        if self.scope(a["actions"]) == "Failed":
                            worst = "Failed"
                    finally:
                        self.results = self.results.parents
                self.loops.pop(name, None)
                rec["status"] = worst
            else:
                raise AssertionError(f"the simulator doesn't run {kind}")
        except WdlError as e:
            rec = {"status": "Failed", "outputs": None, "body": None, "error": str(e)}
        self.results[name] = rec
        return rec["status"]


# --- a fake ARM for the sweeper's requests ---

def ago(minutes: float) -> str:
    return iso(ticks(NOW) - int(minutes * MINUTE))


@dataclass
class Group:
    """One container group as ACI lists it: no tags, any name. total and rate are the egress metric's
    Average in bytes per second over the group's life and over the last 10 min; None means a point
    with no average, "empty" a metric with no timeseries."""
    name: str
    cpu: float = 2.0
    mem: float = 8.0
    age: float = 30
    total: object = 0.0
    rate: object = 0.0
    containers: int = 1

    @property
    def id(self) -> str:
        return f"{ACI_LIST}/{self.name}"

    def aci(self) -> dict:
        req = {"cpu": self.cpu, "memoryInGB": self.mem}
        return {"id": self.id, "name": self.name, "type": CG, "location": "westus3", "properties": {
            "osType": "Linux", "containers": [{"name": f"c{i}", "properties": {"resources": {"requests": req}}}
                                              for i in range(self.containers)]}}


def small(name, **kw):
    return Group(name, 2.0, 8.0, **kw)


def medium(name, **kw):
    return Group(name, 4.0, 16.0, **kw)


def metric(avg) -> dict:
    point = {"timeStamp": ago(0)}
    if avg == "empty":
        return {"value": [{"name": {"value": "NetworkBytesTransmittedPerSecond"}, "timeseries": []}]}
    if avg is not None:
        point["average"] = avg
    return {"value": [{"name": {"value": "NetworkBytesTransmittedPerSecond"}, "unit": "BytesPerSecond",
                       "timeseries": [{"metadatavalues": [], "data": [point]}]}]}


class FakeArm:
    """Answers one scenario's ARM requests and records them. A request it doesn't expect, with its
    exact query, gets a 400 and lands in .unexpected, so a wrong URI or query fails loudly.

    `codes` overrides an answer: keys "groups", "resources", "budget", "tags", "tag-write",
    "lock-read", "lock-delete", ("metrics", name) and ("delete", name); values a status or TIMEOUT.
    """

    def __init__(self, groups=(), others=(), spend=0.0, tag=None, locked=False, codes=None, next_link=None):
        self.groups, self.others, self.spend, self.tag = list(groups), list(others), spend, tag
        self.locked, self.codes, self.next_link = locked, codes or {}, next_link
        self.calls: list = []
        self.unexpected: list = []

    def answer(self, key, code, body):
        got = self.codes.get(key, code)
        if got is TIMEOUT:
            return TIMEOUT
        return got, (body if 200 <= got < 300 else {"error": {"code": f"Fake{got}"}})

    def __call__(self, method, uri, queries, body):
        self.calls.append((method, uri, queries, body))
        path = uri[len(ARM):] if uri.startswith(ARM + "/") else None
        q = dict(queries)
        by_id = {g.id: g for g in self.groups}
        if (method, path, q) == ("GET", ACI_LIST, {"api-version": "2023-05-01"}):
            listing = {"value": [g.aci() for g in self.groups]}
            if self.next_link:
                listing["nextLink"] = self.next_link
            return self.answer("groups", 200, listing)
        if (method, path, q) == ("GET", f"{CI_ID}/resources", {"$expand": "createdTime", "api-version": "2021-04-01"}):
            rows = [{"id": g.id, "name": g.name, "type": CG, "createdTime": ago(g.age)} for g in self.groups]
            rows += [{"id": f"{CI_ID}/providers/{t}/{n}", "name": n, "type": t, "createdTime": ago(age)}
                     for n, t, age in self.others]
            return self.answer("resources", 200, {"value": rows})
        if (method, path, q) == ("GET", BUDGET, {"api-version": "2023-05-01"}):
            return self.answer("budget", 200, {"properties": {"amount": 85, "timeGrain": "Monthly",
                                                              "currentSpend": {"amount": self.spend, "unit": "USD"}}})
        if path == TAGS and q == {"api-version": "2021-04-01"}:
            if method == "GET":
                tags = {"ManagedBy": "terraform", **({"ci-sweeper-over-at": self.tag} if self.tag is not None else {})}
                return self.answer("tags", 200, {"properties": {"tags": tags}})
            if method == "PATCH":
                return self.answer("tag-write", 200, body)
        if path == LOCK_ID and q == {"api-version": "2022-04-01"}:
            if method == "GET":
                return self.answer("lock-read", 404 if self.locked else 200, {"name": "dispatcher-test"})
            if method == "DELETE":
                return self.answer("lock-delete", 204 if self.locked else 200, {})
        if method == "DELETE" and path in by_id and q == {"api-version": "2023-05-01"}:
            return self.answer(("delete", by_id[path].name), 200, {})
        g = by_id.get((path or "").removesuffix("/providers/Microsoft.Insights/metrics"))
        if method == "GET" and g and path.endswith("/metrics") and set(q) == {
                "api-version", "metricnames", "aggregation", "interval", "timespan"} and (
                q["api-version"], q["metricnames"], q["aggregation"], q["interval"]) == (
                "2023-10-01", "NetworkBytesTransmittedPerSecond", "Average", "FULL"):
            start, _, end = q["timespan"].partition("/")
            fmt = lambda t: t[:19] + "Z"  # noqa: E731
            if end == fmt(NOW) and start == fmt(ago(10)):
                return self.answer(("metrics", g.name), 200, metric(g.rate))
            if end == fmt(NOW) and start == fmt(ago(g.age + 1)):
                return self.answer(("metrics", g.name), 200, metric(g.total))
            # With no createdTime the template falls back to the rate window, a minute early.
            if end == fmt(NOW) and start == fmt(ago(11)) and self.codes.get("resources", 200) != 200:
                return self.answer(("metrics", g.name), 200, metric(g.total))
        self.unexpected.append((method, uri, q))
        return 400, {"error": {"code": "FakeUnexpected"}}

    def deletes(self) -> list[str]:
        return [u[len(ARM):] for m, u, _, _ in self.calls if m == "DELETE"]

    def tag_writes(self) -> list[dict]:
        return [b for m, u, _, b in self.calls if m == "PATCH"]


def sweep(*groups, **kw) -> tuple[dict, FakeArm]:
    arm = FakeArm(groups, **kw)
    result = Run(definition(), arm).go()
    assert not arm.unexpected, f"requests the fake didn't expect: {arm.unexpected}"
    return result, arm


OK = {"status": "Succeeded", "code": None}
LOCKED = {"status": "Failed", "code": "Locked"}
LOCK_FAILED = {"status": "Failed", "code": "LockFailed"}
DEGRADED = {"status": "Failed", "code": "Degraded"}
MARK = {"operation": "Merge", "properties": {"tags": {"ci-sweeper-over-at": "2026-11-05T12:00:00Z"}}}


# --- values and grants ---

def test_sweeper_values():
    """fence.json's sweeper block is the brief's, and its numbers agree with the rest of the fence."""
    f = fence()
    s = f["sweeper"]
    assert s == EXPECTED_SWEEPER
    # $0.60 an hour, the spec's ceiling; two sweeps in a row fit the window, three don't.
    assert s["ceiling_micro_per_hour"] == 600_000
    assert s["interval_minutes"] < s["repeat_window_minutes"] < 2 * s["interval_minutes"]
    # One price per fence shape, plus the price of any other shape (F19: Task 6 pins the same literals).
    assert set(s["price_micro_per_hour"]) == {f"{x['cpu']}/{x['memory_gb']}" for x in f["shapes"]} | {"other"}
    assert s["price_micro_per_hour"]["other"] > s["ceiling_micro_per_hour"]
    # One deletable type: the template's DELETE carries that type's api-version (phase 3 changes both).
    assert s["delete_types"] == [CG]
    assert s["egress_rate_window_minutes"] == 10 and f["egress"]["cap_bytes"] == 10_000_000_000


def test_sweeper_role_exact():
    body = block(tf("sweeper.tf"), "resource", "azurerm_role_definition", "sweeper")
    assert has(body, "name", '"Realliance CI sweeper"')
    assert has(body, "scope", "azurerm_resource_group.ci.id")
    assert has(body, "actions", "local.sweeper.actions")
    assert has(tf("sweeper.tf"), "sweeper", "local.fence.sweeper")
    for empty in ("not_actions", "data_actions", "not_data_actions"):
        assert has(body, empty, "[]"), empty
    assert has(body, "assignable_scopes", "[azurerm_resource_group.ci.id]")
    assert len(re.findall(r"^\s*permissions\s*\{", body, re.M)) == 1
    assert not re.search(r'dynamic\s+"permissions"', body)
    actions = fence()["sweeper"]["actions"]
    assert actions == EXPECTED_SWEEPER["actions"] and len(set(actions)) == len(actions)
    assert not [a for a in actions if "*" in a]
    # Authorization rights: reading assignments, and deleting one, which the ABAC condition narrows.
    assert [a for a in actions if a.startswith("Microsoft.Authorization/")] == [
        "Microsoft.Authorization/roleAssignments/read", "Microsoft.Authorization/roleAssignments/delete"]


def test_sweeper_role_covers_every_request():
    """Each request the template makes needs one of the role's actions, and each action has a use,
    except resourceGroups/read, which the brief grants for reading the group itself."""
    need = set()
    for _, a in http_actions(definition()):
        uri, method = a["inputs"]["uri"], a["inputs"]["method"]
        if uri.endswith("/providers/Microsoft.Insights/metrics"):
            need.add("Microsoft.Insights/metrics/read")
        elif "/providers/Microsoft.Resources/tags/" in uri:
            need.add("Microsoft.Resources/tags/" + {"GET": "read", "PATCH": "write"}[method])
        elif "/providers/Microsoft.Consumption/budgets/" in uri:
            need.add("Microsoft.Consumption/budgets/read")
        elif "/providers/Microsoft.Authorization/roleAssignments/" in uri:
            need.add("Microsoft.Authorization/roleAssignments/" + {"GET": "read", "DELETE": "delete"}[method])
        elif uri.endswith("/resources"):
            need.add("Microsoft.Resources/subscriptions/resourceGroups/resources/read")
        else:
            need.add(f"{CG}/" + {"GET": "read", "DELETE": "delete"}[method])
    assert need == set(fence()["sweeper"]["actions"]) - {"Microsoft.Resources/subscriptions/resourceGroups/read"}


def test_sweeper_assignment_and_lock_condition():
    """The sweeper's identity holds its role on the CI group, and ABAC narrows its delete right to
    assignments of the dispatcher's role. Cost Management Reader and the history reader are other
    roles, so the sweeper can't delete them even by mistake."""
    text = tf("sweeper.tf")
    body = block(text, "resource", "azurerm_role_assignment", "sweeper")
    assert has(body, "principal_id", SWEEPER_MSI)
    assert has(body, "scope", CI_GROUP)
    assert has(body, "condition_version", '"2.0"')
    assert has(body, "condition", "local.sweeper_lock_condition")
    assert has(text, "sweeper_lock_condition", f'"{SWEEPER_LOCK_CONDITION}"')
    assert has(body, "skip_service_principal_aad_check", "true")


def test_history_reader_exact():
    text = tf("sweeper.tf")
    role = block(text, "resource", "azurerm_role_definition", "history_reader")
    assert has(role, "name", '"Realliance CI sweeper history reader"')
    assert has(role, "actions", '["Microsoft.Logic/workflows/runs/read"]')
    for empty in ("not_actions", "data_actions", "not_data_actions"):
        assert has(role, empty, "[]"), empty
    # A custom role's assignable scope can't be a single resource, so the role may go anywhere in the
    # guard group, and the one assignment puts it on the workflow only.
    assert has(role, "scope", "azurerm_resource_group.guard.id")
    assert has(role, "assignable_scopes", "[azurerm_resource_group.guard.id]")
    a = block(text, "resource", "azurerm_role_assignment", "history_reader")
    assert has(a, "scope", WORKFLOW) and has(a, "principal_id", SP)
    assert has(a, "role_definition_id", "azurerm_role_definition.history_reader.role_definition_resource_id")


def test_failed_run_alert():
    text = tf("sweeper.tf")
    group = block(text, "resource", "azurerm_monitor_action_group", "sweeper_failed")
    assert has(group, "name", '"ci-sweeper-failed"')
    assert has(group, "resource_group_name", "azurerm_resource_group.guard.name")
    assert re.search(r'dynamic\s+"email_receiver"\s*\{', group)
    assert has(group, "for_each", "range(nonsensitive(length(var.alert_emails)))")
    assert has(group, "email_address", "var.alert_emails[email_receiver.value]")
    alert = block(text, "resource", "azurerm_monitor_metric_alert", "sweeper_failed")
    assert has(alert, "scopes", f"[{WORKFLOW}]")
    assert has(alert, "frequency", '"PT5M"') and has(alert, "window_size", '"PT15M"')
    assert has(alert, "enabled", "true")
    crit = _body(alert, re.search(r"^\s*criteria\s*\{", alert, re.M).end())
    for key, value in (("metric_namespace", '"Microsoft.Logic/workflows"'), ("metric_name", '"RunsFailed"'),
                       ("aggregation", '"Total"'), ("operator", '"GreaterThan"'), ("threshold", "0")):
        assert has(crit, key, value), key
    assert has(alert, "action_group_id", "azurerm_monitor_action_group.sweeper_failed.id")


def test_workflow_resource():
    text = tf("sweeper.tf")
    wf = block(text, "resource", "azapi_resource", "sweeper")
    assert has(wf, "type", '"Microsoft.Logic/workflows@2019-05-01"')
    assert has(wf, "name", '"ci-sweeper"')
    assert has(wf, "parent_id", "azurerm_resource_group.guard.id")
    assert has(wf, "location", "local.fence.region")
    assert has(_body(wf, re.search(r"^\s*identity\s*\{", wf, re.M).end()), "type", '"SystemAssigned"')
    assert has(wf, "state", '"Enabled"')  # a disabled sweeper stops silently (Review Focus 5)
    assert has(wf, "definition", "local.sweeper_definition")
    assert has(text, "sweeper_definition", 'jsondecode(templatefile("${path.module}/sweeper/workflow.json.tftpl", {')
    # First run after the dispatcher's assignment exists: a missing assignment reads as locked.
    assert has(wf, "depends_on", "[azurerm_role_assignment.dispatcher]")


# --- the template, statically ---

def test_template_inputs():
    """Ids come from plan-time values (F6); every number and name from fence.json."""
    inputs = template_inputs()
    ids = {k: v for k, v in inputs.items() if not v.startswith(("local.fence.", "local.sweeper.", "one(local.sweeper."))}
    assert ids == {"ci_group_id": "azurerm_resource_group.ci.id",
                   "dispatcher_assignment_id": "local.dispatcher_assignment_id",
                   "budget_name": "azurerm_consumption_budget_resource_group.ci.name"}
    assert inputs["delete_type"] == "one(local.sweeper.delete_types)"
    assert "azurerm_role_assignment" not in " ".join(inputs.values())
    d = definition()
    assert set(d) == {"$schema", "contentVersion", "parameters", "triggers", "actions", "outputs"}
    assert d["parameters"] == {} and d["outputs"] == {}


def test_trigger():
    d = definition()
    assert set(d["triggers"]) == {"Recurrence"}
    t = d["triggers"]["Recurrence"]
    assert t["type"] == "Recurrence"
    assert t["recurrence"] == {"frequency": "Minute", "interval": 10}
    assert t["runtimeConfiguration"] == {"concurrency": {"runs": 1}}


def test_every_http_action_has_a_timeout():
    """Review Focus 5: every request has a bound, so with no Until loop and loops over finite lists,
    a run ends; with concurrency 1, a hung request can hold the queue for minutes, not forever."""
    d = definition()
    https = http_actions(d)
    assert len(https) == 13
    for path, a in https:
        name = "/".join(path)
        assert a.get("limit") == {"timeout": "PT1M"}, name
        assert a["inputs"]["authentication"] == MSI, name
        # One retry, 5 s later. The policy lives in inputs: Azure refuses it on the action itself.
        assert a["inputs"].get("retryPolicy") == RETRY and "retryPolicy" not in a, name
        assert a["inputs"]["uri"].startswith((ARM + "/", ARM + "@{")), name
        assert set(a["inputs"]) <= {"method", "uri", "queries", "body", "authentication", "retryPolicy"}, name
        assert set(a) <= {"type", "inputs", "limit", "runAfter", "operationOptions"}, name
        # A DELETE returns when ARM accepts it; the next sweep sees the result.
        assert (a.get("operationOptions") == "DisableAsyncPattern") == (a["inputs"]["method"] == "DELETE"), name
    assert fence()["sweeper"]["action_timeout"] == "PT1M"
    types = {a["type"] for _, a in walk(d["actions"])}
    assert types <= {"Http", "InitializeVariable", "IncrementVariable", "AppendToArrayVariable", "Compose", "Query",
                     "Foreach", "If", "Terminate"}, types


def test_action_names_unique_and_runafter_local():
    seen = [p[-1] for p, _ in walk(definition()["actions"])]
    assert len(seen) == len(set(seen))
    for path, a in walk(definition()["actions"]):
        for want in a.get("runAfter", {}).values():
            assert want and set(want) <= {"Succeeded", "Failed", "TimedOut", "Skipped"}, path


def test_references_are_upstream():
    """The runtime refuses a reference to an action that isn't upstream of the one reading it.
    Every actions(), body() and outputs() names an ancestor: a runAfter predecessor, transitively,
    in the same scope or an enclosing one. items() names an enclosing loop, and variables() an
    initialized variable."""
    d = definition()
    variables = {a["inputs"]["variables"][0]["name"] for a in d["actions"].values() if a["type"] == "InitializeVariable"}

    def check(actions: dict, outer: set, loops: tuple):
        for name, a in actions.items():
            up, todo = set(), list(a.get("runAfter", {}))
            while todo:
                p = todo.pop()
                if p not in up:
                    up.add(p)
                    todo += list(actions[p].get("runAfter", {}))
            visible = outer | up
            own = {k: v for k, v in a.items() if k not in ("actions", "else")}
            for s in strings(own):
                for fn, ref in re.findall(r"\b(actions|outputs|body)\('([^']+)'\)", s):
                    assert ref in visible, f"{name} reads {fn}('{ref}'), which isn't upstream"
                for ref in re.findall(r"\bitems\('([^']+)'\)", s):
                    assert ref in loops, f"{name} reads items('{ref}') outside that loop"
                for ref in re.findall(r"\bvariables\('([^']+)'\)", s):
                    assert ref in variables, ref
            if a["type"] in ("IncrementVariable", "AppendToArrayVariable"):
                assert a["inputs"]["name"] in variables
            check(a.get("actions", {}), visible, loops + ((name,) if a["type"] == "Foreach" else ()))
            check(a.get("else", {}).get("actions", {}), visible, loops)

    check(d["actions"], set(), ())


def test_expressions_parse():
    """Every expression in the template parses (the simulator's grammar is the runtime's subset)."""
    count = 0
    for s in strings(definition()):
        if s.startswith("@") and not s.startswith(("@{", "@@")):
            wdl_parse(s[1:])
            count += 1
        for j, k in interpolations(s):
            wdl_parse(s[j + 2:k - 1])
            count += 1
    assert count > 40


def test_age_kill():
    d = definition()
    assert find(d, "Read_resources")["inputs"]["queries"] == {"$expand": "createdTime", "api-version": "2021-04-01"}
    q = find(d, "Old_compute")
    assert q["type"] == "Query" and q["inputs"]["from"] == "@body('Read_resources')?['value']"
    assert q["inputs"]["where"] == (
        "@and(equals(toLower(item()?['type']), toLower('Microsoft.ContainerInstance/containerGroups')), "
        "less(ticks(item()?['createdTime']), ticks(addMinutes(outputs('Now'), -90))), "
        "not(contains(variables('deleted'), toLower(item()?['id']))))")
    loop = find(d, "Delete_old")
    assert loop["foreach"] == "@body('Old_compute')"
    assert loop["runtimeConfiguration"] == {"concurrency": {"repetitions": 1}}
    delete = find(d, "Delete_old_group")["inputs"]
    assert (delete["method"], delete["uri"], delete["queries"]) == (
        "DELETE", ARM + "@{items('Delete_old')?['id']}", {"api-version": "2023-05-01"})


def test_age_kill_runs():
    """Older than 90 min goes, whatever its name; 90 min exactly stays; other types stay."""
    old, edge, young = small("anything", age=95), small("ci-small-1-1-1", age=90), medium("x", age=30)
    result, arm = sweep(old, edge, young, others=[("old-nsg", "Microsoft.Network/networkSecurityGroups", 500)])
    assert result == OK
    assert arm.deletes() == [old.id]
    assert arm.tag_writes() == []


def avg(action: str) -> str:
    return (f"coalesce(first(coalesce(first(coalesce(first(coalesce(body('{action}')?['value'], json('[]')))"
            f"?['timeseries'], json('[]')))?['data'], json('[]')))?['average'], 0)")


def shape_is(cpu: int, mem: int) -> str:
    c, m = ("coalesce(outputs('Requests')?['cpu'], 0)", "coalesce(outputs('Requests')?['memoryInGB'], 0)")
    return (f"and(equals(length(outputs('Containers')), 1), greaterOrEquals({c}, {cpu}), lessOrEquals({c}, {cpu}), "
            f"greaterOrEquals({m}, {mem}), lessOrEquals({m}, {mem}))")


def test_burn_by_shape():
    """F19: the literals Task 6's ShapePrice pins, and nothing else."""
    s = fence()["sweeper"]
    assert s["price_micro_per_hour"] == {"2/8": 117000, "4/16": 233000, "other": 1000000}
    d = definition()
    expr = find(d, "Shape_price")["inputs"]
    assert expr == f"@if({shape_is(2, 8)}, 117000, if({shape_is(4, 16)}, 233000, 1000000))"
    assert find(d, "Containers")["inputs"] == "@coalesce(items('Each_group')?['properties']?['containers'], json('[]'))"
    assert find(d, "Requests")["inputs"] == (
        "@coalesce(first(outputs('Containers'))?['properties']?['resources']?['requests'], json('{}'))")
    assert find(d, "Group_burn")["inputs"] == (
        f"@add(outputs('Shape_price'), div(mul(mul({avg('Egress_rate')}, 3600), 87000), 1000000000.0))")
    assert find(d, "Add_burn")["inputs"] == {"name": "burn", "value": "@outputs('Group_burn')"}
    # The burn sums in one loop at a time (a parallel increment isn't documented as safe).
    assert find(d, "Each_group")["runtimeConfiguration"] == {"concurrency": {"repetitions": 1}}
    init = find(d, "Init_burn")["inputs"]["variables"]
    assert init == [{"name": "burn", "type": "float", "value": 0}]


@pytest.mark.parametrize("cpu,mem,containers,price", [
    (2.0, 8.0, 1, 117000), (2, 8, 1, 117000), (4.0, 16.0, 1, 233000), (2.0, 16.0, 1, 1000000), (1.0, 2.0, 1, 1000000),
    (0, 0, 1, 1000000), (2.0, 8.0, 2, 1000000), (2.0, 8.0, 0, 1000000), (4.0, 8.0, 1, 1000000), (2.5, 8.0, 1, 1000000)])
def test_shape_price_table(cpu, mem, containers, price):
    """Task 6's TestShapePriceMatchesSweeper cases, evaluated: 2/8, 4/16, and anything else at $1."""
    run = Run(definition(), FakeArm())
    run.loops["Each_group"] = Group("g", cpu, mem, containers=containers).aci()
    for name in ("Containers", "Requests", "Shape_price"):
        assert run.action(name, find(definition(), name)) == "Succeeded"
    assert run.results["Shape_price"]["outputs"] == price


def test_shape_price_without_containers():
    run = Run(definition(), FakeArm())
    run.loops["Each_group"] = {"id": "x", "name": "x", "properties": {}}
    for name in ("Containers", "Requests", "Shape_price"):
        assert run.action(name, find(definition(), name)) == "Succeeded"
    assert run.results["Shape_price"]["outputs"] == 1000000


TRIP = {"or": [
    {"and": [
        {"greater": ["@variables('burn')", 600000]},
        {"greaterOrEquals": ["@{coalesce(body('Read_tags')?['properties']?['tags']?['ci-sweeper-over-at'], '')}",
                             "@{formatDateTime(addMinutes(outputs('Now'), -12), 'yyyy-MM-ddTHH:mm:ssZ')}"]},
    ]},
    {"greaterOrEquals": ["@float(string(coalesce(body('Read_budget')?['properties']?['currentSpend']?['amount'], 0)))",
                         85]},
    {"equals": ["@outputs('Read_dispatcher_assignment')?['statusCode']", 404]},
]}
LOCK_DONE = {"or": [
    {"equals": ["@actions('Delete_dispatcher_assignment')?['status']", "Succeeded"]},
    {"equals": ["@outputs('Delete_dispatcher_assignment')?['statusCode']", 404]},
]}
EVERY_END = ["Succeeded", "Failed", "TimedOut"]


def test_trip_rule():
    """Two sweeps over $0.60 an hour, or the $85 budget spent, or the lock already in place. The trip
    deletes the dispatcher's assignment first, then every group whatever that DELETE did, and ends
    Failed (F7)."""
    f = fence()
    assert (f["sweeper"]["ceiling_micro_per_hour"], f["sweeper"]["repeat_window_minutes"], f["budget_ci_usd"]) == (
        600000, 12, 85)
    trip = find(definition(), "Trip")
    assert trip["type"] == "If" and trip["expression"] == TRIP
    acts = trip["actions"]
    assert set(acts) == {"Delete_dispatcher_assignment", "Delete_all_groups", "Lock_result"}
    assert [n for n, a in acts.items() if a.get("runAfter", {}) == {}] == ["Delete_dispatcher_assignment"]
    lock = acts["Delete_dispatcher_assignment"]
    assert (lock["type"], lock["inputs"]["method"], lock["inputs"]["uri"], lock["inputs"]["queries"]) == (
        "Http", "DELETE", ARM + LOCK_ID, {"api-version": "2022-04-01"})
    loop = acts["Delete_all_groups"]
    assert loop["type"] == "Foreach" and loop["foreach"] == "@body('Live_groups')"
    assert loop["runAfter"] == {"Delete_dispatcher_assignment": EVERY_END}
    inner = loop["actions"]["Delete_group"]["inputs"]
    assert (inner["method"], inner["uri"]) == ("DELETE", ARM + "@{items('Delete_all_groups')?['id']}")
    result = acts["Lock_result"]
    assert result["runAfter"] == {"Delete_all_groups": EVERY_END}
    assert result["expression"] == LOCK_DONE
    ends = {n: a["inputs"] for n, a in [*result["actions"].items(), *result["else"]["actions"].items()]}
    assert {n: (i["runStatus"], i["runError"]["code"]) for n, i in ends.items()} == {
        "Terminate_locked": ("Failed", "Locked"), "Terminate_lock_failed": ("Failed", "LockFailed")}
    assert set(result["actions"]) == {"Terminate_locked"}
    # The groups the trip deletes: every listed group not already deleted in this run.
    live = find(definition(), "Live_groups")["inputs"]
    assert live == {"from": "@body('Read_groups')?['value']",
                    "where": "@not(contains(variables('deleted'), toLower(item()?['id'])))"}


def trip_ctx(burn=0.0, tag=None, spend=0.0, lock_code=200) -> Run:
    run = Run(definition(), FakeArm())
    run.vars.update(burn=burn, deleted=[], problems=[])
    tags = {"properties": {"tags": {} if tag is None else {"ci-sweeper-over-at": tag}}}
    run.results.update(
        Now={"status": "Succeeded", "outputs": NOW, "body": NOW},
        Read_tags={"status": "Succeeded", "outputs": {"statusCode": 200, "body": tags}, "body": tags},
        Read_budget={"status": "Succeeded", "body": {"properties": {"currentSpend": {"amount": spend}}}},
        Read_dispatcher_assignment={"status": "Succeeded" if lock_code == 200 else "Failed",
                                    "outputs": {"statusCode": lock_code}, "body": {}})
    return run


@pytest.mark.parametrize("burn,tag,spend,lock,trips", [
    (600001.0, ago(10)[:19] + "Z", 0, 200, True),    # second over-ceiling sweep in a row
    (600001.0, ago(12)[:19] + "Z", 0, 200, True),    # the window's edge
    (600001.0, ago(13)[:19] + "Z", 0, 200, False),   # too old: this sweep is a first one
    (600000.0, ago(10)[:19] + "Z", 0, 200, False),   # at the ceiling is allowed (the gate allows <=)
    (600001.0, None, 0, 200, False),                 # first over-ceiling sweep
    (5e7, "garbage", 0, 200, True),                  # a tag only Chris could write: compares, never throws
    (0.0, None, 85, 200, True),                      # the budget spent
    (0.0, None, 84.99, 200, False),
    (0.0, None, "85.00", 200, True),                 # a decimal ARM sends as a string still compares
    (0.0, None, 0, 404, True),                       # already locked: the dispatcher can't delete its groups
    (0.0, None, 0, 403, False),                      # can't read the assignment: not proof of a lock
])
def test_trip_truth_table(burn, tag, spend, lock, trips):
    assert trip_ctx(burn, tag, spend, lock).condition(find(definition(), "Trip")["expression"]) is trips


@pytest.mark.parametrize("status,code,locked", [
    ("Succeeded", 200, True), ("Succeeded", 204, True), ("Failed", 404, True),
    ("Failed", 400, False), ("Failed", 403, False), ("Failed", 409, False), ("Failed", 429, False),
    ("Failed", 500, False), ("TimedOut", None, False)])
def test_lock_counts_only_2xx_and_404(status, code, locked):
    """F6/F7: a 404 means the assignment is already gone. Any other failure isn't a lock."""
    run = Run(definition(), FakeArm())
    run.results["Delete_dispatcher_assignment"] = {
        "status": status, "outputs": None if code is None else {"statusCode": code}}
    assert run.condition(find(definition(), "Lock_result")["expression"]) is locked


def test_lock_deletes_only_the_dispatcher_assignment():
    """Review Focus 2: one DELETE aims at Microsoft.Authorization, and its target is the id sweeper.tf
    builds from local.dispatcher_assignment_id. No other assignment id reaches the template."""
    d = definition()
    auth = [(p, a) for p, a in http_actions(d) if "microsoft.authorization" in a["inputs"]["uri"].lower()]
    deletes = [a for _, a in auth if a["inputs"]["method"] == "DELETE"]
    assert len(deletes) == 1 and deletes[0]["inputs"]["uri"] == ARM + LOCK_ID
    # The one other Authorization request reads the same assignment.
    assert [(p[-1], a["inputs"]["method"], a["inputs"]["uri"]) for p, a in auth if a["inputs"]["method"] != "DELETE"] == [
        ("Read_dispatcher_assignment", "GET", ARM + LOCK_ID)]
    # Every other DELETE targets an id from the CI group's own lists.
    for p, a in http_actions(d):
        if a["inputs"]["method"] == "DELETE" and a is not deletes[0]:
            assert re.fullmatch(re.escape(ARM) + r"@\{items\('\w+'\)\?\['id'\]\}", a["inputs"]["uri"]), p
    rendered = json.dumps(d)
    assert rendered.count("roleAssignments") == 2 and rendered.count(LOCK_ID) == 2
    assert "Cost Management" not in rendered and "history" not in rendered.lower()


def test_egress_rule():
    """Review Focus 6, E17: every group in the list, whatever its name or tags; the cap and the price
    from fence.json's egress block; nothing read from tags, the dispatcher, or outside the CI group."""
    f = fence()
    d = definition()
    loop = find(d, "Each_group")
    assert loop["foreach"] == "@body('Read_groups')?['value']"
    assert find(d, "Read_groups")["inputs"]["queries"] == {"api-version": "2023-05-01"}
    assert find(d, "Read_groups")["inputs"]["uri"] == ARM + ACI_LIST
    for name, start in (("Egress_total", "addMinutes(outputs('Created'), -1)"),
                        ("Egress_rate", "addMinutes(outputs('Now'), -10)")):
        inp = find(d, name)["inputs"]
        assert inp["uri"] == ARM + "@{items('Each_group')?['id']}/providers/Microsoft.Insights/metrics", name
        assert inp["queries"] == {
            "api-version": "2023-10-01", "metricnames": f["egress"]["metric"], "aggregation": "Average",
            "interval": "FULL", "timespan": f"@{{formatDateTime({start}, 'yyyy-MM-ddTHH:mm:ssZ')}}/"
                                            "@{formatDateTime(outputs('Now'), 'yyyy-MM-ddTHH:mm:ssZ')}"}, name
    # Bytes over the life = the average × the seconds since creation (E17).
    assert find(d, "Created")["inputs"] == (
        "@coalesce(first(body('Find_created'))?['createdTime'], addMinutes(outputs('Now'), -10))")
    assert find(d, "Find_created")["inputs"] == {
        "from": "@coalesce(body('Read_resources')?['value'], json('[]'))",
        "where": "@equals(toLower(item()?['id']), toLower(items('Each_group')?['id']))"}
    assert find(d, "Egress_bytes")["inputs"] == (
        f"@mul({avg('Egress_total')}, div(sub(ticks(outputs('Now')), ticks(outputs('Created'))), 10000000))")
    cap = find(d, "Over_egress_cap")
    assert cap["expression"] == {"greaterOrEquals": ["@outputs('Egress_bytes')", 10000000000]}
    assert f["egress"]["cap_bytes"] == 10000000000 and f["egress"]["micro_per_gb"] == 87000
    assert "87000" in find(d, "Group_burn")["inputs"]
    delete = cap["actions"]["Delete_over_cap"]["inputs"]
    assert (delete["method"], delete["uri"]) == ("DELETE", ARM + "@{items('Each_group')?['id']}")
    # Nothing in the loop reads a tag, the budget, the assignment, or anything but the lists and metrics.
    loop_strings = " ".join(strings(loop))
    for banned in ("tags", "Read_tags", "Read_budget", "Read_dispatcher_assignment", "parameters(", "ci-"):
        assert banned not in loop_strings, banned
    for fn, ref in re.findall(r"\b(actions|outputs|body)\('([^']+)'\)", loop_strings):
        assert ref in {"Read_groups", "Read_resources", "Now"} | {p[-1] for p, _ in walk(loop["actions"])}, ref


# --- the template, run ---

def test_run_idle():
    result, arm = sweep()
    assert result == OK
    assert {m for m, *_ in arm.calls} == {"GET"} and len(arm.calls) == 5


def test_run_first_over_ceiling_sweep_marks_the_tag():
    result, arm = sweep(medium("a"), medium("b"), medium("c"))  # 699_000
    assert result == OK
    assert arm.deletes() == [] and arm.tag_writes() == [MARK]


def test_run_second_over_ceiling_sweep_locks_first():
    """The drill (Task 5 Step 3): the assignment goes before any group (F7), and the run fails."""
    groups = [medium(n) for n in ("a", "b", "c")]
    result, arm = sweep(*groups, tag=ago(10)[:19] + "Z")
    assert result == LOCKED
    assert arm.deletes() == [LOCK_ID] + [g.id for g in groups]
    assert arm.tag_writes() == []


@pytest.mark.parametrize("answer", [403, 409, 500, TIMEOUT])
def test_run_failed_lock_still_deletes_every_group(answer):
    """F7: a failed or hung assignment DELETE never skips the group deletes, and the run says so."""
    groups = [medium(n) for n in ("a", "b", "c")]
    result, arm = sweep(*groups, tag=ago(10)[:19] + "Z", codes={"lock-delete": answer})
    assert result == LOCK_FAILED
    assert arm.deletes() == [LOCK_ID] + [g.id for g in groups]


def test_run_lock_answered_404_is_locked():
    result, arm = sweep(medium("a"), spend=90.0, codes={"lock-delete": 404})
    assert result == LOCKED and arm.deletes() == [LOCK_ID, medium("a").id]


def test_run_failed_group_delete_after_the_lock_still_ends_locked():
    groups = [medium(n) for n in ("a", "b", "c")]
    result, arm = sweep(*groups, tag=ago(10)[:19] + "Z", codes={("delete", "b"): 409, ("delete", "c"): TIMEOUT})
    assert result == LOCKED and arm.deletes() == [LOCK_ID] + [g.id for g in groups]


def test_run_budget_spent_locks_with_no_compute():
    result, arm = sweep(spend=85.0)
    assert result == LOCKED and arm.deletes() == [LOCK_ID]


def test_run_already_locked_keeps_compute_at_zero():
    """While locked, the dispatcher can't delete its own groups, so every sweep deletes them all."""
    g = small("made-during-propagation", age=3)
    result, arm = sweep(g, locked=True)
    assert result == LOCKED and arm.deletes() == [LOCK_ID, g.id]


def test_run_stale_tag_is_rewritten():
    result, arm = sweep(medium("a"), medium("b"), medium("c"), tag=ago(13)[:19] + "Z")
    assert result == OK and arm.deletes() == [] and arm.tag_writes() == [MARK]


def test_run_under_ceiling_clears_the_tag():
    tag = ago(10)[:19] + "Z"
    result, arm = sweep(small("a"), tag=tag)
    assert result == OK
    assert arm.tag_writes() == [{"operation": "Delete", "properties": {"tags": {"ci-sweeper-over-at": tag}}}]


def test_run_unknown_shape_counts_a_dollar_an_hour():
    result, arm = sweep(Group("odd", 1.0, 2.0))
    assert result == OK and arm.tag_writes() == [MARK]


@pytest.mark.parametrize("avg_bps,age,deleted", [
    (6_000_000.0, 30, True),       # 10.8 GB over 30 min
    (5_000_000.0, 30, False),      # 9.0 GB
    (1_000_000.0, 200, True),      # 12 GB over 200 min (the age kill would come too)
    (None, 30, False), ("empty", 30, False)])  # no data yet
def test_run_egress_cap(avg_bps, age, deleted):
    """Any group at or over 10 GB goes, whatever its name, and the run carries on."""
    g = small("not-ours-at-all", age=age, total=avg_bps)
    result, arm = sweep(g)
    assert result == OK
    assert arm.deletes() == ([g.id] if deleted else [])


@pytest.mark.parametrize("bytes_,over", [(10_000_000_000, True), (9_999_999_999, False), (1.0e10, True)])
def test_egress_cap_is_at_or_over(bytes_, over):
    run = Run(definition(), FakeArm())
    run.results["Egress_bytes"] = {"status": "Succeeded", "outputs": bytes_}
    assert run.condition(find(definition(), "Over_egress_cap")["expression"]) is over


def test_run_egress_cap_then_age_kill_deletes_once():
    g = small("x", age=95, total=6_000_000.0)
    result, arm = sweep(g)
    assert result == OK and arm.deletes() == [g.id]


def test_run_egress_rate_counts_in_the_burn():
    """117_000 + 2 MB/s × 3600 × 87_000 / 1e9 = 743_400 > 600_000: a hot uploader marks the tag, and
    on the next sweep locks, though its compute alone is far under the ceiling."""
    g = small("uploader", rate=2_000_000.0)
    assert sweep(g)[1].tag_writes() == [MARK]
    result, arm = sweep(g, tag=ago(10)[:19] + "Z")
    assert result == LOCKED and arm.deletes() == [LOCK_ID, g.id]
    assert sweep(small("quiet", rate=1_000_000.0))[1].tag_writes() == []  # 430_200


@pytest.mark.parametrize("codes", [{("metrics", "b"): 500}, {("metrics", "b"): TIMEOUT}])
def test_run_unread_egress_is_degraded_but_enforces_the_rest(codes):
    old = small("a", age=95)
    result, arm = sweep(old, small("b"), codes=codes)
    assert result == DEGRADED
    assert arm.deletes() == [old.id]


@pytest.mark.parametrize("key", ["budget", "tags", "resources", "groups", "lock-read"])
def test_run_failed_read_is_degraded(key):
    result, _ = sweep(small("a"), codes={key: 500})
    assert result == DEGRADED


def test_run_budget_unread_keeps_the_age_kill():
    old = small("a", age=95)
    result, arm = sweep(old, codes={"budget": 404})
    assert result == DEGRADED and arm.deletes() == [old.id]


def test_run_groups_unread_still_locks_on_the_budget():
    """With no group list the trip can't delete groups, but the lock still goes."""
    result, arm = sweep(small("a"), spend=90.0, codes={"groups": 500})
    assert result == LOCKED and arm.deletes() == [LOCK_ID]


def test_run_failed_delete_is_degraded():
    old = small("a", age=95)
    result, arm = sweep(old, codes={("delete", "a"): 409})
    assert result == DEGRADED and arm.deletes() == [old.id]


def test_run_delete_answered_404_is_fine():
    result, _ = sweep(small("a", age=95), codes={("delete", "a"): 404})
    assert result == OK


def test_run_tag_write_failure_is_degraded():
    result, _ = sweep(medium("a"), medium("b"), medium("c"), codes={"tag-write": 500})
    assert result == DEGRADED


def test_run_paged_list_is_degraded():
    """The sweeper reads one page; a list that pages fails the run loudly instead of hiding groups."""
    result, _ = sweep(small("a"), next_link=ARM + ACI_LIST + "?$skipToken=x")
    assert result == DEGRADED


# --- files and hooks -------------------------------------------------------------------------------


def test_no_guid_in_public_files():
    """F14: no UUID-shaped string in a file git would publish (secrets and the lock excepted)."""
    listed = subprocess.run(["git", "-C", str(REPO), "ls-files", "--cached", "--others", "--exclude-standard",
                             "--", "azure/ci"], capture_output=True, text=True, check=True).stdout.split()
    hits = []
    for rel in listed:
        p = REPO / rel
        if ".secret." in p.name or p.name == ".terraform.lock.hcl" or not p.is_file():
            continue
        for n, line in enumerate(p.read_text().splitlines(), 1):
            if GUID.search(line):
                hits.append(f"{rel}:{n}")
    assert listed, "nothing listed under azure/ci"
    assert not hits, f"UUID-shaped strings at {hits}"  # locations only, never the value


def test_secret_tfvars_is_git_crypt():
    """ci.secret.auto.tfvars exists and git-crypt filters it (checked without opening it)."""
    assert (CI / "ci.secret.auto.tfvars").is_file()
    attr = subprocess.run(["git", "-C", str(REPO), "check-attr", "filter", "--", "azure/ci/ci.secret.auto.tfvars"],
                          capture_output=True, text=True, check=True).stdout
    assert attr.strip().endswith(": filter: git-crypt")


def test_tf_sh():
    sh = tf("tf.sh")
    assert sh.startswith("#!/usr/bin/env bash\n") and os.access(CI / "tf.sh", os.X_OK)
    assert "set -euo pipefail" in sh
    assert "az account show >/dev/null 2>&1 ||" in sh
    assert re.search(r"grep -q '\^subscription_id' \"\$secret\"", sh)
    assert 'secret="$repo/azure/ci/ci.secret.auto.tfvars"' in sh
    assert re.search(r"^umask 077$", sh, re.M)
    assert "exec terraform" not in sh  # every run goes through the redacting filter
    assert re.search(r"^  trap '' INT TERM$", sh, re.M)  # a Ctrl-C mustn't end the filter mid-apply
    assert sh.rstrip().endswith('exit "$status"')
    # Round 3: raw output only to an explicit --to/--append file; TF_CLI_ARGS* and TF_LOG* refused.
    assert "for v in ${!TF_CLI_ARGS*} ${!TF_LOG*}; do" in sh and "compgen" not in sh.replace("no compgen", "")
    assert '3>"$dest"' in sh and '3>>"$dest"' in sh
    assert "[ -f /dev/stdout ]" not in sh  # a transcript can be a regular file (round 3 R2)


@pytest.fixture
def tf_rig(tmp_path):
    """tf.sh in a scratch repo, with stub `az` and `terraform` first on PATH.

    The stub terraform prints a fresh UUID (upper and lower case) on both streams, its umask, and
    exits with $STUB_RC. With STUB_LOCK set it also prints Lock Info on stderr, as an "Error
    acquiring the state lock" does. It touches $STUB_MARK, so a refused call is provably not run.
    Nothing here touches Azure or the real module.
    """
    root = tmp_path / "repo"
    (root / "azure/ci").mkdir(parents=True)
    shutil.copy2(CI / "tf.sh", root / "azure/ci/tf.sh")
    (root / "azure/ci/ci.secret.auto.tfvars").write_text('subscription_id = "stub"\n')
    subprocess.run(["git", "init", "-q", str(root)], check=True, cwd=tmp_path, env=clean_env())
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "az").write_text("#!/bin/sh\nexit \"${STUB_AZ_RC:-0}\"\n")
    (bin_ / "terraform").write_text(
        "#!/bin/sh\n"
        'echo "out $STUB_ID $STUB_UP args=$*"\n'
        'echo "err $STUB_ID $STUB_UP" >&2\n'
        'echo "umask $(umask)"\n'
        'if [ -n "$STUB_LOCK" ]; then\n'
        '  printf "Lock Info:\\n  ID:        %s\\n" "$STUB_LOCK" >&2\n'
        '  printf "\\342\\224\\202   ID:        %s\\n" "$STUB_LOCK" >&2\n'
        '  printf "  ID:        %s trailing\\n" "$STUB_LOCK" >&2\n'
        '  printf "  Path:      x/%s\\n" "$STUB_LOCK" >&2\n'
        'fi\n'
        ': > "$STUB_MARK"\n'
        'exit "${STUB_RC:-0}"\n'
    )
    for f in ("az", "terraform"):
        (bin_ / f).chmod(0o755)
    ident = str(uuid.uuid4())
    mark = tmp_path / "ran"
    env = {k: v for k, v in clean_env().items() if not k.startswith(("TF_CLI_ARGS", "TF_LOG"))}
    env.update(PATH=f"{bin_}:{os.environ['PATH']}", STUB_ID=ident, STUB_UP=ident.upper(), STUB_MARK=str(mark))

    def run(*args, to_file=False, cwd=None, **extra):
        """Run tf.sh; stdout is a pipe, or with to_file a regular file whose text lands in .stdout."""
        mark.unlink(missing_ok=True)
        if not to_file:
            r = subprocess.run([str(root / "azure/ci/tf.sh"), *args], capture_output=True, text=True,
                               env={**env, **extra}, cwd=cwd or tmp_path)
        else:
            out = tmp_path / "stdout"
            with out.open("w") as fh:
                r = subprocess.run([str(root / "azure/ci/tf.sh"), *args], stdout=fh, stderr=subprocess.PIPE,
                                   text=True, env={**env, **extra}, cwd=cwd or tmp_path)
            r.stdout = out.read_text()
        r.ran = mark.exists()
        return r

    return run, ident


@pytest.mark.parametrize("args", [("plan", "-no-color"), ("apply", "x.tfplan"), ("show", "x.tfplan"),
                                  ("output",), ("state", "list")])
def test_tf_sh_redacts(tf_rig, args):
    """Review m5: no UUID-shaped string leaves tf.sh on either stream, and the exit code survives."""
    run, ident = tf_rig
    r = run(*args, STUB_RC="3")
    assert r.returncode == 3
    for stream in (r.stdout, r.stderr):
        if GUID.search(stream):
            pytest.fail(f"a UUID-shaped string got through tf.sh {' '.join(args)}")
    assert r.stdout.startswith("out <id> <id> ") and r.stderr.strip() == "err <id> <id>"
    assert "umask 0077" in r.stdout


RAW_FORMS = [("output", "-raw", "dispatcher_client_id"), ("output", "-json"), ("show", "-json", "x.tfplan"),
             ("output", "-json=true"), ("output", "--raw", "x"), ("output", "-raw=1", "x"), ("show", "--json=TRUE", "p"),
             ("-no-color", "output", "-raw", "x")]


def no_raw(r, ident: str) -> None:
    """Nothing raw on tf.sh's own stdout or stderr: the stub's UUID never appears in either."""
    for stream in (r.stdout, r.stderr):
        if ident in stream or ident.upper() in stream:
            pytest.fail("raw output reached tf.sh's stdout or stderr")


@pytest.mark.parametrize("args", RAW_FORMS)
def test_tf_sh_raw_to_file(tf_rig, tmp_path, args):
    """Round 3 R2: `--to FILE` takes the raw output, exact, in a new 0600 file. tf.sh's own stdout
    stays empty, stderr is redacted, and the exit code survives."""
    run, ident = tf_rig
    dest = tmp_path / "out.json"
    r = run("--to", str(dest), *args, STUB_RC="5")
    assert r.returncode == 5 and r.ran
    assert dest.read_text().startswith(f"out {ident} {ident.upper()} ")
    assert (dest.stat().st_mode & 0o777) == 0o600
    assert r.stdout == "" and r.stderr.strip() == "err <id> <id>"
    no_raw(r, ident)


@pytest.mark.parametrize("args", RAW_FORMS)
@pytest.mark.parametrize("to_file", [False, True])
def test_tf_sh_raw_needs_a_destination(tf_rig, args, to_file):
    """Round 3 R2: without --to or --append a raw form is refused, before terraform runs, whatever
    stdout is. An agent's transcript can be a regular file."""
    run, ident = tf_rig
    r = run(*args, to_file=to_file)
    assert r.returncode == 2 and not r.ran and r.stdout == ""
    assert "needs a destination" in r.stderr
    no_raw(r, ident)


def test_tf_sh_append(tf_rig, tmp_path):
    """--append keeps what's there and adds the raw output, and it tightens an existing 0644 file to
    0600. --to truncates."""
    run, ident = tf_rig
    dest = tmp_path / "secrets.env"
    dest.write_text("KEEP=1\nCI_X=")
    dest.chmod(0o644)
    r = run("--append", str(dest), "output", "-raw", "x")
    assert r.returncode == 0 and r.ran and r.stdout == ""
    text = dest.read_text()
    assert text.startswith(f"KEEP=1\nCI_X=out {ident} ")
    assert (dest.stat().st_mode & 0o777) == 0o600
    no_raw(r, ident)
    r = run("--to", str(dest), "output", "-json")
    assert r.returncode == 0 and dest.read_text().startswith(f"out {ident} ")
    assert "KEEP=1" not in dest.read_text()


def test_tf_sh_destination_is_relative_to_the_caller(tf_rig, tmp_path):
    """tf.sh cds into azure/ci, but a relative destination means the caller's directory."""
    run, ident = tf_rig
    (tmp_path / "work").mkdir()
    r = run("--append", "forge.env", "output", "-raw", "x", cwd=tmp_path / "work")
    assert r.returncode == 0 and (tmp_path / "work/forge.env").read_text().startswith(f"out {ident} ")


def test_tf_sh_refuses_bad_destinations(tf_rig, tmp_path):
    """No link, directory, device, /proc path, or tf.sh's own stdout (a transcript, say)."""
    run, ident = tf_rig
    target = tmp_path / "target"
    target.write_text("")
    link = tmp_path / "link"
    link.symlink_to(target)
    for dest in (str(link), str(tmp_path), "/dev/stdout", "/dev/null", "/proc/self/fd/1", str(tmp_path / "no/dir/x")):
        r = run("--to", dest, "output", "-raw", "x")
        assert r.returncode == 2 and not r.ran, dest
        no_raw(r, ident)
    # The caller's stdout is a regular file, and naming it is refused.
    r = run("--to", str(tmp_path / "stdout"), "output", "-raw", "x", to_file=True)
    assert r.returncode == 2 and not r.ran and "own stdout" in r.stderr
    assert target.read_text() == ""


@pytest.mark.parametrize("args", [("output", "-json=false"), ("show", "-json=0", "p"), ("plan", "-json")])
def test_tf_sh_not_raw_forms_are_redacted(tf_rig, args):
    """A false flag, or -json on a command whose output isn't a data path, stays filtered."""
    run, _ = tf_rig
    r = run(*args, STUB_RC="3")
    assert r.returncode == 3 and r.ran and r.stdout.startswith("out <id> <id> ")


@pytest.mark.parametrize("args", [("state", "pull"), ("state", "push", "x.tfstate"), ("-no-color", "state", "pull"),
                                  ("-chdir=elsewhere", "plan"), ("-chdir", "elsewhere", "plan"),
                                  ("--chdir=.", "output", "-raw", "x"), ("--to", "f", "plan"),
                                  ("--append", "f", "output", "x"), ("output", "-raw", "x", "--to", "f"), ("plan", "--to", "f"),
                                  ("--to",), ("--to", "", "output", "-raw", "x")])
@pytest.mark.parametrize("to_file", [False, True])
def test_tf_sh_refuses(tf_rig, args, to_file):
    """Rounds 2 and 3: `state pull|push`, `-chdir`, a destination on a non-raw command, a misplaced
    --to, and an empty one are refused, whatever stdout is."""
    run, ident = tf_rig
    r = run(*args, to_file=to_file)
    assert r.returncode == 2 and not r.ran and r.stdout == ""
    assert r.stderr.startswith("tf.sh: ")
    no_raw(r, ident)


@pytest.mark.parametrize("var,value,args", [
    ("TF_CLI_ARGS_output", "-raw", ("output", "dispatcher_client_secret")),  # the reviewer's case
    ("TF_CLI_ARGS", "-no-color", ("plan",)),
    ("TF_CLI_ARGS_show", "-json", ("show", "p")),
    ("TF_LOG", "trace", ("apply", "x.tfplan")),
    ("TF_LOG_PROVIDER", "debug", ("plan",)),
])
def test_tf_sh_refuses_tf_cli_args(tf_rig, var, value, args):
    """Round 3 m-a: terraform splices TF_CLI_ARGS* into argv after tf.sh's checks, and TF_LOG*
    traces print request bodies. Either one set means exit 2, with terraform never run."""
    run, ident = tf_rig
    r = run(*args, **{var: value})
    assert r.returncode == 2 and not r.ran and r.stdout == ""
    assert var in r.stderr and value not in r.stderr.replace(var, "")
    no_raw(r, ident)


def test_tf_sh_keeps_the_lock_id(tf_rig):
    """Round 2 n1: Lock Info's `ID:` line stays exact for `force-unlock`; nothing else does."""
    run, _ = tf_rig
    lock = str(uuid.uuid4())
    r = run("plan", STUB_LOCK=lock, STUB_RC="1")
    assert r.returncode == 1
    lines = r.stderr.splitlines()
    assert f"  ID:        {lock}" in lines
    assert f"\u2502   ID:        {lock}" in lines
    assert "  ID:        <id> trailing" in lines and "  Path:      x/<id>" in lines
    assert "err <id> <id>" in lines


def test_tf_sh_refuses_without_az(tf_rig):
    run, _ = tf_rig
    r = run("plan", STUB_AZ_RC="1")
    assert r.returncode == 1 and "not logged in" in r.stderr and not r.ran


def test_readme_names_the_tf_sh_rules():
    """The README tells Chris about the lock id, raw output to files, and the saved-plan rollback."""
    text = (CI / "README.md").read_text()
    assert "force-unlock -force <ID>" in text and "ID:" in text  # round 3 m-c: no hidden prompt
    assert "--to" in text and "--append" in text and "0600" in text
    assert "TF_CLI_ARGS" in text
    assert "plan -destroy -out=" in text
    # Task 2 review n-a: the destination check compares only tf.sh's own stdout and stderr.
    assert "never name a transcript or a capture file" in text
    # Task 3: the sweeper, its lock, and how the lock is undone.
    assert "## The sweeper" in text and "Locked" in text and "Degraded" in text


def test_variables():
    found = labelled(tf("variables.tf"), "variable")
    assert set(found) == {"alert_emails", "subscription_id"}
    for name, body in found.items():
        # Both come from ci.secret.auto.tfvars; sensitive keeps them out of plan output.
        assert has(body, "sensitive", "true"), name


def test_pre_commit_hook():
    cfg = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text())
    hooks = [h for r in cfg["repos"] if r["repo"] == "local" for h in r["hooks"]]
    hook = next(h for h in hooks if h["id"] == "azure-ci")
    assert hook["entry"] == "python -m pytest -q -p no:cacheprovider scripts/test_azure_ci.py"
    assert hook["files"] == r"^(azure/ci/.*|scripts/test_azure_ci\.py|\.renovaterc\.json|\.pre-commit-config\.yaml)$"
    assert hook["pass_filenames"] is False and hook["language"] == "system"


def could_match_azure_ci(rule: dict) -> bool:
    """Conservative: it returns True unless a matcher in the rule provably excludes azure/ci's providers."""
    files = ["azure/ci/main.tf", "azure/ci/.terraform.lock.hcl"]
    names = ["hashicorp/azurerm", "hashicorp/azuread", "hashicorp/time", "Azure/azapi", "azurerm", "azuread",
             "time", "azapi"]
    if "matchManagers" in rule and "terraform" not in rule["matchManagers"]:
        return False
    if "matchDatasources" in rule and "terraform-provider" not in rule["matchDatasources"]:
        return False
    if "matchFileNames" in rule and not any(fnmatch.fnmatch(f, p) for f in files for p in rule["matchFileNames"]):
        return False
    for key in ("matchPackageNames", "matchDepNames"):
        if key in rule and not any(fnmatch.fnmatch(n, p) or p.strip("/") in n for n in names for p in rule[key]):
            return False
    return True


def test_renovate_never_automerges_azure_ci():
    """Review O2: no provider bump reaches the fence unreviewed (:automergeMinor would merge one)."""
    rules = json.loads((REPO / ".renovaterc.json").read_text())["packageRules"]
    idx = [i for i, r in enumerate(rules) if r.get("matchFileNames") == ["azure/ci/**"]]
    assert len(idx) == 1
    rule = rules[idx[0]]
    assert rule["automerge"] is False
    # Its own group: in group:allNonMajor its automerge: false would hold up every other bump.
    assert rule.get("groupName") and rule.get("groupSlug")
    # After every rule that could match: no later rule turns automerge back on.
    for r in rules[idx[0] + 1:]:
        assert not (could_match_azure_ci(r) and r.get("automerge") is True), r.get("description", r)


def test_fmt():
    r = subprocess.run(["terraform", "fmt", "-check", "-recursive", str(CI)], capture_output=True, text=True)
    assert r.returncode == 0, f"terraform fmt would change: {r.stdout}"


# --- plan-level (AZURE_CI_PLAN_JSON) ---------------------------------------------------------------

PLAN = os.environ.get("AZURE_CI_PLAN_JSON")
needs_plan = pytest.mark.skipif(not PLAN, reason="set AZURE_CI_PLAN_JSON to a `tf.sh show -json` file")


@pytest.fixture(scope="module")
def plan() -> dict:
    return json.loads(Path(PLAN).read_text())


def changes(plan: dict) -> dict[str, dict]:
    return {c["address"]: c for c in plan.get("resource_changes", [])}


def config(plan: dict) -> dict[str, dict]:
    return {r["address"]: r for r in plan["configuration"]["root_module"].get("resources", [])}


def refs(res: dict, attr: str) -> list[str]:
    return res.get("expressions", {}).get(attr, {}).get("references", [])


@needs_plan
def test_plan_creates_only(plan):
    """The first apply's plan only (review m4): after Task 4, a plan has prior state and this skips."""
    prior = plan.get("prior_state", {}).get("values", {}).get("root_module", {}).get("resources", [])
    if any(r.get("mode") == "managed" for r in prior):
        pytest.skip("the plan has prior managed state; test_plan_creates_only covers the first apply only")
    ch = changes(plan)
    assert set(ch) == EXPECTED_ADDRESSES
    not_create = sorted(a for a, c in ch.items() if c["change"]["actions"] != ["create"])
    assert not not_create, f"planned actions other than create at {not_create}"
    assert all(c["mode"] == "managed" for c in ch.values())
    # One change per instance, and every planned instance has one (F30: for_each expands).
    instances = plan["planned_values"]["root_module"].get("resources", [])
    assert len(ch) == len(instances) == len(EXPECTED_ADDRESSES)


@needs_plan
def test_plan_sp_has_two_assignments_in_ci_group(plan):
    """Two in the CI group, plus Task 3's history reader on the sweeper workflow, and nothing else."""
    cfg = config(plan)
    sp = "azuread_service_principal.dispatcher.object_id"
    ra = {a: r for a, r in cfg.items() if r["type"] == "azurerm_role_assignment"}
    mine = {a for a, r in ra.items() if sp in refs(r, "principal_id")}
    assert mine == {"azurerm_role_assignment.dispatcher", "azurerm_role_assignment.dispatcher_cost_reader",
                    "azurerm_role_assignment.history_reader"}
    for a in mine - {"azurerm_role_assignment.history_reader"}:
        if "azurerm_resource_group.ci.id" not in refs(ra[a], "scope"):
            pytest.fail(f"{a} is not scoped to the CI group")
    if "azapi_resource.sweeper.id" not in refs(ra["azurerm_role_assignment.history_reader"], "scope"):
        pytest.fail("the history reader is not scoped to the sweeper workflow")
    # The sweeper's own identity holds exactly one assignment, on the CI group.
    msi = {a for a, r in ra.items() if any(x.startswith("azapi_resource.sweeper.identity") for x in refs(r, "principal_id"))}
    assert msi == {"azurerm_role_assignment.sweeper"}
    if "azurerm_resource_group.ci.id" not in refs(ra["azurerm_role_assignment.sweeper"], "scope"):
        pytest.fail("the sweeper's assignment is not scoped to the CI group")
    after = {a: changes(plan)[a]["change"]["after"] for a in mine}
    assert after["azurerm_role_assignment.dispatcher_cost_reader"]["role_definition_name"] == "Cost Management Reader"
    assert all(v["principal_type"] == "ServicePrincipal" for v in after.values())
    # F6: the name is the uuidv5 the sweeper's lock depends on. Compared silently (F14).
    want = str(uuid.uuid5(uuid.NAMESPACE_URL, "realliance-ci/dispatcher-assignment"))
    if after["azurerm_role_assignment.dispatcher"].get("name") != want:
        pytest.fail("the dispatcher assignment's planned name is not uuidv5(url, realliance-ci/dispatcher-assignment)")


@needs_plan
def test_plan_lock_target_is_deterministic(plan):
    """Review I1: the output reads the local, and no expression reads the assignment's id."""
    out = plan["configuration"]["root_module"]["outputs"]["dispatcher_assignment_id"]["expression"]
    assert out.get("references") == ["local.dispatcher_assignment_id"]
    blob = json.dumps(plan["configuration"])
    assert "azurerm_role_assignment.dispatcher.id" not in blob


@needs_plan
def test_plan_matches_files(plan):
    """The planned values equal fence.json and the policy files, so nothing drifts between them."""
    f = fence()
    after = {a: c["change"]["after"] for a, c in changes(plan).items()}
    for key in POLICY_FILES:
        p = after[f'azurerm_policy_definition.fence["{key}"]']
        assert json.loads(p["policy_rule"]) == policy(key)["policyRule"], key
        assert p["mode"] == "Indexed" and p["policy_type"] == "Custom", key
    role = after["azurerm_role_definition.dispatcher"]
    assert role["name"] == "Realliance CI dispatcher"
    perms = role["permissions"]
    assert len(perms) == 1 and perms[0]["actions"] == f["dispatcher_actions"]
    assert not any(perms[0].get(k) for k in ("not_actions", "data_actions", "not_data_actions"))
    assert after["azurerm_consumption_budget_resource_group.ci"]["amount"] == f["budget_ci_usd"]
    assert after["azurerm_consumption_budget_resource_group.guard"]["amount"] == f["budget_guard_usd"]
    assert after["azurerm_resource_group.ci"]["name"] == f["ci_group"]
    assert after["azurerm_resource_group.guard"]["name"] == f["guard_group"]
    assert after["azurerm_resource_group.ci"]["location"] == f["region"]
    assert after["time_rotating.dispatcher"]["rotation_days"] == 150
    assert after["azurerm_resource_group_policy_assignment.fence"]["enforce"] is True
    # Task 3: the sweeper's role, the history reader, and the failed-run alert.
    sweeper = after["azurerm_role_definition.sweeper"]
    assert sweeper["name"] == "Realliance CI sweeper"
    assert len(sweeper["permissions"]) == 1 and sweeper["permissions"][0]["actions"] == f["sweeper"]["actions"]
    assert not any(sweeper["permissions"][0].get(k) for k in ("not_actions", "data_actions", "not_data_actions"))
    reader = after["azurerm_role_definition.history_reader"]
    assert reader["name"] == "Realliance CI sweeper history reader"
    assert len(reader["permissions"]) == 1
    assert reader["permissions"][0]["actions"] == ["Microsoft.Logic/workflows/runs/read"]
    assert not any(reader["permissions"][0].get(k) for k in ("not_actions", "data_actions", "not_data_actions"))
    assert after["azurerm_role_assignment.sweeper"]["condition_version"] == "2.0"
    alert = after["azurerm_monitor_metric_alert.sweeper_failed"]
    assert (alert["frequency"], alert["window_size"], alert["enabled"]) == ("PT5M", "PT15M", True)
    assert [(c["metric_name"], c["aggregation"], c["operator"], c["threshold"]) for c in alert["criteria"]] == [
        ("RunsFailed", "Total", "GreaterThan", 0)]
    assert after["azurerm_monitor_action_group.sweeper_failed"]["name"] == "ci-sweeper-failed"
    wf = after["azapi_resource.sweeper"]
    assert (wf["type"], wf["name"], wf["location"]) == ("Microsoft.Logic/workflows@2019-05-01", "ci-sweeper",
                                                        f["region"])
    assert [i["type"] for i in wf["identity"]] == ["SystemAssigned"]
