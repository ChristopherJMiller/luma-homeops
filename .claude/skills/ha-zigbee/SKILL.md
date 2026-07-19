---
name: ha-zigbee
description: Diagnose and manage Zigbee devices via zigbee2mqtt (which runs on the VyOS router, not the cluster) — watch pairings live, rename/remove devices, clean up stale entries, and correlate to Home Assistant. Use when onboarding/re-pairing Zigbee devices, when HA shows a Zigbee device unavailable, or investigating a Zigbee mesh problem. NEVER ssh-rebuild or nixos-rebuild the router. NEVER leave permit_join on indefinitely.
---

# ha-zigbee

Zigbee is **not** in the cluster. `zigbee2mqtt` (z2m) runs as a podman container on the VyOS router (`192.168.0.1`, config in `router/ansible/`, data `/mnt/zigbee2mqtt/`, TI Z-Stack coordinator on `/dev/ttyUSB0`, frontend `:8585`). It publishes to the in-cluster **mosquitto** broker (`192.168.0.8`, namespace `home-assistant`, anonymous auth), which HA consumes. A **separate Philips Hue Bridge** also feeds HA — don't confuse Hue-bridge lights with Zigbee-on-z2m devices.

## Watch pairings live (best diagnostic)
z2m logs to stdout (podman), at debug level. Filter hard or it's a firehose:
```bash
ssh -o ServerAliveInterval=15 chris@192.168.0.1 'sudo podman logs -f zigbee2mqtt 2>&1' \
  | grep --line-buffered -E "z2m: (Starting interview|Successfully interviewed|Failed to interview|Device '[^']*' (joined|reconnected|left the network))|been paired" \
  | sed -u -E 's/\x1b\[[0-9;]*m//g'
```
Use the `Monitor` tool for this (sudo is passwordless on the router). Success = `Successfully interviewed 'X', device has successfully been paired`.

**Xiaomi/Aqara sensors fail the interview by falling asleep** (`Failed to interview … not successfully paired`). Re-pair and **press the device's reset button every 2–3s** to keep it awake through the ~15s interview.

## Manage devices via MQTT (rename / remove)
z2m acts on `zigbee2mqtt/bridge/request/...`. Publish from the mosquitto pod:
```bash
export KUBECONFIG=/tmp/galaxy-kubeconfig
MP=$(kubectl -n home-assistant get pods -o name | grep mosquitto | head -1)
# rename (updates z2m DB + HA discovery):
kubectl -n home-assistant exec "$MP" -c mosquitto -- \
  mosquitto_pub -h localhost -t zigbee2mqtt/bridge/request/device/rename \
  -m '{"from":"<current-name-or-ieee>","to":"<new-name>"}'
# remove (force needed for offline devices):
kubectl -n home-assistant exec "$MP" -c mosquitto -- \
  mosquitto_pub -h localhost -t zigbee2mqtt/bridge/request/device/remove \
  -m '{"id":"0x<ieee>","force":true}'
```
Confirm on `zigbee2mqtt/bridge/response/device/#` (subscribe with `mosquitto_sub -C 1 -W 8`); look for `"status":"ok"`.

**z2m leaves retained cruft on remove**: `device_automation` trigger configs under `homeassistant/.../config` and `zigbee2mqtt/<id>/availability` are NOT cleared, leaving HA ghost entities. Flush them:
```bash
kubectl -n home-assistant exec "$MP" -c mosquitto -- sh -c '
  for t in $(mosquitto_sub -h localhost -t "homeassistant/+/+/+/config" -v -W 5 | awk "/0x<ieee>/{print \$1}" | sort -u); do
    mosquitto_pub -h localhost -t "$t" -r -n; done'
```

## Inspect state (no live traffic needed)
On the router: `/mnt/zigbee2mqtt/state.json` (per-device state + linkquality), `database.db` (registry: ieee, type, model, `lastSeen` epoch-ms — convert with `date -u -d @$((ms/1000))`). Do NOT dump `configuration.yaml` wholesale — it holds the network key + MQTT creds.

Read broker state: `mosquitto_sub -t zigbee2mqtt/+/availability -v -W 5` (online/offline per device), `zigbee2mqtt/bridge/info` (version, permit_join, coordinator firmware).

## Correlate to HA
An HA entity `unavailable` + a z2m device with an old `lastSeen` = the device dropped off the mesh (needs re-pair or is unpowered). After a z2m rename, **HA keeps the old sticky entity_id** (only the friendly name changes) — rename the entity_id in the HA UI registry for clean automations (see the `ha-config` skill).

## Rules
- **Pull, never push to the router**: change z2m config in `router/ansible/` and apply via the `vyos-deploy` skill; don't hand-edit the container.
- **permit_join**: turn it OFF once done pairing (leaving it on is a stability/security risk). Check via `bridge/info`.
- z2m is old (v1.40.x, 2021 coordinator firmware) — a z2m 2.x + coordinator flash is a known future task; treat as a planned, gated change.
