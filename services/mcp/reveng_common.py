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
import hashlib as _hashlib

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


def _list_sessions(project_dir: Optional[str] = None) -> list[dict]:
    # An explicit folder wins; else the bound agent project root; else no filter. An empty
    # string is "unknown", never realpath("") (which would silently mean the worker's cwd).
    root = None if _rc.in_cell() else (project_dir or _rc.root_value("CHUDWARE_PROJECT_DIR"))
    target = _os.path.normcase(_os.path.realpath(root)) if root else None
    base = _reveng_dir()
    if not base.is_dir():
        return []
    sessions = []
    for path in sorted(base.glob("*/session.json")):
        try:
            manifest = _json.loads(path.read_text())
            _require_record_owner(manifest)
            session = _load_session(manifest["session_id"])
            if (session is not None and
                    (target is None or (session.get("project_dir") and
                     _os.path.normcase(_os.path.realpath(session["project_dir"])) == target))):
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


def _make_session(session_id: str, name: str, part_ref: Optional[str],
                  project_dir: Optional[str] = None) -> dict:
    _validate_handle(session_id)
    now = _now_iso()
    session = {
        "schema": "aicad.reveng_session/1",
        "session_id": session_id,
        "_owner": _rc.owner_key(),
        "name": name,
        "created_at": now,
        "updated_at": now,
        "status": "capturing",
        "part_ref": part_ref,
        "lane": "scan",
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
    if not _rc.in_cell():
        root = project_dir or _rc.root_value("CHUDWARE_PROJECT_DIR")
        session["project_dir"] = _os.path.realpath(root) if root else None
    return session


def _load_session(session_id: str, *, merge_gaussian: bool = True) -> Optional[dict]:
    try:
        p = _session_json_path(session_id)
        session = _json.loads(p.read_text())
        if not isinstance(session, dict) or session.get("session_id") != session_id:
            raise KeyError("not_found")
        _require_record_owner(session)
        # Bundle manifests carry portable session-relative mesh paths.
        artifacts = session.get("artifacts")
        if isinstance(artifacts, dict):
            root = p.parent.resolve()
            for key in ("mesh_path", "raw_mesh_path"):
                value = artifacts.get(key)
                if isinstance(value, str) and value and not _Path(value).is_absolute():
                    resolved = (root / value).resolve()
                    if not resolved.is_relative_to(root):
                        raise KeyError("not_found")
                    artifacts[key] = str(resolved)
        from services.reveng.gaussian_jobs import status as splat_status
        gaussian = splat_status(session_id) if merge_gaussian else {}
        if gaussian.get("run_id"):
            session["gaussian"] = gaussian
            artifacts = session.setdefault("artifacts", {})
            # Persisted artifacts (including imported or calibrated splats) take
            # precedence over the supervisor's native-gauge preview.
            dataset = session.get("sfm_dataset") or {}
            same_gauge = bool(dataset.get("dataset_dir")
                              and dataset.get("dataset_dir") == gaussian.get("dataset_dir"))
            legacy_preview = (not dataset and not session.get("recon_transform")
                              and not session.get("imported_splat") and not artifacts.get("mesh_path"))
            if (not artifacts.get("splat_path") and gaussian.get("splat_path")
                    and (same_gauge or legacy_preview)):
                artifacts["splat_path"] = str(_Path(gaussian["splat_path"]).resolve())
            if (artifacts.get("splat_transform_applied") and session.get("recon_transform")
                    and artifacts.get("raw_splat_path") == gaussian.get("splat_path")
                    and gaussian.get("view_camera")):
                import numpy as np
                from services.vision.recon_transform import ReconTransform
                transform = ReconTransform.from_json(session["recon_transform"])
                camera = dict(gaussian["view_camera"])
                camera["eye"] = transform.apply(np.asarray(camera["eye"])).tolist()
                for key in ("forward", "up"):
                    camera[key] = (np.asarray(transform.rotation) @ np.asarray(camera[key])).tolist()
                camera["focus_distance"] *= transform.mm_per_unit
                gaussian["view_camera"] = camera
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
    if isinstance(gaussian, dict) and gaussian.get("splat_path"):
        artifacts = dict(stored.get("artifacts") or {})
        projected = str(_Path(gaussian["splat_path"]).resolve())
        if artifacts.get("splat_path") == projected:
            previous = _read(_session_json_path(sid)).get("artifacts") or {}
            if "splat_path" in previous:
                artifacts["splat_path"] = previous["splat_path"]
            else:
                artifacts.pop("splat_path", None)
            stored["artifacts"] = artifacts
    _atomic(_session_json_path(sid), stored)


def _find_reference_models(query: str, *, limit: int = 8) -> dict:
    try:
        return _find_reference_models_impl(query, limit=limit)
    except Exception as exc:
        return {"ok": False, "reason": "model_search_failed", "hint": str(exc)}


def _find_reference_models_impl(query: str, *, limit: int = 8) -> dict:
    """Find available reference CAD without claiming uncertain matches are exact."""
    import re
    from urllib.parse import urlparse, unquote

    query = str(query or "").strip()
    if not query:
        return {"ok": False, "reason": "query_required", "hint": "Provide a product name, part number, or model URL."}
    parsed = urlparse(query)
    if parsed.scheme in ("http", "https") and parsed.hostname:
        return {
            "ok": True, "query": query,
            "candidates": [{"title": unquote(_Path(parsed.path).name) or query,
                            "site": (parsed.hostname.split(".")[-2]
                                     if parsed.hostname.endswith(".com") else parsed.hostname), "url": query,
                            "confidence": "exact", "attach": False}],
            "standard_part": None, "coverage": {"sites_searched": 0, "raw_hits": 1},
            "hint": "Use the URL to acquire this reference model.",
        }

    from services.parts.model_matcher import match_model
    mpn = query if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-./]{2,30}", query) and any(
        c.isdigit() for c in query) else ""
    # Pull the whole fan-out: match_model orders a tier by part_id, so its own cut would keep an
    # arbitrary alphabetical slice of the (usually all-"weak") title matches.
    result = match_model({"title": query, "mpn": mpn}, limit=50)
    if not result.get("ok"):
        return result
    candidates = []
    standard_part = None
    for match in result.get("matches", []):
        plan = match.get("artifact_plan") or {}
        if match.get("site") == "standard_hardware":
            standard_part = {"title": match.get("title"), "family": plan.get("family"),
                             "size": plan.get("size"), "plan": plan}
            continue
        part_id = str(match.get("part_id") or "")
        url = part_id.split("::", 1)[1] if part_id.startswith("catalog:") and "::" in part_id else ""
        parsed_url = urlparse(url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
            continue
        candidates.append({"title": match.get("title"), "site": match.get("site"),
                           "url": url, "confidence": match.get("confidence"),
                           "attach": bool(match.get("attach"))})
    # Within a confidence tier, prefer titles that are mostly the query ("Servo MG996R") over
    # titles that merely mention it ("Robotic gripper for MG996R servo ...").
    tier_rank = {"exact": 0, "strong": 1}
    wanted = set(re.findall(r"[a-z0-9]+", query.lower()))

    def _relevance(candidate: dict) -> tuple:
        words = re.findall(r"[a-z0-9]+", str(candidate.get("title") or "").lower())
        shared = len(wanted & set(words))
        return (tier_rank.get(candidate.get("confidence"), 2),
                -(shared / len(words)) if words else 0.0, -shared)

    candidates = sorted(candidates, key=_relevance)[:max(1, int(limit or 8))]
    coverage = result.get("coverage") or {"sites_searched": 0, "raw_hits": 0}
    if candidates:
        hint = "Confirm the model identity before using it."
    elif standard_part:
        hint = "Use the standard part plan to create the matching model."
    elif not coverage.get("sites_searched"):
        hint = ("No CAD-model site was reachable (the browser stack is unavailable; only public "
                "Printables is searched without it). Paste a model URL or drop a downloaded CAD file.")
    else:
        hint = "No existing model found — build one from a datasheet or specs."
    return {"ok": True, "query": query, "candidates": candidates,
            "standard_part": standard_part, "coverage": coverage, "hint": hint}


def _has_reference_match(result: dict) -> bool:
    """True when building our own model would likely duplicate one that already exists.

    Community CAD sites carry no part-number field, so a GrabCAD "Servo MG996R" is only a
    title ("weak") match; it still counts when its title contains every word of the query.
    """
    import re
    wanted = set(re.findall(r"[a-z0-9]+", str(result.get("query") or "").lower()))

    def covers(candidate: dict) -> bool:
        title = set(re.findall(r"[a-z0-9]+", str(candidate.get("title") or "").lower()))
        return bool(wanted) and wanted <= title

    return bool(result.get("standard_part") or any(
        candidate.get("confidence") in ("exact", "strong") or covers(candidate)
        for candidate in result.get("candidates", [])))


def _store_reference_search(session: dict, result: dict) -> None:
    reference = session.setdefault("reference", {})
    reference.update({key: result.get(key) for key in
                      ("query", "candidates", "standard_part", "coverage", "hint")})
    reference["status"] = "done"
    reference.pop("error", None)
    reference["searched_at"] = _now_iso()
    _save_session(session)


def _start_reference_search(session: dict, query: str, limit: int) -> None:
    """Run the CAD-library fan-out off the request thread; the result lands in the session.

    The fan-out renders ~20 vendor pages through the single-request CEF daemon (roughly a
    minute and a half cold), and worker stdio dispatch is serialized, so a blocking call would
    stall every other GUI request. ``reference.status`` is ``searching`` until the thread stores
    ``done`` (with the results) or ``failed`` (with ``error``); a newer search for a different
    query supersedes an older one, whose late result is dropped.
    """
    import contextvars

    sid = session["session_id"]
    reference = session.setdefault("reference", {})
    reference.update(query=query, status="searching", started_at=_now_iso())
    reference.pop("error", None)
    _save_session(session)

    def run() -> None:
        result = _find_reference_models(query, limit=limit)
        try:
            with _REFERENCE_SEARCH_LOCK:
                current = _load_session(sid)
                if current is None or (current.get("reference") or {}).get("query") != query:
                    return
                if result.get("ok"):
                    _store_reference_search(current, result)
                else:
                    current["reference"].update(
                        status="failed",
                        error=" — ".join(str(v) for v in (result.get("reason"), result.get("hint"))
                                         if v))
                    _save_session(current)
        except Exception:  # noqa: BLE001 — a daemon thread has no caller to report to
            import logging
            logging.getLogger(__name__).exception("reference model search for %s failed", sid)

    context = contextvars.copy_context()   # carries the caller's owner / project binding
    _threading.Thread(target=context.run, args=(run,), name=f"reveng-find-{sid}",
                      daemon=True).start()


_REFERENCE_SEARCH_LOCK = _threading.Lock()


def _invalidate_downstream(session: dict) -> None:
    """Detach current derived state without deleting already published snapshots."""
    artifacts = session.setdefault("artifacts", {})
    for key in ("fits_path", "step_path", "cadquery_path", "mesh_materialization"):
        artifacts.pop(key, None)
    artifacts["doc_id"] = None
    session.pop("result", None)
    for stage in ("model", "review", "deliver"):
        session.setdefault("stages", {})[stage] = "idle"

def _reset_transform_artifacts(session: dict) -> None:
    """Restore native previews without discarding the current cleaned surface."""
    artifacts = session.setdefault("artifacts", {})
    raw_splat = artifacts.get("raw_splat_path")
    if artifacts.get("splat_transform_applied") and not raw_splat:
        raise ValueError("cannot recalibrate transformed splat without its native artifact")
    mesh = artifacts.get("mesh_path")
    raw = artifacts.get("raw_mesh_path")
    if mesh and artifacts.get("mesh_transform_applied"):
        old = session.get("recon_transform")
        materialized = artifacts.get("mesh_materialization") or {}
        can_reuse_raw = bool(
            raw and _Path(raw).is_file() and old
            and materialized.get("recon_transform") == old
            and materialized.get("source_sha256") == _file_sha256(str(raw))
            and materialized.get("mesh_sha256") == _file_sha256(str(mesh))
        )
        if not can_reuse_raw:
            if not old:
                raise ValueError("cannot recalibrate transformed mesh without its previous transform")
            import numpy as np
            from services.vision.mesh_cleanup import apply_transform_to_mesh
            from services.vision.recon_transform import ReconTransform
            previous = ReconTransform.from_json(old)
            inverse_rotation = np.asarray(previous.rotation).T
            inverse = ReconTransform(
                1.0 / previous.mm_per_unit, inverse_rotation.tolist(),
                (-inverse_rotation @ np.asarray(previous.translation_mm)
                 / previous.mm_per_unit).tolist(), source="inverse_reconstruction")
            target = _session_path(session["session_id"]) / "mesh" / f"native-{_secrets.token_hex(8)}.ply"
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(".pending-" + target.name)
            try:
                result = apply_transform_to_mesh(str(mesh), inverse.to_json(), str(temporary))
                if not result.get("ok"):
                    raise ValueError("could not restore native measurement mesh: "
                                     + str(result.get("reason") or result))
                _os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            raw = str(target)
            artifacts["raw_mesh_path"] = raw
        artifacts.update(mesh_path=raw, mesh_units="reconstruction", mesh_transform_applied=False)
    if raw_splat:
        artifacts.update(splat_path=raw_splat, splat_units="reconstruction",
                         splat_transform_applied=False)
    artifacts.pop("mesh_materialization", None)


def _file_sha256(path: str) -> str:
    digest = _hashlib.sha256()
    with _Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fit_provenance(session: dict) -> dict:
    """Bind fits to actual mesh bytes and the complete reconstruction transform."""
    artifacts = session.get("artifacts") or {}
    mesh = artifacts.get("mesh_path")
    if not mesh:
        raise ValueError("no measurement mesh")
    raw = artifacts.get("raw_mesh_path") or mesh
    mesh_hash = _file_sha256(str(mesh))
    return {
        "mesh_path": str(mesh),
        "mesh_sha256": mesh_hash,
        "raw_mesh_sha256": mesh_hash if raw == mesh else _file_sha256(str(raw)),
        "mesh_transform_applied": bool(artifacts.get("mesh_transform_applied")),
        "recon_transform": session.get("recon_transform"),
    }


def _load_current_fits(session: dict) -> Optional[dict]:
    """Load explicitly attached fits only when their geometry and calibration match."""
    artifacts = session.get("artifacts") or {}
    path = artifacts.get("fits_path")
    if not path:
        return None
    try:
        path = _Path(path)
        root = _session_path(session["session_id"])
        if not path.is_absolute():
            path = root / path
        if _rc.in_cell() and not path.resolve().is_relative_to(root.resolve()):
            return None
        fits = _json.loads(path.read_text())
        if not isinstance(fits, dict):
            return None
        provenance = fits.get("provenance")
        if not isinstance(provenance, dict):
            return None
        current = _fit_provenance(session)
        if (provenance.get("recon_transform") != current["recon_transform"]
                or provenance.get("raw_mesh_sha256") != current["raw_mesh_sha256"]):
            return None
        if (provenance.get("mesh_sha256") == current["mesh_sha256"]
                and provenance.get("mesh_transform_applied") == current["mesh_transform_applied"]):
            return fits
        # Applying the identical similarity to the same source mesh is not a
        # replacement. Verify both source and output bytes before reusing fits.
        materialized = artifacts.get("mesh_materialization") or {}
        if (not provenance.get("mesh_transform_applied")
                and current["mesh_transform_applied"]
                and materialized.get("source_sha256") == provenance.get("mesh_sha256")
                and materialized.get("mesh_sha256") == current["mesh_sha256"]
                and materialized.get("recon_transform") == current["recon_transform"]):
            return fits
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def _session_summary(s: dict) -> dict:
    summary = {
        "session_id": s["session_id"],
        "name": s["name"],
        "status": s["status"],
        "created_at": s["created_at"],
        "updated_at": s["updated_at"],
        "part_ref": s.get("part_ref"),
        "project_dir": s.get("project_dir"),
        "lane": s.get("lane", "scan"),
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

def _source_signature(paths: list[str]) -> list[dict]:
    """Identify the exact input set whose arbitrary SfM gauge is being reused."""
    rows = []
    for name in paths:
        path = _Path(name).resolve()
        stat = path.stat()
        rows.append({"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    return rows


def _dataset_matches(dataset: dict, paths: list[str]) -> bool:
    try:
        return (dataset.get("source_signature") == _source_signature(paths)
                and _Path(dataset["dataset_dir"]).is_dir()
                and _Path(dataset["sparse_dir"]).is_dir()
                and _Path(dataset["images_dir"]).is_dir())
    except (OSError, KeyError, TypeError):
        return False

def _splat_sparse_dir(session: dict, splat_path: str) -> Optional[str]:
    """Only pair camera poses with a splat known to share their SfM gauge."""
    if session.get("imported_splat"):
        return None
    dataset = session.get("sfm_dataset") or {}
    dataset_dir = dataset.get("dataset_dir")
    artifacts = session.get("artifacts") or {}
    if not dataset_dir or not splat_path or not artifacts.get("raw_splat_path"):
        return None
    if _Path(splat_path).resolve() != _Path(artifacts["raw_splat_path"]).resolve():
        return None
    gaussian = session.get("gaussian") or {}
    supervised_match = (
        gaussian.get("dataset_dir") == dataset_dir
        and gaussian.get("splat_path")
        and _Path(gaussian["splat_path"]).resolve() == _Path(splat_path).resolve()
    )
    if artifacts.get("splat_dataset_dir") != dataset_dir and not supervised_match:
        return None
    if not _dataset_matches(dataset, _session_image_paths(session["session_id"])):
        return None
    return str(dataset["sparse_dir"])


def _materialize_metric_splat(session: dict) -> None:
    """Publish a transformed PLY from the immutable native cloud, never twice."""
    transform = session.get("recon_transform")
    artifacts = session.setdefault("artifacts", {})
    raw = artifacts.get("raw_splat_path") or artifacts.get("splat_path")
    if not transform or not raw or not _Path(raw).is_file():
        return
    from services.formats.splat_io import load_splat, save_splat, transform_gaussian
    from services.vision.recon_transform import ReconTransform
    target = _session_path(session["session_id"]) / "splat" / f"metric-{_secrets.token_hex(8)}.ply"
    target.parent.mkdir(parents=True, exist_ok=True)
    cloud = transform_gaussian(load_splat(raw), ReconTransform.from_json(transform))
    fd, temporary = _tempfile.mkstemp(prefix=".metric-", dir=target.parent)
    _os.close(fd)
    try:
        save_splat(cloud, temporary, "ply")
        _os.replace(temporary, target)
    finally:
        _Path(temporary).unlink(missing_ok=True)
    artifacts.update(raw_splat_path=str(_Path(raw).resolve()), splat_path=str(target),
                     splat_units="mm", splat_transform_applied=True)


def _metric_mesh(session: dict) -> tuple[Optional[str], Optional[dict]]:
    """Materialize an already confirmed transform once for CAD and deviation."""
    artifacts = session.get("artifacts") or {}
    path = artifacts.get("mesh_path")
    if not path:
        return None, {"ok": False, "reason": "no_mesh"}
    if artifacts.get("mesh_transform_applied"):
        return path, None
    transform = session.get("recon_transform")
    if not transform or not transform.get("confirmed"):
        return None, {"ok": False, "reason": "scale_unconfirmed",
                      "hint": "Confirm a reconstruction scale before building or measuring a CAD part."}
    source_sha256 = _file_sha256(str(path))
    from services.vision.mesh_cleanup import apply_transform_to_mesh
    target = _session_path(session["session_id"]) / "mesh" / f"metric-{_secrets.token_hex(8)}.ply"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(".pending-" + target.name)
    try:
        result = apply_transform_to_mesh(path, transform, str(temporary))
        if not result.get("ok"):
            return None, result
        _os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    artifacts.update(raw_mesh_path=artifacts.get("raw_mesh_path") or path,
                     mesh_path=str(target), mesh_units="mm", mesh_transform_applied=True,
                     mesh_materialization={"source_sha256": source_sha256,
                                           "mesh_sha256": _file_sha256(str(target)),
                                           "recon_transform": transform})
    session["artifacts"] = artifacts
    _save_session(session)
    return str(target), None


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
    quality: str = "normal",
    backend: Optional[str] = None,
    progress: Optional[Any] = None,
):
    """Return a deterministic node ``agent_fn`` that runs the RE pipeline stages.

    ``agent_fn(request, context_pack) -> AgentResult`` is the seam node_runner
    dispatches every non-verify/non-gate node through. Rather than invoke an LLM,
    this dispatcher wires each node to the real vision backends, degrading honestly
    to a non-success result (which the node ESCALATES on per its on_fail) when a
    backend module or its provisioning is absent. It never re-invokes the reveng
    MCP launch tools (that would recurse).

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
            root = _session_path(session_id)
            session_dir = str(root)
            s = _load_session(session_id, merge_gaussian=False) or _make_session(session_id, session_id, None)
            dataset = s.get("sfm_dataset") or {}
            if _dataset_matches(dataset, images):
                return True, "reused shared sparse dataset", {
                    "sparse": {"ok": True, "registered": dataset.get("registered", 0),
                               "backend": "shared_dataset"},
                    "sparse_dir": dataset["sparse_dir"], "used_ffwd": False,
                    "dataset_dir": dataset["dataset_dir"], "reused": True,
                }
            if (dataset or s.get("recon_transform") or (s.get("scale") or {}).get("mm_per_unit")
                    or (s.get("artifacts") or {}).get("mesh_path")
                    or (s.get("artifacts") or {}).get("doc_id")):
                # A new solve has an unrelated gauge; no old scale or geometry is valid.
                _invalidate_downstream(s)
                s.pop("recon_transform", None)
                s.pop("axis_alignment", None)
                s.pop("sfm_dataset", None)
                s["scale"] = {"method": None, "mm_per_unit": None,
                              "residual_pct": None, "confirmed": False}
                for key in ("sparse_dir", "mesh_path", "raw_mesh_path", "mesh_units",
                            "mesh_transform_applied", "splat_path", "raw_splat_path",
                            "splat_units", "splat_transform_applied", "splat_dataset_dir",
                            "mesh_materialization"):
                    s.setdefault("artifacts", {}).pop(key, None)
                s.setdefault("provenance", []).append({
                    "step": "invalidate_sfm_gauge", "reason": "capture inputs changed",
                    "ts": _now_iso(),
                })
                _save_session(s)
            work_base = root / "sfm"
            work_base.mkdir(parents=True, exist_ok=True)
            work = _Path(_tempfile.mkdtemp(prefix="solve-", dir=work_base))
            input_dir = work / "input_images"
            input_dir.mkdir()
            source_map = {}
            signature = _source_signature(images)
            for index, source in enumerate(images):
                src = _Path(source)
                name = f"{index:06d}_{src.name}"
                _shutil.copy2(src, input_dir / name)
                source_map[name] = str(src.resolve())
            if signature != _source_signature(images):
                return False, "capture_changed", {"detail": "input images changed during snapshot"}
            image_dir = str(input_dir)
            solve_dir = str(work / "solve")
            _Path(solve_dir).mkdir()
            staged_images = [str(input_dir / name) for name in source_map]

            sparse_result: dict = {"ok": False, "reason": "sparse_not_attempted"}
            used_ffwd = False

            # Primary sparse: COLMAP / pycolmap
            try:
                from services.vision.colmap_pipeline import run_sparse  # noqa: PLC0415
                from services.vision.frames import clip_camera_groups  # noqa: PLC0415
            except Exception as exc:  # noqa: BLE001
                run_sparse = None  # type: ignore[assignment]
                sparse_result = {
                    "ok": False,
                    "reason": "backend_not_provisioned",
                    "hint": f"colmap_pipeline import failed: {exc}",
                }
            if run_sparse is not None:
                try:
                    sparse_result = run_sparse(
                        image_dir, solve_dir, single_camera=False,
                        camera_groups=clip_camera_groups(source_map)) or {
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
                    from services.vision.colmap_pipeline import read_model
                    ffwd_root = work / "ffwd"
                    ffwd = run_ffwd_sfm(staged_images, str(ffwd_root)) or {
                        "ok": False, "reason": "run_ffwd_sfm returned empty",
                    }
                    if ffwd.get("ok"):
                        candidate = ffwd.get("sparse_dir") or str(ffwd_root / "sparse")
                        _, _, points = read_model(candidate)
                        if points:
                            ffwd["sparse_dir"] = candidate
                            sparse_result = ffwd
                            used_ffwd = True
                        elif not sparse_result.get("ok"):
                            sparse_result = {"ok": False, "reason": "ffwd_has_no_sparse_points",
                                             "hint": "Feed-forward poses alone cannot train or reconstruct a surface."}
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

            sparse_dir = sparse_result.get("sparse_dir") or str(_Path(solve_dir) / "sparse")
            try:
                from services.vision.colmap_pipeline import prepare_dataset
                dataset = prepare_dataset(sparse_dir, image_dir, str(work / "dataset"),
                                          source_image_map=source_map) or {}
            except Exception as exc:
                return False, "dataset_preparation_failed", {"detail": str(exc)}
            if not dataset.get("ok"):
                return False, dataset.get("reason") or "dataset_preparation_failed", dataset
            dataset["source_signature"] = signature
            dataset["source_images"] = images
            s = _load_session(session_id, merge_gaussian=False) or s
            s["sfm_dataset"] = dataset
            s.setdefault("artifacts", {})["sparse_dir"] = dataset["sparse_dir"]
            s["stages"]["reconstruct"] = "running"
            _save_session(s)
            return True, "sparse model registered", {
                "sparse": sparse_result,
                "sparse_dir": dataset["sparse_dir"],
                "dataset_dir": dataset["dataset_dir"],
                "submitted": len(images), "registration_ratio": _registration_ratio(sparse_result, len(images)),
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
            """Build visual splats and a surface from the same immutable SfM gauge."""
            session_dir = str(_session_path(session_id))
            s = _load_session(session_id, merge_gaussian=False) or {}
            dataset = s.get("sfm_dataset") or {}
            dataset_dir = dataset.get("dataset_dir")
            if not dataset_dir or not _dataset_matches(dataset, _session_image_paths(session_id)):
                return False, "no_shared_dataset", {
                    "hint": "Run sparse reconstruction for the current capture set first."}
            sparse_dir = dataset["sparse_dir"]
            notes = []
            mesh_path = None
            approximate = False
            splat_path = (s.get("artifacts") or {}).get("raw_splat_path")
            if splat_path and not _splat_sparse_dir(_load_session(session_id) or s, splat_path):
                # Existing clouds without matching provenance belong to another gauge.
                splat_path = None

            if backend not in ("brush", "splat"):
                _progress("dense", 45, "Dense MVS (OpenMVS) started")
                try:
                    from services.vision.dense_backends import run_openmvs
                    omvs = run_openmvs(
                        dataset_dir, session_dir,
                        working_resolution={"draft": 2, "normal": 1, "high": 0}[quality],
                        texture_size={"draft": 1024, "normal": 2048, "high": 4096}[quality]) or {}
                    if omvs.get("ok") and omvs.get("mesh_path"):
                        mesh_path = omvs["mesh_path"]
                        notes.append("openmvs")
                    else:
                        notes.append("openmvs:" + str(omvs.get("reason") or "failed"))
                except Exception as exc:
                    notes.append(f"openmvs:{exc}")
            if not splat_path or not _Path(splat_path).is_file():
                _progress("splat", 50, "Training Gaussian splat from shared cameras")
                try:
                    from services.vision.gaussian_pipeline import train_splat_from_dataset
                    from services.reveng.gaussian_jobs import gpu_slot
                    with gpu_slot(_session_path(session_id)):
                        trained = train_splat_from_dataset(
                            dataset_dir, str(_Path(session_dir) / "splat" / ("training-" + _secrets.token_hex(4))),
                            iterations={"draft": 500, "normal": 2000, "high": 7000}[quality])
                    if trained.get("ok") and trained.get("splat_path"):
                        splat_path = trained["splat_path"]
                    else:
                        notes.append("splat:" + str(trained.get("reason") or "failed"))
                except Exception as exc:
                    notes.append(f"splat:{exc}")
            if splat_path:
                artifacts = s.setdefault("artifacts", {})
                artifacts.update(raw_splat_path=splat_path, splat_path=splat_path,
                                 splat_units="reconstruction", splat_transform_applied=False,
                                 splat_dataset_dir=dataset_dir)
                try:
                    _materialize_metric_splat(s)
                except Exception as exc:
                    notes.append(f"splat_transform:{exc}")
                _save_session(s)

            if not mesh_path and backend != "openmvs" and splat_path:
                _progress("mesh", 58, "Extracting approximate surface from Gaussian centers")
                try:
                    from services.formats.splat_io import load_splat, splat_to_mesh
                    out = _Path(session_dir) / "mesh" / f"splat-approx-{_secrets.token_hex(8)}.ply"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    converted = splat_to_mesh(load_splat(splat_path), str(out),
                                              method="auto", sparse_dir=sparse_dir) or {}
                    if converted.get("ok") and converted.get("mesh_path"):
                        mesh_path = converted["mesh_path"]
                        approximate = True
                        notes.append("splat_to_mesh:approximate")
                    else:
                        notes.append("splat_to_mesh:" + str(converted.get("reason") or "failed"))
                except Exception as exc:
                    notes.append(f"splat_to_mesh:{exc}")
            if not mesh_path:
                return False, "no_mesh", {"sparse": sparse_result, "dense_notes": notes,
                    "used_ffwd": used_ffwd, "hint": "Install OpenMVS or Brush and Open3D; "
                    "a Gaussian PLY is not itself a measurement surface."}

            cleaned = mesh_path
            transform_applied = False
            transform_json = s.get("recon_transform")
            transform = None
            if transform_json and (not require_confirmed_scale or transform_json.get("confirmed")):
                from services.vision.recon_transform import ReconTransform
                transform = ReconTransform.from_json(transform_json)
            _progress("mesh", 62, "Cleaning measurement surface", mesh_path)
            try:
                from services.vision.mesh_cleanup import cleanup_mesh
                out_clean = str(_Path(session_dir) / "mesh" / f"cleaned-{_secrets.token_hex(8)}.ply")
                _Path(out_clean).parent.mkdir(parents=True, exist_ok=True)
                cleaned_result = cleanup_mesh(mesh_path, out_clean, recon_transform=transform) or {}
                if cleaned_result.get("ok") and cleaned_result.get("mesh_path"):
                    cleaned = cleaned_result["mesh_path"]
                    transform_applied = bool(transform and cleaned_result.get("scale_applied"))
                else:
                    notes.append("cleanup:" + str(cleaned_result.get("reason") or "failed"))
            except Exception as exc:
                notes.append(f"cleanup:{exc}")
            mesh_units = "mm" if transform_applied else "reconstruction"
            s = _load_session(session_id, merge_gaussian=False) or s
            _invalidate_downstream(s)
            s.setdefault("artifacts", {}).pop("mesh_materialization", None)
            s.setdefault("artifacts", {}).update(
                sparse_dir=sparse_dir, raw_mesh_path=mesh_path, mesh_path=cleaned,
                mesh_units=mesh_units, mesh_transform_applied=transform_applied)
            s.setdefault("honesty", {})["approximate"] = approximate
            s["stages"]["reconstruct"] = "done"
            s["provenance"].append({"step": provenance_step, "sparse_backend": sparse_result.get("backend"),
                                    "used_ffwd": used_ffwd, "dense": notes, "mesh_units": mesh_units,
                                    "approximate": approximate, "ts": _now_iso()})
            _save_session(s)
            _progress("mesh", 64, f"mesh ready ({mesh_units})", cleaned)
            return True, "reconstruction complete", {
                "sparse": sparse_result, "mesh_path": cleaned, "mesh_units": mesh_units,
                "splat_path": (s.get("artifacts") or {}).get("splat_path"),
                "approximate": approximate, "used_ffwd": used_ffwd, "dense_notes": notes}

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
            if s.get("recon_transform") and not s["recon_transform"].get("confirmed"):
                _invalidate_downstream(s)
            scale["confirmed"] = True
            if s.get("recon_transform"):
                s["recon_transform"]["confirmed"] = True
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
                fit_provenance = _fit_provenance(s)
                fits = segment_fit(mesh_path, session_id=session_id,
                                   recon_transform=s.get("recon_transform"),
                                   transform_applied=bool((s.get("artifacts") or {}).get("mesh_transform_applied")),
                                   approximate=False) or {}
            except Exception as exc:  # noqa: BLE001
                return _Result(False, f"segment_fit error: {exc}")
            if not fits.get("ok"):
                return _Result(
                    False,
                    fits.get("reason") or "backend_not_provisioned: segment_fit",
                    fits,
                )
            latest = _load_session(session_id, merge_gaussian=False)
            try:
                unchanged = latest is not None and _fit_provenance(latest) == fit_provenance
            except (OSError, ValueError):
                unchanged = False
            if not unchanged:
                if latest is not None and _load_current_fits(latest) is None:
                    _invalidate_downstream(latest)
                    _save_session(latest)
                return _Result(False, "fit_input_changed", {
                    "ok": False, "reason": "fit_input_changed",
                    "hint": "The mesh or calibration changed during fitting; fit again."})
            s = latest
            _invalidate_downstream(s)
            fits["provenance"] = fit_provenance
            fits["surface_approximate"] = bool((s.get("honesty") or {}).get("approximate"))
            fits["approximate"] = bool(fits.get("approximate") or fits["surface_approximate"])
            # Persist aicad.reveng_fits/1 for n_parametric / GUI
            fits_path = _session_path(session_id) / "fits.json"
            try:
                fits_path.write_text(_json.dumps(fits, indent=2))
            except Exception as exc:  # noqa: BLE001
                _save_session(s)
                return _Result(False, "fits_persistence_error", {
                    "ok": False, "reason": "fits_persistence_error", "hint": str(exc)})
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
            mesh_path, metric_error = _metric_mesh(s)
            if metric_error:
                return _Result(False, metric_error.get("reason", "metric_mesh_failed"), metric_error)

            # Never resurrect a detached or stale fits.json from disk.
            hints = _load_current_fits(s)

            # The mesh is already in metric coordinates; CAD-Recode computes its own
            # normalization and inverse, not the reconstruction similarity transform.
            scale_transform = None

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
                # A generated STEP or source text is not itself an editable rebuild.
                doc_id = ml.get("doc_id")
                step_path = ml.get("step_path")
                re_res = None
                if not doc_id and step_path and _Path(str(step_path)).is_file():
                    re_res = _call_reverse_engineer(
                        str(step_path), name=s.get("name"),
                        hints=hints if isinstance(hints, dict) else None)
                    if (re_res.get("ok") and re_res.get("doc_id")
                            and re_res.get("features")):
                        doc_id = re_res["doc_id"]
                        ml = {**ml, "reverse_engineer": re_res}
                elif doc_id:
                    try:
                        from services.mcp import core_bridge as cb
                        bodies = cb.call("asm_world_bodies", {"doc_id": doc_id, "explode": False}).get("bodies")
                        tree = cb.call("doc_tree", {"doc_id": doc_id}).get("tree") or {}
                        if not bodies or not tree.get("features"):
                            doc_id = None
                    except Exception as exc:
                        ml.update(ok=False, reason="build_verification_failed", hint=str(exc))
                        from .reveng_build_tools import _persist_build_result
                        _persist_build_result(session_id, strategy="ml", state="failed",
                                              payload=ml, reason=ml["reason"])
                        return _Result(False, ml["reason"], ml)
                if not doc_id:
                    detail = re_res if isinstance(re_res, dict) else {}
                    reason = detail.get("reason") or detail.get("error") or "no_features_rebuilt"
                    ml = {**ml, "ok": False, "reason": reason, "match_pct": None,
                          "summary": detail.get("summary") or "no features rebuilt",
                          "recognition": detail.get("recognition"), "reverse_engineer": detail}
                    from .reveng_build_tools import _persist_build_result
                    _persist_build_result(session_id, strategy="ml", state="failed",
                                          payload=ml, reason=reason)
                    return _Result(False, reason, ml)
                ml["doc_id"] = doc_id
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
                s["stages"]["model"] = "done"
                s["provenance"].append({
                    "step": "n_parametric",
                    "strategy": "ml",
                    "surface_approximate": bool((s.get("honesty") or {}).get("approximate")),
                    "ts": _now_iso(),
                })
                _save_session(s)
                ml["surface_approximate"] = bool((s.get("honesty") or {}).get("approximate"))
                from .reveng_build_tools import _persist_build_result
                _persist_build_result(session_id, strategy="ml", state="built", payload=ml)
                return _Result(True, "ml parametric complete", ml)

            # Default: deterministic reverse_engineer with fit hints
            re_res = _call_reverse_engineer(
                mesh_path,
                name=s.get("name"),
                hints=hints if isinstance(hints, dict) else None,
            )
            if isinstance(re_res.get("features"), list) and not re_res["features"]:
                re_res.update(ok=False, reason="no_features_rebuilt", match_pct=None)
                re_res.setdefault("summary", "no features rebuilt — see recognition.unclaimed")
            if not re_res.get("ok") or not re_res.get("doc_id"):
                reason = (re_res.get("reason") or re_res.get("error")
                          or "no_features_rebuilt")
                re_res["ok"] = False
                re_res["reason"] = reason
                from .reveng_build_tools import _persist_build_result
                _persist_build_result(session_id, strategy="deterministic", state="failed",
                                      payload=re_res, reason=reason)
                return _Result(False, reason, re_res)
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
                "surface_approximate": bool((s.get("honesty") or {}).get("approximate")),
                "ts": _now_iso(),
            })
            _save_session(s)
            re_res["surface_approximate"] = bool((s.get("honesty") or {}).get("approximate"))
            from .reveng_build_tools import _persist_build_result
            _persist_build_result(session_id, strategy="deterministic", state="built", payload=re_res)
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
    quality: str = "normal",
    backend: Optional[str] = None,
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
        "quality": quality,
        "backend": backend,
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
        session_id, strategy=strategy, quality=quality, backend=backend, progress=progress,
    )

    def _invoke() -> dict:
        return executor.run(
            graph,
            initial_state={"inputs": {"session_id": session_id, "strategy": strategy,
                                      "quality": quality, "backend": backend}},
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
        "quality": quality,
        "backend": backend,
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
        quality=rec.get("quality") or "normal",
        backend=rec.get("backend"),
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
