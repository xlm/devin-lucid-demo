use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};

use crate::config::Rules;
use crate::processors::charging_session::{ChargingSessionProcessor, ErrorRecord, Output};
use crate::state::{read_checkpoint, write_checkpoint_atomic, ProcessorState};

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ReplayStats {
    pub lines: u64,
    pub accepted: u64,
    pub duplicates_dropped: u64,
    pub errors: BTreeMap<String, u64>,
    pub summaries: u64,
}

#[derive(Clone, Debug)]
pub struct ReplayOptions {
    pub input: PathBuf,
    pub output_dir: PathBuf,
    pub stop_after: Option<u64>,
    pub checkpoint: Option<PathBuf>,
    pub resume_from: Option<PathBuf>,
}

#[derive(Debug)]
pub struct ReplayError(String);

impl fmt::Display for ReplayError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl Error for ReplayError {}

impl ReplayError {
    fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

/// Runs deterministic JSONL replay and appends canonical output records.
pub fn run(opts: &ReplayOptions) -> Result<ReplayStats, ReplayError> {
    if opts.stop_after.is_some() != opts.checkpoint.is_some() {
        return Err(ReplayError::new(
            "--stop-after and --checkpoint must be used together",
        ));
    }

    let input = fs::read(&opts.input).map_err(|error| {
        ReplayError::new(format!(
            "cannot read input {}: {error}",
            opts.input.display()
        ))
    })?;
    let lines = input_lines(&input);
    let state = match &opts.resume_from {
        Some(path) => read_checkpoint(path).map_err(|error| {
            ReplayError::new(format!(
                "cannot read checkpoint {}: {error}",
                path.display()
            ))
        })?,
        None => ProcessorState::default(),
    };
    let offset = state.input_offset;
    if offset > lines.len() as u64 {
        return Err(ReplayError::new(
            "checkpoint input offset exceeds the number of input lines",
        ));
    }
    log_line(&format!("start offset={offset}"));

    fs::create_dir_all(&opts.output_dir).map_err(|error| {
        ReplayError::new(format!(
            "cannot create output directory {}: {error}",
            opts.output_dir.display()
        ))
    })?;
    let summaries_path = opts.output_dir.join("summaries.jsonl");
    let errors_path = opts.output_dir.join("errors.jsonl");
    let summaries_file = open_append(&summaries_path)?;
    let errors_file = open_append(&errors_path)?;
    let mut summaries_writer = BufWriter::new(summaries_file);
    let mut errors_writer = BufWriter::new(errors_file);

    let requested_end = opts
        .stop_after
        .map(|count| offset.saturating_add(count))
        .unwrap_or(lines.len() as u64);
    let end = requested_end.min(lines.len() as u64);
    let mut processor = if opts.resume_from.is_some() {
        // TODO(workflow-c): restore keyed state, dedupe and watermark on resume.
        ChargingSessionProcessor::new(Rules::CONTRACT)
    } else {
        ChargingSessionProcessor::from_state(Rules::CONTRACT, state)
    };
    let mut stats = ReplayStats::default();

    for index in offset..end {
        let line_no = index + 1;
        let line = std::str::from_utf8(lines[index as usize]).unwrap_or("");
        let outputs = processor.process_line(line_no, line);
        stats.lines += 1;
        for output in outputs {
            write_output(
                output,
                line_no,
                &mut summaries_writer,
                &mut errors_writer,
                &mut stats,
            )?;
        }
    }
    let counters = processor.counters();
    stats.accepted = counters.accepted;
    stats.duplicates_dropped = counters.duplicates_dropped;

    if opts.stop_after.is_some() {
        flush_and_sync(&mut summaries_writer, &summaries_path)?;
        flush_and_sync(&mut errors_writer, &errors_path)?;
        write_checkpoint_atomic(
            opts.checkpoint
                .as_deref()
                .expect("validated checkpoint option"),
            processor.state(),
        )
        .map_err(|error| ReplayError::new(format!("cannot write checkpoint: {error}")))?;
    } else {
        for output in processor.finish() {
            write_output(
                output,
                processor.state().input_offset,
                &mut summaries_writer,
                &mut errors_writer,
                &mut stats,
            )?;
        }
        summaries_writer
            .flush()
            .map_err(|error| file_error("cannot flush", &summaries_path, error))?;
        errors_writer
            .flush()
            .map_err(|error| file_error("cannot flush", &errors_path, error))?;
    }

    log_stats(&stats);
    Ok(stats)
}

fn input_lines(input: &[u8]) -> Vec<&[u8]> {
    if input.is_empty() {
        return Vec::new();
    }
    let mut lines: Vec<_> = input.split(|byte| *byte == b'\n').collect();
    if input.last() == Some(&b'\n') {
        lines.pop();
    }
    lines
}

fn open_append(path: &Path) -> Result<File, ReplayError> {
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|error| file_error("cannot open output", path, error))
}

fn write_output(
    output: Output,
    line_no: u64,
    summaries_writer: &mut BufWriter<File>,
    errors_writer: &mut BufWriter<File>,
    stats: &mut ReplayStats,
) -> Result<(), ReplayError> {
    match output {
        Output::Summary(summary) => {
            write_json_line(summaries_writer, &summary)?;
            stats.summaries += 1;
        }
        Output::Error(error) => {
            log_error(line_no, &error);
            *stats
                .errors
                .entry(error.error_type.as_str().to_owned())
                .or_default() += 1;
            write_json_line(errors_writer, &error)?;
        }
    }
    Ok(())
}

fn write_json_line<T: serde::Serialize>(
    writer: &mut BufWriter<File>,
    record: &T,
) -> Result<(), ReplayError> {
    serde_json::to_writer(&mut *writer, record)
        .map_err(|error| ReplayError::new(format!("cannot serialize output record: {error}")))?;
    writer
        .write_all(b"\n")
        .map_err(|error| ReplayError::new(format!("cannot write output record: {error}")))
}

fn flush_and_sync(writer: &mut BufWriter<File>, path: &Path) -> Result<(), ReplayError> {
    writer
        .flush()
        .map_err(|error| file_error("cannot flush", path, error))?;
    writer
        .get_ref()
        .sync_all()
        .map_err(|error| file_error("cannot sync", path, error))
}

fn file_error(action: &str, path: &Path, error: std::io::Error) -> ReplayError {
    ReplayError::new(format!("{action} {}: {error}", path.display()))
}

fn log_line(message: &str) {
    eprintln!("{message}");
}

fn log_error(line_no: u64, error: &ErrorRecord) {
    let field = error.field.as_deref().unwrap_or("null");
    log_line(&format!(
        "line={line_no} error_type={} field={field}",
        error.error_type.as_str()
    ));
}

fn log_stats(stats: &ReplayStats) {
    let error_counts = serde_json::to_string(&stats.errors).unwrap_or_else(|_| "{}".to_owned());
    log_line(&format!(
        "stats lines={} accepted={} duplicates_dropped={} errors={} summaries={}",
        stats.lines, stats.accepted, stats.duplicates_dropped, error_counts, stats.summaries
    ));
}
