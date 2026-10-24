from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


class CutoverError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@contextmanager
def locked(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "lease.lock").open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CutoverError(f"required file is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CutoverError(f"cannot read JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CutoverError(f"expected a JSON object in {path}")
    return value


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def write_json_atomic(path: Path, value: dict) -> None:
    write_text_atomic(path, json.dumps(value, sort_keys=True, indent=2) + "\n")


def append_audit_unlocked(
    state_dir: Path,
    *,
    event: str,
    actor: str,
    from_value: str | None,
    to_value: str | None,
    details: dict | None = None,
) -> dict:
    entry = {
        "ts": utc_now(),
        "from": from_value,
        "to": to_value,
        "event": event,
        "actor": actor,
        "details": details or {},
    }
    path = state_dir / "audit.jsonl"
    with path.open("a", encoding="utf-8") as audit_file:
        audit_file.write(json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        audit_file.write("\n")
        audit_file.flush()
        os.fsync(audit_file.fileno())
    return entry


def read_audit_unlocked(state_dir: Path) -> list[dict]:
    path = state_dir / "audit.jsonl"
    if not path.exists():
        return []
    entries = []
    try:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("entry is not a JSON object")
                entries.append(value)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise CutoverError(f"cannot read audit log {path}: {exc}") from exc
    return entries


def read_audit(state_dir: Path) -> list[dict]:
    with locked(state_dir):
        return read_audit_unlocked(state_dir)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
