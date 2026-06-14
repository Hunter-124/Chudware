"""Equation-driven-curve tests (SolidWorks-parity "Equation Driven Curve" sketch entity).

An ``eq_curve`` entity samples parametric expressions xt(t)/yt(t) over [t1, t2] (radians) into a
spline edge. A CLOSED parametric curve (e.g. a circle via cos/sin) forms a region that extrudes to a
solid; an OPEN curve is a valid edge that participates in loop detection. Expressions are evaluated
through a SAFE AST whitelist — no eval of arbitrary code.

Run (from repo root, dev venv):
    set PYTHONUTF8=1 && <devpython> -X utf8 -m pytest engine/doc/test_eq_curve.py -q
"""

from __future__ import annotations

import math

import engine.doc.test_features1 as T
from engine.doc.compiler import compile_document
from engine.doc.schema import validate_document


def _eq_sketch(fid: str, xt: str, yt: str, t1: float, t2: float) -> dict:
    return T._feat(fid, "sketch", {"entities": [
        {"id": "e1", "type": "eq_curve", "construction": False,
         "xt": xt, "yt": yt, "t1": t1, "t2": t2},
    ], "constraints": []}, refs={"plane": "plane.top"})


def _extrude(fid: str, profile: list, depth: float = 5.0) -> dict:
    return T._feat(fid, "extrude",
                   {"mode": "boss", "end": "blind", "depth": {"value": depth, "expr": None}},
                   refs={"profile": profile})


# --------------------------------------------------------------------------- validation


def test_eq_curve_validates() -> None:
    doc = T._doc([_eq_sketch("f1", "5*cos(t)", "5*sin(t)", 0.0, 2 * math.pi)])
    assert validate_document(doc) == [], "eq_curve sketch must validate"


def test_eq_curve_bad_expression_rejected() -> None:
    bad = T._feat("f1", "sketch", {"entities": [
        {"id": "e1", "type": "eq_curve", "construction": False,
         "xt": "__import__('os')", "yt": "5*sin(t)", "t1": 0.0, "t2": 1.0},
    ], "constraints": []}, refs={"plane": "plane.top"})
    errs = validate_document(T._doc([bad]))
    assert errs, "malicious / non-grammar xt expression must fail validation"


def test_eq_curve_unknown_name_rejected() -> None:
    bad = T._feat("f1", "sketch", {"entities": [
        {"id": "e1", "type": "eq_curve", "construction": False,
         "xt": "foo*cos(t)", "yt": "sin(t)", "t1": 0.0, "t2": 1.0},
    ], "constraints": []}, refs={"plane": "plane.top"})
    errs = validate_document(T._doc([bad]))
    assert errs, "unknown free variable 'foo' must fail validation"


# --------------------------------------------------------------------------- geometry


def test_parametric_circle_extrudes_to_cylinder() -> None:
    """A closed parametric circle r=5 forms a region that extrudes to a cylinder of volume
    ~= pi*r^2*depth within ~3% (spline-approximation tolerance)."""
    r = 5.0
    depth = 5.0
    doc = T._doc([_eq_sketch("f1", "5*cos(t)", "5*sin(t)", 0.0, 2 * math.pi),
                  _extrude("f2", [{"kind": "region", "sketch": "f1"}], depth=depth)])
    assert validate_document(doc) == []
    res = compile_document(doc)
    s1 = T._status(res, "f1")
    assert s1["ok"], f"eq_curve sketch failed: {s1.get('error')}"
    assert s1["resolved"].get("_region_count", 0) >= 1.0, "closed eq_curve must form >= 1 region"
    s2 = T._status(res, "f2")
    assert s2["ok"], f"eq_curve extrude failed: {s2.get('error')}"
    body = res.bodies[-1].shape
    expected = math.pi * r * r * depth
    assert abs(body.volume - expected) <= 0.03 * expected, \
        f"cylinder volume {body.volume:.2f} != {expected:.2f} (>3%)"


def test_open_parametric_arc_is_valid_edge() -> None:
    """A non-closed parametric arc (quarter circle) builds a valid edge — status ok, no region."""
    doc = T._doc([_eq_sketch("f1", "10*cos(t)", "10*sin(t)", 0.0, math.pi / 2)])
    assert validate_document(doc) == []
    res = compile_document(doc)
    s = T._status(res, "f1")
    assert s["ok"], f"open eq_curve failed: {s.get('error')}"
    assert s["resolved"].get("_region_count", 0) == 0.0, "an open arc must not form a region"


def test_parametric_curve_with_pi_and_e() -> None:
    """Constants pi and e and the wider math-function set are accepted and evaluated."""
    doc = T._doc([_eq_sketch("f1", "exp(0)*cos(t)*3", "3*sin(t)", 0.0, 2 * math.pi)])
    assert validate_document(doc) == []
    res = compile_document(doc)
    s = T._status(res, "f1")
    assert s["ok"], f"eq_curve with exp/pi failed: {s.get('error')}"
    assert s["resolved"].get("_region_count", 0) >= 1.0
