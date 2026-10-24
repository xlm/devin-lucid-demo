import importlib.util
import json
import subprocess
import sys

import jsonschema

from verification.common import REPO_ROOT, load_jsonl

BATTERY_ROOT = REPO_ROOT / "fixtures" / "battery"
SCENARIOS = BATTERY_ROOT / "scenarios"
EXPECTED = BATTERY_ROOT / "expected"
SCHEMAS = {
    name: json.loads((REPO_ROOT / "contracts" / f"{name}.schema.json").read_text())
    for name in (
        "battery.telemetry.v1",
        "battery.health.daily.v1",
        "battery.errors.v1",
    )
}


def load_reference_model():
    path = BATTERY_ROOT / "tools" / "reference_model.py"
    spec = importlib.util.spec_from_file_location("battery_reference_model", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_battery_fixtures_match_generator():
    process = subprocess.run(
        [sys.executable, "fixtures/battery/tools/generate_fixtures.py", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stdout + process.stderr


def test_expected_outputs_satisfy_battery_schemas():
    for scenario in sorted(path for path in EXPECTED.iterdir() if path.is_dir()):
        for record in load_jsonl(scenario / "summaries.jsonl"):
            jsonschema.validate(record, SCHEMAS["battery.health.daily.v1"])
        for record in load_jsonl(scenario / "errors.jsonl"):
            jsonschema.validate(record, SCHEMAS["battery.errors.v1"])


def test_reference_model_accepted_inputs_satisfy_telemetry_schema():
    reference = load_reference_model()
    for scenario in sorted(path for path in SCENARIOS.iterdir() if path.is_dir()):
        for line in (scenario / "input.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                reference.validate(line)
            except reference.Invalid:
                continue
            jsonschema.validate(json.loads(line), SCHEMAS["battery.telemetry.v1"])


def test_expected_outputs_never_include_restricted_location_values():
    for scenario in sorted(path for path in EXPECTED.iterdir() if path.is_dir()):
        for kind in ("summaries.jsonl", "errors.jsonl"):
            for record in load_jsonl(scenario / kind):
                assert "synthetic_location" not in record
                assert all(
                    not (isinstance(value, str) and value.startswith("SYN-"))
                    for value in record.values()
                )


def test_imbalance_trap_uses_per_event_spread():
    records = load_jsonl(EXPECTED / "imbalance_trap" / "summaries.jsonl")
    summary = next(record for record in records if record["pack_id"] == "PK-0109-A")
    assert summary["max_cell_imbalance_v"] == 0.11999999999999966
    assert summary["max_cell_imbalance_v"] != summary["max_cell_voltage_v"] - summary["min_cell_voltage_v"]
