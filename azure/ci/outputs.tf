# Every output is sensitive: these feed the ci-dispatch-azure SealedSecret and the sweeper, never a
# terminal. Read one with `./azure/ci/tf.sh output -raw <name>` into a pipe (Task 4 Step 3).

output "dispatcher_client_id" {
  description = "The dispatcher app's client id."
  value       = azuread_application.dispatcher.client_id
  sensitive   = true
}

output "tenant_id" {
  description = "The tenant the dispatcher authenticates against."
  value       = data.azuread_client_config.current.tenant_id
  sensitive   = true
}

output "dispatcher_client_secret" {
  description = "The dispatcher's client secret. It rotates at 150 days (fence.tf)."
  value       = azuread_application_password.dispatcher.value
  sensitive   = true
}

output "dispatcher_assignment_id" {
  description = "The custom-role assignment the sweeper's lock deletes (F6, F30)."
  value       = azurerm_role_assignment.dispatcher.id
  sensitive   = true
}
