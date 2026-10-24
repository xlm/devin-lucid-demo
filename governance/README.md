# Privacy governance package

This is a synthetic demo of processing governance for the charging-session
processor. The declarations are review inputs, not proof of runtime
controls.

## How the pieces fit

- `governance/fields/charging.yaml` is the field manifest. It declares
  topics, fields, classifications, purposes, permitted destinations,
  logging, retention, access and encryption intent.
- `governance/lineage/charging.lineage.yaml` declares transformations,
  generated fields, validated-only input fields and restricted inputs.
- `governance/policies/migration-policies.yaml` is the policy rule source,
  including rule identifiers, severity, parameters and static/runtime
  meanings.
- `governance/policies/migration-policies.rego` is optional illustrative
  material for OPA users. It is not executed in this demo, and its behavior
  is not guaranteed to match the typed Python checker, which is authoritative
  and the default. The Rego file mirrors rule identifiers and intent, and a
  test keeps the identifier sets in sync.
- `governance/baselines/charging.baseline.json` is the human-approved
  baseline for the legacy processor. The privacy gate always compares
  against this file, regardless of the verification runner's baseline.
- `legacy-scala/processing-inventory.yaml` and
  `rust-processor/processing-inventory.yaml` declare processing for the
  legacy and candidate implementations. `implementation` is informational
  and is not a governed dimension.
- `verification/privacy/` validates these declarations, computes the
  inventory diff and applies the policy rules.
- `verification/evidence/` packages run context, gate results and output
  metadata for reviewer inspection.

The inventory has 12 dimensions, in this order: inputs, sensitive_fields,
purposes, transforms, output_schema, output_destinations, retention,
access, logging, external_deps, replication and deployment_boundary.

## Ownership and review

Humans own the baselines, policies, manifests, inventories and acceptance
tests. No agent or automated check edits them to force a pass. A baseline
change requires a human edit, recomputing `content_sha256` and review. The
hash detects silent edits, but does not authenticate the editor. Code
review and CODEOWNERS are the actual review controls.

Gate dispositions are recommendations for a human reviewer, not legal or
compliance determinations. A privacy reviewer reviews the package and
decides what action is appropriate. Only a human can move
`ReadyForReview -> ApprovedForCutover`. Baseline and policy changes are
human decisions.

## What the checks inspect

| Basis | Claims and limits |
|---|---|
| Runtime-observed | The privacy gate reads candidate summaries, errors and logs for the selected fixture scenarios. These checks describe only the files observed in that run. |
| Declared-manifest | Inventory, manifest, lineage, baseline, schema and policy comparisons inspect declarations only. They do not establish runtime behavior. |
| Source-scan | A heuristic scans Rust log-like macro arguments for identifier field names. It can miss logging a whole struct with `{:?}`, a pre-formatted string, aliases or other indirect flows. |

Declared retention, access control and encryption are not verified at
runtime. Runtime checks cover only the fixture scenarios. Source scanning
is not a substitute for code review or runtime evidence.

The demo covers fast-tier replay. The stated ordering guarantee is per
key only. Restart behavior is at-least-once. No exactly-once, global
ordering or atomic cutover claim is made. Passing checks are not proof of
production safety and do not approve release or remove privacy-review
obligations.

## Run

```sh
source scripts/_env.sh && $PY -m pytest -q verification/tests/test_privacy_*.py verification/tests/test_evidence_*.py
$PY verification/cli.py evidence
```
