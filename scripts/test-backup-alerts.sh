#!/usr/bin/env bash
# Unit-test the backup alerts (cluster/backups/prometheusrule.yaml) with promtool: every nightly
# backup CronJob must be covered by BackupCronJobStale, BackupCronJobNeverSucceeded and
# BackupJobFailed. Offline — no cluster access.
#
#   nix shell nixpkgs#prometheus.cli nixpkgs#yq-go --command scripts/test-backup-alerts.sh
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
yq '.spec' "$repo/cluster/backups/prometheusrule.yaml" > "$tmp/rules.yaml"

# CronJob name -> namespace, for every backup the alerts must cover.
cronjobs="logical-backup-acid-forgejo:forgejo forgejo-dump:forgejo immich-backup:immich restic-ci-dispatch:ci-dispatch"
{
  echo "rule_files: [rules.yaml]"
  echo "evaluation_interval: 1m"
  echo "tests:"
  for cj in $cronjobs; do
    name=${cj%%:*}; ns=${cj#*:}
    cat <<T
  - name: $name never succeeded
    interval: 1m
    input_series:
      - series: 'kube_cronjob_info{namespace="$ns",cronjob="$name"}'
        values: '1x3100'
    alert_rule_test:
      - eval_time: 50h
        alertname: BackupCronJobNeverSucceeded
        exp_alerts:
          - exp_labels: {severity: warning, namespace: $ns, cronjob: $name}
            exp_annotations: {summary: "Backup $ns/$name has never succeeded"}
  - name: $name stale
    interval: 1m
    input_series:
      - series: 'kube_cronjob_status_last_successful_time{namespace="$ns",cronjob="$name"}'
        values: '0x2500'
    alert_rule_test:
      - eval_time: 40h
        alertname: BackupCronJobStale
        exp_alerts:
          - exp_labels: {severity: warning, namespace: $ns, cronjob: $name}
            exp_annotations:
              summary: "Backup $ns/$name has not succeeded in >36h"
              description: "Last success was 1d 16h 0m 0s ago. See docs/backups.md."
  - name: $name job failed
    interval: 1m
    input_series:
      - series: 'kube_job_failed{namespace="$ns",condition="true",job_name="$name-29361234"}'
        values: '1x30'
    alert_rule_test:
      - eval_time: 20m
        alertname: BackupJobFailed
        exp_alerts:
          - exp_labels: {severity: warning, namespace: $ns, condition: "true", job_name: $name-29361234}
            exp_annotations:
              summary: "Backup job $ns/$name-29361234 failed"
              description: "kubectl -n $ns logs job/$name-29361234"
T
  done
} > "$tmp/test.yaml"
cd "$tmp" && promtool test rules test.yaml
