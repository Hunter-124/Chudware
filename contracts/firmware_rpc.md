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
