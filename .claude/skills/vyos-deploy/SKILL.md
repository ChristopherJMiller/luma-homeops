---
name: vyos-deploy
description: Apply a configuration change to the VyOS router via the Ansible playbooks under router/ansible/. Use when the user wants to change firewall rules, NAT, DHCP, interfaces, or any router config. Wraps the deploy in commit-confirm 10 so a misconfig auto-rolls-back if the operator can't reach the router. NEVER skips commit-confirm. NEVER touches router config outside the Ansible playbooks. NEVER pushes a change that would lock out SSH from the LAN.
---

# vyos-deploy

Apply a router config change via the Ansible playbooks in `router/ansible/`, with `commit-confirm 10` as the safety net.

The router is the single point of failure for the entire homelab — if a config commit locks SSH out, nothing else is reachable until physical console intervention. `commit-confirm` auto-rolls-back after 10 minutes if no `confirm` is issued, which buys us a "I can still reach it" reality check before the change becomes permanent.

## Hard rules (CLAUDE.md S8)

- **Always use `commit-confirm 10`** for changes that touch firewall, NAT, interfaces, DHCP, or anything that affects SSH reachability.
- **Always preview first** with `ansible-playbook --check --diff` before applying.
- **Never commit a config that drops SSH on the management network** without an explicit "I am at the console" sign-off from the user.
- **Never edit live router config** outside Ansible. The playbook is the source of truth; manual edits drift.
- **Backup before deploy.** The Ansible playbook does this automatically into `router/ansible/backups/` — verify the latest backup exists after the run.

## Connectivity check

```bash
# SSH key must be in agent
ssh-add -l | grep -q ed25519 || ssh-add ~/.ssh/id_ed25519

# Reach test
ssh -o BatchMode=yes chris@192.168.0.1 \
  '/opt/vyatta/bin/vyatta-op-cmd-wrapper show version | head -3'
```

## Preview the change

```bash
cd router/ansible
ansible-galaxy collection install -r requirements.yml  # if first run

# Always check first
ansible-playbook site.yml --check --diff
```

Read the diff. If the diff is empty, there's nothing to deploy.

If the diff includes lines under `interfaces`, `firewall`, `nat`, `dhcp-server`, or `service ssh`, this is a high-risk change → use commit-confirm. If it's purely DNS, NTP, hostname, or container config, lower risk.

## Apply with commit-confirm (high-risk changes)

```bash
# Open a commit-confirm window MANUALLY on the router first.
# This is the rollback parachute — Ansible's commit alone has no auto-rollback.
ssh chris@192.168.0.1
configure
commit-confirm 10  # changes auto-rollback in 10 minutes if not confirmed
exit
exit

# In a separate terminal, run the playbook:
cd router/ansible
ansible-playbook site.yml [--tags <relevant>]

# Verify reachability + functionality (depends on what changed)
ssh chris@192.168.0.1 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show interfaces'
ssh chris@192.168.0.1 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show firewall'
ssh chris@192.168.0.1 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show nat destination'
# Test connectivity from a third location if possible (curl from outside, ping from a client device)

# If everything works, confirm before the 10-minute timer expires:
ssh chris@192.168.0.1
configure
confirm
exit
exit
```

If the timer elapses without `confirm`, VyOS rolls back automatically — including any Ansible-applied changes from that session, since they're all part of the same uncommitted set.

## Apply without commit-confirm (low-risk changes only)

For purely additive, non-network-path changes — DNS forwarder rules, NTP, container config tweaks, hostname:

```bash
cd router/ansible
ansible-playbook site.yml --tags <relevant>
# Verify
ssh chris@192.168.0.1 '/opt/vyatta/bin/vyatta-op-cmd-wrapper show <whatever>'
```

When in doubt, treat it as high-risk.

## Verify the backup landed

```bash
ls -ltr router/ansible/backups/ | tail -3
```

If no new file appeared after a run, the playbook didn't run its backup task → STOP, investigate.

## Tags reference

From `router/ansible/site.yml`:
- `system` — hostname, NTP, DNS, console
- `network` — interface config (HIGH RISK)
- `firewall` — firewall groups + rules (HIGH RISK)
- `nat` — port forwards + masquerade (HIGH RISK)
- `services` — DHCP, syslog, prometheus exporter (MEDIUM RISK)
- `monitoring` — prometheus + syslog only

`network`, `firewall`, `nat` always need commit-confirm.

## When to abort and surface

- Reach test before deploy fails
- `--check --diff` shows changes you didn't expect (drift from outside source)
- Backup file isn't created during the run
- Verification step fails after the deploy
- Router becomes unreachable during the deploy

If the router is unreachable: **wait for the commit-confirm timer to elapse** (10 min). Don't drive across town to the console unless you're sure you're outside the auto-rollback window.

## What this skill does NOT do

- Edit `router/ansible/group_vars/vyos_routers.yml` or playbooks for the user — that's a separate authoring task.
- Manually mutate live router config (`set service dhcp-server …` outside the playbook). All changes flow through Ansible.
- Decide whether a config change is correct. The user owns the *what*; this skill owns the *how to deploy safely*.
- Bypass commit-confirm because "this change is small". The cost of being wrong is too high.
