# Rust charging-session processor

This crate implements the contract's charging-session processor as a library
and a small `charging-processor` binary. `model` parses and validates input,
`state` provides the keyed `StateStore` seam and checkpoint serialization,
`processors::charging_session` applies event-time rules, and `replay` reads
JSONL and writes summary and error JSONL. `main` provides the replay and
optional serve commands. `kafka` is compiled only with the `kafka` feature.
That optional feature enables Tokio and rdkafka; default builds do not include
either dependency.

Replay uses `InMemoryStateStore`, a `BTreeMap`-backed implementation of
`StateStore`. The store returns owned key state and iterates in ascending
`(vehicle_id, session_id)` order. Checkpoints serialize its deterministic
snapshot together with the global input offset and watermark. A RocksDB-backed
implementation could be added behind the same trait in the future, but this
crate does not add RocksDB.

The fast replay tier has no broker dependency:

```sh
cargo build --release
cargo test
cargo run -- replay --input ../fixtures/scenarios/normal_session/input.jsonl \
  --output-dir /tmp/charging-output
```

`replay` requires `--input` and `--output-dir`. Outputs are appended to
`summaries.jsonl` and `errors.jsonl`. To checkpoint after the next N lines,
pass both `--stop-after N` and `--checkpoint FILE`. Restore with
`--resume-from FILE`; resume can be combined with another stop and checkpoint.
`--help` prints command help. Exit code 0 means success, 1 means a fatal I/O or
checkpoint error, and 2 means invalid command usage.

Replay always uses `Rules::CONTRACT` (600000 ms allowed lateness and 1800000
ms inactivity timeout). Environment overrides only affect `serve`.

| Serve environment variable | Default |
| --- | --- |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` |
| `CHARGING_INPUT_TOPIC` | `charging.events.v1` |
| `CHARGING_SUMMARY_TOPIC` | `charging.session.summary.v1` |
| `CHARGING_ERROR_TOPIC` | `charging.errors.v1` |
| `CHARGING_CONSUMER_GROUP` | `charging-processor` |
| `CHARGING_TRANSACTIONAL_ID` | `charging-processor-1` |
| `CHARGING_ALLOWED_LATENESS_MS` | `600000` |
| `CHARGING_INACTIVITY_TIMEOUT_MS` | `1800000` |

## Guarantee boundaries

Replay restart is at-least-once. A crash after output files are flushed but
before the checkpoint rename can cause records to be emitted again on resume.
The downstream consumer's upsert by `(vehicle_id, session_id)` absorbs replayed
summaries. Output and checkpoint are separate files and are not committed as
one operation.

Kafka mode uses a Kafka transaction for input offsets and output records on one
broker cluster. Its transactional guarantee applies only to Kafka-to-Kafka
read-process-write when downstream consumers use `read_committed`. It is not
end-to-end. Processor state is in memory and a restart loses dedupe sets, open
sessions and the watermark. One instance is assumed, the watermark is global
across the partitions it reads, and streaming has no end-of-input flush.

Closed-key and dedupe state is retained for the lifetime of a processor run,
with no TTL, as a demo simplification. Logs go to stderr and omit
`vehicle_id`, `session_id` and `synthetic_location` values. Error logs may
include line numbers, error types and field names. Checkpoints retain key ids
as processor state, not as logs, and should be treated as restricted data.
Synthetic fixtures and passing tests do not establish production safety or
remove the need for privacy review.

## Contract rule mapping

| Contract rule | Rust implementation |
| --- | --- |
| s4.1 validation | `model::validate_line` |
| s4.1 echo and s6.2 identifier echo | `model::echo_ids`, `processors::charging_session::error_record` |
| s5 step 2-3 dedupe | `ChargingSessionProcessor::is_duplicate`, `ChargingSessionProcessor::record_event_id` |
| s5 step 4 late routing | `ChargingSessionProcessor::is_late` |
| s5 step 5 closed routing | `ChargingSessionProcessor::is_closed` |
| s5 step 5 accumulation | `ChargingSessionProcessor::accumulate`, `ChargingSessionProcessor::open_session` |
| s5 step 6 watermark | `ChargingSessionProcessor::advance_watermark`, `ChargingSessionProcessor::watermark` |
| s5 step 7 timer firing | `ChargingSessionProcessor::session_expiry`, `ChargingSessionProcessor::fire_timers` |
| End-of-input flush | `ChargingSessionProcessor::finish` |
| s6.1 summary | `ChargingSessionProcessor::build_summary` |
| s7 checkpoint and restore | `state::write_checkpoint_atomic`, `state::read_checkpoint`, `state::StateStore::snapshot`, `state::StateStore::restore` |
| s6.4 logging | `replay::log_error`, `replay::log_stats` |

## Known deviations from `reference_model.py`

Rust follows the contract's ASCII patterns `[0-9]` and strict anchored
patterns. Python `\d` and `$` in the reference also accept Unicode digits and
a trailing newline, which fixtures do not exercise. When MAX compares an
integer literal with a float, Rust compares both as f64 (exact for integers up
to 2^53); two integers or two floats compare exactly.
