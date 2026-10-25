# legacy-scala: legacy charging-session processor

The legacy system being migrated: a Scala 2.13 + Spark Structured Streaming
job that turns `charging.events.v1` telemetry into
`charging.session.summary.v1` records and `charging.errors.v1` errors,
plus a file-replay mode that runs without a cluster.

The replay mode is the approved legacy behaviour. It reproduces
`fixtures/expected/` exactly and is the `legacy` processor in
`processors.yaml`. The normative rules are `docs/build-contract.md`
sections 4-7. This README describes the implementation, not the contract.

All data in this repository is synthetic (`VEH-nnnn`, `SYN-*`).

## Layout

| Path | Role |
|---|---|
| `src/main/scala/.../EventValidator.scala` | Line validation in contract order (section 4.1), identifier echo |
| `src/main/scala/.../SessionState.scala` | Per-key accumulator, dedupe set and open/closed status |
| `src/main/scala/.../Windowing.scala` | Watermark, lateness, inactivity timeout, expiry, timer order |
| `src/main/scala/.../ReplayEngine.scala` | One input line at a time in arrival order, end-of-input flush |
| `src/main/scala/.../ReplayCheckpoint.scala` | Private checkpoint format, atomic write (temp file + rename) |
| `src/main/scala/.../ReplayMain.scala` | `replay` CLI, append-mode JSONL output, stderr logs |
| `src/main/scala/.../ChargingSessionProcessor.scala` | Spark Structured Streaming job (Kafka, or local files for the demo) |
| `bin/build` | Compile and write the runtime classpath files |
| `bin/charging-processor` | Replay entrypoint used by `processors.yaml` |
| `bin/check-fixtures` | Self-check of every scenario against `fixtures/expected/` |
| `bin/spark-local` | Optional Spark smoke demo, outside the gates |

The replay engine and the Spark job share `EventValidator`, `SessionState`
and `Windowing`. Replay never loads a Spark class: `spark-sql` is a
`Provided` dependency, so it is absent from the replay classpath.

## Build

Needs JDK 17 and sbt 1.10.11 (on `PATH` or in `~/.local/sbt`). sbt picks up
`.sbtopts` and `project/repositories`, which resolve all dependencies from
the Google mirror of Maven Central, because Maven Central can answer
HTTP 429 on build VMs.

```sh
legacy-scala/bin/build            # compile, write target/replay-classpath.txt
legacy-scala/bin/build --spark    # also write target/spark-classpath.txt
cd legacy-scala && sbt -batch test
```

Every `bin/` script works from any directory.

## Replay mode (the gated baseline)

```sh
legacy-scala/bin/charging-processor replay \
  --input fixtures/scenarios/normal_session/input.jsonl --output-dir /tmp/out
```

Outputs are appended to `<dir>/summaries.jsonl` and `<dir>/errors.jsonl`.
Exit codes: 0 success, 1 fatal error (for example a missing input file),
2 usage error.

Restart: process the first `n` lines, flush outputs, write a checkpoint and
exit without the end-of-input flush, then resume into the same directory.

```sh
legacy-scala/bin/charging-processor replay --input in.jsonl --output-dir out \
  --stop-after 9 --checkpoint out/ckpt.json
legacy-scala/bin/charging-processor replay --input in.jsonl --output-dir out \
  --resume-from out/ckpt.json
```

The checkpoint holds the input offset, `max_event_ts`, and every key's
dedupe set, open session and closed status. It is written only after the
outputs are flushed and closed. A crash between the two re-emits records on
resume, so delivery is at-least-once, not exactly-once. Downstream consumers
absorb this by upserting by `(vehicle_id, session_id)`.

Logs go to stderr and contain only counts, offsets, line numbers and error
types, never `vehicle_id`, `session_id` or `synthetic_location` values.

### Self-check

```sh
legacy-scala/bin/build
legacy-scala/bin/check-fixtures                # 13 scenarios
legacy-scala/bin/check-fixtures --every-split  # also restart at every line
```

It runs each scenario as `scenario.json` says (restart scenarios run
`--stop-after` then `--resume-from`), parses both sides and requires the
same records, with the same order within each `(vehicle_id, session_id)`
key. Order across keys is not checked. Numbers compare as parsed values,
so `15` and `15.0` are equal (contract section 6.3). `--every-split` repeats every
scenario as a restart at each possible split point and expects the same
output as a single run.

Matching the fixtures shows that replay reproduces the approved behaviour
on these synthetic scenarios. It is not evidence of production safety and
does not replace privacy review.

## Spark job

`ChargingSessionProcessor` is the legacy streaming job: it reads
`charging.events.v1` from Kafka, validates each record, applies
`withWatermark("event_time", "10 minutes")` and
`flatMapGroupsWithState` with `EventTimeTimeout`, and writes summaries to
`charging.session.summary.v1` keyed by `vehicle_id`, and errors to
`charging.errors.v1`. Kafka mode needs `spark-sql-kafka-0-10` on the
runtime classpath, which this build does not ship. It compiles in CI but is
not run by any tier here. Its header comment lists where it differs from
the replay contract.

### `bin/spark-local` (optional smoke demo, not gated)

Runs the same job on `master local[*]` with a file source instead of Kafka,
`Trigger.AvailableNow` and one staged file per micro-batch, then writes
`summaries.jsonl` and `errors.jsonl` to the output directory (which it
overwrites).

```sh
legacy-scala/bin/build --spark
legacy-scala/bin/spark-local --input fixtures/scenarios/concurrent_sessions/input.jsonl \
  --output-dir /tmp/spark-out [--batch-lines 10]
```

Staged file modification times are fixed values, so no output depends on
the wall clock. Spark is on the classpath only for this mode
(`target/spark-classpath.txt`).

It is not the gated baseline, and its output is expected to differ from
`fixtures/expected/`:

- The Spark watermark advances once per micro-batch, from the previous
  batch's maximum event time, while the contract advances it per event. So
  lateness and timer firing depend on where batches are cut. Spark also
  drops most rows behind the watermark before the state function sees them,
  so they never become `LATE_EVENT_DROPPED` errors.
- A stream has no end of input, so sessions still open when the input runs
  out are never emitted. Most scenarios therefore produce fewer summaries.
- Order across keys within a batch is not arrival order.
- Each run starts a JVM and a local Spark session, several seconds per
  scenario, against well under a second for replay.

On the 13 scenarios it exits 0 every time. Summaries are mostly a strict
subset of the expected ones (for example 3 of 4 in `concurrent_sessions`,
0 of 1 in `normal_session`). In `state_recovery`, an event that replay
rejects as `SESSION_ALREADY_CLOSED` is merged into the session, because
the batch watermark has not closed it yet. Validation errors match
exactly, while lateness and closed-session errors mostly do not appear.

## Known legacy quirks

Behaviour a migration has to preserve or consciously change. All of it is
pinned by the fixtures or by the contract.

- **Energy is a MAX, not a sum.** `energy_delivered_kwh` is cumulative, so
  `total_energy_kwh` is the largest reading accepted for the session. A
  meter that resets mid-session reports the pre-reset maximum.
- **Dedupe runs before the lateness check.** A late event's `event_id` is
  still recorded, so a later on-time copy of it is dropped silently. The
  first copy wins even if a later copy carries other values.
- **Dedupe is per session key.** The same `event_id` on another
  `(vehicle_id, session_id)` is a different event.
- **COMPLETE does not close a session at once.** A session closes when the
  watermark reaches its expiry (`last_ts` for COMPLETE), that is, once an
  event at least 10 minutes later arrives on any key. Events arriving
  before then are still merged in, even after the COMPLETE reading.
- **Events for a closed session still move the watermark.**
  `SESSION_ALREADY_CLOSED` events advance `max_event_ts` and can fire other
  sessions' timers. Late events do not.
- **Closed keys are kept forever.** Their dedupe set and closed status live
  for the whole run (and in the checkpoint), with no TTL.
- **Timer order is `(expiry, vehicle_id, session_id)` by code point.**
  Strings compare by Unicode code point, not UTF-16 unit, matching Python.
- **Numbers are doubles.** JSON numbers are parsed as binary64.
  `sequence_number` must be integral (3 and 3.0 both mean 3) and at most
  2^53 - 1. Outputs pass readings through unchanged but may print `60`
  for `60.0`.
- **Line numbers count every line**, including empty and malformed ones.
  A final trailing newline adds no line.
- **Ingestion time is validated and then ignored.** `synthetic_location`
  is validated and then dropped: it never reaches outputs, logs or the
  checkpoint.
- **The checkpoint is not bound to its input file.** Resume checks only
  that the stored offset is within the input, so resuming against a
  different file is not detected.
- **The whole input is read into memory**, which suits fixture-sized
  replays only.

## Guarantee boundaries

- Replay is deterministic for a given input and split point, and restart
  output equals single-run output. Delivery across a crash is
  at-least-once.
- Only order within a session key is meaningful. Nothing here provides a
  global order across keys, exactly-once delivery or an atomic cutover.
- Passing `bin/check-fixtures` or `sbt test` is not a production-safety or
  privacy-compliance claim.
