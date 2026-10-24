#!/usr/bin/env bash
# Optional integration tier: real topics, keys, offsets and transactions on
# Redpanda. Requires Docker. Not needed for the fast tier.
source "$(dirname "$0")/_env.sh"
if [[ ! -f verification/integration/__init__.py ]]; then
  echo "integration tier not implemented at this revision (see docs/limitations.md)" >&2
  exit 3
fi
scripts/start-infra.sh
"$PY" -m verification.integration "$@"
