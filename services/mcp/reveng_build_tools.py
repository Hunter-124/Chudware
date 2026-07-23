"""Reverse-engineering reconstruction MCP tools (T-RF8 split of reveng_tools.py): reconstruct, single_photo, fit_primitives, build_parametric, attach_result, scan-mesh import, bundle export, make_part_from_library, research_source."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional
from pathlib import Path as _Path
import json as _json
import shutil as _shutil

from .reveng_common import (
    _DATASHEET_NODE_STAGE,
    _SCAN_NODE_STAGE,
    _dumps,
    _err,
    _launch_reveng_mission,
    _load_session,
    _make_pipeline_agent_fn,
    _make_session,
    _new_session_id,
    _not_provisioned,
    _now_iso,
    _research_ingest,
    _reveng_status,
    _save_session,
    _session_image_paths,
    _session_path,
)

def register_reveng_build_tools(mcp: FastMCP) -> None:
    """Reverse-engineering reconstruction MCP tools (T-RF8 split of reveng_tools.py): reconstruct, single_photo, fit_primitives, build_parametric, attach_result, scan-mesh import, bundle export, make_part_from_library, research_source."""


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
            from services.vision.single_photo import (  # noqa: PLC0415
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
            from services.vision.segment_fit import segment_fit  # noqa: PLC0415
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
          - ``ml`` — CAD-Recode build_parametric (services.vision.cad_recode)
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
                from services.parts import register_provider as _reg_provider  # noqa: PLC0415
                from services.parts.adapters.reveng_result_adapter import RevengResultAdapter  # noqa: PLC0415
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
                from services.parts import get as _hub_get  # noqa: PLC0415
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
                from services.parts import register_provider as _reg_provider  # noqa: PLC0415
                from services.parts.adapters.reveng_result_adapter import (  # noqa: PLC0415
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
