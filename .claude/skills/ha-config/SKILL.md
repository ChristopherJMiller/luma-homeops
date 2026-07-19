---
name: ha-config
description: Add or edit git-tracked Home Assistant config — automations, helpers, templates, scenes, dashboards — as HA "packages" in cluster/home-assistant/packages/, validate with check_config, and deploy safely. Use whenever changing HA automations/helpers/entities, migrating UI config to git, or wiring new HA behavior. NEVER edit these automations in the HA UI (they are package-defined and read-only there). NEVER trust check_config's exit code. NEVER let a generic YAML formatter touch HA config.
---

# ha-config

Home Assistant runs in-cluster (Argo `home-assistant-release`, chart from `~/Repos/ha-helm`). Its git-tracked config is delivered as **packages**, not edited in the UI.

## Architecture (how config reaches HA)

```
cluster/home-assistant/packages/*.yaml      <- SOURCE OF TRUTH (edit here)
  -> kustomize configMapGenerator (disableNameSuffixHash) => ConfigMap ha-packages
  -> ha-helm extraConfigMounts mounts it at /config/packages
  -> HA loads it via  homeassistant.packages: !include_dir_named packages/
```
Relevant chart values in `cluster/applications/home-assistant-release.yaml`:
`externalUrl`, `packages.enabled: true`, `extraConfigMounts`, `checkConfig.enabled: true`.
Each package file bundles any mix of domains (`automation:`, `template:`, `input_boolean:`, `adaptive_lighting:`, …) for one feature/room.

## Workflow: edit -> validate -> deploy

### 1. Edit the package
Add/modify `cluster/home-assistant/packages/<feature>.yaml`. If it's a new file, add it to the `configMapGenerator.files` list in `cluster/home-assistant/kustomization.yaml`.

**Filenames MUST use underscores** (`morning_routine.yaml`, not `morning-routine.yaml`). HA rejects hyphen package slugs and *silently skips the whole file* (`Package will not be initialized`).

### 2. Look up real entity_ids first
Automations reference entity_ids, and HA's are often not what you'd guess (sticky after Zigbee renames, mangled by integrations). Get the truth from the recorder:
```bash
export KUBECONFIG=/tmp/galaxy-kubeconfig   # refresh from Talos if needed
kubectl -n home-assistant exec acid-ha-0 -c postgres -- psql -U postgres -d homeassistant -tAc \
  "SELECT entity_id FROM states_meta WHERE entity_id LIKE 'switch.%bedroom%' ORDER BY 1;"
```
Notes: the recorder **excludes the `automation` and `update` domains**, so those won't appear. YAML-platform entities without a unique_id aren't in `.storage/core.entity_registry`. The HA MCP server (`.mcp.json`) is **read/call only** — it can't create helpers or rename entities; use the HA UI or WS/REST API for that.

### 3. Validate with real check_config (never skip)
`check_config` is the gate. Run it in the **deployed image version**, with any custom integrations present.
```bash
SC=/tmp/ha-check; rm -rf "$SC"; mkdir -p "$SC/packages" "$SC/custom_components"
cp cluster/home-assistant/packages/*.yaml "$SC/packages/"
cp -r cluster/home-assistant/blueprints/. "$SC/blueprints/" 2>/dev/null || true
printf 'homeassistant:\n  packages: !include_dir_named packages/\n' > "$SC/configuration.yaml"
: > "$SC/secrets.yaml"
# custom components aren't in the vanilla image — fetch any the packages use:
git clone --depth 1 https://github.com/basnijholt/adaptive-lighting /tmp/al && \
  cp -r /tmp/al/custom_components/adaptive_lighting "$SC/custom_components/"
TAG=$(grep -oE 'tag: [0-9]{4}\.[0-9]+\.[0-9]+' cluster/applications/home-assistant-release.yaml | head -1 | awk '{print $2}')
docker run --rm -v "$SC":/config "docker.io/homeassistant/home-assistant:$TAG" \
  python -m homeassistant --script check_config -c /config 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep -iE 'ERROR|Invalid config|could not be validated|will not be initialized|invalid slug|Platform error' \
  && echo "!!! FIX ERRORS ABOVE" || echo "CLEAN"
```
**check_config EXITS 0 EVEN ON ERRORS** — always grep the output; never trust `$?`. It also **cannot catch runtime template errors** (e.g. a `device_class: timestamp` sensor returning `""` when its source is unknown) — guard those with an `availability:` template.

For a full-fidelity check (whole live config + your changes), assemble it inside the running pod against a `/tmp/cfgtest` copy of `/config` and run the pod's own `check_config` — do this before enabling anything gate-like on a RWO volume.

### 4. Deploy
Commit + push (pre-commit runs; direct main pushes are fine here). Argo syncs the `home-assistant` app (updates the ConfigMap) and, for chart-value changes, the release.
**There is no stakater/reloader deployed**, so a packages-only edit (ConfigMap content change) does NOT auto-reload HA:
```bash
# after Argo has synced the ha-packages ConfigMap:
kubectl -n home-assistant rollout restart deploy/ha-home-assistant
```
Watch the rollout with the `safe-rollout` skill / `Monitor`. The `check-config` **init container** re-validates before the main container starts — a non-zero exit there stalls the pod (HA down on this RWO volume), so validate first.

## Hard rules / gotchas
- **Never edit package automations in the HA UI** — it reports "Only automations in automations.yaml can be deleted"; they're read-only there by design.
- **yamlfmt corrupts HA YAML**: it strips quotes, and YAML 1.1 then coerces `'on'`→bool and `'HH:MM:SS'`→sexagesimal int. HA config dirs are excluded from yamlfmt in `.pre-commit-config.yaml`; keep them excluded and keep `'on'`/`'off'`/times quoted in packages. (Values inside the Argo `valuesObject` are safe — Go YAML re-quotes on render.)
- **Never `--no-verify`** — fix hook failures (repo rule S6).
- `.storage` islands (integrations/config_entries, entity/device/area registry, UI helpers, storage-mode dashboards) are **not** git-trackable — bootstrap manually, and rely on a `.storage` backup for recovery. Everything downstream (automations/scripts/scenes/templates/helpers/packages/blueprints/YAML dashboards) IS git-trackable.
- CI (`.github/workflows/ha-check-config.yaml`) runs this same check on every packages change; add new custom integrations to its fetch step.
