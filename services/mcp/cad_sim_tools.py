"""CAD simulation MCP tools (T-RF8 split of cad_tools.py): run_fea_* + run_cfd_*."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional
from typing import Union
import math
import re

from .cad_common import (
    CoreError,
    coerce_json_or_repr,
    require_list,
    require_mapping,
    _autosave_all_open,
    _default_conductivity,
    _default_specific_heat,
    _doc,
    _dumps,
    _err,
    _first_temperature,
    _merge_face_sets,
    _persist_study,
    _resolve_face_points,
    _resolve_material,
    _sid_match,
    _solve_sync_or_handoff,
    _start_job,
    _tmp,
    _SID_RE,
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
    "coarsest": 0.1, "very_coarse": 0.1, "verycoarse": 0.1,
    "coarse": 0.25, "low": 0.25, "draft": 0.25,
    "medium": 0.5, "normal": 0.5, "default": 0.5, "standard": 0.5, "moderate": 0.5,
    "fine": 0.75, "high": 0.75,
    "finest": 1.0, "very_fine": 1.0, "veryfine": 1.0,
}

# Signed-axis spellings a model writes instead of a 3-vector `direction`. parse_side's
# convention (core/doc/picks.cpp): Chudware is Z-up, so up=+Z / down=-Z.
_AXIS_DIRS = {
    "+x": (1.0, 0.0, 0.0), "x": (1.0, 0.0, 0.0), "-x": (-1.0, 0.0, 0.0),
    "+y": (0.0, 1.0, 0.0), "y": (0.0, 1.0, 0.0), "-y": (0.0, -1.0, 0.0),
    "+z": (0.0, 0.0, 1.0), "z": (0.0, 0.0, 1.0), "-z": (0.0, 0.0, -1.0),
    "up": (0.0, 0.0, 1.0), "down": (0.0, 0.0, -1.0),
}

_BC_SHAPE_HELP = ('accepted shapes: {"face": <query>, "force": [fx,fy,fz]} (N, world XYZ), '
                  '{"face": <query>, "pressure": <MPa>}, or the polar form '
                  '{"face": <query>, "magnitude": <N>, "direction": [dx,dy,dz]}. '
                  '"faces": [<query>, ...] is accepted as the plural of "face", and the '
                  'unit-suffixed spelling of the SAME quantity is read as the canonical key '
                  '("face_query"->face, "magnitude_n"/"force_n"->magnitude, '
                  '"pressure_mpa"->pressure, "temperature_c"->temperature).')

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

# Keys of a raw geometry QUERY / query-result match dict. Such a dict is passed straight to
# the engine query grammar (core/doc/picks.h) and is NOT a BC wrapper, so it is never
# key-checked: {"of":"face","side":"bottom"}, {"sid":...}, {"kind":"face","surf":"plane",...}.
_QUERY_DICT_KEYS = frozenset({"of", "kind", "surf", "sid", "centroid", "hint_index",
                              "area_or_len", "owner_feature"})

# The canonical keys a fixtures/loads/hot_faces/cold_faces item may carry, i.e. exactly the
# keys the solver payload builders below and in run_fea_thermal actually READ.
_BC_ITEM_KEYS = frozenset({
    "face", "faces", "type", "force", "pressure", "magnitude", "direction",
    "temperature", "flux_w_m2", "h_w_m2k", "t_amb_c", "schedule",
})

# Purely-cosmetic item labels ("name":"tip_load" in the live payload). They name the load for
# a reader and can never change what the solver computes, so they ride along instead of
# turning an otherwise-correct entry into a refusal. Same closed-list discipline as
# arg_reconciliation.DESCRIPTIVE_ARGS: nothing that could carry geometry, a quantity or a unit.
_BC_ITEM_LABELS = frozenset({"name", "label", "comment", "note", "notes", "description",
                             "title", "caption"})

# Normalised (lower, no _ - or space) supplied key -> canonical item key.
_ITEM_KEY_ALIASES: dict[str, str] = {
    # --- the face-reference slot. A BC item has exactly ONE selection slot, so any spelling
    # of "which surface this applies to" is unambiguous HERE.
    "facequery": "face", "faceref": "face", "facesel": "face", "faceselector": "face",
    "facesid": "face", "targetface": "face", "surface": "face", "on": "face",
    "onface": "face", "appliedto": "face",
    "facesquery": "faces", "facequeries": "faces", "facerefs": "faces",
    "faceslist": "faces", "surfaces": "faces", "targetfaces": "faces",
    # --- the scalar magnitude. `_n` / `newtons` is the unit the accepted shape already
    # states, so these are the SAME quantity, not a conversion.
    "magnituden": "magnitude", "magnitudenewtons": "magnitude",
    "forcen": "magnitude", "forcenewtons": "magnitude", "forcemagnitude": "magnitude",
    "forcemagnituden": "magnitude", "loadn": "magnitude", "loadmagnitude": "magnitude",
    # --- pressure / temperature / flux / film coefficient: unit-suffixed spellings of the
    # unit this tool already documents (MPa, °C, W/m², W/m²K).
    "pressurempa": "pressure",
    "temperaturec": "temperature", "temperaturedegc": "temperature", "tempc": "temperature",
    "temp": "temperature",
    "flux": "flux_w_m2", "heatflux": "flux_w_m2", "heatfluxwm2": "flux_w_m2",
    "h": "h_w_m2k", "htc": "h_w_m2k", "htcwm2k": "h_w_m2k",
    "filmcoefficient": "h_w_m2k", "convectioncoefficient": "h_w_m2k",
    "tamb": "t_amb_c", "tambientc": "t_amb_c", "ambienttemperature": "t_amb_c",
    "ambienttempc": "t_amb_c",
    # --- direction / kind. DELIBERATELY EXCLUDED: `axis` (an axis is unsigned, a direction is
    # signed) and `load_type` (run_fea_fatigue's top-level load_type names the cyclic SPECTRUM
    # "reversed"/"zero_based", a different thing from a BC kind — so it stays a refusal).
    "directionvector": "direction", "dir": "direction",
    "forcedirection": "direction", "loaddirection": "direction",
    "kind": "type", "bctype": "type", "fixturetype": "type", "constrainttype": "type",
}

_FACE_REF_NORMS = frozenset(
    {"face", "faces"} | {n for n, c in _ITEM_KEY_ALIASES.items() if c in ("face", "faces")})

# Face-RELATIVE direction words. See _norm_direction: these are REFUSED, not resolved.
_FACE_RELATIVE_DIRS = frozenset({
    "normal", "facenormal", "normaltoface", "surfacenormal", "alongnormal", "alongthenormal",
    "perpendicular", "perpendiculartoface", "inward", "inwards", "outward", "outwards",
    "intoface", "intotheface", "outofface",
})


def _norm_key(name) -> str:
    """Fold a dict key for matching: case / underscore / dash / space are noise."""
    return str(name).lower().replace("_", "").replace("-", "").replace(" ", "")


def _canon_item_keys(e: dict, where: str, notes: Optional[list] = None) -> dict:
    """Rename a BC item's key SPELLINGS onto the canonical ones, and refuse the rest.

    Applies only to a BC WRAPPER — a dict carrying some spelling of a face reference. A raw
    geometry query / query-result match dict is handed to the engine grammar verbatim and is
    left completely alone.

    There is no catch-all: a key that is neither canonical, a known spelling, nor a cosmetic
    label is REFUSED naming the real vocabulary. Silently dropping it would hide a setting the
    caller believed it had applied, which is exactly the failure mode this file exists to
    prevent."""
    if any(k in e for k in _QUERY_DICT_KEYS):
        return e
    if not any(_norm_key(k) in _FACE_REF_NORMS for k in e):
        return e
    out: dict = {}
    renamed: list = []
    for k, v in e.items():
        if k in _BC_ITEM_KEYS or k in _BC_ITEM_LABELS:
            out[k] = v
            continue
        canon = _ITEM_KEY_ALIASES.get(_norm_key(k))
        if canon is None:
            raise ValueError(
                f"{where}: unknown key {k!r} in this boundary-condition entry — it is not a "
                f"spelling of any accepted key, and guessing would run a load you did not "
                f"ask for. Accepted keys: {sorted(_BC_ITEM_KEYS)} (plus an optional "
                f"descriptive {sorted(_BC_ITEM_LABELS)}). " + _BC_SHAPE_HELP)
        if canon in e or canon in out:
            other = e[canon] if canon in e else out[canon]
            raise ValueError(
                f"{where}: {k!r} is another spelling of {canon!r} and BOTH were given "
                f"({v!r} vs {other!r}) — that is ambiguous. Pass exactly one.")
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
    """mesh_fineness as a 0..1 float, accepting the preset WORDS as well as a number."""
    if isinstance(value, str):
        key = value.strip().lower().replace("-", "_").replace(" ", "_")
        if key in _FINENESS_WORDS:
            return _FINENESS_WORDS[key]
        try:
            return float(value)
        except ValueError:
            raise ValueError(
                f"{arg_name} must be a number 0..1 or one of "
                f"{sorted(set(_FINENESS_WORDS))} — got {value!r}.") from None
    return float(value)


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
                'face `axis`, so you can copy it and choose the sign. For a load that acts '
                'along the face normal BY DEFINITION, use {"face": <query>, "pressure": '
                "<MPa>} (inward on the face) instead of a force.")
        d = coerce_json_or_repr(d)
    if isinstance(d, dict) and all(k in d for k in ("x", "y", "z")):
        d = [d["x"], d["y"], d["z"]]
    if not isinstance(d, (list, tuple)) or len(d) != 3:
        raise ValueError(f"{where}: 'direction' must be [dx,dy,dz] (or one of "
                         f"{sorted(_AXIS_DIRS)}), got {raw!r}.")
    try:
        comps = [float(x) for x in d]
    except (TypeError, ValueError):
        raise ValueError(f"{where}: 'direction' components must be numbers, "
                         f"got {raw!r}.") from None
    n = math.sqrt(sum(c * c for c in comps))
    if n <= 1e-12:
        raise ValueError(f"{where}: 'direction' {raw!r} has zero length — it cannot point "
                         "anywhere, so the load is undefined. Give a real direction "
                         "[dx,dy,dz] (any length; it is normalized), or drop "
                         "magnitude/direction and pass force:[fx,fy,fz] directly.")
    return [c / n for c in comps]


def _split_face_list(e: dict, where: str) -> list:
    """Fan a plural `faces: [q1, q2]` entry out to one entry per face, siblings preserved."""
    if "faces" in e and "face" in e:
        raise ValueError(f"{where}: both 'face' and 'faces' were given ({e['face']!r} vs "
                         f"{e['faces']!r}) — that is ambiguous. Pass exactly one: 'face' for "
                         "a single query, 'faces' for a list.")
    key = "faces" if "faces" in e else ("face" if "face" in e else None)
    if key is None:
        return [e]
    v = e[key]
    if isinstance(v, str) and v.strip()[:1] in ("[", "{"):
        v = coerce_json_or_repr(v)
    if isinstance(v, (list, tuple)):
        if not v:
            raise ValueError(f"{where}: {key!r} is an empty list — there is nothing to apply "
                             "this boundary condition to. Name at least one face query "
                             "(query_geometry(...) or describe_part(doc_id) to find one).")
        rest = {k: x for k, x in e.items() if k != key}
        return [{**rest, "face": f} for f in v]
    if key == "faces":
        rest = {k: x for k, x in e.items() if k != key}
        return [{**rest, "face": v}]
    return [e]


def _polar_to_force(e: dict, where: str) -> dict:
    """`magnitude` (+ `direction`) -> the explicit `force` vector the solver payload wants.

    force = unit(direction) * magnitude. A supplied `force` that DISAGREES with the polar
    form is ambiguous and rejected rather than silently resolved one way."""
    if e.get("magnitude") is None:
        return e
    try:
        mag = float(e["magnitude"])
    except (TypeError, ValueError):
        raise ValueError(f"{where}: 'magnitude' must be a number, got {e['magnitude']!r}. "
                         + _BC_SHAPE_HELP) from None
    d = e.get("direction")
    kind = str(e.get("type") or "").strip().lower()
    is_pressure = kind.startswith("press")

    if d is None:
        if "force" in e:
            vec = [float(x) for x in e["force"]]
            nf = math.sqrt(sum(c * c for c in vec))
            if abs(nf - mag) > 1e-6 * max(1.0, abs(mag)):
                raise ValueError(
                    f"{where}: 'force' {e['force']!r} has magnitude {nf:g} but 'magnitude' "
                    f"says {mag:g} — those disagree, so which load was meant is ambiguous. "
                    "Pass force:[fx,fy,fz] alone, or magnitude + direction alone.")
            return {k: v for k, v in e.items() if k != "magnitude"}
        if "pressure" in e:
            if abs(float(e["pressure"]) - mag) > 1e-6 * max(1.0, abs(mag)):
                raise ValueError(
                    f"{where}: 'pressure' {e['pressure']!r} and 'magnitude' {mag:g} disagree "
                    "— ambiguous. Pass one of them.")
            return {k: v for k, v in e.items() if k != "magnitude"}
        if is_pressure:
            out = {k: v for k, v in e.items() if k not in ("magnitude", "type")}
            out["pressure"] = mag
            return out
        raise ValueError(f"{where}: 'magnitude':{mag:g} was given with no 'direction', so the "
                         "load has no orientation and cannot be built. " + _BC_SHAPE_HELP)

    if is_pressure:
        raise ValueError(f"{where}: type 'pressure' was given together with a 'direction' "
                         f"{d!r} — pressure acts along the face normal and takes no direction. "
                         "Pass pressure:<MPa> for a pressure, or force/magnitude+direction "
                         "for a vector load.")
    unit = _norm_direction(d, where)
    vec = [c * mag for c in unit]
    if "force" in e:
        given = [float(x) for x in e["force"]]
        tol = 1e-6 * max(1.0, abs(mag))
        if len(given) != 3 or any(abs(a - b) > tol for a, b in zip(given, vec)):
            raise ValueError(
                f"{where}: 'force' {e['force']!r} disagrees with magnitude {mag:g} x "
                f"direction {d!r} (= {[round(c, 6) for c in vec]}) — ambiguous. Pass "
                "force:[fx,fy,fz] alone, or magnitude + direction alone.")
    out = {k: v for k, v in e.items() if k not in ("magnitude", "direction", "type")}
    out["force"] = vec
    return out


def _norm_bc_list(entries: list, arg_name: str, *, unwrap_query: bool = False,
                  notes: Optional[list] = None) -> list:
    """Normalize a fixtures/loads list at the tool boundary. IDENTITY for entries already
    in the accepted shape — the payload handed to the C++ solver is unchanged for inputs
    that already worked.

    unwrap_query: fixtures consume only the face QUERY, so a wrapper dict
    ({"face": q, "type": "fixed"}) collapses to `q`; loads / hot_faces / cold_faces keep
    their siblings (force, pressure, temperature, flux_w_m2, schedule, ...).
    notes: appended to with one human-readable line per entry whose KEYS were renamed, so
    the calling tool can echo the rename back in its `interpreted` channel — a rename the
    model cannot see is a rename it will keep re-learning."""
    out: list = []
    for i, raw in enumerate(entries):
        where = f"{arg_name}[{i}]"
        e = raw
        if isinstance(e, str) and e.strip()[:1] == "{":
            e = coerce_json_or_repr(e)
        if not isinstance(e, dict):
            out.append(raw)          # a bare query string / already-structured query
            continue
        e = _canon_item_keys(e, where, notes)
        for one in _split_face_list(e, where):
            one = _polar_to_force(one, where)
            if unwrap_query and "face" in one:
                out.append(one["face"])
            else:
                out.append(one)
    return out


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
                    raise ValueError(f"face sid {s!r} resolved but carries no surface points.")
                return [fp]
            raise ValueError(
                f"No face with sid {s!r} in this document (sids change whenever the model "
                "changes). Call describe_part(doc_id) for the current face/edge sid roster, "
                "or query_geometry(...) for a geometry query.")
        if re.fullmatch(r"[fF]\d+", s):
            raise ValueError(
                f"{s!r} is a FEATURE id, not a face — a boundary condition needs a face. "
                "Call describe_part(doc_id) for the face sid roster (F58db6d style, which "
                "this tool accepts directly), or query_geometry(...) for a geometry query.")
    return _resolve_face_points(doc_id, query)


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
            raise ValueError(f"each load needs a 'face' query: {ld!r}. " + _BC_SHAPE_HELP)
        # Merge multiple face-point sets for this load (keeps face_normal/surface
        # hints exactly when they stay valid — see _merge_face_sets).
        merged = _merge_face_sets(_sim_face_points(d, ld["face"]))
        spec: dict = {"faces": merged}
        if "pressure" in ld:
            spec["type"] = "pressure"
            spec["magnitude"] = float(ld["pressure"])
        elif "force" in ld:
            spec["type"] = "force"
            spec["vector"] = [float(x) for x in ld["force"]]
        else:
            raise ValueError(f"load needs 'force':[x,y,z] or 'pressure':<MPa>: {ld!r}. "
                             + _BC_SHAPE_HELP)
        specs.append(spec)
    if not any(_load_is_driving(s) for s in specs):
        raise _StudyRefusal({"ok": False, "error": (
            f"{analysis} has no non-zero load, so the solve is identically zero — every "
            "stress, displacement and factor of safety would come back 0 / saturated, "
            "which reads as 'infinitely safe' rather than 'never asked'. Supply at least "
            'one load with a non-zero magnitude, e.g. loads=[{"face":"top","force":'
            '[0,0,-200]}] (newtons, world XYZ) or [{"face":"top","pressure":0.5}] (MPa). '
            "This study has no self-weight/gravity term, so an unloaded part carries "
            "nothing at all.")})
    return specs


def _build_static_study(fixtures, loads, material, mesh_fineness, doc_id, notes: list) -> dict:
    """SETUP stage for run_fea_static / sim_run(analysis="fea_static")."""
    d = _doc(doc_id)
    fixtures = require_list(fixtures, "fixtures",
                            'a list of face queries, e.g. ["bottom"] or '
                            '[{"of":"face","side":"bottom","pick":"largest"}]')
    loads = require_list(loads, "loads",
                         'a list of {face query + force/pressure}, e.g. '
                         '[{"face":"top","force":[0,0,-500]}]')
    fixtures = _norm_bc_list(fixtures, "fixtures", unwrap_query=True, notes=notes)
    loads = _norm_bc_list(loads, "loads", notes=notes)
    mesh_fineness = _norm_fineness(mesh_fineness)
    mat = _resolve_material(coerce_json_or_repr(material))
    # fixtures=[] and "fixtures that resolved to nothing" are DIFFERENT mistakes, and the
    # single old message diagnosed the wrong one — it told a caller who supplied no queries
    # at all to go check its queries with query_geometry(), buying a wasted retry.
    if not fixtures:
        raise _StudyRefusal({"ok": False, "error":
                             "static FEA needs at least one fixed face — you passed "
                             "fixtures=[]. An unconstrained part under load has nothing to "
                             "react against (rigid-body motion), so there is no static "
                             'solution to report. Pass the mounted face, e.g. fixtures='
                             '["bottom"]. fixtures=[] is only meaningful for run_fea_modal, '
                             "where it means a free-free modal solve."})
    step = _tmp(".step")
    cb.call("doc_export_step", {"doc_id": d, "path": step})

    fixture_sets: list = []
    for q in fixtures:
        fixture_sets.extend(_sim_face_points(d, q))
    if not fixture_sets:
        raise _StudyRefusal({"ok": False, "error":
                             "no fixture faces resolved — the face queries you provided "
                             "didn't match any surface points. Static FEA requires at least "
                             "one fixed face. Use query_geometry() to confirm the face query."})

    load_specs = _build_load_specs(d, loads, "static FEA")

    study = {
        "step_path": step,
        "material": mat,
        "fixtures": fixture_sets,
        "loads": load_specs,
        "mesh_order": 2,        # tet10 captures bending
        "fineness": mesh_fineness,
    }
    return {"analysis": "fea_static", "doc": d, "study": study, "material": mat,
            "material_arg": material, "fixtures": fixtures, "loads": loads,
            "mesh_fineness": mesh_fineness, "step_path": step}


def _build_modal_study(fixtures, material, mesh_fineness, doc_id, notes: list) -> dict:
    """SETUP stage for run_fea_modal / sim_run(analysis="fea_modal")."""
    d = _doc(doc_id)
    fixtures = require_list(fixtures, "fixtures",
                            'a list of face queries held fixed, e.g. ["bottom"]')
    fixtures = _norm_bc_list(fixtures, "fixtures", unwrap_query=True, notes=notes)
    mesh_fineness = _norm_fineness(mesh_fineness)
    mat = _resolve_material(coerce_json_or_repr(material))
    step = _tmp(".step")
    cb.call("doc_export_step", {"doc_id": d, "path": step})
    fixture_sets: list = []
    for q in fixtures:
        fixture_sets.extend(_sim_face_points(d, q))
    # F6 (free-free modal): when fixtures=[] the caller is requesting an unconstrained
    # solve — the C++ fea_modal_solve handles empty fixed_nodes by computing the full
    # 6 rigid-body modes near 0 Hz followed by the elastic modes. Only reject when the
    # caller DID supply fixture queries but NONE of them resolved to real face points
    # (which is almost certainly a geometry-query mistake, not intentional free-free).
    if fixtures and not fixture_sets:
        raise _StudyRefusal({"ok": False, "error": "no fixture faces resolved — "
                             "the face queries you provided didn't match any surface points. "
                             "Use query_geometry() to find the right face query, or pass "
                             "fixtures=[] for a free-free (unconstrained) solve."})
    # 2nd-order (tet10) elements: quadratic bending accuracy — no spurious split on
    # a square section and no low-frequency bias the way tet4 had (Bug N-1).
    study = {"step_path": step, "material": mat, "fixtures": fixture_sets,
             "mesh_order": 2, "fineness": mesh_fineness}
    return {"analysis": "fea_modal", "doc": d, "study": study, "material": mat,
            "material_arg": material, "fixtures": fixtures, "fixture_sets": fixture_sets,
            "free_free": not fixture_sets, "mesh_fineness": mesh_fineness,
            "step_path": step}


# core/sim/fea_static.cpp clamps yield/stress into [0, 15]; 15 therefore means "saturated",
# not "15x margin", and an unloaded study used to report exactly that (see _build_load_specs).
_FOS_CAP = 15.0


def _collect_static(ctx: dict, res: dict, notes: list) -> dict:
    """COLLECT stage for a static solve — the ONE place the reply envelope is built."""
    d = ctx["doc"]
    mat = ctx.get("material") or {}
    if not res.get("ok"):
        return {"ok": False, "doc_id": d, "error": res.get("error", "solve failed")}
    # Persist setup+results into the doc so they save inside the .step+.
    sid = _persist_study(d, {
        "type": "fea_static",
        "name": f"Static stress ({mat.get('name', ctx.get('material_arg'))})",
        "engine": "cpp",
        "material": mat,
        "fixtures": ctx.get("fixtures"),
        "loads": ctx.get("loads"),
        "mesh_fineness": ctx.get("mesh_fineness"),
        "results": res.get("summary", {}),
        "n_dof": res.get("n_dof"),
    })
    # The engine's sim_run_static reply also carries n_elems / n_nodes / mesh_type_used /
    # solver_notes; only n_dof was forwarded, so a caller could not see the mesh it just
    # solved on (no way to judge whether a refinement is needed) and any solver WARNING
    # was dropped on the floor. Measured 2026-07-30: engine keys n_elems/n_nodes present,
    # reply had neither.
    out = {"ok": True, "doc_id": d, "material": mat.get("name", ctx.get("material_arg")),
           "summary": res.get("summary", {}), "n_dof": res.get("n_dof"),
           "n_elems": res.get("n_elems"), "n_nodes": res.get("n_nodes"),
           "mesh": res.get("mesh_type_used"),
           "saved_to_study": bool(sid)}
    if res.get("solver_notes"):
        out["solver_notes"] = res["solver_notes"]
    # min_fos saturates at 15 (core/sim/fea_static.cpp clamp_fos) — say so, otherwise a cap
    # is read as a computed margin.
    if float((res.get("summary") or {}).get("min_fos") or 0.0) >= _FOS_CAP:
        out["fos_note"] = (f"min_fos is reported at the solver's saturation cap ({_FOS_CAP}); "
                           "the true margin is at least that, not exactly that.")
    if not sid:
        # _persist_study is best-effort and swallows its own errors; reporting a flat True
        # would be a verdict we never checked (and after a background job collected in a
        # LATER agent turn the document is legitimately no longer open here).
        out["study_not_saved_reason"] = (
            f"the results could not be written into document {d} (it is not open in this "
            "process). The numbers above are unaffected.")
    return _interpreted(out, notes)


def _n_rigid_modes(freqs: list) -> int:
    return sum(1 for f in freqs if f < 0.5)  # rigid ~0 Hz, elastic >> 0.5 Hz


def _collect_modal(ctx: dict, freqs: list, n_modes: int, notes: list,
                   note: Optional[str] = None) -> dict:
    """COLLECT stage for a modal solve."""
    d = ctx["doc"]
    mat = ctx.get("material") or {}
    sid = _persist_study(d, {
        "type": "fea_modal",
        "name": f"Modal ({mat.get('name', ctx.get('material_arg'))})",
        "engine": "cpp",
        "material": mat,
        "fixtures": ctx.get("fixtures"),
        "n_modes": n_modes,
        "mesh_fineness": ctx.get("mesh_fineness"),
        "results": {"freqs_hz": freqs, "n_modes": len(freqs)},
    })
    out = {"ok": True, "doc_id": d, "material": mat.get("name", ctx.get("material_arg")),
           "freqs_hz": freqs, "n_modes": len(freqs), "saved_to_study": bool(sid)}
    if not sid:
        out["study_not_saved_reason"] = (
            f"the results could not be written into document {d} (it is not open in this "
            "process). The frequencies above are unaffected.")
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
    return (f"warning: free-free solve resolved only {n}/6 rigid-body modes — the mode list "
            "may be missing rigid modes (eigensolver degenerate-cluster limitation); "
            "elastic frequencies are unaffected")


# Analyses whose SETUP produces a self-contained study (an exported STEP path + JSON), which
# is the precondition for solving them in a separate, killable process. run_cfd_accurate is
# deliberately absent: it already always-backgrounds with its own live progress channel, and
# run_fea_frequency_response / run_fea_fatigue post-process a static field with Python between
# engine calls, so their solve is not one atomic RPC. Those keep background=True + get_job().
_ASYNC_ANALYSES = ("fea_static", "fea_modal")


def _collect_job(st: dict, raw: dict) -> dict:
    """COLLECT stage for a finished async job: the job's own meta IS the setup context, so
    this produces byte-identical envelopes to the synchronous tools."""
    meta = st.get("meta") or {}
    kind = meta.get("analysis") or st.get("kind")
    notes = list(meta.get("notes") or [])
    if kind == "fea_static":
        return _collect_static(meta, raw, notes)
    if kind == "fea_modal":
        if not raw.get("ok"):
            return {"ok": False, "doc_id": meta.get("doc"),
                    "error": raw.get("error", "modal solve failed")}
        n = int(meta.get("n_modes") or 6)
        freqs = list(raw.get("freqs_hz", []))
        note = None
        if meta.get("free_free"):
            note = _modal_rigid_note(freqs)
            if note:
                note += (". The background path does NOT re-solve with a wider subspace the "
                         "way the synchronous tool does (that would be a second job) — re-run "
                         "run_fea_modal synchronously if the rigid-mode count matters")
            freqs = freqs[:n]
        return _collect_modal(meta, freqs, n, notes, note)
    return {"ok": False, "error": f"no result collector for job kind {kind!r}"}


def register_cad_sim_tools(mcp: FastMCP) -> None:
    """CAD simulation MCP tools (T-RF8 split of cad_tools.py): run_fea_* + run_cfd_*."""


    # ---------------------------------------------------------------- simulation

    @mcp.tool()
    def run_fea_static(fixtures: Union[list, str], loads: Union[list, str],
                       material: Union[str, dict] = "steel",
                       mesh_fineness: Union[float, str] = 0.5, doc_id: Optional[str] = None,
                       background: bool = False) -> str:
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
        background : if True, return a job_id immediately and solve on a worker thread — poll
                     get_job(job_id). Use it for fine meshes that may exceed the call timeout.

        Returns {ok, doc_id, material, n_nodes, n_elems, mesh, summary:{...}} — the
        scalars live under "summary", NOT at the top level: summary.min_fos,
        summary.max_von_mises_mpa, summary.max_disp_mm (plus tresca/rankine/principal
        variants). min_fos = yield / max stress; > 1 holds. min_fos saturates at a
        15.0 cap, reported in "fos_note" — a capped value means "at least this", not
        "exactly this".
        Tip: query_geometry(...) first to confirm your fixture/load faces resolve.
        """
        try:
            bc_notes: list = []
            ctx = _build_static_study(fixtures, loads, material, mesh_fineness,
                                      doc_id, bc_notes)
            d = ctx["doc"]

            def _solve() -> dict:
                res = cb.call("sim_run_static", {"study": ctx["study"]})
                return _collect_static(ctx, res, bc_notes)

            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            if background:
                jid = _start_job("fea_static", _solve)
                return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                               "note": "solving on a worker thread — poll get_job(job_id)"})
            return _dumps(_solve_sync_or_handoff("fea_static", _solve))
        except _StudyRefusal as r:
            return _dumps(r.payload)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def run_fea_modal(fixtures: Union[list, str], material: Union[str, dict] = "steel",
                      n_modes: int = 6,
                      mesh_fineness: Union[float, str] = 0.5, doc_id: Optional[str] = None,
                      background: bool = False) -> str:
        """Modal FEA — the natural frequencies (Hz) of the held part. Returns the first n_modes.

        fixtures : list of face queries held fixed (e.g. ["bottom"]). An unconstrained part has
                   rigid-body modes near 0 Hz, so fix it like it is mounted. A query is
                   query_geometry vocabulary, a face SID from describe_part ("F58db6d"), or a
                   wrapper {"face": q} / {"faces": [q1, q2]}.
        material : name or explicit dict (density drives the mode frequencies).
        mesh_fineness : 0..1, or a preset word ("coarse"/"medium"/"fine"/"finest").
        background : if True, return a job_id and solve on a worker thread — poll get_job(job_id).
        """
        try:
            bc_notes: list = []
            ctx = _build_modal_study(fixtures, material, mesh_fineness, doc_id, bc_notes)
            d = ctx["doc"]
            fixture_sets = ctx["fixture_sets"]
            study = ctx["study"]

            def _solve() -> dict:
                # Free-free (no fixtures): the 0 Hz rigid-body cluster is 6-fold
                # degenerate. FIXED IN THE ENGINE 2026-07-12 (fea_modal.cpp
                # over-requests + widens the Krylov subspace + verifies the rigid
                # count internally), so this caller-side over-request + verify is
                # now a redundant SAFETY NET — kept because it is cheap and turns
                # any regression into a visible warning note instead of silently
                # wrong mode numbering.
                free_free = not fixture_sets
                solve_n = _modal_solve_n(n_modes, free_free)
                res = cb.call("sim_run_modal", {"study": study, "n_modes": solve_n})
                if not res.get("ok"):
                    return {"ok": False, "doc_id": d,
                            "error": res.get("error", "modal solve failed")}
                freqs = list(res.get("freqs_hz", []))
                note = None
                if free_free:
                    for _ in range(2):
                        if _n_rigid_modes(freqs) >= 6:
                            break
                        solve_n += 6
                        retry = cb.call("sim_run_modal",
                                        {"study": study, "n_modes": solve_n})
                        if not retry.get("ok"):
                            break
                        res, freqs = retry, list(retry.get("freqs_hz", []))
                    note = _modal_rigid_note(freqs)
                    freqs = freqs[:n_modes]
                return _collect_modal(ctx, freqs, n_modes, bc_notes, note)

            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            if background:
                jid = _start_job("fea_modal", _solve)
                return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                               "note": "solving on a worker thread — poll get_job(job_id)"})
            return _dumps(_solve_sync_or_handoff("fea_modal", _solve))
        except _StudyRefusal as r:
            return _dumps(r.payload)
        except Exception as e:  # noqa: BLE001
            return _err(e)


    # ------------------------------------------------ asynchronous solves (sim_* job family)
    # The synchronous tools above are unchanged. These four are the non-blocking contract:
    # start -> poll -> collect, with a cancel that really kills the solver, and results that
    # are never presented as converged unless they are.

    @mcp.tool()
    def sim_run(analysis: str = "fea_static",
                fixtures: Optional[Union[list, str]] = None,
                loads: Optional[Union[list, str]] = None,
                material: Union[str, dict] = "steel",
                mesh_fineness: Union[float, str] = 0.5,
                n_modes: int = 6,
                doc_id: Optional[str] = None,
                study: Optional[str] = None,
                label: Optional[str] = None) -> str:
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
                return _dumps({"ok": False, "error":
                               f"sim_run(analysis={analysis!r}) is not an asynchronous "
                               f"analysis. Supported: {list(_ASYNC_ANALYSES)}. Every other "
                               "solver keeps its own background=True + get_job(job_id) path "
                               "(run_cfd_flow, run_cfd_accurate, run_fea_frequency_response, "
                               "run_fea_fatigue)."})
            if fixtures is None:
                raise ValueError(
                    "sim_run needs fixtures=[...] — the face queries held fixed, e.g. "
                    '["bottom"]. For an unconstrained modal solve pass fixtures=[].')
            notes: list = []
            if kind == "fea_static":
                ctx = _build_static_study(fixtures, loads, material, mesh_fineness,
                                          doc_id, notes)
                method = "sim_run_static"
                params = {"study": ctx["study"]}
            else:
                ctx = _build_modal_study(fixtures, material, mesh_fineness, doc_id, notes)
                ctx["n_modes"] = int(n_modes)
                method = "sim_run_modal"
                params = {"study": ctx["study"],
                          "n_modes": _modal_solve_n(int(n_modes), ctx["free_free"])}
            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            meta = {k: v for k, v in ctx.items() if k != "study"}
            meta["notes"] = notes
            if label:
                meta["label"] = label
            job = simjobs.start(kind, "core_rpc",
                                {"method": method, "params": params}, meta)
            return _dumps({"ok": True, "job_id": job["job_id"], "state": "running",
                           "analysis": kind, "doc_id": ctx["doc"], "pid": job["pid"],
                           "note": "solving in a separate process — this call did NOT wait. "
                                   f"Poll sim_status('{job['job_id']}'), then "
                                   f"sim_results('{job['job_id']}'). The job is persisted, so "
                                   "it can be collected in a later turn.",
                           "next_actions": [f"sim_status('{job['job_id']}')"]})
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
            st.pop("meta", None)  # the resolved study setup is bulky and not useful here
            state = st["state"]
            if state == "done":
                st["next_actions"] = [f"sim_results('{job_id}')"]
            elif state == "running":
                st["next_actions"] = [f"sim_status('{job_id}')",
                                      f"sim_results('{job_id}', partial=True)",
                                      f"sim_cancel('{job_id}')"]
                st["note"] = ("still solving — nothing has converged yet; sim_results with "
                              "partial=True returns the last snapshot, clearly marked partial")
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
        """Collect a sim_run job's results.

        partial=False (default): the full converged result, in exactly the shape the
        synchronous tool returns. On a job that has NOT finished this REFUSES — it will not
        hand back half a solution dressed as an answer.

        partial=True: the last snapshot the solver published, always tagged
        `partial: true, converged: false`. When the stage published nothing (a direct FEA
        solve has no meaningful intermediate state) this says so instead of inventing one.
        """
        try:
            st = simjobs.status(job_id)
            state = st.get("state")
            if state == "unknown":
                return _dumps(st)
            if partial:
                snap = simjobs.partial(job_id)
                base = {"job_id": job_id, "state": state, "partial": True,
                        "converged": False, "elapsed_s": st.get("elapsed_s")}
                if not snap:
                    base.update(ok=False, error=(
                        f"no partial results exist for {job_id}: the {st.get('kind')} solve "
                        "published no intermediate state (a direct FEA solve has no "
                        "converging iterations to report). This is NOT an empty result — "
                        "nothing has been computed to report yet."))
                    return _dumps(base)
                base.update(ok=True, snapshot=snap, warning=(
                    "PARTIAL SNAPSHOT — this is the solver's last intermediate state, NOT a "
                    "converged solution. Do not quote these numbers as results."))
                if state == "done":
                    base["note"] = ("the job has since FINISHED — call "
                                    f"sim_results('{job_id}') for the converged result")
                return _dumps(base)
            if state != "done":
                out = {"ok": False, "job_id": job_id, "state": state,
                       "elapsed_s": st.get("elapsed_s"),
                       "error": f"job {job_id} is {state} — there are no converged results to "
                                "return. Refusing to answer with a partial or absent solution."}
                if state in ("running", "queued"):
                    out["hint"] = (f"poll sim_status('{job_id}'), or "
                                   f"sim_results('{job_id}', partial=True) for the last "
                                   "snapshot (clearly marked partial)")
                elif st.get("error"):
                    out["solver_error"] = st["error"]
                return _dumps(out)
            raw = simjobs.raw_result(job_id)
            if raw is None:
                return _dumps({"ok": False, "job_id": job_id, "state": state,
                               "error": "the job is marked done but its result file could not "
                                        "be read — nothing can be reported honestly. Re-run "
                                        "the solve."})
            out = _collect_job(st, raw)
            out.update(job_id=job_id, state="done", converged=True,
                       elapsed_s=st.get("elapsed_s"))
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def sim_jobs(limit: int = 20) -> str:
        """List recent sim_run jobs (newest first) with their reconciled state and elapsed
        time. Jobs persist across agent turns, so this also shows solves an earlier turn
        started."""
        try:
            rows = []
            for j in simjobs.list_jobs(int(limit)):
                meta = j.pop("meta", None) or {}
                j["doc_id"] = meta.get("doc")
                if meta.get("label"):
                    j["label"] = meta["label"]
                rows.append(j)
            return _dumps({"ok": True, "jobs": rows, "count": len(rows),
                           "registry": str(simjobs.jobs_dir())})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def run_fea_frequency_response(fixtures: Union[list, str],
                                   excitation_dir: Optional[Union[list, str]] = None,
                                   sweep_hz: Optional[Union[list, str]] = None, zeta: float = 0.02,
                                   base_accel_g: float = 1.0,
                                   psd_breakpoints: Optional[Union[list, str]] = None,
                                   vibration_spec: Optional[Union[dict, str]] = None,
                                   n_modes: int = 6,
                                   material: Union[str, dict] = "steel",
                                   mesh_fineness: Union[float, str] = 0.5,
                                   doc_id: Optional[str] = None,
                                   background: bool = False) -> str:
        """Harmonic + random (PSD) vibration response of the held part vs a base excitation.

        Extends run_fea_modal into a frequency-response solve by modal superposition:
        each mode becomes an SDOF oscillator (participation-factor weighted); harmonic
        response uses the classical transmissibility sweep, random response uses Miles'
        equation per mode (Steinberg 2000 Eq. 6.6). Modes combine by SRSS.

        fixtures       : face queries held fixed (base/support) — same syntax as run_fea_modal.
        excitation_dir : base-excitation unit direction [dx,dy,dz]; default [0,0,1] (Z-up axis).
        sweep_hz       : harmonic sweep points; default 200 log-spaced points, 10–2000 Hz.
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
            d = _doc(doc_id)
            fixtures = require_list(fixtures, "fixtures",
                                     'face queries held fixed, e.g. ["bottom"]')
            bc_notes: list = []
            fixtures = _norm_bc_list(fixtures, "fixtures", unwrap_query=True, notes=bc_notes)
            mesh_fineness = _norm_fineness(mesh_fineness)
            if excitation_dir is not None:
                excitation_dir = require_list(excitation_dir, "excitation_dir",
                                               "a 3-vector [dx,dy,dz]")
            if sweep_hz is not None:
                sweep_hz = require_list(sweep_hz, "sweep_hz", "a list of frequencies in Hz")
            if psd_breakpoints is not None:
                psd_breakpoints = require_list(psd_breakpoints, "psd_breakpoints",
                                                "a list of [f_hz, psd_g2_per_hz] pairs")
            if vibration_spec is not None:
                vibration_spec = require_mapping(
                    vibration_spec, "vibration_spec",
                    'a dict like {"max_peak_accel_g": g} and/or {"max_grms": g}')
            mat = _resolve_material(coerce_json_or_repr(material))
            ex_dir = [float(x) for x in (excitation_dir or [0.0, 0.0, 1.0])]
            if sweep_hz is not None:
                sweep = [float(f) for f in sweep_hz]
            else:  # default: 200 log-spaced points across the common vibe-test band
                sweep = [10.0 * (2000.0 / 10.0) ** (i / 199.0) for i in range(200)]
            psd = None
            if psd_breakpoints is not None:
                psd = [(float(p[0]), float(p[1])) for p in psd_breakpoints]
            step = _tmp(".step")
            cb.call("doc_export_step", {"doc_id": d, "path": step})
            fixture_sets: list = []
            for q in fixtures:
                fixture_sets.extend(_sim_face_points(d, q))
            if fixtures and not fixture_sets:
                return _dumps({"ok": False, "error": "no fixture faces resolved — "
                               "the face queries you provided didn't match any surface "
                               "points. Use query_geometry() to find the right face query."})
            study = {"step_path": step, "material": mat, "fixtures": fixture_sets,
                     "mesh_order": 2, "fineness": mesh_fineness}

            def _solve() -> dict:
                res = cb.call("sim_run_modal", {"study": study, "n_modes": n_modes})
                if not res.get("ok"):
                    return {"ok": False, "doc_id": d,
                            "error": res.get("error", "modal solve failed")}
                freqs = res.get("freqs_hz", [])
                shapes = res.get("mode_shapes", [])
                n_nodes = int(res.get("n_nodes", 0))
                if not freqs or not shapes or n_nodes <= 0:
                    return {"ok": False, "doc_id": d, "error":
                            "modal solve returned no mode shapes — cannot build the "
                            "frequency response (need freqs_hz + mode_shapes + n_nodes)"}
                # Total mass from the solid at the study material's density (eval_mass
                # returns mass_g = volume_mm3 * density; kg = g / 1000).
                dens = float(mat.get("density_g_cm3", 0.0))
                meas = cb.call("eval_mass", {"doc_id": d, "density_g_cm3": dens})
                total_mass_kg = float((meas or {}).get("mass_g") or 0.0) / 1000.0
                if total_mass_kg <= 0.0:
                    return {"ok": False, "doc_id": d, "error":
                            "could not determine part mass (volume or density missing) — "
                            "set a material with density_g_cm3 and ensure the part has "
                            "solid volume"}
                # Lazy import — keeps server module load independent of numpy availability.
                from services.sim.vibration import (
                    evaluate_vibration_spec, harmonic_response, miles_response)
                harm = harmonic_response(
                    freqs_hz=freqs, mode_shapes_flat=shapes, n_nodes=n_nodes,
                    total_mass_kg=total_mass_kg, excitation_dir=ex_dir,
                    sweep_hz=sweep, zeta=zeta, base_accel_g=base_accel_g)
                if psd is not None:
                    rand: dict = miles_response(
                        freqs_hz=freqs, mode_shapes_flat=shapes, n_nodes=n_nodes,
                        total_mass_kg=total_mass_kg, excitation_dir=ex_dir,
                        psd_breakpoints=psd, zeta=zeta)
                    rand["ok"] = True
                else:  # honest degrade — NEVER assume a default vibration profile
                    rand = {"ok": False, "reason": "no_psd_provided",
                            "hint": "Provide psd_breakpoints=[[f_hz, g2_per_hz], ...] "
                                    "(e.g. from the qualification spec) for random-"
                                    "vibration GRMS via Miles' equation."}
                verdict = evaluate_vibration_spec(
                    vibration_spec, harm, rand if rand.get("ok") else None)
                _persist_study(d, {
                    "type": "fea_frequency_response",
                    "name": f"Frequency response ({mat.get('name', material)})",
                    "engine": "cpp+py_modal_superposition",
                    "material": mat, "fixtures": fixtures, "n_modes": n_modes,
                    "mesh_fineness": mesh_fineness,
                    "results": {"freqs_hz": freqs,
                                "peak_accel_g": harm.get("peak_accel_g"),
                                "peak_freq_hz": harm.get("peak_freq_hz"),
                                "total_grms": rand.get("total_grms"),
                                "verdict": verdict},
                })
                return _interpreted(
                    {"ok": True, "doc_id": d, "material": mat.get("name", material),
                     "freqs_hz": freqs, "harmonic": harm, "random": rand,
                     "verdict": verdict,
                     "damping_assumption": f"zeta={zeta} (Q={1.0 / (2.0 * zeta):g}) — "
                                           "assumed modal damping, not measured",
                     "saved_to_study": True}, bc_notes)

            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            if background:
                jid = _start_job("fea_frequency_response", _solve)
                return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                               "note": "solving on a worker thread — poll get_job(job_id)"})
            return _dumps(_solve_sync_or_handoff("fea_frequency_response", _solve))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def run_cfd_flow(velocity: float = 10.0, aoa: float = 0.0, fluid: str = "air",
                     cells: int = 96, n_steps: int = 2000, doc_id: Optional[str] = None,
                     background: bool = False) -> str:
        """Wind-tunnel CFD over the current part/assembly (LBM). Returns drag, lift, Cd, Reynolds.

        CONVENTIONS (Chudware is Z-up): freestream is along +X; LIFT is the +Z (up) force; SPAN is Y.
        +aoa pitches the nose up (rotation about Y), so a cambered wing makes +lift. `reynolds` and
        `cd` use ref_length_mm = the part's LONGEST span (NOT chord) — for an airfoil, simulate the
        wing alone or read `ref_length_mm` and renormalise to chord yourself.

        velocity : free-stream speed (m/s).   aoa : angle of attack (degrees).
        fluid    : "air" or "water".          cells : grid pitch = domain's longest span / cells
                   (higher = finer + slower; practical 48-200).  n_steps : LBM iterations (~500-4000).
        background : if True, return a job_id immediately and solve on a worker thread (recommended
                     for cells>=120 or n_steps>=2500, which can exceed the call timeout) — then poll
                     get_job(job_id).

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
            d = _doc(doc_id)
            # Live-progress side-channel: the engine writes {pct,step,n_steps} to this file as the
            # LBM iterates; get_job reads it so a background run reports progress like OpenFOAM.
            prog_path = _tmp(".json")
            prog: dict = {"pct": 0, "stage": "lbm_solve", "progress_file": prog_path}

            def _solve() -> dict:
                res = cb.call("sim_run_flow", {"doc_id": d, "velocity": velocity, "aoa": aoa,
                                               "fluid": fluid, "cells": cells, "n_steps": n_steps,
                                               "progress_path": prog_path})
                if not res.get("ok"):
                    return {"ok": False, "doc_id": d,
                            "error": res.get("error", "flow sim failed")}
                out = {"ok": True, "doc_id": d}
                for k in ("drag_n", "lift_n", "cd", "frontal_area_mm2", "reynolds",
                          "ref_length_mm", "velocity_ms", "aoa_deg", "fluid", "grid",
                          "converged", "residual", "voxel_mm", "min_feature_mm",
                          "min_feature_voxels", "steps_run", "diverged", "warning"):
                    if k in res:
                        out[k] = res[k]
                _persist_study(d, {
                    "type": "cfd_flow",
                    "name": f"CFD wind tunnel ({fluid}, {velocity} m/s, aoa {aoa})",
                    "engine": "cpp-lbm",
                    "params": {"velocity": velocity, "aoa": aoa, "fluid": fluid,
                               "cells": cells, "n_steps": n_steps},
                    "results": {k: out[k] for k in out if k not in ("ok", "doc_id")},
                })
                out["saved_to_study"] = True
                return out

            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            if background:
                jid = _start_job("cfd_flow", _solve, progress=prog)
                return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                               "note": "solving on a worker thread — poll get_job(job_id)"})
            return _dumps(_solve_sync_or_handoff("cfd_flow", _solve, progress=prog))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def run_cfd_accurate(velocity: float = 10.0, aoa: float = 0.0, fluid: str = "air",
                         preset: str = "coarse", doc_id: Optional[str] = None) -> str:
        """HIGH-FIDELITY external-aero CFD via OpenFOAM (steady RANS, k-omega SST + prism layers).

        Use this when you need a TRUSTWORTHY absolute Cd/Cl, not the coarse directional estimate
        from run_cfd_flow (the built-in LBM). This is the validated path — sphere/cube land within
        ~10-15% of experiment (regression gate: tools/foam_validation.py). It body-fits a
        snappyHexMesh with boundary layers and solves to convergence, so it takes MINUTES and
        requires OpenFOAM installed — detected automatically on Linux (sourced env / system install /
        user-space conda env / CHUDWARE_OPENFOAM root), native Windows blueCFD, or WSL openfoam2412.
        All OpenFOAM invocations are subprocesses (GPL isolation — never linked). It ALWAYS runs in
        the background: returns a job_id immediately; poll get_job(job_id) (which reports live
        meshing/solve progress) for the result.

        CONVENTIONS match run_cfd_flow (Z-up): freestream +X, lift +Z, +aoa pitches nose up.
        velocity : m/s.   aoa : degrees.   fluid : "air" | "water".
        preset   : "coarse" (fastest, ~600 iters) | "medium" | "fine" | "xfine" (finest/slowest).
                   Start coarse to sanity-check, refine if you need the digits.

        Result (via get_job): {cd, cl, ld (lift/drag), drag_n, lift_n, reynolds, ref_area_mm2,
        vmax_mps, converged, iters, notes}. If `converged` is false, raise the preset / iterations.
        """
        try:
            from services.sim.foam_runner import capabilities
            caps = capabilities()
            if not caps.get("openfoam"):
                import os as _os
                how = ("`python -m tools.provision_openfoam --install` (user-space conda-forge "
                       "env, no sudo), or unpack an openfoam.com tarball under ~/OpenFOAM/, or "
                       "set CHUDWARE_OPENFOAM to an install root"
                       if _os.name == "posix" else
                       "`python -m tools.provision_openfoam --install` (WSL openfoam2412) or "
                       "install blueCFD-Core natively and set %CHUDWARE_OPENFOAM%")
                return _dumps({"ok": False, "skipped": True, "reason": "openfoam_not_installed",
                               "error": "OpenFOAM is not available on this machine. Provision it "
                               f"with {how}. The coarse LBM run_cfd_flow still works."})
            if preset not in ("coarse", "medium", "fine", "xfine"):
                return _dumps({"ok": False, "error": f"unknown preset {preset!r} — use "
                               "coarse|medium|fine|xfine"})
            d = _doc(doc_id)
            # OpenFOAM meshes a watertight STL surface (build_case handles mm -> m).
            stl = _tmp(".stl")
            cb.call("export_file", {"doc_id": d, "path": stl, "format": "stl"})
            fluid_dict = ({"nu": 1.0e-6, "rho": 1000.0} if fluid == "water"
                          else {"nu": 1.5e-5, "rho": 1.225})

            prog: dict = {"pct": 0, "stage": "start", "text": "queued"}

            def _on_progress(ev: dict) -> None:
                prog.update(pct=ev.get("pct", 0), stage=ev.get("stage", ""),
                            text=ev.get("text", ""))

            def _solve() -> dict:
                from services.sim.openfoam import run_openfoam_tunnel
                r = run_openfoam_tunnel(stl, direction=(1.0, 0.0, 0.0),
                                        speed_mps=float(velocity), preset=preset,
                                        aoa_deg=float(aoa), fluid=fluid_dict,
                                        on_progress=_on_progress)
                return {"ok": True, "doc_id": d,
                        "solver": f"OpenFOAM RANS (k-omega SST, {caps.get('openfoam_kind')})",
                        "preset": preset, "cd": r.cd, "cl": r.cl, "ld": r.ld,
                        "drag_n": r.drag_n, "lift_n": r.lift_n, "side_n": r.side_n,
                        "reynolds": r.reynolds, "ref_area_mm2": r.ref_area_mm2,
                        "vmax_mps": r.vmax_mps, "converged": (not r.diverged),
                        "iters": r.steps_run, "notes": r.solver_notes,
                        "velocity_ms": float(velocity), "aoa_deg": float(aoa), "fluid": fluid}

            jid = _start_job("cfd_openfoam", _solve, progress=prog)
            return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                           "solver": caps.get("openfoam_version"), "preset": preset,
                           "note": "OpenFOAM RANS runs for minutes — poll get_job(job_id); it "
                                   "reports live meshing/solve progress."})
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

    def _part_bbox_dims(d: str) -> tuple:
        """Return (L, b, h) = the part's world bounding-box size (mm), sorted so
        L is the longest span (the slender axis for beam/column proxies)."""
        r = cb.call("asm_world_bodies", {"doc_id": d, "explode": False})
        bodies = r.get("bodies", [])
        if not bodies:
            raise ValueError("no bodies in the document — build geometry first")
        # Union of all body bboxes.
        lo = [float("inf")] * 3
        hi = [float("-inf")] * 3
        for bd in bodies:
            bb = bd.get("bbox", {})
            mn, mx = bb.get("min"), bb.get("max")
            if not (mn and mx):
                continue
            for i in range(3):
                lo[i] = min(lo[i], float(mn[i]))
                hi[i] = max(hi[i], float(mx[i]))
        size = sorted((hi[i] - lo[i] for i in range(3)), reverse=True)
        if size[0] <= 0:
            raise ValueError("degenerate bounding box")
        return size[0], size[1] if size[1] > 0 else size[0], \
            size[2] if size[2] > 0 else size[0]



    @mcp.tool()
    def run_fea_fatigue(fixtures: Union[list, str], loads: Union[list, str],
                        material: Union[str, dict] = "steel",
                        load_type: str = "reversed", load_ratio: float = -1.0,
                        surface: str = "machined", reliability_pct: float = 50.0,
                        temperature_c: float = 20.0, theory: str = "goodman",
                        mesh_fineness: Union[float, str] = 0.5, doc_id: Optional[str] = None,
                        background: bool = False) -> str:
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

        Returns {ok, regime: infinite|finite|static_overload, cycles, fos_endurance,
        se_corrected_mpa, theories{...}}. regime="infinite" ⇒ >1e6 cycles (runout).
        Degrades like run_fea_static when the C++ FEA mesher backend is unavailable.
        """
        try:
            from . import fea_advanced as fa
            d = _doc(doc_id)
            fixtures = require_list(fixtures, "fixtures",
                                     'SAME plain-English face queries as run_fea_static')
            loads = require_list(loads, "loads",
                                  'SAME {face + force/pressure} shape as run_fea_static')
            bc_notes: list = []
            fixtures = _norm_bc_list(fixtures, "fixtures", unwrap_query=True, notes=bc_notes)
            loads = _norm_bc_list(loads, "loads", notes=bc_notes)
            mesh_fineness = _norm_fineness(mesh_fineness)
            mat = _resolve_material(coerce_json_or_repr(material))
            Sut = float(mat.get("ultimate_mpa") or 0.0)
            Se_base = float(mat.get("endurance_mpa") or 0.0)
            if Sut <= 0:
                return _dumps({"ok": False, "error":
                               f"material {mat.get('name', material)!r} has no ultimate_mpa — "
                               "fatigue needs ultimate + endurance strength."})
            step = _tmp(".step")
            cb.call("doc_export_step", {"doc_id": d, "path": step})
            fixture_sets: list = []
            for q in fixtures:
                fixture_sets.extend(_sim_face_points(d, q))
            if not fixture_sets:
                return _dumps({"ok": False, "error": (
                    "fatigue FEA needs at least one fixed face — you passed fixtures=[]. "
                    "An unconstrained part has nothing to react the cyclic load against."
                    if not fixtures else
                    "no fixture faces resolved — use query_geometry() to confirm the "
                    "face query.")})
            load_specs = _build_load_specs(d, loads, "fatigue")

            surf_map = {"ground": 0, "machined": 1, "hot_rolled": 2, "as_forged": 3}
            lt_map = {"reversed": {"lmin": -1.0, "lmax": 1.0},
                      "zero_based": {"lmin": 0.0, "lmax": 1.0},
                      "ratio": {"lmin": float(load_ratio), "lmax": 1.0}}
            corr = fa.corrected_endurance(Se_base, Sut, surface=surf_map.get(surface, 1),
                                          load_case=0, temperature_c=temperature_c,
                                          reliability_pct=reliability_pct)
            fat = {"ultimate_mpa": Sut, "yield_mpa": float(mat.get("yield_mpa") or 0.0),
                   "se_corrected_mpa": corr["se_corrected_mpa"],
                   "se_base_mpa": corr["se_base_mpa"]}
            fat.update(lt_map.get(load_type, lt_map["reversed"]))
            study = {"step_path": step, "material": mat, "fixtures": fixture_sets,
                     "loads": load_specs, "mesh_order": 2, "fineness": mesh_fineness,
                     "fatigue": fat}

            def _solve() -> dict:
                try:
                    res = cb.call("sim_run_fatigue", {"study": study})
                except CoreError as ce:
                    if "unknown method" in str(ce):
                        return {"ok": False, "skipped": True, "doc_id": d,
                                "backend": "cpp fea",
                                "reason": "the C++ FEA solver backend (sim_run_fatigue) is not "
                                          "available in this build (mesher not compiled). Fatigue "
                                          "reuses the static stress field — it needs the same FEA "
                                          "backend as run_fea_static."}
                    raise
                if not res.get("ok"):
                    return {"ok": False, "doc_id": d,
                            "error": res.get("error", "fatigue solve failed")}
                theories = res.get("summary", {}).get("theories", {})
                life = fa.fatigue_summarize(theories, corr["se_corrected_mpa"], Sut,
                                            theory=theory)
                life.update({"ok": True, "doc_id": d, "marin": corr,
                             "material": mat.get("name", material)})
                _persist_study(d, {
                    "type": "fea_fatigue",
                    "name": f"Fatigue ({mat.get('name', material)}, {theory})",
                    "engine": "cpp",
                    "material": mat, "fixtures": fixtures, "loads": loads,
                    "results": {"theories": life["theories"], "regime": life["regime"],
                                "cycles": life["cycles"], "fos_endurance": life["fos_endurance"]},
                })
                life["saved_to_study"] = True
                return _interpreted(life, bc_notes)

            _autosave_all_open()
            if background:
                jid = _start_job("fea_fatigue", _solve)
                return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d})
            return _dumps(_solve_sync_or_handoff("fea_fatigue", _solve))
        except _StudyRefusal as r:
            return _dumps(r.payload)
        except CoreError as e:
            # C++ FEA backend absent (mesher not built) → honest degrade, same as static.
            if "unknown method" in str(e):
                return _dumps({"ok": False, "skipped": True,
                               "reason": "the C++ FEA solver backend (sim_run_fatigue) is not "
                                         "available in this build (mesher not compiled). Fatigue "
                                         "reuses the static stress field — it needs the same FEA "
                                         "backend as run_fea_static.", "backend": "cpp fea"})
            return _err(e)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def run_fea_thermal(hot_faces: Union[list, str], cold_faces: Union[list, str],
                        material: Union[str, dict] = "steel",
                        conductivity_w_mk: Optional[float] = None,
                        transient: Optional[Union[dict, str]] = None,
                        loads: Optional[Union[list, str]] = None,
                        specific_heat_j_kgk: Optional[float] = None,
                        mesh_fineness: Union[float, str] = 0.5,
                        doc_id: Optional[str] = None) -> str:
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
        """
        try:
            bc_notes: list = []
            hot_faces = require_list(hot_faces, "hot_faces",
                                      'a list of {"face": <query>, "temperature": <°C>}')
            cold_faces = require_list(cold_faces, "cold_faces",
                                       'a list of {"face": <query>, "temperature": <°C>}')
            if loads is not None:
                loads = require_list(loads, "loads",
                                      'a list of {"face": <query>, "flux_w_m2": <q>} or '
                                      '{"face": <query>, "h_w_m2k": <h>} entries')
                loads = _norm_bc_list(loads, "loads", notes=bc_notes)
            hot_faces = _norm_bc_list(hot_faces, "hot_faces", notes=bc_notes)
            cold_faces = _norm_bc_list(cold_faces, "cold_faces", notes=bc_notes)
            mesh_fineness = _norm_fineness(mesh_fineness)
            if transient is not None:
                transient = require_mapping(transient, "transient",
                                             'a dict like {"t_end_s": 60, "dt_s": 0.5}')
            material = coerce_json_or_repr(material)
            if transient is not None:
                d = _doc(doc_id)
                mat = _resolve_material(material)
                k_cond = (float(conductivity_w_mk) if conductivity_w_mk is not None
                          else float(mat.get("conductivity_w_mk")
                                     or _default_conductivity(mat, material)))
                if not transient.get("t_end_s"):
                    return _dumps({"ok": False, "error":
                                   "transient needs a dict with t_end_s (seconds), e.g. "
                                   "{\"t_end_s\": 60, \"dt_s\": 0.5}"})
                cp = (float(specific_heat_j_kgk) if specific_heat_j_kgk is not None
                      else float(mat.get("cp_j_kgk") or _default_specific_heat(mat, material)))
                step = _tmp(".step")
                cb.call("doc_export_step", {"doc_id": d, "path": step})

                def _faceset(q):
                    merged = _merge_face_sets(_sim_face_points(d, q))
                    return merged if merged["points"] else None

                fixtures = []
                for f in (list(hot_faces or []) + list(cold_faces or [])):
                    if not isinstance(f, dict) or "temperature" not in f or "face" not in f:
                        raise ValueError("each hot/cold face needs {'face': <query>, "
                                         f"'temperature': <°C>}}: {f!r}")
                    fs = _faceset(f["face"])
                    if fs is None:
                        return _dumps({"ok": False, "error":
                                       f"face query {f['face']!r} resolved to no faces — "
                                       "use query_geometry() to confirm"})
                    fs["temp_c"] = float(f["temperature"])
                    fixtures.append(fs)

                load_specs = []
                for ld in (loads or []):
                    if not isinstance(ld, dict) or "face" not in ld:
                        raise ValueError(f"each thermal load needs a 'face' query: {ld!r}")
                    fs = _faceset(ld["face"])
                    if fs is None:
                        return _dumps({"ok": False, "error":
                                       f"load face query {ld['face']!r} resolved to no faces"})
                    if "flux_w_m2" in ld:
                        spec = {"faces": fs, "type": "flux",
                                "magnitude": float(ld["flux_w_m2"])}
                        if isinstance(ld.get("schedule"), dict):
                            spec["schedule"] = ld["schedule"]
                    elif "h_w_m2k" in ld:
                        spec = {"faces": fs, "type": "convection",
                                "h": float(ld["h_w_m2k"]),
                                "t_amb_c": float(ld.get("t_amb_c", 20.0))}
                    else:
                        raise ValueError("thermal load needs 'flux_w_m2' (+ optional "
                                         f"'schedule') or 'h_w_m2k'/'t_amb_c': {ld!r}")
                    load_specs.append(spec)

                # An adiabatic heat-up with no Dirichlet face is well-posed ONLY when a
                # flux/convection load drives it. With neither, C dT/dt + K T = 0 from a
                # uniform t_init has the constant solution: measured 2026-07-30,
                # transient={"t_end_s":10,"dt_s":1} with no faces and no loads returned
                # ok:true, max_temp_c 20.0 == t_init — an "answer" that is just the input.
                if not fixtures and not load_specs:
                    return _dumps({"ok": False, "error": (
                        "the transient study has no thermal input at all — no hot/cold face "
                        "held at a temperature and no flux/convection load, so the part just "
                        f"stays at t_init_c ({float(transient.get('t_init_c', 20.0))} °C) for "
                        "the whole run. Give it at least one Dirichlet face "
                        '(hot_faces=[{"face":"+x","temperature":100}]) or one load '
                        '(loads=[{"face":"top","flux_w_m2":5000}] / '
                        '[{"face":"top","h_w_m2k":25,"t_amb_c":20}]).')})

                study = {"step_path": step,
                         "material": {**mat, "k_w_mk": k_cond, "cp_j_kgk": cp},
                         "fixtures": fixtures, "loads": load_specs,
                         "fineness": mesh_fineness,
                         "transient": {
                             "t_end_s": float(transient["t_end_s"]),
                             "dt_s": float(transient.get("dt_s") or 0.0),
                             "t_init_c": float(transient.get("t_init_c", 20.0)),
                             "frame_times_s": [float(t) for t in
                                               transient.get("frame_times_s", [])],
                             "capacitance": transient.get("capacitance", "lumped"),
                         }}
                res = cb.call("sim_run_thermal", {"study": study})
                if not res.get("ok"):
                    return _dumps({"ok": False, "doc_id": d,
                                   "error": res.get("error", "transient thermal solve failed")})
                frames = res.get("frames") or []
                frame_summ = [{"t_s": fr.get("t_s"),
                               "max_temp_c": round(max(fr.get("temperature") or [0.0]), 3)}
                              for fr in frames]
                out = {"ok": True, "doc_id": d, "transient": True,
                       "solver": "C++ core transient conduction (backward Euler, "
                                 f"{study['transient']['capacitance']} capacitance)",
                       "material": mat.get("name", material),
                       "conductivity_w_mk": k_cond, "specific_heat_j_kgk": cp,
                       "summary": res.get("summary", {}),
                       "n_steps": res.get("n_steps"), "n_nodes": res.get("n_nodes"),
                       "n_elems": res.get("n_elems"),
                       "frames": frame_summ,
                       "solver_notes": res.get("solver_notes", []),
                       "note": "summary.max_temp_c is the PEAK over the whole run "
                               "(max-temp envelope), at summary.max_temp_time_s; "
                               "per-frame nodal fields persisted in the doc study."}
                _persist_study(d, {"type": "fea_thermal_transient",
                                   "name": f"Transient thermal ({mat.get('name', material)})",
                                   "engine": "cpp",
                                   "material": {**mat, "k_w_mk": k_cond, "cp_j_kgk": cp},
                                   "transient": study["transient"],
                                   "results": {"summary": res.get("summary", {}),
                                               "frames": frames,
                                               "envelope_max": res.get("envelope_max")}})
                return _dumps(_interpreted(out, bc_notes))

            from . import fea_advanced as fa
            if fa.ccx_path() is None:
                return _dumps(fa._ccx_absent_result("thermal steady-state"))
            d = _doc(doc_id)
            mat = _resolve_material(material)
            k_cond = (float(conductivity_w_mk) if conductivity_w_mk is not None
                      else float(mat.get("conductivity_w_mk") or _default_conductivity(mat, material)))
            T_hot = _first_temperature(hot_faces, "hot_faces")
            T_cold = _first_temperature(cold_faces, "cold_faces")
            L, b, h = _part_bbox_dims(d)
            gen = fa.build_thermal_bar_inp(L, b, h, k_cond, T_hot, T_cold)
            run = fa.run_ccx(gen["inp"], job="thermal")
            if run.get("skipped"):
                return _dumps(run)
            if not run.get("ok") or not run.get("dat_path"):
                return _dumps({"ok": False, "error": run.get("stderr", "")[-400:]
                               or run.get("error", "ccx thermal run failed"),
                               "backend": run.get("backend")})
            mid_T = fa.parse_dat_nodal(run["dat_path"], "NT", gen["mid_nodes"], component=1)
            out = {"ok": True, "doc_id": d, "backend": run["backend"],
                   "solver": "CalculiX *HEAT TRANSFER, STEADY STATE (box proxy)",
                   "conductivity_w_mk": k_cond, "T_hot_c": T_hot, "T_cold_c": T_cold,
                   "mid_plane_T_c": (round(mid_T, 3) if mid_T is not None else None),
                   "expected_mid_T_c": round(gen["expected_mid_T"], 3),
                   "note": "box-proxy 1-D conduction estimate; ccx GPL-2.0 subprocess."}
            _persist_study(d, {"type": "fea_thermal", "name": f"Thermal ({mat.get('name', material)})",
                               "engine": "calculix", "results": out})
            return _dumps(_interpreted(out, bc_notes))
        except CoreError as e:
            return _err(e)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def run_fea_buckling(fixtures: list, load: Union[list, str], material: Union[str, dict] = "steel",
                         n_modes: int = 4, doc_id: Optional[str] = None) -> str:
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
            from . import fea_advanced as fa
            if fa.ccx_path() is None:
                return _dumps(fa._ccx_absent_result("linear buckling"))
            d = _doc(doc_id)
            load = require_list(load, "load",
                                 'a single-item list [{"face": <query>, "force": [x,y,z]}]')
            bc_notes: list = []
            load = _norm_bc_list(load, "load", notes=bc_notes)
            mat = _resolve_material(coerce_json_or_repr(material))
            E = float(mat["E_mpa"]); nu = float(mat.get("nu", 0.3))
            # A malformed load used to fall back to a hard-coded 1000 N SILENTLY (ok:true,
            # a critical_loads_n the caller never asked for) — worse than a crash, because
            # the response looked like a real answer to a different question. Teach the
            # shape instead of guessing a magnitude the caller never supplied.
            if not (load and isinstance(load[0], dict) and "force" in load[0]):
                raise ValueError(
                    "load must be a single-item list [{'face': <query>, 'force': [x,y,z]}] "
                    f"— got {load!r}. The force magnitude is what the buckling load factor "
                    "scales; there is no safe default to assume. " + _BC_SHAPE_HELP)
            vec = load[0]["force"]
            P = math.sqrt(sum(float(x) ** 2 for x in vec))
            # The surviving half of that same defect: `max(1.0, |F|)` turned force=[0,0,0]
            # into a 1 N study and reported `applied_load_n: 1.0` plus a full
            # critical_loads_n table (measured 2026-07-30). The clamp existed to keep the
            # λ-scaling from dividing by zero; refusing is the honest way to do that.
            if P <= 0.0:
                raise ValueError(
                    f"the buckling load is zero ({vec!r}). λ scales the APPLIED load, so a "
                    "zero load makes every critical_loads_n zero and the answer "
                    "meaningless. Pass the real compressive load, e.g. "
                    "load=[{'face':'top','force':[0,0,-1000]}].")
            L, b, h = _part_bbox_dims(d)
            gen = fa.build_buckling_column_inp(L, b, h, E, nu, P, nz=4)
            run = fa.run_ccx(gen["inp"], job="buckle")
            if run.get("skipped"):
                return _dumps(run)
            if not run.get("ok") or not run.get("dat_path"):
                return _dumps({"ok": False, "error": run.get("stderr", "")[-400:]
                               or run.get("error", "ccx buckling run failed"),
                               "backend": run.get("backend")})
            factors = fa.parse_buckle_factors(run["dat_path"])[:n_modes]
            crit = [round(f * P, 3) for f in factors]
            out = {"ok": True, "doc_id": d, "backend": run["backend"],
                   "solver": "CalculiX *BUCKLE (box proxy, fixed-free column)",
                   "applied_load_n": P, "load_factors": [round(f, 4) for f in factors],
                   "critical_loads_n": crit,
                   "euler_reference_pcr_n": round(gen["expected_Pcr"], 3),
                   "note": "box-proxy Euler buckling estimate; ccx GPL-2.0 subprocess. "
                           "load_factor<1 ⇒ buckles below the applied load."}
            _persist_study(d, {"type": "fea_buckling",
                               "name": f"Buckling ({mat.get('name', material)})",
                               "engine": "calculix", "results": out})
            return _dumps(_interpreted(out, bc_notes))
        except CoreError as e:
            return _err(e)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def run_fea_contact(pressure_mpa: float = 1.0, material: Union[str, dict] = "steel",
                        doc_id: Optional[str] = None) -> str:
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
            from . import fea_advanced as fa
            if fa.ccx_path() is None:
                return _dumps(fa._ccx_absent_result("nonlinear contact"))
            d = _doc(doc_id)
            mat = _resolve_material(coerce_json_or_repr(material))
            E = float(mat["E_mpa"]); nu = float(mat.get("nu", 0.3))
            L, b, h = _part_bbox_dims(d)
            gen = fa.build_contact_block_inp(L, b, h, E, nu, float(pressure_mpa))
            run = fa.run_ccx(gen["inp"], job="contact")
            if run.get("skipped"):
                return _dumps(run)
            if not run.get("ok") or not run.get("dat_path"):
                return _dumps({"ok": False, "error": run.get("stderr", "")[-400:]
                               or run.get("error", "ccx contact run failed"),
                               "backend": run.get("backend")})
            rf = fa.parse_dat_nodal(run["dat_path"], "RF", gen["base_nodes"], component=3)
            reaction = abs(rf * len(gen["base_nodes"])) if rf is not None else None
            out = {"ok": True, "doc_id": d, "backend": run["backend"],
                   "solver": "CalculiX *STATIC,NLGEOM + *CONTACT PAIR (two-block proxy)",
                   "applied_load_n": round(gen["applied_load_n"], 3),
                   "base_reaction_n": (round(reaction, 3) if reaction is not None else None),
                   "note": "box-proxy contact conservation check; base reaction should "
                           "equal the applied load. ccx GPL-2.0 subprocess."}
            _persist_study(d, {"type": "fea_contact",
                               "name": f"Contact ({mat.get('name', material)})",
                               "engine": "calculix", "results": out})
            return _dumps(out)
        except CoreError as e:
            return _err(e)
        except Exception as e:  # noqa: BLE001
            return _err(e)
