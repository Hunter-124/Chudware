"""Tests for tool_guidance — the built-in curated per-tool options layer.

DoD coverage: every guidance entry names a real registry tool (or a known
pseudo-tool), uses only the allowed option keys, and every shippable tool
carries a curated ``prompt_note`` — the "AI usage guidance" line the Tools
panel and the tools-slot prompt both render. Curated ``param_defaults`` are
validated against the live signatures: real parameter names, no no-op copies
of the signature's own default, JSON-scalar values, and inheritance through
``config.get_tool_options`` with the user overlay winning.
"""
from __future__ import annotations

import unittest

from services.agent.tool_guidance import (
    GUIDANCE_KEYS, PSEUDO_TOOLS, TOOL_GUIDANCE, guidance_for,
)


def _registry_names() -> set[str]:
    from services.mcp import tool_registry as tr

    tr.warm_catalog()
    return set((tr.catalog() or {}).keys())


# Frozen at import (collection) time: pytest imports every test module before
# running any test, so this snapshot predates in-test registry pollution
# (e.g. fake tools registered by other suites and never cleaned up).
_LIVE_NAMES = _registry_names()


class TestGuidanceEntries(unittest.TestCase):
    def test_only_allowed_keys(self):
        for name, entry in TOOL_GUIDANCE.items():
            extra = set(entry) - GUIDANCE_KEYS
            self.assertFalse(extra, f"{name}: disallowed keys {extra}")

    def test_every_entry_names_a_real_tool(self):
        known = _LIVE_NAMES | PSEUDO_TOOLS
        for name in TOOL_GUIDANCE:
            self.assertIn(name, known, f"guidance for unknown tool {name!r}")

    def test_every_tool_has_a_curated_note(self):
        missing = sorted(
            n for n in (_LIVE_NAMES | PSEUDO_TOOLS)
            if not str((TOOL_GUIDANCE.get(n) or {}).get("prompt_note") or "").strip()
        )
        self.assertEqual(
            missing, [],
            f"{len(missing)} tools without curated prompt_note: {missing[:10]} — "
            "add an entry for each in services/agent/tool_guidance/<family>.py "
            "(create the family module + register it in _FAMILIES if the family "
            "is new), then re-run tools/gen_tool_panel_tables.py")

    def test_notes_are_one_line(self):
        for name, entry in TOOL_GUIDANCE.items():
            note = str(entry.get("prompt_note") or "")
            self.assertNotIn("\n", note, f"{name}: prompt_note must be one line")

    def test_guidance_for_unknown_is_empty(self):
        self.assertEqual(guidance_for("no_such_tool_zzz"), {})


class TestDrawingRoleCoverageGate(unittest.TestCase):
    def test_drawing_detailer_keeps_audit_tool_within_role_cap(self):
        from services.agent import roles

        card = roles.load_role("drawing_detailer")
        self.assertLessEqual(len(card.tool_allowlist), roles.MAX_TOOLS_PER_ROLE)
        self.assertIn("audit_drawing", card.concrete_tools)



class TestManufacturingRoleWiring(unittest.TestCase):
    def test_manufacturing_planner_keeps_required_tools_within_role_cap(self):
        from services.agent import roles

        card = roles.load_role("manufacturing_planner")
        required = {
            "mfg_plan_upsert",
            "mfg_plan_get",
            "mfg_plan_check",
            "mfg_plan_render",
            "resolve_process",
            "set_process",
            "dfm_check",
            "fit_calc",
            "recommend_fit",
            "machining_ref_search",
            "research_thread_spec",
            "tap_drill",
            "feeds_speeds",
        }
        self.assertLessEqual(len(card.tool_allowlist), roles.MAX_TOOLS_PER_ROLE)
        self.assertTrue(required <= set(card.concrete_tools))

    def test_manufacturing_plan_consumers_read_plan_ledger_section(self):
        from services.agent import roles

        for role_id in (
            "spec_writer",
            "feature_modeler",
            "drawing_detailer",
            "cam_programmer",
            "fixer",
        ):
            with self.subTest(role=role_id):
                card = roles.load_role(role_id)
                self.assertIn("mfg_plans", card.ledger_sections["read"])

def _resolved_signature(name: str):
    """inspect.signature of the live callable for *name*, or None when the
    tool does not resolve in-process (pseudo-tools need a mission dispatcher)."""
    import inspect

    from services.agent import agent_turn as at

    fn = at.resolver_for(None)(name)
    if fn is None:
        return None
    try:
        return inspect.signature(fn)
    except (TypeError, ValueError):
        return None


class TestParamDefaults(unittest.TestCase):
    """Curated param_defaults must track the LIVE signatures: real parameter
    names, values that actually change behavior (no no-op copies of the
    signature's own default), and JSON-scalar shapes the panel can render."""

    # Tools with curated param_defaults that do not resolve in-process and are
    # therefore exempt from signature introspection. Keep empty: every
    # param_defaults entry should target a live registry callable.
    ALLOWED_SKIP: frozenset[str] = frozenset()

    def _defaults_entries(self) -> dict[str, dict]:
        return {
            n: e["param_defaults"]
            for n, e in TOOL_GUIDANCE.items()
            if isinstance(e.get("param_defaults"), dict) and e["param_defaults"]
        }

    def test_param_defaults_keys_are_real_parameters(self):
        entries = self._defaults_entries()
        self.assertTrue(entries, "expected at least one curated param_defaults entry")
        skipped = []
        for name, defaults in sorted(entries.items()):
            sig = _resolved_signature(name)
            if sig is None:
                skipped.append(name)
                continue
            params = sig.parameters
            has_var_kw = any(
                p.kind is p.VAR_KEYWORD for p in params.values()
            )
            for key in defaults:
                self.assertTrue(
                    key in params or has_var_kw,
                    f"{name}: param_defaults key {key!r} is not a parameter of "
                    f"the live signature {sig}",
                )
        self.assertLessEqual(
            len(skipped), len(self.ALLOWED_SKIP),
            f"unresolved tools with param_defaults grew: {skipped}",
        )
        self.assertEqual(
            sorted(set(skipped) - self.ALLOWED_SKIP), [],
            f"param_defaults on tools that do not resolve in-process: {skipped} "
            "— name them in ALLOWED_SKIP with a reason or drop the entry",
        )

    def test_param_defaults_are_not_noops(self):
        import inspect

        for name, defaults in sorted(self._defaults_entries().items()):
            sig = _resolved_signature(name)
            if sig is None:
                continue
            for key, value in defaults.items():
                param = sig.parameters.get(key)
                if param is None or param.default is inspect.Parameter.empty:
                    continue
                self.assertNotEqual(
                    value, param.default,
                    f"{name}.{key}: curated default {value!r} equals the "
                    f"signature's own default — a no-op entry that only "
                    f"clutters the panel",
                )

    def test_param_defaults_are_json_scalars(self):
        import json

        def _check(value, where):
            self.assertNotIsInstance(
                value, (dict, set, tuple),
                f"{where}: objects/tuples are not allowed in param_defaults",
            )
            if isinstance(value, list):
                for i, item in enumerate(value):
                    _check(item, f"{where}[{i}]")
            else:
                self.assertIsInstance(
                    value, (str, int, float, bool, type(None)),
                    f"{where}: not a JSON scalar: {value!r}",
                )

        for name, defaults in sorted(self._defaults_entries().items()):
            for key, value in defaults.items():
                _check(value, f"{name}.{key}")
            json.dumps(defaults)  # raises if anything is non-serializable

    def test_get_tool_options_inherits_curated_defaults(self):
        from services.agent import config

        for name, defaults in sorted(self._defaults_entries().items()):
            merged = config.get_tool_options({}, name)
            self.assertEqual(
                merged["param_defaults"], defaults,
                f"{name}: get_tool_options did not inherit the curated "
                f"param_defaults layer",
            )

    def test_user_overlay_still_wins(self):
        from services.agent import config

        entries = self._defaults_entries()
        name = sorted(entries)[0]
        key = sorted(entries[name])[0]
        cfg = {"agent": {"tool_options": {name: {"param_defaults": {key: "user-value"}}}}}
        merged = config.get_tool_options(cfg, name)
        self.assertEqual(
            merged["param_defaults"].get(key), "user-value",
            "user config.json param_defaults must override the curated layer",
        )


class TestPanelTablesSnapshot(unittest.TestCase):
    """host/src/ui/tool_panel_tables.snapshot.json must match the LIVE registry
    — re-run `tools/gen_tool_panel_tables.py` when tools are added/renamed."""

    def test_snapshot_matches_live_registry(self):
        import json
        from pathlib import Path

        snap_path = (
            Path(__file__).resolve().parents[2]
            / "host/src/ui/tool_panel_tables.snapshot.json"
        )
        if not snap_path.exists():
            self.fail(f"panel snapshot missing: {snap_path} — run "
                      "tools/gen_tool_panel_tables.py")
        snap = json.loads(snap_path.read_text())
        live = _LIVE_NAMES | PSEUDO_TOOLS
        snap_tools = {n for names in snap.get("fam_tools", {}).values() for n in names}
        missing = sorted(live - snap_tools)
        extra = sorted(snap_tools - live)
        self.assertEqual(
            (missing, extra), ([], []),
            "panel tables drifted — re-run tools/gen_tool_panel_tables.py and "
            "add prompt_note entries for new tools in "
            "services/agent/tool_guidance/<family>.py")


if __name__ == "__main__":
    unittest.main()
