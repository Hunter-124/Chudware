"""Suite-wide isolation guards for services/agent.

Only one guard lives here so far: the MCP tool catalog is a PROCESS-GLOBAL
singleton (``mcp_server.tool_registry``), and several modules in this suite
deliberately rebuild it — ``clear_catalog()`` for a controlled clean room, then
``register_*_tools`` against a throwaway stub MCP (``test_org.py``,
``test_roles.py``, ``test_tool_registry.py``). None of them restore it, so a
cleared or reshaped catalog leaks into every test that runs later.

That leak is silent rather than loud: role tool loadouts are intersected with
the live catalog, so a reshaped catalog reshapes an allow-set. Concretely,
``test_router_dispatch.py::test_cleanup_request_runs_general_agent`` loses
``plm_checkpoint`` from the general_agent loadout and the copilot's second tool
call is denied — but only when it runs after ``test_org.py``. Restore the warm
baseline at teardown so catalog contents are a suite invariant instead of a
function of alphabetical file ordering.
"""
from __future__ import annotations

import pytest


def _catalog_fingerprint() -> tuple:
    """Names PLUS the family/risk tags role expansion depends on.

    Names alone are not enough: the polluting suites re-register the same tool
    names against family labels derived from module stems, and
    ``roles.RoleCard.concrete_tools`` expands ``family:<name>`` tokens from those
    tags under a per-role cap — so a retagged catalog silently reshuffles which
    tools win a seat even when every name is still present.
    """
    from mcp_server import tool_registry as reg
    return tuple(sorted(
        (name, (meta or {}).get("family"), (meta or {}).get("risk"))
        for name, meta in (reg.catalog() or {}).items()
    ))


@pytest.fixture(scope="session")
def _warm_catalog_fingerprint() -> tuple:
    """Fingerprint of the fully warmed catalog, measured once per session."""
    try:
        from mcp_server import tool_registry as reg
        reg.warm_catalog()
        fp = _catalog_fingerprint()
    except Exception:  # pragma: no cover - registry unavailable
        return ()
    # A registry with no replayable baseline cannot be restored; leave the guard
    # off rather than clear a catalog we could not rebuild.
    return fp if len(fp) > 3 else ()


@pytest.fixture(autouse=True)
def _restore_warm_tool_catalog(_warm_catalog_fingerprint: tuple):
    """Reset the process-global tool catalog to its warm baseline if a test
    changed its contents.

    Teardown-only, so a module/class fixture that establishes its own clean room
    is never fought with mid-test; conftest fixtures tear down AFTER module and
    class fixtures, so even a module that clears the catalog in its own teardown
    is corrected here. Cost in the common case is one tuple compare.
    """
    yield
    if not _warm_catalog_fingerprint:
        return
    try:
        from mcp_server import tool_registry as reg
        if _catalog_fingerprint() != _warm_catalog_fingerprint:
            reg.clear_catalog()
            reg.warm_catalog()
    except Exception:  # pragma: no cover - never fail a test on cleanup
        pass
