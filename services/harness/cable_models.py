"""Per-length electrical models of harness cable: R, L, C, Z0, propagation delay.

Closed forms for the three geometries a harness actually uses, plus datasheet values
supplied by the caller (which always win over geometry):

* **two-wire line** (twisted pair, parallel pair, ribbon pair) - Pozar, *Microwave
  Engineering* 4e, Table 2.1 "Parameters for some common transmission lines"
  (also Ramo, Whinnery & Van Duzer, *Fields and Waves in Communication Electronics*):
  ``L' = (mu/pi) acosh(s/d)``, ``C' = pi eps / acosh(s/d)``, ``R' = 2 Rs/(pi d)``
  with ``Rs = sqrt(pi f mu0 rho)``; ``Z0 = (eta0/(pi sqrt(er_eff))) acosh(s/d)``.
* **coax** - Pozar Table 2.1: ``L' = (mu/2pi) ln(b/a)``, ``C' = 2 pi eps / ln(b/a)``,
  ``R' = Rs/(2 pi) (1/a + 1/b)``; ``Z0 = (eta0/(2 pi sqrt(er))) ln(b/a)``.
* **twist** - a helix of diameter ``s`` and lay ``P`` is longer than the cable by
  ``sqrt(1 + (pi s / P)^2)`` (plain helix geometry); R', L', C' and delay per cable
  metre are scaled by it, Z0 is unchanged.

The two-wire formulas assume ONE homogeneous dielectric. A real insulated pair has
field partly in air and partly in insulation, so the effective permittivity lies
between 1 and the insulation permittivity. Unless the caller supplies ``er_eff`` (or
datasheet values) this module returns that RANGE (``lo``/``hi``) instead of a single
number, and ``value`` is None - a caller can still prove a failure from the bound that
is unfavourable, but can never claim a pass from the favourable one.

Insulation permittivities are only tabulated for materials with a cited value
(Pozar App. B: polyethylene 2.25, PTFE/Teflon 2.08). PVC and friends vary by
formulation and frequency, so they must be supplied.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from services.harness.wire_data import RHO_CU_20_OHM_MM2_PER_M, NotTabulated

MU0 = 4.0e-7 * math.pi            # H/m
EPS0 = 8.8541878128e-12           # F/m
C0 = 299792458.0                  # m/s
ETA0 = math.sqrt(MU0 / EPS0)      # ohm, ~376.73
_RHO_CU_OHM_M = RHO_CU_20_OHM_MM2_PER_M * 1e-6

#: Insulation relative permittivity with a cited source: name -> (er, citation).
INSULATION_ER: dict[str, tuple[float, str]] = {
    "air": (1.0, "vacuum/air"),
    "polyethylene": (2.25, "Pozar, Microwave Engineering 4e, App. B (polyethylene)"),
    "pe": (2.25, "Pozar, Microwave Engineering 4e, App. B (polyethylene)"),
    "ptfe": (2.08, "Pozar, Microwave Engineering 4e, App. B (Teflon/PTFE)"),
    "teflon": (2.08, "Pozar, Microwave Engineering 4e, App. B (Teflon/PTFE)"),
}

KINDS = ("twisted_pair", "parallel", "ribbon", "coax")

#: Wire-kind (harness C4 `wires[].kind`) -> default cable geometry kind.
_WIRE_KIND_TO_CABLE = {"twisted_pair": "twisted_pair", "wire": "parallel"}


def skin_depth_m(f_hz: float, rho_ohm_m: float = _RHO_CU_OHM_M) -> float:
    return math.sqrt(rho_ohm_m / (math.pi * f_hz * MU0))


def surface_resistance_ohm(f_hz: float, rho_ohm_m: float = _RHO_CU_OHM_M) -> float:
    """``Rs = sqrt(pi f mu0 rho)`` (Pozar eq. 1.130)."""
    return math.sqrt(math.pi * f_hz * MU0 * rho_ohm_m)


def twist_length_factor(s_mm: float, lay_mm: Optional[float]) -> float:
    """Wire path length / cable length for a pair twisted with lay ``lay_mm`` (1.0 if untwisted)."""
    if not lay_mm:
        return 1.0
    if lay_mm <= 0:
        raise NotTabulated("twist_lay_mm must be positive")
    return math.sqrt(1.0 + (math.pi * s_mm / lay_mm) ** 2)


def _num(spec: dict, key: str, positive: bool = True) -> Optional[float]:
    v = spec.get(key)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError) as exc:
        raise NotTabulated(f"{key} must be a number, got {v!r}") from exc
    if not math.isfinite(f) or (positive and f <= 0):
        raise NotTabulated(f"{key} must be a positive finite number, got {v!r}")
    return f


def _complete_lossless(c: Optional[float], l: Optional[float], z0: Optional[float],
                       td: Optional[float]) -> tuple[Optional[float], ...]:
    """Fill (C, L, Z0, td) per metre from any two of them (lossless line)."""
    c, l, z0, td = c, l, z0, td
    if c is not None and l is not None:
        z0 = math.sqrt(l / c); td = math.sqrt(l * c)
    elif c is not None and z0 is not None:
        td = z0 * c; l = z0 * z0 * c
    elif c is not None and td is not None:
        z0 = td / c; l = td * td / c
    elif l is not None and z0 is not None:
        c = l / (z0 * z0); td = l / z0
    elif l is not None and td is not None:
        c = td * td / l; z0 = l / td
    elif z0 is not None and td is not None:
        c = td / z0; l = z0 * td
    return c, l, z0, td


def _two_wire(d_mm: float, s_mm: float, er: float) -> dict:
    if s_mm <= d_mm:
        raise NotTabulated(
            f"two-wire geometry needs centre spacing > conductor diameter (s={s_mm:g} mm, d={d_mm:g} mm)")
    k = math.acosh(s_mm / d_mm)
    c = math.pi * EPS0 * er / k                       # F/m
    l = MU0 / math.pi * k                             # H/m (external, high-frequency)
    z0 = ETA0 / (math.pi * math.sqrt(er)) * k
    td = math.sqrt(er) / C0
    return {"c_f_per_m": c, "l_h_per_m": l, "z0_ohm": z0, "td_s_per_m": td}


def _coax(d_mm: float, big_d_mm: float, er: float) -> dict:
    if big_d_mm <= d_mm:
        raise NotTabulated("coax dielectric_od_mm must exceed the centre conductor diameter")
    k = math.log(big_d_mm / d_mm)
    return {"c_f_per_m": 2.0 * math.pi * EPS0 * er / k, "l_h_per_m": MU0 / (2.0 * math.pi) * k,
            "z0_ohm": ETA0 / (2.0 * math.pi * math.sqrt(er)) * k, "td_s_per_m": math.sqrt(er) / C0}


def evaluate_cable(spec: Optional[dict], conductor: Optional[dict] = None,
                   wire: Optional[dict] = None, f_hz: Optional[float] = None) -> dict:
    """Per-metre electrical model of one signal cable.

    ``spec`` (all optional, mm / ohm / pF / ns):
      ``kind`` twisted_pair | parallel | ribbon | coax (default from the wire kind);
      ``conductor_diameter_mm`` (default: the wire's resolved conductor diameter);
      ``spacing_mm`` centre-to-centre (default: the wire's insulation OD, i.e. touching);
      ``insulation`` material name from :data:`INSULATION_ER` or ``er`` (insulation permittivity);
      ``er_eff`` explicit effective permittivity (collapses the bounds to one value);
      ``twist_lay_mm``; ``dielectric_od_mm`` (coax); ``shield_r_ohm_per_m`` (coax);
      ``datasheet``: {capacitance_pf_per_m, inductance_uh_per_m, z0_ohm, delay_ns_per_m,
      velocity_factor, resistance_ohm_per_km} - any two of C/L/Z0/delay complete the rest.

    Returns ``{kind, geometry, r_dc_ohm_per_m, loop_r_dc_ohm_per_m, r_hf_ohm_per_m,
    c_pf_per_m:{lo,hi,value}, l_nh_per_m, z0_ohm:{lo,hi,value}, td_ns_per_m:{lo,hi,value},
    bounded:bool, basis:[...], citations:[...], confidence, limitations:[...]}`` where
    ``value`` is None while the permittivity is only bounded.
    """
    spec = dict(spec or {})
    wire = wire or {}
    basis: list[str] = []
    cites: list[str] = []
    limits: list[str] = []
    ds = spec.get("datasheet") or {}

    r_dc = None
    if _num(ds, "resistance_ohm_per_km") is not None:
        r_dc = _num(ds, "resistance_ohm_per_km") / 1000.0
        basis.append("conductor resistance from datasheet")
    elif conductor is not None:
        r_dc = conductor["r20_ohm_per_km"] / 1000.0
        basis.append(f"conductor resistance at 20 C: {conductor['basis']}")

    kind = spec.get("kind") or _WIRE_KIND_TO_CABLE.get(str(wire.get("kind") or ""), None)
    c_ds, z_ds = _num(ds, "capacitance_pf_per_m"), _num(ds, "z0_ohm")
    l_ds, td_ds = _num(ds, "inductance_uh_per_m"), _num(ds, "delay_ns_per_m")
    vf = _num(ds, "velocity_factor")
    if vf is not None:
        if vf > 1.0:
            raise NotTabulated("datasheet velocity_factor must be <= 1")
        td_ds = 1e9 / (vf * C0)
    have = [x for x in (c_ds, l_ds, z_ds, td_ds) if x is not None]
    twist = 1.0
    geom: dict[str, Any] = {}
    out: dict[str, Any] = {"kind": kind or "datasheet", "geometry": geom, "limitations": limits}

    if have:
        c, l, z0, td = _complete_lossless(
            None if c_ds is None else c_ds * 1e-12,
            None if l_ds is None else l_ds * 1e-6,
            z_ds, None if td_ds is None else td_ds * 1e-9)
        supplied = (None if c_ds is None else c_ds * 1e-12,
                    None if l_ds is None else l_ds * 1e-6,
                    z_ds, None if td_ds is None else td_ds * 1e-9)
        calculated = (c, l, z0, td)
        if any(a is not None and b is not None and not math.isclose(a, b, rel_tol=0.05)
               for a, b in zip(supplied, calculated)):
            raise NotTabulated("datasheet C/L/Z0/delay values are inconsistent with the lossless-line "
                               "model; supply mutually consistent values or only the needed quantities")
        basis.append("caller-supplied datasheet values; missing values derived only where possible "
                     "using lossless-line relations")
        out.update(_pack(c, l, z0, td, bounded=False, twist=1.0))
        out["confidence"] = "medium"
        limits.append("frequency dependence and distributed losses are not modelled; missing quantities stay unknown")
    elif kind in ("twisted_pair", "parallel", "ribbon"):
        d = _num(spec, "conductor_diameter_mm")
        if d is None:
            if conductor is None:
                raise NotTabulated("conductor_diameter_mm is required (no resolved conductor)")
            d = conductor["diameter_mm"]
            basis.append("conductor diameter from the resolved gauge (solid-equivalent)")
        s = _num(spec, "spacing_mm")
        if s is None:
            od = wire.get("insulation_od_mm")
            if not od:
                raise NotTabulated("spacing_mm is required (the wire has no insulation_od_mm)")
            s = float(od)
            basis.append("centre spacing = insulation OD (insulated conductors touching)")
        geom.update({"conductor_diameter_mm": d, "spacing_mm": s})
        twist = twist_length_factor(s, _num(spec, "twist_lay_mm")) if kind == "twisted_pair" else 1.0
        if twist != 1.0:
            geom["twist_length_factor"] = twist
            basis.append("per-metre values scaled by the helix length factor")
        er_eff = _num(spec, "er_eff")
        er_ins = _num(spec, "er")
        if er_ins is None and spec.get("insulation"):
            key = str(spec["insulation"]).strip().lower()
            if key not in INSULATION_ER:
                raise NotTabulated(
                    f"insulation {spec['insulation']!r} has no cited permittivity; supply er (the "
                    "insulation's datasheet relative permittivity) or er_eff or datasheet capacitance")
            er_ins, cite = INSULATION_ER[key]
            cites.append(cite)
        if er_ins is not None and er_ins < 1.0:
            raise NotTabulated("insulation relative permittivity must be >= 1")
        cites.append("Pozar, Microwave Engineering 4e, Table 2.1 (parallel-wire line)")
        if er_eff is not None:
            if er_eff < 1.0:
                raise NotTabulated("er_eff must be >= 1")
            r = _two_wire(d, s, er_eff)
            basis.append("homogeneous-dielectric two-wire formula at the supplied er_eff")
            out.update(_pack(r["c_f_per_m"], r["l_h_per_m"], r["z0_ohm"], r["td_s_per_m"], False, twist))
            out["confidence"] = "medium"
        else:
            lo = _two_wire(d, s, 1.0)
            if er_ins is not None:
                hi = _two_wire(d, s, er_ins)
                basis.append("er_eff bounded between air (1) and the insulation permittivity "
                             f"({er_ins:g}); supply er_eff or datasheet values to collapse the range")
                out.update(_pack_range(lo, hi, twist))
            else:
                basis.append("er_eff unknown: only the air-dielectric bound (er_eff=1) is available; "
                             "the capacitance can only be BOUNDED BELOW - supply insulation er/er_eff "
                             "or datasheet capacitance to bound it above")
                out.update(_pack_range(lo, None, twist))
            out["confidence"] = "low"
        limits.append("neighbouring-conductor coupling and bundle proximity effect are not modelled")
    elif kind == "coax":
        d = _num(spec, "conductor_diameter_mm")
        if d is None and conductor is not None:
            d = conductor["diameter_mm"]
        big_d = _num(spec, "dielectric_od_mm")
        if d is None or big_d is None:
            raise NotTabulated("coax needs conductor_diameter_mm and dielectric_od_mm (or datasheet values)")
        er = _num(spec, "er")
        if er is None and spec.get("insulation"):
            key = str(spec["insulation"]).strip().lower()
            if key not in INSULATION_ER:
                raise NotTabulated(f"insulation {spec['insulation']!r} has no cited permittivity; supply er")
            er, cite = INSULATION_ER[key]
            cites.append(cite)
        if er is None:
            raise NotTabulated("coax needs the dielectric permittivity er (or insulation material)")
        if er < 1.0:
            raise NotTabulated("coax dielectric relative permittivity must be >= 1")
        geom.update({"conductor_diameter_mm": d, "dielectric_od_mm": big_d, "er": er})
        r = _coax(d, big_d, er)
        cites.append("Pozar, Microwave Engineering 4e, Table 2.1 (coaxial line)")
        basis.append("coax closed form with the given dielectric")
        out.update(_pack(r["c_f_per_m"], r["l_h_per_m"], r["z0_ohm"], r["td_s_per_m"], False, 1.0))
        out["confidence"] = "high"
        sh = _num(spec, "shield_r_ohm_per_m", positive=False)
        if sh is not None:
            if sh < 0:
                raise NotTabulated("shield_r_ohm_per_m must be nonnegative")
            geom["shield_r_ohm_per_m"] = sh
    else:
        raise NotTabulated(
            "cable kind unknown: set cable.kind to twisted_pair/parallel/ribbon/coax or supply "
            "cable.datasheet with two of capacitance_pf_per_m / inductance_uh_per_m / z0_ohm / "
            "delay_ns_per_m (plain 'wire' is treated as parallel; 'shielded' needs a datasheet)")

    out["basis"] = basis
    out["citations"] = cites
    r_conductor = None if r_dc is None else r_dc * twist
    out["r_dc_ohm_per_m"] = r_conductor
    if kind == "coax":
        shield_r = _num(spec, "shield_r_ohm_per_m", positive=False)
        out["loop_r_dc_ohm_per_m"] = (
            None if r_conductor is None or shield_r is None else r_conductor + shield_r)
        if shield_r is None:
            limits.append("coax shield DC resistance is unknown; loop resistance is unavailable")
    else:
        out["loop_r_dc_ohm_per_m"] = None if r_conductor is None else 2.0 * r_conductor
    out["r_hf_ohm_per_m"] = None
    if f_hz and "conductor_diameter_mm" in geom:
        d_m = geom["conductor_diameter_mm"] * 1e-3
        r_skin = surface_resistance_ohm(f_hz) / (math.pi * d_m) * twist
        out["r_hf_ohm_per_m"] = max(r_skin, r_conductor or 0.0)
        out["skin_depth_um"] = skin_depth_m(f_hz) * 1e6
        out["f_hz"] = f_hz
    return out


def _band(lo: Optional[float], hi: Optional[float], value: Optional[float]) -> dict:
    return {"lo": lo, "hi": hi, "value": value}


def _pack(c: Optional[float], l: Optional[float], z0: Optional[float], td: Optional[float],
          bounded: bool, twist: float) -> dict:
    cp = None if c is None else c * 1e12 * twist
    return {
        "bounded": bounded,
        "c_pf_per_m": _band(cp, cp, cp),
        "l_nh_per_m": None if l is None else l * 1e9 * twist,
        "z0_ohm": _band(z0, z0, z0),
        "td_ns_per_m": _band(*(3 * [None if td is None else td * 1e9 * twist])),
    }


def _pack_range(lo: dict, hi: Optional[dict], twist: float) -> dict:
    """er_eff in [1, er_ins]: C and td rise with er, Z0 falls."""
    c_lo = lo["c_f_per_m"] * 1e12 * twist
    c_hi = None if hi is None else hi["c_f_per_m"] * 1e12 * twist
    td_lo = lo["td_s_per_m"] * 1e9 * twist
    td_hi = None if hi is None else hi["td_s_per_m"] * 1e9 * twist
    return {
        "bounded": True,
        "c_pf_per_m": _band(c_lo, c_hi, None),
        "l_nh_per_m": lo["l_h_per_m"] * 1e9 * twist,
        "z0_ohm": _band(None if hi is None else hi["z0_ohm"], lo["z0_ohm"], None),
        "td_ns_per_m": _band(td_lo, td_hi, None),
    }
