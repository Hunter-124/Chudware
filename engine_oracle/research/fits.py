"""ISO 286 fit calculator and dimension-scheme helper.

Contract: ``contracts/advisor.md`` §7 (aicad.calc/1 envelope). Pillar 20:
the advisory layer's bore/shaft example — "bore Ø19.80 vs shaft Ø20.00, for
H7/g6 use 20.000–20.021" — backed by real ISO 286 data and a calculator.

Public API
----------
- :func:`fit_calc` — hole+shaft band, clearance range, fit character, aicad.calc/1.
- :func:`tolerance_band` — single-component band [lower, upper] in mm.
- :func:`recommend_fit` — functional keywords → recommended fit classes + rationale.

Envelope note
~~~~~~~~~~~~~
These functions return the §7 **aicad.calc/1** shape, NOT the Pillar 8
``common.envelope()`` shape used by ``research_*`` tools.  The mechanism
(bundled data, deterministic code, no network) is the same; the keys differ.
Use ``_calc_envelope()`` internally.

Accuracy notes
~~~~~~~~~~~~~~
- ISO 286 range boundary convention: "over X up to and including Y", so
  nominal 50 mm falls in the 30–50 range, not 50–80.
- IT grades are computed via ``i = 0.45*D^(1/3)+0.001*D`` (D in mm, result
  in µm) then multiplied by the grade factor and rounded to the nearest
  integer µm.  ``source.kind="closed_form"``.
- Fundamental deviations for shafts d/e/f/g/h/js: formula-derived,
  ``source.kind="closed_form"``. Shafts k/n/p/r/s: tabulated values from
  ISO 286-1:2010 Table 9, ``source.kind="table"``.
- H holes (EI=0, ES=+IT): formula-derived, ``source.kind="closed_form"``.
  Other uppercase holes (G/F/E/D/JS/K/N/P/R/S) require the Δ-correction
  rule per ISO 286-1 §5.3 and are **not** implemented in this release;
  they raise ``FitError("unsupported_hole")``.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Optional

# ──────────────────────────────────────────────────── data loading ────────────

_DATA_DIR = Path(__file__).parent / "data"

def _load_json(name: str) -> dict:
    return json.loads((_DATA_DIR / name).read_text(encoding="utf-8"))


# Lazy singletons
_ISO286: Optional[dict] = None
_FIT_RECS: Optional[dict] = None


def _iso286() -> dict:
    global _ISO286
    if _ISO286 is None:
        _ISO286 = _load_json("iso286.json")
    return _ISO286


def _fit_recs() -> list[dict]:
    global _FIT_RECS
    if _FIT_RECS is None:
        _FIT_RECS = _load_json("fit_recommendations.json")["fits"]
    return _FIT_RECS


# ──────────────────────────────────────────────────── error ───────────────────

class FitError(ValueError):
    """Raised for bad input; caught at the MCP boundary and returned as §6."""
    def __init__(self, reason: str, hint: str) -> None:
        super().__init__(hint)
        self.reason = reason
        self.hint = hint


def _fail(reason: str, hint: str) -> dict:
    """§6 degradation shape — mirrors ``common.fail()``."""
    return {"ok": False, "reason": reason, "hint": hint}


# ──────────────────────────────────────────────────── envelope ────────────────

def _calc_envelope(calc: str, answer: Any, units: str, equation: str,
                   inputs: dict, source_kind: str, citation: str,
                   notes: list[str] | None = None, prefilled: dict | None = None) -> dict:
    """Build an aicad.calc/1 envelope (contracts/advisor.md §7)."""
    return {
        "schema": "aicad.calc/1",
        "ok": True,
        "calc": calc,
        "answer": answer,
        "units": units,
        "equation": equation,
        "inputs": {
            **({"prefilled": prefilled} if prefilled else {}),
            **inputs,
        },
        "source": {
            "kind": source_kind,
            "citation": citation,
        },
        "notes": notes or [],
    }


# ──────────────────────────────────────────────────── range lookup ────────────

def _find_range(nominal_mm: float) -> dict:
    """Return the IT-grade row for *nominal_mm* using ISO 'over X up to Y' convention.

    Raises FitError for out-of-range nominals (< 1 mm or > 500 mm).
    """
    if nominal_mm < 1.0 or nominal_mm > 500.0:
        raise FitError(
            "out_of_range",
            f"nominal {nominal_mm} mm is outside ISO 286 coverage 1–500 mm.",
        )
    for row in _iso286()["it_grades"]["ranges"]:
        lo, hi = row["over_mm"], row["up_to_mm"]
        # ISO convention: "over lo, up to and including hi"
        if lo < nominal_mm <= hi:
            return row
    # Edge: exactly 1 mm maps to 1–3 range
    if nominal_mm == 1.0:
        return _iso286()["it_grades"]["ranges"][0]
    raise FitError("out_of_range", f"no range found for nominal {nominal_mm} mm (bug).")


def _it_grade(row: dict, grade_str: str) -> tuple[int, str]:
    """Return (IT_value_um, source_kind) for a grade string like 'IT7' or '7'."""
    key = grade_str if grade_str.startswith("IT") else f"IT{grade_str}"
    if key not in row:
        raise FitError(
            "unsupported_grade",
            f"IT grade {grade_str!r} not in data; available: IT5–IT12.",
        )
    return int(row[key]), _iso286()["it_grades"]["_source_kind"]


# ──────────────────────────────────────────────────── shaft deviation ─────────

_FORMULA_SHAFTS = frozenset("defg")

def _shaft_es_um(letter: str, nominal_mm: float, row: dict) -> tuple[float, str]:
    """Upper deviation (es) in µm for lowercase shaft letter + nominal.

    Returns (es_um, source_kind).  Raises FitError for unsupported letters.
    """
    D = row["D_mean_mm"]

    if letter == "h":
        return 0.0, "closed_form"

    if letter == "js":
        # Caller computes ±IT/2 after grade is known; es returned as 0 placeholder.
        # tolerance_band() handles JS directly — this branch isn't normally called.
        raise FitError("internal", "js deviation handled in tolerance_band, not here.")

    if letter in _FORMULA_SHAFTS:
        formulas: dict[str, Any] = {
            "d": lambda D: -16 * D ** 0.44,
            "e": lambda D: -11 * D ** 0.41,
            "f": lambda D: -5.5 * D ** 0.41,
            "g": lambda D: -2.5 * D ** 0.34,
        }
        es = round(formulas[letter](D))
        return float(es), "closed_form"

    # Tabulated shafts (k, n, p, r, s)
    shaft_data = _iso286()["shaft_deviations"].get(letter)
    if shaft_data is None:
        raise FitError(
            "unsupported_deviation",
            f"shaft letter {letter!r} not in data; supported: d, e, f, g, h, js, k, n, p, r, s.",
        )
    if "_todo" in shaft_data:
        raise FitError(
            "unsupported_deviation",
            f"shaft {letter!r} is deferred: {shaft_data['_todo']}",
        )
    for r in shaft_data["ranges"]:
        lo, hi = r["over_mm"], r["up_to_mm"]
        if lo < nominal_mm <= hi:
            return float(r["es_um"]), shaft_data["_source_kind"]
    if nominal_mm == 1.0:
        r = shaft_data["ranges"][0]
        return float(r["es_um"]), shaft_data["_source_kind"]
    raise FitError("out_of_range", f"tabulated shaft {letter!r}: no range for {nominal_mm} mm.")


# ──────────────────────────────────────────────────── tolerance_band ──────────

def tolerance_band(nominal_mm: float, grade_str: str, deviation_letter: str,
                   basis: str = "shaft") -> dict:
    """Return the aicad.calc/1 envelope for a single component's tolerance band.

    ``deviation_letter`` is lowercase (shaft) or uppercase (hole); only 'H' is
    supported for holes.  ``grade_str`` is e.g. "7" or "IT7".

    Returns ``{"lower_mm": float, "upper_mm": float}`` in the ``answer`` field,
    where lower/upper are **offsets** from nominal (lower <= 0 for clearance fits).
    Absolute sizes: nominal + lower, nominal + upper.
    """
    row = _find_range(nominal_mm)
    it_um, it_source = _it_grade(row, grade_str)

    if basis == "hole" or deviation_letter.isupper():
        letter = deviation_letter.upper()
        if letter == "H":
            lower_um, upper_um = 0.0, float(it_um)
            source_kind = "closed_form"
        elif letter == "JS":
            upper_um = math.floor(it_um / 2)
            lower_um = -math.ceil(it_um / 2)
            source_kind = "closed_form"
        else:
            return _fail(
                "unsupported_hole",
                f"Hole deviation {letter!r} requires the Δ-correction rule (ISO 286-1 §5.3) "
                f"and is not implemented in this release. Supported holes: H, JS.",
            )
    else:
        letter = deviation_letter.lower()
        if letter == "js":
            upper_um = math.floor(it_um / 2)
            lower_um = -math.ceil(it_um / 2)
            source_kind = "closed_form"
        else:
            es_um, source_kind = _shaft_es_um(letter, nominal_mm, row)
            upper_um = es_um                    # es = upper deviation (signed)
            lower_um = es_um - it_um            # ei = es - IT

    if it_source == "table" or source_kind == "table":
        source_kind = "table"

    return _calc_envelope(
        calc="tolerance_band",
        answer={
            "lower_mm": round(lower_um / 1000, 6),
            "upper_mm": round(upper_um / 1000, 6),
        },
        units="mm",
        equation=f"ISO 286-2, nominal={nominal_mm}mm, {deviation_letter}{grade_str}",
        inputs={"nominal_mm": nominal_mm, "grade": grade_str, "deviation": deviation_letter},
        source_kind=source_kind,
        citation="ISO 286-2:2010",
    )


# ──────────────────────────────────────────────────── fit_calc ───────────────

_FIT_RE = re.compile(
    r"""^
    (?P<hole>[A-Z][A-Z]?)(?P<hgrade>\d+)   # hole: e.g. H7
    (?:[/\\]                                # separator
    (?P<shaft>[a-z][a-z]?)(?P<sgrade>\d+)  # shaft: e.g. g6
    )?
    $""",
    re.VERBOSE,
)


def fit_calc(nominal: float, fit: str,
             prefilled: Optional[dict] = None) -> dict:
    """Compute hole and shaft tolerance bands plus fit character.

    Parameters
    ----------
    nominal : float
        Nominal dimension in mm (both hole and shaft share this).
    fit : str
        ISO 286 fit designation, combined (``"H7/g6"``) or single (``"H7"``,
        ``"g6"``).  Combined forms give full fit analysis; single forms give
        just the band.
    prefilled : dict, optional
        ``{param_name: source_string}`` — records which inputs came from live
        geometry measurement rather than user input (ledger provenance per §7).

    Returns
    -------
    dict
        aicad.calc/1 envelope.  ``answer`` has:

        - ``hole`` : [lower_mm, upper_mm] offsets from nominal, or null if
          fit is shaft-only.
        - ``shaft`` : [lower_mm, upper_mm] offsets, or null if hole-only.
        - ``clearance`` : [min_mm, max_mm] (positive = clearance; negative = interference).
          null for single-component fits.
        - ``fit_character`` : ``"clearance"`` | ``"transition"`` | ``"press"`` | null.
        - ``hole_absolute`` : [min_abs_mm, max_abs_mm] or null.
        - ``shaft_absolute`` : [min_abs_mm, max_abs_mm] or null.
    """
    fit = (fit or "").strip()
    m = _FIT_RE.match(fit)

    # Try shaft-only input (lowercase first char)
    if m is None and fit and fit[0].islower():
        shaft_re = re.compile(r"^(?P<shaft>[a-z][a-z]?)(?P<sgrade>\d+)$")
        ms = shaft_re.match(fit)
        if ms:
            m_dict = {"hole": None, "hgrade": None,
                      "shaft": ms.group("shaft"), "sgrade": ms.group("sgrade")}
        else:
            return _fail("bad_fit", f"Cannot parse fit string {fit!r}. "
                         "Use 'H7/g6', 'H7', or 'g6'.")
    elif m:
        m_dict = m.groupdict()
    else:
        return _fail("bad_fit", f"Cannot parse fit string {fit!r}. "
                     "Use 'H7/g6', 'H7', or 'g6'.")

    hole_letter = m_dict.get("hole")
    hole_grade  = m_dict.get("hgrade")
    shaft_letter = m_dict.get("shaft")
    shaft_grade  = m_dict.get("sgrade")

    notes: list[str] = []
    all_table = False

    # ── hole band ──
    hole_band: Optional[tuple[float, float]] = None
    if hole_letter and hole_grade:
        try:
            hb = tolerance_band(nominal, hole_grade, hole_letter, basis="hole")
        except FitError as e:
            return _fail(e.reason, e.hint)
        if not hb["ok"]:
            return hb
        hole_band = (hb["answer"]["lower_mm"], hb["answer"]["upper_mm"])
        if hb["source"]["kind"] == "table":
            all_table = True

    # ── shaft band ──
    shaft_band: Optional[tuple[float, float]] = None
    if shaft_letter and shaft_grade:
        try:
            sb = tolerance_band(nominal, shaft_grade, shaft_letter, basis="shaft")
        except FitError as e:
            return _fail(e.reason, e.hint)
        if not sb["ok"]:
            return sb
        shaft_band = (sb["answer"]["lower_mm"], sb["answer"]["upper_mm"])
        if sb["source"]["kind"] == "table":
            all_table = True

    # ── clearance + character ──
    clearance: Optional[tuple[float, float]] = None
    character: Optional[str] = None

    if hole_band is not None and shaft_band is not None:
        # clearance = hole_size - shaft_size
        # min clearance = hole_min - shaft_max = (lower_h) - (upper_s)
        # max clearance = hole_max - shaft_min = (upper_h) - (lower_s)
        min_clr = round(hole_band[0] - shaft_band[1], 6)
        max_clr = round(hole_band[1] - shaft_band[0], 6)
        clearance = (min_clr, max_clr)

        if min_clr >= 0.0:
            character = "clearance"
        elif max_clr <= 0.0:
            character = "press"
        else:
            character = "transition"

    # ── absolute sizes ──
    hole_abs = None
    if hole_band is not None:
        hole_abs = [round(nominal + hole_band[0], 6), round(nominal + hole_band[1], 6)]

    shaft_abs = None
    if shaft_band is not None:
        shaft_abs = [round(nominal + shaft_band[0], 6), round(nominal + shaft_band[1], 6)]

    source_kind = "table" if all_table else "closed_form"
    if hole_band is not None and shaft_band is not None:
        # Mixed: if either component used tabulated data, mark mixed
        h_kind = "table" if hole_letter and hole_letter.upper() not in ("H", "JS") else "closed_form"
        s_kind = "table" if shaft_letter and shaft_letter.lower() not in ("d", "e", "f", "g", "h", "js") else "closed_form"
        if h_kind == "table" or s_kind == "table":
            source_kind = "table"
            notes.append("Shaft fundamental deviation from ISO 286-1 Table 9 (tabulated).")
        else:
            source_kind = "closed_form"

    components = []
    if hole_letter and hole_grade:
        components.append(f"{hole_letter}{hole_grade}")
    if shaft_letter and shaft_grade:
        components.append(f"{shaft_letter}{shaft_grade}")
    equation = f"ISO 286-2, nominal={nominal}mm, {'/'.join(components)}"

    answer: dict[str, Any] = {
        "hole":    list(hole_band) if hole_band is not None else None,
        "shaft":   list(shaft_band) if shaft_band is not None else None,
        "clearance": list(clearance) if clearance is not None else None,
        "fit_character": character,
        "hole_absolute": hole_abs,
        "shaft_absolute": shaft_abs,
    }

    if character == "press" and clearance is not None:
        notes.append(
            f"Press fit: min interference {-clearance[1]*1000:.1f} µm, "
            f"max interference {-clearance[0]*1000:.1f} µm."
        )
    elif character == "transition" and clearance is not None:
        notes.append(
            f"Transition fit: clearance range {clearance[0]*1000:.1f}..{clearance[1]*1000:.1f} µm "
            f"(negative = interference)."
        )

    return _calc_envelope(
        calc="fit_calc",
        answer=answer,
        units="mm",
        equation=equation,
        inputs={
            "nominal": nominal,
            "fit": fit,
        },
        source_kind=source_kind,
        citation="ISO 286-2:2010",
        notes=notes,
        prefilled=prefilled,
    )


# ──────────────────────────────────────────────────── recommend_fit ───────────

def recommend_fit(function: str) -> dict:
    """Map a functional keyword to recommended ISO 286 fit classes + rationale.

    ``function`` is a free-text string; matching is keyword-based (case-insensitive).
    Returns an aicad.calc/1 envelope.  ``answer`` is a list of matching
    ``{id, recommended_classes, character, rationale, applications, notes}`` dicts,
    ordered by match quality.

    Examples
    --------
    >>> recommend_fit("sliding")["answer"][0]["recommended_classes"]
    ['H7/g6']
    >>> recommend_fit("press fit")["answer"][0]["character"]
    'press'
    """
    text = (function or "").strip().lower()
    if not text:
        return _fail("bad_query", "function keyword is empty; try 'slide', 'press', 'run', etc.")

    tokens = set(re.findall(r"[a-z]+", text))
    scored: list[tuple[int, dict]] = []

    for entry in _fit_recs():
        score = 0
        for kw in entry["keywords"]:
            kw_tokens = set(re.findall(r"[a-z]+", kw.lower()))
            overlap = len(tokens & kw_tokens)
            if overlap > 0:
                score += overlap
        if score > 0:
            scored.append((score, entry))

    if not scored:
        all_kws = sorted({kw for e in _fit_recs() for kw in e["keywords"]})
        return _fail(
            "not_found",
            f"No fit recommendation matched {function!r}. "
            f"Try one of: {', '.join(all_kws[:20])}.",
        )

    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for _, entry in scored:
        results.append({
            "id": entry["id"],
            "recommended_classes": entry["recommended_classes"],
            "character": entry["character"],
            "rationale": entry["rationale"],
            "applications": entry["applications"],
            "notes": entry["notes"],
            "typical_clearance_range_um": entry.get("typical_clearance_range_um"),
        })

    return _calc_envelope(
        calc="recommend_fit",
        answer=results,
        units=None,
        equation="keyword match against fit_recommendations.json",
        inputs={"function": function},
        source_kind="table",
        citation="ISO 286-2:2010; Shigley's Mechanical Engineering Design 10th ed.",
        notes=[f"Matched {len(results)} fit class(es) for keyword {function!r}."],
    )
