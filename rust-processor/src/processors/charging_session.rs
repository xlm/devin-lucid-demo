use std::cmp::Ordering;

use serde::{Deserialize, Serialize};
use serde_json::Number;

use crate::config::Rules;
use crate::model::{cmp_numbers, validate_line, EchoIds, ErrorType, ValidatedEvent};
use crate::state::{InMemoryStateStore, ProcessorState, Session, SessionKey, StateStore};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SummaryRecord {
    pub avg_power_kw: Number,
    pub completion_reason: String,
    pub end_ts: String,
    pub event_count: u64,
    pub peak_power_kw: Number,
    pub schema_version: String,
    pub session_id: String,
    pub start_ts: String,
    pub total_energy_kwh: Number,
    pub vehicle_id: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ErrorRecord {
    pub error_type: ErrorType,
    pub event_id: Option<String>,
    pub field: Option<String>,
    pub input_line: u64,
    pub schema_version: String,
    pub session_id: Option<String>,
    pub vehicle_id: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Output {
    Summary(SummaryRecord),
    Error(ErrorRecord),
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct ProcessorCounters {
    pub accepted: u64,
    pub duplicates_dropped: u64,
}

pub struct ChargingSessionProcessor<S: StateStore = InMemoryStateStore> {
    rules: Rules,
    state: ProcessorState<S>,
    counters: ProcessorCounters,
}

impl ChargingSessionProcessor<InMemoryStateStore> {
    /// Creates a processor backed by the ordered in-memory state store.
    pub fn new(rules: Rules) -> Self {
        Self::from_state(rules, ProcessorState::default())
    }
}

impl<S: StateStore> ChargingSessionProcessor<S> {
    /// Creates a processor from global and keyed state.
    pub fn from_state(rules: Rules, state: ProcessorState<S>) -> Self {
        Self {
            rules,
            state,
            counters: ProcessorCounters::default(),
        }
    }

    /// Returns the global processor state and its keyed store.
    pub fn state(&self) -> &ProcessorState<S> {
        &self.state
    }

    /// Returns counters for lines processed by this processor instance.
    pub fn counters(&self) -> ProcessorCounters {
        self.counters
    }

    /// Processes one input line and returns records in emission order.
    pub fn process_line(&mut self, line_no: u64, line: &str) -> Vec<Output> {
        self.state.input_offset = line_no;
        let event = match validate_line(line) {
            Ok(event) => event,
            Err(error) => {
                return vec![Output::Error(error_record(
                    line_no,
                    error.error_type,
                    error.field,
                    error.ids,
                ))];
            }
        };
        let key = (event.vehicle_id.clone(), event.session_id.clone());
        if self.is_duplicate(&key, &event.event_id) {
            self.counters.duplicates_dropped += 1;
            return Vec::new();
        }
        self.record_event_id(&key, &event.event_id);

        if Self::is_late(self.watermark(), event.ts_ms) {
            return vec![Output::Error(error_record(
                line_no,
                ErrorType::LateEventDropped,
                None,
                event_ids(&event),
            ))];
        }

        let mut outputs = Vec::new();
        if self.is_closed(&key) {
            outputs.push(Output::Error(error_record(
                line_no,
                ErrorType::SessionAlreadyClosed,
                None,
                event_ids(&event),
            )));
        } else {
            if let Some(mut key_state) = self.state.store.get(&key) {
                if let Some(session) = key_state.session.as_mut() {
                    Self::accumulate(session, &event);
                    self.state.store.put(key.clone(), key_state);
                } else {
                    self.open_session(key.clone(), &event);
                }
            } else {
                self.open_session(key.clone(), &event);
            }
            self.counters.accepted += 1;
        }

        self.advance_watermark(event.ts_ms);
        outputs.extend(self.fire_timers(self.watermark()));
        outputs
    }

    /// Flushes all remaining open sessions as end-of-input.
    pub fn finish(&mut self) -> Vec<Output> {
        self.fire_timers(None)
    }

    /// Checks dedupe for this session key without changing state.
    fn is_duplicate(&self, key: &SessionKey, event_id: &str) -> bool {
        self.state
            .store
            .get(key)
            .is_some_and(|key_state| key_state.seen.contains(event_id))
    }

    /// Records an event id in this key's dedupe state.
    fn record_event_id(&mut self, key: &SessionKey, event_id: &str) {
        let mut key_state = self.state.store.get(key).unwrap_or_default();
        key_state.seen.insert(event_id.to_owned());
        self.state.store.put(key.clone(), key_state);
    }

    /// Computes the current event-time watermark.
    fn watermark(&self) -> Option<i64> {
        self.state
            .max_event_ts
            .map(|timestamp| timestamp - self.rules.allowed_lateness_ms)
    }

    /// Rejects timestamps strictly behind an existing watermark.
    fn is_late(watermark: Option<i64>, timestamp: i64) -> bool {
        watermark.is_some_and(|watermark| timestamp < watermark)
    }

    /// Checks whether the session key has already been finalized.
    fn is_closed(&self, key: &SessionKey) -> bool {
        self.state
            .store
            .get(key)
            .is_some_and(|key_state| key_state.closed)
    }

    /// Updates aggregate fields from an accepted event.
    fn accumulate(session: &mut Session, event: &ValidatedEvent) {
        session.start_ts = session.start_ts.min(event.ts_ms);
        session.last_ts = session.last_ts.max(event.ts_ms);
        session.max_sequence_number = session.max_sequence_number.max(event.sequence_number);
        if cmp_numbers(&event.energy_delivered_kwh, &session.max_energy) == Ordering::Greater {
            session.max_energy = event.energy_delivered_kwh.clone();
        }
        if cmp_numbers(&event.charging_power_kw, &session.peak_power) == Ordering::Greater {
            session.peak_power = event.charging_power_kw.clone();
        }
        session.event_count += 1;
        session.complete |= event.charging_state == "COMPLETE";
    }

    /// Opens a session for the first accepted event at a key.
    fn open_session(&mut self, key: SessionKey, event: &ValidatedEvent) {
        let mut key_state = self.state.store.get(&key).unwrap_or_default();
        key_state.session = Some(Session {
            start_ts: event.ts_ms,
            last_ts: event.ts_ms,
            max_sequence_number: event.sequence_number,
            max_energy: event.energy_delivered_kwh.clone(),
            peak_power: event.charging_power_kw.clone(),
            event_count: 1,
            complete: event.charging_state == "COMPLETE",
        });
        self.state.store.put(key, key_state);
    }

    /// Advances the global maximum event timestamp.
    fn advance_watermark(&mut self, timestamp: i64) {
        self.state.max_event_ts = Some(
            self.state
                .max_event_ts
                .map_or(timestamp, |previous| previous.max(timestamp)),
        );
    }

    /// Calculates timer expiry from completion or inactivity.
    fn session_expiry(session: &Session, rules: Rules) -> i64 {
        if session.complete {
            session.last_ts
        } else {
            session.last_ts + rules.inactivity_timeout_ms
        }
    }

    /// Closes and emits every session due at the current watermark.
    fn fire_timers(&mut self, watermark: Option<i64>) -> Vec<Output> {
        let mut due: Vec<_> = self
            .state
            .store
            .iter()
            .filter_map(|(key, key_state)| {
                let session = key_state.session?;
                let expiry = Self::session_expiry(&session, self.rules);
                watermark
                    .map_or(true, |watermark| expiry <= watermark)
                    .then_some((expiry, key))
            })
            .collect();
        due.sort();

        due.into_iter()
            .filter_map(|(_, key)| {
                let mut key_state = self.state.store.get(&key)?;
                let session = key_state.session.take()?;
                key_state.closed = true;
                self.state.store.put(key.clone(), key_state);
                Some(Output::Summary(Self::build_summary(&key, &session)))
            })
            .collect()
    }

    /// Builds the canonical summary fields for a finalized session.
    fn build_summary(key: &SessionKey, session: &Session) -> SummaryRecord {
        SummaryRecord {
            avg_power_kw: session.peak_power.clone(),
            completion_reason: if session.complete {
                "COMPLETE"
            } else {
                "INACTIVITY_TIMEOUT"
            }
            .to_owned(),
            end_ts: crate::model::format_ts(session.last_ts),
            event_count: session.event_count,
            peak_power_kw: session.peak_power.clone(),
            schema_version: "1.0".to_owned(),
            session_id: key.1.clone(),
            start_ts: crate::model::format_ts(session.start_ts),
            total_energy_kwh: session.max_energy.clone(),
            vehicle_id: key.0.clone(),
        }
    }
}

fn event_ids(event: &ValidatedEvent) -> EchoIds {
    EchoIds {
        event_id: Some(event.event_id.clone()),
        vehicle_id: Some(event.vehicle_id.clone()),
        session_id: Some(event.session_id.clone()),
    }
}

fn error_record(
    input_line: u64,
    error_type: ErrorType,
    field: Option<&'static str>,
    ids: EchoIds,
) -> ErrorRecord {
    ErrorRecord {
        error_type,
        event_id: ids.event_id,
        field: field.map(str::to_owned),
        input_line,
        schema_version: "1.0".to_owned(),
        session_id: ids.session_id,
        vehicle_id: ids.vehicle_id,
    }
}
