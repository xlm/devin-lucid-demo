"""Behavioural differential gate for replay processor outputs."""
from __future__ import annotations

from verification.common import RunContext, check, gate_result, write_gate
from verification.diff_harness.comparator import CATEGORIES, compare, load_outputs
from verification.diff_harness.report import write_markdown
from verification.diff_harness.runs import baseline_selection, build_status, run_ok, run_record

MAX_FINDINGS_PER_CHECK = 20


def _detail(category: str, findings: list) -> str:
    if not findings:
        return f"no {category}"
    examples = "; ".join(finding.detail for finding in findings[:3])
    return f"{len(findings)} {category} finding(s): {examples}"


def _scenario_checks(ctx: RunContext, scenario: str, selection: dict) -> list[dict]:
    checks = []
    candidate_run = run_record(ctx, ctx.candidate, scenario)
    checks.append(check(
        f"{scenario}:candidate:run",
        "pass" if run_ok(candidate_run) else "fail",
        "candidate scenario run completed" if run_ok(candidate_run)
        else "candidate scenario run did not complete successfully",
        category="equivalence",
        verification="runtime",
        scenario=scenario,
        evidence=candidate_run or {},
    ))
    processors = [ctx.candidate]
    if selection["used"] == "legacy":
        processors.append("legacy")
        legacy_run = run_record(ctx, "legacy", scenario)
        checks.append(check(
            f"{scenario}:legacy:run",
            "pass" if run_ok(legacy_run) else "fail",
            "legacy scenario run completed" if run_ok(legacy_run)
            else "legacy scenario run did not complete successfully",
            category="equivalence",
            verification="runtime",
            scenario=scenario,
            evidence=legacy_run or {},
        ))

    expected = load_outputs(ctx.expected_dir(scenario))
    input_text = (ctx.scenario_dir(scenario) / "input.jsonl").read_text()
    pairs = []
    candidate = load_outputs(ctx.output_dir(ctx.candidate, scenario))
    pairs.append(("candidate_vs_expected", ctx.candidate, "expected", candidate, expected))
    if selection["used"] == "legacy":
        legacy = load_outputs(ctx.output_dir("legacy", scenario))
        pairs.extend((
            ("legacy_vs_expected", "legacy", "expected", legacy, expected),
            ("candidate_vs_legacy", ctx.candidate, "legacy", candidate, legacy),
        ))
    for pair_name, actual_name, baseline_name, actual, baseline in pairs:
        findings = compare(
            baseline,
            actual,
            tolerance=ctx.tolerance,
            strict=ctx.strict,
            input_text=input_text,
        )
        for category in CATEGORIES:
            category_findings = [finding for finding in findings
                                 if finding.category == category]
            evidence = {
                "actual": actual_name,
                "baseline": baseline_name,
                "baseline_used": selection["used"],
                "tolerance": ctx.tolerance,
                "strict": ctx.strict,
                "findings_total": len(category_findings),
                "findings": [finding.to_dict()
                             for finding in category_findings[:MAX_FINDINGS_PER_CHECK]],
            }
            if category == "MISSING_OUTPUT":
                evidence["missing_files"] = actual.missing_files
            checks.append(check(
                f"{scenario}:{pair_name}:{category}",
                "fail" if category_findings else "pass",
                _detail(category, category_findings),
                category=category,
                verification="runtime",
                scenario=scenario,
                evidence=evidence,
            ))
    return checks


def run_gate(ctx: RunContext) -> dict:
    checks = []
    selection = baseline_selection(ctx)
    checks.append(check(
        "baseline.selection",
        "pass",
        f"using {selection['used']} baseline: {selection['reason']}",
        category="equivalence",
        verification="runtime",
        evidence=selection,
    ))
    candidate_build = build_status(ctx, ctx.candidate)
    if candidate_build is None or candidate_build.get("status") == "failed":
        reason = "candidate build record missing" if candidate_build is None else \
            candidate_build.get("error") or "candidate build failed"
        result = write_gate(ctx, gate_result(
            "behavioural", checks, error=f"candidate build failed: {reason}",
        ))
        write_markdown(ctx, result)
        return result
    candidate_dir = ctx.run_dir / "outputs" / ctx.candidate
    if not candidate_dir.is_dir():
        result = write_gate(ctx, gate_result(
            "behavioural", checks, error="candidate output directory missing",
        ))
        write_markdown(ctx, result)
        return result

    for scenario in ctx.scenarios:
        try:
            checks.extend(_scenario_checks(ctx, scenario, selection))
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
    result = write_gate(ctx, gate_result("behavioural", checks))
    write_markdown(ctx, result)
    return result
