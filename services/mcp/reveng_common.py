"""Shared session store + pipeline machinery for the reveng MCP tool family modules
(T-RF8 split of the former reveng_tools.py god file): session JSON CRUD, research/PDF
ingest, provision status, pipeline-agent factory, mission launcher.
Imported by the reveng_*_tools family modules; re-exported through the reveng_tools
facade."""

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
    from services.vision.research_ingest import research_and_ingest as _research_and_ingest
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
    base = _os.environ.get("CHUDWARE_REVENG_DIR")
    if base:
        return _Path(base).expanduser()
    xdg = _os.environ.get("XDG_DATA_HOME", _os.path.expanduser("~/.local/share"))
    return _Path(xdg) / "Chudware" / "reveng"


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
    """Optional runs-dir override for hermetic tests (CHUDWARE_REVENG_RUNS_DIR)."""
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
        ``ml`` — lazy-import services.vision.cad_recode.build_parametric.
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
                    from services.vision.dense_backends import run_openmvs  # noqa: PLC0415
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
                    from services.vision.dense_backends import run_brush  # noqa: PLC0415
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

            # Mesh cleanup when we have a mesh.
            #
            # session.scale.mm_per_unit is a RATIO (multiply reconstruction-space lengths
            # by it to get mm) — exactly what cleanup_mesh's mm_per_unit means. When the
            # scale stage has not run yet it is None/0: leave the mesh in reconstruction
            # units rather than inventing a factor, and say so on the session.
            cleaned = mesh_path
            mesh_unscaled = True
            if mesh_path:
                try:
                    from services.vision.mesh_cleanup import cleanup_mesh  # noqa: PLC0415
                    out_clean = str(
                        _Path(session_dir) / "mesh" / "cleaned.ply"
                    )
                    _Path(out_clean).parent.mkdir(parents=True, exist_ok=True)
                    mm_per_unit = None
                    try:
                        raw_scale = (_load_session(session_id) or {}).get(
                            "scale", {}
                        ).get("mm_per_unit")
                        if raw_scale is not None:
                            mm_per_unit = float(raw_scale)
                    except (TypeError, ValueError):
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

            s = _load_session(session_id) or s
            s.setdefault("artifacts", {})
            s["artifacts"]["sparse_dir"] = sparse_dir
            if cleaned:
                s["artifacts"]["mesh_path"] = cleaned
                # Honest units tag: consumers (fit / parametric / GUI) must not read a
                # reconstruction-unit mesh as millimetres.
                s["artifacts"]["mesh_units"] = "reconstruction" if mesh_unscaled else "mm"
            if splat_path:
                s["artifacts"]["splat_path"] = splat_path
            s["stages"]["reconstruct"] = "done"
            s["provenance"].append({
                "step": "n_reconstruct",
                "sparse_backend": sparse_result.get("backend"),
                "used_ffwd": used_ffwd,
                "dense": dense_notes,
                "mesh_units": "reconstruction" if mesh_unscaled else "mm",
                "ts": _now_iso(),
            })
            _save_session(s)
            return _Result(
                True,
                "reconstruction complete",
                {
                    "sparse": sparse_result,
                    "mesh_path": cleaned,
                    "mesh_units": "reconstruction" if mesh_unscaled else "mm",
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
