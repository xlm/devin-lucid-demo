"""Single verification entry point: verify, evidence, cutover (docs/build-contract.md s10)."""
from __future__ import annotations

import argparse
import importlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from verification.common import (REPO_ROOT, RunContext, all_scenarios, gate_result,  # noqa: E402
                                 git_dirty, git_sha, write_gate)

GATES = [
    ("behavioural", "verification.diff_harness"),
    ("state_ordering", "verification.diff_harness.state_ordering"),
    ("contract", "verification.contract_checks"),
    ("privacy", "verification.privacy"),
]


def build_context(args) -> RunContext:
    scenarios = args.scenario or all_scenarios()
    return RunContext(run_dir=Path(args.run_dir).resolve(), scenarios=scenarios,
                      baseline=args.baseline, strict=args.strict, tolerance=args.tolerance,
                      skip_build=args.skip_build)


def run_module_gate(ctx: RunContext, gate: str, module: str) -> dict:
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        return write_gate(ctx, gate_result(gate, [], error=f"gate not implemented: {exc}"))
    try:
        return mod.run_gate(ctx)
    except Exception as exc:  # a crashing gate is an error, never a pass
        return write_gate(ctx, gate_result(gate, [], error=f"{type(exc).__name__}: {exc}"))


def cmd_verify(args) -> int:
    ctx = build_context(args)
    if ctx.run_dir.exists() and not args.keep:
        shutil.rmtree(ctx.run_dir)
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    (ctx.run_dir / "context.json").write_text(json.dumps({
        "git_sha": git_sha(), "git_dirty": git_dirty(), "scenarios": ctx.scenarios,
        "baseline": ctx.baseline, "candidate": ctx.candidate, "strict": ctx.strict,
        "tolerance": ctx.tolerance}, indent=2) + "\n")
    try:
        runner = importlib.import_module("verification.runner")
    except ModuleNotFoundError:
        print("verification.runner not implemented yet", file=sys.stderr)
        return 1
    runner.run_all(ctx)
    results = [run_module_gate(ctx, gate, module) for gate, module in GATES if gate in args.gates]
    for r in results:
        s = r["summary"]
        print(f"{r['gate']:<15} {r['status'].upper():<6} {s['passed']}/{s['checks']} checks passed"
              + (f"  ({r['error']})" if r.get("error") else ""))
    return 0 if all(r["status"] == "pass" for r in results) else 1


def cmd_evidence(args) -> int:
    ctx = build_context(args)
    try:
        evidence = importlib.import_module("verification.evidence")
    except ModuleNotFoundError:
        print("verification.evidence not implemented yet", file=sys.stderr)
        return 1
    summary = evidence.build(ctx, out_dir=Path(args.out).resolve() if args.out else None)
    print(json.dumps(summary, indent=2))
    return 0


def cmd_cutover(args, rest: list[str]) -> int:
    try:
        cutover = importlib.import_module("verification.cutover")
    except ModuleNotFoundError:
        print("verification.cutover not implemented yet", file=sys.stderr)
        return 1
    return cutover.main(build_context(args), rest)


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--run-dir", default=str(REPO_ROOT / "build-output" / "latest"))
    common.add_argument("--scenario", action="append", help="repeatable; default all")
    common.add_argument("--baseline", choices=["legacy", "expected"], default="legacy",
                        help="legacy: run the Scala processor; expected: committed fixtures only")
    common.add_argument("--strict", action="store_true", help="exact numeric equality")
    common.add_argument("--tolerance", type=float, default=1e-6)
    common.add_argument("--skip-build", action="store_true")

    parser = argparse.ArgumentParser(prog="verification/cli.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("verify", parents=[common], help="run processors and all gates")
    v.add_argument("--keep", action="store_true", help="do not clear the run dir first")
    v.add_argument("--gates", nargs="+", default=[g for g, _ in GATES])
    e = sub.add_parser("evidence", parents=[common], help="build the evidence package")
    e.add_argument("--out", help="also copy the package here (e.g. reports/)")
    sub.add_parser("cutover", parents=[common], help="shadow/cutover/rollback simulator",
                   add_help=False)

    args, rest = parser.parse_known_args(argv)
    if args.command == "cutover":
        return cmd_cutover(args, rest)
    if rest:
        parser.error(f"unrecognized arguments: {' '.join(rest)}")
    return cmd_verify(args) if args.command == "verify" else cmd_evidence(args)


if __name__ == "__main__":
    sys.exit(main())
