from pathlib import Path
from types import SimpleNamespace

import pytest

from services.agent.omp_host import OmpSessionHost, _Session, _cad_session_overlay


class Bridge:
    def __init__(self):
        self.calls = []
        self.fail = False

    def call(self, operation, arguments, *, timeout):
        self.calls.append((operation, arguments))
        if self.fail:
            raise RuntimeError('policy rejected')
        return {'compaction': arguments['compaction']}


def test_live_policy_preserves_exact_method_and_journal(tmp_path):
    journal = tmp_path / 'history.jsonl'
    content = b'{"type":"message","content":[{"type":"thinking","thinkingSignature":"opaque"}]}\n'
    journal.write_bytes(content)
    bridge = Bridge()
    client = SimpleNamespace(_omp_bridge=bridge)
    session = _Session('a', client, str(journal), None)
    host = OmpSessionHost()
    shake = {'enabled': True, 'methodOrder': ['shake']}
    host._refresh_compaction(session, {'settings': {'compaction_settings': shake}})
    assert bridge.calls == [('settings_set', {'compaction': shake})]
    assert session.client is client
    assert journal.read_bytes() == content
    host._refresh_compaction(session, {'settings': {'compaction_settings': {}}})
    assert bridge.calls[-1] == ('settings_set', {'compaction': {}})
    assert session.compaction_settings == {}


def test_failed_live_policy_keeps_previous_effective_setting():
    bridge = Bridge()
    session = _Session('a', SimpleNamespace(_omp_bridge=bridge), None, None,
                       compaction_settings={'methodOrder': ['snapcompact']})
    bridge.fail = True
    with pytest.raises(RuntimeError, match='policy rejected'):
        OmpSessionHost()._refresh_compaction(
            session, {'settings': {'compaction_settings': {'methodOrder': ['soft']}}})
    assert session.compaction_settings == {'methodOrder': ['snapcompact']}


def test_immutable_overlay_does_not_rewrite_other_policy(tmp_path, monkeypatch):
    from services import paths
    monkeypatch.setattr(paths, 'ROOT', tmp_path)
    summary = Path(_cad_session_overlay('off', {'methodOrder': ['soft']}))
    original = summary.read_bytes()
    shake = Path(_cad_session_overlay('off', {'methodOrder': ['shake']}))
    assert summary != shake
    assert summary.read_bytes() == original
