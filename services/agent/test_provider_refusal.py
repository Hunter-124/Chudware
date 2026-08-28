"""test_provider_refusal.py — a vendor content refusal is not an outage.

Live defect this pins (2026-08-26, run-153290332111741): the user asked for a
machinable hand-hammer pellet press plus shop drawings. Anthropic's classifier
refused it, and the run died with

    status=failed  stop_reason=provider_error
    error_detail="anthropic: BridgeUnavailable: omp-bridge anthropic: Refusal (bio)"

Three things were wrong at once. The sidecar was healthy, so "BridgeUnavailable"
was a lie that sends the user to re-check credentials. `provider_error` is the
generic retryable bucket, so nothing downstream could act on the real cause. And
three other credentialed providers sat idle in the same config while the project
directory ended the day with a PLM vault and zero deliverables.

Proven here (no network):
  1. a structured refusal raises ProviderRefused carrying the vendor + category;
  2. an error-class stop that names a NON-refusal type stays BridgeUnavailable
     (the structured field beats the prose match);
  3. vendors that never populate stopDetails are still recognised from prose;
  4. "connection refused" is a transport fault, never a policy decision;
  5. classify_provider_error buckets it as provider_refused, non-retryable;
  6. failover candidates exclude the whole refusing vendor FAMILY, one per family;
  7. run_turn re-issues the identical prompt to another vendor and finishes;
  8. when every vendor declines, the turn stops as provider_refused with a
     sentence that names who declined instead of blaming the bridge.
"""
from __future__ import annotations

import pytest

from services.agent import agent_turn, omp_bridge
from services.agent.providers import base as pbase
from services.agent.providers.omp_bridge_provider import OmpBridgeProvider


@pytest.fixture(autouse=True)
def _clean_sink():
    prev = pbase.set_provider_delta_sink(None)
    yield
    pbase.set_provider_delta_sink(prev)


def _bridge_returns(monkeypatch, result):
    def fake_call(method, params=None, timeout=120, **kwargs):
        if method != "complete":
            return {}
        return result

    monkeypatch.setattr(omp_bridge, "call", fake_call)


# --- 1. structured refusal ---------------------------------------------------

def test_structured_refusal_raises_provider_refused(monkeypatch):
    _bridge_returns(monkeypatch, {
        "text": "", "thinking": "", "usage": {}, "stopReason": "error",
        "errorStatus": None,
        "errorMessage": "Refusal (bio): I can't help with that.",
        "stopDetails": {"type": "refusal", "category": "bio",
                        "explanation": "I can't help with that."},
    })
    prov = OmpBridgeProvider(provider="anthropic", model="claude-fable-5")

    with pytest.raises(omp_bridge.ProviderRefused) as ei:
        prov.complete_ex("sys", [{"role": "user", "content": "design a pellet press"}])

    assert ei.value.provider == "anthropic"
    assert ei.value.category == "bio"
    # The vendor is named as the actor; the bridge is not blamed.
    assert "anthropic declined this request (bio)" in str(ei.value)
    assert "BridgeUnavailable" not in str(ei.value)
    # Still a BridgeUnavailable subclass so existing fall-back call sites work.
    assert isinstance(ei.value, omp_bridge.BridgeUnavailable)


# --- 2. a non-refusal error type must not be re-read as a refusal ------------

def test_structured_non_refusal_type_stays_bridge_unavailable(monkeypatch):
    # "overloaded" carries the word "refusal" nowhere, but a vendor could put
    # policy prose in errorMessage. The structured type is authoritative.
    _bridge_returns(monkeypatch, {
        "text": "", "thinking": "", "usage": {}, "stopReason": "error",
        "errorMessage": "upstream refusal to serve: capacity",
        "stopDetails": {"type": "overloaded"},
    })
    prov = OmpBridgeProvider(provider="anthropic", model="claude-fable-5")

    with pytest.raises(omp_bridge.BridgeUnavailable) as ei:
        prov.complete_ex("sys", [{"role": "user", "content": "hi"}])
    assert not isinstance(ei.value, omp_bridge.ProviderRefused)


# --- 3. vendors with no stopDetails ------------------------------------------

@pytest.mark.parametrize("message", [
    "content_policy_violation: request rejected",
    "Response blocked by safety settings (PROHIBITED_CONTENT)",
    "The response was filtered due to the prompt triggering our content "
    "management policy",
])
def test_prose_only_refusals_are_recognised(monkeypatch, message):
    _bridge_returns(monkeypatch, {
        "text": "", "thinking": "", "usage": {}, "stopReason": "error",
        "errorMessage": message,
    })
    prov = OmpBridgeProvider(provider="openai", model="gpt-5.6-sol")

    with pytest.raises(omp_bridge.ProviderRefused) as ei:
        prov.complete_ex("sys", [{"role": "user", "content": "hi"}])
    # No vendor category available — empty, never fabricated.
    assert ei.value.category == ""


# --- 4. transport faults are not policy decisions ---------------------------

@pytest.mark.parametrize("message", [
    "connect ECONNREFUSED 127.0.0.1:11434",
    "fetch failed: connection refused",
    "upstream declined the TLS handshake",
])
def test_transport_faults_are_never_refusals(monkeypatch, message):
    _bridge_returns(monkeypatch, {
        "text": "", "thinking": "", "usage": {}, "stopReason": "error",
        "errorMessage": message,
    })
    prov = OmpBridgeProvider(provider="ollama", model="llama3.2:latest")

    with pytest.raises(omp_bridge.BridgeUnavailable) as ei:
        prov.complete_ex("sys", [{"role": "user", "content": "hi"}])
    assert not isinstance(ei.value, omp_bridge.ProviderRefused), message


# --- 4b. the THROW path (Gemini, OpenAI Responses) --------------------------

def _client_answering(frame: dict):
    """A real OmpBridgeClient whose child is replaced by one queued response frame.

    `_call_once` is the code under test — the point is that the frame's `kind`
    field decides the exception type, so a hand-rolled raise would prove nothing.
    """
    from queue import Queue

    class _Stdin:
        def write(self, _line): pass
        def flush(self): pass

    class _Proc:
        stdin = _Stdin()
        def poll(self): return None

    client = omp_bridge.OmpBridgeClient.__new__(omp_bridge.OmpBridgeClient)
    client._proc = _Proc()
    client._next_id = 1
    client._out_q = Queue()
    import json as _json
    client._out_q.put(_json.dumps({"id": 1, **frame}))
    return client


def test_a_content_blocked_rpc_error_is_a_refusal():
    """Google and the OpenAI Responses path do not return an error-stop message —
    they raise ProviderResponseError{kind:"content-blocked"}, which reaches Python
    as an RPC failure. Without the forwarded `kind` that is indistinguishable from
    the bridge dying, so those two vendors would never fail over."""
    client = _client_answering(
        {"ok": False, "error": {"message": "incomplete: content_filter",
                                "kind": "content-blocked"}})

    with pytest.raises(omp_bridge.ProviderRefused) as ei:
        client._call_once("complete", {"provider": "gemini"}, timeout=5)
    assert ei.value.provider == "gemini"
    assert "declined this request" in str(ei.value)


def test_an_rpc_error_without_that_kind_is_still_an_outage():
    client = _client_answering(
        {"ok": False, "error": {"message": "No API key for provider: gemini",
                                "status": 401}})

    with pytest.raises(omp_bridge.BridgeUnavailable) as ei:
        client._call_once("complete", {"provider": "gemini"}, timeout=5)
    assert not isinstance(ei.value, omp_bridge.ProviderRefused)
    assert ei.value.status == 401


def test_complete_ex_backfills_the_provider_on_a_bare_refusal(monkeypatch):
    def fake_call(method, params=None, timeout=120, **kwargs):
        raise omp_bridge.ProviderRefused("the provider declined this request: blocked")

    monkeypatch.setattr(omp_bridge, "call", fake_call)
    prov = OmpBridgeProvider(provider="gemini", model="gemini-2.5-pro")

    with pytest.raises(omp_bridge.ProviderRefused) as ei:
        prov.complete_ex("sys", [{"role": "user", "content": "hi"}])
    assert ei.value.provider == "gemini"


def test_a_refusal_is_never_salvaged_from_a_partial_stream(monkeypatch):
    """Text streamed before the filter tripped is not an answer. Salvaging it
    would report a fragment as a finished reply and swallow the refusal."""
    def fake_call(method, params=None, timeout=120, **kwargs):
        cb = kwargs.get("on_event")
        if cb is not None:
            cb("delta", {"text": "Sure, here is how you would "})
        raise omp_bridge.ProviderRefused("the provider declined this request: blocked",
                                         provider="gemini")

    monkeypatch.setattr(omp_bridge, "call", fake_call)

    class _Sink:
        def __init__(self):
            self.feeds = []

        def __call__(self, delta, channel):
            self.feeds.append((delta, channel))

        def discard(self):
            self.feeds.clear()

    sink = _Sink()
    pbase.set_provider_delta_sink(sink)
    prov = OmpBridgeProvider(provider="gemini", model="gemini-2.5-pro")

    with pytest.raises(omp_bridge.ProviderRefused):
        prov.complete_ex("sys", [{"role": "user", "content": "hi"}])
    assert sink.feeds, "the fragment still displayed; only the RETURN is refused"


# --- 5. classification ------------------------------------------------------

def test_refusal_classifies_non_retryable_and_out_of_the_auth_bucket():
    exc = omp_bridge.ProviderRefused(
        "gemini declined this request (dangerous_content): blocked",
        status=400, provider="gemini", category="dangerous_content")
    stop, retryable = agent_turn.classify_provider_error(exc)
    # HTTP 400 alone would have landed in provider_auth and told the user their
    # key was bad. A refusal outranks the status code.
    assert stop == "provider_refused"
    assert retryable is False


def test_refusal_detail_names_the_vendor_not_the_bridge():
    exc = omp_bridge.ProviderRefused(
        "anthropic declined this request (bio): nope",
        provider="anthropic", category="bio")
    detail = agent_turn.format_provider_error_detail(exc, None)
    assert detail.startswith("anthropic declined this request on content policy")
    assert "category: bio" in detail
    for lie in ("BridgeUnavailable", "ProviderRefused", "Provider error"):
        assert lie not in detail


# --- 6. failover candidate selection ----------------------------------------

def test_failover_excludes_the_whole_vendor_family_one_row_each():
    from services.agent.role_models import refusal_failover_candidates

    cfg = {"enabled_providers": [
        {"provider": "anthropic", "model": "claude-opus-5"},
        {"provider": "anthropic-sub", "model": "claude-sonnet-5"},
        {"provider": "moonshot", "model": "kimi-k3"},
        {"provider": "kimi-sub", "model": "k3"},
        {"provider": "gemini", "model": "gemini-2.5-pro"},
    ]}
    got = refusal_failover_candidates(["anthropic"], cfg=cfg)
    providers = [c["provider"] for c in got]

    # The sibling subscription shares Anthropic's classifier — excluded.
    assert "anthropic" not in providers and "anthropic-sub" not in providers
    # moonshot/kimi-sub are one family; only one row is offered.
    assert len([p for p in providers if p in ("moonshot", "kimi-sub")]) == 1
    assert "gemini" in providers

    # Refusing everything leaves nothing, rather than looping a refused vendor.
    assert refusal_failover_candidates(
        ["anthropic", "moonshot", "gemini"], cfg=cfg) == []


# --- 7 & 8. the turn-level behaviour ----------------------------------------

class _RefusingProvider:
    """Declines every completion, the way Anthropic declined the pellet press."""

    def __init__(self, name="anthropic", category="bio"):
        self.name = name
        self.model = "claude-opus-5"
        self.category = category
        self.calls = 0

    def complete(self, system, messages, max_tokens=8000):
        self.calls += 1
        raise omp_bridge.ProviderRefused(
            f"{self.name} declined this request ({self.category}): no",
            provider=self.name, category=self.category)


class _AcceptingProvider:
    """A different vendor that answers the identical prompt.

    It runs a tool and THEN hands off, because the done-gate rejects a finish
    envelope with no evidence behind it — a bare ``{"done": true}`` first reply
    stops the turn as ``no_code``, which would make this fixture prove nothing
    about the failover.
    """

    def __init__(self, name="gemini"):
        self.name = name
        self.model = "gemini-2.5-pro"
        self.calls = 0
        self.seen_messages: list = []

    def complete(self, system, messages, max_tokens=8000):
        self.calls += 1
        self.seen_messages = list(messages)
        if self.calls == 1:
            return '```json\n{"tool": "measure_part", "args": {}}\n```'
        return '```json\n{"done": true, "handoff": {"summary": "built it"}}\n```'


def _install_failover(monkeypatch, enabled, resolved):
    """Point the failover at *enabled* rows, served by the *resolved* map."""
    from services.agent import config as agent_config

    monkeypatch.setattr(agent_config, "load_config",
                        lambda *a, **k: {"enabled_providers": list(enabled)})

    def fake_get_provider(name=None, cfg=None, role=None, **kwargs):
        if name not in resolved:
            raise RuntimeError(f"no provider {name!r}")
        return resolved[name]

    monkeypatch.setattr(agent_config, "get_provider", fake_get_provider)


def test_a_refused_turn_fails_over_to_another_vendor_and_finishes(monkeypatch):
    """The identical prompt reaches a different vendor, and that vendor owns the
    REST of the turn: a failover that forgot to rebind `provider` would refuse
    again — and pay for it — on every subsequent iteration."""
    refuser = _RefusingProvider()
    accepter = _AcceptingProvider()
    _install_failover(
        monkeypatch,
        enabled=[{"provider": "anthropic", "model": "claude-opus-5"},
                 {"provider": "gemini", "model": "gemini-2.5-pro"}],
        resolved={"gemini": accepter})

    res = agent_turn.run_turn("design a pellet press", refuser,
                              allowed_tools=["measure_part"],
                              tool_dispatch=lambda t, a: {"ok": True})

    assert res.success is True, res.result
    assert res.stop_reason == "confirmed"
    assert refuser.calls == 1, "the refusing vendor must not be paid twice"
    assert accepter.calls == 2, "the accepting vendor runs the whole turn"
    # The failover carries the SAME request, not a sanitised paraphrase.
    assert any("pellet press" in str(m.get("content"))
               for m in accepter.seen_messages)


def test_every_vendor_refusing_stops_honestly(monkeypatch):
    refuser = _RefusingProvider()
    second = _RefusingProvider(name="gemini", category="dangerous_content")
    _install_failover(
        monkeypatch,
        enabled=[{"provider": "anthropic", "model": "claude-opus-5"},
                 {"provider": "gemini", "model": "gemini-2.5-pro"}],
        resolved={"gemini": second})

    res = agent_turn.run_turn("design a pellet press", refuser,
                              allowed_tools=["measure_part"],
                              tool_dispatch=lambda t, a: {"ok": True})

    assert res.success is False
    assert res.stop_reason == "provider_refused", res.result
    detail = res.result["error_detail"]
    assert "anthropic declined this request on content policy" in detail
    assert "category: bio" in detail
    # The old lie must never come back.
    assert "BridgeUnavailable" not in detail
    assert second.calls == 1, "each alternative vendor is tried exactly once"


def test_no_alternative_provider_still_reports_the_refusal(monkeypatch):
    """A single-provider config has nowhere to go. That must still surface the
    refusal by name, not a generic provider_error."""
    refuser = _RefusingProvider()
    _install_failover(
        monkeypatch,
        enabled=[{"provider": "anthropic", "model": "claude-opus-5"}],
        resolved={})

    res = agent_turn.run_turn("design a pellet press", refuser,
                              allowed_tools=["measure_part"],
                              tool_dispatch=lambda t, a: {"ok": True})

    assert res.success is False
    assert res.stop_reason == "provider_refused", res.result
    assert refuser.calls == 1
