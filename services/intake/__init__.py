"""Chudware requirements-intake / photo-measurement / first-try-fit "intake brain".

This package is the **pure decision logic** for the subsystem designed in
``docs/INTAKE_MEASUREMENT_DFM_PLAN.md`` — the part that decides *which* real-world
dimensions actually gate a mating interface, *how confident* a measurement is, *what to
ask the user for*, and *whether a part is safe to build*. The plan's north star: the parts
that **matter** fit on the first try, so metrology and "go measure with calipers" demands
are spent **only** on dimensions that gate a real mate; everything else gets a sensible
default and a flag.

:mod:`services.intake.intake` holds the whole brain — the canonical §2 schemas (each with a
``new_*`` factory and ``to_dict``/``from_dict`` so they round-trip through JSON exactly as
they persist into the ``.step+`` doc), the mating-feature taxonomy, the critical-dimension
ledger + gate, the deterministic confidence scorer + demand rule, the photo-request builder,
the FitReport + fit-test coupon, and the never-block policy helper.

It is **pure** by construction: stdlib only plus the DfM process numbers, which now come
from the C++ core (``core/dfm/process_profiles.cpp``) via ``core_bridge`` — **no engine/OCP
imports, no file I/O** — every entry point is a function over plain dicts, so the MCP tools
and the in-app agent stage can both call it as a thin adapter (the plan's "one shared
decision module both surfaces call") and it is trivially unit-testable.
"""
from __future__ import annotations

from .intake import (
    ARCHETYPES,
    TIER_ORDER,
    TIER_UNCERT_MM,
    Archetype,
    CriticalDimension,
    CriticalDimensionLedger,
    FitReport,
    IntakeState,
    MeasuredFeature,
    MeasuredPartProfile,
    apply_confirmation,
    assumptions,
    build_fit_report,
    build_ledger,
    build_photo_request,
    classify,
    compute_demands,
    coupon_spec,
    gate_decision,
    gate_ok,
    is_satisfied,
    new_critical_dimension,
    new_fit_report,
    new_intake_state,
    new_measured_feature,
    new_measured_part,
    required_tier,
    score_claim,
    tier_index,
    unresolved_critical,
    worst_tier,
)

__all__ = [
    "ARCHETYPES",
    "TIER_ORDER",
    "TIER_UNCERT_MM",
    "Archetype",
    "CriticalDimension",
    "CriticalDimensionLedger",
    "FitReport",
    "IntakeState",
    "MeasuredFeature",
    "MeasuredPartProfile",
    "apply_confirmation",
    "assumptions",
    "build_fit_report",
    "build_ledger",
    "build_photo_request",
    "classify",
    "compute_demands",
    "coupon_spec",
    "gate_decision",
    "gate_ok",
    "is_satisfied",
    "new_critical_dimension",
    "new_fit_report",
    "new_intake_state",
    "new_measured_feature",
    "new_measured_part",
    "required_tier",
    "score_claim",
    "tier_index",
    "unresolved_critical",
    "worst_tier",
]
