use std::cmp::Ordering;

use serde::{Deserialize, Serialize};
use serde_json::{Number, Value};

const MAX_SEQUENCE_NUMBER: i128 = 9_007_199_254_740_991;
const ID_MAX_LEN: usize = 64;
const MILLIS_PER_DAY: i64 = 86_400_000;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct EchoIds {
    pub event_id: Option<String>,
    pub vehicle_id: Option<String>,
    pub session_id: Option<String>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub enum ErrorType {
    #[serde(rename = "MALFORMED_JSON")]
    MalformedJson,
    #[serde(rename = "MISSING_FIELD")]
    MissingField,
    #[serde(rename = "INVALID_FIELD")]
    InvalidField,
    #[serde(rename = "UNSUPPORTED_SCHEMA_VERSION")]
    UnsupportedSchemaVersion,
    #[serde(rename = "LATE_EVENT_DROPPED")]
    LateEventDropped,
    #[serde(rename = "SESSION_ALREADY_CLOSED")]
    SessionAlreadyClosed,
}

impl ErrorType {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::MalformedJson => "MALFORMED_JSON",
            Self::MissingField => "MISSING_FIELD",
            Self::InvalidField => "INVALID_FIELD",
            Self::UnsupportedSchemaVersion => "UNSUPPORTED_SCHEMA_VERSION",
            Self::LateEventDropped => "LATE_EVENT_DROPPED",
            Self::SessionAlreadyClosed => "SESSION_ALREADY_CLOSED",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ValidationError {
    pub error_type: ErrorType,
    pub field: Option<&'static str>,
    pub ids: EchoIds,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ValidatedEvent {
    pub event_id: String,
    pub vehicle_id: String,
    pub session_id: String,
    pub ts_ms: i64,
    pub sequence_number: u64,
    pub charging_state: String,
    pub charging_power_kw: Number,
    pub energy_delivered_kwh: Number,
}

#[derive(Clone, Copy, Debug)]
enum Num {
    Int(i128),
    Float(f64),
}

impl Num {
    fn from_number(number: &Number) -> Option<Self> {
        if let Some(value) = number.as_u64() {
            Some(Self::Int(i128::from(value)))
        } else if let Some(value) = number.as_i64() {
            Some(Self::Int(i128::from(value)))
        } else {
            number
                .as_f64()
                .filter(|value| value.is_finite())
                .map(Self::Float)
        }
    }

    fn as_f64(self) -> f64 {
        match self {
            Self::Int(value) => value as f64,
            Self::Float(value) => value,
        }
    }

    fn cmp(self, other: Self) -> Ordering {
        match (self, other) {
            (Self::Int(left), Self::Int(right)) => left.cmp(&right),
            _ => self
                .as_f64()
                .partial_cmp(&other.as_f64())
                .unwrap_or(Ordering::Equal),
        }
    }
}

/// Parses, validates and extracts the fields used by session processing.
pub fn validate_line(line: &str) -> Result<ValidatedEvent, ValidationError> {
    let value: Value = serde_json::from_str(line).map_err(|_| malformed_error())?;
    let Some(object) = value.as_object() else {
        return Err(malformed_error());
    };
    let ids = echo_ids(object);

    let schema_version = required(object, "schema_version", &ids)?;
    let Some(schema_version) = schema_version.as_str() else {
        return Err(invalid_error("schema_version", &ids));
    };
    let Some((major, minor)) = schema_version.split_once('.') else {
        return Err(invalid_error("schema_version", &ids));
    };
    if major.is_empty()
        || minor.is_empty()
        || schema_version.matches('.').count() != 1
        || !schema_version
            .bytes()
            .all(|byte| byte == b'.' || byte.is_ascii_digit())
    {
        return Err(invalid_error("schema_version", &ids));
    }
    if major != "1" {
        return Err(ValidationError {
            error_type: ErrorType::UnsupportedSchemaVersion,
            field: Some("schema_version"),
            ids,
        });
    }

    let event_id = required(object, "event_id", &ids)?
        .as_str()
        .filter(|value| valid_id(value))
        .ok_or_else(|| invalid_error("event_id", &ids))?
        .to_owned();

    let vehicle_id = required(object, "vehicle_id", &ids)?
        .as_str()
        .filter(|value| is_vehicle_id(value))
        .ok_or_else(|| invalid_error("vehicle_id", &ids))?
        .to_owned();

    let session_id = required(object, "session_id", &ids)?
        .as_str()
        .filter(|value| valid_id(value))
        .ok_or_else(|| invalid_error("session_id", &ids))?
        .to_owned();

    let ts_ms = parse_timestamp(
        required(object, "event_timestamp", &ids)?
            .as_str()
            .ok_or_else(|| invalid_error("event_timestamp", &ids))?,
    )
    .ok_or_else(|| invalid_error("event_timestamp", &ids))?;

    parse_timestamp(
        required(object, "ingestion_timestamp", &ids)?
            .as_str()
            .ok_or_else(|| invalid_error("ingestion_timestamp", &ids))?,
    )
    .ok_or_else(|| invalid_error("ingestion_timestamp", &ids))?;

    let sequence_number = required(object, "sequence_number", &ids)?
        .as_number()
        .and_then(sequence_number)
        .ok_or_else(|| invalid_error("sequence_number", &ids))?;

    let charging_state = required(object, "charging_state", &ids)?
        .as_str()
        .filter(|value| matches!(*value, "IDLE" | "CHARGING" | "COMPLETE" | "FAULT"))
        .ok_or_else(|| invalid_error("charging_state", &ids))?
        .to_owned();

    let _battery_soc = required(object, "battery_soc", &ids)?
        .as_number()
        .and_then(Num::from_number)
        .filter(|value| {
            value.cmp(Num::Int(0)) != Ordering::Less
                && value.cmp(Num::Int(100)) != Ordering::Greater
        })
        .ok_or_else(|| invalid_error("battery_soc", &ids))?;
    let charging_power_kw = nonnegative_number(object, "charging_power_kw", &ids)?;
    let energy_delivered_kwh = nonnegative_number(object, "energy_delivered_kwh", &ids)?;

    if let Some(location) = object
        .get("synthetic_location")
        .filter(|value| !value.is_null())
    {
        if !location.as_str().is_some_and(is_synthetic_location) {
            return Err(invalid_error("synthetic_location", &ids));
        }
    }

    Ok(ValidatedEvent {
        event_id,
        vehicle_id,
        session_id,
        ts_ms,
        sequence_number,
        charging_state,
        charging_power_kw,
        energy_delivered_kwh,
    })
}

pub fn format_ts(timestamp_ms: i64) -> String {
    let days = timestamp_ms.div_euclid(MILLIS_PER_DAY);
    let day_ms = timestamp_ms.rem_euclid(MILLIS_PER_DAY);
    let (year, month, day) = civil_from_days(days);
    let hour = day_ms / 3_600_000;
    let minute = (day_ms % 3_600_000) / 60_000;
    let second = (day_ms % 60_000) / 1_000;
    let millisecond = day_ms % 1_000;

    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{millisecond:03}Z")
}

fn required<'a>(
    object: &'a serde_json::Map<String, Value>,
    name: &'static str,
    ids: &EchoIds,
) -> Result<&'a Value, ValidationError> {
    match object.get(name) {
        Some(value) if !value.is_null() => Ok(value),
        _ => Err(ValidationError {
            error_type: ErrorType::MissingField,
            field: Some(name),
            ids: ids.clone(),
        }),
    }
}

fn echo_ids(object: &serde_json::Map<String, Value>) -> EchoIds {
    let id = |name: &str| {
        object
            .get(name)
            .and_then(Value::as_str)
            .filter(|value| valid_id(value))
            .map(str::to_owned)
    };
    EchoIds {
        event_id: id("event_id"),
        vehicle_id: object
            .get("vehicle_id")
            .and_then(Value::as_str)
            .filter(|value| is_vehicle_id(value))
            .map(str::to_owned),
        session_id: id("session_id"),
    }
}

fn malformed_error() -> ValidationError {
    ValidationError {
        error_type: ErrorType::MalformedJson,
        field: None,
        ids: EchoIds {
            event_id: None,
            vehicle_id: None,
            session_id: None,
        },
    }
}

fn invalid_error(field: &'static str, ids: &EchoIds) -> ValidationError {
    ValidationError {
        error_type: ErrorType::InvalidField,
        field: Some(field),
        ids: ids.clone(),
    }
}

fn valid_id(value: &str) -> bool {
    let length = value.chars().count();
    (1..=ID_MAX_LEN).contains(&length)
}

fn is_vehicle_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 8 && &bytes[..4] == b"VEH-" && bytes[4..].iter().all(u8::is_ascii_digit)
}

fn is_synthetic_location(value: &str) -> bool {
    value
        .strip_prefix("SYN-")
        .is_some_and(|suffix| !suffix.is_empty() && suffix.bytes().all(is_location_byte))
}

fn is_location_byte(byte: u8) -> bool {
    byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'-'
}

fn sequence_number(number: &Number) -> Option<u64> {
    match Num::from_number(number)? {
        Num::Int(value) if (0..=MAX_SEQUENCE_NUMBER).contains(&value) => u64::try_from(value).ok(),
        Num::Float(value)
            if value >= 0.0 && value <= MAX_SEQUENCE_NUMBER as f64 && value.fract() == 0.0 =>
        {
            Some(value as u64)
        }
        _ => None,
    }
}

fn nonnegative_number(
    object: &serde_json::Map<String, Value>,
    name: &'static str,
    ids: &EchoIds,
) -> Result<Number, ValidationError> {
    let value = required(object, name, ids)?;
    let number = value
        .as_number()
        .filter(|number| {
            Num::from_number(number).is_some_and(|value| value.cmp(Num::Int(0)) != Ordering::Less)
        })
        .ok_or_else(|| invalid_error(name, ids))?;
    Ok(number.clone())
}

fn parse_timestamp(value: &str) -> Option<i64> {
    let bytes = value.as_bytes();
    if bytes.len() != 24
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || bytes[10] != b'T'
        || bytes[13] != b':'
        || bytes[16] != b':'
        || bytes[19] != b'.'
        || bytes[23] != b'Z'
    {
        return None;
    }
    for (index, byte) in bytes.iter().enumerate() {
        if matches!(index, 4 | 7 | 10 | 13 | 16 | 19 | 23) {
            continue;
        }
        if !byte.is_ascii_digit() {
            return None;
        }
    }

    let year = decimal(&bytes[0..4])? as i64;
    let month = decimal(&bytes[5..7])? as i64;
    let day = decimal(&bytes[8..10])? as i64;
    let hour = decimal(&bytes[11..13])? as i64;
    let minute = decimal(&bytes[14..16])? as i64;
    let second = decimal(&bytes[17..19])? as i64;
    let millisecond = decimal(&bytes[20..23])? as i64;
    if year == 0
        || !(1..=12).contains(&month)
        || day == 0
        || day > days_in_month(year, month)
        || hour > 23
        || minute > 59
        || second > 59
    {
        return None;
    }

    let days = days_from_civil(year, month, day);
    Some(days * MILLIS_PER_DAY + hour * 3_600_000 + minute * 60_000 + second * 1_000 + millisecond)
}

fn decimal(bytes: &[u8]) -> Option<u32> {
    bytes.iter().try_fold(0_u32, |value, byte| {
        byte.is_ascii_digit()
            .then(|| value * 10 + u32::from(*byte - b'0'))
    })
}

fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        2 if is_leap_year(year) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

fn is_leap_year(year: i64) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let adjusted_year = year - if month <= 2 { 1 } else { 0 };
    let era = adjusted_year.div_euclid(400);
    let year_of_era = adjusted_year - era * 400;
    let adjusted_month = month + if month > 2 { -3 } else { 9 };
    let day_of_year = (153 * adjusted_month + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

fn civil_from_days(days_since_epoch: i64) -> (i64, i64, i64) {
    let shifted_days = days_since_epoch + 719_468;
    let era = shifted_days.div_euclid(146_097);
    let day_of_era = shifted_days - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let mut year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    year += if month <= 2 { 1 } else { 0 };
    (year, month, day)
}

#[cfg(test)]
mod tests {
    use super::{format_ts, parse_timestamp};

    #[test]
    fn timestamp_round_trip_covers_leap_and_pre_epoch_dates() {
        for value in [
            "0001-01-01T00:00:00.000Z",
            "1969-12-31T23:59:59.999Z",
            "1970-01-01T00:00:00.000Z",
            "2028-02-29T12:34:56.789Z",
            "9999-12-31T23:59:59.999Z",
        ] {
            let timestamp = parse_timestamp(value).expect("valid timestamp");
            assert_eq!(format_ts(timestamp), value);
        }
    }
}
