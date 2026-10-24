import copy
import hashlib
import html
import json
import re

from verification.common import REPO_ROOT, RunContext, canonical
from verification.evidence import PRIVACY_STATEMENT, build
from verification.privacy import evaluate


def _context(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return RunContext(
        repo_root=REPO_ROOT,
        run_dir=run_dir,
        scenarios=["normal_session"],
        baseline="expected",
        candidate="candidate",
    )


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_builds_sections_hashes_self_contained_html_and_identical_copy(tmp_path):
    ctx = _context(tmp_path)
    _write_json(ctx.run_dir / "context.json", {
        "git_sha": "run-sha",
        "git_dirty": False,
        "scenarios": ["normal_session"],
        "baseline": "expected",
        "candidate": "candidate",
        "strict": False,
        "tolerance": 1e-6,
    })
    _write_json(ctx.run_dir / "gates" / "behavioural.json", {
        "gate": "behavioural",
        "status": "pass",
        "checks": [{"id": "example", "status": "pass", "verification": "runtime",
                    "evidence": {"basis": "runtime-observed"}}],
    })
    _write_json(ctx.run_dir / "gates" / "contract.json", {
        "gate": "contract",
        "status": "fail",
        "checks": [{
            "id": "contract.example",
            "status": "fail",
            "scenario": "normal_session",
            "category": "schema",
            "detail": "<script>synthetic contract mismatch</script>",
            "verification": "static",
            "evidence": {"basis": "declared-manifest"},
        }],
    })
    privacy_result = evaluate(ctx)
    _write_json(ctx.run_dir / "gates" / "privacy.json", privacy_result)
    output_dir = ctx.output_dir("candidate", "normal_session")
    _write_json(output_dir / "run.json", {"exit_codes": {"candidate": 0}, "duration_ms": 123})
    (output_dir / "summaries.jsonl").write_text('{"session_id":"S-0012-A"}\n', encoding="utf-8")
    (output_dir / "errors.jsonl").write_text("", encoding="utf-8")
    (output_dir / "processor.log").write_text("processed synthetic records\n", encoding="utf-8")

    out_dir = tmp_path / "copy"
    result = build(ctx, out_dir=out_dir)
    evidence_dir = ctx.run_dir / "evidence"
    evidence = json.loads((evidence_dir / "evidence.json").read_text(encoding="utf-8"))

    assert set(evidence["sections"]) == set("ABCDEF")
    assert evidence["sections"]["A"]["status"] == "present"
    assert evidence["sections"]["A"]["processors"]["runs"] == [{
        "processor": "candidate",
        "scenario": "normal_session",
        "status": "present",
        "exit_codes": {"candidate": 0},
        "duration_ms": 123,
    }]
    assert evidence["sections"]["B"]["status"] == "present"
    assert evidence["sections"]["C"]["status"] == "present"
    gates = evidence["sections"]["D"]["gates"]
    assert gates["behavioural"]["status"] == "pass"
    assert gates["contract"]["failing_checks"][0]["id"] == "contract.example"
    assert gates["behavioural"]["by_basis"] == {"runtime-observed": 1}
    assert gates["behavioural"]["by_verification_kind"] == {"runtime": 1, "static": 0}
    assert gates["privacy"]["by_basis"]
    assert gates["state_ordering"]["status"] == "missing"
    assert evidence["sections"]["E"]["disposition"]["recommendation"] == (
        privacy_result["disposition"]["recommendation"]
    )
    assert "outputs/candidate/normal_session/summaries.jsonl" in evidence["sections"]["F"]["files"]
    assert "outputs/candidate/normal_session/errors.jsonl" in evidence["sections"]["F"]["files"]
    assert "outputs/candidate/normal_session/processor.log" in evidence["sections"]["F"]["files"]
    assert evidence["integrity"]["files"] == evidence["sections"]["F"]["files"]

    package = copy.deepcopy(evidence)
    package["integrity"]["package_sha256"] = None
    expected_package_hash = hashlib.sha256(canonical(package).encode("utf-8")).hexdigest()
    assert evidence["integrity"]["package_sha256"] == expected_package_hash
    assert result["package_sha256"] == expected_package_hash
    for relative_path, digest in evidence["integrity"]["files"].items():
        assert _checksum(ctx.run_dir / relative_path) == digest

    checksum_lines = (evidence_dir / "evidence.sha256").read_text(encoding="utf-8").splitlines()
    checksums = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in checksum_lines}
    assert set(checksums) == {"evidence.json", "evidence.md", "evidence.html"}
    for name, digest in checksums.items():
        assert _checksum(evidence_dir / name) == digest

    html_text = (evidence_dir / "evidence.html").read_text(encoding="utf-8")
    markdown = (evidence_dir / "evidence.md").read_text(encoding="utf-8")
    for rendered in (evidence, markdown):
        text = json.dumps(rendered, ensure_ascii=False) if isinstance(rendered, dict) else rendered
        assert PRIVACY_STATEMENT in text
        assert "\u2014" not in text
    assert PRIVACY_STATEMENT in html.unescape(html_text)
    assert "\u2014" not in html_text
    assert "http://" not in html_text
    assert "https://" not in html_text
    assert not re.search(r"<script\b", html_text, re.IGNORECASE)
    assert "&lt;script&gt;synthetic contract mismatch&lt;/script&gt;" in html_text
    assert not re.search(r"<link\b", html_text, re.IGNORECASE)
    assert expected_package_hash in markdown and expected_package_hash in html_text
    assert _checksum(evidence_dir / "evidence.json") in markdown
    assert _checksum(evidence_dir / "evidence.json") in html_text
    for name in result["files"]:
        assert (out_dir / name).read_bytes() == (evidence_dir / name).read_bytes()


def test_reports_unreadable_gate_and_missing_context(tmp_path):
    ctx = _context(tmp_path)
    _write_json(ctx.run_dir / "gates" / "state_ordering.json", ["not", "an", "object"])
    result = build(ctx)
    evidence = json.loads((ctx.run_dir / "evidence" / "evidence.json").read_text(encoding="utf-8"))
    assert result["gates"]["state_ordering"] == "unreadable"
    assert evidence["sections"]["D"]["gates"]["state_ordering"]["status"] == "unreadable"
    assert evidence["sections"]["A"]["status"] == "missing"
    assert evidence["sections"]["E"]["disposition"]["recommendation"] == "needs-clarification"
    assert "privacy gate missing" in evidence["sections"]["E"]["disposition"]["reasons"]["clarification"]
    assert evidence["sections"]["F"]["file_statuses"]["context.json"] == "missing"
    assert evidence["sections"]["F"]["file_statuses"]["gates/state_ordering.json"] == "unreadable"


def test_unreadable_privacy_gate_is_not_reported_as_missing_or_pass(tmp_path):
    ctx = _context(tmp_path)
    (ctx.run_dir / "context.json").write_text("{broken", encoding="utf-8")
    privacy_path = ctx.run_dir / "gates" / "privacy.json"
    privacy_path.parent.mkdir(parents=True)
    privacy_path.write_text("{broken", encoding="utf-8")

    result = build(ctx)
    evidence = json.loads((ctx.run_dir / "evidence" / "evidence.json").read_text(encoding="utf-8"))
    assert result["gates"]["privacy"] == "unreadable"
    assert evidence["sections"]["B"]["status"] == "unreadable"
    assert evidence["sections"]["C"]["status"] == "unreadable"
    assert evidence["sections"]["E"]["disposition"]["reasons"]["clarification"] == [
        "privacy gate unreadable"
    ]
