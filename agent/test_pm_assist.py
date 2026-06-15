"""PropertyManager AI assist tests."""
from __future__ import annotations

import json
import os
import tempfile

from agent import pm_assist
from agent.providers.mock_provider import MockProvider


class RecordingMock(MockProvider):
    def __init__(self, reply: str):
        super().__init__([reply])
        self.systems: list[str] = []
        self.users: list[str] = []

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        self.systems.append(system)
        self.users.append(messages[-1]["content"][0]["text"] if messages else "")
        return super().complete(system, messages, max_tokens=max_tokens)


def _extrude_params() -> dict:
    return {
        "doc_id": "",
        "intent": "extrude up to the selected top plane",
        "form_schema": {
            "type": "extrude",
            "title": "Boss-Extrude",
            "fields": [
                {"key": "op", "kind": "combo", "visible": True, "options": ["boss", "cut"]},
                {"key": "start", "kind": "combo", "visible": True,
                 "options": ["sketch_plane", "offset", "from_reference", "offset_from_reference"]},
                {"key": "profile", "kind": "selector", "visible": True},
                {"key": "end", "kind": "combo", "visible": True,
                 "options": ["blind", "through_all", "mid_plane", "up_to_reference",
                             "offset_from_reference"]},
                {"key": "end_ref", "kind": "selector", "visible": False},
                {"key": "depth", "kind": "dim", "visible": False},
            ],
        },
        "current": {"fields": {"profile": {"count": 1}, "end_ref": {"count": 0}}},
        "selected_refs": [],
    }


def test_pm_assist_filters_to_visible_safe_fields() -> None:
    reply = json.dumps({
        "answer": "Use an up-to reference.",
        "patches": [
            {"key": "end", "value": "up_to_reference"},
            {"key": "depth", "value": 999},
            {"key": "end_ref", "value": {"kind": "face", "id": 12}},
            {"key": "op", "value": "bogus"},
        ],
        "activate_well": "end_ref",
        "highlights": ["end", "end_ref", "missing"],
    })
    prov = RecordingMock(reply)
    out = pm_assist.assist_property_manager(_extrude_params(), None, prov)
    assert out["patches"] == [{"key": "end", "value": "up_to_reference"}], out
    assert out["activate_well"] == "end_ref", out
    assert out["highlights"] == ["end"], out
    assert prov.systems[0].startswith(f"ROLE: {pm_assist.PM_ASSIST_ROLE}")
    assert "form_schema" in prov.users[0]
    print("[pm-assist] filters unsafe/invisible patches")


def test_pm_assist_heuristic_fallback() -> None:
    out = pm_assist.assist_property_manager(_extrude_params(), None, MockProvider(["not json"]))
    assert {"key": "end", "value": "up_to_reference"} in out["patches"], out
    assert out["activate_well"] == "end_ref", out
    print("[pm-assist] heuristic fallback chooses up_to_reference + end_ref")


def test_worker_agent_pm_assist_adapter() -> None:
    import engine.worker as worker

    assert "agent_pm_assist_v1" in worker._METHODS
    assert "agent_pm_assist_v1" in worker._STRUCTURED_ERROR_METHODS

    fd, replies_path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"pm_assist": [json.dumps({
            "answer": "Set end to up to reference.",
            "patches": [{"key": "end", "value": "up_to_reference"}],
            "activate_well": "end_ref",
        })]}, fh)
    old_prov = os.environ.get("AI_CAD_PROVIDER")
    old_replies = os.environ.get("AI_CAD_MOCK_REPLIES")
    os.environ["AI_CAD_PROVIDER"] = "mock"
    os.environ["AI_CAD_MOCK_REPLIES"] = replies_path
    try:
        res = worker._METHODS["agent_pm_assist_v1"](_extrude_params(), lambda e: None)
        assert res["patches"] == [{"key": "end", "value": "up_to_reference"}], res
        assert res["activate_well"] == "end_ref", res
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
    print("[pm-assist worker] adapter registered and returns validated actions")


def main() -> None:
    test_pm_assist_filters_to_visible_safe_fields()
    test_pm_assist_heuristic_fallback()
    test_worker_agent_pm_assist_adapter()
    print("\nPM ASSIST TESTS PASSED")


if __name__ == "__main__":
    main()
