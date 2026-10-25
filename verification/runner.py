"""Run registered processors and record outputs in a verification run directory.

The processors.override.yaml file is a test hook. It replaces processor
entries for the current run and is not part of the frozen registry.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

RUN_TIMEOUT_S = 60
BUILD_TIMEOUT_S = 1800
REQUIRED_TOOLS = {"legacy": ["java"]}


def _load_registry(ctx) -> tuple[dict, set[str]]:
    registry_path = ctx.repo_root / "processors.yaml"
    processors = yaml.safe_load(registry_path.read_text())["processors"]
    override_path = ctx.run_dir / "processors.override.yaml"
    override_names = set()
    if override_path.is_file():
        overrides = yaml.safe_load(override_path.read_text()) or {}
        for name, entry in overrides.get("processors", {}).items():
            processors[name] = entry
            override_names.add(name)
    return processors, override_names


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _build(ctx, name: str, config: dict, overridden: bool) -> dict:
    output_dir = ctx.run_dir / "outputs" / name
    output_dir.mkdir(parents=True, exist_ok=True)
    unavailable_path = output_dir / "UNAVAILABLE.json"
    if unavailable_path.exists():
        unavailable_path.unlink()
    log_path = output_dir / "build.log"
    command = config.get("build", [])
    required = config.get("requires", []) if overridden else \
        config.get("requires", REQUIRED_TOOLS.get(name, []))
    missing = next((tool for tool in required if shutil.which(tool) is None), None)
    started = time.monotonic()
    status = "ok"
    exit_code = 0
    error = None
    with log_path.open("w", encoding="utf-8") as log:
        if missing is not None:
            status = "failed"
            exit_code = None
            error = f"{missing} not found on PATH"
        elif ctx.skip_build:
            status = "skipped"
            exit_code = None
        else:
            try:
                process = subprocess.run(
                    command,
                    cwd=ctx.repo_root,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=BUILD_TIMEOUT_S,
                    check=False,
                )
                exit_code = process.returncode
                if exit_code != 0:
                    status = "failed"
                    error = f"build exited with code {exit_code}"
            except subprocess.TimeoutExpired:
                status = "failed"
                exit_code = None
                error = f"timeout after {BUILD_TIMEOUT_S} s"
            except OSError as exc:
                status = "failed"
                exit_code = None
                error = f"launch failed: {type(exc).__name__} {exc}"
    record = {
        "processor": name,
        "status": status,
        "command": command,
        "exit_code": exit_code,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "error": error,
    }
    _write_json(output_dir / "build.json", record)
    return record


def _append_stderr(path: Path, stderr) -> None:
    if isinstance(stderr, bytes):
        text = stderr.decode(errors="replace")
    else:
        text = stderr or ""
    with path.open("a", encoding="utf-8") as log:
        log.write(text)


def _run_command(command: list[str], cwd: Path, log_path: Path) -> tuple[int | None,
                                                                         int, str | None]:
    started = time.monotonic()
    try:
        with log_path.open("a", encoding="utf-8") as log:
            process = subprocess.run(
                command,
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=log,
                timeout=RUN_TIMEOUT_S,
                check=False,
            )
        return process.returncode, int((time.monotonic() - started) * 1000), None
    except subprocess.TimeoutExpired as exc:
        _append_stderr(log_path, exc.stderr)
        return None, int((time.monotonic() - started) * 1000), \
            f"timeout after {RUN_TIMEOUT_S} s"
    except OSError as exc:
        return None, int((time.monotonic() - started) * 1000), \
            f"launch failed: {type(exc).__name__} {exc}"


def _run_scenario(ctx, name: str, config: dict, scenario: str) -> None:
    output_dir = ctx.output_dir(name, scenario)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    log_path = output_dir / "processor.log"
    log_path.touch()
    scenario_input = ctx.scenario_dir(scenario) / "input.jsonl"
    meta = ctx.scenario_meta(scenario)
    command = config["command"] + [
        "replay",
        "--input", str(scenario_input),
        "--output-dir", str(output_dir),
    ]
    mode = meta["run"]["mode"]
    commands = []
    exit_codes = []
    error = None
    duration_ms = 0
    checkpoint_written = None
    if mode == "restart":
        checkpoint = ctx.run_dir / "checkpoints" / name / f"{scenario}.ckpt"
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        if checkpoint.exists():
            checkpoint.unlink()
        first = command + [
            "--stop-after", str(meta["run"]["stop_after"]),
            "--checkpoint", str(checkpoint),
        ]
        commands.append(first)
        code, duration, error = _run_command(first, ctx.repo_root, log_path)
        exit_codes.append(code)
        duration_ms += duration
        checkpoint_written = checkpoint.is_file()
        if code == 0:
            if checkpoint_written:
                second = command + ["--resume-from", str(checkpoint)]
                commands.append(second)
                code, duration, error = _run_command(second, ctx.repo_root, log_path)
                exit_codes.append(code)
                duration_ms += duration
            else:
                error = "checkpoint not written after --stop-after"
    else:
        commands.append(command)
        code, duration_ms, error = _run_command(command, ctx.repo_root, log_path)
        exit_codes.append(code)
    _write_json(output_dir / "run.json", {
        "processor": name,
        "scenario": scenario,
        "mode": mode,
        "commands": commands,
        "exit_codes": exit_codes,
        "duration_ms": duration_ms,
        "error": error,
        "checkpoint_written": checkpoint_written,
    })


def run_all(ctx) -> None:
    processors, overridden = _load_registry(ctx)
    names = [ctx.candidate]
    if ctx.baseline == "legacy":
        names.append("legacy")
    build_records = {}
    for name in names:
        config = processors[name]
        build_records[name] = _build(ctx, name, config, name in overridden)
    if "legacy" in build_records and build_records["legacy"]["status"] == "failed":
        legacy_build = build_records["legacy"]
        unavailable = {
            "processor": "legacy",
            "reason": legacy_build["error"] or "legacy build failed",
            "fallback_baseline": "expected",
        }
        _write_json(ctx.run_dir / "outputs" / "legacy" / "UNAVAILABLE.json", unavailable)
        print(f"legacy unavailable: {unavailable['reason']}", file=sys.stderr)
    if ctx.candidate not in build_records:
        return
    candidate_build = build_records[ctx.candidate]
    if candidate_build["status"] == "failed":
        print(f"candidate build failed: {candidate_build['error']}", file=sys.stderr)
        return
    for name in names:
        build_record = build_records[name]
        if name == "legacy" and build_record["status"] == "failed":
            continue
        for scenario in ctx.scenarios:
            _run_scenario(ctx, name, processors[name], scenario)
