# Build contract (FROZEN)

This file is the shared contract for every work package (WP) that builds
this repository. It was written by the build orchestrator in WP0 and is
frozen. Sub-agents must not edit it or any other frozen path (section 11).
If a WP needs a change, it stops and raises a contract conflict to the
orchestrator (section 12) instead of working around the contract.

Everything here is normative. Where this file and any other document
disagree, this file wins until the orchestrator changes it.

## 1. Non-negotiable constraints

These are carried verbatim into every sub-agent prompt.

1. Synthetic data only, no real location/PII.
2. Do not let automated checks or any agent edit privacy policies/baselines/acceptance tests to force a pass.
3. Do not claim exactly-once, global ordering, atomic cutover, or that passing checks equals production safety or removes privacy-review obligations; state guarantee boundaries explicitly.
4. Keep the fast tier runnable with no infrastructure; put Kafka/Redpanda exactly-once behind the optional integration tier.
5. Provide a single verification CLI (verification/cli.py) exposing verify, evidence, and cutover.
6. If time-constrained, deliver a reliable single-processor demo (WP0-WP5, WP7, WP8) before WP6/WP9 - a reliable single-processor demo beats a fragile full platform.
7. Agents are engineering coordinators only. No agent approves cutover, production release or privacy compliance. The `ReadyForReview -> ApprovedForCutover` transition is a human action.

## 2. Toolchain

| Tool | Version | Notes |
|---|---|---|
| Python | 3.10+ | `verification/`, `consumer-warranty/`, fixtures tooling. Deps in `requirements.txt` only (jsonschema, PyYAML, pytest). Scripts create `.venv/` with the system interpreter. |
| Rust | stable (1.80+) | `rust-processor/`. Default features build with no Kafka/C dependencies. |
| Scala | 2.13.16, sbt 1.10.11, JDK 17 | `legacy-scala/`. Spark 3.5.x is a `Provided` dependency used only by the streaming job, never by replay mode. |

Maven Central can rate-limit (HTTP 429) some build VMs. `legacy-scala/`
therefore ships a repo-local resolver config that uses the Google mirror
of Maven Central only, picked up automatically by sbt:

- `legacy-scala/.sbtopts`: `-Dsbt.override.build.repos=true` and `-Dsbt.repository.config=project/repositories`
- `legacy-scala/project/repositories`: `[repositories]`, `local`, `google-maven: https://maven-central.storage-download.googleapis.com/maven2/`

## 3. Execution tiers

- **Fast tier (default, no infrastructure):** `fixtures/scenarios/*` ->
  each processor in replay mode -> canonical JSONL -> verification gates
  -> evidence. No Kafka, no Docker, no network beyond dependency download.
  Entry point: `scripts/run-fast.sh`.
- **Integration tier (optional):** Docker Compose Redpanda (a Kafka API
  compatible broker, a documented demo simplification) exercising real
  topics, keys, offsets and transactions. Entry point:
  `scripts/run-integration.sh`. Nothing in the fast tier may depend on it.

## 4. Input: `charging.events.v1`

Schema: `contracts/charging.events.v1.schema.json`. One JSON object per
line. Fields:

| Field | Type | Rule |
|---|---|---|
| `event_id` | string | 1-64 chars |
| `vehicle_id` | string | `^VEH-[0-9]{4}$` (synthetic; anything else, e.g. a VIN, is invalid) |
| `session_id` | string | 1-64 chars |
| `event_timestamp` | string | `YYYY-MM-DDTHH:MM:SS.sssZ`, exactly millisecond precision, UTC, a real calendar date |
| `ingestion_timestamp` | string | same format; validated, otherwise unused |
| `sequence_number` | number | finite, integral value (3 and 3.0 both mean 3), 0..9007199254740991 |
| `charging_state` | string | `IDLE`, `CHARGING`, `COMPLETE`, `FAULT` |
| `battery_soc` | number | finite, 0..100 inclusive |
| `charging_power_kw` | number | finite, >= 0 |
| `energy_delivered_kwh` | number | finite, >= 0, **CUMULATIVE** since session start |
| `schema_version` | string | `^[0-9]+\.[0-9]+$`; major must be `1` |
| `synthetic_location` | string or null, optional | `^SYN-[A-Z0-9-]+$`; classification `sensitive_location`; restricted |

Booleans are not numbers. A JSON string containing digits is not a number.
Unknown additional fields are ignored (additive minor versions).

### 4.1 Validation order

A line is validated in this exact order and the first failure wins:

1. Parse as JSON. Not valid JSON (including empty lines and the
   non-standard tokens `NaN`, `Infinity`, `-Infinity`), or valid JSON that
   is not an object -> `MALFORMED_JSON`, `field: null`.
2. `schema_version`: missing or null -> `MISSING_FIELD`; not a string or
   not matching `^[0-9]+\.[0-9]+$` -> `INVALID_FIELD`; major != 1 ->
   `UNSUPPORTED_SCHEMA_VERSION`. `field: "schema_version"` in all cases.
3. Then, for each field in this order: `event_id`, `vehicle_id`,
   `session_id`, `event_timestamp`, `ingestion_timestamp`,
   `sequence_number`, `charging_state`, `battery_soc`,
   `charging_power_kw`, `energy_delivered_kwh`: missing or null ->
   `MISSING_FIELD`; present but violating its rule -> `INVALID_FIELD`.
   `field` is the field name.
4. `synthetic_location`: if present and not null and not matching its
   pattern -> `INVALID_FIELD`, `field: "synthetic_location"`.

Input lines are the file split on `\n`; a final trailing newline does not
create an extra line. Line numbers are 1-based and count every line,
including malformed and empty ones.

## 5. Processing semantics

Constants: `ALLOWED_LATENESS = 10 min` (600000 ms),
`INACTIVITY_TIMEOUT = 30 min` (1800000 ms). All times are event time in
integer epoch milliseconds.

Session key: `(vehicle_id, session_id)`.

Per-key state: dedupe set of `event_id`, `start_ts` (min event time),
`last_ts` (max event time), `max_sequence_number`, `max_energy`,
`peak_power`, `event_count`, `complete` flag, and an open/closed status.
Closed keys keep their dedupe set and closed status for the lifetime of
the run (a demo simplification: real deployments need state TTL).

Global state: `max_event_ts` over all events that reached step 6 below
(none initially). `watermark = max_event_ts - ALLOWED_LATENESS`; before
the first such event there is no watermark and nothing is late.

Each input line, in file (arrival) order:

1. Validate (section 4.1). Invalid -> one error record; stop.
2. If `event_id` is already in the key's dedupe set -> drop silently (no
   output of any kind, no watermark change); stop. Dedupe is scoped to the
   session key: the same `event_id` on another key is a different event.
   The first occurrence wins even if a later copy carries other values.
3. Add `event_id` to the key's dedupe set.
4. If a watermark exists and `event_timestamp < watermark` ->
   `LATE_EVENT_DROPPED` error; stop (no watermark change).
5. If the key is closed -> `SESSION_ALREADY_CLOSED` error. Otherwise
   accept the event into the key's open session (creating it if needed):
   `start_ts = min`, `last_ts = max`, `max_sequence_number = max`,
   `max_energy = max(energy_delivered_kwh)`,
   `peak_power = max(charging_power_kw)`, `event_count += 1`,
   `complete |= (charging_state == COMPLETE)`.
6. `max_event_ts = max(max_event_ts, event_timestamp)`.
7. Fire timers: every open session whose
   `expiry <= watermark` is emitted and becomes closed, where
   `expiry = last_ts` if `complete`, else `last_ts + INACTIVITY_TIMEOUT`.
   Emit in ascending `(expiry, vehicle_id, session_id)` order (plain
   string comparison).

At end of input the watermark becomes +infinity: every remaining open
session is emitted in ascending `(expiry, vehicle_id, session_id)` order.

So a session completes on `COMPLETE` or on inactivity timeout, whichever
expires first; late events within the watermark update the summary;
events behind the watermark go to the error path.

`total_energy_kwh = MAX(energy_delivered_kwh)` over the accepted events of
the session. It is **not** a sum of readings and **not** a sum of deltas.
`energy_delivered_kwh` is cumulative, and a sum is wrong as soon as
readings are duplicated, late or out of order.

`peak_power_kw = MAX(charging_power_kw)`. Both values are passed through
from an input reading unchanged; no floating-point arithmetic is applied.

## 6. Outputs

### 6.1 `charging.session.summary.v1`

Schema: `contracts/charging.session.summary.v1.schema.json`
(`additionalProperties: false`). One record per emitted session:

| Field | Value |
|---|---|
| `session_id` | key |
| `vehicle_id` | key; **Kafka message key** (partition ordering per vehicle) |
| `start_ts` | `start_ts`, formatted `YYYY-MM-DDTHH:MM:SS.sssZ` |
| `end_ts` | `last_ts`, same format |
| `total_energy_kwh` | `max_energy` |
| `peak_power_kw` | `peak_power` |
| `event_count` | accepted events |
| `completion_reason` | `COMPLETE` if `complete`, else `INACTIVITY_TIMEOUT` |
| `schema_version` | `"1.0"` |

### 6.2 `charging.errors.v1`

Schema: `contracts/charging.errors.v1.schema.json`. Fields: `error_type`,
`field`, `input_line`, `event_id`, `vehicle_id`, `session_id`,
`schema_version` (`"1.0"`). Error records never contain the raw payload,
measurements or `synthetic_location`.

Identifier echo: for validation errors each of `event_id`, `vehicle_id`,
`session_id` is copied from the input object if that field on its own is
valid (string, 1-64 chars; `vehicle_id` must also match its pattern), else
`null`, independently of which field failed. Non-object or unparsable
input echoes all three as `null`. For `LATE_EVENT_DROPPED` and
`SESSION_ALREADY_CLOSED` the three ids come from the event.

### 6.3 Canonical JSONL

The canonical form of a record is JSON with keys sorted by code point,
separators `,` and `:` with no whitespace, UTF-8, one record per line,
`\n` terminated. Comparison is on parsed values, never on bytes: a
processor may print `15` or `15.0` and `1.55e1` or `15.5`. Tools that
hash or store outputs re-serialise them canonically first (Python
`json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`).

Output order within a file is emission order. Only order within a session
key is meaningful. No tool may assert a global total order across keys.

### 6.4 Logs

Processors write diagnostic logs to stderr only. Logs must never contain
any `vehicle_id`, `session_id` or `synthetic_location` value. Counts,
`event_id`s, line numbers and error types are allowed. The runner stores
stderr as `processor.log`, and the privacy gate scans it.

## 7. Replay CLI (both processors)

Registry: `processors.yaml` (`legacy`, `candidate`). Each processor is
invoked as `<command> replay --input <file> --output-dir <dir> [options]`:

| Option | Meaning |
|---|---|
| `--input <file>` | scenario `input.jsonl` |
| `--output-dir <dir>` | appends to `<dir>/summaries.jsonl` and `<dir>/errors.jsonl` (created if missing) |
| `--stop-after <n>` with `--checkpoint <file>` | process the next `n` input lines, flush outputs, then write a checkpoint atomically (temp file + rename) and exit 0 without the end-of-input flush |
| `--resume-from <file>` | restore all state (input offset, `max_event_ts`, every key's dedupe set, open sessions and closed status) and continue from the stored offset |

Exit codes: 0 success, 1 fatal error, 2 usage error. Checkpoint format is
private to each processor. A run that crashes between flushing outputs
and writing the checkpoint re-emits on resume (at-least-once); the
consumer's idempotent upsert absorbs this. This is not exactly-once.

## 8. Downstream consumer state (`consumer-warranty`)

The consumer reads summary records in order and upserts them by
`(vehicle_id, session_id)` (a later record for the same key replaces the
earlier one, so replays are idempotent). Records that fail the summary
schema are rejected and counted, never applied. Its observable state,
schema `contracts/warranty.consumer-state.v1.schema.json`:

```json
{"schema_version": "1.0", "vehicles": {"VEH-0040": {
  "warranty_energy_kwh": 66.0, "session_count": 3,
  "high_power_session_count": 1, "inactivity_timeout_count": 1}}}
```

- `warranty_energy_kwh`: sum of `total_energy_kwh` over the vehicle's
  sessions, summed in ascending `session_id` order, then rounded with
  Python `round(x, 3)`.
- `session_count`: distinct sessions.
- `high_power_session_count`: sessions with `peak_power_kw >= 150.0`.
- `inactivity_timeout_count`: sessions with `completion_reason == INACTIVITY_TIMEOUT`.

CLI: `python -m warranty_consumer --input <summaries.jsonl> [--replays N] --state-out <state.json>`.
The consumer is unchanged by the migration: no WP after WP4 edits
`consumer-warranty/`.

## 9. Fixtures

- `fixtures/scenarios/<name>/input.jsonl` and `scenario.json`
  (`name`, `description`, `covers`, `run`, `consumer_replays`).
  `run` is `{"mode": "single"}` or `{"mode": "restart", "stop_after": n}`
  (run once with `--stop-after n --checkpoint`, then once with
  `--resume-from`, same output dir).
- `fixtures/expected/<name>/summaries.jsonl`, `errors.jsonl` (canonical,
  emission order) and `consumer_state.json`. Expected outputs are the
  approved legacy behaviour.
- Both are generated by `fixtures/tools/generate_fixtures.py` from
  `fixtures/tools/reference_model.py`, the executable form of sections
  4-8. `generate_fixtures.py --check` fails CI on any drift.
- Scenarios: `normal_session`, `concurrent_sessions`, `duplicates`,
  `out_of_order`, `late_events_window_boundary`, `malformed_null`,
  `schema_evolution`, `processor_restart`, `consumer_replay`,
  `partitioning_ordering`, `state_recovery`, `numeric_precision`,
  `downstream_contract`.
- Synthetic ids only: vehicles `VEH-nnnn`, locations `SYN-*`, base time
  `2026-01-15T08:00:00.000Z`.

## 10. Verification seams

`verification/cli.py` is the single entry point (`verify`, `evidence`,
`cutover`). Gates communicate only through files in a run directory
(default `build-output/latest/`):

```
build-output/latest/
  context.json                       written by cli.py: git SHA, scenarios, processors, mode
  outputs/<processor>/<scenario>/    summaries.jsonl, errors.jsonl, processor.log, run.json
  gates/behavioural.json             WP3
  gates/contract.json                WP4
  gates/privacy.json                 WP5
  gates/state_ordering.json          WP3 (restart/recovery/per-key ordering subset)
  evidence/evidence.json|.md|.html   WP5
```

`run.json`: `{"processor", "scenario", "commands": [[...]], "exit_codes": [...], "duration_ms"}`.

Every gate file has this shape:

```json
{"gate": "behavioural", "status": "pass|fail|error",
 "summary": {"checks": 0, "passed": 0, "failed": 0},
 "checks": [{"id": "...", "status": "pass|fail|skip", "scenario": "... or null",
             "category": "...", "detail": "human readable",
             "evidence": {"...": "machine readable"},
             "verification": "runtime|static"}]}
```

`verification` says whether the claim was observed by running code
(`runtime`) or only by reading declarations or source (`static`). Every
check must set it.

Each gate module exposes `run_gate(ctx: verification.common.RunContext) -> dict`
(the dict above) and writes nothing outside its own gate file and
`ctx.run_dir`. `RunContext` is defined in `verification/common.py`
(frozen), together with the helpers `check()`, `gate_result()` and
`write_gate()` that every gate uses to build and write its file. The
runner (WP3) exposes `verification.runner.run_all(ctx) -> None` and fills
`outputs/`. With `ctx.baseline == "expected"` (or when the legacy
processor cannot be built) the runner does not run `legacy` and the gates
use `fixtures/expected/` as the baseline, recording that fact in their
checks. The evidence generator (WP5) exposes
`verification.evidence.build(ctx, out_dir: Path | None) -> dict` and reads
only `ctx.run_dir`. The cutover simulator (WP6) exposes
`verification.cutover.main(ctx, argv) -> int`.

Behavioural diff categories (WP3, ids used by evidence and cutover):
`MISSING_OUTPUT`, `EXTRA_OUTPUT`, `DUPLICATE_OUTPUT`,
`AGGREGATION_MISMATCH`, `NUMERIC_MISMATCH`, `ORDERING_WITHIN_KEY`,
`WINDOWING_MISMATCH`, `INVALID_STATE_TRANSITION`,
`SCHEMA_INCOMPATIBLE`, `SEMANTIC_MISMATCH`, `ERROR_PATH_MISMATCH`.
Numeric fields compare with an explicit tolerance (default absolute
`1e-6`) or, with `--strict`, exact binary64 equality.

Contract gate (WP4): JSON Schema, required-field preservation, field
semantics, per-key ordering, replay idempotence, no unexpected extra
outputs, identical consumer state from baseline vs candidate output.

Privacy gate (WP5): 12-dimension inventory diff against the approved
baseline and the automated policy checks, each labelled `runtime` or
`static`. Review dispositions (`no-material-change`,
`needs-clarification`, `policy-violation`,
`new-processing-requires-review`) are recommendations, not legal
determinations.

## 11. Ownership and frozen paths

Frozen (orchestrator only): `docs/build-contract.md`, `contracts/*.json`,
`governance/classifications.yaml`, `fixtures/**`, `processors.yaml`,
`verification/cli.py`, `verification/common.py`, `requirements.txt`,
`scripts/*.sh`, `.github/workflows/ci.yml`.

| WP | Owns (creates/edits only these) | Branch |
|---|---|---|
| WP1 legacy Scala/Spark | `legacy-scala/**` | `wp1-legacy-scala` |
| WP2 Rust processor | `rust-processor/**` except `rust-processor/processing-inventory.yaml` | `wp2-rust-processor` |
| WP3 differential harness | `verification/runner.py`, `verification/diff_harness/**`, `verification/tests/test_diff_*.py` | `wp3-diff-harness` |
| WP4 consumer + contract checks | `consumer-warranty/**`, `contracts/consumer-contract.md`, `verification/contract_checks/**`, `verification/tests/test_contract_*.py` | `wp4-consumer-contract` |
| WP5 governance + privacy + evidence | `governance/**` except `classifications.yaml`, `*/processing-inventory.yaml`, `verification/privacy/**`, `verification/evidence/**`, `verification/tests/test_privacy_*.py`, `verification/tests/test_evidence_*.py` | `wp5-governance-privacy` |
| Orchestrator | WP0, WP6 (`verification/cutover/**`), WP7 (tags), WP8 (`playbooks/`, `docs/`), integration | `build/*` |

Each WP branches from `build/wp0-contract`, commits only inside its owned
paths, keeps history linear (no merge commits), uses Conventional Commits
with a subject of at most 50 characters and body lines wrapped at 72, and
opens a PR against `build/wp0-contract`.

## 12. Contract conflicts

If a WP finds that the contract is wrong, ambiguous or insufficient, it
does not edit frozen paths and does not silently diverge. It writes the
issue into its PR description under a `## Contract conflicts` heading
(what, where, proposed change, impact), implements the contract as
written where possible, and reports the conflict in its final message.
The orchestrator decides and, if needed, changes the contract for all WPs.

## 13. Technology choices and rationale

The legacy processor uses Scala 2.13 and Spark Structured Streaming via
sbt. Its `replay` mode reads JSONL files and runs without a Spark cluster.
`legacy-scala/bin/spark-local` is an optional local Spark smoke demo, not a
gate. It exercises Spark's file source and micro-batches, whose watermark
and end-of-input behavior differs from deterministic replay. Keeping it
outside the gates also avoids making the fast tier depend on Spark runtime
setup.

The Rust processor uses an in-process keyed `StateStore` trait. Replay uses
`InMemoryStateStore`. Tokio and `rdkafka` are optional dependencies behind
the `kafka` feature. RocksDB is a possible future implementation of the
store trait, not a current dependency.

`docker-compose.yml` defines a single-node Redpanda broker for the optional
integration design. Redpanda is a lighter Kafka-API-compatible drop-in for
this demo. Its single-node development configuration has no replication or
authentication. It is not a production topology or a customer technology
choice. The optional integration tier runs bounded Kafka `serve` checks but
does not prove end-to-end exactly-once delivery or production readiness.

Both processors exchange summaries and errors as canonical JSONL for
language-neutral comparison. The default privacy policy engine, when WP5
lands, is a typed Python checker. It reads
`governance/policies/migration-policies.yaml` and dispatches the declared
rule IDs to Python implementations. The adjacent
`migration-policies.rego` is optional illustration only. It is not executed
or used as an automatic fallback by this demo.

## 14. Determinism and numeric precision

Fixtures and processor outputs must use fixed seeds where randomness is
needed, fixed explicit timestamps, and no wall-clock dependence. Existing
fixtures use explicit event timestamps. Test-only randomized input uses an
explicit seed. Processor output semantics must not depend on the time at
which a replay runs.

Numeric comparison uses the explicit absolute tolerance, default `1e-6`,
and `--strict` for exact binary64 equality. Strict mode helps surface
Scala/Rust binary64 representation and accumulation drift. A tolerance can
hide small differences, while strict binary64 equality is not decimal
arithmetic or a business-domain accuracy proof. Run metadata and cutover
audit timestamps may use wall-clock time, but are not processor output
fields compared as fixture results.

## 15. Work breakdown

Complexity is a relative planning estimate. Owners follow the ownership
plan in section 11. WP7 is named as tags there, but its detailed tagging
convention is not specified.

| WP | Scope | Depends on | Parallelisable | Complexity | Owner |
|---|---|---|---|---|---|
| WP0 | Shared contract, schemas, fixtures and repository seams | None | No | M | Orchestrator |
| WP1 | Legacy Scala/Spark processor and replay | WP0 | Yes | L | Sub-agent |
| WP2 | Rust processor and replay | WP0 | Yes | L | Sub-agent |
| WP3 | Runner and behavioral/state-ordering comparisons | WP0 | Yes | M | Sub-agent |
| WP4 | Warranty consumer and contract gate | WP0 | Yes | M | Sub-agent |
| WP5 | Governance manifests, privacy checks and evidence package | WP0 | Yes | L | Sub-agent |
| WP6 | Local shadow, cutover and rollback simulator | WP0-WP5 | No | M | Orchestrator |
| WP7 | Tags, with details to be set by the orchestrator | WP6 | No | S | Orchestrator |
| WP8 | Documentation and playbooks | WP0-WP7 | No | M | Orchestrator |
| WP9 | Optional Kafka integration tier | Core WP0-WP8, after core | Yes | L | Orchestrator |

The sequential spine is:

`WP0 -> WP1-WP5 (parallel) -> WP6 -> WP7 -> WP8, WP9 optional after the core.`
