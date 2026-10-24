import json
import random
from pathlib import Path

import pytest

from warranty_consumer import HIGH_POWER_KW, WarrantyLedger

ROOT = Path(__file__).resolve().parents[2]


def record(session_id="S-0001", **overrides):
    value = {
        "session_id": session_id,
        "vehicle_id": "VEH-0001",
        "start_ts": "2026-01-15T08:00:00.000Z",
        "end_ts": "2026-01-15T08:01:00.000Z",
        "total_energy_kwh": 1.0,
        "peak_power_kw": 149.0,
        "event_count": 1,
        "completion_reason": "COMPLETE",
        "schema_version": "1.0",
    }
    return {**value, **overrides}


def test_upsert_replaces_earlier_record_for_same_key():
    ledger = WarrantyLedger()

    assert ledger.apply(record(total_energy_kwh=1.0))
    assert ledger.apply(record(total_energy_kwh=2.0))

    assert ledger.state()["vehicles"]["VEH-0001"]["warranty_energy_kwh"] == 2.0
    assert ledger.state()["vehicles"]["VEH-0001"]["session_count"] == 1


@pytest.mark.parametrize("replays", [1, 2, 3])
def test_replays_produce_identical_state(replays):
    ledger = WarrantyLedger()
    summaries = [record(total_energy_kwh=0.1), record("S-0002", total_energy_kwh=0.2)]

    for delivery in range(1, replays + 1):
        for summary in summaries:
            ledger.apply(summary, delivery=delivery)

    assert ledger.state()["vehicles"]["VEH-0001"]["session_count"] == 2
    assert ledger.accepted == 2 * replays


@pytest.mark.parametrize("mutate", [
    lambda item: item.pop("event_count"),
    lambda item: item.update(unexpected="value"),
    lambda item: item.update(total_energy_kwh=True),
    lambda item: item.update(total_energy_kwh="1.0"),
    lambda item: item.update(vehicle_id="WVW-SYNTHETIC-0001"),
    lambda item: item.update(event_count=0),
])
def test_invalid_records_are_counted_and_not_applied(mutate):
    bad = record()
    mutate(bad)
    ledger = WarrantyLedger()

    assert not ledger.apply(bad, line=7, delivery=2)

    assert ledger.accepted == 0
    assert ledger.rejected == 1
    assert ledger.state() == {"schema_version": "1.0", "vehicles": {}}
    assert ledger.reasons[0]["line"] == 7
    assert ledger.reasons[0]["delivery"] == 2
    assert "WVW-SYNTHETIC-0001" not in json.dumps(ledger.reasons)


@pytest.mark.parametrize("field", ["total_energy_kwh", "peak_power_kw"])
def test_non_finite_summary_numbers_are_rejected(field):
    ledger = WarrantyLedger()

    assert not ledger.apply(record(**{field: float("inf")}), line=4, delivery=2)

    assert ledger.accepted == 0
    assert ledger.rejected == 1
    assert ledger.state() == {"schema_version": "1.0", "vehicles": {}}
    assert ledger.reasons == [{
        "line": 4,
        "delivery": 2,
        "path": field,
        "validator": "finite",
    }]


def test_non_object_record_is_rejected_as_parse_error():
    ledger = WarrantyLedger()

    assert not ledger.apply([], line=3, delivery=1)

    assert ledger.reasons == [{"line": 3, "delivery": 1, "validator": "json_parse"}]


def test_high_power_boundary_and_inactivity_count():
    ledger = WarrantyLedger()
    assert ledger.apply(record("S-0001", peak_power_kw=HIGH_POWER_KW))
    assert ledger.apply(record(
        "S-0002",
        peak_power_kw=149.999,
        completion_reason="INACTIVITY_TIMEOUT",
    ))

    assert ledger.state()["vehicles"]["VEH-0001"] == {
        "warranty_energy_kwh": 2.0,
        "session_count": 2,
        "high_power_session_count": 1,
        "inactivity_timeout_count": 1,
    }


def test_energy_is_rounded_to_three_decimal_places():
    ledger = WarrantyLedger()
    ledger.apply(record("S-0001", total_energy_kwh=0.1))
    ledger.apply(record("S-0002", total_energy_kwh=0.2))

    assert ledger.state()["vehicles"]["VEH-0001"]["warranty_energy_kwh"] == 0.3


def test_state_is_independent_of_arrival_order_across_sessions():
    summaries = [
        record("S-0003", total_energy_kwh=3.0),
        record("S-0001", total_energy_kwh=1.0),
        record("S-0002", total_energy_kwh=2.0),
    ]
    shuffled = summaries[:]
    random.Random(3).shuffle(shuffled)
    states = []
    for ordered in (summaries, shuffled):
        ledger = WarrantyLedger()
        for summary in ordered:
            ledger.apply(summary)
        states.append(ledger.state())

    assert states[0] == states[1]


@pytest.mark.parametrize("scenario", sorted(path.name for path in (ROOT / "fixtures" / "expected").iterdir()))
def test_library_state_matches_each_fixture(scenario):
    summaries = [
        json.loads(line)
        for line in (ROOT / "fixtures" / "expected" / scenario / "summaries.jsonl")
        .read_text().splitlines()
        if line.strip()
    ]
    ledger = WarrantyLedger()
    for summary in summaries:
        ledger.apply(summary)

    expected = json.loads(
        (ROOT / "fixtures" / "expected" / scenario / "consumer_state.json").read_text()
    )
    assert ledger.state() == expected
