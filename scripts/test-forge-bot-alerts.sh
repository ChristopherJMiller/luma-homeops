#!/usr/bin/env bash
# forge-bot's alerts as deployed (cluster/forge-bot/prometheusrule.yaml):
#   1. the PrometheusRule is named and labelled so this Prometheus loads it;
#   2. promtool check rules --lint=all --lint-fatal on its spec;
#   3. promtool test rules with forge-bot's own unit tests (cluster/forge-bot/alerts_test.yaml);
#   4. both copies still equal internal/metrics/alerts.yaml and alerts_test.yaml at the commit of
#      the image statefulset.yaml pins (scripts/sync-forge-bot-alerts.sh writes them).
# forge-bot's CI has no promtool (final review M12), so its alert tests run here. Like
# scripts/test-backup-alerts.sh: offline (after the first `nix shell` fetch), no cluster access.
#
#   nix develop --command scripts/test-forge-bot-alerts.sh      (also the pre-commit hook forge-bot-alerts)
#
# promtool comes from PATH, else `nix shell nixpkgs#prometheus.cli`. The forge-bot checkout for
# step 4 is $FORGE_BOT_SRC (default ~/Repos/forge-bot).
# Exit 0 pass, 1 fail, 2 could not run (no forge-bot checkout holding the pinned commit:
# git -C ~/Repos/forge-bot fetch).
set -euo pipefail
repo=$(cd "$(dirname "$0")/.." && pwd)
dir=$repo/cluster/forge-bot
src=${FORGE_BOT_SRC:-$HOME/Repos/forge-bot}
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

if command -v promtool >/dev/null; then
  promtool=(promtool)
else
  promtool=(nix shell nixpkgs#prometheus.cli -c promtool)
fi
# Inside a pre-commit hook git exports GIT_DIR, GIT_INDEX_FILE and friends for luma-homeops; drop
# them so -C reaches the forge-bot checkout. GIT_CONFIG_GLOBAL: Chris's URL rewrites never apply.
fgit() {
  env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_COMMON_DIR -u GIT_OBJECT_DIRECTORY \
    -u GIT_ALTERNATE_OBJECT_DIRECTORIES -u GIT_PREFIX GIT_CONFIG_GLOBAL=/dev/null git -C "$src" "$@"
}

# Step 4's inputs: the pinned commit's two files.
short=$(sed -nE 's#^ *image: git\.realliance\.net/realliance-ops/forge-bot:sha-([0-9a-f]{7})@sha256:[0-9a-f]{64}$#\1#p' "$dir/statefulset.yaml")
[ "$(printf '%s\n' "$short" | grep -c .)" -eq 1 ] || { echo "FAIL: expected exactly one pinned forge-bot image in statefulset.yaml" >&2; exit 1; }
full=$(fgit rev-parse --verify --quiet "$short^{commit}") ||
  { echo "could not run: commit $short is not in $src (git -C $src fetch, or set FORGE_BOT_SRC)" >&2; exit 2; }
mkdir "$tmp/upstream"
fgit show "$full:internal/metrics/alerts.yaml" > "$tmp/upstream/alerts.yaml"
fgit show "$full:internal/metrics/alerts_test.yaml" > "$tmp/upstream/alerts_test.yaml"

# Steps 1 and 4, and the spec extracted for promtool. The test file names its rules alerts.yaml.
python3 - "$dir" "$tmp" <<'PY'
import sys
from pathlib import Path

import yaml

d, tmp = Path(sys.argv[1]), Path(sys.argv[2])
fail = []
docs = [x for x in yaml.safe_load_all((d / "prometheusrule.yaml").read_text()) if x is not None]
if len(docs) != 1 or docs[0].get("kind") != "PrometheusRule":
    sys.exit("FAIL: prometheusrule.yaml must hold exactly one PrometheusRule")
rule = docs[0]
if rule["metadata"].get("name") != "forge-bot-alerts":
    fail.append("the PrometheusRule must be named forge-bot-alerts")
want = {"prometheus": "kube-prometheus", "role": "alert-rules", "release": "prometheus"}
labels = rule["metadata"].get("labels", {})
if {k: labels.get(k) for k in want} != want:
    fail.append(f"labels {labels} lack {want}; release=prometheus is what the ruleSelector picks")
(tmp / "alerts.yaml").write_text(yaml.safe_dump(rule["spec"], sort_keys=False))
if rule["spec"] != yaml.safe_load((tmp / "upstream/alerts.yaml").read_text()):
    fail.append("prometheusrule.yaml spec differs from the pinned commit's internal/metrics/alerts.yaml"
                " (run scripts/sync-forge-bot-alerts.sh)")
if (d / "alerts_test.yaml").read_bytes() != (tmp / "upstream/alerts_test.yaml").read_bytes():
    fail.append("alerts_test.yaml differs from the pinned commit's internal/metrics/alerts_test.yaml"
                " (run scripts/sync-forge-bot-alerts.sh)")
for f in fail:
    print("FAIL:", f, file=sys.stderr)
sys.exit(1 if fail else 0)
PY
echo "in sync with forge-bot $full"

cp "$dir/alerts_test.yaml" "$tmp/alerts_test.yaml"
"${promtool[@]}" check rules --lint=all --lint-fatal "$tmp/alerts.yaml"
(cd "$tmp" && "${promtool[@]}" test rules alerts_test.yaml)

# The site-owned rules (prometheusrule-site.yaml) have no upstream copy: lint and test them alone.
python3 - "$dir" "$tmp" <<'PY'
import sys
from pathlib import Path

import yaml

d, tmp = Path(sys.argv[1]), Path(sys.argv[2])
docs = [x for x in yaml.safe_load_all((d / "prometheusrule-site.yaml").read_text()) if x is not None]
if len(docs) != 1 or docs[0].get("kind") != "PrometheusRule":
    sys.exit("FAIL: prometheusrule-site.yaml must hold exactly one PrometheusRule")
labels = docs[0]["metadata"].get("labels", {})
if labels.get("release") != "prometheus":
    sys.exit("FAIL: prometheusrule-site.yaml lacks release=prometheus")
(tmp / "alerts_site.yaml").write_text(yaml.safe_dump(docs[0]["spec"], sort_keys=False))
PY
cp "$dir/alerts_site_test.yaml" "$tmp/alerts_site_test.yaml"
"${promtool[@]}" check rules --lint=all --lint-fatal "$tmp/alerts_site.yaml"
(cd "$tmp" && "${promtool[@]}" test rules alerts_site_test.yaml)
