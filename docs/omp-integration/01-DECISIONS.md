# OMP → CAD Integration — Locked Decisions (2026-07-23) — HISTORICAL, superseded 2026-08-31

> **Not current authority.** Every section above the
> [2026-08-31 superseding section](#2026-08-31--superseding-architecture-chudcode-rpc-is-the-only-online-runtime)
> at the bottom of this file is dated provenance. Do not implement from it. That includes
> the bun bridge, `vendor/omp/`, `scripts/vendor_omp.py`, the bridge-forwarded "stable RPC"
> table, and the implementation order. The vendored implementation, updater and Bun
> adapter were deleted (`docs/REFACTOR_LOG.md`, 2026-08-31 entry); the table remains
> historical contract text, not a callable bridge. The current online runtime is Chudcode RPC:
> the superseding section,
> [ARCHITECTURE.md, Agent runtime](../../ARCHITECTURE.md#agent-runtime), and
> [`.claude/skills/cad-chudcode-integration/SKILL.md`](../../.claude/skills/cad-chudcode-integration/SKILL.md).
> The 2026-07-23 text below is kept as the record of what was decided then.

As of 2026-07-23, this was the authoritative record of the choices made after the research and
verification rounds. It superseded the "Open decisions" section of `00-OPTIONS-AND-DECISIONS.md`,
and implementation followed these choices until the 2026-08-31 section below replaced them.

## Strategy
- **Curated selective reuse via a bun "bridge" sidecar + deterministic re-vendor script.**
  Keep CAD's C++ core + governance (roles, delegation, councils, org, guardrails, tool_registry).
  Harvest OMP's infra. Fork of the engine loop stays open for later, not now.

## Focus / non-goals
- **Primary target: the built-in copilot, refined to OMP quality but for CAD, with a real GUI.**
- **On hold (explicit):** exposing CAD's MCP tools to *external* harnesses, and CAD consuming
  *external* MCP servers (MCP-client). Not this cycle.

## Providers
- All OMP providers reachable in the **live GUI agent**, so the bridge branch goes in
  **`core/agent/providers.cpp::complete()`** (C++ → bun bridge). Python provider path is not the
  live path; it may follow later for advisor/MCP/tests.
- **UI sign-in**: users sign into providers from CAD's GUI (API key + OAuth where the vendor
  offers it). Designer assessment drives the UX. Current must-haves: Claude stack + Kimi K3.
- Contract keys: `name`, `supports_vision`, `max_tokens`, `reasoning_effort`. No `temperature`.

## Memory
- **Hybrid**: keep CAD's `project_memory` public API (remember/recall/format_for_context/stats/
  resolve_project_root); back it with OMP **mnemopi** via the bridge. Embeddings OFF by default
  (keyword recall); LLM-extraction routed through the bridge. Storage stays per-project.

## Compaction (snapcompact)
- **Adopt OMP image tier + keep CAD text compaction**, with the **safety revamp**: recoverable
  artifact side-channel for any drop, per-frame checksums validated across IPC, caller-raisable
  frame cap, text-only fallback tier, cross-provider compatibility, minimize dataloss.
- **Implemented (2026-07-27)**: bridge exposes an additive **`snap_render` RPC**
  (`vendor/omp/bridge/src/snap-render.ts`; params `{text, priorText?, modelId?, api?,
  maxFrames?, includeThinking?}` → `{frames[], text, textHead, textTail, totalChars,
  truncatedChars, checksum, summary, shortSummary}`). Python compaction consumes ONLY this
  text→PNG renderer; the canonical cut/serialize stays local in
  `services/agent/compaction.py` (its tests define the semantics). The earlier full-bridge
  `compact` consumption (`_bridge_compact`) was deleted — its divergent cut semantics broke
  the local suite under `CHUDWARE_COMPACTION_BACKEND=omp`; the zero-caller bridge `compact`
  RPC was also deleted in the 2026-08-27 YAGNI sweep. OG source + frames persist in
  `services/agent/snap_store.py`
  (content-addressed, sha256-validated), enabling cumulative re-snap (stamp-line chain in the
  conversation) and cross-provider re-snap on switch — no data loss either way.
- **New UI options**:
  - "Text-compact with cheaper model after cache unloaded."
  - **Cache-warming**: keep model context hot by sending sample hello/continue messages then
    resuming from the pre-warm point; warm only when the task/timeout exceeds the provider's
    cache-hold window (cache warming ≈ 1/10 the cost of full input re-send). Drive with OMP's
    cache-hit tracking + recorded per-provider cache-length times.
- **Cost accounting**: assign API/token cost values to every popular model/provider so cost
  summaries (per part / per session) are accurate regardless of provider.

## Firmware
- **Full breadth glue** at `core/firmware/hardware.cpp`: ARM + AVR + FPGA + RISC-V + PIC + NVIC +
  PLC (potentially) + more. Debug/program/emulate/serial. Verified gaps to fill first: AVR flash
  (avrdude), ESP32/GDB debug, OpenOCD/GDB, live physical serial monitor, `fw_debug_read_mem` UI.
- Minimal-moving-parts glue: probe-rs (DAP-native), OpenOCD (GDB/MI), avrdude+simavr (AVR),
  Renode (emu), pyserial/pyusb (transport); non-DAP tools driven via `gdb --interpreter=mi2`.
  Reuse OMP's DAP client for the debug surface.

## UI
- Build the unified **Arduino-style firmware IDE** in `host/src/ui/firmware/fw_workspace.cpp`
  (board→compile→flash→debug/serial + editor + AI copilot in one scaled view).
- **Cross-platform + local/webui parity is a hard requirement.** Keep it clean, YAGNI.

## Browser automation
- **No Chudcode browser cutover was attempted.** The audited `browser` and `computer` tools
  work in their intended harness, but are currently exposed only as in-process AgentTools,
  without a stable bridge, CLI, or library entry point callable by CAD.
- `browser` and relay modes use CDP-family transports, which do not satisfy the deliberately
  no-CDP CEF vendor/anti-bot path. `computer` drives an isolated Xvfb desktop; it does not
  expose an embeddable texture or off-screen frame source for CAD's web-view surface.
- Reconsider integration when upstream provides a **stable callable service/library API plus
  a non-CDP vendor transport and/or an embeddable off-screen frame surface**. This is a
  current integration-capability mismatch, not a claim that Chudcode is generally broken.

## Runtime / distribution
- Bundle **bun** (hard dep: `bun:sqlite`) in the AppDir/installer, per-OS/arch. Bundle
  **pi-natives** only where the snapcompact image tier runs. Bridge runs `bun install` once
  against the vendored workspace (gitignored node_modules).

## Stable RPC quick reference
- The original twelve host/UI C-ABI names remain stable. Parameters and results are JSON; the
  core forwards each call to the parenthesized bun bridge method:
  - `agent_provider_list` → `auth_list_providers`
  - `agent_provider_state` `{provider?}` → `auth_state`
  - `agent_provider_validate_key` `{provider, apiKey}` → `auth_validate_key`
  - `agent_provider_set_key` `{provider, apiKey}` → `auth_set_key`
  - `agent_provider_remove_key` `{provider, account?}` → `auth_remove_key`
  - `agent_provider_begin_oauth` `{provider}` → `auth_begin_oauth`
  - `agent_provider_oauth_poll` `{handle}` → `auth_oauth_poll`
  - `agent_provider_oauth_manual` `{handle, code}` → `auth_oauth_manual`
  - `agent_provider_oauth_cancel` `{handle}` → `auth_oauth_cancel`
  - `agent_provider_logout` `{provider, account?}` → `auth_logout`
  - `agent_models_list` `{provider?}` → `models_list`
  - `agent_cost_summary` `{events?}` → `cost_summary`
- Memory callers use the bridge directly:
  - `memory_remember` `{root, content, kind?, source?, importance?}` → `{id}`
  - `memory_recall` `{root, query, limit?, tokenBudget?, kind?}` → `{hits}`
  - `memory_stats` `{root}` → the per-project memory statistics

## Implementation order (YAGNI, focus-first)
1. **Foundation**: vendoring layout + `scripts/vendor_omp.py` (pinned SHA, whitelist, lockfile);
   bun bridge with `complete` op (providers); proven end-to-end.
2. **Live providers + UI sign-in**: C++ branch in `providers.cpp` → bridge; provider catalog +
   sign-in UI (designer-driven).
3. **Cost + cache**: cost table; cache-warming + cheaper-model-after-unload options.
4. **Memory hybrid** (mnemopi behind project_memory API).
5. **Snapcompact** image tier + safety revamp.
6. **Firmware** glue breadth + unified IDE UI.

---

## 2026-08-31 — Superseding architecture: Chudcode RPC is the only online runtime

**Status:** this section supersedes every decision and implementation instruction above,
including the frozen RPC quick reference and implementation order. It also supersedes the
operational conclusions in `00-OPTIONS-AND-DECISIONS.md`. Those documents and the `research/`
dossiers remain checked in only as dated provenance for why the boundary moved; they are not
current runbooks. Do not implement from an earlier section.

### Ownership boundary

- **Chudcode owns universal online agent behavior:** top-level and nested provider execution,
  provider authentication, model catalog/selection, durable session history and branching,
  memory, compaction, retry and prompt-cache behavior, browser/computer tools, and speech to
  text. Chudware calls the compiled service through protocol-v2 `chudcode --mode rpc`; ACP is
  generic editor interoperability, not the embedding protocol.
- **Chudware owns product and CAD behavior:** CAD tool schemas and dispatch, document and
  workspace authority, roles, councils, mission/DAG policy, delegation governance, approvals,
  guardrails, PLM, event presentation, and product documentation. CAD tools are registered
  in-band as Chudcode host tools and remain subject to the same live registry, allowlist,
  guardrail, cancellation, and audit path as every other Chudware call.
- The deleted OMP provider loop, Bun adapter, private Python provider loop, and vendored source
  tree are not compatibility surfaces. There is no second online provider path and no source
  checkout or package installation at runtime.
- Native C++ provider code remains only for the explicitly offline, local, modeling-safe
  fallback. General work without an online provider fails honestly; it never falls through to
  a hidden network loop.

### Authoritative runtime topology

`core/agent/run.cpp` sends every online top-level run to the long-lived services worker's
`agent_admin_turn`. `services/worker_main.py` owns one process-global
`ChudcodeSessionHost`; that host owns one persistent `chudcode_rpc.RpcClient` process per
Chudware conversation. `services/agent/worker_rpc.py` assembles the CAD role prompt and
run-scoped host tools, then the session host configures and prompts Chudcode. Nested agents and
their provider calls remain inside that Chudcode session.

The services worker's stdio protocol stays serialized for ordinary calls and streamed events.
Cancel, steer, and status use the worker's separate authenticated control server
(`worker_control_info`): a private Unix socket on POSIX or random loopback TCP on Windows.
This lane must remain usable while the stdio `agent_admin_turn` is blocked. Worker shutdown
stops the control server and disposes every live and transient Chudcode client.

One Chudware `session_id` maps to one live RPC process. A persisted
`chudcode_session_file` may seed that process once and cannot be changed while it is live.
Session capacity is bounded; only the least-recently-used idle session may be evicted. Explicit
close is idempotent, aborts any active run, disposes the process, and preserves the persisted
session file.

Recovery stays on that live service boundary: `chudcode_export_session` delegates to
Chudcode's HTML export (defaulting to the project `chat-exports` directory);
`chudcode_branch_session` branches at a real entry id; `chudcode_repair_session` returns a
bounded doctor summary and invokes Chudcode's backup-producing `/session doctor --repair`
workflow only when requested; `chudcode_close_session` disposes runtime state without deleting
the persisted file. Never repair or branch by rewriting session JSONL in Chudware.

### Tool and event invariants

- Build host tools from the effective live CAD registry for the turn. `None`, `[]`, and a
  non-empty allowlist remain distinct; mode/category/meta floors and mutation verification are
  resolved before advertising schemas. Never advertise an unbound or async-only CAD tool.
- Chudcode builtin names stay Chudcode-owned unless a CAD registry entry explicitly claims the
  name. CAD policy refusal and guardrail responses stay machine-readable. Tool exceptions
  propagate as tool errors; they are never converted into fabricated successful results.
- Streamed deltas are the only source of visible assistant text. Completed message envelopes
  are lifecycle records and must not replay final text. Preserve tool-call IDs across
  start/progress/end, render only terminal tool events as `agent.tool_call`, and map unknown
  frames deterministically with bounded data rather than dropping or inventing semantics.
- A turn succeeds only after a terminal `agent_end`. Event-forwarding failure makes the result
  fail; ending idle without the terminal frame is a protocol error. Cancel before prompt is a
  valid terminal cancellation; cancel after prompt calls `abort()` once. Duplicate steering
  text is idempotent, and steering received before prompt start is queued.

### Distribution and shared product vocabulary

`vendor/chudcode/CHUDCODE_ARTIFACT.lock` is the sole release identity: schema
`chudcode-artifact-lock-v1`, Chudcode product version, repository commit, RPC protocol, and
SHA-256/immutable URL for every supported platform. Packaging stages an already-built,
target-matched binary offline and puts canonical `chud`/`chud.exe` beside the services worker.
A null artifact pin is an intentional fail-closed release state.

UI sharing is specification sharing, not linked widget code. Chudcode's
`assets/ui-shared-manifest.json` and Chudware's checked-in copy use
`chud-ui-shared-v1` and must be byte-identical. They define common palette, tone/status names,
usage thresholds, font provenance, and small icon vocabulary; each product keeps an independent
native implementation, and Chudware remains authoritative for runtime state and CAD-specific UI.
