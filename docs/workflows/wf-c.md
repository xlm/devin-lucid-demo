# Workflow C: find and fix a behavioural bug (flagship)

**Start:** tag `wf-c` (same commit as `behavioural-defect`).
**Reset:** `scripts/reset-state.sh wf-c`

## Problem
The Rust candidate looks done. `cargo test` is green and its output is
schema-valid. But `total_energy_kwh` sums deltas of
`energy_delivered_kwh`, which is a cumulative reading. The warranty
consumer's per-vehicle energy totals would be wrong.

## Devin prompt
> Use playbooks/migration-playbook.md. On tag wf-c, scripts/run-fast.sh
> fails. Find the root cause, explain why the Rust crate's own tests did
> not catch it and why schema validation passes, fix the candidate (not
> the fixtures, gates or tests under verification/), and show the gates
> passing. Report the before/after failing check ids and the consumer
> state difference.

## Files to investigate
`build-output/latest/gates/behavioural.json`,
`build-output/latest/gates/contract.json`,
`build-output/latest/consumer/`, `rust-processor/src/processors/charging_session.rs`,
`legacy-scala/src/main/scala/com/example/telemetry/charging/SessionState.scala`,
`contracts/consumer-contract.md`.

## Expected changes
Accumulate `max(energy_delivered_kwh)`, and replace the crate's wrong
sum-based unit test with one that encodes the cumulative semantics.

## Verification
```sh
scripts/run-fast.sh          # fails before the fix, passes after
```

## Observable evidence
Before: behavioural `fail` with `<scenario>:candidate_vs_legacy:AGGREGATION_MISMATCH` and `NUMERIC_MISMATCH` on `total_energy_kwh`, contract `fail` on `contract.field_semantics` and `contract.consumer_state`, while `contract.json_schema` passes. Schema checks pass. After: all gates pass.

## Failure modes
Changing `fixtures/expected/`. Treating it as a numeric tolerance issue.
Leaving the wrong unit test in place.

## Proves / does not prove
Proves the gates catch a semantically wrong but schema-valid output, and
that consumer state is compared, not just records. Does not prove all
semantic bugs are caught: only those the fixtures exercise.
