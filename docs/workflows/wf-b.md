# Workflow B: migrate to Rust

**Start:** tag `wf-b` (same commit as `rust-scaffold`). The Rust service
skeleton builds, `src/processors/charging_session.rs` is stubbed.
Intermediate state: tag `partial`.
**Reset:** `scripts/reset-state.sh wf-b`

## Problem
Implement the charging-session processor in Rust on the established
skeleton, behaviourally equivalent to the legacy job.

## Devin prompt
> Use playbooks/migration-playbook.md. On tag wf-b, implement the
> charging-session processor in rust-processor/src/processors/charging_session.rs
> so it is behaviourally equivalent to legacy-scala, following
> docs/build-contract.md. Keep the existing skeleton. Do not modify
> fixtures, contracts, verification/ or governance/. Iterate until
> scripts/run-fast.sh passes, then report gate results with check ids,
> the evidence sha256, and what the gates do not prove.

## Files to investigate
`rust-processor/src/` (all), `rust-processor/README.md`,
`legacy-scala/src/main/scala/.../charging/`, `docs/build-contract.md`
sections 4-8, `processors.yaml`.

## Expected changes
`rust-processor/src/processors/charging_session.rs` and its tests.

## Verification
```sh
(cd rust-processor && cargo fmt --check && cargo clippy -- -D warnings && cargo test)
scripts/run-fast.sh
```

## Observable evidence
`build-output/latest/gates/*.json` all `pass`,
`build-output/latest/evidence/evidence.html`.

## Failure modes
Summing energy deltas. Wall-clock timestamps in output. Logging
identifiers. Editing fixtures to match.

## Reset
`scripts/reset-state.sh wf-b`. From `partial`, the timeout, watermark
and restore rules remain.

## Proves / does not prove
Proves fixture-level equivalence per key with the legacy replay. Does not
prove Kafka behaviour, performance, or equivalence on unseen inputs.
