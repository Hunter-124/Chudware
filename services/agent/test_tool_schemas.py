"""Tests for tool_schemas.py — trimmed schema rendering for the §9.1 tools slot.

DoD coverage: turn prompts carry trimmed schemas for exactly the allowed tools;
token cost is MEASURED (same 4-chars/token stick as tiers.TurnAssembler) and
budget overflow omits tools honestly instead of blowing the slot."""
from __future__ import annotations

from services.agent.tiers import _count_tokens
from services.agent.tool_schemas import (
    DEFAULT_TOOLS_BUDGET_TOKENS, describe_callable, render_tools_block)


def _add_features(ops: list, doc_id: str = "", background: bool = False) -> dict:
    """Apply a list of document ops to build/edit the part. Second sentence with
    much more detail that a trimmed tier must not pay for.

    A whole further paragraph of prose."""
    return {}


def _no_doc(x: int) -> dict:
    return {}


class TestDescribeCallable:
    def test_signature_carries_kwargs_types_defaults(self):
        s = describe_callable("add_features", _add_features)
        assert s["name"] == "add_features"
        assert "ops: list" in s["signature"]
        assert "doc_id: str = ''" in s["signature"]
        assert "background: bool = False" in s["signature"]

    def test_never_raises_on_unsignaturable(self):
        s = describe_callable("weird", print)  # builtins may lack a signature
        assert s["name"] == "weird"


class TestRenderToolsBlock:
    def _resolver(self, tool):
        return {"add_features": _add_features, "no_doc": _no_doc}.get(tool)

    def test_exactly_the_allowed_tools_rendered(self):
        text, meta = render_tools_block(
            ["add_features", "no_doc"], resolver=self._resolver)
        assert meta["rendered"] == ["add_features", "no_doc"]
        assert "add_features(ops: list" in text
        assert "no_doc(x: int)" in text

    def test_trim_keeps_first_sentence_only(self):
        text, _ = render_tools_block(["add_features"], resolver=self._resolver,
                                     trim=True, expand=False)
        assert "Apply a list of document ops" in text
        assert "Second sentence" not in text

    def test_untrimmed_keeps_first_paragraph(self):
        text, _ = render_tools_block(["add_features"], resolver=self._resolver,
                                     trim=False, expand=False)
        assert "Second sentence" in text
        assert "whole further paragraph" not in text

    def test_expand_spends_leftover_budget_on_full_docstring(self):
        # The floor discards everything past the first paragraph. When the tier's
        # own budget has room, the authored guidance below it is what teaches the
        # model the call shape — it must reach the prompt.
        floor, _ = render_tools_block(["add_features"], resolver=self._resolver,
                                      trim=False, expand=False)
        full, meta = render_tools_block(["add_features"], resolver=self._resolver,
                                        trim=False)
        assert "whole further paragraph" not in floor
        assert "whole further paragraph" in full
        assert meta["expanded"] == ["add_features"]

    def test_expansion_never_exceeds_budget(self):
        for budget in (30, 60, 120, 2500):
            text, meta = render_tools_block(
                ["add_features", "no_doc"], resolver=self._resolver,
                budget_tokens=budget, include_category_menu=False)
            assert meta["tokens"] <= budget, (budget, meta)
            assert _count_tokens(text) <= budget

    def test_expansion_never_costs_a_tool_its_slot(self):
        # A tool's prose must never push a DIFFERENT tool out of the block: the
        # floor pass reserves every tool first, expansion only spends what is left.
        _, floor = render_tools_block(
            ["add_features", "no_doc"], resolver=self._resolver,
            budget_tokens=80, include_category_menu=False, expand=False)
        _, grown = render_tools_block(
            ["add_features", "no_doc"], resolver=self._resolver,
            budget_tokens=80, include_category_menu=False)
        assert grown["rendered"] == floor["rendered"]
        assert grown["omitted"] == floor["omitted"]

    def test_expanded_block_keeps_the_slot_indent(self):
        # Full docstrings keep their line breaks (worked examples depend on them),
        # so every carried line must still sit inside the block frame.
        text, meta = render_tools_block(["add_features"], resolver=self._resolver,
                                        trim=False, include_category_menu=False)
        assert meta["expanded"] == ["add_features"]
        for ln in text.splitlines()[1:]:
            assert ln.startswith("- ") or ln.startswith("    "), repr(ln)

    def test_nothing_to_expand_is_not_reported_as_expanded(self):
        text, meta = render_tools_block(["no_doc"], resolver=self._resolver,
                                        include_category_menu=False)
        assert meta["expanded"] == []

    def test_tier_supplies_budget_and_trim(self):
        # Every production call site omitted both, so every run silently used the
        # local-24k fallback — frontier's own 10000-token allocation never applied.
        _, front = render_tools_block(["add_features"], resolver=self._resolver,
                                      tier="frontier")
        _, local = render_tools_block(["add_features"], resolver=self._resolver,
                                      tier="local-24k")
        assert front["budget_tokens"] == 10000
        assert front["trimmed"] is False
        assert local["budget_tokens"] == DEFAULT_TOOLS_BUDGET_TOKENS
        assert local["trimmed"] is True

    def test_explicit_args_beat_the_tier(self):
        _, meta = render_tools_block(["add_features"], resolver=self._resolver,
                                     tier="frontier", budget_tokens=777, trim=True)
        assert meta["budget_tokens"] == 777
        assert meta["trimmed"] is True

    def test_unknown_tier_falls_back_instead_of_raising(self):
        for bogus in ("", "nope", None):
            _, meta = render_tools_block(["add_features"], resolver=self._resolver,
                                         tier=bogus)
            assert meta["budget_tokens"] == DEFAULT_TOOLS_BUDGET_TOKENS, bogus

    def test_parameterized_provider_tiers_use_frontier_schema_budget(self):
        for tier in (
            "xai:grok-4.5",
            "openrouter:anthropic/claude-sonnet-4",
        ):
            _, meta = render_tools_block(
                ["add_features"], resolver=self._resolver, tier=tier
            )
            assert meta["budget_tokens"] == 10000
            assert meta["trimmed"] is False

    def test_token_cost_is_measured_not_invented(self):
        text, meta = render_tools_block(["add_features"], resolver=self._resolver)
        assert meta["tokens"] == _count_tokens(text)
        assert meta["budget_tokens"] == DEFAULT_TOOLS_BUDGET_TOKENS

    def test_budget_overflow_omits_honestly(self):
        text, meta = render_tools_block(
            ["add_features", "no_doc"], resolver=self._resolver, budget_tokens=30)
        assert meta["omitted"], meta
        assert meta["tokens"] <= 30
        for name in meta["omitted"]:
            assert f"{name}(" not in text

    def test_unresolved_tool_listed_name_only(self):
        text, meta = render_tools_block(["ghost_tool"], resolver=self._resolver)
        assert meta["unresolved"] == ["ghost_tool"]
        assert "ghost_tool(…)  # schema unavailable" in text

    def test_empty_allowlist_renders_nothing(self):
        text, meta = render_tools_block([], resolver=self._resolver)
        assert text == "" and meta["tokens"] == 0

    def test_broken_resolver_is_unresolved_not_crash(self):
        def _bad(tool):
            raise RuntimeError("registry down")
        text, meta = render_tools_block(["add_features"], resolver=_bad)
        assert meta["unresolved"] == ["add_features"]

    def test_pseudo_tools_have_fallback_schemas(self):
        # Advertised pseudo-tools must never render "# schema unavailable".
        for pseudo in ("delegate", "enable_tools", "list_tool_categories",
                       "design_recipe"):
            text, meta = render_tools_block(
                [pseudo], resolver=lambda _t: None, include_category_menu=False)
            assert meta["unresolved"] == [], (pseudo, meta)
            assert f"{pseudo}(" in text
            assert "schema unavailable" not in text

    def test_coordinate_claim_advertises_only_callable_release_name(self):
        from services.agent.mission_dispatch import MissionDispatch

        text, _ = render_tools_block(
            ["coordinate_claim"],
            resolver=lambda name: (
                MissionDispatch.coordinate_claim
                if name == "coordinate_claim"
                else None
            ),
            budget_tokens=2000,
            trim=False,
            include_category_menu=False,
        )
        assert "coordinate_release" in text
        assert "release_claim" not in text

    def test_multiline_paragraph_cannot_break_block_indent(self):
        def _multi() -> dict:
            """Plan a mission: validate the deliverable manifest and compile the
            role-assigned mission DAG (does NOT execute)."""
            return {}

        text, _ = render_tools_block(
            ["mission_start"],
            resolver=lambda n: _multi if n == "mission_start" else None,
            trim=False, include_category_menu=False)
        body_lines = [ln for ln in text.splitlines()[1:] if ln]
        for ln in body_lines:
            assert ln.startswith("- ") or ln.startswith("    "), ln
