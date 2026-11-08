# Public DNS for the galaxy edge — see README.md.
#
# Design: every public hostname is a wildcard CNAME to its zone apex; the
# apex A record carries the WAN IP; Traefik selects the backend by Host
# header. Adding an app is one Ingress with a host — no DNS change.

terraform {
  required_version = ">= 1.5"
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }

  # Shared homelab state backend (created by azure/bootstrap).
  backend "azurerm" {
    resource_group_name  = "rg-luma-homelab-terraform-state"
    storage_account_name = "lumahomelabstateq978tb72"
    container_name       = "terraform-state"
    key                  = "cloudflare-dns.tfstate"
  }
}

# Auth via CLOUDFLARE_API_TOKEN — exported by ./tf.sh from the git-crypt
# secret the cluster already uses (cluster/dns/cloudflare-api-token.secret.yaml).
provider "cloudflare" {}

# Workers resources only (workers.tf), with a second token the cluster never
# gets: Workers Editor on the account, Workers Routes write on realliance.net.
# ./tf.sh sets TF_VAR_workers_api_token from the git-crypt file
# workers-api-token.secret.conf (one `api-token=<value>` line). DNS records
# stay on the default provider: this token has no DNS write.
provider "cloudflare" {
  alias     = "workers"
  api_token = var.workers_api_token
}

variable "workers_api_token" {
  description = "Cloudflare token for the workers provider alias; ./tf.sh sets it."
  type        = string
  sensitive   = true

  # An empty api_token could let the provider fall back to CLOUDFLARE_API_TOKEN,
  # the cluster's DNS token, without a word.
  validation {
    condition     = length(trimspace(var.workers_api_token)) > 0
    error_message = "workers_api_token is empty: run terraform through ./tf.sh."
  }
}
