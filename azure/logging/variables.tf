variable "resource_group_name" {
  description = "Name of the Azure resource group for luma-homelab logging"
  type        = string
  default     = "rg-luma-homelab-logging"
}

variable "location" {
  description = "Azure region for resources"
  type        = string
  default     = "West US 3"
}

variable "workspace_name" {
  description = "Name of the Log Analytics workspace"
  type        = string
  default     = "luma-homelab-logs-workspace"
}

variable "retention_days" {
  description = "Log retention period in days"
  type        = number
  default     = 90
  
  validation {
    condition     = var.retention_days >= 30 && var.retention_days <= 730
    error_message = "Retention days must be between 30 and 730."
  }
}

variable "daily_quota_gb" {
  description = "Daily ingestion quota in GB to control costs"
  type        = number
  default     = 1
  
  validation {
    condition     = var.daily_quota_gb >= 0.1 && var.daily_quota_gb <= 100
    error_message = "Daily quota must be between 0.1 and 100 GB."
  }
}

variable "monthly_budget_usd" {
  description = "Monthly budget for logging costs in USD"
  type        = number
  default     = 10
}

variable "alert_emails" {
  description = "Email addresses for budget alerts"
  type        = list(string)
  default     = []
}

variable "environment" {
  description = "Environment tag"
  type        = string
  default     = "luma-homelab"
}