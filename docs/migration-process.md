# Migration process

This workflow uses synthetic fixtures only. The full privacy and evidence
steps depend on WP5 landing. Start each command sequence from the repository
root with:

```sh
export PATH="$HOME/.local/sbt/bin:$PATH"
source scripts/_env.sh
```

| Step | Actor | Command | Artifacts and outcome |
|---|---|---|---|
| Understand legacy | Devin | `legacy-scala/bin/check-fixtures` | Prints scenario-by-scenario results and `13/13 scenarios match fixtures/expected`. Temporary replay outputs are removed. `fixtures/scenarios/` and `fixtures/expected/` remain the source and reference data. |
| Scaffold candidate | Devin, with human code review | No repository scaffold command exists. Build the current registered Rust candidate with `cargo build --release --quiet --manifest-path rust-processor/Cargo.toml`. | The processor source, `processors.yaml` registration and `rust-processor/target/release/charging-processor`. A future processor requires an explicit implementation and registry entry. |
| Implement and unit-check | Devin | `cargo test --quiet --manifest-path rust-processor/Cargo.toml` | Rust unit and integration test results. Replay and checkpoint tests use temporary data. |
| Shadow | Devin | `"$PY" verification/cli.py cutover init --state-dir build-output/docs-cutover-shadow`<br>`"$PY" verification/cli.py cutover start-candidate --state-dir build-output/docs-cutover-shadow`<br>`"$PY" verification/cli.py cutover shadow --state-dir build-output/docs-cutover-shadow` | `build-output/docs-cutover-shadow/state.json`, `lease.json`, `audit.jsonl`, and `shadow/<scenario>/` outputs. Shadow input hashes are recorded. Legacy retains the lease and shadow output does not append to `topic.jsonl`. |
| Run gates | Devin | `"$PY" verification/cli.py verify --baseline expected --skip-build --gates behavioural contract state_ordering --run-dir build-output/docs-checks`<br><br>Full fast run: `scripts/run-fast.sh` (after WP5 lands). | The checked command writes `context.json`, replay records, JSONL outputs and the behavioral, contract and state-ordering gate JSON. The full run adds the privacy gate and attempts evidence packaging. |
| Package evidence | Devin | `"$PY" verification/cli.py evidence --run-dir build-output/latest` (after WP5 lands) | `build-output/latest/evidence/evidence.json`, `evidence.md`, `evidence.html` and `evidence.sha256`. Confirm these files exist because `run-fast.sh` currently ignores an evidence-command failure. |
| Human review | Devin prepares, human decides | `"$PY" verification/cli.py --run-dir build-output/latest cutover evaluate --state-dir build-output/docs-cutover-shadow` (after WP5 lands). Human-only approval template: `"$PY" verification/cli.py --run-dir build-output/latest cutover approve --state-dir build-output/docs-cutover-shadow --approver '<name>' --evidence-sha256 '<evidence-json-sha256>'` (after WP5 lands, human only, do not run by agents). | Evaluation records gate hashes and evidence hash, then stops at `ReadyForReview` or `ValidationFailed`. Only a human in a TTY may run the approval template after reviewing the evidence and entering `APPROVE <first 12 hex of evidence hash>`. Agents must not run approval. |
| Cut over | Human operator, after approval | `"$PY" verification/cli.py cutover cutover --state-dir build-output/docs-cutover-shadow` (after WP5 lands and human approval, not run by agents) | Lease handover audit entries and candidate-active state. A gap between producers remains possible. |
| Roll back | Human operator | `"$PY" verification/cli.py cutover rollback --state-dir build-output/docs-cutover-shadow` then `"$PY" verification/cli.py cutover restore --state-dir build-output/docs-cutover-shadow` (after cutover, not run here) | The lease returns to legacy with an incremented fencing token. State, lease and audit records remain in the state directory. |

The compact walkthrough is `"$PY" verification/cli.py cutover simulate`
(after WP5 lands and a clean verification run has produced the current
gates). It runs shadow and evaluation, prints the approval command, and
stops without approving. The simulator uses a single-host file lock. It is
not a distributed lease or atomic handover, and the consumer upsert only
mitigates identical duplicate summaries.
