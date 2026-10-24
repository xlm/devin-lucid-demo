import argparse
import json
import sys
from pathlib import Path

import jsonschema

from .ledger import WarrantyLedger, _schema


def _reject_constant(token: str):
    raise ValueError(token)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m warranty_consumer")
    parser.add_argument("--input", required=True)
    parser.add_argument("--replays", type=int, default=1)
    parser.add_argument("--state-out", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.replays < 1:
        parser.error("--replays must be at least 1")

    input_path = Path(args.input)
    try:
        lines = input_path.read_text().splitlines()
    except OSError as exc:
        print(f"cannot read input file: {exc}", file=sys.stderr)
        return 1

    ledger = WarrantyLedger()
    for delivery in range(1, args.replays + 1):
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line, parse_constant=_reject_constant)
            except (json.JSONDecodeError, ValueError):
                ledger.reject_parse(line=line_number, delivery=delivery)
                continue
            if not isinstance(record, dict):
                ledger.reject_parse(line=line_number, delivery=delivery)
                continue
            ledger.apply(record, line=line_number, delivery=delivery)

    state = ledger.state()
    state_schema = _schema("warranty.consumer-state.v1.schema.json")
    try:
        jsonschema.Draft202012Validator(state_schema).validate(state)
    except jsonschema.ValidationError as exc:
        print(f"generated state failed schema validation: {exc.validator}", file=sys.stderr)
        return 1

    output_path = Path(args.state_out)
    rejects_path = Path(str(output_path) + ".rejects.json")
    rejects = {
        "accepted": ledger.accepted,
        "rejected": ledger.rejected,
        "replays": args.replays,
        "reasons": ledger.reasons,
    }
    try:
        state_text = json.dumps(state, sort_keys=True, indent=2, allow_nan=False) + "\n"
        rejects_text = json.dumps(rejects, sort_keys=True, indent=2, allow_nan=False) + "\n"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(state_text)
        rejects_path.parent.mkdir(parents=True, exist_ok=True)
        rejects_path.write_text(rejects_text)
    except (OSError, ValueError) as exc:
        print(f"cannot write output files: {exc}", file=sys.stderr)
        return 1

    print(
        f"accepted={ledger.accepted} rejected={ledger.rejected} replays={args.replays}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
