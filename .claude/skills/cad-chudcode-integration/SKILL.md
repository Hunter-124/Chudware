---
name: cad-chudcode-integration
description: "Extend or debug Chudware's compiled Chudcode RPC integration: persistent sessions, host tools, control, auth, memory, events, recovery, and release artifacts"
---

# Chudware ↔ Chudcode integration

Use this skill for online agent execution, provider/auth/model settings, session persistence or
recovery, memory, compaction/retry, browser/computer, STT, streamed events, host-tool dispatch,
or the embedded Chudcode release artifact.

Read `.claude/skills/chudware-dev/SKILL.md` first for the repository map and global lifecycle
rules. Contracts are append-only. The frozen material under `docs/omp-integration/` is dated
provenance; only its 2026-08-31 superseding section is operational.

## Authority boundary

- Chudcode owns every online top-level and nested provider call, auth, model catalog/selection,
  persistent session history, branching, memory, compaction, retry/cache behavior,
  browser/computer eval preludes, and STT.
- Chudware owns CAD tools and schemas, documents/workspaces, roles, councils, mission/DAG
  policy, delegation governance, approvals, guardrails, PLM, event presentation, and the
  offline-safe native local-modeling fallback.
- `chudcode --mode rpc` protocol v2 is the sole embedding protocol. Do not introduce another
  online provider loop, an external MCP hop, or a runtime source/package install.

## Current topology

```text
host GUI / hostd
  -> core/agent/run.cpp
  -> persistent services worker (core/hostd/worker_bridge.cpp)
  -> services/worker_main.py::agent_admin_turn
  -> services/agent/worker_rpc.py::_agent_admin_turn
  -> services/agent/chudcode_host.py::ChudcodeSessionHost
  -> chudcode_rpc.RpcClient
  -> compiled chud --mode rpc
```

`services/worker_main.py` is long-lived. Its process-global `ChudcodeSessionHost` owns one
persistent RPC process per Chudware conversation. `worker_rpc._agent_admin_turn` coordinates
the online request; `online_turn.py` owns workspace/model/settings assembly and run-scoped
environment lifetime. CAD role prompt, effective host tools, callbacks and event context
reach the Chudcode host without a second provider loop.

Ordinary calls and event notifications use the services worker's newline-delimited stdio
protocol. Stop/steer/status cannot wait behind a blocked turn, so `worker_main` also starts
`services/agent/chudcode_control.py::ControlServer`. `worker_control_info` returns its private
authenticated endpoint: mode-0600 Unix socket on POSIX, random loopback TCP on Windows. Never
log the token or move control back onto the serialized stdio lane.

## Files by responsibility

| File | Responsibility |
|---|---|
| `services/agent/chudcode_host.py` | Persistent/transient RPC clients, session LRU, turn configuration, cancel/steer/status, typed auth/memory/session calls, attachments |
| `services/agent/chudcode_control.py` | Bounded authenticated control server and platform endpoint |
| `services/agent/chudcode_tools.py` | Live CAD registry → run-scoped Chudcode host-tool definitions and guarded dispatch |
| `services/agent/chudcode_events.py` | Deterministic bounded RPC-frame → Chudware feed-event mapping |
| `services/agent/online_turn.py` | Worker turn-assembly helpers and run-scoped workspace/credential lifetime |
| `services/agent/progress_watchdog.py` | Per-turn research/no-mutation detection and bounded steer |
| `services/agent/subagent_registry.py` | PID/start-time/heartbeat liveness reconciliation and project-scoped roster |
| `services/agent/chudcode_roles.py` | CAD role/prompt conversion at the Chudcode boundary |
| `services/agent/worker_rpc.py` | Public worker methods, online turn assembly, settings/auth/memory/session seams |
| `services/worker_main.py` | Persistent worker entrypoint, control-server lifetime, global Chudcode shutdown |
| `services/worker/worker_client.cpp` | Native worker transport, online/offline route split, control-lane client |
| `core/agent/run.cpp` | Native top-level entry and online handoff; offline-safe fallback only |
| `vendor/chudcode/CHUDCODE_ARTIFACT.lock` | Compiled release identity for all supported targets |
| `scripts/pin_chudcode.py` | Pin/check/verify/stage/fetch the compiled artifact |
| `assets/ui-shared-manifest.json` | Checked-in `chud-ui-shared-v1` vocabulary copied byte-for-byte from Chudcode |

## Session and lifecycle rules

- Chudware `session_id` is the live host key. A `chudcode_session_file` can seed a new live
  process and cannot change while that process is live.
- Capacity is bounded by `CHUDWARE_CHUDCODE_MAX_SESSIONS` (default 8, maximum 32). Evict only
  the least-recently-used idle session; if all are active, refuse the new session.
- `run_id` is unique while active. Cancel before prompt returns a valid cancelled result;
  cancel after prompt sends `abort()` once. Duplicate cancel and identical steer are
  idempotent. Pre-start steering queues until immediately after `prompt()`.
- A turn must observe terminal `agent_end` after `wait_for_idle`; idle without it is a protocol
  error. Event-forwarding errors fail the result rather than hiding a broken UI boundary.
- Online turns have no implicit time limit. Missing/zero `timeout` waits in 30 s slices:
  `RpcClient.wait_for_idle(None)` is a 60 s default, not an unlimited wait. A positive
  caller `timeout` bounds the RUN: expiry aborts once, waits 30 s for the terminal,
  then returns `stop_reason:"turn_budget"`. A confirmed abort leaves history live for
  continue; an unconfirmed abort disposes the connection so unseen work cannot survive.
  `run_mission.py --timeout-s 0` is unbounded; native transport timeout `-1` is unbounded.
- `agent_end` is not success. `chudcode_host._unrecovered_provider_failure` returns
  `ok:false, stop_reason:"provider_error"` when the turn's last assistant record is a failure:
  a failed retry (`auto_retry_end success:false`) or a non-retryable rejection, which Chudcode
  records only as an assistant `message_end` with `stopReason:"error"` + `errorMessage` (e.g.
  a 400). Missing the second shape made a rejected Opus 5.5 turn render as "The agent
  finished without reporting what it did" (2026-09-24).
- `chudcode_close_session {session_id}` is idempotent: mark active runs cancelled, abort live
  work, dispose the process, and preserve persisted history.
- On worker exit, stop the control server before disposing all saved-session and transient
  clients. Do not leave an RPC process detached from the worker that owns it.
- hostd never replays an `agent_admin_turn` whose worker died after receiving it
  (`worker_bridge.cpp`, `worker_lost_mid_turn`): a replay is a second, unwatched run in a new
  Chudcode session (c221 → c223, 2026-09-27). "continue" resumes from the bound session file.

## Recovery/export worker methods

- `chudcode_export_session {session_id,output_path?}` calls live Chudcode HTML export and
  returns `{ok,path}`. Without `output_path`, use the project's `chat-exports` directory.
- `chudcode_branch_session {session_id,entry_id}` creates a real branch at the specified entry
  and returns `{ok,chudcode_session_id,chudcode_session_file}`.
- `chudcode_rewind_session {session_id,user_text,occurrence,session_file?,workspace_dir?}`
  forks **before** an edited user message on the active runtime branch. Resolve the
  zero-based occurrence through the runtime's `get_entries`, never an append-order JSONL
  scan. The original file is preserved; success returns the child session identity/file
  and retires that exact process before a queued turn can claim it. A closed runtime
  resumes only the explicit file, without a provider call. The GUI stops and waits for
  the actual terminal, rewinds runtime then geometry, parks the old visible tail, persists
  the new link, and only then dispatches one edited turn. Failure keeps the original
  transcript/link; an asynchronous rollback must never race the replacement turn.
- `chudcode_repair_session {session_id,repair:false}` returns a bounded doctor summary.
  `repair:true` invokes Chudcode `/session doctor --repair`, including its backup workflow.
- `chudcode_close_session {session_id}` disposes runtime state without deleting the persisted
  file.

Never hand-edit session JSONL, invent an entry id, or report a repair that did not come from the
runtime's doctor command.

## Host-tool rules

1. Build tools from the live CAD MCP/mission registries with
   `chudcode_tools.build_host_tools`; it is an adapter, not another catalog.
2. Preserve allowlist tri-state: `None` unrestricted, `[]` restricted to live floors,
   non-empty list ordinarily restricted. Order in `_effective_names`: role/explicit allowlist
   → union on-demand loads (`enable_tools` tokens via `tool_categories.resolve_tool_requests`)
   → mode policy (so Ask/Plan stay read-only whatever was loaded) → meta/bug-report floors →
   `verify_battery` when mutating tools are present → render schemas.
2b. The main chat (`worker_rpc._agent_admin_turn`) advertises
   `tool_categories.MAIN_CHAT_CORE_TOOLS`, now including `part_search`, `part_get`,
   `part_source`, `match_model`, `make_standard_part`, `fetch_cad_model`,
   `import_artifact` and `simulate_motion`, plus (2026-09-28, the tools c211–c233 loaded on
   demand or bypassed through bash) `inspect_native_files`, `read_native_file`, `get_job`,
   `wait_job`, `inspect_section`, `dfm_check`, `analyze_stl_geometry`, `list_world_bodies`,
   `interface_check` and `render_animation`, and `browse_models` — the model-facing thumbnail
   explorer: labelled contact sheets (4×3 numbered tiles, ~1.5k image tokens per 12 models)
   of real renders. `model_thumbs` renders the file's own display mesh (`cache.aicm`/STL,
   `mesh_preview`: engine iso camera, no document session) and falls back to the engine's
   silent `doc_open(internal)`→`doc_render_png`→`doc_close` for STEP/IGES/reference
   assemblies, caching in `<project>/.plm/thumbs/<content_key>.png` — the GUI PLM grid's own
   cache (key = cad_graph `meta.dh`). `fs_read` of a CAD file returns its card + one
   labelled render (`ok:true`); `fs_list` rows carry the one-line summary. Since 2026-10-03
   the core also carries `print_media_inventory` (read) and `publish_print_media` (mutating,
   contract `mutating_tools`, so Ask/Plan refuse it): printer-stick delivery is part of
   "print this", and without them agents shell-copied G-code beside stale revisions. Its
   system prompt carries the MORE TOOLS menu
   naming every other registered tool; `enable_tools(tools=[...])` and
   `enable_tools(categories=[group])` load the rest mid-turn through the single refresh
   callback `chudcode_roles._register_category_refresh` (re-runs `build_host_tools` and
   `set_custom_tools`, reports `unknown` tokens). The composer's `scope`
   (`{doc_id, feature_ids, selection}`) is rendered by `worker_rpc._editor_context_block`.
3. Advertise and dispatch the same effective set. Skip unbound and async-only entries.
   Chudcode builtin names remain Chudcode-owned unless a CAD registry entry explicitly claims
   ownership.
4. Dispatch through CAD policy, cancellation, mission/run resolver, scaffold/guardrail checks,
   and `agent_turn._dispatch_tool` accounting/loop guard. Preserve document/run/actor context
   injection and progress/cancel callbacks.
5. Return policy and guardrail refusals in their machine-readable failure shapes. Let tool
   exceptions propagate as tool errors. Normalize successful values to finite JSON-safe data;
   never fabricate success.
6. **Every host-tool return value crosses the wire as `{content:[{type:"text",text}], details}`**
   (`chudcode_tools.host_tool_result`, applied in `HostToolBinding.to_host_tool`, for results
   AND progress updates). A `host_tool_result` whose `result` lacks `content` never resolves
   inside Chudcode 0.3.0: `write xd://<tool>` hangs, `wait_for_idle` hangs with it, and the
   GUI shows "Thinking…" until the 2-hour cap. Verified live for `{"ok":false,…}`,
   `{"ok":true,…}` and `{"docs":[…]}`. CAD tools keep returning raw JSON objects; the
   envelope is applied in exactly one place.
7. The main chat MUST send `workspace_dir` (`composer.cpp attachAgentConversationOptions`,
   from `st.workspaceDir`). It is the `fs_*` jail (`CHUDWARE_FS_ROOT`), the project-save
   root and the Chudcode process cwd; without it every `fs_list/fs_glob/fs_read` refuses with
   `workspace_root_unavailable` and Chudcode's own `read`/`glob` browse the worker's cwd (the
   repo). The core's `agent_workspace_dir()` only runs on the offline lane.
8. Transient commands (auth state, provider list, model catalog, memory, login) share ONE
   long-lived no-session `chud --mode rpc` process (`ChudcodeSessionHost._service_client`,
   warmed by `worker_main._warm_chudcode_service`). Spawning costs ~1.1 s and the GUI issues a
   dozen of these at startup, so spawn-per-call was the "providers take 30 s" defect. Calls are
   serialized on `_service_lock`; a dead process is replaced on the next call; `shutdown()`
   stops it. Never reintroduce a per-call spawn.
8b. **Session processes are lean (2026-09-27).** `_make_client` spawns conversation clients
   with `--tools <CAD_SESSION_TOOLS> --no-skills --no-lsp`, plus `--no-mcp` when the
   binary's `--help` lists it (`_binary_supports_flag`, probed once per binary identity;
   Chudcode 0.4.0 supports it). Sub-agents inherit that ceiling. The shared
   service client stays unrestricted.
8c. **CAD session overlay (2026-09-28).** `_make_client` passes
   `--config <data>/chudcode/cad-session-<backend>-<content-hash>.yml` (`_cad_session_overlay`, gated on
   `--help`). It disables user-level context files (`context-file:user:*` — Hunter's
   `~/.claude/CLAUDE.md` dev brief made gpt-6-sol pull/commit/push a firmware repo) and pins
   memory: the GUI's "Project memory" (`agent.memory_backend` = off | sharpshooter |
   mnemo, default sharpshooter) arrives as host param `memory_backend`; `mnemo` is
   translated only at the runtime boundary to `memory.backend: mnemopi` and
   `mnemopi.scoping: per-project`. Each backend is keyed by the process cwd =
   project folder, and a no-workspace session gets `off`. `--tools` carries only
   that backend's tools (`_CAD_MEMORY_TOOLS`; chud exits on one it lacks). A
   binary without sharpshooter settings falls back to Mnemopi. Project context
   files still load. The shared no-session client also takes a backend-configured
   overlay and is recycled when the GUI selection changes, so explicit-root
   memory commands do not silently use Chudcode's default `off`.
   Chudcode's `<active-repo-context>` (a nested repo = "Active project") cannot be switched
   off; `general_agent.yaml` HOW TO WORK 7 overrides it.
   The lean builtins use `task`/`wait` for orchestration (`history://`, `agent://`,
   `proc://` via read/write), not the removed `hub` tool. Browser/desktop automation
   is available inside `eval` through `browser` and `computer` preludes; the CAD
   overlay pins `browser.enabled: true` and `computer.enabled: true`, not
   standalone `--tools` entries.
8e. **The Compaction choice governs online sessions (2026-10-03).** `online_turn`
   translates Summary to `methodOrder: [soft]`, Shake to `[shake]`, Snapcompact to
   `[snapcompact]`, Off to `enabled: false`, and Automatic leaves runtime policy intact.
   Supersede/drop/keep-recent options accompany the choice. Overlays are immutable and
   content-keyed; never rewrite a shared watched overlay for a different session.
   Chudcode 0.4.1 has no acknowledged method-setting RPC. A changed choice refreshes
   only an idle connection at the next turn or manual compact, resuming the exact saved
   history; unchanged connections stay persistent. Eval scratch state resets on that
   refresh. Explicit auto-compaction/retry switches survive. Manual compact names the
   committed entry's actual method, never a guessed default.
8d. **CAD calls made inside eval get feed rows (2026-09-28).** eval's `tool.write("xd://…")`
   reaches the host under an eval-bridge id (`js-write-…`) no tool frame owns, so the feed
   showed only "eval". `run_turn` binds `host_call_observer` on the run context
   (`_bind_host_call_observer`, unbound in `finally`); `HostToolBinding.execute` reports
   `host_tool_start/_end` frames for top-level-agent calls (`_nested_call_reporter`); the host
   forwards only ids no in-flight `tool_execution_*` call owns, parented to the single open
   call, through `forward` (mapper + watchdog). Sub-agent calls stay on `subagent_event`.
9. **CAD's skill packs reach the model through `cad_skill`, never `skill://`.** The
   `skill://` scheme is resolved by Chudcode's `read` builtin against Chudcode's OWN skill
   registry, which contains nothing from this repo — a role card that printed
   `skill://assembly-merge` cost a live mission a turn and got `Unknown skill`.
   `mission_dispatch.linear_role_prompt` (the one composition point for the main chat AND
   delegated roles) appends `skills.catalog_block()`; the fetch verb is the CAD host tool
   `cad_skill(name, path=None)` (`cad_doc_tools`, `MAIN_CHAT_CORE_TOOLS` +
   `copilot_mode_tools.META_TOOL_FLOOR`, so a capped role can still see it). Trigger-based
   injection is `chudcode_tools._skill_prefix` + `_prefixed`: a skill whose `triggers.tools`
   names the dispatched tool rides back as a bounded prefix on that tool's FIRST result text
   per session, with `details` untouched. A call from eval (`_eval_bridged`: Chudcode's
   bridge ids are `js-<tool>-<uuid>` for the Python and JS kernels) never gets it and does
   not consume it — eval code `json.loads` the text, and the prefix broke every parse.
   Never re-copy a SKILL.md into a role card.
9b. **Results stay parseable, bounded and honest (2026-09-28).** Chudcode spills a result over
   `tools.artifactSpillThreshold` (50 KB) to an artifact with a 20 KB head + tail, and `write
   xd://` then REPLACES the CAD `details` with its truncation meta, so a 600 KB `describe_part`
   reached the model and eval as broken JSON (c211–c230: dozens of eval cells writing
   `[X result]`/artifact/line-number strippers). `host_tool_result` parses JSON-text returns
   (most MCP tools return `json.dumps`) into `details`, and bounds the text to
   `_RESULT_TEXT_BYTES` (46 000 B) minus any skill prefix: top-level keys survive, and below
   them the list/string/id-keyed map whose halving saves most is halved first
   (`_shorten_largest`, so a 16-number transform stays whole while the component list
   shortens); a `_truncated{full_result, full_bytes, shortened}` key names the complete JSON
   spilled to `<data>/cache/tool_results/<session>/` (`_spill_full_result`, 100 kept).
   A CAD `{"ok": false}` is a normal return with `isError` unset: `chudcode_events.
   cad_result_payload` reads `details` or `details.xdev.inner`, so the feed card fails with
   the tool's own `error`/`outcome_kind` and `ProgressWatchdog` does not count it as a
   mutation. The host dispatch path runs `agent_turn._reconcile_kwargs` like the offline
   resolver: synonyms map (reported in `arg_interpretation`), unknown/missing arguments are
   teaching errors that charge no cap — never a raw `TypeError` naming `<locals>`.
10. For physical sourcing, `core/parts/{catalog,generate}.cpp` supplies the cited
    `hobby_servo` envelopes (SG90/MG90S/MG996R/MG995/DS3218/micro_9g);
    `core/api/libchudware.cpp::parts_make_impl` publishes the `output_axis` datum.
    `parts_tools.fetch_cad_model_impl(url,name,import_part)` accepts direct HTTPS CAD and
    supported model pages, stores file + provenance sidecar under `<project>/reference/`
    and optionally imports. Printables guest CAD uses `adapters/printables.py` GraphQL
    via curl; MakerWorld/GrabCAD may require login rather than promise a download.
11. `chudcode_host.run_turn` owns `ProgressWatchdog`: after 12 completed research calls
    or six minutes since progress without a successful CAD mutation, steer once per
    stall and at most twice per turn. Reading `xd://` is documentation, not a
    mutation; only successful CAD mutators reset progress. `host_params` mode
    Ask/Plan and `plan_only` exempt the turn. `roles/general_agent.yaml` tells multi-part
    mechanisms to write `design/layout.md`, build/save each part, mate every joint
    and sweep with `simulate_motion` instead of researching indefinitely.
12. `research_fetch_page` (`services/research/fetch_page.py`) flags JS bootstrap
    shells and retries a rendered browser before returning `js_shell`;
    `research_web_search` (`web_search.py`/`providers.py`) distinguishes DDG's
    HTTP 202 challenge (`provider_blocked`) from a real empty search (`no_results`).
    Neither is an `ok:true` empty page/result.
13. `subagent_registry.list_sessions(project_dir=...)` returns only records stamped
    for that project; missing project ownership never matches. Stale live records
    become terminal `abandoned` only from owner PID/start-time or heartbeat
    evidence, not age while the owner is alive.
14. **Task sub-agents inherit the host tools (2026-09-27).** Before this, Chudcode's `task`
    sub-agents got only MCP proxies: the RPC host tools lived solely in the top-level
    session's registry (`rpc-mode.ts` `hostToolBridge.setTools` → `session.refreshRpcHostTools`),
    so every spawned CAD worker reported "CAD tool devices are NOT mounted" and the main
    agent built parts serially. Now `RpcHostToolBridge` is the session's `HostToolSource`;
    `structured-subagent.ts` passes it as `inheritedHostTools`, and `createAgentSession`
    registers agent-bound adapters (explicit agent `tools:` allowlists narrow them;
    restricted/plan children get none), follows later `set_host_tools`, and hands the source
    to grandchildren. Calls ride the SAME client as `host_tool_call` frames with additive
    `agentId`/`parentAgentId` (= `subagent_lifecycle.payload.id` / spawner); chudcode_rpc
    exposes them as `HostToolContext.agent_id/parent_agent_id`. Aborting a sub-agent sends
    `host_tool_cancel` for its in-flight call.
    CAD side: `dispatch_host_tool(..., agent_id=, parent_agent_id=)` runs every sub-agent
    call concurrently (no dispatch lock around the tool body); `_dispatch_tool` RESERVES the
    per-tool cap slot under `_CALL_ACCOUNTING_LOCK` and refunds calls that never ran;
    `tool_calls` records (with `agent_id`) append under `_RECORDS_LOCK`; `enable_tools`
    refreshes serialize; `_mode_refusal` re-applies Ask/Plan at dispatch (`mode_refused`);
    skill injection dedupes per `session:agent_id`. `online_turn.resolve_settings` defaults
    `subagent_subscription="events"`: `chudcode_events` maps `subagent_lifecycle/progress` to
    one upserted `sub_run` per sub-agent (parent = the `task` call id) and `subagent_event`
    tool frames to `agent.tool_call` with `subagent_id` + `parent_id`=task call, dropping the
    child's text; `ProgressWatchdog` counts a sub-agent's successful CAD mutation as turn
    progress but not its research. `general_agent.yaml` PARALLEL SUB-AGENTS: layout.md
    contract first, one document per sub-agent, parent integrates and mates.
15. **`ask_user` asks the human in the desktop chat (2026-10-03).** Before this every online
    `ask_user` returned suggested defaults instantly (`MissionDispatch.ask_user`,
    `blocking=False`), so models ended turns with prose question lists (the plane chat).
    The GUI composer sends `interactive_questions: true`; `run_turn` then binds
    `context["ask_human"]` (`chudcode_questions.TurnQuestions.ask`) and dispatch injects it
    into any target that declares `ask_human` (`_HOST_INJECTED_PARAMS`, hidden from
    schemas). The batch goes out as `agent.question`, the call blocks with no timeout,
    and resolves from the control action `answer` (`ChudcodeSessionHost.answer`, hostd
    `agent_answer`), a user steer (`steer(from_user=True)` resolves the oldest pending
    batch as `user_reply` — steering would queue behind the blocked call forever), or
    Stop (`_cancel_run` closes it). Each end emits `agent.answer {source}`. The progress
    watchdog holds while a batch is pending and restarts its clock afterwards; its own
    nudges steer with `from_user=False`. Headless turns (no flag) keep the default path.

## Event rules

`chudcode_events.map_rpc_frame` with per-turn `RpcFrameState` is the translation authority.

- Only streamed deltas produce visible assistant text. Final message envelopes are lifecycle
  records; replaying their content double-renders text.
- Preserve `text`/`thinking` channels. Empty text flushes are not thinking boundaries.
- Unwrap `custom{customType}` before classification: title/thinking/notice/available-command
  lifecycle frames are metadata/status (or ignored), never completed tool cards.
- Remember start args/intent by actual Chudcode call id, carrying bounded previews and
  content-derived targets to the terminal `agent.tool_call`; `write xd://tool` uses
  the JSON content rather than the device path to name its target, while `read xd://`
  only inspects tool documentation. Start/progress tool events map to status; a
  terminal result/cancel maps once. A synchronous host call with a result is terminal.
- A failed builtin's reason lives only in `tool_execution_end.result.content[].text`
  (`isError:true`): unknown tool names, calls skipped because a steer was queued
  ("Skipped due to queued user message" — Chudcode drops the rest of that tool batch and
  tells the model to retry), thrown tool errors. `_tool_end` carries it as `error_preview`;
  before 2026-09-27 every such card read "tool failed".
- The resume key is bound when the turn STARTS (2026-09-27): right after `agent_start`,
  `run_turn` emits a `session_bound` frame (`get_state` sessionId/sessionFile) that maps to
  `agent.session {chudcode_session_id, chudcode_session_file}`; `app.cpp` stores it in the
  conversation's `ChudcodeSessionLink` and persists immediately. The post-turn doctor capture
  alone never ran when hostd died mid-turn, so a 2-hour session (c217) had no resume key.
- Preserve correlation (`run_id`, `session_id`, node/span/parent/subagent ids), but synthesize
  only deterministic digest ids when the source omitted one.
- Unknown frames remain visible as bounded deterministic `sub_run` rows. The new
  `prompt_result` (request id, `agentInvoked`, status, `sessionSettled`),
  `session_settled`, and `queue_update` steering/follow-up snapshots are RPC
  bookkeeping, not feed activity; ignore them. `agent_end.isTerminal:false`
  remains nonterminal even when `yielded` is present. `messageId` and nested
  `subagent_event.payload.event` retain their message/tool lineage.
  Never drop genuinely unknown frames or guess a stronger semantic event.
- Provider usage maps to both `agent.provider_usage` and `agent.context`; todos, retry,
  compaction, fallback, spawn/subagent, and terminal events retain their existing feed shapes.
- **The context meter (`agent.context`) speaks the frozen `{used, window, window_source}`
  contract (`host/src/app.cpp` reads nothing else).** Chudcode 0.3.0 emits no usage frame:
  usage exists only on the assistant `message_end` envelope, mapped to `used` with
  Chudcode's `calculateContextTokens` (skipping error/aborted and nested-agent messages).
  `run_turn` also emits `get_state().contextUsage` — Chudcode's own footer figure — once
  right after `agent_start` (the GUI zeroes the meter on `run_started`) and once after
  `agent_end`. Before 2026-09-24 the keys were `context_tokens`/`context_window`, so the
  GUI only ever showed its transcript estimate ("~22k" for a 296k session).

## Auth, memory, and settings

Service-level auth/model/memory calls use the shared, serialized no-session client
`ChudcodeSessionHost._service_client` (through `transient_call`); it is replaced after death
or when the selected memory backend changes, and disposed on shutdown, not spawned per
request. Its `--config` overlay pins the GUI-selected backend rather than the runtime's
default `off`, so `memory_remember`/`memory_recall`/`memory_stats` operate on the same
project-root backend as sessions. Use `session_call` only for an already-live session:
stats/state, compact/handoff, auto-compaction/auto-retry, and STT. A status/settings
request must not silently create or resume a conversation.

Secrets stay in typed `chudcode_rpc` wrappers. UI responses may expose state, account labels,
usage, and model metadata, never credential bytes. Login UI requests are bounded and allow only
safe web URLs before they enter Chudware's `extension_ui_request` event.

`chudcode_models_list` rows carry `reasoningEfforts` (from the catalog's `thinking.efforts`)
and `vision` (from `inputModalities`). The composer's effort menu reads that ladder; without
it an id missing from `model_registry.h`'s static table fell back to Low/Medium/High.
Anthropic OAuth requests present Chudcode's Claude Code fingerprint
(`packages/ai/src/providers/claude-code-fingerprint.ts`). Upstream self-heals its
`DEFAULT_CLAUDE_CODE_VERSION`; `PI_AI_CLAUDE_CODE_VERSION` explicitly overrides
it when necessary. The Codex model list is gated by `CODEX_CLIENT_VERSION`
(`packages/catalog/src/wire/codex.ts`, currently 0.159.0). If a released Codex
model is absent, probe `/backend-api/codex/models?client_version=<latest @openai/codex>`
before anything else.
`refresh_catalog_if_stale` re-discovers immediately when the `chud` binary's stat identity
changes, not only after its 2-hour TTL.

## Binary and release workflow

Runtime binary lookup order is explicit constructor hint, `CHUDWARE_CHUDCODE_BIN`, canonical
`chud`/`chud.exe` beside the packaged Python executable, then developer `PATH`. Production must
use the signed-payload-adjacent binary.

`vendor/chudcode/CHUDCODE_ARTIFACT.lock` uses schema `chudcode-artifact-lock-v1` and records
Chudcode product version, repository commit, RPC protocol 2, and URL/SHA-256 for exactly
`linux-x86_64`, `linux-aarch64`, `macos-x86_64`, `macos-arm64`, and `windows-x86_64`. Null URL
and digest fields intentionally make packaging fail closed.

```bash
python3 scripts/pin_chudcode.py check-lock
python3 scripts/pin_chudcode.py verify --target linux-x86_64 --input /secure/chud-linux-x86_64
python3 scripts/pin_chudcode.py stage --target linux-x86_64 \
  --input /secure/chud-linux-x86_64 --output /tmp/chud
```

`stage` is offline, verifies target/digest/version before and after copy, and publishes only the
canonical filename atomically. Only explicit `fetch` performs network I/O. Pin all five targets
atomically; partial lock updates are invalid.

For retained-release recovery, validate that release's notices with
`--lock <release>/CHUDCODE_ARTIFACT.lock verify-notices --source-dir <release>`,
then its binary with `verify --target <host-target> --input <release>/bin/chud`.
The global `check-lock` publication gate requires all platforms and is not a
host-specific restorability check. Notice hashes remain mandatory even when
another platform's download is unpinned.

## Shared UI vocabulary

CAD's `assets/ui-shared-manifest.json` must be byte-identical to Chudcode's file and declare
`chud-ui-shared-v1`. It shares palette tokens, tone/status vocabulary, usage thresholds, font
provenance, and small product icons. It does not share runtime state or widget code: Chudware
keeps an independent C++ implementation and remains authoritative for CAD-specific UI state.
