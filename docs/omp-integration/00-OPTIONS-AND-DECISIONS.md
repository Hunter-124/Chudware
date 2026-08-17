# OMP → CAD (Chudware): Integration Options & Decisions

Consolidated synthesis of 11 research dossiers (see `./research/`). Purpose: give you a
feature-by-feature menu of **KEEP / PORT / SIDECAR / FORK / GLUE** choices so we can decide
what to bring over from oh-my-pi ("OMP") before implementing.

- **KEEP** = CAD's existing code stays; OMP has nothing better.
- **PORT** = reimplement OMP's pure-logic feature in CAD (Python/C++); no runtime dep.
- **SIDECAR** = run OMP's real TypeScript verbatim in a bun subprocess ("the bridge"); byte-identical to OMP, updated by re-vendoring.
- **FORK** = adopt OMP's engine as a base and drive it from CAD's UI.
- **GLUE** = wire in an external open-source tool (firmware).

OMP vendored at `scratch/omp-src` (pinned git `6ae7cdb`), upstream `github.com/can1357/oh-my-pi`.

---

## 0. TL;DR — recommended strategy

**Curated selective reuse, not a wholesale fork.** Three findings drive this:

1. **CAD's real engine is the C++ core** (`core/agent/run.cpp`); `services/agent/loop.py` is a
   dead stub. `services/agent` is the host-RPC bridge + CAD-domain reasoning.
2. **CAD's governance layer is often *more advanced* than OMP's** — roles (versioned
   `aicad.role/1` schema, tool-allowlists, GBNF grammar), `delegation.py` (budget tree +
   approval + kill-switch), `councils.py`, `org.py`, `guardrails.py` (hard physics floors),
   `redaction.py` (ITAR), and the `tool_registry.py` `_get_fn` dual-dispatch gate. OMP has no
   equivalents. **These are the crown jewels — KEEP.**
3. **OMP's genuine wins are the generic infra**: the 44-provider + auth layer, a production
   **MCP client** (CAD has none), the **DAP/LSP** debugger substrate, **slash-commands**,
   **advisor hardening**, **prewalk**, **session atomic-writes**, and the **snapcompact image
   tier**. Precedent already exists: `compaction.py` and the handoff prompt are attributed
   in-code as OMP ports.

So: **reuse OMP's infra via a bun "bridge" sidecar (+ small Python ports), keep CAD's engine
and governance, and make re-vendoring a deterministic script** so we keep inheriting upstream
improvements. Fork stays a *later, optional* move for the engine loop only.

---

## 1. Reuse mechanism (the foundation — built regardless of feature choices)

- **Vendored subtree**: `vendor/omp/` holding only the OMP packages we consume, pinned to an
  upstream commit recorded in `vendor/omp/OMP_VENDOR.lock`.
- **Deterministic update**: `scripts/vendor_omp.py --sha <git-sha>` (also `--from`, `--verify`) clones/fetches upstream at the
  pinned SHA, copies the whitelisted package paths, rewrites the lockfile, and verifies a
  content hash. Re-run with a newer SHA = pull upstream "goodies"; diff is reviewable.
- **The bridge**: one bun subprocess, launched like CAD's other `worker_rpc` workers
  (newline-delimited JSON over stdio — structurally identical to OMP's `--mode rpc` and CAD's
  `worker_rpc/2`). Ops: `complete` (providers), `memory.*`, `compact`, and later
  `dap.*` / `mcp.*` / `slash.*`. Bun discovered on `PATH` with a bundled fallback binary.
- **Small Python PORTs** for pure-logic wins with no bun-only deps (prewalk state machine,
  atomic writes, typed provider errors, credential resolver, advisor emission-guard).

---

## 2. Feature matrix

| # | Feature | OMP location | CAD counterpart | Rec | Effort | Notes / risk |
|---|---------|--------------|-----------------|-----|--------|--------------|
| **Providers & auth** |
| 1 | 44 providers + streaming | `ai/src/providers/*` | ~24 partial Py providers | **SIDECAR** | M | bridge `complete`; collapse stream→(text,thinking,usage) |
| 2 | Credential store + a/b/c retry-rotate | `ai/auth-storage.ts`,`auth-retry.ts` | per-provider hand-rolled 429/rotation | **PORT** (typed error + resolver) or **SIDECAR** (full multi-account pools) | S–M | fixes "switching providers" pain |
| 3 | Catalog resolution (models.dev + cache) | `catalog/model-manager.ts` | `model_catalog.py` (merge only) | **PORT** short-TTL cache | S | optional |
| 4 | Cross-provider history replay | `providers/transform-messages.ts` | none (resolved once/proc) | **KEEP/skip** | — | only needed for live mid-session hot-swap |
| **Memory** |
| 5 | mnemopi vector memory | `mnemopi/*` | `project_memory.py` (SQLite+FTS5, 329L) | **DECISION** (see Q2) | M | embeddings need native/API or run keyword-degraded; global-bank vs per-project semantics |
| **Compaction** |
| 6 | snapcompact image tier | `snapcompact/*` (+native Rust) | `compaction.py` (text) | **SIDECAR + REVAMP** | L | needs `pi-natives` binary + vision model; unbounded loss past 80-frame cap, no checksums, no non-vision fallback |
| 7 | Recoverability primitives | — | `shake_messages`, `apply_supersede_reads`, `reinject_archive` | **KEEP (CAD ahead)** | — | feed these into the revamp |
| **Advisors / roles / orchestration** |
| 8 | Advisor engine (emission-guard, quarantine) | `coding-agent/advisor/*` | `proactive_advisor.py` (lighter) | **PORT guard/quarantine**; **SIDECAR** full watchdog (optional) | S / M | anti-flood + untrusted-output hardening |
| 9 | Roles / delegation / councils / org | `task/`, `swarm-extension` | `roles.py`,`delegation.py`,`councils.py`,`org.py` | **KEEP (CAD ahead)** | — | — |
| 10 | Swarm YAML DAG scheduler | `swarm-extension/*` | none (org clusters, no DAG) | **PORT concept** (run via CAD `delegation.py`) | M | optional |
| **Session & model switching** |
| 11 | Prewalk (mid-session model swap) | `session/agent-session.ts` | none | **PORT** | M | needs C++ loop to hot-swap model + inject hidden msg |
| 12 | Session atomic writes / corruption tolerance | `session/session-storage.ts` | `ledger.py` (replay, torn-line tolerant) but non-atomic snapshot/workspace writes | **PORT** | S | directly targets session-corruption pain |
| **Tools / external** |
| 13 | MCP **client** | `coding-agent/mcp/*` | none (CAD is MCP *server* only) | **SIDECAR/PORT** | M | pure win: "use outside MCPs & other tools" |
| 14 | Slash-commands / "magic keywords" | `slash-commands/*`, `extensibility/custom-commands/*` | none | **PORT** | S–M | user-defined + `.md` commands |
| 15 | Tool registry / approval gate | `tools/approval.ts` (3-tier) | `tool_registry.py` `_get_fn` (family+risk+lease) | **KEEP authoritative** (OMP tier = derived projection) | — | crown jewel |
| **Debugger substrate (firmware)** |
| 16 | DAP client (gdb/lldb-dap, `dap.json`, custom_request) | `coding-agent/dap/*` | shallow probe-rs/esptool only | **PORT/SIDECAR** | M | reuse target for GDB/OpenOCD/probe-rs |
| 17 | LSP client (clangd built-in) | `coding-agent/lsp/*` | none | **PORT/SIDECAR** | S | clangd for embedded C/C++ |
| **Engine / UI** |
| 18 | Headless engine (`createAgentSession`, `--mode rpc`) | `coding-agent/session/*` | C++ core loop | **KEEP C++ authoritative + SIDECAR**; FORK later (optional) | — / L | CAD engine + governance already ahead |

---

## 3. Firmware workstream (extend, not greenfield)

CAD already has a **mature firmware pillar**: `core/firmware/` (C++ engine: project/toolchains/
emulator/hardware/pinouts/platform_db/scenario/coverify/powerprofile), `services/mcp/
firmware_tools.py` (MCP surface), `host/src/ui/firmware/fw_workspace.cpp` (GUI). It does
CMake/ESP-IDF/arduino-cli builds, Renode/QEMU emulation, probe-rs/esptool flash, shallow debug.

**Gaps to fill (all attach at `core/firmware/subprocess.{h,cpp}`):**
- No GDB/GDB-MI, no OpenOCD, no raw JTAG/SWD → **GLUE**.
- No AVR toolchain/avrdude/simavr → **GLUE**.
- No in-process serial/USB (subprocess-only) → **GLUE** (pyserial/pyusb).
- No firmware slash-commands/triggers → **PORT** (from #14).
- No external MCP client → **SIDECAR/PORT** (from #13).

**Recommended minimal glue set for "any AVR or ARM" (4 external processes, all DAP or GDB/MI):**
- **probe-rs** — ARM debug + flash, **DAP-native** → slots straight into OMP's DAP client.
- **OpenOCD** — GDB/MI fallback for exotic ARM chips.
- **avrdude** (AVR flash) + **simavr** (AVR debug/emulate, built-in GDB server).
- **Renode** — ARM emulation (native GDB server, scriptable).
- Transport: **pyserial** + **pyusb/libusb**. Toolchains: **arm-none-eabi-gcc**, **avr-gcc**,
  **clangd** (`--query-driver` for bare-metal includes).
- Non-DAP tools (OpenOCD/Renode/simavr/QEMU/J-Link/BMP) driven uniformly via
  `gdb --interpreter=mi2`, so one adapter path covers them all.
- Prior art: `embedded-debugger-mcp` (wraps probe-rs as an MCP server) — design reference.

**Deprioritize** OMP's Python/Julia coding tooling; keep the C/C++/asm + embedded path.

---

## 4. Arduino-inspired embedded IDE UI (concept)

Evolve `fw_workspace.cpp` into a single, properly-scaled ImGui view with everything visible at
once (Arduino-IDE ergonomics, pro-grade depth):

```
┌ Target bar: [Board/MCU ▼][Toolchain ▼][Port ▼]  [Build][Flash][Debug][Monitor] ┐
├─────────────┬───────────────────────────────┬──────────────────────────────────┤
│ Project /   │  Code editor (clangd LSP:      │  AI copilot pane                 │
│ board tree  │  diag, complete, hover)        │  (chat + inline fix + role pick) │
│             ├───────────────────────────────┤  driven by CAD agent + bridge    │
│ Pinmap /    │  Debug: registers, memory,     │                                  │
│ peripherals │  breakpoints, call stack, vars │                                  │
│             │  (DAP: probe-rs / gdb-MI)      │                                  │
├─────────────┴───────────────────────────────┴──────────────────────────────────┤
│ Bottom dock: Build log | Serial monitor/plotter | Flash progress | GDB console  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

Both human and AI drive the same actions (build/flash/debug/monitor) through the same
`firmware_tools` MCP surface, so the AI's steps are visible and reproducible in the UI.

---

## 5. Snapcompact revamp (safety) — if we adopt the image tier

1. **Recoverable side-channel**: foveation/doc-column drops write an `artifact://`-style
   recoverable payload (mirror CAD's `shake_messages` placeholders) instead of a count-only stub.
2. **Per-frame checksums** validated on load and after every IPC hop across the bridge.
3. **Text-only fallback tier** when no vision model / no native binary — CAD's `compaction.py`
   is effectively the reference impl already.
4. **Cap fixes**: make the 80-frame ceiling caller-raisable; never silently delete the oldest
   dense-middle slice without a recoverable archive.

---

## 6. Open decisions (answered via the questions accompanying this doc)

- **Q1 Strategy**: curated selective reuse (recommended) vs aggressive fork vs minimal providers-only.
- **Q2 Memory**: replace `project_memory` with mnemopi / keep CAD / hybrid.
- **Q3 Snapcompact**: adopt image tier now (native+vision) / text-fallback-first revamp / keep CAD text-only.
- **Q4 Provider auth**: port typed-error+resolver only / full AuthStorage sidecar (multi-account pools).
- **Q5 Firmware glue set**: full recommended set / ARM-only subset / AVR-only subset.
- **Q6 Firmware UI**: build the unified Arduino-style `fw_workspace` now / defer.
- **Q7 Distribution**: confirm bundling `bun` (+ native `pi-natives` if snapcompact image tier) in the installer is acceptable.
- **Q8 First implementation slice** to land after decisions.

---

## 7. Verification corrections (graphify + source, 2026-07-23)

Five verification subagents cross-checked the plan against CAD's graphify graph and source.
Net verdict: strategy holds; several load-bearing details corrected. Dossiers: `research/verify-*.md`.

### 7.1 Provider seam — MAJOR correction (changes matrix row 1)
- **The live GUI agent run is 100% native C++.** `core/agent/config.cpp::resolve_provider` +
  `core/agent/providers.cpp::complete()` (libcurl/WinHTTP) run in-process in the core;
  `run.cpp::make_provider` builds ONE `ProviderFn` per run.
- `services/agent/providers/*` + `config.py::_resolve_provider` is a **parallel, parity-tested
  Python implementation that is DEAD for GUI runs** — used only by advisor / MCP tools / tests.
  `worker_rpc.py` carries auth/session/skills RPCs, **never completions**.
- **Therefore the bridge insertion point for the LIVE agent is a new provider branch inside
  `core/agent/providers.cpp::complete()` (C++ → bun bridge over local socket/stdio)** — not the
  Python resolver. (Rewiring the Python resolver only reaches advisor/MCP/tests.) This is a
  bigger, C++-side lift than originally sketched; call it out in Q1/Q8.
- Contract corrections: **drop `temperature`** (no provider accepts it; Anthropic rejects it —
  only `reasoning_effort` is tunable) and **drop `_mega6_degraded`** (zero occurrences in repo).
  Load-bearing provider_cfg keys are `name`, `supports_vision`, `max_tokens`, `reasoning_effort`;
  catalog meta (context/vision/efforts) comes from a separate `agent_list_models` RPC.
- Cursor is the only provider with no native C++ transport; today it dead-ends in a refusal.

### 7.2 Bun bridge — CONFIRMED end-to-end
- A throwaway bun script imported `getBundledModel` (`@oh-my-pi/pi-catalog`) + `stream`
  (`@oh-my-pi/pi-ai`) from TS source (no build step), built a real Model+Context, and
  `stream().result()` did a live HTTPS round-trip returning a resolved `AssistantMessage`
  (`stopReason:"error"`,`errorStatus:401`) for a fake key. Pipeline works standalone.
- Corrections: no tsconfig `paths` — resolution is bun-workspace `node_modules` + package
  `exports`, so the bridge must run `bun install` once (gitignored, ~2s). `.result()` **embeds
  provider errors as data** (check `stopReason`/`errorStatus`), it does not throw. `bun:sqlite`
  is imported unconditionally → **bun (not Node) is a hard runtime dep**.

### 7.3 Event/engine contract — corrections (affects FORK/engine reuse cost)
- OMP `--mode rpc` is an **async, uncorrelated** AgentSessionEvent stream; CAD `worker_rpc/2` is
  **call-scoped** (`{id,method,params}`→`{id,event,data}*`→`{id,ok,result}`). A translator must
  **synthesize CAD span/session ids** per OMP session/turn/tool-call.
- OMP `text_delta` (final-answer streaming) has **no home** in `agent_events/2` — needs a
  separate chat-message seam. `agent.thought.delta` and `agent.spawn` currently have **no GUI
  consumer** in `ai_workspace.cpp` (host work required to make them visible).
- CAD's `Provider.complete/complete_ex` is non-streaming; true live-typing UX is a separate
  streaming-seam project, orthogonal to the OMP integration.

### 7.4 Tool gate — CONFIRMED + hardened
- `services/mcp/tool_registry.py` `_FamilyView`/`_guard` is the single registration+dispatch gate
  ({family,risk}, write-lease, allow-list). Keep authoritative.
- New firmware/MCP-client tools must register on **both** the live surface (`_reg.view(mcp,fam)`)
  **and** the DAG-verify battery surface (`services/dag/verify.py`) for parity — firmware already
  does this. **CAD has zero MCP-client code** (exhaustive grep) — external MCP consumption is
  net-new and its discovered tools must be wrapped through the same gate.

### 7.5 Firmware seam — corrections (sharpens the GLUE targets)
- Debugger/programmer attach point is **`core/firmware/hardware.cpp`** (`probe_rs_flash/reset/
  halt/read_mem`, `esptool_flash`), which calls the generic `subprocess.cpp`. New
  OpenOCD/GDB/avrdude backends extend `hardware.cpp`, not `subprocess.cpp`.
- Fully wired today: **probe-rs (Cortex-M)** flash/reset/halt/mem-read, **esptool (ESP32)** flash.
- Real gaps (the GLUE work): **AVR has no flash backend (no avrdude anywhere)**, **ESP32 has no
  debug/GDB path**, **no OpenOCD/GDB anywhere**, **no live physical serial/UART monitor** (the
  console only shows emulator output), and `fw_debug_read_mem` has an RPC/tool but **no UI
  control**. `contracts/firmware_rpc.md` is **append-only §1–§11** (the §5 table is stale).
- `host/src/ui/firmware/fw_workspace.cpp` (left project rail / center build+emulate / right
  Flash+Debug rail, async `hwCall()`) is the ready seam for the unified Arduino-style view.
