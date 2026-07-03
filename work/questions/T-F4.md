# T-F4 Questions

## Q1: Doc-scoped ledger for intake — where should IntakeLedger live?

**Context:** `contracts/interaction.md §5` specifies that `scope=doc` answers persist to
the document's intake ledger (`IntakeState`, `engine_oracle/intake/intake.py`).
`services/agent/interaction.py` (the reference impl, T-F3) only has `FileLedger`
(run-scoped) and `MemoryLedger` — no doc-scoped store.

The task work order says "shared ledger: a dimension confirmed via intake is visible to a
DAG gate and never re-asked." The intake tools use `scope=doc` (the `.step+` doc record);
DAG gates use `scope=run` (the run JSONL). These are DIFFERENT scope namespaces.

**Q:** For the cross-mechanism no-re-ask test (intake dim visible to gate), should we:

(a) Use the SAME `run_id` scope for both (`FileLedger`) — meaning intake confirmations
    must also be written to the run ledger alongside the doc ledger. This is simplest,
    testable without libaicad.so, but slightly diverges from the contract §5 "doc-scoped"
    language.

(b) Add an `IntakeLedger` adapter in `services/mcp/` that wraps `MemoryLedger` in tests
    and the real doc store in production. This is contract-faithful but adds complexity
    and requires libaicad.so for real integration.

(c) The test should use `run_id` as the shared key for BOTH mechanisms (both record to
    the same `FileLedger`/`MemoryLedger` in the test) — that's the real scenario (the MCP
    session has a run_id, and the DAG gate checks the SAME run's ledger). Intake calls
    `ask_user(..., scope="run", run_id=run_id)` and so does the gate.

**Recommended answer:** (c). The cross-mechanism test is: call `ask_user` from the intake
path with `scope="run", run_id=<X>`, then show the gate for the same run already has the
answer in its ledger (no second transport call). This is what contracts/interaction.md §5
describes: "answered for this run ⇒ resume". The doc-scope path is orthogonal and can be
wired to `IntakeLedger` later (post-T-F4). No edit to `interaction.py` needed for
this approach.

**Not blocking:** proceeding with (c) unless advisor says otherwise.
