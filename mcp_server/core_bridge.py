"""Thin ctypes bridge from the MCP server to the AI-CAD C++ core (``libaicad.dll``).

This is the SAME engine the desktop app loads: the staged ``dist/AI-CAD/libaicad.dll``
exposes every modeling / simulation / drawing capability as a string-in/string-out JSON
RPC (``aicad_call(method, params_json, &out_json)``; see ``core/api/libaicad.cpp``). The
in-app agent drives it through the host; here we drive it from Python so an MCP client
(Claude) can do the same — write parametric feature ops, render, run FEA/CFD, export.

Loading mirrors the proven probe loaders (``tools/vocab_battery.py``,
``tools/mesh_quality_verify.py``): add the dist dir to the DLL search path (so the
transitive OCCT / gmsh deps resolve), point ``AI_CAD_PROMPTS_DIR`` at the staged prompts
(so plain-English geometry queries + cheatsheets behave exactly like the engine), then
``CDLL`` the dll and bind the two C-ABI entry points.

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
import threading
from pathlib import Path
from typing import Any, Optional


class CoreError(RuntimeError):
    """A hard failure from the C++ core (nonzero rc / OpError envelope / generic error)."""

    def __init__(self, message: str, code: str = "", detail: Any = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------- dist resolution

def _repo_root() -> Path:
    # mcp_server/core_bridge.py -> repo root is two parents up.
    return Path(__file__).resolve().parents[1]


def dist_dir() -> Path:
    """The dist/AI-CAD directory holding libaicad.dll + its deps + staged prompts.

    Honours $AI_CAD_DIST (absolute path to the dist dir); else defaults to the repo's
    ``dist/AI-CAD``. The directory is NOT required to exist until the DLL is first loaded.
    """
    env = os.environ.get("AI_CAD_DIST")
    if env:
        return Path(env)
    return _repo_root() / "dist" / "AI-CAD"


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
        dist = dist_dir()
        dll_path = dist / "libaicad.dll"
        if not dll_path.exists():
            raise CoreError(
                f"libaicad.dll not found at {dll_path}. Build + stage the C++ core with "
                f"build.bat (or set AI_CAD_DIST to the dist/AI-CAD directory)."
            )
        # The dll's transitive deps (OCCT, gmsh, ...) live alongside it; put the dir on the
        # native search path before loading. add_dll_directory is the Windows-correct way.
        try:
            os.add_dll_directory(str(dist))
        except (AttributeError, OSError):
            # Non-Windows / older Python: fall back to PATH (best effort).
            os.environ["PATH"] = str(dist) + os.pathsep + os.environ.get("PATH", "")
        # Geometry queries + the agent cheatsheets resolve relative to the prompts dir; point
        # it at the staged copy so the MCP behaves identically to the engine. Don't clobber an
        # explicit override.
        os.environ.setdefault(
            "AI_CAD_PROMPTS_DIR", str(dist / "app" / "agent" / "prompts")
        )
        dll = ctypes.CDLL(str(dll_path))
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
