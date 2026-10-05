# Verify: firmware pillar seams (debugger/programmer/serial + Arduino-style IDE UI)

> **Historical research (2026-07), superseded 2026-08-31. Not a current runbook.** This dossier
> is dated evidence from the OMP integration research round. Its bun bridge/sidecar,
> `vendor/omp/` and `scripts/vendor_omp.py` were deleted. Any `scratch/omp-src/` or
> inspection-checkout path is an untracked local research copy, not a dependency. Its CAD
> file/line citations are point-in-time. Online agent execution, provider auth, models,
> sessions, memory and compaction now run in the embedded Chudcode RPC runtime. See
> [01-DECISIONS.md, 2026-08-31 section](../01-DECISIONS.md#2026-08-31--superseding-architecture-chudcode-rpc-is-the-only-online-runtime),
> [ARCHITECTURE.md, Agent runtime](../../../ARCHITECTURE.md#agent-runtime), and
> [`.claude/skills/cad-chudcode-integration/SKILL.md`](../../../.claude/skills/cad-chudcode-integration/SKILL.md).

## Summary of corrections (lead)

1. **The plan's "gaps" claim needs sharpening: probe-rs debug/flash and esptool flash already
   exist end-to-end (core → RPC → MCP → UI buttons). What's genuinely missing is (a) any GDB
   protocol/stub attach point, (b) any raw serial/UART monitor (live device UART, as opposed to
   emulator UART capture), and (c) `avrdude`/openocd support (Arduino toolchain builds via
   `arduino-cli` but has NO flash/debug backend — only cortex-m via probe-rs and esp32 via
   esptool are wired).** CONFIRMED via `core/firmware/hardware.cpp`, `provision.cpp` family
   table (avr has `can_flash: {[], []}` — nothing populates it).
2. `core/firmware/subprocess.{h,cpp}` is a **generic, backend-agnostic** subprocess runner
   (`which()` + `run_capture()`only) — it is NOT itself a debugger/programmer API; the
   tool-specific argv construction (`probe-rs download/reset/read`, `esptool.py write_flash`)
   lives in `core/firmware/hardware.cpp`, one layer up. A new debugger backend (openocd/gdb/
   avrdude) would add a new `hardware.{h,cpp}`-style wrapper function that calls
   `run_capture()`, NOT touch subprocess.cpp itself. CORRECTED (dossier apparently pointed at
   subprocess.{h,cpp} as *the* attach API; the real attach point one level up is `hardware.cpp`).
3. `contracts/firmware_rpc.md` §5's table is stale/incomplete versus the actual RPC surface:
   it lists only `fw_flash` (5 tools) but §8 (appended later) documents the real GUI-facing
   set including `fw_debug_start`, `fw_debug_halt`, `fw_debug_read_mem`, `fw_status`,
   `fw_projects`. The MCP tool file additionally has `fw_sensor_scenario` and
   `fw_verify_board`/`fw_power_profile` not in §5's table either — CONFIRMED the contract is
   append-only/layered, not a single frozen table; must read all of §1–§11, not just §5.
4. Everything else in the plan's factual claims (core_bridge JSON-RPC path, register_firmware_tools
   pattern, fw_workspace panel layout) is CONFIRMED accurate.

## 1. `core/firmware/subprocess.{h,cpp}` — external-process launch API

File: `core/firmware/subprocess.h` (35 lines), `core/firmware/subprocess.cpp` (278 lines).
Namespace `chudware::firmware`.

```cpp
struct RunResult {
    int returncode = -1;       // -1 = tool not found / timeout
    std::string stdout_text, stderr_text;
    bool timed_out = false;
    bool not_found = false;
};
std::string which(const std::string& name);                 // PATH resolution
RunResult run_capture(const std::vector<std::string>& argv,
                      const std::string& cwd, double timeout_s);  // fork+exec (POSIX) or
                                                                     // CreateProcess (Win32)
```

- POSIX impl (subprocess.cpp:175-273): `fork()`/`execvp()`, non-blocking pipes + `poll()` loop,
  wall-clock timeout kills with `SIGKILL`, newline-normalizes stdout/stderr (parity with Python
  `subprocess.run(text=True)`).
- Windows impl: `CreateProcessA` + pipes, same timeout/kill semantics.
- **Never raises; never blocks the caller thread indefinitely** — this is the correct base for a
  new debugger/programmer wrapper (openocd, real gdb, avrdude) to build on.
- **This file has NO knowledge of any specific tool.** The actual "attach points" a new debugger
  integration extends are the callers one directory-flat level up:
  - `core/firmware/hardware.cpp` (`hardware.h`) — `probe_rs_flash()`, `probe_rs_reset()`,
    `probe_rs_halt()`, `probe_rs_read_mem()` (throws on failure, caught by RPC layer),
    `esptool_flash()`. Each builds an argv vector and calls `run_capture()`.
  - `core/firmware/toolchains.cpp` — build-side subprocess use (cmake/ninja/idf.py/arduino-cli).
- **Verdict: CONFIRMED** as the process-launch primitive; **CORRECTED** framing — a new
  debugger/programmer integration point is `hardware.{h,cpp}` (adding e.g.
  `openocd_flash()`/`gdb_attach()`/`avrdude_flash()` following the `probe_rs_*` pattern), not
  `subprocess.{h,cpp}` itself, which stays generic.

## 2. `services/mcp/firmware_tools.py` — `register_firmware_tools(mcp)` / `core_bridge.call`

File: `services/mcp/firmware_tools.py` (405 lines). Pattern: identical to `ecad_tools.py`.

- `register_firmware_tools(mcp)` (L35) registers `@mcp.tool()`-decorated closures inside itself;
  each tool body is a thin wrapper: build a `params: dict`, call `cb.call("<rpc_method>", params)`
  (`cb` = `from . import core_bridge as cb`, L19), `json.dumps(...)` the result.
- Full tool roster (confirmed by reading the whole file): `fw_status`, `fw_new_project`,
  `fw_build`, `fw_sources`, `fw_sim_run`, `fw_verify_board`, `fw_power_profile`, then a labeled
  **"Hardware path (T-C4)"** section (L262-331): `fw_flash`, `fw_debug_start`, `fw_debug_halt`,
  `fw_debug_read_mem`, then `fw_sensor_scenario` (L333-405).
- **To register a new tool** (e.g. `fw_serial_open`/`fw_gdb_attach`): add another
  `@mcp.tool()` closure inside `register_firmware_tools`, call `cb.call("<new_rpc_name>", params)`,
  and add the matching `rpc_<name>()` handler + `call()` dispatch entry in
  `core/firmware/firmware_rpc.cpp` (confirmed via graphify: `rpc_new_project`, `rpc_build`,
  `rpc_sim_run`, `rpc_verify_board`, `rpc_power_profile`, `rpc_debug_read_mem`, `rpc_projects`,
  and a top-level `call()` dispatcher at firmware_rpc.cpp:557).
- `core_bridge.call` path: `services/mcp/core_bridge.py` — `call(method, params)` is a **ctypes**
  bridge, NOT literally "JSON-RPC over a socket" for the in-process MCP-server mode. It loads
  `libchudware.{so,dll,dylib}` once (`_load()`, L196-256), and the single exposed entry point is
  `dll.chudware_call(method: char*, params_json: char*, &out_json: char**)` (string-in/string-out
  JSON). There is a separate `daemon_bridge` mode (`CHUDWARE_ENGINE_DAEMON`) referenced at L207-219
  that routes over a real socket to `chudware-hostd` instead of loading the DLL in-process — the
  in-process ctypes call and the daemon socket call are two alternate transports behind the same
  `cb.call()` surface (not read in full here; flagged as present but out of this dossier's scope).
- **Verdict: CONFIRMED** exactly as the plan describes: register a tool → `cb.call(rpc_name, params)`
  → `chudware_call()` C-ABI → `core/firmware/firmware_rpc.cpp` dispatcher → handler.

## 3. `host/src/ui/firmware/fw_workspace.cpp` — panels + build/flash/emulate invocation

File: 1089 lines, `namespace ui::firmware`. Header comment (L1-17) states layout precisely:

- **Left rail**: project list (`fw_projects`, ASYNC load via `st.rpc.call`) + New Project +
  selected project's sources.
- **Center**: Build section (`fw_build` → parsed-warnings console) over Emulation section
  (platform + scenario assertions → `fw_sim_run` → live UART console) over GPIO trace viewer
  (renders `aicad.firmware.trace/1` waveforms per contract §7).
- **Right**: backend doctor (`fw_status`, T-FIN4) + **Flash/Debug** — buttons exist only when
  doctor reports the backend present, else a guidance banner.
- **Hardware rail wiring** (L948-987): a single `char chipBuf[64]` input (default
  `"STM32F407VGTx"`), a local `hwCall(method, params)` lambda that does `st.rpc.call(method, ...)`
  async and stores the human-readable result in `s.hwOut`. Three buttons:
  - `"Flash ELF"` → `hwCall("fw_flash", {elf_path, chip})` (L971-973), gated on `canHw && haveElf`.
  - `"Debug: run"`/similar → `hwCall("fw_debug_start", {chip})` (L977-978).
  - `"Debug: halt"` → `hwCall("fw_debug_halt", {chip})` (L979-981).
  - **No `fw_debug_read_mem` UI control exists** — the RPC/tool exists but the workspace has no
    register/memory viewer widget calling it. CONFIRMED gap (memory-inspection UI is unbuilt,
    though the RPC plumbing is there).
  - **No serial/UART live-device monitor panel** — the only UART console shown is the
    `fw_sim_run` emulator UART log (`s.uartLog`, populated by the emulation section), not a live
    physical-device serial port reader. CONFIRMED gap.
- All calls go through `st.rpc.call(method, json_params, callback)` — the same worker-RPC path
  the rest of the host GUI uses (see `AppState::rpc` — not the MCP path; consistent with
  contract §8's statement that the GUI talks over `worker_rpc`, not MCP).
- **Extension seam for a single "board→compile→flash→debug/serial" view**: this file's existing
  vertical layout (rail / build+emulate+trace / hardware) is the natural place to add a
  **Serial** subsection next to Flash/Debug in the right-hand hardware rail, plus a **Debug**
  subsection with register/memory views calling the already-registered `fw_debug_read_mem`. Both
  would follow the exact same `hwCall()`-async-lambda pattern already in place — no new
  architecture needed, just new widgets + (for serial) a new RPC method and hardware.cpp backend
  (there is currently no live serial-port read/write RPC at all, emulated or physical).

## 4. `contracts/firmware_rpc.md` — contract shape

- **Not a single frozen table.** It's an append-only living doc: §1 (toolchain posture/licensing),
  §2 (`FirmwareProject`), §3 (`BuildResult`), §4 (`SimResult`), §5 ("Tool signatures" — **stale**,
  only lists 5 of the ~12 actual tools), §6 (`compile_commands.json`), §7 (trace artifact,
  T-FIN1, appended), §8 (**"Worker endpoints — GUI seam", T-FIN5, appended** — this is where
  `fw_debug_start`/`fw_debug_halt`/`fw_debug_read_mem`/`fw_status`/`fw_projects` are actually
  documented), §9 (sensor scenarios, T-FIN3), §10 (`fw_verify_board`, T-FIN6), §11 (power
  profile, T-FIN7).
- Error envelope fixed since §1: `{code, message, detail}`, same as `worker_rpc.md`/`ecad_rpc.md`.
- Licensing posture (§1, binding): every toolchain/programmer backend is invoked **as an
  unmodified subprocess only** — never linked into `libchudware`. This is the constraint any new
  debugger (openocd GPL-2, gdb GPL-3, avrdude GPL-2) MUST honor — subprocess-only, exactly the
  `hardware.cpp` + `subprocess.cpp` pattern already used for probe-rs/esptool.
- **Verdict: CONFIRMED** shape/intent; **CORRECTED** — do not treat §5 as the complete tool list;
  the debug/GUI methods are in §8, appended later. Any new tool-doc addition should append a new
  numbered section, following the file's own convention, not edit §5's table (which is
  historically stale already).

## Existing serial/USB/GDB code — precise confirmation of the gap

Grepped `core/firmware/*` for `probe-rs|esptool|openocd|avrdude|gdb`:
- `probe-rs` and `esptool.py`/`esptool` are the **only** hardware-facing tools referenced anywhere
  in `core/firmware/` (hardware.cpp, firmware_rpc.cpp, provision.cpp, platform_db.cpp).
- **Zero matches** for `openocd`, `avrdude`, or `gdb` anywhere under `core/firmware/`.
- **No serial-port UART reader** (e.g. termios/pyserial-equivalent open/read loop) exists in
  `core/firmware/` — `fw_sim_run`'s `uart_log` is captured from the **emulator** (Renode/QEMU
  process stdout), not a physical serial device.
- AVR family (arduino-cli build backend) has a `can_flash` capability struct that is **never
  populated** with a required-tool list in `provision.cpp`'s `kFamilyRules` (confirmed at the
  `families["avr"]` block, L67-70 region) — i.e., AVR builds via arduino-cli but nothing flashes
  it. This directly confirms the plan's "gaps" claim for AVR/avrdude specifically, while
  correcting it for cortex-m (probe-rs debug: CONFIRMED present) and esp32 (esptool flash:
  CONFIRMED present, but ESP32 debug/gdb: CONFIRMED absent — no JTAG/gdbserver wrapper for esp32
  either).

## Attach-point summary table

| capability | exists today | file:function |
|---|---|---|
| cortex-m flash | YES (probe-rs) | `hardware.cpp:probe_rs_flash` L70-76 |
| cortex-m reset/run | YES (probe-rs) | `hardware.cpp:probe_rs_reset` L79-83 |
| cortex-m halt | YES (probe-rs) | `hardware.cpp:probe_rs_halt` L86-90 |
| cortex-m mem read | YES (probe-rs) | `hardware.cpp:probe_rs_read_mem` L92-~135 |
| esp32 flash | YES (esptool.py) | `hardware.cpp:esptool_flash` L140-149 |
| esp32 debug/gdb | NO | — |
| avr flash (avrdude) | NO | `provision.cpp` avr family: `can_flash` unpopulated |
| any real GDB stub/attach | NO | — |
| live serial/UART monitor (physical device) | NO | — |
| generic subprocess launch primitive | YES | `subprocess.h/.cpp: which()/run_capture()` |
| MCP tool registration pattern | YES | `firmware_tools.py:register_firmware_tools` |
| GUI RPC mirror (worker_rpc) | YES | `firmware_rpc.md §8`, `fw_workspace.cpp` hwCall lambda |
