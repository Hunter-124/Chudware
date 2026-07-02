"""services/serving — adapter serving & per-insertion-point routing (T-H9, Pillar 16).

Implements the `routing` seam reserved in ``contracts/providers.md`` §7: a per-agent
insertion-point routing table that decides *which brain answers* at each call site
(base model, a served LoRA adapter, or the safe deterministic/frontier fallback),
plus the canary-rollout / rollback / human-gated-promotion machinery.

Nothing here launches vLLM or imports a model runtime. Serving is an EXTERNAL,
OpenAI-compatible vLLM process the user starts (see ``docs/serving_runbook.md``);
this package only *routes* to it via the frozen providers/1 contract.
"""
from __future__ import annotations

from .routing import (
    Fallback,
    RouteDecision,
    RouteEntry,
    RoutingTable,
    load_routing_table,
    save_routing_table,
)

__all__ = [
    "Fallback",
    "RouteDecision",
    "RouteEntry",
    "RoutingTable",
    "load_routing_table",
    "save_routing_table",
]
