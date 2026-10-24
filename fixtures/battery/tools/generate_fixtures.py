"""Regenerate fixtures/battery/scenarios and fixtures/battery/expected.

Usage: python fixtures/battery/tools/generate_fixtures.py [--check]
With --check, fail if any committed fixture differs from what would be generated.
All timestamps are fixed offsets from BASE, so output never depends on the clock.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import reference_model as rm  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BASE_MS = rm.parse_ts("2026-01-15T08:00:00.000Z")
MIN = 60_000


def ts(minute: float, extra_ms: int = 0) -> str:
    return rm.format_ts(BASE_MS + int(round(minute * MIN)) + extra_ms)


def ev(eid, veh, pack, minute, seq, vmin, vmax, temp, soh, *, extra_ms=0, schema="1.0",
       delay_s=3, **extra) -> str:
    rec = {
        "event_id": eid, "vehicle_id": veh, "pack_id": pack,
        "event_timestamp": ts(minute, extra_ms),
        "ingestion_timestamp": ts(minute, extra_ms + delay_s * 1000),
        "sequence_number": seq,
        "cell_voltage_min_v": vmin, "cell_voltage_max_v": vmax,
        "pack_temp_c": temp, "state_of_health_pct": soh,
        "schema_version": schema,
    }
    rec.update(extra)
    return json.dumps(rec, separators=(",", ":"))


def series(prefix, veh, pack, minutes, *, start_seq=1, v0=3.62, temp0=21.0, soh=97.4):
    """Ordered, well-behaved readings for one pack: small imbalance, slowly warming."""
    return [ev(f"BAT-{prefix}-{i + 1:03d}", veh, pack, m, start_seq + i,
               round(v0 + 0.01 * (i % 3), 3), round(v0 + 0.03 + 0.01 * (i % 2), 3),
               round(temp0 + 0.5 * i, 1), round(soh - 0.01 * i, 2))
            for i, m in enumerate(minutes)]


SCENARIOS: dict[str, dict] = {}


def scenario(name, description, covers, lines, *, run=None):
    SCENARIOS[name] = {
        "meta": {"name": name, "description": description, "covers": covers,
                 "run": run or {"mode": "single"}},
        "lines": lines,
    }


na = series("NRM-A", "VEH-0101", "PK-0101-A", [0, 30, 60, 90, 120, 180, 240, 300])
nb = series("NRM-B", "VEH-0101", "PK-0101-B", [5, 65, 125, 245], v0=3.58, temp0=19.5, soh=93.1)
scenario(
    "normal_day",
    "One vehicle with two packs, readings in order within one UTC day; one summary per "
    "(vehicle_id, pack_id, day), emitted by the end-of-input flush.",
    ["normal day", "per-pack key", "end-of-input flush"],
    [na[0], nb[0], na[1], na[2], nb[1], na[3], na[4], nb[2], na[5], na[6], nb[3], na[7]],
)

# Minute 950 is 23:50 on 2026-01-15; minute 960 is 00:00 on 2026-01-16.
scenario(
    "day_boundary",
    "Readings either side of UTC midnight split into two daily keys. 23:59:59.999 belongs "
    "to the first day and 00:00:00.000 to the second. The first day closes mid-stream "
    "exactly when the watermark reaches its day end, and a later reading for it is "
    "DROPPED_LATE.",
    ["day boundary", "day closes at watermark == day end", "late after close"],
    [ev("BAT-DAY-001", "VEH-0102", "PK-0102-A", 940, 1, 3.61, 3.66, 24.0, 95.5),
     ev("BAT-DAY-002", "VEH-0102", "PK-0102-A", 955, 2, 3.60, 3.67, 24.5, 95.5),
     ev("BAT-DAY-003", "VEH-0102", "PK-0102-A", 959, 3, 3.59, 3.65, 25.0, 95.4, extra_ms=59_999),
     ev("BAT-DAY-004", "VEH-0102", "PK-0102-A", 960, 4, 3.62, 3.66, 25.5, 95.3),
     ev("BAT-DAY-005", "VEH-0102", "PK-0102-A", 965, 5, 3.62, 3.67, 25.0, 95.3),
     ev("BAT-DAY-006", "VEH-0102", "PK-0102-A", 969, 6, 3.63, 3.67, 24.5, 95.3, extra_ms=59_999),
     ev("BAT-DAY-007", "VEH-0102", "PK-0102-A", 970, 7, 3.63, 3.68, 24.0, 95.2),
     ev("BAT-DAY-008", "VEH-0102", "PK-0102-A", 959, 8, 3.40, 3.90, 40.0, 80.0, extra_ms=59_998),
     ev("BAT-DAY-009", "VEH-0102", "PK-0102-A", 980, 9, 3.64, 3.68, 23.5, 95.2)],
)

d3 = series("DUP", "VEH-0103", "PK-0103-A", [0, 20, 40, 60, 80])
scenario(
    "duplicates",
    "Exact duplicates, a same-event_id duplicate with a different payload (first wins), "
    "event_id reuse on another pack and on another day of the same pack (both accepted: "
    "dedupe is scoped to the daily key), and a duplicate after the day closed (silently "
    "dropped, not DROPPED_LATE).",
    ["duplicates", "dedupe keeps first", "dedupe is scoped to the daily key"],
    [d3[0], d3[1], d3[1],
     ev("BAT-DUP-002", "VEH-0103", "PK-0103-B", 25, 1, 3.55, 3.60, 20.0, 91.0),
     d3[2],
     ev("BAT-DUP-003", "VEH-0103", "PK-0103-A", 41, 3, 3.10, 4.10, 55.0, 50.0),
     d3[3], d3[4],
     ev("BAT-DUP-001", "VEH-0103", "PK-0103-A", 1500, 6, 3.61, 3.64, 18.0, 97.3),
     ev("BAT-DUP-901", "VEH-0103", "PK-0103-A", 1520, 7, 3.61, 3.65, 18.5, 97.3),
     d3[2]],
)

scenario(
    "out_of_order",
    "Readings arrive out of event-time order within allowed lateness. "
    "latest_state_of_health_pct follows the greatest event_timestamp, not arrival order, "
    "and on equal timestamps the greater sequence_number wins even when it arrives first.",
    ["out-of-order", "latest by event time", "sequence tie-break"],
    [ev("BAT-OOO-001", "VEH-0104", "PK-0104-A", 0, 1, 3.60, 3.65, 22.0, 96.0),
     ev("BAT-OOO-003", "VEH-0104", "PK-0104-A", 8, 3, 3.61, 3.66, 23.0, 95.8),
     ev("BAT-OOO-002", "VEH-0104", "PK-0104-A", 4, 2, 3.58, 3.64, 26.5, 95.9),
     ev("BAT-OOO-005", "VEH-0104", "PK-0104-A", 12, 5, 3.62, 3.67, 24.0, 95.6),
     ev("BAT-OOO-004", "VEH-0104", "PK-0104-A", 12, 4, 3.62, 3.66, 24.0, 95.7),
     ev("BAT-OOO-101", "VEH-0105", "PK-0105-A", 3, 1, 3.55, 3.61, 30.0, 88.0),
     ev("BAT-OOO-102", "VEH-0105", "PK-0105-A", 9, 2, 3.56, 3.60, 31.0, 87.9),
     ev("BAT-OOO-006", "VEH-0104", "PK-0104-A", 6, 6, 3.59, 3.65, 23.5, 99.0)],
)

scenario(
    "late_beyond_watermark",
    "Watermark = max event time - 10 min. A reading exactly at the watermark is accepted, "
    "one 1 ms behind it is DROPPED_LATE and does not move the watermark, a later copy of "
    "that dropped reading is silently de-duplicated, and readings far behind are dropped.",
    ["late events", "watermark boundary", "dedupe before lateness"],
    [ev("BAT-LAT-001", "VEH-0106", "PK-0106-A", 0, 1, 3.60, 3.66, 22.0, 94.0),
     ev("BAT-LAT-002", "VEH-0106", "PK-0106-A", 30, 2, 3.61, 3.66, 22.5, 94.0),
     ev("BAT-LAT-003", "VEH-0106", "PK-0106-A", 20, 3, 3.59, 3.67, 23.0, 93.9),
     ev("BAT-LAT-004", "VEH-0106", "PK-0106-A", 20, 4, 3.20, 3.90, 45.0, 80.0, extra_ms=-1),
     ev("BAT-LAT-005", "VEH-0107", "PK-0107-A", 25, 1, 3.57, 3.62, 21.0, 90.0),
     ev("BAT-LAT-004", "VEH-0106", "PK-0106-A", 20, 4, 3.20, 3.90, 45.0, 80.0, extra_ms=-1),
     ev("BAT-LAT-006", "VEH-0106", "PK-0106-A", 60, 5, 3.62, 3.67, 23.5, 93.8),
     ev("BAT-LAT-007", "VEH-0107", "PK-0107-A", 5, 2, 3.00, 4.20, 60.0, 70.0),
     ev("BAT-LAT-008", "VEH-0106", "PK-0106-A", 50, 6, 3.62, 3.66, 23.0, 93.8)],
)

ma = series("MAL", "VEH-0108", "PK-0108-A", [0, 10, 20, 30])


def bad(n, minute=15, **override):
    rec = json.loads(ev(f"BAT-MAL-B{n:02d}", "VEH-0108", "PK-0108-A", minute, 100 + n,
                        3.60, 3.65, 22.0, 96.0))
    for k, v in override.items():
        if v is ...:
            rec.pop(k)
        else:
            rec[k] = v
    return json.dumps(rec, separators=(",", ":"))


scenario(
    "malformed_null",
    "Malformed JSON, non-object JSON, missing/null/invalid fields, out-of-range "
    "measurements, cell_voltage_max_v below cell_voltage_min_v, NaN, an empty line, a "
    "VIN-like vehicle_id, an impossible date, an unsupported major version and a "
    "real-looking location are routed to the error path. A valid SYN-* location and an "
    "unknown additive field are accepted but never propagated; the valid readings still "
    "produce a summary.",
    ["malformed", "null measurements", "error path", "identifier echo rules",
     "restricted field not propagated"],
    [ma[0],
     "this is not json",
     "[1,2,3]",
     bad(1, event_id=...),
     bad(2, pack_temp_c=None),
     bad(3, pack_id=""),
     bad(4, cell_voltage_min_v=-0.1),
     bad(5, cell_voltage_max_v=5.5),
     bad(6, cell_voltage_min_v=3.70, cell_voltage_max_v=3.60),
     ma[1],
     bad(7, state_of_health_pct=101),
     bad(8, pack_temp_c=-60.0),
     bad(9, event_timestamp="2026-01-15 08:15:00"),
     "",
     bad(10, sequence_number=3.5),
     bad(11, state_of_health_pct="96.0"),
     bad(12, schema_version=...),
     bad(13, vehicle_id="WVWZZZ1JZXW000001"),
     bad(14).replace('"pack_temp_c":22.0', '"pack_temp_c":NaN'),
     bad(15, event_timestamp="2026-02-30T08:15:00.000Z"),
     bad(16, schema_version="2.0"),
     bad(17, synthetic_location="52.5200,13.4050"),
     bad(18, cell_voltage_max_v=True),
     ma[2].replace('"schema_version":"1.0"',
                   '"schema_version":"1.1","synthetic_location":"SYN-GRID-0108","bms_build":"syn-7"'),
     ma[3]],
)

scenario(
    "imbalance_trap",
    "The lowest cell voltage and the highest cell voltage of the day come from different "
    "readings. max_cell_imbalance_v is the largest per-reading (max - min) spread, which "
    "is well below max_cell_voltage_v - min_cell_voltage_v. A second pack has its worst "
    "spread on a reading that sets neither daily extreme.",
    ["semantic trap", "per-reading imbalance", "binary64 subtraction"],
    [ev("BAT-IMB-001", "VEH-0109", "PK-0109-A", 0, 1, 3.60, 3.70, 22.0, 92.0),
     ev("BAT-IMB-101", "VEH-0109", "PK-0109-B", 10, 1, 3.40, 3.45, 20.0, 89.0),
     ev("BAT-IMB-002", "VEH-0109", "PK-0109-A", 30, 2, 3.50, 3.55, 21.0, 92.0),
     ev("BAT-IMB-102", "VEH-0109", "PK-0109-B", 40, 2, 3.52, 3.71, 24.0, 88.9),
     ev("BAT-IMB-003", "VEH-0109", "PK-0109-A", 60, 3, 3.68, 3.80, 27.0, 91.9),
     ev("BAT-IMB-103", "VEH-0109", "PK-0109-B", 70, 3, 3.75, 3.85, 23.0, 88.9),
     ev("BAT-IMB-004", "VEH-0109", "PK-0109-A", 90, 4, 3.66, 3.74, 25.0, 91.9)],
)

ra = series("RST-A", "VEH-0110", "PK-0110-A", [940, 950, 958, 963, 970, 975])
rb = series("RST-B", "VEH-0111", "PK-0111-A", [945, 961, 972], v0=3.55, soh=90.2)
scenario(
    "processor_restart",
    "The processor stops after line 5, checkpoints and resumes across a UTC midnight. "
    "After resuming, a duplicate of a pre-restart reading and a reading behind the "
    "checkpointed watermark must be handled exactly as in an uninterrupted run.",
    ["processor restart", "checkpoint/resume", "checkpointed dedupe", "checkpointed watermark"],
    [ra[0], rb[0], ra[1], ra[2], rb[1], ra[3], ra[1], ra[4],
     ev("BAT-RST-L01", "VEH-0111", "PK-0111-A", 950, 9, 3.50, 3.70, 30.0, 80.0),
     rb[2], ra[5]],
    run={"mode": "restart", "stop_after": 5},
)


def render() -> dict[Path, str]:
    files: dict[Path, str] = {}
    for name, sc in SCENARIOS.items():
        lines = sc["lines"]
        files[ROOT / "scenarios" / name / "input.jsonl"] = "".join(l + "\n" for l in lines)
        files[ROOT / "scenarios" / name / "scenario.json"] = json.dumps(sc["meta"], indent=2) + "\n"
        summaries, errors = rm.run(lines)
        exp = ROOT / "expected" / name
        files[exp / "summaries.jsonl"] = "".join(rm.canonical(r) + "\n" for r in summaries)
        files[exp / "errors.jsonl"] = "".join(rm.canonical(r) + "\n" for r in errors)
    return files


def main() -> int:
    files = render()
    if "--check" in sys.argv:
        stale = [p for p, c in files.items() if not p.exists() or p.read_text() != c]
        for p in stale:
            print(f"stale fixture: {p.relative_to(ROOT.parents[1])}")
        return 1 if stale else 0
    for p, c in files.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(c)
    print(f"wrote {len(files)} files for {len(SCENARIOS)} scenarios")
    return 0


if __name__ == "__main__":
    sys.exit(main())
