from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema

from verification.common import (
    REPO_ROOT,
    RunContext,
    all_scenarios,
    check,
    gate_result,
    git_dirty,
    git_sha,
    load_jsonl,
    write_gate,
)

SENTINEL_VEHICLE_ID = "VEH-9999"
RESTART_SKIP_REASON = (
    "serve mode keeps dedupe, watermark and open sessions in memory, "
    "so a restart loses state; see docs/exactly-once-semantics.md"
)
EXPECTED_RESTART_CHECKS = (
    "key_partition_affinity",
    "offsets_committed",
    "no_reprocessing_on_restart",
)
OUTPUT_TIMEOUT_S = 60
OUTPUT_QUIET_S = 2
RESTART_OBSERVE_S = 5
PROCESS_STOP_TIMEOUT_S = 10


@dataclass(frozen=True)
class KafkaRecord:
    topic: str
    partition: int
    offset: int
    key: str | None
    value: bytes


def _parse_event(line: bytes) -> dict | None:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _fixture_inputs(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "fixtures" / "scenarios").glob("*/input.jsonl"))


def _assert_sentinel_vehicle_unused(repo_root: Path) -> None:
    for path in _fixture_inputs(repo_root):
        for line in path.read_bytes().splitlines():
            event = _parse_event(line)
            if event is not None and event.get("vehicle_id") == SENTINEL_VEHICLE_ID:
                raise AssertionError(
                    f"{SENTINEL_VEHICLE_ID} is reserved for watermark sentinels, "
                    f"but appears in {path}"
                )


def build_watermark_sentinel(
    repo_root: Path,
    scenario: str,
    *,
    event_id: str | None = None,
) -> dict:
    """Build a valid event that advances the global watermark past fixture data."""
    _assert_sentinel_vehicle_unused(repo_root)
    input_path = repo_root / "fixtures" / "scenarios" / scenario / "input.jsonl"
    event_times = [
        parsed
        for line in input_path.read_bytes().splitlines()
        if (event := _parse_event(line)) is not None
        if (parsed := _parse_timestamp(event.get("event_timestamp"))) is not None
    ]
    if not event_times:
        raise ValueError(f"scenario {scenario} has no parseable event timestamps")

    sentinel_time = max(event_times) + timedelta(hours=24)
    sentinel = {
        "event_id": event_id or f"EVT-WM-{uuid.uuid4().hex}",
        "vehicle_id": SENTINEL_VEHICLE_ID,
        "session_id": "watermark-sentinel",
        "event_timestamp": _format_timestamp(sentinel_time),
        "ingestion_timestamp": _format_timestamp(sentinel_time),
        "sequence_number": 0,
        "charging_state": "CHARGING",
        "battery_soc": 0,
        "charging_power_kw": 0,
        "energy_delivered_kwh": 0,
        "schema_version": "1.0",
    }
    schema = json.loads(
        (repo_root / "contracts" / "charging.events.v1.schema.json").read_text()
    )
    jsonschema.validate(sentinel, schema)
    return sentinel


def _record_payload(record: KafkaRecord | dict) -> dict | None:
    if isinstance(record, KafkaRecord):
        value = record.value
    elif "value" in record:
        value = record["value"]
    else:
        return record if isinstance(record, dict) else None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _record_key(record: KafkaRecord | dict) -> str | None:
    key = record.key if isinstance(record, KafkaRecord) else record.get("key")
    if isinstance(key, bytes):
        key = key.decode("utf-8", errors="replace")
    return key if isinstance(key, str) else None


def _record_position(record: KafkaRecord | dict) -> tuple[int, int]:
    if isinstance(record, KafkaRecord):
        return record.partition, record.offset
    return int(record.get("partition", 0)), int(record.get("offset", 0))


def filter_sentinel_records(records: list[KafkaRecord | dict]) -> list:
    return [
        record
        for record in records
        if (_record_payload(record) or {}).get("vehicle_id") != SENTINEL_VEHICLE_ID
    ]


def check_partition_affinity(
    records: list[KafkaRecord | dict],
    expected_summaries: list[dict],
) -> tuple[bool, dict]:
    key_mismatches = []
    invalid_records = []
    partitions: dict[str, set[int]] = defaultdict(set)
    actual_order: dict[str, list[object]] = defaultdict(list)
    ordered_records = sorted(records, key=_record_position)

    for record in ordered_records:
        payload = _record_payload(record)
        if payload is None or not isinstance(payload.get("vehicle_id"), str):
            invalid_records.append(_record_position(record))
            continue
        vehicle_id = payload["vehicle_id"]
        if _record_key(record) != vehicle_id:
            key_mismatches.append({
                "vehicle_id": vehicle_id,
                "record_key": _record_key(record),
                "partition": _record_position(record)[0],
                "offset": _record_position(record)[1],
            })
        partition, _offset = _record_position(record)
        partitions[vehicle_id].add(partition)
        actual_order[vehicle_id].append(payload.get("session_id"))

    expected_order: dict[str, list[object]] = defaultdict(list)
    for record in expected_summaries:
        vehicle_id = record.get("vehicle_id")
        if isinstance(vehicle_id, str):
            expected_order[vehicle_id].append(record.get("session_id"))

    multi_partition = {
        vehicle_id: sorted(partition_ids)
        for vehicle_id, partition_ids in partitions.items()
        if len(partition_ids) > 1
    }
    order_mismatches = {
        vehicle_id: {
            "expected": expected_order.get(vehicle_id, []),
            "actual": actual_order.get(vehicle_id, []),
        }
        for vehicle_id in sorted(set(expected_order) | set(actual_order))
        if expected_order.get(vehicle_id, []) != actual_order.get(vehicle_id, [])
    }
    evidence = {
        "key_mismatches": key_mismatches,
        "vehicles_in_multiple_partitions": multi_partition,
        "per_vehicle_order_mismatches": order_mismatches,
        "invalid_records": invalid_records,
    }
    passed = not any(evidence.values())
    return passed, evidence


def restart_skip_checks(scenario: str) -> list[dict]:
    return [
        check(
            f"kafka.{name}:{scenario}",
            "skip",
            RESTART_SKIP_REASON,
            category="restart",
            verification="runtime",
            scenario=scenario,
            evidence={"reason": RESTART_SKIP_REASON},
        )
        for name in EXPECTED_RESTART_CHECKS
    ]


def _kafka_clients():
    from confluent_kafka import Consumer, KafkaError, Producer, TopicPartition
    from confluent_kafka.admin import AdminClient, NewTopic

    return Consumer, KafkaError, Producer, TopicPartition, AdminClient, NewTopic


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _expected_counts(repo_root: Path, scenario: str) -> dict[str, int]:
    directory = repo_root / "fixtures" / "expected" / scenario
    for filename in ("summaries.jsonl", "errors.jsonl"):
        if not (directory / filename).is_file():
            raise FileNotFoundError(directory / filename)
    return {
        "summaries": len(load_jsonl(directory / "summaries.jsonl")),
        "errors": len(load_jsonl(directory / "errors.jsonl")),
    }


def _create_topics(admin, NewTopic, topics: dict[str, str]) -> None:
    topic_specs = [
        # A single input partition preserves deterministic arrival for the global watermark.
        NewTopic(topics["input"], num_partitions=1, replication_factor=1),
        NewTopic(topics["summaries"], num_partitions=3, replication_factor=1),
        NewTopic(topics["errors"], num_partitions=3, replication_factor=1),
    ]
    futures = admin.create_topics(
        topic_specs,
        operation_timeout=15,
        request_timeout=30,
    )
    for future in futures.values():
        future.result()


def _cleanup_topics(admin, repo_root: Path) -> int:
    scenario_slugs = "|".join(
        re.escape(scenario.replace("_", "-"))
        for scenario in all_scenarios(repo_root)
    )
    pattern = re.compile(
        rf"^charging-(?:{scenario_slugs})-[0-9a-f]{{12}}-"
        r"(?:input|summaries|errors)$"
    )
    metadata = admin.list_topics(timeout=15)
    stale_topics = [
        name for name in metadata.topics if pattern.fullmatch(name)
    ]
    if not stale_topics:
        return 0
    futures = admin.delete_topics(
        stale_topics,
        operation_timeout=15,
        request_timeout=30,
    )
    for future in futures.values():
        future.result()
    return len(stale_topics)


def _input_key(line: bytes) -> bytes | None:
    event = _parse_event(line)
    vehicle_id = event.get("vehicle_id") if event is not None else None
    return vehicle_id.encode("utf-8") if isinstance(vehicle_id, str) else None


def _produce_inputs(
    Producer,
    bootstrap_servers: str,
    input_topic: str,
    input_lines: list[bytes],
    sentinel: dict,
) -> int:
    producer = Producer({
        "bootstrap.servers": bootstrap_servers,
        "acks": "all",
        "enable.idempotence": True,
    })
    delivery_errors = []

    def on_delivery(error, _message):
        if error is not None:
            delivery_errors.append(str(error))

    for line in input_lines:
        producer.produce(
            input_topic,
            value=line,
            key=_input_key(line),
            partition=0,
            on_delivery=on_delivery,
        )
        producer.poll(0)
    sentinel_bytes = json.dumps(sentinel, separators=(",", ":")).encode("utf-8")
    producer.produce(
        input_topic,
        value=sentinel_bytes,
        key=SENTINEL_VEHICLE_ID.encode("utf-8"),
        partition=0,
        on_delivery=on_delivery,
    )
    remaining = producer.flush(30)
    if remaining:
        raise TimeoutError(f"{remaining} input messages were not delivered")
    if delivery_errors:
        raise RuntimeError("input delivery failed: " + "; ".join(delivery_errors))
    return len(input_lines) + 1


def _poll_record(consumer, KafkaError, timeout: float) -> KafkaRecord | None:
    message = consumer.poll(timeout)
    if message is None:
        return None
    if message.error() is not None:
        if message.error().code() == KafkaError._PARTITION_EOF:
            return None
        raise RuntimeError(f"Kafka consumer error: {message.error()}")
    key = message.key()
    return KafkaRecord(
        topic=message.topic(),
        partition=message.partition(),
        offset=message.offset(),
        key=key.decode("utf-8", errors="replace") if key is not None else None,
        value=message.value() or b"",
    )


def _wait_for_assignment(consumer, KafkaError, timeout_s: float = 15) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        record = _poll_record(consumer, KafkaError, 0.1)
        if record is not None:
            raise RuntimeError("output topic received records before serve started")
        if consumer.assignment():
            return
    raise TimeoutError("output consumer did not receive a partition assignment")


def _committed_offset(
    Consumer,
    TopicPartition,
    bootstrap_servers: str,
    group_id: str,
    input_topic: str,
) -> int:
    probe = Consumer({
        "bootstrap.servers": bootstrap_servers,
        "group.id": group_id,
        "enable.auto.commit": False,
    })
    try:
        partitions = probe.committed(
            [TopicPartition(input_topic, 0)],
            timeout=10,
        )
        return partitions[0].offset
    finally:
        probe.close()


def _topic_end_offset(Consumer, TopicPartition, bootstrap_servers: str, topic: str) -> int:
    probe = Consumer({
        "bootstrap.servers": bootstrap_servers,
        "group.id": f"verification-end-offset-{uuid.uuid4().hex}",
        "enable.auto.commit": False,
    })
    try:
        _low, high = probe.get_watermark_offsets(
            TopicPartition(topic, 0),
            timeout=10,
            cached=False,
        )
        return high
    finally:
        probe.close()


def _wait_for_outputs(
    consumer,
    KafkaError,
    Consumer,
    TopicPartition,
    process: subprocess.Popen,
    topics: dict[str, str],
    group_id: str,
    bootstrap_servers: str,
    end_offset: int,
    expected: dict[str, int],
) -> tuple[dict[str, list[KafkaRecord]], str | None]:
    records = {topics["summaries"]: [], topics["errors"]: []}
    counts = {topics["summaries"]: 0, topics["errors"]: 0}
    expected_by_topic = {
        topics["summaries"]: expected["summaries"],
        topics["errors"]: expected["errors"],
    }
    deadline = time.monotonic() + OUTPUT_TIMEOUT_S
    quiet_deadline = None
    last_offset_probe = 0.0
    input_committed = False

    while time.monotonic() < deadline:
        try:
            record = _poll_record(consumer, KafkaError, 0.2)
        except Exception as exc:
            return records, f"{type(exc).__name__}: {exc}"
        if record is not None:
            quiet_deadline = None
            if record.topic in records:
                if filter_sentinel_records([record]):
                    records[record.topic].append(record)
                    counts[record.topic] += 1

        process_code = process.poll()
        if process_code is not None and process_code != 0:
            return records, f"serve exited with code {process_code}"

        enough_outputs = all(
            counts[topic] >= expected_count
            for topic, expected_count in expected_by_topic.items()
        )
        now = time.monotonic()
        if enough_outputs and now - last_offset_probe >= 0.5:
            last_offset_probe = now
            try:
                input_committed = (
                    _committed_offset(
                        Consumer,
                        TopicPartition,
                        bootstrap_servers,
                        group_id,
                        topics["input"],
                    )
                    == end_offset
                )
            except Exception:
                input_committed = False
        if enough_outputs and input_committed:
            if quiet_deadline is None:
                quiet_deadline = now + OUTPUT_QUIET_S
            elif now >= quiet_deadline:
                return records, None

    return records, (
        "timed out waiting for expected committed outputs and input offset "
        f"(summaries={counts[topics['summaries']]}/{expected['summaries']}, "
        f"errors={counts[topics['errors']]}/{expected['errors']}, "
        f"expected_input_offset={end_offset})"
    )


def _write_records(path: Path, records: list[KafkaRecord]) -> None:
    ordered = sorted(records, key=lambda record: (record.partition, record.offset))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(record.value.rstrip(b"\r\n") + b"\n" for record in ordered)
    )


def _stop_process(process: subprocess.Popen | None) -> int | None:
    if process is None:
        return None
    if process.poll() is None:
        try:
            process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            return process.wait(timeout=PROCESS_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait(timeout=PROCESS_STOP_TIMEOUT_S)
    return process.returncode


def _serve_command(binary: Path) -> list[str]:
    return [str(binary), "serve"]


def _serve_environment(
    topics: dict[str, str],
    group_id: str,
    transactional_id: str,
    bootstrap_servers: str,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({
        "KAFKA_BOOTSTRAP_SERVERS": bootstrap_servers,
        "KAFKA_AUTO_OFFSET_RESET": "earliest",
        "CHARGING_INPUT_TOPIC": topics["input"],
        "CHARGING_SUMMARY_TOPIC": topics["summaries"],
        "CHARGING_ERROR_TOPIC": topics["errors"],
        "CHARGING_CONSUMER_GROUP": group_id,
        "CHARGING_TRANSACTIONAL_ID": transactional_id,
    })
    return environment


def _check(
    scenario: str,
    name: str,
    passed: bool,
    detail: str,
    evidence: dict,
) -> dict:
    return check(
        f"kafka.{name}:{scenario}",
        "pass" if passed else "fail",
        detail,
        category=name,
        verification="runtime",
        scenario=scenario,
        evidence=evidence,
    )


def _run_scenario(
    ctx: RunContext,
    scenario: str,
    binary: Path,
    bootstrap_servers: str,
) -> tuple[list[dict], str | None]:
    Consumer, KafkaError, Producer, TopicPartition, AdminClient, NewTopic = _kafka_clients()
    output_dir = ctx.output_dir(ctx.candidate, scenario)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    summaries_path = output_dir / "summaries.jsonl"
    errors_path = output_dir / "errors.jsonl"
    summaries_path.touch()
    errors_path.touch()
    processor_log = output_dir / "processor.log"
    command = _serve_command(binary)
    run_record = {
        "processor": ctx.candidate,
        "scenario": scenario,
        "mode": "single",
        "commands": [command],
        "exit_codes": [],
        "duration_ms": 0,
        "error": None,
        "topics": {},
        "input_count": 0,
        "expected_counts": {},
        "actual_counts": {"summaries": 0, "errors": 0},
        "consumer_group": None,
        "transactional_id": None,
        "process_returncodes": [],
        "restart": {},
    }
    kafka_checks = []
    scenario_error = None
    topics = {}
    group_id = ""
    transactional_id = ""
    end_offset = None
    all_records: dict[str, list[KafkaRecord]] = {}
    consumer = None
    first_process = None
    restart_process = None
    started = time.monotonic()

    with processor_log.open("ab") as log_file:
        try:
            run_id = uuid.uuid4().hex[:12]
            topic_prefix = f"charging-{scenario.replace('_', '-')}-{run_id}"
            topics = {
                "input": f"{topic_prefix}-input",
                "summaries": f"{topic_prefix}-summaries",
                "errors": f"{topic_prefix}-errors",
            }
            group_id = f"charging-{scenario}-{run_id}"
            transactional_id = f"charging-{scenario}-{run_id}"
            run_record.update({
                "topics": topics,
                "consumer_group": group_id,
                "transactional_id": transactional_id,
            })
            admin = AdminClient({"bootstrap.servers": bootstrap_servers})
            _create_topics(admin, NewTopic, topics)
            input_path = ctx.scenario_dir(scenario) / "input.jsonl"
            input_lines = input_path.read_bytes().splitlines(keepends=True)
            sentinel = build_watermark_sentinel(ctx.repo_root, scenario)
            input_count = _produce_inputs(
                Producer,
                bootstrap_servers,
                topics["input"],
                input_lines,
                sentinel,
            )
            expected = _expected_counts(ctx.repo_root, scenario)
            run_record["input_count"] = input_count
            run_record["expected_counts"] = expected
            end_offset = _topic_end_offset(
                Consumer,
                TopicPartition,
                bootstrap_servers,
                topics["input"],
            )
            consumer = Consumer({
                "bootstrap.servers": bootstrap_servers,
                "group.id": f"verification-output-{run_id}",
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
                "isolation.level": "read_committed",
            })
            consumer.subscribe([topics["summaries"], topics["errors"]])
            _wait_for_assignment(consumer, KafkaError)
            first_process = subprocess.Popen(
                command,
                cwd=ctx.repo_root,
                env=_serve_environment(
                    topics,
                    group_id,
                    transactional_id,
                    bootstrap_servers,
                ),
                stdout=subprocess.DEVNULL,
                stderr=log_file,
            )
            all_records, scenario_error = _wait_for_outputs(
                consumer,
                KafkaError,
                Consumer,
                TopicPartition,
                first_process,
                topics,
                group_id,
                bootstrap_servers,
                end_offset,
                expected,
            )
        except Exception as exc:
            scenario_error = f"{type(exc).__name__}: {exc}"
        finally:
            first_returncode = _stop_process(first_process)
            if first_process is not None:
                run_record["process_returncodes"].append(first_returncode)

            if topics:
                all_records.setdefault(topics.get("summaries", ""), [])
                all_records.setdefault(topics.get("errors", ""), [])
                _write_records(
                    summaries_path,
                    filter_sentinel_records(all_records.get(topics.get("summaries", ""), [])),
                )
                _write_records(
                    errors_path,
                    filter_sentinel_records(all_records.get(topics.get("errors", ""), [])),
                )
            run_record["actual_counts"] = {
                "summaries": sum(
                    1 for _ in summaries_path.read_bytes().splitlines() if _.strip()
                ),
                "errors": sum(
                    1 for _ in errors_path.read_bytes().splitlines() if _.strip()
                ),
            }

        if topics and group_id and end_offset is not None:
            try:
                committed = _committed_offset(
                    Consumer,
                    TopicPartition,
                    bootstrap_servers,
                    group_id,
                    topics["input"],
                )
                kafka_checks.append(_check(
                    scenario,
                    "offsets_committed",
                    committed == end_offset,
                    "consumer group committed the input topic end offset"
                    if committed == end_offset
                    else "consumer group offset does not equal the input topic end offset",
                    {
                        "committed_offset": committed,
                        "input_end_offset": end_offset,
                        "topic": topics["input"],
                        "partition": 0,
                    },
                ))
            except Exception as exc:
                kafka_checks.append(_check(
                    scenario,
                    "offsets_committed",
                    False,
                    f"could not read committed offset: {type(exc).__name__}: {exc}",
                    {"input_end_offset": end_offset, "topic": topics["input"]},
                ))
        else:
            kafka_checks.append(_check(
                scenario,
                "offsets_committed",
                False,
                "input topic or consumer group was not created",
                {},
            ))

        expected_summaries = load_jsonl(
            ctx.expected_dir(scenario) / "summaries.jsonl"
        )
        actual_summaries = all_records.get(topics.get("summaries", ""), [])
        affinity_passed, affinity_evidence = check_partition_affinity(
            filter_sentinel_records(actual_summaries),
            expected_summaries,
        )
        kafka_checks.append(_check(
            scenario,
            "key_partition_affinity",
            affinity_passed,
            "summary keys, vehicle partitions and per-key order match expected"
            if affinity_passed
            else "summary key, partition or per-key order constraint failed",
            affinity_evidence,
        ))

        restart_evidence = {
            "observed_seconds": 0,
            "new_committed_records": 0,
            "same_consumer_group": group_id,
            "same_transactional_id": transactional_id,
        }
        restart_passed = False
        restart_error = None
        if (
            first_process is not None
            and consumer is not None
            and scenario_error is None
        ):
            try:
                restart_process = subprocess.Popen(
                    command,
                    cwd=ctx.repo_root,
                    env=_serve_environment(
                        topics,
                        group_id,
                        transactional_id,
                        bootstrap_servers,
                    ),
                    stdout=subprocess.DEVNULL,
                    stderr=log_file,
                )
                observed = []
                restart_started = time.monotonic()
                restart_deadline = restart_started + RESTART_OBSERVE_S
                while time.monotonic() < restart_deadline:
                    record = _poll_record(consumer, KafkaError, 0.2)
                    if record is not None:
                        observed.append(record)
                    if restart_process.poll() is not None:
                        restart_error = (
                            "restarted serve exited with code "
                            f"{restart_process.returncode}"
                        )
                        break
                restart_evidence.update({
                    "observed_seconds": round(time.monotonic() - restart_started, 3),
                    "new_committed_records": len(observed),
                    "records": [
                        {
                            "topic": record.topic,
                            "partition": record.partition,
                            "offset": record.offset,
                        }
                        for record in observed
                    ],
                })
                restart_passed = (
                    restart_error is None
                    and len(observed) == 0
                    and time.monotonic() - restart_started >= RESTART_OBSERVE_S
                )
            except Exception as exc:
                restart_error = f"{type(exc).__name__}: {exc}"
            finally:
                restart_returncode = _stop_process(restart_process)
                run_record["restart"] = {
                    "command": command,
                    "returncode": restart_returncode,
                    **restart_evidence,
                    "error": restart_error,
                }
        else:
            restart_error = scenario_error or "first serve run did not complete"
            run_record["restart"] = {
                **restart_evidence,
                "error": restart_error,
            }

        kafka_checks.append(_check(
            scenario,
            "no_reprocessing_on_restart",
            restart_passed,
            "restarted serve produced no new committed output records"
            if restart_passed
            else (
                restart_error
                or "restarted serve produced committed output records"
            ),
            {**restart_evidence, "error": restart_error},
        ))

        if consumer is not None:
            consumer.close()

    run_record["duration_ms"] = int((time.monotonic() - started) * 1000)
    run_record["error"] = scenario_error
    run_record["exit_codes"] = [
        0 if scenario_error is None else (
            first_process.returncode
            if first_process is not None and first_process.returncode not in (None, 0)
            else 1
        )
    ]
    _write_json(output_dir / "run.json", run_record)
    return kafka_checks, scenario_error


def _build_processor(ctx: RunContext) -> tuple[Path, dict]:
    output_dir = ctx.run_dir / "outputs" / ctx.candidate
    output_dir.mkdir(parents=True, exist_ok=True)
    target_dir = ctx.repo_root / "rust-processor" / "target" / "integration"
    command = ["cargo", "build", "--release", "--features", "kafka"]
    environment = os.environ.copy()
    environment["CARGO_TARGET_DIR"] = str(target_dir)
    log_path = ctx.run_dir / "build.log"
    started = time.monotonic()
    try:
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.run(
                command,
                cwd=ctx.repo_root / "rust-processor",
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=1800,
                check=False,
            )
        exit_code = process.returncode
        error = None if exit_code == 0 else f"build exited with code {exit_code}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        exit_code = None
        error = f"{type(exc).__name__}: {exc}"
    record = {
        "processor": ctx.candidate,
        "status": "ok" if exit_code == 0 else "failed",
        "command": command,
        "exit_code": exit_code,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "error": error,
        "target_dir": str(target_dir),
    }
    _write_json(output_dir / "build.json", record)
    return target_dir / "release" / "charging-processor", record


def _existing_gate(ctx: RunContext, gate_name: str, module_name: str) -> dict:
    try:
        module = __import__(module_name, fromlist=["run_gate"])
        return module.run_gate(ctx)
    except Exception as exc:
        result = gate_result(
            gate_name,
            [],
            error=f"{type(exc).__name__}: {exc}",
        )
        return write_gate(ctx, result)


def _print_summary(result: dict) -> None:
    checks = result["checks"]
    skipped = sum(item["status"] == "skip" for item in checks)
    summary = result["summary"]
    print(
        f"integration {result['status'].upper()}: "
        f"{summary['passed']} passed, {summary['failed']} failed, "
        f"{skipped} skipped"
    )
    for item in checks:
        if item["status"] in ("fail", "skip"):
            print(f"  {item['status'].upper():4} {item['id']}: {item['detail']}")
    if result.get("error"):
        print(f"  ERROR {result['error']}")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir",
        default=str(REPO_ROOT / "build-output" / "integration"),
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=all_scenarios(),
        help="repeatable; default all",
    )
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--tolerance", type=float, default=1e-6)
    parser.add_argument("--keep", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    run_dir = Path(args.run_dir).resolve()
    if run_dir.exists() and not args.keep:
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    selected = list(dict.fromkeys(args.scenario or all_scenarios()))
    single_scenarios = []
    checks = []
    context = RunContext(
        run_dir=run_dir,
        scenarios=[],
        baseline="expected",
        candidate="candidate-kafka",
        strict=args.strict,
        tolerance=args.tolerance,
    )
    restart_scenarios = [
        scenario
        for scenario in selected
        if context.scenario_meta(scenario).get("run", {}).get("mode") == "restart"
    ]
    for scenario in restart_scenarios:
        checks.extend(restart_skip_checks(scenario))
    fatal_error = None
    _write_json(run_dir / "context.json", {
        "git_sha": git_sha(context.repo_root),
        "git_dirty": git_dirty(context.repo_root),
        "scenarios": selected,
        "baseline": context.baseline,
        "candidate": context.candidate,
        "strict": context.strict,
        "tolerance": context.tolerance,
    })
    try:
        binary, build_record = _build_processor(context)
        if build_record["status"] != "ok":
            fatal_error = f"Kafka-feature Rust build failed: {build_record['error']}"
        else:
            AdminClient = _kafka_clients()[4]
            stale_topics_removed = _cleanup_topics(
                AdminClient({"bootstrap.servers": os.environ.get(
                    "KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"
                )}),
                context.repo_root,
            )
            _write_json(run_dir / "context.json", {
                "git_sha": git_sha(context.repo_root),
                "git_dirty": git_dirty(context.repo_root),
                "scenarios": selected,
                "baseline": context.baseline,
                "candidate": context.candidate,
                "strict": context.strict,
                "tolerance": context.tolerance,
                "stale_integration_topics_removed": stale_topics_removed,
            })
            for scenario in selected:
                metadata = context.scenario_meta(scenario)
                if metadata.get("run", {}).get("mode") == "restart":
                    continue
                single_scenarios.append(scenario)
                kafka_checks, scenario_error = _run_scenario(
                    context,
                    scenario,
                    binary,
                    os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
                )
                checks.extend(kafka_checks)
                if scenario_error is not None:
                    fatal_error = f"{scenario}: {scenario_error}"
                    break

                from verification.diff_harness.comparator import compare, load_outputs

                findings = compare(
                    load_outputs(context.expected_dir(scenario)),
                    load_outputs(context.output_dir(context.candidate, scenario)),
                    tolerance=context.tolerance,
                    strict=context.strict,
                    input_text=(
                        context.scenario_dir(scenario) / "input.jsonl"
                    ).read_text(),
                )
                if findings:
                    checks.append(check(
                        f"integration.output_matches_expected:{scenario}",
                        "fail",
                        f"{len(findings)} behavioral finding(s) against expected output",
                        category="equivalence",
                        verification="runtime",
                        scenario=scenario,
                        evidence={
                            "findings": [finding.to_dict() for finding in findings],
                        },
                    ))
                    break
    except Exception as exc:
        fatal_error = f"{type(exc).__name__}: {exc}"

    context.scenarios = single_scenarios
    behavioral = _existing_gate(
        context,
        "behavioural",
        "verification.diff_harness",
    )
    contract = _existing_gate(
        context,
        "contract",
        "verification.contract_checks",
    )
    checks = behavioral.get("checks", []) + contract.get("checks", []) + checks
    gate_errors = [
        f"{result['gate']}: {result['error']}"
        for result in (behavioral, contract)
        if result.get("status") == "error"
    ]
    if gate_errors:
        fatal_error = "; ".join(([fatal_error] if fatal_error else []) + gate_errors)
    result = gate_result("integration", checks, error=fatal_error)
    result["summary"]["skipped"] = sum(
        item["status"] == "skip" for item in checks
    )
    result["scenarios"] = selected
    write_gate(context, result)
    _print_summary(result)
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
