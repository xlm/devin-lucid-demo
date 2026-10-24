# devin-lucid-demo

A self-contained demonstration of migrating a legacy Scala/Spark
vehicle-telemetry processor to Rust with behavioural verification,
downstream-consumer continuity, privacy-impact evidence and a local
shadow/cutover simulator.

All data is synthetic. Nothing here is real vehicle, location or
personal data.

This integration branch contains WP0-WP9, including privacy and evidence,
the migration playbook and workflows, and the battery-health legacy
processor registered as unmigrated. The fast replay tier is implemented.
The optional integration tier is not implemented at this revision.

## Layout

| Path | What |
|---|---|
| `docs/build-contract.md` | Frozen data model, output format, fixture conventions, ownership |
| `contracts/` | JSON Schemas for input events, session summaries, errors and consumer state |
| `fixtures/` | 13 deterministic scenarios and their expected outputs, plus the generator |
| `governance/` | Field manifests, lineage, policies and baselines |
| `verification/cli.py` | Single CLI: `verify`, `evidence`, `cutover` |
| `scripts/` | `run-fast.sh`, `run-integration.sh`, `reset-state.sh`, `start-infra.sh`, `stop-infra.sh` |

## Quickstart

Use the repository environment and sbt path first:

```sh
export PATH="$HOME/.local/sbt/bin:$PATH"
source scripts/_env.sh
```

The fast command runs replay verification and packages evidence. It does
not require Kafka or Docker:

```sh
scripts/run-fast.sh
"$PY" verification/cli.py cutover simulate
```

The simulator stops at `ReadyForReview` and prints the human approval
command. Agents must not approve. Run the verification and simulator from a
clean committed checkout so the recorded Git context matches the current
revision. The command now fails when verification or evidence packaging
fails.

Check that generated fixture files still match their source:

```sh
"$PY" fixtures/tools/generate_fixtures.py --check
```

## Documentation

- [Architecture](docs/architecture.md)
- [Battery-health processor](docs/battery-health-processor.md)
- [Build contract](docs/build-contract.md)
- [Demo states](docs/demo-states.md)
- [Evidence coverage matrix](docs/evidence-coverage-matrix.md)
- [Delivery and cutover guarantees](docs/exactly-once-semantics.md)
- [Limitations](docs/limitations.md)
- [Migration process](docs/migration-process.md)
- [Migration playbook](playbooks/migration-playbook.md)
- [Workflow examples A-F](docs/workflows/)
