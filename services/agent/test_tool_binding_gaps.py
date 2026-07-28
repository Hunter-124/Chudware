"""Regression tests for the three "advertised but impossible" binding gaps.

Each of these was measured against real agent runs (93 gym ledgers, replayed by
``tools/flywheel/replay_ledger_failures.py``), not imagined:

  * ``ask_user`` is in role cards everywhere but is ``async def`` + FastMCP
    ``Context`` over MCP, so the synchronous turn dispatcher could never bind it
    — 15 recorded failures, every single call, in every run without a mission.
  * a choice question written the way ``cad_guide('interaction')`` documents it
    (``options: ["PLA", …]``) failed validation, because only the ``choices``
    spelling coerced bare strings into ``{value,label}``.
  * models write ``add-features`` / ``mcp__chudware__add_features`` /
    ``cad-board/add_features``.  Folding punctuation leaves exactly one
    candidate, so that is a resolution, not a guess.
"""
from __future__ import annotations

import pytest

from services.agent import agent_turn as at
from services.agent.interaction import Question, _coerce_options


# --------------------------------------------------------------------------- #
# ask_user binds and answers honestly without a mission                        #
# --------------------------------------------------------------------------- #

class TestAskUserBinding:
    def test_ask_user_binds_outside_a_mission(self):
        assert at._bind_tool("ask_user") is not None, (
            "ask_user is advertised in role cards; an unbindable advertised tool "
            "fails 100% of calls")

    def test_plain_string_question_is_accepted(self):
        r = at._bind_tool("ask_user")("What material should the bracket be?")
        assert r["ok"] is True
        assert len(r["answers"]) == 1

    def test_answers_come_from_suggested_and_are_marked_default(self):
        """Never a fabricated human reply — contract §4."""
        r = at._bind_tool("ask_user")(questions=[
            {"id": "material", "kind": "choice", "prompt": "What material?",
             "suggested": "PLA", "options": ["PLA", "PETG", "aluminium 6061"]},
            {"id": "bore.dia", "kind": "dimension", "prompt": "Bore diameter?",
             "suggested": {"value": 8.0, "unit": "mm"}},
        ])
        assert r["ok"] is True
        assert r["answers"]["material"]["value"] == "PLA"
        assert r["answers"]["material"]["source"] == "default"
        assert r["answers"]["bore.dia"]["canonical"] == {"value": 8.0, "unit": "mm"}
        assert "default" in r["note"]

    def test_question_singular_alias_survives_kwarg_reconciliation(self):
        fn = at._bind_tool("ask_user")
        kwargs, _notes, err = at._reconcile_kwargs("ask_user", fn,
                                                   {"question": "Which face?"})
        assert err is None, "the singular spelling must not be rejected"
        assert fn(**kwargs)["ok"] is True

    def test_no_questions_is_a_teaching_error_with_an_example(self):
        r = at._bind_tool("ask_user")()
        assert r["ok"] is False
        assert r["status"] == "bad_arguments"
        assert r["example"], "an error that shows no correct shape teaches nothing"

    def test_doc_scope_persists_and_replays(self, ):
        """A doc-scoped answer must not be re-asked — contract §5."""
        new_part = at._bind_tool("new_part")
        if new_part is None:
            pytest.skip("engine unavailable")
        import json
        doc_id = json.loads(new_part(name="ask_user_scope")).get("doc_id")
        if not doc_id:
            pytest.skip("engine unavailable")
        fn = at._bind_tool("ask_user")
        q = [{"id": "finish", "kind": "text", "prompt": "Surface finish?",
              "suggested": "as-printed"}]
        first = fn(questions=q, scope="doc", doc_id=doc_id)
        second = fn(questions=q, scope="doc", doc_id=doc_id)
        assert first["answers"]["finish"]["source"] == "default"
        assert second["answers"]["finish"]["source"] == "resumed"


# --------------------------------------------------------------------------- #
# choice options: accept the documented shape                                  #
# --------------------------------------------------------------------------- #

class TestChoiceOptions:
    def test_bare_strings_under_options_are_coerced(self):
        q = Question.from_dict({"id": "m", "kind": "choice", "prompt": "?",
                                "suggested": "PLA", "options": ["PLA", "PETG"]})
        assert q.options == [{"value": "PLA", "label": "PLA"},
                             {"value": "PETG", "label": "PETG"}]

    def test_canonical_dicts_pass_through_unchanged(self):
        canonical = [{"value": "pla", "label": "PLA"}]
        assert _coerce_options(canonical) == canonical

    def test_choices_spelling_still_works(self):
        q = Question.from_dict({"id": "m", "kind": "choice", "prompt": "?",
                                "choices": ["a", "b"]})
        assert [o["value"] for o in q.options] == ["a", "b"]

    def test_single_entry_label_value_mapping(self):
        assert _coerce_options([{"Aluminium 6061": "al6061"}]) == [
            {"value": "al6061", "label": "Aluminium 6061"}]

    def test_absent_options_stay_none_so_choices_can_win(self):
        assert _coerce_options(None) is None


# --------------------------------------------------------------------------- #
# tool-name folding                                                            #
# --------------------------------------------------------------------------- #

class TestToolNameFolding:
    @pytest.mark.parametrize("written", [
        "add-features",
        "mcp__chudware__add_features",
        "cad-board/add_features",
        "Add_Features",
        " add features ",
    ])
    def test_punctuation_variants_fold_to_the_real_tool(self, written):
        assert at._canonical_tool_name(written) == "add_features"

    def test_an_exact_name_is_not_reported_as_a_rename(self):
        assert at._canonical_tool_name("add_features") is None

    def test_an_invented_name_is_not_forced_onto_a_neighbour(self):
        """Folding must never become fuzzy matching."""
        assert at._canonical_tool_name("edit-part") is None

    def test_concept_redirect_names_the_real_shape(self):
        err = at._unresolved_tool_error("edit-part")
        assert err["ok"] is False
        assert "add_features" in err["hint"]
        assert "edit_feature" in err["hint"]

    def test_suggestions_survive_hyphenation(self):
        """difflib scored hyphenated names against underscore names poorly."""
        err = at._unresolved_tool_error("describe-parts")
        assert "describe_part" in err["closest_tools"]

class TestClassAAdvertisedToolsBind:
    """Main's ledger forensics (2026-07-28, ``local://ledger-gaps.md``) flagged
    ``"tool 'X' could not be resolved"`` — 65+ occurrences across cycle-1-real-r2/
    r3/r4-cuda, local-1 and post-mega* bench batches — as the single highest-
    frequency agent-facing failure in the corpus. All of those ledgers predate
    2026-07-26 (commit 2b40e2cd, "bind 93 dead tools, teach bad args, widen
    ledger vocabulary"), which rewrote ``_bind_tool`` to fall back onto the
    real MCP server registry instead of ``reviewer_exec``'s hand-maintained
    module list. This class proves the fix still holds for every name in that
    report: a regression here means Class A silently reopened.
    """

    @pytest.mark.parametrize("name", [
        "ledger_get", "ledger_note", "ask_user", "dag_plan", "dag_status",
        "buildgraph_plan", "buildgraph_status", "describe_project",
        "describe_part", "part_search", "part_place", "fit_calc",
        "council_convene", "dag_replan",
    ])
    def test_advertised_tool_binds_outside_a_mission(self, name):
        assert at._bind_tool(name) is not None, (
            f"{name!r} is advertised (role card / MCP registry) but "
            f"_bind_tool returned None — Class A has reopened for this name")

    def test_delegate_is_correctly_diagnosed_as_mission_scoped_not_dead(self):
        """``delegate`` is the one name in the report that genuinely does NOT
        bind outside a mission — it lives on ``MissionDispatch`` (needs a
        live ledger/run_id/delegation tree), not the MCP server registry.
        That is not a dead advertisement: the error must say so precisely,
        not hand back the old bare "could not be resolved"."""
        assert at._bind_tool("delegate") is None
        assert "delegate" in at._known_tool_names()
        err = at._unresolved_tool_error("delegate")
        assert err["status"] == "known_but_unbound"
        assert err["harness_bug"] is True
        assert "mission" in err["error"].lower()
        assert "could not be resolved" in err["error"]
