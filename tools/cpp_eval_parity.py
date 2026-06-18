"""cpp_eval_parity.py — Python-vs-C++ Evaluate-suite parity.

Gates the C++ doc-engine Evaluate methods (libaicad.dll) against the Python oracle
(engine/doc/evaluate.py via engine/worker.py's _eval_* handlers). Each part is built
through the SAME doc_* op batch on both engines, then the eval method is called on each
and the results compared.

Currently covers: eval_mass (mass_props).

eval_mass comparison rationale:
  * volume_mm3 / mass_g / com / inertia / principal_moments are deterministic outputs of
    OCCT BRepGProp + an eigenvalue solve, so they MUST match tightly (1e-6 relative). The
    Python oracle runs cadquery-ocp 7.8.1 and the C++ core OCCT 7.9.3, but the mass
    integrals of an identical analytic B-rep are version-independent.
  * principal_axes are eigenVECTORS — their SIGN is library-arbitrary, and when two
    principal moments are equal (e.g. a cylinder's two radial axes) the eigenspace is a
    plane and ANY orthonormal pair in it is valid. So each axis is checked up to a ± sign
    ONLY when its moment is non-degenerate; a degenerate axis is reported and skipped
    (still a correct equivalence — never a weakened assertion on the well-defined outputs).

Run under the venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" \
        tools\cpp_eval_parity.py --dll core\build-occt\bin\Release\libaicad.dll

Exit code = number of mismatched checks (0 = full parity).
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402


# --------------------------------------------------------------------------- ops
def _line(x1, y1, x2, y2):
    return {"op": "sketch_add_entity", "sketch": "$0",
            "entity": {"type": "line", "construction": False, "p1": [x1, y1], "p2": [x2, y2]}}


def rect_then_extrude(w, h, depth):
    return [
        {"op": "add_feature", "feature": {"type": "sketch", "name": "Sketch1",
                                          "params": {"entities": [], "constraints": []},
                                          "refs": {"plane": "plane.top"}}},
        _line(0, 0, w, 0), _line(w, 0, w, h), _line(w, h, 0, h), _line(0, h, 0, 0),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": "Boss1",
            "params": {"mode": "boss", "end": "blind", "depth": {"value": depth, "expr": None}},
            "refs": {"profile": [{"kind": "region", "sketch": "$0",
                                  "centroid": [w / 2, h / 2], "area": w * h, "hint_index": 0}]}}},
    ]


def cyl_ops(radius, height):
    return [{"op": "add_feature", "feature": {"type": "cylinder", "name": "Cyl1", "params": {
        "radius": {"value": radius, "expr": None}, "height": {"value": height, "expr": None}}}}]


# --------------------------------------------------------------------------- engines
def py_session():
    from engine.doc import session

    class PyS:
        def __init__(self):
            self.s = session.new_session("part")
            self.doc_id = self.s.doc_id

        def apply(self, base_rev, ops):
            return session.apply_ops(self.s, base_rev, ops, None)
    return PyS()


class CppS:
    def __init__(self, core):
        self.core = core
        rc, r = core.call("doc_new", {"kind": "part"})
        self.doc_id = r["doc_id"]

    def apply(self, base_rev, ops):
        rc, r = self.core.call("doc_apply_ops",
                               {"doc_id": self.doc_id, "base_revision": base_rev, "ops": ops})
        return r


# --------------------------------------------------------------------------- oracle
def py_mass(py, density, body=None):
    """Mirror engine/worker.py::_eval_mass: last (or body_id'th) body -> evaluate.mass_props."""
    from engine.doc import session as docsess
    import engine.doc.evaluate as ev
    result = docsess._ensure_built(py.s)
    bodies = list(result.bodies)
    shape = bodies[(body - 1) if body else -1].shape
    return ev.mass_props(shape, density)


# --------------------------------------------------------------------------- compare helpers
def _rel(a, b, tol=1e-6):
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def _vec_close(a, b, tol=1e-6):
    return all(_rel(x, y, tol) for x, y in zip(a, b))


def _axis_match_up_to_sign(c, o, tol=1e-5):
    same = max(abs(x - y) for x, y in zip(c, o))
    flip = max(abs(x + y) for x, y in zip(c, o))
    return min(same, flip) <= tol


def compare_mass(pres, cres):
    """Return a list of diff strings ([] == match)."""
    diffs = []
    # Surface vs solid branch must agree.
    if bool(pres.get("is_surface")) != bool(cres.get("is_surface")):
        return [f"is_surface {pres.get('is_surface')}!={cres.get('is_surface')}"]

    if pres.get("is_surface"):
        if not _rel(pres["area_mm2"], cres["area_mm2"]):
            diffs.append(f"area {pres['area_mm2']:.4f}!={cres['area_mm2']:.4f}")
        if not _vec_close(pres["com"], cres["com"], 1e-5):
            diffs.append(f"com {pres['com']}!={cres['com']}")
        return diffs

    if not _rel(pres["volume_mm3"], cres["volume_mm3"]):
        diffs.append(f"vol {pres['volume_mm3']:.4f}!={cres['volume_mm3']:.4f}")
    if not _rel(pres["mass_g"], cres["mass_g"]):
        diffs.append(f"mass {pres['mass_g']:.6f}!={cres['mass_g']:.6f}")
    if not _vec_close(pres["com"], cres["com"], 1e-5):
        diffs.append(f"com {pres['com']}!={cres['com']}")
    if not _vec_close(pres["inertia"], cres["inertia"], 1e-5):
        diffs.append("inertia tensor mismatch")
    pm_p, pm_c = pres["principal_moments"], cres["principal_moments"]
    if not _vec_close(pm_p, pm_c, 1e-5):
        diffs.append(f"principal_moments {pm_p}!={pm_c}")
    if pres.get("material", {}).get("density_g_cm3") != cres.get("material", {}).get("density_g_cm3"):
        diffs.append("density mismatch")

    # principal_axes: up-to-sign, but ONLY for non-degenerate moments (a repeated moment
    # spans a plane/space where any orthonormal basis is valid).
    if not diffs:
        n_axes_checked = 0
        for i in range(3):
            degenerate = any(j != i and abs(pm_p[i] - pm_p[j]) <= 1e-4 * max(1.0, abs(pm_p[i]))
                             for j in range(3))
            if degenerate:
                continue
            n_axes_checked += 1
            if not _axis_match_up_to_sign(cres["principal_axes"][i], pres["principal_axes"][i]):
                diffs.append(f"axis[{i}] {cres['principal_axes'][i]} !~ ±{pres['principal_axes'][i]}")
        compare_mass.last_axes_note = f"axes±:{n_axes_checked}/3"
    return diffs


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", default=str(REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"))
    args = ap.parse_args()

    dll = Path(args.dll)
    if not dll.exists():
        print(f"[eval-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[eval-parity] libaicad ABI={core.abi()}")

    mism = 0
    total = 0

    # eval_mass over solids with distinct geometry + a density sweep.
    cases = [
        ("box 40x30x10  d=7.85", rect_then_extrude(40, 30, 10), 7.85, None),
        ("box 40x30x10  d=2.70", rect_then_extrude(40, 30, 10), 2.70, None),  # density scaling
        ("box 40x30x10  body=1", rect_then_extrude(40, 30, 10), 7.85, 1),     # explicit body id
        ("cube 20       d=7.85", rect_then_extrude(20, 20, 20), 7.85, None),  # degenerate axes
        ("cyl r10 h25   d=7.85", cyl_ops(10, 25), 7.85, None),                # 2 degenerate axes
        ("box 50x20x8   d=7.85", rect_then_extrude(50, 20, 8), 7.85, None),   # distinct moments
    ]
    for label, ops, density, body in cases:
        total += 1
        py = py_session()
        py.apply(0, ops)
        cpp = CppS(core)
        cpp.apply(0, ops)

        try:
            pres = py_mass(py, density, body)
        except Exception as exc:  # noqa: BLE001
            print(f"  {('eval_mass ' + label):<28} DIFF: py raised {type(exc).__name__}: {exc}")
            mism += 1
            continue
        cargs = {"doc_id": cpp.doc_id, "density_g_cm3": density}
        if body:
            cargs["body"] = body
        rc, cres = core.call("eval_mass", cargs)
        if rc != 0:
            print(f"  {('eval_mass ' + label):<28} DIFF: cpp rc={rc} {str(cres)[:70]}")
            mism += 1
            continue

        compare_mass.last_axes_note = ""
        diffs = compare_mass(pres, cres)
        note = f"  [{compare_mass.last_axes_note}]" if not diffs and compare_mass.last_axes_note else ""
        verdict = "MATCH" if not diffs else "DIFF: " + "; ".join(diffs)
        print(f"  {('eval_mass ' + label):<28} {verdict}{note}")
        mism += 0 if not diffs else 1

    # bad body_id errors on both engines.
    total += 1
    py = py_session(); py.apply(0, rect_then_extrude(10, 10, 10))
    cpp = CppS(core); cpp.apply(0, rect_then_extrude(10, 10, 10))
    be_diffs = []
    try:
        py_mass(py, 7.85, body=9)
        be_diffs.append("py did not raise")
    except Exception:  # noqa: BLE001 — IndexError/OpError expected
        pass
    rc, cres = core.call("eval_mass", {"doc_id": cpp.doc_id, "density_g_cm3": 7.85, "body": 9})
    if rc == 0:
        be_diffs.append("cpp did not error")
    print(f"  {'eval_mass bad body_id':<28} {'MATCH' if not be_diffs else 'DIFF: ' + '; '.join(be_diffs)}")
    mism += 0 if not be_diffs else 1

    print(f"\n[eval-parity] {total - mism}/{total} checks at full parity")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())
