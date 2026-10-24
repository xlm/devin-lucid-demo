# Playbook: migrate a streaming processor from Scala/Spark to Rust

Reusable Devin Playbook for this repository. Paste it into a Devin
Playbook, or reference it from a session prompt. It applies to any
processor registered in `processors.yaml`. Workflows A-F in
`docs/workflows/` are worked examples of each phase.

## Roles

- **Devin** reads code, writes the candidate, runs gates, packages
  evidence and explains findings. Devin is an engineering assistant. It
  never approves cutover, release or privacy compliance.
- **Human reviewer** reads the evidence package, decides, and is the only
  actor who runs `cutover approve`, `cutover cutover`, or edits
  `governance/policies/`, `governance/baselines/`, `fixtures/expected/`
  or `verification/tests/`.

## Procedure

1. **Reset and orient.** `scripts/reset-state.sh wf-a` if a demo tag is
   given,
   then `export PATH="$HOME/.local/sbt/bin:$PATH"; source scripts/_env.sh`.
   Read `docs/build-contract.md`, `processors.yaml`, the processor's
   field manifest and lineage under `governance/`.
2. **Understand the legacy job.** Read the Scala sources and tests. Write
   down the per-key state, dedupe rule, window and timeout rules, late
   data handling, error paths, and every aggregate's exact semantics
   (cumulative versus delta, per-event versus per-window). Confirm with
   `legacy-scala/bin/check-fixtures` (or the processor's own self-check).
   Cite file and line for each rule.
3. **Implement the candidate** on the established Rust skeleton
   (`rust-processor/`): keep `main.rs`, `config.rs`, `kafka.rs`,
   `state.rs`, `model.rs`, add or finish one module under
   `src/processors/`. Replay mode must be deterministic: no wall clock,
   no random seeds, canonical JSONL (contract section 8). Logs go to
   stderr without `vehicle_id`, `session_id` or `synthetic_location`.
4. **Unit-check.** `cargo fmt --check`, `cargo clippy -- -D warnings`,
   `cargo test` in `rust-processor/`.
5. **Run all gates.** `scripts/run-fast.sh`. Read every non-pass check in
   `build-output/latest/gates/*.json`, not only the summary line.
   Numeric differences: re-run with `--strict` to see last-ULP drift,
   and explain any accepted drift.
6. **Fix the candidate, never the oracle.** On a failure, change the
   candidate. Do not edit expected fixtures, gates, tests, policies or
   baselines to make a check pass. If you believe the oracle is wrong,
   stop and ask the human, with evidence.
7. **Privacy review evidence.** Update the candidate's
   `processing-inventory.yaml` to describe what the code actually does.
   Read `gates/privacy.json`: the recommendation, each finding, and its
   `verification` basis (runtime, declared, source scan, hash). Explain
   which claims are runtime-inspected and which are declaration-only.
8. **Package evidence.** `"$PY" verification/cli.py evidence` writes
   `build-output/latest/evidence/` (JSON, Markdown, HTML, sha256),
   sections A-F. Attach it for the reviewer.
9. **Shadow and evaluate.** `"$PY" verification/cli.py cutover simulate
   --state-dir .cutover` runs init, start-candidate, shadow and evaluate,
   then prints the approval command and stops at `ReadyForReview`.
10. **Hand over to the human.** Report: what changed, gate results with
    check ids, privacy recommendation, evidence sha256, and limitations.
    Do not run `cutover approve`. It requires a TTY and a typed phrase
    by design.
11. **Rollback rehearsal (human-run, optional).** After a human approval
    and cutover: `"$PY" verification/cli.py cutover rollback --state-dir
    .cutover` then `"$PY" verification/cli.py cutover restore --state-dir
    .cutover`, and confirm the fencing token increased and consumer state
    is unchanged.

## Guarantee boundaries to state in every report

- Passing gates show equivalence on the committed synthetic fixtures,
  per key. They do not prove production safety.
- Ordering is compared within a key only. Global ordering is never
  claimed.
- The local lease is a single-host demo mechanism. Cutover is not atomic
  and not exactly-once.
- The privacy recommendation is input to a human review, not a legal or
  compliance determination.

## Forbidden

- Editing `fixtures/expected/`, `verification/` gates or tests,
  `governance/policies/`, `governance/baselines/`, `contracts/` to force
  a pass.
- Running `cutover approve` or `cutover cutover`.
- Real location data or PII. Fixtures are synthetic.
