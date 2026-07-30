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
     neither libchudware nor a running daemon). This runs every register_*_tools(...)
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

import json
import os
import pathlib
import subprocess
import sys
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

# Take the snapshot in a CLEAN SUBPROCESS. Importing here at module scope was meant
# to run before any other test could disturb the process-wide catalog, but pytest
# imports every test module during COLLECTION, so "before any test runs" is not
# early enough: if a sibling module imported services.mcp.server first and rebuilt
# the catalog at import time, `import services.mcp.server` here is a cache hit that
# re-registers nothing and the snapshot captures the polluted dict.
#
# Measured 2026-07-30: `pytest services/mcp/test_registration_catalog.py` passes,
# while `pytest services/agent services/dag services/mcp/test_registration_catalog.py`
# failed both consistency tests -- families 'files'/'shell'/'sim' apparently empty
# and unknown families like 'interaction' present. A clean import registers 418
# tools / 420 catalog entries. Verified pre-existing by re-running with that day's
# unrelated edits stashed. A subprocess cannot be reached by in-process ordering at
# all, which is the only way to make this file's premise actually true.
try:
    _snap = json.loads(subprocess.run(
        [sys.executable, "-c",
         "import json, services.mcp.server as s;"
         "from services.mcp import tool_registry as r;"
         "print(json.dumps({'tools': sorted(s.mcp._tool_manager._tools),"
         " 'catalog': r.catalog()}))"],
        cwd=str(pathlib.Path(__file__).resolve().parents[2]),
        capture_output=True, text=True, timeout=300, check=True,
    ).stdout.splitlines()[-1])
    _TOOL_NAMES = set(_snap["tools"])
    _CATALOG_SNAPSHOT = _snap["catalog"]
except Exception as exc:  # pragma: no cover - environment missing mcp/anyio packages
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
        with patch.dict(os.environ, {"CHUDWARE_TOOLS_ALLOW": "", "CHUDWARE_TOOLS_DENY_RISK": ""}), \
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


class TestWarmClearCatalogSemantics(unittest.TestCase):
    """clear_catalog() = clean room; warm_catalog() = reset-to-warm. Both must hold
    in the SAME process, in either order, however many times.

    Regression: warm_catalog() used to warm purely as a side effect of the FIRST
    ``import mcp_server.server``. Once anything imported server.py (this very module
    does, at collection time) the memo latched, so after a clear_catalog() the
    families only server.py registers — ascii2d, office, power_budget, heatsink,
    ecad_thermal — could never be restored, and every later caller in the process
    silently saw a short catalog (this broke services/agent/test_tool_guidance.py).
    """

    @classmethod
    def setUpClass(cls):
        if _IMPORT_ERROR is not None:
            raise unittest.SkipTest(f"services.mcp.server import failed: {_IMPORT_ERROR!r}")
        cls.office_tools = sorted(
            n for n, m in _CATALOG_SNAPSHOT.items() if m.get("family") == "office")
        if not cls.office_tools:
            raise unittest.SkipTest("no family 'office' tool in the post-import catalog")

    def tearDown(self):
        # Leave the process-wide catalog as this module found it, for sibling files.
        reg.clear_catalog()
        for name, meta in _CATALOG_SNAPSHOT.items():
            reg.record(name, meta.get("family"), meta.get("risk"))

    def test_clear_then_warm_restores_server_only_families(self):
        probe = self.office_tools[0]
        reg.clear_catalog()
        self.assertNotIn(probe, reg.catalog(),
                         "clear_catalog() must actually empty the catalog")

        reg.warm_catalog()
        cat = reg.catalog()
        self.assertIn(probe, cat,
                      "warm_catalog() after clear_catalog() did not restore the "
                      "server-only 'office' family — the one-shot-import landmine is back")
        self.assertEqual(cat[probe].get("family"), "office")
        families = {m.get("family") for m in cat.values()}
        for fam in ("office", "ecad", "gsl", "org"):
            self.assertIn(fam, families, f"family {fam!r} missing after clear+warm")

        # And it stays restored across a second warm (idempotent, not destructive).
        reg.warm_catalog()
        self.assertIn(probe, reg.catalog())

    def test_clear_catalog_is_a_genuine_clean_room(self):
        """A caller asking for a clean room gets ONLY the always-on meta-tools —
        clear_catalog() must never implicitly replay the warm baseline."""
        reg.clear_catalog()
        cat = reg.catalog()
        expected = {name for name, _fam, _risk in reg._META_TOOLS}
        self.assertEqual(set(cat), expected,
                         "clear_catalog() leaked non-meta entries into a clean room")

        # A clean room that then registers exactly one family sees exactly that family.
        reg.record("clean_room_probe", "cad", reg.RISK_READ)
        self.assertEqual({m["family"] for m in reg.catalog().values()},
                         {"workspace", "cad"})

    def test_org_family_is_registered_on_the_live_server(self):
        """The Settings -> AI -> Tools 'org' toggle governs real, registered tools."""
        for name in ("channel_send", "request_raise", "request_resolve", "council_convene"):
            self.assertIn(name, _TOOL_NAMES, f"{name} not on the FastMCP server")
            self.assertEqual((_CATALOG_SNAPSHOT.get(name) or {}).get("family"), "org",
                             f"{name} is not catalog-tagged family 'org'")


if __name__ == "__main__":
    # Also runnable as: python -m services.mcp.test_registration_catalog
    unittest.main(verbosity=2)
