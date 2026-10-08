"""Editable jurisdictional criteria, not a claim of universal code compliance.

GLUMRB Recommended Standards for Wastewater Facilities, 2014 §§33.1,33.41,
33.45,34.1; Recommended Standards for Water Works, 2018 §8.2.1. US customary
values are converted exactly (international foot 0.3048 m, inch 0.0254 m).
"""
from copy import deepcopy
import math
from .errors import CivilError

WW = "GLUMRB, Recommended Standards for Wastewater Facilities (2014)"
WATER = "GLUMRB, Recommended Standards for Water Works (2018) §8.2.1"
PSI_TO_HEAD_M = 6894.757293168 / (1000.0 * 9.80665)
# The published table includes through 42 inches. Percent slopes -> dimensionless.
_SLOPES = [(8, .40), (10, .28), (12, .22), (15, .15), (18, .12), (21, .10),
           (24, .08), (27, .067), (30, .058), (33, .052), (36, .046),
           (39, .041), (42, .037)]
_BASE = {
    "gravity_min_velocity_full_ms": 2 * .3048,
    "gravity_max_velocity_ms": 10 * .3048,
    "gravity_min_diameter_sanitary_m": 8 * .0254,
    "gravity_min_slope_by_diameter": [[d*.0254, s/100] for d, s in _SLOPES],
    "gravity_max_depth_ratio": .8,
    "manhole_max_spacing_m": [[15*.0254, 400*.3048], [30*.0254, 500*.3048]],
    "min_cover_m": 1.2,
    "water_min_pressure_m": 20*PSI_TO_HEAD_M,
    "water_normal_min_pressure_m": 35*PSI_TO_HEAD_M,
    "water_max_pressure_m": 100*PSI_TO_HEAD_M,
    "fire_residual_min_m": 20*PSI_TO_HEAD_M,
    "water_max_velocity_ms": 3.0,
    "references": {
        "gravity_min_velocity_full_ms": WW + " §33.41: 2 ft/s, Manning n=0.013; ≥48 in requires 3 ft/s separately",
        "gravity_max_velocity_ms": WW + " §33.45: >10 ft/s requires special protection, a screening threshold not prohibition",
        "gravity_min_diameter_sanitary_m": WW + " §33.1: 8 in nominal (exact conversion; publication also gives rounded 200 mm)",
        "gravity_min_slope_by_diameter": WW + " §33.41 recommended minimum slope table, percent; nominal inch diameters converted exactly",
        "gravity_max_depth_ratio": "practice: 0.8 design depth reserve; not a GLUMRB requirement; confirm local authority",
        "manhole_max_spacing_m": WW + " §34.1: 400 ft for ≤15 in, 500 ft for 18–30 in; no universal limit given above 30 in",
        "min_cover_m": "practice: 1.2 m starting value only; frost/loads/site and jurisdiction govern; §33.2 does not prescribe a numeric cover",
        "water_min_pressure_m": WATER + ": 20 psi minimum under all flow conditions; rho=1000 kg/m3, g=9.80665 m/s2",
        "water_normal_min_pressure_m": WATER + ": minimum working 35 psi; normal working approximately 60–80 psi",
        "water_max_pressure_m": WATER + ": static pressures >100 psi require pressure reduction; screening threshold",
        "fire_residual_min_m": WATER + ": 20 psi under all flow conditions (including fire flow)",
        "water_max_velocity_ms": "practice: 3 m/s screening value; not specified as a universal limit by GLUMRB",
    },
}
PROFILES = {"ten_states_2014": deepcopy(_BASE), "custom": deepcopy(_BASE)}


def resolve(criteria: dict | None = None) -> dict:
    """Resolve profile plus validated overrides into an independent flat limits dict."""
    if criteria is None:
        criteria = {"profile": "ten_states_2014", "overrides": {}}
    if not isinstance(criteria, dict):
        raise CivilError("invalid_criteria")
    unknown = set(criteria) - {"profile", "overrides"}
    if unknown:
        raise CivilError("unknown_criteria_key", keys=sorted(unknown))
    profile = criteria.get("profile", "ten_states_2014")
    if not isinstance(profile, str) or profile not in PROFILES:
        raise CivilError("unknown_criteria_profile", profile=profile)
    overrides = criteria.get("overrides", {})
    if not isinstance(overrides, dict):
        raise CivilError("invalid_criteria")
    unknown = set(overrides) - set(_BASE)
    if unknown:
        raise CivilError("unknown_criteria_key", keys=sorted(unknown))
    result = deepcopy(PROFILES[profile])
    references = overrides.get("references", {})
    if not isinstance(references, dict):
        raise CivilError("invalid_criteria", "references must be an object")
    if set(references) - (set(_BASE) - {"references"}):
        raise CivilError("unknown_criteria_key", keys=sorted(set(references)-set(_BASE)))
    for key, value in overrides.items():
        if key == "references":
            continue
        if key in ("gravity_min_slope_by_diameter", "manhole_max_spacing_m"):
            if not isinstance(value, list) or not value:
                raise CivilError("invalid_criteria", key=key)
            previous = -math.inf
            for row in value:
                if not isinstance(row, (list, tuple)) or len(row) != 2 or not all(_positive(v) for v in row) or row[0] <= previous:
                    raise CivilError("invalid_criteria", "Diameter table must be positive and ascending", key=key)
                previous = row[0]
        elif not _positive(value) or (key == "gravity_max_depth_ratio" and value > 1):
            raise CivilError("invalid_criteria", key=key)
        result[key] = deepcopy(value)
        result["references"][key] = references.get(key, "user override: source/authority not supplied; not the published profile value")
    for key, value in references.items():
        if not isinstance(value, str) or not value.strip():
            raise CivilError("invalid_criteria", key=key)
        result["references"][key] = value
    if result["water_min_pressure_m"] > result["water_max_pressure_m"]:
        raise CivilError("invalid_criteria", "Minimum pressure exceeds maximum")
    return result


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0
