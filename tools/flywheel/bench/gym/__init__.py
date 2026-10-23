"""Design gym: graded challenges + failure taxonomy + assess-adjust-rerun loop.

The gym is the standing eval harness for the agent mission stack (run_mission end
to end, deliberation=full). It is deliberately an INDEPENDENT GRADER: it does NOT trust
the mission pipeline's own verify gate (which derives PASS structurally from node
completion — see ``loop._phase_gate_context``). Instead the gym:

  1. runs each challenge through ``run_mission`` with a fixture-supplied intake_fn and a
     turn-scripted agent_fn that writes REAL ledger events (so the classifier reads real
     event data, not a fabricated verdict),
  2. grades each challenge's acceptance criteria itself, against the produced ledger /
     artifacts, with deterministic ground-truth checks (``challenges/*.py``),
  3. replays the ledger to classify failures by taxonomy — every classification carrying
     evidence refs (event seq/kind), never vibes,
  4. emits a per-cycle markdown + JSON report: pass rates per challenge x tier, taxonomy
     counts, and RECOMMENDED DIAL ADJUSTMENTS (config/data only; prompt-wording changes
     are reported as findings for human review, never applied here).

HONESTY NOTE (read this before trusting a number). A genuine model-in-the-loop cycle
needs (a) a real tool-executing agent_fn, (b) a provisioned model (a local model such
as qwen3-4b, or a frontier key), and (c) the C++ core for real geometry checks. In this
environment none of the three is present (see ``reports/`` cycle-1 for the exact probe).
So Cycle 1 runs in MOCK mode: the agent_fn replays scripted per-role turns that emit
ledger events. This proves the harness + classifier + report end to end and is labeled
as mock everywhere. Scripted turns do not react to a dial change, so any pass-rate
DELTA from a dial adjustment is only meaningful for the deterministic, structural dials
(council thresholds, trigger packs, tier budgets asserted at assembly) — those deltas
are real; model-behaviour deltas are documented as pending-provision.
"""
from __future__ import annotations

from .taxonomy import (  # noqa: F401
    FailureClass, Classification, classify_ledger, TAXONOMY,
)

__all__ = ["FailureClass", "Classification", "classify_ledger", "TAXONOMY"]
