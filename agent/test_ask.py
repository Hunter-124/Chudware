"""Wave D — ask-mode tests (mock provider, real document sessions).

House style: plain ``main()`` runner with asserts (see agent/test_orchestrator.py); also pytest-
collectable. No API key, no network — set ``AI_CAD_PROVIDER=mock`` when running the suite.

Coverage:
  * with a doc — the single ask prompt carries the feature list (ids/types/names/key params),
    doc status and units, plus the question; the system prompt redirects change requests to
    Edit-part mode; the provider's reply is returned verbatim;
  * without a doc — no session (or an empty doc) answers gracefully (general-usage context),
    never raises;
  * worker adapter — agent_ask_v2 is registered in _METHODS + _STRUCTURED_ERROR_METHODS and the
    thin adapter returns {"answer": ...} for a real doc id, an empty doc_id and an unknown one.
"""
from __future__ import annotations

import json
import os
import tempfile

from agent import ask
from agent.providers.mock_provider import MockProvider
from engine.doc import session as docsess


class RecordingMock(MockProvider):
    """MockProvider that records every (system, user_text) prompt it is asked to complete."""

    def __init__(self, reply: str):
        super().__init__([reply])
        self.systems: list[str] = []
        self.users: list[str] = []

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        self.systems.append(system)
        self.users.append(messages[-1]["content"][0]["text"] if messages else "")
        return super().complete(system, messages, max_tokens=max_tokens)


_BOX_OP = {"op": "add_feature", "feature": {"type": "box", "name": "Plate",
    "params": {"length": {"value": 40, "expr": None}, "width": {"value": 30, "expr": None},
               "height": {"value": 10, "expr": None}}}}


def test_ask_with_doc_context() -> None:
    """The ask prompt carries the feature digest + status + units; the reply comes back verbatim."""
    s = docsess.new_session("part", name="ask_doc")
    docsess.apply_ops(s, 0, [_BOX_OP])
    prov = RecordingMock("The plate is 40 x 30 x 10 mm.")

    out = ask.answer_question("how big is the plate?", s, prov)
    assert out == "The plate is 40 x 30 x 10 mm."
    assert len(prov.systems) == 1, "ask mode must make exactly ONE provider call"

    system, body = prov.systems[0], prov.users[0]
    assert system.startswith(f"ROLE: {ask.ASK_ROLE}"), "role marker for mock routing"
    assert "Edit-part mode" in system, "change requests must be redirected to Edit-part mode"
    # The compact doc context: feature line with id/type/name + key params, units, status.
    assert "f1 box Plate" in body, body
    assert "length=40" in body and "width=30" in body and "height=10" in body
    assert "UNITS: MMGS" in body
    assert "STATUS:" in body and "compile cleanly" in body
    assert "QUESTION:\nhow big is the plate?" in body
    print("[ask doc] answer ok; context carries features/units/status")


def test_ask_without_doc() -> None:
    """No session and an empty doc both answer gracefully (general CAD usage context)."""
    prov = RecordingMock("Use Design-new mode to start a part.")
    out = ask.answer_question("how do I start a part?", None, prov)
    assert out == "Use Design-new mode to start a part."
    assert "NO DOCUMENT IS OPEN" in prov.users[0]

    # An open-but-empty doc states it has no features yet (still no error).
    s = docsess.new_session("part", name="ask_empty")
    prov2 = RecordingMock("It is empty.")
    out2 = ask.answer_question("what does my part contain?", s, prov2)
    assert out2 == "It is empty."
    assert "none yet" in prov2.users[0]
    print("[ask nodoc] graceful no-doc + empty-doc contexts")


def test_worker_agent_ask_v2_adapter() -> None:
    """agent_ask_v2 is registered (structured errors) and the adapter answers for a real doc id,
    an EMPTY doc_id and an UNKNOWN doc_id (the latter two fall back to general usage)."""
    import engine.worker as worker

    assert "agent_ask_v2" in worker._METHODS, "agent_ask_v2 must be registered"
    assert "agent_ask_v2" in worker._STRUCTURED_ERROR_METHODS, \
        "agent_ask_v2 must use the structured error envelope"

    s = docsess.new_session("part", name="ask_worker")
    docsess.apply_ops(s, 0, [_BOX_OP])

    # Role-keyed scripted replies -> config "mock" resolves a RoleScriptedProvider that routes
    # on the "ROLE: ask" marker the ask prompt carries.
    fd, replies_path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"ask": ["It has one box feature, 40x30x10."]}, fh)
    old_prov = os.environ.get("AI_CAD_PROVIDER")
    old_replies = os.environ.get("AI_CAD_MOCK_REPLIES")
    os.environ["AI_CAD_PROVIDER"] = "mock"
    os.environ["AI_CAD_MOCK_REPLIES"] = replies_path
    try:
        fn = worker._METHODS["agent_ask_v2"]
        res = fn({"doc_id": s.doc_id, "question": "what features does it have?"}, lambda e: None)
        assert res == {"answer": "It has one box feature, 40x30x10."}, res
        # Empty + unknown doc ids answer gracefully (no exception, an answer comes back).
        res2 = fn({"doc_id": "", "question": "how do I start?"}, lambda e: None)
        assert res2["answer"]
        res3 = fn({"doc_id": "not-a-real-doc", "question": "hello?"}, lambda e: None)
        assert res3["answer"]
    finally:
        if old_prov is None:
            os.environ.pop("AI_CAD_PROVIDER", None)
        else:
            os.environ["AI_CAD_PROVIDER"] = old_prov
        if old_replies is None:
            os.environ.pop("AI_CAD_MOCK_REPLIES", None)
        else:
            os.environ["AI_CAD_MOCK_REPLIES"] = old_replies
        try:
            os.unlink(replies_path)
        except OSError:
            pass
    print("[ask worker] adapter answers for real/empty/unknown doc ids")


def main() -> None:
    test_ask_with_doc_context()
    test_ask_without_doc()
    test_worker_agent_ask_v2_adapter()
    print("\nASK MODE (Wave D) TESTS PASSED")


if __name__ == "__main__":
    main()
