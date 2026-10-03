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

Built in. `site.yml` arms VyOS's commit-confirm timer before any
`network`, `firewall`, `nat` or `services` include
(`playbooks/commit-confirm-arm.yml`). It checks from this machine that
the LAN still reaches the WAN and the port-forwards still answer
(`commit_confirm_verify_urls`), then cancels the timer
(`playbooks/commit-confirm-verify.yml`) and saves. If anything fails
first, the router reboots to its last saved config after
`commit_confirm_minutes`. Don't race it.

Don't open `commit-confirm` in a separate SSH session: it can't cover
Ansible's commits (VyOS answers "No configuration changes to commit").

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
