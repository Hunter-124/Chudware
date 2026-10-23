"""Research / look-it-up MCP tools — the ``research_*`` family.

Thin MCP adapter over ``services.research`` (the pure brain). Contract:
``contracts/research_tools.md``. v1 exposes ``web_search`` + ``fetch_page``; every helper
returns the §1 answer envelope (``{ok, answer, units, source_url, quote, fetched_ts,
confidence, cached}``) so a limited model quotes it verbatim and a frontier model chains
it. Failures come back as the §6 degradation shape (``{ok:false, reason, hint}``), never a
traceback — CI passes with no external keys/binaries.

``fit_calc`` and ``recommend_fit`` provide the ISO 286 fit calculator.
These return the ``aicad.calc/1`` envelope (``contracts/advisor.md`` §7), not the §1
research envelope — different schema, same "code does the maths" discipline.
Call ``fit_calc`` BEFORE ``hole_sizing`` / bore-diameter decisions to get the ISO-table
band; use ``recommend_fit`` first when the fit class itself is not yet decided.

``research_eng_calc`` adds registry routing (beam, bolted_joint, thread_engagement,
press_fit, wall_thickness, snap_fit) and a ``list_calcs`` discovery tool. The original
``research_eng_calc`` (axial_stress, hoop_stress, …) is preserved and extended:
calc_ids not found in the new registry fall through to the legacy formulas.

Input dialect
~~~~~~~~~~~~~
``fit_calc`` / ``recommend_fit`` / ``research_material_lookup`` / ``research_eng_calc``
own their own input dialect: each takes a var-keyword parameter, so the agent-turn
reconciler hands the raw call through untouched and the TOOL decides what the model
meant.  Every rewrite is echoed on the response as ``interpreted: [...]`` (the
``cad_doc_tools`` convention) — genuinely ambiguous input gets a teaching error naming
the accepted forms, never a silent guess.  A canonical call is passed through byte
for byte: no notes means no added keys.
"""
from __future__ import annotations

import difflib
import re
from typing import Annotated, Any, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import Field

# The unit-literal parser ("20mm", "0.75in", 5'6") already exists ONCE, in cad_doc_tools,
# and this module already imports the cad_tools facade that loads it — so importing it
# here adds no dependency edge and no import cost.  A second copy (or a new shared
# module) would be a second thing to keep correct.
from services.mcp.cad_doc_tools import _dim_mm, _interpreted_notes
from services.mcp.cad_tools import _dumps, _err
from services import research
import services.research.calcs as _calcs  # registers all registry calculators
from services.research import fits as _fits
from services.research import machining_reference as _machining_ref
from services.research.material_lookup import _PROP_ALIASES, _PROP_UNITS
from services.research.calcs.registry import calc_envelope as _calc_envelope
# Read-only introspection of the brain this module adapts; neither has a public
# accessor and duplicating either would be a second source of truth.
from services.research.eng_calc import _FORMULAS as _LEGACY_FORMULAS
from services.research.eng_calc import formulas as _legacy_formulas

# ---------------------------------------------------------------------------
# Var-keyword annotation.
#
# FastMCP's func_metadata does NOT special-case inspect.Parameter.VAR_KEYWORD: a bare
# ``**kwargs`` becomes a REQUIRED pydantic field literally named "kwargs", which breaks
# every FastMCP call including the canonical one (registration still succeeds, so it
# looks fine until an external MCP client calls it).  Annotating it with a Field default
# makes that field optional; the built-in agent path (_bind_tool -> tool.fn) still sees
# var_kw=True and passes the real call through untouched.  FastMCP then hands the body
# ``{"kwargs": None}``, which _extras() strips.
_Extra = Annotated[Any, Field(default=None)]


def _extras(supplied: dict) -> dict:
    """Flatten a var-keyword payload: strip FastMCP's literal ``kwargs`` field and
    merge any dict a model nested under ``kwargs``/``extra``/``args``."""
    out = dict(supplied)
    for name in ("kwargs", "extra", "extras", "kw"):
        blob = out.pop(name, None)
        if isinstance(blob, dict):
            for k, v in blob.items():
                out.setdefault(k, v)
    return out


def _pick(src: dict, keys: tuple) -> tuple:
    """Pop the first present (case-insensitive) key from *src*. -> (key, value)."""
    lower = {k.lower(): k for k in src}
    for want in keys:
        real = lower.get(want)
        if real is not None and src.get(real) is not None:
            return real, src.pop(real)
    return None, None


# ---------------------------------------------------------------------------
# fit_calc input dialect — spellings the field ledgers actually show.
_NOMINAL_KEYS = ("nominal", "nominal_mm", "nominal_diameter", "nominal_size",
                 "basic_size", "basic_dimension", "diameter", "dia", "d", "size",
                 "dimension")
_SHAFT_KEYS = ("shaft", "shaft_diameter", "shaft_dia", "shaft_size", "shaft_mm",
               "shd", "pin", "pin_diameter", "pin_dia", "male", "male_diameter")
_HOLE_KEYS = ("hole", "hole_diameter", "hole_dia", "hole_size", "hole_mm",
              "bore", "bore_diameter", "bore_dia", "bore_size",
              "female", "female_diameter")
# `fit` first so an explicit designation always beats a stray `tolerance`.
_FIT_KEYS = ("fit", "fit_class", "fit_type", "iso_fit", "fit_designation",
             "designation", "tolerance_class", "tolerance", "class", "band", "grade")
_SHAFT_TOL_KEYS = ("shaft_tolerance", "shaft_tol", "shaft_class", "shaft_fit",
                   "shaft_band", "shaft_grade", "pin_tolerance")
_HOLE_TOL_KEYS = ("hole_tolerance", "hole_tol", "hole_class", "hole_fit", "hole_grade",
                  "bore_tolerance", "bore_tol", "bore_class", "bore_band")
_LIMIT_KEYS = ("clearance_limit", "max_clearance", "clearance_max", "clearance_budget",
               "clearance_target", "target_clearance")
_OUTPUT_KEYS = ("output", "outputs", "fields", "field", "select", "returns")

# Every ISO 286 fundamental-deviation letter, so "d20" (Ø20 shorthand) is not mistaken
# for a d-shaft at grade 20 — the grade bound does that.
_ISO_LETTERS = frozenset(
    "a b c cd d e ef f fg g h js j k m n p r s t u v x y z za zb zc".split())
_CLASS_RE = re.compile(r"^([A-Za-z]{1,2})(\d{1,2})$")
_PAIR_SPLIT_RE = re.compile(r"[/\\|]|\s+")
# Ø / dia / D prefix on a bare number: the CAD shorthand a model writes ("d20", "Ø6.35").
_DIA_PREFIX_RE = re.compile(r"^(?:dia\.?|d|\u00d8|\u00f8|\u2300|\u03c6|\u03d5)\s*(?=[\d.])",
                            re.IGNORECASE)
# "±0.05", "+/-0.05", "+0.021/-0", "+0.021/0"
_SYM_TOL_RE = re.compile(r"^(?:\u00b1|\+/-|\+-)\s*([\d.]+)$")
_PAIR_TOL_RE = re.compile(r"^([+-]?[\d.]+)\s*/\s*([+-]?[\d.]+)$")


def _parse_class(text: Any) -> Optional[list]:
    """``"H7/g6"`` -> ``[("H", 7), ("g", 6)]``; ``"h7"`` -> ``[("h", 7)]``; else None.

    Accepts ``/``, ``\\``, ``-`` and whitespace as the hole/shaft separator.  A letter
    outside the ISO 286 set, or a grade above IT18, is not a fit class (that is how
    ``"d20"`` stays a Ø20 diameter rather than becoming a d-shaft at grade 20).
    """
    if not isinstance(text, str):
        return None
    s = text.strip().replace("-", "/")
    if not s:
        return None
    parts = [p for p in _PAIR_SPLIT_RE.split(s) if p]
    if not parts or len(parts) > 2:
        return None
    comps = []
    for p in parts:
        m = _CLASS_RE.match(p)
        if m is None:
            return None
        letter, grade = m.group(1), int(m.group(2))
        if letter.lower() not in _ISO_LETTERS or grade > 18:
            return None
        comps.append((letter, grade))
    return comps


def _class_str(comps: list, side: Optional[str] = None) -> str:
    """Render components as an ISO designation, fixing case by role.

    A two-part designation is always ``HOLE/shaft``; a single part takes its case from
    *side* when the caller knows which one it is (``bore_tolerance="h7"`` is an H7 bore).
    """
    if len(comps) == 2:
        return f"{comps[0][0].upper()}{comps[0][1]}/{comps[1][0].lower()}{comps[1][1]}"
    letter, grade = comps[0]
    if side == "hole":
        letter = letter.upper()
    elif side == "shaft":
        letter = letter.lower()
    return f"{letter}{grade}"


def _size_mm(value: Any) -> Any:
    """A diameter in mm from a number, a numeric string, a unit literal, or ``"d20"``.

    An ``int`` is passed through as an ``int`` so the canonical ``fit_calc(nominal=20,
    fit="H7/g6")`` echoes ``nominal: 20`` exactly as it always has."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return None
    s = _DIA_PREFIX_RE.sub("", value.strip())
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    return _dim_mm(s)


def _as_size_or_class(value: Any) -> tuple:
    """A `hole=`/`shaft=`/`diameter=` value -> ``("size", mm)`` or ``("class", comps)``."""
    mm = _size_mm(value)
    if mm is not None:
        return "size", mm
    comps = _parse_class(value)
    if comps is not None:
        return "class", comps
    return "unparsed", value


def _resolve_designation(raw: Any, notes: list, key: str) -> tuple:
    """A `fit=`/`fit_type=`/`tolerance=` value -> ``(kind, payload)``.

    ``("class", comps)`` for an ISO designation, ``("number", float)`` for a bare
    number (a clearance budget or a ± band, depending on where it lands),
    ``("function", "H7/g6")`` for a functional keyword resolved through
    ``recommend_fit``, or ``("unparsed", raw)``.
    """
    comps = _parse_class(raw)
    if comps is not None:
        return "class", comps
    num = _size_mm(raw)
    if num is not None:
        return "number", num
    if not isinstance(raw, str) or not raw.strip():
        return "unparsed", raw
    rec = research.recommend_fit(raw.replace("_", " ").replace("-", " "))
    if rec.get("ok") and rec.get("answer"):
        classes = (rec["answer"][0].get("recommended_classes") or [])
        if classes:
            resolved = classes[0]
            notes.append(
                f"read {key}={raw!r} as a functional description, not an ISO class: "
                f"recommend_fit picked {resolved!r} ({rec['answer'][0].get('character')})")
            return "function", _parse_class(resolved)
    return "unparsed", raw


def _band(nominal: float, grade, letter: str, basis: str) -> dict:
    """``research.tolerance_band`` that degrades instead of raising.

    The brain raises ``FitError`` for an out-of-range nominal or an IT grade outside
    IT5–IT12; only its own ``fit_calc`` catches that, so every call from here goes
    through this wrapper and gets the §6 shape back."""
    try:
        return research.tolerance_band(nominal, str(grade), letter, basis=basis)
    except _fits.FitError as e:
        return {"ok": False, "reason": e.reason, "hint": e.hint}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "error", "hint": str(e)}


def _tolerance_band(spec: Any, nominal: float, side: str, notes: list,
                    key: str) -> tuple:
    """A per-side tolerance -> ``(lower_mm, upper_mm)`` offsets from *nominal*.

    Accepts an ISO class (``"h7"``, case-corrected for the side), a bare number
    (``±t``), ``"±0.05"`` / ``"+0.02/-0.01"``, or ``[lo, hi]``.  Anything else is
    dropped WITH a note naming the accepted forms — never silently.
    """
    if spec is None:
        return 0.0, 0.0
    if isinstance(spec, (list, tuple)) and len(spec) == 2:
        try:
            lo, hi = float(spec[0]), float(spec[1])
        except (TypeError, ValueError):
            lo = hi = None
        if lo is not None:
            return (min(lo, hi), max(lo, hi))
    if isinstance(spec, str):
        m = _SYM_TOL_RE.match(spec.strip())
        if m:
            t = abs(float(m.group(1)))
            notes.append(f"read {key}={spec!r} as ±{t} mm on the {side}")
            return -t, t
        m = _PAIR_TOL_RE.match(spec.strip())
        if m and _parse_class(spec) is None:
            lo, hi = float(m.group(1)), float(m.group(2))
            notes.append(f"read {key}={spec!r} as {min(lo, hi)}..{max(lo, hi)} mm "
                         f"on the {side}")
            return min(lo, hi), max(lo, hi)
    comps = _parse_class(spec)
    if comps is not None and len(comps) == 1:
        letter, grade = comps[0]
        want = letter.upper() if side == "hole" else letter.lower()
        if want != letter:
            notes.append(f"read {key}={spec!r} as {want}{grade} — the {side} side of an "
                         f"ISO 286 pair is spelled "
                         f"{'upper' if side == 'hole' else 'lower'}case")
        band = _band(nominal, grade, want, side)
        if band.get("ok"):
            return band["answer"]["lower_mm"], band["answer"]["upper_mm"]
        notes.append(f"{key}={spec!r} could not be applied ({band.get('hint')}) — the "
                     f"{side} was taken as exactly {nominal} mm")
        return 0.0, 0.0
    if isinstance(spec, bool):
        num = None
    elif isinstance(spec, (int, float)):
        num = float(spec)
    elif isinstance(spec, str):
        try:
            num = float(spec.strip())
        except ValueError:
            num = None
    else:
        num = None
    if num is not None:
        t = abs(num)
        notes.append(f"read {key}={spec!r} as ±{t} mm on the {side}")
        return -t, t
    notes.append(
        f"{key}={spec!r} is not an ISO 286 class, a number or a ±band — ignored; the "
        f"{side} was taken as exactly {nominal} mm. Use e.g. "
        f"{'H7' if side == 'hole' else 'g6'}, 0.02 (for ±0.02 mm), or [-0.02, 0.007].")
    return 0.0, 0.0


def _infer_nominal(hole_mm: float, shaft_mm: float) -> float:
    """The basic size behind a measured pair: the coarsest rounding of the mean that
    still holds BOTH sizes inside one IT12 band (20.02/20.00 -> 20)."""
    mean = (hole_mm + shaft_mm) / 2.0
    for places in (0, 1, 2, 3):
        cand = round(mean, places)
        if cand <= 0:
            continue
        it12 = _band(cand, 12, "H", "hole")
        if not it12.get("ok"):
            continue
        tol = it12["answer"]["upper_mm"]
        if abs(hole_mm - cand) <= tol and abs(shaft_mm - cand) <= tol:
            return float(cand)
    return round(mean, 6)


_CANDIDATE_FITS: Optional[list] = None


def _candidate_fits() -> list:
    """The repo's curated preferred-fit CLASS NAMES (data/fit_recommendations.json).

    Names only — every tolerance number still comes from ``research.tolerance_band``,
    so there is exactly one ISO 286 table in the tree."""
    global _CANDIDATE_FITS
    if _CANDIDATE_FITS is None:
        seen: list = []
        for entry in _fits._fit_recs():
            for cls in entry.get("recommended_classes") or []:
                if cls not in seen:
                    seen.append(cls)
        _CANDIDATE_FITS = seen
    return _CANDIDATE_FITS


def _closest_iso_fits(nominal: float, actual_lo: float, actual_hi: float,
                      limit: int = 3) -> list:
    """Standard ISO 286 fits at *nominal* ranked by how well their clearance range
    matches the measured one.

    Primary key: does the standard band CONTAIN the whole measured interval
    (``covers_measured``, ``outside`` = how far it pokes out).  Secondary: how centred
    the measurement sits in the band, normalised by band width — that is what makes a
    20.020 bore on a 20.000 shaft come back as H7/h6 rather than the narrower H6/h5
    it only just squeaks into.
    """
    mid_actual = (actual_lo + actual_hi) / 2.0
    ranked = []
    for cls in _candidate_fits():
        try:
            env = research.fit_calc(nominal, cls)
        except Exception:  # noqa: BLE001 — an unsupported class is simply not a candidate
            continue
        clr = (env.get("answer") or {}).get("clearance") if env.get("ok") else None
        if not clr:
            continue
        c_lo, c_hi = float(clr[0]), float(clr[1])
        outside = max(0.0, c_lo - actual_lo) + max(0.0, actual_hi - c_hi)
        width = max(c_hi - c_lo, 1e-9)
        offset = abs(mid_actual - (c_lo + c_hi) / 2.0) / width
        slack = abs(actual_lo - c_lo) + abs(actual_hi - c_hi)
        ranked.append((round(outside, 9), round(offset, 6), round(slack, 9), cls,
                       [c_lo, c_hi], env["answer"].get("fit_character")))
    ranked.sort(key=lambda r: (r[0], r[1], r[2]))
    return [{"fit": cls, "clearance_mm": rng, "fit_character": character,
             "covers_measured": outside == 0.0, "deviation_mm": round(slack, 6)}
            for outside, _offset, slack, cls, rng, character in ranked[:limit]]


def _character(min_clr: float, max_clr: float) -> str:
    if min_clr >= 0.0:
        return "clearance"
    if max_clr <= 0.0:
        return "press"
    return "transition"


def _mirror_hole_band(nominal: float, letter: str, grade: int) -> Optional[tuple]:
    """Band for an uppercase hole letter the tables do not carry directly.

    ISO 286 defines the shaft-basis system as the mirror of the hole-basis one, and the
    Δ-correction (§5.3) exists precisely so that ``P7/h6`` is the SAME fit as ``H7/p6``.
    That makes ``EI = -es(lowercase letter)``, ``ES = EI + IT`` exact for both the
    general (a–h) and the special (k–s) letter groups, using only the tables already
    here.  Returns ``(lower_mm, upper_mm)`` offsets, or None when the mirror shaft is
    itself unsupported.
    """
    shaft = _band(nominal, grade, letter.lower(), "shaft")
    it = _band(nominal, grade, "H", "hole")
    if not shaft.get("ok") or not it.get("ok"):
        return None
    ei = -shaft["answer"]["upper_mm"]
    return round(ei, 6), round(ei + it["answer"]["upper_mm"], 6)


def _assemble(calc: str, nominal: float, hole_band, shaft_band, equation: str,
              inputs: dict, notes: list, prefilled, citation: str,
              source_kind: str = "table", extra_answer: Optional[dict] = None) -> dict:
    """One aicad.calc/1 envelope in the exact shape ``research.fit_calc`` returns."""
    clearance = character = None
    if hole_band is not None and shaft_band is not None:
        min_clr = round(hole_band[0] - shaft_band[1], 6)
        max_clr = round(hole_band[1] - shaft_band[0], 6)
        clearance = [min_clr, max_clr]
        character = _character(min_clr, max_clr)
    answer = {
        "hole": list(hole_band) if hole_band is not None else None,
        "shaft": list(shaft_band) if shaft_band is not None else None,
        "clearance": clearance,
        "fit_character": character,
        "hole_absolute": ([round(nominal + hole_band[0], 6),
                           round(nominal + hole_band[1], 6)]
                          if hole_band is not None else None),
        "shaft_absolute": ([round(nominal + shaft_band[0], 6),
                            round(nominal + shaft_band[1], 6)]
                           if shaft_band is not None else None),
    }
    if extra_answer:
        answer.update(extra_answer)
    return _calc_envelope(calc=calc, answer=answer, units="mm", equation=equation,
                          inputs=inputs, source_kind=source_kind, citation=citation,
                          notes=notes, prefilled=prefilled)


_OUTPUT_FIELDS = {
    "bore_size": "hole_absolute", "hole_size": "hole_absolute",
    "bore_diameter": "hole_absolute", "hole_diameter": "hole_absolute",
    "bore": "hole_absolute", "hole": "hole_absolute",
    "hole_absolute": "hole_absolute",
    "shaft_size": "shaft_absolute", "shaft_diameter": "shaft_absolute",
    "shaft": "shaft_absolute", "shaft_absolute": "shaft_absolute",
    "clearance": "clearance", "interference": "clearance",
    "fit": "fit_character", "fit_character": "fit_character",
    "character": "fit_character",
    "hole_band": "hole", "hole_deviation": "hole",
    "shaft_band": "shaft", "shaft_deviation": "shaft",
}


def _apply_output(answer: dict, spec: Any, notes: list) -> None:
    """Honour ``output="bore_size,clearance"`` by projecting those fields, and say what
    the envelope already carries so the next call does not need the parameter."""
    if isinstance(spec, str):
        wanted = [w.strip() for w in re.split(r"[,;|]| and ", spec) if w.strip()]
    elif isinstance(spec, (list, tuple)):
        wanted = [str(w).strip() for w in spec if str(w).strip()]
    else:
        return
    picked, unknown = {}, []
    for name in wanted:
        key = _OUTPUT_FIELDS.get(name.lower().replace(" ", "_"))
        if key is None:
            unknown.append(name)
        else:
            picked[name] = answer.get(key)
    if picked:
        answer["requested"] = picked
        notes.append(f"output={spec!r} -> answer.requested; the envelope already carries "
                     f"hole, shaft, clearance, fit_character, hole_absolute and "
                     f"shaft_absolute, so output= is never required")
    if unknown:
        notes.append(f"output field(s) {unknown} are not part of this envelope; "
                     f"available: {sorted(set(_OUTPUT_FIELDS))}")


def _ambiguous_fit(supplied: dict) -> dict:
    """The one genuinely under-determined call: a single size and nothing to fit it to."""
    return {
        "ok": False,
        "schema": "aicad.calc/1",
        "reason": "ambiguous_fit_input",
        "hint": (
            "fit_calc needs to know WHICH fit, and one diameter alone does not say. "
            "Two modes: "
            "(1) DESIGN — a nominal size plus an ISO 286 class or a functional "
            "description, e.g. fit_calc(nominal=20, fit='H7/g6') or "
            "fit_calc(diameter=20, fit='sliding'); "
            "(2) MEASURED — the two real sizes, e.g. "
            "fit_calc(hole_diameter=20.02, shaft_diameter=20.0), optionally with "
            "hole_tolerance='H7' / shaft_tolerance=0.01. "
            "Use recommend_fit('sliding') first if the class is undecided."),
        "modes": {
            "design": "fit_calc(nominal=20, fit='H7/g6')",
            "measured": "fit_calc(hole_diameter=20.02, shaft_diameter=20.0)",
        },
        "received": {k: v for k, v in supplied.items() if k != "kwargs"},
    }


def _echo_size(notes: list, key: str, raw: Any, mm: float, canonical: str) -> None:
    """Echo a renamed key and/or a unit conversion; silent for a canonical number."""
    if isinstance(raw, str) and key != canonical:
        notes.append(f"interpreted {key}={raw!r} as {canonical}={mm} mm")
    elif isinstance(raw, str):
        notes.append(f"read {key}={raw!r} as {mm} mm")
    elif key != canonical:
        notes.append(f"interpreted {key!r} as {canonical!r}")


def has_fit_inputs(supplied: dict) -> bool:
    """True when a call carries enough for :func:`fit_from_kwargs` to try a pair.

    Used by ``intake_tools.fit_report`` to tell an ad-hoc pair from a document report.
    """
    args = _extras(supplied)
    lower = {k.lower() for k in args if args.get(k) is not None}
    return bool(lower & set(_SHAFT_KEYS + _HOLE_KEYS + _NOMINAL_KEYS + _FIT_KEYS
                            + _SHAFT_TOL_KEYS + _HOLE_TOL_KEYS))


def fit_from_kwargs(supplied: dict, prefilled: Optional[dict] = None,
                    calc: str = "fit_calc") -> dict:
    """The whole ``fit_calc`` input dialect -> an aicad.calc/1 envelope (or §6 fail).

    Shared with ``intake_tools.fit_report`` so an ad-hoc pair there is computed by
    exactly this code.  See the ``fit_calc`` docstring for the accepted forms.
    """
    args = _extras(supplied)
    args.pop("prefilled", None)
    notes: list = []

    nominal_key, nominal_raw = _pick(args, _NOMINAL_KEYS)
    shaft_key, shaft_raw = _pick(args, _SHAFT_KEYS)
    hole_key, hole_raw = _pick(args, _HOLE_KEYS)
    fit_key, fit_raw = _pick(args, _FIT_KEYS)
    stol_key, stol_raw = _pick(args, _SHAFT_TOL_KEYS)
    htol_key, htol_raw = _pick(args, _HOLE_TOL_KEYS)
    _, limit_raw = _pick(args, _LIMIT_KEYS)
    _, output_raw = _pick(args, _OUTPUT_KEYS)
    # A second fit-ish key left over (fit_type='sliding' AND tolerance='h7').
    second_key, second_raw = _pick(args, _FIT_KEYS)

    nominal = None
    if nominal_raw is not None:
        nominal = _size_mm(nominal_raw)
        if nominal is None:
            notes.append(f"could not read {nominal_key}={nominal_raw!r} as a size "
                         f"— ignored")
        else:
            _echo_size(notes, nominal_key, nominal_raw, nominal, "nominal")

    # hole=/shaft= carry either a size or a class ("hole":"H7" is a real ledger dialect).
    hole_size = shaft_size = None
    if hole_raw is not None:
        kind, payload = _as_size_or_class(hole_raw)
        if kind == "size":
            hole_size = payload
            _echo_size(notes, hole_key, hole_raw, payload, "hole_diameter")
        elif kind == "class" and htol_raw is None:
            htol_key, htol_raw = hole_key, hole_raw
        else:
            notes.append(f"could not read {hole_key}={hole_raw!r} as a diameter or an "
                         f"ISO class — ignored")
    if shaft_raw is not None:
        kind, payload = _as_size_or_class(shaft_raw)
        if kind == "size":
            shaft_size = payload
            _echo_size(notes, shaft_key, shaft_raw, payload, "shaft_diameter")
        elif kind == "class" and stol_raw is None:
            stol_key, stol_raw = shaft_key, shaft_raw
        else:
            notes.append(f"could not read {shaft_key}={shaft_raw!r} as a diameter or an "
                         f"ISO class — ignored")

    measured = hole_size is not None and shaft_size is not None

    # ---- the fit designation (design mode) -------------------------------------
    designation = None
    budget = _size_mm(limit_raw) if limit_raw is not None else None
    if fit_raw is not None:
        kind, payload = _resolve_designation(fit_raw, notes, fit_key)
        if kind in ("class", "function"):
            if measured:
                notes.append(
                    f"{fit_key}={fit_raw!r} names a fit class, but both real sizes were "
                    f"given — reporting the ACTUAL fit plus the standard classes closest "
                    f"to it (answer.closest_iso_fits)")
            else:
                designation = payload
                if kind == "class" and fit_key != "fit":
                    notes.append(f"interpreted {fit_key!r} as 'fit' "
                                 f"({_class_str(payload)})")
        elif kind == "number" and budget is None:
            budget = payload
            notes.append(
                f"read {fit_key}={fit_raw!r} as a clearance budget in mm and checked the "
                f"computed fit against it — pass shaft_tolerance=/hole_tolerance= for a "
                f"size tolerance, or fit='H7/g6' for an ISO class")
        elif kind == "unparsed":
            notes.append(f"{fit_key}={fit_raw!r} is neither an ISO 286 class nor a fit "
                         f"keyword recommend_fit knows — ignored")
    if second_raw is not None:
        kind, payload = _resolve_designation(second_raw, notes, second_key)
        if kind == "class" and payload is not None and len(payload) == 1:
            letter = payload[0][0]
            if letter.islower() and stol_raw is None:
                stol_key, stol_raw = second_key, second_raw
            elif letter.isupper() and htol_raw is None:
                htol_key, htol_raw = second_key, second_raw
        elif kind == "number" and budget is None:
            budget = payload
            notes.append(f"read {second_key}={second_raw!r} as a clearance budget in mm")

    if not measured:
        # An explicit per-side class refines a designation that came from a keyword.
        if designation is not None and len(designation) == 2:
            comps = _parse_class(stol_raw) if stol_raw is not None else None
            if comps is not None and len(comps) == 1:
                designation = [designation[0], (comps[0][0].lower(), comps[0][1])]
                notes.append(f"{stol_key}={stol_raw!r} overrides the shaft side -> "
                             f"{_class_str(designation)}")
                stol_raw = None
            comps = _parse_class(htol_raw) if htol_raw is not None else None
            if comps is not None and len(comps) == 1:
                designation = [(comps[0][0].upper(), comps[0][1]), designation[1]]
                notes.append(f"{htol_key}={htol_raw!r} overrides the hole side -> "
                             f"{_class_str(designation)}")
                htol_raw = None
        if designation is None:
            # One or two lone per-side classes ARE the designation
            # (`hole="H7", shaft="h7"` is a real ledger dialect).
            hcomps = _parse_class(htol_raw) if htol_raw is not None else None
            scomps = _parse_class(stol_raw) if stol_raw is not None else None
            hcomps = hcomps if hcomps and len(hcomps) == 1 else None
            scomps = scomps if scomps and len(scomps) == 1 else None
            if hcomps and scomps:
                designation = [(hcomps[0][0].upper(), hcomps[0][1]),
                               (scomps[0][0].lower(), scomps[0][1])]
                notes.append(f"read {htol_key}={htol_raw!r} + {stol_key}={stol_raw!r} as "
                             f"the fit {_class_str(designation)}")
                htol_raw = stol_raw = None
            elif hcomps or scomps:
                comps, side, key, raw = ((hcomps, "hole", htol_key, htol_raw) if hcomps
                                         else (scomps, "shaft", stol_key, stol_raw))
                designation = [(comps[0][0].upper() if side == "hole"
                                else comps[0][0].lower(), comps[0][1])]
                notes.append(f"read {key}={raw!r} as the ISO class to compute the "
                             f"{side} band for")
                if side == "hole":
                    htol_raw = None
                else:
                    stol_raw = None

    if measured:
        return _measured_mode(hole_size, shaft_size, htol_raw, htol_key, stol_raw,
                              stol_key, nominal, budget, output_raw, notes, prefilled,
                              calc)

    if designation is not None:
        size = nominal
        if size is None:
            size = hole_size if hole_size is not None else shaft_size
        if size is None:
            return _ambiguous_fit(supplied)
        # A lone LOWERCASE class is a shaft class — but if the only diameter in the
        # call is a bore there is no shaft for it to describe, so it is the bore's
        # class miscased. The reverse (an uppercase class beside a shaft diameter) is
        # a legitimate "the hole this shaft goes into" and is left alone.
        if (len(designation) == 1 and designation[0][0].islower()
                and hole_size is not None and shaft_size is None):
            notes.append(
                f"read {_class_str(designation)} as the BORE's class "
                f"{_class_str(designation, side='hole')} — the only diameter given is a "
                f"bore and a lowercase class is a shaft class. Pass shaft_diameter= too, "
                f"or fit='H7/{_class_str(designation)}', if you meant a bore of "
                f"\u00d8{hole_size} mating a {_class_str(designation)} shaft")
            designation = [(designation[0][0].upper(), designation[0][1])]
        return _design_mode(size, designation, budget, output_raw, notes, prefilled, calc)

    return _ambiguous_fit(supplied)


def _finish(env: dict, answer_extra: Optional[dict], budget, output_raw,
            notes: list, mode: str) -> dict:
    """Common tail: clearance budget check, output projection, and the echo.

    A call that needed NO interpretation gets its envelope back untouched — the
    canonical ``fit_calc(nominal=20, fit='H7/g6')`` response is byte-identical to the
    one this tool has always returned.
    """
    if not env.get("ok"):
        return env
    answer = env.get("answer") or {}
    if answer_extra:
        answer.update(answer_extra)
    if budget is not None:
        clr = answer.get("clearance")
        if clr:
            answer["limit_check"] = {
                "limit_mm": budget,
                "min_clearance_mm": clr[0],
                "max_clearance_mm": clr[1],
                "within_limit": bool(clr[1] <= budget + 1e-9),
            }
        else:
            notes.append("a clearance budget needs BOTH sides — give a hole/shaft pair "
                         "(e.g. fit='H7/g6') and it will be checked")
    if output_raw is not None:
        _apply_output(answer, output_raw, notes)
    if notes:
        env["mode"] = mode
        env["interpreted"] = _interpreted_notes(notes)
    return env


def _design_mode(nominal: float, designation: list, budget, output_raw, notes: list,
                 prefilled, calc: str) -> dict:
    """Nominal + ISO class -> the tolerance bands (today's behaviour, widened input)."""
    fit = _class_str(designation)
    env = research.fit_calc(nominal, fit, prefilled=prefilled)
    if not env.get("ok") and env.get("reason") == "unsupported_hole":
        env = _shaft_basis_mirror(nominal, designation, notes, prefilled, calc)
    return _finish(env, None, budget, output_raw, notes, "design")


def _shaft_basis_mirror(nominal: float, designation: list, notes: list, prefilled,
                        calc: str) -> dict:
    """``G7/h7`` and friends: compute the shaft-basis fit through its hole-basis twin."""
    hole_letter, hole_grade = designation[0]
    hole_band = _mirror_hole_band(nominal, hole_letter, hole_grade)
    if hole_band is None:
        return {"ok": False, "schema": "aicad.calc/1", "reason": "unsupported_hole",
                "hint": (f"hole deviation {hole_letter!r} is not derivable from the "
                         f"bundled ISO 286 tables (supported letters: "
                         f"H, JS, and any of D E F G K N P R S via the shaft-basis "
                         f"mirror). The hole-basis equivalent always works: "
                         f"fit_calc({nominal}, 'H{hole_grade}/"
                         f"{hole_letter.lower()}{hole_grade}').")}
    shaft_band = None
    if len(designation) == 2:
        sb = research.tolerance_band(nominal, str(designation[1][1]),
                                     designation[1][0].lower(), basis="shaft")
        if not sb.get("ok"):
            return sb
        shaft_band = (sb["answer"]["lower_mm"], sb["answer"]["upper_mm"])
    fit = _class_str(designation)
    twin = (f"H{hole_grade}/{hole_letter.lower()}"
            f"{designation[1][1] if len(designation) == 2 else hole_grade}")
    notes.append(
        f"{fit} is a SHAFT-BASIS fit; the bundled tables carry hole H/JS only, so the "
        f"{hole_letter}{hole_grade} band is derived by the ISO 286 mirror rule (§5.3) — "
        f"{fit} is by definition the same fit as {twin}")
    return _assemble(
        calc=calc, nominal=nominal, hole_band=hole_band, shaft_band=shaft_band,
        equation=f"ISO 286-1 shaft-basis mirror of {twin}, nominal={nominal}mm, {fit}",
        inputs={"nominal": nominal, "fit": fit, "hole_basis_equivalent": twin},
        notes=[], prefilled=prefilled, citation="ISO 286-1:2010 §5.3; ISO 286-2:2010",
        source_kind="closed_form")


def _measured_mode(hole_mm: float, shaft_mm: float, htol, htol_key, stol, stol_key,
                   nominal, budget, output_raw, notes: list, prefilled,
                   calc: str) -> dict:
    """Two real sizes -> the ACTUAL fit, its character, and the nearest standard class."""
    if nominal is None:
        nominal = _infer_nominal(hole_mm, shaft_mm)
        notes.append(f"measured mode: hole \u00d8{hole_mm} and shaft \u00d8{shaft_mm} -> "
                     f"basic size {nominal} mm (pass nominal= to override)")
    else:
        notes.append(f"measured mode: actual fit of hole \u00d8{hole_mm} on shaft "
                     f"\u00d8{shaft_mm} at nominal {nominal} mm")
    hole_band_off = _tolerance_band(htol, hole_mm, "hole", notes, htol_key or "hole_tolerance")
    shaft_band_off = _tolerance_band(stol, shaft_mm, "shaft", notes,
                                     stol_key or "shaft_tolerance")
    hole_abs = [round(hole_mm + hole_band_off[0], 6), round(hole_mm + hole_band_off[1], 6)]
    shaft_abs = [round(shaft_mm + shaft_band_off[0], 6),
                 round(shaft_mm + shaft_band_off[1], 6)]
    min_clr = round(hole_abs[0] - shaft_abs[1], 6)
    max_clr = round(hole_abs[1] - shaft_abs[0], 6)
    character = _character(min_clr, max_clr)
    closest = _closest_iso_fits(nominal, min_clr, max_clr)
    body = [f"Actual {character} fit: {min_clr * 1000:.1f}..{max_clr * 1000:.1f} µm "
            f"(negative = interference)."]
    if closest:
        body.append("Closest standard ISO 286 fit at this size: "
                    + ", ".join(f"{c['fit']} ({c['clearance_mm'][0] * 1000:.1f}.."
                                f"{c['clearance_mm'][1] * 1000:.1f} µm)"
                                for c in closest))
    env = _calc_envelope(
        calc=calc,
        answer={
            "hole": list(hole_band_off), "shaft": list(shaft_band_off),
            "clearance": [min_clr, max_clr], "fit_character": character,
            "hole_absolute": hole_abs, "shaft_absolute": shaft_abs,
            "nominal_mm": nominal, "closest_iso_fits": closest,
        },
        units="mm",
        equation=(f"clearance = hole - shaft; hole {hole_abs[0]}..{hole_abs[1]}, "
                  f"shaft {shaft_abs[0]}..{shaft_abs[1]} mm"),
        inputs={"hole_mm": hole_mm, "shaft_mm": shaft_mm, "nominal_mm": nominal},
        source_kind="closed_form", citation="ISO 286-2:2010", notes=body,
        prefilled=prefilled)
    return _finish(env, None, budget, output_raw, notes, "measured")


# recommend_fit input dialect — what a model calls the "what is this joint for?" field.
_FUNCTION_KEYS = ("function", "fit_type", "application", "purpose", "use", "use_case",
                  "description", "intent", "query", "text", "kind", "type", "fit",
                  "requirement")

# ---------------------------------------------------------------------------
# research_material_lookup input dialect
_MATERIAL_KEYS = ("material", "material_name", "name", "alloy", "spec", "grade",
                  "query", "q", "text")
_PROPERTY_KEYS = ("property", "prop", "properties", "property_name", "quantity",
                  "attribute", "field")
# A model reaching for material SELECTION rather than a lookup.
_SELECTION_KEYS = ("application", "environment", "part_type", "use_case", "use",
                   "requirement", "requirements", "service", "duty", "purpose",
                   "function", "criteria")
_PROP_PHRASES: Optional[dict] = None


def _prop_phrases() -> dict:
    """Every property spelling the brain accepts, as a space-separated phrase."""
    global _PROP_PHRASES
    if _PROP_PHRASES is None:
        keys = set(_PROP_ALIASES) | set(_PROP_UNITS)
        _PROP_PHRASES = {k.replace("_", " ").replace("'", "").lower(): k for k in keys}
    return _PROP_PHRASES


def _split_material_query(text: str) -> Optional[tuple]:
    """``"6061 yield strength"`` -> ``("6061", "yield_strength")``.

    Only splits on an UNAMBIGUOUS trailing property phrase; anything else stays whole
    (the brain's own fuzzy alloy matching handles "aluminum", "6061", "6061-T6")."""
    words = re.findall(r"[^\s,;]+", str(text).strip())
    if len(words) < 2:
        return None
    phrases = _prop_phrases()
    for n in (3, 2, 1):
        if len(words) <= n:
            continue
        tail = " ".join(w.strip("().'\"").lower() for w in words[-n:])
        key = phrases.get(tail) or phrases.get(tail.replace(" ", "_"))
        if key:
            return " ".join(words[:-n]), key
    return None


# ---------------------------------------------------------------------------
# research_eng_calc input dialect
_FORMULA_KEYS = ("formula", "calc", "calc_id", "name", "formula_id", "id", "calculator")
_PARAMS_KEYS = ("params", "inputs", "values", "args", "arguments", "input", "data")
_ARITHMETIC = frozenset({
    "add", "addition", "sum", "plus", "subtract", "subtraction", "minus", "difference",
    "multiply", "multiplication", "product", "times", "divide", "division", "quotient",
    "average", "mean", "total", "count", "max", "min", "round", "sqrt",
})

# Textbook symbols for a canonical stem, guarded by the UNIT SUFFIX the model wrote.
# The unit is the whole safety mechanism: `P_N` is newtons, so it can only be the force
# input, while a bare `P` (point load? pressure?) is never guessed. Every entry below is
# (symbol, unit) -> canonical stem, and both sides carry the same physical quantity.
_CALC_PARAM_SYMBOLS: dict = {
    ("l", "mm"): "length",      # L = span/length in every mechanics text
    ("p", "n"): "force",        # P = point load, in newtons
    ("f", "n"): "force",        # F = force (the registry `beam` calc spells it F_n)
    ("m", "nmm"): "moment",     # M = bending moment
    ("a", "mm2"): "area",       # A = cross-sectional area
    ("t", "mm"): "thickness",   # t = wall thickness (T_nm is torque — different unit)
}

# b/h spellings that describe a rectangle, for the I_mm4 teaching error.
_RECT_B_KEYS = ("b_mm", "b", "base_mm", "width_mm")
_RECT_H_KEYS = ("h_mm", "h", "height_mm", "depth_mm")


def _symbol_param(want: str, supplied, wanted) -> Optional[str]:
    """The supplied key that is `want` written as its textbook symbol, or None.

    Only fires when the unit suffixes match exactly and the supplied key is not itself
    a canonical param of this formula (never steal `L_mm` from `beam`, which wants it).
    """
    if "_" not in want:
        return None
    wstem, wunit = want.rsplit("_", 1)
    for key in supplied:
        if "_" not in key or key in wanted:
            continue
        sym, unit = key.rsplit("_", 1)
        if unit.lower() != wunit.lower():
            continue
        if _CALC_PARAM_SYMBOLS.get((sym.lower(), unit.lower())) == wstem.lower():
            return key
    return None


def _rect_inertia_hint(params: dict, wanted) -> Optional[str]:
    """Teach I = b*h^3/12 instead of assuming the caller's section is a rectangle.

    `I_mm4` is a property of a section SHAPE. Computing it from b/h would silently pick
    the shape for the caller and hand back a confident deflection for a section they
    never described, so this returns words, never a value.
    """
    if "I_mm4" not in wanted or "I_mm4" in params:
        return None
    lower = {k.lower(): k for k in params}
    b_key = next((lower[k] for k in _RECT_B_KEYS if k in lower), None)
    h_key = next((lower[k] for k in _RECT_H_KEYS if k in lower), None)
    if b_key is None and h_key is None:
        return None
    hint = (
        f"I_mm4 is the second moment of area of the SECTION and cannot be derived from "
        f"{sorted(k for k in (b_key, h_key) if k)} without assuming a section shape — "
        f"none was assumed. For a solid rectangle of width b and height h (h measured in "
        f"the bending direction): I_mm4 = b*h**3/12.")
    try:
        if b_key is not None and h_key is not None:
            b = float(params[b_key])
            h = float(params[h_key])
            hint += (f" With b={b:g}, h={h:g} that is I_mm4 = {b * h ** 3 / 12.0:.6g}. "
                     f"Re-issue the call with I_mm4 if that is your section.")
    except (TypeError, ValueError):
        pass
    return hint + (" A different shape needs its own I: use the `beam` calculator, which "
                   "takes section/b_mm/h_mm (or d_mm) and computes I itself.")


def _all_calc_ids() -> list:
    return sorted(set(_calcs.list_calcs()) | set(_legacy_formulas()))


def _group_calcs() -> dict:
    """The live catalogue grouped so a model can pick without reading every id."""
    groups: dict = {}
    for cid in _all_calc_ids():
        if "stress" in cid:
            g = "stress"
        elif "deflection" in cid or cid == "beam":
            g = "beam & deflection"
        elif cid in ("bolted_joint", "thread_engagement", "press_fit", "snap_fit"):
            g = "joints & fasteners"
        elif "factor" in cid or "margin" in cid:
            g = "margins"
        elif "area" in cid or "thickness" in cid or "bend" in cid or "draft" in cid:
            g = "geometry & sizing"
        else:
            g = "other"
        groups.setdefault(g, []).append(cid)
    return {k: sorted(v) for k, v in sorted(groups.items())}


def _normalise_calc_id(name: str) -> str:
    s = str(name).strip().lower().replace("-", "_").replace(" ", "_")
    for suffix in ("_calc", "_calculator", "_formula", "_equation"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


def _resolve_formula(name: str, notes: list) -> Optional[str]:
    """Exact id, then a near-miss spelling — resolved out loud, never silently."""
    ids = _all_calc_ids()
    if name in ids:
        return name
    norm = _normalise_calc_id(name)
    if norm in ids:
        notes.append(f"resolved formula {name!r} to {norm!r}")
        return norm
    close = difflib.get_close_matches(norm, ids, n=1, cutoff=0.8)
    if close:
        notes.append(f"resolved formula {name!r} to the closest registered calculator "
                     f"{close[0]!r}")
        return close[0]
    return None


def _unknown_formula(name: str, notes: list) -> dict:
    """The teaching not_found: nearest ids, the grouped catalogue, and the arithmetic
    escape hatch a model keeps reaching for (`formula="add"`)."""
    ids = _all_calc_ids()
    norm = _normalise_calc_id(name)
    suggestions = difflib.get_close_matches(norm, ids, n=3, cutoff=0.4)
    out = {
        "ok": False,
        "reason": "not_found",
        "hint": (f"unknown formula {name!r}. Closest registered: "
                 f"{suggestions or 'none'}. Pick one from `known` (or call list_calcs() "
                 f"for the full input schemas)."),
        "closest": suggestions,
        "known": _group_calcs(),
    }
    if norm in _ARITHMETIC:
        out["hint"] = (
            f"research_eng_calc evaluates NAMED engineering formulas, not arithmetic — "
            f"there is no {name!r} calculator and there will not be one. Do plain "
            f"arithmetic yourself in the answer; use this tool only for the formulas in "
            f"`known` (e.g. research_eng_calc('circle_area', {{'diameter_mm': 20}})).")
    if notes:
        out["interpreted"] = _interpreted_notes(notes)
    return out


def _reconcile_calc_params(params: dict, wanted, formula: str, notes: list) -> dict:
    """Map the model's param spelling onto the formula's real names.

    Mostly structural, not a synonym list: every canonical name carries its unit as a
    suffix (``force_n``, ``area_mm2``, ``I_mm4``), matching is case-insensitive (``E_MPa``
    is ``E_mpa``), and a model writing the bare stem is understood. On top of that a small
    unit-guarded symbol table resolves the standard mechanics symbols (``L_mm`` ->
    ``length_mm``, ``P_N`` -> ``force_n``). Unit-suffixed string values ("20mm") are
    converted for ``*_mm`` params. A key that is already a canonical param of this formula
    is never re-pointed at a different one.
    """
    out = dict(params)
    for want in wanted:
        if want in out:
            continue
        lower = {k.lower(): k for k in out}
        stem = want.rsplit("_", 1)[0] if "_" in want else want
        src = lower.get(want.lower()) or lower.get(stem.lower())
        if src is None:
            src = _symbol_param(want, list(out), wanted)
        if src is None:
            close = difflib.get_close_matches(want.lower(), list(lower), n=1, cutoff=0.85)
            src = lower[close[0]] if close else None
        if src in wanted and src != want:
            src = None  # it is this formula's own param; leave it where the model put it
        if src is not None and src != want:
            out[want] = out.pop(src)
            notes.append(f"interpreted param {src!r} as {want!r}")
    # A circle is the one place where the natural input (radius) is not the param.
    if formula == "circle_area" and "diameter_mm" not in out:
        for key in ("radius_mm", "radius", "r"):
            if key in out:
                try:
                    out["diameter_mm"] = 2.0 * float(out.pop(key))
                except (TypeError, ValueError):
                    break
                notes.append(f"interpreted param {key!r} as a radius -> "
                             f"diameter_mm={out['diameter_mm']}")
                break
    for key, value in list(out.items()):
        if key.endswith("_mm") and isinstance(value, str):
            mm = _dim_mm(value)
            if mm is not None:
                out[key] = mm
                notes.append(f"converted {key}={value!r} to {mm} mm")
    return out


def register_research_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def research_web_search(query: str, provider: Optional[str] = None,
                            max_results: int = 5) -> str:
        """Search the web for up-to-date facts instead of recalling from memory.

        Returns ranked results plus (for providers that offer one) an LLM-ready `answer`,
        each with `source_url` for provenance. `provider` is optional: Tavily is used when
        `TAVILY_API_KEY` is set, else the keyless DuckDuckGo fallback (labeled
        `confidence:"limited"`). Offline mode returns `{ok:false, reason:"offline"}`.
        Use this to FIND sources, then `research_fetch_page` to READ one.
        """
        try:
            return _dumps(research.web_search(query, provider=provider,
                                              max_results=max_results))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def research_fetch_page(url: str, tier: str = "datasheet") -> str:
        """Fetch a page and return clean readability->markdown `content` with provenance.

        Caches per-user locally (keyed by URL) with tiered freshness — `tier` is one of
        `pricing` (hours), `datasheet` (days), `standard` (~immutable); pick by how fast
        the fact changes. `fetched_ts` and `cached` are always surfaced. Offline mode
        serves the cache only (`confidence:"offline"`) or `{ok:false, reason:"offline"}`
        on a miss. Quote the returned `content` verbatim for any factual claim.
        """
        try:
            return _dumps(research.fetch_page(url, tier=tier))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # --- Source helpers (same §1 answer envelope; bundled-first, provenance-stamped) ---

    @mcp.tool()
    def research_material_lookup(material: Optional[str] = None,
                                 property: Optional[str] = None,
                                 **kwargs: _Extra) -> str:
        """Look up material properties (yield/ultimate/modulus/density/...) with provenance.

        Serves the bundled unified material registry first (`confidence:"bundled"`,
        first-party handbook nominal values) — pass `property` (e.g. "yield", "density")
        for one SI-normalised quantity, or omit it for the full set. A material not in the
        registry falls through to cited web search when online (never fabricates a value,
        never touches MatWeb/MakeItFrom). Quote the returned value verbatim.

        `material` is also accepted as `query`, `name`, `material_name`, `alloy`, `spec`,
        `grade`, `q` or `text`; `property` as `prop`, `properties`, `property_name`,
        `quantity` or `attribute`. A single string carrying both is split on its trailing
        property phrase and the split is echoed in `interpreted`:
            research_material_lookup(query="6061 yield strength")
            → material="6061", property="yield_strength" → 276 MPa, bundled
        A string with no recognised property tail is passed through whole (the registry
        already resolves "6061", "6061-T6" and "aluminum").

        This is a LOOKUP for a NAMED material, not a material selector. A call that only
        describes an application (`application=`, `environment=`, `part_type=`, `use=`,
        `requirements=`) is served as a cited web search for candidates — and says so —
        because there is no bundled selection table to answer it from. The reliable
        two-step is: `research_web_search("bushing material for a sliding fit")` to get
        candidate alloys, then `research_material_lookup("<alloy>", "yield")` for each
        one's numbers.
        """
        try:
            kwargs.pop("kwargs", None)
            args = _extras(kwargs)
            notes: list = []
            mkey, mat = _pick(args, _MATERIAL_KEYS)
            if material is not None:
                mat = material
            elif mkey is not None:
                notes.append(f"interpreted {mkey!r} as 'material'")
            pkey, prop = _pick(args, _PROPERTY_KEYS)
            if property is not None:
                prop = property
            elif pkey is not None:
                notes.append(f"interpreted {pkey!r} as 'property'")

            selection = {k: v for k, v in args.items()
                         if k.lower() in _SELECTION_KEYS and v is not None}
            if (mat is None or not str(mat).strip()) and selection:
                mat = " ".join(str(v).replace("_", " ") for v in selection.values())
                notes.append(
                    f"no material named — composed the selection query {mat!r} from "
                    f"{sorted(selection)} and returned cited candidates. "
                    f"research_material_lookup answers with bundled numbers only for a "
                    f"NAMED material; use research_web_search for selection, then call "
                    f"this per candidate alloy")
            elif selection:
                notes.append(
                    f"{sorted(selection)} are not lookup filters and were ignored — this "
                    f"tool answers for the named material {str(mat)!r}. Use "
                    f"research_web_search to shortlist by application")
            if mat is None or not str(mat).strip():
                return _dumps({
                    "ok": False, "reason": "bad_query",
                    "hint": ("Provide a material name (e.g. '6061-T6', 'A36 steel'), "
                             "optionally with a property: "
                             "research_material_lookup('6061-T6', 'yield'). To choose a "
                             "material from an application, use research_web_search "
                             "first — this tool looks up a named one.")})

            if prop is None:
                split = _split_material_query(mat)
                if split is not None:
                    mat, prop = split
                    notes.append(f"split the query into material={mat!r} and "
                                 f"property={prop!r}")
            out = research.material_lookup(str(mat), property=str(prop) if prop else None)
            if notes and isinstance(out, dict):
                out["interpreted"] = _interpreted_notes(notes)
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def research_thread_spec(designation: str) -> str:
        """Screw-thread dimensions COMPUTED from ISO 724 / ASME B1.1 (never fetched).

        Accepts metric (`M8`, `M8x1.25`) or Unified-inch (`1/4-20 UNC`, `#10-24`)
        designations; returns pitch/minor diameters and the tensile stress area. Bundled
        (`confidence:"bundled"`) — the standard is cited in `quote`.
        """
        try:
            return _dumps(research.thread_spec(designation))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def research_standard_section(designation: str) -> str:
        """Structural section properties: AISC W-shapes (`W8x31`) and ASME B36.10M pipe
        (`pipe 2 sch 40`). Returns area / moment of inertia / section modulus from the
        bundled official dimensional tables (`confidence:"bundled"`).
        """
        try:
            return _dumps(research.standard_section(designation))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def research_unit_convert(value: Optional[Any] = None,
                              unit: Optional[str] = None,
                              family: Optional[str] = None,
                              from_unit: Optional[str] = None,
                              to_unit: Optional[str] = None,
                              batch: Optional[list] = None,
                              conversions: Optional[list] = None) -> str:
        """Convert units deterministically — code converts, not the model (the ksi/MPa
        6.9x error). Reach for this INSTEAD of doing the arithmetic in your head.

        Three call shapes:
        * `research_unit_convert(value=25, from_unit="ksi", to_unit="MPa")` — arbitrary
          conversion. The family is resolved FROM THE UNITS; do not state it.
        * `research_unit_convert(value=1, unit="in", family="length")` — canonical
          direction only (`length`->mm, `angle`->deg, `count`).
        * `research_unit_convert(batch=[{"value":1,"from_unit":"in","to_unit":"mm"}, ...])`
          — one call for many; each entry reports its own result.

        Families: `length`, `area`, `volume`, `angle`, `mass`, `force`, `pressure`
        (Pa/kPa/MPa/GPa/psi/ksi/bar/atm), `torque` (N*m/N*mm/lbf*in/lbf*ft),
        `temperature` (degC/degF/K — affine, handled correctly), `count`.
        A cross-family pair (`mm`->`kg`) is REFUSED naming both families, never coerced;
        an ambiguous case variant (`NM`: nanometre or newton-metre?) is refused too.
        """
        try:
            items = batch if batch is not None else conversions
            return _dumps(research.unit_convert(value, unit, family,
                                                from_unit=from_unit, to_unit=to_unit,
                                                batch=items))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def research_eng_calc(formula: Optional[str] = None, params: Optional[dict] = None,
                          raw_quote: Optional[str] = None,
                          prefilled: Optional[dict] = None,
                          **kwargs: _Extra) -> str:
        """Evaluate a named deterministic engineering formula in SI (code does the maths).

        **Calculator registry** (aicad.calc/1 envelope, contracts/advisor.md §7):
        `beam`, `bolted_joint`, `thread_engagement`, `press_fit`,
        `wall_thickness`, `snap_fit`.  Call `list_calcs` for the full registry
        with input schemas.

        **Legacy formulas** (fallback, §1 envelope):
        `axial_stress`, `bending_stress`, `hoop_stress`, `safety_factor`,
        `cantilever_end_deflection`, `circle_area`.

        `formula` is also accepted as `calc`, `calc_id`, `name`, `formula_id`, `id` or
        `calculator`; `params` as `inputs`, `values`, `args`, `arguments` or `data`.
        Loose parameters are folded into `params`, so
        `research_eng_calc("circle_area", diameter_mm=20)` works too.

        Param names are reconciled structurally: every canonical name carries its unit
        as a suffix, so the bare stem is accepted and echoed — `force`→`force_n`,
        `area`→`area_mm2`, `I`→`I_mm4`, `diameter`→`diameter_mm`. Matching ignores case,
        so `E_MPa` is `E_mpa`. The standard mechanics symbols resolve when the unit
        suffix agrees — `L_mm`→`length_mm`, `P_N`/`F_N`→`force_n`, `M_Nmm`→`moment_nmm`,
        `A_mm2`→`area_mm2`, `t_mm`→`thickness_mm` — while a bare symbol with no unit
        (`P` could be pressure or point load) is never guessed. `circle_area` also
        takes a radius (`r`/`radius`) and doubles it. Unit-suffixed strings on a `*_mm`
        param ("20mm", "0.75in") are converted. Every rewrite appears in `interpreted`.

        `I_mm4` is never derived from `b_mm`/`h_mm`: the second moment of area depends on
        the section SHAPE, so the call is refused with the rectangular substitution
        `I_mm4 = b*h**3/12` to pass explicitly (or use the `beam` calculator, which takes
        `section`/`b_mm`/`h_mm` and computes I itself).

        A misspelled or unknown `formula` returns the closest registered calculator(s)
        by edit distance plus the whole catalogue grouped by category (`known`), so the
        next call can pick — a near-miss spelling is resolved outright and echoed.
        This tool does NOT do arithmetic: there is no `add`/`sum` calculator.

        `prefilled` optionally records which inputs came from live geometry
        (ledger provenance per §7, e.g. `{"L_mm": "doc_bbox z body=shaft"}`).
        """
        try:
            kwargs.pop("kwargs", None)
            args = _extras(kwargs)
            notes: list = []
            fkey, fname = _pick(args, _FORMULA_KEYS)
            if formula is not None:
                fname = formula
            elif fkey is not None:
                notes.append(f"interpreted {fkey!r} as 'formula'")
            pkey, praw = _pick(args, _PARAMS_KEYS)
            kw = dict(params or {})
            if isinstance(praw, dict):
                kw.update(praw)
                if pkey is not None:
                    notes.append(f"interpreted {pkey!r} as 'params'")
            # Anything still loose is a param the model wrote at the top level.
            for k, v in args.items():
                if k not in ("raw_quote", "prefilled") and v is not None:
                    kw.setdefault(k, v)
                    notes.append(f"folded loose argument {k!r} into params")
            if fname is None or not str(fname).strip():
                return _dumps({
                    "ok": False, "reason": "bad_query",
                    "hint": ("research_eng_calc needs a formula id, e.g. "
                             "research_eng_calc('circle_area', {'diameter_mm': 20}). "
                             "Pick one from `known` or call list_calcs()."),
                    "known": _group_calcs()})
            resolved = _resolve_formula(str(fname).strip(), notes)
            if resolved is None:
                return _dumps(_unknown_formula(str(fname).strip(), notes))

            entry = _calcs.get_calc(resolved)
            if entry is not None:
                schema = entry.get("input_schema") or {}
                kw = _reconcile_calc_params(kw, list(schema), resolved, notes)
                out = _calcs.run_calc(resolved, kw, prefilled=prefilled)
                if not out.get("ok") and out.get("reason") == "bad_query":
                    out["input_schema"] = schema
                    out["hint"] = (f"{out.get('hint', '')} Expected params for "
                                   f"{resolved!r}: {sorted(schema)}; you passed "
                                   f"{sorted(kw)}.").strip()
                    rect = _rect_inertia_hint(kw, list(schema))
                    if rect:
                        out["hint"] = f"{out['hint']} {rect}"
                        out["substitution"] = "I_mm4 = b*h**3/12 (solid rectangle)"
            else:
                required = _LEGACY_FORMULAS[resolved][1]
                kw = _reconcile_calc_params(kw, required, resolved, notes)
                if raw_quote is not None:
                    kw["raw_quote"] = raw_quote
                out = research.eng_calc(resolved, **kw)
                if not out.get("ok") and out.get("reason") == "bad_query":
                    out["required_params"] = list(required)
                    out["doc"] = _LEGACY_FORMULAS[resolved][2]
                    out["hint"] = (f"{out.get('hint', '')} {resolved!r} needs "
                                   f"{list(required)} — {_LEGACY_FORMULAS[resolved][2]} "
                                   f"You passed {sorted(k for k in kw if k != 'raw_quote')}."
                                   ).strip()
                    rect = _rect_inertia_hint(kw, required)
                    if rect:
                        out["hint"] = f"{out['hint']} {rect}"
                        out["substitution"] = "I_mm4 = b*h**3/12 (solid rectangle)"
            if notes and isinstance(out, dict):
                out["interpreted"] = _interpreted_notes(notes)
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def list_calcs() -> str:
        """Discover registered engineering calculators (calculator registry).

        Returns {calc_id: {input_schema, prefill_hints}} for all registered
        calculators.  Use calc_ids with ``research_eng_calc`` to compute results.
        """
        try:
            return _dumps({"ok": True, "calcs": _calcs.list_calcs()})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def research_datasheet_fetch(source: str, tier: str = "datasheet") -> str:
        """Extract a datasheet PDF (local path or http(s) URL) to markdown with provenance.

        Uses only the MIT-licensed `pdfminer.six` extractor (AGPL PyMuPDF is excluded); if
        that optional dependency is absent it degrades (`{ok:false, reason:"no_pdf_tool"}`).
        Returns clean `content` markdown you should quote verbatim for any factual claim.
        """
        try:
            return _dumps(research.datasheet_fetch(source, tier=tier))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # --- ISO 286 fit calculator (aicad.calc/1 envelope, contracts/advisor.md §7) ---
    # IMPORTANT: call fit_calc BEFORE making any hole-sizing or bore-diameter decisions.
    # The fit class (e.g. H7/g6) must be resolved first — use recommend_fit if unknown.

    @mcp.tool()
    def fit_calc(nominal: Optional[float] = None, fit: Optional[str] = None,
                 prefilled: Optional[dict] = None, **kwargs: _Extra) -> str:
        """ISO 286 fit maths, in DESIGN mode (what should the sizes be?) or MEASURED
        mode (what fit do these two real sizes actually give?) — code does the maths.

        DESIGN MODE — a nominal size plus a fit class. Call this BEFORE hole sizing: it
        gives the ISO-table band the bore and shaft must hit.
            fit_calc(nominal=20, fit="H7/g6")
            fit_calc(diameter=20, fit_type="sliding")     # keyword -> recommend_fit
            fit_calc(shaft_diameter=20, fit_class="h7")   # single-component band
            fit_calc(nominal=20, hole="H7", shaft="h7")   # per-side classes

        MEASURED MODE — the two real sizes; returns the ACTUAL clearance/interference,
        its character, and the closest standard ISO 286 fit(s) for that basic size.
            fit_calc(hole_diameter=20.02, shaft_diameter=20.0)
            fit_calc(bore=20.0, bore_tolerance="H7", shaft=20.0, shaft_tolerance="g6")
            fit_calc(hole=20.0, hole_tolerance=0.01, shaft=20.01, shaft_tolerance=0.005)

        Accepted spellings (the tool normalises and ECHOES every rewrite in
        `interpreted`; a canonical call is returned byte-identical, with no extra keys):
        - nominal: `nominal`, `nominal_diameter`, `nominal_size`, `basic_size`,
          `diameter`, `dia`, `d`, `size`, `dimension`
        - fit: `fit`, `fit_class`, `fit_type`, `iso_fit`, `designation`, `tolerance`,
          `tolerance_class`, `class`, `band`, `grade` — an ISO designation ("H7/g6",
          "H7\\g6", "H7-g6", "H7", "g6") OR a functional keyword ("sliding",
          "press fit", "dowel"), which is resolved through `recommend_fit` and echoed
        - shaft: `shaft`, `shaft_diameter`, `shaft_dia`, `shaft_size`, `shd`, `pin`,
          `pin_diameter`; hole: `hole`, `hole_diameter`, `hole_dia`, `bore`,
          `bore_diameter`, `bore_dia`, `hole_size`, `bore_size`
        - per-side tolerance: `shaft_tolerance`/`shaft_tol`/`shaft_class` and
          `hole_tolerance`/`bore_tolerance`/`bore_class` — an ISO class ("g6", and a
          bore spelled "h7" is corrected to "H7"), a number (±t mm), "±0.05",
          "+0.02/-0.01", or [lower, upper]
        - sizes may carry units or the Ø shorthand: 20, "20", "20mm", "0.75in", "d20"
        - `clearance_limit`/`max_clearance`: checked against the computed fit and
          reported as `answer.limit_check`
        - `output="bore_size,clearance"`: projects those fields into
          `answer.requested` (never required — the envelope already carries them)

        Returns an `aicad.calc/1` envelope (`contracts/advisor.md §7`):
        - `answer.hole` / `answer.shaft`: [lower_mm, upper_mm] offsets from nominal
        - `answer.clearance`: [min_mm, max_mm] (negative = interference)
        - `answer.fit_character`: "clearance" | "transition" | "press"
        - `answer.hole_absolute` / `answer.shaft_absolute`: absolute bands [min, max]
        - measured mode adds `answer.nominal_mm` and `answer.closest_iso_fits`
        - `mode` ("design"|"measured") and `interpreted` appear only when the call
          needed interpretation

        `prefilled` optionally records which inputs came from live geometry measurement
        (ledger provenance per §7, e.g. `{"nominal": "doc_eval_measure F7"}`).

        A single diameter with no fit class and no mating diameter is genuinely
        ambiguous and returns a teaching error naming both modes.

        Spot-check values (ISO 286-2:2010): 20H7/g6 → hole 20.000–20.021, shaft
        19.980–19.993, clearance 0.007–0.041 mm, character=clearance.
        50H7/p6 → interference 0.001–0.042 mm, character=press.
        Shaft-basis designations (G7/h7, P7/h6) are computed through their hole-basis
        twin by the ISO 286-1 §5.3 mirror rule and say so in `interpreted`.
        `source.kind` is "closed_form" for formula-derived deviations (g, f, e, d, h),
        "table" for tabulated ones (k, n, p, r, s).
        """
        try:
            kwargs.pop("kwargs", None)
            if nominal is not None:
                kwargs["nominal"] = nominal
            if fit is not None:
                kwargs["fit"] = fit
            return _dumps(fit_from_kwargs(kwargs, prefilled=prefilled))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def recommend_fit(function: Optional[str] = None, **kwargs: _Extra) -> str:
        """Map a functional description to recommended ISO 286 fit classes with rationale.

        `function` is a free-text keyword: "sliding", "press fit", "running bearing",
        "dowel pin", "location", "transition", "bearing seat", etc. Also accepted as
        `fit_type`, `application`, `purpose`, `use`, `description`, `intent`, `query`,
        `text` (every rewrite is echoed in `interpreted`).

        Returns an `aicad.calc/1` envelope; `answer` is a list of matching fit
        recommendations, each with `recommended_classes`, `character`
        (clearance/transition/press), `rationale`, and `applications`.

        Pass a size as well — `diameter`, `nominal`, `shaft_diameter`, `bore_diameter`,
        … — and each recommendation comes back WITH the computed band for that size
        under `band` (`hole`/`shaft`/`clearance`/`hole_absolute`/`shaft_absolute`), so
        there is no need for a second `fit_calc` call:
            recommend_fit("sliding", diameter=20)
            → H7/g6, band.hole_absolute [20.000, 20.021], clearance [0.007, 0.041]

        Without a size, the workflow is:
        1. `recommend_fit("sliding")` → H7/g6
        2. `fit_calc(20.0, "H7/g6")` → hole 20.000–20.021, shaft 19.980–19.993
        3. Edit bore feature to 20.000–20.021 (or confirm with ask_user for
           function-critical dims).
        """
        try:
            kwargs.pop("kwargs", None)
            args = _extras(kwargs)
            notes: list = []
            key, text = _pick(args, _FUNCTION_KEYS)
            if function is not None:
                text = function
            elif key is not None:
                notes.append(f"interpreted {key!r} as {'function'!r}")
            if text is None or not str(text).strip():
                return _dumps({
                    "ok": False, "schema": "aicad.calc/1", "reason": "bad_query",
                    "hint": ("recommend_fit needs a functional description: "
                             "recommend_fit('sliding'), recommend_fit('press fit'), "
                             "recommend_fit('dowel pin'). Add diameter=20 to get the "
                             "computed band back with it.")})
            out = research.recommend_fit(str(text).replace("_", " ").replace("-", " "))
            if not out.get("ok"):
                return _dumps(out)
            _, size_raw = _pick(args, _NOMINAL_KEYS + _SHAFT_KEYS + _HOLE_KEYS)
            size = _size_mm(size_raw) if size_raw is not None else None
            if size is not None:
                for entry in out.get("answer") or []:
                    for cls in entry.get("recommended_classes") or []:
                        band = research.fit_calc(size, cls)
                        if band.get("ok"):
                            entry["band"] = dict(band["answer"], fit=cls,
                                                 nominal_mm=size)
                            break
                notes.append(f"computed the ISO band at \u00d8{size} mm for each "
                             f"recommendation (answer[].band) — no second fit_calc "
                             f"call needed")
            if notes:
                out["interpreted"] = _interpreted_notes(notes)
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def machining_ref_search(query: Optional[str] = None, topic: Optional[str] = None,
                             material: Optional[str] = None, process: Optional[str] = None,
                             limit: int = 10, **kwargs: _Extra) -> str:
        """Search the bundled, standards-cited machining reference.

        Filters may be supplied as `topic`/`category`, `material`/`alloy`, and
        `process`/`operation`. Canonical facts are not duplicated: fits, basic threads,
        materials, tap drills, and cutting data return `delegate_tool` pointers.
        """
        try:
            kwargs.pop("kwargs", None)
            args = _extras(kwargs)
            notes: list[str] = []
            qkey, qvalue = _pick(args, ("query", "q", "text", "search", "term"))
            if query is None and qvalue is not None:
                query = str(qvalue)
                notes.append(f"interpreted {qkey!r} as 'query'")
            for canonical, aliases, current in (
                ("topic", ("topic", "category", "subject"), topic),
                ("material", ("material", "alloy", "grade"), material),
                ("process", ("process", "operation", "method"), process),
            ):
                key, value = _pick(args, aliases)
                if current is None and value is not None:
                    if canonical == "topic":
                        topic = str(value)
                    elif canonical == "material":
                        material = str(value)
                    else:
                        process = str(value)
                    notes.append(f"interpreted {key!r} as {canonical!r}")
            lkey, lvalue = _pick(args, ("limit", "max_results", "max", "count"))
            if lvalue is not None:
                limit = lvalue
                notes.append(f"interpreted {lkey!r} as 'limit'")
            out = _machining_ref.machining_ref_search(
                str(query or ""), topic=topic, material=material, process=process, limit=limit
            )
            if notes and isinstance(out, dict):
                out["interpreted"] = _interpreted_notes(notes)
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def machining_ref_get(record_id: Optional[str] = None, **kwargs: _Extra) -> str:
        """Return one bundled machining-reference row by its stable record id."""
        try:
            kwargs.pop("kwargs", None)
            args = _extras(kwargs)
            key, value = _pick(args, ("record_id", "id", "record", "reference_id", "ref_id"))
            notes: list[str] = []
            if record_id is None and value is not None:
                record_id = str(value)
                notes.append(f"interpreted {key!r} as 'record_id'")
            out = _machining_ref.machining_ref_get(str(record_id or ""))
            if notes and isinstance(out, dict):
                out["interpreted"] = _interpreted_notes(notes)
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def machining_ref_index_pdf(path: Optional[str] = None,
                                project_root: Optional[str] = None,
                                **kwargs: _Extra) -> str:
        """Index a user-owned PDF by page in the project's local `.plm/references` FTS5.

        The SHA-256-keyed index never leaves the project and reports scanned/no-text pages
        without pretending OCR occurred.
        """
        try:
            kwargs.pop("kwargs", None)
            args = _extras(kwargs)
            notes: list[str] = []
            pkey, pvalue = _pick(args, ("path", "file", "pdf", "source", "pdf_path"))
            if path is None and pvalue is not None:
                path = str(pvalue)
                notes.append(f"interpreted {pkey!r} as 'path'")
            rkey, rvalue = _pick(args, ("project_root", "project", "root", "workspace"))
            if project_root is None and rvalue is not None:
                project_root = str(rvalue)
                notes.append(f"interpreted {rkey!r} as 'project_root'")
            if path is None or not str(path).strip():
                return _dumps({"ok": False, "reason": "bad_query",
                               "hint": "Provide path to a user-owned PDF."})
            out = _machining_ref.machining_ref_index_pdf(str(path), project_root=project_root)
            if notes and isinstance(out, dict):
                out["interpreted"] = _interpreted_notes(notes)
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def machining_ref_search_pdf(query: Optional[str] = None,
                                 source_id: Optional[str] = None,
                                 limit: int = 8, **kwargs: _Extra) -> str:
        """Search project-local user-PDF indexes and return bounded page-cited snippets."""
        try:
            kwargs.pop("kwargs", None)
            args = _extras(kwargs)
            notes: list[str] = []
            qkey, qvalue = _pick(args, ("query", "q", "text", "search", "term"))
            if query is None and qvalue is not None:
                query = str(qvalue)
                notes.append(f"interpreted {qkey!r} as 'query'")
            skey, svalue = _pick(args, ("source_id", "source", "pdf_id", "reference_id"))
            if source_id is None and svalue is not None:
                source_id = str(svalue)
                notes.append(f"interpreted {skey!r} as 'source_id'")
            lkey, lvalue = _pick(args, ("limit", "max_results", "max", "count"))
            if lvalue is not None:
                limit = lvalue
                notes.append(f"interpreted {lkey!r} as 'limit'")
            out = _machining_ref.machining_ref_search_pdf(
                str(query or ""), source_id=source_id, limit=limit
            )
            if notes and isinstance(out, dict):
                out["interpreted"] = _interpreted_notes(notes)
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)
