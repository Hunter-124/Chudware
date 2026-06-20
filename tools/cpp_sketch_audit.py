"""Adversarial audit of the 2D sketch constraint solver (py-vs-C++, fast sketch_cli path).

The corpus (solver_diff, 63 fixtures) is curated; this generates the COUPLED / dense /
chain-like topologies that stress the shared nsolve core the hardest — the same class
that exposed the mate-chain LM stall (fixed in solve_pos). Each network runs through both
engines (engine.doc.sketch_solver.solve_sketch and sketch_cli) and is compared:

  * structural ALWAYS: converged, dof, per-constraint status, redundant/conflicting sets
    (all gauge-invariant);
  * entity coordinates ONLY when fully-defined (dof==0) — an under-defined sketch has a
    free gauge so raw coords are non-unique (mirrors the mate-audit structural/pose split).

A DIFF is a SUCCESS (a real divergence the curated corpus missed).

    & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools/cpp_sketch_audit.py \
        --cli core/build/bin/Release/sketch_cli.exe
"""
import argparse
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.solver_diff import run_python, run_cpp, _discover_cli, _coord_diffs  # noqa: E402


def line(eid, x1, y1, x2, y2, construction=False):
    return {"id": eid, "type": "line", "construction": construction,
            "p1": [x1, y1], "p2": [x2, y2]}


def circle(eid, cx, cy, r, construction=False):
    return {"id": eid, "type": "circle", "construction": construction,
            "center": [cx, cy], "radius": r}


def arc(eid, cx, cy, r, start, end, ccw=True):
    return {"id": eid, "type": "arc", "construction": False,
            "center": [cx, cy], "radius": r, "start": start, "end": end, "ccw": ccw}


def con(ctype, refs, value=None):
    # anchor type is "fix" (not "fixed"); dimensional values are {value, expr} dicts.
    c = {"type": ctype, "refs": refs}
    if value is not None:
        c["value"] = {"value": float(value), "expr": None}
    return c


def fx(name, entities, constraints, env=None):
    # assign per-sketch constraint ids (c1, c2, ...) — the solver requires them.
    for i, c in enumerate(constraints, 1):
        c.setdefault("id", f"c{i}")
    return {"name": name, "sketch": {"entities": entities, "constraints": constraints},
            "env": env or {}}


# --------------------------------------------------------------------------- networks
def polyline_chain(n, seg=10.0):
    """n lines as an alternating-H/V staircase (coincident chain) — the 2D analog of the mate
    coincident chain, fully-defined. Geometry is initialized AT the staircase pose (as the real
    UI lays entities down where drawn), so we test convergence of the COUPLED system, not
    far-from-start basin selection (a bad-start LM lands engine-dependently — see note below)."""
    ents, cons = [], []
    x = y = 0.0
    for i in range(1, n + 1):
        nx, ny = (x + seg, y) if i % 2 else (x, y + seg)
        ents.append(line(f"e{i}", x, y, nx, ny))
        x, y = nx, ny
    for i in range(1, n):
        cons.append(con("coincident", [f"e{i}.p2", f"e{i+1}.p1"]))
    cons.append(con("fix", ["e1.p1"]))
    for i in range(1, n + 1):
        cons.append(con("horizontal" if i % 2 else "vertical", [f"e{i}"]))
        cons.append(con("distance", [f"e{i}.p1", f"e{i}.p2"], value=seg))
    return fx(f"polyline_chain_{n}", ents, cons)


def regular_polygon(n, r=20.0):
    """Closed n-gon: chain coincidences + all-equal-length + equal interior angle proxy via
    a fixed first edge. Coupled; left under-defined on rotation (structural compare only)."""
    ents = []
    for i in range(n):
        a0 = 2 * math.pi * i / n
        a1 = 2 * math.pi * (i + 1) / n
        ents.append(line(f"e{i+1}", r * math.cos(a0), r * math.sin(a0),
                         r * math.cos(a1), r * math.sin(a1)))
    cons = []
    for i in range(1, n + 1):
        nxt = i % n + 1
        cons.append(con("coincident", [f"e{i}.p2", f"e{nxt}.p1"]))
    for i in range(2, n + 1):
        cons.append(con("equal", [f"e1", f"e{i}"]))
    cons.append(con("fix", ["e1.p1"]))
    cons.append(con("horizontal", ["e1"]))
    cons.append(con("distance", ["e1.p1", "e1.p2"], value=15.0))
    return fx(f"regular_polygon_{n}", ents, cons)


def tangent_line_arc_chain():
    """line -> tangent arc -> line -> tangent arc (slot-ish open chain). Tangency at the
    solution is the linearization-degenerate case that historically broke."""
    ents = [
        line("e1", 0, 0, 40, 0),
        arc("e2", 40, 10, 10, -90, 90),
        line("e3", 40, 20, 0, 20),
        arc("e4", 0, 10, 10, 90, 270),
    ]
    cons = [
        con("coincident", ["e1.p2", "e2.p1"]),
        con("coincident", ["e2.p2", "e3.p1"]),
        con("coincident", ["e3.p2", "e4.p1"]),
        con("coincident", ["e4.p2", "e1.p1"]),
        con("tangent", ["e1", "e2"]), con("tangent", ["e2", "e3"]),
        con("tangent", ["e3", "e4"]), con("tangent", ["e4", "e1"]),
        con("horizontal", ["e1"]),
        con("fix", ["e1.p1"]),
        con("distance", ["e1.p1", "e1.p2"], value=40.0),
        con("equal", ["e2", "e4"]),
    ]
    return fx("tangent_line_arc_slot", ents, cons)


def dense_truss():
    """4 points fully cross-braced by distance constraints (rigid truss) — dense coupling,
    fully-defined up to placement gauge. Built as lines sharing endpoints."""
    ents = [
        line("e1", 0, 0, 30, 0),
        line("e2", 30, 0, 30, 30),
        line("e3", 30, 30, 0, 30),
        line("e4", 0, 30, 0, 0),
        line("e5", 0, 0, 30, 30),   # diagonal
        line("e6", 30, 0, 0, 30),   # diagonal
    ]
    cons = [
        con("coincident", ["e1.p2", "e2.p1"]),
        con("coincident", ["e2.p2", "e3.p1"]),
        con("coincident", ["e3.p2", "e4.p1"]),
        con("coincident", ["e4.p2", "e1.p1"]),
        con("coincident", ["e5.p1", "e1.p1"]), con("coincident", ["e5.p2", "e2.p2"]),
        con("coincident", ["e6.p1", "e2.p1"]), con("coincident", ["e6.p2", "e3.p2"]),
        con("fix", ["e1.p1"]), con("horizontal", ["e1"]),
        con("distance", ["e1.p1", "e1.p2"], value=30.0),
        con("distance", ["e2.p1", "e2.p2"], value=30.0),
        con("distance", ["e4.p1", "e4.p2"], value=30.0),
        con("perpendicular", ["e1", "e2"]),
    ]
    return fx("dense_truss_braced", ents, cons)


def redundant_rect():
    """Rectangle with H on top+bottom AND parallel(top,bottom): the parallel is REDUNDANT
    (no new DOF). Both engines must blame the SAME constraint, not over-define."""
    ents = [line("e1", 0, 0, 40, 0), line("e2", 40, 0, 40, 20),
            line("e3", 40, 20, 0, 20), line("e4", 0, 20, 0, 0)]
    cons = [
        con("coincident", ["e1.p2", "e2.p1"]), con("coincident", ["e2.p2", "e3.p1"]),
        con("coincident", ["e3.p2", "e4.p1"]), con("coincident", ["e4.p2", "e1.p1"]),
        con("horizontal", ["e1"]), con("horizontal", ["e3"]),
        con("vertical", ["e2"]), con("vertical", ["e4"]),
        con("parallel", ["e1", "e3"]),   # redundant given both horizontal
        con("fix", ["e1.p1"]),
        con("distance", ["e1.p1", "e1.p2"], value=40.0),
        con("distance", ["e2.p1", "e2.p2"], value=20.0),
    ]
    return fx("redundant_rect_parallel", ents, cons)


def conflicting_dims():
    """A line given two contradictory length dims -> over-defined/conflicting; both engines
    must classify the SAME blame."""
    ents = [line("e1", 0, 0, 30, 0)]
    cons = [con("fix", ["e1.p1"]), con("horizontal", ["e1"]),
            con("distance", ["e1.p1", "e1.p2"], value=30.0),
            con("distance", ["e1.p1", "e1.p2"], value=45.0)]
    return fx("conflicting_double_dim", ents, cons)


def large_scale_chain():
    """polyline chain at a large coordinate scale (x~1e4) — conditioning stress."""
    f = polyline_chain(5, seg=2000.0)
    f["name"] = "large_scale_chain"
    return f


def bolt_circle(n=6, r=30.0):
    """n small circles equally spaced on a bolt-circle: each concentric-distance from a fixed
    center hole + equal radius. Coupled radial web (left rotationally free -> structural cmp)."""
    ents = [circle("e0", 0, 0, 8.0)]  # center hole
    for i in range(n):
        a = 2 * math.pi * i / n
        ents.append(circle(f"e{i+1}", r * math.cos(a), r * math.sin(a), 3.0))
    cons = [con("fix", ["e0.center"]), con("radius", ["e0"], value=8.0)]
    for i in range(1, n + 1):
        cons.append(con("radius", [f"e{i}"], value=3.0))
        cons.append(con("distance", ["e0.center", f"e{i}.center"], value=r))
    return fx(f"bolt_circle_{n}", ents, cons)


def build_all():
    out = []
    for n in (3, 5, 8, 12):
        out.append(polyline_chain(n))
    for n in (5, 6, 8):
        out.append(regular_polygon(n))
    out += [tangent_line_arc_chain(), dense_truss(), redundant_rect(),
            conflicting_dims(), large_scale_chain(), bolt_circle(6), bolt_circle(8)]
    return out


def compare_audit(py, cpp, tol):
    """Structural parity always; coordinate parity only when fully-defined (dof==0)."""
    diffs = []
    if py["converged"] != cpp["converged"]:
        diffs.append(f"converged: py={py['converged']} cpp={cpp['converged']}")
    if py["dof"] != cpp["dof"]:
        diffs.append(f"dof: py={py['dof']} cpp={cpp['dof']}")
    if py.get("statuses") != cpp.get("statuses"):
        diffs.append(f"statuses differ py={py.get('statuses')} cpp={cpp.get('statuses')}")
    if set(py.get("redundant", [])) != set(cpp.get("redundant", [])):
        diffs.append(f"redundant py={sorted(set(py.get('redundant', [])))} "
                     f"cpp={sorted(set(cpp.get('redundant', [])))}")
    if set(py.get("conflicting", [])) != set(cpp.get("conflicting", [])):
        diffs.append(f"conflicting py={sorted(set(py.get('conflicting', [])))} "
                     f"cpp={sorted(set(cpp.get('conflicting', [])))}")
    # coords only meaningful when both fully-defined AND converged
    if py["dof"] == 0 and cpp["dof"] == 0 and py["converged"] and cpp["converged"]:
        diffs += _coord_diffs(py.get("entities", {}), cpp.get("entities", {}), tol)
    return diffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", default=None)
    ap.add_argument("--tol", type=float, default=1e-6)
    args = ap.parse_args()
    cli = Path(args.cli) if args.cli else _discover_cli()
    if not cli or not cli.exists():
        sys.stderr.write("sketch_cli not found (build core/build target sketch_cli)\n")
        return 2

    fixtures = build_all()
    print(f"[sketch-audit] {len(fixtures)} adversarial sketch(es) | cli={cli.name}")
    print("-" * 78)
    mism = 0
    for f in fixtures:
        name = f["name"]
        try:
            py = run_python(f)
            cpp = run_cpp(cli, f)
        except Exception as exc:  # noqa: BLE001
            print(f"DIFF  {name:<26} raised {type(exc).__name__}: {exc}")
            mism += 1
            continue
        diffs = compare_audit(py, cpp, args.tol)
        if diffs:
            print(f"DIFF  {name:<26} {'; '.join(diffs)[:96]}")
            mism += 1
        else:
            print(f"ok    {name:<26} dof={py['dof']} conv={py['converged']}")
    print("-" * 78)
    print(f"[sketch-audit] {len(fixtures) - mism}/{len(fixtures)} MATCH ; {mism} divergence(s)")
    return 1 if mism else 0


if __name__ == "__main__":
    raise SystemExit(main())
