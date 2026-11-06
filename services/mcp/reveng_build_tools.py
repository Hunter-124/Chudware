"""Reverse-engineering reconstruction MCP tools (split out of reveng_tools.py): reconstruct, single_photo, fit_primitives, build_parametric, attach_result, scan-mesh import, bundle export, make_part_from_library, research_source."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional
from pathlib import Path as _Path
import json as _json
import shutil as _shutil
import os as _os
import tempfile as _tempfile
import math as _math

from services.mcp.tool_registry import model_selectors

from .reveng_common import (
    _DATASHEET_NODE_STAGE,
    _SCAN_NODE_STAGE,
    _dumps,
    _err,
    _find_session_run,
    _find_reference_models,
    _has_reference_match,
    _store_reference_search,
    _fit_provenance,
    _invalidate_downstream,
    _launch_reveng_mission,
    _load_current_fits,
    _load_session,
    _make_pipeline_agent_fn,
    _make_session,
    _mission_running,
    _new_session_id,
    _not_provisioned,
    _now_iso,
    _read_run_record,
    _reject_reveng_gate,
    _research_ingest,
    _resume_reveng_mission,
    _reveng_status,
    _save_session,
    _session_image_paths,
    _session_path,
)


def _extract_match_pct(payload):
    """Dig the boolean-compare score out of a parametric-build payload.

    ``reverse_engineer`` reports ``match_pct`` at the top level; the ML lane nests its
    reverse_engineer result and cad_recode may report its own ``compare`` block, so probe
    all three rather than silently persisting ``None`` for a build that WAS scored.
    Returns ``None`` only when no lane produced a number (degraded compare).
    """
    if not isinstance(payload, dict):
        return None
    nested = [payload.get("match_pct")]
    for key in ("compare", "reverse_engineer"):
        sub = payload.get(key)
        if isinstance(sub, dict):
            nested.append(sub.get("match_pct"))
    for probe in nested:
        if isinstance(probe, (int, float)) and not isinstance(probe, bool):
            return round(float(probe), 3)
    return None


def _persist_build_result(session_id: str, *, strategy: str, state: str,
                          payload=None, reason=None, run_id=None) -> dict:
    """Record what the parametric build actually produced under ``session["result"]``.

    ``describe_reveng_result`` reads exactly this block — it is the ONLY place the
    strategy and the compare score are written, so a build that ran unscored stays
    visibly unscored (``scored: false``) instead of reporting a fabricated number.
    """
    s = _load_session(session_id)
    if s is None:
        return {}
    payload = payload if isinstance(payload, dict) else {}
    match_pct = _extract_match_pct(payload) if state == "built" else None
    block = {}
    s["result"] = block
    if state in ("failed", "running"):
        for key in ("doc_id", "step_path", "cadquery_path"):
            s.setdefault("artifacts", {}).pop(key, None)
    if state == "failed":
        s.setdefault("stages", {})["model"] = "failed"
    for stage in ("review", "deliver"):
        s.setdefault("stages", {})[stage] = "idle"
    block.update({
        "strategy": strategy,
        "state": state,
        "match_pct": match_pct,
        "scored": match_pct is not None,
        "doc_id": (payload.get("doc_id") or (s.get("artifacts") or {}).get("doc_id"))
                  if state == "built" else None,
        "mesh_fallback": bool(payload.get("mesh_fallback")) if state == "built" else False,
        "built_at": _now_iso(),
    })
    if run_id:
        block["run_id"] = run_id
    if reason:
        block["reason"] = str(reason)
    else:
        block.pop("reason", None)
    if payload.get("summary"):
        block["summary"] = payload["summary"]
    else:
        block.pop("summary", None)
    _save_session(s)
    return block


def _attach_session_result(session: dict, part_ref: str) -> tuple[str | None, dict]:
    """Publish an explicit file reference, never a live document handle as a path."""
    from services.agent import request_context as rc
    from services.reveng.gaussian_jobs import _atomic

    session_id = session["session_id"]
    artifacts = session.get("artifacts") or {}
    doc_id = artifacts.get("doc_id")
    mesh_path = artifacts.get("mesh_path")
    step_path = artifacts.get("step_path")
    owned_paths = []
    native_path = None
    geometry_path = None
    source_kind = None
    try:
        if doc_id:
            from services.mcp import core_bridge as cb

            # The doc remains live and user-owned. A nonbinding save records its
            # complete editable history without rebinding or clearing its state.
            # Keep a real native artifact in the session, including its cached
            # build, for reopen and bundle export.
            root = _session_path(session_id).resolve()
            fd, name = _tempfile.mkstemp(prefix=".reveng-", suffix=".step+", dir=root)
            _os.close(fd)
            native_path = _Path(name)
            owned_paths.append(native_path)
            saved = cb.call("doc_save", {
                "doc_id": doc_id, "path": str(native_path), "preserve_path": True,
            })
            cache = saved.get("cache") or {}
            if (saved.get("ok") is False or saved.get("_is_error") or
                    cache.get("skipped_reason") in {
                        "empty_mesh", "not_converged", "component_failed",
                        "live_sources_unsaved", "feature_failed", "unverified", "error",
                    } or not native_path.is_file() or native_path.stat().st_size == 0):
                raise cb.CoreError("reverse-engineered document has no complete saved geometry",
                                   code="rebuild_failed", detail={"cache": cache})
            geometry_path = str(native_path)
            source_kind = "parametric"
        elif step_path or mesh_path:
            source = step_path or mesh_path
            source_kind = "step" if step_path else "mesh"
            geometry_path = str(source)

        if geometry_path and rc.in_cell():
            # PartRecord paths must be workspace-relative in a cell. Materialize
            # an exclusively-created copy there, without replacing user files.
            source_path = rc.cell_path(geometry_path, private=True)
            fd, name = _tempfile.mkstemp(
                prefix=".reveng-", suffix=source_path.suffix, dir=rc.cell_path("/work")
            )
            workspace_path = _Path(name)
            owned_paths.append(workspace_path)
            with _os.fdopen(fd, "wb") as output, source_path.open("rb") as input_file:
                _shutil.copyfileobj(input_file, output)
            geometry_path = workspace_path.name

        session["part_ref"] = part_ref
        session["provenance"].append({
            "step": "attach_result", "part_ref": part_ref, "ts": _now_iso(),
        })
        _save_session(session)
        built = session.get("result") or {}
        sidecar = {
            "schema": "aicad.reveng_result/1",
            "session_id": session_id,
            "part_ref": part_ref,
            "name": session.get("name", part_ref),
            "doc_id": doc_id,
            "mesh_path": mesh_path,
            "step_path": step_path,
            "native_path": str(native_path) if native_path is not None else None,
            "geometry_ref": (
                {"kind": "import_file", "path": geometry_path} if geometry_path else None
            ),
            "geometry_source": source_kind,
            "strategy": built.get("strategy"),
            "match_pct": built.get("match_pct"),
            "scored": bool(built.get("scored")),
            "mesh_fallback": bool(built.get("mesh_fallback")) if doc_id else False,
            "attached_at": _now_iso(),
        }
        _atomic(_session_path(session_id) / "reveng_result.json", sidecar)
    except Exception:
        for path in owned_paths:
            path.unlink(missing_ok=True)
        raise

    try:
        from services.parts import register_provider
        from services.parts.adapters.reveng_result_adapter import RevengResultAdapter
        register_provider(RevengResultAdapter())
    except Exception:
        pass  # the persisted sidecar remains available to the live adapter
    return geometry_path, session

def register_reveng_build_tools(mcp: FastMCP) -> None:
    """Reverse-engineering reconstruction MCP tools (split out of reveng_tools.py): reconstruct, single_photo, fit_primitives, build_parametric, attach_result, scan-mesh import, bundle export, make_part_from_library, research_source."""

    @mcp.tool()
    def reveng_start_splat(
        session_id: str, mode: str = "environment", live: bool = False,
        video_path: str = "", quality: str = "normal", iterations: int = 2000,
        crop_bounds: Optional[list[float]] = None,
    ) -> str:
        """Start local Gaussian reconstruction immediately, without CAD scale or mesh gates.

        Live jobs train bounded snapshots as accepted images arrive. Finish requests
        a full-quality pass. Object mode uses an explicit reconstruction-unit crop,
        not automatic segmentation. Interrupted runs restart rather than resume.
        """
        from services.reveng.gaussian_jobs import start
        return _dumps(start(session_id, mode, live, video_path, quality, iterations, crop_bounds))

    @mcp.tool()
    def reveng_splat_status(session_id: str) -> str:
        """Read durable Gaussian progress and the latest immutable preview without waiting."""
        from services.reveng.gaussian_jobs import status
        return _dumps(status(session_id))

    @mcp.tool()
    def reveng_finish_splat(session_id: str) -> str:
        """Finish live acquisition with a full-quality pass over the latest accepted frames."""
        from services.reveng.gaussian_jobs import finish
        return _dumps(finish(session_id))

    @mcp.tool()
    def reveng_cancel_splat(session_id: str) -> str:
        """Stop the owned Gaussian process tree, retaining the last valid preview."""
        from services.reveng.gaussian_jobs import cancel
        return _dumps(cancel(session_id))

    @mcp.tool()
    def reveng_crop_splat(session_id: str, bounds: Optional[list[float]] = None) -> str:
        """Crop saved Gaussian centers in reconstruction units; null restores the original.

        Bounds are [xmin,ymin,zmin,xmax,ymax,zmax]. Training must be stopped.
        Every edit uses the unmodified source and preserves all Gaussian properties.
        """
        from services.reveng.gaussian_jobs import crop
        return _dumps(crop(session_id, bounds))


    # ── §8.2 WRITE — long-job stubs (RE-17 wires real DAG) ──────────────────

    @mcp.tool()
    def reveng_reconstruct(
        session_id: str,
        quality: str = "normal",
        backend: Optional[str] = None,
    ) -> str:
        """Launch sparse + dense reconstruction for *session_id* — returns immediately.

        ``quality`` is one of ``draft`` | ``normal`` | ``high``.
        When a reconstruction backend is provisioned this starts the ``reveng_scan``
        DAG mission on a worker thread and returns ``{ok, run_id, status: "running"}``
        straight away (contracts/reveng.md §0 — a multi-minute job never blocks the
        single-threaded worker loop). Progress streams as ``aicad.reveng_events/1`` on
        the run's EventJournal (GUI stepper) and is mirrored onto
        ``session.run`` for polling via ``get_reveng_session``.

        The mission runs capture → sparse SfM → **scale-confirm gate** → dense/splat/
        mesh → fit → parametric → **dimension-confirm gate** → verify → deliver. Both
        gates PAUSE the run; inspect the pending one with ``reveng_gate_status`` and
        continue (or stop) it with ``reveng_resolve_gate``.

        A session that already has a mission in flight is refused with
        ``mission_already_running``. With no reconstruction backend provisioned this
        returns the honest ``backend_not_provisioned`` envelope — check
        ``reveng_status()`` and install COLMAP (COLMAP_BIN).
        """
        s = _load_session(session_id)
        if quality not in ("draft", "normal", "high"):
            return _dumps({"ok": False, "reason": "quality must be draft, normal or high"})
        if backend not in (None, "auto", "openmvs", "brush", "splat"):
            return _dumps({"ok": False, "reason": "backend must be auto, openmvs, brush or splat"})
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
            result = _launch_reveng_mission(
                session_id, "reveng_scan", _SCAN_NODE_STAGE,
                quality=quality, backend=backend, background=True,
            )
            if result.get("ok") is False:
                return _dumps(result)
            return _dumps({"ok": True, **result})
        except Exception as e:
            return _err(e)



    # ── §8.2 WRITE — human gates on a running mission ───────────────────────
    #
    # The scan mission suspends at its two approve gates. Without these two tools the
    # pause is a dead end: executor.run() returns "paused" and nothing in the product
    # ever calls executor.resume(), so a GUI-started reconstruction stops at the scale
    # gate forever. They are deliberately NOT one auto-approving call — the
    # scale-confirm gate existing at all is the guarantee that a human validated the
    # millimetres before dense reconstruction consumed them.

    def _gate_pending(session_id: str, gate: dict) -> dict:
        """The data a human needs to judge *gate*, read live off the session."""
        kind = (gate or {}).get("gate_kind") or ""
        if kind == "scale_confirm":
            s = _load_session(session_id) or {}
            scale = dict(s.get("scale") or {})
            mm = scale.get("mm_per_unit")
            try:
                usable = mm is not None and float(mm) > 0.0
            except (TypeError, ValueError):
                usable = False
            return {
                "mm_per_unit": scale.get("mm_per_unit"),
                "residual_pct": scale.get("residual_pct"),
                "method": scale.get("method"),
                "confirmed": bool(scale.get("confirmed")),
                "detail": scale.get("detail"),
                "mesh_units_if_approved": "mm" if usable else "reconstruction",
                "hint": (
                    "approve to scale the dense mesh by this ratio"
                    if usable else
                    "no scale solved — approving continues UNSCALED (mesh stays in "
                    "reconstruction units); solve one first with reveng_set_scale()"
                ),
            }
        if kind == "dimension_confirm":
            from .reveng_session_tools import collect_session_dims  # noqa: PLC0415
            ledger = collect_session_dims(session_id)
            rows = [
                d for d in (ledger.get("dims") or [])
                if d.get("fit_critical") and not d.get("confirmed")
            ]
            return {
                "dims": rows,
                "n_outstanding": len(rows),
                "scale_confirmed": bool(ledger.get("scale_confirmed")),
                "hint": (
                    "confirm these with confirm_dimensions before approving"
                    if rows else
                    "no outstanding fit-critical rows — safe to approve"
                ),
            }
        return {}

    def _resolve_run(session_id: str, run_id: str):
        """(run_record, error_envelope) for the addressed mission."""
        if run_id:
            rec = _read_run_record(run_id)
            if rec is None:
                return None, {
                    "ok": False,
                    "reason": "run_not_found",
                    "hint": f"no reveng mission recorded for run_id {run_id!r}",
                }
            return rec, None
        if not session_id:
            return None, {
                "ok": False,
                "reason": "missing_param: session_id or run_id",
                "hint": "address the mission by its session_id or its run_id",
            }
        rec = _find_session_run(session_id)
        if rec is None:
            return None, {
                "ok": False,
                "reason": "no_mission",
                "hint": f"session {session_id!r} has no reveng mission on record — "
                        "start one with reveng_reconstruct()",
                "session_id": session_id,
            }
        return rec, None

    @mcp.tool()
    @model_selectors("run_id")
    def reveng_gate_status(session_id: str = "", run_id: str = "") -> str:
        """Report a reveng mission's status and the human gate it is waiting on.

        Address the mission by ``session_id`` (its current run) or by ``run_id``.

        Returns ``{ok, run_id, session_id, status, template, gate}`` where ``status`` is
        ``running`` | ``paused`` | ``done`` | ``failed`` | ``rejected``, and ``gate`` is
        null unless the run is suspended. A pending gate carries ``node_id``,
        ``gate_kind`` (``scale_confirm`` | ``dimension_confirm`` | ``final_accept``),
        the prompt, and a ``pending`` block with exactly what the human must judge:
        the solved ``mm_per_unit`` + ``residual_pct`` for a scale gate, the outstanding
        fit-critical dimension rows for a dimension gate.

        Answer it with ``reveng_resolve_gate``.
        """
        try:
            rec, err = _resolve_run(session_id, run_id)
            if err is not None:
                return _dumps(err)
            sid = rec.get("session_id") or session_id
            gate = rec.get("gate")
            out = {
                "ok": True,
                "run_id": rec.get("run_id"),
                "session_id": sid,
                "template": rec.get("template"),
                "status": rec.get("status"),
                "started_at": rec.get("started_at"),
                "updated_at": rec.get("updated_at"),
                "live": bool(_mission_running(sid)),
                "gate": None,
            }
            if rec.get("error"):
                out["error"] = rec["error"]
            if gate:
                out["gate"] = {**gate, "pending": _gate_pending(sid, gate)}
                out["hint"] = (
                    "call reveng_resolve_gate(session_id, decision='approve'|'reject')"
                )
            return _dumps(out)
        except Exception as e:
            return _err(e)



    @mcp.tool()
    @model_selectors("run_id")
    def reveng_resolve_gate(
        session_id: str,
        decision: str,
        run_id: str = "",
        note: str = "",
    ) -> str:
        """Answer the human gate a reveng mission is suspended on.

        ``decision`` is required and must be ``approve`` or ``reject`` — there is no
        default, because a scale-confirm gate silently defaulting to approve would
        destroy the only guarantee the pipeline offers about its millimetres.

        ``approve`` resumes the mission from the gate on a worker thread and returns
        ``{ok, run_id, status: "running"}`` immediately (the work past a scale gate is
        the whole dense/mesh pass). ``reject`` stops it: the checkpoint stays suspended
        at the gate so nothing downstream runs, and the run reports
        ``status: "rejected"`` with the reason. A rejected run remains resumable —
        approve it later to continue from the same checkpoint.
        """
        try:
            choice = (decision or "").strip().lower()
            if choice not in ("approve", "reject"):
                return _dumps({
                    "ok": False,
                    "reason": f"invalid decision {decision!r}",
                    "hint": "decision must be 'approve' or 'reject'",
                })
            rec, err = _resolve_run(session_id, run_id)
            if err is not None:
                return _dumps(err)
            rid = str(rec.get("run_id") or "")
            if choice == "reject":
                return _dumps(_reject_reveng_gate(rid, note=note))
            result = _resume_reveng_mission(rid, "approve")
            if result.get("ok") is False:
                return _dumps(result)
            return _dumps({"ok": True, "decision": "approve", **result})
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
        mesh_dir = _session_path(session_id) / "mesh"
        mesh_dir.mkdir(parents=True, exist_ok=True)
        run_dir = _Path(_tempfile.mkdtemp(prefix="single-", dir=mesh_dir))
        try:
            result = reconstruct_single_photo(
                image_path,
                session_id,
                quality=quality,
                output_dir=str(run_dir),
            ) or {}
        except Exception as exc:  # noqa: BLE001
            _shutil.rmtree(run_dir, ignore_errors=True)
            return _dumps({
                "ok": False,
                "reason": f"single_photo error: {exc}",
                "honesty": {"approximate": True},
            })
        if not result.get("ok"):
            _shutil.rmtree(run_dir, ignore_errors=True)
        # Always tag honesty.approximate for single-photo
        s = _load_session(session_id) or s
        s.setdefault("honesty", {})
        s["honesty"]["approximate"] = True
        s["honesty"]["single_photo_tier"] = (
            result.get("backend") or ("trellis" if quality == "high" else "triposr")
        )
        if result.get("ok") and result.get("mesh_path"):
            _invalidate_downstream(s)
            s.setdefault("artifacts", {}).update(
                mesh_path=result["mesh_path"], raw_mesh_path=result["mesh_path"],
                mesh_units="reconstruction", mesh_transform_applied=False)
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
    def reveng_fit_primitives(session_id: str, approximate: bool = False) -> str:
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
            provenance = _fit_provenance(s)
        except (OSError, ValueError) as exc:
            return _dumps({"ok": False, "reason": f"fit provenance error: {exc}"})
        try:
            from services.vision.segment_fit import segment_fit  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            return _not_provisioned(
                f"segment_fit unavailable ({exc}). pip install open3d"
            )
        try:
            fits = segment_fit(mesh_path, session_id=session_id,
                               recon_transform=s.get("recon_transform"),
                               transform_applied=bool((s.get("artifacts") or {}).get("mesh_transform_applied")),
                               approximate=approximate) or {}
        except Exception as exc:  # noqa: BLE001
            return _dumps({"ok": False, "reason": f"segment_fit error: {exc}"})
        if not fits.get("ok"):
            return _dumps(fits if isinstance(fits, dict) else {
                "ok": False, "reason": "segment_fit failed",
            })
        current = _load_session(session_id)
        try:
            unchanged = current is not None and _fit_provenance(current) == provenance
        except (OSError, ValueError):
            unchanged = False
        if not unchanged:
            if current is not None and _load_current_fits(current) is None:
                _invalidate_downstream(current)
                _save_session(current)
            return _dumps({"ok": False, "reason": "mesh_changed_during_fit",
                           "hint": "The mesh or calibration changed while fitting; fit again."})
        s = current
        fits["surface_approximate"] = bool(s.get("honesty", {}).get("approximate"))
        fits["approximate"] = bool(fits.get("approximate") or fits["surface_approximate"])
        fits["provenance"] = provenance
        _invalidate_downstream(s)
        fits_path = _session_path(session_id) / "fits.json"
        try:
            fits_path.write_text(_json.dumps(fits, indent=2))
        except Exception as exc:  # noqa: BLE001
            _save_session(s)
            return _dumps({"ok": False, "reason": f"fits persistence error: {exc}"})
        s.setdefault("artifacts", {})
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
          - ``override_approximate`` — allow parametric on approximate surfaces
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
                "reason": "session marked approximate",
                "hint": (
                    "A splat-derived or single-photo surface is not an exact measured part. "
                    "Independently verify critical features and pass "
                    "strategy='override_approximate' to build from it."
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
                    strategy=rebuild_strategy, background=True,
                )
                if result.get("ok") is False:
                    return _dumps(result)
                # Async lane: the mission runs on a worker thread, so record the
                # strategy now — describe_reveng_result says "running" instead of
                # "never built", and the score lands on the session when it finishes.
                _persist_build_result(
                    session_id, strategy=rebuild_strategy, state="running",
                    run_id=result.get("run_id"),
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
            if (isinstance(payload, dict) and isinstance(payload.get("features"), list)
                    and not payload["features"]):
                payload = {**payload, "ok": False, "reason": "no_features_rebuilt",
                           "match_pct": None}
                payload.setdefault("summary", "no features rebuilt — see recognition.unclaimed")
            ok = bool(getattr(res, "success", False)) and not (
                isinstance(payload, dict) and payload.get("ok") is False)
            out = {
                "ok": ok,
                "strategy": rebuild_strategy,
                "session_id": session_id,
            }
            if isinstance(payload, dict):
                out.update(payload)
            out["ok"] = ok
            if not ok:
                out["doc_id"] = None
            if not ok and "reason" not in out and "error" not in out:
                out["reason"] = (
                    payload.get("detail") if isinstance(payload, dict)
                    else "parametric build failed"
                )
            built = _persist_build_result(
                session_id, strategy=rebuild_strategy,
                state="built" if ok else "failed", payload=payload,
                reason=None if ok else (out.get("reason") or out.get("error")),
            )
            out["match_pct"] = built.get("match_pct")
            out["scored"] = bool(built.get("scored"))
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
            geometry_ref, s = _attach_session_result(s, part_ref)

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

        Copies the native mesh to ``<session>/mesh/``. ``scale_mm`` is the
        millimetres-per-source-unit ratio, not a physical feature dimension.
        It records the transform for fitting and later metric mesh materialization.
        """
        try:
            s = _load_session(session_id)
            if s is None:
                return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
            from services.agent import request_context
            src = request_context.cell_path(path)
            if not src.is_file():
                return _dumps({"ok": False, "reason": f"file not found: {path}"})
            if scale_mm is not None and (not _math.isfinite(scale_mm) or scale_mm <= 0):
                return _dumps({"ok": False, "reason": "scale_mm must be finite and positive"})
            mesh_dir = _session_path(session_id) / "mesh"
            mesh_dir.mkdir(parents=True, exist_ok=True)
            fd, name = _tempfile.mkstemp(prefix="import-", suffix=src.suffix.lower(), dir=mesh_dir)
            _os.close(fd)
            dest = _Path(name)
            try:
                _shutil.copy2(str(src), str(dest))
            except Exception:
                dest.unlink(missing_ok=True)
                raise
            _invalidate_downstream(s)
            # An unrelated imported scan cannot inherit a prior SfM gauge or splat.
            s.pop("sfm_dataset", None)
            s.pop("axis_alignment", None)
            s.pop("imported_splat", None)
            for key in ("sparse_dir", "splat_path", "raw_splat_path",
                        "splat_units", "splat_transform_applied", "splat_dataset_dir", "fits_path"):
                s["artifacts"].pop(key, None)
            s["artifacts"].update(mesh_path=str(dest), raw_mesh_path=str(dest),
                                  mesh_units="reconstruction", mesh_transform_applied=False)
            if scale_mm is not None:
                from services.vision.recon_transform import ReconTransform
                transform = ReconTransform(mm_per_unit=float(scale_mm),
                    rotation=[[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]],
                    translation_mm=[0., 0., 0.], source="import_scan_mesh", confirmed=True)
                s["recon_transform"] = transform.to_json()
                s["scale"].update(mm_per_unit=float(scale_mm), method="import_scan_mesh", confirmed=True)
            else:
                s.pop("recon_transform", None)
                s["scale"].update(mm_per_unit=None, method=None, confirmed=False)
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

        Current mesh artifact paths in the archive manifest are archive-relative.
        The latest imported scan is archived under its original filename.
        Returns ``{ok, path: str}`` on success.
        """
        try:
            s = _load_session(session_id)
            if s is None:
                return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
            import zipfile
            from services.agent import request_context
            dest_path = (request_context.cell_path(dest) if request_context.in_cell()
                         else _Path(dest).expanduser())
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            session_root = _session_path(session_id).resolve()
            manifest = _json.loads((session_root / "session.json").read_text())

            def owned_file(value):
                if not isinstance(value, str) or not value:
                    return None
                candidate = _Path(value)
                if not candidate.is_absolute():
                    candidate = session_root / candidate
                candidate = candidate.resolve()
                if candidate.is_file() and candidate.is_relative_to(session_root):
                    return candidate.relative_to(session_root).as_posix()
                return None

            imported_rel = stable_rel = None
            for row in reversed(manifest.get("provenance") or []):
                if not isinstance(row, dict) or row.get("step") != "import_scan_mesh":
                    continue
                imported_rel = owned_file(row.get("mesh_path"))
                source = row.get("source")
                if imported_rel and isinstance(source, str) and _Path(source).name:
                    stable_rel = (_Path("mesh") / _Path(source).name).as_posix()
                    row["mesh_path"] = stable_rel
                    break
            for key in ("mesh_path", "raw_mesh_path"):
                value = (manifest.get("artifacts") or {}).get(key)
                relative = owned_file(value)
                if relative:
                    manifest["artifacts"][key] = (
                        stable_rel if relative == imported_rel and stable_rel else relative)

            with zipfile.ZipFile(str(dest_path), "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("session.json", _json.dumps(manifest, indent=2))
                for fpath in session_root.rglob("*"):
                    if not fpath.is_file():
                        continue
                    relative = fpath.relative_to(session_root).as_posix()
                    if relative == "session.json":
                        continue
                    if stable_rel and relative == imported_rel:
                        relative = stable_rel
                    elif stable_rel and relative == stable_rel:
                        # Preserve an older file that already occupies this archive name.
                        index = 1
                        while True:
                            alternate = (_Path("mesh") / f"_archive_previous_{index}" /
                                         _Path(stable_rel).name).as_posix()
                            if not (session_root / alternate).exists():
                                relative = alternate
                                break
                            index += 1
                    zf.write(str(fpath), relative)
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
        project_dir: Optional[str] = None,
        force_build: bool = False,
    ) -> str:
        """Find an existing CAD model first, or research, build and attach a library part.

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
            project_dir:               Working folder owning the session.
            force_build:               Skip model search and build from research.

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
            session = _make_session(sid, label, part_ref, project_dir)
            session["lane"] = "reference"
            _save_session(session)

            if not force_build and (part_number or query):
                reference = _find_reference_models(part_number or query)
                if reference.get("ok"):
                    _store_reference_search(session, reference)
                    _save_session(session)
                    if _has_reference_match(reference):
                        return _dumps({
                            "ok": True, "session_id": sid, "part_ref": part_ref,
                            "model_present": False,
                            "existing_models": reference["candidates"],
                            "standard_part": reference["standard_part"],
                            "hint": "Existing CAD model(s) found — call reveng_use_model(session_id, url=...) instead of building one.",
                        })

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

            geometry_ref, s2 = _attach_session_result(s2, part_ref)

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

        Run ``reveng_find_models`` first and use an existing CAD model when available.

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
