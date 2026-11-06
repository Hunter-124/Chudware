"""Wire sizing for a harness: current, voltage drop, conductor temperature, ampacity, protection.

``analyze(harness, ...)`` takes an ``aicad.harness/1`` dict (connectors / wires / segments, see
:mod:`services.harness.model`) plus loads, and returns per-wire results, per-segment bundle
results, findings with VERIFIED fixes, and an honest coverage statement. It is a pure function
of its inputs: nothing is persisted and no engine kernel is needed.

What it computes, and from what
-------------------------------
* **Resistance** - :func:`services.harness.wire_data.resolve_conductor` (ASTM B258 AWG,
  IEC 60228 metric, or a datasheet value), referred to the conductor temperature with
  ``alpha_20 = 0.00393 /C``.
* **Current** - from explicit loads (per wire or per net) and optionally an
  ``aicad.powerbudget/1`` rail table. A load has a continuous current, an optional true-RMS
  current, and an optional peak (motor inrush / stall) with its duration or duty.
* **Round-trip drop** - forward wire plus its return conductor when the return path is in the
  harness (a ground wire between the same two connectors, or an explicit ``return_wire``).
  Voltage is reported AT THE LOAD END for continuous and peak current.
* **Conductor temperature** - coupled steady Joule heating I²R(T), radial conduction through
  each wire's insulation, and natural convection (Churchill & Chu 1975 horizontal cylinder)
  and radiation from an idealized shared outer surface. The conductor is treated as a solid
  equivalent-area cylinder. Insulation material properties can be selected or supplied explicitly.
  Bundle contact/void resistance, enclosure effects and external heating are omitted.
* **Ampacity** - tabulated ratings are installation-specific screening references. The thermal
  model limit is an estimate, not a certified or qualified ampacity or protection rating.
* **Peak current** - the adiabatic conductor temperature rise of a pulse (closed form, copper
  density/heat capacity from Incropera Table A.1) and, with a fuse ``i2t_a2s``, a nuisance-blow
  check. No time-current curve is invented.

Never invented: a wire without a known current, temperature rating, length or size is reported
as NOT evaluated and the overall verdict is ``incomplete`` rather than ``pass``.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Optional

from services.harness import wire_data as wd
from services.harness.wire_data import NotTabulated

SCHEMA = "aicad.harness.electrical/1"

# --- thermal constants ------------------------------------------------------------------------
_SIGMA = 5.670374419e-8        # W/m^2K^4
_G = 9.80665
_P_ATM = 101325.0
_R_AIR = 287.05                # J/(kg K)
_CP_AIR = 1007.0               # J/(kg K), Incropera Table A.4 at 300 K
_CU_RHO_KG_M3 = 8933.0         # Incropera Table A.1, copper (300 K)
_CU_CP_J_KG_K = 385.0          # Incropera Table A.1, copper (300 K)
DEFAULT_AMBIENT_C = 30.0       # NEC Table 310.16 reference ambient
DEFAULT_MAX_DROP_PCT = 3.0     # NEC 210.19(A) Informational Note (branch circuit); recommendation
DEFAULT_WARN_MARGIN_C = 10.0   # heuristic early-warning band below the insulation rating
DEFAULT_WARN_UTILISATION = 0.8  # heuristic early-warning band for ampacity utilisation

_GROUND_NETS = frozenset({"GND", "GNDA", "GNDD", "AGND", "DGND", "PGND", "EGND", "VSS", "VSSA",
                          "0V", "RTN", "RETURN", "V-", "BATT-", "VBAT-"})


class ThermalRange(ValueError):
    """The natural-convection correlation is outside its validity range / no steady state."""


# ============================================================================== thermal model

def _air_props(t_k: float) -> tuple[float, float, float]:
    """(rho kg/m3, mu Pa.s, k W/mK) - ideal gas at 1 atm; Sutherland (White: S_mu=110.4 K, S_k=194 K)."""
    mu = 1.716e-5 * (t_k / 273.15) ** 1.5 * (273.15 + 110.4) / (t_k + 110.4)
    k = 0.0241 * (t_k / 273.15) ** 1.5 * (273.15 + 194.0) / (t_k + 194.0)
    return _P_ATM / (_R_AIR * t_k), mu, k


def h_total_w_m2k(d_m: float, ts_c: float, ta_c: float, emissivity: float) -> dict:
    """Surface heat-transfer coefficient of a horizontal cylinder in still air (conv + rad)."""
    dt = abs(ts_c - ta_c)
    tf = (ts_c + ta_c) / 2.0 + 273.15
    rho, mu, k = _air_props(tf)
    nu = mu / rho
    alpha = k / (rho * _CP_AIR)
    pr = nu / alpha
    ra = _G * (1.0 / tf) * dt * d_m ** 3 / (nu * alpha)
    if ra > 1e9:
        raise ThermalRange(f"Rayleigh number {ra:.3g} exceeds the 1e9 validity limit of the "
                           "Churchill-Chu laminar correlation")
    nu_d = 0.36 + 0.518 * ra ** 0.25 / (1.0 + (0.559 / pr) ** (9.0 / 16.0)) ** (4.0 / 9.0)
    h_conv = nu_d * k / d_m
    ts_k, ta_k = ts_c + 273.15, ta_c + 273.15
    h_rad = emissivity * _SIGMA * (ts_k ** 2 + ta_k ** 2) * (ts_k + ta_k)
    return {"h": h_conv + h_rad, "h_conv": h_conv, "h_rad": h_rad, "ra": ra, "nu": nu_d}


def _surface_loss_w_per_m(d_m: float, ts_c: float, ta_c: float, emissivity: float) -> float:
    if ts_c <= ta_c:
        return 0.0
    return math.pi * d_m * h_total_w_m2k(d_m, ts_c, ta_c, emissivity)["h"] * (ts_c - ta_c)




def _insulation_resistance(rec: dict) -> Optional[float]:
    """Radial insulation thermal resistance per metre, K m/W."""
    cond, od = rec["conductor"], rec["od_mm"]
    k = _num(rec["spec"].get("insulation_thermal_conductivity_w_mk"))
    if cond is None or od is None or k is None:
        return None
    diameter = math.sqrt(4.0 * cond["csa_mm2"] / math.pi)
    if k <= 0 or od <= diameter:
        return None
    return math.log(od / diameter) / (2.0 * math.pi * k)


def _wire_heat(item: tuple[float, float, float], surface_c: float) -> float:
    current, r20, resistance = item
    q20 = current * current * r20
    denominator = 1.0 - q20 * wd.ALPHA_CU_20_PER_C * resistance
    if denominator <= 0:
        raise ThermalRange("insulation heat feedback has no finite steady state")
    return q20 * (1.0 + wd.ALPHA_CU_20_PER_C * (surface_c - 20.0)) / denominator


def _coupled_temperature(items: dict, diameter_m: float, ambient: float, emissivity: float) -> tuple:
    """Solve shared surface balance and each conductor's radial temperature rise."""
    if not items:
        return ambient, {}
    lo, hi = ambient, ambient + 1.0
    for _ in range(40):
        if _surface_loss_w_per_m(diameter_m, hi, ambient, emissivity) >= sum(
                _wire_heat(item, hi) for item in items.values()):
            break
        lo, hi = hi, ambient + 2.0 * (hi - ambient)
        if hi - ambient > 800:
            raise ThermalRange("no steady state within 800 C of ambient")
    else:
        raise ThermalRange("unable to bracket thermal steady state")
    for _ in range(70):
        mid = (lo + hi) / 2.0
        if _surface_loss_w_per_m(diameter_m, mid, ambient, emissivity) >= sum(
                _wire_heat(item, mid) for item in items.values()):
            hi = mid
        else:
            lo = mid
    surface = (lo + hi) / 2.0
    return surface, {wid: surface + _wire_heat(item, surface) * item[2]
                     for wid, item in items.items()}


def pulse_temperature_c(t0_c: float, i_a: float, r20_ohm_per_m: float, area_mm2: float,
                        duration_s: float) -> float:
    """Adiabatic conductor temperature after a current pulse, exact for the linear R(T) model.

    ``dT/dt = I^2 R(T) / (rho_m cp A)`` with ``R = R20 (1 + alpha (T - 20))`` gives
    ``1 + alpha (T - 20) = (1 + alpha (T0 - 20)) exp(alpha I^2 R20 t / (rho_m cp A))``. No heat
    leaves the conductor, so this is an UPPER bound for the pulse.
    """
    heat_cap = _CU_RHO_KG_M3 * _CU_CP_J_KG_K * area_mm2 * 1e-6   # J/(m K)
    k = wd.ALPHA_CU_20_PER_C * i_a * i_a * r20_ohm_per_m * duration_s / heat_cap
    x0 = 1.0 + wd.ALPHA_CU_20_PER_C * (t0_c - 20.0)
    return 20.0 + (x0 * math.exp(k) - 1.0) / wd.ALPHA_CU_20_PER_C


# ============================================================================== helpers

def _r(x: Optional[float], nd: int = 4) -> Optional[float]:
    return None if x is None else round(float(x), nd)


def _num(x: Any) -> Optional[float]:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def wire_length_m(w: dict) -> Optional[float]:
    """Conductor length: routed path + service loop. None until the wire is routed."""
    routed = _num(w.get("length_mm"))
    if not routed or routed <= 0:
        return None
    return (routed + (_num(w.get("service_loop_mm")) or 0.0)) / 1000.0


def _endpoint_id(end: Any) -> Optional[str]:
    if not isinstance(end, dict):
        return None
    return end.get("connector") or end.get("node")


def _is_ground_wire(w: dict) -> bool:
    if str(w.get("signal_class") or "").lower() == "ground":
        return True
    return str(w.get("net") or "").upper() in _GROUND_NETS


def _status_rank(s: str) -> int:
    return {"fail": 3, "warn": 2, "unknown": 1, "pass": 0, "info": 0, "not_evaluated": 1}.get(s, 0)


def _worst(*statuses: str) -> str:
    present = [s for s in statuses if s]
    return max(present, key=_status_rank) if present else "not_evaluated"


# ============================================================================== preparation

def _wire_spec(w: dict, specs: dict) -> dict:
    out: dict = {}
    out.update(specs.get("*") or {})
    out.update(specs.get(w.get("id")) or {})
    for key in ("temp_rating_c", "stranded", "conductor_class", "r20_ohm_per_km",
                "insulation", "insulation_thermal_conductivity_w_mk", "emissivity"):
        if w.get(key) is not None:
            out.setdefault(key, w[key])
    return out


def _resolve_wires(harness: dict, specs: dict) -> tuple[dict, list[dict]]:
    """wire id -> prepared wire record; plus findings for wires that cannot be resolved."""
    prepared: dict[str, dict] = {}
    findings: list[dict] = []
    for w in harness.get("wires") or []:
        wid = w.get("id")
        if not wid:
            continue
        spec = _wire_spec(w, specs)
        rec: dict[str, Any] = {"wire": w, "id": wid, "spec": spec, "conductor": None,
                               "length_m": wire_length_m(w), "problems": []}
        try:
            rec["conductor"] = wd.resolve_conductor(
                gauge_awg=w.get("gauge_awg"), csa_mm2=w.get("csa_mm2"),
                conductor_class=spec.get("conductor_class"),
                r20_ohm_per_km=spec.get("r20_ohm_per_km"), stranded=spec.get("stranded"))
        except NotTabulated as exc:
            rec["problems"].append(f"conductor: {exc}")
        material = spec.get("insulation")
        properties = wd.resolve_insulation(material) if material else {
            "material": None, "thermal_conductivity_w_mk": None, "emissivity": None,
            "max_temperature_c": None, "sources": {}, "assumptions": []}
        sources = dict(properties["sources"])
        for key, prop in (("insulation_thermal_conductivity_w_mk", "thermal_conductivity_w_mk"),
                          ("emissivity", "emissivity"), ("temp_rating_c", "max_temperature_c")):
            if spec.get(key) is not None:
                properties[prop] = spec[key]
                sources[prop] = "caller-supplied wire specification"
            elif properties.get(prop) is not None:
                spec[key] = properties[prop]
        properties["sources"] = sources
        rec["insulation"] = properties
        rating = _num(spec.get("temp_rating_c"))
        rec["rating_c"] = rating
        rec["od_mm"] = _num(w.get("insulation_od_mm"))
        k_raw = spec.get("insulation_thermal_conductivity_w_mk")
        if k_raw is not None and (_num(k_raw) is None or _num(k_raw) <= 0):
            raise ValueError("insulation_thermal_conductivity_w_mk must be positive and finite")
        e_raw = spec.get("emissivity")
        if e_raw is not None and (_num(e_raw) is None or not 0 <= _num(e_raw) <= 1):
            raise ValueError("wire emissivity must be finite and between 0 and 1")
        if spec.get("temp_rating_c") is not None and (rating is None or rating <= -200 or rating > 800):
            raise ValueError("temp_rating_c must be finite and in (-200, 800] C")
        if w.get("insulation_od_mm") is not None and (rec["od_mm"] is None or rec["od_mm"] <= 0):
            raise ValueError("insulation_od_mm must be positive and finite")
        if rec["length_m"] is None:
            rec["problems"].append("unrouted: length_mm is 0/None (run harness_route)")
        prepared[wid] = rec
    return prepared, findings


def _segments_of(harness: dict, prepared: dict) -> dict[str, dict]:
    """segment id -> {id, od_mm, wires:[ids present in the harness]}."""
    segs: dict[str, dict] = {}
    for s in harness.get("segments") or []:
        sid = s.get("id")
        if not sid:
            continue
        segs[sid] = {"id": sid, "od_mm": _num(s.get("od_mm")),
                     "wires": [x for x in (s.get("wires") or []) if x in prepared]}
    for wid, rec in prepared.items():
        for sid in rec["wire"].get("segments") or []:
            if sid in segs and wid not in segs[sid]["wires"]:
                segs[sid]["wires"].append(wid)
    return segs


def _rails_voltage(net: Optional[str], rails: dict) -> Optional[float]:
    if net is None:
        return None
    for k, v in rails.items():
        if str(k).upper() == str(net).upper():
            return _num(v)
    return None


def _norm_load(raw: dict) -> dict:
    cur = _num(raw.get("current_a"))
    out = {
        "current_a": cur,
        "current_rms_a": _num(raw.get("current_rms_a")),
        "peak_a": _num(raw.get("peak_a")),
        "peak_duration_s": _num(raw.get("peak_duration_s")),
        "peak_duty": _num(raw.get("peak_duty")),
        "duty": _num(raw.get("duty")),
        "voltage_v": _num(raw.get("voltage_v")),
        "supply_min_v": _num(raw.get("supply_min_v")),
        "min_load_voltage_v": _num(raw.get("min_load_voltage_v")),
        "max_drop_pct": _num(raw.get("max_drop_pct")),
        "continuous": bool(raw.get("continuous")),
        "kind": raw.get("kind"),
        "label": raw.get("label"),
        "return_wire": raw.get("return_wire"),
    }
    for key in ("current_a", "current_rms_a", "peak_a", "peak_duration_s", "peak_duty",
                "duty", "voltage_v", "supply_min_v", "min_load_voltage_v", "max_drop_pct"):
        if raw.get(key) is not None and (out[key] is None or out[key] < 0):
            raise ValueError(f"{key} must be a finite nonnegative number")
    for key in ("duty", "peak_duty"):
        if out[key] is not None and out[key] > 1:
            raise ValueError(f"{key} must be between 0 and 1")
    for key in ("voltage_v", "supply_min_v"):
        if out[key] == 0:
            raise ValueError(f"{key} must be positive")
    if cur is None:
        raise ValueError("load current_a is required; supply the continuous current in amperes")
    return out


def _resolve_loads(harness: dict, prepared: dict, loads: Optional[list], rails: dict,
                   power_budget: Optional[dict], assumptions: list[str],
                   findings: list[dict]) -> dict[str, dict]:
    """wire id -> normalised load (forward wires only; returns are derived later)."""
    by_net: dict[str, list[str]] = {}
    for wid, rec in prepared.items():
        net = rec["wire"].get("net")
        if net:
            by_net.setdefault(str(net), []).append(wid)

    out: dict[str, dict] = {}

    def apply(entry: dict, origin: str) -> None:
        load = _norm_load(entry)
        targets: list[str] = []
        if entry.get("wire"):
            targets = [entry["wire"]]
            if entry["wire"] not in prepared:
                findings.append({"severity": "warn", "check": "load_target",
                                 "message": f"{origin}: wire {entry['wire']!r} is not in the harness"})
                return
        elif entry.get("net"):
            targets = [w for w in by_net.get(str(entry["net"]), [])
                       if not _is_ground_wire(prepared[w]["wire"])]
            if not targets:
                findings.append({"severity": "warn", "check": "load_target",
                                 "message": f"{origin}: no wire carries net {entry['net']!r}"})
                return
            if len(targets) > 1:
                mode = str(entry.get("apply") or "each")
                if mode == "split":
                    for k in ("current_a", "current_rms_a", "peak_a"):
                        if load[k] is not None:
                            load[k] = load[k] / len(targets)
                    assumptions.append(f"{origin}: net {entry['net']} current split equally over "
                                       f"{len(targets)} wires")
                else:
                    assumptions.append(
                        f"{origin}: net {entry['net']} has {len(targets)} wires; the full current is "
                        "applied to EACH (conservative; use apply='split' or per-wire loads)")
        else:
            findings.append({"severity": "warn", "check": "load_target",
                             "message": f"{origin}: needs 'wire' or 'net'"})
            return
        for wid in targets:
            ld = dict(load)
            ld["origin"] = origin
            if ld["voltage_v"] is None:
                ld["voltage_v"] = _rails_voltage(prepared[wid]["wire"].get("net"), rails)
            out[wid] = ld

    # Rails from a power budget first (lowest priority), then harness loads, then explicit.
    if isinstance(power_budget, dict):
        for rail in power_budget.get("rails") or []:
            name = rail.get("name")
            avg, peak = _num(rail.get("i_avg_a")), _num(rail.get("i_peak_a"))
            if not name or (avg is None and peak is None):
                findings.append({"severity": "info", "check": "power_budget",
                                 "message": f"rail {name!r}: current unknown in the power budget; "
                                            "wires on that net are not evaluated from it"})
                continue
            if not by_net.get(str(name)):
                continue
            entry = {"net": name, "current_a": avg if avg is not None else peak,
                     "voltage_v": rail.get("voltage_v_nom")}
            if peak is not None and avg is not None and peak > avg:
                entry["peak_a"] = peak
            apply(entry, f"power_budget rail {name}")
    for i, raw in enumerate(harness.get("loads") or []):
        if isinstance(raw, dict):
            apply(raw, f"harness.loads[{i}]")
    for i, raw in enumerate(loads or []):
        if isinstance(raw, dict):
            apply(raw, f"loads[{i}]")
    return out


def _pair_returns(prepared: dict, loads: dict[str, dict]) -> dict[str, list[str]]:
    """forward wire id -> return wire ids (ground wires joining the same two connectors)."""
    ground = [wid for wid, rec in prepared.items() if _is_ground_wire(rec["wire"])]
    pairs: dict[str, list[str]] = {}
    for wid, ld in loads.items():
        rec = prepared[wid]
        if _is_ground_wire(rec["wire"]):
            continue
        explicit = ld.get("return_wire")
        if explicit:
            pairs[wid] = [explicit] if explicit in prepared else []
            continue
        ends = {_endpoint_id(rec["wire"].get("from")), _endpoint_id(rec["wire"].get("to"))}
        match = [g for g in ground
                 if {_endpoint_id(prepared[g]["wire"].get("from")),
                     _endpoint_id(prepared[g]["wire"].get("to"))} == ends]
        # Several ground wires do not prove parallel return connectivity.
        pairs[wid] = match if len(match) == 1 else []
    return pairs


# ============================================================================== core run

def _heating_current(ld: dict) -> float:
    """Heating-equivalent (RMS) current of a load, A."""
    cont = ld.get("current_a") or 0.0
    rms = ld.get("current_rms_a")
    base = rms if rms is not None else cont
    pk, pd = ld.get("peak_a"), ld.get("peak_duty")
    duty = ld.get("duty")
    duty = 1.0 if duty is None else duty
    if pk is not None and pd:
        sq = base * base * (1.0 - pd) + pk * pk * pd
    else:
        sq = base * base
    return math.sqrt(max(duty, 0.0) * sq)


def _run(harness: dict, opts: dict) -> dict:
    ambient = opts["ambient_c"]
    emiss = opts["emissivity"]
    assumptions: list[str] = list(opts.get("_assumptions") or [])
    findings: list[dict] = []
    prepared, f0 = _resolve_wires(harness, opts["wire_specs"])
    findings.extend(f0)
    segs = _segments_of(harness, prepared)
    loads = _resolve_loads(harness, prepared, opts["loads"], opts["rails"], opts["power_budget"],
                           assumptions, findings)
    pairs = _pair_returns(prepared, loads)

    # Return conductors inherit the forward current (shared over parallel returns by conductance).
    full: dict[str, dict] = {w: dict(ld, role="forward") for w, ld in loads.items()}
    for fwd, rets in pairs.items():
        ld = loads[fwd]
        conds = {}
        for rid in rets:
            c = prepared[rid]["conductor"]
            conds[rid] = (1.0 / c["r20_ohm_per_km"]) if c else None
        if not rets or any(v is None for v in conds.values()):
            continue
        total_g = sum(conds.values())
        for rid in rets:
            share = conds[rid] / total_g
            ent = full.setdefault(rid, {"current_a": 0.0, "peak_a": None, "duty": ld.get("duty"),
                                        "peak_duty": ld.get("peak_duty"), "role": "return",
                                        "origin": f"return of {fwd}", "returns_for": []})
            ent["current_a"] = (ent.get("current_a") or 0.0) + (ld.get("current_a") or 0.0) * share
            if ld.get("current_rms_a") is not None:
                ent["current_rms_a"] = (ent.get("current_rms_a") or 0.0) + ld["current_rms_a"] * share
            if ld.get("peak_a") is not None:
                ent["peak_a"] = (ent.get("peak_a") or 0.0) + ld["peak_a"] * share
            ent.setdefault("returns_for", []).append(fwd)

    count_unloaded = bool(opts["count_unloaded_wires"])
    # --- per-segment thermal / ccc state
    seg_rows: dict[str, dict] = {}
    wire_seg_temp: dict[str, tuple[float, str]] = {}
    for sid, seg in segs.items():
        member_items = {}
        n_loaded = n_unloaded = 0
        missing = []
        for wid in seg["wires"]:
            rec = prepared[wid]
            current = _heating_current(full[wid]) if wid in full else 0.0
            if current > 0:
                n_loaded += 1
            else:
                n_unloaded += 1
            resistance = _insulation_resistance(rec)
            if rec["conductor"] is None or resistance is None:
                missing.append(wid)
            else:
                member_items[wid] = (current, rec["conductor"]["r20_ohm_per_km"] / 1000.0, resistance)
        vacuum = any(_num(prepared[wid]["spec"].get("nasa_pressure_psia")) is not None
                     and _num(prepared[wid]["spec"].get("nasa_pressure_psia")) < 4.3
                     for wid in seg["wires"])
        ribbon = any(str(prepared[wid]["wire"].get("kind") or "").lower() == "ribbon"
                     for wid in seg["wires"])
        od = seg["od_mm"]
        member_emissivities = {_num(prepared[wid]["spec"].get("emissivity")) for wid in seg["wires"]}
        segment_emissivity = emiss
        if segment_emissivity is None and len(member_emissivities) == 1:
            segment_emissivity = next(iter(member_emissivities))
            if segment_emissivity is not None:
                assumptions.append(f"segment {sid}: shared surface assumed to have the common wire insulation emissivity; supply emissivity for a jacket")
        temp, temperatures, note = None, {}, None
        if ribbon:
            note = ("ribbon cable is not a circular wire; use a manufacturer ampacity with its installation "
                    "conditions or a ribbon-specific thermal analysis")
        elif vacuum:
            note = "still-air convection model is not applicable in vacuum"
        elif not od or od <= 0:
            note = "segment has no positive od_mm; bundle heating not evaluated"
        elif missing:
            note = "missing conductor, insulation OD or conductivity for: " + ", ".join(missing)
        elif segment_emissivity is None:
            note = "bundle surface emissivity unknown or mixed; supply emissivity for the outer surface"
        else:
            try:
                temp, temperatures = _coupled_temperature(member_items, od / 1000.0, ambient, segment_emissivity)
            except ThermalRange as exc:
                note = str(exc)
        heat = sum(_wire_heat(item, temp) for item in member_items.values()) if temp is not None else None
        seg_rows[sid] = {"id": sid, "od_mm": od, "n_wires": len(seg["wires"]), "n_loaded": n_loaded,
                         "n_unloaded": n_unloaded, "bundle_temp_c": temp, "note": note,
                         "heat_w_per_m": heat, "_member_items": member_items,
                         "emissivity": segment_emissivity,
                         "_thermal_inputs_valid": bool(od and od > 0 and not missing and not vacuum
                                                       and not ribbon and segment_emissivity is not None)}
        for wid, wire_temp in temperatures.items():
            if wid not in wire_seg_temp or wire_temp > wire_seg_temp[wid][0]:
                wire_seg_temp[wid] = (wire_temp, sid)

    # --- per-wire evaluation
    rows: dict[str, dict] = {}
    fuses = opts["fuses"]
    for wid, rec in prepared.items():
        ld = full.get(wid)
        row = _eval_wire(wid, rec, ld, segs, seg_rows, wire_seg_temp, count_unloaded, opts,
                         assumptions, findings, fuses)
        rows[wid] = row

    # --- round-trip drops need both temperatures, so compute after rows exist
    for fwd, rets in pairs.items():
        _eval_drop(fwd, rets, rows, prepared, loads[fwd], opts, findings)

    result = _assemble(harness, opts, prepared, rows, seg_rows, loads, pairs, assumptions, findings)
    return result


def _eval_wire(wid, rec, ld, segs, seg_rows, wire_seg_temp, count_unloaded, opts, assumptions,
               findings, fuses) -> dict:
    w = rec["wire"]
    ambient = opts["ambient_c"]
    row: dict[str, Any] = {
        "id": wid, "net": w.get("net"), "signal_class": w.get("signal_class"),
        "from": w.get("from"), "to": w.get("to"),
        "gauge": rec["conductor"]["label"] if rec["conductor"] else None,
        "length_m": _r(rec["length_m"], 4), "problems": list(rec["problems"]),
        "role": None, "current_a": None, "peak_a": None, "status": "not_evaluated", "checks": {},
        "insulation": rec["insulation"],
    }
    if rec["conductor"] is None or rec["length_m"] is None:
        row["status"] = "not_evaluated"
        findings.append({"severity": "warn", "wire": wid, "check": "wire_resolvable",
                         "message": f"wire {wid} not evaluated: " + "; ".join(rec["problems"])})
        return row
    cond = rec["conductor"]
    r20_m = cond["r20_ohm_per_km"] / 1000.0
    row["resistance_20c_ohm"] = _r(r20_m * rec["length_m"], 6)
    row["resistance_basis"] = cond["basis"]
    if cond.get("notes"):
        row["notes"] = list(cond["notes"])
    if ld is None:
        cls = str(w.get("signal_class") or "").lower()
        if cls in ("power", "ground"):
            row["status"] = "not_evaluated"
            findings.append({
                "severity": "warn", "wire": wid, "check": "current_unknown",
                "message": f"{cls} wire {wid} (net {w.get('net')}) has no load current: ampacity, "
                           "drop and heating are NOT evaluated. Supply loads=[{wire|net, current_a}] "
                           "or a power_budget with this rail"})
        else:
            row["status"] = "not_evaluated"
            row["note"] = "no load entry; treated as negligible-current signal wire (not counted in heating)"
        return row

    row["role"] = ld.get("role")
    row["current_a"] = _r(ld.get("current_a"), 4)
    row["peak_a"] = _r(ld.get("peak_a"), 4)
    row["load_origin"] = ld.get("origin")
    i_heat = _heating_current(ld)
    row["heating_current_a"] = _r(i_heat, 4)
    checks = row["checks"]

    # temperature
    member_segments = [sid for sid, segment in segs.items() if wid in segment["wires"]]
    seg_t = wire_seg_temp.get(wid)
    d_single_mm = rec["od_mm"]
    ribbon = str(w.get("kind") or "").lower() == "ribbon"
    wire_emissivity = opts["emissivity"] if opts["emissivity"] is not None else _num(rec["spec"].get("emissivity"))
    t_c: Optional[float] = None
    t_basis = None
    if (seg_t is not None and all(sid in segs for sid in (w.get("segments") or []))
            and all(seg_rows[sid]["bundle_temp_c"] is not None for sid in member_segments)):
        t_c, sid = seg_t
        t_basis = f"bundle {sid}"
    elif not ribbon and not member_segments and not rec["wire"].get("segments") and not (
            _num(rec["spec"].get("nasa_pressure_psia")) is not None
            and _num(rec["spec"].get("nasa_pressure_psia")) < 4.3):
        resistance = _insulation_resistance(rec)
        if d_single_mm and resistance is not None and wire_emissivity is not None:
            try:
                _, temps = _coupled_temperature({wid: (i_heat, r20_m, resistance)},
                                                d_single_mm / 1000.0, ambient, wire_emissivity)
                t_c, t_basis = temps[wid], "isolated wire in free air"
            except ThermalRange as exc:
                t_basis = str(exc)
    row["conductor_temp_c"] = None if t_c is None else (_r(t_c, 2) if math.isfinite(t_c) else None)
    row["temp_rise_c"] = None if t_c is None or not math.isfinite(t_c) else _r(t_c - ambient, 2)
    row["temp_basis"] = t_basis
    if t_c is not None:
        row["_t_c"] = t_c

    rating = rec["rating_c"]
    # thermal verdict
    th_status = "not_evaluated"
    if t_c is None and ribbon:
        row["thermal_note"] = ("ribbon cable is not a circular wire; supply manufacturer ampacity with "
                               "installation conditions or use a ribbon-specific thermal analysis")
    elif t_c is None:
        row["thermal_note"] = ("thermal model requires conductor size, insulation_od_mm greater than "
                               "equivalent conductor diameter, insulation conductivity and surface emissivity "
                               "for each bundle member; also check segment notes")
    elif rating is None:
        th_status = "not_evaluated"
        row["thermal_note"] = ("insulation temp_rating_c unknown: temperature is reported but cannot be "
                               "judged; supply wire_specs {'*': {'temp_rating_c': ...}} or the wire's temp_rating_c")
        findings.append({"severity": "warn", "wire": wid, "check": "temp_rating_unknown",
                         "message": f"wire {wid}: insulation temperature rating unknown; conductor "
                                    f"temperature {row['conductor_temp_c']} C cannot be judged"})
    elif not math.isfinite(t_c) or t_c > rating:
        th_status = "warn"
    elif t_c > rating - opts["warn_margin_c"]:
        th_status = "warn"
    elif rating is not None:
        th_status = "not_evaluated"
        row["thermal_note"] = "Estimated conductor temperature is not a qualified wire ampacity."
    checks["thermal"] = {"status": th_status, "rating_c": rating,
                         "margin_c": None if rating is None or t_c is None or not math.isfinite(t_c) else _r(rating - t_c, 2),
                         "insulation_od_mm": rec["od_mm"],
                         "equivalent_conductor_diameter_mm": _r(math.sqrt(4 * cond["csa_mm2"] / math.pi), 6),
                         "insulation_thermal_conductivity_w_mk": _num(rec["spec"].get("insulation_thermal_conductivity_w_mk")),
                         "emissivity": (seg_rows[seg_t[1]]["emissivity"] if seg_t is not None
                                        and t_basis and t_basis.startswith("bundle ") else wire_emissivity),
                         "emissivity_basis": ("caller-supplied outer surface emissivity" if opts["emissivity"] is not None
                                              else "insulation material or wire specification; bundle surface assumed common"),
                         "air_pressure_pa_assumed": _P_ATM,
                         "model": "I²R(T), radial insulation conduction and idealized shared-surface "
                                  "natural convection and radiation; bundle contact/void resistance omitted",
                         "confidence": "low"}

    # ampacity: table path
    tbl: dict[str, Any] = {"status": "not_evaluated"}
    if rating is not None and cond["kind"] == "awg":
        try:
            base = wd.nec_table_ampacity(cond["awg"], rating)
            amb_f = wd.nec_ambient_factor(rating, ambient)
            sid_worst, n_ccc = None, 1
            for sid in (w.get("segments") or []):
                row_s = seg_rows.get(sid)
                if row_s:
                    n = row_s["n_loaded"] + (row_s["n_unloaded"] if count_unloaded else 0)
                    if n >= n_ccc:
                        n_ccc, sid_worst = n, sid
            adj = wd.nec_adjustment_factor(max(n_ccc, 1))
            if amb_f is None:
                tbl = {"status": "fail", "reason": f"ambient {ambient} C is outside NEC Table "
                       f"310.15(B)(2)(a) for {rating:g} C insulation (not permitted)"}
            else:
                derated = base * amb_f * adj
                util = i_heat / derated
                st = "fail" if util > 1.0 else ("warn" if util > opts["warn_utilisation"] else "pass")
                tbl = {"status": st, "table_a": base, "ambient_factor": amb_f, "bundle_factor": adj,
                       "current_carrying_conductors": n_ccc, "derated_a": _r(derated, 3),
                       "utilisation": _r(util, 3), "scope": wd.NEC_SCOPE,
                       "citation": "NFPA 70 Tables 310.16, 310.15(B)(2)(a), 310.15(C)(1)"}
        except NotTabulated as exc:
            tbl = {"status": "not_evaluated", "reason": str(exc)}
    elif rating is not None:
        tbl = {"status": "not_evaluated", "reason": "metric conductor: no NEC AWG row; thermal model only"}
    checks["ampacity_table"] = tbl
    # Datasheet ampacity is valid only within the installation conditions supplied
    # with it; it must never be silently extrapolated to hotter or larger bundles.
    ds = rec["spec"].get("ampacity")
    if ds is not None:
        if not isinstance(ds, dict):
            raise ValueError("wire_specs ampacity must be an object with current_a, ambient_c, "
                             "max_conductors and source")
        allowed = _num(ds.get("current_a"))
        max_ambient = _num(ds.get("ambient_c"))
        max_conductors = _num(ds.get("max_conductors"))
        source = ds.get("source")
        if (allowed is None or allowed <= 0 or max_ambient is None or max_conductors is None
                or max_conductors < 1 or int(max_conductors) != max_conductors
                or not isinstance(source, str) or not source.strip()):
            raise ValueError("ampacity requires positive current_a, finite ambient_c, integer "
                             "max_conductors >= 1, and a source")
        count = max([len(s["wires"]) for s in segs.values() if wid in s["wires"]] or [1])
        if ambient <= max_ambient and count <= max_conductors:
            util = i_heat / allowed
            checks["ampacity_datasheet"] = {
                "status": "fail" if util > 1 else ("warn" if util > opts["warn_utilisation"] else "pass"),
                "derated_a": allowed, "utilisation": _r(util, 3),
                "basis": f"caller-supplied {source}, ambient <= {max_ambient:g} C, "
                         f"at most {int(max_conductors)} conductors",
                "confidence": "source_dependent"}
        else:
            checks["ampacity_datasheet"] = {
                "status": "not_evaluated",
                "basis": "ambient or bundle size exceeds supplied ampacity conditions",
                "confidence": "source_dependent"}

    # Opt-in asserts that NASA's spaceflight installation scope applies.
    nasa_pressure = rec["spec"].get("nasa_pressure_psia")
    if nasa_pressure is not None:
        pressure = _num(nasa_pressure)
        if pressure is None or pressure < 0:
            raise ValueError("nasa_pressure_psia must be finite and nonnegative")
        nasa = {"status": "not_evaluated", "scope": wd.NASA_WIRE_SCOPE}
        try:
            if rating is None or cond["kind"] != "awg":
                raise NotTabulated("NASA table requires AWG and insulation temperature rating")
            count = max([len(s["wires"]) for s in segs.values() if wid in s["wires"]] or [1])
            allowed = wd.nasa_harness_ampacity(cond["awg"], rating, pressure_psia=pressure,
                                               ambient_c=ambient, n_wires=count)
            util = i_heat / allowed
            nasa.update({"status": "fail" if util > 1 else ("warn" if util > opts["warn_utilisation"] else "pass"),
                         "derated_a": _r(allowed, 3), "utilisation": _r(util, 3),
                         "total_wires": count, "pressure_psia": pressure,
                         "citation": "NASA MSFC-STD-3012A Appendix A pp. 127-128"})
        except NotTabulated as exc:
            nasa["reason"] = str(exc)
        checks["ampacity_nasa"] = nasa

    # ampacity: thermal-model current limit at the rating
    cap_a = None
    if (not ribbon and rating is not None and ambient < rating and (member_segments or wire_emissivity is not None)
            and all(sid in segs for sid in (w.get("segments") or []))
            and not (nasa_pressure is not None and pressure < 4.3)):
        try:
            caps = [_thermal_cap_a(wid, rec, ambient, rating,
                                   seg_rows[sid]["emissivity"] if sid is not None else wire_emissivity, sid, seg_rows)
                    for sid in (member_segments or [None])]
            cap_a = min(caps) if all(c is not None for c in caps) else None
        except (ThermalRange, KeyError):
            cap_a = None
        if cap_a is not None:
            util = i_heat / cap_a if cap_a > 0 else float("inf")
            checks["ampacity_thermal"] = {
                "status": "warn" if util > 1.0 else "not_evaluated",
                "limit_a": _r(cap_a, 3), "utilisation": _r(util, 3) if math.isfinite(util) else None,
                "basis": "estimated radial-conduction and surface heat balance, not a qualified ampacity",
                "confidence": "low"}
    elif rating is not None and ambient >= rating:
        checks["ampacity_thermal"] = {"status": "fail", "limit_a": 0.0,
                                      "basis": f"ambient {ambient} C >= insulation rating {rating} C"}
    # An explicitly selected wire's datasheet rating is authoritative over generic
    # reference tables; an out-of-scope datasheet must not fall back silently.
    rating_keys = ("ampacity_datasheet",) if ds is not None else ("ampacity_table", "ampacity_nasa")
    states = [checks.get(k, {}).get("status") for k in (*rating_keys, "ampacity_thermal", "thermal")]
    evaluated_states = [s for s in states if s in ("pass", "warn", "fail")]
    amp_status = _worst(*evaluated_states)
    evaluated = bool(evaluated_states)
    row["ampacity_status"] = amp_status if evaluated else "not_evaluated"
    row["status"] = row["ampacity_status"] if evaluated else "not_evaluated"

    # derated ampacity used for protection
    ratings = [x for x in (
        ((checks.get("ampacity_datasheet") or {}).get("derated_a") if ds is not None else tbl.get("derated_a")),
        (None if ds is not None else (checks.get("ampacity_nasa") or {}).get("derated_a")))
               if x is not None]
    row["derated_ampacity_a"] = min(ratings) if ratings else None
    row["derated_ampacity_basis"] = "applicable NEC, NASA spaceflight and/or supplied datasheet screen" if ratings else None

    # peak pulse
    pk = ld.get("peak_a")
    if pk is not None and pk > (ld.get("current_a") or 0.0):
        pulse: dict[str, Any] = {"peak_a": _r(pk, 3)}
        dur = ld.get("peak_duration_s")
        if dur and t_c is not None and math.isfinite(t_c):
            t_pk = pulse_temperature_c(t_c, pk, r20_m, cond["csa_mm2"], dur)
            pulse.update({"duration_s": dur, "pulse_temp_c": _r(t_pk, 1),
                          "basis": "adiabatic upper bound (no heat leaves the conductor during the pulse)"})
            if rating is not None and t_pk > rating:
                pulse["status"] = "warn"
                findings.append({
                    "severity": "warn", "wire": wid, "check": "peak_pulse",
                    "message": f"wire {wid}: {pk:g} A for {dur:g} s drives the conductor to at most "
                               f"{t_pk:.0f} C (adiabatic bound) vs insulation rating {rating:g} C; "
                               "acceptable only if the insulation datasheet allows that short excursion"})
            else:
                pulse["status"] = "pass" if rating is not None else "not_evaluated"
        else:
            pulse["status"] = "not_evaluated"
            pulse["note"] = ("peak current given without peak_duration_s: wire heating by the peak is "
                             "not evaluated (drop at peak and fuse checks still are)")
        if row.get("derated_ampacity_a") is not None and pk > row["derated_ampacity_a"]:
            pulse["exceeds_steady_ampacity"] = True
        checks["peak"] = pulse

    # fuse
    fz = _fuse_for(wid, w, fuses)
    if fz is not None:
        _eval_fuse(wid, row, ld, fz, findings, opts)
    if row["status"] in ("fail", "warn"):
        _finding_for_ampacity(wid, row, ld, findings)
    return row


def _thermal_cap_a(wid, rec, ambient, rating, emissivity, sid, seg_rows) -> Optional[float]:
    """Estimate current at the conductor rating, holding other wire currents fixed."""
    resistance = _insulation_resistance(rec)
    if resistance is None:
        return None
    if sid is not None:
        seg = seg_rows[sid]
        if not seg["_thermal_inputs_valid"] or wid not in seg["_member_items"]:
            return None
        d_m = seg["od_mm"] / 1000.0
        others = [v for k, v in seg["_member_items"].items() if k != wid]
    else:
        d_m = rec["od_mm"] / 1000.0
        others = []
    def balance(surface):
        return (_surface_loss_w_per_m(d_m, surface, ambient, emissivity)
                - sum(_wire_heat(item, surface) for item in others)
                - (rating - surface) / resistance)
    if balance(rating) <= 0:
        return 0.0
    lo, hi = ambient, rating
    for _ in range(70):
        mid = (lo + hi) / 2.0
        if balance(mid) > 0:
            hi = mid
        else:
            lo = mid
    q = (rating - (lo + hi) / 2.0) / resistance
    r20_m = rec["conductor"]["r20_ohm_per_km"] / 1000.0
    factor = 1.0 + wd.ALPHA_CU_20_PER_C * (rating - 20.0)
    return math.sqrt(max(q, 0.0) / (r20_m * factor))


def _fuse_for(wid: str, w: dict, fuses: list) -> Optional[dict]:
    for fz in fuses or []:
        if fz.get("wire") == wid:
            return fz
    for fz in fuses or []:
        if fz.get("net") and str(fz["net"]) == str(w.get("net")):
            return fz
    return None


def _eval_fuse(wid, row, ld, fz, findings, opts) -> None:
    rating_a = _num(fz.get("rating_a"))
    if rating_a is None or rating_a <= 0:
        findings.append({"severity": "warn", "wire": wid, "check": "fuse",
                         "message": f"fuse entry for {wid} has no positive rating_a"})
        return
    out: dict[str, Any] = {"rating_a": rating_a}
    status = "pass"
    derated = row.get("derated_ampacity_a")
    if derated is None:
        out["protects_wire"] = "not_evaluated"
        out["note"] = "wire ampacity not evaluated, so protection cannot be confirmed"
        status = "unknown"
    elif rating_a > derated:
        out["protects_wire"] = False
        status = "fail"
        findings.append({
            "severity": "fail", "wire": wid, "check": "fuse_protects_wire",
            "message": f"fuse {rating_a:g} A exceeds wire {wid} derated ampacity {derated:g} A "
                       f"({row.get('derated_ampacity_basis')}): the wire can overheat before the fuse acts",
            "fix": {"action": "fuse_rating_at_most", "rating_a": math.floor(derated * 10) / 10.0,
                    "or": "upsize the wire"}})
    else:
        out["protects_wire"] = True
    cont = ld.get("current_a") or 0.0
    need = cont * (1.25 if ld.get("continuous") else 1.0)
    out["required_min_a"] = _r(need, 3)
    out["continuous_rule"] = ("125 % of a continuous load (NEC 210.20(A)/215.3, building-wiring scope)"
                              if ld.get("continuous") else "load current only")
    if rating_a < need:
        status = "fail"
        out["nuisance_blow"] = True
        findings.append({
            "severity": "fail", "wire": wid, "check": "fuse_nuisance",
            "message": f"fuse {rating_a:g} A is below the required {need:g} A for the load on {wid}",
            "fix": {"action": "fuse_rating_at_least", "rating_a": math.ceil(need * 10) / 10.0}})
    i2t = _num(fz.get("i2t_a2s"))
    pk, dur = ld.get("peak_a"), ld.get("peak_duration_s")
    if i2t and pk and dur:
        peak_i2t = pk * pk * dur
        out["peak_i2t_a2s"] = _r(peak_i2t, 3)
        out["fuse_prearc_i2t_a2s"] = i2t
        if peak_i2t >= i2t:
            status = "fail"
            findings.append({
                "severity": "fail", "wire": wid, "check": "fuse_inrush",
                "message": f"peak {pk:g} A for {dur:g} s is {peak_i2t:.3g} A^2s >= the fuse's "
                           f"{i2t:.3g} A^2s pre-arcing rating: the fuse will open on inrush/stall; "
                           "datasheet temperature/aging derating is NOT applied here"})
    elif pk and not (i2t and dur):
        out["inrush_note"] = ("peak_a given but fuse i2t_a2s and/or peak_duration_s missing: nuisance "
                              "blowing on inrush is NOT evaluated (needs the fuse's time-current data)")
    out["status"] = status
    row["checks"]["fuse"] = out
    if status == "fail":
        row["status"] = _worst(row["status"], "fail")


def _finding_for_ampacity(wid, row, ld, findings) -> None:
    for name in ("ampacity_table", "ampacity_datasheet", "ampacity_nasa", "ampacity_thermal", "thermal"):
        if name in ("ampacity_table", "ampacity_nasa") and "ampacity_datasheet" in row["checks"]:
            continue
        c = row["checks"].get(name) or {}
        if c.get("status") in ("fail", "warn"):
            if name == "thermal":
                msg = (f"wire {wid}: conductor {row.get('conductor_temp_c')} C vs insulation rating "
                       f"{c.get('rating_c')} C (margin {c.get('margin_c')} C)")
            else:
                msg = (f"wire {wid}: {row.get('heating_current_a')} A vs "
                       f"{c.get('derated_a', c.get('limit_a'))} A {name.replace('_', ' ')} limit "
                       f"(utilisation {c.get('utilisation')})")
            findings.append({"severity": c["status"], "wire": wid, "check": name, "message": msg,
                             "basis": c.get("basis") or c.get("model") or c.get("scope"),
                             "confidence": c.get("confidence", "published_screening_reference")})


def _eval_drop(fwd: str, rets: list[str], rows: dict, prepared: dict, ld: dict, opts: dict,
               findings: list[dict]) -> None:
    row = rows[fwd]
    rec = prepared[fwd]
    if rec["conductor"] is None or rec["length_m"] is None:
        return
    ambient = opts["ambient_c"]

    def r_hot(wid: str) -> tuple[float, str]:
        rr, pr = rows[wid], prepared[wid]
        r20 = pr["conductor"]["r20_ohm_per_km"] / 1000.0 * pr["length_m"]
        t = rr.get("_t_c")
        if t is not None and math.isfinite(t):
            return wd.resistance_at(r20, t), f"at {t:.0f} C conductor temperature"
        return wd.resistance_at(r20, max(ambient, 20.0)), f"at ambient {max(ambient, 20.0):g} C (no heating model)"

    r_f, basis_f = r_hot(fwd)
    r20_f = rec["conductor"]["r20_ohm_per_km"] * rec["length_m"] / 1000.0
    usable = [x for x in rets if prepared[x]["conductor"] is not None and prepared[x]["length_m"] is not None]
    if rets and len(usable) == len(rets):
        g = sum(1.0 / r_hot(x)[0] for x in usable)
        r_ret = 1.0 / g
        ret_known = True
    else:
        r_ret = 0.0
        ret_known = False
    r_loop = r_f + r_ret
    cur, pk = ld.get("current_a"), ld.get("peak_a")
    v_nom, v_min = ld.get("voltage_v"), ld.get("supply_min_v")
    drop = {"forward_ohm": _r(r_f, 6), "return_ohm": _r(r_ret, 6) if ret_known else None,
            "return_wires": list(rets), "loop_ohm": _r(r_loop, 6), "resistance_basis": basis_f,
            "return_path": "in harness" if ret_known else "not in harness (one-way drop only)"}
    if cur is not None:
        drop["drop_v"] = _r(cur * r_loop, 5)
        if ret_known:
            r20_ret = 1.0 / sum(1.0 / (prepared[x]["conductor"]["r20_ohm_per_km"]
                                      * prepared[x]["length_m"] / 1000.0) for x in usable)
            drop["drop_20c_v"] = _r(cur * (r20_f + r20_ret), 6)
    if pk is not None:
        drop["peak_drop_v"] = _r(pk * r_loop, 5)
    status = "unknown"
    limit_pct = ld.get("max_drop_pct") if ld.get("max_drop_pct") is not None else opts["max_drop_pct"]
    if v_nom and cur is not None:
        pct = 100.0 * cur * r_loop / v_nom
        drop["supply_v"] = v_nom
        drop["drop_pct"] = _r(pct, 3)
        drop["load_end_v"] = _r(v_nom - cur * r_loop, 4)
        drop["limit_pct"] = limit_pct
        drop["limit_basis"] = ("caller max_drop_pct" if ld.get("max_drop_pct") is not None else
                               "NEC 210.19(A) Informational Note: 3 % branch-circuit recommendation "
                               "(guidance, not a requirement)")
        status = "fail" if pct > limit_pct else "pass"
        if pk is not None:
            vs = v_min if v_min is not None else v_nom
            drop["load_end_v_at_peak"] = _r(vs - pk * r_loop, 4)
            drop["peak_drop_pct"] = _r(100.0 * pk * r_loop / vs, 3)
            if v_min is not None:
                drop["supply_min_v"] = v_min
        mlv = ld.get("min_load_voltage_v")
        if mlv is not None:
            vs = v_min if v_min is not None else v_nom
            worst_i = max(pk or 0.0, cur)
            v_at = vs - worst_i * r_loop
            drop["min_load_voltage_v"] = mlv
            drop["load_end_v_worst"] = _r(v_at, 4)
            if v_at < mlv:
                status = "fail"
    elif v_nom is None and cur is not None:
        drop["note"] = ("rail voltage unknown: drop reported in volts only; supply loads[].voltage_v "
                        "or rails={net: V} to judge % drop and the load-end voltage")
    if not ret_known:
        if status == "pass":
            status = "unknown"
        drop["note"] = "Return path unknown: drop is a lower bound and load voltage an upper bound."
        findings.append({"severity": "info", "wire": fwd, "check": "return_path",
                         "message": f"wire {fwd}: return path unknown or ambiguous; provide return_wire explicitly"})
    row["checks"]["voltage_drop"] = dict(drop, status=status)
    row["status"] = _worst(row["status"], status if status == "fail" else "")
    if status == "fail":
        pct = drop.get("drop_pct")
        if pct is not None and pct > limit_pct:
            msg = (f"wire {fwd}: round-trip drop {drop['drop_v']} V = {pct}% of {v_nom:g} V exceeds "
                   f"{limit_pct:g}%; load end sees {drop['load_end_v']} V")
        else:
            msg = (f"wire {fwd}: worst-case load-end voltage {drop.get('load_end_v_worst')} V is below "
                   f"the required minimum {drop.get('min_load_voltage_v')} V")
        findings.append({"severity": "fail", "wire": fwd, "check": "voltage_drop", "message": msg})


# ============================================================================== assembly

def _assemble(harness, opts, prepared, rows, seg_rows, loads, pairs, assumptions, findings) -> dict:
    ambient = opts["ambient_c"]
    statuses = [r["status"] for r in rows.values()]
    must_eval = [wid for wid, r in rows.items()
                 if str(r.get("signal_class") or "").lower() in ("power", "ground") or wid in loads
                 or r.get("role") == "return"]
    unevaluated = [wid for wid in must_eval if rows[wid]["status"] == "not_evaluated"]
    if any(s == "fail" for s in statuses):
        verdict = "fail"
    elif any(s == "warn" for s in statuses):
        verdict = "warn"
    elif (unevaluated or not any(s == "pass" for s in statuses)
          or any((rows[wid]["checks"].get("voltage_drop") or {}).get("status") == "unknown"
                 for wid in pairs)):
        verdict = "incomplete"
    else:
        verdict = "pass"
    sev_rank = {"fail": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: sev_rank.get(f.get("severity"), 3))
    seg_out = []
    for s in seg_rows.values():
        t = s["bundle_temp_c"]
        seg_out.append({"id": s["id"], "od_mm": s["od_mm"], "wires": s["n_wires"],
                        "loaded_wires": s["n_loaded"], "unloaded_wires": s["n_unloaded"],
                        "heat_w_per_m": _r(s["heat_w_per_m"], 3),
                        "bundle_temp_c": None if t is None or not math.isfinite(t) else _r(t, 2),
                        "note": s["note"]})
    return {
        "ok": True, "schema": SCHEMA, "harness_id": harness.get("id"), "verdict": verdict,
        "ambient_c": ambient,
        "coverage": {"wires": len(rows), "evaluated": sum(1 for r in rows.values() if r["status"] != "not_evaluated"),
                     "loaded": len(loads), "power_or_ground_unevaluated": unevaluated},
        "wires": [_public(r) for r in rows.values()],
        "segments": seg_out, "findings": findings, "assumptions": assumptions,
        "models": {
            "resistance": "ASTM B258 (AWG) / IEC 60228 (metric) at 20 C; alpha_20=0.00393/C",
            "thermal": "estimated I²R(T), cylindrical insulation conduction and Churchill-Chu "
                       "convection plus radiation; bundle contact/void resistance omitted",
            "ampacity_table": wd.NEC_SCOPE,
        },
        "limitations": [
            "steady-state only (wire thermal time constant not modelled); peak pulses use an adiabatic bound",
            "still air, natural convection only; no sun/engine-bay radiant load, no enclosed-duct derating",
            "no crimp/terminal resistance or temperature rise, no connector contact drop",
            "short-circuit (I^2t) wire protection and fuse time-current curves are not evaluated",
            "PWM ripple/EMI and cable inductance are not modelled; supply current_rms_a for PWM heating",
        ],
    }


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


# ============================================================================== entry points

def analyze(harness: dict, *, loads: Optional[list] = None, ambient_c: Optional[float] = None,
            rails: Optional[dict] = None, power_budget: Optional[dict] = None,
            fuses: Optional[list] = None, wire_specs: Optional[dict] = None,
            max_drop_pct: Optional[float] = None, emissivity: Optional[float] = None,
            count_unloaded_wires: bool = False, warn_margin_c: Optional[float] = None,
            warn_utilisation: Optional[float] = None, find_fixes: bool = True) -> dict:
    """Run the electrical check on a harness dict. See the module docstring."""
    assumptions: list[str] = []
    if ambient_c is None:
        amb = DEFAULT_AMBIENT_C
        assumptions.append(f"ambient {DEFAULT_AMBIENT_C:g} C assumed (NEC Table 310.16 reference); pass "
                           "ambient_c for hot zones")
    else:
        amb = float(ambient_c)
    if not math.isfinite(amb) or not -200 < amb <= 800:
        raise ValueError("ambient_c must be finite and in (-200, 800] C for the copper model")
    opts = {
        "ambient_c": amb,
        "emissivity": None if emissivity is None else float(emissivity),
        "loads": loads or [], "rails": dict(rails or {}), "power_budget": power_budget,
        "fuses": fuses or [], "wire_specs": wire_specs or {},
        "max_drop_pct": DEFAULT_MAX_DROP_PCT if max_drop_pct is None else float(max_drop_pct),
        "count_unloaded_wires": count_unloaded_wires,
        "warn_margin_c": DEFAULT_WARN_MARGIN_C if warn_margin_c is None else float(warn_margin_c),
        "warn_utilisation": DEFAULT_WARN_UTILISATION if warn_utilisation is None else float(warn_utilisation),
        "_assumptions": assumptions,
    }
    if opts["emissivity"] is not None and (not 0 <= opts["emissivity"] <= 1 or not math.isfinite(opts["emissivity"])):
        raise ValueError("emissivity must be finite and between 0 and 1")
    if not 0 <= opts["warn_utilisation"] <= 1:
        raise ValueError("warn_utilisation must be between 0 and 1")
    if not math.isfinite(opts["max_drop_pct"]) or opts["max_drop_pct"] < 0:
        raise ValueError("max_drop_pct must be finite and nonnegative")
    assumptions.append("horizontal cylinder in still air; equivalent solid conductor diameter from CSA; "
                       "shared bundle surface isothermal, contact/void resistance omitted; model is not certified")
    assumptions.append("insulation properties are material estimates unless overridden with wire-specific data; "
                       "NASA screening only when nasa_pressure_psia explicitly asserts its spaceflight scope")
    if not count_unloaded_wires:
        assumptions.append("wires with no load entry are treated as negligible-current signal wires and "
                           "are NOT counted as current-carrying conductors (count_unloaded_wires=True to count)")
    result = _run(harness, opts)
    if find_fixes:
        _attach_fixes(harness, opts, result)
    return result


# ============================================================================== fixes

def _upsized(conductor_wire: dict, steps: int) -> Optional[dict]:
    """Copy of a wire dict stepped ``steps`` sizes thicker (insulation wall kept), or None."""
    w = copy.deepcopy(conductor_wire)
    od = _num(w.get("insulation_od_mm"))
    try:
        if w.get("gauge_awg") not in (None, ""):
            n = wd.parse_awg(w["gauge_awg"])
            d0 = wd.awg_diameter_mm(n)
            for _ in range(steps):
                n2 = wd.next_larger_awg(n)
                if n2 is None:
                    return None
                n = n2
            w["gauge_awg"] = n
            d1 = wd.awg_diameter_mm(n)
        elif w.get("csa_mm2"):
            c = float(w["csa_mm2"])
            d0 = wd.metric_diameter_mm(c)
            for _ in range(steps):
                c2 = wd.next_larger_csa(c)
                if c2 is None:
                    return None
                c = c2
            w["csa_mm2"] = c
            d1 = wd.metric_diameter_mm(c)
        else:
            return None
    except NotTabulated:
        return None
    if od is not None:
        w["insulation_od_mm"] = od + (d1 - d0)
    w.pop("r20_ohm_per_km", None)   # a datasheet value belongs to the old size
    return w


def _fail_ids(result: dict) -> set[str]:
    return {w["id"] for w in result["wires"] if w["status"] == "fail"}


def _attach_fixes(harness: dict, opts: dict, result: dict) -> None:
    failing = sorted(_fail_ids(result))
    if not failing:
        return
    base_fail = set(failing)
    # Datasheet resistance is size-specific; do not reuse it to certify an upsized candidate.
    override_specs = opts["wire_specs"]
    fixes: list[dict] = []
    done: set[str] = set()
    by_id = {w["id"]: w for w in harness.get("wires") or []}
    for wid in failing:
        if wid in done:
            continue
        group = [wid]
        row = next(w for w in result["wires"] if w["id"] == wid)
        vd = row["checks"].get("voltage_drop") or {}
        for rid in vd.get("return_wires") or []:
            if rid in by_id and rid not in group:
                group.append(rid)
        if any((override_specs.get(g) or {}).get(k) is not None
               for g in [*group, "*"] for k in ("r20_ohm_per_km", "ampacity")):
            fixes.append({"wires": group, "verified": False,
                          "message": "Obtain resistance and insulation data for candidate larger wires "
                                     "before checking an upsize."})
            continue
        found = None
        for steps in range(1, 9):
            trial = copy.deepcopy(harness)
            tw = {w["id"]: w for w in trial["wires"]}
            ok_all = True
            for gid in group:
                up = _upsized(by_id[gid], steps)
                if up is None:
                    ok_all = False
                    break
                tw[gid].clear()
                tw[gid].update(up)
            if not ok_all:
                break
            # bundles grow: re-estimate each touched segment OD by conserving wire area
            for seg in trial.get("segments") or []:
                members = seg.get("wires") or []
                if any(g in members for g in group) and seg.get("od_mm"):
                    old = sum((by_id[m].get("insulation_od_mm") or 0.0) ** 2 for m in members if m in by_id)
                    new = sum((tw[m].get("insulation_od_mm") or 0.0) ** 2 for m in members if m in tw)
                    if old > 0:
                        seg["od_mm"] = float(seg["od_mm"]) * math.sqrt(new / old)
            inner = dict(opts)
            inner["_assumptions"] = []
            res = _run(trial, inner)
            new_fail = _fail_ids(res)
            same = {w["id"]: w for w in res["wires"]}
            if (wid in same and same[wid]["status"] == "pass"
                    and not (new_fail - base_fail)
                    and (same[wid]["checks"].get("voltage_drop") or {}).get("status", "pass") == "pass"):
                found = (steps, res, trial)
                break
        if found is None:
            fixes.append({"wires": group, "verified": False,
                          "message": "no combination of up to 8 larger sizes clears this wire without "
                                     "creating a new failure; reduce the load, split the bundle, raise the "
                                     "insulation rating, or shorten the run"})
            continue
        steps, res, trial = found
        tw = {w["id"]: w for w in trial["wires"]}
        fx = {"wires": group, "verified": True, "steps": steps,
              "changes": [{"wire": g, "from": _size_label(by_id[g]), "to": _size_label(tw[g])} for g in group],
              "assumption": "insulation wall thickness kept; bundle OD re-estimated by conserving wire area",
              "result": {k: v for k, v in same_summary(res, wid).items()}}
        fixes.append(fx)
        done.update(group)
        for f in result["findings"]:
            if f.get("wire") in group and f.get("severity") == "fail" and "fix" not in f:
                f["fix"] = {"action": "upsize", "changes": fx["changes"], "verified": True}
    result["fixes"] = fixes


def _size_label(w: dict) -> str:
    if w.get("gauge_awg") not in (None, ""):
        return f"{wd.awg_label(wd.parse_awg(w['gauge_awg']))} AWG"
    return f"{float(w['csa_mm2']):g} mm2"


def same_summary(res: dict, wid: str) -> dict:
    row = next(w for w in res["wires"] if w["id"] == wid)
    vd = row["checks"].get("voltage_drop") or {}
    return {"status": row["status"], "conductor_temp_c": row.get("conductor_temp_c"),
            "drop_pct": vd.get("drop_pct"), "load_end_v": vd.get("load_end_v"),
            "derated_ampacity_a": row.get("derated_ampacity_a")}
