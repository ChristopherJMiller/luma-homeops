#!/usr/bin/env bash
# Offline route matrix for the sealed Alertmanager config. Prints receiver names only.
# Needs yq + amtool (nix develop). The decrypted copy is 0600 and deleted on exit.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
t=$(mktemp -d); trap 'rm -rf "$t"' EXIT
yq '.stringData["alertmanager.yaml"]' cluster/prometheus-stack/alertmanager-config.secret.yaml > "$t/am.yaml"
fail=0
check() { # receiver labels...
  local want=$1; shift
  if amtool config routes test --config.file "$t/am.yaml" --verify.receivers="$want" "$@" >/dev/null 2>&1; then
    echo "ok   $* -> $want"
  else echo "FAIL $* -> expected $want"; fail=1; fi
}
for a in CIDispatchSpendHigh CIDispatchLocked CIDispatchOrphanCompute CIDispatchDrift CIDispatchPaidNotReady CISweeperStale; do
  for s in warning critical; do check critical-alerts "alertname=$a" "severity=$s"; done
done
check homelab-default alertname=SomeOtherThing severity=warning
check critical-alerts alertname=KubeAPIDown severity=critical
exit $fail
