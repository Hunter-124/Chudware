"""Manufacturing-process profiles + rule-based process resolver (FDM-default).

This module is the single owner of the DfM "fit brain" described in
``docs/INTAKE_MEASUREMENT_DFM_PLAN.md`` section 5. Both surfaces — the MCP ``cad_tools``
and the in-app C++ agent (via the ``core_bridge`` seam) — call it, so the process numbers,
the ambiguity gate, and the clearance math are identical on every path. It replaces the
hard-coded prose anchors in ``agent/prompts/planner.md`` ("slip fit 0.1-0.3, press fit
~0.05") and the literal minima baked into ``wall_thickness`` / ``draft_analysis``.

Primary manufacturing is **FDM 3D printing**, so FDM is the default prior everywhere and the
loaded numbers are the printable ones (a 0.05 mm press interference, the old planner anchor,
is unprintable — that was a real bug). Other processes are first-class but only chosen when a
cue clearly outscores FDM; otherwise we record an FDM assumption and proceed rather than
interrogate the user.

Five public entry points:

* :func:`get_profile` — a fresh copy of a process profile, with optional per-printer overrides.
* :func:`resolve_process` — the rule-based resolver + ambiguity gate.
* :func:`fit_clearance` — the diametral clearance for one fit class on a process.
* :func:`modeled_dim` — the stacked, build-ready dimension for a mating feature.
* :func:`process_block_text` — the human-readable PROCESS BLOCK appended to the planner prompt.

Stdlib-only; no engine/OCP imports so it is safe to use from any layer.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Optional


# --------------------------------------------------------------------------- profile model

@dataclass(frozen=True)
class ProcessProfile:
    """One manufacturing process and its DfM parameters.

    Frozen so the shared :data:`PROFILES` table can never be mutated in place; callers always
    go through :func:`get_profile`, which hands back a plain ``dict`` *copy* (the JSON shape the
    plan persists into the ``.step+`` doc — see the ProcessProfile schema in section 2).

    Field reference (all lengths mm, angles degrees) — see the plan's section-5 table:

    * ``process``           — canonical id (``fdm``/``cnc_mill``/``sheet_metal``/``injection``/
                              ``lathe``/``laser``).
    * ``material_default``  — the default material name + ``density_g_cm3``.
    * ``min_wall_mm``       — thinnest printable/machinable wall.
    * ``min_feature_mm``    — smallest resolvable positive feature (boss/rib/text).
    * ``hole_comp_mm``      — added to a *printed* hole diameter to hit nominal (0 for cnc/laser).
    * ``tolerance_band_mm`` — the general ± band; feeds every clearance.
    * ``fit_classes_mm``    — diametral clearance per fit class {slip, location, press, free}.
    * ``draft``             — {required, min_deg} mold-release draft.
    * ``overhang``          — {max_deg, max_bridge_mm} unsupported-geometry flags (FDM >45deg).
    * ``thread``            — {strategy, printed_ok_min} how threads are made.
    """

    process: str
    material_default: dict[str, Any]
    min_wall_mm: float
    min_feature_mm: float
    hole_comp_mm: float
    tolerance_band_mm: float
    fit_classes_mm: dict[str, Optional[float]]
    draft: dict[str, Any]
    overhang: dict[str, Optional[float]]
    thread: dict[str, Any]

    def to_params(self) -> dict[str, Any]:
        """The ``params`` sub-object exactly as the doc-resident ProcessProfile stores it."""
        return {
            "min_wall_mm": self.min_wall_mm,
            "min_feature_mm": self.min_feature_mm,
            "hole_comp_mm": self.hole_comp_mm,
            "tolerance_band_mm": self.tolerance_band_mm,
            "fit_classes_mm": copy.deepcopy(self.fit_classes_mm),
            "draft": copy.deepcopy(self.draft),
            "overhang": copy.deepcopy(self.overhang),
            "thread": copy.deepcopy(self.thread),
        }


# The load-bearing numbers from the plan's section-5 table. FDM is the default; its values are
# the printable ones the rest of the pipeline trusts. A ``None`` clearance means the fit class is
# not meaningful for that process (e.g. sheet/laser do not get a diametral shaft-in-bore class).
PROFILES: dict[str, ProcessProfile] = {
    # ---- DEFAULT -----------------------------------------------------------------
    "fdm": ProcessProfile(
        process="fdm",
        material_default={"name": "PLA", "density_g_cm3": 1.24},
        min_wall_mm=1.0,
        min_feature_mm=0.8,
        hole_comp_mm=0.20,                # printed holes shrink; oversize the bore by this
        tolerance_band_mm=0.40,
        fit_classes_mm={"slip": 0.40, "location": 0.20, "press": 0.10, "free": 0.50},
        draft={"required": False, "min_deg": 0.0},
        overhang={"max_deg": 45, "max_bridge_mm": 5},
        # FDM press fits are unreliable -> steer to heat-set inserts; small threads not printed.
        thread={"strategy": "heat_set_insert", "printed_ok_min": "M5"},
    ),
    # ---- subtractive -------------------------------------------------------------
    "cnc_mill": ProcessProfile(
        process="cnc_mill",
        material_default={"name": "Aluminium 6061-T6", "density_g_cm3": 2.70},
        min_wall_mm=0.5,
        min_feature_mm=0.5,
        hole_comp_mm=0.0,                 # drilled/reamed to size — no print compensation
        tolerance_band_mm=0.05,
        fit_classes_mm={"slip": 0.05, "location": 0.02, "press": -0.02, "free": 0.10},
        draft={"required": False, "min_deg": 0.0},
        overhang={"max_deg": None, "max_bridge_mm": None},
        thread={"strategy": "tapped", "printed_ok_min": None},
    ),
    "lathe": ProcessProfile(
        process="lathe",
        material_default={"name": "Aluminium 6061-T6", "density_g_cm3": 2.70},
        min_wall_mm=0.5,
        min_feature_mm=0.5,
        hole_comp_mm=0.0,
        tolerance_band_mm=0.05,
        fit_classes_mm={"slip": 0.05, "location": 0.02, "press": -0.02, "free": 0.10},
        draft={"required": False, "min_deg": 0.0},
        overhang={"max_deg": None, "max_bridge_mm": None},
        thread={"strategy": "single_point_or_tapped", "printed_ok_min": None},
    ),
    # ---- formed / cut sheet ------------------------------------------------------
    "sheet_metal": ProcessProfile(
        process="sheet_metal",
        material_default={"name": "Steel (mild)", "density_g_cm3": 7.85},
        min_wall_mm=1.0,                  # "gauge" — the sheet thickness is the wall
        min_feature_mm=1.0,
        hole_comp_mm=0.0,
        tolerance_band_mm=0.10,
        # diametral fit classes do not apply to a flat folded part; holes are punched to size
        fit_classes_mm={"slip": None, "location": None, "press": None, "free": None},
        draft={"required": False, "min_deg": 0.0},
        overhang={"max_deg": None, "max_bridge_mm": None},
        thread={"strategy": "clinch_or_weld_nut", "printed_ok_min": None},
    ),
    "laser": ProcessProfile(
        process="laser",
        material_default={"name": "Acrylic", "density_g_cm3": 1.18},
        min_wall_mm=1.0,
        min_feature_mm=0.5,
        hole_comp_mm=0.0,                 # 2D cut; kerf is the comp, handled per-job
        tolerance_band_mm=0.10,
        fit_classes_mm={"slip": None, "location": None, "press": None, "free": None},
        draft={"required": False, "min_deg": 0.0},
        overhang={"max_deg": None, "max_bridge_mm": None},
        thread={"strategy": "none", "printed_ok_min": None},
    ),
    # ---- molded ------------------------------------------------------------------
    "injection": ProcessProfile(
        process="injection",
        material_default={"name": "ABS", "density_g_cm3": 1.04},
        min_wall_mm=1.0,
        min_feature_mm=0.5,
        hole_comp_mm=0.0,                 # cavity shrink is modeled as a global shrink %, not a bore add
        tolerance_band_mm=0.10,
        fit_classes_mm={"slip": 0.10, "location": 0.05, "press": 0.02, "free": 0.20},
        draft={"required": True, "min_deg": 1.0},     # 1-3deg; release demands draft
        overhang={"max_deg": None, "max_bridge_mm": None},
        thread={"strategy": "molded", "printed_ok_min": None},
    ),
}

#: Public list of every known process id, for error messages and option lists.
KNOWN_PROCESSES: list[str] = list(PROFILES.keys())

#: The default process when nothing in the request pins another one.
DEFAULT_PROCESS: str = "fdm"

#: Per-printer override keys (FDM calibration) that :func:`get_profile` merges into params.
#: Persisted in app config per the plan's "Calibration is a v1 requirement" note.
_OVERRIDE_KEYS: tuple[str, ...] = ("xy_comp", "hole_comp")


def _profile_to_dict(p: ProcessProfile) -> dict[str, Any]:
    """The doc-resident ProcessProfile JSON shape (section 2) for one process — a fresh copy."""
    return {
        "process": p.process,
        "material": copy.deepcopy(p.material_default),
        "params": p.to_params(),
    }


# --------------------------------------------------------------------------- get_profile

def get_profile(process: str, overrides: Optional[dict] = None) -> dict[str, Any]:
    """Return a fresh COPY of a process profile, with optional per-printer overrides merged in.

    The shared :data:`PROFILES` entries are never returned or mutated — callers get a brand-new
    dict each time (same discipline as ``_resolve_material`` in ``cad_tools.py``). The shape is
    the doc-resident ProcessProfile ``{process, material, params}`` so it can be persisted as-is.

    ``overrides`` (FDM per-printer calibration, persisted in app config):

    * ``hole_comp`` -> replaces ``params.hole_comp_mm`` (the printer's measured bore shrink).
    * ``xy_comp``   -> the printer's XY dimensional bias; recorded as ``params.xy_comp_mm`` and
      added on top of the general ``tolerance_band_mm`` so every clearance widens with it.

    Raises ``ValueError`` (listing the known processes) if ``process`` is not recognized.
    """
    key = _norm(process)
    if key not in PROFILES:
        raise ValueError(
            f"Unknown process {process!r}. Known processes: {KNOWN_PROCESSES}."
        )
    prof = _profile_to_dict(PROFILES[key])
    if overrides:
        params = prof["params"]
        if overrides.get("hole_comp") is not None:
            params["hole_comp_mm"] = float(overrides["hole_comp"])
        if overrides.get("xy_comp") is not None:
            xy = float(overrides["xy_comp"])
            params["xy_comp_mm"] = xy
            # widen the general band by the printer's XY bias so clearances absorb it
            params["tolerance_band_mm"] = round(params["tolerance_band_mm"] + xy, 4)
        unknown = set(overrides) - set(_OVERRIDE_KEYS)
        if unknown:
            # tolerate-but-record: stash unrecognized keys so a caller typo is visible, not silent
            params.setdefault("_extra_overrides", {}).update(
                {k: overrides[k] for k in sorted(unknown)}
            )
    return prof


# --------------------------------------------------------------------------- fit math

def fit_clearance(process: str, fit_class: str) -> float:
    """The diametral clearance (mm) to add for one fit class on a process.

    For FDM: slip 0.40, location 0.20, press 0.10, free 0.50 (the plan's load-bearing numbers).
    Raises ``ValueError`` for an unknown process or fit class, or one the process does not define
    (e.g. a diametral fit on flat sheet metal).
    """
    key = _norm(process)
    if key not in PROFILES:
        raise ValueError(f"Unknown process {process!r}. Known: {KNOWN_PROCESSES}.")
    fits = PROFILES[key].fit_classes_mm
    fc = str(fit_class).strip().lower()
    if fc not in fits:
        raise ValueError(
            f"Unknown fit class {fit_class!r}. Known: {sorted(fits)}."
        )
    val = fits[fc]
    if val is None:
        raise ValueError(
            f"Process {key!r} has no diametral {fc!r} fit class "
            f"(it is a flat/sheet process; holes are cut to size)."
        )
    return float(val)


def modeled_dim(nominal_mm: float, fit_class: str, process: str,
                is_hole: bool = False) -> dict[str, Any]:
    """Stack the fit clearance (and hole compensation) onto a nominal mating dimension.

    The modeler builds the *receptacle* so the real mating part fits on the first try, so the
    clearance opens the receptacle up:

    * A shaft passthrough / bore receiving a ``nominal_mm`` shaft, FDM slip:
      ``modeled = nominal + 0.40`` (and, when ``is_hole=True``, plus the printed-hole comp so the
      shrunken printed bore still lands at the intended size).
    * A press class returns a NEGATIVE clearance (interference) on subtractive processes; for FDM
      the press class is a small positive value because reliable interference is not printable
      (the plan steers press fits to heat-set inserts).

    Returns ``{modeled_mm, clearance_mm, hole_comp_mm, rationale}``.
    """
    key = _norm(process)
    clr = fit_clearance(key, fit_class)
    prof = PROFILES[key]
    hole_comp = prof.hole_comp_mm if is_hole else 0.0
    nominal = float(nominal_mm)
    modeled = nominal + clr + hole_comp
    fc = str(fit_class).strip().lower()
    bits = [f"{prof.process} {fc} clearance {clr:+.2f} mm"]
    if is_hole and hole_comp:
        bits.append(f"+ printed-hole comp {hole_comp:.2f} mm")
    rationale = f"{nominal:g} mm nominal, " + ", ".join(bits)
    return {
        "modeled_mm": round(modeled, 4),
        "clearance_mm": clr,
        "hole_comp_mm": hole_comp,
        "rationale": rationale,
    }


# --------------------------------------------------------------------------- resolver

# Explicit process keywords that SHORT-CIRCUIT the resolver (no ambiguity question). Ordered so
# more specific multi-word phrases win before a bare verb. Each maps to a canonical process id.
_EXPLICIT_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("3d print", "fdm"), ("3-d print", "fdm"), ("3d-print", "fdm"),
    ("fdm", "fdm"), ("fff", "fdm"), ("printed", "fdm"), ("print", "fdm"),
    ("injection mold", "injection"), ("injection-mold", "injection"),
    ("injection", "injection"), ("molded", "injection"), ("moulded", "injection"),
    ("mold", "injection"), ("mould", "injection"),
    ("sheet metal", "sheet_metal"), ("sheet-metal", "sheet_metal"),
    ("laser cut", "laser"), ("laser-cut", "laser"), ("laser", "laser"),
    ("cnc", "cnc_mill"), ("machine from", "cnc_mill"), ("machined", "cnc_mill"),
    ("machine", "cnc_mill"), ("mill", "cnc_mill"),
    ("turned", "lathe"), ("turn ", "lathe"), ("lathe", "lathe"),
)

# Materials that pin (or forbid) a process. Metals make FDM impossible (must-block when FDM leads
# but a metal is named) and imply a subtractive/sheet default when no process is stated.
_METAL_WORDS: tuple[str, ...] = (
    "steel", "stainless", "aluminum", "aluminium", "alu", "titanium", "brass",
    "bronze", "copper", "iron", "metal", "carbon steel", "mild steel", "6061",
)
_PLASTIC_WORDS: tuple[str, ...] = (
    "pla", "abs", "petg", "plastic", "nylon", "tpu", "resin", "polycarbonate", "pc",
)

# Rule-based, explainable cue table (plan section 5). Each cue is one or more regex patterns; a
# match adds weight to a candidate process. Weights are tuned so a single strong cue clears FDM
# but a lone soft cue lands "within margin" and fires the ambiguity gate.
_CUES: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    # constant-thickness plate/sheet OR bends/flanges -> sheet metal (a STRONG cue group)
    ("sheet_metal", 1.0, (
        r"\bbend(s|ing)?\b", r"\bflange(s)?\b", r"\bfold(ed|s)?\b",
        r"\bbent\b", r"\bgauge\b",
        r"\b(constant|uniform)[- ]thickness\b",
        # "2mm steel plate" / "2 mm plate" / "3mm sheet" — allow one material word between
        r"\b\d+(\.\d+)?\s?mm\s+(?:\w+\s+){0,2}?(plate|sheet)\b",
    )),
    ("sheet_metal", 0.5, (r"\bbracket\b", r"\benclosure\b", r"\bchassis\b")),
    # threaded-in-metal / tight-tolerance / flatness -> CNC mill
    ("cnc_mill", 1.0, (
        r"\btap(ped|ping)?\b", r"\bthreaded\b", r"\bthread(s)?\b",
        r"\btight[- ]toleranc", r"\bprecision\b", r"\breamed\b",
    )),
    ("cnc_mill", 0.7, (
        r"\bflat(ness)?\b", r"\bmust be flat\b", r"\bsurface finish\b", r"\bpocket\b",
    )),
    # body of revolution / turned features -> lathe
    ("lathe", 1.0, (
        r"\bturn(ed|ing)?\b", r"\blathe\b", r"\bbody of revolution\b",
        r"\bspindle\b", r"\bshaft\b(?=.*\bround\b)",
    )),
    ("lathe", 0.6, (r"\bround\s+(bar|stock|rod)\b", r"\bconcentric\b")),
    # draft / high qty / molded -> injection
    ("injection", 1.0, (r"\bdraft\b", r"\bmolded\b", r"\bmoulded\b", r"\binjection\b")),
    ("injection", 0.8, (
        r"\b(\d{4,})\s*(units|pieces|pcs|off|qty)\b",
        r"\bmass[- ]produc", r"\bhigh[- ]volume\b",
    )),
    # flat profile cut / gasket / panel -> laser
    ("laser", 1.0, (r"\bgasket\b", r"\blaser\b", r"\bkerf\b")),
    ("laser", 0.6, (
        r"\bpanel\b", r"\bflat\s+(profile|cut)\b", r"\bcut\s+from\s+(sheet|acrylic)\b",
    )),
)

# When a non-FDM leader beats FDM by LESS than this, with no explicit material/quantity to
# anchor it, we are "within margin" -> fire the ambiguity gate (plan section 5 rule (a)).
_MARGIN: float = 1.0001


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def _spec_text(request_or_spec: Any) -> str:
    """Flatten a request string OR a normalized-intake-style dict into searchable lowercase text."""
    if isinstance(request_or_spec, dict):
        parts: list[str] = []
        for key in ("object", "request", "summary", "text"):
            v = request_or_spec.get(key)
            if isinstance(v, str):
                parts.append(v)
        for key in ("features", "constraints", "notes"):
            v = request_or_spec.get(key)
            if isinstance(v, (list, tuple)):
                parts.extend(str(x) for x in v)
        return _norm(" ".join(parts))
    return _norm(request_or_spec)


def _explicit_process(text: str) -> Optional[str]:
    """Return the canonical process for the first explicit keyword in ``text``, else None."""
    for kw, proc in _EXPLICIT_KEYWORDS:
        if kw in text:
            return proc
    return None


def _material_is_metal(material: Optional[str], text: str) -> bool:
    m = _norm(material)
    if any(w in m for w in _METAL_WORDS):
        return True
    # also catch a metal named inline in the request when no explicit material arg was passed
    return any(re.search(rf"\b{re.escape(w)}\b", text) for w in _METAL_WORDS)


def _material_is_plastic(material: Optional[str], text: str) -> bool:
    m = _norm(material)
    if any(w in m for w in _PLASTIC_WORDS):
        return True
    return any(re.search(rf"\b{re.escape(w)}\b", text) for w in _PLASTIC_WORDS)


def _score_cues(text: str) -> dict[str, float]:
    """Sum cue weights per process over ``text`` (each cue group counts once)."""
    scores: dict[str, float] = {}
    for proc, weight, patterns in _CUES:
        if any(re.search(p, text) for p in patterns):
            scores[proc] = scores.get(proc, 0.0) + weight
    return scores


# A cue total at/above this counts as a "strong" (non-soft) cue for the tie test.
_STRONG_CUE: float = 1.0


def _strong_tie(scores: dict[str, float]) -> Optional[tuple[str, str]]:
    """Two distinct processes each carrying a STRONG cue -> the ambiguous pair (rule (b)).

    The plan's worked contrast ("constant 2mm -> sheet; threaded+flat -> CNC") wants the gate to
    fire whenever two non-FDM processes are each strongly cued, even if a soft cue tips the raw
    sum slightly. So we compare at strong-cue granularity, returning the two highest-scored
    strong candidates (CNC preferred as the leader when tied on score).
    """
    strong = sorted(((p, s) for p, s in scores.items() if s >= _STRONG_CUE),
                    key=lambda kv: (kv[1], kv[0] != "cnc_mill"), reverse=False)
    strong.sort(key=lambda kv: kv[1], reverse=True)
    if len(strong) < 2:
        return None
    # leader = highest score; on a score tie prefer cnc_mill as the conventional metal default
    top = strong[0]
    second = strong[1]
    if abs(top[1] - second[1]) < 1e-9 and second[0] == "cnc_mill":
        top, second = second, top
    return top[0], second[0]


def _quantity_from(text: str, quantity: Optional[int]) -> Optional[int]:
    if quantity is not None:
        try:
            return int(quantity)
        except (TypeError, ValueError):
            return None
    m = re.search(r"\b(\d{2,})\s*(units|pieces|pcs|off|qty|quantity)\b", text)
    return int(m.group(1)) if m else None


def resolve_process(request_or_spec: Any, material: Optional[str] = None,
                    quantity: Optional[int] = None) -> dict[str, Any]:
    """Resolve the manufacturing process from a request, with the FDM-default ambiguity gate.

    Implements the plan's section-5 rules:

    * **Prior = FDM.** Bias to NOT asking — the user's primary method.
    * **Short-circuit (no question):** an explicit process keyword ("print", "machine from
      aluminum", "laser cut") or an explicit metal material that pins a non-FDM process.
    * **Gate FIRES (``ambiguous=True``, one clarification):**
        (a) a non-FDM cue scores *within margin* of FDM with no explicit material/quantity;
        (b) two non-FDM cues tie (sheet-metal vs CNC);
        (c) material is impossible for the leading process (metal+FDM) -> ``must_block=True``.

    Returns a dict with the documented keys::

        {chosen, confidence, runner_up, ambiguous, must_block,
         clarification | None, assumptions: [...], why}

    where ``clarification`` (present only when ``ambiguous``) is ``{ask, suggested, options}``.
    """
    text = _spec_text(request_or_spec)
    qty = _quantity_from(text, quantity)
    has_explicit_qty = quantity is not None or qty is not None
    is_metal = _material_is_metal(material, text)
    is_plastic = _material_is_plastic(material, text)
    has_explicit_material = bool(_norm(material)) or is_metal or is_plastic

    # ---- 1. explicit process keyword short-circuits everything --------------------
    explicit = _explicit_process(text)
    if explicit is not None:
        assumptions: list[str] = []
        must_block = False
        clar = None
        # explicit FDM but a metal material is impossible -> must-block (rule (c))
        if explicit == "fdm" and is_metal:
            must_block = True
            clar = {
                "ask": ("You asked to 3D-print this, but named a metal material. FDM cannot "
                        "print metal. Switch to a metal process (CNC mill / sheet metal), or "
                        "keep FDM with a plastic?"),
                "suggested": "cnc_mill",
                "options": ["cnc_mill", "sheet_metal", "fdm"],
            }
            return _resolution("fdm", 0.99, _runner_up_after("fdm", {}), True, True,
                               clar, assumptions,
                               "explicit 3D-print cue but metal material is impossible for FDM")
        return _resolution(explicit, 0.95, None, False, False, None, assumptions,
                           f"explicit '{explicit}' process cue in the request")

    # ---- 2. explicit metal material pins a non-FDM (metal) process ----------------
    cue_scores = _score_cues(text)
    if is_metal:
        # A metal anchor removes FDM from contention but does NOT by itself pick WHICH metal
        # process. Restrict to the metal-capable cues; default CNC (the general metal default).
        metal_scores = {p: s for p, s in cue_scores.items()
                        if p in ("cnc_mill", "sheet_metal", "lathe")}
        # Two strong metal cues (e.g. constant 2mm plate -> sheet vs threaded+flat -> CNC): the
        # metal anchor settled "not FDM" but WHICH metal process is still ambiguous -> rule (b).
        pair = _strong_tie(metal_scores)
        if pair is not None:
            lead, other = pair
            return _resolution(
                lead, 0.5, other, True, False,
                _two_way_clarification(lead, other),
                ["metal material -> not FDM, but two strong metal cues compete"],
                f"metal pins a metal process but '{lead}' and '{other}' cues compete; asking which",
            )
        if metal_scores:
            chosen = max(metal_scores, key=metal_scores.get)
            runner = _runner_up_after(chosen, metal_scores)
        else:
            chosen, runner = "cnc_mill", None
        return _resolution(
            chosen, 0.9, runner, False, False, None,
            [f"metal material -> {chosen} (FDM impossible)"],
            "explicit metal material pins a metal process",
        )

    # ---- 3. score cues against the FDM prior --------------------------------------
    # FDM starts with a baseline prior; a plastic material reinforces it.
    fdm_prior = 0.8 + (0.4 if is_plastic else 0.0)
    scores: dict[str, float] = {DEFAULT_PROCESS: fdm_prior}
    for proc, s in cue_scores.items():
        scores[proc] = scores.get(proc, 0.0) + s
    # qty>=1000 is itself an injection cue even without the word "molded"
    if qty is not None and qty >= 1000:
        scores["injection"] = scores.get("injection", 0.0) + 1.0

    non_fdm = {p: s for p, s in scores.items() if p != DEFAULT_PROCESS}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    leader, lead_score = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else None

    # ---- 3a. two strong non-FDM cues compete (rule (b)) --------------------------
    pair = _strong_tie(non_fdm)
    if pair is not None:
        a, b = pair
        return _resolution(
            a, 0.5, b, True, False,
            _two_way_clarification(a, b), ["process ambiguous: two strong non-FDM cues compete"],
            f"two non-FDM cues compete ({a} vs {b}); asking which",
        )

    # ---- 3b. a non-FDM leader within margin of FDM, no anchor (rule (a)) ---------
    if leader != DEFAULT_PROCESS:
        fdm_score = scores.get(DEFAULT_PROCESS, 0.0)
        within_margin = (lead_score - fdm_score) < _MARGIN
        if within_margin and not has_explicit_material and not has_explicit_qty:
            return _resolution(
                leader, 0.5, DEFAULT_PROCESS, True, False,
                _two_way_clarification(leader, DEFAULT_PROCESS),
                [f"{leader} cue present but within margin of the FDM default"],
                f"'{leader}' cue scores within margin of the FDM default with no material/qty anchor",
            )
        # leader clears FDM decisively -> pick it, record runner-up, no question
        conf = min(0.9, 0.6 + (lead_score - fdm_score) * 0.15)
        return _resolution(
            leader, round(conf, 2), runner_up, False, False, None,
            [f"{leader} selected over the FDM default by cue margin"],
            f"'{leader}' cues outscore the FDM default",
        )

    # ---- 4. FDM wins on the prior (the common, no-ask path) -----------------------
    conf = 0.86 if not is_plastic else 0.9
    why = ("explicit plastic material with no non-FDM cues" if is_plastic
           else "no non-FDM cues; FDM is the default prior")
    return _resolution(
        DEFAULT_PROCESS, conf, runner_up if runner_up != DEFAULT_PROCESS else None,
        False, False, None, ["process FDM/PLA assumed (default)"], why,
    )


def _resolution(chosen: str, confidence: float, runner_up: Optional[str],
                ambiguous: bool, must_block: bool, clarification: Optional[dict],
                assumptions: list[str], why: str) -> dict[str, Any]:
    """Assemble the documented resolution dict (single construction point)."""
    return {
        "chosen": chosen,
        "confidence": round(float(confidence), 3),
        "runner_up": runner_up,
        "ambiguous": bool(ambiguous),
        "must_block": bool(must_block),
        "clarification": clarification,
        "assumptions": list(assumptions),
        "why": why,
    }


def _runner_up_after(chosen: str, scores: dict[str, float]) -> Optional[str]:
    others = [p for p in scores if p != chosen]
    if not others:
        return None
    return max(others, key=scores.get)


_PRETTY = {
    "fdm": "FDM 3D print", "cnc_mill": "CNC from plate/billet", "sheet_metal": "sheet metal (laser+bend)",
    "injection": "injection mold", "lathe": "lathe (turned)", "laser": "laser cut",
}


def _two_way_clarification(a: str, b: str) -> dict[str, Any]:
    """The single clarification question for an A-vs-B process ambiguity (suggested = A)."""
    return {
        "ask": (f"Make this by {_PRETTY.get(a, a)} or {_PRETTY.get(b, b)}? "
                f"[suggested: {_PRETTY.get(a, a)}]"),
        "suggested": a,
        "options": [a, b],
    }


# --------------------------------------------------------------------------- PROCESS BLOCK

def process_block_text(profile: dict[str, Any]) -> str:
    """Render the human-readable PROCESS BLOCK appended to the planner prompt.

    Terse, in the style of ``ops_cheatsheet.md`` — gives the planner the min wall, the fit
    table, the thread strategy, and the overhang rule for the bound process so it commits
    *printable* clearances (replacing the old hard-coded "slip 0.1-0.3, press ~0.05" anchors).

    ``profile`` is a doc-resident ProcessProfile dict (``get_profile`` output, possibly with
    overrides + ``material`` / ``assumed`` fields layered on).
    """
    params = profile.get("params", profile)
    process = profile.get("process", "fdm")
    mat = profile.get("material") or {}
    mat_name = mat.get("name", "")
    pretty = _PRETTY.get(process, process)

    lines: list[str] = []
    head = f"PROCESS BLOCK — manufacturing = {pretty.upper()}"
    if mat_name:
        head += f" / {mat_name}"
    if profile.get("assumed"):
        head += "  (assumed default — confirm if wrong)"
    lines.append(head)
    lines.append("Commit dimensions that PRINT/MACHINE on the first try, not textbook anchors.")

    mw = params.get("min_wall_mm")
    mf = params.get("min_feature_mm")
    if mw is not None:
        lines.append(f"  - Min wall {mw:g} mm; smallest feature {mf:g} mm. Walls thinner than "
                     f"this warp or fail to form.")

    fits = params.get("fit_classes_mm", {})
    fit_bits = [f"{name} {v:+.2f}" for name in ("slip", "location", "press", "free")
                if (v := fits.get(name)) is not None]
    if fit_bits:
        lines.append("  - Diametral fit clearance to ADD to the receptacle (mm): "
                     + ", ".join(fit_bits) + ".")
        lines.append("    e.g. a slip-fit bore for a Dx shaft is modeled Ø(x"
                     f"{_signed(fits.get('slip'))}); pick the class by how it must mate.")

    hc = params.get("hole_comp_mm")
    if hc:
        lines.append(f"  - Printed holes shrink: oversize every printed bore diameter by "
                     f"{hc:g} mm to land on nominal.")

    thread = params.get("thread", {})
    strat = thread.get("strategy")
    if strat:
        ok_min = thread.get("printed_ok_min")
        note = f" (printed threads only ≥ {ok_min})" if ok_min else ""
        lines.append(f"  - Threads: {strat.replace('_', ' ')}{note}.")

    draft = params.get("draft", {})
    if draft.get("required"):
        lines.append(f"  - Draft REQUIRED ≥ {draft.get('min_deg', 0):g}° on every face along "
                     f"the pull, or it won't release.")

    over = params.get("overhang", {})
    if over.get("max_deg") is not None:
        bridge = over.get("max_bridge_mm")
        b = f"; bridges over {bridge:g} mm need support" if bridge else ""
        lines.append(f"  - Overhangs steeper than {over['max_deg']:g}° from vertical need "
                     f"support or a chamfer{b}.")

    return "\n".join(lines)


def _signed(v: Optional[float]) -> str:
    return "" if v is None else f"{v:+.2f}"
