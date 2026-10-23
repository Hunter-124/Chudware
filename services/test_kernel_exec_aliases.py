"""Kernel robustness: common build123d keyword typos should BUILD, not crash the script.

Regression for the "computer case" failure where one `Cylinder(r=...)` (instead of
`radius=`) at line 80 killed an otherwise-fine 82-line model across every iteration.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

# The executor was re-homed to services (see services/kernel_exec.py).
from services.kernel_exec import run_code


def _build(code: str, name: str = "alias_test"):
    with tempfile.TemporaryDirectory() as d:
        return run_code(code, Path(d), name)


def test_primitive_kwarg_aliases() -> None:
    cases = {
        "Cylinder r=/h=":     "result = Cylinder(r=4, h=10)",
        "Cylinder d=":        "result = Cylinder(d=8, height=10)",      # diameter -> radius 4
        "Box l=/w=/h=":       "result = Box(l=20, w=10, h=5)",
        "Sphere d=":          "result = Sphere(d=10)",                  # radius 5
        "RegularPolygon r/n": "result = extrude(RegularPolygon(r=6, n=6), amount=4)",
    }
    for label, code in cases.items():
        res = _build(code)
        assert res.ok, f"{label} should build, got: {res.error}"
        assert res.volume and res.volume > 0, f"{label} produced no volume"
    # diameter really halves: Sphere(d=10) == Sphere(radius=5)
    a = _build("result = Sphere(d=10)")
    b = _build("result = Sphere(radius=5)")
    assert abs(a.volume - b.volume) < 1e-6, "diameter alias must equal radius/2"
    print("[aliases] primitives build with r=/d=/h=/l=/w=/n= ; diameter halves correctly")


def test_mixed_radius_and_r_builds() -> None:
    """The exact failure shape: most calls use radius=, one slips to r=."""
    code = (
        "result = Box(40, 40, 40)\n"
        "result -= Cylinder(radius=3, height=40)\n"        # correct
        "result -= Pos(10, 10, 0) * Cylinder(r=2, height=40)\n"  # the typo that used to crash
    )
    res = _build(code)
    assert res.ok, f"mixed radius=/r= should build, got: {res.error}"
    print("[aliases] mixed radius=/r= script builds (was the computer-case crash)")


def test_fillet_chamfer_forgiving() -> None:
    """fillet/chamfer must tolerate the common `edges=`/`objects=` kwarg and the radius<->length
    mix-up (build123d wants the edges as the first POSITIONAL arg; chamfer's magnitude is
    `length`, fillet's is `radius`). This was a real crash: `chamfer(solid, edges=es, radius=r)`
    -> "unexpected keyword argument 'edges'", which made the agent devolve."""
    cases = {
        "chamfer edges=/radius=": ("b = Box(20, 20, 20)\n"
                                   "result = chamfer(b, edges=b.edges().filter_by(Axis.Z), radius=2)"),
        "fillet edges=/length=":  ("b = Box(20, 20, 20)\n"
                                   "result = fillet(b, edges=b.edges().filter_by(Axis.Z), length=2)"),
        "chamfer correct":        ("b = Box(20, 20, 20)\n"
                                   "result = chamfer(b.edges().filter_by(Axis.Z), length=2)"),
        "fillet correct":         ("b = Box(20, 20, 20)\n"
                                   "result = fillet(b.edges().filter_by(Axis.Z), radius=2)"),
    }
    for label, code in cases.items():
        res = _build(code)
        assert res.ok, f"{label} should build, got: {res.error}"
        assert res.volume and res.volume > 0, f"{label} produced no volume"
    print("[edges] fillet/chamfer tolerate edges=/objects= + radius<->length; correct calls unaffected")


def main() -> None:
    test_primitive_kwarg_aliases()
    test_mixed_radius_and_r_builds()
    test_fillet_chamfer_forgiving()
    print("\nKERNEL ALIAS TESTS PASSED")


if __name__ == "__main__":
    main()
