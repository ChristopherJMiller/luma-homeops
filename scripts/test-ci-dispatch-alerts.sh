#!/usr/bin/env bash
# Unit-test the five phase 1 alerts of ci-dispatch (cluster/ci-dispatch/dispatcher/prometheusrule.yaml)
# with promtool, against the cases in cluster/ci-dispatch/dispatcher/alerts_test.yaml: each alert
# fires on its trigger and stays quiet on its healthy twin. The rules file is linted first
# (promtool --lint-fatal: the two CIDispatchLaunchFailing rules need distinct `reason` labels).
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
