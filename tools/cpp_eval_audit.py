"""cpp_eval_audit.py — ADVERSARIAL audit of the C++ doc-engine Evaluate suite vs the oracle.

tools/cpp_eval_parity.py (40 curated cases, green) covers the happy path; this hunts the
divergences it MISSES by throwing HARDER, DEGENERATE, and DIVERSE geometry at BOTH engines
and comparing. A DIFF here is a SUCCESS — a real bug the curated harness never exercised.

Same shape as the sibling audits (cpp_derived_audit / cpp_mate_audit): build adversarial
cases, run the SAME op batch through Python (engine.doc.session + engine.doc.evaluate, the
ORACLE) and C++ (libaicad.dll eval_*), diff with the parity comparison helpers, print
MATCH/DIFF, exit nonzero on divergence.

Adversarial dimensions covered (beyond the parity baseline):
  * MASS      : thin plate, micro (sub-mm) + macro (metre) scale, heterogeneous density sweep
                incl. extreme densities, NON-axis-aligned (rotated-rectangle) prism (off-diagonal
                inertia + rotated principal axes), boolean CUT (through hole), boolean FUSE
                (merged overlap), hollow SHELL (internal void), multi-body w/ explicit body id.
  * SECTION   : off-centre + cylinder/diamond bodies (wire-centroid parity — the analytic path is
                degenerate on BOTH by design); plane through a bounding face; bogus-plane rejection.
  * SYMMETRY  : rotated prism, cut/hollow bodies, cylinder, asymmetric offset hole; bogus plane.
  * INTERFER. : TANGENT (touching, not overlapping) bodies, 3- and 4-body parts, full CONTAINMENT,
                small-overlap, all-clear fan-out.
  * MEASURE   : curved faces (cylinder wall, whole sphere), diamond-prism face area + face-face
                angle (non-90), face of a cut body.
  * FIELDS    : draft on a filleted box (curved faces preserve the {-90,0,90} plateau set),
                curvature on a FILLET (constant 1/r) + sphere, thickness on a hollow shell
                (wall band), a long bar, and a cylinder (looser, tessellation-derived bands —
                MIRRORING cpp_eval_parity's band logic; NOT loosened until green).

Run under the venv:
  & "$env:APPDATA\\AI-CAD\\runtime\\venv\\Scripts\\python.exe" \\
        tools\\cpp_eval_audit.py --dll core\\build-occt\\bin\\Release\\libaicad.dll

Exit code = number of divergences (0 = the C++ Evaluate suite matches the oracle everywhere
this audit could reach).
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402
from tools.cpp_eval_parity import (  # noqa: E402
    CppS, py_session,
    py_mass, py_check, py_section, py_symmetry, py_interference, py_measure,
    py_field_aicm, _read_aicm_field, _ref_for,
    compare_mass, compare_check, compare_section, compare_symmetry,
    compare_interference, compare_measure,
    compare_field_set, compare_field_physical, compare_field_physical_pair,
    rect_then_extrude, cyl_ops, sphere_ops, box_at,
)


# --------------------------------------------------------------------------- op builders
class B:
    """Tracks the op-index of each add_feature so sketch entities/region refs address the
    right sketch via ``$N`` (a $N placeholder = the id minted by the op at batch index N)."""

    def __init__(self):
        self.ops: list[dict] = []

    def _add(self, op):
        i = len(self.ops)
        self.ops.append(op)
        return i

    def sketch(self, plane="plane.top", name="S"):
        i = self._add({"op": "add_feature", "feature": {
            "type": "sketch", "name": name,
            "params": {"entities": [], "constraints": []}, "refs": {"plane": plane}}})
        return f"${i}"

    def feature(self, feat):
        i = self._add({"op": "add_feature", "feature": feat})
        return f"${i}"

    def line(self, sk, x1, y1, x2, y2, construction=False):
        self._add({"op": "sketch_add_entity", "sketch": sk,
                   "entity": {"type": "line", "construction": construction,
                              "p1": [x1, y1], "p2": [x2, y2]}})

    def circle(self, sk, cx, cy, r, construction=False):
        self._add({"op": "sketch_add_entity", "sketch": sk,
                   "entity": {"type": "circle", "construction": construction,
                              "center": [cx, cy], "radius": r}})

    def rect(self, sk, x0, y0, w, h):
        self.line(sk, x0, y0, x0 + w, y0)
        self.line(sk, x0 + w, y0, x0 + w, y0 + h)
        self.line(sk, x0 + w, y0 + h, x0, y0 + h)
        self.line(sk, x0, y0 + h, x0, y0)

    def poly(self, sk, pts):
        """Closed polygon from a vertex list."""
        n = len(pts)
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            self.line(sk, x1, y1, x2, y2)


def _region(sk, centroid, area, hint=0):
    return {"kind": "region", "sketch": sk, "centroid": list(centroid),
            "area": float(area), "hint_index": hint}


def _dim(v):
    return {"value": v, "expr": None}


def _extrude(b, sk, regions, depth, mode="boss", end="blind", merge=True, name="X"):
    params = {"mode": mode, "end": end, "depth": _dim(depth)}
    if not merge:
        params["merge"] = False
    return b.feature({"type": "extrude", "name": name, "params": params,
                      "refs": {"profile": regions}})


def _poly_area_centroid(pts):
    """Shoelace area + centroid of a simple polygon (CCW or CW; area returned positive)."""
    a = 0.0
    cx = 0.0
    cy = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        cr = x0 * y1 - x1 * y0
        a += cr
        cx += (x0 + x1) * cr
        cy += (y0 + y1) * cr
    a *= 0.5
    if abs(a) < 1e-12:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return 0.0, [sum(xs) / n, sum(ys) / n]
    cx /= (6.0 * a)
    cy /= (6.0 * a)
    return abs(a), [cx, cy]


# ---- composite geometry builders ------------------------------------------------------
def rotated_rect_prism(w, h, deg, depth):
    """A w×h rectangle CENTRED at origin, rotated `deg` in-plane, extruded `depth`. The rotation
    yields off-diagonal cross-section inertia and rotated principal axes (a real principal_axes
    parity probe). Distinct w≠h keeps the in-plane principal moments non-degenerate."""
    th = math.radians(deg)
    c, s = math.cos(th), math.sin(th)
    base = [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]
    pts = [(x * c - y * s, x * s + y * c) for (x, y) in base]
    area, cen = _poly_area_centroid(pts)
    b = B()
    sk = b.sketch()
    b.poly(sk, pts)
    _extrude(b, sk, [_region(sk, cen, area)], depth)
    return b.ops


def box_with_hole(w, h, depth, hx, hy, r):
    """A w×h×depth box (boss) minus a through cylinder of radius r at (hx,hy) — one solid with a
    bored hole (extra cylindrical face; an 'open void')."""
    b = B()
    s1 = b.sketch()
    b.rect(s1, 0, 0, w, h)
    _extrude(b, s1, [_region(s1, [w / 2, h / 2], w * h)], depth, name="Boss")
    s2 = b.sketch(name="Hole")
    b.circle(s2, hx, hy, r)
    _extrude(b, s2, [_region(s2, [hx, hy], math.pi * r * r)], depth,
             mode="cut", end="through_all", name="Cut")
    return b.ops


def fused_overlap(w, h, depth, dx):
    """Two w×h×depth boxes offset by dx in +X, FUSED (merge=True) into one solid (union)."""
    b = B()
    s1 = b.sketch()
    b.rect(s1, 0, 0, w, h)
    _extrude(b, s1, [_region(s1, [w / 2, h / 2], w * h)], depth, name="A")
    s2 = b.sketch(name="S2")
    b.rect(s2, dx, 0, w, h)
    _extrude(b, s2, [_region(s2, [dx + w / 2, h / 2], w * h)], depth, merge=True, name="Bx")
    return b.ops


def shelled_box(w, h, depth, t):
    """A w×h×depth box hollowed to wall thickness t with the top (+Z) face removed (an internal
    void). Uses a face selector for the +Z top face at z=depth."""
    b = B()
    s1 = b.sketch()
    b.rect(s1, 0, 0, w, h)
    _extrude(b, s1, [_region(s1, [w / 2, h / 2], w * h)], depth, name="Boss")
    top = {"kind": "face", "surf": "plane", "centroid": [w / 2, h / 2, depth],
           "axis": [0, 0, 1], "area_or_len": w * h, "owner_feature": None, "hint_index": 0}
    b.feature({"type": "shell", "name": "Sh", "params": {"thickness": _dim(t)},
               "refs": {"remove": [top]}})
    return b.ops


def filleted_box(w, h, depth, r):
    """A w×h×depth box with one vertical edge (at x=0,y=0) filleted radius r."""
    b = B()
    s1 = b.sketch()
    b.rect(s1, 0, 0, w, h)
    _extrude(b, s1, [_region(s1, [w / 2, h / 2], w * h)], depth, name="Boss")
    vedge = {"kind": "edge", "surf": "line", "centroid": [0.0, 0.0, depth / 2],
             "axis": [0.0, 0.0, 1.0], "area_or_len": float(depth),
             "owner_feature": None, "hint_index": 0}
    b.feature({"type": "fillet", "name": "F", "params": {"radius": _dim(r)},
               "refs": {"edges": [vedge]}})
    return b.ops


def surf_planar_ops(w, h):
    """A single planar FACE surface body (kind=surface) — exercises the mass_props is_surface
    AREA branch + the eval_check free-edge census (every boundary edge is free)."""
    b = B()
    s = b.sketch()
    b.rect(s, 0, 0, w, h)
    b.feature({"type": "surf_planar", "name": "SP", "params": {},
               "refs": {"profile": [_region(s, [w / 2, h / 2], w * h)]}})
    return b.ops


def surf_extrude_ops(w, h, depth):
    """An OPEN side-wall shell (no caps) — a surface body whose area = perimeter*depth and whose
    every boundary edge is free (a deeper exercise of the is_surface + free_edges branches)."""
    b = B()
    s = b.sketch()
    b.rect(s, 0, 0, w, h)
    b.feature({"type": "surf_extrude", "name": "SE",
               "params": {"end": "blind", "depth": _dim(depth)},
               "refs": {"profile": [_region(s, [w / 2, h / 2], w * h)]}})
    return b.ops


def n_body_ops(boxes):
    """boxes = [(x0,y0,w,h,depth), ...] each a SEPARATE body (merge=False). Returns the op batch
    with each block's $base wired correctly (every box_at block is 6 ops)."""
    ops = []
    for i, (x0, y0, w, h, depth) in enumerate(boxes):
        ops += box_at(x0, y0, w, h, depth, f"B{i + 1}", merge=False, base=6 * i)
    return ops


# --------------------------------------------------------------------------- harness
def build_both(core, ops):
    py = py_session()
    py.apply(0, ops)
    cpp = CppS(core)
    cpp.apply(0, ops)
    return py, cpp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", default=str(
        REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"))
    args = ap.parse_args()

    dll = Path(args.dll)
    if not dll.exists():
        print(f"[eval-audit] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[eval-audit] libaicad ABI={core.abi()}")

    total = 0
    mism = 0

    def record(label, diffs, note=""):
        nonlocal total, mism
        total += 1
        if diffs:
            mism += 1
        verdict = "MATCH" if not diffs else "DIFF: " + "; ".join(str(d) for d in diffs)
        suffix = f"  [{note}]" if note and not diffs else ""
        print(f"  {label:<40} {verdict}{suffix}")

    # ============================================================== eval_mass
    print("--- eval_mass (analytic; vol/mass 1e-6, com/inertia 1e-5) ---")

    def mass_case(label, ops, density=7.85, body=None):
        try:
            py, cpp = build_both(core, ops)
        except Exception as exc:  # noqa: BLE001 — a build crash is itself reportable
            record("eval_mass " + label, [f"build raised {type(exc).__name__}: {exc}"])
            return
        try:
            pres = py_mass(py, density, body)
        except Exception as exc:  # noqa: BLE001
            record("eval_mass " + label, [f"py raised {type(exc).__name__}: {exc}"])
            return
        cargs = {"doc_id": cpp.doc_id, "density_g_cm3": density}
        if body:
            cargs["body"] = body
        rc, cres = core.call("eval_mass", cargs)
        if rc != 0:
            record("eval_mass " + label, [f"cpp rc={rc} {str(cres)[:70]}"])
            return
        compare_mass.last_axes_note = ""
        diffs = compare_mass(pres, cres)
        record("eval_mass " + label, diffs, compare_mass.last_axes_note)

    mass_case("thin plate 40x30x0.1", rect_then_extrude(40, 30, 0.1))
    mass_case("micro box 0.5x0.4x0.3", rect_then_extrude(0.5, 0.4, 0.3))
    mass_case("macro box 4000x3000x1000", rect_then_extrude(4000, 3000, 1000))
    mass_case("density 1e-4 (near zero)", rect_then_extrude(40, 30, 10), density=1e-4)
    mass_case("density 21.45 (platinum)", rect_then_extrude(40, 30, 10), density=21.45)
    mass_case("density 0.0 (massless)", rect_then_extrude(40, 30, 10), density=0.0)
    mass_case("rotated rect prism 30x12 @25deg", rotated_rect_prism(30, 12, 25, 8))
    mass_case("rotated rect prism 30x12 @45deg", rotated_rect_prism(30, 12, 45, 8))
    mass_case("box w/ through hole r6", box_with_hole(40, 30, 20, 20, 15, 6))
    mass_case("fused overlap dx=20", fused_overlap(30, 20, 10, 20))
    mass_case("hollow shell t=2", shelled_box(40, 30, 20, 2))
    mass_case("filleted box r3", filleted_box(40, 30, 20, 3))
    # multi-body: select each body explicitly + the default (last).
    three = n_body_ops([(0, 0, 20, 20, 10), (40, 0, 10, 10, 30), (0, 40, 15, 25, 5)])
    mass_case("3-body select body=1", three, body=1)
    mass_case("3-body select body=2", three, body=2)
    mass_case("3-body select body=3 (last)", three, body=3)
    mass_case("3-body default (last)", three)
    # SURFACE bodies -> the is_surface AREA report branch (zero volume/mass, surface centroid).
    mass_case("surf_planar (area report)", surf_planar_ops(40, 30))
    mass_case("surf_extrude open shell (area)", surf_extrude_ops(40, 30, 10))

    # ============================================================== eval_section_props
    print("--- eval_section_props (degenerate analytic path; wire-centroid parity) ---")

    def section_case(label, ops, plane):
        try:
            py, cpp = build_both(core, ops)
            pres = py_section(py, plane)
        except Exception as exc:  # noqa: BLE001
            record("eval_section " + label, [f"py raised {type(exc).__name__}: {exc}"])
            return
        rc, cres = core.call("eval_section_props", {"doc_id": cpp.doc_id, "plane": plane})
        if rc != 0:
            record("eval_section " + label, [f"cpp rc={rc} {str(cres)[:70]}"])
            return
        record("eval_section " + label, compare_section(pres, cres))

    # An offset box: a section through plane.right (x=0) misses it entirely (box at x>=5) -> empty
    # section, centroid (0,0,0) on both. plane.front/top pass through it -> real wire centroid.
    offset = box_at(5, 0, 30, 20, 10, "Off")
    section_case("offset box plane.right (miss)", offset, "plane.right")
    section_case("offset box plane.front", offset, "plane.front")
    section_case("offset box plane.top (base face)", offset, "plane.top")
    section_case("cyl r10 plane.right", cyl_ops(10, 25), "plane.right")
    section_case("diamond prism plane.front", rotated_rect_prism(30, 12, 25, 8), "plane.front")
    section_case("box w/ hole plane.front", box_with_hole(40, 30, 20, 20, 15, 6), "plane.front")
    section_case("hollow shell plane.front", shelled_box(40, 30, 20, 2), "plane.front")

    # ============================================================== eval_symmetry
    print("--- eval_symmetry (symmetric-difference volume; flag + deviation 1e-5) ---")

    def symmetry_case(label, ops, plane, body=None):
        try:
            py, cpp = build_both(core, ops)
            pres = py_symmetry(py, plane, body)
        except Exception as exc:  # noqa: BLE001
            record("eval_symmetry " + label, [f"py raised {type(exc).__name__}: {exc}"])
            return
        cargs = {"doc_id": cpp.doc_id, "plane": plane}
        if body:
            cargs["body"] = body
        rc, cres = core.call("eval_symmetry", cargs)
        if rc != 0:
            record("eval_symmetry " + label, [f"cpp rc={rc} {str(cres)[:70]}"])
            return
        record("eval_symmetry " + label, compare_symmetry(pres, cres))

    symmetry_case("rotated prism @45 plane.front", rotated_rect_prism(20, 20, 45, 8), "plane.front")
    symmetry_case("rotated prism @25 plane.right", rotated_rect_prism(30, 12, 25, 8), "plane.right")
    symmetry_case("centred hole box plane.front", box_with_hole(40, 30, 20, 20, 15, 6), "plane.front")
    symmetry_case("offset hole box plane.front (asym)",
                  box_with_hole(40, 30, 20, 28, 15, 6), "plane.front")
    symmetry_case("hollow shell plane.front", shelled_box(40, 30, 20, 2), "plane.front")
    symmetry_case("hollow shell plane.right", shelled_box(40, 30, 20, 2), "plane.right")
    symmetry_case("cyl r10 plane.right", cyl_ops(10, 25), "plane.right")
    symmetry_case("sphere r12 plane.front", sphere_ops(12), "plane.front")
    symmetry_case("fused overlap plane.front", fused_overlap(30, 20, 10, 20), "plane.front")

    # ============================================================== eval_interference
    print("--- eval_interference (pairwise overlap/clearance, eps=1e-3) ---")

    def interference_case(label, ops):
        try:
            py, cpp = build_both(core, ops)
            pres = py_interference(py)
        except Exception as exc:  # noqa: BLE001
            record("eval_interference " + label, [f"py raised {type(exc).__name__}: {exc}"])
            return
        rc, cres = core.call("eval_interference", {"doc_id": cpp.doc_id})
        if rc != 0:
            record("eval_interference " + label, [f"cpp rc={rc} {str(cres)[:70]}"])
            return
        record("eval_interference " + label, compare_interference(pres, cres))

    # tangent: boxes 0-20 and 20-40 share the x=20 face (overlap vol ~0 -> clearance, distance 0).
    interference_case("2-body tangent (touch)", n_body_ops([(0, 0, 20, 20, 10), (20, 0, 20, 20, 10)]))
    # small overlap (1 mm): vol = 1*20*10 = 200 >> eps -> interference pair.
    interference_case("2-body small overlap 1mm",
                      n_body_ops([(0, 0, 20, 20, 10), (19, 0, 20, 20, 10)]))
    # 3 bodies: B1&B2 overlap, B3 clear of both.
    interference_case("3-body 1 overlap + 1 clear",
                      n_body_ops([(0, 0, 20, 20, 10), (15, 0, 20, 20, 10), (40, 0, 20, 20, 10)]))
    # 3 bodies all clear (3 clearance pairs).
    interference_case("3-body all clear",
                      n_body_ops([(0, 0, 20, 20, 10), (40, 0, 20, 20, 10), (0, 40, 20, 20, 10)]))
    # full containment: small box wholly inside a big box (overlap vol = small vol).
    interference_case("containment small-in-big",
                      n_body_ops([(0, 0, 40, 40, 40), (15, 15, 10, 10, 10)]))
    # 4 bodies: a chain of overlaps + one outlier.
    interference_case("4-body chain",
                      n_body_ops([(0, 0, 20, 20, 10), (15, 0, 20, 20, 10),
                                  (30, 0, 20, 20, 10), (80, 0, 20, 20, 10)]))

    # ============================================================== eval_measure
    print("--- eval_measure (curved/odd faces; refs resolved vs the LAST body) ---")

    def measure_case(label, ops, kinds_indices):
        try:
            py, cpp = build_both(core, ops)
            refs = [_ref_for(py, k, i) for (k, i) in kinds_indices]
        except Exception as exc:  # noqa: BLE001
            record("eval_measure " + label, [f"setup raised {type(exc).__name__}: {exc}"])
            return
        py_err = None
        try:
            pres = py_measure(py, refs)
        except Exception as exc:  # noqa: BLE001 — symmetric dangling ref -> oracle raises
            py_err = exc
        rc, cres = core.call("eval_measure", {"doc_id": cpp.doc_id, "refs": refs})
        if py_err is not None:
            ok = rc != 0  # oracle could not resolve -> C++ MUST also reject; same outcome = MATCH
            record("eval_measure " + label,
                   [] if ok else [f"py dangled but cpp rc={rc}"],
                   "both dangle" if ok else "")
            return
        if rc != 0:
            record("eval_measure " + label, [f"cpp rc={rc} {str(cres)[:70]}"])
            return
        record("eval_measure " + label, compare_measure(pres, cres))

    # Whole sphere = one face -> area = 4*pi*r^2 (index 0, trivially unambiguous).
    measure_case("sphere face area", sphere_ops(12), [("face", 0)])
    # Cylinder faces: walk a few indices (the side wall is geometrically distinct; caps are a
    # symmetric pair that may dangle — both engines must agree either way).
    measure_case("cyl face[0]", cyl_ops(10, 25), [("face", 0)])
    measure_case("cyl face[1]", cyl_ops(10, 25), [("face", 1)])
    measure_case("cyl face[2]", cyl_ops(10, 25), [("face", 2)])
    # Diamond prism: face areas + a non-90 face-face angle pair.
    measure_case("diamond prism face[0]", rotated_rect_prism(30, 12, 25, 8), [("face", 0)])
    measure_case("diamond prism faces 0+2", rotated_rect_prism(30, 12, 25, 8),
                 [("face", 0), ("face", 2)])
    # Cut body: an extra cylindrical face from the bore.
    measure_case("box-w-hole face[0]", box_with_hole(40, 30, 20, 20, 15, 6), [("face", 0)])
    # Sphere vs a face pair is degenerate (1 face); use a filleted box face.
    measure_case("filleted box face[0]", filleted_box(40, 30, 20, 3), [("face", 0)])

    # ============================================================== bogus-plane rejection parity
    print("--- bogus-plane rejection parity (section + symmetry) ---")

    def reject_case(label, method, cargs_extra, py_call):
        nonlocal total, mism
        total += 1
        py, cpp = build_both(core, rect_then_extrude(40, 30, 10))
        py_raised = False
        try:
            py_call(py)
        except Exception:  # noqa: BLE001 — non-reserved plane -> OpError on the oracle
            py_raised = True
        rc, _cres = core.call(method, {"doc_id": cpp.doc_id, **cargs_extra})
        ok = py_raised and rc != 0
        if not ok:
            mism += 1
        print(f"  {label:<40} "
              f"{'MATCH (both reject)' if ok else f'DIFF: py_raised={py_raised} cpp_rc={rc}'}")

    reject_case("section bogus plane id", "eval_section_props", {"plane": "plane.bogus"},
                lambda py: py_section(py, "plane.bogus"))
    reject_case("symmetry bogus datum sel", "eval_symmetry",
                {"plane": {"kind": "datum", "feature": "d_made_up"}},
                lambda py: py_symmetry(py, {"kind": "datum", "feature": "d_made_up"}))

    # ============================================================== eval_check
    print("--- eval_check (valid solids -> no issues on both) ---")

    def check_case(label, ops):
        try:
            py, cpp = build_both(core, ops)
            pres = py_check(py)
        except Exception as exc:  # noqa: BLE001
            record("eval_check " + label, [f"py raised {type(exc).__name__}: {exc}"])
            return
        rc, cres = core.call("eval_check", {"doc_id": cpp.doc_id})
        if rc != 0:
            record("eval_check " + label, [f"cpp rc={rc} {str(cres)[:70]}"])
            return
        record("eval_check " + label, compare_check(pres, cres))

    check_case("box w/ hole", box_with_hole(40, 30, 20, 20, 15, 6))
    check_case("hollow shell", shelled_box(40, 30, 20, 2))
    check_case("filleted box", filleted_box(40, 30, 20, 3))
    check_case("fused overlap", fused_overlap(30, 20, 10, 20))
    check_case("sphere", sphere_ops(12))
    check_case("thin plate", rect_then_extrude(40, 30, 0.1))
    # SURFACE bodies -> the NON-empty issue path (free-edge census): the classification multiset
    # must match (this is the only place the audit drives eval_check off the empty-issues path).
    check_case("surf_planar (free edges)", surf_planar_ops(40, 30))
    check_case("surf_extrude (free edges)", surf_extrude_ops(40, 30, 10))

    # ============================================================== FIELD methods
    # Tessellation-derived: compare the SCLR field STATISTICALLY (distinct-value set + range, or a
    # physical band). Bands MIRROR cpp_eval_parity — NOT loosened until green.
    print("--- field methods (draft/curvature/thickness; tessellation-band parity) ---")

    def field_case(label, ops, method, params, cmp_fn, pair_phys=False):
        try:
            py, cpp = build_both(core, ops)
            ppath = py_field_aicm(py, method, params)
            psc, _pb = _read_aicm_field(ppath)
        except Exception as exc:  # noqa: BLE001
            record(method + " " + label, [f"py raised {type(exc).__name__}: {exc}"])
            return
        rc, cres = core.call(method, {"doc_id": cpp.doc_id, **params})
        if rc != 0:
            record(method + " " + label, [f"cpp rc={rc} {str(cres)[:70]}"])
            return
        try:
            csc, _cb = _read_aicm_field(cres["aicm_path"])
        except Exception as exc:  # noqa: BLE001
            record(method + " " + label, [f"cpp aicm unreadable: {exc}"])
            return
        res = cmp_fn(psc, csc)
        note = ""
        if isinstance(res, tuple):  # compare_field_physical -> (diffs, note)
            diffs, note = res
        else:
            diffs = res
        if not diffs and pair_phys:
            diffs = compare_field_physical_pair(psc, csc)
        record(method + " " + label, diffs, note)

    # draft on a filleted box: the fillet face (vertical) keeps draft 0, so the plateau set stays
    # {-90, 0, +90} on both meshes.
    field_case("filleted box pull+Z", filleted_box(40, 30, 20, 3), "eval_draft",
               {"pull": [0, 0, 1]}, lambda p, c: compare_field_set(p, c, set_digits=0, set_tol=1.0))
    # draft on a box w/ hole (cylindrical bore wall ⟂ Z -> draft 0): set still {-90,0,90}.
    field_case("box-w-hole pull+Z", box_with_hole(40, 30, 20, 20, 15, 6), "eval_draft",
               {"pull": [0, 0, 1]}, lambda p, c: compare_field_set(p, c, set_digits=0, set_tol=1.0))

    # curvature on a fillet (radius 3 -> constant max curvature 1/3 ≈ 0.333 on the fillet face;
    # box faces 0). Distinct-value set {0, 0.333}.
    field_case("filleted box max curv", filleted_box(40, 30, 20, 3), "eval_curvature",
               {"kind": "max"}, lambda p, c: compare_field_set(p, c, set_digits=2, set_tol=2e-2))
    field_case("filleted box mean curv", filleted_box(40, 30, 20, 3), "eval_curvature",
               {"kind": "mean"}, lambda p, c: compare_field_set(p, c, set_digits=2, set_tol=2e-2))
    # sphere r8 (uniform mean = 1/r = 0.125, gaussian = 1/r^2 = 0.0156).
    field_case("sphere r8 mean curv", sphere_ops(8), "eval_curvature",
               {"kind": "mean"}, lambda p, c: compare_field_set(p, c, set_digits=3, set_tol=5e-3))

    # thickness: hollow shell wall = t (2 mm); long bar -> min cross (10); cylinder -> diameter 20.
    field_case("hollow shell t=2 (~2mm)", shelled_box(40, 30, 20, 2), "eval_thickness", {},
               lambda p, c: compare_field_physical(c, 2.0), pair_phys=True)
    field_case("bar 50x10x10 (~10mm)", rect_then_extrude(50, 10, 10), "eval_thickness", {},
               lambda p, c: compare_field_physical(c, 10.0), pair_phys=True)
    # A tall cylinder has TWO competing thickness plateaus — side wall (2r=20) and caps (h=25) —
    # nearly tied in vertex count, so an absolute single-value expectation does not apply; the
    # faithful gate is cross-engine modal agreement (compare_field_physical_pair).
    field_case("cyl r10 h25 (py==cpp)", cyl_ops(10, 25), "eval_thickness", {},
               lambda p, c: compare_field_physical_pair(p, c))

    print(f"\n[eval-audit] {total - mism}/{total} MATCH ; {mism} divergence(s)")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())
