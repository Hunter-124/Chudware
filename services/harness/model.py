"""``aicad.harness/1`` — wire-harness model, derived routing state, persistence and report.

A harness is one JSON object persisted in the document's ``studies`` array (type
``"harness"``, name = harness id), following the ``piping_network`` study pattern
(``services/mcp/piping_tools.py``). A document may hold several harnesses.

Schema (units: mm, degrees; every vector is ``[x, y, z]`` world mm)::

    {schema: "aicad.harness/1", id, name,
     defaults: {packing_factor, bend_radius_x_od, clearance_mm, service_loop_mm, strip_mm},
     connectors: [{id, ref, family, family_label, part_number?, pins, pitch_mm?, rows?,
                   variant?, gender?, component?, attach?, exit_dir?,
                   frame: {origin, normal, up},
                   cavities: [{pin, signal?, wire?}]}],
     wires: [{id, from: {connector, pin, treatment?}, to: {connector, pin, treatment?},
              gauge_awg? | csa_mm2?, insulation_od_mm, insulation?, color?,
              kind: wire|twisted_pair|shielded|coax|ribbon, ribbon_width_mm?, ribbon_thickness_mm?,
              signal_class?: power|signal|data|shielded_drain|ground,
              shield_ground_end?: from|to|both|none, net?, signal?, group?,
              length_mm, service_loop_mm, strip_mm, min_bend_radius_mm,
              bend_radius_basis, bend_radius_confidence, segments: [ids]}],
     nodes: [{id, kind: connector|breakout|clamp|waypoint|splice, pos}],
     segments: [{id, a, b, path, od_mm, min_bend_radius_mm, wires: [ids], length_mm,
                 sleeve_id_mm?, override_od_mm?, override_packing_factor?,
                 override_min_bend_radius_mm?}],
     keepouts: [...], loads: [...], geometry: {...}, analyses: {...}}

Conventions (binding on every consumer):

* ``connector.frame.origin`` is the point where the wire bundle leaves the connector;
  ``frame.normal`` is the outward normal of the face it is mounted on (mating direction);
  ``exit_dir`` (default ``frame.normal``) is the direction the wires LEAVE into free space.
  A connector's node id equals the connector id.
* A segment is a bundle of wires between two nodes. Its ``path`` is a dense polyline whose
  first/last points are the nodes' positions; ``segment.a``→``segment.b`` is path order.
* All ``min_bend_radius_mm`` values and caller-specified ``bend_radius_x_od``
  multiples refer to path centerline radius. Sourced inside-radius estimates add
  half the applicable OD. Ribbon cables use a circumscribed circular envelope;
  their rectangular swept shape and orientation are not represented.
* ``wire.length_mm`` is the ROUTED length (sum of the segments the wire rides). The
  physical cut length is :func:`wire_cut_length_mm` = routed + ``service_loop_mm`` +
  ``strip_mm`` (``strip_mm`` is the total termination allowance, both ends combined).
* Wire routes, segment wire lists, bundle OD and bundle bend radius are DERIVED state:
  :func:`recompute_derived` rebuilds them from the connector nodes + segment tree, so a
  wire always rides the unique tree path between its two connectors.
"""
from __future__ import annotations

import copy
import math
import re
from typing import Any, Optional

from services.harness.wire_data import (
    NotTabulated,
    awg_diameter_mm,
    awg_label,
    metric_diameter_mm,
    parse_awg,
)

SCHEMA = "aicad.harness/1"
STUDY_TYPE = "harness"
STUDY_ENGINE = "py-harness"

WIRE_KINDS = ("wire", "twisted_pair", "shielded", "coax", "ribbon")
#: EXACT strings shared with ``services/mcp/harness_electrical_tools.py`` (HarnessElectrical).
SIGNAL_CLASSES = ("power", "signal", "data", "shielded_drain", "ground")
SHIELD_ENDS = ("from", "to", "both", "none")
END_TREATMENTS = ("crimp", "ferrule", "solder", "tinned", "bare")
NODE_KINDS = ("connector", "breakout", "clamp", "waypoint", "splice")
#: Segment fields owned by ``recompute_derived``; every other segment field is author intent.
SEGMENT_DERIVED = ("od_mm", "min_bend_radius_mm", "wires", "length_mm", "od_basis",
                   "bend_radius_basis", "bend_radius_confidence")

#: Optional wire properties consumed by electrical analyses.
WIRE_PASSTHROUGH_KEYS = ("temp_rating_c", "stranded", "conductor_class", "r20_ohm_per_km",
                         "insulation", "insulation_thermal_conductivity_w_mk", "emissivity",
                         "part_number", "notes")

NASA_BEND_SOURCE = "https://s3vi.ndc.nasa.gov/ssri-kb/static/resources/nasa-std-8739.4a.pdf"
BEND_CONVENTION_SOURCE = "https://pickeringconnect.com/blog/minimum-bend-radius/"
PACKING_SOURCES = (
    "https://rapidharness.com/support/design-types/harnesses/bundle-diameters",
    "https://onepullwire.com/5-steps-to-calculate-the-diameter-of-bundled-cable/",
)
# One Pull Wire's mixed-size example uses a 1.2 diameter multiplier. This
# translates to an area fraction, not a universal physical packing fraction.
DEFAULT_PACKING_FACTOR = 1.0 / (1.2 ** 2)

DEFAULTS: dict[str, Optional[float]] = {
    "packing_factor": DEFAULT_PACKING_FACTOR,
    # Caller-supplied centerline multiplier overrides the individual-wire estimate.
    "bend_radius_x_od": None,
    # Default clearance from obstacles (C2 contract default), mm.
    "clearance_mm": 2.0,
    # Slack / termination allowances. 0 = "not specified": reports list every wire that
    # relies on it instead of pretending the cut length is complete.
    "service_loop_mm": 0.0,
    "strip_mm": 0.0,
}

#: Nets whose NAME conventionally means a return/ground conductor (used only to PRE-FILL
#: signal_class when seeding from a netlist; labelled a heuristic in every reply).
GROUND_NET_NAMES = frozenset({"GND", "AGND", "DGND", "PGND", "EGND", "GNDA", "GNDD", "VSS", "VSSA",
                              "0"})


class HarnessError(ValueError):
    """A refusal with a machine code, an actionable hint and optional structured detail."""

    def __init__(self, code: str, message: str, hint: Optional[str] = None, **extra: Any):
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.extra = extra

    def to_dict(self) -> dict:
        out: dict = {"ok": False, "code": self.code, "error": str(self)}
        if self.hint:
            out["hint"] = self.hint
        out.update(self.extra)
        return out


# ----------------------------------------------------------------------------- vectors

def _r(x: float, nd: int = 4) -> float:
    v = round(float(x), nd)
    return 0.0 if v == 0 else v


def v3(v: Any, what: str) -> list:
    """Validate and copy an ``[x, y, z]`` vector of finite numbers."""
    try:
        out = [float(c) for c in v]
    except (TypeError, ValueError):
        raise HarnessError("bad_vector", f"{what} must be [x, y, z] numbers in mm; got {v!r}")
    if len(out) != 3 or not all(math.isfinite(c) for c in out):
        raise HarnessError("bad_vector", f"{what} must be three finite numbers [x, y, z]; got {v!r}")
    return out


def vsub(a: list, b: list) -> list:
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def vadd(a: list, b: list) -> list:
    return [a[0] + b[0], a[1] + b[1], a[2] + b[2]]


def vscale(a: list, s: float) -> list:
    return [a[0] * s, a[1] * s, a[2] * s]


def vdot(a: list, b: list) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def vcross(a: list, b: list) -> list:
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def vnorm(a: list) -> float:
    return math.sqrt(vdot(a, a))


def vunit(a: list, what: str = "vector") -> list:
    n = vnorm(a)
    if n < 1e-12:
        raise HarnessError("zero_vector", f"{what} has zero length")
    return [a[0] / n, a[1] / n, a[2] / n]


def vdist(a: list, b: list) -> float:
    return vnorm(vsub(a, b))


def angle_deg(a: list, b: list) -> float:
    """Angle between two non-zero vectors, degrees in [0, 180]."""
    c = vdot(a, b) / (vnorm(a) * vnorm(b))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def polyline_length(path: list) -> float:
    return sum(vdist(path[i], path[i + 1]) for i in range(len(path) - 1))


def dedupe_path(path: list, tol: float = 1e-9) -> list:
    out: list = []
    for p in path:
        if not out or vdist(out[-1], p) > tol:
            out.append(list(p))
    return out


def simplify_polyline(path: list, tol: float) -> list:
    """Ramer-Douglas-Peucker: keep the end points and every vertex needed to stay within ``tol``."""
    if len(path) <= 2:
        return [list(p) for p in path]
    keep = [False] * len(path)
    keep[0] = keep[-1] = True
    stack = [(0, len(path) - 1)]
    while stack:
        lo, hi = stack.pop()
        a, b = path[lo], path[hi]
        ab = vsub(b, a)
        den = vdot(ab, ab)
        worst, at = -1.0, -1
        for i in range(lo + 1, hi):
            t = 0.0 if den == 0 else max(0.0, min(1.0, vdot(vsub(path[i], a), ab) / den))
            d = vdist(path[i], vadd(a, vscale(ab, t)))
            if d > worst:
                worst, at = d, i
        if worst > tol:
            keep[at] = True
            stack.extend(((lo, at), (at, hi)))
    return [list(p) for p, k in zip(path, keep) if k]


def closest_on_polyline(p: list, path: list) -> "tuple[float, int, float, list]":
    """``(distance, leg_index, t, point)`` of the point on ``path`` nearest to ``p``."""
    best: "tuple[float, int, float, list]" = (math.inf, 0, 0.0, list(path[0]))
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        ab = vsub(b, a)
        den = vdot(ab, ab)
        t = 0.0 if den < 1e-18 else max(0.0, min(1.0, vdot(vsub(p, a), ab) / den))
        q = vadd(a, vscale(ab, t))
        d = vdist(p, q)
        if d < best[0]:
            best = (d, i, t, q)
    return best


def point_at_distance(path: list, s: float) -> "tuple[list, int]":
    """The point ``s`` mm along ``path`` (clamped) and the index of the leg it lies on."""
    acc = 0.0
    for i in range(len(path) - 1):
        leg = vdist(path[i], path[i + 1])
        if acc + leg >= s or i == len(path) - 2:
            t = 0.0 if leg < 1e-12 else max(0.0, min(1.0, (s - acc) / leg))
            return vadd(path[i], vscale(vsub(path[i + 1], path[i]), t)), i
        acc += leg
    return list(path[0]), 0


# ----------------------------------------------------------------------------- wire sizing

def conductor_diameter_mm(gauge_awg: Any = None, csa_mm2: Any = None) -> Optional[float]:
    """Equivalent solid-conductor diameter of a gauge (ASTM B258) or a metric CSA, mm.

    Returns None when neither is given. Refuses both (ambiguous) and unparsable values.
    """
    if gauge_awg is not None and csa_mm2 is not None:
        raise HarnessError("gauge_conflict", "pass gauge_awg OR csa_mm2, not both",
                           hint="a wire has one conductor size; use csa_mm2 for metric cable")
    if gauge_awg is not None:
        try:
            return awg_diameter_mm(parse_awg(gauge_awg))
        except NotTabulated as exc:
            raise HarnessError("bad_gauge", str(exc), hint="gauge_awg is a whole AWG size "
                               "(e.g. 22, '4/0')") from exc
    if csa_mm2 is not None:
        try:
            a = float(csa_mm2)
        except (TypeError, ValueError):
            raise HarnessError("bad_gauge", f"csa_mm2 must be a positive number, got {csa_mm2!r}")
        if not math.isfinite(a) or a <= 0:
            raise HarnessError("bad_gauge", f"csa_mm2 must be a positive number, got {csa_mm2!r}")
        return metric_diameter_mm(a)
    return None


def bundle_od_mm(ods: list, packing_factor: Optional[float] = None) -> float:
    """Estimate bundle diameter from insulated item ODs, in mm.

    RapidHarness's method uses one OD for one item, the sum for two, and
    for larger bundles max(1.2 * sqrt(sum(OD²)), the two largest ODs).
    One Pull Wire's mixed-diameter example supplies the 1.2 multiplier.
    ``packing_factor`` is the occupied area fraction (1 / multiplier²);
    this is a planning estimate, not a fit or clearance guarantee.
    https://rapidharness.com/support/design-types/harnesses/bundle-diameters
    https://onepullwire.com/5-steps-to-calculate-the-diameter-of-bundled-cable/
    """
    d = sorted((float(x) for x in ods), reverse=True)
    if not d:
        raise HarnessError("empty_bundle", "a bundle needs at least one wire")
    if any((not math.isfinite(x)) or x <= 0 for x in d):
        raise HarnessError("bad_od", f"wire ODs must be positive finite mm; got {ods!r}")
    if len(d) == 1:
        return d[0]
    if len(d) == 2:
        return d[0] + d[1]
    pf = DEFAULT_PACKING_FACTOR if packing_factor is None else float(packing_factor)
    if not (0.0 < pf <= 1.0):
        raise HarnessError("bad_packing_factor", f"packing_factor must be in (0, 1]; got {pf}")
    return max(math.sqrt(sum(x * x for x in d) / pf), d[0] + d[1])


def validate_insulation(value: Any) -> Optional[str]:
    """Preserve the material name without pretending all formulations are tabulated."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HarnessError("bad_insulation", "insulation must be a nonempty material name")
    return value.strip()


def _polyimide(wire: dict) -> bool:
    return str(wire.get("insulation") or "").strip().lower() in ("polyimide", "kapton")


def _wire_bend_default(wire: dict) -> tuple[float, str, str]:
    """Static-screening estimate; NASA does not provide a generic individual-wire row."""
    poly = _polyimide(wire)
    coax = wire.get("kind") == "coax"
    if wire.get("kind") == "ribbon" and not poly:
        return (2.5 * float(wire["insulation_od_mm"]),
                "IPC/WHMA-A-620E flat ribbon 2 x OD inside radius, as reported by Pickering; "
                "using circumscribed width/thickness envelope as OD plus 0.5 envelope for "
                "centerline. Conservative circular approximation; orientation, rectangular "
                "sweep and actual cable flex limits are not modeled. " + BEND_CONVENTION_SOURCE,
                "low")
    factor = 10 if poly else 6
    subject = ("polyimide insulated wire" if poly else "individual coax" if coax else
               "conservative extrapolation from overall harness with coax/large wire")
    basis = (f"NASA-STD-8739.4A §7.2.19 Table 7-1: {subject}, {factor} x OD minimum; "
             "assuming inside-radius convention, centerline adds 0.5 x OD. Installed static "
             "screening only; verify insulation, cable datasheet and dynamic-flex limits. "
             + NASA_BEND_SOURCE + " Convention: " + BEND_CONVENTION_SOURCE)
    return (factor + 0.5) * float(wire["insulation_od_mm"]), basis, "low"


def bundle_bend_radius(members: list, od: float) -> tuple[float, str, str]:
    """Return a centerline radius estimate for a completed bundle and its members."""
    if not members:
        raise HarnessError("empty_bundle", "a bundle needs at least one wire")
    member_radius = max(float(w["min_bend_radius_mm"]) +
                        max(0.0, (od - float(w["insulation_od_mm"])) / 2)
                        for w in members)
    if len(members) == 1:
        return member_radius, members[0].get("bend_radius_basis", "caller supplied"), \
            members[0].get("bend_radius_confidence", "caller supplied")
    if any(_polyimide(w) for w in members):
        factor, category = 10, "harness containing polyimide"
    elif (any(w.get("kind") == "coax" for w in members) or
          any(w.get("gauge_awg") is None or w["gauge_awg"] < 10 for w in members)):
        factor, category = 6, "harness with coax, large or unspecified conductor size"
    else:
        factor, category = 3, "harness of AWG 10 or smaller without coax"
    radius = max(member_radius, (factor + 0.5) * od)
    basis = (f"NASA-STD-8739.4A §7.2.19 Table 7-1: {category}, {factor} x bundle OD; "
             "assumed inside radius converted to centerline by adding half bundle OD; "
             "also accommodates member radii at the inner edge. Static screening only; "
             "unknown insulation, cable construction and flex conditions require datasheets. "
             + NASA_BEND_SOURCE + " Convention: " + BEND_CONVENTION_SOURCE)
    return radius, basis, "low"


def bundle_requirements(members: list, packing_factor: Optional[float],
                        overrides: Optional[dict] = None) -> dict:
    """OD and centerline bend radius a bundle of ``members`` must satisfy, as stored on segments.

    The single source for :func:`recompute_derived` and the router, so a route is planned for the
    requirement the later check applies. ``overrides`` is a segment dict whose ``override_od_mm``,
    ``override_packing_factor`` and ``override_min_bend_radius_mm`` win over the estimate.
    ``od_mm`` is None when the packing of three or more wires is unknown.
    """
    overrides = overrides or {}
    pf = overrides.get("override_packing_factor", packing_factor)
    if "override_od_mm" in overrides:
        od, od_basis = _r(overrides["override_od_mm"]), "caller supplied segment OD"
    elif pf is None and len(members) >= 3:
        od, od_basis = None, "unknown: packing factor not specified"
    else:
        od = _r(bundle_od_mm([m["insulation_od_mm"] for m in members], pf))
        od_basis = f"estimated from item ODs and area packing factor {pf!r}"
    if od is None:
        radius = overrides.get("override_min_bend_radius_mm")
        return {"od_mm": None, "od_basis": od_basis, "min_bend_radius_mm": radius,
                "bend_radius_basis": ("caller supplied segment centerline radius" if
                                      radius is not None else "unknown: bundle OD is not specified"),
                "bend_radius_confidence": "explicit" if radius is not None else "unknown"}
    radius, basis, confidence = bundle_bend_radius(members, od)
    if "override_min_bend_radius_mm" in overrides:
        basis = ("caller supplied segment centerline radius; estimated radius without "
                 f"override is {_r(radius)} mm")
        radius, confidence = overrides["override_min_bend_radius_mm"], "explicit"
    return {"od_mm": od, "od_basis": od_basis, "min_bend_radius_mm": _r(radius),
            "bend_radius_basis": basis, "bend_radius_confidence": confidence}


#: NEC (NFPA 70) Chapter 9, Table 1 — maximum conductor fill of a raceway cross-section:
#: 53 % for one conductor, 31 % for two, 40 % for more than two. Applied here only as the
#: reference limit for a wire bundle inside a user-specified sleeve/conduit inner diameter.
def nec_fill_limit(n_wires: int) -> float:
    return 0.53 if n_wires <= 1 else (0.31 if n_wires == 2 else 0.40)


# ----------------------------------------------------------------------------- connector families

#: Pitch / pin-count data only — no housing envelopes are invented. ``common_pin_counts`` is
#: advisory (a non-listed count is WARNED about, never refused); ``None`` = any count.
CONNECTOR_FAMILIES: "dict[str, dict]" = {
    "jst_ph": {"label": "JST PH", "pitch_mm": 2.0, "rows": 1, "common_pin_counts": None,
               "source": "JST PH series catalog: 2.00 mm pitch, single row"},
    "jst_xh": {"label": "JST XH", "pitch_mm": 2.5, "rows": 1, "common_pin_counts": None,
               "source": "JST XH series catalog: 2.50 mm pitch, single row"},
    "jst_gh": {"label": "JST GH", "pitch_mm": 1.25, "rows": 1, "common_pin_counts": None,
               "source": "JST GH series catalog: 1.25 mm pitch, single row"},
    "jst_sh": {"label": "JST SH", "pitch_mm": 1.0, "rows": 1, "common_pin_counts": None,
               "source": "JST SH series catalog: 1.00 mm pitch, single row"},
    "molex_microfit_3": {"label": "Molex Micro-Fit 3.0", "pitch_mm": 3.0, "rows": None,
                         "common_pin_counts": None,
                         "source": "Molex Micro-Fit 3.0 connector family: 3.00 mm pitch "
                                   "(single and dual row variants)"},
    "molex_minifit_jr": {"label": "Molex Mini-Fit Jr.", "pitch_mm": 4.2, "rows": None,
                         "common_pin_counts": None,
                         "source": "Molex Mini-Fit Jr. connector family: 4.20 mm pitch"},
    "header_2p54": {"label": "0.1 in pin header / Dupont", "pitch_mm": 2.54, "rows": None,
                    "common_pin_counts": None,
                    "source": "0.100 in (2.54 mm) standard pin-header grid"},
    "dsub_de9": {"label": "D-sub DE-9", "pitch_mm": 2.77, "rows": 2, "common_pin_counts": (9,),
                 "source": "D-subminiature (IEC 60807-3 / MIL-DTL-24308) contact spacing "
                           "2.77 mm (0.109 in); DE shell = 9 contacts in two rows"},
    "deutsch_dt": {"label": "TE DEUTSCH DT", "pitch_mm": None, "rows": None,
                   "common_pin_counts": (2, 3, 4, 6, 8, 12),
                   "source": "TE Connectivity DEUTSCH DT series: 2/3/4/6/8/12-way housings; "
                             "cavities are not on a regular pitch"},
    "m8": {"label": "M8 circular (IEC 61076-2-104)", "pitch_mm": None, "rows": None,
           "common_pin_counts": (3, 4, 6, 8),
           "source": "IEC 61076-2-104 M8 connectors; no regular contact pitch"},
    "m12_a": {"label": "M12 A-coded (IEC 61076-2-101)", "pitch_mm": None, "rows": None,
              "common_pin_counts": (3, 4, 5, 8, 12),
              "source": "IEC 61076-2-101 M12 A-coding (sensor/actuator); no regular contact pitch"},
    "m12_b": {"label": "M12 B-coded (IEC 61076-2-101)", "pitch_mm": None, "rows": None,
              "common_pin_counts": (5,),
              "source": "IEC 61076-2-101 M12 B-coding (fieldbus, 5-pin)"},
    "m12_d": {"label": "M12 D-coded (IEC 61076-2-101)", "pitch_mm": None, "rows": None,
              "common_pin_counts": (4,),
              "source": "IEC 61076-2-101 M12 D-coding (industrial Ethernet, 4-pin)"},
    "m12_x": {"label": "M12 X-coded (IEC 61076-2-109)", "pitch_mm": None, "rows": None,
              "common_pin_counts": (8,),
              "source": "IEC 61076-2-109 M12 X-coding (industrial Ethernet, 8-pin)"},
    "phoenix_mstb_5p08": {"label": "Phoenix Contact MSTB 2,5 (5.08 mm)", "pitch_mm": 5.08,
                          "rows": 1, "common_pin_counts": None,
                          "source": "Phoenix Contact COMBICON MSTB 2,5/..-ST-5,08 designation "
                                    "(5.08 mm pitch)"},
    "phoenix_mc_3p5": {"label": "Phoenix Contact MC 1,5 (3.5 mm)", "pitch_mm": 3.5, "rows": 1,
                       "common_pin_counts": None,
                       "source": "Phoenix Contact COMBICON MC 1,5/..-ST-3,5 designation "
                                 "(3.50 mm pitch)"},
    "terminal_block": {"label": "Terminal block (screw/spring)", "pitch_mm": None, "rows": 1,
                       "common_pin_counts": None, "requires_pitch": True,
                       "source": "pitch is read from the part datasheet and supplied by the "
                                 "caller (pitch_mm required)"},
    "generic": {"label": "Generic connector", "pitch_mm": None, "rows": None,
                "common_pin_counts": None,
                "source": "caller-supplied pins / pitch_mm / part_number"},
}

_FAMILY_ALIASES = {
    "ph": "jst_ph", "jstph": "jst_ph", "xh": "jst_xh", "jstxh": "jst_xh",
    "gh": "jst_gh", "jstgh": "jst_gh", "sh": "jst_sh", "jstsh": "jst_sh",
    "microfit": "molex_microfit_3", "micro_fit": "molex_microfit_3",
    "molex_micro_fit": "molex_microfit_3", "molex_micro_fit_3": "molex_microfit_3",
    "molex_micro_fit_3_0": "molex_microfit_3", "microfit_3": "molex_microfit_3",
    "microfit3": "molex_microfit_3", "molex_microfit": "molex_microfit_3",
    "minifit": "molex_minifit_jr", "mini_fit": "molex_minifit_jr",
    "molex_mini_fit_jr": "molex_minifit_jr", "minifit_jr": "molex_minifit_jr",
    "dupont": "header_2p54", "header": "header_2p54", "pin_header": "header_2p54",
    "header_2_54": "header_2p54", "header_2_54mm": "header_2p54",
    "de9": "dsub_de9", "db9": "dsub_de9", "dsub9": "dsub_de9", "d_sub_9": "dsub_de9",
    "dt": "deutsch_dt", "deutsch": "deutsch_dt", "deutsch_dt_series": "deutsch_dt",
    "m12": "m12_a", "m12a": "m12_a", "m12_a_coded": "m12_a", "m12b": "m12_b", "m12d": "m12_d",
    "m12x": "m12_x", "mstb": "phoenix_mstb_5p08", "terminal": "terminal_block",
    "screw_terminal": "terminal_block", "spring_terminal": "terminal_block",
}


def resolve_family(family: str) -> "tuple[str, dict]":
    """``(family_id, table_entry)`` for a family spelling, or a refusal listing the table."""
    key = re.sub(r"[^a-z0-9]+", "_", str(family or "").lower()).strip("_")
    key = _FAMILY_ALIASES.get(key, key)
    if key in CONNECTOR_FAMILIES:
        return key, CONNECTOR_FAMILIES[key]
    raise HarnessError(
        "unknown_family", f"unknown connector family {family!r}",
        hint="use a family id from `families`, or family='generic' with pins=<n>, "
             "pitch_mm=<datasheet pitch> and part_number=<MPN>",
        families=sorted(CONNECTOR_FAMILIES))


# ----------------------------------------------------------------------------- construction

def slug(text: str, fallback: str = "harness") -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return s or fallback


def next_id(prefix: str, items: list, extra_taken: "Optional[set]" = None) -> str:
    """``<prefix><n>`` with n one above the highest numeric suffix already used."""
    taken = {str(i.get("id")) for i in items if isinstance(i, dict)} | set(extra_taken or ())
    n = 0
    for t in taken:
        m = re.fullmatch(re.escape(prefix) + r"(\d+)", t)
        if m:
            n = max(n, int(m.group(1)))
    return f"{prefix}{n + 1}"


def new_harness(name: str, harness_id: Optional[str] = None,
                defaults: Optional[dict] = None, existing_ids: "Optional[list]" = None) -> dict:
    """A blank harness. ``defaults`` overrides :data:`DEFAULTS` (every key validated)."""
    hid = slug(harness_id or name)
    taken = set(existing_ids or ())
    if harness_id is not None and hid in taken:
        raise HarnessError("duplicate_harness", f"harness {hid!r} already exists in this document",
                           hint="pick another harness_id or load the existing one",
                           existing=sorted(taken))
    base, n = hid, 1
    while hid in taken:
        n += 1
        hid = f"{base}_{n}"
    d = dict(DEFAULTS)
    for k, v in (defaults or {}).items():
        if k not in DEFAULTS:
            raise HarnessError("unknown_default", f"unknown harness default {k!r}",
                               hint=f"defaults keys: {sorted(DEFAULTS)}")
        try:
            fv = float(v)
        except (TypeError, ValueError):
            raise HarnessError("bad_default", f"default {k} must be a number; got {v!r}")
        if not math.isfinite(fv) or fv < 0 or (k in ("packing_factor", "bend_radius_x_od") and fv <= 0):
            raise HarnessError("bad_default", f"default {k}={v!r} is out of range")
        d[k] = fv
    if d["packing_factor"] is not None and d["packing_factor"] > 1.0:
        raise HarnessError("bad_default", "packing_factor must be in (0, 1]")
    return {"schema": SCHEMA, "id": hid, "name": str(name or hid), "defaults": d,
            "packing_factor_basis": "caller supplied" if "packing_factor" in (defaults or {}) else "estimate",
            "connectors": [], "wires": [], "nodes": [], "segments": [], "keepouts": [],
            "loads": [], "geometry": {}, "analyses": {}}


def mutated(h: dict) -> dict:
    """Mark ``h`` edited: analyses computed on the previous state no longer apply."""
    h["analyses"] = {}
    return h


def default_up(normal: list) -> list:
    ref = [0.0, 0.0, 1.0] if abs(normal[2]) < 0.9 else [1.0, 0.0, 0.0]
    u = vsub(ref, vscale(normal, vdot(ref, normal)))
    return vunit(u, "default up")


def make_frame(origin: Any, normal: Any, up: Any = None) -> dict:
    o = v3(origin, "frame.origin")
    n = vunit(v3(normal, "frame.normal"), "frame.normal")
    if up is None:
        u = default_up(n)
    else:
        raw = v3(up, "frame.up")
        u = vsub(raw, vscale(n, vdot(raw, n)))
        if vnorm(u) < 1e-9:
            raise HarnessError("bad_frame", "frame.up is parallel to frame.normal")
        u = vunit(u)
    return {"origin": [_r(c) for c in o], "normal": [_r(c, 6) for c in n],
            "up": [_r(c, 6) for c in u]}


def get_connector(h: dict, key: str) -> dict:
    """The connector with this id or ``ref`` (ids win), or a refusal naming the roster."""
    for c in h["connectors"]:
        if c["id"] == key:
            return c
    for c in h["connectors"]:
        if c.get("ref") == key:
            return c
    raise HarnessError("unknown_connector", f"no connector {key!r} in harness {h['id']!r}",
                       hint="harness_add_connector first; known: "
                            + (", ".join(c["id"] for c in h["connectors"]) or "none"),
                       connectors=[c["id"] for c in h["connectors"]])


def get_wire(h: dict, wid: str) -> dict:
    for w in h["wires"]:
        if w["id"] == wid:
            return w
    raise HarnessError("unknown_wire", f"no wire {wid!r} in harness {h['id']!r}",
                       wires=[w["id"] for w in h["wires"]][:50])


def get_node(h: dict, nid: str) -> Optional[dict]:
    for n in h["nodes"]:
        if n["id"] == nid:
            return n
    return None


def get_segment(h: dict, sid: str) -> dict:
    for s in h["segments"]:
        if s["id"] == sid:
            return s
    raise HarnessError("unknown_segment", f"no segment {sid!r} in harness {h['id']!r}",
                       segments=[s["id"] for s in h["segments"]][:50])


def ensure_connector_nodes(h: dict) -> None:
    """One ``connector`` node per connector, positioned at the connector frame origin."""
    for c in h["connectors"]:
        n = get_node(h, c["id"])
        pos = list(c["frame"]["origin"])
        if n is None:
            h["nodes"].append({"id": c["id"], "kind": "connector", "pos": pos})
        else:
            n["pos"] = pos
            n["kind"] = "connector"


def normalize_exit_dir(value: Any) -> list:
    """Unit exit direction rounded to 6 decimals, as stored on a connector."""
    return [_r(c, 6) for c in vunit(v3(value, "exit_dir"), "exit_dir")]


def add_connector(h: dict, *, ref: str, family: str, pins: Optional[int], frame: dict,
                  pitch_mm: Optional[float] = None, rows: Optional[int] = None,
                  part_number: Optional[str] = None, variant: Optional[str] = None,
                  gender: Optional[str] = None, component: Optional[str] = None,
                  attach: Optional[dict] = None, exit_dir: Any = None,
                  signals: Optional[dict] = None, connector_id: Optional[str] = None) -> "tuple[dict, list]":
    """Add a connector. Returns ``(connector, warnings)``."""
    ref = str(ref or "").strip()
    if not ref:
        raise HarnessError("ref_required", "ref (the designator, e.g. 'J1') is required")
    fam_id, fam = resolve_family(family)
    warnings: list = []
    if pins is None or isinstance(pins, bool) or int(pins) != pins or int(pins) < 1:
        raise HarnessError("bad_pins", f"pins must be a positive integer; got {pins!r}",
                           hint="the number of cavities/contacts of this connector")
    pins = int(pins)
    if pins > 256:
        raise HarnessError("bad_pins", f"pins={pins} is implausible (max 256)")
    common = fam.get("common_pin_counts")
    if common and pins not in common:
        warnings.append(f"{fam['label']}: {pins} is not one of the common cavity counts "
                        f"{list(common)}; confirm against the part datasheet")
    pitch = pitch_mm if pitch_mm is not None else fam.get("pitch_mm")
    if fam.get("requires_pitch") and pitch is None:
        raise HarnessError("pitch_required",
                           f"{fam['label']} needs pitch_mm from the part datasheet",
                           hint="terminal-block pitch varies by series; pass pitch_mm=<mm>")
    if pitch_mm is not None and fam.get("pitch_mm") is not None and \
            abs(float(pitch_mm) - fam["pitch_mm"]) > 1e-6:
        raise HarnessError("pitch_mismatch",
                           f"{fam['label']} is {fam['pitch_mm']} mm pitch; got pitch_mm={pitch_mm}",
                           hint="use family='generic' for a different pitch", source=fam["source"])
    if pitch is not None:
        pitch = float(pitch)
        if not math.isfinite(pitch) or pitch <= 0:
            raise HarnessError("bad_pitch", f"pitch_mm must be positive; got {pitch_mm!r}")
    cid = (str(connector_id).strip() if connector_id
           else re.sub(r"[^A-Za-z0-9_]+", "_", ref).strip("_")) or "C"
    if get_node(h, cid) is not None:
        raise HarnessError("duplicate_node", f"connector id {cid!r} is already a routing node",
                           hint="supply a distinct connector_id")
    if any(c["id"] == cid for c in h["connectors"]):
        raise HarnessError("duplicate_connector", f"connector {cid!r} already exists",
                           hint="pass a different ref/connector_id or edit the existing one")
    if any(c.get("ref") == ref for c in h["connectors"]):
        raise HarnessError("duplicate_ref", f"ref {ref!r} is already used by another connector")
    cavities = [{"pin": str(i), "signal": None, "wire": None} for i in range(1, pins + 1)]
    for k, v in (signals or {}).items():
        cav = next((c for c in cavities if c["pin"] == str(k)), None)
        if cav is None:
            raise HarnessError("unknown_pin", f"signals names pin {k!r}, but {ref} has pins 1..{pins}")
        cav["signal"] = str(v)
    if not isinstance(frame, dict):
        raise HarnessError("bad_frame", "frame must specify origin and normal")
    frame = make_frame(frame.get("origin"), frame.get("normal"), frame.get("up"))
    conn: dict = {"id": cid, "ref": ref, "family": fam_id, "family_label": fam["label"],
                  "pins": pins, "frame": frame, "cavities": cavities}
    if part_number:
        conn["part_number"] = str(part_number)
    if pitch is not None:
        conn["pitch_mm"] = pitch
    r = rows if rows is not None else fam.get("rows")
    if r is not None:
        conn["rows"] = int(r)
    if variant:
        conn["variant"] = str(variant)
    if gender:
        conn["gender"] = str(gender)
    if component:
        conn["component"] = str(component)
    if attach:
        conn["attach"] = attach
    if exit_dir is not None:
        conn["exit_dir"] = normalize_exit_dir(exit_dir)
    h["connectors"].append(conn)
    ensure_connector_nodes(h)
    mutated(h)
    return conn, warnings


def connector_exit_dir(conn: dict) -> list:
    """Direction the wires leave the connector (``exit_dir`` or the frame normal)."""
    return list(conn.get("exit_dir") or conn["frame"]["normal"])


def edit_connector(h: dict, connector_id: str, frame: Any = None,
                   exit_dir: Any = None) -> "tuple[dict, dict]":
    """Move or re-aim a connector; validates like :func:`add_connector`.

    ``frame`` may hold any of ``origin`` / ``normal`` / ``up``; omitted parts keep their value
    (a new ``normal`` without ``up`` re-derives ``up``); a changed frame drops the stored ``attach``
    face placement. Routes leave a connector along its old
    frame, so every segment touching it is removed and its wires become unrouted.
    Returns ``(connector, report)``; the report has ``changed``, ``removed_segments`` and
    ``unrouted_wires``.
    """
    conn = get_connector(h, connector_id)
    if frame is None and exit_dir is None:
        raise HarnessError("nothing_to_edit", "edit_connector needs frame and/or exit_dir",
                           hint="frame={origin, normal, up} (any subset) and/or exit_dir=[x, y, z]")
    new_frame = conn["frame"]
    if frame is not None:
        if not isinstance(frame, dict) or not frame or set(frame) - {"origin", "normal", "up"}:
            raise HarnessError("bad_frame", "frame must be a non-empty object with origin, normal and/or up")
        old = conn["frame"]
        up = frame["up"] if "up" in frame else (None if "normal" in frame else old["up"])
        new_frame = make_frame(frame.get("origin", old["origin"]), frame.get("normal", old["normal"]), up)
    new_exit = conn.get("exit_dir") if exit_dir is None else normalize_exit_dir(exit_dir)
    if new_frame == conn["frame"] and new_exit == conn.get("exit_dir"):
        return conn, {"changed": False, "removed_segments": [], "unrouted_wires": []}
    if new_frame != conn["frame"]:
        conn.pop("attach", None)  # the face placement no longer describes where the connector is
    conn["frame"] = new_frame
    if new_exit is not None:
        conn["exit_dir"] = new_exit
    report = unroute_connector(h, conn["id"])
    mutated(h)
    return conn, {"changed": True, **report}


def remove_connector(h: dict, connector_id: str) -> dict:
    """Delete a connector no wire references; returns the unroute report.

    Segments that only touch its node (e.g. a stale junction) are removed with it.
    """
    conn = get_connector(h, connector_id)
    users = [w["id"] for w in h["wires"]
             if conn["id"] in (w["from"]["connector"], w["to"]["connector"])]
    if users:
        raise HarnessError("connector_in_use", f"connector {conn['id']} is wired by {users}",
                           hint="remove those wires first (harness_edit action='remove_wire'), "
                                "then remove the connector", wires=users)
    report = unroute_connector(h, conn["id"])
    h["connectors"].remove(conn)
    h["nodes"][:] = [n for n in h["nodes"] if n["id"] != conn["id"]]
    mutated(h)
    return report


def parse_endpoint(h: dict, ep: Any, what: str) -> dict:
    """``{"connector": id, "pin": "3", "treatment"?}`` from a dict or a ``"J1:3"`` string."""
    treatment = None
    if isinstance(ep, str):
        if ":" not in ep:
            raise HarnessError("bad_endpoint", f"{what} {ep!r} must be 'connector:pin' (e.g. 'J1:3')")
        ck, pk = ep.rsplit(":", 1)
    elif isinstance(ep, dict) and "connector" in ep and "pin" in ep:
        ck, pk, treatment = ep["connector"], ep["pin"], ep.get("treatment")
    else:
        raise HarnessError("bad_endpoint", f"{what} must be {{'connector': id, 'pin': n}} or "
                           f"'J1:3'; got {ep!r}")
    conn = get_connector(h, str(ck).strip())
    pin = str(pk).strip()
    if not any(c["pin"] == pin for c in conn["cavities"]):
        raise HarnessError("unknown_pin", f"{what}: connector {conn['id']} has no pin {pin!r}",
                           hint=f"pins are 1..{conn['pins']}")
    out = {"connector": conn["id"], "pin": pin}
    if treatment is not None:
        t = str(treatment).strip().lower()
        if t not in END_TREATMENTS:
            raise HarnessError("bad_treatment", f"{what} treatment {treatment!r} is not one of "
                               f"{list(END_TREATMENTS)}")
        out["treatment"] = t
    return out


def add_wire(h: dict, *, from_: Any, to: Any, insulation_od_mm: Any = None,
             gauge_awg: Any = None, csa_mm2: Any = None, kind: str = "wire",
             color: Optional[str] = None, net: Optional[str] = None,
             signal: Optional[str] = None, group: Optional[str] = None,
             signal_class: Optional[str] = None, shield_ground_end: Optional[str] = None,
             service_loop_mm: Optional[float] = None, strip_mm: Optional[float] = None,
             min_bend_radius_mm: Optional[float] = None,
             bend_radius_x_od: Optional[float] = None, wire_id: Optional[str] = None,
             allow_shared_pin: bool = False, extra: Optional[dict] = None,
             insulation: Optional[str] = None, ribbon_width_mm: Optional[float] = None,
             ribbon_thickness_mm: Optional[float] = None) -> "tuple[dict, list]":
    """Add one wire. Returns ``(wire, warnings)``; refuses with :class:`HarnessError`."""
    warnings: list = []
    k = str(kind or "wire").strip().lower()
    if k not in WIRE_KINDS:
        raise HarnessError("bad_kind", f"kind {kind!r} is not one of {list(WIRE_KINDS)}")
    if k == "ribbon":
        try:
            width, thickness = float(ribbon_width_mm), float(ribbon_thickness_mm)
        except (TypeError, ValueError):
            raise HarnessError("ribbon_dimensions_required",
                               "ribbon_width_mm and ribbon_thickness_mm are required positive dimensions")
        if not all(math.isfinite(x) and x > 0 for x in (width, thickness)):
            raise HarnessError("ribbon_dimensions_required",
                               "ribbon_width_mm and ribbon_thickness_mm must be positive and finite")
        envelope = math.hypot(width, thickness)
        if insulation_od_mm is not None:
            try:
                supplied_od = float(insulation_od_mm)
            except (TypeError, ValueError):
                raise HarnessError("bad_od", "ribbon insulation_od_mm must match its circumscribed envelope")
            if not math.isfinite(supplied_od) or not math.isclose(supplied_od, envelope, rel_tol=1e-6, abs_tol=1e-6):
                raise HarnessError("bad_od", "ribbon insulation_od_mm must match hypot(ribbon_width_mm, ribbon_thickness_mm)")
        insulation_od_mm = envelope
        warnings.append("Ribbon routing uses a circumscribed circular envelope; orientation and rectangular sweep are not modeled.")
    elif ribbon_width_mm is not None or ribbon_thickness_mm is not None:
        raise HarnessError("bad_ribbon_dimensions", "ribbon dimensions apply only to kind='ribbon'")
    if insulation_od_mm is None:
        raise HarnessError(
            "insulation_od_required", "insulation_od_mm is required",
            hint="take the insulated outer diameter (mm) from the wire/cable datasheet; it is "
                 "wire-type specific so it cannot be derived from the AWG size")
    try:
        od = float(insulation_od_mm)
    except (TypeError, ValueError):
        raise HarnessError("bad_od", f"insulation_od_mm must be a positive number; got "
                           f"{insulation_od_mm!r}")
    if not math.isfinite(od) or od <= 0:
        raise HarnessError("bad_od", f"insulation_od_mm must be a positive number; got {od}")
    cond = conductor_diameter_mm(gauge_awg, csa_mm2)
    if cond is None:
        raise HarnessError("gauge_required", "supply gauge_awg or csa_mm2")
    if cond is not None and od + 1e-9 < cond:
        raise HarnessError(
            "od_below_conductor",
            f"insulation_od_mm={od} is smaller than the conductor diameter {cond:.3f} mm "
            f"of the stated gauge", hint="check the gauge and the datasheet OD")
    sc = None
    if signal_class is not None:
        sc = str(signal_class).strip().lower().replace("-", "_")
        if sc not in SIGNAL_CLASSES:
            raise HarnessError("bad_signal_class", f"signal_class {signal_class!r} is not one of "
                               f"{list(SIGNAL_CLASSES)}")
    sg = None
    if shield_ground_end is not None:
        sg = str(shield_ground_end).strip().lower()
        if sg not in SHIELD_ENDS:
            raise HarnessError("bad_shield_end", f"shield_ground_end {shield_ground_end!r} is not "
                               f"one of {list(SHIELD_ENDS)}",
                               hint="which end of the cable the shield/drain is grounded at")
    if sg is not None and k not in ("shielded", "coax") and sc != "shielded_drain":
        raise HarnessError("shield_on_unshielded",
                           "shield_ground_end only applies to kind='shielded'/'coax' or "
                           "signal_class='shielded_drain'")
    a = parse_endpoint(h, from_, "from")
    b = parse_endpoint(h, to, "to")
    if a["connector"] == b["connector"]:
        raise HarnessError("same_connector",
                           f"both ends are on connector {a['connector']}: a jumper inside one "
                           "connector has no routed length",
                           hint="model it as a jumper on the board/connector, not a harness wire")
    ca, cb_ = get_connector(h, a["connector"]), get_connector(h, b["connector"])
    wid = wire_id or next_id("w", h["wires"])
    if any(w["id"] == wid for w in h["wires"]):
        raise HarnessError("duplicate_wire", f"wire {wid!r} already exists")
    for ep, conn in ((a, ca), (b, cb_)):
        cav = next(c for c in conn["cavities"] if c["pin"] == ep["pin"])
        if cav.get("wire") and not allow_shared_pin:
            raise HarnessError(
                "pin_in_use", f"{conn['ref']} pin {ep['pin']} already carries wire {cav['wire']}",
                hint="one wire per cavity; pass allow_shared_pin=True only for a deliberate "
                     "two-wire crimp or splice")
    d = h["defaults"]
    extra_insulation = (extra or {}).get("insulation")
    if insulation is not None and extra_insulation is not None:
        if validate_insulation(insulation) != validate_insulation(extra_insulation):
            raise HarnessError("insulation_conflict", "insulation and extra.insulation disagree")
    material = validate_insulation(insulation if insulation is not None else extra_insulation)
    confidence = "explicit"
    if min_bend_radius_mm is not None:
        mbr = float(min_bend_radius_mm)
        if not math.isfinite(mbr) or mbr <= 0:
            raise HarnessError("bad_bend_radius", "min_bend_radius_mm must be positive")
        basis = "explicit centerline radius (caller supplied)"
    elif bend_radius_x_od is not None:
        x = float(bend_radius_x_od)
        if not math.isfinite(x) or x <= 0:
            raise HarnessError("bad_bend_radius", "bend_radius_x_od must be positive")
        mbr, basis = x * od, f"{x:g} x OD centerline (caller-stated multiple)"
    elif d.get("bend_radius_x_od") is not None:
        x = d["bend_radius_x_od"]
        mbr = x * od
        basis = f"{x:g} x OD centerline (caller-specified harness default)"
    else:
        mbr, basis, confidence = _wire_bend_default(
            {"kind": k, "insulation_od_mm": od, "insulation": material})
        warnings.append(f"wire {wid}: bend radius is a low-confidence static screening estimate; "
                        "verify cable datasheet, insulation and operating conditions")
    slp = d["service_loop_mm"] if service_loop_mm is None else float(service_loop_mm)
    stp = d["strip_mm"] if strip_mm is None else float(strip_mm)
    if slp < 0 or stp < 0 or not math.isfinite(slp + stp):
        raise HarnessError("bad_allowance", "service_loop_mm and strip_mm must be >= 0")
    wire: dict = {"id": wid, "from": a, "to": b, "insulation_od_mm": od, "kind": k,
                  "length_mm": None, "service_loop_mm": slp, "strip_mm": stp,
                  "min_bend_radius_mm": _r(mbr) if mbr is not None else None,
                  "bend_radius_basis": basis, "bend_radius_confidence": confidence, "segments": []}
    if k == "ribbon":
        wire["ribbon_width_mm"] = width
        wire["ribbon_thickness_mm"] = thickness
    if gauge_awg is not None:
        wire["gauge_awg"] = parse_awg(gauge_awg)
    if csa_mm2 is not None:
        wire["csa_mm2"] = float(csa_mm2)
    for key, val in (("color", color), ("net", net), ("signal", signal), ("group", group),
                     ("signal_class", sc), ("shield_ground_end", sg)):
        if val is not None and val != "":
            wire[key] = val if key in ("signal_class", "shield_ground_end") else str(val)
    for key in WIRE_PASSTHROUGH_KEYS:
        if key != "insulation" and extra and key in extra and extra[key] is not None:
            wire[key] = extra[key]
    if material is not None:
        wire["insulation"] = material
    h["wires"].append(wire)
    for ep, conn in ((a, ca), (b, cb_)):
        cav = next(c for c in conn["cavities"] if c["pin"] == ep["pin"])
        cav["wire"] = wid if not cav.get("wire") else f"{cav['wire']},{wid}"
        if not cav.get("signal") and (signal or net):
            cav["signal"] = str(signal or net)
    mutated(h)
    return wire, warnings


def remove_wire(h: dict, wire_id: str) -> dict:
    """Remove a wire and prune what only it used; returns the :func:`prune_graph` report."""
    w = get_wire(h, wire_id)
    h["wires"].remove(w)
    for c in h["connectors"]:
        for cav in c["cavities"]:
            ids = [x for x in str(cav.get("wire") or "").split(",") if x and x != wire_id]
            cav["wire"] = ",".join(ids) if ids else None
    report = prune_graph(h)
    mutated(h)
    return report


# ----------------------------------------------------------------------------- segment graph

def seg_length(seg: dict) -> float:
    return polyline_length(seg["path"])


def segment_fields(seg: dict) -> dict:
    """A segment's author-intent fields (overrides, sleeves...): everything but identity,
    geometry and what ``recompute_derived`` owns."""
    return {k: v for k, v in seg.items() if k not in ("id", "a", "b", "path") + SEGMENT_DERIVED}


def _adjacency(h: dict) -> "dict[str, list]":
    adj: dict = {}
    for s in sorted(h["segments"], key=lambda s: s["id"]):
        adj.setdefault(s["a"], []).append((s, s["b"], True))
        adj.setdefault(s["b"], []).append((s, s["a"], False))
    return adj


def tree_path(h: dict, a: str, b: str) -> "Optional[list]":
    """``[(segment, forward)]`` along the (unique, if acyclic) route ``a``→``b``; None if none."""
    if a == b:
        return []
    adj = _adjacency(h)
    if a not in adj or b not in adj:
        return None
    prev: dict = {a: None}
    queue = [a]
    while queue:
        cur = queue.pop(0)
        if cur == b:
            break
        for seg, other, fwd in adj.get(cur, []):
            if other not in prev:
                prev[other] = (cur, seg, fwd)
                queue.append(other)
    if b not in prev:
        return None
    out: list = []
    cur = b
    while prev[cur] is not None:
        p, seg, fwd = prev[cur]
        out.append((seg, fwd))
        cur = p
    out.reverse()
    return out


def components(h: dict) -> "dict[str, int]":
    """Connected-component index of every node (isolated nodes get their own)."""
    parent = {n["id"]: n["id"] for n in h["nodes"]}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for s in h["segments"]:
        for end in (s["a"], s["b"]):
            parent.setdefault(end, end)
        ra, rb = find(s["a"]), find(s["b"])
        if ra != rb:
            parent[ra] = rb
    roots: dict = {}
    out: dict = {}
    for nid in sorted(parent):
        r = find(nid)
        out[nid] = roots.setdefault(r, len(roots))
    return out


def find_cycle_segments(h: dict) -> list:
    """Ids of segments whose addition closed a loop (empty when the graph is a forest)."""
    parent: dict = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    bad: list = []
    for s in sorted(h["segments"], key=lambda s: s["id"]):
        ra, rb = find(s["a"]), find(s["b"])
        if ra == rb:
            bad.append(s["id"])
        else:
            parent[ra] = rb
    return bad


def add_segment(h: dict, a: str, b: str, path: list, **fields: Any) -> dict:
    """Append a segment between existing nodes; its end points are snapped to the nodes.

    Coordinates keep full precision: rounding a dense arc shifts its sampled bend radius, and
    the end points must equal the node positions exactly.
    """
    na, nb = get_node(h, a), get_node(h, b)
    if na is None or nb is None:
        raise HarnessError("unknown_node", f"segment endpoints {a!r}/{b!r} must be existing nodes")
    pts = dedupe_path([list(map(float, p)) for p in path])
    if len(pts) < 2:
        pts = [list(na["pos"]), list(nb["pos"])]
    pts[0] = list(na["pos"])
    pts[-1] = list(nb["pos"])
    pts = dedupe_path(pts)
    if len(pts) < 2:
        raise HarnessError("zero_length_segment", f"segment {a}->{b} has zero length")
    seg = {"id": next_id("s", h["segments"]), "a": a, "b": b, "path": pts, "od_mm": None,
           "min_bend_radius_mm": None, "wires": [], "length_mm": 0.0}
    seg.update(fields)
    h["segments"].append(seg)
    return seg


def split_segment_at(h: dict, seg_id: str, point: list, kind: str = "breakout",
                     tol: float = 1e-3) -> str:
    """Split a segment at the path point nearest ``point``; returns the node id there.

    A split within ``tol`` of an end returns that end node without splitting.
    """
    seg = get_segment(h, seg_id)
    path = seg["path"]
    _d, i, _t, q = closest_on_polyline(point, path)
    if vdist(q, path[0]) <= tol:
        return seg["a"]
    if vdist(q, path[-1]) <= tol:
        return seg["b"]
    nid = next_id("n", h["nodes"])
    pos = list(q)
    node = {"id": nid, "kind": kind, "pos": pos}
    h["nodes"].append(node)
    first = dedupe_path(path[:i + 1] + [pos])
    first[-1] = list(pos)  # dedupe may have dropped ``pos`` against a vertex within 1e-9
    second = dedupe_path([pos] + path[i + 1:])
    extra = segment_fields(seg)
    idx = h["segments"].index(seg)
    h["segments"].remove(seg)
    s1 = {"id": next_id("s", h["segments"]), "a": seg["a"], "b": nid, "path": first,
          "od_mm": None, "min_bend_radius_mm": None, "wires": [], "length_mm": 0.0, **extra}
    h["segments"].insert(idx, s1)
    s2 = {"id": next_id("s", h["segments"]), "a": nid, "b": seg["b"], "path": second,
          "od_mm": None, "min_bend_radius_mm": None, "wires": [], "length_mm": 0.0, **extra}
    h["segments"].insert(idx + 1, s2)
    # Provenance: a wire running between the two halves passes straight through the new node. The
    # halves also take this segment's place in the ``through`` of the nodes at its ends (an id the
    # first half may reuse must not be left pointing at the far end).
    node["through"] = [s1["id"], s2["id"]]
    for end_id, half in ((seg["a"], s1), (seg["b"], s2)):
        end = get_node(h, end_id)
        if end and seg["id"] in (end.get("through") or []):
            end["through"] = [half["id"] if x == seg["id"] else x for x in end["through"]]
    return nid


def snap_to_graph(h: dict, point: list, tol: float) -> "Optional[tuple[float, str, list]]":
    """Nearest graph feature within ``tol``: ``(dist, node_id_or_segment_id, point)``.

    A node within ``tol`` wins (returns its id); otherwise the nearest segment point.
    """
    best_node = None
    for n in sorted(h["nodes"], key=lambda n: n["id"]):
        d = vdist(point, n["pos"])
        if d <= tol and (best_node is None or d < best_node[0]):
            best_node = (d, n["id"], list(n["pos"]))
    if best_node:
        return best_node
    best = None
    for s in h["segments"]:
        d, _i, _t, q = closest_on_polyline(point, s["path"])
        if d <= tol and (best is None or d < best[0]):
            best = (d, s["id"], q)
    return best


def _routed_wire_ids(h: dict) -> list:
    return [w["id"] for w in h["wires"] if w.get("length_mm") is not None]


def _clean_through(h: dict) -> None:
    """Drop ``through`` from nodes whose two segments no longer both touch them (removed, merged
    away or otherwise stale), so a segment id that ``next_id`` reuses can never match old provenance."""
    ends: dict = {}
    for s in h["segments"]:
        for end in (s["a"], s["b"]):
            ends.setdefault(end, set()).add(s["id"])
    for n in h["nodes"]:
        through = n.get("through")
        if through is not None and not (len(set(through)) == 2 and set(through) <= ends.get(n["id"], set())):
            del n["through"]


def prune_graph(h: dict) -> dict:
    """Drop what no wire uses: wireless segments, orphaned non-connector nodes, and breakouts left
    with exactly the two halves of the split that made them (merged back into one segment when
    their non-derived fields are identical). A breakout left with a branch and one trunk piece is
    kept: merging them would turn the junction corner into a kink inside one bundle.

    Returns ``{removed_segments, removed_nodes, merged_segments}`` and leaves derived state fresh.
    """
    recompute_derived(h)
    removed_segments = [s["id"] for s in h["segments"] if not s["wires"]]
    h["segments"][:] = [s for s in h["segments"] if s["wires"]]
    merged: list = []
    for node in [n for n in h["nodes"] if n["kind"] == "breakout"]:
        touching = [s for s in h["segments"] if node["id"] in (s["a"], s["b"])]
        if len(touching) != 2:
            continue
        s1, s2 = touching
        if set(node.get("through") or ()) != {s1["id"], s2["id"]}:
            continue
        fields = [segment_fields(s) for s in touching]
        if fields[0] != fields[1]:
            continue
        p1, a = (s1["path"], s1["a"]) if s1["b"] == node["id"] else (s1["path"][::-1], s1["b"])
        p2, b = (s2["path"], s2["b"]) if s2["a"] == node["id"] else (s2["path"][::-1], s2["a"])
        if a == b or a == node["id"] or b == node["id"]:
            continue
        s1.update(a=a, b=b, path=dedupe_path(p1 + p2))
        far = get_node(h, b)  # s2 is gone: s1 now runs to the node s2 reached
        if far and s2["id"] in (far.get("through") or []):
            far["through"] = [s1["id"] if x == s2["id"] else x for x in far["through"]]
        h["segments"].remove(s2)
        h["nodes"].remove(node)
        merged.append({"node": node["id"], "kept": s1["id"], "dropped": s2["id"]})
    used = {end for s in h["segments"] for end in (s["a"], s["b"])}
    removed_nodes = [n["id"] for n in h["nodes"] if n["kind"] != "connector" and n["id"] not in used]
    h["nodes"][:] = [n for n in h["nodes"] if n["id"] not in removed_nodes]
    _clean_through(h)
    recompute_derived(h)
    return {"removed_segments": removed_segments, "removed_nodes": removed_nodes + [m["node"] for m in merged],
            "merged_segments": merged}


def remove_segments(h: dict, segment_ids: list) -> dict:
    """Delete segments (their wires become unrouted), then :func:`prune_graph`.

    Returns the prune report with every removed segment id and the newly ``unrouted_wires``.
    """
    recompute_derived(h)
    before = _routed_wire_ids(h)
    gone = set(segment_ids)
    removed = [s["id"] for s in h["segments"] if s["id"] in gone]
    h["segments"][:] = [s for s in h["segments"] if s["id"] not in gone]
    report = prune_graph(h)
    after = set(_routed_wire_ids(h))
    return {**report, "removed_segments": removed + report["removed_segments"],
            "unrouted_wires": [w for w in before if w not in after]}


def unroute_connector(h: dict, connector_id: str) -> dict:
    """Remove every segment touching a connector's node; its wires become unrouted."""
    return remove_segments(h, [s["id"] for s in h["segments"] if connector_id in (s["a"], s["b"])])


def remove_segments_of_wires(h: dict, wire_ids: list) -> dict:
    """Remove the segments carrying any of ``wire_ids`` so those wires can be routed afresh.

    Refuses (``reroute_shared_segment``) when such a segment also carries other wires.
    """
    recompute_derived(h)
    wanted = set(wire_ids)
    doomed = [s for s in h["segments"] if wanted & set(s["wires"])]
    shared = {w for s in doomed for w in s["wires"]} - wanted
    if shared:
        others = [w["id"] for w in h["wires"] if w["id"] in shared]
        raise HarnessError("reroute_shared_segment",
                           f"segments {[s['id'] for s in doomed if shared & set(s['wires'])]} also carry "
                           f"wires that were not selected: {others}",
                           hint="include those wires in wire_ids (or omit wire_ids to reroute all)",
                           wires=others)
    return remove_segments(h, [s["id"] for s in doomed])


def wire_route(h: dict, wire: dict) -> "tuple[list, list]":
    """The wire's centreline and its junctions: ``(points, junctions)``.

    ``points`` is the tree path from ``from`` to ``to`` as one polyline; ``junctions`` are the
    indices in it where the wire changes between two segments that are not the two halves of one
    split (a node's ``through``): passing straight through a breakout or clamp is not a junction,
    leaving along a branch is. Both are empty when the wire is unrouted.
    """
    path = tree_path(h, wire["from"]["connector"], wire["to"]["connector"])
    if not path:
        return [], []
    nodes = {n["id"]: n for n in h["nodes"]}
    pts: list = []
    junctions: list = []
    left = None  # (segment, node id) the wire has just reached the end of
    for seg, fwd in path:
        sp = seg["path"] if fwd else list(reversed(seg["path"]))
        if pts and {left[0]["id"], seg["id"]} != set((nodes.get(left[1]) or {}).get("through") or ()):
            junctions.append(len(pts) - 1)
        pts.extend(sp if not pts else sp[1:])
        left = (seg, seg["b"] if fwd else seg["a"])
    return pts, junctions


def _continuation(h: dict, seg: dict, node_id: str, reach: float, max_points: int) -> list:
    """Points leading away from ``node_id`` along the segment that continues ``seg`` through it,
    until ``max_points`` or ``reach`` mm.

    Only a node made by a split knows its through pair (``through``); a segment outside it (a
    branch), or any segment at a node without provenance, has no continuation. Guessing one from
    leg angles would hand a nearly tangent branch the trunk's points and turn its junction into a
    false kink.
    """
    through = (get_node(h, node_id) or {}).get("through")
    if not through or len(set(through)) != 2 or seg["id"] not in through:
        return []
    partner_id = next(x for x in through if x != seg["id"])
    partner = next((s for s in h["segments"] if s["id"] == partner_id), None)
    if partner is None or node_id not in (partner["a"], partner["b"]):
        return []
    pts = partner["path"] if partner["a"] == node_id else partner["path"][::-1]
    out: list = []
    walked, prev = 0.0, pts[0]
    for p in pts[1:]:
        walked += vdist(prev, p)
        out.append(list(p))
        prev = p
        if len(out) >= max_points or walked >= reach:
            break
    return out


def segment_check_path(h: dict, seg: dict, max_points: int = 64) -> "tuple[list, Optional[list]]":
    """A segment's path extended with context from its through-continuation, for a bend check.

    A segment cut out of a router arc (by a breakout or clamp inside it) starts or ends with a
    chord or two of that arc, which alone read as a kink. At each end node made by a split, the
    other half of the split (the node's ``through`` partner) lends its points, excluding the shared
    node, until ``max_points`` or a length of pi x the segment's own ``min_bend_radius_mm``. Only
    geometry is borrowed; the requirement stays the segment's. Returns ``(path, report_range)``:
    ``report_range`` is ``[first, last]``, the segment's own vertex indices inside the extended
    path, or None when there is no context.
    """
    path = [list(p) for p in seg["path"]]
    radius = seg.get("min_bend_radius_mm")
    if not radius or radius <= 0:
        return path, None
    reach = math.pi * radius
    before = _continuation(h, seg, seg["a"], reach, max_points)
    after = _continuation(h, seg, seg["b"], reach, max_points)
    if not before and not after:
        return path, None
    return before[::-1] + path + after, [len(before), len(before) + len(path) - 1]


def recompute_derived(h: dict) -> dict:
    """Rebuild wire routes, segment wire lists, bundle OD and bend radius from the tree.

    A wire with no connected connector pair keeps ``length_mm = None`` (never 0).
    """
    defaults = h.setdefault("defaults", {})
    for key, value in DEFAULTS.items():
        if key not in defaults:
            defaults[key] = value
    h.setdefault("packing_factor_basis", "estimate" if defaults["packing_factor"] == DEFAULT_PACKING_FACTOR
                 else "caller supplied (legacy)")
    for w in h["wires"]:
        if w.get("min_bend_radius_mm") is None:
            if defaults.get("bend_radius_x_od") is not None:
                w["min_bend_radius_mm"] = _r(defaults["bend_radius_x_od"] * w["insulation_od_mm"])
                w["bend_radius_basis"] = "caller-specified harness default, centerline"
                w["bend_radius_confidence"] = "explicit"
            else:
                radius, basis, confidence = _wire_bend_default(w)
                w.update(min_bend_radius_mm=_r(radius), bend_radius_basis=basis,
                         bend_radius_confidence=confidence)
        else:
            w.setdefault("bend_radius_confidence", "explicit")
    ensure_connector_nodes(h)
    for s in h["segments"]:
        s["wires"] = []
        s["length_mm"] = _r(seg_length(s), 3)
    by_id = {w["id"]: w for w in h["wires"]}
    for w in h["wires"]:
        path = tree_path(h, w["from"]["connector"], w["to"]["connector"])
        if not path:
            w["segments"] = []
            w["length_mm"] = None
            continue
        w["segments"] = [s["id"] for s, _f in path]
        w["length_mm"] = _r(sum(seg_length(s) for s, _f in path), 3)
        for s, _f in path:
            s["wires"].append(w["id"])
    pf = defaults["packing_factor"]
    for s in h["segments"]:
        members = [by_id[i] for i in s["wires"] if i in by_id]
        if members:
            s.update(bundle_requirements(members, pf, s))
        else:
            s["od_mm"] = None
            s["min_bend_radius_mm"] = None
            s.pop("od_basis", None)
            s.pop("bend_radius_basis", None)
            s.pop("bend_radius_confidence", None)
    return h


def assumptions(h: dict) -> dict:
    """Expose estimates and overrides without claiming workmanship compliance."""
    return {
        "packing_factor": h["defaults"].get("packing_factor", DEFAULT_PACKING_FACTOR),
        "packing_factor_basis": h.get("packing_factor_basis", "legacy unspecified"),
        "packing_sources": list(PACKING_SOURCES),
        "packing_scope": ("Estimated circular bundle, excluding coverings; each wire record is "
                          "counted as one item. Grouped or twisted assembly OD is not inferred; "
                          "override segment OD using a datasheet or measurement. Not a measured fit guarantee."),
        "bend_sources": [NASA_BEND_SOURCE, BEND_CONVENTION_SOURCE],
        "bend_scope": ("Installed static screening only. NASA's radius reference geometry is assumed "
                       "inside, following the IPC convention; centerline adds half OD. "
                       "Cable datasheets, actual insulation and dynamic flex can require larger radii."),
        "wires": [{"id": w["id"], "min_bend_radius_mm": w.get("min_bend_radius_mm"),
                   "basis": w.get("bend_radius_basis"), "confidence": w.get("bend_radius_confidence")}
                  for w in h["wires"]],
        "segments": [{"id": s["id"], "od_mm": s.get("od_mm"), "od_basis": s.get("od_basis"),
                      "min_bend_radius_mm": s.get("min_bend_radius_mm"),
                      "basis": s.get("bend_radius_basis"), "confidence": s.get("bend_radius_confidence"),
                      "override_packing_factor": s.get("override_packing_factor")}
                     for s in h["segments"]],
    }


# ----------------------------------------------------------------------------- derived values

def wire_cut_length_mm(w: dict) -> Optional[float]:
    """Cut length = routed + service loop + strip/termination allowance; None if unrouted."""
    if w.get("length_mm") is None:
        return None
    return _r(float(w["length_mm"]) + float(w.get("service_loop_mm") or 0.0)
              + float(w.get("strip_mm") or 0.0), 3)


def wire_gauge_label(w: dict) -> str:
    if w.get("gauge_awg") is not None:
        n = int(w["gauge_awg"])
        return f"{awg_label(n)} AWG"
    if w.get("csa_mm2") is not None:
        return f"{w['csa_mm2']:g} mm2"
    return "n/a"


def validate_harness(h: dict) -> dict:
    """Referential-integrity check: ``{errors: [...], warnings: [...]}`` (never raises)."""
    errors: list = []
    warnings: list = []
    if h.get("schema") != SCHEMA:
        errors.append(f"schema is {h.get('schema')!r}, expected {SCHEMA!r}")
    for coll in ("connectors", "wires", "nodes", "segments"):
        ids = [x.get("id") for x in h.get(coll, [])]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            errors.append(f"duplicate {coll} ids: {dup}")
    connectors = {c["id"]: c for c in h.get("connectors", [])}
    nids = {n["id"] for n in h.get("nodes", [])}
    for w in h.get("wires", []):
        for end in ("from", "to"):
            ep = w.get(end) or {}
            conn = connectors.get(ep.get("connector"))
            if conn is None:
                errors.append(f"wire {w.get('id')} {end} names unknown connector "
                              f"{ep.get('connector')!r}")
            elif not any(str(c.get("pin")) == str(ep.get("pin"))
                         for c in conn.get("cavities", [])):
                errors.append(f"wire {w.get('id')} {end} names unknown pin {ep.get('pin')!r} "
                              f"on connector {conn['id']}")
    for s in h.get("segments", []):
        for end in ("a", "b"):
            if s.get(end) not in nids:
                errors.append(f"segment {s.get('id')} {end}={s.get(end)!r} is not a node")
        if len(s.get("path") or []) < 2:
            errors.append(f"segment {s.get('id')} has fewer than two path points")
    nodes = {n["id"]: n for n in h.get("nodes", [])}
    for s in h.get("segments", []):
        p = s.get("path") or []
        for end, idx in (("a", 0), ("b", -1)):
            n = nodes.get(s.get(end))
            if n and p and vdist(p[idx], n["pos"]) > 0.05:
                errors.append(f"segment {s['id']} path end {end} is "
                              f"{vdist(p[idx], n['pos']):.3f} mm from node {n['id']}")
    loops = find_cycle_segments(h)
    if loops:
        errors.append(f"segments {loops} close a loop; a harness must be a tree "
                      "(wires follow the unique tree path between connectors)")
    return {"errors": errors, "warnings": warnings}


# ----------------------------------------------------------------------------- persistence

def _call(method: str, params: dict) -> dict:
    from services.mcp import core_bridge as cb  # lazy: loading it does not open the engine
    return cb.call(method, params)


def _studies(doc_id: str) -> list:
    res = _call("sim_study_list", {"doc_id": doc_id})
    if not isinstance(res, dict) or res.get("ok") is False or not isinstance(res.get("studies"), list):
        raise HarnessError("study_list_failed", "could not read the document's studies", detail=res)
    return [s for s in res["studies"] if isinstance(s, dict)]


def harness_studies(doc_id: str) -> list:
    return [s for s in _studies(doc_id) if s.get("type") == STUDY_TYPE]


def list_harnesses(doc_id: str) -> list:
    """``[{id, name, connectors, wires, segments}]`` of every harness saved in the document."""
    out = []
    for s in harness_studies(doc_id):
        h = (s.get("params") or {}).get("harness") or {}
        out.append({"id": s.get("name") or h.get("id"), "name": h.get("name"),
                    "connectors": len(h.get("connectors") or []),
                    "wires": len(h.get("wires") or []),
                    "segments": len(h.get("segments") or [])})
    return out


def load_harness(doc_id: str, harness_id: Optional[str] = None) -> dict:
    """Deep copy of the saved harness. ``harness_id`` may be omitted when exactly one exists."""
    studies = harness_studies(doc_id)
    names = [s.get("name") for s in studies]
    if harness_id is None:
        if len(studies) == 1:
            hit = studies[0]
        else:
            raise HarnessError(
                "harness_ambiguous" if studies else "no_harness",
                f"document {doc_id} has {len(studies)} harness(es); name one"
                if studies else f"document {doc_id} has no harness",
                hint="harness_new(doc_id, name) creates one" if not studies
                else "pass harness_id=<one of harnesses>", harnesses=names)
    else:
        hit = next((s for s in studies if s.get("name") == harness_id), None)
        if hit is None:
            raise HarnessError("unknown_harness", f"no harness {harness_id!r} in document {doc_id}",
                               hint="harness_new(doc_id, name) creates one; or pick an existing id",
                               harnesses=names)
    h = copy.deepcopy((hit.get("params") or {}).get("harness"))
    if not isinstance(h, dict) or h.get("schema") != SCHEMA:
        raise HarnessError("bad_harness_study", f"study {hit.get('id')!r} does not hold an {SCHEMA} "
                           "harness")
    for key, empty in (("connectors", []), ("wires", []), ("nodes", []), ("segments", []),
                       ("keepouts", []), ("loads", []), ("geometry", {}), ("analyses", {})):
        h.setdefault(key, copy.deepcopy(empty))
    recompute_derived(h)
    return h


def save_harness(doc_id: str, harness: dict, results: Optional[dict] = None) -> dict:
    """Persist ``harness`` into the document's ``harness`` study (replacing the same id).

    ``results`` (optional) is merged into ``harness["analyses"]``. Unlike the best-effort
    solver studies this RAISES :class:`HarnessError` when the write fails: a harness edit that
    was not saved must never be reported as done.
    """
    check = validate_harness(harness)
    if check["errors"]:
        raise HarnessError("invalid_harness", "; ".join(check["errors"][:5]),
                           hint="fix the listed problems before saving", errors=check["errors"])
    if results:
        harness.setdefault("analyses", {}).update(results)
    summary = summarize(harness)
    rec = {"type": STUDY_TYPE, "name": harness["id"], "engine": STUDY_ENGINE,
           "params": {"schema": SCHEMA, "harness": harness}, "results": {"summary": summary}}
    try:
        existing = next((s for s in _studies(doc_id)
                         if s.get("type") == STUDY_TYPE and s.get("name") == harness["id"]), None)
        if existing and existing.get("id"):
            rec["id"] = existing["id"]
            res = _call("sim_study_edit", {"doc_id": doc_id, "study_id": existing["id"],
                                           "study": rec})
        else:
            res = _call("sim_study_add", {"doc_id": doc_id, "study": rec})
    except Exception as exc:  # noqa: BLE001 — surfaced, never swallowed
        raise HarnessError("not_saved", f"harness {harness['id']!r} could not be written into "
                           f"document {doc_id}: {exc}",
                           hint="the edit was NOT persisted; check the document is open "
                                "read-write and retry") from exc
    if not isinstance(res, dict) or res.get("ok") is False or not res.get("study_id"):
        raise HarnessError("not_saved", f"document did not confirm saving harness {harness['id']!r}",
                           hint="check document write access and retry", detail=res)
    return {"doc_id": doc_id, "study_id": res["study_id"],
            "revision": res.get("revision"), "saved_to_study": True}


def delete_harness(doc_id: str, harness_id: str) -> bool:
    for s in harness_studies(doc_id):
        if s.get("name") == harness_id and s.get("id"):
            res = _call("sim_study_delete", {"doc_id": doc_id, "study_id": s["id"]})
            if isinstance(res, dict) and res.get("ok") is False:
                raise HarnessError("not_deleted", f"document did not delete harness {harness_id!r}",
                                   hint="check document write access and retry", detail=res)
            return True
    return False


# ----------------------------------------------------------------------------- summaries

def summarize(h: dict) -> dict:
    routed = [w for w in h["wires"] if w.get("length_mm") is not None]
    return {"connectors": len(h["connectors"]), "wires": len(h["wires"]),
            "routed_wires": len(routed), "unrouted_wires": len(h["wires"]) - len(routed),
            "segments": len(h["segments"]), "nodes": len(h["nodes"]),
            "total_routed_wire_length_mm": _r(sum(w["length_mm"] for w in routed), 3)}


def connector_pin_table(h: dict, connector_id: str) -> list:
    """Per-pin rows ``{pin, signal, wire, signal_class, color, gauge, to_connector, to_pin}``."""
    conn = get_connector(h, connector_id)
    by_id = {w["id"]: w for w in h["wires"]}
    rows = []
    for cav in conn["cavities"]:
        wids = [x for x in str(cav.get("wire") or "").split(",") if x]
        if not wids:
            rows.append({"pin": cav["pin"], "signal": cav.get("signal"), "wire": None,
                         "signal_class": None, "color": None, "gauge": None,
                         "to_connector": None, "to_pin": None})
            continue
        for wid in wids:
            w = by_id.get(wid)
            if w is None:
                continue
            far = w["to"] if w["from"]["connector"] == conn["id"] and w["from"]["pin"] == cav["pin"] \
                else w["from"]
            far_conn = next((c for c in h["connectors"] if c["id"] == far["connector"]), None)
            rows.append({"pin": cav["pin"], "signal": cav.get("signal") or w.get("signal")
                         or w.get("net"), "wire": wid, "signal_class": w.get("signal_class"),
                         "color": w.get("color"), "gauge": wire_gauge_label(w),
                         "to_connector": (far_conn or {}).get("ref") or far["connector"],
                         "to_pin": far["pin"]})
    return rows


def connector_from_to(h: dict, connector_id: str) -> list:
    """Grouped per-connector from/to list: one row per far connector with its wire/pin pairs."""
    conn = get_connector(h, connector_id)
    groups: dict = {}
    for row in connector_pin_table(h, connector_id):
        if row["wire"] is None:
            continue
        g = groups.setdefault(row["to_connector"], {"to": row["to_connector"], "wires": []})
        g["wires"].append({"wire": row["wire"], "pin": row["pin"], "to_pin": row["to_pin"],
                           "signal": row["signal"]})
    return [{"connector": conn["ref"], **g} for _k, g in sorted(groups.items(), key=lambda kv: str(kv[0]))]


def build_report(h: dict) -> dict:
    """Cut list, per-wire lengths, connector tables and BOM rows (pure, bounded by the model)."""
    recompute_derived(h)
    rows: list = []
    unrouted: list = []
    relying: list = []
    for w in h["wires"]:
        cut = wire_cut_length_mm(w)
        if cut is None:
            unrouted.append(w["id"])
        if not w.get("service_loop_mm") and not w.get("strip_mm"):
            relying.append(w["id"])
        ca = next((c for c in h["connectors"] if c["id"] == w["from"]["connector"]), {})
        cb_ = next((c for c in h["connectors"] if c["id"] == w["to"]["connector"]), {})
        rows.append({"wire": w["id"],
                     "from": f"{ca.get('ref', w['from']['connector'])}:{w['from']['pin']}",
                     "to": f"{cb_.get('ref', w['to']['connector'])}:{w['to']['pin']}",
                     "gauge": wire_gauge_label(w), "color": w.get("color"), "kind": w["kind"],
                     "signal_class": w.get("signal_class"), "net": w.get("net"),
                     "insulation_od_mm": w["insulation_od_mm"],
                     "routed_length_mm": w.get("length_mm"),
                     "service_loop_mm": w.get("service_loop_mm", 0.0),
                     "strip_mm": w.get("strip_mm", 0.0), "cut_length_mm": cut,
                     "from_treatment": w["from"].get("treatment"),
                     "to_treatment": w["to"].get("treatment")})
    stock: dict = {}
    for r, w in zip(rows, h["wires"]):
        if r["cut_length_mm"] is None:
            continue
        key = (r["gauge"], r["color"], r["kind"], r["insulation_od_mm"])
        e = stock.setdefault(key, {"gauge": r["gauge"], "color": r["color"], "kind": r["kind"],
                                   "insulation_od_mm": r["insulation_od_mm"], "count": 0,
                                   "total_cut_length_mm": 0.0})
        e["count"] += 1
        e["total_cut_length_mm"] = _r(e["total_cut_length_mm"] + r["cut_length_mm"], 3)
    conns: dict = {}
    for c in h["connectors"]:
        key = (c["family"], c.get("part_number"), c["pins"])
        e = conns.setdefault(key, {"family": c["family_label"], "part_number": c.get("part_number"),
                                   "pins": c["pins"], "quantity": 0, "refs": []})
        e["quantity"] += 1
        e["refs"].append(c["ref"])
    terminations: dict = {}
    for w in h["wires"]:
        for end in ("from", "to"):
            t = w[end].get("treatment") or "unspecified"
            key = (t, wire_gauge_label(w))
            terminations[key] = terminations.get(key, 0) + 1
    warnings: list = []
    if unrouted:
        warnings.append(f"{len(unrouted)} wire(s) are not routed — cut length unknown: "
                        f"{unrouted[:20]}")
    if relying:
        warnings.append(f"{len(relying)} wire(s) have service_loop_mm = strip_mm = 0 (not "
                        f"specified): {relying[:20]} — cut length is routed length only")
    return {
        "wires": rows,
        "connectors": [{"id": c["id"], "ref": c["ref"], "family": c["family_label"],
                        "part_number": c.get("part_number"), "pins": c["pins"],
                        "pinout": connector_pin_table(h, c["id"]),
                        "from_to": connector_from_to(h, c["id"])} for c in h["connectors"]],
        "cut_list": sorted(stock.values(), key=lambda e: (str(e["gauge"]), str(e["color"]))),
        "bom": {
            "connector_housings": sorted(conns.values(), key=lambda e: e["refs"][0]),
            "wire_stock": sorted(stock.values(), key=lambda e: (str(e["gauge"]), str(e["color"]))),
            "wire_terminations": [{"treatment": k[0], "gauge": k[1], "count": n}
                                  for k, n in sorted(terminations.items())],
            "note": "terminal/contact/ferrule part numbers depend on the connector series and "
                    "wire size and are not inferred; fill them from the connector datasheet",
        },
        "summary": summarize(h),
        "assumptions": [
            "cut length = routed length + service_loop_mm + strip_mm (strip_mm = total "
            "termination allowance, both ends)",
            f"bundle OD uses area packing factor {h['defaults']['packing_factor']!r}; "
            "diameter is an estimate, not a measured clearance guarantee",
        ],
        "design_assumptions": assumptions(h),
        "warnings": warnings,
        "unrouted_wires": unrouted,
    }
