"""Firmware compile-loop handlers (T-C2): ``fw_new_project`` / ``fw_build`` / ``fw_sources``.

Framework-free, plain ``dict`` in → ``dict`` out — the MCP wrapper
(``services/mcp/firmware_tools.py``) and the JSON-RPC worker call these directly, mirroring
``engine.ecad.rpc``. Expected failures come back as the shared error envelope
``{"code", "message", "detail"}`` (``contracts/firmware_rpc.md`` §1), never raised.

The compile loop is: ``fw_new_project`` (seeds the project + a pin-map header from the linked ECAD
board) → write/edit sources → ``fw_build`` (BuildResult with parsed warnings) → read errors → fix →
``fw_sources`` to add/remove files → repeat.
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
    - ``"gpio:<PIN>=<level>"`` — passes when the GPIO pin is at the given logic level.
    - plain string — treated as ``"uart:<text>"``.
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
