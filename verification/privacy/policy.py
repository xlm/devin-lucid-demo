from __future__ import annotations

from verification.common import canonical
from verification.privacy.inventory import by_id, diff_dimension


def _all_changed(dimension: str, baseline: dict, candidate: dict) -> list[str]:
    delta = diff_dimension(baseline.get(dimension, []), candidate.get(dimension, []))
    return delta["added"] + delta["removed"] + delta["changed"]


def no_new_sensitive_output_fields(baseline, candidate, manifest, params):
    baseline_outputs = by_id(baseline.get("output_schema", []))
    restricted = set(params.get("restricted_classifications", []))
    restricted_names = {
        field.get("name") for field in manifest.get("fields", [])
        if field.get("classification") in restricted
    }
    findings = []
    for output in candidate.get("output_schema", []):
        old = baseline_outputs.get(output.get("id"), {})
        new_fields = set(output.get("fields", [])) - set(old.get("fields", []))
        forbidden = new_fields | (set(output.get("fields", [])) & restricted_names)
        if forbidden:
            findings.append(f"{output.get('id')}: prohibited or new fields {sorted(forbidden)}")
    return findings


def no_sensitive_location_propagation(baseline, candidate, manifest, params):
    restricted = set(params.get("restricted_classifications", []))
    return [
        f"{item.get('id')}: restricted field propagates to {item.get('propagates_to')}"
        for item in candidate.get("sensitive_fields", [])
        if item.get("classification") in restricted and item.get("propagates_to") != []
    ]


def no_identifiers_to_unapproved_destinations(baseline, candidate, manifest, params):
    identifiers = set(params.get("identifier_classifications", []))
    log_kinds = set(params.get("log_destination_kinds", []))
    before_fields = by_id(baseline.get("sensitive_fields", []))
    before_destinations = by_id(baseline.get("output_destinations", []))
    candidate_destinations = by_id(candidate.get("output_destinations", []))
    manifest_fields = {
        field.get("name"): field for field in manifest.get("fields", [])
        if field.get("topic") == "charging.events.v1"
    }
    findings = []
    for item in candidate.get("sensitive_fields", []):
        if item.get("classification") not in identifiers:
            continue
        name = item.get("id")
        previous = set(before_fields.get(name, {}).get("propagates_to", []))
        permitted = set(manifest_fields.get(name, {}).get("permitted_destinations", []))
        propagates = set(item.get("propagates_to", []))
        invalid = propagates - previous | propagates - permitted
        for destination in propagates:
            if candidate_destinations.get(destination, {}).get("kind") in log_kinds:
                invalid.add(destination)
        if invalid:
            findings.append(f"{name}: unapproved propagation destinations {sorted(invalid)}")
    for destination_id, item in candidate_destinations.items():
        allowed = set(before_destinations.get(destination_id, {}).get("identifiers", []))
        extra = set(item.get("identifiers", [])) - allowed
        if extra:
            findings.append(f"{destination_id}: unapproved identifiers {sorted(extra)}")
    return findings


def no_retention_change(baseline, candidate, manifest, params):
    return [] if not _all_changed("retention", baseline, candidate) else ["retention differs from baseline"]


def no_new_destinations(baseline, candidate, manifest, params):
    previous = by_id(baseline.get("output_destinations", []))
    return [f"new output destination: {item.get('id')}" for item in candidate.get("output_destinations", [])
            if item.get("id") not in previous]


def no_expanded_access(baseline, candidate, manifest, params):
    previous = by_id(baseline.get("access", []))
    findings = []
    for item in candidate.get("access", []):
        old = previous.get(item.get("id"))
        if old is None:
            findings.append(f"new access entry: {item.get('id')}")
        elif not set(item.get("readers", [])) <= set(old.get("readers", [])) or not set(
                item.get("writers", [])) <= set(old.get("writers", [])):
            findings.append(f"expanded access: {item.get('id')}")
    return findings


def no_altered_sensitive_transformations(baseline, candidate, manifest, params):
    sensitive_inputs = {
        field.get("name") for field in manifest.get("fields", [])
        if field.get("topic") == "charging.events.v1" and (field.get("personal") is True
                                                           or field.get("sensitive") is True)
    }
    before = by_id(baseline.get("transforms", []))
    after = by_id(candidate.get("transforms", []))
    findings = []
    for item_id in sorted(before.keys() | after.keys()):
        old = before.get(item_id)
        new = after.get(item_id)
        if (old is not None and set(old.get("inputs", [])) & sensitive_inputs
                or new is not None and set(new.get("inputs", [])) & sensitive_inputs):
            if old is None or new is None or canonical(old) != canonical(new):
                findings.append(f"sensitive transform changed: {item_id}")
    return findings


def no_relaxed_logging_restrictions(baseline, candidate, manifest, params):
    previous = by_id(baseline.get("logging", []))
    findings = []
    for item in candidate.get("logging", []):
        old = previous.get(item.get("id"))
        if old is None:
            findings.append(f"new logging entry: {item.get('id')}")
            continue
        if (not set(old.get("prohibited_content", [])) <= set(item.get("prohibited_content", []))
                or not set(item.get("allowed_content", [])) <= set(old.get("allowed_content", []))):
            findings.append(f"logging restrictions relaxed: {item.get('id')}")
    return findings


def review_new_external_dependencies(baseline, candidate, manifest, params):
    previous = by_id(baseline.get("external_deps", []))
    return [
        f"external dependency added or changed: {item.get('id')}"
        for item in candidate.get("external_deps", [])
        if item.get("id") not in previous or canonical(item) != canonical(previous[item.get("id")])
    ]


def review_replication_change(baseline, candidate, manifest, params):
    return [] if not _all_changed("replication", baseline, candidate) else ["replication differs from baseline"]


def review_deployment_boundary_change(baseline, candidate, manifest, params):
    return [] if not _all_changed("deployment_boundary", baseline, candidate) else [
        "deployment boundary differs from baseline"
    ]


def review_new_inputs_or_purposes(baseline, candidate, manifest, params):
    findings = []
    for dimension in ("inputs", "purposes", "sensitive_fields"):
        delta = diff_dimension(baseline.get(dimension, []), candidate.get(dimension, []))
        changed = delta["added"] + delta["changed"]
        if changed:
            findings.append(f"{dimension} changed: {sorted(changed)}")
    return findings


RULE_IMPLEMENTATIONS = {
    "no-new-sensitive-output-fields": no_new_sensitive_output_fields,
    "no-sensitive-location-propagation": no_sensitive_location_propagation,
    "no-identifiers-to-unapproved-destinations": no_identifiers_to_unapproved_destinations,
    "no-retention-change": no_retention_change,
    "no-new-destinations": no_new_destinations,
    "no-expanded-access": no_expanded_access,
    "no-altered-sensitive-transformations": no_altered_sensitive_transformations,
    "no-relaxed-logging-restrictions": no_relaxed_logging_restrictions,
    "review-new-external-dependencies": review_new_external_dependencies,
    "review-replication-change": review_replication_change,
    "review-deployment-boundary-change": review_deployment_boundary_change,
    "review-new-inputs-or-purposes": review_new_inputs_or_purposes,
}
