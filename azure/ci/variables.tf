# Both values come from ci.secret.auto.tfvars (git-crypt), as azure/logging does (E9). Sensitive keeps
# them out of plan output.

variable "subscription_id" {
  description = "Azure subscription id."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", var.subscription_id))
    error_message = "subscription_id must be a lowercase subscription GUID."
  }
}

variable "alert_emails" {
  description = "Who the budgets email."
  type        = list(string)
  sensitive   = true

  validation {
    condition     = length(var.alert_emails) > 0
    error_message = "A budget needs at least one address to email."
  }
}
