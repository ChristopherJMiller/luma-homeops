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

Built in, on every run (the parachute tasks are tagged `always`, so no
`--tags`/`--skip-tags` combination escapes it). `site.yml` refuses to start
if a rollback is already pending, arms VyOS's commit-confirm timer and
asserts it is running (`playbooks/commit-confirm-arm.yml`), applies, then
checks from this machine that the LAN still reaches the WAN and the
port-forwards still answer (`commit_confirm_verify_urls`) and that a
*fresh* SSH login works, cancels the timer
(`playbooks/commit-confirm-verify.yml`), and saves only once the timer is
confirmed stopped. If anything fails
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

## Tailscale (out-of-band, not Ansible-managed)

The router is the `vyos` node on the tailnet: the upstream Debian package
(`tailscaled.service`, state in `/var/lib/tailscale`), installed by hand. It
lives on the running image's overlay, not `/config`, so **a VyOS image
upgrade drops it** — reinstall and `tailscale up` again afterwards. Node key
expiry is disabled in the admin console. `--accept-dns=false`: VyOS owns
`/etc/resolv.conf`, and MagicDNS fighting it was a standing health warning.

Upgrade in place from the apt repo it came from (refresh only that list):

```bash
sudo apt-get update -o Dir::Etc::sourcelist=sources.list.d/tailscale.list -o Dir::Etc::sourceparts=-
sudo apt-get install --only-upgrade --no-install-recommends tailscale
```

The router has a TPM, and since 1.90 tailscaled seals the node key to it on
upgrade — an older binary may not read the state back. Before the 1.74.1 →
1.102.4 hop (2026-10-05) the state and the old .deb were saved to
`/config/tailscale-backup/` (root-only; the state file holds node keys):
rollback is stop tailscaled, restore the state, `dpkg -i` the .deb.

Planned: run it as an Ansible-managed VyOS container (like zigbee2mqtt) with
state on `/config`, done together with the VyOS image upgrade that would
otherwise delete it. Carry the TPM over (`/dev/tpmrm0`) or the sealed state
won't open.

It is a subnet router for **single hosts only** — today `192.168.0.2/32`, the
KVM wired to the Mac (`kvm` static mapping in `group_vars/vyos_routers.yml`).
Not the whole `/24`, on purpose:

- Least privilege: the KVM is physical control of the Mac, and under default
  ACLs every tailnet device gets every approved route.
- On Linux, `--accept-routes` puts tailnet routes in table 52, which is
  consulted before `main` — a LAN host with it on (rowlett) would send all
  its LAN traffic through the router's tunnel if the `/24` were approved.

To expose another host: give it a static mapping outside the DHCP pool, then

```bash
ssh chris@192.168.0.1 'sudo tailscale set --advertise-routes=192.168.0.2/32,<ip>/32'
```

(the list replaces, it doesn't append) and approve the new route in the admin
console: Machines → `vyos` → Edit route settings.

## Troubleshooting
- **SSH issues**: Check `inventory.yml` SSH key path
- **Permission denied**: Verify SSH key access to router
- **Module errors**: Run `ansible-galaxy collection install vyos.vyos`
- **Debug mode**: Add `-vvv` to any ansible-playbook command
