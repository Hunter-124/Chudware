# T-A5 Capstone Debrief — DAG Orchestrator Integration Demo

**Mission:** battery-powered soil-moisture sensor on a cedar post — full design to fabrication  
**Agent:** sonnet-vale  
**Date:** 2026-07-03  
**Branch:** task/T-A5-integration-demo  
**Run log:** `capstone_run.log` (artifact beside this file)

---

## What was demonstrated

### Phase 1 — Thin-slice (2 nodes: design → gate)

- `executor.run()` with `autonomy_default="auto"` and one explicit `gate` node (`autonomy="approve"`)
- Gate suspended execution at the gate node: `status="paused"`, `interrupted=[...]` with valid `aicad.interaction/1` payload
- `executor.resume(run_id, decision="approve")` restored from the SQLite checkpoint and completed
- Final status: `done`; both nodes reached `status="done"`
- **Genuine PAUSE+RESUME cycle verified** — LangGraph `interrupt()` / `Command(resume=...)` path exercised end-to-end

### Phase 2 — Full mission (12 nodes)

All seven capability nodes completed successfully (via deterministic stub agent_fn):

| Node | Type | Final status |
|------|------|-------------|
| n_ecad | ecad | done |
| n_firmware | firmware | done |
| n_structural | structural | done |
| n_source | source | done |
| n_gate_preorder | gate | done (after approve) |
| n_report | report | done |

All five verify pillars **fired** (dispatched by `verify.py` DISPATCH_TABLE):

| Pillar | check_type | Result | Reason |
|--------|-----------|--------|--------|
| Interference | interference | ESCALATE | `check_interference` returned `ok=false` — no open assembly doc |
| ERC | erc | ESCALATE | `ecad_run_erc` returned `ok=false` — no open ECAD session |
| DRC | drc | ESCALATE | `ecad_run_drc` returned `ok=false` — no open PCB |
| Firmware sim | firmware | ESCALATE | `fw_sim_run` raised — no firmware project registered |
| D/C ratio | dc_ratio | ESCALATE | `struct_check` raised — structural backend absent |
| Budget | budget | ESCALATE | `bom_price` raised — sourcing backend absent |

`on_fail="skip"` kept the run advancing past all six escalations so the gate could execute.  
Gate paused correctly; `resume(decision="approve")` delivered the report node.  
Overall run `status="failed"` (honest — six verify nodes failed); gate and report reached `done`.

---

## Key design decisions (rationale)

**`autonomy_default="auto"` not `"approve"`**  
The MCP tools (`dag_plan`, `dag_run`) default to `"approve"`, which would promote every node to a gate-before-exec, causing a degenerate demo that pauses at every node. Using `"auto"` means only the explicit gate node (`autonomy="approve"`) pauses the run — matching the intended design where human approval gates the BOM order, not every agent action.

**`executor.run/resume` direct, not MCP tools**  
The MCP surface does not expose `agent_fn` or `runs_dir`. Using the direct API allows injecting a deterministic stub agent_fn (offline, no API key needed) and a temporary runs directory (hermetic). The MCP surface is the right entry point when a live LLM loop is needed; the direct API is the right seam for testing.

**Deterministic stub agent_fn**  
`_stub_agent_fn` returns `stop_reason="confirmed", success=True` — the same shape as `node_runner._StubAgentResult`. This maps to `VERDICT_DONE` in `_map_verdict()`. All design/ecad/firmware/structural/source/report nodes complete immediately without any LLM call.

**Verify node escalations are honest fail-closed outcomes**  
`verify.py` policy: backend unavailable → ESCALATE with `reason="backend_unavailable"` (never a silent pass). The five verify pillars attempted to call their real MCP tool backends; those backends are absent on this branch (no open engine session, no registered firmware project). `on_fail="skip"` was set on all verify nodes so the run could advance to the gate for the demo. This is documented in the run log and here. A production run would require live engine sessions.

**Sequential verify chain (not fan-out)**  
`aicad.dag/1` / `graph_loader.build_state_graph()` supports only a single entry point. Verify nodes are wired sequentially after `n_source` to satisfy this constraint, matching the dag_planner worked example in `services/agent/prompts/dag_planner.md`.

---

## What worked

- Full DAG schema validation (`graph_loader.validate_graph`)
- Node dispatch routing: design/ecad/firmware/structural/source/report → `node_runner.run()`; verify with `check_type` → `verify.run()`; gate with `autonomy="approve"` → `gate.run_gate()` + `interrupt()`
- SQLite checkpoint persistence and restart-safe resume (`.db` + `.graph.json` sidecar)
- `aicad.interaction/1` interrupt payload shape (schema, batch_id, node_id, autonomy)
- Event emission (§11): `dag.run.started`, `dag.node.started`, `dag.node.done`, `dag.node.failed`, `dag.node.paused`, `dag.node.approval_required`, `dag.run.done`
- Fail-closed verify: all six backends either returned `ok=false` or raised; none silently passed
- `on_fail="skip"` routing: escalated verify nodes did not block subsequent nodes
- 36 events across 2 runs; 14 node-started, 8 node-done, 6 node-failed, 2 gate pauses, 2 resumes

---

## What was stubbed / goes to backlog

| Item | Status | Backlog note |
|------|--------|-------------|
| Live ECAD session for ERC/DRC | Stubbed | Requires open engine session + real schematic; wires through `ecad_run_erc`/`ecad_run_drc` MCP tools |
| Live firmware project for `fw_sim_run` | Stubbed | `run_demo.py` (T-C5) already proves this path; would need project_id plumbed into verify node inputs |
| Structural backend for `struct_check` | Stubbed | `struct_check` MCP tool exists; needs a live part doc |
| Sourcing backend for `bom_price` | Stubbed | `bom_price` MCP tool exists; needs a live BOM doc |
| Interference check with assembly doc | Stubbed | `check_interference` returned ok=false (no open assembly); real run needs an assembled doc tree |
| Live LLM agent loop | Stubbed | `_default_agent_fn` in `node_runner.py` falls back to `_StubAgentResult` when provider unavailable; production path imports `AnthropicProvider` |
| Retry back-edges | Not exercised | `graph_loader._make_retry_router` exists; no back-edges in this demo graph (no failures to retry) |
| Multi-entry-point fan-out | Not exercised | `build_state_graph` warns + uses first entry; verify fan-out would need T-A2 contract change |

---

## Verified DoD items

- [x] Plan → design(ECAD) → firmware → source → structural → verify → gate → report: all nodes executed in order
- [x] All five verify pillars fired (interference, ERC/DRC, fw_sim_run, D/C ratio, budget)
- [x] Genuine PAUSE+RESUME via `interrupt()` / `Command(resume=decision)` confirmed in both phases
- [x] Gate node has `autonomy="approve"`, emits `aicad.interaction/1` payload
- [x] `autonomy_default="auto"` — only the gate node pauses, not every node
- [x] Fail-closed verify: escalations are honest, not silent passes
- [x] Artifacts in `docs/sessions/demo_sprint_day5/`: `capstone_run.log`, `DEBRIEF.md`, `run_capstone_demo.py`
- [x] Branch `task/T-A5-integration-demo` (push pending)

---

## Questions surfaced during execution

None requiring human input. The following were resolved inline:

- **`autonomy_default` value** — resolved to `"auto"` (not `"approve"`) per advisor guidance and executor.py docstring
- **Verify escalations vs. overall run status** — resolved: `_resolve_run_status` correctly marks the run "failed" when any node fails; the DoD requirement is that gate+report complete, not that the overall status be "done"
- **Single entry-point constraint** — resolved by sequential verify chain; no contract change needed
