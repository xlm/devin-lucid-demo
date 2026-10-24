import json
import sys

import yaml

from verification.common import REPO_ROOT, RunContext
from verification.diff_harness.runs import baseline_selection, run_record
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
    path = output_dir / name
    with path.open("a", encoding="utf-8") as stream:
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


def override(tmp_path, *, candidate="good", legacy=None, candidate_build=None,
             legacy_build=None, legacy_requires=None, candidate_command=None):
    processors = {
        "candidate": {
            "build": candidate_build or [sys.executable, "-c", "pass"],
            "command": candidate_command or [sys.executable, str(write_script(tmp_path, candidate))],
        },
    }
    if legacy is not None or legacy_requires is not None:
        entry = {
            "build": legacy_build or [sys.executable, "-c", "pass"],
            "command": [sys.executable, str(write_script(tmp_path, legacy or "good"))],
        }
        if legacy_requires is not None:
            entry["requires"] = legacy_requires
        processors["legacy"] = entry
    (tmp_path / "processors.override.yaml").write_text(
        yaml.safe_dump({"processors": processors}))


def context(tmp_path, scenarios, *, baseline="expected", skip_build=False):
    return RunContext(
        repo_root=REPO_ROOT,
        run_dir=tmp_path,
        scenarios=scenarios,
        baseline=baseline,
        skip_build=skip_build,
    )


def test_run_records_restart_commands_logs_and_checkpoint(tmp_path):
    override(tmp_path)
    ctx = context(tmp_path, ["normal_session", "processor_restart"])
    run_all(ctx)
    record = run_record(ctx, "candidate", "normal_session")
    assert record["mode"] == "single"
    assert len(record["commands"]) == 1
    assert record["exit_codes"] == [0]
    assert record["error"] is None
    assert record["checkpoint_written"] is None
    restart = run_record(ctx, "candidate", "processor_restart")
    assert restart["mode"] == "restart"
    assert len(restart["commands"]) == 2
    assert restart["exit_codes"] == [0, 0]
    assert restart["checkpoint_written"] is True
    assert "--stop-after" in restart["commands"][0]
    assert "--checkpoint" in restart["commands"][0]
    assert "--resume-from" in restart["commands"][1]
    assert restart["commands"][0][restart["commands"][0].index("--output-dir") + 1] == \
        restart["commands"][1][restart["commands"][1].index("--output-dir") + 1]
    checkpoint = tmp_path / "checkpoints" / "candidate" / "processor_restart.ckpt"
    assert json.loads(checkpoint.read_text()) == {"offset": 9}
    log = ctx.output_dir("candidate", "normal_session") / "processor.log"
    assert "replayed" in log.read_text()


def test_restart_without_checkpoint_records_error_and_skips_resume(tmp_path):
    override(tmp_path, candidate="no_checkpoint")
    ctx = context(tmp_path, ["processor_restart"])
    run_all(ctx)
    record = run_record(ctx, "candidate", "processor_restart")
    assert record["error"] == "checkpoint not written after --stop-after"
    assert len(record["commands"]) == 1
    assert record["checkpoint_written"] is False


def test_outputs_are_cleaned_between_runs_and_skipped_build_is_recorded(tmp_path):
    override(tmp_path)
    ctx = context(tmp_path, ["normal_session"])
    run_all(ctx)
    run_all(ctx)
    summaries = (ctx.output_dir("candidate", "normal_session")
                 / "summaries.jsonl").read_text().splitlines()
    expected = (REPO_ROOT / "fixtures" / "expected" / "normal_session"
                / "summaries.jsonl").read_text().splitlines()
    assert summaries == expected

    skipped_dir = tmp_path / "skip-build"
    skipped_dir.mkdir()
    override(skipped_dir)
    skipped = context(skipped_dir, ["normal_session"], skip_build=True)
    run_all(skipped)
    build = json.loads((skipped_dir / "outputs" / "candidate" / "build.json").read_text())
    assert build["status"] == "skipped"
    assert build["exit_code"] is None
    assert run_record(skipped, "candidate", "normal_session")["exit_codes"] == [0]


def test_candidate_build_failure_stops_runs_and_both_gates_error(tmp_path):
    override(tmp_path, candidate_build=[sys.executable, "-c", "raise SystemExit(1)"])
    ctx = context(tmp_path, ["normal_session"])
    run_all(ctx)
    build = json.loads((tmp_path / "outputs" / "candidate" / "build.json").read_text())
    assert build["status"] == "failed"
    assert not ctx.output_dir("candidate", "normal_session").exists()


def test_legacy_build_failure_falls_back_to_expected(tmp_path):
    override(tmp_path, legacy="good",
             legacy_build=[sys.executable, "-c", "raise SystemExit(1)"])
    ctx = context(tmp_path, ["normal_session"], baseline="legacy")
    run_all(ctx)
    unavailable = json.loads((tmp_path / "outputs" / "legacy" / "UNAVAILABLE.json").read_text())
    assert unavailable["fallback_baseline"] == "expected"
    assert not ctx.output_dir("legacy", "normal_session").exists()
    selection = baseline_selection(ctx)
    assert selection["used"] == "expected"


def test_missing_required_tool_marks_legacy_unavailable(tmp_path):
    override(tmp_path, legacy="good", legacy_requires=["definitely-not-a-real-tool-xyz"])
    ctx = context(tmp_path, ["normal_session"], baseline="legacy")
    run_all(ctx)
    unavailable = json.loads((tmp_path / "outputs" / "legacy" / "UNAVAILABLE.json").read_text())
    assert "definitely-not-a-real-tool-xyz" in unavailable["reason"]
    assert baseline_selection(ctx)["used"] == "expected"


def test_missing_command_is_recorded_without_raising(tmp_path):
    missing = tmp_path / "not-a-command"
    override(tmp_path, candidate_command=[str(missing)])
    ctx = context(tmp_path, ["normal_session"])
    run_all(ctx)
    record = run_record(ctx, "candidate", "normal_session")
    assert record["exit_codes"] == [None]
    assert "launch failed: FileNotFoundError" in record["error"]


def test_timeout_and_nonzero_exit_are_recorded(tmp_path, monkeypatch):
    import verification.runner as runner

    monkeypatch.setattr(runner, "RUN_TIMEOUT_S", 1)
    override(tmp_path, candidate="sleepy")
    ctx = context(tmp_path, ["normal_session"])
    run_all(ctx)
    timed_out = run_record(ctx, "candidate", "normal_session")
    assert timed_out["exit_codes"] == [None]
    assert timed_out["error"] == "timeout after 1 s"

    crash_dir = tmp_path / "crash"
    crash_dir.mkdir()
    override(crash_dir, candidate="crash")
    crashed = context(crash_dir, ["normal_session"])
    run_all(crashed)
    crash_record = run_record(crashed, "candidate", "normal_session")
    assert crash_record["exit_codes"] == [1]
    assert crash_record["error"] is None
