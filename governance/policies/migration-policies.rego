# Optional illustrative material for OPA users. This is not executed in the
# demo, and behavior is not guaranteed to match the authoritative Python
# checker. Rule ids and intent are mirrored here.
package charging.migration

import rego.v1

restricted_names contains field.name if {
	some field in input.manifest.fields
	field.classification in input.params.restricted_classifications
}

baseline_output_contains(output_id, field_name) if {
	some output in input.baseline.output_schema
	output.id == output_id
	field_name in output.fields
}

baseline_sensitive_allows(field_id, destination_id) if {
	some field in input.baseline.sensitive_fields
	field.id == field_id
	destination_id in field.propagates_to
}

manifest_allows(field_id, destination_id) if {
	some field in input.manifest.fields
	field.topic == "charging.events.v1"
	field.name == field_id
	destination_id in field.permitted_destinations
}

baseline_destination_contains(destination_id, identifier) if {
	some destination in input.baseline.output_destinations
	destination.id == destination_id
	identifier in destination.identifiers
}

baseline_access_contains(access_id, reader, writer) if {
	some item in input.baseline.access
	item.id == access_id
	reader in item.readers
	writer in item.writers
}

baseline_transform_contains(transform_id, transform) if {
	some item in input.baseline.transforms
	item.id == transform_id
	item == transform
}

violation contains {"rule": "no-new-sensitive-output-fields", "msg": output.id} if {
	some output in input.candidate.output_schema
	some field_name in output.fields
	not baseline_output_contains(output.id, field_name)
}

violation contains {"rule": "no-new-sensitive-output-fields", "msg": output.id} if {
	some output in input.candidate.output_schema
	some field_name in output.fields
	field_name in restricted_names
}

violation contains {"rule": "no-sensitive-location-propagation", "msg": field.id} if {
	some field in input.candidate.sensitive_fields
	field.classification in input.params.restricted_classifications
	count(field.propagates_to) > 0
}

violation contains {"rule": "no-identifiers-to-unapproved-destinations", "msg": field.id} if {
	some field in input.candidate.sensitive_fields
	field.classification in input.params.identifier_classifications
	some destination in field.propagates_to
	not baseline_sensitive_allows(field.id, destination)
}

violation contains {"rule": "no-identifiers-to-unapproved-destinations", "msg": field.id} if {
	some field in input.candidate.sensitive_fields
	field.classification in input.params.identifier_classifications
	some destination in field.propagates_to
	not manifest_allows(field.id, destination)
}

violation contains {"rule": "no-identifiers-to-unapproved-destinations", "msg": destination.id} if {
	some destination in input.candidate.output_destinations
	some identifier in destination.identifiers
	not baseline_destination_contains(destination.id, identifier)
}

violation contains {"rule": "no-identifiers-to-unapproved-destinations", "msg": field.id} if {
	some field in input.candidate.sensitive_fields
	field.classification in input.params.identifier_classifications
	some destination in input.candidate.output_destinations
	destination.id in field.propagates_to
	destination.kind in input.params.log_destination_kinds
}

violation contains {"rule": "no-retention-change", "msg": "retention differs"} if {
	input.candidate.retention != input.baseline.retention
}

violation contains {"rule": "no-new-destinations", "msg": destination.id} if {
	some destination in input.candidate.output_destinations
	not some baseline in input.baseline.output_destinations
	baseline.id == destination.id
}

violation contains {"rule": "no-expanded-access", "msg": access.id} if {
	some access in input.candidate.access
	not some baseline in input.baseline.access
	baseline.id == access.id
}

violation contains {"rule": "no-expanded-access", "msg": access.id} if {
	some access in input.candidate.access
	some reader in access.readers
	not baseline_access_contains(access.id, reader, access.writers[0])
}

violation contains {"rule": "no-altered-sensitive-transformations", "msg": transform.id} if {
	some transform in input.candidate.transforms
	some field in input.manifest.fields
	field.topic == "charging.events.v1"
	field.name in transform.inputs
	field.personal == true
	not baseline_transform_contains(transform.id, transform)
}

violation contains {"rule": "no-altered-sensitive-transformations", "msg": transform.id} if {
	some transform in input.candidate.transforms
	some field in input.manifest.fields
	field.topic == "charging.events.v1"
	field.name in transform.inputs
	field.sensitive == true
	not baseline_transform_contains(transform.id, transform)
}

violation contains {"rule": "no-relaxed-logging-restrictions", "msg": item.id} if {
	some item in input.candidate.logging
	not some baseline in input.baseline.logging
	baseline.id == item.id
}

violation contains {"rule": "no-relaxed-logging-restrictions", "msg": item.id} if {
	some item in input.candidate.logging
	some baseline in input.baseline.logging
	baseline.id == item.id
	some forbidden in baseline.prohibited_content
	not forbidden in item.prohibited_content
}

violation contains {"rule": "no-relaxed-logging-restrictions", "msg": item.id} if {
	some item in input.candidate.logging
	some baseline in input.baseline.logging
	baseline.id == item.id
	some allowed in item.allowed_content
	not allowed in baseline.allowed_content
}

review contains {"rule": "review-new-external-dependencies", "msg": item.id} if {
	some item in input.candidate.external_deps
	not some baseline in input.baseline.external_deps
	baseline == item
}

review contains {"rule": "review-replication-change", "msg": "replication differs"} if {
	input.candidate.replication != input.baseline.replication
}

review contains {"rule": "review-deployment-boundary-change", "msg": "deployment boundary differs"} if {
	input.candidate.deployment_boundary != input.baseline.deployment_boundary
}

review contains {"rule": "review-new-inputs-or-purposes", "msg": dimension} if {
	some dimension in {"inputs", "purposes", "sensitive_fields"}
	some item in input.candidate[dimension]
	not some baseline in input.baseline[dimension]
	baseline.id == item.id
}

review contains {"rule": "review-new-inputs-or-purposes", "msg": dimension} if {
	some dimension in {"inputs", "purposes", "sensitive_fields"}
	some item in input.candidate[dimension]
	some baseline in input.baseline[dimension]
	baseline.id == item.id
	baseline != item
}
