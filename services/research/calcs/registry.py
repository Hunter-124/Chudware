"""Calculator registry for ``services/research/calcs``.

Each calculator is registered with:
  - calc_id: str key
  - fn: callable(**inputs) -> dict (aicad.calc/1 envelope)
  - input_schema: {param_name: {type, unit, description, family}}
  - prefill_hints: {param_name: prefill_hint_kind}  — informs prefill.py

Contract: contracts/advisor.md §7 (aicad.calc/1).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# aicad.calc/1 envelope builder
# ---------------------------------------------------------------------------

def calc_envelope(
    calc: str,
    answer: Any,
    units: Optional[str],
    equation: str,
    inputs: dict,
    source_kind: str,
    citation: str,
    notes: Optional[list[str]] = None,
    prefilled: Optional[dict] = None,
) -> dict:
    """Build an aicad.calc/1 envelope (contracts/advisor.md §7).

    ``prefilled`` records {param_name: source_description} for inputs that
    came from live geometry rather than the user.
    """
    inp: dict[str, Any] = {}
    if prefilled:
        inp["prefilled"] = prefilled
    inp.update(inputs)
    return {
        "schema": "aicad.calc/1",
        "ok": True,
        "calc": calc,
        "answer": answer,
        "units": units,
        "equation": equation,
        "inputs": inp,
        "source": {
            "kind": source_kind,
            "citation": citation,
        },
        "notes": notes or [],
    }


def calc_fail(reason: str, hint: str) -> dict:
    """§6 degradation — mirrors fits.py ``_fail``."""
    return {"ok": False, "schema": "aicad.calc/1", "reason": reason, "hint": hint}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, dict] = {}


def register(
    calc_id: str,
    fn: Callable,
    input_schema: dict[str, dict],
    prefill_hints: Optional[dict[str, str]] = None,
) -> None:
    """Register a calculator.

    Parameters
    ----------
    calc_id:
        Unique key (e.g. ``"beam"``, ``"bolted_joint"``).
    fn:
        callable(**inputs) -> dict (aicad.calc/1).
    input_schema:
        {param_name: {type, unit, description, family}}.
        ``family`` ∈ {length, force, torque, pressure, material_ref,
                       dimensionless, string, integer}.
    prefill_hints:
        {param_name: hint_kind}  — consumed by prefill.py.
        hint_kind ∈ {cylinder_diameter, bbox_x, bbox_y, bbox_z, span,
                      wall_thickness, material_id}.
    """
    _REGISTRY[calc_id] = {
        "fn": fn,
        "input_schema": input_schema,
        "prefill_hints": prefill_hints or {},
    }


def get_calc(calc_id: str) -> Optional[dict]:
    """Return registry entry or None."""
    return _REGISTRY.get(calc_id)


def list_calcs() -> dict[str, dict]:
    """Return {calc_id: {input_schema, prefill_hints}} for discovery (no fn)."""
    return {
        cid: {"input_schema": v["input_schema"], "prefill_hints": v["prefill_hints"]}
        for cid, v in _REGISTRY.items()
    }


def run_calc(calc_id: str, inputs: dict, prefilled: Optional[dict] = None) -> dict:
    """Dispatch to a registered calculator.

    ``prefilled`` is passed through to the calc function so the envelope can
    record provenance.  Unregistered ``calc_id`` returns §6 degradation.
    """
    entry = _REGISTRY.get(calc_id)
    if entry is None:
        return calc_fail(
            "not_found",
            f"unknown calc_id {calc_id!r}; known: {sorted(_REGISTRY)}.",
        )
    try:
        return entry["fn"](prefilled=prefilled, **inputs)
    except TypeError as exc:
        return calc_fail("bad_query", f"calc {calc_id!r}: wrong params — {exc}.")
    except Exception as exc:  # noqa: BLE001
        return calc_fail("error", f"calc {calc_id!r}: {exc}.")
