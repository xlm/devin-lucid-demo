import json

import pytest
import yaml

from verification.common import REPO_ROOT, RunContext, all_scenarios
from verification.privacy import evaluate
from verification.privacy.disposition import recommend
from verification.privacy.policy import RULE_IMPLEMENTATIONS
from verification.privacy.runtime import no_identifiers_in_log
from verification.privacy.static_checks import source_scan


def _context(tmp_path, scenarios=("normal_session",)):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    source_dir = run_dir / "src"
    source_dir.mkdir()
    (source_dir / "main.rs").write_text(
        'fn main() { eprintln!("processed {} lines", n); }\n',
        encoding="utf-8",
    )
    ctx = RunContext(repo_root=REPO_ROOT, run_dir=run_dir, scenarios=list(scenarios),
                     baseline="expected", candidate="candidate")
    for scenario in scenarios:
        output_dir = ctx.output_dir("candidate", scenario)
        output_dir.mkdir(parents=True)
        expected_dir = REPO_ROOT / "fixtures" / "expected" / scenario
        for name in ("summaries.jsonl", "errors.jsonl"):
            (output_dir / name).write_bytes((expected_dir / name).read_bytes())
        (output_dir / "processor.log").write_text("processed 12 lines\n", encoding="utf-8")
    return ctx, source_dir


def _candidate_copy(ctx, name="candidate.yaml"):
    path = ctx.run_dir / name
    path.write_bytes((REPO_ROOT / "rust-processor" / "processing-inventory.yaml").read_bytes())
    return path


def _mutated_candidate(ctx, mutate):
    path = _candidate_copy(ctx)
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(document["dimensions"])
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _evaluate(ctx, source_dir, candidate_inventory=None):
    return evaluate(ctx, candidate_inventory=candidate_inventory, source_dir=source_dir)


def test_clean_candidate_inventory_and_fixture_outputs_have_no_material_change(tmp_path):
    scenarios = all_scenarios(REPO_ROOT)
    ctx, source_dir = _context(tmp_path, scenarios)
    result = _evaluate(ctx, source_dir)
    assert result["status"] == "pass"
    assert result["summary"]["status_reason"] == "recommendation is no-material-change"
    assert result["disposition"]["recommendation"] == "no-material-change"
    assert result["inventory"]["candidate"]["path"] == "rust-processor/processing-inventory.yaml"
    assert all(check["status"] == "pass" for check in result["checks"])
    assert result["summary"]["checks"] == len(result["checks"])
    assert result["summary"]["passed"] == sum(
        check["status"] == "pass" for check in result["checks"]
    )
    assert result["summary"]["failed"] == sum(
        check["status"] == "fail" for check in result["checks"]
    )
    assert all(check["evidence"]["basis"] in {
        "runtime-observed", "declared-manifest", "source-scan", "integrity"
    } for check in result["checks"])


def test_leaked_location_and_identifier_fail_specific_checks_without_identifier_values(tmp_path):
    ctx, source_dir = _context(tmp_path, ("schema_evolution",))

    def leak(dimensions):
        summary = next(item for item in dimensions["output_schema"]
                       if item["id"] == "charging.session.summary.v1")
        summary["fields"].append("synthetic_location")
        location = next(item for item in dimensions["sensitive_fields"]
                        if item["id"] == "synthetic_location")
        location["propagates_to"] = ["charging.session.summary.v1"]

    inventory = _mutated_candidate(ctx, leak)
    output = ctx.output_dir("candidate", "schema_evolution")
    record = json.loads((output / "summaries.jsonl").read_text().splitlines()[0])
    record["synthetic_location"] = "SYN-GRID-0042"
    (output / "summaries.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    (output / "processor.log").write_text("failed VEH-0012\n", encoding="utf-8")
    result = _evaluate(ctx, source_dir, inventory)
    failed = {item["id"] for item in result["checks"] if item["status"] == "fail"}
    assert result["status"] == "fail"
    assert result["summary"]["status_reason"] == (
        "recommendation is policy-violation; human review required"
    )
    assert result["disposition"]["recommendation"] == "policy-violation"
    assert {
        "policy.no-new-sensitive-output-fields.static",
        "policy.no-sensitive-location-propagation.static",
        "runtime.output_fields",
        "runtime.no_sensitive_location",
        "runtime.no_identifiers_in_log",
    } <= failed
    gate_path = ctx.run_dir / "gates" / "privacy.json"
    gate_path.parent.mkdir(parents=True)
    gate_path.write_text(json.dumps(result), encoding="utf-8")
    assert "VEH-0012" not in gate_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("case", "recommendation", "failed_check"),
    [
        ("retention", "policy-violation", "policy.no-retention-change.static"),
        ("destination", "policy-violation", "policy.no-new-destinations.static"),
        ("access", "policy-violation", "policy.no-expanded-access.static"),
        ("vehicle-transform", "policy-violation",
         "policy.no-altered-sensitive-transformations.static"),
        ("energy-transform", "needs-clarification", "diff.transforms"),
        ("logging", "policy-violation", "policy.no-relaxed-logging-restrictions.static"),
        ("external-dependency", "new-processing-requires-review",
         "policy.review-new-external-dependencies.static"),
        ("replication", "new-processing-requires-review",
         "policy.review-replication-change.static"),
        ("deployment", "new-processing-requires-review",
         "policy.review-deployment-boundary-change.static"),
        ("new-purpose", "new-processing-requires-review",
         "policy.review-new-inputs-or-purposes.static"),
        ("removed-purpose", "needs-clarification", "diff.purposes"),
    ],
)
def test_policy_rule_dispositions(tmp_path, case, recommendation, failed_check):
    ctx, source_dir = _context(tmp_path)

    def mutate(dimensions):
        if case == "retention":
            dimensions["retention"][0]["period"] = "P30D"
        elif case == "destination":
            dimensions["output_destinations"].append(
                {"id": "new-topic", "kind": "topic", "identifiers": []}
            )
        elif case == "access":
            dimensions["access"][0]["readers"].append("new-reader")
        elif case == "vehicle-transform":
            item = next(item for item in dimensions["transforms"]
                        if item["id"] == "charging.session.summary.v1.vehicle_id")
            item["op"] = "HASH"
        elif case == "energy-transform":
            item = next(item for item in dimensions["transforms"]
                        if item["id"] == "charging.session.summary.v1.total_energy_kwh")
            item["op"] = "SUM"
        elif case == "logging":
            dimensions["logging"][0]["prohibited_content"].remove("vehicle_id")
        elif case == "external-dependency":
            dimensions["external_deps"].append({"id": "new-library", "kind": "library"})
        elif case == "replication":
            dimensions["replication"][0]["contents"].append("another state field")
        elif case == "deployment":
            dimensions["deployment_boundary"][0]["region"] = "synthetic-region-2"
        elif case == "new-purpose":
            dimensions["purposes"].append({"id": "new-purpose", "description": "synthetic test"})
        elif case == "removed-purpose":
            dimensions["purposes"].pop()

    inventory = _mutated_candidate(ctx, mutate)
    result = _evaluate(ctx, source_dir, inventory)
    failed = {item["id"] for item in result["checks"] if item["status"] == "fail"}
    assert failed_check in failed
    assert result["disposition"]["recommendation"] == recommendation
    if case == "energy-transform":
        assert failed == {"diff.transforms"}


def test_missing_outputs_and_source_do_not_claim_no_material_change(tmp_path):
    ctx, source_dir = _context(tmp_path)
    for child in ctx.run_dir.glob("outputs/candidate/normal_session/*"):
        child.unlink()
    (ctx.output_dir("candidate", "normal_session")).rmdir()
    missing_outputs = _evaluate(ctx, source_dir)
    runtime = [item for item in missing_outputs["checks"] if item["verification"] == "runtime"]
    assert {item["status"] for item in runtime} == {"skip"}
    assert missing_outputs["status"] == "error"
    assert missing_outputs["summary"]["status_reason"] == (
        "every runtime check was skipped; no candidate outputs inspected"
    )
    assert missing_outputs["disposition"]["recommendation"] == "needs-clarification"

    ctx2, _ = _context(tmp_path / "source")
    missing_source = _evaluate(ctx2, ctx2.run_dir / "absent-source")
    source_check = next(item for item in missing_source["checks"]
                        if item["id"] == "source_scan.identifier_logging")
    assert source_check["status"] == "skip"
    assert source_check["detail"] == "candidate source not present; static claim not established"
    assert missing_source["disposition"]["recommendation"] == "needs-clarification"
    assert all(
        item["status"] == "pass"
        for item in missing_source["checks"] if item["verification"] == "runtime"
    )
    assert missing_source["status"] == "fail"
    assert missing_source["summary"]["status_reason"] == (
        "recommendation is needs-clarification; human review required"
    )


def test_missing_errors_file_skips_output_runtime_checks(tmp_path):
    ctx, source_dir = _context(tmp_path)
    output_dir = ctx.output_dir("candidate", "normal_session")
    (output_dir / "errors.jsonl").unlink()

    result = _evaluate(ctx, source_dir)

    checks = {
        item["id"]: item for item in result["checks"]
        if item["id"] in {"runtime.output_fields", "runtime.no_sensitive_location"}
    }
    for check in checks.values():
        assert check["status"] == "skip"
        assert check["detail"] == (
            "candidate output incomplete; missing: ['errors.jsonl']; "
            "runtime claim not established"
        )
        assert check["evidence"]["missing_files"] == ["errors.jsonl"]
    assert set(checks) == {"runtime.output_fields", "runtime.no_sensitive_location"}
    assert result["disposition"]["recommendation"] == "needs-clarification"


def test_missing_processor_log_skips_sensitive_location_check(tmp_path):
    ctx, source_dir = _context(tmp_path)
    output_dir = ctx.output_dir("candidate", "normal_session")
    (output_dir / "processor.log").unlink()

    result = _evaluate(ctx, source_dir)

    checks = {item["id"]: item for item in result["checks"] if item["verification"] == "runtime"}
    location_check = checks["runtime.no_sensitive_location"]
    identifiers_check = checks["runtime.no_identifiers_in_log"]
    assert location_check["status"] == "skip"
    assert location_check["detail"] == (
        "candidate output incomplete; missing: ['processor.log']; "
        "runtime claim not established"
    )
    assert location_check["evidence"]["missing_files"] == ["processor.log"]
    assert identifiers_check["status"] == "skip"
    assert result["disposition"]["recommendation"] == "needs-clarification"


def test_source_scan_skips_existing_directory_without_rust_sources(tmp_path):
    source_dir = tmp_path / "empty-source"
    source_dir.mkdir()

    result = source_scan(source_dir)

    assert result["status"] == "skip"
    assert result["detail"] == "no Rust sources found; static claim not established"
    assert result["evidence"]["severity"] == "clarification"
    assert result["evidence"]["basis"] == "source-scan"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('fn f() { eprintln!("{}", ev.vehicle_id); }', "fail"),
        ('fn f() { eprintln!(\n "x {}",\n session_id\n); }', "fail"),
        ("fn f() { tracing::info!(vehicle_id = %v); }", "fail"),
        ('fn f() { log::warn!("session_id {}", value); }', "fail"),
        ('fn f() { eprintln!("processed {} events", n); }', "pass"),
        ("fn f() { let vehicle_id = value; }", "pass"),
    ],
)
def test_source_scan_log_macro_attack_matrix(tmp_path, source, expected):
    source_dir = tmp_path / "run" / "src"
    source_dir.mkdir(parents=True)
    (source_dir / "main.rs").write_text(source, encoding="utf-8")
    result = source_scan(source_dir)
    assert result["status"] == expected
    assert result["evidence"]["basis"] == "source-scan"


@pytest.mark.parametrize(
    ("log_text", "matched"),
    [
        ("VEH-00123", False),
        ("S-0012-A", True),
        ("xS-0012-A", False),
        ("VEH-0012-x", True),
        ("prefix-S-0012-A", True),
        ("S-0012-A-retry", True),
    ],
)
def test_identifier_log_matching_uses_whole_tokens(tmp_path, log_text, matched):
    ctx, _ = _context(tmp_path, ("schema_evolution",))
    output_dir = ctx.output_dir("candidate", "schema_evolution")
    (output_dir / "processor.log").write_text(log_text, encoding="utf-8")
    result = no_identifiers_in_log(ctx, "schema_evolution", output_dir)
    assert (result["status"] == "fail") is matched
    assert "VEH-0012" not in json.dumps(result)
    assert "S-0012-A" not in json.dumps(result)


def test_disposition_precedence():
    checks = [
        {"id": "clarification", "status": "fail", "evidence": {"severity": "clarification"}},
        {"id": "review", "status": "fail", "evidence": {"severity": "review"}},
        {"id": "violation", "status": "fail", "evidence": {"severity": "violation"}},
    ]
    assert recommend(checks)["recommendation"] == "policy-violation"
    assert recommend(checks[:2])["recommendation"] == "new-processing-requires-review"
    assert recommend(checks[:1])["recommendation"] == "needs-clarification"


def test_gate_writes_only_gate_and_does_not_change_governance(tmp_path):
    ctx, source_dir = _context(tmp_path)
    from verification.privacy import run_gate

    protected = [
        REPO_ROOT / "governance",
        REPO_ROOT / "legacy-scala" / "processing-inventory.yaml",
        REPO_ROOT / "rust-processor" / "processing-inventory.yaml",
    ]
    before = {
        path.relative_to(REPO_ROOT): path.read_bytes()
        for item in protected
        for path in ([item] if item.is_file() else sorted(item.rglob("*")))
        if path.is_file()
    }
    _evaluate(ctx, source_dir)
    before_gate = {path for path in ctx.run_dir.rglob("*") if path.is_file()}
    result = run_gate(ctx)
    after_gate = {path for path in ctx.run_dir.rglob("*") if path.is_file()}
    assert after_gate - before_gate == {ctx.gate_path("privacy")}
    written = json.loads(ctx.gate_path("privacy").read_text(encoding="utf-8"))
    assert written["status"] == result["status"]
    assert written["summary"]["status_reason"] == result["summary"]["status_reason"]
    after = {
        path.relative_to(REPO_ROOT): path.read_bytes()
        for item in protected
        for path in ([item] if item.is_file() else sorted(item.rglob("*")))
        if path.is_file()
    }
    assert before == after


def test_unknown_candidate_processor_is_a_gate_error(tmp_path):
    ctx, source_dir = _context(tmp_path)
    ctx.candidate = "unlisted"
    result = _evaluate(ctx, source_dir)
    assert result["status"] == "error"
    assert result["summary"]["status_reason"] == f"gate error: {result['error']}"
    assert result["disposition"]["recommendation"] == "needs-clarification"
