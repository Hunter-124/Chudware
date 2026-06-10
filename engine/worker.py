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
    return {"pong": True, "build123d": ver}


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


_METHODS = {
    "ping": _ping,
    "run_model": _run_model,
    "agent_design": _agent_design,
    "get_prompts": _get_prompts,
}


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
            # Lead with the concise "Type: message" (callers often show only the first line)
            # then the full traceback for debugging.
            _emit({"id": rid, "ok": False,
                   "error": f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"})


if __name__ == "__main__":
    serve()
