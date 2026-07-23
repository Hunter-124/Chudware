# Contract: DAG Orchestrator Schema — `aicad.dag/1`

> **Version 1.** Changes to any field, enum value, or checkpoint format require a
> schema-string bump (`aicad.dag/2`) and a migration note here.
> This file was frozen by T-A1 (2026-07-02) after B/C/D workstream review (see
> T-A1 check-in in work/FOREMAN.md). All downstream tasks (T-A2–T-A4, T-F4,
> T-F12, T-F17) consume this contract as-is.

---

## 0. Design intent

The DAG orchestrator runs a **directed acyclic graph of capability nodes** over the
Chudware engine. Each node represents one bounded capability (design, simulate, verify,
…). The executor loads a graph JSON, builds a LangGraph `StateGraph`, runs it with
SQLite-persisted checkpoints, and surfaces run state for mission-control UIs and
agent-loop integrations.

**Seams reserved for downstream tasks:**

| Seam | Reserved for | Description |
|---|---|---|
| `autonomy: "approve"` pause/resume | T-A3 | machine.h `interrupt()` / `pause` verdict wired here |
| `verify` node type | T-A4 | Full verify-node execution logic lives in T-A4 |
| `dag_replan` | T-F12 | Splice nodes into a running DAG — not built in T-A1/A2/A3 |

Do **not** implement these seams in T-A1 through T-A3 without explicit advisor sign-off.

---

## 1. Graph JSON envelope

The graph document that `executor.run()` accepts:

```json
{
  "schema": "aicad.dag/1",
  "id": "<uuid4>",
  "name": "wind-tunnel bracket",
  "nodes": [ Node, … ],
  "edges": [ Edge, … ]
}
```

- `id` — UUID4 string, assigned by the caller, persisted in the checkpoint DB as the run identifier.
- `nodes` — ordered list of `Node` objects (§2).
- `edges` — list of `Edge` objects (§4).

---

## 2. Node object

```json
{
  "id": "n1",
  "type": "design",
  "name": "model bracket",
  "inputs": { "request": "...", "doc_id": "..." },
  "tools": ["cad_guide", "add_features", "render_part"],
  "end_condition": "render_matches_request",
  "verify": null,
  "on_fail": "pause",
  "autonomy": "auto"
}
```

### 2.1 Node fields

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Opaque unique id within the graph (`"n1"`, `"n2"`, …). |
| `type` | `NodeType` | yes | Capability type — see §3. |
| `name` | string | yes | Human-readable label for UI and logs. |
| `inputs` | object | yes | Arbitrary key→value pairs passed to the node handler. Keys are node-type-specific (§3). Supports template refs like `"$n1.output.doc_id"` resolved at runtime. |
| `tools` | string[] | no | MCP tool allow-list for this node's agent call. `null`/absent = all tools available (max autonomy). Used by tool-governance (Pillar 9). |
| `end_condition` | string | no | Natural-language or structured hint for when the node considers itself done. The executor passes this to the agent system prompt. `null` = node handler decides. |
| `verify` | `VerifySpec \| null` | no | If present, the executor runs a verify sub-node after the main node. **Spec reserved for T-A4.** For now: `null` always; a non-null value is accepted but the verify logic is a no-op stub. |
| `on_fail` | `FailAction` | yes | What to do if the node fails or exceeds budget — see §5. |
| `autonomy` | `AutonomyLevel` | yes | How much human oversight this node needs — see §6. |

### 2.2 Node output

Each node handler returns an output object merged into the graph state under key `"<node_id>.output"`:

```json
{
  "status": "done",
  "doc_id": "...",
  "summary": "Bracket modelled with 6 features.",
  "data": { … }
}
```

`data` is node-type-specific and may be empty.

---

## 3. Node types (`NodeType` enum)

| Value | Description |
|---|---|
| `"plan"` | Mission planner: decomposes a natural-language request into a list of capability tasks (may produce a new sub-graph). |
| `"design"` | CAD modelling: calls the agent design loop to produce/modify a part or assembly. |
| `"ecad"` | Schematic/PCB design via the ECAD agent. |
| `"firmware"` | Firmware/embedded code generation (reserved; executor stubs). |
| `"structural"` | FEA static/modal analysis on an existing doc. |
| `"source"` | BOM sourcing: find and price parts from vendor APIs (WS-B). |
| `"simulate"` | CFD/flow simulation on an existing doc. |
| `"verify"` | Quality check: runs a verifier battery against the current doc state. **Reserved for T-A4** — executor runs stub. |
| `"gate"` | Human-checkpoint: emit a question, wait for approval or override before proceeding. Requires `autonomy: "approve"` or `"manual"`. |
| `"report"` | Synthesise results into a plain-language verdict / deliverable. |

---

## 4. Edge object

```json
{ "from": "n1", "to": "n2" }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `from` | string | yes | Source node id. |
| `to` | string | yes | Target node id. |

Conditional branching (e.g., `on_fail` routing) is expressed by the executor logic, not additional edge fields, in this version.

---

## 5. Fail action (`FailAction` enum)

| Value | Description |
|---|---|
| `"pause"` | Emit a `dag.node.failed` event, suspend the run (set run state `"paused"`), and wait for human intervention or `dag_resume` command. |
| `"abort"` | Set run state `"failed"` and stop. |
| `"skip"` | Mark the node `"failed"` but continue execution of successor nodes. |
| `"retry"` | Re-run the node up to `max_retries` (default 1) before falling through to `"pause"`. |

---

## 6. Autonomy levels (`AutonomyLevel` enum)

| Value | Description |
|---|---|
| `"auto"` | Node runs without user interaction. The executor calls the agent and advances on success. |
| `"approve"` | Node pauses **before execution** to emit a `dag.node.approval_required` event and wait for the user to confirm. **Seam for T-A3 `interrupt()`** — the actual interrupt mechanism is wired by T-A3; the executor in T-A1 stubs this as an immediate approval pass-through and emits a warning. |
| `"manual"` | Node never runs autonomously: executor emits `dag.node.manual_required`, sets run state `"paused"`, and waits for the user to supply the output directly. |

---

## 7. Run states (`RunState` enum)

| Value | Description |
|---|---|
| `"pending"` | Run created, not yet started. |
| `"running"` | At least one node is actively executing. |
| `"paused"` | Execution suspended (gate, approval, or failure). Resumes via `dag_resume`. |
| `"done"` | All non-skipped nodes completed successfully. |
| `"failed"` | Run aborted due to unrecoverable failure. |

---

## 8. Node execution states (`NodeState` enum)

| Value | Description |
|---|---|
| `"pending"` | Not yet reached by the executor. |
| `"running"` | Currently executing. |
| `"done"` | Completed successfully. |
| `"failed"` | Execution failed (see `on_fail` for disposition). |
| `"skipped"` | Skipped by `on_fail: "skip"` or gate bypass. |
| `"paused"` | Waiting for human input (approval, gate, manual). |

---

## 9. Checkpoint format

**Storage:** SQLite via `langgraph-checkpoint-sqlite` (`SqliteSaver`).

**Location:** `<base_dir>/<run_id>.db`

- **Default `base_dir`** (set by the executor): `~/.local/share/Chudware/runs/` on Linux/macOS;
  `%APPDATA%\Chudware\runs\` on Windows. Callers may override via the `runs_dir` parameter to
  `executor.run()` — required for hermetic tests.
- The directory is created on first use.

**Checkpoint contents** (managed by LangGraph, not authored here): LangGraph persists the full
`StateGraph` channel values at each node transition. The run's DAG state at any checkpoint can
be read back for resume and for the mission-control UX feed (T-F17).

**Thread model:** Each graph execution uses `thread_id = run_id` as the LangGraph thread
identifier. Multiple runs over the same graph are separate threads.

---

## 10. Run result object

`executor.run()` returns:

```json
{
  "run_id": "<uuid4>",
  "status": "done",
  "nodes": {
    "n1": { "status": "done", "output": { … } },
    "n2": { "status": "done", "output": { … } }
  },
  "checkpoint_db": "/path/to/<run_id>.db"
}
```

---

## 11. Events

The executor emits events to the optional `emit` callback (same pattern as `services/agent/loop.py`):

| Event type | Payload keys | When |
|---|---|---|
| `dag.run.started` | `run_id`, `graph_id`, `node_count` | Before first node |
| `dag.node.started` | `run_id`, `node_id`, `node_type`, `node_name` | Before each node |
| `dag.node.done` | `run_id`, `node_id`, `output` | After each node succeeds |
| `dag.node.failed` | `run_id`, `node_id`, `error` | On node failure |
| `dag.node.paused` | `run_id`, `node_id`, `reason` | On pause (gate / approve / manual) |
| `dag.node.approval_required` | `run_id`, `node_id` | `autonomy: "approve"` stub |
| `dag.node.manual_required` | `run_id`, `node_id` | `autonomy: "manual"` |
| `dag.run.done` | `run_id`, `status`, `node_states` | After last node |

---

## 12. Reserved concepts (do not implement before the noted task)

- **`dag_replan`** — splice/insert/remove nodes in a running graph. Reserved for T-F12.
  Leave the seam: `executor.run()` must not assume the node list is immutable, but
  implementing the mutation API is out of scope here.
- **`interrupt()`** — machine.h pause verdict for `autonomy: "approve"` nodes. Reserved for T-A3.
- **Full verify-node logic** — the `"verify"` node type stub is all T-A1 delivers. T-A4 owns
  the verifier battery and result schema.

---

## 13. Schema validation

A conforming producer MUST:
1. Include `"schema": "aicad.dag/1"` at the top level.
2. Provide `"id"` (UUID4), `"nodes"` (non-empty array), `"edges"` (array, may be empty).
3. Each node must have `id`, `type` (from §3), `name`, `inputs` (object), `on_fail` (from §5), `autonomy` (from §6).

A conforming consumer MUST reject documents where `schema` is absent or not `"aicad.dag/1"`.

Python schema validation lives in `services/dag/graph_loader.py` (`validate_graph()`).

---

## 14. Narration view (additive, T-F8 — merged 2026-07-02)

`describe_project(run=...)` (services/mcp/gsl_tools.py) additionally consumes these
OPTIONAL run-dict fields; executors SHOULD populate them (narration degrades gracefully
via `.get()` when absent — this appendix is additive and does not bump the schema):

- `project` — human project name string
- `budget` — `{"tokens_used": int, "tokens_cap": int}` (aliases `spent`/`cap` accepted)
- `open_questions` — `[{"node": id, "question"|"text": str, "blocked_reason"?: str}]`

T-A2's node runner / executor is the natural producer (recorded in its Driver notes).
