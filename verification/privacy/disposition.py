from __future__ import annotations

RECOMMENDATIONS = (
    "policy-violation",
    "new-processing-requires-review",
    "needs-clarification",
    "no-material-change",
)

STATEMENT = (
    "Automated recommendation for a human privacy reviewer. It is not a legal or compliance "
    "determination, does not approve cutover and does not discharge privacy-review obligations."
)


def recommend(checks: list[dict], error: str | None = None) -> dict:
    reasons = {
        severity: [
            item["id"] for item in checks
            if (
                item["status"] == "fail"
                or severity == "clarification" and item["status"] == "skip"
            ) and item.get("evidence", {}).get("severity") == severity
        ]
        for severity in ("violation", "review", "clarification")
    }
    if any(reasons["violation"]):
        recommendation = "policy-violation"
    elif any(reasons["review"]):
        recommendation = "new-processing-requires-review"
    elif error or any(
        item["status"] in ("fail", "skip")
        and item.get("evidence", {}).get("severity") == "clarification"
        for item in checks
    ):
        recommendation = "needs-clarification"
    else:
        recommendation = "no-material-change"
    actions = [
        "A privacy reviewer reviews this package and decides what action is appropriate.",
        "ReadyForReview -> ApprovedForCutover is a human action.",
        "Only humans change baselines and policies.",
    ]
    if recommendation == "policy-violation":
        actions.insert(0, "A human reviewer assesses each violation and determines remediation.")
    elif recommendation == "new-processing-requires-review":
        actions.insert(0, "A human reviewer reviews the processing changes before any approval decision.")
    elif recommendation == "needs-clarification":
        actions.insert(0, "A human reviewer resolves missing, skipped, or unclear evidence.")
    return {"recommendation": recommendation, "reasons": reasons, "statement": STATEMENT,
            "required_human_actions": actions}
