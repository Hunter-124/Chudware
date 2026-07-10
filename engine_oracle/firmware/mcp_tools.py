"""Firmware compile-loop handlers (T-C2): ``fw_new_project`` / ``fw_build`` / ``fw_sources``.
Hardware path handlers (T-C4): ``fw_flash`` / ``fw_debug_start`` / ``fw_debug_halt`` /
``fw_debug_read_mem``.

Framework-free, plain ``dict`` in → ``dict`` out — the MCP wrapper
(``services/mcp/firmware_tools.py``) and the JSON-RPC worker call these directly, mirroring
``engine.ecad.rpc``. Expected failures come back as the shared error envelope
``{"code", "message", "detail"}`` (``contracts/firmware_rpc.md`` §1), never raised.

The compile loop is: ``fw_new_project`` (seeds the project + a pin-map header from the linked ECAD
board) → write/edit sources → ``fw_build`` (BuildResult with parsed warnings) → read errors → fix →
``fw_sources`` to add/remove files → repeat.

Hardware path: ``fw_flash`` → flash ELF/bin to target → ``fw_debug_start`` → halt core →
``fw_debug_read_mem`` → read registers/memory → ``fw_debug_halt`` → halt/reset.
All hardware ops degrade gracefully when probe-rs / esptool.py is absent (``success=False``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from engine.firmware import mcu_pinouts
from engine.firmware import pinmap as fw_pinmap
from engine.firmware import project as fw_project
from engine.firmware import toolchains as fw_toolchains
from engine.firmware.emulator import SimResult, run_simulation


def _err(code: str, message: str, detail: str = "") -> dict:
    return {"code": code, "message": message, "detail": detail}


def fw_new_project(params: dict) -> dict:
    """``{name?, mcu_family?, toolchain?, board_doc_ref?}`` → ``{project_id, project[, pinmap_path]}``.

    Creates the project (auto-selecting ``mcu_family`` from the ECAD BOM when omitted, §2) and, if
    the board ref resolves, seeds ``build/include/pinmap.h`` from its net names (best-effort).
    """
    name = params.get("name") or None
    mcu = params.get("mcu_family") or params.get("mcu") or None
    toolchain = params.get("toolchain") or "cmake"
    board_doc_ref = params.get("board_doc_ref") or None

    try:
        project = fw_project.new_project(board_doc_ref, mcu, toolchain, name=name)
    except ValueError as e:
        return _err("invalid_request", str(e))

    result: dict[str, Any] = {"project_id": project.project_id, "project": project.to_dict()}

    # Seed the pin-map header from the linked board's netlist (never fatal to project creation).
    if board_doc_ref:
        try:
            doc = mcu_pinouts.resolve_board_doc(board_doc_ref)
            if doc is not None:
                path = fw_pinmap.write_pinmap(project, doc, mcu_family=project.mcu_family)
                result["pinmap_path"] = str(path)
        except Exception:  # pragma: no cover - defensive; header seeding is a convenience
            pass
    return result


def fw_build(params: dict) -> dict:
    """``{project_id}`` → ``BuildResult`` dict (contract §3). A missing toolchain CLI is reported
    inside the result (``success=False``, ``returncode=-1``, ``stderr`` prefixed
    ``toolchain_not_found``), never raised — the compile loop reads it like any other build error."""
    project_id = params.get("project_id")
    if not project_id:
        return _err("invalid_request", "fw_build requires 'project_id'")
    try:
        project = fw_project.load(str(project_id))
    except FileNotFoundError as e:
        return _err("unknown_project", str(e))
    return fw_toolchains.build(project).to_dict()


def fw_sources(params: dict) -> dict:
    """``{project_id, add?:[str], remove?:[str]}`` → ``{src_paths}``.

    Lists the project's source files, or adds/removes them. Paths are relative to the project root.
    Adding a not-yet-existing path creates an empty stub file (so the build sees it); removing only
    unregisters it (the file on disk is left in place). Returns the resulting ``src_paths``.
    """
    project_id = params.get("project_id")
    if not project_id:
        return _err("invalid_request", "fw_sources requires 'project_id'")
    try:
        project = fw_project.load(str(project_id))
    except FileNotFoundError as e:
        return _err("unknown_project", str(e))

    add = [str(p) for p in (params.get("add") or [])]
    remove = [str(p) for p in (params.get("remove") or [])]
    changed = False

    for rel in remove:
        if rel in project.src_paths:
            project.src_paths.remove(rel)
            changed = True

    for rel in add:
        if rel not in project.src_paths:
            project.src_paths.append(rel)
            changed = True
        # Materialise an empty stub so a subsequent build can compile it.
        try:
            abs_path = project.root_dir / rel
            if not abs_path.exists():
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_text("", encoding="utf-8")
        except Exception:  # pragma: no cover - defensive; registration still succeeds
            pass

    if changed:
        project.src_paths.sort()
        fw_project.save(project)
    return {"src_paths": list(project.src_paths)}


def fw_sim_run(params: dict) -> dict:
    """``{project_id, elf_path?, scenario?, assertions?:[str]}`` → ``SimResult`` dict (contract §4).

    Runs the firmware emulation loop: discovers the ELF from the project's build dir if
    ``elf_path`` is omitted, then dispatches to Renode (when installed + a platform file
    exists) or falls back to QEMU. Neither emulator present → ``passed=False`` with an
    explanation in ``uart_log`` (graceful degradation, same posture as ``fw_build``).

    ``assertions`` is a list of assertion strings:
    - ``"uart:<text>"`` — passes when the text appears in the captured UART/semihosting log.
    - ``"gpio:<PIN>=<level>"`` — passes when the GPIO pin is at the given logic level at end
      of run (Renode: per-pin state-change hooks + ODR snapshot on stm32f4_discovery;
      QEMU: the firmware-side ``aicad_trace.h`` semihosting shim reports pin events).
    - ``"gpio:<PIN>=<level>@><T>ms"`` (also ``@<``/``@``) — timeline form: a time-stamped
      pin event at that level after/before/at-or-after ``T`` ms.
    - plain string — treated as ``"uart:<text>"``.

    The result includes ``gpio_events`` (time-ordered pin timeline) and ``trace_path`` —
    a structured JSON trace artifact (``aicad.firmware.trace/1``, contract §7) written
    next to the ELF with interleaved pin + UART events and the final GPIO snapshot.

    T-FIN2: supported emulation targets come from ``platform_db.capability_matrix()``
    (stm32f4 / stm32f1 / nrf52 full Renode platforms, rp2040 minimal UART-only,
    esp32 build+flash only). When an assertion needs a peripheral the platform lacks,
    the run refuses honestly (``passed=False`` with a ``platform_capability`` reason)
    unless the QEMU semihosting fallback can cover it.
    """
    project_id = params.get("project_id")
    if not project_id:
        return _err("invalid_request", "fw_sim_run requires 'project_id'")
    try:
        project = fw_project.load(str(project_id))
    except FileNotFoundError as e:
        return _err("unknown_project", str(e))

    # Resolve ELF: explicit param > glob the build dir
    elf_path = params.get("elf_path") or None
    if not elf_path:
        build_dir = project.root_dir / project.build_dir
        for pattern in ("*.elf", "*.bin", "*.hex"):
            hits = sorted(build_dir.rglob(pattern))
            if hits:
                elf_path = str(hits[0])
                break

    if not elf_path:
        # No ELF and no build yet — graceful SimResult (caller should run fw_build first)
        return SimResult(
            passed=False,
            uart_log="no_elf: no .elf found in build dir — run fw_build first",
        ).to_dict()

    assertions: list[str] = list(params.get("assertions") or [])

    result = run_simulation(
        elf_path=str(elf_path),
        mcu_family=project.mcu_family or "cortex-m4",
        assertions=assertions,
        timeout_s=float(params.get("timeout_s", 30)),
    )
    return result.to_dict()


def fw_verify_board(params: dict) -> dict:
    """``{board_doc_ref, project_id?, sources?:{name:text}, sch_doc_ref?, mcu_family?,
    trace_path?, emulate?:bool}`` → co-verification findings (T-FIN6, contract §9).

    Static checks always run (firmware ``PIN_<NET>`` refs vs board nets/MCU pads, strap-pin
    repurposing, direction clashes, per-rail decoupling). The dynamic pass runs when either
    ``trace_path`` points at an existing ``aicad.firmware.trace/1`` artifact, or
    ``emulate=true`` and the project has a built ELF (a real emulation is launched via
    ``run_simulation`` and its pin events are diffed — falls back gracefully when no
    emulator is installed, reported in ``dynamic.reason``).

    Firmware pin references come from ``sources`` when given, else the project's own source
    files (``pinmap.h`` is excluded — its defines are declarations, not uses).

    Result: ``{ok, mcu_family, findings:[violation], summary:{n, errors, warnings, rules},
    fw_pins:{token:{reads,writes,refs,files}}, dynamic:{ran, source, reason?}}``.
    Findings use the ERC/DRC violation shape plus a ``fix`` hint. Advisory — never blocks.
    """
    from engine.firmware import coverify

    board_ref = params.get("board_doc_ref") or ""
    board = mcu_pinouts.resolve_board_doc(str(board_ref))
    if board is None:
        return _err("unknown_board", f"board_doc_ref {board_ref!r} did not resolve to an "
                    "open ECAD doc or a readable file")
    sch = mcu_pinouts.resolve_board_doc(str(params.get("sch_doc_ref") or "")) \
        if params.get("sch_doc_ref") else None

    project = None
    if params.get("project_id"):
        try:
            project = fw_project.load(str(params["project_id"]))
        except FileNotFoundError as e:
            return _err("unknown_project", str(e))

    sources = params.get("sources")
    if not isinstance(sources, dict):
        sources = coverify.project_sources(project) if project else {}
    sources = {k: v for k, v in sources.items() if not str(k).endswith("pinmap.h")}
    if not sources:
        return _err("invalid_request", "fw_verify_board needs firmware sources: pass "
                    "'sources' or a 'project_id' whose project has source files")

    mcu_family = (params.get("mcu_family") or
                  (project.mcu_family if project else None) or
                  mcu_pinouts.select_mcu_family(board))
    fw_pins = coverify.scan_firmware_pins(sources)

    # dynamic pass: existing trace artifact, or a real emulation when asked
    trace: Optional[dict] = None
    dynamic: dict = {"ran": False, "source": None}
    if params.get("trace_path"):
        trace = coverify.load_trace(str(params["trace_path"]))
        if trace is None:
            dynamic["reason"] = f"trace_path {params['trace_path']!r} unreadable"
        else:
            dynamic = {"ran": True, "source": "trace"}
    elif params.get("emulate") and project is not None:
        elf = None
        build_dir = project.root_dir / project.build_dir
        for pattern in ("*.elf", "*.bin", "*.hex"):
            hits = sorted(build_dir.rglob(pattern))
            if hits:
                elf = str(hits[0])
                break
        if not elf:
            dynamic["reason"] = "no_elf: run fw_build first"
        else:
            # Synthetic gpio assertions install the Renode pin hooks for every pin the
            # firmware writes; we only consume the observed events, not passed/failed.
            hooks = [f"gpio:{t}=1" for t, u in sorted(fw_pins.items())
                     if u.get("writes") and mcu_pinouts.resolve_pin(t, mcu_family) is not None]
            sim = run_simulation(elf, mcu_family or "cortex-m4", hooks,
                                 timeout_s=float(params.get("timeout_s", 30)))
            if sim.gpio_events or sim.gpio_states:
                trace = {"events": sim.gpio_events, "gpio_final": sim.gpio_states}
                dynamic = {"ran": True, "source": "emulation"}
            else:
                dynamic["reason"] = sim.uart_log[:200] or "emulation produced no pin events"

    findings = coverify.verify_board(board, fw_pins, sch=sch, mcu_family=mcu_family,
                                     trace=trace)
    return {
        "ok": True,
        "mcu_family": mcu_family,
        "findings": findings,
        "summary": coverify.summarize(findings),
        "fw_pins": fw_pins,
        "dynamic": dynamic,
    }


# ---------------------------------------------------------------------------
# Hardware path (T-C4): probe-rs flash/debug
# ---------------------------------------------------------------------------

def fw_flash(params: dict) -> dict:
    """``{elf_path, chip, port?, baud?}`` → ``FlashResult`` dict.

    Flashes *elf_path* to the target board using probe-rs (when ``port`` is absent or the
    path is an ``.elf``) or esptool.py (when ``port`` is provided and the path is a
    ``.bin``). Missing binary → ``success=False`` with ``output`` explaining what's needed;
    never an error envelope (graceful degradation, same posture as ``fw_build``).

    ``chip`` identifies the target MCU for probe-rs (e.g. ``"STM32F407VGTx"``).
    For ESP32 targets, pass the serial port as ``port`` and a ``.bin`` image.

    [~] Live flash (Erik): requires a DAP probe (probe-rs) or USB serial (esptool.py)
        connected to the target board. See hardware.py docstrings for exact CLI invocations.
    """
    from engine.firmware.hardware import ProbeRsFlasher, EsptoolFlasher

    elf_path = params.get("elf_path") or params.get("bin_path") or ""
    if not elf_path:
        return _err("invalid_request", "fw_flash requires 'elf_path'")

    chip = params.get("chip") or ""
    port = params.get("port") or ""

    # Route: esptool for explicit serial port + .bin; probe-rs otherwise.
    if port and elf_path.endswith(".bin"):
        baud = int(params.get("baud", 460800))
        flash_offset = params.get("flash_offset", "0x0")
        result = EsptoolFlasher().flash(elf_path, port, baud=baud,
                                        flash_offset=flash_offset,
                                        timeout_s=float(params.get("timeout_s", 60)))
    else:
        if not chip:
            return _err("invalid_request", "fw_flash requires 'chip' for probe-rs flashing")
        result = ProbeRsFlasher().flash(elf_path, chip,
                                        timeout_s=float(params.get("timeout_s", 60)))
    return result.to_dict()


def fw_debug_start(params: dict) -> dict:
    """``{chip}`` → ``FlashResult`` dict (reset + run).

    Resets the target and starts execution via probe-rs. Use this to bring the board out of
    halt or a failed state before reading memory.

    [~] Live run (Erik): ``probe-rs reset --chip <chip>``
        Requires a DAP probe connected.
    """
    from engine.firmware.hardware import ProbeRsDebugger

    chip = params.get("chip") or ""
    if not chip:
        return _err("invalid_request", "fw_debug_start requires 'chip'")

    result = ProbeRsDebugger().reset(chip, timeout_s=float(params.get("timeout_s", 30)))
    return result.to_dict()


def fw_debug_halt(params: dict) -> dict:
    """``{chip}`` → ``FlashResult`` dict (reset into halt).

    Resets the target and holds it in halt so registers/memory can be inspected.

    [~] Live run (Erik): ``probe-rs reset --chip <chip> --halt``
        Requires a DAP probe connected.
    """
    from engine.firmware.hardware import ProbeRsDebugger

    chip = params.get("chip") or ""
    if not chip:
        return _err("invalid_request", "fw_debug_halt requires 'chip'")

    result = ProbeRsDebugger().halt(chip, timeout_s=float(params.get("timeout_s", 30)))
    return result.to_dict()


def fw_debug_read_mem(params: dict) -> dict:
    """``{chip, address, length}`` → ``{success, data_hex, address, length, ms}``.

    Reads *length* bytes from *address* on the halted target via probe-rs. Returns the raw
    bytes hex-encoded in ``data_hex``. When probe-rs is absent or the read fails,
    ``success=False`` with ``reason`` explaining the failure.

    [~] Live run (Erik): ``probe-rs read --chip <chip> <address_hex> <length>``
        Target must be halted first (use fw_debug_halt). Requires a DAP probe connected.
    """
    import time as _time
    from engine.firmware.hardware import ProbeRsDebugger

    chip = params.get("chip") or ""
    if not chip:
        return _err("invalid_request", "fw_debug_read_mem requires 'chip'")

    address_raw = params.get("address")
    length_raw = params.get("length")
    if address_raw is None or length_raw is None:
        return _err("invalid_request", "fw_debug_read_mem requires 'address' and 'length'")

    try:
        address = int(str(address_raw), 0)  # accept "0x20000000" or decimal
        length = int(str(length_raw), 0)
    except ValueError as e:
        return _err("invalid_request", f"invalid address or length: {e}")

    t0 = _time.perf_counter()
    try:
        data = ProbeRsDebugger().read_memory(
            address, length, chip, timeout_s=float(params.get("timeout_s", 30))
        )
        return {
            "success": True,
            "data_hex": data.hex(),
            "address": hex(address),
            "length": len(data),
            "ms": round((_time.perf_counter() - t0) * 1000.0, 2),
        }
    except RuntimeError as e:
        return {
            "success": False,
            "reason": str(e),
            "address": hex(address),
            "length": length,
            "ms": round((_time.perf_counter() - t0) * 1000.0, 2),
        }
