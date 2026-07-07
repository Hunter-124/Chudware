# AI Workspace v2 — Implementation Plan

> Architecture/planning pass for tracker **Section M** (`host/GUI_POLISH_TRACKER.md`, rows M1–M20).
> Redesign the shipped AI hub into a compact, **transparency-first**, **agent-execution-DAG-centric** surface.
> This document is the decomposition source for the build agents; it changes **no code**.
>
> **Ownership / collision rule:**
> - `host/src/ui/ai/ai_workspace.cpp` — the main AI-workspace surface (M1–M11, M15–M17 layout/DAG).
> - `host/src/ui/legacy_chat.cpp` — the **shared** chat renderer, ALSO the docked Copilot. Edit surgically (M12–M14, M18–M19 touch it).
> - `host/src/ui/activity_panel.cpp/.h` — existing `events.jsonl` consumer (`parseEvents`/`drawAttempt`); reuse/extend for the DAG store.
> - Backend event emission lives in `services/agent/` (loop.py, agent_turn.py, delegation.py, ladder.py, ledger.py), `services/dag/` (executor.py, node_runner.py, event_journal.py), and the `engine_oracle` worker transport (`services/worker/worker_client.cpp`).

---

## 1. Current-state map

### 1.1 AI workspace UI — `host/src/ui/ai/ai_workspace.cpp` (what M1–M8 refactor)
- **Entry:** `drawAiWorkspace(AppState&)` — `ai_workspace.cpp:685–733`. Three-column layout: left rail (235px), center (flex), right settings (300px, collapsible). Sync at `:692–693` (`refreshModelAvailability` + `chatWorkspaceSync`).
- **State struct `AiWsState`** — `ai_workspace.cpp:50–73`: `centerView` (0=Conversation, 1=Project Plan), `showSettings`, `runs[]` (`DagRun`), `selRun`, `loadedRun`, `nodes[]` (`DagNode`), `pan`, `zoom`, `dag_status` probe fields. Supporting structs `DagNode` (`:36–40`), `DagRun` (`:42–48`).
- **Tab switcher** — `ai_workspace.cpp:709–716`: two `viewChip()` (`:130–143`) buttons **"Conversation" | "Project Plan"** + a Settings toggle chip. Dispatch at `:719–726` → `drawChatCenter()` or `drawDagView()`.  → **M6/M7/M8 rewrite this into "Agent Workflow | Session Plan" and drop "Conversation".**
- **DAG canvas** — `drawDagView()` `:443–517`, `drawDagCanvas()` `:331–441` (pan/zoom, Bézier edges `:373–388`, status-colored nodes `:390–430`, `statusColor()` `:321–327`).
  - **Data source:** `scanRuns()` `:165–212` reads `~/.local/share/AI-CAD/runs/*.buildgraph.json`; `loadRunGraph()` `:235–270`; `layoutDag()` (longest-path) `:215–233`.
  - **This is the *mission plan* graph** (design→gate→verify stages with live node status), **NOT** the agent-loop tool-call/subagent trace. Steering via `dag_approve`/`dag_abort`/`dag_status` RPC (`steerRun()` `:300–319`, `probeDagStatus()` `:274–298`). See §3 for why this must NOT be conflated with the M9 Agent Workflow DAG.
- **Settings pane (inference block to WIPE)** — `drawSettingsPane()` `:606–679`:
  - Model assignment via `drawProviderModelPicker(st, busy, /*slim=*/true)` `:621` (Provider/Model/API key/Privacy). → **M2 WIPE.**
  - Vision reviewer toggle `:626–644` (`expVisionModel`/`expVisionBaseUrl`). → **M2 WIPE.**
  - Workflow settings `:649–666`: plan mode radio (Supervised/Autopilot) `:650–654`, `maxIters` `:661`, `maxTokens` `:663`, `contextWindow` `:665`. → **M3 KEEP, relocate lower-right.**
  - Persists via `saveSettings(st.settings)` `:668`.
- **Chat embedding** — `drawChatCenter()` `:573–602` reserves composer height (`s_composerH`, one-frame lag) then calls `chatDrawLog(st)` `:591` + `chatDrawComposer(st,busy,canRun)` `:599` (both shared with docked Copilot).
- **Left rail** — `drawSessionsRail()` `:521–569`: `chatDrawSessionsList()` `:529` + a sub-agents list `:534–566` that iterates `s_ai.nodes[]` filtered by `.assigned` (honest empty-state; G3).

### 1.2 Chat data model + renderer (informs M12–M14, M18, M19)
- **`ChatMsg`** — `app_state.h:408–421`: `fromUser`, `text`, `running`, `errKind`, `liveStatus`, `plan` (`PlanInfo`), **`activity` (`vector<ActivityItem>`)**, `curMissionTask`, `steps` (`vector<AgentStep>`, legacy), `clarify`.
- **`ActivityItem`** — `app_state.h:396–406`: `stage` (intake/plan/model/edit/review/fix/memory), `title`, `detail` (raw thoughts/output, collapsible), `reason`, `status`, `durationMs/tokensIn/tokensOut/opsCount`, `expanded`, `missionHeader`. **This is the closest existing transparency surface — stage-granular, NOT tool-call-granular.**
- **`AgentStep`** — `app_state.h:360–370` (legacy per-iteration card: thinking/code/diff/error).
- **`Conversation`** — `app_state.h:432–438`: `id`, `title`, `msgs` (parked when inactive), `lastContextTokens`, `queuedPrompts`. Active session lives in `AppState::chat` (`app_state.h:~899`), tabs in `st.conversations`, `st.activeConv`. **No `run_id` field** (see §3 gap).
- **Renderer** — `legacy_chat.cpp`:
  - `chatDrawLog()` `:3513` → `drawUserBlock()` `:1110` / `drawAssistantTurn()` `:1133–1228` (8-section turn: header, plan card `drawPlanCard()` `:426`, **agent timeline `drawAgentTimeline()` `:1089` → `drawActionCard()` `:991` / `drawThinkingBlock()` `:946`**, prose card, clarify gate `drawClarifyCard()` `:821`, legacy steps `drawAgentStep()` `:627`, live spinner + **model-transparency line** `:1188–1194`, usage footer, error block).
  - **`feedTurnActivity()`** — declared `app_state.h:426`, defined `legacy_chat.cpp:527–605`: the ONLY place `ActivityItem`s are built; consumes `mission_started`/`mission_task_*`/`stage_started`/`agent_output`/`stage_result`/`run_finished`.
  - `submitTurn()` `:2527–2800+`: appends user + running-assistant `ChatMsg`, routes to `launchMissionRunChat()` / `agent_simulate_v1` / `agent_design_v1`, applies streamed `AgentEvent`s in the RPC callback.
  - Composer `drawComposer()` `:3160–3255`, input `drawChatInput()` `:161`/called `:3226` (multi-line, height measured into `s_composerH` — **M18 changes this fixed-reserve to dynamic upward growth**).
  - Docked-only: `drawAIPanel()` `:3265`, `drawCopilotHeader()`, `drawSessionsPanel()`, `drawSettingsView()`, `drawRuntimeBanner()`.
- **Already shown inline:** plan checklist, activity timeline (expandable to thinking/code/diff/thumbnail), streaming prose, clarify gate, model-transparency line. **NOT shown:** individual tool calls (only aggregated into stage rows), context-injection events, structured streamed thoughts.
- **M19 note:** `chatDrawLog()` iterates ALL `st.chat` every frame — no virtualization; persist only saves text/fromUser/errKind (not steps/activity).

### 1.3 Execution event model — producers, transport, consumers
- **Contract (KEY FINDING):** `contracts/agent_events.md` defines schema **`aicad.agent_events/1`** (frozen T-F17). Storage: `~/.local/share/AI-CAD/cache/runs/<run_id>/events.jsonl` (persist-then-callback + fsync). Envelope: `{kind, ts, run_id, ...}`. **Already-defined kinds:** DAG lifecycle (`dag.run.started/done`, `dag.node.started/done/failed/paused/approval_required/manual_required`); **agent-level `agent.thought`, `agent.tool_call` (tool_name + args_digest/result_digest SHA + latency_ms), `agent.artifact`, `agent.question`, `agent.answer`**; `mission.brief.*`. Steering RPCs (`dag_pause`/`dag_approve`/`dag_inject`/`dag_set_autonomy`) documented, implemented in `services/dag/worker_rpc.py`.
- **Journal:** `services/dag/event_journal.py:53` `EventJournal.emit(kind, payload)` (stamps `kind/ts/run_id`, append + fsync) + `.wrap(other)` composite emitter.
- **Producers today:**
  - **DAG executor/node_runner** (`services/dag/executor.py:110–118`, `node_runner.py:308–405`) — emit all 8 `dag.*` lifecycle kinds. **Complete per contract.**
  - **Built-in agent loop** (`services/agent/loop.py`) — `_emit()` `:825` dual-sinks to `on_event` **and** (for some paths) `EventJournal`. Emits only coarse kinds: `status` (phase thinking/building/rendering `:1186–1251`), `plan` `:444`, `buildgraph` `:485`, `iteration` `:1251`, `mission_phase` `:1551`, `mission.eta` `:917`, `ai_artifact` `:965`. **Does NOT emit `agent.tool_call` / `agent.thought` / context-injection / subagent-spawn.**
  - **Ledger** (`services/agent/ledger.py:197`) — `agent_turn.py:~181` logs tool_call records to the ledger (persistent), **but NOT through `on_event`** → invisible to the live GUI.
  - **Delegation** (`services/agent/delegation.py:407–469`) spawns subagents — **no spawn event.** **Advisor/council** (`services/agent/ladder.py:419`) emits `ladder_council` — **not in the frozen vocabulary.**
- **Transport (GUI-side):**
  - `AgentEvent` — `app_state.h:442–452`: coarse (kind/index/phase/text/thinking/code/diff/plan…). **No timestamp, no parent link, no tool/subagent/context fields.**
  - `JobState.events` (`app_state.h:301`, mutex `evMtx`) — drained on the GL thread once/frame in `app.cpp` (routes `orch_*` → ActivityPanel, legacy → ChatMsg).
  - Worker streaming: `services/worker/worker_client.cpp:851–855` delivers `{"id","event","data"}` (no `ok`) frames to an `onEvent` (`EventFn`) callback **before** the final response; `jobs.cpp:~34–97` wraps them into `AgentEvent` with an `"orch_"` prefix. Batched replay path also exists (`worker_client.cpp:620–644`). **This dual-sink stream already flows worker→GUI live.**
- **GUI consumer:** `host/src/ui/activity_panel.cpp/.h` (`parseEvents` `activity_panel.h:94`, `drawAttempt` `:97`) already parses events.jsonl-style data into a per-attempt activity view. **Note:** the contract names `mission_control_panel.cpp` as the DAG consumer — **that file does NOT exist**; `ActivityPanel` is a *list/attempt* view, not a *graph* renderer.

---

## 2. Gap analysis for M20 (the load-bearing prerequisite)

**Verdict: the event *schema* and *transport* largely EXIST; the *producers* are ~40% instrumented and need targeted new emission. This is a "wire + extend existing", not a "build from scratch".**

| Layer | State | What's needed |
|-------|-------|---------------|
| Event schema/contract | **Exists** (`agent_events.md`, frozen `/1`) | Bump to `/2` for the net-new kinds + human-readable target field (below). |
| Storage/journal | **Exists** (`EventJournal`, events.jsonl, fsync) | Reuse as-is. |
| Worker→GUI transport | **Exists** (streaming `onEvent` + `JobState.events` drain) | Reuse the dual-sink drain for live; keep events.jsonl for replay. |
| DAG lifecycle events | **Complete** (executor/node_runner) | None. |
| **Agent-loop tool-call events** | **MISSING** | Emit `agent.tool_call` at `agent_turn.py:~181` (currently ledger-only). |
| **Agent-loop thought events** | **Partial** (coarse `status/thinking`) | Emit structured `agent.thought` at `loop.py:~1140`. |
| **Context-injection events** | **MISSING** | New kind `agent.context_injected`; emit at `loop.py` context-pack assembly (~L1300–1400). |
| **Subagent spawn/branch events** | **MISSING** | New kind `agent.spawn`; emit at `delegation.py:spawn_agent()` `:407`. |
| **Advisor/council events** | **Off-vocabulary** (`ladder_council`) | New kind `agent.advisor` (or map onto `agent.thought`) at `ladder.py:419`. |
| **Parent linkage** | **MISSING** on agent-level events | Add `parent_node_id`/`actor`/`stage`/`attempt` fields (needs schema bump). |
| **Human-readable tool target** | **MISSING** (`args_digest` is a SHA) | M14 needs "what it acts on" — add a `target` string field (schema bump). |
| Session→run_id index | **MISSING** | `Conversation` has no `run_id`; needed to scope the DAG per session (M8/M9). |

### 2.1 Proposed minimal event schema (`aicad.agent_events/2` delta)
Keep the `/1` envelope (`kind, ts, run_id`). Add to every agent-level event: **`parent_node_id`** (string; links a node to its spawner/trunk), **`actor`** (which agent/subagent emitted it), **`seq`** (monotonic per-run ordering). Add net-new kinds:
- `agent.tool_call` (extend `/1`): keep digests; **add `target`** (human string, e.g. "top face of Part1", "sketch S3") for M14.
- `agent.context_injected`: `{node_id, parent_node_id, source ("ledger"|"advisory"|"previous_output"|"memory"), summary (≤80), token_est}` — M12.
- `agent.spawn`: `{node_id, parent_node_id, subagent_id, role, stage, budget}` — M9/M11.
- `agent.advisor`: `{node_id, parent_node_id, kind ("advise"|"design_review"|"council"), verdict_summary}` — M9.
- (optional) `agent.thought` token-stream variant `agent.thought.delta` — **gated on OPEN QUESTION Q4 (is streaming even available?).**

### 2.2 Exact emission points (5 MVP edits, all backend)
1. `services/agent/agent_turn.py:~181` — after the tool-call record is built, also `on_event({"kind":"agent.tool_call", target, tool_name, digests, latency_ms, node_id, parent_node_id})`.
2. `services/agent/loop.py` run entry (~L1442) — `on_event = EventJournal(run_id).wrap(on_event)` so the loop's agent-level events also persist to events.jsonl (parity with DAG).
3. `services/agent/loop.py:~1140` — emit structured `agent.thought` for parsed thinking blocks.
4. `services/agent/loop.py` context assembly (~L1300–1400) — emit `agent.context_injected` for ledger/advisory/prior-output injections.
5. `services/agent/delegation.py:spawn_agent()` `:407` (+ `ladder.py:419`) — emit `agent.spawn` / `agent.advisor` with `parent_node_id`.

### 2.3 Transport recommendation
**Reuse the existing dual sink — do NOT invent a new subscription RPC.**
- **Live (chat transparency M12–M14 + DAG M9):** extend `AgentEvent` (`app_state.h:442`) with the new kinds/fields and drain them through the existing `JobState.events` GL-thread path that already carries chat events. Zero new transport.
- **Replay / persistence:** events.jsonl (already written by `EventJournal`), consumed like `ActivityPanel` does today (rescan-on-mtime). The GUI-side **Agent Workflow DAG store** is built from the same drained events (in-memory graph), with events.jsonl as the durable/replay source.

---

## 3. The dominant architectural fork: two different DAGs

The current `drawDagView` renders `*.buildgraph.json` — the **mission plan graph** (design/gate/verify stages with live status, steered by `dag_approve`). The M9 **Agent Workflow DAG** is a **different graph**: the agent loop's tool-call / context-injection / subagent-branch / advisor trace for one chat session, derived from the §2.1 event stream.

**Consequence for the build:**
- **REUSE the renderer** — `drawDagCanvas()` pan/zoom/Bézier/node code (`ai_workspace.cpp:331–441`) and `statusColor()` are graph-shape-agnostic; keep them.
- **REPLACE the data model + source** — `DagNode`/`DagRun`/`scanRuns()`/`loadRunGraph()` (buildgraph sidecars) are swapped for an event-derived agent-execution graph store fed by the drained `AgentEvent`s (§2.3), scoped by session→run_id.
- The two graphs coexist behind the **M7 tab switcher**: **Agent Workflow** (new event-DAG) | **Session Plan** (the existing buildgraph view, renamed from Project Plan, scoped per session).
- Do NOT bolt tool-call nodes onto the buildgraph loader — they are separate stores sharing one canvas widget.

---

## 4. Phased build plan

> **Parallelism:** Phase 1 (layout) and Phase 5 (chat UX) have **zero backend dependency** and can run immediately, in parallel with Phase 0. Phases 2, 3, 4 are **gated on Phase 0** (the event stream). Phase 0's backend and GUI-transport halves can proceed in parallel once the schema (`/2`) is agreed.

### Phase 0 — Event model + transport (M20) — **GATES 2/3/4**
- **Goal:** emit the structured, timestamped, parent-linked agent-execution event stream and deliver it live to the GUI.
- **Files:** *(backend)* `contracts/agent_events.md` (bump `/1`→`/2`, add kinds/fields §2.1), `services/agent/loop.py`, `services/agent/agent_turn.py`, `services/agent/delegation.py`, `services/agent/ladder.py`, `services/agent/ledger.py` (optional `on_event` bridge), `services/dag/event_journal.py` (reuse). *(GUI transport)* `host/src/core/app_state.h` (extend `AgentEvent`; add `run_id` to `Conversation`), `host/src/core/jobs.cpp` (wrap new fields), the `app.cpp` drain (route new kinds), a new GUI-side **AgentExecGraph store** (new small file under `host/src/ui/ai/`, or extend `activity_panel.*`).
- **Deps:** none (schema agreement first).
- **Closes:** M20. (Enables M9/M11/M12/M13/M14.)
- **Note:** honest-empty behavior until producers emit — build against the frozen contract + the events.jsonl example (§6 of the contract) so it's testable without a live model.

### Phase 1 — Layout / chrome refactor (M1–M8) — **parallel with Phase 0**
- **Goal:** compact, transparency-first shell; wipe inference settings; relocate regions; rename/retab.
- **Files:** `host/src/ui/ai/ai_workspace.cpp` (rewrite `drawAiWorkspace` region math `:685–733`; delete the inference block from `drawSettingsPane` `:621–644`; keep workflow knobs `:649–666` relocated lower-right; replace tab switcher `:709–726` with **Agent Workflow | Session Plan**; drop the Conversation tab so chat is always-visible lower-left; move DAG upper-center); ensure the wiped model/effort/vision settings exist on the **main Settings page** (`host/src/ui/settings_modal.cpp` — verify coverage, add if missing).
- **Deps:** none.
- **Closes:** M1, M2, M3, M4, M5, M6, M7, M8. (M7/M8 only *rename+retab* here; the new DAG content lands in Phase 2.)

### Phase 2 — Agent Workflow DAG rendering + collapse (M9, M10) — **gated on Phase 0**
- **Goal:** render the live agent-execution DAG from the event store; collapsible/scalable nodes.
- **Files:** `host/src/ui/ai/ai_workspace.cpp` (new `drawAgentWorkflowDag()` reusing `drawDagCanvas` `:331–441`; new node model for tool-call/context/subagent/advisor nodes with collapse state; wire to the Phase-0 AgentExecGraph store; keep `drawDagView`/buildgraph as the renamed "Session Plan" tab).
- **Deps:** Phase 0 (event store), Phase 1 (tab shell).
- **Closes:** M9, M10.

### Phase 3 — Chat transparency (M12, M13, M14) — **gated on Phase 0**
- **Goal:** show context-injection events, streamed thoughts, and tool calls (tool + target) inline in the chat.
- **Files:** `host/src/core/app_state.h` (extend `ActivityItem`/add a tool-call/context row type); `host/src/ui/legacy_chat.cpp` (`feedTurnActivity` `:527–605` to fold the new `agent.tool_call`/`agent.context_injected`/`agent.thought` kinds; `drawActionCard` `:991`/`drawAgentTimeline` `:1089` to render tool+target and injection rows). **Edit `legacy_chat.cpp` surgically — it is the shared docked Copilot renderer.**
- **Deps:** Phase 0.
- **Closes:** M12, M14; **M13 partial** (falls back to completed-block thoughts if streaming unavailable — see Q4).

### Phase 4 — Subagent open/steer + recolor/lock/read-only (M11, M15, M16, M17) — **gated on Phase 0 + Phase 2**
- **Goal:** click a subagent node (incl. active) → open/steer its chat in the main conversation window; recolor + lock while viewing a subagent.
- **Files:** `host/src/ui/ai/ai_workspace.cpp` (subagent-node click → set a "viewing subagent" mode bound to a subagent_id/run_id; route its conversation into the main chat window); `host/src/ui/legacy_chat.cpp` (recolor the conversation, disable "end chat", make model/effort/starting-prompt read-only when in subagent-view mode); `host/src/core/app_state.h` (subagent-view state + subagent chat registry). Requires a **subagent session registry** (from `agent.spawn` events, Phase 0).
- **Deps:** Phase 0 (spawn events + registry), Phase 2 (subagent nodes to click).
- **Closes:** M11, M15, M16, M17.

### Phase 5 — Chat UX (M18, M19) — **parallel, independent**
- **Goal:** composer auto-grows upward (capped); endless lazy scrollback.
- **Files:** `host/src/ui/legacy_chat.cpp` (`drawComposer` `:3160`/`drawChatInput` `:161` — replace fixed height reserve with dynamic upward growth + cap; `chatDrawLog` `:3513` — virtualize: render only visible range + buffer, lazy-load older `ChatMsg`s). **Shared renderer — surgical edits.**
- **Deps:** none.
- **Closes:** M18, M19.

### Suggested wave ordering
- **Wave A (immediate, parallel):** Phase 0 (backend + GUI transport), Phase 1 (layout), Phase 5 (chat UX).
- **Wave B (after Phase 0):** Phase 2 (DAG), Phase 3 (transparency).
- **Wave C (after Phase 0 + Phase 2):** Phase 4 (subagent steering).

---

## 5. OPEN QUESTIONS for Hunter (genuine forks only)

1. **Schema bump policy.** The `agent_events` contract is **frozen `/1`** and forbids new kinds/fields without a schema-string bump. M9/M12/M14 require net-new kinds (`agent.context_injected`, `agent.spawn`, `agent.advisor`) and a human-readable `target` field on `agent.tool_call`. **Approve bumping to `aicad.agent_events/2` (extend the frozen contract) vs. a parallel `agent_ui_events` file?** (Recommendation: bump the frozen contract — it is the single-vocabulary rule.)
2. **`target` on tool calls.** `agent.tool_call` currently carries only SHA `args_digest`/`result_digest`. M14 ("what it acts on") needs a plain-language target. **OK to add a `target` string emitted by the tool dispatcher, accepting it's not privacy-hashed?** (Some tools' args are geometry references, not secrets.)
3. **Agent Workflow DAG store — new vs. extend.** Build a **new** GUI-side event-graph store fed by the drained events, or **extend `ActivityPanel`** (which already parses events.jsonl but as a per-attempt *list*, not a graph)? (Recommendation: new store; reuse only the canvas renderer. The buildgraph loader must NOT be reused — different graph, see §3.)
4. **Streamed thoughts (M13) availability.** The contract's `agent.thought` fires on a **completed** reasoning block, not token-streamed. Whether provider streaming is plumbed through the harness to the GUI is **unverified**. **Is live token-streamed thinking actually available from the current providers/harness, or is M13 acceptably "show the completed thought block as soon as it lands"?**
5. **Subagent session registry.** There is delegation (`delegation.py:spawn_agent`) but **no enumerable live subagent/session registry** the GUI can list/open (G3 today is honest-empty). M11/M15–M17 require one. **Confirm subagents are real, addressable sessions (openable + steerable), or is M11 scoped to *view* (read-only replay of a spawned branch) for v1?**
6. **Session→run_id scoping.** M9 is "for the selected session" but `Conversation` has no `run_id` and each turn may mint its own run_id. **A session should aggregate all its turns' run_ids into one DAG — confirm that's the intent (vs. one DAG per turn).**

---

## 6. Summary for the orchestrator
- **M20 verdict:** schema + storage + worker→GUI transport already exist (`contracts/agent_events.md /1`, `EventJournal`, streaming `onEvent` drain). Instrumentation is ~40% done (DAG lifecycle complete; agent-loop tool-call/thought/context/spawn/advisor events missing). **~5 backend emission edits + a schema `/2` bump + a GUI event-graph store** close the gap. No new transport needed.
- **Two-DAG rule:** the M9 Agent Workflow DAG is a **new event-derived graph** reusing the existing canvas renderer, NOT the buildgraph loader. "Session Plan" = the renamed existing buildgraph view.
- **Parallelism:** Phase 1 (layout) + Phase 5 (chat UX) start now; Phases 2/3/4 gate on Phase 0.
