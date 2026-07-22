"""Reverse-engineering session + ingest MCP tools (T-RF8 split of reveng_tools.py): session CRUD/status/coverage/deviation, images/video/datasheet ingest, scale setting, scale-mat print."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional
from pathlib import Path as _Path
import os as _os
import secrets as _secrets
import shutil as _shutil

from .reveng_common import (
    _dumps,
    _err,
    _extract_frames_safe,
    _ingest_pdf,
    _load_session,
    _make_session,
    _new_session_id,
    _not_provisioned,
    _now_iso,
    _research_ingest,
    _reveng_dir,
    _reveng_status,
    _save_session,
    _session_path,
    _session_summary,
)

def register_reveng_session_tools(mcp: FastMCP) -> None:
    """Reverse-engineering session + ingest MCP tools (T-RF8 split of reveng_tools.py): session CRUD/status/coverage/deviation, images/video/datasheet ingest, scale setting, scale-mat print."""


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
