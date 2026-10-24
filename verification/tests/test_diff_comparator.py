import json

from verification.common import REPO_ROOT
from verification.diff_harness.comparator import (
    CATEGORIES,
    Outputs,
    compare,
    load_outputs,
)

START = "2026-01-15T08:00:00.000Z"
END = "2026-01-15T08:01:00.000Z"


def summary(vehicle="VEH-0001", session="S-1", **updates):
    record = {
        "session_id": session,
        "vehicle_id": vehicle,
        "start_ts": START,
        "end_ts": END,
        "total_energy_kwh": 26.0,
        "peak_power_kw": 52.5,
        "event_count": 2,
        "completion_reason": "COMPLETE",
        "schema_version": "1.0",
    }
    record.update(updates)
    return record


def error(vehicle="VEH-0001", session="S-1", **updates):
    record = {
        "error_type": "INVALID_FIELD",
        "field": "battery_soc",
        "input_line": 2,
        "event_id": "EVT-0001",
        "vehicle_id": vehicle,
        "session_id": session,
        "schema_version": "1.0",
    }
    record.update(updates)
    return record


def categories(findings):
    return [finding.category for finding in findings]


def test_identical_outputs_and_cross_key_permutations_have_no_findings():
    baseline = Outputs(
        summaries=[summary("VEH-0001", "S-1"), summary("VEH-0002", "S-2")],
        errors=[error("VEH-0001", "S-1"), error("VEH-0002", "S-2",
                                                    input_line=3, event_id="EVT-0002")],
    )
    assert compare(baseline, baseline) == []
    permuted = Outputs(
        summaries=list(reversed(baseline.summaries)),
        errors=list(reversed(baseline.errors)),
    )
    assert compare(baseline, permuted) == []


def test_within_key_swap_is_reported():
    first = summary(session="S-1", start_ts=START, end_ts=END)
    second = summary(session="S-1", start_ts=END, end_ts="2026-01-15T08:02:00.000Z")
    findings = compare(Outputs(summaries=[first, second]),
                       Outputs(summaries=[second, first]))
    assert "ORDERING_WITHIN_KEY" in categories(findings)


def test_missing_extra_and_duplicate_summaries():
    base = summary()
    assert "MISSING_OUTPUT" in categories(compare(
        Outputs(summaries=[base]), Outputs()))
    assert "EXTRA_OUTPUT" in categories(compare(
        Outputs(), Outputs(summaries=[base])))
    identical_duplicates = compare(Outputs(summaries=[base]),
                                   Outputs(summaries=[base, base]))
    assert "DUPLICATE_OUTPUT" in categories(identical_duplicates)
    assert "INVALID_STATE_TRANSITION" not in categories(identical_duplicates)
    differing_duplicates = compare(
        Outputs(summaries=[base]),
        Outputs(summaries=[base, summary(total_energy_kwh=51.0)]),
    )
    assert "DUPLICATE_OUTPUT" in categories(differing_duplicates)
    assert "INVALID_STATE_TRANSITION" in categories(differing_duplicates)


def test_aggregation_and_numeric_comparison_rules():
    base = summary()
    event_count = compare(Outputs(summaries=[base]),
                         Outputs(summaries=[summary(event_count=3)]))
    assert categories(event_count) == ["AGGREGATION_MISMATCH"]

    material = compare(Outputs(summaries=[base]),
                       Outputs(summaries=[summary(total_energy_kwh=51.0)]))
    assert categories(material) == ["NUMERIC_MISMATCH", "AGGREGATION_MISMATCH"]
    numeric = material[0].data
    assert numeric == {
        "field": "total_energy_kwh",
        "baseline": 26.0,
        "actual": 51.0,
        "abs_diff": 25.0,
        "tolerance": 1e-6,
        "strict": False,
        "mode": "tolerance",
    }
    assert "tolerance mode" in material[0].detail
    tiny = summary(total_energy_kwh=0.9999999999999999)
    base_one = summary(total_energy_kwh=1.0)
    assert compare(Outputs(summaries=[base_one]), Outputs(summaries=[tiny])) == []
    strict = compare(Outputs(summaries=[base_one]), Outputs(summaries=[tiny]), strict=True)
    assert categories(strict) == ["NUMERIC_MISMATCH"]
    assert strict[0].data["mode"] == "strict"
    assert "strict mode" in strict[0].detail
    integer_base = summary(total_energy_kwh=15)
    float_actual = summary(total_energy_kwh=15.0)
    assert compare(Outputs(summaries=[integer_base]),
                   Outputs(summaries=[float_actual]), strict=True) == []


def test_numeric_tolerance_boundary_and_windowing_fields():
    base = summary(total_energy_kwh=0.0)
    below = summary(total_energy_kwh=0.999999e-6)
    above = summary(total_energy_kwh=1.000001e-6)
    assert compare(Outputs(summaries=[base]), Outputs(summaries=[below])) == []
    assert "NUMERIC_MISMATCH" in categories(compare(
        Outputs(summaries=[base]), Outputs(summaries=[above])))
    changed = summary(start_ts="2026-01-15T07:59:00.000Z",
                      end_ts="2026-01-15T08:02:00.000Z",
                      completion_reason="INACTIVITY_TIMEOUT")
    window = compare(Outputs(summaries=[summary()]), Outputs(summaries=[changed]))
    assert [finding.data["field"] for finding in window
            if finding.category == "WINDOWING_MISMATCH"] == [
                "start_ts", "end_ts", "completion_reason",
            ]


def test_invalid_transition_rules_for_errors_and_closed_without_summary():
    baseline_error_only = Outputs(errors=[error()])
    actual_summary = compare(baseline_error_only, Outputs(summaries=[summary()]))
    assert "EXTRA_OUTPUT" in categories(actual_summary)
    assert "INVALID_STATE_TRANSITION" in categories(actual_summary)
    closed = error(error_type="SESSION_ALREADY_CLOSED")
    findings = compare(Outputs(), Outputs(errors=[closed]))
    assert "INVALID_STATE_TRANSITION" in categories(findings)


def test_actual_schema_errors_and_unparsable_lines_are_findings():
    invalid_records = [
        summary(extra="not allowed"),
        summary(vehicle="VIN-0001"),
        summary(total_energy_kwh="15"),
        summary(total_energy_kwh=True),
    ]
    findings = compare(Outputs(), Outputs(summaries=invalid_records,
                                          unparsable=[{"stream": "errors", "line": 7}]))
    schema_findings = [finding for finding in findings
                       if finding.category == "SCHEMA_INCOMPATIBLE"]
    assert len(schema_findings) == 5
    assert {finding.data.get("line") for finding in schema_findings
            if "line" in finding.data} == {7}


def test_error_path_multiset_and_per_key_order():
    first = error(input_line=2, event_id="EVT-1")
    second = error(input_line=3, event_id="EVT-2")
    missing_extra = compare(Outputs(errors=[first, second]),
                            Outputs(errors=[second, error(input_line=4, event_id="EVT-3")]))
    assert len([finding for finding in missing_extra
                if finding.category == "ERROR_PATH_MISMATCH"]) == 2
    repeated = compare(Outputs(errors=[first]), Outputs(errors=[first, first]))
    extra = next(finding for finding in repeated
                 if finding.category == "ERROR_PATH_MISMATCH")
    assert extra.data["direction"] == "extra"
    assert extra.data["count"] == 1
    changed = error(input_line=2, event_id="EVT-1", field="charging_state")
    field_change = compare(Outputs(errors=[first]), Outputs(errors=[changed]))
    assert len([finding for finding in field_change
                if finding.category == "ERROR_PATH_MISMATCH"]) == 2
    ordering = compare(Outputs(errors=[first, second]), Outputs(errors=[second, first]))
    assert "ORDERING_WITHIN_KEY" in categories(ordering)
    malformed_first = error(
        input_line=1, error_type="MALFORMED_JSON", field=None,
        event_id=None, vehicle=None, session=None,
    )
    malformed_second = error(
        input_line=2, error_type="MALFORMED_JSON", field=None,
        event_id=None, vehicle=None, session=None,
    )
    assert compare(Outputs(errors=[malformed_first, malformed_second]),
                   Outputs(errors=[malformed_second, malformed_first])) == []
    assert compare(Outputs(errors=[error(input_line=1)]),
                   Outputs(errors=[error(input_line=1.0)])) == []
    fractional_line = compare(
        Outputs(errors=[error(input_line=1)]),
        Outputs(errors=[error(input_line=1.5)]),
    )
    assert "ERROR_PATH_MISMATCH" in categories(fractional_line)


def input_event(event_id="EVT-0001", timestamp=START, energy=13.0, power=20.0,
                charging_state="CHARGING", vehicle="VEH-0001", session="S-1"):
    return {
        "event_id": event_id,
        "vehicle_id": vehicle,
        "session_id": session,
        "event_timestamp": timestamp,
        "energy_delivered_kwh": energy,
        "charging_power_kw": power,
        "charging_state": charging_state,
    }


def input_text(*records):
    return "\n".join(json.dumps(record) for record in records) + "\n"


def semantic_summary(**updates):
    return summary(**updates)


def semantic_findings(outputs, text):
    return [finding for finding in compare(outputs, outputs, input_text=text)
            if finding.category == "SEMANTIC_MISMATCH"]


def test_semantics_reject_sum_of_cumulative_energy_and_non_input_peak():
    text = input_text(
        input_event(timestamp=START, energy=13.0, power=20.0),
        input_event(event_id="EVT-0002", timestamp=END, energy=26.0, power=40.0,
                    charging_state="COMPLETE"),
    )
    outputs = Outputs(summaries=[semantic_summary(
        total_energy_kwh=39.0, peak_power_kw=41.0)])
    rules = {finding.data["rule"] for finding in semantic_findings(outputs, text)}
    assert "energy_is_input_reading" in rules
    assert "peak_power_is_input_reading" in rules


def test_semantics_check_window_completion_counts_and_key():
    records = [
        input_event(timestamp=START, energy=13.0, power=20.0),
        input_event(event_id="EVT-0002", timestamp=END, energy=26.0, power=40.0),
    ]
    outputs = Outputs(summaries=[semantic_summary(
        start_ts="2026-01-15T08:02:00.000Z",
        event_count=3,
        completion_reason="COMPLETE",
    )])
    rules = {finding.data["rule"] for finding in semantic_findings(
        outputs, input_text(*records))}
    assert {"start_ts_in_input", "complete_has_input_reading",
            "event_count_bounded_by_input"} <= rules
    missing_key = Outputs(summaries=[semantic_summary(vehicle="VEH-0099")])
    assert "key_in_input" in {
        finding.data["rule"] for finding in semantic_findings(
            missing_key, input_text(*records))
    }


def test_semantics_timestamp_window_requires_fixed_width_ascii_utc():
    valid = Outputs(summaries=[semantic_summary(
        start_ts=START, end_ts=START, event_count=1,
        total_energy_kwh=13.0, peak_power_kw=20.0,
        completion_reason="INACTIVITY_TIMEOUT",
    )])
    valid_rules = {finding.data["rule"] for finding in semantic_findings(
        valid, input_text(input_event(timestamp=START)))}
    assert "start_ts_in_input" not in valid_rules
    assert "end_ts_in_input" not in valid_rules

    invalid_timestamps = [
        START.replace("2", "\u0662", 1),
        START[:-1] + "+00:00",
        START[:-1] + "z",
        START.replace(".000Z", ".00Z"),
        START.replace("T", " "),
    ]
    for timestamp in invalid_timestamps:
        outputs = Outputs(summaries=[semantic_summary(
            start_ts=timestamp, end_ts=timestamp, event_count=1,
            total_energy_kwh=13.0, peak_power_kw=20.0,
            completion_reason="INACTIVITY_TIMEOUT",
        )])
        rules = {finding.data["rule"] for finding in semantic_findings(
            outputs, input_text(input_event(timestamp=timestamp)))}
        assert {"start_ts_in_input", "end_ts_in_input"} <= rules


def test_semantics_check_error_echo_and_input_line_range():
    text = input_text(input_event())
    errors = Outputs(errors=[
        error(input_line=1, event_id="EVT-WRONG"),
        error(input_line=2, event_id="EVT-0001"),
    ])
    rules = {finding.data["rule"] for finding in semantic_findings(errors, text)}
    assert "error_echo" in rules
    assert "error_input_line" in rules


def test_fixture_outputs_satisfy_semantics_even_in_strict_mode():
    for scenario_dir in sorted((REPO_ROOT / "fixtures" / "scenarios").iterdir()):
        expected = load_outputs(REPO_ROOT / "fixtures" / "expected" / scenario_dir.name)
        text = (scenario_dir / "input.jsonl").read_text()
        assert compare(expected, expected, input_text=text, strict=True) == [], scenario_dir.name


def test_load_outputs_tolerates_missing_bad_and_non_object_lines(tmp_path):
    assert load_outputs(tmp_path).missing_files == ["summaries.jsonl", "errors.jsonl"]
    (tmp_path / "summaries.jsonl").write_text('{"ok": true}\nNaN\n[]\n\n')
    (tmp_path / "errors.jsonl").write_text("")
    outputs = load_outputs(tmp_path)
    assert outputs.summaries == [{"ok": True}]
    assert outputs.unparsable == [
        {"stream": "summaries", "line": 2},
        {"stream": "summaries", "line": 3},
        {"stream": "summaries", "line": 4},
    ]
    assert outputs.missing_files == []


def test_missing_actual_file_and_blank_line_are_findings(tmp_path):
    missing = compare(Outputs(), Outputs(missing_files=["errors.jsonl"]))
    finding = next(item for item in missing if item.category == "MISSING_OUTPUT")
    assert finding.stream == "errors"
    assert finding.key is None
    assert finding.data["file"] == "errors.jsonl"
    assert compare(Outputs(missing_files=["errors.jsonl"]), Outputs()) == []

    first = summary("VEH-0001", "S-1")
    second = summary("VEH-0002", "S-2")
    (tmp_path / "summaries.jsonl").write_text(
        json.dumps(first) + "\n\n" + json.dumps(second) + "\n")
    (tmp_path / "errors.jsonl").write_text("")
    actual = load_outputs(tmp_path)
    findings = compare(Outputs(summaries=[first, second]), actual)
    assert any(
        item.category == "SCHEMA_INCOMPATIBLE"
        and item.stream == "summaries"
        and item.data["line"] == 2
        for item in findings
    )


def test_final_newline_does_not_add_an_unparsable_line(tmp_path):
    expected = summary()
    (tmp_path / "summaries.jsonl").write_text(json.dumps(expected) + "\n")
    (tmp_path / "errors.jsonl").write_text("")
    actual = load_outputs(tmp_path)
    assert actual.unparsable == []
    assert compare(Outputs(summaries=[expected]), actual) == []
