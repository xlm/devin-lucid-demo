use charging_processor::model::{format_ts, validate_line, ErrorType};

fn event(timestamp: &str, power: &str, energy: &str) -> String {
    format!(
        "{{\"schema_version\":\"1.0\",\"event_id\":\"event-1\",\
         \"vehicle_id\":\"VEH-0001\",\"session_id\":\"session-1\",\
         \"event_timestamp\":\"{timestamp}\",\"ingestion_timestamp\":\"{timestamp}\",\
         \"sequence_number\":3.0,\"charging_state\":\"CHARGING\",\
         \"battery_soc\":50,\"charging_power_kw\":{power},\
         \"energy_delivered_kwh\":{energy}}}"
    )
}

#[test]
fn validates_events_and_preserves_numeric_literals() {
    let validated = validate_line(&event(
        "2028-02-29T12:34:56.789Z",
        "12.345678901234567",
        "7",
    ))
    .unwrap();

    assert_eq!(validated.event_id, "event-1");
    assert_eq!(validated.sequence_number, 3);
    assert_eq!(
        validated.charging_power_kw.to_string(),
        "12.345678901234567"
    );
    assert_eq!(validated.energy_delivered_kwh.to_string(), "7");
}

#[test]
fn rejects_invalid_timestamps_and_round_trips_epoch_boundaries() {
    for timestamp in [
        "2026-02-29T00:00:00.000Z",
        "0000-01-01T00:00:00.000Z",
        "2028-02-29T00:00:60.000Z",
    ] {
        let error = validate_line(&event(timestamp, "1", "1")).unwrap_err();
        assert_eq!(error.error_type, ErrorType::InvalidField);
        assert_eq!(error.field, Some("event_timestamp"));
    }

    for timestamp in [
        "1970-01-01T00:00:00.000Z",
        "1969-12-31T23:59:59.999Z",
        "2028-02-29T12:34:56.789Z",
    ] {
        let timestamp_ms = validate_line(&event(timestamp, "1", "1")).unwrap().ts_ms;
        assert_eq!(format_ts(timestamp_ms), timestamp);
    }
}
