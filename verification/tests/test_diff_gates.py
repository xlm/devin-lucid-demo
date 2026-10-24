import sys

import yaml

from verification.common import REPO_ROOT, RunContext
from verification.diff_harness import run_gate as behavioural_gate
from verification.diff_harness.comparator import CATEGORIES
from verification.diff_harness.state_ordering import run_gate as state_gate
from verification.runner import run_all

FAKE_PROCESSOR = r'''
import json
import os
import sys
import time
from pathlib import Path

BEHAVIOR = Path(__file__).stem
arguments = sys.argv[1:]
if BEHAVIOR == "sleepy":
    time.sleep(5)
if BEHAVIOR == "crash":
    sys.exit(1)

input_path = Path(arguments[arguments.index("--input") + 1])
output_dir = Path(arguments[arguments.index("--output-dir") + 1])
scenario = input_path.parent.name
repo_root = input_path.parents[3]
expected_dir = repo_root / "fixtures" / "expected" / scenario
output_dir.mkdir(parents=True, exist_ok=True)

def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

def append_records(name, records):
    with (output_dir / name).open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")

summary_path = output_dir / "summaries.jsonl"
error_path = output_dir / "errors.jsonl"
summary_path.touch(exist_ok=True)
error_path.touch(exist_ok=True)
expected_summaries = read_jsonl(expected_dir / "summaries.jsonl")
expected_errors = read_jsonl(expected_dir / "errors.jsonl")

if "--stop-after" in arguments:
    if BEHAVIOR == "no_checkpoint":
        sys.stderr.write("ignored checkpoint\n")
        sys.exit(0)
    if BEHAVIOR == "dup_on_resume":
        append_records("summaries.jsonl", expected_summaries[:1])
    checkpoint = Path(arguments[arguments.index("--checkpoint") + 1])
    temporary = checkpoint.with_suffix(checkpoint.suffix + ".tmp")
    temporary.write_text(json.dumps({"offset": int(arguments[arguments.index("--stop-after") + 1])}))
    os.replace(temporary, checkpoint)
    sys.stderr.write("replayed records\n")
    sys.exit(0)

if BEHAVIOR == "wrong_energy":
    sums = {}
    for line in input_path.read_text().splitlines():
        try:
            event = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        vehicle_id = event.get("vehicle_id")
        session_id = event.get("session_id")
        energy = event.get("energy_delivered_kwh")
        if isinstance(vehicle_id, str) and isinstance(session_id, str) \
                and isinstance(energy, (int, float)) and not isinstance(energy, bool):
            key = (vehicle_id, session_id)
            sums[key] = sums.get(key, 0.0) + energy
    for record in expected_summaries:
        record["total_energy_kwh"] = sums.get(
            (record["vehicle_id"], record["session_id"]), record["total_energy_kwh"])

if BEHAVIOR == "many_extra":
    for index in range(25):
        expected_summaries.append({
            "session_id": f"S-EXTRA-{index:03d}",
            "vehicle_id": f"VEH-{9000 + index:04d}",
            "start_ts": "2026-01-15T08:00:00.000Z",
            "end_ts": "2026-01-15T08:00:00.000Z",
            "total_energy_kwh": 1.0,
            "peak_power_kw": 1.0,
            "event_count": 1,
            "completion_reason": "COMPLETE",
            "schema_version": "1.0",
        })

append_records("summaries.jsonl", expected_summaries)
append_records("errors.jsonl", expected_errors)
sys.stderr.write(f"replayed {len(input_path.read_text().splitlines())} records\n")
'''


def write_script(tmp_path, behavior):
    script = tmp_path / f"{behavior}.py"
    script.write_text(FAKE_PROCESSOR)
    return script


def configure(tmp_path, behavior="good", *, legacy=False, candidate_build=None,
              legacy_build=None):
    processors = {
        "candidate": {
            "build": candidate_build or [sys.executable, "-c", "pass"],
            "command": [sys.executable, str(write_script(tmp_path, behavior))],
        },
    }
    if legacy:
        processors["legacy"] = {
            "build": legacy_build or [sys.executable, "-c", "pass"],
            "command": [sys.executable, str(write_script(tmp_path, "good"))],
        }
    (tmp_path / "processors.override.yaml").write_text(
        yaml.safe_dump({"processors": processors}))


def context(tmp_path, scenarios, baseline="expected"):
    return RunContext(
        repo_root=REPO_ROOT,
        run_dir=tmp_path,
        scenarios=scenarios,
        baseline=baseline,
    )


def test_good_fakes_pass_both_gates_and_write_report(tmp_path):
    configure(tmp_path)
    ctx = context(tmp_path, [
        "normal_session", "processor_restart", "state_recovery",
        "partitioning_ordering", "duplicates",
    ])
    run_all(ctx)
    behavioral = behavioural_gate(ctx)
    state = state_gate(ctx)
    assert behavioral["status"] == "pass"
    assert state["status"] == "pass"
    report = tmp_path / "gates" / "behavioural.md"
    assert report.is_file()
    report_text = report.read_text()
    assert "What this gate does not assert" in report_text
    assert "\u2014" not in report_text
    assert all(item["verification"] == "runtime" for item in behavioral["checks"])
    assert all(item["category"] in CATEGORIES or item["category"] == "equivalence"
               for item in behavioral["checks"])
    assert any(item["id"] == "partitioning_ordering:comparator:cross_key_permutation_passes"
               and item["status"] == "pass" for item in state["checks"])
    assert any(item["id"] == "partitioning_ordering:comparator:within_key_swap_fails"
               and item["status"] == "pass" for item in state["checks"])


def test_wrong_energy_fails_aggregation_and_semantic_checks(tmp_path):
    configure(tmp_path, "wrong_energy")
    ctx = context(tmp_path, ["normal_session"])
    run_all(ctx)
    result = behavioural_gate(ctx)
    assert result["status"] == "fail"
    failed = {item["id"] for item in result["checks"] if item["status"] == "fail"}
    assert "normal_session:candidate_vs_expected:AGGREGATION_MISMATCH" in failed
    assert "normal_session:candidate_vs_expected:SEMANTIC_MISMATCH" in failed


def test_resume_duplicate_fails_state_gate(tmp_path):
    configure(tmp_path, "dup_on_resume")
    ctx = context(tmp_path, ["processor_restart"])
    run_all(ctx)
    result = state_gate(ctx)
    duplicate = next(item for item in result["checks"]
                     if item["id"] == "processor_restart:candidate:no_duplicate_after_resume")
    assert duplicate["status"] == "fail"
    assert result["status"] == "fail"


def test_missing_restart_checkpoint_fails_run_and_restart_checks(tmp_path):
    configure(tmp_path, "no_checkpoint")
    ctx = context(tmp_path, ["processor_restart"])
    run_all(ctx)
    behavioral = behavioural_gate(ctx)
    run_check = next(item for item in behavioral["checks"]
                     if item["id"] == "processor_restart:candidate:run")
    assert run_check["status"] == "fail"

    state = state_gate(ctx)
    restart_check = next(item for item in state["checks"]
                         if item["id"] == "processor_restart:candidate:restart_executed")
    assert restart_check["status"] == "fail"


def test_finding_evidence_is_bounded_to_twenty(tmp_path):
    configure(tmp_path, "many_extra")
    ctx = context(tmp_path, ["normal_session"])
    run_all(ctx)
    result = behavioural_gate(ctx)
    check = next(item for item in result["checks"]
                 if item["id"] == "normal_session:candidate_vs_expected:EXTRA_OUTPUT")
    assert check["evidence"]["findings_total"] == 25
    assert len(check["evidence"]["findings"]) == 20


def test_candidate_build_failure_returns_error_from_both_gates(tmp_path):
    configure(tmp_path, candidate_build=[sys.executable, "-c", "raise SystemExit(1)"])
    ctx = context(tmp_path, ["normal_session"])
    run_all(ctx)
    assert behavioural_gate(ctx)["status"] == "error"
    assert state_gate(ctx)["status"] == "error"


def test_legacy_fallback_is_reported_and_available_pairs_run(tmp_path):
    configure(tmp_path, legacy=True,
              legacy_build=[sys.executable, "-c", "raise SystemExit(1)"])
    fallback_ctx = context(tmp_path, ["normal_session"], baseline="legacy")
    run_all(fallback_ctx)
    fallback = behavioural_gate(fallback_ctx)
    selection = next(item for item in fallback["checks"]
                     if item["id"] == "baseline.selection")
    assert selection["evidence"]["used"] == "expected"

    legacy_dir = tmp_path / "legacy-good"
    legacy_dir.mkdir()
    configure(legacy_dir, legacy=True)
    legacy_ctx = context(legacy_dir, ["normal_session"], baseline="legacy")
    run_all(legacy_ctx)
    result = behavioural_gate(legacy_ctx)
    assert result["status"] == "pass"
    ids = {item["id"] for item in result["checks"]}
    assert "normal_session:legacy_vs_expected:AGGREGATION_MISMATCH" in ids
    assert "normal_session:candidate_vs_legacy:AGGREGATION_MISMATCH" in ids


def test_timeout_fails_candidate_run_gate_check(tmp_path, monkeypatch):
    import verification.runner as runner

    monkeypatch.setattr(runner, "RUN_TIMEOUT_S", 1)
    configure(tmp_path, "sleepy")
    ctx = context(tmp_path, ["normal_session"])
    run_all(ctx)
    result = behavioural_gate(ctx)
    run_check = next(item for item in result["checks"]
                     if item["id"] == "normal_session:candidate:run")
    assert run_check["status"] == "fail"
