"""Markdown summary for the behavioural gate."""
from __future__ import annotations

import json
from pathlib import Path

from verification.diff_harness.comparator import CATEGORIES
from verification.diff_harness.runs import baseline_selection


def write_markdown(ctx, result: dict) -> Path:
    selection = baseline_selection(ctx)
    lines = [
        "# Behavioural verification report",
        "",
        f"- Baseline used: `{selection['used']}`",
        f"- Baseline reason: {selection['reason']}",
        f"- Absolute tolerance: `{ctx.tolerance}`",
        f"- Strict numeric comparison: `{str(ctx.strict).lower()}`",
        "",
        "## Failing categories by scenario and pair",
        "",
        "| Scenario | Pair | Failing categories |",
        "| --- | --- | --- |",
    ]
    rows = []
    for item in result.get("checks", []):
        if item.get("status") != "fail" or item.get("category") not in CATEGORIES:
            continue
        parts = item["id"].split(":")
        if len(parts) < 3:
            continue
        scenario, pair = parts[0], parts[1]
        rows.append((scenario, pair, item["category"],
                     item.get("evidence", {}).get("findings_total", 0)))
    if rows:
        for scenario, pair, category, count in rows:
            lines.append(f"| `{scenario}` | `{pair}` | `{category}` ({count}) |")
    else:
        lines.append("| None | None | None |")

    lines.extend(("", "## Failing check details", ""))
    failures = [item for item in result.get("checks", [])
                if item.get("status") == "fail"]
    if failures:
        for item in failures:
            lines.extend((
                f"### `{item['id']}`",
                "",
                f"- Category: `{item['category']}`",
                f"- Detail: {item['detail']}",
            ))
            evidence = item.get("evidence", {})
            findings = evidence.get("findings", [])
            if findings:
                lines.extend(("", "```json",
                              json.dumps(findings, indent=2, sort_keys=True),
                              "```"))
            lines.append("")
    else:
        lines.append("No failing checks.")

    lines.extend((
        "## What this gate does not assert",
        "",
        "This gate does not assert global or cross-key order, or per-vehicle order across sessions.",
        "It does not assert wall-clock timing or exactly-once processing. Restart runs are "
        "at-least-once per contract section 7. A crash between output flush and checkpoint "
        "can re-emit output.",
        "Passing checks are not production safety and do not remove privacy-review obligations.",
        "",
    ))
    path = ctx.run_dir / "gates" / "behavioural.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
