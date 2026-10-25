from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from verification.common import REPO_ROOT
from verification.integration.harness import (
    KafkaRecord,
    RESTART_SKIP_REASON,
    SENTINEL_VEHICLE_ID,
    build_watermark_sentinel,
    check_partition_affinity,
    filter_sentinel_records,
    restart_skip_checks,
)


def test_watermark_sentinel_is_valid_later_and_uses_reserved_vehicle() -> None:
    sentinel = build_watermark_sentinel(
        REPO_ROOT,
        "normal_session",
        event_id="EVT-WM-UNIT-TEST",
    )
    schema = json.loads(
        (REPO_ROOT / "contracts" / "charging.events.v1.schema.json").read_text()
    )
    Draft202012Validator(schema).validate(sentinel)
    fixture = (REPO_ROOT / "fixtures" / "scenarios" / "normal_session" /
               "input.jsonl").read_text()
    event_times = [
        json.loads(line)["event_timestamp"]
        for line in fixture.splitlines()
        if line.strip()
    ]

    assert sentinel["vehicle_id"] == SENTINEL_VEHICLE_ID
    assert sentinel["session_id"] == "watermark-sentinel"
    assert sentinel["event_timestamp"] > max(event_times)
    for input_path in (REPO_ROOT / "fixtures" / "scenarios").glob("*/input.jsonl"):
        assert SENTINEL_VEHICLE_ID not in input_path.read_text()


def test_sentinel_records_are_filtered() -> None:
    records = [
        KafkaRecord(
            "summaries",
            0,
            1,
            "VEH-9999",
            b'{"vehicle_id":"VEH-9999","session_id":"watermark-sentinel"}',
        ),
        KafkaRecord(
            "summaries",
            1,
            0,
            "VEH-0001",
            b'{"vehicle_id":"VEH-0001","session_id":"S-0001-A"}',
        ),
    ]

    assert filter_sentinel_records(records) == records[1:]


def test_partition_affinity_accepts_expected_vehicle_order() -> None:
    expected = [
        {"vehicle_id": "VEH-0001", "session_id": "S-0001-A"},
        {"vehicle_id": "VEH-0001", "session_id": "S-0001-B"},
    ]
    records = [
        {
            "partition": 2,
            "offset": 4,
            "key": "VEH-0001",
            "value": expected[0],
        },
        {
            "partition": 2,
            "offset": 8,
            "key": "VEH-0001",
            "value": expected[1],
        },
    ]

    passed, evidence = check_partition_affinity(records, expected)

    assert passed
    assert evidence == {
        "key_mismatches": [],
        "vehicles_in_multiple_partitions": {},
        "per_vehicle_order_mismatches": {},
        "invalid_records": [],
    }


def test_partition_affinity_rejects_wrong_keys_partitions_and_order() -> None:
    expected = [
        {"vehicle_id": "VEH-0001", "session_id": "S-0001-A"},
        {"vehicle_id": "VEH-0001", "session_id": "S-0001-B"},
    ]
    records = [
        {
            "partition": 0,
            "offset": 4,
            "key": "VEH-WRONG",
            "value": expected[1],
        },
        {
            "partition": 1,
            "offset": 2,
            "key": "VEH-0001",
            "value": expected[0],
        },
    ]

    passed, evidence = check_partition_affinity(records, expected)

    assert not passed
    assert evidence["key_mismatches"]
    assert evidence["vehicles_in_multiple_partitions"] == {
        "VEH-0001": [0, 1],
    }
    assert evidence["per_vehicle_order_mismatches"]["VEH-0001"] == {
        "expected": ["S-0001-A", "S-0001-B"],
        "actual": ["S-0001-B", "S-0001-A"],
    }


def test_restart_scenarios_are_explicitly_skipped() -> None:
    for scenario in ("processor_restart", "state_recovery"):
        checks = restart_skip_checks(scenario)

        assert [item["status"] for item in checks] == ["skip", "skip", "skip"]
        assert all(item["detail"] == RESTART_SKIP_REASON for item in checks)
        assert all(item["scenario"] == scenario for item in checks)
