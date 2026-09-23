# The Microsoft identity provider behind Dex.
#
# sign_in_audience is the load-bearing setting: "AzureADandPersonalMicrosoftAccount"
# is what lets a plain outlook.com/hotmail.com account sign in. The default
# (AzureADMyOrg) would restrict this to the Realliance tenant, which is exactly
# the case this app exists to avoid.

resource "azuread_application" "dex" {
  display_name = "galaxy-dex"
  owners       = [data.azuread_client_config.current.object_id]

  # Work/school accounts in any tenant AND personal Microsoft accounts.
  sign_in_audience = "AzureADandPersonalMicrosoftAccount"

  api {
    # Required by Entra whenever personal accounts are in the audience, and
    # what OIDC wants regardless: v1 tokens are the legacy AAD format.
    requested_access_token_version = 2
  }

  web {
    redirect_uris = ["https://dex.chrismiller.xyz/callback"]

    implicit_grant {
      # Dex uses the authorization-code flow; no implicit tokens.
      access_token_issuance_enabled = false
      id_token_issuance_enabled     = false
    }
  }

  required_resource_access {
    # Microsoft Graph
    resource_app_id = "00000003-0000-0000-c000-000000000000"

    # User.Read (delegated) — the minimum needed to read the signed-in user's
    # profile and email. Dex asks for openid/profile/email on top of this.
    resource_access {
      id   = "e1fe6dd8-ba31-4d61-89e7-88639da4683d"
      type = "Scope"
    }
  }
}

# Client secret. Terraform stores this in state (azurerm blob — encrypted at
# rest, access-controlled), and it is surfaced via the sensitive output below
# so it can be copied into the git-crypt'd cluster secret. Rotate by tainting
# this resource.
resource "azuread_application_password" "dex" {
  application_id = azuread_application.dex.id
  display_name   = "dex-oidc-connector"
  end_date       = "2028-01-01T00:00:00Z"
}

# A service principal in this tenant for the app. Not strictly required for
# personal-account sign-in, but it makes the app visible under Enterprise
# Applications and is needed if you ever add tenant-scoped policy.
resource "azuread_service_principal" "dex" {
  client_id = azuread_application.dex.client_id
  owners    = [data.azuread_client_config.current.object_id]
}

output "dex_client_id" {
  value       = azuread_application.dex.client_id
  description = "Entra application (client) ID for the Dex microsoft connector."
}

output "dex_client_secret" {
  value       = azuread_application_password.dex.value
  sensitive   = true
  description = "Client secret. Read with: terraform output -raw dex_client_secret"
}

output "dex_tenant" {
  value       = "common"
  description = <<-EOT
    Tenant value for Dex's microsoft connector. "common" admits both
    work/school and personal Microsoft accounts, matching sign_in_audience.
    Access control is the oauth2-proxy allowlist, not the tenant.
  EOT
}
