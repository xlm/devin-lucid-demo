#!/usr/bin/env bash
# Fast tier: fixtures -> both processors in replay mode -> gates -> evidence.
# No Kafka, no Docker. Extra args go to `cli.py verify` (e.g. --strict,
# --baseline expected, --scenario out_of_order).
source "$(dirname "$0")/_env.sh"
verify_status=0
"$PY" verification/cli.py verify "$@" || verify_status=$?
evidence_status=0
"$PY" verification/cli.py evidence || evidence_status=$?
if (( verify_status != 0 )); then
  exit "$verify_status"
fi
exit "$evidence_status"
