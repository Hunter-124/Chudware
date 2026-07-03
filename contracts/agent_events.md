# Contract: Agent Events — `aicad.agent_events/1`

> **Frozen by T-F17 (2026-07-03).** Append-only; schema-string bump required for any
> field change or new enum value. This is the single event vocabulary shared by two
> producers (the DAG executor and the built-in agent loop) and two sinks (the
> mission-control UI live stream and the telemetry journal). Never duplicate this
> vocabulary in a parallel file.
>
> **Binding relationships:**
> - DAG-side events (`dag.*`) bind to the 8-event table in `contracts/dag_schema.md §11`.
>   Field names here MUST match exactly.
> - Thought / tool_call / artifact kinds reuse the T-H1 telemetry vocabulary
>   (`contracts/telemetry.md`) for the overlapping fields so one pipeline has two sinks.
> - Question / answer events bind to `contracts/interaction.md §7` (`ask_question` /
>   `ask_answer` kinds) — no new fields, referenced here for discoverability only.

---

## 1. Storage & transport

**Location:** `~/.local/share/AI-CAD/cache/runs/<run_id>/events.jsonl` (Linux/macOS)
or `%APPDATA%\AI-CAD\cache\runs\<run_id>\events.jsonl` (Windows).

One JSON object per line. The DAG executor writes this file via its `emit` callback
using `EventJournal` (services/dag/event_journal.py — created by T-F17). The built-in
agent loop (services/agent/loop.py) already writes to the same path pattern via
`contracts/events.md`; future versions SHOULD also emit the DAG-vocabulary events when
wrapping a DAG run.

**Replay:** The mission-control panel (`host/src/ui/mission_control_panel.cpp`) scans
`~/.local/share/AI-CAD/cache/runs/` for directories containing `events.jsonl` and
presents them as replayable runs. Loading is triggered on panel open; it re-reads the
file if the mtime changed.

**Persist-then-callback rule:** the line is written to disk (and flushed) before the
in-process UI callback fires. If the process crashes after write but before callback,
the record persists. This is the same guarantee as `contracts/telemetry.md §1.2`.

---

## 2. Envelope

Every event is a bare JSON object (no wrapper envelope). All events carry:

| Field   | Type   | Required | Description |
|---------|--------|----------|-------------|
| `kind`  | string | yes      | Event discriminator (§3) |
| `ts`    | string | yes      | ISO-8601 UTC timestamp (`2026-07-03T12:00:00Z`) |
| `run_id`| string | yes      | The run this event belongs to |

Unknown fields MUST be tolerated (skip). Unknown `kind` values MUST be tolerated (skip).

---

## 3. Event kinds

### 3.1 DAG lifecycle (bind to `contracts/dag_schema.md §11`)

| kind | Additional fields | When |
|------|-------------------|------|
| `dag.run.started` | `graph_id`, `node_count`, `graph_name` | Before first node |
| `dag.node.started` | `node_id`, `node_type`, `node_name` | Before each node |
| `dag.node.done` | `node_id`, `output_summary` (string, ≤120 chars) | After node succeeds |
| `dag.node.failed` | `node_id`, `error` | On node failure |
| `dag.node.paused` | `node_id`, `reason` (`"gate"` \| `"manual"` \| `"fail"`) | On pause |
| `dag.node.approval_required` | `node_id` | `autonomy: "approve"` gate |
| `dag.node.manual_required` | `node_id` | `autonomy: "manual"` node |
| `dag.run.done` | `status` (`"done"` \| `"failed"` \| `"paused"`), `node_states` (object) | After last node |

`node_states` in `dag.run.done` is `{node_id: {"status": NodeState}}` — a compact
summary for the panel's final-state rendering (not the full output dict).

### 3.2 Agent thought & action (T-H1 telemetry vocabulary — reused)

These are emitted by node runners that call an agent loop. They share fields with the
T-H1 `ai_artifact` / `ai_artifact_action` event kinds so a single telemetry sink can
consume both vocabularies.

| kind | Fields | When |
|------|--------|------|
| `agent.thought` | `node_id`, `text` (≤500 chars; truncated with `…` if longer), `model` | Model produced a reasoning/thinking block |
| `agent.tool_call` | `node_id`, `tool_name`, `args_digest` (SHA-256 hex of JSON args, first 16 chars), `result_digest` (SHA-256 hex of result text, first 16 chars), `latency_ms` | Tool call completed |
| `agent.artifact` | `node_id`, `artifact_id`, `kind`, `path`, `summary` (≤80 chars) | Agent produced a deliverable |
| `agent.question` | `node_id`, `batch_id`, `prompt` | Agent raised a blocking question (mirrors `ask_question`) |
| `agent.answer` | `node_id`, `batch_id`, `source` (`"user"` \| `"default"`) | Question answered |

### 3.3 Steering events (mission-control → executor)

These are NOT written to `events.jsonl` (they are inbound commands, not log entries).
They are documented here for discoverability; the actual wire format is the worker RPC
(see §4 / `contracts/worker_rpc.md` for the `dag_approve` / `dag_resume` / `dag_pause`
/ `dag_inject` methods registered by T-F17).

| Steering action | RPC method | Payload |
|----------------|------------|---------|
| Pause a running mission | `dag_pause` | `{run_id}` |
| Resume / approve a gate | `dag_approve` | `{run_id, decision: "approve"\|"reject"\|{action:"modify",output:{…}}}` |
| Inject an instruction | `dag_inject` | `{run_id, instruction, at_node_boundary: true}` |
| Change autonomy dial | `dag_set_autonomy` | `{run_id, level: "auto"\|"approve"\|"manual"}` |

These methods are implemented in `services/dag/worker_rpc.py` (T-F17).

---

## 4. Design-brief event (Elicitation gold — `contracts/telemetry.md §5.4`)

The design-brief flow produces two additional events consumed by the telemetry exporter:

| kind | Fields | When |
|------|--------|------|
| `mission.brief.proposed` | `run_id`, `batch_id`, `brief_ref` (path to aicad.req/1 doc) | Agent proposed a design brief via `ask_user` approval |
| `mission.brief.signed` | `run_id`, `batch_id`, `brief_ref`, `signed_ref` (path to final brief) | User approved / edited and signed the brief |

These ride the same `events.jsonl` stream. The telemetry exporter (T-H5) uses them to
derive elicitation-gold training pairs per `contracts/telemetry.md §5.4`.

---

## 5. Schema validation

A conforming producer MUST:
1. Write `kind`, `ts`, and `run_id` on every record.
2. Include the additional fields listed in §3 for each `kind`.
3. Flush the file (`fsync` equivalent) before firing any in-process callback.

A conforming consumer MUST:
1. Tolerate unknown `kind` values (skip the line).
2. Tolerate unknown fields on known `kind` values (ignore).
3. Never crash on a malformed line (skip and continue).

---

## 6. Example stream (36 events — capstone thin-slice + full-mission)

```jsonl
{"kind":"dag.run.started","ts":"2026-07-03T00:47:41Z","run_id":"c8efe1ae","graph_id":"thin-slice-soil-sensor","node_count":2,"graph_name":"Thin-slice: design+gate"}
{"kind":"dag.node.started","ts":"2026-07-03T00:47:41Z","run_id":"c8efe1ae","node_id":"n_design","node_type":"design","node_name":"ECAD schematic stub"}
{"kind":"dag.node.done","ts":"2026-07-03T00:47:41Z","run_id":"c8efe1ae","node_id":"n_design","output_summary":"Stub ECAD design completed"}
{"kind":"dag.node.paused","ts":"2026-07-03T00:47:41Z","run_id":"c8efe1ae","node_id":"n_gate_preorder","reason":"gate"}
{"kind":"dag.node.approval_required","ts":"2026-07-03T00:47:41Z","run_id":"c8efe1ae","node_id":"n_gate_preorder"}
{"kind":"dag.node.done","ts":"2026-07-03T00:47:41Z","run_id":"c8efe1ae","node_id":"n_gate_preorder","output_summary":"Gate approved"}
{"kind":"dag.run.done","ts":"2026-07-03T00:47:41Z","run_id":"c8efe1ae","status":"done","node_states":{"n_design":{"status":"done"},"n_gate_preorder":{"status":"done"}}}
```
