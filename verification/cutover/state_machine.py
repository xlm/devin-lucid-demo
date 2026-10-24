from __future__ import annotations

from pathlib import Path

from verification.cutover.storage import (
    CutoverError,
    append_audit_unlocked,
    locked,
    read_json,
    utc_now,
    write_json_atomic,
    write_text_atomic,
)

TRANSITIONS = {
    (None, "init"): "LegacyActive",
    ("LegacyActive", "start-candidate"): "CandidateDev",
    ("CandidateDev", "shadow"): "ShadowValidation",
    ("ShadowValidation", "evaluate-fail"): "ValidationFailed",
    ("ShadowValidation", "evaluate-pass"): "ReadyForReview",
    ("ValidationFailed", "rework"): "CandidateDev",
    ("ReadyForReview", "approve"): "ApprovedForCutover",
    ("ApprovedForCutover", "cutover"): "CandidateActive",
    ("CandidateActive", "rollback"): "RolledBack",
    ("RolledBack", "restore"): "LegacyActive",
}


class InvalidTransition(CutoverError):
    pass


def state_path(state_dir: Path) -> Path:
    return state_dir / "state.json"


def get_state_unlocked(state_dir: Path) -> str | None:
    path = state_path(state_dir)
    if not path.exists():
        return None
    state = read_json(path).get("state")
    if not isinstance(state, str):
        raise CutoverError(f"invalid state in {path}")
    return state


def get_state(state_dir: Path) -> str:
    with locked(state_dir):
        state = get_state_unlocked(state_dir)
    if state is None:
        raise CutoverError(f"state is not initialized in {state_dir}")
    return state


def target_state(current: str | None, event: str) -> str:
    try:
        return TRANSITIONS[(current, event)]
    except KeyError as exc:
        source = current if current is not None else "Uninitialized"
        raise InvalidTransition(f"transition rejected: {source} --{event}--> (no legal transition)") from exc


def initialize_state(state_dir: Path, *, force: bool = False, actor: str = "cutover-cli") -> str:
    state_dir.mkdir(parents=True, exist_ok=True)
    with locked(state_dir):
        if state_path(state_dir).exists() and not force:
            raise CutoverError(f"state already exists in {state_dir}; use --force to reinitialize")
        for name in ("state.json", "lease.json"):
            (state_dir / name).unlink(missing_ok=True)
        write_text_atomic(state_dir / "audit.jsonl", "")
        write_text_atomic(state_dir / "topic.jsonl", "")

        state = target_state(None, "init")
        write_json_atomic(state_path(state_dir), {
            "state": state,
            "updated_at": utc_now(),
        })
        append_audit_unlocked(
            state_dir,
            event="init",
            actor=actor,
            from_value=None,
            to_value=state,
            details={},
        )
        lease = {"holder": "legacy", "token": 1, "acquired_at": utc_now()}
        write_json_atomic(state_dir / "lease.json", lease)
        append_audit_unlocked(
            state_dir,
            event="lease-acquire",
            actor=actor,
            from_value=None,
            to_value="legacy",
            details={"token": 1},
        )
        return state


def transition(
    state_dir: Path,
    event: str,
    *,
    actor: str = "cutover-cli",
    details: dict | None = None,
) -> tuple[str, str]:
    with locked(state_dir):
        current = get_state_unlocked(state_dir)
        target = target_state(current, event)
        write_json_atomic(state_path(state_dir), {"state": target, "updated_at": utc_now()})
        append_audit_unlocked(
            state_dir,
            event=event,
            actor=actor,
            from_value=current,
            to_value=target,
            details=details,
        )
    return current, target
