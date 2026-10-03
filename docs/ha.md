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
| `deploy [--shot]` | After a push: wait for Argo + hook, then health (+ shots) |
| `revision`, `selftest` | Config revision hash; read-only end-to-end check |

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

## Not in git (yet)

Integrations/config entries, the entity/device/area registries, UI helpers, storage dashboards (`lovelace`, `map`, `claude-preview`) and Lovelace resources live in HA's `.storage`. The next plan describes them in `cluster/home-assistant/state/` with `hactl import/plan/apply`. Users and tokens can never be declared.
