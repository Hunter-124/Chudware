"""WI-16 snapshot tests — per-capability SOFT phrasing blocks.

Acceptance (harness-tiers-spec.md WI-16):
  * A system text contains calc-before-model (and datum-first / fillet-last).
  * C system text contains NEITHER the rigid A checklist NOR 'after every mutation'.
  * Assembly is keyed off resolve_capability / letter at prompt-build time.
  * C prefers MCP lite instruction profile.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from services.agent import prompt_tiers as pt
from services.agent import roles
from services.agent.capability import profile_for_letter, resolve_capability


_ROLES = ("feature_modeler", "geometry_designer", "sketcher")
_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


class TestSoftBlocks(unittest.TestCase):
    def test_soft_a_has_calc_before_model(self):
        text = pt.soft_phrasing("A")
        self.assertIn("calculate-before-model", text)
        for snip in pt.A_REQUIRED_SNIPPETS:
            self.assertIn(snip, text, f"A soft block missing {snip!r}")
        self.assertIn("after every mutation", text)
        self.assertIn("use templates", text)
        self.assertIn(pt.SOFT_BLOCK_MARKER, text)

    def test_soft_b_is_suggestive(self):
        text = pt.soft_phrasing("B")
        self.assertIn("RECOMMENDED", text)
        self.assertIn("feature groups", text)
        self.assertIn("preflight", text.lower())
        self.assertNotIn("calculate-before-model", text)
        self.assertNotIn("after every mutation", text)

    def test_soft_c_is_minimal_hard_floor_mirror(self):
        text = pt.soft_phrasing("C")
        self.assertIn("verify_battery", text)
        self.assertIn("checkpoint", text.lower())
        for snip in pt.C_FORBIDDEN_SNIPPETS:
            self.assertNotIn(snip, text, f"C soft block must not contain {snip!r}")

    def test_unknown_letter_defaults_to_c(self):
        self.assertEqual(pt.normalize_letter("Z"), "C")
        self.assertEqual(pt.soft_phrasing(None), pt.soft_phrasing("C"))
        self.assertEqual(pt.soft_phrasing(""), pt.soft_phrasing("C"))


class TestAssembleSystem(unittest.TestCase):
    def test_a_system_contains_calc_before_model(self):
        """WI-16 acceptance: A system text contains calc-before-model."""
        card = roles.load_role("feature_modeler")
        system = roles.system_prompt_for_capability(card, "A")
        self.assertIn("calculate-before-model", system)
        self.assertIn(pt.SOFT_BLOCK_MARKER, system)
        # Base role prose is still present.
        self.assertIn("FEATURE MODELER", system)

    def test_c_system_excludes_rigid_and_per_mutation(self):
        """WI-16 acceptance: C system has neither rigid checklist nor 'after every mutation'."""
        card = roles.load_role("feature_modeler")
        system = roles.system_prompt_for_capability(card, "C")
        self.assertIn(pt.SOFT_BLOCK_MARKER, system)
        self.assertIn("verify_battery", system)
        for snip in pt.C_FORBIDDEN_SNIPPETS:
            self.assertNotIn(snip, system, f"C system must not contain {snip!r}")

    def test_assemble_context_capability_kwarg(self):
        card = roles.load_role("sketcher")
        ctx_a = roles.assemble_context(card, capability="A", ledger_render="§LEDGER§")
        ctx_c = roles.assemble_context(card, capability="C", ledger_render="§LEDGER§")
        self.assertIn("calculate-before-model", ctx_a)
        self.assertNotIn("calculate-before-model", ctx_c)
        self.assertIn("§LEDGER§", ctx_a)
        self.assertIn("§LEDGER§", ctx_c)
        # Soft block precedes ledger (§9.1 order still holds).
        self.assertLess(ctx_a.index(pt.SOFT_BLOCK_MARKER), ctx_a.index("§LEDGER§"))

    def test_default_capability_is_c_freedom(self):
        """Default (None) must be C so frontier is never taxed by accident."""
        card = roles.load_role("feature_modeler")
        default = roles.system_prompt_for_capability(card, None)
        explicit_c = roles.system_prompt_for_capability(card, "C")
        self.assertEqual(default, explicit_c)
        self.assertNotIn("after every mutation", default)

    def test_capability_profile_object(self):
        card = roles.load_role("geometry_designer")
        prof_a = profile_for_letter("A")
        prof_c = profile_for_letter("C")
        sys_a = roles.system_prompt_for_capability(card, prof_a)
        sys_c = roles.system_prompt_for_capability(card, prof_c)
        self.assertIn("calculate-before-model", sys_a)
        self.assertNotIn("calculate-before-model", sys_c)

    def test_resolve_capability_local_is_a(self):
        """Assembly keyed off resolve_capability: local-24k → A phrasing."""
        card = roles.load_role("feature_modeler")
        cap = resolve_capability("local-24k")
        self.assertEqual(cap.letter, "A")
        system = roles.system_prompt_for_capability(card, cap)
        self.assertIn("calculate-before-model", system)

    def test_resolve_capability_frontier_is_c(self):
        card = roles.load_role("feature_modeler")
        cap = resolve_capability("frontier")
        self.assertEqual(cap.letter, "C")
        system = roles.system_prompt_for_capability(card, cap)
        self.assertNotIn("after every mutation", system)
        self.assertNotIn("calculate-before-model", system)

    def test_all_modeling_roles_a_vs_c(self):
        for role_id in _ROLES:
            card = roles.load_role(role_id)
            a = roles.system_prompt_for_capability(card, "A")
            c = roles.system_prompt_for_capability(card, "C")
            self.assertIn("calculate-before-model", a, role_id)
            self.assertNotIn("after every mutation", c, role_id)
            self.assertNotIn("calculate-before-model", c, role_id)


class TestBaseRoleProseFreedom(unittest.TestCase):
    """Base role YAML must not embed A-only mandatory phrases that would tax C."""

    def test_feature_modeler_base_no_after_every_mutation(self):
        card = roles.load_role("feature_modeler")
        # Base card alone (no soft append) — C freedom depends on this.
        self.assertNotIn("after every mutation", card.system_prompt)
        self.assertNotIn("after every mutation", card.goal)

    def test_geometry_designer_base_mentions_soft_hook(self):
        card = roles.load_role("geometry_designer")
        self.assertIn("Capability SOFT", card.system_prompt)


class TestMcpProfilePreference(unittest.TestCase):
    def test_c_prefers_lite(self):
        self.assertEqual(pt.mcp_profile_for_letter("C"), "lite")
        self.assertEqual(pt.mcp_profile_for_letter("A"), "full")
        self.assertEqual(pt.mcp_profile_for_letter("B"), "full")

    def test_mcp_soft_tails_present_in_server_source(self):
        """MCP instruction blocks carry SOFT tails and C-prefer-lite selection."""
        server = (Path(__file__).resolve().parents[1] / "mcp" / "server.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("[CAPABILITY_SOFT]", server)
        self.assertIn("calculate-before-model", server)
        self.assertIn("_resolve_mcp_profile", server)
        self.assertIn("Prefer verify_battery before done", server)


class TestPromptMdHooks(unittest.TestCase):
    def test_modeler_executor_reviewer_mention_capability_soft(self):
        for name in ("modeler.md", "executor.md", "reviewer.md"):
            text = (_PROMPTS_DIR / name).read_text(encoding="utf-8")
            self.assertIn("CAPABILITY_SOFT", text, name)


class TestAppendIdempotent(unittest.TestCase):
    def test_reappend_replaces_not_duplicates(self):
        base = "You are the MODELER."
        once = pt.append_soft_block(base, "A")
        twice = pt.append_soft_block(once, "A")
        self.assertEqual(once.count(pt.SOFT_BLOCK_MARKER), 1)
        self.assertEqual(twice.count(pt.SOFT_BLOCK_MARKER), 1)
        # Switch letter replaces.
        switched = pt.append_soft_block(once, "C")
        self.assertEqual(switched.count(pt.SOFT_BLOCK_MARKER), 1)
        self.assertNotIn("calculate-before-model", switched)
        self.assertIn("verify_battery", switched)


if __name__ == "__main__":
    unittest.main()
