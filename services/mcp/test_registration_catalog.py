"""Registration-catalog consistency test (audit fix, companion to test_mcp_freshness.py).

test_mcp_freshness.py checks that every tool is *discoverable* (named in server
instructions / cad_guide text). This file checks the orthogonal, runtime property:
every tool actually registered on the FastMCP server also carries a tool_registry
catalog entry tagging it with a real family — the thing the Settings -> AI -> Tools
per-family toggle and deny_reason() dispatch guard depend on (contracts/providers.md
§6). A tool registered through a bare ``mcp.tool()`` instead of
``tool_registry.view(mcp, family).tool()`` gets NO catalog entry, so
``deny_reason()`` silently skips its family check and the tool is always allowed
no matter what the user toggles off — see services/mcp/tool_registry.py:deny_reason.

How it works:
  1. Import services.mcp.server (real import, no stubs — core_bridge only loads the
     C++ core lazily inside call(), never at module import time, so this needs
     neither libaicad nor a running daemon). This runs every register_*_tools(...)
     call site for real and populates tool_registry's process-wide catalog.
  2. Snapshot the live FastMCP tool names + catalog immediately after import, at
     MODULE level, before any other test module's fixtures get a chance to run.
     (tool_registry.clear_catalog()/record() operate on one shared process-wide
     dict; sibling test files, e.g. services/agent/test_tool_registry.py, legitimately
     clear/rebuild that dict around their own tests. Snapshotting immediately after
     import decouples this file from pytest's cross-file execution order.)
  3. Each test restores the live catalog from that snapshot in setUpClass, then
     asserts against it.

Run:
  python -m pytest services/mcp/test_registration_catalog.py -v
"""
from __future__ import annotations

import copy
import os
import unittest
from unittest.mock import patch

from services.mcp import tool_registry as reg  # lightweight: no C++ core / mcp package needed
from services.agent.config import DEFAULT_TOOL_FAMILIES
import services.agent.config as _cfg

# ---------------------------------------------------------------------------
# Import server.py for real (module load time, before any test runs) and snapshot
# the result. See module docstring point 2 for why this must happen here and not
# inside setUpClass.
# ---------------------------------------------------------------------------
_IMPORT_ERROR: Exception | None = None
_TOOL_NAMES: set[str] = set()
_CATALOG_SNAPSHOT: dict[str, dict] = {}

try:
    import services.mcp.server as _srv  # noqa: PLC0415 - side effect: registers every tool
    _TOOL_NAMES = set(_srv.mcp._tool_manager._tools.keys())
    _CATALOG_SNAPSHOT = copy.deepcopy(reg.catalog())
except Exception as exc:  # pragma: no cover - environment missing mcp/anyio packages
    _srv = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


class TestRegistrationCatalog(unittest.TestCase):
    """Runtime consistency: server.py registration wiring matches the tool_registry catalog."""

    @classmethod
    def setUpClass(cls):
        if _IMPORT_ERROR is not None:
            raise unittest.SkipTest(f"services.mcp.server import failed: {_IMPORT_ERROR!r}")
        if not _TOOL_NAMES:
            raise unittest.SkipTest("services.mcp.server registered zero tools — broken import")
        # Re-seed the live tool_registry catalog from the post-import snapshot so this
        # class is immune to other test modules calling tool_registry.clear_catalog()
        # (a process-wide global) elsewhere in the same pytest session.
        reg.clear_catalog()
        for name, meta in _CATALOG_SNAPSHOT.items():
            reg.record(name, meta.get("family"), meta.get("risk"))
        reg.reset_policy()

    def test_every_registered_tool_has_a_valid_catalog_family(self):
        """No tool reaches the FastMCP registry without a catalog family in DEFAULT_TOOL_FAMILIES.

        A missing/invalid family means deny_reason() can never deny that tool (contracts/
        providers.md §6) — the Settings -> AI -> Tools family toggle is silently inert for it.
        """
        problems: list[str] = []
        for name in sorted(_TOOL_NAMES):
            entry = _CATALOG_SNAPSHOT.get(name)
            if entry is None:
                problems.append(f"  {name}: NO catalog entry (registered via bare mcp.tool(), "
                                 "not tool_registry.view(mcp, family).tool())")
                continue
            family = entry.get("family")
            if family not in DEFAULT_TOOL_FAMILIES:
                problems.append(f"  {name}: family {family!r} not in DEFAULT_TOOL_FAMILIES")
        self.assertFalse(
            problems,
            "Tools whose Settings -> AI -> Tools family toggle is inert:\n"
            + "\n".join(problems)
            + "\n\nFix: register through tool_registry.view(mcp, '<family>') in server.py "
              "with a family from services.agent.config.DEFAULT_TOOL_FAMILIES.",
        )

    def test_every_default_family_has_at_least_one_tool(self):
        """Every family in DEFAULT_TOOL_FAMILIES gates a real tool (catches phantom families)."""
        counts = {fam: 0 for fam in DEFAULT_TOOL_FAMILIES}
        for meta in _CATALOG_SNAPSHOT.values():
            fam = meta.get("family")
            if fam in counts:
                counts[fam] += 1
        phantom = sorted(fam for fam, n in counts.items() if n == 0)
        self.assertFalse(
            phantom,
            f"Families with ZERO registered tools (dead Settings -> AI -> Tools toggle, or "
            f"the family should be removed from DEFAULT_TOOL_FAMILIES): {phantom}",
        )

    def test_deny_reason_respects_a_disabled_family_for_a_real_tool(self):
        """Disabling a family in config actually denies a REAL tool tagged with it, and
        re-enabling it restores access — the exact behavior the Settings -> AI -> Tools
        per-family checkbox relies on."""
        cad_tools = sorted(
            name for name in _TOOL_NAMES
            if (_CATALOG_SNAPSHOT.get(name) or {}).get("family") == "cad"
        )
        self.assertTrue(cad_tools, "expected at least one real tool tagged family 'cad'")
        probe = cad_tools[0]

        default_opts = dict(
            enabled=True, timeout_s=None, max_calls_per_turn=None,
            param_defaults={}, prompt_note="",
        )
        with patch.dict(os.environ, {"AI_CAD_TOOLS_ALLOW": "", "AI_CAD_TOOLS_DENY_RISK": ""}), \
             patch.object(_cfg, "get_tool_options", lambda cfg, name: dict(default_opts)):
            # Baseline: family enabled (default) -> allowed.
            with patch.object(_cfg, "is_tool_family_enabled", lambda fam, cfg=None: True):
                self.assertIsNone(reg.deny_reason(probe))

            # Disable 'cad' -> the probe tool is denied, reason names it + its family.
            with patch.object(_cfg, "is_tool_family_enabled",
                               lambda fam, cfg=None: fam != "cad"):
                reason = reg.deny_reason(probe)
                self.assertIsNotNone(reason, f"expected {probe!r} to be denied with 'cad' off")
                self.assertIn(probe, reason)
                self.assertIn("cad", reason)

            # Re-enable 'cad' -> allowed again.
            with patch.object(_cfg, "is_tool_family_enabled", lambda fam, cfg=None: True):
                self.assertIsNone(reg.deny_reason(probe))


if __name__ == "__main__":
    # Also runnable as: python -m services.mcp.test_registration_catalog
    unittest.main(verbosity=2)
