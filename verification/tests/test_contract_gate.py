import json
import shutil

import pytest

from verification.common import REPO_ROOT, RunContext, all_scenarios
from verification.contract_checks import run_gate


def make_context(tmp_path, scenarios, *, baseline="expected", strict=False):
    return RunContext(
        run_dir=tmp_path,
        scenarios=list(scenarios),
        baseline=baseline,
        strict=strict,
    )


def prepare_candidate(ctx, scenarios):
    for scenario in scenarios:
        output = ctx.output_dir("candidate", scenario)
        output.mkdir(parents=True)
        expected = ctx.expected_dir(scenario)
        for filename in ("summaries.jsonl", "errors.jsonl"):
            shutil.copyfile(expected / filename, output / filename)


def read_records(path):
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def write_records(path, records):
    path.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records))


def checks_by_id(result, scenario=None):
    checks = result["checks"]
    if scenario is not None:
        checks = [item for item in checks if item["scenario"] == scenario]
    return {item["id"]: item for item in checks}


def derive_buggy_energy(scenario):
    grouped = {}
    seen = {}
    input_path = REPO_ROOT / "fixtures" / "scenarios" / scenario / "input.jsonl"
    for line in input_path.read_text().splitlines():
        event = json.loads(line)
        key = (event["vehicle_id"], event["session_id"])
        event_ids = seen.setdefault(key, set())
        if event["event_id"] in event_ids:
            continue
        event_ids.add(event["event_id"])
        readings = grouped.setdefault(key, [])
        readings.append(event["energy_delivered_kwh"])
    totals = {}
    for key, readings in grouped.items():
        total = 0.0
        previous = 0.0
        for energy in readings:
            total += max(0.0, energy - previous)
            previous = energy
        totals[key] = total
    return totals


def test_happy_path_passes_for_every_fixture_scenario(tmp_path):
    scenarios = all_scenarios()
    ctx = make_context(tmp_path, scenarios)
    prepare_candidate(ctx, scenarios)

    result = run_gate(ctx)

    assert result["status"] == "pass"
    assert all(item["status"] == "pass" for item in result["checks"])
    assert ctx.gate_path("contract").exists()
    assert all(item["verification"] in ("runtime", "static") for item in result["checks"])
    assert all(
        item["evidence"]["baseline_source"] == "expected"
        for item in result["checks"]
    )


@pytest.mark.parametrize(
    ("scenario", "vehicle_id"),
    [("out_of_order", "VEH-0007"), ("downstream_contract", "VEH-0040")],
)
def test_cumulative_energy_bug_is_detected_by_semantics_and_consumer(
    tmp_path, scenario, vehicle_id
):
    ctx = make_context(tmp_path, [scenario])
    prepare_candidate(ctx, [scenario])
    records_path = ctx.output_dir("candidate", scenario) / "summaries.jsonl"
    records = read_records(records_path)
    buggy_totals = derive_buggy_energy(scenario)
    expected_path = ctx.expected_dir(scenario) / "summaries.jsonl"
    expected_records = read_records(expected_path)
    differs = False
    for record in records:
        key = (record["vehicle_id"], record["session_id"])
        if key in buggy_totals:
            expected = next(
                item for item in expected_records
                if (item["vehicle_id"], item["session_id"]) == key
            )
            if buggy_totals[key] != expected["total_energy_kwh"]:
                differs = True
            record["total_energy_kwh"] = buggy_totals[key]
    assert differs
    write_records(records_path, records)

    result = run_gate(ctx)
    checks = checks_by_id(result, scenario)

    assert result["status"] == "fail"
    assert checks["contract.json_schema"]["status"] == "pass"
    semantics = checks["contract.field_semantics"]
    assert semantics["status"] == "fail"
    assert any(item["field"] == "total_energy_kwh" for item in semantics["evidence"]["mismatches"])
    consumer = checks["contract.consumer_state"]
    assert consumer["status"] == "fail"
    assert "warranty_energy_kwh" in consumer["evidence"]["diff"][vehicle_id]


def test_extra_field_fails_schema_and_no_extra_outputs(tmp_path):
    scenario = "normal_session"
    ctx = make_context(tmp_path, [scenario])
    prepare_candidate(ctx, [scenario])
    path = ctx.output_dir("candidate", scenario) / "summaries.jsonl"
    records = read_records(path)
    records[0]["unexpected"] = "synthetic"
    write_records(path, records)

    checks = checks_by_id(run_gate(ctx), scenario)

    assert checks["contract.json_schema"]["status"] == "fail"
    assert checks["contract.no_extra_outputs"]["status"] == "fail"


def test_dropped_record_fails_missing_key_and_consumer_state(tmp_path):
    scenario = "normal_session"
    ctx = make_context(tmp_path, [scenario])
    prepare_candidate(ctx, [scenario])
    path = ctx.output_dir("candidate", scenario) / "summaries.jsonl"
    write_records(path, read_records(path)[1:])

    checks = checks_by_id(run_gate(ctx), scenario)

    assert checks["contract.field_semantics"]["status"] == "fail"
    assert checks["contract.field_semantics"]["evidence"]["missing_keys"]
    assert checks["contract.consumer_state"]["status"] == "fail"


def test_identical_duplicate_emission_is_idempotent(tmp_path):
    scenario = "normal_session"
    ctx = make_context(tmp_path, [scenario])
    prepare_candidate(ctx, [scenario])
    path = ctx.output_dir("candidate", scenario) / "summaries.jsonl"
    path.write_text(path.read_text() + path.read_text().splitlines()[0] + "\n")

    checks = checks_by_id(run_gate(ctx), scenario)

    assert checks["contract.replay_idempotence"]["status"] == "pass"
    assert checks["contract.replay_idempotence"]["evidence"]["duplicate_emissions"] == 1
    assert checks["contract.consumer_state"]["status"] == "pass"
    assert checks["contract.no_extra_outputs"]["status"] == "pass"


def test_numeric_equivalent_reemission_is_idempotent(tmp_path):
    scenario = "normal_session"
    ctx = make_context(tmp_path, [scenario])
    prepare_candidate(ctx, [scenario])
    path = ctx.output_dir("candidate", scenario) / "summaries.jsonl"
    equivalent = read_records(path)[0]
    equivalent["event_count"] = float(equivalent["event_count"])
    path.write_text(path.read_text() + json.dumps(equivalent) + "\n")

    check = checks_by_id(run_gate(ctx), scenario)["contract.replay_idempotence"]

    assert check["status"] == "pass"
    assert check["evidence"]["duplicate_emissions"] >= 1


def test_within_vehicle_reordering_fails_but_cross_vehicle_order_is_ignored(tmp_path):
    scenario = "downstream_contract"
    ctx = make_context(tmp_path, [scenario])
    prepare_candidate(ctx, [scenario])
    path = ctx.output_dir("candidate", scenario) / "summaries.jsonl"
    records = read_records(path)
    index_a = next(i for i, item in enumerate(records) if item["session_id"] == "S-0040-A")
    index_b = next(i for i, item in enumerate(records) if item["session_id"] == "S-0040-B")
    records[index_a], records[index_b] = records[index_b], records[index_a]
    write_records(path, records)

    checks = checks_by_id(run_gate(ctx), scenario)

    assert checks["contract.per_key_ordering"]["status"] == "fail"
    assert checks["contract.consumer_state"]["status"] == "pass"

    records = read_records(ctx.expected_dir(scenario) / "summaries.jsonl")
    vehicle_index = next(i for i, item in enumerate(records) if item["vehicle_id"] == "VEH-0041")
    records.append(records.pop(vehicle_index))
    write_records(path, records)

    cross_key_checks = checks_by_id(run_gate(ctx), scenario)
    assert cross_key_checks["contract.per_key_ordering"]["status"] == "pass"


def test_conflicting_reemission_fails_replay_idempotence(tmp_path):
    scenario = "normal_session"
    ctx = make_context(tmp_path, [scenario])
    prepare_candidate(ctx, [scenario])
    path = ctx.output_dir("candidate", scenario) / "summaries.jsonl"
    conflicting = read_records(path)[0]
    conflicting["peak_power_kw"] += 1
    path.write_text(path.read_text() + json.dumps(conflicting) + "\n")

    check = checks_by_id(run_gate(ctx), scenario)["contract.replay_idempotence"]

    assert check["status"] == "fail"
    assert check["evidence"]["conflicting_reemissions"]


@pytest.mark.parametrize("strict, expected", [(False, "pass"), (True, "fail")])
def test_numeric_tolerance_and_strict_modes(tmp_path, strict, expected):
    scenario = "normal_session"
    ctx = make_context(tmp_path, [scenario], strict=strict)
    prepare_candidate(ctx, [scenario])
    path = ctx.output_dir("candidate", scenario) / "summaries.jsonl"
    records = read_records(path)
    records[0]["total_energy_kwh"] += 1e-9
    write_records(path, records)

    check = checks_by_id(run_gate(ctx), scenario)["contract.field_semantics"]

    assert check["status"] == expected


def test_integral_float_spelling_matches_parsed_value(tmp_path):
    scenario = "normal_session"
    ctx = make_context(tmp_path, [scenario], strict=True)
    prepare_candidate(ctx, [scenario])
    path = ctx.output_dir("candidate", scenario) / "summaries.jsonl"
    records = read_records(path)
    records[0]["event_count"] = float(records[0]["event_count"])
    write_records(path, records)

    checks = checks_by_id(run_gate(ctx), scenario)

    assert checks["contract.json_schema"]["status"] == "pass"
    assert checks["contract.field_semantics"]["status"] == "pass"


def test_legacy_baseline_is_selected_when_present_and_expected_is_fallback(tmp_path):
    scenario = "normal_session"
    ctx = make_context(tmp_path, [scenario], baseline="legacy")
    prepare_candidate(ctx, [scenario])
    legacy_dir = ctx.output_dir("legacy", scenario)
    legacy_dir.mkdir(parents=True)
    for filename in ("summaries.jsonl", "errors.jsonl"):
        expected_path = ctx.expected_dir(scenario) / filename
        assert expected_path.is_file()
        shutil.copyfile(expected_path, legacy_dir / filename)
        assert (legacy_dir / filename).is_file()
    legacy_records = read_records(legacy_dir / "summaries.jsonl")
    legacy_records[0]["total_energy_kwh"] = 999.0
    write_records(legacy_dir / "summaries.jsonl", legacy_records)

    checks = checks_by_id(run_gate(ctx), scenario)
    semantics = checks["contract.field_semantics"]
    assert semantics["evidence"]["baseline_source"] == "legacy"
    assert semantics["evidence"]["mismatches"][0]["baseline"] == 999.0

    fallback_ctx = make_context(tmp_path / "fallback", [scenario], baseline="legacy")
    prepare_candidate(fallback_ctx, [scenario])
    fallback = checks_by_id(run_gate(fallback_ctx), scenario)
    assert fallback["contract.field_semantics"]["evidence"]["baseline_source"] == "expected"
    assert all(
        (fallback_ctx.expected_dir(scenario) / filename).is_file()
        for filename in ("summaries.jsonl", "errors.jsonl")
    )


def test_missing_candidate_output_fails_and_skips_other_checks(tmp_path):
    scenario = "normal_session"
    ctx = make_context(tmp_path, [scenario])

    result = run_gate(ctx)
    checks = checks_by_id(result, scenario)

    assert result["status"] == "fail"
    assert checks["contract.candidate_output"]["status"] == "fail"
    assert all(
        checks[f"contract.{name}"]["status"] == "skip"
        for name in (
            "json_schema",
            "required_fields",
            "field_semantics",
            "per_key_ordering",
            "replay_idempotence",
            "no_extra_outputs",
            "consumer_state",
        )
    )


@pytest.mark.parametrize("filename", ("summaries.jsonl", "errors.jsonl"))
def test_missing_candidate_required_file_fails(tmp_path, filename):
    scenario = "normal_session"
    ctx = make_context(tmp_path, [scenario])
    prepare_candidate(ctx, [scenario])
    (ctx.output_dir("candidate", scenario) / filename).unlink()

    result = run_gate(ctx)
    checks = checks_by_id(result, scenario)

    assert result["status"] == "fail"
    assert checks["contract.candidate_output_files"]["status"] == "fail"
    assert checks["contract.candidate_output_files"]["evidence"]["missing_files"] == [
        filename
    ]
    assert checks["contract.json_schema"]["status"] == "skip"


def test_candidate_output_directory_does_not_satisfy_required_file(tmp_path):
    scenario = "normal_session"
    ctx = make_context(tmp_path, [scenario])
    prepare_candidate(ctx, [scenario])
    errors_path = ctx.output_dir("candidate", scenario) / "errors.jsonl"
    errors_path.unlink()
    errors_path.mkdir()

    check = checks_by_id(run_gate(ctx), scenario)["contract.candidate_output_files"]

    assert check["status"] == "fail"
    assert check["evidence"]["missing_files"] == ["errors.jsonl"]


def test_present_empty_errors_file_is_valid(tmp_path):
    scenario = "normal_session"
    ctx = make_context(tmp_path, [scenario])
    prepare_candidate(ctx, [scenario])
    errors_path = ctx.output_dir("candidate", scenario) / "errors.jsonl"

    assert errors_path.is_file()
    assert errors_path.read_text() == ""
    assert run_gate(ctx)["status"] == "pass"


def test_unparsable_candidate_line_fails_schema_without_crashing(tmp_path):
    scenario = "normal_session"
    ctx = make_context(tmp_path, [scenario])
    prepare_candidate(ctx, [scenario])
    path = ctx.output_dir("candidate", scenario) / "summaries.jsonl"
    path.write_text(path.read_text() + "not-json\n")

    result = run_gate(ctx)
    check = checks_by_id(result, scenario)["contract.json_schema"]

    assert result["status"] == "fail"
    assert check["status"] == "fail"
    assert any(item["validator"] == "json_parse" for item in check["evidence"]["errors"])
