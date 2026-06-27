"""Script-style tests for :mod:`engine.dfm.process_profiles`.

Run as ``python -m engine.dfm.test_process_profiles`` from the repo root. Prints one line per
case (PASS/FAIL) and exits nonzero if anything failed — the project's script-style test
convention (cf. ``engine/test_parts_library.py`` / ``mcp_server.test_ecad_tools``), NOT pytest.

Covers the load-bearing rules and numbers from ``docs/INTAKE_MEASUREMENT_DFM_PLAN.md`` section 5:
profile loading + copy isolation, the FDM default, explicit-keyword short-circuit, the
ambiguity gate (within-margin and two-way ties), the metal+FDM must-block, the fit/hole math,
per-printer overrides, and the documented resolver return shape.
"""
from __future__ import annotations

import sys

from engine.dfm import process_profiles as pp


_FAILS: list[str] = []
_PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    """Record one assertion; print PASS/FAIL without aborting the whole run on a failure."""
    global _PASSES
    if cond:
        _PASSES += 1
        print(f"PASS  {name}")
    else:
        _FAILS.append(name)
        print(f"FAIL  {name}" + (f"  -- {detail}" if detail else ""))


def approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(float(a) - float(b)) <= tol


# --------------------------------------------------------------------------- profiles


def test_every_profile_loads_and_is_a_copy() -> None:
    for proc in pp.KNOWN_PROCESSES:
        prof = pp.get_profile(proc)
        check(f"get_profile({proc!r}) returns the right process",
              prof["process"] == proc, repr(prof.get("process")))
        # mutating the returned copy must NOT leak into the shared table or a second copy
        prof["params"]["min_wall_mm"] = -999
        prof["params"]["fit_classes_mm"]["slip"] = -999
        fresh = pp.get_profile(proc)
        leaked = (fresh["params"]["min_wall_mm"] == -999
                  or fresh["params"]["fit_classes_mm"].get("slip") == -999)
        check(f"get_profile({proc!r}) returns an isolated COPY", not leaked,
              "mutation leaked into shared table")


def test_unknown_process_raises() -> None:
    try:
        pp.get_profile("sla_resin_widget")
        check("get_profile(unknown) raises ValueError", False, "no exception")
    except ValueError as e:
        check("get_profile(unknown) raises ValueError listing known processes",
              all(p in str(e) for p in ("fdm", "cnc_mill")), str(e))


def test_fdm_load_bearing_numbers() -> None:
    p = pp.get_profile("fdm")["params"]
    check("FDM min_wall 1.0", approx(p["min_wall_mm"], 1.0))
    check("FDM hole_comp 0.20", approx(p["hole_comp_mm"], 0.20))
    check("FDM tolerance_band 0.40", approx(p["tolerance_band_mm"], 0.40))
    f = p["fit_classes_mm"]
    check("FDM slip 0.40", approx(f["slip"], 0.40))
    check("FDM location 0.20", approx(f["location"], 0.20))
    check("FDM press 0.10", approx(f["press"], 0.10))
    check("FDM free 0.50", approx(f["free"], 0.50))
    check("FDM overhang flag >45deg", p["overhang"]["max_deg"] == 45)
    check("FDM thread = heat_set_insert", p["thread"]["strategy"] == "heat_set_insert")
    check("FDM printed_ok_min = M5", p["thread"]["printed_ok_min"] == "M5")


# --------------------------------------------------------------------------- resolver


_RESULT_KEYS = {"chosen", "confidence", "runner_up", "ambiguous", "must_block",
                "clarification", "assumptions", "why"}


def test_resolver_return_shape() -> None:
    r = pp.resolve_process("a small widget")
    check("resolve_process returns all documented keys",
          set(r) == _RESULT_KEYS, f"got {sorted(r)}")
    check("resolve_process assumptions is a list", isinstance(r["assumptions"], list))


def test_fdm_is_default() -> None:
    r = pp.resolve_process("design a small enclosure for a sensor")
    check("bare request defaults to FDM, not ambiguous",
          r["chosen"] == "fdm" and not r["ambiguous"], repr(r))


def test_explicit_print_short_circuits() -> None:
    r = pp.resolve_process("3D print this bracket")
    check("'3D print this bracket' -> fdm, no ambiguity",
          r["chosen"] == "fdm" and not r["ambiguous"] and not r["must_block"], repr(r))


def test_machine_from_aluminum_short_circuits() -> None:
    r = pp.resolve_process("machine this from aluminum", material="aluminum")
    check("'machine from aluminum' -> cnc short-circuit, no ask",
          r["chosen"] == "cnc_mill" and not r["ambiguous"], repr(r))


def test_steel_plate_threaded_flat_is_ambiguous() -> None:
    # the plan's worked contrast: constant 2mm -> sheet cue; threaded-in-metal + flatness -> CNC
    r = pp.resolve_process(
        "2mm steel plate, 100x60, four M4 threaded holes, must be flat")
    check("steel plate / threaded / flat fires the process gate (ambiguous)",
          r["ambiguous"] is True, repr(r))
    check("ambiguous result carries a clarification {ask,suggested,options}",
          isinstance(r["clarification"], dict)
          and {"ask", "suggested", "options"} <= set(r["clarification"]), repr(r))
    opts = set(r["clarification"]["options"]) if r["clarification"] else set()
    check("clarification offers sheet_metal vs cnc_mill",
          {"sheet_metal", "cnc_mill"} <= opts, repr(opts))


def test_metal_plus_fdm_must_block() -> None:
    r = pp.resolve_process("3D print this in steel", material="steel")
    check("explicit FDM + metal material -> must_block",
          r["must_block"] is True and r["ambiguous"] is True, repr(r))


def test_injection_quantity_cue() -> None:
    r = pp.resolve_process("make 5000 of this molded housing", quantity=5000)
    check("high-qty molded housing -> injection",
          r["chosen"] == "injection" and not r["ambiguous"], repr(r))


def test_laser_gasket_cue() -> None:
    # a lone soft non-FDM cue with no anchor lands within margin -> gate fires (rule a)
    r = pp.resolve_process("a flat gasket")
    check("'flat gasket' surfaces laser within margin -> ambiguous gate",
          r["ambiguous"] is True and "laser" in {r["chosen"], r["runner_up"]}, repr(r))


# --------------------------------------------------------------------------- fit math


def test_modeled_dim_fdm_slip_adds_040() -> None:
    m = pp.modeled_dim(3.00, "slip", "fdm", is_hole=False)
    check("FDM slip shaft passthrough 3.00 -> 3.40",
          approx(m["modeled_mm"], 3.40) and approx(m["clearance_mm"], 0.40), repr(m))
    # a printed hole/bore additionally absorbs the hole-comp
    mh = pp.modeled_dim(3.00, "slip", "fdm", is_hole=True)
    check("FDM slip printed bore adds slip + hole_comp (3.00 -> 3.60)",
          approx(mh["modeled_mm"], 3.60) and approx(mh["hole_comp_mm"], 0.20), repr(mh))
    check("modeled_dim returns rationale text", bool(m.get("rationale")))


def test_fit_clearance_unknown_class_raises() -> None:
    try:
        pp.fit_clearance("fdm", "interference")
        check("fit_clearance(unknown class) raises", False, "no exception")
    except ValueError:
        check("fit_clearance(unknown class) raises ValueError", True)
    try:
        pp.fit_clearance("sheet_metal", "slip")
        check("fit_clearance on a class the process lacks raises", False, "no exception")
    except ValueError:
        check("fit_clearance(sheet_metal, slip) raises (no diametral class)", True)


def test_per_printer_override_changes_hole_comp() -> None:
    base = pp.get_profile("fdm")["params"]["hole_comp_mm"]
    over = pp.get_profile("fdm", overrides={"hole_comp": 0.35})["params"]
    check("per-printer override changes hole_comp", approx(over["hole_comp_mm"], 0.35)
          and not approx(base, 0.35), f"base={base} over={over['hole_comp_mm']}")
    # xy_comp widens the general band and is recorded
    xy = pp.get_profile("fdm", overrides={"xy_comp": 0.05})["params"]
    check("per-printer xy_comp widens tolerance band + is recorded",
          approx(xy["xy_comp_mm"], 0.05) and xy["tolerance_band_mm"] > 0.40, repr(xy))


# --------------------------------------------------------------------------- process block


def test_process_block_text() -> None:
    prof = pp.get_profile("fdm")
    prof["assumed"] = True
    txt = pp.process_block_text(prof)
    ok = ("PROCESS BLOCK" in txt and "Min wall 1" in txt
          and "slip" in txt and "heat set insert" in txt)
    check("process_block_text renders min wall + fit table + thread strategy", ok, txt[:200])


def main() -> int:
    test_every_profile_loads_and_is_a_copy()
    test_unknown_process_raises()
    test_fdm_load_bearing_numbers()
    test_resolver_return_shape()
    test_fdm_is_default()
    test_explicit_print_short_circuits()
    test_machine_from_aluminum_short_circuits()
    test_steel_plate_threaded_flat_is_ambiguous()
    test_metal_plus_fdm_must_block()
    test_injection_quantity_cue()
    test_laser_gasket_cue()
    test_modeled_dim_fdm_slip_adds_040()
    test_fit_clearance_unknown_class_raises()
    test_per_printer_override_changes_hole_comp()
    test_process_block_text()

    total = _PASSES + len(_FAILS)
    print("-" * 60)
    if _FAILS:
        print(f"FAILED {len(_FAILS)}/{total}: {', '.join(_FAILS)}")
        return 1
    print(f"PROCESS PROFILES TESTS PASSED ({_PASSES}/{total} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
