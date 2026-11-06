"""Gaussian interchange tools for the shared reverse-engineering registry."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import uuid
from typing import Optional

from services.agent import request_context as _rc
from services.formats.splat_io import MAX_BYTES, load_splat, save_splat, splat_to_mesh
from .reveng_common import (
    _busy_envelope, _dumps, _err, _find_session_run, _invalidate_downstream,
    _load_session, _make_session, _metric_mesh, _new_session_id, _now_iso,
    _release_mission, _reserve_mission, _save_session, _session_path, _splat_sparse_dir,
)


def _input_path(path: str, session_root: Path | None = None) -> Path:
    """In a cell, accept workspace files or artifacts of this owned session only."""
    resolved = _rc.cell_path(path, private=session_root is not None)
    if _rc.in_cell() and session_root is not None:
        work = Path("/work").resolve()
        root = session_root.resolve()
        if not (resolved == work or work in resolved.parents or resolved == root or root in resolved.parents):
            raise ValueError("cell_refused")
    if not resolved.is_file():
        raise FileNotFoundError(f"splat file not found: {path}")
    return resolved


def _publish_splat(cloud, dest: Path, fmt: str) -> None:
    """Do not replace an existing output with a partial conversion."""
    fd, temporary = tempfile.mkstemp(prefix=".splat-", dir=dest.parent)
    os.close(fd)
    try:
        save_splat(cloud, temporary, fmt)
        os.replace(temporary, dest)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _copy_source(source: Path, dest: Path) -> None:
    """Snapshot a bounded source before decoding so provenance matches the PLY."""
    with source.open("rb") as src, dest.open("xb") as out:
        before = os.fstat(src.fileno())
        if before.st_size <= 0 or before.st_size > MAX_BYTES:
            raise ValueError("empty or oversized splat source")
        remaining = before.st_size
        while remaining:
            block = src.read(min(remaining, 1024 * 1024))
            if not block:
                raise ValueError("splat source changed while copying")
            out.write(block)
            remaining -= len(block)
        after = os.fstat(src.fileno())
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if src.read(1) or signature(before) != signature(after):
            raise ValueError("splat source changed while copying")


def _claim_session(session_id: str) -> tuple[str | None, dict | None]:
    """Prevent jobs starting while a splat mutates their session artifacts."""
    token = "splat_edit_" + uuid.uuid4().hex
    busy = _reserve_mission(session_id, token)
    if busy:
        return None, _busy_envelope(session_id, busy)
    try:
        run = _find_session_run(session_id)
        if run and run.get("status") in ("running", "paused"):
            _release_mission(session_id, token)
            return None, _busy_envelope(session_id, run.get("run_id") or "paused")
        return token, None
    except Exception:
        _release_mission(session_id, token)
        raise


def register_reveng_splat_tools(mcp) -> None:
    """Register the same splat operations used by the GUI worker bridge."""

    @mcp.tool()
    def reveng_import_splat(path: str, session_id: Optional[str] = None,
                            project_dir: Optional[str] = None) -> str:
        """Import a Gaussian scene as a persistent reverse-engineering reference.

        Accepts Gaussian .ply, antimatter15 .splat, Niantic .spz, or .ksplat.
        Converts into the native renderer's binary little-endian Gaussian PLY,
        preserving degree 0–3 spherical harmonics where the source carries them.
        The original is retained in the session. Omissions and source quantization
        are reported in ``lossy``. No coordinate axis, physical units, or metric
        scale is guessed. If session_id is omitted a new session is created.

        An unrelated scene cannot safely inherit a reconstruction gauge. A
        session with sparse data, a mesh, fitted features or a confirmed scale
        must be left intact; import into a fresh session, then calibrate it.
        This is a visual radiance reference, not an accurate physical mesh.
        Returns {ok, session_id, gaussian_ply, count, source_format, lossy}.
        """
        reservation = None
        try:
            if session_id:
                session = _load_session(session_id, merge_gaussian=False)
                if session is None:
                    return _dumps({"ok": False, "reason": "session_not_found"})
                reservation, busy = _claim_session(session_id)
                if busy:
                    return _dumps(busy)
                session = _load_session(session_id, merge_gaussian=False)
                if session is None:
                    return _dumps({"ok": False, "reason": "session_not_found"})
                root = _session_path(session_id)
                transform = session.get("recon_transform") or {}
                if transform.get("confirmed") or (session.get("scale") or {}).get("confirmed"):
                    return _dumps({"ok": False, "reason": "confirmed_scale_has_unrelated_gauge",
                                   "hint": "Import into a new session, then calibrate the imported splat."})
                projected = _load_session(session_id) or session
                artifacts = projected.get("artifacts") or {}
                gaussian = projected.get("gaussian") or {}
                if (any(artifacts.get(key) for key in
                        ("sparse_dir", "mesh_path", "raw_mesh_path", "fits_path", "doc_id"))
                        or (gaussian.get("run_id") and gaussian.get("status") not in
                            ("done", "cancelled", "failed", "interrupted"))
                        or (session.get("stages") or {}).get("reconstruct") == "running"):
                    return _dumps({"ok": False, "reason": "session_has_existing_reconstruction",
                                   "hint": "Import into a new session to avoid mixing reconstruction gauges."})
            else:
                session_id = _new_session_id()
                session = _make_session(session_id, "Imported Gaussian splat", None, project_dir)
                root = _session_path(session_id, create=True)
            src = _input_path(path, root)
            if src.stat().st_size > MAX_BYTES:
                raise ValueError("splat source exceeds byte limit")
            batch = root / "splat" / ("import-" + uuid.uuid4().hex)
            batch.mkdir(parents=True, exist_ok=False)
            try:
                original = batch / ("source" + src.suffix.lower())
                _copy_source(src, original)
                cloud = load_splat(original)
                ply = batch / "gaussian.ply"
                _publish_splat(cloud, ply, "ply")
                artifacts = session.setdefault("artifacts", {})
                artifacts.update(splat_path=str(ply.resolve()), raw_splat_path=str(ply.resolve()),
                                 splat_units="reconstruction", splat_transform_applied=False)
                artifacts.pop("splat_dataset_dir", None)
                session["imported_splat"] = {"path": str(ply.resolve()), "source_path": str(original.resolve()),
                                             "source_format": cloud.source_format, "count": len(cloud.means),
                                             "lossy": list(cloud.lossy)}
                from services.vision.recon_transform import ReconTransform
                session.pop("axis_alignment", None)
                session["recon_transform"] = ReconTransform.identity().to_json()
                session["scale"] = {"method": None, "mm_per_unit": None,
                                    "residual_pct": None, "confirmed": False}
                session.setdefault("stages", {})["scale"] = "pending"
                session.setdefault("stages", {})["capture"] = "done"
                session.setdefault("provenance", []).append({"step": "import_splat", "source": str(src),
                                                               "source_path": str(original.resolve()),
                                                               "splat_path": str(ply.resolve()), "ts": _now_iso()})
                _save_session(session)
            except Exception:
                shutil.rmtree(batch, ignore_errors=True)
                raise
            return _dumps({"ok": True, "session_id": session_id, "gaussian_ply": str(ply.resolve()),
                           "artifact": {"path": str(ply.resolve()), "size_bytes": ply.stat().st_size,
                                        "kind": "gaussian_ply"},
                           "count": len(cloud.means), "source_format": cloud.source_format,
                           "lossy": list(cloud.lossy)})
        except Exception as exc:
            return _err(exc)
        finally:
            if reservation:
                _release_mission(session_id, reservation)

    @mcp.tool()
    def reveng_convert_splat(path: str, out_format: str, out_path: Optional[str] = None) -> str:
        """Convert Gaussian .ply/.splat/.spz/.ksplat to renderer PLY or .splat.

        Pass a file path or an owned reverse-engineering session ID as ``path``;
        a session uses its current published (possibly metric) splat. ``out_format``
        is ``ply`` or ``splat``. If ``out_path`` is omitted, a unique filename is
        chosen beside the input (in a cell, session exports go into /work).
        An explicit output may replace that path, but cannot overwrite the source.
        The coordinate frame and units are unchanged. Returns {ok, path, count,
        source_format, out_format, units, lossy}; .splat discards higher-order SH and
        quantizes color, opacity and rotation, which is recorded in ``lossy``.
        """
        try:
            candidate = _rc.cell_path(path)
            from_session = not candidate.is_file()
            if from_session:
                session = _load_session(path)
                if session is None:
                    return _dumps({"ok": False, "reason": "session_not_found_or_splat_path_invalid"})
                raw = (session.get("artifacts") or {}).get("splat_path")
                if not raw:
                    return _dumps({"ok": False, "reason": "session_has_no_splat"})
                src = _input_path(str(raw), _session_path(path))
                units = (session.get("artifacts") or {}).get("splat_units") or "reconstruction"
            else:
                src = _input_path(path)
                units = "source"
            fmt = out_format.lower().lstrip(".")
            if fmt not in ("ply", "splat"):
                return _dumps({"ok": False, "reason": "unsupported_output_format",
                               "hint": "Output format must be ply or splat."})
            if out_path:
                dest = _rc.cell_path(out_path)
            else:
                directory = Path("/work") if from_session and _rc.in_cell() else src.parent
                dest = directory / f"{src.stem}-converted-{uuid.uuid4().hex[:8]}.{fmt}"
            if dest == src:
                return _dumps({"ok": False, "reason": "source_equals_output"})
            cloud = load_splat(src)
            _publish_splat(cloud, dest, fmt)
            return _dumps({"ok": True, "path": str(dest), "count": len(cloud.means),
                           "source_format": cloud.source_format, "out_format": fmt, "units": units,
                           "lossy": list(cloud.lossy)})
        except Exception as exc:
            return _err(exc)

    def _mesh(src: Path, dest: Path, session_id: str | None, sparse_dir, kwargs: dict) -> dict:
        """Mesh one splat; with a session, attach the raw mesh. The caller holds the claim."""
        cloud = load_splat(src)
        if sparse_dir:
            kwargs = {**kwargs, "sparse_dir": sparse_dir}
        try:
            result = splat_to_mesh(cloud, dest, **kwargs)
            if not result.get("ok"):
                dest.unlink(missing_ok=True)
                return result
            if not dest.is_file():
                raise ValueError("mesher returned no mesh artifact")
            session = None
            if session_id is not None:
                # Re-read: fusion runs for minutes and other writers may have saved meanwhile.
                session = _load_session(session_id)
                if session is None:
                    raise ValueError("session disappeared while meshing")
                _invalidate_downstream(session)
                artifacts = session.setdefault("artifacts", {})
                artifacts.pop("mesh_materialization", None)
                artifacts.update(mesh_path=str(dest.resolve()), raw_mesh_path=str(dest.resolve()),
                                 mesh_units="reconstruction", mesh_transform_applied=False)
                session.setdefault("honesty", {})["approximate"] = True
                session.setdefault("stages", {})["reconstruct"] = "done"
                session.setdefault("provenance", []).append({"step": "splat_to_mesh", "source": str(src),
                                                              "mesh_path": str(dest.resolve()),
                                                              "method": result.get("method", kwargs["method"]),
                                                              "approximate": True,
                                                              "ts": _now_iso()})
                _save_session(session)
        except Exception:
            dest.unlink(missing_ok=True)
            raise
        result["raw_mesh_path"] = str(dest.resolve())
        result["mesh_path"] = str(dest.resolve())
        result["units"] = "reconstruction"
        if session is not None and (session.get("recon_transform") or {}).get("confirmed"):
            try:
                metric_path, failure = _metric_mesh(session)
                if failure:
                    result["warning"] = "Metric mesh materialization failed; raw reconstruction units retained: " + str(failure)
                elif metric_path:
                    result["mesh_path"] = str(Path(metric_path).resolve())
                    result["units"] = "mm"
            except Exception as exc:
                result["warning"] = "Metric mesh materialization failed; raw reconstruction units retained: " + str(exc)
        result["source_format"] = cloud.source_format
        result["lossy"] = list(cloud.lossy)
        if session_id is not None:
            result["session_id"] = session_id
        return result

    def _mesh_in_background(session_id: str, reservation: str, src: Path, dest: Path,
                            sparse_dir, kwargs: dict) -> None:
        """Run ``_mesh`` off the request thread and record the outcome as session.mesh_job.

        Worker stdio dispatch is serialized, so a conversion lasting minutes would stall
        every other GUI request. The session claim is held until the job is recorded.
        """
        import contextvars
        import json
        import threading

        started = _now_iso()

        def run() -> None:
            try:
                try:
                    result = _mesh(src, dest, session_id, sparse_dir, kwargs)
                except Exception as exc:  # noqa: BLE001 — reported through the session
                    result = json.loads(_err(exc))
                job = {"status": "done" if result.get("ok") else "failed", "started_at": started,
                       "finished_at": _now_iso()}
                if result.get("ok"):
                    job.update({key: result[key] for key in
                                ("mesh_path", "raw_mesh_path", "points_path", "pruned_splat_path",
                                 "method", "units", "vertices", "triangles", "voxel_size",
                                 "support_plane", "fallback_reason", "warning")
                                if key in result})
                else:
                    job["error"] = " — ".join(str(result[key]) for key in ("reason", "hint", "error")
                                              if result.get(key)) or "mesh extraction failed"
                current = _load_session(session_id)
                if current is not None:
                    current["mesh_job"] = job
                    _save_session(current)
            except Exception:  # noqa: BLE001 — a daemon thread has no caller to report to
                import logging
                logging.getLogger(__name__).exception("splat meshing for %s failed", session_id)
            finally:
                _release_mission(session_id, reservation)

        context = contextvars.copy_context()   # carries the caller's owner / project binding
        threading.Thread(target=context.run, args=(run,), name=f"reveng-mesh-{session_id}",
                         daemon=True).start()

    @mcp.tool()
    def reveng_splat_to_mesh(path_or_session: str, min_opacity: Optional[float] = None,
                             method: str = "auto", remove_floor: bool = True,
                             wait: bool = True) -> str:
        """Prune a Gaussian reconstruction to its subject, then extract a point cloud and mesh.

        Pruning drops low-opacity haze, everything outside the camera orbit, oversized
        and needle-like splats, the dominant floor/table plane (``remove_floor``; pass
        false for a subject that is itself a flat plate) and statistical floaters.
        ``min_opacity`` is chosen adaptively when omitted; a fixed value such as 0.5
        discards almost every splat of a Brush reconstruction, whose opacities are low.
        ``auto`` uses matching registered views for depth fusion when available,
        otherwise reports a Poisson fallback. Explicit ``tsdf``, ``poisson``
        and ``ball_pivoting`` methods are also available. Depth fusion renders the
        whole splat's median depth inside the pruned subject's bounds from up to 120
        registered views, drops the support plane the subject stands on
        (``remove_floor``), steep views, depth jumps and surfaces only one view
        renders, and colors the mesh from the dataset photographs; it runs for
        minutes, in parallel worker processes.
        A session's raw splat is used so its persisted similarity transform is
        applied only once by the CAD fitting pipeline. The raw mesh is attached
        to the session with explicit reconstruction-unit metadata.

        Returns {ok, mesh_path, points_path, pruned_splat_path, prune, approximate: true,
        ...}. ``points_path`` is the pruned colored point cloud and ``pruned_splat_path``
        the pruned Gaussians, both in reconstruction units. Gaussian centers are
        not a measured physical surface, hidden surfaces cannot be recovered,
        and splat-only dimensions must be independently calibrated/verified.
        For a session, ``wait=False`` returns ``{ok, session_id, pending: true}`` at once;
        get_reveng_session's ``session.mesh_job`` then goes ``running`` → ``done`` (with
        mesh_path, points_path, ...) or ``failed`` (with ``error``).
        Open3D must be provisioned in the services runtime.
        """
        reservation = None
        try:
            sparse_dir = None
            session_id = None
            try:
                candidate = _rc.cell_path(path_or_session)
                is_file = candidate.is_file()
            except (ValueError, OSError):
                is_file = False
            if is_file:
                if not wait:
                    return _dumps({"ok": False, "reason": "wait_false_requires_session",
                                   "hint": "Background meshing reports through a session; pass a session ID."})
                src = _input_path(path_or_session)
                dest = src.with_name(f"{src.stem}-approx-{uuid.uuid4().hex[:8]}.ply")
            else:
                session = _load_session(path_or_session, merge_gaussian=False)
                if session is None:
                    return _dumps({"ok": False, "reason": "session_not_found_or_splat_path_invalid"})
                reservation, busy = _claim_session(path_or_session)
                if busy:
                    return _dumps(busy)
                session = _load_session(path_or_session)
                if session is None:
                    return _dumps({"ok": False, "reason": "session_not_found_or_splat_path_invalid"})
                session_id = path_or_session
                root = _session_path(path_or_session)
                artifacts = session.get("artifacts") or {}
                raw = artifacts.get("raw_splat_path") or artifacts.get("splat_path")
                if not raw:
                    return _dumps({"ok": False, "reason": "session_has_no_splat"})
                src = _input_path(str(raw), root)
                mesh_dir = root / "mesh"
                mesh_dir.mkdir(parents=True, exist_ok=True)
                dest = mesh_dir / ("splat-approx-" + uuid.uuid4().hex + ".ply")
                sparse_dir = _splat_sparse_dir(session, str(src))
            kwargs = {"method": method, "min_opacity": min_opacity, "remove_floor": remove_floor}
            if not wait:
                session["mesh_job"] = {"status": "running", "started_at": _now_iso()}
                _save_session(session)
                _mesh_in_background(session_id, reservation, src, dest, sparse_dir, kwargs)
                reservation = None  # Released by the background job.
                return _dumps({"ok": True, "session_id": session_id, "pending": True})
            return _dumps(_mesh(src, dest, session_id, sparse_dir, kwargs))
        except Exception as exc:
            return _err(exc)
        finally:
            if reservation:
                _release_mission(path_or_session, reservation)
