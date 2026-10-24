"""Privacy inventory and policy gate for the synthetic charging demo."""
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from verification.common import RunContext, all_scenarios, check, gate_result, write_gate
from verification.privacy.baseline import content_sha256
from verification.privacy.disposition import recommend
from verification.privacy.inventory import (
    DIMENSIONS,
    dimension_ids,
    diff_dimensions,
    load_document,
    validate_dimensions,
)
from verification.privacy.policy import RULE_IMPLEMENTATIONS
from verification.privacy.runtime import runtime_checks
from verification.privacy.static_checks import manifest_checks, source_scan

BASELINE_NOTE = (
    "Privacy comparison always uses governance/baselines/charging.baseline.json, "
    "independent of ctx.baseline."
)
DEFAULT_PARAMS = {
    "restricted_classifications": ["sensitive_location", "security_sensitive"],
    "identifier_classifications": ["vehicle_identifying"],
    "log_destination_kinds": ["log"],
    "restricted_value_pattern": r"(?<![A-Za-z0-9-])SYN-[A-Z0-9-]+",
}


def _relative_path(repo_root: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    return Path(os.path.relpath(path.resolve(), repo_root.resolve())).as_posix()


def _candidate_paths(ctx: RunContext) -> tuple[Path | None, Path | None, str | None]:
    try:
        registry = load_document(ctx.repo_root / "processors.yaml")
        command = registry["processors"][ctx.candidate]["command"]
        processor_dir = ctx.repo_root / command[0].split("/")[0]
    except (OSError, KeyError, IndexError, TypeError, yaml.YAMLError) as exc:
        return None, None, f"unknown processor name or invalid registry entry {ctx.candidate!r}: {exc}"
    return processor_dir / "processing-inventory.yaml", processor_dir / "src", None


def _severity_check(check_id: str, passed: bool, detail: str, severity: str, *,
                    category: str, evidence: dict | None = None) -> dict:
    data = {"severity": severity}
    data.update(evidence or {})
    if data.get("basis") == "declared-manifest" and "declarations only" not in detail:
        detail = f"declarations only: {detail}"
    return check(check_id, "pass" if passed else "fail", detail, category=category,
                 verification="static", evidence=data)


def _load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def evaluate(ctx: RunContext, *, candidate_inventory: Path | None = None,
             baseline_path: Path | None = None, source_dir: Path | None = None,
             governance_dir: Path | None = None) -> dict:
    governance_dir = governance_dir or ctx.repo_root / "governance"
    baseline_path = baseline_path or governance_dir / "baselines" / "charging.baseline.json"
    default_candidate, default_source, path_error = _candidate_paths(ctx)
    candidate_inventory = candidate_inventory or default_candidate
    source_dir = source_dir or default_source
    error_messages = [path_error] if path_error else []

    baseline = {}
    try:
        baseline = _load_json(baseline_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        error_messages.append(f"baseline could not be read: {exc}")

    candidate = None
    candidate_load_error = None
    if candidate_inventory is not None:
        try:
            candidate = load_document(candidate_inventory)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            candidate_load_error = f"candidate inventory could not be read: {exc}"
            error_messages.append(candidate_load_error)
    baseline_dimensions = baseline.get("dimensions", {})
    candidate_dimensions = candidate.get("dimensions", {}) if candidate else {}

    checks = []
    computed_hash = content_sha256(baseline) if baseline else None
    recorded_hash = baseline.get("content_sha256")
    hash_ok = computed_hash is not None and recorded_hash == computed_hash
    checks.append(_severity_check(
        "baseline.integrity", hash_ok,
        "recorded baseline hash matches recomputed content" if hash_ok
        else "baseline was edited without re-approval and results are not trustworthy",
        "clarification", category="baseline_integrity",
        evidence={"basis": "integrity", "recorded": recorded_hash, "computed": computed_hash},
    ))

    legacy_path = ctx.repo_root / "legacy-scala" / "processing-inventory.yaml"
    try:
        legacy_dimensions = load_document(legacy_path).get("dimensions")
        legacy_match = legacy_dimensions == baseline_dimensions
        legacy_detail = "legacy inventory declarations match the approved baseline" if legacy_match else (
            "legacy inventory declarations do not match the approved baseline"
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        legacy_match = False
        legacy_detail = f"legacy inventory could not be read: {exc}"
    checks.append(_severity_check(
        "baseline.legacy_inventory_matches", legacy_match, legacy_detail, "clarification",
        category="baseline_integrity", evidence={"basis": "declared-manifest"},
    ))

    baseline_problems = validate_dimensions(baseline_dimensions)
    checks.append(_severity_check(
        "inventory.baseline.well_formed", not baseline_problems,
        "baseline has the 12 required dimensions with unique item ids" if not baseline_problems
        else "; ".join(baseline_problems),
        "clarification", category="inventory_well_formedness",
        evidence={"basis": "declared-manifest", "problems": baseline_problems},
    ))

    if candidate is None:
        candidate_problems = [candidate_load_error or "candidate processor is unknown"]
    else:
        candidate_problems = validate_dimensions(candidate_dimensions)
    candidate_valid = candidate is not None and not candidate_problems
    checks.append(_severity_check(
        "inventory.candidate.well_formed", candidate_valid,
        "candidate has the 12 required dimensions with unique item ids" if candidate_valid
        else "; ".join(candidate_problems),
        "clarification", category="inventory_well_formedness",
        evidence={"basis": "declared-manifest", "problems": candidate_problems},
    ))

    checks.extend(manifest_checks(ctx.repo_root, governance_dir, baseline, candidate))
    deltas = diff_dimensions(baseline_dimensions, candidate_dimensions) if (
        not baseline_problems and candidate_valid
    ) else {}
    if deltas:
        for dimension in DIMENSIONS:
            delta = deltas[dimension]
            unchanged = not any(delta.values())
            checks.append(_severity_check(
                f"diff.{dimension}", unchanged,
                f"{dimension} matches baseline" if unchanged else f"{dimension} differs from baseline",
                "clarification", category="inventory_diff",
                evidence={"basis": "declared-manifest", "diff": delta},
            ))

    policies = []
    policy_path = governance_dir / "policies" / "migration-policies.yaml"
    try:
        policy_document = load_document(policy_path)
        params = policy_document["parameters"]
        rules = policy_document["rules"]
        policy_ids = [rule["id"] for rule in rules]
        missing_implementations = sorted(set(policy_ids) - set(RULE_IMPLEMENTATIONS))
        extra_implementations = sorted(set(RULE_IMPLEMENTATIONS) - set(policy_ids))
        if missing_implementations or extra_implementations:
            error_messages.append(
                f"policy rule implementation mismatch: missing={missing_implementations}, "
                f"extra={extra_implementations}"
            )
            rules = []
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        params = DEFAULT_PARAMS
        rules = []
        error_messages.append(f"migration policy data could not be read: {exc}")

    if candidate_valid and not baseline_problems and rules:
        for rule in rules:
            rule_id = rule["id"]
            findings = RULE_IMPLEMENTATIONS[rule_id](
                baseline_dimensions, candidate_dimensions,
                load_document(governance_dir / "fields" / "charging.yaml"), params,
            )
            severity = rule["severity"]
            passed = not findings
            item = _severity_check(
                f"policy.{rule_id}.static", passed,
                f"static policy {rule_id} passed" if passed else "; ".join(findings),
                severity, category="policy",
                evidence={"basis": "declared-manifest", "rule": rule_id, "findings": findings},
            )
            checks.append(item)
            policies.append({"id": rule_id, "title": rule.get("title", rule_id),
                             "severity": severity, "status": item["status"]})

    checks.append(source_scan(source_dir or (ctx.repo_root / "__candidate_source_missing__")))
    scenarios = ctx.scenarios or all_scenarios(ctx.repo_root)
    checks.extend(runtime_checks(ctx, scenarios, baseline_dimensions, params))
    gate_error = "; ".join(error_messages) if error_messages else None
    result = gate_result("privacy", checks, error=gate_error)
    result.update({
        "disposition": recommend(checks, gate_error),
        "inventory": {
            "baseline": {
                "path": _relative_path(ctx.repo_root, baseline_path),
                "processor": baseline.get("processor"),
                "approval": baseline.get("approval"),
                "content_sha256_recorded": recorded_hash,
                "content_sha256_computed": computed_hash,
                "implementation": baseline.get("implementation"),
                "dimensions": dimension_ids(baseline_dimensions) if not baseline_problems else {},
            },
            "candidate": {
                "path": _relative_path(ctx.repo_root, candidate_inventory),
                "processor": candidate.get("processor") if candidate else ctx.candidate,
                "implementation": candidate.get("implementation") if candidate else None,
                "dimensions": dimension_ids(candidate_dimensions) if candidate_valid else {},
            },
            "diff": deltas,
            "implementation_changed": (
                baseline.get("implementation") != candidate.get("implementation")
                if candidate is not None else None
            ),
            "baseline_note": BASELINE_NOTE,
        },
        "policies": policies,
    })
    return result


def run_gate(ctx: RunContext) -> dict:
    return write_gate(ctx, evaluate(ctx))


__all__ = ["content_sha256", "evaluate", "run_gate"]
