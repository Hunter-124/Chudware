"""Thin ctypes bridge from the MCP server to the Chudware C++ core (``libchudware``).

This is the SAME engine the desktop app loads: the native core library
(``libchudware.dll`` on Windows, ``libchudware.so`` on Linux, ``libchudware.dylib`` on macOS)
exposes every modeling / simulation / drawing capability as a string-in/string-out JSON
RPC (``chudware_call(method, params_json, &out_json)``; see ``core/api/libchudware.cpp``). The
in-app agent drives it through the host; here we drive it from Python so an MCP client
(Claude) can do the same — write parametric feature ops, render, run FEA/CFD, export.

Loading mirrors the proven probe loaders (``tools/vocab_battery.py``,
``tools/mesh_quality_verify.py``) and the host's own resolver (``cpp_core.cpp``): locate
the library (``$CHUDWARE_LIBCHUDWARE`` explicit path → ``$CHUDWARE_DIST`` dir → the platform's
known build/stage dirs), put its directory on the native dependency search path, point
``CHUDWARE_PROMPTS_DIR`` at the prompts, then ``CDLL`` the library and bind the C ABI.

Public surface:
    call(method, params) -> dict     one RPC; raises CoreError on a hard failure
    dim(value) -> dict               a parametric dimension literal {"value":v,"expr":None}
    feat(type, name, params, refs)   an add_feature op for doc_apply_ops
    dist_dir() -> str                the resolved dist/Chudware directory
"""
from __future__ import annotations

from contextvars import ContextVar
import ctypes
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional


class CoreError(RuntimeError):
    """A hard failure from the C++ core (nonzero rc / OpError envelope / generic error)."""

    def __init__(self, message: str, code: str = "", detail: Any = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------- library resolution

def _repo_root() -> Path:
    """Locate the repo root from the canonical ``services/mcp`` package path.

    Walking up to repository markers also supports the staged application layout
    without relying on a compatibility junction.
    """
    here = Path(__file__).resolve()
    for p in [here.parent, *here.parents]:
        if (p / "requirements.txt").is_file() and (
            (p / "services" / "mcp").is_dir() or (p / "core" / "api" / "libchudware.h").is_file()
        ):
            return p
    return here.parents[2]


def _lib_name() -> str:
    """Platform filename of the core library."""
    if sys.platform == "win32":
        return "libchudware.dll"
    if sys.platform == "darwin":
        return "libchudware.dylib"
    return "libchudware.so"


def dist_dir() -> Path:
    """The dist/Chudware directory holding the core library + its deps + staged prompts.

    Honours $CHUDWARE_DIST (absolute path to the dist dir); else defaults to the repo's
    ``dist/Chudware`` (the Windows ``build.bat`` stage target). The directory is NOT
    required to exist until the library is first loaded; on Linux there is no ``dist``
    stage and the library is found in a build dir instead (see ``_lib_candidates``).
    """
    env = os.environ.get("CHUDWARE_DIST")
    if env:
        return Path(env)
    return _repo_root() / "dist" / "Chudware"


def _lib_candidates() -> list[Path]:
    """Ordered candidate paths to the core library, mirroring the host's resolver:
    explicit ``$CHUDWARE_LIBCHUDWARE`` → the ``$CHUDWARE_DIST`` / dist dir → the platform's
    known build dirs. First existing one wins.
    """
    name = _lib_name()
    out: list[Path] = []
    env_lib = os.environ.get("CHUDWARE_LIBCHUDWARE")
    if env_lib:
        out.append(Path(env_lib))
    out.append(dist_dir() / name)  # honours $CHUDWARE_DIST; Windows stage default
    root = _repo_root()
    if sys.platform == "win32":
        # Full OCCT/FEA engine first, then the self-contained OCCT-off beachhead.
        out.append(root / "build" / "windows-occt" / "bin" / name)
        out.append(root / "core" / "build-occt" / "bin" / "Release" / name)
        out.append(root / "host" / "build-win" / "bin" / name)
        out.append(root / "build" / "windows-core" / "bin" / name)
    else:
        # Prefer the full OCCT/Polyhedral-FEA engine. The host copy is last because the
        # canonical core build trees carry the authoritative engine artifact.
        out.append(root / "core" / "build-occt-linux" / "bin" / name)
        # core/build-linux is the standard cmake --build target used by gate.sh and CI.
        # Added here to fix the "all-skip" incident where CHUDWARE_LIBCHUDWARE was required
        # even when the .so was already built at this well-known path (see issue #6).
        out.append(root / "core" / "build-linux" / "bin" / name)
        out.append(root / "host" / "build-linux" / "bin" / name)
    return out


def _resolve_lib() -> Path:
    """First existing candidate library path, or raise CoreError listing what was tried."""
    tried = _lib_candidates()
    for p in tried:
        if p.exists():
            return p
    name = _lib_name()
    hint = (
        "build.bat" if sys.platform == "win32"
        else "cmake --build core/build-occt-linux (then set CHUDWARE_LIBCHUDWARE or CHUDWARE_DIST)"
    )
    raise CoreError(
        f"{name} not found. Tried: {', '.join(str(p) for p in tried)}. "
        f"Build + stage the C++ core ({hint})."
    )


def _prompts_default() -> Path:
    """Where the staged/source agent prompts live for $CHUDWARE_PROMPTS_DIR. Prefers the
    Windows dist stage when present, else the repo source (``services/agent/prompts``)."""
    staged = dist_dir() / "app" / "services" / "agent" / "prompts"
    if staged.is_dir():
        return staged
    return _repo_root() / "services" / "agent" / "prompts"


# ---------------------------------------------------------------- DLL load (once)

_lock = threading.Lock()
_dll: Optional[ctypes.CDLL] = None


def _load() -> ctypes.CDLL:
    global _dll
    if _dll is not None:
        return _dll
    with _lock:
        if _dll is not None:
            return _dll
        # Mode-divergence guard: a legacy in-process engine editing docs while a
        # live hostd serves the same docs to other clients = two sources of
        # truth. Still a WARNING this release (the N+1 task upgrades it to a
        # refusal once the legacy path is scheduled for deletion).
        try:
            from . import daemon_bridge

            if not _daemon_mode() and daemon_bridge.daemon_socket_alive():
                print(
                    "[core_bridge] WARNING: an chudware-hostd daemon is running but this "
                    "process is loading its OWN in-process engine "
                    "(CHUDWARE_ENGINE_DAEMON=0). Documents will NOT be shared with the "
                    "daemon's clients. This becomes a hard refusal next release.",
                    file=sys.stderr,
                )
        except Exception:
            pass  # the guard must never block a legacy load
        lib_path = _resolve_lib()
        lib_dir = lib_path.parent
        # Optional native dependencies live alongside the library; put that directory on
        # the platform loader path before loading.
        if sys.platform == "win32":
            # add_dll_directory is the Windows-correct way (PATH is not consulted for deps
            # of a DLL loaded by absolute path on modern Windows).
            try:
                os.add_dll_directory(str(lib_dir))
            except (AttributeError, OSError):
                os.environ["PATH"] = str(lib_dir) + os.pathsep + os.environ.get("PATH", "")
        else:
            # On Linux/macOS the .so/.dylib already carries an $ORIGIN RUNPATH that resolves
            # its siblings; prepend the dir to LD_LIBRARY_PATH as a best-effort belt-and-braces.
            ld = "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
            os.environ[ld] = str(lib_dir) + os.pathsep + os.environ.get(ld, "")
        # Geometry queries + the agent cheatsheets resolve relative to the prompts dir; point
        # it at the staged/source copy so the MCP behaves identically to the engine. Don't
        # clobber an explicit override.
        os.environ.setdefault("CHUDWARE_PROMPTS_DIR", str(_prompts_default()))
        dll = ctypes.CDLL(str(lib_path))
        dll.chudware_call.restype = ctypes.c_int
        dll.chudware_call.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_char_p),
        ]
        dll.chudware_free.argtypes = [ctypes.c_char_p]
        _dll = dll
        _warn_if_sim_missing(dll, lib_path)
        return _dll


def _warn_if_sim_missing(dll: ctypes.CDLL, lib_path: Path) -> None:
    """Warn when an OCCT/Polyhedral-FEA-disabled engine was selected."""
    try:
        out = ctypes.c_char_p()
        rc = dll.chudware_call(b"sim_run_static", b"{}", ctypes.byref(out))
        raw = out.value.decode("utf-8", "replace") if out.value else ""
        if out.value:
            dll.chudware_free(out)
        # "unknown method" (rc != 0) => the sim RPCs aren't compiled into this .so.
        # A dispatched-but-invalid-study reply ("study must have ...") means sim IS present.
        if rc != 0 and "unknown method" in raw:
            sys.stderr.write(
                "[core_bridge] WARNING: loaded a sim-less libchudware "
                f"({lib_path}) — FEA RPCs are not compiled in. Rebuild with "
                "CHUDWARE_WITH_OCCT=ON and CHUDWARE_WITH_POLYMESH=ON, then restart "
                "the server.\n"
            )
            sys.stderr.flush()
    except Exception:
        # A probe failure must never block engine loading — this is advisory only.
        pass


def available() -> bool:
    """True if the DLL can be located + loaded (for graceful degradation / test skips)."""
    try:
        _load()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- the one RPC call

# Engine backend mode (shared-engine refactor, see hostd plan). Phase-8 flip:
#   CHUDWARE_ENGINE_DAEMON unset / =1 -> DEFAULT: route through the shared
#     chudware-hostd daemon (ONE engine for the GUI + every MCP process; live doc
#     sharing, no per-process copy).
#   CHUDWARE_ENGINE_DAEMON=0 -> explicit opt-out: legacy in-process ctypes load
#     (kept at least one release as the safety net; removal is a named N+1 task).
# Only initial startup failure before an engine session is established permits
# legacy fallback. Lost replies and failed reconnects must never switch engines.
_DAEMON_FALLBACK_WARNED = False


def _daemon_mode() -> bool:
    """Daemon mode is the default; only an explicit '0' selects legacy."""
    return os.environ.get("CHUDWARE_ENGINE_DAEMON", "1") != "0"


def active_daemon_mode() -> bool:
    """True when calls are ACTUALLY going through the daemon right now: daemon
    mode is selected AND we have not fallen back to the legacy in-process engine
    after an initial startup failure. Gates daemon-only behavior that would be wrong on
    the fallback path (e.g. skipping the legacy .step+ handoff publisher)."""
    return _daemon_mode() and not _DAEMON_FALLBACK_WARNED


def reset_daemon_fallback() -> None:
    """Clear the sticky legacy-engine demotion (see call()). Test seam: the
    fallback is deliberately process-permanent in production, but pytest runs
    hundreds of tests in one process, and a test that intentionally breaks a
    daemon must not strand every later test in legacy mode (suite-order bugs).
    The root conftest resets this around each test."""
    global _DAEMON_FALLBACK_WARNED
    _DAEMON_FALLBACK_WARNED = False


# Foreign-edit piggyback (Phase 3): the daemon injects "_foreign_edits":[...]
# into a result when another client advanced the doc since OUR last touch. We
# strip it off the engine result here (tool code never expects the key) and
# buffer it; cad_tools._dumps appends a human-readable note to the next tool
# output. Legacy mode never buffers anything, so this is purely additive.
# None is the standalone MCP/process slot. Host dispatch binds a (turn, agent)
# key so task names reused by another session cannot share documents or notes.
CURRENT_AGENT: ContextVar[object] = ContextVar("cad_current_agent", default=None)
_FOREIGN_EDITS_PENDING: dict[object, list] = {}
_FOREIGN_EDITS_LOCK = threading.Lock()
_BUSY_PREVIEW_BUDGET_S = 30.0


def consume_foreign_edits() -> list:
    """Drain only the calling agent's buffered foreign-edit records."""
    with _FOREIGN_EDITS_LOCK:
        return _FOREIGN_EDITS_PENDING.pop(CURRENT_AGENT.get(), [])


def _call_via_daemon(method: str, params: Optional[dict]) -> dict:
    from . import daemon_bridge  # local import: zero cost on the legacy path

    connection = daemon_bridge.client()
    original = dict(params or {})
    # Implicit agent batches are serialized and geometry-validated under DocLock.
    # Disable hostd's last-seen CAS injection; explicit CAS remains opt-in.
    if method == "doc_apply_ops" and "base_revision" not in original:
        original["base_revision"] = -1
    busy_deadline = None
    while True:
        result = connection.call(method, original)
        if isinstance(result, dict):
            fe = result.pop("_foreign_edits", None)
            if isinstance(fe, list) and fe:
                with _FOREIGN_EDITS_LOCK:
                    _FOREIGN_EDITS_PENDING.setdefault(CURRENT_AGENT.get(), []).extend(fe)
        if not (isinstance(result, dict) and result.pop("_is_error", False)):
            return result if isinstance(result, dict) else {"result": result}
        code = str(result.get("code", ""))
        if code == "busy_preview":
            # hostd emits this only before dispatch, for mutators/edit_hold.
            # Unlike a transport failure, the operation cannot have applied.
            now = time.monotonic()
            if busy_deadline is None:
                busy_deadline = now + _BUSY_PREVIEW_BUDGET_S
            left = busy_deadline - now
            if left <= 0:
                doc = original.get("doc_id") or "this document"
                raise CoreError(
                    f"The user is editing {doc} in the app right now; "
                    "work on something else and retry.",
                    code=code, detail=result,
                )
            retry_ms = result.get("retry_ms", (result.get("detail") or {}).get("retry_ms", 500))
            time.sleep(min(max(float(retry_ms) / 1000.0, 0.001), left))
            continue
        msg = result.get("message") or result.get("error") or json.dumps(result)[:400]
        raise CoreError(msg, code=code, detail=result.get("detail"))


def revert_own_batch(doc_id, checkpoint: dict | None, applied_revision) -> bool:
    """Revert only our sole batch since the snapshot, with a live revision guard."""
    if not isinstance(checkpoint, dict):
        return False
    checkpoint_revision = checkpoint.get("revision")
    if (type(checkpoint_revision) is not int
            or type(applied_revision) is not int
            or applied_revision != checkpoint_revision + 1
            or not checkpoint.get("checkpoint")):
        return False
    try:
        call("doc_revert", {"doc_id": doc_id, "checkpoint": checkpoint["checkpoint"],
                            "expect_revision": applied_revision})
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == "engine_connection_lost":
            raise
        return False
    return True


def call(method: str, params: Optional[dict] = None) -> dict:
    """Invoke one C++ core RPC. Returns the parsed JSON result dict.

    Raises CoreError on a hard failure: a nonzero return code (generic ``{"error":...}`` or
    a doc/ecad OpError envelope ``{"code","message","detail"}``). A soft, in-band failure
    that the core reports at rc 0 (e.g. a sim returning ``{"ok": false, "error": ...}``) is
    returned as-is so the caller can surface it without an exception.
    """
    global _DAEMON_FALLBACK_WARNED
    # STICKY fallback: once an initial startup failure demotes us to the in-process
    # engine we stay there for the life of this process. Retrying the daemon
    # per-call re-pays the full connect/spawn budget (~15 s) on EVERY engine
    # call — measured 150x slowdown (docs/review/PERF_AUDIT_2026-07-11.md §1) — and
    # switching back mid-session would strand doc_ids created in the in-process
    # engine. Recovery = restart the server (the warning says so).
    if _daemon_mode() and not _DAEMON_FALLBACK_WARNED:
        from .daemon_bridge import DaemonDraining, DaemonError, DaemonSessionLost

        try:
            return _call_via_daemon(method, params)
        except DaemonSessionLost as e:
            raise CoreError(str(e), code=e.code, detail=e.detail) from e
        except DaemonDraining as e:
            # The shared daemon runs and refuses new clients while it drains. A private
            # in-process engine would not share its documents, so never fall back here.
            raise CoreError(str(e), code=e.code, detail=e.detail) from e
        except DaemonError as e:
            # Initial startup failure only: no operation reached this engine.
            # Established-session loss and native RPC errors never trigger fallback.
            _DAEMON_FALLBACK_WARNED = True
            print(
                f"[core_bridge] WARNING: hostd unavailable ({e}); falling back to "
                "the legacy in-process engine for the REST OF THIS SESSION. Doc "
                "sharing with the GUI is OFF; restart this server to retry hostd.",
                file=sys.stderr,
            )
    dll = _load()
    out = ctypes.c_char_p()
    # NOTE: native stdout (OCCT statistics etc.) is kept off the MCP JSON-RPC channel by a
    # PROCESS-WIDE redirect installed once at server startup (see server.py), NOT by toggling
    # fd 1 here. A per-call toggle races the transport's response writer (FastMCP runs sync
    # tools off-thread) and silently drops replies -> "request timed out".
    rc = dll.chudware_call(
        method.encode("utf-8"),
        json.dumps(params or {}).encode("utf-8"),
        ctypes.byref(out),
    )
    raw = out.value.decode("utf-8") if out.value else ""
    if out.value:
        dll.chudware_free(out)
    try:
        result = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        if rc != 0:
            raise CoreError(f"{method}: non-JSON error reply: {raw[:400]}")
        return {"_raw": raw}
    if rc != 0:
        if isinstance(result, dict):
            msg = result.get("message") or result.get("error") or json.dumps(result)[:400]
            raise CoreError(msg, code=str(result.get("code", "")), detail=result.get("detail"))
        raise CoreError(f"{method}: rc={rc} {raw[:400]}")
    return result if isinstance(result, dict) else {"result": result}


# ---------------------------------------------------------------- op builders

def dim(value: float) -> dict:
    """A parametric dimension literal as the feature schema expects: {"value": v, "expr": None}."""
    return {"value": value, "expr": None}


def feat(ftype: str, name: str, params: Optional[dict] = None,
         refs: Optional[dict] = None, **extra: Any) -> dict:
    """Build an ``add_feature`` op for doc_apply_ops.

    ``params`` are the feature's parameters (dimensions via ``dim()``); ``refs`` carry the
    plain-English geometry queries (e.g. ``{"face": {"of": "face", "side": "top"}}``).
    """
    f = {"type": ftype, "name": name, "params": params or {}, "refs": refs or {}}
    f.update(extra)
    return {"op": "add_feature", "feature": f}
