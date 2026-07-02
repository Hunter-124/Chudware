"""Property-model tests for :mod:`engine.piping.properties`.

The built-in (dependency-free) path is always exercised via ``prefer="builtin"`` so the values
are deterministic on a machine without CoolProp/thermo. Physics is checked against textbook
reference values (hand calculations): ideal-gas density, Sutherland viscosity monotonicity, and
the tabulated IAPWS water points. House style: ``main()`` + asserts.

Run (from repo root):  python -m engine.piping.test_properties
"""

from __future__ import annotations

from engine.piping import properties as P

R = P.R_UNIVERSAL


def test_water_reference_point():
    r = P.fluid_properties("water", T=293.15, P=P.P_ATM, prefer="builtin")
    assert r["ok"] and r["phase"] == "liquid", r
    assert abs(r["rho"] - 998.2) < 0.1, r          # NIST/IAPWS @20 °C
    assert abs(r["mu"] - 1.002e-3) < 5e-6, r
    assert abs(r["pvap"] - 2339.0) < 5.0, r        # saturation @20 °C
    assert "IAPWS" in r["source"], r


def test_water_out_of_range_softfails():
    r = P.fluid_properties("water", T=450.0, prefer="builtin")   # 176 °C — past table
    assert r["ok"] is False and "range" in r["reason"], r


def test_air_ideal_gas_and_scaling():
    # Standard air @15 °C, 1 atm ≈ 1.225 kg/m³ (ideal gas, M=0.0289647).
    a = P.fluid_properties("air", T=288.15, P=P.P_ATM, prefer="builtin")
    assert a["ok"] and a["phase"] == "gas", a
    hand = P.P_ATM * 0.0289647 / (R * 288.15)
    assert abs(a["rho"] - hand) < 1e-6, (a["rho"], hand)
    assert abs(a["rho"] - 1.225) < 0.005, a
    assert abs(a["mu"] - 1.81e-5) < 5e-7, a        # Sutherland @20 °C
    # Ideal-gas density is linear in pressure.
    a2 = P.fluid_properties("air", T=288.15, P=2 * P.P_ATM, prefer="builtin")
    assert abs(a2["rho"] - 2 * a["rho"]) < 1e-6, (a2["rho"], a["rho"])


def test_sutherland_viscosity_increases_with_T():
    lo = P.fluid_properties("nitrogen", T=250.0, prefer="builtin")["mu"]
    hi = P.fluid_properties("nitrogen", T=400.0, prefer="builtin")["mu"]
    assert hi > lo, (lo, hi)                        # gas viscosity rises with temperature


def test_named_gases_have_molar_mass():
    for name in ("methane", "natural_gas", "nitrogen", "carbon_dioxide"):
        r = P.fluid_properties(name, T=288.15, P=3e6, prefer="builtin")
        assert r["ok"] and r["molar_mass"] and r["molar_mass"] > 0, r
        assert r["rho"] > 0 and r["mu"] > 0, r
        assert r["source"], r                       # every value is cited


def test_unknown_fluid_softfails():
    r = P.fluid_properties("unobtanium", prefer="builtin")
    assert r["ok"] is False and "unknown" in r["reason"], r
    assert P.fluid_properties("air", T=-5.0, prefer="builtin")["ok"] is False


def test_resolve_solver_fluid():
    # None -> water default, sourced.
    d = P.resolve_solver_fluid(None)
    assert abs(d["rho"] - 998.2) < 0.1 and d["name"] == "water", d
    assert d["source"], d
    # explicit rho/mu passthrough.
    e = P.resolve_solver_fluid({"rho": 850.0, "mu": 5e-3})
    assert e["rho"] == 850.0 and e["mu"] == 5e-3, e
    # named lookup carries molar_mass + phase for the gas solver.
    g = P.resolve_solver_fluid({"name": "methane", "T": 288.15, "P": 3e6})
    assert g["phase"] == "gas" and g["molar_mass"] > 0, g
    # T_C accepted as celsius.
    c = P.resolve_solver_fluid({"name": "water", "T_C": 20.0})
    assert abs(c["rho"] - 998.2) < 0.1, c
    # bad shape -> contract-shaped.
    assert P.resolve_solver_fluid({"foo": 1})["ok"] is False


def main():
    test_water_reference_point()
    test_water_out_of_range_softfails()
    test_air_ideal_gas_and_scaling()
    test_sutherland_viscosity_increases_with_T()
    test_named_gases_have_molar_mass()
    test_unknown_fluid_softfails()
    test_resolve_solver_fluid()
    print("test_properties OK")


if __name__ == "__main__":
    main()
