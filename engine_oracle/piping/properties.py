"""Fluid thermophysical properties for :mod:`engine.piping`.

Single entry point :func:`fluid_properties(name, *, T, P) -> dict` returns density ``rho``
(kg/m³), dynamic viscosity ``mu`` (Pa·s) and — for liquids — saturation (vapour) pressure
``pvap`` (Pa) for a named fluid at a temperature/pressure, so the hydraulic solver
(:mod:`engine.piping.solve`) draws its inputs from a **sourced** property model instead of a
hardcoded density/viscosity constant.

Backend order (feature-detected, soft-fail — the house pattern of ``catalog.nearest_pipe`` and
``solve.have_fluids``):

1. **CoolProp** (MIT) — REFPROP-grade equations of state, when importable.
2. **thermo** (MIT, Caleb Bell family — the same author as the already-vendored ``fluids``),
   when importable.
3. **Built-in cited fallback** — always available, *no new dependency*: liquid water from a
   tabulated IAPWS/NIST reference (interpolated within its stated range); gases from the
   ideal-gas law (density) + Sutherland's law (viscosity) with cited constants.

**No value is ever fabricated.** Every successful lookup carries a ``source`` string and a
``basis`` note; an unknown fluid or an out-of-range condition degrades **contract-shaped**
(``{"ok": False, "reason": ..., "hint": ...}``), never a guess and never a traceback.

Units are SI throughout this module (K, Pa, kg/m³, Pa·s), matching the solver's internal SI
working units; the piping document itself stays in mm (contract §1).
"""

from __future__ import annotations

from typing import Any, Optional

R_UNIVERSAL = 8.314462618  # J/(mol·K), CODATA 2018
T0_C = 273.15              # 0 °C in K
P_ATM = 101325.0           # standard atmosphere, Pa
STD_T = 288.15             # 15 °C — ISO 13443 "standard" reference for gas volumes
STD_P = P_ATM


def _err(reason: str, hint: Optional[str] = None, **extra: Any) -> dict:
    out: dict[str, Any] = {"ok": False, "reason": reason}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return out


# ── fluid registry ────────────────────────────────────────────────────────────
# Canonical name -> aliases. Keeps CoolProp/thermo names and everyday names aligned.
_ALIASES = {
    "water": {"water", "h2o"},
    "air": {"air"},
    "methane": {"methane", "ch4"},
    "natural_gas": {"natural_gas", "natural gas", "naturalgas", "ng"},
    "nitrogen": {"nitrogen", "n2"},
    "carbon_dioxide": {"carbon_dioxide", "carbon dioxide", "co2"},
}

# CoolProp fluid names for the EOS backend.
_COOLPROP_NAME = {
    "water": "Water", "air": "Air", "methane": "Methane",
    "nitrogen": "Nitrogen", "carbon_dioxide": "CarbonDioxide",
    # natural_gas handled specially (pseudo-pure via methane + gravity correction).
}

# Ideal-gas molar mass M (kg/mol) + Sutherland viscosity constants
# (mu_ref at T_ref, Sutherland S) for the built-in gas fallback.
#   mu(T) = mu_ref * (T/T_ref)^1.5 * (T_ref + S) / (T + S)      [Sutherland 1893]
# Constants: White, "Viscous Fluid Flow" 3rd ed. Table 1-2 (air, N2, CO2); CRC Handbook /
# Sutherland-fit of NIST data (methane). Molar masses: IUPAC 2021 atomic weights.
_GAS = {
    #            M (kg/mol)   mu_ref (Pa·s)  T_ref (K)  S (K)   source note
    "air":            (0.0289647, 1.716e-5, 273.15, 110.4),
    "methane":        (0.0160425, 1.024e-5, 273.15, 164.0),
    "nitrogen":       (0.0280134, 1.663e-5, 273.15, 111.0),
    "carbon_dioxide": (0.0440095, 1.370e-5, 273.15, 222.0),
}
# Natural gas: a pipeline-representative pseudo-composition (specific gravity ~0.60).
# M and Sutherland constants taken as methane-dominant with a standard gravity correction.
_NATURAL_GAS = (0.0179, 1.10e-5, 273.15, 164.0)  # M for G=0.60 vs air (0.60*28.9647e-3)

# Built-in liquid-water reference table (1 atm). rho, mu, pvap vs temperature.
# Sources: rho & pvap — IAPWS-IF97 / NIST Chemistry WebBook saturation line;
# mu — IAPWS 2008 formulation / CRC Handbook 97th ed. Values at 10 °C steps, 0–100 °C.
_WATER_T_C = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
_WATER_RHO = [999.84, 999.70, 998.21, 995.65, 992.22, 988.03,
              983.20, 977.76, 971.79, 965.31, 958.35]           # kg/m³
_WATER_MU = [1.7916e-3, 1.3060e-3, 1.0016e-3, 0.7972e-3, 0.6527e-3, 0.5465e-3,
             0.4660e-3, 0.4035e-3, 0.3540e-3, 0.3142e-3, 0.2818e-3]   # Pa·s
_WATER_PVAP = [611.3, 1228.0, 2339.0, 4246.0, 7384.0, 12350.0,
               19940.0, 31190.0, 47390.0, 70140.0, 101325.0]    # Pa (saturation)

_WATER_SOURCE = ("IAPWS-IF97 / NIST Chemistry WebBook (rho, pvap); "
                 "IAPWS 2008 / CRC Handbook 97th ed. (mu)")


def _canon(name: str) -> Optional[str]:
    key = str(name).strip().lower()
    for canon, al in _ALIASES.items():
        if key in al:
            return canon
    return None


def _interp(x: float, xs: list, ys: list) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            f = (x - xs[i]) / (xs[i + 1] - xs[i])
            return ys[i] + f * (ys[i + 1] - ys[i])
    return ys[-1]


def have_coolprop() -> bool:
    try:
        import CoolProp  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def have_thermo() -> bool:
    try:
        import thermo  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# ── backends ──────────────────────────────────────────────────────────────────

def _coolprop_props(canon: str, T: float, P: float) -> Optional[dict]:
    cp_name = _COOLPROP_NAME.get(canon if canon != "natural_gas" else "methane")
    if cp_name is None:
        return None
    try:
        from CoolProp.CoolProp import PropsSI
        import CoolProp
        rho = float(PropsSI("D", "T", T, "P", P, cp_name))
        mu = float(PropsSI("V", "T", T, "P", P, cp_name))
        pvap = None
        try:
            pvap = float(PropsSI("P", "T", T, "Q", 0, cp_name))
        except Exception:  # noqa: BLE001  (supercritical / no two-phase line)
            pvap = None
        note = f"CoolProp {getattr(CoolProp, '__version__', '?')} EOS PropsSI({cp_name})"
        if canon == "natural_gas":
            note += " (methane proxy)"
        return {"rho": rho, "mu": mu, "pvap": pvap, "source": note}
    except Exception:  # noqa: BLE001
        return None


def _thermo_props(canon: str, T: float, P: float) -> Optional[dict]:
    name = "methane" if canon == "natural_gas" else canon.replace("_", " ")
    try:
        import thermo
        chem = thermo.Chemical(name, T=T, P=P)
        rho = float(chem.rho)
        mu = float(chem.mu)
        pvap = None
        try:
            pvap = float(chem.Psat) if chem.Psat is not None else None
        except Exception:  # noqa: BLE001
            pvap = None
        note = f"thermo {getattr(thermo, '__version__', '?')} Chemical({name})"
        if canon == "natural_gas":
            note += " (methane proxy)"
        return {"rho": rho, "mu": mu, "pvap": pvap, "source": note}
    except Exception:  # noqa: BLE001
        return None


def _builtin_props(canon: str, T: float, P: float) -> dict:
    """Cited fallback: tabulated liquid water; ideal-gas + Sutherland gases."""
    if canon == "water":
        Tc = T - T0_C
        if Tc < _WATER_T_C[0] - 0.5 or Tc > _WATER_T_C[-1] + 0.5:
            return _err(f"water property table covers 0–100 °C; T={Tc:.1f} °C is out of range",
                        hint="install CoolProp (MIT) for the full IAPWS-IF97 range")
        return {
            "rho": _interp(Tc, _WATER_T_C, _WATER_RHO),
            "mu": _interp(Tc, _WATER_T_C, _WATER_MU),
            "pvap": _interp(Tc, _WATER_T_C, _WATER_PVAP),
            "source": f"built-in table [{_WATER_SOURCE}], linear interp @ {Tc:.1f} °C",
        }
    # gases
    if canon == "natural_gas":
        M, mu_ref, T_ref, S = _NATURAL_GAS
        src = ("ideal-gas rho (M=0.0179 kg/mol, G=0.60 pipeline gas, ISO 6976 basis); "
               "Sutherland mu (methane-dominant, White VFF Table 1-2)")
    else:
        params = _GAS.get(canon)
        if params is None:
            return _err(f"no built-in property model for {canon!r}",
                        hint="supported: water, air, methane, natural_gas, nitrogen, carbon_dioxide")
        M, mu_ref, T_ref, S = params
        src = (f"ideal-gas rho (M={M} kg/mol, IUPAC 2021); "
               f"Sutherland mu (mu_ref={mu_ref} @ {T_ref} K, S={S} K; White VFF Table 1-2 / CRC)")
    if T <= 0:
        return _err(f"absolute temperature must be > 0 K (got {T})")
    rho = P * M / (R_UNIVERSAL * T)                       # ideal gas, Z≈1
    mu = mu_ref * (T / T_ref) ** 1.5 * (T_ref + S) / (T + S)
    return {"rho": rho, "mu": mu, "pvap": None, "source": src, "molar_mass": M}


# ── public API ──────────────────────────────────────────────────────────────

def fluid_properties(name: str, *, T: float = 293.15, P: float = P_ATM,
                     prefer: Optional[str] = None) -> dict:
    """Sourced thermophysical properties for a named fluid at ``T`` (K), ``P`` (Pa).

    Returns ``{"ok": True, "name", "phase", "T_K", "P_Pa", "rho", "mu", "pvap", "molar_mass",
    "source", "backend"}`` — ``pvap`` (Pa) is ``None`` for gases — or a contract-shaped
    ``{"ok": False, "reason", "hint"}`` for an unknown fluid / out-of-range condition.

    Backend order is CoolProp -> thermo -> built-in cited fallback; ``prefer="builtin"`` forces
    the last (used by tests to exercise the dependency-free path deterministically).
    """
    canon = _canon(name)
    if canon is None:
        return _err(f"unknown fluid {name!r}",
                    hint="supported: water, air, methane, natural_gas, nitrogen, carbon_dioxide")
    if T <= 0:
        return _err(f"absolute temperature must be > 0 K (got {T})",
                    hint="pass T in kelvin, e.g. T=293.15 for 20 °C")
    if P <= 0:
        return _err(f"absolute pressure must be > 0 Pa (got {P})",
                    hint="pass P in pascals absolute, e.g. P=101325 for 1 atm")

    props = None
    backend = "builtin"
    if prefer != "builtin":
        if have_coolprop():
            props = _coolprop_props(canon, T, P)
            if props is not None:
                backend = "coolprop"
        if props is None and have_thermo():
            props = _thermo_props(canon, T, P)
            if props is not None:
                backend = "thermo"
    if props is None:
        props = _builtin_props(canon, T, P)
        backend = "builtin"
    if not props.get("ok", True) and "reason" in props:
        return props  # out-of-range from builtin

    phase = "liquid" if canon == "water" else "gas"
    return {
        "ok": True,
        "name": canon,
        "phase": phase,
        "T_K": round(T, 4),
        "P_Pa": round(P, 3),
        "rho": props["rho"],
        "mu": props["mu"],
        "pvap": props.get("pvap"),
        "molar_mass": props.get("molar_mass"),
        "source": props["source"],
        "backend": backend,
    }


def resolve_solver_fluid(fluid: Optional[dict]) -> dict:
    """Normalise a solver ``fluid`` argument into ``{rho, mu, name, source, ...}``.

    Accepts three shapes and never fabricates:
      * ``None``               -> water @ 20 °C, 1 atm (the historical default), sourced.
      * ``{"rho":..,"mu":..}`` -> used verbatim (explicit override), ``source="caller-supplied"``.
      * ``{"name":"air","T":..,"P":..}`` (T in K, P in Pa; ``T_C`` in °C also accepted) ->
        looked up via :func:`fluid_properties`.

    Returns a dict with ``rho``/``mu`` (and ``pvap``/``phase``/``source`` when available) on
    success, or a contract-shaped ``{"ok": False, ...}`` that the solver surfaces unchanged.
    """
    if fluid is None:
        p = fluid_properties("water", T=293.15, P=P_ATM)
        return {"name": "water", "rho": p["rho"], "mu": p["mu"], "pvap": p["pvap"],
                "phase": "liquid", "source": p["source"], "backend": p["backend"],
                "T_K": 293.15, "P_Pa": P_ATM, "molar_mass": None}
    if "rho" in fluid and "mu" in fluid:
        out = dict(fluid)
        out.setdefault("name", fluid.get("name", "custom"))
        out.setdefault("source", "caller-supplied rho/mu")
        out.setdefault("pvap", fluid.get("pvap"))
        out.setdefault("phase", fluid.get("phase", "liquid"))
        return out
    if "name" in fluid:
        T = fluid.get("T")
        if T is None and "T_C" in fluid:
            T = float(fluid["T_C"]) + T0_C
        if T is None:
            T = 293.15
        P = fluid.get("P", P_ATM)
        p = fluid_properties(fluid["name"], T=float(T), P=float(P),
                             prefer=fluid.get("prefer"))
        if not p.get("ok"):
            return p
        return {"name": p["name"], "rho": p["rho"], "mu": p["mu"], "pvap": p["pvap"],
                "phase": p["phase"], "source": p["source"], "backend": p["backend"],
                "T_K": p["T_K"], "P_Pa": p["P_Pa"], "molar_mass": p["molar_mass"]}
    return _err("fluid must be null, {'rho','mu'} or {'name', 'T', 'P'}",
                hint="e.g. {'name':'air','T':293.15,'P':500000} or {'rho':998.2,'mu':1.002e-3}")
