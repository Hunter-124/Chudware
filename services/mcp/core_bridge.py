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
    env = os.environ.get("AI_CAD_DIST")
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
    env_lib = os.environ.get("AI_CAD_LIBAICAD")
    if env_lib:
        out.append(Path(env_lib))
    out.append(dist_dir() / name)  # honours $AI_CAD_DIST; Windows stage default
    if sys.platform != "win32":
        # No dist stage off-Windows: the .so is produced in a build dir (and copied next
        # to the host exe). Its $ORIGIN RUNPATH resolves the sibling Netgen/lib3mf deps.
        root = _repo_root()
        # core/build-linux is the standard cmake --build target used by gate.sh and CI.
        # Added here to fix the "all-skip" incident where AI_CAD_LIBAICAD was required
        # even when the .so was already built at this well-known path (see issue #6).
        out.append(root / "core" / "build-linux" / "bin" / name)
        out.append(root / "core" / "build-occt-linux" / "bin" / name)
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
        os.environ.setdefault("AI_CAD_PROMPTS_DIR", str(_prompts_default()))
        dll = ctypes.CDLL(str(lib_path))
        dll.aicad_call.restype = ctypes.c_int
        dll.aicad_call.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_char_p),
        ]
        dll.aicad_free.argtypes = [ctypes.c_char_p]
        _dll = dll
        return _dll


def available() -> bool:
    """True if the DLL can be located + loaded (for graceful degradation / test skips)."""
    try:
        _load()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- the one RPC call

def call(method: str, params: Optional[dict] = None) -> dict:
    """Invoke one C++ core RPC. Returns the parsed JSON result dict.

    Raises CoreError on a hard failure: a nonzero return code (generic ``{"error":...}`` or
    a doc/ecad OpError envelope ``{"code","message","detail"}``). A soft, in-band failure
    that the core reports at rc 0 (e.g. a sim returning ``{"ok": false, "error": ...}``) is
    returned as-is so the caller can surface it without an exception.
    """
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
