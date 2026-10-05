"""IP-18 regen_diagnose MCP seam: the callable backend fires on a rebuild-failure doc, returns a
fix-preview card, and degrades honestly when its model step cannot run. The default model step
is a tool-free agent-runtime completion (``ChudcodeSessionHost.complete_text``), exercised here
through an injected process-free RPC client; no binary is started and no provider is contacted.
Offline/local-only binds an offline-safe provider. No core (structural verify)."""
import json
import random
from types import SimpleNamespace

import pytest

from services.agent import config as agent_config
from services.agent import regen_diagnosis as RD
from services.agent import role_models
from services.agent.chudcode_host import (
    ChudcodeSessionHost,
    _set_default_chudcode_host_for_tests,
)
from services.agent.providers.base import ProviderUnreachable
from services.agent.providers.mock_provider import MockProvider
from services.mcp import regen_tools as RT
from tools.flywheel.bench.corpus import regen_corpus as RC


class _HelperClient:
    """The RpcClient surface ``complete_text`` drives, without a process."""

    protocol_version = 2

    def __init__(self, script, **kwargs):
        self.kwargs = kwargs
        self.script = script
        self.listeners = []
        self.prompts = []
        self.models = []
        self.stopped = False

    def start(self):
        return self

    def on_notification(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def set_model(self, provider, model):
        self.models.append((provider, model))

    def prompt_and_wait(self, message, timeout=None):
        self.prompts.append(message)
        error = self.script.get("error")
        message_end = {"role": "assistant", "stopReason": "error" if error else "stop"}
        if error:
            message_end["errorMessage"] = error
        for listener in list(self.listeners):
            listener({"type": "message_end", "message": message_end})
        return SimpleNamespace(assistant_text=self.script.get("reply"))

    def stop(self):
        self.stopped = True


@pytest.fixture
def runtime(monkeypatch):
    """The process-global agent host with a process-free client and the runtime's own model."""
    clients = []
    script = {}

    def factory(**kwargs):
        clients.append(_HelperClient(script, **kwargs))
        return clients[-1]

    host = ChudcodeSessionHost(client_factory=factory)
    monkeypatch.setattr(host, "_resolve_binary", lambda *, required: None)
    monkeypatch.setattr(agent_config, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(role_models, "resolve_role_model",
                        lambda *a, **k: SimpleNamespace(provider="", model=""))
    previous = _set_default_chudcode_host_for_tests(host)
    try:
        yield host, clients, script
    finally:
        _set_default_chudcode_host_for_tests(previous)
        host.shutdown()


def _corrupt(mclass, seed=1):
    corrupted, _ = RC.mutate(RC.load_seed(), mclass, random.Random(seed))
    return {"revision": 0, "tree": corrupted, "owner_map": {}}


def _oracle_reply(document):
    ops = []
    for f in RD.detect_failures(document):
        if f.kind == "dangling_ref":
            ops.append({"op": "repoint_ref", "feature": f.feature,
                        "from": f.options["from"], "to": f.options["valid_feature_ids"][0]})
        elif f.kind == "suppressed_parent":
            ops.append({"op": "unsuppress", "feature": f.feature})
        elif f.kind == "bad_selector":
            ops.append({"op": "set_selector_kind", "feature": f.feature,
                        "old_kind": f.options["bad_kind"], "kind": "face"})
    return json.dumps({"diagnosis": "repair", "fix_ops": ops})


def test_seam_returns_card_and_logs_ip18():
    doc = _corrupt("suppressed_parent", seed=2)   # un-suppress rebuilds cleanly in fixture OR live
    events = []
    out = RT.diagnose_failed_document(
        doc, provider=MockProvider([_oracle_reply(doc)]),
        on_event=events.append, consent_fn=lambda: "meta")
    assert out["ok"] is True
    assert out["artifact_id"]
    card = out["card"]
    assert card["kind"] == "fix_preview_card" and card["fix_ops"]
    # verify runs with the strongest available signal (fixture w/o core, else live) — robust to env
    assert card["verifier"]["detail"]["mode"] in ("fixture", "live-rebuild-only", "live")
    assert card["verifier"]["score"] == 1.0
    # IP-18 correction trace emitted
    assert any(e.get("kind") == "ai_artifact" and e["insertion_point"] == "IP-18" for e in events)


def test_seam_degrades_without_provider(runtime):
    _, clients, script = runtime
    script["error"] = "no provider credentials configured"
    doc = _corrupt("bad_selector", seed=3)
    out = RT.diagnose_failed_document(doc, provider=None)
    assert out["ok"] is False
    assert "no provider credentials configured" in out["reason"]
    assert any(f["kind"] == "bad_selector" for f in out["failures"])
    assert len(clients) == 1 and clients[0].stopped


def test_unreachable_provider_degrades_instead_of_raising():
    """A configured provider that cannot be reached must not crash the diagnosis.

    Regression: openai_provider raised a bare RuntimeError for a refused connection, so
    a user with LM Studio configured but not running got an exception out of
    regen_diagnose instead of the failure list the tool promises in its docstring.
    """
    class _Down:
        def complete(self, *a, **k):
            raise ProviderUnreachable("Could not reach the model server at http://localhost:1234/v1")

        def complete_ex(self, *a, **k):
            raise ProviderUnreachable("Could not reach the model server at http://localhost:1234/v1")

    doc = _corrupt("bad_selector", seed=3)
    out = RT.diagnose_failed_document(doc, provider=_Down())
    assert out["ok"] is False
    assert "provider unreachable" in out["reason"]
    assert "localhost:1234" in out["reason"]          # names the endpoint that is down
    assert any(f["kind"] == "bad_selector" for f in out["failures"])


def test_tool_registration_and_json_contract():
    captured = {}

    class _FakeMCP:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    RT.register_regen_tools(_FakeMCP())
    assert "regen_diagnose" in captured
    fn = captured["regen_diagnose"]
    doc = _corrupt("suppressed_parent", seed=4)
    # inject an oracle provider by monkeypatching the resolver for this call
    orig = RT._resolve_provider
    RT._resolve_provider = lambda cancel=None: MockProvider([_oracle_reply(doc)])
    try:
        raw = fn(json.dumps(doc))
    finally:
        RT._resolve_provider = orig
    result = json.loads(raw)
    assert result["ok"] is True
    assert result["card"]["kind"] == "fix_preview_card"


def test_tool_reports_bad_json():
    captured = {}

    class _FakeMCP:
        def tool(self):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn
            return deco

    RT.register_regen_tools(_FakeMCP())
    assert "regen_diagnose" in captured
    result = json.loads(captured["regen_diagnose"]("{not valid json"))
    assert result["ok"] is False and "invalid JSON" in result["error"]


def test_default_helper_returns_logged_fix_card(runtime):
    host, clients, script = runtime
    doc = _corrupt("suppressed_parent", seed=2)
    script["reply"] = _oracle_reply(doc)
    events = []
    out = RT.diagnose_failed_document(
        doc, on_event=events.append, consent_fn=lambda: "meta", verify=False)
    assert out["ok"] is True and out["card"]["fix_ops"]
    assert out["artifact_id"]
    assert any(event.get("kind") == "ai_artifact" for event in events)
    assert len(clients) == 1 and clients[0].stopped
    assert "Rebuild failed. Repair it." in clients[0].prompts[0]
    assert not host._sessions and not host._helper_clients


def test_explicit_offline_helper_binds_only_offline_safe_provider(monkeypatch):
    from services.agent.providers.chudcode_provider import helper_provider

    bound = MockProvider(["offline repair"])
    monkeypatch.setattr(agent_config, "load_config", lambda: {"offline_mode": True})
    monkeypatch.setattr(agent_config, "get_provider", lambda **kwargs: bound)
    monkeypatch.setattr(agent_config, "provider_offline_safe", lambda provider: provider is bound)
    assert helper_provider("fixer").complete("", []) == "offline repair"

    monkeypatch.setattr(agent_config, "provider_offline_safe", lambda provider: False)
    with pytest.raises(agent_config.OfflineModeError, match="offline helper"):
        helper_provider("fixer")


def test_cancelled_default_helper_retains_detected_failures_without_prompting(runtime):
    _, clients, _ = runtime
    doc = _corrupt("bad_selector", seed=3)
    out = RT.diagnose_failed_document(doc, cancel=lambda: True)
    assert not out["ok"] and "cancelled" in out["reason"]
    assert any(f["kind"] == "bad_selector" for f in out["failures"])
    assert not clients


def test_incomplete_selected_model_fails_without_runtime_default_substitution(runtime, monkeypatch):
    _, clients, _ = runtime
    monkeypatch.setattr(agent_config, "load_config", lambda: {"provider": "codex"})
    doc = _corrupt("bad_selector", seed=3)
    out = RT.diagnose_failed_document(doc)
    assert not out["ok"] and "needs both provider and model" in out["reason"]
    assert not clients
