from __future__ import annotations

from pathlib import Path

import yaml

from verification.common import canonical

DIMENSIONS = (
    "inputs",
    "sensitive_fields",
    "purposes",
    "transforms",
    "output_schema",
    "output_destinations",
    "retention",
    "access",
    "logging",
    "external_deps",
    "replication",
    "deployment_boundary",
)


def load_document(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return value


def validate_dimensions(dimensions) -> list[str]:
    problems = []
    if not isinstance(dimensions, dict):
        return ["dimensions must be an object"]
    for name in dimensions.keys() - set(DIMENSIONS):
        problems.append(f"unknown dimension: {name}")
    for name in DIMENSIONS:
        items = dimensions.get(name)
        if not isinstance(items, list):
            problems.append(f"{name} must be a list")
            continue
        ids = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                problems.append(f"{name}[{index}] must be an object")
                continue
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                problems.append(f"{name}[{index}] must have a non-empty string id")
            else:
                ids.append(item_id)
        for item_id in sorted({value for value in ids if ids.count(value) > 1}):
            problems.append(f"{name} has duplicate id: {item_id}")
    return problems


def by_id(items: list[dict]) -> dict[str, dict]:
    return {item["id"]: item for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)}


def diff_dimension(baseline: list[dict], candidate: list[dict]) -> dict:
    before = by_id(baseline)
    after = by_id(candidate)
    return {
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "changed": sorted(
            item_id for item_id in before.keys() & after.keys()
            if canonical(before[item_id]) != canonical(after[item_id])
        ),
    }


def diff_dimensions(baseline: dict, candidate: dict) -> dict:
    return {name: diff_dimension(baseline[name], candidate[name]) for name in DIMENSIONS}


def dimension_ids(dimensions: dict) -> dict[str, list[str]]:
    return {name: sorted(item["id"] for item in dimensions.get(name, []) if isinstance(item, dict)
                         and isinstance(item.get("id"), str))
            for name in DIMENSIONS}
