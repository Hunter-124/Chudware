# OMP headless-engine drivable surface — can CAD's C++ host drive OMP instead of the TUI?

> **Historical research (2026-07), superseded 2026-08-31. Not a current runbook.** This dossier
> is dated evidence from the OMP integration research round. Its bun bridge/sidecar,
> `vendor/omp/` and `scripts/vendor_omp.py` were deleted. Any `scratch/omp-src/` or
> inspection-checkout path is an untracked local research copy, not a dependency. Its CAD
> file/line citations are point-in-time. Online agent execution, provider auth, models,
> sessions, memory and compaction now run in the embedded Chudcode RPC runtime. See
> [01-DECISIONS.md, 2026-08-31 section](../01-DECISIONS.md#2026-08-31--superseding-architecture-chudcode-rpc-is-the-only-online-runtime),
> [ARCHITECTURE.md, Agent runtime](../../../ARCHITECTURE.md#agent-runtime), and
> [`.claude/skills/cad-chudcode-integration/SKILL.md`](../../../.claude/skills/cad-chudcode-integration/SKILL.md).

Scope: `packages/coding-agent` (the actual agent CLI/engine), `packages/tui` (render-only component
library, no agent logic), `packages/agent` (`@oh-my-pi/pi-agent-core`, the core loop), `packages/collab-web`
(existing non-TUI frontend precedent), and `packages/mcp*`. Compared against CAD's existing
`worker_rpc`/`agent_events` seam.

## 1. The engine/UI boundary already exists and is TUI-independent

The TUI (`packages/tui`) is a pure terminal-rendering component library (boxes, scroll views, markdown,
diffing) — it has **zero session/agent logic**. All agent-loop state and lifecycle lives in
`packages/coding-agent/src/session/agent-session.ts`, a single ~17k-line class:

> "AgentSession - Core abstraction for agent lifecycle and session management. This class is shared
> between all run modes (interactive, print, rpc)." — `coding-agent/src/session/agent-session.ts:1-14`

Every existing frontend (interactive TUI, ACP JSON-RPC, headless RPC stdio, single-shot print mode,
collab-web browser client) is a thin I/O adapter around this one class. This is the seam CAD should
drive.

### Core event union (`AgentSessionEvent`)
`coding-agent/src/session/agent-session.ts:585-632` extends the lower-level core `AgentEvent`
(`packages/agent/src/types.ts:738-759`, the `@oh-my-pi/pi-agent-core` package) with session-level events.

**Core `AgentEvent` (packages/agent/src/types.ts:738-759)** — the base agent-loop event stream:
- `agent_start` / `agent_end {messages, telemetry?, coverage?}`
- `turn_start` / `turn_end {message, toolResults}`
- `message_start {message}` — user, assistant, or toolResult message appended
- `message_update {message, assistantMessageEvent}` — **streaming deltas** (assistant only)
- `message_end {message}`
- `tool_execution_start {toolCallId, toolName, args, intent?}`
- `tool_execution_update {toolCallId, toolName, args, partialResult}`
- `tool_execution_end {toolCallId, toolName, result, isError?}`

**Session-level additions (agent-session.ts:585-632):**
`auto_compaction_start/end`, `auto_retry_start/end`, `retry_fallback_applied/succeeded`,
`ttsr_triggered`, `todo_reminder`, `todo_auto_clear`, `irc_message`, `notice{level,message}`,
`thinking_level_changed`, `goal_updated`.

Subscribe via `session.subscribe((event: AgentSessionEvent) => …)` — used identically by every
consumer (autolearn controller `autolearn/controller.ts:65`, mnemopi `mnemopi/state.ts:490`, collab
host `collab/host.ts:270-272`, ACP mode `modes/acp/acp-agent.ts:1148-1223`).

**Thinking/reasoning** streams as `message_update` events with `assistantMessageEvent.type ===
"thinking_delta"` (see the Gemini-detector consumer at `agent-session.ts:5520-5526` for the exact
event shape). There is no separate "thinking" event type — it's folded into the same streaming
channel as text.

**Tool approval gate.** `#wrapToolForAcpPermission` (`agent-session.ts:6648-6745`) wraps
permission-required tools in a `Proxy`; on call it invokes `bridge.requestPermission(request, options,
signal)` and blocks until the client resolves an outcome (`allow_once`/`allow_always`/`reject_once`/
`reject_always`/`cancelled`). This is the **client-side approval protocol** — any frontend (ACP client,
or a future CAD bridge) implements `requestPermission` to gate tool calls. Only fires when
`bridge.capabilities.requestPermission` is set and the session isn't in explicit yolo/auto-approve
mode (`#isExplicitAutoApproveMode`, same file ~6655).

### Public command surface (methods on `AgentSession`)
- `prompt(text, images?, options?)` — send user message, `steer(text, images?)` — interrupt
  mid-run (`agent-session.ts:8402`), `abort(options?)` — cancel current turn
  (`agent-session.ts:9044`), `newSession()`, `switchSession(path)`, `branch(entryId)`,
  `navigateTree(targetId, options)`, `compact(...)`, `handoff(...)`.
- Model/thinking: `setInterruptMode(mode)` (`agent-session.ts:9834-9840`), model switching via
  `formatModelString`/`resolveModelOverride` helpers plumbed through the RPC `set_model`/
  `cycle_model` commands (see §2).
- `clearQueue(options?)` (`agent-session.ts:8840`) drops queued steers/follow-ups.

## 2. Headless is a first-class, already-shipped mode — no TUI required

`coding-agent/src/main.ts` dispatches on `--mode`:
- `--mode rpc` / `--mode rpc-ui` (`main.ts:1143-1509`) → `runRpcMode(session, ..., eventBus)`
  (`modes/rpc/rpc-mode.ts`). **Newline-delimited JSON over stdin/stdout** — commands in,
  responses+events out. `rpc-ui` variant additionally exposes a tool-UI context (for e.g. rendering
  tool call widgets) while still headless.
- `--mode acp` (`main.ts:1387-1401`) → `runAcpMode(createAcpSession)` (`modes/acp/acp-mode.ts`,
  `modes/acp/acp-agent.ts`) — implements the **Agent Client Protocol** (Zed's JSON-RPC agent
  protocol) over stdio.
- Default non-interactive path (piped stdin, `--print`) → `runPrintMode` (`modes/print-mode.ts`) —
  single-shot, no persistent session driving.

All three protocol modes explicitly own stdin and set `Bun.env.PI_NO_PTY = "1"` /
`PI_NO_TITLE = "1"` (`main.ts:1148-1153`) — **the TUI's terminal machinery (PTY, title bar) is
disabled entirely**, confirming headless operation is a supported, tested configuration, not an
incidental side effect of piping. `runRpcMode`/`runAcpMode` are dynamically imported
("Branch-only protocol runner: keep RPC/ACP server code out of normal interactive startup" —
`main.ts:1399, 1507`), i.e. structurally isolated from the interactive/TUI code path.

### RPC mode protocol detail (the best-fit precedent for CAD)
`modes/rpc/rpc-types.ts` (full file, 502 lines) is the complete schema:

```
/** Commands are sent as JSON lines on stdin.
 *  Responses and events are emitted as JSON lines on stdout. */
```
(`rpc-mode.ts:1-11`)

- **`RpcCommand`** union (rpc-types.ts:27-87): `prompt`, `steer`, `follow_up`, `abort`,
  `abort_and_prompt`, `new_session`, `get_state`, `get_available_commands`, `set_todos`,
  `set_host_tools`, `set_host_uri_schemes`, `set_subagent_subscription`, `get_subagents`,
  `get_subagent_messages`, **`set_model`**, `cycle_model`, `get_available_models`,
  `set_thinking_level`, `cycle_thinking_level`, `set_steering_mode`, `set_follow_up_mode`,
  `set_interrupt_mode`, `compact`, `set_auto_compaction`, `set_auto_retry`, `abort_retry`, `bash`,
  `abort_bash`, `get_session_stats`, `export_html`, `switch_session`, `branch`,
  `get_branch_messages`, `get_last_assistant_text`, `set_session_name`, `handoff`, `get_messages`,
  `get_login_providers`, `login`.
- **`RpcResponse`** union (rpc-types.ts:170-299) mirrors every command 1:1 with
  `{id?, type:"response", command, success:true, data}` or `{success:false, error}`.
- **Events**: `AgentSessionEvent` objects streamed as they occur (imported directly, not
  redefined) — i.e. the RPC event stream **is** the same `AgentSessionEvent` union documented in
  §1, just serialized as JSON lines instead of in-process callbacks.
- **Host tool bridge** (`set_host_tools`, `RpcHostToolBridge` in `modes/rpc/host-tools.ts`) and
  **host URI scheme bridge** (`set_host_uri_schemes`, `host-uris.ts`) let the *driving host*
  register its own tools/URI handlers that the agent can call — this is the RPC-mode equivalent of
  a custom-tool extension point, useful if CAD wants to expose CAD-specific tools (sketch ops,
  doc mutation) directly to the OMP agent loop rather than proxying through a separate provider
  seam.
- Subagent introspection (`get_subagents`, `get_subagent_messages`, `set_subagent_subscription`
  levels `off`/`progress`/`events`) streams nested subagent lifecycle/progress — directly maps to
  CAD's `agent.spawn` / delegation concept.

**Conclusion: `--mode rpc` is essentially a pre-built, production-hardened version of what CAD's
`worker_rpc.md` protocol 2 already is** (newline-delimited JSON, request/response + streamed
events, host-injectable tools) — just over a Bun/Node process instead of a Python worker.

## 3. Non-TUI frontend precedent: `collab-web`

`packages/collab-web` is a **browser (React + Vite) frontend** that already proves the engine can be
driven by a completely different UI stack:
- `collab-web/src/lib/socket.ts` — WebSocket transport.
- `collab/host.ts` (in coding-agent) is the **server side** of this: it subscribes to
  `AgentSessionEvent` (`collab/host.ts:270-272`) and rebroadcasts a filtered/shrunk subset
  (`WIRE_AGENT_EVENT_TYPES`, `shrinkForReplication`) over the wire as
  `{t:"event", event: AgentSessionEvent}` frames (`collab/protocol.ts:94-97`).
- `collab-web/src/components/transcript/Transcript.tsx`, `ToolCard.tsx` render assistant
  messages/tool calls; `collab-web/src/tool-render/tools/*.tsx` (one file per built-in tool: bash,
  edit, read, write, grep, glob, web-search, task, todo, hub, job, lsp, ask, github, browser, fetch,
  eval, generate-image, inspect-image, memory-*, report-tool-issue, resolve, debug, ast-edit,
  ast-grep) is a **per-tool renderer registry** (`tool-render/registry.ts`) — the exact pattern CAD
  would need for a "render tool_call by tool_name" panel.
- `collab-web/src/components/shell/Composer.tsx` is the input box (user msg send);
  `AgentDrawer.tsx`/`AgentsPanel.tsx` show subagent state.

This is strong precedent that a completely custom, non-terminal UI (here: a web app; for CAD: the
Qt/C++/ImGui-ish host) can consume the same `AgentSessionEvent` wire protocol collab-web/host.ts
already externalizes. collab-web talks to a **remote/shared** session (multi-guest editing
semantics via CRDT-ish state broadcast) which is more complex than CAD needs; the RPC mode (§2) is
architecturally simpler and a better direct fit since it's single-client, local-process,
stdio-based.

`packages/mcp` (`coding-agent/src/mcp/*`) is the **client-side** MCP tool-connection manager
(`discoverAndLoadMCPTools`, `MCPManager`) — OMP *consumes* MCP servers as tool providers; it does
not itself expose an MCP server frontend for driving the agent loop. Not a headless-driving
precedent; skip for this purpose.

## 4. Event/command catalog a CAD-driven UI must implement

### Must render (events, from `AgentSessionEvent` / RPC event stream)
| Event | Fields | UI need |
|---|---|---|
| `message_start`/`message_update`/`message_end` | `message`, `assistantMessageEvent` | streaming assistant text + thinking deltas (`assistantMessageEvent.type === "thinking_delta"` etc.) |
| `tool_execution_start/update/end` | `toolCallId,toolName,args,intent?` / `partialResult` / `result,isError?` | tool call cards (per-tool renderer, à la collab-web `tool-render/tools/*`) |
| `turn_start`/`turn_end` | — / `message,toolResults` | turn boundaries |
| `agent_start`/`agent_end` | — / `messages,telemetry?,coverage?` | run lifecycle |
| `auto_compaction_start/end`, `auto_retry_start/end` | reason/action/result | background maintenance banners |
| `thinking_level_changed` | level/configured/resolved | reasoning-effort indicator |
| `notice` | level, message | toast/banner |
| `todo_reminder`/`todo_auto_clear` | todos | todo panel sync |
| `goal_updated` | goal, state | goal-mode panel |
| permission request (via `bridge.requestPermission`, not a stream event) | toolCallId, toolName, title, rawInput, locations | **approval dialog** — must resolve with an outcome or the tool call blocks |

### Must send (commands, RPC-mode vocabulary — §2 list is the exact contract)
User message (`prompt`/`steer`/`follow_up`), approve/deny (via `requestPermission` resolution, ACP
mode) or persisted policy (RPC mode has no explicit approve/deny command — RPC mode expects the
session to run in explicit yolo/auto-approve, since RPC has no permission-bridge concept; **ACP
mode** is the one with the interactive approval protocol), cancel (`abort`/`abort_and_prompt`),
switch model (`set_model{provider,modelId}`/`cycle_model`), set thinking level
(`set_thinking_level`/`cycle_thinking_level`), bash (`bash`/`abort_bash`), session mgmt
(`new_session`,`switch_session`,`branch`,`compact`,`handoff`), state polling (`get_state` →
`RpcSessionState{model,thinkingLevel,isStreaming,isCompacting,...,contextUsage}`).

**Important gap for CAD:** RPC mode has no approval gate — it's designed for either fully-autonomous
(yolo) operation or a host that pre-decides tool policy via `tools.approval` settings. If CAD wants
an interactive per-tool-call approve/deny like its own worker RPC probably doesn't need (CAD tools
are geometry ops, not filesystem/bash), **ACP mode** is the one with `requestPermission` built in.
Driving OMP for CAD's actual use (agent calls CAD-specific tools, not bash/edit/write) likely wants
RPC mode's host-tool bridge (`set_host_tools`) rather than ACP's tool set, since ACP's built-in tools
are file/bash-oriented coding tools, not CAD-domain tools.

## 5. CAD's existing worker↔host protocol, for adaptation-cost comparison

CAD already runs an **architecturally identical** transport to OMP's RPC mode:

- **Wire format** (`contracts/worker_rpc.md:1-8`): "newline-delimited JSON over stdio. Request
  `{"id": n, "method": str, "params": {…}}` → zero or more event lines `{"id": n, "event": kind,
  "data": {…}}` → final `{"id": n, "ok": true, "result": {…}}` or `{"id": n, "ok": false, "error":
  {code, message, detail}}`." — this is a near-exact structural match to OMP's RPC-mode
  `{id?, type:"response", command, success, data|error}` + streamed event lines.
- **Multiple Python worker processes** per domain: `services/agent/worker_rpc.py` (the one already
  read: merges `SUBSCRIPTION_METHODS`, `session_cmds` (`session_compact`/`shake`/`handoff`/`btw`),
  skills, triggers/packs, ai_roles/model_rankings, bench), plus separate worker_rpc.py files for
  `sim/em`, `parts`, `reveng`, `plm`, `dag`, `deliverables` — CAD already runs **N specialized
  worker processes**, so adding one more (a Bun/OMP RPC-mode process) fits the existing
  multi-worker pattern; it is not an architectural departure.
- **C++ host side**: `host/src/core/sim_client.cpp:164-166` shows the call-site pattern:
  `worker_.call(method, params, result, err, timeoutMs, onEvent)` — a generic RPC client with an
  `onEvent` callback already exists (`Worker` class, presumably `worker_rpc.h/.cpp` — not found
  under that literal name; likely named differently or templated per-domain, confirm exact
  filename before implementation but the call surface at `sim_client.cpp:164` is representative).
- **Existing agent event contract**: `contracts/agent_events.md` (`aicad.agent_events/2`) — CAD
  already has its own agent-execution event schema (`agent.thought`, `agent.tool_call{target,
  args_digest,result_digest,ok,latency_ms}`, `agent.artifact`, `agent.context_injected`,
  `agent.spawn`, `agent.advisor`, `agent.classification`, `plan_state`, `agent_steer`) emitted by
  `services/agent/agent_events.py` (Python) **and independently** by `core/agent/events.cpp`
  (C++ agent harness) — CAD runs its own dual-language agent loop today (Python `services.agent` +
  a native C++ `core/agent` harness used for the in-app copilot). Consumed GUI-side at
  `host/src/core/app_state.h` (`AgentEvent` struct) and `host/src/core/jobs.cpp` (drain), rendered
  in `host/src/ui/copilot/feed.cpp:1494` / `activity_timeline.cpp:417`.
- **Provider seam** (per task constraints): `Provider.complete(system,messages,max_tokens)->str` /
  `complete_ex->(text,thinking)` — **synchronous, non-streaming**. This is the single biggest
  mismatch versus OMP: OMP's entire session/UI contract is built around **streaming**
  (`message_update` deltas, `assistantMessageEvent`, `thinking_delta`). CAD's own contract already
  flags this: `agent_events.md:121` — `agent.thought.delta` is "reserved... NO producer emits this
  today" specifically because `Provider.complete`/`complete_ex` "returns whole strings
  (non-streaming)". **Any FORK/SIDECAR approach that wants OMP's live streaming UX requires either
  (a) a new streaming provider seam in CAD/services/agent (replacing or adding to `complete_ex`), or
  (b) accepting OMP's stream degraded to arrival-of-whole-message granularity** (which is what CAD's
  own C++ harness already does — "Batched post-run on in-proc C++ path (no live stream refactor)"
  per `agent_events.md:230`).

### Adaptation cost gauge
- **Transport**: near-zero conceptual delta — both are newline-delimited-JSON-over-stdio,
  request/response + event-stream. A CAD `WorkerClient`-equivalent (`sim_client.cpp` pattern) could
  point at a Bun `--mode rpc` OMP process today with modest glue.
  Estimate: **S–M** (spawn/lifecycle management + response↔request `id` correlation + event
  fan-out into `app_state.h`/`jobs.cpp`, all boilerplate CAD already does per-worker).
- **Event vocabulary mapping**: **M** — CAD's `agent_events/2` and OMP's `AgentSessionEvent` are
  different but structurally compatible vocabularies (both have thought/tool_call/artifact-shaped
  events); a translation layer (OMP event → CAD `agent_events/2` kind, or render OMP events
  natively in a new GUI panel) is needed either way. §2.1 parent/id linkage in CAD's contract has no
  direct OMP equivalent (OMP's tree comes from `toolCallId` + subagent registry, not a universal
  span id) — would need synthesis.
  Estimate: **M**.
  Risk: CAD's `activity_timeline.cpp`/`feed.cpp` transparency rows are hand-built for the
  `agent_events/2` shape (`args_digest`/`target`/`parent_id`) — reusing them for OMP events means
  either reshaping OMP events into that exact schema at the bridge, or building a **second** render
  path (duplicate UI code, against the codebase's own "one vocabulary" rule at
  `agent_events.md:5-7`: "Never duplicate this vocabulary in a parallel file").
- **Tool surface**: **L** — OMP ships ~30 general-purpose coding tools (bash/edit/write/read/grep/
  glob/task/hub/mcp/lsp/eval/tts/web-search/...). None are CAD-domain (sketch/doc/feature ops).
  Either (a) use OMP RPC mode's host-tool bridge (`set_host_tools`) to inject CAD's own
  `doc_apply_ops`-style tools and disable/hide OMP's built-ins, or (b) run OMP's coding tools
  alongside CAD tools (bash/edit become "edit generated Python/build123d files" — plausible given
  CAD is Python-scripted geometry, but changes the product surface materially). This tool-surface
  decision is the single largest scope question, independent of transport.
- **Streaming provider mismatch** (above): **M**, only if live token streaming in the OMP sense is a
  goal; otherwise **S** (accept the coarser "whole message arrives" granularity CAD's own harness
  already uses).
- **Native binaries / bun-only APIs risk**: OMP coding-agent depends on `@oh-my-pi/pi-natives`
  (`sdk.ts:130`, `MacOSPowerAssertion`) and Bun-specific runtime hooks (`Bun.env`, `bun test`
  detection `isBunTestRuntime`) — running OMP headless still requires the **Bun runtime** as a
  subprocess (not portable to a pure-Node embed), which is fine for a sidecar architecture (spawn
  `bun run coding-agent --mode rpc`) but rules out in-process embedding into the C++ host without a
  full N-API/Bun-FFI bridge — reinforcing that **sidecar-over-stdio (RPC mode), not in-process
  embedding, is the only realistic path**, matching the already-decided bridge approach.

## Recommendation

| Feature | OMP location | CAD counterpart | Rec | Effort | Risks |
|---|---|---|---|---|---|
| Headless agent-loop process | `coding-agent/src/modes/rpc/*` (`--mode rpc`) | `services/*/worker_rpc.py` pattern | **SIDECAR** — spawn as one more specialized worker, same call pattern as `sim_client.cpp` | S–M | Bun runtime dependency; process lifecycle mgmt |
| Event stream → GUI | `AgentSessionEvent` (agent-session.ts:585) | `aicad.agent_events/2` (agent_events.md) | **SIDECAR + translation layer** — do not duplicate render code; map OMP events into `agent_events/2`-shaped rows for `feed.cpp`/`activity_timeline.cpp` reuse, OR add a clearly-separate "OMP session" panel | M | vocabulary divergence, span-id synthesis, duplicate-render-path risk |
| Tool approval / permission gate | `#wrapToolForAcpPermission` (agent-session.ts:6648), ACP `requestPermission` | CAD tool policy (implicit, no explicit gate found) | **PORT the *concept*, not the code** — implement CAD-side `requestPermission`-equivalent bridge if per-call approval desired | S | ACP built-in tools are file/bash-shaped, not CAD's approval needs |
| Tool set (bash/edit/write/grep/...) | `coding-agent/src/tools/*` | CAD's `doc_apply_ops`/domain tools | **KEEP CAD's own tools**; inject via OMP RPC mode's `set_host_tools` bridge, hide/disable OMP's file tools unless Python-script editing is in scope | M–L | scope creep into a general coding agent vs. CAD-domain agent |
| Streaming (thinking/text deltas) | `message_update`/`assistantMessageEvent` (core `AgentEvent`) | `Provider.complete_ex` (whole-string, non-streaming), `agent.thought.delta` reserved-unused | **PORT** a streaming provider seam if live-typing UX is wanted; else **KEEP** current whole-message granularity | M (if streaming wanted) | requires new provider seam across CAD's provider implementations |
| Non-TUI frontend architecture | `collab-web` (React+WS) + `collab/host.ts` broadcaster | none | Precedent only — confirms multi-frontend-over-one-session works; not directly reusable (collab is multi-guest/CRDT, heavier than CAD needs) | — | — |

**Bottom line:** OMP's `--mode rpc` is materially the same shape as CAD's own worker RPC contract —
adopting OMP as a driven engine via a bun sidecar is the correct move already implied by the
decided architecture, and the wire-protocol adaptation cost is low. The real cost centers are (1)
event-vocabulary reconciliation with CAD's frozen `agent_events/2` contract (avoid a second parallel
vocabulary), (2) deciding whether OMP's general coding tool set is in-scope or should be replaced
entirely by CAD's host-injected domain tools, and (3) whether CAD wants true token-level streaming
(requires a provider-seam change orthogonal to the OMP integration itself).
