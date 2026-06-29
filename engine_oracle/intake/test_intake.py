"""Script-style tests for :mod:`engine.intake.intake` — the requirements-intake "brain".

Run as ``python -m engine.intake.test_intake`` from the repo root. Prints one line per case
(PASS/FAIL) and exits nonzero if anything failed — the project's script-style convention
(cf. ``engine/dfm/test_process_profiles.py`` / ``engine/test_parts_library.py`` /
``mcp_server.test_ecad_tools``), NOT pytest.

Coverage maps to ``docs/INTAKE_MEASUREMENT_DFM_PLAN.md`` and the build spec:

* the worked gearmotor-bracket journey (§1): shaft slip demands a measured value, vision-low and
  hole-spacing-medium both raise demands, a cosmetic dim defaults+flags with no demand, the gate
  resolves after typed confirmation, and the FitReport comes back ``pass``;
* the deterministic confidence scorer's tiers from concrete signals;
* required-tier scaling (press/slip/clearance, + the photo downgrade);
* the schemas' JSON round-trip;
* the taxonomy + plain-word classifier;
* ``coupon_spec`` for a bore;
* the never-block gate (no hard block by default; must-block on metal+FDM).
"""
from __future__ import annotations

import sys

from engine.intake import intake as ik
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


def approx(a, b, tol: float = 1e-6) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


# --------------------------------------------------------------------------- fixtures


def gearmotor_part() -> ik.MeasuredPartProfile:
    """The §1 worked example: a photographed gearmotor with three mating features.

    shaft -> low-confidence single soft close-up; holes -> medium; pocket -> high. Overall bracket
    size is cosmetic and not even a feature here (it defaults + flags, never asked).
    """
    shaft = ik.new_measured_feature(
        "output_shaft", "shaft_in_bore", dims={"dia_mm": 3.0},
        confidence="low", uncert_mm=0.4, provenance={"method": "card_homography", "photos": ["p3"]})
    holes = ik.new_measured_feature(
        "mount_holes", "bolt_pattern", dims={"spacing_mm": 17.0, "dia_mm": 2.2, "count": 2},
        confidence="medium", provenance={"method": "card_homography"})
    pocket = ik.new_measured_feature(
        "body_pocket", "prism_pocket", dims={"x_mm": 24.1, "y_mm": 31.0},
        confidence="high", uncert_mm=0.3, provenance={"method": "card_homography",
                                                       "photos": ["p1", "p2"]})
    return ik.new_measured_part("mp_1", "gearmotor", features=[shaft, holes, pocket])


# --------------------------------------------------------------------------- 1. schemas


def test_schema_roundtrip() -> None:
    part = gearmotor_part()
    d = part.to_dict()
    back = ik.MeasuredPartProfile.from_dict(d)
    check("MeasuredPartProfile round-trips through dict", back.to_dict() == d, repr(back.to_dict()))
    led = ik.build_ledger(part, "fdm")
    lback = ik.CriticalDimensionLedger.from_dict(led.to_dict())
    check("CriticalDimensionLedger round-trips", lback.to_dict() == led.to_dict())
    st = ik.new_intake_state(doc_id="doc_7", revision=42)
    check("IntakeState round-trips", ik.IntakeState.from_dict(st.to_dict()).to_dict() == st.to_dict())
    fr = ik.new_fit_report("fdm")
    check("FitReport round-trips", ik.FitReport.from_dict(fr.to_dict()).to_dict() == fr.to_dict())


def test_new_factories_present() -> None:
    check("new_measured_feature defaults fit from archetype",
          ik.new_measured_feature("s", "shaft_in_bore").fit == "slip")
    check("new_measured_part starts empty", ik.new_measured_part("mp", "x").features == [])
    cd = ik.new_critical_dimension("cd_1", "k.dia", "k", "shaft_in_bore")
    check("new_critical_dimension starts unknown + critical",
          cd.status == "unknown" and cd.is_critical is True)
    check("new_intake_state starts open", ik.new_intake_state().status == "open")


# --------------------------------------------------------------------------- 2. taxonomy


def test_archetype_table_all_critical() -> None:
    expected = {"shaft_in_bore", "bolt_pattern", "prism_pocket", "connector_cutout",
                "board_outline", "snap_fit", "lid_mate", "press_boss"}
    check("ARCHETYPES has all eight spec'd archetypes", set(ik.ARCHETYPES) == expected,
          repr(sorted(ik.ARCHETYPES)))
    check("every archetype is_critical=True", all(a.is_critical for a in ik.ARCHETYPES.values()))
    check("every archetype has critical_keys + a fit class",
          all(a.critical_keys and a.default_fit for a in ik.ARCHETYPES.values()))
    check("shaft_in_bore default fit = slip", ik.ARCHETYPES["shaft_in_bore"].default_fit == "slip")
    check("press_boss default fit = press", ik.ARCHETYPES["press_boss"].default_fit == "press")


def test_classify_plain_words() -> None:
    cases = {
        "the output shaft pokes through": "shaft_in_bore",
        "it bolts on with two screws": "bolt_pattern",
        "a USB-C port on the side": "connector_cutout",
        "holds the circuit board": "board_outline",
        "a slot for the battery": "prism_pocket",
        "the lid snaps on": "snap_fit",
        "press-fit boss": "press_boss",
    }
    for text, want in cases.items():
        got = ik.classify(text)
        check(f"classify({text!r}) -> {want}", got == want, f"got {got!r}")
    check("classify(unknown) -> None", ik.classify("a generic blobby thing") is None)


# --------------------------------------------------------------------------- 3. ledger / gate


def test_build_ledger_seeds_unknown() -> None:
    led = ik.build_ledger(gearmotor_part(), "fdm")
    keys = {r.key for r in led.rows}
    check("ledger seeds one row per critical_key",
          keys == {"output_shaft.dia", "mount_holes.spacing", "mount_holes.dia",
                   "body_pocket.x", "body_pocket.y"}, repr(sorted(keys)))
    check("every seeded row starts status=unknown",
          all(r.status == "unknown" for r in led.rows))
    check("gate is NOT ok with unknowns", ik.gate_ok(led) is False)
    check("unresolved_critical lists all five", len(ik.unresolved_critical(led)) == 5)


def test_apply_confirmation_and_gate() -> None:
    led = ik.build_ledger(gearmotor_part(), "fdm")
    for key, val, st in [("output_shaft.dia", 3.00, "measured"),
                         ("mount_holes.spacing", 18.0, "measured"),
                         ("mount_holes.dia", 2.2, "measured"),
                         ("body_pocket.x", 24.1, "given"),
                         ("body_pocket.y", 31.0, "given")]:
        row = ik.apply_confirmation(led, key, val, status=st)
        check(f"apply_confirmation sets {key} -> {st} + value",
              row.status == st and approx(row.nominal_mm, val) and row.resolved_by == "user_confirmed")
    check("gate clears once every critical resolved", ik.gate_ok(led) is True)
    check("unresolved_critical empty after resolution", ik.unresolved_critical(led) == [])


def test_apply_confirmation_missing_key_raises() -> None:
    led = ik.build_ledger(gearmotor_part(), "fdm")
    try:
        ik.apply_confirmation(led, "no_such.key", 1.0)
        check("apply_confirmation(missing) raises KeyError", False, "no exception")
    except KeyError:
        check("apply_confirmation(missing key) raises KeyError", True)


def test_assumptions_view() -> None:
    led = ik.build_ledger(gearmotor_part(), "fdm")
    ik.apply_confirmation(led, "body_pocket.x", 80.0, resolved_by="assumed", status="assumed")
    a = ik.assumptions(led)
    check("assumptions() surfaces an assumed-resolved row",
          any("body_pocket.x" in s for s in a), repr(a))


# --------------------------------------------------------------------------- 4. scorer / demand


def test_score_claim_tiers() -> None:
    # typed caliper -> measured, full stop
    m = ik.score_claim({"caliper_readout": 3.00})
    check("typed caliper -> measured (uncert 0.05)",
          m["tier"] == "measured" and approx(m["uncert_mm"], 0.05), repr(m))
    # card + in-plane + cross-view agreement + sharp edges -> high
    hi = ik.score_claim({"scale_source": "card", "in_plane": True,
                         "agrees_across_views": True, "edge_quality": "sharp"})
    check("card+2views+sharp -> high", hi["tier"] == "high", repr(hi))
    # single soft-edge coin shot, out of plane -> low (the shaft case in §1 step 7)
    lo = ik.score_claim({"scale_source": "coin", "in_plane": False, "edge_quality": "soft"})
    check("coin + out-of-plane + soft -> low", lo["tier"] == "low", repr(lo))
    # ruler in-plane, no cross-view -> medium (the hole-spacing case)
    md = ik.score_claim({"scale_source": "ruler", "in_plane": True})
    check("ruler in-plane single-view -> medium", md["tier"] == "medium", repr(md))
    check("score uses explicit uncert_mm when present",
          approx(ik.score_claim({"scale_source": "card", "in_plane": True,
                                 "agrees_across_views": True, "uncert_mm": 0.12})["uncert_mm"], 0.12))


def test_required_tier_scaling() -> None:
    check("press fit demands measured", ik.required_tier("press", "fdm") == "measured")
    check("slip fit demands high", ik.required_tier("slip", "fdm") == "high")
    check("clearance fit demands medium", ik.required_tier("clearance", "fdm") == "medium")
    check("cosmetic demands medium", ik.required_tier("cosmetic", "fdm") == "medium")
    # photo dim feeding a tolerance-sensitive (press) fit -> one step down (press->location->high)
    check("press from photo downgrades one step (->high)",
          ik.required_tier("press", "fdm", from_photo=True) == "high")
    # a flat sheet process has no tight diametral fit -> nothing tighter than medium
    check("slip on sheet_metal can't demand more than medium",
          ik.required_tier("slip", "sheet_metal") == "medium")


def test_is_satisfied_ladder() -> None:
    check("measured satisfies high", ik.is_satisfied("measured", "high") is True)
    check("medium does NOT satisfy high", ik.is_satisfied("medium", "high") is False)
    check("high satisfies high", ik.is_satisfied("high", "high") is True)
    check("low does NOT satisfy measured", ik.is_satisfied("low", "measured") is False)


def test_demands_worked_example() -> None:
    part = gearmotor_part()
    led = ik.build_ledger(part, "fdm")
    demands = ik.compute_demands(part, led, "fdm")
    keys = {d["key"] for d in demands}
    # §1 step 8: shaft (slip, low) and hole-spacing (location, medium) BOTH demand a typed value
    check("shaft slip low-confidence raises a demand", "output_shaft.dia" in keys, repr(keys))
    check("hole-spacing medium raises a demand (required high)",
          "mount_holes.spacing" in keys, repr(keys))
    # the cosmetic-grade body pocket measured HIGH meets its medium requirement -> NO demand
    check("high-confidence pocket raises NO demand",
          "body_pocket.x" not in keys and "body_pocket.y" not in keys, repr(keys))
    # a measured-tier demand asks for calipers explicitly
    shaft_demand = next(d for d in demands if d["key"] == "output_shaft.dia")
    check("shaft demand asks for a typed value", "type" in shaft_demand["ask"].lower(), shaft_demand["ask"])
    check("demand carries required>achieved tiers",
          shaft_demand["required_tier"] == "high" and shaft_demand["achieved_tier"] == "low",
          repr(shaft_demand))


def test_cosmetic_dim_defaults_no_demand() -> None:
    # a lone cosmetic feature (a prism pocket measured high) produces NO demand and stays buildable
    pocket = ik.new_measured_feature("body_pocket", "prism_pocket",
                                     dims={"x_mm": 24.1, "y_mm": 31.0}, confidence="high")
    part = ik.new_measured_part("mp_c", "thing", features=[pocket])
    led = ik.build_ledger(part, "fdm")
    demands = ik.compute_demands(part, led, "fdm")
    check("cosmetic-grade high-confidence dim produces no demand", demands == [], repr(demands))


def test_demand_cleared_by_typed_value() -> None:
    part = gearmotor_part()
    led = ik.build_ledger(part, "fdm")
    ik.apply_confirmation(led, "output_shaft.dia", 3.00, status="measured")
    demands = ik.compute_demands(part, led, "fdm")
    check("a typed-value-resolved row no longer demands",
          "output_shaft.dia" not in {d["key"] for d in demands}, repr(demands))


# --------------------------------------------------------------------------- 5. photo request


def test_build_photo_request() -> None:
    part = gearmotor_part()
    req = ik.build_photo_request("gearmotor", part.features, "fdm")
    check("photo request has the documented keys",
          {"ask_text", "views", "scale_options", "request_id_seed"} <= set(req), repr(sorted(req)))
    check("scale options are card/caliper/ruler/coin",
          req["scale_options"] == ["card", "caliper", "ruler", "coin"], repr(req["scale_options"]))
    check("views include ortho top/front/side", {"top", "front", "side"} <= set(req["views"]))
    txt = req["ask_text"].lower()
    check("ask names a scale reference + same plane", "scale" in txt and "plane" in txt)
    check("ask demands calipers for the fit-critical features", "caliper" in txt, txt[:160])
    check("request_id_seed is a stable seed string", req["request_id_seed"].startswith("req_"))
    # determinism: same inputs -> same seed (pure, no randomness/IO)
    req2 = ik.build_photo_request("gearmotor", part.features, "fdm")
    check("photo request is deterministic", req["request_id_seed"] == req2["request_id_seed"])


# --------------------------------------------------------------------------- 6. fit report / coupon


def test_fit_report_ok_after_resolution() -> None:
    part = gearmotor_part()
    led = ik.build_ledger(part, "fdm")
    for key, val in [("output_shaft.dia", 3.00), ("mount_holes.spacing", 18.0),
                     ("mount_holes.dia", 2.2), ("body_pocket.x", 24.1), ("body_pocket.y", 31.0)]:
        ik.apply_confirmation(led, key, val, status="measured")
    fr = ik.build_fit_report(part, led, "fdm")
    check("FitReport overall=pass after all criticals resolved", fr.overall == "pass", repr(fr.overall))
    shaft = next(c for c in fr.checks if c["key"] == "output_shaft.dia")
    # §1 step 9: shaft passthrough modeled = 3.00 + FDM slip 0.40 (+ printed-hole comp 0.20)
    check("shaft passthrough stacks FDM slip clearance onto nominal",
          shaft["source_mm"] == 3.00 and shaft["modeled_mm"] > 3.0 and shaft["verdict"] == "ok",
          repr(shaft))
    check("FitReport lists a check per critical", len(fr.checks) == 5, repr(len(fr.checks)))
    check("FitReport records the process assumption",
          any("FDM" in a for a in fr.assumptions), repr(fr.assumptions))


def test_fit_report_at_risk_when_unresolved() -> None:
    part = gearmotor_part()
    led = ik.build_ledger(part, "fdm")
    fr = ik.build_fit_report(part, led, "fdm")  # nothing confirmed
    check("FitReport overall=at_risk while a critical is soft/unmeasured",
          fr.overall == "at_risk", repr(fr.overall))
    verdicts = {c["verdict"] for c in fr.checks}
    check("at-risk report carries a non-ok verdict",
          verdicts & {"risky", "unmeasured", "tight"}, repr(verdicts))


def test_coupon_spec_bore() -> None:
    shaft = ik.new_measured_feature("output_shaft", "shaft_in_bore", dims={"dia_mm": 3.0}, fit="slip")
    cs = ik.coupon_spec(shaft, "fdm")
    check("coupon_spec(bore) ok", cs["ok"] is True, repr(cs))
    ops = cs["ops"]
    check("coupon is a box + the interface op-list (no geometry)",
          [o["op"] for o in ops] == ["box", "hole"], repr([o.get("op") for o in ops]))
    hole = ops[1]
    # the test bore is stacked with FDM slip clearance + printed-hole comp (3.0 + 0.40 + 0.20)
    check("coupon bore is stacked to the modeled fit dim",
          approx(hole["dia_mm"], 3.60), repr(hole))
    check("coupon reports a printable estimate", cs["est_print_min"] and cs["est_mass_g"])


def test_coupon_spec_unsupported_archetype() -> None:
    lid = ik.new_measured_feature("lid", "lid_mate", dims={"dia_mm": 40.0})
    cs = ik.coupon_spec(lid, "fdm")
    check("coupon_spec refuses an out-of-scope archetype", cs["ok"] is False, repr(cs))


# --------------------------------------------------------------------------- 7. never-block gate


def test_gate_never_hard_blocks_by_default() -> None:
    led = ik.build_ledger(gearmotor_part(), "fdm")  # everything still unknown
    gd = ik.gate_decision(led)  # default: not strict, no impossible material
    check("default gate does NOT hard-block", gd["block"] is False, repr(gd))
    check("default gate surfaces at least one ask", len(gd["ask"]) >= 1, repr(gd["ask"]))
    check("default gate emits an at_risk warning", len(gd["warnings"]) >= 1, repr(gd["warnings"]))
    check("default gate has no must-block reasons", gd["must_block_reasons"] == [])


def test_gate_must_block_metal_on_fdm() -> None:
    led = ik.build_ledger(gearmotor_part(), "fdm")
    gd = ik.gate_decision(led, material="steel", process="fdm")
    check("metal material on an FDM process MUST block",
          gd["block"] is True and gd["must_block_reasons"], repr(gd))


def test_gate_strict_fit_blocks_unmeasured_bore() -> None:
    # strict-fit + an unmeasured slip/press bore is the opt-in hard guarantee
    shaft = ik.new_measured_feature("output_shaft", "shaft_in_bore", dims={}, fit="press")
    led = ik.build_ledger(ik.new_measured_part("mp", "x", features=[shaft]), "fdm")
    soft = ik.gate_decision(led, strict_fit=False)
    strict = ik.gate_decision(led, strict_fit=True)
    check("non-strict gate does not block the unmeasured press bore", soft["block"] is False)
    check("strict-fit gate DOES block an unmeasured press bore",
          strict["block"] is True and any("strict-fit" in r for r in strict["must_block_reasons"]),
          repr(strict["must_block_reasons"]))


def test_package_reexports_public_api() -> None:
    """``from engine.intake import classify`` works at package level (not only submodule)."""
    from engine.intake import classify as pkg_classify, worst_tier
    check("package-level classify matches submodule",
          pkg_classify("output shaft") == ik.classify("output shaft"), repr(pkg_classify))
    check("worst_tier picks the weakest tier",
          worst_tier(["high", "low", "medium"]) == "low")


def test_imports_shared_process_math() -> None:
    # parity: the fit numbers come from the SAME process_profiles the planner uses
    m = pp.modeled_dim(3.00, "slip", "fdm", is_hole=True)
    check("shared process math: FDM slip bore 3.00 -> 3.60",
          approx(m["modeled_mm"], 3.60), repr(m))


def main() -> int:
    test_schema_roundtrip()
    test_new_factories_present()
    test_archetype_table_all_critical()
    test_classify_plain_words()
    test_build_ledger_seeds_unknown()
    test_apply_confirmation_and_gate()
    test_apply_confirmation_missing_key_raises()
    test_assumptions_view()
    test_score_claim_tiers()
    test_required_tier_scaling()
    test_is_satisfied_ladder()
    test_demands_worked_example()
    test_cosmetic_dim_defaults_no_demand()
    test_demand_cleared_by_typed_value()
    test_build_photo_request()
    test_fit_report_ok_after_resolution()
    test_fit_report_at_risk_when_unresolved()
    test_coupon_spec_bore()
    test_coupon_spec_unsupported_archetype()
    test_gate_never_hard_blocks_by_default()
    test_gate_must_block_metal_on_fdm()
    test_gate_strict_fit_blocks_unmeasured_bore()
    test_package_reexports_public_api()
    test_imports_shared_process_math()

    total = _PASSES + len(_FAILS)
    print("-" * 60)
    if _FAILS:
        print(f"FAILED {len(_FAILS)}/{total} checks: {', '.join(_FAILS)}")
        return 1
    print(f"INTAKE BRAIN TESTS PASSED ({_PASSES}/{total} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
