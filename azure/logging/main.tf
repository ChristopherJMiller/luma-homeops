terraform {
  required_version = ">= 1.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
  
  # Remote state backend - run azure/bootstrap first!
  backend "azurerm" {
    resource_group_name  = "rg-luma-homelab-terraform-state"
    storage_account_name = "lumahomelabstateq978tb72"
    container_name       = "terraform-state"
    key                  = "logging.tfstate"
  }
}

provider "azurerm" {
  features {}
  subscription_id = "ddc1f03e-6903-4508-a23d-1754eb304cad"
}

# Resource Group for luma-homelab logging
resource "azurerm_resource_group" "luma_homelab_logging" {
  name     = var.resource_group_name
  location = var.location

  tags = {
    Environment = var.environment
    Purpose     = "logging"
    ManagedBy   = "terraform"
  }
}

# Log Analytics Workspace
resource "azurerm_log_analytics_workspace" "luma_homelab" {
  name                = var.workspace_name
  location            = azurerm_resource_group.luma_homelab_logging.location
  resource_group_name = azurerm_resource_group.luma_homelab_logging.name
  sku                 = "PerGB2018"
  retention_in_days   = var.retention_days
  daily_quota_gb      = var.daily_quota_gb

  tags = {
    Environment = var.environment
    Purpose     = "kubernetes-logging"
    ManagedBy   = "terraform"
  }
}

# Data Collection Rule for cost control
resource "azurerm_monitor_data_collection_rule" "luma_homelab_logging" {
  name                = "${var.workspace_name}-dcr"
  resource_group_name = azurerm_resource_group.luma_homelab_logging.name
  location            = azurerm_resource_group.luma_homelab_logging.location

  destinations {
    log_analytics {
      workspace_resource_id = azurerm_log_analytics_workspace.luma_homelab.id
      name                  = "luma-homelab-logs"
    }
  }

  data_flow {
    streams      = ["Microsoft-Syslog", "Microsoft-Event"]
    destinations = ["luma-homelab-logs"]
  }

  data_sources {
    syslog {
      facility_names = ["*"]
      log_levels     = ["Debug", "Info", "Notice", "Warning", "Error", "Critical", "Alert", "Emergency"]
      name           = "luma-homelab-syslog"
    }
  }

  tags = {
    Environment = var.environment
    Purpose     = "data-collection"
    ManagedBy   = "terraform"
  }
}

# Budget alert for cost control
resource "azurerm_consumption_budget_resource_group" "luma_homelab_logging_budget" {
  name              = "luma-homelab-logging-budget"
  resource_group_id = azurerm_resource_group.luma_homelab_logging.id

  amount     = var.monthly_budget_usd
  time_grain = "Monthly"

  time_period {
    start_date = "2025-08-01T00:00:00Z"
    end_date   = "2030-12-31T23:59:59Z"
  }

  filter {
    dimension {
      name = "ResourceGroupName"
      values = [
        azurerm_resource_group.luma_homelab_logging.name,
      ]
    }
  }

  notification {
    enabled   = true
    threshold = 80
    operator  = "GreaterThan"

    contact_emails = var.alert_emails
  }

  notification {
    enabled   = true
    threshold = 100
    operator  = "GreaterThan"

    contact_emails = var.alert_emails
  }
}
