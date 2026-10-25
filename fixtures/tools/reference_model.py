"""Executable form of docs/build-contract.md, used only to generate fixtures/expected.

This is not a processor under test. It exists so that every expected output
is derived mechanically from the frozen rules instead of being hand-typed.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

ALLOWED_LATENESS_MS = 10 * 60 * 1000
INACTIVITY_TIMEOUT_MS = 30 * 60 * 1000
HIGH_POWER_KW = 150.0
MAX_SEQUENCE = 9007199254740991

TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
VEHICLE_RE = re.compile(r"^VEH-\d{4}$")
SCHEMA_RE = re.compile(r"^(\d+)\.(\d+)$")
LOCATION_RE = re.compile(r"^SYN-[A-Z0-9-]+$")
STATES = ("IDLE", "CHARGING", "COMPLETE", "FAULT")
ID_MAX_LEN = 64


class Invalid(Exception):
    def __init__(self, error_type: str, field_name: str | None):
        self.error_type = error_type
        self.field_name = field_name


def parse_ts(value) -> int | None:
    if not isinstance(value, str) or not TS_RE.match(value):
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000) if dt.microsecond % 1000 == 0 else None


def format_ts(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def valid_id(v) -> bool:
    return isinstance(v, str) and 0 < len(v) <= ID_MAX_LEN


def echo_ids(obj) -> dict:
    if not isinstance(obj, dict):
        return {"event_id": None, "vehicle_id": None, "session_id": None}
    vid = obj.get("vehicle_id")
    return {
        "event_id": obj.get("event_id") if valid_id(obj.get("event_id")) else None,
        "vehicle_id": vid if isinstance(vid, str) and VEHICLE_RE.match(vid) else None,
        "session_id": obj.get("session_id") if valid_id(obj.get("session_id")) else None,
    }


def _reject_constant(token):
    raise ValueError(token)


def validate(line: str) -> dict:
    try:
        obj = json.loads(line, parse_constant=_reject_constant)
    except ValueError:
        raise Invalid("MALFORMED_JSON", None)
    if not isinstance(obj, dict):
        raise Invalid("MALFORMED_JSON", None)

    def present(name):
        if obj.get(name) is None:
            raise Invalid("MISSING_FIELD", name)
        return obj[name]

    sv = present("schema_version")
    m = SCHEMA_RE.match(sv) if isinstance(sv, str) else None
    if not m:
        raise Invalid("INVALID_FIELD", "schema_version")
    if m.group(1) != "1":
        raise Invalid("UNSUPPORTED_SCHEMA_VERSION", "schema_version")
    if not valid_id(present("event_id")):
        raise Invalid("INVALID_FIELD", "event_id")
    v = present("vehicle_id")
    if not (isinstance(v, str) and VEHICLE_RE.match(v)):
        raise Invalid("INVALID_FIELD", "vehicle_id")
    if not valid_id(present("session_id")):
        raise Invalid("INVALID_FIELD", "session_id")
    ts = parse_ts(present("event_timestamp"))
    if ts is None:
        raise Invalid("INVALID_FIELD", "event_timestamp")
    if parse_ts(present("ingestion_timestamp")) is None:
        raise Invalid("INVALID_FIELD", "ingestion_timestamp")
    seq = present("sequence_number")
    if not (is_number(seq) and float(seq).is_integer() and 0 <= seq <= MAX_SEQUENCE):
        raise Invalid("INVALID_FIELD", "sequence_number")
    if present("charging_state") not in STATES:
        raise Invalid("INVALID_FIELD", "charging_state")
    soc = present("battery_soc")
    if not (is_number(soc) and 0 <= soc <= 100):
        raise Invalid("INVALID_FIELD", "battery_soc")
    for name in ("charging_power_kw", "energy_delivered_kwh"):
        x = present(name)
        if not (is_number(x) and x >= 0):
            raise Invalid("INVALID_FIELD", name)
    loc = obj.get("synthetic_location")
    if loc is not None and not (isinstance(loc, str) and LOCATION_RE.match(loc)):
        raise Invalid("INVALID_FIELD", "synthetic_location")
    return {
        "event_id": obj["event_id"],
        "vehicle_id": v,
        "session_id": obj["session_id"],
        "ts": ts,
        "seq": int(seq),
        "state": obj["charging_state"],
        "power": obj["charging_power_kw"],
        "energy": obj["energy_delivered_kwh"],
    }


@dataclass
class Session:
    start_ts: int
    last_ts: int
    max_seq: int
    max_energy: float
    peak_power: float
    event_count: int
    complete: bool

    def expiry(self) -> int:
        return self.last_ts if self.complete else self.last_ts + INACTIVITY_TIMEOUT_MS


@dataclass
class Processor:
    summaries: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    seen: dict = field(default_factory=dict)
    open: dict = field(default_factory=dict)
    closed: set = field(default_factory=set)
    max_event_ts: int | None = None

    def watermark(self) -> int | None:
        return None if self.max_event_ts is None else self.max_event_ts - ALLOWED_LATENESS_MS

    def error(self, line_no, error_type, field_name, ids):
        self.errors.append({**ids, "error_type": error_type, "field": field_name,
                            "input_line": line_no, "schema_version": "1.0"})

    def process(self, line_no: int, line: str):
        try:
            e = validate(line)
        except Invalid as inv:
            try:
                raw = json.loads(line, parse_constant=_reject_constant)
            except ValueError:
                raw = None
            self.error(line_no, inv.error_type, inv.field_name, echo_ids(raw))
            return
        key = (e["vehicle_id"], e["session_id"])
        ids = {"event_id": e["event_id"], "vehicle_id": e["vehicle_id"], "session_id": e["session_id"]}
        seen = self.seen.setdefault(key, set())
        if e["event_id"] in seen:
            return
        seen.add(e["event_id"])
        wm = self.watermark()
        if wm is not None and e["ts"] < wm:
            self.error(line_no, "LATE_EVENT_DROPPED", None, ids)
            return
        if key in self.closed:
            self.error(line_no, "SESSION_ALREADY_CLOSED", None, ids)
        else:
            s = self.open.get(key)
            if s is None:
                self.open[key] = Session(e["ts"], e["ts"], e["seq"], e["energy"], e["power"], 1,
                                         e["state"] == "COMPLETE")
            else:
                s.start_ts = min(s.start_ts, e["ts"])
                s.last_ts = max(s.last_ts, e["ts"])
                s.max_seq = max(s.max_seq, e["seq"])
                s.max_energy = max(s.max_energy, e["energy"])
                s.peak_power = max(s.peak_power, e["power"])
                s.event_count += 1
                s.complete = s.complete or e["state"] == "COMPLETE"
        self.max_event_ts = e["ts"] if self.max_event_ts is None else max(self.max_event_ts, e["ts"])
        self.fire(self.watermark())

    def fire(self, wm: int | None):
        due = sorted((s.expiry(), k) for k, s in self.open.items() if wm is None or s.expiry() <= wm)
        for _, key in due:
            s = self.open.pop(key)
            self.closed.add(key)
            self.summaries.append({
                "session_id": key[1], "vehicle_id": key[0],
                "start_ts": format_ts(s.start_ts), "end_ts": format_ts(s.last_ts),
                "total_energy_kwh": s.max_energy, "peak_power_kw": s.peak_power,
                "event_count": s.event_count,
                "completion_reason": "COMPLETE" if s.complete else "INACTIVITY_TIMEOUT",
                "schema_version": "1.0",
            })

    def end_of_input(self):
        self.fire(None)


def run(lines: list[str]) -> tuple[list, list]:
    p = Processor()
    for i, line in enumerate(lines, start=1):
        p.process(i, line)
    p.end_of_input()
    return p.summaries, p.errors


def consumer_state(summaries: list, replays: int = 1) -> dict:
    latest: dict = {}
    for _ in range(replays):
        for s in summaries:
            latest[(s["vehicle_id"], s["session_id"])] = s
    vehicles: dict = {}
    for (vid, sid) in sorted(latest):
        s = latest[(vid, sid)]
        v = vehicles.setdefault(vid, {"energy": 0.0, "sessions": 0, "high": 0, "timeouts": 0})
        v["energy"] += s["total_energy_kwh"]
        v["sessions"] += 1
        v["high"] += s["peak_power_kw"] >= HIGH_POWER_KW
        v["timeouts"] += s["completion_reason"] == "INACTIVITY_TIMEOUT"
    return {"schema_version": "1.0", "vehicles": {vid: {
        "warranty_energy_kwh": round(v["energy"], 3),
        "session_count": v["sessions"],
        "high_power_session_count": v["high"],
        "inactivity_timeout_count": v["timeouts"],
    } for vid, v in vehicles.items()}}


def canonical(record) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
