# Workflow D: downstream compatibility, shadow, cutover, rollback

**Start:** tag `wf-d` (same commit as `consumer-contract-violation`).
**Reset:** `scripts/reset-state.sh wf-d`

## Problem
The candidate adds an output field `avg_power_kw` and serialises
timestamps as `+00:00` instead of `Z`. The warranty consumer must keep
working unchanged. Then rehearse shadow, cutover and rollback.

## Devin prompt
> Use playbooks/migration-playbook.md. On tag wf-d, make the Rust
> candidate compatible with consumer-warranty without changing
> consumer-warranty/ or contracts/. Explain each contract gate finding.
> Then run `verification/cli.py cutover simulate` and stop at
> ReadyForReview. Give me the approval command, but do not approve.

## Files to investigate
`contracts/consumer-contract.md`, `contracts/charging.session.summary.v1.schema.json`,
`consumer-warranty/src/warranty_consumer/`, `verification/contract_checks/gate.py`,
`build-output/latest/gates/contract.json`, `verification/cutover/`,
`docs/exactly-once-semantics.md`.

## Expected changes
Remove the extra field and restore timestamp formatting in the candidate.

## Verification
After committing the candidate fix, run the fast tier from a clean checkout.

```sh
scripts/run-fast.sh
"$PY" verification/cli.py cutover simulate --state-dir .cutover
# human, in a terminal:
"$PY" verification/cli.py cutover approve --state-dir .cutover --approver '<name>' --evidence-sha256 '<sha>'
"$PY" verification/cli.py cutover simulate --after-approval --state-dir .cutover
```

## Observable evidence
Before: contract `fail` on `contract.json_schema` (timestamp pattern), `contract.field_semantics`, `contract.no_extra_outputs` (`avg_power_kw`) and `contract.consumer_state`. Behavioural reports `SCHEMA_INCOMPATIBLE`, `WINDOWING_MISMATCH`, `SEMANTIC_MISMATCH`. Privacy reports `runtime.output_fields` because a new output field is new processing that needs review. After: contract gate passes, consumer state equal.
Cutover: `.cutover/state.json`, `.cutover/lease.json` fencing token, and
`.cutover/audit.jsonl`.

## Failure modes
Changing the consumer or schema instead of the producer. Agent running
`cutover approve`. Claiming the handover is atomic.

## Proves / does not prove
Proves the unchanged consumer reaches identical state on the fixtures,
and that the simulator refuses approval on stale or failing evidence and
rejects fenced producers. Does not prove atomic or exactly-once handover:
the lease is single-host and can leave a no-producer window.
