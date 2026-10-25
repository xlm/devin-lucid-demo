# Delivery and cutover guarantees

## Fast verification tier

The fast tier runs deterministic replay against fixed synthetic fixtures.
Comparisons may assert ordering within a vehicle key. They do not assert a
global order across keys. Re-running replay can reproduce the same records,
but that is not a delivery guarantee for a live pipeline.

The cutover simulator stores state and a topic-like JSONL file on one host.
Its lease is one coordination file protected by `fcntl.flock`. This is a
local demonstration mechanism, not a distributed lease, a broker fence, or
an exactly-once protocol.

## Optional Kafka integration tier

`scripts/run-integration.sh` builds the Kafka-feature Rust service and runs
the 11 single-mode fixtures on fresh topics. Each input topic has one
partition. The processor's watermark is global, so interleaving records
across input partitions could change which events are late. Summary and
error topics have three partitions. Summary keys and per-vehicle partition
and order behavior are checked.

After fixture input, the harness produces a schema-valid
`VEH-9999` watermark sentinel with an event timestamp 24 hours later than
the latest fixture event. That advances the watermark so inactivity timers
can emit expected summaries. Sentinel records are excluded from saved
candidate output. The sentinel vehicle ID is reserved and checked against
all fixtures.

The `processor_restart` and `state_recovery` scenarios are reported as
skipped. `serve` keeps dedupe, watermark and open-session state in memory, so
a restart loses state. A future remedy is durable state in a Kafka changelog
written in the same transaction as output records and input offsets.

The checks cover Kafka-to-Kafka transactions and committed offsets on one
Redpanda node, plus a short restart check for reprocessing. They do not
establish end-to-end exactly-once delivery. External side effects, durable
processor state, replay and consumer databases need their own coordination
or idempotency. Single-node Redpanda is a demo simplification, not a
production availability or failure model.

## Lease handover and rollback

The simulator releases the current holder, then acquires the lease for the
next holder with an incremented fencing token. Each lease update is serialized
under the local file lock and atomically replaced as a file. The two updates,
processor execution, topic append, and state transition are not one atomic
operation. A crash or a slow process can leave a window where neither side
produces. Handover can also leave duplicates or gaps. A stale producer append
is rejected when the local holder or fencing token no longer matches, but
this check does not fence a process at a broker.

The downstream warranty consumer uses an idempotent upsert keyed by
`(vehicle_id, session_id)`. Replaying or handing over a duplicate summary
therefore replaces the prior value instead of adding a second session. That
mitigates duplicates, but cannot recover a missing summary. Ordering claims
remain limited to an individual vehicle key.

`cutover approve` has a local demo guard that requires a TTY and an exact
confirmation phrase. A process with a PTY can bypass this demonstration
guard. It is not a real authorization control, and agents must not approve
cutover. Real approval belongs out of band, for example in a protected
GitHub environment or a change ticket.

## Production requirements

A production design needs broker-side producer fencing, typically with a
stable `transactional.id` and broker-managed producer epochs. It also needs
consumer offset handover coordinated with transactional writes, plus a real
coordination service for ownership and recovery. The complete application
boundary must be analyzed separately. Passing fast-tier checks or this local
simulation does not establish production safety, global ordering, atomic
cutover, end-to-end exactly-once delivery, or privacy approval.
