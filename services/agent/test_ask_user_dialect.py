"""Tests for the ask_user argument dialect: ONE shared normalizer + teaching
rejection (services.agent.interaction) used by every entry point -
MissionDispatch.ask_user, agent_turn._ask_user_sync, and the MCP `ask_user`
tool (services.mcp.interaction_tools). Regression coverage for the live
desktop-run failures (`ask_user got no question text`, `{}` x4 until the
loop guard killed the run) and a behavioral check that every entry point
gives the same verdict on the same live payload.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from services.agent import agent_turn, interaction
from services.agent.interaction import (
    ask_user_bad_arguments_error, normalize_questions,
)
from services.agent.ledger import Ledger
from services.agent.mission_dispatch import MissionDispatch


@pytest.fixture()
def cache_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CHUDWARE_CACHE_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture()
def ledger(tmp_path):
    return Ledger("ask_user_dialect_run", path=tmp_path / "ledger.json")


@pytest.fixture()
def md(ledger, cache_root):
    return MissionDispatch(run_id="ask_user_dialect_run", ledger=ledger)


# Verbatim (trimmed) live payload, run-73073258489640 (claude-sonnet-5,
# 2026-07-28): every item carried its text under `question`, not `prompt`.
LIVE_QUESTION_KEY_PAYLOAD = [
    {"id": "purpose",
     "question": ("This project (489-final) already contains a part named "
                  "CD-1101_MudBucket_Wheel.step+ which looks like a go-kart "
                  "wheel, not an oil-rig mud bucket. New standalone part?"),
     "type": "choice",
     "options": ["Yes, new standalone oilfield mud bucket part",
                 "No, I actually meant something related to CD-1101_MudBucket_Wheel"],
     "suggested": "Yes, new standalone oilfield mud bucket part"},
    {"id": "dims", "question": "What size/capacity should the mud bucket be?"},
]


# ---------------------------------------------------------------------------
# accepted shapes — normalize_questions is the single source of truth
# ---------------------------------------------------------------------------

ACCEPTED_SHAPES = [
    pytest.param("Which material?", 1, id="bare-string"),
    pytest.param(["Q1?", "Q2?"], 2, id="list-of-strings"),
    pytest.param({"id": "q1", "prompt": "Material?"}, 1, id="single-dict"),
    pytest.param([{"id": "q1", "prompt": "Material?"}], 1, id="list-of-one-dict"),
    pytest.param(LIVE_QUESTION_KEY_PAYLOAD, 2, id="live-question-key-payload"),
    pytest.param([{"prompt": "q"}] * 9, 4, id="caps-at-contract-four"),
]


class TestAcceptedShapes:
    @pytest.mark.parametrize("raw, expected_count", ACCEPTED_SHAPES)
    def test_accepted_and_normalized(self, raw, expected_count):
        qs = normalize_questions(raw)
        assert len(qs) == expected_count
        for q in qs:
            assert str(q["prompt"]).strip()
            assert q["id"]
            assert q["kind"] in interaction.KINDS

    def test_live_question_key_payload_prompts_survive(self):
        """The `question` key must map onto `prompt`, not vanish — this exact
        shape produced 'ask_user got no question text' on a real desktop run
        before every ask_user entry point shared this normalizer."""
        qs = normalize_questions(LIVE_QUESTION_KEY_PAYLOAD)
        assert [q["id"] for q in qs] == ["purpose", "dims"]
        assert qs[0]["prompt"].startswith("This project (489-final)")
        assert qs[0]["kind"] == "choice"
        assert qs[1]["prompt"] == "What size/capacity should the mud bucket be?"

    def test_choice_with_bare_string_options_defaults_suggested(self):
        """A choice question with plain-string options (not {value,label}
        dicts) and no suggested must get an in-range default, not ''."""
        qs = normalize_questions([{"id": "q1", "prompt": "Pick one",
                                   "kind": "choice", "options": ["a", "b"]}])
        assert qs[0]["suggested"] == "a"

    def test_end_to_end_through_mission_dispatch(self, md):
        out = md.ask_user(questions=LIVE_QUESTION_KEY_PAYLOAD)
        assert out["ok"] is True
        assert set(out["answers"]) == {"purpose", "dims"}

    def test_end_to_end_through_agent_turn_bridge(self):
        out = agent_turn._ask_user_sync(questions=LIVE_QUESTION_KEY_PAYLOAD)
        assert out["ok"] is True
        assert set(out["answers"]) == {"purpose", "dims"}

    def test_end_to_end_through_mcp_tool(self, cache_root):
        raw = asyncio.run(_call_mcp_ask_user(
            questions=LIVE_QUESTION_KEY_PAYLOAD, blocking=False, run_id="r-live"))
        answers = json.loads(raw)
        assert set(answers) == {"purpose", "dims"}

    def test_mcp_tool_accepts_bare_string(self, cache_root):
        raw = asyncio.run(_call_mcp_ask_user(
            questions="Which material?", blocking=False, run_id="r-str"))
        answers = json.loads(raw)
        assert len(answers) == 1

    def test_mcp_tool_accepts_top_level_prompt_alias(self, cache_root):
        raw = asyncio.run(_call_mcp_ask_user(
            prompt="Which color?", blocking=False, run_id="r-alias"))
        answers = json.loads(raw)
        assert len(answers) == 1


# ---------------------------------------------------------------------------
# genuinely empty calls -> teaching rejection naming the corrected call
# ---------------------------------------------------------------------------

EMPTY_SHAPES = [
    pytest.param(None, id="none"),
    pytest.param({}, id="empty-dict"),
    pytest.param([], id="empty-list"),
    pytest.param("", id="empty-string"),
    pytest.param([""], id="list-of-empty-string"),
    pytest.param([{"id": "q1"}], id="dict-with-no-prompt-text"),
]


class TestBadArgumentsTeaching:
    @pytest.mark.parametrize("raw", EMPTY_SHAPES)
    def test_normalizer_yields_nothing_usable(self, raw):
        norm = [q for q in normalize_questions(raw) if str(q.get("prompt") or "").strip()]
        assert norm == []

    def test_teaching_error_names_the_corrected_call(self):
        err = ask_user_bad_arguments_error()
        assert err["ok"] is False
        assert err["status"] == "bad_arguments"
        assert ('ask_user(questions=[{"id": "q1", "prompt": "Which material?", '
                '"suggested": "AL_6061_T6"}])') in err["error"]
        assert 'ask_user(questions="Which material?")' in err["error"]
        assert err["example"][0]["prompt"]

    def test_mission_dispatch_ask_user_empty_call(self, md):
        assert md.ask_user() == ask_user_bad_arguments_error()

    def test_agent_turn_ask_user_sync_empty_call(self):
        assert agent_turn._ask_user_sync() == ask_user_bad_arguments_error()

    def test_mcp_ask_user_empty_call(self, cache_root):
        raw = asyncio.run(_call_mcp_ask_user(questions=None, blocking=False, run_id="r-empty"))
        assert json.loads(raw) == ask_user_bad_arguments_error()

    def test_mcp_ask_user_empty_dict_call(self, cache_root):
        """The exact recorded live failure: ask_user({}) x4 (run-56153804024698)."""
        raw = asyncio.run(_call_mcp_ask_user(questions={}, blocking=False, run_id="r-empty2"))
        assert json.loads(raw) == ask_user_bad_arguments_error()


# ---------------------------------------------------------------------------
# every ask_user entry point gives the same verdict
# ---------------------------------------------------------------------------

class TestEntryPointsAgree:
    def test_all_entry_points_agree_on_the_live_regression_payload(self, md):
        """Same payload, same ids, same rejection/acceptance verdict everywhere."""
        direct_ids = {q["id"] for q in normalize_questions(LIVE_QUESTION_KEY_PAYLOAD)}

        via_mission = md.ask_user(questions=LIVE_QUESTION_KEY_PAYLOAD)
        assert set(via_mission["answers"]) == direct_ids

        via_agent_turn = agent_turn._ask_user_sync(questions=LIVE_QUESTION_KEY_PAYLOAD)
        assert set(via_agent_turn["answers"]) == direct_ids


async def _call_mcp_ask_user(**kwargs):
    from mcp.server.fastmcp import FastMCP
    from services.mcp import interaction_tools

    mcp = FastMCP("t")
    interaction_tools.register_interaction_tools(mcp)
    fn = mcp._tool_manager._tools["ask_user"].fn

    class _NoClientCtx:
        async def elicit(self, **_kw):
            raise RuntimeError("no elicitation client in test")

    return await fn(ctx=_NoClientCtx(), **kwargs)
