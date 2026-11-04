#!/usr/bin/env bash
# Unit-test PagesRelayQueueStuck and PagesRelayPushLost (cluster/pages/monitoring.yaml) with promtool:
# it fires when sites have waited in the relay's queue for 15 minutes with no
# forward attempt at all, and stays quiet for a queue that drains, a site that
# waits out a hold (at most 5 min), an empty queue, and a queue whose forwards
# fail (PagesRelayUpstreamError's). Offline — no cluster access.
#
#   nix shell --inputs-from . nixpkgs#prometheus.cli nixpkgs#yq-go --command scripts/test-pages-alerts.sh
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
rules="$repo/cluster/pages/monitoring.yaml"
yq 'select(.kind == "PrometheusRule") | .spec' "$rules" > "$tmp/rules.yaml"
# The annotations as the rule has them (JSON is YAML), so the test checks the
# labels and the firing, not a copy of the prose.
ann=$(yq -o json -I0 'select(.kind == "PrometheusRule") | .spec.groups[].rules[] | select(.alert == "PagesRelayQueueStuck") | .annotations' "$rules")
[ -n "$ann" ] || { echo "PagesRelayQueueStuck not found in $rules" >&2; exit 1; }
lostann=$(yq -o json -I0 'select(.kind == "PrometheusRule") | .spec.groups[].rules[] | select(.alert == "PagesRelayPushLost") | .annotations' "$rules")
[ -n "$lostann" ] || { echo "PagesRelayPushLost not found in $rules" >&2; exit 1; }

depth='pages_relay_queue_depth{namespace="pages",job="pages-relay"}'
fwd='pages_relay_events_total{namespace="pages",job="pages-relay",outcome="forwarded"}'
err='pages_relay_events_total{namespace="pages",job="pages-relay",outcome="upstream_error"}'
lost='pages_relay_holds_total{namespace="pages",job="pages-relay",outcome="aborted_lost"}'
req='pages_relay_holds_total{namespace="pages",job="pages-relay",outcome="aborted"}'
cat > "$tmp/test.yaml" <<T
rule_files: [rules.yaml]
evaluation_interval: 1m
tests:
  - name: sites wait 15m and nothing is forwarded
    interval: 1m
    input_series:
      - {series: '$depth', values: '0 1x40'}
      - {series: '$fwd', values: '3x41'}
      - {series: '$err', values: '0x41'}
    alert_rule_test:
      - {eval_time: 10m, alertname: PagesRelayQueueStuck, exp_alerts: []}
      - eval_time: 17m
        alertname: PagesRelayQueueStuck
        exp_alerts:
          - exp_labels: {severity: warning, namespace: pages}
            exp_annotations: $ann
  - name: a site waits behind holds, forwards every 5m
    interval: 1m
    input_series:
      - {series: '$depth', values: '0 1x40'}
      - {series: '$fwd', values: '0x4 1x4 2x4 3x4 4x4 5x4 6x4 7x4 8x1'}
      - {series: '$err', values: '0x41'}
    alert_rule_test:
      - {eval_time: 30m, alertname: PagesRelayQueueStuck, exp_alerts: []}
  - name: empty queue, nothing to forward
    interval: 1m
    input_series:
      - {series: '$depth', values: '0x41'}
      - {series: '$fwd', values: '0x41'}
      - {series: '$err', values: '0x41'}
    alert_rule_test:
      - {eval_time: 30m, alertname: PagesRelayQueueStuck, exp_alerts: []}
  - name: a backlog that clears within 15m
    interval: 1m
    input_series:
      - {series: '$depth', values: '0 1x9 0x31'}
      - {series: '$fwd', values: '0x41'}
      - {series: '$err', values: '0x41'}
    alert_rule_test:
      - {eval_time: 9m, alertname: PagesRelayQueueStuck, exp_alerts: []}
      - {eval_time: 20m, alertname: PagesRelayQueueStuck, exp_alerts: []}
  - name: forwards that fail are attempts (PagesRelayUpstreamError alerts)
    interval: 1m
    input_series:
      - {series: '$depth', values: '0 1x40'}
      - {series: '$fwd', values: '0x41'}
      - {series: '$err', values: '0+1x41'}
    alert_rule_test:
      - {eval_time: 20m, alertname: PagesRelayQueueStuck, exp_alerts: []}
  - name: a lost push (a retry aborted too) fires
    interval: 1m
    input_series:
      - {series: '$lost', values: '0x5 1x20'}
      - {series: '$req', values: '0x25'}
    alert_rule_test:
      - {eval_time: 4m, alertname: PagesRelayPushLost, exp_alerts: []}
      - eval_time: 7m
        alertname: PagesRelayPushLost
        exp_alerts:
          - exp_labels: {severity: warning, namespace: pages}
            exp_annotations: $lostann
      - {eval_time: 20m, alertname: PagesRelayPushLost, exp_alerts: []}
  - name: an abort that was re-queued heals itself and stays quiet
    interval: 1m
    input_series:
      - {series: '$lost', values: '0x25'}
      - {series: '$req', values: '0x5 1 2x19'}
    alert_rule_test:
      - {eval_time: 7m, alertname: PagesRelayPushLost, exp_alerts: []}
      - {eval_time: 20m, alertname: PagesRelayPushLost, exp_alerts: []}
T
cd "$tmp" && promtool check rules rules.yaml && promtool test rules test.yaml
