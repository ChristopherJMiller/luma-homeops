# Entra app registrations for cluster identity — see README.md.
#
# Exists so that family members with Microsoft/Outlook accounts can sign in.
# oauth2-proxy speaks to exactly one provider, so the multi-provider path is
# Dex brokering Google + Microsoft; this module registers the Microsoft side.
#
# Unlike Google's "Web application" OAuth client (console-only, no API), Entra
# app registrations are fully manageable as code — which is why this half is
# terraform and the Google half is a documented manual artifact.

terraform {
  required_version = ">= 1.5"
  required_providers {
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.9"
    }
  }

  # Shared homelab state backend (created by azure/bootstrap), same storage
  # account as cloudflare/dns — different key.
  backend "azurerm" {
    resource_group_name  = "rg-luma-homelab-terraform-state"
    storage_account_name = "lumahomelabstateq978tb72"
    container_name       = "terraform-state"
    key                  = "entra-apps.tfstate"
  }
}

# Auth comes from `az login` (the same session the state backend uses).
provider "azuread" {}

data "azuread_client_config" "current" {}
