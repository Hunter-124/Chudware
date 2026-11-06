import json

from services.mcp import _envelopes, core_bridge


def test_error_writers_expose_message_and_code_without_copying_hint():
    dumps, analysis_err, _ = _envelopes.analysis_envelope()
    for value in (analysis_err("failed to analyze"),
                  _envelopes.report_err("failed to analyze"),
                  _envelopes.compact_err("failed to analyze")):
        result = json.loads(value)
        assert result["ok"] is False
        assert result["error"] == "failed to analyze"
        assert result["code"] == "error"
        assert "hint" not in result
    reason = _envelopes.reason_err("invalid_input", hint="Provide a dimension")
    assert reason == {"ok": False, "error": "invalid_input", "code": "invalid_input",
                      "reason": "invalid_input", "hint": "Provide a dimension"}


def test_private_fallback_is_visible_on_shared_replies(monkeypatch):
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "1")
    monkeypatch.setattr(core_bridge, "_DAEMON_FALLBACK_WARNED", True)
    result = json.loads(_envelopes.dumps({"ok": True, "value": 3}))
    assert result["engine"] == "private"
    assert "not shared" in result["engine_warning"]
    assert result["value"] == 3


def test_tool_reply_normalizes_reason_only_and_nested_errors():
    reason = _envelopes.tool_reply({"ok": False, "reason": "unavailable"})
    assert reason["error"] == "unavailable" and reason["code"] == "unavailable"
    nested = json.loads(_envelopes.tool_reply(
        '{"ok": false, "error": {"message": "Cannot open", "code": "not_found"}}'))
    assert nested["error"] == "Cannot open" and nested["code"] == "not_found"
    assert _envelopes.tool_reply("plain text") == "plain text"
