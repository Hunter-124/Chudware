"""End-to-end example: brief from a chat transcript → requirement verified by measure_between.

This module satisfies DoD item 3: "brief authored from a chat transcript, requirement verified
by measure_between, status flips to verified with evidence ref."

Run standalone:
    python -m services.requirements.example
"""
from __future__ import annotations

import json

from ..requirements import RequirementsDoc


def build_example_brief() -> RequirementsDoc:
    """Build a design brief as if from a chat transcript.

    Scenario: user asks for a bracket that accepts a 608 bearing (22 mm bore, press fit)
    and can hold at least 50 N load in static use.
    """
    doc = RequirementsDoc.new("Bearing Bracket — design brief")

    # --- Stakeholder needs (prose, no numbers) ---
    n1 = doc.add_need(
        "The bracket bore must accept a standard bearing with a press fit.",
        source="chat",
    )
    n2 = doc.add_need(
        "The bracket must withstand the expected static load without yielding.",
        source="chat",
    )

    # --- Requirements ---
    # R1: bore diameter (dimensional, numeric quantity → quantity field)
    doc.add_requirement(
        need_id=n1.id,
        kind="dimensional",
        text="Bore accepts a standard bearing with press fit, no gap.",
        verify_by=[
            {"verifier": "measure_between", "args": "bore inner diameter"},
            {"verifier": "fit_report"},
        ],
        quantity={
            "param": "bore_dia",
            "op": "==",
            "value": 22.0,
            "unit": "mm",
            "tol": {"plus": 0.0, "minus": 0.013},
        },
    )

    # R2: static load (functional, numeric quantity → quantity field)
    doc.add_requirement(
        need_id=n2.id,
        kind="functional",
        text="Bracket must withstand static load without yielding under expected service conditions.",
        verify_by=[
            {"verifier": "run_fea_static"},
        ],
        quantity={
            "param": "max_von_mises",
            "op": "<=",
            "value": 250.0,
            "unit": "MPa",
        },
    )

    return doc


def simulate_verification(doc: RequirementsDoc) -> RequirementsDoc:
    """Simulate what happens when measure_between returns a passing result.

    In production this would be called by the req_status MCP tool after a verifier runs.
    """
    # User confirms R1 first (required before verified)
    doc.set_status("R1", "user_confirmed", who="user")

    # Verifier runs and passes → agent sets verified with evidence
    evidence = [
        {
            "verifier": "measure_between",
            "result": {"measured_mm": 22.0, "within_tol": True},
            "ts": "2026-07-02T12:00:00Z",
        }
    ]
    doc.set_status("R1", "verified", who="agent", evidence=evidence)

    return doc


def main() -> None:
    doc = build_example_brief()
    print("=== After authoring ===")
    print(doc.to_markdown())
    print()

    doc = simulate_verification(doc)
    print("=== After verification ===")
    print(doc.to_markdown())
    print()

    # Round-trip check
    d = doc.to_dict()
    doc2 = RequirementsDoc.from_dict(d)
    assert doc2.to_dict() == d, "Round-trip failed!"
    print("Round-trip: OK")

    # Pretty-print the JSON
    print("\n=== JSON ===")
    print(json.dumps(d, indent=2))


if __name__ == "__main__":
    main()
