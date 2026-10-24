from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from verification.common import check
from verification.privacy.inventory import DIMENSIONS, by_id

CLASSIFICATION_FILE = "classifications.yaml"
MANIFEST_FILE = "fields/charging.yaml"
LINEAGE_FILE = "lineage/charging.lineage.yaml"
POLICY_FILE = "policies/migration-policies.yaml"
LOG_MACRO = re.compile(
    r"(?<![\w:])(?:eprintln|println|eprint|print|dbg|info|warn|error|debug|trace|"
    r"(?:log|tracing)::[A-Za-z_]\w*)!\s*\("
)
IDENTIFIER_NAME = re.compile(r"\b(vehicle_id|session_id|synthetic_location)\b")


def _result(check_id, passed, detail, *, category="static_manifest", evidence=None):
    data = {"severity": "clarification", "basis": "declared-manifest"}
    data.update(evidence or {})
    if data["basis"] == "declared-manifest" and "declarations only" not in detail:
        detail = f"declarations only: {detail}"
    return check(check_id, "pass" if passed else "fail", detail, category=category,
                 verification="static", evidence=data)


def _load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def manifest_checks(repo_root: Path, governance_dir: Path, baseline: dict,
                    candidate: dict | None) -> list[dict]:
    checks = []
    manifest_path = governance_dir / MANIFEST_FILE
    classification_path = governance_dir / CLASSIFICATION_FILE
    lineage_path = governance_dir / LINEAGE_FILE
    policy_path = governance_dir / POLICY_FILE
    try:
        manifest = _load(manifest_path)
        classifications = _load(classification_path)
        lineage = _load(lineage_path)
        policies = _load(policy_path)
        manifest_fields = manifest["fields"]
        topics = manifest["topics"]
        params = policies["parameters"]
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        detail = f"manifest, lineage, classifications, or policy data could not be read: {exc}"
        for check_id in (
            "manifest.classifications_known",
            "manifest.covers_contract_schemas",
            "manifest.restricted_fields_not_permitted",
            "manifest.identifier_fields_not_logged",
            "lineage.references_known",
            "lineage.restricted_not_propagated",
            "inventory.candidate.consistent_with_manifest",
        ):
            checks.append(_result(check_id, False, detail))
        return checks

    known_classifications = {
        item.get("id") for item in classifications.get("classifications", [])
        if isinstance(item, dict)
    }
    unknown = sorted({field.get("classification") for field in manifest_fields
                      if field.get("classification") not in known_classifications})
    checks.append(_result("manifest.classifications_known", not unknown,
                          "declared manifest classifications are known" if not unknown
                          else f"declared manifest has unknown classification ids: {unknown}",
                          evidence={"unknown": unknown}))

    schema_errors = []
    duplicate_fields = []
    fields_by_topic: dict[str, list[str]] = {}
    for field in manifest_fields:
        topic = field.get("topic")
        name = field.get("name")
        if isinstance(topic, str) and isinstance(name, str):
            fields_by_topic.setdefault(topic, []).append(name)
    for topic, names in fields_by_topic.items():
        duplicate_fields.extend(f"{topic}.{name}" for name in set(names) if names.count(name) > 1)
    for topic in topics:
        topic_id = topic.get("id")
        schema_path = repo_root / topic.get("schema", "")
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            schema_fields = set(schema["properties"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            schema_errors.append(f"{topic_id}: schema unavailable ({exc})")
            continue
        manifest_names = set(fields_by_topic.get(topic_id, []))
        if manifest_names != schema_fields:
            schema_errors.append(
                f"{topic_id}: manifest-only={sorted(manifest_names - schema_fields)}, "
                f"schema-only={sorted(schema_fields - manifest_names)}"
            )
    if duplicate_fields:
        schema_errors.append(f"duplicate topic/name pairs: {sorted(duplicate_fields)}")
    checks.append(_result("manifest.covers_contract_schemas", not schema_errors,
                          "declared manifest field names match contract schema properties"
                          if not schema_errors else "; ".join(schema_errors),
                          evidence={"errors": schema_errors, "duplicates": sorted(duplicate_fields)}))

    restricted = set(params.get("restricted_classifications", []))
    restricted_bad = sorted(
        field.get("name") for field in manifest_fields
        if field.get("classification") in restricted
        and (field.get("permitted_destinations") != [] or field.get("logging") != "prohibited")
    )
    checks.append(_result("manifest.restricted_fields_not_permitted", not restricted_bad,
                          "declared restricted fields have no permitted destinations and prohibit logging"
                          if not restricted_bad else f"restricted fields violate restrictions: {restricted_bad}",
                          evidence={"fields": restricted_bad}))

    identifiers = set(params.get("identifier_classifications", []))
    log_kinds = set(params.get("log_destination_kinds", []))
    baseline_dimensions = baseline.get("dimensions", {})
    candidate_dimensions = (candidate or {}).get("dimensions", baseline_dimensions)
    log_destination_ids = {
        item.get("id") for item in candidate_dimensions.get("output_destinations", [])
        if item.get("kind") in log_kinds
    }
    identifier_bad = sorted(
        field.get("name") for field in manifest_fields
        if field.get("classification") in identifiers
        and (field.get("logging") != "prohibited"
             or set(field.get("permitted_destinations", [])) & log_destination_ids)
    )
    checks.append(_result("manifest.identifier_fields_not_logged", not identifier_bad,
                          "declared identifier fields prohibit logging and have no log destination"
                          if not identifier_bad else f"identifier fields permit logging: {identifier_bad}",
                          evidence={"fields": identifier_bad}))

    field_paths = {f"{field['topic']}.{field['name']}" for field in manifest_fields
                   if isinstance(field.get("topic"), str) and isinstance(field.get("name"), str)}
    baseline_destination_ids = {
        item.get("id") for item in baseline_dimensions.get("output_destinations", [])
        if isinstance(item, dict)
    }
    lineage_errors = []
    for edge in lineage.get("edges", []):
        for key in ("from", "to"):
            value = edge.get(key)
            if value not in field_paths and not (key == "to" and value in baseline_destination_ids):
                lineage_errors.append(f"edge {key} reference is unknown: {value}")
    for group, key in (("generated", "field"), ("validated_only", None), ("restricted", "field")):
        for item in lineage.get(group, []):
            value = item if key is None else item.get(key)
            if value not in field_paths:
                lineage_errors.append(f"{group} reference is unknown: {value}")

    edge_from = {edge.get("from") for edge in lineage.get("edges", [])}
    edge_to = {edge.get("to") for edge in lineage.get("edges", [])}
    generated = {item.get("field") for item in lineage.get("generated", [])}
    validated_only = set(lineage.get("validated_only", []))
    restricted_paths = {item.get("field") for item in lineage.get("restricted", [])}
    for field in manifest_fields:
        path = f"{field.get('topic')}.{field.get('name')}"
        if field.get("topic") == "charging.events.v1" and path not in (
                edge_from | validated_only | restricted_paths):
            lineage_errors.append(f"input field has no lineage disposition: {path}")
        if field.get("topic") in {"charging.session.summary.v1", "charging.errors.v1"} and path not in (
                edge_to | generated):
            lineage_errors.append(f"output field has no lineage source: {path}")
    checks.append(_result("lineage.references_known", not lineage_errors,
                          "declared lineage references resolve to manifest fields or baseline destinations"
                          if not lineage_errors else "; ".join(lineage_errors),
                          evidence={"errors": lineage_errors}))

    restricted_source_fields = {
        item.get("field") for item in lineage.get("restricted", [])
    }
    propagated = sorted(
        edge.get("from") for edge in lineage.get("edges", [])
        if edge.get("from") in restricted_source_fields
    )
    checks.append(_result("lineage.restricted_not_propagated", not propagated,
                          "declared lineage has no edges from restricted fields"
                          if not propagated else f"restricted fields propagate: {propagated}",
                          evidence={"fields": propagated}))

    if candidate is not None:
        dimensions = candidate.get("dimensions", {})
        manifest_by_topic = {}
        for field in manifest_fields:
            manifest_by_topic.setdefault(field.get("topic"), set()).add(field.get("name"))
        consistency_errors = []
        for output in dimensions.get("output_schema", []):
            topic = output.get("id")
            expected = manifest_by_topic.get(topic)
            actual = set(output.get("fields", []))
            if expected is None or actual != expected:
                consistency_errors.append(
                    f"{topic}: inventory fields={sorted(actual)}, manifest fields="
                    f"{sorted(expected) if expected is not None else 'topic missing'}"
                )
        sensitive_expected = {
            field.get("name") for field in manifest_fields
            if field.get("topic") == "charging.events.v1"
            and (field.get("personal") is True or field.get("sensitive") is True)
        }
        sensitive_items = dimensions.get("sensitive_fields", [])
        sensitive_ids = {item.get("id") for item in sensitive_items}
        missing_sensitive = sorted(sensitive_expected - sensitive_ids)
        if missing_sensitive:
            consistency_errors.append(f"sensitive fields missing from inventory: {missing_sensitive}")
        manifest_input_fields = {
            field.get("name"): field for field in manifest_fields
            if field.get("topic") == "charging.events.v1"
        }
        output_destinations = by_id(dimensions.get("output_destinations", []))
        identifier_classes = set(params.get("identifier_classifications", []))
        for item in sensitive_items:
            source = manifest_input_fields.get(item.get("id"))
            if source is None:
                consistency_errors.append(f"sensitive field has no manifest input: {item.get('id')}")
                continue
            propagates_to = set(item.get("propagates_to", []))
            permitted = set(source.get("permitted_destinations", []))
            if not propagates_to <= permitted:
                consistency_errors.append(
                    f"{item.get('id')}: propagates outside manifest destinations "
                    f"{sorted(propagates_to - permitted)}"
                )
            if item.get("classification") in identifier_classes:
                for destination_id, destination in output_destinations.items():
                    if destination.get("kind") not in {"topic", "log"}:
                        continue
                    listed = item.get("id") in set(destination.get("identifiers", []))
                    propagated = destination_id in propagates_to
                    if listed != propagated:
                        consistency_errors.append(
                            f"{item.get('id')}: destination identifier and propagation disagree for "
                            f"{destination_id}"
                        )
        checks.append(_result("inventory.candidate.consistent_with_manifest",
                              not consistency_errors,
                              "declared candidate inventory is consistent with the field manifest"
                              if not consistency_errors else "; ".join(consistency_errors),
                              evidence={"errors": consistency_errors}))
    else:
        checks.append(_result(
            "inventory.candidate.consistent_with_manifest", False,
            "declared candidate inventory is unavailable",
            evidence={"errors": ["candidate inventory is unavailable"]},
        ))
    return checks


def _macro_end(text: str, open_index: int) -> int:
    depth = 0
    quote = None
    escaped = False
    index = open_index
    limit = min(len(text), open_index + 2000)
    while index < limit:
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return limit


def source_scan(source_dir: Path) -> dict:
    check_id = "source_scan.identifier_logging"
    if not source_dir.is_dir():
        return check(check_id, "skip", "candidate source not present; static claim not established",
                     category="static_source_scan", verification="static",
                     evidence={"severity": "clarification", "basis": "source-scan", "hits": []})
    hits = []
    read_errors = []
    for path in sorted(source_dir.rglob("*.rs")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            read_errors.append(f"{path}: {exc}")
            continue
        for match in LOG_MACRO.finditer(text):
            end = _macro_end(text, match.end() - 1)
            arguments = text[match.end():end]
            names = sorted(set(IDENTIFIER_NAME.findall(arguments)))
            if names:
                hits.append({
                    "file": path.relative_to(source_dir).as_posix(),
                    "line": text.count("\n", 0, match.start()) + 1,
                    "identifiers": names,
                })
    if read_errors:
        return check(check_id, "fail", "one or more candidate Rust source files could not be scanned",
                     category="static_source_scan", verification="static",
                     evidence={"severity": "clarification", "basis": "source-scan",
                               "hits": hits, "errors": read_errors})
    return check(check_id, "fail" if hits else "pass",
                 "heuristic source scan found identifier-like names in log macro arguments" if hits
                 else "heuristic source scan found no identifier-like names in log macro arguments",
                 category="static_source_scan", verification="static",
                 evidence={"severity": "clarification", "basis": "source-scan", "hits": hits})
