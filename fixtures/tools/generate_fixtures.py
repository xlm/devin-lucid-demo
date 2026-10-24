"""Regenerate fixtures/scenarios and fixtures/expected from the frozen contract.

Usage: python fixtures/tools/generate_fixtures.py [--check]
With --check, fail if any committed fixture differs from what would be generated.
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


def ev(eid, veh, sess, minute, seq, state, soc, power, energy, *, extra_ms=0,
       schema="1.0", delay_s=2, **extra) -> str:
    rec = {
        "event_id": eid, "vehicle_id": veh, "session_id": sess,
        "event_timestamp": ts(minute, extra_ms),
        "ingestion_timestamp": ts(minute, extra_ms + delay_s * 1000),
        "sequence_number": seq, "charging_state": state, "battery_soc": soc,
        "charging_power_kw": power, "energy_delivered_kwh": energy,
        "schema_version": schema,
    }
    rec.update(extra)
    return json.dumps(rec, separators=(",", ":"))


def session(prefix, veh, sess, minutes, energies, powers, *, complete=True, start_seq=1, soc0=20.0):
    """Ordered events for one session; the last is COMPLETE when complete=True."""
    out = []
    for i, (m, e, p) in enumerate(zip(minutes, energies, powers)):
        last = i == len(minutes) - 1
        state = "COMPLETE" if complete and last else "CHARGING"
        out.append((m, ev(f"EVT-{prefix}-{i + 1:03d}", veh, sess, m, start_seq + i, state,
                          round(soc0 + 2.5 * i, 1), 0.0 if state == "COMPLETE" else p, e)))
    return out


def by_time(*sessions):
    return [line for _, line in sorted((x for s in sessions for x in s), key=lambda t: t[0])]


SCENARIOS: dict[str, dict] = {}


def scenario(name, description, covers, lines, *, run=None, consumer_replays=1):
    SCENARIOS[name] = {
        "meta": {"name": name, "description": description, "covers": covers,
                 "run": run or {"mode": "single"}, "consumer_replays": consumer_replays},
        "lines": lines,
    }


scenario(
    "normal_session",
    "One vehicle, one charging session, events in order, ends with COMPLETE.",
    ["normal session", "cumulative energy = MAX"],
    by_time(session("NRM", "VEH-0001", "S-0001-A", [0, 5, 10, 15, 20, 25, 27],
                    [0.0, 4.2, 8.5, 12.9, 17.1, 21.0, 21.6],
                    [50.0, 52.5, 51.8, 50.9, 49.7, 30.2, 0.0])),
)

scenario(
    "concurrent_sessions",
    "Interleaved sessions across vehicles; sequential sessions on one vehicle; "
    "a mid-stream inactivity timeout and an end-of-input timeout.",
    ["concurrent sessions", "inactivity timeout", "end-of-input flush"],
    by_time(
        session("CON2A", "VEH-0002", "S-0002-A", [0, 5, 10, 15, 20],
                [0.0, 5.0, 10.0, 15.0, 18.0], [60.0, 60.0, 59.0, 58.0, 0.0]),
        session("CON3A", "VEH-0003", "S-0003-A", [2, 8, 14, 20, 26, 32],
                [0.0, 1.1, 2.2, 3.3, 4.4, 5.5], [11.0] * 6, complete=False),
        session("CON2B", "VEH-0002", "S-0002-B", [40, 45, 50, 55, 60],
                [0.0, 7.5, 15.0, 22.5, 26.0], [90.0, 92.0, 91.0, 88.0, 0.0]),
        session("CON4A", "VEH-0004", "S-0004-A", [1, 11, 21, 31, 41, 51, 61, 71, 80],
                [0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0], [12.0] * 9, complete=False),
    ),
)

dup5 = [l for _, l in session("DUP", "VEH-0005", "S-0005-A", [0, 5, 10, 15, 20],
                              [0.0, 6.0, 12.0, 18.0, 22.0], [72.0, 72.0, 71.0, 70.0, 0.0])]
dup6 = [l for _, l in session("DUP", "VEH-0006", "S-0006-A", [1, 10, 20, 30, 40],
                              [0.0, 3.0, 6.0, 9.0, 12.0], [20.0] * 5, complete=False)]
scenario(
    "duplicates",
    "Exact duplicates, a same-event_id duplicate with a different payload (first wins), "
    "event_id reuse on a different session key (both accepted), a duplicate after the "
    "session closed (silently dropped), and a new event for a closed session.",
    ["duplicates", "dedupe keeps first", "dedupe is scoped to session key", "closed session"],
    [dup5[0], dup6[0], dup5[1], dup5[2], dup5[2], dup6[1], dup5[3],
     ev("EVT-DUP-003", "VEH-0005", "S-0005-A", 11, 3, "CHARGING", 25.0, 71.0, 99.9),
     dup5[4], dup6[2], dup6[3], dup6[4], dup5[1],
     ev("EVT-DUP-900", "VEH-0005", "S-0005-A", 35, 6, "IDLE", 31.0, 0.0, 22.0)],
)

ooo7 = session("OOO7", "VEH-0007", "S-0007-A", [0, 4, 8, 12, 16, 20, 24],
               [0.0, 3.0, 6.0, 9.0, 12.0, 15.0, 16.5], [45.0] * 6 + [0.0])
ooo8 = session("OOO8", "VEH-0008", "S-0008-A", [1, 6, 11, 16],
               [0.0, 4.0, 8.0, 10.0], [48.0, 120.0, 50.0, 0.0])
o7 = [l for _, l in ooo7]
o8 = [l for _, l in ooo8]
scenario(
    "out_of_order",
    "Events arrive out of event-time order within allowed lateness; cumulative energy "
    "readings arrive non-monotonically, and the peak-power reading arrives after its "
    "session's COMPLETE event, exactly at the watermark.",
    ["out-of-order", "cumulative energy = MAX", "start_ts = min event time"],
    [o7[0], o7[2], o8[0], o7[1], o7[4], o8[2], o7[3], o8[3], o8[1], o7[6], o7[5]],
)

late9 = session("LAT9", "VEH-0009", "S-0009-A", [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50],
                [0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0, 45.0, 47.0],
                [60.0] * 10 + [0.0])
l9 = [l for _, l in late9]
l10 = [l for _, l in session("LAT10", "VEH-0010", "S-0010-A", [1, 5], [0.0, 1.5], [22.0, 22.0],
                             complete=False)]
scenario(
    "late_events_window_boundary",
    "Watermark = max event time - 10 min. An event exactly at the watermark is accepted, "
    "one 1 ms before it is dropped late, a late-but-allowed event updates peak power, a "
    "session times out exactly when watermark == last_ts + 30 min, and a later event for "
    "that closed session is routed to the error path.",
    ["late events", "window boundary", "inactivity timeout boundary", "closed session"],
    [l9[0], l10[0], l9[1], l10[1], l9[2], l9[3], l9[4], l9[5], l9[6],
     ev("EVT-LAT9-L01", "VEH-0009", "S-0009-A", 21, 50, "CHARGING", 40.0, 75.0, 21.0),
     ev("EVT-LAT9-L02", "VEH-0009", "S-0009-A", 20, 51, "CHARGING", 40.0, 61.0, 20.0),
     ev("EVT-LAT9-L03", "VEH-0009", "S-0009-A", 20, 52, "CHARGING", 40.0, 99.0, 19.9, extra_ms=-1),
     l9[7], l9[8], l9[9],
     ev("EVT-LAT10-900", "VEH-0010", "S-0010-A", 36, 3, "CHARGING", 30.0, 22.0, 2.0),
     l9[10]],
)

m11 = [l for _, l in session("MAL", "VEH-0011", "S-0011-A", [0, 5, 10, 15],
                             [0.0, 3.0, 6.0, 7.5], [36.0, 36.0, 35.0, 0.0])]


def bad(n, minute=7, **override):
    rec = json.loads(ev(f"EVT-MAL-B{n:02d}", "VEH-0011", "S-0011-A", minute, 100 + n,
                        "CHARGING", 30.0, 30.0, 4.0))
    for k, v in override.items():
        if v is ...:
            rec.pop(k)
        else:
            rec[k] = v
    return json.dumps(rec, separators=(",", ":"))


scenario(
    "malformed_null",
    "Malformed JSON, non-object JSON, missing/null/invalid fields, NaN, an empty line, a "
    "VIN-like vehicle_id and an impossible date are routed to the error path; the valid "
    "events of the same session still produce a summary.",
    ["malformed", "null measurements", "error path", "identifier echo rules"],
    [m11[0],
     "this is not json",
     "[1,2,3]",
     bad(1, event_id=...),
     bad(2, energy_delivered_kwh=None),
     m11[1],
     bad(3, charging_power_kw=-5.0),
     bad(4, charging_state="CHARGNG"),
     bad(5, battery_soc=150),
     bad(6, event_timestamp="2026-01-15 08:07:00"),
     "",
     bad(7, sequence_number=3.5),
     bad(8, energy_delivered_kwh="5.0"),
     bad(9, schema_version=...),
     bad(10, vehicle_id="WVWZZZ1JZXW000001"),
     bad(11).replace('"battery_soc":30.0', '"battery_soc":NaN'),
     bad(12, event_timestamp="2026-02-30T08:07:00.000Z"),
     m11[2], m11[3]],
)

s12 = [l for _, l in session("SCH", "VEH-0012", "S-0012-A", [0, 5, 10, 15, 20, 25],
                             [0.0, 4.0, 8.0, 12.0, 16.0, 18.0], [48.0] * 5 + [0.0])]


def v11(line, **extra):
    rec = json.loads(line)
    rec["schema_version"] = "1.1"
    rec.update(extra)
    return json.dumps(rec, separators=(",", ":"))


scenario(
    "schema_evolution",
    "Minor versions 1.0, 1.1 and 1.10 are accepted and unknown additive fields are ignored; "
    "major version 2.0 is rejected; a malformed version string is invalid. "
    "synthetic_location is accepted on input but never propagated; a real-looking "
    "coordinate string is rejected.",
    ["schema evolution", "unknown fields ignored", "restricted field not propagated"],
    [s12[0],
     v11(s12[1], charger_type="DC_FAST", firmware_build="syn-1"),
     v11(s12[2], synthetic_location="SYN-GRID-0042"),
     bad(20, minute=12).replace("VEH-0011", "VEH-0012").replace("S-0011-A", "S-0012-A")
         .replace('"schema_version":"1.0"', '"schema_version":"2.0"'),
     bad(21, minute=12).replace("VEH-0011", "VEH-0012").replace("S-0011-A", "S-0012-A")
         .replace('"schema_version":"1.0"', '"schema_version":"v1"'),
     v11(s12[3], synthetic_location="52.5200,13.4050"),
     v11(s12[4], schema_version="1.10", synthetic_location="SYN-GRID-0043"),
     s12[5]],
)

scenario(
    "processor_restart",
    "The processor is stopped after line 9, checkpoints, and resumes; the concatenated "
    "output must equal an uninterrupted run.",
    ["processor restart", "checkpoint/resume"],
    by_time(
        session("RST13", "VEH-0013", "S-0013-A", [0, 6, 12, 18, 24, 30],
                [0.0, 6.0, 12.0, 18.0, 24.0, 26.0], [62.0] * 5 + [0.0]),
        session("RST14", "VEH-0014", "S-0014-A", [3, 9, 15, 21, 27, 40],
                [0.0, 2.0, 4.0, 6.0, 8.0, 9.0], [20.0] * 5 + [0.0]),
        session("RST15", "VEH-0015", "S-0015-A", [1, 2, 4],
                [0.0, 0.8, 1.2], [24.0, 24.0, 0.0]),
    ),
    run={"mode": "restart", "stop_after": 9},
)

scenario(
    "consumer_replay",
    "The summary stream is delivered to the consumer twice; consumer state must be "
    "identical to a single delivery (idempotent upsert by session key).",
    ["consumer replay", "idempotence"],
    by_time(
        session("REP16A", "VEH-0016", "S-0016-A", [0, 10, 20], [0.0, 10.0, 14.0], [62.0, 60.0, 0.0]),
        session("REP16B", "VEH-0016", "S-0016-B", [40, 50, 60], [0.0, 25.0, 31.0], [155.0, 150.0, 0.0]),
        session("REP17A", "VEH-0017", "S-0017-A", [5, 15, 25], [0.0, 3.0, 5.5], [18.0, 18.0, 0.0]),
        session("REP18A", "VEH-0018", "S-0018-A", [7, 17], [0.0, 2.5], [15.0, 15.0], complete=False),
    ),
    consumer_replays=2,
)

part = []
for i in range(6):
    veh = f"VEH-{20 + i:04d}"
    off = i * 1.5
    part.append(session(f"PRT{i}A", veh, f"S-{20 + i:04d}-A", [off, off + 6, off + 12, off + 18],
                        [0.0, 3.0 + i, 6.0 + 2 * i, 7.0 + 2 * i], [40.0 + i] * 3 + [0.0]))
    part.append(session(f"PRT{i}B", veh, f"S-{20 + i:04d}-B", [off + 35, off + 41, off + 47],
                        [0.0, 2.0 + i, 3.5 + i], [30.0 + i] * 2 + [0.0], start_seq=10))
scenario(
    "partitioning_ordering",
    "Six vehicles (as if spread over partitions) with two sequential sessions each, "
    "interleaved. Only per-key order is meaningful; cross-key order is not asserted.",
    ["partitioning", "per-key ordering", "no global ordering"],
    by_time(*part),
)

r19 = [l for _, l in session("REC19", "VEH-0019", "S-0019-A", [0, 5, 10, 15, 20, 25],
                             [0.0, 5.0, 10.0, 15.0, 20.0, 22.0], [64.0] * 5 + [0.0])]
r29 = [l for _, l in session("REC29", "VEH-0029", "S-0029-A", [1, 3],
                             [0.0, 1.0], [30.0, 0.0])]
scenario(
    "state_recovery",
    "Restart after line 8. After resuming, a duplicate of a pre-restart event, a late "
    "event and an event for a session closed before the restart must be handled exactly "
    "as in an uninterrupted run, so dedupe set, watermark and closed-session state must "
    "be part of the checkpoint.",
    ["state recovery", "checkpointed dedupe", "checkpointed watermark", "checkpointed closed sessions"],
    [r19[0], r29[0], r29[1], r19[1], r19[2], r19[3],
     ev("EVT-REC19-X01", "VEH-0019", "S-0019-A", 16, 40, "CHARGING", 30.0, 66.0, 15.5),
     r19[4],
     r19[2],
     ev("EVT-REC19-L01", "VEH-0019", "S-0019-A", 9, 41, "CHARGING", 30.0, 90.0, 9.0),
     ev("EVT-REC29-900", "VEH-0029", "S-0029-A", 12, 3, "CHARGING", 30.0, 30.0, 1.5),
     r19[5]],
    run={"mode": "restart", "stop_after": 8},
)

tenths = [round(0.1 * i, 1) for i in range(11)]
scenario(
    "numeric_precision",
    "Decimal-looking cumulative readings (0.1 steps), long mantissas, an integer literal, "
    "exponent notation, tiny values and the 150 kW high-power boundary. MAX must pass "
    "values through exactly; strict mode compares exact binary64 values.",
    ["numeric precision", "strict vs tolerance comparison", "high-power boundary"],
    by_time(
        session("NUM30", "VEH-0030", "S-0030-A", [i * 2 for i in range(11)], tenths,
                [150.0] * 10 + [0.0]),
        session("NUM31", "VEH-0031", "S-0031-A", [1, 3, 5],
                [0.0, 12.345678901234567, 12.345678901234568], [149.99999999, 1e-7, 0.0]),
    ) + [
        ev("NUM32-001", "VEH-0032", "S-0032-A", 30, 1, "CHARGING", 50.0, 7, 0),
        ev("NUM32-002", "VEH-0032", "S-0032-A", 32, 2, "CHARGING", 51.0, 7, 15),
        ev("NUM32-003", "VEH-0032", "S-0032-A", 34, 3, "COMPLETE", 52.0, 0, 1.55e1).replace("15.5", "1.55e1"),
    ],
)

dc40a = session("DWN40A", "VEH-0040", "S-0040-A", [0, 4, 8, 12, 16, 20],
                [0.0, 12.0, 24.0, 36.0, 44.0, 48.0], [180.0, 178.0, 175.0, 160.0, 120.0, 0.0])
d40a = [l for _, l in dc40a]
scenario(
    "downstream_contract",
    "A mix that exercises every consumer-state field: high-power DC, slow AC, a FAULT "
    "mid-session, an inactivity timeout, multiple sessions per vehicle and out-of-order "
    "arrival of cumulative readings.",
    ["downstream contract", "consumer state", "out-of-order cumulative readings"],
    [d40a[0], d40a[2], d40a[1], d40a[4], d40a[3], d40a[5]] + by_time(
        session("DWN40B", "VEH-0040", "S-0040-B", [60, 90, 120, 150],
                [0.0, 5.5, 11.0, 15.0], [11.0, 11.0, 11.0, 0.0]),
        session("DWN40C", "VEH-0040", "S-0040-C", [200, 210], [0.0, 3.0], [22.0, 22.0],
                complete=False),
        [(m, l.replace('"CHARGING"', '"FAULT"') if m == 35 else l) for m, l in
         session("DWN41A", "VEH-0041", "S-0041-A", [25, 30, 35, 40, 45],
                 [0.0, 6.0, 6.0, 12.0, 15.0], [70.0, 72.0, 0.0, 71.0, 0.0])],
        [(205 + i * 5, ev(f"EVT-DWN42-{i:03d}", "VEH-0042", "S-0042-A", 205 + i * 5, i,
                          "CHARGING", 40.0, 7.0, i * 0.5)) for i in range(10)],
    ),
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
        state = rm.consumer_state(summaries, sc["meta"]["consumer_replays"])
        files[exp / "consumer_state.json"] = json.dumps(state, indent=2, sort_keys=True) + "\n"
    return files


def main() -> int:
    files = render()
    if "--check" in sys.argv:
        stale = [p for p, c in files.items() if not p.exists() or p.read_text() != c]
        for p in stale:
            print(f"stale fixture: {p.relative_to(ROOT.parent)}")
        return 1 if stale else 0
    for p, c in files.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(c)
    print(f"wrote {len(files)} files for {len(SCENARIOS)} scenarios")
    return 0


if __name__ == "__main__":
    sys.exit(main())
