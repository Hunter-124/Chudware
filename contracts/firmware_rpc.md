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

Every toolchain backend is invoked **as an unmodified subprocess** — never linked into `libaicad`
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
  `AICAD_GPIO_EVT <PIN>=<level>` line; after the run sleep it reads each referenced port's
  ODR between `AICAD_GPIO_ODR_BEGIN/END <port>` echo markers for the final snapshot
  (snapshot wins over the last event on merge). Hook prints carry no timestamp
  (`t_ms: null`); ordering is preserved via `seq`.
- **QEMU** (semihosting shim): QEMU exposes no external pin observer, so the firmware
  reports its own transitions via `AICAD_GPIO_TRACE(pin_str, level, t_ms)` /
  `AICAD_GPIO_TRACE_NT(pin_str, level)` from the project template header
  `include/aicad_trace.h` — a printf of the same `AICAD_GPIO_EVT` line
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
`AICAD_T_MARK <ms>`, which the parser uses to coarsely time-stamp subsequent UART lines
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
