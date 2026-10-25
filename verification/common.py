"""Shared types and helpers for verification gates (frozen, see docs/build-contract.md s10)."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_STATUSES = ("pass", "fail", "error")
CHECK_STATUSES = ("pass", "fail", "skip")
VERIFICATION_KINDS = ("runtime", "static")


@dataclass
class RunContext:
    repo_root: Path = REPO_ROOT
    run_dir: Path = REPO_ROOT / "build-output" / "latest"
    scenarios: list[str] = field(default_factory=list)
    baseline: str = "legacy"  # "legacy" (run the Scala processor) or "expected" (fixtures only)
    candidate: str = "candidate"
    strict: bool = False
    tolerance: float = 1e-6
    skip_build: bool = False

    def scenario_dir(self, scenario: str) -> Path:
        return self.repo_root / "fixtures" / "scenarios" / scenario

    def expected_dir(self, scenario: str) -> Path:
        return self.repo_root / "fixtures" / "expected" / scenario

    def output_dir(self, processor: str, scenario: str) -> Path:
        return self.run_dir / "outputs" / processor / scenario

    def gate_path(self, gate: str) -> Path:
        return self.run_dir / "gates" / f"{gate}.json"

    def scenario_meta(self, scenario: str) -> dict:
        return json.loads((self.scenario_dir(scenario) / "scenario.json").read_text())


def all_scenarios(repo_root: Path = REPO_ROOT) -> list[str]:
    return sorted(p.name for p in (repo_root / "fixtures" / "scenarios").iterdir() if p.is_dir())


def canonical(record) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def check(id: str, status: str, detail: str, *, category: str, verification: str,
          scenario: str | None = None, evidence: dict | None = None) -> dict:
    assert status in CHECK_STATUSES and verification in VERIFICATION_KINDS
    return {"id": id, "status": status, "scenario": scenario, "category": category,
            "detail": detail, "evidence": evidence or {}, "verification": verification}


def gate_result(gate: str, checks: list[dict], *, error: str | None = None) -> dict:
    failed = sum(c["status"] == "fail" for c in checks)
    status = "error" if error else ("fail" if failed else "pass")
    result = {"gate": gate, "status": status,
              "summary": {"checks": len(checks), "passed": sum(c["status"] == "pass" for c in checks),
                          "failed": failed},
              "checks": checks}
    if error:
        result["error"] = error
    return result


def write_gate(ctx: RunContext, result: dict) -> dict:
    path = ctx.gate_path(result["gate"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def git_sha(repo_root: Path = REPO_ROOT) -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def git_dirty(repo_root: Path = REPO_ROOT) -> bool:
    out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo_root,
                         capture_output=True, text=True).stdout
    return bool(out.strip())
