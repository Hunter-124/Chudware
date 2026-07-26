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
