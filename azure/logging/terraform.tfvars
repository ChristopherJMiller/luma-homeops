# Azure Log Analytics Configuration for Luma Homelab
# Copy this file to terraform.tfvars and customize the values

# Basic Configuration
resource_group_name = "rg-luma-homelab-logging"
location           = "West US 3"
workspace_name     = "luma-homelab-logs-workspace"

# Log Retention (31 days, the free tier)
retention_days = 31

# Cost Controls
daily_quota_gb     = 0.25  # 0.25GB daily limit, about $17/month ceiling
monthly_budget_usd = 15    # Budget alert threshold

# Alert Configuration
# alert_emails and subscription_id live in logging.secret.auto.tfvars (git-crypt)

# Environment
environment = "luma-homelab"
