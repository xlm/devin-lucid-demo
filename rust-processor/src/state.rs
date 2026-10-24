//! Checkpoints hold key ids as processor state, not logs, and are restricted.

use std::collections::{BTreeMap, BTreeSet};
use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::Path;

use serde::{Deserialize, Serialize};
use serde_json::Number;

pub type SessionKey = (String, String);

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Session {
    pub start_ts: i64,
    pub last_ts: i64,
    pub max_sequence_number: u64,
    pub max_energy: Number,
    pub previous_energy: Number,
    pub peak_power: Number,
    pub event_count: u64,
    pub complete: bool,
}

#[derive(Clone, Debug, PartialEq, Default, Serialize, Deserialize)]
pub struct KeyState {
    pub seen: BTreeSet<String>,
    pub session: Option<Session>,
    pub closed: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct StoreEntry {
    pub vehicle_id: String,
    pub session_id: String,
    pub state: KeyState,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct StoreSnapshot {
    pub entries: Vec<StoreEntry>,
}

pub trait StateStore {
    /// Returns an owned copy so callers do not depend on a backend borrow.
    fn get(&self, key: &SessionKey) -> Option<KeyState>;
    /// Inserts or replaces a key's state.
    fn put(&mut self, key: SessionKey, value: KeyState);
    /// Removes and returns a key's state.
    fn remove(&mut self, key: &SessionKey) -> Option<KeyState>;
    /// Iterates keys in ascending session-key order.
    fn iter(&self) -> Box<dyn Iterator<Item = (SessionKey, KeyState)> + '_>;
    /// Produces a deterministic snapshot in ascending session-key order.
    fn snapshot(&self) -> StoreSnapshot;
    /// Restores a store from a snapshot.
    fn restore(snapshot: StoreSnapshot) -> Self
    where
        Self: Sized;
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct InMemoryStateStore {
    map: BTreeMap<SessionKey, KeyState>,
}

impl StateStore for InMemoryStateStore {
    fn get(&self, key: &SessionKey) -> Option<KeyState> {
        self.map.get(key).cloned()
    }

    fn put(&mut self, key: SessionKey, value: KeyState) {
        self.map.insert(key, value);
    }

    fn remove(&mut self, key: &SessionKey) -> Option<KeyState> {
        self.map.remove(key)
    }

    fn iter(&self) -> Box<dyn Iterator<Item = (SessionKey, KeyState)> + '_> {
        Box::new(
            self.map
                .iter()
                .map(|(key, value)| (key.clone(), value.clone())),
        )
    }

    fn snapshot(&self) -> StoreSnapshot {
        StoreSnapshot {
            entries: self
                .iter()
                .map(|((vehicle_id, session_id), state)| StoreEntry {
                    vehicle_id,
                    session_id,
                    state,
                })
                .collect(),
        }
    }

    fn restore(snapshot: StoreSnapshot) -> Self {
        Self {
            map: snapshot
                .entries
                .into_iter()
                .map(|entry| ((entry.vehicle_id, entry.session_id), entry.state))
                .collect(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Default)]
pub struct ProcessorState<S: StateStore = InMemoryStateStore> {
    pub input_offset: u64,
    pub max_event_ts: Option<i64>,
    pub store: S,
}

#[derive(Deserialize, Serialize)]
struct Checkpoint {
    format: String,
    version: u32,
    input_offset: u64,
    max_event_ts: Option<i64>,
    store: StoreSnapshot,
}

pub fn write_checkpoint_atomic<S: StateStore>(
    path: &Path,
    state: &ProcessorState<S>,
) -> io::Result<()> {
    let checkpoint = Checkpoint {
        format: "charging-processor-checkpoint".to_owned(),
        version: 1,
        input_offset: state.input_offset,
        max_event_ts: state.max_event_ts,
        store: state.store.snapshot(),
    };
    let contents = serde_json::to_vec(&checkpoint)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    let temporary_path = temporary_path(path);
    let mut file = OpenOptions::new()
        .create(true)
        .truncate(true)
        .write(true)
        .open(&temporary_path)?;
    file.write_all(&contents)?;
    file.sync_all()?;
    fs::rename(&temporary_path, path)?;
    if let Some(parent) = path.parent() {
        if let Ok(directory) = File::open(parent) {
            let _ = directory.sync_all();
        }
    }
    Ok(())
}

pub fn read_checkpoint(path: &Path) -> io::Result<ProcessorState> {
    let contents = fs::read(path)?;
    let checkpoint: Checkpoint = serde_json::from_slice(&contents)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    if checkpoint.format != "charging-processor-checkpoint" {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "unknown checkpoint format",
        ));
    }
    if checkpoint.version != 1 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "unsupported checkpoint version",
        ));
    }
    if checkpoint
        .store
        .entries
        .iter()
        .any(|entry| entry.state.closed && entry.state.session.is_some())
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "checkpoint violates closed-session invariant",
        ));
    }
    let entries_len = checkpoint.store.entries.len();
    let store = InMemoryStateStore::restore(checkpoint.store);
    if store.map.len() != entries_len {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "checkpoint contains duplicate session keys",
        ));
    }
    Ok(ProcessorState {
        input_offset: checkpoint.input_offset,
        max_event_ts: checkpoint.max_event_ts,
        store,
    })
}

fn temporary_path(path: &Path) -> std::path::PathBuf {
    let mut name: OsString = path.as_os_str().to_owned();
    name.push(".tmp");
    name.into()
}
