# Demo states

Each runnable state is a single commit based directly on the `build/integration`
tip after the cutover-scenario and evidence-runner fixes. The variants change
only the Rust processor and its crate tests, except `legacy-baseline`, which
removes `rust-processor/`. `validated-migration` is the integration tip itself.

| State | Workflow | Planted behavior | Behavioral | State ordering | Contract | Privacy | Recommendation | Default ref |
|---|---|---|---|---|---|---|---|---|
| `legacy-baseline` | A | Rust candidate removed. Legacy remains registered. Its build failure is reported without crashing the runner. | error | error | fail | error | `needs-clarification` | `demo/legacy-baseline` |
| `rust-scaffold` | B | Service skeleton and replay CLI remain. Charging-session processing is a workflow-B TODO stub that emits no records. | fail | fail | fail | pass | `no-material-change` | `demo/rust-scaffold` |
| `partial` | B | Validation, dedupe, accumulation, and COMPLETE closure remain. Timeout, watermark late-event routing, and checkpoint restore are workflow-C TODOs. | fail | fail | fail | pass | `no-material-change` | `demo/partial` |
| `behavioural-defect` | C | `total_energy_kwh` is the sum of reading deltas. A crate test encodes that wrong expectation. | fail | pass | fail | pass | `no-material-change` | `demo/behavioural-defect` |
| `consumer-contract-violation` | D | Summaries add `avg_power_kw` and timestamps use `+00:00` instead of `Z`. | fail | fail | fail | fail | `policy-violation` | `demo/consumer-contract-violation` |
| `privacy-violation` | E | The last accepted `synthetic_location` is copied to summaries. Session-close logs include `vehicle_id`. | fail | pass | fail | fail | `policy-violation` | `demo/privacy-violation` |
| `validated-migration` | Core | Validated integration tip with all four gates passing. | pass | pass | pass | pass | `no-material-change` | `build/integration` |
| `extra-unmigrated` | F | Integration tip with battery-health registered as an unmigrated legacy processor. Charging gates still pass. | pass | pass | pass | pass | `no-material-change` | `build/integration` |

Gate values are the statuses recorded in `build-output/latest/evidence/evidence.json`.
The checker also validates the privacy recommendation and the fast command's
exit status.

## Findings by workflow

- Workflow A's legacy fixture check passes all 13 scenarios. With the Rust
  directory removed, the current registry still invokes the candidate build.
  The runner records the build failure and returns rather than crashing.
  Behavioral, state-ordering, and privacy gates report `error`, while the
  contract gate reports `fail`. The privacy recommendation is
  `needs-clarification`.
- Workflow B's scaffold emits empty summaries and errors. Behavioral,
  state-ordering, and contract gates fail because expected output is absent.
- Workflow B's partial processor fails scenarios exercising the omitted
  behaviors: `concurrent_sessions`, `consumer_replay`, `downstream_contract`,
  `duplicates`, `late_events_window_boundary`, `processor_restart`, and
  `state_recovery`.
- Workflow C's behavioral defect fails `out_of_order` checks
  `out_of_order:candidate_vs_expected:AGGREGATION_MISMATCH`,
  `out_of_order:candidate_vs_expected:NUMERIC_MISMATCH`,
  `out_of_order:candidate_vs_legacy:AGGREGATION_MISMATCH`, and
  `out_of_order:candidate_vs_legacy:NUMERIC_MISMATCH`. Contract checks
  `contract.field_semantics` and `contract.consumer_state` fail for
  `out_of_order`. Schema checks pass.
- Workflow D's timestamp change is detected by behavioral
  `SCHEMA_INCOMPATIBLE` and `WINDOWING_MISMATCH` findings and contract
  `contract.json_schema` checks. The extra `avg_power_kw` field also triggers
  `contract.no_extra_outputs`. The privacy gate fails because the new output
  field is outside the baseline schema.
- Workflow E's privacy gate reports `runtime.no_sensitive_location`,
  `runtime.no_identifiers_in_log`, and `runtime.output_fields`, with the
  recommendation `policy-violation`. Behavioral and contract gates also fail
  because the extra summary field violates the output schema. State ordering
  passes.
- Workflow F uses the integration tip, which includes the planned second
  processor (battery-health) as unmigrated. All charging gates pass.

## Checking states

Run the branch-backed checker with:

```sh
scripts/check-demo-states.sh
```

When state-name tags are created later, the same table can be checked with:

```sh
scripts/check-demo-states.sh --refs tags
```

The checker creates detached worktrees under
`build-output/demo-states/<state>`, runs `scripts/run-fast.sh` in each, checks
the four gate statuses and privacy recommendation, prints a PASS/FAIL table,
and removes the worktrees. It does not create tags.

## Reset procedure

After selecting a state tag, reset the repository using:

```sh
git checkout <tag> && scripts/reset-state.sh
```

These synthetic demonstrations illustrate migration and review workflows.
They do not establish production safety, atomic cutover, global ordering, or
end-to-end exactly-once delivery. Privacy evidence is for human review and does
not replace it. Agents must not approve cutover.
