# Evidence coverage matrix

This matrix maps the synthetic demo's requirements to executable checks and
artifacts. From the repository root, set `PATH` to include
`$HOME/.local/sbt/bin` and source `scripts/_env.sh` before running commands
that use `$PY`.

| Requirement | Repository component | Executable check / command | Generated evidence artifact | Live Devin workflow (A-F) | Residual risk / limitation |
|---|---|---|---|---|---|
| Downstream consumer continuity | `consumer-warranty/`, `verification/contract_checks/gate.py` | `"$PY" verification/cli.py verify --baseline expected --skip-build --gates behavioural contract state_ordering --run-dir build-output/docs-checks` | `build-output/docs-checks/gates/contract.json`; `build-output/docs-checks/consumer/{baseline,candidate}/<scenario>.json` and `.rejects.json` | D | The consumer upserts by `(vehicle_id, session_id)`. It absorbs identical replays, not conflicting values or missing summaries. Ordering is per vehicle only. |
| Behavioural equivalence | `legacy-scala/`, `rust-processor/`, `verification/diff_harness/`, `fixtures/expected/` | `scripts/run-fast.sh` | `build-output/latest/gates/behavioural.json`; `build-output/latest/outputs/{legacy,candidate}/<scenario>/{summaries,errors}.jsonl` | A, B, C | Full comparison is fixture-bounded. Numeric tolerance can hide small differences. `--strict` uses exact binary64 equality, not decimal arithmetic. |
| Event ordering & duplicate handling | `verification/diff_harness/state_ordering.py`, `verification/diff_harness/comparator.py` | `"$PY" verification/cli.py verify --baseline expected --skip-build --gates behavioural contract state_ordering --run-dir build-output/docs-checks` | `build-output/docs-checks/gates/state_ordering.json`; checks include `partitioning_ordering:comparator:cross_key_permutation_passes`, `partitioning_ordering:comparator:within_key_swap_fails`, and `duplicates:candidate_vs_expected:DUPLICATE_OUTPUT` in the behavioral gate | B, D | Comparisons preserve order within a `(vehicle_id, session_id)` key. They do not establish global order across keys or live delivery behavior. |
| State recovery | `verification/runner.py`, `verification/diff_harness/state_ordering.py`, `rust-processor/src/state.rs` | `"$PY" verification/cli.py verify --baseline expected --skip-build --gates behavioural contract state_ordering --run-dir build-output/docs-checks` | `build-output/docs-checks/gates/state_ordering.json`; `build-output/docs-checks/outputs/candidate/processor_restart/{run.json,summaries.jsonl,errors.jsonl}`; `build-output/docs-checks/checkpoints/candidate/processor_restart.ckpt` | B, D | Replay restart is at-least-once. Output files and checkpoints are separate writes. The consumer upsert mitigates identical duplicates but cannot recover gaps. |
| Safe cutover & rollback | `verification/cutover/` | `"$PY" verification/cli.py cutover simulate` after a clean run has generated all gates | `build-output/cutover-sim-*/{state.json,lease.json,audit.jsonl,topic.jsonl}` and isolated `shadow/` outputs | D | The lease uses one host and `fcntl.flock`. Handover is not atomic and can leave a no-producer window. Approval is a human action. |
| Privacy/data-handling equivalence | `governance/`, `verification/privacy/` | `scripts/run-fast.sh` | `build-output/latest/gates/privacy.json`, including the declared inventory diff and check bases | E | Manifest and lineage checks inspect declarations. Runtime checks cover selected synthetic outputs only. Passing checks do not discharge privacy review. |
| Automated policy-violation detection | `verification/privacy/policy.py`, `verification/privacy/runtime.py`, `verification/privacy/static_checks.py` | `scripts/run-fast.sh` | `build-output/latest/gates/privacy.json`; examples include `policy.no-sensitive-location-propagation.static`, `runtime.no_sensitive_location`, and `source_scan.identifier_logging` | E | Static rules evaluate declared manifests. Runtime checks inspect fixture outputs. The source scan is heuristic and can miss indirect logging. |
| Privacy-review evidence generation | `verification/evidence/` | `"$PY" verification/cli.py evidence --run-dir build-output/latest` | `build-output/latest/evidence/{evidence.json,evidence.md,evidence.html,evidence.sha256}` | E | The package hash detects content changes, not who authored them. Evidence is for human review and is not approval. `scripts/run-fast.sh` runs evidence even after gate failure and propagates either failure. |
| Repeatable migration of subsequent jobs | `processors.yaml` (`unmigrated:`), `legacy-scala/bin/check-battery-fixtures`, `docs/battery-health-processor.md` | `legacy-scala/bin/check-battery-fixtures`; `scripts/run-fast.sh` | Battery fixture self-check output; charging-only `build-output/latest/evidence/evidence.json` | F | Gates are exercised for charging only until a battery candidate is registered. |

Demo tags: Workflow A uses `wf-a`, B uses `wf-b`, C uses `wf-c`, D uses
`wf-d`, E uses `wf-e`, and F uses `wf-f`.

## How to read this

Gate JSON contains machine-readable status and checks. Run output directories
hold the processor JSONL and restart records. Evidence packages collect gate
results and file hashes. The workflow letter indicates
the live migration activity: A understand legacy, B migrate to Rust, C find
and fix the cumulative-versus-sum energy bug, D preserve downstream
compatibility and rehearse shadow/cutover/rollback, E detect and review
privacy changes, and F repeat the process for a subsequent processor.

## Not covered

- Production deployment, availability or operational readiness.
- A live Kafka or Redpanda integration run. `scripts/run-integration.sh`
  reports that the integration tier is not implemented at this revision.
- End-to-end exactly-once delivery, global ordering or atomic cutover.
- A real privacy review, legal determination or approval to release.
- Non-synthetic input data or a completed subsequent-job migration.
