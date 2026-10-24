from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from numbers import Real
from pathlib import Path

import jsonschema

from verification.common import RunContext, canonical, check, gate_result, write_gate


SUMMARY_SCHEMA = "charging.session.summary.v1.schema.json"
ERROR_SCHEMA = "charging.errors.v1.schema.json"
SEMANTIC_FIELDS = (
    "start_ts",
    "end_ts",
    "event_count",
    "completion_reason",
    "vehicle_id",
    "session_id",
    "schema_version",
)
NUMERIC_FIELDS = ("total_energy_kwh", "peak_power_kw")


def _load_schema(ctx: RunContext, name: str) -> dict:
    return json.loads((ctx.repo_root / "contracts" / name).read_text())


def _relative(path: Path, ctx: RunContext) -> str:
    resolved = path.resolve()
    for root in (ctx.repo_root.resolve(), ctx.run_dir.resolve()):
        try:
            return str(resolved.relative_to(root))
        except ValueError:
            continue
    return str(resolved)


def _baseline(ctx: RunContext, scenario: str) -> tuple[Path, str]:
    legacy = ctx.output_dir("legacy", scenario)
    if ctx.baseline == "legacy" and legacy.exists():
        return legacy, "legacy"
    return ctx.expected_dir(scenario), "expected"


def _parse_constant(token: str):
    raise ValueError(token)


def _read_jsonl(path: Path) -> tuple[list[tuple[int, object]], int, list[dict]]:
    if not path.exists():
        return [], 0, []
    lines = path.read_text().splitlines()
    records = []
    errors = []
    for line_number, line in enumerate(lines, start=1):
        try:
            records.append((line_number, json.loads(line, parse_constant=_parse_constant)))
        except (json.JSONDecodeError, ValueError):
            errors.append({
                "file": path.name,
                "line": line_number,
                "path": None,
                "validator": "json_parse",
            })
    return records, len(lines), errors


def _schema_errors(
    records: list[tuple[int, object]],
    schema: dict,
    filename: str,
    parse_errors: list[dict],
) -> list[dict]:
    validator = jsonschema.Draft202012Validator(schema)
    errors = list(parse_errors)
    for line_number, record in records:
        for error in validator.iter_errors(record):
            errors.append({
                "file": filename,
                "line": line_number,
                "path": "/".join(str(part) for part in error.absolute_path) or None,
                "validator": error.validator,
            })
    return errors


def _valid_key(record) -> tuple[str, str] | None:
    if not isinstance(record, dict):
        return None
    vehicle_id = record.get("vehicle_id")
    session_id = record.get("session_id")
    if not isinstance(vehicle_id, str) or not isinstance(session_id, str):
        return None
    return vehicle_id, session_id


def _effective(records: list) -> dict[tuple[str, str], dict]:
    latest = {}
    for record in records:
        key = _valid_key(record)
        if key is not None:
            latest[key] = record
    return latest


def _keys_as_dict(keys) -> list[dict]:
    return [
        {"vehicle_id": vehicle_id, "session_id": session_id}
        for vehicle_id, session_id in sorted(keys)
    ]


def _numeric(value) -> bool:
    if not isinstance(value, Real) or isinstance(value, bool):
        return False
    try:
        return Decimal(str(value)).is_finite()
    except InvalidOperation:
        return False


def _numeric_equal(left, right, ctx: RunContext) -> bool:
    if not _numeric(left) or not _numeric(right):
        return False
    if ctx.strict:
        return left == right
    return abs(Decimal(str(left)) - Decimal(str(right))) <= Decimal(str(ctx.tolerance))


def _same_json_value(left, right) -> bool:
    # Parsed-value equality (contract s6.3): 4 equals 4.0, but a bool is never a number.
    return isinstance(left, bool) == isinstance(right, bool) and left == right


def _parse_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return None


def _check_evidence(
    evidence: dict,
    ctx: RunContext,
    baseline_source: str,
    baseline_dir: Path,
    candidate_dir: Path,
) -> dict:
    return {
        **evidence,
        "baseline_source": baseline_source,
        "baseline_dir": _relative(baseline_dir, ctx),
        "candidate_dir": _relative(candidate_dir, ctx),
    }


def _make_check(
    name: str,
    status: str,
    detail: str,
    scenario: str,
    ctx: RunContext,
    baseline_source: str,
    baseline_dir: Path,
    candidate_dir: Path,
    evidence: dict,
) -> dict:
    return check(
        f"contract.{name}",
        status,
        detail,
        category=name,
        verification="runtime",
        scenario=scenario,
        evidence=_check_evidence(evidence, ctx, baseline_source, baseline_dir, candidate_dir),
    )


def _ordering(records: list, shared_keys: set[tuple[str, str]]) -> dict[str, list[str]]:
    order = {}
    seen = set()
    for record in records:
        key = _valid_key(record)
        if key is None or key not in shared_keys or key in seen:
            continue
        seen.add(key)
        order.setdefault(key[0], []).append(key[1])
    return order


def _record_reemissions(records: list) -> tuple[list[dict], int]:
    first = {}
    conflicts = []
    duplicates = 0
    for emission_index, record in enumerate(records, start=1):
        key = _valid_key(record)
        if key is None:
            continue
        if key not in first:
            first[key] = record
        elif set(first[key]) == set(record) and all(
            _same_json_value(first[key][field], record[field])
            for field in first[key]
        ):
            duplicates += 1
        else:
            conflicts.append({
                "vehicle_id": key[0],
                "session_id": key[1],
                "emission_index": emission_index,
            })
    return conflicts, duplicates


def _empty_input(ctx: RunContext, side: str, scenario: str) -> Path:
    directory = ctx.run_dir / "consumer" / side
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{scenario}.summaries.jsonl"
    path.write_text("")
    return path


def _consumer_run(
    ctx: RunContext,
    scenario: str,
    side: str,
    summaries_path: Path,
    replays: int,
) -> dict:
    directory = ctx.run_dir / "consumer" / side
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / f"{scenario}.json"
    rejects_path = Path(str(state_path) + ".rejects.json")
    state_path.unlink(missing_ok=True)
    rejects_path.unlink(missing_ok=True)
    input_path = summaries_path if summaries_path.exists() else _empty_input(ctx, side, scenario)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((
        str(ctx.repo_root / "consumer-warranty" / "src"),
        env.get("PYTHONPATH", ""),
    )).rstrip(os.pathsep)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [
        sys.executable,
        "-m",
        "warranty_consumer",
        "--input",
        str(input_path),
        "--replays",
        str(replays),
        "--state-out",
        str(state_path),
    ]
    try:
        proc = subprocess.run(
            command,
            cwd=ctx.repo_root,
            env=env,
            capture_output=True,
            text=True,
        )
        returncode = proc.returncode
        stderr_tail = "\n".join(proc.stderr.splitlines()[-20:])
    except OSError as exc:
        returncode = None
        stderr_tail = str(exc)

    state = None
    rejects = None
    if returncode == 0 and state_path.exists():
        state = json.loads(state_path.read_text())
    if rejects_path.exists():
        rejects_record = json.loads(rejects_path.read_text())
        rejects = {
            "accepted": rejects_record.get("accepted"),
            "rejected": rejects_record.get("rejected"),
        }
    return {
        "state": state,
        "state_path": _relative(state_path, ctx),
        "rejects": rejects,
        "returncode": returncode,
        "stderr_tail": stderr_tail,
    }


def _state_diff(baseline: dict | None, candidate: dict | None) -> tuple[dict, list[str], list[str]]:
    if baseline is None or candidate is None:
        return {}, [], []
    baseline_vehicles = baseline.get("vehicles", {})
    candidate_vehicles = candidate.get("vehicles", {})
    missing = sorted(set(baseline_vehicles) - set(candidate_vehicles))
    extra = sorted(set(candidate_vehicles) - set(baseline_vehicles))
    differences = {}
    for vehicle_id in sorted(set(baseline_vehicles) & set(candidate_vehicles)):
        base = baseline_vehicles[vehicle_id]
        cand = candidate_vehicles[vehicle_id]
        field_diff = {
            field: {"baseline": base.get(field), "candidate": cand.get(field)}
            for field in sorted(set(base) | set(cand))
            if not _same_json_value(base.get(field), cand.get(field))
        }
        if field_diff:
            differences[vehicle_id] = field_diff
    return differences, missing, extra


def _run_scenario(ctx: RunContext, scenario: str) -> list[dict]:
    baseline_dir, baseline_source = _baseline(ctx, scenario)
    candidate_dir = ctx.output_dir(ctx.candidate, scenario)
    if not candidate_dir.exists():
        return [
            _make_check(
                "candidate_output",
                "fail",
                "Candidate output directory is missing.",
                scenario,
                ctx,
                baseline_source,
                baseline_dir,
                candidate_dir,
                {"candidate_dir_exists": False},
            ),
            *[
                _make_check(
                    name,
                    "skip",
                    "candidate output missing",
                    scenario,
                    ctx,
                    baseline_source,
                    baseline_dir,
                    candidate_dir,
                    {},
                )
                for name in (
                    "json_schema",
                    "required_fields",
                    "field_semantics",
                    "per_key_ordering",
                    "replay_idempotence",
                    "no_extra_outputs",
                    "consumer_state",
                )
            ],
        ]

    summary_path = candidate_dir / "summaries.jsonl"
    errors_path = candidate_dir / "errors.jsonl"
    candidate_entries, candidate_line_count, candidate_parse_errors = _read_jsonl(summary_path)
    candidate_raw = [record for _line, record in candidate_entries]
    candidate_records = [record for record in candidate_raw if isinstance(record, dict)]
    candidate_error_entries, error_line_count, error_parse_errors = _read_jsonl(errors_path)
    summary_schema = _load_schema(ctx, SUMMARY_SCHEMA)
    error_schema = _load_schema(ctx, ERROR_SCHEMA)
    schema_errors = _schema_errors(
        candidate_entries, summary_schema, "summaries.jsonl", candidate_parse_errors
    )
    schema_errors.extend(
        _schema_errors(candidate_error_entries, error_schema, "errors.jsonl", error_parse_errors)
    )
    candidate_summaries = _effective(candidate_records)
    baseline_entries, _, _ = _read_jsonl(baseline_dir / "summaries.jsonl")
    baseline_raw = [record for _line, record in baseline_entries]
    baseline_records = [record for record in baseline_raw if isinstance(record, dict)]
    baseline_summaries = _effective(baseline_records)
    shared_keys = set(baseline_summaries) & set(candidate_summaries)

    checks = [
        _make_check(
            "json_schema",
            "fail" if schema_errors else "pass",
            "Candidate summary and error records parse and validate against their schemas."
            if not schema_errors
            else "Candidate summary or error records do not all parse and validate against their schemas.",
            scenario,
            ctx,
            baseline_source,
            baseline_dir,
            candidate_dir,
            {
                "line_counts": {
                    "summaries.jsonl": candidate_line_count,
                    "errors.jsonl": error_line_count,
                },
                "errors": schema_errors,
            },
        )
    ]

    missing_fields = []
    for key in sorted(shared_keys):
        missing = sorted(set(baseline_summaries[key]) - set(candidate_summaries[key]))
        if missing:
            missing_fields.append({
                "vehicle_id": key[0],
                "session_id": key[1],
                "missing_fields": missing,
            })
    checks.append(_make_check(
        "required_fields",
        "fail" if missing_fields else "pass",
        "Candidate records preserve every field present in the corresponding baseline record."
        if not missing_fields
        else "Some candidate records omit fields present in their baseline records.",
        scenario,
        ctx,
        baseline_source,
        baseline_dir,
        candidate_dir,
        {"missing_fields": missing_fields},
    ))

    mismatches = []
    missing_keys = sorted(set(baseline_summaries) - set(candidate_summaries))
    for record in candidate_records:
        vehicle_id = record.get("vehicle_id")
        session_id = record.get("session_id")
        start = _parse_timestamp(record.get("start_ts"))
        end = _parse_timestamp(record.get("end_ts"))
        if start is None or end is None or end < start:
            mismatches.append({
                "vehicle_id": vehicle_id,
                "session_id": session_id,
                "field": "end_ts",
                "baseline": "valid timestamp not earlier than start_ts",
                "candidate": record.get("end_ts"),
            })
        if record.get("completion_reason") not in ("COMPLETE", "INACTIVITY_TIMEOUT"):
            mismatches.append({
                "vehicle_id": vehicle_id,
                "session_id": session_id,
                "field": "completion_reason",
                "baseline": ["COMPLETE", "INACTIVITY_TIMEOUT"],
                "candidate": record.get("completion_reason"),
            })
    for key in sorted(shared_keys):
        baseline_record = baseline_summaries[key]
        candidate_record = candidate_summaries[key]
        for field in SEMANTIC_FIELDS:
            baseline_value = baseline_record.get(field)
            candidate_value = candidate_record.get(field)
            if not _same_json_value(baseline_value, candidate_value):
                mismatches.append({
                    "vehicle_id": key[0],
                    "session_id": key[1],
                    "field": field,
                    "baseline": baseline_value,
                    "candidate": candidate_value,
                })
        for field in NUMERIC_FIELDS:
            baseline_value = baseline_record.get(field)
            candidate_value = candidate_record.get(field)
            if not _numeric_equal(baseline_value, candidate_value, ctx):
                mismatches.append({
                    "vehicle_id": key[0],
                    "session_id": key[1],
                    "field": field,
                    "baseline": baseline_value,
                    "candidate": candidate_value,
                })
    semantics_fail = bool(mismatches or missing_keys)
    checks.append(_make_check(
        "field_semantics",
        "fail" if semantics_fail else "pass",
        "Candidate fields preserve baseline semantics and intrinsic timestamp and completion rules."
        if not semantics_fail
        else "Candidate fields differ from baseline semantics or violate intrinsic summary rules.",
        scenario,
        ctx,
        baseline_source,
        baseline_dir,
        candidate_dir,
        {
            "mismatches": mismatches,
            "missing_keys": _keys_as_dict(missing_keys),
            "numeric_comparison": "exact" if ctx.strict else f"absolute tolerance {ctx.tolerance}",
        },
    ))

    baseline_order = _ordering(baseline_records, shared_keys)
    candidate_order = _ordering(candidate_records, shared_keys)
    ordering_violations = {
        vehicle_id: {
            "baseline": baseline_order.get(vehicle_id, []),
            "candidate": candidate_order.get(vehicle_id, []),
        }
        for vehicle_id in sorted(set(baseline_order) | set(candidate_order))
        if baseline_order.get(vehicle_id, []) != candidate_order.get(vehicle_id, [])
    }
    checks.append(_make_check(
        "per_key_ordering",
        "fail" if ordering_violations else "pass",
        "Per-vehicle session order matches the baseline; no global order is asserted."
        if not ordering_violations
        else "Per-vehicle session order differs from the baseline; no global order is asserted.",
        scenario,
        ctx,
        baseline_source,
        baseline_dir,
        candidate_dir,
        {"ordering_violations": ordering_violations},
    ))

    conflicting_reemissions, duplicate_emissions = _record_reemissions(candidate_records)
    replay_count = 0
    try:
        replay_count = int(ctx.scenario_meta(scenario).get("consumer_replays", 1))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        replay_count = 1
    replay_one = _consumer_run(ctx, scenario, "candidate-replay1", summary_path, 1)
    replay_two = _consumer_run(ctx, scenario, "candidate-replay2", summary_path, 2)
    replay_failures = [
        {
            "side": name,
            "returncode": result["returncode"],
            "stderr_tail": result["stderr_tail"],
        }
        for name, result in (("replay1", replay_one), ("replay2", replay_two))
        if result["returncode"] != 0
    ]
    replay_pass = (
        not conflicting_reemissions
        and not replay_failures
        and replay_one["state"] is not None
        and replay_two["state"] is not None
        and canonical(replay_one["state"]) == canonical(replay_two["state"])
    )
    checks.append(_make_check(
        "replay_idempotence",
        "pass" if replay_pass else "fail",
        "Candidate state is unchanged by a second delivery and repeated keys have identical records."
        if replay_pass
        else "Candidate replays change state, fail to run, or contain conflicting repeated keys.",
        scenario,
        ctx,
        baseline_source,
        baseline_dir,
        candidate_dir,
        {
            "state_paths": {
                "replay1": replay_one["state_path"],
                "replay2": replay_two["state_path"],
            },
            "conflicting_reemissions": conflicting_reemissions,
            "duplicate_emissions": duplicate_emissions,
            "consumer_failures": replay_failures,
        },
    ))

    extra_keys = sorted(set(candidate_summaries) - set(baseline_summaries))
    summary_properties = set(summary_schema.get("properties", {}))
    extra_fields = []
    for record in candidate_records:
        key = _valid_key(record)
        if key is None:
            continue
        allowed = set(baseline_summaries[key]) if key in baseline_summaries else summary_properties
        fields = sorted(set(record) - allowed)
        if fields:
            extra_fields.append({
                "vehicle_id": key[0],
                "session_id": key[1],
                "extra_fields": fields,
            })
    no_extra_fail = bool(extra_keys or extra_fields)
    checks.append(_make_check(
        "no_extra_outputs",
        "fail" if no_extra_fail else "pass",
        "Candidate output contains no new keys or unapproved fields."
        if not no_extra_fail
        else "Candidate output contains new keys or fields not present in the baseline contract.",
        scenario,
        ctx,
        baseline_source,
        baseline_dir,
        candidate_dir,
        {"extra_keys": _keys_as_dict(extra_keys), "extra_fields": extra_fields},
    ))

    baseline_run = _consumer_run(
        ctx, scenario, "baseline", baseline_dir / "summaries.jsonl", replay_count
    )
    candidate_run = _consumer_run(
        ctx, scenario, "candidate", summary_path, replay_count
    )
    state_diff, missing_vehicles, extra_vehicles = _state_diff(
        baseline_run["state"], candidate_run["state"]
    )
    baseline_matches_fixture = None
    if baseline_source == "expected" and baseline_run["state"] is not None:
        fixture_state_path = ctx.expected_dir(scenario) / "consumer_state.json"
        fixture_state = json.loads(fixture_state_path.read_text())
        baseline_matches_fixture = canonical(baseline_run["state"]) == canonical(fixture_state)
    consumer_failures = [
        {
            "side": side,
            "returncode": result["returncode"],
            "stderr_tail": result["stderr_tail"],
        }
        for side, result in (("baseline", baseline_run), ("candidate", candidate_run))
        if result["returncode"] != 0
    ]
    state_equal = (
        baseline_run["state"] is not None
        and candidate_run["state"] is not None
        and canonical(baseline_run["state"]) == canonical(candidate_run["state"])
    )
    consumer_pass = (
        state_equal
        and not consumer_failures
        and (baseline_matches_fixture is not False)
    )
    consumer_evidence = {
        "diff": state_diff,
        "missing_vehicles": missing_vehicles,
        "extra_vehicles": extra_vehicles,
        "rejects": {
            "baseline": baseline_run["rejects"],
            "candidate": candidate_run["rejects"],
        },
        "state_paths": {
            "baseline": baseline_run["state_path"],
            "candidate": candidate_run["state_path"],
        },
        "consumer_failures": consumer_failures,
    }
    if baseline_source == "expected":
        consumer_evidence["baseline_matches_fixture"] = baseline_matches_fixture
    checks.append(_make_check(
        "consumer_state",
        "pass" if consumer_pass else "fail",
        "Baseline and candidate produce identical consumer application state."
        if consumer_pass
        else "Baseline and candidate consumer application states differ or a consumer run failed.",
        scenario,
        ctx,
        baseline_source,
        baseline_dir,
        candidate_dir,
        consumer_evidence,
    ))
    return checks


def run_gate(ctx: RunContext) -> dict:
    checks = []
    for scenario in ctx.scenarios:
        checks.extend(_run_scenario(ctx, scenario))
    return write_gate(ctx, gate_result("contract", checks))
