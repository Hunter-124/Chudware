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

    Resolution: ``CHUDWARE_COPILOT_MODES_JSON`` (explicit file, tests) →
    ``<repo>/contracts/copilot_modes.json`` via this file's parents[2]
    (services/agent/ → repo root). Mirrors the C++ loader's order.
    """
    env = os.environ.get("CHUDWARE_COPILOT_MODES_JSON")
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

# Always-on MAIN-agent loadout (file handling, shell, web fetch) unioned onto
# the advertised list AND the dispatch policy for unrestricted modes
# (agent/swarm) even when a role card narrows the tool list. Absent key ->
# empty (older contracts without the floor keep prior behavior).
AGENT_DEFAULT_TOOLS: frozenset[str] = frozenset(_CONTRACT.get("agent_default_tools", ()))

# File-handling floor unioned onto every DAG-node/subagent role loadout.
SUBAGENT_DEFAULT_TOOLS: frozenset[str] = frozenset(_CONTRACT.get("subagent_default_tools", ()))

# The universal main chat needs the complete fs family plus shell/web fetch.
# Keep this derived from the two contract-owned floors so prompt assembly has
# one name and cannot silently omit the read half or the write half.
MAIN_CHAT_DEFAULT_TOOLS: frozenset[str] = (
    AGENT_DEFAULT_TOOLS | SUBAGENT_DEFAULT_TOOLS
)


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
    subagent: bool = False,
) -> Optional[list[str]]:
    """Apply mode policy while preserving open-vs-explicit tool scopes.

    ``role_tools=None`` means unrestricted in agent/swarm. An explicit empty
    iterable still receives the applicable always-on floor. Child turns use
    :data:`SUBAGENT_DEFAULT_TOOLS`; main turns use
    :data:`AGENT_DEFAULT_TOOLS`.
    """
    m = normalize_mode(mode)
    if plan_only:
        m = "plan"

    mode_set = tools_for_mode(m)
    role_specified = role_tools is not None
    role_list = [str(t) for t in (role_tools or []) if t]
    role_set = set(role_list)
    default_floor = SUBAGENT_DEFAULT_TOOLS if subagent else AGENT_DEFAULT_TOOLS

    if m == "swarm":
        if not role_specified:
            return None
        return sorted(
            role_set | SWARM_EXTRA_TOOLS | SHARED_DEFAULT_TOOLS | default_floor
        )

    if m == "agent":
        if not role_specified:
            return None
        # Agent mode stays lean: CAD document-lifecycle tools remain behind
        # enable_tools. Only the correct lane's fs/shell floor is automatic.
        return sorted(role_set | default_floor)

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
    subagent: bool = False,
) -> bool:
    """True when *tool* may run under *mode* (+ optional role intersection)."""
    name = (tool or "").strip()
    if not name:
        return False
    allowed = apply_mode_tools(
        mode, role_tools, plan_only=plan_only, subagent=subagent)
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
