from __future__ import annotations

import json
import os
from pathlib import Path

from verification.cutover.lease import read_lease_unlocked
from verification.cutover.storage import (
    append_audit_unlocked,
    locked,
    sha256_bytes,
)


def append_summary(
    state_dir: Path,
    *,
    producer: str,
    fencing_token: int,
    scenario: str,
    input_sha256: str,
    record: dict,
) -> bool:
    record_sha256 = sha256_bytes(
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    with locked(state_dir):
        lease = read_lease_unlocked(state_dir)
        if lease["holder"] != producer or lease["token"] != fencing_token:
            append_audit_unlocked(
                state_dir,
                event="produce-fenced",
                actor=producer,
                from_value=producer,
                to_value=lease["holder"],
                details={
                    "expected_token": fencing_token,
                    "current_token": lease["token"],
                    "input_sha256": input_sha256,
                    "record_sha256": record_sha256,
                    "scenario": scenario,
                },
            )
            return False

        entry = {
            "producer": producer,
            "fencing_token": fencing_token,
            "key": record["vehicle_id"],
            "record": record,
        }
        payload = (
            json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        fd = os.open(state_dir / "topic.jsonl", os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(fd, remaining)
                if written == 0:
                    raise OSError("topic append wrote no bytes")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        append_audit_unlocked(
            state_dir,
            event="topic-append",
            actor=producer,
            from_value=producer,
            to_value="topic",
            details={
                "fencing_token": fencing_token,
                "input_sha256": input_sha256,
                "record_sha256": record_sha256,
                "scenario": scenario,
            },
        )
    return True
