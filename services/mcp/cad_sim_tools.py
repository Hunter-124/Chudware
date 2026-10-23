"""CAD simulation MCP tools (split out of cad_tools.py): run_fea_* + run_cfd_*."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional
from typing import Union
import copy
import math
import os
import re
import time

from .cad_common import (
    CoreError,
    archive_before_write,
    coerce_json_or_repr,
    require_list,
    require_mapping,
    resolve_agent_path,
    _autosave_all_open,
    _canonical_material_keys,
    _default_conductivity,
    _default_specific_heat,
    _doc,
    _dumps,
    _err,
    _handoff_doc_name,
    _merge_face_sets,
    _persist_study,
    _resolve_face_points,
    _resolve_material_name,
    _resolve_material,
    _sid_match,
    _start_job,
    collect_durable,
    collect_durable_terminal,
    register_durable_collector,
    solve_durable_or_handoff,
    _tmp,
    _SID_RE,
    _SYNC_SOLVE_BUDGET_S,
    cb,
)
from services.sim import jobs as simjobs
from . import cad_common as _cad_jobs


# ------------------------------------------------------- boundary-condition dialect (live fix)
# Measured 2026-07-28, one static-FEA task, claude-sonnet-4-5 — FOUR distinct rejections, every
# one of them a setup the solver fully supports written in a shape the tool didn't read:
#   loads=[{"face": q, "type":"force", "magnitude":200, "direction":[1,0,0]}]
#       -> "load needs 'force':[x,y,z] or 'pressure':<MPa>"   — the SAME vector, polar form.
#   fixtures=[{"faces":["the planar face at Z=0"], ...}], loads=[{"faces":[...], ...}]
#       -> "each load needs a 'face' query"                   — the plural key.
#   loads=[{"face":"Fb7fb00", "force":[0,-2000,0]}]
#       -> "the grammar is TOKENS, not prose"                 — a real face sid it already had.
#   run_fea_static(..., mesh_fineness="coarse")                — run_cfd_accurate's preset word.
# All four are EXACT, lossless conversions, so they are normalized here at the tool boundary,
# before any study payload is built. This is semantic normalization, not argument-NAME aliasing:
# name aliases live only in services/agent/arg_reconciliation.py.

# mesh_fineness keyword <-> number. The words are run_cfd_accurate's / services/sim/foam_case.py
# preset vocabulary (coarse/medium/fine), which is why a model reaches for them here too; 0.5 is
# this file's own default, so "medium" is a no-op rather than a new behaviour.
_FINENESS_WORDS = {
    "coarsest": 0.1,
    "very_coarse": 0.1,
    "verycoarse": 0.1,
    "coarse": 0.25,
    "low": 0.25,
    "draft": 0.25,
    "medium": 0.5,
    "normal": 0.5,
    "default": 0.5,
    "standard": 0.5,
    "moderate": 0.5,
    "fine": 0.75,
    "high": 0.75,
    "finest": 1.0,
    "very_fine": 1.0,
    "veryfine": 1.0,
}

# Signed-axis spellings a model writes instead of a 3-vector `direction`. parse_side's
# convention (core/doc/picks.cpp): Chudware is Z-up, so up=+Z / down=-Z.
_AXIS_DIRS = {
    "+x": (1.0, 0.0, 0.0),
    "x": (1.0, 0.0, 0.0),
    "-x": (-1.0, 0.0, 0.0),
    "+y": (0.0, 1.0, 0.0),
    "y": (0.0, 1.0, 0.0),
    "-y": (0.0, -1.0, 0.0),
    "+z": (0.0, 0.0, 1.0),
    "z": (0.0, 0.0, 1.0),
    "-z": (0.0, 0.0, -1.0),
    "up": (0.0, 0.0, 1.0),
    "down": (0.0, 0.0, -1.0),
}

_BC_SHAPE_HELP = (
    'accepted shapes: {"face": <query>, "force": [fx,fy,fz]} (N, world XYZ), '
    '{"face": <query>, "pressure": <MPa>}, or the polar form '
    '{"face": <query>, "magnitude": <N>, "direction": [dx,dy,dz]}. '
    '"faces": [<query>, ...] is accepted as the plural of "face", and the '
    "unit-suffixed spelling of the SAME quantity is read as the canonical key "
    '("face_query"->face, "magnitude_n"/"force_n"->magnitude, '
    '"pressure_mpa"->pressure, "temperature_c"->temperature).'
)

# ------------------------------------------------------- boundary-condition ITEM KEYS (live fix)
# Measured 2026-07-29, sim suite, claude-sonnet-4-5 (general lane). VERBATIM load entry:
#   {"name":"tip_load","face_query":"the opposite end face","type":"force",
#    "magnitude_n":500,"direction":[0,0,-1]}
#       -> "each load needs a 'face' query" ... next to an accepted-shapes list that ALREADY
#          contains the polar magnitude+direction form the model used. The SHAPE was supported
#          (_polar_to_force); only the two KEY SPELLINGS missed.
# Both renames are lossless and stay inside one quantity:
#   face_query  -> face       a face QUERY is exactly what `face` holds; there is one
#                             selection slot in a BC item, so no other reading exists.
#   magnitude_n -> magnitude  the `_n` is the UNIT the accepted shape already documents
#                             ('"magnitude": <N>'), i.e. the SAME scalar in the SAME newtons.
# Aliasing across a quantity/unit BOUNDARY is deliberately NOT done anywhere in this file —
# see services/agent/arg_reconciliation.QUANTITY_MISMATCH for why (a millimetre size is
# never mapped onto the dimensionless mesh_fineness). run_fea_static and run_fea_contact
# answer that mismatch the other way, by taking a REAL `mesh_size_mm` parameter (an exact
# element size, mutually exclusive with the dial); the refusal still stands for every tool
# that only has the dial. Every rename here is echoed in the tool's
# `interpreted` channel, so the model can see what its key was read as.

# A raw geometry query has no `face`/`faces` wrapper key and is passed through unchanged,
# e.g. {"of":"face","side":"bottom"} or {"kind":"face","surf":"plane",...}. Once a dict
# carries a face-reference wrapper, every sibling is a BC field and is checked below.

# The canonical keys a fixtures/loads/hot_faces/cold_faces item may carry, i.e. exactly the
# keys the solver payload builders below and in run_fea_thermal actually READ.
_BC_ITEM_KEYS = frozenset(
    {
        "face",
        "faces",
        "type",
        "force",
        "pressure",
        "magnitude",
        "direction",
        "temperature",
        "flux_w_m2",
        "h_w_m2k",
        "t_amb_c",
        "schedule",
    }
)

# Purely-cosmetic item labels ("name":"tip_load" in the live payload). They name the load for
# a reader and can never change what the solver computes, so they ride along instead of
# turning an otherwise-correct entry into a refusal. Same closed-list discipline as
# arg_reconciliation.DESCRIPTIVE_ARGS: nothing that could carry geometry, a quantity or a unit.
_BC_ITEM_LABELS = frozenset(
    {"name", "label", "comment", "note", "notes", "description", "title", "caption"}
)

# Normalised (lower, no _ - or space) supplied key -> canonical item key.
_ITEM_KEY_ALIASES: dict[str, str] = {
    # --- the face-reference slot. A BC item has exactly ONE selection slot, so any spelling
    # of "which surface this applies to" is unambiguous HERE.
    "facequery": "face",
    "faceref": "face",
    "facesel": "face",
    "faceselector": "face",
    "facesid": "face",
    "targetface": "face",
    "surface": "face",
    "on": "face",
    "onface": "face",
    "appliedto": "face",
    "facesquery": "faces",
    "facequeries": "faces",
    "facerefs": "faces",
    "faceslist": "faces",
    "surfaces": "faces",
    "targetfaces": "faces",
    # --- the scalar magnitude. `_n` / `newtons` is the unit the accepted shape already
    # states, so these are the SAME quantity, not a conversion.
    "magnituden": "magnitude",
    "magnitudenewtons": "magnitude",
    "forcen": "magnitude",
    "forcenewtons": "magnitude",
    "forcemagnitude": "magnitude",
    "forcemagnituden": "magnitude",
    "loadn": "magnitude",
    "loadmagnitude": "magnitude",
    # --- pressure / temperature / flux / film coefficient: unit-suffixed spellings of the
    # unit this tool already documents (MPa, °C, W/m², W/m²K).
    "pressurempa": "pressure",
    "temperaturec": "temperature",
    "temperaturedegc": "temperature",
    "tempc": "temperature",
    "temp": "temperature",
    "flux": "flux_w_m2",
    "heatflux": "flux_w_m2",
    "heatfluxwm2": "flux_w_m2",
    "h": "h_w_m2k",
    "htc": "h_w_m2k",
    "htcwm2k": "h_w_m2k",
    "filmcoefficient": "h_w_m2k",
    "convectioncoefficient": "h_w_m2k",
    "tamb": "t_amb_c",
    "tambientc": "t_amb_c",
    "ambienttemperature": "t_amb_c",
    "ambienttempc": "t_amb_c",
    # --- direction / kind. DELIBERATELY EXCLUDED: `axis` (an axis is unsigned, a direction is
    # signed) and `load_type` (run_fea_fatigue's top-level load_type names the cyclic SPECTRUM
    # "reversed"/"zero_based", a different thing from a BC kind — so it stays a refusal).
    "directionvector": "direction",
    "dir": "direction",
    "forcedirection": "direction",
    "loaddirection": "direction",
    "kind": "type",
    "bctype": "type",
    "fixturetype": "type",
    "constrainttype": "type",
}

_FACE_REF_NORMS = frozenset(
    {"face", "faces"}
    | {n for n, c in _ITEM_KEY_ALIASES.items() if c in ("face", "faces")}
)

# A dict with no face reference can still plainly be a boundary-condition wrapper: the
# malformed live load carried type/magnitude/direction but omitted only its face. Detect those
# shapes so they receive the face-reference diagnostic instead of being mistaken for a raw
# geometry query. `kind="face"` remains a raw query; kind/type alone is a wrapper only when its
# value names a boundary-condition kind.
_BC_WRAPPER_TYPES = frozenset(
    {
        "fixed",
        "clamped",
        "encastre",
        "roller",
        "force",
        "pressure",
    }
)


def _is_bc_wrapper(entry: dict) -> bool:
    norms = {_norm_key(key) for key in entry}
    if norms & _FACE_REF_NORMS:
        return True
    for norm in norms:
        canon = _ITEM_KEY_ALIASES.get(norm, _CANON_ITEM_BY_NORM.get(norm))
        if canon not in (None, "face", "faces", "type"):
            return True
    for key, value in entry.items():
        norm = _norm_key(key)
        canon = _ITEM_KEY_ALIASES.get(norm, _CANON_ITEM_BY_NORM.get(norm))
        if canon == "type" and str(value).strip().lower() in _BC_WRAPPER_TYPES:
            return True
    return False


# Face-RELATIVE direction words. See _norm_direction: these are REFUSED, not resolved.
_FACE_RELATIVE_DIRS = frozenset(
    {
        "normal",
        "facenormal",
        "normaltoface",
        "surfacenormal",
        "alongnormal",
        "alongthenormal",
        "perpendicular",
        "perpendiculartoface",
        "inward",
        "inwards",
        "outward",
        "outwards",
        "intoface",
        "intotheface",
        "outofface",
    }
)


def _norm_key(name) -> str:
    """Fold a dict key for matching: case / underscore / dash / space are noise."""
    return str(name).lower().replace("_", "").replace("-", "").replace(" ", "")


_CANON_ITEM_BY_NORM = {_norm_key(k): k for k in _BC_ITEM_KEYS}
_CANON_LABEL_BY_NORM = {_norm_key(k): k for k in _BC_ITEM_LABELS}


def _finite_float(value, where: str) -> float:
    """A real finite scalar, with the public argument named in the error."""
    if isinstance(value, bool):
        raise ValueError(f"{where} must be a finite number, got {value!r}.")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where} must be a finite number, got {value!r}.") from None
    if not math.isfinite(out):
        raise ValueError(f"{where} must be finite, got {value!r}.")
    return out


def _positive_int(value, where: str) -> int:
    """A strictly-positive integer; do not silently truncate 3.7 to 3."""
    if isinstance(value, bool):
        raise ValueError(f"{where} must be a positive integer, got {value!r}.")
    try:
        out = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(
            f"{where} must be a positive integer, got {value!r}."
        ) from None
    try:
        exact = float(value)
    except (TypeError, ValueError, OverflowError):
        exact = float(out)
    if out <= 0 or not math.isfinite(exact) or exact != out:
        raise ValueError(f"{where} must be a positive integer, got {value!r}.")
    return out


_MATERIAL_INPUT_KEY_NORMS = frozenset(
    {
        "name",
        "e",
        "empa",
        "youngsmodulusmpa",
        "youngmodulusmpa",
        "elasticmodulusmpa",
        "nu",
        "poisson",
        "poissonsratio",
        "poissonratio",
        "yieldmpa",
        "yieldstrengthmpa",
        "sigmaympa",
        "densitygcm3",
        "density",
        "rhogcm3",
        "ultimatempa",
        "endurancempa",
        "conductivitywmk",
        "kwmk",
        "cpjkgk",
    }
)

_THERMAL_MATERIAL_KEYS = {
    "conductivitywmk": "conductivity_w_mk",
    "kwmk": "conductivity_w_mk",
    "cpjkgk": "cp_j_kgk",
}


def _canonical_thermal_material_keys(material: dict) -> dict:
    """Canonicalize the thermal fields without invoking mechanical-property validation."""
    mechanical = _canonical_material_keys(material)
    out: dict = {}
    aliased: dict = {}
    for key, value in mechanical.items():
        canon = _THERMAL_MATERIAL_KEYS.get(_norm_key(key))
        if canon is None:
            out[key] = value
        elif key == canon:
            out[canon] = value
        else:
            aliased[canon] = value
    for canon, value in aliased.items():
        out.setdefault(canon, value)
    return out


def _resolve_study_material(
    material, analysis: str, *, conductivity_w_mk=None, specific_heat_j_kgk=None
) -> dict:
    """Resolve a material and enforce only the properties the selected physics consumes."""
    raw = coerce_json_or_repr(material)
    if isinstance(raw, dict):
        # `plasticity` is a LAW, not a property, and only two studies solve it. Naming
        # it here means a caller that put it in the material dict — or aimed it at
        # modal/buckling/thermal/fatigue, where it would be an elastic answer under a
        # plastic label — is told where it belongs instead of reading a generic
        # unsupported-property list.
        if any(_norm_key(k) == "plasticity" for k in raw):
            raise ValueError(
                "material.plasticity is not a material property and is not accepted in "
                "the material dict. Small-strain J2 plasticity is a parameter of the "
                "STATIC and CONTACT solvers only: pass "
                'plasticity={"yield_mpa": 276, "hardening_mpa": 1200} to '
                "run_fea_static or run_fea_contact. It is refused for modal "
                "(eigenvalues linearize about an unstressed state, so they stay "
                "elastic), buckling (a critical multiplier is defined only for a "
                "linear pre-buckling response), thermal (no stress to bound) and "
                "fatigue (its S-N curves are nominal-elastic-stress curves)."
            )
        if analysis == "static" and "per_body" in raw:
            unknown = [k for k in raw if k not in ("per_body", "default")]
            if unknown:
                raise ValueError(
                    f"material has unsupported properties {unknown!r}; refusing to ignore them. "
                    "A per-body material accepts only per_body and default."
                )
            per_body = raw["per_body"]
            if not isinstance(per_body, dict):
                raise ValueError(
                    "material.per_body must be an object mapping one-based STEP solid ordinals "
                    "to material names or property objects."
                )
            resolved: dict = {}
            for ordinal, value in per_body.items():
                key = str(ordinal)
                if re.fullmatch(r"[1-9][0-9]*", key) is None:
                    raise ValueError(
                        f"material.per_body key {ordinal!r} must be a one-based STEP solid ordinal."
                    )
                if key in resolved:
                    raise ValueError(f"material.per_body repeats body ordinal {key}.")
                leaf = coerce_json_or_repr(value)
                if isinstance(leaf, dict):
                    leaf = dict(leaf)
                    leaf.setdefault("yield_mpa", 250.0)
                if isinstance(leaf, dict) and "per_body" in leaf:
                    raise ValueError(
                        f"material.per_body[{key}] must be a material name or property object, "
                        "not another per_body assignment."
                    )
                resolved[key] = _resolve_study_material(leaf, analysis)
            out = {"per_body": resolved}
            if "default" in raw:
                leaf = coerce_json_or_repr(raw["default"])
                if isinstance(leaf, dict):
                    leaf = dict(leaf)
                    leaf.setdefault("yield_mpa", 250.0)
                if isinstance(leaf, dict) and "per_body" in leaf:
                    raise ValueError(
                        "material.default must be a material name or property object, "
                        "not another per_body assignment."
                    )
                out["default"] = _resolve_study_material(leaf, analysis)
            elif not resolved:
                raise ValueError("material.per_body is empty and no default material was supplied.")
            return out
        unknown = [k for k in raw if _norm_key(k) not in _MATERIAL_INPUT_KEY_NORMS]
        if unknown:
            raise ValueError(
                f"material has unsupported properties {unknown!r}; refusing to ignore them. "
                "Use E_mpa, nu, density_g_cm3, yield_mpa, ultimate_mpa, endurance_mpa, "
                "conductivity_w_mk/k_w_mk, or cp_j_kgk."
            )

    thermal = analysis in ("thermal_steady", "thermal_transient")
    explicit = isinstance(raw, dict)
    if thermal:
        resolved = raw if explicit else _resolve_material_name(raw)
        mat = _canonical_thermal_material_keys(resolved)
        if conductivity_w_mk is not None:
            mat["conductivity_w_mk"] = conductivity_w_mk
        elif not explicit and mat.get("conductivity_w_mk") is None:
            mat["conductivity_w_mk"] = _default_conductivity(mat, raw)
        if analysis == "thermal_transient":
            if specific_heat_j_kgk is not None:
                mat["cp_j_kgk"] = specific_heat_j_kgk
            elif not explicit and mat.get("cp_j_kgk") is None:
                mat["cp_j_kgk"] = _default_specific_heat(mat, raw)
    else:
        mat = _resolve_material(raw)

    required_by_analysis = {
        "static": ("E_mpa", "nu", "yield_mpa"),
        "buckling": ("E_mpa", "nu"),
        "fatigue": ("E_mpa", "nu", "yield_mpa", "ultimate_mpa", "endurance_mpa"),
        "modal": ("E_mpa", "nu", "density_g_cm3"),
        "frequency_response": ("E_mpa", "nu", "density_g_cm3"),
        "thermal_steady": ("conductivity_w_mk",),
        "thermal_transient": ("density_g_cm3", "conductivity_w_mk", "cp_j_kgk"),
    }
    for key in required_by_analysis.get(analysis, ()):
        value = _finite_float(mat.get(key), f"material.{key}")
        if key != "nu" and value <= 0.0:
            raise ValueError(
                f"{analysis} requires material.{key} > 0, got {mat.get(key)!r}. "
                "Choose a material carrying that property or pass it explicitly."
            )
        mat[key] = value
    return mat


def _canon_item_keys(e: dict, where: str, notes: Optional[list] = None) -> dict:
    """Rename a boundary-condition wrapper's key spellings onto the canonical ones.

    A wrapper normally carries a face reference, but a malformed wrapper can omit only that
    slot while still carrying unmistakable BC fields such as force, magnitude or temperature.
    Those entries are canonicalized too so the caller receives the missing-face contract.
    Raw geometry query / query-result match dicts are handed to the engine grammar verbatim.

    There is no catch-all: a key that is neither canonical, a known spelling, nor a cosmetic
    label is REFUSED naming the real vocabulary. Silently dropping it would hide a setting the
    caller believed it had applied, which is exactly the failure mode this file exists to
    prevent."""
    if not _is_bc_wrapper(e):
        return e
    out: dict = {}
    renamed: list = []
    for k, v in e.items():
        if k in _BC_ITEM_KEYS or k in _BC_ITEM_LABELS:
            out[k] = v
            continue
        norm = _norm_key(k)
        label = _CANON_LABEL_BY_NORM.get(norm)
        if label is not None:
            if label in out:
                raise ValueError(f"{where}: duplicate descriptive key {k!r}/{label!r}.")
            out[label] = v
            renamed.append(f"{k!r} as {label!r}")
            continue
        canon = _ITEM_KEY_ALIASES.get(norm, _CANON_ITEM_BY_NORM.get(norm))
        if canon is None:
            raise ValueError(
                f"{where}: unknown key {k!r} in this boundary-condition entry — it is not a "
                f"spelling of any accepted key, and guessing would run a load you did not "
                f"ask for. Accepted keys: {sorted(_BC_ITEM_KEYS)} (plus an optional "
                f"descriptive {sorted(_BC_ITEM_LABELS)}). " + _BC_SHAPE_HELP
            )
        if canon in e or canon in out:
            other = e[canon] if canon in e else out[canon]
            raise ValueError(
                f"{where}: {k!r} is another spelling of {canon!r} and BOTH were given "
                f"({v!r} vs {other!r}) — that is ambiguous. Pass exactly one."
            )
        out[canon] = v
        renamed.append(f"{k!r} as {canon!r}")
    if not renamed:
        return e
    if notes is not None:
        notes.append(f"{where}: read " + ", ".join(renamed))
    return out


def _interpreted(out: dict, notes: list) -> dict:
    """Echo BC key renames onto a SUCCESSFUL result under `interpreted`.

    A rename the model cannot see is a rename it keeps re-learning: it wrote `magnitude_n`,
    got a stress number back, and has no way to know the canonical key is `magnitude`. Only
    attached when something was actually rewritten, so a canonical call's payload is
    byte-identical to what it was before."""
    if notes:
        out["interpreted"] = list(notes)
    return out


def _norm_fineness(value, arg_name: str = "mesh_fineness") -> float:
    """mesh_fineness as a finite 0..1 float, accepting documented preset words."""
    if isinstance(value, str):
        key = value.strip().lower().replace("-", "_").replace(" ", "_")
        if key in _FINENESS_WORDS:
            return _FINENESS_WORDS[key]
    try:
        out = _finite_float(value, arg_name)
    except ValueError:
        if isinstance(value, str):
            raise ValueError(
                f"{arg_name} must be a number 0..1 or one of "
                f"{sorted(set(_FINENESS_WORDS))} — got {value!r}."
            ) from None
        raise
    if not 0.0 <= out <= 1.0:
        raise ValueError(f"{arg_name} must be in 0..1, got {value!r}.")
    return out


def _advisor_params(learned_advisor: bool, advisor_max_dof: int) -> dict:
    """Validate the default-off Polyhedral-FEA advisor controls."""
    if not isinstance(learned_advisor, bool):
        raise ValueError("learned_advisor must be true or false")
    if isinstance(advisor_max_dof, bool) or not isinstance(advisor_max_dof, int):
        raise ValueError("advisor_max_dof must be a non-negative integer")
    if advisor_max_dof < 0 or advisor_max_dof > 0xFFFFFFFF:
        raise ValueError("advisor_max_dof must be from 0 through 4294967295")
    return {"learned_advisor": learned_advisor, "advisor_max_dof": advisor_max_dof}


def _norm_direction(d, where: str) -> list:
    """A `direction` value -> a UNIT 3-vector. Normalizing first is the whole point: a
    non-unit direction must never silently scale the load magnitude. Zero length is an
    error, not a silent no-op force."""
    raw = d
    if isinstance(d, str):
        s = d.strip().lower()
        if s in _AXIS_DIRS:
            return list(_AXIS_DIRS[s])
        if _norm_key(s) in _FACE_RELATIVE_DIRS:
            # DELIBERATE REFUSAL, not a default. Live 2026-07-29 a load arrived as
            # {"face":"right face","force_n":1200,"direction":"normal"}. Two independent
            # reasons this cannot be resolved into a vector here, and neither is fixable by
            # picking an axis: (1) direction is normalized at the tool BOUNDARY, before any
            # face query is resolved, so there is no face normal in hand yet; (2) even with
            # the face resolved, "normal" does not say which SIGN — outward is tension,
            # inward is compression, opposite answers from the same input. Defaulting to an
            # axis would solve a different load and report success, so refuse and name the
            # two shapes that DO express a normal-acting load.
            raise ValueError(
                f"{where}: 'direction' {raw!r} is a FACE-RELATIVE word, not a vector, and it "
                "cannot be resolved here: directions are normalized before any face query is "
                "resolved, and 'normal' does not say which SIGN it means (outward = tension, "
                "inward = compression). Give an explicit vector 'direction': [dx,dy,dz] (any "
                "length; it is normalized) — query_geometry(<your face query>) reports the "
                "face `axis`, so you can copy it and choose the sign. For a load that acts "
                'along the face normal BY DEFINITION, use {"face": <query>, "pressure": '
                "<MPa>} (inward on the face) instead of a force."
            )
        d = coerce_json_or_repr(d)
    if isinstance(d, dict):
        if set(d) != {"x", "y", "z"}:
            raise ValueError(
                f"{where}: direction object must contain exactly x, y, z; got "
                f"{sorted(str(k) for k in d)}."
            )
        d = [d["x"], d["y"], d["z"]]
    if not isinstance(d, (list, tuple)) or len(d) != 3:
        raise ValueError(
            f"{where}: 'direction' must be [dx,dy,dz] (or one of "
            f"{sorted(_AXIS_DIRS)}), got {raw!r}."
        )
    comps = [_finite_float(x, f"{where}.direction[{i}]") for i, x in enumerate(d)]
    n = math.sqrt(sum(c * c for c in comps))
    if not math.isfinite(n) or n <= 1e-12:
        raise ValueError(
            f"{where}: 'direction' {raw!r} has zero length or a non-finite "
            "component — it cannot point anywhere. Give a real direction "
            "[dx,dy,dz] (any length; it is normalized), or drop "
            "magnitude/direction and pass "
            "force:[fx,fy,fz] directly."
        )
    return [c / n for c in comps]


def _split_face_list(e: dict, where: str) -> list:
    """Fan a plural `faces: [q1, q2]` entry out to one entry per face, siblings preserved."""
    if "faces" in e and "face" in e:
        raise ValueError(
            f"{where}: both 'face' and 'faces' were given ({e['face']!r} vs "
            f"{e['faces']!r}) — that is ambiguous. Pass exactly one: 'face' for "
            "a single query, 'faces' for a list."
        )
    key = "faces" if "faces" in e else ("face" if "face" in e else None)
    if key is None:
        return [e]
    v = e[key]
    if isinstance(v, str) and v.strip()[:1] in ("[", "{"):
        v = coerce_json_or_repr(v)
    if isinstance(v, (list, tuple)):
        if not v:
            raise ValueError(
                f"{where}: {key!r} is an empty list — there is nothing to apply "
                "this boundary condition to. Name at least one face query "
                "(query_geometry(...) or describe_part(doc_id) to find one)."
            )
        rest = {k: x for k, x in e.items() if k != key}
        return [{**rest, "face": f} for f in v]
    if key == "faces":
        rest = {k: x for k, x in e.items() if k != key}
        return [{**rest, "face": v}]
    return [e]


def _force_vector(value, where: str) -> list:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(
            f"{where}: 'force' must be exactly [fx,fy,fz] in newtons, got {value!r}."
        )
    return [_finite_float(x, f"{where}.force[{i}]") for i, x in enumerate(value)]


def _polar_to_force(e: dict, where: str) -> dict:
    """Normalize one mechanical load without discarding contradictory or unused fields."""
    kind = str(e.get("type") or "").strip().lower().replace("-", "_")
    if kind in ("vector", "vector_force", "load"):
        kind = "force"
    if kind and kind not in ("force", "pressure"):
        raise ValueError(
            f"{where}: unsupported load type {e.get('type')!r}; use 'force' or "
            "'pressure'."
        )
    has_force = "force" in e
    has_pressure = "pressure" in e
    if has_force and has_pressure:
        raise ValueError(
            f"{where}: both 'force' and 'pressure' were supplied; pass exactly "
            "one because they are different load models."
        )
    if "direction" in e and e.get("magnitude") is None:
        raise ValueError(
            f"{where}: 'direction' is only meaningful with 'magnitude'; pass "
            "magnitude + direction or an explicit force vector."
        )

    if has_force:
        given = _force_vector(e["force"], where)
        if kind == "pressure":
            raise ValueError(
                f"{where}: type 'pressure' contradicts the supplied force vector."
            )
        if e.get("magnitude") is not None:
            mag = _finite_float(e["magnitude"], f"{where}.magnitude")
            if mag < 0.0:
                raise ValueError(
                    f"{where}: magnitude must be non-negative; put the sign in "
                    "the direction vector."
                )
            if "direction" in e:
                expected = [c * mag for c in _norm_direction(e["direction"], where)]
                tol = 1e-6 * max(1.0, abs(mag))
                if any(abs(a - b) > tol for a, b in zip(given, expected)):
                    raise ValueError(
                        f"{where}: 'force' {e['force']!r} disagrees with magnitude {mag:g} x "
                        f"direction {e['direction']!r} — pass one representation."
                    )
            else:
                nf = math.sqrt(sum(c * c for c in given))
                if abs(nf - mag) > 1e-6 * max(1.0, abs(mag)):
                    raise ValueError(
                        f"{where}: force magnitude {nf:g} disagrees with "
                        f"'magnitude' {mag:g}; pass one representation."
                    )
        out = {
            k: v
            for k, v in e.items()
            if k not in ("force", "magnitude", "direction", "type")
        }
        out["force"] = given
        return out

    if has_pressure:
        pressure = _finite_float(e["pressure"], f"{where}.pressure")
        if kind == "force":
            raise ValueError(
                f"{where}: type 'force' contradicts the supplied pressure."
            )
        if "direction" in e:
            raise ValueError(
                f"{where}: pressure acts along the face normal and takes no direction."
            )
        if e.get("magnitude") is not None:
            mag = _finite_float(e["magnitude"], f"{where}.magnitude")
            if abs(pressure - mag) > 1e-6 * max(1.0, abs(mag)):
                raise ValueError(
                    f"{where}: pressure {pressure:g} and magnitude {mag:g} "
                    "disagree; pass one representation."
                )
        out = {k: v for k, v in e.items() if k not in ("pressure", "magnitude", "type")}
        out["pressure"] = pressure
        return out

    if e.get("magnitude") is None:
        if kind:
            raise ValueError(
                f"{where}: type {kind!r} was supplied without its load value. "
                + _BC_SHAPE_HELP
            )
        return e
    mag = _finite_float(e["magnitude"], f"{where}.magnitude")
    if kind == "pressure":
        if "direction" in e:
            raise ValueError(
                f"{where}: pressure acts along the face normal and takes no direction."
            )
        out = {k: v for k, v in e.items() if k not in ("magnitude", "type")}
        out["pressure"] = mag
        return out
    if "direction" not in e:
        raise ValueError(
            f"{where}: 'magnitude':{mag:g} was given with no 'direction', so "
            "the load has no orientation. " + _BC_SHAPE_HELP
        )
    if mag < 0.0:
        raise ValueError(
            f"{where}: magnitude must be non-negative; put the sign in the "
            "direction vector."
        )
    unit = _norm_direction(e["direction"], where)
    out = {k: v for k, v in e.items() if k not in ("magnitude", "direction", "type")}
    out["force"] = [c * mag for c in unit]
    return out


def _norm_bc_list(
    entries: list,
    arg_name: str,
    *,
    unwrap_query: bool = False,
    notes: Optional[list] = None,
) -> list:
    """Normalize fixtures, mechanical loads and thermal BCs at the tool boundary.

    Fixture wrappers are validated and unwrapped without ever entering mechanical-load
    normalization. Other wrappers retain their siblings and normalize force representations.
    Raw geometry queries remain byte-identical.
    """
    out: list = []
    for i, raw in enumerate(entries):
        where = f"{arg_name}[{i}]"
        e = raw
        if isinstance(e, str) and e.strip()[:1] == "{":
            e = coerce_json_or_repr(e)
        if not isinstance(e, dict):
            out.append(raw)
            continue
        wrapper = _is_bc_wrapper(e)
        if not wrapper:
            out.append(e)
            continue
        e = _canon_item_keys(e, where, notes)
        if "face" not in e and "faces" not in e:
            if arg_name == "loads":
                raise ValueError(
                    f"each load needs a 'face' query: {e!r}. " + _BC_SHAPE_HELP
                )
            label = "fixture" if unwrap_query else f"{arg_name} entry"
            raise ValueError(
                f"{where}: each {label} needs a 'face' query; pass the target surface "
                "under 'face' (one query) or 'faces' (a list)."
            )
        for one in _split_face_list(e, where):
            if unwrap_query:
                extras = set(one) - {"face", "type"} - _BC_ITEM_LABELS
                if extras:
                    raise ValueError(
                        f"{where}: fixture entries only support a face and type='fixed'; "
                        f"unsupported fields {sorted(extras)!r} would otherwise be ignored."
                    )
                fixture_type = str(one.get("type") or "fixed").strip().lower()
                if fixture_type not in ("fixed", "clamped", "encastre"):
                    raise ValueError(
                        f"{where}: unsupported fixture type {one.get('type')!r}; this solver "
                        "supports only fully fixed/clamped faces."
                    )
                out.append(one["face"])
            else:
                out.append(_polar_to_force(one, where))
    return out


def _reject_ignored_fields(entry: dict, allowed: set, where: str) -> None:
    unknown = set(entry) - allowed - _BC_ITEM_LABELS
    if unknown:
        raise ValueError(
            f"{where} has unsupported fields {sorted(unknown)!r}; refusing to ignore them. "
            f"Accepted fields: {sorted(allowed)}."
        )


def _validate_thermal_schedule(schedule, where: str) -> bool:
    """Validate one flux schedule and return whether it is ever non-zero."""
    if schedule is None:
        return True
    if not isinstance(schedule, dict):
        raise ValueError(f"{where} must be an object, got {schedule!r}.")
    has_duty = "duty" in schedule
    has_points = "points" in schedule
    if has_duty == has_points:
        raise ValueError(f"{where} needs exactly one of 'duty' or 'points'.")
    _reject_ignored_fields(schedule, {"duty", "points", "period_s"}, where)
    period = None
    if "period_s" in schedule:
        period = _finite_float(schedule["period_s"], f"{where}.period_s")
        if period <= 0.0:
            raise ValueError(f"{where}.period_s must be > 0.")
    if has_duty:
        duty = _finite_float(schedule["duty"], f"{where}.duty")
        if not 0.0 <= duty <= 1.0:
            raise ValueError(f"{where}.duty must be in [0,1].")
        if duty < 1.0 and period is None:
            raise ValueError(f"{where} with duty < 1 needs period_s > 0.")
        return duty > 0.0
    points = schedule["points"]
    if not isinstance(points, (list, tuple)) or not points:
        raise ValueError(
            f"{where}.points must be a non-empty list of [t_s, scale] pairs."
        )
    times: list = []
    scales: list = []
    for i, point in enumerate(points):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(f"{where}.points[{i}] must be [t_s, scale].")
        times.append(_finite_float(point[0], f"{where}.points[{i}][0]"))
        scales.append(_finite_float(point[1], f"{where}.points[{i}][1]"))
    if times[0] != 0.0 or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError(
            f"{where}.points times must start at 0 and be strictly ascending."
        )
    if period is not None and period <= times[-1]:
        raise ValueError(f"{where}.period_s must be greater than the last breakpoint.")
    return any(abs(scale) > 0.0 for scale in scales)


def _sim_face_points(doc_id: str, query) -> list:
    """_resolve_face_points, plus the raw face SID handle a model pastes back from an earlier
    describe_part / query_geometry result.

    The ENGINE query grammar (core/doc/picks.h kQueryGrammar) has no sid predicate, so a sid
    string cannot be a query — but a sid IS resolvable to the very selector doc_face_points
    needs, by scanning the live roster (cad_common._sid_match). Live 2026-07-28 a model passed
    the real face id "Fb7fb00" it had just been handed and got "the grammar is TOKENS, not
    prose", which teaches nothing about the id it was holding."""
    if isinstance(query, str):
        s = query.strip()
        if _SID_RE.fullmatch(s):
            m = _sid_match(doc_id, s)
            if m is not None:
                fp = cb.call("doc_face_points", {"doc_id": doc_id, "selector": m})
                if not fp.get("points"):
                    raise ValueError(
                        f"face sid {s!r} resolved but carries no surface points."
                    )
                return [fp]
            raise ValueError(
                f"No face with sid {s!r} in this document (sids change whenever the model "
                "changes). Call describe_part(doc_id) for the current face/edge sid roster, "
                "or query_geometry(...) for a geometry query."
            )
        if re.fullmatch(r"[fF]\d+", s):
            raise ValueError(
                f"{s!r} is a FEATURE id, not a face — a boundary condition needs a face. "
                "Call describe_part(doc_id) for the face sid roster (F58db6d style, which "
                "this tool accepts directly), or query_geometry(...) for a geometry query."
            )
    return _resolve_face_points(doc_id, query)


def _selector_text(query) -> str:
    """A short, quotable label for the face query one BC faceset came from.

    Stamped onto every resolved faceset as `selector` so an engine refusal can name
    something the caller can act on. A mesh ordinal cannot: one face query expands to
    as many facesets as it matched, so `fixtures[3]` in the study is not fixtures[3]
    in the request.
    """
    text = query.strip() if isinstance(query, str) else repr(query)
    return text[:120]


def _bc_facesets(study: dict):
    """(kind, index, faceset) for every resolved BC faceset of a mechanical study."""
    for i, fs in enumerate(study.get("fixtures") or []):
        if isinstance(fs, dict):
            yield "fixture", i, fs
    for i, spec in enumerate(study.get("loads") or []):
        fs = spec.get("faces") if isinstance(spec, dict) else None
        if isinstance(fs, dict):
            yield "load", i, fs


def _warn_bc_filters_across_clearance(
    study: dict, max_gap_mm: float, notes: list
) -> None:
    """Warn BEFORE a contact solve when a face filter reaches across the clearance.

    A plane or cylinder/quadric faceset carries a TIGHT `perp_tol_mm` (doc_face_points
    emits 1.5 mm) — the distance from the analytic surface within which a mesh node
    counts as ON that face. Across a contact interface that tolerance competes with the
    clearance the study exists to open and close: at perp_tol_mm 1.5 mm over a 0.35 mm
    fit the filter resolves the MATING body's face too, and a fixture that holds BOTH
    sides of an interface grounds the joint instead of loading it — the live keyway
    isolate converged, with a 0% equilibrium error, carrying 9.9 N of a 1500 N load.

    The engine refuses such a selection once it has the mesh (core/sim/fea_selection.h),
    but this comparison needs neither a mesh nor a solve. A contact solve is minutes;
    saying it here costs one float compare, so the caller is not told after the fact.
    """
    loose: list = []
    for kind, index, fs in _bc_facesets(study):
        perp = fs.get("perp_tol_mm")
        if isinstance(perp, bool) or not isinstance(perp, (int, float)):
            continue
        if isinstance(fs.get("surface"), dict):
            shape = "cylinder/quadric"
        elif fs.get("face_normal"):
            shape = "plane"
        else:
            continue  # pure-Euclidean faceset: perp_tol is not consulted at all
        if float(perp) <= max_gap_mm:
            continue
        selector = fs.get("selector") or f"{kind}s[{index}]"
        loose.append(f"{kind} {selector} ({shape} filter, perp_tol_mm {float(perp):g})")
    if not loose:
        return
    notes.append(
        "SELECTION WARNING (pre-solve): "
        + "; ".join(loose)
        + f" — that tolerance is WIDER than max_gap_mm={max_gap_mm:g}, so the filter "
        "reaches across the very interface it is meant to sit on one side of and may "
        "resolve the mating body's face as well. A fixture or load that lands on both "
        "bodies of a contact pair grounds (or loads) the joint through the SELECTION "
        "instead of through the contact: the study then converges, with a 0% "
        "equilibrium error, on a non-answer — the live keyway isolate carried 9.9 N of "
        "a 1500 N load that way. The engine REFUSES that selection naming both bodies "
        "and the count captured on each; to avoid the round trip, select by body/face "
        "id, or pick a face whose own clearance to the mating body exceeds the "
        "tolerance."
    )


def _require_sim_geometry(doc_id: str) -> list:
    """Cheap setup gate shared by public solvers before export/meshing."""
    result = cb.call("asm_world_bodies", {"doc_id": doc_id, "explode": False})
    bodies = result.get("bodies") if isinstance(result, dict) else None
    if not isinstance(bodies, list) or not bodies:
        raise ValueError(
            "simulation needs at least one solid body; the document has no resolvable "
            "geometry. Build/import the part or assembly before creating a study."
        )
    for index, body in enumerate(bodies):
        bbox = body.get("bbox") if isinstance(body, dict) else None
        lo = bbox.get("min") if isinstance(bbox, dict) else None
        hi = bbox.get("max") if isinstance(bbox, dict) else None
        if not (
            isinstance(lo, (list, tuple))
            and isinstance(hi, (list, tuple))
            and len(lo) == 3
            and len(hi) == 3
        ):
            raise ValueError(
                f"simulation body {index} has no finite 3-D bounding box; it cannot be meshed "
                "as a solid."
            )
        spans = [
            _finite_float(hi[i], f"body[{index}].bbox.max[{i}]")
            - _finite_float(lo[i], f"body[{index}].bbox.min[{i}]")
            for i in range(3)
        ]
        if any(span <= 1e-9 for span in spans):
            raise ValueError(
                f"simulation body {index} is degenerate ({spans!r} mm); solid FEA/CFD "
                "requires non-zero span on all three axes."
            )
    return bodies


def _validate_flow_result(res: dict, expected_velocity: float) -> dict:
    """Validate the physical scalar envelope of a successful fast-CFD solve."""
    if not isinstance(res, dict):
        raise ValueError("flow solver returned a non-object result")
    if not isinstance(res.get("converged"), bool):
        raise ValueError("flow solver returned no convergence status")
    required = (
        "drag_n",
        "lift_n",
        "cd",
        "frontal_area_mm2",
        "reynolds",
        "ref_length_mm",
        "velocity_ms",
        "residual",
        "voxel_mm",
    )
    values = {
        key: _finite_float(res.get(key), f"flow result.{key}") for key in required
    }
    for key in (
        "drag_n",
        "cd",
        "frontal_area_mm2",
        "reynolds",
        "ref_length_mm",
        "velocity_ms",
        "voxel_mm",
    ):
        if values[key] <= 0.0:
            raise ValueError(
                f"flow result.{key} must be > 0 under a non-zero freestream; got "
                f"{values[key]:g}. Refusing a degenerate zero-field result."
            )
    if not math.isclose(
        values["velocity_ms"], expected_velocity, rel_tol=1e-9, abs_tol=1e-12
    ):
        raise ValueError(
            f"flow result.velocity_ms={values['velocity_ms']:g} does not match the requested "
            f"{expected_velocity:g} m/s"
        )
    for key in ("min_feature_mm", "min_feature_voxels"):
        if key in res and res[key] is not None:
            value = _finite_float(res[key], f"flow result.{key}")
            if value <= 0.0:
                raise ValueError(f"flow result.{key} must be > 0 when reported")
    if "steps_run" in res:
        _positive_int(res["steps_run"], "flow result.steps_run")
    return values


def _validate_transient_thermal_result(res: dict, t_end: float) -> dict:
    """Validate a complete transient thermal envelope before persistence/reporting."""
    counts = {
        key: _positive_int(res.get(key), f"thermal result.{key}")
        for key in ("n_steps", "n_nodes", "n_elems")
    }
    # Thermal n_dof is the number of free scalar unknowns, not total nodal DOFs. It can be
    # zero for a fully prescribed field and must never exceed n_nodes.
    if res.get("n_dof") is not None:
        exact = _finite_float(res["n_dof"], "thermal result.n_dof")
        n_dof = int(exact)
        if exact != n_dof or n_dof < 0 or n_dof > counts["n_nodes"]:
            raise ValueError("thermal result.n_dof must be an integer in [0,n_nodes]")
        counts["n_dof"] = n_dof
    frames = res.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("thermal solver reported success but returned no frames")
    frame_summaries: list[dict] = []
    frame_fields: list[list[float]] = []
    times: list[float] = []
    for i, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise ValueError(f"thermal frame {i} is not an object")
        temps = frame.get("temperature")
        if not isinstance(temps, list) or len(temps) != counts["n_nodes"]:
            got = len(temps) if isinstance(temps, list) else 0
            raise ValueError(
                f"thermal frame {i} contains {got}/{counts['n_nodes']} nodal temperatures"
            )
        clean = [
            _finite_float(value, f"thermal frame {i}.temperature[{j}]")
            for j, value in enumerate(temps)
        ]
        time_s = _finite_float(frame.get("t_s"), f"thermal frame {i}.t_s")
        if time_s < 0.0 or time_s > t_end + 1e-6 * max(1.0, t_end):
            raise ValueError(f"thermal frame {i} time lies outside [0,t_end_s]")
        times.append(time_s)
        frame_fields.append(clean)
        frame_summaries.append({"t_s": time_s, "max_temp_c": round(max(clean), 3)})
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("thermal result frame times are not strictly ascending")
    if not math.isclose(times[-1], t_end, rel_tol=1e-6, abs_tol=1e-6):
        raise ValueError(
            f"thermal result ends at {times[-1]:g}s instead of requested t_end_s={t_end:g}"
        )

    summary_raw = res.get("summary")
    if not isinstance(summary_raw, dict):
        raise ValueError("thermal solver reported success without a summary")
    summary_keys = ("max_temp_c", "max_temp_time_s", "min_temp_c", "final_max_temp_c")
    summary = {
        key: _finite_float(summary_raw.get(key), f"thermal summary.{key}")
        for key in summary_keys
    }
    if summary["min_temp_c"] > summary["max_temp_c"]:
        raise ValueError("thermal summary minimum exceeds its maximum")
    time_tol = 1e-6 * max(1.0, t_end)
    if not -time_tol <= summary["max_temp_time_s"] <= t_end + time_tol:
        raise ValueError("thermal summary.max_temp_time_s lies outside [0,t_end_s]")

    envelope = res.get("envelope_max")
    if not isinstance(envelope, list) or len(envelope) != counts["n_nodes"]:
        got = len(envelope) if isinstance(envelope, list) else 0
        raise ValueError(
            f"thermal envelope_max contains {got}/{counts['n_nodes']} nodal values"
        )
    envelope = [
        _finite_float(value, f"thermal envelope_max[{i}]")
        for i, value in enumerate(envelope)
    ]
    tol = 1e-3 * max(1.0, abs(summary["max_temp_c"]))
    if abs(max(envelope) - summary["max_temp_c"]) > tol:
        raise ValueError("thermal summary.max_temp_c disagrees with envelope_max")
    if abs(max(frame_fields[-1]) - summary["final_max_temp_c"]) > tol:
        raise ValueError(
            "thermal summary.final_max_temp_c disagrees with the final frame"
        )
    return {
        "counts": counts,
        "frames": frames,
        "frame_summaries": frame_summaries,
        "summary": {**summary_raw, **summary},
        "envelope_max": envelope,
    }


# ------------------------------------------------------- atomised solve pipeline (async jobs)
# Every sim tool used to be one monolithic closure: resolve the doc, build the study, call the
# engine, persist, shape the reply. That shape can only ever run start-to-finish inside one MCP
# call, which is why a long solve could only be waited on. Splitting it into three atomic stages
#
#     SETUP   (parent) — needs the live document: export STEP, resolve face queries -> a study
#                        dict that is pure JSON and references only files on disk
#     SOLVE   (child)  — one engine RPC on that study; killable, restartable, turn-independent
#     COLLECT (parent) — persist the study record + shape the reply envelope
#
# is what lets sim_run/sim_status/sim_cancel/sim_results exist WITHOUT a second implementation
# of the physics: the synchronous tools call the SAME setup and the SAME collect, so an async
# answer is equal to the sync answer by construction, not by a promise in a docstring.


class _StudyRefusal(Exception):
    """A setup-time refusal carrying the EXACT reply envelope its tool already returned."""

    def __init__(self, payload: dict):
        super().__init__(payload.get("error", "refused"))
        self.payload = payload


# ------------------------------------------------------- the zero-input study (live fix)
# Measured 2026-07-30 driving the bracket flow ("will this hold 200 N?") end to end on an
# 80x40x8 plate, left face fixed:
#   run_fea_static(fixtures=["left"], loads=[])
#       -> ok:true, max_von_mises_mpa 0.0, max_disp_mm 0.0, min_fos 15.0
#   run_fea_static(fixtures=["left"], loads=[{"face":"right","force":[0,0,0]}])
#       -> byte-identical
#   run_fea_fatigue(fixtures=["left"], loads=[])
#       -> ok:true, regime "infinite", fos_endurance null   ("this part never fails")
# 15.0 is core/sim/fea_static.cpp's FoS saturation cap (yield/0 clamps to 15), so a study
# with NO LOAD reports the single most reassuring number the surface can produce. There is
# no body-load term anywhere in the study payload (no gravity / self-weight in
# fea_static.cpp), so a linear-static solve with no non-zero load is identically zero BY
# CONSTRUCTION: the reply is never an answer to the caller's question, only a well-formed
# lie about it. Refused at SETUP, before a mesh is built, so it costs one call and teaches.


def _load_is_driving(spec: dict) -> bool:
    """Does this solver load spec put any energy into the model?"""
    if spec.get("type") == "pressure":
        return abs(float(spec.get("magnitude") or 0.0)) > 0.0
    return any(abs(float(c)) > 0.0 for c in (spec.get("vector") or ()))


def _build_load_specs(d: str, loads: list, analysis: str) -> list:
    """Key-normalized `loads` -> the solver's load payload, refusing a study nothing drives.

    Shared by run_fea_static and run_fea_fatigue (whose loops were character-identical) so
    the zero-input guard cannot be added to one solve and forgotten in the other.
    """
    specs: list = []
    for ld in loads:
        if not isinstance(ld, dict) or "face" not in ld:
            raise ValueError(
                f"each load needs a 'face' query: {ld!r}. " + _BC_SHAPE_HELP
            )
        unsupported = set(ld) - {"face", "force", "pressure"} - _BC_ITEM_LABELS
        if unsupported:
            raise ValueError(
                f"{analysis} load has unsupported fields {sorted(unsupported)!r}; refusing "
                "to ignore them. Mechanical loads accept face + exactly one of force or "
                "pressure."
            )
        # Preserve each matched CAD face's complete geometric oracle under one
        # authored load; native union quadrature never multiplies its resultant.
        merged = _merge_face_sets(_sim_face_points(d, ld["face"]))
        merged["selector"] = _selector_text(ld["face"])
        spec: dict = {"faces": merged}
        if "pressure" in ld:
            spec["type"] = "pressure"
            spec["magnitude"] = _finite_float(
                ld["pressure"], f"{analysis} load pressure"
            )
        elif "force" in ld:
            spec["type"] = "force"
            spec["vector"] = _force_vector(ld["force"], f"{analysis} load")
        else:
            raise ValueError(
                f"load needs 'force':[x,y,z] or 'pressure':<MPa>: {ld!r}. "
                + _BC_SHAPE_HELP
            )
        specs.append(spec)
    if not any(_load_is_driving(s) for s in specs):
        raise _StudyRefusal(
            {
                "ok": False,
                "error": (
                    f"{analysis} has no non-zero load, so the solve is identically zero — every "
                    "stress, displacement and factor of safety would come back 0 / saturated, "
                    "which reads as 'infinitely safe' rather than 'never asked'. Supply at least "
                    'one load with a non-zero magnitude, e.g. loads=[{"face":"top","force":'
                    '[0,0,-200]}] (newtons, world XYZ) or [{"face":"top","pressure":0.5}] (MPa). '
                    "This study has no self-weight/gravity term, so an unloaded part carries "
                    "nothing at all."
                ),
            }
        )
    return specs


def _reject_fully_constrained_loads(
    fixture_sets: list, load_specs: list, analysis: str
) -> None:
    """A load entirely on fixed sample points becomes a reaction-only zero field."""
    def sample_points(faceset):
        for leaf in faceset.get("facesets", [faceset]):
            for point in (leaf.get("points") or []):
                if isinstance(point, (list, tuple)) and len(point) == 3:
                    yield tuple(float(c) for c in point)

    fixed = {point for fs in fixture_sets for point in sample_points(fs)}
    for i, spec in enumerate(load_specs):
        loaded = set(sample_points(spec.get("faces", {})))
        if loaded and loaded.issubset(fixed):
            raise _StudyRefusal(
                {
                    "ok": False,
                    "error": (
                        f"{analysis} load[{i}] is applied entirely on a fully fixed face. Its force "
                        "would be absorbed as a support reaction while the solved displacement/stress "
                        "field stays zero. Apply the load to a distinct free face, or change the "
                        "fixture to match the real support."
                    ),
                }
            )


def _norm_vtu_path(vtu_path) -> Optional[str]:
    """Validate an optional VTU output request before any solve starts.

    The engine creates the parent directories and fails the whole run on a write error
    rather than reporting a partial success. The engine process has its own cwd, so a
    relative path is resolved here against the project root (`resolve_agent_path`, the
    same rule as save_part); with no project root it is refused. The reply then names
    the absolute file that the engine writes.

    Pure: an existing file at that path is protected by `_archive_existing_vtu()`, which
    a caller runs only once the whole setup has passed validation.
    """
    return _norm_abs_out_path(vtu_path, "vtu_path")


def _archive_existing_vtu(vtu_path: Optional[str], notes: Optional[list] = None) -> None:
    """Move any .vtu already at *vtu_path* aside instead of letting the solve truncate it.

    That file is the FIELD an earlier released number came from, and the engine's writer
    would simply overwrite it. It is renamed into `superseded_<YYYY-MM-DD>_<short-hash>/`
    beside itself (free even for a 27 MB file) and the run SAYS so through `notes`. There
    is no in-place option: nothing here can re-derive the old field, so replacing it is a
    deliberate `rm` by the caller.

    Call this only after every setup validation has passed — a run about to be refused
    must not move a file. `_build_static_study` does it beside the STEP export, for the
    same reason.
    """
    archived = archive_before_write([vtu_path]) if vtu_path else None
    if archived and isinstance(notes, list):
        notes.append(
            f"the .vtu already at {vtu_path} was NOT overwritten: it was archived to "
            f"{archived['archived_to']} (sha256 {(archived['sha256'] or '')[:12]}) "
            "before this solve started."
        )


def _norm_abs_out_path(path, arg_name: str) -> Optional[str]:
    """The absolute form of an optional solver output path (relative = project-root)."""
    if path is None:
        return None
    return resolve_agent_path(path, arg_name)


# The `mesh_fineness` default every static-shaped entry point declares. An explicit
# element size and a fineness dial are two answers to the same question, and only one of
# them can be honoured, so the clash has to be detectable — which needs the default to be
# a named constant rather than a literal repeated in five signatures.
_DEFAULT_FINENESS = 0.5


def _norm_mesh_size_mm(mesh_size_mm, mesh_fineness: float) -> Optional[float]:
    """Validate an EXPLICIT characteristic element size (mm) for a static study.

    `mesh_fineness` is a 0..1 dial the mesh-size governor reads as a suggestion; a size in
    mm maps straight to `study.target_size_mm` and is the release-grade spelling (it is
    what every `run_fea_convergence` leg sets, and the only way to RE-SOLVE a released
    ladder leg at the exact size that produced the number being released).

    They are mutually exclusive: a caller who states both has stated two different meshes,
    and silently preferring one would attribute a field to a mesh nobody asked for — the
    exact confusion this argument exists to end. `mesh_fineness` left at its default is
    not a statement, so `mesh_size_mm` alone is never a clash.
    """
    if mesh_size_mm is None:
        return None
    size = _finite_float(mesh_size_mm, "mesh_size_mm")
    if size <= 0.0:
        raise ValueError(
            "mesh_size_mm must be finite and > 0 when given: it is the characteristic "
            "element size in millimetres, e.g. 6.0."
        )
    if mesh_fineness != _DEFAULT_FINENESS:
        raise ValueError(
            f"pass mesh_size_mm ({size:g} mm, an exact element size that maps to "
            f"study.target_size_mm) OR mesh_fineness ({mesh_fineness}, the 0..1 dial the "
            "mesh-size governor clamps against an element budget) — not both. They are "
            "two different meshes, and a result has to name the one it was solved on. "
            "Drop mesh_fineness to solve at the stated size."
        )
    return size


def _norm_min_size_mm(
    min_size_mm, size_mm: Optional[float], size_arg: str = "mesh_size_mm"
) -> Optional[float]:
    """Validate an EXPLICIT size-band FLOOR (mm) — `study.min_size_mm`.

    The mesher answers a feature it cannot resolve by descending polymesh's own
    resolution hints, and that descent stops at the study's element-size band floor
    (core/sim/mesher.cpp, `loaded.band.min_len`), which defaults to HALF the base size
    (core/sim/mesh_size_band.cpp). A feature needing less than that half-size is refused
    with "polymesh needs an element size of X mm ... below this study's element-size band
    floor of Y mm — raise fineness or set min_size_mm to mesh it". This argument is that
    floor, stated: it lowers it so the descent can reach X.

    It may not exceed the stated element size. `min_size_mm > mesh_size_mm` would raise
    the base size itself (mesh_size_band.cpp clamps base up to the floor), so it would
    silently coarsen the mesh the caller just named instead of letting the mesher refine
    below it.
    """
    if min_size_mm is None:
        return None
    floor = _finite_float(min_size_mm, "min_size_mm")
    if floor <= 0.0:
        raise ValueError(
            "min_size_mm must be finite and > 0 when given: it is the smallest element "
            "size in millimetres the mesher may descend to, e.g. 0.5."
        )
    if size_mm is not None and floor > size_mm:
        raise ValueError(
            f"min_size_mm ({floor:g} mm) cannot exceed {size_arg} ({size_mm:g} mm): the "
            "floor would raise the base element size instead of letting the mesher "
            "refine below it, coarsening the very mesh you stated. Pass a floor under "
            "the size polymesh asked for, e.g. min_size_mm=0.5 for a 0.78 mm feature."
        )
    return floor


def _norm_plasticity(plasticity, mat: dict) -> Optional[dict]:
    """Validate and canonicalize the `plasticity=` argument of run_fea_static/contact.

    None / False / {} means "elastic", which is the default every existing caller gets.
    Anything else must be a dict of {yield_mpa, hardening_mpa, max_increments, tol};
    yield_mpa defaults to the resolved material's own yield strength, which is the
    number summary.min_fos is already measured against, so the two cannot disagree.
    """
    if plasticity is None or plasticity is False:
        return None
    raw = coerce_json_or_repr(plasticity)
    if raw is None or raw is False:
        return None
    if raw is True:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(
            "plasticity must be a dict, e.g. "
            '{"yield_mpa": 276, "hardening_mpa": 1200, "max_increments": 10}; '
            "omit it (or pass None) for the linear-elastic material."
        )
    canon_by_norm = {
        _norm_key(k): k
        for k in ("yield_mpa", "hardening_mpa", "max_increments", "tol")
    }
    unknown = sorted(k for k in raw if _norm_key(k) not in canon_by_norm)
    if unknown:
        raise ValueError(
            f"plasticity has unsupported keys {unknown!r}; refusing to ignore them. "
            "Supported: yield_mpa (MPa, defaults to material.yield_mpa), "
            "hardening_mpa (post-yield TANGENT MODULUS in MPa, 0 = perfectly "
            "plastic), max_increments (load steps, default 10), tol (relative "
            "residual, default 1e-3)."
        )
    canon = {canon_by_norm[_norm_key(k)]: v for k, v in raw.items()}
    out: dict = {"enabled": True}
    if "per_body" in mat:
        mat = mat.get("default") or next(iter(mat["per_body"].values()))
    yield_mpa = canon.get("yield_mpa", mat.get("yield_mpa"))
    out["yield_mpa"] = _finite_float(yield_mpa, "plasticity.yield_mpa")
    if out["yield_mpa"] <= 0.0:
        raise ValueError("plasticity.yield_mpa must be > 0")
    hardening = _finite_float(
        canon.get("hardening_mpa", 0.0), "plasticity.hardening_mpa"
    )
    if hardening < 0.0:
        raise ValueError(
            "plasticity.hardening_mpa is a post-yield TANGENT MODULUS in MPa and must "
            "be >= 0; 0 means perfectly plastic (a flat yield plateau). It is not a "
            "ratio of E — for structural steel it is about 1000-2000 MPa."
        )
    out["hardening_mpa"] = hardening
    if "max_increments" in canon:
        inc = canon["max_increments"]
        if isinstance(inc, bool) or not isinstance(inc, int):
            raise ValueError(
                "plasticity.max_increments must be an integer from 1 through 1000"
            )
        if not 1 <= inc <= 1000:
            raise ValueError(
                "plasticity.max_increments must be an integer from 1 through 1000"
            )
        out["max_increments"] = inc
    if "tol" in canon:
        tol = _finite_float(canon["tol"], "plasticity.tol")
        if not 0.0 < tol < 1.0:
            raise ValueError(
                "plasticity.tol is a RELATIVE residual tolerance "
                "(||out-of-balance force|| / ||applied load||) and must be in (0, 1)"
            )
        out["tol"] = tol
    return out


def _resolve_study_interfaces(interfaces: dict, bodies: list) -> dict:
    """Resolve validated body names using the exporter-authoritative solid ordinals."""
    if not any(
        isinstance(body, str)
        for kind in ("bonded", "contact")
        for pair in interfaces.get(kind, [])
        for body in pair
    ):
        return interfaces
    by_name: dict = {}
    for body in bodies:
        name = body.get("name")
        if isinstance(name, str) and name:
            by_name.setdefault(name, []).append(body)

    def ordinal(ref) -> int:
        if isinstance(ref, int):
            return ref
        matches = by_name.get(ref, [])
        if not matches:
            raise ValueError(f"interfaces references unknown body name {ref!r}")
        if len(matches) != 1:
            raise ValueError(
                f"interfaces body name {ref!r} is ambiguous; use 1-based STEP solid ordinals"
            )
        values = matches[0].get("source_ordinals")
        if not isinstance(values, list) or not values or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in values
        ):
            raise ValueError(
                f"interfaces body name {ref!r} has no authoritative STEP solid ordinals; "
                "use 1-based STEP solid ordinals or rebuild the engine"
            )
        if len(values) != 1:
            raise ValueError(
                f"interfaces body name {ref!r} spans multiple STEP solids {values!r}; "
                "use explicit 1-based STEP solid ordinals to select the intended pair"
            )
        return values[0]

    resolved = dict(interfaces)
    for kind in ("bonded", "contact"):
        if kind not in interfaces:
            continue
        resolved[kind] = [[ordinal(a), ordinal(b)] for a, b in interfaces[kind]]
    return resolved


def _build_static_study(
    fixtures,
    loads,
    material,
    mesh_fineness,
    learned_advisor,
    advisor_max_dof,
    doc_id,
    notes: list,
    vtu_path: Optional[str] = None,
    export_system_path: Optional[str] = None,
    target_size_mm: Optional[float] = None,
    min_size_mm: Optional[float] = None,
    plasticity: Optional[dict] = None,
    material_analysis: str = "static",
    interfaces: Optional[dict] = None,
    bond_gap_mm: Optional[float] = None,
) -> dict:
    """SETUP stage for run_fea_static / sim_run(analysis="fea_static")."""
    if interfaces is not None:
        simjobs.validate_interfaces(interfaces)
    if bond_gap_mm is not None:
        bond_gap_mm = _finite_float(bond_gap_mm, "bond_gap_mm")
        if bond_gap_mm < 0.0:
            raise ValueError("bond_gap_mm must be finite and >= 0 when given")
    d = _doc(doc_id)
    bodies = _require_sim_geometry(d)
    if interfaces is not None:
        interfaces = _resolve_study_interfaces(interfaces, bodies)
    fixtures = require_list(
        fixtures,
        "fixtures",
        'a list of face queries, e.g. ["bottom"] or '
        '[{"of":"face","side":"bottom","pick":"largest"}]',
    )
    loads = require_list(
        loads,
        "loads",
        "a list of {face query + force/pressure}, e.g. "
        '[{"face":"top","force":[0,0,-500]}]',
    )
    fixtures = _norm_bc_list(fixtures, "fixtures", unwrap_query=True, notes=notes)
    loads = _norm_bc_list(loads, "loads", notes=notes)
    mesh_fineness = _norm_fineness(mesh_fineness)
    target_size_mm = _norm_mesh_size_mm(target_size_mm, mesh_fineness)
    min_size_mm = _norm_min_size_mm(min_size_mm, target_size_mm)
    vtu_path = _norm_vtu_path(vtu_path)
    export_system_path = _norm_abs_out_path(export_system_path, "export_system_path")
    mat = _resolve_study_material(material, material_analysis)
    plasticity = _norm_plasticity(plasticity, mat)
    if plasticity is not None:
        mat = {**mat, "plasticity": plasticity}
    advisor = _advisor_params(learned_advisor, advisor_max_dof)
    # fixtures=[] and "fixtures that resolved to nothing" are DIFFERENT mistakes, and the
    # single old message diagnosed the wrong one — it told a caller who supplied no queries
    # at all to go check its queries with query_geometry(), buying a wasted retry.
    if not fixtures:
        raise _StudyRefusal(
            {
                "ok": False,
                "error": "static FEA needs at least one fixed face — you passed "
                "fixtures=[]. An unconstrained part under load has nothing to "
                "react against (rigid-body motion), so there is no static "
                "solution to report. Pass the mounted face, e.g. fixtures="
                '["bottom"]. fixtures=[] is only meaningful for run_fea_modal, '
                "where it means a free-free modal solve.",
            }
        )
    fixture_sets: list = []
    for q in fixtures:
        # One query can match several faces; each resolved faceset keeps the QUERY as
        # its label so an engine refusal names what the caller wrote, not an ordinal
        # into the expanded list.
        label = _selector_text(q)
        for fs in _sim_face_points(d, q):
            if isinstance(fs, dict):
                fs["selector"] = label
            fixture_sets.append(fs)
    if not fixture_sets:
        raise _StudyRefusal(
            {
                "ok": False,
                "error": "no fixture faces resolved — the face queries you provided "
                "didn't match any surface points. Static FEA requires at least "
                "one fixed face. Use query_geometry() to confirm the face query.",
            }
        )

    load_specs = _build_load_specs(d, loads, "static FEA")
    _reject_fully_constrained_loads(fixture_sets, load_specs, "static FEA")
    # Export only after all setup validation and face resolution succeeds.
    step = _tmp(".step")
    cb.call("doc_export_step", {"doc_id": d, "path": step})
    # Same rule as the export above: the previous .vtu is moved aside only once the
    # setup is known good.
    _archive_existing_vtu(vtu_path, notes)

    study = {
        "step_path": step,
        "material": mat,
        "fixtures": fixture_sets,
        "loads": load_specs,
        "mesh_order": 2,
        "fineness": mesh_fineness,
    }
    if interfaces is not None:
        study["interfaces"] = interfaces
    if bond_gap_mm is not None:
        study["bond_gap_mm"] = bond_gap_mm
    if target_size_mm is not None:
        # Exactly what a convergence leg sets, so a leg can be re-solved at its own size:
        # `fineness` stays in the payload (the engine reads it only when no explicit size
        # is present) and target_size_mm bypasses the mesh-size governor's band.
        study["target_size_mm"] = target_size_mm
    if min_size_mm is not None:
        # The size-band FLOOR, beside the base size it belongs to: it is what bounds the
        # mesher's feature-resolution descent, so a feature below base/2 is reachable
        # only when this is stated (core/sim/mesher.cpp band.min_len).
        study["min_size_mm"] = min_size_mm
    if vtu_path:
        study["vtu_path"] = vtu_path
    if export_system_path:
        study["export_system_path"] = export_system_path
    study.update(advisor)
    return {
        "analysis": "fea_static",
        "doc": d,
        "study": study,
        "material": mat,
        "material_arg": material,
        "fixtures": fixtures,
        "loads": loads,
        "mesh_fineness": mesh_fineness,
        "target_size_mm": target_size_mm,
        "min_size_mm": min_size_mm,
        "vtu_path": vtu_path,
        "export_system_path": export_system_path,
        **advisor,
        "step_path": step,
    }


def _build_adaptive_study(
    fixtures,
    loads,
    material,
    mesh_fineness,
    max_passes,
    eta_target,
    learned_advisor,
    advisor_max_dof,
    doc_id,
    notes: list,
    vtu_path: Optional[str] = None,
) -> dict:
    """SETUP stage for run_fea_adaptive / sim_run_adaptive."""
    if isinstance(max_passes, bool) or not isinstance(max_passes, int):
        raise ValueError("max_passes must be an integer from 1 through 6")
    if not 1 <= max_passes <= 6:
        raise ValueError("max_passes must be from 1 through 6")
    eta_target = _finite_float(eta_target, "eta_target")
    if eta_target <= 0.0:
        raise ValueError("eta_target must be finite and greater than 0")
    ctx = _build_static_study(
        fixtures,
        loads,
        material,
        mesh_fineness,
        learned_advisor,
        advisor_max_dof,
        doc_id,
        notes,
        vtu_path=vtu_path,
    )
    ctx["study"]["adapt"] = {"max_passes": max_passes, "eta_target": eta_target}
    # _build_static_study supplies its own analysis tag. Override it before _job_meta()
    # serializes this context, or durable collection dispatches the adaptive result through
    # the ordinary static collector after both a synchronous wait and an MCP reconnect.
    ctx["analysis"] = "fea_adaptive"
    ctx["max_passes"] = max_passes
    ctx["eta_target"] = eta_target
    return ctx


def _build_contact_study(
    fixtures,
    load,
    material,
    max_gap_mm,
    max_iterations,
    mesh_size_mm,
    mesh_order,
    doc_id,
    notes: list,
    vtu_path: Optional[str] = None,
    interface_size_mm: Optional[float] = None,
    min_size_mm: Optional[float] = None,
    plasticity: Optional[dict] = None,
    interfaces: Optional[dict] = None,
    penalty_scale: float = 1.0,
    bond_gap_mm: Optional[float] = None,
    closed_fit: bool = True,
    closed_fit_gap_mm: Optional[float] = None,
) -> dict:
    """SETUP stage for run_fea_contact.

    Same resolution as a static study — the fixture/load face queries, the STEP export,
    the fully-constrained-load refusal — plus `study.contact`, which switches the engine
    onto the unilateral active-set solve over the mesh's own body interfaces.
    """
    if (
        bond_gap_mm is not None
        and interfaces is None
    ):
        raise ValueError(
            "bond_gap_mm with contact requires interfaces to distinguish bonded "
            "and contact body pairs"
        )
    if not isinstance(closed_fit, bool):
        raise ValueError("closed_fit must be boolean")
    if closed_fit_gap_mm is not None:
        closed_fit_gap_mm = _finite_float(closed_fit_gap_mm, "closed_fit_gap_mm")
        if closed_fit_gap_mm < 0:
            raise ValueError("closed_fit_gap_mm must be non-negative")
    penalty_scale = _finite_float(penalty_scale, "penalty_scale")
    if penalty_scale <= 0.0:
        raise ValueError("penalty_scale must be finite and > 0")
    max_gap_mm = _finite_float(max_gap_mm, "max_gap_mm")
    if max_gap_mm <= 0.0:
        raise ValueError(
            "max_gap_mm must be > 0: it is the widest mesh gap still treated as a "
            "contact candidate. Measure the real clearance (check_interference / "
            "measure) and pass just above it, e.g. 0.2 for a 0.1 mm slip fit."
        )
    max_iterations = _positive_int(max_iterations, "max_iterations")
    if max_iterations > 500:
        raise ValueError("max_iterations must be from 1 through 500")
    if isinstance(mesh_order, bool) or mesh_order not in (1, 2):
        raise ValueError("mesh_order must be 1 (tet4) or 2 (tet10).")
    if mesh_size_mm is not None:
        mesh_size_mm = _finite_float(mesh_size_mm, "mesh_size_mm")
        if mesh_size_mm <= 0.0:
            raise ValueError("mesh_size_mm must be finite and > 0 when given")
    min_size_mm = _norm_min_size_mm(min_size_mm, mesh_size_mm)
    if interface_size_mm is not None:
        interface_size_mm = _finite_float(interface_size_mm, "interface_size_mm")
        if interface_size_mm <= 0.0:
            raise ValueError(
                "interface_size_mm must be finite and > 0 when given: it is the element "
                "size the CONTACT FACES are refined to (locally), so pass a size that "
                "resolves the measured clearance — about a quarter of it."
            )
    ctx = _build_static_study(
        fixtures,
        require_list(
            load,
            "load",
            "a face query plus a force or pressure, e.g. "
            '{"face":"top","force":[0,0,-500]}',
        ),
        material,
        0.5,
        False,
        0,
        doc_id,
        notes,
        vtu_path=vtu_path,
        plasticity=plasticity,
        interfaces=interfaces,
        bond_gap_mm=bond_gap_mm,
    )
    ctx["study"]["mesh_order"] = mesh_order
    if mesh_size_mm is not None:
        ctx["study"]["target_size_mm"] = mesh_size_mm
    if min_size_mm is not None:
        ctx["study"]["min_size_mm"] = min_size_mm
    ctx["min_size_mm"] = min_size_mm
    ctx["study"]["contact"] = {
        "enabled": True,
        "max_gap_mm": max_gap_mm,
        "max_iterations": max_iterations,
        "penalty_scale": penalty_scale,
        "closed_fit": closed_fit,
    }
    if closed_fit_gap_mm is not None:
        ctx["study"]["contact"]["closed_fit_gap_mm"] = closed_fit_gap_mm
    if interface_size_mm is not None:
        ctx["study"]["contact"]["interface_size_mm"] = interface_size_mm
    ctx["interface_size_mm"] = interface_size_mm
    ctx["plasticity"] = ctx["study"]["material"].get("plasticity")
    # A face filter wider than the clearance is a defect the caller can fix from the
    # reply, and the comparison needs neither a mesh nor a solve — so it is said now,
    # not after minutes of meshing and an engine refusal.
    _warn_bc_filters_across_clearance(ctx["study"], max_gap_mm, notes)
    # _build_static_study tags itself "fea_static"; the durable collector dispatches on
    # this tag, so leaving it would collect a contact run through the static collector
    # and drop the contact verdict.
    ctx["analysis"] = "fea_contact"
    ctx["max_gap_mm"] = max_gap_mm
    ctx["max_iterations"] = max_iterations
    ctx["penalty_scale"] = penalty_scale
    ctx["mesh_size_mm"] = mesh_size_mm
    ctx["mesh_order"] = mesh_order
    return ctx


def _collect_adaptive(ctx: dict, res: dict, notes: list) -> dict:
    """Collect adaptive FEA without disguising an under-resolved pass as static success."""
    if not res.get("ok"):
        return _collect_static(ctx, res, notes)
    adapt = res.get("adapt")
    if not isinstance(adapt, dict):
        return {
            "ok": False,
            "doc_id": ctx["doc"],
            "error": "adaptive solver reported success without convergence metadata.",
        }
    try:
        global_eta = _finite_float(adapt.get("global_eta"), "adapt.global_eta")
        eta_target = _finite_float(adapt.get("eta_target"), "adapt.eta_target")
        passes_run = _positive_int(adapt.get("passes_run"), "adapt.passes_run")
    except ValueError as exc:
        return {
            "ok": False,
            "doc_id": ctx["doc"],
            "error": f"adaptive solver returned incomplete convergence metadata: {exc}",
        }
    passes = adapt.get("passes")
    eta_target_met = adapt.get("eta_target_met")
    stop_reason = adapt.get("stop_reason")
    if global_eta < 0.0 or eta_target <= 0.0:
        return {
            "ok": False,
            "doc_id": ctx["doc"],
            "error": "adaptive solver returned invalid global_eta or eta_target.",
        }
    if (
        not isinstance(passes, list)
        or len(passes) != passes_run
        or not all(isinstance(item, dict) for item in passes)
    ):
        return {
            "ok": False,
            "doc_id": ctx["doc"],
            "error": "adaptive solver returned missing or inconsistent pass metadata.",
        }
    if (
        not isinstance(eta_target_met, bool)
        or not isinstance(stop_reason, str)
        or not stop_reason
    ):
        return {
            "ok": False,
            "doc_id": ctx["doc"],
            "error": "adaptive solver returned invalid eta_target_met or stop_reason.",
        }

    if eta_target_met:
        resolution_note = (
            "The adaptive error target was met; the reported field is from the final "
            "refined mesh."
        )
    else:
        resolution_note = (
            "The adaptive error target was not met; this result is under-resolved and is "
            "retained for diagnosis, not presented as a converged static result."
        )
    out = _collect_static(
        ctx,
        res,
        notes,
        study_type="fea_adaptive",
        study_name="Adaptive static stress",
        study_extra={
            "adapt": adapt,
            "converged": eta_target_met,
            "resolution_note": resolution_note,
        },
    )
    static_ok = bool(out.get("ok"))
    out.update(
        {
            "global_eta": global_eta,
            "eta_target": eta_target,
            "eta_target_met": eta_target_met,
            "passes_run": passes_run,
            "passes": passes,
            "stop_reason": stop_reason,
            "adapt": adapt,
            "converged": static_ok and eta_target_met,
            "resolution_note": resolution_note,
        }
    )
    if not static_ok:
        return out
    if not eta_target_met:
        out["ok"] = False
        out["error"] = (
            f"adaptive solve stopped without meeting eta_target={eta_target:g} "
            f"(global_eta={global_eta:g}, stop_reason={stop_reason}); the final-pass "
            "diagnostics are retained below but are not a converged result."
        )
    return out


def _build_modal_study(
    fixtures,
    material,
    mesh_fineness,
    learned_advisor,
    advisor_max_dof,
    doc_id,
    notes: list,
    vtu_path: Optional[str] = None,
) -> dict:
    """SETUP stage for run_fea_modal / sim_run(analysis="fea_modal")."""
    d = _doc(doc_id)
    _require_sim_geometry(d)
    fixtures = require_list(
        fixtures, "fixtures", 'a list of face queries held fixed, e.g. ["bottom"]'
    )
    fixtures = _norm_bc_list(fixtures, "fixtures", unwrap_query=True, notes=notes)
    mesh_fineness = _norm_fineness(mesh_fineness)
    vtu_path = _norm_vtu_path(vtu_path)
    mat = _resolve_study_material(material, "modal")
    advisor = _advisor_params(learned_advisor, advisor_max_dof)
    fixture_sets: list = []
    for q in fixtures:
        fixture_sets.extend(_sim_face_points(d, q))
    # F6 (free-free modal): when fixtures=[] the caller is requesting an unconstrained
    # solve — the C++ fea_modal_solve handles empty fixed_nodes by computing the full
    # 6 rigid-body modes near 0 Hz followed by the elastic modes. Only reject when the
    # caller DID supply fixture queries but NONE of them resolved to real face points
    # (which is almost certainly a geometry-query mistake, not intentional free-free).
    if fixtures and not fixture_sets:
        raise _StudyRefusal(
            {
                "ok": False,
                "error": "no fixture faces resolved — "
                "the face queries you provided didn't match any surface points. "
                "Use query_geometry() to find the right face query, or pass "
                "fixtures=[] for a free-free (unconstrained) solve.",
            }
        )
    # Export only after all supplied fixtures have resolved.
    step = _tmp(".step")
    cb.call("doc_export_step", {"doc_id": d, "path": step})
    _archive_existing_vtu(vtu_path, notes)
    # Modal bending accuracy requires the same quadratic discretization as the
    # engine's direct STEP-backed modal path. A first-order graded-tet mesh
    # over-stiffens the first free-free elastic mode by ~40%.
    study = {
        "step_path": step,
        "material": mat,
        "fixtures": fixture_sets,
        "mesh_order": 2,
        "fineness": mesh_fineness,
    }
    if vtu_path:
        study["vtu_path"] = vtu_path
    study.update(advisor)
    return {
        "analysis": "fea_modal",
        "doc": d,
        "study": study,
        "material": mat,
        "material_arg": material,
        "fixtures": fixtures,
        "fixture_sets": fixture_sets,
        "free_free": not fixture_sets,
        "mesh_fineness": mesh_fineness,
        "vtu_path": vtu_path,
        **advisor,
        "step_path": step,
    }


# core/sim/fea_static.cpp clamps yield/stress into [0, 15]; 15 therefore means "saturated",
# not "15x margin", and an unloaded study used to report exactly that (see _build_load_specs).
_FOS_CAP = 15.0


# The release-evidence fields the engine attaches BESIDE `summary` (never inside it):
#   mesh_quality  aspect/jacobian extremes, pct_poor, min tet volume, node/elem counts,
#                 order, char_len_mm (the DELIVERED element size in mm — the mesh-size
#                 governor sizes against an element budget, so it is routinely not the
#                 requested target_size_mm; null when the route meshed nothing itself),
#                 connected_components / unfixed_components (the mechanism gate)
#                 and thin_wall_ratio (wall thickness in element sizes)
#   reactions     applied vs reaction force/moment plus equilibrium_error_pct (static family
#                 only — a modal or thermal solve has no applied load to balance)
#   plausibility  max_disp_over_bbox_diag + flag: an "implausible" flag means the peak
#                 displacement exceeded the whole part, so the small-strain assumption the
#                 solve is built on is broken and the number is not a deflection
#   warnings      the engine's own top-level warning strings (displacement_implausible, …)
#   provenance    engine_build_id / polymesh_commit / solver / mesher / timestamp_utc
#   vtu_path      the .vtu the solve wrote, present only when the study requested one
# They are forwarded VERBATIM into the reply and into the persisted study: an evidence packet
# must show what the solver reported, not a value this file re-derived. A key the engine did
# not send stays ABSENT rather than becoming a null the reader has to interpret.
#   contact       the unilateral-contact block of a run_fea_contact solve: candidate vs
#                 active pair counts, iterations, converged, per-body-pair resultant and
#                 peak pressure, held_by_contact_only, and the tangential-stabilizer share
_EVIDENCE_KEYS = (
    "mesh_quality",
    "reactions",
    "plausibility",
    "warnings",
    "provenance",
    "vtu_path",
    "contact",
    "plasticity",
    "material",
)


def _evidence_fields(res: dict) -> dict:
    """The engine's mesh-quality / equilibrium / provenance / VTU record, as sent."""
    if not isinstance(res, dict):
        return {}
    return {k: res[k] for k in _EVIDENCE_KEYS if res.get(k) is not None}


def _collect_static(
    ctx: dict,
    res: dict,
    notes: list,
    *,
    study_type: str = "fea_static",
    study_name: str = "Static stress",
    study_extra: Optional[dict] = None,
) -> dict:
    """COLLECT stage for a static-shaped solve — the one reply-envelope implementation."""
    d = ctx["doc"]
    mat = ctx.get("material") or {}
    if not res.get("ok"):
        return {"ok": False, "doc_id": d, "error": res.get("error", "solve failed")}
    summary = res.get("summary")
    if not isinstance(summary, dict):
        return {
            "ok": False,
            "doc_id": d,
            "error": "static solver reported success without a result summary.",
        }
    needed = ("max_von_mises_mpa", "max_disp_mm", "min_fos")
    try:
        values = {k: _finite_float(summary.get(k), f"summary.{k}") for k in needed}
        counts = {
            k: _positive_int(res.get(k), k) for k in ("n_dof", "n_elems", "n_nodes")
        }
    except ValueError as exc:
        return {
            "ok": False,
            "doc_id": d,
            "error": f"static solver returned an incomplete/non-finite result: {exc}",
        }
    if any(values[k] < 0.0 for k in needed):
        return {
            "ok": False,
            "doc_id": d,
            "error": "static solver returned a negative stress, displacement, or factor "
            "of safety; the result is physically invalid.",
        }
    if values["max_von_mises_mpa"] == 0.0 and values["max_disp_mm"] == 0.0:
        return {
            "ok": False,
            "doc_id": d,
            "error": "a non-zero load produced an identically zero stress/displacement "
            "field; refusing false success. Confirm the load is not entirely on "
            "fixed nodes and that its face resolves onto the mesh.",
        }
    if counts["n_dof"] < 3 * counts["n_nodes"]:
        return {
            "ok": False,
            "doc_id": d,
            "error": "static solver returned an inconsistent mesh envelope: n_dof is "
            "smaller than three translational DOFs per node.",
        }
    summary = {**summary, **values}
    advisor = res.get("advisor")
    advisor_record = {"advisor": advisor} if isinstance(advisor, dict) else {}
    evidence = _evidence_fields(res)
    # Persist setup+results into the doc so they save inside the .step+.
    study_record = {
        "type": study_type,
        "name": f"{study_name} ({mat.get('name', ctx.get('material_arg'))})",
        "engine": "cpp",
        "material": mat,
        "fixtures": ctx.get("fixtures"),
        "loads": ctx.get("loads"),
        "mesh_fineness": ctx.get("mesh_fineness"),
        # Absent unless the caller stated an exact size: a persisted study whose
        # mesh_fineness is the untouched default must not read as "meshed at 0.5" when
        # target_size_mm is what the mesher actually honoured.
        **({"target_size_mm": ctx["target_size_mm"]}
           if ctx.get("target_size_mm") is not None else {}),
        # Same rule for the band floor: it is only in the record when it was stated, and
        # it is part of the discretization the numbers above belong to.
        **({"min_size_mm": ctx["min_size_mm"]}
           if ctx.get("min_size_mm") is not None else {}),
        "learned_advisor": ctx.get("learned_advisor", False),
        "advisor_max_dof": ctx.get("advisor_max_dof", 0),
        "results": summary,
        "n_dof": counts["n_dof"],
        "n_elems": counts["n_elems"],
        "n_nodes": counts["n_nodes"],
        "mesh": res.get("mesh_type_used"),
        "solver_notes": res.get("solver_notes", []),
        **advisor_record,
        **evidence,
    }
    if study_extra:
        study_record.update(study_extra)
    sid = _persist_study(d, study_record)
    # The engine's sim_run_static reply also carries n_elems / n_nodes / mesh_type_used /
    # solver_notes; only n_dof was forwarded, so a caller could not see the mesh it just
    # solved on (no way to judge whether a refinement is needed) and any solver WARNING
    # was dropped on the floor. Measured 2026-07-30: engine keys n_elems/n_nodes present,
    # reply had neither.
    out = {
        "ok": True,
        "doc_id": d,
        "material": mat.get("name", ctx.get("material_arg")),
        "summary": summary,
        "n_dof": counts["n_dof"],
        "n_elems": counts["n_elems"],
        "n_nodes": counts["n_nodes"],
        "mesh": res.get("mesh_type_used"),
        "saved_to_study": bool(sid),
        **evidence,
    }
    if res.get("solver_notes"):
        out["solver_notes"] = res["solver_notes"]
    if advisor_record:
        out.update(advisor_record)
    # min_fos saturates at 15 (core/sim/fea_static.cpp clamp_fos) — say so, otherwise a cap
    # is read as a computed margin.
    if values["min_fos"] >= _FOS_CAP:
        out["fos_note"] = (
            f"min_fos is reported at the solver's saturation cap ({_FOS_CAP}); "
            "the true margin is at least that, not exactly that."
        )
    if not sid:
        # _persist_study is best-effort and swallows its own errors; reporting a flat True
        # would be a verdict we never checked (and after a background job collected in a
        # LATER agent turn the document is legitimately no longer open here).
        out["study_not_saved_reason"] = (
            f"the results could not be written into document {d} (it is not open in this "
            "process). The numbers above are unaffected."
        )
    return _interpreted(out, notes)


def _collect_contact(ctx: dict, res: dict, notes: list) -> dict:
    """COLLECT stage for a unilateral-contact solve.

    The reply envelope is the static one (summary + mesh_quality + reactions +
    provenance + vtu_path), and `contact` rides beside it through _EVIDENCE_KEYS. What
    this adds is the refusal a static collector cannot make: a contact run whose reply
    carries no contact block was not a contact solve, whatever else it computed.
    """
    if res.get("ok"):
        contact = res.get("contact")
        if not isinstance(contact, dict) or contact.get("candidate_pairs") is None:
            return {
                "ok": False,
                "doc_id": ctx.get("doc"),
                "error": "the solver reported success without a contact block, so there "
                "is no evidence any interface was evaluated. Refusing to present it "
                "as a contact result.",
            }
    out = _collect_static(
        ctx, res, notes, study_type="fea_contact", study_name="Contact"
    )
    if not out.get("ok"):
        # Forward the engine's contact evidence onto the refusal as well.
        if isinstance(res.get("contact"), dict):
            out["contact"] = res["contact"]
        for key in ("provenance", "mesh_quality", "n_elems", "n_nodes", "elapsed_s"):
            if key in res:
                out[key] = res[key]
        # A plastic collapse refusal is only actionable with the load fraction it
        # reached, so the plasticity block rides on the refusal too.
        if isinstance(res.get("plasticity"), dict):
            out["plasticity"] = res["plasticity"]
        return out
    contact = out.get("contact") or {}
    active = contact.get("active_pairs")
    if active == 0:
        out["contact_note"] = (
            "NO contact pair carried load: every candidate separated under this load, so "
            "these numbers are the two bodies solved independently, not a contact result. "
            "Check the load direction (a unilateral contact carries compression only) and "
            "that max_gap_mm covers the real clearance."
        )
    elif contact.get("tangential_force_fraction", 0.0) > 0.05:
        out["contact_note"] = (
            "the frictionless tangential stabilizer is carrying "
            f"{contact['tangential_force_fraction'] * 100:.1f}% of the contact resultant, "
            "which means the seated part is only held sideways by that numerical spring. "
            "Add the real tangential restraint (a fixture, or a second contact face) "
            "before trusting the shear-driven stresses."
        )
    return out


def _n_rigid_modes(freqs: list) -> int:
    return sum(1 for f in freqs if f < 0.5)  # rigid ~0 Hz, elastic >> 0.5 Hz


def _collect_modal(
    ctx: dict,
    freqs: list,
    n_modes: int,
    notes: list,
    note: Optional[str] = None,
    diagnostics: Optional[dict] = None,
) -> dict:
    """COLLECT stage for a modal solve."""
    d = ctx["doc"]
    mat = ctx.get("material") or {}
    if len(freqs) < n_modes:
        return {
            "ok": False,
            "doc_id": d,
            "error": f"modal solver returned only {len(freqs)}/{n_modes} requested modes; "
            "refusing a partial mode list.",
        }
    try:
        freqs = [
            _finite_float(f, f"freqs_hz[{i}]") for i, f in enumerate(freqs[:n_modes])
        ]
    except ValueError as exc:
        return {
            "ok": False,
            "doc_id": d,
            "error": f"modal solver returned a non-finite frequency: {exc}",
        }
    if any(f < 0.0 for f in freqs):
        return {
            "ok": False,
            "doc_id": d,
            "error": "modal solver returned a negative frequency; the eigensolution is "
            "not physically reportable.",
        }
    if any(b < a for a, b in zip(freqs, freqs[1:])):
        return {
            "ok": False,
            "doc_id": d,
            "error": "modal solver returned frequencies out of ascending order; refusing "
            "a mode list whose numbering is not the requested lowest-first order.",
        }
    modal_meta: dict = {}
    if diagnostics is not None:
        try:
            n_nodes = _positive_int(diagnostics.get("n_nodes"), "modal result.n_nodes")
            n_dof = _positive_int(diagnostics.get("n_dof"), "modal result.n_dof")
        except ValueError as exc:
            return {
                "ok": False,
                "doc_id": d,
                "error": f"modal solver returned incomplete mesh diagnostics: {exc}",
            }
        if n_dof < 3 * n_nodes:
            return {
                "ok": False,
                "doc_id": d,
                "error": "modal solver returned an inconsistent mesh envelope: n_dof is "
                "smaller than three translational DOFs per node.",
            }
        modal_meta = {
            "n_nodes": n_nodes,
            "n_dof": n_dof,
            "mesh": diagnostics.get("mesh_type_used"),
            "solver_notes": diagnostics.get("solver_notes", []),
        }
        if isinstance(diagnostics.get("advisor"), dict):
            modal_meta["advisor"] = diagnostics["advisor"]
        # mesh_quality / provenance / vtu_path ride the same diagnostics payload; a modal
        # solve carries no `reactions` (there is no applied load to balance).
        modal_meta.update(_evidence_fields(diagnostics))
    sid = _persist_study(
        d,
        {
            "type": "fea_modal",
            "name": f"Modal ({mat.get('name', ctx.get('material_arg'))})",
            "engine": "cpp",
            "material": mat,
            "fixtures": ctx.get("fixtures"),
            "n_modes": n_modes,
            "mesh_fineness": ctx.get("mesh_fineness"),
            "learned_advisor": ctx.get("learned_advisor", False),
            "advisor_max_dof": ctx.get("advisor_max_dof", 0),
            "results": {"freqs_hz": freqs, "n_modes": len(freqs)},
            **modal_meta,
        },
    )
    out = {
        "ok": True,
        "doc_id": d,
        "material": mat.get("name", ctx.get("material_arg")),
        "freqs_hz": freqs,
        "n_modes": len(freqs),
        "saved_to_study": bool(sid),
        **modal_meta,
    }
    if not sid:
        out["study_not_saved_reason"] = (
            f"the results could not be written into document {d} (it is not open in this "
            "process). The frequencies above are unaffected."
        )
    if note:
        out["note"] = note
    return _interpreted(out, notes)


def _modal_solve_n(n_modes: int, free_free: bool) -> int:
    """How many modes to ASK the eigensolver for. A free-free part spends its first six on
    rigid-body motion, so over-request (and widen the Krylov subspace) to still get n_modes
    elastic ones."""
    return max(n_modes + 6, 14) if free_free else n_modes


def _modal_rigid_note(freqs: list) -> Optional[str]:
    """The free-free sanity warning, or None when all six rigid modes came back."""
    n = _n_rigid_modes(freqs)
    if n >= 6:
        return None
    return (
        f"warning: free-free solve resolved only {n}/6 rigid-body modes — the mode list "
        "may be missing rigid modes (eigensolver degenerate-cluster limitation); "
        "elastic frequencies are unaffected"
    )


def _merge_default_frequency_sweep(sweep: list, modal_freqs: list) -> list:
    """Add reportable in-band resonances to the default logarithmic sample grid."""
    resonances: list[float] = []
    for raw in modal_freqs:
        try:
            frequency = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(frequency) and 10.0 <= frequency <= 2000.0:
            resonances.append(frequency)
    return sorted(set(sweep + resonances))


# ------------------------------------------------------- durable job wiring
# Every heavy solve below runs its SOLVE stage in a child process tracked by the SQLite
# registry in services/sim/jobs.py. What stays in this process is the SETUP (it needs the
# live document to resolve face queries and export geometry) and the COLLECT (it writes
# the solved study back into that document). The child gets a payload that is pure JSON
# and references only files on disk, so it shares nothing with this process and can be
# killed, polled from a later turn, or collected by a completely different server after a
# restart.
#
# There is exactly ONE collector per job kind and the synchronous tool calls the SAME one,
# so an asynchronous answer equals the synchronous answer by construction.

#: Analyses `sim_run` will start. Every heavy solver is durable now; this list is only the
#: subset `sim_run`'s own argument surface (fixtures/loads/material/n_modes) can express.
_ASYNC_ANALYSES = ("fea_static", "fea_modal")


def _job_notes(st: dict) -> tuple:
    """A finished job's setup context and BC-normalization notes, from its own metadata."""
    meta = st.get("meta") or {}
    return meta, list(meta.get("notes") or [])


def _collect_job_static(st: dict, raw: dict) -> dict:
    meta, notes = _job_notes(st)
    return _collect_static(meta, raw, notes)


def _collect_job_adaptive(st: dict, raw: dict) -> dict:
    meta, notes = _job_notes(st)
    return _collect_adaptive(meta, raw, notes)


def _collect_job_contact(st: dict, raw: dict) -> dict:
    meta, notes = _job_notes(st)
    return _collect_contact(meta, raw, notes)


def _collect_job_modal(st: dict, raw: dict) -> dict:
    meta, notes = _job_notes(st)
    if not raw.get("ok"):
        return {
            "ok": False,
            "doc_id": meta.get("doc"),
            "error": raw.get("error", "modal solve failed"),
        }
    n = int(meta.get("n_modes") or 6)
    freqs = list(raw.get("freqs_hz", []))
    note = None
    if meta.get("free_free"):
        # The subspace widening itself happened in the child (jobs._stage_modal_rpc), so
        # this is the SAME verdict the synchronous path reports, not a weaker one.
        note = _modal_rigid_note(freqs)
        freqs = freqs[:n]
    return _collect_modal(meta, freqs, n, notes, note, raw)


def _collect_job_fatigue(st: dict, raw: dict) -> dict:
    """COLLECT stage for a fatigue solve: Marin-corrected endurance + Basquin life."""
    from . import fea_advanced as fa  # noqa: PLC0415

    meta, notes = _job_notes(st)
    d = meta.get("doc")
    mat = meta.get("material") or {}
    theory = str(meta.get("theory") or "goodman")
    corr = meta.get("marin") or {}
    if not raw.get("ok"):
        return {
            "ok": False,
            "doc_id": d,
            "error": raw.get("error", "fatigue solve failed"),
        }
    theories = (raw.get("summary") or {}).get("theories")
    if not isinstance(theories, dict) or not theories:
        return {
            "ok": False,
            "doc_id": d,
            "error": "fatigue solver returned no mean-stress theory results; no life or safety "
            "factor can be reported.",
        }
    if theory not in theories:
        return {
            "ok": False,
            "doc_id": d,
            "error": f"fatigue solver did not return the requested {theory!r} theory "
            f"(available: {sorted(theories)}).",
        }
    try:
        se = _finite_float(corr.get("se_corrected_mpa"), "marin.se_corrected_mpa")
        sut = _finite_float(meta.get("ultimate_mpa"), "material.ultimate_mpa")
    except ValueError as exc:
        return {
            "ok": False,
            "doc_id": d,
            "error": f"fatigue setup metadata is incomplete: {exc}",
        }
    life = fa.fatigue_summarize(theories, se, sut, theory=theory)
    life.update(
        {
            "ok": True,
            "doc_id": d,
            "marin": corr,
            "material": mat.get("name", meta.get("material_arg")),
        }
    )
    sid = _persist_study(
        d,
        {
            "type": "fea_fatigue",
            "name": f"Fatigue ({mat.get('name', meta.get('material_arg'))}, {theory})",
            "engine": "cpp",
            "material": mat,
            "fixtures": meta.get("fixtures"),
            "loads": meta.get("loads"),
            "results": {
                "theories": life["theories"],
                "regime": life["regime"],
                "cycles": life["cycles"],
                "fos_endurance": life["fos_endurance"],
            },
        },
    )
    life["saved_to_study"] = bool(sid)
    if not sid:
        life["study_not_saved_reason"] = (
            f"the results could not be written into document {d} (it is not open in this "
            "process). The life numbers above are unaffected."
        )
    return _interpreted(life, notes)


def _collect_job_frequency_response(st: dict, raw: dict) -> dict:
    """COLLECT stage for a frequency-response solve.

    The child returns the raw eigensolution (frequencies + mode shapes); the modal
    superposition is a pure numpy reduction with no engine in it, so it runs here where the
    document is available to persist the study.
    """
    meta, notes = _job_notes(st)
    d = meta.get("doc")
    mat = meta.get("material") or {}
    if not raw.get("ok"):
        return {
            "ok": False,
            "doc_id": d,
            "error": raw.get("error", "modal solve failed"),
        }
    n_modes = int(meta.get("n_modes") or 6)
    freqs = raw.get("freqs_hz")
    shapes = raw.get("mode_shapes")
    try:
        n_nodes = _positive_int(raw.get("n_nodes"), "modal result.n_nodes")
    except ValueError as exc:
        return {
            "ok": False,
            "doc_id": d,
            "error": f"modal solve returned invalid node diagnostics: {exc}",
        }
    if not isinstance(freqs, list) or len(freqs) < n_modes:
        got = len(freqs) if isinstance(freqs, list) else 0
        return {
            "ok": False,
            "doc_id": d,
            "error": f"modal solve returned only {got}/{n_modes} requested frequencies; "
            "refusing a partial frequency-response basis.",
        }
    try:
        freqs = [
            _finite_float(f, f"modal result.freqs_hz[{i}]") for i, f in enumerate(freqs)
        ]
    except ValueError as exc:
        return {
            "ok": False,
            "doc_id": d,
            "error": f"modal solve returned an invalid frequency: {exc}",
        }
    if any(f < 0.0 for f in freqs):
        return {
            "ok": False,
            "doc_id": d,
            "error": "modal solve returned a negative frequency; refusing an "
            "unphysical frequency-response basis.",
        }
    if not isinstance(shapes, list) or not shapes:
        return {
            "ok": False,
            "doc_id": d,
            "error": "modal solve returned no mode shapes — cannot build the frequency response "
            "(need freqs_hz + mode_shapes + n_nodes)",
        }
    sweep = list(meta.get("sweep_hz") or [])
    if meta.get("default_sweep"):
        sweep = _merge_default_frequency_sweep(sweep, freqs)
    zeta = float(meta.get("zeta") or 0.02)
    psd = [tuple(pair) for pair in (meta.get("psd_breakpoints") or [])] or None
    # Lazy import — keeps server module load independent of numpy availability.
    from services.sim.vibration import (  # noqa: PLC0415
        evaluate_vibration_spec,
        harmonic_response,
        miles_response,
    )

    kwargs = {
        "freqs_hz": freqs,
        "mode_shapes_flat": shapes,
        "n_nodes": n_nodes,
        "total_mass_kg": float(meta["total_mass_kg"]),
        "excitation_dir": list(meta.get("excitation_dir") or [0.0, 0.0, 1.0]),
        "zeta": zeta,
    }
    harm = harmonic_response(
        sweep_hz=sweep, base_accel_g=float(meta.get("base_accel_g") or 1.0), **kwargs
    )
    if psd is not None:
        rand: dict = miles_response(psd_breakpoints=psd, **kwargs)
        rand["ok"] = True
    else:  # honest degrade — NEVER assume a default vibration profile
        rand = {
            "ok": False,
            "reason": "no_psd_provided",
            "hint": "Provide psd_breakpoints=[[f_hz, g2_per_hz], ...] "
            "(e.g. from the qualification spec) for random-"
            "vibration GRMS via Miles' equation.",
        }
    verdict = evaluate_vibration_spec(
        meta.get("vibration_spec"), harm, rand if rand.get("ok") else None
    )
    sid = _persist_study(
        d,
        {
            "type": "fea_frequency_response",
            "name": f"Frequency response ({mat.get('name', meta.get('material_arg'))})",
            "engine": "cpp+py_modal_superposition",
            "material": mat,
            "fixtures": meta.get("fixtures"),
            "n_modes": n_modes,
            "mesh_fineness": meta.get("mesh_fineness"),
            "results": {
                "freqs_hz": freqs,
                "peak_accel_g": harm.get("peak_accel_g"),
                "peak_freq_hz": harm.get("peak_freq_hz"),
                "total_grms": rand.get("total_grms"),
                "verdict": verdict,
            },
        },
    )
    out = {
        "ok": True,
        "doc_id": d,
        "material": mat.get("name", meta.get("material_arg")),
        "freqs_hz": freqs,
        "harmonic": harm,
        "random": rand,
        "verdict": verdict,
        "damping_assumption": f"zeta={zeta} (Q={1.0 / (2.0 * zeta):g}) — "
        "assumed modal damping, not measured",
        "saved_to_study": bool(sid),
    }
    if not sid:
        out["study_not_saved_reason"] = (
            f"the results could not be written into document {d}; the response numbers are "
            "unaffected."
        )
    return _interpreted(out, notes)


#: Engine keys the LBM wind-tunnel reply forwards verbatim.
_FLOW_RESULT_KEYS = (
    "drag_n",
    "lift_n",
    "cd",
    "frontal_area_mm2",
    "reynolds",
    "ref_length_mm",
    "velocity_ms",
    "aoa_deg",
    "fluid",
    "grid",
    "converged",
    "residual",
    "voxel_mm",
    "min_feature_mm",
    "min_feature_voxels",
    "steps_run",
    "diverged",
    "warning",
)


def _collect_job_cfd_flow(st: dict, raw: dict) -> dict:
    """COLLECT stage for the built-in (LBM) wind tunnel."""
    meta, _notes = _job_notes(st)
    d = meta.get("doc")
    if not raw.get("ok"):
        return {"ok": False, "doc_id": d, "error": raw.get("error", "flow sim failed")}
    if raw.get("diverged"):
        return {
            "ok": False,
            "doc_id": d,
            "converged": False,
            "diverged": True,
            "error": raw.get("warning")
            or "flow solver diverged; no result is reportable. "
            "Reduce velocity or refine the grid.",
        }
    try:
        _validate_flow_result(raw, float(meta["velocity"]))
    except (ValueError, KeyError, TypeError) as exc:
        return {
            "ok": False,
            "doc_id": d,
            "error": f"flow solver returned an incomplete/non-finite or degenerate "
            f"result: {exc}",
        }
    out = {"ok": True, "doc_id": d}
    for k in _FLOW_RESULT_KEYS:
        if k in raw:
            out[k] = raw[k]
    sid = _persist_study(
        d,
        {
            "type": "cfd_flow",
            "name": (
                f"CFD wind tunnel ({meta.get('fluid')}, {meta.get('velocity')} m/s, "
                f"aoa {meta.get('aoa')})"
            ),
            "engine": "cpp-lbm",
            "params": {
                "velocity": meta.get("velocity"),
                "aoa": meta.get("aoa"),
                "fluid": meta.get("fluid"),
                "cells": meta.get("cells"),
                "n_steps": meta.get("n_steps"),
            },
            "results": {k: out[k] for k in out if k not in ("ok", "doc_id")},
        },
    )
    out["saved_to_study"] = bool(sid)
    return out


def _collect_job_openfoam(st: dict, raw: dict) -> dict:
    """COLLECT stage for the OpenFOAM RANS wind tunnel."""
    meta, _notes = _job_notes(st)
    d = meta.get("doc")
    if not raw.get("ok"):
        return {
            "ok": False,
            "doc_id": d,
            "error": raw.get("error", "OpenFOAM run failed"),
        }
    try:
        values = {
            k: _finite_float(raw.get(k), f"OpenFOAM {k}")
            for k in (
                "cd",
                "cl",
                "ld",
                "drag_n",
                "lift_n",
                "side_n",
                "reynolds",
                "ref_area_mm2",
                "vmax_mps",
            )
        }
        iters = _positive_int(raw.get("steps_run"), "OpenFOAM iterations")
    except ValueError as exc:
        return {
            "ok": False,
            "doc_id": d,
            "converged": False,
            "error": f"OpenFOAM returned an incomplete result: {exc}",
        }
    if (
        values["drag_n"] <= 0.0
        or values["cd"] <= 0.0
        or values["reynolds"] <= 0.0
        or values["ref_area_mm2"] <= 0.0
        or values["vmax_mps"] <= 0.0
    ):
        return {
            "ok": False,
            "doc_id": d,
            "converged": False,
            "error": "OpenFOAM returned a degenerate zero/negative drag, coefficient, "
            "Reynolds number, reference area, or velocity; no aerodynamic "
            "result is reportable.",
        }
    diverged = bool(raw.get("diverged"))
    out = {
        "ok": not diverged,
        "doc_id": d,
        "solver": f"OpenFOAM RANS (k-omega SST, {meta.get('openfoam_kind')})",
        "preset": meta.get("preset"),
        **values,
        "converged": (not diverged),
        "iters": iters,
        "notes": list(raw.get("solver_notes") or []),
        "velocity_ms": meta.get("velocity"),
        "aoa_deg": meta.get("aoa"),
        "fluid": meta.get("fluid"),
    }
    if diverged:
        out["error"] = (
            "OpenFOAM diverged; the diagnostic fields are retained but must not "
            "be used as converged coefficients."
        )
        out["next_actions"] = ["retry with preset='medium' or lower velocity"]
        return out
    sid = _persist_study(
        d,
        {
            "type": "cfd_openfoam",
            "name": (
                f"OpenFOAM wind tunnel ({meta.get('fluid')}, {meta.get('velocity')} m/s, "
                f"aoa {meta.get('aoa')})"
            ),
            "engine": "openfoam",
            "params": {
                "velocity": meta.get("velocity"),
                "aoa": meta.get("aoa"),
                "fluid": meta.get("fluid"),
                "preset": meta.get("preset"),
            },
            "results": {k: v for k, v in out.items() if k not in ("ok", "doc_id")},
        },
    )
    out["saved_to_study"] = bool(sid)
    return out


#: job kind -> COLLECT stage. `sim_results` and `get_job` both resolve through this, so a
#: job collected from either surface produces the same envelope and persists its study once.
_JOB_COLLECTORS = {
    "fea_static": _collect_job_static,
    "fea_adaptive": _collect_job_adaptive,
    "fea_contact": _collect_job_contact,
    "fea_modal": _collect_job_modal,
    "fea_fatigue": _collect_job_fatigue,
    "fea_frequency_response": _collect_job_frequency_response,
    "cfd_flow": _collect_job_cfd_flow,
    "cfd_openfoam": _collect_job_openfoam,
}

for _kind, _fn in _JOB_COLLECTORS.items():
    register_durable_collector(_kind, _fn)


def _job_meta(ctx: dict, notes: list, extra: Optional[dict] = None) -> dict:
    """The setup context the COLLECT stage needs, as pure JSON.

    The study is dropped: it is already in the job payload, and its resolved face-point
    sets are the bulkiest thing in a setup — duplicating them into the registry row would
    put megabytes into a SQLite column for no reader.
    """
    meta = {k: v for k, v in ctx.items() if k != "study"}
    meta["notes"] = list(notes)
    if extra:
        meta.update(extra)
    return meta


def _started(job: dict, doc_id) -> dict:
    """The immediate reply for a background solve: a job id that outlives this process."""
    jid = job["job_id"]
    return {
        "ok": True,
        "job_id": jid,
        "status": "running",
        "state": "running",
        "doc_id": doc_id,
        "pid": job.get("pid"),
        "cancel_supported": True,
        "note": (
            "solving in a SEPARATE PROCESS — this call did not wait. The job is "
            "persisted on disk, so it survives an MCP restart and can be collected "
            f"in a later turn. Poll get_job('{jid}') or sim_status('{jid}'); "
            "progress.last_advance_s measures counter/phase inactivity, not heartbeat age. "
            "Cancel for lack of progress only when progress.stalled is true "
            "(running with >120s unchanged counters), not during a healthy long probe. "
            f"sim_cancel('{jid}') really terminates the solver."
        ),
        "next_actions": [f"get_job('{jid}')"],
    }


def _run_solve(
    kind: str, stage: str, payload: dict, meta: dict, *, background: bool, doc_id
) -> dict:
    """Start one durable solve, and either hand back its id or wait out the sync budget."""
    if background:
        return _started(simjobs.start(kind, stage, payload, meta), doc_id)
    return solve_durable_or_handoff(kind, stage, payload, meta)


# ---------------------------------------------- in-process (thread) jobs behind the sim_* tools
#: cad_common._start_job mints ``job-<hex>`` for work that runs as a DRIVER THREAD in this
#: server process rather than as a durable ``sim-*`` child process. Simulation tools hand
#: those ids out too (the convergence ladder driver, the motion->FEA position sweep), so
#: sim_results/sim_status resolve them from the same table get_job reads — an id a sim
#: tool returned must never come back "unknown" from the sim tools.
_THREAD_JOB_PREFIX = "job-"
_THREAD_SIM_KINDS = frozenset({"fea_convergence", "motion_fea_sweep"})
_THREAD_STATES = {"running": simjobs.RUNNING, "done": simjobs.DONE, "error": simjobs.FAILED}


def _thread_job(job_id) -> Optional[dict]:
    """A consistent copy of an in-process job's record (sim states), or None if absent."""
    if not isinstance(job_id, str) or job_id.startswith(_cad_jobs._DURABLE_JOB_PREFIX):
        return None
    with _cad_jobs._JOB_LOCK:
        job = _cad_jobs._JOBS.get(job_id)
        if job is None:
            return None
        snap = dict(job)
        # The ladder driver mutates its progress (and the leg-id list in it) in place.
        progress = copy.deepcopy(job.get("progress") or {})
    started = snap.get("started_at") or time.time()
    end = snap.get("done_at") or time.time()
    out = {
        "job_id": job_id,
        "kind": snap.get("kind"),
        "state": _THREAD_STATES.get(snap.get("status"), str(snap.get("status"))),
        "durable": False,
        "started_at": snap.get("started_at"),
        "done_at": snap.get("done_at"),
        "elapsed_s": round(float(end) - float(started), 3),
        "progress": progress,
        "result": snap.get("result"),
        "error": snap.get("error"),
    }
    out.update({key: snap[key] for key in ("code", "detail") if key in snap})
    return out


def _unknown_thread_job(job_id: str) -> dict:
    """The honest miss for a ``job-*`` id: a lost in-process driver, not a sim registry row."""
    ttl_min = _cad_jobs._JOB_DONE_TTL_S / 60.0
    return {
        "ok": False,
        "job_id": job_id,
        "state": "unknown",
        "error": (
            f"No background job {job_id!r} in this MCP server process. job-* ids are "
            "in-process driver jobs (run_fea_convergence's ladder, "
            "motion_fea_handoff_sweep, background add_features), not rows of the durable "
            "sim registry: they exist only in the server process that started them, and a "
            f"finished one can be dropped once it has been done for {ttl_min:.0f} min. A "
            "restarted server loses the driver but not a ladder's legs — those are durable "
            "sim-* jobs: sim_jobs() lists them and run_fea_convergence(job_ids=[...]) "
            "rebuilds the table from the ones that finished."
        ),
    }


def _thread_job_results(job: dict, partial: bool) -> dict:
    """sim_results for an in-process job: the same refuse/partial/converged contract."""
    job_id, state, progress = job["job_id"], job["state"], job["progress"]
    if partial:
        base = {
            "job_id": job_id,
            "state": state,
            "partial": True,
            "converged": False,
            "elapsed_s": job["elapsed_s"],
        }
        if not progress:
            base.update(
                ok=False,
                error=(
                    f"no partial results exist for {job_id}: the {job['kind']} job "
                    "published no intermediate state. This is NOT an empty result — "
                    "nothing has been computed to report yet."
                ),
            )
            return base
        base.update(
            ok=True,
            progress=progress,
            snapshot={"partial": True, **progress},
            warning=(
                "PARTIAL SNAPSHOT — this is the job's last progress record, NOT a "
                "converged solution. Do not quote it as a result."
            ),
        )
        if state == simjobs.DONE:
            base["note"] = (
                f"the job has since FINISHED — call sim_results('{job_id}') for the "
                "converged result"
            )
        return base
    if state == simjobs.DONE:
        result = job["result"]
        out = dict(result) if isinstance(result, dict) else {"ok": True, "value": result}
        # The ladder reports its own convergence verdict; other kinds converge exactly
        # when they finished ok.
        out.setdefault("converged", bool(out.get("ok")))
        out.update(job_id=job_id, state=simjobs.DONE, elapsed_s=job["elapsed_s"])
        if progress:
            out["progress"] = progress
        return out
    out = {
        "ok": False,
        "job_id": job_id,
        "state": state,
        "elapsed_s": job["elapsed_s"],
        "error": f"job {job_id} is {state} — there are no converged results to return. "
        "Refusing to answer with a partial or absent solution.",
    }
    if progress:
        out["progress"] = progress
    if state == simjobs.FAILED:
        out["solver_error"] = job["error"]
        out.update({key: job[key] for key in ("code", "detail") if key in job})
        return out
    out["hint"] = (
        f"wait_job('{job_id}') blocks until it finishes; sim_results('{job_id}', "
        "partial=True) returns the progress so far, clearly marked partial"
    )
    current = progress.get("current_job_id")
    if current:
        out["hint"] += (
            f". The leg solving now is the durable job {current} (sim_status/sim_cancel "
            "take that id)"
        )
    return out


def _thread_job_status(job: dict) -> dict:
    """sim_status for an in-process job: state, elapsed and progress, never the result."""
    job_id, state = job["job_id"], job["state"]
    st = {key: value for key, value in job.items() if key != "result"}
    if st.get("error") is None:
        st.pop("error")
    st["ok"] = state != simjobs.FAILED
    if state == simjobs.DONE:
        st["next_actions"] = [f"sim_results('{job_id}')"]
    elif state == simjobs.RUNNING:
        st["next_actions"] = [f"wait_job('{job_id}')", f"sim_results('{job_id}', partial=True)"]
        st["note"] = (
            "still running in this server process (an in-process driver, not a durable "
            "solver process) — nothing has converged yet."
        )
        current = job["progress"].get("current_job_id")
        if current:
            st["note"] += (
                f" The leg solving now is the durable job {current}: sim_status('{current}') "
                f"shows its solver progress and sim_cancel('{current}') kills it."
            )
    return st


def _thread_job_cancel(job: dict) -> dict:
    """sim_cancel for an in-process job: there is no solver process of its own to kill."""
    job_id, state = job["job_id"], job["state"]
    out = {
        "ok": False,
        "job_id": job_id,
        "state": state,
        "error": (
            f"job {job_id} is an in-process {job['kind']} driver, not a solver process: "
            "sim_cancel has no process of its own to kill, and nothing was cancelled."
        ),
    }
    current = job["progress"].get("current_job_id")
    if state == simjobs.RUNNING and current:
        out["hint"] = (
            f"sim_cancel('{current}') kills the leg solving now; a failed leg stops the "
            "ladder unless it was started with continue_on_fail=True."
        )
    return out


# ------------------------------------------------------------------ mesh convergence (ladder)
# The simulation runbook's release gate: "Mesh at three successively finer characteristic
# sizes. Do not accept a single-mesh peak", then "compare monitored non-singular stress and
# displacement values between meshes. State the convergence metric and tolerance BEFORE
# choosing the final mesh." That is a study of N static solves on ONE setup, so the ladder is
# built here from a single resolved static context: same STEP export, same resolved fixture /
# load point sets, only the discretization changes. Re-running run_fea_static per size would
# re-export and re-resolve every leg, and nothing would guarantee the legs share a setup.

#: Default convergence tolerance, in percent of the previous leg's monitored value. 5% is
#: tighter than the <10% settle rule the sim skill states for a two-mesh check, because a
#: three-point ladder can see the trend and a released number should not sit on the edge.
_CONVERGENCE_TOL_PCT = 5.0

#: The `summary` scalars a caller may monitor. A convergence study is only meaningful on a
#: value that converges: peak-stress family, displacement, or the factor of safety derived
#: from them. Anything else in `summary` (or a typo) is refused rather than tabulated.
_MONITORABLE = (
    "max_von_mises_mpa",
    "max_disp_mm",
    "min_fos",
    "max_tresca_mpa",
    "max_rankine_mpa",
    "max_principal_mpa",
    "min_principal_mpa",
)


#: A ladder leg only means something if its MESH really changed. core/sim/mesh_size_band
#: governs the requested size against an element budget ("auto-coarsened for interactivity"),
#: so on a big part several `fineness` rungs can collapse onto ONE budget-limited mesh: three
#: legs, identical n_dof, delta 0.0% — which the tolerance test then reads as convergence.
#: Observed live on a 2.6 m tube frame: 394,896 DOF three times over, reported converged.
#: Anything under this much DOF growth between consecutive legs is not a finer mesh.
_LADDER_MIN_DOF_GROWTH_PCT = 5.0

#: The substrings the engine's mesh-size governor stamps into its notes when its element
#: budget decides the discretization instead of the requested size
#: (core/sim/mesh_size_band.cpp). BOTH outcomes collapse a `fineness` ladder onto one mesh,
#: so both have to be disclosed: the governor either raises the size ("auto-coarsened ...",
#: for interactivity or by the memory governor) or, since 2026-09-05, refuses to raise it
#: past the geometry floor of four elements across the part's smallest dimension and pins
#: every rung at the same size instead ("element budget exceeded but NOT coarsened: ...").
#: The floor note deliberately does NOT carry the "auto-coarsened" marker — nothing was
#: coarsened — which is exactly why matching that one marker alone left a degenerate ladder
#: with no engine words explaining itself.
_COARSENED_MARKERS = (
    "auto-coarsened",
    "element budget exceeded but NOT coarsened",
)


def _result_note_strings(out: dict, *, include_error: bool = False) -> list:
    """Every free-text note a collected leg envelope carries, flattened into one list.

    The engine's own words live in three different places depending on the solve
    (`solver_notes`, `notes`, `mesh_quality.notes|note`), and a REFUSED solve says what
    happened only in `error` — which is exactly where the resource governor's refusal
    lands, so callers that need it ask for it explicitly.
    """
    pools: list = [out.get("solver_notes"), out.get("notes")]
    quality = out.get("mesh_quality")
    if isinstance(quality, dict):
        pools.extend((quality.get("notes"), quality.get("note")))
    if include_error:
        pools.append(out.get("error"))
    found: list = []
    for pool in pools:
        items = [pool] if isinstance(pool, str) else pool
        if not isinstance(items, (list, tuple)):
            continue
        found.extend(s for s in items if isinstance(s, str) and s)
    return found


def _coarsening_notes(out: dict) -> list:
    """The engine notes saying the size governor, not the request, set the discretization."""
    found: list = []
    for note in _result_note_strings(out):
        if note in found:
            continue
        if any(marker in note for marker in _COARSENED_MARKERS):
            found.append(note)
    return found


# ------------------------------------------------------------------ the MEMORY governor
# A different governor from the mesh-size one above, and the one that killed a live study:
# core/util/resource_governor budgets every solve a fraction of the RAM AVAILABLE at the
# moment it starts (CHUDWARE_MEM_FRAC of TOTAL, then clamped to available minus
# CHUDWARE_HEADROOM_GB, floored at min(3 GB, 40% of total)). It is recomputed per call, so
# parallel solves shrink each other's budget — twelve legs launched at once were all
# refused for want of memory while the same meshes solved fine one at a time. The number
# is therefore evidence about the MACHINE at solve time and belongs on the row.

#: The governor's decision text, as core/util/resource_governor.cpp formats it:
#:   plan/admit report  "governor: plan what=fea_solver workers=6/6 reserve=0.10GB
#:                       budget=9.42GB rss=1.20GB avail=13.60GB total=31.00GB ..."
#:   admission refusal  "static FEA solve needs an estimated 20.5 GB but this machine's
#:                       budget is 9.4 GB (31 GB RAM, 13.6 GB available) — ..."
#: The refusal is the important one: it arrives as the failed leg's `error` and IS the
#: explanation for a leg that solves alone and fails beside its siblings.
_GB_NUM = r"([0-9]+(?:\.[0-9]+)?)"
_GOV_FIELD_RES = {
    "budget_gb": re.compile(r"\bbudget=" + _GB_NUM + "GB"),
    "avail_gb": re.compile(r"\bavail=" + _GB_NUM + "GB"),
    "total_gb": re.compile(r"\btotal=" + _GB_NUM + "GB"),
}
_GOV_WORKERS_RE = re.compile(r"\bworkers=([0-9]+)")
_GOV_REFUSAL_RE = re.compile(
    r"budget is " + _GB_NUM + r" GB \(" + _GB_NUM + r" GB RAM, " + _GB_NUM
    + r" GB available\)"
)
_GOV_ESTIMATE_RE = re.compile(r"estimated " + _GB_NUM + r" GB")


def _memory_budget(out: dict) -> dict:
    """The memory governor's decision for one leg, in the governor's OWN numbers.

    ``{budget_gb, avail_gb, total_gb, workers}`` (plus ``estimate_gb`` when the refusal
    states what the solve wanted), taken from whatever the envelope exposes — the plan
    line when the solver logged it, the refusal prose when the leg was turned away.
    Returns ``{}`` when nothing in the envelope names a budget: an unreported decision
    stays absent rather than becoming a number this file invented.
    """
    found: dict = {}
    for text in _result_note_strings(out, include_error=True):
        if "governor" not in text and "budget" not in text:
            continue
        refusal = _GOV_REFUSAL_RE.search(text)
        if refusal:
            found.setdefault("budget_gb", float(refusal.group(1)))
            found.setdefault("total_gb", float(refusal.group(2)))
            found.setdefault("avail_gb", float(refusal.group(3)))
            estimate = _GOV_ESTIMATE_RE.search(text)
            if estimate:
                found.setdefault("estimate_gb", float(estimate.group(1)))
        for name, pattern in _GOV_FIELD_RES.items():
            hit = pattern.search(text)
            if hit:
                found.setdefault(name, float(hit.group(1)))
        workers = _GOV_WORKERS_RE.search(text)
        if workers:
            found.setdefault("workers", int(workers.group(1)))
    return found


#: Said on every ladder reply, because the agent that starved this study believed the
#: budget was a fixed ~70% of TOTAL RAM while the governor was reporting 9.4-13.6 GB.
_RESOURCES_NOTE = (
    "the engine's memory governor gives each solve a fraction of the RAM AVAILABLE when "
    "that solve starts (CHUDWARE_MEM_FRAC of total, then clamped to available minus "
    "CHUDWARE_HEADROOM_GB) — NOT a fixed share of total RAM. Concurrent studies therefore "
    "shrink each other's budget until the fine meshes are refused outright, so the legs of "
    "this ladder solve ONE AT A TIME and you should run ONE LADDER AT A TIME."
)


def _resources_line(rows: list) -> str:
    """One line on what the machine granted this ladder, plus the rule it follows."""
    for row in reversed(rows):
        budget = row.get("memory_budget")
        if not isinstance(budget, dict) or not budget:
            continue
        parts: list = []
        for key, label in (
            ("budget_gb", "budget"),
            ("avail_gb", "available"),
            ("total_gb", "total"),
        ):
            if key in budget:
                parts.append(f"{label} {budget[key]:.2f} GB")
        if "workers" in budget:
            parts.append(f"workers {budget['workers']}")
        if parts:
            return "governor reported " + ", ".join(parts) + " — " + _RESOURCES_NOTE
        break
    return _RESOURCES_NOTE


def _degenerate_legs(rows: list) -> list:
    """Ladder pairs that are the SAME discretization, so their delta is not a convergence.

    Two shapes count: any two legs with an identical (n_dof, n_elems) mesh — the governor
    handing back one budget-limited mesh for several rungs — and consecutive legs whose DOF
    count barely moved, which is the same failure with rounding on top.
    """
    hits: list = []
    seen: dict = {}
    for i, row in enumerate(rows):
        dof, elems = row.get("n_dof"), row.get("n_elems")
        if not isinstance(dof, (int, float)) or isinstance(dof, bool) or dof <= 0:
            continue
        key = (dof, elems)
        if key in seen:
            hits.append(
                {
                    "legs": [seen[key], i],
                    "n_dof": [dof, dof],
                    "n_elems": [elems, elems],
                    "dof_growth_pct": 0.0,
                    "identical_mesh": True,
                }
            )
        else:
            seen[key] = i
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        a, b = prev.get("n_dof"), cur.get("n_dof")
        if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in (a, b)):
            continue
        if a <= 0:
            continue
        growth = (float(b) - float(a)) / abs(float(a)) * 100.0
        identical = a == b and prev.get("n_elems") == cur.get("n_elems")
        if identical or growth >= _LADDER_MIN_DOF_GROWTH_PCT:
            continue  # identical pairs are already recorded above; healthy growth is fine
        hits.append(
            {
                "legs": [i - 1, i],
                "n_dof": [a, b],
                "n_elems": [prev.get("n_elems"), cur.get("n_elems")],
                "dof_growth_pct": round(growth, 3),
                "identical_mesh": False,
            }
        )
    hits.sort(key=lambda h: (h["legs"][1], h["legs"][0]))
    for hit in hits:
        reasons = [
            (rows[i].get("governor") or {}).get("reason")
            for i in hit["legs"]
        ]
        hit["cause"] = next(
            (reason for reason in reasons if reason in
             ("memory_admission", "size_band_floor", "feature_resolution",
              "probe_time_budget")),
            "feature_resolution",
        )
    return hits


#: A delivered size this far off the request is a different study from the one asked for.
#: Live on 2026-09-06: a ladder requested at 1.6/1.2/0.9 mm was meshed at 5.62/5.43/5.42 mm
#: by the memory governor — ratios of 3.5x, 4.5x and 6.0x — and the only trace was governor
#: prose the reader had to reverse-engineer the size out of.
_SIZE_DELIVERY_MAX_RATIO = 1.25
#: The other direction: a mesh finer than asked for is also not the stated discretization
#: (a band floor or a feature-refine can pull it down), and it changes the cost of the leg.
_SIZE_DELIVERY_MIN_RATIO = 0.8


def _size_delivery(rows: list) -> list:
    """Requested vs DELIVERED element size, one entry per solved leg.

    `size_mm` on a row is what the caller asked for and `delivered_size_mm` is what the
    mesher actually meshed at (engine `mesh_quality.char_len_mm`). They are routinely not
    the same number — the memory governor sizes against an element budget — and until this
    table existed the delivered size appeared nowhere in the reply at all, so a ladder read
    as three fine meshes when it was three coarse ones. A leg that failed or was skipped has
    no mesh and gets no entry; `ratio` is null when either side is unavailable (a `fineness`
    rung states no size in mm, and an inline mesh reports no length).
    """
    out: list = []
    for i, row in enumerate(rows):
        if row.get("error") or row.get("skipped"):
            continue
        requested = row.get("size_mm")
        delivered = row.get("delivered_size_mm")
        usable = [
            v
            for v in (requested, delivered)
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
        ratio = (
            round(float(delivered) / float(requested), 4)
            if len(usable) == 2 and float(requested) > 0.0
            else None
        )
        out.append(
            {
                "leg": i,
                "requested_mm": requested,
                "delivered_mm": delivered,
                "ratio": ratio,
            }
        )
    return out


def _size_delivery_note(delivery: list) -> Optional[str]:
    """One sentence naming the legs whose delivered size missed the request, else None.

    Pure function of the `size_delivery` table so the disclosure can be tested without an
    engine. It is appended to the reply's existing `note` rather than added as a second
    note field: a reader who is being told the ladder is degenerate must be told in the
    same breath that the sizes are not the ones they asked for.
    """
    coarse = [
        e for e in delivery
        if isinstance(e.get("ratio"), (int, float)) and e["ratio"] > _SIZE_DELIVERY_MAX_RATIO
    ]
    fine = [
        e for e in delivery
        if isinstance(e.get("ratio"), (int, float)) and e["ratio"] < _SIZE_DELIVERY_MIN_RATIO
    ]
    if not coarse and not fine:
        return None
    parts = [
        f"leg {e['leg']} asked {e['requested_mm']:g} mm and got {e['delivered_mm']:g} mm "
        f"({e['ratio']:g}x)"
        for e in sorted(coarse + fine, key=lambda e: e["leg"])
    ]
    return (
        "DELIVERED SIZE != REQUESTED SIZE — "
        + "; ".join(parts)
        + f" (tolerance: {_SIZE_DELIVERY_MIN_RATIO}x-{_SIZE_DELIVERY_MAX_RATIO}x of the "
        "request); the mesh-size governor sized these legs against its element budget, so "
        "size_mm is the request and each row's delivered_size_mm is what solved."
    )


def _convergence_ladder(sizes_mm, fineness) -> tuple:
    """The explicit coarse-to-fine discretization ladder: ("target_size_mm"|"fineness", [...]).

    Both spellings are EXPLICIT lists — the point of the study is that the caller states the
    ladder before seeing any result, so there is no implicit default ladder here. Refusing a
    non-monotone list is not pedantry: `delta_pct_vs_prev` is only a convergence statement
    when each leg really is finer than the one before it.
    """
    if sizes_mm is not None and fineness is not None:
        raise ValueError(
            "pass sizes_mm (characteristic element size in mm) OR fineness (the 0..1 "
            "mesh_fineness scale), not both — they are two spellings of one ladder."
        )
    if sizes_mm is not None:
        raw = require_list(
            sizes_mm,
            "sizes_mm",
            "a coarse-to-fine list of at least 2 characteristic element sizes in mm, "
            "e.g. [4, 2, 1]",
        )
        sizes = [_finite_float(v, f"sizes_mm[{i}]") for i, v in enumerate(raw)]
        if any(s <= 0.0 for s in sizes):
            raise ValueError("every sizes_mm entry must be > 0 mm.")
        if len(sizes) < 2:
            raise ValueError(
                "a convergence study needs at least 2 meshes — one mesh has nothing to "
                "converge against. The runbook asks for three successively finer sizes."
            )
        if any(b >= a for a, b in zip(sizes, sizes[1:])):
            raise ValueError(
                "sizes_mm must be strictly DECREASING (coarse first), e.g. [4, 2, 1]: "
                "each leg has to be finer than the previous one."
            )
        return "target_size_mm", sizes
    if fineness is None:
        raise ValueError(
            "run_fea_convergence needs an explicit ladder: sizes_mm=[4,2,1] "
            "(characteristic element size in mm) or fineness=[0.25,0.5,0.75] "
            '(the mesh_fineness scale, preset words accepted).'
        )
    raw = require_list(
        fineness,
        "fineness",
        'a coarse-to-fine list of at least 2 mesh_fineness values, e.g. '
        '[0.25, 0.5, 0.75] or ["coarse","medium","fine"]',
    )
    vals = [_norm_fineness(v, f"fineness[{i}]") for i, v in enumerate(raw)]
    if len(vals) < 2:
        raise ValueError(
            "a convergence study needs at least 2 meshes — one mesh has nothing to "
            "converge against. The runbook asks for three successively finer meshes."
        )
    if any(b <= a for a, b in zip(vals, vals[1:])):
        raise ValueError(
            "fineness must be strictly INCREASING (coarse first), e.g. [0.25,0.5,0.75]: "
            "each leg has to be finer than the previous one."
        )
    return "fineness", vals


def _leg_label(step: dict) -> str:
    """How a ladder rung is named in prose: its stated size, else its fineness."""
    size = step.get("target_size_mm")
    if isinstance(size, (int, float)) and not isinstance(size, bool):
        return f"size {size} mm"
    return f"fineness {step.get('fineness')}"


def _leg_vtu_path(vtu_dir: str, doc_id, index: int, step: dict) -> str:
    """Where leg *index* writes its field: ``<vtu_dir>/<doc>_leg<k>_h<size>_o<order>.vtu``.

    The name has to survive being read months later beside the packet it belongs to, so it
    carries the three things that make a ladder leg what it is — its position, its
    discretization and its element order. A fineness rung has no size in mm, so it is
    named `f<fineness>` instead of `h<size>`; nothing else changes.

    The `h<size>` in the name is the REQUESTED size, fixed before the leg is solved. It is
    NOT necessarily the size that was meshed: the engine's mesh-size governor sizes against
    an element budget, so a file named `_h0.9_` can hold a 5.4 mm field. The row's
    `delivered_size_mm` (engine `mesh_quality.char_len_mm`) is the truth about the mesh
    behind this file; the filename is only a stable label for the rung that asked for it.
    """
    base = (_handoff_doc_name(str(doc_id)) or "").strip() or str(doc_id or "part")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._-") or "part"
    size = step.get("target_size_mm")
    rung = (
        f"h{size:g}"
        if isinstance(size, (int, float)) and not isinstance(size, bool)
        else f"f{step.get('fineness')}"
    )
    return os.path.join(
        vtu_dir, f"{stem}_leg{index}_{rung}_o{step.get('mesh_order')}.vtu"
    )


def _convergence_skipped_row(step: dict, reason: str) -> dict:
    """A leg that was never solved because an earlier leg failed and stopped the ladder.

    It keeps its row for the same reason a failed leg does: a table that simply ends at
    the coarse end reads as "converged there".
    """
    return {
        "size_mm": step.get("target_size_mm"),
        "fineness": step.get("fineness"),
        "mesh_order": step.get("mesh_order"),
        # Only when the ladder stated a floor: it is part of the discretization this
        # row's numbers belong to, and it is what let the leg resolve its features.
        **({"min_size_mm": step["min_size_mm"]}
           if step.get("min_size_mm") is not None else {}),
        "skipped": True,
        "error": reason,
        "monitored_value": None,
        "delta_pct_vs_prev": None,
    }


def _convergence_row(step: dict, out: dict, monitor: str, prev: Optional[float]) -> dict:
    """One ladder row from a collected static envelope, plus its delta against the previous.

    A leg that failed keeps its row (with `error`) instead of vanishing: a table missing its
    finest mesh silently reads as "converged at the coarse end".
    """
    row = {
        "size_mm": step.get("target_size_mm"),
        # What the mesher DELIVERED, filled in below from the engine's own
        # mesh_quality.char_len_mm. `size_mm` above stays the REQUESTED size; the two
        # differ whenever the mesh-size governor sizes against its element budget, and
        # without this number on the row that substitution was invisible.
        "delivered_size_mm": None,
        "fineness": step.get("fineness"),
        "mesh_order": step.get("mesh_order"),
        **({"min_size_mm": step["min_size_mm"]}
           if step.get("min_size_mm") is not None else {}),
    }
    if isinstance(out.get("provenance"), dict):
        row["provenance"] = out["provenance"]
        row["governor"] = out["provenance"].get("governor", {})
    budget = _memory_budget(out)
    if budget:
        # Attached BEFORE the failure return: on a refused leg the governor's budget is
        # not a footnote, it is the reason the leg has no result.
        row["memory_budget"] = budget
    if not out.get("ok"):
        row["error"] = out.get("error") or "leg failed"
        row["monitored_value"] = None
        row["delta_pct_vs_prev"] = None
        return row
    summary = out.get("summary") or {}
    row["n_dof"] = out.get("n_dof")
    row["n_nodes"] = out.get("n_nodes")
    row["n_elems"] = out.get("n_elems")
    row["max_von_mises_mpa"] = summary.get("max_von_mises_mpa")
    row["max_disp_mm"] = summary.get("max_disp_mm")
    row["min_fos"] = summary.get("min_fos")
    # The ladder row is what a released packet quotes, and max_von_mises_mpa is a
    # fixture-ring-MASKED reduction: carrying it without its reduction label and its
    # unmasked twin is how a packet came to report 105.98 MPa as "the peak at the
    # keyway bolt-hole edge" while the field peaked at 402.93 MPa there. Pass-through
    # only — nothing is re-derived here.
    for key in ("max_von_mises_reduction", "max_von_mises_raw_mpa", "min_fos_raw",
                "underresolved_wall", "resolution_caveat"):
        if key in summary:
            row[key] = summary.get(key)
    if isinstance(out.get("mesh_quality"), dict):
        row["mesh_quality"] = out["mesh_quality"]
        delivered = out["mesh_quality"].get("char_len_mm")
        if isinstance(delivered, (int, float)) and not isinstance(delivered, bool):
            row["delivered_size_mm"] = delivered
    if isinstance(out.get("reactions"), dict):
        row["equilibrium_error_pct"] = out["reactions"].get("equilibrium_error_pct")
    # The solve echoes the .vtu it wrote; the step is the fallback for an engine build
    # that writes the file without echoing the path, because a row that names no field
    # cannot be released even when the field is on disk.
    vtu = out.get("vtu_path") or step.get("vtu_path")
    if vtu:
        row["vtu_path"] = vtu
    plausibility = out.get("plausibility")
    if isinstance(plausibility, dict):
        row["plausibility"] = plausibility
        if plausibility.get("flag") == "implausible":
            # A leg whose peak displacement exceeded the whole part did not solve the
            # elastic problem this ladder is refining, so it is neither a reference for
            # the next leg nor evidence of convergence. It keeps its row (and its
            # numbers, for diagnosis) and is excluded from the arithmetic.
            row["implausible"] = True
    governor = _coarsening_notes(out)
    if governor:
        # The engine's own admission that it did not mesh at the size we asked for. Without
        # it on the row, a collapsed ladder looks like a well-behaved one.
        row["mesh_governor_notes"] = governor
    value = summary.get(monitor)
    row["monitored_value"] = value
    # A zero previous value has no percentage change (division by zero is not "0% drift"),
    # so the delta is honestly absent and the tolerance check cannot pass on it. An
    # implausible leg has no comparable value at all.
    if (
        row.get("implausible")
        or prev in (None, 0.0)
        or not isinstance(value, (int, float))
    ):
        row["delta_pct_vs_prev"] = None
    else:
        row["delta_pct_vs_prev"] = round(100.0 * (value - prev) / abs(prev), 3)
    return row


def _released_leg(rows: list) -> Optional[dict]:
    """The leg a release would quote: the FINEST one that actually solved.

    A ladder's purpose is to justify one number, and that number comes from the finest
    usable leg — not from the table as a whole. Naming it (with its .vtu, when the run
    was given a `vtu_dir`) is what lets `sim_evidence_packet` hash the field the released
    scalars came from instead of a packet claiming a convergence table with no field
    behind it. A failed, skipped or implausible leg is not a candidate: it produced no
    releasable number. This is NOT a verdict — `converged` is; a released leg exists even
    on a ladder that has not converged, because the run still has a best available field.
    """
    for i in range(len(rows) - 1, -1, -1):
        row = rows[i]
        if row.get("error") or row.get("skipped") or row.get("implausible"):
            continue
        leg = {
            "index": i,
            "size_mm": row.get("size_mm"),
            "fineness": row.get("fineness"),
            "mesh_order": row.get("mesh_order"),
            "n_dof": row.get("n_dof"),
            "monitored_value": row.get("monitored_value"),
            "delta_pct_vs_prev": row.get("delta_pct_vs_prev"),
        }
        if row.get("vtu_path"):
            leg["vtu_path"] = row["vtu_path"]
        return leg
    return None


def _convergence_envelope(
    doc_id, monitor: str, tol_pct: float, rows: list, notes: list
) -> dict:
    """The convergence verdict over a finished ladder — never a PASS on a partial table."""
    failed = [i for i, r in enumerate(rows) if r.get("error") and not r.get("skipped")]
    skipped = [i for i, r in enumerate(rows) if r.get("skipped")]
    degenerate = _degenerate_legs(rows)
    implausible = [i for i, r in enumerate(rows) if r.get("implausible")]
    delivery = _size_delivery(rows)
    last_delta = rows[-1].get("delta_pct_vs_prev") if rows else None
    converged = (
        not failed
        and not skipped
        and not degenerate
        and not implausible
        and len(rows) >= 2
        and isinstance(last_delta, (int, float))
        and abs(last_delta) < tol_pct
    )
    out = {
        "ok": not failed and not skipped,
        "doc_id": doc_id,
        "monitor": monitor,
        "tol_pct": tol_pct,
        "table": rows,
        "n_meshes": len(rows),
        "converged": converged,
        "degenerate_ladder": bool(degenerate),
        # Requested vs delivered element size per solved leg, beside the degeneracy
        # verdict because it is usually the CAUSE of one: legs the governor collapsed
        # onto one budget-limited size are also legs whose delivered size is not the
        # size the caller asked for.
        "size_delivery": delivery,
        "final_delta_pct": last_delta,
        "resources": _resources_line(rows),
        "note": (
            "converged is |delta of the FINEST leg| < tol_pct on the monitored value; "
            "a peak that keeps growing with refinement is a singularity, not strength."
        ),
    }
    released = _released_leg(rows)
    if released:
        out["released_leg"] = released
    if degenerate:
        out["degenerate_legs"] = degenerate
        governor = {
            str(i): r["mesh_governor_notes"]
            for i, r in enumerate(rows)
            if r.get("mesh_governor_notes")
        }
        pairs = ", ".join(
            f"legs {h['legs'][0]}&{h['legs'][1]} "
            + (
                f"identical ({h['n_dof'][0]} DOF, {h['n_elems'][0]} elems)"
                if h["identical_mesh"]
                else f"only {h['dof_growth_pct']}% DOF apart"
            )
            for h in degenerate
        )
        out["note"] = (
            "DEGENERATE LADDER — this is NOT a convergence statement. The legs did not "
            f"discretize differently: {pairs} (threshold: consecutive legs must differ by "
            f"at least {_LADDER_MIN_DOF_GROWTH_PCT}% in n_dof). A near-zero "
            "delta_pct_vs_prev between two copies of the same mesh says nothing about "
            "mesh sensitivity, so converged is forced false. Cause: the mesh-size "
            "governor (core/sim/mesh_size_band) decides the size against an element "
            "budget rather than honouring the request, so on a large part several "
            "`fineness` rungs land on one budget-limited size — and on a THIN part they "
            "land on one geometry floor instead, because the governor will not coarsen "
            "below four elements across the part's smallest dimension. Remedy: re-run "
            "with explicit sizes_mm=[...] (they map straight to study.target_size_mm and "
            "bypass the fineness governor), e.g. sizes_mm=[8,5,3] for a large frame. If "
            "the governor is still the one choosing — this result's "
            "mesh_governor_notes carries its exact words for every affected leg — then "
            "its element budget is capping the whole ladder and run_fea_adaptive (which "
            "refines against an error indicator instead of a stated size) is the "
            "alternative evidence path."
        )
        if governor:
            out["mesh_governor_notes"] = governor
    if implausible:
        out["implausible_legs"] = implausible
        out["note"] = (
            f"IMPLAUSIBLE LEG(S) {implausible} — this is NOT a convergence statement. "
            "Those legs' peak displacement exceeded the model's own bounding-box "
            "diagonal, so the linear-elastic small-strain assumption the solve is built "
            "on is broken and the number is not a deflection: the usual cause is a "
            "mechanism in the mesh (a thin wall meshed one element thick, or a region "
            "held by too few fixture nodes). They keep their rows for diagnosis but are "
            "excluded from delta_pct_vs_prev and from converged. Read each leg's "
            "plausibility.max_disp_over_bbox_diag and mesh_quality.thin_wall_ratio / "
            "unfixed_components, refine so h <= the wall thickness, and confirm the "
            "fixture faces resolved onto mesh nodes."
        )
    # Appended LAST so it survives whichever verdict claimed `note` above: a ladder that
    # was not meshed at the sizes it stated has to say so in the same sentence a reader
    # takes its verdict from, not in a field they have to know to look for.
    delivery_note = _size_delivery_note(delivery)
    if delivery_note:
        out["note"] = out["note"] + " " + delivery_note
    if skipped:
        out["skipped_legs"] = skipped
    if failed:
        out["error"] = (
            f"{len(failed)} of {len(rows)} convergence legs failed (rows "
            f"{failed})"
            + (
                f" and the {len(skipped)} finer leg(s) after the first failure (rows "
                f"{skipped}) were never solved"
                if skipped
                else ""
            )
            + "; the table is retained for diagnosis and is NOT a convergence "
            "statement."
        )
    elif skipped:
        out["error"] = (
            f"{len(skipped)} of {len(rows)} convergence legs were skipped (rows "
            f"{skipped}); a partial ladder is NOT a convergence statement."
        )
    return _interpreted(out, notes)


def _convergence_leg_solve(
    payload: dict, meta: dict, deadline: Optional[float], job_id: Optional[str] = None
) -> tuple:
    """Solve ONE ladder leg as a durable job, returning ``(job_id, envelope | None)``.

    Same three primitives `solve_durable_or_handoff` drives (start, status, collect), but
    the JOB ID comes back for EVERY leg and the whole ladder shares ONE transport deadline
    instead of taking the sync budget N times over. Both matter: a collected envelope
    carries no id, and without one a ladder whose tail overran could never be reassembled
    into a single table. `None` means "still running at the deadline" — the leg is left
    solving in its own process, exactly as a handoff does.

    `job_id` re-attaches to a leg that is ALREADY solving (the synchronous call ran out of
    transport budget in the middle of one), and `deadline=None` waits for the leg however
    long it takes — what the background ladder driver does, because the next leg must not
    start until this one is finished.
    """
    jid = job_id or simjobs.start("fea_static", "core_rpc", payload, meta)["job_id"]
    interval = 0.02
    while deadline is None or time.time() < deadline:
        st = simjobs.status(jid)
        state = st.get("state")
        if state == simjobs.DONE:
            return jid, collect_durable(jid, st)
        if state in (simjobs.FAILED, simjobs.CANCELLED):
            return jid, {
                **(simjobs.refusal_result(jid) or {}),
                "ok": False,
                "job_id": jid,
                "state": state,
                "error": st.get("error") or f"solve {state}",
            }
        if state == "unknown":
            # The registry lost the job (pruned, or a spool wiped underneath us). An
            # unbounded wait must never be a wait for something that cannot arrive.
            return jid, {
                "ok": False,
                "job_id": jid,
                "state": state,
                "error": st.get("error") or f"job {jid} is no longer in the job registry",
            }
        time.sleep(interval)
        interval = min(0.4, interval * 1.5)
    return jid, None


class _ConvergenceLadder:
    """The sequential driver for one mesh-convergence ladder.

    The legs of a ladder are N static solves of ONE setup on ever finer meshes, and they
    must NOT run at the same time. core/util/resource_governor grants each solve a
    fraction of the RAM AVAILABLE when it starts, so simultaneous legs shrink each
    other's budget: observed live, three legs times four ladders launched back-to-back
    ran as twelve concurrent child processes and every one of them failed for want of
    memory, while the same meshes solved cleanly one at a time on a quiet machine.

    So exactly one leg is ever in flight, coarse to fine. `pump` is resumable: the
    synchronous call pumps it under the transport budget, and when that budget runs out
    the SAME object keeps pumping (deadline `None`) on the background thread, which is
    why the tail of an overrunning ladder is still sequential.
    """

    def __init__(
        self,
        plan: list,
        doc_id,
        monitor: str,
        tol_pct: float,
        notes: list,
        continue_on_fail: bool,
    ) -> None:
        self.plan = plan
        self.doc_id = doc_id
        self.monitor = monitor
        self.tol_pct = tol_pct
        self.notes = notes
        self.continue_on_fail = continue_on_fail
        self.rows: list = []
        self.job_ids: list = []
        self.prev: Optional[float] = None
        self.index = 0
        self.inflight: Optional[str] = None
        self.stop_reason: Optional[str] = None
        self.governor_probes: list = []
        # Handed to the thread-job table by reference, so get_job() sees the ladder
        # advance leg by leg instead of one opaque "running".
        self.progress: dict = {
            "legs_total": len(plan),
            "legs_done": 0,
            "sequential": True,
            "leg_job_ids": self.job_ids,
        }

    def pump(self, deadline: Optional[float]) -> Optional[dict]:
        """Solve the remaining legs IN ORDER; the envelope, or None at the deadline."""
        while self.index < len(self.plan):
            step, payload, meta = self.plan[self.index]
            if self.stop_reason is not None:
                self._record(_convergence_skipped_row(step, self.stop_reason))
                continue
            if self.inflight is None:
                if self.governor_probes:
                    payload["params"]["study"]["governor_probes"] = self.governor_probes
                self.inflight = simjobs.start(
                    "fea_static", "core_rpc", payload, meta
                )["job_id"]
                self.job_ids.append(self.inflight)
                self.progress["current_leg"] = self.index
                self.progress["current_job_id"] = self.inflight
            _jid, out = _convergence_leg_solve(payload, meta, deadline, self.inflight)
            if out is None:
                return None  # this leg is still solving; the ladder resumes in background
            row = _convergence_row(step, out, self.monitor, self.prev)
            governor = (out.get("provenance") or {}).get("governor") or {}
            probes = governor.get("probes")
            if isinstance(probes, list) and probes:
                self.governor_probes = list(probes)
                # The delivered mesh may not be the last attempted (refused) probe.
                # It, not a rejected finer mesh, anchors the following leg.
                delivered = governor.get("measured_probe")
                if isinstance(delivered, dict):
                    self.governor_probes.append(delivered)
            if row.get("error") and not self.continue_on_fail:
                self.stop_reason = (
                    f"not solved: leg {self.index} ({_leg_label(step)}) failed and stopped "
                    f"the ladder — {row['error']}. A finer leg means nothing without the "
                    "coarser one it is compared against, and its mesh is the more "
                    "expensive of the two. Pass continue_on_fail=True to solve the "
                    "remaining legs anyway."
                )
            # An implausible leg is not a reference either: the next leg's delta has to
            # be measured against the last leg that actually solved the elastic problem.
            if isinstance(row.get("monitored_value"), (int, float)) and not row.get(
                "implausible"
            ):
                self.prev = float(row["monitored_value"])
            self.inflight = None
            self._record(row)
        return self.envelope()

    def _record(self, row: dict) -> None:
        self.rows.append(row)
        self.index += 1
        self.progress["legs_done"] = self.index

    def envelope(self) -> dict:
        """The finished ladder's verdict, with the leg jobs that produced it."""
        out = _convergence_envelope(
            self.doc_id, self.monitor, self.tol_pct, self.rows, self.notes
        )
        out["job_ids"] = list(self.job_ids)
        return out


def _convergence_handoff(run: _ConvergenceLadder) -> dict:
    """Reply for a ladder that finishes in the background: ONE job, legs still in order.

    The legs stay durable child processes (cancellable, restart-surviving), but they are
    STARTED by this one driver job, so at most one of them exists at a time. The old
    shape queued every leg at once and returned N job ids — which is exactly how a
    three-leg study became three simultaneous solves competing for one memory budget.
    """
    # Snapshot what is already finished BEFORE the driver thread exists: once it runs it
    # mutates `rows`/`job_ids` under us, and a reply whose leg list depends on thread
    # timing is a reply the caller cannot reason about. The LIVE view is `progress`,
    # which get_job reads by reference.
    done = len(run.rows)
    leg_ids = list(run.job_ids)
    completed = list(run.rows)
    resources = _resources_line(run.rows)
    jid = _start_job("fea_convergence", lambda: run.pump(None), run.progress)
    lead = (
        f"the first {done} of {len(run.plan)} legs finished inside the call budget; the "
        "rest continue in ONE background job"
        if done
        else f"the whole {len(run.plan)}-leg ladder runs in ONE background job"
    )
    return {
        "ok": True,
        "doc_id": run.doc_id,
        "state": "running",
        "job_id": jid,
        "monitor": run.monitor,
        "tol_pct": run.tol_pct,
        "n_legs": len(run.plan),
        "legs_done": done,
        "sequential": True,
        "leg_job_ids": leg_ids,
        "completed_legs": completed,
        "cancel_supported": True,
        "resources": resources,
        "note": (
            f"{lead}. The legs solve STRICTLY ONE AT A TIME, coarse to fine — each is its "
            "own durable child process, but the next one is not started until the current "
            f"one is done. Poll get_job('{jid}'): while it runs, `progress` carries "
            "legs_done / current_job_id / leg_job_ids; when it is done, `result` is the "
            f"WHOLE convergence table (sim_results('{jid}') returns the same table). "
            "sim_cancel kills a leg, not this driver: pass it the leg id in "
            "progress.current_job_id, and cancelling that leg stops the ladder. If this "
            "server restarts mid-ladder the driver is gone but the legs are not: "
            "sim_jobs() lists them and run_fea_convergence(job_ids=[...]) rebuilds the "
            "table from the ones that finished. Do not start another study while this "
            f"runs: {_RESOURCES_NOTE}"
        ),
        "next_actions": [f"get_job('{jid}')"],
    }


def _convergence_from_jobs(job_ids, monitor: str, tol_pct: float) -> dict:
    """Assemble the table from ladder legs that are already finished.

    The normal path is `get_job(<ladder job id>)`, which returns the table the sequential
    driver assembled. This is the recovery path: the leg ids are durable (they outlive the
    driver, and a restarted server can still collect them), so a ladder whose driver died
    can be reassembled from `progress.leg_job_ids`.

    Each leg is an ordinary durable `fea_static` job, so it collects through exactly the
    same COLLECT stage (and writes exactly one study into the document) as a synchronous
    leg. The ladder position rides in the job's own metadata, so the table cannot be
    mislabelled by the caller re-ordering the ids.
    """
    ids = require_list(
        job_ids, "job_ids", "the list of job ids run_fea_convergence(background=True) returned"
    )
    if len(ids) < 2:
        raise ValueError(
            "a convergence study needs at least 2 legs; pass every job id the "
            "background call returned."
        )
    legs: list = []
    pending: list = []
    doc_id = None
    for jid in ids:
        st = simjobs.status(str(jid))
        meta = st.get("meta") or {}
        step = meta.get("convergence_step")
        if not isinstance(step, dict):
            raise ValueError(
                f"job {jid!r} is not a run_fea_convergence leg (no convergence_step "
                "metadata); it cannot be placed on the ladder."
            )
        doc_id = doc_id or meta.get("doc")
        if st.get("state") not in simjobs.TERMINAL:
            pending.append({"job_id": jid, "state": st.get("state"), **step})
            continue
        if st.get("state") == simjobs.DONE:
            out = collect_durable(str(jid), st)
        else:
            out = {
                **(simjobs.refusal_result(str(jid)) or {}),
                "ok": False,
                "error": st.get("error") or f"solve {st.get('state')}",
            }
        legs.append((step, out))
    if pending:
        return {
            "ok": False,
            "doc_id": doc_id,
            "reason": "convergence_legs_pending",
            "pending": pending,
            "error": (
                f"{len(pending)} of {len(ids)} legs have not finished; a partial ladder is "
                "not a convergence statement. Poll sim_status(job_id) and call again."
            ),
        }
    legs.sort(key=lambda pair: int(pair[0].get("index", 0)))
    rows: list = []
    prev: Optional[float] = None
    for step, out in legs:
        row = _convergence_row(step, out, monitor, prev)
        if isinstance(row.get("monitored_value"), (int, float)) and not row.get(
            "implausible"
        ):
            prev = float(row["monitored_value"])
        rows.append(row)
    return _convergence_envelope(doc_id, monitor, tol_pct, rows, [])


def _backend_missing(exc: Exception, method: str) -> bool:
    """True when a job refused to start because the C++ solver isn't in this build.

    `jobs.start` probes the backend before it queues anything, so an absent solver surfaces
    as a start-time refusal rather than a job that can only fail — but it has to be told
    apart from a genuinely bad study, which is the same exception type.
    """
    msg = str(exc).lower()
    if "backend is unavailable" in msg:
        return True
    return method in msg and ("cannot dispatch" in msg or "unknown method" in msg)


def register_cad_sim_tools(mcp: FastMCP) -> None:
    """CAD simulation MCP tools (split out of cad_tools.py): run_fea_* + run_cfd_*."""

    # ---------------------------------------------------------------- simulation

    @mcp.tool()
    def run_fea_static(
        fixtures: Union[list, str],
        loads: Union[list, str],
        material: Union[str, dict] = "steel",
        mesh_fineness: Union[float, str] = _DEFAULT_FINENESS,
        mesh_size_mm: Optional[float] = None,
        min_size_mm: Optional[float] = None,
        plasticity: Optional[dict] = None,
        learned_advisor: bool = False,
        advisor_max_dof: int = 0,
        doc_id: Optional[str] = None,
        vtu_path: Optional[str] = None,
        export_system_path: Optional[str] = None,
        background: bool = False,
        interfaces: Optional[dict] = None,
        bond_gap_mm: Optional[float] = None,
    ) -> str:
        """Linear-static stress FEA on the current part. Returns max stress, displacement, and FoS.

        fixtures : list of face queries held fixed, e.g. ["bottom"] or
                   [{"of":"face","side":"bottom","pick":"largest"}].
        loads    : list of {face query + a force or pressure}, e.g.
                   [{"face":"top","force":[0,0,-500]}]   # newtons, world XYZ
                   [{"face":"top","pressure":0.5}]       # MPa, inward: +p PUSHES INTO the
                   #   face, so +p on a bore is INTERNAL pressure; use -p for external
                   [{"face":"top","magnitude":500,"direction":[0,0,-1]}]  # polar form of the
                   #   force above — direction is normalized, so its length never scales the load
                   Plural "faces":[q1,q2] is accepted and applies the SAME load to each face.
                   A face query is query_geometry vocabulary ("top", {"of":"face",...}) or a face
                   SID from describe_part / query_geometry ("F58db6d") — a FEATURE id is not a face.
        material : a name ("steel","aluminum","titanium","stainless","brass","abs","pla") or an
                   explicit dict {E_mpa, nu, density_g_cm3, yield_mpa, ...}.
                   Or {per_body: {"1": name_or_dict, ...}, default: name_or_dict} for
                   heterogeneous bodies; ordinals are one-based STEP solid ordinals.
                   default is optional when every body has an explicit assignment.
        interfaces : optional body-pair policy object with exactly these optional keys:
                   {"bonded":[[a,b],...], "contact":[[a,b],...],
                    "default":"contact"|"bonded"|"free"}.
                   Each a/b is a positive 1-based STEP solid ordinal or a body-name string.
                   Explicit pairs override default; omitted default is "free" here.
                   Names resolve through the exported solid roster; unknown, ambiguous,
                   or multi-solid names are refused with an explicit ordinal remedy.
                   The engine validates mesh identity and refuses pairs in both lists.
                   Use bonded pairs for welded/glued or idealized press-fit joints.
                   For mixed press-fit + clearance joints, use run_fea_contact with
                   bonded and contact lists so clearance joints can open and close.
        bond_gap_mm : optional nonnegative finite bonded-search limit in mm. With
                   interfaces, this limits only bonded pairs (zero means touching);
                   when omitted, bonded pairs use the measured gap within max_gap_mm.
                   Without interfaces a positive value enables legacy all-nearby bonding.
        mesh_fineness : 0..1 (default 0.5), or a preset word
                   ("coarse"=0.25, "medium"=0.5, "fine"=0.75, "finest"=1.0).
                   Second-order (tet10) elements are used for accuracy.
        mesh_size_mm : the EXACT characteristic element size in mm, e.g. 6.0 — mutually
                   exclusive with mesh_fineness (passing both is refused: they are two
                   different meshes). It maps straight to study.target_size_mm and
                   bypasses the mesh-fineness governor, so it is the release-grade
                   spelling and the ONLY way to re-solve one run_fea_convergence leg on
                   the mesh that produced its number — pass the leg's size_mm together
                   with vtu_path to write that leg's field.
        min_size_mm : the element-size band FLOOR in mm — the smallest element the
                   mesher may descend to. Raise the resolution of a SMALL FEATURE
                   without restating the global size: when a run is refused with
                   "polymesh needs an element size of X mm ... below this study's
                   element-size band floor of Y mm", pass min_size_mm just under X
                   (0.5 for a 0.78 mm feature) and the mesher's feature-resolution
                   descent can reach it. It may not exceed mesh_size_mm — a floor above
                   the stated size would coarsen the mesh instead of refining it — and
                   it costs elements and memory, so pair it with a COARSE global size.
        plasticity : opt-in SMALL-STRAIN J2 (von Mises) ELASTIC-PLASTIC material, as
                   {yield_mpa, hardening_mpa, max_increments, tol}. Omitted = the
                   linear-elastic solve this tool has always done.
                     yield_mpa      initial yield (MPa); omitted uses material.yield_mpa
                     hardening_mpa  post-yield TANGENT MODULUS in MPa — NOT a ratio of
                                    E. 0 = perfectly plastic (a flat plateau, so the
                                    part has no reserve past its plastic hinge);
                                    structural steel is about 1000-2000 MPa
                     max_increments load steps over [first yield, full load], default
                                    10; a non-converging step is halved automatically
                     tol            relative residual tolerance, default 1e-3
                   WHAT IT IS FOR: a linear-elastic solve cannot redistribute. Where a
                   sharp edge or a small bearing patch takes the load, the elastic
                   answer funnels all of it through however many nodes the mesh put
                   there and reports a stress the material could never carry (the case
                   this exists for: 1446 N through 12 node pairs on a hinge saddle
                   edge, 450 MPa mean against a 276 MPa yield). With the law on, that
                   edge YIELDS, the load spreads until it fits inside the flow stress,
                   and summary.max_von_mises_mpa comes back capped at
                   yield + hardening_mpa * plasticity.max_equivalent_plastic_strain.
                   Reach for it when an elastic peak sits well above yield in a small
                   region and you need to know whether the part survives by yielding
                   locally rather than what the singular peak says.
                   Returns a top-level "plasticity" block: {enabled, yield_mpa,
                   hardening_mpa, tol, requested_increments, increments,
                   newton_iterations_total, converged, load_fraction,
                   max_equivalent_plastic_strain, plastic_volume_mm3,
                   plastic_fraction, yielded_nodes}, plus the nodal field
                   "equivalent_plastic_strain" (also written into any vtu_path).
                   REFUSALS, both of them physics and not preference: a load past the
                   part's LIMIT LOAD is refused as a collapse mechanism naming the
                   load_fraction it did carry (there is no equilibrium to report, and a
                   last Newton iterate is not a stress state); and a peak plastic
                   strain over 5 % raises a "warnings" entry, because the mesh is never
                   updated and past a few percent the geometry itself has changed.
                   Not accepted by run_fea_modal / run_fea_buckling / run_fea_thermal /
                   the fatigue tools, and not combinable with an adaptive run.
        learned_advisor : opt-in Polyhedral-FEA sizing/order advice; false by default.
        advisor_max_dof : optional non-negative advisor budget; 0 leaves it unset.
        background : if True, return a job_id immediately and solve in a separate process —
                     poll get_job(job_id). Use it for fine meshes that may exceed the call
                     timeout. The job is persisted on disk, so it survives an MCP restart
                     and can be collected in a later turn; sim_cancel(job_id) kills it.
        vtu_path : optional path for a VTK XML UnstructuredGrid (.vtu, ASCII) the
                   solver writes after the solve — point data displacement (mm, 3-vector),
                   von_mises (MPa) and summary_node (the 1/0 mask that selected the
                   reported peak, see below) on the solved tet4/tet10 mesh, for ParaView
                   or an independent cross-check. A relative path is project-root
                   relative. Echoed back as the absolute "vtu_path"; a write
                   failure fails the whole run rather than reporting a partial success.
        export_system_path : optional path for a JSON dump of the RESOLVED
                   system, written immediately before the solve: {points [[x,y,z] mm],
                   elems [[node ids]], nodes_per_elem, fixed_nodes [flat node-index list,
                   each pinning all 3 DOFs], nodal_forces [[fx,fy,fz] N per node],
                   E_mpa, nu, bond_springs, contact_springs}. For an independent
                   cross-solve (CalculiX etc.); a write failure fails the run. A relative
                   path is project-root relative.

        Returns {ok, doc_id, material, n_nodes, n_elems, mesh, summary:{...}} — the
        scalars live under "summary", NOT at the top level: summary.min_fos,
        summary.max_von_mises_mpa, summary.max_disp_mm (plus tresca/rankine/principal
        variants). min_fos = yield / max stress; > 1 holds. min_fos saturates at a
        15.0 cap, reported in "fos_note" — a capped value means "at least this", not
        "exactly this".

        summary.max_von_mises_mpa IS NOT THE FIELD'S PEAK. Stress at an ideal fully
        fixed constraint is a discretization singularity, so the summary reduction
        skips every node within two element rings of a fixed node; the same mask sets
        min_fos and the principal/shear extrema. That decision is reported, never
        implied:
          summary.max_von_mises_reduction  "node_averaged_excluding_fixture_ring2",
                                           or "node_averaged" when nothing was excluded
          summary.max_von_mises_raw_mpa    the same reduction over ALL nodes
          summary.min_fos_raw              yield / that raw peak
          summary.summary_nodes_excluded / summary_nodes_total   how many nodes it dropped
          summary.reduction_masked_keys / reduction_unmasked_keys which scalars in this
                                           object share the mask — the Tresca/Rankine
                                           pair does NOT, so max_tresca_mpa can exceed
                                           max_von_mises_mpa by the singular factor
        Quote max_von_mises_mpa (and the FoS from it) only WITH the reduction named; if
        your fixture faces ARE the feature of interest — a bolt hole, a bearing seat —
        the peak you care about is inside the excluded ring and max_von_mises_raw_mpa is
        the number to look at, mesh-sensitivity caveat and all. The requested .vtu
        carries von_mises with summary_node beside it, so both numbers are re-derivable
        from the file: mask==1 gives max_von_mises_mpa, the whole array gives
        max_von_mises_raw_mpa.

        Release evidence rides BESIDE "summary": "mesh_quality" (aspect/jacobian extremes,
        pct_poor, node/elem counts, order, char_len_mm — the element size the mesher
        DELIVERED, which is not the size you asked for whenever the memory governor
        resized against its element budget — plus connected_components /
        unfixed_components and thin_wall_ratio — the wall thickness in element sizes,
        under 1 meaning one element spans the wall), "reactions" (applied vs reaction
        force/moment plus equilibrium_error_pct — check it before trusting a stress),
        "plausibility" ({max_disp_over_bbox_diag, bbox_diag_mm, flag}) and "provenance"
        (engine_build_id, polymesh_commit, solver, mesher, timestamp_utc).
        A mesh whose components do not ALL reach a fixture is REFUSED, not solved: an
        island free to translate and rotate has no unique answer, and the equilibrium
        residual cannot see it (a mechanism carries no load, so the residual is tiny
        BECAUSE the model is broken). When plausibility.flag is "implausible" the peak
        displacement exceeded the part's own bounding-box diagonal — the small-strain
        assumption is broken, "warnings" says so at the top level, and the numbers are
        diagnostics rather than a deflection.
        Tip: query_geometry(...) first to confirm your fixture/load faces resolve.
        """
        try:
            bc_notes: list = []
            ctx = _build_static_study(
                fixtures,
                loads,
                material,
                mesh_fineness,
                learned_advisor,
                advisor_max_dof,
                doc_id,
                bc_notes,
                vtu_path=vtu_path,
                export_system_path=export_system_path,
                target_size_mm=mesh_size_mm,
                min_size_mm=min_size_mm,
                plasticity=plasticity,
                interfaces=interfaces,
                bond_gap_mm=bond_gap_mm,
            )
            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            return _dumps(
                _run_solve(
                    "fea_static",
                    "core_rpc",
                    {"method": "sim_run_static", "params": {"study": ctx["study"]}},
                    _job_meta(ctx, bc_notes),
                    background=background,
                    doc_id=ctx["doc"],
                )
            )
        except _StudyRefusal as r:
            return _dumps(r.payload)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def run_fea_adaptive(
        fixtures: Union[list, str],
        loads: Union[list, str],
        material: Union[str, dict] = "steel",
        mesh_fineness: Union[float, str] = 0.5,
        max_passes: int = 3,
        eta_target: float = 0.05,
        learned_advisor: bool = False,
        advisor_max_dof: int = 0,
        doc_id: Optional[str] = None,
        vtu_path: Optional[str] = None,
        background: bool = False,
    ) -> str:
        """Adaptive linear-static stress FEA for decision-driving peak stresses.

        It re-meshes and re-solves up to max_passes times, so it is several times slower
        than run_fea_static. Prefer it when a peak stress drives a decision, or when a
        coarse result looks mesher-sensitive. Its dimensionless relative error indicator
        is smaller when the discretization is better; it does not bound stress error.

        fixtures : list of face queries held fixed, in the run_fea_static dialect.
        loads : list of face queries plus force (N) or pressure (MPa), as for run_fea_static.
        material : a registry name or explicit material property dict.
        mesh_fineness : starting fineness from 0..1 or coarse/medium/fine/finest.
        max_passes : maximum remesh-and-solve passes, from 1 through 6.
        eta_target : finite positive target for the dimensionless relative error indicator.
        learned_advisor : opt in to upstream PolyMesh mesh advice; false by default.
        advisor_max_dof : optional non-negative advisor budget; 0 leaves it unset.
        background : if True, return a job_id immediately; poll get_job(job_id). The job
                     runs in its own process and survives an MCP restart.
        vtu_path : optional .vtu path for the FINAL adapted mesh (ASCII VTK XML
                   UnstructuredGrid; point data displacement in mm, von_mises in MPa and
                   the summary_node 1/0 reduction mask). A relative path is project-root
                   relative.
        The reply carries "mesh_quality", "reactions" (with equilibrium_error_pct) and
        "provenance" beside "summary", as run_fea_static does — including the reduction
        disclosure (summary.max_von_mises_reduction / max_von_mises_raw_mpa /
        min_fos_raw): each pass's peak is the fixture-ring-masked one, so an adaptive
        study that refines toward a fixture never reports what it is refining.
        """
        try:
            bc_notes: list = []
            ctx = _build_adaptive_study(
                fixtures,
                loads,
                material,
                mesh_fineness,
                max_passes,
                eta_target,
                learned_advisor,
                advisor_max_dof,
                doc_id,
                bc_notes,
                vtu_path=vtu_path,
            )
            _autosave_all_open()
            return _dumps(
                _run_solve(
                    "fea_adaptive",
                    "core_rpc",
                    {"method": "sim_run_adaptive", "params": {"study": ctx["study"]}},
                    _job_meta(ctx, bc_notes),
                    background=background,
                    doc_id=ctx["doc"],
                )
            )
        except _StudyRefusal as r:
            return _dumps(r.payload)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def run_fea_convergence(
        fixtures: Optional[Union[list, str]] = None,
        loads: Optional[Union[list, str]] = None,
        material: Union[str, dict] = "steel",
        sizes_mm: Optional[Union[list, str]] = None,
        fineness: Optional[Union[list, str]] = None,
        min_size_mm: Optional[float] = None,
        mesh_order: int = 2,
        monitor: Optional[str] = None,
        tol_pct: float = _CONVERGENCE_TOL_PCT,
        doc_id: Optional[str] = None,
        vtu_dir: Optional[str] = None,
        background: bool = False,
        continue_on_fail: bool = False,
        job_ids: Optional[Union[list, str]] = None,
    ) -> str:
        """Mesh-convergence study: ONE static setup solved at N stated discretizations.

        This is the release gate a single mesh cannot pass — a coarse mesh under-reports
        peak stress, which reads as a passing factor of safety. State the ladder BEFORE
        you solve, then report the table.

        fixtures / loads / material : exactly the run_fea_static dialect. The setup is
                   resolved ONCE (one STEP export, one set of fixture/load point sets) and
                   reused by every leg, so the legs differ only in discretization.
        sizes_mm : coarse-to-fine characteristic element sizes in mm, strictly decreasing,
                   e.g. [4, 2, 1]. This is the runbook spelling (`-h` in the polymesh CLI)
                   and the RELEASE-GRADE one: each entry maps straight to
                   study.target_size_mm and bypasses the mesh-fineness governor.
        fineness : the alternative spelling — a strictly increasing list on the 0..1
                   mesh_fineness scale, e.g. [0.25, 0.5, 0.75] or ["coarse","medium","fine"].
                   A fineness ladder is SUBJECT TO the mesh-size governor
                   (core/sim/mesh_size_band), which clamps the derived size against an
                   element budget for interactivity: on a large part several rungs can
                   collapse onto one identical mesh, and identical meshes cannot converge.
                   Prefer sizes_mm for anything you intend to release.
                   Pass exactly one of sizes_mm / fineness; at least 2 entries either way.
        min_size_mm : the element-size band FLOOR in mm, shared by EVERY leg — the
                   smallest element the mesher may descend to. A leg refused with
                   "polymesh needs an element size of X mm ... below this study's
                   element-size band floor of Y mm" cannot resolve a feature the
                   ladder's own sizes put out of reach (the floor is half the leg's
                   size): pass min_size_mm just under X (0.5 for a 0.78 mm feature) and
                   every leg can reach it. It may not exceed the FINEST sizes_mm entry —
                   a floor above a stated size would coarsen that leg instead of
                   refining it — and it costs elements and memory on every leg, so keep
                   the ladder's sizes coarse.
        mesh_order : 1 (tet4) or 2 (tet10, default). Keep it constant across the ladder —
                   changing element order mid-study is a different discretization family.
        monitor : which `summary` scalar the convergence is judged on; default
                   "max_von_mises_mpa". Also accepted: max_disp_mm, min_fos,
                   max_tresca_mpa, max_rankine_mpa, max_principal_mpa, min_principal_mpa.
                   Monitor a NON-singular location's driver: a re-entrant corner peak
                   diverges by construction and will never settle. Note which of these
                   the fixture-ring mask applies to (run_fea_static: masked =
                   max_von_mises_mpa / min_fos / the principal pair; UNMASKED =
                   max_disp_mm and the tresca/rankine pair) — a ladder that monitors a
                   masked scalar is converging on a number the .vtu's peak will exceed,
                   and each leg's summary names its own reduction.
        tol_pct : convergence tolerance in percent of the previous leg's monitored value
                   (default 5).
        background : if True, return immediately with the id of ONE background job that
                   solves the ladder; poll get_job(job_id) and its `result` is the whole
                   table. A synchronous call that overruns the transport budget switches
                   to exactly this shape by itself, so a long ladder is never lost.
        continue_on_fail : by default a failed leg STOPS the ladder and every finer leg is
                   marked skipped ({"skipped": true}) with the reason — a finer leg is
                   meaningless without the coarser one it is compared against, and its
                   mesh is the more expensive of the two. Pass True to solve the rest
                   anyway (e.g. when the coarse leg failed for a setup reason you have
                   already ruled out for the finer ones).
        job_ids : RECOVERY path — assemble the table from legs already finished (ladder
                   positions come from the jobs' own metadata, so the order you pass them
                   in does not matter). The leg ids are in the ladder job's
                   progress.leg_job_ids. fixtures/loads are NOT needed on this call; they
                   are required only when starting a new ladder.
        vtu_dir : optional directory the legs write their FIELDS into (a relative path is
                   project-root relative) — one .vtu
                   per leg, named <doc>_leg<k>_h<size>_o<order>.vtu (a fineness rung is
                   named f<fineness> instead). Each row then carries `vtu_path` and the
                   reply's `released_leg` names the finest solved leg's file, which is the
                   one a release quotes and the one sim_evidence_packet hashes: without
                   this a converged ladder has a table and no field behind it, and the
                   only recovery was a fourth solve. The write happens INSIDE the leg
                   (still strictly one leg at a time, so peak memory is unchanged), and a
                   .vtu already at a leg's path is ARCHIVED into
                   superseded_<date>_<hash>/ rather than overwritten — same rule as
                   run_fea_static's vtu_path. The engine creates the directory.

        THE LEGS ARE SEQUENTIAL and your studies should be too: the engine's memory
        governor grants each solve a fraction of the RAM AVAILABLE when it starts (not a
        fixed share of total RAM), so concurrent studies shrink each other's budget until
        the fine meshes are refused outright. Run ONE ladder at a time; each leg row
        carries the governor's own numbers in `memory_budget` when the engine reported
        them, and the reply's `resources` line states the rule.

        Returns {ok, monitor, tol_pct, converged, degenerate_ladder, size_delivery,
        final_delta_pct, n_meshes, resources, job_ids, table:[{size_mm|fineness,
        delivered_size_mm, mesh_order, n_dof,
        n_nodes, n_elems, max_von_mises_mpa, max_disp_mm, min_fos, monitored_value,
        delta_pct_vs_prev, mesh_quality, equilibrium_error_pct, plausibility,
        implausible, mesh_governor_notes, memory_budget}]}. `converged` is |the FINEST
        leg's delta| < tol_pct. A failed or skipped leg keeps its row and forces
        ok:false — a table missing its finest mesh would read as "converged at the
        coarse end".
        `released_leg` names the leg a release quotes — the FINEST one that actually
        solved — with its index, size/fineness, mesh_order, n_dof, monitored_value and
        (when `vtu_dir` was given) its `vtu_path`. It is not a verdict: it exists even on
        a ladder that did not converge, and `converged` is still the statement to read.
        Hand the whole reply to sim_evidence_packet, which hashes that leg's field.
        `degenerate_ladder:true` means two legs meshed the same (identical n_dof/n_elems,
        or under 5% DOF growth between consecutive legs); converged is then forced false
        and `note` carries the remedy, because a 0% delta between two copies of one mesh
        is not evidence.
        `size_mm` is the size you REQUESTED and `delivered_size_mm` is the size the mesher
        actually meshed at (engine mesh_quality.char_len_mm) — the memory governor sizes
        against an element budget, so a ladder asked for 1.6/1.2/0.9 mm can be delivered
        at 5.62/5.43/5.42 mm. `size_delivery` is that comparison per solved leg
        ({leg, requested_mm, delivered_mm, ratio}); when any ratio leaves 0.8x-1.25x of the
        request, `note` names those legs. The .vtu filenames carry the REQUESTED size, so
        `delivered_size_mm` is the only truth about what was meshed.
        A leg whose peak displacement exceeded the part's own bounding-box diagonal is
        marked `implausible:true` and listed in `implausible_legs`: it left the
        linear-elastic small-strain regime (usually a mechanism — a thin wall meshed one
        element thick), so it carries no delta, never becomes the reference for the next
        leg, and forces converged false.
        """
        try:
            tol_pct = _finite_float(tol_pct, "tol_pct")
            if tol_pct <= 0.0:
                raise ValueError("tol_pct must be > 0.")
            if monitor is None:
                monitor = "max_von_mises_mpa"
            if monitor not in _MONITORABLE:
                raise ValueError(
                    f"monitor={monitor!r} is not a convergence-monitorable summary "
                    f"scalar; choose one of {list(_MONITORABLE)}."
                )
            vtu_dir = _norm_abs_out_path(vtu_dir, "vtu_dir")
            if job_ids is not None:
                if vtu_dir:
                    raise ValueError(
                        "vtu_dir cannot be honoured on the job_ids recovery path: the "
                        "field is written BY the solve, and those legs have already run. "
                        "A leg started with vtu_dir already carries its vtu_path on its "
                        "row; otherwise re-solve just the leg you intend to release with "
                        "run_fea_static(mesh_size_mm=<that row's size_mm>, vtu_path=...)."
                    )
                return _dumps(_convergence_from_jobs(job_ids, monitor, tol_pct))
            if fixtures is None or loads is None:
                missing = [
                    n for n, v in (("fixtures", fixtures), ("loads", loads)) if v is None
                ]
                raise ValueError(
                    f"{' and '.join(missing)} required to START a convergence ladder. "
                    "Pass the run_fea_static dialect, or pass job_ids=[...] to assemble "
                    "the table from legs that are already running."
                )
            if isinstance(mesh_order, bool) or mesh_order not in (1, 2):
                raise ValueError("mesh_order must be 1 (tet4) or 2 (tet10).")
            key, ladder = _convergence_ladder(sizes_mm, fineness)
            # The floor is shared by the whole ladder, so it is checked against the
            # FINEST stated leg: a floor above any leg's own size would coarsen that leg.
            min_size_mm = _norm_min_size_mm(
                min_size_mm,
                min(ladder) if key == "target_size_mm" else None,
                "the finest sizes_mm leg",
            )
            bc_notes: list = []
            # The base context resolves the setup; the first leg's own fineness overwrites
            # the placeholder below, so nothing is solved at an unstated discretization.
            ctx = _build_static_study(
                fixtures,
                loads,
                material,
                ladder[0] if key == "fineness" else 0.5,
                False,
                0,
                doc_id,
                bc_notes,
                min_size_mm=min_size_mm,
            )
            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            plan: list = []
            for i, value in enumerate(ladder):
                study = {**ctx["study"], "mesh_order": mesh_order, key: value}
                step = {
                    "index": i,
                    "mesh_order": mesh_order,
                    "target_size_mm": value if key == "target_size_mm" else None,
                    "min_size_mm": min_size_mm,
                    "fineness": study["fineness"],
                }
                # The leg's own discretization, so the study this leg persists names the
                # mesh it was solved on rather than the base context's placeholder.
                leg_ctx = {
                    **ctx,
                    "study": study,
                    "target_size_mm": step["target_size_mm"],
                }
                if vtu_dir:
                    # The setup is already validated and exported at this point, so a
                    # field left by an earlier run may now be moved aside. The WRITE
                    # itself happens inside the leg's own solve, which is still the only
                    # thing running: peak memory is unchanged by asking for a field.
                    leg_vtu = _leg_vtu_path(vtu_dir, ctx["doc"], i, step)
                    _archive_existing_vtu(leg_vtu, bc_notes)
                    study["vtu_path"] = leg_vtu
                    step["vtu_path"] = leg_vtu
                    leg_ctx["vtu_path"] = leg_vtu
                plan.append(
                    (
                        step,
                        {"method": "sim_run_static", "params": {"study": study}},
                        _job_meta(leg_ctx, bc_notes, {"convergence_step": step}),
                    )
                )
            run = _ConvergenceLadder(
                plan, ctx["doc"], monitor, tol_pct, bc_notes, bool(continue_on_fail)
            )
            if background:
                return _dumps(_convergence_handoff(run))
            out = run.pump(time.time() + _SYNC_SOLVE_BUDGET_S)
            if out is None:
                # The ladder outran the transport budget in the middle of a leg. The REST
                # of it still has to run one leg at a time, so the same driver finishes it
                # on a background thread instead of queueing every remaining leg at once.
                return _dumps(_convergence_handoff(run))
            return _dumps(out)
        except _StudyRefusal as r:
            return _dumps(r.payload)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def run_fea_modal(
        fixtures: Union[list, str],
        material: Union[str, dict] = "steel",
        n_modes: int = 6,
        mesh_fineness: Union[float, str] = 0.5,
        learned_advisor: bool = False,
        advisor_max_dof: int = 0,
        doc_id: Optional[str] = None,
        vtu_path: Optional[str] = None,
        background: bool = False,
    ) -> str:
        """Modal FEA — the natural frequencies (Hz) of the held part. Returns the first n_modes.

        fixtures : list of face queries held fixed (e.g. ["bottom"]). An unconstrained part has
                   rigid-body modes near 0 Hz, so fix it like it is mounted. A query is
                   query_geometry vocabulary, a face SID from describe_part ("F58db6d"), or a
                   wrapper {"face": q} / {"faces": [q1, q2]}.
        material : name or explicit dict (density drives the mode frequencies).
        mesh_fineness : 0..1, or a preset word ("coarse"/"medium"/"fine"/"finest").
        learned_advisor : opt-in Polyhedral-FEA mesh advice. It does not promote the
                          static-trained model as a modal optimizer.
        advisor_max_dof : optional non-negative advisor budget; 0 leaves it unset.
        background : if True, return a job_id and solve in a separate process — poll
                     get_job(job_id). The job survives an MCP restart.
        vtu_path : optional .vtu path (a relative path is project-root relative); the
                   modal write carries the mode shapes as
                   point data. A modal reply has "mesh_quality" and "provenance" but no
                   "reactions" — there is no applied load to balance.
        """
        try:
            n_modes = _positive_int(n_modes, "n_modes")
            bc_notes: list = []
            ctx = _build_modal_study(
                fixtures,
                material,
                mesh_fineness,
                learned_advisor,
                advisor_max_dof,
                doc_id,
                bc_notes,
                vtu_path=vtu_path,
            )
            ctx["n_modes"] = n_modes
            # Free-free (no fixtures): the 0 Hz rigid-body cluster is 6-fold degenerate.
            # FIXED IN THE ENGINE 2026-07-12 (fea_modal.cpp over-requests + widens the
            # Krylov subspace + verifies the rigid count internally), so the over-request
            # and the re-solve below it are a redundant SAFETY NET — kept because they are
            # cheap and turn any regression into a visible warning note instead of silently
            # wrong mode numbering. Both live in the SOLVE stage (jobs._stage_modal_rpc) so
            # the synchronous and background answers cannot drift apart.
            free_free = ctx["free_free"]
            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            return _dumps(
                _run_solve(
                    "fea_modal",
                    "modal_rpc",
                    {
                        "study": ctx["study"],
                        "solve_n": _modal_solve_n(n_modes, free_free),
                        "widen_free_free": free_free,
                    },
                    _job_meta(ctx, bc_notes),
                    background=background,
                    doc_id=ctx["doc"],
                )
            )
        except _StudyRefusal as r:
            return _dumps(r.payload)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ------------------------------------------------ asynchronous solves (sim_* job family)
    # The synchronous tools above start the SAME durable jobs; these four are the explicit
    # non-blocking contract: start -> poll -> collect, with a cancel that really kills the
    # solver, and results that are never presented as converged unless they are.

    @mcp.tool()
    def sim_run(
        analysis: str = "fea_static",
        fixtures: Optional[Union[list, str]] = None,
        loads: Optional[Union[list, str]] = None,
        material: Union[str, dict] = "steel",
        mesh_fineness: Union[float, str] = 0.5,
        learned_advisor: bool = False,
        advisor_max_dof: int = 0,
        n_modes: int = 6,
        doc_id: Optional[str] = None,
        study: Optional[str] = None,
        label: Optional[str] = None,
    ) -> str:
        """Start an FEA solve in a SEPARATE PROCESS and return a job_id immediately.

        Use this instead of run_fea_static/run_fea_modal when you do not want to sit inside
        the solve: the call returns in milliseconds, you keep working, and you collect the
        answer later — including in a LATER TURN, because the job registry is persisted on
        disk (a job outlives the process that started it).

        analysis : "fea_static" (fixtures + loads) or "fea_modal" (fixtures; [] = free-free).
                   `study=` is accepted as the same argument.
        Every other argument means exactly what it means on run_fea_static / run_fea_modal,
        and the study is built by the SAME setup code, so the answer is identical to the
        synchronous one.
        learned_advisor : default-off Polyhedral-FEA sizing/order advice.
        advisor_max_dof : optional non-negative advisor budget; 0 leaves it unset.

        Then: sim_status(job_id) -> queued|running|done|failed|cancelled (+ progress, elapsed)
              sim_results(job_id) -> the full result once done (partial=True while running)
              sim_cancel(job_id)  -> kills the solver process and PROVES it is gone
        """
        try:
            kind = str(study or analysis or "").strip().lower().replace("-", "_")
            if kind in ("static", "stress"):
                kind = "fea_static"
            elif kind in ("modal", "frequency", "frequencies"):
                kind = "fea_modal"
            if kind not in _ASYNC_ANALYSES:
                return _dumps(
                    {
                        "ok": False,
                        "error": f"sim_run(analysis={analysis!r}) is not an analysis sim_run "
                        f"can express. Supported: {list(_ASYNC_ANALYSES)}. Every "
                        "other solver starts the same kind of durable job through "
                        "its own tool with background=True (run_cfd_flow, "
                        "run_fea_frequency_response, run_fea_fatigue) or always "
                        "(run_cfd_accurate); sim_status/sim_results/sim_cancel "
                        "accept those job ids too.",
                    }
                )
            if fixtures is None:
                raise ValueError(
                    "sim_run needs fixtures=[...] — the face queries held fixed, e.g. "
                    '["bottom"]. For an unconstrained modal solve pass fixtures=[].'
                )
            notes: list = []
            if kind == "fea_static":
                ctx = _build_static_study(
                    fixtures,
                    loads,
                    material,
                    mesh_fineness,
                    learned_advisor,
                    advisor_max_dof,
                    doc_id,
                    notes,
                )
                stage = "core_rpc"
                payload = {
                    "method": "sim_run_static",
                    "params": {"study": ctx["study"]},
                }
            else:
                n_modes = _positive_int(n_modes, "n_modes")
                ctx = _build_modal_study(
                    fixtures,
                    material,
                    mesh_fineness,
                    learned_advisor,
                    advisor_max_dof,
                    doc_id,
                    notes,
                )
                ctx["n_modes"] = n_modes
                stage = "modal_rpc"
                payload = {
                    "study": ctx["study"],
                    "solve_n": _modal_solve_n(n_modes, ctx["free_free"]),
                    "widen_free_free": ctx["free_free"],
                }
            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            meta = _job_meta(ctx, notes, {"label": label} if label else None)
            job = simjobs.start(kind, stage, payload, meta)
            return _dumps(
                {
                    "ok": True,
                    "job_id": job["job_id"],
                    "state": "running",
                    "analysis": kind,
                    "doc_id": ctx["doc"],
                    "pid": job["pid"],
                    "note": "solving in a separate process — this call did NOT wait. "
                    f"Poll sim_status('{job['job_id']}'), then "
                    f"sim_results('{job['job_id']}'). The job is persisted, so "
                    "it can be collected in a later turn.",
                    "next_actions": [f"sim_status('{job['job_id']}')"],
                }
            )
        except _StudyRefusal as r:
            return _dumps(r.payload)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sim_status(job_id: str) -> str:
        """State of a sim job: queued|running|done|failed|cancelled, elapsed, progress.

        The state is reconciled against the OPERATING SYSTEM, not just a stored flag: a
        worker that died without writing a result is reported as `failed` with its last
        output, never as `running` and never as `done`.
        progress.probe preserves native live fill counters; last_advance_s is time
        since counters/phase advanced, independent of 30-second heartbeats.
        stalled means running with >120 seconds without advancement. Cancel for
        lack of progress only when stalled is true, not for healthy long probes.
        A failed/cancelled run of a kind that retains them (topology) is persisted to
        its study once and returned as `detail` under `code` solve_failed|solve_cancelled.
        In-process driver jobs (job-* ids from run_fea_convergence and
        motion_fea_handoff_sweep) resolve too, marked durable:false.
        """
        try:
            thread = _thread_job(job_id)
            if thread is not None:
                return _dumps(_thread_job_status(thread))
            if str(job_id).startswith(_THREAD_JOB_PREFIX):
                return _dumps(_unknown_thread_job(job_id))
            st = simjobs.status(job_id)
            if st.get("state") == "unknown":
                return _dumps(st)
            state = st["state"]
            # Collect a failed/cancelled job of a kind that keeps one (topology) BEFORE the
            # meta goes: its terminal hook reads the run's setup from it.
            terminal = collect_durable_terminal(job_id, st)
            st.pop(
                "meta", None
            )  # the resolved study setup is bulky and not useful here
            if terminal is not None:
                st["code"] = f"solve_{state}"
                st["detail"] = terminal
            if state == "done":
                st["next_actions"] = [f"sim_results('{job_id}')"]
            elif state == "running":
                st["next_actions"] = [
                    f"sim_status('{job_id}')",
                    f"sim_results('{job_id}', partial=True)",
                ]
                if st.get("progress", {}).get("stalled"):
                    st["next_actions"].append(f"sim_cancel('{job_id}')")
                st["note"] = (
                    "still solving — nothing has converged yet; sim_results with "
                    "partial=True returns the last snapshot, clearly marked partial. "
                    "Cancel for lack of progress only when progress.stalled is true, "
                    "not for a healthy long probe."
                )
            return _dumps(st)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sim_cancel(job_id: str) -> str:
        """Terminate a sim_run job's solver process (SIGTERM, then SIGKILL) and prove it died.

        The reply carries the evidence — `pid`, the `signals` sent, `pid_alive_after` and
        `group_alive_after` — and only reports state "cancelled" when the process (and its
        whole process group, which catches solver subprocesses) is actually gone. If the
        process survives, this returns ok:false and says the solver is STILL RUNNING rather
        than flipping a flag and letting you believe the CPU was freed.
        For lack of progress, cancel only when progress.stalled is true (>120s
        without counter/phase advancement), not merely because a probe is long.
        """
        try:
            thread = _thread_job(job_id)
            if thread is not None:
                return _dumps(_thread_job_cancel(thread))
            if str(job_id).startswith(_THREAD_JOB_PREFIX):
                return _dumps(_unknown_thread_job(job_id))
            return _dumps(simjobs.cancel(job_id))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sim_results(job_id: str, partial: bool = False) -> str:
        """Collect a simulation job's results — from ANY tool that returned a job_id.

        partial=False (default): the full converged result, in exactly the shape the
        synchronous tool returns. On a job that has NOT finished this REFUSES — it will not
        hand back half a solution dressed as an answer. A failed/cancelled run of a kind
        that retains them (topology) returns its persisted ok:false, converged:false
        envelope with the solver's own evidence. `converged` is the solve's own verdict:
        a finished topology run that hit its iteration limit is ok:true, converged:false.

        partial=True: the last progress or solver-intermediate snapshot, always tagged
        `partial: true, converged: false`. A phase-only record can have no percentage or
        field values: it describes work in flight, not a partial solution. If the stage
        published nothing, this says so instead of inventing a snapshot.
        progress carries live probe counters, last_advance_s and stalled, recomputed
        at polling time even if the worker is silent. Cancel for lack of progress
        only when stalled is true, not for a healthy long probe.

        The collect stage runs exactly ONCE per job however many times it is polled and from
        however many processes, so a solved study is written into the document once.

        In-process driver jobs (job-* ids, e.g. a background run_fea_convergence ladder or
        motion_fea_handoff_sweep) follow the same contract: done -> the result get_job
        reports (the whole convergence table), running -> refused, partial=True -> their
        progress record (legs_done / leg_job_ids / current_job_id).
        """
        try:
            thread = _thread_job(job_id)
            if thread is not None:
                return _dumps(_thread_job_results(thread, partial))
            if str(job_id).startswith(_THREAD_JOB_PREFIX):
                return _dumps(_unknown_thread_job(job_id))
            st = simjobs.status(job_id)
            state = st.get("state")
            if state == "unknown":
                return _dumps(st)
            if partial:
                progress = st.get("progress")
                snap = {"partial": True, **progress} if progress else None
                base = {
                    "job_id": job_id,
                    "state": state,
                    "partial": True,
                    "converged": False,
                    "elapsed_s": st.get("elapsed_s"),
                }
                terminal = collect_durable_terminal(job_id, st)
                if terminal is not None:
                    # The run's terminal outcome is now persisted; its retained (last
                    # iterate) numbers stay out of a partial reply.
                    base["terminal"] = {k: v for k, v in terminal.items() if k != "result"}
                if progress:
                    base["progress"] = progress
                if not snap:
                    base.update(
                        ok=False,
                        error=(
                            f"no partial results exist for {job_id}: the {st.get('kind')} solve "
                            "published no intermediate state (a direct FEA solve has no "
                            "converging iterations to report). This is NOT an empty result — "
                            "nothing has been computed to report yet."
                        ),
                    )
                    return _dumps(base)
                base.update(
                    ok=True,
                    snapshot=snap,
                    warning=(
                        "PARTIAL SNAPSHOT — this is the solver's last intermediate state, NOT a "
                        "converged solution. Do not quote these numbers as results."
                    ),
                )
                if state == "done":
                    base["note"] = (
                        "the job has since FINISHED — call "
                        f"sim_results('{job_id}') for the converged result"
                    )
                return _dumps(base)
            if state in (simjobs.FAILED, simjobs.CANCELLED):
                terminal = collect_durable_terminal(job_id, st)
                if terminal is not None:
                    # The kind keeps its failed/cancelled runs: return that persisted
                    # envelope with its evidence, never as a converged answer.
                    out = dict(terminal)
                    out.update(ok=False, job_id=job_id, state=state, converged=False,
                               elapsed_s=st.get("elapsed_s"))
                    out.setdefault("error", st.get("error") or f"solve {state}")
                    if st.get("progress"):
                        out["progress"] = st["progress"]
                    return _dumps(out)
            if state != "done":
                out = {
                    "ok": False,
                    "job_id": job_id,
                    "state": state,
                    "elapsed_s": st.get("elapsed_s"),
                    "error": f"job {job_id} is {state} — there are no converged results to "
                    "return. Refusing to answer with a partial or absent solution.",
                }
                if st.get("progress"):
                    out["progress"] = st["progress"]
                if state in ("running", "queued"):
                    out["hint"] = (
                        f"poll sim_status('{job_id}'), or "
                        f"sim_results('{job_id}', partial=True) for the last "
                        "snapshot (clearly marked partial)"
                    )
                elif st.get("error"):
                    out["solver_error"] = st["error"]
                if state == "failed":
                    refusal = simjobs.refusal_result(job_id)
                    if refusal:
                        # These explain why the study refused; never promote
                        # unconverged displacement, stress, or summary fields.
                        for key in ("contact", "mesh_quality", "reason", "notes",
                                    "selection_spans_contact", "unfixed_islands",
                                    "provenance", "n_elems", "n_nodes"):
                            if key in refusal:
                                out[key] = refusal[key]
                        if refusal.get("error"):
                            out["solver_error"] = refusal["error"]
                return _dumps(out)
            out = dict(collect_durable(job_id, st))
            # A collector that reports its own convergence (topology: ok yet iteration-
            # limited) keeps it; other kinds converge exactly when they collected ok.
            out.setdefault("converged", bool(out.get("ok")))
            out.update(
                job_id=job_id,
                state="done",
                elapsed_s=st.get("elapsed_s"),
            )
            if st.get("progress"):
                out["progress"] = st["progress"]
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sim_jobs(limit: int = 20) -> str:
        """List recent sim jobs (newest first) with their reconciled state and elapsed
        time. `jobs` are the durable sim-* registry rows; they persist across agent turns,
        so this also shows solves an earlier turn started. `process_jobs` are this server
        process's in-process simulation drivers (job-* ids: convergence ladders, motion
        sweeps) — they do not survive a server restart, but a ladder's legs do (in `jobs`)."""
        try:
            limit = _positive_int(limit, "limit")
            rows = []
            for j in simjobs.list_jobs(limit):
                meta = j.pop("meta", None) or {}
                j["doc_id"] = meta.get("doc")
                if meta.get("label"):
                    j["label"] = meta["label"]
                rows.append(j)
            with _cad_jobs._JOB_LOCK:
                thread_ids = [
                    jid
                    for jid, job in _cad_jobs._JOBS.items()
                    if job.get("kind") in _THREAD_SIM_KINDS
                ]
            process_jobs = []
            for jid in reversed(thread_ids):  # insertion order is start order
                job = _thread_job(jid)
                if job is not None:
                    job.pop("result")
                    if job.get("error") is None:
                        job.pop("error")
                    process_jobs.append(job)
                if len(process_jobs) >= limit:
                    break
            return _dumps(
                {
                    "ok": True,
                    "jobs": rows,
                    "count": len(rows),
                    "process_jobs": process_jobs,
                    "registry": str(simjobs.jobs_dir()),
                }
            )
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def run_fea_frequency_response(
        fixtures: Union[list, str],
        excitation_dir: Optional[Union[list, str]] = None,
        sweep_hz: Optional[Union[list, str]] = None,
        zeta: float = 0.02,
        base_accel_g: float = 1.0,
        psd_breakpoints: Optional[Union[list, str]] = None,
        vibration_spec: Optional[Union[dict, str]] = None,
        n_modes: int = 6,
        material: Union[str, dict] = "steel",
        mesh_fineness: Union[float, str] = 0.5,
        doc_id: Optional[str] = None,
        background: bool = False,
    ) -> str:
        """Harmonic + random (PSD) vibration response of the held part vs a base excitation.

        Extends run_fea_modal into a frequency-response solve by modal superposition:
        each mode becomes an SDOF oscillator (participation-factor weighted); harmonic
        response uses the classical transmissibility sweep, random response uses Miles'
        equation per mode (Steinberg 2000 Eq. 6.6). Modes combine by SRSS.

        fixtures       : face queries held fixed (base/support) — same syntax as run_fea_modal.
        excitation_dir : base-excitation unit direction [dx,dy,dz]; default [0,0,1] (Z-up axis).
        sweep_hz       : harmonic sweep points; default 200 log-spaced points, 10–2000 Hz,
                         plus every finite modal frequency inside that band.
        zeta           : modal damping ratio (default 0.02 = 2% — an assumption, echoed back).
        base_accel_g   : harmonic base input amplitude in g (default 1.0 = unit sweep).
        psd_breakpoints: [[f_hz, psd_g2_per_hz], ...] random-vibration input (log-log
                         interpolated). OMITTED → the random field returns
                         {ok:false, reason:"no_psd_provided"} — no default profile is assumed.
        vibration_spec : {"max_peak_accel_g": g} and/or {"max_grms": g} → pass/fail `verdict`
                         (the DAG verify-node end condition). Omitted → verdict is null.

        Returns {ok, freqs_hz, harmonic:{sweep_hz, accel_g, peak_accel_g, peak_freq_hz,
        per_mode, citation, limitations}, random:{per_mode, total_grms, ...} | degraded,
        verdict, damping_assumption}. Response is acceleration amplification (g) —
        physical displacement/stress are NOT recovered (unit-normalized mode shapes).
        """
        try:
            n_modes = _positive_int(n_modes, "n_modes")
            zeta = _finite_float(zeta, "zeta")
            if not 0.0 < zeta < 1.0:
                raise ValueError(
                    f"zeta must be a damping ratio in (0,1), got {zeta!r}."
                )
            base_accel_g = _finite_float(base_accel_g, "base_accel_g")
            if base_accel_g <= 0.0:
                raise ValueError(
                    "base_accel_g must be > 0; a zero base excitation produces "
                    "an identically zero response, not an analysis."
                )
            d = _doc(doc_id)
            _require_sim_geometry(d)
            fixtures = require_list(
                fixtures, "fixtures", 'face queries held fixed, e.g. ["bottom"]'
            )
            bc_notes: list = []
            fixtures = _norm_bc_list(
                fixtures, "fixtures", unwrap_query=True, notes=bc_notes
            )
            if not fixtures:
                raise ValueError(
                    "frequency response needs at least one fixed support face; base excitation "
                    "of a free-free body is undefined. Use run_fea_modal(fixtures=[]) only "
                    "when free-free natural frequencies are the intended result."
                )
            mesh_fineness = _norm_fineness(mesh_fineness)
            ex_dir = (
                _norm_direction(excitation_dir, "excitation_dir")
                if excitation_dir is not None
                else [0.0, 0.0, 1.0]
            )
            using_default_sweep = sweep_hz is None
            if sweep_hz is not None:
                sweep_hz = require_list(
                    sweep_hz,
                    "sweep_hz",
                    "a non-empty list of positive frequencies in Hz",
                )
                if not sweep_hz:
                    raise ValueError("sweep_hz must contain at least one frequency.")
                sweep = [
                    _finite_float(f, f"sweep_hz[{i}]") for i, f in enumerate(sweep_hz)
                ]
                if any(f <= 0.0 for f in sweep):
                    raise ValueError("every sweep_hz frequency must be > 0 Hz.")
            else:
                sweep = [10.0 * (2000.0 / 10.0) ** (i / 199.0) for i in range(200)]
            psd = None
            if psd_breakpoints is not None:
                psd_breakpoints = require_list(
                    psd_breakpoints,
                    "psd_breakpoints",
                    "a non-empty list of [f_hz, psd_g2_per_hz] pairs",
                )
                if not psd_breakpoints:
                    raise ValueError("psd_breakpoints cannot be empty when supplied.")
                psd = []
                for i, point in enumerate(psd_breakpoints):
                    if not isinstance(point, (list, tuple)) or len(point) != 2:
                        raise ValueError(
                            f"psd_breakpoints[{i}] must be [f_hz, g2_per_hz]."
                        )
                    f = _finite_float(point[0], f"psd_breakpoints[{i}][0]")
                    level = _finite_float(point[1], f"psd_breakpoints[{i}][1]")
                    if f <= 0.0 or level <= 0.0:
                        raise ValueError("PSD frequencies and levels must both be > 0.")
                    psd.append((f, level))
            if vibration_spec is not None:
                vibration_spec = require_mapping(
                    vibration_spec,
                    "vibration_spec",
                    'a dict like {"max_peak_accel_g": g} and/or {"max_grms": g}',
                )
                unknown = set(vibration_spec) - {"max_peak_accel_g", "max_grms"}
                if unknown:
                    raise ValueError(
                        f"vibration_spec has unsupported keys {sorted(unknown)!r}; use "
                        "max_peak_accel_g and/or max_grms."
                    )
                if not vibration_spec:
                    raise ValueError("vibration_spec cannot be empty when supplied.")
                for key, value in vibration_spec.items():
                    limit = _finite_float(value, f"vibration_spec.{key}")
                    if limit < 0.0:
                        raise ValueError(f"vibration_spec.{key} must be >= 0.")
                    vibration_spec[key] = limit
            mat = _resolve_study_material(material, "frequency_response")
            fixture_sets: list = []
            for q in fixtures:
                fixture_sets.extend(_sim_face_points(d, q))
            if not fixture_sets:
                raise ValueError(
                    "no fixture faces resolved — use query_geometry() to confirm the fixed "
                    "support face before running frequency response."
                )
            step = _tmp(".step")
            cb.call("doc_export_step", {"doc_id": d, "path": step})
            study = {
                "step_path": step,
                "material": mat,
                "fixtures": fixture_sets,
                "mesh_order": 2,
                "fineness": mesh_fineness,
            }
            # Total mass comes from the LIVE document at the study material's density
            # (eval_mass returns mass_g = volume_mm3 * density; kg = g / 1000). It is a
            # setup input to the modal superposition, so it is measured HERE, while the
            # document is certainly open — the collect stage may run in a later turn, after
            # a restart, with nothing but the job's metadata to work from.
            dens = _finite_float(mat.get("density_g_cm3"), "material.density_g_cm3")
            meas = cb.call("eval_mass", {"doc_id": d, "density_g_cm3": dens}) or {}
            total_mass_kg = (
                _finite_float(meas.get("mass_g"), "eval_mass.mass_g") / 1000.0
            )
            if total_mass_kg <= 0.0:
                raise ValueError(
                    "could not determine part mass (volume or density missing) — set a "
                    "material with density_g_cm3 and ensure the part has solid volume"
                )
            ctx = {
                "analysis": "fea_frequency_response",
                "doc": d,
                "material": mat,
                "material_arg": material,
                "fixtures": fixtures,
                "n_modes": n_modes,
                "mesh_fineness": mesh_fineness,
                "step_path": step,
                "excitation_dir": ex_dir,
                "sweep_hz": sweep,
                "default_sweep": using_default_sweep,
                "zeta": zeta,
                "base_accel_g": base_accel_g,
                "psd_breakpoints": [list(p) for p in psd] if psd is not None else None,
                "vibration_spec": vibration_spec,
                "total_mass_kg": total_mass_kg,
            }
            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            return _dumps(
                _run_solve(
                    "fea_frequency_response",
                    "core_rpc",
                    {
                        "method": "sim_run_modal",
                        "params": {"study": study, "n_modes": n_modes},
                    },
                    _job_meta(ctx, bc_notes),
                    background=background,
                    doc_id=d,
                )
            )
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def run_cfd_flow(
        velocity: float = 10.0,
        aoa: float = 0.0,
        fluid: str = "air",
        cells: int = 96,
        n_steps: int = 2000,
        doc_id: Optional[str] = None,
        background: bool = False,
    ) -> str:
        """Wind-tunnel CFD over the current part/assembly (LBM). Returns drag, lift, Cd, Reynolds.

        CONVENTIONS (Chudware is Z-up): freestream is along +X; LIFT is the +Z (up) force; SPAN is Y.
        +aoa pitches the nose up (rotation about Y), so a cambered wing makes +lift. `reynolds` and
        `cd` use ref_length_mm = the part's LONGEST span (NOT chord) — for an airfoil, simulate the
        wing alone or read `ref_length_mm` and renormalise to chord yourself.

        velocity : free-stream speed (m/s).   aoa : angle of attack (degrees).
        fluid    : "air" or "water".          cells : grid pitch = domain's longest span / cells
                   (higher = finer + slower; practical 48-200).  n_steps : LBM iterations (~500-4000).
        background : if True, return a job_id immediately and solve in a separate process
                     (recommended for cells>=120 or n_steps>=2500, which can exceed the call
                     timeout) — then poll get_job(job_id) for live {pct, step} progress. The
                     job survives an MCP restart; sim_cancel(job_id) really kills the solver.

        RESOLUTION MATTERS: the result reports `voxel_mm`, `min_feature_mm`, and
        `min_feature_voxels` (how many cells span the thinnest part dimension). If a thin wing/tail
        edge spans < ~8 voxels it is UNDER-RESOLVED — lift especially reads near zero — and a
        `warning` is returned; raise `cells` or simulate the thin part on its own. Also reported:
        `converged` + `residual` (force settle over the last fifth of the run); a non-converged run
        is under-iterated, not physically zero.

        ⚠ ABSOLUTE Cd IS UNCALIBRATED. This is a coarse voxel LBM: the staircased surface
        over-counts force (more so on curved/angled faces), so the magnitude of drag_n/cd can be
        off by an order of magnitude and is resolution-dependent. Use it to COMPARE configurations
        and read TRENDS (does +aoa add lift? does a fillet cut drag? A vs B?), which it now gets
        right (drag scales as V^2, Cd is velocity-independent). For a TRUSTWORTHY absolute Cd/Cl,
        use run_cfd_accurate() (OpenFOAM RANS — validated, but minutes per run). Treat these LBM
        numbers as a fast directional estimate, not a certified coefficient.
        """
        try:
            velocity = _finite_float(velocity, "velocity")
            if velocity <= 0.0:
                raise ValueError(
                    "velocity must be > 0 m/s; zero freestream produces a "
                    "zero-field study with undefined coefficients."
                )
            aoa = _finite_float(aoa, "aoa")
            fluid = str(fluid).strip().lower()
            if fluid not in ("air", "water"):
                raise ValueError(
                    f"fluid must be 'air' or 'water', got {fluid!r}; refusing "
                    "to substitute air for an unknown fluid."
                )
            cells = _positive_int(cells, "cells")
            if cells < 16:
                raise ValueError(
                    "cells must be at least 16; a coarser voxel grid cannot "
                    "resolve a useful flow field (48-200 is recommended)."
                )
            n_steps = _positive_int(n_steps, "n_steps")
            d = _doc(doc_id)
            _require_sim_geometry(d)
            # The solve runs in a child process with its OWN engine and no document table,
            # so the geometry travels as a file: export the solid once here, and the stage
            # re-opens exactly that file (jobs._open_geometry) before the tunnel RPC.
            step = _tmp(".step")
            cb.call("doc_export_step", {"doc_id": d, "path": step})
            ctx = {
                "analysis": "cfd_flow",
                "doc": d,
                "velocity": velocity,
                "aoa": aoa,
                "fluid": fluid,
                "cells": cells,
                "n_steps": n_steps,
                "step_path": step,
            }
            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            return _dumps(
                _run_solve(
                    "cfd_flow",
                    "core_rpc",
                    {
                        "method": "sim_run_flow",
                        "open_step": step,
                        # Live-progress side-channel: the engine writes {pct, step, n_steps} into
                        # the job's own spool as the LBM iterates, and the worker folds it into the
                        # partial snapshot get_job/sim_status report.
                        "progress_file": simjobs.PROGRESS_TOKEN,
                        "params": {
                            "velocity": velocity,
                            "aoa": aoa,
                            "fluid": fluid,
                            "cells": cells,
                            "n_steps": n_steps,
                            "progress_path": simjobs.PROGRESS_TOKEN,
                        },
                    },
                    _job_meta(ctx, []),
                    background=background,
                    doc_id=d,
                )
            )
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def run_cfd_accurate(
        velocity: float = 10.0,
        aoa: float = 0.0,
        fluid: str = "air",
        preset: str = "coarse",
        doc_id: Optional[str] = None,
    ) -> str:
        """HIGH-FIDELITY external-aero CFD via OpenFOAM (steady RANS, k-omega SST + prism layers).

        Use this when you need a TRUSTWORTHY absolute Cd/Cl, not the coarse directional estimate
        from run_cfd_flow (the built-in LBM). This is the validated path — sphere/cube land within
        ~10-15% of experiment (regression gate: tools/foam_validation.py). It body-fits a
        snappyHexMesh with boundary layers and solves to convergence, so it takes MINUTES and
        requires OpenFOAM installed — detected automatically on Linux (sourced env / system install /
        user-space conda env / CHUDWARE_OPENFOAM root), native Windows blueCFD, or WSL openfoam2412.
        All OpenFOAM invocations are subprocesses (GPL isolation — never linked), and they run
        inside the JOB's process group, so they always run in the background: this returns a
        job_id immediately; poll get_job(job_id) (which reports live meshing/solve progress)
        for the result. The job is persisted on disk — it survives an MCP restart and can be
        collected in a later turn — and sim_cancel(job_id) terminates the mesher and solver
        for real instead of leaving them burning a core.

        CONVENTIONS match run_cfd_flow (Z-up): freestream +X, lift +Z, +aoa pitches nose up.
        velocity : m/s.   aoa : degrees.   fluid : "air" | "water".
        preset   : "coarse" (fastest, ~600 iters) | "medium" | "fine" | "xfine" (finest/slowest).
                   Start coarse to sanity-check, refine if you need the digits.

        Result (via get_job): {cd, cl, ld (lift/drag), drag_n, lift_n, reynolds, ref_area_mm2,
        vmax_mps, converged, iters, notes}. If `converged` is false, raise the preset / iterations.
        """
        try:
            velocity = _finite_float(velocity, "velocity")
            if velocity <= 0.0:
                raise ValueError(
                    "velocity must be > 0 m/s; zero freestream produces no "
                    "aerodynamic study."
                )
            aoa = _finite_float(aoa, "aoa")
            fluid = str(fluid).strip().lower()
            if fluid not in ("air", "water"):
                raise ValueError(
                    f"fluid must be 'air' or 'water', got {fluid!r}; refusing "
                    "to silently use air."
                )
            preset = str(preset).strip().lower()
            if preset not in simjobs.FOAM_PRESETS:
                return _dumps(
                    {
                        "ok": False,
                        "error": f"unknown preset {preset!r} — use "
                        "coarse|medium|fine|xfine",
                    }
                )
            from services.sim.foam_runner import capabilities

            caps = capabilities()
            if not caps.get("openfoam"):
                import os as _os

                how = (
                    "`python -m tools.provision_openfoam --install` (user-space conda-forge "
                    "env, no sudo), or unpack an openfoam.com tarball under ~/OpenFOAM/, or "
                    "set CHUDWARE_OPENFOAM to an install root"
                    if _os.name == "posix"
                    else "`python -m tools.provision_openfoam --install` (WSL openfoam2412) or "
                    "install blueCFD-Core natively and set %CHUDWARE_OPENFOAM%"
                )
                return _dumps(
                    {
                        "ok": False,
                        "skipped": True,
                        "reason": "openfoam_not_installed",
                        "error": "OpenFOAM is not available on this machine. Provision it "
                        f"with {how}. The coarse LBM run_cfd_flow still works.",
                    }
                )
            d = _doc(doc_id)
            _require_sim_geometry(d)
            # OpenFOAM meshes a watertight STL surface (build_case handles mm -> m).
            stl = _tmp(".stl")
            cb.call("export_file", {"doc_id": d, "path": stl, "format": "stl"})
            fluid_dict = (
                {"nu": 1.0e-6, "rho": 1000.0}
                if fluid == "water"
                else {"nu": 1.5e-5, "rho": 1.225}
            )
            ctx = {
                "analysis": "cfd_openfoam",
                "doc": d,
                "velocity": velocity,
                "aoa": aoa,
                "fluid": fluid,
                "preset": preset,
                "stl_path": stl,
                "openfoam_kind": caps.get("openfoam_kind"),
            }
            _autosave_all_open()
            job = simjobs.start(
                "cfd_openfoam",
                "openfoam_tunnel",
                {
                    "stl_path": stl,
                    "speed_mps": velocity,
                    "aoa_deg": aoa,
                    "preset": preset,
                    "fluid": fluid_dict,
                },
                _job_meta(ctx, []),
            )
            out = _started(job, d)
            out.update(solver=caps.get("openfoam_version"), preset=preset)
            out["note"] = (
                "OpenFOAM RANS runs for minutes — it solves in a SEPARATE PROCESS "
                f"and reports live meshing/solve progress. Poll get_job('"
                f"{job['job_id']}'); the job is persisted, so it survives an MCP "
                f"restart, and sim_cancel('{job['job_id']}') really stops it."
            )
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ================================================================
    # Advanced FEA — fatigue / thermal / buckling / contact FEA
    # These tools solve on the real CAD mesh through the C++ engine.
    # fea_advanced retains fatigue post-processing and the legacy ccx contact
    # worker/V&V helpers, not a thermal or buckling proxy.
    # ================================================================

    def _part_bbox_world_spans(d: str) -> tuple[float, float, float]:
        """Return finite positive world-X/Y/Z union spans for proxy mappings."""
        bodies = _require_sim_geometry(d)
        lo = [float("inf")] * 3
        hi = [float("-inf")] * 3
        for index, body in enumerate(bodies):
            bbox = body["bbox"]
            for axis in range(3):
                lo[axis] = min(
                    lo[axis],
                    _finite_float(bbox["min"][axis], f"body[{index}].bbox.min[{axis}]"),
                )
                hi[axis] = max(
                    hi[axis],
                    _finite_float(bbox["max"][axis], f"body[{index}].bbox.max[{axis}]"),
                )
        spans = tuple(hi[axis] - lo[axis] for axis in range(3))
        if any(not math.isfinite(span) or span <= 0.0 for span in spans):
            raise ValueError(
                f"part bounding box is missing or degenerate ({spans!r}); proxy solvers "
                "require a finite 3-D solid with non-zero span on every axis."
            )
        return spans

    def _part_bbox_dims(d: str) -> tuple:
        """Return longest-to-shortest bounding-box spans for orientation-free proxies."""
        size = sorted(_part_bbox_world_spans(d), reverse=True)
        return size[0], size[1], size[2]

    @mcp.tool()
    def run_fea_fatigue(
        fixtures: Union[list, str],
        loads: Union[list, str],
        material: Union[str, dict] = "steel",
        load_type: str = "reversed",
        load_ratio: float = -1.0,
        surface: str = "machined",
        reliability_pct: float = 50.0,
        temperature_c: float = 20.0,
        theory: str = "goodman",
        mesh_fineness: Union[float, str] = 0.5,
        doc_id: Optional[str] = None,
        background: bool = False,
        load_case: str = "bending",
    ) -> str:
        """High-cycle fatigue life (S-N) on the current part — reuses the linear-static
        stress field, then applies mean-stress theory + Marin correction + Basquin life.

        Runs the SAME static solve as run_fea_static (fixtures/loads/material), then the
        engine (`sim_run_fatigue`) evaluates every mean-stress theory (Goodman/Gerber/
        Soderberg/ASME/Morrow) node-by-node and returns each theory's max equivalent
        fully-reversed amplitude sa_eq. This tool then applies the Marin endurance
        correction + reliability + the Shigley S-N life line (mirrors the desktop app's
        fatigue math exactly) to report cycles-to-failure + a factor of safety.

        fixtures/loads : SAME shapes as run_fea_static — including the polar
                         {"magnitude": N, "direction": [dx,dy,dz]} load form, plural
                         "faces": [q1, q2], and a face SID ("F58db6d") as a query.
        material       : name or dict (needs ultimate_mpa + endurance_mpa; the built-in
                         materials all carry them).
        load_type      : "reversed" (R=-1, default) | "zero_based" (R=0) | "ratio" (use
                         load_ratio) — the cyclic load spectrum on the static result.
        surface        : "ground"|"machined"|"hot_rolled"|"as_forged" (Marin ka).
        reliability_pct: design reliability for ke (50/90/99/99.9...).
        theory         : which mean-stress theory to report at top level (default goodman).
        load_case      : "bending" (default), "axial", or "torsion" for Marin kb/kc.
                         Rectangular bending/torsion uses d_e=0.808*sqrt(b*h) from the
                         two smaller bounding-box spans; axial uses kb=1.

        Returns {ok, regime: infinite|finite|static_overload, cycles, fos_endurance,
        se_corrected_mpa, theories{...}}. regime="infinite" ⇒ >1e6 cycles (runout).
        Degrades like run_fea_static when the C++ FEA mesher backend is unavailable.
        """
        try:
            from . import fea_advanced as fa

            d = _doc(doc_id)
            fixtures = require_list(
                fixtures,
                "fixtures",
                "SAME plain-English face queries as run_fea_static",
            )
            loads = require_list(
                loads, "loads", "SAME {face + force/pressure} shape as run_fea_static"
            )
            bc_notes: list = []
            fixtures = _norm_bc_list(
                fixtures, "fixtures", unwrap_query=True, notes=bc_notes
            )
            loads = _norm_bc_list(loads, "loads", notes=bc_notes)
            mesh_fineness = _norm_fineness(mesh_fineness)
            mat = _resolve_study_material(material, "fatigue")
            Sut = _finite_float(mat["ultimate_mpa"], "material.ultimate_mpa")
            Se_base = _finite_float(mat["endurance_mpa"], "material.endurance_mpa")
            surface = str(surface).strip().lower().replace("-", "_").replace(" ", "_")
            surf_map = {"ground": 0, "machined": 1, "hot_rolled": 2, "as_forged": 3}
            if surface not in surf_map:
                raise ValueError(
                    f"surface must be one of {sorted(surf_map)}, got {surface!r}."
                )
            load_type = str(load_type).strip().lower().replace("-", "_")
            if load_type not in ("reversed", "zero_based", "ratio"):
                raise ValueError(
                    "load_type must be 'reversed', 'zero_based', or 'ratio'; "
                    f"got {load_type!r}."
                )
            load_ratio = _finite_float(load_ratio, "load_ratio")
            if load_type == "ratio" and load_ratio >= 1.0:
                raise ValueError(
                    "load_ratio must be < 1 for a cyclic ratio study (R=min/max)."
                )
            theory = str(theory).strip().lower()
            valid_theories = {"goodman", "gerber", "soderberg", "asme", "morrow"}
            if theory not in valid_theories:
                raise ValueError(
                    f"theory must be one of {sorted(valid_theories)}, got {theory!r}."
                )
            reliability_pct = _finite_float(reliability_pct, "reliability_pct")
            if not 50.0 <= reliability_pct <= 99.999:
                raise ValueError(
                    "reliability_pct must be in the supported Marin table range "
                    "50..99.999."
                )
            temperature_c = _finite_float(temperature_c, "temperature_c")
            if not 20.0 <= temperature_c <= 550.0:
                raise ValueError(
                    "temperature_c must be in the supported Marin correction "
                    "range 20..550 °C; extrapolation is not supported."
                )
            load_case = str(load_case).strip().lower()
            load_case_map = {"bending": 0, "axial": 1, "torsion": 2}
            if load_case not in load_case_map:
                raise ValueError(
                    f"load_case must be one of {sorted(load_case_map)}, got "
                    f"{load_case!r}."
                )
            fixture_sets: list = []
            for q in fixtures:
                fixture_sets.extend(_sim_face_points(d, q))
            if not fixture_sets:
                return _dumps(
                    {
                        "ok": False,
                        "error": (
                            "fatigue FEA needs at least one fixed face — you passed fixtures=[]. "
                            "An unconstrained part has nothing to react the cyclic load against."
                            if not fixtures
                            else "no fixture faces resolved — use query_geometry() to confirm the "
                            "face query."
                        ),
                    }
                )
            load_specs = _build_load_specs(d, loads, "fatigue")
            _reject_fully_constrained_loads(fixture_sets, load_specs, "fatigue FEA")
            _, b, h = _part_bbox_dims(d)
            diameter_mm = 0.808 * math.sqrt(b * h)
            corr = fa.corrected_endurance(
                Se_base,
                Sut,
                surface=surf_map[surface],
                load_case=load_case_map[load_case],
                diameter_mm=diameter_mm,
                temperature_c=temperature_c,
                reliability_pct=reliability_pct,
            )
            corr["diameter_mm"] = diameter_mm
            corr["load_case"] = load_case
            lt_map = {
                "reversed": {"lmin": -1.0, "lmax": 1.0},
                "zero_based": {"lmin": 0.0, "lmax": 1.0},
                "ratio": {"lmin": load_ratio, "lmax": 1.0},
            }
            fat = {
                "ultimate_mpa": Sut,
                "yield_mpa": float(mat["yield_mpa"]),
                "se_corrected_mpa": corr["se_corrected_mpa"],
                "se_base_mpa": corr["se_base_mpa"],
            }
            fat.update(lt_map[load_type])
            # Export only after all material/BC/fatigue setup checks have passed.
            step = _tmp(".step")
            cb.call("doc_export_step", {"doc_id": d, "path": step})
            study = {
                "step_path": step,
                "material": mat,
                "fixtures": fixture_sets,
                "loads": load_specs,
                "mesh_order": 2,
                "fineness": mesh_fineness,
                "fatigue": fat,
            }

            ctx = {
                "analysis": "fea_fatigue",
                "doc": d,
                "material": mat,
                "material_arg": material,
                "fixtures": fixtures,
                "loads": loads,
                "mesh_fineness": mesh_fineness,
                "step_path": step,
                "theory": theory,
                "marin": corr,
                "ultimate_mpa": Sut,
                "load_type": load_type,
            }
            _autosave_all_open()
            try:
                return _dumps(
                    _run_solve(
                        "fea_fatigue",
                        "core_rpc",
                        {"method": "sim_run_fatigue", "params": {"study": study}},
                        _job_meta(ctx, bc_notes),
                        background=background,
                        doc_id=d,
                    )
                )
            except ValueError as ve:
                # The registry probes the solver before it queues anything, so an absent
                # backend refuses the START rather than producing a job that can only fail.
                # Same honest degrade the synchronous path always reported.
                if not _backend_missing(ve, "sim_run_fatigue"):
                    raise
                return _dumps(
                    {
                        "ok": False,
                        "skipped": True,
                        "doc_id": d,
                        "backend": "cpp fea",
                        "reason": "the C++ FEA solver backend (sim_run_fatigue) is not "
                        "available in this build (mesher not compiled). "
                        "Fatigue reuses the static stress field — it needs "
                        "the same FEA backend as run_fea_static.",
                    }
                )
        except _StudyRefusal as r:
            return _dumps(r.payload)
        except CoreError as e:
            # C++ FEA backend absent (mesher not built) → honest degrade, same as static.
            if "unknown method" in str(e):
                return _dumps(
                    {
                        "ok": False,
                        "skipped": True,
                        "reason": "the C++ FEA solver backend (sim_run_fatigue) is not "
                        "available in this build (mesher not compiled). Fatigue "
                        "reuses the static stress field — it needs the same FEA "
                        "backend as run_fea_static.",
                        "backend": "cpp fea",
                    }
                )
            return _err(e)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def run_fea_thermal(
        hot_faces: Union[list, str],
        cold_faces: Union[list, str],
        material: Union[str, dict] = "steel",
        conductivity_w_mk: Optional[float] = None,
        transient: Optional[Union[dict, str]] = None,
        loads: Optional[Union[list, str]] = None,
        specific_heat_j_kgk: Optional[float] = None,
        mesh_fineness: Union[float, str] = 0.5,
        learned_advisor: bool = False,
        advisor_max_dof: int = 0,
        doc_id: Optional[str] = None,
        vtu_path: Optional[str] = None,
    ) -> str:
        """Thermal conduction on the real part tet mesh using the C++ engine.

        STEADY (default, transient=None): solves ∇·(k∇T)=0 with fixed-temperature
        faces, heat-flux faces and convection. A fixed temperature or convection
        boundary anchors the temperature datum. Time schedules require transient.

        TRANSIENT: solves C dT/dt + K T = f(t) with implicit backward Euler.
          transient : {"t_end_s": <s>, "dt_s": <s, optional — defaults t_end/200>,
                       "t_init_c": <°C, default 20>, "frame_times_s": [<s>, ...],
                       "capacitance": "lumped"|"consistent" (default lumped)}
          hot_faces/cold_faces : Dirichlet faces held at their temperature for the whole
                       run, e.g. [{"face": "+x", "temperature": 100}]. May be EMPTY for
                       transient (a flux-only adiabatic heat-up is well-posed) — but then
                       `loads` must supply the flux/convection, or there is no thermal
                       input at all and the study is refused.
          loads     : optional time-varying heat loads, each
                       {"face": <query>, "flux_w_m2": <q>, "schedule": {...}}  or
                       {"face": <query>, "h_w_m2k": <h>, "t_amb_c": <°C>}   (convection)
                      schedule (per load, optional — omitted = always on):
                       {"duty": 0.25, "period_s": 1.0}          duty-cycle shorthand —
                          the direct mapping for an aicad.powerprofile/1 per-rail duty
                          profile (rail duty d, period p)
                       {"points": [[t_s, scale], ...], "period_s": <opt>}  piecewise
                          sample-and-hold (a residency timeline maps verbatim)
          specific_heat_j_kgk : cp override; else material dict, else a typical value.
        hot_faces/cold_faces/loads all accept the plural "faces": [q1, q2] key (fanned out to
        one entry per face, siblings kept) and a face SID ("F58db6d") as a face query.
        Returns frame times + per-frame max temperature, the PEAK temperature over the
        whole run (max-temp envelope) and when it occurred. Full nodal frames stay in
        the study result inside the doc.

        conductivity_w_mk : thermal conductivity override (W/m·K); else from material
                            (falls back to a typical value for the named material).
        learned_advisor : default-off Polyhedral-FEA mesh advice; not a thermal optimizer.
        advisor_max_dof : optional non-negative advisor budget; 0 leaves it unset.
        vtu_path : optional .vtu path with nodal temperature point data. A relative path
                   is project-root relative.
        Both routes return mesh_quality and provenance beside summary. Steady
        returns temperature_max_c, temperature_min_c and flux_max_w_m2 without
        a time axis; full nodal temperatures are persisted in the doc study.
        """
        try:
            bc_notes: list = []
            hot_faces = require_list(
                hot_faces,
                "hot_faces",
                'a list of {"face": <query>, "temperature": <°C>}',
            )
            cold_faces = require_list(
                cold_faces,
                "cold_faces",
                'a list of {"face": <query>, "temperature": <°C>}',
            )
            loads = (
                []
                if loads is None
                else require_list(
                    loads,
                    "loads",
                    'a list of {"face": <query>, "flux_w_m2": <q>} or '
                    '{"face": <query>, "h_w_m2k": <h>} entries',
                )
            )
            loads = _norm_bc_list(loads, "loads", notes=bc_notes)
            hot_faces = _norm_bc_list(hot_faces, "hot_faces", notes=bc_notes)
            cold_faces = _norm_bc_list(cold_faces, "cold_faces", notes=bc_notes)
            mesh_fineness = _norm_fineness(mesh_fineness)
            advisor = _advisor_params(learned_advisor, advisor_max_dof)
            vtu_path = _norm_vtu_path(vtu_path)
            material = coerce_json_or_repr(material)
            if conductivity_w_mk is not None:
                conductivity_w_mk = _finite_float(
                    conductivity_w_mk, "conductivity_w_mk"
                )
                if conductivity_w_mk <= 0.0:
                    raise ValueError("conductivity_w_mk must be > 0.")
            if specific_heat_j_kgk is not None:
                specific_heat_j_kgk = _finite_float(
                    specific_heat_j_kgk, "specific_heat_j_kgk"
                )
                if specific_heat_j_kgk <= 0.0:
                    raise ValueError("specific_heat_j_kgk must be > 0.")
            if transient is not None:
                transient = require_mapping(
                    transient, "transient", 'a dict like {"t_end_s": 60, "dt_s": 0.5}'
                )
                unknown = set(transient) - {
                    "t_end_s",
                    "dt_s",
                    "t_init_c",
                    "frame_times_s",
                    "capacitance",
                }
                if unknown:
                    raise ValueError(
                        f"transient has unsupported fields {sorted(unknown)!r}; refusing to "
                        "ignore them."
                    )
                if "t_end_s" not in transient:
                    raise ValueError("transient needs t_end_s in seconds.")
                t_end = _finite_float(transient["t_end_s"], "transient.t_end_s")
                if t_end <= 0.0:
                    raise ValueError("transient.t_end_s must be > 0.")
                dt = (
                    _finite_float(transient["dt_s"], "transient.dt_s")
                    if transient.get("dt_s") is not None
                    else 0.0
                )
                if dt < 0.0 or dt > t_end:
                    raise ValueError(
                        "transient.dt_s must be > 0 and <= t_end_s when supplied."
                    )
                if "dt_s" in transient and dt == 0.0:
                    raise ValueError("transient.dt_s must be > 0 when supplied.")
                t_init = _finite_float(
                    transient.get("t_init_c", 20.0), "transient.t_init_c"
                )
                frame_times_raw = transient.get("frame_times_s", [])
                if not isinstance(frame_times_raw, (list, tuple)):
                    raise ValueError("transient.frame_times_s must be a list.")
                frame_times = [
                    _finite_float(t, f"transient.frame_times_s[{i}]")
                    for i, t in enumerate(frame_times_raw)
                ]
                if any(t < 0.0 or t > t_end for t in frame_times):
                    raise ValueError("transient.frame_times_s must lie in [0,t_end_s].")
                if any(b <= a for a, b in zip(frame_times, frame_times[1:])):
                    raise ValueError(
                        "transient.frame_times_s must be strictly ascending."
                    )
                capacitance = (
                    str(transient.get("capacitance", "lumped")).strip().lower()
                )
                if capacitance not in ("lumped", "consistent"):
                    raise ValueError(
                        "transient.capacitance must be 'lumped' or 'consistent'."
                    )

            d = _doc(doc_id)
            _require_sim_geometry(d)
            mat = _resolve_study_material(
                material,
                "thermal_transient" if transient is not None else "thermal_steady",
                conductivity_w_mk=conductivity_w_mk,
                specific_heat_j_kgk=specific_heat_j_kgk,
            )
            k_cond = mat["conductivity_w_mk"]
            cp = mat.get("cp_j_kgk")

            def _faceset(q):
                faces = _sim_face_points(d, q)
                # Tighten each planar oracle separately before forming the union:
                # differently oriented planes must never share one normal.
                for face in faces:
                    normal = face.get("face_normal")
                    points = face.get("points")
                    if normal and points:
                        norm = math.sqrt(sum(v * v for v in normal))
                        if norm > 0.0 and all(
                            abs(sum((p[i] - points[0][i]) * normal[i] for i in range(3)))
                            <= 1e-6 * norm for p in points
                        ):
                            face["perp_tol_mm"] = 1e-6
                return _merge_face_sets(faces)

            fixtures = []
            thermal_driving = False
            for i, f in enumerate(list(hot_faces) + list(cold_faces)):
                where = f"temperature_faces[{i}]"
                if (
                    not isinstance(f, dict)
                    or "temperature" not in f
                    or "face" not in f
                ):
                    raise ValueError(
                        "each hot/cold face needs {'face': <query>, "
                        f"'temperature': <°C>}}: {f!r}"
                    )
                _reject_ignored_fields(f, {"face", "temperature"}, where)
                temp = _finite_float(f["temperature"], f"{where}.temperature")
                fs = _faceset(f["face"])
                if fs is None:
                    raise ValueError(
                        f"face query {f['face']!r} resolved to no faces — "
                        "use query_geometry() to confirm."
                    )
                fs["temp_c"] = temp
                fixtures.append(fs)
                thermal_driving = thermal_driving or transient is None or abs(temp - t_init) > 0.0

            load_specs = []
            for i, ld in enumerate(loads):
                where = f"loads[{i}]"
                if not isinstance(ld, dict) or "face" not in ld:
                    raise ValueError(
                        f"each thermal load needs a 'face' query: {ld!r}"
                    )
                has_flux = "flux_w_m2" in ld
                has_film = "h_w_m2k" in ld
                if has_flux == has_film:
                    raise ValueError(
                        f"{where} needs exactly one of flux_w_m2 or h_w_m2k."
                    )
                fs = _faceset(ld["face"])
                if fs is None:
                    raise ValueError(
                        f"load face query {ld['face']!r} resolved to no faces."
                    )
                if has_flux:
                    _reject_ignored_fields(
                        ld, {"face", "flux_w_m2", "schedule"}, where
                    )
                    flux = _finite_float(ld["flux_w_m2"], f"{where}.flux_w_m2")
                    active = _validate_thermal_schedule(
                        ld.get("schedule"), f"{where}.schedule"
                    )
                    spec = {"faces": fs, "type": "flux", "magnitude": flux}
                    if "schedule" in ld and transient is None:
                        raise ValueError("steady thermal loads cannot have a time schedule")
                    if "schedule" in ld:
                        spec["schedule"] = ld["schedule"]
                    thermal_driving = thermal_driving or (
                        active and abs(flux) > 0.0
                    )
                else:
                    _reject_ignored_fields(
                        ld, {"face", "h_w_m2k", "t_amb_c"}, where
                    )
                    htc = _finite_float(ld["h_w_m2k"], f"{where}.h_w_m2k")
                    if htc <= 0.0:
                        raise ValueError(f"{where}.h_w_m2k must be > 0.")
                    ambient = _finite_float(
                        ld.get("t_amb_c", 20.0), f"{where}.t_amb_c"
                    )
                    spec = {
                        "faces": fs,
                        "type": "convection",
                        "h": htc,
                        "t_amb_c": ambient,
                    }
                    thermal_driving = thermal_driving or transient is None or abs(ambient - t_init) > 0.0
                load_specs.append(spec)

            if transient is not None and not thermal_driving:
                raise _StudyRefusal(
                    {
                        "ok": False,
                        "error": (
                            "the transient study has no thermal input / non-zero excitation: every "
                            "fixed temperature and convection ambient equals t_init_c, and every "
                            "active heat flux is zero. The part would only echo its initial "
                            "temperature. Supply a different face/ambient temperature or a "
                            "non-zero scheduled flux."
                        ),
                    }
                )

            step = _tmp(".step")
            cb.call("doc_export_step", {"doc_id": d, "path": step})
            _archive_existing_vtu(vtu_path, bc_notes)
            study = {
                "step_path": step,
                "material": {**mat, "k_w_mk": k_cond},
                "fixtures": fixtures,
                "loads": load_specs,
                "mesh_order": 2,
                "fineness": mesh_fineness,
            }
            if transient is not None:
                study["transient"] = {
                    "t_end_s": t_end, "dt_s": dt, "t_init_c": t_init,
                    "frame_times_s": frame_times, "capacitance": capacitance,
                }
            if vtu_path:
                study["vtu_path"] = vtu_path
            study.update(advisor)
            res = cb.call("sim_run_thermal", {"study": study})
            if not res.get("ok"):
                return _dumps(
                    {
                        "ok": False,
                        "doc_id": d,
                        "error": res.get("error", "thermal solve failed"),
                    }
                )
            if transient is None:
                out = {
                    "ok": True, "doc_id": d, "transient": False,
                    "solver": "C++ core steady conduction",
                    "material": mat.get("name", material),
                    "conductivity_w_mk": k_cond,
                    **{key: res[key] for key in (
                        "summary", "temperature_max_c", "temperature_min_c",
                        "flux_max_w_m2", "n_nodes", "n_elems")},
                    "solver_notes": res.get("solver_notes", []),
                    **_evidence_fields(res),
                }
                if isinstance(res.get("advisor"), dict):
                    out["advisor"] = res["advisor"]
                sid = _persist_study(d, {
                    "type": "fea_thermal", "name": f"Thermal ({mat.get('name', material)})",
                    "engine": "cpp", "material": mat, **advisor,
                    **_evidence_fields(res), "results": res,
                })
                out["saved_to_study"] = bool(sid)
                return _dumps(_interpreted(out, bc_notes))
            try:
                checked = _validate_transient_thermal_result(res, t_end)
            except ValueError as exc:
                return _dumps(
                    {
                        "ok": False,
                        "doc_id": d,
                        "error": f"thermal solver returned an incomplete/non-finite "
                        f"or inconsistent result: {exc}",
                    }
                )
            frames = checked["frames"]
            frame_summ = checked["frame_summaries"]
            summary = checked["summary"]
            counts = checked["counts"]
            out = {
                "ok": True,
                "doc_id": d,
                "transient": True,
                "solver": "C++ core transient conduction (backward Euler, "
                f"{capacitance} capacitance)",
                "material": mat.get("name", material),
                "conductivity_w_mk": k_cond,
                "specific_heat_j_kgk": cp,
                "summary": summary,
                "n_steps": counts["n_steps"],
                "n_nodes": counts["n_nodes"],
                "n_elems": counts["n_elems"],
                "frames": frame_summ,
                "solver_notes": res.get("solver_notes", []),
                "note": "summary.max_temp_c is the PEAK over the whole run "
                "(max-temp envelope), at summary.max_temp_time_s; "
                "per-frame nodal fields persisted in the doc study.",
                **_evidence_fields(res),
            }
            advisor_record = (
                {"advisor": res["advisor"]}
                if isinstance(res.get("advisor"), dict)
                else {}
            )
            out.update(advisor_record)
            sid = _persist_study(
                d,
                {
                    "type": "fea_thermal_transient",
                    "name": f"Transient thermal ({mat.get('name', material)})",
                    "engine": "cpp",
                    "material": {**mat, "k_w_mk": k_cond, "cp_j_kgk": cp},
                    "transient": study["transient"],
                    **advisor,
                    **advisor_record,
                    **_evidence_fields(res),
                    "results": {
                        "summary": summary,
                        "frames": frames,
                        "envelope_max": checked["envelope_max"],
                    },
                },
            )
            out["saved_to_study"] = bool(sid)
            return _dumps(_interpreted(out, bc_notes))
        except CoreError as e:
            return _err(e)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def run_fea_buckling(
        fixtures: Union[list, str],
        load: Union[list, str],
        material: Union[str, dict] = "steel",
        n_modes: int = 4,
        doc_id: Optional[str] = None,
        size_mm: float = 8.0,
        vtu_path: Optional[str] = None,
        max_load_factor: float = 1e4,
    ) -> str:
        """Linear eigenvalue buckling on the real CAD mesh using the engine solver.

        fixtures : mounted face queries, with the same syntax as run_fea_static.
        load : reference face forces in N or pressures in MPa, using the static
               load dialect, including magnitude/direction and plural faces.
        material : named material or explicit elastic material dict.
        n_modes : number of positive buckling modes, lowest load factor first.
        size_mm : target quadratic-tetrahedron size in mm (default 8).
        vtu_path : optional .vtu path containing normalized mode shapes. A relative path
                   is project-root relative.
        max_load_factor : finite positive search ceiling (default 10000). A critical
                          load above this multiple of the reference load is reported
                          as no buckling mode below the specified ceiling.

        Each modes entry has load_factor and directional participation {x,y,z}
        (normalized squared shape components, not effective mass fractions).
        critical_load_factor scales the reference load; critical_load_n reports
        its critical magnitude.
        A factor below one predicts instability below the applied reference load.
        The engine refuses when no positive mode is below max_load_factor, including
        non-compressive prestress. This is linear elastic buckling, not nonlinear collapse.
        Read mesh_quality and provenance, and refine before relying on a margin.
        """
        try:
            n_modes = _positive_int(n_modes, "n_modes")
            size_mm = _finite_float(size_mm, "size_mm")
            if size_mm <= 0.0:
                raise ValueError("size_mm must be > 0")
            max_load_factor = _finite_float(max_load_factor, "max_load_factor")
            if max_load_factor <= 0.0:
                raise ValueError("max_load_factor must be > 0")
            bc_notes: list = []
            ctx = _build_static_study(
                fixtures, load, material, 0.5, False, 0, doc_id, bc_notes,
                target_size_mm=size_mm, vtu_path=vtu_path,
                material_analysis="buckling",
            )
            _autosave_all_open()
            out = cb.call(
                "sim_run_buckling", {"study": ctx["study"], "n_modes": n_modes,
                                     "max_load_factor": max_load_factor}
            )
            out["doc_id"] = ctx["doc"]
            if out.get("ok"):
                sid = _persist_study(
                    ctx["doc"],
                    {
                        "type": "fea_buckling",
                        "name": f"Buckling ({ctx['material'].get('name', material)})",
                        "engine": "engine",
                        "results": out,
                    },
                )
                out["saved_to_study"] = bool(sid)
            return _dumps(_interpreted(out, bc_notes))
        except _StudyRefusal as e:
            return _dumps(e.payload)
        except CoreError as e:
            return _err(e)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def run_fea_contact(
        fixtures: Union[list, str],
        load: Union[list, str, dict],
        material: Union[str, dict] = "steel",
        max_gap_mm: float = 0.5,
        max_iterations: int = 25,
        mesh_size_mm: Optional[float] = None,
        interface_size_mm: Optional[float] = None,
        min_size_mm: Optional[float] = None,
        plasticity: Optional[dict] = None,
        mesh_order: int = 1,
        doc_id: Optional[str] = None,
        vtu_path: Optional[str] = None,
        interfaces: Optional[dict] = None,
        penalty_scale: float = 1.0,
        bond_gap_mm: Optional[float] = None,
        closed_fit: bool = True,
        closed_fit_gap_mm: Optional[float] = None,
    ) -> str:
        """Frictionless unilateral CONTACT FEA on the real meshed bodies of this document.

        What it solves: a small-deflection static problem whose contact-classified
        interfaces carry compression only; bonded interfaces also carry tension.
        The mesher meshes every body, and the engine finds facing surface-node pairs
        across at most max_gap_mm (normals from the mesh boundary faces, each keeping its
        initial signed gap), and an ACTIVE-SET loop solves repeatedly: pairs whose gap
        closed press, pairs that went into tension are released, until the set stops
        changing. So a gap really closes, a seated face really lifts off, and load only
        crosses where the parts touch.

        Use it for: a slip-fit pin in a clearance bore (which side of the bore takes the
        load), a bolted or shimmed stack (how the clamp load splits between seats), a part
        merely SEATED in a pocket or on a pad, and any assembly where run_fea_static would
        have to pretend the joint is welded. Mixed press-fit + clearance assemblies use
        interfaces to bond the welded/glued or idealized press-fit body pairs while the
        clearance pairs remain unilateral contact. For an entirely bonded study use
        run_fea_static with interfaces instead.

        What it does NOT do: friction (the interface is frictionless; a small tangential
        stabilizer removes the sliding nullspace and its share of the resultant is reported
        as contact.tangential_force_fraction — if that is more than a few percent the model
        needs a real tangential restraint), large sliding or large deflection (small-strain
        only: the pairing is computed on the UNDEFORMED mesh), and bolt preload. Preload is
        approximated by applying the clamp force as a face load on the clamped face — e.g.
        load={"face":"top","force":[0,0,-5000]} for a 5 kN bolt — and reading the seat
        reaction out of contact.pairs.

        fixtures : list of face queries held fixed, in the run_fea_static dialect
                   (["bottom"], [{"of":"face","side":"bottom","pick":"largest"}], face SIDs).
        load     : ONE face query plus a force (N, world XYZ) or pressure (MPa), e.g.
                   {"face":"top","force":[0,0,-500]}; a list is accepted.
        material : a registry name ("steel","aluminum",...) or an explicit property dict.
                   Or {per_body: {"1": name_or_dict, ...}, default: name_or_dict};
                   STEP solid ordinals are one-based; each body uses its own E_mpa/nu/yield_mpa,
                   and default is optional when every body is explicitly assigned.
        max_gap_mm : widest mesh gap still treated as a contact candidate (default 0.5).
                   Measure the real clearance first and pass just above it: too small finds
                   no pair (refused, with the measured body count), too large pairs surfaces
                   that never touch.
        max_iterations : active-set iterations (default 25). A set that never settles is
                   REFUSED, not averaged.
        penalty_scale : positive finite multiplier of the contact penalty stiffness
                   (default 1.0). Reduce it if an overly stiff penalty makes pairs chatter;
                   the engine echoes penalty_scale and max_iterations under contact.
        interfaces : optional body-pair policy object with exactly these optional keys:
                   {"bonded":[[a,b],...], "contact":[[a,b],...],
                    "default":"contact"|"bonded"|"free"}.
                   Each a/b is a positive 1-based STEP solid ordinal or a body-name string.
                   Explicit pairs override default; omitted default is "contact" here
                   ("free" on ordinary run_fea_static). Example for a press-fit pin and
                   clearance bore: {"bonded":[["pin","carrier"]],
                   "contact":[["pin","bore"]], "default":"free"}.
                   Names resolve through the exported solid roster; unknown, ambiguous,
                   or multi-solid names are refused with an explicit ordinal remedy.
                   Ordinal-only objects pass unchanged to study.interfaces. The engine
                   validates mesh identity and refuses pairs in both lists. Every declared
                   pair preserves separate bodies through meshing.
        bond_gap_mm : optional nonnegative finite bonded-search limit in mm. It requires
                   interfaces on a contact run and limits only bonded pairs (zero means
                   touching); when omitted, use the measured pair gap within max_gap_mm.
                   It does not widen the clearance-contact search.
        closed_fit : default True: CAD-measured gaps below closed_fit_gap_mm
                   (default one quarter of the measured interface element size) are
                   initially closed, still compression-only and free tangentially.
                   This does NOT resolve the clearance: resolution.mode="closed_fit"
                   discloses the conformal-fit upper-bound pressure distribution.
                   False preserves the clearance-resolved model and its refusals.
        closed_fit_gap_mm : optional non-negative closure threshold in mm. Gaps
                   without an analytic CAD measurement are never idealized.
        mesh_size_mm : explicit BACKGROUND element size in mm; omitted uses the medium
                   fineness band. Do NOT use this to resolve a clearance — that is what
                   interface_size_mm is for, at a fraction of the element count.
        interface_size_mm : element size the CONTACT FACES are refined to, in mm, applied
                   as LOCAL refinement (fine cells seeded on the candidate faces, the rest
                   of the part left at the background size). It is a HARD CONTRACT, like
                   an explicit mesh_size_mm: no size floor may raise it silently — not the
                   element-count governor, not the fill's own graded lattice (whose finest
                   local cell is background/4), not polymesh's Cartesian grid-cell floor
                   on the background itself. The mesh is bisected AT the interface until
                   it is met, the delivered size is MEASURED, and a study that cannot
                   reach it is REFUSED naming what bound (the element ceiling with its
                   count, or the size that was achievable) instead of solving a coarser
                   interface. Pass about a quarter of the measured clearance: for a
                   0.35 mm keyway clearance, 0.09-0.14 mm. Resolving the same clearance
                   with mesh_size_mm instead means meshing the WHOLE part that fine — the
                   case that motivated this knob needed 1.16M elements that way.
        min_size_mm : the element-size band FLOOR in mm — the smallest element the
                   mesher may descend to anywhere. This is the knob for a small feature
                   AWAY from the interface (interface_size_mm owns the interface): when
                   a run is refused with "polymesh needs an element size of X mm ...
                   below this study's element-size band floor of Y mm", pass
                   min_size_mm just under X (0.5 for a 0.78 mm feature). It may not
                   exceed mesh_size_mm — a floor above the stated background size would
                   coarsen it instead of refining — and it costs elements and memory, so
                   pair it with a coarse mesh_size_mm.
        plasticity : opt-in SMALL-STRAIN J2 elastic-plastic material, same dict as
                   run_fea_static ({yield_mpa, hardening_mpa, max_increments, tol};
                   hardening_mpa is a TANGENT MODULUS in MPa, 0 = perfectly plastic).
                   THIS IS THE ANSWER TO A BEARING EDGE. A frictionless linear-elastic
                   contact cannot redistribute: whatever arc the geometry closes takes
                   the whole load at whatever pressure that implies, and on a sharp
                   seat that is a number the steel could never carry (the case this
                   exists for: 1446 N through 12 node pairs on a hinge saddle,
                   450 MPa mean against a 276 MPa yield, with no mechanism in the model
                   by which it could spread). With plasticity on, the ACTIVE-SET LOOP
                   RUNS INSIDE EACH LOAD INCREMENT — contact state and plastic state
                   both advance per increment — so the edge yields and the patch grows
                   within the increment instead of one lagging the other. Measured on
                   the pin-on-edge gate at 16 kN: contact_area_mm2 19.6 -> 70.3 (3.6x)
                   and mean_contact_pressure_mpa 810 -> 226 (3.2x yield -> 0.9x yield).
                   Read contact.contact_area_mm2 and contact.mean_contact_pressure_mpa
                   against the elastic run, and the top-level "plasticity" block
                   (max_equivalent_plastic_strain, plastic_fraction, yielded_nodes,
                   load_fraction, converged) for how much of the part paid for it. A
                   peak plastic strain over 5 % raises a "warnings" entry: the mesh is
                   never updated, so past that the patch the solve spread the load over
                   is not the patch the part has.
        mesh_order : 1 = tet4 (default, fastest), 2 = tet10.
        vtu_path : optional .vtu path for the solved fields, as for
                   run_fea_static (displacement, von_mises and the summary_node mask).

        THE INTERFACE RESOLUTION REFUSAL. A contact study can converge, balance to 0.0%
        equilibrium error and look plausible while being a point-load artefact: the live
        case was a 0.350 mm keyway meshed at 2.91 mm at the interface, which converged
        with 1 of 848 candidate pairs active, 2902 N through that one node pair and
        13,543 MPa of reported "contact pressure". So the run is REFUSED (ok:false) when
        either
          * the interface is not resolved a priori. WHICH test applies is reported as
            resolution.criterion, because it depends on whether there is a clearance at
            all: "h_over_clearance" (min gap > 0) refuses when the element size AT the
            interface is more than 4x the clearance AND fewer than 6 candidate pairs sit
            within the SEAT LIMIT; "elements_across_patch" (TOUCHING or
            coincident faces, min gap <= 0, where h/gap is undefined) refuses when fewer
            than 3 elements span the candidate patch (h_interface > max_h_interface_mm =
            sqrt(candidate_area_mm2)/3) AND fewer than 6 pairs seat, or
          * the solved load crossed through fewer than 6 active pairs.
        The refusal carries "contact":{... "resolution":{h_interface_mm, gap_mm,
        criterion, candidate_pairs, seatable_pairs, seat_limit_mm,
        initial_penetration_max_mm, min_active_pairs,
        candidate_area_mm2, required_interface_size_mm, resolved, reason, plus
        ratio/max_ratio under h_over_clearance or max_h_interface_mm /
        min_elements_across_patch under elements_across_patch}} and names the
        interface_size_mm that would resolve it. A conformal interface (two flat faces
        across a uniform gap, a concentric pin in a bore) is NOT refused on the ratio
        alone — every pair seats there and the patch pressure is real.

        WHAT SEATS. seat_limit_mm is a LENGTH the interface supplies —
        max(0.5 x h_interface, 0.25 x max_gap_mm, 1e-4) — and seatable_pairs is how many
        candidates start the solve inside it. Half the interface element size is the
        resolution statement: the elements bounding the interface cannot describe a gap
        finer than that, so a candidate below it is not distinguishable from touching and
        the active-set loop begins there; every other candidate seats later if and only
        if the deformation actually closes it, and a seated pair that ends in tension is
        released. The limit used to be twice the CLOSEST APPROACH, which collapses to
        zero the moment one node lands inside the partner face — on a nominally-touching
        fit that made the seated population mesh luck (the same live request seated 8,
        then 12, then 13, then 2 pairs). Initial PENETRATION is never force: a negative
        mesh gap is placement/facet error, it is clamped to a touching gap, and its depth
        is reported as resolution.initial_penetration_max_mm. If a run reports a large
        initial_penetration_max_mm and few active pairs, the bodies were placed
        interfering and what looked like contact pressure before was preload.

        A SELECTION MAY NOT SPAN THE INTERFACE. No fixture and no load may resolve to
        nodes on BOTH bodies of a contact pair. The tolerance that finds the nodes of a
        face is routinely WIDER than the clearance a contact study exists to open and
        close: a plane or cylinder/quadric faceset carries perp_tol_mm = 1.5 mm, so
        aimed at one side of a 0.35 mm fit it reaches straight through and resolves the
        mating body's face too. A fixture that holds both sides of an interface grounds
        the joint through the SELECTION instead of through the contact, and then nothing
        downstream can see it — the mechanism gate is satisfied (both bodies reach a
        fixture), the interface is meshed fine, and the fixtures react everything, so
        the run reports CONVERGED at a 0% equilibrium error while the contact carries
        almost nothing (the live keyway isolate: 9.9 N of a 1500 N load, 6 active
        pairs). So the run is REFUSED (ok:false) naming the offending selector, the two
        bodies it captured, how many of its nodes landed on each, and the remedy:
        "selection_spans_contact":{kind, selector, bodies:[i,j], nodes_captured:[a,b],
        body_nodes:[a,b], median_gap_mm, min_gap_mm}. Fix it by tightening the face
        filter's perp_tol_mm below the clearance, or by selecting by body/face id so
        the selection cannot leave its own body. Two selectors, one per body, are fine
        — that is a properly grounded assembly whose interface still transmits load;
        what is refused is ONE selector holding both sides. When a filter's perp_tol_mm
        is merely WIDER than max_gap_mm the reply carries a "SELECTION WARNING
        (pre-solve)" note before the solve starts, so the mistake does not cost a
        meshing round trip.

        Returns the run_fea_static envelope — summary (max_von_mises_mpa / max_disp_mm /
        min_fos, plus the reduction disclosure max_von_mises_reduction /
        max_von_mises_raw_mpa / min_fos_raw: the reported peak skips two element rings
        around ideal fixtures, which on a contact study is usually exactly where the
        load path lands), mesh_quality, reactions (check equilibrium_error_pct first),
        plausibility, provenance, vtu_path — PLUS "contact":
          {candidate_pairs, active_pairs, contact_iterations, converged, max_gap_mm,
           interface_size_mm, penalty_scale, penalty_stiffness_n_per_mm, max_iterations,
           tangential_stabilization, tangential_force_fraction, candidate_area_mm2,
           contact_area_mm2, mean_contact_pressure_mpa, contact_pressure_reliable,
           resolution:{...}, held_by_contact_only:[component ids held ONLY by contact],
           contact_carries_load, load_path:{applied_n, carried_n, fraction, note},
           pairs:[{bodies:[i,j], candidate_pairs, active_pairs, contact_force_n,
              force_vector_n:[x,y,z], contact_area_mm2, mean_contact_pressure_mpa,
              max_contact_pressure_mpa, max_penetration_mm}]}
        contact.interfaces reports bonded/contact lists of {a,b,pairs,area_mm2} and
        unmatched declared pairs; inspect these to verify the intended mixed load path.
        contact_area_mm2 is the summed TRIBUTARY area of the pairs that carried load
        (one side of the interface, each node counted once), so
        contact_force_n / contact_area_mm2 is a mean patch pressure — reported as
        mean_contact_pressure_mpa. contact_pressure_reliable says whether that division
        means anything: it is true only when the interface passed the resolution guard
        and at least 6 pairs with real tributary area carried the load. When it is false,
        quote the resultant (contact_force_n) and not a pressure.
        THE PRESSURE IS ALSO ONLY AN ANSWER WHEN THE CONTACT CARRIES THE LOAD.
        load_path reports what the study actually routed through this interface:
        applied_n is the resultant of the applied loads on the components held ONLY
        by contact, carried_n the total contact_force_n across the active pairs, and
        fraction their quotient. contact_carries_load — and therefore
        contact_pressure_reliable — is true only when that fraction reaches 0.5.
        Two ways it fails. If held_by_contact_only is EMPTY, every body is grounded
        on its own, so the interface is a REDUNDANT parallel path: the study is not
        refused, but contact_carries_load is false and force/area is the share this
        stiffness path picked up, not a seat pressure (the usual cause is a fixture
        face set that reached across the clearance onto the mating body — see the
        selection rule above). If a body IS held only by contact and the interface
        still takes less than half of what is applied to it, the study is REFUSED:
        a converged solve that routes the load elsewhere answers a different
        question. load_path.note carries the diagnosis in both cases and is empty
        exactly when the interface is the load path.
        force_vector_n is what the lower-numbered component pushes onto the higher one, so
        its direction says WHICH side is in contact. A load that pulls the bodies apart
        leaves nothing holding them: that is refused as a mechanism, not solved.
        Tip: query_geometry() first to confirm the fixture/load faces resolve, and
        check_interference() to measure the clearance you are passing as max_gap_mm —
        then pass a quarter of it as interface_size_mm.
        """
        try:
            bc_notes: list = []
            ctx = _build_contact_study(
                fixtures,
                load,
                material,
                max_gap_mm,
                max_iterations,
                mesh_size_mm,
                mesh_order,
                doc_id,
                bc_notes,
                vtu_path=vtu_path,
                interface_size_mm=interface_size_mm,
                min_size_mm=min_size_mm,
                plasticity=plasticity,
                interfaces=interfaces,
                penalty_scale=penalty_scale,
                bond_gap_mm=bond_gap_mm,
                closed_fit=closed_fit,
                closed_fit_gap_mm=closed_fit_gap_mm,
            )
            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            return _dumps(
                _run_solve(
                    "fea_contact",
                    "core_rpc",
                    {"method": "sim_run_static", "params": {"study": ctx["study"]}},
                    _job_meta(ctx, bc_notes),
                    background=False,
                    doc_id=ctx["doc"],
                )
            )
        except _StudyRefusal as r:
            return _dumps(r.payload)
        except CoreError as e:
            return _err(e)
        except Exception as e:  # noqa: BLE001
            return _err(e)
