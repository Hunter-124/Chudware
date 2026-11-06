"""Conductor and ampacity data for wire-harness electrical checks.

Every number here comes from a cited standard table or from a closed-form
definition in one. Nothing is invented: a value that no cited table gives is
refused (raises :class:`NotTabulated`) so the caller can say what to supply
instead of guessing.

Sources
-------
* **AWG diameters / areas** - ASTM B258-18 "Standard Specification for Standard
  Nominal Diameters and Cross-Sectional Areas of AWG Sizes of Solid Round Wires
  Used as Electrical Conductors": ``d_n = 0.005 in * 92**((36 - n) / 39)`` with
  ``n = 0, -1, -2, -3`` for 1/0, 2/0, 3/0, 4/0.
* **Copper resistivity at 20 C** - ASTM B258 / ASTM B3 / IEC 60028 annealed copper,
  100 % IACS: ``0.017241 ohm*mm^2/m`` (= 10.371 ohm*cmil/ft).
* **Temperature coefficient** - ``alpha_20 = 0.00393 /C`` for 100 % IACS copper
  (ASTM B193 / IEC 60028; IEC 60228 Annex A uses the same coefficient).
* **Metric conductors** - IEC 60228:2004 Table 2 (classes 1 and 2) and Table 3
  (class 5, flexible): maximum DC resistance of a plain (untinned) copper
  conductor at 20 C, ohm/km.
* **Ampacity** - NFPA 70 (NEC) Table 310.16 (copper, 60/75/90 C columns, not more
  than three current-carrying conductors in a raceway, cable or direct-buried,
  30 C ambient). SCOPE: general building wiring, 14 AWG and larger. The table does
  not exist below 14 AWG or above 90 C insulation. Supply an applicable conductor
  datasheet rating; the thermal model is not a qualified ampacity.
* **Ambient correction** - NEC Table 310.15(B)(2)(a), reference ambient 30 C.
* **Bundle adjustment** - NEC Table 310.15(C)(1), more than three current-carrying
  conductors in a raceway or cable.
* **Spaceflight wire derating** - NASA MSFC-STD-3012A Appendix A, pp. 127-128,
  https://nepp.nasa.gov/files/26094/MSFC-STD-3012A.pdf. The single-wire table
  and helical bundle equation apply only under the stated spaceflight conditions;
  they do not establish chassis wiring compliance.
"""
from __future__ import annotations

import math
from typing import Any, Optional

# --------------------------------------------------------------------------- constants

#: Annealed copper resistivity at 20 C, ohm*mm^2/m (ASTM B258 / B3 / IEC 60028, 100 % IACS).
RHO_CU_20_OHM_MM2_PER_M = 0.017241
#: Temperature coefficient of resistance of 100 % IACS copper referred to 20 C, 1/C
#: (ASTM B193 / IEC 60028).
ALPHA_CU_20_PER_C = 0.00393

#: Smallest/largest AWG number supported (4/0 is -3 by definition; ASTM B258 tabulates to 40).
AWG_MIN = -3
AWG_MAX = 40


class NotTabulated(ValueError):
    """A required value is not available from a cited table; ``args[0]`` says what to supply."""


# --------------------------------------------------------------------------- insulation
#
# These are screening references, not specifications for an unidentified wire.
# Surface emissivity in particular depends on color, surface, wavelength and
# temperature; an IR camera value is only an approximation to total emissivity.
_INSULATION_WIRE_CHART = "https://www.paragonmfgcorp.com/tech-library/wire-insulation-chart/"
_INSULATION_FLUORO = "https://midlandplastics.com/download_file/force/83/213"
_INSULATION_EMISSIVITY = "https://www.testo.com/en-US/know-how/thermal-imaging-camera/emissivity-table"
_INSULATION_PVC_K = "https://www.matec-conferences.org/articles/matecconf/pdf/2018/34/matecconf_ifcae-iot2018_03014.pdf"
_INSULATION_XLPE = "https://www.chemours.com/en/-/media/files/teflon/tefzel-750-appliance-wire-product-info.pdf"
_INSULATION_CABLE_K = "https://www.cableizer.com/documentation/rho_i/"
_INSULATION_CABLE_E = "https://www.cableizer.com/documentation/epsilon_e/"
_INSULATION_PTFE_E = "https://www.engineeringtoolbox.com/emissivity-coefficients-d_447.html"
_INSULATION_TPU_K = "https://analyzing-testing.netzsch.com/en-US/polymers-netzsch-com/thermoplastic-elastomers/tpu-thermoplastic-urethane-elastomer"

# Conductivity W/(m K), emissivity, example wire temperature rating C,
# followed by the source for each quantity. None means not established.
_INSULATION_PROPERTIES = {
    "PVC": (0.16, 0.94, 80.0, _INSULATION_PVC_K, _INSULATION_EMISSIVITY, _INSULATION_WIRE_CHART),
    "PTFE": (0.25, 0.92, 260.0, _INSULATION_FLUORO, _INSULATION_PTFE_E, _INSULATION_WIRE_CHART),
    "FEP": (0.25, 0.9, 200.0, _INSULATION_FLUORO, _INSULATION_CABLE_E, _INSULATION_WIRE_CHART),
    "ETFE": (0.23, 0.9, 150.0, _INSULATION_FLUORO, _INSULATION_CABLE_E, _INSULATION_WIRE_CHART),
    "XLPE": (1.0 / 3.5, 0.92, 125.0, _INSULATION_CABLE_K, _INSULATION_CABLE_E, _INSULATION_XLPE),
    "silicone": (0.2, 0.9, 150.0, _INSULATION_CABLE_K, _INSULATION_CABLE_E, _INSULATION_WIRE_CHART),
    "TPE": (0.19, 0.9, 90.0, _INSULATION_TPU_K, _INSULATION_CABLE_E, _INSULATION_WIRE_CHART),
}
_INSULATION_ALIASES = {
    "PVC": "PVC", "POLYVINYL CHLORIDE": "PVC",
    "PTFE": "PTFE", "TFE": "PTFE", "POLYTETRAFLUOROETHYLENE": "PTFE",
    "FEP": "FEP", "ETFE": "ETFE", "TEFZEL": "ETFE",
    "XLPE": "XLPE", "CROSS LINKED POLYETHYLENE": "XLPE",
    "CROSSLINKED POLYETHYLENE": "XLPE", "IRRADIATED PE": "XLPE",
    "IRRADIATED POLYETHYLENE": "XLPE",
    "SILICONE": "silicone", "SILICONE RUBBER": "silicone",
    "TPE": "TPE", "THERMOPLASTIC ELASTOMER": "TPE",
}


def resolve_insulation(insulation: str) -> dict:
    """Return cited screening properties; absent properties remain explicitly unknown.

    Temperature ratings are examples, not ratings of the actual wire. Use its
    manufacturer's rating and measured/datasheet properties whenever available.
    Ambiguous names such as 'Teflon' do not identify a single polymer.
    """
    if not isinstance(insulation, str):
        raise ValueError("insulation must identify a material")
    key = " ".join(insulation.upper().replace("-", " ").replace("_", " ").split())
    material = _INSULATION_ALIASES.get(key)
    if material is None:
        raise ValueError(f"unknown or ambiguous insulation {insulation!r}; specify material properties")
    conductivity, emissivity, rating, k_source, e_source, rating_source = _INSULATION_PROPERTIES[material]
    assumptions = [
        "Screening values are examples, not specifications of this wire; formulation and construction vary.",
        "The temperature rating is an example only; use the actual wire's continuous rating.",
    ]
    if material == "XLPE":
        assumptions.append("Chemours comparison Table 1 lists 125 C and 150 C XLPE wire examples; conductivity is reciprocal of 3.5 K m/W thermal resistivity.")
    if material == "silicone":
        assumptions.append("Conductivity is reciprocal of the cited 5.0 K m/W silicone rubber thermal resistivity.")
    if material == "TPE":
        assumptions.append("Conductivity is a TPU example used as a low-confidence TPE screening assumption, not a general TPE measurement; use the actual formulation's value.")
    if material in ("FEP", "ETFE", "TPE"):
        assumptions.append("Emissivity 0.9 is a generic cable-surface screening assumption, not a measurement of this polymer; actual emissivity can differ.")
    if emissivity is not None:
        assumptions.append("Emissivity is an infrared reference approximated as total emissivity; surface and temperature affect it.")
    if conductivity is not None:
        assumptions.append("Thermal conductivity is a published representative material value, not a measurement of the actual insulation.")
    return {
        "material": material,
        "thermal_conductivity_w_mk": conductivity,
        "emissivity": emissivity,
        "max_temperature_c": rating,
        "sources": {
            "thermal_conductivity_w_mk": k_source,
            "emissivity": e_source,
            "max_temperature_c": rating_source,
        },
        "assumptions": assumptions,
    }


# --------------------------------------------------------------------------- AWG

def parse_awg(value: Any) -> int:
    """Parse ``18``, ``"18"``, ``"18 AWG"``, ``"4/0"``, ``"0000"`` ... into the AWG number.

    4/0 -> -3, 3/0 -> -2, 2/0 -> -1, 1/0 -> 0 (the ASTM B258 convention).
    """
    if isinstance(value, bool):
        raise NotTabulated(f"gauge_awg {value!r} is not an AWG size")
    if isinstance(value, (int, float)):
        if float(value) != int(value):
            raise NotTabulated(f"gauge_awg {value!r} is not a whole AWG size")
        n = int(value)
    else:
        text = str(value).strip().upper().replace("AWG", "").strip()
        zeros = {"0000": -3, "000": -2, "00": -1, "4/0": -3, "3/0": -2, "2/0": -1, "1/0": 0}
        if text in zeros:
            n = zeros[text]
        else:
            try:
                n = int(text)
            except ValueError as exc:
                raise NotTabulated(f"gauge_awg {value!r} is not an AWG size") from exc
    if not AWG_MIN <= n <= AWG_MAX:
        raise NotTabulated(f"AWG {n} outside the ASTM B258 range {AWG_MIN}..{AWG_MAX}")
    return n


def awg_label(n: int) -> str:
    return {-3: "4/0", -2: "3/0", -1: "2/0", 0: "1/0"}.get(n, str(n))


def awg_diameter_mm(n: int) -> float:
    """ASTM B258 nominal solid-conductor diameter, mm."""
    return 0.127 * 92.0 ** ((36 - n) / 39.0)


def awg_area_mm2(n: int) -> float:
    return math.pi / 4.0 * awg_diameter_mm(n) ** 2


def awg_r20_ohm_per_km(n: int) -> float:
    """Nominal DC resistance of a solid annealed-copper AWG conductor at 20 C, ohm/km."""
    return RHO_CU_20_OHM_MM2_PER_M / awg_area_mm2(n) * 1000.0


# --------------------------------------------------------------------------- metric (IEC 60228)

#: IEC 60228:2004 maximum conductor DC resistance at 20 C, plain copper, ohm/km.
#: Keys are nominal cross sections in mm^2. Class "1_2" = Table 2 (solid / stranded
#: classes 1 and 2, identical values); class "5" = Table 3 (flexible).
IEC_60228_R20_OHM_PER_KM: dict[str, dict[float, float]] = {
    "1_2": {0.5: 36.0, 0.75: 24.5, 1.0: 18.1, 1.5: 12.1, 2.5: 7.41, 4.0: 4.61, 6.0: 3.08,
            10.0: 1.83, 16.0: 1.15, 25.0: 0.727, 35.0: 0.524, 50.0: 0.387, 70.0: 0.268,
            95.0: 0.193},
    "5": {0.5: 39.0, 0.75: 26.0, 1.0: 19.5, 1.5: 13.3, 2.5: 7.98, 4.0: 4.95, 6.0: 3.30,
          10.0: 1.91, 16.0: 1.21, 25.0: 0.780, 35.0: 0.554, 50.0: 0.386, 70.0: 0.272,
          95.0: 0.206},
}

#: Preferred IEC 60228 nominal sections (mm^2) used when stepping a metric wire up a size.
IEC_STANDARD_CSA_MM2 = tuple(sorted(IEC_60228_R20_OHM_PER_KM["5"]))


def metric_diameter_mm(csa_mm2: float) -> float:
    """Diameter of the equivalent SOLID round conductor of that area, mm (not the stranded OD)."""
    return math.sqrt(4.0 * csa_mm2 / math.pi)


def next_larger_awg(n: int) -> Optional[int]:
    """Next thicker AWG size (smaller number), or None at 4/0."""
    return n - 1 if n > AWG_MIN else None


def next_larger_csa(csa_mm2: float) -> Optional[float]:
    for std in IEC_STANDARD_CSA_MM2:
        if std > csa_mm2 + 1e-9:
            return std
    return None


# --------------------------------------------------------------------------- conductor resolution

def resolve_conductor(gauge_awg: Any = None, csa_mm2: Optional[float] = None,
                      conductor_class: Optional[str] = None,
                      r20_ohm_per_km: Optional[float] = None,
                      stranded: Optional[bool] = None) -> dict:
    """Resolve a wire's copper conductor into area, diameter and 20 C resistance.

    Exactly one of ``gauge_awg`` / ``csa_mm2`` is required. ``r20_ohm_per_km`` (a
    datasheet value) overrides the table. Returns ``{kind, label, awg?, csa_mm2,
    diameter_mm, r20_ohm_per_km, basis, confidence, notes[]}``; raises
    :class:`NotTabulated` when the size cannot be resolved honestly.
    """
    notes: list[str] = []
    if gauge_awg not in (None, "") and csa_mm2 not in (None, 0):
        raise NotTabulated("wire carries both gauge_awg and csa_mm2; keep exactly one")
    if gauge_awg in (None, "") and csa_mm2 in (None, 0):
        raise NotTabulated("wire has neither gauge_awg nor csa_mm2; the conductor size is unknown")
    if gauge_awg not in (None, ""):
        n = parse_awg(gauge_awg)
        out: dict[str, Any] = {"kind": "awg", "label": f"{awg_label(n)} AWG", "awg": n,
                               "csa_mm2": awg_area_mm2(n), "diameter_mm": awg_diameter_mm(n)}
        out["r20_ohm_per_km"] = awg_r20_ohm_per_km(n)
        out["basis"] = ("ASTM B258 nominal solid diameter with annealed-copper resistivity "
                        f"{RHO_CU_20_OHM_MM2_PER_M} ohm*mm^2/m at 20 C (solid-equivalent)")
        out["confidence"] = "high" if not stranded else "medium"
        if stranded:
            notes.append("stranded conductors read higher than the solid-equivalent resistance "
                         "(lay increment, ASTM B8); supply the datasheet r20_ohm_per_km for the "
                         "exact value")
    else:
        csa = float(csa_mm2)
        if not csa > 0 or not math.isfinite(csa):
            raise NotTabulated(f"csa_mm2 {csa_mm2!r} must be a positive finite number")
        cls = conductor_class or "5"
        if cls not in IEC_60228_R20_OHM_PER_KM:
            raise NotTabulated(f"conductor_class {cls!r} unknown; use '1_2' (solid/stranded) or '5' (flexible)")
        table = IEC_60228_R20_OHM_PER_KM[cls]
        out = {"kind": "metric", "label": f"{csa:g} mm2", "csa_mm2": csa,
               "diameter_mm": metric_diameter_mm(csa)}
        row = next((r for a, r in table.items() if abs(a - csa) < 1e-9), None)
        if row is not None:
            out["r20_ohm_per_km"] = row
            out["basis"] = (f"IEC 60228:2004 {'Table 2' if cls == '1_2' else 'Table 3'} class "
                            f"{cls.replace('_', '/')} max DC resistance at 20 C")
            out["confidence"] = "high"
        elif r20_ohm_per_km is None:
            raise NotTabulated(f"{csa:g} mm2 has no IEC 60228 row: supply the conductor's "
                               "datasheet r20_ohm_per_km")
    if r20_ohm_per_km is not None:
        r = float(r20_ohm_per_km)
        if not r > 0 or not math.isfinite(r):
            raise NotTabulated(f"r20_ohm_per_km {r20_ohm_per_km!r} must be positive and finite")
        out["r20_ohm_per_km"] = r
        out["basis"] = "datasheet value supplied by the caller (ohm/km at 20 C)"
        out["confidence"] = "high"
        notes = []
    out["notes"] = notes
    return out


def resistance_at(r20_ohm: float, temp_c: float) -> float:
    """Copper resistance at ``temp_c`` from the 20 C value (linear alpha_20 model)."""
    return r20_ohm * (1.0 + ALPHA_CU_20_PER_C * (temp_c - 20.0))


# --------------------------------------------------------------------------- NEC ampacity

NEC_SCOPE = ("NFPA 70 (NEC) Table 310.16: copper, 14 AWG to 4/0, insulation rated 60/75/90 C, "
             "not more than three current-carrying conductors in a raceway or cable, 30 C ambient. "
             "General building-wiring scope; applicability to a particular chassis or harness "
             "is not established by this screening.")

#: NEC Table 310.16 copper allowable ampacity (A): AWG -> {insulation rating C: amps}.
#: Keys are AWG numbers (-3 = 4/0).
NEC_310_16_COPPER_A: dict[int, dict[int, int]] = {
    14: {60: 15, 75: 20, 90: 25},
    12: {60: 20, 75: 25, 90: 30},
    10: {60: 30, 75: 35, 90: 40},
    8: {60: 40, 75: 50, 90: 55},
    6: {60: 55, 75: 65, 90: 75},
    4: {60: 70, 75: 85, 90: 95},
    3: {60: 85, 75: 100, 90: 115},
    2: {60: 95, 75: 115, 90: 130},
    1: {60: 110, 75: 130, 90: 145},
    0: {60: 125, 75: 150, 90: 170},
    -1: {60: 145, 75: 175, 90: 195},
    -2: {60: 165, 75: 200, 90: 225},
    -3: {60: 195, 75: 230, 90: 260},
}

#: NEC Table 310.15(B)(2)(a): rows of (ambient upper bound C, factor@60C, @75C, @90C);
#: None = "not permitted" (ambient at or above what the insulation rating allows).
#: Reference ambient 30 C.
NEC_310_15_B2A_ROWS: tuple[tuple[float, Optional[float], Optional[float], Optional[float]], ...] = (
    (10, 1.29, 1.20, 1.15), (15, 1.22, 1.15, 1.12), (20, 1.15, 1.11, 1.08),
    (25, 1.08, 1.05, 1.04), (30, 1.00, 1.00, 1.00), (35, 0.91, 0.94, 0.96),
    (40, 0.82, 0.88, 0.91), (45, 0.71, 0.82, 0.87), (50, 0.58, 0.75, 0.82),
    (55, 0.41, 0.67, 0.76), (60, None, 0.58, 0.71), (65, None, 0.47, 0.65),
    (70, None, 0.33, 0.58), (75, None, None, 0.50), (80, None, None, 0.41),
    (85, None, None, 0.29),
)

#: NEC Table 310.15(C)(1): (max conductor count, percent of the 310.16 ampacity).
NEC_310_15_C1_ROWS: tuple[tuple[int, float], ...] = (
    (3, 1.00), (6, 0.80), (9, 0.70), (20, 0.50), (30, 0.45), (40, 0.40),
)
NEC_310_15_C1_ABOVE_40 = 0.35


def nec_table_ampacity(awg: int, rating_c: float) -> int:
    """NEC 310.16 copper ampacity (A) for ``awg`` at an insulation rating of 60/75/90 C."""
    if awg not in NEC_310_16_COPPER_A:
        raise NotTabulated(
            f"NEC Table 310.16 starts at 14 AWG; {awg_label(awg)} AWG has no row. Supply the "
            "wire's datasheet ampacity or rely on the thermal model")
    col = NEC_310_16_COPPER_A[awg]
    key = int(rating_c) if float(rating_c) == int(rating_c) else None
    if key not in col:
        raise NotTabulated(
            f"NEC Table 310.16 columns are 60/75/90 C; an insulation rating of {rating_c} C is "
            "not tabulated (higher ratings need NEC 310.17 / the wire datasheet)")
    return col[key]


def nec_ambient_factor(rating_c: float, ambient_c: float) -> Optional[float]:
    """NEC 310.15(B)(2)(a) correction factor, or None when the ambient is not permitted."""
    key = int(rating_c) if float(rating_c) == int(rating_c) else None
    idx = {60: 1, 75: 2, 90: 3}.get(key)
    if idx is None:
        raise NotTabulated(
            f"NEC Table 310.15(B)(2)(a) columns are 60/75/90 C; {rating_c} C is not tabulated")
    if ambient_c > NEC_310_15_B2A_ROWS[-1][0]:
        return None
    for row in NEC_310_15_B2A_ROWS:
        if ambient_c <= row[0]:
            return row[idx]
    return None  # unreachable


def nec_ambient_factor_formula(rating_c: float, ambient_c: float) -> Optional[float]:
    """The NEC 310.15(B) ambient correction ``sqrt((Tc - Ta) / (Tc - 30))`` (continuous form)."""
    if ambient_c >= rating_c:
        return None
    return math.sqrt((rating_c - ambient_c) / (rating_c - 30.0))


def nec_adjustment_factor(n_current_carrying: int) -> float:
    """NEC 310.15(C)(1) adjustment for ``n`` current-carrying conductors in one bundle/cable."""
    if n_current_carrying < 1:
        raise NotTabulated("bundle has no current-carrying conductors")
    for upper, factor in NEC_310_15_C1_ROWS:
        if n_current_carrying <= upper:
            return factor
    return NEC_310_15_C1_ABOVE_40


# --------------------------------------------------------------------------- NASA spaceflight wire

NASA_WIRE_SCOPE = (
    "NASA MSFC-STD-3012A Appendix A pp. 127-128: spaceflight single-wire "
    "ratings for 200 C wire, vacuum below 4.3 psia at 94 C ambient or "
    "non-vacuum at least 4.3 psia in an on-orbit cabin at 22 C ambient. "
    "Bundle equation applies to round single conductors in helically wound "
    "bundles and counts all wires. Wires at these non-vacuum currents must "
    "not be accessible to the crew. Not a general chassis wiring approval. "
    "https://nepp.nasa.gov/files/26094/MSFC-STD-3012A.pdf"
)

# Appendix A single-wire current, (vacuum, non-vacuum), A. Only published rows.
NASA_MSFC_3012A_SINGLE_WIRE_A: dict[int, tuple[float, float]] = {
    26: (3.4, 3.8), 24: (4.7, 5.4), 22: (6.5, 7.4),
    20: (8.8, 10.0), 18: (11.6, 13.2), 16: (13.3, 15.0),
    14: (18.0, 20.0), 12: (25.0, 29.0), 10: (34.8, 40.0),
    8: (56.0, 63.0), 6: (80.0, 92.0), 4: (110.0, 120.0),
    2: (150.5, 170.5), 0: (220.5, 260.0),
}


def nasa_single_wire_ampacity(awg: int, rating_c: float, *,
                              pressure_psia: float, ambient_c: float) -> float:
    """NASA Appendix A single-wire current; refuse conditions not in the table.

    The table is for spaceflight, not arbitrary terrestrial free air. For
    vacuum the nominal maximum wire temperature is 147 C, and for cabin
    non-vacuum it is 118 C. Lower-temperature wire uses note 3 factors.
    """
    if awg not in NASA_MSFC_3012A_SINGLE_WIRE_A:
        raise NotTabulated(f"NASA MSFC-STD-3012A has no {awg_label(awg)} AWG row")
    if not math.isfinite(pressure_psia) or pressure_psia < 0:
        raise NotTabulated("NASA wire table requires a finite absolute pressure >= 0 psia")
    vacuum = pressure_psia < 4.3
    expected_ambient = 94 if vacuum else 22
    if not math.isfinite(ambient_c) or ambient_c != expected_ambient:
        raise NotTabulated(
            f"NASA {'vacuum' if vacuum else 'non-vacuum cabin'} table requires "
            f"{expected_ambient} C ambient")
    factors = {200: (1.0, 1.0), 150: (0.65, 0.80),
               135: (0.45, 0.75), 105: (None, 0.65)}
    factor_pair = factors.get(rating_c)
    if factor_pair is None:
        raise NotTabulated("NASA wire table covers 105, 135, 150 and 200 C wire only")
    index = 0 if vacuum else 1
    factor = factor_pair[index]
    if factor is None:
        raise NotTabulated("NASA MSFC-STD-3012A prohibits 105 C wire in vacuum")
    return NASA_MSFC_3012A_SINGLE_WIRE_A[awg][index] * factor


def nasa_bundle_factor(n_wires: int) -> float:
    """NASA Appendix A note 1: total wires in a helically wound bundle.

    The printed inequalities omit N=15, but both formulas give 0.5 there.
    """
    if isinstance(n_wires, bool) or not isinstance(n_wires, int) or n_wires < 1:
        raise NotTabulated("NASA bundle equation requires a positive integer total wire count")
    return (29 - n_wires) / 28 if n_wires <= 15 else 0.5


def nasa_harness_ampacity(awg: int, rating_c: float, *, pressure_psia: float,
                          ambient_c: float, n_wires: int) -> float:
    """Per-wire NASA current limit for the stated single-wire and bundle scope."""
    return nasa_single_wire_ampacity(
        awg, rating_c, pressure_psia=pressure_psia, ambient_c=ambient_c
    ) * nasa_bundle_factor(n_wires)
