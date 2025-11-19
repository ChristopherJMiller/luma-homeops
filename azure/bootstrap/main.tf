terraform {
  required_version = "1.14.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "3.117.1"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.7.2"
    }
  }
}

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy = true
    }
  }
  subscription_id = "ddc1f03e-6903-4508-a23d-1754eb304cad"
}

# Get current client configuration
data "azurerm_client_config" "current" {}

# Random suffix for globally unique names
resource "random_string" "suffix" {
  length  = 8
  special = false
  upper   = false
}

# Resource Group for Terraform state and secrets
resource "azurerm_resource_group" "terraform_state" {
  name     = "rg-luma-homelab-terraform-state"
  location = "West US 3"

  tags = {
    Environment = "luma-homelab"
    Purpose     = "terraform-state"
    ManagedBy   = "terraform"
  }
}

# Storage Account for Terraform state
resource "azurerm_storage_account" "terraform_state" {
  name                     = "lumahomelabstate${random_string.suffix.result}"
  resource_group_name      = azurerm_resource_group.terraform_state.name
  location                 = azurerm_resource_group.terraform_state.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  
  # Security settings
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
  
  tags = {
    Environment = "luma-homelab"
    Purpose     = "terraform-state"
    ManagedBy   = "terraform"
  }
}

# Container for Terraform state files
resource "azurerm_storage_container" "terraform_state" {
  name                  = "terraform-state"
  storage_account_name  = azurerm_storage_account.terraform_state.name
  container_access_type = "private"
}

