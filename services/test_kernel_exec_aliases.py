"""Kernel robustness: common build123d keyword typos should BUILD, not crash the script.

Regression for the "computer case" failure where one `Cylinder(r=...)` (instead of
`radius=`) at line 80 killed an otherwise-fine 82-line model across every iteration.
"""
from __future__ import annotations

import math
import re
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

# The executor was re-homed to services (see services/kernel_exec.py).
from services.kernel_exec import _build123d_namespace, _exec_model, run_code


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


def _model(code: str):
    namespace, stdout, error = _exec_model(code)
    assert error is None, error
    return namespace, stdout


def _effective_radius(stdout: str) -> float:
    match = re.search(r"\beffective=([0-9.eE+-]+)", stdout)
    assert match is not None
    return float(match.group(1))


def _assert_reported_bracket(stdout: str, effective: float) -> None:
    match = re.search(
        r"observed bracket \[([0-9.eE+-]+), ([0-9.eE+-]+)\] mm, "
        r"resolution ([0-9.eE+-]+) mm", stdout,
    )
    assert match is not None
    lower, upper, resolution = map(float, match.groups())
    assert lower <= effective <= upper
    assert upper - lower <= max(1e-6, 1e-5 * max(abs(lower), abs(upper)))
    assert math.isclose(resolution, upper - lower, rel_tol=1e-5, abs_tol=1e-14)


def _assert_box_fillet(namespace, radius: float) -> None:
    from build123d import Axis, GeomType, fillet

    shape = namespace["result"]
    original = namespace["b"]
    expected = fillet(original.edges().filter_by(Axis.Z), radius)
    assert shape.is_valid and len(shape.solids()) == 1
    assert shape.solids()[0].is_manifold
    assert len(shape.faces().filter_by(GeomType.CYLINDER)) == 4
    assert math.isclose(shape.volume, expected.volume, rel_tol=1e-9, abs_tol=1e-8)
    assert shape.volume < original.volume


@pytest.mark.parametrize("call,radius", [
    ("fillet(es, 2)", 2.0),
    ("fillet(b, edges=es, length=2)", 2.0),
    ("fillet(b, objects=es, size=2)", 2.0),
    ("fillet(b, edge_list=es, r=2)", 2.0),
    ("fillet(b, edge=es, amount=2)", 2.0),
    ("fillet(b, object=es, dist=2)", 2.0),
    ("fillet(b, targets=es, radius=2)", 2.0),
    ("fillet(es, radius=0.05)", 0.05),
    ("fillet(filter(lambda e: True, es), radius=2)", 2.0),
])
def test_fillet_valid_radius_and_aliases(call: str, radius: float) -> None:
    namespace, stdout = _model(
        "b = Box(20, 12, 8)\n"
        "es = b.edges().filter_by(Axis.Z)\n"
        f"result = {call}\n"
    )
    assert "effective=" not in stdout
    _assert_box_fillet(namespace, radius)


def test_fillet_oversize_uses_all_edges_and_reports_actual_radius() -> None:
    namespace, stdout = _model(
        "b = Box(20, 12, 8)\n"
        "es = b.edges().filter_by(Axis.Z)\n"
        "result = fillet(b, edges=es, length=20)\n"
    )
    effective = _effective_radius(stdout)
    assert 5.9 < effective < 6.0
    _assert_reported_bracket(stdout, effective)
    _assert_box_fillet(namespace, effective)


@pytest.mark.parametrize("requested", ["0", "-1", "1e-12"])
def test_fillet_too_small_uses_a_verified_positive_radius(requested: str) -> None:
    namespace, stdout = _model(
        "b = Box(20, 12, 8)\n"
        "es = b.edges().filter_by(Axis.Z)\n"
        f"result = fillet(es, radius={requested})\n"
    )
    effective = _effective_radius(stdout)
    assert 0 < effective < 0.001
    _assert_reported_bracket(stdout, effective)
    _assert_box_fillet(namespace, effective)


def test_fillet_search_changes_builder_only_once() -> None:
    namespace, stdout = _model(
        "calls = []\n"
        "with BuildPart() as bp:\n"
        "    Box(20, 12, 8)\n"
        "    before = bp.part.volume\n"
        "    add = bp._add_to_context\n"
        "    def counted(*args, **kwargs):\n"
        "        calls.append(bp.part.volume)\n"
        "        return add(*args, **kwargs)\n"
        "    bp._add_to_context = counted\n"
        "    result = fillet(bp.edges().filter_by(Axis.Z), radius=20)\n"
        "    after = bp.part.volume\n"
    )
    assert _effective_radius(stdout) < 6
    assert namespace["calls"] == [namespace["before"]]
    assert namespace["after"] < namespace["before"]
    assert namespace["bp"].part.is_valid and namespace["result"].is_valid
    assert math.isclose(namespace["after"], namespace["result"].volume, rel_tol=1e-9)


def test_fillet_foreign_edge_rejects_without_changing_builder() -> None:
    namespace, stdout = _model(
        "foreign = Box(4, 4, 4)\n"
        "calls = []\n"
        "with BuildPart() as bp:\n"
        "    Box(20, 12, 8)\n"
        "    before = bp.part.volume\n"
        "    add = bp._add_to_context\n"
        "    def counted(*args, **kwargs):\n"
        "        calls.append(1)\n"
        "        return add(*args, **kwargs)\n"
        "    bp._add_to_context = counted\n"
        "    try:\n"
        "        fillet([bp.edges()[0], foreign.edges()[0]], radius=20)\n"
        "    except ValueError:\n"
        "        rejected = True\n"
        "    after = bp.part.volume\n"
        "result = bp.part\n"
    )
    assert namespace.get("rejected")
    assert not namespace["calls"] and namespace["before"] == namespace["after"]
    assert "effective=" not in stdout


def test_unclassified_kernel_failure_is_not_a_radius_bound(monkeypatch) -> None:
    from OCP import BRepFilletAPI

    native_builder = BRepFilletAPI.BRepFilletAPI_MakeFillet
    attempts = []

    class FailingBuilder:
        def __init__(self, shape):
            self.native = native_builder(shape)

        def Add(self, radius, edge):
            return self.native.Add(radius, edge)

        def Contour(self, edge):
            return self.native.Contour(edge)

        def Build(self):
            attempts.append(1)
            raise RuntimeError("kernel operation failed")

    monkeypatch.setattr(BRepFilletAPI, "BRepFilletAPI_MakeFillet", FailingBuilder)
    namespace, stdout = _model(
        "with BuildPart() as bp:\n"
        "    Box(20, 12, 8)\n"
        "    before = bp.part.volume\n"
        "    try:\n"
        "        fillet(bp.edges().filter_by(Axis.Z), radius=20)\n"
        "    except RuntimeError:\n"
        "        rejected = True\n"
        "    after = bp.part.volume\n"
        "result = bp.part\n"
    )
    assert namespace.get("rejected") and attempts == [1]
    assert namespace["before"] == namespace["after"]
    assert "effective=" not in stdout


@pytest.mark.parametrize("radius", [
    float("nan"), float("inf"), -float("inf"),
    Decimal("NaN"), Decimal("Infinity"), Decimal("1e-999"),
])
def test_fillet_nonfinite_or_unrepresentable_radius_is_an_error(radius, capsys) -> None:
    namespace = _build123d_namespace()
    box = namespace["Box"](20, 12, 8)
    edges = box.edges().filter_by(namespace["Axis"].Z)
    with pytest.raises(ValueError):
        namespace["fillet"](edges, radius=radius)
    assert "effective=" not in capsys.readouterr().out


@pytest.mark.parametrize("call,error_type", [
    pytest.param(lambda fillet, edges: fillet(edges, radius=2, unknown=True),
                 TypeError, id="unknown-keyword"),
    pytest.param(lambda fillet, edges: fillet(edges), TypeError, id="missing-radius"),
    pytest.param(lambda fillet, edges: fillet(edges, radius="wrong"),
                 TypeError, id="wrong-radius-type"),
    pytest.param(lambda fillet, edges: fillet([], radius=2), ValueError, id="no-edges"),
])
def test_fillet_api_errors_are_not_clamped(call, error_type, capsys) -> None:
    namespace = _build123d_namespace()
    box = namespace["Box"](20, 12, 8)
    edges = box.edges().filter_by(namespace["Axis"].Z)
    with pytest.raises(error_type):
        call(namespace["fillet"], edges)
    assert "effective=" not in capsys.readouterr().out


def test_fillet_seam_is_not_silently_dropped() -> None:
    _, stdout, error = _exec_model(
        "b = Cylinder(3, 10)\n"
        "result = fillet(b.edges(), radius=1)\n"
    )
    assert error is not None and "unsupported" in error.lower()
    assert "effective=" not in stdout


def test_native_2d_fillet_still_works() -> None:
    from build123d import Rectangle, fillet

    namespace, stdout = _model(
        "s = Rectangle(20, 12)\n"
        "result = fillet(s.vertices(), radius=2)\n"
    )
    reference = Rectangle(20, 12)
    expected = fillet(reference.vertices(), radius=2)
    assert namespace["result"].is_valid
    assert math.isclose(namespace["result"].area, expected.area, rel_tol=1e-9)
    assert "effective=" not in stdout


def main() -> None:
    test_primitive_kwarg_aliases()
    test_mixed_radius_and_r_builds()
    test_fillet_chamfer_forgiving()
    print("\nKERNEL ALIAS TESTS PASSED")


if __name__ == "__main__":
    main()
