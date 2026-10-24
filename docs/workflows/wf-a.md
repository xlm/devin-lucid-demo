# Workflow A: understand the legacy processor

**Start:** tag `wf-a` (same commit as `legacy-baseline`). No Rust candidate exists.
**Reset:** `scripts/reset-state.sh wf-a`

## Problem
A team inherits the Scala/Spark charging-session job and must know its
exact behaviour before migrating it.

## Devin prompt
> Use playbooks/migration-playbook.md, steps 1-2. On tag wf-a, explain
> exactly what legacy-scala's charging-session processor does: session
> key, dedupe, completion rules, watermark and late data, error paths,
> and how each summary field is computed. Cite file and line for every
> rule, and confirm your explanation against fixtures/expected by running
> the legacy self-check. Call out anything that would be easy to get
> wrong in a rewrite. Do not change code.

## Files to investigate
`legacy-scala/src/main/scala/com/example/telemetry/charging/`
(`SessionState.scala`, `Windowing.scala`, `EventValidator.scala`,
`ChargingSessionProcessor.scala`), `legacy-scala/README.md`,
`docs/build-contract.md`, `fixtures/scenarios/`, `fixtures/expected/`.

## Expected changes
None. The output is an explanation.

## Verification
```sh
legacy-scala/bin/build
legacy-scala/bin/check-fixtures          # 13/13 scenarios match
"$PY" verification/cli.py verify          # exits 1 with candidate build errors, not a dedicated unavailable status
```

## Observable evidence
Self-check output, and an explanation that names the cumulative energy
rule (`total_energy_kwh` is the max of `energy_delivered_kwh`, not a sum),
the 10 min lateness and 30 min inactivity rules, and dedupe by
`event_id` keeping the first copy.

## Failure modes
Explaining from names instead of code. Missing the cumulative energy
trap. Claiming global ordering.

## Proves / does not prove
Proves the explanation is consistent with the committed fixtures. Does not
prove behaviour on inputs outside the fixtures, or on a Spark cluster.
