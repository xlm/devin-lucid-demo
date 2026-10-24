import json
from pathlib import Path

import jsonschema

HIGH_POWER_KW = 150.0


def _schema(name: str) -> dict:
    path = Path(__file__).resolve().parents[3] / "contracts" / name
    return json.loads(path.read_text())


class WarrantyLedger:
    def __init__(self, schema: dict | None = None):
        self.validator = jsonschema.Draft202012Validator(
            schema if schema is not None else _schema("charging.session.summary.v1.schema.json")
        )
        self._latest = {}
        self.accepted = 0
        self.rejected = 0
        self.reasons = []

    def apply(self, record, *, line: int = 1, delivery: int = 1) -> bool:
        if not isinstance(record, dict):
            self._reject({"line": line, "delivery": delivery, "validator": "json_parse"})
            return False

        error = next(iter(self.validator.iter_errors(record)), None)
        if error is not None:
            self._reject({
                "line": line,
                "delivery": delivery,
                "path": "/".join(str(part) for part in error.absolute_path) or None,
                "validator": error.validator,
            })
            return False

        key = (record["vehicle_id"], record["session_id"])
        self._latest[key] = record
        self.accepted += 1
        return True

    def reject_parse(self, *, line: int, delivery: int) -> None:
        self._reject({"line": line, "delivery": delivery, "validator": "json_parse"})

    def _reject(self, reason: dict) -> None:
        self.rejected += 1
        self.reasons.append(reason)

    def state(self) -> dict:
        vehicles = {}
        for (vehicle_id, _session_id), record in sorted(self._latest.items()):
            vehicle = vehicles.setdefault(vehicle_id, {
                "energy": 0.0,
                "sessions": 0,
                "high_power": 0,
                "timeouts": 0,
            })
            vehicle["energy"] += record["total_energy_kwh"]
            vehicle["sessions"] += 1
            vehicle["high_power"] += record["peak_power_kw"] >= HIGH_POWER_KW
            vehicle["timeouts"] += record["completion_reason"] == "INACTIVITY_TIMEOUT"

        return {
            "schema_version": "1.0",
            "vehicles": {
                vehicle_id: {
                    "warranty_energy_kwh": round(vehicle["energy"], 3),
                    "session_count": vehicle["sessions"],
                    "high_power_session_count": vehicle["high_power"],
                    "inactivity_timeout_count": vehicle["timeouts"],
                }
                for vehicle_id, vehicle in vehicles.items()
            },
        }
