"""Reverse-engineering session + ingest MCP tools (split out of reveng_tools.py): session CRUD/status/coverage/deviation, images/video/datasheet ingest, scale setting, scale-mat print."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional, Union
from pathlib import Path as _Path
import json as _json
import math as _math
import os as _os
import re as _re
import hashlib as _hashlib
import tempfile as _tempfile
import shutil as _shutil

from .cad_common import coerce_json_or_repr, require_list, require_mapping, resolve_agent_path
from .reveng_common import (
    _dumps,
    _err,
    _extract_frames_safe,
    _find_reference_models,
    _has_reference_match,
    _ingest_pdf,
    _invalidate_downstream,
    _load_current_fits,
    _load_session,
    _list_sessions,
    _make_session,
    _mission_running,
    _new_session_id,
    _not_provisioned,
    _start_reference_search,
    _now_iso,
    _research_ingest,
    _reveng_status,
    _reset_transform_artifacts,
    _save_session,
    _session_image_paths,
    _session_path,
    _store_reference_search,
    _session_summary,
)


# ─────────────────────────────────────────────────────────────────────────────
# Capture-coverage analysis
# ─────────────────────────────────────────────────────────────────────────────

#: Variance-of-Laplacian floor below which a still is judged too blurry to use. The
#: same number is ``services.vision.frames.DEFAULT_BLUR_THRESHOLD`` and
#: ``chudware::platform::kSharpnessBlurThreshold`` (host/src/platform/camera.h, the
#: live capture HUD) — capture and audit must agree on what "blurry" means. The video
#: culler instead ranks frames against their own clip (``frames.RELATIVE_BLUR_FRACTION``).
BLUR_THRESHOLD: float = 100.0

#: The guided orbit the capture GUI draws — two rings plus the top shot, as
#: ``(azimuth_deg, elevation_deg)``. Mirrors ``kShotPlan`` in
#: host/src/ui/reveng/webcam_capture.cpp: index i here IS shot position i there.
SHOT_PLAN_11: tuple = (
    (0.0, 30.0), (36.0, 30.0), (72.0, 30.0), (108.0, 30.0), (144.0, 30.0),
    (18.0, 60.0), (54.0, 60.0), (90.0, 60.0), (126.0, 60.0), (162.0, 60.0),
    (0.0, 90.0),
)

#: Spin steps swept when fitting the shot plan onto a reconstruction's arbitrary frame.
_PLAN_SPIN_STEPS: int = 72
_MAX_SLOT_ERROR_DEG: float = 25.0



def _file_sig(path: _Path) -> str:
    """Cheap change stamp for the blur cache: mtime + size."""
    st = path.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def _score_session_blur(session: dict, paths: list) -> tuple:
    """Variance-of-Laplacian score per still, cached in the session manifest.

    Returns ``(scores, dirty, note)``. ``scores`` maps image path → score and simply
    omits anything that could not be scored — an unscored image is never reported as
    sharp. ``dirty`` says the cache changed and the manifest is worth re-saving.
    ``note`` carries the honest reason when OpenCV is absent or a file would not decode.
    Cache rows are keyed by path and validated against mtime+size, so a repeat call
    rescans nothing.
    """
    cache = session.setdefault("analysis", {}).setdefault("blur", {})
    scores: dict = {}
    sigs: dict = {}
    stale: list = []
    dirty = False
    for p in paths:
        try:
            sigs[p] = _file_sig(_Path(p))
        except OSError:
            continue
        hit = cache.get(p)
        if (isinstance(hit, dict) and hit.get("sig") == sigs[p]
                and isinstance(hit.get("score"), (int, float))):
            scores[p] = float(hit["score"])
        else:
            stale.append(p)
    for gone in [k for k in cache if k not in sigs]:
        del cache[gone]
        dirty = True
    if not stale:
        return scores, dirty, None
    try:
        from services.vision.fiducial import require_cv2  # noqa: PLC0415
        from services.vision.frames import blur_score  # noqa: PLC0415
        cv2 = require_cv2()
    except Exception as exc:  # noqa: BLE001
        return scores, dirty, (
            f"sharpness not scored ({exc}); pip install opencv-python to grade blur"
        )
    undecodable = 0
    for p in stale:
        try:
            img = cv2.imread(p)
            score = None if img is None else float(blur_score(img))
        except Exception:  # noqa: BLE001
            score = None
        if score is None:
            undecodable += 1
            continue
        scores[p] = score
        cache[p] = {"sig": sigs[p], "score": round(score, 3)}
        dirty = True
    note = (
        f"{undecodable} still(s) could not be decoded for blur scoring"
        if undecodable else None
    )
    return scores, dirty, note


def _rot_about(np, axis, angle: float):
    """Rodrigues rotation matrix about ``axis`` (normalised here)."""
    k = axis / (float(np.linalg.norm(axis)) + 1e-12)
    K = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
    return np.eye(3) + _math.sin(angle) * K + (1.0 - _math.cos(angle)) * (K @ K)


def _rot_between(np, a, b):
    """Minimal rotation carrying unit vector ``a`` onto unit vector ``b``."""
    v = np.cross(a, b)
    s = float(np.linalg.norm(v))
    c = float(np.dot(a, b))
    if s < 1e-12:
        if c > 0.0:
            return np.eye(3)
        seed = np.array([0.0, 1.0, 0.0]) if abs(a[0]) > 0.9 else np.array([1.0, 0.0, 0.0])
        return _rot_about(np, np.cross(a, seed), _math.pi)
    vx = np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]])
    return np.eye(3) + vx + (vx @ vx) * ((1.0 - c) / (s * s))


def _plan_directions(np):
    """The 11 shot-plan slots as unit vectors in a canonical Z-up frame."""
    rows = []
    for az_deg, el_deg in SHOT_PLAN_11:
        az, el = _math.radians(az_deg), _math.radians(el_deg)
        rows.append([
            _math.cos(el) * _math.cos(az),
            _math.cos(el) * _math.sin(az),
            _math.sin(el),
        ])
    return np.asarray(rows, dtype="float64")


def _orbit_center(np, centers, axes):
    """Least-squares point closest to every camera's viewing axis — the part's centre."""
    A = np.zeros((3, 3), dtype="float64")
    b = np.zeros(3, dtype="float64")
    for c, d in zip(centers, axes):
        M = np.eye(3) - np.outer(d, d)
        A += M
        b += M @ c
    try:
        p = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        return centers.mean(axis=0)
    return p if bool(np.all(np.isfinite(p))) else centers.mean(axis=0)


def _bin_views_to_plan(np, dirs):
    """Assign each unit view direction to its nearest shot-plan slot.

    A COLMAP world frame has an arbitrary gauge, so "nearest slot" only means something
    once the plan is fitted to the data. Two of the three rotational degrees of freedom
    are pinned by mapping the plan's mean direction onto the observed mean direction;
    the remaining spin about that axis is swept coarsely and then refined around the
    winner, so the reported residual measures the capture, not the sweep's step size.
    Returns per-view nearest slots and errors, plus the nearest error for every slot.
    """
    plan = _plan_directions(np)
    plan_mean = plan.mean(axis=0)
    plan_mean = plan_mean / float(np.linalg.norm(plan_mean))
    view_mean = dirs.mean(axis=0)
    view_norm = float(np.linalg.norm(view_mean))
    if view_norm < 1e-9:
        # Views cancel out (a full sphere) — no mean to align to; keep the raw frame.
        axis, base = np.array([0.0, 0.0, 1.0]), np.eye(3)
    else:
        axis = view_mean / view_norm
        base = _rot_between(np, plan_mean, axis)
    rows = np.arange(len(dirs))

    def _fit(spin: float):
        R = _rot_about(np, axis, spin) @ base
        cos = np.clip(dirs @ (plan @ R.T).T, -1.0, 1.0)
        idx = np.argmax(cos, axis=1)
        err = np.arccos(cos[rows, idx])
        return float(err.sum()), idx, err, np.arccos(cos.max(axis=0))

    coarse = 2.0 * _math.pi / _PLAN_SPIN_STEPS
    best_spin, best = 0.0, _fit(0.0)
    for step in range(1, _PLAN_SPIN_STEPS):
        spin = step * coarse
        cand = _fit(spin)
        if cand[0] < best[0]:
            best_spin, best = spin, cand
    # Refine inside the winning bucket: the coarse grid alone would floor the residual
    # at half a step (~2.5°) and make a good orbit look sloppier than it is.
    for step in range(-10, 11):
        spin = best_spin + step * (coarse / 10.0)
        cand = _fit(spin)
        if cand[0] < best[0]:
            best = cand
    return best[1], np.degrees(best[2]), np.degrees(best[3])


def _resolve_sparse_dir(session_id: str, session: dict):
    """Locate a COLMAP binary or text model for a session."""
    candidates: list = []
    raw = (session.get("artifacts") or {}).get("sparse_dir")
    if raw:
        p = _Path(raw)
        candidates.append(p if p.is_absolute() else _session_path(session_id) / raw)
    candidates.append(_session_path(session_id) / "sparse")
    for root in candidates:
        if not root.is_dir():
            continue
        if any((root / f"cameras.{ext}").is_file()
               and (root / f"images.{ext}").is_file() for ext in ("bin", "txt")):
            return root
        for sub in sorted(root.iterdir()):
            if sub.is_dir() and any(
                (sub / f"cameras.{ext}").is_file()
                and (sub / f"images.{ext}").is_file() for ext in ("bin", "txt")
            ):
                return sub
    return None


def _pose_bin_session(session_id: str, session: dict, paths: list) -> tuple:
    """Bin registered views into shot-plan slots from the COLMAP poses.

    Returns ``(slots, errors_deg, nearest_slot_errors, note)``. ``slots`` maps image
    paths to nearest slots, or is ``None`` when no usable sparse model exists.
    """
    sparse_dir = _resolve_sparse_dir(session_id, session)
    if sparse_dir is None:
        return None, {}, None, (
            "no COLMAP model on disk, so coverage is an image-count estimate; run "
            "reveng_reconstruct first"
        )
    try:
        import numpy as np  # noqa: PLC0415
        from services.vision.scale_solver import read_colmap_sparse  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return None, {}, None, f"pose binning unavailable ({exc}); pip install numpy"
    try:
        cams = read_colmap_sparse(str(sparse_dir))
    except Exception as exc:  # noqa: BLE001
        return None, {}, None, f"sparse model unreadable ({exc})"
    name_map = (session.get("sfm_dataset") or {}).get("image_name_map") or {}
    by_path = {str(_Path(p).resolve()): p for p in paths}
    by_name = {_Path(p).name: p for p in paths}
    named: list = []
    centers: list = []
    axes: list = []
    for name, cam in cams.items():
        source = name_map.get(name)
        path = by_path.get(str(_Path(source).resolve())) if source else None
        path = path or by_name.get(_Path(name).name)
        if path is None:
            continue
        named.append(path)
        centers.append(cam.center)
        axes.append(cam.R[2, :])          # camera +Z (the view axis) in world coords
    if len(named) < 2:
        return {p: None for p in named}, {}, [None] * len(SHOT_PLAN_11), (
            f"{len(named)} of this session's stills are registered in the sparse model; "
            "need at least 2 poses to bin coverage"
        )
    centers = np.asarray(centers, dtype="float64")
    axes = np.asarray(axes, dtype="float64")
    axes = axes / (np.linalg.norm(axes, axis=1, keepdims=True) + 1e-12)
    dirs = centers - _orbit_center(np, centers, axes)
    norms = np.linalg.norm(dirs, axis=1, keepdims=True)
    if float(norms.min()) < 1e-9:
        return ({p: None for p in named}, {}, [None] * len(SHOT_PLAN_11),
                "degenerate rig: a camera sits on the estimated part centre")
    idx, err, nearest = _bin_views_to_plan(np, dirs / norms)
    return (
        {p: int(i) for p, i in zip(named, idx)},
        {p: float(e) for p, e in zip(named, err)},
        [round(float(e), 2) for e in nearest],
        None,
    )


def _coverage_quality(covered: int, sharp_slots: int, sharp_fraction) -> str:
    """Grade coverage from real signals: slots reached, and how many of them are sharp.

    ``sharp_fraction`` is ``None`` when nothing could be scored — an unmeasured set is
    not penalised, but it cannot lift a partial orbit to ``good`` either.
    """
    if covered >= len(SHOT_PLAN_11) and (sharp_fraction is None or sharp_fraction >= 0.8):
        return "good"
    if covered >= 5 and sharp_slots >= 5:
        return "partial"
    return "insufficient"


# ─────────────────────────────────────────────────────────────────────────────
# Per-session dimension ledger (list_reveng_dims)
# ─────────────────────────────────────────────────────────────────────────────

#: Half-width tolerance band implied by each honesty tier (contracts/reveng.md §0,
#: same numbers as ``services.vision.measure._TIER_REL_UNCERT``). ``approximate`` is
#: deliberately absent: an unscaled number has no honest band.
_TIER_REL_TOL: dict = {"high": 0.02, "medium": 0.05, "low": 0.10}

#: intake tier ladder (services/intake/intake.py TIER_ORDER) → reveng §0 tiers. A typed
#: caliper ("measured") is at least ``high``; reveng's vocabulary tops out there.
_INTAKE_TIER_MAP: dict = {
    "measured": "high", "high": "high", "medium": "medium", "low": "low",
}

#: intake ``resolved_by`` → contracts/reveng.md §0 ``provenance.method`` vocabulary.
_RESOLVED_METHOD: dict = {
    "user_confirmed": "user_confirmed",
    "vision_high": "photo_fiducial",
    "reference": "datasheet",
    "assumed": "guessed",
}

#: Trailing unit annotation on a datasheet column header, e.g. ``"OD (mm)"``.
_UNIT_RE = _re.compile(r"[\(\[]\s*(mm|cm|m|in(?:ch(?:es)?)?)\s*[\)\]]", _re.IGNORECASE)


def _tier_tolerance(value_mm, tier: str, floor_mm=None):
    """Tolerance half-band for a value at ``tier``, never tighter than ``floor_mm``."""
    rel = _TIER_REL_TOL.get(tier)
    if rel is None or value_mm is None:
        return round(float(floor_mm), 4) if floor_mm else None
    tol = rel * abs(float(value_mm))
    if floor_mm:
        tol = max(tol, float(floor_mm))
    return round(tol, 4)


def _add_dim_row(rows: list, *, key: str, label: str, value_mm, tier: str, source: str,
                 method: str, tolerance_mm=None, confirmed: bool = False,
                 fit_critical: bool = False, note=None, extra=None) -> None:
    """Append one ledger row.

    ``key`` / ``tier`` / ``required`` are the aliases the Review-stage parser
    (host/src/ui/reveng/dim_review.cpp) already reads; ``id`` / ``label`` /
    ``honesty_tier`` / ``fit_critical`` are the v2 names. Same data, both spellings, so
    the GUI wave can migrate without a flag day.
    """
    row = {
        "id": f"d{len(rows) + 1}",
        "key": key,
        "label": label,
        "value_mm": None if value_mm is None else round(float(value_mm), 4),
        "tolerance_mm": (tolerance_mm if tolerance_mm is not None
                         else _tier_tolerance(value_mm, tier)),
        "source": source,
        "provenance_method": method,
        "honesty_tier": tier,
        "tier": tier,
        "confirmed": bool(confirmed),
        "fit_critical": bool(fit_critical),
        "required": bool(fit_critical),
    }
    if note:
        row["note"] = note
    if extra:
        row.update(extra)
    rows.append(row)


def _datasheet_dims(session_id: str, rows: list):
    """Rows (a): dimension candidates the datasheet / research lane already extracted.

    ``research_bundle.json`` holds what the web+PDF lane parsed; ``datasheet.json`` holds
    the raw docling/pdfplumber tables from a user-supplied PDF. A number read straight
    off a manufacturer datasheet is the strongest evidence this pipeline ever gets
    (tier ``high``); one scraped out of page prose is a ``low``-tier hint.
    """
    ds_dir = _session_path(session_id) / "datasheet"
    if not ds_dir.is_dir():
        return None
    try:
        from services.vision.datasheet_ingest import _DIM_KEYWORDS  # noqa: PLC0415
        from services.vision.research_ingest import _dim_mm  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return f"datasheet dims skipped ({exc})"
    # "mass"/"weight" are dimension-TABLE hints, not lengths — never emit them as mm.
    dim_headers = set(_DIM_KEYWORDS) - {"mass", "weight"}
    seen: set = set()
    used: dict = {}

    def _emit(label, value, source: str, tier: str) -> None:
        text = (str(label or "")).strip() or "dim"
        if value is None:
            return
        marker = (text.lower(), round(float(value), 3))
        if marker in seen:
            return
        seen.add(marker)
        # Two sources can disagree about the same label (a bundle says OD 42, a table
        # says OD 47). That conflict is exactly what a reviewer must see, so keep both
        # and give each a unique key — ``key`` is what confirm_dimensions round-trips.
        base = f"datasheet.{text.lower().replace(' ', '_')}"
        used[base] = used.get(base, 0) + 1
        key = base if used[base] == 1 else f"{base}_{used[base]}"
        _add_dim_row(
            rows, key=key, label=text, value_mm=value, tier=tier, source=source,
            method="datasheet",
        )

    bundle = ds_dir / "research_bundle.json"
    if bundle.is_file():
        try:
            data = _json.loads(bundle.read_text())
        except Exception:  # noqa: BLE001
            data = {}
        for cand in data.get("candidate_dims") or []:
            if not isinstance(cand, dict):
                continue
            web = (cand.get("provenance") or "datasheet") == "web"
            _emit(cand.get("label"), cand.get("mm"),
                  "web" if web else "datasheet", "low" if web else "high")

    sheet = ds_dir / "datasheet.json"
    if sheet.is_file():
        try:
            data = _json.loads(sheet.read_text())
        except Exception:  # noqa: BLE001
            data = {}
        for table in data.get("tables") or []:
            if not isinstance(table, dict) or not table.get("is_dimension_table"):
                continue
            headers = [str(h) for h in (table.get("headers") or [])]
            for row in table.get("rows") or []:
                for header, cell in zip(headers, row or []):
                    unit = _UNIT_RE.search(header)
                    label = _UNIT_RE.sub("", header).strip()
                    if label.lower().rstrip(".") not in dim_headers:
                        continue      # part-number / qty / page columns are not dims
                    mm = _dim_mm(cell, unit.group(1).lower() if unit else "mm")
                    if mm is not None and 0.05 <= mm <= 2000.0:
                        _emit(label, mm, "datasheet", "high")
    return None


def _intake_dims(session_id: str, session: dict, rows: list):
    """Rows (c): photo-measured dims + user-confirmed values from the intake ledger.

    ``measure_from_photo`` lands each read in the doc's ``measured_parts`` / ``crit_dims``
    meta (services/mcp/intake_tools.py) tiered by the fiducial's own optics — surfaced
    here at exactly the tier it earned, never re-scored.
    """
    doc_id = (session.get("artifacts") or {}).get("doc_id")
    if not doc_id:
        return None
    try:
        from services.mcp import core_bridge as cb  # noqa: PLC0415
        measured = cb.call(
            "doc_get_meta", {"doc_id": doc_id, "key": "measured_parts"}
        ).get("value")
        ledger = cb.call(
            "doc_get_meta", {"doc_id": doc_id, "key": "crit_dims"}
        ).get("value")
    except Exception as exc:  # noqa: BLE001
        return f"photo-measured dims unavailable ({exc}); engine bridge not reachable"

    crit: dict = {}
    for row in (ledger or {}).get("rows") or []:
        if isinstance(row, dict) and row.get("key"):
            crit[str(row["key"])] = row

    profile = measured[-1] if isinstance(measured, list) and measured else None
    # Resolve each intake photo back to a still in the session gallery so the Review
    # stage can jump to the image a measurement came from. Absent when it does not
    # resolve — an unmatched photo is left unlinked rather than guessed at.
    gallery = {_Path(p).name: (i, p) for i, p in enumerate(_session_image_paths(session_id))}
    photo_by_id: dict = {}
    for img in (profile or {}).get("images") or []:
        if not isinstance(img, dict) or not img.get("photo_id"):
            continue
        for cand in (img.get("preview_path"), img.get("resource_link")):
            hit = gallery.get(_Path(str(cand)).name) if cand else None
            if hit:
                photo_by_id[img["photo_id"]] = hit
                break
    claimed: set = set()
    for feat in (profile or {}).get("features") or []:
        if not isinstance(feat, dict):
            continue
        fkey = feat.get("key") or "feature"
        prov = feat.get("provenance") or {}
        tier = _INTAKE_TIER_MAP.get(feat.get("confidence"), "low")
        uncert = feat.get("uncert_mm")
        for dim_name, value in (feat.get("dims") or {}).items():
            # ``dims`` stores lengths with an ``_mm`` suffix and metadata bare
            # ({"spacing_mm": 18.0, "count": 2}) — a hole COUNT is not a millimetre.
            if not str(dim_name).endswith("_mm"):
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            key = f"{fkey}.{dim_name}"
            ledger_key = f"{fkey}.{str(dim_name)[:-3]}"    # taxonomy keys are bare
            crow = crit.get(key) or crit.get(ledger_key) or {}
            if crow:
                claimed.add(crow.get("key") or key)
            hit = photo_by_id.get(prov.get("photo_id"))
            extra = {"image_index": hit[0], "image_path": hit[1]} if hit else {}
            confirmed = crow.get("resolved_by") == "user_confirmed"
            typed = crow.get("nominal_mm") if confirmed else None
            if isinstance(typed, (int, float)) and not isinstance(typed, bool):
                # A typed caliper value supersedes the photo reading for the SAME dim —
                # one row, the authoritative number, with the photo read kept as context.
                extra["photo_value_mm"] = round(float(value), 4)
                value, tier_row = float(typed), "high"
                source, method, tol = "typed", "user_confirmed", None
            else:
                tier_row, source = tier, "photo"
                method = prov.get("method") or "photo_fiducial"
                tol = uncert if isinstance(uncert, (int, float)) else None
            _add_dim_row(
                rows, key=key, label=key.replace("_", " "), value_mm=value,
                tier=tier_row, source=source, method=method,
                tolerance_mm=tol if tol is not None else _tier_tolerance(value, tier_row),
                confirmed=confirmed, fit_critical=bool(crow.get("is_critical", False)),
                extra=extra or None,
            )

    for key, crow in crit.items():
        if key in claimed or crow.get("status") == "unknown":
            continue      # already surfaced above, or demanded-but-unanswered
        value = crow.get("nominal_mm")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        resolved = crow.get("resolved_by")
        confirmed = resolved == "user_confirmed"
        _add_dim_row(
            rows, key=key, label=key.replace("_", " "), value_mm=value,
            tier="high" if confirmed else _INTAKE_TIER_MAP.get(
                crow.get("required_tier"), "low"),
            source="typed" if confirmed else "intake",
            method=_RESOLVED_METHOD.get(resolved, "guessed"),
            confirmed=confirmed, fit_critical=bool(crow.get("is_critical", False)),
        )
    return None


def _fits_dims(session_id: str, session: dict, rows: list):
    """Rows (b): primitives RANSAC-fitted to the mesh (``aicad.reveng_fits/1``).

    A cylinder yields a diameter and a length; a parallel plane pair yields a thickness.
    Those numbers are only millimetres when the mesh was scaled at cleanup, so an
    unscaled (or single-photo ``approximate``) session gets tier ``approximate`` and a
    note saying the value is in reconstruction units — never a fake measurement.
    """
    data = _load_current_fits(session)
    if data is None:
        if (session.get("artifacts") or {}).get("fits_path"):
            return "primitive fits are stale or unreadable; run reveng_fit_primitives again"
        return None
    fits = [f for f in (data.get("fits") or []) if isinstance(f, dict)]
    if not fits:
        return None

    scaled = (session.get("scale") or {}).get("mm_per_unit") is not None
    approximate = bool((session.get("honesty") or {}).get("approximate"))
    if scaled and not approximate:
        tier, note = "medium", None
    elif not scaled:
        tier = "approximate"
        note = ("unscaled reconstruction units, not mm — run reveng_set_scale then "
                "reveng_fit_primitives again")
    else:
        tier = "approximate"
        note = "single-photo mesh: proportions only, not a measurement"

    planes: list = []
    n_cyl = 0
    for fit in fits:
        params = fit.get("params_mm") or {}
        resid = fit.get("residual_mm")
        resid = float(resid) if isinstance(resid, (int, float)) else None
        if fit.get("kind") == "cylinder":
            tag = (fit.get("face_ids") or [f"cyl_{n_cyl}"])[0]
            n_cyl += 1
            radius, height = params.get("radius"), params.get("height")
            summary = f"cylinder r={float(radius):.2f}" if isinstance(
                radius, (int, float)) else "cylinder"
            if isinstance(height, (int, float)):
                summary += f" h={float(height):.2f}"
            if isinstance(radius, (int, float)) and radius > 0:
                dia = 2.0 * float(radius)
                _add_dim_row(
                    rows, key=f"fit.{tag}.diameter", label=f"{tag} diameter",
                    value_mm=dia, tier=tier, source="fitted", method="fitted",
                    tolerance_mm=_tier_tolerance(dia, tier, resid), note=note,
                    extra={"fit_summary": summary},
                )
            if isinstance(height, (int, float)) and height > 0:
                _add_dim_row(
                    rows, key=f"fit.{tag}.length", label=f"{tag} length",
                    value_mm=float(height), tier=tier, source="fitted", method="fitted",
                    tolerance_mm=_tier_tolerance(height, tier, resid), note=note,
                    extra={"fit_summary": summary},
                )
        elif fit.get("kind") == "plane":
            normal, origin = params.get("normal"), params.get("origin")
            if (isinstance(normal, list) and len(normal) == 3
                    and isinstance(origin, list) and len(origin) == 3):
                planes.append((fit, [float(x) for x in normal], [float(x) for x in origin]))

    # A slab shows up as two peeled planes with (anti)parallel normals: their separation
    # along the shared normal IS the wall thickness.
    pairs: list = []
    for i in range(len(planes)):
        for j in range(i + 1, len(planes)):
            (fa, na, oa), (fb, nb, ob) = planes[i], planes[j]
            if abs(sum(x * y for x, y in zip(na, nb))) < 0.98:
                continue
            gap = abs(sum(n * (b - a) for n, a, b in zip(na, oa, ob)))
            if gap < 0.05:
                continue      # the same face peeled twice, not a thickness
            conf = float(fa.get("confidence") or 0.0) + float(fb.get("confidence") or 0.0)
            resid = max(float(fa.get("residual_mm") or 0.0),
                        float(fb.get("residual_mm") or 0.0))
            pairs.append((conf, gap, resid, i, j))
    pairs.sort(key=lambda t: -t[0])
    kept: list = []
    for _conf, gap, resid, i, j in pairs:
        if len(kept) >= 3:
            break
        if any(abs(gap - g) <= max(0.1, 0.02 * g) for g in kept):
            continue          # same slab measured off a different plane pairing
        kept.append(gap)
        tag_a = (planes[i][0].get("face_ids") or [f"plane_{i}"])[0]
        tag_b = (planes[j][0].get("face_ids") or [f"plane_{j}"])[0]
        _add_dim_row(
            rows, key=f"fit.{tag_a}_{tag_b}.thickness",
            label=f"{tag_a}/{tag_b} thickness", value_mm=gap, tier=tier,
            source="fitted", method="fitted",
            tolerance_mm=_tier_tolerance(gap, tier, resid or None), note=note,
            extra={"fit_summary": f"parallel plane pair, gap {gap:.2f}"},
        )
    return None


def collect_session_dims(session_id: str) -> dict:
    """Assemble the Review-stage dimension ledger for *session_id*.

    Shared by the ``list_reveng_dims`` MCP tool and the GUI worker bridge so both
    transports read one implementation. Sources stack strongest-evidence-first —
    datasheet, then photo/intake measurement, then primitives fitted to the mesh — and a
    session that genuinely has no dimensions returns an empty list, never demo rows.
    """
    session = _load_session(session_id)
    if session is None:
        return {
            "ok": False,
            "reason": f"session {session_id!r} not found",
            "hint": "call list_reveng_sessions for the live session ids",
        }
    rows: list = []
    notes = []
    for note in (
        _datasheet_dims(session_id, rows),
        _intake_dims(session_id, session, rows),
        _fits_dims(session_id, session, rows),
    ):
        if note:
            notes.append(note)
    out: dict = {
        "ok": True,
        "session_id": session_id,
        "dims": rows,
        "sources": sorted({r["source"] for r in rows}),
        "scale_confirmed": bool((session.get("scale") or {}).get("confirmed")),
    }
    if notes:
        out["notes"] = notes
    if not rows:
        out["hint"] = (
            "no dimensions recorded yet — ingest a datasheet (reveng_ingest_datasheet), "
            "measure a photo (measure_from_photo), or fit primitives "
            "(reveng_fit_primitives)"
        )
    return out


def _ingest_stills(session_id: str, session: dict, sources: list) -> tuple:
    """Publish verified stills atomically without overwriting same-named sources."""
    from services.vision.fiducial import require_cv2
    from services.vision.frames import blur_score

    cv2 = require_cv2()
    root = _session_path(session_id).resolve()
    images_dir = root / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    before = set(_session_image_paths(session_id))
    rows = {r["path"]: r for r in session.get("source_images", []) if r.get("path")}
    accepted, failed = [], []
    for source in dict.fromkeys(str(_Path(p).expanduser().resolve()) for p in sources):
        src = _Path(source)
        try:
            if not src.is_file():
                raise FileNotFoundError(f"image not found: {src}")
            if src.suffix.lower() not in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"):
                raise ValueError(f"unsupported image type: {src.suffix}")
            image = cv2.imread(str(src))
            if image is None or image.size == 0:
                raise ValueError(f"could not decode image: {src}")
            score = float(blur_score(image))
            if not _math.isfinite(score):
                raise ValueError(f"invalid image sharpness: {src}")
            with src.open("rb") as stream:
                digest = _hashlib.file_digest(stream, "sha256").hexdigest()
            # Captures already published inside this session remain in place.
            dest = src if src.parent in (images_dir, root / "frames") else (
                images_dir / f"still_{digest}{src.suffix.lower()}"
            )
            if not dest.is_file() or dest.stat().st_size == 0:
                temporary = None
                try:
                    with _tempfile.NamedTemporaryFile(prefix=".ingest-", dir=images_dir, delete=False) as stream:
                        temporary = _Path(stream.name)
                        with src.open("rb") as input_stream:
                            _shutil.copyfileobj(input_stream, stream)
                    _os.replace(temporary, dest)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            path = str(dest)
            row = rows.setdefault(path, {"path": path, "source_path": source, "source_paths": []})
            if source not in row.setdefault("source_paths", []):
                row["source_paths"].append(source)
            row.update(blur_score=round(score, 3), content_sha256=digest, kind="image")
            session.setdefault("analysis", {}).setdefault("blur", {})[path] = {
                "sig": _file_sig(dest), "score": round(score, 3),
            }
            accepted.append(dict(row, source_path=source))
        except Exception as exc:
            failed.append({"path": source, "reason": str(exc)})
    session["source_images"] = list(rows.values())
    current = set(_session_image_paths(session_id))
    session["artifacts"]["images"] = len(current)
    session["stages"]["capture"] = "done" if current else "idle"
    return len({row["path"] for row in accepted} - before), accepted, failed


def _source_image_gallery(session_id: str, session: dict) -> list:
    """Rehydrate the gallery from files, retaining video-frame provenance."""
    paths = _session_image_paths(session_id)
    rows = {r["path"]: dict(r) for r in session.get("source_images", []) if r.get("path")}
    for entry in session.get("provenance", []):
        for frame in entry.get("frames", []):
            if isinstance(frame, dict) and frame.get("path"):
                rows.setdefault(frame["path"], dict(frame, kind="video"))
    scores, _, _ = _score_session_blur(session, paths)
    gallery = []
    for path in paths:
        row = rows.get(path, {"path": path, "kind": "video" if _Path(path).parent.name == "frames" else "image"})
        row["blur_score"] = scores.get(path)
        gallery.append(row)
    session["artifacts"]["images"] = len(paths)
    return gallery




def register_reveng_session_tools(mcp: FastMCP) -> None:
    """Reverse-engineering session + ingest MCP tools (split out of reveng_tools.py): session CRUD/status/coverage/deviation, images/video/datasheet ingest, scale setting, scale-mat print."""


    # ── §8.1 READ ────────────────────────────────────────────────────────────

    @mcp.tool()
    def list_reveng_sessions(project_dir: Optional[str] = None) -> str:
        """List sessions belonging to the requested or current project, if known.

        Returns ``{ok, sessions: [session_summary]}`` where each summary has
        session_id, name, status, created_at, updated_at, part_ref, images.
        """
        sessions = [_session_summary(s) for s in _list_sessions(project_dir)]
        return _dumps({"ok": True, "sessions": sessions})



    @mcp.tool()
    def get_reveng_session(session_id: str) -> str:
        """Return the full ``aicad.reveng_session/1`` manifest for *session_id*.

        Returns ``{ok, session, source_images}``; gallery paths refer to files on disk.
        """
        s = _load_session(session_id)
        if s is None:
            return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
        job = s.get("mesh_job")
        if isinstance(job, dict) and job.get("status") == "running" and not str(
                _mission_running(session_id) or "").startswith("splat_edit_"):
            # A background mesh holds its session claim until it records the outcome, so a
            # running job without that claim died with an earlier worker.
            job.update(status="failed", finished_at=_now_iso(),
                       error="interrupted: the worker stopped before meshing finished")
            _save_session(s)
        source_images = _source_image_gallery(session_id, s)
        return _dumps({"ok": True, "session": s, "source_images": source_images})



    @mcp.tool()
    def get_reveng_coverage(session_id: str) -> str:
        """Return measured capture coverage against the 11-shot orbit plan.

        Returns ``{ok, coverage: {plan, captured, missing, blur_flags, quality, method,
        images, plan_slots, slot_counts, ...}}``. ``captured`` / ``missing`` are shot
        position indices (0–10) into the same ``kShotPlan`` table the capture GUI draws.

        ``method`` says how coverage was determined and MUST be surfaced:
        ``"pose_binned"`` when a COLMAP model is available (a view counts only
        within 25 degrees of its nearest slot; unresolved poses claim no coverage),
        or ``"count_proxy"`` without a usable model (one still assumed to cover
        one slot — an estimate, not a measurement). ``blur_flags[i]`` describes ``captured[i]`` and is
        only ever true where a still was actually scored below the shared
        variance-of-Laplacian threshold (100.0); an unscored image is never called sharp
        or blurry, and the reason lands in ``notes``.
        """
        s = _load_session(session_id)
        if s is None:
            return _dumps({
                "ok": False,
                "reason": f"session {session_id!r} not found",
                "hint": "call list_reveng_sessions for the live session ids",
            })
        paths = _session_image_paths(session_id)
        scores, cache_dirty, blur_note = _score_session_blur(s, paths)
        if cache_dirty:
            _save_session(s)

        notes = [n for n in (blur_note,) if n]
        plan_size = len(SHOT_PLAN_11)
        slots, bin_err, nearest_errors, pose_note = _pose_bin_session(session_id, s, paths)
        if pose_note:
            notes.append(pose_note)

        def _blurred(path: str):
            """True/False once scored, None when we could not score it."""
            score = scores.get(path)
            return None if score is None else score < BLUR_THRESHOLD

        slot_counts = [0] * plan_size
        if slots is not None:
            method = "pose_binned"
            usable = [False] * plan_size   # ≥1 view in this slot that is not known-blurry
            for path, slot in slots.items():
                if slot is None or path not in bin_err or bin_err[path] > _MAX_SLOT_ERROR_DEG:
                    continue
                slot_counts[slot] += 1
                if _blurred(path) is not True:
                    usable[slot] = True
            captured = [i for i in range(plan_size) if slot_counts[i] > 0]
            blur_flags = [not usable[i] for i in captured]
        else:
            method = "count_proxy"
            captured = list(range(min(len(paths), plan_size)))
            for i in captured:
                slot_counts[i] = 1
            blur_flags = [_blurred(paths[i]) is True for i in captured]

        covered = set(captured)
        missing = [i for i in range(plan_size) if i not in covered]
        n_sharp = sum(1 for p in paths if _blurred(p) is False)
        n_blurred = sum(1 for p in paths if _blurred(p) is True)
        n_scored = n_sharp + n_blurred
        sharp_fraction = (n_sharp / n_scored) if n_scored else None
        sharp_slots = len(captured) - sum(1 for f in blur_flags if f)

        images = []
        for i, path in enumerate(paths):
            score = scores.get(path)
            images.append({
                "index": i,
                "path": path,
                "name": _Path(path).name,
                "blur_score": round(score, 2) if score is not None else None,
                "blurred": _blurred(path),
                "registered": (path in slots) if slots is not None else None,
                "slot": slots.get(path) if slots is not None else None,
                "bin_error_deg": round(bin_err[path], 2) if slots is not None and path in bin_err else None,
            })

        coverage = {
            "plan": "11_shot",
            "captured": captured,
            "missing": missing,
            "blur_flags": blur_flags,
            "quality": _coverage_quality(len(captured), sharp_slots, sharp_fraction),
            "method": method,
            "plan_slots": [
                {"index": i, "azimuth_deg": az, "elevation_deg": el}
                for i, (az, el) in enumerate(SHOT_PLAN_11)
            ],
            "slot_counts": slot_counts,
            "images": images,
            "n_images": len(paths),
            "n_registered": len(slots) if slots is not None else 0,
            "n_scored": n_scored,
            "n_sharp": n_sharp,
            "n_blurred": n_blurred,
            "blur_threshold": BLUR_THRESHOLD,
        }
        if slots is not None:
            coverage["max_slot_error_deg"] = _MAX_SLOT_ERROR_DEG
            coverage["nearest_slot_error_deg"] = nearest_errors
        if slots is not None and bin_err:
            coverage["mean_bin_error_deg"] = round(
                sum(bin_err.values()) / len(bin_err), 2
            )
        if notes:
            coverage["notes"] = notes
        return _dumps({"ok": True, "coverage": coverage})

    @mcp.tool()
    def reveng_sparse_preview(session_id: str) -> str:
        """Return at most 5000 sparse points and camera frusta in one coordinate frame.

        ``points`` and ``frusta`` use millimetres only when scale is confirmed;
        otherwise both remain in the native reconstruction gauge. ``total_points``
        counts finite points before sampling and ``sparse_points_path`` identifies
        the complete sparse point artifact.
        """
        try:
            import numpy as np
            from services.vision.colmap_pipeline import read_model
            from services.vision.scale_solver import _intrinsic_matrix, _quat_to_rot
            from services.vision.recon_transform import ReconTransform

            session = _load_session(session_id, merge_gaussian=False)
            if session is None:
                return _dumps({"ok": False, "reason": "session not found"})
            transform = (ReconTransform.from_json(session["recon_transform"])
                         if session.get("recon_transform") else None)
            confirmed = bool(transform and transform.confirmed)
            display_transform = transform if confirmed else None
            out = {"ok": True, "points": [], "frusta": [], "total_points": 0,
                   "sparse_points_path": None,
                   "recon_transform": session.get("recon_transform") if confirmed else None,
                   "units": "mm" if confirmed else "reconstruction",
                   "scale_confirmed": confirmed,
                   "mesh_preview_path": None, "warnings": []}
            sparse = _resolve_sparse_dir(session_id, session)
            if sparse:
                for filename in ("points3D.bin", "points3D.txt"):
                    if (sparse / filename).is_file():
                        out["sparse_points_path"] = str(sparse / filename)
                        break
                cameras, images, points = read_model(sparse)
                rows = list(points.values())
                invalid_points = sum(not np.isfinite(p.xyz).all() for p in rows)
                if invalid_points:
                    rows = [p for p in rows if np.isfinite(p.xyz).all()]
                    out["warnings"].append(f"Skipped {invalid_points} non-finite sparse points.")
                out["total_points"] = len(rows)
                if len(rows) > 5000:
                    rows = [rows[i] for i in np.linspace(0, len(rows) - 1, 5000, dtype=int)]
                xyz = np.asarray([p.xyz for p in rows], dtype=float).reshape(-1, 3)
                display = display_transform.apply(xyz) if display_transform else xyz
                out["points"] = [list(map(float, pos)) + list(map(int, p.rgb))
                                 for pos, p in zip(display, rows)]
                extent = float(np.linalg.norm(np.ptp(xyz, axis=0))) if len(xyz) else 1.0
                depth = max(extent * 0.04, 1e-6)
                image_rows = list(images.values())
                if len(image_rows) > 256:
                    image_rows = [image_rows[i] for i in np.linspace(0, len(image_rows) - 1, 256, dtype=int)]
                camera_errors = set()
                for image in image_rows:
                    try:
                        camera = cameras[image.camera_id]
                        K = _intrinsic_matrix(camera.model, camera.params)
                        if not np.isfinite(K).all() or K[0, 0] <= 0 or K[1, 1] <= 0:
                            raise ValueError(f"invalid intrinsics for {camera.model}")
                        rotation = _quat_to_rot(*image.qvec)
                        center = -rotation.T @ np.asarray(image.tvec)
                        pixels = [(0, 0), (camera.width, 0),
                                  (camera.width, camera.height), (0, camera.height)]
                        corners = [center + rotation.T @ np.array(
                            [(x - K[0, 2]) * depth / K[0, 0],
                             (y - K[1, 2]) * depth / K[1, 1], depth]) for x, y in pixels]
                        coords = np.asarray([center, *corners])
                        if display_transform:
                            coords = display_transform.apply(coords)
                        if coords.shape != (5, 3) or not np.isfinite(coords).all():
                            raise ValueError("non-finite camera frustum")
                        out["frusta"].append(coords.reshape(-1).tolist())
                    except (KeyError, ValueError, IndexError, TypeError) as exc:
                        camera_errors.add(str(exc))
                out["warnings"].extend(f"Camera frustum skipped: {error}"
                                       for error in sorted(camera_errors))
            mesh = (session.get("artifacts") or {}).get("mesh_path")
            if mesh and _Path(mesh).is_file():
                try:
                    import trimesh
                    stat = _Path(mesh).stat()
                    key = _hashlib.sha256(
                        f"{mesh}:{stat.st_mtime_ns}:{stat.st_size}:{_json.dumps(session.get('recon_transform'), sort_keys=True)}".encode()
                    ).hexdigest()[:20]
                    preview = _session_path(session_id) / "preview" / f"{key}.stl"
                    if not preview.is_file():
                        preview.parent.mkdir(parents=True, exist_ok=True)
                        geometry = trimesh.load(mesh, force="mesh", process=False)
                        if display_transform and not (session.get("artifacts") or {}).get("mesh_transform_applied"):
                            geometry.vertices = display_transform.apply(geometry.vertices)
                        geometry.export(str(preview), file_type="stl")
                    out["mesh_preview_path"] = str(preview)
                    out["mesh_preview_artifact"] = {
                        "path": str(preview), "size_bytes": preview.stat().st_size, "kind": "binary_stl"}
                except Exception as exc:
                    out["warnings"].append(f"mesh preview unavailable: {exc}")
            splat = (session.get("artifacts") or {}).get("splat_path")
            if splat and _Path(splat).is_file():
                out["splat_artifact"] = {
                    "path": splat, "size_bytes": _Path(splat).stat().st_size, "kind": "gaussian_ply"}
            return _dumps(out)
        except Exception as exc:
            return _err(exc)



    @mcp.tool()
    def get_reveng_deviation(
        session_id: str,
        ref_path: Optional[str] = None,
        brep_doc_id: Optional[str] = None,
        n_samples: int = 20_000,
        alignment: str = "world",
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
            Absolute path to a reference mesh; optional with ``brep_doc_id``.
        brep_doc_id:
            Optional B-Rep document to tessellate for comparison.
        n_samples:
            Surface sample count for KD-tree probe (default 20 000).
        alignment:
            ``world`` compares in the current frame; ``icp`` explicitly aligns first.
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
        if ref_path is not None and not _os.path.exists(ref_path):
            return _dumps({
                "ok": False,
                "reason": f"ref_path not found: {ref_path!r}",
                "hint": "check the file path",
            })
        try:
            from .reveng_common import _metric_mesh
            mesh_path, metric_error = _metric_mesh(s)
        except Exception as exc:  # noqa: BLE001
            return _dumps({"ok": False, "reason": f"metric mesh conversion failed: {exc}"})
        if metric_error:
            return _dumps(metric_error)

        try:
            from services.vision.deviation import compute_deviation
            result = compute_deviation(
                mesh_path,
                ref_path,
                session_id=session_id,
                brep_doc_id=brep_doc_id,
                alignment=alignment,
                n_samples=n_samples,
            )
        except Exception as exc:  # noqa: BLE001
            return _dumps({"ok": False, "reason": f"deviation computation failed: {exc}"})
        if isinstance(result, dict):
            result["surface_approximate"] = bool((s.get("honesty") or {}).get("approximate"))

        return _dumps(result)



    @mcp.tool()
    def describe_reveng_result(session_id: str) -> str:
        """Describe the reverse-engineering result for *session_id*.

        Returns ``{ok, result: {doc_id, match_pct, strategy, mesh_fallback, provenance,
        scored, state, built_at, reason}}``. ``match_pct`` and ``strategy`` are read back
        from what ``reveng_build_parametric`` persisted under ``session["result"]`` — this
        tool reports, it never scores.

        The Model stage renders one of five distinct outcomes, so read ``state`` (never
        just a null ``match_pct``):

        * ``ok: false``, ``reason: "no result yet"`` — nothing built and no doc attached.
        * ``state: "running"`` — a build mission is in flight; the score lands later.
        * ``state: "built"`` + ``scored: true`` — ``match_pct`` is a real number.
        * ``state: "built"`` + ``scored: false`` — it built, but comparison degraded.
        * ``state: "failed"`` — the build ran and failed; ``reason`` says why.
        * ``state: "attached"`` — a doc exists but no build in this session produced it,
          so there is nothing to score against.
        """
        s = _load_session(session_id)
        if s is None:
            return _dumps({
                "ok": False,
                "reason": f"session {session_id!r} not found",
                "hint": "call list_reveng_sessions for the live session ids",
            })
        built = s.get("result") or {}
        doc_id = (s.get("artifacts") or {}).get("doc_id") or built.get("doc_id")
        if not doc_id and not built:
            return _dumps({
                "ok": False,
                "reason": "no result yet",
                "hint": "run reveng_build_parametric first",
            })
        state = built.get("state") or ("attached" if doc_id else "unknown")
        match_pct = built.get("match_pct") if state == "built" else None
        result = {
            "doc_id": doc_id,
            "match_pct": match_pct,
            "strategy": built.get("strategy"),
            "mesh_fallback": bool(built.get("mesh_fallback", False)),
            "provenance": s.get("provenance", []),
            "scored": match_pct is not None,
            "state": state,
            "built_at": built.get("built_at"),
        }
        if built.get("summary"):
            result["summary"] = built["summary"]
        if match_pct is None:
            if built.get("reason"):
                result["reason"] = built["reason"]
            elif state == "running":
                result["reason"] = "build still running; poll mission_status for the run"
            elif built:
                result["reason"] = (
                    "unscored — no comparison score was recorded for this result"
                )
            else:
                result["reason"] = (
                    "no build recorded for this session; the doc was attached by "
                    "another path, so there is nothing to score against"
                )
        return _dumps({"ok": True, "result": result})



    @mcp.tool()
    def list_reveng_dims(session_id: str) -> str:
        """Return the dimension ledger the Review stage confirms against.

        Returns ``{ok, dims: [{id, key, label, value_mm, tolerance_mm, source,
        provenance_method, honesty_tier, confirmed, fit_critical, ...}]}``.

        Rows are assembled from whatever the session actually has, strongest evidence
        first: datasheet-extracted candidates (tier ``high``; web-scraped prose ``low``),
        photo-measured intake claims at the tier ``services/vision/measure.py`` scored
        them, and primitives fitted to the mesh (tier ``medium`` on a scaled mesh,
        ``approximate`` when the session is unscaled or single-photo). ``key`` is the
        ``confirm_dimensions`` ledger key to send back.

        A session with no dimensions yet returns ``{ok: true, dims: []}`` plus a hint —
        this tool never invents rows.
        """
        try:
            return _dumps(collect_session_dims(session_id))
        except Exception as e:
            return _err(e)



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

    @mcp.tool()
    def reveng_install_tools(tools: Optional[list[str]] = None) -> str:
        """Download and install the missing Reverse Engineering tools (Brush splat trainer,
        OpenMVS dense mesher) into the per-user tools directory. Both are SHA-256-pinned
        upstream releases run only as subprocesses; about 210 MB in total.

        ``tools`` is a subset of ``["brush", "openmvs"]`` (default: every missing one). Returns
        at once with ``{ok, install: {running, tool, message, done, total, installed, errors}}``;
        the download runs in the background, so poll ``reveng_status().install`` until
        ``running`` is false, then confirm ``provisioning``. Idempotent while a run is in flight
        and a no-op when nothing is missing. Linux x86_64 only; elsewhere the error says to set
        ``BRUSH_BIN`` / ``OPENMVS_BIN``.
        """
        try:
            from services.vision.reveng_install import start_install, supported
            if not supported():
                return _dumps({"ok": False, "reason": "unsupported_platform",
                               "hint": "Pinned binaries are Linux x86_64 only; set BRUSH_BIN / "
                                       "OPENMVS_BIN to your own builds."})
            return _dumps({"ok": True, "install": start_install(tools)})
        except ValueError as e:
            return _dumps({"ok": False, "reason": "invalid_tools", "hint": str(e)})
        except Exception as e:
            return _err(e)



    # ── §8.2 WRITE ───────────────────────────────────────────────────────────

    @mcp.tool()
    def reveng_find_models(session_id: str, query: str, limit: int = 8,
                           wait: bool = True) -> str:
        """Search CAD libraries (GrabCAD, TraceParts, Printables, McMaster, ...) for an existing
        model of a part BEFORE building one, and record the result as session.reference.

        Returns ``{ok, session_id, query, candidates:[{title, site, url, confidence, attach}],
        standard_part, coverage, hint}``. A URL query is returned as its own candidate. The
        fan-out renders many vendor pages and can take a minute or more; ``wait=False`` returns
        ``{ok, session_id, query, pending: true}`` at once and the result appears in
        get_reveng_session's ``session.reference`` (``status`` searching → done | failed).
        """
        try:
            session = _load_session(session_id)
            if session is None:
                return _dumps({"ok": False, "reason": "not_found", "hint": "Session not found."})
            session["lane"] = "reference"
            query = str(query or "").strip()
            if not wait and query:
                _start_reference_search(session, query, limit)
                return _dumps({"ok": True, "session_id": session_id, "query": query,
                               "pending": True})
            result = _find_reference_models(query, limit=limit)
            if result.get("ok"):
                _store_reference_search(session, result)
            else:
                _save_session(session)
            return _dumps({**result, "session_id": session_id})
        except Exception as e:
            return _err(e)

    @mcp.tool()
    def reveng_use_model(session_id: str, url: Optional[str] = None,
                         path: Optional[str] = None) -> str:
        """Use an existing CAD file or acquire a URL as this session's reference model."""
        try:
            from services.agent import request_context as rc
            from services.mcp.parts_tools import _cad_format, fetch_cad_model_impl

            session = _load_session(session_id)
            if session is None:
                return _dumps({"ok": False, "reason": "not_found", "hint": "Session not found."})
            if bool(url) == bool(path):
                return _dumps({"ok": False, "reason": "invalid_input",
                               "hint": "Provide exactly one of url or path.", "session_id": session_id})
            project = session.get("project_dir")
            if url:
                # The session's working folder exactly — never an outer marked ancestor.
                result = fetch_cad_model_impl(url, name=session.get("name"), import_part=False,
                                              project_root=_Path(project) if project else None)
                if not result.get("ok"):
                    return _dumps({**result, "session_id": session_id})
                model_path = result["artifact_path"]
                fmt = result.get("format")
                provenance = result.get("provenance") or {}
                source_url = result.get("source_url") or url
                site = provenance.get("site")
                digest = provenance.get("sha256")
                license_note = provenance.get("license")
            else:
                if project:
                    with rc.bind_roots(project):
                        model_path = resolve_agent_path(path, "path")
                else:
                    model_path = resolve_agent_path(path, "path")
                model_file = _Path(model_path)
                fmt = _cad_format(model_path)
                if not model_file.is_file() or not fmt or fmt == "zip":
                    return _dumps({"ok": False, "reason": "invalid_model",
                                   "hint": "Provide an existing supported CAD file.", "session_id": session_id})
                with model_file.open("rb") as stream:
                    digest = _hashlib.file_digest(stream, "sha256").hexdigest()
                source_url = site = license_note = None
            ts = _now_iso()
            model = {"path": model_path, "format": fmt, "source_url": source_url,
                     "site": site, "sha256": digest, "license": license_note, "ts": ts}
            session.setdefault("reference", {})["model"] = model
            session.setdefault("artifacts", {})["reference_model_path"] = model_path
            session.setdefault("stages", {}).update({"capture": "done", "model": "done"})
            session["status"] = "done"
            session["lane"] = "reference"
            session.setdefault("provenance", []).append({"step": "use_reference_model", **model})
            _save_session(session)
            return _dumps({"ok": True, "session_id": session_id, "path": model_path,
                           "format": fmt, "source_url": source_url, "site": site})
        except Exception as e:
            return _err(e)


    @mcp.tool()
    def reveng_start_session(
        name: Optional[str] = None,
        part_ref: Optional[str] = None,
        research: Optional[str] = None,
        project_dir: Optional[str] = None,
    ) -> str:
        """Create a new reverse engineering session.

        Args:
            name: Human label for this session (defaults to a timestamp label).
            part_ref: Optional parts-hub ``part_id`` when seeded from the library.
                When supplied the session is seeded with any photos and datasheet
                paths already recorded on the PartRecord (``metadata.photos`` and
                ``metadata.datasheet``).
            research: Optional seed for the web/part-number lane — pass a part number,
                search query, or vendor URL. Search for existing CAD before research.
            project_dir: Working folder owning this session.

        Returns ``{ok, session_id, session}`` (plus ``research_result`` when the seed
        was provided).
        """
        try:
            sid = _new_session_id()
            label = name or f"RE session {_now_iso()}"
            session = _make_session(sid, label, part_ref, project_dir)
            if research:
                session["lane"] = "reference"
            _save_session(session)

            # RE-23: seed photos / datasheet from the parts-hub entry when
            # part_ref is supplied and carries embedded asset paths.
            seeded: dict = {}
            if part_ref:
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
                                copied, _, failures = _ingest_stills(sid, session, photos)
                                seeded["images"] = copied
                                if failures:
                                    seeded["image_failures"] = failures
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
            if part_ref and not seeded.get("images"):
                session["lane"] = "reference"
                _save_session(session)

            result: dict = {"ok": True, "session_id": sid, "session": session, "seeded": seeded}
            if research:
                reference = _find_reference_models(research)
                result["reference"] = reference
                if reference.get("ok"):
                    _store_reference_search(session, reference)
                if not reference.get("ok") or not _has_reference_match(reference):
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
        paths: Optional[Union[list, str]] = None,
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
            # Counts are rebuilt from disk, never incremented from attempted copies.
            sources: list[_Path] = []
            if dir:
                src = _Path(dir).expanduser()
                if not src.is_dir():
                    return _dumps({"ok": False, "reason": f"directory not found: {dir}"})
                sources.extend(p for p in sorted(src.iterdir()) if p.is_file()
                               and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"))
            if isinstance(paths, str) and paths.strip():
                # SILENT WRONG BEHAVIOR: `for p in paths` over a stringified/python-repr
                # list ("['a.jpg','b.jpg']") iterates CHARACTER BY CHARACTER, building
                # single-letter "paths" that all fail `.is_file()` — added stays 0 with
                # no error at all, silently reporting an empty ingest as success.
                paths = require_list(paths, "paths",
                                     'a list of image file paths, e.g. ["a.jpg", "b.jpg"]')
            if paths:
                sources.extend(_Path(p).expanduser() for p in paths)
            if not sources:
                return _dumps({"ok": False, "reason": "no image sources supplied or found"})
            added, accepted, failed = _ingest_stills(session_id, s, sources)
            _save_session(s)
            result = {"ok": not failed, "added": added, "images": accepted,
                      "failed": failed, "session": s}
            if failed:
                result.update(reason=f"{len(failed)} image source(s) could not be imported",
                              hint="valid sources were retained; check failed paths and image decoding")
            return _dumps(result)
        except Exception as e:
            return _err(e)



    @mcp.tool()
    def reveng_add_video(
        session_id: str,
        path: str,
        max_frames: int = 120,
        wait: bool = False,
    ) -> str:
        """Extract video frames into the session, without starting splat training.

        Normally returns a run_id immediately. Poll get_reveng_session for run status
        and progress; ``wait=True`` is available for a short clip or a scripted caller.
        """
        reserved = False
        try:
            from . import reveng_common as common
            s = _load_session(session_id, merge_gaussian=False)
            if s is None:
                return _dumps({"ok": False, "reason": f"session {session_id!r} not found"})
            video = common._rc.cell_path(path)
            if not video.is_file():
                return _dumps({"ok": False, "reason": f"file not found: {path}"})
            if isinstance(max_frames, bool) or not isinstance(max_frames, int) or max_frames < 1:
                return _dumps({"ok": False, "reason": "max_frames must be a positive integer"})
            existing = common._find_session_run(session_id)
            if existing and existing.get("status") in ("running", "paused"):
                return _dumps(common._busy_envelope(session_id, existing["run_id"]))
            run_id = "video_" + common._secrets.token_hex(8)
            busy = common._reserve_mission(session_id, run_id)
            if busy:
                return _dumps(common._busy_envelope(session_id, busy))
            reserved = True

            def extract() -> dict:
                before = set(_session_image_paths(session_id))
                frames_dir = str(_session_path(session_id) / "frames")
                result = _extract_frames_safe(str(video), frames_dir, max_frames=max_frames)
                current = set(_session_image_paths(session_id))
                session = _load_session(session_id, merge_gaussian=False)
                if session is None:
                    return {"ok": False, "reason": "session not found"}
                session["artifacts"]["images"] = len(current)
                session["stages"]["capture"] = "done" if current else "idle"
                if not result.get("ok"):
                    _save_session(session)
                    return dict(result, session=session)
                frames = result.get("frames", [])
                added = len({row["path"] for row in frames} - before)
                missing = [row.get("path") for row in frames if row.get("path") not in current]
                if missing:
                    _save_session(session)
                    return {"ok": False, "reason": "extracted frames are missing from disk",
                            "missing": missing, "session": session}
                rows = {r["path"]: r for r in session.get("source_images", []) if r.get("path")}
                for frame in frames:
                    rows[frame["path"]] = dict(frame, kind="video",
                                              blur_score=frame.get("sharpness"))
                session["source_images"] = list(rows.values())
                provenance = {
                    "step": "add_video", "source_path": str(video),
                    "clip_id": result.get("clip_id"), "frames": frames,
                    "frames_kept": len(frames), "frames_added": added,
                    "frames_sampled": result.get("sampled", 0),
                    "frames_rejected_blurry": result.get("rejected_blurry", 0),
                    "max_frames": max_frames, "sample_cap": result.get("sample_cap"),
                    "truncated": result.get("truncated", False),
                    "count_known": result.get("count_known", True),
                    "total_frames": result.get("total_frames", 0),
                    "warnings": result.get("warnings", []),
                }
                if not any(all(old.get(k) == v for k, v in provenance.items() if k != "frames_added")
                           for old in session["provenance"]):
                    session["provenance"].append(dict(provenance, run_id=run_id, ts=_now_iso()))
                _save_session(session)
                return {"ok": True, "run_id": run_id, "added": added,
                        "extraction": result, "session": session}

            if wait:
                try:
                    result = extract()
                    result["run_id"] = None
                    return _dumps(result)
                finally:
                    common._release_mission(session_id, run_id)

            rec = {"schema": "aicad.reveng_run/1", "run_id": run_id,
                   "_owner": common._rc.owner_key(), "session_id": session_id,
                   "template": "video_extraction", "status": "running",
                   "gate": None, "started_at": _now_iso(), "updated_at": _now_iso()}
            common._write_run_record(rec)
            common._record_session_run(rec)
            progress = common.make_reveng_stage_emitter(run_id, session_id)
            progress("capture", 0, "Extracting video frames")

            def invoke() -> dict:
                result = extract()
                if result.get("ok"):
                    progress("capture", 100, "Video frames extracted")
                    return {"status": "done"}
                return {"status": "failed", "error": result.get("reason") or "video extraction failed"}

            common._spawn_mission(session_id, run_id, invoke, rec, {})
            reserved = False
            return _dumps({"ok": True, "run_id": run_id, "session_id": session_id,
                           "status": "running", "async": True})
        except Exception as e:
            if reserved:
                common._release_mission(session_id, run_id)
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



    # ── §8.2 SCALE ───────────────────────────────────────────────────────────
    #
    # Scale is the most dimension-critical value in the whole RE pipeline: every millimetre
    # a user 3D-prints comes out of session.scale.mm_per_unit. Each method below therefore
    # runs a real solver; none of them silently copies a number the caller typed and calls
    # it a measurement.

    #: Provenance vocabulary for a caller-supplied dimension (contracts/reveng.md §0 +
    #: contracts/interaction.md). "guessed" is permitted, never silent.
    _KNOWN_DIM_SOURCES = ("caliper", "datasheet", "photo_fiducial", "guessed")

    def _scale_sparse_dir(session: dict, session_id: str) -> Optional[str]:
        """Absolute COLMAP sparse-model dir, or None when reconstruction has not run."""
        raw = (session.get("artifacts") or {}).get("sparse_dir")
        if not raw:
            return None
        p = _Path(str(raw))
        if not p.is_absolute():
            p = _session_path(session_id) / p
        return str(p) if p.is_dir() else None

    def _scale_images_dir(session_id: str) -> str:
        """Use the exact undistorted images paired with the session's sparse model."""
        session = _load_session(session_id, merge_gaussian=False) or {}
        dataset = session.get("sfm_dataset") or {}
        if dataset.get("images_dir"):
            return dataset["images_dir"]
        root = _session_path(session_id)
        imgs = root / "images"
        return str(imgs if imgs.is_dir() and any(imgs.iterdir()) else root / "frames")

    def _read_colmap_points3d(sparse_dir: str) -> list:
        """Read sparse points from either supported COLMAP representation."""
        from services.vision.colmap_pipeline import read_model
        _, _, points = read_model(sparse_dir)
        return [list(point.xyz) for point in points.values()]

    def _pixel_pair(value) -> Optional[list]:
        """Coerce a caller-supplied point to ``[x, y]`` floats, or None when malformed."""
        try:
            return [float(value[0]), float(value[1])]
        except (TypeError, ValueError, IndexError, KeyError):
            return None

    @mcp.tool()
    def reveng_set_scale(
        session_id: str,
        method: str,
        params: Optional[Union[dict, str]] = None,
    ) -> str:
        """Solve and record scale calibration for *session_id* (contracts/reveng.md §8.2).

        ``scale.mm_per_unit`` is a **ratio**: multiply reconstruction-space lengths by it to
        get millimetres. ``method`` picks a real solver:

        * ``aruco_mat`` — automatic. Detects the printed ChArUco scale mat across the session's
          registered images, triangulates every resolved chessboard corner against the COLMAP
          sparse model and fits the board layout (``scale_solver.solve_scale_charuco``);
          ``residual_pct`` is the RMS fit residual relative to the board's corner radius. A
          legacy single-marker mat is solved from its four corners when no board is found.
          Requires a reconstruction. ``params``: ``{board?: "A4"|"Letter", charuco?: {dict_name,
          squares_x, squares_y, square_len_mm, marker_len_mm}, dict_name?, marker_id?,
          marker_len_mm?, images?|images_dir?}``. The defaults are exactly what
          ``reveng_print_scale_mat`` prints, so solver and mat agree by construction.
        * ``two_point`` — manual. Two pixels of known real separation in one registered image
          (``solve_scale_two_point``). Requires a reconstruction. ``params``:
          ``{image|image_index, p1: [x,y], p2: [x,y], distance_mm,
          image_space: "undistorted"}``; use the dataset image, not the raw source.
        * ``known_dim`` — divide the physical ``value_mm`` by the same feature's
          reconstruction-space ``recon_length``. Alternatively provide ``point_a``
          and ``point_b`` as 3D reconstruction coordinates. ``source`` is one of
          ``caliper|datasheet|photo_fiducial|guessed``.

        All methods leave ``scale.confirmed = False``. After human verification,
        approve a mission gate with ``reveng_resolve_gate``, or call
        ``reveng_confirm_scale`` if there is no active mission. Returns
        ``{ok, scale, session}`` or an honest ``{ok: false, reason, hint}``.
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
            # LIVE CRASH class (cad_common.coerce_json_or_repr docstring): `params`
            # used to reach `params.get("value_mm")` below as a bare string — 'str'
            # object has no attribute 'get' — whenever a model sent it as JSON or a
            # python-repr dict instead of a real dict.
            params = require_mapping(params, "params",
                "a JSON object, e.g. {'value_mm': 25.4, 'source': 'caliper'}") if params else {}
            warnings: list = []

            if method == "known_dim":
                # A physical dimension is not a scale until paired with a model span.
                try:
                    value_mm = float(params.get("value_mm"))
                except (TypeError, ValueError):
                    return _dumps({
                        "ok": False,
                        "reason": "known_dim requires a numeric value_mm",
                        "hint": "params: {feature: str, value_mm: float, source: "
                                f"one of {_KNOWN_DIM_SOURCES}}}",
                    })
                if not _math.isfinite(value_mm) or value_mm <= 0.0:
                    return _dumps({
                        "ok": False,
                        "reason": f"value_mm must be positive, got {value_mm}",
                        "hint": "mm_per_unit is a ratio (reconstruction units → mm)",
                    })
                try:
                    if params.get("recon_length") is not None:
                        recon_length = float(params["recon_length"])
                    else:
                        a, b = params["point_a"], params["point_b"]
                        if len(a) != 3 or len(b) != 3:
                            raise ValueError("points must have three coordinates")
                        recon_length = _math.dist([float(v) for v in a], [float(v) for v in b])
                    if not _math.isfinite(recon_length) or recon_length <= 0:
                        raise ValueError("reconstruction length must be finite and positive")
                except (KeyError, TypeError, ValueError, OverflowError):
                    return _dumps({
                        "ok": False,
                        "reason": "known_dim requires a positive recon_length or two distinct 3D points",
                        "hint": "params: {value_mm, recon_length, source} or "
                                "{value_mm, point_a: [x,y,z], point_b: [x,y,z], source}",
                    })
                source = str(params.get("source") or "guessed")
                if source not in _KNOWN_DIM_SOURCES:
                    return _dumps({
                        "ok": False,
                        "reason": f"unknown scale source {source!r}",
                        "hint": f"contracts/reveng.md §0 requires one of {_KNOWN_DIM_SOURCES}",
                    })
                if source == "guessed":
                    warnings.append(
                        "scale source is 'guessed' — every dimension derived from it is "
                        "unverified; measure the part with calipers and re-run with "
                        "source='caliper', or solve it with aruco_mat / two_point"
                    )
                solved = {
                    "method": "known_dim",
                    "mm_per_unit": value_mm / recon_length,
                    "residual_pct": None,
                    "detail": {"feature": params.get("feature"), "source": source,
                               "value_mm": value_mm, "recon_length": recon_length},
                }
                prov_source = source
            else:
                # Both solvers live in services.vision — degrade honestly when absent.
                try:
                    from services.vision.fiducial import VisionUnavailable  # type: ignore
                    from services.vision.scale_solver import (  # type: ignore
                        read_colmap_sparse,
                        solve_scale_aruco,
                        solve_scale_charuco,
                        solve_scale_two_point,
                    )
                except ImportError as exc:
                    return _not_provisioned(
                        f"scale_solver not available ({exc}); "
                        "pip install opencv-python-headless numpy"
                    )

                sparse_dir = _scale_sparse_dir(s, session_id)
                if sparse_dir is None:
                    return _dumps({
                        "ok": False,
                        "reason": "scale_needs_sparse",
                        "hint": f"method {method!r} solves scale against the COLMAP sparse model "
                                "— run reveng_reconstruct(session_id) first (two_point needs it "
                                "too), or use method='known_dim' with a caliper measurement",
                    })

                if method == "aruco_mat":
                    try:
                        from services.vision.calib_sheet import BoardSpec  # type: ignore
                        from services.vision.scale_mat import MAT_BOARD, MarkerSpec  # type: ignore
                    except ImportError as exc:
                        return _not_provisioned(
                            f"scale_mat not available ({exc}); pip install opencv-python-headless"
                        )
                    board = params.get("board", "A4")
                    if board not in ("A4", "Letter"):
                        return _dumps({
                            "ok": False,
                            "reason": f"invalid board {board!r}",
                            "hint": "must be 'A4' or 'Letter'",
                        })
                    # Page size never changes the marker edge; the edge is the scale reference.
                    marker_cfg = dict(params.get("marker") or {})
                    for key in ("dict_name", "marker_id", "marker_len_mm"):
                        if params.get(key) is not None:
                            marker_cfg[key] = params[key]
                    try:
                        spec = MarkerSpec.from_dict(marker_cfg)
                        board_spec = (BoardSpec.from_dict(params["charuco"])
                                      if params.get("charuco") else MAT_BOARD)
                    except (TypeError, ValueError) as exc:
                        return _dumps({
                            "ok": False,
                            "reason": f"invalid mat spec: {exc}",
                            "hint": "params accept dict_name (cv2.aruco.DICT_*), marker_id (int), "
                                    "marker_len_mm (float) and charuco {dict_name, squares_x, "
                                    "squares_y, square_len_mm, marker_len_mm}",
                        })
                    images_dir = params.get("images_dir")
                    if not images_dir and params.get("images"):
                        dirs = {str(_Path(p).parent.resolve()) for p in params["images"]}
                        if len(dirs) != 1:
                            return _dumps({
                                "ok": False,
                                "reason": "params.images span more than one directory",
                                "hint": "the ArUco solver scans a single image directory — pass "
                                        "images from one folder, or set images_dir explicitly",
                            })
                        images_dir = dirs.pop()
                    images_dir = str(images_dir or _scale_images_dir(session_id))
                    if not _Path(images_dir).is_dir():
                        return _dumps({
                            "ok": False,
                            "reason": f"image directory not found: {images_dir}",
                            "hint": "add stills with reveng_add_images() or reveng_add_video()",
                        })
                    try:
                        try:
                            res = solve_scale_charuco(sparse_dir=sparse_dir, images_dir=images_dir,
                                                      board=board_spec)
                            mat = {"kind": "charuco", **board_spec.to_dict()}
                        except ValueError as charuco_exc:
                            try:
                                res = solve_scale_aruco(
                                    sparse_dir=sparse_dir,
                                    images_dir=images_dir,
                                    marker_len_mm=spec.marker_len_mm,
                                    dict_name=spec.dict_name,
                                    marker_id=spec.marker_id,
                                )
                            except ValueError as marker_exc:
                                raise ValueError(f"ChArUco mat: {charuco_exc}; single-marker mat: "
                                                 f"{marker_exc}") from marker_exc
                            mat = {"kind": "marker", **spec.to_dict()}
                    except VisionUnavailable as exc:
                        return _dumps({
                            "ok": False,
                            "reason": exc.reason,
                            "hint": exc.hint,
                        })
                    except (ValueError, FileNotFoundError) as exc:
                        # Verbatim: the solver knows how many views resolved the mat and
                        # why that was not enough.
                        return _dumps({
                            "ok": False,
                            "reason": str(exc),
                            "hint": "re-shoot with the printed scale mat flat, unoccluded and "
                                    "sharp in several views, or fall back to method='two_point'",
                        })
                    solved = res.to_scale_dict(confirmed=False)
                    solved["detail"] = dict(res.detail)
                    solved["detail"].update({
                        "n_views": res.n_views,
                        "board": board,
                        "mat": mat,
                        "images_dir": images_dir,
                        "sparse_dir": sparse_dir,
                    })
                    prov_source = "photo_fiducial"
                else:  # two_point
                    dataset = s.get("sfm_dataset") or {}
                    if dataset and params.get("image_space") != "undistorted":
                        return _dumps({
                            "ok": False, "reason": "two_point_requires_undistorted_image",
                            "hint": "Select pixels on the paired undistorted image under "
                                    f"{dataset.get('images_dir')} and pass image_space='undistorted'.",
                        })
                    p1 = _pixel_pair(params.get("p1"))
                    p2 = _pixel_pair(params.get("p2"))
                    if p1 is None or p2 is None:
                        return _dumps({
                            "ok": False,
                            "reason": "two_point requires p1 and p2 as [x, y] pixel pairs",
                            "hint": "params: {image|image_index, p1: [x,y], p2: [x,y], "
                                    "distance_mm: float}",
                        })
                    try:
                        distance_mm = float(params.get("distance_mm"))
                    except (TypeError, ValueError):
                        return _dumps({
                            "ok": False,
                            "reason": "two_point requires a numeric distance_mm",
                            "hint": "the real-world separation, in millimetres, between p1 and p2",
                        })
                    if not _math.isfinite(distance_mm) or distance_mm <= 0.0:
                        return _dumps({
                            "ok": False,
                            "reason": f"distance_mm must be positive, got {distance_mm}",
                            "hint": "measure the real span between the two clicked points",
                        })
                    try:
                        cams = read_colmap_sparse(sparse_dir)
                    except VisionUnavailable as exc:
                        return _dumps({"ok": False, "reason": exc.reason, "hint": exc.hint})
                    except (FileNotFoundError, ValueError, KeyError) as exc:
                        return _dumps({
                            "ok": False,
                            "reason": f"could not read the sparse model: {exc}",
                            "hint": "verify the paired binary or text COLMAP model in the shared dataset",
                        })
                    if not cams:
                        return _dumps({
                            "ok": False,
                            "reason": "the sparse model has no registered images",
                            "hint": "re-run reveng_reconstruct(session_id) — SfM registered nothing",
                        })
                    names = sorted(cams)
                    image_name = params.get("image")
                    index = params.get("image_index")
                    if isinstance(image_name, int) and not isinstance(image_name, bool):
                        image_name, index = None, image_name
                    if image_name is None and index is not None:
                        try:
                            i = int(index)
                        except (TypeError, ValueError):
                            return _dumps({
                                "ok": False,
                                "reason": f"image_index must be an integer, got {index!r}",
                                "hint": f"0..{len(names) - 1} over the registered images",
                            })
                        if not 0 <= i < len(names):
                            return _dumps({
                                "ok": False,
                                "reason": f"image_index {i} out of range (0..{len(names) - 1})",
                                "hint": f"registered images: {names[:12]}",
                            })
                        image_name = names[i]
                    if image_name is None:
                        if len(names) != 1:
                            return _dumps({
                                "ok": False,
                                "reason": "two_point needs to know which registered image the "
                                          "two clicks came from",
                                "hint": "pass image=<name> or image_index=<int>; registered: "
                                        f"{names[:12]}",
                            })
                        image_name = names[0]
                    image_name = str(image_name)
                    if image_name not in cams:
                        base = _os.path.basename(image_name)
                        mapping = dataset.get("image_name_map") or {}
                        matches = [n for n in names if _os.path.basename(n) == base]
                        if not matches:
                            matches = [n for n, src in mapping.items() if n in cams and
                                       (str(src) == image_name or _os.path.basename(str(src)) == base)]
                        if len(matches) != 1:
                            return _dumps({
                                "ok": False,
                                "reason": f"image {image_name!r} is not registered in the "
                                          "sparse model",
                                "hint": f"registered images: {names[:12]}",
                            })
                        image_name = matches[0]
                    points3d = _read_colmap_points3d(sparse_dir)
                    if len(points3d) < 2:
                        return _dumps({
                            "ok": False,
                            "reason": "the sparse model has fewer than two 3D points to depth the clicks against",
                            "hint": "reconstruct a better-connected sparse point cloud",
                        })
                    try:
                        res = solve_scale_two_point(
                            cameras=list(cams.values()),
                            points3d=points3d,
                            p1=p1,
                            p2=p2,
                            distance_mm=distance_mm,
                            image_name=image_name,
                        )
                    except VisionUnavailable as exc:
                        return _dumps({"ok": False, "reason": exc.reason, "hint": exc.hint})
                    except ValueError as exc:
                        return _dumps({
                            "ok": False,
                            "reason": str(exc),
                            "hint": "click two points that both land on reconstructed geometry in "
                                    "that image, and check distance_mm is their real separation",
                        })
                    solved = res.to_scale_dict(confirmed=False)
                    solved["detail"] = dict(res.detail)
                    solved["detail"].update({
                        "image": image_name,
                        "p1": p1,
                        "p2": p2,
                        "distance_mm": distance_mm,
                        "n_points3d": len(points3d),
                        "sparse_dir": sparse_dir,
                    })
                    prov_source = "photo_fiducial"

            scale = s.setdefault("scale", {})
            scale["method"] = solved["method"]
            scale["mm_per_unit"] = solved["mm_per_unit"]
            scale["residual_pct"] = solved["residual_pct"]
            scale["detail"] = solved["detail"]
            scale["confirmed"] = False  # human gate: confirm_dimensions
            from services.vision.recon_transform import ReconTransform
            previous = s.get("recon_transform") or {}
            # Recover the current surface in the old gauge before changing it.
            _reset_transform_artifacts(s)
            s["recon_transform"] = ReconTransform(
                mm_per_unit=float(scale["mm_per_unit"]),
                rotation=previous.get("rotation") or [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]],
                translation_mm=previous.get("translation_mm") or [0., 0., 0.],
                source=method, residual_mm=None, confirmed=False,
            ).to_json()
            _invalidate_downstream(s)
            s.setdefault("stages", {})["scale"] = "done"
            s.setdefault("provenance", []).append({
                "step": "set_scale",
                "method": method,
                "source": prov_source,
                "params": params,
                "mm_per_unit": scale["mm_per_unit"],
                "residual_pct": scale["residual_pct"],
                "ts": _now_iso(),
            })
            _save_session(s)
            out = {"ok": True, "scale": scale, "session": s}
            if warnings:
                out["warnings"] = warnings
            return _dumps(out)
        except Exception as e:
            return _err(e)



    @mcp.tool()
    def reveng_confirm_scale(session_id: str) -> str:
        """Explicitly approve a solved scale when no scan mission gate is pending.

        Use only after a person has verified the reference measurement. If a scan
        mission is paused, approve its existing gate with reveng_resolve_gate.
        """
        try:
            from .reveng_common import _find_session_run
            s = _load_session(session_id, merge_gaussian=False)
            if s is None:
                return _dumps({"ok": False, "reason": "session_not_found"})
            run = _find_session_run(session_id)
            if run and run.get("status") in ("running", "paused"):
                return _dumps({"ok": False, "reason": "mission_gate_active",
                               "hint": "Use reveng_gate_status and reveng_resolve_gate for the active mission."})
            transform = s.get("recon_transform")
            scale = s.get("scale") or {}
            if not transform or scale.get("mm_per_unit") is None:
                return _dumps({"ok": False, "reason": "scale_unsolved",
                               "hint": "Solve the scale before confirming it."})
            from services.vision.recon_transform import ReconTransform
            checked = ReconTransform.from_json(transform)
            if not _math.isclose(checked.mm_per_unit, float(scale["mm_per_unit"]), rel_tol=1e-12):
                return _dumps({"ok": False, "reason": "scale_transform_mismatch"})
            was_confirmed = checked.confirmed
            if not checked.confirmed:
                _invalidate_downstream(s)
            checked.confirmed = True
            s["recon_transform"] = checked.to_json()
            warnings = []
            if not was_confirmed:
                try:
                    from .reveng_common import _materialize_metric_splat
                    _materialize_metric_splat(s)
                except Exception as exc:
                    warnings.append(f"could not transform splat preview: {exc}")
            scale["confirmed"] = True
            s["scale"] = scale
            s.setdefault("stages", {})["scale"] = "done"
            s.setdefault("provenance", []).append({
                "step": "confirm_scale", "method": scale.get("method"),
                "mm_per_unit": checked.mm_per_unit, "ts": _now_iso()})
            _save_session(s)
            out = {"ok": True, "scale": scale, "session": s}
            if warnings:
                out["warnings"] = warnings
            return _dumps(out)
        except Exception as e:
            return _err(e)


    @mcp.tool()
    def reveng_align_axes(session_id: str, method: str = "planes",
                          rotation: Optional[list[list[float]]] = None) -> str:
        """Align a reconstruction's arbitrary gauge to world axes before rebuilding.

        ``planes`` uses dominant orthogonal fitted planes or mesh faces, falling
        back to PCA; ``pca`` uses mesh points; ``manual`` accepts an absolute
        proper 3x3 native-to-world rotation. The selected axes are suggestions,
        not verified manufacturing datums. Refit after alignment.
        """
        reservation = None
        try:
            import numpy as np
            from services.vision.axis_align import align_axes
            from services.vision.recon_transform import ReconTransform
            from .reveng_common import _materialize_metric_splat, _release_mission
            from .reveng_splat_tools import _claim_session

            if _load_session(session_id, merge_gaussian=False) is None:
                return _dumps({"ok": False, "reason": "session_not_found"})
            reservation, busy = _claim_session(session_id)
            if busy:
                return _dumps(busy)
            session = _load_session(session_id, merge_gaussian=False)
            if session is None:
                return _dumps({"ok": False, "reason": "session_not_found"})
            old = (ReconTransform.from_json(session["recon_transform"])
                   if session.get("recon_transform") else ReconTransform.identity())
            method = str(method).strip().lower()
            if method not in ("planes", "pca", "manual"):
                return _dumps({"ok": False, "reason": "invalid_alignment_method",
                               "hint": "Use planes, pca, or manual."})
            aligned = None
            source = None
            if method == "manual":
                aligned = align_axes(method="manual", rotation=rotation)
                source = "manual"
            elif method == "planes":
                fits = _load_current_fits(session) or {}
                plane_fits = [fit for fit in fits.get("fits", [])
                              if isinstance(fit, dict) and fit.get("kind") == "plane"
                              and isinstance(fit.get("params_mm"), dict)
                              and fit["params_mm"].get("normal") is not None]
                if len(plane_fits) >= 2:
                    try:
                        normals = np.asarray([fit["params_mm"]["normal"] for fit in plane_fits],
                                             dtype=float) @ np.asarray(old.rotation)
                        weights = [float(fit.get("inliers") or fit.get("confidence") or 1)
                                   for fit in plane_fits]
                        aligned = align_axes(method="planes", normals=normals, weights=weights)
                        source = "fits"
                    except (TypeError, ValueError):
                        pass
            if aligned is None:
                import trimesh
                artifacts = session.get("artifacts") or {}
                mesh_path = artifacts.get("mesh_path")
                if not mesh_path or not _Path(mesh_path).is_file():
                    return _dumps({"ok": False, "reason": "no_mesh",
                                   "hint": "Produce a measurement mesh before automatic alignment."})
                geometry = trimesh.load(mesh_path, force="mesh", process=False)
                points = np.asarray(geometry.vertices, dtype=float)
                normals = weights = None
                faces = getattr(geometry, "faces", None)
                if faces is not None and len(faces):
                    normals = np.asarray(geometry.face_normals, dtype=float)
                    weights = np.asarray(geometry.area_faces, dtype=float)
                if artifacts.get("mesh_transform_applied"):
                    old_rotation = np.asarray(old.rotation)
                    points = ((points - np.asarray(old.translation_mm)) @ old_rotation
                              / old.mm_per_unit)
                    if normals is not None:
                        normals = normals @ old_rotation
                aligned = align_axes(points, method=method, normals=normals, weights=weights)
                source = "mesh"
            old.rotation = aligned["rotation"]
            checked = ReconTransform.from_json(old.to_json())
            _reset_transform_artifacts(session)
            _invalidate_downstream(session)
            session["recon_transform"] = checked.to_json()
            session["axis_alignment"] = {"requested_method": method, "method": aligned["method"],
                                         "source": source, "rotation": checked.rotation}
            if aligned.get("fallback_reason"):
                session["axis_alignment"]["fallback_reason"] = aligned["fallback_reason"]
            warnings = []
            if checked.confirmed:
                try:
                    _materialize_metric_splat(session)
                except Exception as exc:
                    warnings.append(f"could not transform splat preview: {exc}")
            session.setdefault("provenance", []).append({
                "step": "align_axes", **session["axis_alignment"], "ts": _now_iso()})
            _save_session(session)
            out = {"ok": True, "session_id": session_id,
                   "alignment": session["axis_alignment"], "recon_transform": checked.to_json(),
                   "session": session}
            if warnings:
                out["warnings"] = warnings
            return _dumps(out)
        except Exception as exc:
            return _err(exc)
        finally:
            if reservation:
                _release_mission(session_id, reservation)


    @mcp.tool()
    def reveng_print_scale_mat(size: str = "A4") -> str:
        """Write the printable ChArUco scale-mat PDF (and a PNG preview) for the RE pipeline.

        ``size`` is ``A4`` (default) or ``Letter``; the 7×10 board of 25 mm squares is the
        same on both. Print at 100 % and check one square with a ruler before capture; the
        ``aruco_mat`` solver in ``reveng_set_scale`` triangulates every corner it resolves.
        """
        if size not in ("A4", "Letter"):
            return _dumps({
                "ok": False,
                "reason": f"invalid size {size!r}",
                "hint": "must be 'A4' or 'Letter'",
            })
        try:
            from services.vision.fiducial import VisionUnavailable  # type: ignore
            from services.vision.scale_mat import generate_scale_mat  # type: ignore
            from . import reveng_common as common
        except ImportError:
            return _not_provisioned(
                "scale_mat module not available; install opencv-python and Pillow"
            )
        try:
            info = generate_scale_mat(size, str(common._reveng_dir() / "scale_mats"))
            return _dumps({"ok": True, **info})
        except VisionUnavailable as exc:
            return _dumps({"ok": False, "reason": exc.reason, "hint": exc.hint})
        except Exception as e:
            return _err(e)
