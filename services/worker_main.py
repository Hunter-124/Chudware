"""services/worker_main.py — the services-side stdio JSON-RPC worker (T-RF20/R5a).

This is the Python worker that survives the engine_oracle/ deletion: every ENGINE method
(doc/sketch/sim/ecad/cam/sourcing/hydraulic/piping/structural/firmware/parts-placement)
runs in the C++ core (libchudware) and is routed there by services/worker/worker_client.cpp;
what remains here are the services-side orchestration surfaces that legitimately stay
Python:

  run_model (build123d script execution — needs an interpreter by nature),
  editable prompts, the persistent Chudcode agent host and its private control lane,
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
import signal
import sys
import threading
import traceback
from collections.abc import Callable

log = logging.getLogger("chudware.worker")
EmitFn = Callable[[dict], None]

_STDOUT_LOCK = threading.Lock()
_CONTROL_SERVER = None

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


def _list_editable_prompts(_params: dict, _emit_event=None):
    """Settings → AI → Prompts: YAML mission roles + core .md prompts (merged)."""
    try:
        from services.agent.roles import list_editable_prompts

        return list_editable_prompts()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _worker_control_info(_params: dict, _emit_event: EmitFn) -> dict:
    server = _CONTROL_SERVER
    if server is None:
        raise RuntimeError("worker control server is not running")
    return server.info()


_METHODS = {
    "ping": _ping,
    "run_model": _run_model,
    "list_editable_prompts": _list_editable_prompts,
    "worker_control_info": _worker_control_info,
}

_STRUCTURED_ERROR_METHODS: set[str] = set()


def _register(
    label: str, import_paths: list[str], attr: str, structured: bool = False
) -> None:
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
_register(
    "vendor_parts", ["services.parts.vendor_parts"], "WORKER_METHODS", structured=True
)

# ---- Deliverables ----
_register("deliverables", ["services.deliverables.worker_rpc"], "WORKER_METHODS")

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
_register("em", ["services.sim.em.worker_rpc"], "WORKER_METHODS")

# ---- Multi-physics seam ----
_register("multiphysics", ["services.sim.multiphysics_rpc"], "WORKER_METHODS")

# ---- Manufacturing printer adapters (Moonraker / OctoPrint / PrusaLink) ----
# HTTP+WebSocket clients live in Python; the hostd manufacturing lane stays the sole
# owner of device claims, approval validation, durable seq and events.
_register(
    "mfg_printers",
    ["services.manufacturing.printers.worker_rpc"],
    "WORKER_METHODS",
    structured=True,
)


def _emit(obj: dict) -> None:
    # Backstop: a tool result carrying a non-JSON object (e.g. fastmcp Image)
    # must never destroy an otherwise-completed turn at the RPC boundary.
    # Producers normalise first (agent_turn._json_safe_result); this catches
    # anything that slips through by stringifying the offender.
    try:
        payload = json.dumps(obj)
    except TypeError:
        payload = json.dumps(obj, default=str)
    with _STDOUT_LOCK:
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()


def _warm_prompt_cache() -> None:
    """Pre-compose the system prompt on a background thread at worker start.

    Composition costs ~1.9 s in a cold process (dominated by importing the tool
    registry to render every allowed tool's schema), and that cost used to land
    on the user's FIRST message in a new session. Doing it here moves it onto
    worker startup, where nobody is waiting. Best-effort by construction: a
    failure here must never stop the worker from serving.
    """
    try:
        from services.agent.prompt_cache import warm

        warm(background=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("prompt cache warm skipped: %s", exc)


def _warm_chudcode_service() -> None:
    """Spawn the shared Chudcode service process on a background thread.

    Every transient command (provider list, auth state, model catalog, memory)
    runs on one long-lived `chud --mode rpc` process; spawning it costs ~1.1 s,
    which otherwise lands on the GUI's first provider query after connect.
    """
    def run() -> None:
        try:
            from services.agent.chudcode_host import get_default_chudcode_host

            get_default_chudcode_host().warm_service()
        except Exception as exc:  # noqa: BLE001
            log.debug("chudcode service warm skipped: %s", exc)

    threading.Thread(target=run, name="chudcode-service-warm", daemon=True).start()


def _drain_box_mirror_on_exit() -> None:
    """Flush whatever the background Box drain has not finished, briefly.

    The turn path only STARTS a drain (see cad_common.save_touched_docs), so this is
    where an in-flight mirror gets a last bounded window. The spool is durable: work
    that does not fit stays queued for the next drain.
    """
    try:
        from services.plm import box_mirror

        if box_mirror.is_enabled() and box_mirror.status()["pending"]:
            box_mirror.drain(block=True, budget_s=10.0)
    except Exception as exc:  # noqa: BLE001 — shutdown is best-effort
        log.debug("box mirror exit drain skipped: %s", exc)


def _invoke_chudcode_control(action: str, params: dict) -> dict:
    from services.agent.worker_rpc import chudcode_control

    return chudcode_control(action, params)


def _shutdown_chudcode() -> None:
    try:
        from services.agent.worker_rpc import chudcode_shutdown

        chudcode_shutdown()
    except Exception as exc:  # noqa: BLE001 — shutdown is best-effort
        log.error("Chudcode shutdown failed: %s", type(exc).__name__)


def serve() -> None:
    from services.agent.chudcode_control import ControlServer

    global _CONTROL_SERVER
    control = ControlServer(_invoke_chudcode_control, paths.RUNTIME).start()
    _CONTROL_SERVER = control
    shutdown_lock = threading.RLock()
    shutdown_complete = False

    def stop_services() -> None:
        nonlocal shutdown_complete
        with shutdown_lock:
            if shutdown_complete:
                return
            shutdown_complete = True
        control.stop()
        _shutdown_chudcode()
        _drain_box_mirror_on_exit()

    old_handlers: dict[int, object] = {}
    if threading.current_thread() is threading.main_thread():
        handled_signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGHUP"):
            handled_signals.append(signal.SIGHUP)

        def on_signal(signum, _frame) -> None:
            stop_services()
            raise SystemExit(128 + signum)

        for signum in handled_signals:
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, on_signal)

    try:
        log.info("worker ready (runtime=%s)", paths.RUNTIME)
        _warm_prompt_cache()
        _warm_chudcode_service()
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

            # Stdio dispatch deliberately remains serialized. The private control
            # listener is the only concurrent path while an agent turn is blocked.
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
                        _emit(
                            {
                                "id": rid,
                                "ok": False,
                                "error": {
                                    "code": "internal",
                                    "message": f"{type(e).__name__}: {e}",
                                    "detail": {"traceback": traceback.format_exc()},
                                },
                            }
                        )
                else:
                    _emit(
                        {
                            "id": rid,
                            "ok": False,
                            "error": f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}",
                        }
                    )
    finally:
        _CONTROL_SERVER = None
        stop_services()
        for signum, old_handler in old_handlers.items():
            signal.signal(signum, old_handler)


if __name__ == "__main__":
    serve()
