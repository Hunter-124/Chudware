# Chudware and Chudcode integration

Chudcode is the open-source (MIT) agent runtime Chudware embeds for online agent turns; it
is a fork of Oh My Pi and ships as the `chud` executable. Chudware drives it exclusively
through `chud --mode rpc` (protocol v2). This document describes the boundary between the
two, the session lifecycle, host-tool and event rules, and how the runtime version is pinned.

Version-specific runtime behavior described here is checked against
[`vendor/chudcode/CHUDCODE_ARTIFACT.lock`](../../vendor/chudcode/CHUDCODE_ARTIFACT.lock);
re-verify it when the lock moves. Daemon, document and simulation invariants are in
[engineering-notes.md](engineering-notes.md).

## Authority boundary

- **Chudcode owns** online agent turns and nested agents: provider calls, provider auth,
  model catalog and selection, persistent session history, branching, memory, compaction,
  retry/cache, browser/computer eval preludes, and speech-to-text.
- **Chudware owns** CAD tools and schemas, documents and workspaces, roles, councils,
  mission/DAG policy, delegation governance, approvals, guardrails, PLM, event presentation,
  and the offline-safe native local-modeling fallback (`core/agent/run.cpp`).
- `chud --mode rpc` is the sole embedding protocol. Never add a second online agent/provider
  loop, an external MCP hop, or a runtime source/package install.
- ECAD planning and regeneration diagnosis use the provider-shaped
  `services/agent/providers/chudcode_provider.py` adapter and
  `ChudcodeSessionHost.complete_text`. Helper calls have a positive finite budget (default
  180 s), cancellation polling and bounded process cleanup. RPC v2 has no per-request
  output-token cap, so the adapter does not claim to enforce `max_tokens`.
- Desktop turns use the provider login configured in Chudcode, or a desktop BYOK key passed
  as `api_key` (isolated per credential in a scoped HOME/session directory). The
  experimental web app's tenant cells have no provider credential route: key bytes never
  enter a cell, so `agent_admin_turn` refuses `credential_isolation_unsupported` and
  `chudcode_models_list` returns an empty catalog. Inside a cell, `chud` launches through
  `chudware-actor-sandbox`; research egress uses a separate lane (`127.0.0.1:18080`) without
  credential injection. See `web/deploy/cell/`.

## Topology

```text
host GUI / hostd -> core/agent/run.cpp -> persistent services worker (core/hostd/worker_bridge.cpp)
  -> services/worker_main.py::agent_admin_turn -> services/agent/worker_rpc.py::_agent_admin_turn
  -> services/agent/chudcode_host.py::ChudcodeSessionHost -> chudcode_rpc.RpcClient -> chud --mode rpc
```

The long-lived worker's process-global `ChudcodeSessionHost` owns one persistent RPC process
per Chudware conversation. `online_turn.py` assembles workspace, model and settings and owns
run-scoped environment lifetime. Ordinary calls use the worker's serialized stdio lane;
Stop/steer/status/answer use `chudcode_control.ControlServer`, whose endpoint and token are
exposed only through `worker_control_info` (mode-0600 Unix socket on POSIX; owner+SYSTEM,
remote-rejecting named pipe on Windows; never TCP). The token is never logged or echoed, and
control never moves back onto stdio. Malformed, oversized (64 KB request), repeated-auth and
oversized-response paths stay bounded, so steer images travel as paths, never inline bytes.

| File | Responsibility |
|---|---|
| `services/agent/chudcode_host.py` | RPC clients, session LRU, turn config, cancel/steer/status/answer, auth/memory/session calls |
| `services/agent/chudcode_control.py` | Bounded authenticated control server |
| `services/agent/chudcode_tools.py` | Live CAD registry to run-scoped host tools, guarded dispatch, result envelope |
| `services/agent/chudcode_events.py` | Deterministic, bounded RPC-frame to feed-event mapping |
| `services/agent/chudcode_questions.py` | Blocking `ask_user` batches |
| `services/agent/online_turn.py` | Turn assembly, settings, run-scoped workspace/credential lifetime |
| `services/agent/progress_watchdog.py` | Research/no-mutation detection and bounded steer |
| `services/agent/subagent_registry.py` | Evidence-based sub-agent liveness, project-scoped roster |
| `services/agent/chudcode_roles.py` | CAD role/prompt conversion, `enable_tools` refresh |
| `services/agent/worker_rpc.py` | Public worker methods; settings/auth/memory/session seams |
| `services/worker_main.py` | Worker entrypoint, control-server lifetime, global Chudcode shutdown |
| `services/worker/worker_client.cpp` | Native transport, online/offline split, control-lane client |
| `vendor/chudcode/CHUDCODE_ARTIFACT.lock`, `scripts/pin_chudcode.py` | Pinned runtime identity; pin/check/verify/stage/fetch |
| `assets/ui-shared-manifest.json` | `chud-ui-shared-v1` vocabulary, byte-identical to Chudcode's copy |

## Turn routing

- A chat send is one `general_agent` turn with the user-picked mode (Agent, Plan or Ask) and,
  when a document is open, `options.scope = {doc_id, feature_ids, selection}` (rendered by
  `worker_rpc._editor_context_block`). There is no classification step: the composer never
  keyword-routes, never creates or replaces a document, and never inlines the transcript
  (`options.history` seeds a fresh session once).
- Offline, general work refuses without an online provider; offline-safe local modeling may
  use the native C++ pipeline. Mission/DAG policy stays CAD-owned; its turns run through Chudcode.
- The main chat must send `workspace_dir`: it is the `fs_*` jail (`CHUDWARE_FS_ROOT`), the
  project-save root and the Chudcode cwd. `resolve_workspace` canonicalizes exactly the
  selected directory and never widens it through ancestor-project discovery.

## Sessions and lifecycle

- The Chudware `session_id` is the live key. `chudcode_session_file` may seed a new process
  and cannot change while it is live. Capacity: `CHUDWARE_CHUDCODE_MAX_SESSIONS` (default 8,
  max 32); only the least-recently-used idle session is evicted, otherwise the call refuses.
- `CHUDWARE_CHUDCODE_EVENT_HISTORY` defaults to 1e6 frames; nothing in the lane may silently
  cap an hours-long turn.
- CAD sockets belong to the owning turn (`TurnConnections`, `services/mcp/daemon_bridge.py`),
  installed in `chudcode_host.run_turn` and leased by `chudcode_tools.dispatch_host_tool`.
  Parallel tool calls use different sockets; nested calls reuse their lease.
- `run_id` is unique while active. Cancel before prompt is a valid cancellation; after prompt
  it sends `abort()` once. Duplicate cancel and identical steer are idempotent.
- **Steers are append-only and tracked to delivery.** A steer stays undelivered until its user
  `message_start` streams. Tools that declare `wake` (for example `wait_job`) end early with
  `woken_by`. Stop calls `remove_queued_message` before `abort()`; withdrawn or still-queued
  steers open the session's next prompt. Never use abort to deliver a message: that drops the
  in-flight response. Delivery requires positive evidence (a streamed user frame or a
  correlated `agent_answer`).
- **Control endpoints live as long as their worker generation.** A single refused or timed-out
  control request does not clear the credentials; teardown or replacement does. Failed
  requests are never blindly replayed.
- A turn requires a terminal `agent_end` after `wait_for_idle`; idle without it is a protocol
  error. `agent_end` is not success: a failed final retry or `stopReason:"error"` returns
  `ok:false, stop_reason:"provider_error"`.
- **No implicit wall-clock cap.** The GUI sends no `timeout`; waits proceed in finite 30 s
  slices so Stop and process death stay observable. An explicit positive timeout aborts once
  on expiry and returns `stop_reason:"turn_budget"`; `progress_watchdog.TurnBudgetReminder`
  sends one reminder steer before the budget ends. `run_mission.py --timeout-s 0` is unbounded.
- **A conversation lives in one worker.** Another worker resumes it only after the owner's
  control action `release` (`release_session`; refuses `session_busy`, never cancels) has
  disposed the idle runtime. Two live runtimes of one session would fork its history.
- `chudcode_close_session` is idempotent: mark runs cancelled, abort, dispose, keep history.
  hostd never replays an `agent_admin_turn` whose worker died (`worker_lost_mid_turn`);
  "continue" resumes from the bound session file.
- Shutdown follows ownership: `worker_bridge.cpp` owns the worker; `worker_main.stop_services`
  closes the control server, then `chudcode_shutdown()` disposes every client. No RPC process
  outlives its worker.
- Nested agents stay inside Chudcode; their events map into the existing feed. Never
  synthesize success when Chudcode reports failure.

## Recovery

Recovery goes through the service boundary, never file surgery:

- `chudcode_export_session {session_id, output_path?}`: live HTML export.
- `chudcode_branch_session {session_id, entry_id}`: real branch at a real entry id.
- `chudcode_rewind_session {session_id, user_text, occurrence, ...}`: forks before an edited
  user message on the active branch; the original file survives.
- `chudcode_repair_session {session_id, repair}`: bounded doctor summary; `repair:true` runs
  Chudcode's backup-producing `/session doctor --repair`.

Never hand-edit session JSONL, invent an entry id, or report a repair the doctor did not do.

## Host-tool rules

1. `chudcode_tools.build_host_tools` derives names, descriptions, schemas and handlers from the
   live CAD MCP/mission registries; it is an adapter, never a second catalog. Chudcode builtin
   names stay Chudcode-owned unless a CAD entry claims them.
2. Allowlists are tri-state: `None` unrestricted, `[]` live floors only, non-empty restricts.
   `_effective_names` applies allowlist, on-demand loads, mode policy (Ask/Plan read-only),
   meta floors, then `verify_battery` when mutators exist. Exactly that set is advertised and
   dispatched; `_mode_refusal` re-applies the mode at dispatch.
3. The main chat advertises `tool_categories.MAIN_CHAT_CORE_TOOLS` plus a menu;
   `enable_tools(tools=|categories=)` loads the rest. Every registration goes through
   `chudcode_host.publish_host_tools`, which also sends `loadable_tools`.
4. Every call crosses the CAD chokepoints: policy refusal, cancellation, mission/run dispatcher,
   scaffold/guardrail checks, then `agent_turn._dispatch_tool` accounting. `_reconcile_kwargs`
   maps synonyms and returns teaching errors, never a raw `TypeError`.
5. Refusals keep machine-readable shapes; success is finite JSON-safe data. A CAD
   `{"ok": false}` is a normal return that fails the card with the tool's own error.
6. Every result crosses as `{content:[{type:"text",text}], details}` via `host_tool_result`; a
   result without `content` never resolves and hangs the turn.
7. Results stay parseable and bounded: text is capped at `_RESULT_TEXT_BYTES` (46 000 B) by
   shortening the largest member, with the full JSON spilled by `_spill_full_result`. Image
   tools return one mapping (`tool_images.image_result`) with image blocks kept in `content`.
8. CAD skill packs reach the model only through `cad_skill(name, path)`, never Chudcode's own
   `skill://` registry, and are never copied into a role card.
9. Desktop transient commands (auth, providers, catalog, memory) share one long-lived
   no-session client (`_service_client`, serialized on `_service_lock`); never spawn one per
   call. Tenant cells refuse this ambient service.
10. Session processes are lean: `_make_client` passes `--tools <CAD session tools> --no-skills
    --no-lsp`, `--no-mcp` when the binary supports it, and an immutable content-keyed
    `--config` overlay that disables user-level context files, pins the GUI memory backend and
    enables browser/computer eval preludes.
11. The user's compaction choice governs (Summary, Shake, Snapcompact, Off; Automatic leaves
    runtime policy). Overlays are immutable; a changed choice refreshes only an idle
    connection at the next turn, resuming the exact history. Manual
    `/compact [summary|snapcompact] [focus]` is a run, not a side RPC: `agent_admin_turn`
    with `compact: {strategy?, instructions?}` builds the turn's session launch parameters
    (`_session_launch_params`) and calls `ChudcodeSessionHost.run_compaction`, which resumes
    a conversation that is not live (it stays live for the next turn) and sends Chudcode
    `compact` with the `/compact` mode (`summary`→`soft`) for that compaction only. The
    request has no time limit; Stop (`cancel(run_id)`) aborts it, and steers are refused
    (`compaction_in_progress`). Shake and Off are refused for manual compaction rather than
    substituted. `session_compact` is offline/standalone-only.
    A resume Chudcode cancels (recorded in another project folder) fails the run or turn
    with `session_file_mismatch` instead of continuing in an empty session.
12. Task sub-agents inherit host tools over the same client with `agent_id`/`parent_agent_id`;
    explicit `tools:` allowlists narrow them; restricted/plan children get none. Each call
    runs inside `cad_common.agent_doc_context`, so each sub-agent has its own current part.
13. `ask_user` blocks for a human answer only when the composer sends
    `interactive_questions: true`; it resolves from a control `answer`, a user steer, or Stop.
    Headless turns keep the non-blocking default.
14. `ProgressWatchdog` steers once per stall (12 research calls or 6 minutes without a
    successful CAD mutation), at most twice per turn; Ask/Plan are exempt.
15. Sourcing honesty: `fetch_cad_model_impl` stores file and provenance under
    `<project>/reference/`; search distinguishes `provider_blocked` from `no_results`; neither is
    an `ok:true` empty result.

## Event rules

`chudcode_events.map_rpc_frame` with a per-turn `RpcFrameState` is the translation authority.

- Only streamed deltas are visible assistant text; completion envelopes never replay it.
- Tool events correlate by Chudcode call id: start/progress map to status; a terminal result
  or cancel maps to one `agent.tool_call` with bounded previews.
- A `session_bound` frame right after `agent_start` maps to `agent.session`; the GUI persists
  the session link immediately so a mid-turn daemon death keeps the resume key.
- Correlation ids (`run_id`, `session_id`, node/span/parent/sub-agent ids, `messageId`) are
  preserved. Unknown frames become bounded deterministic `sub_run` rows, never dropped or
  upgraded to a guessed event.
- Auto-compaction start/end pair into one upserted `sub_run`.
- Provider usage maps to `agent.provider_usage` and `agent.context`; the context meter speaks
  only the `{used, window, window_source}` contract.

## Auth, memory and settings

- Desktop service calls go through `transient_call` on the shared client, whose overlay pins
  the GUI-selected memory backend so `memory_*` tools see the same project backend as sessions.
  `session_call` only targets an already-live session; a status request never creates one.
- Provider secrets stay inside typed `chudcode_rpc` wrappers; UI responses expose state, labels,
  usage and model metadata, never credential bytes. Login requests are bounded and allow only
  safe web URLs.
- `chudcode_models_list` rows carry `reasoningEfforts` and `vision`; `refresh_catalog_if_stale`
  re-discovers when the `chud` binary identity changes.

## Binary lookup and version pin

`ChudcodeSessionHost` resolves the executable in this order: constructor hint,
`CHUDWARE_CHUDCODE_BIN`, `chud`/`chud.exe` beside the packaged Python interpreter, then (in
developer mode only) `PATH`. Production uses the binary staged beside the signed payload.

The lock (`chudcode-artifact-lock-v1`) records the Chudcode version, source repository and
commit, RPC protocol 2, the two locked notice files, and a URL plus SHA-256 for each target
(`linux-x86_64`, `macos-x86_64`, `windows-x86_64`). Binaries are downloaded from public
GitHub releases; the Windows binary is built natively on Windows from the locked commit
(a cross-compiled Windows CLI crashed under Bun 1.4.0). Null fields fail packaging closed;
all targets are pinned atomically with `pin`. The
installer and bootstrap refuse to provision without the lock
(`services/installer/provision.py`, `host/src/bootstrap.cpp`).

```bash
python3 scripts/pin_chudcode.py check-lock
python3 scripts/pin_chudcode.py verify --target linux-x86_64 --input <path>/chud
python3 scripts/pin_chudcode.py stage  --target linux-x86_64 --input <path>/chud --output <dir>/chud
python3 scripts/pin_chudcode.py fetch  --target linux-x86_64 --output <dir>/chud
```

`stage` is offline, verifies target, digest and version before and after the copy, and
publishes the canonical filename atomically; only `fetch`/`ensure` touch the network, and the
lock digest stays authoritative. `verify-notices --source-dir <release>` checks the notice
hashes of a retained release independently of binary availability. `check-lock` is a
publication gate, not a per-host restorability check.

## Shared UI vocabulary

`assets/ui-shared-manifest.json` is byte-identical to Chudcode's copy and declares
`chud-ui-shared-v1` (palette, tone/status vocabulary, usage thresholds, font provenance, small
icons). There is no shared runtime state or widget code; Chudware's C++ UI stays authoritative
for CAD-specific state.
