# Public DNS for the galaxy edge — see README.md.
#
# Design: every public hostname is a wildcard CNAME to its zone apex; the
# apex A record carries the WAN IP; Traefik selects the backend by Host
# header. Adding an app is one Ingress with a host — no DNS change.

terraform {
  required_version = "1.16.4"
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "5.25.0"
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
