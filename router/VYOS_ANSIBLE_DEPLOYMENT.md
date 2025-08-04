# VyOS Ansible Deployment Guide

## Quick Setup

```bash
# Install collections
cd router/ansible
ansible-galaxy collection install -r requirements.yml

# Test connectivity
ansible vyos_routers -m vyos.vyos.vyos_command -a "commands='show version'"
```

## Deploy Configuration

### Full Deployment
```bash
cd router/ansible

# Always test first
ansible-playbook site.yml --check --diff

# Deploy everything
ansible-playbook site.yml
```

### Selective Deployment
```bash
# Deploy only monitoring (Phase 1)
ansible-playbook site.yml --tags monitoring

# Deploy specific components
ansible-playbook site.yml --tags "system,network"
ansible-playbook site.yml --tags firewall
ansible-playbook site.yml --tags services
```

## Rollback Procedures

### Automatic Backups
- Configuration backups created automatically before each deployment
- Stored in `backups/vyos-config-backup-<timestamp>.txt`

### Manual Rollback
```bash
# If deployment fails, rollback using backup
ssh chris@192.168.0.1
configure
load /path/to/backup/vyos-config-backup-<timestamp>.txt
commit
save
```

### Safe Deployment with Rollback Timer
```bash
# For major changes, use commit-confirm on router
ssh chris@192.168.0.1
configure
commit-confirm 10  # Auto-rollback in 10 minutes

# Run Ansible deployment
ansible-playbook site.yml

# If everything works, confirm on router:
confirm

# If something breaks, just wait 10 minutes for auto-rollback
```

## Verification
```bash
# Check deployment worked
ansible vyos_routers -m vyos.vyos.vyos_command -a "commands='show interfaces'"
ansible vyos_routers -m vyos.vyos.vyos_command -a "commands='show firewall'"

# Test monitoring (Phase 1)
curl http://192.168.0.1:9100/metrics
```

## Troubleshooting
- **SSH issues**: Check `inventory.yml` SSH key path
- **Permission denied**: Verify SSH key access to router
- **Module errors**: Run `ansible-galaxy collection install vyos.vyos`
- **Debug mode**: Add `-vvv` to any ansible-playbook command