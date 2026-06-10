"""M0 contract-enforcement tests: fixtures validate, validator rejects garbage,
equations evaluate per the grammar, golden AICM is byte-stable.

Run:  python -m engine.doc.test_schema_fixtures   (from repo root, any py>=3.11)
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "docs" / "contracts" / "fixtures"

from engine.doc.equations import (  # noqa: E402
    EquationError,
    evaluate,
    evaluate_globals,
    validate_expr,
)
from engine.doc.schema import (  # noqa: E402
    validate_assembly,
    validate_document,
    validate_part,
)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_part_fixture_valid() -> None:
    doc = _load("part_bracket.json")
    errs = validate_part(doc)
    assert errs == [], "part_bracket.json should validate clean:\n" + "\n".join(errs)
    assert validate_document(doc) == []
    print("[schema] part_bracket.json validates clean")


def test_asm_fixture_valid() -> None:
    doc = _load("asm_clamp.json")
    errs = validate_assembly(doc)
    assert errs == [], "asm_clamp.json should validate clean:\n" + "\n".join(errs)
    assert validate_document(doc) == []
    print("[schema] asm_clamp.json validates clean")


def test_validator_rejects_garbage() -> None:
    base = _load("part_bracket.json")

    d = copy.deepcopy(base)
    d["features"][1]["type"] = "frobnicate"
    assert any("not a feature type" in e for e in validate_part(d))

    d = copy.deepcopy(base)
    del d["features"][1]["params"]["depth"]
    assert any("params.depth: required" in e for e in validate_part(d))

    d = copy.deepcopy(base)
    d["features"][0]["params"]["constraints"][0]["refs"] = ["e1.bogus", "e2.p1"]
    assert any("not a valid handle" in e for e in validate_part(d))

    d = copy.deepcopy(base)
    d["features"][0]["params"]["constraints"][4]["refs"] = ["e99"]
    assert any("unknown entity 'e99'" in e for e in validate_part(d))

    d = copy.deepcopy(base)
    d["features"][4]["refs"]["edges"][0]["kind"] = "blob"
    assert any(".kind:" in e for e in validate_part(d))

    d = copy.deepcopy(base)
    d["features"][1]["params"]["depth"] = {"expr": "thickness"}  # value missing
    assert any("depth.value" in e for e in validate_part(d))

    d = copy.deepcopy(base)
    d["features"][1]["params"]["depth"]["expr"] = "thickness + nope"
    assert any("unknown name(s) ['nope']" in e for e in validate_part(d))

    d = copy.deepcopy(base)
    d["features"][5]["refs"]["features"] = ["f99"]
    assert any("does not precede" in e for e in validate_part(d))

    d = copy.deepcopy(base)
    d["features"][0]["params"]["constraints"][0]["value"] = {"value": 1.0, "expr": None}
    assert any("must not carry a value" in e for e in validate_part(d))

    a = _load("asm_clamp.json")
    a["mates"][2]["value"] = None  # distance mate without value
    assert any("value: required for distance" in e for e in validate_assembly(a))

    print("[schema] validator rejects malformed documents with actionable errors")


def test_equations() -> None:
    assert evaluate("2 + 3 * 4") == 14.0
    assert evaluate("2 ^ 3") == 8.0
    assert abs(evaluate("sin(30)") - 0.5) < 1e-12, "trig must be in degrees"
    assert abs(evaluate("asin(0.5)") - 30.0) < 1e-9
    assert evaluate("max(2, min(9, 5))") == 5.0
    assert evaluate("thickness * 2", {"thickness": 6.0}) == 12.0

    env = evaluate_globals(
        [
            {"name": "c", "expr": "b + 1"},   # out of order on purpose
            {"name": "a", "expr": "10"},
            {"name": "b", "expr": "a * 2"},
        ]
    )
    assert env == {"a": 10.0, "b": 20.0, "c": 21.0}

    for bad, why in [
        ("__import__('os')", "function"),
        ("a.b", "construct"),
        ("[1,2]", "construct"),
        ("'x'", "numbers"),
    ]:
        err = validate_expr(bad)
        assert err is not None and why in err, f"{bad!r} should fail with {why}: {err}"

    try:
        evaluate("1 / 0")
        raise AssertionError("division by zero must raise")
    except EquationError:
        pass

    try:
        evaluate_globals([{"name": "x", "expr": "y"}, {"name": "y", "expr": "x"}])
        raise AssertionError("cycle must raise")
    except EquationError as exc:
        assert "cycle or unknown" in str(exc)

    print("[equations] grammar, degrees trig, ordering, cycles, sandboxing OK")


def test_golden_aicm() -> None:
    sys.path.insert(0, str(FIXTURES))
    try:
        import make_box_aicm  # type: ignore
    finally:
        sys.path.pop(0)

    a = make_box_aicm.generate()
    b = make_box_aicm.generate()
    assert a == b, "generator must be deterministic"
    assert a[:4] == b"AICM"

    golden = FIXTURES / "box.aicm"
    assert golden.exists(), "golden box.aicm missing — run make_box_aicm.py"
    on_disk = golden.read_bytes()
    assert a == on_disk, (
        f"generated AICM ({len(a)} B) differs from committed golden ({len(on_disk)} B) — "
        "format drift is a contract version bump"
    )
    print(f"[aicm] golden byte-stable ({len(a)} bytes, 24 verts / 12 tris / 12 edges / 8 verts)")


def main() -> None:
    test_part_fixture_valid()
    test_asm_fixture_valid()
    test_validator_rejects_garbage()
    test_equations()
    test_golden_aicm()
    print("\nSCHEMA/FIXTURE/EQUATION/AICM CONTRACT TESTS PASSED")


if __name__ == "__main__":
    main()
