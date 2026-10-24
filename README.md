# devin-lucid-demo

A self-contained demonstration of migrating a legacy Scala/Spark
vehicle-telemetry processor to Rust with behavioural verification,
downstream-consumer continuity and privacy-impact evidence.

All data is synthetic. Nothing here is real vehicle, location or
personal data.

> Status: WP0 (shared contract) only. Processors, gates, simulator and
> workflow docs land in later work packages; see
> [docs/build-contract.md](docs/build-contract.md).

## Layout

| Path | What |
|---|---|
| `docs/build-contract.md` | Frozen data model, output format, fixture conventions, ownership |
| `contracts/` | JSON Schemas for input events, session summaries, errors and consumer state |
| `fixtures/` | 13 deterministic scenarios and their expected outputs, plus the generator |
| `governance/` | Data classifications, field manifests, lineage, policies, baselines |
| `verification/cli.py` | Single CLI: `verify`, `evidence`, `cutover` |
| `scripts/` | `run-fast.sh`, `run-integration.sh`, `reset-state.sh`, `start-infra.sh`, `stop-infra.sh` |

## Fast tier

No Kafka or Docker needed:

```sh
scripts/run-fast.sh            # all scenarios, all gates, evidence
scripts/run-fast.sh --strict   # exact numeric comparison
```

Fixture integrity check:

```sh
python3 fixtures/tools/generate_fixtures.py --check
```
