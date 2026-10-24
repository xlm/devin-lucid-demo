use std::collections::BTreeSet;
use std::path::PathBuf;
use std::process::ExitCode;

use charging_processor::replay::{run, ReplayOptions};

const USAGE: &str = "\
Usage:
  charging-processor replay --input <file> --output-dir <dir> [--stop-after <n> --checkpoint <file>] [--resume-from <file>]
  charging-processor serve
  charging-processor --help

Exit codes: 0 success, 1 fatal error, 2 usage error.
Replay uses contract rules. Environment overrides apply to serve only.";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() {
        print_usage(false);
        return ExitCode::from(2);
    }
    if args
        .iter()
        .any(|argument| argument == "--help" || argument == "-h")
    {
        print_usage(true);
        return ExitCode::SUCCESS;
    }

    match args[0].as_str() {
        "replay" => run_replay(&args[1..]),
        "serve" if args.len() == 1 => run_serve(),
        _ => {
            print_usage(false);
            ExitCode::from(2)
        }
    }
}

fn run_replay(args: &[String]) -> ExitCode {
    let options = match parse_replay_options(args) {
        Ok(options) => options,
        Err(()) => {
            print_usage(false);
            return ExitCode::from(2);
        }
    };
    match run(&options) {
        Ok(_) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("fatal: {error}");
            ExitCode::from(1)
        }
    }
}

fn parse_replay_options(args: &[String]) -> Result<ReplayOptions, ()> {
    let mut values = std::collections::BTreeMap::new();
    let mut seen = BTreeSet::new();
    let mut index = 0;
    while index < args.len() {
        let argument = &args[index];
        let (name, inline_value) = match argument.split_once('=') {
            Some((name, value)) => (name, Some(value.to_owned())),
            None => (argument.as_str(), None),
        };
        if !matches!(
            name,
            "--input" | "--output-dir" | "--stop-after" | "--checkpoint" | "--resume-from"
        ) || !seen.insert(name.to_owned())
        {
            return Err(());
        }
        let value = if let Some(value) = inline_value {
            if value.is_empty() {
                return Err(());
            }
            value
        } else {
            index += 1;
            let Some(value) = args.get(index) else {
                return Err(());
            };
            if value.starts_with("--") || value.is_empty() {
                return Err(());
            }
            value.clone()
        };
        values.insert(name.to_owned(), value);
        index += 1;
    }

    let input = values.get("--input").ok_or(())?;
    let output_dir = values.get("--output-dir").ok_or(())?;
    let stop_after = values
        .get("--stop-after")
        .map(|value| value.parse::<u64>().map_err(|_| ()))
        .transpose()?;
    let checkpoint = values.get("--checkpoint");
    if stop_after.is_some() != checkpoint.is_some() {
        return Err(());
    }
    Ok(ReplayOptions {
        input: PathBuf::from(input),
        output_dir: PathBuf::from(output_dir),
        stop_after,
        checkpoint: checkpoint.map(PathBuf::from),
        resume_from: values.get("--resume-from").map(PathBuf::from),
    })
}

fn run_serve() -> ExitCode {
    #[cfg(feature = "kafka")]
    {
        match charging_processor::kafka::serve(charging_processor::config::ServiceConfig::from_env())
        {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => {
                eprintln!("fatal: {error}");
                ExitCode::from(1)
            }
        }
    }
    #[cfg(not(feature = "kafka"))]
    {
        eprintln!(
            "charging-processor was built without Kafka support (rebuild with --features kafka)"
        );
        ExitCode::from(2)
    }
}
fn print_usage(stdout: bool) {
    if stdout {
        println!("{USAGE}");
    } else {
        eprintln!("{USAGE}");
    }
}
