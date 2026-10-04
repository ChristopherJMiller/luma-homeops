# Home Assistant runbook

HA on galaxy: `https://home.chrismiller.xyz`, namespace `home-assistant`, Argo apps `home-assistant` (kustomize: config ConfigMaps, backups, the `ha-reload` hook) and `home-assistant-release` (helm chart `ChristopherJMiller/ha-helm`). Design: `docs/superpowers/specs/2026-10-03-ha-agent-overhaul-design.md`. Day-to-day workflow: the `ha-config` skill.

## hactl

`tools/hactl`, on PATH inside `nix develop`. `hactl --help` lists commands; each takes `--json`.

| Command | Use |
|---|---|
| `find`, `state`, `history`, `stats`, `template`, `trace`, `log` | Read live HA (history flags flapping; trace shows each step) |
| `shot PATH…` | Screenshots at phone 412 / desktop 1440, with a report (error cards, theme, unavailable cards, console errors) |
| `preview FILE` | Push a dashboard to `/claude-preview` (admin-only scratch) and screenshot it |
| `lint [--offline]` | Config rules + `check_config`; live adds entity-id and dashboard-template checks |
| `health` | Drift, hook status, failing automations, repairs, top log errors |
| `call DOMAIN.SERVICE` | Actions; locks/notify/tts/restart need `--confirmed` |
| `deploy [--shot]` | After a push: wait for Argo + hook + rollout, apply the committed `state/` manifests, then health (+ shots); refuses uncommitted `state/` edits |
| `revision`, `selftest` | Config revision hash; read-only end-to-end check |
| `import [--force]` | Write `state/` manifests from live HA (bootstrap; refuses to overwrite) |
| `plan` | Diff `state/` manifests against live HA; exit 2 when there are changes |
| `apply [--prune]` | Converge HA to the manifests; deletes only with `--prune`; never deletes integrations |

`deploy` and `health` read the cluster with kubectl: `export KUBECONFIG=/tmp/galaxy-kubeconfig` first.

Entities referenced on purpose before they exist (an integration still to be added) are listed, with a reason, in `cluster/home-assistant/lint-allow-missing.txt`; everything else unknown fails `lint`.

## Tokens

All three are long-lived tokens on Chris's own HA account (his choice; HA has no scoped tokens). Each is revocable on its own from his HA profile → Security.

| Token | Lives in | Used by |
|---|---|---|
| hactl | `tools/hactl/agent.secret.yaml` (git-crypt) | `hactl` (or `$HA_TOKEN`) |
| gitops-reload | `cluster/home-assistant/ha-reload-token.secret.yaml` (git-crypt) → sealed `ha-reload-token.yaml` | the `ha-reload` hook |
| prometheus-scrape | (ops-fixes plan) | Prometheus |

Rotate: mint a new token in HA and save it to a mode-600 file (never paste it into a terminal or chat). For the sealed one, regenerate the `.secret.yaml` straight to disk (never let it print; don't `cat` it afterwards):

```bash
( umask 077 && kubectl create secret generic ha-reload-token -n home-assistant --from-file=token="$HOME/.config/galaxy/reload-token" --dry-run=client -o yaml > cluster/home-assistant/ha-reload-token.secret.yaml )
```

Then delete the old `ha-reload-token.yaml`, run `./sign.sh`, commit, push, and revoke the old token. Symptom of a dead hactl token: `HA rejected the token (401)`, or `shot` failing with "HA showed its login page".

## How a change reaches HA

1. Pre-commit writes `packages/config_revision.yaml`: a hash of packages, dashboards and themes, exposed as `sensor.ha_config_revision`.
2. Argo syncs the ConfigMaps. Kubelet updates the pod's mounted files within ~2 min.
3. The **`ha-reload` PostSync Job** (`hooks/ha_reload.py`) calls `homeassistant.reload_all` every 20 s until the sensor shows the new revision (timeout 6 min). It then checks every top-level package key is a loaded integration. If one isn't, it restarts HA once (patches the Deployment's `restartedAt`; the strategy is `Recreate`) and waits until it is.
4. `hactl deploy` waits for all of that, then runs `health`.

A failed hook: `kubectl -n home-assistant logs job/ha-reload`. It keeps the last run until the next sync. Fix the cause in git and push; the next sync reruns it.

## Screenshots: how auth works

HA's frontend keeps OAuth tokens in `localStorage.hassTokens`, not a cookie. `hactl shot` plants the hactl token there before the frontend boots, in a throwaway browser context. The wrapper drops the host's `LD_LIBRARY_PATH` (a system alsa-lib built against a newer glibc kills the nix-built browser). HA's frontend logs a harmless "Subscription not found" rejection on most loads; the report filters it.

## Declarative state (`cluster/home-assistant/state/`)

HA's `.storage` parts are described in git and converged with `hactl apply` (terraform-style). `hactl deploy` runs `apply` after every push; `hactl health` reports a non-empty plan as drift.

| File | Holds | Matched by | Managed |
|---|---|---|---|
| `areas.yaml` | floors, labels, areas | id (HA's slug of the name at creation) | fully; deletes need `--prune` |
| `devices.yaml` | area, name, labels, disabled | one `identifiers`/`connections` pair (compared as strings) | listed devices and fields only |
| `entities.yaml` | entity_id, name, icon, area, labels, hidden, disabled; `remove:` list | platform + unique_id | listed entities and fields only |
| `helpers.yaml` | config-entry helpers: `create` (menu + answers), `options` | domain + title | fully; deletes need `--prune` |
| `integrations.yaml` | integrations that must exist; `create`, `options`, `credentials`, `manual` | domain + title | presence + options; never deleted |
| `dashboards.yaml` | storage dashboards, Lovelace resources | url_path / url | fully; deletes need `--prune` |
| `credentials.yaml` | secrets for config flows (git-crypt; never print it) | key named by `credentials:` | — |

To change any of it: edit the manifest, `hactl plan`, then commit and push (deploy applies), or `hactl apply` directly. Never change these things in the HA UI or with ad-hoc API calls; the next plan flags them and apply reverts them. yamlfmt formats these files on commit; that is expected.

Cannot be declared: users and long-lived tokens; the human step of interactive integrations (Hue link button, Plex sign-in, phone app registration: `plan` shows their `manual:` text until done); runtime state (history, restore-state).
