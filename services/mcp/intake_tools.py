"""Intake / photo-measurement / first-try-fit MCP tools.

The north star: never silently guess a fit-critical dimension. When the user references a real part
we have no model for, we (1) try standard reference data first, (2) ask for photos WITH a scale
reference, (3) DEMAND a typed caliper value for any measured-tier critical dim, (4) resolve the
manufacturing process (FDM default, clarify if ambiguous), (5) build the receptacle to FIT with
process clearances, and (6) emit a FitReport (+ optional printable fit-test coupon) before export.

The pure brain is services.intake.intake + the C++ DfM process profiles (core/dfm, reached
via the libchudware ``dfm_*`` methods through core_bridge). These tools are the thin MCP
adapter: they drive the flow and PERSIST the artifacts inside the .step+ via doc_set_meta/doc_get_meta
(the same opaque-doc-key mechanism as `studies`), so an MCP-built part reopens in-app intact. The
elicitation is naturally multi-turn over MCP: a tool returns the ask, the user answers next turn.
"""
from __future__ import annotations

import json as _json
import math as _math
import os as _os
import tempfile as _tempfile
from typing import Optional

from mcp.server.fastmcp import FastMCP

from services.mcp import core_bridge as cb
from services.mcp.cad_tools import _doc, _dumps, _err
# fit_report's ad-hoc pair is the SAME engine as fit_calc — one dialect, one maths.
# _Extra is the var-keyword annotation FastMCP needs (a bare **kwargs becomes a
# required "kwargs" field there and breaks every call); see research_tools.
from services.mcp.research_tools import (
    _Extra,
    fit_from_kwargs as _fit_from_kwargs,
    has_fit_inputs as _has_fit_inputs,
)
from services.intake import intake as ik


def _dfm(method: str, params: dict):
    """One C++ DfM RPC (core/dfm/process_profiles.cpp). Raises ValueError on the oracle's
    error envelope so the tools' ``except Exception -> _err`` path is unchanged."""
    r = cb.call(method, params)
    if not r.get("ok"):
        raise ValueError(r.get("error") or f"{method} failed")
    return r.get("result")


def _canon_process(process: Optional[str]) -> Optional[str]:
    """Accept the process words a human (or model) actually writes.

    Delegates to the single alias table in ``services.agent.geometry_helpers``
    rather than keeping a second copy here. Unknown words pass through so the
    C++ "Unknown process 'x'. Known processes: [...]" teaching error still fires.
    """
    try:
        from services.agent.geometry_helpers import (  # noqa: PLC0415
            canonical_process_strict,
        )
        return canonical_process_strict(process)
    except Exception:  # noqa: BLE001 — never let an alias lookup break a tool
        return process


# The oracle's DEFAULT_PROCESS (engine_oracle/dfm/process_profiles.py) — FDM is the prior.
_DEFAULT_PROCESS = "fdm"

try:  # Vision is optional at runtime — a Pillow/module import hiccup must not stop server boot.
    from services import vision as _vz
except Exception:  # noqa: BLE001
    _vz = None

# Interaction contract (contracts/interaction.md).  Lazy import: the mcp.server namespace
# collision must not prevent process_profiles / intake from loading.  The ask_user
# path is only exercised at call time via _ask_via_contract(), never at module import.
def _ask_via_contract(questions: list, run_id: Optional[str], transport=None,
                      ledger=None, doc_id: Optional[str] = None) -> dict:
    """Route a batch of questions through the ONE interaction contract (§3 envelope).

    This replaces bespoke Q/A plumbing in resolve_process (process clarification) and
    confirm_dimensions (caliper demands).  Uses FileLedger(run_id) when run_id is
    supplied so answers persist to the run ledger and replay on resume (§5 — a
    dimension confirmed here is visible to a DAG gate for the same run_id, never re-asked).

    When ``doc_id`` is supplied (and no ``run_id``), uses IntakeLedger(doc_id) so
    answers persist inside the document's IntakeState meta record, round-tripped
    as JSON into the .step+ via doc_set_meta (contract §5 "scope=doc").
    The store travels WITH the document, so a dimension confirmed in one session
    is never re-asked after the doc is saved and reopened — even though the
    engine mints a fresh doc_id on every open (core/doc/session.cpp new_doc_id).

    Falls back to MemoryLedger when neither run_id nor doc_id is available.

    ``transport`` and ``ledger`` are injectable for tests (no live MCP client needed).
    When ``transport`` is None and ``run_id`` is present the call is non-blocking so
    the MCP tool does not stall: the questions are recorded and the suggested defaults
    are auto-accepted, just like a non-interactive pipeline run.  The caller (MCP tool)
    then returns the question payload to the user for the next turn.
    """
    from services.agent.interaction import (
        FileLedger, MemoryLedger, ask_user, make_batch)
    from services.mcp.intake_ledger import IntakeLedger

    if ledger is None:
        if run_id:
            ledger = FileLedger(run_id)
        elif doc_id:
            ledger = IntakeLedger(doc_id)
        else:
            ledger = MemoryLedger()

    # Determine scope: run-scoped when run_id is known; doc-scoped when doc_id is
    # known; fall back to "run" scope with MemoryLedger for legacy callers that
    # supply neither (non-persisting session-local path, same as before doc-scoped ledgers).
    if run_id:
        scope, effective_run_id, effective_doc_id = "run", run_id, None
    elif doc_id:
        scope, effective_run_id, effective_doc_id = "doc", None, doc_id
    else:
        scope, effective_run_id, effective_doc_id = "run", None, None

    # Non-blocking: the MCP tool layer is multi-turn; it surfaces questions to the
    # user and returns.  We record Q/A into the ledger so that if a later turn
    # (or a DAG gate for the same run) re-encounters the same id, it replays from
    # the ledger without re-asking.
    return ask_user(
        questions,
        scope=scope,
        run_id=effective_run_id,
        doc_id=effective_doc_id,
        blocking=transport is not None,
        transport=transport,
        ledger=ledger,
        autonomy="auto",   # auto-accept suggested defaults when no transport
    )

# doc["meta"] keys (persisted in the .step+).
_K_PROCESS = "process"
_K_MEASURED = "measured_parts"
_K_LEDGER = "crit_dims"
_K_FIT = "fit_report"
_K_INTAKE = "intake"
_K_PHOTOS = "photos"


def _measure_state(doc_id: str, measured, ledger) -> dict:
    """What the document ACTUALLY holds — the fit_report error has to name it."""
    photos = _get(doc_id, _K_PHOTOS)
    return {
        "doc_id": doc_id,
        "measured_parts": len(measured.features) if measured is not None else 0,
        "ledger_rows": len(ledger.rows) if ledger is not None else 0,
        "critical_rows": (len([r for r in ledger.rows if r.is_critical])
                          if ledger is not None else 0),
        "photos_registered": len(photos.get("photos", {})) if isinstance(photos, dict) else 0,
    }


def _nothing_measured(doc_id: str, process: str, measured, ledger) -> dict:
    """The teaching version of "nothing measured yet": what is missing, which tool
    fills it, in what order, and a one-liner that works right now."""
    state = _measure_state(doc_id, measured, ledger)
    missing = ("no measured part AND no critical-dimension ledger"
               if measured is None and ledger is None else
               "no measured part (the ledger exists but has no part behind it)"
               if measured is None else
               "no critical-dimension ledger (a measured part exists but was never "
               "turned into a ledger)")
    return {
        "ok": False,
        "error": "nothing measured yet",
        "reason": "no_measurements",
        "state": state,
        "missing": missing,
        "hint": (
            f"fit_report reports the mating pairs recorded on doc {doc_id}, and that "
            f"document has {state['measured_parts']} measured feature(s) and "
            f"{state['ledger_rows']} ledger row(s) — {missing}. Populate it first, in "
            f"this order: "
            f"1) submit_part_photos(claims=[{{feature, dim, value_mm, scale_source, "
            f"in_plane}}]) — or measure_from_photo(photo_id, px_a, px_b, feature, dim) "
            f"once per mating pair — builds measured_parts + crit_dims; "
            f"2) confirm_dimensions([{{key, value_mm}}]) for every open_demand it "
            f"returns (typed caliper values); "
            f"3) fit_report() — now it has pairs to report. "
            f"For a ONE-OFF pair you do not need any of that: pass the diameters "
            f"directly."),
        "example": ('submit_part_photos(claims=[{"feature": "output_shaft", '
                    '"dim": "dia", "value_mm": 20.0, "scale_source": "caliper_typed", '
                    '"in_plane": true}])  then  fit_report()'),
        "ad_hoc_example": ('fit_report(bore_diameter=20.02, shaft_diameter=20.0)  or  '
                           'fit_report(bore_diameter=20, tolerance="H7/g6")'),
        "next_actions": [
            "submit_part_photos(claims=[...]) — record the measured dims",
            "measure_from_photo(photo_id, px_a, px_b, feature, dim) — measure one dim",
            "confirm_dimensions([{key, value_mm}]) — close fit-critical demands",
            "fit_report(bore_diameter=..., shaft_diameter=...) — ad-hoc pair, no doc state",
        ],
        "process": process,
    }


def _ad_hoc_fit_report(doc_id: str, process: str, supplied: dict,
                       doc_has_measurements: bool) -> dict:
    """One mating pair straight from the arguments, via the fit_calc engine.

    Clearly labelled as an ad-hoc pair: it is NOT derived from the document, is not
    persisted as the document's fit report, and does not clear the intake gate."""
    env = _fit_from_kwargs(supplied)
    notes = list(env.get("interpreted") or [])
    notes.insert(0, "ad-hoc pair computed from the arguments you passed — this is NOT "
                    "the document's fit report and does not clear the intake gate; "
                    "call fit_report() with no diameters after submit_part_photos / "
                    "confirm_dimensions for that")
    if doc_has_measurements:
        notes.append(f"doc {doc_id} DOES have recorded measurements — call fit_report() "
                     f"with no diameters for the document report")
    if not env.get("ok"):
        env = dict(env)
        env["interpreted"] = notes
        env["mode"] = "ad_hoc_pair"
        return env
    answer = env.get("answer") or {}
    verdict = "ok" if answer.get("fit_character") else "unmeasured"
    return {
        "ok": True,
        "mode": "ad_hoc_pair",
        "process": process,
        "fit_report": {
            "checks": [{
                "feature": "ad_hoc_pair",
                "key": "ad_hoc_pair.dia",
                "modeled_mm": (answer.get("hole_absolute") or [None])[0],
                "source_mm": (answer.get("shaft_absolute") or [None])[0],
                "fit": env.get("inputs", {}).get("fit"),
                "clearance_mm": answer.get("clearance"),
                "source": "argument",
                "confidence": "given",
                "verdict": verdict,
            }],
            "assumptions": ([f"process {process.upper()} assumed",
                             "sizes taken from the call, not from the document"]
                            + ([] if verdict == "ok" else
                               ["only one side was supplied — clearance is not "
                                "computable; add the mating diameter or a two-part fit "
                                "class such as 'H7/g6'"])),
            "process": process,
            "overall": "pass" if verdict == "ok" else "at_risk",
            "coupon": {},
        },
        "fit": env,
        "interpreted": notes,
    }


def _get(doc_id: str, key: str):
    try:
        return cb.call("doc_get_meta", {"doc_id": doc_id, "key": key}).get("value")
    except Exception:  # noqa: BLE001
        return None


def _set(doc_id: str, key: str, value) -> None:
    cb.call("doc_set_meta", {"doc_id": doc_id, "key": key, "value": value})


def _process_id(doc_id: str) -> str:
    prof = _get(doc_id, _K_PROCESS)
    if isinstance(prof, dict) and prof.get("process"):
        return prof["process"]
    return _DEFAULT_PROCESS


def _load_ledger(doc_id: str):
    raw = _get(doc_id, _K_LEDGER)
    return ik.CriticalDimensionLedger.from_dict(raw) if isinstance(raw, dict) else None


def _load_measured(doc_id: str):
    raw = _get(doc_id, _K_MEASURED)
    if isinstance(raw, list) and raw:
        return ik.MeasuredPartProfile.from_dict(raw[-1])
    return None


# ----------------------------------------------------------------- photo ingestion / metrology

def _load_photos(doc_id: str):
    """The doc's :class:`PhotoRegistry` (empty if none yet); ``None`` when vision is unavailable."""
    if _vz is None:
        return None
    raw = _get(doc_id, _K_PHOTOS)
    return _vz.PhotoRegistry.from_dict(raw if isinstance(raw, dict) else {})


def _photo_cache_dir(doc_id: str) -> str:
    return _os.path.join(_tempfile.gettempdir(), "chudware_vision", str(doc_id))


def _register_photos(doc_id: str, photos: list) -> list:
    """Ingest each path -> stable photo_id + downscaled preview (≤1024px/<1MB) + full-res link."""
    reg = _load_photos(doc_id)
    cache = _photo_cache_dir(doc_id)
    out = []
    for item in photos or []:
        if isinstance(item, dict):
            path, lbl = item.get("path"), item.get("label", "part")
        else:
            path, lbl = item, "part"
        rec = reg.register(path, label=lbl, cache_dir=cache)
        out.append({
            "photo_id": rec.photo_id, "preview_path": rec.preview_path,
            "resource_link": rec.resource_link, "mime": rec.mime,
            "width": rec.width, "height": rec.height,
            "preview_width": rec.preview_width, "preview_height": rec.preview_height,
            "preview_bytes": rec.preview_bytes,
        })
    _set(doc_id, _K_PHOTOS, reg.to_dict())
    return out


def _preserve_confirmations(old, ledger) -> None:
    """Carry typed/given/standard resolutions from a prior ledger onto a freshly rebuilt one, so a
    later photo measurement never silently wipes a confirm_dimensions caliper value."""
    if old is None:
        return
    for r in old.rows:
        if r.status in ("given", "measured", "standard") and r.nominal_mm is not None \
                and ledger.row(r.key) is not None:
            ik.apply_confirmation(ledger, r.key, r.nominal_mm, status=r.status,
                                  resolved_by=r.resolved_by)


def _record_photo_measure(doc_id: str, rec, feature: str, dim: str, res, fit, proc):
    """Land one fiducial measurement in the intake ledger at ITS tier (never geometry)."""
    mp = _load_measured(doc_id) or ik.new_measured_part("mp_1", rec.label, source="photo")
    prov = dict(res.provenance)
    prov["photo_id"] = rec.photo_id
    prov["uncert_mm"] = res.uncert_mm
    feat = mp.feature(feature)
    if feat is None:
        arch = ik.classify(feature) or "prism_pocket"
        mp.features.append(ik.new_measured_feature(
            feature, arch, dims={dim: res.value_mm}, fit=fit,
            confidence=res.tier, uncert_mm=res.uncert_mm, provenance=prov))
    else:
        feat.dims[dim] = res.value_mm
        feat.confidence = ik.worst_tier([feat.confidence, res.tier])
        feat.uncert_mm = res.uncert_mm
        feat.provenance.update(prov)
        if fit:
            feat.fit = fit
    if rec.photo_id not in [im.get("photo_id") for im in mp.images]:
        mp.images.append({"photo_id": rec.photo_id, "resource_link": rec.resource_link,
                          "preview_path": rec.preview_path})
    old = _load_ledger(doc_id)
    ledger = ik.build_ledger(mp, process=proc)
    _preserve_confirmations(old, ledger)
    demands = ik.compute_demands(mp, ledger, process=proc)
    _set(doc_id, _K_MEASURED, [mp.to_dict()])
    _set(doc_id, _K_LEDGER, ledger.to_dict())
    return mp, ledger, demands


def _dfm_number(value):
    """Return one finite scalar from feature-param or plain-number storage."""
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if _math.isfinite(number) else None


def _dfm_features(tree_result: dict) -> list[dict]:
    """Flatten the feature rows exposed by doc_tree without interpreting geometry."""
    root = tree_result.get("tree", tree_result) if isinstance(tree_result, dict) else {}
    found: list[dict] = []
    seen: set[int] = set()

    def visit(node) -> None:
        if isinstance(node, list):
            for child in node:
                visit(child)
            return
        if not isinstance(node, dict):
            return
        marker = id(node)
        if marker in seen:
            return
        seen.add(marker)
        if node.get("type") and isinstance(node.get("params"), dict):
            found.append(node)
        for key in ("features", "children"):
            visit(node.get(key))

    visit(root)
    return found


_DFM_FEATURE_SIZE_KEYS = (
    "diameter", "dia", "width", "thickness", "wall_thickness", "rib_thickness",
    "radius", "fillet_radius", "chamfer", "size", "height", "length",
)
_MACHINING_PROCESSES = frozenset({"cnc_mill", "lathe"})
_MACHINING_HOLE_ASPECT_WARNING = 5.0


def _dfm_min_feature(features: list[dict], limit) -> dict:
    threshold = _dfm_number(limit)
    if threshold is None:
        return {
            "check": "min_feature", "status": "not_evaluated", "ok": None,
            "error": "ProcessProfile has no numeric min_feature_mm threshold.",
        }
    candidates = []
    for feature in features:
        params = feature.get("params") or {}
        for key in _DFM_FEATURE_SIZE_KEYS:
            value = _dfm_number(params.get(key))
            if value is not None and value > 0:
                candidates.append({
                    "feature_id": feature.get("id") or feature.get("name"),
                    "parameter": key, "value_mm": value,
                })
    if not candidates:
        return {
            "check": "min_feature", "status": "not_evaluated", "ok": None,
            "limit_mm": threshold,
            "error": "doc_tree exposed no numeric feature-size parameters to evaluate.",
        }
    smallest = min(candidates, key=lambda row: row["value_mm"])
    passed = smallest["value_mm"] >= threshold
    return {
        "check": "min_feature", "status": "pass" if passed else "fail",
        "ok": passed, "value_mm": round(smallest["value_mm"], 4),
        "limit_mm": threshold, "feature_id": smallest["feature_id"],
        "parameter": smallest["parameter"],
        "note": f"smallest declared feature is {smallest['value_mm']:.3g}mm "
                f"vs process minimum {threshold:g}mm",
    }


def _dfm_hole_aspect(features: list[dict], process: str) -> dict:
    if process not in _MACHINING_PROCESSES:
        return {
            "check": "hole_depth_diameter", "status": "pass", "ok": True,
            "note": "machining-only blind-hole aspect warning is not applicable",
        }
    holes = []
    unevaluated = []
    for feature in features:
        if str(feature.get("type") or "").strip().casefold() != "hole":
            continue
        params = feature.get("params") or {}
        diameter = _dfm_number(params.get("diameter", params.get("dia")))
        depth = _dfm_number(params.get("depth"))
        feature_id = feature.get("id") or feature.get("name")
        if diameter is None or diameter <= 0 or depth is None or depth <= 0:
            unevaluated.append(feature_id)
            continue
        ratio = depth / diameter
        holes.append({
            "feature_id": feature_id, "diameter_mm": diameter, "depth_mm": depth,
            "ratio": round(ratio, 3), "warning": ratio > _MACHINING_HOLE_ASPECT_WARNING,
        })
    if unevaluated:
        return {
            "check": "hole_depth_diameter", "status": "not_evaluated", "ok": None,
            "limit_ratio": _MACHINING_HOLE_ASPECT_WARNING, "holes": holes,
            "error": "Machining hole(s) lack numeric diameter/depth for aspect evaluation: "
                     + ", ".join(str(value) for value in unevaluated),
        }
    warnings = [hole for hole in holes if hole["warning"]]
    if warnings:
        return {
            "check": "hole_depth_diameter", "status": "fail", "ok": False,
            "severity": "warning", "limit_ratio": _MACHINING_HOLE_ASPECT_WARNING,
            "holes": warnings,
            "note": "hole depth/diameter exceeds the 5:1 machining review threshold; "
                    "review drill reach, pecking, coolant and runout",
        }
    return {
        "check": "hole_depth_diameter", "status": "pass", "ok": True,
        "limit_ratio": _MACHINING_HOLE_ASPECT_WARNING, "holes": holes,
        "note": "all evaluable blind machining holes are at or below 5:1 depth/diameter",
    }


def _dfm_strategy_word(value) -> Optional[str]:
    word = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "tap": "tapped", "tapping": "tapped",
        "single_pointed": "single_point", "thread_milled": "thread_mill",
        "heatset_insert": "heat_set_insert", "insert": "heat_set_insert",
        "print": "printed", "3d_printed": "printed",
        "clinch": "clinch_nut", "weld": "weld_nut", "moulded": "molded",
    }
    return aliases.get(word, word) or None


def _dfm_thread_strategy(features: list[dict], process: str, profile_thread: dict) -> dict:
    threads = []
    unknown = []
    for feature in features:
        kind = str(feature.get("type") or "").strip().casefold()
        params = feature.get("params") or {}
        designation = next((params.get(key) for key in
                            ("thread", "standard", "tap", "thread_size")
                            if params.get(key)), None)
        explicit = next((params.get(key) for key in
                         ("thread_strategy", "manufacturing_strategy",
                          "manufacturing_method", "strategy")
                         if params.get(key) is not None), None)
        strategy = _dfm_strategy_word(explicit)
        family = _dfm_strategy_word(params.get("family"))
        style = _dfm_strategy_word(params.get("style"))
        if kind == "standard_part" and family == "heat_set_insert":
            strategy = "heat_set_insert"
            designation = designation or params.get("size")
        elif kind == "hole" and style == "tapped":
            strategy = strategy or "tapped"
        elif params.get("printed") is True:
            strategy = "printed"
        is_thread = bool(designation or strategy or kind == "thread")
        if not is_thread:
            continue
        row = {
            "feature_id": feature.get("id") or feature.get("name"),
            "designation": designation,
            "observed_strategy": strategy,
        }
        if strategy is None:
            unknown.append(row)
        else:
            threads.append(row)
    if unknown:
        return {
            "check": "thread_strategy", "status": "not_evaluated", "ok": None,
            "expected_strategy": profile_thread.get("strategy"), "threads": threads + unknown,
            "error": "Thread-bearing feature(s) do not state a manufacturing strategy: "
                     + ", ".join(str(row["feature_id"]) for row in unknown),
        }
    if not threads:
        return {
            "check": "thread_strategy", "status": "pass", "ok": True,
            "expected_strategy": profile_thread.get("strategy"), "threads": [],
            "note": "no thread-bearing features require a process-strategy check",
        }
    allowed = {
        "fdm": {"heat_set_insert", "printed"},
        "cnc_mill": {"tapped", "thread_mill"},
        "lathe": {"tapped", "single_point", "thread_mill"},
        "sheet_metal": {"clinch_nut", "weld_nut"},
        "laser": set(),
        "injection": {"molded"},
    }.get(process, {_dfm_strategy_word(profile_thread.get("strategy"))})
    allowed.discard(None)
    incompatible = [row for row in threads if row["observed_strategy"] not in allowed]
    # Printed FDM threads are only supported at or above the profile's stated minimum.
    printed_min = str(profile_thread.get("printed_ok_min") or "").strip().upper()
    if process == "fdm" and printed_min.startswith("M"):
        minimum = _dfm_number(printed_min[1:])
        for row in threads:
            designation = str(row.get("designation") or "").strip().upper()
            if row["observed_strategy"] != "printed" or not designation.startswith("M"):
                continue
            major = _dfm_number(designation[1:].split("X", 1)[0])
            if minimum is not None and (major is None or major < minimum):
                incompatible.append(row)
    passed = not incompatible
    return {
        "check": "thread_strategy", "status": "pass" if passed else "fail",
        "ok": passed, "expected_strategy": profile_thread.get("strategy"),
        "allowed_strategies": sorted(allowed), "threads": threads,
        "incompatible": incompatible,
        "note": "thread strategies match the selected process" if passed else
                "thread strategy conflicts with the selected process profile",
    }


def _dfm_build_direction(value: Optional[list[float]]) -> list[float]:
    vector = [0, 0, 1] if value is None else value
    if not isinstance(vector, (list, tuple)) or len(vector) != 3:
        raise ValueError("build_dir must be a finite, nonzero [x, y, z] vector")
    if any(isinstance(x, bool) for x in vector):
        raise ValueError("build_dir must be a finite, nonzero [x, y, z] vector")
    try:
        components = [float(x) for x in vector]
    except (TypeError, ValueError, OverflowError):
        raise ValueError("build_dir must be a finite, nonzero [x, y, z] vector") from None
    magnitude = _math.hypot(*components)
    if not _math.isfinite(magnitude) or magnitude == 0:
        raise ValueError("build_dir must be a finite, nonzero [x, y, z] vector")
    return [x / magnitude for x in components]


# Rows the DFAM battery adds to an FDM dfm_check (services/dfam). min_wall replaces the
# engine vertex-sampled reading for FDM with an area-sampled, face-attributed one.
_DFAM_ROWS = ("min_wall", "overhang", "bridges", "support", "small_holes", "internal_voids",
              "bed_adhesion", "build_volume")


def _dfam_overhang_legacy(row: dict, build_dir: list[float], limit) -> dict:
    """Keep the overhang row's long-standing keys (unsupported_area_mm2, worst[], …)
    beside the DFAM fields, so earlier consumers read the same numbers."""
    metrics = row.get("metrics") or {}
    worst = []
    for region in row.get("regions") or []:
        faces = region.get("faces") or []
        entry = {"face": faces[0] if faces else None,
                 "area_mm2": region.get("area_mm2"),
                 "overhang_deg": region.get("max_angle_deg"),
                 "centroid": region.get("centroid")}
        built_by = region.get("built_by") or []
        if built_by:
            entry["built_by"] = {"id": built_by[0]}
        worst.append(entry)
    area = metrics.get("area_mm2")
    return {**row, "limit_deg": _dfm_number(limit), "build_dir": build_dir,
            "unsupported_area_mm2": round(float(area), 3) if area is not None else None,
            "unsupported_face_count": metrics.get("face_count", len(worst)),
            "worst": worst[:5]}


def _dfm_additive(doc_id: str, params: dict, build_dir: list[float], *,
                  suggest_orientation: bool, printer: Optional[str], detail: bool,
                  wanted: Optional[set]) -> tuple[list[dict], dict]:
    """DFAM rows for an FDM part plus extras {estimate, orientation, printer}.

    A geometry failure turns every requested DFAM row into not_evaluated carrying the
    original error — never a silent pass."""
    names = [name for name in _DFAM_ROWS if wanted is None or name in wanted]
    extras: dict = {}
    if not names and not suggest_orientation:
        return [], extras
    try:
        from services.dfam import analyze_printability, optimize_orientation
        from services.mcp import print_tools as pt

        resolved = None
        if printer:
            install, catalog = pt.slicer_session()
            if install is None:
                raise ValueError(catalog.get("content") or "slicer not installed")
            best, _ = pt.pick_printer(catalog, printer, None)
            if best is None:
                raise ValueError(f"no vendor printer preset matches {printer!r}")
            resolved = catalog.resolve(best["name"])
            extras["printer"] = best["name"]
        mesh, _label, _source = pt.load_doc(doc_id)
        print_params = pt.print_params(resolved=resolved, dfm=params)
        analysis = analyze_printability(mesh, print_params, build_dir,
                                        checks=names or None, detail=detail)
        rows = []
        for row in analysis["checks"]:
            if row.get("check") == "overhang":
                row = _dfam_overhang_legacy(
                    row, build_dir, (params.get("overhang") or {}).get("max_deg"))
            rows.append(row)
        extras["estimate"] = analysis.get("estimate")
        if suggest_orientation:
            extras["orientation"] = optimize_orientation(mesh, print_params, top_k=3)
        return rows, extras
    except Exception as exc:  # noqa: BLE001
        return [{"check": name, "status": "not_evaluated", "ok": None,
                 "note": f"printability geometry unavailable: {exc}", "error": str(exc)}
                for name in names], extras


def _dfm_verdict(checks: list[dict]) -> tuple[str, Optional[bool]]:
    """fail > not_evaluated > pass. ``warn`` rows pass (they carry advice, not a defect)
    and ``not_applicable`` rows do not count as evaluated checks."""
    applicable = [check for check in checks if check.get("status") != "not_applicable"]
    if any(check.get("status") == "fail" for check in applicable):
        return "fail", False
    if not applicable or any(check.get("status") == "not_evaluated" for check in applicable):
        return "not_evaluated", None
    return "pass", True


def register_intake_tools(mcp: FastMCP) -> None:

    # ----------------------------------------------------------------- manufacturing process

    @mcp.tool()
    def resolve_process(request: str, material: Optional[str] = None,
                        quantity: Optional[int] = None,
                        run_id: Optional[str] = None) -> str:
        """Decide the manufacturing process from the request (FDM 3D-printing is the default).

        Returns {chosen, confidence, runner_up, ambiguous, must_block, clarification, assumptions,
        why, interaction?}. When `ambiguous` is true the tool routes the single clarification
        question through the interaction contract (contracts/interaction.md) and returns an
        `interaction` key with the question batch — the answer is recorded in the run ledger
        (run_id) so a DAG gate for the same run never re-asks.  Pass run_id to enable cross-
        mechanism replay; omit to use a session-local memory ledger.
        Bind the resolved choice with set_process().
        """
        try:
            resolution = _dfm("dfm_resolve_process", {
                "request": request, "material": material, "quantity": quantity})
            result = dict(resolution)
            clarification = resolution.get("clarification")
            if clarification and resolution.get("ambiguous") and not resolution.get("must_block"):
                # Route the clarification through the ONE interaction contract.
                # The question key is stable (based on the runner_up pair) so the ledger
                # can replay it on resume (contract §5).
                runner_up = resolution.get("runner_up") or "alternative"
                chosen = resolution.get("chosen", "fdm")
                q_id = f"process.clarify.{chosen}_vs_{runner_up}"
                questions = [{
                    "id": q_id,
                    "kind": "choice",
                    "prompt": clarification,
                    "suggested": chosen,
                    "required": False,
                    "why": resolution.get("why", "Process is ambiguous for this request."),
                    "options": [
                        {"value": chosen, "label": chosen},
                        {"value": runner_up, "label": runner_up},
                    ],
                }]
                answers = _ask_via_contract(questions, run_id=run_id)
                answered_value = answers.get(q_id, {}).get("value")
                if answered_value and answered_value != chosen:
                    result["chosen"] = answered_value
                result["interaction"] = {
                    "schema": "aicad.interaction/1",
                    "question_id": q_id,
                    "question": clarification,
                    "answered": answered_value,
                    "source": answers.get(q_id, {}).get("source", "default"),
                }
            return _dumps(result)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def set_process(process: str = "fdm", material: Optional[str] = None,
                   printer_overrides: Optional[dict] = None, doc_id: Optional[str] = None) -> str:
        """Bind the manufacturing ProcessProfile to the part (persisted in the .step+).

        `process`: fdm|cnc_mill|sheet_metal|injection|lathe|laser. `printer_overrides`: per-printer
        {xy_comp, hole_comp} for FDM calibration. Returns the resolved profile (fit-clearance table,
        min wall, thread strategy, hole compensation) — the numbers every later fit-budgeted
        dimension uses. Call this once the process is known (or accept the FDM default).
        """
        try:
            d = _doc(doc_id)
            process = _canon_process(process)
            prof = _dfm("dfm_get_profile", {"process": process,
                                            "overrides": printer_overrides})
            if material:
                prof.setdefault("material", {})["default"] = material
            _set(d, _K_PROCESS, prof)
            return _dumps({"ok": True, "process": prof,
                           "process_block": _dfm("dfm_process_block_text", {"profile": prof})})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def dfm_check(process: Optional[str] = None, doc_id: Optional[str] = None,
                  build_dir: Optional[list] = None,
                  suggest_orientation: bool = False, printer: Optional[str] = None,
                  checks: Optional[list] = None, detail: bool = False) -> str:
        """Evaluate DfM as pass, fail, or not_evaluated against the ProcessProfile.

        Every process: wall thickness, declared minimum feature size, machining blind-hole
        depth/diameter, thread strategy, molding draft. FDM adds the deterministic
        printability battery, measured on the part at the build direction:
          min_wall      area-sampled ray thickness per face (fail below the process minimum
                        or the nozzle; warn when only one perimeter pair fits; thin skins)
          overhang      downward faces steeper than the limit that are neither bed contact,
                        bridges nor small self-supporting horizontal holes (unsupported area,
                        worst faces, fixes such as bed-edge fillet -> 45° chamfer, teardrop)
          bridges       anchored horizontal spans vs the bridge limit
          support       estimated support volume, support landing on the model, and support
                        trapped where no tool can reach it (enclosed/narrow-access cavities)
          small_holes   vertical holes below the printable diameter, horizontal holes that
                        need a teardrop
          internal_voids sealed internal cavities (internal geometry) and floating bodies
          bed_adhesion  first-layer contact, tip ratio, centre of mass over the footprint
          build_volume  fit on `printer`'s bed (any vendor preset name, e.g. "ender 3")
        Every failing/warning row names faces (sids) and the features that built them, a
        model-frame centroid and concrete fixes. ``estimate`` gives a deterministic material /
        time / support-share model to compare design revisions (slice_part gives slicer truth).

        ``build_dir`` is the print-up vector (default [0,0,1]); ``suggest_orientation`` adds
        the top-3 measured orientations (optimize_print_orientation has the full ranking).
        ``checks`` limits the rows (names above plus min_feature, hole_depth_diameter,
        thread_strategy, draft); ``detail`` lists up to 50 regions per row instead of 5.
        ``dfm_pass`` is true only when every applicable check ran and passed (warn rows pass);
        evaluation gaps/errors are ``not_evaluated`` with ``dfm_pass:null`` and the error.
        """
        try:
            direction = _dfm_build_direction(build_dir)
            d = _doc(doc_id)
            proc = _canon_process(process) or _process_id(d)
            prof = _dfm("dfm_get_profile", {"process": proc})
            params = prof.get("params", {})
            wanted = ({str(name).strip() for name in checks if str(name).strip()}
                      if checks else None)
            rows = []
            extras: dict = {}

            minwall = _dfm_number(params.get("min_wall_mm"))
            if proc == "fdm":
                rows, extras = _dfm_additive(
                    d, params, direction, suggest_orientation=suggest_orientation,
                    printer=printer, detail=detail, wanted=wanted)
            elif wanted is None or "min_wall" in wanted:
                try:
                    thickness = cb.call("eval_thickness", {"doc_id": d})
                    measured = _dfm_number((thickness or {}).get("min_thickness_mm"))
                    if measured is None or minwall is None:
                        rows.append({
                            "check": "min_wall", "status": "not_evaluated", "ok": None,
                            "limit_mm": minwall,
                            "error": "eval_thickness returned no numeric min_thickness_mm"
                                     if measured is None else
                                     "ProcessProfile has no numeric min_wall_mm threshold",
                        })
                    else:
                        passed = measured >= minwall
                        rows.append({
                            "check": "min_wall", "status": "pass" if passed else "fail",
                            "value_mm": round(measured, 3), "limit_mm": minwall, "ok": passed,
                            "note": f"thinnest wall {measured:.2f}mm vs "
                                    f"{proc} minimum {minwall:g}mm",
                        })
                except Exception as exc:  # noqa: BLE001
                    rows.append({
                        "check": "min_wall", "status": "not_evaluated", "ok": None,
                        "limit_mm": minwall, "error": str(exc),
                    })
            tree_rows = [name for name in ("min_feature", "hole_depth_diameter",
                                           "thread_strategy")
                         if wanted is None or name in wanted]
            if tree_rows:
                features = None
                tree_error = None
                try:
                    features = _dfm_features(cb.call("doc_tree", {"doc_id": d}))
                except Exception as exc:  # noqa: BLE001
                    tree_error = str(exc)
                for check_name in tree_rows:
                    if features is None:
                        rows.append({
                            "check": check_name, "status": "not_evaluated", "ok": None,
                            "error": tree_error,
                        })
                    elif check_name == "min_feature":
                        rows.append(_dfm_min_feature(features, params.get("min_feature_mm")))
                    elif check_name == "hole_depth_diameter":
                        rows.append(_dfm_hole_aspect(features, proc))
                    else:
                        rows.append(_dfm_thread_strategy(
                            features, proc,
                            params.get("thread") if isinstance(params.get("thread"), dict)
                            else {},
                        ))

            draft = params.get("draft") if isinstance(params.get("draft"), dict) else {}
            if draft.get("required") and (wanted is None or "draft" in wanted):
                minimum_draft = _dfm_number(draft.get("min_deg"))
                try:
                    result = cb.call("eval_draft", {
                        "doc_id": d, "pull": [0, 0, 1],
                        "angle_deg": minimum_draft if minimum_draft is not None else 1.0,
                    })
                    below = result.get("n_faces_below_threshold")
                    if not isinstance(below, int):
                        rows.append({
                            "check": "draft", "status": "not_evaluated", "ok": None,
                            "limit_deg": minimum_draft,
                            "error": "eval_draft returned no integer n_faces_below_threshold",
                        })
                    else:
                        passed = below == 0
                        rows.append({
                            "check": "draft", "status": "pass" if passed else "fail",
                            "faces_below": below, "limit_deg": minimum_draft, "ok": passed,
                            "note": f"{proc} needs >= {minimum_draft:g}deg draft",
                        })
                except Exception as exc:  # noqa: BLE001
                    rows.append({
                        "check": "draft", "status": "not_evaluated", "ok": None,
                        "limit_deg": minimum_draft, "error": str(exc),
                    })

            status, dfm_pass = _dfm_verdict(rows)
            warnings = [row["check"] for row in rows if row.get("status") == "warn"]
            return _dumps({
                "ok": True, "process": proc, "status": status,
                "dfm_pass": dfm_pass, "build_dir": direction, "checks": rows,
                **({"warnings": warnings} if warnings else {}),
                **{key: value for key, value in extras.items() if value is not None},
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ----------------------------------------------------------------- photo measurement

    @mcp.tool()
    def request_part_photos(label: str, critical_features: list,
                           process: Optional[str] = None, doc_id: Optional[str] = None) -> str:
        """Ask the user for photos of a real part we have NO standard model for, so we can measure it.

        `critical_features`: the things that must FIT — plain descriptions or {key, description, fit}
        dicts (e.g. ["the motor body pocket", "the output shaft", "the 2 mounting holes"]). Returns a
        prioritized, templated ask: which orthographic views + close-ups to shoot, the mandatory
        SCALE reference (a printable scale card / calipers-in-frame / ruler) IN THE SAME PLANE as the
        feature, and which dims will need a typed caliper number. Persists an IntakeState (awaiting
        the user's photos); they reply next turn — submit them with submit_part_photos(). Call
        standard-parts lookup FIRST; only photograph a part we truly can't resolve.
        """
        try:
            d = _doc(doc_id)
            proc = process or _process_id(d)
            req = ik.build_photo_request(label, critical_features, process=proc)
            st = ik.new_intake_state(doc_id=d)
            st.status = "awaiting_user"
            st.pending_request_id = req.get("request_id_seed")
            st.needs = [{"kind": "photo", "label": label, "request_id": st.pending_request_id}]
            # Carry forward doc-scoped interaction Q/A records (contract §5): a fresh
            # photo request must never wipe answers already confirmed for this doc.
            prior = _get(d, _K_INTAKE)
            if isinstance(prior, dict) and isinstance(prior.get("interaction"), dict):
                st.interaction = prior["interaction"]
            _set(d, _K_INTAKE, st.to_dict())
            return _dumps({"ok": True, "request": req, "request_id": st.pending_request_id,
                           "status": "awaiting_user"})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def submit_part_photos(claims: Optional[list] = None, label: str = "part",
                          doc_id: Optional[str] = None, photos: Optional[list] = None) -> str:
        """Register the user's real photo FILES and/or submit the measurements you read from them.

        `photos` (optional): a list of image file paths (or {path, label}) the user dropped in — we
        ingest each into a stable `photo_id`, emit a downscaled preview (≤1024px / <1MB, the agent-
        facing cap) and keep a `resource_link` to the full-res original, then return those ids. Look
        at the previews, then measure fit-critical dims deterministically with
        measure_from_photo(photo_id, [x,y], [x,y], feature, dim) — code measures, you never guess.

        `claims` (optional): measurements YOU read directly, each {feature, dim, value_mm,
        scale_source (card|caliper_typed|ruler|coin|none), in_plane (bool), agrees_across_views
        (bool), edge_quality (sharp|soft), caliper_readout?}. We tier each from those signals, build
        a MeasuredPartProfile + the critical-dimension ledger for the bound process, and return
        `open_demands` — fit-critical dims whose confidence is below what their fit needs (get a
        typed caliper number for those via confirm_dimensions; a photo guess won't press-fit).
        Persists everything in the part. Call with only `photos` to register, only `claims` to
        measure, or both.
        """
        try:
            d = _doc(doc_id)
            photo_out = _register_photos(d, photos) if (photos and _vz is not None) else []
            claims = claims or []
            if not claims:
                latest = _get(d, _K_MEASURED)
                return _dumps({
                    "ok": True, "photos": photo_out,
                    "measured_part": (latest[-1] if isinstance(latest, list) and latest else None),
                    "note": "photos registered — call measure_from_photo(photo_id, [x,y], [x,y], "
                            "feature, dim) to measure a dimension, or resubmit with claims.",
                })
            proc = _process_id(d)
            by_feat: dict = {}
            for c in claims:
                fkey = c.get("feature") or "feature"
                tier = ik.score_claim(c)
                feat = by_feat.setdefault(fkey, {"dims": {}, "tiers": []})
                if c.get("dim"):
                    feat["dims"][c["dim"]] = c.get("value_mm")
                feat["tiers"].append(tier.get("tier", "low"))
            features = []
            for fkey, fv in by_feat.items():
                arch = ik.classify(fkey) or "prism_pocket"
                worst = ik.worst_tier(fv["tiers"]) if fv["tiers"] else "low"
                features.append(ik.new_measured_feature(fkey, arch, dims=fv["dims"],
                                                        confidence=worst))
            mp = ik.new_measured_part("mp_1", label, source="photo")
            mp.features = features
            ledger = ik.build_ledger(mp, process=proc)
            demands = ik.compute_demands(mp, ledger, process=proc)
            # Auto-resolve the critical dims that DO have a satisfying photo value (not in demands):
            # only the genuinely-soft or never-measured criticals should keep blocking the gate, so
            # a high-confidence photo dim doesn't force a needless typed re-confirmation.
            demand_keys = {x.get("key") for x in demands if isinstance(x, dict)}
            for row in ledger.rows:
                if row.key in demand_keys or row.status != "unknown":
                    continue
                dim = row.key.rsplit(".", 1)[-1]
                feat = mp.feature(row.feature)
                val = feat.dims.get(dim) if feat is not None else None
                if val is not None:
                    ik.apply_confirmation(ledger, row.key, val, status="measured",
                                          resolved_by="vision")
            _set(d, _K_MEASURED, [mp.to_dict()])
            _set(d, _K_LEDGER, ledger.to_dict())
            return _dumps({"ok": True, "measured_part": mp.to_dict(),
                           "open_demands": demands,
                           "photos": photo_out,
                           "gate_ok": ik.gate_ok(ledger)})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def measure_from_photo(photo_id: str, px_a: list, px_b: list, feature: str, dim: str,
                           fit: Optional[str] = None, board: Optional[dict] = None,
                           doc_id: Optional[str] = None) -> str:
        """Measure ONE in-plane dimension between two pixel points against a ChArUco fiducial.

        You pick the two pixel endpoints on a registered photo; DETERMINISTIC code does the
        measuring (VLMs pick pixels, code measures). A printed ChArUco/ArUco
        sheet lying flat in the feature's plane fixes the pixel→mm scale via homography. `px_a`/`px_b`
        are [x, y] pixel coords in the FULL-RES image; register it first with
        submit_part_photos(photos=[...]) and pass the returned `photo_id`. `feature`/`dim` name where
        the result lands — use the BARE taxonomy dim key (e.g. feature="mount_holes", dim="spacing";
        feature="output_shaft", dim="dia"); `fit` optionally sets the fit class (press|slip|
        location|clearance|…). The confidence tier comes from the marker PIXEL AREA (resolution) and
        planarity — bigger/flatter markers ⇒ higher tier; out-of-plane is flagged low, never guessed.
        The dim lands in the intake ledger with provenance.method="photo_fiducial"; it NEVER writes
        geometry. The intake layer then applies its policy: a photo dim on a tolerance-sensitive fit
        is downgraded one step and flagged (never a typed 'measured'), and anything still below its
        required tier — every out-of-plane/low read — keeps demanding a typed caliper value via
        confirm_dimensions. Returns the measurement, its tier + honesty band, and the updated
        open_demands / gate. Optional `board` overrides the default 5×7 30 mm ChArUco spec
        ({dict_name, squares_x, squares_y, square_len_mm, marker_len_mm}).
        """
        try:
            if _vz is None:
                return _dumps({"ok": False, "reason": "vision module unavailable",
                               "hint": "install Pillow + opencv-python-headless (Apache-2.0)"})
            if not _vz.HAVE_CV2:
                return _dumps({"ok": False, "reason": "OpenCV (cv2) is not installed",
                               "hint": "pip install opencv-python-headless (Apache-2.0; ArUco is "
                                       "in core objdetect)"})
            d = _doc(doc_id)
            reg = _load_photos(d)
            rec = reg.get(photo_id) if reg else None
            if rec is None:
                return _dumps({"ok": False, "reason": f"unknown photo_id {photo_id!r}",
                               "hint": "register the photo first via submit_part_photos(photos=[...])"})
            spec = _vz.BoardSpec.from_dict(board) if board else _vz.DEFAULT_BOARD
            res = _vz.measure_from_photo(rec.path, tuple(px_a), tuple(px_b), spec)
            if not res.ok:
                return _dumps({"ok": False, "reason": res.reason, "hint": res.hint,
                               "measurement": res.to_dict()})
            proc = _process_id(d)
            claim = res.to_claim(feature, dim, fit=fit)
            mp, ledger, demands = _record_photo_measure(d, rec, feature, dim, res, fit, proc)
            return _dumps({
                "ok": True,
                "measurement": res.to_dict(),
                "claim": claim,
                "tier": res.tier,
                "honesty": res.honesty,
                "measured_part": mp.to_dict(),
                "open_demands": demands,
                "gate_ok": ik.gate_ok(ledger),
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def confirm_dimensions(confirmations: list, doc_id: Optional[str] = None,
                           run_id: Optional[str] = None) -> str:
        """Close fit-critical demands with TYPED caliper values (the dims we refuse to guess).

        `confirmations`: [{key, value_mm}] — e.g. [{"key":"output_shaft.dia","value_mm":3.0},
        {"key":"mount_holes.spacing","value_mm":18.0}]. Marks each ledger row measured/given and
        returns what's still blocking + whether the critical-dim gate is now clear (so geometry can
        commit). Persists the updated ledger.

        When `run_id` is supplied each confirmed dimension is ALSO recorded in the run-scoped
        interaction ledger (contracts/interaction.md §5) so a DAG gate for the same run_id
        never re-asks for these values.  Without a run_id the dimension is recorded in the
        DOCUMENT's intake ledger instead (IntakeState, persisted inside the .step+): it
        survives save/close/reopen, so a dimension you confirm today is never re-asked in a
        later session on the same part.
        """
        try:
            d = _doc(doc_id)
            ledger = _load_ledger(d)
            if ledger is None:
                return _dumps({"ok": False, "error": "no ledger yet — run submit_part_photos first"})
            for c in confirmations:
                ik.apply_confirmation(ledger, c.get("key"), c.get("value_mm"))
            _set(d, _K_LEDGER, ledger.to_dict())
            still = [r.key for r in ik.unresolved_critical(ledger)]

            # Route each confirmed dimension through the interaction contract so that
            # the answer is also persisted in the interaction ledger for replay.
            # - When run_id is supplied: persists to the run-scoped FileLedger so a
            #   DAG gate for the same run_id never re-asks (cross-mechanism no-re-ask).
            # - Otherwise: persists to the doc's IntakeLedger (IntakeState meta,
            #   rides the .step+) so the answer survives save/reopen (§5).
            #   `d` is the RESOLVED doc-id string (cad_tools._doc raises when there
            #   is no doc), the same identity the _K_LEDGER write above targets —
            #   so a confirmation always has a valid persistence target here.
            if confirmations:
                questions = []
                for c in confirmations:
                    key = c.get("key")
                    val_mm = c.get("value_mm")
                    if key is not None and val_mm is not None:
                        questions.append({
                            "id": f"intake.dim.{key}",
                            "kind": "dimension",
                            "family": "length",
                            "prompt": f"Caliper value for {key}?",
                            "suggested": {"value": float(val_mm), "unit": "mm"},
                            "required": False,
                            "why": "Fit-critical dimension from caliper measurement.",
                        })
                if questions:
                    _ask_via_contract(questions, run_id=run_id,
                                      doc_id=d if not run_id else None)

            return _dumps({"ok": True, "gate_ok": ik.gate_ok(ledger),
                           "still_blocking": still})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def intake_status(doc_id: Optional[str] = None) -> str:
        """Where the intake stands: bound process, measured parts, the critical-dim ledger, what is
        still blocking first-try fit, and any pending photo/dimension request (the resume token)."""
        try:
            d = _doc(doc_id)
            ledger = _load_ledger(d)
            return _dumps({
                "ok": True,
                "process": _get(d, _K_PROCESS),
                "intake": _get(d, _K_INTAKE),
                "measured_parts": _get(d, _K_MEASURED),
                "gate_ok": ik.gate_ok(ledger) if ledger else True,
                "blocking": [r.key for r in ik.unresolved_critical(ledger)] if ledger else [],
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def fit_report(doc_id: Optional[str] = None, **kwargs: _Extra) -> str:
        """The fit report to review before printing: each mating pair's modeled-vs-source dimension,
        the clearance applied for the process, its source/confidence, and an ok/at_risk verdict, plus
        the assumptions made. Proves first-try fit (or flags what rests on a soft measurement).

        DOCUMENT REPORT (no arguments) — reports the pairs recorded on the part. It needs
        measurements to exist first: `submit_part_photos(claims=[...])` (or
        `measure_from_photo(...)` per dimension) builds `measured_parts` + the
        critical-dimension ledger, then `confirm_dimensions([{key, value_mm}])` closes
        any fit-critical demand, then `fit_report()`. With nothing recorded the tool
        says exactly which of those is missing and what the document currently holds.

        AD-HOC PAIR (explicit diameters) — no measurement flow needed for a one-off
        question; the answer is labelled `mode: "ad_hoc_pair"`, NOT a document report:
            fit_report(bore_diameter=20.02, shaft_diameter=20.0)   # actual fit
            fit_report(bore_diameter=20, tolerance="H7/g6")        # designed fit
            fit_report(bore="d20", band="sliding-fit")             # keyword -> H7/g6
            fit_report(nominal=20, hole="H7", shaft="h7")
        Sizes accept units and the Ø shorthand ("20mm", "0.75in", "d20"); per-side
        classes/tolerances (`bore_tolerance="H7"`, `shaft_tolerance=0.01`) are accepted.
        This is the same engine as `fit_calc` — see its docstring for every accepted
        spelling — and every rewrite is echoed in `interpreted`.
        """
        try:
            kwargs.pop("kwargs", None)
            d = _doc(doc_id)
            proc = _process_id(d)
            measured = _load_measured(d)
            ledger = _load_ledger(d)
            if _has_fit_inputs(kwargs):
                return _dumps(_ad_hoc_fit_report(d, proc, kwargs,
                                                 measured is not None and ledger is not None))
            if measured is None or ledger is None:
                return _dumps(_nothing_measured(d, proc, measured, ledger))
            fr = ik.build_fit_report(measured, ledger, process=proc)
            _set(d, _K_FIT, fr.to_dict())
            return _dumps({"ok": True, "fit_report": fr.to_dict()})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def make_fit_coupon(interface_key: str, doc_id: Optional[str] = None) -> str:
        """Build a small printable FIT-TEST COUPON of one critical interface (bore / bolt-pattern /
        connector cutout) so the user can verify the real fit in a ~20-min print before committing
        the whole part. Builds it as a NEW part from the measured + process-stacked dims and returns
        its doc_id (export it to STL with export_part). The single best first-try-fit de-risk on FDM.
        """
        try:
            d = _doc(doc_id)
            proc = _process_id(d)
            measured = _load_measured(d)
            if measured is None:
                return _dumps({"ok": False, "error": "no measured part to base a coupon on"})
            feat = measured.feature(interface_key)
            spec = ik.coupon_spec(feat if feat is not None else interface_key, process=proc)
            ops = spec.get("ops") or []
            if not ops:
                return _dumps({"ok": False, "error": f"no coupon recipe for {interface_key!r}",
                               "spec": spec})
            r = cb.call("doc_new", {"kind": "part", "name": f"coupon_{interface_key}"})
            cdoc = r.get("doc_id")
            cb.call("doc_apply_ops", {"doc_id": cdoc, "ops": ops})
            return _dumps({"ok": True, "coupon_doc_id": cdoc, "interface": interface_key,
                           "spec": spec, "note": "export_part(fmt='stl') to print it"})
        except Exception as e:  # noqa: BLE001
            return _err(e)
