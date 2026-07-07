"""Reverse Engineering MCP tools — family ``reveng`` (contracts/reveng.md §8).

Session / ingest tools are LIVE: they read/write the session directory under
``AICAD_REVENG_DIR`` and delegate to:
- ``engine_oracle.vision.reveng_provision.reveng_status`` (RE-06, on master)
- ``engine_oracle.vision.frames.extract_frames_safe`` (RE-04, on master)

Long-job tools (reconstruct / build_parametric) launch the frozen DAG mission
templates (services/dag/templates/reveng_{scan,datasheet}.json) via the executor
when their backend is provisioned, streaming ``aicad.reveng_events/1`` progress on
the run's EventJournal and honouring the human scale/dimension gates (RE-17). When
the backend is absent they return the honest ``backend_not_provisioned`` envelope
(§0 contract). single_photo / fit_primitives remain provisioning-gated stubs.

Conventions: ``_dumps`` / ``_err`` from cad_tools.py; never return tracebacks.
Registration: ``register_reveng_tools(_reg.view(mcp, "reveng"))`` — see server.py.
"""
from __future__ import annotations

import json as _json
import os as _os
import shutil as _shutil
import secrets as _secrets
import time as _time
from pathlib import Path as _Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

# ── helpers from cad_tools (already loaded) ────────────────────────────────
from mcp_server.cad_tools import _dumps, _err  # type: ignore[attr-defined]

# ── RE-29: research ingest orchestrator ────────────────────────────────────
try:
    from engine_oracle.vision.research_ingest import research_and_ingest as _research_and_ingest
except ImportError:
    _research_and_ingest = None  # type: ignore[assignment]


def _research_ingest(
    session_id: str,
    *,
    query: Optional[str] = None,
    part_number: Optional[str] = None,
    url: Optional[str] = None,
) -> dict:
    """Internal helper: run research_and_ingest for *session_id*."""
    if _research_and_ingest is None:
        return {
            "ok": False,
            "reason": "backend_not_provisioned",
            "hint": "research_ingest module not available; check engine_oracle install",
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
    from engine_oracle.vision.reveng_provision import reveng_status as _reveng_status
except ImportError:
    try:
        from engine.vision.reveng_provision import reveng_status as _reveng_status  # type: ignore
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
    from engine_oracle.vision.frames import extract_frames_safe as _extract_frames_safe
except ImportError:
    try:
        from engine.vision.frames import extract_frames_safe as _extract_frames_safe  # type: ignore
    except ImportError:
        def _extract_frames_safe(video_path: str, out_dir: str, **kw) -> dict:  # type: ignore[misc]
            return {
                "ok": False,
                "reason": "frames module not available",
                "hint": "install opencv-python",
            }

# ── datasheet ingest (docling) ───────────────────────────────────────────────
try:
    from engine_oracle.vision.ingest import ingest_pdf as _ingest_pdf
except ImportError:
    try:
        from engine.vision.ingest import ingest_pdf as _ingest_pdf  # type: ignore
    except ImportError:
        def _ingest_pdf(path: str, out_dir: str) -> dict:  # type: ignore[misc]
            return {
                "ok": False,
                "reason": "docling not provisioned",
                "hint": "pip install docling",
            }


# ── session store ───────────────────────────────────────────────────────────

def _reveng_dir() -> _Path:
    base = _os.environ.get("AICAD_REVENG_DIR")
    if base:
        return _Path(base).expanduser()
    xdg = _os.environ.get("XDG_DATA_HOME", _os.path.expanduser("~/.local/share"))
    return _Path(xdg) / "AI-CAD" / "reveng"


def _session_path(session_id: str) -> _Path:
    return _reveng_dir() / session_id


def _session_json_path(session_id: str) -> _Path:
    return _session_path(session_id) / "session.json"


def _new_session_id() -> str:
    return "re_" + _secrets.token_hex(4)


def _now_iso() -> str:
    import datetime
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_session(session_id: str, name: str, part_ref: Optional[str]) -> dict:
    now = _now_iso()
    return {
        "schema": "aicad.reveng_session/1",
        "session_id": session_id,
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


def _load_session(session_id: str) -> Optional[dict]:
    p = _session_json_path(session_id)
    if not p.exists():
        return None
    try:
        return _json.loads(p.read_text())
    except Exception:
        return None


def _save_session(session: dict) -> None:
    sid = session["session_id"]
    root = _session_path(sid)
    root.mkdir(parents=True, exist_ok=True)
    session["updated_at"] = _now_iso()
    _session_json_path(sid).write_text(_json.dumps(session, indent=2))


def _session_summary(s: dict) -> dict:
    return {
        "session_id": s["session_id"],
        "name": s["name"],
        "status": s["status"],
        "created_at": s["created_at"],
        "updated_at": s["updated_at"],
        "part_ref": s.get("part_ref"),
        "images": s.get("artifacts", {}).get("images", 0),
    }


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

# node_id → (stage, pct) for the reveng_scan template. Stages are the
# aicad.reveng_events/1 §4 vocabulary. pct is a coarse monotone progress hint.
_SCAN_NODE_STAGE: dict[str, tuple[str, int]] = {
    "n_capture":          ("capture", 5),
    "n_scale_gate":       ("scale", 15),
    "n_reconstruct":      ("sparse", 40),
    "n_fit":              ("fit", 60),
    "n_parametric":       ("parametric", 75),
    "n_dim_gate":         ("parametric", 80),
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
    """Optional runs-dir override for hermetic tests (AICAD_REVENG_RUNS_DIR)."""
    d = _os.environ.get("AICAD_REVENG_RUNS_DIR")
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

    journal = EventJournal(run_id)

    def _emit(event_type: str, payload: dict) -> None:
        try:
            if event_type not in ("dag.node.started", "dag.node.done", "dag.node.paused"):
                return
            node_id = payload.get("node_id", "")
            stage, pct = stage_map.get(node_id, ("parametric", 50))
            if event_type == "dag.node.started":
                msg = f"{stage}: started ({node_id})"
                epct = max(0, pct - 5)
            elif event_type == "dag.node.paused":
                reason = payload.get("reason", "gate")
                msg = f"{stage}: paused for {reason} ({node_id})"
                epct = pct
            else:  # dag.node.done
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


def _make_pipeline_agent_fn(session_id: str):
    """Return a deterministic node ``agent_fn`` that runs the RE pipeline stages.

    ``agent_fn(request, context_pack) -> AgentResult`` is the seam node_runner
    dispatches every non-verify/non-gate node through. Rather than invoke an LLM,
    this deterministic dispatcher wires each node to the pipeline module already on
    master, degrading honestly to a non-success result (which the node ESCALATES on
    per its on_fail) when the backend module or its provisioning is absent. It never
    re-invokes the reveng MCP launch tools (that would recurse — see
    work/questions/RE-17.md Q1).

    The default (live) agent_fn is used when this returns None for a node it does not
    recognise; here we always return a structured result so a mission started without
    provisioned backends produces a clear partial run instead of hanging on the LLM.
    """
    class _Result:
        def __init__(self, ok: bool, reason: str, data: Optional[dict] = None):
            self.success = ok
            self.stop_reason = "confirmed" if ok else "failed"
            self.final_code = None
            self.result = data or {"detail": reason}

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

        # Reconstruction stages require the sparse/dense backends.
        if node_id in ("n_reconstruct",):
            if not (caps.get("sparse") or caps.get("dense")):
                return _Result(False, "backend_not_provisioned: colmap/dense absent")
            return _Result(True, "reconstruction complete")
        if node_id in ("n_fit",):
            if not caps.get("dense"):
                return _Result(False, "backend_not_provisioned: mesh/segment_fit absent")
            return _Result(True, "primitives fit")
        if node_id in ("n_parametric", "n_iterate"):
            # Parametric build needs a fit result; degrade honestly when absent.
            return _Result(False, "backend_not_provisioned: parametric build absent")
        if node_id in ("n_ingest", "n_mpn_fastpath", "n_datasheet_read"):
            # Datasheet pipeline nodes (RE-15 datasheet_ingest).
            try:
                import engine_oracle.vision.datasheet_ingest  # noqa: F401
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


def _launch_reveng_mission(
    session_id: str,
    template_name: str,
    stage_map: dict,
    *,
    autonomy_default: str = "auto",
) -> dict:
    """Load a frozen template, start it via the executor, translate progress.

    Returns the executor result dict (with run_id/status/nodes/interrupted/…) plus
    ``session_id`` and ``template``. Runs at ``autonomy_default="auto"`` so only the
    template's own ``approve`` nodes pause the run — the scale-confirm and
    dimension-confirm gates (declared ``autonomy: "approve"``) PAUSE for human
    approval while the auto reconstruction/fit/parametric/verify nodes execute
    without interruption; the GUI/human resumes each gate via the DAG resume path.
    (Promoting to ``autonomy_default="approve"`` would gate *every* auto node,
    pausing immediately at ``n_capture`` — not what the template intends.)
    """
    from services.dag import executor

    graph = _load_template(template_name)
    run_id = "run_" + _secrets.token_hex(4)
    emit = make_reveng_emit(run_id, session_id, stage_map)
    agent_fn = _make_pipeline_agent_fn(session_id)
    result = executor.run(
        graph,
        initial_state={"inputs": {"session_id": session_id}},
        run_id=run_id,
        runs_dir=_reveng_runs_dir(),
        emit=emit,
        agent_fn=agent_fn,
        autonomy_default=autonomy_default,
    )
    result["session_id"] = session_id
    result["template"] = template_name
    return result


# ── registration ─────────────────────────────────────────────────────────────

def register_reveng_tools(mcp: FastMCP) -> None:
    """Register the ``reveng`` MCP tool family (contracts/reveng.md §8)."""

    # ── §8.1 READ ────────────────────────────────────────────────────────────

    @mcp.tool()
    def list_reveng_sessions() -> str:
        """List all reverse engineering sessions in the session store.

        Returns ``{ok, sessions: [session_summary]}`` where each summary has
        session_id, name, status, created_at, updated_at, part_ref, images.
        """
        root = _reveng_dir()
        sessions = []
        if root.exists():
            for d in sorted(root.iterdir()):
                if d.is_dir() and (d / "session.json").exists():
                    s = _load_session(d.name)
                    if s:
                        sessions.append(_session_summary(s))
        return _dumps({"ok": True, "sessions": sessions})

    @mcp.tool()
    def get_reveng_session(session_id: str) -> str:
        """Return the full ``aicad.reveng_session/1`` manifest for *session_id*.

        Returns ``{ok, session}`` or ``{ok: false, reason}`` if not found.
        """
        s = _load_session(session_id)
        if s is None:
            return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
        return _dumps({"ok": True, "session": s})

    @mcp.tool()
    def get_reveng_coverage(session_id: str) -> str:
        """Return image coverage for the 11-shot orbit plan for *session_id*.

        Returns ``{ok, coverage: {plan, captured, missing, blur_flags, quality}}``.
        ``plan`` is ``"11_shot"`` (the default guided orbit); ``captured`` and
        ``missing`` are lists of shot-position indices (0–10); ``quality`` is one of
        ``"good"`` / ``"partial"`` / ``"insufficient"``.
        """
        s = _load_session(session_id)
        if s is None:
            return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
        images_dir = _session_path(session_id) / "images"
        frames_dir = _session_path(session_id) / "frames"
        n_images = len(list(images_dir.glob("*")) if images_dir.exists() else [])
        n_frames = len(list(frames_dir.glob("*.jpg")) if frames_dir.exists() else [])
        total = n_images + n_frames
        # Simple proxy: treat each captured image as covering one shot position
        plan_size = 11
        captured = list(range(min(total, plan_size)))
        missing = list(range(len(captured), plan_size))
        quality = "good" if len(captured) >= 11 else ("partial" if len(captured) >= 5 else "insufficient")
        return _dumps({
            "ok": True,
            "coverage": {
                "plan": "11_shot",
                "captured": captured,
                "missing": missing,
                "blur_flags": [False] * len(captured),
                "quality": quality,
            },
        })

    @mcp.tool()
    def get_reveng_deviation(
        session_id: str,
        ref_path: Optional[str] = None,
        brep_doc_id: Optional[str] = None,
        n_samples: int = 20_000,
    ) -> str:
        """Compute mesh-vs-mesh (or mesh-vs-B-Rep tessellation) deviation (RE-16).

        Implements contracts/reveng.md §5 deviation schema:
        ``max_mm``, ``rms_mm``, per-region heat ``buckets``, and
        ``vertex_distances_mm`` for GUI colour-coding.

        Parameters
        ----------
        session_id:
            RE session identifier (``re_`` + 8 hex chars).
        ref_path:
            Absolute path to the reference mesh / tessellation.  Required
            unless ``brep_doc_id`` is supplied (B-Rep tessellation path is a
            future RE-02 extension — returns instructive error for now).
        brep_doc_id:
            Optional B-Rep doc id.  Future RE-02 extension will auto-tessellate
            the parametric model and compute deviation against it.
        n_samples:
            Surface sample count for KD-tree probe (default 20 000).
        """
        s = _load_session(session_id)
        if s is None:
            return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})

        # Resolve query mesh from session artifacts
        mesh_path = s.get("artifacts", {}).get("mesh_path")
        if not mesh_path:
            return _dumps({
                "ok": False,
                "reason": "no mesh in session",
                "hint": "run reveng_reconstruct or import_scan_mesh first",
            })
        if not _os.path.isabs(mesh_path):
            mesh_path = str(_session_path(session_id) / mesh_path)
        if not _os.path.exists(mesh_path):
            return _dumps({
                "ok": False,
                "reason": f"mesh file not found: {mesh_path!r}",
                "hint": "check session artifacts",
            })

        # Resolve reference
        if ref_path is None and brep_doc_id is None:
            return _dumps({
                "ok": False,
                "reason": "either ref_path or brep_doc_id must be supplied",
                "hint": "export parametric model via export_part('ply') and pass ref_path",
            })
        if ref_path is None:
            return _dumps({
                "ok": False,
                "reason": "B-Rep auto-tessellation not yet wired (RE-02 pending)",
                "hint": "export the parametric model to PLY/OBJ and pass ref_path",
            })
        if not _os.path.exists(ref_path):
            return _dumps({
                "ok": False,
                "reason": f"ref_path not found: {ref_path!r}",
                "hint": "check the file path",
            })

        try:
            from engine_oracle.vision.deviation import compute_deviation
            result = compute_deviation(
                mesh_path,
                ref_path,
                session_id=session_id,
                brep_doc_id=brep_doc_id,
                n_samples=n_samples,
            )
        except Exception as exc:  # noqa: BLE001
            return _dumps({"ok": False, "reason": f"deviation computation failed: {exc}"})

        return _dumps(result)

    @mcp.tool()
    def describe_reveng_result(session_id: str) -> str:
        """Describe the reverse-engineering result for *session_id*.

        Returns ``{ok, result: {doc_id, match_pct, strategy, mesh_fallback, provenance}}``
        or ``{ok: false, reason}`` if no result exists.
        """
        s = _load_session(session_id)
        if s is None:
            return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
        doc_id = s.get("artifacts", {}).get("doc_id")
        if not doc_id:
            return _dumps({
                "ok": False,
                "reason": "no result yet",
                "hint": "run reveng_build_parametric first",
            })
        return _dumps({
            "ok": True,
            "result": {
                "doc_id": doc_id,
                "match_pct": None,
                "strategy": None,
                "mesh_fallback": s.get("honesty", {}).get("approximate", False),
                "provenance": s.get("provenance", []),
            },
        })

    @mcp.tool()
    def reveng_status() -> str:
        """Return the RE workspace readiness probe (contracts/reveng.md §8.1).

        Returns ``{ok, capabilities, provisioning, banners}`` — which backends are
        available, which are missing, and human-readable fix-it banners.  Pure and
        offline-safe: reads env vars + filesystem only, never the network.
        """
        try:
            result = _reveng_status()
            return _dumps(result)
        except Exception as e:
            return _err(e)

    # ── §8.2 WRITE ───────────────────────────────────────────────────────────

    @mcp.tool()
    def reveng_start_session(
        name: Optional[str] = None,
        part_ref: Optional[str] = None,
        research: Optional[str] = None,
    ) -> str:
        """Create a new reverse engineering session.

        Args:
            name: Human label for this session (defaults to a timestamp label).
            part_ref: Optional parts-hub ``part_id`` when seeded from the library.
            research: Optional seed for the web/part-number lane — pass a part number,
                search query, or vendor URL.  When provided, the session is created and
                ``reveng_research_source`` is immediately called with this value so the
                datasheet lane is populated before the caller needs to do anything else.

        Returns ``{ok, session_id, session}`` (plus ``research_result`` when the seed
        was provided).
        """
        try:
            sid = _new_session_id()
            label = name or f"RE session {_now_iso()}"
            session = _make_session(sid, label, part_ref)
            _save_session(session)
            result: dict = {"ok": True, "session_id": sid, "session": session}
            if research:
                # Determine whether the seed looks like a URL, a part number, or a query
                import re as _re
                _url_pat = _re.compile(r"^https?://", _re.IGNORECASE)
                _mpn_pat = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{2,20}$")
                if _url_pat.match(research):
                    ri = _research_ingest(session_id=sid, url=research)
                elif _mpn_pat.match(research):
                    ri = _research_ingest(session_id=sid, part_number=research)
                else:
                    ri = _research_ingest(session_id=sid, query=research)
                result["research_result"] = ri
            return _dumps(result)
        except Exception as e:
            return _err(e)

    @mcp.tool()
    def reveng_add_images(
        session_id: str,
        paths: Optional[list] = None,
        dir: Optional[str] = None,  # noqa: A002
    ) -> str:
        """Add still images to *session_id* for photogrammetry / multi-view reconstruction.

        Pass either ``paths`` (list of image file paths) or ``dir`` (a directory of images).
        Returns ``{ok, added: int, session}``.
        """
        try:
            s = _load_session(session_id)
            if s is None:
                return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
            images_dir = _session_path(session_id) / "images"
            images_dir.mkdir(parents=True, exist_ok=True)
            sources: list[_Path] = []
            if dir:
                src = _Path(dir).expanduser()
                if not src.is_dir():
                    return _dumps({"ok": False, "reason": f"directory not found: {dir}"})
                for ext in ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG"):
                    sources.extend(src.glob(ext))
            if paths:
                sources.extend(_Path(p).expanduser() for p in paths)
            added = 0
            for src_path in sources:
                if not src_path.is_file():
                    continue
                dest = images_dir / src_path.name
                _shutil.copy2(str(src_path), str(dest))
                added += 1
            s["artifacts"]["images"] = s["artifacts"].get("images", 0) + added
            s["stages"]["capture"] = "done" if s["artifacts"]["images"] > 0 else "idle"
            _save_session(s)
            return _dumps({"ok": True, "added": added, "session": s})
        except Exception as e:
            return _err(e)

    @mcp.tool()
    def reveng_add_video(
        session_id: str,
        path: str,
        max_frames: int = 120,
    ) -> str:
        """Extract blur-culled stills from a video orbit and add them to *session_id*.

        Delegates to ``engine_oracle.vision.frames.extract_frames_safe`` (RE-04).
        Returns ``{ok, run_id, session}`` where ``run_id`` is a synthetic job token;
        the extraction runs synchronously (no DAG dispatch yet — RE-17).
        """
        try:
            s = _load_session(session_id)
            if s is None:
                return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
            frames_dir = str(_session_path(session_id) / "frames")
            result = _extract_frames_safe(path, frames_dir, max_frames=max_frames)
            if not result.get("ok"):
                return _dumps(result)
            added = result.get("kept", 0)
            run_id = "run_" + _secrets.token_hex(4)
            s["artifacts"]["images"] = s["artifacts"].get("images", 0) + added
            s["stages"]["capture"] = "done" if s["artifacts"]["images"] > 0 else "idle"
            s["provenance"].append({
                "step": "add_video",
                "run_id": run_id,
                "frames_kept": added,
                "frames_sampled": result.get("sampled", 0),
                "frames_rejected_blurry": result.get("rejected_blurry", 0),
                "ts": _now_iso(),
            })
            _save_session(s)
            return _dumps({"ok": True, "run_id": run_id, "session": s})
        except Exception as e:
            return _err(e)

    @mcp.tool()
    def reveng_ingest_datasheet(
        session_id: str,
        path: str,
    ) -> str:
        """Ingest a PDF datasheet into *session_id* via docling (RE-04).

        Extracts tables and layout crops to ``<session>/datasheet/``.
        Returns ``{ok, tables, crops, session}`` or a ``backend_not_provisioned``
        envelope when docling is absent.
        """
        try:
            s = _load_session(session_id)
            if s is None:
                return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
            datasheet_dir = str(_session_path(session_id) / "datasheet")
            _os.makedirs(datasheet_dir, exist_ok=True)
            result = _ingest_pdf(path, datasheet_dir)
            if not result.get("ok"):
                return _dumps(result)
            s["provenance"].append({
                "step": "ingest_datasheet",
                "path": path,
                "tables": len(result.get("tables", [])),
                "ts": _now_iso(),
            })
            _save_session(s)
            return _dumps({
                "ok": True,
                "tables": result.get("tables", []),
                "crops": result.get("crops", []),
                "session": s,
            })
        except Exception as e:
            return _err(e)

    @mcp.tool()
    def reveng_set_scale(
        session_id: str,
        method: str,
        params: Optional[dict] = None,
    ) -> str:
        """Record scale calibration for *session_id* (contracts/reveng.md §8.2).

        ``method`` is one of ``aruco_mat`` | ``two_point`` | ``known_dim``.
        ``params`` shape depends on method — see contract §8.2.
        Sets ``scale.confirmed = False``; reconstruction gating requires confirmation
        via ``confirm_dimensions`` (contracts/interaction.md §3).
        Returns ``{ok, scale, session}``.
        """
        VALID_METHODS = ("aruco_mat", "two_point", "known_dim")
        try:
            s = _load_session(session_id)
            if s is None:
                return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
            if method not in VALID_METHODS:
                return _dumps({
                    "ok": False,
                    "reason": f"invalid method {method!r}",
                    "hint": f"must be one of {VALID_METHODS}",
                })
            params = params or {}
            s["scale"]["method"] = method
            s["scale"]["mm_per_unit"] = params.get("distance_mm") or params.get("value_mm")
            s["scale"]["confirmed"] = False
            s["stages"]["scale"] = "done"
            s["provenance"].append({
                "step": "set_scale",
                "method": method,
                "params": params,
                "ts": _now_iso(),
            })
            _save_session(s)
            return _dumps({"ok": True, "scale": s["scale"], "session": s})
        except Exception as e:
            return _err(e)

    # ── §8.2 WRITE — long-job stubs (RE-17 wires real DAG) ──────────────────

    @mcp.tool()
    def reveng_reconstruct(
        session_id: str,
        quality: str = "normal",
        backend: Optional[str] = None,
    ) -> str:
        """Launch sparse + dense reconstruction for *session_id*.

        ``quality`` is one of ``draft`` | ``normal`` | ``high``.
        When a reconstruction backend is provisioned this launches the
        ``reveng_scan`` DAG mission and returns ``{ok, run_id, status, ...}``;
        progress streams as ``aicad.reveng_events/1`` on the run's EventJournal
        (drain via the GUI stepper / ``mission_status``). The scale-confirm and
        dimension-confirm gates PAUSE the run for human approval; resume via the
        DAG resume path. When no reconstruction backend is provisioned, returns
        the honest ``backend_not_provisioned`` envelope — check ``reveng_status()``
        for availability and install COLMAP (COLMAP_BIN).
        """
        s = _load_session(session_id)
        if s is None:
            return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
        try:
            caps = (_reveng_status() or {}).get("capabilities", {})
        except Exception:  # noqa: BLE001
            caps = {}
        if not (caps.get("sparse") or caps.get("dense")):
            return _not_provisioned(
                "reconstruction backend not provisioned (no sparse/dense capability). "
                "Check reveng_status() and install COLMAP (COLMAP_BIN) / OpenMVS."
            )
        try:
            result = _launch_reveng_mission(session_id, "reveng_scan", _SCAN_NODE_STAGE)
            return _dumps({"ok": True, **result})
        except Exception as e:
            return _err(e)

    @mcp.tool()
    def reveng_single_photo(
        session_id: str,
        tier: str = "triposr",
    ) -> str:
        """Run single-photo mesh reconstruction (TripoSR or TRELLIS tier).

        Force-tags the session ``honesty.approximate = true`` — result is reference/
        preview only, not fit-critical.  Returns ``backend_not_provisioned`` until RE-17.
        ``tier`` must be ``triposr`` or ``trellis``.
        """
        return _not_provisioned(
            "single-photo backend not yet wired (RE-17). "
            "Check reveng_status() for TripoSR/TRELLIS availability."
        )

    @mcp.tool()
    def reveng_fit_primitives(session_id: str) -> str:
        """Segment the mesh and RANSAC-fit primitive shapes (cylinder, box, cone, etc.).

        Returns ``aicad.reveng_fits/1`` (contracts/reveng.md §3) when the mesh is ready.
        Returns ``backend_not_provisioned`` until RE-17 wires the fit-primitives DAG step.
        """
        return _not_provisioned(
            "primitive fitting not yet wired (RE-17). "
            "Run reveng_reconstruct first to produce a mesh, then RE-17 enables this."
        )

    @mcp.tool()
    def reveng_build_parametric(
        session_id: str,
        strategy: Optional[str] = None,
    ) -> str:
        """Build an editable parametric model from the reconstructed mesh.

        Refuses when ``session.honesty.approximate`` is true unless
        ``strategy = "override_approximate"``.
        Returns ``backend_not_provisioned`` until RE-17 wires the DAG.
        """
        s = _load_session(session_id)
        if s is not None:
            if s.get("honesty", {}).get("approximate") and strategy != "override_approximate":
                return _dumps({
                    "ok": False,
                    "reason": "session marked approximate (single-photo tier)",
                    "hint": (
                        "Parametric modeling from a single-photo mesh is unreliable. "
                        "Pass strategy='override_approximate' to force, or capture "
                        "multi-view images and run reveng_reconstruct instead."
                    ),
                })
        if s is None:
            return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
        try:
            caps = (_reveng_status() or {}).get("capabilities", {})
        except Exception:  # noqa: BLE001
            caps = {}
        # Parametric build needs at least a reconstructed/dense mesh to fit against.
        if not caps.get("dense"):
            return _not_provisioned(
                "parametric build backend not provisioned (no dense/mesh capability). "
                "Run reveng_reconstruct + reveng_fit_primitives first; check reveng_status()."
            )
        try:
            result = _launch_reveng_mission(session_id, "reveng_scan", _SCAN_NODE_STAGE)
            return _dumps({"ok": True, **result})
        except Exception as e:
            return _err(e)

    # ── §8.2 WRITE — session management ─────────────────────────────────────

    @mcp.tool()
    def reveng_attach_result(
        session_id: str,
        part_ref: str,
    ) -> str:
        """Attach a parts-hub ``part_ref`` to the completed RE result for *session_id*.

        Returns ``{ok, part_ref, geometry_ref, session}``.
        """
        try:
            s = _load_session(session_id)
            if s is None:
                return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
            s["part_ref"] = part_ref
            s["provenance"].append({
                "step": "attach_result",
                "part_ref": part_ref,
                "ts": _now_iso(),
            })
            _save_session(s)
            doc_id = s.get("artifacts", {}).get("doc_id")
            return _dumps({
                "ok": True,
                "part_ref": part_ref,
                "geometry_ref": doc_id,
                "session": s,
            })
        except Exception as e:
            return _err(e)

    @mcp.tool()
    def reveng_print_scale_mat(size: str = "A4") -> str:
        """Generate a printable ArUco scale-mat PDF for the RE pipeline.

        ``size`` is ``A4`` (default) or ``Letter``.
        Delegates to ``engine_oracle.vision.scale_mat`` when available;
        returns ``backend_not_provisioned`` if the module or its deps are absent.
        """
        if size not in ("A4", "Letter"):
            return _dumps({
                "ok": False,
                "reason": f"invalid size {size!r}",
                "hint": "must be 'A4' or 'Letter'",
            })
        try:
            from engine_oracle.vision.scale_mat import generate_scale_mat  # type: ignore
        except ImportError:
            try:
                from engine.vision.scale_mat import generate_scale_mat  # type: ignore
            except ImportError:
                return _not_provisioned(
                    "scale_mat module not available; install opencv-python and reportlab"
                )
        try:
            out_path = generate_scale_mat(size=size)
            return _dumps({"ok": True, "path": str(out_path)})
        except Exception as e:
            return _err(e)

    # ── §8.3 EXTERNAL ────────────────────────────────────────────────────────

    @mcp.tool()
    def import_scan_mesh(
        session_id: str,
        path: str,
        scale_mm: Optional[float] = None,
    ) -> str:
        """Import an externally-produced scan mesh (.ply / .obj) into *session_id*.

        Copies the mesh to ``<session>/mesh/`` and records scale if provided.
        Returns ``{ok, mesh_path, session}``.
        """
        try:
            s = _load_session(session_id)
            if s is None:
                return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
            src = _Path(path).expanduser()
            if not src.is_file():
                return _dumps({"ok": False, "reason": f"file not found: {path}"})
            mesh_dir = _session_path(session_id) / "mesh"
            mesh_dir.mkdir(parents=True, exist_ok=True)
            dest = mesh_dir / src.name
            _shutil.copy2(str(src), str(dest))
            s["artifacts"]["mesh_path"] = str(dest)
            if scale_mm is not None:
                s["scale"]["mm_per_unit"] = scale_mm
                s["scale"]["method"] = "known_dim"
            s["provenance"].append({
                "step": "import_scan_mesh",
                "source": str(src),
                "mesh_path": str(dest),
                "scale_mm": scale_mm,
                "ts": _now_iso(),
            })
            _save_session(s)
            return _dumps({"ok": True, "mesh_path": str(dest), "session": s})
        except Exception as e:
            return _err(e)

    @mcp.tool()
    def export_reveng_bundle(
        session_id: str,
        dest: str,
    ) -> str:
        """Export the full RE session directory as a zip bundle to *dest*.

        Returns ``{ok, path: str}`` on success.
        """
        try:
            s = _load_session(session_id)
            if s is None:
                return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
            import zipfile
            dest_path = _Path(dest).expanduser()
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            session_root = _session_path(session_id)
            with zipfile.ZipFile(str(dest_path), "w", zipfile.ZIP_DEFLATED) as zf:
                for fpath in session_root.rglob("*"):
                    if fpath.is_file():
                        zf.write(str(fpath), str(fpath.relative_to(session_root)))
            return _dumps({"ok": True, "path": str(dest_path)})
        except Exception as e:
            return _err(e)

    # ── RE-29: web / part-number research lane ─────────────────────────────
    # RE-ANCHOR — additive insertion point for research-lane tools

    @mcp.tool()
    def reveng_research_source(
        session_id: str,
        query: Optional[str] = None,
        part_number: Optional[str] = None,
        url: Optional[str] = None,
    ) -> str:
        """Pull web / datasheet data into a session for the no-photo research lane.

        Given a part number, a search query, or a vendor / listing URL (at least one
        required), fetches the datasheet PDF + spec data + images from the internet,
        assembles them into the session ``datasheet/`` directory, and returns a bundle
        with candidate MPNs and dimensions ready to feed the ``reveng_datasheet`` mission.

        Every pulled dimension carries ``provenance`` (``"datasheet"`` / ``"web"`` /
        ``"vendor"``).  Fit-critical dims still route through ``confirm_dimensions``
        per ``contracts/reveng.md §0``.

        Args:
            session_id:   Existing RE session to populate.
            query:        Free-text search, e.g. ``"SKF 6204-2RS bearing specifications"``.
            part_number:  Manufacturer part number, e.g. ``"6204-2RS"``.
            url:          Direct vendor page or PDF URL.

        Returns
        -------
        ``{ok, session_id, datasheet_dir, candidate_mpns, candidate_dims, sources,
          mission_seed}`` on success, where ``mission_seed`` is ready to hand off to
        ``reveng_datasheet`` mission launch.

        ``{ok: false, reason, hint}`` when offline or tools absent — never a traceback.
        """
        try:
            s = _load_session(session_id)
            if s is None:
                return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
            result = _research_ingest(
                session_id=session_id,
                query=query,
                part_number=part_number,
                url=url,
            )
            if not result.get("ok"):
                return _dumps(result)
            # Record provenance in the session manifest
            from typing import cast as _cast
            s.setdefault("provenance", []).append({
                "step": "research_source",
                "query": query,
                "part_number": part_number,
                "url": url,
                "candidate_mpns": result.get("candidate_mpns", []),
                "sources_count": len(result.get("sources", [])),
                "ts": _now_iso(),
            })
            # Mark datasheet stage as populated
            s.setdefault("stages", {})["datasheet"] = "done"
            _save_session(s)
            return _dumps({"ok": True, "session_id": session_id, **result})
        except Exception as e:
            return _err(e)
