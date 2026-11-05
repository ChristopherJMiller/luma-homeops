"""Static tests for azure/ci/, the fence around paid CI on Azure Container Instances.

    python -m pytest -q -p no:cacheprovider scripts/test_azure_ci.py

Runs as the `azure-ci` pre-commit hook (runner classes phase 2, Tasks 2 and 3). Every value a test
pins comes from azure/ci/fence.json, and test_fence_values pins fence.json itself.

The plan-level tests run only when AZURE_CI_PLAN_JSON names the output of

    umask 077; ./azure/ci/tf.sh show -json "$SCRATCH/ci.tfplan" > "$SCRATCH/plan.json"

That file holds the subscription id and other Azure ids, so it stays in a mode-0700 scratch dir.
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

# Every resource instance the plan creates (F30: for_each and count expand). Task 3 adds its own.
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
}

SP = "azuread_service_principal.dispatcher.object_id"
CI_GROUP = "azurerm_resource_group.ci.id"

# Every role assignment in azure/ci, anywhere in any .tf file: {name: (principal, role, scope)}. A new
# one fails test_role_assignments_allow_listed until it's added here on purpose. Task 3 adds the
# sweeper identity's assignment and the history reader.
ROLE_ASSIGNMENTS = {
    "dispatcher": (SP, ("role_definition_id", "azurerm_role_definition.dispatcher.role_definition_resource_id"),
                   CI_GROUP),
    "dispatcher_cost_reader": (SP, ("role_definition_name", '"Cost Management Reader"'), CI_GROUP),
}

# Where the dispatcher's service principal may hold any role (E8). Task 3 adds the sweeper workflow,
# for the history reader.
DISPATCHER_SCOPES = {CI_GROUP}

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


def blocks(text: str, kind: str, rtype: str) -> dict[str, str]:
    """{name: body} for every `kind "rtype" "name" {` block in text."""
    pat = re.compile(rf'^{kind}\s+"{re.escape(rtype)}"\s+"([^"]+)"\s*\{{', re.M)
    return {m.group(1): _body(text, m.end()) for m in pat.finditer(text)}


def block(text: str, kind: str, rtype: str, name: str) -> str:
    found = blocks(text, kind, rtype)
    assert name in found, f'{kind} "{rtype}" "{name}" not found'
    return found[name]


def labelled(text: str, kind: str) -> dict[str, str]:
    """{label: body} for every one-label block (`output "x" {`, `provider "x" {`, `variable "x" {`)."""
    pat = re.compile(rf'^{kind}\s+"([^"]+)"\s*\{{', re.M)
    return {m.group(1): _body(text, m.end()) for m in pat.finditer(text)}


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
    if not (CI / "sweeper.tf").exists():
        pytest.skip("sweeper.tf arrives with Task 3, which makes this test unconditional")
    assert re.search(r"dispatcher_assignment_id\s*=\s*local\.dispatcher_assignment_id\b", tf("sweeper.tf"))


def test_terraform_tests(tmp_path):
    """azure/ci/tests/*.tftest.hcl with mocked providers (review I1).

    The restore scenario: with the CI group's id known and the assignment's unknown, plan time
    already knows the lock's target. The postcondition accepts the deterministic id in any case and
    refuses another. Runs from a copy without ci.secret.auto.tfvars, which terraform test would
    otherwise load.
    """
    data = CI / ".terraform"
    if not (data / "providers").is_dir():
        pytest.skip("run ./azure/ci/tf.sh init first: terraform test needs the providers")
    mod = tmp_path / "ci"
    mod.mkdir()
    for src in [*CI.glob("*.tf"), CI / "fence.json", CI / ".terraform.lock.hcl"]:
        shutil.copy2(src, mod / src.name)
    for d in ("policies", "tests"):
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
    """fence.tf grants nothing at subscription or management-group scope."""
    code = re.sub(r"#.*", "", tf("fence.tf"))
    for bad in ("/subscriptions/", "azurerm_subscription", "subscription_id", "management_group"):
        assert bad not in code, bad


def test_role_assignments_allow_listed():
    """Review I2: every role assignment in every .tf file is in ROLE_ASSIGNMENTS, exactly.

    The dispatcher's service principal holds roles only at DISPATCHER_SCOPES. An assignment
    declared in main.tf, or any new one, fails until it's added on purpose.
    """
    code = re.sub(r"#.*", "", all_tf())
    found = blocks(code, "resource", "azurerm_role_assignment")
    assert set(found) == set(ROLE_ASSIGNMENTS)
    for name, body in found.items():
        principal, (role_key, role), scope = ROLE_ASSIGNMENTS[name]
        assert has(body, "principal_id", principal), name
        assert has(body, role_key, role), name
        assert has(body, "scope", scope), name
        assert not re.search(r"^\s*(count|for_each)\s*=", body, re.M), name
        if principal == SP:
            assert scope in DISPATCHER_SCOPES, name
            assert has(body, "principal_type", '"ServicePrincipal"'), name
    # Nothing else that grants: no other role-assignment or Graph-permission resource, no
    # Authorization resource through azapi, and no Graph permission on the dispatcher's app.
    for bad in ("azuread_app_role_assignment", "azuread_directory_role_assignment",
                "azuread_directory_role_member", "azuread_service_principal_delegated_permission_grant",
                "azurerm_role_management_policy", "azurerm_pim_"):
        assert bad not in code, bad
    assert not re.search(r'type\s*=\s*"Microsoft\.Authorization/', code)
    assert "required_resource_access" not in block(code, "resource", "azuread_application", "dispatcher")


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


@pytest.fixture
def tf_rig(tmp_path):
    """tf.sh in a scratch repo, with stub `az` and `terraform` first on PATH.

    The stub terraform prints a fresh UUID (upper and lower case) on both streams, its umask, and
    exits with $STUB_RC. Nothing here touches Azure or the real module.
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
        'exit "${STUB_RC:-0}"\n'
    )
    for f in ("az", "terraform"):
        (bin_ / f).chmod(0o755)
    ident = str(uuid.uuid4())
    env = clean_env(PATH=f"{bin_}:{os.environ['PATH']}", STUB_ID=ident, STUB_UP=ident.upper())

    def run(*args, **extra):
        return subprocess.run([str(root / "azure/ci/tf.sh"), *args], capture_output=True, text=True,
                              env={**env, **extra})

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


@pytest.mark.parametrize("args", [("output", "-raw", "dispatcher_client_id"), ("output", "-json"),
                                  ("show", "-json", "x.tfplan")])
def test_tf_sh_data_paths_stay_raw(tf_rig, args):
    """`output -raw|-json` and `show -json` feed pipes and 0600 files, so stdout stays exact.
    stderr is still redacted, and the exit code survives."""
    run, ident = tf_rig
    r = run(*args, STUB_RC="2")
    assert r.returncode == 2
    assert r.stdout.startswith(f"out {ident} {ident.upper()} ")
    assert r.stderr.strip() == "err <id> <id>"


def test_tf_sh_refuses_without_az(tf_rig):
    run, _ = tf_rig
    r = run("plan", STUB_AZ_RC="1")
    assert r.returncode == 1 and "not logged in" in r.stderr and "out " not in r.stdout


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
    cfg = config(plan)
    sp = "azuread_service_principal.dispatcher.object_id"
    ra = {a: r for a, r in cfg.items() if r["type"] == "azurerm_role_assignment"}
    mine = {a for a, r in ra.items() if sp in refs(r, "principal_id")}
    assert mine == {"azurerm_role_assignment.dispatcher", "azurerm_role_assignment.dispatcher_cost_reader"}
    for a in mine:
        if "azurerm_resource_group.ci.id" not in refs(ra[a], "scope"):
            pytest.fail(f"{a} is not scoped to the CI group")
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
