#!/usr/bin/env bash
# Optional integration tier: real topics, keys, offsets and transactions on
# Redpanda. Requires Docker. Not needed for the fast tier.
source "$(dirname "$0")/_env.sh"
"$PY" -m pip install --quiet -r requirements-integration.txt
scripts/start-infra.sh
"$PY" -m verification.integration "$@"
