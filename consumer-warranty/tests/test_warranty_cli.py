import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PATH = ROOT / "consumer-warranty" / "src"


def run_cli(args):
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(PACKAGE_PATH), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, "-m", "warranty_consumer", *map(str, args)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def summary():
    return {
        "session_id": "S-0001",
        "vehicle_id": "VEH-0001",
        "start_ts": "2026-01-15T08:00:00.000Z",
        "end_ts": "2026-01-15T08:01:00.000Z",
        "total_energy_kwh": 1.5,
        "peak_power_kw": 150.0,
        "event_count": 1,
        "completion_reason": "COMPLETE",
        "schema_version": "1.0",
    }


def reject_json_constant(token):
    raise ValueError(token)


def test_cli_writes_valid_state_and_value_free_rejects(tmp_path):
    source = tmp_path / "summaries.jsonl"
    output = tmp_path / "nested" / "state.json"
    valid = summary()
    invalid = {**summary(), "vehicle_id": "WVW-SYNTHETIC-0001"}
    source.write_text(
        json.dumps(valid) + "\n" + json.dumps(invalid) + "\n[]\nnot-json\n"
    )

    result = run_cli(["--input", source, "--state-out", output, "--replays", "2"])

    assert result.returncode == 0, result.stderr
    state = json.loads(output.read_text())
    schema = json.loads(
        (ROOT / "contracts" / "warranty.consumer-state.v1.schema.json").read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(state)
    assert state["vehicles"]["VEH-0001"]["session_count"] == 1
    rejects_path = Path(str(output) + ".rejects.json")
    rejects = json.loads(rejects_path.read_text())
    assert rejects["accepted"] == 2
    assert rejects["rejected"] == 6
    assert rejects["replays"] == 2
    assert "WVW-SYNTHETIC-0001" not in rejects_path.read_text()
    assert "VEH-0001" not in result.stderr
    assert "session" not in result.stderr


def test_cli_rejects_overflowing_number_and_writes_strict_json(tmp_path):
    source = tmp_path / "summaries.jsonl"
    output = tmp_path / "state.json"
    line = json.dumps(summary()).replace(
        '"total_energy_kwh": 1.5',
        '"total_energy_kwh":1e400',
    )
    source.write_text(line + "\n")

    result = run_cli(["--input", source, "--state-out", output])

    assert result.returncode == 0, result.stderr
    state = json.loads(output.read_text(), parse_constant=reject_json_constant)
    assert state == {"schema_version": "1.0", "vehicles": {}}
    rejects_path = Path(str(output) + ".rejects.json")
    rejects = json.loads(
        rejects_path.read_text(),
        parse_constant=reject_json_constant,
    )
    assert rejects["accepted"] == 0
    assert rejects["rejected"] == 1
    assert rejects["reasons"] == [{
        "line": 1,
        "delivery": 1,
        "path": "total_energy_kwh",
        "validator": "finite",
    }]


def test_cli_replays_one_two_and_three_produce_same_state(tmp_path):
    source = tmp_path / "summaries.jsonl"
    source.write_text(json.dumps(summary()) + "\n")
    states = []

    for replays in (1, 2, 3):
        output = tmp_path / f"state-{replays}.json"
        result = run_cli([
            "--input", source, "--state-out", output, "--replays", replays,
        ])
        assert result.returncode == 0, result.stderr
        states.append(json.loads(output.read_text()))

    assert states[0] == states[1] == states[2]


@pytest.mark.parametrize("scenario", sorted(path.name for path in (ROOT / "fixtures" / "expected").iterdir()))
def test_cli_and_library_match_fixture_consumer_state(tmp_path, scenario):
    expected_dir = ROOT / "fixtures" / "expected" / scenario
    metadata = json.loads(
        (ROOT / "fixtures" / "scenarios" / scenario / "scenario.json").read_text()
    )
    output = tmp_path / f"{scenario}.json"

    result = run_cli([
        "--input", expected_dir / "summaries.jsonl",
        "--state-out", output,
        "--replays", metadata["consumer_replays"],
    ])

    assert result.returncode == 0, result.stderr
    expected = json.loads((expected_dir / "consumer_state.json").read_text())
    assert json.loads(output.read_text()) == expected


def test_cli_zero_replays_is_usage_error(tmp_path):
    source = tmp_path / "summaries.jsonl"
    source.write_text("")

    result = run_cli(["--input", source, "--state-out", tmp_path / "state.json", "--replays", 0])

    assert result.returncode == 2


def test_cli_missing_input_is_fatal(tmp_path):
    result = run_cli([
        "--input", tmp_path / "missing.jsonl", "--state-out", tmp_path / "state.json",
    ])

    assert result.returncode == 1
    assert "cannot read input file" in result.stderr
