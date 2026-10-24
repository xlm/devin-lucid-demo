use charging_processor::config::Rules;
use charging_processor::model::{validate_line, ErrorType};
use charging_processor::processors::charging_session::{
    ChargingSessionProcessor, ErrorRecord, Output, ProcessorCounters, SummaryRecord,
};
use charging_processor::state::{read_checkpoint, write_checkpoint_atomic, StateStore};
use serde_json::{json, Value};
use tempfile::tempdir;

#[allow(clippy::too_many_arguments)]
fn event(
    event_id: &str,
    vehicle_id: &str,
    session_id: &str,
    timestamp: &str,
    state: &str,
    sequence: &str,
    power: &str,
    energy: &str,
) -> String {
    format!(
        "{{\"schema_version\":\"1.0\",\"event_id\":{},\"vehicle_id\":{},\"session_id\":{},\
         \"event_timestamp\":{},\"ingestion_timestamp\":{},\"sequence_number\":{},\
         \"charging_state\":{},\"battery_soc\":50,\"charging_power_kw\":{},\
         \"energy_delivered_kwh\":{}}}",
        serde_json::to_string(event_id).unwrap(),
        serde_json::to_string(vehicle_id).unwrap(),
        serde_json::to_string(session_id).unwrap(),
        serde_json::to_string(timestamp).unwrap(),
        serde_json::to_string(timestamp).unwrap(),
        sequence,
        serde_json::to_string(state).unwrap(),
        power,
        energy
    )
}

fn replace_field(line: &str, field: &str, value: Value) -> String {
    let mut parsed: Value = serde_json::from_str(line).unwrap();
    parsed[field] = value;
    parsed.to_string()
}

fn at(minute: u32, millisecond: u32) -> String {
    format!("2026-01-15T08:{minute:02}:00.{millisecond:03}Z")
}

fn processor() -> ChargingSessionProcessor {
    ChargingSessionProcessor::new(Rules::CONTRACT)
}

fn summary(output: &Output) -> &SummaryRecord {
    match output {
        Output::Summary(summary) => summary,
        Output::Error(_) => panic!("expected summary"),
    }
}

fn error(output: &Output) -> &ErrorRecord {
    match output {
        Output::Error(error) => error,
        Output::Summary(_) => panic!("expected error"),
    }
}

fn collect_all(lines: &[String]) -> Vec<Output> {
    let mut processor = processor();
    let mut outputs = Vec::new();
    for (index, line) in lines.iter().enumerate() {
        outputs.extend(processor.process_line(index as u64 + 1, line));
    }
    outputs.extend(processor.finish());
    outputs
}

#[test]
fn dedupe_is_scoped_to_key_and_first_copy_wins() {
    let first = event(
        "event-1",
        "VEH-0001",
        "session-a",
        &at(0, 0),
        "CHARGING",
        "1",
        "2.5",
        "1",
    );
    let duplicate = event(
        "event-1",
        "VEH-0001",
        "session-a",
        &at(40, 0),
        "COMPLETE",
        "9",
        "99",
        "99",
    );
    let reused_id = event(
        "event-1",
        "VEH-0002",
        "session-b",
        &at(5, 0),
        "COMPLETE",
        "2",
        "3",
        "4",
    );
    let outputs = collect_all(&[first.clone(), duplicate.clone(), reused_id]);
    let summaries: Vec<_> = outputs
        .iter()
        .filter_map(|output| match output {
            Output::Summary(record) => Some(record),
            Output::Error(_) => None,
        })
        .collect();
    assert_eq!(summaries.len(), 2);
    let first_summary = summaries
        .iter()
        .find(|record| record.vehicle_id == "VEH-0001")
        .unwrap();
    assert_eq!(first_summary.event_count, 1);
    assert_eq!(first_summary.total_energy_kwh.to_string(), "1");

    let mut duplicate_processor = processor();
    duplicate_processor.process_line(1, &first);
    assert!(duplicate_processor.process_line(2, &duplicate).is_empty());
    let watermark_probe = event(
        "event-2",
        "VEH-0003",
        "session-c",
        &at(5, 0),
        "CHARGING",
        "1",
        "1",
        "1",
    );
    let probe_outputs = duplicate_processor.process_line(3, &watermark_probe);
    assert!(!probe_outputs
        .iter()
        .any(|output| matches!(output, Output::Error(_))));
    assert_eq!(
        duplicate_processor.state().max_event_ts,
        validate_line(&watermark_probe)
            .ok()
            .map(|event| event.ts_ms)
    );
    assert_eq!(
        duplicate_processor
            .state()
            .store
            .get(&("VEH-0001".to_owned(), "session-a".to_owned()))
            .unwrap()
            .seen
            .len(),
        1
    );
}

#[test]
fn counters_track_accepted_duplicates_and_rejected_events() {
    let first = event(
        "event-1",
        "VEH-0001",
        "counter-session",
        &at(0, 0),
        "CHARGING",
        "1",
        "1",
        "1",
    );
    let duplicate = event(
        "event-1",
        "VEH-0001",
        "counter-session",
        &at(1, 0),
        "COMPLETE",
        "2",
        "9",
        "9",
    );
    let complete = event(
        "event-2",
        "VEH-0001",
        "counter-session",
        &at(0, 0),
        "COMPLETE",
        "2",
        "2",
        "2",
    );
    let advance = event(
        "advance",
        "VEH-0002",
        "advance-session",
        &at(20, 0),
        "CHARGING",
        "1",
        "1",
        "1",
    );
    let closed = event(
        "event-3",
        "VEH-0001",
        "counter-session",
        &at(20, 0),
        "CHARGING",
        "3",
        "3",
        "3",
    );
    let late = event(
        "late",
        "VEH-0002",
        "advance-session",
        &at(9, 0),
        "CHARGING",
        "2",
        "2",
        "2",
    );

    let mut processor = processor();
    processor.process_line(1, &first);
    processor.process_line(2, &duplicate);
    processor.process_line(3, &complete);
    processor.process_line(4, &advance);
    processor.process_line(5, &closed);
    processor.process_line(6, &late);

    assert_eq!(
        processor.counters(),
        ProcessorCounters {
            accepted: 3,
            duplicates_dropped: 1,
        }
    );
}

#[test]
fn watermark_boundary_accepts_equality_and_drops_one_millisecond_before() {
    let lines = [
        event(
            "event-latest",
            "VEH-0001",
            "session-a",
            &at(10, 0),
            "CHARGING",
            "2",
            "4",
            "2",
        ),
        event(
            "event-boundary",
            "VEH-0001",
            "session-a",
            &at(0, 0),
            "CHARGING",
            "1",
            "3",
            "1",
        ),
        event(
            "event-late",
            "VEH-0001",
            "session-a",
            "2026-01-15T07:59:59.999Z",
            "CHARGING",
            "3",
            "100",
            "100",
        ),
    ];
    let outputs = collect_all(&lines);
    let late: Vec<_> = outputs
        .iter()
        .filter_map(|output| match output {
            Output::Error(record) => Some(record),
            Output::Summary(_) => None,
        })
        .collect();
    assert_eq!(late.len(), 1);
    assert_eq!(late[0].error_type, ErrorType::LateEventDropped);
    let record = outputs
        .iter()
        .find_map(|output| match output {
            Output::Summary(record) => Some(record),
            Output::Error(_) => None,
        })
        .unwrap();
    assert_eq!(record.event_count, 2);
    assert_eq!(record.start_ts, at(0, 0));
}

#[test]
fn inactivity_timeout_fires_at_the_exact_watermark_boundary() {
    let mut processor = processor();
    let start = event(
        "start",
        "VEH-0001",
        "session-timeout",
        &at(0, 0),
        "CHARGING",
        "1",
        "1",
        "1",
    );
    processor.process_line(1, &start);
    let one_ms_before = event(
        "advance-before",
        "VEH-0002",
        "session-before",
        "2026-01-15T08:39:59.999Z",
        "CHARGING",
        "1",
        "1",
        "1",
    );
    assert!(processor.process_line(2, &one_ms_before).is_empty());
    let boundary = event(
        "advance-boundary",
        "VEH-0003",
        "session-boundary",
        &at(40, 0),
        "CHARGING",
        "1",
        "1",
        "1",
    );
    let outputs = processor.process_line(3, &boundary);
    let fired = outputs
        .iter()
        .find_map(|output| match output {
            Output::Summary(record) if record.session_id == "session-timeout" => Some(record),
            _ => None,
        })
        .unwrap();
    assert_eq!(fired.completion_reason, "INACTIVITY_TIMEOUT");
}

#[test]
fn complete_session_fires_when_watermark_reaches_last_timestamp() {
    let mut processor = processor();
    let complete = event(
        "complete",
        "VEH-0001",
        "session-complete",
        &at(0, 0),
        "COMPLETE",
        "1",
        "7",
        "8",
    );
    assert!(processor.process_line(1, &complete).is_empty());
    let advance = event(
        "advance",
        "VEH-0002",
        "session-advance",
        &at(10, 0),
        "CHARGING",
        "1",
        "1",
        "1",
    );
    let outputs = processor.process_line(2, &advance);
    assert!(outputs.iter().any(|output| {
        matches!(output, Output::Summary(record) if record.session_id == "session-complete")
    }));
}

#[test]
fn closed_session_routes_new_events_to_error_path_with_ids() {
    let mut processor = processor();
    let complete = event(
        "event-1",
        "VEH-0001",
        "session-closed",
        &at(0, 0),
        "COMPLETE",
        "1",
        "1",
        "1",
    );
    processor.process_line(1, &complete);
    let advance = event(
        "advance",
        "VEH-0002",
        "session-advance",
        &at(10, 0),
        "CHARGING",
        "1",
        "1",
        "1",
    );
    processor.process_line(2, &advance);
    let closed = event(
        "event-2",
        "VEH-0001",
        "session-closed",
        &at(10, 0),
        "CHARGING",
        "2",
        "1",
        "1",
    );
    let outputs = processor.process_line(3, &closed);
    let record = error(&outputs[0]);
    assert_eq!(record.error_type, ErrorType::SessionAlreadyClosed);
    assert_eq!(record.event_id.as_deref(), Some("event-2"));
    assert_eq!(record.vehicle_id.as_deref(), Some("VEH-0001"));
    assert_eq!(record.session_id.as_deref(), Some("session-closed"));
    let state = processor
        .state()
        .store
        .get(&("VEH-0001".to_owned(), "session-closed".to_owned()))
        .unwrap();
    assert!(state.closed);
    assert!(state.session.is_none());
}

#[test]
fn numeric_values_are_passed_through_and_booleans_and_strings_are_rejected() {
    let precise = event(
        "precise",
        "VEH-0001",
        "precise-session",
        &at(0, 0),
        "COMPLETE",
        "3.0",
        "12.345678901234567",
        "12.345678901234567",
    );
    let integer = event(
        "integer",
        "VEH-0002",
        "integer-session",
        &at(0, 0),
        "COMPLETE",
        "4",
        "7",
        "7",
    );
    let outputs = collect_all(&[precise, integer]);
    let serialized: Vec<_> = outputs
        .iter()
        .filter_map(|output| match output {
            Output::Summary(record) => Some(serde_json::to_string(record).unwrap()),
            Output::Error(_) => None,
        })
        .collect();
    assert!(serialized
        .iter()
        .any(|line| line.contains("12.345678901234567")));
    assert!(serialized
        .iter()
        .any(|line| line.contains("\"total_energy_kwh\":7")));

    for bad_number in ["1e400", "true", "\"5.0\"", "-1"] {
        let line = event(
            "bad-number",
            "VEH-0001",
            "bad-number-session",
            &at(0, 0),
            "COMPLETE",
            "1",
            "1",
            bad_number,
        );
        let validation_error = validate_line(&line).unwrap_err();
        assert_eq!(validation_error.error_type, ErrorType::InvalidField);
        assert_eq!(validation_error.field, Some("energy_delivered_kwh"));
    }
    assert!(validate_line(&event(
        "negative-zero",
        "VEH-0001",
        "zero-session",
        &at(0, 0),
        "COMPLETE",
        "0.0",
        "-0.0",
        "-0.0"
    ))
    .is_ok());
}

#[test]
fn numeric_bounds_accept_endpoints_and_reject_values_outside_them() {
    let valid = event(
        "event",
        "VEH-0001",
        "session",
        &at(0, 0),
        "CHARGING",
        "0",
        "0",
        "0",
    );
    for soc in [json!(0), json!(100), json!(100.0)] {
        assert!(validate_line(&replace_field(&valid, "battery_soc", soc)).is_ok());
    }
    for soc in [json!(-1), json!(101)] {
        assert_eq!(
            validate_line(&replace_field(&valid, "battery_soc", soc))
                .unwrap_err()
                .field,
            Some("battery_soc")
        );
    }

    assert_eq!(
        validate_line(&replace_field(&valid, "charging_power_kw", json!(-0.001)))
            .unwrap_err()
            .field,
        Some("charging_power_kw")
    );
    for sequence in [json!(-1), json!(9_007_199_254_740_992_u64)] {
        assert_eq!(
            validate_line(&replace_field(&valid, "sequence_number", sequence))
                .unwrap_err()
                .field,
            Some("sequence_number")
        );
    }
    assert_eq!(
        validate_line(&replace_field(
            &valid,
            "sequence_number",
            json!(9_007_199_254_740_991_u64)
        ))
        .unwrap()
        .sequence_number,
        9_007_199_254_740_991
    );
}

#[test]
fn malformed_inputs_and_sequence_numeric_forms_match_contract() {
    assert_eq!(
        validate_line("").unwrap_err().error_type,
        ErrorType::MalformedJson
    );
    assert_eq!(
        validate_line("NaN").unwrap_err().error_type,
        ErrorType::MalformedJson
    );
    assert_eq!(
        validate_line("[1,2]").unwrap_err().error_type,
        ErrorType::MalformedJson
    );
    assert_eq!(
        validate_line(&event(
            "sequence-float",
            "VEH-0001",
            "sequence-session",
            &at(0, 0),
            "CHARGING",
            "3.0",
            "1",
            "1"
        ))
        .unwrap()
        .sequence_number,
        3
    );
    let fractional = event(
        "sequence-fraction",
        "VEH-0001",
        "sequence-session",
        &at(0, 0),
        "CHARGING",
        "3.5",
        "1",
        "1",
    );
    assert_eq!(
        validate_line(&fractional).unwrap_err().field,
        Some("sequence_number")
    );
}

#[test]
fn identifier_echo_is_independent_and_length_counts_unicode_scalars() {
    let bad_vehicle = event(
        "valid-event",
        "VIN12345",
        "valid-session",
        &at(0, 0),
        "CHARGING",
        "1",
        "1",
        "1",
    );
    let validation = validate_line(&bad_vehicle).unwrap_err();
    assert_eq!(validation.field, Some("vehicle_id"));
    assert_eq!(validation.ids.event_id.as_deref(), Some("valid-event"));
    assert_eq!(validation.ids.vehicle_id, None);
    assert_eq!(validation.ids.session_id.as_deref(), Some("valid-session"));

    let too_long = event(
        &"x".repeat(65),
        "VEH-0001",
        "valid-session",
        &at(0, 0),
        "CHARGING",
        "1",
        "1",
        "1",
    );
    assert_eq!(
        validate_line(&too_long).unwrap_err().field,
        Some("event_id")
    );
    let empty_id = event(
        "",
        "VEH-0001",
        "valid-session",
        &at(0, 0),
        "CHARGING",
        "1",
        "1",
        "1",
    );
    assert_eq!(
        validate_line(&empty_id).unwrap_err().field,
        Some("event_id")
    );
    let unicode_id = event(
        &"é".repeat(64),
        "VEH-0001",
        "valid-session",
        &at(0, 0),
        "CHARGING",
        "1",
        "1",
        "1",
    );
    assert!(validate_line(&unicode_id).is_ok());
}

#[test]
fn schema_vehicle_and_location_patterns_reject_unicode_and_newline_bypasses() {
    let valid = event(
        "event",
        "VEH-0123",
        "session",
        &at(0, 0),
        "CHARGING",
        "1",
        "1",
        "1",
    );
    assert!(validate_line(&valid).is_ok());

    for schema in ["١.0", "1.0\n"] {
        let line = replace_field(&valid, "schema_version", json!(schema));
        assert_eq!(
            validate_line(&line).unwrap_err().field,
            Some("schema_version")
        );
    }
    assert_eq!(
        validate_line(&replace_field(&valid, "schema_version", json!("01.0")))
            .unwrap_err()
            .error_type,
        ErrorType::UnsupportedSchemaVersion
    );
    assert_eq!(
        validate_line(&replace_field(&valid, "vehicle_id", json!("VEH-٠١٢٣")))
            .unwrap_err()
            .field,
        Some("vehicle_id")
    );

    for location in ["SYN-lower", "SYN-AB\n", "SYN-ABé", " SYN-AB"] {
        let line = replace_field(&valid, "synthetic_location", json!(location));
        assert_eq!(
            validate_line(&line).unwrap_err().field,
            Some("synthetic_location")
        );
    }
    let location = replace_field(&valid, "synthetic_location", json!("SYN-A9-B"));
    assert!(validate_line(&location).is_ok());
    let null_location = replace_field(&valid, "synthetic_location", Value::Null);
    assert!(validate_line(&null_location).is_ok());
    let mut processor = processor();
    processor.process_line(1, &location);
    let output = processor.finish();
    let summary = serde_json::to_string(&summary(&output[0])).unwrap();
    assert!(!summary.contains("synthetic_location"));
    assert!(!summary.contains("SYN-A9-B"));
}

#[test]
fn timestamps_validate_gregorian_dates_and_ascii_digits() {
    for timestamp in [
        "2026-02-29T00:00:00.000Z",
        "0000-01-01T00:00:00.000Z",
        "2028-02-29T00:00:60.000Z",
        "٢٠٢٨-02-29T00:00:00.000Z",
    ] {
        let line = event(
            "event", "VEH-0001", "session", timestamp, "CHARGING", "1", "1", "1",
        );
        assert_eq!(
            validate_line(&line).unwrap_err().field,
            Some("event_timestamp")
        );
    }
    assert!(validate_line(&event(
        "event",
        "VEH-0001",
        "session",
        "2028-02-29T00:00:00.000Z",
        "CHARGING",
        "1",
        "1",
        "1"
    ))
    .is_ok());
}

#[test]
fn emission_order_breaks_expiry_ties_by_vehicle_and_session() {
    let lines = [
        event(
            "1",
            "VEH-0002",
            "session-a",
            &at(0, 0),
            "COMPLETE",
            "1",
            "1",
            "1",
        ),
        event(
            "2",
            "VEH-0001",
            "session-z",
            &at(0, 0),
            "COMPLETE",
            "1",
            "1",
            "1",
        ),
        event(
            "3",
            "VEH-0001",
            "session-a",
            &at(0, 0),
            "COMPLETE",
            "1",
            "1",
            "1",
        ),
    ];
    let summaries: Vec<_> = collect_all(&lines)
        .iter()
        .filter_map(|output| match output {
            Output::Summary(record) => Some((record.vehicle_id.clone(), record.session_id.clone())),
            Output::Error(_) => None,
        })
        .collect();
    assert_eq!(
        summaries,
        [
            ("VEH-0001".to_owned(), "session-a".to_owned()),
            ("VEH-0001".to_owned(), "session-z".to_owned()),
            ("VEH-0002".to_owned(), "session-a".to_owned())
        ]
    );
}

#[test]
fn checkpoint_restores_state_and_resumed_outputs_match_uninterrupted_run() {
    let lines = [
        event(
            "event-1",
            "VEH-0001",
            "session-a",
            &at(0, 0),
            "CHARGING",
            "1",
            "2.5",
            "1",
        ),
        event(
            "event-2",
            "VEH-0001",
            "session-a",
            &at(2, 0),
            "COMPLETE",
            "2",
            "3.5",
            "2.5",
        ),
        event(
            "event-3",
            "VEH-0002",
            "session-b",
            &at(3, 0),
            "COMPLETE",
            "1",
            "1",
            "1",
        ),
    ];
    let uninterrupted = collect_all(&lines);
    let mut first_half = processor();
    let mut partial_outputs = first_half.process_line(1, &lines[0]);
    partial_outputs.extend(first_half.process_line(2, &lines[1]));

    let directory = tempdir().unwrap();
    let checkpoint = directory.path().join("processor.checkpoint");
    write_checkpoint_atomic(&checkpoint, first_half.state()).unwrap();
    let restored = read_checkpoint(&checkpoint).unwrap();
    assert_eq!(restored, *first_half.state());

    let mut resumed = ChargingSessionProcessor::from_state(Rules::CONTRACT, restored);
    partial_outputs.extend(resumed.process_line(3, &lines[2]));
    partial_outputs.extend(resumed.finish());
    assert_eq!(partial_outputs, uninterrupted);
}

#[test]
fn mixed_numeric_max_keeps_the_existing_value_on_ties() {
    let first = event(
        "first",
        "VEH-0001",
        "session",
        &at(0, 0),
        "CHARGING",
        "1",
        "2.0",
        "15.0",
    );
    let tie = event(
        "tie",
        "VEH-0001",
        "session",
        &at(1, 0),
        "COMPLETE",
        "2",
        "2",
        "15",
    );
    let outputs = collect_all(&[first, tie]);
    let record = outputs
        .iter()
        .find_map(|output| match output {
            Output::Summary(record) => Some(record),
            Output::Error(_) => None,
        })
        .unwrap();
    assert_eq!(record.total_energy_kwh.to_string(), "15.0");
    assert_eq!(record.peak_power_kw.to_string(), "2.0");
}

#[test]
fn schema_version_and_location_patterns_are_strict() {
    let invalid_schema = event(
        "event",
        "VEH-0001",
        "session",
        &at(0, 0),
        "CHARGING",
        "1",
        "1",
        "1",
    )
    .replace("\"schema_version\":\"1.0\"", "\"schema_version\":\"1.\"");
    assert_eq!(
        validate_line(&invalid_schema).unwrap_err().field,
        Some("schema_version")
    );

    let invalid_vehicle = event(
        "event",
        "VEH-٠٠٠١",
        "session",
        &at(0, 0),
        "CHARGING",
        "1",
        "1",
        "1",
    );
    assert_eq!(
        validate_line(&invalid_vehicle).unwrap_err().field,
        Some("vehicle_id")
    );

    let invalid_location = event(
        "event",
        "VEH-0001",
        "session",
        &at(0, 0),
        "CHARGING",
        "1",
        "1",
        "1",
    )
    .replace(
        "\"energy_delivered_kwh\":1}",
        "\"energy_delivered_kwh\":1,\"synthetic_location\":\"SYN-A\\n\"}",
    );
    assert_eq!(
        validate_line(&invalid_location).unwrap_err().field,
        Some("synthetic_location")
    );
}
