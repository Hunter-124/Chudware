"""Tests for services/agent/interaction.py against contracts/interaction.md.

Covers: unit normalization (§6), the three transports (§8) round-tripping typed
answers, persistence + replay on resume (§5), autonomy defaulting, contract-error
validation (§2/§3/§10), and the persist-then-callback event ordering (§7).
"""
from __future__ import annotations

import io
import json
import math

import pytest

from agent import interaction
from agent.interaction import (
    InAppTransport,
    InteractionError,
    MCPElicitationTransport,
    CLITransport,
    Question,
    ask_user,
    normalize_dimension,
)


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Every ask_user() call in this module may construct a run-scoped FileLedger;
    redirect it under tmp_path so nothing ever touches the real ~/.local/share."""
    monkeypatch.setenv("AICAD_CACHE_ROOT", str(tmp_path))
    yield


# --------------------------------------------------------------------------- #
# 1. normalize_dimension                                                      #
# --------------------------------------------------------------------------- #

def test_normalize_dimension_in_to_mm():
    assert normalize_dimension(0.5, "in") == {"value": 12.7, "unit": "mm"}


def test_normalize_dimension_mil_to_mm():
    result = normalize_dimension(1000, "mil")
    assert result["unit"] == "mm"
    assert result["value"] == pytest.approx(25.4)


def test_normalize_dimension_rad_to_deg():
    result = normalize_dimension(math.pi, "rad", "angle")
    assert result["unit"] == "deg"
    assert result["value"] == pytest.approx(180.0)


def test_normalize_dimension_count_passthrough():
    assert normalize_dimension(5, "", "count") == {"value": 5.0, "unit": "count"}
    assert normalize_dimension(5, "count", "count") == {"value": 5.0, "unit": "count"}


def test_normalize_dimension_angle_unit_on_length_family_raises():
    with pytest.raises(InteractionError):
        normalize_dimension(10, "deg", "length")


def test_normalize_dimension_unknown_unit_raises():
    with pytest.raises(InteractionError):
        normalize_dimension(10, "banana", "length")


# --------------------------------------------------------------------------- #
# 2. In-app transport round-trip                                              #
# --------------------------------------------------------------------------- #

def test_inapp_transport_round_trip():
    q_dim = Question(id="bore.dia", kind="dimension", prompt="Bore diameter?",
                      suggested={"value": 10.0, "unit": "mm"}, family="length")
    q_choice = Question(id="finish", kind="choice", prompt="Surface finish?",
                         suggested="as_machined",
                         options=[{"value": "as_machined"}, {"value": "anodized"}])

    def provider(batch_dict):
        assert batch_dict["schema"] == interaction.SCHEMA_BATCH
        return {"bore.dia": {"value": 0.5, "unit": "in"}, "finish": "anodized"}

    answers = ask_user([q_dim, q_choice], run_id="r_inapp", transport=InAppTransport(provider))

    assert answers["bore.dia"]["canonical"] == {"value": 12.7, "unit": "mm"}
    assert answers["bore.dia"]["source"] == "user"
    assert answers["finish"]["value"] == "anodized"
    assert answers["finish"]["source"] == "user"


# --------------------------------------------------------------------------- #
# 3. MCP elicitation round-trip + schema_for                                  #
# --------------------------------------------------------------------------- #

def test_mcp_elicitation_transport_round_trip():
    q_dim = Question(id="bore.dia", kind="dimension", prompt="Bore diameter?",
                      suggested={"value": 10.0, "unit": "mm"}, family="length")
    q_choice = Question(id="finish", kind="choice", prompt="Surface finish?",
                         suggested="as_machined",
                         options=[{"value": "as_machined"}, {"value": "anodized"}])

    def fake_elicit(message, schema):
        if "diameter" in message.lower():
            return {"value": 0.5, "unit": "in"}
        return {"value": "anodized"}

    transport = MCPElicitationTransport(elicit=fake_elicit)
    answers = ask_user([q_dim, q_choice], run_id="r_mcp", transport=transport)

    assert answers["bore.dia"]["canonical"] == {"value": 12.7, "unit": "mm"}
    assert answers["bore.dia"]["source"] == "user"
    assert answers["finish"]["value"] == "anodized"
    assert answers["finish"]["source"] == "user"


def test_mcp_elicitation_schema_for():
    q_dim = Question(id="d", kind="dimension", prompt="?", suggested={"value": 1, "unit": "mm"})
    schema = MCPElicitationTransport.schema_for(q_dim)
    assert schema["properties"]["value"]["type"] == "number"
    assert schema["properties"]["unit"]["type"] == "string"

    q_choice = Question(id="c", kind="choice", prompt="?", suggested="a",
                         options=[{"value": "a"}, {"value": "b"}])
    schema = MCPElicitationTransport.schema_for(q_choice)
    assert schema["properties"]["value"]["enum"] == ["a", "b"]

    q_appr = Question(id="ap", kind="approval", prompt="?", suggested=True, proposal="do it")
    schema = MCPElicitationTransport.schema_for(q_appr)
    assert schema["properties"]["value"]["type"] == "boolean"


# --------------------------------------------------------------------------- #
# 4. CLI transport round-trip                                                 #
# --------------------------------------------------------------------------- #

def test_cli_transport_round_trip():
    q_dim = Question(id="bore.dia", kind="dimension", prompt="Bore diameter?",
                      suggested={"value": 10.0, "unit": "mm"}, family="length")
    q_choice = Question(id="finish", kind="choice", prompt="Surface finish?",
                         suggested="as_machined",
                         options=[{"value": "as_machined"}, {"value": "anodized"}])
    transport = CLITransport(in_stream=io.StringIO("0.5 in\n2\n"), out_stream=io.StringIO())

    answers = ask_user([q_dim, q_choice], run_id="r_cli", transport=transport)

    assert answers["bore.dia"]["canonical"] == {"value": 12.7, "unit": "mm"}
    assert answers["bore.dia"]["source"] == "user"
    assert answers["finish"]["value"] == "anodized"
    assert answers["finish"]["source"] == "user"


def test_cli_transport_empty_line_yields_suggested_default():
    q = Question(id="bore.dia", kind="dimension", prompt="Bore diameter?",
                 suggested={"value": 10.0, "unit": "mm"}, family="length")
    transport = CLITransport(in_stream=io.StringIO("\n"), out_stream=io.StringIO())

    answers = ask_user([q], run_id="r_cli_default", transport=transport)

    assert answers["bore.dia"]["source"] == "default"
    assert answers["bore.dia"]["canonical"] == {"value": 10.0, "unit": "mm"}


# --------------------------------------------------------------------------- #
# 5. Persistence + replay on resume                                          #
# --------------------------------------------------------------------------- #

def test_replay_on_resume_never_reinvokes_transport():
    q = Question(id="bore.dia", kind="dimension", prompt="Bore diameter?",
                 suggested={"value": 10.0, "unit": "mm"}, family="length", required=True)

    def provider(batch_dict):
        return {"bore.dia": {"value": 0.5, "unit": "in"}}

    first = ask_user([q], run_id="r_resume", transport=InAppTransport(provider))
    assert first["bore.dia"]["source"] == "user"
    assert first["bore.dia"]["canonical"] == {"value": 12.7, "unit": "mm"}

    def boom(batch_dict):
        raise AssertionError("transport must not be invoked on resume")

    second = ask_user([q], run_id="r_resume", transport=InAppTransport(boom))

    assert second["bore.dia"]["source"] == "resumed"
    assert second["bore.dia"]["canonical"] == first["bore.dia"]["canonical"]
    assert second["bore.dia"]["value"] == first["bore.dia"]["value"]

    ledger_path = interaction.run_dir("r_resume") / "interaction.jsonl"
    assert ledger_path.exists()
    records = [json.loads(l) for l in ledger_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    kinds = {r["t"] for r in records}
    assert "question" in kinds
    assert "answer" in kinds


# --------------------------------------------------------------------------- #
# 6. autonomy="auto" and blocking=False -> all suggested defaults             #
# --------------------------------------------------------------------------- #

def test_autonomy_auto_and_nonblocking_uses_suggested_defaults():
    q1 = Question(id="a", kind="dimension", prompt="A?", suggested={"value": 1.0, "unit": "mm"})
    q2 = Question(id="b", kind="choice", prompt="B?", suggested="x",
                  options=[{"value": "x"}, {"value": "y"}])

    answers = ask_user([q1, q2], run_id="r_auto", blocking=False, autonomy="auto")

    assert answers["a"]["source"] == "default"
    assert answers["a"]["canonical"] == {"value": 1.0, "unit": "mm"}
    assert answers["b"]["source"] == "default"
    assert answers["b"]["value"] == "x"


# --------------------------------------------------------------------------- #
# 7. Contract-error cases                                                     #
# --------------------------------------------------------------------------- #

def test_more_than_four_questions_raises():
    qs = [Question(id=f"q{i}", kind="text", prompt="x", suggested="a") for i in range(5)]
    with pytest.raises(InteractionError):
        ask_user(qs, run_id="r_toomany")


def test_question_with_suggested_none_raises():
    q = Question(id="q", kind="text", prompt="x", suggested=None)
    with pytest.raises(InteractionError):
        ask_user([q], run_id="r_nosuggest")


def test_required_question_unanswered_by_blocking_transport_raises():
    q = Question(id="q", kind="text", prompt="x", suggested="default text", required=True)

    def provider(batch_dict):
        return {}  # declines to answer

    with pytest.raises(InteractionError):
        ask_user([q], run_id="r_unanswered", transport=InAppTransport(provider))


def test_choice_without_options_raises():
    q = Question(id="q", kind="choice", prompt="x", suggested="a", options=None)
    with pytest.raises(InteractionError):
        ask_user([q], run_id="r_nooptions")


# --------------------------------------------------------------------------- #
# 8. Events + persist-then-callback ordering                                  #
# --------------------------------------------------------------------------- #

def test_events_persist_then_callback_ordering():
    events: list[tuple[str, str]] = []
    run_id = "r_events"

    def on_event(event, data):
        events.append((event, data.get("id")))
        if event == "ask_answer":
            # Persist-then-callback (contract §7): the ledger write already
            # happened by the time this event fires.
            ledger_path = interaction.run_dir(run_id) / "interaction.jsonl"
            records = [json.loads(l) for l in
                       ledger_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            recorded = [r for r in records if r.get("t") == "answer" and r.get("id") == data["id"]]
            assert recorded, "answer must be persisted before the ask_answer event fires"

    q1 = Question(id="a", kind="text", prompt="A?", suggested="def_a")
    q2 = Question(id="b", kind="text", prompt="B?", suggested="def_b")

    def provider(batch_dict):
        return {"a": "user_a", "b": "user_b"}

    ask_user([q1, q2], run_id=run_id, transport=InAppTransport(provider), on_event=on_event)

    assert events.index(("ask_question", "a")) < events.index(("ask_answer", "a"))
    assert events.index(("ask_question", "b")) < events.index(("ask_answer", "b"))
    assert {e for e, _ in events} == {"ask_question", "ask_answer"}
