"""Python client for the OMP (oh-my-pi) bridge — the newline-delimited JSON-RPC
sidecar at ``vendor/omp/bridge/src/bridge.ts`` that reuses OMP's provider/memory/
compaction subsystems verbatim (see ``OMP_INTEGRATION_PLAN.md`` §RPC CONTRACT).

Protocol (stdin/stdout of a ``bun run <repo>/vendor/omp/bridge/src/bridge.ts``
child): one JSON object per line each direction —
``-> {"id": <int>, "method": <str>, "params": {...}}``
``<- {"id": <int>, "ok": true, "result": {...}}`` or
``<- {"id": <int>, "ok": false, "error": {"message": <str>, "status"?: <int>}}``

Spawn/path-resolution + respawn-on-crash semantics are structurally mirrored on
``core/agent/omp_bridge_client.{h,cpp}`` (the C++ client of the same bridge):
same env overrides (``AI_CAD_BUN``, ``AI_CAD_OMP_BRIDGE``), same bun discovery
order, same "child death -> reap + respawn once, then fail" retry rule. This
module is the *Python* seam used by ``project_memory.py`` / ``compaction.py``
(services run in-process in Python; the C++ core has its own client for
``providers.cpp``).
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any


class BridgeUnavailable(RuntimeError):
    """The OMP bridge could not service a call.

    Raised for every failure mode of :meth:`OmpBridgeClient.call`: bun / the
    bridge script can't be found, the child process couldn't be spawned or
    died mid-call, the call timed out, or the bridge itself replied
    ``ok: false``. Callers that want a hybrid OMP-backed path with a
    pure-Python fallback (``project_memory``, ``compaction``) catch this one
    exception and fall back.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _ChildDead(Exception):
    """Internal signal: the bridge process died (write failed / stdout EOF).

    Distinguishes "respawn and retry once" from a live-but-slow call
    (timeout) or a live bridge that replied with a structured RPC error —
    neither of the latter two should trigger a respawn (mirrors
    ``OmpBridgeClient::call`` in the C++ client).
    """


def _env_or(key: str, default: str) -> str:
    val = os.environ.get(key)
    return val if val else default


def _resolve_bun_exe() -> str:
    """bun discovery order: ``AI_CAD_BUN`` override, common install locations,
    else the bare ``"bun"`` name (resolved against ``$PATH`` by the OS when we
    spawn it). Mirrors ``resolveBunExe()`` in ``omp_bridge_client.cpp``.
    """
    env = os.environ.get("AI_CAD_BUN")
    if env:
        return env
    home = os.environ.get("HOME") or ""
    if home:
        candidate = os.path.join(home, ".bun", "bin", "bun")
        if os.path.isfile(candidate):
            return candidate
    for candidate in ("/usr/local/bin/bun", "/usr/bin/bun"):
        if os.path.isfile(candidate):
            return candidate
    return "bun"


def _bridge_marker(root: str | os.PathLike) -> Path:
    return Path(root) / "vendor" / "omp" / "bridge" / "src" / "bridge.ts"


def _resolve_repo_root() -> str:
    """Repo root resolution: ``AI_CAD_REPO`` override (validated), else walk up
    from this file looking for the ``vendor/omp/bridge/src/bridge.ts`` marker
    (this file always lives at ``<repo>/services/agent/omp_bridge.py``, so the
    walk succeeds from any cwd/venv), else ``$HOME/Repositories/CAD`` as a last
    resort. Mirrors ``resolveRepoRoot()`` in ``omp_bridge_client.cpp``.
    """
    env = os.environ.get("AI_CAD_REPO")
    if env and _bridge_marker(env).is_file():
        return env
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parent.parents):
        if _bridge_marker(parent).is_file():
            return str(parent)
    home = os.environ.get("HOME")
    return f"{home}/Repositories/CAD" if home else "."


def _resolve_bridge_script() -> str:
    return _env_or("AI_CAD_OMP_BRIDGE", str(_bridge_marker(_resolve_repo_root())))


class OmpBridgeClient:
    """Owns one ``bun run <repo>/vendor/omp/bridge/src/bridge.ts`` child process
    and speaks the id-correlated JSON-RPC protocol described at module level.

    Not meant to be instantiated directly by callers — use :func:`call` (the
    process-wide singleton) unless a test needs an isolated instance.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._out_q: Queue[str | None] = Queue()
        self._next_id = 1
        self._bun_exe: str | None = None
        self._bridge_script: str | None = None
        self._paths_resolved = False

    def _resolve_paths(self) -> None:
        if self._paths_resolved:
            return
        self._bun_exe = _resolve_bun_exe()
        self._bridge_script = _resolve_bridge_script()
        if not os.path.isfile(self._bridge_script):
            raise BridgeUnavailable(
                f"OMP bridge script not found: {self._bridge_script} (set "
                "AI_CAD_OMP_BRIDGE to the script path, or AI_CAD_REPO to a "
                "checkout containing vendor/omp/)"
            )
        self._paths_resolved = True

    def _read_loop(self, proc: "subprocess.Popen[str]", out_q: "Queue[str | None]") -> None:
        stdout = proc.stdout
        try:
            if stdout is not None:
                for line in stdout:
                    out_q.put(line)
        except (OSError, ValueError):
            pass
        finally:
            out_q.put(None)  # EOF / dead-child sentinel

    def _spawn(self) -> None:
        # Caller holds self._lock; self._proc is None or already reaped.
        self._resolve_paths()
        assert self._bun_exe is not None and self._bridge_script is not None
        try:
            proc = subprocess.Popen(
                [self._bun_exe, "run", self._bridge_script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,  # bridge.ts logs [omp-bridge] lines to stderr; leave inherited
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise BridgeUnavailable(
                f"failed to spawn OMP bridge ({self._bun_exe} run {self._bridge_script}): {exc}"
            ) from exc
        out_q: Queue[str | None] = Queue()
        self._proc = proc
        self._out_q = out_q
        threading.Thread(target=self._read_loop, args=(proc, out_q), daemon=True).start()

    def _stop_locked(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001 — best-effort teardown, never raise from stop()
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _call_once(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self._proc is None or self._proc.poll() is not None:
            self._stop_locked()
            self._spawn()
        assert self._proc is not None and self._proc.stdin is not None

        req_id = self._next_id
        self._next_id += 1
        line = json.dumps({"id": req_id, "method": method, "params": params}) + "\n"
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise _ChildDead(f"write failed: {exc}") from exc

        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"OMP bridge timed out waiting for {method!r} ({timeout}s)")
            try:
                raw = self._out_q.get(timeout=remaining)
            except Empty:
                raise TimeoutError(f"OMP bridge timed out waiting for {method!r} ({timeout}s)") from None
            if raw is None:
                raise _ChildDead("bridge process closed stdout")
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue  # non-JSON noise should not happen (stdout is response-only)
            if not isinstance(msg, dict) or msg.get("id") != req_id:
                continue

            if msg.get("ok"):
                result = msg.get("result")
                return result if isinstance(result, dict) else {"value": result}

            error = msg.get("error")
            if isinstance(error, dict):
                raise BridgeUnavailable(
                    str(error.get("message") or "OMP bridge RPC error"),
                    status=error.get("status") if isinstance(error.get("status"), int) else None,
                )
            raise BridgeUnavailable(str(error) if error else "unknown OMP bridge RPC error")

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 120) -> dict[str, Any]:
        """Id-correlated call to the bridge. Returns the ``result`` object on
        success (non-object results are wrapped as ``{"value": ...}``).

        Raises :class:`BridgeUnavailable` for every failure mode (bun/script
        missing, spawn failure, child death, timeout, or an ``ok: false``
        reply). A dead child triggers exactly one respawn-and-retry; a live
        but slow call (timeout) or a structured RPC error does not respawn.
        """
        with self._lock:
            body = params or {}
            last_exc: Exception | None = None
            for attempt in range(2):
                try:
                    return self._call_once(method, body, timeout)
                except _ChildDead as exc:
                    last_exc = exc
                    self._stop_locked()
                    if attempt == 1:
                        break
                    continue  # spawn() runs at the top of the next _call_once
                except TimeoutError as exc:
                    raise BridgeUnavailable(str(exc)) from exc
            raise BridgeUnavailable(f"OMP bridge process died: {last_exc}")

    def stop(self) -> None:
        """Kill the child process, if running (tests / shutdown)."""
        with self._lock:
            self._stop_locked()


_singleton_lock = threading.Lock()
_singleton: OmpBridgeClient | None = None


def get_client() -> OmpBridgeClient:
    """Process-wide singleton :class:`OmpBridgeClient`."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = OmpBridgeClient()
        return _singleton


def call(method: str, params: dict[str, Any] | None = None, timeout: float = 120) -> dict[str, Any]:
    """Convenience wrapper: ``get_client().call(...)``. See
    :meth:`OmpBridgeClient.call`.
    """
    return get_client().call(method, params, timeout=timeout)


def ping(timeout: float = 10) -> dict[str, Any]:
    """Cheap liveness probe (bridge method ``ping`` -> ``{pong, bun}``)."""
    return call("ping", {}, timeout=timeout)


def shutdown() -> None:
    """Stop and drop the singleton client (tests)."""
    global _singleton
    with _singleton_lock:
        client, _singleton = _singleton, None
    if client is not None:
        client.stop()
