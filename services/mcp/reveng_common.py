"""Shared session store + pipeline machinery for the reveng MCP tool family modules:
session JSON CRUD, research/PDF ingest, provision status, pipeline-agent factory,
mission launcher. Imported by the reveng_*_tools family modules; re-exported through
the reveng_tools facade."""

from __future__ import annotations

import json as _json
import os as _os
import platform as _platform
import shutil as _shutil
import secrets as _secrets
import threading as _threading
import time as _time
from pathlib import Path as _Path
from typing import Any, Optional
import sqlite3 as _sqlite3
import tempfile as _tempfile

from services.agent import request_context as _rc

from mcp.server.fastmcp import FastMCP

# ── helpers from cad_tools (already loaded) ────────────────────────────────
from services.mcp.cad_tools import _dumps, _err as _cad_err  # type: ignore[attr-defined]


def _err(exc: Exception) -> str:
    if isinstance(exc, KeyError) and exc.args == ("not_found",):
        out = {"ok": False, "error": "not_found", "reason": "not_found"}
        if _rc.in_cell():
            out["code"] = "not_found"
        return _dumps(out)
    return _cad_err(exc)

# ── RE-29: research ingest orchestrator ────────────────────────────────────
try:
    from services.vision.research_ingest import research_and_ingest as _research_and_ingest
    _RESEARCH_INGEST_IMPORT_ERROR: Optional[str] = None
except ImportError as _exc:
    _research_and_ingest = None  # type: ignore[assignment]
    _RESEARCH_INGEST_IMPORT_ERROR = str(_exc)


def _research_ingest(
    session_id: str,
    *,
    query: Optional[str] = None,
    part_number: Optional[str] = None,
    url: Optional[str] = None,
) -> dict:
    """Internal helper: run research_and_ingest for *session_id*."""
    if _research_and_ingest is None:
        detail = f" ({_RESEARCH_INGEST_IMPORT_ERROR})" if _RESEARCH_INGEST_IMPORT_ERROR else ""
        return {
            "ok": False,
            "reason": "backend_not_provisioned",
            "hint": ("services.vision.research_ingest failed to import" + detail
                     + "; the Chudware services runtime is incomplete — reprovision it "
                       "(scripts/provision_venv.sh)"),
        }
    session_root = _session_path(session_id)
    try:
        return _research_and_ingest(
            session_root,
            query=query,
            part_number=part_number,
            url=url,
        )
    except Exception as exc:
        return {"ok": False, "reason": "ingest_error", "hint": str(exc)}


# ── RE-06: provisioning probe ───────────────────────────────────────────────
try:
    from services.vision.reveng_provision import reveng_status as _reveng_status
except ImportError:
    def _reveng_status() -> dict:  # type: ignore[misc]
        return {
            "ok": True,
            "capabilities": {k: False for k in
                             ("sparse", "dense", "splat", "single_photo", "webcam", "gpu")},
            "provisioning": {k: False for k in
                             ("colmap", "openmvs", "brush", "triposr", "vggt", "docling")},
            "banners": ["reveng_provision not available"],
            "missing": ["colmap", "openmvs", "brush", "triposr", "vggt", "docling"],
        }

# ── RE-04: video frame extractor ────────────────────────────────────────────
try:
    from services.vision.frames import extract_frames_safe as _extract_frames_safe
except ImportError:
    def _extract_frames_safe(video_path: str, out_dir: str, **kw) -> dict:  # type: ignore[misc]
        return {
            "ok": False,
            "reason": "frames module not available",
            "hint": "install opencv-python",
        }

# ── datasheet ingest (docling) ───────────────────────────────────────────────
try:
    from services.vision.datasheet_ingest import ingest_datasheet as _ingest_datasheet
except ImportError:
    _ingest_datasheet = None  # type: ignore[assignment]


def _ingest_pdf(path: str, out_dir: str) -> dict:
    """Adapter onto RE-15's datasheet_ingest.

    ``reveng_ingest_datasheet`` passes ``<session>/datasheet`` as ``out_dir``, but
    ``ingest_datasheet`` takes the session ROOT and writes the ``datasheet/`` subdir
    itself. It returns ``{ok, tables, crops, mpn_candidates}`` (docling → pdfplumber
    backend chain) or an honest degradation envelope — never raises.
    """
    if _ingest_datasheet is None:
        return {
            "ok": False,
            "reason": "backend_not_provisioned",
            "hint": "pip install docling  (or pdfplumber for text-table extraction)",
        }
    from pathlib import Path as _P
    return _ingest_datasheet(path, _P(out_dir).parent)


# ── session store ───────────────────────────────────────────────────────────

def _reveng_dir() -> _Path:
    if _rc.in_cell():
        return _rc.actor_state_root() / "reveng"
    base = _os.environ.get("CHUDWARE_REVENG_DIR")
    if base:
        return _Path(base).expanduser()
    xdg = _os.environ.get("XDG_DATA_HOME", _os.path.expanduser("~/.local/share"))
    return _Path(xdg) / "Chudware" / "reveng"


def _validate_handle(handle: str) -> None:
    if (not isinstance(handle, str) or not handle or handle in (".", "..")
            or "/" in handle or "\\" in handle or "\x00" in handle):
        raise KeyError("not_found")


def _require_record_owner(record: dict) -> None:
    owner = record.get("_owner")
    _rc.require_owner(tuple(owner) if isinstance(owner, (list, tuple)) else owner)


def _session_path(session_id: str, *, create: bool = False) -> _Path:
    """Resolve a handle, never interpolate a cell handle into a filesystem path."""
    _validate_handle(session_id)
    base = _reveng_dir()
    if not _rc.in_cell():
        path = base / session_id
        if path.resolve().parent != base.resolve():
            raise KeyError("not_found")
        return path
    base.mkdir(parents=True, exist_ok=True)
    with _sqlite3.connect(base / "handles.sqlite", timeout=15) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS handles (scope TEXT PRIMARY KEY, directory TEXT NOT NULL)")
        key = _rc.scoped_key(session_id)
        row = conn.execute("SELECT directory FROM handles WHERE scope=?", (key,)).fetchone()
        if row is None:
            if not create:
                raise KeyError("not_found")
            # Directory identity is independent of the client/server session handle.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT directory FROM handles WHERE scope=?", (key,)).fetchone()
            if row is None:
                directory = _tempfile.mkdtemp(prefix="session-", dir=base)
                conn.execute("INSERT INTO handles VALUES (?, ?)", (key, directory))
                row = (directory,)
    path = _Path(row[0])
    if path.resolve().parent != base.resolve():
        raise KeyError("not_found")
    manifest = path / "session.json"
    if manifest.is_file():
        try:
            _require_record_owner(_json.loads(manifest.read_text()))
        except (OSError, ValueError, TypeError, AttributeError):
            raise KeyError("not_found") from None
    return path


def _list_sessions() -> list[dict]:
    base = _reveng_dir()
    if not base.is_dir():
        return []
    sessions = []
    for path in sorted(base.glob("*/session.json")):
        try:
            manifest = _json.loads(path.read_text())
            _require_record_owner(manifest)
            session = _load_session(manifest["session_id"])
            if session is not None:
                sessions.append(session)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sessions


def _session_json_path(session_id: str) -> _Path:
    return _session_path(session_id) / "session.json"


def _new_session_id() -> str:
    return "re_" + _secrets.token_hex(16 if _rc.in_cell() else 4)


def _now_iso() -> str:
    import datetime
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_session(session_id: str, name: str, part_ref: Optional[str]) -> dict:
    _validate_handle(session_id)
    now = _now_iso()
    return {
        "schema": "aicad.reveng_session/1",
        "session_id": session_id,
        "_owner": _rc.owner_key(),
        "name": name,
        "created_at": now,
        "updated_at": now,
        "status": "capturing",
        "part_ref": part_ref,
        "stages": {
            "capture": "idle",
            "scale": "pending",
            "reconstruct": "idle",
            "model": "idle",
            "review": "idle",
            "deliver": "idle",
        },
        "scale": {
            "method": None,
            "mm_per_unit": None,
            "residual_pct": None,
            "confirmed": False,
        },
        "artifacts": {
            "images": 0,
            "sparse_dir": None,
            "mesh_path": None,
            "splat_path": None,
            "doc_id": None,
        },
        "honesty": {
            "pipeline": "scan_mesh_ai_parametric",
            "single_photo_tier": None,
            "approximate": False,
        },
        "provenance": [],
    }


def _load_session(session_id: str, *, merge_gaussian: bool = True) -> Optional[dict]:
    try:
        p = _session_json_path(session_id)
        session = _json.loads(p.read_text())
        if not isinstance(session, dict) or session.get("session_id") != session_id:
            raise KeyError("not_found")
        _require_record_owner(session)
        from services.reveng.gaussian_jobs import status as splat_status
        gaussian = splat_status(session_id) if merge_gaussian else {}
        if gaussian.get("run_id"):
            session["gaussian"] = gaussian
            artifacts = session.setdefault("artifacts", {})
            for key in ("splat_path", "sparse_dir"):
                value = gaussian.get(key)
                if value:
                    artifacts[key] = str(_Path(value).resolve())
        return session
    except (OSError, ValueError, KeyError, TypeError):
        if _rc.in_cell():
            raise KeyError("not_found") from None
        return None


def _save_session(session: dict) -> None:
    _require_record_owner(session)
    sid = session["session_id"]
    root = _session_path(sid, create=True)
    root.mkdir(parents=True, exist_ok=True)
    session["updated_at"] = _now_iso()
    from services.reveng.gaussian_jobs import _atomic, _read
    stored = dict(session)
    gaussian = stored.pop("gaussian", None)
    if isinstance(gaussian, dict) and gaussian.get("run_id"):
        # Projection is read-only. A later capture save must not bake Gaussian
        # sparse coordinates into the CAD mission's own artifact namespace.
        previous = _read(_session_json_path(sid)).get("artifacts") or {}
        artifacts = dict(stored.get("artifacts") or {})
        for key in ("splat_path", "sparse_dir"):
            if gaussian.get(key) and artifacts.get(key) == gaussian[key]:
                if key in previous:
                    artifacts[key] = previous[key]
                else:
                    artifacts.pop(key, None)
        stored["artifacts"] = artifacts
    _atomic(_session_json_path(sid), stored)


def _session_summary(s: dict) -> dict:
    summary = {
        "session_id": s["session_id"],
        "name": s["name"],
        "status": s["status"],
        "created_at": s["created_at"],
        "updated_at": s["updated_at"],
        "part_ref": s.get("part_ref"),
        "images": len(_session_image_paths(s["session_id"])),
    }
    gaussian = s.get("gaussian")
    if isinstance(gaussian, dict) and gaussian.get("run_id"):
        summary["gaussian"] = {
            "status": gaussian.get("status", ""),
            "stage": gaussian.get("stage", ""),
            "mode": gaussian.get("mode", ""),
            "frames": gaussian.get("frames", 0),
            "registered": gaussian.get("registered", 0),
            "pct": gaussian.get("pct", 0),
        }
    return summary


# ── backend_not_provisioned stub ─────────────────────────────────────────────

def _not_provisioned(hint: str) -> str:
    return _dumps({
        "ok": False,
        "reason": "backend_not_provisioned",
        "hint": hint,
    })


# ── RE-17: DAG mission launch + aicad.reveng_events/1 emission ───────────────
#
# The long-job tools (reveng_reconstruct / reveng_build_parametric) launch the
# frozen DAG mission templates under services/dag/templates/ via the executor.
# They gate on reveng_status(): when the needed backend is absent they return the
# honest backend_not_provisioned envelope (unchanged behaviour for un-provisioned
# environments — including CI, where nothing is provisioned); when present they
# start the mission and return a run_id.
#
# Progress is surfaced as aicad.reveng_events/1 records: we wrap the executor's
# emit callback and translate the frozen dag.node.* events (§11 of dag_schema.md)
# into reveng stage events, keyed by node_id → stage. The executor's EventJournal
# persists BOTH the dag.* events and our translated reveng events to the same run's
# events.jsonl, which the GUI (feedRevEngEvent) drains.

_TEMPLATES_DIR = _Path(__file__).resolve().parent.parent / "dag" / "templates"

# node_id → (stage, pct), or (start_stage, done_stage, pct) for a node that spans more
# than one stage. Stages are the aicad.reveng_events/1 §4 vocabulary; pct is a coarse
# monotone progress hint.
#
# ``n_dense`` passes through three §4 stages (dense → splat → mesh) and emits the
# intermediate ones itself via the progress emitter from make_reveng_stage_emitter();
# it opens on ``dense`` and closes on ``mesh`` so the GUI sub-step track never rewinds.
_SCAN_NODE_STAGE: dict[str, tuple] = {
    "n_capture":          ("capture", 5),
    "n_sparse":           ("sparse", 25),
    "n_scale_gate":       ("scale", 35),
    "n_dense":            ("dense", "mesh", 55),
    "n_fit":              ("fit", 65),
    "n_parametric":       ("parametric", 78),
    "n_dim_gate":         ("parametric", 82),
    "n_verify_deviation": ("review", 90),
    "n_iterate":          ("parametric", 92),
    "n_deliver":          ("deliver", 100),
}

# node_id → (stage, pct) for the reveng_datasheet template.
#
# RE-17 scope note: the two long-job tools wired here (reveng_reconstruct /
# reveng_build_parametric) both drive the *scan* pipeline (reveng_scan.json) per
# contracts/reveng.md §8.2 — datasheet-sourced builds are launched by RE-15's
# datasheet-ingest tool, which is out of RE-17's write-set. This stage map is the
# ready-to-use translation table for that launcher (its template is already frozen
# at services/dag/templates/reveng_datasheet.json); it is intentionally not yet
# referenced by a tool in this module. Unknown node ids fall back to a "parametric"
# stage in make_reveng_emit so an event is still emitted.
_DATASHEET_NODE_STAGE: dict[str, tuple[str, int]] = {
    "n_ingest":         ("capture", 10),
    "n_mpn_fastpath":   ("capture", 25),
    "n_datasheet_read": ("fit", 45),
    "n_dim_gate":       ("parametric", 60),
    "n_build":          ("parametric", 80),
    "n_fix_dims":       ("parametric", 82),
    "n_final_gate":     ("review", 90),
    "n_deliver":        ("deliver", 100),
}


def _load_template(name: str) -> dict:
    """Load a frozen aicad.dag/1 mission template by base name (no extension)."""
    path = _TEMPLATES_DIR / f"{name}.json"
    return _json.loads(path.read_text())


def _reveng_runs_dir() -> Optional[_Path]:
    """Optional runs-dir override for hermetic tests (CHUDWARE_REVENG_RUNS_DIR)."""
    if _rc.in_cell():
        return _rc.actor_state_root() / "runs"
    d = _os.environ.get("CHUDWARE_REVENG_RUNS_DIR")
    return _Path(d).expanduser() if d else None


def make_reveng_emit(run_id: str, session_id: str, stage_map: dict):
    """Return an emit callback that also writes aicad.reveng_events/1 records.

    The returned callable has signature ``(event_type, payload)`` — the executor's
    emit contract. For every ``dag.node.started`` / ``dag.node.done`` /
    ``dag.node.paused`` event it derives the reveng stage from *stage_map* and
    writes an ``aicad.reveng_events/1`` line to the SAME run's events.jsonl via a
    dedicated EventJournal, so the GUI stepper drains stage progress alongside the
    raw dag.* events. Never raises (progress emission must not break a run).
    """
    from services.dag.event_journal import EventJournal
    owner = _rc.owner_key()

    journal = EventJournal(run_id)

    def _emit(event_type: str, payload: dict) -> None:
        try:
            _rc.require_owner(owner)
            if event_type not in ("dag.node.started", "dag.node.done", "dag.node.paused"):
                return
            node_id = payload.get("node_id", "")
            entry = stage_map.get(node_id) or ("parametric", 50)
            # A node whose work spans several §4 stages declares (start, done, pct) so
            # its completion event does not rewind the GUI sub-step track to the stage
            # it opened with; the 2-tuple form means start and done share one stage.
            if len(entry) == 3:
                start_stage, done_stage, pct = entry
            else:
                start_stage, pct = entry
                done_stage = start_stage
            stage = start_stage
            if event_type == "dag.node.started":
                msg = f"{stage}: started ({node_id})"
                epct = max(0, pct - 5)
            elif event_type == "dag.node.paused":
                reason = payload.get("reason", "gate")
                msg = f"{stage}: paused for {reason} ({node_id})"
                epct = pct
            else:  # dag.node.done
                stage = done_stage
                msg = f"{stage}: done ({node_id})"
                epct = pct
            journal.emit("aicad.reveng_events/1", {
                "schema": "aicad.reveng_events/1",
                "session_id": session_id,
                "stage": stage,
                "pct": int(max(0, min(100, epct))),
                "msg": msg,
                "artifact": payload.get("artifact"),
            })
        except Exception:  # noqa: BLE001 — progress emission is best-effort
            return

    return _emit


def make_reveng_stage_emitter(run_id: str, session_id: str):
    """Return ``progress(stage, pct, msg, artifact=None)`` for INTRA-node stages.

    make_reveng_emit() translates one dag.node.* event into one stage, which is all a
    node whose work maps 1:1 onto a stage needs.  ``n_dense`` is not one of those: it
    runs dense MVS, then optionally a splat, then mesh cleanup — three distinct
    ``aicad.reveng_events/1`` §4 stages (``dense``, ``splat``, ``mesh``) that were
    declared in the contract but never emitted while all of that hid inside a single
    ``sparse``-stage node.  This handle writes them to the SAME run journal, so the GUI
    sub-step track (which substring-matches ``sparse``/``dense``/``mesh`` on the
    ``stage`` field, in that order) actually lights up.

    ``stage`` is written bare — never a prose sentence — so that match stays exact;
    human-readable detail belongs in ``msg``.  Never raises: progress is best-effort.
    """
    from services.dag.event_journal import EventJournal
    owner = _rc.owner_key()

    journal = EventJournal(run_id)

    def _progress(stage: str, pct: int, msg: str, artifact: Any = None) -> None:
        try:
            _rc.require_owner(owner)
            journal.emit("aicad.reveng_events/1", {
                "schema": "aicad.reveng_events/1",
                "session_id": session_id,
                "stage": stage,
                "pct": int(max(0, min(100, int(pct)))),
                "msg": msg,
                "artifact": artifact,
            })
        except Exception:  # noqa: BLE001 — progress emission is best-effort
            return

    return _progress


def _call_reverse_engineer(
    source: str,
    *,
    name: Optional[str] = None,
    hints: Optional[dict] = None,
) -> dict:
    """Invoke the recon ``reverse_engineer`` MCP tool programmatically.

    Registers tools on a throwaway FastMCP so the pipeline agent_fn does not
    depend on a live server. Tests may monkeypatch this helper.
    """
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: PLC0415
        from services.mcp.recon_tools import register_recon_tools  # noqa: PLC0415

        mcp = FastMCP("_reveng_internal")
        register_recon_tools(mcp)
        tool = mcp._tool_manager.get_tool("reverse_engineer")
        if tool is None or not getattr(tool, "fn", None):
            return {"ok": False, "error": "reverse_engineer tool not registered"}
        raw = tool.fn(source=source, name=name, hints=hints)
        if isinstance(raw, str):
            return _json.loads(raw)
        return raw if isinstance(raw, dict) else {"ok": False, "error": "bad reverse_engineer result"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"reverse_engineer failed: {exc}"}


def _session_image_paths(session_id: str) -> list[str]:
    """Collect stills + extracted frames for a session (images/ then frames/)."""
    root = _session_path(session_id)
    paths: list[str] = []
    for sub in ("images", "frames"):
        d = root / sub
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix.lower() in (
                ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp",
            ):
                paths.append(str(p.resolve()))
    return paths


def _registration_ratio(sparse_result: dict, n_images: int) -> float:
    """Fraction of images registered by sparse SfM (0..1)."""
    if n_images <= 0:
        return 0.0
    reg = sparse_result.get("registered")
    if reg is None:
        reg = sparse_result.get("n_images") or sparse_result.get("n_registered") or 0
    try:
        return max(0.0, min(1.0, float(reg) / float(n_images)))
    except (TypeError, ValueError):
        return 0.0


def _autosolve_scale(session_id: str) -> dict:
    """Attempt the ArUco-mat scale solve now that a sparse model exists.

    The primary scale path is fully automatic: a printed ArUco mat in frame plus a
    registered sparse model is everything ``solve_scale_aruco`` needs.  Running it at
    the tail of ``n_sparse`` — immediately before the scale-confirm gate — is what puts
    real numbers (``mm_per_unit`` + ``residual_pct``) in front of the human at the
    pause, instead of asking them to approve a blank.  It never overwrites a scale the
    session already carries (a caliper ``known_dim``, or an earlier solve), and a
    failure is reported rather than fatal: the gate still runs, and the mission may
    proceed unscaled with an honest ``mesh_units: "reconstruction"``.
    """
    s = _load_session(session_id, merge_gaussian=False) or {}
    existing = (s.get("scale") or {}).get("mm_per_unit")
    try:
        if existing is not None and float(existing) > 0.0:
            return {
                "attempted": False,
                "reason": "scale_already_set",
                "mm_per_unit": float(existing),
                "residual_pct": (s.get("scale") or {}).get("residual_pct"),
                "method": (s.get("scale") or {}).get("method"),
            }
    except (TypeError, ValueError):
        pass
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: PLC0415
        from .reveng_session_tools import (  # noqa: PLC0415
            register_reveng_session_tools,
        )

        mcp = FastMCP("_reveng_scale_internal")
        register_reveng_session_tools(mcp)
        tool = mcp._tool_manager.get_tool("reveng_set_scale")
        fn = getattr(tool, "fn", None) if tool is not None else None
        if fn is None:
            return {"attempted": False, "reason": "reveng_set_scale not registered"}
        raw = fn(session_id=session_id, method="aruco_mat", params={})
        out = _json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:  # noqa: BLE001
        return {"attempted": True, "ok": False,
                "reason": f"aruco_autosolve_error: {exc}"}
    if not isinstance(out, dict):
        return {"attempted": True, "ok": False,
                "reason": "bad reveng_set_scale result"}
    scale = out.get("scale") or {}
    return {
        "attempted": True,
        "ok": bool(out.get("ok")),
        "method": "aruco_mat",
        "mm_per_unit": scale.get("mm_per_unit"),
        "residual_pct": scale.get("residual_pct"),
        "reason": out.get("reason"),
        "hint": out.get("hint"),
    }


def _make_pipeline_agent_fn(
    session_id: str,
    *,
    strategy: str = "deterministic",
    progress: Optional[Any] = None,
):
    """Return a deterministic node ``agent_fn`` that runs the RE pipeline stages.

    ``agent_fn(request, context_pack) -> AgentResult`` is the seam node_runner
    dispatches every non-verify/non-gate node through. Rather than invoke an LLM,
    this dispatcher wires each node to the real vision backends, degrading honestly
    to a non-success result (which the node ESCALATES on per its on_fail) when a
    backend module or its provisioning is absent. It never re-invokes the reveng
    MCP launch tools (that would recurse — see work/questions/RE-17.md Q1).

    strategy:
        ``deterministic`` (default) — reverse_engineer with fit hints.
        ``ml`` — lazy-import services.vision.cad_recode.build_parametric.

    progress:
        Optional ``(stage, pct, msg, artifact=None)`` callback from
        make_reveng_stage_emitter(), used by ``n_dense`` to emit the ``dense`` /
        ``splat`` / ``mesh`` sub-stages that a node-level stage map cannot express.
        Absent (direct calls, unit tests) it is a no-op.
    """
    class _Result:
        def __init__(self, ok: bool, reason: str, data: Optional[dict] = None):
            self.success = ok
            self.stop_reason = "confirmed" if ok else "failed"
            self.final_code = None
            self.result = data or {"detail": reason}

    strat = (strategy or "deterministic").strip().lower()
    if strat in ("override_approximate",):
        # override is a honesty gate on the MCP tool, not a rebuild strategy
        strat = "deterministic"

    def _noop_progress(stage: str, pct: int, msg: str, artifact: Any = None) -> None:
        return None

    _progress = progress if callable(progress) else _noop_progress

    def _agent_fn(request: str, context_pack: dict):
        node_id = context_pack.get("node_id") or context_pack.get("node_name", "")
        caps = {}
        try:
            caps = (_reveng_status() or {}).get("capabilities", {})
        except Exception:  # noqa: BLE001
            caps = {}

        # Capture ingest: sources are added by the interactive tools before launch;
        # treat as satisfied (honest — no backend needed to report coverage).
        if node_id == "n_capture":
            return _Result(True, "capture sources ingested")

        # ── Reconstruction, split at the scale gate ──────────────────────────
        #
        # RE-40: this used to be ONE ``n_reconstruct`` node running sparse → dense →
        # mesh, sitting DOWNSTREAM of the scale-confirm gate.  That deadlocked: both
        # real scale solvers (aruco_mat, two_point) resolve against a registered COLMAP
        # sparse model and answer ``scale_needs_sparse`` without one, so the gate could
        # never be satisfied at the point the template asked for it.  Sparse now lands
        # BEFORE the gate and dense AFTER it — which is also the right photogrammetry
        # order: never pay for dense/mesh reconstruction of a part whose millimetres a
        # human has not signed off, and clean the mesh with that confirmed factor.

        def _run_sparse() -> tuple[bool, str, dict]:
            """Sparse SfM (+ feed-forward fallback). Returns ``(ok, reason, payload)``."""
            images = _session_image_paths(session_id)
            if not images:
                return False, "no_images", {
                    "detail": "session has no images/ or frames/ for reconstruction",
                }
            session_dir = str(_session_path(session_id))
            image_dir = str(_session_path(session_id) / "images")
            # Prefer images/ when present; otherwise point at frames/.
            if not (_session_path(session_id) / "images").is_dir() or not any(
                _session_path(session_id).joinpath("images").iterdir()
                if (_session_path(session_id) / "images").is_dir() else []
            ):
                image_dir = str(_session_path(session_id) / "frames")

            sparse_result: dict = {"ok": False, "reason": "sparse_not_attempted"}
            used_ffwd = False

            # Primary sparse: COLMAP / pycolmap
            try:
                from services.vision.colmap_pipeline import run_sparse  # noqa: PLC0415
            except Exception as exc:  # noqa: BLE001
                run_sparse = None  # type: ignore[assignment]
                sparse_result = {
                    "ok": False,
                    "reason": "backend_not_provisioned",
                    "hint": f"colmap_pipeline import failed: {exc}",
                }
            if run_sparse is not None:
                try:
                    sparse_result = run_sparse(image_dir, session_dir) or {
                        "ok": False, "reason": "run_sparse returned empty",
                    }
                except Exception as exc:  # noqa: BLE001
                    sparse_result = {
                        "ok": False,
                        "reason": f"run_sparse error: {exc}",
                        "hint": "check pycolmap / image set",
                    }

            weak = (
                not sparse_result.get("ok")
                or _registration_ratio(sparse_result, len(images)) < 0.60
            )
            if weak:
                try:
                    from services.vision.ffwd_sfm import run_ffwd_sfm  # noqa: PLC0415
                    ffwd = run_ffwd_sfm(images, session_dir) or {
                        "ok": False, "reason": "run_ffwd_sfm returned empty",
                    }
                    if ffwd.get("ok"):
                        sparse_result = ffwd
                        used_ffwd = True
                    elif not sparse_result.get("ok"):
                        sparse_result = ffwd
                except Exception as exc:  # noqa: BLE001
                    if not sparse_result.get("ok"):
                        sparse_result = {
                            "ok": False,
                            "reason": "backend_not_provisioned",
                            "hint": f"sparse+ffwd unavailable: {exc}",
                        }

            if not sparse_result.get("ok"):
                return (
                    False,
                    sparse_result.get("reason") or "backend_not_provisioned: sparse",
                    sparse_result,
                )

            sparse_dir = (
                sparse_result.get("sparse_dir")
                or str(_Path(session_dir) / "sparse")
            )
            s = _load_session(session_id, merge_gaussian=False) or _make_session(session_id, session_id, None)
            s.setdefault("artifacts", {})
            s["artifacts"]["sparse_dir"] = sparse_dir
            s["stages"]["reconstruct"] = "running"
            _save_session(s)
            return True, "sparse model registered", {
                "sparse": sparse_result,
                "sparse_dir": sparse_dir,
                "used_ffwd": used_ffwd,
            }

        def _run_dense(
            sparse_result: dict,
            sparse_dir: Optional[str],
            used_ffwd: bool,
            *,
            require_confirmed_scale: bool,
            provenance_step: str,
        ) -> tuple[bool, str, dict]:
            """Dense MVS + mesh cleanup. Returns ``(ok, reason, payload)``.

            ``require_confirmed_scale`` decides which scales may touch the geometry.
            On the gated template path the scale-confirm gate is immediately upstream,
            so only a scale a human actually confirmed is applied — that gate is the
            product's guarantee that the delivered millimetres were validated.  The
            ungated ``n_reconstruct`` compatibility path has no such gate, so it keeps
            the historical rule (apply any solved ratio).  Neither path ever invents a
            factor: with nothing usable the mesh stays in reconstruction units and
            ``artifacts.mesh_units`` says exactly that.
            """
            session_dir = str(_session_path(session_id))
            s = _load_session(session_id, merge_gaussian=False) or _make_session(session_id, session_id, None)
            if not sparse_dir:
                sparse_dir = (s.get("artifacts") or {}).get("sparse_dir")
            if not sparse_dir:
                return False, "no_sparse", {
                    "detail": "dense reconstruction consumes the COLMAP sparse model",
                    "hint": "run the n_sparse node (reveng_reconstruct) first",
                }

            # CAD reconstruction requires an actual mesh. Brush belongs exclusively
            # to gaussian_jobs; a Gaussian PLY is never a mesh reconstruction.
            mesh_path: Optional[str] = None
            dense_notes: list = []

            if _os.environ.get("OPENMVS_BIN"):
                _progress("dense", 45, "dense MVS (OpenMVS) started")
                try:
                    from services.vision.dense_backends import run_openmvs  # noqa: PLC0415
                    omvs = run_openmvs(sparse_dir, session_dir) or {}
                    if omvs.get("ok") and omvs.get("mesh_path"):
                        mesh_path = omvs["mesh_path"]
                        dense_notes.append("openmvs")
                        _progress("dense", 55, "dense MVS complete", mesh_path)
                    else:
                        dense_notes.append(
                            f"openmvs:{omvs.get('reason') or 'failed'}"
                        )
                except Exception as exc:  # noqa: BLE001
                    dense_notes.append(f"openmvs_import:{exc}")

            if not mesh_path:
                # Sparse-only is not a full reconstruct success for the mesh path,
                # but if sparse ok we still report honest partial.
                s = _load_session(session_id, merge_gaussian=False) or s
                s.setdefault("artifacts", {})
                s["artifacts"]["sparse_dir"] = sparse_dir
                _save_session(s)
                return False, "no_mesh", {
                    "sparse": sparse_result,
                    "dense_notes": dense_notes,
                    "used_ffwd": used_ffwd,
                    "hint": "Configure OPENMVS_BIN for CAD mesh reconstruction; "
                            "use reveng_start_splat for the separate Gaussian workflow.",
                }

            # Mesh cleanup when we have a mesh.
            #
            # session.scale.mm_per_unit is a RATIO (multiply reconstruction-space lengths
            # by it to get mm) — exactly what cleanup_mesh's mm_per_unit means. When no
            # usable scale is on the session, leave the mesh in reconstruction units
            # rather than inventing a factor, and say so on the session.
            cleaned = mesh_path
            mesh_unscaled = True
            if mesh_path:
                _progress("mesh", 62, "mesh cleanup started", mesh_path)
                try:
                    from services.vision.mesh_cleanup import cleanup_mesh  # noqa: PLC0415
                    out_clean = str(
                        _Path(session_dir) / "mesh" / "cleaned.ply"
                    )
                    _Path(out_clean).parent.mkdir(parents=True, exist_ok=True)
                    mm_per_unit = None
                    scale_block = (_load_session(session_id, merge_gaussian=False) or {}).get("scale") or {}
                    try:
                        raw_scale = scale_block.get("mm_per_unit")
                        if raw_scale is not None:
                            mm_per_unit = float(raw_scale)
                    except (TypeError, ValueError):
                        mm_per_unit = None
                    if mm_per_unit and mm_per_unit > 0.0 and require_confirmed_scale \
                            and not scale_block.get("confirmed"):
                        dense_notes.append(
                            "cleanup:unconfirmed — a scale of "
                            f"{mm_per_unit} mm/unit is solved but the scale-confirm gate "
                            "has not approved it; mesh stays in reconstruction units"
                        )
                        mm_per_unit = None
                    if not mm_per_unit or mm_per_unit <= 0.0:
                        mm_per_unit = None
                        dense_notes.append(
                            "cleanup:unscaled — session.scale.mm_per_unit is unset; mesh stays "
                            "in reconstruction units until reveng_set_scale() solves the scale"
                        )
                    cl = cleanup_mesh(mesh_path, out_clean, mm_per_unit=mm_per_unit) or {}
                    if cl.get("ok") and cl.get("mesh_path"):
                        cleaned = cl["mesh_path"]
                        mesh_unscaled = not bool(cl.get("scale_applied"))
                    else:
                        dense_notes.append(
                            f"cleanup:{cl.get('reason') or 'skipped'}"
                        )
                except Exception as exc:  # noqa: BLE001
                    dense_notes.append(f"cleanup_import:{exc}")

            mesh_units = "reconstruction" if mesh_unscaled else "mm"
            s = _load_session(session_id, merge_gaussian=False) or s
            s.setdefault("artifacts", {})
            s["artifacts"]["sparse_dir"] = sparse_dir
            if cleaned:
                s["artifacts"]["mesh_path"] = cleaned
                # Honest units tag: consumers (fit / parametric / GUI) must not read a
                # reconstruction-unit mesh as millimetres.
                s["artifacts"]["mesh_units"] = mesh_units
            s["stages"]["reconstruct"] = "done"
            s["provenance"].append({
                "step": provenance_step,
                "sparse_backend": sparse_result.get("backend"),
                "used_ffwd": used_ffwd,
                "dense": dense_notes,
                "mesh_units": mesh_units,
                "ts": _now_iso(),
            })
            _save_session(s)
            _progress("mesh", 64, f"mesh ready ({mesh_units})", cleaned)
            return True, "reconstruction complete", {
                "sparse": sparse_result,
                "mesh_path": cleaned,
                "mesh_units": mesh_units,
                "splat_path": None,
                "used_ffwd": used_ffwd,
                "dense_notes": dense_notes,
            }

        # ── n_sparse: SfM only, then solve scale off the fresh sparse model ──
        if node_id == "n_sparse":
            ok, reason, payload = _run_sparse()
            if not ok:
                return _Result(False, reason, payload)
            payload["scale"] = _autosolve_scale(session_id)
            return _Result(True, reason, payload)

        # ── n_scale_gate: the human already approved by the time we run ──────
        #
        # graph_loader runs gate.run_gate() BEFORE the node body, so reaching here
        # means the interrupt returned "approve".  Recording confirmed=True is
        # therefore the human's decision, never an assumption — a reject never gets
        # here (the handler returns paused without dispatching the agent).
        if node_id == "n_scale_gate":
            s = _load_session(session_id, merge_gaussian=False)
            if s is None:
                return _Result(False, f"session {session_id!r} not found")
            scale = s.setdefault("scale", {})
            mm = scale.get("mm_per_unit")
            try:
                usable = mm is not None and float(mm) > 0.0
            except (TypeError, ValueError):
                usable = False
            if not usable:
                # Approving a gate whose payload showed mm_per_unit: null is an
                # informed decision to continue UNSCALED — record it as such rather
                # than confirming a number that does not exist.
                scale["confirmed"] = False
                s.setdefault("provenance", []).append({
                    "step": "n_scale_gate",
                    "confirmed": False,
                    "scale_state": "unsolved",
                    "ts": _now_iso(),
                })
                _save_session(s)
                return _Result(True, "scale gate passed unscaled", {
                    "confirmed": False,
                    "scale_state": "unsolved",
                    "mm_per_unit": None,
                    "hint": "no scale solved — the mesh will stay in reconstruction "
                            "units (artifacts.mesh_units='reconstruction'); run "
                            "reveng_set_scale() and re-run to get millimetres",
                })
            scale["confirmed"] = True
            s.setdefault("stages", {})["scale"] = "done"
            s.setdefault("provenance", []).append({
                "step": "n_scale_gate",
                "confirmed": True,
                "method": scale.get("method"),
                "mm_per_unit": scale.get("mm_per_unit"),
                "residual_pct": scale.get("residual_pct"),
                "ts": _now_iso(),
            })
            _save_session(s)
            return _Result(True, "scale confirmed", {
                "confirmed": True,
                "mm_per_unit": scale.get("mm_per_unit"),
                "residual_pct": scale.get("residual_pct"),
                "method": scale.get("method"),
            })

        # ── n_dense: CAD mesh with the CONFIRMED scale ──────────────────────
        if node_id == "n_dense":
            s = _load_session(session_id, merge_gaussian=False) or {}
            sparse_dir = (s.get("artifacts") or {}).get("sparse_dir")
            ok, reason, payload = _run_dense(
                {}, sparse_dir, False,
                require_confirmed_scale=True,
                provenance_step="n_dense",
            )
            return _Result(ok, reason, payload)

        # ── n_reconstruct: pre-split compatibility alias ─────────────────────
        #
        # The frozen template no longer has this node; direct callers that still ask
        # for it get the historical composite (sparse then dense in one call) with the
        # historical ungated scale rule, so nothing that addressed it by name breaks.
        if node_id == "n_reconstruct":
            ok, reason, payload = _run_sparse()
            if not ok:
                return _Result(False, reason, payload)
            ok, reason, dense_payload = _run_dense(
                payload.get("sparse") or {},
                payload.get("sparse_dir"),
                bool(payload.get("used_ffwd")),
                require_confirmed_scale=False,
                provenance_step="n_reconstruct",
            )
            if not ok:
                dense_payload.setdefault("sparse", payload.get("sparse") or {})
                dense_payload.setdefault("used_ffwd", bool(payload.get("used_ffwd")))
            return _Result(ok, reason, dense_payload)

        # ── Primitive fit ────────────────────────────────────────────────────
        if node_id in ("n_fit",):
            s = _load_session(session_id, merge_gaussian=False)
            if s is None:
                return _Result(False, f"session {session_id!r} not found")
            mesh_path = (s.get("artifacts") or {}).get("mesh_path")
            if not mesh_path:
                return _Result(
                    False,
                    "no_mesh",
                    {"detail": "run reconstruction first; session has no mesh_path"},
                )
            try:
                from services.vision.segment_fit import segment_fit  # noqa: PLC0415
            except Exception as exc:  # noqa: BLE001
                return _Result(
                    False,
                    "backend_not_provisioned: segment_fit",
                    {"hint": f"segment_fit import failed: {exc}"},
                )
            try:
                fits = segment_fit(mesh_path, session_id=session_id) or {}
            except Exception as exc:  # noqa: BLE001
                return _Result(False, f"segment_fit error: {exc}")
            if not fits.get("ok"):
                return _Result(
                    False,
                    fits.get("reason") or "backend_not_provisioned: segment_fit",
                    fits,
                )
            # Persist aicad.reveng_fits/1 for n_parametric / GUI
            fits_path = _session_path(session_id) / "fits.json"
            try:
                fits_path.write_text(_json.dumps(fits, indent=2))
            except Exception:  # noqa: BLE001
                pass
            s.setdefault("artifacts", {})
            s["artifacts"]["fits_path"] = str(fits_path)
            s["stages"]["model"] = "fitting"
            s["provenance"].append({
                "step": "n_fit",
                "n_fits": len(fits.get("fits") or []),
                "ts": _now_iso(),
            })
            _save_session(s)
            return _Result(True, "primitives fit", fits)

        # ── Parametric rebuild ───────────────────────────────────────────────
        if node_id in ("n_parametric", "n_iterate"):
            s = _load_session(session_id, merge_gaussian=False)
            if s is None:
                return _Result(False, f"session {session_id!r} not found")
            mesh_path = (s.get("artifacts") or {}).get("mesh_path")
            if not mesh_path:
                return _Result(
                    False,
                    "no_mesh",
                    {"detail": "parametric build needs a cleaned mesh_path"},
                )

            # Load fits hints if present
            hints = None
            fits_path = (s.get("artifacts") or {}).get("fits_path")
            if fits_path and _Path(fits_path).is_file():
                try:
                    hints = _json.loads(_Path(fits_path).read_text())
                except Exception:  # noqa: BLE001
                    hints = None
            if hints is None:
                alt = _session_path(session_id) / "fits.json"
                if alt.is_file():
                    try:
                        hints = _json.loads(alt.read_text())
                    except Exception:  # noqa: BLE001
                        hints = None

            scale_transform = None
            sc = s.get("scale") or {}
            if sc.get("mm_per_unit") is not None:
                scale_transform = {
                    "scale": sc.get("mm_per_unit"),
                    "method": sc.get("method"),
                }

            if strat == "ml":
                try:
                    from services.vision.cad_recode import (  # noqa: PLC0415
                        build_parametric,
                    )
                except Exception as exc:  # noqa: BLE001
                    return _Result(
                        False,
                        "backend_not_provisioned: cad_recode",
                        {"hint": f"cad_recode import failed: {exc}"},
                    )
                try:
                    ml = build_parametric(
                        mesh_path,
                        str(_session_path(session_id)),
                        scale_transform,
                    ) or {}
                except TypeError:
                    # tolerate keyword-only signature variants
                    try:
                        ml = build_parametric(
                            mesh_path,
                            str(_session_path(session_id)),
                            scale_transform=scale_transform,
                        ) or {}
                    except Exception as exc:  # noqa: BLE001
                        return _Result(False, f"build_parametric error: {exc}")
                except Exception as exc:  # noqa: BLE001
                    return _Result(False, f"build_parametric error: {exc}")
                if not ml.get("ok") and ml.get("error"):
                    return _Result(
                        False,
                        ml.get("error") or "ml parametric failed",
                        ml,
                    )
                # Prefer step import into a doc when step_path present
                doc_id = ml.get("doc_id")
                step_path = ml.get("step_path")
                if not doc_id and step_path and _Path(str(step_path)).is_file():
                    re_res = _call_reverse_engineer(
                        str(step_path),
                        name=s.get("name"),
                        hints=hints if isinstance(hints, dict) else None,
                    )
                    if re_res.get("ok") and re_res.get("doc_id"):
                        doc_id = re_res["doc_id"]
                        ml = {**ml, "reverse_engineer": re_res}
                s = _load_session(session_id, merge_gaussian=False) or s
                s.setdefault("artifacts", {})
                if doc_id:
                    s["artifacts"]["doc_id"] = doc_id
                if step_path:
                    s["artifacts"]["step_path"] = step_path
                if ml.get("cadquery_src"):
                    cq_path = _session_path(session_id) / "cad_recode.py"
                    try:
                        cq_path.write_text(str(ml["cadquery_src"]))
                        s["artifacts"]["cadquery_path"] = str(cq_path)
                    except Exception:  # noqa: BLE001
                        pass
                s["stages"]["model"] = "done" if doc_id or step_path else "failed"
                s["provenance"].append({
                    "step": "n_parametric",
                    "strategy": "ml",
                    "ts": _now_iso(),
                })
                _save_session(s)
                if not (doc_id or step_path or ml.get("cadquery_src")):
                    return _Result(
                        False,
                        ml.get("error") or "ml parametric produced no artifact",
                        ml,
                    )
                return _Result(True, "ml parametric complete", ml)

            # Default: deterministic reverse_engineer with fit hints
            re_res = _call_reverse_engineer(
                mesh_path,
                name=s.get("name"),
                hints=hints if isinstance(hints, dict) else None,
            )
            if not re_res.get("ok"):
                return _Result(
                    False,
                    re_res.get("error")
                    or re_res.get("reason")
                    or "reverse_engineer failed",
                    re_res,
                )
            doc_id = re_res.get("doc_id")
            s = _load_session(session_id, merge_gaussian=False) or s
            s.setdefault("artifacts", {})
            if doc_id:
                s["artifacts"]["doc_id"] = doc_id
            s["stages"]["model"] = "done" if doc_id else "failed"
            s["provenance"].append({
                "step": "n_parametric",
                "strategy": "deterministic",
                "match_pct": re_res.get("match_pct"),
                "ts": _now_iso(),
            })
            _save_session(s)
            return _Result(True, "parametric complete", re_res)

        if node_id in ("n_ingest", "n_mpn_fastpath", "n_datasheet_read"):
            # Datasheet pipeline nodes (RE-15 datasheet_ingest).
            try:
                import services.vision.datasheet_ingest  # noqa: F401
                return _Result(True, "datasheet stage complete")
            except Exception:  # noqa: BLE001
                return _Result(False, "backend_not_provisioned: docling absent")
        if node_id in ("n_build", "n_fix_dims"):
            return _Result(False, "backend_not_provisioned: parametric build absent")
        if node_id in ("n_deliver",):
            return _Result(True, "result attached")

        # Unrecognised node — succeed as a pass-through so the walk advances.
        return _Result(True, f"pass-through node {node_id!r}")

    return _agent_fn



# ── Mission run bookkeeping: async launch, gate pause/resume ─────────────────
#
# contracts/reveng.md §0 promises a long job returns a run_id immediately. The scan
# mission is minutes of COLMAP/OpenMVS work and services/worker_main.py is a SINGLE
# threaded request loop, so running it inline blocks every other RPC — including the
# gate-status polls that exist to unblock the very run being polled. Missions therefore
# run on a daemon worker thread and everything a later call needs is read back from
# disk, never from memory:
#
#   session.json["run"]           — progress, the source of truth the GUI/session tools read
#   <runs>/<run_id>.reveng.json   — launch parameters resume() needs to rebuild the emit +
#                                   agent_fn seams, plus the pending gate description
#
# The executor already persists the topology (<run_id>.graph.json) and the LangGraph
# checkpoint (<run_id>.db) beside them, so resuming needs nothing that died with the
# process. The in-process thread table below is only used to refuse a SECOND concurrent
# mission on one session — it is authoritative for "running right now" in a way a stale
# manifest can never be.

_MISSION_THREADS: dict[str, tuple[str, Any]] = {}
_MISSION_LOCK = _threading.Lock()


def worker_activity() -> dict:
    from services.worker_activity import registry_lock

    with registry_lock(_MISSION_LOCK):
        # Keys are opaque scoped registry keys, not handles to resolve on disk.
        active = sum(_slot_busy(entry) for key, entry in _MISSION_THREADS.items()
                     if _rc.owns_registry_key(key))
    return {"by_kind": {"reveng_missions": active}}


def _runs_base_dir() -> _Path:
    """Directory holding <run_id>.db / .graph.json / .reveng.json for reveng runs."""
    override = _reveng_runs_dir()
    if override is not None:
        return override
    try:
        from services.dag.executor import default_runs_dir  # noqa: PLC0415
        return default_runs_dir()
    except Exception:  # noqa: BLE001 — executor pulls langgraph in; keep bookkeeping alive
        if _platform.system() == "Windows":
            appdata = _os.environ.get("APPDATA") or _os.path.expanduser(
                r"~\AppData\Roaming"
            )
            return _Path(appdata) / "Chudware" / "runs"
        xdg = _os.environ.get("XDG_DATA_HOME") or _os.path.expanduser("~/.local/share")
        return _Path(xdg) / "Chudware" / "runs"


def _run_record_path(run_id: str) -> _Path:
    _validate_handle(run_id)
    if _rc.in_cell():
        from services.dag.storage import resolve_run
        return resolve_run(run_id).root / "reveng.json"
    return _runs_base_dir() / f"{run_id}.reveng.json"


def _write_run_record(rec: dict) -> None:
    """Persist the aicad.reveng_run/1 sidecar (best-effort; never breaks a run)."""
    _require_record_owner(rec)
    try:
        path = _run_record_path(rec["run_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(rec, indent=2))
    except Exception:  # noqa: BLE001
        if _rc.in_cell():
            raise
        return


def _read_run_record(run_id: str) -> Optional[dict]:
    try:
        path = _run_record_path(run_id)
        rec = _json.loads(path.read_text())
        if not isinstance(rec, dict):
            raise KeyError("not_found")
        _require_record_owner(rec)
        return rec
    except (OSError, ValueError, KeyError, TypeError):
        if _rc.in_cell():
            raise KeyError("not_found") from None
        return None


def _run_summary(rec: dict) -> dict:
    """The subset of the run record that belongs on the session manifest."""
    return {
        "run_id": rec.get("run_id"),
        "template": rec.get("template"),
        "status": rec.get("status"),
        "gate": rec.get("gate"),
        "started_at": rec.get("started_at"),
        "updated_at": rec.get("updated_at"),
        "error": rec.get("error"),
    }


def _record_session_run(rec: dict) -> None:
    """Mirror the run record onto session.json — the progress source of truth."""
    try:
        s = _load_session(rec.get("session_id") or "", merge_gaussian=False)
        if s is None:
            return
        s["run"] = _run_summary(rec)
        _save_session(s)
    except Exception:  # noqa: BLE001
        return


def _gate_from_result(graph: Optional[dict], result: dict) -> Optional[dict]:
    """Describe the gate a paused executor result is suspended on, or None."""
    payloads = [p for p in (result.get("interrupted") or []) if isinstance(p, dict)]
    if not payloads:
        return None
    nodes = {n.get("id"): n for n in ((graph or {}).get("nodes") or [])}
    payload = payloads[0]
    node_id = payload.get("node_id") or ""
    node = nodes.get(node_id) or {}
    question = (payload.get("questions") or [{}])[0]
    return {
        "node_id": node_id,
        "node_name": node.get("name") or node_id,
        "gate_kind": (node.get("inputs") or {}).get("gate_kind") or "",
        "autonomy": payload.get("autonomy") or node.get("autonomy") or "approve",
        "prompt": question.get("prompt") or "",
        "proposal": question.get("proposal") or "",
    }


def _apply_run_result(rec: dict, result: dict, graph: Optional[dict]) -> dict:
    """Fold an executor result into the run record + session manifest."""
    rec["status"] = result.get("status") or "running"
    rec["gate"] = _gate_from_result(graph, result)
    rec["updated_at"] = _now_iso()
    if result.get("error"):
        rec["error"] = result["error"]
    else:
        rec.pop("error", None)
    _write_run_record(rec)
    _record_session_run(rec)
    return rec


def _slot_busy(entry: Optional[tuple]) -> bool:
    """True while a session's mission slot is claimed (reserved or thread alive)."""
    if entry is None:
        return False
    thread = entry[1]
    return thread is None or getattr(thread, "ident", 0) is None or thread.is_alive()


def _mission_running(session_id: str) -> Optional[str]:
    """The run_id of the mission in flight for *session_id*, else None."""
    if _rc.in_cell():
        _load_session(session_id, merge_gaussian=False)
    session_id = _rc.scoped_key(session_id)
    with _MISSION_LOCK:
        entry = _MISSION_THREADS.get(session_id)
        if _slot_busy(entry):
            return entry[0]
        if entry is not None:
            _MISSION_THREADS.pop(session_id, None)
    return None


def _reserve_mission(session_id: str, run_id: str) -> Optional[str]:
    """Atomically claim the session's mission slot; return the busy run_id on refusal."""
    with _MISSION_LOCK:
        if _rc.in_cell():
            _load_session(session_id, merge_gaussian=False)
        key = _rc.scoped_key(session_id)
        entry = _MISSION_THREADS.get(key)
        if _slot_busy(entry):
            return entry[0]
        from services.reveng.gaussian_jobs import active_run
        splat_run = active_run(_session_path(session_id))
        if splat_run:
            return splat_run
        _MISSION_THREADS[key] = (run_id, None)
    return None


def _release_mission(session_id: str, run_id: str) -> None:
    with _MISSION_LOCK:
        _drop_slot(session_id, run_id)


def _drop_slot(session_id: str, run_id: str) -> None:
    """Free *session_id*'s mission slot if *run_id* still owns it. Caller holds the lock."""
    session_id = _rc.scoped_key(session_id)
    entry = _MISSION_THREADS.get(session_id)
    if entry is not None and entry[0] == run_id:
        _MISSION_THREADS.pop(session_id, None)


def _busy_envelope(session_id: str, run_id: str) -> dict:
    return {
        "ok": False,
        "reason": "mission_already_running",
        "hint": f"session {session_id!r} already has mission {run_id} in flight — "
                "poll reveng_gate_status() for its stage/gate, or wait for it to finish",
        "session_id": session_id,
        "run_id": run_id,
    }


def _spawn_mission(session_id: str, run_id: str, invoke, rec: dict, graph: dict) -> None:
    """Run *invoke* on a daemon thread, folding its result into the run record."""
    ctx = _rc.current_context()
    project = _rc.root_value("CHUDWARE_PROJECT_DIR")
    save = _rc.root_value("CHUDWARE_PROJECT_SAVE_DIR")
    def _body() -> None:
        try:
            result = invoke()
        except Exception as exc:  # noqa: BLE001 — a crashed mission must still be reported
            result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        # Publishing the result and dropping the slot are ONE critical section: the
        # instant a reader can see status "paused" it must be able to resolve that
        # gate. Published-then-released, the two are a window in which the run record
        # says "waiting for a human" while _reserve_mission still answers
        # mission_already_running — so the GUI's (and the test's) poll-then-approve
        # gets refused for a mission that is doing nothing but waiting for it. Every
        # slot reader takes _MISSION_LOCK, so a resolve that arrives mid-publish now
        # blocks on the lock instead of reading a stale claim.
        with _MISSION_LOCK:
            try:
                _apply_run_result(rec, result, graph)
            finally:
                _drop_slot(session_id, run_id)

    def _bound_body() -> None:
        with _rc.bind_context(ctx), _rc.bind_roots(project, save):
            _body()

    thread = _threading.Thread(target=_bound_body, name=f"reveng-{run_id}", daemon=True)
    with _MISSION_LOCK:
        _MISSION_THREADS[_rc.scoped_key(session_id)] = (run_id, thread)
    try:
        from services.worker_activity import start_registered_thread

        start_registered_thread(thread)
    except BaseException:
        with _MISSION_LOCK:
            _drop_slot(session_id, run_id)
        raise


def _launch_reveng_mission(
    session_id: str,
    template_name: str,
    stage_map: dict,
    *,
    autonomy_default: str = "auto",
    strategy: str = "deterministic",
    background: bool = False,
) -> dict:
    """Load a frozen template, start it via the executor, translate progress.

    Returns the executor result dict (with run_id/status/nodes/interrupted/…) plus
    ``session_id`` and ``template``. Runs at ``autonomy_default="auto"`` so only the
    template's own ``approve`` nodes pause the run — the scale-confirm and
    dimension-confirm gates (declared ``autonomy: "approve"``) PAUSE for human
    approval while the auto sparse/dense/fit/parametric/verify nodes execute
    without interruption; the GUI/human resumes each gate via
    ``_resume_reveng_mission`` (the ``reveng_resolve_gate`` tool).
    (Promoting to ``autonomy_default="approve"`` would gate *every* auto node,
    pausing immediately at ``n_capture`` — not what the template intends.)

    ``background=True`` returns as soon as the run_id exists and finishes the mission
    on a daemon thread; the caller then reads progress from the session manifest and
    the run's ``aicad.reveng_events/1`` journal. A second background mission on a
    session that already has one in flight is REFUSED with an honest envelope rather
    than racing it for session.json.

    strategy is forwarded to the pipeline agent_fn for n_parametric
    (``deterministic`` | ``ml``).
    """
    from services.dag import executor

    graph = _load_template(template_name)
    run_id = "run_" + _secrets.token_hex(4)
    if _rc.in_cell():
        _load_session(session_id, merge_gaussian=False)
        from services.dag.storage import create_run
        run_id = create_run(handle=run_id, autonomy=autonomy_default).run_id
    rec = {
        "schema": "aicad.reveng_run/1",
        "run_id": run_id,
        "_owner": _rc.owner_key(),
        "session_id": session_id,
        "template": template_name,
        "strategy": strategy,
        "autonomy_default": autonomy_default,
        "stage_map": {k: list(v) for k, v in (stage_map or {}).items()},
        "status": "running",
        "gate": None,
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
    }

    # Build the emitters on the CALLING thread so the run's events.jsonl exists the
    # moment reveng_reconstruct hands back the run_id: the GUI tails that file to draw
    # progress, and a background mission would otherwise leave a window where the
    # advertised run_id addresses nothing.
    emit = make_reveng_emit(run_id, session_id, stage_map)
    progress = make_reveng_stage_emitter(run_id, session_id)
    agent_fn = _make_pipeline_agent_fn(
        session_id, strategy=strategy, progress=progress,
    )

    def _invoke() -> dict:
        return executor.run(
            graph,
            initial_state={"inputs": {"session_id": session_id, "strategy": strategy}},
            run_id=run_id,
            runs_dir=_reveng_runs_dir(),
            emit=emit,
            agent_fn=agent_fn,
            autonomy_default=autonomy_default,
        )

    if not background:
        _write_run_record(rec)
        _record_session_run(rec)
        result = _invoke()
        _apply_run_result(rec, result, graph)
        result["session_id"] = session_id
        result["template"] = template_name
        result["strategy"] = strategy
        return result

    busy = _reserve_mission(session_id, run_id)
    if busy:
        return _busy_envelope(session_id, busy)
    _write_run_record(rec)
    _record_session_run(rec)
    progress("capture", 0, f"mission {template_name} queued")
    _spawn_mission(session_id, run_id, _invoke, rec, graph)
    return {
        "run_id": run_id,
        "name": graph.get("name", ""),
        "status": "running",
        "async": True,
        "session_id": session_id,
        "template": template_name,
        "strategy": strategy,
    }


def _find_session_run(session_id: str) -> Optional[dict]:
    """The run record for *session_id*'s current mission, or None.

    Reads the session manifest's ``run`` pointer first (source of truth), then falls
    back to scanning the runs dir so a mission launched before the pointer existed is
    still addressable.
    """
    s = _load_session(session_id, merge_gaussian=False)
    run_id = ((s or {}).get("run") or {}).get("run_id")
    if run_id:
        rec = _read_run_record(str(run_id))
        if rec is not None:
            return rec
    if _rc.in_cell():
        return None
    base = _runs_base_dir()
    if not base.is_dir():
        return None
    newest: Optional[tuple[float, dict]] = None
    for path in base.glob("*.reveng.json"):
        try:
            rec = _json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(rec, dict) or rec.get("session_id") != session_id:
            continue
        mtime = path.stat().st_mtime
        if newest is None or mtime > newest[0]:
            newest = (mtime, rec)
    return newest[1] if newest else None


def _resume_reveng_mission(
    run_id: str,
    decision: Any = "approve",
    *,
    background: bool = True,
) -> dict:
    """Resume a gate-paused reveng mission with a human decision.

    Rebuilds the emit + agent_fn seams from the run record and calls
    ``executor.resume``, which feeds the decision back into the suspended
    ``interrupt()`` and continues from the gate.

    Only ``approve`` is ever handed to the executor. A REJECT is honoured by NOT
    resuming: graph_loader's reject branch returns a plain state patch, so LangGraph
    would happily walk on to the next node with the gate merely marked paused — which
    for a scale-confirm gate would mean dense reconstruction proceeding on scale a
    human just refused. Leaving the checkpoint suspended is what actually stops the
    mission, and it stays resumable if the rejection was a mistake.
    """
    rec = _read_run_record(run_id)
    if rec is None:
        return {
            "ok": False,
            "reason": "run_not_found",
            "hint": f"no run record for {run_id!r}; call reveng_gate_status(session_id) "
                    "for the session's current mission",
        }
    session_id = rec.get("session_id") or ""
    if rec.get("status") not in ("paused", "rejected"):
        return {
            "ok": False,
            "reason": f"run_not_paused: {rec.get('status')}",
            "hint": "only a mission suspended at an approve gate can be resumed",
            "run_id": run_id,
            "session_id": session_id,
        }

    from services.dag import executor

    graph = _load_template(rec.get("template") or "reveng_scan")
    stage_map = {
        k: (tuple(v) if isinstance(v, (list, tuple)) else v)
        for k, v in (rec.get("stage_map") or {}).items()
    }
    strategy = rec.get("strategy") or "deterministic"

    # Same as the launch path: emitters on the calling thread so the resumed run keeps
    # writing to the run's existing journal from the instant this call returns.
    emit = make_reveng_emit(run_id, session_id, stage_map)
    agent_fn = _make_pipeline_agent_fn(
        session_id,
        strategy=strategy,
        progress=make_reveng_stage_emitter(run_id, session_id),
    )

    def _invoke() -> dict:
        return executor.resume(
            run_id,
            graph_json=graph,
            decision=decision,
            runs_dir=_reveng_runs_dir(),
            emit=emit,
            agent_fn=agent_fn,
            autonomy_default=rec.get("autonomy_default") or "auto",
        )

    if not background:
        result = _invoke()
        _apply_run_result(rec, result, graph)
        result["session_id"] = session_id
        result["template"] = rec.get("template")
        return result

    busy = _reserve_mission(session_id, run_id)
    if busy:
        return _busy_envelope(session_id, busy)
    rec["status"] = "running"
    rec["gate"] = None
    rec["updated_at"] = _now_iso()
    _write_run_record(rec)
    _record_session_run(rec)
    _spawn_mission(session_id, run_id, _invoke, rec, graph)
    return {
        "run_id": run_id,
        "status": "running",
        "async": True,
        "session_id": session_id,
        "template": rec.get("template"),
    }


def _reject_reveng_gate(run_id: str, note: str = "") -> dict:
    """Stop a gate-paused mission because the human rejected the gate.

    The LangGraph checkpoint is left suspended at the interrupt — that is what makes
    the stop real: nothing downstream of the gate runs. The record says why, and a
    later ``approve`` can still resume the same checkpoint.
    """
    rec = _read_run_record(run_id)
    if rec is None:
        return {"ok": False, "reason": "run_not_found",
                "hint": f"no run record for {run_id!r}"}
    if rec.get("status") != "paused":
        return {
            "ok": False,
            "reason": f"run_not_paused: {rec.get('status')}",
            "hint": "only a mission suspended at an approve gate can be rejected",
            "run_id": run_id,
            "session_id": rec.get("session_id"),
        }
    gate = rec.get("gate") or {}
    rec["status"] = "rejected"
    rec["updated_at"] = _now_iso()
    rec["error"] = (
        f"gate {gate.get('gate_kind') or gate.get('node_id') or 'unknown'} rejected"
        + (f": {note}" if note else "")
    )
    _write_run_record(rec)
    _record_session_run(rec)
    return {
        "ok": True,
        "run_id": run_id,
        "session_id": rec.get("session_id"),
        "status": "rejected",
        "gate": gate,
        "reason": rec["error"],
        "hint": "the mission is stopped at the gate and nothing downstream ran; "
                "fix the input and call reveng_resolve_gate(decision='approve') to "
                "continue from the same checkpoint",
    }
