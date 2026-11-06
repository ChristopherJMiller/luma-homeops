# Paid CI on Azure Container Instances, the fence: two resource groups, two budgets, Azure Policy,
# the dispatcher's custom role and its service principal (runner classes phase 2, Task 2).
#
# Spec: the "Budget enforcement and backstops" section of
# docs/superpowers/specs/2026-11-04-runner-classes-design.md. This module implements layers 5
# (the role and Policy on the CI group), 7 (the sweeper and its lock, sweeper.tf, Task 3) and 8
# (the CI group's budget). Layer 9, the $120 subscription budget, stays in azure/logging/.
#
# Every number lives in fence.json, which scripts/test_azure_ci.py pins.
#
# Chris applies, always: ./azure/ci/tf.sh apply <plan>. An agent stops at plan.

terraform {
  required_version = ">= 1.5"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "3.9.0"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.12"
    }
    # The sweeper's Logic App (sweeper.tf). azurerm has no resource that takes a raw workflow
    # definition with a managed identity, so azapi creates that one resource and nothing else.
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.0"
    }
  }

  # Shared homelab state backend (created by azure/bootstrap), different key.
  backend "azurerm" {
    resource_group_name  = "rg-luma-homelab-terraform-state"
    storage_account_name = "lumahomelabstateq978tb72"
    container_name       = "terraform-state"
    key                  = "realliance-ci.tfstate"
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id

  # Register nothing as a side effect. On 2026-11-05 (Task 2 Step 1) Microsoft.ContainerInstance
  # and Microsoft.Logic both read Registered, so this module declares no
  # azurerm_resource_provider_registration.
  resource_provider_registrations = "none"
}

# Auth comes from `az login`, the same session the state backend uses.
provider "azuread" {}

# The sweeper's workflow only (sweeper.tf). Like azurerm, it registers nothing as a side effect.
provider "azapi" {
  subscription_id            = var.subscription_id
  skip_provider_registration = true
}

locals {
  fence = jsondecode(file("${path.module}/fence.json"))
}

# The CI group holds job compute only, behind the Policy fence (fence.tf). The dispatcher can write
# here and nowhere else.
resource "azurerm_resource_group" "ci" {
  name     = local.fence.ci_group
  location = local.fence.region

  tags = {
    ManagedBy = "terraform"
    Purpose   = "realliance-ci-compute"
  }

  lifecycle {
    # The sweeper (Task 3) keeps its state in two tags, and only it writes them: "two sweeps in a
    # row" (ci-sweeper-over-at) and a pending lock (ci-sweeper-lock). Terraform must leave both, so a
    # restore plan stays 1 to add, 0 to change.
    ignore_changes = [tags["ci-sweeper-over-at"], tags["ci-sweeper-lock"]]
  }
}

# The guard group holds the sweeper (Task 3). The dispatcher has no rights in it.
resource "azurerm_resource_group" "guard" {
  name     = local.fence.guard_group
  location = local.fence.region

  tags = {
    ManagedBy = "terraform"
    Purpose   = "realliance-ci-guard"
  }
}

# Layer 8: the CI group's budget over the calendar month. It emails at 80 % and 100 %. The lock
# comes from the sweeper, which reads this budget's currentSpend every run (E5).
resource "azurerm_consumption_budget_resource_group" "ci" {
  name              = "realliance-ci"
  resource_group_id = azurerm_resource_group.ci.id

  amount     = local.fence.budget_ci_usd
  time_grain = "Monthly"

  time_period {
    # Azure refuses a budget whose start month is in the past when it's created or replaced. Bump
    # this to the current month if the apply slips past November 2026.
    start_date = "2026-11-01T00:00:00Z"
    end_date   = "2030-12-31T23:59:59Z"
  }

  notification {
    enabled        = true
    threshold      = 80
    operator       = "GreaterThan"
    threshold_type = "Actual"
    contact_emails = var.alert_emails
  }

  notification {
    enabled        = true
    threshold      = 100
    operator       = "GreaterThan"
    threshold_type = "Actual"
    contact_emails = var.alert_emails
  }

  lifecycle {
    precondition {
      # It must exceed the code's period ceiling, or legitimate use would trip the lock (F30).
      condition     = local.fence.budget_ci_usd > local.fence.paid_cap_usd
      error_message = "fence.json: budget_ci_usd must exceed paid_cap_usd."
    }
  }
}

# The guard group's budget (E6): email only. It catches a runaway sweeper.
resource "azurerm_consumption_budget_resource_group" "guard" {
  name              = "realliance-ci-guard"
  resource_group_id = azurerm_resource_group.guard.id

  amount     = local.fence.budget_guard_usd
  time_grain = "Monthly"

  time_period {
    # Same rule as the CI budget: the start month can't be in the past at creation.
    start_date = "2026-11-01T00:00:00Z"
    end_date   = "2030-12-31T23:59:59Z"
  }

  notification {
    enabled        = true
    threshold      = 80
    operator       = "GreaterThan"
    threshold_type = "Actual"
    contact_emails = var.alert_emails
  }

  notification {
    enabled        = true
    threshold      = 100
    operator       = "GreaterThan"
    threshold_type = "Actual"
    contact_emails = var.alert_emails
  }
}
