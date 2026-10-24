#!/usr/bin/env bash
# Fast tier: fixtures -> both processors in replay mode -> gates -> evidence.
# No Kafka, no Docker. Extra args go to `cli.py verify` (e.g. --strict,
# --baseline expected, --scenario out_of_order).
source "$(dirname "$0")/_env.sh"
status=0
"$PY" verification/cli.py verify "$@" || status=$?
"$PY" verification/cli.py evidence || true
exit $status
