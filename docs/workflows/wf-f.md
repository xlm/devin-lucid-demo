# Workflow F: repeat the process on a second processor

**Start:** tag `wf-f` (same commit as `extra-unmigrated`). The
battery-health legacy job is registered under `unmigrated:` in
`processors.yaml` with no Rust candidate.
**Reset:** `scripts/reset-state.sh wf-f`

## Problem
Show the process is repeatable: migrate battery-health using the same
playbook, gates and evidence.

## Devin prompt
> Use playbooks/migration-playbook.md to migrate the battery-health
> processor (docs/battery-health-processor.md) to Rust on the existing
> rust-processor skeleton. Reuse the charging migration's structure.
> Register the candidate in processors.yaml, add the processing
> inventory, run the gates and evidence, and report what was reused, what
> was new, and any gap in the verification framework for this processor.
> Do not edit fixtures/battery/expected, contracts/ or governance
> baselines and policies.

## Files to investigate
`docs/battery-health-processor.md`,
`legacy-scala/src/main/scala/com/example/telemetry/battery/`,
`fixtures/battery/`, `contracts/battery.*`, `governance/*/battery*`,
`processors.yaml`, the charging migration in `rust-processor/`.

## Expected changes
A new `rust-processor/src/processors/battery_health.rs`, a registry entry,
an inventory, tests.

## Verification
```sh
legacy-scala/bin/check-battery-fixtures
(cd rust-processor && cargo test)
scripts/run-fast.sh
```

## Observable evidence
Battery fixtures matched by the candidate, a gap list. The trap:
`max_cell_imbalance_v` is the max per-event spread, not daily max minus
daily min.

## Failure modes
Computing imbalance from daily extremes. Assuming the charging gates
cover battery without registering it.

## Proves / does not prove
Proves the playbook and fixture approach transfer to a second job. Does
not prove the verification harness is fully processor-generic: gaps found
here are expected output of the workflow.
