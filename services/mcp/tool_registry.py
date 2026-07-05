"""MCP tool registry — tags every tool with {family, risk} and enforces a
per-context allow-list at DISPATCH.

Contract: ``contracts/providers.md`` §6. This module is the ONE enforcement point;
the individual ``*_tools.py`` modules are never edited. ``server.py`` wraps each
``register_*_tools(mcp)`` call site with :func:`view`, which:

  1. records every ``@mcp.tool()`` registration under its family + a derived risk class
     into a process-wide :func:`catalog`, and
  2. wraps each tool function in ONE shared guard so that, at call time, a tool not in
     the active allow-list returns a contract-shaped ``{"ok": False, "reason", "hint"}``
     (never a traceback, never a silent success) instead of running.

Default policy = everything allowed (no behavior change vs. today). The DAG node contract
(T-A1 ``dag_schema``) passes explicit per-node ``tools:`` allow-lists that bind here later.
"""
from __future__ import annotations

import inspect
import os
from functools import wraps

# Risk classes (contract §6). Order = increasing sensitivity.
RISK_READ = "read"
RISK_WRITE = "write"
RISK_SPEND = "spend"
RISK_EXTERNAL = "external"
RISKS = (RISK_READ, RISK_WRITE, RISK_SPEND, RISK_EXTERNAL)

# Name-pattern risk classifier. Checked most-sensitive first so `export_*`/`*_post`
# outrank a generic write verb. A tool that matches nothing defaults to WRITE
# (fail-safe: treat unknown as mutating, never as free read).
_EXTERNAL_PREFIXES = ("export_", "submit_", "import_", "request_")
_EXTERNAL_SUFFIXES = ("_post", "_export")
_READ_PREFIXES = ("describe_", "query_", "measure_", "list_", "get_", "check_",
                  "render_", "inspect_", "review_", "compare_", "draft_", "fit_",
                  "wall_", "contact_")
_READ_SUFFIXES = ("_status",)
_READ_EXACT = {"hover", "cad_guide", "intake_status", "ecad_status", "cam_job_tree",
              "cam_materials", "cam_op_types", "cam_tool_types", "cam_post_dialects",
              "list_parts", "list_configurations", "list_world_bodies",
              "list_standard_parts",
              # Design ledger (T-W1): ledger_get reads the ledger; the name starts with
              # "ledger_" not "get_", so the prefix classifier misses it — explicit entry needed.
              "ledger_get",
              # Workspace (T-W16): status/events are read-only poll tools.
              "workspace_status", "workspace_doc_events"}

# Explicit overrides for names the classifier would mislabel. name -> risk.
_RISK_OVERRIDES: dict[str, str] = {
    # nothing metered/spend today; add here when a paid-compute tool lands.
}


def classify_risk(name: str) -> str:
    """Derive the risk class for a tool *name* (contract §6)."""
    if name in _RISK_OVERRIDES:
        return _RISK_OVERRIDES[name]
    if name in _READ_EXACT:
        return RISK_READ
    if name.startswith(_EXTERNAL_PREFIXES) or name.endswith(_EXTERNAL_SUFFIXES):
        return RISK_EXTERNAL
    if name.startswith(_READ_PREFIXES) or name.endswith(_READ_SUFFIXES):
        return RISK_READ
    return RISK_WRITE


# --------------------------------------------------------------------------- catalog
# Process-wide tag catalog: tool name -> {"family": str, "risk": str}.
_CATALOG: dict[str, dict] = {}


def catalog() -> dict[str, dict]:
    """The live {name: {family, risk}} tag catalog."""
    return _CATALOG


def clear_catalog() -> None:
    """Reset the catalog (tests)."""
    _CATALOG.clear()


def record(name: str, family: str, risk: str | None = None) -> dict:
    entry = {"family": family, "risk": risk or classify_risk(name)}
    _CATALOG[name] = entry
    return entry


# --------------------------------------------------------------------------- policy
# Active allow-list policy. ``None`` for either field means "fall back to env"; an
# allow-list of ``None`` (after env) means EVERYTHING is allowed (default).
_ACTIVE_ALLOW: set[str] | None = None
_ACTIVE_DENY_RISK: set[str] | None = None
_POLICY_SET = False


def set_policy(allow=None, deny_risk=None) -> None:
    """Set the active allow-list policy explicitly (DAG node / tests).

    *allow*: iterable of tool names and/or ``"family:<name>"`` tokens, or ``None`` = allow
    all. *deny_risk*: iterable of risk-class names to deny wholesale, or ``None`` = deny none.
    """
    global _ACTIVE_ALLOW, _ACTIVE_DENY_RISK, _POLICY_SET
    _ACTIVE_ALLOW = set(allow) if allow is not None else None
    _ACTIVE_DENY_RISK = set(deny_risk) if deny_risk is not None else None
    _POLICY_SET = True


def reset_policy() -> None:
    """Drop the explicit policy and fall back to environment (tests)."""
    global _ACTIVE_ALLOW, _ACTIVE_DENY_RISK, _POLICY_SET
    _ACTIVE_ALLOW = _ACTIVE_DENY_RISK = None
    _POLICY_SET = False


def _env_allow() -> set[str] | None:
    raw = os.environ.get("AI_CAD_TOOLS_ALLOW", "").strip()
    if not raw:
        return None
    return {t.strip() for t in raw.split(",") if t.strip()}


def _env_deny_risk() -> set[str] | None:
    raw = os.environ.get("AI_CAD_TOOLS_DENY_RISK", "").strip()
    if not raw:
        return None
    return {t.strip().lower() for t in raw.split(",") if t.strip()}


def _current_allow() -> set[str] | None:
    if _POLICY_SET and _ACTIVE_ALLOW is not None:
        return _ACTIVE_ALLOW
    if _POLICY_SET and _ACTIVE_ALLOW is None:
        return None
    return _env_allow()


def _current_deny_risk() -> set[str] | None:
    if _POLICY_SET and _ACTIVE_DENY_RISK is not None:
        return _ACTIVE_DENY_RISK
    if _POLICY_SET and _ACTIVE_DENY_RISK is None:
        return None
    return _env_deny_risk()


def is_allowed(name: str) -> bool:
    """True if *name* may be dispatched under the active policy (default: all allowed)."""
    return deny_reason(name) is None


def deny_reason(name: str) -> str | None:
    """None if allowed; else a human reason string for the refusal."""
    risk = (_CATALOG.get(name) or {}).get("risk") or classify_risk(name)
    deny_risk = _current_deny_risk()
    if deny_risk and risk in deny_risk:
        return (f"tool {name!r} is risk-class {risk!r}, which is denied by the active "
                f"policy (AI_CAD_TOOLS_DENY_RISK)")
    allow = _current_allow()
    if allow is None:
        return None  # default: everything allowed
    family = (_CATALOG.get(name) or {}).get("family")
    if name in allow or (family and f"family:{family}" in allow):
        return None
    return (f"tool {name!r} is not in the active allow-list "
            f"(set AI_CAD_TOOLS_ALLOW or the DAG node tools: list to permit it)")


def refusal(name: str) -> dict:
    """The contract-shaped refusal payload for a disallowed dispatch (contract §6.1)."""
    return {
        "ok": False,
        "reason": deny_reason(name) or f"tool {name!r} is not permitted",
        "hint": ("This tool is gated by the per-context allow-list. Permit it via the DAG "
                 "node's tools: list, or AI_CAD_TOOLS_ALLOW / AI_CAD_TOOLS_DENY_RISK."),
    }


# --------------------------------------------------------------------------- guard + view
def _workspace_lease_refusal(doc_id: str, name: str) -> dict | None:
    """Check write-lease for a mutating doc_id-bearing tool call.

    Returns None to proceed, or a contract-shaped refusal dict if another
    agent holds the write lease.  Import is deferred to avoid circular imports
    (workspace.py imports from interaction.py, not from tool_registry.py).

    Read tools (RISK_READ) are never checked — this is the hot path for reads
    and the check is gated on risk class first so there is zero overhead for
    read-only calls.
    """
    if classify_risk(name) == RISK_READ:
        return None  # read tools never need a lease check
    try:
        from services.agent.workspace import check_write_lease  # deferred import
        result = check_write_lease(doc_id, name)
        return result  # None or refusal dict
    except Exception:  # noqa: BLE001 — guard errors must never crash dispatch
        return None


def _guard(fn, name):
    """Wrap *fn* so a disallowed dispatch returns :func:`refusal` instead of running.

    ONE shared guard for every tool — this is the single dispatch chokepoint. The wrapper
    preserves *fn*'s signature (via functools.wraps' ``__wrapped__``) so FastMCP still
    introspects the real parameters, and matches its sync/async-ness.

    T-W16 extension: mutating tools that carry a ``doc_id`` keyword argument are also
    checked against the active workspace lease registry.  Read tools are never checked
    (classify_risk fast-path).  If no workspace session is active the check is a no-op.
    """
    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def aguard(*args, **kwargs):
            if not is_allowed(name):
                return refusal(name)
            doc_id = kwargs.get("doc_id")
            if doc_id:
                denial = _workspace_lease_refusal(doc_id, name)
                if denial is not None:
                    return denial
            return await fn(*args, **kwargs)
        return aguard

    @wraps(fn)
    def guard(*args, **kwargs):
        if not is_allowed(name):
            return refusal(name)
        doc_id = kwargs.get("doc_id")
        if doc_id:
            denial = _workspace_lease_refusal(doc_id, name)
            if denial is not None:
                return denial
        return fn(*args, **kwargs)
    return guard


class _FamilyView:
    """A thin proxy over a FastMCP-like object that stamps registrations with *family*.

    Delegates every attribute to the wrapped ``mcp`` EXCEPT ``tool``: that is intercepted so
    each registered function is tagged into the catalog and wrapped in the dispatch guard.
    """

    def __init__(self, mcp, family: str):
        self._mcp = mcp
        self._family = family

    def tool(self, *t_args, **t_kwargs):
        inner = self._mcp.tool(*t_args, **t_kwargs)
        family = self._family

        def deco(fn):
            name = t_kwargs.get("name") or getattr(fn, "__name__", "?")
            record(name, family)
            return inner(_guard(fn, name))
        return deco

    def __getattr__(self, item):
        return getattr(self._mcp, item)


def view(mcp, family: str) -> _FamilyView:
    """Wrap *mcp* so ``register_*_tools(view(mcp, family))`` tags + guards its tools."""
    return _FamilyView(mcp, family)
