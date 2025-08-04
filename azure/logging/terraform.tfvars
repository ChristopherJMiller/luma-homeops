# Azure Log Analytics Configuration for Luma Homelab
# Copy this file to terraform.tfvars and customize the values

# Basic Configuration
resource_group_name = "rg-luma-homelab-logging"
location           = "West US 3"
workspace_name     = "luma-homelab-logs-workspace"

# Log Retention (90 days as per plan)
retention_days = 90

# Cost Controls
daily_quota_gb     = 1     # 1GB daily limit (30GB/month max)
monthly_budget_usd = 10    # Budget alert threshold

# Alert Configuration
alert_emails = [
  "happy.stool5187@fastmail.com"
]

# Environment
environment = "luma-homelab"
