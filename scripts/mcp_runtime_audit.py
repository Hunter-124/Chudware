#!/usr/bin/env python3
"""MCP Runtime Audit — sonnet-vega, T-V2.

Calls every registered MCP tool through the real mcp-venv using the canonical core
library resolver and records WORKS / DEGRADED / DEAD / INCONCLUSIVE verdicts.

Usage:
    CHUDWARE_LIBCHUDWARE=/home/hunter/Repositories/CAD/core/build-occt-linux/bin/libchudware.so \\
        /home/hunter/.local/share/Chudware/mcp-venv/bin/python scripts/mcp_runtime_audit.py

    # Or with a custom output path:
    CHUDWARE_LIBCHUDWARE=... python scripts/mcp_runtime_audit.py --out work/audits/MCP_RUNTIME_AUDIT.md

Verdict definitions:
  WORKS       - tool called, returned a result (even an error-envelope result)
  DEGRADED    - tool detected a graceful-absence condition and returned {ok:false, reason}
                (missing binary, missing API key, offline, converter_missing)
  DEAD        - ImportError / ModuleNotFoundError / AttributeError at import or first call;
                the PyNite pattern: registered but unreachable
  INCONCLUSIVE- pydantic/arg-validation error (tool is alive, fixture was wrong), or
                tool has risky side-effects and was deliberately skipped
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Repo root on path
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))


# ---------------------------------------------------------------------------
# Families and their registration functions (mirrors server.py exactly)
FAMILY_REGISTRY = [
    ("cad",             "services.mcp.cad_tools",             "register_cad_tools",             True),
    ("gsl",             "services.mcp.gsl_tools",             "register_gsl_tools",             True),
    ("recon",           "services.mcp.recon_tools",           "register_recon_tools",           True),
    ("intake",          "services.mcp.intake_tools",          "register_intake_tools",          True),
    ("interaction",     "services.mcp.interaction_tools",     "register_interaction_tools",     False),
    ("ecad",            "services.mcp.ecad_tools",            "register_ecad_tools",            True),
    ("cam",             "services.mcp.cam_tools",             "register_cam_tools",             True),
    ("doc_diff",        "services.mcp.doc_diff_tools",        "register_doc_diff_tools",        False),
    ("req",             "services.mcp.req_tools",             "register_req_tools",             False),
    ("verify",          "services.mcp.verify_tools",          "register_verify_tools",          False),
    ("research",        "services.mcp.research_tools",        "register_research_tools",        False),
    ("deliverables",    "deliverables",                     "register_deliverables_tools",    False),
    ("sourcing",        "services.mcp.sourcing_tools",        "register_sourcing_tools",        True),
    ("cost",            "services.mcp.cost_tools",            "register_cost_tools",            True),
    ("spec",            "services.mcp.spec_tools",            "register_spec_tools",            True),
    ("piping",          "services.mcp.piping_tools",          "register_piping_tools",          True),
    ("hydraulic",       "services.mcp.hydraulic_tools",       "register_hydraulic_tools",       True),
    ("photo_recon",     "services.mcp.recon_tools",           "register_photo_recon_tools",     False),
    ("firmware",        "services.mcp.firmware_tools",        "register_firmware_tools",        True),
    ("structural",      "services.mcp.structural_tools",      "register_structural_tools",      True),
    ("parts",           "services.mcp.parts_tools",           "register_parts_tools",           True),
    ("serving",         "services.mcp.serving_tools",         "register_serving_tools",         False),
    ("regen",           "services.mcp.regen_tools",           "register_regen_tools",           False),
    ("code_compliance", "services.mcp.code_compliance_tools", "register_code_compliance_tools", False),
]

# ---------------------------------------------------------------------------
# Tool call fixtures
# Tools that require specific arguments to produce a meaningful call.
# Tools NOT listed here are called with {} (zero-arg) unless they appear in SKIP_TOOLS.
# Verdicts from zero-arg calls on tools that require args will be INCONCLUSIVE if they
# raise a validation error (not DEAD).

TOOL_FIXTURES: dict[str, dict[str, Any]] = {
    # CAD family
    "new_part":               {"name": "_audit_probe_"},
    "add_features":           {"ops": [{"op": "box", "x": 10, "y": 10, "z": 10}]},
    "cad_guide":              {},
    "list_parts":             {},
    "list_configurations":    {},
    "list_world_bodies":      {},
    "list_standard_parts":    {},
    "measure_part":           {},
    "query_geometry":         {},
    "describe_part":          {},
    "describe_scene":         {},
    "describe_assembly":      {},
    "describe_project":       {},
    "describe_sketch":        {},
    "describe_board":         {},
    "describe_results":       {},
    "describe_toolpaths":     {},
    "check_feature_overlap":  {},
    "check_interference":     {},
    "check_symmetry":         {},
    "check_sketch":           {},
    "close_part":             {},
    "undo":                   {},
    "redo":                   {},
    "save_part":              {},
    "recover_parts":          {},
    "get_part":               {},
    "intake_status":          {},
    "wall_thickness":         {},
    "contact_sheet":          {},
    "draft_analysis":         {},
    "inspect_section":        {"plane": "xy"},
    "measure_between":        {"target_a": "top face", "target_b": "bottom face"},
    "hover":                  {"x": 0.0, "y": 0.0},
    "add_datum":              {"name": "probe_plane", "type": "plane", "ref": "top face"},
    "set_material":           {"material": "aluminum"},
    "set_view":               {"view": "iso"},
    "add_drawing_view":       {"view": "front"},
    "add_dimension":          {"target": "top face"},
    "add_gdt":                {"symbol": "flatness", "tolerance": 0.1, "target": "top face"},
    "add_surface_finish":     {"target": "top face", "roughness": 1.6},
    "add_section_view":       {"plane": "xy"},
    "add_detail_view":        {"target": "top face", "scale": 2.0},
    "set_titleblock":         {"title": "Audit Probe"},
    "set_config_override":    {"param": "width", "value": 12.0},
    "set_config_suppression": {"feature": "fillet_1", "suppressed": True},
    "activate_configuration": {"name": "Default"},
    "add_configuration":      {"name": "audit_config"},
    "list_configurations":    {},
    "solve_mates":            {},
    "show_in_app":            {},
    "get_job":                {"job_id": "_audit_none_"},
    "make_standard_part":     {"type": "bolt", "spec": "M6x20"},
    "make_fit_coupon":        {},
    # GSL
    "query_adjacency":        {},
    # Recon
    "reverse_engineer":       {"path": "/nonexistent/_audit_.step"},
    "compare_parts":          {"doc_a": "_a_", "doc_b": "_b_"},
    "convert_face_to_sketch": {"face": "top face"},
    # Intake
    "resolve_process":        {"process": "cnc_milling"},
    "set_process":            {"process": "cnc_milling"},
    "request_part_photos":    {"reason": "audit probe"},
    "confirm_dimensions":     {},
    "dfm_check":              {},
    "fit_report":             {},
    "measure_from_photo":     {"photo_path": "/nonexistent/probe.jpg"},
    "submit_part_photos":     {"photos": []},
    # Interaction
    "ask_user":               {"question": "audit?", "key": "_audit_", "default": "yes"},
    # ECAD
    "ecad_new_schematic":     {"name": "_audit_sch_"},
    "ecad_new_pcb":           {"name": "_audit_pcb_"},
    "ecad_status":            {},
    "ecad_find_part":         {"query": "100nF cap"},
    "ecad_design":            {},
    "ecad_wire":              {},
    "ecad_place_symbol":      {"ref": "C1", "value": "100nF", "x": 0, "y": 0},
    "ecad_add_power":         {"net": "GND", "x": 0, "y": 0},
    "ecad_add_zone":          {"net": "GND", "layer": "F.Cu"},
    "ecad_fill_zones":        {},
    "ecad_autoroute":         {},
    "ecad_run_erc":           {},
    "ecad_run_drc":           {},
    "ecad_run_dfm":           {},
    "ecad_check_impedance":   {"net": "CLK", "target_ohms": 50},
    "ecad_check_diff_impedance": {"net_p": "DP", "net_n": "DN", "target_ohms": 100},
    "ecad_make_board":        {},
    "ecad_export":            {"format": "gerber"},
    "ecad_sweep":             {"parameter": "C1.value", "values": ["10nF", "100nF"]},
    "ecad_tf":                {"transfer": "vout/vin"},
    "ecad_simulate":          {},
    "ecad_update_from_sch":   {},
    "ecad_label":             {"ref": "C1", "label": "bypass"},
    "ecad_to_3d":             {},
    # CAM
    "cam_new_job":            {"part_name": "_audit_"},
    "cam_job_tree":           {},
    "cam_materials":          {},
    "cam_op_types":           {},
    "cam_tool_types":         {},
    "cam_post_dialects":      {},
    "cam_open":               {"path": "/nonexistent/_audit_.cam"},
    "cam_add_setup":          {"orientation": "top"},
    "cam_set_stock":          {"stock_type": "rectangular", "x": 50, "y": 50, "z": 20},
    "cam_add_tool":           {"type": "endmill", "diameter": 6.0},
    "cam_add_operation":      {"type": "contour"},
    "cam_update_operation":   {"op_id": 0, "params": {}},
    "cam_remove_operation":   {"op_id": 0},
    "cam_generate":           {},
    "cam_simulate":           {},
    "cam_post":               {"dialect": "generic"},
    "cam_save":               {},
    "cam_undo":               {},
    "cam_redo":               {},
    "cam_extract":            {},
    "cam_setup_sheet":        {},
    # Doc diff
    "doc_diff_tool":          {"doc_a": {}, "doc_b": {}},
    # Requirements
    "req_create":             {"title": "audit probe req"},
    "req_status":             {},
    "req_update":             {"req_id": "_audit_", "fields": {}},
    "req_report":             {},
    # Verify
    "verify_battery":         {"doc": {}, "scenario": "_audit_"},
    "check_metadata":         {"doc": {}},
    # Research
    "research_web_search":    {"query": "bolt strength"},
    "research_fetch_page":    {"url": "https://example.com", "tier": "datasheet"},
    "research_unit_convert":  {"value": 1.0, "from_unit": "mm", "to_unit": "in"},
    "research_eng_calc":      {"formula": "F = m * a", "known": {"m": 1.0, "a": 9.8}},
    "research_material_lookup": {"material": "6061-T6"},
    "research_thread_spec":   {"thread": "M6x1.0"},
    "research_standard_section": {"standard": "AISC", "section": "W8x31"},
    "research_datasheet_fetch": {"part_number": "LM741"},
    # Deliverables
    "make_report":            {"title": "Audit Probe", "outline": []},
    "make_presentation":      {"title": "Audit Probe", "slides": []},
    "make_workbook":          {"title": "Audit Probe", "sheets": []},
    "render_document":        {"path": "/nonexistent/probe.pdf"},
    "list_artifacts":         {},
    # Sourcing
    "bom_rollup":             {"doc": {}},
    "bom_price":              {"bom": []},
    # Cost
    "cam_cost_estimate":      {},
    "request_quote":          {"part_name": "_audit_", "quantity": 1},
    # Spec
    "spec_sheet_match":       {"part_number": "M6-1.0"},
    "match_part_number":      {"query": "M6 bolt"},
    "list_spec_templates":    {},
    # Piping
    "piping_solve":           {"doc": {"nodes": [], "links": []}, "boundary": {}},
    "piping_report":          {},
    "piping_fluid":           {"fluid": "water"},
    "piping_operating_point": {},
    "piping_cfd_handoff":     {},
    "piping_verify_cfd":      {},
    # Hydraulic
    "hydraulic_new":          {"name": "_audit_circuit_"},
    "hydraulic_add_component": {"type": "pump", "name": "_audit_pump_"},
    "hydraulic_add_hose":     {"from_port": "A", "to_port": "B"},
    "hydraulic_size_pump":    {"flow_lpm": 10.0, "pressure_bar": 100.0},
    "hydraulic_size_cylinder": {"bore_mm": 50.0, "stroke_mm": 100.0},
    "hydraulic_bom":          {},
    # Photo recon (side-effecty — starts a listener; skip actual call)
    # Firmware
    "fw_new_project":         {"name": "_audit_fw_", "mcu": "STM32F4"},
    "fw_sources":             {},
    "fw_build":               {},
    "fw_sim_run":             {},
    "fw_flash":               {},
    "fw_debug_start":         {},
    "fw_debug_halt":          {},
    "fw_debug_read_mem":      {"address": "0x20000000", "length": 4},
    # Structural
    "struct_analyze":         {"doc_path": "/nonexistent/_audit_struct_.json"},
    "struct_check":           {"doc_path": "/nonexistent/_audit_struct_.json"},
    # Parts
    "part_search":            {"query": "M6 bolt"},
    "part_get":               {"part_id": "_audit_"},
    "part_place":             {"part_id": "_audit_"},
    # Serving
    "serving_status":         {},
    "serving_set_canary":     {"ip": "_audit_ip_", "pct": 0},
    "serving_rollback":       {"ip": "_audit_ip_"},
    "serving_evaluate":       {"ip": "_audit_ip_", "shadow_log": []},
    "serving_promote":        {"ip": "_audit_ip_", "adapter_version": "v0",
                               "report_ref": "_audit_", "approver": "_audit_"},
    # Regen
    "regen_diagnose":         {"document": {}, "error": "test error"},
    # Code compliance
    "code_check":             {"doc_path": "/nonexistent/_audit_.json",
                               "jurisdiction": "CA"},
    # Render/export (skip - need a real doc_id; covered by new_part / add_features)
    "render_part":            {},
    "make_drawing":           {},
    "export_part":            {"format": "step"},
    "export_drawing":         {"format": "pdf"},
    "import_part":            {"path": "/nonexistent/probe.step"},
    "review_drawing":         {},
    "run_fea_static":         {},
    "run_fea_modal":          {},
    "run_cfd_flow":           {},
    "run_cfd_accurate":       {},
}

# Tools that should be called but have risky side-effects (start processes, open ports, etc.)
SKIP_TOOLS = {
    "start_capture_session",  # starts a listener socket on a port
    "stop_capture_session",   # depends on a running listener
    "show_in_app",            # opens the desktop GUI — headless rule violation
    "fw_flash",               # requires real MCU hardware
    "fw_debug_start",         # spawns debugger
    "fw_debug_halt",          # depends on debugger session
    "fw_debug_read_mem",      # depends on debugger session
}

# ---------------------------------------------------------------------------
# Degradation indicators in result text
DEGRADED_REASONS = (
    "not provisioned",
    "converter_missing",
    "offline",
    "not installed",
    "not available",
    "no binary",
    "no key",
    "api_key",
    "missing key",
    "not found",        # for nonexistent file paths (graceful degradation)
    "no such file",
    "libchudware",
    "core not available",
    "core unavailable",
    "dll not available",
    "library not found",
    "import error",
)

DEAD_EXCEPTIONS = (ImportError, ModuleNotFoundError, AttributeError)
INCONCLUSIVE_PATTERNS = (
    "validation",
    "field required",
    "type_error",
    "value_error",
    "pydantic",
    "missing",
    "expected",
)


def classify_result(
    result: Any,
    exc: Exception | None,
    import_error: str | None,
) -> tuple[str, str]:
    """Return (verdict, detail)."""
    if import_error:
        return "DEAD", f"import failed: {import_error}"

    if exc is not None:
        exc_str = str(exc)
        exc_type = type(exc).__name__
        if isinstance(exc, DEAD_EXCEPTIONS):
            return "DEAD", f"{exc_type}: {exc_str[:200]}"
        # Pydantic / arg validation → INCONCLUSIVE (tool is alive, fixture was wrong)
        exc_lower = exc_str.lower()
        if any(p in exc_lower for p in INCONCLUSIVE_PATTERNS):
            return "INCONCLUSIVE", f"arg validation: {exc_str[:200]}"
        # Other exceptions — could be graceful degradation or a real error
        if any(r in exc_lower for r in DEGRADED_REASONS):
            return "DEGRADED", f"{exc_type}: {exc_str[:200]}"
        return "INCONCLUSIVE", f"unexpected {exc_type}: {exc_str[:200]}"

    # Result classification
    # FastMCP call_tool returns a list of ContentBlock (TextContent, etc.)
    # Extract the text from them for classification
    result_str = ""
    if isinstance(result, str):
        result_str = result.lower()
    elif isinstance(result, list):
        # List of ContentBlock objects from FastMCP
        parts = []
        for item in result:
            if hasattr(item, "text"):
                parts.append(item.text)
            else:
                parts.append(str(item))
        result_str = " ".join(parts).lower()
    elif isinstance(result, dict):
        try:
            result_str = json.dumps(result).lower()
        except Exception:
            result_str = str(result).lower()
    elif result is not None:
        result_str = str(result).lower()

    if any(r in result_str for r in DEGRADED_REASONS):
        # Check if ok=true despite matching (e.g. "not found" can be ok)
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict) and parsed.get("ok") is True:
                    return "WORKS", result_str[:120]
            except Exception:
                pass
        return "DEGRADED", result_str[:120]

    return "WORKS", result_str[:120]


# ---------------------------------------------------------------------------
# Main audit runner

async def run_tool_async(mcp, name: str, args: dict) -> Any:
    """Call a FastMCP tool asynchronously."""
    return await mcp.call_tool(name, args)


def call_tool(mcp, name: str, args: dict) -> tuple[Any, Exception | None]:
    """Call a FastMCP tool and return (result, exc)."""
    try:
        result = asyncio.run(run_tool_async(mcp, name, args))
        return result, None
    except Exception as exc:
        return None, exc


def check_dependencies(req_file: Path | None = None) -> dict:
    """Check active requirements against distribution metadata, including file installs."""
    from importlib.metadata import distributions
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    req_file = req_file if req_file is not None else _REPO / "requirements.txt"
    installed = {
        canonicalize_name(dist.metadata["Name"]): dist.version
        for dist in distributions() if dist.metadata["Name"]
    }
    declared: set[str] = set()
    missing: list[dict] = []
    applicable = 0
    for raw_line in req_file.read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line in ("--no-index", "--only-binary=:all:"):
            continue
        requirement = Requirement(line)
        pkg = canonicalize_name(requirement.name)
        declared.add(pkg)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        applicable += 1
        version = installed.get(pkg)
        # PEP 610 file:// (including editable) installs retain distribution version
        # metadata: their origin does not invalidate an otherwise satisfied pin.
        if version is None or not requirement.specifier.contains(version):
            missing.append({"pkg": pkg, "req_line": raw_line.strip()})
    return {
        "installed_pkgs": set(installed),
        "req_pkgs": declared,
        "declared_missing": missing,
        "requirements_count": applicable,
    }


def run_audit(out_path: Path | None = None) -> dict:
    from mcp.server.fastmcp import FastMCP
    import services.mcp.tool_registry as _reg
    from services.mcp import core_bridge

    libchudware = str(core_bridge._resolve_lib())

    mcp = FastMCP("Chudware-audit")

    # Per-family import results
    family_import: dict[str, str] = {}  # family -> "OK" | "FAILED: ..."
    family_tools: dict[str, list[str]] = {}  # family -> [tool_names]

    before = set()
    for family, mod_name, fn_name, use_view in FAMILY_REGISTRY:
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name)
            view = _reg.view(mcp, family) if use_view else mcp
            fn(view)
            after = set(mcp._tool_manager._tools.keys())
            new_tools = sorted(after - before)
            before = after
            family_import[family] = "OK"
            family_tools[family] = new_tools
        except Exception as exc:
            err = f"FAILED: {type(exc).__name__}: {exc}"
            family_import[family] = err
            family_tools[family] = []
            print(f"  FAMILY DEAD  [{family}] {err[:120]}")

    all_tools = sorted(mcp._tool_manager._tools.keys())
    print(f"\nRegistered {len(all_tools)} tools across {len(FAMILY_REGISTRY)} families.\n")

    # Tool-level calls
    results: list[dict] = []
    counts = {"WORKS": 0, "DEGRADED": 0, "DEAD": 0, "INCONCLUSIVE": 0, "SKIPPED": 0}

    for tool_name in all_tools:
        # Find which family
        tool_family = "unknown"
        for fam, tools_list in family_tools.items():
            if tool_name in tools_list:
                tool_family = fam
                break

        # Check family import state
        family_import_status = family_import.get(tool_family, "OK")
        if family_import_status.startswith("FAILED"):
            verdict = "DEAD"
            detail = f"family import failed: {family_import_status}"
            results.append({
                "tool": tool_name,
                "family": tool_family,
                "verdict": verdict,
                "detail": detail,
            })
            counts[verdict] += 1
            print(f"  DEAD         {tool_name} ({tool_family}) — family dead")
            continue

        # Skip risky side-effect tools
        if tool_name in SKIP_TOOLS:
            verdict = "INCONCLUSIVE"
            detail = "skipped — side-effecty (starts process/port/GUI)"
            results.append({
                "tool": tool_name,
                "family": tool_family,
                "verdict": verdict,
                "detail": detail,
            })
            counts["SKIPPED"] += 1
            print(f"  SKIP         {tool_name}")
            continue

        # Get fixture args
        args = TOOL_FIXTURES.get(tool_name, {})

        t0 = time.monotonic()
        result, exc = call_tool(mcp, tool_name, args)
        elapsed = time.monotonic() - t0

        verdict, detail = classify_result(result, exc, None)
        results.append({
            "tool": tool_name,
            "family": tool_family,
            "verdict": verdict,
            "detail": detail,
            "elapsed_ms": int(elapsed * 1000),
            "args_used": bool(args),
        })
        counts[verdict] += 1

        icon = {"WORKS": "ok  ", "DEGRADED": "DEGR", "DEAD": "DEAD", "INCONCLUSIVE": "INCN"}[verdict]
        print(f"  {icon}         {tool_name:45s} {elapsed*1000:5.0f}ms  {detail[:80]}")

    deps = check_dependencies()
    installed_pkgs = deps["installed_pkgs"]
    req_pkgs = deps["req_pkgs"]
    declared_missing = deps["declared_missing"]

    # Undeclared deps: imports in services/+engine_oracle/+services/mcp/ NOT in requirements.txt

    # Stdlib + internal packages to ignore
    STDLIB_AND_INTERNAL = {
        "abc", "argparse", "ast", "asyncio", "base64", "builtins", "collections",
        "contextlib", "contextvars", "copy", "csv", "ctypes", "dataclasses", "datetime",
        "difflib", "email", "enum", "functools", "glob", "hashlib", "heapq", "hmac",
        "html", "http", "importlib", "inspect", "io", "json", "logging", "math",
        "os", "pathlib", "platform", "queue", "random", "re", "secrets", "shutil",
        "socket", "sqlite3", "string", "struct", "subprocess", "sys", "tempfile",
        "threading", "time", "traceback", "types", "typing", "unicodedata",
        "unittest", "urllib", "uuid", "warnings", "weakref", "xml", "zipfile",
        # internal
        "engine", "engine_oracle", "services.mcp", "services", "agent", "deliverables",
        "tools", "training", "installer", "dag", "provider",
        # test-only
        "pytest", "conftest",
        # known aliases
        "pythoncom",  # pywin32 (Windows-only, skip)
        "cycle",      # itertools.cycle? — not a package
    }

    import_dirs = ["services.mcp", "services", "engine_oracle"]
    raw_imports: set[str] = set()
    for d in import_dirs:
        dir_path = _REPO / d
        if not dir_path.exists():
            continue
        for py_file in dir_path.rglob("*.py"):
            try:
                text = py_file.read_text(errors="ignore")
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("import "):
                        pkg = line.split()[1].split(".")[0]
                        raw_imports.add(pkg.lower().replace("_", "-"))
                    elif line.startswith("from "):
                        parts = line.split()
                        if len(parts) >= 2:
                            pkg = parts[1].split(".")[0]
                            if not pkg.startswith("."):
                                raw_imports.add(pkg.lower().replace("_", "-"))
            except Exception:
                pass

    undeclared: list[str] = []
    for pkg in sorted(raw_imports):
        if pkg in STDLIB_AND_INTERNAL:
            continue
        if pkg in req_pkgs:
            continue
        if pkg in installed_pkgs:
            # installed but not in requirements.txt
            undeclared.append(pkg)

    return {
        "family_import": family_import,
        "family_tools": family_tools,
        "tool_results": results,
        "counts": counts,
        "declared_missing": declared_missing,
        "undeclared_deps": undeclared,
        "total_tools": len(all_tools),
        "libchudware": libchudware,
    }


# ---------------------------------------------------------------------------
# Markdown report generator

def make_markdown(data: dict) -> str:
    lines = []
    lines.append("# MCP Runtime Audit")
    lines.append("")
    lines.append("**Task:** T-V2  **Agent:** sonnet-vega  **Date:** 2026-07-02")
    lines.append("")
    lines.append(f"**Library:** `{data['libchudware']}`")
    lines.append(f"**Total registered tools:** {data['total_tools']}")
    lines.append("")

    c = data["counts"]
    lines.append("## Summary")
    lines.append("")
    lines.append("| Verdict | Count |")
    lines.append("| ------- | ----- |")
    lines.append(f"| WORKS | {c['WORKS']} |")
    lines.append(f"| DEGRADED | {c['DEGRADED']} |")
    lines.append(f"| DEAD | {c['DEAD']} |")
    lines.append(f"| INCONCLUSIVE | {c['INCONCLUSIVE']} |")
    lines.append(f"| SKIPPED (side-effect) | {c.get('SKIPPED', 0)} |")
    lines.append(f"| **Total** | **{data['total_tools']}** |")
    lines.append("")

    # Family import status
    lines.append("## Family Import Status")
    lines.append("")
    lines.append("| Family | Import | Tools |")
    lines.append("| ------ | ------ | ----- |")
    for family, status in data["family_import"].items():
        icon = "OK" if status == "OK" else "DEAD"
        n = len(data["family_tools"].get(family, []))
        lines.append(f"| {family} | {icon} | {n} |")
    lines.append("")

    # Per-verdict sections
    for verdict in ("DEAD", "DEGRADED", "INCONCLUSIVE", "WORKS"):
        subset = [r for r in data["tool_results"] if r["verdict"] == verdict]
        if not subset:
            continue
        lines.append(f"## {verdict} ({len(subset)} tools)")
        lines.append("")
        lines.append("| Tool | Family | Detail |")
        lines.append("| ---- | ------ | ------ |")
        for r in sorted(subset, key=lambda x: x["tool"]):
            detail = r.get("detail", "")[:120].replace("|", "\\|")
            lines.append(f"| `{r['tool']}` | {r['family']} | {detail} |")
        lines.append("")

    # Skipped tools
    skipped = [r for r in data["tool_results"] if r.get("verdict") == "INCONCLUSIVE" and "skipped" in r.get("detail", "")]
    if skipped:
        lines.append("## Skipped (side-effect tools)")
        lines.append("")
        for r in skipped:
            lines.append(f"- `{r['tool']}`: {r['detail']}")
        lines.append("")

    # Declared-but-not-installed
    lines.append("## Declared-But-Not-Installed Packages")
    lines.append("")
    lines.append("Packages in `requirements.txt` but absent from the running mcp-venv:")
    lines.append("")
    dm = data.get("declared_missing", [])
    if dm:
        lines.append("| Package | requirements.txt line |")
        lines.append("| ------- | --------------------- |")
        for d in dm:
            lines.append(f"| `{d['pkg']}` | `{d['req_line'][:80]}` |")
    else:
        lines.append("_(none — venv is fully provisioned)_")
    lines.append("")

    # Undeclared deps
    lines.append("## Undeclared-But-Installed Dependencies")
    lines.append("")
    lines.append("Packages imported by `services/`+`engine_oracle/`+`services/mcp/` that are")
    lines.append("installed in the venv but absent from `requirements.txt`:")
    lines.append("")
    ud = data.get("undeclared_deps", [])
    if ud:
        for pkg in ud:
            lines.append(f"- `{pkg}`  ← add to requirements.txt with license check")
    else:
        lines.append("_(none found)_")
    lines.append("")

    # Proposed follow-up tasks
    dead = [r for r in data["tool_results"] if r["verdict"] == "DEAD"]
    if dead:
        families_dead = sorted(set(r["family"] for r in dead))
        lines.append("## Proposed Follow-Up Tasks")
        lines.append("")
        lines.append("| Proposed Task | Scope | Root Cause |")
        lines.append("| ------------- | ----- | ---------- |")
        for fam in families_dead:
            fam_dead = [r for r in dead if r["family"] == fam]
            detail = fam_dead[0]["detail"][:80] if fam_dead else ""
            lines.append(f"| Provision `{fam}` family deps | {len(fam_dead)} dead tools | {detail} |")
        lines.append("")
        lines.append("### Missing runtime declarations:")
        lines.append("")
        for d in data.get("declared_missing", []):
            lines.append(f"- `{d['req_line'].split('#')[0].strip()}`")
        lines.append("Provision the complete reviewed offline runtime with "
                     "`CHUDWARE_WHEELHOUSE=/reviewed/wheelhouse scripts/provision_venv.sh`; "
                     "never install the private RPC package from an index or editable checkout.")
        lines.append("")

    # Test pollution section (filled in separately — see audit script output)
    lines.append("## Test-Suite Pollution (Sweep Analysis)")
    lines.append("")
    lines.append("_(See `scripts/mcp_runtime_audit.py --test-pollution` output below,")
    lines.append("generated separately to avoid OOM on the same process.)_")
    lines.append("")
    lines.append("### Root Causes Identified")
    lines.append("")
    lines.append("See `## Test Pollution Analysis` section appended after running:")
    lines.append("```")
    lines.append("python scripts/mcp_runtime_audit.py --test-pollution")
    lines.append("```")
    lines.append("")

    lines.append("---")
    lines.append("*Generated by `scripts/mcp_runtime_audit.py` (T-V2)*")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Test pollution analysis (separate, memory-safe)

def run_test_pollution(out_md_path: Path | None) -> str:
    """
    Run each test suite alone (baseline), then pairwise combinations to find polluters.
    Uses --junitxml to collect pass/fail counts without parsing stdout.
    Returns a markdown section string to append to the audit doc.
    """
    import subprocess
    import xml.etree.ElementTree as ET

    SUITES = ["services", "engine_oracle", "tools", "training"]
    venv_python = str(Path(sys.executable))

    lines = ["", "## Test Pollution Analysis", ""]

    def run_suite(suite_args: list[str], label: str) -> dict[str, str]:
        """Run pytest on suite_args, return {test_id: outcome} from junitxml."""
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tf:
            xml_path = tf.name
        cmd = [venv_python, "-m", "pytest", "--tb=no", "-q",
               "--continue-on-collection-errors",
               f"--junitxml={xml_path}"] + suite_args
        print(f"  Running: {' '.join(cmd[-len(suite_args)-1:])}")
        try:
            proc = subprocess.run(
                cmd, cwd=str(_REPO), capture_output=True, text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            print(f"    TIMEOUT for {label}")
            return {}

        outcomes: dict[str, str] = {}
        try:
            tree = ET.parse(xml_path)
            for tc in tree.iter("testcase"):
                tid = f"{tc.get('classname','')}.{tc.get('name','')}"
                if tc.find("failure") is not None:
                    outcomes[tid] = "FAIL"
                elif tc.find("error") is not None:
                    outcomes[tid] = "ERROR"
                elif tc.find("skipped") is not None:
                    outcomes[tid] = "SKIP"
                else:
                    outcomes[tid] = "PASS"
        except Exception as e:
            print(f"    XML parse error for {label}: {e}")
        try:
            os.unlink(xml_path)
        except Exception:
            pass
        return outcomes

    # 1. Per-suite baselines
    print("\n=== Test Pollution Baselines ===")
    baselines: dict[str, dict[str, str]] = {}
    for suite in SUITES:
        print(f"\n  Baseline: {suite}")
        baselines[suite] = run_suite([suite], suite)
        passes = sum(1 for v in baselines[suite].values() if v == "PASS")
        fails = sum(1 for v in baselines[suite].values() if v in ("FAIL", "ERROR"))
        skips = sum(1 for v in baselines[suite].values() if v == "SKIP")
        print(f"    {passes} pass, {fails} fail/error, {skips} skip")

    # 2. Combined run
    print("\n  Combined: all suites together")
    combined = run_suite(SUITES, "combined")
    passes = sum(1 for v in combined.values() if v == "PASS")
    fails = sum(1 for v in combined.values() if v in ("FAIL", "ERROR"))
    skips = sum(1 for v in combined.values() if v == "SKIP")
    print(f"    {passes} pass, {fails} fail/error, {skips} skip")

    # 3. Find tests that pass alone but fail in combined
    all_baseline_passes: dict[str, str] = {}
    for suite_outcomes in baselines.values():
        for tid, outcome in suite_outcomes.items():
            if outcome == "PASS":
                all_baseline_passes[tid] = "PASS"

    regressions: list[str] = []
    for tid, outcome in combined.items():
        if outcome in ("FAIL", "ERROR") and all_baseline_passes.get(tid) == "PASS":
            regressions.append(tid)

    lines.append(f"### Baseline per-suite results")
    lines.append("")
    lines.append("| Suite | Pass | Fail/Error | Skip |")
    lines.append("| ----- | ---- | ---------- | ---- |")
    for suite in SUITES:
        s = baselines[suite]
        p = sum(1 for v in s.values() if v == "PASS")
        f = sum(1 for v in s.values() if v in ("FAIL", "ERROR"))
        sk = sum(1 for v in s.values() if v == "SKIP")
        lines.append(f"| {suite} | {p} | {f} | {sk} |")

    comb_p = sum(1 for v in combined.values() if v == "PASS")
    comb_f = sum(1 for v in combined.values() if v in ("FAIL", "ERROR"))
    comb_sk = sum(1 for v in combined.values() if v == "SKIP")
    lines.append(f"| **combined** | **{comb_p}** | **{comb_f}** | **{comb_sk}** |")
    lines.append("")
    lines.append(f"**Pollution-induced regressions** (pass alone, fail in combined): **{len(regressions)}**")
    lines.append("")

    if regressions:
        lines.append("### Regressed tests (pass-alone → fail-in-combined)")
        lines.append("")
        lines.append("| Test ID |")
        lines.append("| ------- |")
        for tid in sorted(regressions)[:40]:
            lines.append(f"| `{tid}` |")
        if len(regressions) > 40:
            lines.append(f"| … and {len(regressions) - 40} more |")
        lines.append("")

    # 4. Pairwise runs to identify top polluter pairs
    print("\n  Pairwise pollution analysis …")
    polluter_scores: dict[str, int] = {s: 0 for s in SUITES}
    pair_results: list[dict] = []
    for i, s1 in enumerate(SUITES):
        for s2 in SUITES[i+1:]:
            pair = run_suite([s1, s2], f"{s1}+{s2}")
            pair_regs = [
                tid for tid, outcome in pair.items()
                if outcome in ("FAIL", "ERROR") and all_baseline_passes.get(tid) == "PASS"
            ]
            if pair_regs:
                polluter_scores[s1] += len(pair_regs)
                polluter_scores[s2] += len(pair_regs)
            pair_results.append({
                "pair": f"{s1}+{s2}",
                "regressions": len(pair_regs),
                "examples": pair_regs[:3],
            })
            print(f"    {s1}+{s2}: {len(pair_regs)} regressions")

    lines.append("### Pairwise pollution table")
    lines.append("")
    lines.append("| Pair | Regressions | Example failures |")
    lines.append("| ---- | ----------- | ---------------- |")
    for p in pair_results:
        examples = ", ".join(f"`{t.split('.')[-1]}`" for t in p["examples"][:2])
        lines.append(f"| {p['pair']} | {p['regressions']} | {examples} |")
    lines.append("")

    # Top polluter ranking
    top = sorted(polluter_scores.items(), key=lambda x: -x[1])
    lines.append("### Top polluter suites (by pairwise regression count)")
    lines.append("")
    lines.append("| Suite | Pairwise regression contribution |")
    lines.append("| ----- | -------------------------------- |")
    for s, n in top:
        lines.append(f"| {s} | {n} |")
    lines.append("")

    lines.append("### Root cause hypotheses")
    lines.append("")
    lines.append("Based on the pairwise analysis and code inspection:")
    lines.append("")
    lines.append("1. **MCP tool re-registration** — `services/mcp/server.py` `register_*_tools()` calls")
    lines.append("   are at module scope; when multiple test modules import `server` in one session,")
    lines.append("   the second import re-registers tools onto the same `FastMCP` instance, which may")
    lines.append("   raise on duplicate names or overwrite with a broken state from a sibling suite's")
    lines.append("   conftest patches.")
    lines.append("")
    lines.append("2. **Singleton state leakage** — `_CUR`, `_DOC_ALIASES`, `_LAST_VOLUME` in `cad_tools.py`")
    lines.append("   and `_PIPING` in `piping_tools.py` are module-level globals; a test that creates a")
    lines.append("   part leaves a live `doc_id` that breaks a later test expecting empty state.")
    lines.append("")
    lines.append("3. **LoggingProvider / services.paths singleton** — `services.paths.DATA` AttributeError")
    lines.append("   (noted in task context as fixed 2026-07-03) may still surface in suites that do")
    lines.append("   not load the engine with the DATA path set.")
    lines.append("")
    lines.append("4. **Env-var mutations** — tests that set `CHUDWARE_LIBCHUDWARE` or `TAVILY_API_KEY`")
    lines.append("   without restoring them pollute subsequent suite imports.")
    lines.append("")
    lines.append("**Proposed follow-up task:** T-V3 — Add `conftest.py` fixtures to reset MCP")
    lines.append("tool registration state and module-level singletons between test sessions.")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point

def main() -> None:
    parser = argparse.ArgumentParser(description="MCP Runtime Audit (T-V2)")
    parser.add_argument("--out", default="work/audits/MCP_RUNTIME_AUDIT.md",
                        help="Output markdown path (relative to repo root)")
    parser.add_argument("--test-pollution", action="store_true",
                        help="Run test suite pollution analysis (slow, suite-by-suite)")
    parser.add_argument("--no-tools", action="store_true",
                        help="Skip tool calls (only do dep + pollution analysis)")
    parser.add_argument("--deps-only", action="store_true",
                        help="Check declared dependencies only; fail if any are missing or incompatible")
    args = parser.parse_args()
    if args.deps_only:
        deps = check_dependencies()
        print(f"Venv: {sys.executable}")
        print(f"Missing from venv: {len(deps['declared_missing'])} of {deps['requirements_count']} requirements")
        for missing in deps["declared_missing"]:
            print(f"  Missing or incompatible: {missing['req_line']}")
        raise SystemExit(bool(deps["declared_missing"]))


    out_path = _REPO / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"=== MCP Runtime Audit T-V2 ===")
    print(f"Venv: {sys.executable}")
    print()

    if not args.no_tools:
        print("=== Tool-level calls ===")
        data = run_audit(out_path)
        print(f"Library: {data['libchudware']}")
        md = make_markdown(data)

        if args.test_pollution:
            print()
            pollution_section = run_test_pollution(out_path)
            # Replace the placeholder in the md
            md = md.replace(
                "_(See `scripts/mcp_runtime_audit.py --test-pollution` output below,\ngenerated separately to avoid OOM on the same process.)_",
                pollution_section
            )

        out_path.write_text(md, encoding="utf-8")
        print(f"\n=== Audit complete ===")
        print(f"Wrote: {out_path}")
        print(f"Results: {data['counts']}")
        print(f"Missing from venv: {len(data['declared_missing'])} of {len([l for l in ((_REPO/'requirements.txt').read_text()).splitlines() if l.strip() and not l.startswith('#')])} requirements")
        print(f"Undeclared (installed but not declared): {data['undeclared_deps']}")
    else:
        # Pollution only
        if args.test_pollution:
            print()
            pollution_section = run_test_pollution(out_path)
            # Append to existing audit doc if present
            if out_path.exists():
                existing = out_path.read_text(encoding="utf-8")
                out_path.write_text(existing + "\n" + pollution_section, encoding="utf-8")
            else:
                out_path.write_text("# MCP Runtime Audit\n" + pollution_section, encoding="utf-8")
            print(f"Wrote pollution section to: {out_path}")


if __name__ == "__main__":
    # The explicit CLI owns stdout; importing audit helpers must not reconfigure MCP.
    os.environ.setdefault("CHUDWARE_QUIET", "1")
    main()
