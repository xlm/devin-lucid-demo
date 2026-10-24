use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::Value;
use tempfile::tempdir;

fn repo_path(relative: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join(relative)
}

fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_charging-processor")
}

fn run_cli(arguments: &[&str]) -> Output {
    Command::new(binary()).args(arguments).output().unwrap()
}

fn records(path: &Path) -> Vec<Value> {
    fs::read_to_string(path)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
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

fn assert_fixture_output(output_dir: &Path, scenario: &str) {
    for file in ["summaries.jsonl", "errors.jsonl"] {
        let actual = records(&output_dir.join(file));
        let expected = records(&repo_path(&format!(
            "../fixtures/expected/{scenario}/{file}"
        )));
        assert_eq!(actual.len(), expected.len(), "{scenario}/{file} count");
        let mut actual_groups = BTreeMap::new();
        let mut expected_groups = BTreeMap::new();
        for record in actual {
            let key = record_key(&record);
            actual_groups
                .entry(key)
                .or_insert_with(Vec::new)
                .push(record);
        }
        for record in expected {
            let key = record_key(&record);
            expected_groups
                .entry(key)
                .or_insert_with(Vec::new)
                .push(record);
        }
        assert_eq!(
            actual_groups.keys().collect::<Vec<_>>(),
            expected_groups.keys().collect::<Vec<_>>(),
            "{scenario}/{file} key groups"
        );
        for (key, expected_group) in expected_groups {
            let actual_group = &actual_groups[&key];
            assert_eq!(
                actual_group.len(),
                expected_group.len(),
                "{scenario}/{file} key {key:?} count"
            );
            assert!(
                actual_group
                    .iter()
                    .zip(expected_group.iter())
                    .all(|(actual, expected)| numeric_aware_equal(actual, expected)),
                "{scenario}/{file} key {key:?} content"
            );
        }
    }
}

fn record_key(record: &Value) -> (Option<String>, Option<String>) {
    (
        record
            .get("vehicle_id")
            .and_then(Value::as_str)
            .map(str::to_owned),
        record
            .get("session_id")
            .and_then(Value::as_str)
            .map(str::to_owned),
    )
}

fn assert_private_stderr(stderr: &[u8]) {
    let stderr = String::from_utf8_lossy(stderr);
    for forbidden in ["VEH-", "S-00", "SYN-"] {
        assert!(
            !stderr.contains(forbidden),
            "stderr included restricted value pattern {forbidden}"
        );
    }
}

fn assert_redacted_fatal(output: Output, expected_role: &str) {
    assert_eq!(output.status.code(), Some(1));
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains(expected_role));
    assert!(!stderr.contains("VEH-0001"));
    assert!(!stderr.contains("S-secret"));
}

#[test]
fn usage_and_fatal_errors_have_the_expected_exit_codes() {
    assert_eq!(run_cli(&[]).status.code(), Some(2));
    assert_eq!(
        run_cli(&["replay", "--input", "input.jsonl"]).status.code(),
        Some(2)
    );
    assert_eq!(
        run_cli(&[
            "replay",
            "--input",
            "one",
            "--input",
            "two",
            "--output-dir",
            "out"
        ])
        .status
        .code(),
        Some(2)
    );
    assert_eq!(
        run_cli(&["replay", "--unknown", "value"]).status.code(),
        Some(2)
    );
    let output = tempdir().unwrap();
    assert_eq!(
        run_cli(&[
            "replay",
            "--input",
            "input.jsonl",
            "--output-dir",
            output.path().to_str().unwrap(),
            "--stop-after",
            "1"
        ])
        .status
        .code(),
        Some(2)
    );
    assert_eq!(
        run_cli(&[
            "replay",
            "--input",
            "input.jsonl",
            "--output-dir",
            output.path().to_str().unwrap(),
            "--stop-after",
            "not-a-number",
            "--checkpoint",
            "checkpoint"
        ])
        .status
        .code(),
        Some(2)
    );
    assert_eq!(
        run_cli(&[
            "replay",
            "--input",
            "/this/path/does/not/exist",
            "--output-dir",
            output.path().to_str().unwrap()
        ])
        .status
        .code(),
        Some(1)
    );
    let input = output.path().join("empty.jsonl");
    let checkpoint = output.path().join("bad-checkpoint");
    fs::write(&input, b"").unwrap();
    fs::write(&checkpoint, b"not a checkpoint").unwrap();
    assert_eq!(
        run_cli(&[
            "replay",
            "--input",
            input.to_str().unwrap(),
            "--output-dir",
            output.path().to_str().unwrap(),
            "--resume-from",
            checkpoint.to_str().unwrap()
        ])
        .status
        .code(),
        Some(1)
    );
    fs::write(
        &checkpoint,
        br#"{"format":"charging-processor-checkpoint","version":1,"input_offset":1,"max_event_ts":null,"store":{"entries":[]}}"#,
    )
    .unwrap();
    assert_eq!(
        run_cli(&[
            "replay",
            "--input",
            input.to_str().unwrap(),
            "--output-dir",
            output.path().to_str().unwrap(),
            "--resume-from",
            checkpoint.to_str().unwrap()
        ])
        .status
        .code(),
        Some(1)
    );
}

#[test]
fn fatal_errors_do_not_log_operator_supplied_paths() {
    let directory = tempdir().unwrap();
    let missing_input = directory
        .path()
        .join("VEH-0001-S-secret-missing-input.jsonl");
    let output_dir = directory.path().join("out");
    let input_error = run_cli(&[
        "replay",
        "--input",
        missing_input.to_str().unwrap(),
        "--output-dir",
        output_dir.to_str().unwrap(),
    ]);
    assert_redacted_fatal(input_error, "cannot read input:");

    let input = directory.path().join("empty.jsonl");
    fs::write(&input, b"").unwrap();
    let missing_checkpoint = directory
        .path()
        .join("VEH-0001-S-secret-missing-checkpoint");
    let checkpoint_error = run_cli(&[
        "replay",
        "--input",
        input.to_str().unwrap(),
        "--output-dir",
        output_dir.to_str().unwrap(),
        "--resume-from",
        missing_checkpoint.to_str().unwrap(),
    ]);
    assert_redacted_fatal(checkpoint_error, "cannot read checkpoint:");
}

#[test]
fn help_prints_to_stdout_with_success_status() {
    let output = run_cli(&["--help"]);
    assert_eq!(output.status.code(), Some(0));
    assert!(String::from_utf8_lossy(&output.stdout).contains("Usage:"));
}

#[cfg(not(feature = "kafka"))]
#[test]
fn serve_without_kafka_support_is_a_usage_error() {
    let output = run_cli(&["serve"]);
    assert_eq!(output.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&output.stderr)
        .contains("charging-processor was built without Kafka support"));
}

#[test]
fn normal_session_cli_output_matches_expected_and_stderr_omits_ids() {
    let scenario = "normal_session";
    let output_dir = tempdir().unwrap();
    let input = repo_path("../fixtures/scenarios/normal_session/input.jsonl");
    let output = run_cli(&[
        "replay",
        "--input",
        input.to_str().unwrap(),
        "--output-dir",
        output_dir.path().to_str().unwrap(),
    ]);
    assert_eq!(output.status.code(), Some(0));
    assert_fixture_output(output_dir.path(), scenario);
    assert_private_stderr(&output.stderr);
}

#[test]
fn malformed_null_cli_stderr_omits_ids_and_sensitive_locations() {
    let output_dir = tempdir().unwrap();
    let input = repo_path("../fixtures/scenarios/malformed_null/input.jsonl");
    let output = run_cli(&[
        "replay",
        "--input",
        input.to_str().unwrap(),
        "--output-dir",
        output_dir.path().to_str().unwrap(),
    ]);
    assert_eq!(output.status.code(), Some(0));
    assert_private_stderr(&output.stderr);
}

#[test]
fn processor_restart_cli_matches_expected_after_resume() {
    let scenario = "processor_restart";
    let output_dir = tempdir().unwrap();
    let input = repo_path("../fixtures/scenarios/processor_restart/input.jsonl");
    let checkpoint = output_dir.path().join("processor.checkpoint");
    let first = run_cli(&[
        "replay",
        "--input",
        input.to_str().unwrap(),
        "--output-dir",
        output_dir.path().to_str().unwrap(),
        "--stop-after=9",
        &format!("--checkpoint={}", checkpoint.display()),
    ]);
    assert_eq!(first.status.code(), Some(0));
    let resumed = run_cli(&[
        "replay",
        "--input",
        input.to_str().unwrap(),
        "--output-dir",
        output_dir.path().to_str().unwrap(),
        &format!("--resume-from={}", checkpoint.display()),
    ]);
    assert_eq!(resumed.status.code(), Some(0));
    assert_fixture_output(output_dir.path(), scenario);
}
