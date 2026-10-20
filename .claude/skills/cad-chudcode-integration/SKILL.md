---
name: cad-chudcode-integration
description: "Extend or debug Chudware's compiled Chudcode RPC integration: persistent sessions, host tools, control, auth, memory, events, compaction, recovery, and release artifacts"
---

# Chudware ↔ Chudcode integration

Canonical skill for the online agent boundary: online execution, provider/auth/model
settings, session persistence and recovery, memory, compaction/retry, browser/computer, STT,
streamed events, host-tool dispatch, and the embedded Chudcode artifact. Repo-wide working
rules, daemon/document/sim invariants: `.claude/skills/chudware-dev/SKILL.md`. Contracts are
append-only. `docs/omp-integration/` is dated provenance, not the current RPC authority.
Version-specific runtime behavior below is checked against
`vendor/chudcode/CHUDCODE_ARTIFACT.lock` — re-verify it when the lock moves.

## Authority boundary

- Chudcode owns online agent turns and nested agents: their provider calls, auth, model
  catalog/selection, persistent session history, branching, memory, compaction, retry/cache,
  browser/computer eval preludes, and STT.
- Chudware owns CAD tools and schemas, documents/workspaces, roles, councils, mission/DAG
  policy, delegation governance, approvals, guardrails, PLM, event presentation, and the
  offline-safe native local-modeling fallback.
- `chudcode --mode rpc` protocol v2 is the sole embedding protocol. Never add a second online
  agent/provider loop, an external MCP hop, or a runtime source/package install.
- ECAD planning and regeneration diagnosis use the Provider-shaped
  `providers/chudcode_provider.py` adapter and `ChudcodeSessionHost.complete_text`.
  Online tool calls inherit their owning turn's hidden model/credential factory
  (cell helpers inherit the principal-bound broker grant, never BYOK key bytes);
  private clients have no tools, skills, rules, memory or saved CAD session. Web actors
  never fall back to desktop credentials. Explicit offline/local helper providers remain.
- Helper calls have a positive finite budget (default 180 s), cancellation polling and
  bounded process cleanup; this is not a strict end-to-end deadline. ECAD also retains
  its separate planning budget. RPC v2 has no per-request output-token cap: do not claim
  `max_tokens` is enforced by this adapter. Ordinary conversations have no implicit cap.
- Web actors never use ambient `chud` auth. Tenant cells run only gateway-validated
  BYOK for Anthropic/OpenAI/xAI/OpenRouter/Gemini. The gateway-owned
  `Webservice/gateway/provider_broker.py` keeps key bytes on the host; the cell gets
  only a `cpg1` grant bound to cell/workspace/actor/UID/epoch/right/team/provider/source/role,
  through private `provider.sock` and actor port `127.0.0.1:18081`. The broker
  revalidates live authority during transfers and resolves the newest rotated key.
  Pool/subscription/sponsor turns still refuse `credential_isolation_unsupported`:
  the current auth broker is BYOK-only. Cell credential identity is
  (workspace, actor, provider), never the key, so rotation keeps cancel/resume working.
  Chud launches through `chudware-actor-sandbox` (`ro` for Ask/Plan, `rw` for Agent);
  distinct public-HTTPS CONNECT research egress uses `127.0.0.1:18080`, without
  provider credential injection. Browser/computer overlays are off. Managers may
  only stop another actor's run (`ctx.control_delegate`). Desktop turns use the host login.

## Topology

```text
host GUI / hostd → core/agent/run.cpp → persistent services worker (core/hostd/worker_bridge.cpp)
  → services/worker_main.py::agent_admin_turn → services/agent/worker_rpc.py::_agent_admin_turn
  → services/agent/chudcode_host.py::ChudcodeSessionHost → chudcode_rpc.RpcClient → chud --mode rpc
```

Browser `/engine` enters the workspace-owned tenant cell through
`Webservice/gateway/engine_bridge.py` / `cells.py`, then its actor-owned worker;
it never reaches the optional shared local/native daemon. Successful local provider
wire/SDK fixtures prove the observed stream/session bytes, not genuine provider
signatures/cache hits, production readiness or release authority.

The long-lived worker's process-global `ChudcodeSessionHost` owns one persistent RPC process
per Chudware conversation. `online_turn.py` owns workspace/model/settings assembly and
run-scoped environment lifetime. Ordinary calls use the worker's serialized stdio lane;
Stop/steer/status/answer use `chudcode_control.ControlServer`, whose endpoint/token is exposed
only via `worker_control_info` (mode-0600 Unix socket on POSIX; owner+SYSTEM,
remote-rejecting named pipe on Windows, never TCP). Never log or echo the token,
never move control back onto stdio. Windows transport qualification requires a
genuine Windows run; Linux fixtures do not attest Windows ACLs. Malformed,
oversized (request 64 KB), repeated-auth and oversized-response paths stay bounded — so
steer images travel as paths, never inline bytes.

| File | Responsibility |
|---|---|
| `services/agent/chudcode_host.py` | RPC clients, session LRU, turn config, cancel/steer/status/answer, auth/memory/session calls |
| `services/agent/chudcode_control.py` | Bounded authenticated control server |
| `services/agent/chudcode_tools.py` | Live CAD registry → run-scoped host tools, guarded dispatch, result envelope |
| `services/agent/chudcode_events.py` | Deterministic bounded RPC-frame → feed-event mapping |
| `services/agent/chudcode_questions.py` | Blocking `ask_user` batches |
| `services/agent/online_turn.py` | Turn assembly, settings, run-scoped workspace/credential lifetime |
| `services/agent/progress_watchdog.py` | Research/no-mutation detection and bounded steer |
| `services/agent/subagent_registry.py` | Evidence-based sub-agent liveness, project-scoped roster |
| `services/agent/chudcode_roles.py` | CAD role/prompt conversion, `enable_tools` refresh |
| `services/agent/worker_rpc.py` | Public worker methods, settings/auth/memory/session seams |
| `services/worker_main.py` | Worker entrypoint, control-server lifetime, global Chudcode shutdown |
| `services/worker/worker_client.cpp` | Native transport, online/offline split, control-lane client |
| `core/agent/run.cpp` | Native entry and online handoff; offline-safe fallback only |
| `vendor/chudcode/CHUDCODE_ARTIFACT.lock`, `scripts/pin_chudcode.py` | Compiled release identity; pin/check/verify/stage/fetch |
| `assets/ui-shared-manifest.json` | `chud-ui-shared-v1` vocabulary, byte-identical to Chudcode's |

## Turn routing

- No classification step: a chat send is ONE `general_agent` turn with the user-picked mode
  (Agent | Plan | Ask, `CopilotMode` 0/1/2) and `options.scope = {doc_id, feature_ids,
  selection}` when a document is open (rendered by `worker_rpc._editor_context_block`). The
  composer never keyword-routes, never creates/replaces a document (the agent's `new_part`
  decision), never inlines the transcript (`options.history` seeds a fresh session once). Do
  not reintroduce GUI `task_route`/`agent.classification` consumers or a Routing card.
- Offline: general work refuses without an online provider; offline-safe local modeling may
  use the native C++ pipeline (with its own stage classification). Explicit mission/DAG policy
  stays CAD-owned; its agent turns run through Chudcode.
- The main chat MUST send `workspace_dir` (`composer.cpp attachAgentConversationOptions`): it
  is the `fs_*` jail (`CHUDWARE_FS_ROOT`), project-save root and Chudcode cwd; without it
  `fs_*` refuses `workspace_root_unavailable`.
  `resolve_workspace` canonicalizes that exact selected directory, never widens a
  nested selection through ancestor-project discovery.

## Session and lifecycle rules

- Chudware `session_id` is the live key. `chudcode_session_file` may seed a new process and
  cannot change while it is live. Capacity: `CHUDWARE_CHUDCODE_MAX_SESSIONS` (8, max 32);
  evict only the LRU idle session, else refuse.
- `RpcClient(max_event_history=CHUDWARE_CHUDCODE_EVENT_HISTORY)` defaults to 1e6 frames;
  nothing in the lane may silently cap an hours-long turn.
- CAD sockets belong to the owning turn, not transient RPC tool-call threads:
  `TurnConnections` in `daemon_bridge.py`, installed in `chudcode_host.run_turn` and leased
  by `chudcode_tools.dispatch_host_tool`. Parallel calls use different sockets; nested calls
  reuse their lease. End-of-turn cleanup retires idle sockets and lets in-flight calls finish.
  Never detach documents after each tool completion or collapse parallel calls onto one socket.
  Sub-agents share the project registry and explicitly name doc_id; no workspace/part copies
  are required. Same-part batches serialize atomically, not as a semantic feature merge.
- `run_id` is unique while active. Cancel before prompt is a valid cancellation; after prompt
  it sends `abort()` once. Duplicate cancel and identical steer are idempotent; pre-start
  steers queue until right after `prompt()`. A steer carries composer attachments/context refs
  like a fresh turn (`agent_steer` → `chudcode_host.steer`).
- **Steers are append-only and tracked to delivery** (c252: a steer sat 2m41s behind a 900 s
  `write xd://wait_job`, Stop then let Chudcode's settle drain answer it in a turn nobody
  watched). A user steer stays in `_Run.undelivered` until its user `message_start` streams
  (`agent_steer {steer_id, status:"delivered"}` → `agent.steer`). Meanwhile tools declaring
  `wake` (host-injected like `cancel`; `wait_job`) end early with `woken_by` — never real work.
  Stop calls `remove_queued_message` BEFORE `abort()`; withdrawn steers (`carried_steer_ids`)
  and ones still queued at the terminal (`carried`) open the session's next prompt. Never
  `abort_and_prompt` or abort to deliver a message: that drops the in-flight response.
- **Delivery needs positive evidence.** A text-only steer consumed by `ask_user` emits
  `agent_answer {source:"reply", steer_id}`; the mapper produces the correlated
  `agent.steer delivered` even if the control response is lost. Image replies retain their
  original text/images and require matching streamed evidence. Unknown user frames and Stop
  never acknowledge all queued messages. GUI delivery is monotonic; legacy reload reconciles
  only exact, unique, image-free question replies within their own turn (`steerstatus` shot).
- **Control endpoints live as long as their worker generation.** `WorkerBridge` and
  `WorkerClient` retain credentials after an individual control refusal/timeout/protocol
  failure; clearing them strands Submit and Stop behind busy stdio (ESPCAM c261).
  Hostd snapshots credentials under the control mutex, then releases it before socket I/O.
  Teardown/replacement invalidates the endpoint; failed requests are never blindly replayed.
- A turn requires terminal `agent_end` after `wait_for_idle`; idle without it is a protocol
  error. Event-forwarding errors fail the turn.
- **No implicit wall-clock cap.** The GUI sends no `timeout`; native transports use `-1`.
  Missing/zero timeout waits in finite 30 s slices (`RpcClient.wait_for_idle(None)` is a 60 s
  default, not unlimited) so Stop and process death stay observable. An explicit positive
  timeout bounds the run: expiry aborts once, waits ≤30 s for the terminal, returns
  `stop_reason:"turn_budget"` (GUI amber card, not System error). Clients add reporting slack
  (GUI +120 s, hostd +60 s). A confirmed abort keeps history for continue; an unconfirmed abort
  disposes the connection. `run_mission.py --timeout-s 0` is unbounded. Only an explicit
  budget is announced: `online_turn.turn_budget_notice` adds one turn-start line, and
  `progress_watchdog.TurnBudgetReminder` sends one steer (`agent.progress_nudge`,
  `reason:"turn_budget"`) at min(80 %, budget − 180 s), or 80 % when that is ≤30 s in, to save
  deliverables then verify (Ask/Plan: answer, no "save"); it is outside rule 15's two-nudge
  cap and cancelled at the terminal.
- `agent_end` is not success: `_unrecovered_provider_failure` returns
  `ok:false, stop_reason:"provider_error"` when the last assistant record is a failed retry
  (`auto_retry_end success:false`) or a `message_end` with `stopReason:"error"`;
  `run_mission.py` exits non-zero on it. `worker_rpc` forwards `stop_reason` on failures.
- A failure after streamed prose is APPENDED to `ChatMsg::text`; the error card lists only tool
  failures the turn never got past (`splitToolRowDetail`).
- OAuth balancing is sticky on the Chudware session id, not resumed history: to escape an
  exhausted account, resume the same `.jsonl` under a new `--session-id`.
- `chudcode_close_session` is idempotent: mark runs cancelled, abort, dispose, keep history.
- hostd never replays an `agent_admin_turn` whose worker died after receiving it
  (`worker_lost_mid_turn`); "continue" resumes from the bound session file.
- Shutdown follows ownership: `worker_bridge.cpp` owns the worker; `worker_main.stop_services`
  closes the control server, then `chudcode_shutdown()` disposes all saved-session and
  transient clients. Process-tree termination stays bounded; no RPC process outlives its worker.
- **Deployment census is work authority, not cached-session count.**
  `ChudcodeSessionHost.worker_activity` positively probes SDK settle/queue/STT and
  process ownership; a cached idle SDK alone does not pin, unavailable evidence does.
  Native `cell_census` independently counts non-control sessions, RPCs, dirty docs,
  cache-verification/profiler tasks and busy/background/unknown workers. The gateway's
  durable private maintenance lease closes new admission without cancelling accepted
  work, generation-fences cell self-exit, then requires actual manager inactivity before
  cutover. Operator probe/wait deadlines never terminate agent work or turn unknown into
  zero. Postmutation failure retains maintenance; only same-lease protected recovery
  with inactive cells and authenticated health may reopen. The optional shared native
  daemon is never a participant; no bridge-only or forced-drain fallback exists.
- Nested agents stay inside Chudcode; session/subagent/retry/compaction/todo/usage events map
  into the existing feed. Never synthesize success when Chudcode reports failure.

## Recovery (service boundary, never file surgery)

- `chudcode_export_session {session_id,output_path?}` → live HTML export (default: project
  `chat-exports`).
- `chudcode_branch_session {session_id,entry_id}` → real branch at a real entry id.
- `chudcode_rewind_session {session_id,user_text,occurrence,…}` forks before an edited user
  message on the active branch; resolve the occurrence via runtime `get_entries`, never a
  JSONL scan. The original file survives; the exact old process retires before a queued turn
  can claim it. The GUI stops and awaits the actual terminal, rewinds runtime then geometry,
  persists the new link, then dispatches one edited turn; failure keeps the original link,
  and an asynchronous rollback never races the replacement turn.
- `chudcode_repair_session {session_id,repair}` → bounded doctor summary; `repair:true` runs
  Chudcode's backup-producing `/session doctor --repair`.
- Never hand-edit session JSONL, invent an entry id, or report a repair the doctor did not do.

## Host-tool rules

1. `chudcode_tools.build_host_tools` derives names/descriptions/schemas/handlers from the live
   CAD MCP/mission registries — an adapter, never a second catalog. Skip unbound and async-only
   entries; Chudcode builtin names stay Chudcode-owned unless a CAD entry claims them.
   Model-facing selectors sharing a harness context name declare `@model_selectors(...)`
   (`services.mcp.tool_registry`); context parameters stay injectable.
2. Allowlist tri-state: `None` unrestricted, `[]` live floors only, non-empty = restriction.
   `_effective_names` order: allowlist → on-demand loads (`tool_categories.resolve_tool_requests`)
   → mode policy (Ask/Plan read-only whatever was loaded; Plan = agent set minus mutators and
   fs_write/fs_edit/run_shell) → meta/bug-report floors → `verify_battery` when mutators exist.
   Advertise and dispatch exactly that set; `_mode_refusal` re-applies the mode at dispatch.
3. The main chat advertises `tool_categories.MAIN_CHAT_CORE_TOOLS` (incl. sourcing, jobs,
   `browse_models`, `print_media_inventory`/`publish_print_media`) plus a MORE TOOLS menu;
   `enable_tools(tools=|categories=)` loads the rest via
   `chudcode_roles._register_category_refresh` and reports unknown tokens. Every tool
   registration goes through `chudcode_host.publish_host_tools`, which also sends
   `loadable_tools` (catalog minus mounted) so Chudcode ≥0.4.3 answers `xd://<unloaded>`
   with the enable_tools step. `run_shell` stays
   menu-only; do not move the whole registry into core. `browse_models` contact sheets and
   `fs_read` of a CAD file show real renders (thumbs share the GUI PLM cache), never stand-ins.
4. Every call crosses CAD chokepoints: policy refusal → cancellation → mission/run dispatcher →
   scaffold/guardrail checks → `agent_turn._dispatch_tool` accounting/loop guard. Preserve
   document/run/actor context injection and progress/cancel callbacks; `_reconcile_kwargs`
   maps synonyms and returns teaching errors (no cap charge), never a raw `TypeError`.
   Bind selected `project_dir`/`save_dir` before turn activation; fresh SDK threads and
   refreshes retain these explicit roots, never ambient tenant authority.
   `_ToolCallRecovery` belongs to the run, not accounting dictionaries: exact retries
   join pending/unconsumed work without spending another slot. Document creation must
   not change its recovery key. Execution settles/refunds once before publishing its
   terminal result. Close recoveries from both context and binding metadata at turn end;
   stale callbacks refuse `run_ended`, while already-running native work may finish.
5. Refusals keep machine-readable shapes; tool exceptions propagate as tool errors; success is
   finite JSON-safe data, never fabricated. A CAD `{"ok": false}` is a normal return:
   `chudcode_events.cad_result_payload` fails the card with the tool's own error, and the
   watchdog does not count it as a mutation.
6. Every return value and progress update crosses as `{content:[{type:"text",text}], details}`
   via `host_tool_result` (applied once in `HostToolBinding.to_host_tool`); a result without
   `content` never resolves and hangs `write xd://` and `wait_for_idle`.
7. Results stay parseable and bounded: JSON-text returns parse into `details`; text is capped
   at `_RESULT_TEXT_BYTES` (46 000 B) minus any skill prefix by halving the largest member
   (`_shorten_largest`, which ends each cut string with a `…[cut: …]` marker), with
   `_truncated{full_result,…}` naming the full JSON spilled by `_spill_full_result`.
   Plain non-JSON guide/skill text also supplies `details` as a string; oversized
   text uses `{items, _truncated}` with a full JSON-string spill. Consumers await
   `tool.write`, check outer `hasError`, then read `details.xdev.inner` as dict|string.
   A dict with `ok:false` is a refusal, not permission to read success-only keys.
   A reporting exception never justifies replaying an already-successful mutation.
   `fs_read` pages (`byte_offset`/`next_byte_offset`, ~32 KB) so it never reaches the cap.
   Image tools cross as ONE mapping (`tool_images.image_result`: `{ok, caption, images,
   saved_path?}`) in text and details; the image blocks stay in `content`.
8. CAD skill packs reach the model only through `cad_skill(name, path)` (in
   `MAIN_CHAT_CORE_TOOLS` and `META_TOOL_FLOOR`), never `skill://` (Chudcode's own registry);
   `mission_dispatch.role_prompt` (online: main chat, delegated roles, prompt preview/baseline)
   and offline-only `linear_role_prompt` share one composition that appends
   `skills.catalog_block()`. Trigger injection
   (`_skill_prefix`/`_prefixed`) rides the first matching result per `session:agent_id`, never
   on eval-bridged calls (`_eval_bridged`). Never copy a SKILL.md into a role card.
   Descriptions carry effective curated `prompt_note`, timeout/recovery policy and
   matching built-in/project-auto `cad_skill` pointers even when eval suppresses the
   result prefix; preserve this suffix when trimming the description. Always-denied
   agent `undo`/`redo` stay out of discovery as well as dispatch.
9. Desktop transient commands (auth, providers, catalog, memory, login) share one
   long-lived no-session client (`_service_client`, warmed by
   `worker_main._warm_chudcode_service`, serialized on `_service_lock`, replaced
   on death or memory-backend change, disposed on shutdown). Never spawn per
   desktop call. Tenant cells refuse this ambient service. Their model catalog
   uses `cell_catalog_models`: an actor/provider-capability-scoped, metadata-only
   SDK helper registered in the execution census and disposed after discovery.
   It does not create/resume a conversation or inherit desktop credentials.
10. Session processes are lean: `_make_client` passes `--tools <CAD_SESSION_TOOLS> --no-skills
    --no-lsp`, `--no-mcp` when `_binary_supports_flag` sees it, and the immutable
    content-keyed `_cad_session_overlay` (`--config`), which disables user-level context files
    (`context-file:user:*`; project context files still load), pins the GUI memory backend
    (`memory_backend`: off | sharpshooter | mnemo → `mnemopi` per-project; no workspace ⇒ `off`;
    a binary without sharpshooter settings falls back to Mnemopi), and enables
    `browser`/`computer` eval preludes. `--tools` carries only the selected backend's
    `_CAD_MEMORY_TOOLS` (chud exits on a tool it lacks). Chudcode's `<active-repo-context>`
    cannot be disabled; `general_agent.yaml` HOW TO WORK overrides it. Orchestration uses
    `task`/`wait` + `agent://`/`history://`/`proc://`.
11. **The Compaction choice governs.** `online_turn` maps Summary→`methodOrder:[soft]`,
    Shake→`[shake]`, Snapcompact→`[snapcompact]`, Off→`enabled:false`; Automatic leaves runtime
    policy. Never substitute a default. Overlays are immutable and content-keyed; never rewrite
    a shared overlay for another session. A changed choice refreshes only an idle connection
    at the next turn or manual compact, resuming the exact history (eval scratch state resets);
    explicit auto-compaction/retry switches survive; manual compact reports the committed method.
11a. **Chats run turns concurrently; a conversation lives in ONE worker.** hostd runs each
    turn in its own pool worker and keeps conversation→worker ownership. A worker's
    `ChudcodeSessionHost` may cache many conversations, but another worker resumes one only
    after the owner's control action `release` (`release_session`: refuses `session_busy`,
    never cancels) disposed it. Never keep two live runtimes of one session: disposing the
    stale one writes `session_exit` at its old leaf and the next resume forks from there.
12. Eval-made CAD calls get feed rows: `run_turn` binds `host_call_observer`
    (`_bind_host_call_observer`); `_nested_call_reporter` emits `host_tool_start/_end` only for
    ids no in-flight tool frame owns.
13. Task sub-agents inherit host tools over the same client with `agent_id`/`parent_agent_id`;
    explicit agent `tools:` allowlists narrow them, restricted/plan children get none, and
    aborting a sub-agent sends `host_tool_cancel`. Sub-agent calls run concurrently: cap slots
    reserve under `_CALL_ACCOUNTING_LOCK` (refunded if never run), records append under
    `_RECORDS_LOCK`, `enable_tools` refreshes serialize. Each call runs inside
    `cad_common.agent_doc_context` (run/session + agent id): each sub-agent has its own current
    part, a doc-less sub-agent call targets that part before the turn doc (`_document_args`), and
    its engine calls use its own thread's hostd connection (one `writer_group` per worker).
    Default `subagent_subscription="events"`
    maps lifecycle to one upserted `sub_run` per sub-agent and `subagent_event` tool frames to
    `agent.tool_call` (`subagent_id`, parent = the `task` call), dropping child text.
    Subscription lifecycle/progress `payload.id` is the assigned task handle (including
    `Parent.Child`), not the generic `agent` definition type. The mapper keeps `name`,
    `agent_type` and late `description` separate; progress/terminal/spawn events never
    downgrade a named trace agent. Persist the assigned-name priority with the DAG.
    Tool-result image references and captions also reach the parent feed (direct CAD
    results and nested `xd://` results), even for failed inspections; never copy base64
    into feed events or manufacture an image for a numeric-only geometry result.
    `general_agent.yaml` PARALLEL SUB-AGENTS: layout contract first, one document per
    sub-agent, parent integrates and mates.
14. `ask_user` asks the human when the composer sends `interactive_questions: true`:
    `chudcode_questions.TurnQuestions.ask` is injected via `_HOST_INJECTED_PARAMS`, emits
    `agent.question`, blocks with no timeout, and resolves from control `answer`, a user steer
    (`from_user=True`), or Stop; each end emits `agent.answer`. The watchdog holds while a
    batch is pending. Headless turns keep the non-blocking default.
15. `ProgressWatchdog` (owned by `run_turn`): after 12 research calls or 6 min without a
    successful CAD mutation, steer once per stall, max twice per turn; `read xd://` is not a
    mutation; a sub-agent's mutation counts, its research does not; Ask/Plan/`plan_only` exempt.
    `general_agent.yaml` steers multi-part mechanisms to `design/layout.md`, one saved part at
    a time, mates at every joint, then `simulate_motion`.
16. Sourcing/research honesty: `hobby_servo` catalog + `output_axis` (`core/parts/`,
    `parts_make_impl`); `fetch_cad_model_impl` stores file + provenance under
    `<project>/reference/`; MakerWorld/GrabCAD may return `login_required`;
    `research_fetch_page` retries JS shells (`js_shell`); search distinguishes
    `provider_blocked` from `no_results`. Neither is an `ok:true` empty result.
17. `subagent_registry.list_sessions(project_dir=…)` returns only records stamped for that
    project; records turn `abandoned` only on owner PID/start-time or heartbeat evidence.
    Explicit project roots (`CHUDWARE_PROJECT_DIR`, then `CHUDWARE_FS_ROOT`, then the
    supplied project directory) are canonicalized without widening to an ancestor
    repository marker. List filters intersect before limiting. Transcript (display
    and raw), steer, terminate and control accept optional `project_dir` /
    `session_id` filters and refuse out-of-scope records before reads or mutations.
    An explicitly blank filter matches nothing; omitted/null filters preserve
    unscoped callers. The GUI supplies project and owner-session scope for inspection
    and controls.

## Event rules

`chudcode_events.map_rpc_frame` with per-turn `RpcFrameState` is the translation authority.

- Only streamed deltas are visible assistant text; message-complete envelopes never replay it.
  Preserve `text`/`thinking` order; an empty text flush is not a thinking boundary.
- Unwrap `custom{customType}` first: title/thinking/notice/command frames are metadata.
- Tool events correlate by Chudcode call id: start/progress → status; terminal result/cancel →
  one `agent.tool_call` with bounded previews; a synchronous host call with a result is
  terminal; `write xd://` names its target from content, `read xd://` only inspects docs. A
  failed builtin's reason (`tool_execution_end.result.content[].text`) travels as `error_preview`.
- Right after `agent_start`, a `session_bound` frame maps to `agent.session`; the GUI persists
  `ChudcodeSessionLink` immediately so a mid-turn hostd death keeps the resume key.
- Preserve correlation (`run_id`, `session_id`, node/span/parent/subagent ids, `messageId`,
  nested `subagent_event.payload.event` lineage); synthesize only deterministic digest ids.
  Unknown frames become bounded deterministic `sub_run` rows, never dropped or upgraded to a
  guessed event. `prompt_result`, `session_settled`, `queue_update` are bookkeeping;
  `agent_end.isTerminal:false` stays nonterminal even with `yielded`. Todo, retry,
  compaction, fallback, spawn/subagent and terminal events keep their existing feed shapes.
- Auto-compaction start/end pair via `RpcFrameState.compaction_id` into ONE upserted `sub_run`
  (speculative compaction emits no frame and needs no row); a repeated assistant `start` is a
  "Model response restarted" retry row, never an unknown row. The GUI shows elapsed time on
  running rows and a silence line after 5 s of no stream.
- Provider usage maps to both `agent.provider_usage` and `agent.context`; the meter speaks only
  the frozen `{used, window, window_source}` contract, fed from assistant `message_end` usage
  (`calculateContextTokens`) and `get_state().contextUsage` after `agent_start`/`agent_end`.

## Auth, memory, settings

- Desktop service calls go through `transient_call` on the shared client; its
  overlay pins the GUI-selected memory backend so `memory_*` use the same
  project backend as sessions. Tenant cells instead use the scoped catalog
  bootstrap above; legacy provider credential mutation is refused, not forwarded
  into another actor's service. `session_call` only targets an already-live
  session; a status/settings request never creates or resumes a conversation.
- Secrets stay in typed `chudcode_rpc` wrappers; UI responses expose state, account labels,
  usage and model metadata, never credential bytes. Login requests are bounded and allow only
  safe web URLs into `extension_ui_request`.
- `chudcode_models_list` rows carry `reasoningEfforts` and `vision`; the composer's effort menu
  reads them. `refresh_catalog_if_stale` re-discovers when the `chud` binary identity changes.

## Binary and release

Lookup order: constructor hint → `CHUDWARE_CHUDCODE_BIN` → `chud`/`chud.exe` beside the packaged
Python → developer `PATH`. Production uses the signed-payload-adjacent binary. The lock
(`chudcode-artifact-lock-v1`) records version, commit, RPC protocol 2 and URL/SHA-256 for
`linux-x86_64`, `macos-x86_64`, `windows-x86_64`; null fields fail packaging closed. Pin all
three atomically. Linux/macOS come from the Chudcode release; Windows is the native build
from `chudcode-runtime-windows-build.yml` (the cross-compiled Windows CLI crashes in Bun
1.4.0). Chudcode release CI moves files through the tag's draft release, never Actions
artifacts; ARM builds are manual and never block a release.

```bash
python3 scripts/pin_chudcode.py check-lock
python3 scripts/pin_chudcode.py verify --target linux-x86_64 --input /secure/chud-linux-x86_64
python3 scripts/pin_chudcode.py stage --target linux-x86_64 --input /secure/chud-linux-x86_64 --output /tmp/chud
```

`stage` is offline, verifies target/digest/version before and after copy, and publishes the
canonical filename atomically; only explicit `fetch` touches the network. Retained-release
recovery: `--lock <release>/CHUDCODE_ARTIFACT.lock verify-notices --source-dir <release>`, then
`verify --target <host-target> --input <release>/bin/chud`. Global `check-lock` is a
publication gate, not a per-host restorability check; notice hashes stay mandatory.

## Shared UI vocabulary

`assets/ui-shared-manifest.json` is byte-identical to Chudcode's and declares
`chud-ui-shared-v1` (palette, tone/status vocabulary, usage thresholds, font provenance, small
icons). No shared runtime state or widget code; Chudware's C++ UI stays authoritative for
CAD-specific state.
