"""CAD simulation MCP tools (T-RF8 split of cad_tools.py): run_fea_* + run_cfd_*."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional
from typing import Union
import math
import os
import re
import time

from .cad_common import (
    CoreError,
    coerce_json_or_repr,
    require_list,
    require_mapping,
    _autosave_all_open,
    _canonical_material_keys,
    _default_conductivity,
    _default_specific_heat,
    _doc,
    _dumps,
    _err,
    _merge_face_sets,
    _persist_study,
    _resolve_face_points,
    _resolve_material_name,
    _resolve_material,
    _sid_match,
    collect_durable,
    register_durable_collector,
    solve_durable_or_handoff,
    _tmp,
    _SID_RE,
    _SYNC_SOLVE_BUDGET_S,
    cb,
)
from services.sim import jobs as simjobs


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
# see services/agent/arg_reconciliation.QUANTITY_MISMATCH for why (mesh_size_mm is refused, not
# mapped onto the dimensionless mesh_fineness). Every rename here is echoed in the tool's
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
        # Merge multiple face-point sets for this load (keeps face_normal/surface
        # hints exactly when they stay valid — see _merge_face_sets).
        merged = _merge_face_sets(_sim_face_points(d, ld["face"]))
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
    fixed = {
        tuple(float(c) for c in point)
        for fs in fixture_sets
        for point in (fs.get("points") or [])
        if isinstance(point, (list, tuple)) and len(point) == 3
    }
    for i, spec in enumerate(load_specs):
        points = spec.get("faces", {}).get("points") or []
        loaded = {
            tuple(float(c) for c in point)
            for point in points
            if isinstance(point, (list, tuple)) and len(point) == 3
        }
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
    rather than reporting a partial success, so the only thing to check here is that the
    caller really gave an absolute path — a relative one would land in whatever directory
    the engine process happens to sit in, which is not a releasable artifact path.
    """
    if vtu_path is None:
        return None
    if not isinstance(vtu_path, str) or not vtu_path.strip():
        raise ValueError("vtu_path must be a non-empty absolute filesystem path.")
    vtu_path = vtu_path.strip()
    if not os.path.isabs(vtu_path):
        raise ValueError(f"vtu_path must be ABSOLUTE, got {vtu_path!r}.")
    return vtu_path


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
) -> dict:
    """SETUP stage for run_fea_static / sim_run(analysis="fea_static")."""
    d = _doc(doc_id)
    _require_sim_geometry(d)
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
    vtu_path = _norm_vtu_path(vtu_path)
    mat = _resolve_study_material(material, "static")
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
        fixture_sets.extend(_sim_face_points(d, q))
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

    study = {
        "step_path": step,
        "material": mat,
        "fixtures": fixture_sets,
        "loads": load_specs,
        "mesh_order": 2,
        "fineness": mesh_fineness,
    }
    if vtu_path:
        study["vtu_path"] = vtu_path
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
        "vtu_path": vtu_path,
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
#   mesh_quality  aspect/jacobian extremes, pct_poor, min tet volume, node/elem counts, order
#   reactions     applied vs reaction force/moment plus equilibrium_error_pct (static family
#                 only — a modal or thermal solve has no applied load to balance)
#   provenance    engine_build_id / polymesh_commit / solver / mesher / timestamp_utc
#   vtu_path      the .vtu the solve wrote, present only when the study requested one
# They are forwarded VERBATIM into the reply and into the persisted study: an evidence packet
# must show what the solver reported, not a value this file re-derived. A key the engine did
# not send stays ABSENT rather than becoming a null the reader has to interpret.
_EVIDENCE_KEYS = ("mesh_quality", "reactions", "provenance", "vtu_path")


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
            f"sim_cancel('{jid}') really terminates the solver."
        ),
        "next_actions": [f"get_job('{jid}')", f"sim_cancel('{jid}')"],
    }


def _run_solve(
    kind: str, stage: str, payload: dict, meta: dict, *, background: bool, doc_id
) -> dict:
    """Start one durable solve, and either hand back its id or wait out the sync budget."""
    if background:
        return _started(simjobs.start(kind, stage, payload, meta), doc_id)
    return solve_durable_or_handoff(kind, stage, payload, meta)


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


def _convergence_row(step: dict, out: dict, monitor: str, prev: Optional[float]) -> dict:
    """One ladder row from a collected static envelope, plus its delta against the previous.

    A leg that failed keeps its row (with `error`) instead of vanishing: a table missing its
    finest mesh silently reads as "converged at the coarse end".
    """
    row = {
        "size_mm": step.get("target_size_mm"),
        "fineness": step.get("fineness"),
        "mesh_order": step.get("mesh_order"),
    }
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
    if isinstance(out.get("mesh_quality"), dict):
        row["mesh_quality"] = out["mesh_quality"]
    if isinstance(out.get("reactions"), dict):
        row["equilibrium_error_pct"] = out["reactions"].get("equilibrium_error_pct")
    if out.get("vtu_path"):
        row["vtu_path"] = out["vtu_path"]
    value = summary.get(monitor)
    row["monitored_value"] = value
    # A zero previous value has no percentage change (division by zero is not "0% drift"),
    # so the delta is honestly absent and the tolerance check cannot pass on it.
    if prev in (None, 0.0) or not isinstance(value, (int, float)):
        row["delta_pct_vs_prev"] = None
    else:
        row["delta_pct_vs_prev"] = round(100.0 * (value - prev) / abs(prev), 3)
    return row


def _convergence_envelope(
    doc_id, monitor: str, tol_pct: float, rows: list, notes: list
) -> dict:
    """The convergence verdict over a finished ladder — never a PASS on a partial table."""
    failed = [i for i, r in enumerate(rows) if r.get("error")]
    last_delta = rows[-1].get("delta_pct_vs_prev") if rows else None
    converged = (
        not failed
        and len(rows) >= 2
        and isinstance(last_delta, (int, float))
        and abs(last_delta) < tol_pct
    )
    out = {
        "ok": not failed,
        "doc_id": doc_id,
        "monitor": monitor,
        "tol_pct": tol_pct,
        "table": rows,
        "n_meshes": len(rows),
        "converged": converged,
        "final_delta_pct": last_delta,
        "note": (
            "converged is |delta of the FINEST leg| < tol_pct on the monitored value; "
            "a peak that keeps growing with refinement is a singularity, not strength."
        ),
    }
    if failed:
        out["error"] = (
            f"{len(failed)} of {len(rows)} convergence legs failed (rows "
            f"{failed}); the table is retained for diagnosis and is NOT a convergence "
            "statement."
        )
    return _interpreted(out, notes)


def _convergence_leg_solve(payload: dict, meta: dict, deadline: float) -> tuple:
    """Solve one ladder leg as a durable job, returning ``(job_id, envelope | None)``.

    Same three primitives `solve_durable_or_handoff` drives (start, status, collect), but
    the JOB ID comes back for EVERY leg and the whole ladder shares ONE transport deadline
    instead of taking the sync budget N times over. Both matter: a collected envelope
    carries no id, and without one a ladder whose tail overran could never be reassembled
    into a single table. `None` means "still running at the deadline" — the leg is left
    solving in its own process, exactly as a handoff does.
    """
    jid = simjobs.start("fea_static", "core_rpc", payload, meta)["job_id"]
    interval = 0.02
    while time.time() < deadline:
        st = simjobs.status(jid)
        state = st.get("state")
        if state == simjobs.DONE:
            return jid, collect_durable(jid, st)
        if state in (simjobs.FAILED, simjobs.CANCELLED):
            return jid, {
                "ok": False,
                "job_id": jid,
                "state": state,
                "error": st.get("error") or f"solve {state}",
            }
        time.sleep(interval)
        interval = min(0.4, interval * 1.5)
    return jid, None


def _convergence_from_jobs(job_ids, monitor: str, tol_pct: float) -> dict:
    """Assemble the table from background legs started by an earlier call.

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
        if st.get("state") != simjobs.DONE:
            pending.append({"job_id": jid, "state": st.get("state"), **step})
            continue
        legs.append((step, collect_durable(str(jid), st)))
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
        if isinstance(row.get("monitored_value"), (int, float)):
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
    """CAD simulation MCP tools (T-RF8 split of cad_tools.py): run_fea_* + run_cfd_*."""

    # ---------------------------------------------------------------- simulation

    @mcp.tool()
    def run_fea_static(
        fixtures: Union[list, str],
        loads: Union[list, str],
        material: Union[str, dict] = "steel",
        mesh_fineness: Union[float, str] = 0.5,
        learned_advisor: bool = False,
        advisor_max_dof: int = 0,
        doc_id: Optional[str] = None,
        vtu_path: Optional[str] = None,
        background: bool = False,
    ) -> str:
        """Linear-static stress FEA on the current part. Returns max stress, displacement, and FoS.

        fixtures : list of face queries held fixed, e.g. ["bottom"] or
                   [{"of":"face","side":"bottom","pick":"largest"}].
        loads    : list of {face query + a force or pressure}, e.g.
                   [{"face":"top","force":[0,0,-500]}]   # newtons, world XYZ
                   [{"face":"top","pressure":0.5}]       # MPa, inward
                   [{"face":"top","magnitude":500,"direction":[0,0,-1]}]  # polar form of the
                   #   force above — direction is normalized, so its length never scales the load
                   Plural "faces":[q1,q2] is accepted and applies the SAME load to each face.
                   A face query is query_geometry vocabulary ("top", {"of":"face",...}) or a face
                   SID from describe_part / query_geometry ("F58db6d") — a FEATURE id is not a face.
        material : a name ("steel","aluminum","titanium","stainless","brass","abs","pla") or an
                   explicit dict {E_mpa, nu, density_g_cm3, yield_mpa, ...}.
        mesh_fineness : 0..1 (default 0.5), or a preset word
                   ("coarse"=0.25, "medium"=0.5, "fine"=0.75, "finest"=1.0).
                   Second-order (tet10) elements are used for accuracy.
        learned_advisor : opt-in Polyhedral-FEA sizing/order advice; false by default.
        advisor_max_dof : optional non-negative advisor budget; 0 leaves it unset.
        background : if True, return a job_id immediately and solve in a separate process —
                     poll get_job(job_id). Use it for fine meshes that may exceed the call
                     timeout. The job is persisted on disk, so it survives an MCP restart
                     and can be collected in a later turn; sim_cancel(job_id) kills it.
        vtu_path : optional ABSOLUTE path for a VTK XML UnstructuredGrid (.vtu, ASCII) the
                   solver writes after the solve — point data displacement (mm, 3-vector)
                   and von_mises (MPa) on the solved tet4/tet10 mesh, for ParaView or an
                   independent cross-check. Echoed back as "vtu_path"; a write failure
                   fails the whole run rather than reporting a partial success.

        Returns {ok, doc_id, material, n_nodes, n_elems, mesh, summary:{...}} — the
        scalars live under "summary", NOT at the top level: summary.min_fos,
        summary.max_von_mises_mpa, summary.max_disp_mm (plus tresca/rankine/principal
        variants). min_fos = yield / max stress; > 1 holds. min_fos saturates at a
        15.0 cap, reported in "fos_note" — a capped value means "at least this", not
        "exactly this".
        Release evidence rides BESIDE "summary": "mesh_quality" (aspect/jacobian extremes,
        pct_poor, node/elem counts, order), "reactions" (applied vs reaction force/moment
        plus equilibrium_error_pct — check it before trusting a stress), and "provenance"
        (engine_build_id, polymesh_commit, solver, mesher, timestamp_utc).
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
        vtu_path : optional ABSOLUTE .vtu path for the FINAL adapted mesh (ASCII VTK XML
                   UnstructuredGrid; point data displacement in mm and von_mises in MPa).
        The reply carries "mesh_quality", "reactions" (with equilibrium_error_pct) and
        "provenance" beside "summary", as run_fea_static does.
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
        mesh_order: int = 2,
        monitor: Optional[str] = None,
        tol_pct: float = _CONVERGENCE_TOL_PCT,
        doc_id: Optional[str] = None,
        background: bool = False,
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
                   e.g. [4, 2, 1]. This is the runbook spelling (`-h` in the polymesh CLI).
        fineness : the alternative spelling — a strictly increasing list on the 0..1
                   mesh_fineness scale, e.g. [0.25, 0.5, 0.75] or ["coarse","medium","fine"].
                   Pass exactly one of sizes_mm / fineness; at least 2 entries either way.
        mesh_order : 1 (tet4) or 2 (tet10, default). Keep it constant across the ladder —
                   changing element order mid-study is a different discretization family.
        monitor : which `summary` scalar the convergence is judged on; default
                   "max_von_mises_mpa". Also accepted: max_disp_mm, min_fos,
                   max_tresca_mpa, max_rankine_mpa, max_principal_mpa, min_principal_mpa.
                   Monitor a NON-singular location's driver: a re-entrant corner peak
                   diverges by construction and will never settle.
        tol_pct : convergence tolerance in percent of the previous leg's monitored value
                   (default 5).
        background : if True, start every leg as its own durable job and return their
                   job_ids immediately; assemble the table afterwards with
                   run_fea_convergence(job_ids=[...]). A synchronous call that overruns the
                   transport budget switches to exactly this shape by itself, so a long
                   ladder is never lost.
        job_ids : assemble the table from legs already started (ladder positions come from
                   the jobs' own metadata, so the order you pass them in does not matter).
                   fixtures/loads are NOT needed on this call — the setup is already baked
                   into the jobs. They are required only when starting a new ladder.

        Returns {ok, monitor, tol_pct, converged, final_delta_pct, n_meshes,
        table:[{size_mm|fineness, mesh_order, n_dof, n_nodes, n_elems,
        max_von_mises_mpa, max_disp_mm, min_fos, monitored_value, delta_pct_vs_prev,
        mesh_quality, equilibrium_error_pct}]}. `converged` is |the FINEST leg's delta| <
        tol_pct. A failed leg keeps its row and forces ok:false — a table missing its
        finest mesh would read as "converged at the coarse end".
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
            if job_ids is not None:
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
            )
            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            rows: list = []
            job_ids_out: list = []
            pending: list = []
            prev: Optional[float] = None
            deadline = time.time() + _SYNC_SOLVE_BUDGET_S
            for i, value in enumerate(ladder):
                study = {**ctx["study"], "mesh_order": mesh_order, key: value}
                step = {
                    "index": i,
                    "mesh_order": mesh_order,
                    "target_size_mm": value if key == "target_size_mm" else None,
                    "fineness": study["fineness"],
                }
                leg_meta = _job_meta(
                    {**ctx, "study": study}, bc_notes, {"convergence_step": step}
                )
                payload = {"method": "sim_run_static", "params": {"study": study}}
                if background or pending:
                    # Either the caller asked for jobs, or an earlier leg used up the
                    # transport budget — then every REMAINING leg is queued too, so the
                    # ladder is collectable as one table instead of half-solved.
                    jid = simjobs.start("fea_static", "core_rpc", payload, leg_meta)[
                        "job_id"
                    ]
                    job_ids_out.append(jid)
                    pending.append(jid)
                    continue
                jid, out = _convergence_leg_solve(payload, leg_meta, deadline)
                job_ids_out.append(jid)
                if out is None:
                    pending.append(jid)
                    continue
                row = _convergence_row(step, out, monitor, prev)
                if isinstance(row.get("monitored_value"), (int, float)):
                    prev = float(row["monitored_value"])
                rows.append(row)
            if pending:
                return _dumps(
                    {
                        "ok": True,
                        "doc_id": ctx["doc"],
                        "state": "running",
                        "job_ids": job_ids_out,
                        "pending_job_ids": pending,
                        "monitor": monitor,
                        "tol_pct": tol_pct,
                        "completed_legs": rows,
                        "cancel_supported": True,
                        "note": (
                            f"{len(pending)} of {len(job_ids_out)} convergence legs are "
                            "solving in SEPARATE processes; the jobs are persisted on disk "
                            "and survive an MCP restart. Poll sim_status(job_id), then "
                            "assemble the whole table with run_fea_convergence(job_ids="
                            "[...]) using job_ids above — every leg, finished ones "
                            "included; their envelopes are replayed, not re-solved."
                        ),
                        "next_actions": [
                            f"sim_status('{pending[0]}')",
                            f"run_fea_convergence(job_ids={job_ids_out!r}, "
                            f"monitor='{monitor}')",
                        ],
                    }
                )
            return _dumps(
                _convergence_envelope(ctx["doc"], monitor, tol_pct, rows, bc_notes)
            )
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
        vtu_path : optional ABSOLUTE .vtu path; the modal write carries the mode shapes as
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
        """State of a sim_run job: queued|running|done|failed|cancelled, elapsed, progress.

        The state is reconciled against the OPERATING SYSTEM, not just a stored flag: a
        worker that died without writing a result is reported as `failed` with its last
        output, never as `running` and never as `done`.
        """
        try:
            st = simjobs.status(job_id)
            if st.get("state") == "unknown":
                return _dumps(st)
            st.pop(
                "meta", None
            )  # the resolved study setup is bulky and not useful here
            state = st["state"]
            if state == "done":
                st["next_actions"] = [f"sim_results('{job_id}')"]
            elif state == "running":
                st["next_actions"] = [
                    f"sim_status('{job_id}')",
                    f"sim_results('{job_id}', partial=True)",
                    f"sim_cancel('{job_id}')",
                ]
                st["note"] = (
                    "still solving — nothing has converged yet; sim_results with "
                    "partial=True returns the last snapshot, clearly marked partial"
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
        """
        try:
            return _dumps(simjobs.cancel(job_id))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sim_results(job_id: str, partial: bool = False) -> str:
        """Collect a simulation job's results — from ANY tool that returned a job_id.

        partial=False (default): the full converged result, in exactly the shape the
        synchronous tool returns. On a job that has NOT finished this REFUSES — it will not
        hand back half a solution dressed as an answer.

        partial=True: the last snapshot the solver published, always tagged
        `partial: true, converged: false`. When the stage published nothing (a direct FEA
        solve has no meaningful intermediate state) this says so instead of inventing one.

        The collect stage runs exactly ONCE per job however many times it is polled and from
        however many processes, so a solved study is written into the document once.
        """
        try:
            st = simjobs.status(job_id)
            state = st.get("state")
            if state == "unknown":
                return _dumps(st)
            if partial:
                snap = simjobs.partial(job_id)
                base = {
                    "job_id": job_id,
                    "state": state,
                    "partial": True,
                    "converged": False,
                    "elapsed_s": st.get("elapsed_s"),
                }
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
            if state != "done":
                out = {
                    "ok": False,
                    "job_id": job_id,
                    "state": state,
                    "elapsed_s": st.get("elapsed_s"),
                    "error": f"job {job_id} is {state} — there are no converged results to "
                    "return. Refusing to answer with a partial or absent solution.",
                }
                if state in ("running", "queued"):
                    out["hint"] = (
                        f"poll sim_status('{job_id}'), or "
                        f"sim_results('{job_id}', partial=True) for the last "
                        "snapshot (clearly marked partial)"
                    )
                elif st.get("error"):
                    out["solver_error"] = st["error"]
                return _dumps(out)
            out = dict(collect_durable(job_id, st))
            out.update(
                job_id=job_id,
                state="done",
                converged=bool(out.get("ok")),
                elapsed_s=st.get("elapsed_s"),
            )
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def sim_jobs(limit: int = 20) -> str:
        """List recent sim_run jobs (newest first) with their reconciled state and elapsed
        time. Jobs persist across agent turns, so this also shows solves an earlier turn
        started."""
        try:
            limit = _positive_int(limit, "limit")
            rows = []
            for j in simjobs.list_jobs(limit):
                meta = j.pop("meta", None) or {}
                j["doc_id"] = meta.get("doc")
                if meta.get("label"):
                    j["label"] = meta["label"]
                rows.append(j)
            return _dumps(
                {
                    "ok": True,
                    "jobs": rows,
                    "count": len(rows),
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
    # T-BL04 — fatigue / thermal / buckling / contact FEA
    # Fatigue routes to the C++ `sim_run_fatigue` solver (same posture as
    # run_fea_static→sim_run_static). Thermal/buckling/contact route to a
    # CalculiX (ccx) subprocess — GPL-2.0, subprocess-only, PATH-discovered,
    # never linked/bundled (contracts/licenses.md) — and degrade HONESTLY
    # (skipped:true) when ccx is absent. See services/mcp/fea_advanced.py.
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
        """Thermal (heat-conduction) FEA — steady-state via CalculiX, TRANSIENT via the
        C++ core solver on the real part mesh.

        STEADY (default, transient=None): solves ∇·(k∇T)=0 with the given faces held at
        fixed temperatures (Dirichlet) via a ccx subprocess. Because arbitrary CAD meshing
        needs the FEA mesher, this ccx path solves on a PRISMATIC BOX PROXY sized to the
        part's bounding box with a 1-D conduction setup (hot end vs cold end) — an
        engineering estimate honest for bar/plate-like parts. CalculiX is GPL-2.0 →
        subprocess-only, PATH-discovered; absent ccx ⇒ {ok:false, skipped:true}.

        TRANSIENT (T-FIN18, pass a `transient` dict): solves C dT/dt + K T = f(t) with
        implicit backward Euler on the REAL tet mesh of the part (C++ core, same solver
        family as run_fea_static — no box proxy, no ccx needed).
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
        learned_advisor : default-off Polyhedral-FEA mesh advice for the transient
                          real-mesh route only. It does not promote the static-trained
                          model as a thermal optimizer.
        advisor_max_dof : optional non-negative advisor budget; 0 leaves it unset.
        vtu_path : TRANSIENT ONLY — optional ABSOLUTE .vtu path (ASCII VTK XML
                   UnstructuredGrid) with the nodal temperature field as point data. The
                   steady route is a CalculiX box proxy, not the part mesh, so it refuses
                   this rather than writing a .vtu of a proxy. A transient reply also
                   carries "mesh_quality" and "provenance" beside "summary".
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
            vtu_path = _norm_vtu_path(vtu_path)
            advisor = _advisor_params(learned_advisor, advisor_max_dof)
            if learned_advisor and transient is None:
                raise ValueError(
                    "learned_advisor is available only on the transient real-mesh "
                    "thermal route; the steady route is a CalculiX box proxy and "
                    "does not run the selected mesher."
                )
            if vtu_path and transient is None:
                raise ValueError(
                    "vtu_path is available only on the transient real-mesh thermal "
                    "route; the steady route solves a CalculiX box proxy, so its field "
                    "is not the part's mesh and must not be released as one."
                )
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
                    "thermal_transient",
                    conductivity_w_mk=conductivity_w_mk,
                    specific_heat_j_kgk=specific_heat_j_kgk,
                )
                k_cond = mat["conductivity_w_mk"]
                cp = mat["cp_j_kgk"]

                def _faceset(q):
                    merged = _merge_face_sets(_sim_face_points(d, q))
                    return merged if merged["points"] else None

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
                    thermal_driving = thermal_driving or abs(temp - t_init) > 0.0

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
                        thermal_driving = thermal_driving or abs(ambient - t_init) > 0.0
                    load_specs.append(spec)

                if not thermal_driving:
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
                study = {
                    "step_path": step,
                    "material": {**mat, "k_w_mk": k_cond, "cp_j_kgk": cp},
                    "fixtures": fixtures,
                    "loads": load_specs,
                    "mesh_order": 2,
                    "fineness": mesh_fineness,
                    "transient": {
                        "t_end_s": t_end,
                        "dt_s": dt,
                        "t_init_c": t_init,
                        "frame_times_s": frame_times,
                        "capacitance": capacitance,
                    },
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
                            "error": res.get("error", "transient thermal solve failed"),
                        }
                    )
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

            if loads:
                raise ValueError(
                    "steady thermal box-proxy does not support flux/convection loads; pass "
                    "transient={...} to run them on the real part mesh instead of silently "
                    "dropping loads."
                )
            if len(hot_faces) != 1 or len(cold_faces) != 1:
                raise ValueError(
                    "steady thermal box-proxy supports exactly one hot face and one cold "
                    "face; use transient={...} for multiple real-mesh boundary faces."
                )
            for label, entry in (
                ("hot_faces[0]", hot_faces[0]),
                ("cold_faces[0]", cold_faces[0]),
            ):
                if (
                    not isinstance(entry, dict)
                    or "face" not in entry
                    or "temperature" not in entry
                ):
                    raise ValueError(f"{label} needs face + temperature.")
                _reject_ignored_fields(entry, {"face", "temperature"}, label)
            T_hot = _finite_float(
                hot_faces[0]["temperature"], "hot_faces[0].temperature"
            )
            T_cold = _finite_float(
                cold_faces[0]["temperature"], "cold_faces[0].temperature"
            )
            if T_hot == T_cold:
                raise ValueError(
                    "hot and cold temperatures are equal, so the steady solution is a "
                    "constant input field with zero heat flow; provide a non-zero temperature "
                    "difference."
                )
            from . import fea_advanced as fa

            if fa.ccx_path() is None:
                return _dumps(fa._ccx_absent_result("thermal steady-state"))
            d = _doc(doc_id)
            mat = _resolve_study_material(
                material, "thermal_steady", conductivity_w_mk=conductivity_w_mk
            )
            k_cond = mat["conductivity_w_mk"]
            hot_set = _merge_face_sets(_sim_face_points(d, hot_faces[0]["face"]))
            cold_set = _merge_face_sets(_sim_face_points(d, cold_faces[0]["face"]))
            if not hot_set["points"] or not cold_set["points"]:
                raise ValueError(
                    "hot/cold face queries must both resolve to surface points."
                )

            def _centroid(points):
                return [
                    sum(_finite_float(p[i], "face point") for p in points) / len(points)
                    for i in range(3)
                ]

            hot_c = _centroid(hot_set["points"])
            cold_c = _centroid(cold_set["points"])
            L = math.sqrt(sum((a - b0) ** 2 for a, b0 in zip(hot_c, cold_c)))
            if not math.isfinite(L) or L <= 1e-9:
                raise ValueError(
                    "hot and cold face centroids coincide; the 1-D box proxy cannot define "
                    "a conduction length from those selections."
                )
            _, b, h = _part_bbox_dims(d)
            gen = fa.build_thermal_bar_inp(L, b, h, k_cond, T_hot, T_cold)
            # Inner subprocess clock, deliberately shorter than this tool's outer
            # harness timeout_s — see fea_advanced's layered-timeout block.
            run = fa.run_ccx(
                gen["inp"], job="thermal", timeout=fa.HARNESS_CCX_TIMEOUT_S
            )
            if run.get("skipped"):
                return _dumps(run)
            if not run.get("ok") or not run.get("dat_path"):
                return _dumps(
                    {
                        "ok": False,
                        "error": run.get("stderr", "")[-400:]
                        or run.get("error", "ccx thermal run failed"),
                        "backend": run.get("backend"),
                    }
                )
            mid_T = fa.parse_dat_nodal(
                run["dat_path"], "NT", gen["mid_nodes"], component=1
            )
            if mid_T is None or not math.isfinite(mid_T):
                return _dumps(
                    {
                        "ok": False,
                        "doc_id": d,
                        "backend": run["backend"],
                        "error": "CalculiX completed but no finite mid-plane "
                        "temperature could be parsed; refusing a false-success "
                        "null result.",
                    }
                )
            out = {
                "ok": True,
                "doc_id": d,
                "backend": run["backend"],
                "solver": "CalculiX *HEAT TRANSFER, STEADY STATE (box proxy)",
                "conductivity_w_mk": k_cond,
                "T_hot_c": T_hot,
                "T_cold_c": T_cold,
                "boundary_separation_mm": L,
                "mid_plane_T_c": round(mid_T, 3),
                "expected_mid_T_c": round(gen["expected_mid_T"], 3),
                "note": "1-D box proxy runs between the selected face centroids; "
                "ccx GPL-2.0 subprocess.",
            }
            sid = _persist_study(
                d,
                {
                    "type": "fea_thermal",
                    "name": f"Thermal ({mat.get('name', material)})",
                    "engine": "calculix",
                    "results": out,
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
        fixtures: list,
        load: Union[list, str],
        material: Union[str, dict] = "steel",
        n_modes: int = 4,
        doc_id: Optional[str] = None,
    ) -> str:
        """Linear (eigenvalue) buckling via CalculiX (ccx subprocess) — critical load
        factor for slender members / thin panels.

        Returns the buckling load FACTORS λ (the applied load × λ = the critical load
        Pcr). λ<1 means the part buckles BELOW the applied load — unsafe. Like the
        thermal tool this solves a BOX PROXY sized to the part bounding box (a
        fixed-free column under axial compression), an Euler-grade estimate honest for
        column/strut-like parts.

        fixtures : face(s) held fixed (the mounted/clamped end), e.g. ["bottom"].
        load     : the compressive load as a single {face + force}, e.g.
                   [{"face":"top","force":[0,0,-1000]}] — magnitude is what λ scales. The
                   polar form [{"face":"top","magnitude":1000,"direction":[0,0,-1]}] is
                   equivalent (direction is normalized before scaling).
        n_modes  : number of buckling modes to return (lowest first).

        CalculiX GPL-2.0 → subprocess-only. Absent ccx ⇒ {ok:false, skipped:true}.
        """
        try:
            n_modes = _positive_int(n_modes, "n_modes")
            from . import fea_advanced as fa

            if fa.ccx_path() is None:
                return _dumps(fa._ccx_absent_result("linear buckling"))
            d = _doc(doc_id)
            fixtures = require_list(
                fixtures, "fixtures", 'one fixed face query, e.g. ["bottom"]'
            )
            load = require_list(
                load, "load", 'a single-item list [{"face": <query>, "force": [x,y,z]}]'
            )
            bc_notes: list = []
            fixtures = _norm_bc_list(
                fixtures, "fixtures", unwrap_query=True, notes=bc_notes
            )
            load = _norm_bc_list(load, "load", notes=bc_notes)
            if len(fixtures) != 1:
                raise ValueError(
                    "buckling box-proxy supports exactly one clamped-end face query; "
                    f"got {len(fixtures)}. Multiple independent fixtures cannot be mapped "
                    "onto its fixed-free column model."
                )
            if len(load) != 1 or not isinstance(load[0], dict):
                raise ValueError(
                    "load must be exactly one {'face': <query>, 'force': [x,y,z]} entry."
                )
            _reject_ignored_fields(load[0], {"face", "force"}, "load[0]")
            if "face" not in load[0] or "force" not in load[0]:
                raise ValueError(
                    "load must contain both face and force; there is no safe default "
                    "buckling excitation."
                )
            vec = _force_vector(load[0]["force"], "load[0]")
            P = math.sqrt(sum(x * x for x in vec))
            if P <= 0.0:
                raise ValueError(
                    "the buckling load is zero. λ scales the applied load, so every "
                    "critical_loads_n value would be meaningless."
                )
            mat = _resolve_study_material(material, "buckling")
            E = _finite_float(mat["E_mpa"], "material.E_mpa")
            nu = _finite_float(mat["nu"], "material.nu")
            fixture_set = _merge_face_sets(_sim_face_points(d, fixtures[0]))
            load_set = _merge_face_sets(_sim_face_points(d, load[0]["face"]))
            if not fixture_set["points"] or not load_set["points"]:
                raise ValueError("fixture and load face queries must both resolve.")
            fixed_points = {
                tuple(float(c) for c in point)
                for point in fixture_set["points"]
                if isinstance(point, (list, tuple)) and len(point) == 3
            }
            loaded_points = {
                tuple(float(c) for c in point)
                for point in load_set["points"]
                if isinstance(point, (list, tuple)) and len(point) == 3
            }
            if loaded_points and loaded_points.issubset(fixed_points):
                raise ValueError(
                    "buckling fixture and load resolve to the same constrained face; the "
                    "real load would be absorbed as a support reaction, while the proxy "
                    "incorrectly places it on the free end."
                )
            normal = load_set.get("face_normal")
            fixture_normal = fixture_set.get("face_normal")
            if not (
                isinstance(normal, (list, tuple))
                and len(normal) == 3
                and isinstance(fixture_normal, (list, tuple))
                and len(fixture_normal) == 3
            ):
                raise ValueError(
                    "buckling needs planar fixture/load end faces with outward normals so the "
                    "fixed-free proxy axis can be mapped without guessing."
                )
            unit_normal = _norm_direction(normal, "load[0].face_normal")
            unit_fixture_normal = _norm_direction(
                fixture_normal, "fixtures[0].face_normal"
            )
            face_opposition = sum(
                a * b0 for a, b0 in zip(unit_normal, unit_fixture_normal)
            )
            if face_opposition > -0.99:
                raise ValueError(
                    "buckling fixture and load faces are not opposing parallel ends; the "
                    f"normal dot product is {face_opposition:g}, but the box proxy requires "
                    "approximately -1."
                )
            normal_component = sum(a * b0 for a, b0 in zip(vec, unit_normal))
            if normal_component >= -1e-9 * max(1.0, P):
                raise ValueError(
                    "buckling requires a compressive force pointing into the selected load "
                    f"face; force·outward_normal={normal_component:g} N is tensile/tangential."
                )
            tangential = math.sqrt(
                max(0.0, P * P - normal_component * normal_component)
            )
            if tangential > 1e-6 * P:
                raise ValueError(
                    "buckling box-proxy supports a purely axial compressive load; the supplied "
                    f"force has {tangential:g} N tangential to the selected end face. Split "
                    "shear/bending into a different analysis instead of silently mapping the "
                    "full vector to axial compression."
                )
            P = -normal_component

            def _centroid(points, label):
                return [
                    sum(_finite_float(point[axis], label) for point in points)
                    / len(points)
                    for axis in range(3)
                ]

            fixture_c = _centroid(fixture_set["points"], "fixture face point")
            load_c = _centroid(load_set["points"], "load face point")
            delta = [load_c[axis] - fixture_c[axis] for axis in range(3)]
            L = math.sqrt(sum(value * value for value in delta))
            if not math.isfinite(L) or L <= 1e-9:
                raise ValueError(
                    "buckling fixture/load face centroids coincide; no column length can be "
                    "mapped to the fixed-free proxy."
                )
            axis_unit = [value / L for value in delta]
            end_alignment = sum(a * b0 for a, b0 in zip(axis_unit, unit_normal))
            if end_alignment < 0.99:
                raise ValueError(
                    "buckling fixture/load faces do not define a column axis aligned with the "
                    f"load-face normal (alignment={end_alignment:g}); the box proxy cannot "
                    "map those selections honestly."
                )
            dominant_axis = max(range(3), key=lambda axis: abs(axis_unit[axis]))
            if abs(axis_unit[dominant_axis]) < 0.99:
                raise ValueError(
                    "buckling box-proxy only supports a world-axis-aligned column; the selected "
                    "end faces are rotated, so world bounding-box transverse dimensions would "
                    "overstate the section."
                )
            spans = _part_bbox_world_spans(d)
            transverse = [spans[axis] for axis in range(3) if axis != dominant_axis]
            b, h = transverse
            gen = fa.build_buckling_column_inp(L, b, h, E, nu, P, nz=4, n_modes=n_modes)
            run = fa.run_ccx(gen["inp"], job="buckle", timeout=fa.HARNESS_CCX_TIMEOUT_S)
            if run.get("skipped"):
                return _dumps(run)
            if not run.get("ok") or not run.get("dat_path"):
                return _dumps(
                    {
                        "ok": False,
                        "error": run.get("stderr", "")[-400:]
                        or run.get("error", "ccx buckling run failed"),
                        "backend": run.get("backend"),
                    }
                )
            factors = fa.parse_buckle_factors(run["dat_path"])
            if len(factors) < n_modes or any(
                not math.isfinite(f) or f <= 0.0 for f in factors[:n_modes]
            ):
                return _dumps(
                    {
                        "ok": False,
                        "doc_id": d,
                        "backend": run["backend"],
                        "error": f"CalculiX returned {len(factors)} usable buckling factors; "
                        f"{n_modes} finite positive modes were requested. Refusing a "
                        "partial/degenerate mode list.",
                    }
                )
            factors = factors[:n_modes]
            crit = [round(f * P, 3) for f in factors]
            out = {
                "ok": True,
                "doc_id": d,
                "backend": run["backend"],
                "solver": "CalculiX *BUCKLE (box proxy, fixed-free column)",
                "applied_load_n": P,
                "load_factors": [round(f, 4) for f in factors],
                "critical_loads_n": crit,
                "euler_reference_pcr_n": round(gen["expected_Pcr"], 3),
                "input_mapping": {
                    "fixture_face": fixtures[0],
                    "load_face": load[0]["face"],
                    "force_dot_outward_normal_n": normal_component,
                    "proxy_length_mm": L,
                    "proxy_cross_section_mm": [b, h],
                    "world_axis": ("x", "y", "z")[dominant_axis],
                    "end_face_normal_alignment": end_alignment,
                },
                "note": "box-proxy Euler buckling estimate after validating a real "
                "clamped face and inward compressive load face; ccx GPL-2.0 "
                "subprocess. load_factor<1 ⇒ buckles below the applied load.",
            }
            sid = _persist_study(
                d,
                {
                    "type": "fea_buckling",
                    "name": f"Buckling ({mat.get('name', material)})",
                    "engine": "calculix",
                    "results": out,
                },
            )
            out["saved_to_study"] = bool(sid)
            return _dumps(_interpreted(out, bc_notes))
        except CoreError as e:
            return _err(e)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def run_fea_contact(
        pressure_mpa: float = 1.0,
        material: Union[str, dict] = "steel",
        doc_id: Optional[str] = None,
    ) -> str:
        """Nonlinear (large-deflection) contact FEA via CalculiX (ccx subprocess) — for
        press-fits, snap-fits, stacked parts touching under load.

        Solves a *STATIC, NLGEOM step with a *CONTACT PAIR transmitting load across a
        touching interface. This ccx path uses a TWO-BLOCK PROXY (an upper block pressed
        onto a lower fixed block) sized to the part bounding box — a validated
        conservation demonstration that the contact interface transmits the applied load
        into the fixed support (base reaction ≈ applied load).

        pressure_mpa : downward pressure on the upper block's top face.

        CalculiX GPL-2.0 → subprocess-only. Absent ccx ⇒ {ok:false, skipped:true}.
        """
        try:
            pressure_mpa = _finite_float(pressure_mpa, "pressure_mpa")
            if pressure_mpa <= 0.0:
                raise ValueError(
                    "pressure_mpa must be > 0; zero/negative pressure does not close and "
                    "load the two-block contact proxy."
                )
            from . import fea_advanced as fa

            if fa.ccx_path() is None:
                return _dumps(fa._ccx_absent_result("nonlinear contact"))
            d = _doc(doc_id)
            mat = _resolve_study_material(material, "contact")
            E = _finite_float(mat["E_mpa"], "material.E_mpa")
            nu = _finite_float(mat["nu"], "material.nu")
            L, b, h = _part_bbox_dims(d)
            gen = fa.build_contact_block_inp(L, b, h, E, nu, pressure_mpa)
            run = fa.run_ccx(
                gen["inp"], job="contact", timeout=fa.HARNESS_CCX_TIMEOUT_S
            )
            if run.get("skipped"):
                return _dumps(run)
            if not run.get("ok") or not run.get("dat_path"):
                return _dumps(
                    {
                        "ok": False,
                        "error": run.get("stderr", "")[-400:]
                        or run.get("error", "ccx contact run failed"),
                        "backend": run.get("backend"),
                    }
                )
            rf = fa.parse_dat_nodal(
                run["dat_path"], "RF", gen["base_nodes"], component=3
            )
            if rf is None or not math.isfinite(rf):
                return _dumps(
                    {
                        "ok": False,
                        "doc_id": d,
                        "backend": run["backend"],
                        "error": "CalculiX completed but no finite base reaction "
                        "could be parsed; contact transmission is unverified.",
                    }
                )
            reaction = abs(rf * len(gen["base_nodes"]))
            applied = gen["applied_load_n"]
            balance_error = abs(reaction - applied) / applied * 100.0
            if balance_error > 5.0:
                return _dumps(
                    {
                        "ok": False,
                        "doc_id": d,
                        "backend": run["backend"],
                        "applied_load_n": round(applied, 3),
                        "base_reaction_n": round(reaction, 3),
                        "equilibrium_error_pct": round(balance_error, 3),
                        "error": "contact result failed the load-conservation check (>5%); "
                        "refusing to present it as a converged contact solve.",
                    }
                )
            out = {
                "ok": True,
                "doc_id": d,
                "backend": run["backend"],
                "solver": "CalculiX *STATIC,NLGEOM + *CONTACT PAIR (two-block proxy)",
                "applied_load_n": round(applied, 3),
                "base_reaction_n": round(reaction, 3),
                "equilibrium_error_pct": round(balance_error, 3),
                "capability": "two-block bounding-box proxy; actual CAD contact pairs are "
                "not mapped by this backend",
                "note": "box-proxy contact conservation check passed within 5%; "
                "ccx GPL-2.0 subprocess.",
            }
            sid = _persist_study(
                d,
                {
                    "type": "fea_contact",
                    "name": f"Contact ({mat.get('name', material)})",
                    "engine": "calculix",
                    "results": out,
                },
            )
            out["saved_to_study"] = bool(sid)
            return _dumps(out)
        except CoreError as e:
            return _err(e)
        except Exception as e:  # noqa: BLE001
            return _err(e)
