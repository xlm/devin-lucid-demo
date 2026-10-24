from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from verification import common
from verification.cutover.lease import (
    acquire_lease,
    handover_lease,
    producer_token,
    read_lease,
    release_lease,
)
from verification.cutover.state_machine import (
    InvalidTransition,
    get_state,
    initialize_state,
    target_state,
    transition,
)
from verification.cutover.storage import (
    CutoverError,
    locked,
    read_audit_unlocked,
    read_json,
    sha256_file,
)
from verification.cutover.topic import append_summary

GATES = ("behavioural", "contract", "privacy", "state_ordering")
GUARANTEE_BOUNDARY = (
    "Demo boundary: single host, single coordination file, flock-based. "
    "This is not a distributed lease or an atomic handover. A no-producer "
    "window can occur, and this simulator does not provide exactly-once."
)
APPROVAL_WARNING = (
    "Local demo guard only. An agent with a PTY could bypass it. "
    "Real approval belongs out of band, for example in a protected GitHub "
    "environment or a change ticket."
)


def confirmation_matches(answer: str, expected: str) -> bool:
    """Approval confirmation must be an exact, case-sensitive phrase."""
    return answer == expected


def _state_dir(ctx, value: str | None) -> Path:
    path = Path(value) if value else ctx.repo_root / ".cutover"
    if not path.is_absolute():
        path = ctx.repo_root / path
    return path.resolve()


def _run_dir(ctx, value: str | None) -> Path:
    path = Path(value) if value else ctx.run_dir
    if not path.is_absolute():
        path = ctx.repo_root / path
    return path.resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verification/cli.py cutover")
    parser.add_argument("--state-dir", default=None)
    commands = parser.add_subparsers(dest="command", required=True)

    def add_command(name: str, help_text: str):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--state-dir", default=argparse.SUPPRESS)
        return command

    init = add_command("init", "initialize state and the legacy lease")
    init.add_argument("--force", action="store_true")

    status = add_command("status", "show state, lease, and audit")
    status.add_argument("--json", action="store_true")

    add_command("start-candidate", "move from LegacyActive to CandidateDev")

    shadow = add_command("shadow", "run candidate in an isolated shadow directory")
    shadow.add_argument("--scenario", action="append")
    shadow.add_argument("--registry")

    evaluate = add_command("evaluate", "evaluate run gates and context")
    evaluate.add_argument("--run-dir")

    add_command("rework", "return a failed candidate to development")

    approve = add_command("approve", "request human cutover approval")
    approve.add_argument("--approver", required=True)
    approve.add_argument("--evidence-sha256", required=True)

    add_command("cutover", "handover the lease to the candidate")

    produce = add_command("produce", "run a processor and append summaries")
    produce.add_argument("--as", dest="producer", choices=("legacy", "candidate"), required=True)
    produce.add_argument("--scenario", required=True)
    produce.add_argument("--registry")

    add_command("rollback", "return the lease to legacy")
    add_command("restore", "restore the LegacyActive state")

    consume = add_command("consume", "run the downstream warranty consumer")
    consume.add_argument("--state-out")

    simulate = add_command("simulate", "walk through shadow and cutover")
    simulate.add_argument("--after-approval", action="store_true")
    simulate.add_argument("--scenario", default="normal_session")
    simulate.add_argument("--registry")
    return parser


def _scenario_input(ctx, scenario: str) -> Path:
    if scenario not in common.all_scenarios(ctx.repo_root):
        raise CutoverError(f"unknown scenario: {scenario}")
    path = ctx.scenario_dir(scenario) / "input.jsonl"
    if not path.is_file():
        raise CutoverError(f"scenario input is missing: {path}")
    return path


def _processor_command(ctx, processor: str, registry: str | None) -> list[str]:
    path = Path(registry) if registry else ctx.repo_root / "processors.yaml"
    if not path.is_absolute():
        path = ctx.repo_root / path
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        command = config["processors"][processor]["command"]
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise CutoverError(f"cannot load {processor} command from {path}: {exc}") from exc
    if isinstance(command, str):
        command = shlex.split(command)
    if not isinstance(command, list) or not command or not all(
        isinstance(part, str) and part for part in command
    ):
        raise CutoverError(f"invalid {processor} command in {path}")
    return command


def _run_processor(
    ctx,
    *,
    processor: str,
    scenario: str,
    output_dir: Path,
    registry: str | None,
) -> int:
    input_path = _scenario_input(ctx, scenario)
    output_dir.mkdir(parents=True, exist_ok=True)
    command = _processor_command(ctx, processor, registry) + [
        "replay",
        "--input",
        str(input_path),
        "--output-dir",
        str(output_dir),
    ]
    try:
        result = subprocess.run(command, cwd=ctx.repo_root, capture_output=True, text=True)
    except OSError as exc:
        print(f"cannot start {processor} processor: {exc}", file=sys.stderr)
        return 1
    if result.returncode:
        print(
            f"{processor} processor failed for {scenario} with exit code {result.returncode}",
            file=sys.stderr,
        )
    return result.returncode


def _cmd_init(args, ctx, state_dir: Path) -> int:
    state = initialize_state(state_dir, force=args.force)
    print(f"initialized {state_dir}: {state}, lease legacy token 1")
    return 0


def _cmd_status(args, state_dir: Path) -> int:
    with locked(state_dir):
        state = read_json(state_dir / "state.json")
        lease = read_json(state_dir / "lease.json")
        audit = read_audit_unlocked(state_dir)[-10:]
    result = {"state": state, "lease": lease, "last_audit": audit}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"state: {state['state']} (updated {state['updated_at']})")
        print(
            f"lease: holder={lease['holder']} token={lease['token']} "
            f"acquired_at={lease['acquired_at']}"
        )
        print("last audit:")
        for entry in audit:
            print(
                f"  {entry['ts']} {entry['event']} {entry['actor']} "
                f"{entry['from']} -> {entry['to']}"
            )
    return 0


def _cmd_start_candidate(state_dir: Path) -> int:
    _, target = transition(state_dir, "start-candidate")
    print(f"state: {target}")
    return 0


def _cmd_shadow(args, ctx, state_dir: Path) -> int:
    lease = read_lease(state_dir)
    if lease["holder"] != "legacy":
        raise CutoverError("legacy must hold the lease during shadow validation")
    scenarios = args.scenario or ctx.scenarios or common.all_scenarios(ctx.repo_root)
    inputs = {
        scenario: {
            "path": str(_scenario_input(ctx, scenario)),
            "sha256": sha256_file(_scenario_input(ctx, scenario)),
        }
        for scenario in scenarios
    }
    transition(state_dir, "shadow", details={"scenario_inputs": inputs})
    failures = []
    for scenario in scenarios:
        output_dir = state_dir / "shadow" / scenario
        if output_dir.exists():
            shutil.rmtree(output_dir)
        if _run_processor(
            ctx,
            processor="candidate",
            scenario=scenario,
            output_dir=output_dir,
            registry=args.registry,
        ):
            failures.append(scenario)
            continue
        print(f"shadowed {scenario} input_sha256={inputs[scenario]['sha256']}")
    if failures:
        print(f"shadow failed for: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("shadow outputs are isolated from topic.jsonl; legacy retains the lease")
    return 0


def _gate_evaluation(ctx, run_dir: Path) -> tuple[list[str], dict, str | None]:
    reasons = []
    hashes = {}
    gates_dir = run_dir / "gates"
    for gate in GATES:
        path = gates_dir / f"{gate}.json"
        if not path.is_file():
            reasons.append(f"missing gate: {gate}")
            continue
        hashes[gate] = sha256_file(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            reasons.append(f"invalid gate {gate}: {exc}")
            continue
        if not isinstance(data, dict) or data.get("status") != "pass":
            reasons.append(f"gate {gate} status is not pass")

    context_path = run_dir / "context.json"
    context = {}
    if not context_path.is_file():
        reasons.append("missing context.json")
    else:
        try:
            context = json.loads(context_path.read_text(encoding="utf-8"))
            if not isinstance(context, dict):
                raise ValueError("context is not a JSON object")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            reasons.append(f"invalid context.json: {exc}")
            context = {}

    current_sha = common.git_sha(ctx.repo_root)
    if context.get("git_sha") != current_sha or current_sha is None:
        reasons.append("context git_sha does not match current HEAD")
    if context.get("git_dirty") is not False:
        reasons.append("context git_dirty is not false")
    if common.git_dirty(ctx.repo_root):
        reasons.append("working tree is dirty")

    evidence_path = run_dir / "evidence" / "evidence.json"
    evidence_sha256 = sha256_file(evidence_path) if evidence_path.is_file() else None
    return reasons, hashes, evidence_sha256


def _cmd_evaluate(args, ctx, state_dir: Path) -> int:
    run_dir = _run_dir(ctx, args.run_dir)
    reasons, hashes, evidence_sha256 = _gate_evaluation(ctx, run_dir)
    event = "evaluate-fail" if reasons else "evaluate-pass"
    details = {
        "run_dir": str(run_dir),
        "gate_sha256": hashes,
        "evidence_sha256": evidence_sha256,
        "reasons": reasons,
    }
    _, target = transition(state_dir, event, details=details)
    if reasons:
        print(f"state: {target}", file=sys.stderr)
        for reason in reasons:
            print(f"- {reason}", file=sys.stderr)
        return 1
    print(f"state: {target}")
    print(f"gate_sha256: {json.dumps(hashes, sort_keys=True)}")
    if evidence_sha256:
        print(f"evidence_sha256: {evidence_sha256}")
    return 0


def _cmd_rework(state_dir: Path) -> int:
    _, target = transition(state_dir, "rework")
    print(f"state: {target}")
    return 0


def _latest_evaluation(state_dir: Path) -> dict | None:
    evaluations = [
        entry
        for entry in read_audit_unlocked(state_dir)
        if entry.get("event") in ("evaluate-pass", "evaluate-fail")
    ]
    return evaluations[-1] if evaluations else None


def _approval_reasons(ctx, run_dir: Path, state_dir: Path, supplied_hash: str) -> list[str]:
    with locked(state_dir):
        evaluation = _latest_evaluation(state_dir)
    if not evaluation or evaluation.get("event") != "evaluate-pass":
        return ["the most recent evaluation did not pass"]
    details = evaluation.get("details", {})
    if details.get("run_dir") != str(run_dir):
        return ["the current run directory differs from the evaluated run"]

    reasons = []
    expected_gates = details.get("gate_sha256", {})
    for gate in GATES:
        path = run_dir / "gates" / f"{gate}.json"
        if not path.is_file() or expected_gates.get(gate) != sha256_file(path):
            reasons.append(f"gate hash changed since evaluate: {gate}")

    evidence_path = run_dir / "evidence" / "evidence.json"
    if not evidence_path.is_file():
        reasons.append("current run has no evidence/evidence.json")
    else:
        current_evidence_sha = sha256_file(evidence_path)
        if details.get("evidence_sha256") != current_evidence_sha:
            reasons.append("evidence hash changed since evaluate")
        if supplied_hash != current_evidence_sha:
            reasons.append("--evidence-sha256 does not match current evidence")

    context_path = run_dir / "context.json"
    try:
        context = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        context = {}
    current_sha = common.git_sha(ctx.repo_root)
    if not isinstance(context, dict) or context.get("git_sha") != current_sha or current_sha is None:
        reasons.append("context git_sha no longer matches current HEAD")
    if not isinstance(context, dict) or context.get("git_dirty") is not False:
        reasons.append("context git_dirty is not false")
    if common.git_dirty(ctx.repo_root):
        reasons.append("working tree is dirty")
    return reasons


def _cmd_approve(args, ctx, state_dir: Path) -> int:
    if get_state(state_dir) != "ReadyForReview":
        transition(state_dir, "approve", actor=args.approver)
    run_dir = _run_dir(ctx, None)
    reasons = _approval_reasons(ctx, run_dir, state_dir, args.evidence_sha256)
    if reasons:
        for reason in reasons:
            print(reason, file=sys.stderr)
        print(APPROVAL_WARNING, file=sys.stderr)
        return 2
    if not sys.stdin.isatty():
        print("approval refused: stdin must be a TTY", file=sys.stderr)
        print(APPROVAL_WARNING, file=sys.stderr)
        return 2

    phrase = f"APPROVE {args.evidence_sha256[:12]}"
    print(APPROVAL_WARNING)
    try:
        answer = input(f"Type {phrase} to confirm: ")
    except EOFError:
        answer = ""
    if not confirmation_matches(answer, phrase):
        print("approval refused: confirmation phrase did not match", file=sys.stderr)
        return 2

    _, target = transition(
        state_dir,
        "approve",
        actor=args.approver,
        details={"approver": args.approver, "evidence_sha256": args.evidence_sha256},
    )
    print(f"state: {target}")
    return 0


def _cmd_cutover(state_dir: Path) -> int:
    current = get_state(state_dir)
    target_state(current, "cutover")
    lease = handover_lease(state_dir, "legacy", "candidate")
    _, target = transition(
        state_dir,
        "cutover",
        details={"holder": lease["holder"], "fencing_token": lease["token"]},
    )
    print(f"state: {target}, lease candidate token {lease['token']}")
    return 0


def _read_summaries(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, json.JSONDecodeError) as exc:
        raise CutoverError(f"cannot read processor summaries {path}: {exc}") from exc


def _cmd_produce(args, ctx, state_dir: Path) -> int:
    input_path = _scenario_input(ctx, args.scenario)
    token = producer_token(state_dir, args.producer)
    if token is None:
        token = read_lease(state_dir)["token"]
    with tempfile.TemporaryDirectory(prefix="produce-", dir=state_dir) as temp_name:
        output_dir = Path(temp_name)
        if _run_processor(
            ctx,
            processor=args.producer,
            scenario=args.scenario,
            output_dir=output_dir,
            registry=args.registry,
        ):
            return 1
        summaries_path = output_dir / "summaries.jsonl"
        if not summaries_path.is_file():
            print("processor did not produce summaries.jsonl", file=sys.stderr)
            return 1
        summaries = _read_summaries(summaries_path)
        input_sha256 = sha256_file(input_path)
        for summary in summaries:
            if not isinstance(summary, dict) or not isinstance(summary.get("vehicle_id"), str):
                raise CutoverError("processor emitted a summary without a vehicle_id")
            if not append_summary(
                state_dir,
                producer=args.producer,
                fencing_token=token,
                scenario=args.scenario,
                input_sha256=input_sha256,
                record=summary,
            ):
                print(
                    f"producer {args.producer} fenced at token {token}; "
                    f"current lease is {read_lease(state_dir)}",
                    file=sys.stderr,
                )
                return 1
    print(f"accepted {len(summaries)} {args.producer} summaries for {args.scenario} at token {token}")
    return 0


def _cmd_rollback(state_dir: Path) -> int:
    current = get_state(state_dir)
    target_state(current, "rollback")
    lease = handover_lease(state_dir, "candidate", "legacy")
    _, target = transition(
        state_dir,
        "rollback",
        details={"holder": lease["holder"], "fencing_token": lease["token"]},
    )
    print(f"state: {target}, lease legacy token {lease['token']}")
    return 0


def _cmd_restore(state_dir: Path) -> int:
    _, target = transition(state_dir, "restore")
    print(f"state: {target}")
    return 0


def _consumer_env(ctx) -> dict:
    env = os.environ.copy()
    paths = [str(ctx.repo_root / "consumer-warranty" / "src"), str(ctx.repo_root)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def _cmd_consume(args, ctx, state_dir: Path) -> int:
    env = _consumer_env(ctx)
    import_check = subprocess.run(
        [sys.executable, "-c", "import warranty_consumer"],
        cwd=ctx.repo_root,
        env=env,
        capture_output=True,
        text=True,
    )
    if import_check.returncode:
        print("consumer-warranty not available at this revision", file=sys.stderr)
        return 3

    output = Path(args.state_out) if args.state_out else state_dir / "consumer-state.json"
    if not output.is_absolute():
        output = ctx.repo_root / output
    with tempfile.TemporaryDirectory(prefix="consume-", dir=state_dir) as temp_name:
        summaries_path = Path(temp_name) / "summaries.jsonl"
        topic_path = state_dir / "topic.jsonl"
        records = []
        with locked(state_dir):
            if topic_path.is_file():
                for line in topic_path.read_text(encoding="utf-8").splitlines():
                    if line:
                        records.append(json.loads(line)["record"])
        summaries_path.write_text(
            "".join(
                json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "warranty_consumer",
                "--input",
                str(summaries_path),
                "--state-out",
                str(output),
            ],
            cwd=ctx.repo_root,
            env=env,
            capture_output=True,
            text=True,
        )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def _approval_command(ctx, state_dir: Path, run_dir: Path) -> str:
    evidence_path = run_dir / "evidence" / "evidence.json"
    if evidence_path.is_file():
        evidence_sha = sha256_file(evidence_path)
    else:
        evidence_sha = "<sha256-of-current-evidence.json>"
    return (
        f"python verification/cli.py --run-dir {shlex.quote(str(run_dir))} cutover approve "
        f"--state-dir {shlex.quote(str(state_dir))} --approver '<name>' "
        f"--evidence-sha256 {shlex.quote(evidence_sha)}"
    )


def _cmd_simulate(args, ctx, state_dir: Path) -> int:
    scenario = args.scenario
    _scenario_input(ctx, scenario)
    print(GUARANTEE_BOUNDARY)
    print(
        "Ordering is per vehicle key. The downstream consumer relies on "
        "idempotent upsert by (vehicle_id, session_id) to absorb duplicates."
    )

    if args.after_approval:
        if get_state(state_dir) != "ApprovedForCutover":
            raise CutoverError("--after-approval requires an ApprovedForCutover state directory")
        _cmd_cutover(state_dir)
        stale_args = argparse.Namespace(producer="legacy", scenario=scenario, registry=args.registry)
        stale_result = _cmd_produce(stale_args, ctx, state_dir)
        if stale_result != 1:
            print("expected stale legacy producer to be fenced", file=sys.stderr)
            return 1
        candidate_args = argparse.Namespace(
            producer="candidate", scenario=scenario, registry=args.registry
        )
        if _cmd_produce(candidate_args, ctx, state_dir):
            return 1
        _cmd_rollback(state_dir)
        _cmd_restore(state_dir)
        print("Rollback restored legacy production with a new fencing token.")
        return 0

    if not args.state_dir:
        temp_root = ctx.repo_root / "build-output"
        temp_root.mkdir(parents=True, exist_ok=True)
        state_dir = Path(tempfile.mkdtemp(prefix="cutover-sim-", dir=temp_root)).resolve()
        print(f"simulation state: {state_dir}")
    initialize_state(state_dir)
    _cmd_start_candidate(state_dir)
    shadow_args = argparse.Namespace(scenario=[scenario], registry=args.registry)
    if _cmd_shadow(shadow_args, ctx, state_dir):
        return 1
    evaluate_args = argparse.Namespace(run_dir=None)
    evaluate_status = _cmd_evaluate(evaluate_args, ctx, state_dir)
    print("STOP: no approval was attempted.")
    print(_approval_command(ctx, state_dir, _run_dir(ctx, None)))
    if evaluate_status:
        print("The command is informational until all gates and context checks pass.")
    return evaluate_status


def main(ctx, argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    state_dir = _state_dir(ctx, args.state_dir)
    try:
        if args.command == "init":
            return _cmd_init(args, ctx, state_dir)
        if args.command == "status":
            return _cmd_status(args, state_dir)
        if args.command == "start-candidate":
            return _cmd_start_candidate(state_dir)
        if args.command == "shadow":
            return _cmd_shadow(args, ctx, state_dir)
        if args.command == "evaluate":
            return _cmd_evaluate(args, ctx, state_dir)
        if args.command == "rework":
            return _cmd_rework(state_dir)
        if args.command == "approve":
            return _cmd_approve(args, ctx, state_dir)
        if args.command == "cutover":
            return _cmd_cutover(state_dir)
        if args.command == "produce":
            return _cmd_produce(args, ctx, state_dir)
        if args.command == "rollback":
            return _cmd_rollback(state_dir)
        if args.command == "restore":
            return _cmd_restore(state_dir)
        if args.command == "consume":
            return _cmd_consume(args, ctx, state_dir)
        if args.command == "simulate":
            return _cmd_simulate(args, ctx, state_dir)
    except (CutoverError, OSError, ValueError) as exc:
        print(f"cutover: {exc}", file=sys.stderr)
        return 1
    return 2
