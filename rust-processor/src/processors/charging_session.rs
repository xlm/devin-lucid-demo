use serde::{Deserialize, Serialize};
use serde_json::Number;

use crate::config::Rules;
use crate::model::ErrorType;
use crate::state::{InMemoryStateStore, ProcessorState, StateStore};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SummaryRecord {
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
    _rules: Rules,
    state: ProcessorState<S>,
    counters: ProcessorCounters,
}

impl ChargingSessionProcessor<InMemoryStateStore> {
    pub fn new(rules: Rules) -> Self {
        Self::from_state(rules, ProcessorState::default())
    }
}

impl<S: StateStore> ChargingSessionProcessor<S> {
    pub fn from_state(rules: Rules, state: ProcessorState<S>) -> Self {
        Self {
            _rules: rules,
            state,
            counters: ProcessorCounters::default(),
        }
    }

    pub fn state(&self) -> &ProcessorState<S> {
        &self.state
    }

    pub fn counters(&self) -> ProcessorCounters {
        self.counters
    }

    pub fn process_line(&mut self, line_no: u64, _line: &str) -> Vec<Output> {
        self.state.input_offset = line_no;
        // TODO(workflow-b): implement validation, state updates and emissions.
        Vec::new()
    }

    pub fn finish(&mut self) -> Vec<Output> {
        // TODO(workflow-b): flush completed sessions at end of input.
        Vec::new()
    }
}
