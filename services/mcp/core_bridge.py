"""Bridge from the MCP tool layer to the Chudware C++ engine (``libchudware``).

This is the SAME engine the desktop app runs. Every modeling / simulation / drawing
capability is one string-in/string-out JSON RPC (``chudware_call(method, params_json,
&out_json)``; see ``core/api/libchudware.cpp``). By default the RPC goes through the shared
engine daemon (``daemon_bridge`` → ``chudware-hostd``): ONE engine for the GUI and every
MCP process, so documents are shared live. ``CHUDWARE_ENGINE_DAEMON=0`` explicitly selects
an in-process ``ctypes`` load of the same library instead.

``call()`` is more than a forwarder. Around the RPC it attaches a document read-write
before an authored change, lists the document before/after to detect that change, and keeps
the touched-document / writer-lease / internal-document bookkeeping the tool layer and the
end-of-run save sweep read. ``_dispatch_call`` is the bare transport (daemon, else in-process).

In-process loading mirrors the host's own resolver (``cpp_core.cpp``): locate
the library (``$CHUDWARE_LIBCHUDWARE`` explicit path → ``$CHUDWARE_DIST`` dir → the platform's
known build/stage dirs), put its directory on the native dependency search path, point
``CHUDWARE_PROMPTS_DIR`` at the prompts, then ``CDLL`` the library and bind the C ABI.

Public surface:
    call(method, params) -> dict     one RPC; raises CoreError on a hard failure
    dim(value) -> dict               a parametric dimension literal {"value":v,"expr":None}
    feat(type, name, params, refs)   an add_feature op for doc_apply_ops
    dist_dir() -> str                the resolved dist/Chudware directory
    private_engine_active() -> bool  hostd was unreachable and a standalone client fell back
                                     to a private in-process engine (replies say so)
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import atexit
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
_native_lifecycle = threading.Condition()
_native_calls = 0
_native_closing = False


def _drain_in_process_tasks() -> None:
    # Never load an unused engine at exit. This hook is registered at import,
    # before the server's later terminal-save hooks, so those saves run first.
    global _native_closing
    with _native_lifecycle:
        _native_closing = True
        while _native_calls:
            _native_lifecycle.wait()
    # _load may be running on the warm-up thread outside an RPC.
    with _lock:
        dll = _dll
    if dll is None:
        return
    try:
        drain = dll.chudware_drain_background_tasks
    except AttributeError:
        return  # older engines do not provide the lifecycle export
    drain.argtypes = []
    drain.restype = ctypes.c_int
    if drain() != 0:
        print("[core_bridge] engine background task drain failed", file=sys.stderr)


atexit.register(_drain_in_process_tasks)


def _load() -> ctypes.CDLL:
    global _dll
    if _native_closing:
        raise CoreError("in-process engine is shutting down")
    if _dll is not None:
        return _dll
    with _lock:
        if _native_closing:
            raise CoreError("in-process engine is shutting down")
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


@contextmanager
def _in_process_call():
    global _native_calls
    with _native_lifecycle:
        if _native_closing:
            raise CoreError("in-process engine is shutting down")
        _native_calls += 1
    try:
        yield _load()
    finally:
        with _native_lifecycle:
            _native_calls -= 1
            if not _native_calls:
                _native_lifecycle.notify_all()


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
# A direct, non-tool client (no agent turn, no hostd cell) whose first connection fails
# outright may fall back to a private in-process engine; the demotion is sticky and every
# reply then carries ``engine: "private"`` (see private_engine_notice). Agent tool calls and
# cells never fall back: they get a CoreError. Cancellation, the spawn cooldown, a lost
# reply and a failed reconnect never switch engines either.
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


def private_engine_active() -> bool:
    """True when hostd was unreachable and this process now serves requests from a private
    in-process engine. An explicit ``CHUDWARE_ENGINE_DAEMON=0`` is the user's choice, not
    a degradation, and does not count."""
    return _daemon_mode() and _DAEMON_FALLBACK_WARNED


def private_engine_notice() -> Optional[dict]:
    """Reply fields every tool reply must carry while :func:`private_engine_active`.

    None on the healthy path. The shared reply writers (``_envelopes.dumps``) stamp these
    onto dict payloads so the caller can see its edits are invisible to the app and to
    other agents.
    """
    if not private_engine_active():
        return None
    return {
        "engine": "private",
        "engine_warning": (
            "chudware-hostd was unreachable, so this reply came from a PRIVATE in-process "
            "engine: its documents are not shared with the Chudware app or other agents. "
            "Restart this MCP server once hostd is available."
        ),
    }

_AGENT_TOOL_CALL: ContextVar[bool] = ContextVar("cad_agent_tool_call", default=False)


@contextmanager
def agent_tool_scope():
    """Mark an MCP tool dispatch so a failed daemon connection cannot demote it."""
    token = _AGENT_TOOL_CALL.set(True)
    try:
        yield
    finally:
        _AGENT_TOOL_CALL.reset(token)


def _agent_call() -> bool:
    """True for MCP tool dispatch, an agent turn, or a hostd cell."""
    if _AGENT_TOOL_CALL.get() or CURRENT_AGENT.get() is not None:
        return True
    from .daemon_bridge import agent_turn_active
    return agent_turn_active()


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
_DOC_RESOLUTION_ROWS: ContextVar[dict] = ContextVar("cad_resolution_rows", default={})


def remember_resolution_rows(rows: list) -> None:
    """Reuse fresh non-busy rows from the current document resolution once."""
    _DOC_RESOLUTION_ROWS.set({str(row["doc_id"]): row for row in rows
                              if row.get("doc_id") and not row.get("busy")})


def _take_resolution_row(doc_id: str):
    rows = _DOC_RESOLUTION_ROWS.get()
    _DOC_RESOLUTION_ROWS.set({})
    return rows.get(str(doc_id))


def consume_foreign_edits() -> list:
    """Drain only the calling agent's buffered foreign-edit records."""
    with _FOREIGN_EDITS_LOCK:
        return _FOREIGN_EDITS_PENDING.pop(CURRENT_AGENT.get(), [])


def release_run_state(run_id, open_ids=None) -> None:
    """Release completed run notifications and stale internal-document marks."""
    from services.agent.request_context import owner_key
    with _FOREIGN_EDITS_LOCK:
        for key in list(_FOREIGN_EDITS_PENDING):
            if isinstance(key, tuple) and key and key[0] == run_id:
                _FOREIGN_EDITS_PENDING.pop(key, None)
    if open_ids is not None:
        owner = owner_key()
        with _INTERNAL_DOCS_LOCK:
            _INTERNAL_DOCS.difference_update(
                entry for entry in tuple(_INTERNAL_DOCS)
                if entry[0] == owner and entry[1] not in open_ids)


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
        if code == "read_only":
            msg = ("The document is attached read-only. Write access is requested "
                   "automatically before editing; retry the operation, and if it "
                   "still fails check your document permissions.")
        detail = result.get("detail")
        if code == "path_bound_to_other_doc":
            detail = {**(detail if isinstance(detail, dict) else {}),
                      "other_doc_id": result.get("other_doc_id")}
        raise CoreError(msg, code=code, detail=detail)


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


def _hostd_unavailable(cause: str) -> CoreError:
    """The error an agent tool call (or hostd cell) gets instead of a private engine."""
    return CoreError(
        f"The shared engine daemon (chudware-hostd) is unavailable: {cause}. An agent tool "
        "call never falls back to a private in-process engine, because its documents would "
        "be invisible to the Chudware app and to other agents. Start or restart Chudware "
        "and retry.",
        code="hostd_unavailable", detail={"cause": cause},
    )


def _dispatch_call(method: str, params: Optional[dict] = None) -> dict:
    """Invoke one C++ core RPC. Returns the parsed JSON result dict.

    Raises CoreError on a hard failure: a nonzero return code (generic ``{"error":...}`` or
    a doc/ecad OpError envelope ``{"code","message","detail"}``). A soft, in-band failure
    that the core reports at rc 0 (e.g. a sim returning ``{"ok": false, "error": ...}``) is
    returned as-is so the caller can surface it without an exception.
    """
    global _DAEMON_FALLBACK_WARNED
    # STICKY fallback (standalone clients only): once an initial startup failure demotes a
    # standalone MCP process to the in-process engine it stays there for the life of the
    # process. Retrying the daemon per-call re-pays the full connect/spawn budget (~15 s)
    # on EVERY engine call — measured 150x slowdown (2026-07-11 perf audit) — and switching
    # back mid-session would strand doc_ids created in the in-process engine. Recovery =
    # restart the server (the warning says so). Replies carry engine:"private" meanwhile.
    if _daemon_mode():
        if not _DAEMON_FALLBACK_WARNED:
            from .daemon_bridge import (
                DaemonCancelled, DaemonDraining, DaemonError, DaemonSessionLost,
                DaemonSpawnBlocked,
            )

            try:
                return _call_via_daemon(method, params)
            except (DaemonSessionLost, DaemonDraining) as e:
                # A lost session never switches engines, and a draining daemon refuses new
                # clients while it runs: a private engine would not share its documents.
                raise CoreError(str(e), code=e.code, detail=e.detail) from e
            except DaemonCancelled as e:
                # A Stop says nothing about whether hostd is up.
                raise CoreError(str(e), code="cancelled") from e
            except DaemonSpawnBlocked as e:
                # Not fresh evidence: an earlier spawn failed and its cooldown still runs.
                raise _hostd_unavailable(str(e)) from e
            except DaemonError as e:
                # Initial startup failure only: no operation reached this engine.
                # Established-session loss and native RPC errors never trigger fallback,
                # and an agent turn or hostd cell is never served by a private engine.
                if _agent_call():
                    raise _hostd_unavailable(str(e)) from e
                _DAEMON_FALLBACK_WARNED = True
                print(
                    f"[core_bridge] WARNING: hostd unavailable ({e}); falling back to "
                    "the legacy in-process engine for the REST OF THIS SESSION. Doc "
                    "sharing with the GUI is OFF; restart this server to retry hostd.",
                    file=sys.stderr,
                )
        elif _agent_call():
            raise _hostd_unavailable("it was unreachable earlier in this process")
    with _in_process_call() as dll:
        out = ctypes.c_char_p()
        # Native stdout stays off the MCP JSON-RPC channel via the
        # process-wide redirect installed once at server startup.
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


# The document methods that only READ, copied from hostd's isDocumentReadOnlyMethod
# (core/hostd/doc_registry.cpp: kExact / kPrefixes). hostd is the authority. The in-process
# engine has no daemon to ask, so this copy must stay aligned with hostd.
_HOSTD_READ_EXACT = frozenset({
    "doc_list", "doc_tree", "doc_query", "doc_bbox", "doc_adjacency",
    "doc_get_meta", "doc_validate", "doc_dry_run_ops", "doc_resolve_pick", "doc_feature_edges",
    "doc_mesh_ref_list", "doc_mesh_ref_mesh", "doc_rebuild", "doc_activate",
    "doc_dep_query", "doc_drawing_views", "doc_sketch_overview",
    "doc_sketch_regions", "doc_compile_cpp", "doc_save", "doc_export_step",
    "doc_clear_history", "doc_render_diff", "doc_view_metrics", "asm_world_bodies",
    "asm_source_status", "asm_bom_components",
    "config_list", "sim_study_list", "weld_cut_list", "sketch_health",
    "sketch3d_health", "sketch_probe", "drawing_get", "drawing_bom",
    "drawing_model_items", "drawing_render_png", "cam_describe", "cam_tree",
    "cam_extract", "ecad_describe", "ecad_tree", "ecad_kicad_status",
    "ecad_kicad_check", "ecad_footprints", "ecad_kicad_libraries", "ecad_symbols",
    "piping_route", "piping_edit", "piping_fitting_k",
    "harness_route", "harness_path_check",
})
_HOSTD_READ_PREFIXES = (
    "doc_face_", "doc_render_", "describe_", "render_", "measure_", "query_",
    "eval_", "sim_", "export", "mesh_", "aicm_", "prof_", "agent_",
)
# Document lifecycle calls. call() books each of them explicitly below, and none edits
# authored content, so none counts as a mutation.
_LIFECYCLE_METHODS = frozenset({
    "attach_doc", "detach_doc", "doc_close", "doc_discard", "doc_open", "doc_reload",
    "doc_checkpoint", "ecad_close", "ecad_save", "ecad_reload",
})
_INTERNAL_DOCS: set[tuple] = set()
_INTERNAL_DOCS_LOCK = threading.Lock()


def _may_mutate_document(method: str, params: dict) -> bool:
    if not params.get("doc_id") or method in _LIFECYCLE_METHODS:
        return False
    if method.startswith("sim_study_"):
        # hostd's `sim_` prefix is read-only, but a study write persists into the document.
        return method != "sim_study_list"
    return not (method in _HOSTD_READ_EXACT or method.startswith(_HOSTD_READ_PREFIXES))


def _document_row(doc_id: str) -> dict | None:
    try:
        rows = _dispatch_call("doc_list", {}).get("docs") or []
        return next((r for r in rows if isinstance(r, dict) and r.get("doc_id") == doc_id), None)
    except Exception:  # noqa: BLE001 — a completed mutation must still be tracked
        return None


def _document_version(row: dict | None) -> tuple | None:
    if row is not None and (row.get("revision") is not None or
                            row.get("metadata_revision") is not None):
        return (row.get("revision"), row.get("metadata_revision"))
    return None


# doc_id -> {"name", "start", "peak"}: the name this process last knew the document by, the
# daemon_start_id it was seen under, and the most content (features + components) it has
# been seen to carry. hostd numbers doc ids per daemon run, so after a restart
# "doc-cpp00000002" can name a different part (HERC audit: FRAME-100, then an FEA doc,
# then the chassis assembly). A mutation that would land on a re-issued id is refused.
_DOC_IDENTITY: dict[str, dict] = {}
_DOC_IDENTITY_LOCK = threading.Lock()
_IN_PROCESS_START_ID = f"in-process-{os.getpid()}"


def _engine_start_id() -> Optional[str]:
    """The current engine run's identity (hostd's hello daemon_start_id); None if unknown."""
    if not active_daemon_mode():
        return _IN_PROCESS_START_ID
    from .daemon_bridge import client
    info = getattr(client(), "server_info", None) or {}
    start = info.get("daemon_start_id")
    return str(start) if start not in (None, "") else None


def _row_content(row: dict) -> int:
    total = 0
    for key in ("feature_count", "component_count"):
        value = row.get(key)
        if type(value) is int and value > 0:
            total += value
    return total


def note_doc_identity(doc_id: Optional[str], name: Optional[str],
                      row: Optional[dict] = None) -> None:
    """Record what this process now knows `doc_id` to be (it was created, opened, listed
    to the caller or named explicitly)."""
    if not doc_id or not isinstance(name, str) or not name:
        return
    start = _engine_start_id()
    content = _row_content(row) if isinstance(row, dict) else 0
    with _DOC_IDENTITY_LOCK:
        seen = _DOC_IDENTITY.get(str(doc_id))
        same = seen is not None and seen["name"] == name and seen["start"] == start
        peak = max(seen["peak"], content) if same else content
        _DOC_IDENTITY[str(doc_id)] = {"name": name, "start": start, "peak": peak}


def note_doc_identities(rows: list) -> None:
    for row in rows or []:
        if isinstance(row, dict) and row.get("doc_id"):
            note_doc_identity(str(row["doc_id"]), row.get("name"), row)


def doc_content_peak(doc_id: str) -> int:
    """The most features + components this process has seen `doc_id` carry (0 = unknown)."""
    with _DOC_IDENTITY_LOCK:
        seen = _DOC_IDENTITY.get(str(doc_id))
        return seen["peak"] if seen else 0


def _forget_doc_identity(doc_id: str) -> None:
    with _DOC_IDENTITY_LOCK:
        _DOC_IDENTITY.pop(str(doc_id), None)


def _check_doc_identity(method: str, doc_id: str, row: Optional[dict]) -> None:
    """Refuse a write/close whose doc_id the engine re-issued to a different part.

    Only a daemon restart re-issues ids, so the comparison runs only when the remembered
    daemon_start_id differs from the current one. `row` is the document's live doc_list
    row (already fetched for the mutation), so the check itself costs no RPC."""
    name = row.get("name") if isinstance(row, dict) else None
    if not isinstance(name, str) or not name:
        return  # not open (the engine reports that) or unnamed: nothing to compare
    start = _engine_start_id()
    with _DOC_IDENTITY_LOCK:
        seen = _DOC_IDENTITY.get(str(doc_id))
    if (seen is not None and seen["name"] != name and seen["start"] is not None
            and start is not None and seen["start"] != start):
        old = seen["name"]
        moved = None
        try:
            moved = next((r.get("doc_id") for r in
                          (_dispatch_call("doc_list", {}).get("docs") or [])
                          if isinstance(r, dict) and r.get("name") == old), None)
        except Exception:  # noqa: BLE001 — the refusal stands without the suggestion
            pass
        hint = (f'"{old}" is open now as doc_id="{moved}"; use that id.' if moved else
                f'"{old}" is not open; reopen it with import_part(path="<its .step+>", '
                "resume=True) and use the doc_id it returns.")
        raise CoreError(
            f"Refused {method} on doc_id={doc_id!r}: the engine restarted and re-issued that "
            f'id to "{name}", but this session last used it for "{old}". Nothing was '
            f"changed. {hint} If you do mean \"{name}\", call list_parts() (it refreshes "
            "the ids this session knows) and retry.",
            code="doc_id_reissued",
            detail={"doc_id": doc_id, "expected_name": old, "current_name": name,
                    "expected_doc_id": moved})
    note_doc_identity(doc_id, name, row)


def lease_document(doc_id: str) -> None:
    """Hold a listed/read document on this turn's connection until the pool closes."""
    if not doc_id or CURRENT_AGENT.get() is None or not active_daemon_mode():
        return
    from .daemon_bridge import client

    connection = client()
    writers = getattr(connection, "_mcp_writer_docs", set())
    readers = getattr(connection, "_mcp_reader_docs", set())
    if doc_id in writers or doc_id in readers:
        return
    reply = _dispatch_call("attach_doc", {"doc_id": doc_id, "mode": "ro"})
    if reply.get("ok") is False:
        raise CoreError(str(reply.get("message") or reply.get("error") or "attach failed"),
                        code=str(reply.get("code") or "attach_failed"), detail=reply)
    readers.add(doc_id)
    connection._mcp_reader_docs = readers


def lease_documents(rows: list) -> None:
    for row in rows:
        if isinstance(row, dict) and row.get("doc_id") and row.get("status") != "closed":
            lease_document(str(row["doc_id"]))


def call(method: str, params: Optional[dict] = None) -> dict:
    """Dispatch an RPC and record successful authored document changes once."""
    from services.agent.request_context import owner_key
    owner = owner_key()
    args = params or {}
    doc_id = args.get("doc_id")
    mutating = _may_mutate_document(method, args)
    # Checkpoints require write access in hostd but do not themselves change the
    # document revision or constitute an authored edit.
    if (mutating or (method == "doc_checkpoint" and doc_id)) and active_daemon_mode():
        from .daemon_bridge import client
        connection = client()
        readers = getattr(connection, "_mcp_reader_docs", set())
        writers = getattr(connection, "_mcp_writer_docs", set())
        # A reused/open document can have an untracked read-only attachment.
        if doc_id in readers or (method == "doc_checkpoint" and doc_id not in writers):
            reply = _dispatch_call("attach_doc", {"doc_id": doc_id, "mode": "rw"})
            if reply.get("ok") is False or reply.get("mode") == "ro":
                raise CoreError(
                    "Could not obtain write access to the document automatically; "
                    "retry the operation and check document permissions if it still fails.",
                    code="read_only", detail=reply)
            readers.discard(doc_id)
            writers.add(doc_id)
            connection._mcp_writer_docs = writers
    before_row = (_take_resolution_row(doc_id) or _document_row(doc_id)) if mutating else None
    if mutating:
        _check_doc_identity(method, str(doc_id), before_row)
    elif method in ("doc_close", "doc_discard") and doc_id:
        # _doc() just listed the documents, so its row costs nothing here.
        _check_doc_identity(method, str(doc_id), _take_resolution_row(doc_id))
    before = _document_version(before_row)
    result = _dispatch_call(method, params)
    if result.get("ok") is False or "rejected_tree" in result:
        return result
    if method in ("doc_new", "doc_open") and result.get("doc_id") and result.get("name"):
        note_doc_identity(str(result["doc_id"]), result["name"], result)
    if method in ("doc_close", "doc_discard") and doc_id and not result.get("detached"):
        _forget_doc_identity(str(doc_id))
    if (active_daemon_mode() and (mutating or
            (method in ("doc_new", "doc_open") and not result.get("reused")))
            and (doc_id or result.get("doc_id"))):
        from .daemon_bridge import client
        connection = client()
        writers = getattr(connection, "_mcp_writer_docs", set())
        writers.add(str(doc_id or result["doc_id"]))
        connection._mcp_writer_docs = writers
    if active_daemon_mode() and method in ("doc_close", "doc_discard", "detach_doc") and doc_id:
        from .daemon_bridge import client
        connection = client()
        getattr(connection, "_mcp_writer_docs", set()).discard(doc_id)
        getattr(connection, "_mcp_reader_docs", set()).discard(doc_id)
    if method in ("doc_new", "doc_open") and result.get("doc_id"):
        with _INTERNAL_DOCS_LOCK:
            if args.get("internal"):
                _INTERNAL_DOCS.add((owner, str(result["doc_id"])))
            elif not result.get("reused"):
                _INTERNAL_DOCS.discard((owner, str(result["doc_id"])))
    if method in ("doc_close", "doc_discard") and doc_id and not result.get("detached"):
        with _INTERNAL_DOCS_LOCK:
            _INTERNAL_DOCS.discard((owner, str(doc_id)))
        from .cad_common import forget_touched_doc
        forget_touched_doc(str(doc_id))
    if method == "doc_save" and doc_id and not args.get("preserve_path"):
        from .cad_common import note_document_saved
        note_document_saved(str(doc_id), result)
    created = (method == "doc_new" and not result.get("reused") and
               not args.get("internal") and result.get("doc_id"))
    if created or mutating:
        target = str(created or doc_id)
        after_row = _document_row(target)
        after = _document_version(after_row)
        with _INTERNAL_DOCS_LOCK:
            internal = (owner, target) in _INTERNAL_DOCS
        if not internal and (created or before is None or after is None or before != after):
            from .cad_common import note_touched_doc
            note_touched_doc(target, row=after_row)
    return result


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
