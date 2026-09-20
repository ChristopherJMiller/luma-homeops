---
name: vyos-deploy
description: Apply a configuration change to the VyOS router via the Ansible playbooks under router/ansible/. Use when the user wants to change firewall rules, NAT, DHCP, interfaces, or any router config. Wraps the deploy in commit-confirm 10 so a misconfig auto-rolls-back if the operator can't reach the router. NEVER skips commit-confirm. NEVER touches router config outside the Ansible playbooks. NEVER pushes a change that would lock out SSH from the LAN.
---

# vyos-deploy

Apply a router config change via the Ansible playbooks in `router/ansible/`, with `commit-confirm 10` as the safety net.

The router is the single point of failure for the entire homelab — if a config commit locks SSH out, nothing else is reachable until physical console intervention. `commit-confirm` auto-rolls-back after 10 minutes if no `confirm` is issued, which buys us a "I can still reach it" reality check before the change becomes permanent.

## Hard rules (CLAUDE.md S8)

- **Always deploy high-risk changes through a playbook that arms `config-mgmt commit_confirm` first** (firewall, NAT, interfaces, DHCP, anything affecting SSH reachability). See "Apply with commit-confirm".
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

The parachute lives **inside the playbook**, not in a separate SSH session.
An interactive `commit-confirm` opened alongside Ansible does NOT cover
Ansible's commits — VyOS refuses it with "No configuration changes to
commit" because the config session is empty. (Learned 2026-09-19, #2507.)

`playbooks/nat.yml` is the reference implementation; copy the pattern into
any other high-risk playbook (`network.yml`, `firewall.yml`) before you
first need it there:

```
arm     vyos_command: sudo sg vyattacfg 'config-mgmt commit_confirm -y -t=N'
          → systemd timer that reboots the router to the SAVED config
          (-y skips the "Proceed ?" prompt, which hangs a non-tty session)
apply   the vyos_config tasks (each commits immediately, save: false)
verify  ansible.builtin.uri from the control node against
          <playbook>_verify_urls — use a Cloudflare-PROXIED hostname so the
          request genuinely leaves via WAN and re-enters through eth0
          (a DNS-only name takes NAT reflection and proves nothing)
confirm vyos_command: sudo systemctl stop commit-confirm.timer, then assert
          `systemctl is-active` says inactive. Do NOT call `config-mgmt
          confirm` — it stops the timer and then tracebacks trying to
          finalise a commit-log entry only the interactive CLI writes.
save    site.yml's final "Commit and save all changes"
```

If `verify` fails the play aborts with the timer still armed → the router
**reboots** to the last saved config within N minutes (the whole LAN drops
for ~1 min). That is the intended outcome; don't race it.

```bash
cd router/ansible
ansible-playbook site.yml --check --diff --tags <tag>   # preview, every time
ansible-playbook site.yml --tags <tag>                  # apply
```

Rehearse a new playbook's arm/verify/confirm chain once with a harmless
change (e.g. a rule `description`) before using it for a real cutover.

**Never enter `set` commands by hand on the router** — not even "just the
two lines from the diff". Everything goes through Ansible so repo == router.
Manual commits also land only in the running config; a reboot silently
reverts them unless someone remembers `save`.
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
