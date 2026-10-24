# Warranty consumer

This is the unchanged downstream service. No work package after WP4 edits
this consumer.

## Run

```sh
source scripts/_env.sh
$PY -m warranty_consumer \
  --input fixtures/expected/downstream_contract/summaries.jsonl \
  --replays 2 \
  --state-out build-output/warranty/state.json
```

The state file is schema-validated before it is written. A side file named
`state.json.rejects.json` records accepted and rejected delivery counts,
replay count, and value-free rejection reasons. Invalid input lines are
counted and never applied. The process exits successfully when individual
records are rejected. A missing input or invalid generated state is fatal.

## Consumer behavior

For `charging.session.summary.v1`, `total_energy_kwh` is the cumulative
maximum `energy_delivered_kwh` observed for a session. It is not a sum of
readings or deltas. The consumer sums that summary value once per distinct
session. Its state follows docs/build-contract.md section 8: energy is summed
in ascending `session_id` order and rounded to three decimals, sessions are
counted distinctly, high power is `peak_power_kw >= 150.0`, and inactivity
timeouts are counted from `completion_reason`.

The Kafka message key is `vehicle_id`. Ordering is per vehicle only, with no
cross-vehicle order assumed. Delivery is at least once. The consumer upserts
by `(vehicle_id, session_id)`, so an identical replay replaces the same
value and is absorbed. A later record wins for the same key.

These boundaries matter: upsert cannot repair an incorrect value or a
conflicting re-emission for the same key. This is not exactly-once
processing. Kafka mode is out of scope and is not built here. Passing local
checks does not establish production safety or replace privacy review.
