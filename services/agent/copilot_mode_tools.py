"""copilot_mode_tools.py — per-mode tool allowlists for Agent|Plan|Swarm|Edit|Ask.

Contract (frozen): contracts/copilot_modes.md — the machine-readable data lives
in contracts/copilot_modes.json (``aicad.copilot_modes/1``) and is loaded at
import time below; this module keeps NO private copy of the sets so the Python
(MCP/DAG) and C++ (in-app, core/agent/copilot_modes.cpp) runtimes cannot
diverge. Parity: services/agent/test_copilot_modes_parity.py vs
core/tests/test_copilot_modes.cpp over the same mode × tool matrix.

  options.mode ∈ agent|plan|swarm|edit|ask
  Mode gates the tool set applied at agent_turn.run_turn (intersected with any
  role-card allowlist the caller already computed).

Policies:
  * ask/plan/edit — restricted mode lists UNION SHARED_DEFAULT_TOOLS (doc
    lifecycle, web, advisor, ask_user, baseline read/help) plus meta-tools
    enable_tools / list_tool_categories so the model can pull more categories.
  * plan  — agent-broad tool access MINUS geometry commit mutators; shared
            defaults (incl. make/save/open/close) stay so plan can build.
  * edit  — doc-scoped mutating tools; shared defaults still include new_part
            / close / import so edit can finish a simple part.
  * agent — full (None = unrestricted at mode layer; role card still applies)
  * swarm — full + explicit corporate/delegate tools kept in the set

Subagents (``delegate``) stay available in agent/swarm/edit/plan. Ask also
gets shared defaults (doc lifecycle + advise + web) so it never dead-ends.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Optional


def _contract_path() -> Path:
    """Locate contracts/copilot_modes.json (canonical mode data).

    Resolution: ``AI_CAD_COPILOT_MODES_JSON`` (explicit file, tests) →
    ``<repo>/contracts/copilot_modes.json`` via this file's parents[2]
    (services/agent/ → repo root). Mirrors the C++ loader's order.
    """
    env = os.environ.get("AI_CAD_COPILOT_MODES_JSON")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "contracts" / "copilot_modes.json"


def _load_contract() -> dict:
    with open(_contract_path(), encoding="utf-8") as fh:
        return json.load(fh)


_CONTRACT = _load_contract()

# Canonical mode vocabulary (frozen with host/core).
COPILOT_MODES = frozenset(_CONTRACT["modes"])
DEFAULT_MODE = _CONTRACT["default_mode"]

# Always-available floor across restricted modes so the agent can get work done,
# talk to the user, reach the advisor, search the web, and manage docs.
SHARED_DEFAULT_TOOLS: frozenset[str] = frozenset(_CONTRACT["shared_default_tools"])

# ---------------------------------------------------------------------------
# Allowlists (None = no mode-level restriction) — loaded from the contract JSON
# ---------------------------------------------------------------------------

# Ask: pure read / research. No mutation, no mission, no new docs.
ASK_TOOLS: frozenset[str] = frozenset(_CONTRACT["mode_tools"]["ask"])

# Plan: same breadth as agent for research/sim/inspect/sourcing/delegate, but
# NEVER geometry/document commit mutators. Used when role_tools is empty (a
# concrete list is required because None cannot express a denylist). When a
# role card is present, apply_mode_tools subtracts mutators from that card
# instead of intersecting with this set — callers that must stay broad should
# pass role_tools=None for plan.
PLAN_TOOLS: frozenset[str] = frozenset(_CONTRACT["mode_tools"]["plan"])

# Edit: mutate current doc; no new_part / mission / corporate activate.
EDIT_TOOLS: frozenset[str] = frozenset(_CONTRACT["mode_tools"]["edit"])

# Explicit denylists used when intersecting open-ended role cards.
_MUTATING_TOOLS: frozenset[str] = frozenset(_CONTRACT["mutating_tools"])

_EDIT_DENIED: frozenset[str] = frozenset(_CONTRACT["edit_denied"])

# Swarm extras always kept when mode is swarm (unioned onto an open allowlist).
SWARM_EXTRA_TOOLS: frozenset[str] = frozenset(_CONTRACT["swarm_extra_tools"])


def normalize_mode(mode: str | None) -> str:
    """Return a canonical mode string (default ``agent``)."""
    m = (mode or DEFAULT_MODE).strip().lower()
    return m if m in COPILOT_MODES else DEFAULT_MODE


def tools_for_mode(mode: str | None) -> Optional[frozenset[str]]:
    """Return the mode allowlist, or None when the mode is unrestricted.

    ``agent`` and ``swarm`` return None (full catalog at the mode layer). Callers
    that need swarm extras should use :func:`apply_mode_tools` which unions
    ``SWARM_EXTRA_TOOLS`` onto any existing allowlist.
    """
    m = normalize_mode(mode)
    if m == "ask":
        return ASK_TOOLS
    if m == "plan":
        return PLAN_TOOLS
    if m == "edit":
        return EDIT_TOOLS
    # agent / swarm — unrestricted at mode layer
    return None


def apply_mode_tools(
    mode: str | None,
    role_tools: Iterable[str] | None = None,
    *,
    plan_only: bool | None = None,
) -> Optional[list[str]]:
    """Intersect *role_tools* with the mode allowlist, then floor with shared defaults.

    Returns:
      * None  — no restriction (agent mode with no role card, or empty inputs
        that mean "full").
      * list  — concrete allowed tool names (sorted for stability in tests).

    Rules:
      * ``plan_only=True`` forces plan semantics even if mode is agent.
      * plan = agent-broad minus geometry commit mutators, then UNION
        :data:`SHARED_DEFAULT_TOOLS` (doc lifecycle stays so plan can build).
      * ask / edit: mode list ∩ role (or mode list), mode denylists applied,
        then UNION shared defaults (shared wins over denylists for the floor).
      * When *role_tools* is None/empty and mode is open → None (unrestricted).
      * swarm unions SWARM_EXTRA_TOOLS onto the role set (or leaves unrestricted).
    """
    m = normalize_mode(mode)
    if plan_only:
        m = "plan"

    mode_set = tools_for_mode(m)
    role_list = [str(t) for t in (role_tools or []) if t]
    role_set = set(role_list)

    if m == "swarm":
        if not role_set:
            # Unrestricted + ensure corporate/delegate names are known to callers
            # that materialize from this helper for prompts.
            return None
        out = sorted(role_set | SWARM_EXTRA_TOOLS)
        return out

    if m == "agent":
        if not role_set:
            return None
        return sorted(role_set)

    # Plan: open like agent minus geometry commit mutators (add_features etc.),
    # then floor with SHARED_DEFAULT_TOOLS so make/save/open/close stay.
    if m == "plan":
        _plan_deny = _MUTATING_TOOLS | {"new_assembly", "edit_feature"}
        # new_part / close_part / import_part live in SHARED_DEFAULT_TOOLS and
        # are re-added below (product: plan can build simple parts).
        if role_set:
            allowed = role_set - _plan_deny
        else:
            allowed = set(PLAN_TOOLS) - _plan_deny
        allowed |= SHARED_DEFAULT_TOOLS
        return sorted(allowed)

    # Restricted modes: ask / edit
    assert mode_set is not None
    if not role_set:
        allowed = set(mode_set)
    else:
        allowed = role_set & mode_set

    if m == "edit":
        allowed -= _EDIT_DENIED
    if m == "ask":
        allowed -= _MUTATING_TOOLS

    # Shared floor always wins for restricted modes (doc lifecycle + advise +
    # web + meta enable_tools / list_tool_categories).
    allowed |= SHARED_DEFAULT_TOOLS
    return sorted(allowed)


def is_tool_allowed(
    tool: str,
    mode: str | None,
    *,
    role_tools: Iterable[str] | None = None,
    plan_only: bool | None = None,
) -> bool:
    """True when *tool* may run under *mode* (+ optional role intersection)."""
    name = (tool or "").strip()
    if not name:
        return False
    allowed = apply_mode_tools(mode, role_tools, plan_only=plan_only)
    if allowed is None:
        return True
    return name in allowed


def mode_options_from_mapping(options: Any | None) -> dict[str, Any]:
    """Normalize a host/core ``options`` mapping into mode-related fields.

    Keys (all optional):
      mode, plan_only, approved_plan, execute_as, corporate_force, scope, advisor
    """
    if not isinstance(options, dict):
        options = {}
    mode = normalize_mode(options.get("mode"))
    plan_only = bool(options.get("plan_only", False))
    if mode == "plan" and "plan_only" not in options:
        plan_only = True
    execute_as = str(options.get("execute_as") or "").strip().lower()
    if execute_as not in ("agent", "swarm", ""):
        execute_as = ""
    corporate_force = bool(options.get("corporate_force", False))
    if mode == "swarm":
        corporate_force = True
    advisor = options.get("advisor")
    if advisor is None:
        advisor = True
    else:
        advisor = bool(advisor)
    scope = options.get("scope")
    if scope is not None and not isinstance(scope, dict):
        scope = None
    approved_plan = options.get("approved_plan")
    if approved_plan is not None and not isinstance(approved_plan, str):
        approved_plan = str(approved_plan)
    return {
        "mode": mode,
        "plan_only": plan_only,
        "approved_plan": approved_plan,
        "execute_as": execute_as or None,
        "corporate_force": corporate_force,
        "scope": scope,
        "advisor": advisor,
    }
