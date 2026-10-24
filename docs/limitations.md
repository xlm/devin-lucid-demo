# Limitations

All fixtures are synthetic. The guarantees below describe only this
repository's local demo and its selected replay inputs.

## Component boundaries

- **Fixtures and legacy replay:** `legacy-scala/bin/check-fixtures` checks
  the legacy replay against its own expected fixtures. It is a self-check,
  not a verification gate or a production claim.
- **Spark smoke mode:** `legacy-scala/bin/spark-local` is optional and
  outside the gates. Its micro-batch and watermark behavior differs from
  replay mode, including end-of-input handling.
- **Rust replay:** The default keyed store is in-process. Restart recovery
  is at-least-once because output and checkpoint updates are not one
  transaction. RocksDB is not included.
- **Consumer and contract checks:** Upsert by `(vehicle_id, session_id)`
  absorbs an identical replay. It cannot repair a missing record or a
  conflicting re-emission. Ordering guarantees are per vehicle key.
- **Behavioral and state gates:** Results cover committed synthetic
  scenarios. Numeric tolerance is not a domain accuracy guarantee, and
  strict binary64 equality is not decimal arithmetic.
- **Privacy checks and evidence:** WP5 source describes declared-manifest,
  runtime-observed and heuristic source-scan checks. Manifest checks are
  static-only. Runtime checks cover selected fixture outputs. The evidence
  hash does not authenticate the author. Neither checks nor evidence
  replace human privacy review.
- **Cutover simulator:** State and lease coordination use one host and a
  local `fcntl.flock` file. Lease transfer and processor execution are not
  one atomic operation. A window with no active producer, duplicates and
  gaps remain possible. The approval prompt is a local demo guard, not a
  real authorization boundary. Agents must not approve.
- **Integration tier:** `scripts/run-integration.sh` exits with status 3
  and reports that the tier is not implemented at this revision. The
  single-node Redpanda Compose file is a demo configuration, not a live
  integration result.
- **Fast runner evidence handling:** `scripts/run-fast.sh` runs evidence
  packaging even when verification fails, then returns a non-zero status if
  either verification or evidence fails.

## Risks

- **Spark/sbt build weight:** Use file-replay mode for the fast workflow.
- **Live Kafka exactly-once fragility:** Keep transaction exercises in the
  optional integration tier only. That tier is not implemented yet.
- **Atomic cutover not achievable locally:** Use the single-active-producer
  lease for the demo and document the non-atomic handover gap.
- **Lineage overclaim:** Compare declared manifests with runtime inspection,
  and label declaration checks as static-only.
- **Time-constrained fallback:** Ship WP0-WP5 plus WP7 and WP8 before WP6
  and WP9.
