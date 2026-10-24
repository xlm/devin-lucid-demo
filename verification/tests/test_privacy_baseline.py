import json

from verification.common import REPO_ROOT, RunContext
from verification.privacy import content_sha256, evaluate


def _context(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    source_dir = run_dir / "src"
    source_dir.mkdir()
    return RunContext(repo_root=REPO_ROOT, run_dir=run_dir, scenarios=["normal_session"],
                      baseline="expected", candidate="candidate"), source_dir


def test_recorded_baseline_hash_and_approval_metadata(tmp_path):
    path = REPO_ROOT / "governance" / "baselines" / "charging.baseline.json"
    baseline = json.loads(path.read_text(encoding="utf-8"))
    assert baseline["content_sha256"] == content_sha256(baseline)
    approval = baseline["approval"]
    assert approval["approved_by"]
    assert approval["approved_at"]
    assert approval["scope"]
    ctx, source_dir = _context(tmp_path)
    result = evaluate(ctx, source_dir=source_dir)
    assert next(item for item in result["checks"] if item["id"] == "baseline.legacy_inventory_matches")[
        "status"
    ] == "pass"


def test_tampered_baseline_fails_integrity(tmp_path):
    ctx, source_dir = _context(tmp_path)
    path = ctx.run_dir / "tampered-baseline.json"
    baseline = json.loads((REPO_ROOT / "governance" / "baselines" / "charging.baseline.json").read_text())
    baseline["dimensions"]["retention"][0]["period"] = "P1D"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    result = evaluate(ctx, baseline_path=path, source_dir=source_dir)
    integrity = next(item for item in result["checks"] if item["id"] == "baseline.integrity")
    assert integrity["status"] == "fail"
    assert "edited without re-approval" in integrity["detail"]
    assert result["disposition"]["recommendation"] != "no-material-change"
