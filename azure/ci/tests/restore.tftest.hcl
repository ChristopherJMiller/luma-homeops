# The lock and its restore (F6, Task 2 review I1), with mocked providers: nothing reaches Azure.
#
# After a lock the CI group still exists and the dispatcher's assignment doesn't, so a restore
# plan knows the group's id and re-creates the assignment. The sweeper's input,
# local.dispatcher_assignment_id, has to be a value that plan knows. Taken from the assignment's
# computed id, it would be unknown, and the sweeper would plan a change during every restore. The
# restore run also asserts that plan time knows the sweeper's whole workflow body (Task 3).
#
# scripts/test_azure_ci.py runs this from a copy of the module without ci.secret.auto.tfvars.

mock_provider "azurerm" {}

# azuread validates owners as UUIDs; uuidv5 keeps a GUID literal out of this file.
mock_provider "azuread" {
  mock_data "azuread_client_config" {
    defaults = {
      object_id = uuidv5("url", "realliance-ci/test-owner")
      tenant_id = uuidv5("url", "realliance-ci/test-tenant")
      client_id = uuidv5("url", "realliance-ci/test-client")
    }
  }
}

mock_provider "azapi" {}

mock_provider "time" {
  mock_resource "time_rotating" {
    defaults = {
      id = "2026-11-05T00:00:00Z"
    }
  }
}

variables {
  subscription_id = uuidv5("url", "realliance-ci/test-subscription")
  alert_emails    = ["ci@example.invalid"]
  want_id         = "/subscriptions/test/resourceGroups/rg-realliance-ci/providers/Microsoft.Authorization/roleAssignments/${uuidv5("url", "realliance-ci/dispatcher-assignment")}"
}

# The groups exist, so planning knows their ids. The assignment's id has no override, so it stays
# unknown until apply, as in a real restore.
override_resource {
  target          = azurerm_resource_group.ci
  override_during = plan
  values = {
    id = "/subscriptions/test/resourceGroups/rg-realliance-ci"
  }
}

override_resource {
  target          = azurerm_resource_group.guard
  override_during = plan
  values = {
    id = "/subscriptions/test/resourceGroups/rg-realliance-ci-guard"
  }
}

run "restore_plan_knows_the_lock_target" {
  command = plan

  assert {
    condition     = local.dispatcher_assignment_id == var.want_id
    error_message = "local.dispatcher_assignment_id is unknown or wrong at plan time while the CI group exists."
  }

  assert {
    condition     = output.dispatcher_assignment_id == var.want_id
    error_message = "The dispatcher_assignment_id output isn't the deterministic id at plan time."
  }

  # jsonencode of a value with any unknown part is unknown, and an unknown condition fails the run,
  # so this passes only if the plan knows every part of the sweeper's body. The restore plan then
  # leaves the workflow unchanged (1 to add, 0 to change).
  assert {
    condition     = length(jsonencode(azapi_resource.sweeper.body)) > 0
    error_message = "The sweeper's workflow body is unknown at plan time, so a restore would change the sweeper."
  }

  assert {
    condition     = azapi_resource.sweeper.body.properties.definition.actions.Trip.actions.Delete_dispatcher_assignment.inputs.uri == "https://management.azure.com${var.want_id}"
    error_message = "The sweeper's lock doesn't delete the dispatcher's assignment in the restore plan."
  }

  assert {
    condition     = azapi_resource.sweeper.body.properties.state == "Enabled"
    error_message = "The sweeper must be enabled."
  }
}

# The postcondition, checked at plan time by giving the assignment a known id. Azure's id must equal
# the deterministic one, whatever case Azure returns.
run "postcondition_accepts_the_deterministic_id" {
  command = plan

  override_resource {
    target          = azurerm_role_assignment.dispatcher
    override_during = plan
    values = {
      id = "/subscriptions/test/resourcegroups/rg-realliance-ci/providers/Microsoft.Authorization/roleAssignments/${uuidv5("url", "realliance-ci/dispatcher-assignment")}"
    }
  }
}

run "postcondition_refuses_another_id" {
  command = plan

  override_resource {
    target          = azurerm_role_assignment.dispatcher
    override_during = plan
    values = {
      id = "/subscriptions/test/resourceGroups/rg-realliance-ci/providers/Microsoft.Authorization/roleAssignments/other"
    }
  }

  expect_failures = [azurerm_role_assignment.dispatcher]
}
