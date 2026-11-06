# The lock and its restore (F6, Task 2 review I1), with mocked providers: nothing reaches Azure.
#
# After a lock the CI group still exists and the dispatcher's assignment doesn't, so a restore
# plan knows the group's id and re-creates the assignment. The sweeper's input,
# local.dispatcher_assignment_id, has to be a value that plan knows. Taken from the assignment's
# computed id, it would be unknown, and the sweeper would plan a change during every restore.
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

# The group exists, so planning knows its id. The assignment's id has no override, so it stays
# unknown until apply, as in a real restore.
override_resource {
  target          = azurerm_resource_group.ci
  override_during = plan
  values = {
    id = "/subscriptions/test/resourceGroups/rg-realliance-ci"
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
