# Contract: Firmware Toolchain RPC — v1

> The firmware pillar's tool surface: create an embedded-firmware project, build it with a real
> cross-toolchain, run a lightweight instruction/IO simulation, and flash a device. Mirrors the
> ECAD precedent (`ecad_rpc.md`): a self-contained engine module (`engine/firmware/`) owns the
> schema + persistence + toolchain wrappers; the MCP bridge (`services/mcp/server.py`, registered
> **day 2**) and the JSON-RPC worker call that engine **directly**. Error envelope is identical to
> `worker_rpc.md` / `ecad_rpc.md`: `{ "code", "message", "detail" }`.
>
> A firmware project may be **linked to an ECAD board document** via `board_doc_ref` (§2) — the
> same loose id/path reference `ecad_update_from_sch` uses to pair a board and a schematic. The
> firmware pillar never edits the board doc; the link is one-way metadata so the agent can pull
> MCU/pin context from the board when generating sources.

## 1. Toolchain posture (licensing)

Every toolchain backend is invoked **as an unmodified subprocess** — never linked into `libchudware`
or any shipped binary. This is the same isolation policy `contracts/licenses.md` grants ngspice:
mere aggregation of a separate GPL tool does not impose its license on our code. No toolchain
source is copied or vendored; we drive documented CLIs only.

| backend | tool | license | how invoked |
|---|---|---|---|
| `cmake` (default) | CMake + `arm-none-eabi-gcc` | BSD-3 / GPL-3-with-runtime-exception | subprocess, cross-compile |
| `espidf` | ESP-IDF `idf.py` | Apache-2.0 | subprocess |
| `arduino` | `arduino-cli` | GPL-3 | subprocess |

The default baseline is CMake + `arm-none-eabi` (zero ToS risk, GPL-clean by aggregation). ESP-IDF
and Arduino are alternate backends selected per project. A missing tool on `PATH` is a **graceful
error** (`toolchain_not_found`), never a crash and never a test failure.

## 2. `FirmwareProject` record

Persisted as a JSON sidecar (`project.json`) in the project directory (atomic tmp+replace, the
`engine.ecad.autosave` idiom). Schema tag `aicad.firmware.project/1`.

| field | type | notes |
|---|---|---|
| `schema` | str | `"aicad.firmware.project/1"` |
| `project_id` | str | 12-hex id (uuid4), also the project dir name |
| `name` | str | display name |
| `board_doc_ref` | str \| null | ECAD board doc_id or `.ecad+` path this firmware targets (§0) |
| `mcu_family` | str | e.g. `"cortex-m4"`, `"esp32"`, an Arduino FQBN |
| `toolchain` | str | `"cmake"` \| `"espidf"` \| `"arduino"` |
| `src_paths` | [str] | source files, **relative to the project root** |
| `build_dir` | str | build output dir, relative to project root (default `"build"`) |
| `root` | str | absolute project root (not persisted verbatim — recomputed on load) |
| `created_at` | float | epoch seconds |

## 3. `BuildResult`

Returned by `fw_build` and `Toolchain.build(project)`.

| field | type | notes |
|---|---|---|
| `success` | bool | true iff the toolchain returned 0 and an artifact was produced |
| `elf_path` | str \| null | absolute path to the built `.elf` (or `.bin`/`.hex`), null on failure |
| `map_path` | str \| null | absolute path to the linker `.map`, null if not produced |
| `stderr` | str | captured toolchain stderr (truncated to a sane bound) |
| `warnings` | [str] | parsed compiler warnings |
| `returncode` | int | subprocess exit code (`-1` = tool not found / timeout) |
| `cmd` | [str] | the argv actually run (for reproducibility) |
| `ms` | float | wall-clock milliseconds |

## 4. `SimResult`

Returned by `fw_sim_run`. A lightweight functional check (UART capture + GPIO snapshot +
assertions); the heavy instruction-accurate backends land in a later slice.

| field | type | notes |
|---|---|---|
| `passed` | bool | true iff every assertion passed |
| `uart_log` | str | captured UART/stdout text |
| `gpio_states` | {str: int} | pin → level at end of run |
| `assertion_results` | [{`name`, `passed`, `detail`}] | per-assertion outcome |
| `ms` | float | wall-clock milliseconds |

## 5. Tool signatures

MCP tools (external/agent callers) and the mirrored worker RPC methods. Params → result. Every
result may instead be the error envelope.

| tool / method | params → result |
|---|---|
| `fw_new_project` | `{name, mcu_family, toolchain?, board_doc_ref?}` → `{project_id, project}` (creates the project dir + CMake template + `compile_commands.json`) |
| `fw_build` | `{project_id}` → `BuildResult` |
| `fw_sources` | `{project_id, add?:[str], remove?:[str]}` → `{src_paths}` (list, or add/remove source files) |
| `fw_sim_run` | `{project_id, elf_path?, scenario?}` → `SimResult` |
| `fw_flash` | `{project_id, port?, elf_path?}` → `{flashed:bool, stderr, returncode}` |

`toolchain` defaults to `"cmake"`. `fw_build` / `fw_flash` return a `toolchain_not_found` error
(not a raise) when the backend CLI is absent from `PATH`.

## 6. `compile_commands.json`

The CMake template sets `CMAKE_EXPORT_COMPILE_COMMANDS ON`, so a real configure emits an accurate
`compile_commands.json` (clangd/IDE tooling). Because a cross-toolchain may be absent in CI,
`fw_new_project` **guarantees the file exists** even without CMake: if a configure is not possible
it writes a synthetic `compile_commands.json` enumerating the template sources, which a later real
configure overwrites. The artifact is always present after `fw_new_project`.

## 7. Simulation trace artifact — `aicad.firmware.trace/1` (T-FIN1, appended)

`fw_sim_run` now emits a structured JSON trace artifact next to the emulated ELF
(`<elf>.trace.json`, atomic tmp+replace; best-effort — a write failure yields
`trace_path: null`, never an error). It is the stable seam for later GUI/scenario work.

| field | type | notes |
|---|---|---|
| `schema` | str | `"aicad.firmware.trace/1"` |
| `source` | str | `"renode"` \| `"qemu"` |
| `generated_at` | float | epoch seconds |
| `events` | [event] | interleaved pin + UART events, ordered by `seq` (log line order) |
| `gpio_final` | {str: int} | end-of-run pin → level snapshot (same data as SimResult `gpio_states`) |

Event object:

| field | type | notes |
|---|---|---|
| `type` | str | `"gpio"` \| `"uart"` |
| `seq` | int | source log line index — total order across both event types |
| `t_ms` | float \| null | virtual-time milliseconds when the emitting side supplied one, else null |
| `pin` | str | gpio only — pin name, e.g. `"PA5"` |
| `level` | int | gpio only — 0 or 1 |
| `text` | str | uart only — the log line (trace marker lines are excluded) |

### 7.1 SimResult additions (extends §4, append-only)

| field | type | notes |
|---|---|---|
| `gpio_events` | [event] | the gpio-type events (same shape as §7), time-ordered |
| `trace_path` | str \| null | absolute path to the trace artifact, null when not written |

### 7.2 GPIO extraction sources

- **Renode**: the generated `.resc` registers a per-pin `AddStateChangedHook` for every pin
  referenced by a `gpio:P<X><n>...` assertion (only when the platform declares `gpioPort<X>`;
  `stm32f4_discovery.repl` now declares ports A–E), each printing an
  `CHUDWARE_GPIO_EVT <PIN>=<level>` line; after the run sleep it reads each referenced port's
  ODR between `CHUDWARE_GPIO_ODR_BEGIN/END <port>` echo markers for the final snapshot
  (snapshot wins over the last event on merge). Hook prints carry no timestamp
  (`t_ms: null`); ordering is preserved via `seq`.
- **QEMU** (semihosting shim): QEMU exposes no external pin observer, so the firmware
  reports its own transitions via `CHUDWARE_GPIO_TRACE(pin_str, level, t_ms)` /
  `CHUDWARE_GPIO_TRACE_NT(pin_str, level)` from the project template header
  `include/chudware_trace.h` — a printf of the same `CHUDWARE_GPIO_EVT` line
  (optionally `t=<n>ms`, also accepting `us`/`s`) over the semihosting console.

### 7.3 Timeline assertion form (extends the `fw_sim_run` assertion grammar)

`gpio:<PIN>=<level>@><T>ms` (after T ms), `gpio:<PIN>=<level>@<<T>ms` (before),
`gpio:<PIN>=<level>@<T>ms` (at-or-after). Evaluated against **time-stamped** gpio events
only; when the pin has none, the assertion fails with detail
`"no time-stamped events for pin '<PIN>'"`. The plain `gpio:<PIN>=<level>` form keeps its
§4 meaning: end-of-run level.

## 8. Worker endpoints — GUI seam (T-FIN5, appended)

The desktop Firmware workspace (host GUI) talks to the engine over the JSON-RPC worker
(`contracts/worker_rpc.md`), not MCP. `engine/firmware/worker_rpc.py` registers a
GUI-facing mirror of the fw_* tools into `engine_oracle/worker.py`; every method is a thin
adapter over the SAME `engine.firmware.mcp_tools` implementation the MCP server uses, so
tool and GUI behavior can never drift.

Registered methods: `fw_new_project`, `fw_build`, `fw_sources`, `fw_sim_run`, `fw_flash`,
`fw_debug_start`, `fw_debug_halt`, `fw_debug_read_mem` — identical params/results to §5 —
plus two GUI conveniences:

| method | params | result |
|---|---|---|
| `fw_status` | `{}` | the T-FIN4 provisioning doctor payload `{ok, tools, families, banners, missing}` — the GUI gates Build/Emulate/Flash/Debug buttons on `families.*.can_*` and shows `banners` verbatim as install guidance |
| `fw_projects` | `{}` | `{projects: [FirmwareProject dict (§2) + "root_dir": absolute path]}`, newest first; unreadable sidecars skipped |

Error posture is unchanged from §1: expected failures come back inside the result
(`{code, message, detail}` envelope or `success=False` / `passed=False`), never a raised
worker error.

## 9. Sensor stimulus scenarios — `aicad.fwscenario/1` (T-FIN3, appended)

The previously-reserved `scenario` param of `fw_sim_run` (§5) is now defined: a declarative
set of time-series stimulus tracks bound to emulated peripherals, compiled into the generated
Renode `.resc` (`engine/firmware/scenario.py`). Accepted as a dict, a JSON string, or a
fixture reference `"fixture:<name>"` (library: `engine/firmware/fixtures/`, ≥4 sensor
archetypes: `i2c_temp_tmp103`, `spi_imu_stub`, `quadrature_encoder`, `adc_thermistor`,
`user_button`). Scenarios are **Renode-path only** — QEMU exposes no peripheral-stimulus
surface; a scenario on the QEMU/no-emulator path refuses honestly (`scenario_unsupported` /
`scenario_error` in `uart_log`, per-assertion `not evaluated` details), never a silent drop.
A structurally invalid scenario is the error envelope `{code:"invalid_scenario", ...}`.

| field | type | notes |
|---|---|---|
| `schema` | str | `"aicad.fwscenario/1"` |
| `name` | str? | display name |
| `tracks` | [track] | non-empty; each track has a `kind` from the table below |

Track kinds (per-kind fields beyond `kind`):

| kind | fields | Renode realisation |
|---|---|---|
| `gpio_edges` | `pin` (`"P<A-K><0-15>"`), `events: [{t_ms, level:0\|1}]` | `Miscellaneous.Button` gated onto the pin; `Press`/`Release` on the timeline |
| `button` | `pin`, `presses: [{t_ms, hold_ms?=50}]` | as above — press/release pair per entry |
| `quadrature` | `pin_a`, `pin_b`, `steps>=1`, `step_ms>0`, `start_ms?=0`, `direction?="cw"\|"ccw"` | two Buttons stepping the x4 Gray sequence 00→10→11→01 |
| `i2c_sensor` | `model:"tmp103"`, `bus?="i2c1"`, `address`, `temperature_c` | upstream `Sensors.TMP103` attached at the address; `Temperature` set at setup |
| `i2c_registers` | `bus?="i2c1"`, `address`, `response:[byte]` | `Mocks.DummyI2CSlave` + `EnqueueResponseByte` per byte |
| `spi_stub` | `bus`, `name?` | `Mocks.DummySPISlave` (fixed-pattern connectivity stub; plan carries a warning) |
| `uart_rx` | `uart?` (default: first UART in the .repl), `events: [{t_ms, text}]` | `sysbus.<uart> WriteChar <byte>` per byte on the timeline |
| `adc_waveform` | `channel?`, `sample_ms?=1`, `waveform: {type:"sine"\|"ramp", min, max, period_ms}` or `{type:"samples", values:[num]}` | `Python.PythonPeripheral` mapped over the family's real ADC register block (SR/DR), feeding 12-bit samples — **no ADC model ships upstream**; only families with a registered stub window (stm32f4, stm32f1) support this; others refuse with an explanation |

Timing model: timed actions are realised by monitor `sleep` interleaving (wall-clock, the
same idiom as the base run window); after every sleep the script echoes
`CHUDWARE_T_MARK <ms>`, which the parser uses to coarsely time-stamp subsequent UART lines
(`t_ms` on §7 uart events — previously always null, now filled when a scenario ran).
Renode-side stimulus syntax is [~] not live-verified (Renode absent on the dev box);
canned .resc-generation + schema tests carry coverage, per the §7 precedent.

### 9.1 Assertion vocabulary extensions (extends §4/§7.3, append-only)

| form | meaning |
|---|---|
| `toggles:<PIN><op><N>` (op: `>=` `<=` `==` `>` `<`) | the pin's level changed <op> N times over the run (event stream; timestamps not required) |
| `period:<PIN>=<P>ms±<TOL>ms` (also `+-`) | mean interval between the pin's time-stamped rising edges is within P±TOL ms; needs ≥2 stamped rising edges, else fails with detail |
| `uart_within:<text>@<T>ms` | a time-stamped UART event containing `<text>` occurs at or before T ms; without scenario time marks it fails with an explanatory detail |

`toggles:`/`period:` require the `gpio_observe` capability (like `gpio:`);
`uart_within:` requires `uart`.

## 10. Firmware ↔ board co-verification — `fw_verify_board` (T-FIN6, appended)

Closes the §2 one-way board link: the board generated `pinmap.h`; `fw_verify_board` checks the
firmware BACK against the board before fab. Engine handler `engine.firmware.mcp_tools.
fw_verify_board` (checks in `engine.firmware.coverify`), mirrored as an MCP tool and a §8 worker
method. **Advisory** — findings never block, same posture as ERC/DRC.

`{board_doc_ref, project_id?, sources?:{name:text}, sch_doc_ref?, mcu_family?, trace_path?,
emulate?:bool}` → `{ok, mcu_family, findings, summary, fw_pins, dynamic}` (or the §1 error
envelope for an unresolvable board/project or missing sources).

- Firmware pin references (`PIN_<TOKEN>` uses + read/write direction heuristics over the GPIO
  call vocabulary: STM32 HAL, `gpio_*`, Arduino, RP2040 SDK) come from `sources` when given,
  else the project's own source files; `pinmap.h` is excluded (declarations, not uses).
- **Static pass** (always): pin-map token resolution reuses `pinmap.pin_model` — the enriched
  per-pin record `{net, token, pin_id, positional, strap, input_only, direction, mux}` built
  with the exact net selection/dedup rules of `generate_pinmap`, so header and model can never
  disagree. `strap` / `input_only` come from per-family tables in `mcu_pinouts`
  (`strap_pins()`: STM32 BOOT0/BOOT1/NRST, ESP32 EN/GPIO0/2/12/15, AVR RESET; input-only:
  ESP32 GPIO34–39). Direction expectations need `sch_doc_ref` (pin types live on symbol pins);
  without it those checks are skipped honestly. Decoupling rails come from the T-FIN8 eng
  metadata (`supply.rails`, `contracts/ecad/boardphys.md`) plus power-named nets on the MCU's
  own pads.
- **Dynamic pass**: runs when `trace_path` points at a §7 `aicad.firmware.trace/1` artifact,
  or `emulate=true` launches the project's built ELF through `run_simulation` live (synthetic
  `gpio:` assertions install pin hooks for every firmware-written positional pin; only the
  observed events are consumed). Observed pin transitions are mapped back to board nets (exact
  net-name match, then decoded-pin-id equality) and diffed against netlist expectations.
  Missing emulators degrade gracefully (`dynamic = {ran:false, reason}`).

Findings use the ERC violation shape (`engine.ecad.erc`) extended append-only with a `fix`
hint: `{rule, severity: "error"|"warning", net: str|null, pins: [str], xy: null, message, fix}`.

| rule | severity | meaning |
|---|---|---|
| `fw_pin_unknown_net` | error | firmware references `PIN_<X>` but no board net produces that token |
| `fw_pin_not_on_mcu` | warning | the net exists but touches no MCU footprint pad |
| `fw_strap_repurposed` | error | a boot-strap / reset pin is used as a general GPIO by firmware |
| `fw_direction_clash` | error | firmware drives a net that already has another push-pull driver (schematic netlist) |
| `fw_drive_input_only` | error | firmware drives a physical pin with no output driver (e.g. ESP32 GPIO34–39) |
| `fw_missing_decouple` | warning | an MCU supply rail has no decoupling capacitor footprint on it |
| `fw_trace_drive_conflict` | error | emulation observed the firmware driving a strap / externally-driven / input-only net |
| `fw_trace_unmapped_pin` | warning | emulation observed activity on a pin with no corresponding board net |

`summary` = `{n, errors, warnings, rules}`; `fw_pins` = the per-token scan
`{reads, writes, refs, files}`; `dynamic` = `{ran, source: "trace"|"emulation"|null, reason?}`.

## 11. Power/duty-cycle profile — `aicad.powerprofile/1` (T-FIN7, appended)

A duty-cycle-weighted per-rail current profile derived from a §7 emulation trace plus
the T-FIN8 per-component rail metadata (`contracts/ecad/boardphys.md` `eng.supply.rails`).
**Bookkeeping, not SPICE** — datasheet envelope currents scaled by observed/derived
activity; the artifact's `model` field states this. Producer:
`engine/firmware/powerprofile.py` (`build_power_profile`). Consumers: system power
budget (T-FIN23); each rail's `schedule` is exactly the `{duty, period_s}` /
`{points:[[t_s,scale]], period_s}` heat-load schedule shape the transient-thermal path
accepts (T-FIN18) — no re-parsing of raw traces downstream.

Honesty rules (inherited from boardphys, binding): unknown currents stay `null`, never
an invented 0 (gaps listed in `coverage.unknown_active_current`); every duty/residency
figure names its source heuristic; unknowable activity is *assumed always active*
(conservative) and labelled as such.

| field | type | notes |
|---|---|---|
| `schema` | str | `"aicad.powerprofile/1"` |
| `model` | str | fixed honesty label: duty-cycle bookkeeping, not SPICE |
| `generated_at` | float | epoch seconds |
| `trace_ref` | str \| null | path of the §7 trace artifact this was derived from |
| `window_ms` | float \| null | run window (max stamped `t_ms`, or caller-supplied); null = no time axis |
| `cpu` | residency | CPU run/sleep residency (below) |
| `pins` | {pin: duty-record} | per-pin GPIO/PWM duty (below) |
| `rails` | [rail] | per-rail current profile (below); `[]` when no board/metadata |
| `coverage` | obj | `{components_total, components_with_rails, unknown_active_current:[ref:rail], notes:[str]}` |

`cpu` residency — `{active_frac, sleep_frac, active_s|null, sleep_s|null, source, note}`.
`source`: `"cpu_markers"` (optional firmware-side `CHUDWARE_CPU_TRACE(state, t_ms)` macro in
`chudware_trace.h` prints `CHUDWARE_CPU_EVT <run|sleep> t=<n>ms`; integrated piecewise, state
before the first marker assumed `run`) or `"assumed_active"` (no markers → 100 % active,
never guessed from UART silence).

`pins[pin]` duty-record — `{duty: float|null, period_s: float|null, toggles: int, source,
note?}`. `source`: `"stamped_events"` (high-time integrated over the window; level before
the first stamped event assumed 0 — ports reset low), `"unstamped_events"` (Renode hook
prints carry no `t`; duty null, toggle count reported), `"final_state_only"` (duty = the
end-of-run snapshot level), `"no_data"`. `period_s` = mean interval of ≥2 stamped rising
edges, else null.

`rails[*]`:

| field | type | notes |
|---|---|---|
| `name` | str | rail/net name (ground rails excluded) |
| `voltage_v_nom` | float \| null | first known nominal voltage among contributors |
| `i_avg_a` | float \| null | Σ per-component `duty·i_active + (1−duty)·i_sleep`; a null `i_sleep` omits the sleep term (avg is then a floor, noted in provenance); null when no contributor has a known active current |
| `i_peak_a` | float \| null | Σ known active currents (all contributors simultaneously active) |
| `residency` | obj | `{active_frac, sleep_frac, active_s, sleep_s}` (nulls when unknowable) — rail-level mean of contributor duties |
| `schedule` | obj \| null | `{duty, period_s}` — the T-FIN18 transient-thermal heat-load schedule shape (`{points, period_s}` reserved for future piecewise producers); `period_s` = dominant measured pin period, else 1.0 s (noted) |
| `provenance` | [str] | per-component lines: duty + its source heuristic + the currents used |

Component→activity mapping: MCU footprints (`mcu_pinouts.component_family`) follow the
CPU residency; a component whose pads sit on a net mapped to an observed trace pin
(net name == pin name, or decoded pin-id equality via `pinmap.pin_model` — the T-FIN6
rules) follows that pin's duty; everything else is assumed always active.

Surfaces:
- `fw_sim_run` — when the project is linked to a board (`board_doc_ref`) and a §7 trace
  was written, it best-effort emits the profile next to the ELF as `<elf>.power.json`
  and adds `power_profile_path: str|null` to its result (append-only extension of §4/§7.1;
  never fails the sim).
- `fw_power_profile` (MCP tool + engine handler `engine.firmware.mcp_tools.
  fw_power_profile`) — `{trace_path, project_id?, board_doc_ref?, mcu_family?,
  window_ms?}` → the profile dict (or the §1 error envelope for a missing/unreadable
  trace or unknown project). Explicit `board_doc_ref` beats the project link; with no
  board the activity half still returns (`rails: []`, coverage note).

## 12. Embedded debugger/programmer/serial backends — avrdude, OpenOCD+GDB/MI, simavr, Renode, live serial monitor (S-FW-CORE, appended)

Closes the three gaps `docs/omp-integration/research/verify-firmware-seam.md` and
`embedded-landscape.md` identified: no AVR flash backend, no real GDB protocol/stub attach
point, no live physical-device serial/UART monitor (as opposed to the §7 emulator UART
capture). Same toolchain posture as §1: every backend below is invoked as an **unmodified
subprocess only** (avrdude GPL-2.0+, OpenOCD GPL-2.0, gdb/`arm-none-eabi-gdb`/`avr-gdb`
GPL-3.0, simavr dual GPL/LGPL, Renode MIT) — never linked into `libchudware`. Attach point:
`core/firmware/hardware.{h,cpp}` (mirrors the existing `probe_rs_*`/`esptool_flash`
wrappers around `subprocess.cpp`'s `run_capture()`); dispatched via
`core/firmware/firmware_rpc.cpp`'s `call()`; MCP tools registered inside the SAME
`register_firmware_tools(mcp)` used for both the live agent surface (`server.py`, family
`"ecad"`) and the DAG-verify battery surface (`verify.py`'s private `battery-firmware`
FastMCP instance) — dual registration is automatic, no extra wiring, per
`docs/omp-integration/research/verify-tool-gate.md` §4.

### 12.1 GDB/MI as the uniform non-DAP adapter shape

None of OpenOCD, simavr, or Renode speak Microsoft DAP (only probe-rs does, already wired
via §5/§8) — they expose the older GDB remote-serial protocol. Per
`embedded-landscape.md`'s "DAP/GDB Integration Notes," every one of them is driven the SAME
way: launch the tool as a background GDB server, then run `<gdb> --batch --interpreter=mi2`
against it with a list of MI commands, capture the text output, tear the server down. This
is `hardware.cpp`'s `gdb_mi_session()` primitive, shared by `openocd_gdb_mi`, `simavr_run`
(gdb-attach mode), and `renode_run` (gdb-attach mode) — one adapter shape instead of a
bespoke shim per tool. **POSIX-only** in this pass (fork/exec background-process
management + SIGTERM/SIGKILL teardown); a non-POSIX build returns
`FlashResult{success:false, output:"unsupported_platform: ..."}`, never a crash.

Each debug RPC call is a **fresh** server+gdb attach/detach cycle (stateless, mirroring
`probe_rs_reset`/`probe_rs_halt`'s existing statelessness) — safe for reset/halt/read-mem on
real Cortex-M hardware because halt state (DHCSR `C_HALT`) is a hardware flag that survives
the host tool disconnecting, but a breakpoint programmed in one call is **not** guaranteed
still active by the time a later call's step/continue runs (no session persists the
OpenOCD/gdb process across calls yet — a future iteration would add a session-id registry
keeping the server resident).

### 12.2 `FlashResult` reuse

Every new backend below returns the SAME `FlashResult` shape §5 already defines for
`fw_flash`/`fw_debug_start`/`fw_debug_halt`: `{success, output, returncode, cmd, ms}`.
`output` is the raw captured tool text (gdb/MI transcripts included verbatim — register/
memory values from `fw_debug_gdb_read_mem` arrive as MI tuples, NOT decoded to a `data_hex`
field the way `fw_debug_read_mem`/probe-rs's memory read is).

### 12.3 AVR flash — `fw_avr_flash`

`avrdude -c <programmer> -p <mcu> [-P <port>] [-b <baud>] -U flash:w:<hex_path>:i`.

| params | result |
|---|---|
| `{hex_path, mcu, programmer?="arduino", port?, baud?=115200, timeout_s?=60}` | `FlashResult` |

Default `programmer="arduino"` matches the AVR family's arduino-cli build backend
(`provision.cpp`); `port` empty omits `-P` (programmer-default, e.g. USB HID programmers
like usbasp); `baud<=0` omits `-b`. Populates the previously-empty AVR `can_flash`
capability (`provision.cpp` `kFamilyRules["avr"]`, was `{[], []}` — now `{["avrdude"], []}`).

### 12.4 ARM debug via OpenOCD + GDB/MI — `fw_debug_gdb_*`

`openocd_cfg` is the OpenOCD `-f` argument (usually a board config pulling in both
interface and target, e.g. `"board/stm32f4discovery.cfg"`); the GDB server defaults to
OpenOCD's standard port 3333.

| method | params | result |
|---|---|---|
| `fw_debug_gdb_reset` | `{openocd_cfg, halt?=true, timeout_s?=30}` | `FlashResult` (`monitor reset halt\|run`) |
| `fw_debug_gdb_halt` | `{openocd_cfg, timeout_s?=30}` | `FlashResult` (`-exec-interrupt` + `monitor halt`) |
| `fw_debug_gdb_step` | `{openocd_cfg, step_over?=false, timeout_s?=30}` | `FlashResult` (`-exec-step-instruction`\|`-exec-next-instruction`) |
| `fw_debug_gdb_set_breakpoint` | `{openocd_cfg, address, timeout_s?=30}` | `FlashResult` (`-break-insert *<address>`) |
| `fw_debug_gdb_read_mem` | `{openocd_cfg, address, length, timeout_s?=30}` | `FlashResult` (`-data-read-memory-bytes <address> <length>`) |

Populates the new `can_debug_gdb` capability on the `cortex-m` family
(`kFamilyRules["cortex-m"]`, `all_of: ["openocd", "arm-none-eabi-gdb"]`) — the first tracked
debug capability for any family (`can_flash` previously covered probe-rs flashing only, with
no equivalent `can_debug*` key at all).

### 12.5 AVR emulation + debug via simavr — `fw_emulate_simavr`

`{elf_path, mcu, run_s?=2.0, gdb?=false, gdb_port?=1234, mi_commands?:string[],
timeout_s?=run_s+10, gdb_binary?="avr-gdb"}` → `FlashResult`.

Free-run mode (`gdb=false`): `simavr -m <mcu> <elf_path>` for `run_s` wall-clock seconds.
simavr never self-exits, so the scheduled stop is `success=true` (output notes `"[stopped
after <run_s>s run window]"`) — only an absent binary or an early non-zero exit is a
failure. GDB-attach mode (`gdb=true`): `simavr -m <mcu> -g <elf_path>` (built-in GDB server,
simavr default port 1234) driven via `avr-gdb --batch --interpreter=mi2` with `mi_commands`.
Practical universal AVR *debug* path per `embedded-landscape.md`: modern UPDI-part debug
protocols (EDBG/mEDBG/PKOB nano) aren't GDB-exposed by an open tool, so simavr's own GDB
server substitutes; real-hardware debugWIRE via avarice stays out of scope (legacy/rare).
Populates `can_debug_gdb` (`all_of: ["simavr", "avr-gdb"]`) and `can_emulate`
(`any_of` now includes `"simavr"`) on the `avr` family.

### 12.6 ARM emulation via Renode — `fw_emulate_renode`

`{resc_path, run_s?=2.0, gdb?=false, gdb_port?=3333, mi_commands?:string[],
timeout_s?=run_s+10, gdb_binary?="arm-none-eabi-gdb"}` → `FlashResult`.

`renode --disable-xwt --console <resc_path>` (identical invocation shape to `fw_sim_run`'s
existing Renode backend, `emulator.cpp`). `resc_path` is a pre-generated Renode script —
this tool runs an EXISTING `.resc` rather than authoring one; pass the same script
`fw_sim_run` would use, or a hand-authored one for scenarios `fw_sim_run` doesn't cover.
Free-run/GDB-attach semantics mirror §12.5 (scheduled stop = success; `gdb=true` expects the
`.resc` to start Renode's GDB server via `machine StartGdbServer <gdb_port>`).

### 12.7 Live serial/UART monitor (physical device) — `fw_serial_*`

The LIVE physical-device counterpart to §7's emulator UART capture — a raw byte pipe, no
framing/protocol assumed. Implemented with raw POSIX termios (8N1, non-canonical,
non-blocking reads) directly in `hardware.cpp` — no pyserial/serialport-rs dependency added
to the C++ core (the "minimal robust path" the landscape dossier flags for the serial
transport layer). Handles are a process-local registry (mutex-guarded
`unordered_map<int, fd>` in `hardware.cpp`), valid for the hosting process's lifetime.

| method | params | result |
|---|---|---|
| `fw_serial_open` | `{port, baud?=115200}` | `{success, handle, port, baud}` \| `{success:false, reason}` |
| `fw_serial_read` | `{handle, max_bytes?=4096}` | `{success, data_hex, bytes}` \| `{success:false, reason}` |
| `fw_serial_write` | `{handle, data_hex?, text?}` | `{success, bytes_written}` \| `{success:false, reason}` |
| `fw_serial_close` | `{handle}` | `{success}` |

`fw_serial_read` is non-blocking (immediate return, poll it repeatedly for a live console);
bytes are always hex-encoded (`data_hex`), never assumed text, matching the `data_hex`
convention `fw_debug_read_mem` already established. `fw_serial_write` accepts either
`data_hex` (raw bytes) or `text` (a plain string convenience); `data_hex` wins if both are
given. Non-POSIX builds: `fw_serial_open` always returns `{success:false, reason:
"unsupported_platform: ..."}`.

### 12.8 Tool registry tagging

All twelve tools above register through the same `register_firmware_tools(mcp)` closures as
the rest of the firmware surface, so `_reg.view(mcp, "ecad")` (`server.py`) tags them family
`"ecad"` and auto-classifies risk via `tool_registry.classify_risk()`: none of the new names
match a `_READ_*`/`_EXTERNAL_*` pattern, so every one — including the two read-shaped ops,
`fw_debug_gdb_read_mem` and `fw_serial_read` — defaults to `RISK_WRITE` (fail-safe;
`fw_debug_read_mem`/probe-rs's existing memory read is tagged the same way for the same
reason: a live probe/port attach is treated as mutating, not a free read). Flash/erase ops
(`fw_avr_flash`) and every debug/emulate/serial op are therefore all `write` risk, gated by
the standard `_guard` allow-list exactly like the rest of the firmware family — no
`tool_registry.py` edit required (module docstring: individual `*_tools.py` modules are
never edited to add tags).

### 12.9 External binaries assumed on PATH

`avrdude`, `openocd`, `arm-none-eabi-gdb` (or `gdb-multiarch` as a cross-capable fallback),
`avr-gdb`, `simavr`. All graceful-missing per §1: `fw_status`'s `tools`/`families` rollup
(`provision.cpp`) now also probes these five and reports `can_flash`/`can_debug_gdb`/
`can_emulate` per family, plus a new AVR-flashing banner alongside the existing
cortex-m/esp32 banners. `avrdude` was additionally added to the "core" tool list that drives
`fw_status`'s top-level `missing` array (was 7 tools, now 8).
