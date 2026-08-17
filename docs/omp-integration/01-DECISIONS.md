# OMP → CAD Integration — Locked Decisions (2026-07-23)

Authoritative record of the choices made after the research + verification rounds. Supersedes
the "Open decisions" section of `00-OPTIONS-AND-DECISIONS.md`. Implementation follows these.

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
  the local suite under `CHUDWARE_COMPACTION_BACKEND=omp`; the bridge's `compact` RPC itself
  remains for the C++ side. OG source + frames persist in `services/agent/snap_store.py`
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

## Runtime / distribution
- Bundle **bun** (hard dep: `bun:sqlite`) in the AppDir/installer, per-OS/arch. Bundle
  **pi-natives** only where the snapcompact image tier runs. Bridge runs `bun install` once
  against the vendored workspace (gitignored node_modules).

## Implementation order (YAGNI, focus-first)
1. **Foundation**: vendoring layout + `scripts/vendor_omp.py` (pinned SHA, whitelist, lockfile);
   bun bridge with `complete` op (providers); proven end-to-end.
2. **Live providers + UI sign-in**: C++ branch in `providers.cpp` → bridge; provider catalog +
   sign-in UI (designer-driven).
3. **Cost + cache**: cost table; cache-warming + cheaper-model-after-unload options.
4. **Memory hybrid** (mnemopi behind project_memory API).
5. **Snapcompact** image tier + safety revamp.
6. **Firmware** glue breadth + unified IDE UI.
