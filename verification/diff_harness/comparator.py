"""Compare processor outputs by session key without asserting global order."""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import jsonschema

from verification.common import REPO_ROOT, canonical

CATEGORIES = (
    "MISSING_OUTPUT",
    "EXTRA_OUTPUT",
    "DUPLICATE_OUTPUT",
    "AGGREGATION_MISMATCH",
    "NUMERIC_MISMATCH",
    "ORDERING_WITHIN_KEY",
    "WINDOWING_MISMATCH",
    "INVALID_STATE_TRANSITION",
    "SCHEMA_INCOMPATIBLE",
    "SEMANTIC_MISMATCH",
    "ERROR_PATH_MISMATCH",
)


@dataclass(frozen=True)
class Finding:
    category: str
    stream: str
    key: tuple | None
    detail: str
    data: dict

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "stream": self.stream,
            "key": list(self.key) if self.key is not None else None,
            "detail": self.detail,
            "data": self.data,
        }


@dataclass
class Outputs:
    summaries: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    unparsable: list[dict] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)


def _reject_constant(value: str):
    raise ValueError(f"invalid JSON constant: {value}")


def _reject_nonfinite(value: str):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non-finite JSON number: {value}")
    return number


def load_outputs(directory: Path) -> Outputs:
    outputs = Outputs()
    for stream in ("summaries", "errors"):
        name = f"{stream}.jsonl"
        path = directory / name
        if not path.exists():
            outputs.missing_files.append(name)
            continue
        records = getattr(outputs, stream)
        for line_no, line in enumerate(path.read_text().splitlines(), start=1):
            try:
                record = json.loads(line, parse_constant=_reject_constant,
                                    parse_float=_reject_nonfinite)
            except (ValueError, json.JSONDecodeError):
                outputs.unparsable.append({"stream": stream, "line": line_no})
                continue
            if not isinstance(record, dict):
                outputs.unparsable.append({"stream": stream, "line": line_no})
                continue
            records.append(record)
    return outputs


@lru_cache(maxsize=None)
def _schema_validator(name: str) -> jsonschema.Draft202012Validator:
    path = REPO_ROOT / "contracts" / f"{name}.schema.json"
    schema = json.loads(path.read_text())
    return jsonschema.Draft202012Validator(schema)


def _record_key(record: dict) -> tuple | None:
    vehicle_id = record.get("vehicle_id")
    session_id = record.get("session_id")
    if isinstance(vehicle_id, str) and isinstance(session_id, str):
        return vehicle_id, session_id
    return None


def _key_sort(key: tuple | None) -> tuple:
    return (0, "", "") if key is None else (1, key[0], key[1])


def _group(records: list[dict]) -> dict[tuple | None, list[dict]]:
    grouped: dict[tuple | None, list[dict]] = defaultdict(list)
    for record in records:
        grouped[_record_key(record)].append(record)
    return dict(grouped)


def _finding(category: str, stream: str, key: tuple | None, detail: str,
             **data) -> Finding:
    return Finding(category, stream, key, detail, data)


def _schema_findings(actual: Outputs) -> list[Finding]:
    findings = []
    for stream, schema_name in (
        ("summaries", "charging.session.summary.v1"),
        ("errors", "charging.errors.v1"),
    ):
        validator = _schema_validator(schema_name)
        for index, record in enumerate(getattr(actual, stream)):
            error = next(iter(validator.iter_errors(record)), None)
            if error is not None:
                findings.append(Finding(
                    "SCHEMA_INCOMPATIBLE",
                    stream,
                    _record_key(record),
                    f"{stream} record {index} does not match its schema: {error.message}",
                    {
                        "stream": stream,
                        "index": index,
                        "message": error.message,
                        "json_path": error.json_path,
                    },
                ))
    for invalid_line in actual.unparsable:
        findings.append(Finding(
            "SCHEMA_INCOMPATIBLE",
            invalid_line["stream"],
            None,
            f"unparsable {invalid_line['stream']} line {invalid_line['line']}",
            {
                "stream": invalid_line["stream"],
                "line": invalid_line["line"],
            },
        ))
    return findings


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _compare_summaries(baseline: Outputs, actual: Outputs, tolerance: float,
                       strict: bool) -> list[Finding]:
    findings = []
    before = _group(baseline.summaries)
    after = _group(actual.summaries)
    baseline_error_keys = {
        _record_key(record) for record in baseline.errors
        if _record_key(record) is not None
    }
    actual_summary_keys = {key for key in after if key is not None}

    for key in sorted(set(before) | set(after), key=_key_sort):
        base_records = before.get(key, [])
        actual_records = after.get(key, [])
        base_canonical = [canonical(record) for record in base_records]
        actual_canonical = [canonical(record) for record in actual_records]
        if base_records and not actual_records:
            findings.append(_finding(
                "MISSING_OUTPUT", "summaries", key,
                "baseline summary key is missing from actual output",
                count=len(base_records),
            ))
            continue
        if actual_records and not base_records:
            findings.append(_finding(
                "EXTRA_OUTPUT", "summaries", key,
                "actual summary key is absent from baseline",
                count=len(actual_records),
            ))
        if len(actual_records) > 1:
            findings.append(_finding(
                "DUPLICATE_OUTPUT", "summaries", key,
                f"actual output emitted {len(actual_records)} summaries for one key",
                count=len(actual_records),
            ))
            if any(value != actual_canonical[0] for value in actual_canonical[1:]):
                findings.append(_finding(
                    "INVALID_STATE_TRANSITION", "summaries", key,
                    "summary changed after the key was already emitted",
                    first=actual_records[0],
                    later=next(record for record, value in zip(actual_records[1:],
                                                                 actual_canonical[1:])
                               if value != actual_canonical[0]),
                ))
        if key in baseline_error_keys and key not in before and actual_records:
            findings.append(_finding(
                "INVALID_STATE_TRANSITION", "summaries", key,
                "actual summary exists for a key routed entirely to baseline errors",
            ))
        if not base_records:
            continue
        if len(actual_records) < len(base_records):
            findings.append(_finding(
                "MISSING_OUTPUT", "summaries", key,
                f"actual output is missing {len(base_records) - len(actual_records)} summary record(s)",
                count=len(base_records) - len(actual_records),
            ))
        if (len(base_records) > 1 and len(actual_records) > 1
                and sorted(base_canonical) == sorted(actual_canonical)
                and base_canonical != actual_canonical):
            findings.append(_finding(
                "ORDERING_WITHIN_KEY", "summaries", key,
                "records for one session key were emitted in a different order",
            ))
            continue
        for base_record, actual_record in zip(base_records, actual_records):
            if base_record.get("event_count") != actual_record.get("event_count"):
                findings.append(_finding(
                    "AGGREGATION_MISMATCH", "summaries", key,
                    f"event_count differs: baseline {base_record.get('event_count')!r}, "
                    f"actual {actual_record.get('event_count')!r}",
                    field="event_count",
                    baseline=base_record.get("event_count"),
                    actual=actual_record.get("event_count"),
                ))
            for field_name in ("total_energy_kwh", "peak_power_kw"):
                base_value = base_record.get(field_name)
                actual_value = actual_record.get(field_name)
                if not _number(base_value) or not _number(actual_value):
                    continue
                try:
                    base_number = float(base_value)
                    actual_number = float(actual_value)
                except OverflowError:
                    continue
                if not math.isfinite(base_number) or not math.isfinite(actual_number):
                    continue
                difference = abs(base_number - actual_number)
                exact = base_number == actual_number
                materially_different = difference > tolerance
                if materially_different or (strict and not exact):
                    mode = "strict" if strict else "tolerance"
                    findings.append(_finding(
                        "NUMERIC_MISMATCH", "summaries", key,
                        f"{field_name} differs in {mode} mode: "
                        f"baseline {base_value!r}, actual {actual_value!r}",
                        field=field_name,
                        baseline=base_value,
                        actual=actual_value,
                        abs_diff=difference,
                        tolerance=tolerance,
                        strict=strict,
                        mode=mode,
                    ))
                if materially_different:
                    findings.append(_finding(
                        "AGGREGATION_MISMATCH", "summaries", key,
                        f"{field_name} differs materially: baseline {base_value!r}, "
                        f"actual {actual_value!r}",
                        field=field_name,
                        baseline=base_value,
                        actual=actual_value,
                    ))
            for field_name in ("start_ts", "end_ts", "completion_reason"):
                if base_record.get(field_name) != actual_record.get(field_name):
                    findings.append(_finding(
                        "WINDOWING_MISMATCH", "summaries", key,
                        f"{field_name} differs: baseline {base_record.get(field_name)!r}, "
                        f"actual {actual_record.get(field_name)!r}",
                        field=field_name,
                        baseline=base_record.get(field_name),
                        actual=actual_record.get(field_name),
                    ))

    for record in actual.errors:
        key = _record_key(record)
        if record.get("error_type") == "SESSION_ALREADY_CLOSED" and key is not None \
                and key not in actual_summary_keys:
            findings.append(_finding(
                "INVALID_STATE_TRANSITION", "errors", key,
                "SESSION_ALREADY_CLOSED was reported without an actual summary",
                error=record,
            ))
    return findings


ERROR_FIELDS = ("input_line", "error_type", "field", "event_id", "vehicle_id", "session_id")


def _error_tuple(record: dict) -> tuple:
    values = []
    for field_name in ERROR_FIELDS:
        value = record.get(field_name)
        if field_name == "input_line" and isinstance(value, float) and value.is_integer():
            value = int(value)
        values.append(value)
    return tuple(values)


def _tuple_token(values: tuple) -> str:
    return canonical(list(values))


def _compare_errors(baseline: Outputs, actual: Outputs) -> list[Finding]:
    findings = []
    base_counts = Counter(_tuple_token(_error_tuple(record)) for record in baseline.errors)
    actual_counts = Counter(_tuple_token(_error_tuple(record)) for record in actual.errors)
    example_by_token = {
        _tuple_token(_error_tuple(record)): _error_tuple(record)
        for record in baseline.errors + actual.errors
    }
    for token in sorted(set(base_counts) | set(actual_counts)):
        missing = max(base_counts[token] - actual_counts[token], 0)
        extra = max(actual_counts[token] - base_counts[token], 0)
        if missing:
            values = example_by_token[token]
            key = _record_key({"vehicle_id": values[4], "session_id": values[5]})
            findings.append(_finding(
                "ERROR_PATH_MISMATCH", "errors", key,
                f"baseline error tuple is missing {missing} time(s)",
                direction="missing",
                tuple=list(values),
                count=missing,
            ))
        if extra:
            values = example_by_token[token]
            key = _record_key({"vehicle_id": values[4], "session_id": values[5]})
            findings.append(_finding(
                "ERROR_PATH_MISMATCH", "errors", key,
                f"actual output has {extra} extra error tuple(s)",
                direction="extra",
                tuple=list(values),
                count=extra,
            ))

    base_groups = _group(baseline.errors)
    actual_groups = _group(actual.errors)
    for key in sorted(set(base_groups) | set(actual_groups), key=_key_sort):
        if key is None:
            continue
        base_records = base_groups.get(key, [])
        actual_records = actual_groups.get(key, [])
        if Counter(_tuple_token(_error_tuple(r)) for r in base_records) != \
                Counter(_tuple_token(_error_tuple(r)) for r in actual_records):
            continue
        base_order = [_tuple_token(_error_tuple(r)) for r in base_records]
        actual_order = [_tuple_token(_error_tuple(r)) for r in actual_records]
        if base_order != actual_order:
            findings.append(_finding(
                "ORDERING_WITHIN_KEY", "errors", key,
                "errors for one session key were emitted in a different order",
            ))
    return findings


def compare(baseline: Outputs, actual: Outputs, *, tolerance: float = 1e-6,
            strict: bool = False, input_text: str | None = None) -> list[Finding]:
    findings = [
        _finding(
            "MISSING_OUTPUT", name.removesuffix(".jsonl"), None,
            f"{name} was not created",
            file=name,
        )
        for name in actual.missing_files
    ]
    findings.extend(_schema_findings(actual))
    findings.extend(_compare_summaries(baseline, actual, tolerance, strict))
    findings.extend(_compare_errors(baseline, actual))
    if input_text is not None:
        from verification.diff_harness.semantic import check

        findings.extend(check(actual, input_text, tolerance=tolerance, strict=strict))
    return findings
