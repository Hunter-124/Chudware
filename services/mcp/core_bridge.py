"""Thin ctypes bridge from the MCP server to the AI-CAD C++ core (``libaicad``).

This is the SAME engine the desktop app loads: the native core library
(``libaicad.dll`` on Windows, ``libaicad.so`` on Linux, ``libaicad.dylib`` on macOS)
exposes every modeling / simulation / drawing capability as a string-in/string-out JSON
RPC (``aicad_call(method, params_json, &out_json)``; see ``core/api/libaicad.cpp``). The
in-app agent drives it through the host; here we drive it from Python so an MCP client
(Claude) can do the same — write parametric feature ops, render, run FEA/CFD, export.

Loading mirrors the proven probe loaders (``tools/vocab_battery.py``,
``tools/mesh_quality_verify.py``) and the host's own resolver (``cpp_core.cpp``): locate
the library (``$AI_CAD_LIBAICAD`` explicit path → ``$AI_CAD_DIST`` dir → the platform's
known build/stage dirs), put its directory on the native dep search path (so the
transitive OCCT / Netgen / lib3mf deps resolve — on Linux the .so's ``$ORIGIN`` RUNPATH
already handles its siblings), point ``AI_CAD_PROMPTS_DIR`` at the prompts (so
plain-English geometry queries + cheatsheets behave exactly like the engine), then
``CDLL`` the library and bind the two C-ABI entry points.

Public surface:
    call(method, params) -> dict     one RPC; raises CoreError on a hard failure
    dim(value) -> dict               a parametric dimension literal {"value":v,"expr":None}
    feat(type, name, params, refs)   an add_feature op for doc_apply_ops
    dist_dir() -> str                the resolved dist/AI-CAD directory
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------- env var compatibility shim
# BILLET_* is the new canonical name for all env vars. AI_CAD_* is the legacy name.
# Read sites use _env() to prefer new names while honoring old ones (with a one-time warn).
# Write sites (gate.sh exports, test setdefault calls) are unchanged — they keep working
# via the fallback.

import warnings as _warnings
_warned_vars: set = set()


def _env(new_name: str, legacy_name: str | None = None, default: str | None = None) -> str | None:
    """Read an env var preferring the BILLET_* name, falling back to AI_CAD_* legacy name.

    Emits a DeprecationWarning once per process per legacy-name when the legacy var is
    the active source, so callers know to update their scripts.
    """
    val = os.environ.get(new_name)
    if val is not None:
        return val
    # SHOPCRAFT_* never shipped but briefly existed as the T-G10 canonical prefix;
    # accept it as a silent second fallback so any stray script keeps working.
    if new_name.startswith("BILLET_"):
        val = os.environ.get("SHOPCRAFT_" + new_name[len("BILLET_"):])
        if val is not None:
            return val
    if legacy_name:
        val = os.environ.get(legacy_name)
        if val is not None:
            if legacy_name not in _warned_vars:
                _warned_vars.add(legacy_name)
                _warnings.warn(
                    f"{legacy_name} is deprecated; use {new_name} instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )
            return val
    return default


class CoreError(RuntimeError):
    """A hard failure from the C++ core (nonzero rc / OpError envelope / generic error)."""

    def __init__(self, message: str, code: str = "", detail: Any = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------- library resolution

def _repo_root() -> Path:
    # mcp_server/core_bridge.py -> repo root is two parents up.
    return Path(__file__).resolve().parents[1]


def _lib_name() -> str:
    """Platform filename of the core library."""
    if sys.platform == "win32":
        return "libaicad.dll"
    if sys.platform == "darwin":
        return "libaicad.dylib"
    return "libaicad.so"


def dist_dir() -> Path:
    """The dist/AI-CAD directory holding the core library + its deps + staged prompts.

    Honours $AI_CAD_DIST (absolute path to the dist dir); else defaults to the repo's
    ``dist/AI-CAD`` (the Windows ``build.bat`` stage target). The directory is NOT
    required to exist until the library is first loaded; on Linux there is no ``dist``
    stage and the library is found in a build dir instead (see ``_lib_candidates``).
    """
    env = _env("BILLET_DIST", "AI_CAD_DIST")
    if env:
        return Path(env)
    return _repo_root() / "dist" / "AI-CAD"


def _lib_candidates() -> list[Path]:
    """Ordered candidate paths to the core library, mirroring the host's resolver:
    explicit ``$AI_CAD_LIBAICAD`` → the ``$AI_CAD_DIST`` / dist dir → the platform's
    known build dirs. First existing one wins.
    """
    name = _lib_name()
    out: list[Path] = []
    env_lib = _env("BILLET_LIBAICAD", "AI_CAD_LIBAICAD")
    if env_lib:
        out.append(Path(env_lib))
    out.append(dist_dir() / name)  # honours $AI_CAD_DIST; Windows stage default
    if sys.platform != "win32":
        # No dist stage off-Windows: the .so is produced in a build dir (and copied next
        # to the host exe). Its $ORIGIN RUNPATH resolves the sibling Netgen/lib3mf deps.
        root = _repo_root()
        # build-occt-linux is the FULL engine (OCCT + Netgen) the desktop app loads — it
        # carries the FEA/CFD sim RPCs (sim_run_static etc., gated behind #ifdef
        # AICAD_WITH_NETGEN in libaicad.cpp). It MUST come before the slim "build-linux"
        # beachhead, which is OCCT/Netgen-free and silently lacks every sim + geometry RPC
        # the MCP exposes (loading it makes run_fea_static report "solver unavailable" even
        # though the UI works). See _warn_if_sim_missing below.
        out.append(root / "core" / "build-occt-linux" / "bin" / name)
        # core/build-linux is the standard cmake --build target used by gate.sh and CI.
        # Added here to fix the "all-skip" incident where AI_CAD_LIBAICAD was required
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
        else "cmake --build core/build-occt-linux (then set AI_CAD_LIBAICAD or AI_CAD_DIST)"
    )
    raise CoreError(
        f"{name} not found. Tried: {', '.join(str(p) for p in tried)}. "
        f"Build + stage the C++ core ({hint})."
    )


def _prompts_default() -> Path:
    """Where the staged/source agent prompts live for $AI_CAD_PROMPTS_DIR. Prefers the
    Windows dist stage when present, else the repo source (``agent/prompts``)."""
    staged = dist_dir() / "app" / "agent" / "prompts"
    if staged.is_dir():
        return staged
    return _repo_root() / "agent" / "prompts"


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
                    "[core_bridge] WARNING: an aicad-hostd daemon is running but this "
                    "process is loading its OWN in-process engine "
                    "(AI_CAD_ENGINE_DAEMON=0). Documents will NOT be shared with the "
                    "daemon's clients. This becomes a hard refusal next release.",
                    file=sys.stderr,
                )
        except Exception:
            pass  # the guard must never block a legacy load
        lib_path = _resolve_lib()
        lib_dir = lib_path.parent
        # The library's transitive deps (OCCT, Netgen, lib3mf, ...) live alongside it; put
        # the dir on the native search path before loading.
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
        # An explicit BILLET_PROMPTS_DIR override must reach the C++ core, which
        # still reads AI_CAD_PROMPTS_DIR — propagate before the default kicks in.
        _sc_prompts = os.environ.get("BILLET_PROMPTS_DIR")
        if _sc_prompts:
            os.environ.setdefault("AI_CAD_PROMPTS_DIR", _sc_prompts)
        os.environ.setdefault("AI_CAD_PROMPTS_DIR", str(_prompts_default()))
        os.environ.setdefault("BILLET_PROMPTS_DIR", os.environ["AI_CAD_PROMPTS_DIR"])
        dll = ctypes.CDLL(str(lib_path))
        dll.aicad_call.restype = ctypes.c_int
        dll.aicad_call.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_char_p),
        ]
        dll.aicad_free.argtypes = [ctypes.c_char_p]
        _dll = dll
        _warn_if_sim_missing(dll, lib_path)
        return _dll


def _warn_if_sim_missing(dll: ctypes.CDLL, lib_path: Path) -> None:
    """The MCP exposes OCCT/Netgen-dependent tools (run_fea_static/modal, CAD geometry).
    A slim OCCT-free ``build-linux`` .so links + loads fine but silently lacks every sim
    RPC, so the FEA tools report "solver unavailable" while the desktop app (which loads
    the full ``build-occt-linux`` engine) works. Probe the FFI dispatcher once at load and
    warn LOUDLY rather than let that misconfiguration masquerade as a solver bug.
    """
    try:
        out = ctypes.c_char_p()
        rc = dll.aicad_call(b"sim_run_static", b"{}", ctypes.byref(out))
        raw = out.value.decode("utf-8", "replace") if out.value else ""
        if out.value:
            dll.aicad_free(out)
        # "unknown method" (rc != 0) => the sim RPCs aren't compiled into this .so.
        # A dispatched-but-invalid-study reply ("study must have ...") means sim IS present.
        if rc != 0 and "unknown method" in raw:
            sys.stderr.write(
                "[core_bridge] WARNING: loaded a sim-less libaicad "
                f"({lib_path}) — FEA/CFD RPCs (sim_run_static) are NOT compiled in. "
                "This is the slim OCCT/Netgen-free build; the MCP will report the FEA "
                "solver as unavailable. Stage the full build-occt-linux engine into "
                "dist/AI-CAD (or set AI_CAD_LIBAICAD to it) and restart the server.\n"
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
#   AI_CAD_ENGINE_DAEMON unset / =1 -> DEFAULT: route through the shared
#     aicad-hostd daemon (ONE engine for the GUI + every MCP process; live doc
#     sharing, no per-process copy).
#   AI_CAD_ENGINE_DAEMON=0 -> explicit opt-out: legacy in-process ctypes load
#     (kept at least one release as the safety net; removal is a named N+1 task).
# During release N a daemon TRANSPORT failure falls back to legacy with a loud
# warning rather than bricking the session; N+1 makes it a hard error.
_DAEMON_FALLBACK_WARNED = False


def _daemon_mode() -> bool:
    """Daemon mode is the default; only an explicit '0' selects legacy."""
    return os.environ.get("AI_CAD_ENGINE_DAEMON", "1") != "0"


def active_daemon_mode() -> bool:
    """True when calls are ACTUALLY going through the daemon right now: daemon
    mode is selected AND we have not fallen back to the legacy in-process engine
    after a transport failure. Gates daemon-only behavior that would be wrong on
    the fallback path (e.g. skipping the legacy .step+ handoff publisher)."""
    return _daemon_mode() and not _DAEMON_FALLBACK_WARNED


# Foreign-edit piggyback (Phase 3): the daemon injects "_foreign_edits":[...]
# into a result when another client advanced the doc since OUR last touch. We
# strip it off the engine result here (tool code never expects the key) and
# buffer it; cad_tools._dumps appends a human-readable note to the next tool
# output. Legacy mode never buffers anything, so this is purely additive.
_FOREIGN_EDITS_PENDING: list = []


def consume_foreign_edits() -> list:
    """Drain buffered foreign-edit records ({doc_id, revision, source_kind, method})."""
    out, _FOREIGN_EDITS_PENDING[:] = _FOREIGN_EDITS_PENDING[:], []
    return out


def _call_via_daemon(method: str, params: Optional[dict]) -> dict:
    from . import daemon_bridge  # local import: zero cost on the legacy path

    result = daemon_bridge.client().call(method, params)
    if isinstance(result, dict):
        fe = result.pop("_foreign_edits", None)
        if isinstance(fe, list):
            _FOREIGN_EDITS_PENDING.extend(fe)
    if isinstance(result, dict) and result.pop("_is_error", False):
        msg = result.get("message") or result.get("error") or json.dumps(result)[:400]
        raise CoreError(msg, code=str(result.get("code", "")), detail=result.get("detail"))
    return result if isinstance(result, dict) else {"result": result}


def call(method: str, params: Optional[dict] = None) -> dict:
    """Invoke one C++ core RPC. Returns the parsed JSON result dict.

    Raises CoreError on a hard failure: a nonzero return code (generic ``{"error":...}`` or
    a doc/ecad OpError envelope ``{"code","message","detail"}``). A soft, in-band failure
    that the core reports at rc 0 (e.g. a sim returning ``{"ok": false, "error": ...}``) is
    returned as-is so the caller can surface it without an exception.
    """
    global _DAEMON_FALLBACK_WARNED
    # STICKY fallback: once a transport failure demotes us to the in-process
    # engine we stay there for the life of this process. Retrying the daemon
    # per-call re-pays the full connect/spawn budget (~15 s) on EVERY engine
    # call — measured 150x slowdown (docs/PERF_AUDIT_2026-07-10.md §1) — and
    # switching back mid-session would strand doc_ids created in the in-process
    # engine. Recovery = restart the server (the warning says so).
    if _daemon_mode() and not _DAEMON_FALLBACK_WARNED:
        from .daemon_bridge import DaemonError

        try:
            return _call_via_daemon(method, params)
        except DaemonError as e:
            # Transport-level failure only (spawn/connect/EOF-after-retry) — RPC
            # errors surface above as CoreError and must NOT trigger fallback.
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
    rc = dll.aicad_call(
        method.encode("utf-8"),
        json.dumps(params or {}).encode("utf-8"),
        ctypes.byref(out),
    )
    raw = out.value.decode("utf-8") if out.value else ""
    if out.value:
        dll.aicad_free(out)
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
