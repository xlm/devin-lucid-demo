import re
from pathlib import Path

import yaml

from verification.common import REPO_ROOT, RunContext
from verification.privacy import evaluate
from verification.privacy.policy import RULE_IMPLEMENTATIONS


def test_rule_ids_match_yaml_python_registry_and_rego():
    policy = yaml.safe_load((REPO_ROOT / "governance/policies/migration-policies.yaml").read_text())
    yaml_ids = {item["id"] for item in policy["rules"]}
    rego = (REPO_ROOT / "governance/policies/migration-policies.rego").read_text()
    rego_ids = set(re.findall(r'"rule":\s*"([^"]+)"', rego))
    assert yaml_ids == set(RULE_IMPLEMENTATIONS) == rego_ids


def test_manifest_and_lineage_checks_pass_for_repo_declarations(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    source_dir = run_dir / "src"
    source_dir.mkdir()
    ctx = RunContext(repo_root=REPO_ROOT, run_dir=run_dir, scenarios=["normal_session"],
                     baseline="expected", candidate="candidate")
    result = evaluate(ctx, source_dir=source_dir)
    checks = {item["id"]: item for item in result["checks"]}
    for check_id in (
        "manifest.classifications_known",
        "manifest.covers_contract_schemas",
        "manifest.restricted_fields_not_permitted",
        "manifest.identifier_fields_not_logged",
        "lineage.references_known",
        "lineage.restricted_not_propagated",
        "inventory.candidate.consistent_with_manifest",
    ):
        assert checks[check_id]["status"] == "pass"
        assert checks[check_id]["evidence"]["basis"] == "declared-manifest"


def test_restricted_and_identifier_fields_are_declared_as_prohibited():
    manifest = yaml.safe_load((REPO_ROOT / "governance/fields/charging.yaml").read_text())
    inventory_paths = (
        REPO_ROOT / "legacy-scala/processing-inventory.yaml",
        REPO_ROOT / "rust-processor/processing-inventory.yaml",
    )
    fields = {item["name"]: item for item in manifest["fields"]}
    location = fields["synthetic_location"]
    assert location["permitted_destinations"] == []
    assert location["logging"] == "prohibited"
    for name in ("vehicle_id", "session_id"):
        assert fields[name]["logging"] == "prohibited"
    for path in inventory_paths:
        dimensions = yaml.safe_load(path.read_text())["dimensions"]
        assert next(item for item in dimensions["sensitive_fields"]
                    if item["id"] == "synthetic_location")["propagates_to"] == []
        assert "synthetic_location" not in {
            field for output in dimensions["output_schema"] for field in output["fields"]
        }


def test_owned_governance_and_gate_sources_contain_no_em_dash():
    paths = [
        *(REPO_ROOT / "governance").rglob("*"),
        REPO_ROOT / "legacy-scala/processing-inventory.yaml",
        REPO_ROOT / "rust-processor/processing-inventory.yaml",
        *(REPO_ROOT / "verification/privacy").rglob("*"),
        *(REPO_ROOT / "verification/evidence").rglob("*"),
    ]
    for path in paths:
        if path.is_file() and path.suffix != ".pyc":
            assert "\u2014" not in path.read_text(encoding="utf-8"), path
