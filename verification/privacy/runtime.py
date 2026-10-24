from __future__ import annotations

import json
import re
from pathlib import Path

from verification.common import check

OUTPUT_FILES = {
    "summaries.jsonl": "charging.session.summary.v1",
    "errors.jsonl": "charging.errors.v1",
}
IDENTIFIER_FIELDS = ("vehicle_id", "session_id")


def _check(check_id, status, detail, severity, *, scenario, rule=None, evidence=None):
    data = {"severity": severity, "basis": "runtime-observed"}
    if rule:
        data["rule"] = rule
    data.update(evidence or {})
    return check(check_id, status, detail, category="runtime_privacy", verification="runtime",
                 scenario=scenario, evidence=data)


def _input_records(path: Path) -> list[dict]:
    records = []
    if not path.is_file():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _skip(check_id, scenario, rule):
    return _check(check_id, "skip", "no candidate output; runtime claim not established",
                  "clarification", scenario=scenario, rule=rule)


def output_fields(ctx, scenario, output_dir: Path, baseline_dimensions: dict) -> dict:
    check_id = "runtime.output_fields"
    rule = "no-new-sensitive-output-fields"
    if not output_dir.is_dir():
        return _skip(check_id, scenario, rule)
    schema_fields = {
        item.get("id"): set(item.get("fields", []))
        for item in baseline_dimensions.get("output_schema", [])
    }
    unexpected = {}
    unparsable = {}
    readable_files = []
    for filename, topic in OUTPUT_FILES.items():
        path = output_dir / filename
        if not path.is_file():
            continue
        readable_files.append(filename)
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                unparsable.setdefault(filename, []).append(line_number)
                continue
            if not isinstance(record, dict):
                unparsable.setdefault(filename, []).append(line_number)
                continue
            extra = set(record) - schema_fields.get(topic, set())
            for field in extra:
                key = f"{filename}:{field}"
                unexpected[key] = unexpected.get(key, 0) + 1
    if not readable_files:
        return _check(check_id, "skip", "candidate output files not present; runtime claim not established",
                      "clarification", scenario=scenario, rule=rule)
    status = "fail" if unexpected or unparsable else "pass"
    severity = "violation" if unexpected else ("clarification" if unparsable else "violation")
    if unexpected:
        detail = f"candidate output contains fields outside baseline schemas: {sorted(unexpected)}"
    elif unparsable:
        detail = "one or more candidate output lines could not be parsed"
    else:
        detail = "candidate output fields are within baseline schemas"
    return _check(check_id, status, detail, severity, scenario=scenario, rule=rule,
                  evidence={"unexpected_fields": unexpected, "unparsable_lines": unparsable,
                            "files": readable_files})


def no_sensitive_location(ctx, scenario, output_dir: Path, params: dict) -> dict:
    check_id = "runtime.no_sensitive_location"
    rule = "no-sensitive-location-propagation"
    if not output_dir.is_dir():
        return _skip(check_id, scenario, rule)
    source = ctx.scenario_dir(scenario) / "input.jsonl"
    location_values = {
        record.get("synthetic_location") for record in _input_records(source)
        if isinstance(record.get("synthetic_location"), str)
    }
    pattern = re.compile(params["restricted_value_pattern"])
    checked = []
    matches = {}
    for filename in ("summaries.jsonl", "errors.jsonl", "processor.log"):
        path = output_dir / filename
        if not path.is_file():
            continue
        checked.append(filename)
        text = path.read_text(encoding="utf-8")
        pattern_count = len(pattern.findall(text))
        value_counts = {value: text.count(value) for value in sorted(location_values) if value in text}
        if pattern_count or value_counts:
            matches[filename] = {"pattern_matches": pattern_count, "synthetic_location_values": value_counts}
    if not checked:
        return _check(check_id, "skip", "candidate output files not present; runtime claim not established",
                      "clarification", scenario=scenario, rule=rule)
    return _check(check_id, "fail" if matches else "pass",
                  "restricted location values occur in candidate output" if matches
                  else "no restricted location values found in candidate output",
                  "violation", scenario=scenario, rule=rule,
                  evidence={"matches": matches, "files": checked})


def no_identifiers_in_log(ctx, scenario, output_dir: Path) -> dict:
    check_id = "runtime.no_identifiers_in_log"
    rule = "no-identifiers-to-unapproved-destinations"
    if not output_dir.is_dir():
        return _skip(check_id, scenario, rule)
    log_path = output_dir / "processor.log"
    if not log_path.is_file():
        return _check(check_id, "skip", "processor.log not present; runtime claim not established",
                      "clarification", scenario=scenario, rule=rule)
    text = log_path.read_text(encoding="utf-8")
    counts = {field: 0 for field in IDENTIFIER_FIELDS}
    for record in _input_records(ctx.scenario_dir(scenario) / "input.jsonl"):
        for field in IDENTIFIER_FIELDS:
            value = record.get(field)
            if not isinstance(value, str):
                continue
            pattern = re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(value) + r"(?![A-Za-z0-9_-])")
            counts[field] += len(pattern.findall(text))
    matched_fields = {field: count for field, count in counts.items() if count}
    return _check(check_id, "fail" if matched_fields else "pass",
                  "identifier values occur in processor.log" if matched_fields
                  else "no input identifiers found in processor.log",
                  "violation", scenario=scenario, rule=rule,
                  evidence={"matched_fields": matched_fields, "files": ["processor.log"]})


def runtime_checks(ctx, scenarios: list[str], baseline_dimensions: dict, params: dict) -> list[dict]:
    checks = []
    for scenario in scenarios:
        output_dir = ctx.output_dir(ctx.candidate, scenario)
        checks.extend((
            output_fields(ctx, scenario, output_dir, baseline_dimensions),
            no_sensitive_location(ctx, scenario, output_dir, params),
            no_identifiers_in_log(ctx, scenario, output_dir),
        ))
    return checks
