"""Event hydrology and level-pool routing in SI units.

References: NRCS TR-55 (1986), chapters 2 and 3; NEH Part 630 chapter 16
(2007), Table 16-1 and eqs. 16-1/16-2; FHWA HDS-2 (2002), section 6.2.2;
HEC-HMS Technical Reference Manual, Modified Puls. These are planning/design
methods, not a continuous soil-moisture model or dynamic-wave network solver.
Every public calculation returns the civil result envelope. Storm ``t_s`` are
interval boundaries and ``depth_mm`` are interval depths; hydrograph ``t_s`` and
``q_m3s`` are instantaneous ordinates with piecewise-linear interpolation.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy.integrate import quad
from scipy.optimize import brentq
from scipy.special import gammaincinv, gammaln

from .errors import CivilError

_G = 9.80665
_TR55 = "USDA SCS TR-55, Urban Hydrology for Small Watersheds (1986), chapters 2–3"
_NEH16 = "USDA NRCS NEH Part 630, Chapter 16 (March 2007), Table 16-1, Appendix 16A, eqs. 16-1/16-2; https://hydrocad.net/neh/630ch16.pdf"
_PFDS = "NOAA Atlas 14, Precipitation Frequency Data Server; https://hdsc.nws.noaa.gov/pfds/"
_PULS = "USACE HEC-HMS Technical Reference Manual, Modified Puls (storage-indication / trapezoidal continuity)"


def _result(name, reference, *, warnings=None, assumptions=None, **values):
    return {"ok": True, **values, "warnings": list(warnings or []),
            "assumptions": list(assumptions or []),
            "method": {"name": name, "reference": reference}}


def _number(value, field, *, positive=False, nonnegative=False):
    if isinstance(value, bool):
        raise CivilError("invalid_parameter", field=field)
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise CivilError("invalid_parameter", field=field) from exc
    if not math.isfinite(value) or (positive and value <= 0) or (nonnegative and value < 0):
        raise CivilError("invalid_parameter", field=field)
    return value


def _vector(values, field, *, positive=False, nonnegative=False):
    if not isinstance(values, (list, tuple, np.ndarray)) or len(values) == 0:
        raise CivilError("invalid_parameter", field=field)
    return np.array([_number(v, field, positive=positive, nonnegative=nonnegative)
                     for v in values], dtype=float)


def _uniform_grid(duration_s, dt_s):
    duration_s = _number(duration_s, "duration_s", positive=True)
    dt_s = _number(dt_s, "dt_s", positive=True)
    count = round(duration_s / dt_s)
    if count < 1 or not math.isclose(count * dt_s, duration_s, rel_tol=1e-10, abs_tol=1e-8):
        raise CivilError("time_grid_mismatch", "duration_s must be an integer multiple of dt_s")
    return np.arange(count + 1, dtype=float) * dt_s


@lru_cache(maxsize=1)
def _tables():
    return json.loads(Path(__file__).with_name("data").joinpath("hydrology_nrcs_tables.json").read_text())


def rational_peak(c, intensity_mm_h, area_m2):
    """Rational peak Q=C i A/3.6e6 (TR-55/HDS-2); not a volume method."""
    c = _number(c, "c", nonnegative=True)
    if c > 1:
        raise CivilError("invalid_runoff_coefficient", "C must lie between 0 and 1")
    intensity = _number(intensity_mm_h, "intensity_mm_h", nonnegative=True)
    area = _number(area_m2, "area_m2", positive=True)
    return _result("Rational peak runoff", "FHWA HDS-2, Highway Hydrology (2002), section 5.3",
                   flow_m3s=c * intensity * area / 3.6e6,
                   assumptions=["Uniform design intensity over the catchment for at least Tc; C is event-specific; peak only, not runoff volume."])


def _composite(parts, key, low, high):
    if not isinstance(parts, (list, tuple)) or not parts:
        raise CivilError("invalid_composite", "Supply area_m2 and coefficient for every subarea")
    total, weighted = 0.0, 0.0
    for part in parts:
        if not isinstance(part, dict):
            raise CivilError("invalid_composite")
        area = _number(part.get("area_m2"), "area_m2", positive=True)
        value = _number(part.get(key), key)
        if not low <= value <= high or (key == "cn" and value == 0):
            raise CivilError("invalid_composite", field=key)
        total += area
        weighted += area * value
    return total, weighted / total


def composite_c(parts):
    """Area-weighted rational C; parts are {area_m2, c} (HDS-2 §5.3)."""
    area, c = _composite(parts, "c", 0, 1)
    return _result("Area-weighted rational coefficient", "FHWA HDS-2 (2002), section 5.3",
                   c=c, area_m2=area)


def composite_cn(parts):
    """Area-weighted CN; parts are {area_m2, cn} (TR-55 chapter 2)."""
    area, cn = _composite(parts, "cn", 0, 100)
    return _result("Area-weighted curve number", _TR55, cn=cn, area_m2=area,
                   assumptions=["Composite-CN runoff is not the sum of subarea runoff; CN nonlinearity may warrant separate subcatchments."])


def _idf_arrays(table):
    if not isinstance(table, dict):
        raise CivilError("invalid_idf_table")
    durations = _vector(table.get("durations_s"), "durations_s", positive=True)
    periods = _vector(table.get("return_periods_yr"), "return_periods_yr", positive=True)
    if np.any(np.diff(durations) <= 0) or np.any(np.diff(periods) <= 0):
        raise CivilError("invalid_idf_table", "Axes must be strictly increasing")
    rows = table.get("intensities_mm_h")
    if not isinstance(rows, (list, tuple)) or len(rows) != len(durations):
        raise CivilError("invalid_idf_table", "Supply intensities_mm_h with duration rows and return-period columns")
    matrix = []
    for row in rows:
        values = _vector(row, "intensities_mm_h", nonnegative=True)
        if len(values) != len(periods):
            raise CivilError("invalid_idf_table", "Intensity dimensions do not match axes")
        matrix.append(values)
    return durations, periods, np.array(matrix)


def idf_intensity(idf_table, duration_s, return_period_yr):
    """Bilinear interpolation of log intensity on log duration and log ARI.

    Input: {durations_s, return_periods_yr, intensities_mm_h}; rows are durations.
    NOAA Atlas 14 tables are user-supplied, never a location-independent default.
    Extrapolation on either axis raises ``outside_idf_table``. Rounded zero
    intensity estimates are retained by the CSV importer, but interpolation
    requiring such a cell refuses ``nonpositive_idf_intensity``.
    """
    durations, periods, matrix = _idf_arrays(idf_table)
    duration = _number(duration_s, "duration_s", positive=True)
    period = _number(return_period_yr, "return_period_yr", positive=True)
    if not durations[0] <= duration <= durations[-1] or not periods[0] <= period <= periods[-1]:
        raise CivilError("outside_idf_table", duration_s=duration, return_period_yr=period)
    def brackets(axis, value):
        index = int(np.searchsorted(axis, value))
        if index < len(axis) and axis[index] == value:
            return [(index, 1.0)]
        fraction = math.log(value / axis[index - 1]) / math.log(axis[index] / axis[index - 1])
        return [(index - 1, 1 - fraction), (index, fraction)]
    logarithm = 0.0
    for row, row_weight in brackets(durations, duration):
        for column, column_weight in brackets(periods, period):
            if matrix[row, column] <= 0:
                raise CivilError("nonpositive_idf_intensity", "Log-log interpolation cannot use a rounded zero; obtain the NOAA depth export instead",
                                 duration_s=float(durations[row]), return_period_yr=float(periods[column]))
            logarithm += row_weight * column_weight * math.log(matrix[row, column])
    intensity = math.exp(logarithm)
    return _result("Log-log IDF interpolation", _PFDS, intensity_mm_h=intensity,
                   duration_s=duration, return_period_yr=period,
                   assumptions=["Log-log interpolation of user-supplied point estimates; no spatial interpolation or extrapolation."])


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[- ]?\s*(min(?:ute)?s?|h(?:r|our)?s?|d(?:ay)?s?)\s*$", re.I)


def idf_from_csv(text):
    """Parse NOAA PFDS CSV depth/intensity estimates; ignore confidence tables.

    Recognizes duration labels (5-min through 60-day), ARI/year header columns,
    inches or mm and inches/hour or mm/hour in the export's metadata. Unitless
    input, AEP-percent headers, incomplete rows and unknown labels are refused.
    Original metadata is retained as provenance; all intensities become mm/h.
    """
    if not isinstance(text, str) or not text.strip():
        raise CivilError("invalid_idf_csv")
    rows = [[cell.strip().lstrip("\ufeff") for cell in row]
            for row in csv.reader(io.StringIO(text))]
    header_index, periods = None, None
    for i, row in enumerate(rows):
        if len(row) < 2:
            continue
        if row[0].lower().rstrip(":") not in ("", "duration", "duration/ari", "duration (hours)", "by duration for ari (years)"):
            continue
        try:
            parsed = [float(re.sub(r"\s*(?:-?\s*(?:years?|yrs?))\s*$", "", c, flags=re.I))
                      for c in row[1:] if c]
        except ValueError:
            continue
        if parsed and len(parsed) == len(row) - 1:
            header_index, periods = i, parsed
            break
    if header_index is None:
        raise CivilError("invalid_idf_csv", "No duration/ARI header found")
    metadata = "\n".join(",".join(row) for row in rows[:header_index]).lower()
    if "annual exceedance" in metadata or "aep" in metadata:
        raise CivilError("unsupported_idf_frequency", "Supply ARI/return periods in years, not AEP columns")
    is_intensity = bool(re.search(r"intensity|(?:inches?|\bmm\b|millimeters?)\s*(?:/|per\s*)\s*(?:h|hour|hr)", metadata))
    has_inches = bool(re.search(r"\binches?\b|\bin/hr\b|\bin/h\b", metadata))
    has_mm = bool(re.search(r"\bmm\b|millimeters?|millimetres?", metadata))
    if has_inches == has_mm:
        raise CivilError("idf_units_unknown", "Export must explicitly specify inches or millimeters")
    factor = 25.4 if has_inches else 1.0
    durations, intensities, ignored_ci = [], [], False
    for row in rows[header_index + 1:]:
        if not row or not any(row):
            if durations:
                break
            continue
        label = row[0].lower().rstrip(":")
        if "confidence" in label or "upper" in label or "lower" in label:
            ignored_ci = True
            break
        match = _DURATION.fullmatch(label)
        if not match:
            if durations and (label.startswith("note") or label.startswith("source")):
                break
            raise CivilError("invalid_idf_csv", "Unknown duration label", label=row[0])
        amount, unit = float(match[1]), match[2].lower()
        duration = amount * (60 if unit.startswith("min") else 3600 if unit.startswith("h") else 86400)
        if not 300 <= duration <= 60 * 86400:
            raise CivilError("invalid_idf_csv", "PFDS durations must lie between 5 minutes and 60 days")
        if len(row) != len(periods) + 1:
            raise CivilError("invalid_idf_csv", "Incomplete duration row", label=row[0])
        values = []
        for cell in row[1:]:
            if "(" in cell:
                ignored_ci = True
                cell = cell.split("(", 1)[0].strip()
            try:
                value = _number(cell, "precipitation", nonnegative=True)
            except CivilError as exc:
                raise CivilError("invalid_idf_csv", "Missing or nonnumeric precipitation", label=row[0]) from exc
            values.append(value * factor * (1 if is_intensity else 3600 / duration))
        durations.append(duration)
        intensities.append(values)
    table = {"durations_s": durations, "return_periods_yr": periods,
             "intensities_mm_h": intensities}
    _idf_arrays(table)
    warnings = ["confidence_intervals_not_used"] if ignored_ci else []
    if any(value == 0 for row in intensities for value in row):
        warnings.append("rounded_zero_intensities_require_depth_export_for_log_interpolation")
    return _result("NOAA PFDS CSV import", _PFDS, **table, warnings=warnings,
                   provenance={"source": "NOAA PFDS CSV supplied by user", "metadata": text.splitlines()[:header_index],
                               "units_in": ("inches" if has_inches else "mm") + ("/hour" if is_intensity else ""),
                               "conversion": "depth × 3600/duration_s → mm/h" if not is_intensity else "intensity → mm/h"})


def _cn_depth(p_mm, cn, ia_ratio):
    storage = 25400 / cn - 254
    ia = ia_ratio * storage
    if p_mm <= ia:
        return 0.0, storage, ia
    return (p_mm - ia) ** 2 / (p_mm - ia + storage), storage, ia


def scs_runoff_depth(p_mm, cn, ia_ratio=0.2):
    """NRCS CN cumulative runoff, Q=(P-Ia)^2/(P-Ia+S), S=25400/CN-254 mm.

    TR-55 chapter 2 uses Ia/S=0.2; a different explicit ratio is reported, not
    silently converted to a different calibrated CN.
    """
    p = _number(p_mm, "p_mm", nonnegative=True)
    cn = _number(cn, "cn", positive=True)
    ratio = _number(ia_ratio, "ia_ratio", nonnegative=True)
    if cn > 100:
        raise CivilError("invalid_curve_number", "CN must lie in (0, 100]")
    if ratio > 1:
        raise CivilError("invalid_initial_abstraction_ratio")
    runoff, storage, ia = _cn_depth(p, cn, ratio)
    warnings = [] if ratio == 0.2 else ["nonstandard_initial_abstraction_ratio_requires_consistent_cn_calibration"]
    return _result("NRCS curve-number runoff", _TR55, runoff_mm=runoff, storage_mm=storage,
                   initial_abstraction_mm=ia, ia_ratio=ratio, warnings=warnings,
                   assumptions=["Event cumulative rainfall, uniform CN and antecedent moisture represented by the supplied CN; no baseflow."])


def sheet_flow_time(length_m, slope, n, p2_mm):
    """TR-55 eq. 3-3; hours=0.007(n L_ft)^0.8/(P2_in^0.5 S^0.4).

    The 100-ft sheet-flow limit follows updated NRCS NEH 630 chapter 15 (2010),
    rather than the older TR-55 300-ft limit. Exceedance is warned, never clamped.
    """
    length = _number(length_m, "length_m", positive=True)
    slope = _number(slope, "slope", positive=True)
    n = _number(n, "n", positive=True)
    p2 = _number(p2_mm, "p2_mm", positive=True)
    time = 3600 * 0.007 * (n * length / 0.3048) ** 0.8 / ((p2 / 25.4) ** 0.5 * slope ** 0.4)
    return _result("TR-55 sheet-flow travel time", _TR55 + "; NRCS NEH 630 Chapter 15 (2010), sheet-flow length guidance",
                   travel_time_s=time, length_m=length,
                   warnings=["sheet_flow_length_exceeds_100_ft"] if length > 30.48 else [],
                   assumptions=["P2 is the local 2-year 24-hour rainfall depth; US formula converted using 0.3048 m/ft and 25.4 mm/in."])


def shallow_flow_time(length_m, slope, paved=False):
    """TR-55 figure 3-1: V=20.3282 sqrt(S) paved, 16.1345 unpaved, ft/s."""
    length = _number(length_m, "length_m", positive=True)
    slope = _number(slope, "slope", positive=True)
    if not isinstance(paved, bool):
        raise CivilError("invalid_parameter", field="paved")
    velocity = (20.3282 if paved else 16.1345) * math.sqrt(slope) * 0.3048
    return _result("TR-55 shallow-concentrated travel time", _TR55,
                   travel_time_s=length / velocity, velocity_ms=velocity,
                   assumptions=["Paved shallow-flow coefficient." if paved else "Unpaved shallow-flow coefficient."])


def channel_flow_time(length_m, slope, n, area_m2, wetted_perimeter_m):
    """TR-55 channel travel time, V=R^(2/3) sqrt(S)/n, SI Manning equation."""
    length = _number(length_m, "length_m", positive=True)
    slope = _number(slope, "slope", positive=True)
    n = _number(n, "n", positive=True)
    area = _number(area_m2, "area_m2", positive=True)
    perimeter = _number(wetted_perimeter_m, "wetted_perimeter_m", positive=True)
    radius = area / perimeter
    velocity = radius ** (2 / 3) * math.sqrt(slope) / n
    return _result("Manning channel travel time", _TR55,
                   travel_time_s=length / velocity, velocity_ms=velocity, hydraulic_radius_m=radius,
                   assumptions=["Uniform flow at the explicitly supplied channel area and wetted perimeter."])


def time_of_concentration(segments):
    """Sum TR-55 flow-path travel times (chapter 3).

    Segment fields: type, length_m (or civil catchment contract's length), slope;
    sheet additionally n,p2_mm; shallow paved; channel n,area_m2,wetted_perimeter_m.
    """
    if not isinstance(segments, (list, tuple)) or not segments:
        raise CivilError("invalid_tc_segments")
    results, warnings, assumptions = [], [], []
    for segment in segments:
        if not isinstance(segment, dict):
            raise CivilError("invalid_tc_segments")
        length = segment.get("length_m", segment.get("length"))
        kind = segment.get("type")
        if kind == "sheet":
            result = sheet_flow_time(length, segment.get("slope"), segment.get("n"), segment.get("p2_mm"))
        elif kind == "shallow":
            result = shallow_flow_time(length, segment.get("slope"), segment.get("paved", False))
        elif kind == "channel":
            result = channel_flow_time(length, segment.get("slope"), segment.get("n"), segment.get("area_m2"), segment.get("wetted_perimeter_m"))
        else:
            raise CivilError("unsupported_tc_segment", segment_type=kind)
        results.append({"type": kind, **result})
        warnings.extend(result["warnings"])
        assumptions.extend(result["assumptions"])
    return _result("TR-55 time of concentration", _TR55, tc_s=sum(r["travel_time_s"] for r in results),
                   segments=results, warnings=warnings, assumptions=list(dict.fromkeys(assumptions)))


def nrcs_unit_hydrograph(area_m2, tc_s, dt_s, peak_rate_factor=484):
    """1-mm NRCS unit hydrograph, Tp=dt/2+0.6Tc; NEH 630 Table 16-1.

    PRF 484 uses the published tabulated shape. Other PRFs in [100,600] use the
    NEH 16-1 gamma shape x^m exp(m(1-x)); m is solved from its integral per
    eq. 16-2, not obtained by scaling a 484 shape without changing its volume.
    Ordinates are normalized after sampling to exactly 1 mm × catchment area
    under trapezoidal integration; the normalization factor is returned.
    """
    area = _number(area_m2, "area_m2", positive=True)
    tc = _number(tc_s, "tc_s", positive=True)
    dt = _number(dt_s, "dt_s", positive=True)
    prf = _number(peak_rate_factor, "peak_rate_factor", positive=True)
    if not 100 <= prf <= 600:
        raise CivilError("unsupported_peak_rate_factor", "Supported PRF range is 100–600")
    lag, tp = 0.6 * tc, dt / 2 + 0.6 * tc
    warnings = ["unit_duration_exceeds_0_133_tc"] if dt > 0.133 * tc else []
    assumptions = ["Lag=0.6Tc; excess rainfall is uniform over dt; no baseflow; full hydrograph tail is included.",
                   "Sampled ordinates normalized to one millimeter of runoff using trapezoidal integration."]
    peak_formula = prf * area * 0.028316846592 * 3600 / (2589988.110336 * 25.4 * tp)
    if prf == 484:
        table = _tables()["unit_hydrograph"]
        t = np.arange(math.ceil(5 * tp / dt) + 1, dtype=float) * dt
        q = np.interp(t / tp, table["t_over_tp"], table["q_over_qp"], left=0, right=0) * peak_formula
        shape_factor = None
    else:
        def prf_of_m(m):
            integral = math.exp(m + float(gammaln(m + 1)) - (m + 1) * math.log(m))
            return 645.33 / integral
        shape_factor = brentq(lambda m: prf_of_m(m) - prf, 0.001, 100)
        end_ratio = float(gammaincinv(shape_factor + 1, 1 - 1e-10)) / shape_factor
        t = np.arange(math.ceil(end_ratio * tp / dt) + 2, dtype=float) * dt
        x = t[1:] / tp
        q = np.zeros(len(t))
        q[1:] = peak_formula * np.exp(shape_factor * (np.log(x) + 1 - x))
        q[-1] = 0.0
        assumptions.append("Non-484 PRF uses the NEH gamma-family equation, not Table 16-1.")
    volume_raw = float(np.trapezoid(q, t))
    if volume_raw <= 0:
        raise CivilError("unit_hydrograph_underresolved", "Reduce dt_s")
    normalization = area * 0.001 / volume_raw
    q *= normalization
    if abs(normalization - 1) > 0.005:
        warnings.append("unit_hydrograph_sampling_changes_peak_more_than_0_5_pct")
    return _result("NRCS dimensionless unit hydrograph", _NEH16, t_s=t.tolist(), q_m3s=q.tolist(),
                   lag_s=lag, time_to_peak_s=tp, unit_depth_mm=1.0, volume_m3=float(np.trapezoid(q, t)),
                   peak_m3s=float(q.max()), peak_rate_factor=prf, gamma_shape_factor=shape_factor,
                   normalization_factor=normalization, warnings=warnings, assumptions=assumptions)


def design_storm(kind="scs_type_ii", depth_mm=None, duration_s=86400, dt_s=300, idf=None):
    """SCS 24-hour mass curves or IDF alternating blocks (HDS-2 §6.2.2).

    SCS storms require exactly 24 hours; published 30-minute ordinates are
    interpolated linearly, not stretched or replaced by proprietary curves.
    IDF input is {table: <idf table>, return_period_yr: ...}; the table can also
    directly carry return_period_yr. Total block depth is scaled to the explicit
    depth_mm, and that scale is reported; this is a temporal shape assumption.
    """
    if not isinstance(kind, str):
        raise CivilError("unsupported_storm", kind=kind)
    depth = _number(depth_mm, "depth_mm", nonnegative=True)
    t = _uniform_grid(duration_s, dt_s)
    warnings, assumptions = [], []
    scaling = 1.0
    if kind.startswith("scs_type_"):
        table = _tables()["storms"]
        if kind not in table:
            raise CivilError("unsupported_storm", kind=kind)
        if t[-1] != table["duration_s"]:
            raise CivilError("unsupported_scs_duration", "SCS I/IA/II/III tables are 24-hour distributions")
        fractions = table[kind]
        cumulative = np.interp(t, np.arange(len(fractions)) * table["time_step_s"], fractions) * depth
        incremental = np.diff(cumulative)
        reference = _tables()["provenance"]["storms_i_ia" if kind in ("scs_type_i", "scs_type_ia") else "storms_ii_iii"]
        assumptions.append("Linear interpolation of published 30-minute SCS cumulative ordinates; 24-hour duration is not rescaled.")
        if float(dt_s) < table["time_step_s"]:
            warnings.append("scs_table_resolution_1800_s_does_not_resolve_subinterval_intensity_peaks")
    elif kind == "idf_alternating_block":
        if not isinstance(idf, dict):
            raise CivilError("idf_required")
        table = idf.get("table", idf)
        period = _number(idf.get("return_period_yr"), "return_period_yr", positive=True)
        depths = np.array([idf_intensity(table, float(d), period)["intensity_mm_h"] * d / 3600 for d in t[1:]])
        blocks = np.diff(np.r_[0.0, depths])
        if np.any(blocks < 0):
            raise CivilError("nonmonotonic_idf_depth", "Cumulative IDF depth must increase with duration")
        blocks = np.sort(blocks)[::-1]
        incremental = np.empty(len(blocks))
        center = (len(blocks) - 1) // 2
        positions = [center]
        for distance in range(1, len(blocks)):
            if center + distance < len(blocks):
                positions.append(center + distance)
            if center - distance >= 0:
                positions.append(center - distance)
        scaling = depth / depths[-1]
        for position, block in zip(positions, blocks):
            incremental[position] = block * scaling
        cumulative = np.r_[0.0, np.cumsum(incremental)]
        reference = "Chow, Maidment and Mays, Applied Hydrology (1988), alternating-block method; " + _PFDS
        assumptions.append("Largest block at the midpoint; remaining blocks alternate right then left.")
        assumptions.append("IDF temporal shape normalized to the supplied event depth; scale factor is reported.")
        if not math.isclose(scaling, 1, rel_tol=1e-8):
            warnings.append("event_depth_differs_from_idf_total_shape_rescaled")
    else:
        raise CivilError("unsupported_storm", kind=kind)
    return _result("Design-storm hyetograph", reference, kind=kind, t_s=t.tolist(), depth_mm=incremental.tolist(),
                   cumulative_mm=cumulative.tolist(), total_depth_mm=float(incremental.sum()),
                   duration_s=float(t[-1]), dt_s=float(dt_s), idf_depth_scale=scaling,
                   warnings=warnings, assumptions=assumptions)


def runoff_hydrograph(catchment, storm, dt_s):
    """Cumulative-CN excess differencing and UH convolution (NEH 630 ch.16).

    catchment: area_m2,cn,tc_s (or tc_segments), optional ia_ratio and
    peak_rate_factor. storm: t_s boundaries and depth_mm interval depths.
    Storm intervals must equal dt_s, so precipitation is not silently resampled.
    """
    if not isinstance(catchment, dict) or not isinstance(storm, dict):
        raise CivilError("invalid_runoff_input")
    dt = _number(dt_s, "dt_s", positive=True)
    t = _vector(storm.get("t_s"), "t_s", nonnegative=True)
    rainfall = _vector(storm.get("depth_mm"), "depth_mm", nonnegative=True)
    if len(t) != len(rainfall) + 1 or t[0] != 0 or not np.allclose(np.diff(t), dt, rtol=1e-10, atol=1e-8):
        raise CivilError("time_grid_mismatch", "Storm boundary times must start at zero and have interval dt_s")
    area = _number(catchment.get("area_m2"), "area_m2", positive=True)
    tc = catchment.get("tc_s")
    tc_result = None
    if tc is None:
        tc_result = time_of_concentration(catchment.get("tc_segments"))
        tc = tc_result["tc_s"]
    cn_result = scs_runoff_depth(float(rainfall.sum()), catchment.get("cn"), catchment.get("ia_ratio", 0.2))
    cn = float(catchment["cn"])
    ratio = cn_result["ia_ratio"]
    cumulative = np.r_[0.0, np.cumsum(rainfall)]
    cumulative_excess = np.array([_cn_depth(float(p), cn, ratio)[0] for p in cumulative])
    excess = np.diff(cumulative_excess)
    uh = nrcs_unit_hydrograph(area, tc, dt, catchment.get("peak_rate_factor", 484))
    q = np.convolve(excess, np.array(uh["q_m3s"]))
    output_times = np.arange(len(q), dtype=float) * dt
    actual_volume = float(np.trapezoid(q, output_times))
    expected_volume = cn_result["runoff_mm"] * area * 0.001
    error = actual_volume - expected_volume
    error_pct = 100 * error / expected_volume if expected_volume else 0.0
    warnings = cn_result["warnings"] + uh["warnings"] + storm.get("warnings", [])
    assumptions = cn_result["assumptions"] + uh["assumptions"]
    if tc_result:
        warnings += tc_result["warnings"]
        assumptions += tc_result["assumptions"]
    if abs(error_pct) > 0.5:
        warnings.append("runoff_volume_balance_exceeds_0_5_pct")
    return _result("NRCS CN excess and unit-hydrograph convolution", _TR55 + "; " + _NEH16,
                   t_s=output_times.tolist(), q_m3s=q.tolist(), excess_depth_mm=excess.tolist(),
                   runoff_depth_mm=cn_result["runoff_mm"], expected_volume_m3=expected_volume,
                   volume_m3=actual_volume, mass_balance_error_m3=error, mass_balance_error_pct=error_pct,
                   volume_conservation_pass=abs(error_pct) <= 0.5, peak_m3s=float(q.max()),
                   peak_time_s=float(output_times[int(q.argmax())]), tc_s=float(tc), warnings=list(dict.fromkeys(warnings)),
                   assumptions=list(dict.fromkeys(assumptions)))


def _curve(points, field, *, increasing_y=False):
    if not isinstance(points, (list, tuple)) or len(points) < 2:
        raise CivilError("invalid_rating_curve", field=field)
    x, y = [], []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise CivilError("invalid_rating_curve", field=field)
        x.append(_number(point[0], field))
        y.append(_number(point[1], field, nonnegative=True))
    x, y = np.array(x), np.array(y)
    if np.any(np.diff(x) <= 0) or np.any(np.diff(y) < 0) or (increasing_y and np.any(np.diff(y) <= 0)):
        raise CivilError("invalid_rating_curve", "Stages must increase and ordinates must be monotonic", field=field)
    return x, y


def _prepare_outlet(outlet, stage_bounds):
    if not isinstance(outlet, dict):
        raise CivilError("invalid_outlet")
    kind = outlet.get("type")
    if kind == "rating_curve":
        stages, flows = _curve(outlet.get("stage_discharge"), "stage_discharge")
        if stages[0] > stage_bounds[0] or stages[-1] < stage_bounds[1]:
            raise CivilError("outlet_rating_outside_table", "Rating must cover the complete stage-storage table")
        return lambda stage: float(np.interp(stage, stages, flows)), "User-supplied monotone stage-discharge rating, linearly interpolated."
    if kind not in ("orifice", "weir", "riser"):
        raise CivilError("unsupported_outlet", outlet_type=kind)
    cd = _number(outlet.get("cd"), "cd", positive=True)
    if cd > 1:
        raise CivilError("invalid_outlet_coefficient", "cd is a dimensionless discharge coefficient in (0,1]")
    tailwater = outlet.get("tailwater_m")
    if tailwater is not None:
        tailwater = _number(tailwater, "tailwater_m")
    if kind == "orifice":
        invert = _number(outlet.get("invert_m"), "invert_m")
        diameter = _number(outlet.get("diameter_m"), "diameter_m", positive=True)
        radius = diameter / 2
        def orifice(stage):
            wetted_height = min(diameter, max(0.0, stage - invert))
            if wetted_height == 0:
                return 0.0
            def integrand(y):
                elevation = invert + y
                upstream_head = stage - elevation
                downstream_head = max(0.0, tailwater - elevation) if tailwater is not None else 0.0
                differential = upstream_head - downstream_head
                if differential <= 0:
                    return 0.0
                width = 2 * math.sqrt(max(0.0, radius ** 2 - (y - radius) ** 2))
                return cd * width * math.sqrt(2 * _G * differential)
            points = [tailwater - invert] if tailwater is not None and invert < tailwater < invert + wetted_height else None
            return float(quad(integrand, 0, wetted_height, points=points, epsabs=1e-10, epsrel=1e-9)[0])
        return orifice, "Vertical circular orifice: Torricelli Cd∫width(y)√(2gΔh(y))dy, including partially wetted aperture and constant tailwater; no reverse flow."
    crest = _number(outlet.get("crest_m"), "crest_m")
    if tailwater is not None and tailwater > crest:
        raise CivilError("submerged_weir_unsupported", "Free sharp-crested weir/riser rating requires downstream water below crest")
    if kind == "riser":
        diameter = _number(outlet.get("diameter_m"), "diameter_m", positive=True)
        orifice_cd = _number(outlet.get("orifice_cd"), "orifice_cd", positive=True)
        if orifice_cd > 1:
            raise CivilError("invalid_outlet_coefficient")
        perimeter, area = math.pi * diameter, math.pi * diameter ** 2 / 4
        def riser(stage):
            head = max(0.0, stage - crest)
            weir = (2 / 3) * cd * math.sqrt(2 * _G) * perimeter * head ** 1.5
            orifice = orifice_cd * area * math.sqrt(2 * _G * head)
            return min(weir, orifice)
        return riser, "Open circular riser: lesser of free perimeter sharp-crested weir and top-orifice rating; barrel/backpressure losses excluded."
    shape = outlet.get("shape", "rectangular")
    if shape == "rectangular":
        width = _number(outlet.get("width_m"), "width_m", positive=True)
        return (lambda stage: (2 / 3) * cd * math.sqrt(2 * _G) * width * max(0.0, stage - crest) ** 1.5,
                "Free suppressed rectangular sharp-crested weir, Q=(2/3)Cd√(2g)b h^1.5; no end contractions or approach-velocity correction.")
    if shape == "v_notch":
        angle = _number(outlet.get("angle_deg"), "angle_deg", positive=True)
        if angle >= 180:
            raise CivilError("invalid_weir_angle")
        return (lambda stage: (8 / 15) * cd * math.sqrt(2 * _G) * math.tan(math.radians(angle) / 2) * max(0.0, stage - crest) ** 2.5,
                "Free sharp-crested V-notch weir, Q=(8/15)Cd√(2g)tan(θ/2)h^2.5.")
    raise CivilError("unsupported_weir_shape", shape=shape)


def outlet_flow(outlet, stage_m):
    """Outlet rating using Torricelli/standard sharp-crested weir equations.

    orifice: cd,diameter_m,invert_m; weir: cd,crest_m,shape=rectangular
    (width_m) or v_notch (angle_deg); riser: cd,orifice_cd,diameter_m,crest_m.
    Optional constant tailwater_m; submerged weirs and reverse flow are refused.
    rating_curve: stage_discharge [[stage_m,q_m3s],...] with no extrapolation.
    Unlike a centroid approximation, the orifice integrates the hydrostatic
    velocity distribution over the wetted circular aperture, continuous at D/2
    and D, and tending to Cd A sqrt(2gh) for deep submergence. See HEC-22
    (2024), chapter 8, outlet structures; Torricelli and hydrostatic pressure.
    """
    stage = _number(stage_m, "stage_m")
    if isinstance(outlet, dict) and outlet.get("type") == "rating_curve":
        stages, _ = _curve(outlet.get("stage_discharge"), "stage_discharge")
        if not stages[0] <= stage <= stages[-1]:
            raise CivilError("outlet_rating_outside_table")
    function, assumption = _prepare_outlet(outlet, (stage, stage))
    if isinstance(outlet, dict) and outlet.get("tailwater_m") is not None and stage < _number(outlet["tailwater_m"], "tailwater_m"):
        raise CivilError("reverse_outlet_flow_unsupported")
    return _result("Outlet structure rating", "Torricelli; standard sharp-crested weir equations; FHWA HEC-22 (2024), outlet structures",
                   flow_m3s=function(stage), stage_m=stage, assumptions=[assumption])


def modified_puls(inflow_hydrograph, stage_storage, outlets, initial_stage, dt_s):
    """Storage-indication routing: S2+dt O2/2=S1+dt(I1+I2-O1)/2.

    Stage-storage is [[stage_m,volume_m3],...] and must be strictly increasing.
    Outlets are those documented by outlet_flow, including explicit rating_curve
    for measured structures or a linear reservoir. Constant tailwater only.
    Piecewise-linear inflow knots are retained in addition to dt_s grid points
    so interpolation does not discard storm volume. Each actual interval is no
    longer than dt_s. No extrapolation of stage-storage and no hidden spillway.
    """
    dt = _number(dt_s, "dt_s", positive=True)
    if not isinstance(inflow_hydrograph, dict):
        raise CivilError("invalid_hydrograph")
    source_t = _vector(inflow_hydrograph.get("t_s"), "t_s", nonnegative=True)
    source_q = _vector(inflow_hydrograph.get("q_m3s"), "q_m3s", nonnegative=True)
    if len(source_t) < 2 or len(source_t) != len(source_q) or np.any(np.diff(source_t) <= 0):
        raise CivilError("invalid_hydrograph", "Hydrograph times must strictly increase and match flow ordinates")
    stages, storage_values = _curve(stage_storage, "stage_storage", increasing_y=True)
    initial = _number(initial_stage, "initial_stage")
    if not stages[0] <= initial <= stages[-1]:
        raise CivilError("initial_stage_outside_storage")
    if not isinstance(outlets, (list, tuple)):
        raise CivilError("invalid_outlet")
    functions, assumptions = [], []
    for outlet in outlets:
        function, assumption = _prepare_outlet(outlet, (float(stages[0]), float(stages[-1])))
        if outlet.get("tailwater_m") is not None and stages[0] < float(outlet["tailwater_m"]):
            raise CivilError("reverse_outlet_flow_unsupported", "The storage range must not extend below a specified downstream stage")
        functions.append(function)
        assumptions.append(assumption)
    def discharge(stage):
        return sum(function(stage) for function in functions)
    def storage(stage):
        return float(np.interp(stage, stages, storage_values))
    grid = source_t[0] + np.arange(math.ceil((source_t[-1] - source_t[0]) / dt), dtype=float) * dt
    times = np.unique(np.r_[grid, source_t])
    times = times[times <= source_t[-1]]
    inflow = np.interp(times, source_t, source_q)
    stage, volumes, outflow = [initial], [storage(initial)], [discharge(initial)]
    for i in range(1, len(times)):
        step = float(times[i] - times[i - 1])
        target = volumes[-1] + step / 2 * (inflow[i - 1] + inflow[i] - outflow[-1])
        def indication(z):
            return storage(z) + step * discharge(z) / 2
        lower, upper = indication(float(stages[0])), indication(float(stages[-1]))
        tolerance = 1e-10 * max(1, abs(target), abs(lower))
        if target < lower - tolerance:
            raise CivilError("routing_timestep_too_large", "Storage would fall below table; reduce dt_s or extend physical lower-stage data", t_s=float(times[i]))
        if target > upper + tolerance:
            raise CivilError("stage_storage_exceeded", "Extend stage-storage and supply an explicit spillway if appropriate", t_s=float(times[i]))
        if abs(target - lower) <= tolerance:
            next_stage = float(stages[0])
        elif abs(target - upper) <= tolerance:
            next_stage = float(stages[-1])
        else:
            next_stage = brentq(lambda z: indication(z) - target, float(stages[0]), float(stages[-1]), xtol=1e-12, rtol=1e-12)
        stage.append(next_stage)
        volumes.append(storage(next_stage))
        outflow.append(discharge(next_stage))
    volume_in = float(np.trapezoid(inflow, times))
    volume_out = float(np.trapezoid(outflow, times))
    balance = volumes[0] + volume_in - volumes[-1] - volume_out
    denominator = volumes[0] + volume_in
    balance_pct = 100 * balance / denominator if denominator else 0.0
    peak_in, peak_out = float(inflow.max()), max(outflow)
    warnings = []
    if abs(balance_pct) > 0.5:
        warnings.append("routing_mass_balance_exceeds_0_5_pct")
    if outflow[0] > 0:
        warnings.append("initial_storage_release_contributes_to_outflow")
    assumptions += ["Level pool; stage-storage is piecewise linear; outlet ratings are time invariant; no seepage, evaporation or unlisted spillway.",
                    "Trapezoidal continuity on a grid retaining all original inflow knots; ending storage is included in mass balance."]
    return _result("Modified Puls level-pool routing", _PULS,
                   inflow_hydrograph={"t_s": times.tolist(), "q_m3s": inflow.tolist()},
                   outflow_hydrograph={"t_s": times.tolist(), "q_m3s": outflow},
                   stage_m=stage, storage_m3=volumes, max_stage_m=max(stage),
                   inflow_volume_m3=volume_in, outflow_volume_m3=volume_out,
                   initial_storage_m3=volumes[0], final_storage_m3=volumes[-1],
                   peak_inflow_m3s=peak_in, peak_outflow_m3s=peak_out,
                   peak_inflow_time_s=float(times[int(inflow.argmax())]),
                   peak_outflow_time_s=float(times[int(np.argmax(outflow))]),
                   peak_attenuation_pct=100 * (peak_in - peak_out) / peak_in if peak_in else None,
                   mass_balance_error_m3=balance, mass_balance_error_pct=balance_pct,
                   mass_balance_pass=abs(balance_pct) <= 0.5, warnings=warnings,
                   assumptions=list(dict.fromkeys(assumptions)))
