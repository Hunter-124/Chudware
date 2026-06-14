"""Fast sketch contract checks that do not require the CAD kernel.

These guard admission/preview edges local LLM workflows rely on: malformed constraints should fail
before they enter document history.
"""
from __future__ import annotations

from engine.doc import ops


def _doc() -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "contract-test",
        "name": "contract-test",
        "units": {"system": "MMGS"},
        "features": [{
            "id": "f1",
            "type": "sketch",
            "name": "Sketch1",
            "refs": {"plane": "plane.top"},
            "params": {
                "entities": [{"id": "e1", "type": "line", "p1": [0, 0], "p2": [10, 0]}],
                "constraints": [],
            },
        }],
    }


def _must_reject(op: dict, text: str) -> None:
    try:
        ops.apply_ops(_doc(), [op])
    except ops.OpError as exc:
        assert text in exc.message, exc.message
        return
    raise AssertionError(f"expected rejection containing {text!r}")


def test_sketch_constraint_admission_rejects_bad_contracts() -> None:
    _must_reject(
        {"op": "sketch_add_constraint", "sketch": "f1",
         "constraint": {"type": "almost_parallel", "refs": ["e1"]}},
        "unsupported sketch constraint type",
    )
    _must_reject(
        {"op": "sketch_add_constraint", "sketch": "f1",
         "constraint": {"type": "distance", "refs": ["e1.p1", "e1.p2"]}},
        "requires a value object",
    )
    _must_reject(
        {"op": "sketch_add_constraint", "sketch": "f1",
         "constraint": {"type": "symmetric", "refs": ["e1.p1", "e1.p2"]}},
        "requires 3 refs",
    )


def main() -> None:
    test_sketch_constraint_admission_rejects_bad_contracts()
    print("SKETCH CONTRACT TESTS PASSED")


if __name__ == "__main__":
    main()
