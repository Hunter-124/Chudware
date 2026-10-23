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

    @staticmethod
    def _clear_doc_interaction(doc_id: str) -> None:
        """Drop any prior ``ask_user`` Q/A from the doc's intake meta record.

        Two correct behaviours conspire against a naive re-run of this test:
        ``new_part`` RESUMES an already-open part of the same name instead of
        minting a duplicate, and doc-scoped answers persist inside the document
        (contract §5 cross-session no-re-ask).  So against a live
        daemon the second run of this test inherits the first run's answer and
        sees ``resumed`` on the very FIRST ask.  Clear the record so the two
        asks below are genuinely the first and the second.
        """
        from services.mcp import core_bridge as cb
        from services.mcp.intake_ledger import K_INTAKE
        state = cb.call("doc_get_meta", {"doc_id": doc_id, "key": K_INTAKE}).get("value")
        if not isinstance(state, dict) or "interaction" not in state:
            return
        state.pop("interaction")
        cb.call("doc_set_meta", {"doc_id": doc_id, "key": K_INTAKE, "value": state})

    def test_doc_scope_persists_and_replays(self):
        """A doc-scoped answer must not be re-asked — contract §5."""
        new_part = at._bind_tool("new_part")
        if new_part is None:
            pytest.skip("engine unavailable")
        import json
        doc_id = json.loads(new_part(name="ask_user_scope")).get("doc_id")
        if not doc_id:
            pytest.skip("engine unavailable")
        self._clear_doc_interaction(doc_id)
        fn = at._bind_tool("ask_user")
        q = [{"id": "finish", "kind": "text", "prompt": "Surface finish?",
              "suggested": "as-printed"}]
        try:
            first = fn(questions=q, scope="doc", doc_id=doc_id)
            second = fn(questions=q, scope="doc", doc_id=doc_id)
            assert first["answers"]["finish"]["source"] == "default"
            assert second["answers"]["finish"]["source"] == "resumed"
        finally:
            self._clear_doc_interaction(doc_id)


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


# ---------------------------------------------------------------------------
# The harness must not INVENT a cause for an unbindable tool (live 2026-07-29)
# ---------------------------------------------------------------------------

def test_mission_scope_is_checked_against_the_real_dispatcher():
    """Mission-owned names answer True; ordinary MCP tools answer False.

    The claim used to be unconditional, so every unbindable tool was reported as
    mission-scoped. This pins the two sides apart against MissionDispatch's own
    handler table rather than a hardcoded list.
    """
    assert at._is_mission_scoped_tool("delegate") is True
    assert at._is_mission_scoped_tool("ask_user") is True
    assert at._is_mission_scoped_tool("new_part") is False
    assert at._is_mission_scoped_tool("import_part") is False


def test_empty_registry_names_the_import_error_not_mission_scope(monkeypatch):
    """A poisoned registry must say so, and quote the import failure.

    Live 2026-07-29 (drawing sweep): new_part, list_parts and import_part were all
    reported as "a mission-scoped tool" while the real cause was a tool module that
    briefly failed to import. That message sends an engineer to mission plumbing and
    away from the actual break, which is worse than saying "cause unknown".
    """
    monkeypatch.setattr(at, "_server_tools", lambda: {})
    monkeypatch.setattr(at, "_SERVER_TOOL_IMPORT_ERROR", "ImportError: boom in cad_x_tools")

    err = at._unresolved_tool_error("new_part")
    assert err["status"] == "known_but_unbound"
    assert err["harness_bug"] is True
    msg = err["error"]
    assert "REGISTRY is empty" in msg, msg
    assert "boom in cad_x_tools" in msg, msg
    assert "not specific to 'new_part'" in msg, msg
    assert "mission-scoped" not in msg, msg          # the fabricated cause is gone


def test_registered_but_unadvertised_name_does_not_claim_mission_scope(monkeypatch):
    """A non-empty registry that simply lacks the name must not blame mission scope."""
    monkeypatch.setattr(at, "_server_tools", lambda: {"new_part": lambda **k: None})
    monkeypatch.setattr(at, "_known_tool_names", lambda: frozenset({"new_part", "ghost_tool"}))

    err = at._unresolved_tool_error("ghost_tool")
    assert err["status"] == "known_but_unbound"
    msg = err["error"]
    assert "mission-scoped" not in msg, msg
    assert "failed to import" in msg or "without ever being registered" in msg, msg


def test_a_failed_registry_import_is_never_cached(monkeypatch):
    """One transient import error must not empty the registry for the whole process.

    The old handler cached {} on failure, so every later _bind_tool returned None
    forever — which is how a momentary module break turned into a run-long outage.
    """
    monkeypatch.setattr(at, "_SERVER_TOOL_CACHE", None)
    calls = {"n": 0}
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def flaky(name, *a, **k):
        if name == "services.mcp.server":
            calls["n"] += 1
            if calls["n"] == 1:
                raise ImportError("transient")
        return real_import(name, *a, **k)

    monkeypatch.setitem(__builtins__.__dict__ if not isinstance(__builtins__, dict)
                        else __builtins__, "__import__", flaky)
    first = at._server_tools()
    assert first == {}, "the failure should yield an empty mapping"
    assert at._SERVER_TOOL_CACHE is None, "a FAILED import must not be cached"
    assert at._SERVER_TOOL_IMPORT_ERROR and "transient" in at._SERVER_TOOL_IMPORT_ERROR

    second = at._server_tools()          # import now succeeds
    assert second, "the retry must repopulate the registry"
    assert "new_part" in second


# ---------------------------------------------------------------------------
# verify_battery CHECK names are not tools, and are not harness bugs either
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "is_manifold", "eval_check", "round_trip", "groundedness",
    "dim_coverage", "layout_coverage", "unit_sanity", "req_join",
])
def test_battery_only_check_redirects_to_verify_battery(name):
    """These eight live in ``battery.CHECKS`` and nowhere in the tool registry.

    ``_known_tool_names()`` unions the check table in so an invented name can be
    told from a real one, which used to route every one of them into the
    "ADVERTISED to you but could not be resolved" branch with
    ``harness_bug: True``. That is wrong twice over: nothing advertises them as
    tools, and the capability is one ``verify_battery`` call away — so the model
    was told to stop trying, and the bench corpus collected a fake harness gap.
    """
    from services.verify.battery import CHECKS

    assert name in CHECKS and at._bind_tool(name) is None
    err = at._unresolved_tool_error(name)
    assert err["status"] == "unknown_tool"
    assert err.get("harness_bug") is None
    assert "could not be resolved" not in err["error"]
    assert f"checks=['{name}']" in err["hint"]
    assert "verify_battery" in err["error"]


def test_battery_check_alias_names_the_canonical_check():
    """``watertight``/``manifold`` are battery aliases for ``is_manifold``."""
    err = at._unresolved_tool_error("watertight")
    assert err["status"] == "unknown_tool"
    assert "is_manifold" in err["hint"]


def test_a_check_that_is_also_a_real_tool_is_left_alone():
    """``measure_part`` is both a battery check and a bindable tool.

    The redirect must never fire for those — it would talk a model out of a
    tool that works.
    """
    from services.verify.battery import CHECKS

    assert "measure_part" in CHECKS
    assert at._bind_tool("measure_part") is not None
    assert at._battery_only_check("measure_part") is None