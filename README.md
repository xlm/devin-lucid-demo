# devin-lucid-demo

A self-contained demonstration of migrating a legacy Scala/Spark
vehicle-telemetry processor to Rust with behavioural verification,
downstream-consumer continuity, privacy-impact evidence and a local
shadow/cutover simulator.

All data is synthetic. Nothing here is real vehicle, location or
personal data.

This integration branch contains WP0-WP4 and WP6. WP5 governance,
privacy and evidence changes are expected from
`origin/wp5-governance-privacy` and have not been merged. The optional
integration tier is not implemented at this revision.

## Layout

| Path | What |
|---|---|
| `docs/build-contract.md` | Frozen data model, output format, fixture conventions, ownership |
| `contracts/` | JSON Schemas for input events, session summaries, errors and consumer state |
| `fixtures/` | 13 deterministic scenarios and their expected outputs, plus the generator |
| `governance/` | Field manifests, lineage, policies and baselines, after WP5 lands |
| `verification/cli.py` | Single CLI: `verify`, `evidence`, `cutover` |
| `scripts/` | `run-fast.sh`, `run-integration.sh`, `reset-state.sh`, `start-infra.sh`, `stop-infra.sh` |

## Quickstart

Use the repository environment and sbt path first:

```sh
export PATH="$HOME/.local/sbt/bin:$PATH"
source scripts/_env.sh
```

After WP5 lands, the fast command runs replay verification and attempts to
package evidence. It does not require Kafka or Docker:

```sh
scripts/run-fast.sh
"$PY" verification/cli.py cutover simulate
```

The simulator stops at `ReadyForReview` and prints the human approval
command. Agents must not approve. Run the verification and simulator from a
clean committed checkout so the recorded Git context matches the current
revision. `run-fast.sh` currently ignores an evidence-command failure, so
confirm the evidence files exist before review.

Check that generated fixture files still match their source:

```sh
"$PY" fixtures/tools/generate_fixtures.py --check
```

## Documentation

- [Architecture](docs/architecture.md)
- [Build contract](docs/build-contract.md)
- [Evidence coverage matrix](docs/evidence-coverage-matrix.md)
- [Delivery and cutover guarantees](docs/exactly-once-semantics.md)
- [Limitations](docs/limitations.md)
- [Migration process](docs/migration-process.md)
