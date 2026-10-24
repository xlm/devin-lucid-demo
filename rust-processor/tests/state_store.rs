use charging_processor::state::{InMemoryStateStore, KeyState, SessionKey, StateStore};

#[test]
fn in_memory_store_supports_keyed_updates_ordered_iteration_and_restore() {
    let mut store = InMemoryStateStore::default();
    let first_key = ("VEH-0001".to_owned(), "session-b".to_owned());
    let second_key = ("VEH-0001".to_owned(), "session-a".to_owned());
    let third_key = ("VEH-0002".to_owned(), "session-a".to_owned());

    let mut first_state = KeyState::default();
    first_state.seen.insert("event-1".to_owned());
    store.put(first_key.clone(), first_state.clone());
    store.put(second_key.clone(), KeyState::default());
    store.put(third_key.clone(), KeyState::default());

    assert_eq!(store.get(&first_key), Some(first_state));
    assert_eq!(
        store
            .iter()
            .map(|(key, _)| key)
            .collect::<Vec<SessionKey>>(),
        vec![second_key.clone(), first_key.clone(), third_key.clone()]
    );

    let snapshot = store.snapshot();
    let restored = InMemoryStateStore::restore(snapshot.clone());
    assert_eq!(restored.snapshot(), snapshot);
    assert_eq!(
        restored.iter().map(|(key, _)| key).collect::<Vec<_>>(),
        vec![second_key.clone(), first_key.clone(), third_key.clone()]
    );

    assert!(store.remove(&first_key).is_some());
    assert!(store.get(&first_key).is_none());
}
