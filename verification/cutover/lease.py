from __future__ import annotations

from pathlib import Path

from verification.cutover.storage import (
    CutoverError,
    append_audit_unlocked,
    locked,
    read_audit_unlocked,
    read_json,
    utc_now,
    write_json_atomic,
)


def read_lease_unlocked(state_dir: Path) -> dict:
    lease = read_json(state_dir / "lease.json")
    holder = lease.get("holder")
    token = lease.get("token")
    if holder is not None and not isinstance(holder, str):
        raise CutoverError(f"invalid lease holder in {state_dir / 'lease.json'}")
    if not isinstance(token, int) or isinstance(token, bool) or token < 1:
        raise CutoverError(f"invalid lease token in {state_dir / 'lease.json'}")
    return lease


def read_lease(state_dir: Path) -> dict:
    with locked(state_dir):
        return read_lease_unlocked(state_dir)


def release_lease(state_dir: Path, holder: str, *, actor: str = "cutover-cli") -> dict:
    with locked(state_dir):
        lease = read_lease_unlocked(state_dir)
        if lease["holder"] is None:
            return lease
        if lease["holder"] != holder:
            raise CutoverError(
                f"cannot release lease held by {lease['holder']}; expected {holder}"
            )
        released = {"holder": None, "token": lease["token"], "acquired_at": None}
        write_json_atomic(state_dir / "lease.json", released)
        append_audit_unlocked(
            state_dir,
            event="lease-release",
            actor=actor,
            from_value=holder,
            to_value=None,
            details={"token": lease["token"]},
        )
        return released


def acquire_lease(state_dir: Path, holder: str, *, actor: str = "cutover-cli") -> dict:
    with locked(state_dir):
        lease = read_lease_unlocked(state_dir)
        if lease["holder"] == holder:
            return lease
        if lease["holder"] is not None:
            raise CutoverError(f"cannot acquire lease for {holder}; held by {lease['holder']}")
        acquired = {
            "holder": holder,
            "token": lease["token"] + 1,
            "acquired_at": utc_now(),
        }
        write_json_atomic(state_dir / "lease.json", acquired)
        append_audit_unlocked(
            state_dir,
            event="lease-acquire",
            actor=actor,
            from_value=None,
            to_value=holder,
            details={"token": acquired["token"]},
        )
        return acquired


def handover_lease(
    state_dir: Path,
    old_holder: str,
    new_holder: str,
    *,
    actor: str = "cutover-cli",
) -> dict:
    lease = read_lease(state_dir)
    if lease["holder"] == new_holder:
        return lease
    if lease["holder"] not in (old_holder, None):
        raise CutoverError(
            f"cannot hand over lease from {old_holder}; held by {lease['holder']}"
        )
    if lease["holder"] == old_holder:
        release_lease(state_dir, old_holder, actor=actor)
    return acquire_lease(state_dir, new_holder, actor=actor)


def producer_token(state_dir: Path, producer: str) -> int | None:
    with locked(state_dir):
        lease = read_lease_unlocked(state_dir)
        if lease["holder"] == producer:
            return lease["token"]
        acquisitions = [
            entry.get("details", {}).get("token")
            for entry in read_audit_unlocked(state_dir)
            if entry.get("event") == "lease-acquire" and entry.get("to") == producer
        ]
        for token in reversed(acquisitions):
            if isinstance(token, int) and not isinstance(token, bool):
                return token
    return None
