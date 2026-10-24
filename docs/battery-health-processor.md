# Battery health processor

Status: not yet migrated, see Workflow F.

The legacy implementation is a Scala replay processor with a Spark Structured
Streaming job. Replay is the reference behavior and runs without Spark or a
broker. The executable specification is
`fixtures/battery/tools/reference_model.py`, and fixtures are generated with
`fixtures/battery/tools/generate_fixtures.py`.

All example data in this processor and its fixtures is synthetic.

## Topics and keys

| Topic | Purpose | Message key |
|---|---|---|
| `battery.telemetry.v1` | Battery pack telemetry input | `vehicle_id` |
| `battery.health.daily.v1` | One daily pack health summary | `vehicle_id` |
| `battery.errors.v1` | Validation and late-event errors | No key |

The summary identity is `(vehicle_id, pack_id, day)`. A vehicle may have
multiple packs, and each pack may produce a summary for each UTC day.

## Input fields

Schema: `contracts/battery.telemetry.v1.schema.json`. One JSON object is read
per input line.

| Field | Rule |
|---|---|
| `schema_version` | String matching `^[0-9]+\.[0-9]+$`, with major version `1` |
| `event_id` | String of 1-64 code points |
| `vehicle_id` | Synthetic string matching `^VEH-[0-9]{4}$` |
| `pack_id` | String of 1-64 code points |
| `event_timestamp` | Real UTC timestamp in `YYYY-MM-DDTHH:MM:SS.sssZ` format |
| `ingestion_timestamp` | Same timestamp format, validated and otherwise unused |
| `sequence_number` | Finite integral number from 0 through 9007199254740991 |
| `cell_voltage_min_v` | Finite number from 0 through 5 |
| `cell_voltage_max_v` | Finite number from 0 through 5 and not less than the minimum |
| `pack_temp_c` | Finite number from -50 through 100 |
| `state_of_health_pct` | Finite number from 0 through 100 |
| `synthetic_location` | Optional nullable string matching `^SYN-[A-Z0-9-]+$`, restricted |

Booleans and numeric strings are not numbers. Unknown additive fields are
ignored. Timestamps must be millisecond-precision UTC strings on real
calendar dates. Year `0000` is not accepted.

## Validation and errors

Validation checks the first failure in this order:

1. Parse JSON and require an object. Invalid JSON, non-standard numeric tokens,
   empty input, and non-object JSON produce `MALFORMED_JSON` with a null field.
2. Validate `schema_version`. Missing or null produces `MISSING_FIELD`,
   malformed values produce `INVALID_FIELD`, and a major version other than
   `1` produces `UNSUPPORTED_SCHEMA_VERSION`.
3. Validate `event_id`, `vehicle_id`, `pack_id`, `event_timestamp`,
   `ingestion_timestamp`, `sequence_number`, `cell_voltage_min_v`,
   `cell_voltage_max_v`, `pack_temp_c`, and `state_of_health_pct` in that
   order. Missing or null values produce `MISSING_FIELD`. Invalid values
   produce `INVALID_FIELD`.
4. Validate `synthetic_location` when present and non-null. A bad value
   produces `INVALID_FIELD`.

The error types are `MALFORMED_JSON`, `MISSING_FIELD`, `INVALID_FIELD`,
`UNSUPPORTED_SCHEMA_VERSION`, and `DROPPED_LATE`. Each
`battery.errors.v1` record has `error_type`, `field`, `input_line`,
`event_id`, `vehicle_id`, `pack_id`, and `schema_version`. An identifier is
echoed only when it independently passes its own validation. Unparseable or
non-object input has null identifiers. Errors never carry the raw payload,
measurements, or `synthetic_location`.

## Daily processing

The per-day key is `(vehicle_id, pack_id, UTC day of event_timestamp)`.
Dedupe is scoped to that key and uses `event_id`. The first occurrence wins.
Validation runs before dedupe. A duplicate is silently dropped before the
lateness check, and does not advance the watermark. A new valid event ID is
remembered before lateness is checked. A late event therefore remains
deduplicated if it appears again.

The watermark is `max accepted event_timestamp - 10 minutes`. There is no
watermark before the first accepted event. An event strictly earlier than the
current watermark produces `DROPPED_LATE`, while an event exactly at the watermark
is accepted. The watermark advances only for events accepted into a daily
aggregate. A day closes when `watermark >= UTC day end`. At end of replay
input, all remaining days are emitted.

Each summary contains `vehicle_id`, `pack_id`, `day`,
`min_cell_voltage_v`, `max_cell_voltage_v`, `max_cell_imbalance_v`,
`max_pack_temp_c`, `latest_state_of_health_pct`, `event_count`, and
`schema_version`. Minimum and maximum voltage and maximum temperature are
aggregated across accepted, de-duplicated events. Event count counts those
same events. The latest SOH is selected by the greatest pair
`(event_timestamp, sequence_number)`. An exact tie keeps the first accepted
reading.

### Per-event imbalance trap

`max_cell_imbalance_v` is the maximum of each event's
`cell_voltage_max_v - cell_voltage_min_v`. It is not the difference between
the day's maximum and minimum cell voltage. In the `imbalance_trap` fixture,
for `PK-0109-A`, the per-event result is
`0.11999999999999966`, while `max_cell_voltage_v - min_cell_voltage_v` is
`3.8 - 3.5 = 0.3`. The extrema come from different readings, so subtracting
them does not describe the largest observed per-reading spread.

## Restricted location handling

`synthetic_location` is validated on input, then discarded. It is not held
in the daily aggregate or checkpoint and never appears in summaries, errors,
or processor diagnostics. The fixtures contain only synthetic data.

## Replay CLI and restart behavior

Build with `legacy-scala/bin/build`, then run:

```sh
legacy-scala/bin/battery-health-processor replay \
  --input <file> --output-dir <dir>
```

The command writes or appends JSONL records to
`summaries.jsonl` and `errors.jsonl`. Restart options follow the charging
replay interface:

```sh
legacy-scala/bin/battery-health-processor replay \
  --input <file> --output-dir <dir> \
  --stop-after <n> --checkpoint <file>

legacy-scala/bin/battery-health-processor replay \
  --input <file> --output-dir <dir> --resume-from <file>
```

The checkpoint stores the input offset, watermark maximum, per-day dedupe
sets, closed-day status, and open aggregates. It never stores
`synthetic_location`. Outputs are flushed before a checkpoint is written. A
crash between those actions can cause records to be emitted again on resume,
so delivery is at-least-once. This is not exactly-once.

The Spark job is a legacy Structured Streaming implementation. It uses
event-time state and a ten-minute watermark, but Spark micro-batch behavior
does not provide the per-event replay ordering and exact closure boundaries.
Replay fixtures describe the reference behavior, not a production guarantee.
