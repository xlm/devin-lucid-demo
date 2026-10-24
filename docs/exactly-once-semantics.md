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

The optional integration tier can exercise keyed Kafka records, producer
transactions, `read_committed` consumers, and offsets committed in the same
transaction as produced records. Records are keyed by `vehicle_id`, so Kafka
partition order is per vehicle key.

Kafka transactions provide exactly-once processing only within Kafka-to-Kafka
transactions on one cluster, when producers and consumers are configured and
used correctly. They do not make the entire pipeline exactly-once. External
side effects, processor state, replay, and consumer databases need their own
coordination or idempotency. A single-node Redpanda setup is a demo
simplification, not a production availability or failure model.

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
