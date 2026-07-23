# Verify: agent-events contract + host RPC transport (OMP integration seam)

## Headline corrections vs. the plan
1. **OMP already streams real thinking/text/tool-call deltas** (`thinking_delta`, `text_delta`,
   `toolcall_delta` inside `message_update.assistantMessageEvent`, `docs/provider-streaming-internals.md:20-23`,
   `docs/rpc.md:623-625`). CAD's `agent.thought.delta` is reserved specifically because CAD's own
   `Provider.complete`/`complete_ex` seam is non-streaming (`contracts/agent_events.md:121`). This means
   a translation layer has a **producer surface CAD never had**: if OMP is wired in via `--mode rpc`,
   the translator is the FIRST real `agent.thought.delta` emitter, and the CAD GUI-side consumer code for
   coalescing deltas by `parent_id` (referenced in the contract, "consumers coalesce deltas sharing a
   `parent_id`") must be verified to actually exist in `ai_workspace.cpp` — **it does NOT yet**
   (`feedAgentDagEvent` in ai_workspace.cpp:184-271 has no `agent.thought.delta` branch, only
   `agent.thought` (completed-block) at line 222). **CORRECTED**: the plan should not assume
   `agent.thought.delta` is purely aspirational infrastructure — it's schema-frozen and documented, but
   there is currently zero GUI-side rendering code for it. A translator emitting it would be invisible to
   the DAG tree view until `feedAgentDagEvent` gains an `agent.thought.delta` branch (append-only, no
   schema bump needed per §5 conformance rules).
2. **Transport shapes are structurally different, not just cosmetically.** CAD's worker RPC
   (`contracts/worker_rpc.md:1-8`) is a **synchronous call/event/result envelope**: `{"id","method","params"}`
   → zero-or-more `{"id","event","data"}` → terminal `{"id","ok","result"}`/`{"id","ok":false,"error"}`.
   OMP's `--mode rpc` (`docs/rpc.md:32-57`) is **not call-scoped at all** for agent events: `AgentSessionEvent`
   frames are emitted directly on stdout with no `id` correlation to any inbound command — only
   `RpcResponse` frames echo `id`. A translator therefore cannot simply "map `event.data`→CAD event
   payload" — it must synthesize the `id`/`run_id`/span linkage CAD requires (§2.1) from session/turn
   state that OMP does not expose per-event. **CORRECTED**: treat this as an impedance mismatch requiring
   the translator to own span-id minting (`new_span_id()` in `services/agent/agent_events.py:35-37`), not
   just field renaming.

## 1. `aicad.agent_events/2` schema (services/agent/agent_events.py + contracts/agent_events.md) — CONFIRMED
- File: `services/agent/agent_events.py` is the single builder module for `/2` agent-level events
  (`agent.tool_call`, `agent.thought`, `agent.context_injected`, `agent.spawn`, `agent.advisor`),
  confirmed at lines 1-19.
- Envelope (bare JSON object, no wrapper): `kind`, `ts`, `run_id` required on every line
  (`contracts/agent_events.md:62-70`).
- §2.1 parent linkage (agent-level events only): `id` (span id, REQUIRED, tree key — NOT `node_id`),
  `parent_id` (optional, join target), `actor` (optional), `seq` (optional, advisory-only) —
  `contracts/agent_events.md:79-89`. Implemented by `_linkage()` in agent_events.py:141-147, which stamps
  `node_id`, `id`, `seq`, and conditionally `parent_id`/`actor`.
- Event vocabulary (confirmed against both contracts/agent_events.md §3 and the live builder module):
  - `agent.thought` — `node_id, text(≤500,…), model` — completed reasoning block (agent_events.py has no
    dedicated builder shown in the first 300 lines but `contracts/agent_events.md:120` documents it and
    `ai_workspace.cpp:222-228` consumes it).
  - `agent.thought.delta` (`/2`, forward-compat, **reserved, no producer**) — `contracts/agent_events.md:121`.
  - `agent.tool_call` — built by `tool_call_event()` (agent_events.py:150-186): `node_id, tool_name,
    target (derived via derive_target()), args_digest, result_digest, ok, latency_ms`, optional additive
    `args_preview` (≤2048 chars) and `artifacts` (≤8, `{path,kind∈image|mesh|file}` via `derive_artifacts()`).
  - `agent.artifact`, `agent.question`, `agent.answer` — documented §3.2, no local builder seen in the
    first 300 lines of agent_events.py (may live elsewhere; not required for this verification's scope).
  - `agent.context_injected`, `agent.spawn`, `agent.advisor` — `/2` net-new, contract §3.2a (lines 133-144).
  - `agent.classification` — builder at agent_events.py:293-352, emitted once at request entry
    (`classification_event()`), per contract's 2026-07-17 addendum (line 235).
  - DAG lifecycle `dag.*` (8-event table, contracts/agent_events.md:99-106) do NOT carry §2.1 linkage.
- `parent_id`→`id` linkage is exactly what the GUI DAG builder joins on (see §2 below) — CONFIRMED.
- Rule reiterated in contract: unknown `kind`/fields MUST be tolerated by consumers (§5, lines 185-188) —
  this is the escape hatch that lets a translator emit CAD-shaped events additively without a schema bump.

## 2. Host consumption: host/src/ui/ai/ai_workspace.cpp — CONFIRMED
- `feedAgentDagEvent(kind, data)` (lines 184-271) is the single ingestion point for the "Agent Workflow DAG"
  view. Comment at 106-107 confirms it's built **purely from the drained `aicad.agent_events/2` stream** by
  joining `parent_id`→`id`, no buildgraph sidecar, no RPC.
- Honesty comment (109-112, graphify-flagged and source-confirmed): only `agent.tool_call`,
  `agent.thought`, `agent.context_injected` have a **live production sink** today. `agent.spawn` /
  `agent.advisor` are emit-wired backend-side but have no production caller (tracker M20) — code exists
  (lines 238-262) so it "lights up" the moment real events arrive, but nothing is fabricated.
- Span-id fallback for legacy/`/1` records without `id`: synthesizes `_leaf<seq><node_id>` (lines 205-209)
  so un-linked records still render as leaves — this is the exact mechanism a translator could lean on if
  it cannot mint proper span ids for some OMP event types, but it degrades tree structure (no real parent
  join), so it should be a last resort, not the default translation strategy.
- Handled kinds in the switch: `agent.tool_call` (211-221), `agent.thought` (222-228),
  `agent.context_injected` (229-237), `agent.spawn` (238-261, including child-root seeding via
  `subagent_id`), `agent.advisor` (262+, truncated in view but present). **No `agent.thought.delta`
  branch exists** — confirms correction #1 above.
- Run-boundary reset: `kind == "run_started" || kind == "dag.run.started"` resets the tree (line 189);
  any event carrying a different `run_id` mid-stream also triggers a reset (193-195) — a translator must
  either preserve OMP's session/turn id as a stable `run_id` per CAD run, or explicitly emit `run_started`/
  `dag.run.started` at OMP session start to avoid spurious tree resets on every event.

## 3. Transport shapes — CONFIRMED (structurally divergent)
### CAD worker RPC (`services/agent/worker_rpc.py` + `contracts/worker_rpc.md:1-18`)
- Wire format: newline-delimited JSON over stdio, **call-scoped**:
  `{"id": n, "method": str, "params": {...}}` →
  zero-or-more `{"id": n, "event": kind, "data": {...}}` →
  terminal `{"id": n, "ok": true, "result": {...}}` or `{"id": n, "ok": false, "error": {code, message, detail}}`.
- `worker_rpc.py` (1346 lines) is a method-table registrar (`SUBSCRIPTION_METHODS`, session/skills/triggers/
  role_models/bench tables merged into `WORKER_METHODS`), each handler signature `(params: dict, _emit:
  Callable) -> dict` — confirmed at lines 47, 72, 168 etc. `_emit` is exactly the mid-call event-emission
  hook that produces the `{"id","event","data"}` frames the contract describes.
- Handshake: `ping` → `{"pong": true, "build123d": ..., "protocol": 2}` (worker_rpc.md:16).
- Steering methods (`dag_pause`, `dag_approve`, `dag_inject`, `dag_set_autonomy`) are NOT written to
  events.jsonl — they're inbound RPC commands documented for discoverability only
  (`contracts/agent_events.md:146-160`), implemented in `services/dag/worker_rpc.py` (confirmed to exist
  via glob).

### OMP `--mode rpc` (docs/rpc.md:1-57, docs/rpc.md:623-625)
- Also newline-delimited JSON / NDJSON over stdio — superficial similarity ends there.
- **No call/result correlation for agent events.** Frames on stdout fall into ~11 categories: ready frame,
  `RpcResponse` (`id`-correlated, command replies only), `AgentSessionEvent` objects (`agent_start`,
  `message_update`, `agent_end`, etc. — **NOT `id`-correlated to any request**), extension UI requests,
  host-tool call/cancel, host-URI request/cancel, extension errors, available-commands updates,
  prompt-lifecycle hints, subagent frames (gated by `set_subagent_subscription`), slash-command side
  channels.
- Startup writes `{"type":"ready"}` before any commands are processed (rpc.md:28).
- `prompt` command is **fire-and-forget-acknowledged**: response says `{data:{agentInvoked:false|true}}`
  but the actual turn's progress/completion is only observable via the async `AgentSessionEvent` stream
  (`agent_start`...`agent_end`), not via the RPC response (rpc.md:166-188).
- Example stream (rpc.md:623-625):
  ```
  { "type": "agent_start" }
  { "type": "message_update", "assistantMessageEvent": { "type": "text_delta", "delta": "..." }, "message": {...} }
  { "type": "agent_end", "messages": [] }
  ```
- Streaming content-block lifecycle triplets (docs/provider-streaming-internals.md:20-23): text
  (`text_start`→`text_delta`*→`text_end`), thinking (`thinking_start`→`thinking_delta`*→`thinking_end`),
  tool call (`toolcall_start`→`toolcall_delta`*→`toolcall_end`) — these arrive nested inside
  `message_update.assistantMessageEvent`, not as top-level frame kinds.

## 4. Translation table: OMP `AgentSessionEvent` → CAD `agent_events/2`
| OMP event (nested path) | CAD kind | Required §2.1 fields translator must synthesize | Notes |
|---|---|---|---|
| `agent_start` | `dag.run.started` or a session-scoped `run_started` marker | `run_id` (mint/reuse OMP session id) | Needed so `feedAgentDagEvent` doesn't spuriously reset (ai_workspace.cpp:189-196). |
| `message_update.assistantMessageEvent.type=="thinking_start"`→`"thinking_delta"`*→`"thinking_end"` | `agent.thought.delta` (deltas) + `agent.thought` (on `thinking_end`, full accumulated text, ≤500 chars per contract) | `id` (span, stable per thinking block — mint once at `thinking_start`, reuse for every delta + the terminal `agent.thought`), `parent_id` (turn/run span), `actor` | **Gap**: `agent.thought.delta` has zero GUI consumer today (see §1/§2) — translator can emit it (schema allows, conformance requires tolerance of unknown-but-documented kinds) but it renders as nothing until `feedAgentDagEvent` adds a branch. `agent.thought` on `thinking_end` is the one that actually renders. |
| `message_update.assistantMessageEvent.type=="text_delta"` | not in CAD `/2` vocabulary at all (no `agent.message`/`agent.text_delta` kind) | — | CAD's agent-level vocabulary has no analog for the final-answer text stream — only `agent.thought`/`agent.tool_call`/`agent.artifact`/etc. The chat transcript itself (not agent_events) is presumably where OMP `text_delta` would need to land, via a different seam (chat message stream), not this contract. **Flag for the plan**: text_delta has no home in agent_events/2; needs a separate mapping decision, not silently dropped. |
| `message_update.assistantMessageEvent.type=="toolcall_start"`→`"toolcall_delta"`*→`"toolcall_end"` | `agent.tool_call` (emitted once, on `toolcall_end`/tool completion, matching CAD's "Tool call completed" semantics, contracts/agent_events.md:122) | `id`, `parent_id`, `actor`, plus `tool_name`, `target` (derive via `derive_target()`-equivalent from tool args), `args_digest`/`result_digest` (SHA-256 first 16 hex, `_digest()` at agent_events.py:40-46), `ok`, `latency_ms` | CAD has no partial/delta tool-call kind — `toolcall_delta` (partial JSON args) must be buffered by the translator and only surfaces as one completed `agent.tool_call`. |
| `tool_execution_start`/`tool_execution_end` (SDK-level, sdk.md:190) | folds into the same `agent.tool_call` emission as above (latency_ms = execution span) | same as above | Two OMP-side signals (provider toolcall stream vs. actual execution) collapse to one CAD event. |
| `turn_start`/`agent_end` | no direct `/2` analog; `agent_end` could map to `dag.run.done` (`status`, `node_states`) if the translator treats an OMP turn as a "run" | `run_id`, `status` | Semantic mismatch: `dag.run.done` is DAG-executor vocabulary (§3.1), not agent-loop vocabulary — using it for an OMP turn conflates two binding relationships the contract explicitly separates (agent_events.md:25-27). Safer to leave `agent_end` untranslated or invent nothing and let the host's own turn-boundary UI handle it outside agent_events. |
| subagent frames (`subagent_lifecycle`, `subagent_progress`, `subagent_event`) | `agent.spawn` (lifecycle start) potentially, `agent.advisor`/nested `agent.tool_call` etc. for progress | `id`, `parent_id`=spawner span, `subagent_id`=child root (matches CAD's `agent.spawn` `subagent_id` field, agent_events.py context / contract line 136) | This is the ONE place OMP's model (subagent frames gated by `set_subagent_subscription`) maps fairly cleanly onto CAD's `agent.spawn`, since CAD's own field semantics (`parent_id`=spawner, `subagent_id`=child root) were designed for exactly this shape. But `agent.spawn` has **no production caller in CAD today** either (ai_workspace.cpp:110-111) — translator would be the first real producer, same gap class as `agent.thought.delta`. |
| `extension_ui_request` / host-tool call/cancel frames | no `/2` analog — these are OMP-internal tool-approval/UI plumbing | — | Out of scope for agent_events; would need a different bridge (interaction.md `ask_question`/`ask_answer`, contracts/agent_events.md:33-34) if CAD wants to surface tool-approval prompts. |

## Verdicts summary
- Event vocabulary / schema fields (§1): **CONFIRMED** against both `contracts/agent_events.md` and
  `services/agent/agent_events.py` source.
- `parent_id`→`id` GUI consumption model (§2): **CONFIRMED**, with the important nuance that
  `agent.thought.delta` has no rendering code — this refines rather than contradicts the "reserved but
  unused" framing in the target prompt.
- CAD worker RPC transport shape (§3): **CONFIRMED** — `{id,method,params}`→`{id,event,data}`*→
  `{id,ok,result|error}`.
- OMP `--mode rpc` transport shape (§3): **CORRECTED relative to a naive "also NDJSON, so it's compatible"
  assumption** — it is NOT call/result-correlated for agent events; `AgentSessionEvent` frames are
  asynchronous and un-`id`-scoped, unlike CAD's per-call event stream. A translation layer must bridge a
  call-scoped protocol (CAD) to a session-scoped async event bus (OMP), which is a genuine architectural
  seam, not just a field-renaming exercise.
- Streaming deltas: **CORRECTED** — OMP already streams real token-level `text_delta`/`thinking_delta`/
  `toolcall_delta` today (not "reserved for the future" on OMP's side); CAD's `agent.thought.delta` is
  reserved specifically because CAD's OWN provider seam is non-streaming. The mismatch is an opportunity
  (OMP could be the first real streaming producer) but requires new GUI code in `ai_workspace.cpp` to be
  useful, and there's no existing CAD analog at all for streaming final-answer text (`text_delta`).
</content>
