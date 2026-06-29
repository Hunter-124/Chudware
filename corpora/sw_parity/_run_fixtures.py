"""Runner: load every corpus_sw_parity/*.json fixture, call solve_sketch,
compare against expect block and geometric checks, print a NOTES-style report.

Run from repo root:
    & "$env:APPDATA\\AI-CAD\\runtime\\venv\\Scripts\\python.exe" corpus_sw_parity/_run_fixtures.py
"""
from __future__ import annotations
import json
import math
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.doc.sketch_solver import solve_sketch

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)))

results = []   # list of (name, agree_or_diverge, notes)


def run_fixture(path: str):
    with open(path) as f:
        fx = json.load(f)
    name = fx["name"]
    sketch = fx["sketch"]
    env = fx.get("env", {})
    expect = fx.get("expect", {})
    sw_rationale = fx.get("sw_rationale", "")

    try:
        sol = solve_sketch(sketch, env, warm=None)
    except Exception as e:
        results.append({
            "name": name,
            "verdict": "ERROR",
            "error": str(e),
            "tb": traceback.format_exc(),
            "sw_rationale": sw_rationale,
        })
        return

    actual = {
        "converged": sol.converged,
        "dof": sol.dof,
        "statuses": sol.statuses,
        "redundant": sol.redundant,
        "conflicting": sol.conflicting,
        "entities": sol.entities,
    }

    divergences = []

    # --- Check converged ---
    if "converged" in expect:
        exp_c = expect["converged"]
        got_c = sol.converged
        if exp_c != got_c:
            divergences.append(f"converged: expected={exp_c}, got={got_c}")

    # --- Check dof ---
    if "dof" in expect:
        exp_d = expect["dof"]
        got_d = sol.dof
        if exp_d != got_d:
            divergences.append(f"dof: expected={exp_d}, got={got_d}")

    # --- Check statuses ---
    if "statuses" in expect:
        for eid, exp_st in expect["statuses"].items():
            got_st = sol.statuses.get(eid, "MISSING")
            if exp_st != got_st:
                divergences.append(f"status[{eid}]: expected={exp_st!r}, got={got_st!r}")

    # --- Check redundant ---
    if "redundant" in expect:
        exp_r = set(expect["redundant"])
        got_r = set(sol.redundant)
        # Special token "c10_or_c12" in F5c
        if exp_r == {"c10_or_c12"}:
            if not (("c10" in got_r) or ("c12" in got_r)):
                divergences.append(f"redundant: expected c10 or c12 in redundant, got={sol.redundant}")
        elif exp_r != got_r:
            divergences.append(f"redundant: expected={sorted(exp_r)}, got={sorted(got_r)}")

    # --- Check conflicting ---
    if "conflicting" in expect:
        exp_cf = expect["conflicting"]
        got_cf = sol.conflicting
        if exp_cf == [] and got_cf:
            divergences.append(f"conflicting: expected=[], got={got_cf}")
        elif exp_cf and not got_cf:
            divergences.append(f"conflicting: expected non-empty, got=[]")

    # --- Per-fixture geometric post-checks ---
    post_checks = _post_checks(name, sol)
    divergences.extend(post_checks)

    verdict = "AGREE" if not divergences else "DIVERGE"
    results.append({
        "name": name,
        "verdict": verdict,
        "divergences": divergences,
        "actual": {
            "converged": sol.converged,
            "dof": sol.dof,
            "statuses": sol.statuses,
            "redundant": sol.redundant,
            "conflicting": sol.conflicting,
        },
        "sw_rationale": sw_rationale,
        "entities": sol.entities,
    })


def _post_checks(name: str, sol) -> list:
    """Fixture-specific geometric post-checks beyond the expect block."""
    issues = []
    ents = sol.entities

    if name == "F1a_triangle_apex_above":
        if "p3" in ents:
            py = ents["p3"]["p"][1]
            if py <= 0:
                issues.append(f"F1a branch flip: apex.y={py:.4f} should be >0 (above base)")
            else:
                h_expected = math.sqrt(40**2 - 30**2)  # ~26.458
                if abs(py - h_expected) > 0.1:
                    issues.append(f"F1a apex y={py:.4f}, expected ~{h_expected:.4f}")

    elif name == "F1b_triangle_apex_below":
        if "p3" in ents:
            py = ents["p3"]["p"][1]
            if py >= 0:
                issues.append(f"F1b branch flip: apex.y={py:.4f} should be <0 (below base)")
            else:
                h_expected = -math.sqrt(40**2 - 30**2)
                if abs(py - h_expected) > 0.1:
                    issues.append(f"F1b apex y={py:.4f}, expected ~{h_expected:.4f}")

    elif name == "F2b_tangent_sign_above":
        if "e2" in ents:
            cy = ents["e2"]["center"][1]
            if cy <= 0:
                issues.append(f"F2b tangent sign flip: circle center.y={cy:.4f} should be >0")
            elif abs(cy - 10.0) > 0.01:
                issues.append(f"F2b circle center.y={cy:.4f}, expected 10.0")

    elif name == "F2c_tangent_sign_below":
        if "e2" in ents:
            cy = ents["e2"]["center"][1]
            if cy >= 0:
                issues.append(f"F2c tangent sign flip: circle center.y={cy:.4f} should be <0")
            elif abs(cy + 10.0) > 0.01:
                issues.append(f"F2c circle center.y={cy:.4f}, expected -10.0")

    elif name == "F3a_arc_ccw_preserved":
        if "e1" in ents:
            a0 = ents["e1"]["start"]
            a1 = ents["e1"]["end"]
            # Check CCW sweep 30->150 (120 degrees)
            sweep = (a1 - a0) % 360
            if sweep < 0 or sweep > 180:
                issues.append(f"F3a arc winding reversed: start={a0:.2f}, end={a1:.2f}, sweep={sweep:.2f}")
            # Check start is near 30, end near 150
            if abs((a0 % 360) - 30.0) > 1.0:
                issues.append(f"F3a arc start angle {a0:.2f} != 30 deg")
            if abs((a1 % 360) - 150.0) > 1.0:
                issues.append(f"F3a arc end angle {a1:.2f} != 150 deg")

    elif name == "F3b_arc_large_sweep_radius_driven":
        if "e1" in ents:
            r = ents["e1"]["radius"]
            a0 = ents["e1"]["start"]
            a1 = ents["e1"]["end"]
            if abs(r - 25.0) > 0.01:
                issues.append(f"F3b radius={r:.4f}, expected 25")
            # angles should be unchanged
            if abs((a0 % 360) - 200.0) > 1.0:
                issues.append(f"F3b start angle changed: {a0:.2f} != 200")
            if abs((a1 % 360) - 340.0) > 1.0:
                issues.append(f"F3b end angle changed: {a1:.2f} != 340")

    elif name == "F3c_arc_endpoint_chain_no_reverse":
        if "e1" in ents:
            a0 = ents["e1"]["start"]
            a1 = ents["e1"]["end"]
            # start should be near 0 (mod 360), end near 90
            if abs((a0 % 360)) > 5.0 and abs((a0 % 360) - 360) > 5.0:
                issues.append(f"F3c arc start {a0:.2f} not near 0 deg")
            if abs((a1 % 360) - 90.0) > 5.0:
                issues.append(f"F3c arc end {a1:.2f} not near 90 deg")
            # Verify no swap: start endpoint should be at ~(10,0), not (0,10)
            cx, cy = ents["e1"]["center"]
            r = ents["e1"]["radius"]
            sx = cx + r * math.cos(math.radians(a0))
            sy = cy + r * math.sin(math.radians(a0))
            if abs(sx - 10.0) > 0.01 or abs(sy) > 0.01:
                issues.append(f"F3c arc start point ({sx:.3f},{sy:.3f}) not at (10,0) - possible winding swap")

    elif name == "F4b_near_zero_length_line":
        if "e1" in ents:
            p1 = ents["e1"]["p1"]
            p2 = ents["e1"]["p2"]
            L = math.hypot(p2[0]-p1[0], p2[1]-p1[1])
            if abs(L - 0.01) > 1e-5:
                issues.append(f"F4b line length={L:.8f}, expected 0.01")
            if math.isnan(L) or math.isinf(L):
                issues.append("F4b NaN/Inf in line coordinates")

    elif name == "F4d_circle_radius_near_zero":
        if "e1" in ents:
            r = ents["e1"]["radius"]
            if math.isnan(r) or math.isinf(r):
                issues.append("F4d NaN/Inf radius")
            elif abs(r - 0.001) > 1e-6:
                issues.append(f"F4d radius={r:.8f}, expected 0.001")

    elif name == "F4e_coincident_circle_centers_tangent":
        if "e1" in ents and "e2" in ents:
            c0 = ents["e1"]["center"]
            c1 = ents["e2"]["center"]
            r0 = ents["e1"]["radius"]
            r1 = ents["e2"]["radius"]
            d = math.hypot(c0[0]-c1[0], c0[1]-c1[1])
            # internal tangency: d should equal |r0-r1| = 5
            if not math.isnan(d) and not math.isinf(d):
                expected_d = abs(r0 - r1)
                if abs(d - expected_d) > 0.1:
                    issues.append(f"F4e center distance={d:.4f}, expected {expected_d:.4f} (internal tangent)")
            if math.isnan(d) or math.isinf(d):
                issues.append("F4e NaN/Inf in circle center coords - degenerate Jacobian bug")

    elif name == "F5b_remove_dim_under":
        if sol.dof != 1:
            pass  # already caught in expect check
        else:
            under_count = sum(1 for s in sol.statuses.values() if s == "under")
            if under_count == 0:
                issues.append("F5b: dof=1 but no entity marked 'under'")

    elif name == "F5c_duplicate_dim_redundant":
        if not sol.redundant:
            issues.append("F5c: no redundant constraints detected for duplicate dim")

    elif name == "F5d_contradictory_dim_conflicting":
        if not sol.conflicting:
            issues.append("F5d: no conflicting constraints detected for contradictory dims")

    elif name == "F6_complex_multientity":
        # Verify the fillet arc is tangent to both adjacent lines
        if "e2" in ents and "e1" in ents and "e3" in ents:
            arc = ents["e2"]
            cx, cy = arc["center"]
            r = arc["radius"]
            # e1 is horizontal at y=0: distance from arc center to line y=0 should = r
            dist_to_e1 = abs(cy)
            if abs(dist_to_e1 - r) > 0.5:
                issues.append(f"F6 arc not tangent to e1: dist={dist_to_e1:.3f}, r={r:.3f}")
            # e3 is vertical: its p1.x = arc center.x + r
            e3 = ents["e3"]
            dist_to_e3 = abs(e3["p1"][0] - cx)
            if abs(dist_to_e3 - r) > 0.5:
                issues.append(f"F6 arc not tangent to e3: dist_to_vertical={dist_to_e3:.3f}, r={r:.3f}")
        # Verify rectangle dimensions
        if "e1" in ents:
            p1 = ents["e1"]["p1"]; p2_e1 = ents["e1"]["p2"]
            # bottom line + arc tangent point: full width = 100
            if "e3" in ents:
                e3 = ents["e3"]
                rect_w = abs(e3["p1"][0] - p1[0])
                if abs(rect_w - 100.0) > 1.0:
                    issues.append(f"F6 rectangle width {rect_w:.2f} != 100")

    return issues


def main():
    files = sorted([
        os.path.join(FIXTURE_DIR, f)
        for f in os.listdir(FIXTURE_DIR)
        if f.endswith(".json") and not f.startswith("_")
    ])

    print(f"Running {len(files)} fixtures...\n")
    for path in files:
        run_fixture(path)

    # Print results
    agree = [r for r in results if r["verdict"] == "AGREE"]
    diverge = [r for r in results if r["verdict"] == "DIVERGE"]
    errors = [r for r in results if r["verdict"] == "ERROR"]

    print("=" * 70)
    print(f"SUMMARY: {len(agree)} AGREE  |  {len(diverge)} DIVERGE  |  {len(errors)} ERROR")
    print("=" * 70)

    if agree:
        print("\n--- AGREE ---")
        for r in agree:
            a = r.get("actual", {})
            print(f"  [OK] {r['name']}")
            print(f"       converged={a.get('converged')} dof={a.get('dof')} "
                  f"redundant={a.get('redundant')} conflicting={a.get('conflicting')}")

    if diverge:
        print("\n--- DIVERGE (candidate bugs) ---")
        for r in diverge:
            a = r.get("actual", {})
            print(f"\n  [DIVERGE] {r['name']}")
            print(f"  actual: converged={a.get('converged')} dof={a.get('dof')} "
                  f"redundant={a.get('redundant')} conflicting={a.get('conflicting')}")
            print(f"  statuses: {a.get('statuses')}")
            for div in r.get("divergences", []):
                print(f"    !! {div}")

    if errors:
        print("\n--- ERRORS ---")
        for r in errors:
            print(f"\n  [ERROR] {r['name']}: {r['error']}")
            print(r.get("tb", ""))

    # Dump full entity coords for each fixture (for notes)
    print("\n\n--- ENTITY COORDS (for NOTES.md) ---")
    for r in results:
        print(f"\n[{r['name']}] verdict={r['verdict']}")
        if "entities" in r:
            for eid, edict in r["entities"].items():
                print(f"  {eid}: {edict}")

    return len(diverge) + len(errors)


if __name__ == "__main__":
    sys.exit(main())
