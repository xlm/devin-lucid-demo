# Evidence coverage matrix

This matrix maps the synthetic demo's requirements to executable checks and
artifacts. From the repository root, set `PATH` to include
`$HOME/.local/sbt/bin` and source `scripts/_env.sh` before running commands
that use `$PY`.

| Requirement | Repository component | Executable check / command | Generated evidence artifact | Live Devin workflow (A-F) | Residual risk / limitation |
|---|---|---|---|---|---|
| Downstream consumer continuity | `consumer-warranty/`, `verification/contract_checks/gate.py` | `"$PY" verification/cli.py verify --baseline expected --skip-build --gates behavioural contract state_ordering --run-dir build-output/docs-checks` | `build-output/docs-checks/gates/contract.json`; `build-output/docs-checks/consumer/{baseline,candidate}/<scenario>.json` and `.rejects.json` | D | The consumer upserts by `(vehicle_id, session_id)`. It absorbs identical replays, not conflicting values or missing summaries. Ordering is per vehicle only. |
| Behavioural equivalence | `legacy-scala/`, `rust-processor/`, `verification/diff_harness/`, `fixtures/expected/` | `scripts/run-fast.sh`; `scripts/run-integration.sh` | Fast outputs in `build-output/latest/`; Kafka outputs in `build-output/integration/outputs/candidate-kafka/`; behavioral gate JSON in each run's `gates/` | A, B, C | Full comparison is fixture-bounded. Numeric tolerance can hide small differences. The integration sentinel flushes inactivity timers but is filtered from saved output. |
| Event ordering & duplicate handling | `verification/diff_harness/state_ordering.py`, `verification/diff_harness/comparator.py`, `verification/integration/` | Fast gate command above; `scripts/run-integration.sh` | Fast `state_ordering.json`; integration `gates/integration.json` with `kafka.key_partition_affinity`, `kafka.offsets_committed`, and `kafka.no_reprocessing_on_restart` checks | B, D | Kafka input has one partition because the watermark is global. Summary checks enforce key affinity and expected per-vehicle order. Neither tier establishes global order across keys or end-to-end exactly-once delivery. |
| State recovery | `verification/runner.py`, `verification/diff_harness/state_ordering.py`, `rust-processor/src/state.rs`, `verification/integration/` | Fast restart verification command above; `scripts/run-integration.sh` | Fast checkpoints and `state_ordering.json`; integration `gates/integration.json` lists restart-mode checks as `skip` | B, D | Replay restart is at-least-once. Kafka `serve` state is in memory, so `processor_restart` and `state_recovery` are skipped. Durable state needs a Kafka changelog in the same transaction as output and offsets. |
| Safe cutover & rollback | `verification/cutover/`, `verification/integration/` | `"$PY" verification/cli.py cutover simulate` after a clean run has generated all gates; `scripts/run-integration.sh` for Kafka processing checks | `build-output/cutover-sim-*/{state.json,lease.json,audit.jsonl,topic.jsonl}` and isolated `shadow/` outputs; `build-output/integration/gates/integration.json` | D | Kafka transactions do not test cutover or make lease handover atomic. The lease uses one host and `fcntl.flock`, and handover can leave a no-producer window. Approval is a human action. |
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
- End-to-end exactly-once delivery, global ordering or atomic cutover. The
  integration checks cover Kafka-to-Kafka transactions on one Redpanda node.
- A real privacy review, legal determination or approval to release.
- Non-synthetic input data or a completed subsequent-job migration.
