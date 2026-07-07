"""Reverse Engineering MCP tools — family ``reveng`` (contracts/reveng.md §8).

Session / ingest tools are LIVE: they read/write the session directory under
``AICAD_REVENG_DIR`` and delegate to:
- ``engine_oracle.vision.reveng_provision.reveng_status`` (RE-06, on master)
- ``engine_oracle.vision.frames.extract_frames_safe`` (RE-04, on master)

Long-job tools (reconstruct / build_parametric / single_photo / fit_primitives)
return the honest ``backend_not_provisioned`` degradation envelope (§0 contract)
until RE-17 wires the real DAG launches.

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
    def get_reveng_deviation(session_id: str) -> str:
        """Return mesh-vs-BRep deviation report for *session_id* (contracts/reveng.md §5).

        Returns the deviation schema or ``{ok: false, reason}`` if the session has no
        completed parametric model yet.
        """
        s = _load_session(session_id)
        if s is None:
            return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
        doc_id = s.get("artifacts", {}).get("doc_id")
        mesh_path = s.get("artifacts", {}).get("mesh_path")
        if not doc_id or not mesh_path:
            return _dumps({
                "ok": False,
                "reason": "no parametric model yet",
                "hint": "run reveng_build_parametric first",
            })
        # Deviation computation requires the real engine — not available at MCP layer yet.
        return _dumps({
            "ok": False,
            "reason": "backend_not_provisioned",
            "hint": "deviation computation requires RE-17 DAG integration",
        })

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
    ) -> str:
        """Create a new reverse engineering session.

        Args:
            name: Human label for this session (defaults to a timestamp label).
            part_ref: Optional parts-hub ``part_id`` when seeded from the library.

        Returns ``{ok, session_id, session}``.
        """
        try:
            sid = _new_session_id()
            label = name or f"RE session {_now_iso()}"
            session = _make_session(sid, label, part_ref)
            _save_session(session)
            return _dumps({"ok": True, "session_id": sid, "session": session})
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
        Returns a ``backend_not_provisioned`` envelope until RE-17 wires the DAG.
        Check ``reveng_status()`` for which reconstruction backends are available.
        """
        return _not_provisioned(
            "reconstruction DAG not yet wired (RE-17). "
            "Check reveng_status() for backend availability and install COLMAP (COLMAP_BIN)."
        )

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
        return _not_provisioned(
            "parametric build not yet wired (RE-17). "
            "Run reveng_reconstruct + reveng_fit_primitives first."
        )

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
