output "workspace_id" {
  description = "The workspace ID of the Log Analytics workspace"
  value       = azurerm_log_analytics_workspace.luma_homelab.workspace_id
}

output "workspace_primary_shared_key" {
  description = "The primary shared key for the Log Analytics workspace"
  value       = azurerm_log_analytics_workspace.luma_homelab.primary_shared_key
  sensitive   = true
}

output "workspace_secondary_shared_key" {
  description = "The secondary shared key for the Log Analytics workspace"
  value       = azurerm_log_analytics_workspace.luma_homelab.secondary_shared_key
  sensitive   = true
}

output "workspace_name" {
  description = "The name of the Log Analytics workspace"
  value       = azurerm_log_analytics_workspace.luma_homelab.name
}

output "resource_group_name" {
  description = "The name of the resource group"
  value       = azurerm_resource_group.luma_homelab_logging.name
}

output "data_collection_rule_id" {
  description = "The ID of the data collection rule"
  value       = azurerm_monitor_data_collection_rule.luma_homelab_logging.id
}