#!/usr/bin/env python3
"""services/worker_main.py — the services-side stdio JSON-RPC worker (T-RF20/R5a).

This is the Python worker that survives the engine_oracle/ deletion: every ENGINE method
(doc/sketch/sim/ecad/cam/sourcing/hydraulic/piping/structural/firmware/parts-placement)
runs in the C++ core (libaicad) and is routed there by services/worker/worker_client.cpp;
what remains here are the services-side orchestration surfaces that legitimately stay
Python:

  run_model (build123d script execution — needs an interpreter by nature),
  get_prompts / list_editable_prompts / agent_cancel (no-op stub),
  vendor-parts registry, deliverables, parts hub FTS, agent GUI seams, reveng,
  dag steering, subagent RPC, PLM vault, EM (T-SIM11 hold), multi-physics seam.

Protocol: newline-delimited JSON-RPC on stdio, identical to engine_oracle/worker.py
(protocol 2 — contracts/worker_rpc.md). The hostd worker bridge
(core/hostd/worker_bridge.cpp) spawns this module for engine-unknown methods; during
the transition it may still spawn ``python -m engine.worker``, which forwards here.

Imports are services.* throughout (the engine_oracle fallback is gone with the oracle).
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from typing import Callable

log = logging.getLogger("ai-cad.worker")
EmitFn = Callable[[dict], None]

from services import paths  # type: ignore  # re-homed from engine_oracle/paths.py


def _ping(_params: dict, _emit_event: EmitFn) -> dict:
    try:
        import build123d
        ver = getattr(build123d, "__version__", "?")
    except Exception:  # noqa: BLE001
        ver = None
    return {"pong": True, "build123d": ver, "protocol": 2}


def _run_model(params: dict, _emit_event: EmitFn) -> dict:
    from dataclasses import asdict

    from services.kernel_exec import run_code_multi

    code = params["code"]
    name = params.get("name", "model")
    out_dir = paths.model_dir(name)
    res = run_code_multi(code, out_dir, name)
    return asdict(res)


def _get_prompts(_params: dict, _emit_event: EmitFn) -> dict:
    """The built-in prompts (+ the active override) for the GUI's Settings editor."""
    from agent import config, loop

    return {"system_prompt": loop.SYSTEM_PROMPT,
            "plan_prompt": loop.PLAN_PROMPT,
            "override": config.load_config().get("system_prompt", "")}


def _list_editable_prompts(_params: dict, _emit_event=None):
    """Settings → AI → Prompts: YAML mission roles + core .md prompts (merged)."""
    try:
        from services.agent.roles import list_editable_prompts
        return list_editable_prompts()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# Retained for agent_cancel; normally empty (the C++ core owns v2 run lifecycles).
_RUNS: dict = {}


def _agent_cancel(params: dict, _emit_event: EmitFn) -> dict:
    """Best-effort cancel: flag the run if this process tracks it (it drives none)."""
    run_id = params["run_id"]
    control = _RUNS.get(run_id)
    if control is None:
        return {"cancelled": False, "reason": "unknown or finished run"}
    control.cancelled = True
    return {"cancelled": True}


_METHODS = {
    "ping": _ping,
    "run_model": _run_model,
    "get_prompts": _get_prompts,
    "list_editable_prompts": _list_editable_prompts,
    "agent_cancel": _agent_cancel,
}

_STRUCTURED_ERROR_METHODS: set[str] = set()


def _register(label: str, import_paths: list[str], attr: str, structured: bool = False) -> None:
    """Register a WORKER_METHODS-style table from the first importable path (dual-path)."""
    import importlib

    for path in import_paths:
        try:
            mod = importlib.import_module(path)
            table = getattr(mod, attr)
            _METHODS.update(table)
            if structured:
                _STRUCTURED_ERROR_METHODS.update(table.keys())
            return
        except Exception as exc:  # noqa: BLE001 — try the next path, else degrade
            last = exc
    log.warning("%s unavailable — its worker methods not registered: %s", label, last)


# ---- Supplier/vendor parts cache (registry only; insert runs in the C++ core) ----
_register("vendor_parts",
          ["services.parts.vendor_parts", "engine.vendor_parts"],
          "WORKER_METHODS", structured=True)

# ---- Deliverables ----
_register("deliverables", ["deliverables.worker_rpc"], "WORKER_METHODS")

# ---- Parts hub FTS + GUI seams ----
_register("parts", ["services.parts.worker_rpc"], "WORKER_METHODS")

# ---- Agent GUI seams (subscription/session/skills/triggers/role_models/bench) ----
_register("agent_gui", ["services.agent.worker_rpc"], "WORKER_METHODS")

# ---- Reverse engineering ----
_register("reveng", ["services.reveng.worker_rpc"], "REVENG_WORKER_METHODS")

# ---- DAG steering ----
_register("dag", ["services.dag.worker_rpc"], "WORKER_METHODS")

# ---- Subagent RPC ----
_register("subagent", ["services.agent.subagent_rpc"], "WORKER_METHODS")

# ---- PLM vault ----
_register("plm", ["services.plm.worker_rpc"], "WORKER_METHODS")

# ---- EM (T-SIM11 hold — moved, not worked) ----
_register("em", ["services.sim.em.worker_rpc", "engine.sim.em.worker_rpc"],
          "WORKER_METHODS")

# ---- Multi-physics seam ----
_register("multiphysics", ["services.sim.multiphysics_rpc", "engine.sim.multiphysics_rpc"],
          "WORKER_METHODS")


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
                to_env = getattr(e, "to_envelope", None)
                if callable(to_env):
                    _emit({"id": rid, "ok": False, "error": to_env()})
                else:
                    _emit({"id": rid, "ok": False, "error": {
                        "code": "internal", "message": f"{type(e).__name__}: {e}",
                        "detail": {"traceback": traceback.format_exc()}}})
            else:
                _emit({"id": rid, "ok": False,
                       "error": f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"})


if __name__ == "__main__":
    serve()
