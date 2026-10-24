# Differential verification harness

The WP3 harness runs registered processors, compares their replay outputs
with the approved expected fixtures, and writes behavioural and
state-ordering gate results under the run directory. It uses synthetic
fixtures only.

## Compared output

The comparator reads `summaries.jsonl` and `errors.jsonl`. It groups
summaries by `(vehicle_id, session_id)` and preserves emission order inside
each key. It never asserts an order across session keys. Error records are
compared as a multiset of `(input_line, error_type, field, event_id,
vehicle_id, session_id)` tuples, with ordering checked only inside each
session key. Missing streams, blank lines, malformed JSON, and non-object
JSON are handled without retaining raw line contents.

Actual records are validated against the summary and error JSON Schemas.
Baseline records are not schema-validated. The comparison categories have
these triggers:

| Category | Trigger |
| --- | --- |
| `MISSING_OUTPUT` | A baseline summary key is absent, or fewer records were emitted for the key. |
| `EXTRA_OUTPUT` | An actual summary key has no baseline record. |
| `DUPLICATE_OUTPUT` | More than one actual summary was emitted for one key. |
| `AGGREGATION_MISMATCH` | `event_count` differs, or a numeric aggregate differs by more than tolerance. |
| `NUMERIC_MISMATCH` | Numeric values differ beyond tolerance, or differ at all in strict mode. |
| `ORDERING_WITHIN_KEY` | Records with the same session key have a different order. Cross-key order is ignored. |
| `WINDOWING_MISMATCH` | `start_ts`, `end_ts`, or `completion_reason` differs. |
| `INVALID_STATE_TRANSITION` | A key changes after emission, a summary appears for a baseline error-only key, or `SESSION_ALREADY_CLOSED` has no actual summary. |
| `SCHEMA_INCOMPATIBLE` | An actual record fails its output schema, or an output line cannot be parsed as a JSON object. |
| `SEMANTIC_MISMATCH` | An actual output violates a necessary condition derived from its scenario input. |
| `ERROR_PATH_MISMATCH` | An error tuple is missing from or extra in the actual error stream, including multiplicity. |

The comparator checks aligned summary records per key. If a key has
multiple baseline records and the same records appear in a different order,
it reports `ORDERING_WITHIN_KEY` and skips field comparison for that key.
One actual summary repeated identically is a duplicate but not a changed
state transition. A later differing summary is both a duplicate and an
invalid state transition.

## Numeric policy

The default absolute tolerance is `1e-6`. Integer and float JSON values
compare by numeric value, so `15` equals `15.0`. In non-strict mode a
nonzero difference at or below tolerance is ignored. In strict mode any
binary64 difference is a `NUMERIC_MISMATCH`. A difference greater than
tolerance also produces `AGGREGATION_MISMATCH`; a strict-only difference
within tolerance does not.

The Scala and Rust processors both use binary64, but their JSON number
formatting and parsing paths, and any arithmetic, can drift in the last
ulp. Tolerance mode absorbs such drift. Strict mode reports a last-ulp
difference as `NUMERIC_MISMATCH` only, not `AGGREGATION_MISMATCH`.
Contract section 5 says values are passed through without arithmetic, so
any strict-mode difference is worth investigating.

## Semantic checks

Semantic checks derive necessary conditions from a superset of the input
events. They do not recompute the processor and do not use expected
`event_count`, since knowing a count does not identify which events were
accepted. They check that summary keys, window timestamps, cumulative
energy values, peak-power values, event counts, and completion states can
be supported by input readings. They also check error line ranges and
echoed identifiers.

For each summary, its key must occur in the input. Its start and end
timestamps must each be timestamps for that key and start must not follow
end. Within that window, total energy and peak power must each equal an
input reading within the numeric policy. Event count cannot exceed the
number of distinct string event IDs, and count one requires an
instantaneous window. `COMPLETE` requires a `COMPLETE` input reading in
the window. For each error, the line number must be in range and each
non-null echoed identifier must match the object on that input line.

Accepted events are a subset of the candidate input readings. Therefore a
semantic failure is a contract violation, but a pass does not prove
correctness.

## Runner and baseline

Each processor has a `build.json` and `build.log` under
`outputs/<processor>/`. Each scenario has a `run.json` and appended
`processor.log` under `outputs/<processor>/<scenario>/`. Restart scenarios
use `checkpoints/<processor>/<scenario>.ckpt`, run a clean stop with
`--stop-after` and `--checkpoint`, then resume into the same output
directory. The harness checks that a checkpoint was written and that resume
flags were passed, but cannot prove the first run stopped after n lines
without reimplementing the processor. Final-output equivalence and
no-duplicate checks cover the observable result. The default run timeout is
60 seconds and the build timeout is 1800 seconds.

If legacy is requested but its required tool is missing or its build
fails, `outputs/legacy/UNAVAILABLE.json` records the reason and the
baseline falls back to expected fixtures. The runner does not mutate the
run context. A candidate build failure prevents candidate scenario runs
and makes the gates return `error`.

For tests only, `processors.override.yaml` in the run directory replaces
named entries from the frozen `processors.yaml` registry. An override's
`requires` list is used as-is and defaults to empty. The override file is a
test seam, not a production registry mechanism.

## Guarantee boundaries

The harness does not assert global or cross-key order. It does not assert
per-vehicle order across sessions, since only session-key order is
meaningful in the contract. It does not assert wall-clock timing.

Restart runs verify a clean stop and resume. They do not assert
exactly-once processing. A crash between output flush and checkpoint can
re-emit output, so restart behavior is at-least-once per contract section
7. Passing checks are not production safety and do not remove
privacy-review obligations. The harness does not assert atomic cutover.

## Running

```sh
source scripts/_env.sh && $PY -m pytest -q verification/tests/test_diff_*.py
$PY verification/cli.py verify --baseline expected --gates behavioural state_ordering
```
