"""tool_guidance.firmware — curated options for the fw_* firmware surface.

The firmware tools are *registered* through ``register_firmware_tools`` and tagged
family ``"ecad"`` by ``services/mcp/server.py`` (they share the board seam), but the
discipline is its own thing: a scaffold -> build -> emulate -> profile chain plus a
hardware flash/debug/serial leg that needs binaries a normal box does not have.
This module is the SOLE owner of guidance for every ``fw_*`` name — the entries
previously filed in ``ecad.py`` were moved here wholesale, so no ``fw_*`` key exists
in any other family module and the ``_load()`` merge order is irrelevant.

Notes state, in order: what the tool is for, the REQUIRED argument names verbatim
from the live signatures in ``services/mcp/firmware_tools.py``, and the next tool in
the chain. The chain is:

    fw_status -> fw_new_project -> fw_sources -> fw_build -> fw_sim_run
              -> fw_power_profile          (fw_verify_board closes the board loop)

``timeout_s`` is set for the tools that spawn a real compiler or emulator
(cmake / arm-none-eabi-gcc / renode / qemu / simavr) or attach to real hardware —
their COLD START alone is 10-20 s, so a short budget kills a healthy build.
"""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "fw_avr_flash": {
        "prompt_note": (
            "Flash an AVR target with avrdude — requires hex_path and mcu "
            "(programmer='arduino' pairs with the arduino-cli build; leave port "
            "empty for USB HID programmers like usbasp). Needs avrdude plus live "
            "hardware; without either, fw_emulate_simavr is the honest path."
        ),
        # avrdude erase+write+verify over a slow serial programmer.
        "timeout_s": 120,
    },
    "fw_build": {
        "prompt_note": (
            "Cross-compile the project to an ELF — requires project_id from "
            "fw_new_project; a missing toolchain returns success=false "
            "returncode=-1, not an error, so surface the install hint instead of "
            "retrying. Then fw_sim_run with the returned elf_path."
        ),
        # cmake configure + arm-none-eabi-gcc from cold: 10-20 s of process
        # start-up before a single line compiles, then the whole template build.
        "timeout_s": 300,
    },
    "fw_debug_gdb_halt": {
        "prompt_note": (
            "Halt an ARM target through OpenOCD + gdb/MI — requires openocd_cfg; "
            "prefer fw_debug_halt when probe-rs covers the chip. Needs openocd and "
            "arm-none-eabi-gdb on PATH plus a live SWD/JTAG probe; then "
            "fw_debug_gdb_read_mem or fw_debug_gdb_step."
        ),
        "timeout_s": 120,
    },
    "fw_debug_gdb_read_mem": {
        "prompt_note": (
            "Read target memory over OpenOCD + gdb/MI — requires openocd_cfg, "
            "address and length, and a halted target (fw_debug_gdb_halt first); "
            "output is the raw MI record, NOT decoded hex, so prefer "
            "fw_debug_read_mem when probe-rs supports the chip."
        ),
        "timeout_s": 120,
    },
    "fw_debug_gdb_reset": {
        "prompt_note": (
            "Reset an ARM target via a fresh OpenOCD + gdb/MI session — requires "
            "openocd_cfg (halt=True stops at reset). Fallback for chips outside "
            "probe-rs's registry; each call re-attaches, so breakpoints do not "
            "reliably persist. Needs openocd plus a live probe."
        ),
        "timeout_s": 120,
    },
    "fw_debug_gdb_set_breakpoint": {
        "prompt_note": (
            "Insert a breakpoint over OpenOCD + gdb/MI — requires openocd_cfg and "
            "address, raw addresses only ('0x8000100') because no symbol table is "
            "loaded, so 'main' will not resolve. May not survive the next call's "
            "fresh attach; continue with fw_debug_gdb_step."
        ),
        "timeout_s": 120,
    },
    "fw_debug_gdb_step": {
        "prompt_note": (
            "Single-instruction step an ARM target — requires openocd_cfg and a "
            "halted target (fw_debug_gdb_halt or a breakpoint hit first); "
            "step_over=True steps over calls. Inspect with fw_debug_gdb_read_mem."
        ),
        "timeout_s": 120,
    },
    "fw_debug_halt": {
        "prompt_note": (
            "Reset-and-halt a target via probe-rs — requires chip (the probe-rs "
            "chip id); needs probe-rs on PATH plus a live DAP probe. Inspect with "
            "fw_debug_read_mem, resume with fw_debug_start."
        ),
        "timeout_s": 120,
    },
    "fw_debug_read_mem": {
        "prompt_note": (
            "Read target memory via probe-rs — requires chip, address and length, "
            "and a halted target (fw_debug_halt first); returns decoded data_hex, "
            "unlike the gdb variant's raw MI record. Needs probe-rs plus a live "
            "probe."
        ),
        "timeout_s": 120,
    },
    "fw_debug_start": {
        "prompt_note": (
            "Reset and run a target via probe-rs — requires chip; the counterpart "
            "that resumes after fw_debug_halt/fw_debug_read_mem inspection. Needs "
            "probe-rs plus a live DAP probe."
        ),
        "timeout_s": 120,
    },
    "fw_emulate_renode": {
        "prompt_note": (
            "Run an EXISTING Renode script — requires resc_path (fw_sim_run "
            "generates .resc files; hand-author only for what it cannot cover); "
            "the scheduled stop after run_s reports success=True. Needs renode on "
            "PATH; for a project-driven run use fw_sim_run instead."
        ),
        # Renode's mono/.NET cold start is 10-20 s before run_s even begins.
        "timeout_s": 180,
    },
    "fw_emulate_simavr": {
        "prompt_note": (
            "Emulate or debug an AVR ELF with simavr — requires elf_path and mcu; "
            "gdb=True drives avr-gdb through mi_commands (the universal AVR debug "
            "path, since modern UPDI debug has no open GDB exposure). Needs simavr "
            "on PATH; real silicon is fw_avr_flash."
        ),
        "timeout_s": 180,
    },
    "fw_flash": {
        "prompt_note": (
            "Flash a built image to real hardware — requires elf_path (probe-rs "
            "for an .elf with chip, esptool for an ESP32 .bin when port is given); "
            "missing tools return success=false, not errors. Needs probe-rs or "
            "esptool plus a connected board; prove the firmware with fw_sim_run "
            "first."
        ),
        "timeout_s": 180,
    },
    "fw_new_project": {
        "prompt_note": (
            "Scaffold the firmware project once and keep its project_id — pass "
            "name and mcu_family (toolchain defaults to 'cmake'), plus "
            "board_doc_ref to link a PCB so mcu_family comes from the BOM and a "
            "pinmap.h of PIN_<NET> defines is generated. Then fw_sources."
        ),
    },
    "fw_power_profile": {
        "prompt_note": (
            "Derive an aicad.powerprofile/1 duty-cycle current profile from an "
            "EXISTING trace — requires trace_path (the <elf>.trace.json fw_sim_run "
            "returned); no live emulation. Bookkeeping, not SPICE; its per-rail "
            "schedules feed power_budget and run_fea_thermal transients."
        ),
    },
    "fw_sensor_scenario": {
        "prompt_note": (
            "Turn a flight/CFD/kinematic profile into emulated sensor stimulus — "
            "pass profile (a JSON object string with t_s plus the physical series) "
            "and sensor_map [{device, address}]; pure Python, no Renode needed. "
            "Feed the returned scenario to fw_sim_run(scenario=...)."
        ),
    },
    "fw_serial_close": {
        "prompt_note": (
            "Release a live serial handle — requires handle from fw_serial_open; "
            "an already-closed or unknown handle returns success=false, not an "
            "error. Last step of the fw_serial_* monitor loop."
        ),
    },
    "fw_serial_open": {
        "prompt_note": (
            "Open a raw 8N1 byte pipe to a PHYSICAL device — requires port (baud "
            "defaults to 115200); the returned handle is process-local and dies "
            "with the host. Then fw_serial_read / fw_serial_write, and always "
            "fw_serial_close. Emulator UART is fw_sim_run instead."
        ),
    },
    "fw_serial_read": {
        "prompt_note": (
            "Non-blocking poll of a live serial handle — requires handle from "
            "fw_serial_open; empty data_hex means nothing buffered, not an error, "
            "so poll repeatedly and decode the hex yourself. Close with "
            "fw_serial_close."
        ),
    },
    "fw_serial_write": {
        "prompt_note": (
            "Write to a live serial handle — requires handle plus either data_hex "
            "(raw bytes) or text (console typing); data_hex wins when both are "
            "given. Read the reply with fw_serial_read."
        ),
    },
    "fw_sim_run": {
        "prompt_note": (
            "Emulate the built firmware and check assertions — requires project_id "
            "(plus elf_path/scenario/assertions as needed) and a successful "
            "fw_build first; scenarios and gpio assertions are Renode-only, ESP32 "
            "is build+flash. An absent emulator is an honest SKIP in uart_log, "
            "never a FAIL to invent around; then fw_power_profile on the returned "
            "trace_path."
        ),
        # renode/qemu cold start is 10-20 s before the ELF executes, then the
        # scenario timeline plays out in real time.
        "timeout_s": 300,
    },
    "fw_sources": {
        "prompt_note": (
            "List or edit the project's source files — requires project_id, with "
            "add/remove paths RELATIVE to the project root; adding a path creates "
            "an empty stub the next build picks up. Write PIN_<NET> constants from "
            "pinmap.h, never raw pin numbers. Then fw_build."
        ),
    },
    "fw_status": {
        "prompt_note": (
            "Firmware provisioning doctor — takes no arguments and never fails "
            "(offline-safe); read families.*.can_build/can_emulate/can_flash and "
            "the install banners before anything else. Call FIRST, then "
            "fw_new_project."
        ),
    },
    "fw_verify_board": {
        "prompt_note": (
            "Pre-fab firmware<->board co-verification — requires board_doc_ref "
            "(pass project_id so pin refs come from the sources, sch_doc_ref for "
            "drive-clash checks, trace_path or emulate=True for the dynamic pass). "
            "Advisory ERC-shaped findings; run after fw_build/fw_sim_run."
        ),
        # emulate=True launches Renode/QEMU live on top of the static scan.
        "timeout_s": 300,
    },
}
