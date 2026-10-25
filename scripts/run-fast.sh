#!/usr/bin/env bash
# Fast tier: fixtures -> both processors in replay mode -> gates -> evidence.
# No Kafka, no Docker. Extra args go to `cli.py verify` (e.g. --strict,
# --baseline expected, --scenario out_of_order).
source "$(dirname "$0")/_env.sh"
verify_args=("$@")
run_dir="$ROOT/build-output/latest"
for ((i = 0; i < ${#verify_args[@]}; i++)); do
  case "${verify_args[i]}" in
    --run-dir)
      if ((i + 1 < ${#verify_args[@]})); then
        run_dir="${verify_args[i + 1]}"
      fi
      ;;
    --run-dir=*)
      run_dir="${verify_args[i]#--run-dir=}"
      ;;
  esac
done
verify_status=0
"$PY" verification/cli.py verify "${verify_args[@]}" || verify_status=$?
evidence_status=0
"$PY" verification/cli.py evidence --run-dir "$run_dir" || evidence_status=$?
if (( verify_status != 0 )); then
  exit "$verify_status"
fi
exit "$evidence_status"
