# Layer 7 of the runner-classes spec: the cloud sweeper (runner classes phase 2, Task 3). A Logic
# App in the guard group runs every 10 min under its own managed identity, and reads nothing the
# dispatcher writes, so it holds even against a compromised dispatcher.
#
# One run, in the order of sweeper/workflow.json.tftpl (scripts/test_azure_ci.py runs it, sweep
# after sweep, in a simulator against a fake ARM):
# 1. Read the CI group's container groups, its resources with createdTime, the CI budget, the
#    group's tags, and the dispatcher's role assignment.
# 2. The lock state. The tag ci-sweeper-lock holds the createdOn of the assignment instance a lock
#    aims at. The lock is PENDING while that instance or an older one exists, or none does. It ends
#    only when a NEWER instance exists (RESTORED), and only Chris's apply can make one: the
#    dispatcher holds no Authorization or tag right, and the sweeper can only delete. An unreadable
#    assignment is neither, so a transient read error never locks.
# 3. Lock first (M1) when the budget's currentSpend is at budget_ci_usd, the assignment is gone, or
#    a lock is pending: straight to step 6, with no egress or age work before it.
# 4. Otherwise, for every container group, whatever its name or tags: read its egress over its life
#    and over the last 10 min, delete it at or over the per-job cap, and add its shape price and
#    its egress rate to the burn (E17). Bytes over the life are the metric's average times the
#    seconds since creation; the query starts a minute early so a brand-new group never asks for
#    an empty span. Then delete compute older than max_age_minutes.
# 5. The rate trip: the burn over the ceiling with ci-sweeper-over-at holding the previous slot.
#    The tag stores the recurrence slot, trigger().scheduledTime, never the clock, so a slow or
#    queued run still sees the previous slot (I2).
# 6. The trip deletes the assignment FIRST, unless it's already gone (M2). Then, in parallel, it
#    deletes every container group, from the ACI list or else the RG's list (M3), and marks
#    ci-sweeper-lock when no lock is pending. It ends Failed, code Locked, when the assignment is
#    gone or its DELETE got a 2xx or a 404; else Cancelled (LockFailed), and every later sweep
#    retries (F6, F7, I1).
# 7. With no trip: over the ceiling, write the slot into ci-sweeper-over-at, else remove it; on
#    RESTORED, remove ci-sweeper-lock.
# 8. Any read, delete, or tag write that failed, or a list with a next page, ends the run Failed,
#    code Degraded. Each outcome has its own alert, below.
#
# Every HTTP action has a 1-min timeout and one retry, the trigger runs one run at a time, and the
# loops run over finite lists, so a hung request holds the queue for minutes, never for good. The
# dispatcher's CISweeperStale (Tasks 15 and 18) covers a sweeper that stops running at all.

locals {
  sweeper = local.fence.sweeper

  # Every input is a constant or an id a plan knows while the CI group exists, so a restore plan
  # after a lock leaves the workflow unchanged (F6, tests/restore.tftest.hcl). one() fails the plan
  # if delete_types grows: the template's DELETE carries the container groups' api-version.
  sweeper_definition = jsondecode(templatefile("${path.module}/sweeper/workflow.json.tftpl", {
    ci_group_id                = azurerm_resource_group.ci.id
    dispatcher_assignment_id   = local.dispatcher_assignment_id
    budget_name                = azurerm_consumption_budget_resource_group.ci.name
    interval_minutes           = local.sweeper.interval_minutes
    max_age_minutes            = local.sweeper.max_age_minutes
    ceiling_micro_per_hour     = local.sweeper.ceiling_micro_per_hour
    repeat_window_minutes      = local.sweeper.repeat_window_minutes
    action_timeout             = local.sweeper.action_timeout
    price_2_8                  = local.sweeper.price_micro_per_hour["2/8"]
    price_4_16                 = local.sweeper.price_micro_per_hour["4/16"]
    price_other                = local.sweeper.price_micro_per_hour["other"]
    delete_type                = one(local.sweeper.delete_types)
    egress_rate_window_minutes = local.sweeper.egress_rate_window_minutes
    budget_ci_usd              = local.fence.budget_ci_usd
    egress_cap_bytes           = local.fence.egress.cap_bytes
    egress_micro_per_gb        = local.fence.egress.micro_per_gb
    egress_metric              = local.fence.egress.metric
  }))

  # ABAC on the sweeper's assignment: of all role assignments, it may delete only those of the
  # dispatcher's role, so never Cost Management Reader or the history reader. If the apply refuses
  # the condition, drop condition and condition_version and ledger it (plan's call).
  sweeper_lock_condition = "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${azurerm_role_definition.dispatcher.role_definition_id}}))"
}

# Layer 7: the sweeper. azapi, because no azurerm resource takes a raw workflow definition.
resource "azapi_resource" "sweeper" {
  type      = "Microsoft.Logic/workflows@2019-05-01"
  name      = "ci-sweeper"
  parent_id = azurerm_resource_group.guard.id
  location  = local.fence.region

  identity {
    type = "SystemAssigned"
  }

  body = {
    properties = {
      state      = "Enabled"
      definition = local.sweeper_definition
    }
  }

  tags = {
    ManagedBy = "terraform"
    Purpose   = "realliance-ci-sweeper"
  }

  response_export_values = []

  # The first run comes after the dispatcher's assignment exists, because a missing assignment
  # reads as locked. Ordering only: a restore plan doesn't change the workflow.
  depends_on = [azurerm_role_assignment.dispatcher]
}

# Layer 7: what the sweeper may do in the CI group, exactly fence.json's sweeper.actions.
resource "azurerm_role_definition" "sweeper" {
  name        = "Realliance CI sweeper"
  scope       = azurerm_resource_group.ci.id
  description = "Runner classes spec, layer 7: list and delete CI compute, read the CI budget, metrics and tags, write the sweeper's tag, and delete the dispatcher's role assignment (the lock)."

  permissions {
    actions          = local.sweeper.actions
    not_actions      = []
    data_actions     = []
    not_data_actions = []
  }

  assignable_scopes = [azurerm_resource_group.ci.id]
}

# The sweeper's identity holds its role on the CI group only. The first runs after the apply may fail
# with 403 until the assignment propagates, which sends one failed-run email.
resource "azurerm_role_assignment" "sweeper" {
  scope                            = azurerm_resource_group.ci.id
  role_definition_id               = azurerm_role_definition.sweeper.role_definition_resource_id
  principal_id                     = azapi_resource.sweeper.identity[0].principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
  description                      = "ci-sweeper: delete stale or runaway CI compute, and lock the dispatcher out."

  condition_version = "2.0"
  condition         = local.sweeper_lock_condition
}

# The dispatcher reads the sweeper's run history (E8) to raise CISweeperStale. A custom role can't be
# assignable at a single resource, so the role may go anywhere in the guard group, and its one
# assignment puts it on the workflow. The lock leaves it in place.
resource "azurerm_role_definition" "history_reader" {
  name        = "Realliance CI sweeper history reader"
  scope       = azurerm_resource_group.guard.id
  description = "Runner classes spec, layer 7: read the sweeper's run history."

  permissions {
    actions          = ["Microsoft.Logic/workflows/runs/read"]
    not_actions      = []
    data_actions     = []
    not_data_actions = []
  }

  assignable_scopes = [azurerm_resource_group.guard.id]
}

resource "azurerm_role_assignment" "history_reader" {
  scope              = azapi_resource.sweeper.id
  role_definition_id = azurerm_role_definition.history_reader.role_definition_resource_id
  principal_id       = azuread_service_principal.dispatcher.object_id
  principal_type     = "ServicePrincipal"
  description        = "ci-dispatch: read the sweeper's run history for CISweeperStale."
}

# The alerts, one per outcome, so none masks another (Task 3 review M5). Logic Apps Consumption
# exposes two failing run statuses as metrics, so:
# - Degraded ends a run Failed: RunsFailed (a Locked run also counts there);
# - LockFailed ends a run Cancelled, the one code that does: RunsCancelled;
# - Locked is the assignment's own delete, from the CI group's Activity Log, once per lock.
# All three email alert_emails through one action group.
resource "azurerm_monitor_action_group" "sweeper_failed" {
  name                = "ci-sweeper-failed"
  resource_group_name = azurerm_resource_group.guard.name
  short_name          = "ci-sweeper"

  # The addresses are sensitive and their count isn't, so the blocks iterate over indices.
  dynamic "email_receiver" {
    for_each = range(nonsensitive(length(var.alert_emails)))
    content {
      name                    = "email-${email_receiver.value}"
      email_address           = var.alert_emails[email_receiver.value]
      use_common_alert_schema = true
    }
  }
}

resource "azurerm_monitor_metric_alert" "sweeper_failed" {
  name                = "ci-sweeper-failed"
  resource_group_name = azurerm_resource_group.guard.name
  scopes              = [azapi_resource.sweeper.id]
  description         = "The CI sweeper ended a run Failed: Degraded (a read, delete or tag write failed) or Locked. Read the run's error in the portal."
  severity            = 1
  enabled             = true
  frequency           = "PT5M"
  window_size         = "PT15M"

  criteria {
    metric_namespace = "Microsoft.Logic/workflows"
    metric_name      = "RunsFailed"
    aggregation      = "Total"
    operator         = "GreaterThan"
    threshold        = 0
  }

  action {
    action_group_id = azurerm_monitor_action_group.sweeper_failed.id
  }
}

resource "azurerm_monitor_metric_alert" "sweeper_lock_failed" {
  name                = "ci-sweeper-lock-failed"
  resource_group_name = azurerm_resource_group.guard.name
  scopes              = [azapi_resource.sweeper.id]
  description         = "The CI sweeper decided to lock the dispatcher, and the assignment delete failed (the run ends Cancelled). Every sweep retries; read the run's Lock_failed step."
  severity            = 0
  enabled             = true
  frequency           = "PT5M"
  window_size         = "PT15M"

  criteria {
    metric_namespace = "Microsoft.Logic/workflows"
    metric_name      = "RunsCancelled"
    aggregation      = "Total"
    operator         = "GreaterThan"
    threshold        = 0
  }

  action {
    action_group_id = azurerm_monitor_action_group.sweeper_failed.id
  }
}

resource "azurerm_monitor_activity_log_alert" "sweeper_locked" {
  name                = "ci-sweeper-locked"
  resource_group_name = azurerm_resource_group.guard.name
  location            = "global"
  scopes              = [azurerm_resource_group.ci.id]
  description         = "A role assignment in the CI group was deleted: the sweeper's lock, or anyone else. Paid CI is down until a terraform apply of azure/ci restores it."
  enabled             = true

  criteria {
    category       = "Administrative"
    operation_name = "Microsoft.Authorization/roleAssignments/delete"
    statuses       = ["Succeeded"]
  }

  action {
    action_group_id = azurerm_monitor_action_group.sweeper_failed.id
  }
}
