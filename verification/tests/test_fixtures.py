import json
import subprocess
import sys

import jsonschema
import pytest

from verification.common import REPO_ROOT, all_scenarios, load_jsonl

SCHEMAS = {name: json.loads((REPO_ROOT / "contracts" / f"{name}.schema.json").read_text())
           for name in ("charging.session.summary.v1", "charging.errors.v1",
                        "warranty.consumer-state.v1")}
REQUIRED = {"normal_session", "concurrent_sessions", "duplicates", "out_of_order",
            "late_events_window_boundary", "malformed_null", "schema_evolution",
            "processor_restart", "consumer_replay", "partitioning_ordering", "state_recovery",
            "numeric_precision", "downstream_contract"}


def test_fixtures_match_generator():
    proc = subprocess.run([sys.executable, "fixtures/tools/generate_fixtures.py", "--check"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_required_scenarios_present():
    assert REQUIRED <= set(all_scenarios())


@pytest.mark.parametrize("scenario", all_scenarios())
def test_expected_outputs_satisfy_contract_schemas(scenario):
    exp = REPO_ROOT / "fixtures" / "expected" / scenario
    for r in load_jsonl(exp / "summaries.jsonl"):
        jsonschema.validate(r, SCHEMAS["charging.session.summary.v1"])
    for r in load_jsonl(exp / "errors.jsonl"):
        jsonschema.validate(r, SCHEMAS["charging.errors.v1"])
    jsonschema.validate(json.loads((exp / "consumer_state.json").read_text()),
                        SCHEMAS["warranty.consumer-state.v1"])


def test_no_restricted_values_in_expected_outputs():
    for scenario in all_scenarios():
        exp = REPO_ROOT / "fixtures" / "expected" / scenario
        for name in ("summaries.jsonl", "errors.jsonl"):
            assert "SYN-" not in (exp / name).read_text()
