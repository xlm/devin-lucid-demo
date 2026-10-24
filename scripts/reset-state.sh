#!/usr/bin/env bash
# Remove generated run state; optionally check out a demo tag.
# Usage: scripts/reset-state.sh [<tag>]   e.g. scripts/reset-state.sh wf-c
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
rm -rf build-output .cutover
if [[ $# -ge 1 ]]; then
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    echo "working tree has local changes; commit or stash them first" >&2
    exit 1
  fi
  git fetch --tags --quiet origin 2>/dev/null || true
  git checkout --quiet --detach "$1"
  echo "checked out $1 ($(git rev-parse --short HEAD))"
fi
echo "state reset"
