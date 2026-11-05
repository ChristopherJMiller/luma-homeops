# Layer 5 of the runner-classes spec: the custom role and Azure Policy on the CI group. They stop
# other regions, sizes, shapes, and resource types at create time, whatever the dispatcher sends.
#
# The dispatcher's identity lives here too: an app registration, its service principal, and a
# client secret that rotates inside the spec's 6-month limit.

locals {
  # One Policy definition per file in policies/. scripts/test_azure_ci.py pins the set of files and
  # every rule in them.
  policies = {
    for f in fileset("${path.module}/policies", "*.json") :
    trimsuffix(f, ".json") => jsondecode(file("${path.module}/policies/${f}"))
  }
}

# Layer 5, Policy. Custom definitions live at subscription scope, and only the initiative's
# assignment on the CI group puts them to work.
resource "azurerm_policy_definition" "fence" {
  for_each = local.policies

  name         = "realliance-ci-${each.key}"
  policy_type  = "Custom"
  mode         = each.value.mode
  display_name = each.value.displayName
  description  = each.value.description
  policy_rule  = jsonencode(each.value.policyRule)
}

# Layer 5, Policy: the initiative realliance-ci-fence bundles the definitions from policies/.
resource "azurerm_policy_set_definition" "fence" {
  name         = "realliance-ci-fence"
  policy_type  = "Custom"
  display_name = "Realliance CI fence"
  description  = "Runner classes spec, layer 5: what a paid CI job's container group may be. Deny only."

  dynamic "policy_definition_reference" {
    for_each = azurerm_policy_definition.fence
    content {
      policy_definition_id = policy_definition_reference.value.id
      reference_id         = policy_definition_reference.key
    }
  }
}

# Layer 5, Policy: the initiative's assignment on the CI group, enforced. A new assignment can take
# up to 30 min to start denying, so a smoke test right after the apply waits for it.
resource "azurerm_resource_group_policy_assignment" "fence" {
  name                 = "realliance-ci-fence"
  resource_group_id    = azurerm_resource_group.ci.id
  policy_definition_id = azurerm_policy_set_definition.fence.id
  display_name         = "Realliance CI fence"
  enforce              = true

  non_compliance_message {
    content = "Denied by the realliance-ci-fence initiative: see azure/ci/policies/ in luma-homeops."
  }
}

# Layer 5, role: exactly the rights in fence.json (E8), assignable on the CI group only. No exec,
# logs, start, stop, or Microsoft.Authorization action. VM, disk, and network rights arrive with
# phase 3.
resource "azurerm_role_definition" "dispatcher" {
  name        = "Realliance CI dispatcher"
  scope       = azurerm_resource_group.ci.id
  description = "Runner classes spec, layer 5: create, read, and delete ACI container groups in the CI group, and read their metrics."

  permissions {
    actions          = local.fence.dispatcher_actions
    not_actions      = []
    data_actions     = []
    not_data_actions = []
  }

  assignable_scopes = [azurerm_resource_group.ci.id]
}

data "azuread_client_config" "current" {}

# The dispatcher's identity: ci-dispatch authenticates as this app with client credentials.
resource "azuread_application" "dispatcher" {
  display_name            = "realliance-ci-dispatch"
  owners                  = [data.azuread_client_config.current.object_id]
  prevent_duplicate_names = true
}

resource "azuread_service_principal" "dispatcher" {
  client_id = azuread_application.dispatcher.client_id
  owners    = [data.azuread_client_config.current.object_id]
}

# The credential lasts at most 180 days (4320h), inside the spec's 6 months. At 150 days this
# resource rotates, and the next apply replaces the password and destroys the old one in the same
# run. The dispatcher fails auth until the new secret ships, so rotation is one STOP done back to
# back (F26): Chris's apply, the pipe into sso-secrets, the reseal, and the deploy push.
resource "time_rotating" "dispatcher" {
  rotation_days = 150
}

resource "azuread_application_password" "dispatcher" {
  application_id      = azuread_application.dispatcher.id
  display_name        = "ci-dispatch"
  end_date            = timeadd(time_rotating.dispatcher.id, "4320h")
  rotate_when_changed = { rotation = time_rotating.dispatcher.id }

  lifecycle {
    create_before_destroy = true
  }
}

# Layer 5, role: the custom role on the CI group. This is the assignment the sweeper's lock deletes
# (Task 3), so it has a fixed name (F6). A restore re-creates the same id the sweeper's definition
# names, and no GUID sits in a file. The Policy assignment goes first, so the dispatcher never holds
# write rights in an unfenced group.
resource "azurerm_role_assignment" "dispatcher" {
  name               = uuidv5("url", "realliance-ci/dispatcher-assignment")
  scope              = azurerm_resource_group.ci.id
  role_definition_id = azurerm_role_definition.dispatcher.role_definition_resource_id
  principal_id       = azuread_service_principal.dispatcher.object_id
  principal_type     = "ServicePrincipal"
  description        = "ci-dispatch: launch and delete paid CI container groups. The sweeper's lock deletes this assignment; a terraform apply restores it."

  depends_on = [azurerm_resource_group_policy_assignment.fence]
}

# Cost Management Reader on the CI group, for the daily drift check. The lock leaves it in place, so
# a locked dispatcher can still report that it's locked (E5).
resource "azurerm_role_assignment" "dispatcher_cost_reader" {
  scope                = azurerm_resource_group.ci.id
  role_definition_name = "Cost Management Reader"
  principal_id         = azuread_service_principal.dispatcher.object_id
  principal_type       = "ServicePrincipal"
}
