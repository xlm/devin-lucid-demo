"""Read and interpret the runner's durable records."""
from __future__ import annotations

import json


def _read_json(path):
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def build_status(ctx, proc: str) -> dict | None:
    return _read_json(ctx.run_dir / "outputs" / proc / "build.json")


def legacy_unavailable(ctx) -> dict | None:
    return _read_json(ctx.run_dir / "outputs" / "legacy" / "UNAVAILABLE.json")


def run_record(ctx, proc: str, scenario: str) -> dict | None:
    return _read_json(ctx.output_dir(proc, scenario) / "run.json")


def run_ok(record: dict | None) -> bool:
    if record is None or record.get("error") is not None:
        return False
    commands = record.get("commands")
    exit_codes = record.get("exit_codes")
    if not isinstance(commands, list) or not isinstance(exit_codes, list):
        return False
    if not commands or not exit_codes or any(code != 0 for code in exit_codes):
        return False
    return record.get("mode") != "restart" or len(commands) == 2


def baseline_selection(ctx) -> dict:
    requested = ctx.baseline
    if requested != "legacy":
        return {
            "requested": requested,
            "used": "expected",
            "reason": "expected baseline requested",
        }
    unavailable = legacy_unavailable(ctx)
    if unavailable is not None:
        reason = unavailable.get("reason", "legacy processor unavailable")
    else:
        status = build_status(ctx, "legacy")
        if status is None:
            reason = "legacy build record missing"
        elif status.get("status") == "failed":
            reason = status.get("error") or "legacy build failed"
        else:
            return {
                "requested": requested,
                "used": "legacy",
                "reason": "legacy build succeeded or was skipped",
            }
    return {"requested": requested, "used": "expected", "reason": reason}
