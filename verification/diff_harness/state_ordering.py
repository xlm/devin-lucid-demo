"""Restart equivalence, state recovery, and per-key ordering checks."""
from __future__ import annotations

from collections import Counter, defaultdict

from verification.common import RunContext, check, gate_result, write_gate
from verification.diff_harness.comparator import Outputs, compare, load_outputs
from verification.diff_harness.runs import baseline_selection, build_status, run_ok, run_record

MAX_FINDINGS_PER_CHECK = 20


def _finding_evidence(findings: list) -> dict:
    return {
        "categories": dict(sorted(Counter(finding.category for finding in findings).items())),
        "findings_total": len(findings),
        "findings": [finding.to_dict() for finding in findings[:MAX_FINDINGS_PER_CHECK]],
    }


def _check_restart(ctx: RunContext, scenario: str, processor: str) -> list[dict]:
    input_text = (ctx.scenario_dir(scenario) / "input.jsonl").read_text()
    record = run_record(ctx, processor, scenario)
    commands = record.get("commands", []) if record else []
    exit_codes = record.get("exit_codes", []) if record else []
    restart_executed = (
        run_ok(record)
        and record.get("checkpoint_written") is True
        and len(commands) == 2
        and len(exit_codes) == 2
        and all(code == 0 for code in exit_codes)
        and all(flag in commands[0] for flag in ("--stop-after", "--checkpoint"))
        and "--resume-from" in commands[1]
    )
    checks = [check(
        f"{scenario}:{processor}:restart_executed",
        "pass" if restart_executed else "fail",
        "restart commands completed with checkpoint and resume flags"
        if restart_executed else "restart did not complete both required commands",
        category="equivalence",
        verification="runtime",
        scenario=scenario,
        evidence=record or {},
    )]
    expected = load_outputs(ctx.expected_dir(scenario))
    actual = load_outputs(ctx.output_dir(processor, scenario))
    findings = compare(
        expected, actual, tolerance=ctx.tolerance, strict=ctx.strict, input_text=input_text,
    )
    checks.append(check(
        f"{scenario}:{processor}:equals_uninterrupted",
        "pass" if not findings else "fail",
        "restart output matches uninterrupted expected output"
        if not findings else f"{len(findings)} finding(s) differ from expected output",
        category="equivalence",
        verification="runtime",
        scenario=scenario,
        evidence=_finding_evidence(findings),
    ))
    duplicate_findings = [finding for finding in findings
                          if finding.category == "DUPLICATE_OUTPUT"]
    extra_errors = [finding for finding in findings
                    if finding.category == "ERROR_PATH_MISMATCH"
                    and finding.data.get("direction") == "extra"]
    duplicate_evidence = duplicate_findings + extra_errors
    checks.append(check(
        f"{scenario}:{processor}:no_duplicate_after_resume",
        "pass" if not duplicate_evidence else "fail",
        "no duplicate summaries or extra errors after resume"
        if not duplicate_evidence else
        f"{len(duplicate_evidence)} duplicate or extra error finding(s) after resume",
        category="DUPLICATE_OUTPUT",
        verification="runtime",
        scenario=scenario,
        evidence=_finding_evidence(duplicate_evidence),
    ))
    ordering = [finding for finding in findings
                if finding.category == "ORDERING_WITHIN_KEY"]
    checks.append(check(
        f"{scenario}:{processor}:per_key_order",
        "pass" if not ordering else "fail",
        "per-key output order matches expected output"
        if not ordering else f"{len(ordering)} within-key ordering finding(s)",
        category="ORDERING_WITHIN_KEY",
        verification="runtime",
        scenario=scenario,
        evidence=_finding_evidence(ordering),
    ))
    return checks


def _check_partitioning(ctx: RunContext, processor: str) -> list[dict]:
    scenario = "partitioning_ordering"
    input_text = (ctx.scenario_dir(scenario) / "input.jsonl").read_text()
    expected = load_outputs(ctx.expected_dir(scenario))
    actual = load_outputs(ctx.output_dir(processor, scenario))
    findings = compare(
        expected, actual, tolerance=ctx.tolerance, strict=ctx.strict, input_text=input_text,
    )
    ordering = [finding for finding in findings
                if finding.category == "ORDERING_WITHIN_KEY"]
    checks = [check(
        f"{scenario}:{processor}:per_key_order",
        "pass" if not ordering else "fail",
        "per-key output order matches expected output"
        if not ordering else f"{len(ordering)} within-key ordering finding(s)",
        category="ORDERING_WITHIN_KEY",
        verification="runtime",
        scenario=scenario,
        evidence=_finding_evidence(ordering),
    )]
    checks.append(check(
        f"{scenario}:{processor}:equals_expected",
        "pass" if not findings else "fail",
        "partitioning output matches expected output"
        if not findings else f"{len(findings)} finding(s) differ from expected output",
        category="equivalence",
        verification="runtime",
        scenario=scenario,
        evidence=_finding_evidence(findings),
    ))
    return checks


def _key(record: dict) -> tuple | None:
    vehicle_id, session_id = record.get("vehicle_id"), record.get("session_id")
    if isinstance(vehicle_id, str) and isinstance(session_id, str):
        return vehicle_id, session_id
    return None


def _groups(records: list[dict]) -> list[list[dict]]:
    grouped = defaultdict(list)
    for record in records:
        grouped[_key(record)].append(record)
    return list(grouped.values())


def _comparator_checks(ctx: RunContext) -> list[dict]:
    scenario = "partitioning_ordering"
    expected = load_outputs(ctx.expected_dir(scenario))
    groups = _groups(expected.summaries)
    permutations = {
        "reversed_groups": list(reversed(groups)),
        "rotated_groups": groups[len(groups) // 2:] + groups[:len(groups) // 2],
        "descending_key_groups": sorted(
            groups,
            key=lambda group: _key(group[0]) or ("", ""),
            reverse=True,
        ),
    }
    permutation_findings = {}
    for name, permuted_groups in permutations.items():
        permuted = Outputs(
            summaries=[record for group in permuted_groups for record in group],
            errors=expected.errors,
        )
        permutation_findings[name] = [
            finding.to_dict() for finding in compare(expected, permuted)
        ]
    cross_key_pass = all(not findings for findings in permutation_findings.values())
    checks = [check(
        f"{scenario}:comparator:cross_key_permutation_passes",
        "pass" if cross_key_pass else "fail",
        "cross-key permutations preserve comparison equivalence"
        if cross_key_pass else "a cross-key permutation changed comparison findings",
        category="ORDERING_WITHIN_KEY",
        verification="runtime",
        scenario=scenario,
        evidence={"permutations": permutation_findings},
    )]

    if not expected.summaries:
        summary_ordering = []
        summary_permutation = []
    else:
        original = list(expected.summaries)
        first = original[0]
        changed = dict(first)
        changed["end_ts"] = "2026-01-15T08:00:00.001Z"
        baseline_records = original[:1] + [changed] + original[1:]
        actual_records = [changed, first] + original[1:]
        summary_baseline = Outputs(summaries=baseline_records, errors=expected.errors)
        summary_actual = Outputs(summaries=actual_records, errors=expected.errors)
        summary_permutation = [finding.to_dict() for finding in compare(
            summary_baseline, summary_actual,
        )]
        summary_ordering = [finding for finding in summary_permutation
                            if finding["category"] == "ORDERING_WITHIN_KEY"]

    first_error = {
        "error_type": "INVALID_FIELD",
        "field": "battery_soc",
        "input_line": 1,
        "event_id": "EVT-ORDER-0001",
        "vehicle_id": "VEH-0001",
        "session_id": "S-ORDER",
        "schema_version": "1.0",
    }
    second_error = dict(first_error, input_line=2, event_id="EVT-ORDER-0002")
    error_baseline = Outputs(errors=[first_error, second_error])
    error_actual = Outputs(errors=[second_error, first_error])
    error_permutation = [finding.to_dict() for finding in compare(
        error_baseline, error_actual,
    )]
    error_ordering = [finding for finding in error_permutation
                      if finding["category"] == "ORDERING_WITHIN_KEY"]
    detected = bool(summary_ordering and error_ordering)
    checks.append(check(
        f"{scenario}:comparator:within_key_swap_fails",
        "pass" if detected else "fail",
        "summary and error within-key swaps are detected"
        if detected else "a synthetic within-key swap was not detected",
        category="ORDERING_WITHIN_KEY",
        verification="runtime",
        scenario=scenario,
        evidence={
            "summary_findings": summary_permutation,
            "error_findings": error_permutation,
        },
    ))
    return checks


def run_gate(ctx: RunContext) -> dict:
    checks = []
    try:
        checks.extend(_comparator_checks(ctx))
    except Exception as exc:
        checks.append(check(
            "partitioning_ordering:comparator:self_check_error",
            "fail",
            f"{type(exc).__name__}: {exc}",
            category="ORDERING_WITHIN_KEY",
            verification="runtime",
            scenario="partitioning_ordering",
            evidence={},
        ))
    candidate_build = build_status(ctx, ctx.candidate)
    if candidate_build is None or candidate_build.get("status") == "failed":
        reason = "candidate build record missing" if candidate_build is None else \
            candidate_build.get("error") or "candidate build failed"
        return write_gate(ctx, gate_result(
            "state_ordering", checks, error=f"candidate build failed: {reason}",
        ))
    candidate_dir = ctx.run_dir / "outputs" / ctx.candidate
    if not candidate_dir.is_dir():
        return write_gate(ctx, gate_result(
            "state_ordering", checks, error="candidate output directory missing",
        ))

    selection = baseline_selection(ctx)
    processors = [ctx.candidate]
    if selection["used"] == "legacy":
        processors.append("legacy")
    for scenario in ctx.scenarios:
        try:
            meta = ctx.scenario_meta(scenario)
            if meta["run"]["mode"] == "restart":
                for processor in processors:
                    checks.extend(_check_restart(ctx, scenario, processor))
        except Exception as exc:
            checks.append(check(
                f"{scenario}:scenario:error",
                "fail",
                f"{type(exc).__name__}: {exc}",
                category="equivalence",
                verification="runtime",
                scenario=scenario,
                evidence={},
            ))
    if "partitioning_ordering" in ctx.scenarios:
        for processor in processors:
            try:
                checks.extend(_check_partitioning(ctx, processor))
            except Exception as exc:
                checks.append(check(
                    f"partitioning_ordering:{processor}:scenario:error",
                    "fail",
                    f"{type(exc).__name__}: {exc}",
                    category="equivalence",
                    verification="runtime",
                    scenario="partitioning_ordering",
                    evidence={},
                ))
    return write_gate(ctx, gate_result("state_ordering", checks))
