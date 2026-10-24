"""Necessary input-derived checks that do not recompute processor acceptance.

Lexical window ordering uses only fixed-width ASCII UTC timestamps.
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict

from verification.diff_harness.comparator import Finding, Outputs

TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$")


def _reject_constant(value: str):
    raise ValueError(f"invalid JSON constant: {value}")


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_float(value) -> float | None:
    if not _number(value):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _readings(input_text: str) -> tuple[list[object], dict[tuple[str, str], list[dict]]]:
    lines = input_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    parsed = []
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for line_no, line in enumerate(lines, start=1):
        try:
            record = json.loads(line, parse_constant=_reject_constant)
        except (ValueError, json.JSONDecodeError):
            record = None
        parsed.append(record)
        if not isinstance(record, dict):
            continue
        vehicle_id = record.get("vehicle_id")
        session_id = record.get("session_id")
        if not isinstance(vehicle_id, str) or not isinstance(session_id, str):
            continue
        timestamp = record.get("event_timestamp")
        if not isinstance(timestamp, str) or not TIMESTAMP_RE.fullmatch(timestamp):
            timestamp = None
        event_id = record.get("event_id")
        by_key[(vehicle_id, session_id)].append({
            "line": line_no,
            "event_id": event_id if isinstance(event_id, str) else None,
            "timestamp": timestamp,
            "energy": _as_float(record.get("energy_delivered_kwh")),
            "power": _as_float(record.get("charging_power_kw")),
            "charging_state": record.get("charging_state"),
        })
    return parsed, by_key


def _numeric_equal(value, candidate: float, tolerance: float, strict: bool) -> bool:
    number = _as_float(value)
    if number is None:
        return True
    difference = abs(number - candidate)
    return number == candidate if strict else difference <= tolerance


def _finding(key, detail: str, rule: str, **data) -> Finding:
    return Finding(
        "SEMANTIC_MISMATCH",
        "summaries" if rule != "error_echo" and rule != "error_input_line" else "errors",
        key,
        detail,
        {"rule": rule, **data},
    )


def check(actual: Outputs, input_text: str, *, tolerance: float,
          strict: bool) -> list[Finding]:
    parsed_lines, readings_by_key = _readings(input_text)
    findings = []
    for index, summary in enumerate(actual.summaries):
        vehicle_id = summary.get("vehicle_id")
        session_id = summary.get("session_id")
        key = ((vehicle_id, session_id)
               if isinstance(vehicle_id, str) and isinstance(session_id, str) else None)
        candidates = readings_by_key.get(key, []) if key is not None else []
        if not candidates:
            findings.append(_finding(
                key, "summary key does not occur in input", "key_in_input",
                index=index,
            ))

        start_ts = summary.get("start_ts")
        end_ts = summary.get("end_ts")
        timestamps = {reading["timestamp"] for reading in candidates
                      if reading["timestamp"] is not None}
        if isinstance(start_ts, str) and start_ts not in timestamps:
            findings.append(_finding(
                key, "start_ts is not an input timestamp for this key", "start_ts_in_input",
                index=index, value=start_ts,
            ))
        if isinstance(end_ts, str) and end_ts not in timestamps:
            findings.append(_finding(
                key, "end_ts is not an input timestamp for this key", "end_ts_in_input",
                index=index, value=end_ts,
            ))
        if isinstance(start_ts, str) and isinstance(end_ts, str):
            if start_ts > end_ts:
                findings.append(_finding(
                    key, "start_ts is later than end_ts", "window_order",
                    index=index, start_ts=start_ts, end_ts=end_ts,
                ))
            window = [reading for reading in candidates
                      if reading["timestamp"] is not None
                      and start_ts <= reading["timestamp"] <= end_ts]
        else:
            window = []

        energy = summary.get("total_energy_kwh")
        energy_number = _as_float(energy)
        window_energies = [reading["energy"] for reading in window
                           if reading["energy"] is not None]
        if energy_number is not None and not any(
                _numeric_equal(energy_number, value, tolerance, strict)
                for value in window_energies):
            findings.append(_finding(
                key, "total_energy_kwh is not an input reading in the summary window",
                "energy_is_input_reading", index=index, value=energy,
            ))

        power = summary.get("peak_power_kw")
        power_number = _as_float(power)
        window_powers = [reading["power"] for reading in window
                         if reading["power"] is not None]
        if power_number is not None and not any(
                _numeric_equal(power_number, value, tolerance, strict)
                for value in window_powers):
            findings.append(_finding(
                key, "peak_power_kw is not an input reading in the summary window",
                "peak_power_is_input_reading", index=index, value=power,
            ))

        event_count = summary.get("event_count")
        if isinstance(event_count, int) and not isinstance(event_count, bool):
            distinct_ids = {reading["event_id"] for reading in window
                            if reading["event_id"] is not None}
            if event_count > len(distinct_ids):
                findings.append(_finding(
                    key, "event_count exceeds distinct input event ids in the window",
                    "event_count_bounded_by_input", index=index,
                    event_count=event_count, distinct_event_ids=len(distinct_ids),
                ))
            if event_count == 1 and isinstance(start_ts, str) and isinstance(end_ts, str) \
                    and start_ts != end_ts:
                findings.append(_finding(
                    key, "event_count of one requires an instantaneous window",
                    "single_event_window", index=index,
                    start_ts=start_ts, end_ts=end_ts,
                ))

        if summary.get("completion_reason") == "COMPLETE" and not any(
                reading["charging_state"] == "COMPLETE" for reading in window):
            findings.append(_finding(
                key, "COMPLETE summary has no COMPLETE input reading in its window",
                "complete_has_input_reading", index=index,
            ))

    line_count = len(parsed_lines)
    for index, error in enumerate(actual.errors):
        input_line = error.get("input_line")
        valid_line = isinstance(input_line, int) and not isinstance(input_line, bool) \
            and 1 <= input_line <= line_count
        key = None
        vehicle_id = error.get("vehicle_id")
        session_id = error.get("session_id")
        if isinstance(vehicle_id, str) and isinstance(session_id, str):
            key = vehicle_id, session_id
        if not valid_line:
            findings.append(_finding(
                key, "error input_line is outside the input range", "error_input_line",
                index=index, input_line=input_line, line_count=line_count,
            ))
            continue
        record = parsed_lines[input_line - 1]
        for field_name in ("event_id", "vehicle_id", "session_id"):
            echoed = error.get(field_name)
            if echoed is not None and (
                    not isinstance(record, dict) or record.get(field_name) != echoed):
                findings.append(_finding(
                    key, f"error {field_name} does not match its input line",
                    "error_echo", index=index, field=field_name,
                    input_line=input_line, value=echoed,
                ))
    return findings
