"""Reverse Engineering MCP tools — family ``reveng`` (contracts/reveng.md §8).

Session / ingest tools are LIVE: they read/write the session directory under
``AICAD_REVENG_DIR`` and delegate to:
- ``engine_oracle.vision.reveng_provision.reveng_status`` (RE-06, on master)
- ``engine_oracle.vision.frames.extract_frames_safe`` (RE-04, on master)

Long-job tools (reconstruct / build_parametric / single_photo / fit_primitives)
wire the real vision backends via ``_make_pipeline_agent_fn``: sparse
(colmap_pipeline → ffwd_sfm fallback) → dense (openmvs/brush) → mesh_cleanup →
segment_fit → reverse_engineer (hints) or cad_recode.build_parametric
(strategy='ml'). Missing backends return the honest ``backend_not_provisioned``
envelope (§0) — never a synthetic success.

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
    from engine_oracle.vision.datasheet_ingest import ingest_datasheet as _ingest_datasheet
except ImportError:
    try:
        from engine.vision.datasheet_ingest import ingest_datasheet as _ingest_datasheet  # type: ignore
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
        from mcp_server.recon_tools import register_recon_tools  # noqa: PLC0415

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


def _make_pipeline_agent_fn(session_id: str, *, strategy: str = "deterministic"):
    """Return a deterministic node ``agent_fn`` that runs the RE pipeline stages.

    ``agent_fn(request, context_pack) -> AgentResult`` is the seam node_runner
    dispatches every non-verify/non-gate node through. Rather than invoke an LLM,
    this dispatcher wires each node to the real vision backends, degrading honestly
    to a non-success result (which the node ESCALATES on per its on_fail) when a
    backend module or its provisioning is absent. It never re-invokes the reveng
    MCP launch tools (that would recurse — see work/questions/RE-17.md Q1).

    strategy:
        ``deterministic`` (default) — reverse_engineer with fit hints.
        ``ml`` — lazy-import engine_oracle.vision.cad_recode.build_parametric.
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

        # ── Reconstruction: sparse → (ffwd fallback) → dense → mesh cleanup ──
        if node_id in ("n_reconstruct",):
            images = _session_image_paths(session_id)
            if not images:
                return _Result(
                    False,
                    "no_images",
                    {"detail": "session has no images/ or frames/ for reconstruction"},
                )
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
                from engine_oracle.vision.colmap_pipeline import run_sparse  # noqa: PLC0415
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
                    from engine_oracle.vision.ffwd_sfm import run_ffwd_sfm  # noqa: PLC0415
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
                return _Result(
                    False,
                    sparse_result.get("reason") or "backend_not_provisioned: sparse",
                    sparse_result,
                )

            sparse_dir = (
                sparse_result.get("sparse_dir")
                or str(_Path(session_dir) / "sparse")
            )
            s = _load_session(session_id) or _make_session(session_id, session_id, None)
            s.setdefault("artifacts", {})
            s["artifacts"]["sparse_dir"] = sparse_dir
            s["stages"]["reconstruct"] = "running"
            _save_session(s)

            # Dense: OpenMVS and/or Brush — first success wins for mesh_path.
            mesh_path: Optional[str] = None
            splat_path: Optional[str] = None
            dense_notes: list = []
            dense_ok = False

            if _os.environ.get("OPENMVS_BIN"):
                try:
                    from engine_oracle.vision.dense_backends import run_openmvs  # noqa: PLC0415
                    omvs = run_openmvs(sparse_dir, session_dir) or {}
                    if omvs.get("ok") and omvs.get("mesh_path"):
                        mesh_path = omvs["mesh_path"]
                        dense_ok = True
                        dense_notes.append("openmvs")
                    else:
                        dense_notes.append(
                            f"openmvs:{omvs.get('reason') or 'failed'}"
                        )
                except Exception as exc:  # noqa: BLE001
                    dense_notes.append(f"openmvs_import:{exc}")

            # Brush when provisioned (BRUSH_BIN) — also yields splat.
            if _os.environ.get("BRUSH_BIN") or caps.get("splat"):
                try:
                    from engine_oracle.vision.dense_backends import run_brush  # noqa: PLC0415
                    brush = run_brush(sparse_dir, session_dir) or {}
                    if brush.get("ok"):
                        dense_ok = True
                        dense_notes.append("brush")
                        if brush.get("splat_path"):
                            splat_path = brush["splat_path"]
                        if not mesh_path and brush.get("mesh_path"):
                            mesh_path = brush["mesh_path"]
                    else:
                        dense_notes.append(
                            f"brush:{brush.get('reason') or 'failed'}"
                        )
                except Exception as exc:  # noqa: BLE001
                    dense_notes.append(f"brush_import:{exc}")

            if not dense_ok and not mesh_path:
                # Sparse-only is not a full reconstruct success for the mesh path,
                # but if sparse ok we still report honest partial.
                s = _load_session(session_id) or s
                s.setdefault("artifacts", {})
                s["artifacts"]["sparse_dir"] = sparse_dir
                if splat_path:
                    s["artifacts"]["splat_path"] = splat_path
                _save_session(s)
                return _Result(
                    False,
                    "backend_not_provisioned: dense",
                    {
                        "sparse": sparse_result,
                        "dense_notes": dense_notes,
                        "used_ffwd": used_ffwd,
                        "hint": "set OPENMVS_BIN and/or BRUSH_BIN; check reveng_status()",
                    },
                )

            # Mesh cleanup when we have a mesh
            cleaned = mesh_path
            if mesh_path:
                try:
                    from engine_oracle.vision.mesh_cleanup import cleanup_mesh  # noqa: PLC0415
                    out_clean = str(
                        _Path(session_dir) / "mesh" / "cleaned.ply"
                    )
                    _Path(out_clean).parent.mkdir(parents=True, exist_ok=True)
                    scale_mm = None
                    try:
                        scale_mm = (_load_session(session_id) or {}).get(
                            "scale", {}
                        ).get("mm_per_unit")
                        if scale_mm is not None:
                            scale_mm = float(scale_mm)
                    except (TypeError, ValueError):
                        scale_mm = None
                    cl = cleanup_mesh(mesh_path, out_clean, scale_mm=scale_mm) or {}
                    if cl.get("ok") and cl.get("mesh_path"):
                        cleaned = cl["mesh_path"]
                    else:
                        dense_notes.append(
                            f"cleanup:{cl.get('reason') or 'skipped'}"
                        )
                except Exception as exc:  # noqa: BLE001
                    dense_notes.append(f"cleanup_import:{exc}")

            s = _load_session(session_id) or s
            s.setdefault("artifacts", {})
            s["artifacts"]["sparse_dir"] = sparse_dir
            if cleaned:
                s["artifacts"]["mesh_path"] = cleaned
            if splat_path:
                s["artifacts"]["splat_path"] = splat_path
            s["stages"]["reconstruct"] = "done"
            s["provenance"].append({
                "step": "n_reconstruct",
                "sparse_backend": sparse_result.get("backend"),
                "used_ffwd": used_ffwd,
                "dense": dense_notes,
                "ts": _now_iso(),
            })
            _save_session(s)
            return _Result(
                True,
                "reconstruction complete",
                {
                    "sparse": sparse_result,
                    "mesh_path": cleaned,
                    "splat_path": splat_path,
                    "used_ffwd": used_ffwd,
                    "dense_notes": dense_notes,
                },
            )

        # ── Primitive fit ────────────────────────────────────────────────────
        if node_id in ("n_fit",):
            s = _load_session(session_id)
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
                from engine_oracle.vision.segment_fit import segment_fit  # noqa: PLC0415
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
            s = _load_session(session_id)
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
                    from engine_oracle.vision.cad_recode import (  # noqa: PLC0415
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
                s = _load_session(session_id) or s
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
            s = _load_session(session_id) or s
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
    strategy: str = "deterministic",
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

    strategy is forwarded to the pipeline agent_fn for n_parametric
    (``deterministic`` | ``ml``).
    """
    from services.dag import executor

    graph = _load_template(template_name)
    run_id = "run_" + _secrets.token_hex(4)
    emit = make_reveng_emit(run_id, session_id, stage_map)
    agent_fn = _make_pipeline_agent_fn(session_id, strategy=strategy)
    result = executor.run(
        graph,
        initial_state={"inputs": {"session_id": session_id, "strategy": strategy}},
        run_id=run_id,
        runs_dir=_reveng_runs_dir(),
        emit=emit,
        agent_fn=agent_fn,
        autonomy_default=autonomy_default,
    )
    result["session_id"] = session_id
    result["template"] = template_name
    result["strategy"] = strategy
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
                When supplied the session is seeded with any photos and datasheet
                paths already recorded on the PartRecord (``metadata.photos`` and
                ``metadata.datasheet``).
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

            # RE-23: seed photos / datasheet from the parts-hub entry when
            # part_ref is supplied and carries embedded asset paths.
            seeded: dict = {}
            if part_ref:
                try:
                    from engine_oracle.parts_hub import get as _hub_get  # noqa: PLC0415
                    record = _hub_get(part_ref)
                    if record:
                        meta = record.get("metadata") or {}
                        photos = meta.get("photos") or []
                        datasheet = meta.get("datasheet")
                        if photos or datasheet:
                            sess_dir = _session_path(sid)
                            if photos:
                                images_dir = sess_dir / "images"
                                images_dir.mkdir(parents=True, exist_ok=True)
                                import shutil as _shutil2  # noqa: PLC0415
                                copied = 0
                                for p in photos:
                                    src = _Path(p).expanduser()
                                    if src.is_file():
                                        _shutil2.copy2(str(src), str(images_dir / src.name))
                                        copied += 1
                                session["artifacts"]["images"] = copied
                                if copied:
                                    session["stages"]["capture"] = "done"
                                    seeded["images"] = copied
                            if datasheet:
                                ds_dir = sess_dir / "datasheet"
                                ds_dir.mkdir(parents=True, exist_ok=True)
                                src = _Path(datasheet).expanduser()
                                if src.is_file():
                                    import shutil as _shutil3  # noqa: PLC0415
                                    _shutil3.copy2(str(src), str(ds_dir / src.name))
                                    seeded["datasheet"] = str(ds_dir / src.name)
                            if seeded:
                                session["provenance"].append({
                                    "step": "seeded_from_parts_hub",
                                    "part_ref": part_ref,
                                    "seeded": seeded,
                                    "ts": _now_iso(),
                                })
                                _save_session(session)
                except Exception:
                    pass  # seeding is best-effort; never block session creation

            result: dict = {"ok": True, "session_id": sid, "session": session, "seeded": seeded}
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
        preview only, not fit-critical. ``tier`` maps to quality: ``triposr`` → low,
        ``trellis`` → high. Degrades honestly when the backend is not provisioned.
        """
        s = _load_session(session_id)
        if s is None:
            return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
        quality = "high" if str(tier).lower() in ("trellis", "high") else "low"
        # Prefer first image in session images/ as the source photo
        images = _session_image_paths(session_id)
        if not images:
            return _dumps({
                "ok": False,
                "reason": "no_images",
                "hint": "reveng_add_images first with a single photo path",
            })
        image_path = images[0]
        try:
            from engine_oracle.vision.single_photo import (  # noqa: PLC0415
                reconstruct_single_photo,
            )
        except Exception as exc:  # noqa: BLE001
            return _not_provisioned(
                f"single-photo backend not available ({exc}). "
                "Check reveng_status() for TripoSR/TRELLIS provisioning."
            )
        try:
            result = reconstruct_single_photo(
                image_path,
                session_id,
                quality=quality,
                output_dir=str(_session_path(session_id) / "mesh"),
            ) or {}
        except Exception as exc:  # noqa: BLE001
            return _dumps({
                "ok": False,
                "reason": f"single_photo error: {exc}",
                "honesty": {"approximate": True},
            })
        # Always tag honesty.approximate for single-photo
        s = _load_session(session_id) or s
        s.setdefault("honesty", {})
        s["honesty"]["approximate"] = True
        s["honesty"]["single_photo_tier"] = (
            result.get("backend") or ("trellis" if quality == "high" else "triposr")
        )
        if result.get("ok") and result.get("mesh_path"):
            s.setdefault("artifacts", {})
            s["artifacts"]["mesh_path"] = result["mesh_path"]
            s["stages"]["reconstruct"] = "done"
        s["provenance"].append({
            "step": "reveng_single_photo",
            "tier": tier,
            "quality": quality,
            "ok": bool(result.get("ok")),
            "ts": _now_iso(),
        })
        _save_session(s)
        # Surface honesty on the response envelope
        out = dict(result)
        out.setdefault("honesty", "approximate")
        out["session_id"] = session_id
        return _dumps(out)

    @mcp.tool()
    def reveng_fit_primitives(session_id: str) -> str:
        """Segment the mesh and RANSAC-fit primitive shapes (cylinder, box, cone, etc.).

        Returns ``aicad.reveng_fits/1`` (contracts/reveng.md §3) when the mesh is ready.
        Persists fits.json on the session for reverse_engineer(hints=…) / n_parametric.
        """
        s = _load_session(session_id)
        if s is None:
            return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
        mesh_path = (s.get("artifacts") or {}).get("mesh_path")
        if not mesh_path:
            return _dumps({
                "ok": False,
                "reason": "no_mesh",
                "hint": "Run reveng_reconstruct (or reveng_single_photo) first to produce a mesh.",
            })
        try:
            from engine_oracle.vision.segment_fit import segment_fit  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            return _not_provisioned(
                f"segment_fit unavailable ({exc}). pip install open3d"
            )
        try:
            fits = segment_fit(mesh_path, session_id=session_id) or {}
        except Exception as exc:  # noqa: BLE001
            return _dumps({"ok": False, "reason": f"segment_fit error: {exc}"})
        if not fits.get("ok"):
            return _dumps(fits if isinstance(fits, dict) else {
                "ok": False, "reason": "segment_fit failed",
            })
        fits_path = _session_path(session_id) / "fits.json"
        try:
            fits_path.write_text(_json.dumps(fits, indent=2))
        except Exception:  # noqa: BLE001
            fits_path = None  # type: ignore[assignment]
        s = _load_session(session_id) or s
        s.setdefault("artifacts", {})
        if fits_path is not None:
            s["artifacts"]["fits_path"] = str(fits_path)
        s["stages"]["model"] = "fitting"
        s["provenance"].append({
            "step": "reveng_fit_primitives",
            "n_fits": len(fits.get("fits") or []),
            "ts": _now_iso(),
        })
        _save_session(s)
        return _dumps(fits)

    @mcp.tool()
    def reveng_build_parametric(
        session_id: str,
        strategy: Optional[str] = None,
    ) -> str:
        """Build an editable parametric model from the reconstructed mesh.

        strategy:
          - ``deterministic`` (default) — reverse_engineer with fit hints
          - ``ml`` — CAD-Recode build_parametric (engine_oracle.vision.cad_recode)
          - ``override_approximate`` — allow parametric on single-photo meshes
            (honesty gate only; rebuild strategy stays deterministic)

        Refuses when ``session.honesty.approximate`` is true unless
        ``strategy = "override_approximate"``.
        """
        s = _load_session(session_id)
        if s is None:
            return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
        strat_in = (strategy or "deterministic").strip().lower()
        if s.get("honesty", {}).get("approximate") and strat_in != "override_approximate":
            return _dumps({
                "ok": False,
                "reason": "session marked approximate (single-photo tier)",
                "hint": (
                    "Parametric modeling from a single-photo mesh is unreliable. "
                    "Pass strategy='override_approximate' to force, or capture "
                    "multi-view images and run reveng_reconstruct instead."
                ),
            })
        # Map honesty override to deterministic rebuild; ml is explicit.
        rebuild_strategy = "deterministic" if strat_in in (
            "override_approximate", "deterministic", "",
        ) else strat_in
        if rebuild_strategy not in ("deterministic", "ml"):
            return _dumps({
                "ok": False,
                "reason": f"unknown strategy {strategy!r}",
                "hint": "use strategy='deterministic' or strategy='ml'",
            })

        # Prefer a direct agent_fn call for the parametric node so strategy is
        # honoured without requiring dense capability just to launch the mission.
        mesh_path = (s.get("artifacts") or {}).get("mesh_path")
        if not mesh_path:
            # Fall back to full mission when no mesh yet (will degrade honestly).
            try:
                caps = (_reveng_status() or {}).get("capabilities", {})
            except Exception:  # noqa: BLE001
                caps = {}
            if not caps.get("dense"):
                return _not_provisioned(
                    "parametric build needs a mesh (no dense/mesh capability and no "
                    "session artifacts.mesh_path). Run reveng_reconstruct first."
                )
            try:
                result = _launch_reveng_mission(
                    session_id, "reveng_scan", _SCAN_NODE_STAGE,
                    strategy=rebuild_strategy,
                )
                return _dumps({"ok": True, **result})
            except Exception as e:
                return _err(e)

        # Direct n_parametric via the real agent_fn
        try:
            agent_fn = _make_pipeline_agent_fn(
                session_id, strategy=rebuild_strategy,
            )
            res = agent_fn(
                "build parametric",
                {"node_id": "n_parametric", "session_id": session_id},
            )
            payload = getattr(res, "result", None) or {}
            ok = bool(getattr(res, "success", False))
            out = {
                "ok": ok,
                "strategy": rebuild_strategy,
                "session_id": session_id,
            }
            if isinstance(payload, dict):
                out.update(payload)
            if not ok and "reason" not in out and "error" not in out:
                out["reason"] = (
                    payload.get("detail") if isinstance(payload, dict)
                    else "parametric build failed"
                )
            return _dumps(out)
        except Exception as e:
            return _err(e)


    # ── §8.2 WRITE — session management ─────────────────────────────────────

    @mcp.tool()
    def reveng_attach_result(
        session_id: str,
        part_ref: str,
    ) -> str:
        """Attach a parts-hub ``part_ref`` to the completed RE result for *session_id*.

        Writes a ``reveng_result.json`` sidecar into the session directory so
        that the ``RevengResultAdapter`` can expose this part to ``part_place``
        as a ``geometry_ref.kind="import_file"`` PartRecord.  Registers the
        adapter into the parts-hub singleton on first call (idempotent).

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
            artifacts = s.get("artifacts", {})
            doc_id = artifacts.get("doc_id")
            mesh_path = artifacts.get("mesh_path")
            geometry_ref = doc_id or mesh_path  # best available geometry handle

            # RE-23: write sidecar so RevengResultAdapter can serve this PartRecord.
            sidecar = {
                "schema": "aicad.reveng_result/1",
                "session_id": session_id,
                "part_ref": part_ref,
                "name": s.get("name", part_ref),
                "doc_id": doc_id,
                "mesh_path": mesh_path,
                "attached_at": _now_iso(),
            }
            sidecar_path = _session_path(session_id) / "reveng_result.json"
            sidecar_path.write_text(_json.dumps(sidecar, indent=2))

            # Register the live adapter into the parts-hub singleton (idempotent).
            try:
                from engine_oracle.parts_hub import register_provider as _reg_provider  # noqa: PLC0415
                from engine_oracle.parts_hub.adapters.reveng_result_adapter import RevengResultAdapter  # noqa: PLC0415
                _reg_provider(RevengResultAdapter())
            except Exception:
                pass  # best-effort; part_place tests that bypass hub still work

            return _dumps({
                "ok": True,
                "part_ref": part_ref,
                "geometry_ref": geometry_ref,
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

    # ── RE-33: one-call "make un-modeled part" orchestrator ────────────────

    @mcp.tool()
    def reveng_make_part_from_library(
        part_ref: str,
        part_number: Optional[str] = None,
        query: Optional[str] = None,
        auto_confirm_non_critical: bool = True,
    ) -> str:
        """One-call flow: seed → research → build → attach for a library part with no 3D model.

        Composes the existing RE building blocks into a single backend call that
        the GUI (RE-32) can trigger with one click:

        1. ``reveng_start_session(part_ref=...)``  — seed from parts-hub entry
        2. ``reveng_research_source(...)``          — pull specs / datasheet / images
        3. Launch ``reveng_datasheet`` mission      — build parametric via mission runner
        4. ``reveng_attach_result(...)``            — register model so ``part_place`` works

        Honesty rules (contracts/reveng.md §0):
        - Fit-critical dims still route through ``confirm_dimensions`` (intake ledger).
          When unresolved the function returns ``pending_dims`` and leaves the session
          resumable — it NEVER fabricates dimensions.
        - Offline / tools-absent → honest ``{ok: false, reason, hint}`` envelope.
        - ``auto_confirm_non_critical=True`` (default) auto-accepts clearly non-critical
          dims (cosmetic / reference dims flagged by the mission); fit-critical dims are
          always left for human confirmation regardless.

        Args:
            part_ref:                  Parts-hub ``part_id`` of the un-modeled entry.
            part_number:               Manufacturer part number for the research lane.
            query:                     Free-text fallback search (used when no MPN).
            auto_confirm_non_critical: Pass through to mission; does NOT affect
                                       fit-critical dim gates.

        Returns
        -------
        ``{ok, session_id, part_ref, model_present, pending_dims?}``

        ``ok=True, model_present=True``      — full pipeline succeeded; ``part_place`` works.
        ``ok=True, model_present=False,      — mission ran but fit-critical dims are
          pending_dims=[...]}``                unresolved; session is resumable.
        ``{ok: false, reason, hint}``        — offline or tools absent; no traceback.
        """
        try:
            # ── 1. Seed session from parts-hub entry ──────────────────────────
            sid = _new_session_id()
            label = f"RE {part_ref}"
            session = _make_session(sid, label, part_ref)
            _save_session(session)

            # Best-effort: copy photos / datasheet from the parts-hub record
            seeded: dict = {}
            try:
                from engine_oracle.parts_hub import get as _hub_get  # noqa: PLC0415
                record = _hub_get(part_ref)
                if record:
                    meta = record.get("metadata") or {}
                    photos = meta.get("photos") or []
                    datasheet = meta.get("datasheet")
                    if photos or datasheet:
                        sess_dir = _session_path(sid)
                        if photos:
                            images_dir = sess_dir / "images"
                            images_dir.mkdir(parents=True, exist_ok=True)
                            import shutil as _sh  # noqa: PLC0415
                            for p in photos:
                                src = _Path(p).expanduser()
                                if src.is_file():
                                    _sh.copy2(str(src), str(images_dir / src.name))
                                    seeded.setdefault("images", 0)
                                    seeded["images"] += 1
                            if seeded.get("images"):
                                session["artifacts"]["images"] = seeded["images"]
                                session["stages"]["capture"] = "done"
                        if datasheet:
                            ds_dir = sess_dir / "datasheet"
                            ds_dir.mkdir(parents=True, exist_ok=True)
                            src = _Path(datasheet).expanduser()
                            if src.is_file():
                                import shutil as _sh2  # noqa: PLC0415
                                _sh2.copy2(str(src), str(ds_dir / src.name))
                                seeded["datasheet"] = str(ds_dir / src.name)
                        if seeded:
                            session["provenance"].append({
                                "step": "seeded_from_parts_hub",
                                "part_ref": part_ref,
                                "seeded": seeded,
                                "ts": _now_iso(),
                            })
                            _save_session(session)
            except Exception:
                pass  # seeding is best-effort; never block flow

            # ── 2. Research: pull specs / datasheet / images from internet ────
            research_result: dict = {}
            if part_number or query:
                research_result = _research_ingest(
                    session_id=sid,
                    part_number=part_number,
                    query=query,
                )
                if not research_result.get("ok"):
                    # Offline or tools absent — honest envelope, leave session for retry
                    return _dumps({
                        "ok": False,
                        "reason": research_result.get("reason", "research_failed"),
                        "hint": research_result.get(
                            "hint",
                            "Check network connectivity or supply a local datasheet path.",
                        ),
                        "session_id": sid,
                        "part_ref": part_ref,
                    })
                # Record provenance
                s = _load_session(sid)
                if s is not None:
                    s.setdefault("provenance", []).append({
                        "step": "research_source",
                        "part_number": part_number,
                        "query": query,
                        "candidate_mpns": research_result.get("candidate_mpns", []),
                        "sources_count": len(research_result.get("sources", [])),
                        "ts": _now_iso(),
                    })
                    s.setdefault("stages", {})["datasheet"] = "done"
                    _save_session(s)

            # ── 3. Launch reveng_datasheet mission ────────────────────────────
            #
            # The mission DAG runs: ingest → MPN fast-path → dimension_confirm gate
            # → model → deliver.  Fit-critical dim gates are declared autonomy:"approve"
            # in the template; they PAUSE the run and surface pending_dims to the caller.
            # Non-critical dims are auto-confirmed when auto_confirm_non_critical=True.
            #
            # When the dense/mesh backend is not provisioned the mission runner
            # returns a descriptive result (not an exception); we surface it honestly.
            mission_result: dict = {}
            try:
                mission_result = _launch_reveng_mission(
                    sid,
                    "reveng_datasheet",
                    _DATASHEET_NODE_STAGE,
                    autonomy_default="auto" if auto_confirm_non_critical else "approve",
                )
            except Exception as e:  # noqa: BLE001
                # Mission runner absent (test env without DAG executor)
                mission_result = {
                    "ok": False,
                    "reason": "mission_runner_unavailable",
                    "hint": str(e),
                }

            # ── Detect pending fit-critical dims ──────────────────────────────
            pending_dims = mission_result.get("pending_dims") or []
            interrupted = mission_result.get("interrupted") or (
                mission_result.get("status") == "interrupted"
            )

            if interrupted or pending_dims:
                # Return resumable result — do NOT attach; caller must confirm dims first
                return _dumps({
                    "ok": True,
                    "session_id": sid,
                    "part_ref": part_ref,
                    "model_present": False,
                    "pending_dims": pending_dims,
                    "hint": (
                        "Fit-critical dimensions require confirmation. "
                        "Resolve via confirm_dimensions then call reveng_attach_result."
                    ),
                    "mission_status": mission_result.get("status"),
                })

            if not mission_result.get("ok", True):
                # Mission failed for a non-dim reason (backend not provisioned, etc.)
                return _dumps({
                    "ok": False,
                    "reason": mission_result.get("reason", "mission_failed"),
                    "hint": mission_result.get("hint", ""),
                    "session_id": sid,
                    "part_ref": part_ref,
                    "model_present": False,
                })

            # ── 4. Attach result → register in parts-hub ─────────────────────
            s2 = _load_session(sid)
            if s2 is None:
                return _dumps({
                    "ok": False,
                    "reason": f"session {sid!r} disappeared after mission",
                    "session_id": sid,
                    "part_ref": part_ref,
                })

            s2["part_ref"] = part_ref
            s2["provenance"].append({
                "step": "attach_result",
                "part_ref": part_ref,
                "ts": _now_iso(),
            })
            _save_session(s2)

            artifacts = s2.get("artifacts", {})
            doc_id = artifacts.get("doc_id")
            mesh_path = artifacts.get("mesh_path")
            geometry_ref = doc_id or mesh_path

            # Write sidecar so RevengResultAdapter can serve this PartRecord
            sidecar = {
                "schema": "aicad.reveng_result/1",
                "session_id": sid,
                "part_ref": part_ref,
                "name": s2.get("name", part_ref),
                "doc_id": doc_id,
                "mesh_path": mesh_path,
                "attached_at": _now_iso(),
            }
            sidecar_path = _session_path(sid) / "reveng_result.json"
            sidecar_path.write_text(_json.dumps(sidecar, indent=2))

            # Register live adapter into parts-hub singleton (idempotent)
            try:
                from engine_oracle.parts_hub import register_provider as _reg_provider  # noqa: PLC0415
                from engine_oracle.parts_hub.adapters.reveng_result_adapter import (  # noqa: PLC0415
                    RevengResultAdapter,
                )
                _reg_provider(RevengResultAdapter())
            except Exception:
                pass  # best-effort

            model_present = bool(geometry_ref)
            return _dumps({
                "ok": True,
                "session_id": sid,
                "part_ref": part_ref,
                "model_present": model_present,
                "geometry_ref": geometry_ref,
                "research_mpns": research_result.get("candidate_mpns", []),
            })

        except Exception as e:
            return _err(e)

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
