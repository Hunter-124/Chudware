---
id: T-C1
title: firmware_rpc.md contract and engine/firmware/ scaffold
workstream: C
owner: opus-vex
status: done
branch: task/T-C1-firmware-scaffold
day: 1
depends_on: []
---

## Context

Workstream C day 1 establishes the firmware pillar's contract and scaffold. `contracts/firmware_rpc.md` defines the toolchain RPC surface: project record schema, build result schema, simulation result schema, and the MCP tool signatures. `engine/firmware/` (or `engine_oracle/firmware/`) follows the ECAD precedent: own schema/session/ops. The toolchain baseline is CMake + arm-none-eabi (zero ToS risk) with ESP-IDF and Arduino CLI as alternate subprocess backends. The firmware project record is linked to the ECAD board doc via doc-meta.

## Files / areas touched

- `contracts/firmware_rpc.md` — new frozen contract
- `engine_oracle/firmware/__init__.py`, `engine_oracle/firmware/project.py` — new module
- `engine_oracle/firmware/toolchains.py` — CMake/arm-none-eabi, ESP-IDF, Arduino CLI subprocess wrappers
- `engine_oracle/doc/` — doc-meta system (existing); understand linking mechanism
- `services/mcp/server.py` — eventual registration point (day 2)

## Steps

1. Run `graphify query "doc-meta document linking ECAD board"` to understand the doc-meta system and how ECAD board docs are referenced.
2. Draft `contracts/firmware_rpc.md`: `FirmwareProject` record schema (project_id, board_doc_ref, mcu_family, toolchain, src_paths, build_dir), build result (`{success, elf_path, map_path, stderr, warnings}`), sim result (`{passed, uart_log, gpio_states, assertion_results}`), tool signatures for `fw_new_project`, `fw_build`, `fw_sources`, `fw_sim_run`, `fw_flash`.
3. Create `engine_oracle/firmware/project.py`: `FirmwareProject` dataclass; `new_project(board_doc_ref, mcu, toolchain) -> FirmwareProject`; `save` / `load` via doc-meta.
4. Create `engine_oracle/firmware/toolchains.py`: abstract `Toolchain.build(project) -> BuildResult`; `CMakeToolchain` (arm-none-eabi); `ESPIDFToolchain`; `ArduinoToolchain` — all subprocess-based with timeout.
5. Write a CMake template project in `engine_oracle/firmware/templates/cmake_arm/` (bare-metal blink skeleton with `compile_commands.json` generation).
6. Smoke-test: `new_project(None, "cortex-m4", "cmake")` creates a project dir with the CMake template.

## Definition of done

- [x] `contracts/firmware_rpc.md` exists, all tool signatures documented
- [x] `engine_oracle/firmware/` package importable
- [x] `new_project()` creates a project dir with CMake template and `compile_commands.json`
- [x] `CMakeToolchain.build()` subprocess call is wired (actual compile may fail in CI without toolchain installed — that is acceptable; test with a mock)
- [x] `python -m pytest engine_oracle/firmware/test_project.py -q` passes (10 passed)

## Implementation notes (opus-vex)

- **`engine` is a symlink → `engine_oracle`** (the git-tracked dir). Code imports via
  `from engine.firmware...` to match the ECAD precedent; files live under `engine_oracle/`.
- **doc-meta linking:** the Python doc engine was rewritten into the C++ core and deleted,
  so there is no heavy Python "doc-meta" subsystem. Board linking is a loose id/path string
  reference (`board_doc_ref`) — the same mechanism `ecad_update_from_sch` uses to pair a board
  and schematic. Persistence is a `project.json` sidecar written with the atomic tmp+`os.replace`
  idiom from `engine.ecad.autosave`. Workspace dir: `AI_CAD_FIRMWARE_DIR` override → else
  `%APPDATA%/AI-CAD/firmware` (temp fallback).
- **`compile_commands.json` guarantee** (contract §6): `new_project` writes a synthetic
  `compile_commands.json` enumerating template sources so the artifact always exists without a
  cross-toolchain; a real `CMakeToolchain.build()` configure regenerates the accurate one.
- **Verification:** 10 pytest tests green (create / roundtrip / compile_commands / mocked build
  success + artifact discovery / graceful tool-absent for all three backends). Real `tc.build()`
  smoke with cmake+ninja present but no `arm-none-eabi-gcc` returns a graceful
  `BuildResult(success=False, returncode=1)` in ~95 ms (no hang) — the documented acceptable case.
- **Did NOT edit** `services/mcp/server.py` (day-2 registration) or `contracts/licenses.md`
  (out of scope; see `work/questions/T-C1.md` Q2).

## ⚠️ Gate status — pre-existing blocker (see work/questions/T-C1.md Q1)

`bash scripts/gate.sh` PASSES every stage EXCEPT `ui/imgui`, which fails to **compile** on a
**pre-existing master break** unrelated to T-C1: `property_manager.cpp` references
`PanelField::selMulti` (10×) but the struct in `ui/abstract/panel_spec.h` has no such member.
Verified present at `f6e156c` (tip before this task's claim). T-C1 adds zero C++, so it neither
causes nor can fix this within its allowed files. Gate summary at review time:

```
check.py     FAIL-RECORDED  (pre-existing, non-fatal)
core build   PASS  (libaicad, OCCT ON)
ui/abstract  PASS
ctest        PASS  (100%)
pytest       SKIP  (no provisioned venv; firmware suite verified separately — 10 passed)
ui/imgui     FAIL  (pre-existing selMulti compile break — NOT from T-C1)
```

The firmware slice is merge-ready on its own; the GUI break needs a separate GUI-workstream fix.

## AI hints

- `graphify query "ECAD engine module doc-meta session"` — understand the ECAD module pattern to mirror it
- `graphify query "doc-meta persist load"` — find doc-meta save/load API
- Relevant files: `engine_oracle/ecad/`, `engine_oracle/doc/`, `engine_oracle/paths.py`
- arm-none-eabi-gcc must be found via PATH; graceful error if not installed (not a test failure)
- CMake template must emit `compile_commands.json` via `set(CMAKE_EXPORT_COMPILE_COMMANDS ON)`
