"""Newline-delimited JSON-RPC worker, spawned by the native host over stdio.

Protocol (one JSON object per line):
  request : {"id": int, "method": str, "params": {...}}
  response: {"id": int, "ok": true, "result": {...}}
            {"id": int, "ok": false, "error": str}

stdout carries ONLY protocol JSON. All diagnostics go to stderr, and the model's own
captured stdout is returned inside the result — so the channel stays clean.

Methods:
  ping                              -> {"pong": true, "build123d": <version>}
  run_model  {code, name?}          -> build a solid OR a multi-part assembly; STEP/STL +
                                       mass props. If the script defines `parts`/`assembly`
                                       the result carries per-part exports + a fit report
                                       (pairwise interference / clearance); single-`result`
                                       scripts behave exactly as before.
  shutdown                          -> graceful exit

Streaming: long-running methods receive an ``emit`` callback. Each call writes a
notification line ``{"id": <reqid>, "event": <kind>, "data": {...}}`` to stdout BEFORE the
final ``{"id", "ok", "result"}`` response, so a UI can show progress as it happens.
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from services import paths  # engine_oracle/paths.py re-homed to services/ (T-RF20)
# T-RF20: the build123d code executor was re-homed to services/kernel_exec.py (it needs a
# Python interpreter by nature and stays services-side, NOT in engine_oracle).
from services.kernel_exec import run_code_multi

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
log = logging.getLogger("ai-cad.worker")

EmitFn = Callable[[dict], None]


def _ping(_params: dict, _emit_event: EmitFn) -> dict:
    try:
        import build123d

        ver = getattr(build123d, "__version__", "?")
    except Exception:  # noqa: BLE001
        ver = None
    return {"pong": True, "build123d": ver, "protocol": 2}


def _run_model(params: dict, _emit_event: EmitFn) -> dict:
    code = params["code"]
    name = params.get("name", "model")
    out_dir = paths.model_dir(name)
    # run_code_multi auto-detects parts/assembly; a single-`result` script returns the same
    # geometry/props as before (is_assembly=False, one entry in parts, fit=None).
    res = run_code_multi(code, out_dir, name)
    return asdict(res)


def _get_prompts(_params: dict, _emit_event: EmitFn) -> dict:
    """The built-in prompts (+ the active override), so the GUI's Settings editor can show
    and prefill them without duplicating the text in C++."""
    from agent import config, loop

    return {"system_prompt": loop.SYSTEM_PROMPT,
            "plan_prompt": loop.PLAN_PROMPT,
            "override": config.load_config().get("system_prompt", "")}




# =========================================================================== C22a orchestrator (M9)
#
# MIGRATED TO THE C++ CORE (libaicad). The v2 AI agent — agent_run_v2 (design), agent_ask_v2
# (ask), agent_pm_assist_v1 (PM assist) and get_prompts_v2 (prompt catalog) — now runs
# in-process in the C++ core, which the host routes to by default (AI_CAD_CPP_AGENT on). The
# Python orchestrator/ask/pm_assist modules were deleted after a final differential-parity pass
# (orch 20 / micro 65 / assist 87 / agent-support 163 all MATCH). T-RF5 then removed the LEGACY
# agent_design code-gen path too (agent/loop.py's run/run_turn went with it) — agent_run_v2 in
# the C++ core is the single agent entry. agent_cancel remains as a harmless no-op for any run
# not tracked in this process (the C++ core owns v2 run lifecycles now).

# Active orchestrator runs: run_id -> control. Retained for agent_cancel; normally empty now.
_RUNS: dict = {}


def _agent_cancel(params: dict, _emit_event: EmitFn) -> dict:
    """Best-effort cancel: flag the run if this process tracks it (it no longer drives v2 runs)."""
    run_id = params["run_id"]
    control = _RUNS.get(run_id)
    if control is None:
        return {"cancelled": False, "reason": "unknown or finished run"}
    control.cancelled = True
    return {"cancelled": True}


# The Python doc modeling engine is gone — every doc/sketch/evaluate/config/drawing/instant3d/
# sim_study/sim-bridge/weldment/2D-export RPC now runs in the C++ core (libaicad); the host routes
# them by the doc-cpp / dwg-cpp / minting rule (see host/src/worker_client.cpp). What stays in the
# Python worker: run_model (build123d script builds), the format-matrix probe, the
# vendor-parts registry, and the AI-ECAD surface.

def _list_editable_prompts(_params: dict, _emit_event=None):
    """Settings → AI → Prompts: YAML mission roles + core .md prompts (merged)."""
    try:
        from services.agent.roles import list_editable_prompts
        return list_editable_prompts()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


_METHODS = {
    "ping": _ping,
    "run_model": _run_model,
    "get_prompts": _get_prompts,
    "list_editable_prompts": _list_editable_prompts,
}

# Format-matrix probe (doc_formats) — REMOVED 2026-07-22 (T-RF19): the matrix is C++
# (core/doc/formats.cpp, dispatched via libaicad). import_file / export_file moved to the C++
# core earlier; the pure helpers in engine.doc.formats stay (engine.ecad.threed imports
# import_path) but no longer register worker methods.

# C22a orchestrator (M9) — agent_run_v2 / get_prompts_v2 run in the C++ core (libaicad); only the
# no-op agent_cancel remains in Python (the C++ core owns v2 run lifecycles now).
_METHODS.update({
    "agent_cancel": _agent_cancel,
})

# Methods that emit the structured error envelope {code, message, detail} on failure. The doc
# engine (which owned most of these) is now C++; only surviving Python surfaces register here.
_STRUCTURED_ERROR_METHODS: set[str] = set()

# ---- Supplier/vendor parts cache (registry only; vendor_part_insert runs in the C++ core) ----
from services.parts import vendor_parts as _vendor_parts  # noqa: E402 — re-homed (T-RF20)

_METHODS.update(_vendor_parts.WORKER_METHODS)
_STRUCTURED_ERROR_METHODS.update(_vendor_parts.WORKER_METHODS.keys())

# === ECAD (Wave C2) — AI-ECAD electrical design methods (schematic/PCB/sim/fab/3d) ===
try:
    from engine.ecad.rpc import METHODS as _ECAD_METHODS, STRUCTURED_ERROR_METHODS as _ECAD_SEM
    _METHODS.update(_ECAD_METHODS)
    _STRUCTURED_ERROR_METHODS |= _ECAD_SEM
except Exception as _ecad_import_err:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger("ai-cad.worker").warning(
        "engine.ecad.rpc unavailable — ecad_* methods not registered: %s", _ecad_import_err)
# === end ECAD ===

# === EM simulation family (T-SIM11) — em_status / em_eigenmode / em_driven / em_timedomain ===
# Full-wave electromagnetics over the pluggable adapter layer (engine.sim.em): openEMS FDTD
# when installed, builtin FDFD eigensolver + analytic uniform-guide model always. GPL solver
# modules are imported lazily inside their adapter only (contracts/license_debt.md).
try:
    from services.sim.em.worker_rpc import WORKER_METHODS as _EM_METHODS  # noqa: E402
    _METHODS.update(_EM_METHODS)
except Exception as _em_import_err:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger("ai-cad.worker").warning(
        "engine.sim.em unavailable — em_* methods not registered: %s", _em_import_err)
# === end EM simulation ===

# === Deliverables preview seam (T-F16, Pillar 15) — render_document / list_artifacts ===
# Reaches the deliverables service (contract deliverables/1 §5.4/§5.5) over the same worker seam
# the ImGui GUI uses for every other service, so the preview pane + artifact cards fetch previews
# asynchronously (AsyncRpc) with no bespoke transport and no format code in C++.
try:
    from deliverables.worker_rpc import WORKER_METHODS as _DELIV_METHODS  # noqa: E402
    _METHODS.update(_DELIV_METHODS)
except Exception as _deliv_import_err:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger("ai-cad.worker").warning(
        "deliverables.worker_rpc unavailable — render_document/list_artifacts not registered: %s",
        _deliv_import_err)
# === end Deliverables ===

# === Parts library seam (T-U2, Pillar 17) — part_search / part_get / part_place ===
# Reaches the unified parts hub (services/parts/, re-homed from engine_oracle/parts_hub/
# in T-RF19) over the same worker seam so the C++/ImGui parts browser panel
# (host/src/ui/parts/) fetches results asynchronously via AsyncRpc with no bespoke
# transport.  Degrades gracefully when the hub or its backing data is unavailable
# (returns empty results, never a traceback).
try:
    from services.parts.worker_rpc import WORKER_METHODS as _PARTS_METHODS  # noqa: E402
    _METHODS.update(_PARTS_METHODS)
except Exception as _parts_import_err:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger("ai-cad.worker").warning(
        "services.parts.worker_rpc unavailable — part_search/get/place not registered: %s",
        _parts_import_err)
# === end Parts library ===
# === Agent GUI seams (T-RF19) — subscription_* / session_* / skills_* / triggers_* /
# ai_roles_* / model_rankings_* / bench_* ===
# These tables used to squat in engine_oracle/parts_hub/ and were folded into
# services/agent/worker_rpc.py — thin wrappers over services.agent.* with zero parts
# coupling. Registered from their real home now; same graceful-absent pattern.
try:
    from services.agent.worker_rpc import WORKER_METHODS as _AGENT_GUI_METHODS  # noqa: E402
    _METHODS.update(_AGENT_GUI_METHODS)
except Exception as _agent_gui_import_err:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger("ai-cad.worker").warning(
        "services.agent.worker_rpc unavailable — subscription_*/session_*/skills_*/"
        "triggers_*/ai_roles_*/bench_* not registered: %s", _agent_gui_import_err)
# === end Agent GUI seams ===
# === Reverse Engineering seam — list/get/start_session/add_images/ingest_datasheet ===
# Worker surface for the RE tab (host/src/ui/reveng/) + the Parts Library
# "Reverse-engineer this part" delivery action.  Thin bridge over the reveng
# session store (services/mcp/reveng_tools) so the GUI reaches it via AsyncRpc
# with no bespoke transport.  Gracefully absent when the RE suite isn't installed
# (the tab degrades to "Reverse Eng tools unavailable").
try:
    from services.reveng.worker_rpc import REVENG_WORKER_METHODS as _REVENG_METHODS  # noqa: E402
    _METHODS.update(_REVENG_METHODS)
except Exception as _reveng_import_err:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger("ai-cad.worker").warning(
        "services.reveng.worker_rpc unavailable — reverse-engineering RPCs not registered: %s",
        _reveng_import_err)
# === end Reverse Engineering ===
# === BOM / sourcing seam (T-U4) — REMOVED 2026-07-22 (T-RF18) ===
# bom_rollup / bom_price / request_quote / match_part_number / vendor_part_fetch /
# spec_sheet_match / list_spec_templates / estimate_made_cost are C++-dispatched
# (core/sourcing, routed via worker_client.cpp); the oracle modules were deleted.
# === end BOM / sourcing ===

# === DAG steering seam (T-F17) — dag_status / dag_approve / dag_abort ===
# Reaches the DAG orchestrator (services/dag/) over the same worker seam so the
# mission-control panel (ui/imgui/src/ui/mission_control_panel.cpp) can steer a
# gate-paused run (approve/reject) and locate the events.jsonl replay trail via
# AsyncRpc.  Thin wrappers over services.dag.executor — same single-implementation
# delegation as the dag_* MCP tools.  Gracefully absent when langgraph isn't
# provisioned (returns ok:false envelopes, never a traceback).
try:
    from services.dag.worker_rpc import WORKER_METHODS as _DAG_METHODS  # noqa: E402
    _METHODS.update(_DAG_METHODS)
except Exception as _dag_import_err:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger("ai-cad.worker").warning(
        "services.dag.worker_rpc unavailable — dag_status/dag_approve/dag_abort"
        " not registered: %s", _dag_import_err)
# === end DAG steering ===

# === Sub-agent registry seam (AI workspace v2 Phase 4, M11/M15–M17) ===
# subagent_list / subagent_transcript / subagent_steer / subagent_terminate — the GUI's
# sub-agent chat viewer + live steering reach the file-backed session registry
# (services/agent/subagent_registry.py) over the same worker seam.  Steering is queued
# and consumed at the sub-agent's next turn boundary (agent_turn.run_turn).
try:
    from services.agent.subagent_rpc import WORKER_METHODS as _SUBAGENT_METHODS  # noqa: E402
    _METHODS.update(_SUBAGENT_METHODS)
except Exception as _subagent_import_err:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger("ai-cad.worker").warning(
        "services.agent.subagent_rpc unavailable — subagent_* methods not registered: %s",
        _subagent_import_err)
# === end Sub-agent registry ===

# === PLM vault seam (T-R5, Pillar 19) — plm_open/status/history/save/checkpoint/... ===
# Reaches the PLM vault engine (services/plm/) over the same worker seam so the in-editor
# PLM GUI (ui/imgui/src/ui/plm_panel.cpp: history timeline, checkpoint/release toolbar,
# compare view, revision badges) drives the SAME single implementation as the plm_* MCP
# tools.  Gracefully absent when dulwich isn't provisioned (each method returns the
# contract-shaped {ok:false, reason:"no_vault"} envelope, never a traceback).
try:
    from services.plm.worker_rpc import WORKER_METHODS as _PLM_METHODS  # noqa: E402
    _METHODS.update(_PLM_METHODS)
except Exception as _plm_import_err:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger("ai-cad.worker").warning(
        "services.plm.worker_rpc unavailable — plm_* worker methods not registered: %s",
        _plm_import_err)
# === end PLM vault ===

# === Firmware seam (T-FIN5) — REMOVED 2026-07-22 (T-RF15) ===
# The full fw_* surface is C++ (core/firmware, routed via worker_client.cpp);
# engine_oracle/firmware was deleted. The MCP firmware tools call the core bridge.
# === end Firmware seam ===

# Multi-physics Simulate-tab seam (T-FIN24): advanced FEA proxies (ccx buckling /
# contact), frequency-response post-processing, and cross-sim handoff mirrors
# (ecad_thermal_handoff / piping_verify_cfd / heatsink_select forward seam) so the
# desktop Simulate workspace drives the SAME implementations as the MCP tools.
# Gracefully absent; the GUI's sim_caps doctor shows honest guidance instead.
try:
    from services.sim.multiphysics_rpc import WORKER_METHODS as _MPHYS_METHODS  # noqa: E402
    _METHODS.update(_MPHYS_METHODS)
except Exception as _mphys_import_err:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger("ai-cad.worker").warning(
        "engine.sim.multiphysics_rpc unavailable — sim_caps/handoff worker methods "
        "not registered: %s", _mphys_import_err)
# === end multi-physics seam ===


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def serve() -> None:
    log.info("worker ready (runtime=%s)", paths.RUNTIME)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:  # noqa: BLE001
            _emit({"id": None, "ok": False, "error": f"bad json: {e}"})
            continue
        rid = req.get("id")
        method = req.get("method")
        if method == "shutdown":
            _emit({"id": rid, "ok": True, "result": {"bye": True}})
            break
        fn = _METHODS.get(method)
        if fn is None:
            _emit({"id": rid, "ok": False, "error": f"unknown method: {method}"})
            continue

        # Per-request event sink: tags each progress notification with this request's id.
        def emit_event(data: dict, _rid=rid) -> None:
            _emit({"id": _rid, "event": data.get("kind", "event"), "data": data})

        try:
            result = fn(req.get("params") or {}, emit_event)
            _emit({"id": rid, "ok": True, "result": result})
        except Exception as e:  # noqa: BLE001
            log.error("method %s failed: %s\n%s", method, e, traceback.format_exc())
            if method in _STRUCTURED_ERROR_METHODS:
                # Structured envelope {code, message, detail}. The surviving structured-error
                # surfaces (ECAD, vendor) raise their own OpError exposing to_envelope(); duck-type
                # it so the worker no longer imports the deleted engine.doc.ops.
                to_env = getattr(e, "to_envelope", None)
                if callable(to_env):
                    _emit({"id": rid, "ok": False, "error": to_env()})
                else:
                    _emit({"id": rid, "ok": False, "error": {
                        "code": "internal", "message": f"{type(e).__name__}: {e}",
                        "detail": {"traceback": traceback.format_exc()}}})
            else:
                # Legacy methods keep the plain-string error (frozen wire shape).
                # Lead with the concise "Type: message" (callers often show only the first line)
                # then the full traceback for debugging.
                _emit({"id": rid, "ok": False,
                       "error": f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"})


if __name__ == "__main__":
    serve()
