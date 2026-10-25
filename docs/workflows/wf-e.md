# Workflow E: privacy evidence, violation detect, explain, remediate

**Start:** tag `wf-e` (same commit as `privacy-violation`).
**Reset:** `scripts/reset-state.sh wf-e`

## Problem
The candidate copies `synthetic_location` (restricted) into each summary
and logs `vehicle_id` on stderr. Both are policy violations.

## Devin prompt
> Use playbooks/migration-playbook.md. On tag wf-e, run the fast tier and
> explain each privacy finding: what data flows where, which policy it
> breaks, and whether the finding is runtime-inspected or declaration
> only. Remediate in the candidate. Do not edit governance/policies,
> governance/baselines or verification/. Regenerate the evidence package
> and summarise sections C-E for a human privacy reviewer.

## Files to investigate
`governance/` (fields, lineage, policies, baselines, README),
`rust-processor/processing-inventory.yaml`, `verification/privacy/`,
`build-output/latest/gates/privacy.json`, `build-output/latest/evidence/`.

## Expected changes
Drop the location from the summary, remove identifiers from logs, keep the
inventory accurate.

## Verification
```sh
scripts/run-fast.sh
open build-output/latest/evidence/evidence.html
```

## Observable evidence
Before: privacy `fail`, recommendation `policy-violation`, checks
`runtime.no_sensitive_location`, `runtime.output_fields`, `runtime.no_identifiers_in_log` and `source_scan.identifier_logging`. After: `no-material-change`, evidence sha256 changes.

## Failure modes
Editing the baseline or policy. Declaring compliance. Hiding the log
line behind a log level instead of removing the identifier.

## Proves / does not prove
Proves the automated checks detect these two leaks in runtime output and
logs, and that evidence is generated and hashed. Does not prove privacy
compliance or discharge the human privacy review. Lineage claims are
declared, not traced.
