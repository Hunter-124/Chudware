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
  agent_design {request, name?, session_id?, reset?, max_iters?}
                                    -> run one turn of the agent loop against a persistent
                                       session (needs a provider). Emits live "event"
                                       notification lines per iteration before the result.
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

from engine import paths
from engine.kernel import run_code_multi

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
log = logging.getLogger("ai-cad.worker")

# Live conversation state keyed by session id, so follow-up requests refine the existing
# part instead of restarting. Held in-process for the life of the worker.
_SESSIONS: dict = {}

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


def _agent_design(params: dict, emit_event: EmitFn) -> dict:
    from agent import config, loop

    request = params["request"]
    name = params.get("name", "agent_model")
    session_id = params.get("session_id") or name
    cfg = config.load_config()
    # Iteration budget: explicit param > config.json "max_iters" > loop's generous default.
    # The loop normally converges (stops the moment a render matches) well before the cap.
    mi = params.get("max_iters")
    if mi is None:
        mi = cfg.get("max_iters")
    max_iters = int(mi) if mi else None
    # Plan mode: explicit param > config.json "plan_mode" > ON. (Only the first turn of a
    # session actually plans; follow-up turns refine against the stored plan.)
    plan = params.get("plan")
    if plan is None:
        plan = cfg.get("plan_mode", True)

    if params.get("reset"):
        _SESSIONS.pop(session_id, None)
    session = _SESSIONS.get(session_id)
    if session is None or session.name != name:
        session = loop.Session(name=name)
        _SESSIONS[session_id] = session

    provider = config.get_provider()
    run = loop.run_turn(session, request, provider,
                        on_event=emit_event, max_iters=max_iters,
                        plan_first=bool(plan))
    return asdict(run)


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
# (orch 20 / micro 65 / assist 87 / agent-support 163 all MATCH). The LEGACY agent_design
# code-gen path (above) stays in Python (agent/loop.py executes build123d code). agent_cancel
# remains as a harmless no-op for any run not tracked in this process (the C++ core owns v2 run
# lifecycles now).

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
# Python worker: the legacy build123d code-gen path (agent_design), the format-matrix probe, the
# vendor-parts registry, and the AI-ECAD surface.
_METHODS = {
    "ping": _ping,
    "run_model": _run_model,
    "agent_design": _agent_design,
    "get_prompts": _get_prompts,
}

# Format-matrix probe (doc_formats). import_file / export_file moved to the C++ core; the pure
# format helpers in engine.doc.formats stay (engine.ecad.threed imports import_path).
from engine.doc import formats as _formats  # noqa: E402

_METHODS.update(_formats.WORKER_METHODS)

# C22a orchestrator (M9) — agent_run_v2 / get_prompts_v2 run in the C++ core (libaicad); only the
# no-op agent_cancel remains in Python (the C++ core owns v2 run lifecycles now).
_METHODS.update({
    "agent_cancel": _agent_cancel,
})

# Methods that emit the structured error envelope {code, message, detail} on failure. The doc
# engine (which owned most of these) is now C++; only surviving Python surfaces register here.
_STRUCTURED_ERROR_METHODS: set[str] = set()

# ---- Supplier/vendor parts cache (registry only; vendor_part_insert runs in the C++ core) ----
from engine import vendor_parts as _vendor_parts  # noqa: E402

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
# Reaches the unified parts hub (engine_oracle/parts_hub/) over the same worker seam so the
# C++/ImGui parts browser panel (ui/imgui/src/ui/parts_panel.cpp) fetches results
# asynchronously via AsyncRpc with no bespoke transport.  Degrades gracefully when the hub
# or its backing data is unavailable (returns empty results, never a traceback).
try:
    from engine_oracle.parts_hub.worker_rpc import WORKER_METHODS as _PARTS_METHODS  # noqa: E402
    _METHODS.update(_PARTS_METHODS)
except Exception as _parts_import_err:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger("ai-cad.worker").warning(
        "engine_oracle.parts_hub.worker_rpc unavailable — part_search/get/place not registered: %s",
        _parts_import_err)
# === end Parts library ===
# === BOM / sourcing seam (T-U4) — bom_rollup / bom_price / request_quote / match_part_number ===
# Thin wrappers over engine.bom + engine.quote_providers + engine_oracle.spec_sheet_matcher,
# exposed over the worker seam so the BOM panel (ui/imgui/src/ui/bom_panel.cpp) reaches them
# via AsyncRpc with no bespoke transport.  Gracefully absent when the engine.bom module is not
# yet provisioned (e.g. CI without the ML track) — the panel degrades to an error state.
try:
    from engine_oracle.sourcing_worker_rpc import WORKER_METHODS as _SOURCING_METHODS  # noqa: E402
    _METHODS.update(_SOURCING_METHODS)
except Exception as _sourcing_import_err:  # noqa: BLE001
    import logging as _logging
    _logging.getLogger("ai-cad.worker").warning(
        "engine_oracle.sourcing_worker_rpc unavailable — bom_price/bom_rollup/request_quote"
        "/match_part_number not registered: %s", _sourcing_import_err)
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
