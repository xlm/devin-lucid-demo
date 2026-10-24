use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use charging_processor::replay::{run, ReplayOptions};
use serde_json::Value;
use tempfile::tempdir;

fn repo_path(relative: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join(relative)
}

fn read_jsonl(path: &Path) -> Vec<Value> {
    let contents = fs::read_to_string(path).unwrap();
    contents
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

fn records_by_key(records: &[Value]) -> BTreeMap<(Option<String>, Option<String>), Vec<Value>> {
    let mut groups = BTreeMap::new();
    for record in records {
        let key = (
            record
                .get("vehicle_id")
                .and_then(Value::as_str)
                .map(str::to_owned),
            record
                .get("session_id")
                .and_then(Value::as_str)
                .map(str::to_owned),
        );
        groups
            .entry(key)
            .or_insert_with(Vec::new)
            .push(record.clone());
    }
    groups
}

fn numeric_aware_equal(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Number(left), Value::Number(right)) => left.as_f64() == right.as_f64(),
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left.iter().all(|(key, value)| {
                    right
                        .get(key)
                        .is_some_and(|other| numeric_aware_equal(value, other))
                })
        }
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right)
                    .all(|(left, right)| numeric_aware_equal(left, right))
        }
        _ => left == right,
    }
}

fn assert_records_match(actual: &Path, expected: &Path) -> Result<(), String> {
    let actual_records = read_jsonl(actual);
    let expected_records = read_jsonl(expected);
    if actual_records.len() != expected_records.len() {
        return Err(format!(
            "count differs: actual {}, expected {}",
            actual_records.len(),
            expected_records.len()
        ));
    }
    let actual_groups = records_by_key(&actual_records);
    let expected_groups = records_by_key(&expected_records);
    if actual_groups.len() != expected_groups.len() {
        return Err(format!(
            "key groups differ: actual {}, expected {}",
            actual_groups.len(),
            expected_groups.len()
        ));
    }
    for (key, expected_group) in expected_groups {
        let Some(actual_group) = actual_groups.get(&key) else {
            return Err(format!("missing key group {key:?}"));
        };
        if actual_group.len() != expected_group.len() {
            return Err(format!("record count differs for key {key:?}"));
        }
        for (index, (actual, expected)) in actual_group.iter().zip(&expected_group).enumerate() {
            if !numeric_aware_equal(actual, expected) {
                return Err(format!(
                    "record {index} differs for key {key:?}: actual {actual}, expected {expected}"
                ));
            }
        }
    }
    Ok(())
}

#[test]
fn all_approved_scenarios_match_expected_outputs_per_key() {
    let scenarios_root = repo_path("../fixtures/scenarios");
    let mut scenarios: Vec<_> = fs::read_dir(&scenarios_root)
        .unwrap()
        .map(|entry| entry.unwrap())
        .filter(|entry| entry.file_type().unwrap().is_dir())
        .collect();
    scenarios.sort_by_key(|entry| entry.file_name());
    assert_eq!(scenarios.len(), 13, "expected all 13 approved scenarios");

    let mut failures = Vec::new();
    for scenario_dir in scenarios {
        let name = scenario_dir.file_name().to_string_lossy().into_owned();
        let scenario: Value =
            serde_json::from_slice(&fs::read(scenario_dir.path().join("scenario.json")).unwrap())
                .unwrap();
        let input = scenario_dir.path().join("input.jsonl");
        let output_dir = tempdir().unwrap();
        let run_config = &scenario["run"];
        let mut options = ReplayOptions {
            input: input.clone(),
            output_dir: output_dir.path().to_path_buf(),
            stop_after: None,
            checkpoint: None,
            resume_from: None,
        };

        let replay_result = if run_config["mode"] == "restart" {
            let checkpoint = output_dir.path().join("processor.checkpoint");
            options.stop_after = Some(run_config["stop_after"].as_u64().unwrap());
            options.checkpoint = Some(checkpoint.clone());
            run(&options).and_then(|_| {
                options.stop_after = None;
                options.checkpoint = None;
                options.resume_from = Some(checkpoint);
                run(&options)
            })
        } else {
            run(&options)
        };
        if let Err(error) = replay_result {
            failures.push(format!("{name}: replay failed: {error}"));
            continue;
        }

        let expected_dir = repo_path(&format!("../fixtures/expected/{name}"));
        for output in ["summaries.jsonl", "errors.jsonl"] {
            if let Err(error) =
                assert_records_match(&output_dir.path().join(output), &expected_dir.join(output))
            {
                failures.push(format!("{name}/{output}: {error}"));
            }
        }
    }
    assert!(
        failures.is_empty(),
        "fixture mismatches:\n{}",
        failures.join("\n")
    );
}

#[test]
fn repeated_restart_replays_have_byte_identical_outputs_and_checkpoints() {
    let input = repo_path("../fixtures/scenarios/state_recovery/input.jsonl");
    let first_dir = tempdir().unwrap();
    let second_dir = tempdir().unwrap();
    let first_checkpoint = first_dir.path().join("checkpoint");
    let second_checkpoint = second_dir.path().join("checkpoint");

    for (output_dir, checkpoint) in [
        (first_dir.path(), &first_checkpoint),
        (second_dir.path(), &second_checkpoint),
    ] {
        run(&ReplayOptions {
            input: input.clone(),
            output_dir: output_dir.to_path_buf(),
            stop_after: Some(8),
            checkpoint: Some(checkpoint.clone()),
            resume_from: None,
        })
        .unwrap();
        run(&ReplayOptions {
            input: input.clone(),
            output_dir: output_dir.to_path_buf(),
            stop_after: None,
            checkpoint: None,
            resume_from: Some(checkpoint.clone()),
        })
        .unwrap();
    }

    for name in ["summaries.jsonl", "errors.jsonl"] {
        assert_eq!(
            fs::read(first_dir.path().join(name)).unwrap(),
            fs::read(second_dir.path().join(name)).unwrap(),
            "{name} must be deterministic"
        );
    }
    assert_eq!(
        fs::read(first_checkpoint).unwrap(),
        fs::read(second_checkpoint).unwrap(),
        "checkpoint bytes must be deterministic"
    );
}
