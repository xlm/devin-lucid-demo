"""Executable form of docs/battery-health-processor.md, used only to generate
fixtures/battery/expected.

This is not a processor under test. It exists so that every expected output of
the battery-health processor is derived mechanically from the written rules
instead of being hand-typed. Timestamp, number and canonical-JSON rules are
the same as the charging model's (fixtures/tools/reference_model.py); they are
copied here so this processor's fixtures stand on their own.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

ALLOWED_LATENESS_MS = 10 * 60 * 1000
MAX_SEQUENCE = 9007199254740991
ID_MAX_LEN = 64
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", re.ASCII)
DAY_MS = 24 * 60 * 60 * 1000
VEHICLE_RE = re.compile(r"^VEH-\d{4}$", re.ASCII)
SCHEMA_RE = re.compile(r"^(\d+)\.(\d+)$", re.ASCII)
LOCATION_RE = re.compile(r"^SYN-[A-Z0-9-]+$")
VOLTAGE_MAX_V = 5.0
TEMP_MIN_C = -50.0
TEMP_MAX_C = 100.0


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


def canonical(record) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Invalid(Exception):
    def __init__(self, error_type: str, field_name: str | None):
        self.error_type = error_type
        self.field_name = field_name


def day_of(ts: int) -> str:
    return format_ts(ts - ts % DAY_MS)[:10]


def echo_ids(obj) -> dict:
    if not isinstance(obj, dict):
        return {"event_id": None, "vehicle_id": None, "pack_id": None}
    vid = obj.get("vehicle_id")
    return {
        "event_id": obj.get("event_id") if valid_id(obj.get("event_id")) else None,
        "vehicle_id": vid if isinstance(vid, str) and VEHICLE_RE.match(vid) else None,
        "pack_id": obj.get("pack_id") if valid_id(obj.get("pack_id")) else None,
    }


def _reject_constant(token):
    raise ValueError(token)


def _parse(line: str):
    try:
        return json.loads(line, parse_constant=_reject_constant)
    except ValueError:
        return None


def validate(line: str) -> dict:
    obj = _parse(line)
    if not isinstance(obj, dict):
        raise Invalid("MALFORMED_JSON", None)

    def present(name):
        if obj.get(name) is None:
            raise Invalid("MISSING_FIELD", name)
        return obj[name]

    def in_range(name, lo, hi):
        x = present(name)
        if not (is_number(x) and lo <= x <= hi):
            raise Invalid("INVALID_FIELD", name)
        return x

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
    if not valid_id(present("pack_id")):
        raise Invalid("INVALID_FIELD", "pack_id")
    ts = parse_ts(present("event_timestamp"))
    if ts is None:
        raise Invalid("INVALID_FIELD", "event_timestamp")
    if parse_ts(present("ingestion_timestamp")) is None:
        raise Invalid("INVALID_FIELD", "ingestion_timestamp")
    seq = present("sequence_number")
    if not (is_number(seq) and float(seq).is_integer() and 0 <= seq <= MAX_SEQUENCE):
        raise Invalid("INVALID_FIELD", "sequence_number")
    vmin = in_range("cell_voltage_min_v", 0, VOLTAGE_MAX_V)
    vmax = in_range("cell_voltage_max_v", 0, VOLTAGE_MAX_V)
    if vmax < vmin:
        raise Invalid("INVALID_FIELD", "cell_voltage_max_v")
    temp = in_range("pack_temp_c", TEMP_MIN_C, TEMP_MAX_C)
    soh = in_range("state_of_health_pct", 0, 100)
    loc = obj.get("synthetic_location")
    if loc is not None and not (isinstance(loc, str) and LOCATION_RE.match(loc)):
        raise Invalid("INVALID_FIELD", "synthetic_location")
    return {"event_id": obj["event_id"], "vehicle_id": v, "pack_id": obj["pack_id"], "ts": ts,
            "seq": int(seq), "vmin": vmin, "vmax": vmax, "temp": temp, "soh": soh}


@dataclass
class Day:
    day_end: int
    min_v: float
    max_v: float
    max_imbalance: float
    max_temp: float
    latest: tuple  # (event_ts, sequence_number) of the event that set soh
    soh: float
    event_count: int

    @classmethod
    def start(cls, e) -> "Day":
        return cls(e["ts"] - e["ts"] % DAY_MS + DAY_MS, e["vmin"], e["vmax"], e["vmax"] - e["vmin"],
                   e["temp"], (e["ts"], e["seq"]), e["soh"], 1)

    def add(self, e) -> None:
        # Strict comparisons: on ties the stored reading is kept (first accepted wins).
        if e["vmin"] < self.min_v:
            self.min_v = e["vmin"]
        if e["vmax"] > self.max_v:
            self.max_v = e["vmax"]
        imbalance = e["vmax"] - e["vmin"]
        if imbalance > self.max_imbalance:
            self.max_imbalance = imbalance
        if e["temp"] > self.max_temp:
            self.max_temp = e["temp"]
        if (e["ts"], e["seq"]) > self.latest:
            self.latest, self.soh = (e["ts"], e["seq"]), e["soh"]
        self.event_count += 1


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
            self.error(line_no, inv.error_type, inv.field_name, echo_ids(_parse(line)))
            return
        key = (e["vehicle_id"], e["pack_id"], day_of(e["ts"]))
        ids = {"event_id": e["event_id"], "vehicle_id": e["vehicle_id"], "pack_id": e["pack_id"]}
        seen = self.seen.setdefault(key, set())
        if e["event_id"] in seen:
            return
        seen.add(e["event_id"])
        wm = self.watermark()
        if wm is not None and e["ts"] < wm:
            self.error(line_no, "DROPPED_LATE", None, ids)
            return
        # A day closes only once watermark >= day_end, so an event for a closed day is late.
        assert key not in self.closed
        d = self.open.get(key)
        if d is None:
            self.open[key] = Day.start(e)
        else:
            d.add(e)
        self.max_event_ts = e["ts"] if self.max_event_ts is None else max(self.max_event_ts, e["ts"])
        self.fire(self.watermark())

    def fire(self, wm: int | None):
        due = sorted((d.day_end, k) for k, d in self.open.items() if wm is None or d.day_end <= wm)
        for _, key in due:
            d = self.open.pop(key)
            self.closed.add(key)
            self.summaries.append({
                "vehicle_id": key[0], "pack_id": key[1], "day": key[2],
                "min_cell_voltage_v": d.min_v, "max_cell_voltage_v": d.max_v,
                "max_cell_imbalance_v": d.max_imbalance, "max_pack_temp_c": d.max_temp,
                "latest_state_of_health_pct": d.soh, "event_count": d.event_count,
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
