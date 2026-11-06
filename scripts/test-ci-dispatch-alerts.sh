#!/usr/bin/env bash
# Unit-test ci-dispatch's alerts, phase 1's eight and phase 2's seven
# (cluster/ci-dispatch/dispatcher/prometheusrule.yaml), with promtool, against the cases in
# cluster/ci-dispatch/dispatcher/alerts_test.yaml: each alert fires on its trigger and stays quiet on
# its healthy twin. The rules file is linted first (promtool --lint-fatal: a name with two rules needs
# a label that tells them apart: `reason` for CIDispatchLaunchFailing and CISweeperStale, `level` for
# CIDispatchSpendHigh).
# Offline: no cluster access.
#
#   nix shell --inputs-from . nixpkgs#prometheus.cli nixpkgs#yq-go --command scripts/test-ci-dispatch-alerts.sh
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
dir="$repo/cluster/ci-dispatch/dispatcher"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
[ -f "$dir/prometheusrule.yaml" ] || { echo "missing $dir/prometheusrule.yaml" >&2; exit 1; }
yq 'select(.kind == "PrometheusRule") | .spec' "$dir/prometheusrule.yaml" > "$tmp/rules.yaml"
cp "$dir/alerts_test.yaml" "$tmp/alerts_test.yaml"
cd "$tmp"
promtool check rules --lint-fatal rules.yaml
promtool test rules alerts_test.yaml
