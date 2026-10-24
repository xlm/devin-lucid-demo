#!/usr/bin/env bash
set -uo pipefail

repo_root=$(git rev-parse --show-toplevel) || exit 2
source "$repo_root/scripts/_env.sh"
set +e

refs=branches
if [[ $# -gt 0 ]]; then
  if [[ $# -ne 2 || "$1" != "--refs" || ( "$2" != "branches" && "$2" != "tags" ) ]]; then
    printf 'usage: %s [--refs branches|tags]\n' "$0" >&2
    exit 2
  fi
  refs=$2
fi

states=(
  legacy-baseline
  rust-scaffold
  partial
  behavioural-defect
  consumer-contract-violation
  privacy-violation
  validated-migration
  extra-unmigrated
)

declare -A expected_behavioural=(
  [legacy-baseline]=error
  [rust-scaffold]=fail
  [partial]=fail
  [behavioural-defect]=fail
  [consumer-contract-violation]=fail
  [privacy-violation]=fail
  [validated-migration]=pass
  [extra-unmigrated]=pass
)
declare -A expected_state_ordering=(
  [legacy-baseline]=error
  [rust-scaffold]=fail
  [partial]=fail
  [behavioural-defect]=pass
  [consumer-contract-violation]=fail
  [privacy-violation]=pass
  [validated-migration]=pass
  [extra-unmigrated]=pass
)
declare -A expected_contract=(
  [legacy-baseline]=fail
  [rust-scaffold]=fail
  [partial]=fail
  [behavioural-defect]=fail
  [consumer-contract-violation]=fail
  [privacy-violation]=fail
  [validated-migration]=pass
  [extra-unmigrated]=pass
)
declare -A expected_privacy=(
  [legacy-baseline]=error
  [rust-scaffold]=pass
  [partial]=pass
  [behavioural-defect]=pass
  [consumer-contract-violation]=fail
  [privacy-violation]=fail
  [validated-migration]=pass
  [extra-unmigrated]=pass
)
declare -A expected_recommendation=(
  [legacy-baseline]=needs-clarification
  [rust-scaffold]=no-material-change
  [partial]=no-material-change
  [behavioural-defect]=no-material-change
  [consumer-contract-violation]=policy-violation
  [privacy-violation]=policy-violation
  [validated-migration]=no-material-change
  [extra-unmigrated]=no-material-change
)

worktree_base="$repo_root/build-output/demo-states"
mkdir -p "$worktree_base" || exit 2
active_worktrees=()

cleanup() {
  local worktree
  for worktree in "${active_worktrees[@]}"; do
    git -C "$repo_root" worktree remove --force "$worktree" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

printf '%-30s %-6s %-8s %-8s %-8s %-8s %-23s\n' \
  "State" "Result" "Behavior" "Ordering" "Contract" "Privacy" "Recommendation"
printf '%-30s %-6s %-8s %-8s %-8s %-8s %-23s\n' \
  "------------------------------" "------" "--------" "--------" "--------" "--------" "-----------------------"

overall=0
for state in "${states[@]}"; do
  if [[ "$refs" == tags ]]; then
    ref=$state
  elif [[ "$state" == validated-migration || "$state" == extra-unmigrated ]]; then
    ref=build/integration
  else
    ref="demo/$state"
  fi

  worktree="$worktree_base/$state"
  log_path="$worktree_base/$state.log"
  if [[ -e "$worktree" ]]; then
    printf '%-30s %-6s %-8s %-8s %-8s %-8s %-23s\n' \
      "$state" "FAIL" "missing" "missing" "missing" "missing" "missing"
    printf 'worktree path already exists: %s\n' "$worktree" >&2
    overall=1
    continue
  fi

  active_worktrees+=("$worktree")
  if ! git -C "$repo_root" worktree add --detach "$worktree" "$ref" >"$log_path" 2>&1; then
    printf '%-30s %-6s %-8s %-8s %-8s %-8s %-23s\n' \
      "$state" "FAIL" "missing" "missing" "missing" "missing" "missing"
    printf 'could not create worktree for %s. Details: %s\n' "$ref" "$log_path" >&2
    git -C "$repo_root" worktree remove --force "$worktree" >/dev/null 2>&1 || true
    active_worktrees=()
    overall=1
    continue
  fi

  (
    cd "$worktree" &&
      scripts/run-fast.sh
  ) >"$log_path" 2>&1
  run_status=$?

  evidence="$worktree/build-output/latest/evidence/evidence.json"
  actual_behavioural=missing
  actual_state_ordering=missing
  actual_contract=missing
  actual_privacy=missing
  actual_recommendation=missing
  if [[ -f "$evidence" ]]; then
    mapfile -t observed < <(
      "$PY" -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    evidence = json.load(stream)
sections = evidence.get("sections", {})
gates = sections.get("D", {}).get("gates", {})
print(gates.get("behavioural", {}).get("status", "missing"))
print(gates.get("state_ordering", {}).get("status", "missing"))
print(gates.get("contract", {}).get("status", "missing"))
print(gates.get("privacy", {}).get("status", "missing"))
print(sections.get("E", {}).get("disposition", {}).get("recommendation", "missing"))
' "$evidence" 2>/dev/null
    )
    if [[ ${#observed[@]} -eq 5 ]]; then
      actual_behavioural=${observed[0]}
      actual_state_ordering=${observed[1]}
      actual_contract=${observed[2]}
      actual_privacy=${observed[3]}
      actual_recommendation=${observed[4]}
    fi
  fi

  result=PASS
  if [[ "$actual_behavioural" != "${expected_behavioural[$state]}" ||
        "$actual_state_ordering" != "${expected_state_ordering[$state]}" ||
        "$actual_contract" != "${expected_contract[$state]}" ||
        "$actual_privacy" != "${expected_privacy[$state]}" ||
        "$actual_recommendation" != "${expected_recommendation[$state]}" ]]; then
    result=FAIL
  fi
  if [[ "$actual_behavioural" == pass &&
        "$actual_state_ordering" == pass &&
        "$actual_contract" == pass &&
        "$actual_privacy" == pass ]]; then
    if [[ $run_status -ne 0 ]]; then
      result=FAIL
    fi
  elif [[ $run_status -eq 0 ]]; then
    result=FAIL
  fi

  printf '%-30s %-6s %-8s %-8s %-8s %-8s %-23s\n' \
    "$state" "$result" "$actual_behavioural" "$actual_state_ordering" \
    "$actual_contract" "$actual_privacy" "$actual_recommendation"
  if [[ "$result" == FAIL ]]; then
    printf 'unexpected results for %s. Details: %s\n' "$state" "$log_path" >&2
    overall=1
  fi

  if ! git -C "$repo_root" worktree remove --force "$worktree" >/dev/null 2>&1; then
    printf 'could not remove worktree: %s\n' "$worktree" >&2
    overall=1
  else
    active_worktrees=()
  fi
done

exit "$overall"
