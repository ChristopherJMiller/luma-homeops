#!/usr/bin/env bash
# Every kustomization under cluster/ must build.
#
# Exists because a kustomization.yaml can reference a resource file that is not
# actually committed — `git commit -am` stages only TRACKED files, so a newly
# created resource silently stays out of the tree. Argo then fails at
# `kustomize build` and the WHOLE Application stops generating manifests
# (ComparisonError, sync=Unknown), not just that one resource. This happened to
# cluster/immich on 2026-09-23.
#
# pre-commit stashes unstaged and untracked files before running hooks, so what
# this builds is what the commit will contain — the same tree Argo will see.
#
# Build output goes to a file, never a $(command substitution): some of these
# render large (cluster/grafana's dashboards especially) and holding that in a
# shell variable made the NEXT iteration's exec fail with E2BIG.
set -uo pipefail

tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
fail=0

while IFS= read -r k; do
  d="${k%/*}"
  if ! kustomize build "$d" > "$tmp" 2>&1; then
    echo "FAIL $d" >&2
    head -5 "$tmp" | sed 's/^/      /' >&2
    fail=1
  fi
done < <(find cluster -name kustomization.yaml | sort)

[ "$fail" -eq 0 ] || { echo "one or more kustomizations do not build — Argo would reject these." >&2; exit 1; }
