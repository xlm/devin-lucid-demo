from __future__ import annotations

import os

import io
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import yaml

from verification import common
from verification.common import REPO_ROOT, RunContext
from verification.cutover import commands
from verification.cutover import main
from verification.cutover.lease import read_lease
from verification.cutover.state_machine import TRANSITIONS, get_state, initialize_state, transition
from verification.cutover.storage import read_audit, sha256_file
from verification.cutover.topic import append_summary


def make_context(tmp_path: Path, run_dir: Path | None = None) -> RunContext:
    return RunContext(repo_root=REPO_ROOT, run_dir=run_dir or tmp_path / "run")


def fake_registry(tmp_path: Path) -> Path:
    script = tmp_path / "fake_processor.py"
    expected_root = REPO_ROOT / "fixtures" / "expected"
    script.write_text(
        "\n".join(
            [
                "import argparse",
                "import shutil",
                "from pathlib import Path",
                "",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('mode')",
                "parser.add_argument('--input', required=True)",
                "parser.add_argument('--output-dir', required=True)",
                "args = parser.parse_args()",
                "scenario = Path(args.input).parent.name",
                f"expected = Path({str(expected_root)!r}) / scenario",
                "output = Path(args.output_dir)",
                "output.mkdir(parents=True, exist_ok=True)",
                "for name in ('summaries.jsonl', 'errors.jsonl'):",
                "    shutil.copyfile(expected / name, output / name)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    registry = tmp_path / "processors.yaml"
    command = [sys.executable, str(script)]
    registry.write_text(
        yaml.safe_dump({"processors": {
            "legacy": {"command": command},
            "candidate": {"command": command},
        }}),
        encoding="utf-8",
    )
    return registry


def prepare_shadow_state(state_dir: Path) -> None:
    initialize_state(state_dir)
    transition(state_dir, "start-candidate")
    transition(state_dir, "shadow", details={"scenario_inputs": {}})


def write_gates(run_dir: Path, *, failing: str | None = None, missing: str | None = None) -> None:
    gates = run_dir / "gates"
    gates.mkdir(parents=True, exist_ok=True)
    for gate in ("behavioural", "contract", "privacy", "state_ordering"):
        if gate == missing:
            continue
        (gates / f"{gate}.json").write_text(
            json.dumps({"gate": gate, "status": "fail" if gate == failing else "pass"}) + "\n",
            encoding="utf-8",
        )


def write_context(run_dir: Path, *, git_sha: str = "test-head", git_dirty: bool = False) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "context.json").write_text(
        json.dumps({"git_sha": git_sha, "git_dirty": git_dirty}) + "\n",
        encoding="utf-8",
    )


def prepare_ready_state(tmp_path: Path, monkeypatch) -> tuple[RunContext, Path, str]:
    run_dir = tmp_path / "run"
    write_gates(run_dir)
    write_context(run_dir)
    evidence = run_dir / "evidence" / "evidence.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{"evidence":"ready"}\n', encoding="utf-8")
    monkeypatch.setattr(common, "git_sha", lambda _root: "test-head")
    monkeypatch.setattr(common, "git_dirty", lambda _root: False)

    ctx = make_context(tmp_path, run_dir)
    state_dir = tmp_path / "state"
    prepare_shadow_state(state_dir)
    assert main(ctx, ["evaluate", "--state-dir", str(state_dir)]) == 0
    return ctx, state_dir, sha256_file(evidence)


def _append_concurrent_batch(args) -> int:
    state_dir, worker, count = args
    accepted = 0
    for offset in range(count):
        index = worker * count + offset
        record = {"vehicle_id": f"VEH-{index:04d}", "session_id": f"S-{index}"}
        accepted += append_summary(
            Path(state_dir),
            producer="legacy",
            fencing_token=1,
            scenario="normal_session",
            input_sha256="a" * 64,
            record=record,
        )
    return accepted


def test_transition_table_covers_every_legal_edge(tmp_path):
    state_dir = tmp_path / "state"
    initialize_state(state_dir)
    visited = {(None, "init")}
    events = [
        "start-candidate",
        "shadow",
        "evaluate-fail",
        "rework",
        "shadow",
        "evaluate-pass",
        "approve",
        "cutover",
        "rollback",
        "restore",
    ]
    expected = [
        "CandidateDev",
        "ShadowValidation",
        "ValidationFailed",
        "CandidateDev",
        "ShadowValidation",
        "ReadyForReview",
        "ApprovedForCutover",
        "CandidateActive",
        "RolledBack",
        "LegacyActive",
    ]
    for event, target in zip(events, expected):
        visited.add((get_state(state_dir), event))
        assert transition(state_dir, event)[1] == target
    assert visited == set(TRANSITIONS)
    assert get_state(state_dir) == "LegacyActive"


def test_illegal_transition_is_rejected(tmp_path, capsys):
    ctx = make_context(tmp_path)
    state_dir = tmp_path / "state"
    initialize_state(state_dir)

    assert main(ctx, ["rollback", "--state-dir", str(state_dir)]) == 1
    assert "transition rejected: LegacyActive --rollback-->" in capsys.readouterr().err
    assert get_state(state_dir) == "LegacyActive"


def test_init_refuses_existing_state_unless_forced(tmp_path):
    ctx = make_context(tmp_path)
    state_dir = tmp_path / "state"
    assert main(ctx, ["init", "--state-dir", str(state_dir)]) == 0
    assert main(ctx, ["init", "--state-dir", str(state_dir)]) == 1
    assert main(ctx, ["init", "--force", "--state-dir", str(state_dir)]) == 0
    assert get_state(state_dir) == "LegacyActive"
    assert read_lease(state_dir) == {
        "holder": "legacy",
        "token": 1,
        "acquired_at": read_lease(state_dir)["acquired_at"],
    }


def test_shadow_isolated_and_audits_input_hashes(tmp_path):
    ctx = make_context(tmp_path)
    state_dir = tmp_path / "state"
    registry = fake_registry(tmp_path)
    initialize_state(state_dir)
    transition(state_dir, "start-candidate")

    assert main(
        ctx,
        ["shadow", "--scenario", "normal_session", "--registry", str(registry),
         "--state-dir", str(state_dir)],
    ) == 0
    assert get_state(state_dir) == "ShadowValidation"
    assert read_lease(state_dir)["holder"] == "legacy"
    assert (state_dir / "shadow" / "normal_session" / "summaries.jsonl").is_file()
    assert (state_dir / "topic.jsonl").read_text(encoding="utf-8") == ""
    shadow = next(entry for entry in read_audit(state_dir) if entry["event"] == "shadow")
    input_path = REPO_ROOT / "fixtures" / "scenarios" / "normal_session" / "input.jsonl"
    assert shadow["details"]["scenario_inputs"]["normal_session"]["sha256"] == sha256_file(input_path)


def test_evaluate_missing_gate_fails(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    write_gates(run_dir, missing="privacy")
    write_context(run_dir)
    monkeypatch.setattr(common, "git_sha", lambda _root: "test-head")
    monkeypatch.setattr(common, "git_dirty", lambda _root: False)
    ctx = make_context(tmp_path, run_dir)
    state_dir = tmp_path / "state"
    prepare_shadow_state(state_dir)

    assert main(ctx, ["evaluate", "--state-dir", str(state_dir)]) == 1
    assert get_state(state_dir) == "ValidationFailed"
    assert "missing gate: privacy" in capsys.readouterr().err


def test_evaluate_failing_gate_fails(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    write_gates(run_dir, failing="contract")
    write_context(run_dir)
    monkeypatch.setattr(common, "git_sha", lambda _root: "test-head")
    monkeypatch.setattr(common, "git_dirty", lambda _root: False)
    ctx = make_context(tmp_path, run_dir)
    state_dir = tmp_path / "state"
    prepare_shadow_state(state_dir)

    assert main(ctx, ["evaluate", "--state-dir", str(state_dir)]) == 1
    assert get_state(state_dir) == "ValidationFailed"
    assert "gate contract status is not pass" in capsys.readouterr().err


def test_evaluate_stale_git_sha_fails(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    write_gates(run_dir)
    write_context(run_dir, git_sha="stale-head")
    monkeypatch.setattr(common, "git_sha", lambda _root: "test-head")
    monkeypatch.setattr(common, "git_dirty", lambda _root: False)
    ctx = make_context(tmp_path, run_dir)
    state_dir = tmp_path / "state"
    prepare_shadow_state(state_dir)

    assert main(ctx, ["evaluate", "--state-dir", str(state_dir)]) == 1
    assert get_state(state_dir) == "ValidationFailed"
    assert "context git_sha does not match current HEAD" in capsys.readouterr().err


def test_evaluate_pass_records_gate_and_evidence_hashes(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    write_gates(run_dir)
    write_context(run_dir)
    evidence = run_dir / "evidence" / "evidence.json"
    evidence.parent.mkdir()
    evidence.write_text('{"evidence":"pass"}\n', encoding="utf-8")
    monkeypatch.setattr(common, "git_sha", lambda _root: "test-head")
    monkeypatch.setattr(common, "git_dirty", lambda _root: False)
    ctx = make_context(tmp_path, run_dir)
    state_dir = tmp_path / "state"
    prepare_shadow_state(state_dir)

    assert main(ctx, ["evaluate", "--state-dir", str(state_dir)]) == 0
    assert get_state(state_dir) == "ReadyForReview"
    entry = next(entry for entry in read_audit(state_dir) if entry["event"] == "evaluate-pass")
    assert set(entry["details"]["gate_sha256"]) == {
        "behavioural", "contract", "privacy", "state_ordering"
    }
    assert entry["details"]["evidence_sha256"] == sha256_file(evidence)


def test_approve_refuses_non_tty_and_wrong_hash(tmp_path, monkeypatch, capsys):
    ctx, state_dir, evidence_sha = prepare_ready_state(tmp_path, monkeypatch)
    monkeypatch.setattr(commands.sys, "stdin", io.StringIO(""))

    assert main(
        ctx,
        ["approve", "--approver", "reviewer", "--evidence-sha256", "0" * 64,
         "--state-dir", str(state_dir)],
    ) == 2
    assert "--evidence-sha256 does not match current evidence" in capsys.readouterr().err
    assert main(
        ctx,
        ["approve", "--approver", "reviewer", "--evidence-sha256", evidence_sha,
         "--state-dir", str(state_dir)],
    ) == 2
    captured = capsys.readouterr()
    assert "stdin must be a TTY" in captured.err
    assert "An agent with a PTY could bypass it" in captured.err
    assert get_state(state_dir) == "ReadyForReview"


def test_approve_refuses_changed_gate_hash(tmp_path, monkeypatch, capsys):
    ctx, state_dir, evidence_sha = prepare_ready_state(tmp_path, monkeypatch)
    (ctx.run_dir / "gates" / "contract.json").write_text('{"status":"fail"}\n', encoding="utf-8")
    monkeypatch.setattr(commands.sys, "stdin", io.StringIO(""))

    assert main(
        ctx,
        ["approve", "--approver", "reviewer", "--evidence-sha256", evidence_sha,
         "--state-dir", str(state_dir)],
    ) == 2
    assert "gate hash changed since evaluate: contract" in capsys.readouterr().err


def test_confirmation_phrase_requires_exact_ascii_text():
    expected = "APPROVE abcdef123456"
    assert commands.confirmation_matches(expected, expected)
    invalid = [
        "approve abcdef123456",
        "APPROVE abcdef123456 ",
        '"APPROVE abcdef123456"',
        "APPROVE abcdef12345٦",
        "APPROVEabcdef123456",
        "APPROVE abcdef123456!",
    ]
    assert all(not commands.confirmation_matches(value, expected) for value in invalid)


def test_fencing_rejects_stale_token_and_non_holder(tmp_path):
    state_dir = tmp_path / "state"
    initialize_state(state_dir)
    record = {"vehicle_id": "VEH-0001", "session_id": "S-1"}

    assert not append_summary(
        state_dir,
        producer="legacy",
        fencing_token=0,
        scenario="normal_session",
        input_sha256="a" * 64,
        record=record,
    )
    assert not append_summary(
        state_dir,
        producer="candidate",
        fencing_token=1,
        scenario="normal_session",
        input_sha256="a" * 64,
        record=record,
    )
    assert (state_dir / "topic.jsonl").read_text(encoding="utf-8") == ""
    assert [entry["event"] for entry in read_audit(state_dir)].count("produce-fenced") == 2


def test_produce_fences_legacy_and_accepts_candidate(tmp_path):
    ctx = make_context(tmp_path)
    state_dir = tmp_path / "state"
    registry = fake_registry(tmp_path)
    initialize_state(state_dir)
    for event in ("start-candidate", "shadow", "evaluate-pass", "approve"):
        transition(state_dir, event)
    assert main(ctx, ["cutover", "--state-dir", str(state_dir)]) == 0

    assert main(
        ctx,
        ["produce", "--as", "legacy", "--scenario", "normal_session",
         "--registry", str(registry), "--state-dir", str(state_dir)],
    ) == 1
    assert main(
        ctx,
        ["produce", "--as", "candidate", "--scenario", "normal_session",
         "--registry", str(registry), "--state-dir", str(state_dir)],
    ) == 0
    topic = [json.loads(line) for line in (state_dir / "topic.jsonl").read_text().splitlines()]
    assert topic
    assert all(entry["producer"] == "candidate" and entry["fencing_token"] == 2 for entry in topic)
    assert any(entry["event"] == "produce-fenced" for entry in read_audit(state_dir))


def test_produce_audits_candidate_without_lease_history(tmp_path):
    ctx = make_context(tmp_path)
    state_dir = tmp_path / "state"
    registry = fake_registry(tmp_path)
    initialize_state(state_dir)

    assert main(
        ctx,
        ["produce", "--as", "candidate", "--scenario", "normal_session",
         "--registry", str(registry), "--state-dir", str(state_dir)],
    ) == 1
    fenced = [entry for entry in read_audit(state_dir) if entry["event"] == "produce-fenced"]
    assert len(fenced) == 1
    assert fenced[0]["details"]["input_sha256"] == sha256_file(
        REPO_ROOT / "fixtures" / "scenarios" / "normal_session" / "input.jsonl"
    )
    assert (state_dir / "topic.jsonl").read_text(encoding="utf-8") == ""


def test_rollback_restores_legacy_and_increments_token(tmp_path):
    ctx = make_context(tmp_path)
    state_dir = tmp_path / "state"
    initialize_state(state_dir)
    for event in ("start-candidate", "shadow", "evaluate-pass", "approve"):
        transition(state_dir, event)
    assert main(ctx, ["cutover", "--state-dir", str(state_dir)]) == 0
    assert main(ctx, ["rollback", "--state-dir", str(state_dir)]) == 0
    assert read_lease(state_dir)["holder"] == "legacy"
    assert read_lease(state_dir)["token"] == 3
    assert main(ctx, ["restore", "--state-dir", str(state_dir)]) == 0
    assert get_state(state_dir) == "LegacyActive"


def test_simulate_stops_before_approval_and_prints_command(tmp_path, capsys):
    ctx = make_context(tmp_path)
    state_dir = tmp_path / "sim-state"
    registry = fake_registry(tmp_path)

    assert main(
        ctx,
        ["simulate", "--scenario", "normal_session", "--registry", str(registry),
         "--state-dir", str(state_dir)],
    ) == 1
    output = capsys.readouterr().out
    assert "STOP: no approval was attempted." in output
    assert "cutover approve" in output
    assert get_state(state_dir) == "ValidationFailed"


def test_simulate_after_approval_runs_cutover_and_rollback(tmp_path):
    ctx = make_context(tmp_path)
    state_dir = tmp_path / "state"
    registry = fake_registry(tmp_path)
    initialize_state(state_dir)
    for event in ("start-candidate", "shadow", "evaluate-pass", "approve"):
        transition(state_dir, event)

    assert main(
        ctx,
        ["simulate", "--after-approval", "--scenario", "normal_session",
         "--registry", str(registry), "--state-dir", str(state_dir)],
    ) == 0
    assert get_state(state_dir) == "LegacyActive"
    assert read_lease(state_dir) == {
        "holder": "legacy",
        "token": 3,
        "acquired_at": read_lease(state_dir)["acquired_at"],
    }


def test_consume_reports_missing_consumer_package(tmp_path, capsys, monkeypatch):
    from verification.cutover import commands

    ctx = make_context(tmp_path)
    state_dir = tmp_path / "state"
    initialize_state(state_dir)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(tmp_path / "empty")
    monkeypatch.setattr(commands, "_consumer_env", lambda _ctx: env)

    assert main(ctx, ["consume", "--state-dir", str(state_dir)]) == 3
    assert "consumer-warranty not available at this revision" in capsys.readouterr().err


def test_concurrent_topic_appends_are_complete_and_lossless(tmp_path):
    state_dir = tmp_path / "state"
    initialize_state(state_dir)
    worker_count = 4
    records_per_worker = 15
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        accepted = list(
            executor.map(
                _append_concurrent_batch,
                [(str(state_dir), worker, records_per_worker) for worker in range(worker_count)],
            )
        )

    lines = (state_dir / "topic.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    expected = worker_count * records_per_worker
    assert sum(accepted) == expected
    assert len(records) == expected
    assert len({record["record"]["vehicle_id"] for record in records}) == expected
    assert all(record["fencing_token"] == 1 for record in records)
    assert sum(entry["event"] == "topic-append" for entry in read_audit(state_dir)) == expected
