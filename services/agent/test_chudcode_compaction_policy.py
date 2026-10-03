from pathlib import Path

from services.agent.chudcode_host import ChudcodeSessionHost, _cad_session_overlay
from services.agent.test_chudcode_host import FakeFactory, run


def policy(method):
    return {"compaction_settings": {"enabled": True, "methodOrder": [method]}}


def test_compaction_change_preserves_history_and_other_sessions(tmp_path):
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    try:
        assert run(host, "a", "a1", settings=policy("shake"))[0]["ok"]
        assert run(host, "b", "b1", settings=policy("snapcompact"))[0]["ok"]
        old, other = factory.clients
        history = tmp_path / "history.jsonl"
        history.write_text('{"type":"session","id":"original"}\n')
        old.session_file = str(history)
        host.session_call("a", "set_auto_compaction", {"enabled": False})
        host.session_call("a", "set_auto_retry", {"enabled": False})
        result, _ = run(host, "a", "a2", settings=policy("soft"))
        assert result["ok"], result
        assert result["chudcode_session_file"] == str(history)
        assert history.read_text() == '{"type":"session","id":"original"}\n'
        assert old.stop_count == 1
        assert other.stop_count == 0
        replacement = factory.clients[-1]
        assert ("set_auto_compaction", {"enabled": False}) in replacement.calls
        assert ("set_auto_retry", {"enabled": False}) in replacement.calls
        assert run(host, "a", "a3", settings=policy("soft"))[0]["ok"]
        assert len(factory.clients) == 3
        assert replacement.stop_count == 0
    finally:
        host.shutdown()


def test_compaction_change_without_history_leaves_existing_session_usable():
    factory = FakeFactory()
    host = ChudcodeSessionHost(client_factory=factory)
    try:
        assert run(host, "a", "a1", settings=policy("shake"))[0]["ok"]
        result, _ = run(host, "a", "a2", settings=policy("soft"))
        assert not result["ok"]
        assert "persisted session history" in result["reason"]
        assert factory.clients[0].stop_count == 0
        assert run(host, "a", "a3", settings=policy("shake"))[0]["ok"]
        assert len(factory.clients) == 1
    finally:
        host.shutdown()


def test_compaction_overlay_cannot_change_another_sessions_policy(tmp_path, monkeypatch):
    from services import paths

    monkeypatch.setattr(paths, "ROOT", tmp_path)
    summary = Path(_cad_session_overlay("off", policy("soft")["compaction_settings"]))
    original = summary.read_bytes()
    shake = Path(_cad_session_overlay("off", policy("shake")["compaction_settings"]))
    assert summary != shake
    assert summary.read_bytes() == original
    assert _cad_session_overlay("off", policy("soft")["compaction_settings"]) == str(summary)
