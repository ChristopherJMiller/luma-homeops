# Azure Log Analytics for Homelab

This Terraform configuration creates an Azure Log Analytics workspace for long-term log storage (90-day retention) as part of the homelab monitoring implementation.

## Architecture

- **VyOS Router**: 30-day log buffer (20-30GB)
- **Azure Log Analytics**: 90-day retention, cost-controlled
- **FluentBit**: Multi-destination log shipping with failover

## Prerequisites

1. Azure CLI installed and authenticated
2. Terraform installed
3. Azure subscription with appropriate permissions

## Setup

1. **Copy configuration file:**
   ```bash
   cp terraform.tfvars.example terraform.tfvars
   ```

2. **Edit terraform.tfvars:**
   - Set your preferred Azure region
   - Configure alert email addresses
   - Adjust daily quota if needed (default: 1GB/day)

3. **Login to Azure:**
   ```bash
   az login
   az account set --subscription "your-subscription-id"
   ```

4. **Deploy infrastructure:**
   ```bash
   terraform init
   terraform plan
   terraform apply
   ```

## Cost Controls

- **Daily Quota**: 1GB/day (configurable)
- **Monthly Budget**: $10 USD with 80% and 100% alerts
- **Retention**: 90 days (as per homelab plan)
- **Estimated Cost**: ~$3-8/month depending on log volume

## Outputs

After deployment, Terraform will output:
- `workspace_id`: For FluentBit configuration
- `workspace_primary_shared_key`: Authentication key (sensitive)
- `resource_group_name`: Azure resource group

## Integration with Kubernetes

The workspace credentials will be used in the FluentBit DaemonSet configuration:
- Create Kubernetes secret with workspace ID and shared key
- Configure FluentBit to send logs to both VyOS (primary) and Azure (backup)

## Monitoring

- Budget alerts sent to configured email addresses
- Azure portal provides ingestion and cost monitoring
- Query logs using KQL (Kusto Query Language)

## Security

- Workspace keys are marked as sensitive in Terraform
- Use Azure Key Vault for production environments
- Regular key rotation recommended