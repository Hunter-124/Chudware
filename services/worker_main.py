"""services/worker_main.py — the services-side stdio JSON-RPC worker (T-RF20/R5a).

This is the Python worker that survives the engine_oracle/ deletion: every ENGINE method
(doc/sketch/sim/ecad/cam/sourcing/hydraulic/piping/structural/firmware/parts-placement)
runs in the C++ core (libchudware) and is routed there by services/worker/worker_client.cpp;
what remains here are the services-side orchestration surfaces that legitimately stay
Python:

  run_model (build123d script execution — needs an interpreter by nature),
  editable prompts, the persistent Chudcode agent host and its private control lane,
  vendor-parts registry, deliverables, parts hub FTS, agent GUI seams, reveng,
  dag steering, subagent RPC, PLM vault, EM (T-SIM11 hold), multi-physics seam,
  manufacturing printer adapters.

The agent GUI seams and subagent RPC tables are REQUIRED: if either cannot import, the
worker fails at startup instead of answering every agent method "unknown method"
(which the GUI reads as "feature absent"). The other service tables are optional and
log a warning when their dependencies are missing.

Protocol: newline-delimited JSON-RPC on stdio (protocol 2 — contracts/worker_rpc.md).
The hostd worker bridge (core/hostd/worker_bridge.cpp) spawns this module for
engine-unknown methods.
"""

from __future__ import annotations

import importlib
import json
import os
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
from services.agent.request_context import bind_context, cell_path, control_delegate, in_cell


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


def _worker_activity(_params: dict, _emit_event: EmitFn) -> dict:
    from services.worker_activity import snapshot

    return snapshot()


def _normalize_face_query(params: dict, _emit_event: EmitFn) -> dict:
    """Pure shared grammar normalization; document resolution stays in the caller's engine."""
    from services.mcp.cad_common import _SID_RE, _as_face_query, _parse_inspect_target

    query = params["query"]
    if isinstance(query, str) and _SID_RE.fullmatch(query.strip()):
        return {"query": {"of": "face"}, "sid": _parse_inspect_target(query)["sid"]}
    return {"query": _as_face_query(query)}


_METHODS = {
    "ping": _ping,
    "run_model": _run_model,
    "list_editable_prompts": _list_editable_prompts,
    "worker_control_info": _worker_control_info,
    "worker_activity": _worker_activity,
    "worker_normalize_face_query": _normalize_face_query,
}

_STRUCTURED_ERROR_METHODS: set[str] = set()


def _register(
    label: str,
    import_path: str,
    attr: str,
    *,
    structured: bool = False,
    required: bool = False,
) -> None:
    """Merge one module's WORKER_METHODS-style table into the dispatch table.

    A required table that cannot import raises, failing worker startup visibly; an
    optional one logs a warning and leaves its methods unregistered.
    """
    try:
        table = getattr(importlib.import_module(import_path), attr)
    except Exception as exc:  # noqa: BLE001 — optional service tables degrade
        if required:
            raise RuntimeError(
                f"required worker table {label!r} failed to load from {import_path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        log.warning("%s unavailable — its worker methods not registered: %s", label, exc)
        return
    _METHODS.update(table)
    if structured:
        _STRUCTURED_ERROR_METHODS.update(table.keys())


# ---- Supplier/vendor parts cache (registry only; insert runs in the C++ core) ----
_register("vendor_parts", "services.parts.vendor_parts", "WORKER_METHODS", structured=True)

# ---- Deliverables ----
_register("deliverables", "services.deliverables.worker_rpc", "WORKER_METHODS")

# ---- Parts hub FTS + GUI seams ----
_register("parts", "services.parts.worker_rpc", "WORKER_METHODS")

# ---- Agent GUI seams (subscription/session/skills/triggers/role_models/bench) ----
_register("agent_gui", "services.agent.worker_rpc", "WORKER_METHODS", required=True)

# ---- Reverse engineering ----
_register("reveng", "services.reveng.worker_rpc", "REVENG_WORKER_METHODS")

# ---- DAG steering ----
_register("dag", "services.dag.worker_rpc", "WORKER_METHODS")

# ---- Subagent RPC ----
_register("subagent", "services.agent.subagent_rpc", "WORKER_METHODS", required=True)

# ---- PLM vault ----
_register("plm", "services.plm.worker_rpc", "WORKER_METHODS")

# ---- EM (T-SIM11 hold — moved, not worked) ----
_register("em", "services.sim.em.worker_rpc", "WORKER_METHODS")

# ---- Multi-physics seam ----
_register("multiphysics", "services.sim.multiphysics_rpc", "WORKER_METHODS")
_register("topology", "services.sim.topology_worker_rpc", "WORKER_METHODS")
_register("piping_fitting_cfd", "services.piping.worker_rpc", "WORKER_METHODS")

# ---- Manufacturing printer adapters (Moonraker / OctoPrint / PrusaLink) ----
# HTTP+WebSocket clients live in Python; the hostd manufacturing lane stays the sole
# owner of device claims, approval validation, durable seq and events.
_register(
    "mfg_printers",
    "services.manufacturing.printers.worker_rpc",
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


def dispatch_request(req: dict, emit_event: EmitFn) -> object:
    """Bind only the hostd envelope, never caller-provided identity parameters."""
    with bind_context(req.get("ctx")):
        method = req["method"]
        control_delegate("cancel" if method in {
            "agent_cancel", "reveng_cancel_splat"} else method)
        params = dict(req.get("params") or {})
        if in_cell() and ("control_delegate" in req or "control_delegate" in params):
            raise ValueError("forbidden_right")
        if in_cell() and method in {"vendor_part_register_local_cad", "vendor_part_scan_library"}:
            for key in ("cad_path", "folder", "artifact_path"):
                if params.get(key):
                    params[key] = str(cell_path(params[key]))
        return _METHODS[method](params, emit_event)


def _shutdown_chudcode() -> None:
    try:
        from services.agent.worker_rpc import chudcode_shutdown

        chudcode_shutdown()
    except Exception as exc:  # noqa: BLE001 — shutdown is best-effort
        log.error("Chudcode shutdown failed: %s", type(exc).__name__)


def _shutdown_bench() -> None:
    try:
        from services.agent.worker_rpc import _shutdown_bench as stop_bench

        stop_bench()
    except Exception as exc:  # noqa: BLE001 — shutdown must continue
        log.error("Bench shutdown failed: %s", type(exc).__name__)


def _reconcile_bench() -> None:
    try:
        from services.agent.worker_rpc import _read_status

        _read_status()
    except Exception as exc:  # noqa: BLE001 — worker still serves without bench
        log.error("Bench recovery failed: %s", type(exc).__name__)


def serve() -> None:
    # Hostd gives each worker one process-local role. Unflagged desktop,
    # standalone and compiled launches retain the original full lifecycle.
    lane = os.environ.get("CHUDWARE_HOSTD_WORKER_LANE", "")
    if lane not in ("", "agent", "agent-turn", "general"):
        raise ValueError("invalid hostd worker lane")
    agent_services = lane != "general"
    bench_services = lane in ("", "agent") and not in_cell()
    if in_cell():
        from services.agent.request_context import process_owner_key
        process_owner_key()
    global _CONTROL_SERVER
    if agent_services:
        from services.agent.chudcode_control import ControlServer

        control = ControlServer(_invoke_chudcode_control, paths.RUNTIME).start()
    else:
        control = None
    _CONTROL_SERVER = control
    shutdown_lock = threading.RLock()
    shutdown_complete = False

    def stop_services() -> None:
        nonlocal shutdown_complete
        with shutdown_lock:
            if shutdown_complete:
                return
            shutdown_complete = True
        try:
            if control is not None:
                control.stop()
        finally:
            if bench_services:
                _shutdown_bench()
            try:
                from services.reveng.gaussian_jobs import shutdown as shutdown_gaussian_jobs
                shutdown_gaussian_jobs()
            except Exception as exc:
                log.error("Gaussian shutdown failed: %s", type(exc).__name__)
            finally:
                try:
                    if agent_services:
                        _shutdown_chudcode()
                finally:
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
        log.info("worker ready (runtime=%s, lane=%s)", paths.RUNTIME, lane or "standalone")
        if agent_services:
            if bench_services:
                _reconcile_bench()
            _warm_prompt_cache()
            if lane != "agent-turn" and not in_cell():
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
                result = dispatch_request(req, emit_event)
                _emit({"id": rid, "ok": True, "result": result})
            except Exception as e:  # noqa: BLE001
                if str(e).strip("'") in {"not_found", "cell_refused", "forbidden_right", "credential_isolation_unsupported"}:
                    code = str(e).strip("'")
                    _emit({"id": rid, "ok": False, "error": {"code": code, "message": code}})
                    continue
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
