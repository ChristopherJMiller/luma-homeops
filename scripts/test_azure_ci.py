"""Static tests for azure/ci/, the fence around paid CI on Azure Container Instances.

    python -m pytest -q -p no:cacheprovider scripts/test_azure_ci.py

Runs as the `azure-ci` pre-commit hook (runner classes phase 2, Tasks 2 and 3). Every value a test
pins comes from azure/ci/fence.json, and test_fence_values pins fence.json itself.

The plan-level tests run only when AZURE_CI_PLAN_JSON names the output of

    umask 077; ./azure/ci/tf.sh show -json "$SCRATCH/ci.tfplan" > "$SCRATCH/plan.json"

That file holds the subscription id and other Azure ids, so it stays in a mode-0700 scratch dir.
Plan-level checks compare ids silently and fail through pytest.fail with an id-free message, so a
failure never prints one. These tests never open a `*.secret.*` file.
"""

import json
import os
import re
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
    "image_like": "ghcr.io/christopherjmiller/forgejo-runner-class*",
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
# lists a rule whose alias goes missing, and the dispatcher's guard still enforces that rule.
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

GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


# --- helpers ---------------------------------------------------------------------------------------


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


# --- the dispatcher's identity and role (layer 5) --------------------------------------------------


def test_dispatcher_assignment_named():
    """F6: a fixed uuidv5 name, so a restore re-creates the id the sweeper deletes."""
    body = block(tf("fence.tf"), "resource", "azurerm_role_assignment", "dispatcher")
    assert has(body, "name", 'uuidv5("url", "realliance-ci/dispatcher-assignment")')
    assert has(body, "role_definition_id", "azurerm_role_definition.dispatcher.role_definition_resource_id")
    out = outputs(tf("outputs.tf"))["dispatcher_assignment_id"]
    assert has(out, "value", "azurerm_role_assignment.dispatcher.id")


def test_sweeper_takes_dispatcher_assignment_id():
    """The sweeper's template gets the lock's target from the assignment resource (Task 3)."""
    if not (CI / "sweeper.tf").exists():
        pytest.skip("sweeper.tf arrives with Task 3, which makes this test unconditional")
    assert re.search(r"dispatcher_assignment_id\s*=\s*azurerm_role_assignment\.dispatcher\.id",
                     tf("sweeper.tf"))


def test_dispatcher_role_exact():
    f = fence()
    body = block(tf("fence.tf"), "resource", "azurerm_role_definition", "dispatcher")
    assert has(body, "name", '"Realliance CI dispatcher"')
    assert has(body, "scope", "azurerm_resource_group.ci.id")
    assert has(body, "actions", "local.fence.dispatcher_actions")
    for empty in ("not_actions", "data_actions", "not_data_actions"):
        assert has(body, empty, "[]"), empty
    assert has(body, "assignable_scopes", "[azurerm_resource_group.ci.id]")
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
    text = tf("fence.tf")
    found = blocks(text, "resource", "azurerm_role_assignment")
    assert set(found) == {"dispatcher", "dispatcher_cost_reader"}
    for name, body in found.items():
        assert has(body, "scope", "azurerm_resource_group.ci.id"), name
        assert has(body, "principal_id", "azuread_service_principal.dispatcher.object_id"), name
        assert has(body, "principal_type", '"ServicePrincipal"'), name
    assert has(found["dispatcher_cost_reader"], "role_definition_name", '"Cost Management Reader"')
    code = re.sub(r"#.*", "", text)
    for bad in ("/subscriptions/", "azurerm_subscription", "subscription_id", "management_group"):
        assert bad not in code, bad


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

    assert f["image_like"].count("*") == 1 and f["image_like"].endswith("*")
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
    assert not re.search(r"^\s*enforce\s*=\s*false", a, re.M)


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
    assert sh.rstrip().endswith('exec terraform "$@"')


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
    assert hook["files"] == r"^(azure/ci/.*|scripts/test_azure_ci\.py|\.pre-commit-config\.yaml)$"
    assert hook["pass_filenames"] is False and hook["language"] == "system"


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
