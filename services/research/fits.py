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
- IT grade values are the tabulated ISO 286-1:2010 Table 1 values in
  ``iso286.json`` (``it_grades``), not computed; a tolerance band built from them
  reports ``source.kind="table"``.
- Shaft fundamental deviations are tabulated; k/m/n/p/r/s use the lower
  deviation, whereas d/e/f/g/h use the upper deviation.
- Hole fundamental deviations A/B/C/CD/D/E/EF/F/FG/G and J/K/M/N/P/R/S
  are tabulated, including the grade-dependent ISO 286 special rule. Sizes above 500 mm and
  unsupported grades are refused rather than extrapolated.
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
            f"IT grade {grade_str!r} not in data; available: IT4–IT12.",
        )
    return int(row[key]), _iso286()["it_grades"]["_source_kind"]


# ──────────────────────────────────────────────────── shaft deviation ─────────

def _shaft_deviation_um(letter: str, grade: int, nominal: float, row: dict) -> tuple[float, str]:
    """Return the tabulated fundamental deviation and whether it is es or ei."""
    values = next(r for r in _HOLE_ROWS if nominal <= r[0])
    if letter in ("a", "b", "c", "cd", "d", "e", "ef", "f", "fg", "g"):
        return -_clearance_deviation_um(letter.upper(), nominal), "upper"
    if letter == "h":
        return 0.0, "upper"
    if letter in ("t", "u", "v", "x", "y", "z", "za", "zb", "zc"):
        return -_interference_deviation_um(letter.upper(), nominal), "lower"
    if letter not in ("k", "m", "n", "p", "r", "s"):
        raise FitError("unsupported_deviation",
                       f"Shaft {letter!r} is not in the bundled fundamental deviation table.")
    if nominal <= 3:
        return {"k": 0, "m": 2, "n": 4, "p": 6, "r": 10, "s": 14}[letter], "lower"
    delta7 = row["IT7"] - row["IT6"]
    ei = {"k": delta7 - values[8], "m": -values[9],
          "n": delta7 - values[10], "p": -values[11],
          "r": -values[12], "s": -values[13]}[letter]
    if letter == "k" and grade >= 8:
        ei = 0
    return ei, "lower"


# ISO 286 hole deviations in micrometres, over previous limit through this limit.
# Source: https://roymech.org/Useful_Tables/ISO_Tolerances/ISO_Hole_LIM_1.html
# Columns: upper bound, EI(D,E,F,G), ES(J6,J7,J8,K7), ES(M>8,N7,P,R,S).
# P/R/S columns are their uncorrected ES; K/M/N and P/R/S use the special
# grade correction for holes above 3 mm. M entries are the uncorrected ES,
# not the visibly erroneous M7/M8 cells at 400-500 mm in that source.
_HOLE_ROWS = (
    (3, 20,14,6,2, 2,4,6,0, -2,-4,-6,-10,-14),
    (6, 30,20,10,4, 5,6,10,3, -4,-4,-12,-15,-19),
    (10,40,25,13,5, 5,8,12,5, -6,-4,-15,-19,-23),
    (14,50,32,16,6, 6,10,15,6, -7,-5,-18,-23,-28),
    (18,50,32,16,6, 6,10,15,6, -7,-5,-18,-23,-28),
    (24,65,40,20,7, 8,12,20,6, -8,-7,-22,-28,-35),
    (30,65,40,20,7, 8,12,20,6, -8,-7,-22,-28,-35),
    (40,80,50,25,9, 10,14,24,7, -9,-8,-26,-34,-43),
    (50,80,50,25,9, 10,14,24,7, -9,-8,-26,-34,-43),
    (65,100,60,30,10, 13,18,28,9, -11,-9,-32,-41,-53),
    (80,100,60,30,10, 13,18,28,9, -11,-9,-32,-43,-59),
    (100,120,72,36,12, 16,22,34,10, -13,-10,-37,-51,-71),
    (120,120,72,36,12, 16,22,34,10, -13,-10,-37,-54,-79),
    (140,145,85,43,14, 18,26,41,12, -15,-12,-43,-63,-92),
    (160,145,85,43,14, 18,26,41,12, -15,-12,-43,-65,-100),
    (180,145,85,43,14, 18,26,41,12, -15,-12,-43,-68,-108),
    (200,170,100,50,15, 22,30,47,13, -17,-14,-50,-77,-122),
    (225,170,100,50,15, 22,30,47,13, -17,-14,-50,-80,-130),
    (250,170,100,50,15, 22,30,47,13, -17,-14,-50,-84,-140),
    (280,190,110,56,17, 25,36,55,16, -20,-14,-56,-94,-158),
    (315,190,110,56,17, 25,36,55,16, -20,-14,-56,-98,-170),
    (355,210,125,62,18, 29,39,60,17, -21,-16,-62,-108,-190),
    (400,210,125,62,18, 29,39,60,17, -21,-16,-62,-114,-208),
    (450,230,135,68,20, 33,43,66,18, -23,-17,-68,-126,-232),
    (500,230,135,68,20, 33,43,66,18, -23,-17,-68,-132,-252),
)

# ISO 286-1:2010 Table 2, A/B/C deviations align with _HOLE_ROWS.
_ABC_HOLES = (
    (270,140,60), (270,140,70), (280,150,80), (290,150,95), (290,150,95),
    (300,160,110), (300,160,110), (310,170,120), (320,180,130),
    (340,190,140), (360,200,150), (380,220,170), (410,240,180),
    (460,260,200), (520,280,210), (580,310,230), (660,340,240),
    (740,380,260), (820,420,280), (920,480,300), (1050,540,330),
    (1200,600,360), (1350,680,400), (1500,760,440), (1650,840,480),
)
_INTERMEDIATE_HOLES = {
    "CD": (34, 46, 56, 70, 85, 100),
    "EF": (10, 14, 18, 23, 28, 35),
    "FG": (4, 6, 8, 10, 12, 15),
}

# ISO 286-1:2010 Table 3, uncorrected ES for T/U/V/X/Y/Z/ZA/ZB/ZC.
# None marks a size/letter combination absent from the standard table.
_INTERFERENCE_LETTERS = ("T", "U", "V", "X", "Y", "Z", "ZA", "ZB", "ZC")
_INTERFERENCE_HOLES = (
    (None,-18,None,-20,None,-26,-32,-40,-60),
    (None,-23,None,-28,None,-35,-42,-50,-80),
    (None,-28,None,-34,None,-42,-52,-67,-97),
    (None,-33,None,-40,None,-50,-64,-90,-130),
    (None,-33,-39,-45,None,-60,-77,-108,-150),
    (None,-41,-47,-54,-63,-73,-98,-136,-188),
    (-41,-48,-55,-64,-75,-88,-118,-160,-218),
    (-48,-60,-68,-80,-94,-112,-148,-200,-274),
    (-54,-70,-81,-97,-114,-136,-180,-242,-325),
    (-66,-87,-102,-122,-144,-172,-226,-300,-405),
    (-75,-102,-120,-146,-174,-210,-274,-360,-480),
    (-91,-124,-146,-178,-214,-258,-335,-445,-585),
    (-104,-144,-172,-210,-254,-310,-400,-525,-690),
    (-122,-170,-202,-248,-300,-365,-470,-620,-800),
    (-134,-190,-228,-280,-340,-415,-535,-700,-900),
    (-146,-210,-252,-310,-380,-465,-600,-780,-1000),
    (-166,-236,-284,-350,-425,-520,-670,-880,-1150),
    (-180,-258,-310,-385,-470,-575,-740,-960,-1250),
    (-196,-284,-340,-425,-520,-640,-820,-1050,-1350),
    (-218,-315,-385,-475,-580,-710,-920,-1200,-1550),
    (-240,-350,-425,-525,-650,-790,-1000,-1300,-1700),
    (-268,-390,-475,-590,-730,-900,-1150,-1500,-1900),
    (-294,-435,-530,-660,-820,-1000,-1300,-1650,-2100),
    (-330,-490,-595,-740,-920,-1100,-1450,-1850,-2400),
    (-360,-540,-660,-820,-1000,-1250,-1600,-2100,-2600),
)


def _interference_deviation_um(letter: str, nominal: float) -> float:
    index = next(i for i, row in enumerate(_HOLE_ROWS) if nominal <= row[0])
    value = _INTERFERENCE_HOLES[index][_INTERFERENCE_LETTERS.index(letter)]
    if value is None:
        raise FitError("unsupported_deviation", f"ISO 286 has no {letter} value at {nominal:g} mm.")
    return value




def _clearance_deviation_um(letter: str, nominal: float) -> float:
    index = next(i for i, row in enumerate(_HOLE_ROWS) if nominal <= row[0])
    if letter in ("A", "B", "C"):
        if nominal <= 1 and letter in ("A", "B"):
            raise FitError("unsupported_deviation", "ISO 286 excludes A and B at or below 1 mm.")
        return _ABC_HOLES[index]["ABC".index(letter)]
    if letter in _INTERMEDIATE_HOLES:
        bounds = (3, 6, 10, 18, 30, 50)
        for i, upper in enumerate(bounds):
            if nominal <= upper:
                return _INTERMEDIATE_HOLES[letter][i]
        raise FitError("unsupported_deviation", f"{letter} is not tabulated above 50 mm.")
    return _HOLE_ROWS[index][1 + "DEFG".index(letter)]




def _hole_deviation_um(letter: str, grade: int, nominal: float, it_row: dict) -> tuple[float, str]:
    """Return EI for D-G, or ES for J-S, using tabulated deviations."""
    values = next(r for r in _HOLE_ROWS if nominal <= r[0])
    if letter in ("A", "B", "C", "CD", "D", "E", "EF", "F", "FG", "G"):
        return _clearance_deviation_um(letter, nominal), "lower"
    if letter == "J":
        if grade not in (6, 7, 8):
            raise FitError("unsupported_hole", "J holes are tabulated only for grades 6, 7 and 8.")
        return values[5 + grade - 6], "upper"
    if letter not in ("K", "M", "N", "P", "R", "S", *_INTERFERENCE_LETTERS):
        raise FitError("unsupported_hole", f"Hole {letter!r} is not in the bundled table.")
    if letter == "N" and grade >= 9 and nominal <= 1:
        raise FitError("unsupported_hole", "ISO 286 excludes N9 and coarser at or below 1 mm.")
    if nominal <= 3:
        if letter in _INTERFERENCE_LETTERS:
            return _interference_deviation_um(letter, nominal), "upper"
        return {"K": 0, "M": -2, "N": -4, "P": -6, "R": -10, "S": -14}[letter], "upper"
    # K/M/N through IT8 and P/R/S through IT7 use Δ = ITn - IT(n-1).
    # IT3 is not bundled, so IT4 special-rule values cannot be derived.
    special = grade <= (8 if letter in ("K", "M", "N") else 7)
    if special and grade == 4:
        raise FitError("unsupported_grade", "IT4 for this hole needs the unbundled IT3 tolerance.")
    delta7 = it_row["IT7"] - it_row["IT6"]
    if letter in _INTERFERENCE_LETTERS:
        base = _interference_deviation_um(letter, nominal)
    else:
        base = {"K": values[8] - delta7, "M": values[9],
                "N": values[10] - delta7, "P": values[11],
                "R": values[12], "S": values[13]}[letter]
    if letter in ("K", "N") and grade >= 9:
        base = 0
    if special:
        base += it_row[f"IT{grade}"] - it_row[f"IT{grade - 1}"]
    # ISO 286-1 Table 2 footnote b overrides the calculated M6 value.
    if letter == "M" and grade == 6 and 250 < nominal <= 315:
        base = -9
    return base, "upper"


# ISO 273 fine/medium/coarse series; published charts:
# https://www.ekinsun.com/custom-fasteners/clearance-hole-chart/
# https://www.trueprecisionmachining.com/engineering/clearance-hole-chart/
_ISO273 = {
    1.6: (1.7, 1.8, 2), 2: (2.2, 2.4, 2.6), 2.5: (2.7, 2.9, 3.1),
    3: (3.2, 3.4, 3.6), 4: (4.3, 4.5, 4.8), 5: (5.3, 5.5, 5.8),
    6: (6.4, 6.6, 7.0), 8: (8.4, 9.0, 10.0), 10: (10.5, 11.0, 12.0),
    12: (13, 13.5, 14.5), 14: (15, 15.5, 16.5), 16: (17, 17.5, 18.5),
    18: (19, 20, 21), 20: (21, 22, 24), 22: (23, 24, 26),
    24: (25, 26, 28), 27: (28, 30, 32), 30: (31, 33, 35), 36: (37, 39, 42),
}


def iso273_clearance_hole(nominal_mm: float, clearance_class: str = "normal") -> dict:
    """Look up a metric ISO 273 clearance diameter; never interpolate sizes."""
    aliases = {"fine": "fine", "close": "fine", "medium": "medium",
               "normal": "medium", "coarse": "coarse", "loose": "coarse"}
    series = aliases.get(str(clearance_class).lower())
    if series is None:
        return _fail("unsupported_clearance_class", "Use fine/close, medium/normal or coarse/loose.")
    if nominal_mm not in _ISO273:
        return _fail("unsupported_fastener_size", "No bundled ISO 273 row for this metric diameter.")
    return {"ok": True, "nominal_mm": nominal_mm, "clearance_class": series,
            "diameter_mm": _ISO273[nominal_mm][("fine", "medium", "coarse").index(series)],
            "citation": "ISO 273:1979; https://www.ekinsun.com/custom-fasteners/clearance-hole-chart/"}


# ──────────────────────────────────────────────────── tolerance_band ──────────

def tolerance_band(nominal_mm: float, grade_str: str, deviation_letter: str,
                   basis: str = "shaft") -> dict:
    """Return the aicad.calc/1 envelope for a single component's tolerance band.

    ``deviation_letter`` is lowercase (shaft) or uppercase (hole).
    ``grade_str`` is e.g. "7" or "IT7".

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
            upper_um = it_um / 2
            lower_um = -it_um / 2
            source_kind = "closed_form"
        else:
            grade = int(grade_str.removeprefix("IT"))
            try:
                deviation, side = _hole_deviation_um(letter, grade, nominal_mm, row)
            except FitError as e:
                return _fail(e.reason, e.hint)
            if side == "lower":
                lower_um, upper_um = deviation, deviation + it_um
            else:
                upper_um, lower_um = deviation, deviation - it_um
            source_kind = "table"
    else:
        letter = deviation_letter.lower()
        if letter == "js":
            upper_um = it_um / 2
            lower_um = -it_um / 2
            source_kind = "closed_form"
        else:
            deviation, side = _shaft_deviation_um(letter, int(grade_str.removeprefix("IT")),
                                                  nominal_mm, row)
            if side == "upper":
                upper_um, lower_um = deviation, deviation - it_um
            else:
                lower_um, upper_um = deviation, deviation + it_um
            source_kind = "table"

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
