"""services/ecad_design — the autonomous ECAD design orchestrator (thin LLM orchestration).

Re-homed from ``engine_oracle/ecad/orchestrator.py`` (thin-client flip): the
orchestrator is a prompt/validate/retry state machine over an LLM provider — services-side
agent tooling, NOT engine logic. The deterministic ECAD machinery it drives (session, ops,
recipe fallback, netlist, SPICE sim) remains in the transitional ``engine.ecad`` /
``engine.sim`` modules until those port to core/.

Worker method registration: ``engine_oracle/ecad/rpc.py``'s ``ecad_design`` method imports
:func:`run_design` from here; the MCP ``ecad_design`` tool (services/mcp/ecad_sim_tools.py)
uses :func:`plan_design` + :func:`run_design`.
"""
from __future__ import annotations

from services.ecad_design.orchestrator import (
    DesignRun,
    StageEvent,
    build_design,
    plan_design,
    run_design,
    simulate_design,
    validate_design,
)

__all__ = [
    "DesignRun",
    "StageEvent",
    "build_design",
    "plan_design",
    "run_design",
    "simulate_design",
    "validate_design",
]
