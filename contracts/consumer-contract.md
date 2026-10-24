# Consumer contract: `charging.session.summary.v1`

This contract describes the input accepted by the unchanged
`consumer-warranty` service. The JSON Schema is
`contracts/charging.session.summary.v1.schema.json`.

## Required fields

| Field | Type and rule | Meaning |
|---|---|---|
| `session_id` | string, 1 to 64 characters | Session key component. |
| `vehicle_id` | string matching `^VEH-[0-9]{4}$` | Vehicle key component and Kafka message key. |
| `start_ts` | UTC timestamp `YYYY-MM-DDTHH:MM:SS.sssZ` | Earliest accepted event time for the session. |
| `end_ts` | UTC timestamp `YYYY-MM-DDTHH:MM:SS.sssZ` | Latest accepted event time for the session. It is not earlier than `start_ts`. |
| `total_energy_kwh` | non-negative number | Maximum cumulative `energy_delivered_kwh` observed for the session. It is not a sum of readings or deltas. |
| `peak_power_kw` | non-negative number | Maximum `charging_power_kw` observed for the session. |
| `event_count` | integer, at least 1 | Count of accepted events for the session. |
| `completion_reason` | `COMPLETE` or `INACTIVITY_TIMEOUT` | Reason the processor emitted the session. |
| `schema_version` | string, exactly `"1.0"` | Summary schema version. |

All fields are required. The schema forbids additional properties. A new
field is a schema change and is not silently accepted by this consumer.

## Key, ordering, and delivery

The Kafka message key is `vehicle_id`. Ordering is meaningful per vehicle
key only. No global ordering across vehicles is promised or checked.

Delivery is at least once. Duplicate delivery and replay must be idempotent
for the consumer's observable state. The consumer upserts by
`(vehicle_id, session_id)`, so the last record for a key determines its
state. Re-emission of a key must be identical. A conflicting re-emission
can change the state and is not repaired by upsert.

## Versioning

Version `1.0` is the current contract. An additive change requires a new
minor version and consumer sign-off. A field removal or semantic change
requires a new major version and topic. The consumer rejects records that
do not validate against the schema it supports. A new version therefore
requires coordinated schema and consumer work.

## Explicit limits

This contract does not guarantee exactly-once delivery, global order across
vehicles, emission timing or latency, or an atomic cutover between
processors. A passing contract gate does not establish production safety
and does not replace privacy review. Kafka integration is outside the fast
contract gate.

## Contract gate

The gate compares runtime outputs with the selected baseline. Its checks
are:

| Check | What it checks |
|---|---|
| `contract.json_schema` | Candidate summary and error lines parse and validate against their schemas. |
| `contract.required_fields` | Fields present in a baseline record remain present in the corresponding candidate record. |
| `contract.field_semantics` | Intrinsic timestamp and completion rules, baseline field equality, numeric tolerance, and missing baseline keys. |
| `contract.per_key_ordering` | First-emission session order matches within each vehicle key. It makes no global ordering claim. |
| `contract.replay_idempotence` | Candidate consumer state matches after one and two deliveries, and repeated keys have identical records. |
| `contract.no_extra_outputs` | Candidate introduces no keys or fields outside the baseline record or summary schema. |
| `contract.consumer_state` | The unchanged consumer derives identical application state from baseline and candidate summaries. With expected fixtures it also records whether the baseline state matches the fixture. |

The runtime checks exercise synthetic fixture scenarios only. They do not
demonstrate behavior on production traffic, establish production safety, or
remove privacy-review obligations.
